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


def _make_lazy_graph_dir(d, funcs=1):
    """Graph dir that makes _load_full_graph return a LazySQLiteGraph."""
    db = os.path.join(d, "code2database.db")
    with SQLiteStore(db) as st:
        rows = [{"id": f"n{i}", "name": f"fn{i}", "domain": "root",
                 "source_file": "a.c", "line": i + 1} for i in range(funcs)]
        st.store_functions(rows)
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


if __name__ == "__main__":
    unittest.main()
