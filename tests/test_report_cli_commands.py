"""Smoke tests for the 13 report-layer CLI commands.

Verifies that all 13 cmd_* handlers in _builder.cmd_report_tools are
importable and callable, and that they are registered in the
code2database_builder CLI parser. Smoke-test level — no DB setup, just
import-time and registration checks.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

import _builder.cmd_report_tools as cr  # noqa: E402
import code2database_builder as cb       # noqa: E402


# (cli_name, module_attr name)
_REPORT_CLI_COMMANDS = [
    ('render-source',           'cmd_render_source'),
    ('verify-consistency',      'cmd_verify_consistency'),
    ('edit-token',              'cmd_edit_token'),
    ('insert-token',            'cmd_insert_token'),
    ('delete-token',            'cmd_delete_token'),
    ('find-macros',             'cmd_find_macros'),
    ('get-pp-branches',         'cmd_get_pp_branches'),
    ('get-string-literals',     'cmd_get_string_literals'),
    ('commit-db-transaction',   'cmd_commit_db_transaction'),
    ('rollback-db-transaction', 'cmd_rollback_db_transaction'),
    ('insert-node-after',       'cmd_insert_node_after'),
    ('delete-node',             'cmd_delete_node'),
    ('add-function',            'cmd_add_function'),
]


class TestReportCliImport(unittest.TestCase):
    """Verify the report-tools module imports cleanly."""

    def test_module_imports_cleanly(self):
        self.assertTrue(hasattr(cr, '_print_json'))
        for cli_name, attr in _REPORT_CLI_COMMANDS:
            self.assertTrue(hasattr(cr, attr),
                            f'missing attr: {attr}')
            self.assertTrue(callable(getattr(cr, attr)),
                            f'not callable: {attr}')

    def test_exactly_thirteen_report_commands(self):
        self.assertEqual(len(_REPORT_CLI_COMMANDS), 13)


class TestReportCliRegistration(unittest.TestCase):
    """Verify each report command is registered in code2database_builder."""

    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS_DIR, 'code2database_builder.py'),
             '--help'],
            capture_output=True, text=True, timeout=30,
        )
        cls.help_output = proc.stdout

    def test_each_report_command_listed_in_help(self):
        for cli_name, _ in _REPORT_CLI_COMMANDS:
            self.assertIn(cli_name, self.help_output,
                          f'command not registered: {cli_name}')

    def test_each_command_mapped_in_main_commands_dict(self):
        # Reconstruct the commands dict by inspecting the source — we can't
        # call main() without argv, but we can verify each handler is the
        # same callable as the registered one via the builder module's
        # imports (cmd_report_tools is imported at top of builder).
        for cli_name, attr in _REPORT_CLI_COMMANDS:
            registered = getattr(cb, attr, None)
            imported = getattr(cr, attr, None)
            self.assertIsNotNone(registered,
                                 f'builder missing {attr} (cmd: {cli_name})')
            self.assertIs(registered, imported,
                          f'builder.{attr} != cmd_report_tools.{attr}')


class TestReportCliNoDbGraceful(unittest.TestCase):
    """Run report commands against an empty graph_dir; ensure no traceback."""

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

    def test_render_source_no_db_no_traceback(self):
        proc = self._run('render-source', ['--file-id', '1'])
        self.assertNotEqual(proc.returncode, -11)
        self.assertNotIn('Traceback (most recent call last)', proc.stderr)

    def test_verify_consistency_no_db_no_traceback(self):
        proc = self._run('verify-consistency', ['--file-id', '1'])
        self.assertNotEqual(proc.returncode, -11)
        self.assertNotIn('Traceback (most recent call last)', proc.stderr)

    def test_find_macros_no_db_no_traceback(self):
        proc = self._run('find-macros', ['--name', 'FOO'])
        self.assertNotEqual(proc.returncode, -11)
        self.assertNotIn('Traceback (most recent call last)', proc.stderr)

    def test_get_pp_branches_no_db_no_traceback(self):
        proc = self._run('get-pp-branches', ['--file-id', '1'])
        self.assertNotEqual(proc.returncode, -11)
        self.assertNotIn('Traceback (most recent call last)', proc.stderr)

    def test_get_string_literals_no_db_no_traceback(self):
        proc = self._run('get-string-literals', ['--pattern', 'foo'])
        self.assertNotEqual(proc.returncode, -11)
        self.assertNotIn('Traceback (most recent call last)', proc.stderr)

    def test_rollback_db_transaction_no_db_no_traceback(self):
        # commit-db-transaction requires --transaction-id; rollback also
        proc = self._run('rollback-db-transaction',
                         ['--transaction-id', 'tx_test_001'])
        self.assertNotEqual(proc.returncode, -11)
        self.assertNotIn('Traceback (most recent call last)', proc.stderr)

    def test_delete_node_no_db_no_traceback(self):
        proc = self._run('delete-node', ['--ast-node-id', '1'])
        self.assertNotEqual(proc.returncode, -11)
        self.assertNotIn('Traceback (most recent call last)', proc.stderr)

    def test_add_function_no_db_no_traceback(self):
        proc = self._run('add-function', ['--signature', 'int foo(void)'])
        self.assertNotEqual(proc.returncode, -11)
        self.assertNotIn('Traceback (most recent call last)', proc.stderr)


if __name__ == '__main__':
    unittest.main()
