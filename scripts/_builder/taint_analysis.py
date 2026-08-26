"""Taint analysis — source/sink/sanitizer framework on DATA_FLOW edges.

Inspired by joern and semgrep: declare sources, sinks, and sanitizers;
propagate taint through the existing DATA_FLOW edge graph.

Design:
- Sources: entry points that introduce untrusted data (e.g., parameters
  of API_entry functions, return values of recv/read functions)
- Sinks: dangerous operations (e.g., memcpy, free, system calls)
- Sanitizers: functions that clean taint (e.g., bounds_check, sanitize)
- Propagation: follow DATA_FLOW edges forward from sources; if a sanitizer
  is on the path, the taint is cleaned

Usage:
    taint-analysis --graph code2db-out/ --source recv --sink memcpy
    taint-analysis --graph code2db-out/ --source-pattern "recv.*" --sink-pattern "memcpy|strcpy"
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import deque, defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple


def taint_analysis(graph_dir: str, sources: List[str], sinks: List[str],
                  sanitizers: List[str] = None, max_depth: int = 10) -> Dict[str, Any]:
    """Run taint analysis from sources to sinks through DATA_FLOW edges.

    Args:
        graph_dir: C2D graph directory.
        sources: List of function names that are taint sources.
        sinks: List of function names that are taint sinks.
        sanitizers: List of function names that clean taint.
        max_depth: Max propagation depth.

    Returns:
        {sources, sinks, flows: [{source, sink, path, sanitized}]}
    """
    from _builder.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)
    sanitizers = sanitizers or []

    # Find source and sink node IDs
    source_nodes = set()
    sink_nodes = set()
    sanitizer_nodes = set()
    for nid, nd in G.nodes(data=True):
        name = nd.get("name", "")
        if any(name == s or fnmatch(name, s) for s in sources):
            source_nodes.add(nid)
        if any(name == s or fnmatch(name, s) for s in sinks):
            sink_nodes.add(nid)
        if any(name == s or fnmatch(name, s) for s in sanitizers):
            sanitizer_nodes.add(nid)

    # BFS from each source through DATA_FLOW + INVOKES edges
    flows = []
    for source_id in source_nodes:
        # Track visited nodes and paths
        visited = {source_id}
        queue = deque([(source_id, [source_id], False)])  # (node, path, is_sanitized)

        while queue:
            node, path, is_sanitized = queue.popleft()
            if len(path) > max_depth:
                continue

            if node in sink_nodes and node != source_id:
                flows.append({
                    "source": G.nodes[source_id].get("name", source_id),
                    "source_id": source_id,
                    "sink": G.nodes[node].get("name", node),
                    "sink_id": node,
                    "path": [G.nodes[n].get("name", n) for n in path],
                    "path_ids": path,
                    "sanitized": is_sanitized,
                    "depth": len(path) - 1,
                })
                continue

            # Follow DATA_FLOW edges (forward = value flows out)
            for succ in G.successors(node):
                ed = G.get_edge_data(node, succ) or {}
                rel = ed.get("relation", "")
                if rel not in ("DATA_FLOW", "DATA_DEP", "INVOKES", "FFI"):
                    continue
                if succ in visited:
                    continue
                visited.add(succ)
                new_sanitized = is_sanitized or (succ in sanitizer_nodes)
                queue.append((succ, path + [succ], new_sanitized))

    return {
        "sources": [G.nodes[s].get("name", s) for s in source_nodes],
        "sinks": [G.nodes[s].get("name", s) for s in sink_nodes],
        "sanitizers": [G.nodes[s].get("name", s) for s in sanitizer_nodes],
        "flows": flows,
        "total_flows": len(flows),
        "unsanitized_flows": sum(1 for f in flows if not f["sanitized"]),
        "sanitized_flows": sum(1 for f in flows if f["sanitized"]),
    }


def fnmatch(name: str, pattern: str) -> bool:
    """Simple wildcard matching."""
    try:
        return bool(re.fullmatch(pattern.replace("*", ".*").replace("?", "."), name))
    except re.error:
        return name == pattern


def cmd_taint_analysis(args):
    """CLI handler: taint-analysis."""
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    sinks = [s.strip() for s in args.sinks.split(",") if s.strip()]
    sanitizers = []
    if getattr(args, "sanitizers", ""):
        sanitizers = [s.strip() for s in args.sanitizers.split(",") if s.strip()]
    result = taint_analysis(
        args.graph, sources, sinks, sanitizers,
        max_depth=getattr(args, "max_depth", 10),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
