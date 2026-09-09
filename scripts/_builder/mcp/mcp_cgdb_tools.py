"""mcp_server.mcp_cgdb_tools — split from mcp_server.py.

19 cgdb_* MCP tool handler functions.
"""

import os
import logging
from _builder.mcp.mcp_cache import _cgdb_store, _mcp_coerce_int, _mcp_coerce_str


def _tool_cgdb_search_symbols(args: dict, graph_dir: str) -> list:
    """Full-text search over cgdb_nodes via FTS5."""
    query = _mcp_coerce_str(args.get("query", ""))
    kind = args.get("kind")
    # Audit issue 25 (MEDIUM): bare int() crashed on non-numeric input.
    # Audit issue 27 (MEDIUM): no upper cap on limit; clients could
    # pass top=999999999 and get back a huge response.
    limit = _mcp_coerce_int(args.get("limit", 50), 50, 1, 200)
    if not query:
        return []
    store = _cgdb_store(graph_dir)
    if store is None:
        return [{"error": "cgdb tables not available — run scan with --extraction-backend auto"}]
    return store.search_symbols(query, kind=kind, limit=limit)


def _tool_cgdb_get_definition(args: dict, graph_dir: str) -> list:
    """Find definition nodes by name (function/var/field/typedef)."""
    name = args.get("name", "")
    if not name:
        return []
    store = _cgdb_store(graph_dir)
    if store is None:
        return [{"error": "cgdb tables not available"}]
    return store.get_definition(name, limit=_mcp_coerce_int(args.get("limit", 10), 10, 1, 100))


def _tool_cgdb_get_function_body(args: dict, graph_dir: str) -> dict:
    """Return the function body source text for a function (name or id)."""
    node = args.get("node", "")
    if not node:
        return {"error": "node parameter required"}
    store = _cgdb_store(graph_dir)
    if store is None:
        return {"error": "cgdb tables not available"}
    result = store.get_function_body(node)
    return result or {"error": f"function {node!r} not found"}



def _tool_cgdb_get_source(args: dict, graph_dir: str) -> dict:
    """Return the source text for a node, with byte-precise attribution.

    Resolution order: source_snippet column → file read via byte_start..byte_end.
    Optional context_bytes adds surrounding bytes; snippet_only skips file read.
    """
    node = args.get("node", "")
    if not node:
        return {"error": "node parameter required"}
    store = _cgdb_store(graph_dir)
    if store is None:
        return {"error": "cgdb tables not available"}
    conn = store._ensure_conn()
    # Resolve node_id from name or numeric
    if isinstance(node, int) or (isinstance(node, str) and node.isdigit()):
        node_id = int(node)
    else:
        rows = store.search_symbols(node, limit=1)
        if not rows:
            return {"error": f"node '{node}' not found"}
        node_id = rows[0]["id"]
    row = conn.execute(
        "SELECT n.id, n.kind, n.name, n.fqn, n.line, n.col, "
        "n.byte_start, n.byte_end, n.source_snippet, "
        "f.path, f.content_hash "
        "FROM cgdb_nodes n LEFT JOIN cgdb_files f ON n.file_id = f.id "
        "WHERE n.id = ?",
        (node_id,)
    ).fetchone()
    if row is None:
        return {"error": f"node_id {node_id} not in cgdb_nodes"}
    (nid, kind, name, fqn, line, col, byte_start, byte_end,
     source_snippet, file_path, content_hash) = row
    snippet_only = bool(args.get("snippet_only", False))
    # Audit issue 25/27: cap context_bytes at a sane upper bound.
    context_bytes = _mcp_coerce_int(args.get("context_bytes", 0), 0, 0, 1_000_000)
    result = {
        "node_id": nid, "kind": kind, "name": name, "fqn": fqn,
        "line": line, "col": col,
        "byte_start": byte_start or 0, "byte_end": byte_end or 0,
        "file_path": file_path, "content_hash": content_hash,
    }
    snippet = source_snippet or ""
    if snippet and not context_bytes and not snippet_only:
        result["source_text"] = snippet
        result["source"] = "source_snippet"
        return result
    if snippet_only:
        result["source_text"] = snippet
        result["source"] = "source_snippet" if snippet else "empty"
        return result
    if not file_path:
        result["source_text"] = snippet
        result["source"] = "source_snippet_no_file"
        return result
    # Resolve relative paths via source_root (cgdb_files.path may be relative).
    resolved_path = _resolve_source_file(file_path, graph_dir)
    if not resolved_path:
        result["source_text"] = snippet
        result["source"] = "file_resolution_failed"
        return result
    try:
        # Cap file size to prevent unbounded memory use (audit issue 18):
        # a malicious or pathological source file (e.g. a generated .c
        # shipped with a toolchain) could otherwise OOM the MCP server.
        # 8 MB is enough for any hand-written TU (the Linux kernel's
        # largest .c is ~700 KB).
        _MAX_SOURCE_BYTES = 8 * 1024 * 1024
        with open(resolved_path, "rb") as fh:
            raw = fh.read(_MAX_SOURCE_BYTES + 1)
        if len(raw) > _MAX_SOURCE_BYTES:
            result["source_text"] = snippet
            result["source"] = ("file_too_large: %d bytes > %d limit"
                                % (len(raw), _MAX_SOURCE_BYTES))
            return result
    except OSError as exc:
        result["source_text"] = snippet
        result["source"] = f"file_read_failed: {exc}"
        return result
    bs = byte_start or 0
    be = byte_end or 0
    if context_bytes > 0:
        lo = max(0, bs - context_bytes)
        hi = min(len(raw), be + context_bytes)
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
    if snippet and not context_bytes:
        result["source_snippet"] = snippet
    return result



def _tool_cgdb_find_invokers(args: dict, graph_dir: str) -> list:
    """Find callers of a node (recursive CTE with cycle protection).

    When include_vtable_dispatch=true, also follows indirect dispatch
    via ops_bindings + invoke_sites tables — finds vtable callers even
    when no pre-computed INVOKES edge exists at scan time.
    """
    node_id = args.get("node_id")
    if node_id is None:
        return [{"error": "node_id required"}]
    store = _cgdb_store(graph_dir)
    if store is None:
        return [{"error": "cgdb tables not available"}]
    # Audit issue 25/27: bare int() + no upper cap on depth/limit.
    # depth=1000000 would cause unbounded recursive CTE; cap at 20.
    # limit=999999999 would return a massive response; cap at 1000.
    depth = _mcp_coerce_int(args.get("depth", 1), 1, 1, 20)
    edge_types = args.get("edge_types", ["INVOKES"])
    limit = _mcp_coerce_int(args.get("limit", 200), 200, 1, 1000)
    include_vtable_dispatch = bool(args.get("include_vtable_dispatch", False))
    return store.find_invokers(_mcp_coerce_int(node_id, 0, 0, 2**63-1), depth=depth,
                               edge_types=edge_types, limit=limit,
                               include_vtable_dispatch=include_vtable_dispatch)



def _tool_cgdb_find_invoked(args: dict, graph_dir: str) -> list:
    """Find callees of a node (recursive CTE).

    When include_vtable_dispatch=true, also resolves vtable dispatch
    via ops_bindings — finds impl functions that may be invoked via
    function pointer calls.
    """
    node_id = args.get("node_id")
    if node_id is None:
        return [{"error": "node_id required"}]
    store = _cgdb_store(graph_dir)
    if store is None:
        return [{"error": "cgdb tables not available"}]
    depth = _mcp_coerce_int(args.get("depth", 1), 1, 1, 20)
    edge_types = args.get("edge_types", ["INVOKES"])
    limit = _mcp_coerce_int(args.get("limit", 500), 500, 1, 1000)
    include_vtable_dispatch = bool(args.get("include_vtable_dispatch", False))
    return store.find_invoked(_mcp_coerce_int(node_id, 0, 0, 2**63-1), depth=depth,
                              edge_types=edge_types, limit=limit,
                              include_vtable_dispatch=include_vtable_dispatch)



def _tool_cgdb_get_struct_layout(args: dict, graph_dir: str) -> dict:
    """Return a struct/union's field layout."""
    name = args.get("name") or args.get("struct")
    if not name:
        return {"error": "name parameter required"}
    store = _cgdb_store(graph_dir)
    if store is None:
        return {"error": "cgdb tables not available"}
    result = store.get_struct_layout(name)
    return result or {"error": f"struct {name!r} not found"}



def _tool_cgdb_find_type_definition(args: dict, graph_dir: str) -> list:
    """Find type definitions (struct/union/enum/typedef) by name."""
    name = args.get("name", "")
    if not name:
        return []
    store = _cgdb_store(graph_dir)
    if store is None:
        return [{"error": "cgdb tables not available"}]
    return store.find_type_definition(name, limit=_mcp_coerce_int(args.get("limit", 10), 10, 1, 100))


def _tool_cgdb_find_ops_impls(args: dict, graph_dir: str) -> list:
    """Find functions bound to a vtable field (e.g., file_operations.read_iter)."""
    field_name = args.get("field_name", "")
    if not field_name:
        return [{"error": "field_name required"}]
    store = _cgdb_store(graph_dir)
    if store is None:
        return [{"error": "cgdb tables not available"}]
    struct_type = args.get("struct_type")
    return store.find_ops_impls(field_name, struct_type=struct_type)



def _tool_cgdb_find_cfg_paths(args: dict, graph_dir: str) -> list:
    """Find CFG paths from entry to exit in a function."""
    func_id = args.get("function_id")
    if func_id is None:
        return [{"error": "function_id required"}]
    store = _cgdb_store(graph_dir)
    if store is None:
        return [{"error": "cgdb tables not available"}]
    return store.find_cfg_paths(_mcp_coerce_int(func_id, 0, 0, 2**63-1),
                                max_len=_mcp_coerce_int(args.get("max_len", 10), 10, 1, 100))


def _tool_cgdb_find_data_flow(args: dict, graph_dir: str) -> dict:
    """Find def-use chain entries for a variable."""
    var_id = args.get("var_id")
    if var_id is None:
        return {"error": "var_id required"}
    store = _cgdb_store(graph_dir)
    if store is None:
        return {"error": "cgdb tables not available"}
    return store.find_data_flow(int(var_id))


def _tool_cgdb_find_aliases(args: dict, graph_dir: str) -> list:
    """Find aliases of a pointer (may_alias / must_alias / no_alias)."""
    ptr_id = args.get("ptr_id")
    if ptr_id is None:
        return [{"error": "ptr_id required"}]
    store = _cgdb_store(graph_dir)
    if store is None:
        return [{"error": "cgdb tables not available"}]
    return store.find_aliases(int(ptr_id))


def _tool_cgdb_find_lock_held_calls(args: dict, graph_dir: str) -> list:
    """Find calls made while a lock is held in a function."""
    func_id = args.get("function_id")
    if func_id is None:
        return [{"error": "function_id required"}]
    store = _cgdb_store(graph_dir)
    if store is None:
        return [{"error": "cgdb tables not available"}]
    return store.find_lock_held_calls(int(func_id))


def _tool_cgdb_check_race_condition(args: dict, graph_dir: str) -> list:
    """Heuristic race-condition check for a function."""
    func_id = args.get("function_id")
    if func_id is None:
        return [{"error": "function_id required"}]
    store = _cgdb_store(graph_dir)
    if store is None:
        return [{"error": "cgdb tables not available"}]
    return store.check_race_condition(int(func_id))


def _tool_cgdb_find_configs_for(args: dict, graph_dir: str) -> list:
    """Return the config predicate text_form for the given node."""
    node_id = args.get("node_id")
    if node_id is None:
        return [{"error": "node_id required"}]
    store = _cgdb_store(graph_dir)
    if store is None:
        return [{"error": "cgdb tables not available"}]
    return store.find_configs_for(int(node_id))


def _tool_cgdb_find_nodes_under_config(args: dict, graph_dir: str) -> list:
    """Find nodes whose config_predicate matches the given predicate text."""
    config = args.get("config", "")
    if not config:
        return [{"error": "config required"}]
    store = _cgdb_store(graph_dir)
    if store is None:
        return [{"error": "cgdb tables not available"}]
    return store.find_nodes_under_config(_mcp_coerce_str(config),
                                         limit=_mcp_coerce_int(args.get("limit", 500), 500, 1, 2000))


def _tool_cgdb_index_status(args: dict, graph_dir: str) -> dict:
    """Return overall cgdb index statistics."""
    store = _cgdb_store(graph_dir)
    if store is None:
        return {"error": "cgdb tables not available"}
    return store.index_status()


def _tool_cgdb_time_travel_query(args: dict, graph_dir: str) -> dict:
    """Return the state of a node at a specific version_id."""
    node_id = args.get("node_id")
    version_id = args.get("version_id")
    if node_id is None or version_id is None:
        return {"error": "node_id and version_id required"}
    store = _cgdb_store(graph_dir)
    if store is None:
        return {"error": "cgdb tables not available"}
    result = store.time_travel_query_node(int(node_id), int(version_id))
    return result or {"error": f"node {node_id} not alive at version {version_id}"}



def _tool_cgdb_list_versions(args: dict, graph_dir: str) -> list:
    """List recent graph_versions rows (newest first)."""
    store = _cgdb_store(graph_dir)
    if store is None:
        return [{"error": "cgdb tables not available"}]
    return store.list_versions(limit=_mcp_coerce_int(args.get("limit", 50), 50, 1, 1000))

