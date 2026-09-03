"""Unit tests for memory_guard.py — adaptive memory management.

Covers: MemoryGuard absolute-MB and fractional threshold logic,
check_and_adapt multiplier behavior (critical backoff, warn linear
backoff, recovery), maybe_gc interval gating, wait_for_memory,
drop_body_text, get_stats, start/stop monitoring; the module-level
helpers adaptive_batch_size, memory_safe_append, create_memory_guard,
get/set_global_guard; and the StreamingWriter /
StreamingJsonObjectWriter streaming JSON serializers.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder import memory_guard as mg
from _builder.memory_guard import (
    MemoryGuard, adaptive_batch_size, memory_safe_append,
    create_memory_guard, get_global_guard, set_global_guard,
    StreamingWriter, StreamingJsonObjectWriter,
)


def _info(total=1000, available=400, used=600, rss=100):
    # usage_percent is a FRACTION in [0,1] — matches get_memory_info's
    # (total-available)/total computation, not a 0-100 percentage.
    return {"total_mb": total, "available_mb": available,
            "used_mb": used, "usage_percent": used / total, "rss_mb": rss}


class _FixedInfoGuard(MemoryGuard):
    """Guard whose memory readings are pinned for deterministic tests."""

    def __init__(self, info, **kw):
        # Must be set BEFORE super().__init__ — the parent constructor
        # calls get_memory_info() (overridden below) for its baseline.
        self._fixed = info
        super().__init__(dynamic=False, **kw)

    def get_memory_info(self):
        info = dict(self._fixed)
        # keep the fraction consistent when tests mutate used_mb
        if info["total_mb"] > 0:
            info["usage_percent"] = info["used_mb"] / info["total_mb"]
        return info


class TestMemoryGuardThresholds(unittest.TestCase):
    def test_absolute_mb_warn_and_crit_levels(self):
        g = _FixedInfoGuard(_info(used=60),
                            warn_threshold_mb=50, crit_threshold_mb=100)
        self.assertTrue(g.is_memory_low())
        self.assertFalse(g.is_memory_critical())
        g._fixed["used_mb"] = 120
        self.assertTrue(g.is_memory_low())
        self.assertTrue(g.is_memory_critical())

    def test_fractional_thresholds(self):
        g = _FixedInfoGuard(_info(total=1000, used=780))
        self.assertEqual(g.warn_threshold, 0.75)
        self.assertEqual(g.crit_threshold, 0.85)
        self.assertTrue(g.is_memory_low())      # 78% >= 75%
        self.assertFalse(g.is_memory_critical())  # 78% < 85%
        g._fixed["used_mb"] = 900
        self.assertTrue(g.is_memory_critical())

    def test_healthy_memory_below_both(self):
        g = _FixedInfoGuard(_info(total=1000, used=400))
        self.assertFalse(g.is_memory_low())
        self.assertFalse(g.is_memory_critical())


class TestCheckAndAdapt(unittest.TestCase):
    def test_healthy_returns_full_multiplier(self):
        g = _FixedInfoGuard(_info(total=1000, used=400))
        m = g.check_and_adapt()
        self.assertEqual(m, 1.0)
        self.assertEqual(g.get_stats()["peak_memory_mb"], 400)

    def test_critical_multiplier_shrinks_compound(self):
        g = _FixedInfoGuard(_info(total=1000, used=950),
                            batch_reduction_factor=0.5)
        m1 = g.check_and_adapt()
        self.assertEqual(m1, 0.5)
        m2 = g.check_and_adapt()  # second critical → factor^2
        self.assertEqual(m2, 0.25)
        self.assertEqual(g.get_stats()["criticals"], 2)

    def test_multiplier_floored_at_0_1(self):
        g = _FixedInfoGuard(_info(total=1000, used=950),
                            batch_reduction_factor=0.5)
        for _ in range(6):
            g.check_and_adapt()
        m = g.check_and_adapt()
        self.assertEqual(m, 0.1)  # 0.5^7 clamped to 0.1

    def test_warn_linear_backoff_between_warn_and_crit(self):
        g = _FixedInfoGuard(_info(total=1000, used=800),
                            warn_threshold_mb=750, crit_threshold_mb=1000)
        m = g.check_and_adapt()
        # over_warn=50, span=250 → 1 - 50/250 = 0.8
        self.assertAlmostEqual(m, 0.8, places=6)
        self.assertEqual(g.get_stats()["warnings"], 1)

    def test_recovery_raises_multiplier_gradually(self):
        g = _FixedInfoGuard(_info(total=1000, used=950),
                            batch_reduction_factor=0.5)
        g.check_and_adapt()  # 0.5
        g._fixed["used_mb"] = 100  # healthy again
        m = g.check_and_adapt()
        self.assertAlmostEqual(m, 0.6)  # 0.5 + 0.1 recovery

    def test_warning_recorded_in_memory_warnings(self):
        g = _FixedInfoGuard(_info(total=1000, used=800),
                            warn_threshold_mb=750, crit_threshold_mb=1000)
        g.check_and_adapt()
        self.assertTrue(any("WARNING" in w for w in g._memory_warnings))


class TestMaybeGc(unittest.TestCase):
    def test_runs_gc_when_memory_at_warn_level(self):
        g = _FixedInfoGuard(_info(total=1000, used=800))
        with mock.patch.object(mg.gc, "collect", return_value=0) as coll:
            ran = g.maybe_gc()
        self.assertTrue(ran)
        coll.assert_called()

    def test_healthy_memory_skips_gc_without_force(self):
        """GC is a stopgap: with memory healthy and no force, skip it."""
        g = _FixedInfoGuard(_info(total=1000, used=400))
        with mock.patch.object(mg.gc, "collect", return_value=0) as coll:
            ran = g.maybe_gc()
        self.assertFalse(ran)
        coll.assert_not_called()

    def test_interval_gates_repeat_calls(self):
        g = _FixedInfoGuard(_info(total=1000, used=800), gc_interval=999)
        g.maybe_gc()
        with mock.patch.object(mg.gc, "collect", return_value=0) as coll:
            ran = g.maybe_gc()
        self.assertFalse(ran)
        coll.assert_not_called()

    def test_force_bypasses_interval_and_level(self):
        g = _FixedInfoGuard(_info(total=1000, used=400), gc_interval=999)
        g.maybe_gc()
        with mock.patch.object(mg.gc, "collect", return_value=0) as coll:
            ran = g.maybe_gc(force=True)
        self.assertTrue(ran)
        coll.assert_called()


class TestWaitForMemory(unittest.TestCase):
    def test_returns_true_when_not_critical(self):
        g = _FixedInfoGuard(_info(used=400))
        self.assertTrue(g.wait_for_memory(timeout=0.5))

    def test_times_out_when_critical(self):
        g = _FixedInfoGuard(_info(total=1000, used=990),
                            crit_threshold=0.85)
        self.assertFalse(g.wait_for_memory(timeout=0.3))


class TestDropBodyText(unittest.TestCase):
    def test_drops_body_text_and_counts(self):
        g = _FixedInfoGuard(_info())
        fns = [{"name": "a", "body_text": "x" * 100},
               {"name": "b", "body_text": "y" * 50},
               {"name": "c", "signature": "int c(void)"}]
        n = g.drop_body_text(fns)
        self.assertEqual(n, 2)
        self.assertNotIn("body_text", fns[0])
        self.assertNotIn("body_text", fns[1])
        self.assertEqual(fns[2]["signature"], "int c(void)")  # untouched


class TestMonitoringLifecycle(unittest.TestCase):
    def test_start_and_stop_monitoring(self):
        g = _FixedInfoGuard(_info())
        g.start_monitoring(interval=0.05)
        try:
            self.assertTrue(g._monitoring)
            self.assertTrue(g._monitor_thread.is_alive())
        finally:
            g.stop_monitoring()
        self.assertFalse(g._monitoring)
        self.assertIsNone(g._monitor_thread)  # cleared after join

    def test_stats_file_written_on_warning(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "stats.json")
            g = _FixedInfoGuard(_info(total=1000, used=950),
                                stats_file=path)
            g.check_and_adapt()
            with open(path, encoding="utf-8") as f:
                stats = json.load(f)
            self.assertGreater(stats["memory"]["used_mb"], 0)
            self.assertTrue(stats["warnings_list"])


class TestAdaptiveBatchSize(unittest.TestCase):
    def test_healthy_memory_full_batch(self):
        g = _FixedInfoGuard(_info(total=1000, used=400))
        self.assertEqual(adaptive_batch_size(1000, g), 1000)

    def test_reduced_memory_scales_batch(self):
        g = _FixedInfoGuard(_info(total=1000, used=950),
                            batch_reduction_factor=0.5)
        self.assertEqual(adaptive_batch_size(1000, g), 500)

    def test_minimum_batch_floor_100(self):
        g = _FixedInfoGuard(_info(total=1000, used=950),
                            batch_reduction_factor=0.5)
        for _ in range(6):
            g.check_and_adapt()
        self.assertEqual(adaptive_batch_size(200, g), 100)


class TestMemorySafeAppend(unittest.TestCase):
    def test_extends_list(self):
        g = _FixedInfoGuard(_info())
        target = [1, 2]
        memory_safe_append(target, [3, 4], guard=g, max_size=1000)
        self.assertEqual(target, [1, 2, 3, 4])

    def test_no_guard_still_appends(self):
        target = []
        memory_safe_append(target, [1], guard=None)
        self.assertEqual(target, [1])

    def test_warns_when_list_exceeds_max_size(self):
        # warning fires when len > max_size AND len % (max_size//2) == 0:
        # 100 + 50 = 150 > 100, 150 % 50 == 0
        target = list(range(100))
        err = io.StringIO()
        with redirect_stderr(err):
            memory_safe_append(target, list(range(50)), max_size=100)
        self.assertEqual(len(target), 150)
        self.assertIn("growing large", err.getvalue())

    def test_guard_hooks_invoked(self):
        g = _FixedInfoGuard(_info())
        with mock.patch.object(g, "check_and_adapt", return_value=1.0) as ca, \
                mock.patch.object(g, "maybe_gc", return_value=False) as gc_:
            memory_safe_append([], [1], guard=g)
        ca.assert_called_once()
        gc_.assert_called_once()


class TestCreateMemoryGuard(unittest.TestCase):
    def test_defaults(self):
        g = create_memory_guard(type("A", (), {}))
        self.assertEqual(g.warn_threshold, MemoryGuard.DEFAULT_WARN_THRESHOLD)
        self.assertEqual(g.crit_threshold, MemoryGuard.DEFAULT_CRIT_THRESHOLD)
        self.assertIsNone(g.stats_file)

    def test_args_passed_through(self):
        args = type("A", (), {
            "memory_warn_threshold": 0.5, "memory_crit_threshold": 0.9,
            "memory_warn_mb": 100.0, "memory_crit_mb": 200.0,
            "memory_stats": "/tmp/x.json", "large_project": True,
        })()
        g = create_memory_guard(args)
        self.assertEqual(g.warn_threshold, 0.5)
        self.assertEqual(g.crit_threshold, 0.9)
        self.assertEqual(g.warn_threshold_mb, 100.0)
        self.assertEqual(g.crit_threshold_mb, 200.0)
        self.assertEqual(g.stats_file, "/tmp/x.json")
        self.assertEqual(g.batch_reduction_factor, 0.3)  # large_project


class TestGlobalGuard(unittest.TestCase):
    def tearDown(self):
        set_global_guard(None)

    def test_default_none(self):
        self.assertIsNone(get_global_guard())

    def test_set_and_get(self):
        g = MemoryGuard(dynamic=False)
        set_global_guard(g)
        self.assertIs(get_global_guard(), g)


class TestStreamingWriter(unittest.TestCase):
    def test_writes_valid_json_array(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.json")
            w = StreamingWriter(path)
            w.begin()
            w.write_item({"id": 1})
            w.write_item({"id": 2})
            count = w.end()
            self.assertEqual(count, 2)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data, [{"id": 1}, {"id": 2}])

    def test_lazy_begin_on_first_write(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.json")
            w = StreamingWriter(path)
            w.write_item({"a": 1})  # begins implicitly
            self.assertTrue(w._started)
            w.end()

    def test_write_items_and_context_manager(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.json")
            with StreamingWriter(path) as w:
                w.write_items([{"x": 1}, {"x": 2}, {"x": 3}])
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), [{"x": 1}, {"x": 2}, {"x": 3}])

    def test_empty_stream_is_valid_empty_array(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.json")
            w = StreamingWriter(path)
            w.begin()
            self.assertEqual(w.end(), 0)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), [])

    def test_unicode_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.json")
            with StreamingWriter(path) as w:
                w.write_item({"注释": "中文内容"})
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), [{"注释": "中文内容"}])


class TestStreamingJsonObjectWriter(unittest.TestCase):
    def test_scalars_and_arrays_combined(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.json")
            w = StreamingJsonObjectWriter(path)
            w.begin()
            w.write_scalar("version", 3)
            w.begin_array("nodes")
            w.write_array_item({"id": "a"})
            w.write_array_item({"id": "b"})
            w.end_array()
            w.write_scalar("done", True)
            w.end()
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["version"], 3)
            self.assertEqual(data["nodes"], [{"id": "a"}, {"id": "b"}])
            self.assertTrue(data["done"])

    def test_empty_array_valid(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.json")
            w = StreamingJsonObjectWriter(path)
            w.begin()
            w.begin_array("edges")
            n = w.end_array()
            w.end()
            self.assertEqual(n, 0)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["edges"], [])


if __name__ == "__main__":
    unittest.main()
