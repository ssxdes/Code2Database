"""callgraph builder module: query."""

import os
import json
import sys
import re
from pathlib import Path
from collections import defaultdict
import networkx as nx
from _builder.utils import _is_condition_alive, _output_result, _find_node_id, _parse_bindings, _load_globals, _streaming_json_lookup, _streaming_json_has_keys
from _builder.graph_build import _load_full_graph
from _builder.token_budget import estimate_tokens, truncate_to_tokens, budget_describe
from _builder.query_cache import cached_query, invalidate_node as _cache_invalidate_node


_BSD_QUEUE_MACRO_NAMES = frozenset({
    "stailq_insert_head", "stailq_insert_tail", "stailq_remove",
    "stailq_first", "stailq_last", "stailq_next", "stailq_entry",
    "tailq_insert_head", "tailq_insert_tail", "tailq_remove",
    "tailq_first", "tailq_last", "tailq_next", "tailq_prev",
    "list_insert_head", "list_insert_tail", "list_remove",
    "list_first", "list_next", "list_prev",
    "splay_left", "splay_right", "splay_root", "splay_min", "splay_max",
    "splay_insert", "splay_remove", "splay_find",
    "rb_tree_insert", "rb_tree_remove", "rb_tree_find",
    "rb_min", "rb_max", "rb_next", "rb_prev",
})


_GENERIC_EXTERNAL_METHOD_NAMES = frozenset({
    "call", "marshal", "unmarshal",
    "string", "info", "warn", "warning", "errorf",
    "debug", "trace", "fatal", "panic",
    "argumentparser", "add_argument", "parse_args",
    "loads", "dumps", "load", "dump",
    "exec", "eval", "compile",
})


def _is_scenario_noise_target(name: str) -> bool:
    """Identify leaf targets that should not appear as scenario chain endpoints.

    These are auto-created placeholder nodes for external/builtin callees
    (e.g., `client.call('...')` → callee name `call`; `dict.items()` → `items`)
    or BSD queue macro expansions. They are not real project functions and
    pollute scenario chains with noise.
    """
    if not name:
        return False
    if name.startswith("<conditional:"):
        return False
    if name in _BSD_QUEUE_MACRO_NAMES:
        return True
    if name in _GENERIC_EXTERNAL_METHOD_NAMES:
        return True
    from _builder.auto_enhance import _is_likely_builtin
    return _is_likely_builtin(name)


def _describe_node_touched(args) -> frozenset:
    """Return the set of node_ids a describe-node query depends on.

    Used by the query cache for node-version invalidation: when any of these
    nodes is updated, the cached describe-node result is dropped.
    """
    try:
        node_id = getattr(args, "node", "") or ""
        if node_id:
            return frozenset({node_id})
    except Exception:
        pass
    return frozenset()


def _load_profile_from_graph_dir(graph_dir):
    """Load the persisted builder profile from the graph output directory.

    The build command persists the builder profile to
    <graph_dir>/.code2database_profile.json so that downstream query commands can
    access project-specific patterns (io_classification keywords,
    macro_condition_prefixes, etc.) without requiring --profile to be re-specified.

    Returns:
        Builder config dict, or None if not found / unreadable.
    """
    if not graph_dir:
        return None
    profile_path = os.path.join(graph_dir, ".code2database_profile.json")
    if not os.path.isfile(profile_path):
        return None
    try:
        with open(profile_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return None


def _is_vtable_dispatch_alive(ed: dict, bindings: dict) -> bool:
    """Check if a vtable_dispatch edge is alive given bindings.

    Vtable dispatch edges use call_condition to encode the module hint:
      #vtable_module=nvme  → alive if bindings contain module=nvme

    If no module binding is given, vtable_dispatch edges are kept (conservative).
    If module binding is given, only the matching dispatch is alive.
    """
    conc = ed.get("concurrency", "")
    if conc != "vtable_dispatch":
        return True  # Not a vtable dispatch, let normal condition logic handle it

    cond = ed.get("call_condition", "")
    if not cond or not bindings:
        # No condition or no bindings: keep all dispatches (conservative)
        return True

    # Parse #vtable_module=<module> condition
    m = re.match(r'^#vtable_module=(\w+)$', cond)
    if m:
        target_module = m.group(1)
        # Check if bindings specify a module that matches
        # Try "module" first, then any vtable_module_keys from profile/bindings
        bound_module = bindings.get("module", "")
        if not bound_module:
            for key in bindings.get("vtable_module_keys", []):
                bound_module = bindings.get(key, "")
                if bound_module:
                    break
        if bound_module:
            return bound_module.lower() == target_module.lower()
        # No module binding specified: keep all (user hasn't disambiguated yet)
        return True

    # For other conditions (e.g., #ifdef), use normal condition check
    return True


def _resolve_detailed_chain(G: nx.DiGraph, start_id: str, bindings: dict,
                             globals_map: dict = None,
                             profile: dict = None) -> dict:
    """Resolve a detailed chain from start_id with bindings.

    Args:
        G: The invocation graph.
        start_id: Starting node ID.
        bindings: Binding definitions for macro conditions.
        globals_map: Global variable map.
        profile: Builder config dict from ProfileSchema.to_builder_config().

    Returns {"steps": [...], "pruned": [...], "concurrent": {...}}
    Each step has: step_num, action, target, condition, branch, concurrent.
    """
    if globals_map is None:
        globals_map = {}
    # Inject vtable_module_keys from profile into bindings so
    # _is_vtable_dispatch_alive can find them
    if profile and "vtable_module_keys" in profile:
        bindings.setdefault("vtable_module_keys", profile["vtable_module_keys"])
    visited = set()
    steps = []
    pruned = []
    concurrent_windows = []
    step_num = [0]

    def _resolve(nid, depth=0):
        if nid in visited or depth > 20:
            return
        visited.add(nid)
        nd = G.nodes[nid]

        for succ in G.successors(nid):
            ed = G.get_edge_data(nid, succ) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            cond = ed.get("call_condition", "")
            conc = ed.get("concurrency", "")
            succ_nd = G.nodes[succ]
            succ_name = succ_nd.get("name", "")

            if _is_scenario_noise_target(succ_name):
                continue

            # Check if this branch is alive given bindings
            alive = True
            if conc == "vtable_dispatch":
                alive = _is_vtable_dispatch_alive(ed, bindings)
            elif cond and bindings:
                alive = _is_condition_alive(cond, bindings, globals_map)

            step_num[0] += 1
            action = "call"
            branch = ""
            is_concurrent = False

            if conc == "vtable_dispatch":
                action = "vtable_dispatch"
            elif conc in ("spawn_target", "thread_spawn", "goroutine"):
                action = "spawn"
                is_concurrent = True
                # Find concurrent calls after this spawn
                spawn_order = ed.get("call_order") or 0
                main_calls = []
                for s2 in G.successors(nid):
                    ed2 = G.get_edge_data(nid, s2) or {}
                    if ed2.get("call_order") is not None and ed2["call_order"] > spawn_order and \
                       ed2.get("concurrency") not in ("spawn_target", "callback"):
                        s2_name = G.nodes[s2].get("name", "")
                        if not _is_scenario_noise_target(s2_name):
                            main_calls.append(s2_name)
                concurrent_windows.append({
                    "spawn_at": f"{nid}:{ed.get('call_order', '')}",
                    "thread_fn": succ_name,
                    "main_thread_calls": main_calls,
                })
            elif conc == "callback":
                action = "callback"

            if cond:
                branch = "then" if alive else "else"

            if alive:
                steps.append({
                    "step": step_num[0],
                    "action": action,
                    "target": succ_name,
                    "condition": cond,
                    "branch": branch,
                    "concurrent": is_concurrent,
                    "confidence": ed.get("confidence", "EXTRACTED"),
                })
                if not succ_nd.get("is_empty", False):
                    _resolve(succ, depth + 1)
            elif cond:
                pruned.append({
                    "condition": cond,
                    "dead_target": succ_name,
                    "reason": f"condition false per binding {bindings}",
                })

    _resolve(start_id)
    return {"steps": steps, "pruned": pruned, "concurrent": concurrent_windows}





def _resolve_simple_chain(G, start_id, bindings, globals_map, max_depth=20):
    """Resolve chain with bindings, return list of {id, name} steps."""
    chain = []
    visited = set()
    stack = [(start_id, 0)]
    while stack:
        nid, depth = stack.pop()
        if nid in visited or depth > max_depth:
            continue
        visited.add(nid)
        nd = G.nodes[nid]
        chain.append({"id": nid, "name": nd.get("name", nid)})
        for succ in G.successors(nid):
            ed = G.get_edge_data(nid, succ) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            conc = ed.get("concurrency", "")
            cond = ed.get("call_condition", "")
            # Check vtable_dispatch edges
            if conc == "vtable_dispatch":
                if not _is_vtable_dispatch_alive(ed, bindings):
                    continue
            elif cond and not _is_condition_alive(cond, bindings, globals_map):
                continue
            stack.append((succ, depth + 1))
    return chain




def _trace_simple_chain(G: nx.DiGraph, start_id: str, bindings: dict,
                        globals_map: dict = None, max_steps: int = 50) -> list:
    """Trace a call chain from start_id, resolving conditions with bindings.
    Returns a list of chain step strings like ['fn_A', '→[cond]fn_B', '→fn_C'].
    max_steps limits total chain entries to prevent explosion on large graphs.
    """
    if globals_map is None:
        globals_map = {}
    visited = set()
    chain = []

    def _trace(nid, depth=0):
        if nid in visited or depth > 15 or len(chain) >= max_steps:
            return
        visited.add(nid)
        nd = G.nodes[nid]
        is_empty = nd.get("is_empty", False)
        name = nd.get("name", "")

        if depth == 0 and not is_empty:
            chain.append(name)

        for succ in G.successors(nid):
            if len(chain) >= max_steps:
                return
            ed = G.get_edge_data(nid, succ) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            cond = ed.get("call_condition", "")
            conc = ed.get("concurrency", "")

            if cond and bindings:
                alive = _is_condition_alive(cond, bindings, globals_map)
                if not alive:
                    continue

            # Check vtable_dispatch edges
            if conc == "vtable_dispatch":
                if not _is_vtable_dispatch_alive(ed, bindings):
                    continue

            succ_nd = G.nodes[succ]
            succ_name = succ_nd.get("name", "")

            if _is_scenario_noise_target(succ_name):
                continue

            if conc == "vtable_dispatch":
                dispatch_cond = cond[:30] if cond else "dispatch"
                chain.append(f"→[vtable:{dispatch_cond}]{succ_name}")
            elif conc in ("spawn_target", "thread_spawn", "goroutine"):
                chain.append(f"→[spawn]{succ_name}")
            elif conc == "callback":
                chain.append(f"→[callback]{succ_name}")
            elif cond:
                short_cond = cond[:30]
                chain.append(f"→[{short_cond}]{succ_name}")
            else:
                chain.append(f"→{succ_name}")

            if not succ_nd.get("is_empty", False):
                _trace(succ, depth + 1)

    _trace(start_id)
    return chain




def _compute_exec_summary(semantic_desc: str, external_desc: str, name: str,
                          labels: list, params: list) -> str:
    """Derive a 1-2 sentence execution summary from available descriptions.

    Priority: semantic_desc → external_desc → label-based heuristic.
    """
    desc = semantic_desc or external_desc
    if desc:
        # Take first 1-2 sentences
        sentences = re.split(r'(?<=[.!?。！？])\s+', desc.strip())
        return sentences[0] if sentences else desc[:120]
    # Heuristic from labels + name
    if "API_entry" in labels:
        return f"Public API entry point: {name}"
    if "thread_processor" in labels:
        return f"Thread entry function: {name}"
    if "callback_func" in labels:
        return f"Callback handler: {name}"
    if "constructor" in labels:
        return f"Constructor: {name}"
    if "destructor" in labels:
        return f"Destructor: {name}"
    if "out_end" in labels:
        return f"External endpoint: {name}"
    return ""


def _compute_hub_info(G: nx.DiGraph, node_id: str) -> dict:
    """Compute hub/connector role information for a node.

    Returns betweenness rank and key paths passing through this node.
    """
    from collections import deque

    in_degree = sum(1 for pred in G.predecessors(node_id)
                   if (G.get_edge_data(pred, node_id) or {}).get("relation") not in ("CONTAINS", "IMPORTS"))
    out_degree = sum(1 for succ in G.successors(node_id)
                    if (G.get_edge_data(node_id, succ) or {}).get("relation") not in ("CONTAINS", "IMPORTS"))

    # Find API entries that can reach this node via reverse BFS (O(V+E) once)
    # instead of nx.has_path per API entry (O(API_count × (V+E)))
    # Use call-only edges (exclude CONTAINS/IMPORTS)
    ancestors = set()
    queue = deque([node_id])
    while queue:
        n = queue.popleft()
        for pred in G.predecessors(n):
            ed = G.get_edge_data(pred, n) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            if pred not in ancestors:
                ancestors.add(pred)
                queue.append(pred)

    api_ancestors = []
    for nid in ancestors:
        ndata = G.nodes[nid]
        if "API_entry" in ndata.get("labels", []):
            api_ancestors.append(ndata.get("name", nid))

    # Find endpoints reachable from this node via forward BFS (O(V+E) once)
    # Use call-only subgraph (exclude CONTAINS/IMPORTS edges)
    desc = set()
    try:
        from _builder.utils import _make_call_graph
        _hub_call_G = _make_call_graph(G)
        desc = nx.descendants(_hub_call_G, node_id)
    except Exception:
        pass

    end_descendants = []
    for nid in desc:
        ndata = G.nodes[nid]
        if "out_end" in ndata.get("labels", []) or "unknown_end" in ndata.get("labels", []):
            end_descendants.append(ndata.get("name", nid))

    hub_role = ""
    if in_degree >= 3 and out_degree >= 3:
        hub_role = "hub"
    elif in_degree >= 2 and out_degree >= 2:
        hub_role = "connector"
    elif api_ancestors and end_descendants:
        hub_role = "bridge"

    result = {
        "hub_role": hub_role,
        "in_degree": in_degree,
        "out_degree": out_degree,
    }
    if api_ancestors:
        result["reachable_from_apis"] = api_ancestors[:5]
    if end_descendants:
        result["reaches_endpoints"] = end_descendants[:5]
    return result


@cached_query('describe-node', ttl=600,
              touched_nodes_fn=_describe_node_touched,
              capture_stdout=True)
def cmd_describe_node(args):
    """Return ALL info about a node in one call — replaces search+neighbors+source-read."""
    graph_dir = args.graph
    node_id = args.node
    detail = getattr(args, "detail", "full")
    context_mode = getattr(args, "context", False)
    include_body = getattr(args, "include_body", False)

    G = _load_full_graph(graph_dir)

    if node_id not in G:
        candidates = [n for n in G.nodes if node_id.lower() in n.lower()]
        if candidates:
            print(f"Node '{node_id}' not found. Similar: {candidates[:5]}", file=sys.stderr)
        else:
            print(f"Node '{node_id}' not found in graph.", file=sys.stderr)
        sys.exit(1)

    nd = G.nodes[node_id]

    # Auto-fill stale nodes from source (no LLM needed)
    if nd.get("stale", False) and not nd.get("semantic_desc", ""):
        from _builder.patcher import lazy_fill_node
        source_root = ""
        master_path = os.path.join(graph_dir, "code2database_master.json")
        if os.path.exists(master_path):
            try:
                import json as _json
                master = _json.loads(Path(master_path).read_text(encoding="utf-8"))
                source_root = master.get("source_root", "")
            except Exception:
                pass
        filled = lazy_fill_node(G, node_id, source_root)
        if filled.get("body_text"):
            nd["body_text"] = filled["body_text"]
        if filled.get("signature"):
            nd["signature"] = filled["signature"]
        nd["stale"] = False  # Mark as filled (not truly fresh, but no longer stale)

    # Callers and callees with line numbers (call edges only)
    callers = []
    for pred in G.predecessors(node_id):
        ed = G.get_edge_data(pred, node_id) or {}
        if ed.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        pred_nd = G.nodes[pred]
        caller_entry = {"id": pred, "name": pred_nd.get("name", ""),
                        "location": f"{pred_nd.get('source_file', '')}:{pred_nd.get('line', 0)}",
                        "call_order": ed.get("call_order"),
                        "call_condition": ed.get("call_condition", ""),
                        "concurrency": ed.get("concurrency", "")}
        if ed.get("preproc_condition"):
            caller_entry["preproc_condition"] = ed["preproc_condition"]
        if not ed.get("preproc_alive", True):
            caller_entry["preproc_alive"] = False
        callers.append(caller_entry)
    callees = []
    for succ in G.successors(node_id):
        ed = G.get_edge_data(node_id, succ) or {}
        if ed.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        succ_nd = G.nodes[succ]
        callee_entry = {"id": succ, "name": succ_nd.get("name", ""),
                        "location": f"{succ_nd.get('source_file', '')}:{succ_nd.get('line', 0)}",
                        "call_order": ed.get("call_order"),
                        "call_condition": ed.get("call_condition", ""),
                        "concurrency": ed.get("concurrency", "")}
        if ed.get("preproc_condition"):
            callee_entry["preproc_condition"] = ed["preproc_condition"]
        if not ed.get("preproc_alive", True):
            callee_entry["preproc_alive"] = False
        callees.append(callee_entry)

    # Collect conditional compilation info
    all_conditions = set()
    for c in callees:
        if c.get("call_condition"):
            all_conditions.add(c["call_condition"])
    for c in callers:
        if c.get("call_condition"):
            all_conditions.add(c["call_condition"])
    cond_info = nd.get("condition_vars", [])

    # Condition branches (call edges only)
    branches = []
    for succ in G.successors(node_id):
        ed = G.get_edge_data(node_id, succ) or {}
        if ed.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        cond = ed.get("call_condition", "")
        if cond:
            succ_nd = G.nodes[succ]
            branches.append({"condition": cond, "target": succ,
                             "target_name": succ_nd.get("name", ""),
                             "target_location": f"{succ_nd.get('source_file', '')}:{succ_nd.get('line', 0)}"})

    # Related chains (from pre-computed index)
    chains_path = os.path.join(graph_dir, ".code2database_chains.json")
    related_chains = []
    if os.path.exists(chains_path):
        chains_data = json.loads(Path(chains_path).read_text(encoding="utf-8"))
        # P0_fix: Handle both formats:
        #   JSON build  → dict with "chains" key
        #   SQLite build → flat list of chain dicts
        if isinstance(chains_data, dict):
            chains_list = chains_data.get("chains", [])
        elif isinstance(chains_data, list):
            chains_list = chains_data
        else:
            chains_list = []
        for chain in chains_list:
            # Handle both formats: chain with "steps" list, or chain with "path" list
            if "steps" in chain:
                chain_node_ids = [s["id"] for s in chain.get("steps", [])]
            elif "path" in chain:
                chain_node_ids = chain.get("path", [])
            else:
                continue
            if node_id in chain_node_ids:
                related_chains.append({
                    "from_api": chain.get("from_api") or chain.get("entry", ""),
                    "to_endpoint": chain.get("to_endpoint") or chain.get("endpoint", ""),
                    "length": chain.get("length", 0),
                })
            if len(related_chains) >= 10:
                break

    # Globals context (enums/constants that appear in conditions)
    globals_context = []
    globals_path = os.path.join(graph_dir, ".code2database_globals.json")
    if os.path.exists(globals_path):
        gd = json.loads(Path(globals_path).read_text(encoding="utf-8"))
        cond_vars = nd.get("condition_vars", [])
        var_names = set()
        for cv in cond_vars:
            var_names.update(cv.get("vars", []))
        # Check local_vars too
        for lv in nd.get("local_vars", []):
            var_names.add(lv.get("name", ""))
        for enum in gd.get("enums", []):
            for v in enum.get("values", []):
                if v["member"] in var_names:
                    globals_context.append({"type": "enum", "name": enum["name"],
                                            "member": v["member"], "value": v.get("value", "")})
        for const in gd.get("constants", []):
            if const["name"] in var_names:
                globals_context.append({"type": "constant", "name": const["name"],
                                        "value": const.get("value_snippet", "")})

    # Concurrency info
    concurrency_info = {"is_spawn_point": False, "is_thread_entry": False,
                        "spawns": [], "spawned_by": [], "concurrent_with": []}
    # Check if this node creates threads
    for ca in nd.get("callee_args", []):
        ci = ca.get("concurrency_info", {})
        if ci.get("is_spawn") or ci.get("concurrency_type") in ("thread_spawn", "goroutine"):
            concurrency_info["is_spawn_point"] = True
            concurrency_info["spawns"].append({
                "target": ci.get("spawn_target", ""),
                "arg": ci.get("spawn_arg", ""),
                "type": ci.get("concurrency_type", ""),
                "call_order": ca.get("call_order"),
            })
    # Check if this is a thread entry (runs in a thread)
    # Either has thread_processor label, or is reached via spawn_target edge
    is_thread_entry = "thread_processor" in nd.get("labels", [])
    for pred in G.predecessors(node_id):
        ed = G.get_edge_data(pred, node_id) or {}
        if ed.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        if ed.get("concurrency") in ("spawn_target", "callback"):
            is_thread_entry = True
            if not any(sb["id"] == pred for sb in concurrency_info["spawned_by"]):
                concurrency_info["spawned_by"].append({
                    "id": pred, "name": G.nodes[pred].get("name", ""),
                    "concurrency": ed.get("concurrency", "spawn_target")})
    if is_thread_entry:
        concurrency_info["is_thread_entry"] = True
        # Find which spawn created this thread
        conc_path = os.path.join(graph_dir, ".code2database_concurrency_index.json")
        if os.path.exists(conc_path):
            # P0_fix: detect file format to avoid loading 500+MB unnecessarily.
            # SQLite build → node_id-keyed dict (no thread_entries key).
            # JSON build  → has thread_entries/concurrent_groups keys.
            has_keys = _streaming_json_has_keys(conc_path, ["thread_entries"])
            if has_keys.get("thread_entries"):
                # JSON build: load full file (expected to be small)
                size_mb = os.path.getsize(conc_path) / (1024 * 1024)
                if size_mb < 200:
                    conc_data = json.loads(Path(conc_path).read_text(encoding="utf-8"))
                    for te in conc_data.get("thread_entries", []):
                        if te["node"] == node_id:
                            for sb in te.get("spawned_by", []):
                                if not any(x["id"] == sb["id"] for x in concurrency_info["spawned_by"]):
                                    concurrency_info["spawned_by"].append(sb)
                            concurrency_info["spawn_arg"] = te.get("spawn_arg", "")
                            break
            # SQLite build format: no thread_entries key — skip (data is derivable from edges)
    # Find concurrent execution windows (what runs in parallel with this node)
    conc_path = os.path.join(graph_dir, ".code2database_concurrency_index.json")
    if os.path.exists(conc_path):
        has_keys = _streaming_json_has_keys(conc_path, ["concurrent_groups"])
        if has_keys.get("concurrent_groups"):
            size_mb = os.path.getsize(conc_path) / (1024 * 1024)
            if size_mb < 200:
                conc_data = json.loads(Path(conc_path).read_text(encoding="utf-8"))
                for cg in conc_data.get("concurrent_groups", []):
                    # If this node spawns a thread, list what runs concurrently
                    if cg["spawn_node"] == node_id:
                        concurrency_info["concurrent_with"].append({
                            "type": "after_spawn",
                            "spawned_thread": cg.get("spawned_thread", ""),
                            "runs_in_parallel": cg.get("concurrent_with_thread", []),
                        })
                    # If this node IS the spawned thread, list what it's concurrent with
                    # Use exact node_id match instead of endswith() to avoid false positives
                    spawned_thread_id = cg.get("spawned_thread_id", "")
                    if spawned_thread_id and node_id == spawned_thread_id:
                        concurrency_info["concurrent_with"].append({
                            "type": "as_spawned_thread",
                            "spawn_node": cg["spawn_node"],
                            "spawn_name": cg["spawn_name"],
                            "concurrent_with_self": cg.get("concurrent_with_thread", []),
                        })
        # SQLite build format: no concurrent_groups key — skip

    # Parameter flow: trace how parameters map to callees
    param_flow = []
    params = nd.get("params", [])
    if params:
        for p in params:
            pname = p["name"]
            flow_entry = {"param": pname, "type": p.get("type", ""),
                          "flows_to_conditions": [], "flows_to_callees": []}
            # Check condition_vars
            for cv in nd.get("condition_vars", []):
                if pname in cv.get("vars", []):
                    flow_entry["flows_to_conditions"].append(cv["condition"])
            # Check callee_args
            for ca in nd.get("callee_args", []):
                for arg in ca.get("args", []):
                    if pname in arg.get("value", ""):
                        flow_entry["flows_to_callees"].append({
                            "callee": ca.get("callee", ""),
                            "arg_pos": arg.get("pos"),
                            "arg_value": arg.get("value", ""),
                            "call_order": ca.get("call_order"),
                        })
            if flow_entry["flows_to_conditions"] or flow_entry["flows_to_callees"]:
                param_flow.append(flow_entry)

    # Exec summary — 1-2 sentence description of what this function does
    exec_summary = _compute_exec_summary(
        nd.get("semantic_desc", ""), nd.get("external_desc", ""),
        nd.get("name", ""), nd.get("labels", []), nd.get("params", []))

    # Hub info (only in --context mode or standard+ detail)
    hub_info = {}
    if context_mode:
        hub_info = _compute_hub_info(G, node_id)

    # Common base
    result = {
        "id": node_id,
        "name": nd.get("name", ""),
        "signature": nd.get("signature", ""),
        "domain": nd.get("domain", ""),
        "labels": nd.get("labels", []),
        "labels_source": nd.get("labels_source", {}),
        "is_empty": nd.get("is_empty", False),
        "conditional_compilation": {
            "conditions": sorted(all_conditions),
            "preproc_vars": cond_info,
        },
    }
    # ASM register-level data flow
    if nd.get("reg_state_final"):
        result["reg_state_final"] = nd["reg_state_final"]
    if nd.get("reg_transfers"):
        result["reg_transfers"] = nd["reg_transfers"]
    if nd.get("language"):
        result["language"] = nd["language"]
    if exec_summary:
        result["exec_summary"] = exec_summary
    if not nd.get("preproc_alive", True):
        result["preproc_alive"] = False

    if nd.get("is_empty", False):
        result["condition"] = nd.get("condition", "")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # Brief: id, name, signature, labels, location, caller/callee names, exec_summary (~200 tokens)
    source_file = nd.get("source_file", "")
    domain = nd.get("domain", "")
    # Brief mode: use domain prefix instead of full path
    if detail == "brief" and source_file:
        parts = source_file.replace("/", ".").split(".")
        domain_prefix = domain if domain else (parts[0] if parts else "")
        result["location"] = f"{domain_prefix}:{nd.get('line', 0)}"
    else:
        result["location"] = f"{source_file}:{nd.get('line', 0)}"
    result["callers"] = [f"{c['name']}@{c['location']}" for c in callers]
    result["callees"] = [f"{c['name']}@{c['location']}" for c in callees]
    # Key conditions
    key_conditions = list(dict.fromkeys(
        c["call_condition"] for c in callees if c.get("call_condition")))
    if key_conditions:
        result["key_conditions"] = key_conditions
    # Edge confidence for this node's edges
    edge_conf = {}
    for c in callees:
        ed = G.get_edge_data(node_id, c["id"]) or {}
        conf = ed.get("confidence", "EXTRACTED")
        if conf != "EXTRACTED":
            edge_conf[c["name"]] = conf
    if edge_conf:
        result["inferred_edges"] = edge_conf
    # Hub role in brief
    if hub_info.get("hub_role"):
        result["hub_role"] = hub_info["hub_role"]
    # Concurrency hints for brief mode
    concurrency_hints = []
    if concurrency_info.get("is_spawn_point"):
        concurrency_hints.append("spawn_point")
    if concurrency_info.get("is_thread_entry"):
        concurrency_hints.append("thread_entry")
    if concurrency_info.get("concurrent_with"):
        concurrency_hints.append("concurrent_execution")
    if concurrency_hints:
        result["concurrency_hints"] = concurrency_hints

    # Thread model info for brief mode
    tm = nd.get("thread_model")
    if tm:
        result["thread_model"] = tm

    # Confidence summary: how accurate is the data for this node?
    # Counts edges by confidence level so LLM knows how much to trust.
    conf_counts = {"EXTRACTED": 0, "INFERRED": 0, "AMBIGUOUS": 0}
    for c in callees:
        ed = G.get_edge_data(node_id, c["id"]) or {}
        conf = ed.get("confidence", "EXTRACTED")
        if conf in conf_counts:
            conf_counts[conf] += 1
    for c in callers:
        ed = G.get_edge_data(c["id"], node_id) or {}
        conf = ed.get("confidence", "EXTRACTED")
        if conf in conf_counts:
            conf_counts[conf] += 1
    total_edges = sum(conf_counts.values())
    if total_edges > 0:
        result["confidence_summary"] = conf_counts
        # Add a hint when most edges are inferred (low confidence)
        if conf_counts["INFERRED"] + conf_counts["AMBIGUOUS"] > conf_counts["EXTRACTED"]:
            result["confidence_warning"] = (
                "Most edges are INFERRED/AMBIGUOUS — consider verifying "
                "with source code via get-code-snippet")

    if detail == "brief":
        # Strip empty fields
        result = {k: v for k, v in result.items() if v or v is False or v == 0}
        max_tokens = getattr(args, "max_tokens", 0)
        result["_token_count"] = estimate_tokens(json.dumps(result, ensure_ascii=False))
        if max_tokens > 0:
            result = budget_describe(result, max_tokens)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # Standard: above + params, condition_vars, concurrency_info (~500 tokens)
    result["location"] = f"{source_file}:{nd.get('line', 0)}"
    result["params"] = nd.get("params", [])
    result["condition_vars"] = nd.get("condition_vars", [])
    result["api_constraints"] = nd.get("api_constraints", "")
    result["external_desc"] = nd.get("external_desc", "")
    result["semantic_desc"] = nd.get("semantic_desc", "")
    result["semantic_source"] = nd.get("semantic_source", "")
    # LLM supplement fields (from update-node command) — include any
    # `_supplemented` keys and the `_supplement_meta` provenance dict so
    # downstream LLM queries can distinguish facts from LLM-added data.
    supp_fields = {k: v for k, v in nd.items()
                   if k.endswith("_supplemented") and v}
    if supp_fields:
        result["supplements"] = supp_fields
    supp_meta = nd.get("_supplement_meta")
    if supp_meta:
        result["supplement_meta"] = supp_meta
    result["concurrency_info"] = concurrency_info
    result["branches"] = branches
    # Expand callers/callees with detail
    result["callers"] = callers
    result["callees"] = callees
    if hub_info:
        result["hub_info"] = hub_info

    # Invariants — preconditions/postconditions/loop_invariants
    # and state machine. Only include if non-empty to avoid bloating the
    # response for nodes that haven't had invariants extracted.
    preconditions = nd.get("preconditions", []) or []
    postconditions = nd.get("postconditions", []) or []
    loop_invariants = nd.get("loop_invariants", []) or []
    state_machine = nd.get("state_machine")
    if preconditions:
        result["preconditions"] = preconditions
    if postconditions:
        result["postconditions"] = postconditions
    if loop_invariants:
        result["loop_invariants"] = loop_invariants
    if state_machine:
        result["state_machine"] = state_machine
    inv_meta = nd.get("_invariant_meta")
    if inv_meta:
        result["invariant_meta"] = inv_meta

    # Auto-fill request — list empty fields the LLM should
    # fill. Lets the LLM complete the loop without manual export/import.
    try:
        from _builder.auto_enhance import compute_fill_request
        fill_request = compute_fill_request(nd)
        if fill_request:
            result["auto_fill_request"] = fill_request
    except Exception:
        pass  # auto_enhance is optional, don't break describe-node on failure

    # Doc-code alignment — if this node has any doc-code
    # mismatches (return value, param name, signature change, stale doc),
    # surface them so the LLM knows the doc may be unreliable.
    doc_stale = nd.get("doc_stale", False)
    if doc_stale:
        result["doc_stale"] = True
        result["doc_stale_reason"] = nd.get("doc_stale_reason", "")
    try:
        from _builder.doc_code_align import (
            _check_return_value_mismatch, _check_param_mismatch,
            _check_signature_change, _check_stale_doc,
        )
        mismatches = []
        mismatches.extend(_check_return_value_mismatch(node_id, nd))
        mismatches.extend(_check_param_mismatch(node_id, nd))
        mismatches.extend(_check_signature_change(node_id, nd))
        if not doc_stale:  # avoid double-reporting if already marked stale
            mismatches.extend(_check_stale_doc(node_id, nd))
        if mismatches:
            result["doc_code_mismatches"] = [m.to_dict() for m in mismatches]
    except Exception:
        pass  # doc_code_align is optional

    # Concurrency summary (standard level)
    concurrency_summary = {}
    if concurrency_info.get("is_spawn_point") or concurrency_info.get("is_thread_entry"):
        concurrency_summary["thread_role"] = "spawn_point" if concurrency_info["is_spawn_point"] else "thread_entry"
        if concurrency_info.get("spawns"):
            concurrency_summary["spawns_threads"] = [s.get("target", "") for s in concurrency_info["spawns"][:3]]
        if concurrency_info.get("concurrent_with"):
            concurrency_summary["concurrent_threads"] = [c.get("thread_fn", "") for c in concurrency_info["concurrent_with"][:3]]
        concurrency_summary["shared_state_access"] = bool(concurrency_info.get("spawns") or concurrency_info.get("concurrent_with"))

    # Threading model info (standard level)
    threading_info = {}
    tm = nd.get("thread_model")
    if tm:
        threading_info["thread_model"] = tm
    if nd.get("thread_entry", False):
        threading_info["thread_entry"] = True
    tmi = nd.get("thread_model_inherited")
    if tmi:
        threading_info["thread_model_inherited"] = tmi
    # Find spawned thread entry points (functions called via thread-creating APIs)
    spawned_entries = []
    for ca in nd.get("callee_args", []):
        ci = ca.get("concurrency_info", {})
        if ci.get("is_spawn") or ci.get("concurrency_type") in ("thread_spawn", "goroutine"):
            spawned_entries.append(ci.get("spawn_target", ""))
    if spawned_entries:
        threading_info["spawns_threads"] = spawned_entries
    if threading_info:
        result["threading"] = threading_info

    if detail == "standard":
        # Add concurrency summary
        if concurrency_summary:
            result["concurrency_summary"] = concurrency_summary
        # Strip empty fields
        result = {k: v for k, v in result.items() if v or v is False or v == 0}
        max_tokens = getattr(args, "max_tokens", 0)
        result["_token_count"] = estimate_tokens(json.dumps(result, ensure_ascii=False))
        if max_tokens > 0:
            result = budget_describe(result, max_tokens)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # Full: all fields. body_text only with --include-body (NOT default)
    if include_body:
        result["body_text"] = nd.get("body_text", "")
    # Reconstruct full local_vars with is_param entries
    params = nd.get("params", [])
    body_vars = nd.get("local_vars", [])
    result["local_vars"] = [{"name": p["name"], "type": p.get("type", ""),
                              "value_snippet": "<param>", "line": 0, "is_param": True}
                             for p in params] + body_vars
    result["callee_args"] = nd.get("callee_args", [])
    result["related_chains"] = related_chains
    result["globals_context"] = globals_context
    result["param_flow"] = param_flow

    # State access info (full detail only)
    state_access = {}
    for sa_key in ("globals_read", "globals_written", "fields_read", "fields_written"):
        sa_val = nd.get(sa_key, [])
        if sa_val:
            state_access[sa_key] = sa_val
    if state_access:
        result["state_access"] = state_access

    # --fields: selective field output
    fields = getattr(args, "fields", None)
    if fields:
        allowed = set(f.strip() for f in fields.split(","))
        # Always include id and name
        allowed.update({"id", "name"})
        result = {k: v for k, v in result.items() if k in allowed}

    # Strip empty fields
    result = {k: v for k, v in result.items()
              if v or v is False or v == 0 or (isinstance(v, list) and len(v) == 0)}
    # --max-tokens budget control
    max_tokens = getattr(args, "max_tokens", 0)
    if max_tokens > 0:
        result = budget_describe(result, max_tokens)
    else:
        result["_token_count"] = estimate_tokens(json.dumps(result, ensure_ascii=False))
    print(json.dumps(result, ensure_ascii=False, indent=2))




def cmd_diff_chains(args):
    """Compare execution paths under two different bindings."""
    G = _load_full_graph(args.graph)
    node_id = _find_node_id(G, args.node)
    if not node_id:
        print(f"Node not found: {args.node}", file=sys.stderr)
        sys.exit(1)

    bindings_a = _parse_bindings(args.bindings_a) if args.bindings_a else {}
    bindings_b = _parse_bindings(args.bindings_b) if args.bindings_b else {}
    globals_map = _load_globals(args.graph)

    chain_a = _resolve_simple_chain(G, node_id, bindings_a, globals_map)
    chain_b = _resolve_simple_chain(G, node_id, bindings_b, globals_map)

    set_a = {s["id"] for s in chain_a}
    set_b = {s["id"] for s in chain_b}

    only_a = [s for s in chain_a if s["id"] not in set_b]
    only_b = [s for s in chain_b if s["id"] not in set_a]
    common = [s for s in chain_a if s["id"] in set_b]

    result = {
        "node": node_id,
        "bindings_a": bindings_a,
        "bindings_b": bindings_b,
        "only_in_a": [{"id": s["id"], "name": s["name"]} for s in only_a],
        "only_in_b": [{"id": s["id"], "name": s["name"]} for s in only_b],
        "common": [{"id": s["id"], "name": s["name"]} for s in common],
        "summary": {
            "total_a": len(chain_a), "total_b": len(chain_b),
            "only_a_count": len(only_a), "only_b_count": len(only_b),
            "common_count": len(common),
        },
    }
    _output_result(result, getattr(args, 'json', False))




def cmd_resolve_chain(args):
    """Given a start node + variable bindings, return pruned call chain with dead branches removed."""
    graph_dir = args.graph
    node_id = args.node
    bindings_raw = args.bindings or ""

    G = _load_full_graph(graph_dir)

    if node_id not in G:
        candidates = [n for n in G.nodes if node_id.lower() in n.lower()]
        if candidates:
            print(f"Node '{node_id}' not found. Similar: {candidates[:5]}", file=sys.stderr)
        else:
            print(f"Node '{node_id}' not found in graph.", file=sys.stderr)
        sys.exit(1)

    # Parse bindings: "mode=1,flag=true"
    bindings = {}
    if bindings_raw:
        for pair in bindings_raw.split(","):
            parts = pair.strip().split("=", 1)
            if len(parts) == 2:
                bindings[parts[0].strip()] = parts[1].strip()

    # Load globals for enum/const resolution
    globals_map = _load_globals(graph_dir)

    # DFS from node_id, pruning branches where binding makes condition false
    visited = set()
    resolved = []

    def _resolve(nid, depth=0):
        if nid in visited or depth > 20:
            return
        visited.add(nid)
        nd = G.nodes[nid]
        step = {"id": nid, "name": nd.get("name", ""),
                "labels": nd.get("labels", []),
                "is_empty": nd.get("is_empty", False),
                "condition": nd.get("condition", ""),
                "signature": nd.get("signature", "") if not nd.get("is_empty") else "",
                "location": f"{nd.get('source_file', '')}:{nd.get('line', 0)}" if not nd.get("is_empty") else "",
                "params": nd.get("params", []) if not nd.get("is_empty") else []}
        # Determine which successors are alive
        alive_edges = []
        for succ in G.successors(nid):
            ed = G.get_edge_data(nid, succ) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            cond = ed.get("call_condition", "")
            conc = ed.get("concurrency", "")
            # Check vtable_dispatch edges
            if conc == "vtable_dispatch":
                if _is_vtable_dispatch_alive(ed, bindings):
                    alive_edges.append((succ, ed))
            elif not cond:
                alive_edges.append((succ, ed))
            elif _is_condition_alive(cond, bindings, globals_map):
                alive_edges.append((succ, ed))
            # else: dead branch, pruned

        step["alive_calls"] = [{"target": succ, "name": G.nodes[succ].get("name", ""),
                                "call_order": ed.get("call_order"),
                                "call_condition": ed.get("call_condition", ""),
                                "concurrency": ed.get("concurrency", "")}
                               for succ, ed in alive_edges]
        step["pruned_calls"] = []
        for succ in G.successors(nid):
            ed = G.get_edge_data(nid, succ) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            cond = ed.get("call_condition", "")
            conc = ed.get("concurrency", "")
            if conc == "vtable_dispatch":
                if not _is_vtable_dispatch_alive(ed, bindings):
                    step["pruned_calls"].append({"target": succ, "name": G.nodes[succ].get("name", ""),
                                                 "call_condition": cond, "concurrency": "vtable_dispatch",
                                                 "reason": "vtable dispatch not selected per bindings"})
            elif cond and not _is_condition_alive(cond, bindings, globals_map):
                step["pruned_calls"].append({"target": succ, "name": G.nodes[succ].get("name", ""),
                                             "call_condition": cond, "reason": "condition false per bindings"})
        # Add concurrency info for this step
        step["concurrency"] = {"is_spawn_point": False, "spawns_thread": ""}
        for ca in nd.get("callee_args", []):
            ci = ca.get("concurrency_info", {})
            if ci.get("is_spawn") or ci.get("concurrency_type") in ("thread_spawn", "goroutine"):
                step["concurrency"]["is_spawn_point"] = True
                step["concurrency"]["spawns_thread"] = ci.get("spawn_target", "")
                step["concurrency"]["spawn_arg"] = ci.get("spawn_arg", "")
                step["concurrency"]["spawn_type"] = ci.get("concurrency_type", "")
        # Add param flow for this step
        step["param_flow"] = []
        for p in nd.get("params", []):
            pname = p["name"]
            pf = {"param": pname, "flows_to_conditions": [], "flows_to_callees": []}
            for cv in nd.get("condition_vars", []):
                if pname in cv.get("vars", []):
                    pf["flows_to_conditions"].append(cv["condition"])
            for ca in nd.get("callee_args", []):
                for arg in ca.get("args", []):
                    if pname in arg.get("value", ""):
                        pf["flows_to_callees"].append({
                            "callee": ca.get("callee", ""),
                            "arg_pos": arg.get("pos"),
                            "arg_value": arg.get("value", ""),
                        })
            if pf["flows_to_conditions"] or pf["flows_to_callees"]:
                step["param_flow"].append(pf)
        resolved.append(step)
        for succ, ed in alive_edges:
            _resolve(succ, depth + 1)

    _resolve(node_id)

    # Build concurrent groups from the resolved steps
    concurrent_groups = []
    conc_path = os.path.join(graph_dir, ".code2database_concurrency_index.json")
    if os.path.exists(conc_path):
        # P0_fix: detect file format — SQLite build lacks concurrent_groups key.
        has_keys = _streaming_json_has_keys(conc_path, ["concurrent_groups"])
        if has_keys.get("concurrent_groups"):
            size_mb = os.path.getsize(conc_path) / (1024 * 1024)
            if size_mb < 200:
                conc_data = json.loads(Path(conc_path).read_text(encoding="utf-8"))
                for cg in conc_data.get("concurrent_groups", []):
                    # Only include groups where the spawn_node is in our resolved steps
                    resolved_ids = {s["id"] for s in resolved}
                    if cg["spawn_node"] in resolved_ids:
                        concurrent_groups.append({
                            "spawn_node": cg["spawn_node"],
                            "spawn_name": cg["spawn_name"],
                            "spawn_call_order": cg.get("spawn_call_order"),
                            "spawned_thread": cg.get("spawned_thread", ""),
                            "concurrent_with_thread": cg.get("concurrent_with_thread", []),
                            "concurrency_type": cg.get("concurrency_type", ""),
                        })

    print(json.dumps({"start_node": node_id, "bindings": bindings,
                       "resolved_steps": resolved,
                       "concurrent_groups": concurrent_groups}, ensure_ascii=False, indent=2))




def cmd_trace_chain(args):
    """One-shot trace from --from to --to with full annotation."""
    G = _load_full_graph(args.graph)
    from_id = _find_node_id(G, args.from_node)
    to_id = _find_node_id(G, args.to_node) if args.to_node else None
    if not from_id:
        print(f"Node not found: {args.from_node}", file=sys.stderr)
        sys.exit(1)

    bindings = _parse_bindings(args.bindings) if args.bindings else {}
    globals_map = _load_globals(args.graph)
    macros_str = getattr(args, 'macros', '')

    # Macro filtering helper: if macros specified, skip edges whose conditions
    # reference macros not in the user's set
    macro_set = set(macros_str.split(",")) if macros_str else set()
    def _macro_alive(cond, macro_set):
        if not cond or not macro_set:
            return True
        # If the condition references a macro not in our set, prune it
        macro_refs = re.findall(r'#ifdef\s+(\w+)|#if\s+defined\((\w+)\)|#if\s+(\w+)', cond)
        for groups in macro_refs:
            for g in groups:
                if g and g not in macro_set:
                    return False
        return True

    result = {"from": from_id, "to": to_id, "bindings": bindings, "path": []}
    if macros_str:
        result["macros"] = list(macro_set)

    # BFS from from_id toward to_id (or all paths if no to_id)
    from collections import deque
    visited = set()
    queue = deque([(from_id, [from_id])])
    found_path = None
    while queue:
        current, path = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        if current == to_id and to_id:
            found_path = path
            break
        for succ in G.successors(current):
            if succ not in visited:
                ed = G.get_edge_data(current, succ) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                cond = ed.get("call_condition", "")
                conc = ed.get("concurrency", "")
                # Check vtable_dispatch edges
                if conc == "vtable_dispatch":
                    if not _is_vtable_dispatch_alive(ed, bindings):
                        continue
                elif cond and not _is_condition_alive(cond, bindings, globals_map):
                    continue
                # Macro filtering
                if macro_set and not _macro_alive(cond, macro_set):
                    continue
                queue.append((succ, path + [succ]))

    path_to_use = found_path if found_path else []
    # When no to_id: return a BFS-ordered traversal from from_id
    bfs_parent = {}  # node → parent in BFS tree (for correct edge annotation)
    if not to_id and not path_to_use:
        # Re-traverse BFS to get ordered visit list
        bfs_visited = {from_id}
        bfs_order = [from_id]
        bfs_queue = deque([from_id])
        bfs_parent[from_id] = None
        while bfs_queue:
            n = bfs_queue.popleft()
            for s in G.successors(n):
                if s not in bfs_visited:
                    ed = G.get_edge_data(n, s) or {}
                    if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                        continue
                    cond = ed.get("call_condition", "")
                    conc = ed.get("concurrency", "")
                    # Check vtable_dispatch edges
                    if conc == "vtable_dispatch":
                        if not _is_vtable_dispatch_alive(ed, bindings):
                            continue
                    elif cond and not _is_condition_alive(cond, bindings, globals_map):
                        continue
                    # Macro filtering
                    if macro_set and not _macro_alive(cond, macro_set):
                        continue
                    bfs_visited.add(s)
                    bfs_order.append(s)
                    bfs_parent[s] = n
                    bfs_queue.append(s)
        path_to_use = bfs_order

    # Annotate each step
    annotated = []
    prev_id = from_id
    for nid in path_to_use:
        if nid == from_id and not annotated:
            nd = G.nodes[nid]
            annotated.append({
                "id": nid, "name": nd.get("name", ""),
                "signature": nd.get("signature", ""),
                "domain": nd.get("domain", ""),
                "labels": nd.get("labels", []),
            })
            continue
        nd = G.nodes[nid]
        # Use BFS parent for edge lookup (not sequential prev_id)
        edge_from = bfs_parent.get(nid, prev_id) if bfs_parent else prev_id
        ed = G.get_edge_data(edge_from, nid) or {}
        step = {
            "id": nid, "name": nd.get("name", ""),
            "signature": nd.get("signature", ""),
            "domain": nd.get("domain", ""),
            "labels": nd.get("labels", []),
            "call_order": ed.get("call_order"),
            "call_condition": ed.get("call_condition", ""),
            "concurrency": ed.get("concurrency", ""),
            "confidence": ed.get("confidence", "EXTRACTED"),
            # Evidence summary so LLM can judge accuracy of each edge.
            # Empty for EXTRACTED (high-confidence AST facts); populated for
            # INFERRED/AMBIGUOUS to explain why the inference was made.
            "evidence": ed.get("evidence", "") if ed.get("confidence") != "EXTRACTED" else "",
            "source": ed.get("source", "ast"),
        }
        annotated.append(step)
        prev_id = nid

    result["path"] = annotated
    result["total_steps"] = len(annotated)
    _output_result(result, getattr(args, 'json', False))


def _get_code_snippet(G: nx.DiGraph, node_id: str, source_root: str = "",
                       context_lines: int = 10, graph_dir: str = "") -> dict:
    """Extract source code snippet around a node's definition.

    Returns the function definition with surrounding context lines.
    No LLM needed — pure file I/O.
    """
    nd = G.nodes[node_id]
    source_file = nd.get("source_file", "")
    line_num = nd.get("line", 0)

    if not source_file or not line_num:
        return {"error": "Node has no source location", "id": node_id, "name": nd.get("name", "")}

    if source_root:
        full_path = os.path.join(source_root, source_file)
    else:
        # Try graph_dir first, then fallback to CWD
        master_path = ""
        if graph_dir:
            master_path = os.path.join(graph_dir, "code2database_master.json")
        if not master_path or not os.path.exists(master_path):
            master_path = os.path.join(os.path.dirname(os.path.abspath("")), "code2database_master.json")
        if os.path.exists(master_path):
            master = json.loads(Path(master_path).read_text(encoding="utf-8"))
            source_root = master.get("source_root", "")
            full_path = os.path.join(source_root, source_file)
        else:
            full_path = source_file

    if not os.path.exists(full_path):
        return {"error": f"Source file not found: {full_path}", "id": node_id, "name": nd.get("name", "")}

    try:
        lines = Path(full_path).read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError as e:
        return {"error": f"Cannot read file: {e}", "id": node_id, "name": nd.get("name", "")}

    # Extract context around the line
    start = max(0, line_num - context_lines - 1)
    end = min(len(lines), line_num + context_lines)
    snippet_lines = lines[start:end]

    result = {
        "id": node_id,
        "name": nd.get("name", ""),
        "source_file": source_file,
        "line": line_num,
        "signature": nd.get("signature", ""),
        "context_lines": context_lines,
        "snippet": "\n".join(f"{i+1:4d} | {line}" for i, line in zip(range(start + 1, end + 1), snippet_lines)),
    }
    return result


def cmd_get_code_snippet(args):
    """Handle get-code-snippet command.

    With --persist: writes the read source code back to the node's
    body_text field (as body_text_supplemented, non-destructive) so
    subsequent describe-node --full can read it without re-reading source.
    Requires user confirmation by default (DB write).
    """
    graph_dir = args.graph
    source_root = getattr(args, "source", "")
    node_id = args.node
    persist = getattr(args, "persist", False)
    auto_yes = getattr(args, "yes", False)
    G = _load_full_graph(graph_dir)

    if node_id not in G:
        candidates = [n for n in G.nodes if node_id.lower() in n.lower()]
        if candidates:
            print(f"Node '{node_id}' not found. Similar: {candidates[:5]}", file=sys.stderr)
        else:
            print(f"Node '{node_id}' not found in graph.", file=sys.stderr)
        sys.exit(1)

    result = _get_code_snippet(G, node_id, source_root,
                               context_lines=getattr(args, "context", 10),
                               graph_dir=graph_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # --persist: write the snippet back to the node's body_text field.
    # This is a DB write, so require user confirmation.
    if persist and "error" not in result:
        snippet_text = result.get("snippet", "")
        node_name = result.get("name", node_id)
        prompt = (
            "=== get-code-snippet --persist: confirmation required ===\n"
            f"  Node: {node_id}  ({node_name})\n"
            f"  Source: {result.get('source_file', '')}:{result.get('line', 0)}\n"
            f"  Snippet length: {len(snippet_text)} chars\n"
            f"  Stored as: body_text_supplemented (non-destructive)\n"
            "\n"
            "This will write the snippet to the callgraph database so future "
            "describe-node calls can read it without re-reading source."
        )
        from _builder.update_cmd import _confirm, _detect_backend
        if not _confirm(prompt, auto_yes):
            print("[persist] write aborted by user.")
            return

        backend = _detect_backend(graph_dir)
        attrs = {"body_text": snippet_text}
        if backend == "json":
            from _builder.update_cmd import _json_update_node
            ok = _json_update_node(graph_dir, node_id, attrs,
                                   source="llm_supplement", confidence="EXTRACTED")
        else:
            from _builder.update_cmd import _sqlite_update_node
            ok = _sqlite_update_node(graph_dir, node_id, attrs,
                                     source="llm_supplement", confidence="EXTRACTED")
        if ok:
            print(f"[persist] Wrote {len(snippet_text)} chars to body_text_supplemented "
                  f"for node {node_id}")


def _collect_dispatch_info(G: nx.DiGraph, start_id: str, max_depth: int = 15,
                           profile: dict = None) -> dict:
    """BFS from start_id, collecting all vtable dispatch points and macro conditions.

    Args:
      profile: Builder config dict (from ProfileSchema.to_builder_config() or the
        persisted .code2database_profile.json). Provides ``macro_condition_prefixes``
        for detecting project-specific macro conditions.

    Returns {
      "vtable_dispatches": [{
        "invoker_id": ..., "caller_name": ...,
        "struct_type": ..., "field": ...,
        "implementations": [{"func_name": ..., "module_hint": ..., "condition": ...}]
      }],
      "macro_conditions": [{"condition": ..., "at_node": ..., "edge_from": ..., "edge_to": ...}]
    }
    """
    from collections import deque

    # Load vtable index for registration details
    vtable_dispatches = []
    macro_conditions = []
    visited = set()
    queue = deque([(start_id, 0)])

    while queue:
        nid, depth = queue.popleft()
        if nid in visited or depth > max_depth:
            continue
        visited.add(nid)

        for succ in G.successors(nid):
            ed = G.get_edge_data(nid, succ) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            conc = ed.get("concurrency", "")
            cond = ed.get("call_condition", "")

            # Collect vtable dispatch points
            if conc == "vtable_dispatch":
                # Find other dispatches from the same caller to the same struct_type/field
                # by examining all vtable_dispatch edges from this node
                caller_name = G.nodes[nid].get("name", nid)
                # Group dispatches by condition prefix (struct_type:field)
                dispatch_info = {
                    "invoker_id": nid,
                    "caller_name": caller_name,
                    "target_name": G.nodes[succ].get("name", ""),
                    "condition": cond,
                    "module_hint": "",
                }
                # Extract module hint from condition #vtable_module=<hint>
                m = re.match(r'^#vtable_module=(\w+)$', cond)
                if m:
                    dispatch_info["module_hint"] = m.group(1)
                vtable_dispatches.append(dispatch_info)

            # Collect macro conditions (non-vtable)
            if cond and conc != "vtable_dispatch":
                # Heuristic: conditions starting with #ifdef/#ifndef are macro conditions
                cond_stripped = cond.strip()
                is_macro_cond = (cond_stripped.startswith("#") or
                                 cond_stripped.startswith("ifdef") or
                                 cond_stripped.startswith("ifndef"))
                # Profile-driven macro condition prefixes (e.g., PROJ_, CONFIG_)
                if not is_macro_cond and profile:
                    for prefix in profile.get("macro_condition_prefixes", []):
                        if cond_stripped.startswith(prefix):
                            is_macro_cond = True
                            break
                if is_macro_cond:
                    macro_conditions.append({
                        "condition": cond,
                        "at_node": G.nodes[nid].get("name", nid),
                        "edge_to": G.nodes[succ].get("name", succ),
                    })

            if succ not in visited:
                queue.append((succ, depth + 1))

    # Group vtable dispatches by caller (same caller → multiple implementations)
    grouped = defaultdict(list)
    for d in vtable_dispatches:
        key = d["invoker_id"]
        grouped[key].append(d)

    # Deduplicate: for each caller, list unique implementations
    deduped_dispatches = []
    for invoker_id, dispatches in grouped.items():
        caller_name = dispatches[0]["caller_name"]
        impls = []
        seen = set()
        for d in dispatches:
            sig = (d["target_name"], d["module_hint"])
            if sig not in seen:
                seen.add(sig)
                impls.append({
                    "func_name": d["target_name"],
                    "module_hint": d["module_hint"],
                    "condition": d["condition"],
                })
        deduped_dispatches.append({
            "invoker_id": invoker_id,
            "caller_name": caller_name,
            "implementations": impls,
        })

    # Deduplicate macro conditions
    seen_conds = set()
    deduped_macros = []
    for mc in macro_conditions:
        if mc["condition"] not in seen_conds:
            seen_conds.add(mc["condition"])
            deduped_macros.append(mc)

    return {
        "vtable_dispatches": deduped_dispatches,
        "macro_conditions": deduped_macros,
    }


# IO path scoring heuristics — classify functions as main IO path vs side paths
# Keywords that strongly indicate a function is NOT on the main data IO path.
# These are organized by category for maintainability.
#
# These are project-agnostic generic defaults. A project profile may extend
# them via ``io_classification.io_main_keywords`` / ``io_side_keywords`` (see
# ProfileSchema); the profile-supplied keywords are merged on top of these
# baselines by ``_get_io_keywords``.
_IO_SIDE_KEYWORDS = frozenset({
    # Error handling
    'error', 'fail', 'abort', 'err_', '_err',
    # Retry/recovery
    'retry', 'recover', 'resubmit',
    # Timeout/watchdog
    'timeout', 'watchdog',
    # Reset/cleanup
    'reset_', 'cleanup', 'clean_',
    # Destruction
    'destroy', 'destruct', 'fini',
    # Memory management (alloc/free are not IO data path)
    'dealloc', 'free_', '_free',
    # Statistics/monitoring
    '_stat', 'stat_', 'iostat', 'stats',
    # Debug/logging
    'debug', 'log_', 'dump_', 'trace_',
    # Testing
    'test_', '_test', 'unit_', '_ut', 'ut_',
    # Validation (not data movement)
    'validate', 'verify_', 'check_',
    # Polling (completion side, not submission)
    'poll_', '_poll',
    # Configuration/management
    'config', 'ioctl',
})

# Keywords that strongly indicate a function IS on the main data IO path.
_IO_MAIN_KEYWORDS = frozenset({
    'submit', 'queue', 'ring', 'doorbell', 'write', 'read', 'send', 'recv',
    'transfer', 'dispatch', 'process_request', 'build_request',
    'cmd_', 'command', 'execute', 'xfer',
    'io_path', 'io_submit',
})

# Keywords that are ambiguous — they appear in both main and side paths.
# These get neutral scores (no boost or penalty).
_IO_NEUTRAL_KEYWORDS = frozenset({
    'init', 'complete', 'done', 'start', 'begin', 'end',
    'alloc', 'get', 'set', 'put', 'add', 'remove',
    'lock', 'unlock', 'map', 'unmap',
})

# Per-profile keyword cache: id(profile) -> (main_set, side_set)
_IO_KEYWORD_CACHE = {}


def _get_io_keywords(profile):
    """Return (main_keywords, side_keywords) frozensets for the given profile.

    Merges the project-agnostic module-level defaults (_IO_MAIN_KEYWORDS,
    _IO_SIDE_KEYWORDS) with the profile-supplied keywords from
    ``io_classification.io_main_keywords`` / ``io_side_keywords``. The profile
    extends the defaults; it does not replace them.

    Results are cached per profile object identity to avoid rebuilding the
    frozensets on every call (scoring is hot path during BFS).
    """
    if not profile:
        return _IO_MAIN_KEYWORDS, _IO_SIDE_KEYWORDS
    cache_key = id(profile)
    if cache_key in _IO_KEYWORD_CACHE:
        return _IO_KEYWORD_CACHE[cache_key]
    io_cls = profile.get("io_classification", {}) if isinstance(profile, dict) else {}
    profile_main = io_cls.get("io_main_keywords", []) or []
    profile_side = io_cls.get("io_side_keywords", []) or []
    main_set = _IO_MAIN_KEYWORDS | frozenset(profile_main)
    side_set = _IO_SIDE_KEYWORDS | frozenset(profile_side)
    _IO_KEYWORD_CACHE[cache_key] = (main_set, side_set)
    return main_set, side_set


def _io_path_score(node_name: str, edge_data: dict = None,
                   profile: dict = None) -> float:
    """Score a node/edge for IO path priority. Higher = more likely on main IO path.

    Args:
      profile: Builder config dict. When provided, ``io_classification``
        keywords extend the module-level generic defaults.

    Returns a score in [0.0, 2.0]:
      2.0 = definitely main IO path (e.g., ring_doorbell, submit_request)
      1.0 = neutral (no strong signal)
      0.0 = definitely side path (e.g., error handling, stats, cleanup)

    This scoring is used by io-path to prioritize BFS traversal order.
    """
    name_lower = node_name.lower()
    main_keywords, side_keywords = _get_io_keywords(profile)

    # Check main IO keywords first — strong positive signal
    main_hits = sum(1 for kw in main_keywords if kw in name_lower)
    if main_hits >= 2:
        return 2.0
    if main_hits == 1:
        base = 1.5
    else:
        base = 1.0

    # Check side path keywords — negative signal
    side_hits = sum(1 for kw in side_keywords if kw in name_lower)
    if side_hits >= 2:
        return 0.0
    if side_hits == 1:
        base -= 0.5

    # Edge-level adjustments
    if edge_data:
        conc = edge_data.get("concurrency", "")
        cond = edge_data.get("call_condition", "")
        conf = edge_data.get("confidence", "")

        # INFERRED edges (vtable dispatch) are high-value for IO path tracing
        if conf == "INFERRED":
            base = min(base + 0.3, 2.0)

        # Conditional edges (#ifdef) are less likely to be the main path
        # unless they're vtable dispatch conditions
        if cond and conc != "vtable_dispatch":
            base -= 0.2

        # Callback edges are typically completion paths, not submission
        if conc == "callback":
            base -= 0.3

    return max(0.0, min(2.0, base))


def _io_path_bfs(G: nx.DiGraph, start_id: str, bindings: dict,
                 globals_map: dict, max_nodes: int = 100,
                 profile: dict = None) -> list:
    """Priority BFS that explores main IO path nodes first.

    Uses _io_path_score to order the BFS frontier, so main IO path
    functions are visited before error/retry/management paths.

    Args:
      profile: Builder config dict. Forwarded to _io_path_score so the
        profile's io_classification keywords extend the generic defaults.

    Returns a list of node IDs in visit order (up to max_nodes).
    """
    import heapq

    visited = {start_id}
    # Priority queue: (-score, tie_breaker, node_id)
    # Negative score because heapq is min-heap, we want max-score first
    counter = 0
    heap = [(-_io_path_score(G.nodes[start_id].get("name", ""), profile=profile), counter, start_id)]
    result = [start_id]
    parent = {start_id: None}

    while heap and len(result) < max_nodes:
        neg_score, _, n = heapq.heappop(heap)
        for s in G.successors(n):
            if s in visited:
                continue
            ed = G.get_edge_data(n, s) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            cond = ed.get("call_condition", "")
            conc = ed.get("concurrency", "")
            # Apply binding-based filtering
            if conc == "vtable_dispatch":
                if not _is_vtable_dispatch_alive(ed, bindings):
                    continue
            elif cond and not _is_condition_alive(cond, bindings, globals_map):
                continue

            visited.add(s)
            parent[s] = n
            # Only add to result if we haven't hit the limit yet
            if len(result) < max_nodes:
                s_name = G.nodes[s].get("name", "")
                score = _io_path_score(s_name, ed, profile=profile)
                counter += 1
                heapq.heappush(heap, (-score, counter, s))
                result.append(s)

    return result, parent


def cmd_io_path(args):
    """Trace IO path from a start function, auto-detecting vtable dispatch options.

    Per the user's guidance on function pointer analysis:
    1. List all registration entry points (vtable struct types)
    2. List all registered values (actual function pointers) for each entry
    3. Determine which target is actually called based on scenario/conditions

    If --bindings are provided (e.g., module=nvme), auto-resolve the path.
    If no bindings, output dispatch options for the user to choose.
    """
    graph_dir = args.graph
    from_name = args.from_node
    to_name = getattr(args, "to_node", "") or ""
    bindings_raw = getattr(args, "bindings", "") or ""
    json_mode = getattr(args, "json", False)
    max_nodes = getattr(args, "max_nodes", 100)

    G = _load_full_graph(graph_dir)
    from_id = _find_node_id(G, from_name)
    if not from_id:
        print(f"Node not found: {from_name}", file=sys.stderr)
        sys.exit(1)
    to_id = _find_node_id(G, to_name) if to_name else None

    bindings = _parse_bindings(bindings_raw) if bindings_raw else {}
    globals_map = _load_globals(graph_dir)

    # Load persisted profile (provides io_classification keywords and
    # macro_condition_prefixes for project-aware scoring and dispatch collection)
    profile = _load_profile_from_graph_dir(graph_dir)

    # Load vtable index for richer dispatch info
    vtable_index = {}
    vtable_path = os.path.join(graph_dir, ".code2database_vtables.json")
    if os.path.exists(vtable_path):
        vtable_data = json.loads(Path(vtable_path).read_text(encoding="utf-8"))
        vtable_index = vtable_data.get("struct_types", {})

    # Step 1: Collect dispatch and condition info from the call chain
    dispatch_info = _collect_dispatch_info(G, from_id, profile=profile)

    # Step 2: If no bindings provided, show dispatch options and exit
    if not bindings and (dispatch_info["vtable_dispatches"] or dispatch_info["macro_conditions"]):
        result = {
            "mode": "interactive",
            "from": from_id,
            "from_name": G.nodes[from_id].get("name", ""),
            "to": to_id,
            "to_name": G.nodes[to_id].get("name", "") if to_id else "",
            "vtable_dispatch_points": [],
            "macro_conditions": dispatch_info["macro_conditions"],
            "hint": "Re-run with --bindings to resolve the path. Example: --bindings 'module=storage,FEATURE_X=1'",
        }

        # Enrich dispatch points with vtable index data
        for dp in dispatch_info["vtable_dispatches"]:
            entry = {
                "caller": dp["caller_name"],
                "implementations": dp["implementations"],
            }
            # Try to find the struct_type/field from vtable index
            # Match by looking for the caller's vtable calls
            for struct_type, fields in vtable_index.items():
                for field, regs in fields.items():
                    reg_names = {r["func_name"] for r in regs}
                    impl_names = {i["func_name"] for i in dp["implementations"]}
                    if impl_names & reg_names:
                        entry["struct_type"] = struct_type
                        entry["field"] = field
                        entry["all_registrations"] = [{
                            "func_name": r["func_name"],
                            "var_name": r.get("var_name", ""),
                            "source_file": r.get("source_file", ""),
                            "condition": r.get("condition", ""),
                        } for r in regs]
                        break
                if "struct_type" in entry:
                    break
            result["vtable_dispatch_points"].append(entry)

        _output_result(result, json_mode)
        return

    # Step 3: Bindings provided — trace the resolved path
    from collections import deque

    if to_id:
        # Target specified: use standard BFS to find shortest path
        bfs_visited = {from_id}
        bfs_order = [from_id]
        bfs_queue = deque([from_id])
        bfs_parent = {from_id: None}
        target_found = False

        while bfs_queue:
            n = bfs_queue.popleft()
            if n == to_id:
                target_found = True
                break
            for s in G.successors(n):
                if s in bfs_visited:
                    continue
                ed = G.get_edge_data(n, s) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                cond = ed.get("call_condition", "")
                conc = ed.get("concurrency", "")
                if conc == "vtable_dispatch":
                    if not _is_vtable_dispatch_alive(ed, bindings):
                        continue
                elif cond and not _is_condition_alive(cond, bindings, globals_map):
                    continue
                bfs_visited.add(s)
                bfs_order.append(s)
                bfs_parent[s] = n
                bfs_queue.append(s)

        if not target_found:
            result = {
                "mode": "resolved",
                "from": from_id,
                "from_name": G.nodes[from_id].get("name", ""),
                "to": to_id,
                "to_name": G.nodes[to_id].get("name", "") if to_id else "",
                "bindings": bindings,
                "path": [],
                "total_steps": 0,
                "error": f"Target {to_id} not reachable from {from_id} with given bindings",
                "explored_nodes": len(bfs_order),
                "pruned_dispatches": [],
            }
            _output_result(result, json_mode)
            return
        # Reconstruct path from to_id back to from_id
        path_list = []
        cur = to_id
        while cur is not None:
            path_list.append(cur)
            cur = bfs_parent.get(cur)
        path_list.reverse()
        found_path = path_list
    else:
        # No target: use priority BFS to explore main IO path first
        found_path, bfs_parent = _io_path_bfs(G, from_id, bindings, globals_map, max_nodes=max_nodes, profile=profile)

    # Step 4: Annotate path with dispatch decisions
    annotated = []
    for nid in (found_path or []):
        nd = G.nodes[nid]
        parent = bfs_parent.get(nid)
        ed = G.get_edge_data(parent, nid) or {} if parent else {}
        step = {
            "id": nid,
            "name": nd.get("name", ""),
            "domain": nd.get("domain", ""),
            "signature": nd.get("signature", ""),
            "location": f"{nd.get('source_file', '')}:{nd.get('line', 0)}" if nd.get("source_file") else "",
            "io_path_score": round(_io_path_score(nd.get("name", ""), ed if parent else None, profile=profile), 2),
        }
        if parent:
            conc = ed.get("concurrency", "")
            cond = ed.get("call_condition", "")
            step["edge_type"] = conc if conc else "call"
            step["condition"] = cond
            step["confidence"] = ed.get("confidence", "EXTRACTED")
            # For vtable_dispatch, show which module was selected
            if conc == "vtable_dispatch":
                m = re.match(r'^#vtable_module=(\w+)$', cond)
                step["dispatch_selected"] = m.group(1) if m else cond
        annotated.append(step)

    # Collect pruned dispatches (which implementations were NOT selected)
    pruned_dispatches = []
    if bindings:
        for dp in dispatch_info["vtable_dispatches"]:
            for impl in dp["implementations"]:
                cond = impl["condition"]
                m = re.match(r'^#vtable_module=(\w+)$', cond)
                if m:
                    mod = m.group(1)
                    bound_mod = bindings.get("module", "")
                    if not bound_mod:
                        for key in bindings.get("vtable_module_keys", []):
                            bound_mod = bindings.get(key, "")
                            if bound_mod:
                                break
                    if bound_mod and bound_mod.lower() != mod.lower():
                        pruned_dispatches.append({
                            "caller": dp["caller_name"],
                            "pruned_func": impl["func_name"],
                            "module": mod,
                            "reason": f"module={bound_mod} selected, {mod} pruned",
                        })

    result = {
        "mode": "resolved",
        "from": from_id,
        "from_name": G.nodes[from_id].get("name", ""),
        "to": to_id,
        "to_name": G.nodes[to_id].get("name", "") if to_id else "",
        "bindings": bindings,
        "path": annotated,
        "total_steps": len(annotated),
        "pruned_dispatches": pruned_dispatches,
    }
    _output_result(result, json_mode)


def cmd_param_flow(args):
    """Trace how a parameter flows through the call chain from a start function.

    For each step, identifies:
      - Which callee receives the parameter (and at which arg position)
      - The argument value expression (e.g., 'ctx', 'ctx->field', 'ctx + 4')
      - Whether the parameter is passed through unchanged, transformed, or
        used as a field access

    The trace stops when:
      - Max depth is reached
      - The parameter no longer appears in any downstream callee_args
      - A cycle is detected (visited set)

    Output: list of flow steps, each with function name, param position,
    arg value, and next-hop callees.
    """
    graph_dir = args.graph
    from_name = args.from_node
    param_name = args.param
    max_depth = getattr(args, "max_depth", 10)
    json_mode = getattr(args, "json", False)

    G = _load_full_graph(graph_dir)
    from_id = _find_node_id(G, from_name)
    if not from_id:
        print(f"Node not found: {from_name}", file=sys.stderr)
        sys.exit(1)

    # Verify the start node has the parameter (or at least has params)
    nd = G.nodes[from_id]
    params = nd.get("params", [])
    if params and not any(p.get("name") == param_name for p in params):
        # Parameter not in start function's params — still trace if it appears
        # in callee_args (could be a local variable or alias)
        print(f"Warning: parameter {param_name!r} not in {from_name!r} params "
              f"({[p.get('name') for p in params]}); tracing as alias",
              file=sys.stderr)

    # BFS: trace the parameter through the call chain
    from collections import deque
    visited = set()
    flow_steps = []
    # Each queue item: (function_id, param_to_track, depth, path_so_far)
    queue = deque([(from_id, param_name, 0, [from_id])])

    while queue:
        cur_id, cur_param, depth, path = queue.popleft()
        if depth >= max_depth:
            continue
        if cur_id in visited:
            continue
        visited.add(cur_id)

        cur_nd = G.nodes[cur_id]
        cur_name = cur_nd.get("name", cur_id)
        callee_args = cur_nd.get("callee_args", [])

        # Find callees where cur_param appears in arg value
        next_hops = []
        for ca in callee_args:
            callee = ca.get("callee", "")
            for arg in ca.get("args", []):
                arg_val = arg.get("value", "")
                if not arg_val:
                    continue
                # Match: param appears as a token in the arg value
                # Use word-boundary regex to avoid substring false positives
                import re as _re
                if _re.search(r'\b' + _re.escape(cur_param) + r'\b', arg_val):
                    next_hops.append({
                        "callee": callee,
                        "arg_pos": arg.get("pos"),
                        "arg_value": arg_val,
                        "call_order": ca.get("call_order"),
                    })

        step = {
            "function": cur_name,
            "function_id": cur_id,
            "depth": depth,
            "param_tracked": cur_param,
            "params": [p.get("name") for p in cur_nd.get("params", [])],
            "next_hops": next_hops,
            "path": list(path),
        }
        flow_steps.append(step)

        # Enqueue next hops
        for nh in next_hops:
            callee_name = nh["callee"]
            invoked_id = _find_node_id(G, callee_name)
            if not invoked_id or invoked_id in visited:
                continue
            # The tracked param in the callee is the arg_value (could be the
            # same name, or a field access like 'ctx->field'). For simplicity,
            # we trace the callee's parameter at the same position.
            callee_nd = G.nodes[invoked_id]
            callee_params = callee_nd.get("params", [])
            arg_pos = nh.get("arg_pos")
            next_param = cur_param  # default: keep tracking same name
            if arg_pos is not None and arg_pos < len(callee_params):
                # The arg at position arg_pos maps to the callee's param
                # at the same position (1-indexed in some scanners)
                pos_idx = arg_pos - 1 if arg_pos >= 1 else arg_pos
                if 0 <= pos_idx < len(callee_params):
                    next_param = callee_params[pos_idx].get("name", cur_param)
            new_path = path + [invoked_id]
            queue.append((invoked_id, next_param, depth + 1, new_path))

    result = {
        "start_function": G.nodes[from_id].get("name", from_id),
        "start_function_id": from_id,
        "param_tracked": param_name,
        "max_depth": max_depth,
        "total_steps": len(flow_steps),
        "flow_steps": flow_steps,
        "reached_end": len(flow_steps) > 0 and not flow_steps[-1]["next_hops"],
    }
    _output_result(result, json_mode)


def cmd_blast_radius(args):
    """Handle blast-radius command — show what's affected when a function changes."""
    graph_dir = args.graph
    node_id = args.node
    depth = getattr(args, "depth", 3)
    G = _load_full_graph(graph_dir)

    if node_id not in G:
        candidates = [n for n in G.nodes if node_id.lower() in n.lower()]
        if candidates:
            print(f"Node '{node_id}' not found. Similar: {candidates[:5]}", file=sys.stderr)
        else:
            print(f"Node '{node_id}' not found in graph.", file=sys.stderr)
        sys.exit(1)

    # Find all reverse-reachable nodes (callers/cascading callers)
    affected_funcs = set()
    affected_apis = []
    affected_tests = []
    frontier = [node_id]
    visited = {node_id}

    for _ in range(depth):
        next_frontier = []
        for n in frontier:
            for pred in G.predecessors(n):
                if pred in visited:
                    continue
                ed = G.get_edge_data(pred, n) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                visited.add(pred)
                nd = G.nodes[pred]
                labels = nd.get("labels", [])
                affected_funcs.add(pred)
                if "API_entry" in labels:
                    affected_apis.append({"id": pred, "name": nd.get("name", ""),
                                          "domain": nd.get("domain", "")})
                # Heuristic: test functions contain unit/test/perf patterns
                name_lower = nd.get("name", "").lower()
                if any(p in name_lower for p in ("test", "ut_", "unit_", "perf", "verify")):
                    affected_tests.append({"id": pred, "name": nd.get("name", ""),
                                           "domain": nd.get("domain", "")})
                next_frontier.append(pred)
        frontier = next_frontier

    # Affected domains
    affected_domains = set()
    for fid in affected_funcs:
        affected_domains.add(G.nodes[fid].get("domain", ""))

    result = {
        "changed_function": node_id,
        "total_affected_functions": len(affected_funcs),
        "affected_apis": affected_apis,
        "affected_tests": affected_tests,
        "affected_domains": sorted(affected_domains),
    }
    _output_result(result, getattr(args, 'json', False))


def cmd_field_access(args):
    """Query which functions read/write a specific struct field or global variable.

    Searches fields_read, fields_written, globals_read, and globals_written
    across all nodes. When --struct is provided, only struct field accesses
    matching that struct name are returned. When --field is provided, both
    struct fields and globals matching that name are returned. Results are
    grouped with writers first, then readers.

    Deficiency 1 fix: when code2database.db exists, uses SQL-native indexed
    lookup via query_router.route_field_access (O(log n)) instead of O(n)
    Python traversal of all nodes. Falls back to NetworkX traversal if
    SQLite unavailable or SQL query fails.

    --value filters writes by the assigned RHS expression (e.g., 'NULL' for
    null-pointer-deref analysis). Reads are excluded when --value is set
    since reads don't have an assigned value.
    """
    struct_name = getattr(args, "struct", "")
    field_name = args.field  # required argument
    value_filter = getattr(args, "value", "") or ""

    graph_dir = args.graph

    # Deficiency 1: try SQL-native path first
    try:
        from _builder.query_router import route_field_access, sqlite_available
        if sqlite_available(graph_dir):
            rows = route_field_access(graph_dir, field_name, struct_name,
                                      assigned_value=value_filter)
            if rows is not None:
                writers = [r for r in rows if r.get("access_type") == "write"]
                # When --value is set, only writers are relevant (reads don't have an assigned value)
                readers = [] if value_filter else [r for r in rows if r.get("access_type") == "read"]
                # De-duplicate (same shape as NetworkX path)
                def _dedupe(entries):
                    seen = set()
                    result = []
                    for e in entries:
                        key = (e["function"], e["struct_chain"], e["field_name"], e["access_type"])
                        if key not in seen:
                            seen.add(key)
                            result.append(e)
                    return result
                result = {
                    "struct": struct_name,
                    "field": field_name,
                    "writers": _dedupe(writers),
                    "readers": _dedupe(readers),
                    "_source": "sqlite",  # provenance marker for debugging
                }
                if value_filter:
                    result["value_filter"] = value_filter
                _output_result(result, getattr(args, 'json', False))
                return
    except Exception as exc:
        print(f"[field-access] SQL path failed, falling back to NetworkX: {exc}",
              file=sys.stderr)

    # Fall back to NetworkX full-graph traversal
    G = _load_full_graph(graph_dir)

    readers = []
    writers = []

    def _value_matches(assigned_value: str) -> bool:
        """Check if the assigned_value matches the --value filter (case-insensitive prefix)."""
        if not value_filter:
            return True
        if not assigned_value:
            return False
        av = assigned_value.strip()
        return av == value_filter or av.lower().startswith(value_filter.lower())

    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False) or ndata.get("node_type") == "file":
            continue
        func_name = ndata.get("name", "")
        domain = ndata.get("domain", "")
        source_file = ndata.get("source_file", "")
        line = ndata.get("line", 0)
        thread_model = ndata.get("thread_model", "")

        # Check fields_read — skipped when --value filter is set
        if not value_filter:
            for fr in ndata.get("fields_read", []):
                sc = fr.get("struct_chain", "")
                fn = fr.get("field_name", "")
                struct_match = (not struct_name) or (struct_name == sc) or (struct_name in sc)
                field_match = (not field_name) or (field_name == fn)
                if struct_match and field_match:
                    readers.append({
                        "function": func_name,
                        "domain": domain,
                        "source_file": source_file,
                        "line": line,
                        "access_type": "read",
                        "struct_chain": sc,
                        "field_name": fn,
                        "thread_model": thread_model,
                    })

        # Check fields_written
        for fw in ndata.get("fields_written", []):
            sc = fw.get("struct_chain", "")
            fn = fw.get("field_name", "")
            struct_match = (not struct_name) or (struct_name == sc) or (struct_name in sc)
            field_match = (not field_name) or (field_name == fn)
            if struct_match and field_match:
                av = fw.get("assigned_value", "")
                if not _value_matches(av):
                    continue
                entry = {
                    "function": func_name,
                    "domain": domain,
                    "source_file": source_file,
                    "line": line,
                    "access_type": "write",
                    "struct_chain": sc,
                    "field_name": fn,
                    "thread_model": thread_model,
                }
                if fw.get("target_func"):
                    entry["target_func"] = fw["target_func"]
                if fw.get("is_param"):
                    entry["is_param"] = True
                if av:
                    entry["assigned_value"] = av
                writers.append(entry)

        # Check globals_read — match by variable name against --field
        # Skipped when --value filter is set (reads don't have an assigned value)
        if field_name and not value_filter:
            for gr in ndata.get("globals_read", []):
                gname = gr.get("name", "")
                if field_name == gname:
                    readers.append({
                        "function": func_name,
                        "domain": domain,
                        "source_file": source_file,
                        "line": line,
                        "access_type": "read",
                        "struct_chain": "(global)",
                        "field_name": gname,
                        "thread_model": thread_model,
                    })

            # Check globals_written — match by variable name against --field
            for gw in ndata.get("globals_written", []):
                gname = gw.get("name", "")
                if field_name == gname:
                    writers.append({
                        "function": func_name,
                        "domain": domain,
                        "source_file": source_file,
                        "line": line,
                        "access_type": "write",
                        "struct_chain": "(global)",
                        "field_name": gname,
                        "thread_model": thread_model,
                    })

    # De-duplicate by (function, struct_chain, field_name, access_type)
    def _dedupe(entries):
        seen = set()
        result = []
        for e in entries:
            key = (e["function"], e["struct_chain"], e["field_name"], e["access_type"])
            if key not in seen:
                seen.add(key)
                result.append(e)
        return result

    unique_writers = _dedupe(writers)
    unique_readers = _dedupe(readers)

    result = {
        "struct": struct_name,
        "field": field_name,
        "writers": unique_writers,
        "readers": unique_readers,
        "_source": "networkx",  # provenance marker
    }
    if value_filter:
        result["value_filter"] = value_filter
    _output_result(result, getattr(args, 'json', False))


def cmd_reverse_trace(args):
    """Reverse trace from a crash point: BFS backward through callers,
    annotating each path with condition and concurrency info.

    Traces all paths that can REACH the crash point (reverse direction),
    useful for debugging crashes. Paths are sorted with entry-point
    origins (API_entry, thread_processor) first, then by path length.
    """
    from collections import deque

    graph_dir = args.graph
    crash_point = args.crash_point
    max_depth = getattr(args, 'max_depth', 10)
    max_paths = getattr(args, 'max_paths', 20)
    macros_str = getattr(args, 'macros', '')
    json_mode = getattr(args, 'json', False)

    G = _load_full_graph(graph_dir)
    crash_id = _find_node_id(G, crash_point)
    if not crash_id:
        candidates = [n for n in G.nodes if crash_point.lower() in n.lower()]
        if candidates:
            print(f"Node '{crash_point}' not found. Similar: {candidates[:5]}", file=sys.stderr)
        else:
            print(f"Node '{crash_point}' not found in graph.", file=sys.stderr)
        sys.exit(1)

    globals_map = _load_globals(graph_dir)
    macro_set = set(macros_str.split(",")) if macros_str else set()

    def _macro_alive(cond, mset):
        """Return True if edge condition is compatible with the macro filter."""
        if not cond or not mset:
            return True
        macro_refs = re.findall(r'#ifdef\s+(\w+)|#if\s+defined\((\w+)\)|#if\s+(\w+)', cond)
        for groups in macro_refs:
            for g in groups:
                if g and g not in mset:
                    return False
        return True

    # Reverse BFS from crash_id along predecessor (caller) edges.
    # Record ALL incoming edges per node (not just BFS-tree edges)
    # so we can enumerate all unique paths through fan-in nodes.
    # reverse_edges[invoked_id] = [(invoker_id, edge_data), ...]
    reverse_edges = defaultdict(list)
    queue = deque([(crash_id, 0)])
    visited = {crash_id}

    while queue:
        nid, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for pred in G.predecessors(nid):
            ed = G.get_edge_data(pred, nid) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            cond = ed.get("call_condition", "")
            conc = ed.get("concurrency", "")
            # Apply macro filtering
            if macro_set and not _macro_alive(cond, macro_set):
                continue
            # Record ALL incoming edges for full path enumeration
            reverse_edges[nid].append((pred, ed))
            if pred not in visited:
                visited.add(pred)
                queue.append((pred, depth + 1))

    # Enumerate all unique paths from any entry point to the crash point.
    # Walk backward from crash_id; at each node with multiple callers,
    # branch into all predecessors to collect every distinct path.
    # A path terminates when a node has no recorded reverse_edges (entry point).
    all_paths = []
    _path_count = [0]
    _max_paths = max_paths if 'max_paths' in dir() else 20

    def _collect_paths(node_id, current_path):
        """Recursively collect paths from node_id back to entry points.

        Includes early termination: stops collecting once _max_paths
        is reached, preventing path explosion in high-fan-in graphs.
        """
        if _path_count[0] >= _max_paths:
            return
        preds = reverse_edges.get(node_id, [])
        if not preds:
            if current_path:
                all_paths.append(list(reversed(current_path)))
                _path_count[0] += 1
            return
        for pred_id, ed in preds:
            if _path_count[0] >= _max_paths:
                return
            if pred_id not in visited and pred_id != crash_id:
                continue
            caller_name = G.nodes[pred_id].get("name", pred_id)
            callee_name = G.nodes[node_id].get("name", node_id)
            cond = ed.get("call_condition", "")
            conc = ed.get("concurrency", "")
            conf = ed.get("confidence", "EXTRACTED")
            step = {
                "caller": caller_name,
                "callee": callee_name,
                "condition": cond,
                "concurrency": conc,
                "confidence": conf,
                "invoker_id": pred_id,
                "invoked_id": node_id,
            }
            invoker_ids_in_path = {s["invoker_id"] for s in current_path}
            if pred_id in invoker_ids_in_path:
                continue
            current_path.append(step)
            _collect_paths(pred_id, current_path)
            current_path.pop()

    _collect_paths(crash_id, [])

    # Annotate each path with entry point labels and build sort key
    annotated_paths = []
    for path_steps in all_paths:
        if not path_steps:
            continue
        # The first step's caller is the entry point of this path
        entry_id = path_steps[0]["invoker_id"]
        entry_nd = G.nodes[entry_id]
        entry_labels = entry_nd.get("labels", [])
        is_api_entry = "API_entry" in entry_labels
        is_thread_processor = "thread_processor" in entry_labels
        # Sort key: (0 = API_entry/thread_processor first, 1 = other), then path length
        sort_priority = 0 if (is_api_entry or is_thread_processor) else 1
        # Strip internal IDs from output steps
        clean_steps = []
        for s in path_steps:
            clean_steps.append({
                "caller": s["caller"],
                "callee": s["callee"],
                "condition": s["condition"],
                "concurrency": s["concurrency"],
                "confidence": s["confidence"],
            })
        annotated_paths.append({
            "depth": len(path_steps),
            "steps": clean_steps,
            "entry_id": entry_id,
            "entry_name": entry_nd.get("name", entry_id),
            "entry_labels": entry_labels,
            "_sort_key": (sort_priority, len(path_steps)),
        })

    # Sort: entry-point origins first, then by path length (shortest first)
    annotated_paths.sort(key=lambda p: p["_sort_key"])

    # Apply max_paths limit
    total_paths_before_limit = len(annotated_paths)
    annotated_paths = annotated_paths[:max_paths]

    # Aggregate critical conditions: count how many paths each condition appears in
    condition_path_counts = defaultdict(int)
    for path in annotated_paths:
        seen_in_path = set()
        for step in path["steps"]:
            cond = step["condition"]
            if cond and cond not in seen_in_path:
                seen_in_path.add(cond)
                condition_path_counts[cond] += 1

    # Sort by frequency (most common first)
    critical_conditions = sorted(condition_path_counts.items(), key=lambda x: -x[1])

    # Aggregate concurrency entry points: nodes that spawn threads
    concurrency_entries = []
    seen_spawn_callers = set()
    for path in annotated_paths:
        for step in path["steps"]:
            conc = step["concurrency"]
            caller = step["caller"]
            if conc in ("spawn_target", "thread_spawn", "goroutine") and caller not in seen_spawn_callers:
                seen_spawn_callers.add(caller)
                concurrency_entries.append({
                    "caller": caller,
                    "type": conc,
                    "spawns": step["callee"],
                })

    # Build result
    crash_name = G.nodes[crash_id].get("name", crash_id)
    ancestors = [nid for nid in visited if nid != crash_id]
    result = {
        "crash_point": crash_id,
        "crash_point_name": crash_name,
        "total_reachable_callers": len(ancestors),
        "total_paths": total_paths_before_limit,
        "returned_paths": len(annotated_paths),
        "paths": [],
        "critical_conditions": [
            {"condition": cond, "path_count": count}
            for cond, count in critical_conditions
        ],
        "concurrency_entry_points": concurrency_entries,
    }
    if macros_str:
        result["macros"] = list(macro_set)
    if total_paths_before_limit > max_paths:
        result["path_limit_applied"] = max_paths

    # Format paths for output
    for i, path in enumerate(annotated_paths, 1):
        entry_labels = path["entry_labels"]
        entry_type = ""
        if "API_entry" in entry_labels:
            entry_type = "API_entry"
        elif "thread_processor" in entry_labels:
            entry_type = "thread_processor"
        path_entry = {
            "path_num": i,
            "depth": path["depth"],
            "entry_point": path["entry_name"],
            "steps": path["steps"],
        }
        if entry_type:
            path_entry["entry_type"] = entry_type
        result["paths"].append(path_entry)

    # Text output formatting
    if not json_mode:
        lines = []
        lines.append(f"Reverse trace from: {crash_name}")
        lines.append(f"Total reachable callers: {len(ancestors)}")
        lines.append(f"Total paths: {total_paths_before_limit}"
                     + (f" (showing {max_paths})" if total_paths_before_limit > max_paths else ""))
        lines.append("Paths:")
        for path_entry in result["paths"]:
            entry_type_str = f" [{path_entry['entry_type']}]" if path_entry.get("entry_type") else ""
            lines.append(f"  Path {path_entry['path_num']} (depth {path_entry['depth']}) "
                         f"from {path_entry['entry_point']}{entry_type_str}:")
            for step in path_entry["steps"]:
                cond_str = step["condition"] if step["condition"] else "none"
                conc_str = step["concurrency"] if step["concurrency"] else "none"
                lines.append(f"    {step['caller']} -> {step['callee']} "
                             f"[condition: {cond_str}, concurrency: {conc_str}]")
        if critical_conditions:
            lines.append("Critical conditions:")
            for cond, count in critical_conditions:
                lines.append(f"  - {cond}: appears in {count} path{'s' if count != 1 else ''}")
        if concurrency_entries:
            lines.append("Concurrency entry points:")
            for entry in concurrency_entries:
                lines.append(f"  - {entry['caller']}: spawns thread ({entry['spawns']})")
        print("\n".join(lines))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Deficiency 2: commit-aware provenance queries
# ---------------------------------------------------------------------------

def cmd_describe_commit(args):
    """Describe which nodes/edges a commit affected.

    Usage: describe-commit --commit a1b2c3d4
    Returns: list of nodes changed by this commit, with diff summaries.

    Engineer question: "I just pulled commit a1b2c3d4 — what does the graph
    show as affected?" This replaces manually running `git show a1b2c3d4`
    then grepping the graph.
    """
    graph_dir = args.graph
    commit = args.commit
    if not commit:
        print("Error: --commit is required", file=sys.stderr)
        sys.exit(1)

    # Try SQL path first (query router)
    try:
        from _builder.query_router import _open_store
        store, _ = _open_store(graph_dir)
        if store is not None:
            try:
                rows = store.query_change_log_by_commit(commit)
                result = {
                    "commit": commit,
                    "affected_nodes": rows,
                    "_source": "sqlite",
                }
                _output_result(result, getattr(args, 'json', False))
                return
            finally:
                store.close()
    except Exception as exc:
        print(f"[describe-commit] SQL path failed, falling back: {exc}",
              file=sys.stderr)

    # Fallback: read change_log.json if it exists
    log_path = os.path.join(graph_dir, ".code2database_change_log.json")
    if os.path.exists(log_path):
        try:
            log_data = json.loads(Path(log_path).read_text(encoding="utf-8"))
            entries = [e for e in log_data
                       if e.get("commit_hash") == commit or e.get("commit_short") == commit]
            result = {"commit": commit, "affected_nodes": entries, "_source": "json"}
            _output_result(result, getattr(args, 'json', False))
            return
        except Exception:
            pass

    print(f"No change log found for commit {commit}. "
          f"Run a build with --track-commits to populate change_log.",
          file=sys.stderr)
    sys.exit(1)


def cmd_node_history(args):
    """Show commit history for a node (introduced/modified through commits).

    Usage: node-history --node <node-id>
    Returns: chronological list of commits that touched this node.

    Engineer question: "This function broke — which commits recently changed
    it?" Replaces manually running `git log -- <file>` and trying to map
    commits to function changes.
    """
    graph_dir = args.graph
    node_id = args.node

    # Try SQL path
    try:
        from _builder.query_router import _open_store
        store, _ = _open_store(graph_dir)
        if store is not None:
            try:
                rows = store.query_change_log_by_node(node_id)
                result = {
                    "node": node_id,
                    "history": rows,
                    "_source": "sqlite",
                }
                _output_result(result, getattr(args, 'json', False))
                return
            finally:
                store.close()
    except Exception as exc:
        print(f"[node-history] SQL path failed, falling back: {exc}",
              file=sys.stderr)

    # Fallback: read change_log.json
    log_path = os.path.join(graph_dir, ".code2database_change_log.json")
    if os.path.exists(log_path):
        try:
            log_data = json.loads(Path(log_path).read_text(encoding="utf-8"))
            entries = [e for e in log_data if e.get("node_id") == node_id]
            # Sort by commit_date descending
            entries.sort(key=lambda e: e.get("commit_date", ""), reverse=True)
            result = {"node": node_id, "history": entries, "_source": "json"}
            _output_result(result, getattr(args, 'json', False))
            return
        except Exception:
            pass

    print(f"No change log found for node {node_id}.", file=sys.stderr)
    sys.exit(1)


def cmd_graph_provenance(args):
    """Show which commit the current graph corresponds to.

    Usage: graph-provenance
    Returns: the source_commit from .code2database_manifest.json.

    Engineer question: "Does this graph correspond to main HEAD or my
    feature branch?" This reads manifest.source_commit, not the database
    write time — engineers want the code commit, not the DB timestamp.
    """
    graph_dir = args.graph
    manifest_path = os.path.join(graph_dir, ".code2database_manifest.json")
    if not os.path.exists(manifest_path):
        print(f"No manifest found at {manifest_path}", file=sys.stderr)
        sys.exit(1)

    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Failed to read manifest: {exc}", file=sys.stderr)
        sys.exit(1)

    source_commit = manifest.get("source_commit")
    if not source_commit:
        print("Manifest has no source_commit. Re-run scan to populate.",
              file=sys.stderr)
        sys.exit(1)

    result = {
        "source_root": manifest.get("source_root"),
        "source_commit": source_commit,
        "file_count": len(manifest.get("files", {})),
        "build_timestamp": manifest.get("build_timestamp"),
        "schema_version": manifest.get("schema_version"),
    }
    _output_result(result, getattr(args, 'json', False))


def cmd_blame_node(args):
    """Attribute a node to its introducing/last-modifying commit.

    Usage: blame-node --node <node-id>
    Returns: commit_meta with introduced_commit, last_modified_commit.

    Engineer question: "Who wrote this function, and when?" — but in commit
    terms (git show <hash>), not vague timestamps.
    """
    graph_dir = args.graph
    node_id = args.node

    # Need the source_root to query git
    manifest_path = os.path.join(graph_dir, ".code2database_manifest.json")
    if not os.path.exists(manifest_path):
        print(f"No manifest found at {manifest_path}", file=sys.stderr)
        sys.exit(1)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    source_root = manifest.get("source_root", "")
    if not source_root or not os.path.isdir(source_root):
        print(f"source_root not found or invalid: {source_root}", file=sys.stderr)
        sys.exit(1)

    # Get the node's source file and line
    G = _load_full_graph(graph_dir)
    if node_id not in G:
        candidates = [n for n in G.nodes if node_id.lower() in n.lower()]
        if candidates:
            print(f"Node '{node_id}' not found. Similar: {candidates[:5]}",
                  file=sys.stderr)
        else:
            print(f"Node '{node_id}' not found in graph.", file=sys.stderr)
        sys.exit(1)

    nd = G.nodes[node_id]
    source_file = nd.get("source_file", "")
    line = nd.get("line", 0)
    if not source_file:
        print(f"Node has no source_file attribute", file=sys.stderr)
        sys.exit(1)

    # Resolve absolute path
    if not os.path.isabs(source_file):
        source_file = os.path.join(source_root, source_file)

    try:
        from _builder.commit_meta import commit_meta_for_node
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from _builder.commit_meta import commit_meta_for_node

    meta = commit_meta_for_node(source_root, node_id, source_file, line)
    result = {
        "node": node_id,
        "name": nd.get("name", ""),
        "source_file": source_file,
        "line": line,
        "commit_meta": meta,
    }
    _output_result(result, getattr(args, 'json', False))


def cmd_find_commits(args):
    """Find commits that recently modified a function or file.

    Usage: find-commits --function <name> [--since 2026-07-01] [--limit 20]
    Returns: commits in reverse-chronological order.

    Engineer question: "Show me the last N commits that touched this function."
    """
    graph_dir = args.graph
    func_name = getattr(args, "function", None) or getattr(args, "node", None)
    since = getattr(args, "since", "")
    limit = getattr(args, "limit", 20)

    if not func_name:
        print("Error: --function is required", file=sys.stderr)
        sys.exit(1)

    # Need source_root
    manifest_path = os.path.join(graph_dir, ".code2database_manifest.json")
    if not os.path.exists(manifest_path):
        print(f"No manifest found at {manifest_path}", file=sys.stderr)
        sys.exit(1)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    source_root = manifest.get("source_root", "")
    if not source_root or not os.path.isdir(source_root):
        print(f"source_root not found: {source_root}", file=sys.stderr)
        sys.exit(1)

    # Find the node to get its source file
    G = _load_full_graph(graph_dir)
    node_id = _find_node_id(G, func_name)
    if not node_id:
        print(f"Function '{func_name}' not found in graph.", file=sys.stderr)
        sys.exit(1)

    nd = G.nodes[node_id]
    source_file = nd.get("source_file", "")
    if not source_file:
        print(f"Node has no source_file", file=sys.stderr)
        sys.exit(1)
    if not os.path.isabs(source_file):
        source_file = os.path.join(source_root, source_file)

    rel = os.path.relpath(source_file, source_root) if os.path.isabs(source_file) else source_file

    # Run git log
    import subprocess
    cmd_args = ["git", "-c", "core.pager=cat", "--no-pager", "log",
                f"--pretty=format:%H|%h|%an|%aI|%s", f"-{limit}"]
    if since:
        cmd_args.append(f"--since={since}")
    cmd_args += ["--", rel]

    try:
        result = subprocess.run(cmd_args, cwd=source_root, capture_output=True,
                                text=True, timeout=30, check=False)
        if result.returncode != 0:
            print(f"git log failed: {result.stderr}", file=sys.stderr)
            sys.exit(1)
    except Exception as exc:
        print(f"git log error: {exc}", file=sys.stderr)
        sys.exit(1)

    commits = []
    for line in result.stdout.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|", 4)
        if len(parts) >= 5:
            commits.append({
                "commit": parts[0], "commit_short": parts[1],
                "author": parts[2], "date": parts[3], "subject": parts[4],
            })

    out = {
        "function": func_name,
        "node_id": node_id,
        "source_file": rel,
        "commits": commits,
        "_source": "git",
    }
    _output_result(out, getattr(args, 'json', False))


