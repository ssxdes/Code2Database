"""index_pack.indexes — split from index_pack.py."""

import os
import json
import sys
import re
from pathlib import Path
from collections import Counter, defaultdict
import networkx as nx
from _builder.query.query import _resolve_detailed_chain, _trace_simple_chain
from _builder.token_budget import estimate_tokens
from _builder.utils import normalize_str_field
import logging

# Import universal skip names from scanner for automatic external endpoint classification
try:
    from _vendor._regex_c_scanner import _UNIVERSAL_SKIP_NAMES as _SCANNER_SKIP_NAMES
except ImportError:
    _SCANNER_SKIP_NAMES = frozenset()



_CB_NUM_SUFFIX_RE = re.compile(r'.*_cb\d+$')
_CB_USCORE_NUM_RE = re.compile(r'.*_cb_\d+$')
_IO_CB_RE = re.compile(r'.*_(read|write|unmap|flush|reset|abort)_cb$')
_LIFECYCLE_CB_RE = re.compile(r'.*_(init|fini|startup|shutdown|destroy)_cb$')
_OBJ_LIFECYCLE_CB_RE = re.compile(r'.*_(construct|destruct|create|delete|remove|add)_cb$')
_CB_PATTERN_RE = re.compile(r'.*(_cb|_cb_\d+|_done|_completion|_cpl|_event)$')
def _build_indexes(G: nx.DiGraph, outdir: str):
    """Pre-compute and write index files for fast queries."""
    import sys as _sys

    # 1. Reverse index: callers/callees per node (call edges only)
    # For large graphs, use streaming write to avoid OOM from json.dumps
    _ri_path = os.path.join(outdir, ".code2database_reverse_index.json")
    _node_count = G.number_of_nodes()
    if _node_count > 100000:
        # Streaming write: avoid building entire reverse_index dict in memory
        print(f"[build] Writing reverse index (streaming, {_node_count} nodes)...",
              file=_sys.stderr)
        with open(_ri_path, "w", encoding="utf-8") as _ri_f:
            _ri_f.write('{')
            _first_node = True
            for nid, ndata in G.nodes(data=True):
                callers = []
                for pred in G.predecessors(nid):
                    ed = G.get_edge_data(pred, nid) or {}
                    if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                        continue
                    callers.append({"id": pred, "name": G.nodes[pred].get("name", ""),
                                    "call_order": ed.get("call_order"),
                                    "call_condition": ed.get("call_condition", ""),
                                    "concurrency": ed.get("concurrency", "")})
                callees = []
                for succ in G.successors(nid):
                    ed = G.get_edge_data(nid, succ) or {}
                    if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                        continue
                    callees.append({"id": succ, "name": G.nodes[succ].get("name", ""),
                                    "call_order": ed.get("call_order"),
                                    "call_condition": ed.get("call_condition", ""),
                                    "concurrency": ed.get("concurrency", "")})
                if not _first_node:
                    _ri_f.write(',')
                _first_node = False
                _ri_f.write(json.dumps(nid, ensure_ascii=False) + ':')
                _ri_f.write(json.dumps({"callers": callers, "callees": callees},
                                       ensure_ascii=False, separators=(',', ':')))
            _ri_f.write('}\n')
    else:
        reverse_index = {}
        for nid, ndata in G.nodes(data=True):
            callers = []
            for pred in G.predecessors(nid):
                ed = G.get_edge_data(pred, nid) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                callers.append({"id": pred, "name": G.nodes[pred].get("name", ""),
                                "call_order": ed.get("call_order"),
                                "call_condition": ed.get("call_condition", ""),
                                "concurrency": ed.get("concurrency", "")})
            callees = []
            for succ in G.successors(nid):
                ed = G.get_edge_data(nid, succ) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                callees.append({"id": succ, "name": G.nodes[succ].get("name", ""),
                                "call_order": ed.get("call_order"),
                                "call_condition": ed.get("call_condition", ""),
                                "concurrency": ed.get("concurrency", "")})
            reverse_index[nid] = {"callers": callers, "callees": callees}
        Path(_ri_path).write_text(
            json.dumps(reverse_index, ensure_ascii=False) + "\n", encoding="utf-8")

    # 2. Condition index: branch conditions per node
    condition_index = {}
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        branches = []
        for succ in G.successors(nid):
            ed = G.get_edge_data(nid, succ) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            cond = ed.get("call_condition", "")
            if cond:
                # Get condition_vars from the target empty node or from the caller
                succ_nd = G.nodes[succ]
                cvars = succ_nd.get("condition_vars", []) if succ_nd.get("is_empty") else []
                # Also check caller's condition_vars
                if not cvars:
                    cvars = ndata.get("condition_vars", [])
                branches.append({"condition": cond, "target_node": succ,
                                 "target_name": G.nodes[succ].get("name", ""),
                                 "condition_vars": cvars})
        if branches:
            condition_index[nid] = branches
    Path(os.path.join(outdir, ".code2database_condition_index.json")).write_text(
        json.dumps(condition_index, ensure_ascii=False) + "\n", encoding="utf-8")

    # 3. Chains index: API_entry → endpoint paths
    # For large graphs: use shortest_path only (fast), skip all_simple_paths (exponential)
    # For small graphs: use all_simple_paths with strict limits
    api_entries = [nid for nid, d in G.nodes(data=True) if "API_entry" in d.get("labels", [])]
    endpoints = [nid for nid, d in G.nodes(data=True)
                 if "out_end" in d.get("labels", []) or "unknown_end" in d.get("labels", [])]
    chains = []
    seen_chains = set()
    ep_set = set(endpoints)
    # Build call-only subgraph for chain pathfinding (exclude CONTAINS/IMPORTS)
    from _builder.utils import _make_call_graph
    _chains_call_G = _make_call_graph(G)

    def _chain_step(path):
        """Build chain_steps from a path."""
        chain_steps = []
        for i, pnid in enumerate(path):
            pnd = G.nodes[pnid]
            step = {"id": pnid, "name": pnd.get("name", ""),
                    "labels": pnd.get("labels", []),
                    "is_empty": pnd.get("is_empty", False),
                    "condition": pnd.get("condition", "")}
            if i > 0:
                ed = G.get_edge_data(path[i-1], pnid) or {}
                step["call_order"] = ed.get("call_order")
                step["call_condition"] = ed.get("call_condition", "")
            chain_steps.append(step)
        return chain_steps

    n_nodes = G.number_of_nodes()
    if n_nodes < 5000 and len(api_entries) * len(endpoints) < 10000:
        # Small graph: compute all simple paths
        for api_id in api_entries[:50]:  # cap at 50 API entries
            for ep_id in endpoints[:100]:  # cap at 100 endpoints
                try:
                    path_count = 0
                    for path in nx.all_simple_paths(_chains_call_G, api_id, ep_id, cutoff=12):
                        real_nodes = tuple(n for n in path if not G.nodes[n].get("is_empty", False))
                        if real_nodes in seen_chains:
                            continue
                        seen_chains.add(real_nodes)
                        chains.append({"from_api": api_id, "to_endpoint": ep_id,
                                       "length": len(path) - 1, "steps": _chain_step(path)})
                        path_count += 1
                        if path_count >= 5:  # max 5 paths per pair
                            break
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    continue
    else:
        # Large graph: only shortest paths for top API entries.
        # For each api_id we run a SINGLE BFS (nx.predecessor) over the call
        # subgraph and reconstruct paths to endpoints/terminals by walking the
        # predecessor tree. This keeps the cost at O(api_count * (V+E)) instead
        # of O(api_count * endpoint_count * (V+E)) — the latter hangs for hours
        # on kernel-sized graphs (716K nodes).
        api_entries_sorted = sorted(api_entries,
                                    key=lambda x: _chains_call_G.out_degree(x), reverse=True)[:30]
        ep_lookup = set(endpoints[:200])
        for api_id in api_entries_sorted:
            # One BFS from this api_id; cutoff bounds the chain depth we care
            # about (12 hops, matching the small-graph branch's cutoff).
            try:
                pred_map, seen = nx.predecessor(_chains_call_G, api_id,
                                                cutoff=12, return_seen=True)
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                continue
            def _reconstruct(target):
                if target not in pred_map:
                    return None
                path = [target]
                cur = target
                while cur != api_id:
                    preds = pred_map.get(cur)
                    if not preds:
                        return None
                    cur = preds[0]
                    path.append(cur)
                path.reverse()
                return path

            found_ep = False
            for ep_id in endpoints[:200]:
                path = _reconstruct(ep_id)
                if path is None:
                    continue
                real_nodes = tuple(n for n in path if not G.nodes[n].get("is_empty", False))
                if real_nodes in seen_chains:
                    continue
                seen_chains.add(real_nodes)
                chains.append({"from_api": api_id, "to_endpoint": ep_id,
                               "length": len(path) - 1, "steps": _chain_step(path)})
                found_ep = True

            # Partial chains: terminal nodes reachable from this API. We can
            # derive these from the same BFS — terminal means out_degree==0
            # in the call subgraph. No second traversal needed.
            if not found_ep:
                terminal_nodes = [n for n in pred_map
                                  if n != api_id
                                  and n not in ep_lookup
                                  and not G.nodes[n].get("is_empty", False)
                                  and _chains_call_G.out_degree(n) == 0][:5]
                for tnid in terminal_nodes:
                    path = _reconstruct(tnid)
                    if path is None:
                        continue
                    real_nodes = tuple(n for n in path
                                       if not G.nodes[n].get("is_empty", False))
                    if real_nodes in seen_chains:
                        continue
                    seen_chains.add(real_nodes)
                    chains.append({"from_api": api_id, "to_endpoint": tnid,
                                   "length": len(path) - 1, "steps": _chain_step(path)})
    chains_data = {"total_chains": len(chains),
                   "api_entries": len(api_entries),
                   "endpoints": len(endpoints)}
    # Streaming write for large chains data
    _chains_path = os.path.join(outdir, ".code2database_chains.json")
    if len(chains) > 5000:
        with open(_chains_path, "w", encoding="utf-8") as _cf:
            _cf.write('{')
            _cf.write(f'"total_chains": {len(chains)}, ')
            _cf.write(f'"api_entries": {len(api_entries)}, ')
            _cf.write(f'"endpoints": {len(endpoints)}, ')
            _cf.write('"chains": [')
            _first = True
            for c in chains:
                if not _first:
                    _cf.write(',')
                _first = False
                json.dump(c, _cf, ensure_ascii=False, separators=(',', ':'))
            _cf.write(']}\n')
    else:
        chains_data["chains"] = chains
        Path(_chains_path).write_text(
            json.dumps(chains_data, ensure_ascii=False) + "\n", encoding="utf-8")

    # Lite chains: top 20 chains with minimal data (<2KB)
    lite_chains = []
    for c in sorted(chains, key=lambda x: -x.get("length", 0))[:20]:
        lite_chains.append({
            "from_api": c.get("from_api", ""),
            "to_endpoint": c.get("to_endpoint", ""),
            "length": c.get("length", 0),
            "path": " → ".join(
                s.get("name", s.get("id", "")) for s in c.get("steps", [])[:8]
                if not s.get("is_empty", False)),
        })
    lite_chains_data = {"total": len(chains), "top_chains": lite_chains}
    Path(os.path.join(outdir, ".code2database_chains_lite.json")).write_text(
        json.dumps(lite_chains_data, ensure_ascii=False, separators=(',', ':')) + "\n",
        encoding="utf-8")

    # 4. Concurrency index: spawn relationships and concurrent groups
    concurrency_index = {"spawn_points": [], "thread_entries": [], "concurrent_groups": []}
    # Find spawn points (nodes that create threads)
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        # Check callee_args for thread spawn patterns
        spawn_info = []
        for ca in ndata.get("callee_args", []):
            ci = ca.get("concurrency_info", {})
            if ci.get("is_spawn") or ci.get("concurrency_type") in ("thread_spawn", "goroutine"):
                target = ci.get("spawn_target", "")
                arg = ci.get("spawn_arg", "")
                spawn_info.append({
                    "callee": ca.get("callee", ""),
                    "spawn_target": target,
                    "spawn_arg": arg,
                    "concurrency_type": ci.get("concurrency_type", ""),
                    "call_order": ca.get("call_order"),
                })
        if spawn_info:
            concurrency_index["spawn_points"].append({
                "node": nid,
                "name": ndata.get("name", ""),
                "spawns": spawn_info,
            })
    # Find thread_processor nodes (functions that run in threads)
    for nid, ndata in G.nodes(data=True):
        if "thread_processor" in ndata.get("labels", []):
            # Find who spawns this function
            spawned_by = []
            for pred in G.predecessors(nid):
                ed = G.get_edge_data(pred, nid) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                if ed.get("concurrency") in ("spawn_target", "thread_spawn", "goroutine"):
                    spawned_by.append({"id": pred, "name": G.nodes[pred].get("name", ""),
                                       "concurrency": ed.get("concurrency", "")})
            # Find the argument passed to this thread function
            spawn_arg = ""
            for sp in concurrency_index["spawn_points"]:
                for s in sp["spawns"]:
                    if s["spawn_target"].lower() in nid.lower() or \
                       s["spawn_target"].lower() in ndata.get("name", "").lower():
                        spawn_arg = s.get("spawn_arg", "")
            concurrency_index["thread_entries"].append({
                "node": nid,
                "name": ndata.get("name", ""),
                "params": ndata.get("params", []),
                "spawned_by": spawned_by,
                "spawn_arg": spawn_arg,
            })
    # Build concurrent groups: for each spawn point, identify which calls run concurrently
    for sp in concurrency_index["spawn_points"]:
        sp_nid = sp["node"]
        sp_nd = G.nodes[sp_nid]
        # Find the spawn_target edge
        for s in sp["spawns"]:
            target_name = s["spawn_target"].lower()
            # The spawned thread runs concurrently with everything after the spawn call
            # Collect calls after the spawn call_order
            spawn_order = s.get("call_order", 0)
            concurrent_calls = []
            for succ in G.successors(sp_nid):
                ed = G.get_edge_data(sp_nid, succ) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                succ_order = ed.get("call_order")
                if succ_order is not None and succ_order > spawn_order and \
                   ed.get("concurrency") not in ("spawn_target", "callback"):
                    concurrent_calls.append({
                        "id": succ, "name": G.nodes[succ].get("name", ""),
                        "call_order": succ_order,
                    })
            if concurrent_calls or s.get("spawn_target"):
                # Resolve spawned_thread name to node ID for exact matching
                spawned_thread_id = ""
                target_name_lower = s.get("spawn_target", "").lower()
                for succ in G.successors(sp_nid):
                    ed = G.get_edge_data(sp_nid, succ) or {}
                    if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                        continue
                    if ed.get("concurrency") in ("spawn_target", "callback") and \
                       G.nodes[succ].get("name", "").lower() == target_name_lower:
                        spawned_thread_id = succ
                        break
                concurrency_index["concurrent_groups"].append({
                    "spawn_node": sp_nid,
                    "spawn_name": sp_nd.get("name", ""),
                    "spawn_call_order": spawn_order,
                    "spawned_thread": s.get("spawn_target", ""),
                    "spawned_thread_id": spawned_thread_id,
                    "concurrent_with_thread": concurrent_calls,
                    "concurrency_type": s.get("concurrency_type", ""),
                })
    Path(os.path.join(outdir, ".code2database_concurrency_index.json")).write_text(
        json.dumps(concurrency_index, ensure_ascii=False) + "\n", encoding="utf-8")





def _build_scenarios_file(G: nx.DiGraph, outdir: str, build_info: dict = None):
    """Generate .code2database_scenarios.json — detailed pre-computed execution scenarios.

    For each API_entry + significant enum/const combination, resolves the full call chain
    with pruned dead branches and concurrent windows. More detailed than the summary
    in context_pack — includes step-by-step resolved chains with pruned_branches.
    """
    globals_path = os.path.join(outdir, ".code2database_globals.json")
    globals_map = {}
    if os.path.exists(globals_path):
        # For large globals files (>100MB), skip loading to avoid OOM
        gsize = os.path.getsize(globals_path)
        if gsize > 100_000_000:
            print(f"[scenarios] Skipping globals: {gsize/1e6:.0f}MB too large",
                  file=sys.stderr)
        else:
            gd = json.loads(Path(globals_path).read_text(encoding="utf-8"))
            for enum in gd.get("enums", []):
                for v in enum.get("values", []):
                    member = v["member"]
                    val = v.get("value", member)
                    try:
                        # Coerce numeric strings to int; leave symbolic values as str
                        # so downstream consistency checks compare types uniformly.
                        val = int(val) if str(val).strip().isdigit() else val
                    except (ValueError, TypeError):
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                    globals_map[member] = val

    api_ids = [nid for nid, d in G.nodes(data=True)
               if "API_entry" in d.get("labels", [])
               and "dead_code" not in d.get("labels", [])]
    # Dead-code function IDs (excluded by build macros)
    dead_ids = {nid for nid, d in G.nodes(data=True)
                if "dead_code" in d.get("labels", [])}
    scenarios = []

    for api_id in api_ids[:30]:
        ndata = G.nodes[api_id]
        # Find condition_vars referencing globals
        relevant_vars = {}
        for cv in ndata.get("condition_vars", []):
            for var in cv.get("vars", []):
                if var in globals_map:
                    relevant_vars[var] = globals_map[var]

        if not relevant_vars:
            # Simple chain, no enum-driven branches
            resolved = _resolve_detailed_chain(G, api_id, {})
            if resolved:
                scenarios.append({
                    "trigger": f"{ndata.get('name', '')}()",
                    "binding": {},
                    "resolved_chain": resolved["steps"],
                    "pruned_branches": resolved["pruned"],
                    "concurrent_window": resolved["concurrent"],
                })
            continue

        # For each relevant variable, try with each value
        for var_name, var_value in relevant_vars.items():
            if isinstance(var_value, int):
                for val in (var_value, 0):  # try true and false
                    binding = {var_name: str(val)}
                    resolved = _resolve_detailed_chain(G, api_id, binding, globals_map)
                    if resolved:
                        scenarios.append({
                            "trigger": f"{ndata.get('name', '')}({var_name}={val})",
                            "binding": binding,
                            "resolved_chain": resolved["steps"],
                            "pruned_branches": resolved["pruned"],
                            "concurrent_window": resolved["concurrent"],
                        })
            else:
                binding = {var_name: str(var_value)}
                resolved = _resolve_detailed_chain(G, api_id, binding, globals_map)
                if resolved:
                    scenarios.append({
                        "trigger": f"{ndata.get('name', '')}({var_name}={var_value})",
                        "binding": binding,
                        "resolved_chain": resolved["steps"],
                        "pruned_branches": resolved["pruned"],
                        "concurrent_window": resolved["concurrent"],
                    })

    scenarios_path = os.path.join(outdir, ".code2database_scenarios.json")
    Path(scenarios_path).write_text(
        json.dumps({"total_scenarios": len(scenarios), "scenarios": scenarios},
                    ensure_ascii=False, separators=(',', ':')) + "\n", encoding="utf-8")





def _build_scenarios_summary_md(G: nx.DiGraph, outdir: str):
    """Generate SCENARIOS_SUMMARY.md — human-readable execution scenario table.

    Linearizes the resolved chain into a single representative path by picking
    the first non-conditional successor at each step (so the displayed path
    reads like a real call chain rather than a DFS fan-out).
    """
    sc_path = os.path.join(outdir, ".code2database_scenarios.json")
    if not os.path.exists(sc_path):
        return
    scenarios_data = json.loads(Path(sc_path).read_text(encoding="utf-8"))
    scenarios = scenarios_data.get("scenarios", []) if isinstance(scenarios_data, dict) else scenarios_data
    if not scenarios:
        return

    def _linearize_chain(chain: list) -> list:
        """Reduce a DFS-fanout chain to a single linear path.

        Strategy: pick the first non-conditional target at each depth, then
        skip duplicate consecutive names (caused by recursive or repeated
        edges). Returns a list of names.
        """
        linear = []
        seen_in_linear = set()
        for step in chain:
            if isinstance(step, dict):
                name = step.get("target", "")
                cond = step.get("condition", "") or ""
            else:
                name = str(step)
                cond = ""
            if not name:
                continue
            # Skip conditional placeholders unless they are the only kind
            # of step we have (then keep one to show the branch).
            if name.startswith("<conditional:"):
                continue
            # Skip consecutive duplicates
            if linear and linear[-1] == name:
                continue
            # Skip cycles
            if name in seen_in_linear:
                continue
            linear.append(name)
            seen_in_linear.add(name)
        return linear

    lines = ["# Execution Scenarios\n"]
    lines.append("| # | Trigger | Path | Concurrent | Pruned Branches |")
    lines.append("|---|---------|------|------------|-----------------|")
    for i, sc in enumerate(scenarios[:30], 1):
        trigger = sc.get("trigger", "")
        chain = sc.get("resolved_chain", [])
        chain_names = _linearize_chain(chain)
        path_str = " → ".join(chain_names[:8])
        if len(chain_names) > 8:
            path_str += " ..."
        cw = sc.get("concurrent_window", [])
        concurrent_names = [w.get("thread_fn", "") for w in cw if w.get("thread_fn")]
        concurrent = ", ".join(concurrent_names)[:40] or "—"
        pruned_items = sc.get("pruned_branches", [])[:3]
        pruned = ", ".join(p.get("condition", str(p))[:20] for p in pruned_items)[:50] or "—"
        lines.append(f"| {i} | {trigger} | {path_str} | {concurrent} | {pruned} |")

    Path(os.path.join(outdir, "SCENARIOS_SUMMARY.md")).write_text(
        "\n".join(lines) + "\n", encoding="utf-8")





def _compute_cross_domain_hotspots(G: nx.DiGraph, top_n: int = 10) -> list:
    """Find domain pairs with the most cross-domain calls."""
    pair_counts = defaultdict(int)
    for u, v, edata in G.edges(data=True):
        if edata.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        u_dom = G.nodes[u].get("domain", "") if u in G else ""
        v_dom = G.nodes[v].get("domain", "") if v in G else ""
        if u_dom and v_dom and u_dom != v_dom:
            pair_counts[(u_dom, v_dom)] += 1
    sorted_pairs = sorted(pair_counts.items(), key=lambda x: x[1], reverse=True)[:top_n]
    return [{"caller_domain": p[0], "callee_domain": p[1], "edge_count": c}
            for p, c in sorted_pairs]





def _compute_data_flow(G: nx.DiGraph, outdir: str) -> dict:
    """Compute data flow index: which params affect which conditions and callees."""
    flow_index = {}
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        if "API_entry" not in ndata.get("labels", []):
            continue
        for p in ndata.get("params", []):
            pname = p["name"]
            ptype = p.get("type", "")
            # Use compound key to avoid collisions when different APIs have same param names
            key = f"{ndata.get('name', '')}.{pname}"
            entry = {"type": ptype, "defined_in": f"{ndata.get('name', '')}(param)",
                      "flows_to_conditions": [], "affects_callees": []}
            for cv in ndata.get("condition_vars", []):
                if pname in cv.get("vars", []):
                    entry["flows_to_conditions"].append(cv["condition"])
            for ca in ndata.get("callee_args", []):
                for arg in ca.get("args", []):
                    if pname in arg.get("value", ""):
                        entry["affects_callees"].append(ca.get("callee", ""))
            if entry["flows_to_conditions"] or entry["affects_callees"]:
                flow_index[key] = entry
    return flow_index





def _compute_hub_functions(G: nx.DiGraph, top_n: int = 10) -> list:
    """Compute hub functions using degree-based heuristic for large graphs.

    For small graphs (<5000 nodes): uses betweenness centrality (accurate).
    For large graphs: uses degree centrality × cross-domain factor (fast).
    Returns list of dicts with 'id', 'name', 'domain', 'betweenness', 'callers_from_domains'.

    Filters out: empty nodes, external-domain nodes, file nodes, builtins
    (Python Py_*, os/sys/io.* etc.), and synthesized stub nodes (no source_file).
    """
    n_nodes = G.number_of_nodes()
    hubs = []

    # Local import to avoid cycle; reuse auto_enhance._is_likely_builtin to
    # filter Python builtins (Py_*, os.path.*, etc.) from hub candidates.
    try:
        from _builder.build.auto_enhance import _is_likely_builtin
    except Exception:
        def _is_likely_builtin(name: str) -> bool:
            return False

    def _is_hub_filterable(nd: dict, nid) -> bool:
        """Return True if this node should be excluded from hub candidates."""
        if nd.get("is_empty"):
            return True
        if nd.get("domain", "") == "external":
            return True
        if not nd.get("source_file"):
            return True
        if nd.get("node_type") == "file":
            return True
        if "file" in nd.get("labels", []):
            return True
        name = nd.get("name", "") or (str(nid) if nid else "")
        if _is_likely_builtin(name):
            return True
        # Synthesized external stubs (caller/callee without definitions) carry
        # the 'external' attr set by _emit_cgdb_records in scanner base.
        attrs = nd.get("attrs") or {}
        if isinstance(attrs, dict) and attrs.get("external"):
            return True
        return False

    if n_nodes < 5000:
        # Small graph: accurate betweenness centrality on call-only subgraph
        try:
            # Build call-only subgraph for betweenness computation
            from _builder.utils import _make_call_graph
            _hub_call_G = _make_call_graph(G)
            k = min(n_nodes, 200)
            bc = nx.betweenness_centrality(_hub_call_G, normalized=True, k=k)
            sorted_nodes = sorted(bc.items(), key=lambda x: x[1], reverse=True)[:top_n * 5]
            for nid, score in sorted_nodes:
                if score <= 0:
                    continue
                nd = G.nodes[nid]
                if _is_hub_filterable(nd, nid):
                    continue
                hubs.append({
                    "id": nid,
                    "name": nd.get("name", nid),
                    "domain": nd.get("domain", ""),
                    "betweenness": round(score, 4),
                    "callers_from_domains": len({G.nodes[p].get("domain", "")
                                                 for p in G.predecessors(nid)
                                                 if p in G
                                                 and G.nodes[p].get("domain") != nd.get("domain")
                                                 and (G.get_edge_data(p, nid) or {}).get("relation") not in ("CONTAINS", "IMPORTS")}),
                })
                if len(hubs) >= top_n:
                    break
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    else:
        # Large graph: fast degree-based heuristic
        # Hub score = in_degree × out_degree × cross_domain_factor
        # Pre-compute call-only degrees in a single pass over edges (much faster
        # than iterating predecessors/successors per node with edge_data lookups).
        call_in_deg = Counter()   # nid → incoming call degree
        call_out_deg = Counter()  # nid → outgoing call degree
        # caller_domain_sets[nid] → set of domains that call nid via call edges
        caller_domain_sets = defaultdict(set)

        for u, v, edata in G.edges(data=True):
            if edata.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            call_out_deg[u] += 1
            call_in_deg[v] += 1
            # Track caller domains for cross-domain computation
            u_dom = G.nodes[u].get("domain", "")
            if u_dom:
                caller_domain_sets[v].add(u_dom)

        # Score candidates using pre-computed degrees
        candidates = []
        for nid, nd in G.nodes(data=True):
            if _is_hub_filterable(nd, nid):
                continue
            in_deg = call_in_deg.get(nid, 0)
            out_deg = call_out_deg.get(nid, 0)
            if in_deg == 0 or out_deg == 0:
                continue
            # Cross-domain callers (exclude own domain)
            own_domain = nd.get("domain", "")
            cross_domain = len(caller_domain_sets.get(nid, set()) - {own_domain})
            # Approximate betweenness: degree product × cross-domain boost
            score = (in_deg * out_deg) * (1 + cross_domain * 0.5)
            candidates.append((nid, score, cross_domain))

        candidates.sort(key=lambda x: x[1], reverse=True)
        # Iterate past builtins/filtered nodes that may appear before top_n.
        for nid, score, cross_domain in candidates:
            if len(hubs) >= top_n:
                break
            nd = G.nodes[nid]
            if _is_hub_filterable(nd, nid):
                continue
            # Normalize score to 0-1 range for consistency with betweenness output
            max_score = candidates[0][1] if candidates else 1
            normalized = score / max_score if max_score > 0 else 0
            hubs.append({
                "id": nid,
                "name": nd.get("name", nid),
                "domain": nd.get("domain", ""),
                "betweenness": round(normalized, 4),
                "callers_from_domains": cross_domain,
            })

    return hubs[:top_n]





def _compute_scenarios(G: nx.DiGraph, outdir: str) -> list:
    """Pre-compute execution scenarios for API_entry nodes with enum-driven branches."""
    scenarios = []
    globals_path = os.path.join(outdir, ".code2database_globals.json")
    globals_map = {}
    enum_type_map = {}  # enum_name → {member: value}
    if os.path.exists(globals_path):
        gsize = os.path.getsize(globals_path)
        if gsize > 100_000_000:
            # Large file: use streaming parser to extract only the 'enums' section
            try:
                import ijson
                print(f"[context_pack] Streaming enums from {gsize/1e6:.0f}MB globals.json",
                      file=sys.stderr)
                with open(globals_path, 'rb') as _gf:
                    parser = ijson.parse(_gf)
                    in_enums = False
                    current_enum_name = ""
                    current_enum_vals = {}
                    for prefix, event, value in parser:
                        if prefix == 'enums' and event == 'start_array':
                            in_enums = True
                        elif in_enums and event == 'end_array' and prefix == 'enums':
                            in_enums = False
                            break
                        elif in_enums:
                            if event == 'start_map' and prefix.startswith('enums.item'):
                                current_enum_name = ""
                                current_enum_vals = {}
                            elif event == 'map_key' and prefix == 'enums.item' and value == 'name':
                                pass  # next string will be the name
                            elif (event == 'string' and prefix == 'enums.item.name'):
                                current_enum_name = value
                            elif prefix.startswith('enums.item.values.item'):
                                if event == 'start_map':
                                    pass
                                elif event == 'map_key' and value == 'member':
                                    pass
                                elif event == 'string' and 'member' in prefix:
                                    member = value
                                    current_enum_vals[member] = member
                                    globals_map[member] = member
                                elif event == 'map_key' and value == 'value':
                                    pass
                                elif event in ('number', 'string') and 'value' in prefix:
                                    try:
                                        val = int(value) if isinstance(value, (int, float)) else str(value)
                                    except (ValueError, TypeError):
                                        val = str(value)
                                    # Store the last member's value
                                    if current_enum_vals:
                                        last_member = list(current_enum_vals.keys())[-1]
                                        current_enum_vals[last_member] = val
                                        globals_map[last_member] = val
                            elif event == 'end_map' and prefix.startswith('enums.item'):
                                if current_enum_name and current_enum_vals:
                                    enum_type_map[current_enum_name] = current_enum_vals
                print(f"[context_pack] Loaded {len(enum_type_map)} enums, "
                      f"{len(globals_map)} enum members from stream", file=sys.stderr)
            except ImportError:
                print(f"[context_pack] ijson not available, skipping scenarios for "
                      f"{gsize/1e6:.0f}MB globals.json", file=sys.stderr)
            except Exception as e:
                print(f"[context_pack] Streaming enums failed: {e}, skipping scenarios",
                      file=sys.stderr)
        else:
            gd = json.loads(Path(globals_path).read_text(encoding="utf-8"))
            for enum in gd.get("enums", []):
                enum_name = enum.get("name", "")
                enum_vals = {}
                for v in enum.get("values", []):
                    member = v["member"]
                    val = v.get("value", member)
                    try:
                        val = int(val) if str(val).strip().isdigit() else val
                    except (ValueError, TypeError):
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                    globals_map[member] = val
                    enum_vals[member] = val
                if enum_name and enum_vals:
                    enum_type_map[enum_name] = enum_vals

    api_ids = [nid for nid, d in G.nodes(data=True) if "API_entry" in d.get("labels", [])]

    for api_id in api_ids[:30]:  # cap for performance
        ndata = G.nodes[api_id]
        # Find condition_vars that reference globals/enums
        relevant_vars = {}  # var_name -> set of possible values
        for cv in ndata.get("condition_vars", []):
            for var in cv.get("vars", []):
                if var in globals_map:
                    relevant_vars[var] = globals_map[var]
                # Also check if var is a param with type matching an enum name
                for p in ndata.get("params", []):
                    if p["name"] == var and p.get("type", "") in enum_type_map:
                        relevant_vars[var] = list(enum_type_map[p["type"]].values())[0]

        if not relevant_vars:
            # No enum-driven branches — just compute the simple chain
            chain = _trace_simple_chain(G, api_id, {})
            if chain:
                scenarios.append({
                    "trigger": f"{ndata.get('name', '')}()",
                    "chain": chain,
                    "condition": "",
                })
            continue

        # For each relevant variable, try resolved chains
        # Simple case: single enum variable with integer values
        for var_name, var_value in relevant_vars.items():
            if isinstance(var_value, int):
                # Try both true and false branches for conditions referencing this var
                bindings_true = {var_name: str(var_value)}
                bindings_false = {var_name: "0"}  # opposite
                chain_t = _trace_simple_chain(G, api_id, bindings_true, globals_map)
                chain_f = _trace_simple_chain(G, api_id, bindings_false, globals_map)
                if chain_t:
                    scenarios.append({
                        "trigger": f"{ndata.get('name', '')}({var_name}={var_value})",
                        "chain": chain_t,
                        "condition": f"{var_name} == {var_value}",
                    })
                if chain_f and chain_f != chain_t:
                    scenarios.append({
                        "trigger": f"{ndata.get('name', '')}({var_name} != {var_value})",
                        "chain": chain_f,
                        "condition": f"{var_name} != {var_value}",
                    })
            else:
                chain = _trace_simple_chain(G, api_id, {var_name: str(var_value)}, globals_map)
                if chain:
                    scenarios.append({
                        "trigger": f"{ndata.get('name', '')}({var_name}={var_value})",
                        "chain": chain,
                        "condition": f"{var_name} = {var_value}",
                    })

    return scenarios



def _generate_mermaid_path_diagram(G: nx.DiGraph, paths: list, title: str = "Critical Paths") -> str:
    """Generate Mermaid flowchart for given paths."""
    import hashlib
    lines = [f"```mermaid", f"flowchart TD"]
    seen = set()
    for path in paths[:5]:
        for i, nid in enumerate(path):
            if nid in seen:
                continue
            seen.add(nid)
            nd = G.nodes[nid]
            name = nd.get("name", nid)
            # Sanitize for mermaid with hash suffix for collision resistance
            safe_id = re.sub(r'[^a-zA-Z0-9_]', '_', nid) + "_" + hashlib.md5(nid.encode()).hexdigest()[:6]
            safe_name = name.replace('"', "'")
            labels = nd.get("labels", [])
            if "API_entry" in labels:
                lines.append(f'    {safe_id}["{safe_name}"]:::api')
            elif "out_end" in labels or "unknown_end" in labels:
                lines.append(f'    {safe_id}("{safe_name}"):::endpoint')
            else:
                lines.append(f'    {safe_id}["{safe_name}"]')
        for i in range(len(path) - 1):
            u_id = re.sub(r'[^a-zA-Z0-9_]', '_', path[i]) + "_" + hashlib.md5(path[i].encode()).hexdigest()[:6]
            v_id = re.sub(r'[^a-zA-Z0-9_]', '_', path[i+1]) + "_" + hashlib.md5(path[i+1].encode()).hexdigest()[:6]
            ed = G.get_edge_data(path[i], path[i+1]) or {}
            cond = ed.get("call_condition", "")
            label = f"|{cond}|" if cond else ""
            lines.append(f"    {u_id} -->{label} {v_id}")
    lines.append("    classDef api fill:#e1f5fe,stroke:#01579b")
    lines.append("    classDef endpoint fill:#fce4ec,stroke:#c62828")
    lines.append("```")
    return "\n".join(lines)



def _classify_endpoint(name: str, domain: str, profile: dict = None,
                       has_source_file: bool = True,
                       source_file: str = "") -> tuple:
    """Classify an endpoint based on naming patterns and domain.

    Args:
        name: Function name to classify.
        domain: Domain string from the graph node.
        profile: Builder config dict from ProfileSchema.to_builder_config().
                 When provided, uses profile's lib_prefix_map instead of
                 hardcoded _EXT_LIB_PREFIXES.

    Returns (type, desc) where type is one of:
    - external_* (categories from profile lib_prefix_map, e.g., external_posix,
      external_openssl, external_lib, etc.),
    - callback, function_pointer, test, internal_private,
      other_internal
    """
    # External library patterns (by name prefix)
    if profile and profile.get("lib_prefix_map"):
        ext_prefixes = profile["lib_prefix_map"]
    else:
        # Universal POSIX/C stdlib prefixes (always available)
        ext_prefixes = {
            'pthread_': 'external_posix',
            'sem_': 'external_posix',
            'epoll_': 'external_posix',
        }

    for prefix, cat in ext_prefixes.items():
        # Case-insensitive matching ONLY for ALL_UPPERCASE prefixes
        # (like SSL_ vs ssl_). Mixed-case prefixes like
        # 'Proj' (C++ namespace) must remain case-sensitive to avoid
        # matching 'proj_' (C function prefix).
        if prefix.isupper() or (prefix.endswith('_') and prefix[:-1].isupper()):
            if name.lower().startswith(prefix.lower()):
                return cat, f"External library function ({prefix[:-1]})"
        else:
            if name.startswith(prefix):
                return cat, f"External library function ({prefix[:-1]})"

    # Auto-classify functions in the scanner's universal skip set.
    # These are standard C/POSIX/library functions that the scanner always skips
    # (e.g., memcpy, pthread_mutex_lock, printf). When they appear as external
    # endpoints (no source file in the project), they should be automatically
    # classified instead of requiring LLM intervention.
    if _SCANNER_SKIP_NAMES and name in _SCANNER_SKIP_NAMES:
        return 'external_lib', 'Standard C/POSIX library function'

    # Auto-classify functions in the profile's skip_names_add list.
    # These are project-specific functions that the profile marks as skip
    # (e.g., kzalloc, spin_lock for Linux kernel). When they appear as external
    # endpoints, they should be automatically classified.
    if profile and name in profile.get("skip_names_add", []):
        return 'external_lib', 'Project-specific library function (profile skip)'

    # Auto-classify tracepoint functions (trace_ prefix = kernel tracepoint infrastructure).
    # These are generated by DECLARE_TRACE/DEFINE_TRACE macros and appear as callees
    # but have no real source in the project — they are false external endpoints.
    if name.startswith('trace_'):
        return 'tracepoint', 'Kernel tracepoint function'

    # C standard library functions (common ones not caught by prefix)
    _C_STD_FUNCS = {
        'strtok', 'strchr', 'strrchr', 'strstr', 'strerror', 'strlen', 'strcpy',
        'strncpy', 'strdup', 'strndup', 'strcmp', 'strncmp', 'strcasecmp', 'strncasecmp',
        'sscanf', 'sprintf', 'snprintf', 'printf', 'fprintf',
        'memcpy', 'memset', 'memmove', 'memcmp',
        'malloc', 'calloc', 'realloc', 'free',
        'atoi', 'atol', 'atof', 'strtol', 'strtoul', 'strtod',
        'qsort', 'bsearch', 'exit', 'abort',
        'fopen', 'fclose', 'fread', 'fwrite', 'fflush', 'fgets',
        'open', 'close', 'read', 'write', 'ioctl',
        'socket', 'bind', 'listen', 'accept', 'connect',
        'send', 'recv', 'sendto', 'recvfrom',
        'setsockopt', 'getsockopt', 'getaddrinfo', 'freeaddrinfo',
        'sigaction', 'signal', 'kill', 'raise',
        'getpid', 'getppid', 'perror',
        'usleep', 'sleep', 'nanosleep',
        'gettimeofday', 'clock_gettime',
        'ntohl', 'ntohs', 'htonl', 'htons', 'inet_ntop', 'inet_pton',
        'isdigit', 'isalpha', 'isalnum', 'isspace', 'isprint',
        'toupper', 'tolower',
        'va_start', 'va_end', 'va_arg', 'va_copy',
        'syslog', 'strcpy_s', 'memcpy_s', 'strcat_s', 'strncat_s',
        'localtime_r', 'gmtime_r', 'asctime_r', 'ctime_r',
        'rand', 'srand',
    }
    if name in _C_STD_FUNCS:
        return 'external_lib', 'C standard library function'

    # Callback patterns with more specific descriptions
    if name.endswith('_cb') or name.endswith('_callback'):
        # Detect callback subtype from naming patterns
        if '_ch_create_' in name or '_ch_destroy_' in name:
            return 'callback', 'Channel lifecycle callback'
        if '_event_' in name:
            return 'callback', 'Event callback'
        if 'rpc_' in name:
            return 'callback', 'RPC callback'
        if 'hotremove' in name or 'hot_remove' in name:
            return 'callback', 'Hot-remove callback'
        return 'callback', 'Callback function'
    # Callback variants: _cb with numeric suffix (_cb3, _cb_1), _cb_ctx, _cb_fun
    if _CB_NUM_SUFFIX_RE.match(name) or _CB_USCORE_NUM_RE.match(name):
        return 'callback', 'Callback function'
    if name.endswith('_cb_ctx') or name.endswith('_cb_fun') or name.endswith('_cb_func'):
        return 'callback', 'Callback function'
    # Names starting with cb_ or containing _cb_ (not just ending)
    if name.startswith('cb_') or '_cb_' in name:
        return 'callback', 'Callback function'
    if name.endswith('_fn') or name.endswith('_handler'):
        return 'function_pointer', 'Function pointer / handler'
    # Function pointer variants: _fun suffix, _intf (interface) suffix
    if name.endswith('_fun') or name.endswith('_intf'):
        return 'function_pointer', 'Function pointer / handler'
    # Completion/event handler patterns: _cpl (completion), _done, _event, on_*
    if name.endswith('_cpl') or name.endswith('_completion') or name.endswith('_done'):
        return 'callback', 'Completion callback'
    if name.endswith('_event') or name.startswith('on_') or '_on_' in name:
        return 'callback', 'Event handler'
    if name.startswith('handle_') or name.endswith('_handler'):
        return 'function_pointer', 'Event handler'
    # R37: Additional callback patterns
    # _ops callback tables (e.g., tgt_destroy_poll_group_ops)
    if name.endswith('_ops'):
        return 'function_pointer', 'Operation table / vtable'
    # IO operation callbacks (common in bdev/nvme)
    if _IO_CB_RE.match(name):
        return 'callback', 'IO completion callback'
    # Init/fini callbacks
    if _LIFECYCLE_CB_RE.match(name):
        return 'callback', 'Lifecycle callback'
    # Poller callbacks
    if name.endswith('_poller') or name.endswith('_poll_fn'):
        return 'callback', 'Poller callback'
    # Construct/destruct callbacks - refine with context
    if _OBJ_LIFECYCLE_CB_RE.match(name):
        if 'channel_' in name:
            return 'callback', 'Channel lifecycle callback'
        return 'callback', 'Object lifecycle callback'

    # Test helpers
    if name.startswith('test_') or '_test_' in name.lower() or name.endswith('_test'):
        return 'test', 'Test function'
    # Unit test helpers (e.g., ut_*, expected_*, dummy_*)
    if name.startswith('ut_') or name.startswith('expected_') or name.startswith('dummy_'):
        return 'test', 'Test helper function'

    # Internal private functions (starting with _)
    if name.startswith('_'):
        return 'internal_private', 'Internal private function'

    # Signal handlers
    if name.startswith('sig_') or 'signal_handler' in name:
        return 'internal_private', 'Signal handler'

    # Program entry points: main() in production code; test_entry in test/fuzz/example code
    if name == 'main':
        # Classify main() based on source context
        # Only mark as test_entry when the source is clearly in a test/example/fuzz path.
        # Note: 'app' is NOT treated as test — many C projects (e.g., SPDK) put
        # production executables in app/. Only test/ut/example/fuzz directories
        # are unambiguously non-production.
        _TEST_PATH_SEGMENTS = ('test', 'tests', 'ut', 'example', 'examples',
                               'fuzz', 'benchmark', 'demo', 'sample',
                               'samples', 'documentation', 'doc',
                               'tools', 'scripts')
        # Check both domain components and source_file path for test indicators
        domain_lower = domain.lower()
        domain_parts = domain_lower.split('.')
        src_lower = source_file.lower().replace("\\", "/")
        src_parts = src_lower.split("/")
        if any(p in _TEST_PATH_SEGMENTS for p in domain_parts) or \
           any(p in _TEST_PATH_SEGMENTS for p in src_parts):
            return 'test_entry', 'Test/example entry point'
        return 'program_entry', 'Program entry point'

    # RPC handler functions (registered via RPC macros)
    if name.startswith('rpc_'):
        return 'rpc_handler', 'RPC handler function'

    # Unresolved callees (no source file) that don't match any known pattern
    # are most likely function pointer parameters — the scanner captured a call
    # through a function pointer parameter but couldn't resolve what it points to.
    if not has_source_file:
        return 'function_pointer', 'Unresolved function pointer call'

    return 'other_internal', ''



def _mark_endpoint_nodes(G: nx.DiGraph, outdir: str, profile: dict = None,
                         vtable_regs: list = None) -> int:
    """Mark external/unresolved nodes as endpoints with automatic classification.

    Args:
        G: The invocation graph.
        outdir: Output directory for endpoint JSON.
        profile: Builder config dict from ProfileSchema.to_builder_config().
        vtable_regs: In-memory vtable_registrations list from the CURRENT
            build (preferred). When None, falls back to reading
            .code2database_vtables.json from outdir — which on the BUILD
            path is the PREVIOUS build's file (the current build writes it
            ~270 lines later), so first builds into a clean dir marked no
            vtable callback endpoints at all.

    Nodes with domain='external' or no successors and no source_file
    are classified as endpoints. Returns the count of marked endpoints.
    """
    endpoint_count = 0
    endpoints = []
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        domain = ndata.get("domain", "")
        name = ndata.get("name", nid.split('.')[-1] if '.' in nid else nid)

        # External domain nodes are endpoints (includes "external" and "external_*")
        if domain == "external" or domain.startswith("external_"):
            if "out_end" not in ndata.get("labels", []):
                labels = list(ndata.get("labels", []))
                labels.append("out_end")
                G.nodes[nid]["labels"] = labels
            ep_type, ep_desc = _classify_endpoint(name, domain, profile=profile,
                                                   has_source_file=bool(ndata.get("source_file", "")),
                                                   source_file=ndata.get("source_file", ""))
            endpoint_count += 1
            endpoints.append({"id": nid, "name": name,
                              "domain": domain, "type": ep_type,
                              "desc": ndata.get("external_desc", "") or ep_desc})
            continue

        # Terminal nodes (no successors) that are likely external endpoints
        # Only mark as endpoint if: no source_file (external) OR has explicit
        # external_desc. Internal leaf functions (with source_file, no external_desc)
        # are NOT endpoints — they're just leaves in the invocation graph.
        # Callback functions (_cb, _done, _completion) are internal callbacks,
        # NOT external endpoints — don't mark them as out_end/unknown_end.
        # Use filtered out_degree (call edges only, exclude CONTAINS/IMPORTS)
        call_out_deg = sum(1 for succ in G.successors(nid)
                          if (G.get_edge_data(nid, succ) or {}).get("relation") not in ("CONTAINS", "IMPORTS"))
        if call_out_deg == 0:
            labels = ndata.get("labels", [])
            is_api = "API_entry" in labels
            is_callback = "callback_func" in labels
            is_already_ep = "out_end" in labels or "unknown_end" in labels
            has_ext_desc = bool(ndata.get("external_desc", ""))
            has_src = bool(ndata.get("source_file", ""))
            # Mark as endpoint only if: external (no src) or has external description.
            # Skip callback_func — they are internal callbacks, not external endpoints.
            # Also skip names matching callback patterns even if not labeled callback_func.
            is_callback_pattern = bool(_CB_PATTERN_RE.match(name))
            if not is_already_ep and not is_api and not is_callback and not is_callback_pattern:
                if not has_src or has_ext_desc:
                    labels = list(labels)
                    ep_type, ep_desc = _classify_endpoint(name, domain, profile=profile,
                                                           has_source_file=has_src,
                                                           source_file=ndata.get("source_file", ""))
                    if ep_type in ("callback", "function_pointer"):
                        # Don't mark callbacks/function pointers as endpoints
                        continue
                    labels.append("out_end" if ep_type != "other_internal" else "unknown_end")
                    G.nodes[nid]["labels"] = labels
                    endpoint_count += 1
                    endpoints.append({"id": nid, "name": name,
                                      "domain": domain, "type": ep_type,
                                      "desc": ndata.get("external_desc", "") or ep_desc})

    # Internal entry points: program entry (main) and RPC handlers (rpc_*)
    # These are legitimate callgraph entry points even though they have callers
    # (e.g., main called by libc, rpc_* registered by RPC framework).
    # Also applies profile endpoint_rules (e.g., rte_eal_init → program_entry).
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        domain = ndata.get("domain", "")
        name = ndata.get("name", nid.split(".")[-1] if "." in nid else nid)
        labels = ndata.get("labels", [])
        is_already_ep = "out_end" in labels or "unknown_end" in labels or "entry_point" in labels

        # Check profile endpoint_rules first (highest priority)
        ep_type_from_rules = None
        if profile and profile.get("endpoint_rules"):
            for rule in profile["endpoint_rules"]:
                if re.match(rule["pattern"], name):
                    ep_type_from_rules = rule["endpoint_type"]
                    break

        if ep_type_from_rules:
            ep_type = ep_type_from_rules
            ep_desc = f"Profile rule: {ep_type}"
        else:
            ep_type, ep_desc = _classify_endpoint(name, domain, profile=profile,
                                                   has_source_file=bool(ndata.get("source_file", "")),
                                                   source_file=ndata.get("source_file", ""))

        if ep_type in ("program_entry", "rpc_handler", "thread_entry", "test_entry") and not is_already_ep:
            labels = list(labels)
            # test_entry uses its own label so downstream filters can drop
            # test-only entries from API_entry chains; the other entry types
            # keep the generic entry_point tag.
            labels.append("test_entry" if ep_type == "test_entry" else "entry_point")
            G.nodes[nid]["labels"] = labels
            endpoint_count += 1
            endpoints.append({"id": nid, "name": name,
                              "domain": domain, "type": ep_type,
                              "desc": ndata.get("external_desc", "") or ep_desc})

    # High entry_score nodes as endpoint candidates: functions with very high
    # entry scores (top percentile) that aren't already classified as endpoints
    # are likely framework entry points or important dispatch functions.
    # Only applies when the node has no predecessor call edges (it's a root)
    # or has very few callers relative to its callees.
    entry_scores = {}
    for nid, ndata in G.nodes(data=True):
        score = ndata.get("entry_score", 0)
        if score > 0:
            entry_scores[nid] = score
    if entry_scores:
        sorted_scores = sorted(entry_scores.values(), reverse=True)
        if sorted_scores:
            # Top 5% as threshold, minimum score of 5.0
            threshold = max(sorted_scores[max(0, len(sorted_scores) // 20)], 5.0)
            for nid, score in entry_scores.items():
                ndata = G.nodes[nid]
                if ndata.get("is_empty", False):
                    continue
                labels = ndata.get("labels", [])
                is_already_ep = any(l in labels for l in ("out_end", "unknown_end", "entry_point", "API_entry"))
                if is_already_ep:
                    continue
                if score >= threshold:
                    name = ndata.get("name", nid.split(".")[-1] if "." in nid else nid)
                    domain = ndata.get("domain", "")
                    # Only mark if it looks like an entry/init/start function
                    name_lower = name.lower()
                    _ENTRY_PATTERNS = (
                        r'_init$', r'_start$', r'_main$', r'_entry$',
                        r'_launch$', r'_boot$', r'_setup$',
                        r'^main$', r'^run$', r'^start$',
                    )
                    if any(re.match(p, name_lower) for p in _ENTRY_PATTERNS):
                        labels = list(labels)
                        labels.append("entry_point")
                        G.nodes[nid]["labels"] = labels
                        endpoint_count += 1
                        endpoints.append({"id": nid, "name": name,
                                          "domain": domain, "type": "framework_entry",
                                          "desc": f"High entry-score entry point (score={score:.1f})"})

    # Vtable callback endpoints: functions registered in vtables are callback
    # entry points — they are called indirectly through function pointer dispatch.
    # Prefer the in-memory registrations from the CURRENT build; the file at
    # .code2database_vtables.json is written LATER in the build pipeline, so
    # on a first build it doesn't exist and on a rebuild it's the previous
    # build's data.
    if vtable_regs is not None:
        # Same index shape the file writer produces:
        # {struct_type: {field: [{func_name, var_name, source_file, condition}]}}
        _vt_index = {}
        for vtable in vtable_regs:
            struct_type = vtable.get("struct_type", "")
            if not struct_type:
                continue
            for reg in vtable.get("registrations", []):
                _vt_index.setdefault(struct_type, {}).setdefault(
                    reg.get("field", ""), []).append({
                        "func_name": reg.get("func_name", ""),
                        "var_name": vtable.get("var_name", ""),
                        "source_file": vtable.get("source_file", ""),
                        "condition": reg.get("condition", ""),
                    })
        vtable_data = {"struct_types": _vt_index}
    else:
        vtable_path = os.path.join(outdir, ".code2database_vtables.json")
        vtable_data = None
        if os.path.exists(vtable_path):
            try:
                vtable_data = json.loads(Path(vtable_path).read_text(encoding="utf-8"))
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                vtable_data = None
    if vtable_data:
        try:
            # Support both old format (vtable_registrations list) and new format (struct_types dict)
            vtable_list = vtable_data.get("vtable_registrations", [])
            struct_types = vtable_data.get("struct_types", {})

            # Build a name-indexed lookup for faster matching
            _name_to_nid = {}
            for nid, ndata in G.nodes(data=True):
                if not ndata.get("is_empty", False):
                    name = ndata.get("name", "")
                    if name:
                        _name_to_nid.setdefault(name, []).append(nid)

            if struct_types:
                # New format: struct_types → {field → [registrations]}
                for struct_type, fields in struct_types.items():
                    for field, regs in fields.items():
                        for reg in regs:
                            func_name = reg.get("func_name", "")
                            if not func_name:
                                continue
                            for target_nid in _name_to_nid.get(func_name, []):
                                if "callback_endpoint" not in G.nodes[target_nid].get("labels", []):
                                    labels = list(G.nodes[target_nid].get("labels", []))
                                    labels.append("callback_endpoint")
                                    G.nodes[target_nid]["labels"] = labels
                                    endpoint_count += 1
                                    endpoints.append({
                                        "id": target_nid,
                                        "name": func_name,
                                        "domain": G.nodes[target_nid].get("domain", ""),
                                        "type": "callback_endpoint",
                                        "desc": f"Vtable callback: {struct_type}.{field}",
                                    })
            elif vtable_list:
                # Legacy format: list of vtable entries with registrations
                for vtable in vtable_list:
                    for reg in vtable.get("registrations", []):
                        func_name = reg.get("func_name", "")
                        if not func_name:
                            continue
                        for target_nid in _name_to_nid.get(func_name, []):
                            if "callback_endpoint" not in G.nodes[target_nid].get("labels", []):
                                labels = list(G.nodes[target_nid].get("labels", []))
                                labels.append("callback_endpoint")
                                G.nodes[target_nid]["labels"] = labels
                                endpoint_count += 1
                                field = reg.get("field", "")
                                struct_type = vtable.get("struct_type", "")
                                endpoints.append({
                                    "id": target_nid,
                                    "name": func_name,
                                    "domain": G.nodes[target_nid].get("domain", ""),
                                    "type": "callback_endpoint",
                                    "desc": f"Vtable callback: {struct_type}.{field}",
                                })
        except (json.JSONDecodeError, IOError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    # non-static functions called from a different domain are API entry candidates.
    # This provides zeroconfig API detection for projects without EXPORT_SYMBOL.
    if profile and profile.get("api_auto_detect"):
        _internal_patterns = profile.get("internal_patterns",
            ["_unit_", "_ut_", "_test_", "_perf_", "_verify_", "_example_",
             "_internal", "_priv", "_stub", "_mock"])
        # Project-declared non-API paths (e.g., test/, examples/, app/, scripts/).
        # Functions defined in these paths are never public API even when they
        # have cross-domain callers (test framework macros, etc.).
        _non_api_paths = tuple(
            (profile.get("project_boundaries", {}) or {}).get("non_api_paths", [])
        )
        for nid, ndata in G.nodes(data=True):
            if ndata.get("is_empty", False):
                continue
            labels = ndata.get("labels", [])
            if "API_entry" in labels:
                continue
            if any(l in labels for l in ("callback_func", "test_entry", "program_entry")):
                continue
            name = ndata.get("name", "")
            if not name:
                continue
            if any(p in name.lower() for p in _internal_patterns):
                continue
            if name.startswith("_"):
                continue
            # Skip functions whose source file lives in a non-API path.
            # This catches test/unit/.../main(), examples/foo.c:helper(), etc.
            src = (ndata.get("source_file", "") or "").replace(os.sep, "/")
            if _non_api_paths and any(p in src for p in _non_api_paths):
                continue
            # Check if function has callers from a different domain
            node_domain = ndata.get("domain", "")
            has_cross_domain_caller = False
            for pred in G.predecessors(nid):
                pred_domain = G.nodes[pred].get("domain", "")
                if pred_domain and pred_domain != node_domain:
                    has_cross_domain_caller = True
                    break
            if has_cross_domain_caller:
                labels = list(labels)
                labels.append("API_entry")
                G.nodes[nid]["labels"] = labels
                endpoint_count += 1

    # Write endpoint list
    if endpoints:
        ep_path = os.path.join(outdir, ".code2database_endpoints.json")
        Path(ep_path).write_text(
            json.dumps({"total_endpoints": len(endpoints), "endpoints": endpoints},
                       ensure_ascii=False, separators=(',', ':')) + "\n", encoding="utf-8")
        # Summary of classification
        from collections import Counter
        type_counts = Counter(ep["type"] for ep in endpoints)
        print(f"Endpoints: {len(endpoints)} classified endpoint(s)")
        for t, c in type_counts.most_common():
            print(f"  {t}: {c}")
        print(f"Endpoint list exported to: {ep_path}")

    return endpoint_count
  
