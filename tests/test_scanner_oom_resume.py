"""Scanner degradation-callback scope and parallel-path tracking.

Covers:
- the pre-OOM callback must be registered where the scan accumulators
  are in scope (the old cmd_scan closure hit NameError on every call
  and memory_guard swallowed it, so the emergency body_text drop never
  ran)
- memory_guard must surface callback failures at warning level
- parallel scans (thread and process pools) must record completed
  files so interrupted scans can resume instead of restarting
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.memory.memory_guard import MemoryGuard


def _make_project(tmpdir, n_files=3):
    src = os.path.join(tmpdir, "src")
    os.makedirs(src, exist_ok=True)
    for i in range(n_files):
        with open(os.path.join(src, "f%d.c" % i), "w") as f:
            f.write("int g_var;\n"
                    "int func_%d(int x) { g_var = x; return g_var; }\n" % i)
    return src


class TestPreOomCallbackScope(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="c2d_preeoom_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_callback_reaches_accumulators_and_drops_body_text(self):
        from code2database_scanner import scan_directory
        src = _make_project(self.tmpdir)
        guard = MemoryGuard(warn_threshold=0.95, crit_threshold=0.99)
        result = scan_directory(source_root=src, workers=1, quiet=True,
                                memory_guard=guard)
        self.assertGreaterEqual(len(result.get("functions", [])), 1)
        cbs = getattr(guard, "_degradation_callbacks", [])
        pre_oom = [c for c in cbs if getattr(c, "_c2d_pre_oom", False)]
        self.assertEqual(len(pre_oom), 1,
                         "scan_directory must register exactly one "
                         "pre-OOM callback on the guard it was given")
        # Invoking it must not raise (the old closure raised NameError)
        # and must actually drop body_text from the scanned functions —
        # the returned list is the same object the closure holds.
        pre_oom[0]({"usage_percent": 0.95})
        for fn in result["functions"]:
            self.assertNotIn("body_text", fn,
                             "emergency drop must clear body_text")
            self.assertIn("globals_written", fn,
                          "state_access must be extracted before dropping")

    def test_repeated_scans_replace_stale_callback(self):
        from code2database_scanner import scan_directory
        src = _make_project(self.tmpdir, n_files=1)
        guard = MemoryGuard(warn_threshold=0.95, crit_threshold=0.99)
        scan_directory(source_root=src, workers=1, quiet=True,
                       memory_guard=guard)
        scan_directory(source_root=src, workers=1, quiet=True,
                       memory_guard=guard)
        cbs = getattr(guard, "_degradation_callbacks", [])
        pre_oom = [c for c in cbs if getattr(c, "_c2d_pre_oom", False)]
        self.assertEqual(len(pre_oom), 1,
                         "in-process rescans must not pile up callbacks")

    def test_memory_guard_logs_callback_failures_at_warning(self):
        import logging
        import _builder.memory.memory_guard as _mg_mod
        guard = MemoryGuard(warn_threshold=0.5, crit_threshold=0.9)
        guard._degradation_callbacks = [lambda info: 1 / 0]
        # Force the critical branch regardless of real system memory.
        guard.get_memory_info = lambda: {
            "total_mb": 100, "used_mb": 99, "usage_percent": 0.99,
            "rss_mb": 99}
        guard._effective_crit_threshold_mb = lambda: None
        guard._effective_warn_threshold_mb = lambda: None
        with self.assertLogs(_mg_mod.__name__, level="WARNING") as logged:
            try:
                guard.check_and_adapt()
            except ZeroDivisionError:
                self.fail("callback exceptions must be contained")
        messages = [r.getMessage() for r in logged.records]
        self.assertTrue(
            any("degradation callback raised" in m for m in messages),
            "callback failure must be visible at warning level, got %s"
            % messages)


class TestParallelCheckpointResume(unittest.TestCase):
    """Parallel scans must record completed files for resume."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="c2d_parckpt_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_interrupted_parallel_scan_saves_resume_point(self):
        from code2database_scanner import scan_directory
        src = _make_project(self.tmpdir, n_files=10)
        out_json = os.path.join(self.tmpdir, "out", "extraction.json")
        os.makedirs(os.path.dirname(out_json), exist_ok=True)
        # A microscopic memory limit trips the parallel-loop stop check
        # deterministically right after the first completed batch.
        guard = MemoryGuard(warn_threshold=0.95, crit_threshold=0.99)
        result = scan_directory(source_root=src, workers=2, quiet=True,
                                parallel_mode="thread",
                                memory_guard=guard, memory_limit_gb=0.001,
                                streaming_output=out_json)
        self.assertTrue(result.get("_stopped_early"),
                        "the tiny memory limit must stop the scan early")
        checkpoint = os.path.join(os.path.dirname(out_json),
                                  "_scan_checkpoint.json")
        self.assertTrue(os.path.exists(checkpoint),
                        "an interrupted parallel scan must leave a "
                        "checkpoint")
        with open(checkpoint, encoding="utf-8") as f:
            cp = json.load(f)
        completed = cp.get("completed_files") or []
        self.assertGreaterEqual(len(completed), 1,
                                "the checkpoint must list the files already "
                                "scanned, not be empty")
        # A resumed run skips the recorded files and finishes cleanly,
        # removing the checkpoint.
        result2 = scan_directory(source_root=src, workers=2, quiet=True,
                                 parallel_mode="thread",
                                 streaming_output=out_json)
        self.assertFalse(result2.get("_stopped_early"))
        self.assertFalse(os.path.exists(checkpoint),
                         "a completed resume must clean up the checkpoint")


class TestSplitPostPassParity(unittest.TestCase):
    """Split-mode scans must run the same post-scan passes as normal ones.

    The split path returned early and skipped id disambiguation and
    cross-file callback detection, so >2000-file (or --split-output)
    scans emitted duplicate ids and no cross-file CALLBACK_ARG edges.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="c2d_splitpost_")
        self.src = os.path.join(self.tmpdir, "src")
        os.makedirs(self.src)
        with open(os.path.join(self.src, "a.c"), "w") as f:
            f.write("int common_main(void) { return 1; }\n"
                    "void my_handler(void *arg) { (void)arg; }\n")
        with open(os.path.join(self.src, "b.c"), "w") as f:
            f.write("int common_main(void) { return 2; }\n"
                    "void start_thread(void) {\n"
                    "    register_ops(my_handler);\n"
                    "}\n")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @staticmethod
    def _split_contents(split_dir):
        fns, edges = [], []
        fn_dir = os.path.join(split_dir, "functions")
        for name in sorted(os.listdir(fn_dir)):
            if name.endswith(".json"):
                with open(os.path.join(fn_dir, name), encoding="utf-8") as f:
                    fns.extend(json.load(f))
        ed_dir = os.path.join(split_dir, "edges")
        for name in sorted(os.listdir(ed_dir)):
            if name.endswith(".json"):
                with open(os.path.join(ed_dir, name), encoding="utf-8") as f:
                    edges.extend(json.load(f))
        return fns, edges

    def test_split_output_has_unique_ids_and_callback_edges(self):
        from code2database_scanner import scan_directory
        result = scan_directory(source_root=self.src, workers=1,
                                quiet=True, split_output=True)
        split_dir = result["_split_dir"]
        fns, edges = self._split_contents(split_dir)
        self.assertEqual(len(fns), 4)
        ids = [fn["id"] for fn in fns]
        self.assertEqual(len(ids), len(set(ids)),
                         "duplicate ids must be disambiguated in split mode")
        # The fn-ptr assignment in b.c referencing a.c's my_handler must
        # produce a cross-file callback edge (none existed before).
        handlers = [e for e in edges
                    if "my_handler" in str(e.get("target", e.get("callee", "")))]
        self.assertGreaterEqual(
            len(handlers), 1,
            "cross-file callback detection must run in split mode")

    def test_split_and_normal_outputs_agree(self):
        from code2database_scanner import scan_directory
        normal = scan_directory(source_root=self.src, workers=1, quiet=True)
        split = scan_directory(source_root=self.src, workers=1, quiet=True,
                               split_output=True)
        fns, edges = self._split_contents(split["_split_dir"])
        self.assertEqual(
            {fn["id"] for fn in normal["functions"]},
            {fn["id"] for fn in fns},
            "split and normal scans must agree on function ids")

    def test_rewrite_keeps_body_text(self):
        from code2database_scanner import scan_directory
        result = scan_directory(source_root=self.src, workers=1,
                                quiet=True, split_output=True)
        fns, _ = self._split_contents(result["_split_dir"])
        with_text = [fn for fn in fns if fn.get("body_text")]
        self.assertEqual(
            len(with_text), len(fns),
            "the duplicate-id rewrite must persist full records — only "
            "the in-memory post-pass copies are slim")


class TestScannerBaseCorrections(unittest.TestCase):
    """_scanner/base.py cross-language local-var and file-id semantics."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="c2d_basecorr_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_c_late_assignment_recorded_as_local_var(self):
        """`int x; ... x = other();` must yield a local var for x.

        The assignment branch only matched Python's 'assignment' node;
        C/C++ use 'assignment_expression' and Go 'assignment_statement',
        so non-declaring assignments were invisible on those languages.
        """
        from code2database_scanner import scan_directory
        src = os.path.join(self.tmpdir, "src")
        os.makedirs(src)
        with open(os.path.join(src, "late.c"), "w") as f:
            f.write("int other(void);\n"
                    "int g_state;\n"
                    "void f(void) {\n"
                    "    int x;\n"
                    "    x = other();\n"
                    "    g_state = x;\n"
                    "}\n")
        result = scan_directory(source_root=src, workers=1, quiet=True)
        self.assertEqual(len(result["functions"]), 1)
        fn = result["functions"][0]
        names = [lv["name"] for lv in fn.get("local_vars", [])]
        self.assertIn("x", names,
                      "C assignment_expression must feed local_vars")
        self.assertNotIn(
            "g_state", names,
            "an assignment to a file-scope variable must stay a global "
            "write, not masquerade as a local")
        self.assertIn("g_state",
                      [gv["name"] for gv in result["globals"]["global_vars"]])

    def test_file_id_hashed_from_relative_path(self):
        """Scan-side file ids must match builder-side ids (relative hash)."""
        from _scanner.unified_id import unified_file_id
        from _builder.cgdb.cgdb_ingest import file_id_for
        from code2database_scanner import scan_directory
        src = os.path.join(self.tmpdir, "src")
        os.makedirs(src)
        with open(os.path.join(src, "one.c"), "w") as f:
            f.write("int one(void) { return 1; }\n")
        result = scan_directory(source_root=src, workers=1, quiet=True)
        # The extraction's file records carry ids computed by base.py;
        # the builder recomputes from the relative path.
        rel = os.path.join("src", "one.c")
        self.assertEqual(unified_file_id(rel), file_id_for(rel))


class TestCmdScanTraversalCount(unittest.TestCase):
    """cmd_scan must traverse the tree once before scanning, not twice."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="c2d_walks_")
        self.src = os.path.join(self.tmpdir, "src")
        os.makedirs(self.src)
        with open(os.path.join(self.src, "a.c"), "w") as f:
            f.write("int a(void) { return 1; }\n")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_single_pre_scan_walk(self):
        import argparse
        import code2database_scanner as _cs
        from code2database_scanner import cmd_scan
        out_json = os.path.join(self.tmpdir, "out", "extraction.json")
        args = argparse.Namespace(
            source=self.src, output=out_json, lang="auto", workers=1,
            macros="", macros_from="", api_prefixes="",
            compile_commands="", clang_args="", files=[],
        )
        _real_walk = _cs.os.walk
        walks = []

        def _counting_walk(top, *a, **kw):
            walks.append(top)
            return _real_walk(top, *a, **kw)

        _cs.os.walk = _counting_walk
        try:
            cmd_scan(args)
        finally:
            _cs.os.walk = _real_walk
        # One pre-scan traversal (stats + C/C++ detection merged), the
        # file-list collection inside scan_directory, and the manifest
        # fingerprint walk at completion — the two pre-scan walks of the
        # old code are now one.
        self.assertEqual(
            len(walks), 3,
            "expected pre-scan + collection + manifest walks, got %r" % walks)
        self.assertTrue(os.path.exists(out_json))


if __name__ == "__main__":
    unittest.main()
