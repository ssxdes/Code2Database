"""Tests for signal-death reporting in update/sync scanner subprocesses.

The update pipeline shells out to code2database_scanner.py twice
(detect-changes, then the changed-file scan). A scanner killed by the
OOM killer returns -9 with EMPTY stderr — the old messages printed
"Error ...: " with nothing after the colon, leaving the cause
undiscoverable without dmesg.
"""
import argparse
import contextlib
import io
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

from _builder.build import update_sync  # noqa: E402


class _TmpSource(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp()
        self.source = os.path.join(self._tmp, "proj")
        self.graph = os.path.join(self._tmp, "g-out")
        os.makedirs(self.source)
        with open(os.path.join(self.source, "main.c"), "w") as f:
            f.write("int main(void){return 0;}\n")


class TestScannerDeathReporting(_TmpSource):

    def test_detect_changes_death_names_the_signal(self):
        with mock.patch.object(
                update_sync.subprocess, "run",
                return_value=SimpleNamespace(returncode=-9, stderr="",
                                             stdout="")):
            args = argparse.Namespace(source=self.source, graph=self.graph)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as cm:
                    update_sync.cmd_update(args)
        self.assertNotEqual(cm.exception.code, 0)
        out = stderr.getvalue()
        self.assertIn("detecting changes", out)
        self.assertIn("SIGKILL", out)
        self.assertIn("OOM", out)

    def test_scan_death_names_the_signal(self):
        # First subprocess call (detect-changes) succeeds and reports
        # one changed file; the second (the scan itself) dies from
        # SIGKILL with empty stderr.
        import json
        os.makedirs(self.graph, exist_ok=True)
        with open(os.path.join(self.graph, "code2database_master.json"),
                  "w") as f:
            json.dump({"source_root": self.source, "domains": {}}, f)
        detect_payload = json.dumps({
            "needs_full_scan": False, "new_paths": ["main.c"],
            "changed_paths": [], "deleted_relpaths": []})
        counter = {"n": 0}

        def _fake_run(cmd, **kw):
            counter["n"] += 1
            if counter["n"] == 1:
                return SimpleNamespace(returncode=0, stderr="",
                                       stdout=detect_payload)
            return SimpleNamespace(returncode=-9, stderr="", stdout="")

        with mock.patch.object(update_sync.subprocess, "run",
                               side_effect=_fake_run):
            args = argparse.Namespace(
                source=self.source, graph=self.graph,
                extraction="", compile_commands="", clang_args="",
                extraction_backend="", parallel_mode="")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as cm:
                    update_sync.cmd_update(args)
        self.assertNotEqual(cm.exception.code, 0)
        out = stderr.getvalue()
        self.assertIn("scanning changed files", out)
        self.assertIn("SIGKILL", out)


if __name__ == "__main__":
    unittest.main()
