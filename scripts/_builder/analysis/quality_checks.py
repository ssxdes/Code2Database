"""Graph-based code quality detectors.

Detectors built on the invocation graph (plus function body text when
the scanner captured it):

  - cycle detection — call cycles between functions and include
    cycles between files (see ``check_cycles``)
  - recursion detection — direct self-loops and indirect cycles with
    termination staging (see ``check_recursion``)
  - array bounds exposure — subscript scan with guard inference
    (see ``check_bounds``)

All detectors are read-only analyses over a built graph directory;
they never modify graph state. Output is JSON-friendly dicts so the
CLI layer can print them directly.

Call edges are edges whose ``relation`` is ``INVOKES`` (the default,
including CALLBACK_ARG confidence edges) or ``DISPATCH``; data-oriented
relations (DATA_FLOW, DATA_DEP, FFI, HOLDER) are excluded so they
cannot fabricate call cycles.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_CALL_RELATIONS = ("", "INVOKES", "DISPATCH")


def _is_call_relation(relation: str) -> bool:
    """True when an edge relation represents a call.

    Allowlist instead of the impact command's blacklist: data-oriented
    relations (DATA_FLOW, DATA_DEP, FFI, HOLDER, LOCKS, ...) must not
    fabricate call cycles. CALLBACK_ARG edges carry their marker in the
    ``confidence`` field and keep the default INVOKES relation, so they
    are covered by the empty-string entry.
    """
    return relation in _CALL_RELATIONS


def _load_graph(graph_dir: str):
    from _builder.graph.graph_build import _load_full_graph
    return _load_full_graph(graph_dir)


def _node_display(G, nid: str) -> str:
    return G.nodes[nid].get("name") or nid


def _scope_matches(G, nid: str, scope: Optional[str]) -> bool:
    """Case-insensitive substring match over id, name, and source file."""
    if not scope:
        return True
    needle = scope.lower()
    nd = G.nodes[nid]
    hay = (nid + " " + (nd.get("name") or "") + " "
           + (nd.get("source_file") or ""))
    return needle in hay.lower()


def _build_call_adjacency(G, scope: Optional[str] = None) -> Dict[str, List[str]]:
    """Adjacency list over call edges, optionally scope-filtered.

    Both endpoints must pass the scope filter so cycles that merely
    pass through filtered nodes are not reported half-way.
    """
    adj: Dict[str, List[str]] = {}
    for u, v, ed in G.edges(data=True):
        if not _is_call_relation(ed.get("relation", "")):
            continue
        if scope and not (_scope_matches(G, u, scope)
                          and _scope_matches(G, v, scope)):
            continue
        adj.setdefault(u, []).append(v)
    return adj


def _tarjan_sccs(adj: Dict[str, List[str]]) -> List[Set[str]]:
    """Iterative Tarjan strongly-connected components.

    Only components with more than one node (or a self-loop) can host
    cycles; callers use the result to prune the DFS enumeration.
    """
    index_counter = [0]
    index: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    on_stack: Set[str] = set()
    stack: List[str] = []
    sccs: List[Set[str]] = []

    for root in adj:
        if root in index:
            continue
        # Iterative DFS: work list holds (node, iterator over successors)
        work = [(root, iter(adj.get(root, ())))]
        index[root] = lowlink[root] = index_counter[0]
        index_counter[0] += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for succ in it:
                if succ not in adj:
                    continue  # not a call-graph node in scope
                if succ not in index:
                    index[succ] = lowlink[succ] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(succ)
                    on_stack.add(succ)
                    work.append((succ, iter(adj.get(succ, ()))))
                    advanced = True
                    break
                elif succ in on_stack:
                    lowlink[node] = min(lowlink[node], index[succ])
            if advanced:
                continue
            # node finished
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index[node]:
                comp: Set[str] = set()
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.add(w)
                    if w == node:
                        break
                sccs.append(comp)
    return sccs


class _CycleBudget:
    """Guards cycle enumeration against pathological blow-up."""

    def __init__(self, max_cycles: int, max_steps: int):
        self.max_cycles = max_cycles
        self.max_steps = max_steps
        self.cycles = 0
        self.steps = 0
        self.steps_exhausted = False

    def step(self) -> bool:
        self.steps += 1
        if self.steps > self.max_steps:
            self.steps_exhausted = True
            return False
        return True

    def found(self) -> bool:
        self.cycles += 1
        return self.cycles < self.max_cycles


def _enumerate_cycles(adj: Dict[str, List[str]], max_length: int,
                      budget: _CycleBudget) -> Tuple[List[List[str]], bool]:
    """Enumerate elementary cycles up to ``max_length`` nodes.

    Each cycle is reported exactly once: the DFS starts from the
    lexicographically smallest node of the cycle and only visits nodes
    that sort >= the start node. Self-loops are reported as length-1
    cycles. Returns (cycles, truncated).
    """
    cycles: List[List[str]] = []
    # Only nodes inside a non-trivial SCC (or with a self-loop) can
    # start a cycle — prune everything else up front.
    sccs = _tarjan_sccs(adj)
    candidates: Set[str] = set()
    for comp in sccs:
        if len(comp) > 1:
            candidates.update(comp)
    for node, targets in adj.items():
        if node in targets:
            candidates.add(node)

    for start in sorted(candidates):
        if budget.steps_exhausted:
            break
        path = [start]
        on_path = {start}
        # successors restricted to nodes >= start (lexicographic pruning)
        stack = [(start, iter(sorted(t for t in adj.get(start, ())
                                     if t >= start)))]
        while stack:
            if not budget.step():
                return _finish(cycles, budget)
            node, it = stack[-1]
            advanced = False
            for succ in it:
                if succ == start:
                    cycle = list(path)
                    cycles.append(cycle)
                    if not budget.found():
                        return _finish(cycles, budget)
                elif succ not in on_path and succ >= start \
                        and len(path) < max_length:
                    on_path.add(succ)
                    path.append(succ)
                    stack.append((succ, iter(sorted(t for t in adj.get(succ, ())
                                                    if t >= start))))
                    advanced = True
                    break
            if not advanced:
                stack.pop()
                if path:
                    on_path.discard(path.pop())
    return _finish(cycles, budget)


def _finish(cycles: List[List[str]], budget: _CycleBudget) -> Tuple[List[List[str]], bool]:
    truncated = budget.steps_exhausted or budget.cycles >= budget.max_cycles
    return cycles, truncated


# ---------------------------------------------------------------------------
# Cycle detection (check-cycles)
# ---------------------------------------------------------------------------

def check_cycles(graph_dir: str, kind: str = "calls", max_length: int = 10,
                 scope: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    """Detect circular dependencies in the built graph.

    Args:
        graph_dir: C2D graph directory.
        kind: ``calls`` (function call cycles) or ``includes`` (file
            #include cycles over IMPORTS edges between file nodes).
        max_length: Maximum number of nodes in a reported cycle.
        scope: Optional substring filter over node id / name / file.
        limit: Maximum cycles to report (enumeration budget caps at
            ``limit`` cycles and 20k DFS steps).

    Returns:
        {kind, total_cycles, truncated, cycles: [{length, nodes,
        names, files}]}
    """
    G = _load_graph(graph_dir)

    if kind == "includes":
        adj = _build_include_adjacency(G, scope)
    elif kind == "calls":
        adj = _build_call_adjacency(G, scope)
    else:
        raise ValueError(f"unknown kind {kind!r} (expected calls|includes)")

    budget = _CycleBudget(max_cycles=limit, max_steps=20000)
    raw_cycles, truncated = _enumerate_cycles(adj, max_length, budget)

    cycles = []
    for cyc in raw_cycles:
        cycles.append({
            "length": len(cyc),
            "nodes": list(cyc),
            "names": [_node_display(G, n) for n in cyc],
            "files": [G.nodes[n].get("source_file", "") for n in cyc],
        })
    # Longest cycles first — they are usually the more structural finds.
    cycles.sort(key=lambda c: (-c["length"], c["nodes"]))

    return {
        "kind": kind,
        "scope": scope or "",
        "max_length": max_length,
        "total_cycles": len(cycles),
        "truncated": truncated,
        "cycles": cycles,
    }


def _build_include_adjacency(G, scope: Optional[str]) -> Dict[str, List[str]]:
    """Adjacency over IMPORTS edges between file nodes."""
    adj: Dict[str, List[str]] = {}
    for u, v, ed in G.edges(data=True):
        if ed.get("relation") != "IMPORTS":
            continue
        if scope and not (_scope_matches(G, u, scope)
                          and _scope_matches(G, v, scope)):
            continue
        adj.setdefault(u, []).append(v)
    return adj


def cmd_check_cycles(args):
    """CLI handler for `code2database_builder.py check-cycles`."""
    import json
    result = check_cycles(
        args.graph,
        kind=getattr(args, "kind", "calls"),
        max_length=getattr(args, "max_length", 10),
        scope=getattr(args, "scope", None),
        limit=getattr(args, "limit", 50),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------------------
# Recursion detection (check-recursion)
# ---------------------------------------------------------------------------

_COND_HEADER_RE = re.compile(r"\b(?:if|else|while|for|switch|do)\b")
_SHORT_CIRCUIT_RE = re.compile(r"&&|\|\||\?")
_RETURN_RE = re.compile(r"^\s*(?:\}\s*)?return\b")
_TERMINATION_ORDER = {"risky": 0, "caution": 1, "safe": 2, "unknown": 3}


def _conditional_context(body: str) -> Dict[int, bool]:
    """Per-line (1-based) flag: is the statement on this line conditional?

    Tracks brace depth (blocks opened under an if/else/while/for/switch/
    do header are conditional and nesting inherits it) plus a pending
    state for brace-less single-statement bodies (`if (x) f();`).
    Short-circuit operators on the line itself also count, covering
    `x && f()` argument guards.
    """
    lines = body.split("\n")
    guard: Dict[int, bool] = {}
    stack: List[bool] = []
    pending = False
    for i, raw in enumerate(lines, 1):
        stripped = raw.strip()
        header = _COND_HEADER_RE.search(raw)
        in_cond = any(stack)
        guard[i] = (in_cond or header is not None or pending
                    or _SHORT_CIRCUIT_RE.search(raw) is not None)
        opens_cond_block = header is not None or in_cond or pending
        if stripped and pending:
            pending = False
        if header is not None and "{" not in raw:
            pending = True
        for ch in raw:
            if ch == "{":
                stack.append(opens_cond_block)
            elif ch == "}" and stack:
                stack.pop()
    return guard


def _strip_signature(body: str, call_names: List[str]) -> str:
    """Drop a leading function signature when body_text includes it.

    Some scanners store the signature line inside body_text; the
    definition `int fact(int n) {` would otherwise register as a call
    site of ``fact``. A head segment is a signature when it has no
    semicolon and one of the names appears in call position before
    the first brace.
    """
    idx = body.find("{")
    if idx <= 0:
        return body
    head = body[:idx]
    if ";" in head:
        return body
    for n in call_names:
        if n and re.search(r"\b" + re.escape(n) + r"\s*\(", head):
            return body[idx:]
    return body


def _analyze_termination(body: str, call_names: List[str]) -> Dict[str, Any]:
    """Stage termination behavior of a recursive function.

    - risky: some recursive call sits on an unconditional path with no
      conditional early-return ahead of it (every invocation recurses)
    - caution: all recursive calls are conditional, but no return
      statement exists outside recursive call lines (no visible base
      case)
    - safe: all recursive calls conditional (or shielded by a
      conditional early-return) and a base-case return exists
    - unknown: body text unavailable
    """
    if not body or not body.strip():
        return {"termination": "unknown", "recursive_call_lines": [],
                "guarded_call_lines": [], "has_base_return": False}
    body = _strip_signature(body, call_names)
    lines = body.split("\n")
    guard = _conditional_context(body)
    call_res = [re.compile(r"\b" + re.escape(n) + r"\s*\(")
                for n in call_names if n]
    # Conditional early-returns (base-case checks): a return inside a
    # conditional construct that executes before a later call shields
    # that call — reaching it requires passing the base case.
    cond_return_lines = [i for i, raw in enumerate(lines, 1)
                         if _RETURN_RE.match(raw) and guard.get(i)]
    call_lines: List[int] = []
    guarded_lines: List[int] = []
    base_return = False
    for i, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("*"):
            continue
        has_call = any(r.search(raw) for r in call_res)
        if has_call:
            call_lines.append(i)
            if guard.get(i) or any(cr < i for cr in cond_return_lines):
                guarded_lines.append(i)
        elif _RETURN_RE.match(raw):
            base_return = True
    if not call_lines:
        # No textual call site found (indirect via fn pointer etc.)
        return {"termination": "unknown", "recursive_call_lines": [],
                "guarded_call_lines": [], "has_base_return": base_return}
    unguarded = [ln for ln in call_lines if ln not in guarded_lines]
    if unguarded:
        termination = "risky"
    elif base_return:
        termination = "safe"
    else:
        termination = "caution"
    return {"termination": termination,
            "recursive_call_lines": call_lines,
            "guarded_call_lines": guarded_lines,
            "has_base_return": base_return}


def check_recursion(graph_dir: str, max_length: int = 10,
                    scope: Optional[str] = None,
                    limit: int = 50) -> Dict[str, Any]:
    """Detect recursion with termination staging.

    Direct recursion = call-graph self-loop; indirect = call cycle of
    two or more functions. Every function on a reported cycle gets a
    finding with its own termination stage derived from its body text.

    Returns:
        {total_recursive_functions, direct, indirect, truncated,
         findings: [{function, name, file, kind, cycle, termination,
                    recursive_call_lines, guarded_call_lines,
                    has_base_return}]}
    """
    G = _load_graph(graph_dir)
    adj = _build_call_adjacency(G, scope)
    budget = _CycleBudget(max_cycles=limit, max_steps=20000)
    cycles, truncated = _enumerate_cycles(adj, max_length, budget)

    findings: List[Dict[str, Any]] = []
    for cyc in cycles:
        cycle_names = [_node_display(G, n) for n in cyc]
        kind = "direct" if len(cyc) == 1 else "indirect"
        for nid in cyc:
            nd = G.nodes[nid]
            analysis = _analyze_termination(nd.get("body_text", ""),
                                            cycle_names)
            findings.append({
                "function": nid,
                "name": _node_display(G, nid),
                "file": nd.get("source_file", ""),
                "kind": kind,
                "cycle": cycle_names,
                **analysis,
            })
    findings.sort(key=lambda f: (_TERMINATION_ORDER[f["termination"]],
                                 f["function"]))

    return {
        "total_recursive_functions": len({f["function"] for f in findings}),
        "direct": sum(1 for f in findings if f["kind"] == "direct"),
        "indirect": sum(1 for f in findings if f["kind"] == "indirect"),
        "truncated": truncated,
        "findings": findings,
    }


def cmd_check_recursion(args):
    """CLI handler for `code2database_builder.py check-recursion`."""
    import json
    result = check_recursion(
        args.graph,
        max_length=getattr(args, "max_length", 10),
        scope=getattr(args, "scope", None),
        limit=getattr(args, "limit", 50),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------------------
# Array bounds exposure (check-bounds)
# ---------------------------------------------------------------------------

_SUBSCRIPT_RE = re.compile(r"(\w+)\s*\[\s*([^\]\'\"]+)\s*\]")
_CONST_INDEX_RES = [
    re.compile(r"^\d+$"),
    re.compile(r"^0[xX][0-9a-fA-F]+$"),
    re.compile(r"\bsizeof\b"),
    re.compile(r"^[A-Z][A-Z_0-9]*$"),
]
_MAP_DECL_RES = [
    re.compile(r"\bstd::(?:unordered_)?map\s*<[^;]*?>\s*(\w+)"),
    re.compile(r"\bauto\s+(\w+)\s*=\s*\w+Get\w*Map"),
]
_GUARD_KEYWORD_RE = re.compile(r"\b(?:if|assert|unlikely|likely|BUG_ON|WARN_ON)\b")
_GUARD_COMPARE_RE = re.compile(r"[<>]=?|!=")
_RANGE_CHECK_RE = re.compile(r"\b(?:out_of_range|outOfRange)\b")
_SAFE_ACCESS_RE = re.compile(r"\.(?:at|value_or)\s*\(")


def _is_skippable_source_line(stripped: str) -> bool:
    """Comment / preprocessor / label lines cannot host real accesses."""
    if not stripped:
        return True
    return (stripped.startswith("//") or stripped.startswith("/*")
            or stripped.startswith("*") or stripped.startswith("#"))


def _map_variable_names(body: str) -> Set[str]:
    names: Set[str] = set()
    for rx in _MAP_DECL_RES:
        for m in rx.finditer(body):
            names.add(m.group(1))
    return names


def _needle_in(raw: str, needle: str) -> bool:
    """Word-boundary containment so `i` does not match inside `if`."""
    if not needle:
        return False
    return re.search(r"\b" + re.escape(needle) + r"\b", raw) is not None


def _find_guard(lines: List[str], access_idx: int, index_expr: str,
                window: int) -> Optional[Tuple[int, str]]:
    """Look back up to ``window`` lines for a guard on ``index_expr``.

    Returns (1-based line number, guard kind) or None. Kinds:
    condition (if/assert with a comparison naming the index),
    range_check (out_of_range markers), safe_access (.at(/.value_or().
    """
    needle = index_expr.strip()
    lo = max(0, access_idx - window)
    for i in range(access_idx, lo - 1, -1):
        raw = lines[i]
        if _is_skippable_source_line(raw.strip()):
            continue
        if _RANGE_CHECK_RE.search(raw):
            return i + 1, "range_check"
        if (_GUARD_KEYWORD_RE.search(raw) and _GUARD_COMPARE_RE.search(raw)
                and _needle_in(raw, needle)):
            return i + 1, "condition"
    for i in range(access_idx, lo - 1, -1):
        if _SAFE_ACCESS_RE.search(lines[i]):
            return i + 1, "safe_access"
    return None


def check_bounds(graph_dir: str, scope: Optional[str] = None,
                 limit: int = 100, window: int = 20) -> Dict[str, Any]:
    """Scan array subscript accesses and infer guard coverage.

    For every function with body text, ``var[index]`` accesses are
    collected; constant indices (decimal, hex, sizeof, ALL_CAPS
    macros), string keys, and map-typed variables are treated as
    non-exposures. Remaining accesses are classified by looking back
    up to ``window`` lines for a guarding condition, range check, or
    safe-access pattern.

    Returns:
        {total_accesses, risky, safe, truncated, findings: [{function,
        name, file, line, var, index, classification, guard_line,
        guard_kind}]}
    """
    G = _load_graph(graph_dir)
    findings: List[Dict[str, Any]] = []
    truncated = False
    for nid in sorted(G.nodes):
        if scope and not _scope_matches(G, nid, scope):
            continue
        nd = G.nodes[nid]
        body = nd.get("body_text", "") or ""
        if not body.strip():
            continue
        lines = body.split("\n")
        map_vars = _map_variable_names(body)
        for i, raw in enumerate(lines):
            if _is_skippable_source_line(raw.strip()):
                continue
            for m in _SUBSCRIPT_RE.finditer(raw):
                var, index_expr = m.group(1), m.group(2).strip()
                if var in map_vars:
                    continue
                if any(rx.search(index_expr) for rx in _CONST_INDEX_RES):
                    continue
                if index_expr.startswith('"') or index_expr.startswith("'"):
                    continue
                guard = _find_guard(lines, i, index_expr, window)
                if guard is not None:
                    guard_line, guard_kind = guard
                    classification, line_no = "safe", guard_line
                else:
                    classification, guard_line, guard_kind = "risky", None, None
                findings.append({
                    "function": nid,
                    "name": _node_display(G, nid),
                    "file": nd.get("source_file", ""),
                    "line": i + 1,
                    "var": var,
                    "index": index_expr,
                    "classification": classification,
                    "guard_line": guard_line,
                    "guard_kind": guard_kind,
                })
                if len(findings) >= limit:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            break

    order = {"risky": 0, "safe": 1}
    findings.sort(key=lambda f: (order[f["classification"]],
                                 f["file"], f["line"], f["function"]))
    return {
        "total_accesses": len(findings),
        "risky": sum(1 for f in findings if f["classification"] == "risky"),
        "safe": sum(1 for f in findings if f["classification"] == "safe"),
        "truncated": truncated,
        "findings": findings,
    }


def cmd_check_bounds(args):
    """CLI handler for `code2database_builder.py check-bounds`."""
    import json
    result = check_bounds(
        args.graph,
        scope=getattr(args, "scope", None),
        limit=getattr(args, "limit", 100),
        window=getattr(args, "window", 20),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
