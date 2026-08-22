"""Runtime guard detection for path feasibility analysis.

Detects 4 types of runtime guards in path conditions:
1. Acquire/release (bd_prepare_to_claim/bd_unclaim, mutex_lock/unlock)
2. Type predicates (sb_is_blkdev_sb, folio_test_*)
3. Identity predicates (bd_holder != sb, bd_holder == sb)
4. Lock state checks (mutex_is_locked, spin_is_locked)
"""
import re
import sys
from typing import List, Dict, Any, Optional, Tuple

_ACQUIRE_RE = re.compile(
    r'\b(bd_prepare_to_claim|mutex_lock|mutex_trylock|spin_lock|spin_trylock|'
    r'down_write|down_read|down|__down|rwsem_down|percpu_down)'
    r'\s*\(([^)]*)\)'
)
_RELEASE_RE = re.compile(
    r'\b(bd_unclaim|mutex_unlock|spin_unlock|up|__up|rwsem_up|percpu_up)'
    r'\s*\(([^)]*)\)'
)
_TYPE_PREDICATE_RE = re.compile(
    r'\b(sb_is_blkdev_sb|folio_test_[a-z_]+|Page[A-Z][a-z]+|'
    r'is_vm_hugetlb_page|is_zone_device_page|PageHead|PageTail|'
    r'PageLRU|PageActive|PageDirty|PageUptoday|PageWriteback|'
    r'PageLocked|PagePrivate|PageMappedToDisk)\s*\(([^)]*)\)'
)
_TYPE_PREDICATE_NEG_RE = re.compile(
    r'!\s*(sb_is_blkdev_sb|folio_test_[a-z_]+|Page[A-Z][a-z]+|'
    r'is_vm_hugetlb_page|is_zone_device_page|PageHead|PageTail|'
    r'PageLRU|PageActive|PageDirty|PageUptoday|PageWriteback|'
    r'PageLocked|PagePrivate|PageMappedToDisk)\s*\(([^)]*)\)'
)
_IDENTITY_NE_RE = re.compile(r'(\w+)\s*!=\s*(\w+)')
_IDENTITY_EQ_RE = re.compile(r'(\w+)\s*==\s*(\w+)')
_LOCK_STATE_RE = re.compile(
    r'\b(mutex_is_locked|spin_is_locked|rwsem_is_locked|'
    r'percpu_rwsem_is_held|lockdep_is_held)\s*\(\s*&?\s*(\w+)\s*\)'
)


def _strip_condition_wrapper(cond: str) -> str:
    cond = cond.strip()
    if cond.startswith('if('):
        cond = cond[3:]
    elif cond.startswith('if ('):
        cond = cond[4:]
    elif cond.startswith('if_cond('):
        cond = cond[len('if_cond('):]
    elif cond.startswith('else if('):
        cond = cond[len('else if('):]
    elif cond.startswith('else if ('):
        cond = cond[len('else if ('):]
    elif cond.startswith('switch('):
        cond = cond[len('switch('):]
    elif cond.startswith('switch ('):
        cond = cond[len('switch ('):]
    if cond.endswith(')'):
        cond = cond[:-1]
    return cond.strip()


def _detect_acquire_release(conditions: List[str]) -> List[Dict[str, Any]]:
    events = []
    for cond in conditions:
        stripped = _strip_condition_wrapper(cond)
        for m in _ACQUIRE_RE.finditer(stripped):
            events.append({
                'kind': 'acquire', 'function': m.group(1),
                'arg': m.group(2).strip(), 'raw': cond,
            })
        for m in _RELEASE_RE.finditer(stripped):
            events.append({
                'kind': 'release', 'function': m.group(1),
                'arg': m.group(2).strip(), 'raw': cond,
            })
    return events


def _analyze_hold_regions(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    stack = []
    unbalanced_acquires = []
    unbalanced_releases = []
    held_at_end = []
    max_depth = 0
    for ev in events:
        if ev['kind'] == 'acquire':
            stack.append(ev)
            if len(stack) > max_depth:
                max_depth = len(stack)
        elif ev['kind'] == 'release':
            if stack:
                stack.pop()
            else:
                unbalanced_releases.append(ev)
    for acq in stack:
        unbalanced_acquires.append(acq)
        held_at_end.append(acq)
    return {
        'unbalanced_acquires': unbalanced_acquires,
        'unbalanced_releases': unbalanced_releases,
        'held_at_end': held_at_end,
        'max_lock_depth': max_depth,
    }


def _extract_predicates(conditions: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    type_preds = []
    identity_preds = []
    lock_state_preds = []
    negated_pairs = set()
    for cond in conditions:
        stripped = _strip_condition_wrapper(cond)
        for m in _TYPE_PREDICATE_NEG_RE.finditer(stripped):
            type_preds.append({
                'predicate': m.group(1), 'arg': m.group(2).strip(),
                'negated': True, 'raw': cond,
            })
            negated_pairs.add((m.group(1), m.group(2).strip()))
        for m in _TYPE_PREDICATE_RE.finditer(stripped):
            if (m.group(1), m.group(2).strip()) not in negated_pairs:
                type_preds.append({
                    'predicate': m.group(1), 'arg': m.group(2).strip(),
                    'negated': False, 'raw': cond,
                })
        for m in _IDENTITY_NE_RE.finditer(stripped):
            if m.group(2) not in ('NULL', 'null', '0', 'None'):
                identity_preds.append({
                    'var': m.group(1), 'value': m.group(2),
                    'op': '!=', 'raw': cond,
                })
        for m in _IDENTITY_EQ_RE.finditer(stripped):
            if m.group(2) not in ('NULL', 'null', '0', 'None'):
                identity_preds.append({
                    'var': m.group(1), 'value': m.group(2),
                    'op': '==', 'raw': cond,
                })
        for m in _LOCK_STATE_RE.finditer(stripped):
            lock_state_preds.append({
                'function': m.group(1), 'arg': m.group(2),
                'raw': cond,
            })
    return {
        'type_predicates': type_preds,
        'identity_predicates': identity_preds,
        'lock_state_predicates': lock_state_preds,
    }


def _find_identity_contradictions(preds: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    contradictions = []
    ne_pairs = {}
    eq_pairs = {}
    for p in preds:
        key = (p['var'], p['value'])
        if p['op'] == '!=':
            ne_pairs[key] = p
        else:
            eq_pairs[key] = p
    for key, ne_pred in ne_pairs.items():
        if key in eq_pairs:
            contradictions.append({
                'kind': 'identity_contradiction',
                'var': key[0], 'value': key[1],
                'reason': f"{key[0]} != {key[1]} and {key[0]} == {key[1]} simultaneously",
                'ne_raw': ne_pred['raw'],
                'eq_raw': eq_pairs[key]['raw'],
            })
    return contradictions


def _find_type_predicate_contradictions(preds: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    contradictions = []
    pos_pairs = {}
    neg_pairs = {}
    for p in preds:
        key = (p['predicate'], p['arg'])
        if p['negated']:
            neg_pairs[key] = p
        else:
            pos_pairs[key] = p
    for key, neg_pred in neg_pairs.items():
        if key in pos_pairs:
            contradictions.append({
                'kind': 'type_predicate_contradiction',
                'predicate': key[0], 'arg': key[1],
                'reason': f"!{key[0]}({key[1]}) and {key[0]}({key[1]}) simultaneously",
                'neg_raw': neg_pred['raw'],
                'pos_raw': pos_pairs[key]['raw'],
            })
    return contradictions


def check_runtime_guards(conditions: List[str]) -> Dict[str, Any]:
    if not conditions:
        return {'feasible': True, 'guards': [], 'contradictions': [],
                'unbalanced_releases': [], 'held_at_end': [],
                'max_lock_depth': 0, 'inferred_bindings': {},
                'confidence': 1.0, 'reason': 'no conditions'}
    events = _detect_acquire_release(conditions)
    hold_info = _analyze_hold_regions(events)
    preds = _extract_predicates(conditions)
    contradictions = []
    contradictions.extend(_find_identity_contradictions(preds['identity_predicates']))
    contradictions.extend(_find_type_predicate_contradictions(preds['type_predicates']))
    guards = events + preds['type_predicates'] + preds['identity_predicates'] + preds['lock_state_predicates']
    inferred_bindings = {}
    for p in preds['identity_predicates']:
        if p['op'] == '!=':
            inferred_bindings[f"{p['var']}_ne"] = p['value']
        else:
            inferred_bindings[f"{p['var']}_eq"] = p['value']
    for tp in preds['type_predicates']:
        arg = tp['arg']
        pred = tp['predicate']
        if pred == 'sb_is_blkdev_sb':
            inferred_bindings[f"{arg}_type"] = "!blkdev" if tp['negated'] else "blkdev"
        elif pred.startswith('folio_test_'):
            inferred_bindings[f"{arg}_type"] = f"!{pred[11:]}" if tp['negated'] else pred[11:]
    feasible = len(contradictions) == 0
    reason = "no contradictions" if feasible else f"{len(contradictions)} contradiction(s) found"
    if hold_info['unbalanced_releases']:
        reason += f"; {len(hold_info['unbalanced_releases'])} unbalanced release(s)"
    return {
        'feasible': feasible,
        'guards': guards,
        'guard_count': len(guards),
        'contradictions': contradictions,
        'unbalanced_releases': hold_info['unbalanced_releases'],
        'held_at_end': hold_info['held_at_end'],
        'max_lock_depth': hold_info['max_lock_depth'],
        'inferred_bindings': inferred_bindings,
        'confidence': 0.0 if contradictions else (0.7 if hold_info['unbalanced_releases'] else 0.9),
        'reason': reason,
    }


def cmd_runtime_guards(args):
    import json
    conditions_str = getattr(args, 'conditions', '') or ''
    conditions = [c.strip() for c in conditions_str.split('|||') if c.strip()]
    if not conditions:
        print("Error: --conditions required (separate with |||)", file=sys.stderr)
        sys.exit(1)
    result = check_runtime_guards(conditions)
    print(json.dumps(result, ensure_ascii=False, indent=2))
