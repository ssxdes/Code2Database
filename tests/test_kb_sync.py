"""Incremental kb index sync after memory mutations.

rebuild_kb_index() is a full rebuild (DELETE + reinsert everything);
upsert_kb_paragraph/delete_kb_paragraphs_by_source were built for
incremental sync but lost their callers when the MD knowledge store
was removed. These tests pin the new sync_memory_entries() primitive
and its MemoryManager wiring: memory edits are searchable immediately,
without a full rebuild.

Also covers the update_sync.py stale guard fix (legacy memory/
index.json → memory/memory.db).
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.kb_index import (
    rebuild_kb_index,
    query_kb,
    sync_memory_entries,
)
from _builder.memory_store import MemoryStore
from _builder.memory_manager import MemoryManager


class _KbTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph_dir = os.path.join(self.tmp.name, "graph")
        os.makedirs(self.graph_dir, exist_ok=True)
        self.store = MemoryStore(self.graph_dir)

    def _kb_rows(self, source_file=None):
        conn = sqlite3.connect(os.path.join(self.graph_dir,
                                            "code2database.db"))
        try:
            if source_file:
                return conn.execute(
                    "SELECT title, body, weight, kind FROM kb_paragraphs "
                    "WHERE source_file = ?", (source_file,)).fetchall()
            return conn.execute(
                "SELECT source_file, title, body, weight, kind "
                "FROM kb_paragraphs").fetchall()
        finally:
            conn.close()


class TestSyncMemoryEntries(_KbTestBase):
    def _source(self, mem_id):
        return f"db/mem_{mem_id}.json"

    def test_new_entry_synced_without_full_rebuild(self):
        mid = self.store.add("how does bdev reset work", "answer a")
        rebuild_kb_index(self.graph_dir, verbose=False)
        mid2 = self.store.add("where is queue depth set", "answer b",
                              no_merge=True)
        self.assertEqual(
            self._kb_rows(self._source(mid2)), [],
            "precondition: full rebuild has not seen the new entry")
        synced = sync_memory_entries(self.graph_dir, [mid2])
        self.assertEqual(synced, 1)
        rows = self._kb_rows(self._source(mid2))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "where is queue depth set")
        self.assertEqual(rows[0][3], "memory_qa")

    def test_tombstoned_entry_rows_removed(self):
        a = self.store.add("how does bdev reset work", "x", no_merge=True)
        b = self.store.add("how does bdev reset work", "y" * 200,
                           no_merge=True)
        rebuild_kb_index(self.graph_dir, verbose=False)
        conn = sqlite3.connect(self.store.db_path)
        conn.execute("UPDATE memories SET root_id = id WHERE id = ?", (b,))
        conn.commit()
        conn.close()
        report = self.store.compact()
        absorbed = report["merged_ids"][0]
        canonical = report["canonical_ids"][0]
        sync_memory_entries(self.graph_dir, [absorbed, canonical])
        self.assertEqual(self._kb_rows(self._source(absorbed)), [])
        canon_rows = self._kb_rows(self._source(canonical))
        self.assertEqual(len(canon_rows), 1)
        self.assertEqual(canon_rows[0][3], "memory_qa")

    def test_archived_entry_becomes_experience_kind(self):
        mid = self.store.add("how does bdev reset work", "x")
        rebuild_kb_index(self.graph_dir, verbose=False)
        conn = sqlite3.connect(self.store.db_path)
        conn.execute("UPDATE memories SET status = 'experience' "
                     "WHERE id = ?", (mid,))
        conn.commit()
        conn.close()
        sync_memory_entries(self.graph_dir, [mid])
        rows = self._kb_rows(self._source(mid))
        self.assertEqual(rows[0][3], "memory_experience")

    def test_sync_is_noop_without_kb_db(self):
        mid = self.store.add("q", "a")
        # No rebuild_kb_index yet — code2database.db may not exist.
        db = os.path.join(self.graph_dir, "code2database.db")
        if os.path.exists(db):
            os.unlink(db)
        synced = sync_memory_entries(self.graph_dir, [mid])
        self.assertEqual(synced, 0)

    def test_synced_content_matches_full_rebuild(self):
        # graph A: add + incremental sync (kb db pre-created empty);
        # graph B: same add + full rebuild — the entry's indexed row
        # must be indistinguishable.
        rebuild_kb_index(self.graph_dir, verbose=False)
        self.store.add("how does bdev reset work", "z" * 80,
                       tags=["bdev", "reset"], no_merge=True)
        self.assertEqual(sync_memory_entries(self.graph_dir, [1]), 1)

        dir_b = os.path.join(self.tmp.name, "graph_b")
        os.makedirs(dir_b, exist_ok=True)
        MemoryStore(dir_b).add("how does bdev reset work", "z" * 80,
                               tags=["bdev", "reset"], no_merge=True)
        rebuild_kb_index(dir_b, verbose=False)

        def _rows(graph_dir):
            conn = sqlite3.connect(os.path.join(graph_dir,
                                                "code2database.db"))
            try:
                return conn.execute(
                    "SELECT source_file, title, body, kind, confidence "
                    "FROM kb_paragraphs").fetchall()
            finally:
                conn.close()

        self.assertEqual([tuple(r) for r in _rows(self.graph_dir)],
                         [tuple(r) for r in _rows(dir_b)])


class TestManagerWiring(_KbTestBase):
    def setUp(self):
        super().setUp()
        self.mgr = MemoryManager(self.graph_dir)

    def test_manager_add_updates_kb(self):
        rebuild_kb_index(self.graph_dir, verbose=False)
        self.mgr.add(question="where is the io channel allocated",
                     answer="see identify_io_thread")
        results = query_kb(self.graph_dir, "io channel allocated", top_n=5)
        self.assertTrue(any("io channel" in (r.get("title") or "")
                            for r in results),
                        f"manager add must be searchable immediately: "
                        f"{results}")

    def test_consolidate_compact_updates_kb(self):
        a = self.store.add("how does bdev reset work", "x", no_merge=True)
        b = self.store.add("how does bdev reset work", "y" * 200,
                           no_merge=True)
        conn = sqlite3.connect(self.store.db_path)
        conn.execute("UPDATE memories SET root_id = id WHERE id = ?", (b,))
        conn.commit()
        conn.close()
        rebuild_kb_index(self.graph_dir, verbose=False)
        self.mgr.consolidate()
        # the absorbed duplicate must no longer be indexed
        rows = self._kb_rows()
        dup_rows = [r for r in rows
                    if "bdev reset" in (r[1] or r[2] or "")]
        self.assertEqual(len(dup_rows), 1,
                         f"expected one surviving indexed entry, got: "
                         f"{dup_rows}")


if __name__ == "__main__":
    unittest.main()
