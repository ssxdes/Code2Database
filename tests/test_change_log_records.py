"""Change-log rows: commit-anchored records from per-file syncs.

Every per-file graph sync records what it rewrote — one row per
re-written node, one per deleted file — anchored to the source commit
that was HEAD when the sync ran. describe-commit / node-history read
those rows back. This is knowledge an LLM cannot regenerate: which
commit touched which part of the graph, as a durable SQL record.
"""
import inspect
import json
import os
import subprocess
import sqlite3
import sys
import tempfile
import unittest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts')
BUILDER = os.path.join(SCRIPTS_DIR, 'code2database_builder.py')
SCANNER = os.path.join(SCRIPTS_DIR, 'code2database_scanner.py')

_SCAN_MEM_FLAGS = ["--memory-limit", "9999",
                   "--memory-warn-threshold", "0.99",
                   "--memory-crit-threshold", "0.999"]
_BUILD_MEM_FLAGS = ["--memory-warn-threshold", "0.99",
                    "--memory-crit-threshold", "0.999"]

_MATH_C = """\
int add(int a, int b) {
    return a + b;
}

int mul(int a, int b) {
    return add(a, b);
}
"""

_MATH_C_V2 = """\
int add(int a, int b) {
    return a + b;
}

int sub(int a, int b) {
    return add(a, -b);
}
"""


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.email=probe@example.com",
         "-c", "user.name=Probe", *args],
        cwd=cwd, capture_output=True, text=True, check=True)


def _make_git_source(root):
    src = os.path.join(root, "proj")
    _write(os.path.join(src, "math.c"), _MATH_C)
    _git(src, "init", "-q")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "initial state")
    return src


class TestRecordSyncChangeLog(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.source = _make_git_source(self.root)
        self.graph = os.path.join(self.root, "out")
        os.makedirs(self.graph)
        sys.path.insert(0, SCRIPTS_DIR)
        from _builder.graph.sqlite_store import SQLiteStore
        self.store = SQLiteStore(
            os.path.join(self.graph, "code2database.db"))
        self.store.connect()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _head_short(self):
        # Production slices the full hash to 8 chars (commit_meta), so
        # the probe must match that convention — `rev-parse --short`
        # yields 7 on some git builds.
        head = _git(self.source, "rev-parse", "HEAD").stdout.strip()
        return head[:8]

    def test_records_node_rows_anchored_to_head(self):
        from _builder.commit_meta import record_sync_change_log
        written = {os.path.join(self.source, "math.c"): ["c::add", "c::mul"]}
        deleted = [os.path.join(self.source, "gone.c")]
        written_count = self.store.query_change_log_by_commit(self._head_short())
        self.assertEqual(written_count, [])
        count = record_sync_change_log(self.store, self.source,
                                       written, deleted)
        self.assertEqual(count, 3)
        rows = self.store.query_change_log_by_commit(self._head_short())
        by_node = {r["node_id"]: r for r in rows if r["node_id"]}
        self.assertEqual(set(by_node), {"c::add", "c::mul"})
        for row in by_node.values():
            self.assertEqual(row["change_type"], "modified")
            self.assertIn("math.c", row["diff_summary"])
        deleted_rows = [r for r in rows if not r["node_id"]]
        self.assertEqual(len(deleted_rows), 1)
        self.assertEqual(deleted_rows[0]["change_type"], "deleted")
        self.assertIn("gone.c", deleted_rows[0]["diff_summary"])

    def test_node_history_reads_back_rows(self):
        from _builder.commit_meta import record_sync_change_log
        written = {os.path.join(self.source, "math.c"): ["c::add"]}
        record_sync_change_log(self.store, self.source, written, [])
        history = self.store.query_change_log_by_node("c::add")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["change_type"], "modified")
        self.assertEqual(history[0]["commit_short"], self._head_short())

    def test_non_vcs_tree_records_nothing(self):
        from _builder.commit_meta import record_sync_change_log
        plain = os.path.join(self.root, "plain")
        os.makedirs(plain)
        _write(os.path.join(plain, "x.c"), "int x(void) { return 0; }\n")
        count = record_sync_change_log(
            self.store, plain, {os.path.join(plain, "x.c"): ["c::x"]}, [])
        self.assertEqual(count, 0)

    def test_nothing_to_record_returns_zero(self):
        from _builder.commit_meta import record_sync_change_log
        self.assertEqual(
            record_sync_change_log(self.store, self.source, {}, []), 0)

    def test_batch_writer_matches_table_shape(self):
        entries = [{
            "commit_hash": "a" * 40, "commit_short": "aaaa",
            "commit_author": "Probe", "commit_date": "2026-01-01",
            "commit_subject": "probe", "branch": "main",
            "node_id": "c::probe", "change_type": "modified",
            "diff_summary": "per-file sync: math.c",
            "affected_attrs": ["body_text"], "logged_at": "2026-01-01T00:00:00",
        }]
        self.store.store_change_log_entries(entries)
        rows = self.store.query_change_log_by_commit("aaaa")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["node_id"], "c::probe")
        self.assertEqual(rows[0]["affected_attrs"], ["body_text"])


class TestGuidanceHonesty(unittest.TestCase):
    """The guidance text must not reference flags that do not exist."""

    def test_describe_commit_has_no_phantom_flag(self):
        from _builder.query.query_provenance import cmd_describe_commit
        self.assertNotIn("track-commits",
                         inspect.getsource(cmd_describe_commit))

    def test_node_history_has_no_phantom_flag(self):
        from _builder.query.query_provenance import cmd_node_history
        self.assertNotIn("track-commits",
                         inspect.getsource(cmd_node_history))


class TestSyncEndToEnd(unittest.TestCase):
    """Real scan -> build -> edit+commit -> per-file sync -> change rows."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = cls._tmp.name
        cls.source = _make_git_source(root)
        cls.graph = os.path.join(root, "out")
        os.makedirs(cls.graph)
        env = dict(os.environ, PYTHONPATH=SCRIPTS_DIR)
        scan = subprocess.run(
            [sys.executable, SCANNER, "scan",
             "--source", cls.source,
             "--output", os.path.join(cls.graph, "extraction.json"),
             "--extraction-backend", "tree-sitter",
             "--no-interactive", "--auto-profile"] + _SCAN_MEM_FLAGS,
            capture_output=True, text=True, timeout=300, env=env)
        if scan.returncode != 0:
            raise AssertionError("scan failed:\n%s\n%s"
                                 % (scan.stdout[-1500:], scan.stderr[-1500:]))
        build = subprocess.run(
            [sys.executable, BUILDER, "build",
             "--extraction", os.path.join(cls.graph, "extraction.json"),
             "--outdir", cls.graph,
             "--storage", "sqlite"] + _BUILD_MEM_FLAGS,
            capture_output=True, text=True, timeout=300, env=env)
        if build.returncode != 0:
            raise AssertionError("build failed:\n%s\n%s"
                                 % (build.stdout[-1500:], build.stderr[-1500:]))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _head_short(self):
        head = _git(self.source, "rev-parse", "HEAD").stdout.strip()
        return head[:8]

    def test_sync_records_change_rows_at_head(self):
        _write(os.path.join(self.source, "math.c"), _MATH_C_V2)
        _git(self.source, "add", "-A")
        _git(self.source, "commit", "-q", "-m", "swap mul for sub")
        env = dict(os.environ, PYTHONPATH=SCRIPTS_DIR)
        proc = subprocess.run(
            [sys.executable, BUILDER, "build-update",
             "--source", self.source,
             "--graph", self.graph,
             "--extraction-backend", "tree-sitter", "--json"],
            capture_output=True, text=True, timeout=300, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        report = json.loads(proc.stdout[proc.stdout.index("{"):])
        self.assertGreaterEqual(report.get("change_log_entries", 0), 1)
        conn = sqlite3.connect(
            os.path.join(self.graph, "code2database.db"))
        try:
            rows = conn.execute(
                "SELECT node_id, change_type FROM change_log "
                "WHERE commit_short = ?", (self._head_short(),)).fetchall()
        finally:
            conn.close()
        self.assertGreaterEqual(len(rows), 1)
        for node_id, change_type in rows:
            self.assertEqual(change_type, "modified")
            self.assertTrue(node_id)


if __name__ == "__main__":
    unittest.main()
