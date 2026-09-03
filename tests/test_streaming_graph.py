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


class TestDeferredReloadCorrectness(unittest.TestCase):
    """set_deferred(False) reload: attribute merge + failure safety.

    - The same (u,v) flushed more than once (later phases re-add it
      with extra attrs) must MERGE on reload, not last-row-wins.
    - A failed reload must raise with deferred mode STILL ACTIVE, so a
      subsequent close() takes the deferred path (no DELETE FROM
      edges) instead of wiping every streamed edge.
    """

    def _make(self):
        d = tempfile.mkdtemp()
        sg = StreamingGraph(os.path.join(d, "code2database.db"))
        sg.set_deferred(True)
        sg.add_node("f1", name="f1")
        sg.add_node("f2", name="f2")
        return d, sg

    def test_cross_flush_attributes_merge_on_reload(self):
        d, sg = self._make()
        sg.add_edge("f1", "f2", confidence="EXTRACTED")
        sg._flush_edges()
        sg.add_edge("f1", "f2", evidence="call site a.c:10")
        sg._flush_edges()
        sg.set_deferred(False)
        attrs = sg._edge_data[("f1", "f2")]
        self.assertEqual(attrs.get("confidence"), "EXTRACTED")
        self.assertEqual(attrs.get("evidence"), "call site a.c:10")
        sg.close()

    def test_failed_reload_keeps_deferred_and_preserves_edges(self):
        import sqlite3
        d, sg = self._make()
        sg.add_edge("f1", "f2", confidence="EXTRACTED")
        sg._flush_edges()
        sg._store._conn.commit()
        # Hide the edges table: reload SELECT fails, data survives
        sg._store._conn.execute("ALTER TABLE edges RENAME TO edges_backup")
        with self.assertRaises(RuntimeError):
            sg.set_deferred(False)
        self.assertTrue(sg._deferred,
                        "deferred flag must stay True after failed reload")
        # close() on the deferred path must NOT delete streamed edges
        sg.close()
        conn = sqlite3.connect(os.path.join(d, "code2database.db"))
        conn.execute("ALTER TABLE edges_backup RENAME TO edges")
        rows = conn.execute(
            "SELECT invoker_id, invoked_id, confidence FROM edges"
        ).fetchall()
        conn.close()
        self.assertEqual(rows, [("f1", "f2", "EXTRACTED")])


class TestDeferredRemoveNode(unittest.TestCase):
    """remove_node during the deferred window must clean the DB too.

    In-memory bookkeeping alone left (a) already-flushed edge rows in
    the edges table (close()'s deferred path never rewrites it) and
    (b) residual _edge_batch entries that close() would re-introduce.
    """

    def test_remove_node_in_deferred_mode_cleans_db_and_batch(self):
        import sqlite3
        d = tempfile.mkdtemp()
        sg = StreamingGraph(os.path.join(d, "code2database.db"))
        sg.set_deferred(True)
        sg.add_node("f1", name="f1")
        sg.add_node("f2", name="f2")
        sg.add_node("f3", name="f3")
        sg.add_edge("f1", "f2", confidence="EXTRACTED")
        sg._flush_edges()  # f1->f2 now lives in SQLite
        sg.add_edge("f1", "f3", confidence="EXTRACTED")  # batch only
        sg.add_edge("f3", "f2", confidence="EXTRACTED")  # batch only
        sg.remove_node("f1")
        sg.close()
        conn = sqlite3.connect(os.path.join(d, "code2database.db"))
        try:
            edges = conn.execute(
                "SELECT invoker_id, invoked_id FROM edges "
                "ORDER BY 1, 2").fetchall()
            funcs = conn.execute(
                "SELECT id FROM functions ORDER BY 1").fetchall()
        finally:
            conn.close()
        self.assertEqual(edges, [("f3", "f2")])
        self.assertEqual(funcs, [("f2",), ("f3",)])


class TestCloseCommitFailure(unittest.TestCase):
    """A failed pre-close commit must surface, not turn into a
    confusing 'cannot start a transaction within a transaction' from
    the BEGIN that follows."""

    def test_commit_failure_raises_original_error(self):
        import sqlite3
        d = tempfile.mkdtemp()
        sg = StreamingGraph(os.path.join(d, "code2database.db"))
        sg.add_node("f1", name="f1")

        class BadCommitConn:
            def __init__(self, real):
                self._real = real

            def commit(self):
                raise sqlite3.OperationalError(
                    "database or disk is full (simulated)")

            def __getattr__(self, name):
                return getattr(self._real, name)

        sg._store._conn = BadCommitConn(sg._store._conn)
        with self.assertRaises(sqlite3.OperationalError) as cm:
            sg.close()
        self.assertIn("disk is full", str(cm.exception))
        self.assertNotIn("within a transaction", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
