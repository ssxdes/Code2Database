"""Unit tests for transactions WAL + snapshot management APIs.

Covers expected inputs/outputs for:
- append_wal_entry: monotonic seq, persisted fields, seq-counter crash
  safety (counter written before WAL append → a lost WAL line never
  causes seq reuse)
- read_wal: full + only_unapplied reads, corrupt-line tolerance,
  missing WAL → []
- mark_wal_entry_applied: single + batch form, corrupt lines preserved
  through rewrite, missing WAL → no-op
- clear_wal: removes WAL + resets seq counter to 1
- delete_snapshot: existing → True/dir gone, missing → False
- prune_snapshots: keeps newest N by id ordering, returns delete count,
  below-threshold → 0
- cmd_tx_restore: --id required, valid snapshot restores db content,
  unknown id reports restored=False
- cmd_tx_list_snapshots: id/timestamp/description/file_count/total_size,
  count, --limit
"""
import argparse
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.ops import transactions

from _builder.ops.transactions import (
    append_wal_entry, read_wal, mark_wal_entry_applied, clear_wal,
    delete_snapshot, prune_snapshots, create_snapshot, list_snapshots,
    cmd_tx_restore, cmd_tx_list_snapshots, _wal_path, _read_wal_seq_counter,
)


def _ns(**kw):
    return argparse.Namespace(**kw)


def _capture_call(fn, args):
    out, err = io.StringIO(), io.StringIO()
    ret, code = None, None
    with redirect_stdout(out), redirect_stderr(err):
        try:
            ret = fn(args)
        except SystemExit as e:
            code = e.code
    return ret, out.getvalue(), err.getvalue(), code


def _make_graph_dir():
    d = tempfile.mkdtemp(prefix="c2d_txwal_")
    db = os.path.join(d, "code2database.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    return d


class TestStaleActiveRollbackFailure(unittest.TestCase):
    """S2 (2026-09-07 review): when a stale active tx cannot be rolled
    back (corrupt/missing snapshot), tx-begin used to clear the WAL
    anyway and proceed with a NEW transaction — destroying the only
    recovery path for the uncommitted writes and orphaning the old tx.

    Correct semantics: abort the new tx-begin, keep the WAL, keep the
    old tx active, and tell the user how to recover."""

    def _stale_dir(self, snapshot_exists=False):
        d = _make_graph_dir()
        snap = create_snapshot(d, "stale tx snapshot")
        if not snapshot_exists:
            # Make restore fail: point the stale tx at a missing snapshot
            import shutil
            shutil.rmtree(os.path.join(d, ".code2database_tx",
                                       "snapshots", snap.id),
                          ignore_errors=True)
        state = transactions.TransactionState(
            tx_id="tx_stale", started_at=0.0, description="stale",
            snapshot_id=snap.id, status="active")
        transactions._write_tx_state(d, state)
        append_wal_entry(d, "update", "n1", {"attr": "x"})
        return d

    def test_cmd_tx_begin_aborts_when_rollback_fails(self):
        from _builder.ops.transactions import cmd_tx_begin
        d = self._stale_dir(snapshot_exists=False)
        _ret, _out, err, code = _capture_call(
            cmd_tx_begin, _ns(graph=d, description="new",
                              file_id=None))
        self.assertEqual(code, 1, "must abort, not proceed")
        self.assertIn("tx-replay-wal", err)
        # WAL must survive for tx-replay-wal recovery
        self.assertTrue(os.path.exists(_wal_path(d)),
                        "WAL was destroyed on rollback failure")
        self.assertGreater(len(read_wal(d)), 0)
        # old tx must still be the active one
        state = transactions._read_tx_state(d)
        self.assertEqual(state.tx_id, "tx_stale")
        self.assertEqual(state.status, "active")

    def test_transaction_ctx_raises_when_rollback_fails(self):
        d = self._stale_dir(snapshot_exists=False)
        with self.assertRaises(RuntimeError):
            with transactions.transaction(d, description="new"):
                pass  # pragma: no cover — begin must abort before yield
        self.assertTrue(os.path.exists(_wal_path(d)),
                        "WAL was destroyed on rollback failure")
        state = transactions._read_tx_state(d)
        self.assertEqual(state.tx_id, "tx_stale")

    def test_stale_rollback_success_still_begins_new_tx(self):
        from _builder.ops.transactions import cmd_tx_begin
        d = self._stale_dir(snapshot_exists=True)
        _ret, out, _err, code = _capture_call(
            cmd_tx_begin, _ns(graph=d, description="new", file_id=None))
        self.assertIsNone(code)
        state = transactions._read_tx_state(d)
        self.assertEqual(state.status, "active")
        self.assertNotEqual(state.tx_id, "tx_stale")
        self.assertEqual(len(read_wal(d)), 0, "WAL cleared after clean rollback")


class TestAppendWALEntry(unittest.TestCase):
    def test_seq_monotonic_and_fields_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            s1 = append_wal_entry(d, "update_node", "n1", {"k": "v"})
            s2 = append_wal_entry(d, "update_edge", "e1", {"w": 2})
            s3 = append_wal_entry(d, "delete_node", "n2", {})
            self.assertEqual((s1, s2, s3), (1, 2, 3))
            entries = read_wal(d)
            self.assertEqual(len(entries), 3)
            self.assertEqual(entries[0]["operation"], "update_node")
            self.assertEqual(entries[0]["target_id"], "n1")
            self.assertEqual(entries[0]["payload"], {"k": "v"})
            self.assertFalse(entries[0]["applied"])
            self.assertIn("timestamp", entries[0])

    def test_seq_counter_survives_lost_wal_line(self):
        """Crash-safety contract: the counter is written BEFORE the WAL
        append. If the WAL loses its last line (simulated crash), the
        next append must NOT reuse the lost seq number."""
        with tempfile.TemporaryDirectory() as d:
            append_wal_entry(d, "op1", "t1", {})
            append_wal_entry(d, "op2", "t2", {})
            # simulate crash after counter write, before/incomplete WAL append:
            with open(_wal_path(d), "r", encoding="utf-8") as f:
                lines = f.readlines()
            with open(_wal_path(d), "w", encoding="utf-8") as f:
                f.writelines(lines[:-1])  # drop the last WAL line
            self.assertEqual(len(read_wal(d)), 1)
            s3 = append_wal_entry(d, "op3", "t3", {})
            self.assertEqual(s3, 3)  # NOT 2 — seq 2 was consumed
            seqs = [e["seq"] for e in read_wal(d)]
            self.assertEqual(seqs, [1, 3])
            self.assertEqual(len(set(seqs)), len(seqs))  # no duplicates


class TestReadWAL(unittest.TestCase):
    def test_missing_wal_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(read_wal(d), [])

    def test_only_unapplied_filters_applied(self):
        with tempfile.TemporaryDirectory() as d:
            append_wal_entry(d, "op", "t1", {})
            append_wal_entry(d, "op", "t2", {})
            mark_wal_entry_applied(d, 1)
            all_entries = read_wal(d)
            self.assertEqual(len(all_entries), 2)
            unapplied = read_wal(d, only_unapplied=True)
            self.assertEqual([e["seq"] for e in unapplied], [2])

    def test_corrupt_lines_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            append_wal_entry(d, "op", "t1", {})
            with open(_wal_path(d), "a", encoding="utf-8") as f:
                f.write("NOT JSON {{{\n")
            append_wal_entry(d, "op", "t2", {})
            entries = read_wal(d)
            self.assertEqual([e["seq"] for e in entries], [1, 2])


class TestMarkWALEntryApplied(unittest.TestCase):
    def test_single_seq(self):
        with tempfile.TemporaryDirectory() as d:
            append_wal_entry(d, "op", "t1", {})
            append_wal_entry(d, "op", "t2", {})
            mark_wal_entry_applied(d, 1)
            entries = read_wal(d)
            self.assertTrue(entries[0]["applied"])
            self.assertFalse(entries[1]["applied"])

    def test_batch_form_single_rewrite(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(5):
                append_wal_entry(d, "op", f"t{i}", {})
            mark_wal_entry_applied(d, [1, 2, 4])
            applied = {e["seq"] for e in read_wal(d) if e["applied"]}
            self.assertEqual(applied, {1, 2, 4})

    def test_corrupt_line_preserved_through_rewrite(self):
        with tempfile.TemporaryDirectory() as d:
            append_wal_entry(d, "op", "t1", {})
            with open(_wal_path(d), "a", encoding="utf-8") as f:
                f.write("CORRUPT { \n")
            append_wal_entry(d, "op", "t2", {})
            mark_wal_entry_applied(d, 2)
            with open(_wal_path(d), "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("CORRUPT {", content)  # not dropped by rewrite
            self.assertEqual(read_wal(d)[1]["seq"], 2)
            self.assertTrue(read_wal(d)[1]["applied"])

    def test_missing_wal_noop(self):
        with tempfile.TemporaryDirectory() as d:
            mark_wal_entry_applied(d, 1)  # must not raise
            self.assertFalse(os.path.exists(_wal_path(d)))


class TestClearWAL(unittest.TestCase):
    def test_clears_file_and_resets_seq(self):
        with tempfile.TemporaryDirectory() as d:
            append_wal_entry(d, "op", "t1", {})
            append_wal_entry(d, "op", "t2", {})
            clear_wal(d)
            self.assertFalse(os.path.exists(_wal_path(d)))
            self.assertEqual(read_wal(d), [])
            # next transaction starts over at seq 1
            self.assertEqual(append_wal_entry(d, "op", "t3", {}), 1)

    def test_clear_on_empty_state_noop(self):
        with tempfile.TemporaryDirectory() as d:
            clear_wal(d)  # must not raise


class TestDeleteSnapshot(unittest.TestCase):
    def test_delete_existing(self):
        with tempfile.TemporaryDirectory() as d:
            snap = create_snapshot(d, "first")
            self.assertTrue(delete_snapshot(d, snap.id))
            self.assertFalse(os.path.exists(snap.path))

    def test_delete_missing_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(delete_snapshot(d, "snap_nonexistent"))


class TestPruneSnapshots(unittest.TestCase):
    def test_keeps_newest_n(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(5):
                create_snapshot(d, f"snap-{i}")
            deleted = prune_snapshots(d, keep=2)
            self.assertEqual(deleted, 3)
            remaining = [s.description for s in list_snapshots(d)]
            # newest-first ordering: keep the two most recent
            self.assertEqual(remaining, ["snap-4", "snap-3"])

    def test_below_threshold_deletes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            create_snapshot(d, "only-one")
            self.assertEqual(prune_snapshots(d, keep=10), 0)
            self.assertEqual(len(list_snapshots(d)), 1)


class TestCmdTxRestore(unittest.TestCase):
    def _setup(self):
        d = _make_graph_dir()
        conn = sqlite3.connect(os.path.join(d, "code2database.db"))
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()
        snap = create_snapshot(d, "pre-change")
        # mutate the db after the snapshot
        conn = sqlite3.connect(os.path.join(d, "code2database.db"))
        conn.execute("INSERT INTO t VALUES (999)")
        conn.commit()
        conn.close()
        return d, snap

    def test_restore_rolls_back_post_snapshot_writes(self):
        d, snap = self._setup()
        ret, out, err, code = _capture_call(
            cmd_tx_restore, _ns(graph=d, id=snap.id))
        self.assertIsNone(code)
        result = json.loads(out)
        self.assertTrue(result["restored"])
        self.assertEqual(result["snapshot_id"], snap.id)
        conn = sqlite3.connect(os.path.join(d, "code2database.db"))
        rows = [r[0] for r in conn.execute("SELECT x FROM t")]
        conn.close()
        self.assertEqual(rows, [1])  # 999 rolled back

    def test_missing_id_exits_1(self):
        with tempfile.TemporaryDirectory() as d:
            ret, out, err, code = _capture_call(
                cmd_tx_restore, _ns(graph=d, id=""))
            self.assertEqual(code, 1)
            self.assertIn("--id required", err)

    def test_unknown_snapshot_reports_not_restored(self):
        d, _ = self._setup()
        ret, out, err, code = _capture_call(
            cmd_tx_restore, _ns(graph=d, id="snap_bogus"))
        self.assertIsNone(code)  # graceful JSON result, not an exception
        result = json.loads(out)
        self.assertFalse(result["restored"])
        self.assertIn("not found", result["reason"])


class TestCmdTxListSnapshots(unittest.TestCase):
    def test_lists_snapshot_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            create_snapshot(d, "alpha")
            create_snapshot(d, "beta")
            ret, out, err, code = _capture_call(
                cmd_tx_list_snapshots, _ns(graph=d, limit=50))
            self.assertIsNone(code)
            result = json.loads(out)
            self.assertEqual(result["count"], 2)
            descs = [s["description"] for s in result["snapshots"]]
            self.assertEqual(descs, ["beta", "alpha"])  # newest first
            for s in result["snapshots"]:
                self.assertIn("id", s)
                self.assertIn("timestamp", s)
                self.assertIn("file_count", s)
                self.assertIn("total_size", s)

    def test_limit_truncates(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(4):
                create_snapshot(d, f"s{i}")
            ret, out, _, _ = _capture_call(
                cmd_tx_list_snapshots, _ns(graph=d, limit=2))
            result = json.loads(out)
            self.assertEqual(result["count"], 2)

    def test_no_snapshots_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            ret, out, _, _ = _capture_call(
                cmd_tx_list_snapshots, _ns(graph=d, limit=50))
            result = json.loads(out)
            self.assertEqual(result["count"], 0)
            self.assertEqual(result["snapshots"], [])


class TestTransactionCommitOrdering(unittest.TestCase):
    """The commit record must be durable before the WAL is destroyed.

    recover_unfinished_wal() treats status "active" as a crash and
    restores the snapshot. If transaction() cleared the WAL before
    persisting status="committed", a crash in between would discard
    writes that were already applied to the live db.
    """

    def test_committed_status_persisted_before_wal_clear(self):
        events = []
        real_write = transactions._write_tx_state
        real_clear = transactions.clear_wal

        def spy_write(graph_dir, state, *a, **kw):
            events.append(("write", state.status))
            return real_write(graph_dir, state, *a, **kw)

        def spy_clear(graph_dir, *a, **kw):
            events.append(("clear_wal", None))
            return real_clear(graph_dir, *a, **kw)

        with tempfile.TemporaryDirectory() as d:
            with unittest.mock.patch.object(transactions, "_write_tx_state", spy_write), \
                 unittest.mock.patch.object(transactions, "clear_wal", spy_clear):
                with transactions.transaction(d, description="ordering"):
                    pass
        writes = [s for kind, s in events if kind == "write"]
        self.assertIn("committed", writes)
        commit_idx = events.index(("write", "committed"))
        clear_indices = [i for i, (kind, _) in enumerate(events)
                         if kind == "clear_wal"]
        self.assertTrue(clear_indices,
                        "WAL was never cleared after commit")
        post_commit_clears = [i for i in clear_indices if i > commit_idx]
        self.assertTrue(post_commit_clears,
                        "clear_wal ran before the committed status write")

    def test_crash_at_wal_clear_keeps_committed_record_and_wal(self):
        """An interruption at the WAL-clear step must not lose the commit.

        With the durable-first ordering the status file already says
        "committed" when clear_wal is interrupted, so recovery takes the
        committed branch (no snapshot rollback of applied writes) and the
        WAL survives as evidence for tx-replay-wal.
        """
        with tempfile.TemporaryDirectory() as d:
            def crash_on_clear(graph_dir, *a, **kw):
                raise KeyboardInterrupt("simulated crash at commit point")

            with unittest.mock.patch.object(transactions, "clear_wal",
                                            crash_on_clear):
                with self.assertRaises(KeyboardInterrupt):
                    with transactions.transaction(d, description="crash"):
                        pass
            state = transactions._read_tx_state(d)
            self.assertIsNotNone(state)
            self.assertEqual(state.status, "committed",
                             "commit record was not durable before the "
                             "WAL-clear step")
            self.assertTrue(os.path.exists(_wal_path(d)),
                            "WAL evidence was destroyed by the interrupted "
                            "commit — tx-replay-wal recovery impossible")


if __name__ == "__main__":
    unittest.main()
