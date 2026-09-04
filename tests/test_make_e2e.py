"""End-to-end test: `make` on a real small C project.

Runs the actual pipeline (env-check + scan + build + all enrichment and
export steps) as a subprocess against a temporary C project, then asserts
every promised artifact exists and the built graph is queryable.

Uses --extraction-backend tree-sitter so the test is hermetic (no
libclang required). Runtime is dominated by the real scan/build.
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

_MAIN_C = """#include "util.h"
int main(void) { return util_sum(1, 2); }
"""

_UTIL_H = """int util_sum(int a, int b);
"""

_UTIL_C = """#include "util.h"
static int g_cache = 0;
int util_sum(int a, int b) { g_cache = a + b; return g_cache; }
"""


class TestMakeEndToEnd(unittest.TestCase):
    """Real `make` run against a real (tiny) project."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = cls._tmp.name
        cls.source = os.path.join(root, "proj")
        cls.graph = os.path.join(root, "out")
        os.makedirs(cls.source)
        for name, text in (("main.c", _MAIN_C), ("util.h", _UTIL_H),
                           ("util.c", _UTIL_C)):
            with open(os.path.join(cls.source, name), "w") as f:
                f.write(text)

        proc = subprocess.run(
            [sys.executable, BUILDER, "make",
             "--source", cls.source,
             "--graph", cls.graph,
             "--extraction-backend", "tree-sitter"],
            capture_output=True, text=True, timeout=600,
        )
        cls.proc = proc

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_make_exits_zero_with_no_failed_steps(self):
        self.assertEqual(self.proc.returncode, 0,
                         "make failed:\n%s\n%s"
                         % (self.proc.stdout[-3000:], self.proc.stderr[-3000:]))
        self.assertIn("0 failed", self.proc.stdout)

    def test_make_reports_all_twelve_steps(self):
        self.assertIn("build pipeline (12 steps)", self.proc.stdout)
        self.assertIn("[make] done:", self.proc.stdout)
        # env-check phase ran first and passed
        self.assertIn("env-check OK", self.proc.stdout)

    def test_core_artifacts_exist(self):
        for rel in ("code2database.db", "extraction.json",
                    "code2database_master.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.graph, rel)),
                            "missing %s" % rel)

    def test_derived_artifacts_exist(self):
        for rel in (".code2database_data_flow.json",
                    ".code2database_data_dep.json",
                    ".code2database_ffi.json",
                    "embeddings.json",
                    "callgraph.html",
                    os.path.join("knowledge", "brief.json")):
            path = os.path.join(self.graph, rel)
            self.assertTrue(os.path.isfile(path), "missing %s" % rel)
            self.assertGreater(os.path.getsize(path), 0,
                               "empty artifact: %s" % rel)

    def test_exports_exist(self):
        vault = os.path.join(self.graph, "obsidian-vault")
        self.assertTrue(os.path.isdir(vault))
        mds = []
        for root, _dirs, files in os.walk(vault):
            mds += [f for f in files if f.endswith(".md")]
        self.assertGreater(len(mds), 0, "obsidian vault has no notes")

    def test_kb_index_tables_created(self):
        conn = sqlite3.connect(os.path.join(self.graph, "code2database.db"))
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            self.assertIn("kb_paragraphs", tables)
        finally:
            conn.close()

    def test_built_graph_is_queryable(self):
        proc = subprocess.run(
            [sys.executable, BUILDER, "load", "--graph", self.graph],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertGreaterEqual(summary["nodes"], 2,
                                "graph should contain main + util_sum")
        self.assertGreaterEqual(summary["edges"], 1)

    def test_data_dep_edges_written(self):
        path = os.path.join(self.graph, ".code2database_data_dep.json")
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        for key in ("globals", "fields", "mod_read_chains"):
            self.assertIn(key, payload)


if __name__ == "__main__":
    unittest.main()
