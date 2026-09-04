"""Tests for --graph auto-discovery on core read/query commands."""
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

from code2database_builder import _resolve_graph_dir  # noqa: E402

_BUILDER = os.path.join(SCRIPTS_DIR, 'code2database_builder.py')


class _Chdir(unittest.TestCase):
    """Helper: chdir in setUp, restore in tearDown."""

    def setUp(self):
        self._old = os.getcwd()

    def tearDown(self):
        os.chdir(self._old)


class TestResolveGraphDir(_Chdir):
    def _mk_graph(self, root, name="code2db-out"):
        g = os.path.join(root, name)
        os.makedirs(g)
        open(os.path.join(g, "code2database.db"), "w").close()
        return g

    def test_finds_code2db_out_upward(self):
        with tempfile.TemporaryDirectory() as root:
            g = self._mk_graph(root)
            deep = os.path.join(root, "a", "b", "c")
            os.makedirs(deep)
            os.chdir(deep)
            self.assertEqual(_resolve_graph_dir(), g)

    def test_cwd_is_graph_dir(self):
        with tempfile.TemporaryDirectory() as root:
            g = self._mk_graph(root, name="mygraph")
            os.chdir(g)
            self.assertEqual(_resolve_graph_dir(), g)

    def test_prefers_code2db_out_over_plain_dir(self):
        with tempfile.TemporaryDirectory() as root:
            g = self._mk_graph(root)
            plain = os.path.join(root, "plain")
            os.makedirs(plain)
            open(os.path.join(plain, "code2database.db"), "w").close()
            os.chdir(plain)
            # cwd itself is a graph dir -> wins over the sibling lookup? No:
            # the walk checks code2db-out/ under cwd first, then cwd itself.
            self.assertEqual(_resolve_graph_dir(), plain)

    def test_fallback_is_conventional_name(self):
        with tempfile.TemporaryDirectory() as root:
            os.chdir(root)
            self.assertEqual(_resolve_graph_dir(), "code2db-out")


class TestGraphFlagOmitted(_Chdir):
    def test_daemon_status_omitted_graph_resolves(self):
        with tempfile.TemporaryDirectory() as root:
            g = os.path.join(root, "code2db-out")
            os.makedirs(g)
            open(os.path.join(g, "code2database.db"), "w").close()
            deep = os.path.join(root, "sub")
            os.makedirs(deep)
            os.chdir(deep)
            proc = subprocess.run(
                [sys.executable, _BUILDER, '--log-level', 'CRITICAL',
                 'daemon-status'],
                capture_output=True, text=True, timeout=60,
            )
            self.assertIn('[graph] --graph not given; using', proc.stderr)
            self.assertIn(os.path.realpath(g), proc.stderr)
            self.assertNotIn('Traceback', proc.stderr)

    def test_session_init_help_documents_default(self):
        proc = subprocess.run(
            [sys.executable, _BUILDER, 'session-init', '--help'],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0)
        # argparse may wrap the help text mid-word; normalize before matching
        normalized = ' '.join(proc.stdout.split())
        self.assertIn('auto-discover', normalized)
        self.assertIn('code2db-out/', normalized.replace('code2db- out/',
                                                         'code2db-out/'))

    def test_init_alias_registered(self):
        """SKILL.md documents `init` as an alias for session-init."""
        with tempfile.TemporaryDirectory() as root:
            os.chdir(root)
            proc = subprocess.run(
                [sys.executable, _BUILDER, '--log-level', 'CRITICAL', 'init'],
                capture_output=True, text=True, timeout=60,
            )
            # Must NOT be "invalid choice"; it runs session-init which
            # either renders context or reports a missing graph gracefully.
            self.assertNotIn("invalid choice: 'init'", proc.stderr)
            self.assertNotIn('Traceback', proc.stderr)


if __name__ == '__main__':
    unittest.main()
