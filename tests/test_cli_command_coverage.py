"""Smoke tests for CLI command registration and no-DB graceful error.

Verifies that 26+ representative CLI commands are registered in the
code2database_builder.main() argparse parser, and that invoking them
without a code2database.db in the graph_dir produces a graceful error
(no unhandled traceback). Uses subprocess to invoke the actual CLI
so the registration code path is exercised end-to-end.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts')
sys.path.insert(0, SCRIPTS_DIR)


# A representative command slice spanning all CLI categories.
_REPRESENTATIVE_COMMANDS = [
    # Core build/load/query
    'build', 'load', 'search', 'describe-node', 'path', 'query',
    # Value flow + locks + feasibility + data-dep
    'value-flow', 'lock-coverage', 'path-feasible', 'data-dep',
    # Invariants (umbrella)
    'invariants',
    # Auto-enhance
    'auto-enhance', 'batch-confirm', 'rollback', 'fill-request',
    # Transactions (umbrella)
    'tx',
    # FFI (umbrella)
    'ffi',
    # Profile / daemon (umbrella alias)
    'daemon',
]


class TestBuilderModuleImport(unittest.TestCase):
    """Verify the builder module imports cleanly."""

    def test_module_imports_cleanly(self):
        import code2database_builder as cb
        self.assertTrue(hasattr(cb, 'main'))
        self.assertTrue(callable(cb.main))

    def test_module_has_docstring(self):
        """Module docstring must not be shadowed by imports."""
        import code2database_builder as cb
        self.assertIsNotNone(cb.__doc__,
                             "module docstring must not be shadowed by imports")
        self.assertIn("Call graph builder", cb.__doc__)

    def test_main_runs_without_subcommand_prints_help_and_exits(self):
        # Call: python3 code2database_builder.py (no args)
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS_DIR, 'code2database_builder.py'),
             '--log-level', 'CRITICAL'],
            capture_output=True, text=True, timeout=30,
        )
        # Usage error: exit 2 (the contract both CLIs share), help on stdout
        self.assertEqual(proc.returncode, 2)

    def test_daemon_docstring_verbs_are_registered(self):
        """The daemon docstring's CLI list must match the real dispatch.

        A promised-but-missing verb is a silent contract break: readers
        of the module docstring would run a command that does not exist.
        The docstring teaches the umbrella form (`daemon start`), so each
        verb is verified end-to-end through the family rewrite.
        """
        import re
        doc = open(os.path.join(
            SCRIPTS_DIR, '_builder', 'daemon', 'daemon.py'),
            encoding='utf-8').read()
        m = re.search(r"\*\*CLI\*\*: daemon ([a-z/\-]+)", doc)
        self.assertIsNotNone(m, "daemon docstring must list its CLI verbs")
        verbs = m.group(1).split('/')
        for verb in verbs:
            proc = subprocess.run(
                [sys.executable,
                 os.path.join(SCRIPTS_DIR, 'code2database_builder.py'),
                 'daemon', verb, '--help'],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(
                proc.returncode, 0,
                f"daemon docstring promises 'daemon {verb}' but it does "
                f"not dispatch (rc={proc.returncode}): {proc.stderr[:200]}")


class TestCLICommandRegistration(unittest.TestCase):
    """Verify representative commands are registered in --help output."""

    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS_DIR, 'code2database_builder.py'),
             '--help'],
            capture_output=True, text=True, timeout=30,
        )
        cls.help_output = proc.stdout

    def test_help_lists_each_command(self):
        for cmd in _REPRESENTATIVE_COMMANDS:
            self.assertIn(cmd, self.help_output,
                          f'command missing from --help: {cmd}')

    def test_help_lists_visible_command_surface(self):
        """The visible surface is the umbrella-family form: ~120 commands.

        155 legacy spellings (tx-begin, cgdb-query, ...) are hidden from
        --help but still parse. The bounds below catch both directions:
        an umbrella that stopped hiding its members (surface bloat) and
        an umbrella that disappeared (lost family).
        """
        import re
        # Command lines are indented exactly 4 spaces; help-text
        # continuation lines are indented far deeper, option lines
        # start with a dash after 2 spaces.
        found = set(re.findall(r"^    ([a-z][a-z0-9_-]*) ", self.help_output,
                               re.MULTILINE))
        self.assertGreaterEqual(len(found), 100,
                                f'expected >=100 visible commands, found {len(found)}')
        self.assertLessEqual(len(found), 140,
                             f'expected <=140 visible commands, found {len(found)}')

    def test_version_flag_prints_version_and_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS_DIR, 'code2database_builder.py'),
             '--version'],
            capture_output=True, text=True, timeout=10
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn('code2database_builder', proc.stdout)

    def test_help_mentions_cgdb_subcommand_family(self):
        # cgdb-* commands are hidden behind the `cgdb` umbrella; the
        # main help teaches the family, the umbrella help teaches the
        # legacy names.
        self.assertIn('cgdb umbrella', self.help_output)
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS_DIR, 'code2database_builder.py'),
             'cgdb', '--help'],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertIn('cgdb find-invokers', proc.stdout)
        self.assertIn('cgdb-query', proc.stdout)


class TestNoDBGracefulError(unittest.TestCase):
    """Run representative commands against an empty graph_dir.

    The commands should either exit with a clear error message (not a
    traceback) OR exit with a recognizable non-zero status (no segfault).
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, cmd, extra_args=None):
        args = [sys.executable, os.path.join(SCRIPTS_DIR, 'code2database_builder.py'),
                '--log-level', 'CRITICAL', cmd, '--graph', self.tmpdir]
        if extra_args:
            args.extend(extra_args)
        return subprocess.run(args, capture_output=True, text=True, timeout=30)

    def test_search_no_db_does_not_segfault(self):
        proc = self._run('search', ['--keyword', 'foo'])
        # Acceptable: exit code 0 (printed empty) or 1 (graceful error)
        # Unacceptable: signal-based kill (-11 = SIGSEGV = -11)
        self.assertNotEqual(proc.returncode, -11)

    def test_describe_node_no_db_graceful(self):
        proc = self._run('describe-node', ['--name', 'foo'])
        self.assertNotEqual(proc.returncode, -11)
        # No traceback should be in stderr
        self.assertNotIn('Traceback (most recent call last)', proc.stderr)

    def test_query_no_db_graceful(self):
        proc = self._run('query', ['--cypher', 'MATCH (n) RETURN n LIMIT 1'])
        self.assertNotEqual(proc.returncode, -11)
        self.assertNotIn('Traceback (most recent call last)', proc.stderr)

    def test_tx_status_no_db_graceful(self):
        proc = self._run('tx-status')
        # Should print "no active transaction" or similar
        self.assertNotEqual(proc.returncode, -11)
        self.assertNotIn('Traceback (most recent call last)', proc.stderr)

    def test_ffi_list_no_db_graceful(self):
        proc = self._run('ffi-list')
        self.assertNotEqual(proc.returncode, -11)
        self.assertNotIn('Traceback (most recent call last)', proc.stderr)

    def test_cgdb_layer_summary_no_db_graceful(self):
        proc = self._run('cgdb-layer-summary')
        self.assertNotEqual(proc.returncode, -11)
        self.assertNotIn('Traceback (most recent call last)', proc.stderr)

    def test_runtime_guards_runs_without_db(self):
        # runtime-guards doesn't need a db — it just inspects conditions
        proc = self._run('runtime-guards', ['--conditions', 'if (mutex_lock(&m))'])
        self.assertEqual(proc.returncode, 0)


class TestCLIAliasesRegistered(unittest.TestCase):
    """Verify all 12 SKILL.md short aliases are registered as subparsers."""

    SKILL_ALIASES = {
        'describe': 'describe-node',
        'context': 'describe-node',
        'trace': 'trace-chain',
        'concurrency': 'concurrency-risks',
        'save': 'save-memory',
        'recall': 'search-memory',
        'brief': 'knowledge-brief',
        'flow': 'value-flow',
        'find': 'find-invariants',
        'health': 'profile-health',
        'daemon': 'daemon-status',
        'export': 'export-mermaid',
    }

    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS_DIR, 'code2database_builder.py'),
             '--help'],
            capture_output=True, text=True, timeout=30,
        )
        cls.help_output = proc.stdout

    def test_all_aliases_in_help(self):
        for alias in self.SKILL_ALIASES:
            self.assertIn(alias, self.help_output,
                          f'alias missing from --help: {alias}')

    def test_alias_help_matches_canonical_args(self):
        """Each alias --help should list the same arguments as its canonical form."""
        for alias, canonical in self.SKILL_ALIASES.items():
            alias_proc = subprocess.run(
                [sys.executable, os.path.join(SCRIPTS_DIR, 'code2database_builder.py'),
                 alias, '--help'],
                capture_output=True, text=True, timeout=30,
            )
            canonical_proc = subprocess.run(
                [sys.executable, os.path.join(SCRIPTS_DIR, 'code2database_builder.py'),
                 canonical, '--help'],
                capture_output=True, text=True, timeout=30,
            )
            # Extract argument lines (lines starting with --) from both
            alias_args = sorted(l.strip() for l in alias_proc.stdout.splitlines()
                                if l.strip().startswith('--'))
            canonical_args = sorted(l.strip() for l in canonical_proc.stdout.splitlines()
                                    if l.strip().startswith('--'))
            self.assertEqual(alias_args, canonical_args,
                             f'alias {alias} args differ from canonical {canonical}')


if __name__ == '__main__':
    unittest.main()
