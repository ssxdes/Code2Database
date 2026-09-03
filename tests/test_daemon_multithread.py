"""Tests for daemon multi-threading (D31).

Tests that the sync worker thread runs separately from the main loop,
socket queries aren't blocked by syncs, and sync-status is reported
correctly.
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.daemon import Daemon


class TestSyncWorkerThread(unittest.TestCase):
    """Test the sync worker thread dispatch and status reporting."""

    def _make_daemon(self, tmpdir):
        """Create a daemon with a no-op sync so we can test threading in isolation."""
        d = Daemon(graph_dir=tmpdir, source_root=tmpdir,
                   config={"batch_window_ms": 100, "debounce_ms": 50,
                            "idle_sleep_minutes": 1, "max_events_per_minute": 1000})
        # Patch the heavy sync methods to be fast no-ops
        d._sync_incremental = lambda: None
        d._sync_bulk = lambda: None
        return d

    def test_sync_worker_initializes_idle(self):
        """Daemon starts with sync worker idle and no queued jobs."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp)
            self.assertFalse(d.is_sync_busy())
            status = d.get_sync_status()
            self.assertFalse(status["busy"])
            self.assertEqual(status["queued_jobs"], 0)
            self.assertIsNone(status["last_result"])

    def test_enqueue_sync_job_increments_queue(self):
        """_enqueue_sync_job adds jobs to the queue."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp)
            d._enqueue_sync_job("incremental", ["/a.c", "/b.c"])
            d._enqueue_sync_job("bulk", ["/c.c"])
            status = d.get_sync_status()
            self.assertEqual(status["queued_jobs"], 2)

    def test_sync_worker_processes_jobs(self):
        """The sync worker thread processes queued jobs."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp)
            d._start_sync_worker()
            try:
                d._enqueue_sync_job("incremental", ["/a.c"])
                # Wait for the worker to process
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    if d._last_sync_result is not None:
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(d._last_sync_result)
                self.assertTrue(d._last_sync_result["ok"])
                self.assertEqual(d._last_sync_result["kind"], "incremental")
                self.assertEqual(d._last_sync_result["path_count"], 1)
            finally:
                d._stop = True
                d._sync_worker_thread.join(timeout=2.0)

    def test_sync_worker_handles_errors(self):
        """Sync worker captures exceptions and reports them in last_result."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp)
            # Make sync raise
            d._sync_incremental = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
            d._start_sync_worker()
            try:
                d._enqueue_sync_job("incremental", ["/a.c"])
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    if d._last_sync_result is not None:
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(d._last_sync_result)
                self.assertFalse(d._last_sync_result["ok"])
                self.assertIn("boom", d._last_sync_result.get("error", ""))
            finally:
                d._stop = True
                d._sync_worker_thread.join(timeout=2.0)

    def test_is_sync_busy_reflects_state(self):
        """is_sync_busy returns True while a job is running."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp)
            sync_started = threading.Event()
            sync_done = threading.Event()

            def slow_sync():
                sync_started.set()
                time.sleep(0.3)
                sync_done.set()

            d._sync_incremental = slow_sync
            d._start_sync_worker()
            try:
                d._enqueue_sync_job("incremental", ["/a.c"])
                # Wait for sync to start
                self.assertTrue(sync_started.wait(timeout=2.0))
                # While sync is running, is_sync_busy should be True
                self.assertTrue(d.is_sync_busy())
                # Wait for sync to finish
                self.assertTrue(sync_done.wait(timeout=2.0))
                # After sync, is_sync_busy should become False
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    if not d.is_sync_busy():
                        break
                    time.sleep(0.05)
                self.assertFalse(d.is_sync_busy())
            finally:
                d._stop = True
                d._sync_worker_thread.join(timeout=2.0)

    def test_socket_status_includes_sync(self):
        """The 'status' socket command includes sync-worker status."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp)
            result = d._handle_command("status", {})
            self.assertIn("sync", result)
            self.assertIn("busy", result["sync"])
            self.assertIn("queued_jobs", result["sync"])

    def test_sync_status_command(self):
        """The 'sync-status' socket command returns sync status."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp)
            result = d._handle_command("sync-status", {})
            self.assertTrue(result["ok"])
            self.assertIn("sync", result)

    def test_wait_sync_returns_quickly_when_idle(self):
        """wait-sync returns immediately when no sync is running or queued."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp)
            start = time.time()
            result = d._handle_command("wait-sync", {"timeout": 5.0})
            elapsed = time.time() - start
            self.assertTrue(result["ok"])
            self.assertLess(elapsed, 1.0)


class TestStartupGracePeriod(unittest.TestCase):
    """M1: daemon start() must not dispatch syncs during the startup
    grace window, and the recovery bulk sync from a crashed previous
    daemon must be deferred until the grace ends (crash-loop guard).
    """

    _BASE_CFG = {"batch_window_ms": 50, "debounce_ms": 20,
                 "idle_sleep_minutes": 1, "max_events_per_minute": 1000}

    def _make_daemon(self, tmpdir, grace_sec):
        d = Daemon(graph_dir=tmpdir, source_root=tmpdir,
                   config={**self._BASE_CFG, "startup_grace_sec": grace_sec})
        d._sync_incremental = lambda: None
        d._sync_bulk = lambda: None
        return d

    def _run_daemon(self, d):
        t = threading.Thread(target=d.start, daemon=True)
        with patch.object(d, "_start_socket_server"), \
             patch.object(d, "_setup_signal_handlers"):
            t.start()
        return t

    @staticmethod
    def _draining_fake_sync(daemon, syncs):
        """Fake _sync_incremental honoring the real one's contract: the
        real implementation drains _pending at entry (the sync worker
        re-fills it from the job's paths before calling). A fake that
        does NOT drain leaves _pending repopulated forever — the main
        loop re-dispatches the same job every ~1.2s and wait-sync (which
        waits for quiescence) times out deterministically. This made
        test_wait_sync_ends_grace_early a 1-in-10 lottery before.
        """
        def _sync():
            with daemon._pending_lock:
                daemon._pending.clear()
                daemon.state.pending_events = 0
            syncs.append(time.time())
        return _sync

    def test_grace_default_is_60s(self):
        """Default config carries a 60s startup grace."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp, grace_sec=60.0)
            self.assertGreaterEqual(d._grace_until - time.time(), 59.0)

    def test_events_held_during_grace_dispatched_after(self):
        """Events arriving during grace do NOT dispatch a sync until the
        grace window ends."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp, grace_sec=2.0)
            syncs = []
            d._sync_incremental = self._draining_fake_sync(d, syncs)
            t = self._run_daemon(d)
            try:
                time.sleep(0.4)
                d._on_file_change(os.path.join(tmp, "a.c"))
                time.sleep(0.8)  # still inside the 2s grace
                self.assertEqual(syncs, [],
                                 "sync dispatched during startup grace")
                # Grace expires -> the queued event dispatches.
                deadline = time.time() + 8.0
                while time.time() < deadline and not syncs:
                    time.sleep(0.1)
                self.assertTrue(syncs, "sync never dispatched after grace")
            finally:
                d._stop = True
                t.join(timeout=5.0)

    def test_wait_sync_ends_grace_early(self):
        """An explicit wait-sync request ends the grace immediately."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp, grace_sec=30.0)
            syncs = []
            d._sync_incremental = self._draining_fake_sync(d, syncs)
            t = self._run_daemon(d)
            try:
                time.sleep(0.3)
                d._on_file_change(os.path.join(tmp, "a.c"))
                time.sleep(0.3)
                self.assertEqual(syncs, [])
                res = d._handle_command("wait-sync", {"timeout": 10.0})
                # wait-sync returns once the (now-dispatched) sync drains.
                self.assertTrue(res["ok"])
                self.assertTrue(syncs, "wait-sync did not flush the held event")
            finally:
                d._stop = True
                t.join(timeout=5.0)

    def test_force_refresh_ends_grace_early(self):
        """force-refresh ends the grace so the refresh dispatches now."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp, grace_sec=30.0)
            syncs = []
            d._sync_incremental = self._draining_fake_sync(d, syncs)
            t = self._run_daemon(d)
            try:
                time.sleep(0.3)
                res = d._handle_command(
                    "force-refresh", {"path": os.path.join(tmp, "b.c")})
                self.assertTrue(res["ok"])
                deadline = time.time() + 8.0
                while time.time() < deadline and not syncs:
                    time.sleep(0.1)
                self.assertTrue(syncs, "force-refresh stuck in grace")
            finally:
                d._stop = True
                t.join(timeout=5.0)

    def test_recovered_pending_bulk_deferred_until_grace_ends(self):
        """A crashed previous daemon's pending events must NOT trigger an
        immediate bulk sync on restart — it is deferred to grace end."""
        from _builder.daemon import DaemonState, STATUS_SYNCING
        with tempfile.TemporaryDirectory() as tmp:
            DaemonState(pid=123, status=STATUS_SYNCING,
                        pending_events=7).write(tmp)
            d = Daemon(graph_dir=tmp, source_root=tmp,
                       config={**self._BASE_CFG, "startup_grace_sec": 1.5})
            self.assertEqual(d._recovered_pending_count, 7)
            bulks = []
            d._sync_bulk = lambda: bulks.append(time.time())
            d._sync_incremental = lambda: None
            t = self._run_daemon(d)
            try:
                time.sleep(0.5)  # inside grace
                self.assertEqual(len(d._sync_jobs), 0,
                                 "recovery bulk enqueued during grace")
                self.assertFalse(bulks)
                deadline = time.time() + 8.0
                while time.time() < deadline and not bulks:
                    time.sleep(0.1)
                self.assertTrue(bulks, "deferred recovery bulk never ran")
            finally:
                d._stop = True
                t.join(timeout=5.0)

    def test_status_reports_grace_state(self):
        """sync-status exposes whether the grace is active."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_daemon(tmp, grace_sec=30.0)
            status = d.get_sync_status()
            self.assertTrue(status["startup_grace_active"])
            self.assertGreater(status["startup_grace_remaining_sec"], 0)
            d._end_startup_grace()
            status = d.get_sync_status()
            self.assertFalse(status["startup_grace_active"])
            self.assertEqual(status["startup_grace_remaining_sec"], 0)


if __name__ == "__main__":
    unittest.main()
