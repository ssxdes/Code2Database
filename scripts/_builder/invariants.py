#!/usr/bin/env python3
"""Invariant extraction for Code2Database.

Models three kinds of invariants that are critical for bug-hunting but
absent from the existing graph:

1. **precondition**: must hold before a function is called.
   Engineer question: "Can I call this with x == NULL?" — if the
   precondition says `x != NULL`, the answer is no, and a caller that
   passes NULL is a bug.

2. **postcondition**: holds after a function returns.
   Engineer question: "After init() returns, is ctx->state guaranteed
   to be READY?" — the postcondition tells you. If a caller assumes
   READY but the postcondition doesn't guarantee it, that's a bug.

3. **loop_invariant**: holds at the top of every loop iteration.
   Engineer question: "Does this loop preserve i < n?" — the loop
   invariant proves the loop terminates and doesn't overflow.

4. **state_machine**: for state variables (e.g., `ctx->state`), model
   the state machine: which functions transition which states? Which
   states are reachable? This answers "can ctx->state ever be FAILED
   after init()?".

Static, regex/AST-light extraction — sound for common patterns, with
confidence labels. LLM enhancement is supported via extract-invariants
/ apply-invariants (mirrors the existing extract-semantics flow).

The graph stores invariants as node attributes:
    nd["preconditions"] = [
        {"condition": "x != NULL", "evidence": "if (x == NULL) return -EINVAL",
         "line": 12, "confidence": "EXTRACTED", "source": "guard_check"}
    ]
    nd["postconditions"] = [
        {"condition": "ctx->state == READY", "evidence": "ctx->state = READY; return 0;",
         "line": 45, "confidence": "EXTRACTED", "source": "explicit_assign"}
    ]
    nd["loop_invariants"] = [
        {"condition": "i < n", "evidence": "for (i = 0; i < n; i++)",
         "line": 20, "confidence": "EXTRACTED", "source": "loop_header"}
    ]
    nd["state_machine"] = {
        "var": "ctx->state",
        "states": ["UNINIT", "READY", "RUNNING", "DONE"],
        "transitions": [
            {"function": "init", "from": "UNINIT", "to": "READY", "line": 10},
            {"function": "start", "from": "READY", "to": "RUNNING", "line": 25},
        ],
    }
"""

import json
import os
import re
import sys
from collections import defaultdict
from typing import Optional, List, Dict, Set, Tuple

from _builder.line_utils import build_line_starts, line_for_offset


# ---------------------------------------------------------------------------
# Precondition extraction
# ---------------------------------------------------------------------------

# Pattern: function entry guard `if (param == NULL) return -EINVAL;`
# Common precondition patterns at function start.
_PRECOND_PATTERNS = [
    # NULL checks on parameters
    (re.compile(
        r'\bif\s*\(\s*(\w+)\s*==\s*NULL\s*\)\s*return\s+(-E\w+|-?\d+)\s*;',
        re.MULTILINE),
     "param != NULL", "null_check"),
    (re.compile(
        r'\bif\s*\(\s*!\s*(\w+)\s*\)\s*return\s+(-E\w+|-?\d+)\s*;',
        re.MULTILINE),
     "param is truthy", "truthy_check"),
    # Range checks
    (re.compile(
        r'\bif\s*\(\s*(\w+)\s*<\s*(\d+)\s*\)\s*return\s+(-E\w+|-?\d+)\s*;',
        re.MULTILINE),
     "param >= N", "lower_bound"),
    (re.compile(
        r'\bif\s*\(\s*(\w+)\s*>\s*(\d+)\s*\)\s*return\s+(-E\w+|-?\d+)\s*;',
        re.MULTILINE),
     "param <= N", "upper_bound"),
    # Type/flag checks
    (re.compile(
        r'\bif\s*\(\s*(\w+)\s*==\s*(\d+|0x[0-9a-fA-F]+)\s*\)\s*return\s+(-E\w+|-?\d+)\s*;',
        re.MULTILINE),
     "param != N", "value_check"),
]


def extract_preconditions(body_text: str, params: List[Dict] = None) -> List[Dict]:
    """Extract preconditions from a function body.

    Looks for early-return guard clauses at the function entry — these
    encode what the caller MUST guarantee before calling.

    Returns a list of {condition, evidence, line, confidence, source} dicts.
    """
    if not body_text:
        return []
    results = []
    param_names = {p.get("name") for p in (params or []) if p.get("name")}
    _line_starts = build_line_starts(body_text)

    for pat, cond_template, source in _PRECOND_PATTERNS:
        for m in pat.finditer(body_text):
            line_no = line_for_offset(_line_starts, m.start())
            # The captured first group is the variable being checked
            var = m.group(1)
            # Only treat as precondition if it's a parameter (not a local)
            if param_names and var not in param_names:
                # Still include it but with lower confidence — could be a
                # local that depends on a parameter indirectly.
                confidence = "INFERRED"
            else:
                confidence = "EXTRACTED"
            # Build the condition string
            if source == "null_check":
                cond = f"{var} != NULL"
            elif source == "truthy_check":
                cond = f"{var} is truthy"
            elif source == "lower_bound":
                cond = f"{var} >= {m.group(2)}"
            elif source == "upper_bound":
                cond = f"{var} <= {m.group(2)}"
            elif source == "value_check":
                cond = f"{var} != {m.group(2)}"
            else:
                cond = cond_template
            results.append({
                "condition": cond,
                "evidence": m.group(0).strip(),
                "line": line_no,
                "confidence": confidence,
                "source": source,
            })
    return results


# ---------------------------------------------------------------------------
# Postcondition extraction
# ---------------------------------------------------------------------------

# Pattern: explicit assignment to a state field before return
# e.g., `ctx->state = READY; return 0;`
_POSTCOND_STATE_ASSIGN = re.compile(
    r'(\w+(?:\s*(?:->|\.)\s*\w+)+)\s*=\s*([A-Z_][A-Z0-9_]*)\s*;'
    r'(?:[^;{]*?)?return\s+([^;{]+);',
    re.MULTILINE | re.DOTALL
)

# Pattern: simple `return 0;` at end implies success postcondition
_RETURN_SUCCESS = re.compile(
    r'\breturn\s+(0|SUCCESS|OK|0;\s*//\s*success)',
    re.IGNORECASE
)

# Pattern: return value indicates error
_RETURN_ERROR = re.compile(
    r'\breturn\s+(-E\w+|-1|FAILED|ERROR)',
    re.IGNORECASE
)


def extract_postconditions(body_text: str) -> List[Dict]:
    """Extract postconditions from a function body.

    Looks for state assignments before return statements and for
    return-value patterns that imply success/failure.

    Returns a list of {condition, evidence, line, confidence, source} dicts.
    """
    if not body_text:
        return []
    results = []
    _line_starts = build_line_starts(body_text)
    # State-assignment postconditions
    for m in _POSTCOND_STATE_ASSIGN.finditer(body_text):
        line_no = line_for_offset(_line_starts, m.start())
        results.append({
            "condition": f"{m.group(1)} == {m.group(2)}",
            "evidence": m.group(0).strip()[:200],
            "line": line_no,
            "confidence": "EXTRACTED",
            "source": "state_assign_before_return",
        })
    # Look for typical success path: returns 0 explicitly
    for m in _RETURN_SUCCESS.finditer(body_text):
        line_no = line_for_offset(_line_starts, m.start())
        results.append({
            "condition": "returns success (0)",
            "evidence": m.group(0).strip(),
            "line": line_no,
            "confidence": "EXTRACTED",
            "source": "return_success",
        })
    return results


# ---------------------------------------------------------------------------
# Loop invariant extraction
# ---------------------------------------------------------------------------

# Pattern: `for (init; cond; update)` — the cond IS the loop invariant
# (or its negation; either way it's the loop-bound property).
_FOR_LOOP = re.compile(
    r'\bfor\s*\(\s*([^;]*);\s*([^;]*);\s*([^)]*)\)',
    re.MULTILINE
)

# Pattern: `while (cond)` — the cond is the loop guard / invariant
_WHILE_LOOP = re.compile(
    r'\bwhile\s*\(\s*([^)]+)\)',
    re.MULTILINE
)


def extract_loop_invariants(body_text: str) -> List[Dict]:
    """Extract loop invariants from a function body.

    For `for (i = 0; i < n; i++)`, the loop invariant is `i < n` (the
    condition that must hold for the loop to continue, and is preserved
    across iterations for terminating loops).

    Returns a list of {condition, evidence, line, confidence, source} dicts.
    """
    if not body_text:
        return []
    results = []
    _line_starts = build_line_starts(body_text)
    for m in _FOR_LOOP.finditer(body_text):
        line_no = line_for_offset(_line_starts, m.start())
        cond = m.group(2).strip()
        if not cond:
            continue
        results.append({
            "condition": cond,
            "evidence": m.group(0).strip()[:200],
            "line": line_no,
            "confidence": "EXTRACTED",
            "source": "for_loop_header",
        })
    for m in _WHILE_LOOP.finditer(body_text):
        line_no = line_for_offset(_line_starts, m.start())
        cond = m.group(1).strip()
        if not cond:
            continue
        results.append({
            "condition": cond,
            "evidence": m.group(0).strip(),
            "line": line_no,
            "confidence": "EXTRACTED",
            "source": "while_loop_header",
        })
    return results


# ---------------------------------------------------------------------------
# State machine extraction
# ---------------------------------------------------------------------------

# Find state assignments to a common state variable
_STATE_ASSIGN = re.compile(
    r'(\w+(?:\s*(?:->|\.)\s*\w+)+)\s*=\s*([A-Z_][A-Z0-9_]*)\s*;'
)

# Find state checks: `if (var == STATE)` or `switch (var) { case STATE:`
_STATE_CHECK = re.compile(
    r'(?:if\s*\(\s*(\w+(?:\s*(?:->|\.)\s*\w+)+)\s*==\s*([A-Z_][A-Z0-9_]*)\s*\)|'
    r'case\s+([A-Z_][A-Z0-9_]*)\s*:)'
)


def extract_state_machine(body_text: str, function_name: str = "",
                          line_offset: int = 0) -> Optional[Dict]:
    """Extract a state machine from a function body.

    Heuristic: find the most-frequently-assigned state variable, then
    collect all states and transitions involving it.

    Returns None if no clear state machine is found, or:
        {
            "var": "ctx->state",
            "states": ["UNINIT", "READY", ...],
            "transitions": [
                {"from": "...", "to": "...", "line": N},
                ...
            ],
        }
    """
    if not body_text:
        return None

    # Count state assignments per variable
    assign_counts: Dict[str, int] = defaultdict(int)
    assignments: List[Tuple[str, str, int]] = []  # (var, value, line)
    _line_starts = build_line_starts(body_text)
    for m in _STATE_ASSIGN.finditer(body_text):
        var = m.group(1).replace(" ", "")
        val = m.group(2)
        line_no = line_for_offset(_line_starts, m.start()) + line_offset
        assign_counts[var] += 1
        assignments.append((var, val, line_no))

    if not assignments:
        return None

    # Pick the most-assigned variable as the state var
    state_var = max(assign_counts.keys(), key=lambda v: assign_counts[v])
    if assign_counts[state_var] < 1:
        # Need at least 1 assignment to have any transition
        return None

    states: Set[str] = set()
    transitions: List[Dict] = []
    # Each assignment is a transition TO this state; FROM is whatever
    # the previous state was (we can't know without control-flow, so
    # mark FROM as unknown unless a check precedes it).
    prev_state: Optional[str] = None
    for var, val, line in assignments:
        if var != state_var:
            continue
        states.add(val)
        # Look at the line above to see if there's a state check
        # (heuristic: `if (var == X)` immediately before this assignment)
        from_state = prev_state
        # Find the source line text
        lines = body_text.split("\n")
        idx = line - 1 - line_offset
        if idx > 0:
            prev_line = lines[idx - 1] if idx - 1 < len(lines) else ""
            m_check = re.search(
                state_var.replace("->", r"\s*->\s*").replace(".", r"\s*\.\s*") +
                r'\s*==\s*([A-Z_][A-Z0-9_]*)',
                prev_line
            )
            if m_check:
                from_state = m_check.group(1)
                states.add(from_state)
        transitions.append({
            "function": function_name,
            "from": from_state or "*",
            "to": val,
            "line": line,
        })
        prev_state = val

    return {
        "var": state_var,
        "states": sorted(states),
        "transitions": transitions,
    }


# ---------------------------------------------------------------------------
# Combined invariant extraction for a single node
# ---------------------------------------------------------------------------

def extract_invariants_for_node(ndata: Dict) -> Dict:
    """Extract all invariants from a single node's data.

    Returns:
        {
            "preconditions": [...],
            "postconditions": [...],
            "loop_invariants": [...],
            "state_machine": {...} or None,
        }
    """
    body = ndata.get("body_text", "")
    params = ndata.get("params", []) or []
    return {
        "preconditions": extract_preconditions(body, params),
        "postconditions": extract_postconditions(body),
        "loop_invariants": extract_loop_invariants(body),
        "state_machine": extract_state_machine(body, ndata.get("name", "")),
    }


# ---------------------------------------------------------------------------
# Whole-graph extraction + apply
# ---------------------------------------------------------------------------

def build_invariants_for_graph(G) -> Dict:
    """Extract invariants for all non-empty nodes in the graph.

    Returns:
        {
            "node_id": {
                "preconditions": [...],
                "postconditions": [...],
                "loop_invariants": [...],
                "state_machine": {...} or None,
            },
            ...
        }
    """
    results = {}
    for nid, nd in G.nodes(data=True):
        if nd.get("is_empty", False) or nd.get("node_type") == "file":
            continue
        if not nd.get("body_text", ""):
            continue
        inv = extract_invariants_for_node(nd)
        # Only include if at least one invariant found
        if (inv["preconditions"] or inv["postconditions"]
                or inv["loop_invariants"] or inv.get("state_machine")):
            results[nid] = inv
    return results


def attach_invariants_to_graph(G, invariants: Dict[str, Dict]):
    """Attach invariants dict to each node as node attributes in-place.

    Stores as:
        nd["preconditions"] = [...]
        nd["postconditions"] = [...]
        nd["loop_invariants"] = [...]
        nd["state_machine"] = {...}
    Plus provenance:
        nd["_invariant_meta"] = {"source": "static_analysis", ...}
    """
    for nid, inv in invariants.items():
        if nid not in G:
            continue
        nd = G.nodes[nid]
        nd["preconditions"] = inv.get("preconditions", [])
        nd["postconditions"] = inv.get("postconditions", [])
        nd["loop_invariants"] = inv.get("loop_invariants", [])
        nd["state_machine"] = inv.get("state_machine")
        nd["_invariant_meta"] = {
            "source": "static_analysis",
            "precondition_count": len(nd["preconditions"]),
            "postcondition_count": len(nd["postconditions"]),
            "loop_invariant_count": len(nd["loop_invariants"]),
            "has_state_machine": bool(nd["state_machine"]),
        }


# ---------------------------------------------------------------------------
# Query: find-invariants
# ---------------------------------------------------------------------------

def find_invariants(G, var_pattern: str = "", value_pattern: str = "",
                    invariant_kind: str = "") -> List[Dict]:
    """Find functions that guarantee a given invariant.

    Args:
        var_pattern: variable name pattern (e.g., 'ctx->state' or 'config')
        value_pattern: value pattern (e.g., 'READY', 'NULL', '!= NULL')
        invariant_kind: 'precondition' / 'postcondition' / 'loop_invariant'
            (empty = search all)

    Returns a list of {function, function_id, kind, condition, line, ...}.

    Engineer question: "Which functions guarantee ctx->state == READY
    after they return?" → find-invariants --var ctx->state --value READY
    --kind postcondition
    """
    results = []
    for nid, nd in G.nodes(data=True):
        if nd.get("is_empty", False) or nd.get("node_type") == "file":
            continue
        # Iterate through the requested invariant kinds
        kinds = [invariant_kind] if invariant_kind else [
            "precondition", "postcondition", "loop_invariant"
        ]
        for kind in kinds:
            entries = nd.get(f"{kind}s", []) or []
            # state_machine is singular
            if kind == "state_machine" and nd.get("state_machine"):
                entries = [nd["state_machine"]]
            for entry in entries:
                cond = entry.get("condition", "") if isinstance(entry, dict) else ""
                if not cond:
                    continue
                # Filter by var pattern
                if var_pattern and var_pattern not in cond:
                    continue
                # Filter by value pattern
                if value_pattern and value_pattern not in cond:
                    continue
                results.append({
                    "function": nd.get("name", nid),
                    "function_id": nid,
                    "kind": kind,
                    "condition": cond,
                    "line": entry.get("line", 0) if isinstance(entry, dict) else 0,
                    "confidence": entry.get("confidence", "") if isinstance(entry, dict) else "",
                    "evidence": entry.get("evidence", "") if isinstance(entry, dict) else "",
                })
    return results


def find_state_machine_for_var(G, var_pattern: str) -> Dict:
    """Find and merge all state machines for a given state variable.

    Combines per-function state machines into a global state machine
    showing all states and all transitions across the codebase.

    Returns:
        {
            "var": "ctx->state",
            "states": [...],
            "transitions": [
                {"function": "init", "from": "UNINIT", "to": "READY", "line": 10},
                ...
            ],
            "transition_functions": ["init", "start", "stop"],
        }
    """
    states: Set[str] = set()
    transitions: List[Dict] = []
    transition_functions: Set[str] = set()
    for nid, nd in G.nodes(data=True):
        sm = nd.get("state_machine")
        if not sm:
            continue
        if var_pattern not in sm.get("var", ""):
            continue
        for s in sm.get("states", []):
            states.add(s)
        for t in sm.get("transitions", []):
            transitions.append(t)
            transition_functions.add(t.get("function", ""))
    return {
        "var": var_pattern,
        "states": sorted(states),
        "transitions": transitions,
        "transition_functions": sorted(transition_functions),
    }


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_extract_invariants(args):
    """Extract invariants from all functions and write to a JSON file
    for review (or apply directly with --apply).

    Usage:
        extract-invariants --graph <dir>                    # extract + write JSON
        extract-invariants --graph <dir> --apply            # extract + attach to graph
        extract-invariants --graph <dir> --node <id>        # extract for one node
    """
    graph_dir = args.graph
    try:
        from _builder.graph_build import _load_full_graph
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from _builder.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)

    node_filter = getattr(args, "node", "")
    if node_filter:
        from _builder.utils import _find_node_id
        nid = _find_node_id(G, node_filter)
        if not nid:
            print(f"Node not found: {node_filter}", file=sys.stderr)
            sys.exit(1)
        inv = extract_invariants_for_node(G.nodes[nid])
        result = {"node_id": nid, "name": G.nodes[nid].get("name", ""), **inv}
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    # Whole graph
    invariants = build_invariants_for_graph(G)
    out_path = os.path.join(graph_dir, ".code2database_invariants.json")
    summary = {
        "node_count": len(invariants),
        "total_preconditions": sum(len(v["preconditions"]) for v in invariants.values()),
        "total_postconditions": sum(len(v["postconditions"]) for v in invariants.values()),
        "total_loop_invariants": sum(len(v["loop_invariants"]) for v in invariants.values()),
        "total_state_machines": sum(1 for v in invariants.values() if v.get("state_machine")),
        "invariants": invariants,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    print(f"Extracted invariants: {summary['node_count']} nodes, "
          f"{summary['total_preconditions']} preconditions, "
          f"{summary['total_postconditions']} postconditions, "
          f"{summary['total_loop_invariants']} loop invariants, "
          f"{summary['total_state_machines']} state machines",
          file=sys.stderr)
    print(f"Wrote {out_path}", file=sys.stderr)

    if getattr(args, "apply", False):
        attach_invariants_to_graph(G, invariants)
        # Write back via split_by_domain
        try:
            from _builder.graph_build import split_by_domain
            master_path = os.path.join(graph_dir, "code2database_master.json")
            if os.path.exists(master_path):
                with open(master_path) as _f:
                    master = json.load(_f)
                source_root = master.get("source_root", "")
                split_by_domain(G, graph_dir, source_root)
                print("Applied invariants to graph", file=sys.stderr)
        except Exception as exc:
            print(f"Apply failed: {exc}", file=sys.stderr)


def cmd_find_invariants(args):
    """Find functions guaranteeing a given invariant.

    Usage:
        find-invariants --graph <dir> --var ctx->state --value READY
        find-invariants --graph <dir> --var config --kind precondition
        find-invariants --graph <dir> --value "!= NULL"
        find-invariants --graph <dir> --state-machine --var ctx->state
    """
    graph_dir = args.graph
    try:
        from _builder.graph_build import _load_full_graph
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from _builder.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)

    if getattr(args, "state_machine", False):
        var = getattr(args, "var", "")
        if not var:
            print("Error: --var required with --state-machine", file=sys.stderr)
            sys.exit(1)
        sm = find_state_machine_for_var(G, var)
        print(json.dumps(sm, ensure_ascii=False, indent=2, default=str))
        return

    var = getattr(args, "var", "")
    value = getattr(args, "value", "")
    kind = getattr(args, "kind", "")
    results = find_invariants(G, var_pattern=var, value_pattern=value,
                              invariant_kind=kind)
    print(json.dumps({
        "query": {"var": var, "value": value, "kind": kind},
        "matches": results,
        "count": len(results),
    }, ensure_ascii=False, indent=2, default=str))


def cmd_apply_invariants(args):
    """Apply LLM-enhanced invariants from a JSON file to the graph.

    Mirrors apply-semantics: LLM reads .code2database_invariants.json,
    fills in additional invariants (marked INFERRED or LLM_VERIFIED),
    then this command writes them back.

    Usage:
        apply-invariants --graph <dir> [--input <file>]
    """
    graph_dir = args.graph
    input_path = getattr(args, "input", "") or os.path.join(
        graph_dir, ".code2database_invariants.json")
    if not os.path.exists(input_path):
        print(f"No invariants file at {input_path}. "
              f"Run 'extract-invariants' first.", file=sys.stderr)
        sys.exit(1)

    try:
        from _builder.graph_build import _load_full_graph, split_by_domain
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from _builder.graph_build import _load_full_graph, split_by_domain
    G = _load_full_graph(graph_dir)

    with open(input_path) as _f:
        data = json.load(_f)
    invariants = data.get("invariants", data)
    if not isinstance(invariants, dict):
        print("Invalid invariants file: 'invariants' must be a dict",
              file=sys.stderr)
        sys.exit(1)

    attach_invariants_to_graph(G, invariants)
    master_path = os.path.join(graph_dir, "code2database_master.json")
    if os.path.exists(master_path):
        with open(master_path) as _f:
            master = json.load(_f)
        source_root = master.get("source_root", "")
        split_by_domain(G, graph_dir, source_root)

    print(f"Applied invariants to {len(invariants)} nodes")


def _load_full_graph_local(graph_dir):
    try:
        from _builder.graph_build import _load_full_graph
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from _builder.graph_build import _load_full_graph
    return _load_full_graph(graph_dir)
