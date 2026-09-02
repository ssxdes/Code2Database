"""MCP server mode for Code2Database.

Exposes core query commands as MCP tools over stdio transport,
enabling LLM agents to query the invocation graph in real-time without
CLI subprocess calls.

Usage:
    python code2database_builder.py serve --graph code2db-out/

MCP Tools exposed (81 total):
    - 34 code2database_* tools (load, search, describe, explore, trace,
      impact, key_paths, concurrency, data_lifecycle, domain, knowledge_query,
      memory_search, semantic_status, blast_radius, field_access, etc.)
    - 19 cgdb_* tools (query, time_travel, configs_for, ops_impls, cfg_paths,
      data_flow, race_check, index_status, sql, views, schema_version,
      versions, find_invokers, find_invoked, path, definition, function_body,
      struct_layout, type_definition, nodes_under_config, path_feasible,
      get_source, layer_summary)
    - 28 design-report tools (render_source, verify_consistency, edit_token,
      insert_token, delete_token, find_macros, get_pp_branches,
      get_string_literals, commit/rollback_db_transaction, insert_node_after,
      delete_node, add_function, find_symbol, callers_of, callees_of,
      who_writes, who_reads, get_context, impact_analysis, get_module_view,
      indirect_targets, alias_set, trace_data_flow, cfg_of,
      path_sensitive_states, precise_write_set, dead_code_in)
"""

import json
import sys
import os
import atexit
from pathlib import Path

from _builder.token_budget import estimate_tokens
import logging


# Module-level graph cache: avoid re-opening SQLite / re-loading graph for
# every MCP tool call. Keyed by graph_dir. LazySQLiteGraph holds an open
# SQLite connection, so reusing it eliminates per-call connection setup
# (which can be 1-2s on large graphs).
_GRAPH_CACHE = {}


def _get_graph(graph_dir: str):
    """Get a cached graph instance, or load and cache a new one.

    For large SQLite-backed graphs, returns a LazySQLiteGraph with a
    persistent SQLite connection. For small graphs, loads eagerly into
    NetworkX (also cached).
    """
    if graph_dir in _GRAPH_CACHE:
        return _GRAPH_CACHE[graph_dir]
    from _builder.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)
    _GRAPH_CACHE[graph_dir] = G
    return G


# Re-export the shared path resolver so existing callers in this module
# continue to work. The canonical home is _builder.utils to avoid each
# module re-implementing the source_root lookup logic.
from _builder.utils import resolve_source_file as _resolve_source_file


def _close_cached_graphs():
    """Close any cached graph connections on exit."""
    for graph_dir, G in list(_GRAPH_CACHE.items()):
        try:
            close = getattr(G, "close", None)
            if callable(close):
                close()
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    _GRAPH_CACHE.clear()


atexit.register(_close_cached_graphs)


# ---------------------------------------------------------------------------
# MCP stdio transport
# ---------------------------------------------------------------------------

# Sentinel for EOF detection — distinct from None (which means "parse error,
# skip and continue"). Using a sentinel object lets the main loop distinguish
# "stdin closed, should exit" from "malformed JSON, skip this message".
_EOF_SENTINEL = object()


def _read_message():
    """Read a JSON-RPC message from stdin (MCP stdio transport).

    Returns:
        dict: parsed JSON-RPC message.
        _EOF_SENTINEL: stdin reached EOF (client disconnected).
        None: malformed/unparseable message (skip, continue loop).

    Implements Content-Length header parsing per MCP spec:
    https://spec.modelcontextprotocol.io/specification/basic/transports/#stdio

    Fallback: when no Content-Length header is present (simple line-based
    clients), the first non-empty line is treated as the JSON body. This
    handles both framed and unframed inputs.
    """
    # Read headers until empty line. A line that doesn't look like a header
    # (no "key: value" pattern) is treated as the body for the fallback path.
    content_length = None
    fallback_body = None
    while True:
        line = sys.stdin.readline()
        if not line:
            return _EOF_SENTINEL  # EOF — client disconnected
        line = line.strip()
        if not line:
            break  # End of headers
        if line.lower().startswith("content-length:"):
            try:
                content_length = int(line.split(":", 1)[1].strip())
            except ValueError:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        elif ":" in line and not line.startswith("{"):
            # Looks like another header (e.g., "Content-Type: ...") — ignore.
            continue
        else:
            # Doesn't look like a header — treat as the body for fallback.
            fallback_body = line
            break

    if content_length is not None:
        # Read exactly content_length bytes
        data = sys.stdin.read(content_length)
        if not data:
            return _EOF_SENTINEL  # EOF
    elif fallback_body is not None:
        # Fallback: the line we already read IS the JSON body.
        data = fallback_body
    else:
        # Fallback: try reading a single JSON line (for simple clients).
        line = sys.stdin.readline()
        if not line:
            return _EOF_SENTINEL  # EOF
        data = line.strip()

    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def _write_message(msg: dict):
    """Write a JSON-RPC message to stdout (MCP stdio transport).

    Includes Content-Length header per MCP spec.
    """
    body = json.dumps(msg, ensure_ascii=False)
    header = f"Content-Length: {len(body.encode('utf-8'))}\r\n\r\n"
    sys.stdout.write(header + body)
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _tool_load(args: dict, graph_dir: str) -> dict:
    """Load and summarize the invocation graph.

    For large SQLite-backed graphs, uses SQL counts instead of iterating
    all nodes in Python. This avoids O(N) iteration over 1.5M+ nodes.
    """
    import os
    db_path = os.path.join(graph_dir, "code2database.db")
    if os.path.exists(db_path) and os.path.getsize(db_path) > 0:
        import sqlite3
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            nodes = cur.execute("SELECT COUNT(*) FROM functions").fetchone()[0]
            edges = cur.execute(
                "SELECT COUNT(*) FROM edges WHERE relation NOT IN ('CONTAINS','IMPORTS')"
            ).fetchone()[0]
            # Use indexed boolean columns when available, LIKE fallback otherwise
            _has_lc = "is_api_entry" in {r[1] for r in cur.execute("PRAGMA table_info(functions)").fetchall()}
            if _has_lc:
                api_count = cur.execute(
                    "SELECT COUNT(*) FROM functions WHERE is_api_entry = 1"
                ).fetchone()[0]
                thread_count = cur.execute(
                    "SELECT COUNT(*) FROM functions WHERE is_thread_processor = 1"
                ).fetchone()[0]
            else:
                api_count = cur.execute(
                    "SELECT COUNT(*) FROM functions WHERE labels LIKE '%API_entry%'"
                ).fetchone()[0]
                thread_count = cur.execute(
                    "SELECT COUNT(*) FROM functions WHERE labels LIKE '%thread_processor%'"
                ).fetchone()[0]
            domains = cur.execute(
                "SELECT COUNT(DISTINCT domain) FROM functions WHERE domain != ''"
            ).fetchone()[0]
            return {
                "nodes": nodes,
                "edges": edges,
                "api_entries": api_count,
                "thread_entries": thread_count,
                "domains": domains,
                "_source": "sqlite",
            }
        except sqlite3.Error:
            # Graceful degradation to the NetworkX path is intentional, but
            # log at WARNING so users see why SQLite queries fail instead
            # of silently falling back.
            logging.getLogger(__name__).warning(
                "mcp overview: sqlite backend failed, falling back to "
                "NetworkX load", exc_info=True)
        finally:
            if conn is not None:
                conn.close()
    from _builder.graph_build import _load_full_graph
    G = _get_graph(graph_dir)
    api_count = sum(1 for _, d in G.nodes(data=True) if "API_entry" in d.get("labels", []))
    thread_count = sum(1 for _, d in G.nodes(data=True) if "thread_processor" in d.get("labels", []))
    return {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "api_entries": api_count,
        "thread_entries": thread_count,
        "domains": len(set(d.get("domain", "") for _, d in G.nodes(data=True))),
    }


def _tool_search(args: dict, graph_dir: str) -> list:
    """Search nodes by keywords.

    For large SQLite-backed graphs, uses SQL LIKE on functions table instead
    of iterating all nodes in Python. This is O(N) but with SQLite's indexing
    it's much faster than Python iteration on 1.5M+ node graphs.
    """
    import os
    keywords = args.get("keywords", "")
    top = args.get("top", 20)
    if not keywords:
        return []
    db_path = os.path.join(graph_dir, "code2database.db")
    if os.path.exists(db_path) and os.path.getsize(db_path) > 0:
        import sqlite3
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            tokens = [t.strip() for t in keywords.replace(",", " ").split() if t.strip()]
            if not tokens:
                return []
            # Build WHERE clause: each token must match name OR signature
            # (using LIKE for case-insensitive substring match)
            where_parts = []
            params = []
            for tok in tokens:
                where_parts.append("(name LIKE ? OR extra_json LIKE ?)")
                params.extend([f"%{tok}%", f"%{tok}%"])
            where_clause = " AND ".join(where_parts)
            sql = (f"SELECT id, name, domain, labels FROM functions "
                   f"WHERE {where_clause} LIMIT ?")
            params.append(top * 5)  # fetch more, then score in Python
            cur.execute(sql, params)
            rows = cur.fetchall()
            # Score in Python: prefer name matches over extra_json matches
            results = []
            for nid, name, domain, labels in rows:
                name_lower = (name or "").lower()
                # Score = number of tokens found in name (higher = better)
                score = sum(1 for t in tokens if t.lower() in name_lower)
                if score == 0:
                    score = 0.3  # matched only in extra_json
                results.append({
                    "id": nid, "name": name, "score": float(score),
                    "domain": domain or "",
                    "labels": (labels.split(",") if labels else []),
                })
            results.sort(key=lambda x: -x["score"])
            return results[:top]
        except sqlite3.Error:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
        finally:
            if conn is not None:
                conn.close()
    from _builder.graph_build import _load_full_graph
    from _builder.utils import _simple_tokenize, _similarity_score, _find_node_id
    G = _get_graph(graph_dir)
    if not G:
        return []
    tokens = _simple_tokenize(keywords)
    if not tokens:
        return []
    results = []
    for nid, nd in G.nodes(data=True):
        if nd.get("is_empty", False):
            continue
        name = nd.get("name", "")
        sig = nd.get("signature", "")
        desc = nd.get("semantic_desc", "")
        text = f"{name} {sig} {desc}"
        score = _similarity_score(tokens, _simple_tokenize(text))
        if score > 0:
            results.append({"id": nid, "name": name, "score": round(score, 3),
                           "domain": nd.get("domain", ""),
                           "labels": nd.get("labels", [])})
    results.sort(key=lambda x: -x["score"])
    return results[:top]


def _tool_describe(args: dict, graph_dir: str) -> dict:
    """Describe a node."""
    from _builder.utils import _find_node_id
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    node_id = _find_node_id(G, args.get("node", ""))
    if not node_id:
        return {"error": f"Node not found: {args.get('node', '')}"}
    nd = G.nodes[node_id]
    detail = args.get("detail", "brief")
    # Build output based on detail level
    result = {"id": node_id, "name": nd.get("name", ""),
              "signature": nd.get("signature", ""), "labels": nd.get("labels", []),
              "domain": nd.get("domain", "")}
    if detail in ("standard", "full"):
        result["params"] = nd.get("params", [])
        result["condition_vars"] = nd.get("condition_vars", [])
        result["concurrency_info"] = [ca.get("concurrency_info", {})
                                       for ca in nd.get("callee_args", [])
                                       if ca.get("concurrency_info", {}).get("is_spawn")]
    if detail == "full":
        result["local_vars"] = nd.get("local_vars", [])
        result["callee_args"] = nd.get("callee_args", [])
        result["body_text"] = nd.get("body_text", "")
    # Callers/callees (call edges only, exclude CONTAINS/IMPORTS)
    callers = [c for c in G.predecessors(node_id)
               if (G.get_edge_data(c, node_id) or {}).get("relation") not in ("CONTAINS", "IMPORTS")][:20]
    callees = [c for c in G.successors(node_id)
               if (G.get_edge_data(node_id, c) or {}).get("relation") not in ("CONTAINS", "IMPORTS")][:20]
    result["callers"] = [{"id": c, "name": G.nodes[c].get("name", "")} for c in callers]
    result["callees"] = [{"id": c, "name": G.nodes[c].get("name", "")} for c in callees]
    return result


def _tool_explore(args: dict, graph_dir: str) -> dict:
    """One-shot context retrieval by query."""
    from _builder.graph_build import _load_full_graph
    from _builder.explore import _tokenize_query, _find_relevant_nodes, \
        _score_node_relevance, _extract_subgraph_context, _extract_key_paths, \
        _generate_exploration_summary, _derive_exec_summary
    from _builder.utils import _find_node_id
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    query = args.get("query", "")
    max_nodes = args.get("max_nodes", 15)
    max_tokens = args.get("max_tokens", 2000)
    query_tokens = _tokenize_query(query)
    if not query_tokens:
        return {"error": "Empty query"}
    relevant = _find_relevant_nodes(G, query_tokens, top_n=max_nodes, graph_dir=graph_dir)
    if not relevant:
        for token in query_tokens:
            nid = _find_node_id(G, token)
            if nid and nid not in {r[0] for r in relevant}:
                nd = G.nodes[nid]
                score = _score_node_relevance(nd, query_tokens)
                relevant.append((nid, max(score, 1.0), nd))
    if not relevant:
        return {"query": query, "result": "no_matching_nodes"}
    n_nodes = G.number_of_nodes()
    adaptive_depth = 3 if n_nodes < 500 else 2
    context = _extract_subgraph_context(G, relevant, max_depth=adaptive_depth, max_nodes=max_nodes)
    key_paths = _extract_key_paths(G, relevant, max_paths=5)
    summary = _generate_exploration_summary(query, relevant, key_paths, context)
    return {
        "query": query,
        "summary": summary,
        "matching_nodes": len(relevant),
        "top_matches": [
            {"name": nd.get("name", ""), "domain": nd.get("domain", ""),
             "labels": nd.get("labels", []),
             "location": f"{nd.get('source_file', '')}:{nd.get('line', 0)}",
             "exec_summary": _derive_exec_summary(nd),
             "relevance": round(score, 2)}
            for nid, score, nd in relevant[:10]
        ],
        "key_paths": key_paths[:5],
        "context_nodes": len(context.get("nodes", [])),
    }


def _tool_trace(args: dict, graph_dir: str) -> dict:
    """Trace chain from A to B."""
    from _builder.graph_build import _load_full_graph
    from _builder.utils import _find_node_id, _make_call_graph
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    from_node = _find_node_id(G, args.get("from", ""))
    to_node = _find_node_id(G, args.get("to", "")) if args.get("to") else None
    if not from_node:
        return {"error": f"Source node not found: {args.get('from', '')}"}
    # Use networkx shortest_path for A→B tracing (call edges only)
    if to_node:
        import networkx as nx
        call_G = _make_call_graph(G)
        try:
            path = nx.shortest_path(call_G, from_node, to_node)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return {"from": from_node, "to": to_node, "path": [], "error": "No path found"}
        annotated = []
        for i, nid in enumerate(path):
            nd = G.nodes[nid]
            step = {"id": nid, "name": nd.get("name", ""),
                    "domain": nd.get("domain", ""), "labels": nd.get("labels", [])}
            if i > 0:
                ed = G.get_edge_data(path[i-1], nid) or {}
                step["call_condition"] = ed.get("call_condition", "")
                step["concurrency"] = ed.get("concurrency", "")
            annotated.append(step)
        return {"from": from_node, "to": to_node, "path": annotated, "length": len(path) - 1}
    else:
        # No target: BFS forward trace
        from collections import deque
        visited = {from_node}
        order = [from_node]
        queue = deque([from_node])
        while queue:
            n = queue.popleft()
            for s in G.successors(n):
                ed = G.get_edge_data(n, s) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                if s not in visited:
                    visited.add(s)
                    order.append(s)
                    queue.append(s)
        annotated = [{"id": nid, "name": G.nodes[nid].get("name", "")} for nid in order]
        return {"from": from_node, "path": annotated, "total": len(order)}


def _tool_impact(args: dict, graph_dir: str) -> dict:
    """Impact analysis for a node."""
    from _builder.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    node_id = _find_node_id(G, args.get("node", ""))
    if not node_id:
        return {"error": f"Node not found: {args.get('node', '')}"}
    direction = args.get("direction", "reverse")
    depth = args.get("depth", 3)
    visited = set()
    result_nodes = []
    frontier = [node_id]
    for _ in range(depth):
        next_frontier = []
        for n in frontier:
            if n in visited:
                continue
            visited.add(n)
            nd = G.nodes[n]
            result_nodes.append({"id": n, "name": nd.get("name", ""),
                                 "domain": nd.get("domain", ""),
                                 "labels": nd.get("labels", [])})
            if direction == "reverse":
                next_frontier.extend(p for p in G.predecessors(n)
                                     if (G.get_edge_data(p, n) or {}).get("relation") not in ("CONTAINS", "IMPORTS"))
            else:
                next_frontier.extend(s for s in G.successors(n)
                                     if (G.get_edge_data(n, s) or {}).get("relation") not in ("CONTAINS", "IMPORTS"))
        frontier = next_frontier
    return {"node": node_id, "direction": direction, "affected": result_nodes,
            "total_affected": len(result_nodes) - 1}


def _tool_key_paths(args: dict, graph_dir: str) -> list:
    """Extract key execution paths."""
    from _builder.key_paths import _compute_entry_scores, _find_endpoints, _extract_key_paths_from_entries
    from _builder.graph_build import _load_full_graph
    G = _get_graph(graph_dir)
    if not G:
        return []
    top = args.get("top", 5)
    from_entry = args.get("from_entry")

    # Compute entry scores as {node_id: float_score} dict (same format as CLI)
    entry_scores = _compute_entry_scores(G)
    if from_entry:
        from _builder.utils import _find_node_id
        nid = _find_node_id(G, from_entry)
        if nid:
            entry_scores = {nid: entry_scores.get(nid, 5.0)}
        else:
            entry_scores = {}

    endpoints = _find_endpoints(G)
    paths = _extract_key_paths_from_entries(G, entry_scores, endpoints, top_n=top)
    return paths


def _tool_concurrency(args: dict, graph_dir: str) -> list:
    """List concurrency risk points."""
    from _builder.graph_build import _load_full_graph
    G = _get_graph(graph_dir)
    if not G:
        return []
    top = args.get("top", 50)
    spawn_points = []
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        for ca in ndata.get("callee_args", []):
            ci = ca.get("concurrency_info", {})
            if ci.get("is_spawn") or ci.get("concurrency_type") in ("thread_spawn", "goroutine"):
                concurrent = []
                spawn_order = ca.get("call_order") or 0
                for succ in G.successors(nid):
                    ed = G.get_edge_data(nid, succ) or {}
                    if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                        continue
                    if ed.get("call_order") is not None and ed["call_order"] > spawn_order and \
                       ed.get("concurrency") not in ("spawn_target", "callback"):
                        concurrent.append(G.nodes[succ].get("name", ""))
                spawn_points.append({
                    "spawn_node": ndata.get("name", ""),
                    "source_file": ndata.get("source_file", ""),
                    "thread_entry": ci.get("spawn_target", ""),
                    "concurrent_with": concurrent[:5],
                    "risk": "Race" if concurrent else "Safe",
                })
    spawn_points.sort(key=lambda x: -len(x["concurrent_with"]))
    return spawn_points[:top]


def _tool_data_lifecycle(args: dict, graph_dir: str) -> dict:
    """Trace resource lifecycle."""
    from _builder.graph_build import _load_full_graph
    from _builder.utils import _simple_tokenize, _similarity_score
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    resource = args.get("resource", "")
    if not resource:
        return {"error": "Missing required parameter: resource"}
    alloc_nodes = []
    use_nodes = []
    release_nodes = []
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        name = ndata.get("name", "")
        tokens = _simple_tokenize(f"{name} {ndata.get('signature', '')} {ndata.get('semantic_desc', '')}")
        if _similarity_score(_simple_tokenize(resource), tokens) > 0:
            labels = ndata.get("labels", [])
            if any(w in name.lower() for w in ("alloc", "malloc", "create", "new", "init")):
                alloc_nodes.append({"name": name, "id": nid, "domain": ndata.get("domain", "")})
            elif any(w in name.lower() for w in ("free", "destroy", "cleanup", "release", "close")):
                release_nodes.append({"name": name, "id": nid, "domain": ndata.get("domain", "")})
            else:
                use_nodes.append({"name": name, "id": nid, "domain": ndata.get("domain", "")})
    return {
        "resource": resource,
        "allocations": alloc_nodes[:10],
        "usages": use_nodes[:10],
        "releases": release_nodes[:10],
    }


def _tool_domain(args: dict, graph_dir: str) -> dict:
    """List nodes/edges in a domain."""
    from _builder.graph_build import _load_full_graph
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    domain_name = args.get("name", "")
    if not domain_name:
        return {"error": "Missing required parameter: name"}
    nodes = [{"id": nid, "name": nd.get("name", ""), "labels": nd.get("labels", [])}
             for nid, nd in G.nodes(data=True)
             if nd.get("domain", "") == domain_name and not nd.get("is_empty", False)]
    return {"domain": domain_name, "nodes": nodes}


def _tool_knowledge_query(args: dict, graph_dir: str) -> dict:
    """Query knowledge by topic.

    Prefers the unified FTS5+BM25 path (kb_paragraphs) when the
    project has code2database.db; falls back to the legacy substring
    search via KnowledgeManager otherwise.
    """
    topic = args.get("topic", "")
    if not topic:
        return {"error": "Missing required parameter: topic"}
    try:
        from _builder.kb_index import query_kb
        results = query_kb(
            graph_dir=graph_dir,
            query=topic,
            top_n=10,
            kinds=["principle", "fact", "pattern", "glossary"],
            min_weight=0.0,
            max_tokens=int(args.get("max_tokens", 500)),
        )
        if results:
            return {
                "topic": args.get("topic", ""),
                "matches": results,
                "engine": "fts5_bm25",
            }
        # No FTS5 hits — fall through to legacy
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    from _builder.knowledge_manager import KnowledgeManager
    km = KnowledgeManager(graph_dir)
    result = km.query_knowledge(args.get("topic", ""), max_tokens=args.get("max_tokens", 500))
    return {"topic": args.get("topic", ""), "result": result, "engine": "substring_fallback"}


def _tool_memory_search(args: dict, graph_dir: str) -> list:
    """Search memory for similar questions.

    Prefers the unified FTS5+BM25 path (kb_paragraphs) when the
    project has code2database.db; falls back to the legacy Jaccard
    search via MemoryManager otherwise.
    """
    query = args.get("query", "")
    if not query:
        return [{"error": "Missing required parameter: query"}]
    try:
        from _builder.kb_index import query_kb
        results = query_kb(
            graph_dir=graph_dir,
            query=query,
            top_n=int(args.get("top", 5)),
            kinds=["memory_qa", "memory_experience"],
            min_weight=0.0,  # no weight filter; let BM25 rank
            max_tokens=4000,
        )
        if results:
            return results
        # Fall through to legacy
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    from _builder.memory_manager import MemoryManager
    mm = MemoryManager(graph_dir)
    return mm.query(args.get("query", ""), top_n=args.get("top", 5))


def _tool_kb_query(args: dict, graph_dir: str) -> dict:
    """Unified FTS5+BM25 query across memory + knowledge.

    Phase 3 of the KB unification. Replaces the need to call
    code2database_memory_search AND code2database_knowledge_query
    separately — this single tool searches both stores via the
    shared kb_paragraphs_fts index.
    """
    from _builder.kb_index import query_kb
    query = args.get("query", "")
    if not query:
        return {"error": "query is required"}
    kinds_str = args.get("kinds", "")
    kinds = [k.strip() for k in kinds_str.split(",") if k.strip()] if kinds_str else None
    results = query_kb(
        graph_dir=graph_dir,
        query=query,
        top_n=int(args.get("top", 10)),
        kinds=kinds,
        min_weight=float(args.get("min_weight", 0.0)),
        max_tokens=int(args.get("max_tokens", 4000)),
    )
    return {
        "query": query,
        "kinds": kinds,
        "total": len(results),
        "results": results,
        "engine": "fts5_bm25",
    }


def _tool_semantic_status(args: dict, graph_dir: str) -> dict:
    """Check if semantic update is recommended."""
    from _builder.changelog_update import get_semantic_update_status
    return get_semantic_update_status(graph_dir)


def _tool_foreign_refs(args: dict, graph_dir: str) -> dict:
    """F3: list cross-C2D foreign refs for a node."""
    import sqlite3
    node_id = args.get("node", "")
    if not node_id:
        return {"error": "node is required"}
    db_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.exists(db_path):
        return {"error": "no db", "foreign_refs": []}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT foreign_c2d_path, foreign_node_id, foreign_name, "
            "foreign_domain, foreign_source_file, foreign_signature, "
            "status, resolution_strategy, last_resolved_at "
            "FROM foreign_refs WHERE local_node_id = ?",
            (node_id,)
        ).fetchall()
        return {
            "node": node_id,
            "foreign_refs_count": len(rows),
            "foreign_refs": [dict(r) for r in rows],
        }
    except sqlite3.OperationalError:
        return {"node": node_id, "foreign_refs_count": 0, "foreign_refs": []}
    finally:
        conn.close()


def _tool_sync_foreign(args: dict, graph_dir: str) -> dict:
    """F3: trigger sync of foreign_refs."""
    from _builder.c2d_foreign import sync_foreign
    return sync_foreign(
        graph_dir,
        foreign_c2d_path=args.get("foreign_c2d", "") or "",
        verbose=False,
    )


def _tool_composite_query(args: dict, graph_dir: str) -> dict:
    """F3: cross-C2D query via ATTACH."""
    from _builder.c2d_phase2 import composite_query
    query = args.get("query", "")
    if not query:
        return {"error": "Missing required parameter: query"}
    foreign_c2ds = []
    fc = args.get("foreign_c2ds", "")
    if fc:
        foreign_c2ds = [s.strip() for s in fc.split(",") if s.strip()]
    return composite_query(
        graph_dir=graph_dir,
        query=query,
        foreign_c2ds=foreign_c2ds,
        top_n=int(args.get("top", 50)),
    )


def _tool_get_code_snippet(args: dict, graph_dir: str) -> dict:
    """Get source code snippet for a node."""
    from _builder.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    node_id = _find_node_id(G, args.get("node", ""))
    if not node_id:
        return {"error": f"Node not found: {args.get('node', '')}"}
    nd = G.nodes[node_id]
    source_file = nd.get("source_file", "")
    line_num = nd.get("line", 0)
    context = args.get("context", 10)
    if not source_file or not line_num:
        return {"error": "No source location for this node"}
    # Resolve source_root from master.json for relative paths
    full_path = _resolve_source_file(source_file, graph_dir)
    try:
        with open(full_path, "r", errors="replace") as f:
            lines = f.readlines()
        start = max(0, line_num - context - 1)
        end = min(len(lines), line_num + context)
        snippet = "".join(lines[start:end])
        return {"node": node_id, "name": nd.get("name", ""),
                "file": source_file, "line": line_num,
                "snippet": snippet}
    except FileNotFoundError:
        return {"error": f"Source file not found: {full_path}"}


def _tool_blast_radius(args: dict, graph_dir: str) -> dict:
    """Blast radius analysis: find affected functions/APIs/tests from a change."""
    from _builder.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    node_id = _find_node_id(G, args.get("node", ""))
    if not node_id:
        return {"error": f"Node not found: {args.get('node', '')}"}
    depth = args.get("depth", 3)
    # Reverse BFS to find all callers up to depth
    visited = set()
    frontier = [node_id]
    affected = []
    api_affected = []
    test_affected = []
    affected_domains = set()
    for d in range(depth):
        next_frontier = []
        for n in frontier:
            if n in visited:
                continue
            visited.add(n)
            nd = G.nodes[n]
            labels = nd.get("labels", [])
            dom = nd.get("domain", "")
            affected.append({"id": n, "name": nd.get("name", ""),
                             "domain": dom, "labels": labels})
            affected_domains.add(dom)
            if "API_entry" in labels:
                api_affected.append({"id": n, "name": nd.get("name", ""), "domain": dom})
            if any(t in nd.get("name", "").lower() for t in ("test_", "_test", "_ut_")):
                test_affected.append({"id": n, "name": nd.get("name", ""), "domain": dom})
            next_frontier.extend(p for p in G.predecessors(n)
                                 if (G.get_edge_data(p, n) or {}).get("relation") not in ("CONTAINS", "IMPORTS"))
        frontier = next_frontier
    return {"changed_node": node_id,
            "depth": depth,
            "affected_functions": len(affected) - 1,
            "affected_apis": api_affected,
            "affected_tests": test_affected,
            "affected_domains": sorted(affected_domains),
            "all_affected": affected}


def _tool_extract_signals(args: dict, graph_dir: str) -> dict:
    """Extract #ifdef condition signals and their affected edges."""
    cond_path = os.path.join(graph_dir, ".code2database_condition_index.json")
    if not os.path.exists(cond_path):
        return {"error": "No condition index found. Run 'build' first."}
    from _builder.graph_build import _load_full_graph
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    cond_data = json.loads(Path(cond_path).read_text(encoding="utf-8"))
    signal_map = {}
    for nid, branches in cond_data.items():
        nd = G.nodes.get(nid)
        if not nd:
            continue
        for branch in branches:
            condition = branch.get("condition", "")
            if not condition:
                continue
            cond_var = condition.strip()
            for prefix in ("#ifdef ", "#ifndef ", "#if ", "#elif "):
                if cond_var.startswith(prefix):
                    cond_var = cond_var[len(prefix):].strip()
                    break
            target_name = branch.get("target_name", "")
            if cond_var not in signal_map:
                signal_map[cond_var] = {"condition": condition,
                                        "edges": [], "functions": set(), "domains": set()}
            signal_map[cond_var]["edges"].append({"source": nd.get("name", ""),
                                                   "target": target_name,
                                                   "condition": condition})
            signal_map[cond_var]["functions"].add(nd.get("name", ""))
            if target_name:
                signal_map[cond_var]["functions"].add(target_name)
            signal_map[cond_var]["domains"].add(nd.get("domain", ""))
    # Convert sets
    for var in signal_map:
        signal_map[var]["functions"] = sorted(signal_map[var]["functions"])
        signal_map[var]["domains"] = sorted(signal_map[var]["domains"])
    sorted_signals = sorted(signal_map.items(), key=lambda x: -len(x[1]["edges"]))
    return {"total_signals": len(sorted_signals),
            "top_signals": {var: data for var, data in sorted_signals[:20]}}


# ---------------------------------------------------------------------------
# Additional tool handlers (D37: MCP server tool expansion)
# ---------------------------------------------------------------------------

def _tool_path_feasible(args: dict, graph_dir: str) -> dict:
    """Check feasibility of a path under #ifdef conditions using Z3 or heuristics."""
    from _builder.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    from _builder.path_feasibility import check_path_feasibility
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    node_hint = args.get("node", "")
    node_id = _find_node_id(G, node_hint)
    if not node_id:
        return {"error": f"node {node_hint!r} not found"}
    config = args.get("config", {})
    result = check_path_feasibility(G, node_id, config)
    return result


def _tool_find_invariants(args: dict, graph_dir: str) -> dict:
    """Find invariants for a function."""
    from _builder.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    from _builder.invariants import extract_invariants_for_node
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    node_hint = args.get("node", "")
    node_id = _find_node_id(G, node_hint)
    if not node_id:
        return {"error": f"node {node_hint!r} not found"}
    ndata = G.nodes[node_id]
    return extract_invariants_for_node(ndata)


def _tool_ffi_trace(args: dict, graph_dir: str) -> dict:
    """Trace FFI boundaries from a function."""
    from _builder.ffi_bridge import trace_ffi
    from _builder.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    node_hint = args.get("node", "")
    node_id = _find_node_id(G, node_hint)
    if not node_id:
        return {"error": f"node {node_hint!r} not found"}
    return trace_ffi(G, node_id)


def _tool_doc_code_check(args: dict, graph_dir: str) -> dict:
    """Check doc-code alignment for a function or all functions."""
    from _builder.doc_code_align import check_doc_code_alignment
    node_filter = [args.get("node")] if args.get("node") else None
    return check_doc_code_alignment(graph_dir, node_filter=node_filter)


def _tool_daemon_status(args: dict, graph_dir: str) -> dict:
    """Check daemon status."""
    import json as _json
    status_path = os.path.join(graph_dir, ".daemon_status.json")
    if not os.path.exists(status_path):
        return {"running": False, "error": "daemon not started"}
    try:
        with open(status_path) as f:
            return _json.load(f)
    except (OSError, _json.JSONDecodeError) as exc:
        return {"running": False, "error": str(exc)}


def _tool_who_allocates(args: dict, graph_dir: str) -> dict:
    """Find functions that allocate a resource."""
    from _builder.graph_build import _load_full_graph
    from _builder.semantic_edges import who_allocates
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    resource = args.get("resource", "")
    results = who_allocates(G, resource)
    return {"count": len(results), "functions": results}


def _tool_who_frees(args: dict, graph_dir: str) -> dict:
    """Find functions that free a resource."""
    from _builder.graph_build import _load_full_graph
    from _builder.semantic_edges import who_frees
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    resource = args.get("resource", "")
    results = who_frees(G, resource)
    return {"count": len(results), "functions": results}


def _tool_who_locks(args: dict, graph_dir: str) -> dict:
    """Find functions that acquire a lock."""
    from _builder.graph_build import _load_full_graph
    from _builder.semantic_edges import who_locks
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    lock_name = args.get("lock", "")
    results = who_locks(G, lock_name)
    return {"count": len(results), "functions": results}


def _tool_explain_label(args: dict, graph_dir: str) -> dict:
    """Explain why a node has a given label."""
    from _builder.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    from _builder.explain import explain_label
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    node_hint = args.get("node", "")
    label = args.get("label", "")
    if not node_hint:
        return {"error": "Missing required parameter: node"}
    if not label:
        return {"error": "Missing required parameter: label"}
    node_id = _find_node_id(G, node_hint)
    if not node_id:
        return {"error": f"node {node_hint!r} not found"}
    return explain_label(G, node_id, label)


def _tool_why_ambiguous(args: dict, graph_dir: str) -> dict:
    """Explain why an edge is marked AMBIGUOUS."""
    from _builder.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    from _builder.explain import why_ambiguous
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    from_hint = args.get("from", "")
    to_hint = args.get("to", "")
    from_id = _find_node_id(G, from_hint)
    to_id = _find_node_id(G, to_hint)
    if not from_id or not to_id:
        return {"error": "node not found"}
    return why_ambiguous(G, from_id, to_id)


def _tool_audit_log(args: dict, graph_dir: str) -> dict:
    """Query the audit log."""
    from _builder.audit_log import query_audit_log
    return query_audit_log(
        graph_dir,
        target_id=args.get("node"),
        command=args.get("command"),
        tx_id=args.get("tx"),
        limit=int(args.get("limit", 100)),
    )


def _tool_happens_before(args: dict, graph_dir: str) -> dict:
    """Check happens-before between a writer and reader via locks/RCU/barriers."""
    from _builder.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    from _builder.memory_ordering import happens_before_analysis
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    writer_hint = args.get("writer", "")
    reader_hint = args.get("reader", "")
    variable = args.get("var", "")
    writer_id = _find_node_id(G, writer_hint)
    reader_id = _find_node_id(G, reader_hint)
    if not writer_id or not reader_id:
        return {"error": "writer or reader node not found"}
    return happens_before_analysis(G, writer_id, reader_id, variable)


def _tool_memory_ordering(args: dict, graph_dir: str) -> dict:
    """Show memory-ordering primitives used by a function."""
    from _builder.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    from _builder.memory_ordering import analyze_memory_ordering
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    node_hint = args.get("node", "")
    node_id = _find_node_id(G, node_hint)
    if not node_id:
        return {"error": f"node {node_hint!r} not found"}
    ndata = G.nodes[node_id]
    info = analyze_memory_ordering(ndata, G, node_id)
    return info.to_dict() if hasattr(info, "to_dict") else vars(info)


def _tool_unbalanced_alloc_free(args: dict, graph_dir: str) -> dict:
    """Find functions that allocate without freeing (or vice versa)."""
    from _builder.graph_build import _load_full_graph
    from _builder.semantic_edges import unbalanced_alloc_free
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    return unbalanced_alloc_free(G)


# ---------------------------------------------------------------------------
# cgdb (code graph database) tools — Phase 5 per doc 5.5.7
# These wrap SQLiteCGDBStore reader methods. When the graph_dir contains a
# code2database.db with cgdb tables, they query the cgdb schema directly.
# ---------------------------------------------------------------------------

_CGDB_STORE_CACHE: dict = {}


def _cgdb_store(graph_dir: str):
    """Get a SQLiteCGDBStore for the graph_dir's code2database.db. Returns None if
    the DB doesn't exist or cgdb tables aren't present.

    The store is cached per graph_dir for the lifetime of the MCP server
    process; the underlying SQLite connection is reused across tool
    invocations to avoid the 1-5ms connect + PRAGMA-setup overhead per
    call. The cache is invalidated if the underlying db file is removed
    or replaced (the existence check still runs).
    """
    import os
    import sqlite3
    db_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.exists(db_path):
        # Drop any stale cache entry.
        _CGDB_STORE_CACHE.pop(graph_dir, None)
        return None
    cached = _CGDB_STORE_CACHE.get(graph_dir)
    if cached is not None:
        # Sanity-check that the cached store's connection is still live.
        try:
            cached._ensure_conn().execute("SELECT 1").fetchone()
            return cached
        except sqlite3.Error:
            _CGDB_STORE_CACHE.pop(graph_dir, None)
    try:
        from _builder.cgdb_store import SQLiteCGDBStore
        store = SQLiteCGDBStore(db_path)
        conn = store._ensure_conn()
        conn.execute("SELECT 1 FROM cgdb_nodes LIMIT 1").fetchone()
        _CGDB_STORE_CACHE[graph_dir] = store
        return store
    except sqlite3.Error:
        # Expected when cgdb tables are absent (tree-sitter builds) or the
        # db doesn't exist — narrow to sqlite3.Error so programming errors
        # (AttributeError/NameError from typos) surface instead of being
        # swallowed as 'cgdb unavailable'.
        return None


def _tool_cgdb_search_symbols(args: dict, graph_dir: str) -> list:
    """Full-text search over cgdb_nodes via FTS5."""
    query = args.get("query", "")
    kind = args.get("kind")
    limit = int(args.get("limit", 50))
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
    return store.get_definition(name, limit=int(args.get("limit", 10)))

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
    context_bytes = int(args.get("context_bytes", 0) or 0)
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
    try:
        with open(resolved_path, "rb") as fh:
            raw = fh.read()
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
    depth = int(args.get("depth", 1))
    edge_types = args.get("edge_types", ["INVOKES"])
    limit = int(args.get("limit", 200))
    include_vtable_dispatch = bool(args.get("include_vtable_dispatch", False))
    return store.find_invokers(int(node_id), depth=depth,
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
    depth = int(args.get("depth", 1))
    edge_types = args.get("edge_types", ["INVOKES"])
    limit = int(args.get("limit", 500))
    include_vtable_dispatch = bool(args.get("include_vtable_dispatch", False))
    return store.find_invoked(int(node_id), depth=depth,
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
    return store.find_type_definition(name, limit=int(args.get("limit", 10)))

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
    return store.find_cfg_paths(int(func_id), max_len=int(args.get("max_len", 10)))

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
    return store.find_nodes_under_config(config, limit=int(args.get("limit", 500)))

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
    return store.list_versions(limit=int(args.get("limit", 50)))

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS = {
    "code2database_load": {
        "description": "Load and summarize the invocation graph. Returns node/edge/domain counts.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": _tool_load,
    },
    "code2database_search": {
        "description": "Search nodes by keywords. Returns matching nodes with scores.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keywords": {"type": "string", "description": "Space-separated keywords"},
                "top": {"type": "integer", "description": "Max results (default 20)"},
            },
            "required": ["keywords"],
        },
        "handler": _tool_search,
    },
    "code2database_describe": {
        "description": "Describe a node by ID or name. Returns function details at brief/standard/full level.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Node ID or name"},
                "detail": {"type": "string", "enum": ["brief", "standard", "full"],
                          "description": "Detail level (default brief)"},
            },
            "required": ["node"],
        },
        "handler": _tool_describe,
    },
    "code2database_explore": {
        "description": "One-shot context retrieval by natural language query. Returns relevant nodes, paths, and conditions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language or symbol query"},
                "max_nodes": {"type": "integer", "description": "Max nodes (default 15)"},
                "max_tokens": {"type": "integer", "description": "Max tokens (default 2000)"},
            },
            "required": ["query"],
        },
        "handler": _tool_explore,
    },
    "code2database_trace": {
        "description": "Trace call chain from one function to another. Returns annotated path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from": {"type": "string", "description": "Source node ID or name"},
                "to": {"type": "string", "description": "Target node ID or name"},
            },
            "required": ["from"],
        },
        "handler": _tool_trace,
    },
    "code2database_impact": {
        "description": "Impact analysis for a node. Returns upstream (reverse) or downstream (forward) affected nodes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Node ID or name"},
                "direction": {"type": "string", "enum": ["reverse", "forward"],
                             "description": "Direction (default reverse)"},
                "depth": {"type": "integer", "description": "Traverse depth (default 3)"},
            },
            "required": ["node"],
        },
        "handler": _tool_impact,
    },
    "code2database_key_paths": {
        "description": "Extract key execution paths from entry points.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "top": {"type": "integer", "description": "Number of paths (default 5)"},
                "from_entry": {"type": "string", "description": "Specific entry point (optional)"},
            },
            "required": [],
        },
        "handler": _tool_key_paths,
    },
    "code2database_concurrency": {
        "description": "List concurrency risk points sorted by risk level.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "top": {"type": "integer", "description": "Max results (default 50)"},
            },
            "required": [],
        },
        "handler": _tool_concurrency,
    },
    "code2database_data_lifecycle": {
        "description": "Trace resource allocation-usage-release paths.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "description": "Resource keyword (e.g. 'buffer')"},
            },
            "required": ["resource"],
        },
        "handler": _tool_data_lifecycle,
    },
    "code2database_domain": {
        "description": "List nodes/edges in a domain.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Domain name (e.g. lib.bdev)"},
            },
            "required": ["name"],
        },
        "handler": _tool_domain,
    },
    "code2database_knowledge_query": {
        "description": "Query knowledge by topic. Returns relevant knowledge entries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Topic to search"},
                "max_tokens": {"type": "integer", "description": "Max output tokens (default 500)"},
            },
            "required": ["topic"],
        },
        "handler": _tool_knowledge_query,
    },
    "code2database_memory_search": {
        "description": "Search memory for similar questions. Returns Q&A pairs with relevance scores.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "top": {"type": "integer", "description": "Max results (default 5)"},
            },
            "required": ["query"],
        },
        "handler": _tool_memory_search,
    },
    "code2database_kb_query": {
        "description": "Unified FTS5+BM25 query across memory + knowledge. Searches both stores via the shared kb_paragraphs_fts index. Returns ranked results with source kind (memory_qa / knowledge_principle / etc.), score, and body.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-form text query (tokenized and AND-joined for FTS5 MATCH)"},
                "top": {"type": "integer", "description": "Max results (default 10)"},
                "kinds": {"type": "string", "description": "Comma-separated kind filter (e.g. 'memory_qa,knowledge_principle')"},
                "min_weight": {"type": "number", "description": "Skip rows with weight below this (default 0.0 = no filter)"},
                "max_tokens": {"type": "integer", "description": "Approximate char cap on returned bodies (default 4000)"},
            },
            "required": ["query"],
        },
        "handler": _tool_kb_query,
    },
    "code2database_foreign_refs": {
        "description": "List cross-C2D foreign references for a node. Shows which functions in external C2Ds (project A) are called by this node (project B). Returns foreign_node_id, name, domain, source_file, signature, status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Local node ID to check foreign refs for"},
            },
            "required": ["node"],
        },
        "handler": _tool_foreign_refs,
    },
    "code2database_sync_foreign": {
        "description": "Trigger sync of foreign_refs with updated foreign C2Ds. Detects when external C2D (A) has changed (mtime/size/count diff) and re-resolves B's foreign_refs. Returns sync summary with newly_resolved/deleted/stale counts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "foreign_c2d": {"type": "string", "description": "Specific foreign C2D path to sync (default: all watched)"},
            },
            "required": [],
        },
        "handler": _tool_sync_foreign,
    },
    "code2database_composite_query": {
        "description": "Cross-C2D query via SQLite ATTACH. Finds callers/callees across local + foreign C2Ds. Supports 'CALLERS_OF name' and 'CALLEES_OF name' query language. Returns results tagged with source_db.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "CALLERS_OF <name> or CALLEES_OF <name> or free-text"},
                "foreign_c2ds": {"type": "string", "description": "Comma-separated foreign C2D paths to attach"},
                "top": {"type": "integer", "description": "Max results (default 50)"},
            },
            "required": ["query"],
        },
        "handler": _tool_composite_query,
    },
    "code2database_semantic_status": {
        "description": "Check if semantic update is recommended based on stale node accumulation.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": _tool_semantic_status,
    },
    "code2database_get_code_snippet": {
        "description": "Get source code snippet for a node. Returns lines around the function definition.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Node ID or name"},
                "context": {"type": "integer", "description": "Lines of context (default 10)"},
            },
            "required": ["node"],
        },
        "handler": _tool_get_code_snippet,
    },
    "code2database_blast_radius": {
        "description": "Blast radius analysis: find all functions, APIs, and tests affected by a change to a function.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Node ID or name of changed function"},
                "depth": {"type": "integer", "description": "Reverse BFS depth (default 3)"},
            },
            "required": ["node"],
        },
        "handler": _tool_blast_radius,
    },
    "code2database_extract_signals": {
        "description": "Extract #ifdef condition signals and their affected edges/functions. Shows how preprocessor conditions control call paths.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": _tool_extract_signals,
    },
    # ---- D37: New tools for expanded MCP coverage (16 -> 34) ----
    "code2database_path_feasible": {
        "description": "Check feasibility of a path under #ifdef conditions using Z3 or heuristics.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Function name or id"},
                "config": {"type": "object", "description": "Build config (e.g., {\"CONFIG_X\": true})"},
            },
            "required": ["node"],
        },
        "handler": _tool_path_feasible,
    },
    "code2database_find_invariants": {
        "description": "Extract preconditions, postconditions, loop invariants for a function.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Function name or id"},
            },
            "required": ["node"],
        },
        "handler": _tool_find_invariants,
    },
    "code2database_ffi_trace": {
        "description": "Trace FFI boundaries from a function (ctypes, cgo, extern C).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Function name or id"},
            },
            "required": ["node"],
        },
        "handler": _tool_ffi_trace,
    },
    "code2database_doc_code_check": {
        "description": "Check doc-code alignment (return values, params, signature mismatches).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Optional: limit check to one function"},
            },
            "required": [],
        },
        "handler": _tool_doc_code_check,
    },
    "code2database_daemon_status": {
        "description": "Check daemon status (running state, pending events, last sync).",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": _tool_daemon_status,
    },
    "code2database_who_allocates": {
        "description": "Find functions that allocate a resource (kmalloc, malloc, new, etc.).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "description": "Optional: filter by resource name"},
            },
            "required": [],
        },
        "handler": _tool_who_allocates,
    },
    "code2database_who_frees": {
        "description": "Find functions that free a resource (kfree, free, delete, etc.).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "description": "Optional: filter by resource name"},
            },
            "required": [],
        },
        "handler": _tool_who_frees,
    },
    "code2database_who_locks": {
        "description": "Find functions that acquire a lock (mutex_lock, spin_lock, etc.).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lock": {"type": "string", "description": "Optional: filter by lock variable name"},
            },
            "required": [],
        },
        "handler": _tool_who_locks,
    },
    "code2database_explain_label": {
        "description": "Explain why a node has a given label (dead_code, API_entry, etc.).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Function name or id"},
                "label": {"type": "string", "description": "Label to explain"},
            },
            "required": ["node", "label"],
        },
        "handler": _tool_explain_label,
    },
    "code2database_why_ambiguous": {
        "description": "Explain why an edge is marked AMBIGUOUS (fn_ptr, dead #ifdef, vtable).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from": {"type": "string", "description": "Caller function name or id"},
                "to": {"type": "string", "description": "Callee function name or id"},
            },
            "required": ["from", "to"],
        },
        "handler": _tool_why_ambiguous,
    },
    "code2database_audit_log": {
        "description": "Query the audit log (who edited what, when, why).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Filter by target node id"},
                "command": {"type": "string", "description": "Filter by command name"},
                "tx": {"type": "string", "description": "Filter by transaction id"},
                "limit": {"type": "integer", "description": "Max entries (default 100)"},
            },
            "required": [],
        },
        "handler": _tool_audit_log,
    },
    "code2database_happens_before": {
        "description": "Check happens-before between a writer and reader via locks, RCU, or memory barriers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "writer": {"type": "string", "description": "Writer function name or id"},
                "reader": {"type": "string", "description": "Reader function name or id"},
                "var": {"type": "string", "description": "Variable name"},
            },
            "required": ["writer", "reader"],
        },
        "handler": _tool_happens_before,
    },
    "code2database_memory_ordering": {
        "description": "Show memory-ordering primitives (RCU, barriers, atomics) used by a function.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Function name or id"},
            },
            "required": ["node"],
        },
        "handler": _tool_memory_ordering,
    },
    "code2database_unbalanced_alloc_free": {
        "description": "Find functions that allocate without freeing (or vice versa).",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": _tool_unbalanced_alloc_free,
    },
    # ---- cgdb (Phase 5): clang-based code graph database tools ----
    "cgdb_search_symbols": {
        "description": "Full-text search over cgdb_nodes via FTS5 (clang backend).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (name or fqn fragment)"},
                "kind": {"type": "string", "description": "Optional node kind filter (function/var/field/typedef/struct/union/enum/parm/decl_ref/call_expr/member_ref)"},
                "limit": {"type": "integer", "description": "Max results (default 50)"},
            },
            "required": ["query"],
        },
        "handler": _tool_cgdb_search_symbols,
    },
    "cgdb_get_definition": {
        "description": "Find definition nodes by name (function/var/field/typedef). Returns id, fqn, file, line, type_spelling.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name to find"},
                "limit": {"type": "integer", "description": "Max results (default 10)"},
            },
            "required": ["name"],
        },
        "handler": _tool_cgdb_get_definition,
    },
    "cgdb_get_function_body": {
        "description": "Return the function body source text for a function (name or id).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Function name or node id"},
            },
            "required": ["node"],
        },
        "handler": _tool_cgdb_get_function_body,
    },
    "cgdb_get_source": {
        "description": "Get source text for a node with byte-precise attribution. Resolution: source_snippet column → file read via byte_start..byte_end.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Function/symbol name or node id"},
                "snippet_only": {"type": "boolean", "description": "Only return source_snippet (skip file read)"},
                "context_bytes": {"type": "integer", "description": "Include N bytes of surrounding context"},
            },
            "required": ["node"],
        },
        "handler": _tool_cgdb_get_source,
    },
    "cgdb_find_invokers": {
        "description": "Find callers of a node via recursive CTE with cycle protection. Set include_vtable_dispatch=true to also follow indirect dispatch via ops_bindings + invoke_sites (finds vtable callers even when no pre-computed INVOKES edge exists).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "integer", "description": "Source node id (cgdb_nodes.id)"},
                "depth": {"type": "integer", "description": "Recursive depth (default 1)"},
                "edge_types": {"type": "array", "items": {"type": "string"}, "description": "Edge kinds to traverse (default [\"INVOKES\"])"},
                "limit": {"type": "integer", "description": "Max results (default 200)"},
                "include_vtable_dispatch": {"type": "boolean", "description": "Also follow indirect dispatch via ops_bindings + invoke_sites (default false)"},
            },
            "required": ["node_id"],
        },
        "handler": _tool_cgdb_find_invokers,
    },
    "cgdb_find_invoked": {
        "description": "Find callees of a node via recursive CTE with cycle protection. Set include_vtable_dispatch=true to also resolve vtable dispatch via ops_bindings (finds impl functions invoked via function pointer calls).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "integer", "description": "Source node id"},
                "depth": {"type": "integer", "description": "Recursive depth (default 1)"},
                "edge_types": {"type": "array", "items": {"type": "string"}, "description": "Edge kinds (default [\"INVOKES\"])"},
                "limit": {"type": "integer", "description": "Max results (default 500)"},
                "include_vtable_dispatch": {"type": "boolean", "description": "Also resolve vtable dispatch via ops_bindings (default false)"},
            },
            "required": ["node_id"],
        },
        "handler": _tool_cgdb_find_invoked,
    },
    "cgdb_get_struct_layout": {
        "description": "Return a struct/union's field layout with types.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Struct name (or use 'struct' alias)"},
                "struct": {"type": "string", "description": "Alias for name"},
            },
            "required": [],
        },
        "handler": _tool_cgdb_get_struct_layout,
    },
    "cgdb_find_type_definition": {
        "description": "Find type definitions (struct/union/enum/typedef) by name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Type name"},
                "limit": {"type": "integer", "description": "Max results (default 10)"},
            },
            "required": ["name"],
        },
        "handler": _tool_cgdb_find_type_definition,
    },
    "cgdb_find_ops_impls": {
        "description": "Find functions bound to a vtable field (e.g., file_operations.read_iter). Uses ops_bindings table.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "field_name": {"type": "string", "description": "Ops field name (e.g., 'read_iter')"},
                "struct_type": {"type": "string", "description": "Optional: limit to a specific struct type (e.g., 'file_operations')"},
            },
            "required": ["field_name"],
        },
        "handler": _tool_cgdb_find_ops_impls,
    },
    "cgdb_find_cfg_paths": {
        "description": "Find CFG paths from entry to exit in a function.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_id": {"type": "integer", "description": "Function node id"},
                "max_len": {"type": "integer", "description": "Max path length (default 10)"},
            },
            "required": ["function_id"],
        },
        "handler": _tool_cgdb_find_cfg_paths,
    },
    "cgdb_find_data_flow": {
        "description": "Find def-use chain entries for a variable.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "var_id": {"type": "integer", "description": "Variable node id"},
            },
            "required": ["var_id"],
        },
        "handler": _tool_cgdb_find_data_flow,
    },
    "cgdb_find_aliases": {
        "description": "Find aliases of a pointer (may_alias / must_alias / no_alias).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ptr_id": {"type": "integer", "description": "Pointer node id"},
            },
            "required": ["ptr_id"],
        },
        "handler": _tool_cgdb_find_aliases,
    },
    "cgdb_find_lock_held_calls": {
        "description": "Find calls made while a lock is held in a function.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_id": {"type": "integer", "description": "Function node id"},
            },
            "required": ["function_id"],
        },
        "handler": _tool_cgdb_find_lock_held_calls,
    },
    "cgdb_check_race_condition": {
        "description": "Heuristic race-condition check for a function (looks for unprotected var accesses).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_id": {"type": "integer", "description": "Function node id"},
            },
            "required": ["function_id"],
        },
        "handler": _tool_cgdb_check_race_condition,
    },
    "cgdb_find_configs_for": {
        "description": "Return the config predicate(s) attached to a node (text_form + config_macros).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "integer", "description": "Node id"},
            },
            "required": ["node_id"],
        },
        "handler": _tool_cgdb_find_configs_for,
    },
    "cgdb_find_nodes_under_config": {
        "description": "Find nodes whose config_predicate matches the given predicate text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "config": {"type": "string", "description": "Predicate text (e.g., 'CONFIG_X' or 'NOT CONFIG_X')"},
                "limit": {"type": "integer", "description": "Max results (default 500)"},
            },
            "required": ["config"],
        },
        "handler": _tool_cgdb_find_nodes_under_config,
    },
    "cgdb_index_status": {
        "description": "Return overall cgdb index statistics (node/edge/type/predicate counts, version count).",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": _tool_cgdb_index_status,
    },
    "cgdb_time_travel_query": {
        "description": "Return the state of a node at a specific version_id (first_seen/last_seen soft-delete).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "integer", "description": "Node id"},
                "version_id": {"type": "integer", "description": "Target version_id"},
            },
            "required": ["node_id", "version_id"],
        },
        "handler": _tool_cgdb_time_travel_query,
    },
    "cgdb_list_versions": {
        "description": "List recent graph_versions rows (newest first).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max results (default 50)"},
            },
            "required": [],
        },
        "handler": _tool_cgdb_list_versions,
    },
}

# ============================================================================
# Merge in 28 design-report MCP tools (render_source / verify_consistency /
# edit_token / ... / commit_db_transaction / rollback_db_transaction /
# insert_node_after / delete_node / add_function).
# These implement design-report appendix B (28 tools: 8 L1 + 8 L2 + 7 L3 +
# 2 writeback + 3 advanced-edit). They are imported from mcp_report_tools
# so mcp_server.py stays under 2000 lines. Total tool count: 53 + 28 = 81.
# ============================================================================
try:
    from _builder.mcp_report_tools import TOOLS_REPORT
    TOOLS.update(TOOLS_REPORT)
except ImportError:
    # mcp_report_tools not available — log loudly so the user knows
    # the server is starting with 53 tools instead of the documented 81.
    logging.getLogger(__name__).error(
        "mcp_report_tools import failed — MCP server starting with "
        "%d tools (expected 81). Design-report tools unavailable.",
        len(TOOLS))


# ---------------------------------------------------------------------------
# MCP server main loop
# ---------------------------------------------------------------------------

def run_mcp_server(graph_dir: str):
    """Run MCP server over stdio transport."""
    # Token tracking
    mcp_stats = {"total_calls": 0, "total_output_tokens": 0, "by_tool": {}}

    def _write_mcp_stats():
        stats_path = os.path.join(graph_dir, ".code2database_mcp_stats.json")
        try:
            Path(stats_path).write_text(
                json.dumps(mcp_stats, indent=2) + "\n", encoding="utf-8")
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    atexit.register(_write_mcp_stats)

    # Initialization
    initialized = False

    while True:
        msg = _read_message()
        if msg is _EOF_SENTINEL:
            break  # stdin closed — clean exit
        if msg is None:
            continue  # malformed message — skip

        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        if method == "initialize":
            initialized = True
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": "Code2Database",
                    "version": "2.0.0",
                },
            }
            _write_message({"jsonrpc": "2.0", "id": msg_id, "result": result})

        elif method == "notifications/initialized":
            pass  # No response needed

        elif method == "tools/list":
            tools_list = []
            for name, tool_def in TOOLS.items():
                tools_list.append({
                    "name": name,
                    "description": tool_def["description"],
                    "inputSchema": tool_def["inputSchema"],
                })
            _write_message({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools_list}})

        elif method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})

            if tool_name not in TOOLS:
                _write_message({
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}
                })
                continue

            try:
                handler = TOOLS[tool_name]["handler"]
                result = handler(tool_args, graph_dir)
                # Track token consumption
                result_json = json.dumps(result, ensure_ascii=False, indent=2)
                tokens = estimate_tokens(result_json)
                mcp_stats["total_calls"] += 1
                mcp_stats["total_output_tokens"] += tokens
                mcp_stats["by_tool"].setdefault(tool_name, {"calls": 0, "tokens": 0})
                mcp_stats["by_tool"][tool_name]["calls"] += 1
                mcp_stats["by_tool"][tool_name]["tokens"] += tokens
                if isinstance(result, dict):
                    result["_token_count"] = tokens
                _write_message({
                    "jsonrpc": "2.0", "id": msg_id,
                    "result": {"content": [{"type": "text",
                                           "text": json.dumps(result, ensure_ascii=False, indent=2)}]}
                })
            except Exception as e:
                _write_message({
                    "jsonrpc": "2.0", "id": msg_id,
                    "result": {"content": [{"type": "text",
                                           "text": json.dumps({"error": str(e)})}],
                              "isError": True}
                })

        elif method == "ping":
            _write_message({"jsonrpc": "2.0", "id": msg_id, "result": {}})

        else:
            if msg_id is not None:
                _write_message({
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"}
                })


def cmd_serve(args):
    """Handle serve command — start MCP server.

    Supports SQLite-only builds (code2database.db without
    code2database_master.json). Large projects (>100K functions) must use
    --storage sqlite, and without this fallback the MCP server cannot
    start for them — breaking LLM agent integration.
    """
    graph_dir = args.graph
    has_master = os.path.exists(os.path.join(graph_dir, "code2database_master.json"))
    has_sqlite = os.path.exists(os.path.join(graph_dir, "code2database.db"))
    if not has_master and not has_sqlite:
        print(f"Error: No invocation graph found at {graph_dir} "
              f"(need code2database_master.json or code2database.db)", file=sys.stderr)
        sys.exit(1)
    run_mcp_server(graph_dir)
