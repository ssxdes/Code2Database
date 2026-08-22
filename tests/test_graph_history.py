"""Tests for graph history versioning (D9)."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder import graph_history
from _builder.graph_history import (
    record_version, list_versions, get_version, node_history,
    graph_diff, _ensure_history_db, _load_nodes_from_dir, _load_edges_from_dir,
    _attr_diff, _edge_key,
)


def _write_graph_dir(tmpdir, nodes, edges, master_domains=None):
    """Write a minimal graph dir with code2database_master.json + domain file."""
    if master_domains is None:
        master_domains = {"test": "domain_test.json"}
    master = {
        "type": "code2database_master",
        "version": 1,
        "domains": master_domains,
    }
    with open(os.path.join(tmpdir, "code2database_master.json"), "w") as f:
        json.dump(master, f)
    # Write each domain file (all nodes/edges in first domain for simplicity)
    for domain, fname in master_domains.items():
        dom_data = {"nodes": nodes, "edges": edges}
        with open(os.path.join(tmpdir, fname), "w") as f:
            json.dump(dom_data, f)


class TestRecordVersion(unittest.TestCase):
    """Test record_version and list_versions."""

    def test_record_version_returns_incrementing_id(self):
        """record_version returns incrementing version_ids."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_graph_dir(tmp, [
                {"id": "n1", "name": "foo", "labels": ["API_entry"]},
                {"id": "n2", "name": "bar", "labels": ["thread_processor"]},
            ], [{"source": "n1", "target": "n2", "relation": "INVOKES"}])
            vid1 = record_version(tmp, description="first")
            vid2 = record_version(tmp, description="second")
            self.assertEqual(vid2, vid1 + 1)

    def test_record_version_stores_counts(self):
        """record_version captures node_count and edge_count automatically."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_graph_dir(tmp, [
                {"id": "n1", "name": "foo"},
                {"id": "n2", "name": "bar"},
                {"id": "n3", "name": "baz"},
            ], [{"source": "n1", "target": "n2", "relation": "INVOKES"}])
            vid = record_version(tmp, description="test")
            v = get_version(tmp, vid)
            self.assertIsNotNone(v)
            self.assertEqual(v["node_count"], 3)
            self.assertEqual(v["edge_count"], 1)

    def test_list_versions_returns_newest_first(self):
        """list_versions returns versions ordered newest first."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_graph_dir(tmp, [{"id": "n1", "name": "foo"}], [])
            record_version(tmp, description="v1")
            record_version(tmp, description="v2")
            record_version(tmp, description="v3")
            versions = list_versions(tmp)
            self.assertEqual(len(versions), 3)
            # Newest first → v3's description should be first
            self.assertEqual(versions[0]["description"], "v3")
            self.assertEqual(versions[2]["description"], "v1")

    def test_get_version_returns_none_for_missing(self):
        """get_version returns None for a non-existent version_id."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_graph_dir(tmp, [], [])
            self.assertIsNone(get_version(tmp, 999))

    def test_record_version_with_explicit_counts(self):
        """record_version accepts explicit node/edge counts."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_graph_dir(tmp, [], [])
            vid = record_version(tmp, description="x",
                                 node_count=42, edge_count=7)
            v = get_version(tmp, vid)
            self.assertEqual(v["node_count"], 42)
            self.assertEqual(v["edge_count"], 7)


class TestGraphDiff(unittest.TestCase):
    """Test graph_diff between two graph states."""

    def test_diff_added_nodes(self):
        """Diff detects newly-added nodes."""
        with tempfile.TemporaryDirectory() as tmp:
            dir_a = os.path.join(tmp, "a")
            dir_b = os.path.join(tmp, "b")
            os.makedirs(dir_a)
            os.makedirs(dir_b)
            _write_graph_dir(dir_a, [
                {"id": "n1", "name": "foo", "labels": ["API_entry"]},
            ], [])
            _write_graph_dir(dir_b, [
                {"id": "n1", "name": "foo", "labels": ["API_entry"]},
                {"id": "n2", "name": "bar", "labels": ["thread_processor"]},
            ], [])
            d = graph_diff(tmp, from_path=dir_a, to_path=dir_b)
            self.assertEqual(d["summary"]["added_nodes"], 1)
            self.assertEqual(d["summary"]["removed_nodes"], 0)
            self.assertEqual(d["added_nodes"][0]["id"], "n2")

    def test_diff_removed_nodes(self):
        """Diff detects removed nodes."""
        with tempfile.TemporaryDirectory() as tmp:
            dir_a = os.path.join(tmp, "a")
            dir_b = os.path.join(tmp, "b")
            os.makedirs(dir_a)
            os.makedirs(dir_b)
            _write_graph_dir(dir_a, [
                {"id": "n1", "name": "foo"},
                {"id": "n2", "name": "bar"},
            ], [])
            _write_graph_dir(dir_b, [
                {"id": "n1", "name": "foo"},
            ], [])
            d = graph_diff(tmp, from_path=dir_a, to_path=dir_b)
            self.assertEqual(d["summary"]["removed_nodes"], 1)
            self.assertEqual(d["removed_nodes"][0]["id"], "n2")

    def test_diff_changed_nodes(self):
        """Diff detects attribute changes in nodes."""
        with tempfile.TemporaryDirectory() as tmp:
            dir_a = os.path.join(tmp, "a")
            dir_b = os.path.join(tmp, "b")
            os.makedirs(dir_a)
            os.makedirs(dir_b)
            _write_graph_dir(dir_a, [
                {"id": "n1", "name": "foo", "complexity": 5, "domain": "kern"},
            ], [])
            _write_graph_dir(dir_b, [
                {"id": "n1", "name": "foo", "complexity": 10, "domain": "kern"},
            ], [])
            d = graph_diff(tmp, from_path=dir_a, to_path=dir_b)
            self.assertEqual(d["summary"]["changed_nodes"], 1)
            self.assertEqual(d["changed_nodes"][0]["id"], "n1")
            self.assertIn("complexity", d["changed_nodes"][0]["changes"])
            self.assertEqual(d["changed_nodes"][0]["changes"]["complexity"]["from"], 5)
            self.assertEqual(d["changed_nodes"][0]["changes"]["complexity"]["to"], 10)

    def test_diff_added_edges(self):
        """Diff detects newly-added edges."""
        with tempfile.TemporaryDirectory() as tmp:
            dir_a = os.path.join(tmp, "a")
            dir_b = os.path.join(tmp, "b")
            os.makedirs(dir_a)
            os.makedirs(dir_b)
            _write_graph_dir(dir_a, [
                {"id": "n1", "name": "foo"},
                {"id": "n2", "name": "bar"},
            ], [])
            _write_graph_dir(dir_b, [
                {"id": "n1", "name": "foo"},
                {"id": "n2", "name": "bar"},
            ], [{"source": "n1", "target": "n2", "relation": "INVOKES"}])
            d = graph_diff(tmp, from_path=dir_a, to_path=dir_b)
            self.assertEqual(d["summary"]["added_edges"], 1)
            self.assertEqual(d["summary"]["removed_edges"], 0)

    def test_diff_removed_edges(self):
        """Diff detects removed edges."""
        with tempfile.TemporaryDirectory() as tmp:
            dir_a = os.path.join(tmp, "a")
            dir_b = os.path.join(tmp, "b")
            os.makedirs(dir_a)
            os.makedirs(dir_b)
            _write_graph_dir(dir_a, [
                {"id": "n1", "name": "foo"},
                {"id": "n2", "name": "bar"},
            ], [{"source": "n1", "target": "n2", "relation": "INVOKES"}])
            _write_graph_dir(dir_b, [
                {"id": "n1", "name": "foo"},
                {"id": "n2", "name": "bar"},
            ], [])
            d = graph_diff(tmp, from_path=dir_a, to_path=dir_b)
            self.assertEqual(d["summary"]["removed_edges"], 1)

    def test_diff_no_changes(self):
        """Diff of identical states reports zero changes."""
        with tempfile.TemporaryDirectory() as tmp:
            dir_a = os.path.join(tmp, "a")
            dir_b = os.path.join(tmp, "b")
            os.makedirs(dir_a)
            os.makedirs(dir_b)
            nodes = [{"id": "n1", "name": "foo"}, {"id": "n2", "name": "bar"}]
            edges = [{"source": "n1", "target": "n2", "relation": "INVOKES"}]
            _write_graph_dir(dir_a, nodes, edges)
            _write_graph_dir(dir_b, nodes, edges)
            d = graph_diff(tmp, from_path=dir_a, to_path=dir_b)
            self.assertEqual(d["summary"]["added_nodes"], 0)
            self.assertEqual(d["summary"]["removed_nodes"], 0)
            self.assertEqual(d["summary"]["changed_nodes"], 0)
            self.assertEqual(d["summary"]["added_edges"], 0)
            self.assertEqual(d["summary"]["removed_edges"], 0)


class TestNodeHistory(unittest.TestCase):
    """Test node_history across versions with snapshot dirs."""

    def test_node_history_present_then_absent(self):
        """node_history shows a node present in v1 and absent in v2."""
        with tempfile.TemporaryDirectory() as tmp:
            # Make tmp itself a valid graph dir so _count_graph works
            _write_graph_dir(tmp, [{"id": "n1", "name": "foo"}], [])
            # v1: node exists
            dir_v1 = os.path.join(tmp, "snap_v1")
            os.makedirs(dir_v1)
            _write_graph_dir(dir_v1, [
                {"id": "n1", "name": "foo", "labels": ["API_entry"]},
            ], [])
            record_version(tmp, description="v1", snapshot_path=dir_v1,
                           node_count=1, edge_count=0)
            # v2: node removed
            dir_v2 = os.path.join(tmp, "snap_v2")
            os.makedirs(dir_v2)
            _write_graph_dir(dir_v2, [], [])
            record_version(tmp, description="v2", snapshot_path=dir_v2,
                           node_count=0, edge_count=0)
            history = node_history(tmp, "n1")
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0]["status"], "present")
            self.assertEqual(history[1]["status"], "absent")

    def test_node_history_returns_empty_for_no_versions(self):
        """node_history returns empty list when no versions exist."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(node_history(tmp, "n1"), [])


class TestEdgeKeyAndAttrDiff(unittest.TestCase):
    """Test helper functions _edge_key and _attr_diff."""

    def test_edge_key_normalizes(self):
        """_edge_key normalizes source/target/relation fields."""
        k1 = _edge_key({"source": "n1", "target": "n2", "relation": "INVOKES"})
        k2 = _edge_key({"invoker_id": "n1", "invoked_id": "n2", "relation": "INVOKES"})
        self.assertEqual(k1, k2)

    def test_attr_diff_detects_changes(self):
        """_attr_diff returns dict of changed attributes."""
        old = {"id": "n1", "name": "foo", "complexity": 5}
        new = {"id": "n1", "name": "foo", "complexity": 10}
        d = _attr_diff(old, new)
        self.assertIn("complexity", d)
        self.assertNotIn("id", d)  # id is skipped
        self.assertNotIn("name", d)

    def test_attr_diff_added_attr(self):
        """_attr_diff detects attributes only in new dict."""
        old = {"id": "n1", "name": "foo"}
        new = {"id": "n1", "name": "foo", "complexity": 5}
        d = _attr_diff(old, new)
        self.assertIn("complexity", d)
        self.assertIsNone(d["complexity"]["from"])
        self.assertEqual(d["complexity"]["to"], 5)


class TestLoadFromDir(unittest.TestCase):
    """Test _load_nodes_from_dir and _load_edges_from_dir."""

    def test_load_nodes_from_dir(self):
        """_load_nodes_from_dir returns dict of nodes keyed by id."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_graph_dir(tmp, [
                {"id": "n1", "name": "foo"},
                {"id": "n2", "name": "bar"},
            ], [])
            nodes = _load_nodes_from_dir(tmp)
            self.assertEqual(len(nodes), 2)
            self.assertIn("n1", nodes)
            self.assertEqual(nodes["n1"]["name"], "foo")

    def test_load_edges_from_dir(self):
        """_load_edges_from_dir returns list of edges."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_graph_dir(tmp, [
                {"id": "n1", "name": "foo"},
                {"id": "n2", "name": "bar"},
            ], [
                {"source": "n1", "target": "n2", "relation": "INVOKES"},
                {"source": "n2", "target": "n1", "relation": "INVOKES"},
            ])
            edges = _load_edges_from_dir(tmp)
            self.assertEqual(len(edges), 2)


if __name__ == "__main__":
    unittest.main()
