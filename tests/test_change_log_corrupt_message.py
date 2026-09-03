"""Regression: a corrupt change_log.json must not masquerade as
'no change log found'.

describe-commit / node-history swallowed JSONDecodeError with a debug
log and fell through to 'Run a build with --track-commits to populate
change_log' — telling the user their data was never tracked when it
existed but was corrupt, sending them on a wrong debugging path.
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.query import cmd_describe_commit, cmd_node_history


def _corrupt_graph_dir():
    d = tempfile.mkdtemp()
    Path(d, ".code2database_change_log.json").write_text(
        '{"corrupt": tru', encoding="utf-8")
    return d


class TestCorruptChangeLogMessage(unittest.TestCase):
    def test_describe_commit_reports_corruption(self):
        d = _corrupt_graph_dir()
        ns = Namespace(graph=d, commit="abc123", json=False)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                cmd_describe_commit(ns)
        self.assertEqual(cm.exception.code, 1)
        msg = err.getvalue()
        self.assertIn("could not be read", msg)
        self.assertIn("corrupt or truncated", msg)
        # must NOT claim the data was never tracked
        self.assertNotIn("to populate change_log", msg)

    def test_node_history_reports_corruption(self):
        d = _corrupt_graph_dir()
        ns = Namespace(graph=d, node="n1", json=False)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                cmd_node_history(ns)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("could not be read", err.getvalue())


if __name__ == "__main__":
    unittest.main()
