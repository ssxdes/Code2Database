"""Tests for C2 backport: cycle protection in var-length BFS path queries."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import networkx as nx

from _builder.query_lang import parse_query, execute_query
from _builder.query_lang import _bfs_paths, RelPattern, NodePattern


def _build_cyclic_graph():
    """A -> B -> A (cycle of length 2)."""
    G = nx.DiGraph()
    G.add_node("A", name="A", labels=[], domain="root")
    G.add_node("B", name="B", labels=[], domain="root")
    G.add_edge("A", "B", relation="INVOKES")
    G.add_edge("B", "A", relation="INVOKES")
    return G


def _build_self_loop_graph():
    """A -> A (self-loop)."""
    G = nx.DiGraph()
    G.add_node("A", name="A", labels=[], domain="root")
    G.add_edge("A", "A", relation="INVOKES")
    return G


def _build_long_cycle_graph():
    """A -> B -> C -> A (cycle of length 3) plus A -> D (acyclic branch)."""
    G = nx.DiGraph()
    G.add_node("A", name="A", labels=[], domain="root")
    G.add_node("B", name="B", labels=[], domain="root")
    G.add_node("C", name="C", labels=[], domain="root")
    G.add_node("D", name="D", labels=[], domain="root")
    G.add_edge("A", "B", relation="INVOKES")
    G.add_edge("B", "C", relation="INVOKES")
    G.add_edge("C", "A", relation="INVOKES")
    G.add_edge("A", "D", relation="INVOKES")
    return G


def _build_diamond_with_back_edge():
    """A -> B -> D
       A -> C -> D
       D -> A (back-edge creating cycle).
    """
    G = nx.DiGraph()
    G.add_node("A", name="A", labels=[], domain="root")
    G.add_node("B", name="B", labels=[], domain="root")
    G.add_node("C", name="C", labels=[], domain="root")
    G.add_node("D", name="D", labels=[], domain="root")
    G.add_edge("A", "B", relation="INVOKES")
    G.add_edge("A", "C", relation="INVOKES")
    G.add_edge("B", "D", relation="INVOKES")
    G.add_edge("C", "D", relation="INVOKES")
    G.add_edge("D", "A", relation="INVOKES")
    return G


class TestBFSCycleProtection(unittest.TestCase):
    """Test that _bfs_paths terminates on cyclic graphs and doesn't revisit nodes."""

    def test_simple_cycle_terminates(self):
        """A <-> B (cycle) doesn't cause infinite loop."""
        G = _build_cyclic_graph()
        rel = RelPattern(rel_type="INVOKES", direction="->", min_hops=1, max_hops=5)
        end_pat = NodePattern(variable="b", label=None, properties={})
        # Should terminate, not hang
        paths = _bfs_paths(G, "A", rel, 5, end_pat)
        # From A, we can reach B (1 hop) and that's it — A is in path so B->A is blocked
        nodes_seen = set()
        for path_n, _ in paths:
            nodes_seen.update(path_n)
        self.assertIn("A", nodes_seen)
        self.assertIn("B", nodes_seen)
        # A should not appear as a re-visited node at depth 2
        # (i.e., no path A -> B -> A)
        for path_n, _ in paths:
            # No node should appear twice in a single path
            self.assertEqual(len(path_n), len(set(path_n)),
                             f"Cycle not protected: {path_n}")

    def test_self_loop_skipped(self):
        """Self-loop A -> A is skipped (A already in path)."""
        G = _build_self_loop_graph()
        rel = RelPattern(rel_type="INVOKES", direction="->", min_hops=1, max_hops=3)
        end_pat = NodePattern(variable="b", label=None, properties={})
        paths = _bfs_paths(G, "A", rel, 3, end_pat)
        # From A, the only neighbor is A itself which is in path → no paths
        # The starting node is not returned as a path of length 0 (min_hops=1).
        for path_n, _ in paths:
            self.assertNotEqual(path_n, ["A", "A"],
                                "Self-loop not skipped")

    def test_long_cycle_terminates(self):
        """A -> B -> C -> A cycle doesn't cause infinite loop."""
        G = _build_long_cycle_graph()
        rel = RelPattern(rel_type="INVOKES", direction="->", min_hops=1, max_hops=10)
        end_pat = NodePattern(variable="b", label=None, properties={})
        # Should terminate
        paths = _bfs_paths(G, "A", rel, 10, end_pat)
        # Every path must have unique nodes (no cycle within a path)
        for path_n, _ in paths:
            self.assertEqual(len(path_n), len(set(path_n)),
                             f"Cycle not protected: {path_n}")
        # We should still be able to reach D (acyclic branch)
        all_nodes = set()
        for path_n, _ in paths:
            all_nodes.update(path_n)
        self.assertIn("D", all_nodes)
        self.assertIn("B", all_nodes)
        self.assertIn("C", all_nodes)

    def test_diamond_with_back_edge_terminates(self):
        """Diamond A->B->D, A->C->D, D->A terminates without infinite loop."""
        G = _build_diamond_with_back_edge()
        rel = RelPattern(rel_type="INVOKES", direction="->", min_hops=1, max_hops=10)
        end_pat = NodePattern(variable="b", label=None, properties={})
        paths = _bfs_paths(G, "A", rel, 10, end_pat)
        for path_n, _ in paths:
            self.assertEqual(len(path_n), len(set(path_n)),
                             f"Cycle not protected: {path_n}")

    def test_max_depth_respected(self):
        """Var-length query with max_depth=2 returns paths of length <= 2."""
        G = _build_long_cycle_graph()
        rel = RelPattern(rel_type="INVOKES", direction="->", min_hops=1, max_hops=2)
        end_pat = NodePattern(variable="b", label=None, properties={})
        paths = _bfs_paths(G, "A", rel, 2, end_pat)
        for path_n, _ in paths:
            # path_n includes start node, so length = hops + 1
            self.assertLessEqual(len(path_n) - 1, 2,
                                 f"Path exceeds max_depth: {path_n}")


class TestVarLengthQueryOnCycles(unittest.TestCase):
    """Test var-length path queries via the public execute_query API on cyclic graphs."""

    def test_varlen_query_terminates_on_cycle(self):
        """MATCH (a)-[:INVOKES*1..5]->(b) terminates on a cyclic graph."""
        G = _build_cyclic_graph()
        q = parse_query(
            "MATCH (a)-[:INVOKES*1..5]->(b) WHERE a.name = 'A' "
            "RETURN b.name AS callee")
        # Should not hang
        rows = execute_query(q, G)
        callees = {r["callee"] for r in rows}
        # From A, reachable nodes within 5 hops (with cycle protection): just B
        self.assertEqual(callees, {"B"})

    def test_varlen_query_long_cycle(self):
        """MATCH (a)-[:INVOKES*1..10]->(b) on A->B->C->A returns B, C, D."""
        G = _build_long_cycle_graph()
        q = parse_query(
            "MATCH (a)-[:INVOKES*1..10]->(b) WHERE a.name = 'A' "
            "RETURN b.name AS callee")
        rows = execute_query(q, G)
        callees = {r["callee"] for r in rows}
        # A->B (1 hop), A->B->C (2 hops), A->D (1 hop)
        # A->B->C->A is blocked (A in path)
        self.assertIn("B", callees)
        self.assertIn("C", callees)
        self.assertIn("D", callees)
        # A should not appear as a callee (it's the start, cycle protection blocks return)
        self.assertNotIn("A", callees)

    def test_varlen_query_self_loop(self):
        """MATCH (a)-[:INVOKES*1..3]->(b) on A->A doesn't return A as callee."""
        G = _build_self_loop_graph()
        q = parse_query(
            "MATCH (a)-[:INVOKES*1..3]->(b) WHERE a.name = 'A' "
            "RETURN b.name AS callee")
        rows = execute_query(q, G)
        callees = {r["callee"] for r in rows}
        # Self-loop is blocked by cycle protection
        self.assertEqual(callees, set())

    def test_varlen_query_diamond_with_back_edge(self):
        """Diamond with back edge returns reachable nodes without revisiting start."""
        G = _build_diamond_with_back_edge()
        q = parse_query(
            "MATCH (a)-[:INVOKES*1..10]->(b) WHERE a.name = 'A' "
            "RETURN b.name AS callee")
        rows = execute_query(q, G)
        callees = {r["callee"] for r in rows}
        # A -> B, A -> C, A -> B -> D, A -> C -> D
        # A -> ... -> A is blocked
        self.assertIn("B", callees)
        self.assertIn("C", callees)
        self.assertIn("D", callees)
        self.assertNotIn("A", callees)


class TestHeterogeneousTraversal(unittest.TestCase):
    """Test that var-length queries can traverse mixed edge types when unspecified."""

    def test_unspecified_rel_type_traverses_any(self):
        """Var-length path without rel_type traverses any edge type."""
        G = nx.DiGraph()
        G.add_node("A", name="A", labels=[], domain="root")
        G.add_node("B", name="B", labels=[], domain="root")
        G.add_node("C", name="C", labels=[], domain="root")
        G.add_edge("A", "B", relation="INVOKES")
        G.add_edge("B", "C", relation="DATA_FLOW")
        # No rel_type filter — should traverse both INVOKES and DATA_FLOW
        q = parse_query(
            "MATCH (a)-[*1..5]->(b) WHERE a.name = 'A' "
            "RETURN b.name AS callee")
        rows = execute_query(q, G)
        callees = {r["callee"] for r in rows}
        self.assertIn("B", callees)
        self.assertIn("C", callees)


if __name__ == "__main__":
    unittest.main()
