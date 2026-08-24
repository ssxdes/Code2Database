"""Cross-C2D foreign reference tracking + live sync.

Phase 1 of the cross-C2D sync design. Lets project B's C2D reference
functions in project A's C2D without merging the two databases. When
A updates, B can sync to detect renamed/deleted/added functions.

Tables (added to B's code2database.db via SCHEMA v13):
- foreign_refs: B's unresolved calls + cached A-side metadata
- watched_c2ds: list of monitored external C2Ds with mtime/size for change detection

Commands:
- c2d-add-foreign: register A as a watched C2D, resolve B's unresolved calls
- c2d-sync-foreign: detect A changes, re-resolve foreign_refs
- c2d-list-foreign: list watched C2Ds with sync status
- c2d-remove-foreign: unregister A, downgrade foreign_refs to out_end

The resolution uses three strategies in order:
1. exact_id: foreign_node_id directly matches A's function id (fastest)
2. exact_name: invoked_name matches A's function name (after project prefix)
3. suffix: invoked_name appears as suffix of A's function id (loose match)

SQLite ATTACH DATABASE is used to query A's db without merging.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Schema (SCHEMA v13 — added to SQLiteStore._create_tables / _migrate_schema)
# ---------------------------------------------------------------------------

FOREIGN_REFS_SCHEMA = """
CREATE TABLE IF NOT EXISTS foreign_refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    local_node_id TEXT NOT NULL,
    invoked_name TEXT NOT NULL,
    invoked_signature TEXT,
    foreign_c2d_path TEXT NOT NULL,
    foreign_project_name TEXT,
    foreign_node_id TEXT,
    foreign_name TEXT,
    foreign_domain TEXT,
    foreign_source_file TEXT,
    foreign_signature TEXT,
    status TEXT NOT NULL DEFAULT 'unresolved',
    resolution_strategy TEXT,
    last_resolved_at TEXT,
    call_order INTEGER,
    call_condition TEXT
);
CREATE INDEX IF NOT EXISTS idx_foreign_refs_local ON foreign_refs(local_node_id);
CREATE INDEX IF NOT EXISTS idx_foreign_refs_status ON foreign_refs(status);
CREATE INDEX IF NOT EXISTS idx_foreign_refs_foreign_c2d
    ON foreign_refs(foreign_c2d_path);
"""

WATCHED_C2DS_SCHEMA = """
CREATE TABLE IF NOT EXISTS watched_c2ds (
    c2d_path TEXT PRIMARY KEY,
    project_name TEXT,
    db_mtime_at_sync TEXT,
    db_size_at_sync INTEGER,
    functions_count_at_sync INTEGER,
    last_synced_at TEXT NOT NULL,
    sync_status TEXT NOT NULL DEFAULT 'unknown'
);
"""


def _ensure_foreign_tables(conn: sqlite3.Connection) -> None:
    """Idempotent table creation (used when _kb_connect created a fresh db)."""
    try:
        conn.executescript(FOREIGN_REFS_SCHEMA + WATCHED_C2DS_SCHEMA)
    except sqlite3.OperationalError:
        pass


def _db_path(graph_dir: str) -> str:
    return os.path.join(graph_dir, "code2database.db")


def _connect(graph_dir: str) -> Optional[sqlite3.Connection]:
    """Connect to B's code2database.db, ensuring foreign tables exist."""
    db_path = _db_path(graph_dir)
    if not os.path.exists(db_path):
        # Create empty db so foreign_* commands work without prior build
        Path(db_path).touch()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _ensure_foreign_tables(conn)
    return conn


def _foreign_db_path(foreign_c2d_path: str) -> str:
    return os.path.join(foreign_c2d_path, "code2database.db")


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

def _get_db_signature(db_path: str) -> Dict[str, Any]:
    """Get mtime/size/functions-count for change detection."""
    sig = {"mtime": "", "size": 0, "functions_count": 0}
    if not os.path.exists(db_path):
        sig["missing"] = True
        return sig
    try:
        stat = os.stat(db_path)
        sig["mtime"] = datetime.fromtimestamp(stat.st_mtime).isoformat()
        sig["size"] = stat.st_size
    except OSError:
        sig["missing"] = True
        return sig
    # Best-effort functions count
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute("SELECT COUNT(*) FROM functions").fetchone()
        sig["functions_count"] = row[0] if row else 0
        conn.close()
    except sqlite3.Error:
        pass  # foreign db may not have functions table yet
    return sig


# ---------------------------------------------------------------------------
# Resolution strategies
# ---------------------------------------------------------------------------

def _resolve_by_exact_id(foreign_conn: sqlite3.Connection,
                          node_id: str) -> Optional[sqlite3.Row]:
    """Strategy 1: look up by exact node id in foreign db."""
    try:
        return foreign_conn.execute(
            "SELECT id, name, domain, source_file, line_number, signature "
            "FROM functions WHERE id = ? LIMIT 1",
            (node_id,)
        ).fetchone()
    except sqlite3.Error:
        return None


def _resolve_by_exact_name(foreign_conn: sqlite3.Connection,
                            invoked_name: str,
                            project_name: str = "",
                            invoked_signature: str = "",
                            table_prefix: str = "") -> Optional[sqlite3.Row]:
    """Strategy 2: invoked_name matches a function in foreign db.

    If project_name is given, the function's id should start with
    `<project_name>_` (because build-multi prefixed it).

    C2 fix: if invoked_signature is provided, use it to disambiguate
    C++ overloads (same name, different signature). Returns the best
    match: exact signature first, then any name match.

    table_prefix: if querying an ATTACHed db, pass e.g. 'foreign_db.'
    so the FROM clause becomes 'foreign_db.functions'.
    """
    if not invoked_name:
        return None
    tbl = f"{table_prefix}functions" if table_prefix else "functions"
    candidates: List[sqlite3.Row] = []
    # Try with project prefix first (more specific)
    if project_name:
        prefixed = f"{project_name}_{invoked_name.lower()}"
        try:
            row = foreign_conn.execute(
                f"SELECT id, name, domain, source_file, line_number, signature "
                f"FROM {tbl} WHERE id = ? LIMIT 1",
                (prefixed,)
            ).fetchone()
            if row:
                candidates.append(row)
        except sqlite3.Error:
            pass
    # Fall back to name match (without project prefix)
    if not candidates:
        try:
            rows = foreign_conn.execute(
                f"SELECT id, name, domain, source_file, line_number, signature "
                f"FROM {tbl} WHERE name = ? LIMIT 10",
                (invoked_name,)
            ).fetchall()
            candidates.extend(rows)
        except sqlite3.Error:
            pass
    if not candidates:
        return None
    # C2: if signature provided, pick the exact match
    if invoked_signature and len(candidates) > 1:
        for c in candidates:
            if c["signature"] and invoked_signature in c["signature"]:
                return c
    # Return first candidate (best-effort)
    return candidates[0]


def _resolve_by_suffix(foreign_conn: sqlite3.Connection,
                        invoked_name: str,
                        limit: int = 5) -> List[sqlite3.Row]:
    """Strategy 3: invoked_name is a suffix of foreign function id."""
    if not invoked_name:
        return []
    # Match functions whose id ends with _<invoked_name> (lowercased)
    pattern = f"%_{invoked_name.lower()}"
    try:
        return foreign_conn.execute(
            "SELECT id, name, domain, source_file, line_number, signature "
            "FROM functions WHERE LOWER(id) LIKE ? LIMIT ?",
            (pattern, limit)
        ).fetchall()
    except sqlite3.Error:
        return []


# ---------------------------------------------------------------------------
# add-foreign
# ---------------------------------------------------------------------------

def add_foreign(graph_dir: str, foreign_c2d_path: str,
                project_name: str = "", rescan_unresolved: bool = False,
                verbose: bool = True) -> Dict[str, Any]:
    """Register a foreign C2D and resolve B's unresolved calls against it.

    Returns summary with counts.
    """
    foreign_db = _foreign_db_path(foreign_c2d_path)
    if not os.path.exists(foreign_db):
        return {"error": f"foreign c2d db not found: {foreign_db}"}
    # S4: verify it's a valid SQLite db (not /etc/passwd via symlink)
    try:
        test_conn = sqlite3.connect(f"file:{foreign_db}?mode=ro", uri=True)
        test_conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        test_conn.close()
    except sqlite3.Error:
        return {"error": f"foreign db is not a valid SQLite database: {foreign_db}"}
    conn = _connect(graph_dir)
    summary: Dict[str, Any] = {
        "foreign_c2d_path": foreign_c2d_path,
        "project_name": project_name,
        "added": True,
        "resolved_count": 0,
        "unresolved_count": 0,
        "stale_count": 0,
        "total_foreign_refs": 0,
    }
    try:
        # Step 1: Insert/update watched_c2ds
        sig = _get_db_signature(foreign_db)
        conn.execute(
            "INSERT OR REPLACE INTO watched_c2ds "
            "(c2d_path, project_name, db_mtime_at_sync, db_size_at_sync, "
            "functions_count_at_sync, last_synced_at, sync_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (foreign_c2d_path, project_name, sig.get("mtime", ""),
             sig.get("size", 0), sig.get("functions_count", 0),
             datetime.now().isoformat(), "ok")
        )
        conn.commit()
        # Step 2: ATTACH foreign db read-only
        conn.execute(
            f"ATTACH DATABASE 'file:{foreign_db}?mode=ro' AS foreign_db"
        )
        # Step 3: Find B's unresolved calls
        # An unresolved call is an edge in B.edges where invoked_id is empty
        # OR invoked_id not present in B.functions (dangling)
        unresolved_edges = conn.execute(
            "SELECT e.invoker_id, e.invoked_id, e.call_order, "
            "e.call_condition, "
            # Best-effort: extract function name from invoked_id if it
            # looks like a name (legacy id format: domain_name)
            "REPLACE(REPLACE(e.invoked_id, '*', ''), 'external_', '') AS invoked_name_guess "
            "FROM edges e "
            "WHERE e.invoked_id = '' "
            "   OR e.invoked_id NOT IN (SELECT id FROM functions) "
            "   OR e.invoked_id LIKE 'external_%' "
            "LIMIT 10000"
        ).fetchall()
        if verbose:
            print(f"[c2d-add-foreign] found {len(unresolved_edges)} "
                  f"unresolved edges in B", file=sys.stderr)
        # Step 4: Resolve each unresolved edge
        for edge in unresolved_edges:
            invoked_name_guess = edge["invoked_name_guess"] or ""
            # Skip empty guesses (can't resolve without a name)
            if not invoked_name_guess:
                continue
            # Try strategies in order
            resolved_row = None
            strategy = None
            # Strategy 2: exact name (with project prefix) — query foreign db
            resolved_row = _resolve_by_exact_name(
                conn, invoked_name_guess, project_name,
                table_prefix="foreign_db.")
            if resolved_row:
                strategy = "exact_name"
            else:
                # Strategy 3: suffix
                candidates = _resolve_by_suffix(conn, invoked_name_guess)
                if len(candidates) == 1:
                    resolved_row = candidates[0]
                    strategy = "suffix"
            if resolved_row:
                # Insert/update foreign_refs
                conn.execute(
                    "INSERT OR REPLACE INTO foreign_refs "
                    "(local_node_id, invoked_name, foreign_c2d_path, "
                    "foreign_project_name, foreign_node_id, foreign_name, "
                    "foreign_domain, foreign_source_file, foreign_signature, "
                    "status, resolution_strategy, last_resolved_at, "
                    "call_order, call_condition) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (edge["invoker_id"], invoked_name_guess,
                     foreign_c2d_path, project_name,
                     resolved_row["id"], resolved_row["name"],
                     resolved_row["domain"], resolved_row["source_file"],
                     resolved_row["signature"], "resolved", strategy,
                     datetime.now().isoformat(),
                     edge["call_order"], edge["call_condition"])
                )
                summary["resolved_count"] += 1
            else:
                # Insert as unresolved
                conn.execute(
                    "INSERT OR REPLACE INTO foreign_refs "
                    "(local_node_id, invoked_name, foreign_c2d_path, "
                    "foreign_project_name, status, call_order, call_condition) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (edge["invoker_id"], invoked_name_guess,
                     foreign_c2d_path, project_name,
                     "unresolved", edge["call_order"], edge["call_condition"])
                )
                summary["unresolved_count"] += 1
        conn.execute("DETACH DATABASE foreign_db")
        conn.commit()
        # Count totals
        total = conn.execute(
            "SELECT COUNT(*) FROM foreign_refs WHERE foreign_c2d_path = ?",
            (foreign_c2d_path,)
        ).fetchone()[0]
        summary["total_foreign_refs"] = total
    except sqlite3.Error as e:
        summary["error"] = str(e)
        try:
            conn.execute("DETACH DATABASE foreign_db")
        except sqlite3.Error:
            pass
    finally:
        conn.close()
    return summary


# ---------------------------------------------------------------------------
# sync-foreign
# ---------------------------------------------------------------------------

def sync_foreign(graph_dir: str, foreign_c2d_path: str = "",
                 verbose: bool = True) -> Dict[str, Any]:
    """Detect changes in foreign C2Ds and re-resolve foreign_refs.

    If foreign_c2d_path is empty, sync all watched C2Ds.
    """
    conn = _connect(graph_dir)
    summary: Dict[str, Any] = {
        "synced_c2ds": [],
        "stale_marked": 0,
        "deleted_marked": 0,
        "newly_resolved": 0,
        "still_unresolved": 0,
    }
    try:
        # Get watched c2ds to sync
        if foreign_c2d_path:
            watched = conn.execute(
                "SELECT * FROM watched_c2ds WHERE c2d_path = ?",
                (foreign_c2d_path,)
            ).fetchall()
        else:
            watched = conn.execute("SELECT * FROM watched_c2ds").fetchall()
        if not watched:
            summary["message"] = "no watched c2ds to sync"
            return summary
        for w in watched:
            c2d_path = w["c2d_path"]
            project_name = w["project_name"] or ""
            fdb_path = _foreign_db_path(c2d_path)
            current_sig = _get_db_signature(fdb_path)
            if current_sig.get("missing"):
                # Foreign db is gone — mark all refs as orphaned
                conn.execute(
                    "UPDATE foreign_refs SET status = 'deleted' "
                    "WHERE foreign_c2d_path = ? AND status = 'resolved'",
                    (c2d_path,)
                )
                conn.execute(
                    "UPDATE watched_c2ds SET sync_status = 'missing' "
                    "WHERE c2d_path = ?",
                    (c2d_path,)
                )
                summary["deleted_marked"] += conn.execute(
                    "SELECT changes()"
                ).fetchone()[0]
                summary["synced_c2ds"].append({
                    "c2d_path": c2d_path,
                    "status": "missing",
                })
                continue
            # Check if mtime/size changed
            mtime_changed = current_sig.get("mtime") != w["db_mtime_at_sync"]
            size_changed = current_sig.get("size") != w["db_size_at_sync"]
            count_changed = (current_sig.get("functions_count", 0)
                              != w["functions_count_at_sync"])
            if not (mtime_changed or size_changed or count_changed):
                summary["synced_c2ds"].append({
                    "c2d_path": c2d_path,
                    "status": "unchanged",
                })
                continue
            if verbose:
                print(f"[c2d-sync-foreign] {c2d_path}: changed "
                      f"(mtime={mtime_changed}, size={size_changed}, "
                      f"count={count_changed})", file=sys.stderr)
            # ATTACH foreign db (new version)
            conn.execute(
                f"ATTACH DATABASE 'file:{fdb_path}?mode=ro' AS foreign_db"
            )
            # Step 1: verify existing resolved refs still exist
            resolved = conn.execute(
                "SELECT id, foreign_node_id, invoked_name, "
                "invoked_signature FROM foreign_refs "
                "WHERE foreign_c2d_path = ? AND status = 'resolved'",
                (c2d_path,)
            ).fetchall()
            for r in resolved:
                if r["foreign_node_id"]:
                    # Try exact_id strategy
                    new_row = _resolve_by_exact_id(
                        conn, r["foreign_node_id"])
                    if new_row:
                        # Still exists — update metadata
                        conn.execute(
                            "UPDATE foreign_refs SET "
                            "foreign_name = ?, foreign_domain = ?, "
                            "foreign_source_file = ?, foreign_signature = ?, "
                            "last_resolved_at = ? WHERE id = ?",
                            (new_row["name"], new_row["domain"],
                             new_row["source_file"], new_row["signature"],
                             datetime.now().isoformat(), r["id"])
                        )
                    else:
                        # Gone — try re-resolve by name
                        new_row = _resolve_by_exact_name(
                            conn, r["invoked_name"], project_name,
                            table_prefix="foreign_db.")
                        if new_row:
                            conn.execute(
                                "UPDATE foreign_refs SET "
                                "foreign_node_id = ?, foreign_name = ?, "
                                "foreign_domain = ?, foreign_source_file = ?, "
                                "foreign_signature = ?, "
                                "resolution_strategy = 'exact_name', "
                                "last_resolved_at = ?, status = 'resolved' "
                                "WHERE id = ?",
                                (new_row["id"], new_row["name"],
                                 new_row["domain"], new_row["source_file"],
                                 new_row["signature"],
                                 datetime.now().isoformat(), r["id"])
                            )
                            summary["stale_marked"] += 0  # auto-recovered
                        else:
                            # Truly gone — mark as deleted
                            conn.execute(
                                "UPDATE foreign_refs SET status = 'deleted' "
                                "WHERE id = ?",
                                (r["id"],)
                            )
                            summary["deleted_marked"] += 1
            # Step 2: re-resolve unresolved + deleted + stale
            unresolved = conn.execute(
                "SELECT id, local_node_id, invoked_name, invoked_signature "
                "FROM foreign_refs "
                "WHERE foreign_c2d_path = ? "
                "AND status IN ('unresolved', 'deleted', 'stale')",
                (c2d_path,)
            ).fetchall()
            for r in unresolved:
                new_row = _resolve_by_exact_name(
                    conn, r["invoked_name"], project_name,
                    table_prefix="foreign_db.")
                if new_row:
                    conn.execute(
                        "UPDATE foreign_refs SET "
                        "foreign_node_id = ?, foreign_name = ?, "
                        "foreign_domain = ?, foreign_source_file = ?, "
                        "foreign_signature = ?, status = 'resolved', "
                        "resolution_strategy = 'exact_name', "
                        "last_resolved_at = ? WHERE id = ?",
                        (new_row["id"], new_row["name"], new_row["domain"],
                         new_row["source_file"], new_row["signature"],
                         datetime.now().isoformat(), r["id"])
                    )
                    summary["newly_resolved"] += 1
                else:
                    summary["still_unresolved"] += 1
            # Update watched_c2ds signature
            conn.execute(
                "UPDATE watched_c2ds SET "
                "db_mtime_at_sync = ?, db_size_at_sync = ?, "
                "functions_count_at_sync = ?, "
                "last_synced_at = ?, sync_status = 'ok' "
                "WHERE c2d_path = ?",
                (current_sig.get("mtime", ""), current_sig.get("size", 0),
                 current_sig.get("functions_count", 0),
                 datetime.now().isoformat(), c2d_path)
            )
            conn.execute("DETACH DATABASE foreign_db")
            summary["synced_c2ds"].append({
                "c2d_path": c2d_path,
                "status": "synced",
            })
        conn.commit()
    except sqlite3.Error as e:
        summary["error"] = str(e)
        try:
            conn.execute("DETACH DATABASE foreign_db")
        except sqlite3.Error:
            pass
    finally:
        conn.close()
    return summary


# ---------------------------------------------------------------------------
# list-foreign
# ---------------------------------------------------------------------------

def list_foreign(graph_dir: str) -> List[Dict[str, Any]]:
    """List all watched foreign C2Ds with sync status + ref counts."""
    conn = _connect(graph_dir)
    try:
        rows = conn.execute("SELECT * FROM watched_c2ds").fetchall()
        result = []
        for r in rows:
            c2d_path = r["c2d_path"]
            # Count foreign_refs by status
            counts_row = conn.execute(
                "SELECT "
                "COUNT(*) AS total, "
                "SUM(CASE WHEN status='resolved' THEN 1 ELSE 0 END) AS resolved, "
                "SUM(CASE WHEN status='unresolved' THEN 1 ELSE 0 END) AS unresolved, "
                "SUM(CASE WHEN status='stale' THEN 1 ELSE 0 END) AS stale, "
                "SUM(CASE WHEN status='deleted' THEN 1 ELSE 0 END) AS deleted "
                "FROM foreign_refs WHERE foreign_c2d_path = ?",
                (c2d_path,)
            ).fetchone()
            result.append({
                "c2d_path": c2d_path,
                "project_name": r["project_name"] or "",
                "sync_status": r["sync_status"],
                "last_synced_at": r["last_synced_at"],
                "functions_count_at_sync": r["functions_count_at_sync"],
                "db_mtime_at_sync": r["db_mtime_at_sync"],
                "foreign_refs_count": counts_row["total"] or 0,
                "resolved_count": counts_row["resolved"] or 0,
                "unresolved_count": counts_row["unresolved"] or 0,
                "stale_count": counts_row["stale"] or 0,
                "deleted_count": counts_row["deleted"] or 0,
            })
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# remove-foreign
# ---------------------------------------------------------------------------

def remove_foreign(graph_dir: str, foreign_c2d_path: str) -> Dict[str, Any]:
    """Unregister a foreign C2D. Foreign_refs rows are marked 'orphaned'
    (preserved for audit) rather than deleted.
    """
    conn = _connect(graph_dir)
    summary: Dict[str, Any] = {
        "foreign_c2d_path": foreign_c2d_path,
        "removed": True,
        "orphaned_refs": 0,
    }
    try:
        # Mark foreign_refs as orphaned (preserve audit trail)
        cur = conn.execute(
            "UPDATE foreign_refs SET status = 'orphaned' "
            "WHERE foreign_c2d_path = ?",
            (foreign_c2d_path,)
        )
        summary["orphaned_refs"] = cur.rowcount
        # Delete from watched_c2ds
        conn.execute(
            "DELETE FROM watched_c2ds WHERE c2d_path = ?",
            (foreign_c2d_path,)
        )
        conn.commit()
    except sqlite3.Error as e:
        summary["error"] = str(e)
    finally:
        conn.close()
    return summary


# ---------------------------------------------------------------------------
# CLI handlers
# ---------------------------------------------------------------------------

def cmd_c2d_add_foreign(args):
    """CLI handler for c2d-add-foreign."""
    summary = add_foreign(
        graph_dir=args.graph,
        foreign_c2d_path=args.foreign_c2d,
        project_name=getattr(args, "project_name", "") or "",
        rescan_unresolved=getattr(args, "rescan_unresolved", False),
        verbose=True,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_c2d_sync_foreign(args):
    """CLI handler for c2d-sync-foreign."""
    summary = sync_foreign(
        graph_dir=args.graph,
        foreign_c2d_path=getattr(args, "foreign_c2d", "") or "",
        verbose=True,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_c2d_list_foreign(args):
    """CLI handler for c2d-list-foreign."""
    result = list_foreign(args.graph)
    if not result:
        print("No watched foreign C2Ds.")
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_c2d_remove_foreign(args):
    """CLI handler for c2d-remove-foreign."""
    summary = remove_foreign(args.graph, args.foreign_c2d)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
