#!/usr/bin/env python3
"""Runtime guard reasoning for Code2Database path-feasibility.

Complements compile-time `#ifdef` guard analysis (handled by
`check_config_feasible` in path_feasibility.py) with detection of common
Linux kernel runtime guard patterns:

1. **Exclusive hold regions** — `bd_prepare_to_claim()` acquires a holder
   lock, `bd_unclaim()` releases it. A path that crosses an acquire without
   a matching release is "guarded" by the holder; a path that calls
   `bd_unclaim()` without a preceding acquire is infeasible.

2. **Type predicates** — `if (!sb_is_blkdev_sb(sb))` guards the path with
   a runtime type check. The path is only feasible when the value passed
   to the predicate matches the expected type.

3. **Identity predicates** — `if (bd_holder != sb)` guards the path with
   a runtime pointer-identity check. Two edges with contradictory identity
   checks (`bd_holder == sb` vs `bd_holder != sb`) on the same path are
   infeasible.

4. **Lock-state checks** — `if (mutex_is_locked(&m))` or
   `if (spin_is_locked(&m))` gates the path on lock state. Paired with
   `mutex_lock()`/`mutex_unlock()` calls, this lets us reason about
   whether a path can execute while a lock is held.

The check returns a structured dict describing each guard found on the
path, whether the guard blocks the path (i.e., the path is infeasible
under the guard), and the inferred bindings (e.g.,
`{'sb_type': 'blkdev', 'bd_holder': '<sb>'}`).

Designed to be called from `cmd_path_feasible` in path_feasibility.py
alongside the existing `solve_path_feasibility` and
`check_config_feasible` calls.
"""

import re
from typing import List, Dict, Any, Optional, Tuple
import logging

_CONDITION_WRAPPER_RE = re.compile(
    r'^(if_cond|if|switch|else_if|case|while|for)\s*\((.*)\)\s*$', re.DOTALL
)
_GUARD_PAT_CACHE: Dict[str, re.Pattern] = {}


# ---------------------------------------------------------------------------
# Pattern 1: Exclusive hold regions (bd_prepare_to_claim / bd_unclaim)
# ---------------------------------------------------------------------------

# Match calls like bd_prepare_to_claim(bdev, holder, hops) — acquire
_log = logging.getLogger(__name__)

_ACQUIRE_RE = re.compile(
    r'\b(bd_prepare_to_claim|mutex_lock|mutex_lock_interruptible|'
    r'mutex_lock_killable|spin_lock|spin_lock_irq|spin_lock_irqsave|'
    r'down_read|down_write|down|read_lock|write_lock)\s*\('
)

# Match calls like bd_unclaim(bdev) or mutex_unlock(&m) — release
_RELEASE_RE = re.compile(
    r'\b(bd_unclaim|bd_abort_claim|mutex_unlock|spin_unlock|'
    r'spin_unlock_irq|spin_unlock_irqrestore|up|up_read|up_write|'
    r'read_unlock|write_unlock)\s*\('
)

# Acquire/release pairs — if we see an acquire on the path, the matching
# release later on the path "neutralizes" it (the lock is no longer held
# past that point).
_ACQUIRE_RELEASE_PAIRS = {
    "bd_prepare_to_claim": ("bd_unclaim", "bd_abort_claim"),
    "mutex_lock": ("mutex_unlock",),
    "mutex_lock_interruptible": ("mutex_unlock",),
    "mutex_lock_killable": ("mutex_unlock",),
    "spin_lock": ("spin_unlock",),
    "spin_lock_irq": ("spin_unlock_irq",),
    "spin_lock_irqsave": ("spin_unlock_irqrestore",),
    "down": ("up",),
    "down_read": ("up_read",),
    "down_write": ("up_write",),
    "read_lock": ("read_unlock",),
    "write_lock": ("write_unlock",),
}


def _detect_acquire_release(
    conditions: List[str],
) -> List[Dict[str, Any]]:
    """Walk the path's conditions in order, detect acquire/release calls.

    Returns a list of dicts:
        [{func, op: 'acquire'|'release', condition, depth_change}]

    `depth_change` is +1 for acquire, -1 for release — caller can sum it
    to know whether the path ends with a lock still held.
    """
    events = []
    for cond in conditions:
        # The condition string may be the call_condition from an edge,
        # which is typically `if(expr)` or `if_cond(expr)` or a bare
        # function call. Strip the wrapper to expose the inner expression.
        inner = _strip_condition_wrapper(cond)
        # Search for acquire calls
        for m in _ACQUIRE_RE.finditer(inner):
            func = m.group(1)
            events.append({
                "func": func,
                "op": "acquire",
                "condition": cond,
                "depth_change": 1,
            })
        for m in _RELEASE_RE.finditer(inner):
            func = m.group(1)
            events.append({
                "func": func,
                "op": "release",
                "condition": cond,
                "depth_change": -1,
            })
    return events


def _strip_condition_wrapper(cond: str) -> str:
    """Strip `if(...)`, `if_cond(...)`, `else if(...)`, `switch(...)` wrappers.

    Returns the inner expression so we can grep for function calls inside.
    """
    if not cond:
        return ""
    s = cond.strip()
    # Strip leading "else "
    if s.startswith("else "):
        s = s[5:]
    # Match `if(...)` or `if_cond(...)` or `switch(...)` — take inner
    m = _CONDITION_WRAPPER_RE.match(s)
    if m:
        return m.group(2)
    return s


def _analyze_hold_regions(
    events: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Given acquire/release events, determine if any lock is held at end.

    Returns:
        {
            'unbalanced_acquires': [list of acquire dicts without matching release],
            'unbalanced_releases': [list of release dicts without preceding acquire],
            'held_at_end': bool,
            'max_depth': int,
        }
    """
    # Stack of acquires not yet matched
    stack = []
    unbalanced_releases = []
    max_depth = 0
    current_depth = 0
    for ev in events:
        if ev["op"] == "acquire":
            stack.append(ev)
            current_depth += 1
            if current_depth > max_depth:
                max_depth = current_depth
        else:  # release
            # Find matching acquire on the stack — try exact pair first,
            # then any acquire (in practice, releases usually match the
            # most recent acquire, but kernel code sometimes releases in
            # a different order).
            matched_idx = None
            # Try exact-pair match first
            for i in range(len(stack) - 1, -1, -1):
                acq = stack[i]
                expected_releases = _ACQUIRE_RELEASE_PAIRS.get(acq["func"], ())
                if ev["func"] in expected_releases:
                    matched_idx = i
                    break
            if matched_idx is not None:
                stack.pop(matched_idx)
                current_depth -= 1
            else:
                unbalanced_releases.append(ev)
    return {
        "unbalanced_acquires": stack,
        "unbalanced_releases": unbalanced_releases,
        "held_at_end": len(stack) > 0,
        "max_depth": max_depth,
    }


# ---------------------------------------------------------------------------
# Pattern 2 & 3: Type/identity predicates in if-conditions
# ---------------------------------------------------------------------------

# sb_is_blkdev_sb(sb) → returns True if sb is a block-device superblock.
# Pattern: if(!sb_is_blkdev_sb(...)) — the path is guarded by "sb is NOT a
# blkdev superblock" — i.e., the path is only feasible when sb is NOT blkdev.
_TYPE_PREDICATE_RE = re.compile(
    r'!\s*(sb_is_blkdev_sb|sb_is_common|PageUptodate|PageDirty|PageLocked|'
    r'PageWriteback|PageError|PagePrivate|folio_test_dirty|folio_test_locked|'
    r'folio_test_writeback|folio_test_uptodate)\s*\(([^)]*)\)'
)

# Positive form: if(sb_is_blkdev_sb(...)) — path feasible when predicate is True.
_TYPE_PREDICATE_POS_RE = re.compile(
    r'(?<![\w!])\s*(sb_is_blkdev_sb|sb_is_common|PageUptodate|PageDirty|'
    r'PageLocked|PageWriteback|PageError|PagePrivate|folio_test_dirty|'
    r'folio_test_locked|folio_test_writeback|folio_test_uptodate)\s*\(([^)]*)\)'
)

# Identity predicate: if(bd_holder != sb) or if(bd_holder == sb)
# We capture both the variable and the value being compared.
_IDENTITY_NEQ_RE = re.compile(
    r'\b(\w+)\s*!=\s*(\w+(?:\s*->\s*\w+)?)'
)
_IDENTITY_EQ_RE = re.compile(
    r'\b(\w+)\s*==\s*(\w+(?:\s*->\s*\w+)?)'
)

# mutex_is_locked(&m) or spin_is_locked(&m) or mutex_is_locked(&dev->lock)
_LOCK_STATE_RE = re.compile(
    r'(mutex_is_locked|spin_is_locked|rwsem_is_locked|'
    r'lockdep_is_held|lock_is_held)\s*\(\s*&?\s*'
    r'([\w\->.]+)\s*\)'
)


def _extract_predicates(
    conditions: List[str],
) -> Dict[str, List[Dict]]:
    """Extract type/identity/lock-state predicates from path conditions.

    Returns:
        {
            'type_predicates': [{predicate, arg, negated, condition}],
            'identity_predicates': [{var, op, value, condition}],
            'lock_state_predicates': [{check, lock, condition}],
        }
    """
    type_preds = []
    identity_preds = []
    lock_state_preds = []

    for cond in conditions:
        inner = _strip_condition_wrapper(cond)

        # Track (predicate, arg) pairs already matched by the negative regex
        # so we don't double-count when the positive regex also matches them.
        negated_pairs = set()

        # Type predicates — negative form (if(!predicate(...)))
        for m in _TYPE_PREDICATE_RE.finditer(inner):
            pred, arg = m.group(1), m.group(2).strip()
            type_preds.append({
                "predicate": pred,
                "arg": arg,
                "negated": True,
                "condition": cond,
            })
            negated_pairs.add((pred, arg))
        # Type predicates — positive form (if(predicate(...)))
        for m in _TYPE_PREDICATE_POS_RE.finditer(inner):
            pred, arg = m.group(1), m.group(2).strip()
            if (pred, arg) in negated_pairs:
                continue
            type_preds.append({
                "predicate": pred,
                "arg": arg,
                "negated": False,
                "condition": cond,
            })

        # Identity predicates
        for m in _IDENTITY_NEQ_RE.finditer(inner):
            # Skip trivial comparisons like x != NULL
            if m.group(2) in ("NULL", "null", "0"):
                continue
            identity_preds.append({
                "var": m.group(1),
                "op": "!=",
                "value": m.group(2).strip(),
                "condition": cond,
            })
        for m in _IDENTITY_EQ_RE.finditer(inner):
            if m.group(2) in ("NULL", "null", "0"):
                continue
            identity_preds.append({
                "var": m.group(1),
                "op": "==",
                "value": m.group(2).strip(),
                "condition": cond,
            })

        # Lock-state predicates
        for m in _LOCK_STATE_RE.finditer(inner):
            lock_state_preds.append({
                "check": m.group(1),
                "lock": m.group(2).strip(),
                "condition": cond,
            })

    return {
        "type_predicates": type_preds,
        "identity_predicates": identity_preds,
        "lock_state_predicates": lock_state_preds,
    }


# ---------------------------------------------------------------------------
# Contradiction detection
# ---------------------------------------------------------------------------

def _find_identity_contradictions(
    identity_preds: List[Dict],
) -> List[Tuple[Dict, Dict]]:
    """Find pairs of identity predicates on the same variable that
    contradict each other (e.g., `bd_holder != sb` and `bd_holder == sb`).

    Returns a list of (pred_a, pred_b) tuples.
    """
    contradictions = []
    for i, a in enumerate(identity_preds):
        for b in identity_preds[i + 1:]:
            if a["var"] != b["var"]:
                continue
            if a["op"] != b["op"]:
                # Same var, different op — check if values are compatible
                # `a != sb` + `b == sb` → contradiction
                if a["value"] == b["value"]:
                    contradictions.append((a, b))
            else:
                # Same op — `a == sb` + `b == foo` → contradiction if values differ
                if a["op"] == "==" and a["value"] != b["value"]:
                    contradictions.append((a, b))
                elif a["op"] == "!=" and a["value"] != b["value"]:
                    # `a != sb` + `b != foo` — not a contradiction per se,
                    # but does narrow the var to be neither sb nor foo.
                    pass
    return contradictions


def _find_type_predicate_contradictions(
    type_preds: List[Dict],
) -> List[Tuple[Dict, Dict]]:
    """Find pairs of type predicates on the same arg that contradict.

    E.g., `sb_is_blkdev_sb(sb)` (positive) + `!sb_is_blkdev_sb(sb)` (negative)
    on the same path is a contradiction.
    """
    contradictions = []
    for i, a in enumerate(type_preds):
        for b in type_preds[i + 1:]:
            if a["predicate"] != b["predicate"]:
                continue
            if a["arg"] != b["arg"]:
                continue
            if a["negated"] != b["negated"]:
                contradictions.append((a, b))
    return contradictions


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_runtime_guards(conditions: List[str]) -> Dict[str, Any]:
    """Detect all runtime guard patterns in the path's conditions.

    Args:
        conditions: list of `call_condition` strings from the path's edges.

    Returns:
        Dict with keys:
            guards: list of guard descriptors
                [{kind: 'acquire'|'release'|'type_predicate'|'identity_predicate'|'lock_state',
                  ...}]
            acquire_release: {events, analysis}
            type_predicates: list
            identity_predicates: list
            lock_state_predicates: list
            guards_count: int
    """
    events = _detect_acquire_release(conditions)
    analysis = _analyze_hold_regions(events)
    preds = _extract_predicates(conditions)

    guards = []
    for ev in events:
        guards.append({
            "kind": ev["op"],  # 'acquire' or 'release'
            "func": ev["func"],
            "condition": ev["condition"],
        })
    for tp in preds["type_predicates"]:
        guards.append({
            "kind": "type_predicate",
            "predicate": tp["predicate"],
            "arg": tp["arg"],
            "negated": tp["negated"],
            "condition": tp["condition"],
        })
    for ip in preds["identity_predicates"]:
        guards.append({
            "kind": "identity_predicate",
            "var": ip["var"],
            "op": ip["op"],
            "value": ip["value"],
            "condition": ip["condition"],
        })
    for lp in preds["lock_state_predicates"]:
        guards.append({
            "kind": "lock_state",
            "check": lp["check"],
            "lock": lp["lock"],
            "condition": lp["condition"],
        })

    return {
        "guards": guards,
        "acquire_release": {
            "events": events,
            "analysis": analysis,
        },
        "type_predicates": preds["type_predicates"],
        "identity_predicates": preds["identity_predicates"],
        "lock_state_predicates": preds["lock_state_predicates"],
        "guards_count": len(guards),
    }


def check_runtime_guards(conditions: List[str]) -> Dict[str, Any]:
    """Check whether runtime guards block the path's feasibility.

    Args:
        conditions: list of `call_condition` strings from the path's edges.

    Returns:
        Dict with keys:
            feasible: True / False (False if any contradiction or
                unbalanced release detected; True otherwise)
            guards: list of guard descriptors (same as detect_runtime_guards)
            contradictions: list of (a, b) tuples describing contradictory
                predicate pairs
            unbalanced_releases: list of release events without preceding acquire
            held_at_end: bool — True if any lock is still held at end of path
            inferred_bindings: dict of bindings implied by guards
                (e.g., {'sb_type': 'blkdev'} from a positive sb_is_blkdev_sb
                predicate)
            confidence: 'EXTRACTED' / 'INFERRED' / 'AMBIGUOUS'
            reason: str (when infeasible)
    """
    return check_runtime_guards_with_profile(conditions, guard_functions=None)


def _detect_profile_guards(
    conditions: List[str],
    guard_functions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Detect guard functions declared in the project profile.

    Each profile entry has the form:
        {"function": "<name>", "kind": <kind>, "effect": <effect>,
         "arg_index": <int>, "description": <str>}

    kind ∈ {"type_predicate", "identity_predicate", "lock_state",
            "acquire", "release"}.

    Returns a list of guard descriptors mirroring the structure produced
    by `detect_runtime_guards` so the two can be merged by the caller.
    """
    if not guard_functions:
        return []
    # Build a regex per declared function — match `name(...)` allowing
    # optional `!` prefix for negated forms (type_predicate only).
    compiled = []
    for entry in guard_functions:
        name = entry.get("function", "")
        if not name:
            continue
        kind = entry.get("kind", "")
        cache_key = f"{kind}:{name}"
        pat = _GUARD_PAT_CACHE.get(cache_key)
        if pat is None:
            # Bound the cache to prevent unbounded growth in long-running
            # MCP server mode (evict all when full — regexes are cheap).
            if len(_GUARD_PAT_CACHE) >= 2048:
                _GUARD_PAT_CACHE.clear()
            esc = re.escape(name)
            if kind == "type_predicate":
                pat = re.compile(rf'(!?)\b{esc}\s*\(([^)]*)\)')
            else:
                pat = re.compile(rf'\b{esc}\s*\(([^)]*)\)')
            _GUARD_PAT_CACHE[cache_key] = pat
        compiled.append((entry, pat))

    results = []
    for cond in conditions:
        inner = _strip_condition_wrapper(cond)
        for entry, pat in compiled:
            for m in pat.finditer(inner):
                negated = False
                arg_str = m.group(m.lastindex)
                if entry.get("kind") == "type_predicate":
                    negated = m.group(1) == "!"
                # Extract the arg_index-th argument from the call.
                arg = ""
                if arg_str:
                    parts = [p.strip() for p in arg_str.split(",")]
                    ai = entry.get("arg_index", 0)
                    if 0 <= ai < len(parts):
                        arg = parts[ai]
                guard_desc = {
                    "kind": entry.get("kind", ""),
                    "function": entry.get("function", ""),
                    "arg": arg,
                    "negated": negated,
                    "condition": cond,
                    "description": entry.get("description", ""),
                    "source": "profile",
                }
                if entry.get("effect"):
                    guard_desc["effect"] = entry["effect"]
                results.append(guard_desc)
    return results


def check_runtime_guards_with_profile(
    conditions: List[str],
    guard_functions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Check runtime guards, optionally augmented by profile-declared guards.

    When `guard_functions` is provided (list of dicts from the project profile),
    those declarations are matched against the path's conditions in addition
    to the hardcoded regex patterns. Profile-declared guards produce
    `inferred_bindings` and `contradictions` just like the built-in predicates.

    Args:
        conditions: list of `call_condition` strings from the path's edges.
        guard_functions: optional list of profile entries, e.g.:
            [{"function": "sb_is_blkdev_sb", "kind": "type_predicate",
              "effect": "blkdev", "arg_index": 0,
              "description": "returns true when arg0 is a block-device superblock"}]

    Returns: same shape as `check_runtime_guards`, with two additions:
        - `guards` entries may carry `source: "profile"` for profile-declared
          guards (built-in regex matches have no `source` key).
        - `profile_bindings` lists bindings inferred specifically from
          profile-declared type predicates (e.g., `{"sb_type": "blkdev"}`).
    """
    detection = detect_runtime_guards(conditions)
    guards = detection["guards"]
    analysis = detection["acquire_release"]["analysis"]

    contradictions: List[Dict[str, Any]] = []
    inferred_bindings: Dict[str, Any] = {}
    profile_bindings: Dict[str, Any] = {}

    # Identity-predicate contradictions
    for a, b in _find_identity_contradictions(detection["identity_predicates"]):
        contradictions.append({
            "kind": "identity_predicate",
            "a": a,
            "b": b,
            "reason": f"identity check {a['var']} {a['op']} {a['value']} "
                      f"contradicts {b['var']} {b['op']} {b['value']}",
        })

    # Type-predicate contradictions
    for a, b in _find_type_predicate_contradictions(detection["type_predicates"]):
        contradictions.append({
            "kind": "type_predicate",
            "a": a,
            "b": b,
            "reason": f"{a['predicate']}({a['arg']}) is both asserted and negated",
        })

    # Unbalanced releases — a release without an acquire is suspicious
    unbalanced_releases = analysis["unbalanced_releases"]

    # Inferred bindings from type predicates
    for tp in detection["type_predicates"]:
        # sb_is_blkdev_sb(sb) positive → sb is a blkdev superblock
        if tp["predicate"] == "sb_is_blkdev_sb" and not tp["negated"]:
            inferred_bindings[f"{tp['arg']}_type"] = "blkdev"
        elif tp["predicate"] == "sb_is_blkdev_sb" and tp["negated"]:
            inferred_bindings[f"{tp['arg']}_type"] = "!blkdev"

    # Inferred bindings from identity predicates
    for ip in detection["identity_predicates"]:
        if ip["op"] == "==":
            inferred_bindings[ip["var"]] = ip["value"]
        elif ip["op"] == "!=":
            inferred_bindings[f"{ip['var']}_ne"] = ip["value"]

    # profile-declared guard functions.
    profile_guards = _detect_profile_guards(conditions, guard_functions or [])
    if profile_guards:
        for pg in profile_guards:
            guards.append(pg)
            kind = pg.get("kind", "")
            arg = pg.get("arg", "")
            effect = pg.get("effect", "")
            negated = pg.get("negated", False)
            if kind == "type_predicate" and arg and effect:
                key = f"{arg}_type"
                if negated:
                    profile_bindings[key] = f"!{effect}"
                    inferred_bindings[key] = f"!{effect}"
                else:
                    profile_bindings[key] = effect
                    inferred_bindings[key] = effect
            elif kind == "identity_predicate" and arg and effect:
                # effect encodes the comparison value (e.g., "sb" for bd_holder == sb)
                profile_bindings[arg] = effect
                inferred_bindings[arg] = effect
            elif kind in ("acquire", "release") and arg:
                # Track lock state — arg is the lock object.
                profile_bindings.setdefault("lock_objects", set()).add(arg)
                inferred_bindings.setdefault("lock_objects", set()).add(arg)
            elif kind == "lock_state" and arg:
                profile_bindings.setdefault("queried_locks", set()).add(arg)
                inferred_bindings.setdefault("queried_locks", set()).add(arg)
        # Convert sets to sorted lists for JSON serializability.
        for k, v in profile_bindings.items():
            if isinstance(v, set):
                profile_bindings[k] = sorted(v)
        for k, v in inferred_bindings.items():
            if isinstance(v, set):
                inferred_bindings[k] = sorted(v)

    # Determine feasibility
    feasible = True
    reason = ""
    if contradictions:
        feasible = False
        reason = "; ".join(c["reason"] for c in contradictions)
    elif unbalanced_releases:
        # Unbalanced release is a code smell but doesn't strictly make the
        # path infeasible (the lock might have been acquired earlier in a
        # caller). Mark as AMBIGUOUS.
        feasible = True
        reason = f"unbalanced release(s): {[r['func'] for r in unbalanced_releases]}"

    # Confidence
    if contradictions:
        confidence = "EXTRACTED"
    elif guards:
        confidence = "INFERRED"
    else:
        confidence = "EXTRACTED"

    return {
        "feasible": feasible,
        "guards": guards,
        "guards_count": len(guards),
        "contradictions": contradictions,
        "unbalanced_releases": unbalanced_releases,
        "held_at_end": analysis["held_at_end"],
        "max_lock_depth": analysis["max_depth"],
        "inferred_bindings": inferred_bindings,
        "profile_bindings": profile_bindings,
        "confidence": confidence,
        "reason": reason,
    }


def cmd_runtime_guards(args):
    """CLI wrapper: `runtime-guards --conditions "..."`."""
    import json
    import sys

    if not getattr(args, "conditions", ""):
        _log.error("Specify --conditions")
        sys.exit(1)

    conds = [c.strip() for c in args.conditions.split("|||") if c.strip()]
    result = check_runtime_guards(conds)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
