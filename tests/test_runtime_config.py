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


if __name__ == "__main__":
    unittest.main()
