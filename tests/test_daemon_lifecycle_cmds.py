"""Tests for the daemon lifecycle CLI commands.

Covers the eight daemon commands that had no test references:

- daemon-stop: no-PID state, recycled-PID state (cleaned, not killed),
  and a real subprocess daemon terminated via SIGTERM
- daemon-pause / daemon-resume: live round-trip over the socket RPC
- daemon-force-refresh: queues a path and ends the startup grace
- daemon-wait-sync: ends grace and reports sync completion
- daemon-logs: missing log file exits 1; existing file path exercised
  (HOME redirected to a sandbox so real user logs are untouched)
- daemon-reload: no-PID error, dead-PID exit
- daemon-list-projects: discovers state/log files under HOME and the
  current graph dir
- daemon-status: not-running state report without a daemon; live
  status via socket with a real in-process daemon

The in-process daemon runs with no-op sync hooks (same pattern as
test_daemon_multithread) so the lifecycle is deterministic. The stop
e2e uses a real builder subprocess so SIGTERM targets a genuine daemon
process, never this test process.
"""
import argparse
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.daemon.daemon import (
    Daemon,
    DaemonState,
    STATUS_RUNNING,
    STATUS_STOPPED,
    _daemon_socket_path,
    daemon_query,
    cmd_daemon_force_refresh,
    cmd_daemon_list_projects,
    cmd_daemon_logs,
    cmd_daemon_pause,
    cmd_daemon_reload,
    cmd_daemon_resume,
    cmd_daemon_status,
    cmd_daemon_stop,
    cmd_daemon_wait_sync,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def _wait_socket(graph_dir, timeout=15.0):
    deadline = time.time() + timeout
    sock_path = _daemon_socket_path(graph_dir)
    while time.time() < deadline:
        if os.path.exists(sock_path):
            # Socket exists — confirm it accepts connections
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(2.0)
                    s.connect(sock_path)
                return True
            except OSError:
                pass
        time.sleep(0.1)
    return False


class TestQueriesWithoutDaemon(unittest.TestCase):
    """Every socket-backed command must degrade cleanly with no daemon."""

    def setUp(self):
        self.graph_dir = tempfile.mkdtemp(prefix="c2d_dmn_none_")
        self.addCleanup(shutil.rmtree, self.graph_dir, ignore_errors=True)

    def test_daemon_query_reports_not_running(self):
        result = daemon_query(self.graph_dir, "status")
        self.assertEqual(result, {"error": "daemon not running"})

    def test_stale_running_state_reports_stale_socket(self):
        DaemonState(pid=12345, status=STATUS_RUNNING).write(self.graph_dir)
        result = daemon_query(self.graph_dir, "status")
        self.assertIn("error", result)
        self.assertIn("stale", result["error"])

    def test_status_prints_not_running_state(self):
        ret, out, err, code = _capture_call(
            cmd_daemon_status, _ns(graph=self.graph_dir))
        self.assertIsNone(code)
        result = json.loads(out)
        self.assertFalse(result["running"])
        self.assertIn("state", result)

    def test_stop_without_pid_exits_1(self):
        ret, out, err, code = _capture_call(
            cmd_daemon_stop, _ns(graph=self.graph_dir))
        self.assertEqual(code, 1)
        self.assertIn("no daemon PID", out)

    def test_stop_with_recycled_pid_clears_state(self):
        # This test process is alive but is NOT a daemon — the PID
        # guard must refuse to signal it and clear the stale state.
        DaemonState(pid=os.getpid(), status=STATUS_RUNNING).write(
            self.graph_dir)
        ret, out, err, code = _capture_call(
            cmd_daemon_stop, _ns(graph=self.graph_dir))
        self.assertEqual(code, 1)
        self.assertIn("state cleared", out)
        state = DaemonState.read(self.graph_dir)
        self.assertEqual(state.pid, 0)
        self.assertEqual(state.status, STATUS_STOPPED)

    def test_socket_commands_report_not_running(self):
        for fn, ns in (
            (cmd_daemon_pause, _ns(graph=self.graph_dir, reason="manual")),
            (cmd_daemon_resume, _ns(graph=self.graph_dir)),
            (cmd_daemon_force_refresh,
             _ns(graph=self.graph_dir, path="/tmp/some_file.c")),
            (cmd_daemon_wait_sync,
             _ns(graph=self.graph_dir, timeout=1.0)),
        ):
            ret, out, err, code = _capture_call(fn, ns)
            self.assertIsNone(code, "%s must not exit on a dead daemon"
                              % fn.__name__)
            result = json.loads(out)
            self.assertEqual(result.get("error"), "daemon not running",
                             "%s wrong error payload" % fn.__name__)

    def test_reload_without_pid_reports_not_running(self):
        ret, out, err, code = _capture_call(
            cmd_daemon_reload, _ns(graph=self.graph_dir))
        self.assertIsNone(code)
        result = json.loads(out)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "daemon not running")

    def test_reload_with_dead_pid_exits_1(self):
        DaemonState(pid=999999, status=STATUS_RUNNING).write(self.graph_dir)
        ret, out, err, code = _capture_call(
            cmd_daemon_reload, _ns(graph=self.graph_dir))
        self.assertEqual(code, 1)
        self.assertIn("error", err)


class TestDaemonLogsAndProjects(unittest.TestCase):
    """daemon-logs / daemon-list-projects read the HOME sandbox."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="c2d_dmn_home_")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        patcher = patch.dict(os.environ, {"HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.graph_dir = tempfile.mkdtemp(prefix="c2d_dmn_logs_")
        self.addCleanup(shutil.rmtree, self.graph_dir, ignore_errors=True)

    def test_logs_missing_file_exits_1(self):
        ret, out, err, code = _capture_call(
            cmd_daemon_logs, _ns(graph=self.graph_dir, follow=False, n=10))
        self.assertEqual(code, 1)
        self.assertIn("No log file", err)

    def test_logs_with_existing_file_runs(self):
        log_dir = os.path.join(self.home, ".callgraph")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir,
                                "daemon-%s.log" % os.path.basename(
                                    self.graph_dir))
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("line1\nline2\nline3\n")
        ret, out, err, code = _capture_call(
            cmd_daemon_logs, _ns(graph=self.graph_dir, follow=False, n=2))
        self.assertIsNone(code)

    def test_list_projects_from_home_logs(self):
        log_dir = os.path.join(self.home, ".callgraph")
        os.makedirs(log_dir, exist_ok=True)
        for name in ("projA", "projB"):
            with open(os.path.join(log_dir, "daemon-%s.log" % name),
                      "w", encoding="utf-8") as f:
                f.write("x\n")
        ret, out, err, code = _capture_call(
            cmd_daemon_list_projects, _ns(graph="."))
        self.assertIsNone(code)
        result = json.loads(out)
        projects = {p["project"] for p in result["projects"]}
        self.assertEqual(projects, {"projA", "projB"})

    def test_list_projects_includes_current_state(self):
        DaemonState(pid=42, status=STATUS_RUNNING).write(self.graph_dir)
        ret, out, err, code = _capture_call(
            cmd_daemon_list_projects, _ns(graph=self.graph_dir))
        self.assertIsNone(code)
        result = json.loads(out)
        mine = [p for p in result["projects"]
                if p["project"] == os.path.basename(self.graph_dir)]
        self.assertEqual(len(mine), 1)
        self.assertIn("state", mine[0])


class TestLiveDaemonLifecycle(unittest.TestCase):
    """In-process daemon (no-op syncs) driven through the CLI handlers."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_dmn_live_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.graph_dir = os.path.join(self.tmp, "graph")
        self.src_dir = os.path.join(self.tmp, "src")
        os.makedirs(self.graph_dir)
        os.makedirs(self.src_dir)
        self.daemon = Daemon(
            graph_dir=self.graph_dir, source_root=self.src_dir,
            config={"batch_window_ms": 50, "debounce_ms": 20,
                    "startup_grace_sec": 30.0,
                    "idle_sleep_minutes": 60,
                    "max_events_per_minute": 10000})
        # Keep the lifecycle deterministic: no real graph syncs.
        self.daemon._sync_incremental = lambda: None
        self.daemon._sync_bulk = lambda: None
        self.thread = None

    def _start_daemon(self):
        with patch.object(self.daemon, "_setup_signal_handlers"):
            self.thread = threading.Thread(target=self.daemon.start,
                                           daemon=True)
            self.thread.start()
        self.assertTrue(_wait_socket(self.graph_dir, timeout=10.0),
                        "daemon socket never came up")

    def tearDown(self):
        if self.thread is not None and self.thread.is_alive():
            self.daemon._stop = True
            self.thread.join(timeout=5.0)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_status_reports_running_with_pid(self):
        self._start_daemon()
        ret, out, err, code = _capture_call(
            cmd_daemon_status, _ns(graph=self.graph_dir))
        self.assertIsNone(code)
        # Live status is the daemon's state dict at top level (not the
        # {"running": False, "state": ...} shape of the dead-daemon path).
        result = json.loads(out)
        self.assertEqual(result["pid"], os.getpid())
        self.assertIn("sync", result)

    def test_pause_resume_roundtrip(self):
        self._start_daemon()
        ret, out, err, code = _capture_call(
            cmd_daemon_pause, _ns(graph=self.graph_dir, reason="manual edit"))
        self.assertIsNone(code)
        self.assertTrue(json.loads(out)["ok"])
        ret, out, err, code = _capture_call(
            cmd_daemon_status, _ns(graph=self.graph_dir))
        self.assertTrue(json.loads(out)["paused"])
        self.assertEqual(json.loads(out)["paused_reason"], "manual edit")
        ret, out, err, code = _capture_call(
            cmd_daemon_resume, _ns(graph=self.graph_dir))
        self.assertIsNone(code)
        self.assertTrue(json.loads(out)["ok"])
        ret, out, err, code = _capture_call(
            cmd_daemon_status, _ns(graph=self.graph_dir))
        self.assertFalse(json.loads(out)["paused"])

    def test_force_refresh_then_wait_sync(self):
        self._start_daemon()
        target = os.path.join(self.src_dir, "a.c")
        with open(target, "w", encoding="utf-8") as f:
            f.write("int main(void) { return 0; }\n")
        ret, out, err, code = _capture_call(
            cmd_daemon_force_refresh, _ns(graph=self.graph_dir,
                                          path=target))
        self.assertIsNone(code)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertIn("queued", result["message"])
        # wait-sync ends the grace and blocks until the queue drains
        ret, out, err, code = _capture_call(
            cmd_daemon_wait_sync, _ns(graph=self.graph_dir, timeout=10.0))
        self.assertIsNone(code)
        result = json.loads(out)
        self.assertTrue(result["ok"], result)

    def test_wait_sync_alone_ends_grace(self):
        self._start_daemon()
        ret, out, err, code = _capture_call(
            cmd_daemon_wait_sync, _ns(graph=self.graph_dir, timeout=5.0))
        self.assertIsNone(code)
        self.assertTrue(json.loads(out)["ok"])


@unittest.skipUnless(os.name == "posix",
                     "SIGTERM-based stop needs POSIX signals")
class TestDaemonStopSubprocess(unittest.TestCase):
    """End-to-end stop: a real builder subprocess daemon gets SIGTERM.

    The daemon runs as a separate process whose cmdline identifies it
    as a genuine Code2Database daemon, so cmd_daemon_stop's PID guard
    accepts it and signals only that process.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_dmn_sub_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.graph_dir = os.path.join(self.tmp, "graph")
        self.src_dir = os.path.join(self.tmp, "src")
        os.makedirs(self.graph_dir)
        os.makedirs(self.src_dir)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.join(REPO, "scripts")
        env["HOME"] = self.tmp  # keep logs in the sandbox
        self.proc = subprocess.Popen(
            [sys.executable,
             os.path.join(REPO, "scripts", "code2database_builder.py"),
             "daemon-start", "--graph", self.graph_dir,
             "--source", self.src_dir],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env)

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5.0)

    def test_stop_terminates_the_daemon_process(self):
        self.assertTrue(_wait_socket(self.graph_dir, timeout=20.0),
                        "subprocess daemon never came up")
        ret, out, err, code = _capture_call(
            cmd_daemon_status, _ns(graph=self.graph_dir))
        self.assertEqual(json.loads(out)["pid"], self.proc.pid)
        ret, out, err, code = _capture_call(
            cmd_daemon_stop, _ns(graph=self.graph_dir))
        self.assertIsNone(code)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["pid"], self.proc.pid)
        deadline = time.time() + 15.0
        while time.time() < deadline and self.proc.poll() is None:
            time.sleep(0.1)
        self.assertIsNotNone(self.proc.poll(),
                             "daemon process did not exit after SIGTERM")
        # State must now report not running
        ret, out, err, code = _capture_call(
            cmd_daemon_status, _ns(graph=self.graph_dir))
        self.assertFalse(json.loads(out)["running"])


if __name__ == "__main__":
    unittest.main()
