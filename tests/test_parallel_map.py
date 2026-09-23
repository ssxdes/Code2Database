"""Tests for _builder.build.parallel helpers (map_nodes process/thread paths).

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

import networkx as nx

from _builder.build.parallel import (
    map_nodes, resolve_jobs, merge_node_attributes,
)


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


class TestMergeNodeAttributes(unittest.TestCase):
    """Contract of the merge helper: work_fn returns a dict of attrs to
    set; falsy values (empty containers, 0, False, None) count as
    "nothing to set" and are skipped — callers that need to write 0 or
    False must do so directly on the node. The merge itself runs on the
    caller thread (no concurrent graph mutation)."""

    def _run(self, results_by_nid):
        G = nx.DiGraph()
        items = []
        for nid in results_by_nid:
            G.add_node(nid, name=nid)
            items.append((nid, {}))
        calls = {nid: i for i, nid in enumerate(results_by_nid)}

        def work(nid, _nd):
            return results_by_nid[nid]

        count = merge_node_attributes(G, items, work, jobs=1)
        return G, count, calls

    def test_truthy_values_merged(self):
        G, count, _ = self._run({
            "a": {"labels": ["x"], "score": 0.5},
            "b": None,
            "c": {"labels": []},
        })
        self.assertEqual(G.nodes["a"]["labels"], ["x"])
        self.assertEqual(G.nodes["a"]["score"], 0.5)
        # b produced None and c produced only falsy values → untouched
        self.assertNotIn("labels", G.nodes["b"])
        self.assertNotIn("labels", G.nodes["c"])
        self.assertEqual(count, 1)

    def test_falsy_values_skipped_by_contract(self):
        G, count, _ = self._run({
            "a": {"zero": 0, "flag": False, "empty": [], "none": None,
                  "text": ""},
        })
        for key in ("zero", "flag", "empty", "none", "text"):
            self.assertNotIn(key, G.nodes["a"])
        self.assertEqual(count, 0)

    def test_mixed_result_touches_node_once(self):
        G, count, _ = self._run({
            "a": {"zero": 0, "labels": ["keep"]},
        })
        self.assertEqual(G.nodes["a"]["labels"], ["keep"])
        self.assertNotIn("zero", G.nodes["a"])
        self.assertEqual(count, 1)


class TestWorkerFailureSemantics(unittest.TestCase):
    """A failing worker leaves a None slot at its index — parallel maps
    never raise through; callers detect per-item loss by checking for
    None."""

    def test_thread_worker_exception_gives_none_slot(self):
        def worker(nid, nd):
            if nd["fail"]:
                raise RuntimeError("boom")
            return nid

        items = [(f"n{i}", {"fail": i == 2}) for i in range(5)]
        results = map_nodes(items, worker, jobs=2)
        self.assertEqual(results[2], None)
        self.assertEqual(results[0], "n0")
        self.assertEqual(results[4], "n4")

    def test_merge_treats_none_result_as_skip(self):
        G = nx.DiGraph()
        G.add_node("a")
        G.add_node("b")

        def worker(nid, _nd):
            if nid == "a":
                raise RuntimeError("boom")
            return {"labels": ["ok"]}

        count = merge_node_attributes(G, [("a", {}), ("b", {})], worker,
                                      jobs=1)
        self.assertEqual(count, 1)
        self.assertNotIn("labels", G.nodes["a"])
        self.assertEqual(G.nodes["b"]["labels"], ["ok"])


class TestResolveJobs(unittest.TestCase):

    def test_sequential_stays_one(self):
        self.assertEqual(resolve_jobs(1), 1)

    def test_non_positive_treated_as_auto(self):
        # Code behavior: jobs <= 0 (incl. negatives) is auto, i.e. >= 2.
        self.assertGreaterEqual(resolve_jobs(0), 2)
        self.assertGreaterEqual(resolve_jobs(-5), 2)


if __name__ == "__main__":
    unittest.main()
