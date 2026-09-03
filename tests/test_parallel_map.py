"""Tests for _builder.parallel helpers (map_nodes process/thread paths).

The map_nodes process path uses the spawn start method: callers
typically hold the full graph/extraction payload in memory, and fork
would copy-on-write map all of it into every worker (OOM on large
builds). Two contracts are pinned here:

1. process mode with a top-level work_fn runs in spawned children and
   returns in-order results;
2. a closure work_fn (not picklable) falls back to the ThreadPool path
   instead of crashing — including under the n>1000 auto-promotion.
"""
import multiprocessing as _mp
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.parallel import map_nodes, resolve_jobs


def _top_level_worker(nid, nd):
    """Module-level (picklable) worker for the spawn pool."""
    return {"nid": nid, "v": nd["x"] * 2, "pid": os.getpid()}


class TestMapNodesProcessMode(unittest.TestCase):

    def test_process_mode_uses_spawn_and_returns_in_order(self):
        items = [(f"n{i}", {"x": i}) for i in range(150)]
        seen = []
        _orig = _mp.get_context

        def _spy(name=None):
            seen.append(name)
            return _orig(name)
        _mp.get_context = _spy
        try:
            results = map_nodes(items, _top_level_worker, jobs=4,
                                parallel_mode="process",
                                explicit_parallel_mode=True)
        finally:
            _mp.get_context = _orig
        self.assertIn("spawn", seen)
        self.assertNotIn("fork", seen)
        self.assertEqual(len(results), 150)
        for i, r in enumerate(results):
            self.assertEqual(r["nid"], f"n{i}")
            self.assertEqual(r["v"], i * 2)
        pids = {r["pid"] for r in results}
        self.assertTrue(pids)
        self.assertNotIn(os.getpid(), pids,
                         "process-mode work ran in the parent, not children")

    def test_closure_falls_back_to_threads(self):
        factor = 3

        def closure_worker(nid, nd):
            return nd["x"] * factor

        # n > 1000 triggers auto-promotion to process mode; the closure
        # is unpicklable, so map_nodes must fall back to threads and
        # still produce correct in-order results.
        items = [(f"n{i}", {"x": i}) for i in range(1200)]
        results = map_nodes(items, closure_worker, jobs=4,
                            parallel_mode="thread",
                            explicit_parallel_mode=False)
        self.assertEqual(len(results), 1200)
        for i, r in enumerate(results):
            self.assertEqual(r, i * 3)


class TestResolveJobs(unittest.TestCase):

    def test_sequential_stays_one(self):
        self.assertEqual(resolve_jobs(1), 1)

    def test_non_positive_treated_as_auto(self):
        # Code behavior: jobs <= 0 (incl. negatives) is auto, i.e. >= 2.
        self.assertGreaterEqual(resolve_jobs(0), 2)
        self.assertGreaterEqual(resolve_jobs(-5), 2)


if __name__ == "__main__":
    unittest.main()
