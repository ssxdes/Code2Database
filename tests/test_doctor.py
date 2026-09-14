"""The doctor command consolidates component health into one report.

Operators (and CI) need a single probe that answers "can this graph be
trusted right now": database integrity, schema versions, content,
freshness, memory, brief, daemon. These tests pin the report shape,
the per-check verdicts on crafted states, and the scriptable exit
codes (0 clean / 1 warnings / 2 failures).
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import _version
from _builder.graph.sqlite_store import SQLiteStore
from _builder.misc.doctor import run_doctor, cmd_doctor

CHECK_NAMES = {"database", "schema", "graph_content", "freshness",
               "memory_store", "knowledge_brief", "daemon"}


def _build_graph(tmp: str) -> str:
    graph_dir = os.path.join(tmp, "code2db-out")
    os.makedirs(graph_dir)
    with SQLiteStore(os.path.join(graph_dir, "code2database.db")) as store:
        store.store_functions([
            {"id": "src_main", "name": "main", "domain": "root",
             "source_file": "src/main.c", "line_number": 1},
            {"id": "src_util_add", "name": "add", "domain": "root",
             "source_file": "src/util/math.c", "line_number": 3},
        ])
        store.store_edges([
            {"invoker": "src_main", "invoked": "src_util_add",
             "relation": "INVOKES", "confidence": "EXTRACTED"},
        ])
    return graph_dir


def _by_name(report: dict) -> dict:
    return {c["check"]: c for c in report["checks"]}


class TestDoctor(unittest.TestCase):

    def test_missing_directory_fails_fast(self):
        with self.assertRaises(SystemExit) as cm:
            cmd_doctor(SimpleNamespace(graph="/no/such/dir", json=False))
        self.assertEqual(cm.exception.code, 2)

    def test_empty_directory_reports_database_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = run_doctor(tmp)
        names = {c["check"] for c in report["checks"]}
        self.assertEqual(names, CHECK_NAMES)
        self.assertEqual(_by_name(report)["database"]["status"], "fail")
        self.assertEqual(report["summary"]["fail"] >= 1, True)
        self.assertEqual(report["exit_code"], 2)

    def test_healthy_graph_core_checks_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph_dir = _build_graph(tmp)
            report = run_doctor(graph_dir)
        by = _by_name(report)
        self.assertEqual(by["database"]["status"], "ok")
        self.assertEqual(by["schema"]["status"], "ok")
        self.assertEqual(by["schema"]["detail"],
                         f"graph schema {SQLiteStore.SCHEMA_VERSION}, "
                         f"cgdb schema 5")
        self.assertEqual(by["graph_content"]["status"], "ok")
        self.assertIn("2 functions", by["graph_content"]["detail"])
        # brief is intentionally a warning until brief-extract runs
        self.assertEqual(by["knowledge_brief"]["status"], "warn")
        self.assertEqual(report["exit_code"], 1)

    def test_foreign_key_violation_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph_dir = _build_graph(tmp)
            db = os.path.join(graph_dir, "code2database.db")
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(
                "INSERT INTO edges (invoker_id, invoked_id, relation) "
                "VALUES ('ghost_a', 'ghost_b', 'INVOKES')")
            conn.commit()
            conn.close()
            report = run_doctor(graph_dir)
        by = _by_name(report)
        self.assertEqual(by["database"]["status"], "fail")
        self.assertIn("foreign key", by["database"]["detail"])
        self.assertEqual(report["exit_code"], 2)

    def test_schema_version_skew_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph_dir = _build_graph(tmp)
            conn = sqlite3.connect(
                os.path.join(graph_dir, "code2database.db"))
            conn.execute("UPDATE meta SET value='1' "
                         "WHERE key='schema_version'")
            conn.commit()
            conn.close()
            report = run_doctor(graph_dir)
        by = _by_name(report)
        self.assertEqual(by["schema"]["status"], "warn")
        self.assertIn("code expects", by["schema"]["detail"])
        self.assertEqual(report["exit_code"], 1)

    def test_cli_json_output_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph_dir = _build_graph(tmp)
            proc = subprocess.run(
                [sys.executable, "scripts/code2database_builder.py",
                 "doctor", "--graph", graph_dir, "--json"],
                capture_output=True, text=True, cwd=os.path.dirname(
                    os.path.dirname(os.path.abspath(__file__))))
        self.assertEqual(proc.returncode, 1, proc.stderr[-2000:])
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["tool_version"], _version.__version__)
        self.assertEqual(doc["exit_code"], 1)
        self.assertEqual({c["check"] for c in doc["checks"]}, CHECK_NAMES)
        self.assertEqual(doc["summary"]["warn"] >= 1, True)


if __name__ == "__main__":
    unittest.main()
