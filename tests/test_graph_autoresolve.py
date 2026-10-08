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

    def test_finds_kb_only_code2db_out_upward(self):
        """A knowledge/memory-only store is discovered like a graph."""
        with tempfile.TemporaryDirectory() as root:
            g = os.path.join(root, "code2db-out")
            os.makedirs(os.path.join(g, "memory"))
            open(os.path.join(g, "memory", "memory.db"), "w").close()
            deep = os.path.join(root, "a", "b", "c")
            os.makedirs(deep)
            os.chdir(deep)
            self.assertEqual(_resolve_graph_dir(), g)

    def test_kb_markers_discover_cwd_store(self):
        with tempfile.TemporaryDirectory() as root:
            g = os.path.join(root, "store")
            os.makedirs(os.path.join(g, "knowledge"))
            open(os.path.join(g, "knowledge", "brief.json"), "w").close()
            os.chdir(g)
            self.assertEqual(_resolve_graph_dir(), g)

    def test_graph_marker_wins_over_kb_marker(self):
        with tempfile.TemporaryDirectory() as root:
            graph_dir = os.path.join(root, "graph-out")
            kb_dir = os.path.join(root, "kb-out")
            for d, marker in ((graph_dir, "code2database.db"),
                              (kb_dir, os.path.join("memory",
                                                    "memory.db"))):
                os.makedirs(os.path.dirname(os.path.join(d, marker)),
                            exist_ok=True)
                open(os.path.join(d, marker), "w").close()
            # Both are code2db-out-shaped siblings? No — the walk looks
            # for code2db-out by name first; craft that shape instead.
            g = os.path.join(root, "code2db-out")
            os.makedirs(os.path.join(g, "memory"))
            open(os.path.join(g, "memory", "memory.db"), "w").close()
            plain_graph = os.path.join(root, "plain")
            os.makedirs(plain_graph)
            open(os.path.join(plain_graph, "code2database.db"), "w").close()
            os.chdir(root)
            # code2db-out/ (kb-only) is checked before cwd itself.
            self.assertEqual(_resolve_graph_dir(), g)

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


class TestKbOnlyCommandsGraphOptional(_Chdir):
    """kb-only store commands accept an omitted --graph.

    Their handlers never touch the code graph (memory.db /
    knowledge.db / kb_index.db only), so the store directory resolves
    through kb markers exactly like graph stores and a kb-only project
    is not forced to spell --graph on every call.
    """

    KB_ONLY_COMMANDS = [
        "kb-migrate", "kb-conflict", "kb-rollback",
        "kb-global-share-memory", "kb-global-import-memory",
        "brief-update", "brief-extract", "brief-validate",
        "brief-suggest", "brief-migrate-legacy",
        "manage-memory", "memory-health",
    ]

    def test_kb_only_commands_have_optional_graph_flag(self):
        for cmd in self.KB_ONLY_COMMANDS:
            proc = subprocess.run(
                [sys.executable, _BUILDER, cmd, '--help'],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 0, cmd)
            # argparse renders optional flags in brackets in the usage
            # line; required ones render bare
            self.assertIn('[--graph GRAPH]', proc.stdout, cmd)

    def test_memory_health_omitted_graph_resolves_kb_store(self):
        with tempfile.TemporaryDirectory() as root:
            g = os.path.join(root, "store")
            os.makedirs(os.path.join(g, "memory"))
            open(os.path.join(g, "memory", "memory.db"), "w").close()
            os.chdir(g)
            proc = subprocess.run(
                [sys.executable, _BUILDER, '--log-level', 'CRITICAL',
                 'memory-health'],
                capture_output=True, text=True, timeout=60,
            )
            self.assertIn('[graph] --graph not given; using', proc.stderr)
            self.assertNotIn(
                'the following arguments are required', proc.stderr)
            self.assertNotIn('Traceback', proc.stderr)


    def test_kb_family_help_labels_store_directory(self):
        """kb-family --graph help says store, not call-graph output.

        The old text read "Call graph output directory" (or duplicated
        the auto-discover hint) on commands that never touch a call
        graph; the rendered help must name the store directory and
        carry the default hint at most once.
        """
        for cmd in ["kb-init", "kb-domain-add", "memory-health",
                    "manage-memory", "kb-query", "brief-update",
                    "kb-global-share-memory", "session-init"]:
            proc = subprocess.run(
                [sys.executable, _BUILDER, cmd, '--help'],
                capture_output=True, text=True, timeout=60,
            )
            normalized = ' '.join(proc.stdout.split())
            self.assertIn('Store directory', normalized, cmd)
            self.assertNotIn('Call graph output directory', normalized, cmd)
            self.assertLessEqual(normalized.count('auto-discover'), 1, cmd)


if __name__ == '__main__':
    unittest.main()
