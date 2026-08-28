"""Code slicing — minimal context extraction via data-flow traversal.

Inspired by joern's joern-slice: produces a minimal set of nodes/edges
that are relevant to a given function or variable, for LLM context.

Two slice types:
- Data-flow slice: all nodes on DATA_FLOW paths to/from a given sink
- Usage slice: all nodes that call or are called by a given function
"""
from __future__ import annotations

import json
from collections import deque
from typing import Any, Dict


def data_flow_slice(graph_dir: str, sink: str, max_depth: int = 8,
                    max_nodes: int = 50) -> Dict[str, Any]:
    """Extract a minimal data-flow slice ending at a sink function.

    Traverses DATA_FLOW edges backwards from the sink, collecting
    all nodes that contribute values to it. This is the minimal context
    needed to understand "how does the data reaching this function get
    constructed?"

    Returns {sink, nodes: [...], edges: [...], stats}
    """
    from _builder.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    G = _load_full_graph(graph_dir)
    node_id = _find_node_id(G, sink)
    if not node_id:
        return {"error": f"sink '{sink}' not found"}

    visited = {node_id}
    nodes = [{"id": node_id, "name": G.nodes[node_id].get("name", node_id),
              "depth": 0}]
    edges = []
    queue = deque([(node_id, 0)])

    while queue and len(nodes) < max_nodes:
        cur, depth = queue.popleft()
        if depth >= max_depth:
            continue
        # Backward DATA_FLOW edges (who feeds data into cur?)
        for pred in G.predecessors(cur):
            ed = G.get_edge_data(pred, cur) or {}
            if ed.get("relation") not in ("DATA_FLOW", "DATA_DEP"):
                continue
            if pred not in visited:
                visited.add(pred)
                nd = G.nodes[pred]
                nodes.append({"id": pred, "name": nd.get("name", pred),
                              "depth": depth + 1,
                              "source_file": nd.get("source_file", "")})
                queue.append((pred, depth + 1))
            edges.append({"source": pred, "target": cur,
                         "relation": ed.get("relation"),
                         "kind": "data_flow"})

    return {"sink": sink, "sink_id": node_id,
            "nodes": nodes, "edges": edges,
            "stats": {"node_count": len(nodes), "edge_count": len(edges),
                       "max_depth": max_depth}}


def usage_slice(graph_dir: str, function: str, max_depth: int = 3,
                max_nodes: int = 30) -> Dict[str, Any]:
    """Extract a usage slice: who calls this function and what it calls.

    Returns the function's caller/callee neighborhood for "how is this
    function used in the codebase?"
    """
    from _builder.graph_build import _load_full_graph
    from _builder.utils import _find_node_id
    G = _load_full_graph(graph_dir)
    node_id = _find_node_id(G, function)
    if not node_id:
        return {"error": f"function '{function}' not found"}

    nodes = [{"id": node_id, "name": G.nodes[node_id].get("name", node_id),
              "depth": 0, "kind": "self"}]
    edges = []
    visited = {node_id}
    queue = deque([(node_id, 0)])

    while queue and len(nodes) < max_nodes:
        cur, depth = queue.popleft()
        if depth >= max_depth:
            continue
        # Callees (what this calls)
        for succ in G.successors(cur):
            ed = G.get_edge_data(cur, succ) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            if succ not in visited:
                visited.add(succ)
                nodes.append({"id": succ, "name": G.nodes[succ].get("name", succ),
                              "depth": depth + 1, "kind": "callee"})
                queue.append((succ, depth + 1))
            edges.append({"source": cur, "target": succ,
                         "relation": ed.get("relation", "INVOKES"),
                         "kind": "call"})
        # Callers (who calls this)
        for pred in G.predecessors(cur):
            ed = G.get_edge_data(pred, cur) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            if pred not in visited:
                visited.add(pred)
                nodes.append({"id": pred, "name": G.nodes[pred].get("name", pred),
                              "depth": depth + 1, "kind": "caller"})
                queue.append((pred, depth + 1))
            edges.append({"source": pred, "target": cur,
                         "relation": ed.get("relation", "INVOKES"),
                         "kind": "call"})

    return {"function": function, "function_id": node_id,
            "nodes": nodes, "edges": edges,
            "stats": {"node_count": len(nodes), "edge_count": len(edges),
                       "max_depth": max_depth}}


def cmd_code_slice(args):
    """CLI handler: code-slice."""
    slice_type = getattr(args, "type", "data-flow")
    if slice_type == "data-flow":
        result = data_flow_slice(args.graph, args.node,
                                 max_depth=getattr(args, "max_depth", 8),
                                 max_nodes=getattr(args, "max_nodes", 50))
    elif slice_type == "usage":
        result = usage_slice(args.graph, args.node,
                             max_depth=getattr(args, "max_depth", 3),
                             max_nodes=getattr(args, "max_nodes", 30))
    else:
        result = {"error": f"unknown slice type: {slice_type}"}
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
