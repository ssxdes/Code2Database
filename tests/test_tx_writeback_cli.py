"""Smoke tests for transaction CLI write-back lifecycle.

Verifies the tx-begin + tx-commit / tx-rollback CLI command handlers can be
invoked end-to-end against a temp graph_dir. The "file-id" tracking (via
mark_file_dirty) is exercised together with the begin/commit cycle to
validate the full write-back loop closure (per AGENTS.md "Transactional
writes" claim).
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from argparse import Namespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.cgdb_schema import apply_cgdb_schema
from _builder.transactions import (
    cmd_tx_begin,
    cmd_tx_commit,
    cmd_tx_rollback,
    cmd_tx_status,
    cmd_tx_snapshot,
    cmd_tx_list_snapshots,
    create_snapshot,
    list_snapshots,
    mark_file_dirty,
    _read_tx_state,
    _write_tx_state,
    _tx_dir,
    _snapshots_dir,
    _wal_path,
    TransactionState,
    transaction,
)


class TestTransactionsModuleImport(unittest.TestCase):
    """Verify the module imports cleanly."""

    def test_module_imports_cleanly(self):
        import _builder.transactions as tx
        for fn in ['cmd_tx_begin', 'cmd_tx_commit', 'cmd_tx_rollback',
                   'cmd_tx_status', 'cmd_tx_snapshot', 'cmd_tx_list_snapshots',
                   'cmd_tx_replay_wal', 'mark_file_dirty', 'create_snapshot',
                   'transaction']:
            self.assertTrue(hasattr(tx, fn), f'missing: {fn}')

    def test_transaction_state_dataclass(self):
        st = TransactionState(tx_id='tx_1', started_at=0.0,
                              description='unit', snapshot_id='snap_1',
                              status='active')
        self.assertEqual(st.status, 'active')
        self.assertEqual(st.dirty_file_ids, [])


class TestTxBegiNoDbGraceful(unittest.TestCase):
    """tx-begin works even without a code2database.db present."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_tx_begin_creates_snapshot_dir(self):
        args = Namespace(graph=self.tmpdir, description='unit test')
        # Suppress stdout from cmd_tx_begin
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_tx_begin(args)
        # tx_state.json should be written
        state_path = os.path.join(_tx_dir(self.tmpdir), 'tx_state.json')
        self.assertTrue(os.path.exists(state_path))
        state = _read_tx_state(self.tmpdir)
        self.assertIsNotNone(state)
        self.assertEqual(state.status, 'active')

    def test_tx_commit_clears_active_state(self):
        # First begin
        cmd_tx_begin(Namespace(graph=self.tmpdir, description='loop test'))
        # Then commit
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            cmd_tx_commit(Namespace(graph=self.tmpdir))
        state = _read_tx_state(self.tmpdir)
        self.assertIsNotNone(state)
        self.assertEqual(state.status, 'committed')


class TestTxBeginWithFileIdTracking(unittest.TestCase):
    """Full loop: tx-begin + mark_file_dirty(file_id) + tx-commit."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        # Create a real code2database.db so the snapshot has something to copy
        db_path = os.path.join(self.tmpdir, 'code2database.db')
        self.conn = sqlite3.connect(db_path)
        apply_cgdb_schema(self.conn)
        # Register a test file row (file_id = 1)
        self.conn.execute(
            "INSERT INTO cgdb_files (path, is_system, language, sha256, "
            "line_count, byte_count, commit_hash) "
            "VALUES ('test.c', 0, 'c', 'abc', 10, 10, 'unit')"
        )
        self.conn.commit()
        self.file_id = 1

    def _cleanup(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_mark_file_dirty_outside_tx_is_noop(self):
        # No active transaction — mark_file_dirty should not crash
        mark_file_dirty(self.tmpdir, self.file_id)
        # No tx_state.json should exist
        state = _read_tx_state(self.tmpdir)
        self.assertIsNone(state)

    def test_full_loop_begin_mark_commit(self):
        from io import StringIO
        from contextlib import redirect_stdout
        with redirect_stdout(StringIO()):
            cmd_tx_begin(Namespace(graph=self.tmpdir,
                                   description='loop with file_id'))
        # Active tx now — mark file dirty
        mark_file_dirty(self.tmpdir, self.file_id)
        state = _read_tx_state(self.tmpdir)
        self.assertEqual(state.status, 'active')
        self.assertIn(self.file_id, state.dirty_file_ids)
        # Commit
        with redirect_stdout(StringIO()):
            cmd_tx_commit(Namespace(graph=self.tmpdir))
        state = _read_tx_state(self.tmpdir)
        self.assertEqual(state.status, 'committed')

    def test_full_loop_begin_rollback(self):
        from io import StringIO
        from contextlib import redirect_stdout
        with redirect_stdout(StringIO()):
            cmd_tx_begin(Namespace(graph=self.tmpdir, description='rollback loop'))
            cmd_tx_rollback(Namespace(graph=self.tmpdir))
        state = _read_tx_state(self.tmpdir)
        self.assertEqual(state.status, 'rolled_back')


class TestTxSnapshotAndList(unittest.TestCase):
    """tx-snapshot + tx-list-snapshots smoke test."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        # Seed with a small db so snapshot has bytes to copy
        db_path = os.path.join(self.tmpdir, 'code2database.db')
        conn = sqlite3.connect(db_path)
        apply_cgdb_schema(conn)
        conn.close()

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_tx_snapshot_creates_snapshot(self):
        from io import StringIO
        from contextlib import redirect_stdout
        with redirect_stdout(StringIO()):
            cmd_tx_snapshot(Namespace(graph=self.tmpdir,
                                      description='manual snap'))
        snaps = list_snapshots(self.tmpdir)
        self.assertGreaterEqual(len(snaps), 1)
        self.assertEqual(snaps[0].description, 'manual snap')

    def test_tx_list_snapshots_prints_json(self):
        from io import StringIO
        from contextlib import redirect_stdout
        # Take a snapshot first
        cmd_tx_snapshot(Namespace(graph=self.tmpdir, description='for-list'))
        buf = StringIO()
        with redirect_stdout(buf):
            cmd_tx_list_snapshots(Namespace(graph=self.tmpdir, limit=10))
        out = buf.getvalue().strip()
        # Should be valid JSON
        data = json.loads(out)
        self.assertIn('snapshots', data)
        self.assertGreaterEqual(len(data['snapshots']), 1)


class TestTxStatus(unittest.TestCase):
    """tx-status reports the current transaction state."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_tx_status_no_active_tx(self):
        from io import StringIO
        from contextlib import redirect_stdout
        buf = StringIO()
        with redirect_stdout(buf):
            cmd_tx_status(Namespace(graph=self.tmpdir))
        out = buf.getvalue().strip()
        data = json.loads(out)
        self.assertIsNone(data['current_tx'])


class TestTransactionContextManager(unittest.TestCase):
    """The transaction() context manager commits on normal exit."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_context_manager_commits_on_exit(self):
        with transaction(self.tmpdir, description='ctx mgr') as state:
            self.assertEqual(state.status, 'active')
        state = _read_tx_state(self.tmpdir)
        self.assertIsNotNone(state)
        self.assertEqual(state.status, 'committed')

    def test_context_manager_rolls_back_on_exception(self):
        with self.assertRaises(RuntimeError):
            with transaction(self.tmpdir, description='ctx mgr rollback'):
                raise RuntimeError('boom')
        state = _read_tx_state(self.tmpdir)
        self.assertIsNotNone(state)
        self.assertEqual(state.status, 'rolled_back')


if __name__ == '__main__':
    unittest.main()
