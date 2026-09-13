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


if __name__ == "__main__":
    unittest.main()
