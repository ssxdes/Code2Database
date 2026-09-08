"""query.query_describe — split from query.py."""

"""callgraph builder module: query."""

import os
import json
import sys
import re
from pathlib import Path
from collections import defaultdict
import networkx as nx
from _builder.utils import _is_condition_alive, _output_result, _find_node_id, _parse_bindings, _load_globals, _streaming_json_lookup, _streaming_json_has_keys
from _builder.query.query_helpers import _fetch_foreign_refs_for_node, _is_vtable_dispatch_alive, _get_code_snippet, _compute_exec_summary, _resolve_simple_chain, _compute_hub_info, _describe_node_touched

from _builder.graph.graph_build import _load_full_graph
from _builder.token_budget import estimate_tokens, truncate_to_tokens, budget_describe
from _builder.query.query_cache import cached_query, invalidate_node as _cache_invalidate_node


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
    snippet_lines = getattr(args, "snippet", 0) or 0

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
        from _builder.ops.patcher import lazy_fill_node
        source_root = ""
        master_path = os.path.join(graph_dir, "code2database_master.json")
        if os.path.exists(master_path):
            try:
                import json as _json
                master = _json.loads(Path(master_path).read_text(encoding="utf-8"))
                source_root = master.get("source_root", "")
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
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
        # Handle both on-disk formats:
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
            # Detect file format to avoid loading 500+MB unnecessarily.
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

    # surface related memory/knowledge entries so the LLM
    # knows what we already know about this node. Uses the unified
    # kb_paragraphs FTS5 index; falls back silently if no DB.
    try:
        from _builder.kb.kb_index import query_kb
        # Use node name + signature as the query — broad recall,
        # LLM can filter further.
        node_name = nd.get("name", node_id)
        node_query = node_name
        sig = nd.get("signature", "")
        if sig:
            node_query += " " + sig
        kb_hits = query_kb(graph_dir, node_query, top_n=3,
                           log_query=False, max_tokens=1000)
        if kb_hits:
            result["memory_refs"] = [h for h in kb_hits
                                      if h.get("source_kind") == "memory"]
            result["knowledge_refs"] = [h for h in kb_hits
                                        if h.get("source_kind") == "knowledge"]
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    # callees) appears in the foreign_refs table, ATTACH the foreign C2D
    # and fetch the foreign node's metadata (name, source_file, signature,
    # body_text). This lets describe-node work seamlessly across C2Ds —
    # the LLM sees one unified view without knowing the data is split.
    try:
        result["foreign_refs"] = _fetch_foreign_refs_for_node(graph_dir, node_id)
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
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

    # --snippet: include source code snippet around the function definition.
    # Runs for all detail levels (brief/standard/full) so callers can get
    # source context without a separate get-code-snippet call.
    # Falls back to source_snippet if already extracted at scan time (D16).
    if snippet_lines > 0:
        existing_snippet = nd.get("source_snippet", "")
        if existing_snippet:
            result["source_snippet"] = existing_snippet
        else:
            try:
                snippet_result = _get_code_snippet(G, node_id,
                                                    context_lines=snippet_lines,
                                                    graph_dir=graph_dir)
                if "error" not in snippet_result:
                    result["source_snippet"] = snippet_result.get("snippet", "")
                    result["source_file"] = snippet_result.get("source_file", "")
                    result["line"] = snippet_result.get("line", 0)
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
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
    # default hidden to avoid 60%+ output token waste on
    # analysis-only queries. Use --fill-requests to show.
    if getattr(args, "fill_requests", False):
        try:
            from _builder.build.auto_enhance import compute_fill_request
            fill_request = compute_fill_request(nd)
            if fill_request:
                result["auto_fill_request"] = fill_request
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass

    # Doc-code alignment — if this node has any doc-code
    # mismatches (return value, param name, signature change, stale doc),
    # surface them so the LLM knows the doc may be unreliable.
    doc_stale = nd.get("doc_stale", False)
    if doc_stale:
        result["doc_stale"] = True
        result["doc_stale_reason"] = nd.get("doc_stale_reason", "")
    try:
        from _builder.misc.doc_code_align import (
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
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass

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
        # Detect file format — SQLite build lacks concurrent_groups key.
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





def _cmd_trace_chain_touched(args) -> frozenset:
    """Nodes a trace-chain query depends on — used by query cache for invalidation."""
    try:
        ids = set()
        for attr in ("from_node", "to_node"):
            v = getattr(args, attr, "") or ""
            if v:
                ids.add(v)
        return frozenset(ids) if ids else frozenset()
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        return frozenset()


@cached_query('trace-chain', ttl=600,
              touched_nodes_fn=_cmd_trace_chain_touched,
              capture_stdout=True)

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


