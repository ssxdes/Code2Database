"""Query helper functions — shared between query split modules.

Split from query.py to avoid circular imports between query.py and its
split modules (query_describe.py, query_io_flow.py, query_provenance.py).
"""

import json
import os
import sys
import re
import logging
from pathlib import Path
from collections import defaultdict, deque
import networkx as nx

from _builder.utils import (
    _resolve_invoked_id, _is_condition_alive, _output_result, _find_node_id,
    _parse_bindings, _load_globals, _streaming_json_lookup, _streaming_json_has_keys,
)
from _builder.graph.graph_build import _load_full_graph
from _builder.token_budget import estimate_tokens, truncate_to_tokens, budget_describe
from _builder.query.query_cache import cached_query


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
    from _builder.build.auto_enhance import _is_likely_builtin
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
    except (TypeError, AttributeError):
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
    except (IOError, OSError) as e:
        import logging
        logging.getLogger(__name__).error(
            "Could not read profile %s: %s — query results will lack "
            "profile-driven features (vtable dispatch, callback patterns, "
            "API prefix matching, etc.)", profile_path, e)
        return None
    except ValueError as e:
        import logging
        logging.getLogger(__name__).error(
            "Corrupt profile JSON at %s: %s — query results will lack "
            "profile-driven features. Fix the JSON syntax error and rebuild.",
            profile_path, e)
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
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
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




def _fetch_foreign_refs_for_node(graph_dir: str, node_id: str) -> list:
    """F1: fetch foreign_ref metadata for a node's cross-C2D callees.

    If the node has edges to foreign_ref stubs (entries in the
    foreign_refs table), ATTACH the foreign C2D db and fetch the
    callee's metadata (name, source_file, signature). Returns a list
    of dicts with foreign callee info, transparent to the LLM.
    """
    import sqlite3 as _sqlite3
    db_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.exists(db_path):
        return []
    conn = _sqlite3.connect(db_path)
    conn.row_factory = _sqlite3.Row
    refs = []
    try:
        # Check if foreign_refs table exists
        try:
            rows = conn.execute(
                "SELECT foreign_c2d_path, foreign_node_id, foreign_name, "
                "foreign_domain, foreign_source_file, foreign_signature, "
                "status, resolution_strategy "
                "FROM foreign_refs WHERE local_node_id = ? "
                "AND status IN ('resolved', 'stale')",
                (node_id,)
            ).fetchall()
        except _sqlite3.OperationalError:
            return []  # foreign_refs table doesn't exist
        for r in rows:
            entry = {
                "foreign_c2d_path": r["foreign_c2d_path"],
                "foreign_node_id": r["foreign_node_id"],
                "foreign_name": r["foreign_name"],
                "foreign_domain": r["foreign_domain"],
                "foreign_source_file": r["foreign_source_file"],
                "foreign_signature": r["foreign_signature"],
                "status": r["status"],
                "resolution_strategy": r["resolution_strategy"],
            }
            refs.append(entry)
    finally:
        conn.close()
    return refs


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
        # i is the 1-indexed file line number (range starts at start+1);
        # do NOT add another +1 here — the old f"{i+1:4d}" labeled every
        # line one higher than its real file line.
        "snippet": "\n".join(f"{i:4d} | {line}" for i, line in zip(range(start + 1, end + 1), snippet_lines)),
    }
    return result




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


# C NULL-form pattern: `0`, `0L`, `(void *)0`, `(struct foo *)0`, with
# optional nesting/whitespace. Shared by _value_is_null_form and
# _value_is_null_form_match below (field-flow / path-guards value filters).
_NULL_FORM_RE = re.compile(
    r"^\(*\s*(?:void\s*\*|[A-Za-z_][A-Za-z0-9_ ]*\*\s*|\s*)\)*0(L?)\s*\)*$",
    re.IGNORECASE,
)


def _value_is_null_form(assigned_value: str) -> bool:
    """Return True if assigned_value is any C form of NULL.

    Recognized forms (case-insensitive, whitespace-ignored):
      - `NULL`, `0`, `0L`
      - `(void *)0`, `(void*)0`, `((void *)0)`, `((void*)0)`
      - `(struct foo *)0`, `(struct foo *)0L` — any pointer-cast-to-zero
    """
    if not assigned_value:
        return False
    av = assigned_value.strip()
    if av == "NULL":
        return True
    return bool(_NULL_FORM_RE.match(av))




def _value_is_null_form_match(assigned_value: str, value_filter: str) -> bool:
    """Return True if value_filter is a NULL-form query AND assigned_value is NULL.

    A NULL-form query is one of: `NULL`, `0`, `0L`, `(void *)0` (case-insensitive).
    When the user asks for any of these, match any C NULL form in the source.
    """
    if not value_filter or not assigned_value:
        return False
    vf = value_filter.strip()
    if vf.upper() == "NULL" or _NULL_FORM_RE.match(vf):
        return _value_is_null_form(assigned_value)
    return False




