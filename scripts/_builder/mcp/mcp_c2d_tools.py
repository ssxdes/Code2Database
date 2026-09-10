"""mcp_server.mcp_c2d_tools — split from mcp_server.py.

36 code2database_* MCP tool handler functions.
"""

import json
import os
import logging
from pathlib import Path
from _builder.token_budget import estimate_tokens
from _builder.mcp.mcp_cache import _get_graph, _mcp_coerce_str, _mcp_coerce_int, mcp_read_only
from _builder.utils import resolve_source_file as _resolve_source_file


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
    from _builder.graph.graph_build import _load_full_graph
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
    keywords = _mcp_coerce_str(args.get("keywords", ""))
    top = _mcp_coerce_int(args.get("top", 20), 20, 1, 500)
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
        except sqlite3.Error as exc:
            # WARNING, not debug: a persistent SQL error (corrupt db,
            # schema drift) silently rerouted every search through the
            # full networkx graph load — slow and unexplained.
            logging.getLogger(__name__).warning(
                "_tool_search SQL path failed, falling back to networkx: %s",
                exc)
        finally:
            if conn is not None:
                conn.close()
    from _builder.graph.graph_build import _load_full_graph
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
    from _builder.graph.graph_build import _load_full_graph
    from _builder.query.explore import _tokenize_query, _find_relevant_nodes, \
        _score_node_relevance, _extract_subgraph_context, _extract_key_paths, \
        _generate_exploration_summary, _derive_exec_summary
    from _builder.utils import _find_node_id
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    query = args.get("query", "")
    max_nodes = _mcp_coerce_int(args.get("max_nodes", 15), 15, 1, 500)
    max_tokens = _mcp_coerce_int(args.get("max_tokens", 2000), 2000, 100, 100000)
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
    from _builder.graph.graph_build import _load_full_graph
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
    from _builder.graph.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    node_id = _find_node_id(G, _mcp_coerce_str(args.get("node", "")))
    if not node_id:
        return {"error": f"Node not found: {_mcp_coerce_str(args.get('node', ''))}"}
    direction = _mcp_coerce_str(args.get("direction", "reverse"))
    # cap depth — depth=1000000 on a 1.5M-node graph
    # would block the dispatch thread indefinitely.
    depth = _mcp_coerce_int(args.get("depth", 3), 3, 1, 20)
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
    from _builder.profile.key_paths import _compute_entry_scores, _find_endpoints, _extract_key_paths_from_entries
    from _builder.graph.graph_build import _load_full_graph
    G = _get_graph(graph_dir)
    if not G:
        return []
    top = _mcp_coerce_int(args.get("top", 5), 5, 1, 100)
    from_entry = _mcp_coerce_str(args.get("from_entry"))

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
    from _builder.graph.graph_build import _load_full_graph
    G = _get_graph(graph_dir)
    if not G:
        return []
    top = _mcp_coerce_int(args.get("top", 50), 50, 1, 500)
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
    from _builder.graph.graph_build import _load_full_graph
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
    from _builder.graph.graph_build import _load_full_graph
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
    """Query knowledge (project brief) by topic.

    Prefers the unified FTS5+BM25 path (kb_paragraphs, indexed from
    brief.json sections) when the project has code2database.db; falls
    back to substring matching against the rendered brief otherwise.
    """
    topic = args.get("topic", "")
    if not topic:
        return {"error": "Missing required parameter: topic"}
    try:
        from _builder.kb.kb_index import query_kb
        results = query_kb(
            graph_dir=graph_dir,
            query=topic,
            top_n=10,
            kinds=["hard_rule", "mode", "abstraction", "description",
                   "must_know", "conventions", "pitfalls",
                   "query_paths"],
            min_weight=0.0,
            max_tokens=_mcp_coerce_int(args.get("max_tokens", 500), 500, 50, 50000),
            update_access=not mcp_read_only(),
            log_query=not mcp_read_only(),
        )
        if results:
            return {
                "topic": args.get("topic", ""),
                "matches": results,
                "engine": "fts5_bm25",
            }
        # No FTS5 hits — fall through to brief fallback
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    from _builder.kb.brief import load_brief, render_brief_prompt
    brief = load_brief(graph_dir)
    if brief is None:
        return {"topic": topic, "result": "No project brief found. "
                "Run brief-extract to initialize.",
                "engine": "brief_fallback"}
    rendered = render_brief_prompt(graph_dir, brief)
    topic_lower = topic.lower()
    matching = [line for line in rendered.split("\n")
                if topic_lower in line.lower()]
    if matching:
        return {"topic": topic, "result": "\n".join(matching[:20]),
                "engine": "brief_fallback"}
    # No line matched — return the full (lean) brief; it IS the
    # knowledge of this project.
    return {"topic": topic, "result": rendered,
            "engine": "brief_fallback"}



def _tool_memory_search(args: dict, graph_dir: str) -> list:
    """Search memory for similar questions.

    Prefers the unified FTS5+BM25 path (kb_paragraphs) when the
    project has code2database.db; falls back to the legacy Jaccard
    search via MemoryManager otherwise. A symbol filter bypasses the
    KB path (kb_paragraphs doesn't carry symbol grounding) and goes
    straight to the memory store's exact symbol matching.
    """
    query = args.get("query", "")
    symbol = args.get("symbol", "")
    if not query and not symbol:
        return [{"error": "Missing required parameter: query"}]
    if symbol:
        try:
            from _builder.memory.memory_store import MemoryStore
            store = MemoryStore(graph_dir)
            return store.search(query or symbol, top_n=_mcp_coerce_int(args.get("top", 5), 5, 1, 100),
                                symbol=symbol)
        except Exception:
            logging.getLogger(__name__).debug("silent exception",
                                              exc_info=True)
            return [{"error": f"symbol search failed for {symbol!r}"}]
    if not query:
        return [{"error": "Missing required parameter: query"}]
    try:
        from _builder.kb.kb_index import query_kb
        results = query_kb(
            graph_dir=graph_dir,
            query=query,
            top_n=_mcp_coerce_int(args.get("top", 5), 5, 1, 100),
            kinds=["memory_qa", "memory_experience"],
            min_weight=0.0,  # no weight filter; let BM25 rank
            max_tokens=4000,
            update_access=not mcp_read_only(),
            log_query=not mcp_read_only(),
        )
        if results:
            return results
        # Fall through to legacy
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    from _builder.memory.memory_manager import MemoryManager
    mm = MemoryManager(graph_dir)
    return mm.query(args.get("query", ""), top_n=_mcp_coerce_int(args.get("top", 5), 5, 1, 100))



def _tool_kb_query(args: dict, graph_dir: str) -> dict:
    """Unified FTS5+BM25 query across memory + knowledge.

    Replaces the need to call
    code2database_memory_search AND code2database_knowledge_query
    separately — this single tool searches both stores via the
    shared kb_paragraphs_fts index.
    """
    from _builder.kb.kb_index import query_kb
    query = _mcp_coerce_str(args.get("query", ""))
    if not query:
        return {"error": "query is required"}
    kinds_str = _mcp_coerce_str(args.get("kinds", ""))
    kinds = [k.strip() for k in kinds_str.split(",") if k.strip()] if kinds_str else None
    # wrap query_kb() in try/except and use
    # _mcp_coerce_float for min_weight — bare float() crashed on a
    # non-numeric string from a client. Mirrors _tool_knowledge_query
    # which already had this protection.
    try:
        results = query_kb(
            graph_dir=graph_dir,
            query=query,
            top_n=_mcp_coerce_int(args.get("top", 10), 10, 1, 100),
            kinds=kinds,
            min_weight=_mcp_coerce_float(args.get("min_weight", 0.0), 0.0, 0.0, 1.0),
            max_tokens=_mcp_coerce_int(args.get("max_tokens", 4000), 4000, 100, 100000),
            update_access=not mcp_read_only(),
            log_query=not mcp_read_only(),
        )
    except Exception as exc:
        return {"error": str(exc)}
    return {
        "query": query,
        "kinds": kinds,
        "total": len(results),
        "results": results,
        "engine": "fts5_bm25",
    }



def _tool_save_memory(args: dict, graph_dir: str) -> dict:
    """Save (or correct) a Q&A memory — the accumulation half of the
    memory protocol.

    MCP-side counterpart of the save-memory CLI: without it, agents
    connected via MCP could search veteran Q&A but never add to it.
    correct=True takes the correction path (reshape the most similar
    entry in place instead of adding a variant of a wrong answer).
    """
    try:
        question = _mcp_coerce_str(args.get("question")).strip()
        answer = _mcp_coerce_str(args.get("answer"))
        author = _mcp_coerce_str(args.get("author"))
        category = _mcp_coerce_str(args.get("category"))
        raw_tags = args.get("tags", "")
        if isinstance(raw_tags, list):
            tags = [_mcp_coerce_str(t).strip() for t in raw_tags
                    if _mcp_coerce_str(t).strip()]
        else:
            _t = _mcp_coerce_str(raw_tags)
            tags = [t.strip() for t in _t.split(",") if t.strip()]
        sym_arg = args.get("symbol", "")
        if isinstance(sym_arg, list):
            symbols = [_mcp_coerce_str(s).strip() for s in sym_arg
                       if _mcp_coerce_str(s).strip()]
        else:
            _s = _mcp_coerce_str(sym_arg)
            symbols = [s.strip() for s in _s.split(",") if s.strip()]
    except ValueError as exc:
        return {"error": f"invalid argument: {exc}"}
    if not question:
        return {"error": "question is required"}
    from _builder.memory.memory_store import MemoryStore
    store = MemoryStore(graph_dir)
    if args.get("correct"):
        result = store.correct_similar(
            question=question, answer=answer, author=author,
            symbols=symbols or None)
    else:
        new_id = store.add(
            question=question, answer=answer, tags=tags,
            category=category or None, author=author,
            symbols=symbols)
        result = {"action": "created", "id": new_id}
    result["question"] = question
    return result



def _tool_session_init(args: dict, graph_dir: str) -> dict:
    """One-shot session context: brief + memory digest + graph +
    known-unknowns.

    The session-start entry for agents: instead of guessing which
    tools to call first, one call returns the project brief (mandatory
    rules/modes/pitfalls), the memory digest (veteran Q&A ranked by
    weight), graph state with brief-drift warning, and known-unknowns
    (repeatedly unanswered queries that should be captured into
    memory). Every layer degrades to a hint when absent — never raises.
    """
    from _builder.mcp.session_init import build_session_context, \
        render_session_context
    ctx = build_session_context(
        graph_dir, memory_top=_mcp_coerce_int(args.get("top", 10), 10, 1, 50))
    # "rendered" is the prompt-ready text form (same as the session-init
    # CLI prints); the structured fields alongside it let agents follow
    # up programmatically (e.g. memory_digest ids, known_unknowns queries)
    ctx["rendered"] = render_session_context(ctx)
    return ctx



def _tool_semantic_status(args: dict, graph_dir: str) -> dict:
    """Check if semantic update is recommended."""
    from _builder.build.changelog_update import get_semantic_update_status
    return get_semantic_update_status(graph_dir)



def _tool_foreign_refs(args: dict, graph_dir: str) -> dict:
    """List cross-C2D foreign refs for a node."""
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
    """Trigger sync of foreign_refs."""
    from _builder.scanner_bridge.c2d_foreign import sync_foreign
    return sync_foreign(
        graph_dir,
        foreign_c2d_path=args.get("foreign_c2d", "") or "",
        verbose=False,
    )



def _tool_composite_query(args: dict, graph_dir: str) -> dict:
    """Cross-C2D query via ATTACH."""
    from _builder.scanner_bridge.c2d_phase2 import composite_query
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
        top_n=_mcp_coerce_int(args.get("top", 50), 50, 1, 200),
    )



def _tool_get_code_snippet(args: dict, graph_dir: str) -> dict:
    """Get source code snippet for a node."""
    from _builder.graph.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    node_id = _find_node_id(G, _mcp_coerce_str(args.get("node", "")))
    if not node_id:
        return {"error": f"Node not found: {_mcp_coerce_str(args.get('node', ''))}"}
    nd = G.nodes[node_id]
    source_file = nd.get("source_file", "")
    line_num = nd.get("line", 0)
    # cap context lines — context=1000000 would dump
    # the entire file.
    context = _mcp_coerce_int(args.get("context", 10), 10, 0, 500)
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
    from _builder.graph.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    node_id = _find_node_id(G, _mcp_coerce_str(args.get("node", "")))
    if not node_id:
        return {"error": f"Node not found: {_mcp_coerce_str(args.get('node', ''))}"}
    # cap depth — depth=1000000 would block dispatch thread.
    depth = _mcp_coerce_int(args.get("depth", 3), 3, 1, 20)
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
    from _builder.graph.graph_build import _load_full_graph
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
    from _builder.graph.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    from _builder.analysis.path_feasibility import check_path_feasibility
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
    from _builder.graph.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    from _builder.analysis.invariants import extract_invariants_for_node
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
    from _builder.misc.ffi_bridge import trace_ffi
    from _builder.graph.graph_build import _load_full_graph
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
    from _builder.misc.doc_code_align import check_doc_code_alignment
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
    from _builder.graph.graph_build import _load_full_graph
    from _builder.graph.semantic_edges import who_allocates
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    resource = args.get("resource", "")
    results = who_allocates(G, resource)
    return {"count": len(results), "functions": results}



def _tool_who_frees(args: dict, graph_dir: str) -> dict:
    """Find functions that free a resource."""
    from _builder.graph.graph_build import _load_full_graph
    from _builder.graph.semantic_edges import who_frees
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    resource = args.get("resource", "")
    results = who_frees(G, resource)
    return {"count": len(results), "functions": results}



def _tool_who_locks(args: dict, graph_dir: str) -> dict:
    """Find functions that acquire a lock."""
    from _builder.graph.graph_build import _load_full_graph
    from _builder.graph.semantic_edges import who_locks
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    lock_name = args.get("lock", "")
    results = who_locks(G, lock_name)
    return {"count": len(results), "functions": results}



def _tool_explain_label(args: dict, graph_dir: str) -> dict:
    """Explain why a node has a given label."""
    from _builder.graph.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    from _builder.misc.explain import explain_label
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
    from _builder.graph.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    from _builder.misc.explain import why_ambiguous
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
    from _builder.ops.audit_log import query_audit_log
    return query_audit_log(
        graph_dir,
        target_id=args.get("node"),
        command=args.get("command"),
        tx_id=args.get("tx"),
        limit=_mcp_coerce_int(args.get("limit", 100), 100, 1, 1000),
    )



def _tool_happens_before(args: dict, graph_dir: str) -> dict:
    """Check happens-before between a writer and reader via locks/RCU/barriers."""
    from _builder.graph.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    from _builder.memory.memory_ordering import happens_before_analysis
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
    from _builder.graph.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    from _builder.memory.memory_ordering import analyze_memory_ordering
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
    from _builder.graph.graph_build import _load_full_graph
    from _builder.graph.semantic_edges import unbalanced_alloc_free
    G = _get_graph(graph_dir)
    if not G:
        return {"error": "Graph not loaded"}
    return unbalanced_alloc_free(G)

