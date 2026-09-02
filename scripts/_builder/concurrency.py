"""callgraph builder module: concurrency."""

import re
import networkx as nx
from _builder.utils import _output_result
from _builder.graph_build import _load_full_graph
import logging

_ALLOC_RE = re.compile(r'(alloc|malloc|new|create|init|open|start|get_mem|make)', re.I)
_RELEASE_RE = re.compile(r'(free|dealloc|delete|destroy|close|stop|cleanup|release|put_mem|drop)', re.I)
_USE_RE = re.compile(r'(read|write|process|handle|access|modify|update|send|recv|copy)', re.I)


def cmd_concurrency_risks(args):
    """List all concurrency risk points sorted by risk level."""
    G = _load_full_graph(args.graph)
    risks = []
    # Track (spawn_function, thread_function) pairs already reported to avoid
    # duplicates when both callee_args (tree-sitter) and edge concurrency (regex)
    # describe the same spawn point (e.g., Go goroutines).
    seen_spawn_pairs = set()

    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty"):
            continue

        # Check callee_args (from tree-sitter scanner)
        for ca in ndata.get("callee_args", []):
            ci = ca.get("concurrency_info", {})
            if ci.get("is_spawn") or ci.get("concurrency_type") in ("thread_spawn", "goroutine"):
                spawn_target = ci.get("spawn_target", "")
                spawn_order = ca.get("call_order")
                if spawn_order is None: spawn_order = 0
                # Dedup key
                dedup = (ndata.get("name", ""), spawn_target)
                if dedup in seen_spawn_pairs:
                    continue
                seen_spawn_pairs.add(dedup)
                main_calls = []
                for succ in G.successors(nid):
                    ed = G.get_edge_data(nid, succ) or {}
                    if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                        continue
                    succ_order = ed.get("call_order")
                    if succ_order is not None and succ_order > spawn_order and \
                       ed.get("concurrency") not in ("spawn_target", "callback"):
                        main_calls.append(G.nodes[succ].get("name", ""))
                # Risk: more concurrent calls = higher risk
                risk_level = "HIGH" if len(main_calls) > 3 else "MEDIUM" if main_calls else "LOW"
                # Shared data risk heuristic: check if any concurrent successor
                # accesses a global variable (via condition_vars or local_vars
                # that look global). This is imprecise — proper detection would
                # require per-function read/write tracking of global/struct vars.
                shared_risk = False
                for succ in G.successors(nid):
                    if succ not in G:
                        continue
                    sed = G.get_edge_data(nid, succ) or {}
                    if sed.get("relation") in ("CONTAINS", "IMPORTS"):
                        continue
                    snd = G.nodes[succ]
                    cvars = snd.get("condition_vars", [])
                    lvars = snd.get("local_vars", [])
                    all_vars = [v.get("name", "") if isinstance(v, dict) else str(v)
                                for v in cvars + lvars]
                    if any(v.startswith("g_") or v.startswith("global_")
                           or "shared" in v.lower() for v in all_vars):
                        shared_risk = True
                        break
                risks.append({
                    "spawn_function": ndata.get("name", ""),
                    "location": f"{ndata.get('source_file', '')}:{ndata.get('line', 0)}",
                    "thread_function": spawn_target,
                    "concurrent_with": main_calls,
                    "risk_level": risk_level,
                    "shared_data_risk": shared_risk,
                })

        # Check edge concurrency (from regex scanner)
        for succ in G.successors(nid):
            ed = G.get_edge_data(nid, succ) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            conc = ed.get("concurrency", "")
            if conc in ("spawn_target", "callback"):
                spawn_target = G.nodes[succ].get("name", succ)
                # Dedup: skip if already reported via callee_args above
                dedup = (ndata.get("name", ""), spawn_target)
                if dedup in seen_spawn_pairs:
                    continue
                seen_spawn_pairs.add(dedup)
                spawn_order = ed.get("call_order")
                if spawn_order is None: spawn_order = 0
                main_calls = []
                for s2 in G.successors(nid):
                    ed2 = G.get_edge_data(nid, s2) or {}
                    if ed2.get("relation") in ("CONTAINS", "IMPORTS"):
                        continue
                    s2_order = ed2.get("call_order")
                    # Only count calls AFTER the spawn point as concurrent
                    if s2 != succ and s2_order is not None and s2_order > spawn_order and \
                       ed2.get("concurrency") not in ("spawn_target", "callback"):
                        main_calls.append(G.nodes[s2].get("name", ""))
                risk_level = "HIGH" if len(main_calls) > 3 else "MEDIUM" if main_calls else "LOW"
                # Callback registration is generally lower risk than thread spawn
                if conc == "callback" and risk_level == "HIGH":
                    risk_level = "MEDIUM"
                risks.append({
                    "spawn_function": ndata.get("name", ""),
                    "location": f"{ndata.get('source_file', '')}:{ndata.get('line', 0)}",
                    "thread_function": spawn_target,
                    "concurrency_type": conc,
                    "concurrent_with": main_calls,
                    "risk_level": risk_level,
                    "shared_data_risk": False,
                })

    risks.sort(key=lambda r: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[r["risk_level"]])
    from collections import Counter
    _counts = Counter(r["risk_level"] for r in risks)
    result = {"total_spawn_points": len(risks),
              "high_risk": _counts.get("HIGH", 0),
              "medium_risk": _counts.get("MEDIUM", 0),
              "low_risk": _counts.get("LOW", 0),
              "risks": risks}
    _output_result(result, getattr(args, 'json', False))




def cmd_data_lifecycle(args):
    """Trace resource allocation→usage→release paths by keyword matching."""
    G = _load_full_graph(args.graph)
    resource_kw = args.resource.lower()
    result = {"resource": args.resource, "alloc": [], "use": [], "release": [], "unknown": [], "paths": []}

    relevant_nodes = []
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty"):
            continue
        name = ndata.get("name", "").lower()
        body = ndata.get("body_text", "").lower()
        lvars = " ".join(v.get("name", "") for v in ndata.get("local_vars", [])).lower()
        searchable = f"{name} {body} {lvars}"
        if resource_kw in searchable:
            role = "unknown"
            if _ALLOC_RE.search(name):
                role = "alloc"
            elif _RELEASE_RE.search(name):
                role = "release"
            elif _USE_RE.search(name):
                role = "use"
            relevant_nodes.append({"id": nid, "name": ndata.get("name", ""),
                                   "role": role, "domain": ndata.get("domain", ""),
                                   "location": f"{ndata.get('source_file', '')}:{ndata.get('line', 0)}"})

    # Categorize
    for n in relevant_nodes:
        result[n["role"]].append(n)

    # Find paths from alloc to release (call edges only)
    alloc_ids = {n["id"] for n in relevant_nodes if n["role"] == "alloc"}
    release_ids = {n["id"] for n in relevant_nodes if n["role"] == "release"}
    # Build call-only subgraph for pathfinding (exclude CONTAINS/IMPORTS)
    from _builder.utils import _make_call_graph
    _lifecycle_call_G = _make_call_graph(G)
    # Single BFS per alloc node; reuse the parent map for all release_ids.
    # Avoids O(alloc_count * release_count * (V+E)) shortest_path queries
    # which would hang on kernel-sized graphs.
    # NOTE: capped at 10 alloc/release nodes to bound runtime — surface
    # the truncation so users know paths may be incomplete.
    _alloc_list = sorted(alloc_ids)
    _release_list = sorted(release_ids)
    if len(_alloc_list) > 10 or len(_release_list) > 10:
        result["truncated"] = True
        result["truncation_note"] = (
            f"path search capped at 10 alloc ({len(_alloc_list)} found) and "
            f"10 release ({len(_release_list)} found) nodes; some paths "
            f"may be missing")
        print(f"[data-lifecycle] warning: {result['truncation_note']}",
              file=sys.stderr)
    release_lookup = set(_release_list[:10])
    for aid in _alloc_list[:10]:
        try:
            pred_map = nx.predecessor(_lifecycle_call_G, aid, cutoff=15)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue
        for rid in release_lookup:
            if rid not in pred_map or rid == aid:
                continue
            path = [rid]
            cur = rid
            while cur != aid:
                preds = pred_map.get(cur)
                if not preds:
                    path = None
                    break
                cur = preds[0]
                path.append(cur)
            if path is None:
                continue
            path.reverse()
            if len(path) <= 15:
                result["paths"].append({
                    "from": G.nodes[aid].get("name", aid),
                    "to": G.nodes[rid].get("name", rid),
                    "through": [G.nodes[s].get("name", s) for s in path[1:-1]],
                    "length": len(path),
                })

    _output_result(result, getattr(args, 'json', False))


