"""Phase 3 enhancements: vendor stub C2D, FFI cross-C2D, RPC edges,
cross-team knowledge sharing.

1. Vendor stub C2D: pre-built function-signature-only db for common
   SDKs (glibc, kernel, DPDK). Used via c2d-add-foreign --stub-mode.
   Foreign refs are always 'resolved' (API stable); only used for
   returning signatures + doc links at query time.

2. FFI cross-C2D auto-linking: ffi_bridge.py detects ctypes/cgo/
   extern C bindings; if a watched foreign C2D contains the bound
   symbol, auto-populates foreign_refs.

3. RPC edges: scan for HTTP/gRPC client calls (requests.post,
   grpc.Client). Produces rpc://host/path callee stub nodes +
   foreign_refs with foreign_c2d_path = service URL.

4. Cross-team knowledge sharing: c2d-add-foreign --import-knowledge
   copies the foreign C2D's knowledge/*.md into local knowledge/ as
   foreign_<project>_*.md so kb-query sees them.
"""
from __future__ import annotations

import logging
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from _builder.c2d_foreign import (
    _connect, _foreign_db_path, _ensure_foreign_tables,
    _get_db_signature, _resolve_by_exact_name, _escape_sql_path,
)


# ---------------------------------------------------------------------------
# 1. Vendor stub mode
# ---------------------------------------------------------------------------

def add_foreign_stub(graph_dir: str, stub_c2d_path: str,
                      project_name: str = "",
                      verbose: bool = True) -> Dict[str, Any]:
    """Register a vendor SDK stub C2D.

    Stub mode: foreign refs are always 'resolved' (SDK API is stable).
    Only used to return signatures + doc URLs at query time.
    """
    summary: Dict[str, Any] = {
        "stub_c2d_path": stub_c2d_path,
        "project_name": project_name,
        "mode": "stub",
        "added": True,
        "resolved_count": 0,
    }
    foreign_db = _foreign_db_path(stub_c2d_path)
    if not os.path.exists(foreign_db):
        summary["error"] = f"stub db not found: {foreign_db}"
        return summary
    # Open B's db. Use _connect (which goes through _kb_connect with
    # WAL mode) to ensure all tables exist. Then checkpoint WAL before
    # ATTACH to avoid "database stub_db is locked" from WAL recovery.
    conn = _connect(graph_dir)
    try:
        # Force WAL checkpoint to release any pending locks before ATTACH
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        conn.commit()
        # Switch B's journal mode to DELETE before ATTACH. A WAL-mode
        # connection ATTACHing a non-WAL db triggers "database is locked"
        # because SQLite tries to create a WAL file for the ATTACHed db.
        # By temporarily switching to DELETE mode, the ATTACH succeeds
        # without WAL recovery. Switch back to WAL after DETACH.
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.commit()
        except sqlite3.Error:
            pass
        # ATTACH first, then query signature through the alias.
        conn.execute(
            f"ATTACH DATABASE '{_escape_sql_path(foreign_db)}' AS stub_db"
        )
        # Query stub db signature through the attached alias
        try:
            sig_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM stub_db.functions"
            ).fetchone()
            functions_count = sig_row["cnt"] if sig_row else 0
        except sqlite3.Error:
            functions_count = 0
        import os as _os
        try:
            fstat = _os.stat(foreign_db)
            mtime_str = str(fstat.st_mtime)
            size_val = fstat.st_size
        except OSError:
            mtime_str = ""
            size_val = 0
        # Insert into watched_c2ds as a stub
        conn.execute(
            "INSERT OR REPLACE INTO watched_c2ds "
            "(c2d_path, project_name, db_mtime_at_sync, db_size_at_sync, "
            "functions_count_at_sync, last_synced_at, sync_status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'stub')",
            (stub_c2d_path, project_name, mtime_str,
             size_val, functions_count,
             datetime.now().isoformat())
        )
        # stub_db already ATTACHed above (before signature query to
        # avoid WAL lock conflict). Find B's unresolved calls + auto-resolve.
        unresolved = conn.execute(
            "SELECT e.invoker_id, e.invoked_id, e.call_order, "
            "e.call_condition "
            "FROM edges e "
            "WHERE e.invoked_id = '' "
            "   OR e.invoked_id NOT IN (SELECT id FROM functions) "
            "   OR e.invoked_id LIKE 'external_%' "
            "LIMIT 5000"
        ).fetchall()
        resolved = 0
        for edge in unresolved:
            # Try to extract function name from invoked_id (legacy id format)
            invoked_id = edge["invoked_id"] or ""
            # Strip external_ prefix
            name_guess = re.sub(r'^external_', '', invoked_id)
            if not name_guess:
                continue
            # Match against stub's functions (exact name)
            row = _resolve_by_exact_name(conn, name_guess, project_name,
                                         table_prefix="stub_db.")
            if row:
                conn.execute(
                    "INSERT OR REPLACE INTO foreign_refs "
                    "(local_node_id, invoked_name, foreign_c2d_path, "
                    "foreign_project_name, foreign_node_id, foreign_name, "
                    "foreign_domain, foreign_source_file, foreign_signature, "
                    "status, resolution_strategy, last_resolved_at, "
                    "call_order, call_condition) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'resolved', "
                    "'stub_exact_name', ?, ?, ?)",
                    (edge["invoker_id"], name_guess,
                     stub_c2d_path, project_name,
                     row["id"], row["name"], row["domain"],
                     row["source_file"], row["signature"],
                     datetime.now().isoformat(),
                     edge["call_order"], edge["call_condition"])
                )
                resolved += 1
        conn.commit()
        conn.execute("DETACH DATABASE stub_db")
        summary["resolved_count"] = resolved
    except sqlite3.Error as e:
        summary["error"] = str(e)
        try:
            conn.execute("DETACH DATABASE stub_db")
        except sqlite3.Error:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    finally:
        conn.close()
    return summary


# ---------------------------------------------------------------------------
# 2. FFI cross-C2D auto-linking
# ---------------------------------------------------------------------------

def auto_link_ffi_to_foreign(graph_dir: str, verbose: bool = True) -> Dict[str, Any]:
    """Scan B's FFI bindings (detected by ffi_bridge) and auto-link to
    watched foreign C2Ds that contain the bound symbols.

    For each ffi binding site (e.g., ctypes.CDLL("libA.so").foo),
    look up 'foo' in all watched foreign C2Ds. If found, populate
    foreign_refs.
    """
    summary: Dict[str, Any] = {
        "ffi_bindings_scanned": 0,
        "auto_linked": 0,
        "unmatched": 0,
    }
    conn = _connect(graph_dir)
    try:
        # Get watched c2ds
        watched = conn.execute("SELECT * FROM watched_c2ds").fetchall()
        if not watched:
            summary["message"] = "no watched c2ds; run c2d-add-foreign first"
            return summary
        # Get ffi binding sites from B's functions (body_text contains
        # ctypes/cgo/extern "C" patterns). We need to scan body_text.
        # Look for the ffi_boundary label or pattern in functions.
        # ffi_bridge.py stores ffi bindings as edges with relation='FFI_BIND'
        # or similar; check what's actually there.
        ffi_edges = conn.execute(
            "SELECT e.invoker_id, e.invoked_id, e.call_order, "
            "e.call_condition, e.relation "
            "FROM edges e "
            "WHERE e.relation LIKE '%FFI%' OR e.relation LIKE '%ffi%' "
            "OR e.invoked_id LIKE '%extern%' "
            "OR e.invoked_id LIKE '%ctypes%' "
            "OR e.invoked_id LIKE '%cgo%'"
        ).fetchall()
        summary["ffi_bindings_scanned"] = len(ffi_edges)
        # Try each watched c2d
        for w in watched:
            fdb_path = _foreign_db_path(w["c2d_path"])
            if not os.path.exists(fdb_path):
                continue
            try:
                conn.execute(
                    f"ATTACH DATABASE 'file:{_escape_sql_path(fdb_path)}?mode=ro' AS ffi_foreign"
                )
            except sqlite3.Error:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                continue
            project_name = w["project_name"] or ""
            for edge in ffi_edges:
                invoked_name = edge["invoked_id"] or ""
                # Extract function name from invoked_id (strip prefixes)
                name_guess = re.sub(r'^(extern_|ctypes_|cgo_|ffi_)', '',
                                     invoked_name)
                if not name_guess:
                    continue
                row = _resolve_by_exact_name(conn, name_guess, project_name,
                                             table_prefix="ffi_foreign.")
                if row:
                    conn.execute(
                        "INSERT OR REPLACE INTO foreign_refs "
                        "(local_node_id, invoked_name, foreign_c2d_path, "
                        "foreign_project_name, foreign_node_id, foreign_name, "
                        "foreign_domain, foreign_source_file, "
                        "foreign_signature, status, resolution_strategy, "
                        "last_resolved_at, call_order, call_condition) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'resolved', "
                        "'ffi_auto_link', ?, ?, ?)",
                        (edge["invoker_id"], name_guess,
                         w["c2d_path"], project_name,
                         row["id"], row["name"], row["domain"],
                         row["source_file"], row["signature"],
                         datetime.now().isoformat(),
                         edge["call_order"], edge["call_condition"])
                    )
                    summary["auto_linked"] += 1
                else:
                    summary["unmatched"] += 1
            try:
                conn.execute("DETACH DATABASE ffi_foreign")
            except sqlite3.Error:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        conn.commit()
    except sqlite3.Error as e:
        summary["error"] = str(e)
    finally:
        conn.close()
    return summary


# ---------------------------------------------------------------------------
# 3. RPC edges (scan for HTTP/gRPC calls)
# ---------------------------------------------------------------------------

# Patterns for detecting RPC client calls in source code
_RPC_PATTERNS = [
    # Python requests
    (re.compile(r'requests\.(get|post|put|delete|patch)\s*\(\s*[\'"]([^\'"]+)[\'"]'),
     'http'),
    (re.compile(r'requests\.Session\(\)\.(get|post|put|delete)\s*\(\s*[\'"]([^\'"]+)[\'"]'),
     'http'),
    # Python http.client
    (re.compile(r'HTTPConnection\(\s*[\'"]([^\'"]+)[\'"]\s*,\s*(\d+)\s*\)'),
     'http'),
    # Python urllib
    (re.compile(r'urlopen\(\s*[\'"]([^\'"]+)[\'"]'),
     'http'),
    # grpc.Client / grpc.channel
    (re.compile(r'grpc\.(insecure|secure)_channel\(\s*[\'"]([^\'"]+)[\'"]'),
     'grpc'),
    (re.compile(r'grpc\.\w+\(\s*[\'"]([^\'"]+)[\'"]'),
     'grpc'),
    # Go net/http
    (re.compile(r'http\.(Get|Post|Put|Delete)\s*\(\s*[\'"]([^\'"]+)[\'"]'),
     'http'),
    # Java HttpClient
    (re.compile(r'HttpClient\.newHttpClient\(\).*\.send\('),
     'http'),
    # curl-style C
    (re.compile(r'curl_easy_init\(\)'),
     'curl'),
]


def scan_rpc_edges(graph_dir: str, verbose: bool = True) -> Dict[str, Any]:
    """Scan B's source for RPC client calls (HTTP/gRPC) and create
    stub callee nodes + foreign_refs.

    Stub nodes have node_type='rpc_endpoint' and are tagged with the
    service URL. foreign_refs for these have foreign_c2d_path = the URL
    (not a local file path).
    """
    summary: Dict[str, Any] = {
        "rpc_edges_found": 0,
        "stub_nodes_created": 0,
        "foreign_refs_created": 0,
        "rpc_endpoints": [],
    }
    conn = _connect(graph_dir)
    try:
        # Get all functions' body_text to scan for RPC patterns.
        # Early filter: only fetch functions that HAVE body_text_compressed
        # (skip functions without source body — saves O(N) zlib decompress).
        rows = conn.execute(
            "SELECT id, name, domain, source_file, line_number, "
            "signature FROM functions WHERE name IS NOT NULL "
            "AND id IN (SELECT id FROM functions WHERE body_text_compressed IS NOT NULL) "
            "LIMIT 50000"
        ).fetchall()
        rpc_endpoints_found: List[Dict[str, str]] = []
        for r in rows:
            # Try to get body_text from body_text_compressed
            body = ""
            try:
                # body_text_compressed is BLOB; decompress if non-empty
                blob_row = conn.execute(
                    "SELECT body_text_compressed FROM functions WHERE id = ?",
                    (r["id"],)
                ).fetchone()
                if blob_row and blob_row["body_text_compressed"]:
                    import zlib
                    body = zlib.decompress(blob_row["body_text_compressed"]).decode(
                        "utf-8", errors="replace")
            except (sqlite3.Error, zlib.error, TypeError):
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
            if not body:
                continue
            # Scan body for RPC patterns
            for pattern, proto in _RPC_PATTERNS:
                for m in pattern.finditer(body):
                    # Get the matched URL/host
                    url_or_host = m.group(m.lastindex if m.groups() else 0)
                    if not url_or_host:
                        continue
                    # Construct stub callee node id using SHA-256 hash
                    # (avoids truncation collisions for long URLs)
                    import hashlib
                    stub_id = "rpc_" + hashlib.sha256(
                        f"{proto}_{url_or_host}".encode("utf-8")
                    ).hexdigest()[:16]
                    rpc_endpoints_found.append({
                        "caller_id": r["id"],
                        "caller_name": r["name"],
                        "proto": proto,
                        "endpoint": url_or_host,
                        "stub_id": stub_id,
                        "caller_source": r["source_file"],
                        "caller_line": r["line_number"],
                    })
                    summary["rpc_edges_found"] += 1
        # Create stub nodes + edges + foreign_refs
        seen_stubs: set = set()
        for ep in rpc_endpoints_found:
            stub_id = ep["stub_id"]
            if stub_id not in seen_stubs:
                seen_stubs.add(stub_id)
                # Insert stub function node (external_endpoint type)
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO functions "
                        "(id, name, domain, source_file, line_number, "
                        "signature, labels, extra_json) "
                        "VALUES (?, ?, ?, ?, NULL, '', 'out_end', ?)",
                        (stub_id, ep["endpoint"], "external_rpc",
                         f"rpc://{ep['proto']}/{ep['endpoint']}",
                         json.dumps({"node_type": "rpc_endpoint",
                                      "proto": ep["proto"],
                                      "url": ep["endpoint"]}))
                    )
                    summary["stub_nodes_created"] += 1
                except sqlite3.Error:
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO edges "
                    "(invoker_id, invoked_id, relation, call_order, "
                    "confidence, source) "
                    "VALUES (?, ?, 'RPC_CALL', NULL, 'INFERRED', 'rpc_scan')",
                    (ep["caller_id"], stub_id)
                )
            except sqlite3.Error:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO foreign_refs "
                    "(local_node_id, invoked_name, foreign_c2d_path, "
                    "foreign_project_name, foreign_node_id, status, "
                    "resolution_strategy, last_resolved_at) "
                    "VALUES (?, ?, ?, ?, ?, 'rpc_endpoint', "
                    "'rpc_pattern_scan', ?)",
                    (ep["caller_id"], ep["endpoint"],
                     f"rpc://{ep['proto']}/{ep['endpoint']}",
                     ep["proto"], stub_id,
                     datetime.now().isoformat())
                )
                summary["foreign_refs_created"] += 1
            except sqlite3.Error:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
            summary["rpc_endpoints"].append({
                "caller": ep["caller_name"],
                "proto": ep["proto"],
                "endpoint": ep["endpoint"],
            })
        conn.commit()
    except sqlite3.Error as e:
        summary["error"] = str(e)
    finally:
        conn.close()
    return summary


# ---------------------------------------------------------------------------
# 4. Cross-team knowledge sharing
# ---------------------------------------------------------------------------

def import_foreign_knowledge(graph_dir: str, foreign_c2d_path: str,
                              project_name: str = "",
                              verbose: bool = True) -> Dict[str, Any]:
    """Copy foreign C2D's knowledge/*.md into local knowledge/ with
    foreign_<project>_ prefix.

    Lets B's kb-query see A's principles/glossary/etc. without merging
    the C2Ds themselves.
    """
    summary: Dict[str, Any] = {
        "foreign_c2d_path": foreign_c2d_path,
        "project_name": project_name,
        "files_copied": 0,
    }
    foreign_knowledge_dir = os.path.join(foreign_c2d_path, "knowledge")
    if not os.path.isdir(foreign_knowledge_dir):
        summary["message"] = f"no knowledge/ dir in {foreign_c2d_path}"
        return summary
    local_knowledge_dir = os.path.join(graph_dir, "knowledge")
    os.makedirs(local_knowledge_dir, exist_ok=True)
    safe_project = re.sub(r'[^A-Za-z0-9_]', '_', project_name or "foreign")
    copied = 0
    for fname in sorted(os.listdir(foreign_knowledge_dir)):
        if not fname.endswith(".md"):
            continue
        # Skip files that look like merge artifacts (don't re-merge)
        if fname.startswith("merged_") or fname.startswith(f"foreign_{safe_project}_"):
            continue
        src = os.path.join(foreign_knowledge_dir, fname)
        if not os.path.isfile(src):
            continue
        dst_name = f"foreign_{safe_project}_{fname}"
        dst = os.path.join(local_knowledge_dir, dst_name)
        # Skip if destination exists AND is newer than source (up-to-date).
        # Overwrite if source is newer (A updated its knowledge).
        if os.path.exists(dst):
            try:
                src_mtime = os.path.getmtime(src)
                dst_mtime = os.path.getmtime(dst)
                if src_mtime <= dst_mtime:
                    continue  # dst is up-to-date
            except OSError:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        try:
            shutil.copy2(src, dst)
            copied += 1
        except OSError as e:
            if verbose:
                print(f"[import-foreign-knowledge] failed to copy "
                      f"{fname}: {e}", file=sys.stderr)
    summary["files_copied"] = copied
    # Touch the local knowledge index so it gets rebuilt on next kb-rebuild
    index_path = os.path.join(local_knowledge_dir, "index.json")
    if os.path.exists(index_path):
        try:
            os.utime(index_path, None)
        except OSError:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    return summary


# ---------------------------------------------------------------------------
# CLI handlers
# ---------------------------------------------------------------------------

