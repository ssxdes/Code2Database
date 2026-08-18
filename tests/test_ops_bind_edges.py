"""Tests for C1 backport: OPS_BIND edges from vtable registrations."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


def _make_extraction(funcs, vtables):
    """Build a minimal extraction dict for build_graph."""
    return {
        "functions": funcs,
        "edges": [],
        "domains": ["root"],
        "lang_stats": {"c": len(funcs)},
        "vtable_registrations": vtables,
    }


class TestOpsBindEdges(unittest.TestCase):
    """Test that build_graph emits explicit OPS_BIND edges."""

    def test_vtable_registration_creates_ops_bind_edge(self):
        """A vtable registration produces an OPS_BIND edge from vtable node to function."""
        from _builder.graph_build import build_graph
        funcs = [
            {"id": "root_ext4_read", "name": "ext4_read",
             "source_file": "fs/ext4/file.c", "line": 10,
             "domain": "root", "labels": []},
        ]
        vtables = [
            {"struct_type": "file_operations", "var_name": "ext4_file_operations",
             "source_file": "fs/ext4/file.c",
             "registrations": [
                 {"field": "read_iter", "func_name": "ext4_read"},
             ]},
        ]
        G, _ = build_graph(_make_extraction(funcs, vtables))
        # Find OPS_BIND edges
        ops_edges = [(u, v, d) for u, v, d in G.edges(data=True)
                      if d.get("relation") == "OPS_BIND"]
        self.assertEqual(len(ops_edges), 1)
        # Source should be the synthetic vtable node
        self.assertEqual(ops_edges[0][0], "vtable::ext4_file_operations")
        # Target should be the function node
        self.assertEqual(ops_edges[0][1], "root_ext4_read")
        # Edge should record the field name
        self.assertEqual(ops_edges[0][2].get("field_name"), "read_iter")
        self.assertEqual(ops_edges[0][2].get("struct_type"), "file_operations")

    def test_multiple_fields_create_multiple_edges(self):
        """A vtable with N registrations creates N OPS_BIND edges."""
        from _builder.graph_build import build_graph
        funcs = [
            {"id": "root_read", "name": "ext4_read",
             "source_file": "fs/ext4/file.c", "line": 10,
             "domain": "root", "labels": []},
            {"id": "root_write", "name": "ext4_write",
             "source_file": "fs/ext4/file.c", "line": 20,
             "domain": "root", "labels": []},
            {"id": "root_open", "name": "ext4_open",
             "source_file": "fs/ext4/file.c", "line": 30,
             "domain": "root", "labels": []},
        ]
        vtables = [
            {"struct_type": "file_operations", "var_name": "ext4_fop",
             "source_file": "fs/ext4/file.c",
             "registrations": [
                 {"field": "read_iter", "func_name": "ext4_read"},
                 {"field": "write_iter", "func_name": "ext4_write"},
                 {"field": "open", "func_name": "ext4_open"},
             ]},
        ]
        G, _ = build_graph(_make_extraction(funcs, vtables))
        ops_edges = [(u, v, d) for u, v, d in G.edges(data=True)
                      if d.get("relation") == "OPS_BIND"]
        self.assertEqual(len(ops_edges), 3)
        fields = {d.get("field_name") for _, _, d in ops_edges}
        self.assertEqual(fields, {"read_iter", "write_iter", "open"})

    def test_vtable_node_created(self):
        """A synthetic vtable node is created in the graph."""
        from _builder.graph_build import build_graph
        funcs = [
            {"id": "root_foo", "name": "foo",
             "source_file": "fs/ext4/file.c", "line": 10,
             "domain": "root", "labels": []},
        ]
        vtables = [
            {"struct_type": "file_operations", "var_name": "my_fop",
             "source_file": "fs/ext4/file.c",
             "registrations": [
                 {"field": "read", "func_name": "foo"},
             ]},
        ]
        G, _ = build_graph(_make_extraction(funcs, vtables))
        self.assertIn("vtable::my_fop", G.nodes)
        ndata = G.nodes["vtable::my_fop"]
        self.assertEqual(ndata.get("kind"), "vtable")
        self.assertEqual(ndata.get("struct_type"), "file_operations")
        self.assertEqual(ndata.get("name"), "my_fop")

    def test_no_ops_bind_when_no_vtable(self):
        """No vtable_registrations → no OPS_BIND edges."""
        from _builder.graph_build import build_graph
        funcs = [
            {"id": "root_foo", "name": "foo",
             "source_file": "src.c", "line": 10,
             "domain": "root", "labels": []},
        ]
        G, _ = build_graph(_make_extraction(funcs, []))
        ops_edges = [(u, v, d) for u, v, d in G.edges(data=True)
                      if d.get("relation") == "OPS_BIND"]
        self.assertEqual(ops_edges, [])

    def test_unknown_function_skipped(self):
        """Vtable registration pointing to unknown function is skipped (no crash)."""
        from _builder.graph_build import build_graph
        funcs = []
        vtables = [
            {"struct_type": "file_operations", "var_name": "fop",
             "source_file": "src.c",
             "registrations": [
                 {"field": "read", "func_name": "missing_func"},
             ]},
        ]
        G, _ = build_graph(_make_extraction(funcs, vtables))
        # No OPS_BIND edge because missing_func is not in graph
        ops_edges = [(u, v, d) for u, v, d in G.edges(data=True)
                      if d.get("relation") == "OPS_BIND"]
        self.assertEqual(ops_edges, [])

    def test_ops_bind_carries_preproc_condition(self):
        """OPS_BIND edge carries the vtable's preproc_condition."""
        from _builder.graph_build import build_graph
        funcs = [
            {"id": "root_foo", "name": "foo",
             "source_file": "src.c", "line": 10,
             "domain": "root", "labels": []},
        ]
        vtables = [
            {"struct_type": "file_operations", "var_name": "fop",
             "source_file": "src.c",
             "condition": "defined(CONFIG_EXT4_FS)",
             "registrations": [
                 {"field": "read", "func_name": "foo"},
             ]},
        ]
        G, _ = build_graph(_make_extraction(funcs, vtables))
        ops_edges = [(u, v, d) for u, v, d in G.edges(data=True)
                      if d.get("relation") == "OPS_BIND"]
        self.assertEqual(len(ops_edges), 1)
        self.assertEqual(ops_edges[0][2].get("preproc_condition"),
                          "defined(CONFIG_EXT4_FS)")
        self.assertTrue(ops_edges[0][2].get("preproc_alive"))

    def test_ops_bind_confidence_is_extracted(self):
        """OPS_BIND edges are tagged EXTRACTED with confidence 1.0."""
        from _builder.graph_build import build_graph
        funcs = [
            {"id": "root_foo", "name": "foo",
             "source_file": "src.c", "line": 10,
             "domain": "root", "labels": []},
        ]
        vtables = [
            {"struct_type": "file_operations", "var_name": "fop",
             "source_file": "src.c",
             "registrations": [
                 {"field": "read", "func_name": "foo"},
             ]},
        ]
        G, _ = build_graph(_make_extraction(funcs, vtables))
        ops_edges = [(u, v, d) for u, v, d in G.edges(data=True)
                      if d.get("relation") == "OPS_BIND"]
        self.assertEqual(ops_edges[0][2].get("confidence"), "EXTRACTED")
        self.assertEqual(ops_edges[0][2].get("source_tag"), "vtable_registration")
        self.assertAlmostEqual(ops_edges[0][2].get("confidence_score", 0), 1.0)

    def test_query_ops_bind_by_field_name(self):
        """After building, we can query 'which functions bind to file_operations.read_iter'."""
        from _builder.graph_build import build_graph
        funcs = [
            {"id": "root_ext4_read", "name": "ext4_read",
             "source_file": "fs/ext4/file.c", "line": 10,
             "domain": "root", "labels": []},
            {"id": "root_xfs_read", "name": "xfs_read",
             "source_file": "fs/xfs/file.c", "line": 10,
             "domain": "root", "labels": []},
        ]
        vtables = [
            {"struct_type": "file_operations", "var_name": "ext4_fop",
             "source_file": "fs/ext4/file.c",
             "registrations": [
                 {"field": "read_iter", "func_name": "ext4_read"},
             ]},
            {"struct_type": "file_operations", "var_name": "xfs_fop",
             "source_file": "fs/xfs/file.c",
             "registrations": [
                 {"field": "read_iter", "func_name": "xfs_read"},
             ]},
        ]
        G, _ = build_graph(_make_extraction(funcs, vtables))
        # Find all functions bound to file_operations.read_iter
        bound = [
            v for u, v, d in G.edges(data=True)
            if d.get("relation") == "OPS_BIND"
            and d.get("field_name") == "read_iter"
            and d.get("struct_type") == "file_operations"
        ]
        self.assertEqual(set(bound), {"root_ext4_read", "root_xfs_read"})


if __name__ == "__main__":
    unittest.main()
