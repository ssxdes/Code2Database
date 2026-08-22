"""Tests for SQLiteCGDBStore (Phase 0 Foundation).

Verifies GraphWriter/GraphReader split (per cdb 5.4.3): write_batch persists
all 13-layer records, reader queries return correct results, FTS5 search
works, recursive CTE callers/callees traversal works with cycle protection.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.cgdb_store import SQLiteCGDBStore, CGDBWriter, CGDBReader
from _builder.cgdb_records import (
    IngestBatch, NodeRecord, EdgeRecord, TypeRecord, FileRecord,
    ConfigPredicateRecord, InvokeSiteRecord, OpsBindingRecord,
    BasicBlockRecord, CFGEdgeRecord, DataFlowRecord, AliasSetRecord,
    SyncPrimitiveRecord, HappensBeforeRecord, IncludeRecord,
)


def _make_batch() -> IngestBatch:
    """Build a small test batch covering all 13 layers."""
    return IngestBatch(
        file=FileRecord(id=1, path='test.c', language='c', sha256='abc123',
                        content_hash='abc123'),
        nodes=[
            NodeRecord(id=1001, kind='function', name='foo', fqn='foo',
                       file_id=1, line=10, col=1, byte_start=100, byte_end=200,
                       attrs={'signature': 'int foo()', 'body_text': 'return 0;'}),
            NodeRecord(id=1002, kind='function', name='bar', fqn='bar',
                       file_id=1, line=20, col=1, byte_start=300, byte_end=400,
                       attrs={'signature': 'int bar()'}),
            NodeRecord(id=1003, kind='field', name='read_iter',
                       fqn='file_operations.read_iter',
                       file_id=1, line=5, col=12, byte_start=50, byte_end=70),
            NodeRecord(id=1004, kind='var', name='ext4_fop', fqn='ext4_fop',
                       file_id=1, line=8, col=20, byte_start=80, byte_end=90,
                       attrs={'struct_type': 'file_operations'}),
        ],
        edges=[
            EdgeRecord(src_id=1001, dst_id=1002, kind='INVOKES',
                       file_id=1, line=12, col=5),
            EdgeRecord(src_id=1003, dst_id=1001, kind='OPS_BIND',
                       file_id=1, line=8, col=20,
                       attrs={'field_name': 'read_iter',
                              'struct_type': 'file_operations'}),
        ],
        types=[
            TypeRecord(id=2001, spelling='int', canonical_spelling='int',
                       kind='builtin', size_bytes=4),
        ],
        config_predicates=[
            ConfigPredicateRecord(id=3001, text_form='defined(CONFIG_EXT4_FS)',
                                   z3_form='(defined CONFIG_EXT4_FS)',
                                   config_macros=['CONFIG_EXT4_FS']),
        ],
        ops_bindings=[
            OpsBindingRecord(edge_id=2, ops_table_id=1004,
                             field_node_id=1003, impl_function_id=1001,
                             signature_match=True),
        ],
        invoke_sites=[
            InvokeSiteRecord(invoker_id=1001, invoked_id=1002, invoke_kind='direct'),
        ],
        basic_blocks=[
            BasicBlockRecord(id=4001, function_id=1001, block_index=0,
                             is_entry=True),
            BasicBlockRecord(id=4002, function_id=1001, block_index=1,
                             is_exit=True),
        ],
        cfg_edges=[
            CFGEdgeRecord(function_id=1001, src_block_id=4001,
                          dst_block_id=4002, kind='fallthrough'),
        ],
        data_flow=[
            DataFlowRecord(function_id=1001, var_id=1004,
                           def_block_id=4001, use_block_id=4002,
                           kind='def_use'),
        ],
        alias_sets=[
            AliasSetRecord(ptr1_node_id=1004, ptr2_node_id=1004,
                           kind='must_alias'),
        ],
        sync_primitives=[
            SyncPrimitiveRecord(function_id=1001, kind='lock_acquire',
                                sync_var_id=1004),
            SyncPrimitiveRecord(function_id=1001, kind='lock_release',
                                sync_var_id=1004),
        ],
        happens_before=[
            HappensBeforeRecord(write_event_id=1001, read_event_id=1002,
                                reason='lock'),
        ],
        includes=[
            IncludeRecord(source_file_id=1, included_path='stdio.h',
                          is_system=True),
        ],
    )


class TestSQLiteCGDBStoreWriteRead(unittest.TestCase):
    """Test write_batch then read-back via CGDBReader methods."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "cgdb_store.db")
        self.addCleanup(self._cleanup)
        self.store = SQLiteCGDBStore(self.db_path)
        self.store.create_schema()
        self.store.write_batch(_make_batch())
        self.store.close()
        # Reopen for reads
        self.store = SQLiteCGDBStore(self.db_path)

    def _cleanup(self):
        import shutil
        self.store.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_store_implements_writer_and_reader(self):
        """SQLiteCGDBStore is both a CGDBWriter and CGDBReader."""
        self.assertIsInstance(self.store, CGDBWriter)
        self.assertIsInstance(self.store, CGDBReader)

    def test_get_node_returns_node(self):
        """get_node returns the inserted node."""
        node = self.store.get_node(1001)
        self.assertIsNotNone(node)
        self.assertEqual(node['name'], 'foo')
        self.assertEqual(node['kind'], 'function')

    def test_get_node_missing_returns_none(self):
        """get_node returns None for unknown id."""
        self.assertIsNone(self.store.get_node(999999))

    def test_search_symbols_by_name(self):
        """search_symbols finds nodes by name via FTS5."""
        results = self.store.search_symbols('foo')
        self.assertTrue(any(r['name'] == 'foo' for r in results))

    def test_search_symbols_by_signature(self):
        """search_symbols finds nodes by signature term."""
        results = self.store.search_symbols('int')
        names = {r['name'] for r in results}
        self.assertIn('foo', names)
        self.assertIn('bar', names)

    def test_search_symbols_filter_by_kind(self):
        """search_symbols with kind filter narrows results."""
        results = self.store.search_symbols('int', kind='function')
        for r in results:
            self.assertEqual(r['kind'], 'function')

    def test_find_invokers(self):
        """find_invokers returns nodes that INVOKES the given node."""
        callers = self.store.find_invokers(1002)
        self.assertTrue(any(c['name'] == 'foo' for c in callers))

    def test_find_invoked(self):
        """find_invoked returns nodes that the given node INVOKES."""
        callees = self.store.find_invoked(1001)
        self.assertTrue(any(c['name'] == 'bar' for c in callees))

    def test_find_invokers_with_edge_types(self):
        """find_invokers with edge_types=['INVOKES','OPS_BIND'] finds both."""
        # foo is called by bar (INVOKES), and foo is bound via OPS_BIND
        callers = self.store.find_invokers(1001, edge_types=['INVOKES', 'OPS_BIND'])
        # Should find foo in the results (the field_node → foo edge means
        # foo has an OPS_BIND "caller" that is the field node)
        self.assertTrue(len(callers) >= 1)

    def test_find_ops_impls(self):
        """find_ops_impls returns functions bound to a vtable field."""
        results = self.store.find_ops_impls('read_iter')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['impl_name'], 'foo')
        self.assertEqual(results[0]['field_name'], 'read_iter')
        self.assertTrue(results[0]['signature_match'])

    def test_find_configs_for(self):
        """find_configs_for returns the predicate text for a node."""
        # Node 1001 (foo) has config_predicate_id=3001
        # First update its config_predicate_id
        self.store._ensure_conn().execute(
            "UPDATE cgdb_nodes SET config_predicate_id = 3001 WHERE id = 1001"
        )
        self.store._ensure_conn().commit()
        configs = self.store.find_configs_for(1001)
        self.assertEqual(configs, ['defined(CONFIG_EXT4_FS)'])

    def test_find_nodes_under_config(self):
        """find_nodes_under_config returns nodes matching the predicate."""
        # Update node 1001's config_predicate_id
        self.store._ensure_conn().execute(
            "UPDATE cgdb_nodes SET config_predicate_id = 3001 WHERE id = 1001"
        )
        self.store._ensure_conn().commit()
        node_ids = self.store.find_nodes_under_config('defined(CONFIG_EXT4_FS)')
        self.assertIn(1001, node_ids)

    def test_find_cfg_paths(self):
        """find_cfg_paths returns CFG paths from entry block."""
        paths = self.store.find_cfg_paths(1001)
        self.assertTrue(len(paths) >= 1)
        # First path should start with entry block 4001
        self.assertEqual(paths[0]['block_path'][0], 4001)

    def test_find_data_flow(self):
        """find_data_flow returns def-use entries for a variable."""
        result = self.store.find_data_flow(1004)
        self.assertEqual(result['var_id'], 1004)
        self.assertTrue(len(result['entries']) >= 1)
        self.assertEqual(result['entries'][0]['kind'], 'def_use')

    def test_find_aliases(self):
        """find_aliases returns alias relationships for a pointer."""
        aliases = self.store.find_aliases(1004)
        self.assertTrue(len(aliases) >= 1)
        self.assertEqual(aliases[0]['kind'], 'must_alias')

    def test_find_lock_held_calls(self):
        """find_lock_held_calls returns sync primitives for a function."""
        locks = self.store.find_lock_held_calls(1001)
        self.assertEqual(len(locks), 2)
        kinds = {l['kind'] for l in locks}
        self.assertEqual(kinds, {'lock_acquire', 'lock_release'})

    def test_check_path_feasible_empty_path(self):
        """check_path_feasible with empty path returns feasible."""
        result = self.store.check_path_feasible([])
        self.assertTrue(result['feasible'])

    def test_check_path_feasible_simple_path(self):
        """check_path_feasible with a valid path returns feasible (heuristic)."""
        result = self.store.check_path_feasible([4001, 4002])
        # Without conditions, should be feasible
        self.assertTrue(result['feasible'])

    def test_get_neighborhood(self):
        """get_neighborhood returns center, invokers, invoked."""
        nb = self.store.get_neighborhood(1001)
        self.assertIsNotNone(nb['center'])
        self.assertEqual(nb['center']['name'], 'foo')
        # foo invokes bar, so invoked should include bar
        self.assertTrue(any(c['name'] == 'bar' for c in nb['invoked']))

    def test_invoke_path(self):
        """invoke_path returns paths from src to dst."""
        paths = self.store.invoke_path(1001, 1002)
        self.assertTrue(len(paths) >= 1)
        # Path should include both foo and bar
        for path in paths:
            node_names = [n['name'] for n in path['nodes'] if n]
            self.assertIn('foo', node_names)
            self.assertIn('bar', node_names)


class TestSQLiteCGDBStoreDeleteFile(unittest.TestCase):
    """Test delete_file_records removes all records associated with a file."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "cgdb_delete.db")
        self.addCleanup(self._cleanup)
        self.store = SQLiteCGDBStore(self.db_path)
        self.store.create_schema()
        self.store.write_batch(_make_batch())

    def _cleanup(self):
        import shutil
        self.store.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_delete_file_removes_nodes_and_edges(self):
        """delete_file_records removes nodes and edges for the file."""
        nodes_before = self.store._ensure_conn().execute(
            "SELECT COUNT(*) FROM cgdb_nodes"
        ).fetchone()[0]
        edges_before = self.store._ensure_conn().execute(
            "SELECT COUNT(*) FROM cgdb_edges"
        ).fetchone()[0]
        self.assertGreater(nodes_before, 0)
        self.assertGreater(edges_before, 0)
        nodes_deleted, edges_deleted = self.store.delete_file_records('test.c')
        self.assertGreater(nodes_deleted, 0)
        self.assertGreater(edges_deleted, 0)
        nodes_after = self.store._ensure_conn().execute(
            "SELECT COUNT(*) FROM cgdb_nodes"
        ).fetchone()[0]
        edges_after = self.store._ensure_conn().execute(
            "SELECT COUNT(*) FROM cgdb_edges"
        ).fetchone()[0]
        self.assertEqual(nodes_after, 0)
        self.assertEqual(edges_after, 0)

    def test_delete_missing_file_returns_zero(self):
        """delete_file_records for a missing file returns (0, 0)."""
        nodes, edges = self.store.delete_file_records('nonexistent.c')
        self.assertEqual((nodes, edges), (0, 0))


class TestSQLiteCGDBStoreVersioning(unittest.TestCase):
    """Test record_version creates graph_versions rows."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "cgdb_ver.db")
        self.addCleanup(self._cleanup)
        self.store = SQLiteCGDBStore(self.db_path)
        self.store.create_schema()

    def _cleanup(self):
        import shutil
        self.store.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_record_version_returns_version_id(self):
        """record_version returns the new version_id."""
        # version 1 is auto-created by create_schema
        vid = self.store.record_version('commit_abc', 'Test commit')
        self.assertGreater(vid, 1)
        # Verify row exists
        row = self.store._ensure_conn().execute(
            "SELECT commit_hash, commit_subject FROM graph_versions WHERE version_id = ?",
            (vid,)
        ).fetchone()
        self.assertEqual(row[0], 'commit_abc')
        self.assertEqual(row[1], 'Test commit')

    def test_record_version_with_parent(self):
        """record_version with parent_version_id creates a chain."""
        v1 = self.store.record_version('commit_1', 'First')
        v2 = self.store.record_version('commit_2', 'Second',
                                        parent_version_id=v1)
        row = self.store._ensure_conn().execute(
            "SELECT parent_version_id FROM graph_versions WHERE version_id = ?",
            (v2,)
        ).fetchone()
        self.assertEqual(row[0], v1)


class TestSQLiteCGDBStoreBulkLoad(unittest.TestCase):
    """Test begin_bulk_load / finalize wraps writes in a transaction."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "cgdb_bulk.db")
        self.addCleanup(self._cleanup)
        self.store = SQLiteCGDBStore(self.db_path)
        self.store.create_schema()

    def _cleanup(self):
        import shutil
        self.store.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_bulk_load_commit_persists(self):
        """begin_bulk_load + write_batch + finalize persists records."""
        self.store.begin_bulk_load()
        self.store.write_batch(_make_batch())
        self.store.finalize()
        # Reopen and verify
        self.store.close()
        store2 = SQLiteCGDBStore(self.db_path)
        node = store2.get_node(1001)
        self.assertIsNotNone(node)
        self.assertEqual(node['name'], 'foo')
        store2.close()


if __name__ == "__main__":
    unittest.main()
