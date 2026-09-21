"""Tests for the call-graph copy guard in _make_call_graph.

_make_call_graph duplicates every node and edge into a fresh DiGraph —
on a LazySQLiteGraph (>=50K nodes, backed by SQLite) that is the eager
load the lazy view exists to avoid: multi-million-node graphs OOM the
process (or hit the MCP per-tool timeout mid-copy). The guard turns
that into an explainable failure in every consumer context.
"""
import os
import sys
import unittest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

import networkx as nx  # noqa: E402

from _builder.utils import _make_call_graph  # noqa: E402


class LazySQLiteGraph:
    _db_path = "/tmp/fake.db"

    def number_of_nodes(self):
        return 2295083


class TestMakeCallGraphGuard(unittest.TestCase):

    def test_lazy_sqlite_graph_refused_with_explanation(self):
        with self.assertRaises(RuntimeError) as cm:
            _make_call_graph(LazySQLiteGraph())
        msg = str(cm.exception)
        self.assertIn("LazySQLiteGraph", msg)
        self.assertIn("2295083", msg)

    def test_in_memory_graph_still_copied(self):
        G = nx.DiGraph()
        G.add_node("a", name="a")
        G.add_node("f", node_type="file")
        G.add_edge("f", "a", relation="CONTAINS")
        G.add_edge("a", "a", relation="INVOKES")
        call_G = _make_call_graph(G)
        self.assertIn("a", call_G)
        self.assertNotIn(("f", "a"), call_G.edges)  # CONTAINS excluded
        # skip_file_nodes drops file nodes entirely
        call_G2 = _make_call_graph(G, skip_file_nodes=True)
        self.assertNotIn("f", call_G2)


if __name__ == "__main__":
    unittest.main()
