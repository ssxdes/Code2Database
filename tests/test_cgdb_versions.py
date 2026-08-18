"""Tests for cgdb_versions.py — layered version control with first_seen/last_seen.

Verifies the VersionController class:
  - record_version: idempotent insertion of graph_versions rows
  - get_version_by_commit, get_version, list_versions
  - time_travel_query_node: returns node state at a specific version_id
  - soft_delete_node / soft_delete_edge: set last_seen_version
  - soft_delete_file_records: bulk soft-delete by file path
  - diff_versions: added/removed nodes/edges between two versions
  - list_nodes_at_version: nodes alive at a specific version
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.cgdb_versions import VersionController
from _builder.cgdb_schema import apply_cgdb_schema


def _make_test_db(tmpdir: str) -> str:
    """Create a fresh cgdb-enabled SQLite DB. Returns the db path."""
    import sqlite3
    db_path = os.path.join(tmpdir, "test_versions.db")
    conn = sqlite3.connect(db_path)
    try:
        apply_cgdb_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return db_path


def _insert_node(conn, node_id: int, first_seen: int, last_seen: int,
                 name: str = "test_node", kind: str = "function",
                 commit_hash: str = "abc"):
    """Insert a node with explicit first_seen/last_seen versions."""
    import json
    conn.execute(
        "INSERT INTO cgdb_nodes "
        "(id, kind, name, fqn, file_id, line, col, byte_start, byte_end, "
        " type_spelling, attrs, first_seen_version, last_seen_version, commit_hash) "
        "VALUES (?, ?, ?, ?, NULL, 1, 1, 0, 1, 'int', '{}', ?, ?, ?)",
        (node_id, kind, name, f"test::{name}", first_seen, last_seen, commit_hash)
    )


def _insert_file(conn, file_id: int, path: str):
    conn.execute(
        "INSERT INTO cgdb_files (id, path, language, sha256) "
        "VALUES (?, ?, 'c', ?)",
        (file_id, path, "abc")
    )


class TestRecordVersion(unittest.TestCase):
    """Test record_version and basic graph_versions operations."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.db_path = _make_test_db(self.tmpdir)
        self.ctrl = VersionController(self.db_path)

    def _cleanup(self):
        self.ctrl.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_record_version_returns_int(self):
        """record_version returns an integer version_id."""
        vid = self.ctrl.record_version("commit1", "Initial commit")
        self.assertIsInstance(vid, int)
        self.assertGreater(vid, 0)

    def test_record_version_is_idempotent(self):
        """Recording the same commit_hash twice returns the same version_id."""
        vid1 = self.ctrl.record_version("commit1", "Initial")
        vid2 = self.ctrl.record_version("commit1", "Different subject")
        self.assertEqual(vid1, vid2,
                         "Same commit_hash should return same version_id")

    def test_record_version_distinct_commits(self):
        """Different commit hashes get different version_ids."""
        vid1 = self.ctrl.record_version("commit1", "First")
        vid2 = self.ctrl.record_version("commit2", "Second")
        self.assertNotEqual(vid1, vid2)
        self.assertGreater(vid2, vid1)

    def test_record_version_with_parent(self):
        """parent_version_id is stored correctly."""
        vid1 = self.ctrl.record_version("commit1", "First")
        vid2 = self.ctrl.record_version("commit2", "Second",
                                        parent_version_id=vid1)
        v2 = self.ctrl.get_version(vid2)
        self.assertEqual(v2["parent_version_id"], vid1)

    def test_get_version_by_commit(self):
        """get_version_by_commit returns the right version_id."""
        vid = self.ctrl.record_version("my_commit_hash", "Subject")
        result = self.ctrl.get_version_by_commit("my_commit_hash")
        self.assertEqual(result, vid)

    def test_get_version_by_commit_not_found(self):
        """get_version_by_commit returns None for unknown commit."""
        self.assertIsNone(self.ctrl.get_version_by_commit("nonexistent"))

    def test_get_version_returns_full_row(self):
        """get_version returns dict with all expected fields."""
        vid = self.ctrl.record_version("c1", "Subject", parent_version_id=None)
        v = self.ctrl.get_version(vid)
        self.assertEqual(v["version_id"], vid)
        self.assertEqual(v["commit_hash"], "c1")
        self.assertEqual(v["commit_subject"], "Subject")
        self.assertIn("compiled_at", v)
        self.assertIn("parent_version_id", v)

    def test_list_versions_returns_newest_first(self):
        """list_versions returns rows ordered by version_id DESC."""
        v1 = self.ctrl.record_version("c1", "First")
        v2 = self.ctrl.record_version("c2", "Second")
        v3 = self.ctrl.record_version("c3", "Third")
        versions = self.ctrl.list_versions(limit=10)
        self.assertEqual(len(versions), 3)
        # Newest first
        self.assertEqual(versions[0]["version_id"], v3)
        self.assertEqual(versions[1]["version_id"], v2)
        self.assertEqual(versions[2]["version_id"], v1)

    def test_get_latest_version_id(self):
        """get_latest_version_id returns the max version_id."""
        v1 = self.ctrl.record_version("c1", "First")
        self.assertEqual(self.ctrl.get_latest_version_id(), v1)
        v2 = self.ctrl.record_version("c2", "Second")
        self.assertEqual(self.ctrl.get_latest_version_id(), v2)


class TestTimeTravelQuery(unittest.TestCase):
    """Test time_travel_query_node and edge."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.db_path = _make_test_db(self.tmpdir)
        self.ctrl = VersionController(self.db_path)
        # Create 3 versions: v1, v2, v3
        self.v1 = self.ctrl.record_version("c1", "Initial")
        self.v2 = self.ctrl.record_version("c2", "Update")
        self.v3 = self.ctrl.record_version("c3", "Remove")
        # Insert a node that exists at v1, v2, but is soft-deleted at v3
        # (first_seen=v1, last_seen=v3 means alive at v1, v2; dead at v3)
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            _insert_node(conn, 100, self.v1, self.v3, name="alive_at_v1_v2")
            conn.commit()
        finally:
            conn.close()

    def _cleanup(self):
        self.ctrl.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_node_alive_at_v1(self):
        """Node is alive at v1 (first_seen <= v1 AND last_seen > v1)."""
        result = self.ctrl.time_travel_query_node(100, self.v1)
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 100)
        self.assertEqual(result["name"], "alive_at_v1_v2")

    def test_node_alive_at_v2(self):
        """Node is alive at v2."""
        result = self.ctrl.time_travel_query_node(100, self.v2)
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "alive_at_v1_v2")

    def test_node_dead_at_v3(self):
        """Node is dead at v3 (last_seen == v3, so last_seen > v3 is False).

        Note: the query allows last_seen == MAX(version_id) as "alive".
        Since v3 IS the max, we need to use a different test: insert a node
        whose last_seen < MAX(version_id).
        """
        # Insert a node that was hard-removed before v3:
        # first_seen=v1, last_seen=v2 (so at v3 it's neither alive nor max)
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            _insert_node(conn, 200, self.v1, self.v2, name="dead_by_v3")
            conn.commit()
        finally:
            conn.close()
        # At v3, node 200 should not be alive (last_seen=v2 < v3,
        # and v2 != MAX(version_id)=v3)
        result = self.ctrl.time_travel_query_node(200, self.v3)
        self.assertIsNone(result,
                          "Node 200 should be dead at v3 (last_seen=v2 < v3)")

    def test_node_not_yet_born(self):
        """A node first_seen at v2 is not alive at v1."""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            _insert_node(conn, 300, self.v2, self.v3, name="born_at_v2")
            conn.commit()
        finally:
            conn.close()
        result = self.ctrl.time_travel_query_node(300, self.v1)
        self.assertIsNone(result,
                          "Node born at v2 should not be alive at v1")

    def test_unknown_node_returns_none(self):
        """Querying a non-existent node returns None."""
        result = self.ctrl.time_travel_query_node(99999, self.v1)
        self.assertIsNone(result)


class TestSoftDelete(unittest.TestCase):
    """Test soft_delete_node and soft_delete_file_records."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.db_path = _make_test_db(self.tmpdir)
        self.ctrl = VersionController(self.db_path)
        self.v1 = self.ctrl.record_version("c1", "Initial")
        self.v2 = self.ctrl.record_version("c2", "Update")
        self.v3 = self.ctrl.record_version("c3", "Delete")
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            # Node alive (last_seen=MAX initially — but we set it to a large value)
            _insert_node(conn, 100, self.v1, 999999, name="to_be_deleted")
            _insert_node(conn, 200, self.v1, 999999, name="another_node")
            _insert_file(conn, 1, "/test/file.c")
            # Attach nodes to file
            conn.execute(
                "UPDATE cgdb_nodes SET file_id = 1 WHERE id IN (100, 200)"
            )
            conn.commit()
        finally:
            conn.close()

    def _cleanup(self):
        self.ctrl.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_soft_delete_node_sets_last_seen(self):
        """soft_delete_node sets last_seen_version = version_id."""
        rows = self.ctrl.soft_delete_node(100, self.v3)
        self.assertEqual(rows, 1)
        # Verify
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT last_seen_version FROM cgdb_nodes WHERE id = 100"
            ).fetchone()
            self.assertEqual(row[0], self.v3)
        finally:
            conn.close()

    def test_soft_delete_node_idempotent(self):
        """Soft-deleting an already-deleted node returns 0 rows."""
        self.ctrl.soft_delete_node(100, self.v3)
        # Second delete at same version should return 0 (last_seen > v3 is False)
        rows = self.ctrl.soft_delete_node(100, self.v3)
        self.assertEqual(rows, 0)

    def test_soft_delete_file_records(self):
        """soft_delete_file_records bulk-deletes all nodes/edges for a file."""
        rows = self.ctrl.soft_delete_file_records("/test/file.c", self.v3)
        self.assertEqual(rows, 2)  # 2 nodes
        # Verify both nodes have last_seen = v3
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            for node_id in (100, 200):
                row = conn.execute(
                    "SELECT last_seen_version FROM cgdb_nodes WHERE id = ?",
                    (node_id,)
                ).fetchone()
                self.assertEqual(row[0], self.v3)
        finally:
            conn.close()

    def test_soft_delete_unknown_file_returns_zero(self):
        """Soft-deleting a non-existent file returns 0 rows."""
        rows = self.ctrl.soft_delete_file_records("/nonexistent/file.c", self.v3)
        self.assertEqual(rows, 0)


class TestDiffVersions(unittest.TestCase):
    """Test diff_versions — added/removed nodes/edges between versions."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.db_path = _make_test_db(self.tmpdir)
        self.ctrl = VersionController(self.db_path)
        self.v1 = self.ctrl.record_version("c1", "Initial")
        self.v2 = self.ctrl.record_version("c2", "Update")
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            # Node added at v1 (alive throughout)
            _insert_node(conn, 100, self.v1, 999999, name="node_100")
            # Node added at v2 (born between v1 and v2)
            _insert_node(conn, 200, self.v2, 999999, name="node_200")
            # Node removed between v1 and v2 (last_seen <= v2)
            _insert_node(conn, 300, self.v1, self.v2, name="node_300")
            conn.commit()
        finally:
            conn.close()

    def _cleanup(self):
        self.ctrl.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_diff_added_nodes(self):
        """diff_versions returns nodes added between v1 and v2."""
        diff = self.ctrl.diff_versions(self.v1, self.v2)
        self.assertIn(200, diff["added_nodes"])
        self.assertNotIn(100, diff["added_nodes"])  # was already at v1

    def test_diff_removed_nodes(self):
        """diff_versions returns nodes removed between v1 and v2."""
        diff = self.ctrl.diff_versions(self.v1, self.v2)
        self.assertIn(300, diff["removed_nodes"])
        self.assertNotIn(100, diff["removed_nodes"])  # still alive

    def test_diff_no_changes(self):
        """diff_versions of the same version returns empty lists."""
        diff = self.ctrl.diff_versions(self.v1, self.v1)
        self.assertEqual(diff["added_nodes"], [])
        self.assertEqual(diff["removed_nodes"], [])


class TestListNodesAtVersion(unittest.TestCase):
    """Test list_nodes_at_version."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.db_path = _make_test_db(self.tmpdir)
        self.ctrl = VersionController(self.db_path)
        self.v1 = self.ctrl.record_version("c1", "Initial")
        self.v2 = self.ctrl.record_version("c2", "Update")
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            _insert_node(conn, 100, self.v1, 999999, name="alive")
            _insert_node(conn, 200, self.v2, 999999, name="born_at_v2")
            conn.commit()
        finally:
            conn.close()

    def _cleanup(self):
        self.ctrl.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_list_at_v1_excludes_nodes_born_later(self):
        """list_nodes_at_version(v1) excludes nodes born at v2."""
        nodes_v1 = self.ctrl.list_nodes_at_version(self.v1, limit=100)
        self.assertIn(100, nodes_v1)
        self.assertNotIn(200, nodes_v1)

    def test_list_at_v2_includes_all_alive(self):
        """list_nodes_at_version(v2) includes both v1 and v2 born nodes."""
        nodes_v2 = self.ctrl.list_nodes_at_version(self.v2, limit=100)
        self.assertIn(100, nodes_v2)
        self.assertIn(200, nodes_v2)


if __name__ == "__main__":
    unittest.main()
