"""Regression tests for the graph_build ProcessPoolExecutor paths.

Both pools were converted from fork to spawn: they run while the parent
process holds large in-memory state (the extraction payload pre-graph;
the full graph post-graph). fork() mapped all of it copy-on-write into
every worker — page-table copies plus COW faults on first write,
multiplied by N workers, OOM'd large builds (2026-09-02: 86GB parent,
48 workers, 251GB box). Spawn children start clean; worker context is
passed explicitly (per-item args, or a pool initializer for the
pre-strip globals).
"""
import multiprocessing as _mp
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import networkx as nx

from _builder import graph_build as gb


class TestStateAccessProcessPool(unittest.TestCase):
    """_extract_state_access_all process path uses spawn and computes
    correct results from explicitly-passed args."""

    def test_process_mode_uses_spawn_and_merges_results(self):
        G = nx.DiGraph()
        extraction = {
            "globals": {"global_vars": [{"name": "g_state"}]},
            "field_assignments": [],
        }
        # >100 candidates to trigger the process path
        for i in range(120):
            G.add_node(f"f{i}", name=f"f{i}",
                       body_text="g_state = 1; local = 2;",
                       local_vars=[{"name": "local"}], params=[])
        seen = []
        _orig = _mp.get_context

        def _spy(name=None):
            seen.append(name)
            return _orig(name)
        _mp.get_context = _spy
        try:
            gb._extract_state_access_all(G, extraction, jobs=4,
                                         parallel_mode="process",
                                         explicit_parallel_mode=True)
        finally:
            _mp.get_context = _orig
        self.assertIn("spawn", seen)
        self.assertNotIn("fork", seen)
        touched = [n for n, d in G.nodes(data=True)
                   if d.get("globals_written")]
        self.assertEqual(len(touched), 120)
        for n in touched:
            self.assertEqual(
                [x["name"] for x in G.nodes[n]["globals_written"]],
                ["g_state"])


class TestPreStripPool(unittest.TestCase):
    """The pre-strip pool passes worker context via the initializer
    (_pre_strip_worker_init) and produces results identical to a
    direct in-process call."""

    def test_initializer_context_and_results_match_direct_call(self):
        globals_data = {"global_vars": [{"name": "g_cfg"}]}
        field_assignments = []
        cached = {
            "var_names": {"g_cfg": {"name": "g_cfg"}},
            "var_names_keys": {"g_cfg"},
            "assign_ops_re": re.compile(
                r'\b(g_cfg)\s*(\+|-|\*|\/|\||\&|\^|\%|<<|>>)?=\s*[^=]'),
        }
        funcs = [{"body_text": "g_cfg = 1;", "local_vars": [],
                  "params": [], "name": f"fn{i}"} for i in range(8)]
        items = [(i, f) for i, f in enumerate(funcs)]

        expected = []
        for i, f in items:
            ai = gb._extract_state_access(
                f["body_text"], f["local_vars"], f["params"],
                globals_data, field_assignments, f["name"],
                _cached_globals=cached)
            expected.append((i, ai))

        ctx = _mp.get_context("spawn")
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(
                max_workers=3, mp_context=ctx,
                initializer=gb._pre_strip_worker_init,
                initargs=(globals_data, field_assignments, cached)) as pool:
            got = list(pool.map(gb._proc_pre_strip_state_access, items))

        self.assertEqual(len(got), len(items))
        for (i_got, ai_got), (i_exp, ai_exp) in zip(got, expected):
            self.assertEqual(i_got, i_exp)
            for k in ("globals_read", "globals_written",
                      "fields_read", "fields_written"):
                self.assertEqual((ai_got or {}).get(k, []),
                                 (ai_exp or {}).get(k, []), k)
        # Every function body writes g_cfg.
        written = [i for i, ai in got if (ai or {}).get("globals_written")]
        self.assertEqual(len(written), 8)
        # The parent's module globals were never populated — no
        # fork-COW reliance.
        self.assertIsNone(gb._PRE_STRIP_GLOBALS)
        self.assertIsNone(gb._PRE_STRIP_FIELD_ASSIGNMENTS)
        self.assertIsNone(gb._PRE_STRIP_CACHED)


if __name__ == "__main__":
    unittest.main()
