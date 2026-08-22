"""Tests for configurable circuit breaker + adaptive batch (D32)."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.daemon import Daemon, DEFAULT_CONFIG


class TestCircuitBreakerConfig(unittest.TestCase):
    """Test configurable circuit breaker thresholds."""

    def test_default_config_has_breaker_options(self):
        """DEFAULT_CONFIG includes the D32 breaker options."""
        self.assertIn("circuit_breaker_window_sec", DEFAULT_CONFIG)
        self.assertIn("circuit_breaker_threshold", DEFAULT_CONFIG)
        self.assertIn("circuit_breaker_cooldown_sec", DEFAULT_CONFIG)
        self.assertIn("adaptive_batch", DEFAULT_CONFIG)
        self.assertIn("format_only_filter", DEFAULT_CONFIG)

    def test_daemon_accepts_custom_threshold(self):
        """Daemon accepts a custom circuit_breaker_threshold."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Daemon(graph_dir=tmp, source_root=tmp,
                       config={"circuit_breaker_threshold": 500})
            self.assertEqual(d.config["circuit_breaker_threshold"], 500)

    def test_daemon_accepts_custom_window(self):
        """Daemon accepts a custom circuit_breaker_window_sec."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Daemon(graph_dir=tmp, source_root=tmp,
                       config={"circuit_breaker_window_sec": 120.0})
            self.assertEqual(d.config["circuit_breaker_window_sec"], 120.0)

    def test_daemon_accepts_custom_cooldown(self):
        """Daemon accepts a custom circuit_breaker_cooldown_sec."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Daemon(graph_dir=tmp, source_root=tmp,
                       config={"circuit_breaker_cooldown_sec": 60.0})
            self.assertEqual(d.config["circuit_breaker_cooldown_sec"], 60.0)


class TestEnvOverrides(unittest.TestCase):
    """Test CALLGRAPH_DAEMON_* env-var overrides."""

    def test_env_threshold_override(self):
        """CALLGRAPH_DAEMON_CIRCUIT_BREAKER_THRESHOLD overrides config."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Daemon(graph_dir=tmp, source_root=tmp,
                       config={"circuit_breaker_threshold": 1000})
            with patch.dict(os.environ,
                              {"CALLGRAPH_DAEMON_CIRCUIT_BREAKER_THRESHOLD": "2500"}):
                d._apply_env_overrides()
            self.assertEqual(d.config["circuit_breaker_threshold"], 2500)

    def test_env_batch_window_override(self):
        """CALLGRAPH_DAEMON_BATCH_WINDOW_MS overrides config."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Daemon(graph_dir=tmp, source_root=tmp,
                       config={"batch_window_ms": 1000})
            with patch.dict(os.environ,
                              {"CALLGRAPH_DAEMON_BATCH_WINDOW_MS": "3000"}):
                d._apply_env_overrides()
            self.assertEqual(d.config["batch_window_ms"], 3000)

    def test_env_adaptive_batch_bool_override(self):
        """CALLGRAPH_DAEMON_ADAPTIVE_BATCH parses 'true'/'false' as bool."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Daemon(graph_dir=tmp, source_root=tmp,
                       config={"adaptive_batch": True})
            with patch.dict(os.environ,
                              {"CALLGRAPH_DAEMON_ADAPTIVE_BATCH": "false"}):
                d._apply_env_overrides()
            self.assertFalse(d.config["adaptive_batch"])

    def test_env_invalid_value_ignored(self):
        """Invalid env-var values are ignored (no crash)."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Daemon(graph_dir=tmp, source_root=tmp,
                       config={"circuit_breaker_threshold": 1000})
            with patch.dict(os.environ,
                              {"CALLGRAPH_DAEMON_CIRCUIT_BREAKER_THRESHOLD": "not_a_number"}):
                d._apply_env_overrides()
            # Original value preserved
            self.assertEqual(d.config["circuit_breaker_threshold"], 1000)

    def test_env_window_sec_float_override(self):
        """CALLGRAPH_DAEMON_CIRCUIT_BREAKER_WINDOW_SEC parses as float."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Daemon(graph_dir=tmp, source_root=tmp,
                       config={"circuit_breaker_window_sec": 60.0})
            with patch.dict(os.environ,
                              {"CALLGRAPH_DAEMON_CIRCUIT_BREAKER_WINDOW_SEC": "90.5"}):
                d._apply_env_overrides()
            self.assertEqual(d.config["circuit_breaker_window_sec"], 90.5)


class TestAdaptiveBatchWindow(unittest.TestCase):
    """Test _compute_adaptive_batch_window."""

    def _make_daemon(self, tmpdir):
        d = Daemon(graph_dir=tmpdir, source_root=tmpdir,
                   config={"adaptive_batch_min_ms": 200,
                            "adaptive_batch_max_ms": 5000,
                            "circuit_breaker_threshold": 1000,
                            "batch_window_ms": 1000})
        return d

    def test_low_load_returns_min(self):
        """Low event rate (<30% threshold) returns min batch window."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp)
            d._event_timestamps = [1.0] * 100  # 100/1000 = 10% load
            result = d._compute_adaptive_batch_window()
            self.assertEqual(result, 200)

    def test_high_load_returns_max(self):
        """High event rate (>80% threshold) returns max batch window."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp)
            d._event_timestamps = [1.0] * 900  # 900/1000 = 90% load
            result = d._compute_adaptive_batch_window()
            self.assertEqual(result, 5000)

    def test_medium_load_interpolates(self):
        """Medium event rate interpolates between min and max."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp)
            d._event_timestamps = [1.0] * 550  # 55% load
            result = d._compute_adaptive_batch_window()
            self.assertGreater(result, 200)
            self.assertLess(result, 5000)

    def test_zero_threshold_returns_base(self):
        """Zero threshold returns base batch_window (no division)."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Daemon(graph_dir=tmp, source_root=tmp,
                       config={"circuit_breaker_threshold": 0,
                                "batch_window_ms": 1500,
                                "adaptive_batch_min_ms": 200,
                                "adaptive_batch_max_ms": 5000})
            result = d._compute_adaptive_batch_window()
            self.assertEqual(result, 1500)


class TestFormatOnlyFilter(unittest.TestCase):
    """Test _filter_format_only and _is_format_only_change."""

    def _make_daemon(self, tmpdir):
        return Daemon(graph_dir=tmpdir, source_root=tmpdir)

    def test_format_only_change_detected(self):
        """Whitespace-only changes are detected as format-only."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp)
            prev = "int main() {\n    return 0;\n}"
            cur = "int  main()  {\n        return  0;\n}"
            self.assertTrue(d._is_format_only_change(prev, cur))

    def test_comment_only_change_detected(self):
        """Comment-only changes are detected as format-only."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp)
            prev = "int main() {\n    return 0;\n}"
            cur = "// new comment\nint main() {\n    return 0;\n}"
            self.assertTrue(d._is_format_only_change(prev, cur))

    def test_real_change_not_format_only(self):
        """Real code changes are NOT detected as format-only."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp)
            prev = "int main() {\n    return 0;\n}"
            cur = "int main() {\n    return 1;\n}"
            self.assertFalse(d._is_format_only_change(prev, cur))

    def test_blank_lines_ignored(self):
        """Blank lines are stripped, so adding/removing them is format-only."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp)
            prev = "int main() {\n    return 0;\n}"
            cur = "int main() {\n\n    return 0;\n\n}"
            self.assertTrue(d._is_format_only_change(prev, cur))

    def test_filter_skips_format_only_files(self):
        """_filter_format_only skips files with only whitespace/comment changes."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp)
            # Write a file with initial content
            p = os.path.join(tmp, "test.c")
            with open(p, "w") as f:
                f.write("int main() {\n    return 0;\n}")
            # First call: file is new, kept
            kept = d._filter_format_only([p])
            self.assertEqual(kept, [p])
            # Now modify only whitespace
            with open(p, "w") as f:
                f.write("int main() {\n\n    return  0;\n}")
            kept2 = d._filter_format_only([p])
            self.assertEqual(kept2, [])  # filtered out

    def test_filter_keeps_real_changes(self):
        """_filter_format_only keeps files with real code changes."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp)
            p = os.path.join(tmp, "test.c")
            with open(p, "w") as f:
                f.write("int main() {\n    return 0;\n}")
            d._filter_format_only([p])  # populate cache
            # Real change
            with open(p, "w") as f:
                f.write("int main() {\n    return 1;\n}")
            kept = d._filter_format_only([p])
            self.assertEqual(kept, [p])

    def test_filter_keeps_deleted_files(self):
        """_filter_format_only always keeps deleted files (path doesn't exist)."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp)
            kept = d._filter_format_only(["/nonexistent/path.c"])
            self.assertEqual(kept, ["/nonexistent/path.c"])

    def test_filter_empty_input(self):
        """_filter_format_only handles empty input."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp)
            self.assertEqual(d._filter_format_only([]), [])


if __name__ == "__main__":
    unittest.main()
