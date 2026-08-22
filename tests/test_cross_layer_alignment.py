"""Smoke tests for cross-layer FK integrity in the cgdb schema.

Verifies that the foreign-key columns referenced by design-report layer
tables actually exist in the schema DDL. Does NOT require libclang — the
schema is applied to an in-memory SQLite db and columns are inspected via
PRAGMA table_info.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.cgdb_schema import (
    apply_cgdb_schema,
    get_cgdb_schema_version,
    CGDB_SCHEMA_VERSION,
)


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn):
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(conn):
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}


class TestSchemaImport(unittest.TestCase):
    """Verify the schema module imports cleanly."""

    def test_module_imports_cleanly(self):
        import _builder.cgdb_schema as cs
        self.assertTrue(hasattr(cs, 'apply_cgdb_schema'))
        self.assertTrue(hasattr(cs, 'CGDB_SCHEMA_VERSION'))
        self.assertGreater(CGDB_SCHEMA_VERSION, 0)


class TestSchemaApplication(unittest.TestCase):
    """apply_cgdb_schema is idempotent and sets the version."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.db_path = os.path.join(self.tmpdir, 'code2database.db')
        self.conn = sqlite3.connect(self.db_path)

    def _cleanup(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_apply_creates_tables(self):
        apply_cgdb_schema(self.conn)
        tables = _tables(self.conn)
        for required in ['meta', 'cgdb_files', 'cgdb_nodes', 'cgdb_edges',
                         'cgdb_types', 'config_predicates', 'conditions',
                         'basic_blocks', 'cfg_edges', 'data_flow',
                         'alias_sets', 'invoke_sites', 'ops_bindings',
                         'sync_primitives', 'happens_before', 'cgdb_includes',
                         'doc_comments', 'node_metadata', 'edge_metadata']:
            self.assertIn(required, tables, f'missing table: {required}')

    def test_apply_sets_version(self):
        apply_cgdb_schema(self.conn)
        self.assertEqual(get_cgdb_schema_version(self.conn), CGDB_SCHEMA_VERSION)

    def test_apply_is_idempotent(self):
        apply_cgdb_schema(self.conn)
        apply_cgdb_schema(self.conn)  # second call must not error
        self.assertEqual(get_cgdb_schema_version(self.conn), CGDB_SCHEMA_VERSION)


class TestCrossLayerFKColumns(unittest.TestCase):
    """Verify cross-layer FK references in the v4 layer tables exist.

    Each FK column should exist on its host table so that L1↔L2↔L3↔L4
    cross-layer joins resolve correctly.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.db_path = os.path.join(self.tmpdir, 'code2database.db')
        self.conn = sqlite3.connect(self.db_path)
        apply_cgdb_schema(self.conn)

    def _cleanup(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_tokens_l1_to_cgdb_files_l1(self):
        cols = _columns(self.conn, 'tokens')
        self.assertIn('file_id', cols)
        self.assertIn('ast_node_id', cols)  # L1↔L2 alignment

    def test_cgdb_nodes_l2_to_files_and_versions(self):
        cols = _columns(self.conn, 'cgdb_nodes')
        self.assertIn('file_id', cols)
        self.assertIn('type_id', cols)
        self.assertIn('config_predicate_id', cols)
        self.assertIn('first_seen_version', cols)
        self.assertIn('last_seen_version', cols)
        self.assertIn('enclosing_symbol_id', cols)

    def test_cgdb_edges_l2_fk_columns(self):
        cols = _columns(self.conn, 'cgdb_edges')
        for c in ['src_id', 'dst_id', 'file_id', 'condition_id',
                  'config_predicate_id', 'enclosing_symbol_id']:
            self.assertIn(c, cols)

    def test_invoke_sites_l7_to_nodes(self):
        cols = _columns(self.conn, 'invoke_sites')
        for c in ['invoker_id', 'invoked_id', 'invoke_expr_id']:
            self.assertIn(c, cols)

    def test_ops_bindings_l7_to_edges_and_nodes(self):
        cols = _columns(self.conn, 'ops_bindings')
        for c in ['edge_id', 'ops_table_id', 'field_node_id', 'impl_function_id']:
            self.assertIn(c, cols)

    def test_ir_functions_l3_to_symbols_and_blocks(self):
        cols = _columns(self.conn, 'ir_functions')
        for c in ['symbol_id', 'entry_block_id']:
            self.assertIn(c, cols)

    def test_indirect_calls_l3_to_edges_and_functions(self):
        cols = _columns(self.conn, 'indirect_calls')
        for c in ['call_edge_id', 'function_id', 'possible_target_symbol_id']:
            self.assertIn(c, cols)

    def test_call_graph_reachability_l4_to_nodes(self):
        cols = _columns(self.conn, 'call_graph_reachability')
        self.assertIn('source_symbol_id', cols)
        self.assertIn('target_symbol_id', cols)
        self.assertIn('config_predicate_id', cols)

    def test_history_snapshots_l4_to_files_and_versions(self):
        cols = _columns(self.conn, 'history_snapshots')
        self.assertIn('source_file_id', cols)
        self.assertIn('graph_version_id', cols)

    def test_alignment_errors_layer_field(self):
        cols = _columns(self.conn, 'alignment_errors')
        self.assertIn('layer', cols)
        self.assertIn('table_name', cols)
        self.assertIn('row_id', cols)

    def test_cross_lang_bindings_bridge(self):
        cols = _columns(self.conn, 'cross_lang_bindings')
        self.assertIn('from_symbol_id', cols)
        self.assertIn('to_symbol_id', cols)
        self.assertIn('ffi_kind', cols)

    def test_ffi_call_sites_to_bindings_and_files(self):
        cols = _columns(self.conn, 'ffi_call_sites')
        self.assertIn('binding_id', cols)
        self.assertIn('file_id', cols)
        self.assertIn('at_token_id', cols)


class TestFTSAndViews(unittest.TestCase):
    """Verify FTS5 virtual tables and views are present."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.db_path = os.path.join(self.tmpdir, 'code2database.db')
        self.conn = sqlite3.connect(self.db_path)
        apply_cgdb_schema(self.conn)

    def _cleanup(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_nodes_fts_virtual_table(self):
        tables = _tables(self.conn)
        self.assertIn('nodes_fts', tables)

    def test_comments_fts_virtual_table(self):
        tables = _tables(self.conn)
        self.assertIn('comments_fts', tables)

    def test_cdb_nodes_view(self):
        views = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'")}
        self.assertIn('cdb_nodes', views)


if __name__ == '__main__':
    unittest.main()
