#!/usr/bin/env python3
"""Query router for Code2Database.

Routes query commands to SQLite SQL when code2database.db exists, falling back to
NetworkX full-graph load only when SQLite is unavailable or the query needs
graph-traversal semantics not yet SQL-native.

Design goals:
- Make SQLite the default query path for hot queries (field-access, invokers,
  invoked, function-by-name, domain listing, thread-processors).
- Preserve exact output parity with the NetworkX path — same dict shape, same
  ordering — so callers don't notice the routing change.
- Provide a single `route_query` entry point so future query commands can
  opt into SQL without rewriting their control flow.
- Never break existing behavior: if SQLite query fails for any reason, fall
  back to NetworkX transparently and log a warning.

Usage:
    from _builder.query_router import route_field_access, route_invokers
    # Try SQL first; if no DB, returns None and caller falls back to NetworkX.
    rows = route_field_access(graph_dir, field_name="pid", struct_name="task_struct")
    if rows is not None:
        # Use rows directly — no G.nodes(data=True) loop needed.
        ...
    else:
        # Fall back to existing NetworkX traversal.
        G = _load_full_graph(graph_dir)
        for nid, ndata in G.nodes(data=True):
            ...
"""

import json
import os
import sys
from typing import Optional, List, Dict, Any


def _db_path_for(graph_dir: str) -> str:
    return os.path.join(graph_dir, "code2database.db")


def sqlite_available(graph_dir: str) -> bool:
    """Return True if code2database.db exists and is non-empty."""
    db_path = _db_path_for(graph_dir)
    return os.path.exists(db_path) and os.path.getsize(db_path) > 0


def _open_store(graph_dir: str):
    """Open SQLiteStore for the given graph_dir. Returns (store, db_path) or (None, None)."""
    if not sqlite_available(graph_dir):
        return None, None
    try:
        try:
            from _builder.sqlite_store import SQLiteStore
        except ImportError:
            sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
            from _builder.sqlite_store import SQLiteStore
        store = SQLiteStore(_db_path_for(graph_dir))
        store.connect()
        return store, _db_path_for(graph_dir)
    except Exception as exc:
        print(f"[query_router] SQLite open failed, will fall back to NetworkX: {exc}",
              file=sys.stderr)
        return None, None


def route_field_access(graph_dir: str, field_name: str,
                       struct_name: str = "", limit: int = 200,
                       assigned_value: str = "") -> Optional[List[Dict]]:
    """SQL-native field-access query. Returns None if SQLite unavailable.

    Output shape matches cmd_field_access's reader/writer entries exactly:
    {function, domain, source_file, line, access_type, struct_chain,
     field_name, thread_model, [target_func], [is_param], [assigned_value]}

    When assigned_value is set (e.g., 'NULL'), only writes whose assigned_value
    matches are returned (reads are excluded since they have no assigned value).
    """
    store, _ = _open_store(graph_dir)
    if store is None:
        return None
    try:
        if assigned_value:
            # Only query writers with the assigned-value filter
            writers = store.query_field_access(field_name, struct_name, "write",
                                               assigned_value=assigned_value,
                                               limit=limit)
            return writers
        readers = store.query_field_access(field_name, struct_name, "read",
                                           limit=limit)
        writers = store.query_field_access(field_name, struct_name, "write",
                                           limit=limit)
        # Also query global access (cmd_field_access matches globals by name when --field given)
        g_readers = store.query_global_access(field_name, "read", limit=limit)
        g_writers = store.query_global_access(field_name, "write", limit=limit)
        return writers + readers + g_writers + g_readers
    except Exception as exc:
        print(f"[query_router] field-access SQL failed, falling back: {exc}",
              file=sys.stderr)
        return None
    finally:
        store.close()


def route_invokers(graph_dir: str, invoked_id: str, limit: int = 200) -> Optional[List[Dict]]:
    """SQL-native invokers query. Returns None if SQLite unavailable."""
    store, _ = _open_store(graph_dir)
    if store is None:
        return None
    try:
        return store.query_invokers_sql(invoked_id, limit)
    except Exception as exc:
        print(f"[query_router] invokers SQL failed, falling back: {exc}", file=sys.stderr)
        return None
    finally:
        store.close()


def route_invoked(graph_dir: str, invoker_id: str, limit: int = 500) -> Optional[List[Dict]]:
    """SQL-native invoked query. Returns None if SQLite unavailable."""
    store, _ = _open_store(graph_dir)
    if store is None:
        return None
    try:
        return store.query_invoked_sql(invoker_id, limit)
    except Exception as exc:
        print(f"[query_router] invoked SQL failed, falling back: {exc}", file=sys.stderr)
        return None
    finally:
        store.close()


# Backwards-compatible aliases (deprecated — use route_invokers/route_invoked).
route_invokers = route_invokers
route_invoked = route_invoked


def route_function_by_id(graph_dir: str, func_id: str) -> Optional[Dict]:
    """SQL-native single-function lookup. Returns None if not found OR no SQLite.

    Note: returns None both when SQLite is unavailable and when the function
    isn't found. Caller should distinguish by checking sqlite_available() first
    if it matters.
    """
    store, _ = _open_store(graph_dir)
    if store is None:
        return None
    try:
        return store.query_function_by_id_sql(func_id)
    except Exception as exc:
        print(f"[query_router] function-by-id SQL failed: {exc}", file=sys.stderr)
        return None
    finally:
        store.close()


def route_functions_by_name(graph_dir: str, name_pattern: str,
                            limit: int = 50) -> Optional[List[Dict]]:
    """SQL-native name search. Returns None if SQLite unavailable."""
    store, _ = _open_store(graph_dir)
    if store is None:
        return None
    try:
        return store.query_functions_by_name_sql(name_pattern, limit)
    except Exception as exc:
        print(f"[query_router] functions-by-name SQL failed: {exc}", file=sys.stderr)
        return None
    finally:
        store.close()


def route_thread_processors(graph_dir: str, limit: int = 200) -> Optional[List[Dict]]:
    """SQL-native thread_processor query. Returns None if SQLite unavailable."""
    store, _ = _open_store(graph_dir)
    if store is None:
        return None
    try:
        return store.query_thread_processors_sql(limit)
    except Exception as exc:
        print(f"[query_router] thread-processors SQL failed: {exc}", file=sys.stderr)
        return None
    finally:
        store.close()


def route_change_log_by_node(graph_dir: str, node_id: str,
                             limit: int = 50) -> Optional[List[Dict]]:
    """Deficiency 2: SQL-native commit history for a node. Returns None if no SQLite."""
    store, _ = _open_store(graph_dir)
    if store is None:
        return None
    try:
        return store.query_change_log_by_node(node_id, limit)
    except Exception as exc:
        print(f"[query_router] change-log SQL failed: {exc}", file=sys.stderr)
        return None
    finally:
        store.close()


# ---------------------------------------------------------------------------
# CTE-based recursive path queries (D7+D8)
# ---------------------------------------------------------------------------

def route_call_chain(graph_dir: str, start_id: str, max_depth: int = 5,
                    direction: str = "down") -> Optional[List[Dict]]:
    """SQL-native recursive call-chain query using CTE.

    Returns a list of {depth, function_id, function_name, source_file, line}
    entries representing the call chain from start_id (down=callees,
    up=callers), up to max_depth hops. Returns None if SQLite unavailable.

    Uses a SQLite recursive CTE:
        WITH RECURSIVE chain(depth, node_id) AS (
            SELECT 0, ?
            UNION
            SELECT c.depth + 1, e.invoker_id  -- or e.invoked_id for 'down'
            FROM chain c
            JOIN edges e ON e.invoked_id = c.node_id  -- or e.invoker_id for 'down'
            WHERE c.depth < ?
        )
        SELECT c.depth, c.node_id, f.name, f.source_file, f.line_number
        FROM chain c JOIN functions f ON f.id = c.node_id
        ORDER BY c.depth;
    """
    store, _ = _open_store(graph_dir)
    if store is None:
        return None
    try:
        return store.query_call_chain_cte(start_id, max_depth, direction)
    except Exception as exc:
        print(f"[query_router] call-chain CTE failed, falling back: {exc}",
              file=sys.stderr)
        return None
    finally:
        store.close()


def route_blast_radius(graph_dir: str, start_id: str,
                      max_depth: int = 10) -> Optional[List[Dict]]:
    """SQL-native blast-radius (downward call chain) query.

    Returns list of {depth, function_id, function_name} for all functions
    reachable from start_id via INVOKES edges, up to max_depth.
    Returns None if SQLite unavailable.
    """
    return route_call_chain(graph_dir, start_id, max_depth, direction="down")


def route_trace_chain(graph_dir: str, end_id: str,
                     max_depth: int = 10) -> Optional[List[Dict]]:
    """SQL-native trace-chain (upward caller chain) query.

    Returns list of {depth, function_id, function_name} for all functions
    that reach end_id via INVOKES edges, up to max_depth.
    Returns None if SQLite unavailable.
    """
    return route_call_chain(graph_dir, end_id, max_depth, direction="up")


def route_path_between(graph_dir: str, from_id: str, to_id: str,
                       max_depth: int = 10) -> Optional[List[Dict]]:
    """SQL-native path-between-two-functions query using CTE.

    Returns list of paths (each path is a list of function_ids from from_id
    to to_id) found via INVOKES edges, up to max_depth. Returns None if
    SQLite unavailable.

    Uses a recursive CTE that accumulates the path as a JSON array.
    """
    store, _ = _open_store(graph_dir)
    if store is None:
        return None
    try:
        return store.query_path_between_cte(from_id, to_id, max_depth)
    except Exception as exc:
        print(f"[query_router] path-between CTE failed: {exc}", file=sys.stderr)
        return None
    finally:
        store.close()
