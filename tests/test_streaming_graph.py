"""Tests for StreamingGraph and LazySQLiteGraph (streaming_graph.py).

Covers:
- LazySQLiteGraph negative cache eviction (set.keys() crash regression)
- StreamingGraph.close() idempotency (double-close empty-table regression)
- LazySQLiteGraph basic read operations
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.streaming_graph import StreamingGraph, LazySQLiteGraph


class TestLazySQLiteGraphNegativeCache(unittest.TestCase):
    """Regression test for the set.keys() crash (BUG-LZ-1)."""

    def _build_db(self, tmpdir):
        """Create a DB with 3 functions and 2 edges via StreamingGraph."""
        db_path = os.path.join(tmpdir, "code2database.db")
        sg = StreamingGraph(db_path)
        sg.add_node("func_a", name="func_a", source_file="test.c", line=10, domain="root")
        sg.add_node("func_b", name="func_b", source_file="test.c", line=20, domain="root")
        sg.add_node("func_c", name="func_c", source_file="test.c", line=30, domain="root")
        sg.add_edge("func_a", "func_b", call_order=0, edge_confidence="EXTRACTED")
        sg.add_edge("func_b", "func_c", call_order=0, edge_confidence="EXTRACTED")
        sg.close()
        return db_path

    def test_eviction_does_not_crash(self):
        """Looking up >_node_neg_cache_max nonexistent IDs must not crash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._build_db(tmpdir)
            with LazySQLiteGraph(db_path) as g:
                g._node_neg_cache_max = 5
                for i in range(20):
                    result = f"nonexistent_{i}" in g
                    self.assertFalse(result)
                self.assertLessEqual(len(g._node_neg_cache),
                                     g._node_neg_cache_max)

    def test_existing_nodes_found(self):
        """Nodes that exist in the DB are found via __contains__."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._build_db(tmpdir)
            with LazySQLiteGraph(db_path) as g:
                self.assertIn("func_a", g)
                self.assertIn("func_b", g)
                self.assertIn("func_c", g)
                self.assertNotIn("func_d", g)


class TestLazySQLiteGraphBasicRead(unittest.TestCase):
    """Basic read operations on LazySQLiteGraph."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(self._tmpdir, "code2database.db")
        sg = StreamingGraph(db_path)
        sg.add_node("func_a", name="func_a", source_file="test.c", line=10, domain="root")
        sg.add_node("func_b", name="func_b", source_file="test.c", line=20, domain="root")
        sg.add_node("func_c", name="func_c", source_file="test.c", line=30, domain="root")
        sg.add_edge("func_a", "func_b", call_order=0, edge_confidence="EXTRACTED")
        sg.add_edge("func_b", "func_c", call_order=0, edge_confidence="EXTRACTED")
        sg.close()
        self._db_path = os.path.join(self._tmpdir, "code2database.db")
        self._g = LazySQLiteGraph(self._db_path)
        self._g.__enter__()

    def tearDown(self):
        self._g.__exit__(None, None, None)
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_successors(self):
        succs = list(self._g.successors("func_a"))
        self.assertIn("func_b", succs)

    def test_predecessors(self):
        preds = list(self._g.predecessors("func_b"))
        self.assertIn("func_a", preds)

    def test_has_edge(self):
        self.assertTrue(self._g.has_edge("func_a", "func_b"))
        self.assertFalse(self._g.has_edge("func_a", "func_c"))

    def test_number_of_nodes(self):
        self.assertEqual(self._g.number_of_nodes(), 3)

    def test_number_of_edges(self):
        self.assertEqual(self._g.number_of_edges(), 2)


class TestStreamingGraphCloseIdempotent(unittest.TestCase):
    """Regression test for double-close empty-table (BUG-LZ-5)."""

    def test_close_is_idempotent(self):
        """Calling close() twice must not wipe the functions table."""
        import sqlite3
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "code2database.db")
            sg = StreamingGraph(db_path)
            sg.add_node("f1", name="f1", source_file="a.c", line=1, domain="root")
            sg.add_node("f2", name="f2", source_file="a.c", line=2, domain="root")
            sg.add_edge("f1", "f2", call_order=0, edge_confidence="EXTRACTED")
            sg.close()

            conn = sqlite3.connect(db_path)
            count_before = conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]
            conn.close()
            self.assertGreater(count_before, 0)

            sg.close()

            conn = sqlite3.connect(db_path)
            count_after = conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]
            conn.close()
            self.assertEqual(count_before, count_after,
                             "second close() wiped the functions table")


if __name__ == "__main__":
    unittest.main()
