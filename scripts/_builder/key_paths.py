"""callgraph builder module: key-paths — automatic key execution path extraction.

Finds the most important execution paths in the graph based on
entry point scores and betweenness centrality. No manual --from/--to needed.
"""

import json
import os
from pathlib import Path
import networkx as nx
from _builder.graph_build import _load_full_graph
from _builder.token_budget import estimate_tokens


def _compute_entry_scores(G: nx.DiGraph) -> dict:
    """Quick entry point scoring without requiring pre-computed index."""
    scores = {}
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        if "file" in ndata.get("labels", []):
            continue
        labels = ndata.get("labels", [])
        score = 0.0
        if "API_entry" in labels:
            score += 5.0
        if "thread_processor" in labels:
            score += 3.0
        if "callback_func" in labels:
            score += 2.5
        if "constructor" in labels:
            score += 2.0
        # Low in-degree + non-zero out-degree suggests entry
        in_deg = sum(1 for pred in G.predecessors(nid)
                     if (G.get_edge_data(pred, nid) or {}).get("relation") not in ("CONTAINS", "IMPORTS"))
        out_deg = sum(1 for succ in G.successors(nid)
                      if (G.get_edge_data(nid, succ) or {}).get("relation") not in ("CONTAINS", "IMPORTS"))
        if in_deg == 0 and out_deg > 0:
            score += 3.0
        elif in_deg <= 1 and out_deg > 2:
            score += 1.5
        # Betweenness approximation
        if out_deg > 3:
            score += 1.0
        if score > 0:
            scores[nid] = score
    return scores


def _find_endpoints(G: nx.DiGraph) -> list:
    """Find endpoint nodes (out_end, unknown_end, or terminal nodes)."""
    endpoints = []
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        labels = ndata.get("labels", [])
        # Skip file nodes
        if "file" in labels or ndata.get("node_type") == "file":
            continue
        if "out_end" in labels or "unknown_end" in labels:
            endpoints.append(nid)
        elif "destructor" in labels:
            endpoints.append(nid)
        else:
            # Terminal node with no call successors (excluding CONTAINS/IMPORTS)
            call_out = sum(1 for succ in G.successors(nid)
                          if (G.get_edge_data(nid, succ) or {}).get("relation") not in ("CONTAINS", "IMPORTS"))
            call_in = sum(1 for pred in G.predecessors(nid)
                         if (G.get_edge_data(pred, nid) or {}).get("relation") not in ("CONTAINS", "IMPORTS"))
            if call_out == 0 and call_in > 0:
                endpoints.append(nid)
    return endpoints


def _extract_key_paths_from_entries(G: nx.DiGraph, entries: dict,
                                     endpoints: list, top_n: int = 5) -> list:
    """Extract key execution paths from top entry points to endpoints."""
    # Sort entries by score
    sorted_entries = sorted(entries.items(), key=lambda x: -x[1])
    paths = []
    seen_paths = set()  # Deduplicate by endpoint

    # Build endpoint set for fast lookup
    endpoint_set = set(endpoints)

    # Build a call-only subgraph for pathfinding (exclude CONTAINS/IMPORTS)
    from _builder.utils import _make_call_graph
    call_G = _make_call_graph(G)

    for entry_id, score in sorted_entries[:50]:
        if len(paths) >= top_n:
            break

        # Single BFS from this entry: nx.predecessor returns the BFS parent
        # map for all reachable nodes in O(V+E). We then reconstruct paths
        # to reachable endpoints by walking the parent pointers — much
        # cheaper than calling nx.shortest_path once per endpoint, which
        # on kernel-sized graphs hangs for hours.
        try:
            pred_map = nx.predecessor(call_G, entry_id, cutoff=24)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue
        if len(pred_map) <= 1:
            continue
        desc = set(pred_map.keys())

        # Find reachable endpoints among descendants
        reachable_ends = [d for d in desc if d in endpoint_set]
        # Also include terminal nodes (out_degree=0) as fallback endpoints
        if not reachable_ends:
            reachable_ends = [d for d in desc if call_G.out_degree(d) == 0]
        if not reachable_ends:
            continue

        def _reconstruct(target):
            if target not in pred_map or target == entry_id:
                return None
            path = [target]
            cur = target
            while cur != entry_id:
                preds = pred_map.get(cur)
                if not preds:
                    return None
                cur = preds[0]
                path.append(cur)
            path.reverse()
            return path

        # Try to find paths to reachable endpoints
        best_path = None
        best_path_score = 0
        for end_id in reachable_ends[:20]:
            if end_id in seen_paths:
                continue
            path = _reconstruct(end_id)
            if path is None:
                continue
            # Score: longer paths with more branches score higher
            path_score = score * len(path)
            # Check for interesting features
            has_concurrency = False
            has_conditions = False
            has_cross_domain = False
            prev_domain = None
            for i, nid in enumerate(path):
                nd = G.nodes[nid]
                cur_domain = nd.get("domain", "")
                if prev_domain and cur_domain != prev_domain:
                    has_cross_domain = True
                prev_domain = cur_domain
                # Check edges for concurrency and conditions
                if i > 0:
                    ed = G.get_edge_data(path[i-1], nid) or {}
                    if ed.get("concurrency") in ("spawn_target", "thread_spawn", "goroutine", "callback"):
                        has_concurrency = True
                    if ed.get("call_condition"):
                        has_conditions = True
            if has_concurrency:
                path_score *= 1.5
            if has_conditions:
                path_score *= 1.2
            if has_cross_domain:
                path_score *= 1.3

            if path_score > best_path_score:
                best_path_score = path_score
                best_path = path

        if best_path:
            # Annotate the path
            annotated_steps = []
            for i, nid in enumerate(best_path):
                nd = G.nodes[nid]
                step = {
                    "name": nd.get("name", ""),
                    "domain": nd.get("domain", ""),
                    "location": f"{nd.get('source_file', '')}:{nd.get('line', 0)}",
                    "labels": nd.get("labels", []),
                }
                if i > 0:
                    ed = G.get_edge_data(best_path[i-1], nid) or {}
                    step["call_condition"] = ed.get("call_condition", "")
                    step["concurrency"] = ed.get("concurrency", "")
                    step["confidence"] = ed.get("confidence", "EXTRACTED")
                annotated_steps.append(step)

            seen_paths.add(best_path[-1])
            paths.append({
                "entry": G.nodes[entry_id].get("name", ""),
                "entry_score": round(score, 2),
                "endpoint": G.nodes[best_path[-1]].get("name", ""),
                "length": len(best_path),
                "steps": annotated_steps,
                "features": {
                    "has_concurrency": any(s.get("concurrency") for s in annotated_steps if s.get("concurrency")),
                    "has_conditions": any(s.get("call_condition") for s in annotated_steps if s.get("call_condition")),
                },
            })

    return paths


def cmd_key_paths(args):
    """Extract key execution paths automatically from entry points."""
    graph_dir = args.graph
    top_n = getattr(args, "top", 5)
    from_entry = getattr(args, "from_entry", None)
    max_tokens = getattr(args, "max_tokens", 0)

    G = _load_full_graph(graph_dir)

    # Load or compute entry scores
    entry_scores = {}
    scores_path = os.path.join(graph_dir, ".code2database_entry_scores.json")
    if os.path.exists(scores_path):
        try:
            scores_data = json.loads(Path(scores_path).read_text(encoding="utf-8"))
            # Support both "entry_points" and "top_entries" keys
            for entry in scores_data.get("entry_points", scores_data.get("top_entries", [])):
                entry_scores[entry.get("id", entry.get("node", ""))] = entry.get("score", 1.0)
        except (json.JSONDecodeError, KeyError):
            entry_scores = _compute_entry_scores(G)
    else:
        entry_scores = _compute_entry_scores(G)

    # If --from specified, override to only that entry
    if from_entry:
        from _builder.utils import _find_node_id
        entry_id = _find_node_id(G, from_entry)
        if entry_id:
            entry_scores = {entry_id: entry_scores.get(entry_id, 5.0)}

    # Find endpoints
    endpoints = _find_endpoints(G)

    # Extract paths
    paths = _extract_key_paths_from_entries(G, entry_scores, endpoints, top_n=top_n)

    result = {
        "key_paths": paths,
        "total_entries_analyzed": len(entry_scores),
        "total_endpoints_found": len(endpoints),
    }

    # Token budget
    result["_token_count"] = estimate_tokens(json.dumps(result, ensure_ascii=False))
    if max_tokens > 0 and result["_token_count"] > max_tokens:
        # Trim step details
        for p in result["key_paths"]:
            p["steps"] = [{"name": s["name"], "domain": s.get("domain", "")}
                          for s in p.get("steps", [])]
        result["_token_count"] = estimate_tokens(json.dumps(result, ensure_ascii=False))

    print(json.dumps(result, ensure_ascii=False, indent=2))
