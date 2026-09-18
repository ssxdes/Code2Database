"""Tests for the cgdb bulk-load index deferral.

A whole-graph rebuild appends millions of rows through write_batch().
begin_bulk_load() drops the non-unique secondary indexes and the cgdb_nodes
FTS5 sync triggers for the load; finalize()/abort_bulk_load() re-create
them. These tests pin every leg of that contract:

  - what is dropped (non-unique indexes on bulk-written tables only;
    UNIQUE indexes stay live so INSERT OR IGNORE / OR REPLACE dedup
    semantics keep working during the load)
  - what is restored (indexes, FTS triggers, per-connection PRAGMAs)
  - the crash-recovery leg (a checkpoint-committed segment survives
    ROLLBACK, so abort must recreate the dropped indexes explicitly)
  - data + FTS correctness after a bulk roundtrip
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.cgdb.cgdb_schema import (
    bulk_index_registry, nodes_fts_trigger_registry,
)
from _builder.cgdb.cgdb_store import SQLiteCGDBStore, _BULK_WRITTEN_TABLES
from _builder.cgdb.cgdb_records import IngestBatch, NodeRecord, FileRecord


# Indexes whose UNIQUE constraint write_batch() dedup depends on — these
# must NEVER be dropped by begin_bulk_load().
_UNIQUE_INDEXES = (
    "idx_cgdb_nodes_unique", "idx_cgdb_edges_unique", "idx_alias_pair",
    "idx_node_metadata_unique", "idx_edge_metadata_unique",
)
# A representative slice of the droppable set (one per hot table).
_DROPPABLE_SAMPLE = (
    "idx_cgdb_nodes_kind", "idx_cgdb_nodes_fqn",
    "idx_cgdb_edges_src_kind", "idx_cgdb_edges_kind",
    "idx_cgdb_types_spelling", "idx_cgdb_files_hash",
    "idx_blocks_function", "idx_doc_comments_node",
)
# Indexes on tables the bulk load never writes (L1 token layer) — the
# per-file DELETE ... WHERE file_id = ? pattern of L1 ingest requires
# these to stay live at all times.
_NON_BULK_INDEXES = ("idx_tokens_file_line", "idx_macros_name")


def _make_node_batch(node_id, path=None, name='bulk_fn'):
    # Distinct path per batch: production derives file ids from the path
    # hash, so two batches never write the same path with different ids
    # (the UNIQUE(path) constraint would otherwise make INSERT OR REPLACE
    # cascade-delete the first file and orphan its nodes).
    if path is None:
        path = f'file{node_id}.c'
    return IngestBatch(
        file=FileRecord(id=node_id, path=path, language='c',
                        sha256='h1', content_hash='h1'),
        nodes=[NodeRecord(id=node_id, kind='function', name=name,
                          fqn=name, file_id=node_id, line=1, col=1,
                          byte_start=0, byte_end=10,
                          attrs={'signature': f'int {name}()'})],
    )


class _TempStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.db_path = os.path.join(self.tmpdir, "bulk_defer.db")
        self.store = SQLiteCGDBStore(self.db_path)
        self.store.create_schema()

    def _cleanup(self):
        import shutil
        self.store.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- helpers ----------------------------------------------------------

    def _index_names(self, conn):
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}

    def _trigger_names(self, conn):
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")}

    def _conn(self):
        return self.store._ensure_conn()


class TestBulkIndexRegistry(_TempStoreTestCase):
    """The registry derives itself from the schema DDL — pin its contract."""

    def test_registry_excludes_unique_indexes(self):
        names = {n for n, _ in bulk_index_registry(_BULK_WRITTEN_TABLES)}
        for u in _UNIQUE_INDEXES:
            self.assertNotIn(u, names)

    def test_registry_excludes_non_bulk_tables(self):
        names = {n for n, _ in bulk_index_registry(_BULK_WRITTEN_TABLES)}
        for i in _NON_BULK_INDEXES:
            self.assertNotIn(i, names)

    def test_registry_covers_bulk_written_tables(self):
        tables = {t for _n, t, _s in
                  __import__('_builder.cgdb.cgdb_schema',
                             fromlist=['_CGDB_INDEX_REGISTRY'])
                  ._CGDB_INDEX_REGISTRY
                  if t in _BULK_WRITTEN_TABLES}
        self.assertTrue(tables)  # sanity: parse found something
        # Every bulk-written table with indexes in the DDL is represented.
        per_table = {}
        for n, s in bulk_index_registry(_BULK_WRITTEN_TABLES):
            m = s.split(" ON ")[1].split("(")[0].strip()
            per_table.setdefault(m, set()).add(n)
        for t in tables:
            self.assertIn(t, per_table, f"no registry entry for {t}")

    def test_fts_trigger_registry_exact(self):
        self.assertEqual(
            {n for n, _ in nodes_fts_trigger_registry()},
            {"cgdb_nodes_ai", "cgdb_nodes_ad", "cgdb_nodes_au"},
        )


class TestBeginBulkLoadDrops(_TempStoreTestCase):
    def test_begin_drops_secondary_indexes_and_fts_triggers(self):
        before_idx = self._index_names(self._conn())
        before_trig = self._trigger_names(self._conn())
        self.store.begin_bulk_load()
        try:
            during_idx = self._index_names(self._conn())
            during_trig = self._trigger_names(self._conn())
            # Non-unique secondary indexes on bulk tables: dropped.
            for i in _DROPPABLE_SAMPLE:
                self.assertNotIn(i, during_idx)
            # UNIQUE indexes: still live (dedup semantics).
            for u in _UNIQUE_INDEXES:
                self.assertIn(u, during_idx)
            # FTS sync triggers: dropped (finalize rebuilds nodes_fts instead).
            for t in ("cgdb_nodes_ai", "cgdb_nodes_ad", "cgdb_nodes_au"):
                self.assertNotIn(t, during_trig)
            # Indexes on non-bulk tables: untouched.
            for i in _NON_BULK_INDEXES:
                self.assertIn(i, during_idx)
            self.assertTrue(before_idx - during_idx)
            self.assertTrue(before_trig - during_trig)
        finally:
            self.store.abort_bulk_load()

    def test_bulk_pragmas_active_during_load(self):
        self.store.begin_bulk_load()
        try:
            conn = self._conn()
            self.assertEqual(
                conn.execute("PRAGMA cache_size").fetchone()[0], -524288)
            self.assertEqual(
                conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("PRAGMA synchronous").fetchone()[0], 0)
        finally:
            self.store.abort_bulk_load()


class TestFinalizeRestores(_TempStoreTestCase):
    def test_finalize_restores_indexes_triggers_and_pragmas(self):
        before_idx = self._index_names(self._conn())
        before_trig = self._trigger_names(self._conn())
        self.store.begin_bulk_load()
        self.store.write_batch(_make_node_batch(7001))
        self.store.finalize()
        self.assertEqual(self._index_names(self._conn()), before_idx)
        self.assertEqual(self._trigger_names(self._conn()), before_trig)
        conn = self._conn()
        self.assertEqual(
            conn.execute("PRAGMA cache_size").fetchone()[0], -65536)
        self.assertEqual(
            conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 1000)
        self.assertEqual(
            conn.execute("PRAGMA synchronous").fetchone()[0], 1)
        self.assertFalse(self.store._bulk_load_active)

    def test_bulk_roundtrip_data_and_fts(self):
        """Nodes written with triggers dropped must land in cgdb_nodes AND
        be findable via nodes_fts after the finalize rebuild."""
        self.store.begin_bulk_load()
        self.store.write_batch(_make_node_batch(7002, name='alpha_bulk_fn'))
        self.store.write_batch(_make_node_batch(7003, name='beta_bulk_fn'))
        self.store.finalize()
        self.assertIsNotNone(self.store.get_node(7002))
        self.assertIsNotNone(self.store.get_node(7003))
        hits = self.store.search_symbols('alpha_bulk_fn')
        self.assertTrue(any(h['id'] == 7002 for h in hits))

    def test_write_batch_without_bulk_keeps_indexes(self):
        before_idx = self._index_names(self._conn())
        self.store.write_batch(_make_node_batch(7004))
        self.assertEqual(self._index_names(self._conn()), before_idx)
        self.assertIsNotNone(self.store.get_node(7004))


class TestAbortRestores(_TempStoreTestCase):
    def test_abort_after_checkpoint_restores_committed_drops(self):
        """Drops committed by commit_bulk_checkpoint survive ROLLBACK —
        abort_bulk_load must recreate them explicitly."""
        before_idx = self._index_names(self._conn())
        before_trig = self._trigger_names(self._conn())
        self.store.begin_bulk_load()
        self.store.write_batch(_make_node_batch(7005))
        self.store.commit_bulk_checkpoint()
        self.store.write_batch(_make_node_batch(7006))
        self.store.abort_bulk_load()
        self.assertEqual(self._index_names(self._conn()), before_idx)
        self.assertEqual(self._trigger_names(self._conn()), before_trig)
        conn = self._conn()
        self.assertEqual(
            conn.execute("PRAGMA cache_size").fetchone()[0], -65536)
        self.assertEqual(
            conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 1000)

    def test_abort_rolls_back_data_and_restores_schema(self):
        before_idx = self._index_names(self._conn())
        self.store.begin_bulk_load()
        self.store.write_batch(_make_node_batch(7007))
        self.store.abort_bulk_load()
        self.assertEqual(self._index_names(self._conn()), before_idx)
        self.assertIsNone(self.store.get_node(7007))


class TestCrashedBulkHeals(_TempStoreTestCase):
    def test_finalize_on_fresh_store_recreates_committed_drops(self):
        """Simulate a build killed after a committed checkpoint segment: the
        drops are persisted in the db, the last open segment rolled back by
        connection close. A later finalize() (no begin) must heal the db."""
        before_idx = self._index_names(self._conn())
        self.store.begin_bulk_load()
        self.store.write_batch(_make_node_batch(7008))
        self.store.commit_bulk_checkpoint()  # drops + data committed
        # Hard-close without abort/finalize (last segment rolls back).
        self.store._conn.close()
        self.store._conn = None
        # Reopen on the same db file: the committed drops are still there.
        reopened = self._conn()
        self.assertTrue(before_idx - self._index_names(reopened))
        # finalize() with no active bulk load heals the schema.
        self.store.finalize()
        self.assertEqual(self._index_names(reopened), before_idx)


class TestBulkDedupSemantics(_TempStoreTestCase):
    """write_batch dedups re-derived rows via INSERT OR IGNORE: the build
    re-emits global records (types, predicates, metadata) plus per-file lazy
    backfill with identical hash-derived ids, and a re-derived duplicate
    must collapse to one row — not replace it, and never duplicate it."""

    def _two_identical_batches(self):
        b = _make_node_batch(7101, name='dedup_fn')
        b2 = _make_node_batch(7101, name='dedup_fn')
        return b, b2

    def test_duplicate_batch_in_bulk_keeps_one_row(self):
        b, b2 = self._two_identical_batches()
        self.store.begin_bulk_load()
        self.store.write_batch(b)
        self.store.write_batch(b2)
        self.store.finalize()
        n = self._conn().execute(
            "SELECT COUNT(*) FROM cgdb_nodes WHERE id = 7101").fetchone()[0]
        f = self._conn().execute(
            "SELECT COUNT(*) FROM cgdb_files WHERE id = 7101").fetchone()[0]
        self.assertEqual(n, 1)
        self.assertEqual(f, 1)

    def test_duplicate_batch_without_bulk_keeps_one_row(self):
        b, b2 = self._two_identical_batches()
        self.store.write_batch(b)
        self.store.write_batch(b2)
        n = self._conn().execute(
            "SELECT COUNT(*) FROM cgdb_nodes WHERE id = 7101").fetchone()[0]
        self.assertEqual(n, 1)

    def test_reingest_same_file_replaces_content(self):
        """The production re-derive pattern: same file id + path, updated
        node attributes. First writer wins, so the original row survives
        (all writers derive from the same source, so identical content)."""
        b1 = _make_node_batch(7102, name='orig_fn')
        b2 = _make_node_batch(7102, name='orig_fn')
        self.store.begin_bulk_load()
        self.store.write_batch(b1)
        self.store.commit_bulk_checkpoint()
        self.store.write_batch(b2)
        self.store.finalize()
        node = self.store.get_node(7102)
        self.assertIsNotNone(node)
        self.assertEqual(node['name'], 'orig_fn')
        n = self._conn().execute(
            "SELECT COUNT(*) FROM cgdb_nodes WHERE id = 7102").fetchone()[0]
        self.assertEqual(n, 1)


    def test_null_commit_hash_lands_as_unknown(self):
        """FileRecord/EdgeRecord default commit_hash to None but the schema
        column is NOT NULL — under OR IGNORE the row would be silently
        dropped (REPLACE used to substitute the default). The writer must
        coerce None → 'unknown' so files and edges always land."""
        b = _make_node_batch(7103, name='no_hash_fn')
        b.edges = [__import__('_builder.cgdb.cgdb_records',
                              fromlist=['EdgeRecord']).EdgeRecord(
            src_id=7103, dst_id=7103, kind='INVOKES', file_id=7103)]
        self.store.begin_bulk_load()
        self.store.write_batch(b)
        self.store.finalize()
        conn = self._conn()
        fh = conn.execute(
            "SELECT commit_hash FROM cgdb_files WHERE id = 7103").fetchone()
        eh = conn.execute(
            "SELECT commit_hash FROM cgdb_edges WHERE src_id = 7103").fetchone()
        nh = conn.execute(
            "SELECT commit_hash FROM cgdb_nodes WHERE id = 7103").fetchone()
        self.assertEqual(fh[0], 'unknown')
        self.assertEqual(eh[0], 'unknown')
        self.assertEqual(nh[0], 'unknown')


if __name__ == "__main__":
    unittest.main()
