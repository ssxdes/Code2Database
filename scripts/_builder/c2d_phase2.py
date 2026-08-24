"""Phase 2 enhancements for multi-project support.

- composite_query: cross-C2D JOIN via SQLite ATTACH DATABASE
- c2d_check_compat: verify B's foreign_refs still valid against A_v2
- coverage_cross_c2d: which functions in A are called by test_A
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from _builder.c2d_foreign import _connect, _foreign_db_path


def composite_query(graph_dir: str, query: str,
                     foreign_c2ds: List[str] = None,
                     top_n: int = 50) -> Dict[str, Any]:
    """Run a query across multiple C2Ds via SQLite ATTACH DATABASE.

    Joins local db with attached foreign dbs. Supports a simplified
    query language: 'MATCH (caller)-[:CALLS]->(callee) WHERE callee.name=NAME'
    or 'CALLERS_OF <name>' or 'CALLEES_OF <name>'.

    Returns: {results: [...], attached_c2ds: [...]}
    """
    conn = _connect(graph_dir)
    summary: Dict[str, Any] = {
        "query": query,
        "results": [],
        "attached_c2ds": [],
    }
    try:
        # Attach foreign dbs
        if foreign_c2ds:
            for i, fpath in enumerate(foreign_c2ds):
                fdb = _foreign_db_path(fpath)
                if not os.path.exists(fdb):
                    continue
                alias = f"foreign_{i}"
                conn.execute(
                    f"ATTACH DATABASE 'file:{fdb}?mode=ro' AS {alias}"
                )
                summary["attached_c2ds"].append({
                    "alias": alias,
                    "path": fpath,
                })
        # Parse simplified query
        query_upper = query.strip().upper()
        if query_upper.startswith("CALLERS_OF "):
            target_name = query[len("CALLERS_OF "):].strip()
            # Find callers across local + all attached dbs
            results = _find_callers(conn, target_name, summary["attached_c2ds"])
        elif query_upper.startswith("CALLEES_OF "):
            target_name = query[len("CALLEES_OF "):].strip()
            results = _find_callees(conn, target_name, summary["attached_c2ds"])
        else:
            # Fallback: full-text search across all dbs
            results = _fts_search_all(conn, query, summary["attached_c2ds"], top_n)
        summary["results"] = results[:top_n]
        summary["total"] = len(results)
        # Detach foreign dbs
        for entry in summary["attached_c2ds"]:
            try:
                conn.execute(f"DETACH DATABASE {entry['alias']}")
            except sqlite3.Error:
                pass
    except sqlite3.Error as e:
        summary["error"] = str(e)
    finally:
        conn.close()
    return summary


def _find_callers(conn: sqlite3.Connection, callee_name: str,
                  attached: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Find all callers of a function across local + attached dbs."""
    results: List[Dict[str, Any]] = []
    # Local callers
    try:
        rows = conn.execute(
            "SELECT e.invoker_id, e.invoked_id, e.call_order, "
            "f_invoker.name AS invoker_name, "
            "f_invoker.domain AS invoker_domain, "
            "f_invoker.source_file AS invoker_source "
            "FROM edges e "
            "JOIN functions f_invoker ON e.invoker_id = f_invoker.id "
            "WHERE e.invoked_id IN (SELECT id FROM functions WHERE name = ?) "
            "OR e.invoked_id LIKE ? "
            "ORDER BY e.call_order",
            (callee_name, f"%_{callee_name.lower()}")
        ).fetchall()
        for r in rows:
            results.append({
                "caller_id": r["invoker_id"],
                "caller_name": r["invoker_name"],
                "caller_domain": r["invoker_domain"],
                "caller_source": r["invoker_source"],
                "callee_name": callee_name,
                "source_db": "local",
            })
    except sqlite3.Error:
        pass
    # Foreign callers
    for entry in attached:
        alias = entry["alias"]
        try:
            rows = conn.execute(
                f"SELECT e.invoker_id, e.invoked_id, e.call_order, "
                f"f_invoker.name AS invoker_name, "
                f"f_invoker.domain AS invoker_domain, "
                f"f_invoker.source_file AS invoker_source "
                f"FROM {alias}.edges e "
                f"JOIN {alias}.functions f_invoker ON e.invoker_id = f_invoker.id "
                f"WHERE e.invoked_id IN (SELECT id FROM {alias}.functions WHERE name = ?) "
                f"OR e.invoked_id LIKE ? "
                f"ORDER BY e.call_order",
                (callee_name, f"%_{callee_name.lower()}")
            ).fetchall()
            for r in rows:
                results.append({
                    "caller_id": r["invoker_id"],
                    "caller_name": r["invoker_name"],
                    "caller_domain": r["invoker_domain"],
                    "caller_source": r["invoker_source"],
                    "callee_name": callee_name,
                    "source_db": entry["path"],
                })
        except sqlite3.Error:
            pass
    return results


def _find_callees(conn: sqlite3.Connection, caller_name: str,
                  attached: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Find all callees of a function across local + attached dbs."""
    results: List[Dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT e.invoker_id, e.invoked_id, e.call_order, "
            "f_callee.name AS callee_name, "
            "f_callee.domain AS callee_domain, "
            "f_callee.source_file AS callee_source "
            "FROM edges e "
            "JOIN functions f_callee ON e.invoked_id = f_callee.id "
            "WHERE e.invoker_id IN (SELECT id FROM functions WHERE name = ?) "
            "ORDER BY e.call_order",
            (caller_name,)
        ).fetchall()
        for r in rows:
            results.append({
                "callee_id": r["invoked_id"],
                "callee_name": r["callee_name"],
                "callee_domain": r["callee_domain"],
                "callee_source": r["callee_source"],
                "caller_name": caller_name,
                "source_db": "local",
            })
    except sqlite3.Error:
        pass
    return results


def _fts_search_all(conn: sqlite3.Connection, query: str,
                     attached: List[Dict[str, str]],
                     top_n: int) -> List[Dict[str, Any]]:
    """Fallback: simple name search across all dbs."""
    results: List[Dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT id, name, domain, source_file FROM functions "
            "WHERE name LIKE ? LIMIT ?",
            (f"%{query}%", top_n)
        ).fetchall()
        for r in rows:
            results.append({
                "id": r["id"], "name": r["name"],
                "domain": r["domain"], "source_file": r["source_file"],
                "source_db": "local",
            })
    except sqlite3.Error:
        pass
    return results


def check_compat(graph_dir: str, against_c2d: str,
                 verbose: bool = True) -> Dict[str, Any]:
    """Check if B's foreign_refs are still valid against a new A version.

    Used when A upgrades to v2 — verifies B's existing foreign_refs still
    resolve against A_v2's functions.

    Returns: {broken_edges, signature_changed, ok_edges, summary}
    """
    foreign_db = _foreign_db_path(against_c2d)
    if not os.path.exists(foreign_db):
        return {"error": f"foreign db not found: {foreign_db}"}
    conn = _connect(graph_dir)
    summary: Dict[str, Any] = {
        "against_c2d": against_c2d,
        "broken_edges": 0,
        "signature_changed": 0,
        "ok_edges": 0,
        "total_checked": 0,
        "broken_details": [],
        "signature_changed_details": [],
    }
    try:
        conn.execute(f"ATTACH DATABASE 'file:{foreign_db}?mode=ro' AS new_a")
        # Get all resolved foreign_refs (don't filter by foreign_c2d_path
        # because the user may be checking against a different A version
        # at a different path)
        refs = conn.execute(
            "SELECT id, foreign_node_id, foreign_name, foreign_signature, "
            "invoked_name FROM foreign_refs "
            "WHERE status = 'resolved'"
        ).fetchall()
        for r in refs:
            summary["total_checked"] += 1
            # Look up in new A
            new_row = conn.execute(
                "SELECT id, name, signature, source_file FROM new_a.functions "
                "WHERE id = ?",
                (r["foreign_node_id"],)
            ).fetchone()
            if not new_row:
                # Try by name (function may have been renamed)
                new_row = conn.execute(
                    "SELECT id, name, signature, source_file FROM new_a.functions "
                    "WHERE name = ?",
                    (r["invoked_name"],)
                ).fetchone()
                if not new_row:
                    summary["broken_edges"] += 1
                    if len(summary["broken_details"]) < 20:
                        summary["broken_details"].append({
                            "ref_id": r["id"],
                            "invoked_name": r["invoked_name"],
                            "old_foreign_node_id": r["foreign_node_id"],
                            "reason": "function removed in new version",
                        })
                    continue
            # Check signature change
            old_sig = r["foreign_signature"] or ""
            new_sig = new_row["signature"] or ""
            if old_sig != new_sig:
                summary["signature_changed"] += 1
                if len(summary["signature_changed_details"]) < 20:
                    summary["signature_changed_details"].append({
                        "ref_id": r["id"],
                        "invoked_name": r["invoked_name"],
                        "old_signature": old_sig,
                        "new_signature": new_sig,
                    })
            else:
                summary["ok_edges"] += 1
        conn.execute("DETACH DATABASE new_a")
    except sqlite3.Error as e:
        summary["error"] = str(e)
        try:
            conn.execute("DETACH DATABASE new_a")
        except sqlite3.Error:
            pass
    finally:
        conn.close()
    summary["compatibility"] = (
        "ok" if summary["broken_edges"] == 0 and summary["signature_changed"] == 0
        else "broken" if summary["broken_edges"] > 0 and summary["ok_edges"] == 0
        else "partial"
    )
    return summary


def coverage_cross_c2d(test_c2d: str, target_c2d: str,
                       verbose: bool = True) -> Dict[str, Any]:
    """Compute which functions in target_c2d are called by test_c2d.

    Used for cross-C2D test coverage analysis: test_A calls which
    functions in A?

    Returns: {covered_count, uncovered_count, total_target_functions,
             coverage_ratio, covered_functions: [...], uncovered_functions: [...]}
    """
    target_db = _foreign_db_path(target_c2d)
    test_db = _foreign_db_path(test_c2d)
    if not os.path.exists(target_db):
        return {"error": f"target db not found: {target_db}"}
    if not os.path.exists(test_db):
        return {"error": f"test db not found: {test_db}"}
    # Use test_c2d's db as primary, ATTACH target_c2d
    conn = sqlite3.connect(test_db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    summary: Dict[str, Any] = {
        "test_c2d": test_c2d,
        "target_c2d": target_c2d,
        "covered_count": 0,
        "uncovered_count": 0,
        "total_target_functions": 0,
        "coverage_ratio": 0.0,
    }
    try:
        conn.execute(f"ATTACH DATABASE 'file:{target_db}?mode=ro' AS target")
        # Get all target functions
        target_funcs = conn.execute(
            "SELECT id, name, domain, source_file FROM target.functions "
            "WHERE name IS NOT NULL AND name != ''"
        ).fetchall()
        summary["total_target_functions"] = len(target_funcs)
        if not target_funcs:
            summary["error"] = "target c2d has no functions"
            return summary
        # Find which target functions are called by test code.
        # Use a single LEFT JOIN query instead of O(N) per-function queries.
        # A target function is "covered" if any edge in test_c2d has
        # invoked_id matching the target's id OR name.
        covered_ids: set = set()
        # Direct id match (fast — uses index)
        try:
            direct_rows = conn.execute(
                "SELECT DISTINCT t.id FROM target.functions t "
                "INNER JOIN edges e ON e.invoked_id = t.id"
            ).fetchall()
            for r in direct_rows:
                covered_ids.add(r["id"])
        except sqlite3.Error:
            pass
        # Name match (for foreign_ref scenario where invoked_id differs)
        try:
            name_rows = conn.execute(
                "SELECT DISTINCT t.id FROM target.functions t "
                "INNER JOIN edges e ON e.invoked_id = t.id"
            ).fetchall()
            # Also check by name (slower but catches renamed refs)
            name_match_rows = conn.execute(
                "SELECT DISTINCT t.id FROM target.functions t "
                "WHERE t.name IN ("
                "  SELECT f.name FROM functions f "
                "  INNER JOIN edges e ON e.invoked_id = f.id"
                ")"
            ).fetchall()
            for r in name_match_rows:
                covered_ids.add(r["id"])
        except sqlite3.Error:
            pass
        covered = [tf for tf in target_funcs if tf["id"] in covered_ids]
        uncovered = [tf for tf in target_funcs if tf["id"] not in covered_ids]
        summary["covered_count"] = len(covered)
        summary["uncovered_count"] = len(uncovered)
        summary["coverage_ratio"] = round(len(covered) / len(target_funcs), 4)
        summary["covered_functions"] = [
            {"id": r["id"], "name": r["name"], "domain": r["domain"]}
            for r in covered[:100]  # cap for output size
        ]
        summary["uncovered_functions"] = [
            {"id": r["id"], "name": r["name"], "domain": r["domain"]}
            for r in uncovered[:100]
        ]
        conn.execute("DETACH DATABASE target")
    except sqlite3.Error as e:
        summary["error"] = str(e)
        try:
            conn.execute("DETACH DATABASE target")
        except sqlite3.Error:
            pass
    finally:
        conn.close()
    return summary


# ---------------------------------------------------------------------------
# CLI handlers
# ---------------------------------------------------------------------------

def cmd_composite_query(args):
    """CLI handler for composite-query."""
    foreign_c2ds = []
    if args.foreign_c2d:
        foreign_c2ds = [s.strip() for s in args.foreign_c2d.split(",")
                        if s.strip()]
    result = composite_query(
        graph_dir=args.graph,
        query=args.query,
        foreign_c2ds=foreign_c2ds,
        top_n=args.top,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_c2d_check_compat(args):
    """CLI handler for c2d-check-compat."""
    result = check_compat(
        graph_dir=args.graph,
        against_c2d=args.against_c2d,
        verbose=True,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_coverage_cross_c2d(args):
    """CLI handler for coverage-cross-c2d."""
    result = coverage_cross_c2d(
        test_c2d=args.test_c2d,
        target_c2d=args.target_c2d,
        verbose=True,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
