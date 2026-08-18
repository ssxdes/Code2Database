"""Tests for the CONFIG() filter extension in query_lang.py.

Verifies that Cypher queries like:
    MATCH (n:Function) WHERE CONFIG(n, 'CONFIG_X') RETURN n.name

parse, evaluate, and route correctly to the cgdb config_predicates table
via the registered _config_lookup_fn (or fall back to the node's
config_predicate_text attribute).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import networkx as nx

from _builder.query_lang import (
    parse_query, execute_query, _eval_where, WhereClause,
    set_config_lookup_fn,
)


def _build_test_graph_with_configs():
    """Build a small graph with config_predicate_text attributes."""
    G = nx.DiGraph()
    G.add_node("n1", name="foo", labels=["API_entry"], domain="kernel",
               config_predicate_text="CONFIG_X")
    G.add_node("n2", name="bar", labels=["thread_processor"], domain="kernel",
               config_predicate_text="NOT CONFIG_X")
    G.add_node("n3", name="baz", labels=["out_end"], domain="lib",
               config_predicate_text="CONFIG_X AND CONFIG_Y")
    G.add_node("n4", name="qux", labels=["API_entry"], domain="lib",
               config_predicate_text="")
    G.add_edge("n1", "n2", relation="INVOKES")
    return G


class TestConfigParser(unittest.TestCase):
    """Test that CONFIG(var, 'pred') parses correctly."""

    def test_simple_config_filter_parses(self):
        q = parse_query("MATCH (n) WHERE CONFIG(n, 'CONFIG_X') RETURN n.name")
        self.assertIsNotNone(q.where)
        self.assertEqual(q.where.op, "CONFIG")
        self.assertEqual(q.where.left, ("attr", "n"))
        self.assertEqual(q.where.right, ("lit", "CONFIG_X"))

    def test_config_filter_in_compound_expression(self):
        q = parse_query(
            "MATCH (n) WHERE n.domain = 'kernel' AND CONFIG(n, 'NOT CONFIG_X') "
            "RETURN n.name"
        )
        self.assertEqual(q.where.op, "AND")
        self.assertEqual(q.where.right.op, "CONFIG")
        self.assertEqual(q.where.right.right, ("lit", "NOT CONFIG_X"))

    def test_config_with_or(self):
        q = parse_query(
            "MATCH (n) WHERE CONFIG(n, 'CONFIG_X') OR CONFIG(n, 'CONFIG_Y') "
            "RETURN n.name"
        )
        self.assertEqual(q.where.op, "OR")
        self.assertEqual(q.where.left.op, "CONFIG")
        self.assertEqual(q.where.right.op, "CONFIG")

    def test_config_with_not_prefix(self):
        q = parse_query(
            "MATCH (n) WHERE NOT CONFIG(n, 'CONFIG_X') RETURN n.name"
        )
        self.assertEqual(q.where.op, "NOT")
        self.assertEqual(q.where.left.op, "CONFIG")


class TestConfigEvaluation(unittest.TestCase):
    """Test the _eval_where function with CONFIG op."""

    def test_direct_match_returns_true(self):
        where = WhereClause(op="CONFIG", left=("attr", "n"),
                            right=("lit", "CONFIG_X"))
        binding = {"n": {"id": 1, "config_predicate_text": "CONFIG_X"}}
        self.assertTrue(_eval_where(where, binding))

    def test_no_match_returns_false(self):
        where = WhereClause(op="CONFIG", left=("attr", "n"),
                            right=("lit", "CONFIG_X"))
        binding = {"n": {"id": 2, "config_predicate_text": "NOT CONFIG_X"}}
        self.assertFalse(_eval_where(where, binding))

    def test_compound_predicate_substring_match(self):
        where = WhereClause(op="CONFIG", left=("attr", "n"),
                            right=("lit", "CONFIG_X"))
        binding = {"n": {"id": 3,
                         "config_predicate_text": "CONFIG_X AND CONFIG_Y"}}
        self.assertTrue(_eval_where(where, binding))

    def test_empty_predicate_no_match(self):
        where = WhereClause(op="CONFIG", left=("attr", "n"),
                            right=("lit", "CONFIG_X"))
        binding = {"n": {"id": 4, "config_predicate_text": ""}}
        self.assertFalse(_eval_where(where, binding))

    def test_not_config_x_search_matches_not_config_x(self):
        where = WhereClause(op="CONFIG", left=("attr", "n"),
                            right=("lit", "NOT CONFIG_X"))
        binding = {"n": {"id": 5, "config_predicate_text": "NOT CONFIG_X"}}
        self.assertTrue(_eval_where(where, binding))

    def test_missing_predicate_attr_no_match(self):
        where = WhereClause(op="CONFIG", left=("attr", "n"),
                            right=("lit", "CONFIG_X"))
        binding = {"n": {"id": 6}}  # no config_predicate_text
        self.assertFalse(_eval_where(where, binding))

    def test_missing_node_var_no_match(self):
        where = WhereClause(op="CONFIG", left=("attr", "n"),
                            right=("lit", "CONFIG_X"))
        binding = {}
        self.assertFalse(_eval_where(where, binding))


class TestConfigLookupFunction(unittest.TestCase):
    """Test that the registered _config_lookup_fn is called when
    config_predicate_text is not present on the node."""

    def setUp(self):
        # Register a fake lookup fn
        self._fake_preds = {
            100: [{"text_form": "CONFIG_X", "config_macros": ["CONFIG_X"]}],
            200: [{"text_form": "NOT CONFIG_X", "config_macros": ["CONFIG_X"]}],
        }
        def lookup(node_id):
            return self._fake_preds.get(int(node_id), [])
        set_config_lookup_fn(lookup)

    def tearDown(self):
        set_config_lookup_fn(None)

    def test_lookup_fn_called_when_no_attr(self):
        where = WhereClause(op="CONFIG", left=("attr", "n"),
                            right=("lit", "CONFIG_X"))
        binding = {"n": {"id": 100}}  # no config_predicate_text, has id
        self.assertTrue(_eval_where(where, binding))

    def test_lookup_fn_returns_no_match(self):
        where = WhereClause(op="CONFIG", left=("attr", "n"),
                            right=("lit", "CONFIG_X"))
        binding = {"n": {"id": 200}}  # NOT CONFIG_X — should not match
        self.assertFalse(_eval_where(where, binding))

    def test_lookup_fn_with_unknown_id(self):
        where = WhereClause(op="CONFIG", left=("attr", "n"),
                            right=("lit", "CONFIG_X"))
        binding = {"n": {"id": 999}}
        self.assertFalse(_eval_where(where, binding))


class TestConfigExecuteQuery(unittest.TestCase):
    """End-to-end test: execute_query with CONFIG filter on a NetworkX graph."""

    def test_filter_returns_only_matching_nodes(self):
        G = _build_test_graph_with_configs()
        q = parse_query("MATCH (n) WHERE CONFIG(n, 'CONFIG_X') RETURN n.name")
        rows = execute_query(q, G)
        names = {r.get("n.name") or r.get("name") for r in rows}
        # foo (CONFIG_X), baz (CONFIG_X AND CONFIG_Y) — but NOT bar (NOT CONFIG_X)
        # qux has empty predicate so excluded
        self.assertIn("foo", names)
        self.assertIn("baz", names)
        self.assertNotIn("bar", names)
        self.assertNotIn("qux", names)

    def test_filter_with_not_prefix(self):
        G = _build_test_graph_with_configs()
        q = parse_query(
            "MATCH (n) WHERE NOT CONFIG(n, 'CONFIG_X') RETURN n.name"
        )
        rows = execute_query(q, G)
        names = {r.get("n.name") or r.get("name") for r in rows}
        # bar (NOT CONFIG_X) and qux (empty) — neither has CONFIG_X
        self.assertIn("bar", names)
        self.assertIn("qux", names)
        self.assertNotIn("foo", names)
        self.assertNotIn("baz", names)


if __name__ == "__main__":
    unittest.main()
