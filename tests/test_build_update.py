"""build-update: precise per-file SQLite graph updates from disk changes.

Real scan -> build (--storage sqlite) -> mutate source -> build-update
round trip. The graph DB must track source edits in seconds: new
functions appear, removed functions (and their edges) vanish, other
files' rows are untouched — without a full rebuild.
"""
import json
import os
import sqlite3
import subprocess
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
#include "math.h"

int add(int a, int b) {
    return a + b;
}

int mul(int a, int b) {
    int acc = 0;
    for (int i = 0; i < b; i++) {
        acc = add(acc, a);
    }
    return acc;
}
"""

_MATH_H = """\
#ifndef MATH_H
#define MATH_H
int add(int a, int b);
int mul(int a, int b);
#endif
"""

# mul removed, sub added
_MATH_C_V2 = """\
#include "math.h"

int add(int a, int b) {
    return a + b;
}

int sub(int a, int b) {
    return add(a, -b);
}
"""

_EXTRA_C = """\
int double_it(int x) {
    return x + x;
}
"""


def _write(root, rel, content):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


class TestBuildUpdate(unittest.TestCase):
    """e2e: sqlite build, then per-file updates driven by disk edits."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = cls._tmp.name
        cls.source = os.path.join(root, "proj")
        cls.graph = os.path.join(root, "out")
        os.makedirs(cls.graph)
        _write(cls.source, "src/util/math.c", _MATH_C)
        _write(cls.source, "src/util/math.h", _MATH_H)

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
                                 % (scan.stdout[-2000:], scan.stderr[-2000:]))
        build = subprocess.run(
            [sys.executable, BUILDER, "build",
             "--extraction", os.path.join(cls.graph, "extraction.json"),
             "--outdir", cls.graph,
             "--storage", "sqlite"] + _BUILD_MEM_FLAGS,
            capture_output=True, text=True, timeout=300, env=env)
        if build.returncode != 0:
            raise AssertionError("build failed:\n%s\n%s"
                                 % (build.stdout[-2000:], build.stderr[-2000:]))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _query(self, sql, params=()):
        """One-shot query: open, fetch, close — never hold a connection
        across the build-update subprocess (WSL1 WAL locking)."""
        path = os.path.join(self.graph, "code2database.db")
        self.assertTrue(os.path.exists(path),
                        "precondition: sqlite-backed graph required")
        conn = sqlite3.connect(path)
        try:
            return [tuple(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def _func_names(self):
        return {r[0] for r in self._query("SELECT name FROM functions")}

    def _run_update(self, extra=()):
        env = dict(os.environ, PYTHONPATH=SCRIPTS_DIR)
        return subprocess.run(
            [sys.executable, BUILDER, "build-update",
             "--source", self.source,
             "--graph", self.graph,
             "--extraction-backend", "tree-sitter",
             "--json"] + list(extra),
            capture_output=True, text=True, timeout=300, env=env)

    @staticmethod
    def _report(proc):
        out = proc.stdout
        return json.loads(out[out.index("{"):])

    def test_modify_file_adds_and_removes_functions(self):
        self.assertIn("mul", self._func_names())
        math_c = os.path.join(self.source, "src/util/math.c")
        with open(math_c, "w") as f:
            f.write(_MATH_C_V2)
        proc = self._run_update()
        self.assertEqual(proc.returncode, 0,
                         "build-update failed:\n%s\n%s"
                         % (proc.stdout[-2000:], proc.stderr[-2000:]))
        report = self._report(proc)
        self.assertGreaterEqual(report["updated_files"], 1)
        names = self._func_names()
        self.assertIn("sub", names, "new function must appear")
        self.assertNotIn("mul", names, "removed function must vanish")
        # mul's edges must not dangle
        dangling = self._query(
            "SELECT COUNT(*) FROM edges e "
            "WHERE e.invoker_id NOT IN (SELECT id FROM functions) "
            "   OR e.invoked_id NOT IN (SELECT id FROM functions)")[0][0]
        self.assertEqual(dangling, 0, "dangling edges after update")
        # The 'file:' pseudo-node + its CONTAINS edges are re-created
        # (drives UI file grouping).
        fnode = self._query(
            "SELECT COUNT(*) FROM functions WHERE node_type = 'file' "
            "AND source_file LIKE '%math.c'")[0][0]
        self.assertEqual(fnode, 1, "file pseudo-node must be re-created")
        contains = self._query(
            "SELECT COUNT(*) FROM edges WHERE relation = 'CONTAINS' "
            "AND invoker_id LIKE 'file:%math.c'")[0][0]
        self.assertEqual(contains, 2,
                         "CONTAINS edges for add+sub must be re-created")

    def test_add_then_delete_file(self):
        _write(self.source, "src/util/extra.c", _EXTRA_C)
        proc = self._run_update()
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        self.assertIn("double_it", self._func_names())
        # cgdb layer must know the file too
        cgdb_has = self._query(
            "SELECT COUNT(*) FROM cgdb_files WHERE path LIKE '%extra.c'"
        )[0][0]
        self.assertGreater(cgdb_has, 0, "cgdb_files missing extra.c")

        os.unlink(os.path.join(self.source, "src/util/extra.c"))
        proc = self._run_update()
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        self.assertNotIn("double_it", self._func_names())
        cgdb_has = self._query(
            "SELECT COUNT(*) FROM cgdb_files WHERE path LIKE '%extra.c'"
        )[0][0]
        self.assertEqual(cgdb_has, 0, "cgdb_files row must be removed")

    def test_noop_when_unchanged(self):
        # First run may self-heal legacy rows (builds left content_hash
        # empty for CWD-relative paths); the second must be a no-op.
        proc = self._run_update()
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        proc = self._run_update()
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        report = self._report(proc)
        self.assertEqual(report["updated_files"], 0)
        self.assertEqual(report["deleted_files"], 0)

    def test_json_only_graph_rejected_with_actionable_error(self):
        with tempfile.TemporaryDirectory() as td:
            graph = os.path.join(td, "out")
            os.makedirs(graph)
            # no code2database.db at all
            env = dict(os.environ, PYTHONPATH=SCRIPTS_DIR)
            proc = subprocess.run(
                [sys.executable, BUILDER, "build-update",
                 "--source", self.source, "--graph", graph, "--json"],
                capture_output=True, text=True, timeout=60, env=env)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("build", (proc.stderr + proc.stdout).lower())


    def test_format_only_change_skips_db_writes(self):
        # Prime: heal this file's baseline (abs path + ast_hash).
        proc = self._run_update()
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        before_f = self._query("SELECT COUNT(*) FROM functions")[0][0]
        before_e = self._query("SELECT COUNT(*) FROM edges")[0][0]
        # Comment + blank-line insertion: same code graph, new layout.
        math_c = os.path.join(self.source, "src/util/math.c")
        with open(math_c) as f:
            content = f.read()
        with open(math_c, "w") as f:
            f.write("// just a comment\n\n" + content)
        proc = self._run_update()
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        report = self._report(proc)
        self.assertGreaterEqual(report["format_only_skipped"], 1,
                                "format-only edit must skip DB writes")
        self.assertEqual(report["updated_files"], 0)
        self.assertEqual(
            self._query("SELECT COUNT(*) FROM functions")[0][0], before_f,
            "functions rows must be untouched by a format-only edit")
        self.assertEqual(
            self._query("SELECT COUNT(*) FROM edges")[0][0], before_e,
            "edge rows must be untouched by a format-only edit")
        # Hash was refreshed — next run sees a clean tree.
        proc = self._run_update()
        report = self._report(proc)
        self.assertEqual(report["changed_files"], 0)
        self.assertEqual(report["added_files"], 0)

    def test_daemon_stale_mark_persists(self):
        """Regression: _mark_file_stale's first UPDATE hit a nonexistent
        `stale` column; the except swallowed it and skipped the
        extra_json update + commit — stale-marking was fully dead."""
        from _builder.daemon import Daemon

        class _D:
            graph_dir = self.graph
            _log = staticmethod(lambda msg: None)

        math_c = os.path.join(self.source, "src/util/math.c")
        Daemon._mark_file_stale(_D(), math_c)
        stale = self._query(
            "SELECT COUNT(*) FROM functions WHERE extra_json "
            "LIKE '%\"stale\"%1%' AND source_file LIKE '%math.c'"
        )[0][0]
        self.assertGreater(
            stale, 0,
            "extra_json.$.stale must be set after daemon stale-mark")


if __name__ == "__main__":
    unittest.main()
