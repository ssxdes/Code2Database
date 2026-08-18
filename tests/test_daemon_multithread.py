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


if __name__ == "__main__":
    unittest.main()
