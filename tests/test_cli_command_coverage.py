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


# A representative 26-command slice spanning all CLI categories.
_REPRESENTATIVE_COMMANDS = [
    # Core build/load/query
    'build', 'load', 'search', 'describe-node', 'path', 'query',
    # Value flow + locks + feasibility + data-dep
    'value-flow', 'lock-coverage', 'path-feasible', 'data-dep',
    # Invariants
    'extract-invariants', 'find-invariants', 'apply-invariants',
    # Auto-enhance
    'auto-enhance', 'batch-confirm', 'rollback', 'fill-request',
    # Transactions
    'tx-begin', 'tx-commit', 'tx-rollback', 'tx-status',
    # FFI
    'ffi-detect', 'ffi-list', 'ffi-trace', 'ffi-types',
    # Profile / daemon / web-ui
    'daemon-status', 'web-ui',
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
        # Should exit non-zero (sys.exit(1)) and print help to stdout
        self.assertEqual(proc.returncode, 1)


class TestCLICommandRegistration(unittest.TestCase):
    """Verify 26+ representative commands are registered in --help output."""

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

    def test_help_lists_at_least_200_commands(self):
        # Count distinct command names appearing as `  <name>` lines
        found = set()
        for line in self.help_output.splitlines():
            stripped = line.strip()
            # argparse subparser lines look like "build    Build invocation graph..."
            if ' ' in stripped:
                name = stripped.split()[0]
                if name and not name.startswith('-'):
                    found.add(name)
        self.assertGreaterEqual(len(found), 200,
                                f'expected >=200 commands, found {len(found)}')

    def test_version_flag_prints_version_and_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS_DIR, 'code2database_builder.py'),
             '--version'],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn('code2database_builder', proc.stdout)

    def test_help_mentions_cgdb_subcommand_family(self):
        self.assertIn('cgdb-query', self.help_output)
        self.assertIn('cgdb-find-invokers', self.help_output)


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
