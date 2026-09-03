"""Tests for Cypher-subset extensions (D25): aggregates, GROUP BY, ORDER BY DESC."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import networkx as nx

from _builder.query_lang import (
    parse_query, execute_query, Query, ReturnItem,
)


def _build_test_graph():
    """Build a small test graph with 4 functions across 2 domains."""
    G = nx.DiGraph()
    G.add_node("n1", name="foo", labels=["API_entry"], domain="kernel",
               line=10, complexity=5)
    G.add_node("n2", name="bar", labels=["thread_processor"], domain="kernel",
               line=20, complexity=10)
    G.add_node("n3", name="baz", labels=["out_end"], domain="lib",
               line=30, complexity=2)
    G.add_node("n4", name="qux", labels=["API_entry"], domain="lib",
               line=40, complexity=8)
    G.add_edge("n1", "n2", relation="INVOKES")
    G.add_edge("n2", "n3", relation="INVOKES")
    return G


class TestCountAggregate(unittest.TestCase):
    """Test COUNT(*) and COUNT(attr) aggregates."""

    def test_count_star_returns_row_count(self):
        """COUNT(*) without GROUP BY returns the total row count."""
        G = _build_test_graph()
        q = parse_query("MATCH (n) RETURN COUNT(*)")
        rows = execute_query(q, G)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count(*)"], 4)

    def test_count_attr_returns_non_null_count(self):
        """COUNT(n.complexity) counts rows with non-null complexity."""
        G = _build_test_graph()
        q = parse_query("MATCH (n) RETURN COUNT(n.complexity) AS c")
        rows = execute_query(q, G)
        self.assertEqual(rows[0]["c"], 4)


class TestSumAvgAggregates(unittest.TestCase):
    """Test SUM and AVG aggregates."""

    def test_sum_aggregate(self):
        """SUM(n.complexity) sums the complexity values."""
        G = _build_test_graph()
        q = parse_query("MATCH (n) RETURN SUM(n.complexity) AS total")
        rows = execute_query(q, G)
        # 5 + 10 + 2 + 8 = 25
        self.assertEqual(rows[0]["total"], 25)

    def test_avg_aggregate(self):
        """AVG(n.complexity) returns the mean."""
        G = _build_test_graph()
        q = parse_query("MATCH (n) RETURN AVG(n.complexity) AS avg_c")
        rows = execute_query(q, G)
        # 25 / 4 = 6.25
        self.assertAlmostEqual(rows[0]["avg_c"], 6.25)


class TestMinMaxAggregates(unittest.TestCase):
    """Test MIN and MAX aggregates."""

    def test_min_aggregate(self):
        """MIN(n.complexity) returns the minimum."""
        G = _build_test_graph()
        q = parse_query("MATCH (n) RETURN MIN(n.complexity) AS min_c")
        rows = execute_query(q, G)
        self.assertEqual(rows[0]["min_c"], 2)

    def test_max_aggregate(self):
        """MAX(n.complexity) returns the maximum."""
        G = _build_test_graph()
        q = parse_query("MATCH (n) RETURN MAX(n.complexity) AS max_c")
        rows = execute_query(q, G)
        self.assertEqual(rows[0]["max_c"], 10)


class TestGroupBy(unittest.TestCase):
    """Test GROUP BY clause."""

    def test_group_by_domain_with_count(self):
        """GROUP BY n.domain with COUNT(*) groups rows by domain."""
        G = _build_test_graph()
        q = parse_query(
            "MATCH (n) RETURN n.domain AS d, COUNT(*) AS cnt GROUP BY n.domain")
        rows = execute_query(q, G)
        # Two domains: kernel (2 functions) and lib (2 functions)
        self.assertEqual(len(rows), 2)
        domain_counts = {r["d"]: r["cnt"] for r in rows}
        self.assertEqual(domain_counts.get("kernel"), 2)
        self.assertEqual(domain_counts.get("lib"), 2)

    def test_group_by_with_sum(self):
        """GROUP BY with SUM aggregates within each group."""
        G = _build_test_graph()
        q = parse_query(
            "MATCH (n) RETURN n.domain AS d, SUM(n.complexity) AS total "
            "GROUP BY n.domain")
        rows = execute_query(q, G)
        domain_totals = {r["d"]: r["total"] for r in rows}
        # kernel: 5 + 10 = 15, lib: 2 + 8 = 10
        self.assertEqual(domain_totals.get("kernel"), 15)
        self.assertEqual(domain_totals.get("lib"), 10)


class TestOrderByDesc(unittest.TestCase):
    """Test ORDER BY with DESC."""

    def test_order_by_ascending(self):
        """ORDER BY n.line sorts ascending by default."""
        G = _build_test_graph()
        q = parse_query("MATCH (n) RETURN n.line AS l ORDER BY n.line LIMIT 3")
        rows = execute_query(q, G)
        lines = [r["l"] for r in rows]
        self.assertEqual(lines, [10, 20, 30])

    def test_order_by_descending(self):
        """ORDER BY n.line DESC sorts descending."""
        G = _build_test_graph()
        q = parse_query("MATCH (n) RETURN n.line AS l ORDER BY n.line DESC LIMIT 2")
        rows = execute_query(q, G)
        lines = [r["l"] for r in rows]
        self.assertEqual(lines, [40, 30])


class TestCompoundWhere(unittest.TestCase):
    """Test compound WHERE clauses with AND/OR/NOT."""

    def test_where_and(self):
        """WHERE n.domain = 'kernel' AND n.line > 15 returns matching rows."""
        G = _build_test_graph()
        q = parse_query(
            "MATCH (n) WHERE n.domain = 'kernel' AND n.line > 15 RETURN n.name AS name")
        rows = execute_query(q, G)
        names = {r["name"] for r in rows}
        self.assertEqual(names, {"bar"})  # only bar is kernel + line 20 > 15

    def test_where_or(self):
        """WHERE n.line < 15 OR n.line > 35 returns matching rows."""
        G = _build_test_graph()
        q = parse_query(
            "MATCH (n) WHERE n.line < 15 OR n.line > 35 RETURN n.name AS name")
        rows = execute_query(q, G)
        names = {r["name"] for r in rows}
        self.assertEqual(names, {"foo", "qux"})  # foo(10) and qux(40)

    def test_where_not(self):
        """WHERE NOT n.domain = 'kernel' returns lib-domain rows."""
        G = _build_test_graph()
        q = parse_query(
            "MATCH (n) WHERE NOT n.domain = 'kernel' RETURN n.name AS name")
        rows = execute_query(q, G)
        names = {r["name"] for r in rows}
        self.assertEqual(names, {"baz", "qux"})


class TestVarLengthPath(unittest.TestCase):
    """Test variable-length path syntax *1..N."""

    def test_varlen_path_finds_reachable(self):
        """(a)-[:INVOKES*1..3]->(b) finds reachable callees up to 3 hops."""
        G = _build_test_graph()
        # n1 -> n2 -> n3, so from n1 we can reach n2 (1 hop) and n3 (2 hops)
        q = parse_query(
            "MATCH (a)-[:INVOKES*1..3]->(b) WHERE a.name = 'foo' "
            "RETURN b.name AS callee")
        rows = execute_query(q, G)
        callees = {r["callee"] for r in rows}
        self.assertIn("bar", callees)  # 1 hop
        self.assertIn("baz", callees)  # 2 hops


class TestReturnItemParsing(unittest.TestCase):
    """Test parsing of ReturnItem with aggregates."""

    def test_aggregate_parsed_correctly(self):
        """COUNT(n.line) is parsed as an aggregate ReturnItem."""
        q = parse_query("MATCH (n) RETURN COUNT(n.line) AS c")
        self.assertEqual(len(q.return_items), 1)
        item = q.return_items[0]
        self.assertTrue(item.is_aggregate)
        self.assertEqual(item.aggregate_func, "COUNT")
        self.assertEqual(item.aggregate_arg, "n.line")
        self.assertEqual(item.alias, "c")

    def test_non_aggregate_parsed_correctly(self):
        """n.name is parsed as a non-aggregate ReturnItem."""
        q = parse_query("MATCH (n) RETURN n.name AS name")
        self.assertEqual(len(q.return_items), 1)
        item = q.return_items[0]
        self.assertFalse(item.is_aggregate)


class TestLimitSemantics(unittest.TestCase):
    """LIMIT parsing and canonical semantics (negative = unlimited,
    0 = zero rows — matching SQLite)."""

    def test_limit_non_integer_is_syntax_error(self):
        for bad in ("MATCH (n) RETURN n LIMIT abc",
                    "MATCH (n) RETURN n LIMIT 2.5"):
            with self.assertRaises(SyntaxError, msg=bad):
                parse_query(bad)

    def test_limit_negative_means_unlimited(self):
        q = parse_query("MATCH (n) RETURN n LIMIT -1")
        self.assertIsNone(q.limit)
        G = _build_test_graph()
        self.assertEqual(len(execute_query(q, G)), 4)

    def test_limit_zero_returns_no_rows(self):
        G = _build_test_graph()
        for query in ("MATCH (n) RETURN n LIMIT 0",
                      "MATCH (n) RETURN n ORDER BY n.line LIMIT 0",
                      "MATCH (n) RETURN COUNT(*) LIMIT 0",
                      "MATCH (a)-[:INVOKES]->(b) RETURN a LIMIT 0",
                      "MATCH (a)-[:INVOKES*1..2]->(b) RETURN a LIMIT 0"):
            rows = execute_query(parse_query(query), G)
            self.assertEqual(rows, [], query)

    def test_limit_positive_slices_normally(self):
        G = _build_test_graph()
        rows = execute_query(parse_query("MATCH (n) RETURN n LIMIT 2"), G)
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
