#!/usr/bin/env python3
"""SQLite-based post-processing for Code2Database.

Replaces NetworkX-iteration-based index building, doc generation,
and validation with SQLite-query-based equivalents to reduce peak memory.

When the graph is stored in SQLite, we can query it efficiently without
keeping the entire NetworkX DiGraph in memory (~16GB for Linux kernel).
This module provides drop-in replacements that operate on the SQLite DB.
"""

import gc
import json
import os
import re
import sys
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
import logging


@contextmanager
def _open_db(db_path):
    """Open SQLite DB for reading with optimized settings.

    Usage: ``with _open_db(db_path) as conn: ...``
    Ensures the connection is closed even on exceptions.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA cache_size=-128000")  # 128MB cache for reads
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
    finally:
        conn.close()


def _has_label_columns(conn) -> bool:
    """Check whether the functions table has indexed boolean label columns.

    Returns True if is_api_entry column exists (implies all 5 label
    columns are present). Used to choose between indexed queries
    (``is_api_entry = 1``) and LIKE fallback (``labels LIKE '%API_entry%'``).
    """
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(functions)").fetchall()}
        return "is_api_entry" in cols
    except Exception:
        return False


def _label_where(label: str, conn=None, table_alias: str = "") -> str:
    """Return a WHERE clause fragment for a label, using indexed column
    if available, otherwise LIKE fallback.

    table_alias: optional prefix like "f." for queries that alias the
    functions table (e.g. "SELECT ... FROM functions f WHERE ...").
    """
    _col_map = {
        "API_entry": "is_api_entry",
        "thread_processor": "is_thread_processor",
        "callback_func": "is_callback_func",
        "out_end": "is_out_end",
        "unknown_end": "is_unknown_end",
    }
    col = _col_map.get(label)
    prefix = f"{table_alias}." if table_alias else ""
    if col and conn and _has_label_columns(conn):
        return f"{prefix}{col} = 1"
    return f"{prefix}labels LIKE '%{label}%'"


_MACRO_RE = re.compile(r'^[A-Z][A-Z0-9_]{2,}$')


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------

def _build_indexes_from_sqlite(db_path, outdir):
    """Build all index files from SQLite.

    For large graphs (>100K nodes), the reverse index uses an optimized
    batch streaming approach (_NODE_BATCH=10000 instead of 1000, reducing
    SQL query count from ~3280 to ~328). The 4 index builders remain
    sequential because they all scan the same edges table — parallel
    reads cause I/O contention and cache thrashing, making it slower
    than sequential on large graphs.
    """
    with _open_db(db_path) as conn:

        node_count = conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]
        edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        print(f"[sqlite-index] {node_count} nodes, {edge_count} edges", file=sys.stderr)

        _ri_path = os.path.join(outdir, ".code2database_reverse_index.json")
        _ci_path = os.path.join(outdir, ".code2database_condition_index.json")
        _conc_path = os.path.join(outdir, ".code2database_concurrency_index.json")

        if node_count <= 100000:
            _build_indexes_merged_small(conn, outdir, _ri_path, _ci_path, _conc_path, node_count)
        else:
            _build_reverse_index_streaming(conn, _ri_path, node_count)
            _build_condition_index_sqlite(conn, outdir, _ci_path)
            _build_concurrency_index_sqlite(conn, outdir, _conc_path)

        _chains_path = os.path.join(outdir, ".code2database_chains.json")
        _chains_lite_path = os.path.join(outdir, ".code2database_chains_lite.json")
        print(f"[sqlite-index] Writing chains index...", file=sys.stderr)
        _build_chains_index_sqlite(conn, outdir, _chains_path, _chains_lite_path)

    pass  # conn closed by context manager


def _build_indexes_merged_small(conn, outdir, ri_path, ci_path, conc_path, node_count):
    """Build reverse + condition + concurrency indexes in a single edges scan.

    For small graphs (≤100K nodes), merges 3 separate full-table scans
    into 1 scan that dispatches to all 3 output files simultaneously.
    """
    all_func_ids = [row[0] for row in conn.execute(
        "SELECT id FROM functions ORDER BY id"
    ).fetchall()]

    name_map = {row[0]: row[1] for row in conn.execute(
        "SELECT id, name FROM functions"
    ).fetchall()}

    callers_map = defaultdict(list)
    callees_map = defaultdict(list)
    cond_map = defaultdict(list)
    conc_map = defaultdict(list)

    rows = conn.execute(
        "SELECT invoker_id, invoked_id, call_order, call_condition, "
        "concurrency, confidence, relation FROM edges "
        "ORDER BY invoker_id"
    ).fetchall()

    for caller, callee, call_order, cond, conc, conf, rel in rows:
        if rel in ('CONTAINS', 'IMPORTS'):
            continue
        if callee:
            callers_map[callee].append((caller, call_order, cond or "", conc or ""))
        if caller:
            callees_map[caller].append((callee, call_order, cond or "", conc or ""))
        if cond:
            cond_map[caller].append({
                "condition": cond, "target_node": callee,
                "target_name": name_map.get(callee, ""), "condition_vars": []
            })
        if conc:
            conc_map[caller].append({
                "id": callee, "name": name_map.get(callee, ""),
                "concurrency": conc, "confidence": conf or ""
            })

    with open(ri_path, "w", encoding="utf-8") as f:
        f.write('{')
        first = True
        for nid in all_func_ids:
            callers = [{"id": c[0], "call_order": c[1],
                        "call_condition": c[2], "concurrency": c[3]}
                       for c in callers_map.get(nid, [])]
            callees = [{"id": c[0], "call_order": c[1],
                        "call_condition": c[2], "concurrency": c[3]}
                       for c in callees_map.get(nid, [])]
            if not first:
                f.write(',')
            first = False
            f.write(json.dumps(nid, ensure_ascii=False) + ':')
            f.write(json.dumps({"callers": callers, "callees": callees},
                               ensure_ascii=False, separators=(',', ':')))
        f.write('}\n')

    with open(ci_path, "w", encoding="utf-8") as f:
        f.write('{')
        first = True
        for nid in all_func_ids:
            branches = cond_map.get(nid, [])
            if not branches:
                continue
            if not first:
                f.write(',')
            first = False
            f.write(json.dumps(nid, ensure_ascii=False) + ':')
            f.write(json.dumps(branches, ensure_ascii=False, separators=(',', ':')))
        f.write('}\n')

    with open(conc_path, "w", encoding="utf-8") as f:
        f.write('{')
        first = True
        for nid in all_func_ids:
            entries = conc_map.get(nid, [])
            if not entries:
                continue
            if not first:
                f.write(',')
            first = False
            f.write(json.dumps(nid, ensure_ascii=False) + ':')
            f.write(json.dumps(entries, ensure_ascii=False, separators=(',', ':')))
        f.write('}\n')

    gc.collect()


def _build_reverse_index_sqlite(conn, outdir, ri_path):
    """Build reverse index (callers/callees per node) from SQLite.

    For large graphs, uses a per-node query approach to avoid building
    the entire reverse index in memory. Each node's callers/callees are
    queried from SQLite (with indexes) and written immediately.
    """
    node_count = conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]

    # For small graphs, use the batch approach (faster)
    if node_count <= 100000:
        _build_reverse_index_small(conn, ri_path)
    else:
        _build_reverse_index_streaming(conn, ri_path, node_count)


def _build_reverse_index_small(conn, ri_path):
    """Build reverse index for small graphs — batch approach."""
    all_func_ids = [row[0] for row in conn.execute(
        "SELECT id FROM functions ORDER BY id"
    ).fetchall()]
    callers_map = defaultdict(list)
    callees_map = defaultdict(list)
    rows = conn.execute(
        "SELECT invoker_id, invoked_id, call_order, call_condition, concurrency "
        "FROM edges WHERE relation NOT IN ('CONTAINS', 'IMPORTS')"
    ).fetchall()
    for caller, callee, call_order, cond, conc in rows:
        if callee:
            callers_map[callee].append((caller, call_order, cond or "", conc or ""))
        if caller:
            callees_map[caller].append((callee, call_order, cond or "", conc or ""))

    with open(ri_path, "w", encoding="utf-8") as f:
        f.write('{')
        first = True
        for nid in all_func_ids:
            callers = [{"id": c[0], "call_order": c[1],
                        "call_condition": c[2], "concurrency": c[3]}
                       for c in callers_map.get(nid, [])]
            callees = [{"id": c[0], "call_order": c[1],
                        "call_condition": c[2], "concurrency": c[3]}
                       for c in callees_map.get(nid, [])]
            if not first:
                f.write(',')
            first = False
            f.write(json.dumps(nid, ensure_ascii=False) + ':')
            f.write(json.dumps({"callers": callers, "callees": callees},
                               ensure_ascii=False, separators=(',', ':')))
        f.write('}\n')
    del callers_map, callees_map
    gc.collect()


def _build_reverse_index_streaming(conn, ri_path, node_count):
    """Build reverse index for large graphs — optimized batch streaming.

    Instead of the old per-1000-nodes approach (3280 SQL queries for
    1.6M nodes), uses larger batches (10000 nodes) to reduce query
    count to ~328 while keeping memory bounded. Each batch loads only
    the callers/callees for that subset of nodes.
    """
    _NODE_BATCH = 10000  # was 1000 — 10x fewer queries, still memory-safe
    all_func_ids = [row[0] for row in conn.execute(
        "SELECT id FROM functions ORDER BY id"
    ).fetchall()]

    with open(ri_path, "w", encoding="utf-8") as f:
        f.write('{')
        first_node = True
        for batch_start in range(0, len(all_func_ids), _NODE_BATCH):
            batch_ids = all_func_ids[batch_start:batch_start + _NODE_BATCH]

            # Query callers for this batch (invoked_id IN batch)
            _callers = defaultdict(list)
            placeholders = ','.join('?' * len(batch_ids))
            rows = conn.execute(
                f"SELECT invoker_id, invoked_id, call_order, call_condition, concurrency "
                f"FROM edges WHERE invoked_id IN ({placeholders}) "
                f"AND relation NOT IN ('CONTAINS', 'IMPORTS')",
                batch_ids
            ).fetchall()
            for caller, callee, call_order, cond, conc in rows:
                _callers[callee].append((caller, call_order, cond or "", conc or ""))

            # Query callees for this batch (invoker_id IN batch)
            _callees = defaultdict(list)
            rows = conn.execute(
                f"SELECT invoker_id, invoked_id, call_order, call_condition, concurrency "
                f"FROM edges WHERE invoker_id IN ({placeholders}) "
                f"AND relation NOT IN ('CONTAINS', 'IMPORTS')",
                batch_ids
            ).fetchall()
            for caller, callee, call_order, cond, conc in rows:
                _callees[caller].append((callee, call_order, cond or "", conc or ""))

            # Write entries for this batch
            for nid in batch_ids:
                callers = [{"id": c[0], "call_order": c[1],
                            "call_condition": c[2], "concurrency": c[3]}
                           for c in _callers.get(nid, [])]
                callees = [{"id": c[0], "call_order": c[1],
                            "call_condition": c[2], "concurrency": c[3]}
                           for c in _callees.get(nid, [])]
                if not first_node:
                    f.write(',')
                first_node = False
                f.write(json.dumps(nid, ensure_ascii=False) + ':')
                f.write(json.dumps({"callers": callers, "callees": callees},
                                   ensure_ascii=False, separators=(',', ':')))

            # Free batch data
            del _callers, _callees
            if batch_start % (_NODE_BATCH * 10) == 0:
                gc.collect()

        f.write('}\n')

    gc.collect()


def _build_condition_index_sqlite(conn, outdir, ci_path):
    """Build condition index from SQLite using streaming write."""
    # Use ORDER BY invoker_id for efficient streaming
    rows = conn.execute(
        "SELECT e.invoker_id, e.invoked_id, e.call_condition, f.name "
        "FROM edges e LEFT JOIN functions f ON e.invoked_id = f.id "
        "WHERE e.call_condition IS NOT NULL AND e.call_condition != '' "
        "AND e.relation NOT IN ('CONTAINS', 'IMPORTS') "
        "ORDER BY e.invoker_id"
    ).fetchall()

    # Streaming write: group by invoker_id as we go
    with open(ci_path, "w", encoding="utf-8") as f:
        f.write('{')
        first = True
        current_caller = None
        current_branches = []
        for caller, callee, cond, callee_name in rows:
            if caller != current_caller:
                # Flush previous caller
                if current_caller is not None:
                    if not first:
                        f.write(',')
                    first = False
                    f.write(json.dumps(current_caller, ensure_ascii=False) + ':')
                    f.write(json.dumps(current_branches, ensure_ascii=False, separators=(',', ':')))
                current_caller = caller
                current_branches = []
            current_branches.append({
                "condition": cond,
                "target_node": callee,
                "target_name": callee_name or "",
                "condition_vars": []
            })
        # Flush last caller
        if current_caller is not None:
            if not first:
                f.write(',')
            f.write(json.dumps(current_caller, ensure_ascii=False) + ':')
            f.write(json.dumps(current_branches, ensure_ascii=False, separators=(',', ':')))
        f.write('}\n')

    gc.collect()


def _build_concurrency_index_sqlite(conn, outdir, conc_path):
    """Build concurrency index from SQLite using streaming write."""
    rows = conn.execute(
        "SELECT e.invoker_id, e.invoked_id, e.concurrency, e.confidence, f.name "
        "FROM edges e LEFT JOIN functions f ON e.invoked_id = f.id "
        "WHERE e.concurrency IS NOT NULL AND e.concurrency != '' "
        "AND e.relation NOT IN ('CONTAINS', 'IMPORTS') "
        "ORDER BY e.invoker_id"
    ).fetchall()

    # Streaming write
    with open(conc_path, "w", encoding="utf-8") as f:
        f.write('{')
        first = True
        current_caller = None
        current_entries = []
        for caller, callee, conc, conf, callee_name in rows:
            if caller != current_caller:
                if current_caller is not None:
                    if not first:
                        f.write(',')
                    first = False
                    f.write(json.dumps(current_caller, ensure_ascii=False) + ':')
                    f.write(json.dumps(current_entries, ensure_ascii=False, separators=(',', ':')))
                current_caller = caller
                current_entries = []
            current_entries.append({
                "id": callee, "name": callee_name or "",
                "concurrency": conc, "confidence": conf or ""
            })
        if current_caller is not None:
            if not first:
                f.write(',')
            f.write(json.dumps(current_caller, ensure_ascii=False) + ':')
            f.write(json.dumps(current_entries, ensure_ascii=False, separators=(',', ':')))
        f.write('}\n')

    gc.collect()


def _build_chains_index_sqlite(conn, outdir, chains_path, chains_lite_path):
    """Build chains index from SQLite using BFS from API entries to endpoints.

    For large graphs, uses SQLite-based BFS instead of NetworkX shortest_path.
    """
    # Get API entries — limit to 200 for large graphs
    api_entries = [row[0] for row in conn.execute(
        f"SELECT id FROM functions WHERE {_label_where('API_entry', conn)} LIMIT 200"
    ).fetchall()]
    endpoints = set(row[0] for row in conn.execute(
        f"SELECT id FROM functions WHERE {_label_where('out_end', conn)} OR {_label_where('unknown_end', conn)}"
    ).fetchall())

    if not api_entries or not endpoints:
        Path(chains_path).write_text("[]\n", encoding="utf-8")
        Path(chains_lite_path).write_text("[]\n", encoding="utf-8")
        return

    # Pre-build adjacency from SQLite for BFS
    # Build callee adjacency: invoker_id → [invoked_id, ...]
    # For large graphs, stream in batches using keyset pagination (rowid > last)
    # to avoid the O(N²) cost of LIMIT/OFFSET on large tables.
    adj = defaultdict(list)
    _BATCH = 100000
    last_rowid = 0
    while True:
        rows = conn.execute(
            "SELECT rowid, invoker_id, invoked_id FROM edges "
            "WHERE relation NOT IN ('CONTAINS', 'IMPORTS') "
            "AND rowid > ? "
            "ORDER BY rowid ASC "
            "LIMIT ?",
            (last_rowid, _BATCH)
        ).fetchall()
        if not rows:
            break
        for rowid, caller, callee in rows:
            adj[caller].append(callee)
            last_rowid = rowid

    # Pre-build name lookup for BFS results (batch query at end)
    chains = []
    chains_lite = []

    for entry_id in api_entries:
        # BFS to find shortest path to any endpoint
        visited = {entry_id}
        # Use deque for O(1) popleft (list.pop(0) is O(N))
        from collections import deque
        queue = deque([(entry_id, [entry_id])])
        found = False
        while queue and not found:
            node, path = queue.popleft()
            for neighbor in adj.get(node, []):
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                new_path = path + [neighbor]
                if neighbor in endpoints:
                    # Found a chain — resolve names in batch later
                    chains.append({
                        "entry": entry_id,
                        "endpoint": neighbor,
                        "path": new_path,
                        "length": len(new_path) - 1,
                    })
                    chains_lite.append({
                        "entry": entry_id,
                        "endpoint": neighbor,
                        "length": len(new_path) - 1,
                    })
                    found = True
                    break
                if len(new_path) <= 10:  # Limit path length
                    queue.append((neighbor, new_path))

        if len(chains) >= 500:  # Limit total chains
            break

    # Batch-resolve names for all chain paths
    _all_nids = set()
    for c in chains:
        _all_nids.update(c["path"])
    # Batch query names in chunks (SQLite has ~999 variable limit)
    _name_map = {}
    _nid_list = list(_all_nids)
    _CHUNK = 900
    for i in range(0, len(_nid_list), _CHUNK):
        chunk = _nid_list[i:i + _CHUNK]
        placeholders = ','.join('?' * len(chunk))
        rows = conn.execute(
            f"SELECT id, name FROM functions WHERE id IN ({placeholders})", chunk
        ).fetchall()
        for nid, name in rows:
            _name_map[nid] = name

    # Fill in path_names
    for c in chains:
        c["path_names"] = [_name_map.get(nid, nid) for nid in c["path"]]
    for c in chains_lite:
        c["entry"] = _name_map.get(c["entry"], c["entry"])
        c["endpoint"] = _name_map.get(c["endpoint"], c["endpoint"])

    Path(chains_path).write_text(
        json.dumps(chains, ensure_ascii=False) + "\n", encoding="utf-8")
    Path(chains_lite_path).write_text(
        json.dumps(chains_lite, ensure_ascii=False) + "\n", encoding="utf-8")
    del adj, chains, chains_lite, _name_map
    gc.collect()


# ---------------------------------------------------------------------------
# Summary / docs generation
# ---------------------------------------------------------------------------

def _build_callgraph_summary_md_from_sqlite(db_path, outdir, source_root="", build_info=None):
    """Generate CODE2DATABASE_SUMMARY.md from SQLite instead of NetworkX."""
    from datetime import datetime

    with _open_db(db_path) as conn:

        # Basic stats
        node_count = conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]
        edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        domain_count = conn.execute(
            "SELECT COUNT(DISTINCT domain) FROM functions "
            "WHERE domain IS NOT NULL AND domain != ''"
        ).fetchone()[0]

        # Single-pass label statistics using indexed boolean columns
        # (falls back to LIKE for pre-migration DBs)
        if _has_label_columns(conn):
            label_stats = conn.execute(
                "SELECT "
                "SUM(is_api_entry) as api_count, "
                "SUM(is_thread_processor) as thread_count, "
                "SUM(is_callback_func) as callback_count, "
                "SUM(CASE WHEN is_out_end = 1 OR is_unknown_end = 1 THEN 1 ELSE 0 END) as endpoint_count "
                "FROM functions"
            ).fetchone()
        else:
            label_stats = conn.execute(
                "SELECT "
                "SUM(CASE WHEN labels LIKE '%API_entry%' THEN 1 ELSE 0 END) as api_count, "
                "SUM(CASE WHEN labels LIKE '%thread_processor%' THEN 1 ELSE 0 END) as thread_count, "
                "SUM(CASE WHEN labels LIKE '%callback_func%' THEN 1 ELSE 0 END) as callback_count, "
                "SUM(CASE WHEN labels LIKE '%out_end%' OR labels LIKE '%unknown_end%' THEN 1 ELSE 0 END) as endpoint_count "
                "FROM functions"
            ).fetchone()
        api_count = label_stats[0] or 0
        thread_count = label_stats[1] or 0
        callback_count = label_stats[2] or 0
        endpoint_count = label_stats[3] or 0

        # Domain stats (exclude empty/blank domains — synthetic vtable nodes
        # have domain='' and shouldn't pollute the distribution)
        domains = {}
        for row in conn.execute(
            "SELECT domain, COUNT(*) FROM functions "
            "WHERE NOT (labels LIKE '%is_empty%') "
            "AND domain IS NOT NULL AND domain != '' "
            "GROUP BY domain ORDER BY COUNT(*) DESC"
        ).fetchall():
            domains[row[0]] = row[1]

        # Edge type distribution
        edge_types = {}
        for row in conn.execute(
            "SELECT relation, COUNT(*) FROM edges GROUP BY relation ORDER BY COUNT(*) DESC"
        ).fetchall():
            edge_types[row[0]] = row[1]

        # Confidence distribution
        confidence_dist = {}
        for row in conn.execute(
            "SELECT confidence, COUNT(*) FROM edges WHERE confidence IS NOT NULL "
            "GROUP BY confidence ORDER BY COUNT(*) DESC"
        ).fetchall():
            confidence_dist[row[0]] = row[1]

        # Build the summary markdown
        project_name = os.path.basename(source_root) if source_root else "project"
        lines = [
            f"# Code2Database Summary: {project_name}\n",
            f"\n> Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n",
            f"\n## Overview (5s read)\n",
            f"\n| Metric | Value |",
            f"|--------|-------|",
            f"| Total functions | {node_count:,} |",
            f"| Total edges | {edge_count:,} |",
            f"| Domains | {domain_count:,} |",
            f"| API entries | {api_count:,} |",
            f"| Thread processors | {thread_count:,} |",
            f"| Callback functions | {callback_count:,} |",
            f"| Endpoints | {endpoint_count:,} |",
        ]

        if domains:
            lines.append(f"\n## Domain Distribution (top 30)\n")
            lines.append("| Domain | Functions |")
            lines.append("|--------|-----------|")
            for dom, cnt in list(domains.items())[:30]:
                lines.append(f"| {dom} | {cnt:,} |")

        if edge_types:
            lines.append(f"\n## Edge Types (legacy edges table)\n")
            lines.append("| Type | Count |")
            lines.append("|------|-------|")
            for et, cnt in edge_types.items():
                lines.append(f"| {et} | {cnt:,} |")

        # Also show cgdb_edges by kind if the table exists.
        try:
            cgdb_edge_kinds = conn.execute(
                "SELECT kind, COUNT(*) FROM cgdb_edges GROUP BY kind ORDER BY COUNT(*) DESC"
            ).fetchall()
            if cgdb_edge_kinds:
                lines.append(f"\n## cgdb_edges by kind\n")
                lines.append("| Kind | Count |")
                lines.append("|------|-------|")
                for kind, cnt in cgdb_edge_kinds:
                    lines.append(f"| {kind} | {cnt:,} |")
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        if confidence_dist:
            lines.append(f"\n## Confidence Distribution\n")
            lines.append("| Confidence | Count |")
            lines.append("|------------|-------|")
            for conf, cnt in confidence_dist.items():
                lines.append(f"| {conf} | {cnt:,} |")

        # cgdb Layer section — show row counts for the 13 semantic tables so the
        # summary reflects the database layer, not just legacy call graph.
        cgdb_tables = [
            ("L0", "graph_versions", "graph_versions"),
            ("L1", "cgdb_nodes", "cgdb_nodes"),
            ("L1", "cgdb_edges", "cgdb_edges"),
            ("L1", "cgdb_files", "cgdb_files"),
            ("L2", "cgdb_types", "cgdb_types"),
            ("L3", "conditions", "conditions"),
            ("L3.5", "config_predicates", "config_predicates"),
            ("L4", "basic_blocks", "basic_blocks"),
            ("L4", "cfg_edges", "cfg_edges"),
            ("L5", "data_flow", "data_flow"),
            ("L6", "alias_sets", "alias_sets"),
            ("L7", "sync_primitives", "sync_primitives"),
            ("L7", "happens_before", "happens_before"),
            ("L8", "ops_bindings", "ops_bindings"),
            ("L9", "doc_comments", "doc_comments"),
            ("L10", "cgdb_includes", "cgdb_includes"),
            ("L11", "invoke_sites", "invoke_sites"),
        ]
        cgdb_rows = []
        has_any_cgdb = False
        for layer, label, table in cgdb_tables:
            try:
                cnt = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                cgdb_rows.append((layer, label, cnt))
                if cnt > 0:
                    has_any_cgdb = True
            except Exception:
                cgdb_rows.append((layer, label, "-"))
        if has_any_cgdb:
            lines.append(f"\n## cgdb Layer (Code Database)\n")
            lines.append("> Row counts for the 13 semantic tables (L0–L11). Run `cgdb-layer-summary --graph <dir>` for full coverage metrics.\n")
            lines.append("| Layer | Table | Rows |")
            lines.append("|-------|-------|------|")
            for layer, label, cnt in cgdb_rows:
                lines.append(f"| {layer} | `{label}` | {cnt:,} |")
            # Coverage metrics for cgdb_nodes
            try:
                total_nodes = conn.execute("SELECT COUNT(*) FROM cgdb_nodes").fetchone()[0]
                if total_nodes > 0:
                    with_desc = conn.execute(
                        "SELECT COUNT(*) FROM cgdb_nodes WHERE description IS NOT NULL AND description != ''"
                    ).fetchone()[0]
                    with_commit = conn.execute(
                        "SELECT COUNT(*) FROM cgdb_nodes WHERE commit_hash IS NOT NULL AND commit_hash != ''"
                    ).fetchone()[0]
                    with_byte = conn.execute(
                        "SELECT COUNT(*) FROM cgdb_nodes WHERE byte_end > byte_start"
                    ).fetchone()[0]
                    with_type = conn.execute(
                        "SELECT COUNT(*) FROM cgdb_nodes WHERE type_id IS NOT NULL"
                    ).fetchone()[0]
                    # Real-source nodes exclude external/builtin refs and
                    # synthesized __cond_ nodes — these have no source location
                    # or type by design.
                    real_total = conn.execute(
                        "SELECT COUNT(*) FROM cgdb_nodes "
                        "WHERE attrs NOT LIKE '%\"external\": true%' "
                        "AND fqn NOT LIKE '%__cond_%'"
                    ).fetchone()[0]
                    real_with_byte = conn.execute(
                        "SELECT COUNT(*) FROM cgdb_nodes "
                        "WHERE byte_end > byte_start "
                        "AND attrs NOT LIKE '%\"external\": true%' "
                        "AND fqn NOT LIKE '%__cond_%'"
                    ).fetchone()[0] if real_total > 0 else 0
                    real_with_type = conn.execute(
                        "SELECT COUNT(*) FROM cgdb_nodes "
                        "WHERE type_id IS NOT NULL "
                        "AND attrs NOT LIKE '%\"external\": true%' "
                        "AND fqn NOT LIKE '%__cond_%'"
                    ).fetchone()[0] if real_total > 0 else 0
                    lines.append("")
                    lines.append("**cgdb_nodes coverage:**")
                    lines.append("")
                    lines.append("| Metric | Count | Coverage |")
                    lines.append("|--------|-------|----------|")
                    lines.append(f"| With description | {with_desc:,} | {with_desc*100//total_nodes}% |")
                    lines.append(f"| With commit_hash | {with_commit:,} | {with_commit*100//total_nodes}% |")
                    lines.append(f"| With type_id | {with_type:,} | {with_type*100//total_nodes}% |")
                    lines.append(f"| With byte range | {with_byte:,} | {with_byte*100//total_nodes}% |")
                    if real_total > 0:
                        lines.append(f"| Real-source nodes (excl. external/cond) | {real_total:,} | {real_total*100//total_nodes}% |")
                        lines.append(f"| Real-source with byte range | {real_with_byte:,} | {real_with_byte*100//real_total}% |")
                        lines.append(f"| Real-source with type_id | {real_with_type:,} | {real_with_type*100//real_total}% |")
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        api_rows = conn.execute(
            f"SELECT name, domain, source_file FROM functions "
            f"WHERE {_label_where('API_entry', conn)} ORDER BY name"
        ).fetchall()
        if api_rows:
            filtered_apis = [(name, domain, src) for name, domain, src in api_rows
                             if not _MACRO_RE.match(name)]
            lines.append(f"\n## API Entries (top 50)\n")
            for name, domain, src in filtered_apis[:50]:
                # Sanitize: strip any embedded newlines/extra whitespace from name
                # (older scanners could leak the multi-line signature into name)
                clean_name = ' '.join(str(name).split())
                lines.append(f"- `{clean_name}` ({domain})")

        # Build info
        if build_info:
            lines.append(f"\n## Build Configuration\n")
            for k, v in build_info.items():
                lines.append(f"- **{k}**: {v}")

        summary_path = os.path.join(outdir, "CODE2DATABASE_SUMMARY.md")
        Path(summary_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    pass  # conn closed by context manager
    return summary_path


def _build_domain_readmes_from_sqlite(db_path, outdir):
    """Generate README.md for each domain subdirectory from SQLite."""
    with _open_db(db_path) as conn:

        # Group nodes by top-level domain
        domain_dir_nodes = defaultdict(list)
        for row in conn.execute(
            "SELECT id, name, domain, labels, signature, source_file "
            "FROM functions WHERE NOT (labels LIKE '%is_empty%')"
        ).fetchall():
            nid, name, domain, labels_json, sig, src = row
            labels = json.loads(labels_json) if labels_json else []
            parts = domain.split(".") if domain else ["root"]
            top_dir = parts[0] if parts else "root"
            domain_dir_nodes[top_dir].append({
                "id": nid, "name": name, "domain": domain,
                "labels": labels, "signature": sig or "", "source_file": src or ""
            })

        for top_dir, nodes in domain_dir_nodes.items():
            readme_dir = os.path.join(outdir, "domains", top_dir)
            readme_path = os.path.join(readme_dir, "README.md")
            os.makedirs(readme_dir, exist_ok=True)

            api_entries = [n for n in nodes if "API_entry" in n.get("labels", [])]
            thread_entries = [n for n in nodes if "thread_processor" in n.get("labels", [])]
            callback_entries = [n for n in nodes if "callback_func" in n.get("labels", [])]

            lines = [f"# Domain: {top_dir}\n"]
            lines.append(f"Functions: {len(nodes)}\n")
            sub_domains = sorted(set(n.get("domain", "root") for n in nodes))
            if len(sub_domains) > 1:
                lines.append(f"Sub-domains: {', '.join(sub_domains)}\n")
            if api_entries:
                lines.append(f"\n## Public API ({len(api_entries)})\n")
                for n in sorted(api_entries, key=lambda x: x.get("name", ""))[:30]:
                    lines.append(f"- `{n['name']}` — {n.get('signature', '')[:80]}")
            if thread_entries:
                lines.append(f"\n## Thread Entries ({len(thread_entries)})\n")
                for n in sorted(thread_entries, key=lambda x: x.get("name", "")):
                    lines.append(f"- `{n['name']}`")
            if callback_entries:
                lines.append(f"\n## Callback Functions ({len(callback_entries)})\n")
                for n in sorted(callback_entries, key=lambda x: x.get("name", ""))[:20]:
                    lines.append(f"- `{n['name']}`")
            Path(readme_path).write_text("\n".join(lines) + "\n", encoding="utf-8")

    pass  # conn closed by context manager
    gc.collect()


def _build_scenarios_file_from_sqlite(db_path, outdir, build_info=None,
                                       builder_profile=None):
    """Generate execution scenarios from SQLite.

    Produces two files:
    - .code2database_scenarios.json (machine-readable)
    - SCENARIOS_SUMMARY.md (human-readable, with execution path traces)

    Scenarios are ordered by out-degree (most connected API entries first)
    and filtered to exclude macro-like names (ALL_UPPERCASE) and entries
    with no call chain (empty paths).

    ``builder_profile`` is the project profile dict. The tool itself is
    project-agnostic; project-specific noise names (e.g. project-specific
    macros like logging wrappers or foreach iterators) are read from
    ``builder_profile['scenario_noise_names']`` instead of being hardcoded.
    """
    import re
    with _open_db(db_path) as conn:

        # Macro-like name pattern: ALL_UPPERCASE with optional digits/underscores
        # (uses module-level _MACRO_RE)

        scenarios = []
        api_rows = conn.execute(
            "SELECT f.id, f.name, f.domain, f.signature, "
            "COALESCE(out_cnt.cnt, 0) as out_deg "
            "FROM functions f "
            "LEFT JOIN (SELECT invoker_id, COUNT(*) as cnt FROM edges WHERE relation='INVOKES' GROUP BY invoker_id) out_cnt "
            "ON f.id = out_cnt.invoker_id "
            f"WHERE {_label_where('API_entry', conn, table_alias='f')} "
            "ORDER BY out_deg DESC, f.name "
            "LIMIT 50"
        ).fetchall()

        # Pre-build adjacency list from edges (1 query instead of 300+ per-hop queries)
        adj = defaultdict(list)
        name_map = {row[0]: row[1] for row in conn.execute(
            "SELECT id, name FROM functions").fetchall()}
        for caller, callee, call_order, cond, conc in conn.execute(
            "SELECT invoker_id, invoked_id, call_order, call_condition, concurrency "
            "FROM edges WHERE relation = 'INVOKES' ORDER BY invoker_id, call_order"
        ).fetchall():
            adj[caller].append((callee, name_map.get(callee, ""), cond or "", conc or ""))

        for api_id, name, domain, sig, out_deg in api_rows:
            if _MACRO_RE.match(name):
                continue
            chain = []
            visited = {api_id}
            current_id = api_id
            concurrent_fns = []
            for hop in range(6):
                callees = adj.get(current_id, [])

                if not callees:
                    break

                # Classify callees into real (non-conditional) and conditional.
                # Prefer following a real call so the chain shows actual dispatch,
                # not branch predicates.
                real_candidates = []
                cond_candidates = []
                for cid, cname, cond, conc in callees:
                    is_cond = cname.startswith("<conditional:")
                    if is_cond:
                        cond_candidates.append((cid, cname, cond, conc))
                    else:
                        real_candidates.append((cid, cname, cond, conc))

                # Pick the next callee to follow.
                chosen = None
                if real_candidates:
                    # Prefer an unvisited real callee; fall back to first real.
                    for cand in real_candidates:
                        if cand[0] not in visited:
                            chosen = cand
                            break
                    if chosen is None:
                        chosen = real_candidates[0]
                elif cond_candidates:
                    # Only conditional callees — pick first unvisited conditional.
                    for cand in cond_candidates:
                        if cand[0] not in visited:
                            chosen = cand
                            break
                    if chosen is None:
                        chosen = cond_candidates[0]

                if chosen is None:
                    break

                cid, cname, cond, conc = chosen
                visited.add(cid)
                chain.append({"target": cname, "condition": cond or "",
                              "concurrency": conc or ""})
                if conc in ("spawn_target", "thread_spawn", "goroutine", "callback"):
                    concurrent_fns.append(cname)
                current_id = cid

            # Only include scenarios with non-empty call chains that contain
            # at least one real (non-conditional) function call
            has_real_call = any(not t["target"].startswith("<conditional:")
                               for t in chain)
            if not chain or not has_real_call:
                continue

            scenarios.append({
                "trigger": name,
                "domain": domain,
                "signature": sig or "",
                "resolved_chain": chain,
                "concurrent_window": [{"thread_fn": fn} for fn in concurrent_fns],
                "pruned_branches": [],
            })

        # Write scenarios JSON
        scenarios_path = os.path.join(outdir, ".code2database_scenarios.json")
        Path(scenarios_path).write_text(
            json.dumps({"scenarios": scenarios}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")

        # Write summary — use rich table format matching NetworkX path quality
        summary_path = os.path.join(outdir, "SCENARIOS_SUMMARY.md")
        lines = ["# Execution Scenarios\n"]
        lines.append(f"Total scenarios: {len(scenarios)}\n")

        def _linearize_chain(chain: list, noise_names: set = None) -> list:
            """Reduce a chain to a single linear path, skipping duplicates and
            conditional placeholders.

            ``noise_names`` is an optional set of identifiers (lowercased) supplied
            by the caller — typically read from the project profile's
            ``scenario_noise_names`` field. The tool itself is project-agnostic;
            project-specific macro/builtin names belong in profiles, not here.
            """
            noise_names = noise_names or set()
            linear = []
            seen = set()
            for step in chain:
                if isinstance(step, dict):
                    name = step.get("target", "")
                else:
                    name = str(step)
                if not name or name.startswith("<conditional:"):
                    continue
                if name in noise_names:
                    continue
                if linear and linear[-1] == name:
                    continue
                if name in seen:
                    continue
                linear.append(name)
                seen.add(name)
            return linear

        # Project-specific noise names live in the profile, not in this tool.
        _noise_names = set()
        if builder_profile:
            for n in builder_profile.get("scenario_noise_names", []) or []:
                _noise_names.add(n.lower())

        if scenarios:
            lines.append("")
            lines.append("| # | Trigger | Domain | Path | Concurrent |")
            lines.append("|---|---------|--------|------|------------|")
            for i, sc in enumerate(scenarios[:50], 1):
                trigger = sc.get("trigger", "")
                domain = sc.get("domain", "")
                chain = sc.get("resolved_chain", [])
                chain_names = _linearize_chain(chain, noise_names=_noise_names)
                path_str = " → ".join(chain_names[:8])
                if len(chain_names) > 8:
                    path_str += f" … (+{len(chain_names) - 8})"
                cw = sc.get("concurrent_window", [])
                concurrent_names = [w.get("thread_fn", "") for w in cw if w.get("thread_fn")]
                if not concurrent_names:
                    concurrent = "—"
                elif len(concurrent_names) == 1:
                    concurrent = concurrent_names[0]
                else:
                    head = ", ".join(concurrent_names[:3])
                    if len(concurrent_names) > 3:
                        concurrent = f"{head} … (+{len(concurrent_names) - 3} more)"
                    else:
                        concurrent = head
                lines.append(f"| {i} | {trigger} | {domain} | {path_str} | {concurrent} |")

        Path(summary_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    pass  # conn closed by context manager
    gc.collect()


def _build_architecture_flows_from_sqlite(db_path, outdir, source_root="", build_info=None):
    """Generate ARCHITECTURE_FLOWS.md from SQLite.

    Provides a narrative of core execution flows, domain cross-reference,
    and hub-based path summaries — matching the NetworkX path output quality.
    """
    from datetime import datetime

    with _open_db(db_path) as conn:

        project_name = os.path.basename(source_root) if source_root else "project"
        lines = [
            f"# Architecture Flows — {project_name}",
            "",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
            "This document describes the core execution flows through the codebase.",
            "Each flow traces the path from an API entry point through the system.",
            "",
            "",
        ]

        # --- Top 5 flows: trace from API entries to endpoints ---
        # Find API entries sorted by out-degree (most connected = most important)
        api_hubs = conn.execute(
            "SELECT f.id, f.name, f.domain, COUNT(e.invoked_id) as out_deg "
            "FROM functions f JOIN edges e ON e.invoker_id = f.id "
            f"WHERE {_label_where('API_entry', conn, table_alias='f')} AND e.relation = 'INVOKES' "
            "GROUP BY f.id ORDER BY out_deg DESC LIMIT 10"
        ).fetchall()

        # Find endpoints (out_end / unknown_end)
        endpoint_ids = set(
            row[0] for row in conn.execute(
                f"SELECT id FROM functions "
                f"WHERE {_label_where('out_end', conn)} OR {_label_where('unknown_end', conn)}"
            ).fetchall()
        )

        for i, (api_id, api_name, api_domain, out_deg) in enumerate(api_hubs[:5], 1):
            # Trace a path from this API entry toward endpoints (BFS, max 10 hops)
            path = [api_id]
            visited = {api_id}
            current = api_id
            chain_str_parts = [api_name]
            conditions = []
            concurrency = []
            domain_crossings = []
            prev_domain = api_domain

            for step in range(10):
                # Get callees of current node
                callees = conn.execute(
                    "SELECT e.invoked_id, f.name, f.domain, e.call_condition, e.concurrency "
                    "FROM edges e JOIN functions f ON e.invoked_id = f.id "
                    "WHERE e.invoker_id = ? AND e.relation = 'INVOKES' "
                    "ORDER BY e.call_order LIMIT 5", (current,)
                ).fetchall()

                if not callees:
                    break

                # Prefer callees that lead deeper (not visited yet)
                next_callee = None
                for cid, cname, cdom, cond, conc in callees:
                    if cid not in visited:
                        next_callee = (cid, cname, cdom, cond, conc)
                        break

                # If all visited, just use the first callee
                if next_callee is None:
                    next_callee = (callees[0][0], callees[0][1], callees[0][2],
                                   callees[0][3], callees[0][4])

                cid, cname, cdom, cond, conc = next_callee
                path.append(cid)
                chain_str_parts.append(cname)
                visited.add(cid)

                if cdom and prev_domain and cdom != prev_domain:
                    domain_crossings.append(
                        f"  - Step {step + 1}: {prev_domain} → {cdom} ({cname})")
                prev_domain = cdom or prev_domain

                if cond:
                    conditions.append(f"  - Step {step + 1}: [{cond}] → {cname}")
                if conc in ("spawn_target", "thread_spawn", "goroutine"):
                    concurrency.append(f"  - Step {step + 1}: {cname} (spawned thread)")
                elif conc == "callback":
                    concurrency.append(f"  - Step {step + 1}: {cname} (callback)")

                if cid in endpoint_ids:
                    break
                current = cid

            # Find the endpoint name
            ep_name = conn.execute(
                "SELECT name FROM functions WHERE id = ?", (path[-1],)
            ).fetchone()
            ep_name = ep_name[0] if ep_name else path[-1]

            lines.append(f"## Flow {i}: {api_name} → {ep_name}")
            lines.append("")
            lines.append(f"**Length**: {len(path) - 1} steps")
            lines.append(f"**Path**: {' → '.join(chain_str_parts[:12])}")
            if len(chain_str_parts) > 12:
                lines[-1] += " ..."
            lines.append("")

            if conditions:
                lines.append("**Conditions**:")
                lines.extend(conditions)
                lines.append("")
            if concurrency:
                lines.append("**Concurrency**:")
                lines.extend(concurrency)
                lines.append("")
            if domain_crossings:
                lines.append("**Domain Crossings**:")
                lines.extend(domain_crossings)
                lines.append("")
            lines.append("---")
            lines.append("")

        # --- Domain Flow Map ---
        lines.append("## Domain Flow Map")
        lines.append("")
        lines.append("Shows which domains call into which other domains.")
        lines.append("")

        domain_edges = {}
        for src, dst, cnt in conn.execute(
            "SELECT f1.domain, f2.domain, COUNT(*) "
            "FROM edges e "
            "JOIN functions f1 ON e.invoker_id = f1.id "
            "JOIN functions f2 ON e.invoked_id = f2.id "
            "WHERE e.relation = 'INVOKES' "
            "AND f1.domain != '' AND f2.domain != '' AND f1.domain != f2.domain "
            "GROUP BY f1.domain, f2.domain ORDER BY COUNT(*) DESC LIMIT 20"
        ).fetchall():
            domain_edges[(src, dst)] = cnt

        for (src, dst), count in sorted(domain_edges.items(), key=lambda x: -x[1]):
            lines.append(f"- **{src}** → **{dst}** ({count} edges)")
        lines.append("")

        # --- Hub Functions ---
        lines.append("## Hub Functions")
        lines.append("")
        lines.append("Most-connected functions (highest in-degree + out-degree).")
        lines.append("")

        # Exclude: empty nodes, external-domain nodes, file nodes, Python builtins
        # (Py_*, os/sys/io.* etc.), and bare builtin method/type names (append, get,
        # print, len, ...) which are auto-created external callees from method calls
        # on builtin types. Mirrors _is_likely_builtin in auto_enhance.py.
        try:
            from _builder.auto_enhance import _BUILTIN_METHOD_NAMES as _BUILTINS_SET
        except Exception:
            _BUILTINS_SET = frozenset()
        _builtin_stdlib_heads = (
            "__builtins__", "builtins", "os", "sys", "io", "math", "time",
            "threading", "asyncio", "logging", "collections", "itertools",
            "functools", "json", "re", "socket", "struct", "subprocess",
            "tarfile", "gzip", "hashlib", "hmac", "ssl", "urllib", "http",
            "sqlite3", "ctypes", "pprint", "weakref", "gc", "signal", "errno",
            "select", "queue", "multiprocessing", "concurrent", "pathlib",
            "shutil", "tempfile", "fnmatch", "glob", "argparse", "configparser",
            "csv", "datetime", "decimal", "fractions", "random", "statistics",
            "string", "textwrap", "unicodedata", "zlib", "bz2", "lzma", "copy",
            "enum", "typing", "dataclasses", "inspect", "traceback", "warnings",
            "contextlib", "unittest", "doctest", "pdb", "profile", "pstats",
            "timeit", "ast", "dis", "compile", "code", "codeop", "imp",
            "importlib", "pkgutil", "modulefinder", "pickle", "shelve",
            "marshal", "array", "bisect", "heapq", "operator", "abc", "types",
            "copyreg", "platform", "locale", "gettext", "calendar",
        )
        _builtin_name_in_list = ",".join(
            "'" + n.replace("'", "''") + "'" for n in sorted(_BUILTINS_SET) if n
        )
        _builtin_head_in_list = ",".join(
            "'" + h.replace("'", "''") + "'" for h in _builtin_stdlib_heads
        )
        builtin_name_filter = (
            "f.name NOT GLOB 'Py_*' "
            "AND f.name NOT GLOB 'PyObject_*' "
            "AND f.name NOT GLOB 'PyType_*' "
            "AND f.name NOT GLOB 'PyList_*' "
            "AND f.name NOT GLOB 'PyDict_*' "
            "AND f.name NOT GLOB 'PyTuple_*' "
            "AND f.name NOT GLOB 'PyBytes_*' "
            "AND f.name NOT GLOB 'PyUnicode_*' "
            "AND f.name NOT GLOB 'PyLong_*' "
            "AND f.name NOT GLOB 'PyFloat_*' "
            "AND f.name NOT GLOB 'PySet_*' "
            "AND f.name NOT GLOB 'PyMethod_*' "
            "AND f.name NOT GLOB 'PyMember_*' "
            "AND f.name NOT GLOB 'PySequence_*' "
            "AND f.name NOT GLOB 'PyMapping_*' "
            "AND f.name NOT GLOB 'PyNumber_*' "
            "AND f.name NOT GLOB 'PyIter_*' "
            + (f"AND f.name NOT IN ({_builtin_name_in_list}) " if _builtin_name_in_list else "")
            + (f"AND (instr(f.name, '.') = 0 OR "
               f"substr(f.name, 1, instr(f.name, '.') - 1) NOT IN ({_builtin_head_in_list})) "
               if _builtin_head_in_list else "")
            + "AND f.domain != 'external' "
            "AND f.domain NOT LIKE 'external_%' "
            "AND f.source_file != ''"
        )

        # Use JOIN aggregation instead of correlated subqueries for O(N) performance
        # on large graphs (1M+ functions).
        hubs = conn.execute(
            "SELECT f.name, f.domain, "
            "COALESCE(in_agg.in_deg, 0) as in_deg, "
            "COALESCE(out_agg.out_deg, 0) as out_deg "
            "FROM functions f "
            "LEFT JOIN (SELECT invoked_id, COUNT(*) as in_deg FROM edges WHERE relation = 'INVOKES' GROUP BY invoked_id) in_agg ON f.id = in_agg.invoked_id "
            "LEFT JOIN (SELECT invoker_id, COUNT(*) as out_deg FROM edges WHERE relation = 'INVOKES' GROUP BY invoker_id) out_agg ON f.id = out_agg.invoker_id "
            f"WHERE f.labels NOT LIKE '%is_empty%' AND {builtin_name_filter} "
            "ORDER BY (COALESCE(in_agg.in_deg, 0) + COALESCE(out_agg.out_deg, 0)) DESC LIMIT 20"
        ).fetchall()

        if hubs:
            lines.append("| Function | Domain | In | Out | Total |")
            lines.append("|----------|--------|-----|-----|-------|")
            for hname, hdom, indeg, outdeg in hubs:
                lines.append(f"| {hname} | {hdom} | {indeg} | {outdeg} | {indeg + outdeg} |")
        lines.append("")

        flows_path = os.path.join(outdir, "ARCHITECTURE_FLOWS.md")
        Path(flows_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    pass  # conn closed by context manager
    gc.collect()
    return flows_path


def _build_context_pack_from_sqlite(db_path, outdir, source_root="", build_info=None):
    """Generate LLM context pack from SQLite.

    Creates micro, lite, standard, and full packs.
    """
    with _open_db(db_path) as conn:

        node_count = conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]
        edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        domain_count = conn.execute("SELECT COUNT(DISTINCT domain) FROM functions").fetchone()[0]

        # Micro pack: just stats
        micro = {
            "total_functions": node_count,
            "total_edges": edge_count,
            "total_domains": domain_count,
        }
        micro_path = os.path.join(outdir, ".code2database_context_pack_micro.json")
        Path(micro_path).write_text(
            json.dumps(micro, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        micro_md_path = os.path.join(outdir, ".code2database_context_pack_micro.md")
        Path(micro_md_path).write_text(
            f"# Context Pack (Micro)\n\n"
            f"- Functions: {node_count:,}\n- Edges: {edge_count:,}\n- Domains: {domain_count:,}\n",
            encoding="utf-8")

        # Lite pack: top API entries + domain list
        api_rows = conn.execute(
            f"SELECT name, domain, signature, id FROM functions "
            f"WHERE {_label_where('API_entry', conn)} ORDER BY name LIMIT 100"
        ).fetchall()
        domains = [row[0] for row in conn.execute(
            "SELECT DISTINCT domain FROM functions ORDER BY domain"
        ).fetchall()]

        lite = {
            "total_functions": node_count,
            "total_edges": edge_count,
            "domains": domains,
            "api_entries": [{"name": r[0], "domain": r[1], "signature": r[2] or ""}
                            for r in api_rows],
        }
        lite_path = os.path.join(outdir, ".code2database_context_pack_lite.json")
        Path(lite_path).write_text(
            json.dumps(lite, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        lite_md_path = os.path.join(outdir, ".code2database_context_pack_lite.md")
        Path(lite_md_path).write_text(
            f"# Context Pack (Lite)\n\n"
            f"- Functions: {node_count:,}\n- Edges: {edge_count:,}\n"
            f"- API entries: {len(api_rows)}\n- Domains: {len(domains)}\n",
            encoding="utf-8")

        # Standard pack: API entries with call relationships
        api_names = [r[0] for r in api_rows]
        api_ids = [r[3] for r in api_rows]
        standard = {
            "total_functions": node_count,
            "total_edges": edge_count,
            "domains": domains,
            "api_entries": [],
        }
        # Batch query: get callees for ALL APIs in 2 queries (was 2N)
        all_callees = {}
        all_callers = {}
        if api_ids:
            _ph = ','.join('?' * len(api_ids))
            for api_id, name, cond, conc, conf in conn.execute(
                f"SELECT e.invoker_id, f.name, e.call_condition, e.concurrency, e.confidence "
                f"FROM edges e JOIN functions f ON e.invoked_id = f.id "
                f"WHERE e.invoker_id IN ({_ph}) "
                f"AND e.relation NOT IN ('CONTAINS', 'IMPORTS')", api_ids):
                all_callees.setdefault(api_id, []).append(
                    {"name": name, "condition": cond or "",
                     "concurrency": conc or "", "confidence": conf or ""})
            for api_id, name, cond, conc, conf in conn.execute(
                f"SELECT e.invoked_id, f.name, e.call_condition, e.concurrency, e.confidence "
                f"FROM edges e JOIN functions f ON e.invoker_id = f.id "
                f"WHERE e.invoked_id IN ({_ph}) "
                f"AND e.relation NOT IN ('CONTAINS', 'IMPORTS')", api_ids):
                all_callers.setdefault(api_id, []).append(
                    {"name": name, "condition": cond or "",
                     "concurrency": conc or "", "confidence": conf or ""})
        for name, domain, sig, api_id in api_rows:
            standard["api_entries"].append({
                "name": name, "domain": domain, "signature": sig or "",
                "callees": all_callees.get(api_id, [])[:20],
                "callers": all_callers.get(api_id, [])[:20],
            })
        standard_path = os.path.join(outdir, ".code2database_context_pack_standard.json")
        Path(standard_path).write_text(
            json.dumps(standard, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        # Full pack: includes thread entries, callbacks, and domain summaries
        thread_rows = conn.execute(
            f"SELECT name, domain, signature FROM functions "
            f"WHERE {_label_where('thread_processor', conn)} LIMIT 100"
        ).fetchall()
        callback_rows = conn.execute(
            f"SELECT name, domain, signature FROM functions "
            f"WHERE {_label_where('callback_func', conn)} LIMIT 100"
        ).fetchall()
        full = dict(standard)
        full["thread_entries"] = [{"name": r[0], "domain": r[1], "signature": r[2] or ""}
                                  for r in thread_rows]
        full["callback_entries"] = [{"name": r[0], "domain": r[1], "signature": r[2] or ""}
                                    for r in callback_rows]
        full_path = os.path.join(outdir, ".code2database_context_pack.json")
        Path(full_path).write_text(
            json.dumps(full, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    pass  # conn closed by context manager
    gc.collect()
    return full_path


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_stats_consistency_sqlite(db_path, pipeline_node_count):
    """Validate consistency between pipeline node count and SQLite count."""
    with _open_db(db_path) as conn:
        sqlite_node_count = conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]
        sqlite_edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    pass  # conn closed by context manager

    delta = abs(pipeline_node_count - sqlite_node_count)
    delta_pct = (delta / max(pipeline_node_count, 1)) * 100

    return {
        "pipeline_count": pipeline_node_count,
        "sqlite_count": sqlite_node_count,
        "delta": delta,
        "delta_pct": round(delta_pct, 2),
        "matches": delta == 0,
        "sqlite_edge_count": sqlite_edge_count,
    }
