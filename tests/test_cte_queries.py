"""Tests for SQL CTE recursive path queries (D7+D8)."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.sqlite_store import SQLiteStore
from _builder.query_router import (
    route_call_chain, route_blast_radius, route_trace_chain,
    route_path_between, sqlite_available,
)


def _build_test_db(db_path: str):
    """Build a small test database with functions and edges."""
    store = SQLiteStore(db_path)
    store.connect()
    # Add functions
    funcs = [
        {"id": "main", "name": "main", "domain": "root",
         "source_file": "main.c", "line": 1, "signature": "int main()",
         "labels": ["API_entry"]},
        {"id": "foo", "name": "foo", "domain": "root",
         "source_file": "foo.c", "line": 5, "signature": "void foo()",
         "labels": []},
        {"id": "bar", "name": "bar", "domain": "root",
         "source_file": "bar.c", "line": 10, "signature": "void bar()",
         "labels": []},
        {"id": "baz", "name": "baz", "domain": "root",
         "source_file": "baz.c", "line": 15, "signature": "void baz()",
         "labels": []},
        {"id": "leaf", "name": "leaf", "domain": "root",
         "source_file": "leaf.c", "line": 20, "signature": "void leaf()",
         "labels": ["out_end"]},
    ]
    store.store_functions(funcs)
    # Add edges: main -> foo -> bar -> baz -> leaf
    edges = [
        {"caller": "main", "callee": "foo", "relation": "INVOKES",
         "call_order": 0, "confidence": "EXTRACTED"},
        {"caller": "foo", "callee": "bar", "relation": "INVOKES",
         "call_order": 0, "confidence": "EXTRACTED"},
        {"caller": "bar", "callee": "baz", "relation": "INVOKES",
         "call_order": 0, "confidence": "EXTRACTED"},
        {"caller": "baz", "callee": "leaf", "relation": "INVOKES",
         "call_order": 0, "confidence": "EXTRACTED"},
    ]
    store.store_edges(edges)
    store.close()


class TestCallChainCTE(unittest.TestCase):
    """Test the recursive CTE for call chains."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "code2database.db")
        _build_test_db(self.db_path)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_downward_call_chain(self):
        """route_call_chain with direction='down' returns callees."""
        result = route_call_chain(self.tmpdir, "main", max_depth=5,
                                  direction="down")
        self.assertIsNotNone(result)
        # main -> foo -> bar -> baz -> leaf (depths 0..4)
        ids = [r["function_id"] for r in result]
        self.assertIn("main", ids)
        self.assertIn("foo", ids)
        self.assertIn("bar", ids)
        self.assertIn("baz", ids)
        self.assertIn("leaf", ids)

    def test_upward_call_chain(self):
        """route_call_chain with direction='up' returns callers."""
        result = route_call_chain(self.tmpdir, "leaf", max_depth=5,
                                  direction="up")
        self.assertIsNotNone(result)
        # leaf <- baz <- bar <- foo <- main
        ids = [r["function_id"] for r in result]
        self.assertIn("leaf", ids)
        self.assertIn("baz", ids)
        self.assertIn("bar", ids)
        self.assertIn("foo", ids)
        self.assertIn("main", ids)

    def test_max_depth_limits_results(self):
        """route_call_chain respects max_depth."""
        result = route_call_chain(self.tmpdir, "main", max_depth=2,
                                  direction="down")
        self.assertIsNotNone(result)
        # Should have main (depth 0), foo (1), bar (2) — but not baz or leaf
        ids = {r["function_id"] for r in result}
        self.assertIn("main", ids)
        self.assertIn("foo", ids)
        self.assertIn("bar", ids)
        self.assertNotIn("baz", ids)
        self.assertNotIn("leaf", ids)

    def test_returns_none_when_sqlite_unavailable(self):
        """route_call_chain returns None when no SQLite db."""
        empty_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(empty_dir,
                                                            ignore_errors=True))
        result = route_call_chain(empty_dir, "main", max_depth=5)
        self.assertIsNone(result)


class TestBlastRadiusAndTraceChain(unittest.TestCase):
    """Test the convenience wrappers."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _build_test_db(os.path.join(self.tmpdir, "code2database.db"))
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_blast_radius_equals_downward_chain(self):
        """route_blast_radius returns the downward call chain."""
        result = route_blast_radius(self.tmpdir, "main", max_depth=5)
        self.assertIsNotNone(result)
        ids = {r["function_id"] for r in result}
        self.assertEqual(ids, {"main", "foo", "bar", "baz", "leaf"})

    def test_trace_chain_equals_upward_chain(self):
        """route_trace_chain returns the upward caller chain."""
        result = route_trace_chain(self.tmpdir, "leaf", max_depth=5)
        self.assertIsNotNone(result)
        ids = {r["function_id"] for r in result}
        self.assertEqual(ids, {"main", "foo", "bar", "baz", "leaf"})


class TestPathBetween(unittest.TestCase):
    """Test the path-between-two-functions CTE."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _build_test_db(os.path.join(self.tmpdir, "code2database.db"))
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_finds_direct_path(self):
        """route_path_between finds a path from main to leaf."""
        result = route_path_between(self.tmpdir, "main", "leaf", max_depth=10)
        self.assertIsNotNone(result)
        self.assertGreater(len(result), 0)
        # First path should be main -> foo -> bar -> baz -> leaf
        first_path = result[0]["path"]
        self.assertEqual(first_path[0], "main")
        self.assertEqual(first_path[-1], "leaf")
        self.assertEqual(first_path, ["main", "foo", "bar", "baz", "leaf"])

    def test_no_path_returns_empty(self):
        """route_path_between returns empty list when no path exists."""
        # Build a db where 'a' and 'b' are disconnected
        db_path = os.path.join(self.tmpdir, "code2database.db")
        # Add disconnected function
        store = SQLiteStore(db_path)
        store.connect()
        store.store_functions([{
            "id": "disconnected", "name": "disconnected",
            "domain": "root", "source_file": "d.c", "line": 1,
            "signature": "void d()", "labels": [],
        }])
        store.close()
        result = route_path_between(self.tmpdir, "main", "disconnected",
                                    max_depth=10)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 0)

    def test_returns_none_when_sqlite_unavailable(self):
        """route_path_between returns None when no SQLite db."""
        empty_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(empty_dir,
                                                            ignore_errors=True))
        result = route_path_between(empty_dir, "a", "b")
        self.assertIsNone(result)


class TestSQLiteAvailable(unittest.TestCase):
    """Test the sqlite_available helper."""

    def test_returns_true_when_db_exists(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmpdir,
                                                            ignore_errors=True))
        _build_test_db(os.path.join(tmpdir, "code2database.db"))
        self.assertTrue(sqlite_available(tmpdir))

    def test_returns_false_when_no_db(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmpdir,
                                                            ignore_errors=True))
        self.assertFalse(sqlite_available(tmpdir))


if __name__ == "__main__":
    unittest.main()
