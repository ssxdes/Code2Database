"""Tests for cgdb_ingest (L1-L2 + FTS5).

Verifies that extract_cgdb_batch() correctly converts a scan result dict
(from ClangScanner or DualBackendScanner) into an IngestBatch that can be
written by SQLiteCGDBStore.
"""
import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.cgdb.cgdb_ingest import extract_cgdb_batch, file_id_for
from _builder.cgdb.cgdb_records import IngestBatch, NodeRecord, EdgeRecord, TypeRecord
from _builder.cgdb.cgdb_store import SQLiteCGDBStore


_TEST_C_SOURCE = textwrap.dedent("""\
    struct buffer {
        char *data;
        int size;
    };

    int validate(struct buffer *buf) {
        if (buf == NULL) return -1;
        return 0;
    }

    int main(int argc, char **argv) {
        struct buffer b = {0, 0};
        return validate(&b);
    }
""")


class TestCgdbIngest(unittest.TestCase):
    """Test extract_cgdb_batch from a synthesized scan result."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.c_path = os.path.join(self.tmpdir, "test_ingest.c")
        with open(self.c_path, 'w') as f:
            f.write(_TEST_C_SOURCE)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_scan_result(self):
        """Use ClangScanner to produce a real scan result for ingest testing."""
        try:
            from _scanner.clang_scanner import ClangScanner, is_clang_available
        except ImportError:
            self.skipTest("ClangScanner not available")
        if not is_clang_available():
            self.skipTest("libclang not available")
        scanner = ClangScanner(is_cpp=False)
        return scanner.scan_file(self.c_path, self.tmpdir)

    def test_file_id_for_is_stable(self):
        """file_id_for produces the same ID for the same path."""
        id1 = file_id_for("/foo/bar.c")
        id2 = file_id_for("/foo/bar.c")
        self.assertEqual(id1, id2)

    def test_file_id_for_high_bit_set(self):
        """File IDs are in the 60-bit range (fit in SQLite signed 64-bit INTEGER)."""
        fid = file_id_for("/foo/bar.c")
        # Must fit in signed 64-bit INTEGER (SQLite constraint)
        self.assertLess(fid, 0x7FFF_FFFF_FFFF_FFFF)
        self.assertGreaterEqual(fid, 0)

    def test_file_id_for_different_paths_different(self):
        """Different paths give different file IDs."""
        id1 = file_id_for("/foo/bar.c")
        id2 = file_id_for("/foo/baz.c")
        self.assertNotEqual(id1, id2)

    def test_extract_cgdb_batch_returns_ingest_batch(self):
        """extract_cgdb_batch returns an IngestBatch instance."""
        result = self._make_scan_result()
        batch = extract_cgdb_batch(result)
        self.assertIsInstance(batch, IngestBatch)

    def test_extract_cgdb_batch_has_file_record(self):
        """Batch has a FileRecord with the correct path."""
        result = self._make_scan_result()
        batch = extract_cgdb_batch(result)
        self.assertIsNotNone(batch.file)
        self.assertEqual(batch.file.path, self.c_path)
        self.assertEqual(batch.file.language, 'c')
        # content_hash should be a non-empty SHA-256
        self.assertTrue(batch.file.content_hash)
        self.assertEqual(len(batch.file.content_hash), 64)

    def test_extract_cgdb_batch_has_nodes(self):
        """Batch has NodeRecords converted from cgdb_nodes."""
        result = self._make_scan_result()
        batch = extract_cgdb_batch(result)
        self.assertGreater(len(batch.nodes), 0)
        # Should contain function nodes for 'validate' and 'main'
        function_nodes = [n for n in batch.nodes if n.kind == 'function']
        names = {n.name for n in function_nodes}
        self.assertIn('validate', names)
        self.assertIn('main', names)

    def test_extract_cgdb_batch_has_types(self):
        """Batch has TypeRecords converted from cgdb_types."""
        result = self._make_scan_result()
        batch = extract_cgdb_batch(result)
        self.assertGreater(len(batch.types), 0)
        # The struct buffer should produce a type record
        spellings = {t.spelling for t in batch.types}
        self.assertTrue(any('buffer' in s for s in spellings),
                        f"Expected 'buffer' in type spellings: {spellings}")

    def test_extract_cgdb_batch_has_edges(self):
        """Batch has EdgeRecords converted from cgdb_edges."""
        result = self._make_scan_result()
        batch = extract_cgdb_batch(result)
        # Should have at least INVOKES or HAS_FIELD edges
        self.assertGreater(len(batch.edges), 0)
        edge_kinds = {e.kind for e in batch.edges}
        self.assertTrue('INVOKES' in edge_kinds or 'HAS_FIELD' in edge_kinds)

    def test_extract_cgdb_batch_writes_to_store(self):
        """End-to-end: extract batch from scan, write to SQLiteCGDBStore."""
        result = self._make_scan_result()
        batch = extract_cgdb_batch(result)
        db_path = os.path.join(self.tmpdir, "cgdb_ingest.db")
        store = SQLiteCGDBStore(db_path)
        store.create_schema()
        store.write_batch(batch)
        store.close()
        # Reopen and verify
        store2 = SQLiteCGDBStore(db_path)
        # File should be there
        file_row = store2._ensure_conn().execute(
            "SELECT path FROM cgdb_files WHERE path = ?", (self.c_path,)
        ).fetchone()
        self.assertIsNotNone(file_row)
        # Should have nodes
        node_count = store2._ensure_conn().execute(
            "SELECT COUNT(*) FROM cgdb_nodes"
        ).fetchone()[0]
        self.assertGreater(node_count, 0)
        # Should have types
        type_count = store2._ensure_conn().execute(
            "SELECT COUNT(*) FROM cgdb_types"
        ).fetchone()[0]
        self.assertGreater(type_count, 0)
        store2.close()

    def test_extract_cgdb_batch_synthesizes_from_legacy_when_no_cgdb(self):
        """When scan_result has no cgdb_nodes (tree-sitter only path),
        extract_cgdb_batch synthesizes NodeRecords from legacy functions."""
        # Simulate a tree-sitter-only result
        scan_result = {
            'file': self.c_path,
            'functions': [
                {'id': 'root.foo', 'name': 'foo', 'line_number': 10,
                 'signature': 'int foo()', 'body_text': 'return 0;'},
                {'id': 'root.bar', 'name': 'bar', 'line_number': 20,
                 'signature': 'int bar()', 'body_text': ''},
            ],
            'edges': [
                {'invoker_id': 'root.foo', 'invoked_id': 'root.bar',
                 'relation': 'INVOKES', 'line': 11},
            ],
            # No cgdb_* keys
        }
        batch = extract_cgdb_batch(scan_result)
        self.assertGreater(len(batch.nodes), 0)
        self.assertGreater(len(batch.edges), 0)
        # Verify synthesized function nodes
        function_names = {n.name for n in batch.nodes if n.kind == 'function'}
        self.assertIn('foo', function_names)
        self.assertIn('bar', function_names)
        # Synthesized nodes should have legacy_function_id set
        legacy_ids = {n.legacy_function_id for n in batch.nodes
                      if n.legacy_function_id}
        self.assertIn('root.foo', legacy_ids)

    def test_extract_cgdb_batch_synthesizes_edges_from_scanner_keys(self):
        """Scanner legacy edges use "source"/"target" keys (c_scanner.py);
        _synthesize_from_legacy must resolve them, not only invoker_id."""
        scan_result = {
            'file': self.c_path,
            'functions': [
                {'id': 'root.foo', 'name': 'foo', 'line_number': 10,
                 'signature': 'int foo()', 'body_text': 'return 0;'},
            ],
            'edges': [
                {'source': 'root.foo', 'target': 'ext.helper',
                 'relation': 'INVOKES', 'line': 11},
            ],
        }
        batch = extract_cgdb_batch(scan_result)
        edge_kinds = {e.kind for e in batch.edges}
        self.assertIn('INVOKES', edge_kinds,
                      "legacy edge with source/target keys was dropped")
        # The external callee must be synthesized as a node so the edge
        # has both endpoints.
        names = {n.name for n in batch.nodes}
        self.assertIn('helper', names)

    def test_ops_binding_dropped_when_its_edge_is_orphaned(self):
        """A binding whose edge lost an endpoint to the node filter must be
        dropped, or write_batch trips the ops_bindings.edge_id FK and the
        whole file's cgdb layer is rolled back."""
        A, B, C, FIELD = 111, 222, 333, 444
        MISSING = 999
        scan_result = {
            'file': self.c_path,
            'cgdb_nodes': [
                {'id': A, 'kind': 'function', 'name': 'a', 'fqn': 'a',
                 'file_id': 1, 'source_layer': 'analysis', 'confidence': 0.9},
                {'id': B, 'kind': 'variable', 'name': 'ops', 'fqn': 'ops',
                 'file_id': 1, 'source_layer': 'analysis', 'confidence': 0.9},
                {'id': C, 'kind': 'function', 'name': 'c', 'fqn': 'c',
                 'file_id': 1, 'source_layer': 'analysis', 'confidence': 0.9},
                {'id': FIELD, 'kind': 'variable', 'name': 'read',
                 'fqn': 'read', 'file_id': 1, 'source_layer': 'analysis',
                 'confidence': 0.9},
            ],
            'cgdb_edges': [
                # This OPS_BIND edge references MISSING, which has no
                # cgdb_node — the node filter drops it.
                {'edge_id': 5001, 'src_id': B, 'dst_id': MISSING,
                 'kind': 'OPS_BIND', 'file_id': 1},
            ],
            'cgdb_ops_bindings': [
                # All three node refs are valid, but edge_id 5001 will not
                # survive the edge filter.
                {'edge_id': 5001, 'ops_table_id': B,
                 'field_node_id': FIELD, 'impl_function_id': C,
                 'signature_match': True},
            ],
        }
        batch = extract_cgdb_batch(scan_result)
        self.assertEqual(
            [e.edge_id for e in batch.edges if e.kind == 'OPS_BIND'],
            [], "orphaned OPS_BIND edge should have been filtered")
        self.assertEqual(
            [o.edge_id for o in batch.ops_bindings],
            [], "binding referencing a dropped edge must be filtered")
        # The surviving batch must write cleanly under FK enforcement.
        db_path = os.path.join(self.tmpdir, "cgdb_orphan_binding.db")
        store = SQLiteCGDBStore(db_path)
        store.create_schema()
        store.write_batch(batch)
        store.close()


class TestSqliteStoreCgdbIntegration(unittest.TestCase):
    """Test that SQLiteStore (legacy) now applies cgdb schema."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_sqlite_store_creates_cgdb_tables(self):
        """SQLiteStore.connect() creates cgdb tables alongside legacy ones."""
        from _builder.graph.sqlite_store import SQLiteStore
        db_path = os.path.join(self.tmpdir, "test.db")
        store = SQLiteStore(db_path)
        store.connect()
        # Legacy tables should exist
        tables = [r[0] for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
        self.assertIn("functions", tables)
        self.assertIn("edges", tables)
        # cgdb tables should also exist (added by apply_cgdb_schema)
        self.assertIn("cgdb_nodes", tables)
        self.assertIn("cgdb_types", tables)
        self.assertIn("cgdb_edges", tables)
        self.assertIn("basic_blocks", tables)
        self.assertIn("ops_bindings", tables)
        self.assertIn("config_predicates", tables)
        # FTS5 virtual table
        self.assertIn("nodes_fts", tables)
        store.close()

    def test_sqlite_store_schema_version_bumped(self):
        """Schema version is bumped to 5 (cgdb schema applied)."""
        from _builder.graph.sqlite_store import SQLiteStore
        db_path = os.path.join(self.tmpdir, "test.db")
        store = SQLiteStore(db_path)
        store.connect()
        self.assertGreaterEqual(SQLiteStore.SCHEMA_VERSION, 5)
        store.close()


if __name__ == "__main__":
    unittest.main()
