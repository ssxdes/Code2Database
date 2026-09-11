"""query.query_io_flow — split from query.py."""

"""callgraph builder module: query."""

import os
import json
import sys
import re
import logging
from pathlib import Path
from collections import defaultdict
import networkx as nx
from _builder.utils import _is_condition_alive, _output_result, _find_node_id, _parse_bindings, _load_globals, _streaming_json_lookup, _streaming_json_has_keys
from _builder.query.query_helpers import _is_vtable_dispatch_alive, _get_code_snippet, _value_is_null_form_match, _load_profile_from_graph_dir, _io_path_bfs, _io_path_score, _collect_dispatch_info, _value_is_null_form

from _builder.graph.graph_build import _load_full_graph
from _builder.token_budget import estimate_tokens, truncate_to_tokens, budget_describe
from _builder.query.query_cache import cached_query, invalidate_node as _cache_invalidate_node



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
        from _builder.ops.update_cmd import _confirm, _detect_backend
        if not _confirm(prompt, auto_yes):
            print("[persist] write aborted by user.")
            return

        backend = _detect_backend(graph_dir)
        attrs = {"body_text": snippet_text}
        if backend == "json":
            from _builder.ops.update_cmd import _json_update_node
            ok = _json_update_node(graph_dir, node_id, attrs,
                                   source="llm_supplement", confidence="EXTRACTED")
        else:
            from _builder.ops.update_cmd import _sqlite_update_node
            ok = _sqlite_update_node(graph_dir, node_id, attrs,
                                     source="llm_supplement", confidence="EXTRACTED")
        if ok:
            print(f"[persist] Wrote {len(snippet_text)} chars to body_text_supplemented "
                  f"for node {node_id}")



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
    visited = set()
    flow_steps = []
    # Each queue item: (function_id, param_to_track, depth, path_so_far)
    queue = [(from_id, param_name, 0, [from_id])]

    while queue:
        cur_id, cur_param, depth, path = queue.pop(0)
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

    When code2database.db exists, uses SQL-native indexed
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

    # try SQL-native path first
    try:
        from _builder.query.query_router import route_field_access, sqlite_available
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
        """Check if the assigned_value matches the --value filter (case-insensitive prefix).

        IMPROVE-1+: Special-case NULL detection — recognize all C forms of NULL:
          - `NULL`, `0`, `(void *)0`, `(struct foo *)0`, `((void *)0)`, `0L`, etc.
        This is critical for null-pointer-deref analysis where the bug report says
        "who set field to NULL" but the source uses `(struct block_device *)0`.
        """
        if not value_filter:
            return True
        if not assigned_value:
            return False
        av = assigned_value.strip()
        # Direct match or prefix match (existing behavior)
        if av == value_filter or av.lower().startswith(value_filter.lower()):
            return True
        # NULL-form equivalence: when user asks for "NULL", match any C null form
        vf_upper = value_filter.upper()
        if vf_upper in ("NULL", "0", "((VOID*)0)", "((VOID *)0)"):
            av_compact = av.replace(" ", "").upper()
            # Strip outer parens repeatedly for compact comparison
            while av_compact.startswith("(") and av_compact.endswith(")"):
                av_compact = av_compact[1:-1]
            # Forms: 0, 0L, NULL, (void*)0, (structfoo*)0, ((void*)0)
            if av_compact == "0" or av_compact == "0L" or av_compact == "NULL":
                return True
            if av_compact.endswith("*0)"):
                # e.g., (structblockdevice*)0 — pointer cast to 0
                return True
            if av_compact.endswith("*0L)"):
                return True
        return False

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


# IMPROVE-1+: NULL-form equivalence helpers shared by cmd_field_flow's SQL
# retry path and NetworkX fallback. Recognize all C forms of NULL so that
# `field-flow --value NULL` matches `(struct block_device *)0`, `((void *)0)`,
# `0`, `0L`, etc. — critical for null-pointer-deref analysis where the bug
# report says "who set field to NULL" but the source uses pointer-cast zero.
# (The NULL-form pattern itself lives in query_helpers next to its users.)



def cmd_field_flow(args):
    """Trace field writes + their reverse call chains.

    Combines `field-access` (who writes field X) with `reverse-trace`
    (how is each writer reached from an entry point). Designed for
    null-pointer-deref / use-after-free / race root-cause analysis:
        field-flow --field b_bdev --value NULL
    returns every function that sets bh->b_bdev = NULL, plus the call
    chain from each entry point that reaches that writer.

    Output schema:
        {
          "struct": "...", "field": "...", "value_filter": "NULL",
          "writers": [
            { "function": "discard_buffer", "source_file": "fs/buffer.c",
              "line": 1558, "assigned_value": "NULL",
              "call_chains": [
                [ "discard_buffer", "invalidate_bh_lru", "__blkdev_put", ... ],
                ...
              ]
            }, ...
          ],
          "summary": { "writer_count": N, "total_chains": M }
        }
    """
    from collections import deque

    graph_dir = args.graph
    struct_name = getattr(args, "struct", "")
    field_name = args.field
    # null-source alias: auto-set --value NULL if invoked as null-source
    value_filter = getattr(args, "value", "") or ""
    if not value_filter and getattr(args, "command", "") == "null-source":
        value_filter = "NULL"
    max_depth = getattr(args, "max_depth", 8)
    max_paths_per_writer = getattr(args, "max_paths_per_writer", 5)
    json_mode = getattr(args, "json", False)

    # Step 1: reuse field-access SQL path to get writers
    writers_rows = None
    try:
        from _builder.query.query_router import route_field_access, sqlite_available
        if sqlite_available(graph_dir):
            writers_rows = route_field_access(graph_dir, field_name, struct_name,
                                              assigned_value=value_filter)
            # IMPROVE-1+: NULL-form equivalence — if user passed --value NULL/0 but
            # the SQL path returned no rows, retry with empty filter and post-filter
            # using _value_is_null_form(). The SQL path uses exact prefix match
            # which misses (struct foo *)0.
            if value_filter and not writers_rows:
                vf_upper = value_filter.upper().replace(" ", "")
                null_query = vf_upper in ("NULL", "0", "((VOID*)0)")
                if null_query:
                    all_writers = route_field_access(graph_dir, field_name, struct_name,
                                                      assigned_value="")
                    writers_rows = [r for r in (all_writers or [])
                                    if _value_is_null_form(r.get("assigned_value", ""))]
    except Exception:
        writers_rows = None

    # Fall back to NetworkX traversal if SQL path unavailable
    if writers_rows is None:
        G_full = _load_full_graph(graph_dir)
        writers_rows = []
        for nid, ndata in G_full.nodes(data=True):
            if ndata.get("is_empty", False) or ndata.get("node_type") == "file":
                continue
            func_name = ndata.get("name", "")
            for fw in ndata.get("fields_written", []):
                sc = fw.get("struct_chain", "")
                fn = fw.get("field_name", "")
                struct_match = (not struct_name) or (struct_name == sc) or (struct_name in sc)
                field_match = (not field_name) or (field_name == fn)
                if not (struct_match and field_match):
                    continue
                av = fw.get("assigned_value", "")
                if value_filter:
                    av_matches = (av and (av == value_filter or av.lower().startswith(value_filter.lower())))
                    if not av_matches and _value_is_null_form_match(av, value_filter):
                        av_matches = True
                    if not av_matches:
                        continue
                writers_rows.append({
                    "function": func_name,
                    "domain": ndata.get("domain", ""),
                    "source_file": ndata.get("source_file", ""),
                    "line": ndata.get("line", 0),
                    "struct_chain": sc,
                    "field_name": fn,
                    "access_type": "write",
                    "assigned_value": av,
                    "thread_model": ndata.get("thread_model", ""),
                    "_node_id": nid,
                })
            for gw in ndata.get("globals_written", []):
                gname = gw.get("name", "")
                if field_name != gname:
                    continue
                writers_rows.append({
                    "function": func_name,
                    "domain": ndata.get("domain", ""),
                    "source_file": ndata.get("source_file", ""),
                    "line": ndata.get("line", 0),
                    "struct_chain": "(global)",
                    "field_name": gname,
                    "access_type": "write",
                    "thread_model": ndata.get("thread_model", ""),
                    "_node_id": nid,
                })

    if not writers_rows:
        result = {
            "struct": struct_name,
            "field": field_name,
            "value_filter": value_filter,
            "writers": [],
            "summary": {"writer_count": 0, "total_chains": 0},
            "note": "No writers found for the given field/value combination.",
        }
        _output_result(result, json_mode)
        return

    # Step 2: load graph once and reverse-BFS from each writer
    G = _load_full_graph(graph_dir)

    def _resolve_nid(func_name):
        """Find node ID by function name (exact, then case-insensitive, then substring)."""
        if func_name in G:
            return func_name
        for nid in G:
            if nid.lower() == func_name.lower():
                return nid
        for nid in G:
            nd = G.nodes[nid]
            if nd.get("name", "") == func_name:
                return nid
        return None

    def _reverse_bfs_chains(start_id, depth, max_paths):
        """BFS backward through caller edges. Return up to max_paths chains
        ending at an entry-point (API_entry, thread_processor) or depth limit."""
        chains = []
        queue = deque([(start_id, [start_id])])
        seen_paths = set()
        while queue and len(chains) < max_paths:
            nid, path = queue.popleft()
            if len(path) - 1 >= depth:
                # Reached depth limit — record this chain
                key = tuple(path)
                if key not in seen_paths:
                    seen_paths.add(key)
                    chains.append(list(path))
                continue
            nd = G.nodes[nid]
            labels = nd.get("labels", [])
            # Entry-point origins stop the chain (they're the root cause source)
            is_entry = ("API_entry" in labels or "thread_processor" in labels)
            preds = list(G.predecessors(nid))
            # Filter out non-call edges
            call_preds = []
            for p in preds:
                ed = G.get_edge_data(p, nid) or {}
                if ed.get("relation") not in ("CONTAINS", "IMPORTS"):
                    call_preds.append(p)
            if not call_preds or is_entry:
                key = tuple(path)
                if key not in seen_paths:
                    seen_paths.add(key)
                    chains.append(list(path))
                continue
            for p in call_preds:
                if p in path:  # cycle guard
                    continue
                queue.append((p, [p] + path))
        return chains[:max_paths]

    # Build writer entries with call chains
    writer_entries = []
    total_chains = 0
    for row in writers_rows:
        func_name = row.get("function", "")
        nid = row.get("_node_id") or _resolve_nid(func_name)
        entry = {
            "function": func_name,
            "domain": row.get("domain", ""),
            "source_file": row.get("source_file", ""),
            "line": row.get("line", 0),
            "struct_chain": row.get("struct_chain", ""),
            "field_name": row.get("field_name", ""),
            "assigned_value": row.get("assigned_value", ""),
            "thread_model": row.get("thread_model", ""),
        }
        # Look up guard_condition from the graph node data.
        # The SQL path doesn't include guard_condition (schema not migrated),
        # so we look it up from the NetworkX graph's fields_written list.
        # This is the key signal for distinguishing real bugs from false
        # positives: a writer guarded by !sb_is_blkdev_sb() is unreachable
        # during ext4 mount, so it cannot be a real NULL-deref suspect.
        # Also look up object_origin — captures the source chain
        # (e.g., "jh->bh") so the agent can compare writer vs reader object
        # origins to detect when they operate on different objects.
        guard_condition = ""
        object_origin = ""
        if nid:
            ndata = G.nodes.get(nid, {})
            for fw in ndata.get("fields_written", []):
                if (fw.get("struct_chain", "") == entry["struct_chain"]
                        and fw.get("field_name", "") == entry["field_name"]):
                    guard_condition = fw.get("guard_condition", "")
                    object_origin = fw.get("object_origin", "")
                    break
        if guard_condition:
            entry["guard_condition"] = guard_condition
            entry["reachable_in_scene"] = "guarded"
        else:
            entry["reachable_in_scene"] = "unguarded"
        if object_origin:
            entry["object_origin"] = object_origin
        if nid:
            chains = _reverse_bfs_chains(nid, max_depth, max_paths_per_writer)
            entry["call_chains"] = chains
            total_chains += len(chains)
            # Surface entry-point origins for quick race-window reasoning
            entry["entry_origins"] = list({c[0] for c in chains if c})
        else:
            entry["call_chains"] = []
            entry["entry_origins"] = []
        writer_entries.append(entry)

    # Sort writers by chain count (most reachable first) then by name
    writer_entries.sort(key=lambda w: (-len(w.get("call_chains", [])), w["function"]))

    # IMPROVE-5: When --value is a NULL-form, also return vulnerable readers
    # — functions that READ the field (potentially dereferencing NULL).
    # This closes the loop on null-pointer-deref analysis: writers + readers
    # together define the race window. Without this, the agent must do a
    # separate `field-access` call and manually correlate.
    #
    # For each reader, compute `concurrent_writers_in_scene`
    # and `race_window_exists`. A writer is "in scene" if reachable_in_scene
    # == "unguarded" (no guard protects it). A writer is "concurrent" if it
    # is in a different thread context from the reader. race_window_exists
    # is True only if at least one writer is both concurrent AND in scene.
    # This is the key signal from KASAN_FINAL_REPORT: all 6 writers are
    # either guarded out or in the same thread context → no race window.
    vulnerable_readers = []
    _reader_collection_error = None
    if value_filter and _value_is_null_form(value_filter):
        try:
            from _builder.query.query_router import route_field_access, sqlite_available
            from _builder.analysis.concurrency_analysis import _same_thread_context
            reader_rows = None
            if sqlite_available(graph_dir):
                reader_rows = route_field_access(graph_dir, field_name, struct_name,
                                                  assigned_value="")
            if reader_rows is None:
                # NetworkX fallback — read fields_read from each node
                for nid, ndata in G.nodes(data=True):
                    if ndata.get("is_empty", False) or ndata.get("node_type") == "file":
                        continue
                    func_name = ndata.get("name", "")
                    for fr in ndata.get("fields_read", []):
                        sc = fr.get("struct_chain", "")
                        fn = fr.get("field_name", "")
                        struct_match = (not struct_name) or (struct_name == sc) or (struct_name in sc)
                        field_match = (not field_name) or (field_name == fn)
                        if struct_match and field_match:
                            vr_entry = {
                                "_node_id": nid,
                                "function": func_name,
                                "domain": ndata.get("domain", ""),
                                "source_file": ndata.get("source_file", ""),
                                "line": ndata.get("line", 0),
                                "struct_chain": sc,
                                "field_name": fn,
                                "thread_model": ndata.get("thread_model", ""),
                            }
                            # Capture object_origin for readers
                            # so agent can compare writer's object_origin
                            # vs reader's object_origin to detect different
                            # objects (e.g., bh from bdev->bd_inode->i_mapping
                            # vs ext4_inode->i_mapping).
                            oo = fr.get("object_origin", "")
                            if oo:
                                vr_entry["object_origin"] = oo
                            vulnerable_readers.append(vr_entry)
            else:
                for r in reader_rows:
                    if r.get("access_type") == "read":
                        # Look up node_id for concurrency check
                        r_nid = _resolve_nid(r.get("function", ""))
                        r["_node_id"] = r_nid
                        vulnerable_readers.append(r)
        except Exception as exc:
            # Reader collection failed mid-way: the already-collected
            # readers are still reported, but an incomplete
            # vulnerable_readers list looks like a clean all-clear —
            # record the degradation in the result instead of a
            # debug-only log.
            logging.getLogger(__name__).warning(
                "field-flow reader collection failed: %s", exc,
                exc_info=True)
            _reader_collection_error = str(exc)
        # A writer is "concurrent" if in a different thread context.
        # A writer is "in scene" if reachable_in_scene == "unguarded".
        # race_window_exists = any writer is both concurrent AND in scene.
        for vr in vulnerable_readers:
            vr_nid = vr.pop("_node_id", None)
            vr_ndata = G.nodes.get(vr_nid, {}) if vr_nid else {}
            concurrent_writers = []
            for w in writer_entries:
                if w.get("reachable_in_scene") != "unguarded":
                    continue  # guarded out → not in scene
                w_nid = _resolve_nid(w.get("function", ""))
                if not w_nid:
                    continue
                w_ndata = G.nodes.get(w_nid, {})
                if not _same_thread_context(vr_ndata, w_ndata):
                    concurrent_writers.append({
                        "function": w.get("function", ""),
                        "assigned_value": w.get("assigned_value", ""),
                        "thread_model": w.get("thread_model", ""),
                    })
            vr["concurrent_writers_in_scene"] = concurrent_writers
            vr["race_window_exists"] = len(concurrent_writers) > 0

    result = {
        "struct": struct_name,
        "field": field_name,
        "value_filter": value_filter,
        "writers": writer_entries,
        "summary": {
            "writer_count": len(writer_entries),
            "total_chains": total_chains,
        },
    }
    if _reader_collection_error is not None:
        # Reader analysis degraded (exception mid-collection): the list
        # above may be incomplete — flag it so an empty-looking result
        # isn't mistaken for "no vulnerable readers".
        result["reader_collection_error"] = _reader_collection_error
        result["summary"]["reader_analysis_degraded"] = True
    if vulnerable_readers:
        result["vulnerable_readers"] = vulnerable_readers
        result["summary"]["vulnerable_reader_count"] = len(vulnerable_readers)
    _output_result(result, json_mode)



def _cmd_reverse_trace_touched(args) -> frozenset:
    """Nodes a reverse-trace query depends on — used by query cache for invalidation."""
    try:
        v = getattr(args, 'crash_point', None) or getattr(args, 'from_node', None) or ""
        if v:
            return frozenset({v})
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    return frozenset()


@cached_query('reverse-trace', ttl=600,
              touched_nodes_fn=_cmd_reverse_trace_touched,
              capture_stdout=True)

def cmd_reverse_trace(args):
    """Reverse trace from a crash point: BFS backward through callers,
    annotating each path with condition and concurrency info.

    Traces all paths that can REACH the crash point (reverse direction),
    useful for debugging crashes. Paths are sorted with entry-point
    origins (API_entry, thread_processor) first, then by path length.
    """
    from collections import deque

    graph_dir = args.graph
    # Accept either --crash-point or --from (unified CLI convention)
    crash_point = getattr(args, 'crash_point', None) or getattr(args, 'from_node', None)
    if not crash_point:
        print("Error: --crash-point (or --from) is required", file=sys.stderr)
        sys.exit(1)
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
    # Bound the enumeration: path count grows exponentially with fan-in,
    # and only max_paths (default 20) survive the sort+truncate below —
    # without a cap a dense subgraph enumerated millions of paths first.
    _PATH_CAP = 10000

    def _collect_paths(node_id, current_path):
        """Recursively collect paths from node_id back to entry points."""
        if len(all_paths) >= _PATH_CAP:
            return
        preds = reverse_edges.get(node_id, [])
        if not preds:
            # This is an entry point (no callers in reverse BFS range)
            if current_path:
                all_paths.append(list(reversed(current_path)))
            return
        for pred_id, ed in preds:
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
            # Avoid cycles in path
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
    path_enumeration_capped = total_paths_before_limit >= 10000
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

    # FIELD_WRITE suspects integration.
    # When --suspect-field is set, query field_access for all writers of that
    # field and include them as suspects in the reverse-trace output. This
    # closes the gap that reverse-trace could see callers of the crash point
    # but not the field-write suspects that may have caused the crash.
    # See 续篇 report field-flow / value-flow 数据未构建 (P0).
    field_write_suspects = []
    suspect_field = getattr(args, 'suspect_field', None)
    suspect_value = getattr(args, 'suspect_value', None)
    suspect_struct = getattr(args, 'suspect_struct', None)
    if suspect_field:
        try:
            from _builder.query.query_router import route_field_access, sqlite_available
            suspect_rows = None
            if sqlite_available(graph_dir):
                suspect_rows = route_field_access(graph_dir, suspect_field,
                                                  suspect_struct or "",
                                                  assigned_value=suspect_value or "")
            # NetworkX fallback — scan fields_written on each node
            if suspect_rows is None:
                suspect_rows = []
                for nid, ndata in G.nodes(data=True):
                    if ndata.get("is_empty", False) or ndata.get("node_type") == "file":
                        continue
                    func_name = ndata.get("name", "")
                    for fw in ndata.get("fields_written", []):
                        sc = fw.get("struct_chain", "")
                        fn = fw.get("field_name", "")
                        struct_match = (not suspect_struct) or (suspect_struct == sc) or (suspect_struct in sc)
                        field_match = (suspect_field == fn)
                        if not (struct_match and field_match):
                            continue
                        av = fw.get("assigned_value", "")
                        if suspect_value:
                            av_matches = (av and (av == suspect_value or av.lower().startswith(suspect_value.lower())))
                            if not av_matches and _value_is_null_form_match(av, suspect_value):
                                av_matches = True
                            if not av_matches:
                                continue
                        suspect_rows.append({
                            "function": func_name,
                            "domain": ndata.get("domain", ""),
                            "source_file": ndata.get("source_file", ""),
                            "line": ndata.get("line", 0),
                            "struct_chain": sc,
                            "field_name": fn,
                            "access_type": "write",
                            "assigned_value": av,
                            "thread_model": ndata.get("thread_model", ""),
                            "_node_id": nid,
                            "_guard_condition": fw.get("guard_condition", ""),
                            "_object_origin": fw.get("object_origin", ""),
                        })

            # Build suspect entries with reverse-BFS call chains
            from collections import deque as _deque
            def _resolve_nid_local(func_name):
                if func_name in G:
                    return func_name
                for nid in G:
                    if nid.lower() == func_name.lower():
                        return nid
                for nid in G:
                    if G.nodes[nid].get("name", "") == func_name:
                        return nid
                return None

            def _reverse_bfs_suspect_chains(start_id, depth, max_paths):
                """BFS backward through caller edges from a suspect writer."""
                chains = []
                queue = _deque([(start_id, [start_id])])
                seen_paths = set()
                while queue and len(chains) < max_paths:
                    nid, path = queue.popleft()
                    if len(path) - 1 >= depth:
                        key = tuple(path)
                        if key not in seen_paths:
                            seen_paths.add(key)
                            chains.append(list(path))
                        continue
                    nd = G.nodes[nid]
                    labels = nd.get("labels", [])
                    is_entry = ("API_entry" in labels or "thread_processor" in labels)
                    call_preds = []
                    for p in G.predecessors(nid):
                        ed = G.get_edge_data(p, nid) or {}
                        if ed.get("relation") not in ("CONTAINS", "IMPORTS"):
                            call_preds.append(p)
                    if not call_preds or is_entry:
                        key = tuple(path)
                        if key not in seen_paths:
                            seen_paths.add(key)
                            chains.append(list(path))
                        continue
                    for p in call_preds:
                        if p in path:
                            continue
                        queue.append((p, [p] + path))
                return chains[:max_paths]

            for row in suspect_rows:
                func_name = row.get("function", "")
                nid = row.get("_node_id") or _resolve_nid_local(func_name)
                suspect_entry = {
                    "function": func_name,
                    "domain": row.get("domain", ""),
                    "source_file": row.get("source_file", ""),
                    "line": row.get("line", 0),
                    "struct_chain": row.get("struct_chain", ""),
                    "field_name": row.get("field_name", ""),
                    "assigned_value": row.get("assigned_value", ""),
                    "thread_model": row.get("thread_model", ""),
                }
                # Attach guard_condition and object_origin (from NetworkX fallback
                # or looked up from fields_written). These are the key signals
                # for distinguishing real bugs from false positives.
                guard_condition = row.get("_guard_condition", "")
                object_origin = row.get("_object_origin", "")
                if not (guard_condition or object_origin) and nid:
                    ndata = G.nodes.get(nid, {})
                    for fw in ndata.get("fields_written", []):
                        if (fw.get("struct_chain", "") == suspect_entry["struct_chain"]
                                and fw.get("field_name", "") == suspect_entry["field_name"]):
                            guard_condition = guard_condition or fw.get("guard_condition", "")
                            object_origin = object_origin or fw.get("object_origin", "")
                            break
                if guard_condition:
                    suspect_entry["guard_condition"] = guard_condition
                    suspect_entry["reachable_in_scene"] = "guarded"
                else:
                    suspect_entry["reachable_in_scene"] = "unguarded"
                if object_origin:
                    suspect_entry["object_origin"] = object_origin
                if nid:
                    chains = _reverse_bfs_suspect_chains(nid, max_depth, max_paths)
                    suspect_entry["call_chains"] = chains
                    suspect_entry["entry_origins"] = list({c[0] for c in chains if c})
                else:
                    suspect_entry["call_chains"] = []
                    suspect_entry["entry_origins"] = []
                field_write_suspects.append(suspect_entry)

            # Sort suspects by chain count (most reachable first)
            field_write_suspects.sort(key=lambda w: (-len(w.get("call_chains", [])), w["function"]))
        except Exception as exc:
            # Don't let suspect integration break the main reverse-trace
            print(f"[reverse-trace] suspect-field integration failed: {exc}",
                  file=sys.stderr)

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
    if path_enumeration_capped:
        result["path_enumeration_capped"] = True
        result["note"] = ("path enumeration capped at 10000 — the graph "
                          "has more distinct paths than counted; tighten "
                          "the query (labels/depth) for exact totals")
    if field_write_suspects:
        result["field_write_suspects"] = field_write_suspects
        result["field_write_suspects_summary"] = {
            "suspect_count": len(field_write_suspects),
            "unguarded_count": sum(
                1 for s in field_write_suspects
                if s.get("reachable_in_scene") == "unguarded"
            ),
            "field": suspect_field or "",
            "value_filter": suspect_value or "",
            "struct_filter": suspect_struct or "",
        }

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
        if field_write_suspects:
            summary = result.get("field_write_suspects_summary", {})
            lines.append("")
            lines.append(
                f"Field write suspects (, field={summary.get('field', '')},"
                f"value_filter={summary.get('value_filter', '') or 'none'},"
                f"struct_filter={summary.get('struct_filter', '') or 'none'}):"
            )
            lines.append(
                f"  Total: {summary.get('suspect_count', 0)} suspect(s), "
                f"{summary.get('unguarded_count', 0)} unguarded"
            )
            for idx, sus in enumerate(field_write_suspects, 1):
                func = sus.get("function", "?")
                reach = sus.get("reachable_in_scene", "unknown")
                chains = sus.get("call_chains", [])
                guard = sus.get("guard_condition", "") or "none"
                origin = sus.get("object_origin", "") or "unknown"
                lines.append(
                    f"  {idx}. {func} [reach={reach}, chains={len(chains)}, "
                    f"guard={guard}, origin={origin}]"
                )
                for ci, chain in enumerate(chains[:3], 1):
                    chain_str = " -> ".join(chain) if isinstance(chain, list) else str(chain)
                    lines.append(f"     chain {ci}: {chain_str}")
        print("\n".join(lines))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# commit-aware provenance queries
# ---------------------------------------------------------------------------

