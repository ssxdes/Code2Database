"""Query/graph cache correctness: WAL-aware invalidation, bounded growth.

Covers:
- cache entries must invalidate when the WAL sidecar changes (WAL-mode
  writes do not touch the main db's mtime until checkpoint)
- scalar results return as-is (no deepcopy) while structured results
  stay copy-on-read
- the per-graph caches are bounded LRUs, not unbounded dicts
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.query.query_cache import _GraphCache


class TestWalAwareInvalidation(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="c2d_wal_")
        self.graph_dir = os.path.join(self.tmpdir, "graph")
        os.makedirs(self.graph_dir)
        # Main db only; WAL appears later (daemon writes).
        with open(os.path.join(self.graph_dir, "code2database.db"), "w") as f:
            f.write("db")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_wal_write_invalidates_entry(self):
        cache = _GraphCache(self.graph_dir)
        cache.put("k", "v", frozenset())
        self.assertEqual(cache.get("k"), "v")
        # Simulate a WAL write some time later.
        time.sleep(0.02)
        wal = os.path.join(self.graph_dir, "code2database.db-wal")
        with open(wal, "w") as f:
            f.write("wal-data")
        self.assertIsNone(
            cache.get("k"),
            "a WAL-side write must invalidate cached results — the main "
            "db mtime does not move until checkpoint")

    def test_no_wal_change_keeps_entry(self):
        cache = _GraphCache(self.graph_dir)
        cache.put("k", "v", frozenset())
        self.assertEqual(cache.get("k"), "v")
        self.assertEqual(cache.get("k"), "v")


class TestScalarFastPath(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="c2d_scalar_")
        self.cache = _GraphCache(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_scalar_returned_without_copy(self):
        marker = "a-long-scalar-result"
        self.cache.put("s", marker, frozenset())
        self.assertIs(self.cache.get("s"), marker,
                      "immutable scalars must not be deepcopied per hit")

    def test_dict_still_copied(self):
        value = {"nodes": [1, 2, 3]}
        self.cache.put("d", value, frozenset())
        got = self.cache.get("d")
        self.assertEqual(got, value)
        self.assertIsNot(got, value,
                         "structured results must stay copy-on-read")

    def test_none_returned(self):
        self.cache.put("n", None, frozenset())
        # None is a valid cached value; get() returns it (callers treat
        # None as "no entry" — acceptable: no query caches None today,
        # but the fast path must not crash on it).
        self.cache._entries["n"] = (time.time(), None,
                                    self.cache._graph_mtime(),
                                    self.cache._epoch)
        self.assertIsNone(self.cache.get("n"))


class TestBoundedGraphCaches(unittest.TestCase):
    def test_query_cache_evicts_oldest_graph(self):
        import _builder.query.query_cache as qc
        tmpdir = tempfile.mkdtemp(prefix="c2d_bound_")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        dirs = [os.path.join(tmpdir, "g%d" % i) for i in range(4)]
        for d in dirs:
            os.makedirs(d)
        _orig_cap = qc._MAX_GRAPH_CACHES
        _orig_caches = qc._CACHES
        qc._MAX_GRAPH_CACHES = 2
        qc._CACHES = type(_orig_caches)()
        try:
            qc._get_cache(dirs[0])
            qc._get_cache(dirs[1])
            qc._get_cache(dirs[2])
            self.assertEqual(len(qc._CACHES), 2)
            self.assertNotIn(dirs[0], qc._CACHES,
                             "the least-recently-used graph must go")
            # Touch dirs[1] then add dirs[3]: dirs[2] becomes the victim.
            qc._get_cache(dirs[1])
            qc._get_cache(dirs[3])
            self.assertEqual(len(qc._CACHES), 2)
            self.assertNotIn(dirs[2], qc._CACHES)
            self.assertIn(dirs[1], qc._CACHES)
        finally:
            qc._MAX_GRAPH_CACHES = _orig_cap
            qc._CACHES = _orig_caches

    def test_mcp_graph_cache_evicts_oldest_graph(self):
        import _builder.mcp.mcp_cache as mc
        tmpdir = tempfile.mkdtemp(prefix="c2d_mcpbound_")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        dirs = [os.path.join(tmpdir, "g%d" % i) for i in range(3)]
        for d in dirs:
            os.makedirs(d)
        _orig_cap = mc._MAX_CACHED_GRAPHS
        _orig_cache = mc._GRAPH_CACHE
        mc._MAX_CACHED_GRAPHS = 2
        mc._GRAPH_CACHE = type(_orig_cache)()
        closed = []

        class _FakeGraph:
            def __init__(self, name):
                self.name = name

            def close(self):
                closed.append(self.name)

        _orig_loader = None
        try:
            # Bypass the real loader: patch the graph_build import target
            # by pre-filling via a loader stub. _get_graph imports
            # _load_full_graph lazily inside the function, so patch the
            # module it imports from.
            import _builder.graph.graph_build as gb
            _orig_loader = gb._load_full_graph

            def _fake_loader(graph_dir):
                return _FakeGraph(graph_dir)

            gb._load_full_graph = _fake_loader
            mc._get_graph(dirs[0])
            mc._get_graph(dirs[1])
            mc._get_graph(dirs[2])
            self.assertEqual(len(mc._GRAPH_CACHE), 2)
            self.assertEqual(closed, [dirs[0]],
                             "evicted graphs must be closed")
        finally:
            gb._load_full_graph = _orig_loader
            mc._MAX_CACHED_GRAPHS = _orig_cap
            mc._GRAPH_CACHE = _orig_cache


if __name__ == "__main__":
    unittest.main()
