"""Unit tests for reasoning.py — datalog, forward chaining, abduction.

The reasoning module converts graph facts into predicate tuples and
derives new knowledge deterministically:

- DatalogEngine: semi-naive fixpoint with recursive rules
  (transitive closure converges, delta-only rounds)
- ForwardChainEngine: non-recursive rules with firing dedup
- abduce(): hypothesis generation ranked by simplicity + confidence
- load_facts: nodes/edges/labels → predicates, edge-fact budget
- rule validation: malformed rules rejected, unbound conclusion
  variables rejected
- parse_observation: predicate(arg1, arg2) parsing

Test coverage:
- transitive closure over a 4-hop chain (recursion)
- cycle-safe transitive closure
- entry reachability through labeled entries
- domain dependency join (3-atom rule with shared variables)
- callback registration rule
- custom rules file loading + validation errors
- abduction: matching conclusion, ranking, no match, direct fact
- observation parsing edge cases
- CLI: default run, --rule filter, unknown rule error, --explain
"""
import json
import os
import tempfile
import unittest

from tests.test_quality_checks import _make_quality_graph


def _reason_graph():
    """a(API_entry) -> b -> c -> d; e: callback_func; a registers e."""
    return _make_quality_graph(
        [{"id": "a", "name": "api_open", "labels": ["API_entry"],
          "domain": "lib.core", "source_file": "/core.c"},
         {"id": "b", "name": "mid_step", "domain": "lib.core",
          "source_file": "/core.c"},
         {"id": "c", "name": "deep_step", "domain": "lib.io",
          "source_file": "/io.c"},
         {"id": "d", "name": "leaf_do", "domain": "lib.io",
          "source_file": "/io.c"},
         {"id": "e", "name": "on_event", "labels": ["callback_func"],
          "domain": "lib.cb", "source_file": "/cb.c"}],
        [{"source": "a", "target": "b"},
         {"source": "b", "target": "c"},
         {"source": "c", "target": "d"},
         {"source": "a", "target": "e"},
         {"source": "d", "target": "b", "relation": "DATA_FLOW"}],
    )


class TestLoadFacts(unittest.TestCase):

    def test_node_and_edge_facts(self):
        from _builder.analysis.reasoning import load_facts
        facts, truncated = load_facts(_reason_graph())
        self.assertFalse(truncated)
        self.assertIn(("isFunction", "a", ""), facts)
        self.assertIn(("definedIn", "a", "/core.c"), facts)
        self.assertIn(("inDomain", "c", "lib.io"), facts)
        self.assertIn(("hasLabel", "a", "API_entry"), facts)
        self.assertIn(("calls", "a", "b"), facts)
        self.assertNotIn(("hasLabel", "b", "API_entry"), facts)

    def test_data_flow_edges_not_calls(self):
        from _builder.analysis.reasoning import load_facts
        facts, _ = load_facts(_reason_graph())
        # d -> b is DATA_FLOW and must not appear as a call
        self.assertNotIn(("calls", "d", "b"), facts)
        self.assertIn(("calls", "a", "b"), facts)

    def test_imports_relation_mapped(self):
        from _builder.analysis.reasoning import load_facts
        g = _make_quality_graph(
            [{"id": "f1", "node_type": "file", "labels": ["file"]},
             {"id": "f2", "node_type": "file", "labels": ["file"]}],
            [{"source": "f1", "target": "f2", "relation": "IMPORTS"}],
        )
        facts, _ = load_facts(g)
        self.assertIn(("imports", "f1", "f2"), facts)

    def test_fact_budget_truncates(self):
        from _builder.analysis.reasoning import load_facts
        g = _make_quality_graph(
            [{"id": "n%d" % i, "name": "f%d" % i} for i in range(10)],
            [{"source": "n%d" % i, "target": "n%d" % j}
             for i in range(10) for j in range(10) if i != j],
        )
        facts, truncated = load_facts(g, max_facts=30)
        self.assertTrue(truncated)
        self.assertLessEqual(len(facts), 30)


class TestDatalogEngine(unittest.TestCase):

    def test_transitive_calls_recursion(self):
        from _builder.analysis.reasoning import (
            DatalogEngine, load_facts, BUILTIN_RULES)
        facts, _ = load_facts(_reason_graph())
        rules = [r for r in BUILTIN_RULES
                 if r["id"].startswith("transitive_calls")]
        eng = DatalogEngine(facts)
        eng.run(rules)
        derived = eng.full - facts
        self.assertIn(("transitivelyCalls", "a", "b"), derived)
        self.assertIn(("transitivelyCalls", "a", "c"), derived)
        self.assertIn(("transitivelyCalls", "a", "d"), derived)
        self.assertIn(("transitivelyCalls", "b", "d"), derived)
        # not derived: reverse direction
        self.assertNotIn(("transitivelyCalls", "d", "a"), derived)

    def test_cycle_convergence(self):
        from _builder.analysis.reasoning import DatalogEngine
        facts = {("calls", "x", "y"), ("calls", "y", "x")}
        rules = [
            {"id": "tc_base", "kind": "datalog",
             "conditions": [("calls", "?A", "?B")],
             "conclusion": ("tc", "?A", "?B"), "confidence": 1.0},
            {"id": "tc_rec", "kind": "datalog",
             "conditions": [("tc", "?A", "?B"), ("calls", "?B", "?C")],
             "conclusion": ("tc", "?A", "?C"), "confidence": 1.0},
        ]
        eng = DatalogEngine(facts)
        eng.run(rules)
        derived = eng.full - facts
        self.assertEqual(derived, {("tc", "x", "y"), ("tc", "y", "x"),
                                   ("tc", "x", "x"), ("tc", "y", "y")})

    def test_shared_variable_join(self):
        from _builder.analysis.reasoning import DatalogEngine
        facts = {
            ("likes", "alice", "bob"), ("likes", "carol", "bob"),
            ("likes", "bob", "dave"),
        }
        rules = [{
            "id": "friend_of_friend", "kind": "datalog",
            "conditions": [("likes", "?X", "?Y"), ("likes", "?Y", "?Z")],
            "conclusion": ("knows", "?X", "?Z"), "confidence": 1.0,
        }]
        eng = DatalogEngine(facts)
        eng.run(rules)
        self.assertIn(("knows", "alice", "dave"), eng.full)
        self.assertIn(("knows", "carol", "dave"), eng.full)
        self.assertNotIn(("knows", "bob", "dave"), eng.full)


class TestForwardChainEngine(unittest.TestCase):

    def test_domain_depends_join(self):
        from _builder.analysis.reasoning import (
            ForwardChainEngine, load_facts, BUILTIN_RULES)
        facts, _ = load_facts(_reason_graph())
        rules = [r for r in BUILTIN_RULES if r["id"] == "domain_depends"]
        eng = ForwardChainEngine(facts)
        eng.run(rules)
        derived = eng.facts - facts
        self.assertIn(("domainCalls", "lib.core", "lib.io"), derived)
        self.assertIn(("domainCalls", "lib.core", "lib.cb"), derived)
        # intra-domain calls (a->b both lib.core, c->d both lib.io) are
        # real edges and are derived too
        self.assertIn(("domainCalls", "lib.core", "lib.core"), derived)
        self.assertIn(("domainCalls", "lib.io", "lib.io"), derived)
        # no edge ever touches lib.cb as a caller
        self.assertNotIn(("domainCalls", "lib.cb", "lib.core"), derived)

    def test_callback_registration(self):
        from _builder.analysis.reasoning import (
            ForwardChainEngine, load_facts, BUILTIN_RULES)
        facts, _ = load_facts(_reason_graph())
        rules = [r for r in BUILTIN_RULES
                 if r["id"] == "callback_registration"]
        eng = ForwardChainEngine(facts)
        eng.run(rules)
        self.assertIn(("registersCallback", "a", "e"), eng.facts)
        self.assertNotIn(("registersCallback", "b", "e"), eng.facts)

    def test_fixpoint_terminates_after_productive_round(self):
        from _builder.analysis.reasoning import ForwardChainEngine
        facts = {("p", "a", "b")}
        rules = [{
            "id": "noop", "kind": "forward",
            "conditions": [("p", "?A", "?B")],
            "conclusion": ("q", "?A", "?B"), "confidence": 1.0,
        }]
        eng = ForwardChainEngine(facts)
        iters = eng.run(rules)
        # one productive round, then quiescence
        self.assertEqual(iters, 1)
        self.assertEqual(len(eng.trace), 1)
        self.assertEqual(eng.facts - facts, {("q", "a", "b")})

    def test_trace_records_rule_and_bindings(self):
        from _builder.analysis.reasoning import ForwardChainEngine
        facts = {("p", "a", "b")}
        rules = [{
            "id": "r1", "kind": "forward",
            "conditions": [("p", "?A", "?B")],
            "conclusion": ("q", "?A", "?B"), "confidence": 1.0,
        }]
        eng = ForwardChainEngine(facts)
        eng.run(rules)
        entry = eng.trace[0]
        self.assertEqual(entry["rule"], "r1")
        self.assertEqual(entry["conclusion"], ["q", "a", "b"])
        self.assertEqual(entry["bindings"], {"?A": "a", "?B": "b"})


class TestRunReasoning(unittest.TestCase):

    def test_builtin_rules_end_to_end(self):
        from _builder.analysis.reasoning import run_reasoning, BUILTIN_RULES
        result = run_reasoning(_reason_graph(), BUILTIN_RULES)
        self.assertEqual(result["base_facts"], len(set(
            f for f in _facts_of(_reason_graph()))))
        self.assertIn("transitivelyCalls", result["derived_facts"])
        self.assertIn("reachableFromEntry", result["derived_facts"])
        self.assertIn("domainCalls", result["derived_facts"])
        self.assertIn(("reachableFromEntry", "d", ""),
                      [tuple(x) for x in result["sample"]] +
                      [tuple(x) for x in _derived_sample(result)])
        self.assertTrue(result["iterations"]["datalog"] >= 1)

    def test_rule_filter(self):
        from _builder.analysis.reasoning import run_reasoning, BUILTIN_RULES
        rules = [r for r in BUILTIN_RULES if r["id"] == "domain_depends"]
        result = run_reasoning(_reason_graph(), rules)
        self.assertEqual(result["rules_run"], ["domain_depends"])
        self.assertIn("domainCalls", result["derived_facts"])
        self.assertNotIn("transitivelyCalls", result["derived_facts"])

    def test_limit_caps_sample(self):
        from _builder.analysis.reasoning import run_reasoning, BUILTIN_RULES
        result = run_reasoning(_reason_graph(), BUILTIN_RULES, limit=2)
        self.assertLessEqual(len(result["sample"]), 2)

    def test_trace_included_when_requested(self):
        from _builder.analysis.reasoning import run_reasoning, BUILTIN_RULES
        result = run_reasoning(_reason_graph(), BUILTIN_RULES,
                               with_trace=True)
        self.assertIn("trace", result)
        self.assertTrue(result["trace"])
        self.assertEqual(result["trace"][0]["engine"], "datalog")


def _facts_of(graph_dir):
    from _builder.analysis.reasoning import load_facts
    facts, _ = load_facts(graph_dir)
    return facts


def _derived_sample(result):
    return result["sample"]


class TestRulesFile(unittest.TestCase):

    def _write_rules(self, rules: dict) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w") as f:
            json.dump(rules, f)
        return path

    def test_custom_rule_load_and_run(self):
        from _builder.analysis.reasoning import (
            load_rules_file, run_reasoning)
        path = self._write_rules({"rules": [{
            "id": "call_cb", "kind": "forward",
            "conditions": [["calls", "?A", "?B"],
                           ["hasLabel", "?B", "callback_func"]],
            "conclusion": ["customCb", "?A", "?B"],
            "confidence": 0.9,
        }]})
        try:
            rules = load_rules_file(path)
            result = run_reasoning(_reason_graph(), rules)
            self.assertIn("customCb", result["derived_facts"])
        finally:
            os.unlink(path)

    def test_unbound_conclusion_variable_rejected(self):
        from _builder.analysis.reasoning import load_rules_file
        path = self._write_rules({"rules": [{
            "id": "bad", "kind": "forward",
            "conditions": [["calls", "?A", "?B"]],
            "conclusion": ["x", "?A", "?C"],
        }]})
        try:
            with self.assertRaises(ValueError):
                load_rules_file(path)
        finally:
            os.unlink(path)

    def test_bad_kind_rejected(self):
        from _builder.analysis.reasoning import load_rules_file
        path = self._write_rules({"rules": [{
            "id": "bad", "kind": "abductive",
            "conditions": [["calls", "?A", "?B"]],
            "conclusion": ["x", "?A", "?B"],
        }]})
        try:
            with self.assertRaises(ValueError):
                load_rules_file(path)
        finally:
            os.unlink(path)

    def test_missing_id_rejected(self):
        from _builder.analysis.reasoning import load_rules_file
        path = self._write_rules({"rules": [{
            "kind": "forward",
            "conditions": [["calls", "?A", "?B"]],
            "conclusion": ["x", "?A", "?B"],
        }]})
        try:
            with self.assertRaises(ValueError):
                load_rules_file(path)
        finally:
            os.unlink(path)


class TestAbduction(unittest.TestCase):

    def test_hypothesis_from_matching_conclusion(self):
        from _builder.analysis.reasoning import abduce, load_facts, BUILTIN_RULES
        facts, _ = load_facts(_reason_graph())
        result = abduce(("reachableFromEntry", "d", ""), BUILTIN_RULES, facts)
        self.assertFalse(result["directly_observed"])
        hyps = {h["rule"]: h for h in result["hypotheses"]}
        self.assertIn("entry_reach", hyps)
        assumptions = [tuple(a) for a in hyps["entry_reach"]["assumptions"]]
        self.assertIn(("hasLabel", "?E", "API_entry"), assumptions)

    def test_verified_assumption_count(self):
        from _builder.analysis.reasoning import abduce, load_facts, BUILTIN_RULES
        facts, _ = load_facts(_reason_graph())
        result = abduce(("reachableFromEntry", "d", ""), BUILTIN_RULES, facts)
        hyps = {h["rule"]: h for h in result["hypotheses"]}
        # transitivelyCalls(a, d) IS derivable but not a base fact;
        # verified counts only base facts, so it stays unverified here
        entry = hyps["entry_reach"]
        self.assertLessEqual(entry["verified_assumptions"],
                             len(entry["assumptions"]))

    def test_no_matching_rule_gives_no_hypotheses(self):
        from _builder.analysis.reasoning import abduce, load_facts, BUILTIN_RULES
        facts, _ = load_facts(_reason_graph())
        result = abduce(("nonexistentPredicate", "x", "y"),
                        BUILTIN_RULES, facts)
        self.assertEqual(result["hypotheses"], [])
        self.assertFalse(result["directly_observed"])

    def test_directly_observed_fact(self):
        from _builder.analysis.reasoning import abduce, load_facts, BUILTIN_RULES
        facts, _ = load_facts(_reason_graph())
        result = abduce(("calls", "a", "b"), BUILTIN_RULES, facts)
        self.assertTrue(result["directly_observed"])

    def test_simpler_rules_rank_first(self):
        from _builder.analysis.reasoning import abduce
        rules = [
            {"id": "complex", "kind": "forward",
             "conditions": [("p", "?A", "?B"), ("q", "?B", "?C"),
                            ("r", "?C", "?D")],
             "conclusion": ("z", "?A", "?D"), "confidence": 1.0},
            {"id": "simple", "kind": "forward",
             "conditions": [("w", "?A", "?D")],
             "conclusion": ("z", "?A", "?D"), "confidence": 1.0},
        ]
        result = abduce(("z", "a", "d"), rules, set())
        self.assertEqual(result["hypotheses"][0]["rule"], "simple")


class TestParseObservation(unittest.TestCase):

    def test_two_args(self):
        from _builder.analysis.reasoning import parse_observation
        self.assertEqual(parse_observation("calls(a, b)"),
                         ("calls", "a", "b"))

    def test_one_arg(self):
        from _builder.analysis.reasoning import parse_observation
        self.assertEqual(parse_observation("reachableFromEntry(fn_x)"),
                         ("reachableFromEntry", "fn_x", ""))

    def test_quotes_stripped(self):
        from _builder.analysis.reasoning import parse_observation
        self.assertEqual(parse_observation('hasLabel(a, "API_entry")'),
                         ("hasLabel", "a", "API_entry"))

    def test_garbage_rejected(self):
        from _builder.analysis.reasoning import parse_observation
        with self.assertRaises(ValueError):
            parse_observation("not a predicate at all")


class TestReasonCLI(unittest.TestCase):

    def test_cli_default_run(self):
        import io
        from contextlib import redirect_stdout
        from _builder.analysis.reasoning import cmd_reason

        class Args:
            graph = _reason_graph()
            rule = None
            rules_file = None
            explain = None
            max_facts = 200000
            limit = 100
            trace = False

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_reason(Args())
        data = json.loads(buf.getvalue())
        self.assertIn("transitivelyCalls", data["derived_facts"])

    def test_cli_rule_filter_and_unknown(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from _builder.analysis.reasoning import cmd_reason

        class Args:
            graph = _reason_graph()
            rule = ["domain_depends"]
            rules_file = None
            explain = None
            max_facts = 200000
            limit = 100
            trace = False

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_reason(Args())
        data = json.loads(buf.getvalue())
        self.assertEqual(data["rules_run"], ["domain_depends"])

        Args.rule = ["no_such_rule"]
        with self.assertRaises(SystemExit) as cm:
            with redirect_stderr(io.StringIO()):
                cmd_reason(Args())
        self.assertEqual(cm.exception.code, 1)

    def test_cli_explain(self):
        import io
        from contextlib import redirect_stdout
        from _builder.analysis.reasoning import cmd_reason

        class Args:
            graph = _reason_graph()
            rule = None
            rules_file = None
            explain = "reachableFromEntry(d)"
            max_facts = 200000
            limit = 100
            trace = False

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_reason(Args())
        data = json.loads(buf.getvalue())
        self.assertEqual(data["observation"], ["reachableFromEntry", "d", ""])
        self.assertTrue(data["hypotheses"])


if __name__ == "__main__":
    unittest.main()
