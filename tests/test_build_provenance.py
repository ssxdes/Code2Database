"""Build provenance: the DB records how it was produced.

The meta-table stamp (timestamp / tool version / source commit / label /
content counts) is knowledge an LLM cannot regenerate — the artifact must
describe itself. These tests cover the stamp/read round trip, the
best-effort contract, the graph-provenance consumption and the wiring of
every path that produces SQLite content.
"""
import contextlib
import inspect
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _version
from _builder.build.build_provenance import (
    read_build_provenance, stamp_build_provenance)


def _make_db(graph_dir, functions=2, edges=1):
    db = os.path.join(graph_dir, "code2database.db")
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE functions (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE edges (invoker TEXT, invoked TEXT)")
        for i in range(functions):
            conn.execute("INSERT INTO functions VALUES (?, ?)",
                         (f"c:fn{i}", f"fn{i}"))
        for i in range(edges):
            conn.execute("INSERT INTO edges VALUES (?, ?)",
                         ("c:fn0", f"c:fn{i + 1}"))
        conn.commit()
    finally:
        conn.close()
    return db


def _make_manifest(graph_dir, head="deadbeef1234"):
    manifest = {
        "source_root": "/tmp/probe-src",
        "files": {"a.c": "1:1"},
        "source_commit": {"head": head, "head_short": head[:8]},
    }
    path = os.path.join(graph_dir, ".code2database_manifest.json")
    Path(path).write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


class TestStampAndRead(unittest.TestCase):

    def test_roundtrip_stamps_all_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_db(tmp, functions=3, edges=2)
            _make_manifest(tmp)
            self.assertTrue(
                stamp_build_provenance(tmp, label="per-file-sync"))
            prov = read_build_provenance(tmp)
            self.assertEqual(prov["build_label"], "per-file-sync")
            self.assertEqual(prov["build_tool_version"],
                             _version.__version__)
            self.assertEqual(prov["build_source_commit"], "deadbeef")
            self.assertEqual(prov["build_node_count"], "3")
            self.assertEqual(prov["build_edge_count"], "2")
            self.assertRegex(prov["build_timestamp"], r"^\d{4}-\d{2}-\d{2}T")

    def test_recomputes_counts_when_not_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_db(tmp, functions=5, edges=4)
            self.assertTrue(stamp_build_provenance(tmp, label="build"))
            prov = read_build_provenance(tmp)
            self.assertEqual(prov["build_node_count"], "5")
            self.assertEqual(prov["build_edge_count"], "4")

    def test_explicit_counts_win_over_recomputed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_db(tmp, functions=5, edges=4)
            stamp_build_provenance(tmp, label="build",
                                   node_count=99, edge_count=98)
            prov = read_build_provenance(tmp)
            self.assertEqual(prov["build_node_count"], "99")
            self.assertEqual(prov["build_edge_count"], "98")

    def test_absent_db_is_a_silent_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(stamp_build_provenance(tmp, label="build"))
            self.assertEqual(read_build_provenance(tmp), {})

    def test_restamp_replaces_previous_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_db(tmp)
            stamp_build_provenance(tmp, label="build")
            stamp_build_provenance(tmp, label="per-file-sync")
            prov = read_build_provenance(tmp)
            self.assertEqual(prov["build_label"], "per-file-sync")
            conn = sqlite3.connect(os.path.join(tmp, "code2database.db"))
            try:
                rows = conn.execute(
                    "SELECT COUNT(*) FROM meta WHERE key LIKE 'build_%'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(rows, 6, "exactly one row per provenance key")

    def test_missing_content_tables_degrade_to_minus_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "code2database.db")
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
            conn.commit()
            conn.close()
            self.assertTrue(stamp_build_provenance(tmp, label="build"))
            prov = read_build_provenance(tmp)
            self.assertEqual(prov["build_node_count"], "-1")
            self.assertEqual(prov["build_edge_count"], "-1")

    def test_non_git_source_records_empty_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_db(tmp)
            # No manifest at all — the commit fact degrades to "".
            self.assertTrue(stamp_build_provenance(tmp, label="build"))
            prov = read_build_provenance(tmp)
            self.assertEqual(prov["build_source_commit"], "")


class TestGraphProvenanceConsumption(unittest.TestCase):

    def test_cmd_graph_provenance_reads_the_stamp(self):
        from _builder.query.query_provenance import cmd_graph_provenance
        with tempfile.TemporaryDirectory() as tmp:
            _make_db(tmp)
            _make_manifest(tmp)
            stamp_build_provenance(tmp, label="per-file-sync")
            out = io.StringIO()
            args = SimpleNamespace(graph=tmp, json=True)
            with contextlib.redirect_stdout(out):
                cmd_graph_provenance(args)
            data = json.loads(out.getvalue())
            self.assertEqual(data["build_label"], "per-file-sync")
            self.assertEqual(data["build_tool_version"],
                             _version.__version__)
            self.assertRegex(data["build_timestamp"], r"^\d{4}-\d{2}-\d{2}T")

    def test_cmd_graph_provenance_without_stamp_keeps_legacy_shape(self):
        from _builder.query.query_provenance import cmd_graph_provenance
        with tempfile.TemporaryDirectory() as tmp:
            _make_db(tmp)
            _make_manifest(tmp)
            out = io.StringIO()
            args = SimpleNamespace(graph=tmp, json=True)
            with contextlib.redirect_stdout(out):
                cmd_graph_provenance(args)
            data = json.loads(out.getvalue())
            self.assertNotIn("build_label", data)
            self.assertIsNone(data["build_timestamp"])


class TestCallSiteWiring(unittest.TestCase):
    """Every path that produces SQLite content must stamp it."""

    def test_full_build_references_stamp(self):
        from _builder.graph.graph_build import cmd_build
        self.assertIn("stamp_build_provenance",
                      inspect.getsource(cmd_build))

    def test_per_file_sync_references_stamp(self):
        from _builder.build.build_update import _build_update_locked
        src = inspect.getsource(_build_update_locked)
        self.assertIn("stamp_build_provenance", src)
        self.assertIn("per-file-sync", src)

    def test_stamp_only_after_content_changes(self):
        from _builder.build.build_update import _build_update_locked
        src = inspect.getsource(_build_update_locked)
        self.assertIn("_content_changed", src)


if __name__ == "__main__":
    unittest.main()
