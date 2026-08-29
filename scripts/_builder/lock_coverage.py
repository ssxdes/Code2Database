#!/usr/bin/env python3
"""Lock-coverage analysis for Code2Database.

Replaces the over-approximate "lock appears in body = held everywhere" rule
in _detect_locks_held with precise lock-held regions: for each access
(read/write of a field or global), determine which locks are actually held
at that point, by tracking lock acquire/release statements line-by-line.

Three improvements over the existing analysis:
1. Lock-held region tracking: lock is held only between acquire and release,
   not for the entire function body. Catches bugs like:
       lock(m); access(x); unlock(m); access(x);  // second access unprotected
2. Per-access lockset: each access records the set of locks held at that
   point, so we can answer "is this specific access protected?" precisely.
3. Caller-locks propagation: if a caller holds lock L when calling callee,
   the callee's accesses are also protected by L. This requires call-context
   analysis (we model it conservatively as a "may-be-held" set).

Algorithm (lightweight, regex-based, no full control-flow graph):
- Walk body_text line by line, maintaining a current_lockset.
- On `lock_acquire(L)`: add L to current_lockset.
- On `lock_release(L)`: remove L from current_lockset.
- On each access (field/global): record (access, current_lockset).
- After scan: report accesses with empty lockset as unprotected.

Limitations (documented, not silently wrong):
- No branch sensitivity: `if (cond) lock(m); access(x);` — we can't know
  if cond is true, so we conservatively treat access(x) as protected iff
  lock appears before it on any path. For now, we use lexical order, which
  is correct for straight-line code but over-approximates for branches.
- No interprocedural path-sensitivity: caller-locks is a single may-set,
  not per-call-site. False positives possible if a caller sometimes holds
  the lock and sometimes doesn't.
- The analysis is sound for the common case (structured code with explicit
  lock/unlock pairs) but should be combined with manual review.
"""

import re
import sys
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Set, Tuple
import logging


@dataclass
class Access:
    """A single field/global access with its lock context."""
    access_type: str  # 'read' or 'write'
    struct_chain: str = ""
    field_name: str = ""
    global_name: str = ""
    line: int = 0
    locks_held: Set[str] = field(default_factory=set)
    caller_locks: Set[str] = field(default_factory=set)  # inherited from caller
    protected: bool = False  # True iff locks_held ∪ caller_locks ≠ ∅
    source: str = ""  # snippet of the access line


@dataclass
class LockCoverage:
    """Lock-coverage result for one function."""
    function: str
    function_id: str = ""
    accesses: List[Access] = field(default_factory=list)
    lock_acquire_lines: List[Tuple[str, int]] = field(default_factory=list)
    lock_release_lines: List[Tuple[str, int]] = field(default_factory=list)
    unprotected_accesses: List[Access] = field(default_factory=list)
    over_approximation_warning: str = ""


# ---------------------------------------------------------------------------
# Lock-held region analysis
# ---------------------------------------------------------------------------

def _compile_lock_patterns(profile: Optional[Dict]) -> Tuple[List[re.Pattern], List[re.Pattern]]:
    """Compile lock acquire/release patterns from profile."""
    if not profile:
        return [], []
    conc = profile.get("concurrency_patterns", {}) or {}
    acquire_strs = conc.get("lock_acquire_patterns", []) or []
    release_strs = conc.get("lock_release_patterns", []) or []
    acquire_pats = [re.compile(p) for p in acquire_strs]
    release_pats = [re.compile(p) for p in release_strs]
    return acquire_pats, release_pats


def analyze_lock_coverage(ndata: Dict, profile: Optional[Dict] = None,
                          caller_locks: Optional[Set[str]] = None,
                          G=None, nid: Optional[str] = None) -> LockCoverage:
    """Analyze lock coverage for one function.

    Args:
        ndata: Node attrs dict.
        profile: Profile with concurrency_patterns.lock_acquire/release_patterns.
        caller_locks: Set of locks held by the caller when invoking this function
            (for interprocedural propagation).
        G, nid: Optional graph + node ID for lazy body_text fetch.

    Returns:
        LockCoverage with per-access locksets and unprotected-access list.
    """
    func_name = ndata.get("name", "")
    func_id = ndata.get("id", "")
    coverage = LockCoverage(function=func_name, function_id=func_id)
    if caller_locks:
        coverage.accesses  # placeholder, will be populated per-access

    acquire_pats, release_pats = _compile_lock_patterns(profile)
    if not acquire_pats and not release_pats:
        # No lock patterns → can't analyze; report all accesses as unprotected
        coverage.over_approximation_warning = (
            "no lock patterns configured — cannot analyze protection"
        )
        return coverage

    # Get body text
    if G is not None and nid is not None:
        try:
            from _builder.concurrency_analysis import _get_body_text
            body = _get_body_text(G, nid)
        except Exception:
            body = ndata.get("body_text", "")
    else:
        body = ndata.get("body_text", "")

    if not body:
        return coverage

    caller_locks = caller_locks or set()
    current_locks: Set[str] = set()
    lines = body.split("\n")

    for line_no, line in enumerate(lines, 1):
        # Build an event stream for this line: each acquire/release/access
        # is annotated with its character position so we can process them
        # in source order (not pattern-group order). This matters when a
        # single line has `mutex_lock(&m); x++; mutex_unlock(&m);` — we
        # must process lock-acquire BEFORE the access, and release AFTER.
        events: List[Tuple[int, str, Any]] = []  # (pos, kind, payload)

        for pat in acquire_pats:
            for m in pat.finditer(line):
                groups = m.groups()
                lock_name = groups[0].lstrip("&") if groups else "__rcu_read_lock__"
                events.append((m.start(), "acquire", lock_name))
        for pat in release_pats:
            for m in pat.finditer(line):
                groups = m.groups()
                lock_name = groups[0].lstrip("&") if groups else "__rcu_read_lock__"
                events.append((m.start(), "release", lock_name))
        # Field accesses — find their position in the line
        for fr in ndata.get("fields_read", []) or []:
            sc = fr.get("struct_chain", "")
            fn = fr.get("field_name", "")
            pos = _find_access_position(line, sc, fn)
            if pos is not None:
                events.append((pos, "access", {
                    "type": "read", "struct_chain": sc, "field_name": fn,
                    "global_name": "", "source": line.strip()[:100],
                }))
        for fw in ndata.get("fields_written", []) or []:
            sc = fw.get("struct_chain", "")
            fn = fw.get("field_name", "")
            pos = _find_access_position(line, sc, fn)
            if pos is not None:
                events.append((pos, "access", {
                    "type": "write", "struct_chain": sc, "field_name": fn,
                    "global_name": "", "source": line.strip()[:100],
                }))
        for gr in ndata.get("globals_read", []) or []:
            gname = gr.get("name", "")
            if gname:
                pos = _find_token_position(line, gname)
                if pos is not None:
                    events.append((pos, "access", {
                        "type": "read", "struct_chain": "", "field_name": "",
                        "global_name": gname, "source": line.strip()[:100],
                    }))
        for gw in ndata.get("globals_written", []) or []:
            gname = gw.get("name", "")
            if gname:
                pos = _find_token_position(line, gname)
                if pos is not None:
                    events.append((pos, "access", {
                        "type": "write", "struct_chain": "", "field_name": "",
                        "global_name": gname, "source": line.strip()[:100],
                    }))

        # Process events in source-order
        events.sort(key=lambda e: e[0])
        for pos, kind, payload in events:
            if kind == "acquire":
                current_locks.add(payload)
                coverage.lock_acquire_lines.append((payload, line_no))
            elif kind == "release":
                current_locks.discard(payload)
                coverage.lock_release_lines.append((payload, line_no))
            elif kind == "access":
                access = Access(
                    access_type=payload["type"],
                    struct_chain=payload["struct_chain"],
                    field_name=payload["field_name"],
                    global_name=payload["global_name"],
                    line=line_no, locks_held=set(current_locks),
                    caller_locks=set(caller_locks),
                    protected=bool(current_locks or caller_locks),
                    source=payload["source"],
                )
                coverage.accesses.append(access)
                if not access.protected:
                    coverage.unprotected_accesses.append(access)

    return coverage


# Cache for per-(struct_chain, field_name) regex patterns.
# Without this, re.search(re.escape(x)+...) recompiles on every
# (field access × line) pair — millions of calls on lock-coverage
# analysis of large functions. The cache keeps the most recent
# patterns (re module's internal 512-entry LRU is too small for
# unique struct_chain × field_name combinations).
_ACCESS_POS_CACHE = {}


def _find_access_position(line: str, struct_chain: str, field_name: str) -> Optional[int]:
    """Find the character position of a field access in a line."""
    if not field_name:
        return None
    # Try struct->field first
    if struct_chain and struct_chain not in ("(global)",):
        cache_key = (struct_chain, field_name)
        if cache_key not in _ACCESS_POS_CACHE:
            _ACCESS_POS_CACHE[cache_key] = re.compile(
                r'\b' + re.escape(struct_chain) + r'\s*(?:->|\.)\s*' + re.escape(field_name))
        m = _ACCESS_POS_CACHE[cache_key].search(line)
        if m:
            return m.start()
    # Fall back to field name alone
    if field_name not in _ACCESS_POS_CACHE:
        _ACCESS_POS_CACHE[field_name] = re.compile(r'\b' + re.escape(field_name) + r'\b')
    m = _ACCESS_POS_CACHE[field_name].search(line)
    return m.start() if m else None


_TOKEN_POS_CACHE = {}


def _find_token_position(line: str, token: str) -> Optional[int]:
    """Find the character position of a token in a line."""
    if token not in _TOKEN_POS_CACHE:
        _TOKEN_POS_CACHE[token] = re.compile(r'\b' + re.escape(token) + r'\b')
    m = _TOKEN_POS_CACHE[token].search(line)
    return m.start() if m else None


def _access_appears_on_line(line: str, struct_chain: str, field_name: str) -> bool:
    """Check if a struct field access appears on a line (kept for backward compat)."""
    return _find_access_position(line, struct_chain, field_name) is not None


def _token_appears_on_line(line: str, token: str) -> bool:
    """Check if a token appears as a word on a line (kept for backward compat)."""
    return _find_token_position(line, token) is not None


# ---------------------------------------------------------------------------
# Interprocedural: propagate caller locks to callees
# ---------------------------------------------------------------------------

def compute_caller_locks(G, node_id: str, profile: Optional[Dict] = None,
                         visited: Optional[Set[str]] = None) -> Set[str]:
    """Compute the set of locks any caller may hold when calling this function.

    Conservative: union of all callers' locksets at their call sites. This is
    a "may-be-held" set — if any caller holds lock L when calling us, L is in
    the set (even if other callers don't hold it).
    """
    if visited is None:
        visited = set()
    if node_id in visited:
        return set()  # avoid cycles
    visited.add(node_id)

    caller_locks: Set[str] = set()
    for pred in G.predecessors(node_id):
        ed = G.get_edge_data(pred, node_id) or {}
        if ed.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        # Analyze the caller's lock state at the call site
        pred_nd = G.nodes[pred]
        pred_coverage = analyze_lock_coverage(pred_nd, profile, G=G, nid=pred)
        # Find the call line for this callee and report locks held there
        callee_name = G.nodes[node_id].get("name", "")
        callee_args = pred_nd.get("callee_args", []) or []
        for ca in callee_args:
            if ca.get("callee") == callee_name:
                # The locks held at the call site are those that were acquired
                # before this call and not yet released. We approximate by
                # taking all locks acquired in the caller (over-approximation
                # for branches, but safe).
                for lock_name, _line in pred_coverage.lock_acquire_lines:
                    caller_locks.add(lock_name)
                break
        # Recurse: caller's caller_locks also apply
        caller_locks |= compute_caller_locks(G, pred, profile, visited)
    return caller_locks


# ---------------------------------------------------------------------------
# Race detection upgrade: use precise locksets
# ---------------------------------------------------------------------------

def detect_races_with_lock_coverage(G, profile: Optional[Dict] = None,
                                    max_pairs: int = 100) -> List[Dict]:
    """Detect races using precise per-access locksets.

    For each pair of accesses to the same field/global from different thread
    contexts, check if their locksets intersect. If not, it's a potential race.

    This replaces the over-approximate "function has lock" check with a
    per-access "this specific access is protected by lock L" check.
    """
    # Collect all accesses across the graph, grouped by access target
    by_target: Dict[str, List[Tuple[str, Access, str]]] = {}  # target → [(func, access, thread_model)]
    for nid, nd in G.nodes(data=True):
        if nd.get("is_empty", False) or nd.get("node_type") == "file":
            continue
        thread_model = nd.get("thread_model", "") or ""
        coverage = analyze_lock_coverage(nd, profile, G=G, nid=nid)
        for acc in coverage.accesses:
            target = acc.struct_chain + "->" + acc.field_name if acc.field_name else acc.global_name
            if not target or target == "->":
                continue
            by_target.setdefault(target, []).append((nid, acc, thread_model))

    # Find conflicting access pairs (same target, different thread, no common lock)
    races = []
    for target, accesses in by_target.items():
        if len(accesses) < 2:
            continue
        # Compare all pairs
        for i in range(len(accesses)):
            for j in range(i + 1, len(accesses)):
                func_a, acc_a, tm_a = accesses[i]
                func_b, acc_b, tm_b = accesses[j]
                # Same function — internal consistency, not a cross-function race
                if func_a == func_b:
                    continue
                # Same thread model — not concurrent
                if tm_a and tm_b and tm_a == tm_b:
                    continue
                # At least one must be a write
                if acc_a.access_type == "read" and acc_b.access_type == "read":
                    continue
                # Check lockset intersection
                locks_a = acc_a.locks_held | acc_a.caller_locks
                locks_b = acc_b.locks_held | acc_b.caller_locks
                common = locks_a & locks_b
                if not common:
                    races.append({
                        "target": target,
                        "access_a": {
                            "function": G.nodes[func_a].get("name", func_a),
                            "function_id": func_a,
                            "type": acc_a.access_type,
                            "line": acc_a.line,
                            "locks_held": list(locks_a),
                            "thread_model": tm_a,
                            "source": acc_a.source,
                        },
                        "access_b": {
                            "function": G.nodes[func_b].get("name", func_b),
                            "function_id": func_b,
                            "type": acc_b.access_type,
                            "line": acc_b.line,
                            "locks_held": list(locks_b),
                            "thread_model": tm_b,
                            "source": acc_b.source,
                        },
                        "common_locks": [],
                        "confidence": "EXTRACTED" if (locks_a or locks_b) else "AMBIGUOUS",
                    })
                    if len(races) >= max_pairs:
                        return races
    return races


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def cmd_lock_coverage(args):
    """Analyze lock coverage for a function or the whole graph.

    Usage:
        lock-coverage --graph <dir> --node <id>     # one function
        lock-coverage --graph <dir> --detect-races  # whole-graph race detection
    """
    import json
    import os
    graph_dir = args.graph
    try:
        from _builder.graph_build import _load_full_graph
    except ImportError:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from _builder.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)

    # Load profile for lock patterns
    profile = None
    profile_path = os.path.join(graph_dir, ".code2database_profile.json")
    if os.path.exists(profile_path):
        try:
            import json as _json
            with open(profile_path) as _f:
                profile = _json.load(_f)
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    if getattr(args, "detect_races", False):
        races = detect_races_with_lock_coverage(G, profile)
        print(json.dumps({
            "races": races,
            "count": len(races),
            "_source": "lock_coverage_analysis",
        }, ensure_ascii=False, indent=2, default=str))
        return

    node = getattr(args, "node", "")
    if not node:
        print("Error: --node or --detect-races required", file=sys.stderr)
        sys.exit(1)

    from _builder.utils import _find_node_id
    node_id = _find_node_id(G, node)
    if not node_id:
        print(f"Node not found: {node}", file=sys.stderr)
        sys.exit(1)

    # Compute caller-locks for interprocedural context
    caller_locks = compute_caller_locks(G, node_id, profile)
    nd = G.nodes[node_id]
    coverage = analyze_lock_coverage(nd, profile, caller_locks=caller_locks,
                                     G=G, nid=node_id)

    result = {
        "function": coverage.function,
        "function_id": coverage.function_id,
        "caller_locks": list(caller_locks),
        "lock_acquires": [{"lock": l, "line": n} for l, n in coverage.lock_acquire_lines],
        "lock_releases": [{"lock": l, "line": n} for l, n in coverage.lock_release_lines],
        "total_accesses": len(coverage.accesses),
        "protected_accesses": len([a for a in coverage.accesses if a.protected]),
        "unprotected_accesses": len(coverage.unprotected_accesses),
        "unprotected": [
            {
                "type": a.access_type,
                "struct_chain": a.struct_chain,
                "field_name": a.field_name,
                "global_name": a.global_name,
                "line": a.line,
                "locks_held": list(a.locks_held),
                "caller_locks": list(a.caller_locks),
                "source": a.source,
            }
            for a in coverage.unprotected_accesses
        ],
        "warning": coverage.over_approximation_warning,
        "_source": "lock_coverage_analysis",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
