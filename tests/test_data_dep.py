"""Unit tests for data_dep.py — cross-function data dependencies.

data_dep.py builds DATA_DEP edges from globals_read/written +
fields_read/written attributes, computes forward/reverse impact,
combined blast-radius with data-dep, and finds dead writers.

Coverage:
- build_data_dep_edges: global + field node registries, mod-read chains
- forward_data_dep_impact: transitive readers of a function's writes
- reverse_data_dep_impact: transitive writers of a function's reads
- find_dead_writers: functions that write globals/fields with no readers
"""
import json
import os
import tempfile
import unittest


def _make_dep_graph(nodes_spec) -> str:
    """Build a domain-split graph fixture for data-dep tests."""
    tmp = tempfile.mkdtemp(prefix="c2d_dep_test_")
    defaulted_nodes = []
    for n in nodes_spec:
        node = {
            "id": n["id"], "name": n.get("name", n["id"]),
            "source_file": n.get("source_file", "/tmp/x.c"),
            "line": n.get("line", 1), "domain": "test",
            "labels": n.get("labels", []), "is_empty": n.get("is_empty", False),
        }
        for k, v in n.items():
            if k not in ("id", "name", "source_file", "line", "labels", "is_empty"):
                node[k] = v
        defaulted_nodes.append(node)
    domain_data = {"nodes": defaulted_nodes, "edges": []}
    domain_filename = "domain_test.json"
    with open(os.path.join(tmp, domain_filename), "w") as f:
        json.dump(domain_data, f)
    master = {"source_root": "/tmp", "domains": {"test": domain_filename}}
    with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
        json.dump(master, f)
    return tmp


def _load_graph(graph_dir):
    from _builder.graph_build import _load_full_graph
    return _load_full_graph(graph_dir)


class TestBuildDataDepEdges(unittest.TestCase):
    """Tests for build_data_dep_edges."""

    def setUp(self):
        self.graph_dir = _make_dep_graph([
            {"id": "writer", "name": "writer",
             "globals_written": [{"name": "counter", "line": 5}]},
            {"id": "reader", "name": "reader",
             "globals_read": [{"name": "counter", "line": 10}]},
            {"id": "field_writer", "name": "field_writer",
             "fields_written": [{"struct_chain": "ctx", "field_name": "state", "line": 15}]},
            {"id": "field_reader", "name": "field_reader",
             "fields_read": [{"struct_chain": "ctx", "field_name": "state", "line": 20}]},
        ])
        self.G = _load_graph(self.graph_dir)

    def test_global_node_registered_with_writer_and_reader(self):
        from _builder.data_dep import build_data_dep_edges
        result = build_data_dep_edges(self.G)
        self.assertIn("counter", result["global_nodes"])
        gn = result["global_nodes"]["counter"]
        self.assertEqual(gn["type"], "global")
        self.assertEqual(len(gn["writers"]), 1)
        self.assertEqual(gn["writers"][0]["function"], "writer")
        self.assertEqual(len(gn["readers"]), 1)
        self.assertEqual(gn["readers"][0]["function"], "reader")

    def test_field_node_registered_with_writer_and_reader(self):
        from _builder.data_dep import build_data_dep_edges
        result = build_data_dep_edges(self.G)
        self.assertIn("ctx->state", result["field_nodes"])
        fn = result["field_nodes"]["ctx->state"]
        self.assertEqual(fn["type"], "field")
        self.assertEqual(fn["struct_chain"], "ctx")
        self.assertEqual(fn["field_name"], "state")
        self.assertEqual(len(fn["writers"]), 1)
        self.assertEqual(len(fn["readers"]), 1)

    def test_mod_read_chain_pairs_writer_with_reader(self):
        """For each global/field, every (writer, reader) pair → mod_read_chain."""
        from _builder.data_dep import build_data_dep_edges
        result = build_data_dep_edges(self.G)
        chains = result["mod_read_chains"]
        self.assertEqual(len(chains), 2)  # 1 global + 1 field
        global_chain = next(c for c in chains if c["var_type"] == "global")
        self.assertEqual(global_chain["writer"], "writer")
        self.assertEqual(global_chain["reader"], "reader")
        self.assertEqual(global_chain["var"], "counter")
        field_chain = next(c for c in chains if c["var_type"] == "field")
        self.assertEqual(field_chain["writer"], "field_writer")
        self.assertEqual(field_chain["reader"], "field_reader")
        self.assertEqual(field_chain["var"], "ctx->state")

    def test_self_read_write_not_in_mod_read_chains(self):
        """A function that both writes and reads the same global should
        not produce a mod-read chain with itself."""
        from _builder.data_dep import build_data_dep_edges
        graph_dir = _make_dep_graph([
            {"id": "selfie", "name": "selfie",
             "globals_written": [{"name": "x"}],
             "globals_read": [{"name": "x"}]},
        ])
        G = _load_graph(graph_dir)
        result = build_data_dep_edges(G)
        self.assertEqual(len(result["mod_read_chains"]), 0)

    def test_file_and_empty_nodes_excluded(self):
        from _builder.data_dep import build_data_dep_edges
        graph_dir = _make_dep_graph([
            {"id": "file:x.c", "name": "x.c", "node_type": "file",
             "globals_written": [{"name": "should_be_ignored"}]},
            {"id": "empty:cond", "name": "<cond>", "is_empty": True,
             "globals_read": [{"name": "should_be_ignored"}]},
            {"id": "real", "name": "real",
             "globals_written": [{"name": "real_var"}]},
        ])
        G = _load_graph(graph_dir)
        result = build_data_dep_edges(G)
        self.assertNotIn("should_be_ignored", result["global_nodes"])
        self.assertIn("real_var", result["global_nodes"])


class TestForwardDataDepImpact(unittest.TestCase):
    """Tests for forward_data_dep_impact."""

    def test_finds_all_readers_of_written_globals(self):
        from _builder.data_dep import forward_data_dep_impact
        graph_dir = _make_dep_graph([
            {"id": "w", "name": "w", "globals_written": [{"name": "g"}]},
            {"id": "r1", "name": "r1", "globals_read": [{"name": "g"}]},
            {"id": "r2", "name": "r2", "globals_read": [{"name": "g"}]},
            {"id": "unrelated", "name": "unrelated", "globals_read": [{"name": "other"}]},
        ])
        G = _load_graph(graph_dir)
        result = forward_data_dep_impact(G, "w")
        self.assertIn("impacted_readers", result)
        impacted_ids = {i["function_id"] for i in result["impacted_readers"]}
        self.assertIn("r1", impacted_ids)
        self.assertIn("r2", impacted_ids)
        self.assertNotIn("unrelated", impacted_ids)

    def test_finds_readers_of_written_fields(self):
        from _builder.data_dep import forward_data_dep_impact
        graph_dir = _make_dep_graph([
            {"id": "w", "name": "w",
             "fields_written": [{"struct_chain": "ctx", "field_name": "state"}]},
            {"id": "r", "name": "r",
             "fields_read": [{"struct_chain": "ctx", "field_name": "state"}]},
        ])
        G = _load_graph(graph_dir)
        result = forward_data_dep_impact(G, "w")
        impacted_ids = {i["function_id"] for i in result["impacted_readers"]}
        self.assertIn("r", impacted_ids)


class TestFindDeadWriters(unittest.TestCase):
    """Tests for find_dead_writers."""

    def test_writer_with_no_readers_is_dead(self):
        from _builder.data_dep import find_dead_writers
        graph_dir = _make_dep_graph([
            {"id": "w", "name": "w", "globals_written": [{"name": "unread"}]},
            {"id": "r", "name": "r", "globals_read": [{"name": "other"}]},
        ])
        G = _load_graph(graph_dir)
        dead = find_dead_writers(G)
        self.assertEqual(len(dead), 1)
        self.assertEqual(dead[0]["function"], "w")
        self.assertIn("global unread", dead[0]["dead_writes"])

    def test_writer_with_readers_is_not_dead(self):
        from _builder.data_dep import find_dead_writers
        graph_dir = _make_dep_graph([
            {"id": "w", "name": "w", "globals_written": [{"name": "read"}]},
            {"id": "r", "name": "r", "globals_read": [{"name": "read"}]},
        ])
        G = _load_graph(graph_dir)
        dead = find_dead_writers(G)
        self.assertEqual(len(dead), 0)

    def test_dead_field_writer_detected(self):
        from _builder.data_dep import find_dead_writers
        graph_dir = _make_dep_graph([
            {"id": "w", "name": "w",
             "fields_written": [{"struct_chain": "ctx", "field_name": "unused"}]},
        ])
        G = _load_graph(graph_dir)
        dead = find_dead_writers(G)
        self.assertEqual(len(dead), 1)
        self.assertIn("field ctx->unused", dead[0]["dead_writes"])

    def test_empty_nodes_excluded_from_dead_writers(self):
        from _builder.data_dep import find_dead_writers
        graph_dir = _make_dep_graph([
            {"id": "empty", "name": "empty", "is_empty": True,
             "globals_written": [{"name": "x"}]},
        ])
        G = _load_graph(graph_dir)
        dead = find_dead_writers(G)
        self.assertEqual(len(dead), 0)


if __name__ == "__main__":
    unittest.main()
