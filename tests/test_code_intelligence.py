"""Unit tests for code_intelligence.py.

Tests 4 CLI command helpers:
- references_of: declaration + callers + field/global R/W locations
- traverse_graph: BFS/DFS with depth + node + token budgets
- hub_nodes: top-connected nodes by non-contains in+out degree
- bridge_nodes: high-betweenness-centrality chokepoints
"""
import json
import os
import tempfile
import unittest


def _make_intel_graph(nodes_spec, edges_spec) -> str:
    """Build a domain-split graph fixture from explicit node/edge lists.

    nodes_spec: list of dicts with at least id+name; optional source_file,
                line, fields_read, fields_written, labels, is_empty
    edges_spec: list of dicts with at least source+target; optional
                relation, call_condition, confidence
    """
    tmp = tempfile.mkdtemp(prefix="c2d_intel_test_")
    # Default-fill nodes
    defaulted_nodes = []
    for n in nodes_spec:
        node = {
            "id": n["id"], "name": n.get("name", n["id"]),
            "source_file": n.get("source_file", "/tmp/x.c"),
            "line": n.get("line", 1), "domain": "test",
            "labels": n.get("labels", []), "is_empty": n.get("is_empty", False),
        }
        # Copy through any extra fields the test wants on the node
        # (node_type, fields_read, fields_written, callee_args, etc.)
        for k, v in n.items():
            if k not in ("id", "name", "source_file", "line", "labels", "is_empty"):
                node[k] = v
        defaulted_nodes.append(node)
    defaulted_edges = []
    for e in edges_spec:
        edge = {"source": e["source"], "target": e["target"],
                "relation": e.get("relation", "INVOKES"),
                "confidence": e.get("confidence", "EXTRACTED")}
        if "call_condition" in e:
            edge["call_condition"] = e["call_condition"]
        defaulted_edges.append(edge)
    domain_data = {"nodes": defaulted_nodes, "edges": defaulted_edges}
    domain_filename = "domain_test.json"
    with open(os.path.join(tmp, domain_filename), "w") as f:
        json.dump(domain_data, f)
    master = {"source_root": "/tmp", "domains": {"test": domain_filename}}
    with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
        json.dump(master, f)
    return tmp


class TestReferencesOf(unittest.TestCase):
    """Tests for references_of()."""

    def setUp(self):
        # foo is called by bar and baz; bar writes a field named 'foo'
        self.graph_dir = _make_intel_graph(
            [{"id": "foo", "name": "foo", "source_file": "/tmp/foo.c", "line": 10},
             {"id": "bar", "name": "bar", "source_file": "/tmp/bar.c", "line": 20,
              "callee_args": [{"callee_id": "foo", "callee_name": "foo", "call_line": 25}],
              "fields_written": [{"field_name": "foo", "struct_chain": "ctx->foo"}]},
             {"id": "baz", "name": "baz", "source_file": "/tmp/baz.c", "line": 30}],
            [{"source": "bar", "target": "foo", "relation": "INVOKES"},
             {"source": "baz", "target": "foo", "relation": "INVOKES",
              "call_condition": "CONFIG_X"}],
        )

    def test_returns_declaration_location(self):
        from _builder.code_intelligence import references_of
        result = references_of(self.graph_dir, "foo")
        self.assertNotIn("error", result)
        decls = [l for f in result["by_file"] for l in f["locations"] if l["kind"] == "declaration"]
        self.assertEqual(len(decls), 1)
        self.assertEqual(decls[0]["file"], "/tmp/foo.c")
        self.assertEqual(decls[0]["line"], 10)

    def test_returns_call_locations(self):
        from _builder.code_intelligence import references_of
        result = references_of(self.graph_dir, "foo")
        calls = [l for f in result["by_file"] for l in f["locations"] if l["kind"] == "call"]
        self.assertEqual(len(calls), 2)
        # bar's call should use callee_args.call_line (25), not bar's line (20)
        bar_call = next(c for c in calls if c["caller"] == "bar")
        self.assertEqual(bar_call["line"], 25)
        # baz's call should carry the call_condition
        baz_call = next(c for c in calls if c["caller"] == "baz")
        self.assertEqual(baz_call["condition"], "CONFIG_X")

    def test_returns_field_write_location(self):
        from _builder.code_intelligence import references_of
        result = references_of(self.graph_dir, "foo")
        writes = [l for f in result["by_file"] for l in f["locations"] if l["kind"] == "write"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["function"], "bar")

    def test_unknown_symbol_returns_error(self):
        from _builder.code_intelligence import references_of
        result = references_of(self.graph_dir, "nonexistent")
        self.assertIn("error", result)

    def test_summary_counts_match_locations(self):
        from _builder.code_intelligence import references_of
        result = references_of(self.graph_dir, "foo")
        s = result["summary"]
        total_kinds = s["declaration"] + s["calls"] + s["reads"] + s["writes"]
        self.assertEqual(total_kinds, result["total"])

    def test_limit_truncates_locations(self):
        """limit caps the LOCATIONS that get grouped into by_file, but
        'total' reflects all locations found (uncapped) — it's the raw
        count before grouping. Verify by_file is truncated but total
        reflects reality."""
        from _builder.code_intelligence import references_of
        result = references_of(self.graph_dir, "foo", limit=1)
        # total is the uncapped count of all locations
        self.assertGreater(result["total"], 1)
        # by_file should contain only 1 location total (capped at limit=1)
        grouped = sum(f["count"] for f in result["by_file"])
        self.assertLessEqual(grouped, 1)


class TestTraverseGraph(unittest.TestCase):
    """Tests for traverse_graph() BFS/DFS."""

    def setUp(self):
        # Linear chain: a → b → c → d → e
        self.graph_dir = _make_intel_graph(
            [{"id": n, "name": n} for n in ["a", "b", "c", "d", "e"]],
            [{"source": s, "target": t, "relation": "INVOKES"}
             for s, t in [("a", "b"), ("b", "c"), ("c", "d"), ("d", "e")]],
        )

    def test_bfs_visits_in_order(self):
        from _builder.code_intelligence import traverse_graph
        result = traverse_graph(self.graph_dir, "a", mode="bfs", max_depth=10, max_nodes=100)
        self.assertNotIn("error", result)
        # BFS from a should visit a, b, c, d, e in depth order
        depths = [n["depth"] for n in result["nodes"]]
        self.assertEqual(depths, [0, 1, 2, 3, 4])

    def test_dfs_visits_all(self):
        from _builder.code_intelligence import traverse_graph
        result = traverse_graph(self.graph_dir, "a", mode="dfs", max_depth=10, max_nodes=100)
        # DFS should also reach all 5 nodes (order may differ)
        self.assertEqual(len(result["nodes"]), 5)
        node_ids = {n["id"] for n in result["nodes"]}
        self.assertEqual(node_ids, {"a", "b", "c", "d", "e"})

    def test_max_depth_truncates(self):
        from _builder.code_intelligence import traverse_graph
        result = traverse_graph(self.graph_dir, "a", mode="bfs", max_depth=2, max_nodes=100)
        # max_depth=2 → only nodes at depth 0, 1, 2 (a, b, c)
        depths = [n["depth"] for n in result["nodes"]]
        self.assertEqual(depths, [0, 1, 2])

    def test_max_nodes_truncates(self):
        from _builder.code_intelligence import traverse_graph
        result = traverse_graph(self.graph_dir, "a", mode="bfs", max_depth=10, max_nodes=2)
        self.assertEqual(len(result["nodes"]), 2)

    def test_contains_edges_excluded(self):
        """CONTAINS / IMPORTS edges should not be traversed."""
        from _builder.code_intelligence import traverse_graph
        graph_dir = _make_intel_graph(
            [{"id": "a", "name": "a"}, {"id": "b", "name": "b"}],
            [{"source": "a", "target": "b", "relation": "CONTAINS"}],
        )
        result = traverse_graph(graph_dir, "a", max_depth=5, max_nodes=10)
        # Should only visit 'a' (CONTAINS edge to b not followed)
        self.assertEqual(len(result["nodes"]), 1)

    def test_unknown_start_returns_error(self):
        from _builder.code_intelligence import traverse_graph
        result = traverse_graph(self.graph_dir, "nonexistent")
        self.assertIn("error", result)

    def test_returns_edges(self):
        from _builder.code_intelligence import traverse_graph
        result = traverse_graph(self.graph_dir, "a", max_depth=2, max_nodes=10)
        self.assertGreater(len(result["edges"]), 0)
        for e in result["edges"]:
            self.assertIn("source", e)
            self.assertIn("target", e)
            self.assertIn("relation", e)


class TestHubNodes(unittest.TestCase):
    """Tests for hub_nodes()."""

    def setUp(self):
        # hub: 3 in + 2 out = 5 total (excluded: file node + empty node)
        # leaf: 1 in + 0 out = 1
        self.graph_dir = _make_intel_graph(
            [{"id": "hub", "name": "hub"},
             {"id": "leaf", "name": "leaf"},
             {"id": "c1", "name": "c1"},
             {"id": "c2", "name": "c2"},
             {"id": "c3", "name": "c3"},
             {"id": "c4", "name": "c4"},
             {"id": "file:x.c", "name": "x.c", "node_type": "file"},
             {"id": "empty:cond", "name": "<cond>", "is_empty": True}],
            # 3 callers → hub (in_degree=3)
            [{"source": "c1", "target": "hub", "relation": "INVOKES"},
             {"source": "c2", "target": "hub", "relation": "INVOKES"},
             {"source": "c3", "target": "hub", "relation": "INVOKES"},
             # hub → c4 + leaf (out_degree=2)
             {"source": "hub", "target": "c4", "relation": "INVOKES"},
             {"source": "hub", "target": "leaf", "relation": "INVOKES"},
             # CONTAINS edges should NOT count toward degree
             {"source": "file:x.c", "target": "hub", "relation": "CONTAINS"}],
        )

    def test_returns_top_by_degree(self):
        from _builder.code_intelligence import hub_nodes
        result = hub_nodes(self.graph_dir, top_n=10)
        self.assertGreater(len(result), 0)
        # hub should be first (degree 5 = 3 in + 2 out)
        self.assertEqual(result[0]["name"], "hub")
        self.assertEqual(result[0]["in_degree"], 3)
        self.assertEqual(result[0]["out_degree"], 2)
        self.assertEqual(result[0]["total_degree"], 5)

    def test_top_n_respected(self):
        from _builder.code_intelligence import hub_nodes
        result = hub_nodes(self.graph_dir, top_n=1)
        self.assertEqual(len(result), 1)

    def test_file_nodes_excluded(self):
        from _builder.code_intelligence import hub_nodes
        result = hub_nodes(self.graph_dir, top_n=100)
        names = [r["name"] for r in result]
        self.assertNotIn("x.c", names)

    def test_empty_nodes_excluded(self):
        from _builder.code_intelligence import hub_nodes
        result = hub_nodes(self.graph_dir, top_n=100)
        names = [r["name"] for r in result]
        self.assertNotIn("<cond>", names)

    def test_contains_edges_excluded_from_degree(self):
        """file:x.c → hub CONTAINS edge should NOT count toward hub's in_degree."""
        from _builder.code_intelligence import hub_nodes
        result = hub_nodes(self.graph_dir, top_n=100)
        hub_entry = next(r for r in result if r["name"] == "hub")
        # in_degree should be 3 (c1, c2, c3) — NOT 4 (which would include file:x.c)
        self.assertEqual(hub_entry["in_degree"], 3)


class TestBridgeNodes(unittest.TestCase):
    """Tests for bridge_nodes() — betweenness centrality."""

    def setUp(self):
        # Bridge topology: a → bridge → c (bridge is on the only a→c path)
        # Plus: a → c (direct, so bridge isn't strictly needed)
        # Actually let's make bridge the ONLY path: a → bridge → c, plus d → bridge
        self.graph_dir = _make_intel_graph(
            [{"id": "a", "name": "a"}, {"id": "bridge", "name": "bridge"},
             {"id": "c", "name": "c"}, {"id": "d", "name": "d"}],
            [{"source": "a", "target": "bridge", "relation": "INVOKES"},
             {"source": "bridge", "target": "c", "relation": "INVOKES"},
             {"source": "d", "target": "bridge", "relation": "INVOKES"}],
        )

    def test_returns_bridge_with_highest_centrality(self):
        from _builder.code_intelligence import bridge_nodes
        result = bridge_nodes(self.graph_dir, top_n=10)
        self.assertGreater(len(result), 0)
        # 'bridge' should have the highest betweenness (it's the chokepoint)
        top = result[0]
        self.assertEqual(top["name"], "bridge")
        self.assertGreater(top["betweenness"], 0)

    def test_top_n_respected(self):
        from _builder.code_intelligence import bridge_nodes
        result = bridge_nodes(self.graph_dir, top_n=1)
        self.assertEqual(len(result), 1)

    def test_contains_edges_excluded_from_centrality(self):
        """CONTAINS / IMPORTS edges should not contribute to betweenness."""
        from _builder.code_intelligence import bridge_nodes
        # File nodes are identified by node_type == "file" (not labels)
        graph_dir = _make_intel_graph(
            [{"id": "a", "name": "a"}, {"id": "b", "name": "b"},
             {"id": "file:x.c", "name": "x.c", "node_type": "file"}],
            [{"source": "a", "target": "b", "relation": "INVOKES"},
             {"source": "file:x.c", "target": "a", "relation": "CONTAINS"},
             {"source": "file:x.c", "target": "b", "relation": "CONTAINS"}],
        )
        result = bridge_nodes(graph_dir, top_n=10)
        names = [r["name"] for r in result]
        # 'a' or 'b' should be top (the only non-file nodes with INVOKES edges)
        self.assertIn("a", names)
        self.assertIn("b", names)
        # file:x.c should not appear (it's a file node, excluded)
        self.assertNotIn("x.c", names)


if __name__ == "__main__":
    unittest.main()
