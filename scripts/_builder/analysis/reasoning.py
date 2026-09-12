"""Rule-based reasoning over the code graph — deterministic and local.

Three engines convert graph facts into predicate tuples and derive
new knowledge without any LLM or network access:

  - ``DatalogEngine`` — semi-naive fixpoint evaluation supporting
    recursive rules (transitive closure over calls/imports)
  - ``ForwardChainEngine`` — classic forward chaining over
    non-recursive rules with per-rule firing dedup
  - ``abduce()`` — hypothesis generation: given an observation,
    propose rule conditions that would explain it, ranked by
    simplicity and confidence

Facts are 3-tuples ``(predicate, arg1, arg2)``; an empty second
argument models a unary predicate. Rule variables start with ``?``.
Built-in rules cover transitive calls, transitive imports, entry
reachability, domain and file dependencies, and callback
registration; projects can add their own via a JSON rules file.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

Fact = Tuple[str, str, str]
Atom = Tuple[str, str, str]

_MAX_ITERATIONS_DATALOG = 100
_MAX_ITERATIONS_FORWARD = 50
_DEFAULT_MAX_FACTS = 200000


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

def load_facts(graph_dir: str,
               max_facts: int = _DEFAULT_MAX_FACTS) -> Tuple[Set[Fact], bool]:
    """Convert the graph into predicate facts.

    Node facts: isFunction, definedIn, inDomain, hasLabel (one per
    label). Edge facts: calls (INVOKES/DISPATCH relations) and
    imports (IMPORTS relation). Returns (facts, truncated) where
    truncated signals the edge-fact budget was exhausted.
    """
    from _builder.graph.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)
    facts: Set[Fact] = set()
    for nid in G.nodes:
        nd = G.nodes[nid]
        facts.add(("isFunction", nid, ""))
        if nd.get("source_file"):
            facts.add(("definedIn", nid, nd["source_file"]))
        if nd.get("domain"):
            facts.add(("inDomain", nid, nd["domain"]))
        for lb in nd.get("labels", []) or []:
            facts.add(("hasLabel", nid, lb))
    truncated = False
    for u, v, ed in G.edges(data=True):
        if len(facts) >= max_facts:
            truncated = True
            break
        rel = ed.get("relation", "")
        if rel == "IMPORTS":
            facts.add(("imports", u, v))
        elif rel in ("", "INVOKES", "DISPATCH"):
            facts.add(("calls", u, v))
    return facts, truncated


# ---------------------------------------------------------------------------
# Rule validation
# ---------------------------------------------------------------------------

def _validate_rule(rule: Dict[str, Any], index: int) -> None:
    if not isinstance(rule, dict):
        raise ValueError(f"rules[{index}] must be an object")
    rid = rule.get("id")
    if not isinstance(rid, str) or not rid:
        raise ValueError(f"rules[{index}].id must be a non-empty string")
    kind = rule.get("kind", "forward")
    if kind not in ("forward", "datalog"):
        raise ValueError(
            f"rules[{index}].kind must be 'forward' or 'datalog'")
    conds = rule.get("conditions")
    if not isinstance(conds, list) or not conds:
        raise ValueError(f"rules[{index}].conditions must be a non-empty list")
    for c in conds:
        if not isinstance(c, list) or not 2 <= len(c) <= 3 or \
                not all(isinstance(x, str) for x in c):
            raise ValueError(
                f"rules[{index}] has a malformed condition {c!r}; "
                f"expected [predicate, arg1, arg2]")
    concl = rule.get("conclusion")
    if not isinstance(concl, list) or not 2 <= len(concl) <= 3 or \
            not all(isinstance(x, str) for x in concl):
        raise ValueError(
            f"rules[{index}].conclusion is malformed; "
            f"expected [predicate, arg1, arg2]")
    bound = set()
    for c in conds:
        bound.update(a for a in c[1:] if a.startswith("?"))
    for a in concl[1:]:
        if a.startswith("?") and a not in bound:
            raise ValueError(
                f"rule '{rid}': conclusion variable {a} is not bound "
                f"by any condition")


def _normalize_atom(raw: List[str]) -> Atom:
    pred = raw[0]
    a1 = raw[1] if len(raw) > 1 else ""
    a2 = raw[2] if len(raw) > 2 else ""
    return (pred, a1, a2)


def _normalize_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": rule["id"],
        "kind": rule.get("kind", "forward"),
        "conditions": [_normalize_atom(c) for c in rule["conditions"]],
        "conclusion": _normalize_atom(rule["conclusion"]),
        "confidence": float(rule.get("confidence", 1.0)),
    }


def load_rules_file(path: str) -> List[Dict[str, Any]]:
    """Load and validate user-defined rules from a JSON file.

    Format::

        {"rules": [{"id": "my_rule", "kind": "datalog",
                    "conditions": [["calls", "?A", "?B"]],
                    "conclusion": ["myPred", "?A", "?B"],
                    "confidence": 0.9}]}
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    raw_rules = data.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ValueError("rules file must contain a 'rules' list")
    rules = []
    for i, rule in enumerate(raw_rules):
        _validate_rule(rule, i)
        rules.append(_normalize_rule(rule))
    return rules


# ---------------------------------------------------------------------------
# Matching primitives
# ---------------------------------------------------------------------------

def _match_atom(atom: Atom, fact: Fact,
                binding: Dict[str, str]) -> Optional[Dict[str, str]]:
    if atom[0] != fact[0]:
        return None
    new = binding
    copied = False
    for a, f in ((atom[1], fact[1]), (atom[2], fact[2])):
        if a.startswith("?"):
            if a in binding:
                if binding[a] != f:
                    return None
            else:
                if not copied:
                    new = dict(binding)
                    copied = True
                new[a] = f
        elif a != f:
            return None
    return new


def _instantiate(atom: Atom, binding: Dict[str, str]) -> Fact:
    def _resolve(a: str) -> str:
        if a.startswith("?"):
            return binding.get(a, a)
        return a
    return (atom[0], _resolve(atom[1]), _resolve(atom[2]))


def _is_var(a: str) -> bool:
    return a.startswith("?")


# ---------------------------------------------------------------------------
# Forward chaining
# ---------------------------------------------------------------------------

class ForwardChainEngine:
    """Classic forward chaining to a fixpoint, non-recursive rules."""

    def __init__(self, facts: Set[Fact],
                 max_iterations: int = _MAX_ITERATIONS_FORWARD):
        self.facts: Set[Fact] = set(facts)
        self.index: Dict[str, Set[Fact]] = defaultdict(set)
        for f in self.facts:
            self.index[f[0]].add(f)
        self.max_iterations = max_iterations
        self.trace: List[Dict[str, Any]] = []

    def _match_conditions(self, conditions: List[Atom],
                          binding: Dict[str, str]) -> Iterable[Dict[str, str]]:
        if not conditions:
            yield binding
            return
        head = conditions[0]
        for fact in list(self.index.get(head[0], ())):
            nb = _match_atom(head, fact, binding)
            if nb is not None:
                yield from self._match_conditions(conditions[1:], nb)

    def run(self, rules: List[Dict[str, Any]]) -> int:
        fired: Set[Tuple[str, Fact]] = set()
        iterations = 0
        for it in range(self.max_iterations):
            iterations = it + 1
            new: List[Fact] = []
            pending: List[Tuple[Fact, Dict[str, Any], Dict[str, str]]] = []
            for rule in rules:
                for binding in self._match_conditions(rule["conditions"], {}):
                    concl = _instantiate(rule["conclusion"], binding)
                    key = (rule["id"], concl)
                    if key in fired or concl in self.facts:
                        continue
                    fired.add(key)
                    new.append(concl)
                    pending.append((concl, rule, binding))
            if not new:
                return it
            for concl in new:
                self.facts.add(concl)
                self.index[concl[0]].add(concl)
            for concl, rule, binding in pending:
                self.trace.append({
                    "iteration": it + 1,
                    "engine": "forward",
                    "rule": rule["id"],
                    "conclusion": list(concl),
                    "bindings": binding,
                })
        return iterations


# ---------------------------------------------------------------------------
# Datalog (semi-naive)
# ---------------------------------------------------------------------------

class DatalogEngine:
    """Semi-naive fixpoint evaluation with recursive rule support.

    Each round only rules with at least one condition position bound
    to the round's delta are re-evaluated (the other positions read
    the full fact index), so work is proportional to new facts rather
    than to the whole database.
    """

    def __init__(self, facts: Set[Fact],
                 max_iterations: int = _MAX_ITERATIONS_DATALOG):
        self.full: Set[Fact] = set(facts)
        self.index: Dict[str, Set[Fact]] = defaultdict(set)
        for f in self.full:
            self.index[f[0]].add(f)
        self.max_iterations = max_iterations
        self.trace: List[Dict[str, Any]] = []

    def _match_mixed(self, conditions: List[Atom], delta_pos: int,
                     delta_index: Dict[str, Set[Fact]],
                     binding: Dict[str, str],
                     pos: int) -> Iterable[Dict[str, str]]:
        if pos == len(conditions):
            yield binding
            return
        atom = conditions[pos]
        source = delta_index if pos == delta_pos else self.index
        for fact in list(source.get(atom[0], ())):
            nb = _match_atom(atom, fact, binding)
            if nb is not None:
                yield from self._match_mixed(
                    conditions, delta_pos, delta_index, nb, pos + 1)

    def run(self, rules: List[Dict[str, Any]]) -> int:
        delta: Set[Fact] = set(self.full)
        iteration = 0
        while delta and iteration < self.max_iterations:
            iteration += 1
            delta_index: Dict[str, Set[Fact]] = defaultdict(set)
            for f in delta:
                delta_index[f[0]].add(f)
            new: Set[Fact] = set()
            for rule in rules:
                conds = rule["conditions"]
                for delta_pos in range(len(conds)):
                    if conds[delta_pos][0] not in delta_index:
                        continue
                    for binding in self._match_mixed(
                            conds, delta_pos, delta_index, {}, 0):
                        concl = _instantiate(rule["conclusion"], binding)
                        if concl in self.full or concl in new:
                            continue
                        new.add(concl)
                        self.trace.append({
                            "iteration": iteration,
                            "engine": "datalog",
                            "rule": rule["id"],
                            "conclusion": list(concl),
                            "bindings": binding,
                        })
            if not new:
                return iteration - 1 if iteration > 1 else 0
            self.full |= new
            for f in new:
                self.index[f[0]].add(f)
            delta = new
        return iteration


# ---------------------------------------------------------------------------
# Abduction
# ---------------------------------------------------------------------------

def _unify_conclusion(conclusion: Atom,
                      observation: Fact) -> Optional[Dict[str, str]]:
    if conclusion[0] != observation[0]:
        return None
    binding: Dict[str, str] = {}
    for a, f in ((conclusion[1], observation[1]),
                 (conclusion[2], observation[2])):
        if _is_var(a):
            if a in binding and binding[a] != f:
                return None
            binding[a] = f
        elif a != f:
            return None
    return binding


def abduce(observation: Fact, rules: List[Dict[str, Any]],
           facts: Set[Fact]) -> Dict[str, Any]:
    """Generate ranked hypotheses explaining an observation.

    A rule explains the observation when its conclusion unifies with
    it; the hypothesis is that the rule's conditions hold (with the
    unification bindings applied). Ranking balances confidence
    against simplicity (fewer assumptions rank first).
    """
    hypotheses = []
    for rule in rules:
        binding = _unify_conclusion(rule["conclusion"], observation)
        if binding is None:
            continue
        assumptions = [_instantiate(c, binding) for c in rule["conditions"]]
        known = sum(1 for a in assumptions if a in facts)
        score = rule["confidence"] / (1.0 + len(assumptions))
        hypotheses.append({
            "rule": rule["id"],
            "assumptions": [list(a) for a in assumptions],
            "verified_assumptions": known,
            "confidence": rule["confidence"],
            "score": round(score, 4),
        })
    hypotheses.sort(key=lambda h: (-h["score"], h["rule"]))
    return {
        "observation": list(observation),
        "directly_observed": observation in facts,
        "hypotheses": hypotheses,
    }


# ---------------------------------------------------------------------------
# Built-in rules
# ---------------------------------------------------------------------------

BUILTIN_RULES: List[Dict[str, Any]] = [
    {
        "id": "transitive_calls_base",
        "kind": "datalog",
        "conditions": [("calls", "?A", "?B")],
        "conclusion": ("transitivelyCalls", "?A", "?B"),
        "confidence": 1.0,
    },
    {
        "id": "transitive_calls",
        "kind": "datalog",
        "conditions": [("transitivelyCalls", "?A", "?B"),
                       ("calls", "?B", "?C")],
        "conclusion": ("transitivelyCalls", "?A", "?C"),
        "confidence": 1.0,
    },
    {
        "id": "entry_reach",
        "kind": "datalog",
        "conditions": [("hasLabel", "?E", "API_entry"),
                       ("transitivelyCalls", "?E", "?X")],
        "conclusion": ("reachableFromEntry", "?X", ""),
        "confidence": 1.0,
    },
    {
        "id": "import_transitivity",
        "kind": "datalog",
        "conditions": [("imports", "?A", "?B"), ("imports", "?B", "?C")],
        "conclusion": ("transitivelyImports", "?A", "?C"),
        "confidence": 1.0,
    },
    {
        "id": "domain_depends",
        "kind": "forward",
        "conditions": [("calls", "?A", "?B"),
                       ("inDomain", "?A", "?DA"),
                       ("inDomain", "?B", "?DB")],
        "conclusion": ("domainCalls", "?DA", "?DB"),
        "confidence": 1.0,
    },
    {
        "id": "callback_registration",
        "kind": "forward",
        "conditions": [("calls", "?A", "?B"),
                       ("hasLabel", "?B", "callback_func")],
        "conclusion": ("registersCallback", "?A", "?B"),
        "confidence": 1.0,
    },
]

BUILTIN_RULE_IDS = {r["id"] for r in BUILTIN_RULES}


# ---------------------------------------------------------------------------
# Top-level runner
# ---------------------------------------------------------------------------

def run_reasoning(graph_dir: str, rules: List[Dict[str, Any]],
                  max_facts: int = _DEFAULT_MAX_FACTS,
                  limit: int = 100,
                  with_trace: bool = False) -> Dict[str, Any]:
    """Run datalog + forward engines over the graph facts.

    Datalog rules are evaluated first (recursion support), then
    forward rules chain over base + datalog-derived facts.
    """
    facts, truncated = load_facts(graph_dir, max_facts=max_facts)
    base_count = len(facts)
    base_set = set(facts)

    datalog_rules = [r for r in rules if r["kind"] == "datalog"]
    forward_rules = [r for r in rules if r["kind"] == "forward"]

    dl_iterations = 0
    fw_iterations = 0
    if datalog_rules:
        dl = DatalogEngine(facts)
        dl_iterations = dl.run(datalog_rules)
        facts = dl.full
        trace = list(dl.trace)
    else:
        trace = []
    if forward_rules:
        fw = ForwardChainEngine(facts)
        fw_iterations = fw.run(forward_rules)
        facts = fw.facts
        trace = trace + fw.trace

    derived = facts - base_set
    derived_counts: Dict[str, int] = defaultdict(int)
    for f in derived:
        derived_counts[f[0]] += 1
    sample = sorted(derived)[:limit]

    result: Dict[str, Any] = {
        "rules_run": [r["id"] for r in rules],
        "base_facts": base_count,
        "facts_truncated": truncated,
        "iterations": {"datalog": dl_iterations, "forward": fw_iterations},
        "derived_facts": dict(sorted(derived_counts.items())),
        "total_derived": len(derived),
        "sample": [list(f) for f in sample],
    }
    if with_trace:
        result["trace"] = trace[:2000]
    return result


_OBSERVATION_RE = re.compile(
    r"^\s*([A-Za-z_]\w*)\s*\(\s*([^,()]+?)?\s*(?:,\s*([^,()]+?)?\s*)?\)\s*$")


def parse_observation(text: str) -> Fact:
    """Parse ``predicate(arg1, arg2)`` / ``predicate(arg1)`` into a fact."""
    m = _OBSERVATION_RE.match(text)
    if not m:
        raise ValueError(
            f"cannot parse observation {text!r}; "
            f"expected predicate(arg1, arg2)")
    pred = m.group(1)
    a1 = (m.group(2) or "").strip().strip("'\"")
    a2 = (m.group(3) or "").strip().strip("'\"")
    return (pred, a1, a2)


def cmd_reason(args):
    """CLI handler for `code2database_builder.py reason`."""
    import sys
    try:
        rules = list(BUILTIN_RULES)
        rules_file = getattr(args, "rules_file", None)
        if rules_file:
            rules.extend(load_rules_file(rules_file))
        selected = getattr(args, "rule", None) or []
        if selected:
            by_id = {r["id"]: r for r in rules}
            missing = [r for r in selected if r not in by_id]
            if missing:
                raise ValueError(
                    f"unknown rule(s): {missing}; available: "
                    f"{sorted(by_id)}")
            rules = [by_id[r] for r in selected]
        explain = getattr(args, "explain", None)
        if explain:
            observation = parse_observation(explain)
            facts, _ = load_facts(args.graph)
            result = abduce(observation, rules, facts)
        else:
            result = run_reasoning(
                args.graph,
                rules,
                max_facts=getattr(args, "max_facts", _DEFAULT_MAX_FACTS),
                limit=getattr(args, "limit", 100),
                with_trace=bool(getattr(args, "trace", False)),
            )
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
