"""config/runtime.json is loaded, validated and actually consumed.

RUNTIME_CONFIG.md promises that edits to this file take effect on the
next pipeline run. These tests pin the loader contract (discovery,
precedence, cache refresh, unknown-key reporting) and the argparse
wiring for every knob the documentation maps to a CLI default.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _builder import runtime_config
from _builder.runtime_config import load_runtime_config, runtime_get


def _write_config(tmpdir: str, data: dict) -> str:
    path = os.path.join(tmpdir, "runtime.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return path


class TestLoaderContract(unittest.TestCase):

    def test_absent_file_yields_empty_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["C2D_RUNTIME_CONFIG"] = os.path.join(tmp, "missing.json")
            try:
                self.assertEqual(load_runtime_config(), {})
                self.assertEqual(runtime_get("scan", "workers", 7), 7)
            finally:
                os.environ.pop("C2D_RUNTIME_CONFIG", None)

    def test_values_load_and_env_path_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp, {"scan": {"workers": 3}})
            os.environ["C2D_RUNTIME_CONFIG"] = path
            try:
                self.assertEqual(runtime_get("scan", "workers", 0), 3)
                self.assertEqual(runtime_get("scan", "parallel_mode", "thread"), "thread")
            finally:
                os.environ.pop("C2D_RUNTIME_CONFIG", None)

    def test_invalid_json_uses_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "runtime.json")
            Path(path).write_text("{not json")
            os.environ["C2D_RUNTIME_CONFIG"] = path
            try:
                with self.assertLogs("callgraph.runtime_config", level="WARNING"):
                    self.assertEqual(load_runtime_config(), {})
            finally:
                os.environ.pop("C2D_RUNTIME_CONFIG", None)

    def test_unknown_keys_are_reported_and_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp, {
                "scan": {"workers": 2, "no_such_knob": 1},
                "no_such_section": {"x": 1},
            })
            os.environ["C2D_RUNTIME_CONFIG"] = path
            try:
                with self.assertLogs("callgraph.runtime_config", level="WARNING") as logs:
                    data = load_runtime_config()
                self.assertEqual(data, {"scan": {"workers": 2}})
                joined = "\n".join(logs.output)
                self.assertIn("no_such_knob", joined)
                self.assertIn("no_such_section", joined)
            finally:
                os.environ.pop("C2D_RUNTIME_CONFIG", None)

    def test_wrong_type_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp, {"scan": {"workers": "many"}})
            os.environ["C2D_RUNTIME_CONFIG"] = path
            try:
                with self.assertLogs("callgraph.runtime_config", level="WARNING"):
                    data = load_runtime_config()
                self.assertEqual(data, {})
            finally:
                os.environ.pop("C2D_RUNTIME_CONFIG", None)

    def test_cache_refreshes_when_mtime_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp, {"scan": {"workers": 1}})
            os.environ["C2D_RUNTIME_CONFIG"] = path
            try:
                self.assertEqual(runtime_get("scan", "workers", 0), 1)
                future = time.time() + 10
                os.utime(path, (future, future))
                _write_config(tmp, {"scan": {"workers": 4}})
                os.utime(path, (future + 5, future + 5))
                self.assertEqual(runtime_get("scan", "workers", 0), 4)
            finally:
                os.environ.pop("C2D_RUNTIME_CONFIG", None)


def _sniff_defaults(entry: str, argv) -> dict:
    """Execute an entry script's main() and capture argparse defaults."""
    spec = importlib.util.spec_from_file_location(
        "_rcprobe_" + Path(entry).stem, SCRIPTS / entry)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    captured = {}
    orig = argparse.ArgumentParser.parse_known_args

    def sniff(self, args=None, namespace=None):
        for act in self._actions:
            if isinstance(act, argparse._SubParsersAction):
                continue
            if act.dest not in ("help", "command") and act.default is not None \
                    and not callable(act.default):
                captured.setdefault(act.dest, act.default)
        return orig(self, args, namespace)

    old_argv = sys.argv[:]
    argparse.ArgumentParser.parse_known_args = sniff
    try:
        sys.argv = [entry] + list(argv)
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    mod.main()
                except SystemExit:
                    pass
    finally:
        argparse.ArgumentParser.parse_known_args = orig
        sys.argv = old_argv
    return captured


class TestArgparseWiring(unittest.TestCase):

    def test_scanner_scan_defaults_follow_runtime_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["C2D_RUNTIME_CONFIG"] = _write_config(tmp, {
                "scan": {"workers": 3, "parallel_mode": "process"}})
            try:
                defaults = _sniff_defaults(
                    "code2database_scanner.py", ["scan", "--help"])
            finally:
                os.environ.pop("C2D_RUNTIME_CONFIG", None)
        self.assertEqual(defaults.get("workers"), 3)
        self.assertEqual(defaults.get("parallel_mode"), "process")

    def test_builder_build_and_query_defaults_follow_runtime_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["C2D_RUNTIME_CONFIG"] = _write_config(tmp, {
                "build": {"default_config": "Release", "max_domain_files": 9},
                "query": {"default_detail": "standard", "default_max_tokens": 333,
                          "explore_max_nodes": 4, "explore_max_tokens": 444}})
            try:
                build = _sniff_defaults(
                    "code2database_builder.py", ["build", "--help"])
                desc = _sniff_defaults(
                    "code2database_builder.py", ["describe-node", "--help"])
                explore = _sniff_defaults(
                    "code2database_builder.py", ["explore-flow", "--help"])
            finally:
                os.environ.pop("C2D_RUNTIME_CONFIG", None)
        self.assertEqual(build.get("max_domain_files"), 9)
        self.assertEqual(build.get("build_config"), "Release")
        self.assertEqual(desc.get("detail"), "standard")
        self.assertEqual(desc.get("max_tokens"), 333)
        self.assertEqual(explore.get("max_nodes"), 4)
        self.assertEqual(explore.get("max_tokens"), 444)

    def test_absent_config_keeps_documented_defaults(self):
        os.environ["C2D_RUNTIME_CONFIG"] = os.path.join(
            tempfile.gettempdir(), "c2d_no_such_runtime.json")
        try:
            desc = _sniff_defaults(
                "code2database_builder.py", ["describe-node", "--help"])
            explore = _sniff_defaults(
                "code2database_builder.py", ["explore-flow", "--help"])
        finally:
            os.environ.pop("C2D_RUNTIME_CONFIG", None)
        self.assertEqual(desc.get("detail"), "brief")
        self.assertEqual(desc.get("max_tokens"), 500)
        self.assertEqual(explore.get("max_nodes"), 15)
        self.assertEqual(explore.get("max_tokens"), 2000)


# Documented-but-unwired knobs: declared in _SPEC and described in
# RUNTIME_CONFIG.md, but no runtime_get() consumer reads them yet.
# They are kept as reserved tuning knobs; a NEW key must either be
# wired to a runtime_get() call or be consciously added here.
_PENDING_KEYS = {
    "scan": ("max_file_size_kb", "skip_dirs"),
    "memory": ("decay_factor", "consolidate_threshold", "scratch_ttl_hours"),
    "semantic": ("stale_ratio_threshold", "stale_api_threshold"),
    "invariants": ("state_machine_threshold", "extract_preconditions",
                   "extract_postconditions", "extract_loop_invariants",
                   "reject_ambiguous"),
    "auto_enhance": ("auto_apply_extracted", "require_confirm_inferred",
                     "reject_ambiguous", "rollback_window",
                     "batch_confirm_size"),
    "transactions": ("snapshot_keep_count", "lock_timeout_seconds"),
    "ffi": ("detect_python_ctypes", "detect_go_cgo", "detect_rust_extern",
            "flag_lossy_conversions"),
    "web_ui": ("port", "host", "open_browser", "max_nodes_render"),
    "benchmark": ("recall_target", "max_tool_calls", "max_tokens"),
    "profile_health": ("min_score", "auto_apply_extracted",
                       "require_confirm_inferred", "bind_to_head"),
    "doc_code": ("check_on_describe", "signature_diff_strict"),
    "daemon": ("enabled", "watch_paths", "exclude_patterns", "debounce_ms",
               "batch_window_ms", "auto_rebuild_outputs",
               "idle_sleep_minutes", "max_events_per_minute", "backend",
               "startup_grace_sec"),
}


class TestSpecDriftGuards(unittest.TestCase):
    """_SPEC keys must be wired, registered as pending, and documented.

    Two retired keys (wal_enabled, auto_replay_on_start) once claimed
    crash-recovery behavior that no code provided — they are gone from
    the spec and the docs, and these guards keep them (and any future
    undocumented-or-unwired key) from drifting back in.
    """

    @classmethod
    def setUpClass(cls):
        cls.scripts_text = {
            str(p): p.read_text(encoding="utf-8")
            for p in SCRIPTS.rglob("*.py")
        }

    def _consumed_keys(self):
        """(section, key) pairs referenced by a runtime_get() call."""
        consumed = set()
        pattern = "runtime_get("
        for text in self.scripts_text.values():
            if pattern not in text:
                continue
            for m in re.finditer(
                    r'runtime_get\(\s*["\']([a-z_]+)["\']\s*,\s*'
                    r'["\']([a-z_]+)["\']', text):
                consumed.add((m.group(1), m.group(2)))
        return consumed

    def test_retired_wal_keys_stay_gone(self):
        spec_keys = {k for keys in runtime_config._SPEC.values()
                     for k in keys}
        self.assertNotIn("wal_enabled", spec_keys)
        self.assertNotIn("auto_replay_on_start", spec_keys)
        for name in ("wal_enabled", "auto_replay_on_start"):
            for text in self.scripts_text.values():
                self.assertNotIn(f'"{name}"', text,
                                 f"retired key {name} resurfaced")
            for doc in ("docs/en/RUNTIME_CONFIG.md",
                        "docs/zh/RUNTIME_CONFIG.md"):
                self.assertNotIn(
                    f"`{name}`",
                    (REPO / doc).read_text(encoding="utf-8"),
                    f"retired key {name} still documented in {doc}")

    def test_spec_keys_are_consumed_or_registered(self):
        consumed = self._consumed_keys()
        undeclared_consumers = {
            pair for pair in consumed
            if pair[0] not in runtime_config._SPEC
            or pair[1] not in runtime_config._SPEC[pair[0]]}
        self.assertEqual(
            undeclared_consumers, set(),
            "runtime_get() calls reference keys missing from _SPEC")
        not_consumed = []
        for section, keys in runtime_config._SPEC.items():
            for key in keys:
                if (section, key) in consumed:
                    continue
                if key in _PENDING_KEYS.get(section, ()):
                    continue
                not_consumed.append(f"{section}.{key}")
        self.assertEqual(
            not_consumed, [],
            "keys declared in _SPEC are neither wired to runtime_get() "
            "nor registered in _PENDING_KEYS: %s" % not_consumed)

    def test_spec_keys_match_the_field_reference(self):
        """RUNTIME_CONFIG.md rows and _SPEC keys must mirror each other."""
        rows = set()
        for line in (REPO / "docs/en/RUNTIME_CONFIG.md").read_text(
                encoding="utf-8").splitlines():
            m = re.match(r"\|\s*`([a-z_]+)`\s*\|", line)
            if m:
                rows.add(m.group(1))
        spec_keys = {k for keys in runtime_config._SPEC.values()
                     for k in keys}
        self.assertEqual(rows - spec_keys, set(),
                         "documented keys missing from _SPEC")
        self.assertEqual(spec_keys - rows, set(),
                         "spec keys missing from the field reference")


if __name__ == "__main__":
    unittest.main()
