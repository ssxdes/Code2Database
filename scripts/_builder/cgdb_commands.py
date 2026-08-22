"""cgdb_commands — CLI wrappers for cgdb (code graph database) queries.

These commands wrap SQLiteCGDBStore + VersionController query methods so
they can be invoked from the code2database_builder CLI. Each cmd_* function
takes argparse args and prints JSON to stdout.

DB convention: <graph_dir>/code2database.db (same as MCP server).
"""
import json
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional

from _builder.cgdb_store import SQLiteCGDBStore
from _builder.cgdb_versions import VersionController


def _open_store(graph_dir: str) -> Optional[SQLiteCGDBStore]:
    """Open SQLiteCGDBStore at <graph_dir>/code2database.db. Returns None and
    prints an error to stderr if DB is missing or cgdb tables absent."""
    db_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.exists(db_path):
        print(f"Error: {db_path} not found. Run scan with --extraction-backend auto "
              "or clang to populate cgdb tables.", file=sys.stderr)
        return None
    try:
        store = SQLiteCGDBStore(db_path)
        conn = store._ensure_conn()
        conn.execute("SELECT 1 FROM cgdb_nodes LIMIT 1").fetchone()
        return store
    except Exception as exc:
        print(f"Error opening cgdb store: {exc}", file=sys.stderr)
        return None


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _resolve_node_id(store: SQLiteCGDBStore, name_or_id: str,
                    prefer_edges: str = "in") -> Optional[int]:
    """Accept either a numeric node_id or a function/symbol name. Returns
    the node_id, or None if not found.

    Resolution order:
    1. Numeric ID — return as-is.
    2. Exact FQN match (edge-aware) — handles cases where the user passed the
       legacy_function_id (e.g., 'root_discard_buffer'). BUT only if that
       node has the preferred edge type; if not, fall through to step 2c/3
       because edges may reference a duplicate node (same name, different fqn).
       `prefer_edges="in"` prefers nodes with incoming edges (right for
       find-invokers); `"out"` prefers nodes with outgoing edges (right for
       find-invoked); `"any"` skips this edge-aware filtering.
    2c. FQN-as-name fallback (edge-aware) — when the user passes a FQN that
       has a domain prefix (e.g., 'root_discard_buffer'), strip the prefix
       to get the function name ('discard_buffer') and look it up by name.
       This handles the common duplicate-node scenario where edges reference
       the fqn=name node, not the fqn=legacy_function_id node.
    3. Exact name match (edge-aware) — try the input as a name first, then
       the prefix-stripped candidate. Prefers the node that has the
       preferred edge type (i.e., is actually used in the graph in the
       direction of interest).
    4. Exact FQN match (no edge requirement) — fallback if no edge-aware
       matches succeeded. Returns any node with this fqn, even if it has no
       edges (e.g., for leaf nodes or external symbols).
    5. FTS5 search fallback.
    """
    if name_or_id.isdigit():
        return int(name_or_id)
    conn = store._ensure_conn()
    edge_join = ""
    edge_having = ""
    if prefer_edges == "in":
        edge_join = "LEFT JOIN cgdb_edges e ON e.dst_id = n.id"
        edge_having = "HAVING COUNT(e.id) > 0"
    elif prefer_edges == "out":
        edge_join = "LEFT JOIN cgdb_edges e ON e.src_id = n.id"
        edge_having = "HAVING COUNT(e.id) > 0"
    # 2. Exact FQN match — but only if the node has the preferred edge type
    if edge_join:
        try:
            row = conn.execute(
                f"SELECT n.id FROM cgdb_nodes n {edge_join} "
                f"WHERE n.fqn = ? GROUP BY n.id {edge_having} LIMIT 1",
                (name_or_id,)
            ).fetchone()
            if row:
                return int(row[0])
        except Exception:
            pass
    # 2c. FQN-as-name fallback: when the user passes a legacy_function_id
    # (e.g., 'root_discard_buffer') that was stored as fqn, but the *real*
    # node (the one edges reference) has fqn=name='discard_buffer'. Try
    # stripping the domain prefix to get the function name, then match by
    # name with edge awareness. Domain prefix = everything up to and
    # including the first underscore when it is short (<=24 chars) and the
    # remainder is non-empty.
    name_candidates = [name_or_id]
    if "_" in name_or_id:
        prefix, _, rest = name_or_id.partition("_")
        if rest and 0 < len(prefix) <= 24 and rest[0].isalpha():
            name_candidates.append(rest)
    for candidate in name_candidates:
        try:
            order_clause = "ORDER BY COUNT(e.id) DESC" if edge_join else ""
            rows = conn.execute(
                f"SELECT n.id FROM cgdb_nodes n {edge_join} "
                f"WHERE n.name = ? GROUP BY n.id {order_clause} LIMIT 1",
                (candidate,)
            ).fetchall()
            if rows:
                return int(rows[0][0])
        except Exception:
            pass
    # 4. Exact FQN match without edge requirement (fallback for leaf nodes)
    try:
        row = conn.execute(
            "SELECT id FROM cgdb_nodes WHERE fqn = ? LIMIT 1",
            (name_or_id,)
        ).fetchone()
        if row:
            return int(row[0])
    except Exception:
        pass
    # 5. FTS5 search fallback
    rows = store.search_symbols(name_or_id, limit=1)
    if not rows:
        return None
    return rows[0]["id"]


# ---- CLI command handlers ----

def cmd_cgdb_query(args):
    """Generic cgdb query — FTS5 symbol search or get_node by id."""
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        if args.node_id:
            _print_json(store.get_node(int(args.node_id)))
        else:
            _print_json(store.search_symbols(args.query, kind=args.kind,
                                              limit=args.limit))
    finally:
        store.close()


def cmd_cgdb_time_travel(args):
    """Query node state at a past version (by commit_hash or version_id)."""
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        vc = VersionController(store._db_path, conn=store._ensure_conn())
        if args.commit_hash:
            version_id = vc.get_version_by_commit(args.commit_hash)
            if version_id is None:
                print(f"Error: no version found for commit {args.commit_hash}",
                      file=sys.stderr)
                sys.exit(1)
        else:
            version_id = int(args.version_id)
        node_id = _resolve_node_id(store, args.node)
        if node_id is None:
            print(f"Error: node '{args.node}' not found", file=sys.stderr)
            sys.exit(1)
        result = vc.time_travel_query_node(node_id, version_id)
        _print_json({"version_id": version_id, "node_state": result})
    finally:
        store.close()


def cmd_cgdb_configs_for(args):
    """List config_predicate text_form(s) that gate a given node."""
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        node_id = _resolve_node_id(store, args.node)
        if node_id is None:
            print(f"Error: node '{args.node}' not found", file=sys.stderr)
            sys.exit(1)
        _print_json({"node_id": node_id,
                     "configs": store.find_configs_for(node_id)})
    finally:
        store.close()


def cmd_cgdb_ops_impls(args):
    """Find ops_bind implementations for a given field name."""
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        _print_json(store.find_ops_impls(args.field, struct_type=args.struct))
    finally:
        store.close()


def cmd_cgdb_cfg_paths(args):
    """Enumerate CFG paths through a function (entry → exit blocks)."""
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        func_id = _resolve_node_id(store, args.function)
        if func_id is None:
            print(f"Error: function '{args.function}' not found", file=sys.stderr)
            sys.exit(1)
        _print_json(store.find_cfg_paths(func_id, max_len=args.max_len))
    finally:
        store.close()


def cmd_cgdb_data_flow(args):
    """Show data_flow entries (def-use chains) for a variable node."""
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        var_id = _resolve_node_id(store, args.var)
        if var_id is None:
            print(f"Error: variable '{args.var}' not found", file=sys.stderr)
            sys.exit(1)
        _print_json(store.find_data_flow(var_id))
    finally:
        store.close()


def cmd_cgdb_race_check(args):
    """Heuristic race-condition check for a function."""
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        func_id = _resolve_node_id(store, args.function)
        if func_id is None:
            print(f"Error: function '{args.function}' not found", file=sys.stderr)
            sys.exit(1)
        _print_json(store.check_race_condition(func_id))
    finally:
        store.close()


def cmd_cgdb_index_status(args):
    """Overall cgdb index statistics: node/edge counts by kind, file count,
    predicate count."""
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        _print_json(store.index_status())
    finally:
        store.close()


def cmd_cgdb_sql(args):
    """Run an arbitrary SQL query against the cgdb database.

    Read-only — rejects any statement that isn't a SELECT, WITH, or EXPLAIN.
    Results are returned as JSON (list of row dicts) or markdown table per
    --format. Use for cross-table joins and ad-hoc analysis that the
    Cypher-subset query language doesn't cover directly.

    Tables: cgdb_nodes, cgdb_edges, cgdb_files, cgdb_types, cgdb_includes,
    cgdb_invoke_sites, cgdb_predicates, cgdb_ops_bindings, basic_blocks,
    cfg_edges, data_flow, sync_primitives, happens_before, alias_sets,
    doc_comments, conditions, config_predicates, graph_versions,
    audit_log, change_log, communities, domain_stats, edge_metadata,
    entry_scores, field_access, functions, global_access, nodes_fts,
    node_metadata.
    """
    db_path = os.path.join(args.graph, "code2database.db")
    if not os.path.exists(db_path):
        print(f"Error: {db_path} not found.", file=sys.stderr)
        sys.exit(1)

    sql = args.sql.strip()
    if not sql:
        print("Error: --sql is required", file=sys.stderr)
        sys.exit(1)

    # Read-only guard: only allow SELECT/WITH/EXPLAIN/PRAGMA (case-insensitive)
    head = sql.lstrip().split(None, 1)[0].upper() if sql.lstrip() else ""
    if head not in ("SELECT", "WITH", "EXPLAIN", "PRAGMA"):
        print(f"Error: only SELECT/WITH/EXPLAIN/PRAGMA allowed (got: {head})",
              file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql)
        rows = [dict(r) for r in cur.fetchall()]
    except sqlite3.Error as exc:
        print(f"SQL error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()

    fmt = getattr(args, "format", "json")
    if fmt == "md":
        if not rows:
            print("No results.")
            return
        cols = list(rows[0].keys())
        print("| " + " | ".join(cols) + " |")
        print("| " + " | ".join("---" for _ in cols) + " |")
        for row in rows:
            print("| " + " | ".join(str(row.get(c, "")) for c in cols) + " |")
    else:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))


PREDEFINED_VIEWS = {
    "hub-functions": (
        "Top-20 most-invoking functions (potential refactoring targets).",
        "SELECT n.name, n.fqn, COUNT(e.id) AS out_invokes "
        "FROM cgdb_nodes n JOIN cgdb_edges e ON e.src_id=n.id "
        "WHERE e.kind='INVOKES' AND n.kind='function' "
        "GROUP BY n.id ORDER BY out_invokes DESC LIMIT 20"
    ),
    "hub-targets": (
        "Top-20 most-invoked functions (potential API hot spots).",
        "SELECT n.name, n.fqn, COUNT(e.id) AS in_invokes "
        "FROM cgdb_nodes n JOIN cgdb_edges e ON e.dst_id=n.id "
        "WHERE e.kind='INVOKES' AND n.kind='function' "
        "GROUP BY n.id ORDER BY in_invokes DESC LIMIT 20"
    ),
    "external-callees": (
        "Functions referenced as callees but never defined in this scan "
        "(external/builtin/missing).",
        "SELECT DISTINCT n.name, n.fqn FROM cgdb_nodes n "
        "WHERE n.kind='function' AND n.attrs LIKE '%\"external\": true%' "
        "ORDER BY n.name LIMIT 100"
    ),
    "sync-hotspots": (
        "Top-20 functions by lock-acquire count (concurrency hotspots).",
        "SELECT n.name, n.fqn, COUNT(sp.id) AS sync_count "
        "FROM cgdb_nodes n JOIN sync_primitives sp ON sp.function_id=n.id "
        "WHERE n.kind='function' "
        "GROUP BY n.id ORDER BY sync_count DESC LIMIT 20"
    ),
    "data-flow-hotspots": (
        "Top-20 functions by def-use chain count (data-flow complexity).",
        "SELECT n.name, n.fqn, COUNT(df.id) AS df_count "
        "FROM cgdb_nodes n JOIN data_flow df ON df.function_id=n.id "
        "WHERE n.kind='function' "
        "GROUP BY n.id ORDER BY df_count DESC LIMIT 20"
    ),
    "alias-clusters": (
        "Pointer alias sets grouped by function, with kind and confidence.",
        "SELECT n.name AS fn_name, a.kind, a.confidence, COUNT(*) AS cluster_size "
        "FROM alias_sets a JOIN cgdb_nodes n ON n.id=a.ptr1_node_id "
        "GROUP BY n.id, a.kind, a.confidence "
        "ORDER BY cluster_size DESC LIMIT 50"
    ),
    "doc-coverage": (
        "Doc-comment coverage: % of functions with doc_comments.",
        "SELECT "
        "  COUNT(*) AS total_functions, "
        "  SUM(CASE WHEN dc.node_id IS NOT NULL THEN 1 ELSE 0 END) AS documented, "
        "  ROUND(100.0 * SUM(CASE WHEN dc.node_id IS NOT NULL THEN 1 ELSE 0 END) / COUNT(*), 2) AS pct "
        "FROM cgdb_nodes n "
        "LEFT JOIN (SELECT DISTINCT node_id FROM doc_comments) dc ON dc.node_id=n.id "
        "WHERE n.kind='function'"
    ),
    "file-includes": (
        "Include/import dependency edges (top 100).",
        "SELECT f.path AS source_file, inc.included_path, inc.is_system "
        "FROM cgdb_includes inc JOIN cgdb_files f ON f.id=inc.source_file_id "
        "ORDER BY f.path LIMIT 100"
    ),
    "version-history": (
        "Recent graph_versions rows (newest first).",
        "SELECT version_id, commit_hash, commit_short, compiled_at "
        "FROM graph_versions ORDER BY version_id DESC LIMIT 20"
    ),
}


def cmd_cgdb_views(args):
    """List predefined views, or run one by name. Predefined views are
    curated SQL queries for common analysis tasks (hub functions, sync
    hotspots, doc coverage, alias clusters, etc.) — useful when you don't
    want to write raw SQL but want richer cross-table analysis than the
    Cypher-subset query language provides.
    """
    name = getattr(args, "name", None)
    if not name:
        print(json.dumps([
            {"name": k, "description": v[0]}
            for k, v in PREDEFINED_VIEWS.items()
        ], ensure_ascii=False, indent=2))
        return

    if name not in PREDEFINED_VIEWS:
        print(f"Error: unknown view '{name}'. Available: "
              f"{', '.join(sorted(PREDEFINED_VIEWS.keys()))}", file=sys.stderr)
        sys.exit(1)

    description, sql = PREDEFINED_VIEWS[name]
    db_path = os.path.join(args.graph, "code2database.db")
    if not os.path.exists(db_path):
        print(f"Error: {db_path} not found.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql)
        rows = [dict(r) for r in cur.fetchall()]
    except sqlite3.Error as exc:
        print(f"SQL error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()

    fmt = getattr(args, "format", "json")
    if fmt == "md":
        if not rows:
            print("No results.")
            return
        cols = list(rows[0].keys())
        print("| " + " | ".join(cols) + " |")
        print("| " + " | ".join("---" for _ in cols) + " |")
        for row in rows:
            print("| " + " | ".join(str(row.get(c, "")) for c in cols) + " |")
    else:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))


def cmd_cgdb_schema_version(args):
    """Report current cgdb schema version and migration history.

    Outputs JSON with:
      - current_version: schema version recorded in meta table
      - latest_version: latest version supported by this binary
      - migrations_available: list of (target_version, description) pairs
      - needs_migration: True if current < latest
    """
    db_path = os.path.join(args.graph, "code2database.db")
    if not os.path.exists(db_path):
        print(f"Error: {db_path} not found.", file=sys.stderr)
        sys.exit(1)
    try:
        from _builder.cgdb_schema import CGDB_SCHEMA_VERSION
        from _builder.cgdb_migrations import MIGRATIONS
    except ImportError:
        print(f"Error: cgdb_schema/cgdb_migrations not importable.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    try:
        # Ensure meta table exists
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", ("cgdb_schema_version",)
        ).fetchone()
        current = int(row[0]) if row else 0
        migrations_available = [
            {"target_version": tv, "function": fn.__name__, "doc": fn.__doc__.strip().split("\n")[0] if fn.__doc__ else ""}
            for tv, fn in MIGRATIONS
        ]
        _print_json({
            "current_version": current,
            "latest_version": CGDB_SCHEMA_VERSION,
            "needs_migration": current < CGDB_SCHEMA_VERSION,
            "migrations_available": migrations_available,
            "db_path": db_path,
        })
    finally:
        conn.close()


def cmd_cgdb_versions(args):
    """List graph_versions rows (newest first), or diff two versions.

    With --diff V1 V2, returns the nodes/edges added (in V2 not in V1) and
    removed (in V1 not in V2). With --diff-names, the diff includes the
    name/fqn of each node/edge for human inspection (otherwise just IDs).
    """
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        vc = VersionController(store._db_path, conn=store._ensure_conn())
        if args.diff:
            v1, v2 = args.diff
            diff = vc.diff_versions(int(v1), int(v2))
            if getattr(args, "diff_names", False):
                conn = store._ensure_conn()
                def _enrich_nodes(ids):
                    if not ids:
                        return []
                    placeholders = ",".join("?" * len(ids))
                    rows = conn.execute(
                        f"SELECT id, name, fqn, kind FROM cgdb_nodes "
                        f"WHERE id IN ({placeholders})",
                        ids
                    ).fetchall()
                    return [{"id": r[0], "name": r[1], "fqn": r[2], "kind": r[3]}
                            for r in rows]
                def _enrich_edges(ids):
                    if not ids:
                        return []
                    placeholders = ",".join("?" * len(ids))
                    rows = conn.execute(
                        f"SELECT e.id, e.kind, sn.name AS src_name, dn.name AS dst_name "
                        f"FROM cgdb_edges e "
                        f"LEFT JOIN cgdb_nodes sn ON sn.id=e.src_id "
                        f"LEFT JOIN cgdb_nodes dn ON dn.id=e.dst_id "
                        f"WHERE e.id IN ({placeholders})",
                        ids
                    ).fetchall()
                    return [{"id": r[0], "kind": r[1],
                             "src_name": r[2], "dst_name": r[3]}
                            for r in rows]
                diff["added_nodes"] = _enrich_nodes(diff["added_nodes"])
                diff["removed_nodes"] = _enrich_nodes(diff["removed_nodes"])
                diff["added_edges"] = _enrich_edges(diff["added_edges"])
                diff["removed_edges"] = _enrich_edges(diff["removed_edges"])
            _print_json(diff)
        else:
            _print_json(vc.list_versions(limit=args.limit))
    finally:
        store.close()


def cmd_cgdb_find_invokers(args):
    """Find invokers (reverse closure) of a node via recursive CTE."""
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        node_id = _resolve_node_id(store, args.node, prefer_edges="in")
        if node_id is None:
            print(f"Error: node '{args.node}' not found", file=sys.stderr)
            sys.exit(1)
        _print_json(store.find_invokers(
            node_id, depth=args.depth,
            edge_types=args.edge_types.split(",") if args.edge_types else None,
            limit=args.limit,
            include_vtable_dispatch=getattr(args, "include_vtable_dispatch", False)))
    finally:
        store.close()


def cmd_cgdb_find_invoked(args):
    """Find invoked (forward closure) of a node via recursive CTE."""
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        node_id = _resolve_node_id(store, args.node, prefer_edges="out")
        if node_id is None:
            print(f"Error: node '{args.node}' not found", file=sys.stderr)
            sys.exit(1)
        _print_json(store.find_invoked(
            node_id, depth=args.depth,
            edge_types=args.edge_types.split(",") if args.edge_types else None,
            limit=args.limit,
            include_vtable_dispatch=getattr(args, "include_vtable_dispatch", False)))
    finally:
        store.close()


def cmd_cgdb_path(args):
    """Find an invoke path from src to dst via recursive CTE."""
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        src_id = _resolve_node_id(store, args.src, prefer_edges="out")
        dst_id = _resolve_node_id(store, args.dst, prefer_edges="in")
        if src_id is None or dst_id is None:
            missing_src = args.src if src_id is None else None
            missing_dst = args.dst if dst_id is None else None
            print(
                f"Error: src '{args.src}' or dst '{args.dst}' not found",
                file=sys.stderr,
            )
            # RPT-KERNEL-D14: emit actionable subsystem hints
            try:
                from _builder.coverage_report import path_not_found_hints
                hints = path_not_found_hints(
                    args.graph,
                    missing_src=missing_src,
                    missing_dst=missing_dst,
                )
                print(f"Hints: {hints['suggestion']}", file=sys.stderr)
                if hints.get("coverage_report_path"):
                    print(
                        f"Coverage report: {hints['coverage_report_path']}",
                        file=sys.stderr,
                    )
                if hints.get("missing_common_subsystems"):
                    print(
                        f"Missing common subsystems: "
                        f"{', '.join(hints['missing_common_subsystems'][:5])}",
                        file=sys.stderr,
                    )
            except Exception:
                pass  # Hints are best-effort; never block the error path
            sys.exit(1)
        _print_json(store.invoke_path(src_id, dst_id, max_len=args.max_len))
    finally:
        store.close()


def cmd_cgdb_definition(args):
    """Find definition nodes (function/var/field/typedef) by name."""
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        _print_json(store.get_definition(args.symbol, limit=args.limit))
    finally:
        store.close()


def cmd_cgdb_function_body(args):
    """Return a function's body source text."""
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        _print_json(store.get_function_body(args.function))
    finally:
        store.close()


def cmd_cgdb_struct_layout(args):
    """Return a struct/union's field layout."""
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        _print_json(store.get_struct_layout(args.struct))
    finally:
        store.close()


def cmd_cgdb_type_definition(args):
    """Find type definitions (struct/union/enum/typedef/class) by name."""
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        _print_json(store.find_type_definition(args.type_name, limit=args.limit))
    finally:
        store.close()


def cmd_cgdb_nodes_under_config(args):
    """Find all nodes gated by a given config predicate."""
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        _print_json(store.find_nodes_under_config(args.config, limit=args.limit))
    finally:
        store.close()


def cmd_cgdb_path_feasible(args):
    """Check feasibility of a CFG path through blocks (uses Z3 if available).

    With --with-configs, also evaluates any config predicates attached to
    the blocks/edges along the path under the provided macro bindings.
    """
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        path = [int(x) for x in args.path.split(",")]
        result = store.check_path_feasible(path)
        # Optional config-predicate layer
        with_configs = getattr(args, "with_configs", "") or ""
        if with_configs:
            try:
                from _builder.path_feasibility import (
                    check_config_feasible, parse_macro_bindings,
                )
                macro_bindings = parse_macro_bindings(with_configs)
                cp_map = store.load_config_predicates_map()
                # Collect config predicates from blocks along the path
                conn = store._ensure_conn()
                placeholders = ",".join("?" * len(path))
                rows = conn.execute(
                    f"SELECT DISTINCT config_predicate_id FROM cgdb_nodes "
                    f"WHERE id IN ({placeholders}) "
                    f"AND config_predicate_id IS NOT NULL",
                    path
                ).fetchall()
                cfg_preds = []
                for r in rows:
                    pid = r[0]
                    if pid in cp_map:
                        tf = cp_map[pid].get("text_form", "")
                        if tf and tf not in cfg_preds:
                            cfg_preds.append(tf)
                result["config_predicates"] = cfg_preds
                result["macro_bindings"] = macro_bindings
                result["config_feasibility"] = check_config_feasible(
                    cfg_preds, macro_bindings)
            except Exception as e:
                result["config_feasibility"] = {
                    "feasible": None,
                    "reason": f"config-predicate layer unavailable: {e}",
                }
        _print_json(result)
    finally:
        store.close()


def cmd_cgdb_get_source(args):
    """Return the source text for a node, with byte-precise attribution.

    Resolution order:
    1. source_snippet column (if populated at scan time)
    2. Read source file directly using byte_start..byte_end offsets

    Output includes the source text, file path, byte range, and line range.
    When --snippet-only is set, only return source_snippet (no file read).
    When --with-context N is set, include N bytes of surrounding context.
    """
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        node_id = _resolve_node_id(store, args.node)
        if node_id is None:
            print(f"Error: node '{args.node}' not found", file=sys.stderr)
            sys.exit(1)
        conn = store._ensure_conn()
        row = conn.execute(
            "SELECT n.id, n.kind, n.name, n.fqn, n.line, n.col, "
            "n.byte_start, n.byte_end, n.source_snippet, n.attrs, "
            "f.path, f.content_hash "
            "FROM cgdb_nodes n LEFT JOIN cgdb_files f ON n.file_id = f.id "
            "WHERE n.id = ?",
            (node_id,)
        ).fetchone()
        if row is None:
            print(f"Error: node_id {node_id} not in cgdb_nodes", file=sys.stderr)
            sys.exit(1)
        (nid, kind, name, fqn, line, col, byte_start, byte_end,
         source_snippet, attrs_json, file_path, content_hash) = row
        result = {
            "node_id": nid,
            "kind": kind,
            "name": name,
            "fqn": fqn,
            "line": line,
            "col": col,
            "byte_start": byte_start or 0,
            "byte_end": byte_end or 0,
            "file_path": file_path,
            "content_hash": content_hash,
        }
        snippet = source_snippet or ""
        if snippet and not getattr(args, "with_context", 0):
            result["source_text"] = snippet
            result["source"] = "source_snippet"
        else:
            if not file_path:
                result["source_text"] = snippet
                result["source"] = "source_snippet_no_file"
                _print_json(result)
                return
            try:
                with open(file_path, "rb") as fh:
                    raw = fh.read()
            except OSError as exc:
                result["source_text"] = snippet
                result["source"] = f"file_read_failed: {exc}"
                _print_json(result)
                return
            bs = byte_start or 0
            be = byte_end or 0
            ctx = int(getattr(args, "with_context", 0) or 0)
            if ctx > 0:
                lo = max(0, bs - ctx)
                hi = min(len(raw), be + ctx)
                chunk = raw[lo:hi]
                result["context_byte_start"] = lo
                result["context_byte_end"] = hi
                result["context_offset_in_chunk"] = bs - lo
                result["context_length_in_chunk"] = be - bs
            else:
                chunk = raw[bs:be] if be > bs else raw[bs:bs]
            try:
                text = chunk.decode("utf-8", errors="replace")
            except Exception:
                text = repr(chunk)
            result["source_text"] = text
            result["source"] = "file_bytes"
            if snippet and not ctx:
                result["source_snippet"] = snippet
        _print_json(result)
    finally:
        store.close()


def cmd_cgdb_layer_summary(args):
    """Write cgdb_layer_summary.md — a human-readable report of all 13 cgdb
    semantic tables with row counts, coverage metrics, and per-layer notes.

    Layers covered:
      L0: graph_versions (time-travel)
      L1: cgdb_nodes, cgdb_edges, cgdb_files (AST graph)
      L2: cgdb_types (type system)
      L3: conditions (CFG branch predicates)
      L3.5: config_predicates (#ifdef trees)
      L4: basic_blocks, cfg_edges (CFG)
      L5: data_flow (def-use chains)
      L6: alias_sets (alias analysis)
      L7: sync_primitives, happens_before (concurrency)
      L8: ops_bindings (vtable / ops_table)
      L9: doc_comments (raw comment text)
      L10: cgdb_includes (file dependencies)
      L11: invoke_sites (call-site attributes)
    """
    store = _open_store(args.graph)
    if store is None:
        sys.exit(1)
    try:
        conn = store._ensure_conn()

        def _count(table: str) -> int:
            try:
                return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                return -1  # table missing

        def _count_where(table: str, where: str) -> int:
            try:
                return conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {where}"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                return -1

        def _group_counts(table: str, col: str, limit: int = 10):
            try:
                rows = conn.execute(
                    f"SELECT {col}, COUNT(*) FROM {table} "
                    f"GROUP BY {col} ORDER BY COUNT(*) DESC LIMIT {limit}"
                ).fetchall()
                return [(r[0], r[1]) for r in rows]
            except sqlite3.OperationalError:
                return []

        # Pull counts for every cgdb table.
        layer_counts = {
            "L0 graph_versions": _count("graph_versions"),
            "L1 cgdb_nodes": _count("cgdb_nodes"),
            "L1 cgdb_edges": _count("cgdb_edges"),
            "L1 cgdb_files": _count("cgdb_files"),
            "L2 cgdb_types": _count("cgdb_types"),
            "L3 conditions": _count("conditions"),
            "L3.5 config_predicates": _count("config_predicates"),
            "L4 basic_blocks": _count("basic_blocks"),
            "L4 cfg_edges": _count("cfg_edges"),
            "L5 data_flow": _count("data_flow"),
            "L6 alias_sets": _count("alias_sets"),
            "L7 sync_primitives": _count("sync_primitives"),
            "L7 happens_before": _count("happens_before"),
            "L8 ops_bindings": _count("ops_bindings"),
            "L9 doc_comments": _count("doc_comments"),
            "L10 cgdb_includes": _count("cgdb_includes"),
            "L11 invoke_sites": _count("invoke_sites"),
        }

        nodes_by_kind = _group_counts("cgdb_nodes", "kind", 15)
        edges_by_kind = _group_counts("cgdb_edges", "kind", 15)
        sync_by_kind = _group_counts("sync_primitives", "kind", 10)

        # Coverage metrics — fraction of nodes that have typed metadata.
        node_total = layer_counts["L1 cgdb_nodes"]
        nodes_with_type = _count_where(
            "cgdb_nodes", "type_id IS NOT NULL"
        ) if node_total > 0 else 0
        nodes_with_pred = _count_where(
            "cgdb_nodes", "config_predicate_id IS NOT NULL"
        ) if node_total > 0 else 0
        nodes_with_enclosing = _count_where(
            "cgdb_nodes", "enclosing_symbol_id IS NOT NULL"
        ) if node_total > 0 else 0
        nodes_with_snippet = _count_where(
            "cgdb_nodes", "source_snippet IS NOT NULL AND source_snippet != ''"
        ) if node_total > 0 else 0
        nodes_with_description = _count_where(
            "cgdb_nodes", "description != ''"
        ) if node_total > 0 else 0
        nodes_with_commit = _count_where(
            "cgdb_nodes", "commit_hash IS NOT NULL AND commit_hash != ''"
        ) if node_total > 0 else 0
        nodes_with_bytes = _count_where(
            "cgdb_nodes", "byte_end > byte_start"
        ) if node_total > 0 else 0
        # Real-source type coverage — exclude external/builtin references
        # and synthesized __cond_ nodes from the denominator (they have no
        # type info by design).
        nodes_real_with_type = _count_where(
            "cgdb_nodes",
            "type_id IS NOT NULL "
            "AND attrs NOT LIKE '%\"external\": true%' "
            "AND fqn NOT LIKE '%__cond_%'"
        ) if node_total > 0 else 0
        # "Real" nodes exclude external/builtin references (attrs.external=true)
        # and synthesized conditional nodes (fqn contains '__cond_'). These
        # legitimately lack source byte ranges, so reporting byte coverage
        # against them understates true source-node coverage.
        nodes_real_total = _count_where(
            "cgdb_nodes",
            "attrs NOT LIKE '%\"external\": true%' "
            "AND fqn NOT LIKE '%__cond_%'"
        ) if node_total > 0 else 0
        nodes_real_with_bytes = _count_where(
            "cgdb_nodes",
            "byte_end > byte_start "
            "AND attrs NOT LIKE '%\"external\": true%' "
            "AND fqn NOT LIKE '%__cond_%'"
        ) if node_total > 0 else 0

        def _pct(num: int, denom: int) -> str:
            if denom <= 0:
                return "—"
            return f"{100.0 * num / denom:.1f}%"

        # Versions timeline (last 10)
        try:
            version_rows = conn.execute(
                "SELECT version_id, commit_hash, compiled_at, commit_subject "
                "FROM graph_versions ORDER BY version_id DESC LIMIT 10"
            ).fetchall()
        except sqlite3.OperationalError:
            version_rows = []

        # Build markdown
        out_path = os.path.join(args.graph, "cgdb_layer_summary.md")
        L = []
        L.append("# Code2Database Layer Summary\n")
        L.append("> Generated by `cgdb-layer-summary`. "
                 "Reports row counts and coverage for all 13 cgdb semantic "
                 "tables (L0–L11).")
        L.append("")
        L.append("## Table Row Counts\n")
        L.append("| Layer | Table | Rows |")
        L.append("|-------|-------|------|")
        for label, cnt in layer_counts.items():
            display = "—" if cnt < 0 else f"{cnt:,}"
            L.append(f"| {label} | `{label.split(' ', 1)[1]}` | {display} |")
        L.append("")

        L.append("## Coverage Metrics (cgdb_nodes)\n")
        L.append("| Metric | Count | Coverage |")
        L.append("|--------|-------|----------|")
        L.append(f"| Total nodes | {node_total:,} | 100% |")
        L.append(f"| With type_id | {nodes_with_type:,} | {_pct(nodes_with_type, node_total)} |")
        L.append(f"| With config_predicate_id | {nodes_with_pred:,} | {_pct(nodes_with_pred, node_total)} |")
        L.append(f"| With enclosing_symbol_id | {nodes_with_enclosing:,} | {_pct(nodes_with_enclosing, node_total)} |")
        L.append(f"| With source_snippet | {nodes_with_snippet:,} | {_pct(nodes_with_snippet, node_total)} |")
        L.append(f"| With description (LLM/heuristic) | {nodes_with_description:,} | {_pct(nodes_with_description, node_total)} |")
        L.append(f"| With commit_hash | {nodes_with_commit:,} | {_pct(nodes_with_commit, node_total)} |")
        L.append(f"| With byte_start < byte_end | {nodes_with_bytes:,} | {_pct(nodes_with_bytes, node_total)} |")
        L.append(f"| Real-source nodes (excl. external/cond) | {nodes_real_total:,} | {_pct(nodes_real_total, node_total)} |")
        L.append(f"| Real-source with byte range | {nodes_real_with_bytes:,} | {_pct(nodes_real_with_bytes, nodes_real_total)} |")
        L.append(f"| Real-source with type_id | {nodes_real_with_type:,} | {_pct(nodes_real_with_type, nodes_real_total)} |")
        L.append("")

        L.append("## Nodes by Kind\n")
        if nodes_by_kind:
            L.append("| Kind | Count |")
            L.append("|------|-------|")
            for kind, cnt in nodes_by_kind:
                L.append(f"| {kind} | {cnt:,} |")
        else:
            L.append("_(no rows)_")
        L.append("")

        L.append("## Edges by Kind\n")
        if edges_by_kind:
            L.append("| Kind | Count |")
            L.append("|------|-------|")
            for kind, cnt in edges_by_kind:
                L.append(f"| {kind} | {cnt:,} |")
        else:
            L.append("_(no rows)_")
        L.append("")

        L.append("## Sync Primitives by Kind\n")
        if sync_by_kind:
            L.append("| Kind | Count |")
            L.append("|------|-------|")
            for kind, cnt in sync_by_kind:
                L.append(f"| {kind} | {cnt:,} |")
        else:
            L.append("_(no rows)_")
        L.append("")

        L.append("## Time-Travel Versions (last 10)\n")
        if version_rows:
            L.append("| version_id | commit_hash | compiled_at | subject |")
            L.append("|------------|-------------|-------------|---------|")
            for vid, ch, ct, note in version_rows:
                ch_short = (ch or "")[:12]
                # compiled_at is an INTEGER unix timestamp; render as-is.
                ct_short = str(ct) if ct is not None else ""
                note_short = (note or "")[:40]
                L.append(f"| {vid} | {ch_short} | {ct_short} | {note_short} |")
        else:
            L.append("_(no versions recorded)_")
        L.append("")

        L.append("## Layer Health Notes\n")
        notes = []
        if layer_counts["L1 cgdb_nodes"] <= 0:
            notes.append("- **L1 cgdb_nodes is empty** — scan with "
                         "`--extraction-backend clang` (C/C++) or any backend "
                         "for other languages; the scanner emits cgdb_nodes "
                         "for all languages now.")
        if layer_counts["L2 cgdb_types"] <= 0:
            notes.append("- **L2 cgdb_types is empty** — type extraction "
                         "is currently limited to C/C++; enhanced type system "
                         "covering all languages is future work.")
        if layer_counts["L3 conditions"] <= 0:
            notes.append("- **L3 conditions is empty** — conditions are "
                         "extracted by ConditionExtractor (IfStmt/WhileStmt/etc.) "
                         "and stored in `cgdb_condition_index.json`; the SQLite "
                         "`conditions` table is populated when the clang backend "
                         "is used.")
        if layer_counts["L4 basic_blocks"] <= 0:
            notes.append("- **L4 basic_blocks/cfg_edges are empty** — the "
                         "tree-sitter backend emits a simplified CFG when "
                         "function body_text is available; if this is 0, "
                         "the scanner may have failed to capture function "
                         "bodies or the language has no control-flow "
                         "statements detected.")
        if layer_counts["L5 data_flow"] <= 0:
            notes.append("- **L5 data_flow is empty** — needs scanner-side "
                         "def-use extraction. All language scanners emit some "
                         "data_flow records; if this is 0, the build may not "
                         "have invoked the cgdb ingest step.")
        if layer_counts["L7 sync_primitives"] <= 0:
            notes.append("- **L7 sync_primitives is empty** — needs scanner-side "
                         "lock/mutex/atomic extraction. All language scanners "
                         "look for sync primitives; if this is 0, the source "
                         "may not contain explicit lock calls.")
        if layer_counts["L8 ops_bindings"] <= 0:
            notes.append("- **L8 ops_bindings is empty** — needs C/C++ scanner "
                         "to detect struct ops_table initializers "
                         "(file_operations.read_iter, etc.). Other languages "
                         "do not currently emit ops_bindings.")
        if node_total > 0 and nodes_with_snippet == 0:
            notes.append("- **source_snippet is empty for all nodes** — "
                         "scan-time snippet capture is not being populated; "
                         "`cgdb-get-source` falls back to reading source files "
                         "via byte_start/byte_end offsets, which works "
                         "correctly when byte coverage is high.")
        if node_total > 0 and nodes_with_description == 0:
            notes.append("- **description column is empty for all nodes** — "
                         "run `heuristic-enhance --all` or `auto-enhance` to "
                         "populate rule-based descriptions. Heuristic "
                         "supplements are also propagated from JSON-side "
                         "supplement store to `cgdb_nodes.description`.")
        if not notes:
            notes.append("- All layers look healthy.")
        L.extend(notes)
        L.append("")

        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(L) + "\n")
        print(out_path)
    finally:
        store.close()


def cmd_cgdb_coverage(args):
    """RPT-KERNEL-D15: Query graph coverage.

    Three modes:
      - `--function NAME`: Is function NAME in the graph? Returns matching
        cgdb_nodes rows (name, fqn, file_path, line).
      - `--file PATH`: Was file PATH scanned? Returns the cgdb_files row
        plus functions defined in that file.
      - neither: Returns subsystem-level summary (scanned/missing
        subsystems, paths to coverage reports).
    """
    from _builder.coverage_report import query_coverage
    function_name = getattr(args, "function", None)
    file_path = getattr(args, "file", None)
    result = query_coverage(args.graph,
                            function_name=function_name,
                            file_path=file_path)
    _print_json(result)
    if result.get("status") == "error":
        sys.exit(1)


def cmd_cgdb_write_coverage(args):
    """RPT-KERNEL-D15: Manually (re)write the coverage reports.

    Useful when the build was run before RPT-KERNEL-D14/D15 was integrated,
    or after a manual DB modification (e.g., tx-commit) that added/removed
    files. Writes both `.code2database_coverage_report.json` (subsystem
    summary) and `.code2database_file_coverage.json` (file-level list).
    """
    from _builder.coverage_report import (
        write_coverage_report, write_file_coverage,
    )
    cov = write_coverage_report(args.graph)
    fcov = write_file_coverage(args.graph)
    result = {
        "status": "ok",
        "coverage_report_path": cov,
        "file_coverage_path": fcov,
    }
    _print_json(result)
    if cov is None and fcov is None:
        sys.exit(1)
