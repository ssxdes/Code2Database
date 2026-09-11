"""Tests for graph_diff (build comparison) between two graph dirs.

The JSON-storage fallback previously misread master["domains"] values
(they are relative file paths, not inline dicts) and crashed with
AttributeError on any JSON-only graph; per-domain compact edge rows and
the master's cross-domain/structural edge lists were never loaded at
all, so edge diffs on JSON graphs always reported zero.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.graph.graph_diff import graph_diff


def _write_json_graph(graph_dir, domains_map, doms, cross=None, struct=None):
    """Write a JSON-only graph layout (no code2database.db)."""
    os.makedirs(graph_dir, exist_ok=True)
    for dom_name, dom_data in doms.items():
        rel = domains_map[dom_name]
        path = os.path.join(graph_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dom_data, f)
    with open(os.path.join(graph_dir, "code2database_master.json"),
              "w", encoding="utf-8") as f:
        json.dump({
            "type": "code2database_master",
            "domains": domains_map,
            "cross_domain_edges": cross or [],
            "structural_edges": struct or [],
        }, f)


def _dom(functions, edges):
    return {"type": "code2database_domain", "domain": "root",
            "functions": functions, "function_details": {},
            "edges": edges}


_F = ["root::f", "f", "a.c", 10, json.dumps(["API_entry"]), "void f()"]
_G = ["root::g", "g", "b.c", 20, "", "void g()"]
_H = ["root::h", "h", "c.c", 30, "", "void h()"]
# Compact edge row: [source, target, call_order, call_condition,
#                    concurrency, confidence, source_tag, confidence_score]
_E_FG = ["root::f", "root::g", 1, "", "direct_call", "EXTRACTED", "ast", 1.0]
_E_GH = ["root::g", "root::h", 1, "", "direct_call", "EXTRACTED", "ast", 1.0]


class TestGraphDiffJsonFallback(unittest.TestCase):

    def setUp(self):
        self.before = tempfile.mkdtemp(prefix="c2d_gd_before_")
        self.after = tempfile.mkdtemp(prefix="c2d_gd_after_")
        self.addCleanup(shutil.rmtree, self.before, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.after, ignore_errors=True)

    def test_json_graph_diff_reports_edge_changes(self):
        domains_map = {"root": os.path.join("domains", "root.json")}
        _write_json_graph(self.before, domains_map,
                          {"root": _dom([_F, _G], [_E_FG])})
        _write_json_graph(self.after, domains_map,
                          {"root": _dom([_F, _G, _H], [_E_FG, _E_GH])})
        res = graph_diff(self.before, self.after, detail="full")
        self.assertEqual(res["stats"]["added_nodes"], 1)
        self.assertEqual(res["stats"]["added_edges"], 1)
        self.assertEqual(res["nodes"]["added"][0]["id"], "root::h")
        self.assertEqual(res["edges"]["added"][0],
                         {"source": "root::g", "target": "root::h",
                          "relation": "INVOKES"})

    def test_json_graph_diff_detects_node_changes(self):
        domains_map = {"root": os.path.join("domains", "root.json")}
        _write_json_graph(self.before, domains_map,
                          {"root": _dom([_F], [])})
        moved = ["root::f", "f", "a.c", 99, json.dumps(["API_entry"]), "void f()"]
        _write_json_graph(self.after, domains_map,
                          {"root": _dom([moved], [])})
        res = graph_diff(self.before, self.after, detail="full")
        self.assertEqual(res["stats"]["changed_nodes"], 1)

    def test_json_graph_diff_includes_master_edges(self):
        domains_map = {"root": os.path.join("domains", "root.json")}
        _write_json_graph(self.before, domains_map,
                          {"root": _dom([_F, _G], [])})
        _write_json_graph(self.after, domains_map,
                          {"root": _dom([_F, _G], [])},
                          cross=[{"source": "root::f", "target": "root::g",
                                  "concurrency": "vtable_dispatch"}],
                          struct=[{"source": "root::f", "target": "root::g",
                                   "relation": "IMPORTS"}])
        res = graph_diff(self.before, self.after, detail="full")
        self.assertEqual(res["stats"]["added_edges"], 2)


if __name__ == "__main__":
    unittest.main()
