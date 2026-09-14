"""Guards for mutating commands on SQLite-backed large graphs.

When master.json + code2database.db coexist and the master reports
>=50K functions, _load_full_graph returns a read-only LazySQLiteGraph.
Commands that mutate the graph used to crash with NotImplementedError
(add_node/add_edge/remove_node) or — worse — silently lose writes
(G.nodes[nid][...] mutates an LRU cache dict that no reader sees).

This file pins the early-exit guards (patch-from-diff, light-scan,
add-semantic-edges) and the SQLite-delegating write paths
(update-node / update-edge supplements, apply-invariants).
"""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.graph.sqlite_store import SQLiteStore  # noqa: E402


def _make_lazy_graph_dir(d, funcs=1, with_edge=False):
    """Graph dir that makes _load_full_graph return a LazySQLiteGraph."""
    db = os.path.join(d, "code2database.db")
    with SQLiteStore(db) as st:
        rows = [{"id": f"n{i}", "name": f"fn{i}", "domain": "root",
                 "source_file": "a.c", "line": i + 1} for i in range(funcs)]
        st.store_functions(rows)
        if with_edge and funcs >= 2:
            st.store_edges([{"invoker": "n0", "invoked": "n1",
                             "call_order": 1,
                             "evidence": "call at a.c:5"}])
    master = os.path.join(d, "code2database_master.json")
    with open(master, "w", encoding="utf-8") as f:
        json.dump({"stats": {"total_functions": 60000},
                   "source_root": d}, f)
    return db


class TestPatcherGuards(unittest.TestCase):
    def test_patch_from_diff_exits_cleanly_on_lazy(self):
        from _builder.ops.patcher import patch_from_diff
        with tempfile.TemporaryDirectory() as d:
            _make_lazy_graph_dir(d)
            with self.assertRaises(SystemExit) as ctx:
                patch_from_diff(d, "diff --git a/a.c b/a.c\n", d)
            self.assertEqual(ctx.exception.code, 2)

    def test_light_scan_exits_cleanly_on_lazy(self):
        from _builder.ops.patcher import light_scan
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "proj")
            os.makedirs(src)
            with open(os.path.join(src, "a.c"), "w") as f:
                f.write("int f(void) { return 1; }\n")
            graph = os.path.join(d, "out")
            os.makedirs(graph)
            _make_lazy_graph_dir(graph)
            with self.assertRaises(SystemExit) as ctx:
                light_scan(src, graph, [os.path.join(src, "a.c")])
            self.assertEqual(ctx.exception.code, 2)


class TestSemanticEdgesGuard(unittest.TestCase):
    def test_add_semantic_edges_exits_cleanly_on_lazy(self):
        from _builder.graph.semantic_edges import cmd_add_semantic_edges
        with tempfile.TemporaryDirectory() as d:
            _make_lazy_graph_dir(d)
            args = SimpleNamespace(graph=d)
            with self.assertRaises(SystemExit) as ctx:
                cmd_add_semantic_edges(args)
            self.assertEqual(ctx.exception.code, 2)


class TestJsonUpdateNodeDelegatesToSqlite(unittest.TestCase):
    """update-node on a lazy graph must reach functions.extra_json —
    the old path mutated an LRU cache dict and lost the supplement."""

    def test_supplement_persisted_to_extra_json(self):
        import sqlite3
        from _builder.ops.update_cmd import _json_update_node
        with tempfile.TemporaryDirectory() as d:
            db = _make_lazy_graph_dir(d, funcs=2)
            ok = _json_update_node(
                d, "n0", {"semantic_desc": "does things"},
                source="test", confidence="INFERRED")
            self.assertTrue(ok)
            conn = sqlite3.connect(db)
            try:
                extra = json.loads(conn.execute(
                    "SELECT extra_json FROM functions WHERE id='n0'")
                    .fetchone()[0])
                self.assertEqual(
                    extra.get("semantic_desc_supplemented"), "does things")
                meta = extra.get("_supplement_meta", {})
                self.assertIn("semantic_desc_supplemented", meta)
                self.assertEqual(
                    meta["semantic_desc_supplemented"]["confidence"],
                    "INFERRED")
            finally:
                conn.close()

    def test_delete_keys_remove_supplement_on_lazy(self):
        import sqlite3
        from _builder.ops.update_cmd import _json_update_node
        with tempfile.TemporaryDirectory() as d:
            db = _make_lazy_graph_dir(d, funcs=2)
            _json_update_node(d, "n0", {"semantic_desc": "temp"},
                              source="test", confidence="INFERRED")
            ok = _json_update_node(
                d, "n0", {}, source="test", confidence="INFERRED",
                delete_keys=["semantic_desc"])
            self.assertTrue(ok)
            conn = sqlite3.connect(db)
            try:
                extra = json.loads(conn.execute(
                    "SELECT extra_json FROM functions WHERE id='n0'")
                    .fetchone()[0])
                self.assertNotIn("semantic_desc_supplemented", extra)
            finally:
                conn.close()


class TestJsonUpdateEdgeDelegatesToSqlite(unittest.TestCase):
    def test_edge_supplement_persisted(self):
        import sqlite3
        from _builder.ops.update_cmd import _json_update_edge
        with tempfile.TemporaryDirectory() as d:
            db = _make_lazy_graph_dir(d, funcs=2, with_edge=True)
            ok = _json_update_edge(
                d, "n0", "n1", {"call_condition": "CONFIG_X"},
                source="test", confidence="EXTRACTED")
            self.assertTrue(ok)
            conn = sqlite3.connect(db)
            try:
                row = conn.execute(
                    "SELECT call_condition FROM edges "
                    "WHERE invoker_id='n0' AND invoked_id='n1'").fetchone()
                self.assertEqual(row[0], "CONFIG_X")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
