"""Code intelligence tools — references-of, traverse-graph, hub-nodes, bridge-nodes.

These tools fill the most critical gaps identified by comparing Code2Database
with 6 high-star open-source projects (Sourcail, code-review-graph,
SocratiCode, emerge, codecharta, codeseek).
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import deque, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from _builder.utils import _find_node_id, _get_body_text


def references_of(graph_dir: str, symbol: str, limit: int = 100) -> Dict[str, Any]:
    """Return ALL source locations where a symbol is referenced.

    Groups results by file with file:line:col and access kind
    (read/write/call/decl). This is the single most-requested feature
    from Sourcetrail's reference-iteration UX.

    Returns:
        {symbol, total, by_file: [{file, locations: [{line, kind}]}], summary}
    """
    from _builder.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)
    node_id = _find_node_id(G, symbol)
    if not node_id:
        return {"error": f"symbol '{symbol}' not found"}

    nd = G.nodes[node_id]
    locations = []
    # 1. Declaration location
    sf = nd.get("source_file", "")
    ln = nd.get("line", 0)
    if sf and ln:
        locations.append({"file": sf, "line": ln, "kind": "declaration"})

    # 2. All callers (incoming call edges)
    for pred in G.predecessors(node_id):
        ed = G.get_edge_data(pred, node_id) or {}
        if ed.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        pred_nd = G.nodes[pred]
        pred_file = pred_nd.get("source_file", "")
        pred_line = pred_nd.get("line", 0)
        # Try to find the actual call site line from callee_args
        call_line = pred_line
        for ca in pred_nd.get("callee_args", []):
            if ca.get("callee_id") == node_id or ca.get("callee_name") == symbol:
                call_line = ca.get("call_line") or ca.get("line") or pred_line
                break
        locations.append({
            "file": pred_file, "line": call_line,
            "kind": "call",
            "caller": pred_nd.get("name", pred),
            "condition": ed.get("call_condition", ""),
            "confidence": ed.get("confidence", "EXTRACTED"),
        })

    # 3. Field/global access sites (if the symbol is a field or global)
    for nid, ndata in G.nodes(data=True):
        if nid == node_id or ndata.get("is_empty", False):
            continue
        for fw in (ndata.get("fields_written") or []):
            if fw.get("field_name", "") == symbol or symbol in fw.get("struct_chain", ""):
                locations.append({
                    "file": ndata.get("source_file", ""),
                    "line": ndata.get("line", 0),
                    "kind": "write",
                    "function": ndata.get("name", nid),
                    "assigned_value": fw.get("assigned_value", ""),
                })
        for fr in (ndata.get("fields_read") or []):
            if fr.get("field_name", "") == symbol or symbol in fr.get("struct_chain", ""):
                locations.append({
                    "file": ndata.get("source_file", ""),
                    "line": ndata.get("line", 0),
                    "kind": "read",
                    "function": ndata.get("name", nid),
                })

    # Group by file
    by_file = defaultdict(list)
    for loc in locations[:limit]:
        by_file[loc["file"]].append(loc)
    files = []
    for fname, locs in sorted(by_file.items()):
        files.append({"file": fname, "count": len(locs), "locations": locs})

    return {
        "symbol": symbol,
        "node_id": node_id,
        "total": len(locations),
        "by_file": files,
        "summary": {
            "declaration": sum(1 for l in locations if l["kind"] == "declaration"),
            "calls": sum(1 for l in locations if l["kind"] == "call"),
            "reads": sum(1 for l in locations if l["kind"] == "read"),
            "writes": sum(1 for l in locations if l["kind"] == "write"),
        },
    }


def traverse_graph(graph_dir: str, start: str, mode: str = "bfs",
                   max_depth: int = 5, max_nodes: int = 100,
                   max_tokens: int = 4000) -> Dict[str, Any]:
    """Free-form BFS/DFS traversal from any node with depth + node budget.

    This fills the gap from code-review-graph's traverse_graph_tool.
    """
    from _builder.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)
    node_id = _find_node_id(G, start)
    if not node_id:
        return {"error": f"node '{start}' not found"}

    visited = {node_id}
    nodes = []
    edges = []
    if mode == "bfs":
        queue = deque([(node_id, 0)])
        while queue and len(nodes) < max_nodes:
            cur, depth = queue.popleft()
            if depth >= max_depth:
                continue
            nd = G.nodes[cur]
            nodes.append({
                "id": cur, "name": nd.get("name", cur),
                "domain": nd.get("domain", ""),
                "depth": depth,
                "labels": nd.get("labels", []),
            })
            for succ in G.successors(cur):
                ed = G.get_edge_data(cur, succ) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                edges.append({
                    "source": cur, "target": succ,
                    "relation": ed.get("relation", "INVOKES"),
                    "condition": ed.get("call_condition", ""),
                })
                if succ not in visited:
                    visited.add(succ)
                    queue.append((succ, depth + 1))
            for pred in G.predecessors(cur):
                ed = G.get_edge_data(pred, cur) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                edges.append({
                    "source": pred, "target": cur,
                    "relation": ed.get("relation", "INVOKES"),
                    "condition": ed.get("call_condition", ""),
                })
                if pred not in visited:
                    visited.add(pred)
                    queue.append((pred, depth + 1))
    else:  # dfs
        stack = [(node_id, 0)]
        while stack and len(nodes) < max_nodes:
            cur, depth = stack.pop()
            if depth >= max_depth:
                continue
            nd = G.nodes[cur]
            nodes.append({
                "id": cur, "name": nd.get("name", cur),
                "domain": nd.get("domain", ""),
                "depth": depth,
                "labels": nd.get("labels", []),
            })
            for succ in list(G.successors(cur)):
                ed = G.get_edge_data(cur, succ) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                edges.append({
                    "source": cur, "target": succ,
                    "relation": ed.get("relation", "INVOKES"),
                    "condition": ed.get("call_condition", ""),
                })
                if succ not in visited:
                    visited.add(succ)
                    stack.append((succ, depth + 1))

    # Token budget: truncate body if needed
    max_chars = max_tokens * 4
    total_chars = sum(len(n.get("name", "")) for n in nodes) + sum(len(e.get("condition", "")) for e in edges)
    truncated = total_chars > max_chars
    if truncated:
        while nodes and total_chars > max_chars:
            n = nodes.pop()
            total_chars -= len(n.get("name", ""))

    return {
        "start": node_id,
        "mode": mode,
        "max_depth": max_depth,
        "nodes": nodes,
        "edges": edges,
        "truncated": truncated or len(nodes) >= max_nodes,
        "total_nodes": len(nodes),
        "total_edges": len(edges),
    }


def hub_nodes(graph_dir: str, top_n: int = 20) -> List[Dict[str, Any]]:
    """Return the most connected nodes (highest in+out degree).

    Fills the hub-detection gap from code-review-graph.
    """
    from _builder.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)
    degrees = []
    for nid, nd in G.nodes(data=True):
        if nd.get("is_empty", False) or nd.get("node_type") == "file":
            continue
        in_deg = sum(1 for p in G.predecessors(nid)
                     if (G.get_edge_data(p, nid) or {}).get("relation") not in ("CONTAINS", "IMPORTS"))
        out_deg = sum(1 for s in G.successors(nid)
                      if (G.get_edge_data(nid, s) or {}).get("relation") not in ("CONTAINS", "IMPORTS"))
        total = in_deg + out_deg
        if total > 0:
            degrees.append({
                "id": nid, "name": nd.get("name", nid),
                "domain": nd.get("domain", ""),
                "in_degree": in_deg, "out_degree": out_deg,
                "total_degree": total,
                "labels": nd.get("labels", []),
            })
    degrees.sort(key=lambda x: -x["total_degree"])
    return degrees[:top_n]


def bridge_nodes(graph_dir: str, top_n: int = 20) -> List[Dict[str, Any]]:
    """Return bridge nodes (high betweenness centrality — chokepoints).

    Fills the bridge-detection gap from code-review-graph.
    Uses networkx betweenness_centrality if available.
    """
    from _builder.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)
    # Filter to call edges only
    call_nodes = set()
    for nid, nd in G.nodes(data=True):
        if nd.get("is_empty", False) or nd.get("node_type") == "file":
            continue
        call_nodes.add(nid)
    sub = G.subgraph(call_nodes).copy()
    # Remove non-call edges
    to_remove = [(u, v) for u, v, d in sub.edges(data=True)
                 if d.get("relation") in ("CONTAINS", "IMPORTS")]
    sub.remove_edges_from(to_remove)

    try:
        import networkx as nx
        bc = nx.betweenness_centrality(sub, k=min(500, len(sub)), normalized=True)
    except (ImportError, Exception):
        # Fallback: use degree as proxy
        bc = {n: sub.degree(n) for n in sub}

    results = []
    for nid, score in sorted(bc.items(), key=lambda x: -x[1])[:top_n]:
        nd = G.nodes[nid]
        results.append({
            "id": nid, "name": nd.get("name", nid),
            "domain": nd.get("domain", ""),
            "betweenness": round(score, 6),
            "labels": nd.get("labels", []),
        })
    return results


# --- CLI handlers ---

def cmd_references_of(args):
    """CLI handler: references-of."""
    result = references_of(args.graph, args.node, limit=getattr(args, "limit", 100))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def cmd_traverse_graph(args):
    """CLI handler: traverse-graph."""
    result = traverse_graph(
        args.graph, args.start,
        mode=getattr(args, "mode", "bfs"),
        max_depth=getattr(args, "max_depth", 5),
        max_nodes=getattr(args, "max_nodes", 100),
        max_tokens=getattr(args, "max_tokens", 4000),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def cmd_hub_nodes(args):
    """CLI handler: hub-nodes."""
    result = hub_nodes(args.graph, top_n=getattr(args, "top", 20))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def cmd_bridge_nodes(args):
    """CLI handler: bridge-nodes."""
    result = bridge_nodes(args.graph, top_n=getattr(args, "top", 20))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
