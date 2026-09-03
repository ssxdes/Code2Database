"""Regression tests for transaction snapshot/restore WAL integrity.

Pins three data-corruption paths fixed in this round:
  1. create_snapshot copied only code2database.db — with the db in WAL
     mode every commit still living in the -wal file was silently lost
     (reproduced pre-fix: snapshot db had neither table nor rows).
     Now the snapshot checkpoints the WAL first and falls back to
     copying the -wal sidecar when a concurrent reader pins the log.
  2. restore_snapshot did not restore the snapshotted -wal sidecar
     (snapshots that carried one restored a torn db).
  3. restore_snapshot swallowed live-sidecar deletion failures
     (except OSError: pass) — a surviving live -wal would be replayed
     by SQLite onto the restored older db, the exact corrupt-hybrid
     state the restore is meant to prevent. Now restored=False + reason.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.transactions import create_snapshot, restore_snapshot


def _wal_db(d):
    db = os.path.join(d, "code2database.db")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")  # keep frames in the WAL
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    for i in range(300):
        conn.execute("INSERT INTO t VALUES (?)", (i,))
    conn.commit()
    return conn


class TestSnapshotWalIntegrity(unittest.TestCase):
    def test_snapshot_captures_wal_resident_commits(self):
        """With a concurrent reader pinning the WAL (checkpoint busy),
        the snapshot must carry the -wal sidecar so no committed data
        is lost."""
        with tempfile.TemporaryDirectory() as d:
            conn = _wal_db(d)
            reader = sqlite3.connect(os.path.join(d, "code2database.db"))
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM t").fetchone()
            try:
                snap = create_snapshot(d)
                sidecars = [f for f in os.listdir(snap.path)
                            if f.startswith("code2database.db-")]
                self.assertIn(
                    "code2database.db-wal", sidecars,
                    "snapshot must carry the -wal when checkpoint is busy")
            finally:
                reader.execute("COMMIT")
                reader.close()

    def test_snapshot_self_contained_when_idle(self):
        """Without a pinning reader the checkpoint folds the WAL in and
        the snapshot db is readable standalone."""
        with tempfile.TemporaryDirectory() as d:
            conn = _wal_db(d)
            try:
                snap = create_snapshot(d)
                sc = sqlite3.connect(
                    os.path.join(snap.path, "code2database.db"))
                n = sc.execute("SELECT COUNT(*) FROM t").fetchone()[0]
                sc.close()
                self.assertEqual(n, 300)
            finally:
                conn.close()


class TestRestoreWalIntegrity(unittest.TestCase):
    def test_restore_returns_exact_snapshot_state(self):
        """Commits made AFTER the snapshot are discarded; commits made
        BEFORE (WAL-resident at snapshot time) survive the restore."""
        with tempfile.TemporaryDirectory() as d:
            conn = _wal_db(d)
            reader = sqlite3.connect(os.path.join(d, "code2database.db"))
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM t").fetchone()
            snap = create_snapshot(d)
            reader.execute("COMMIT")
            reader.close()
            # post-snapshot commits that a rollback must discard
            for i in range(300, 450):
                conn.execute("INSERT INTO t VALUES (?)", (i,))
            conn.commit()
            res = restore_snapshot(d, snap.id)
            self.assertTrue(res["restored"], res)
            c2 = sqlite3.connect(os.path.join(d, "code2database.db"))
            n = c2.execute("SELECT COUNT(*) FROM t").fetchone()[0]
            c2.close()
            conn.close()
            self.assertEqual(n, 300)

    def test_restore_surfaces_sidecar_delete_failure(self):
        """A live -wal that cannot be deleted must NOT be reported as a
        successful restore — SQLite would replay it onto the restored
        older db (corrupt hybrid)."""
        with tempfile.TemporaryDirectory() as d:
            conn = _wal_db(d)
            snap = create_snapshot(d)
            conn.close()
            # Sabotage: replace the live -wal with a directory so
            # os.remove raises OSError (undelectable).
            wal = os.path.join(d, "code2database.db-wal")
            if os.path.exists(wal):
                os.remove(wal)
            os.makedirs(wal)
            try:
                res = restore_snapshot(d, snap.id)
                self.assertFalse(res["restored"], res)
                self.assertIn("code2database.db-wal", res.get("reason", ""))
            finally:
                os.rmdir(wal)


class TestGraphLockReentrancy(unittest.TestCase):
    """Same-thread nested lock acquisition used to deadlock.

    flock locks are per open-file-description: a second GraphLock from
    the same process opened a NEW fd and blocked against itself for the
    full timeout (60s in transaction()), then raised TimeoutError —
    `with transaction(...)` code that internally called write_lock() on
    the same graph_dir was unusable.
    """

    def test_nested_same_thread_locks(self):
        import time
        from _builder.transactions import write_lock, read_lock
        with tempfile.TemporaryDirectory() as d:
            t0 = time.time()
            with write_lock(d, timeout=5.0):
                with write_lock(d, timeout=5.0):
                    with read_lock(d, timeout=5.0):
                        pass
            self.assertLess(time.time() - t0, 2.0)

    def test_transaction_with_nested_write_lock(self):
        import time
        from _builder.transactions import transaction, write_lock
        with tempfile.TemporaryDirectory() as d:
            t0 = time.time()
            try:
                with transaction(d, description="outer"):
                    with write_lock(d, timeout=5.0):
                        pass
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            self.assertLess(time.time() - t0, 5.0)

    def test_cross_thread_still_blocks(self):
        import threading
        import time
        from _builder.transactions import GraphLock
        with tempfile.TemporaryDirectory() as d:
            outer = GraphLock(d)
            self.assertTrue(outer.acquire_write(timeout=5.0))
            res = {}

            def other():
                l2 = GraphLock(d)
                t0 = time.time()
                res["ok"] = l2.acquire_write(timeout=1.0)
                res["elapsed"] = time.time() - t0

            t = threading.Thread(target=other)
            t.start()
            t.join()
            self.assertFalse(res["ok"], "cross-thread lock must block")
            self.assertGreaterEqual(res["elapsed"], 0.9)
            outer.release()
            # and becomes acquirable after release
            l3 = GraphLock(d)
            self.assertTrue(l3.acquire_write(timeout=2.0))
            l3.release()

    def test_sequential_cycles(self):
        from _builder.transactions import write_lock
        with tempfile.TemporaryDirectory() as d:
            for _ in range(3):
                with write_lock(d, timeout=2.0):
                    pass


if __name__ == "__main__":
    unittest.main()
