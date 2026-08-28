"""Explain-label and why-ambiguous commands.

Provides "why" queries on top of the existing "what" queries:
- explain-label: explain why a node has a particular label (dead_code,
  API_entry, thread_processor, callback_func, constructor, destructor,
  out_end, unknown_end, ffi_boundary, file, in_end, race_risk, etc.)
- why-ambiguous: explain why an edge is marked AMBIGUOUS (fn_ptr dispatch,
  preprocessor dead branch, vtable dispatch, etc.)

The evidence chain is built from the node/edge attributes themselves — no
external reasoning needed. This gives LLM users a way to ask "why is X
classified as Y?" without reading the source of the classification logic.
"""
import sys
import json
from typing import Dict

from _builder.graph_build import _load_full_graph
from _builder.utils import _find_node_id


# ---------------------------------------------------------------------------
# Label explanation rules
# ---------------------------------------------------------------------------

LABEL_EXPLANATIONS = {
    "API_entry": {
        "summary": "Function is a public entry point — externally callable",
        "evidence_keys": ["is_api_entry", "entry_score", "labels_source"],
        "rules": [
            ("entry_score >= threshold",
             "The function scored above the API-entry threshold in entry_scoring"),
            ("no internal callers",
             "The function has no callers within the scanned codebase"),
            ("externally visible name",
             "The function name matches a registration/export pattern"),
        ],
    },
    "thread_processor": {
        "summary": "Function runs as a thread main or is dispatched to a thread",
        "evidence_keys": ["thread_model", "thread_entry", "labels_source"],
        "rules": [
            ("thread_model set",
             "The function has a thread_model attribute (kthread/workqueue/tasklet/etc.)"),
            ("spawn target",
             "The function is the target of a thread-spawning call (kthread_run, pthread_create, etc.)"),
        ],
    },
    "callback_func": {
        "summary": "Function is registered as a callback (invoked indirectly)",
        "evidence_keys": ["labels_source", "callback_registration"],
        "rules": [
            ("registered as callback",
             "The function is passed as an argument to a registration function (e.g., register_handler(&foo))"),
            ("callback naming",
             "The function name matches a callback naming pattern (ends in _cb, _handler, _done, etc.)"),
        ],
    },
    "constructor": {
        "summary": "Function is a class/struct constructor",
        "evidence_keys": ["labels_source"],
        "rules": [
            ("AST node type",
             "The function was parsed as a constructor_definition AST node"),
            ("naming convention",
             "The function name matches the enclosing class/struct name"),
        ],
    },
    "destructor": {
        "summary": "Function is a class/struct destructor",
        "evidence_keys": ["labels_source"],
        "rules": [
            ("AST node type",
             "The function was parsed as a destructor_definition AST node"),
            ("naming convention",
             "The function name starts with ~ or matches the destroy pattern"),
        ],
    },
    "out_end": {
        "summary": "Function is a leaf in the invocation graph (no callees)",
        "evidence_keys": [],
        "rules": [
            ("zero out-edges",
             "The function has no INVOKES edges to other functions in the graph"),
        ],
    },
    "unknown_end": {
        "summary": "Function calls only unknown/external callees",
        "evidence_keys": [],
        "rules": [
            ("all callees unresolved",
             "All callee names could not be resolved to graph nodes (external library calls)"),
        ],
    },
    "in_end": {
        "summary": "Function is a leaf that is reached but calls nothing",
        "evidence_keys": [],
        "rules": [
            ("zero out-edges with callers",
             "The function has callers but no callees"),
        ],
    },
    "dead_code": {
        "summary": "Function is excluded by preprocessor conditions",
        "evidence_keys": ["preproc_condition", "preproc_alive"],
        "rules": [
            ("preprocessor guard inactive",
             "All #ifdef / #if conditions guarding this function evaluate to false in the current build config"),
            ("no live callers",
             "All call sites are inside dead preprocessor branches"),
        ],
    },
    "ffi_boundary": {
        "summary": "Function is a foreign-function-interface boundary",
        "evidence_keys": ["ffi_role", "ffi_binding"],
        "rules": [
            ("FFI binding detected",
             "The function participates in a ctypes / cgo / extern \"C\" / pybind11 binding"),
        ],
    },
    "file": {
        "summary": "Node represents a file (not a function)",
        "evidence_keys": [],
        "rules": [
            ("file-level node",
             "This node was created to represent a source file (e.g., for deleted-file tracking)"),
        ],
    },
    "race_risk": {
        "summary": "Function participates in a potential data race",
        "evidence_keys": ["race_risks"],
        "rules": [
            ("shared access without synchronization",
             "The function reads/writes shared state that another concurrent function also accesses, with no shared lock"),
            ("detected by detect-races",
             "The detect-races command flagged this function in a race report"),
        ],
    },
}


def explain_label(G, node_id: str, label: str) -> Dict:
    """Explain why a node has a given label.

    Returns a dict with:
        label: the queried label
        node: node id
        summary: human-readable explanation
        evidence: list of (rule, explanation) pairs that applied
        available_evidence_attrs: which node attrs support the explanation
    """
    if node_id not in G.nodes:
        return {"error": f"node {node_id!r} not found in graph"}

    nd = G.nodes[node_id]
    labels = nd.get("labels", []) or []
    has_label = label in labels

    explanation = LABEL_EXPLANATIONS.get(label, {
        "summary": f"Label {label!r} is not a recognized built-in label",
        "evidence_keys": [],
        "rules": [("unknown label",
                   "This label is not in the built-in LABEL_EXPLANATIONS table; "
                   "it may be project-specific")],
    })

    # Build evidence: which rules fired based on node attrs
    evidence = []
    for rule_name, rule_expl in explanation["rules"]:
        # Generic rule firing: include if any of the evidence_keys have data
        if rule_name == "zero out-edges":
            out_edges = [(u, v) for u, v in G.out_edges(node_id)
                         if (G.get_edge_data(u, v) or {}).get("relation")
                         not in ("CONTAINS", "IMPORTS")]
            if not out_edges:
                evidence.append({"rule": rule_name, "explanation": rule_expl,
                                 "data": {"out_edges": 0}})
        elif rule_name == "zero out-edges with callers":
            out_edges = list(G.out_edges(node_id))
            in_edges = list(G.in_edges(node_id))
            if not out_edges and in_edges:
                evidence.append({"rule": rule_name, "explanation": rule_expl,
                                 "data": {"out_edges": 0, "in_edges": len(in_edges)}})
        elif rule_name == "all callees unresolved":
            callees = list(G.successors(node_id))
            if not callees:
                evidence.append({"rule": rule_name, "explanation": rule_expl,
                                 "data": {"callees": 0}})
        elif rule_name == "preprocessor guard inactive":
            pp = nd.get("preproc_condition") or ""
            alive = nd.get("preproc_alive", True)
            if pp and not alive:
                evidence.append({"rule": rule_name, "explanation": rule_expl,
                                 "data": {"preproc_condition": pp,
                                          "preproc_alive": False}})
        elif rule_name == "no live callers":
            in_edges = list(G.in_edges(node_id))
            dead_callers = 0
            for caller, _ in in_edges:
                ed = G.get_edge_data(caller, node_id) or {}
                if ed.get("preproc_alive") is False:
                    dead_callers += 1
            if in_edges and dead_callers == len(in_edges):
                evidence.append({"rule": rule_name, "explanation": rule_expl,
                                 "data": {"total_callers": len(in_edges),
                                          "dead_callers": dead_callers}})
        elif rule_name == "FFI binding detected":
            if nd.get("ffi_role") or nd.get("ffi_binding"):
                evidence.append({"rule": rule_name, "explanation": rule_expl,
                                 "data": {"ffi_role": nd.get("ffi_role"),
                                          "ffi_binding": nd.get("ffi_binding")}})
        elif rule_name == "registered as callback":
            cb_reg = nd.get("callback_registration") or {}
            if cb_reg:
                evidence.append({"rule": rule_name, "explanation": rule_expl,
                                 "data": cb_reg})
        elif rule_name == "callback naming":
            name = nd.get("name", "")
            if any(name.endswith(s) for s in ("_cb", "_callback", "_handler",
                                              "_done", "_fn", "_event")):
                evidence.append({"rule": rule_name, "explanation": rule_expl,
                                 "data": {"name": name}})
        elif rule_name == "thread_model set":
            if nd.get("thread_model"):
                evidence.append({"rule": rule_name, "explanation": rule_expl,
                                 "data": {"thread_model": nd.get("thread_model")}})
        elif rule_name == "spawn target":
            if nd.get("thread_entry") or nd.get("is_spawn_target"):
                evidence.append({"rule": rule_name, "explanation": rule_expl,
                                 "data": {"thread_entry": nd.get("thread_entry")}})
        elif rule_name == "entry_score >= threshold":
            es = nd.get("entry_score", 0)
            if es and es > 0:
                evidence.append({"rule": rule_name, "explanation": rule_expl,
                                 "data": {"entry_score": es}})
        elif rule_name == "shared access without synchronization":
            race_risks = nd.get("race_risks") or []
            if race_risks:
                evidence.append({"rule": rule_name, "explanation": rule_expl,
                                 "data": {"race_risks": race_risks[:3]}})
        elif rule_name == "AST node type":
            ls = nd.get("labels_source") or {}
            if label in ls:
                evidence.append({"rule": rule_name, "explanation": rule_expl,
                                 "data": ls.get(label, {})})
        elif rule_name == "detected by detect-races":
            if nd.get("race_risks"):
                evidence.append({"rule": rule_name, "explanation": rule_expl,
                                 "data": {"race_risks_count": len(nd["race_risks"])}})

    return {
        "node": node_id,
        "name": nd.get("name", ""),
        "label": label,
        "has_label": has_label,
        "summary": explanation["summary"],
        "evidence": evidence,
        "all_labels_on_node": labels,
        "labels_source": nd.get("labels_source", {}),
    }


def why_ambiguous(G, edge_from: str, edge_to: str) -> Dict:
    """Explain why an edge is marked AMBIGUOUS.

    Returns a dict with:
        edge: (from, to)
        confidence: confidence on the edge
        reasons: list of explanation strings
        evidence: structured evidence (kind, weight, note)
    """
    if edge_from not in G.nodes:
        return {"error": f"node {edge_from!r} not found"}
    if edge_to not in G.nodes:
        return {"error": f"node {edge_to!r} not found"}
    if not G.has_edge(edge_from, edge_to):
        return {"error": f"no edge from {edge_from!r} to {edge_to!r}"}

    ed = G.get_edge_data(edge_from, edge_to) or {}
    confidence = ed.get("confidence", "EXTRACTED")
    reasons = []
    raw_evidence = ed.get("evidence", []) or []
    # Normalize evidence entries: they may be dicts (preferred) or strings
    # (legacy form). A single string is treated as one entry; a list of
    # strings becomes one entry per string. Character-by-character
    # splitting must be avoided.
    evidence = []
    if isinstance(raw_evidence, str):
        evidence.append({"kind": "string", "note": raw_evidence})
    elif isinstance(raw_evidence, list):
        for e in raw_evidence:
            if isinstance(e, dict):
                evidence.append(e)
            elif isinstance(e, str) and len(e) > 1:
                evidence.append({"kind": "string", "note": e})

    if confidence != "AMBIGUOUS":
        return {
            "edge": [edge_from, edge_to],
            "confidence": confidence,
            "is_ambiguous": False,
            "reasons": [f"Edge confidence is {confidence}, not AMBIGUOUS"],
            "evidence": evidence,
        }

    # Identify the AMBIGUOUS reason from evidence kinds
    fn_ptr_evidence = [e for e in evidence if e.get("kind") == "fn_ptr_call"]
    preproc_dead_evidence = [e for e in evidence if e.get("kind") == "ast_call"
                             and e.get("weight") == 0.0]
    vtable_dispatch = ed.get("relation") == "callback_dispatch"

    if fn_ptr_evidence:
        for ev in fn_ptr_evidence:
            reasons.append(
                f"Indirect call via function pointer: {ev.get('note', 'unknown')}. "
                "The actual callee is determined at runtime through a function "
                "pointer field; the static invocation graph cannot resolve which "
                "concrete function is invoked.")
    if preproc_dead_evidence:
        for ev in preproc_dead_evidence:
            reasons.append(
                f"Dead preprocessor branch: {ev.get('note', 'unknown')}. "
                "The call site is inside an #ifdef / #if branch that evaluates "
                "to false in the current build configuration.")
    if vtable_dispatch:
        reasons.append(
            "Vtable dispatch: this edge represents a dynamic dispatch through "
            "a vtable / ops structure. Multiple concrete callees may be "
            "invoked depending on the runtime type of the object.")
    if ed.get("concurrency") == "fn_ptr":
        reasons.append(
            "Function pointer call: the callee name was extracted from a "
            "function pointer expression (e.g., `ops->read`), but the actual "
            "concrete function invoked depends on what was assigned to the "
            "function pointer at runtime. Use `find-invariants` or look at "
            "the function pointer's assignment sites for the dispatch set.")
    if ed.get("concurrency") == "callback":
        reasons.append(
            "Callback registration: the callee is registered as a callback "
            "and invoked indirectly when the registering function dispatches "
            "the event.")
    # Also detect string-evidence reasons
    for ev in evidence:
        if ev.get("kind") == "string":
            note = ev.get("note", "")
            if "fn_ptr_call" in note or "fn_ptr" in note:
                if not any("Function pointer" in r for r in reasons):
                    reasons.append(
                        f"Function pointer call (from evidence): {note}. "
                        "The static analyzer could not resolve the concrete "
                        "callee; the dispatch set is determined by what's "
                        "assigned to the function pointer at runtime.")

    if not reasons:
        reasons.append(
            "Edge is marked AMBIGUOUS but no specific reason was recorded "
            "in the evidence chain. This may be a heuristic fallback.")

    return {
        "edge": [edge_from, edge_to],
        "confidence": confidence,
        "is_ambiguous": True,
        "reasons": reasons,
        "evidence": evidence,
        "edge_data": {k: v for k, v in ed.items()
                      if k not in ("evidence",)},
    }


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_explain_label(args):
    """Explain why a node has a given label.

    Examples:
      explain-label --graph out/ --node my_func --label dead_code
    """
    graph_dir = args.graph
    node_hint = args.node
    label = args.label

    G = _load_full_graph(graph_dir)
    node_id = _find_node_id(G, node_hint)
    if not node_id:
        print(f"Error: node matching {node_hint!r} not found", file=sys.stderr)
        sys.exit(1)

    result = explain_label(G, node_id, label)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_why_ambiguous(args):
    """Explain why an edge is marked AMBIGUOUS.

    Examples:
      why-ambiguous --graph out/ --from caller --to callee
    """
    graph_dir = args.graph
    from_hint = args.from_node
    to_hint = args.to_node

    G = _load_full_graph(graph_dir)
    from_id = _find_node_id(G, from_hint)
    to_id = _find_node_id(G, to_hint)
    if not from_id:
        print(f"Error: 'from' node matching {from_hint!r} not found",
              file=sys.stderr)
        sys.exit(1)
    if not to_id:
        print(f"Error: 'to' node matching {to_hint!r} not found",
              file=sys.stderr)
        sys.exit(1)

    result = why_ambiguous(G, from_id, to_id)
    print(json.dumps(result, indent=2, ensure_ascii=False))
