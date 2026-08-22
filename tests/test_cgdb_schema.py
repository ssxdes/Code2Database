"""Tests for cgdb schema (Phase 0 Foundation).

Verifies that apply_cgdb_schema creates all 13-layer tables, FTS5 virtual
table works, triggers fire correctly, and the schema is idempotent.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.cgdb_schema import (
    apply_cgdb_schema, CGDB_SCHEMA_VERSION,
    get_cgdb_schema_version, needs_cgdb_migration,
)


class TestSchemaCreation(unittest.TestCase):
    """Test that apply_cgdb_schema creates all expected tables."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "cgdb_test.db")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _apply(self):
        conn = sqlite3.connect(self.db_path)
        apply_cgdb_schema(conn)
        return conn

    def test_creates_all_layer_tables(self):
        """All 13-layer cgdb tables are created."""
        conn = self._apply()
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
        # L0
        self.assertIn("graph_versions", tables)
        self.assertIn("meta", tables)
        # L1
        self.assertIn("cgdb_files", tables)
        self.assertIn("cgdb_nodes", tables)
        self.assertIn("cgdb_edges", tables)
        # L2
        self.assertIn("cgdb_types", tables)
        # L3
        self.assertIn("conditions", tables)
        # L3.5
        self.assertIn("config_predicates", tables)
        # L4
        self.assertIn("basic_blocks", tables)
        self.assertIn("cfg_edges", tables)
        # L5
        self.assertIn("data_flow", tables)
        self.assertIn("alias_sets", tables)
        # L7
        self.assertIn("invoke_sites", tables)
        self.assertIn("ops_bindings", tables)
        # L8
        self.assertIn("sync_primitives", tables)
        self.assertIn("happens_before", tables)
        # L9
        self.assertIn("cgdb_includes", tables)
        # FTS5
        self.assertIn("nodes_fts", tables)
        conn.close()

    def test_creates_all_indexes(self):
        """Key indexes are created."""
        conn = self._apply()
        indexes = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
        ).fetchall()]
        self.assertIn("idx_cgdb_nodes_kind", indexes)
        self.assertIn("idx_cgdb_nodes_name", indexes)
        self.assertIn("idx_cgdb_nodes_fqn", indexes)
        self.assertIn("idx_cgdb_edges_src_kind", indexes)
        self.assertIn("idx_cgdb_edges_dst_kind", indexes)
        self.assertIn("idx_pred_text", indexes)
        self.assertIn("idx_opsbind_field", indexes)
        self.assertIn("idx_blocks_function", indexes)
        conn.close()

    def test_creates_triggers(self):
        """FTS5 sync triggers are created."""
        conn = self._apply()
        triggers = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name"
        ).fetchall()]
        self.assertIn("cgdb_nodes_ai", triggers)
        self.assertIn("cgdb_nodes_ad", triggers)
        self.assertIn("cgdb_nodes_au", triggers)
        conn.close()

    def test_creates_view(self):
        """cdb_nodes view is created."""
        conn = self._apply()
        views = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        ).fetchall()]
        self.assertIn("cdb_nodes", views)
        conn.close()

    def test_schema_version_recorded(self):
        """Schema version is stored in meta."""
        conn = self._apply()
        self.assertEqual(get_cgdb_schema_version(conn), CGDB_SCHEMA_VERSION)
        self.assertFalse(needs_cgdb_migration(conn))
        conn.close()

    def test_idempotent(self):
        """Calling apply_cgdb_schema twice doesn't error."""
        conn = self._apply()
        # Second call should not error
        apply_cgdb_schema(conn)
        self.assertEqual(get_cgdb_schema_version(conn), CGDB_SCHEMA_VERSION)
        conn.close()


class TestFTS5Search(unittest.TestCase):
    """Test FTS5 full-text search over cgdb_nodes."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "cgdb_fts.db")
        self.addCleanup(self._cleanup)
        conn = sqlite3.connect(self.db_path)
        apply_cgdb_schema(conn)
        # Insert default version 1
        conn.execute(
            "INSERT INTO graph_versions (version_id, commit_hash, compiled_at) "
            "VALUES (1, 'initial', 0)"
        )
        # Insert test nodes with denormalized signature/body_text columns
        conn.execute(
            "INSERT INTO cgdb_nodes (id, kind, name, fqn, line, col, byte_start, byte_end, "
            "signature, body_text, attrs) "
            "VALUES (1, 'function', 'foo', 'foo', 1, 1, 0, 10, "
            "'int foo()', 'return 0;', '{}')"
        )
        conn.execute(
            "INSERT INTO cgdb_nodes (id, kind, name, fqn, line, col, byte_start, byte_end, "
            "signature, body_text, attrs) "
            "VALUES (2, 'function', 'bar', 'bar', 1, 1, 0, 10, "
            "'int bar(int x)', '', '{}')"
        )
        conn.execute(
            "INSERT INTO cgdb_nodes (id, kind, name, fqn, line, col, byte_start, byte_end, attrs) "
            "VALUES (3, 'var', 'counter', 'counter', 5, 5, 50, 60, '{}')"
        )
        conn.commit()
        self.conn = conn

    def _cleanup(self):
        import shutil
        if hasattr(self, 'conn'):
            self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_search_by_name(self):
        """Search for 'foo' returns the foo node."""
        rows = self.conn.execute(
            "SELECT name FROM nodes_fts WHERE nodes_fts MATCH 'foo'"
        ).fetchall()
        self.assertEqual([r[0] for r in rows], ["foo"])

    def test_search_by_signature_term(self):
        """Search for 'int' returns both foo and bar (signatures contain 'int')."""
        rows = self.conn.execute(
            "SELECT name FROM nodes_fts WHERE nodes_fts MATCH 'int'"
        ).fetchall()
        names = {r[0] for r in rows}
        self.assertIn("foo", names)
        self.assertIn("bar", names)

    def test_search_returns_column_values(self):
        """FTS5 returns column values, not just rowids."""
        rows = self.conn.execute(
            "SELECT name, signature FROM nodes_fts WHERE nodes_fts MATCH 'foo'"
        ).fetchall()
        self.assertEqual(rows[0], ("foo", "int foo()"))

    def test_search_no_match(self):
        """Search for non-existent term returns empty."""
        rows = self.conn.execute(
            "SELECT name FROM nodes_fts WHERE nodes_fts MATCH 'nonexistent'"
        ).fetchall()
        self.assertEqual(rows, [])

    def test_delete_removes_from_fts(self):
        """Deleting a node removes it from FTS index."""
        self.conn.execute("DELETE FROM cgdb_nodes WHERE name = 'foo'")
        rows = self.conn.execute(
            "SELECT name FROM nodes_fts WHERE nodes_fts MATCH 'foo'"
        ).fetchall()
        self.assertEqual(rows, [])

    def test_update_syncs_fts(self):
        """Updating a node's name, fqn, and signature updates the FTS index."""
        # Update name, fqn, and signature — all FTS-indexed columns
        self.conn.execute(
            "UPDATE cgdb_nodes SET name = 'qux', fqn = 'qux', signature = 'int qux()' "
            "WHERE name = 'foo'"
        )
        # Old name 'foo' should no longer match anywhere
        rows = self.conn.execute(
            "SELECT name FROM nodes_fts WHERE nodes_fts MATCH 'foo'"
        ).fetchall()
        self.assertEqual(rows, [])
        # New name 'qux' should match
        rows = self.conn.execute(
            "SELECT name FROM nodes_fts WHERE nodes_fts MATCH 'qux'"
        ).fetchall()
        self.assertEqual([r[0] for r in rows], ["qux"])


class TestNodeKindCheckConstraint(unittest.TestCase):
    """Test that cgdb_nodes.kind CHECK constraint accepts valid kinds and rejects invalid."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "cgdb_check.db")
        self.addCleanup(self._cleanup)
        conn = sqlite3.connect(self.db_path)
        apply_cgdb_schema(conn)
        conn.execute(
            "INSERT INTO graph_versions (version_id, commit_hash, compiled_at) "
            "VALUES (1, 'initial', 0)"
        )
        conn.commit()
        self.conn = conn

    def _cleanup(self):
        import shutil
        if hasattr(self, 'conn'):
            self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_valid_kinds_accepted(self):
        """All listed node kinds are accepted by CHECK constraint."""
        valid_kinds = [
            'function', 'method', 'constructor', 'destructor',
            'var', 'parm', 'field', 'enum_constant', 'typedef',
            'struct', 'class', 'union', 'enum',
            'stmt', 'expr', 'decl_ref', 'member_ref',
            'label', 'namespace', 'template', 'concept',
            'file', 'macro', 'include',
            'vtable', 'ops_table',
        ]
        for i, kind in enumerate(valid_kinds):
            self.conn.execute(
                "INSERT INTO cgdb_nodes (id, kind, name, fqn, line, col, byte_start, byte_end, attrs) "
                f"VALUES ({1000+i}, ?, 'name{i}', 'fqn{i}', 1, 1, 0, 10, '{{}}')",
                (kind,)
            )
        self.conn.commit()
        count = self.conn.execute("SELECT COUNT(*) FROM cgdb_nodes").fetchone()[0]
        self.assertEqual(count, len(valid_kinds))

    def test_invalid_kind_rejected(self):
        """Invalid kind is rejected by CHECK constraint."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO cgdb_nodes (kind, name, fqn, line, col, byte_start, byte_end, attrs) "
                "VALUES ('invalid_kind', 'x', 'x', 1, 1, 0, 10, '{}')"
            )


class TestEdgeKindCheckConstraint(unittest.TestCase):
    """Test that cgdb_edges.kind CHECK constraint accepts valid kinds."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "cgdb_edge.db")
        self.addCleanup(self._cleanup)
        conn = sqlite3.connect(self.db_path)
        apply_cgdb_schema(conn)
        conn.execute(
            "INSERT INTO graph_versions (version_id, commit_hash, compiled_at) "
            "VALUES (1, 'initial', 0)"
        )
        # Insert two nodes for edges
        conn.execute(
            "INSERT INTO cgdb_nodes (id, kind, name, fqn, line, col, byte_start, byte_end, attrs) "
            "VALUES (1, 'function', 'a', 'a', 1, 1, 0, 10, '{}')"
        )
        conn.execute(
            "INSERT INTO cgdb_nodes (id, kind, name, fqn, line, col, byte_start, byte_end, attrs) "
            "VALUES (2, 'function', 'b', 'b', 1, 1, 0, 10, '{}')"
        )
        conn.commit()
        self.conn = conn

    def _cleanup(self):
        import shutil
        if hasattr(self, 'conn'):
            self.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_valid_edge_kinds(self):
        """OPS_BIND, INVOKES, READS, etc. are all accepted."""
        valid_kinds = [
            'INVOKES', 'OPS_BIND', 'READS', 'WRITES',
            'ALLOCATES', 'FREES', 'LOCKS', 'UNLOCKS',
            'NEXT', 'BRANCHES',
            'HAS_FIELD', 'HAS_PARAM', 'HAS_LOCAL',
            'RETURNS', 'DECLARES', 'REFERENCES',
            'OVERRIDES', 'IMPLEMENTS', 'INSTANTIATES',
            'THROWS', 'IMPORTS', 'MACRO_EXPANDS_TO',
            'FFI_BINDS', 'FFI_INVOKES',
        ]
        for kind in valid_kinds:
            self.conn.execute(
                "INSERT INTO cgdb_edges (src_id, dst_id, kind) VALUES (1, 2, ?)",
                (kind,)
            )
        self.conn.commit()
        count = self.conn.execute("SELECT COUNT(*) FROM cgdb_edges").fetchone()[0]
        self.assertEqual(count, len(valid_kinds))

    def test_invalid_edge_kind_rejected(self):
        """Invalid edge kind is rejected."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO cgdb_edges (src_id, dst_id, kind) VALUES (1, 2, 'INVALID')"
            )


if __name__ == "__main__":
    unittest.main()
