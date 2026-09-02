#!/usr/bin/env python3
"""Value-flow analysis for Code2Database.

Models how values flow across functions — not just how parameters propagate
(param-flow does that), but how a function's *return value* depends on its
inputs and how that value is consumed by the caller.

Three new capabilities:
1. return_value extraction: for each function, capture what expression it
   returns and which params/globals/fields that expression references.
2. DATA_FLOW edges: a new edge type connecting a caller's arg-expression
   to a callee's return-value, so you can walk the value graph.
3. taint tracking: mark a source (e.g., user input, file read) and trace
   which functions/sinks it reaches.

Why this matters for bug-hunting:
- "Where does this NULL come from?" → reverse-walk DATA_FLOW edges.
- "Can user input reach this pointer deref?" → forward-taint from source.
- "Does this integer overflow propagate?" → trace value transforms.

Implementation note: this is a *static, syntactic* value-flow — it uses
regex/AST-light heuristics on body_text. It is sound for the common cases
(direct returns, simple field accesses) but over-approximates for complex
control flow. Confidence is labeled accordingly (EXTRACTED for direct,
INFERRED for transformed, AMBIGUOUS for lost-in-control-flow).
"""

import json
import os
import re

# Cache for per-callee-name assignment regexes. Avoids re.compile on
# every hop during value-flow traversal (query-time, but depth-bounded
# at ~10 hops — cache helps when same callee appears in multiple flows).
_ASGN_RE_CACHE = {}
import sys
from collections import deque
from typing import Optional, List, Dict, Any, Set, Tuple
import logging


# ---------------------------------------------------------------------------
# Return-value extraction (lightweight, regex-based)
# ---------------------------------------------------------------------------

# Match `return EXPR;` capturing the EXPR (handles nested parens up to depth 4)
_log = logging.getLogger(__name__)

_RETURN_RE = re.compile(
    r'\breturn\s+([^;{]+?);',
    re.MULTILINE
)

# Match common NULL/0/error returns that are bug-relevant
_NULL_PATTERNS = [
    (r'\bNULL\b', 'NULL'),
    (r'(?<![\w.])0\s*(?![.\d])', 'zero'),
    (r'-E\w+', 'errno'),  # -EINVAL, -ENOMEM, etc.
    (r'\bERR_PTR\s*\(', 'err_ptr'),
]

# Known security sink function name patterns. Functions matching these
# names are true sinks (dangerous operations where taint reaching them
# is a security concern). Leaf functions NOT matching these patterns
# are "terminals" — the taint flow ends there, but they are not
# inherently dangerous (e.g., a utility like max(a,b) is a leaf but
# not a sink).
_SINK_NAME_RE = re.compile(
    r'^(?:'
    r'memcpy|memmove|memset|memchr|memcmp|strcpy|strncpy|strcat|strncat|'  # memory
    r'strlen|strchr|strstr|strtok|sprintf|snprintf|vsprintf|vsnprintf|'  # string
    r'gets|fgets|fread|recv|read|readlink|fscanf|sscanf|'  # input
    r'system|popen|exec|execl|execv|execve|execvp|fork|'  # exec
    r'free|realloc|'  # memory mgmt
    r'write|fwrite|send|sendto|sendmsg|printf|fprintf|vfprintf|'  # output
    r'setuid|setgid|seteuid|setegid|chroot|chmod|chown|'  # privilege
    r'open|creat|openat|'  # file open
    r'dlopen|dlsym|'  # dynamic loading
    r'mmap|munmap|mprotect|'  # memory mapping
    r'ioctl|'  # device control
    r'kill|raise|signal|'  # signals
    r'__builtin_\w+|'  # compiler builtins (__builtin_trap, __builtin_abort, etc.)
    r'abort|exit|_exit|atexit|assert|'  # termination
    r'error|panic|BUG|WARN'  # error/panic
    r')(?:_r|_s|_l|_unlocked)?(?:@.*)?$'
)


def extract_return_expressions(body_text: str) -> List[Dict]:
    """Extract return expressions from a function body.

    Returns a list of dicts with:
        expr: the returned expression text
        line: approximate line number in body
        references: list of identifiers (params, globals, fields) the expr reads
        null_like: True if expr is NULL/0/-EINVAL/ERR_PTR
        confidence: 'EXTRACTED' for direct returns, 'INFERRED' for transformed
    """
    if not body_text:
        return []
    results = []
    # Find line offsets for line-number attribution
    line_offsets = [0]
    for i, ch in enumerate(body_text):
        if ch == '\n':
            line_offsets.append(i + 1)

    for m in _RETURN_RE.finditer(body_text):
        expr = m.group(1).strip()
        if not expr or expr in (';', '}'):
            continue
        # Compute line number
        pos = m.start(1)
        line_no = 1
        for off in line_offsets:
            if off <= pos:
                line_no += 1
            else:
                break
        line_no = max(1, line_no - 1)

        # Identify references in the expression
        refs = _extract_references(expr)
        # Check if NULL-like
        null_like = False
        for pat, _label in _NULL_PATTERNS:
            if re.search(pat, expr):
                null_like = True
                break
        # Confidence: direct param/constant return is EXTRACTED; complex
        # expression is INFERRED (we can't be sure of the data flow without
        # full path-sensitive analysis).
        confidence = "EXTRACTED" if len(refs) <= 1 and _is_simple_expr(expr) else "INFERRED"

        results.append({
            "expr": expr,
            "line": line_no,
            "references": refs,
            "null_like": null_like,
            "confidence": confidence,
        })
    return results


def _extract_references(expr: str) -> List[str]:
    """Extract identifier references from an expression.

    Captures: simple identifiers, field accesses (a->b, a.b), and array
    indexing (a[i]). Skips C keywords and numeric literals.
    """
    # Strip strings and comments to avoid false refs
    cleaned = re.sub(r'"[^"]*"', '""', expr)
    cleaned = re.sub(r"'[^']*'", "''", cleaned)
    cleaned = re.sub(r'//[^\n]*', '', cleaned)
    cleaned = re.sub(r'/\*.*?\*/', '', cleaned, flags=re.DOTALL)
    # Find: ident.ident.ident or ident->ident (chain of accesses)
    refs = []
    seen = set()
    for m in re.finditer(r'[A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*)*', cleaned):
        ref = m.group(0).replace(' ', '')  # normalize "a -> b" to "a->b"
        if ref in ('NULL', 'true', 'false', 'void', 'return', 'if', 'else',
                   'while', 'for', 'switch', 'case', 'break', 'continue',
                   'sizeof', 'typeof', 'auto', 'register', 'volatile',
                   'const', 'static', 'extern', 'inline'):
            continue
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


def _is_simple_expr(expr: str) -> bool:
    """A simple expression is a single identifier, constant, or field access."""
    # No operators (except member access), no function calls
    if '(' in expr:
        return False
    if any(op in expr for op in ('+', '-', '*', '/', '%', '&', '|', '^',
                                  '<<', '>>', '&&', '||', '==', '!=',
                                  '<', '>', '?', ':')):
        # Allow leading - for negative constants
        if expr.lstrip().startswith('-') and expr.count('-') == 1:
            return True
        return False
    return True


# ---------------------------------------------------------------------------
# DATA_FLOW edge construction
# ---------------------------------------------------------------------------

def build_data_flow_edges(G) -> List[Dict]:
    """Build DATA_FLOW edges by analyzing callee_args and return expressions.

    For each INVOKES edge (caller → callee), if the caller passes an argument
    that references a tracked value (param/global/field), and the callee
    returns an expression referencing its own params, we add a DATA_FLOW edge:

        caller -[DATA_FLOW {via_arg: arg_pos, transform: expr}]-> callee

    Plus a return-flow edge:

        callee -[RETURN_FLOW {returns: expr, references: [...]}]-> caller

    Edges are stored with confidence and source attribution.
    """
    edges = []
    # Pre-compute return expressions per node
    return_exprs = {}
    for nid, nd in G.nodes(data=True):
        body = nd.get("body_text", "")
        if body:
            return_exprs[nid] = extract_return_expressions(body)

    # For each INVOKES edge, derive DATA_FLOW + RETURN_FLOW
    for u, v, ed in G.edges(data=True):
        if ed.get("relation") != "INVOKES":
            continue
        caller_nd = G.nodes[u]
        callee_nd = G.nodes[v]
        callee_args = caller_nd.get("callee_args", []) or []
        # Find the matching callee_args entry for this callee
        matching_ca = None
        for ca in callee_args:
            if ca.get("callee") == callee_nd.get("name", "") or ca.get("callee") == v:
                matching_ca = ca
                break
        if not matching_ca:
            continue

        # For each arg, create a DATA_FLOW edge recording the value expression
        for arg in matching_ca.get("args", []):
            arg_val = arg.get("value", "")
            if not arg_val:
                continue
            refs = _extract_references(arg_val)
            if not refs:
                continue
            edges.append({
                "caller": u,
                "callee": v,
                "relation": "DATA_FLOW",
                "arg_pos": arg.get("pos"),
                "arg_value": arg_val,
                "references": refs,
                "call_order": matching_ca.get("call_order"),
                "confidence": "EXTRACTED" if _is_simple_expr(arg_val) else "INFERRED",
                "source": "value_flow_analysis",
            })

        # RETURN_FLOW: callee returns → caller (the caller's expression using
        # the call result depends on the callee's return value)
        callee_returns = return_exprs.get(v, [])
        for ret in callee_returns:
            edges.append({
                "caller": v,  # return originates from callee
                "callee": u,  # consumed by caller (the calling function)
                "relation": "RETURN_FLOW",
                "return_expr": ret["expr"],
                "return_refs": ret["references"],
                "null_like": ret["null_like"],
                "confidence": ret["confidence"],
                "source": "value_flow_analysis",
            })

    return edges


def attach_data_flow_to_graph(G, edges: List[Dict]):
    """Add DATA_FLOW/RETURN_FLOW edges to the NetworkX graph in-place."""
    for e in edges:
        caller = e.pop("caller")
        callee = e.pop("callee")
        if caller not in G or callee not in G:
            continue
        # If edge already exists (INVOKES), we add data_flow metadata to it;
        # otherwise add a new edge with relation=DATA_FLOW/RETURN_FLOW.
        if G.has_edge(caller, callee):
            existing = G.get_edge_data(caller, callee) or {}
            # Annotate existing edge
            existing.setdefault("data_flow", []).append(e)
        else:
            G.add_edge(caller, callee, **e)


# ---------------------------------------------------------------------------
# Reverse value-flow trace: "where does this value come from?"
# ---------------------------------------------------------------------------

def reverse_value_trace(G, start_id: str, value_pattern: str,
                        max_depth: int = 10) -> Dict:
    """Trace where a value comes from, walking backward through DATA_FLOW.

    Engineer question: "This pointer is NULL when it reaches function X —
    where does the NULL originate?"

    Algorithm: BFS forward through callees (we want to find which callee
    in the call tree returns the value), checking each function's return
    expressions for NULL-like values that match the pattern. Also walks
    backward through callers to see if the value is propagated from a
    caller's arg.
    """
    visited = set()
    queue = deque([(start_id, value_pattern, 0, [start_id])])
    sources = []

    while queue:
        cur_id, cur_pattern, depth, path = queue.popleft()
        if depth >= max_depth:
            continue
        if cur_id in visited:
            continue
        visited.add(cur_id)

        cur_nd = G.nodes[cur_id]
        # Check this node's return expressions for NULL-like values
        body = cur_nd.get("body_text", "")
        if body and depth > 0:  # don't check the start node (we're tracing INTO it)
            returns = extract_return_expressions(body)
            for ret in returns:
                if not ret["null_like"]:
                    continue
                if _value_matches(ret["expr"], cur_pattern):
                    sources.append({
                        "function": cur_nd.get("name", cur_id),
                        "function_id": cur_id,
                        "return_expr": ret["expr"],
                        "line": ret["line"],
                        "confidence": ret["confidence"],
                        "path": list(path),
                    })
                    if len(sources) >= 20:
                        return {"start": start_id, "pattern": value_pattern,
                                "sources": sources, "truncated": True}

        # Walk forward into callees (the value might originate deeper)
        for succ in G.successors(cur_id):
            ed = G.get_edge_data(cur_id, succ) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            # Check if caller passes a value matching the pattern to this callee
            callee_args = cur_nd.get("callee_args", []) or []
            passes_pattern = False
            for ca in callee_args:
                if ca.get("callee") != G.nodes[succ].get("name", ""):
                    continue
                for arg in ca.get("args", []):
                    if _value_references(arg.get("value", ""), cur_pattern):
                        passes_pattern = True
                        break
            new_path = path + [succ]
            # Always explore callees (the value could originate there even if
            # the caller doesn't pass it explicitly — e.g., a callee returns NULL)
            queue.append((succ, cur_pattern, depth + 1, new_path))

    return {
        "start": start_id,
        "pattern": value_pattern,
        "sources": sources,
        "truncated": False,
    }


def _value_matches(expr: str, pattern: str) -> bool:
    """Check if a return expression matches a value pattern (e.g., 'NULL')."""
    if pattern.upper() == "NULL":
        return bool(re.search(r'\bNULL\b', expr))
    if pattern == "0":
        return bool(re.search(r'\b0\b', expr)) and 'NULL' not in expr
    return pattern in expr


def _value_references(arg_val: str, pattern: str) -> bool:
    """Check if an arg-value expression references the pattern (as a token)."""
    if not arg_val:
        return False
    if pattern.upper() == "NULL":
        return bool(re.search(r'\bNULL\b', arg_val))
    return bool(re.search(r'\b' + re.escape(pattern) + r'\b', arg_val))


# ---------------------------------------------------------------------------
# Forward taint tracking: "where can this value go?"
# ---------------------------------------------------------------------------

def forward_taint_trace(G, source_id: str, taint_pattern: str,
                        max_depth: int = 10) -> Dict:
    """Trace where a tainted value flows, walking forward through DATA_FLOW.

    Engineer question: "This is user input — which sinks can it reach?"
    """
    visited = set()
    queue = deque([(source_id, taint_pattern, 0, [source_id])])
    sinks = []
    # Cache the name → id index on the graph object itself so repeated calls
    # to forward_taint_trace don't rebuild it from scratch. Use
    # G.graph['_name_index_v1'] as a stable cache key (bump the version
    # suffix if the index format ever changes). Invalidate when node count
    # changes (handles add/remove; rename without count change is a known
    # limitation — acceptable for a query-time cache on typically read-only
    # graphs).
    _name_index = G.graph.get('_name_index_v1')
    _cached_count = G.graph.get('_name_index_count')
    _cur_count = G.number_of_nodes()
    if _name_index is None or _cached_count != _cur_count:
        _name_index = {}
        for _n, _nd in G.nodes(data=True):
            _nm = _nd.get("name")
            if _nm:
                _name_index.setdefault(_nm, _n)
        G.graph['_name_index_v1'] = _name_index
        G.graph['_name_index_count'] = _cur_count

    while queue:
        cur_id, cur_taint, depth, path = queue.popleft()
        if depth >= max_depth:
            continue
        if cur_id in visited:
            continue
        visited.add(cur_id)

        cur_nd = G.nodes[cur_id]
        callee_args = cur_nd.get("callee_args", []) or []
        for ca in callee_args:
            callee_name = ca.get("callee", "")
            for arg in ca.get("args", []):
                arg_val = arg.get("value", "")
                if _value_references(arg_val, cur_taint):
                    # Find the callee node — use name index for O(1) lookup.
                    invoked_id = _name_index.get(callee_name)
                    if invoked_id is None and callee_name in G:
                        invoked_id = callee_name
                    if invoked_id and invoked_id not in visited:
                        sink_entry = {
                            "function": callee_name,
                            "function_id": invoked_id,
                            "arg_pos": arg.get("pos"),
                            "arg_value": arg_val,
                            "depth": depth + 1,
                            "path": path + [invoked_id],
                        }
                        # Is this a sink? A leaf function (no
                        # callee_args) is either a true security sink
                        # (matches known sink patterns) or a terminal
                        # (taint flow ends but not inherently dangerous).
                        callee_nd = G.nodes[invoked_id]
                        if not callee_nd.get("callee_args"):
                            if _SINK_NAME_RE.match(callee_name):
                                sink_entry["kind"] = "sink"
                            else:
                                sink_entry["kind"] = "terminal"
                                sink_entry["note"] = (
                                    "leaf function — taint flow ends "
                                    "here but not a known security sink"
                                )
                            sinks.append(sink_entry)
                        else:
                            queue.append((invoked_id, arg_val, depth + 1, path + [invoked_id]))
                    elif invoked_id:
                        sinks.append({
                            "function": callee_name,
                            "function_id": invoked_id,
                            "arg_pos": arg.get("pos"),
                            "arg_value": arg_val,
                            "depth": depth + 1,
                            "path": path + [invoked_id],
                            "note": "already visited — possible sink",
                        })
                    if len(sinks) >= 50:
                        return {"source": source_id, "taint": taint_pattern,
                                "sinks": sinks, "truncated": True}

    return {
        "source": source_id,
        "taint": taint_pattern,
        "sinks": sinks,
        "truncated": False,
    }


# ---------------------------------------------------------------------------
# Alias extraction: parse local-variable assignments from body text (D19)
# ---------------------------------------------------------------------------

# Match simple assignments: `type *alias = expr;` or `alias = expr;`
# Captures (alias_name, rhs_expr). Skips `==`, `<=`, `>=`.
# Uses lookbehind so consecutive statements (`p = x; p = q;`) both match.
_ALIAS_RE = re.compile(
    r'(?:(?<=^)|(?<=\n)|(?<=;)|(?<=\{))\s*'
    r'(?:[A-Za-z_][\w\s\*]*?\s+\*?\s*)?'
    r'([A-Za-z_]\w*)\s*=\s*([^;{]+?);',
    re.MULTILINE
)


def extract_aliases(body_text: str) -> Dict[str, str]:
    """Extract local variable aliases from a function body.

    Returns a dict mapping alias_name → rhs_expr. Only single-assignment
    forms are captured (no compound ops like +=). The RHS is the source
    expression the alias refers to.

    Example:
        body = "int *p = x; p = q; foo(p);"
        returns {"p": "q"}  # last assignment wins
    """
    aliases: Dict[str, str] = {}
    if not body_text:
        return aliases
    for m in _ALIAS_RE.finditer(body_text):
        name = m.group(1)
        rhs = m.group(2).strip()
        # Skip control-flow keywords
        if name in ("if", "while", "for", "switch", "return"):
            continue
        # Skip comparison ops that escaped the regex
        if rhs.startswith("="):
            continue
        aliases[name] = rhs
    return aliases


def resolve_alias(alias_name: str, aliases: Dict[str, str],
                  depth: int = 0, seen: Optional[Set[str]] = None) -> str:
    """Recursively resolve an alias to its ultimate source expression.

    If `alias_name` is in `aliases`, follow the chain until we reach a
    non-alias expression. Guards against cycles with a seen-set and
    a depth cap.

    Example:
        aliases = {"p": "q", "q": "x"}
        resolve_alias("p", aliases) → "x"
    """
    if seen is None:
        seen = set()
    if alias_name in seen or depth > 10:
        return alias_name
    seen.add(alias_name)
    if alias_name not in aliases:
        return alias_name
    rhs = aliases[alias_name]
    # If RHS is a single identifier that is itself an alias, recurse
    rhs_stripped = rhs.strip()
    if re.fullmatch(r'[A-Za-z_]\w*', rhs_stripped) and rhs_stripped in aliases:
        return resolve_alias(rhs_stripped, aliases, depth + 1, seen)
    return rhs_stripped


def interprocedural_value_flow(G, start_id: str, value_pattern: str,
                               max_depth: int = 10,
                               direction: str = "forward") -> Dict:
    """Multi-hop interprocedural value-flow trace with alias propagation.

    Walks DATA_FLOW edges across function boundaries, propagating aliases
    within each function body. At each hop:
    1. Extract aliases from the current function's body.
    2. Resolve the value_pattern through the alias chain.
    3. Find DATA_FLOW edges where the alias-resolved pattern matches.
    4. Recurse into the next function.

    direction='forward': trace where a value flows to (sinks).
    direction='reverse': trace where a value came from (sources).

    Returns:
    {
        "start": start_id,
        "pattern": value_pattern,
        "direction": direction,
        "hops": [
            {"function": ..., "function_id": ..., "aliases": {...},
             "matched_via": ..., "depth": N},
            ...
        ],
        "endpoints": [{"function": ..., "reason": "sink"|"source"|"dead-end"}],
        "truncated": bool
    }
    """
    hops: List[Dict[str, Any]] = []
    endpoints: List[Dict[str, Any]] = []
    visited: Set[str] = set()

    def _follow(cur_id: str, cur_pattern: str, depth: int, path: List[str]):
        if depth >= max_depth:
            return
        if cur_id in visited:
            endpoints.append({"function": G.nodes[cur_id].get("name", cur_id),
                              "function_id": cur_id, "reason": "cycle",
                              "depth": depth})
            return
        visited.add(cur_id)

        cur_nd = G.nodes[cur_id]
        body = cur_nd.get("body_text", "") or ""
        aliases = extract_aliases(body)
        resolved = resolve_alias(cur_pattern, aliases)

        hops.append({
            "function": cur_nd.get("name", cur_id),
            "function_id": cur_id,
            "depth": depth,
            "input_pattern": cur_pattern,
            "resolved_pattern": resolved,
            "aliases": dict(list(aliases.items())[:10]),
            "alias_count": len(aliases),
        })

        # Find next-hop edges
        next_hops: List[Tuple[str, str, str]] = []  # (next_id, via, new_pattern)
        if direction == "forward":
            # Outgoing DATA_FLOW + INVOKES edges
            for _, dst, ed in G.out_edges(cur_id, data=True):
                rel = ed.get("relation", "INVOKES")
                if rel == "DATA_FLOW":
                    via = ed.get("via_arg", "?")
                    next_hops.append((dst, f"DATA_FLOW via {via}", resolved))
                elif rel == "INVOKES":
                    # Check if any arg references the resolved pattern
                    callee_args = cur_nd.get("callee_args", []) or []
                    callee_name = G.nodes[dst].get("name", "")
                    for ca in callee_args:
                        if ca.get("callee") != callee_name:
                            continue
                        for arg in ca.get("args", []):
                            arg_val = arg.get("value", "")
                            if _value_references(arg_val, resolved):
                                next_hops.append((dst, f"INVOKES arg{arg.get('pos','?')}",
                                                  arg_val))
                                break
        else:  # reverse
            # Incoming INVOKES edges: caller's return value or assigned local
            for src, _, ed in G.in_edges(cur_id, data=True):
                rel = ed.get("relation", "INVOKES")
                if rel == "RETURN_FLOW":
                    ret_expr = ed.get("returns", "")
                    if ret_expr and _value_references(ret_expr, resolved):
                        next_hops.append((src, "RETURN_FLOW", ret_expr))
                elif rel == "INVOKES":
                    # Caller may have: `x = callee(...)` — check caller body
                    caller_body = G.nodes[src].get("body_text", "") or ""
                    if caller_body and resolved in caller_body:
                        # Find assignment from callee. Cache compiled regex
                        # per callee_name to avoid re.compile on every hop.
                        callee_name = cur_nd.get("name", "")
                        if callee_name not in _ASGN_RE_CACHE:
                            _ASGN_RE_CACHE[callee_name] = re.compile(
                                r'([A-Za-z_]\w*)\s*=\s*' + re.escape(callee_name) + r'\s*\('
                            )
                        asgn_re = _ASGN_RE_CACHE[callee_name]
                        for m in asgn_re.finditer(caller_body):
                            alias = m.group(1)
                            next_hops.append((src, f"assigned to {alias}", alias))
                            break

        if not next_hops:
            endpoints.append({"function": cur_nd.get("name", cur_id),
                              "function_id": cur_id, "reason": "dead-end",
                              "depth": depth})
            return

        for next_id, via, new_pattern in next_hops:
            if next_id in visited:
                endpoints.append({
                    "function": G.nodes[next_id].get("name", next_id),
                    "function_id": next_id, "reason": "cycle",
                    "depth": depth + 1,
                })
                continue
            # Update the last hop's "via" info
            if hops:
                hops[-1]["via"] = via
            _follow(next_id, new_pattern, depth + 1, path + [next_id])

    _follow(start_id, value_pattern, 0, [start_id])

    return {
        "start": start_id,
        "pattern": value_pattern,
        "direction": direction,
        "hops": hops,
        "endpoints": endpoints,
        "truncated": len(hops) >= max_depth,
    }


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_value_flow(args):
    """Build and query value-flow edges.

    Usage:
        value-flow --graph <dir> --build            # build DATA_FLOW edges
        value-flow --graph <dir> --reverse --node <id> --pattern NULL
        value-flow --graph <dir> --taint --node <id> --pattern user_input
    """
    graph_dir = args.graph
    G = _load_full_graph_local(graph_dir)

    if getattr(args, "build", False):
        edges = build_data_flow_edges(G)
        out_path = os.path.join(graph_dir, ".code2database_data_flow.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"edges": edges, "count": len(edges)}, f,
                      ensure_ascii=False, indent=2)
        print(f"Built {len(edges)} DATA_FLOW/RETURN_FLOW edges → {out_path}",
              file=sys.stderr)
        return

    if getattr(args, "reverse", False):
        node = args.node
        pattern = args.pattern or "NULL"
        from _builder.utils import _find_node_id
        node_id = _find_node_id(G, node)
        if not node_id:
            _log.error("Node not found: %s", node)
            sys.exit(1)
        result = reverse_value_trace(G, node_id, pattern,
                                     getattr(args, "max_depth", 10))
        _output_value_result(result, getattr(args, "json", False))
        return

    if getattr(args, "taint", False):
        node = args.node
        pattern = args.pattern or "user_input"
        from _builder.utils import _find_node_id
        node_id = _find_node_id(G, node)
        if not node_id:
            _log.error("Node not found: %s", node)
            sys.exit(1)
        result = forward_taint_trace(G, node_id, pattern,
                                     getattr(args, "max_depth", 10))
        _output_value_result(result, getattr(args, "json", False))
        return

    if getattr(args, "interprocedural", False):
        node = args.node
        pattern = args.pattern or "NULL"
        direction = "reverse" if getattr(args, "reverse", False) else "forward"
        from _builder.utils import _find_node_id
        node_id = _find_node_id(G, node)
        if not node_id:
            _log.error("Node not found: %s", node)
            sys.exit(1)
        result = interprocedural_value_flow(
            G, node_id, pattern,
            max_depth=getattr(args, "max_depth", 10),
            direction=direction,
        )
        _output_value_result(result, getattr(args, "json", False))
        return

    print("Specify --build, --reverse, --taint, or --interprocedural",
          file=sys.stderr)
    sys.exit(1)


def _load_full_graph_local(graph_dir):
    try:
        from _builder.graph_build import _load_full_graph
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from _builder.graph_build import _load_full_graph
    return _load_full_graph(graph_dir)


def _output_value_result(result, json_mode):
    if json_mode:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
