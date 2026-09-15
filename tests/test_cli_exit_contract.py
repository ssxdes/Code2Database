"""Exit-code and error-shape contract shared by both CLIs.

Both entry points follow the same contract: 0=success, 1=error,
2=usage error (no command / unknown command), 130=interrupt. On an
unexpected error both print one concise line to stderr; the full
traceback is opt-in via C2D_TRACEBACK=1.
"""
import contextlib
import io
import os
import sys
import unittest
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import importlib.util


def _load(script):
    spec = importlib.util.spec_from_file_location(
        "_exitprobe_" + os.path.basename(script)[:-3], os.path.join(SCRIPTS, script))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestUsageExitCodes(unittest.TestCase):

    def test_builder_no_command_exits_two(self):
        builder = _load("code2database_builder.py")
        out = io.StringIO()
        old_argv = sys.argv[:]
        try:
            sys.argv = ["code2database_builder.py"]
            with contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit) as cm:
                    builder.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("usage", out.getvalue().lower())

    def test_scanner_no_command_exits_two(self):
        scanner = _load("code2database_scanner.py")
        out = io.StringIO()
        old_argv = sys.argv[:]
        try:
            sys.argv = ["code2database_scanner.py"]
            with contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit) as cm:
                    scanner.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(cm.exception.code, 2)

    def test_scanner_unknown_command_exits_two(self):
        scanner = _load("code2database_scanner.py")
        err = io.StringIO()
        old_argv = sys.argv[:]
        try:
            sys.argv = ["code2database_scanner.py", "no-such-verb"]
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as cm:
                    scanner.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(cm.exception.code, 2)


class TestErrorShape(unittest.TestCase):

    def _run_scanner_with_raising_handler(self):
        scanner = _load("code2database_scanner.py")
        err = io.StringIO()
        old_argv = sys.argv[:]
        try:
            sys.argv = ["code2database_scanner.py", "scan",
                        "--source", "/nonexistent-probe"]
            with patch.object(scanner, "cmd_scan",
                              side_effect=RuntimeError("probe-boom")):
                with contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as cm:
                        scanner.main()
        finally:
            sys.argv = old_argv
        return cm.exception.code, err.getvalue()

    def test_scanner_error_is_one_line_without_traceback(self):
        code, err = self._run_scanner_with_raising_handler()
        self.assertEqual(code, 1)
        self.assertIn("Error: probe-boom", err)
        self.assertNotIn("Traceback", err)

    def test_scanner_traceback_is_opt_in(self):
        old = os.environ.get("C2D_TRACEBACK")
        os.environ["C2D_TRACEBACK"] = "1"
        try:
            code, err = self._run_scanner_with_raising_handler()
        finally:
            if old is None:
                os.environ.pop("C2D_TRACEBACK", None)
            else:
                os.environ["C2D_TRACEBACK"] = old
        self.assertEqual(code, 1)
        self.assertIn("Traceback", err)
        self.assertIn("Error: probe-boom", err)

    def _run_builder_with_raising_handler(self):
        builder = _load("code2database_builder.py")
        err = io.StringIO()
        old_argv = sys.argv[:]
        try:
            sys.argv = ["code2database_builder.py", "kb-known-unknowns"]
            with patch.object(builder, "cmd_kb_known_unknowns",
                              side_effect=RuntimeError("probe-boom")):
                with contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as cm:
                        builder.main()
        finally:
            sys.argv = old_argv
        return cm.exception.code, err.getvalue()

    def test_builder_error_is_one_line_without_traceback(self):
        code, err = self._run_builder_with_raising_handler()
        self.assertEqual(code, 1)
        self.assertIn("Error: probe-boom", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
