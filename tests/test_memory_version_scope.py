"""Version identity for memories (branch / release tag).

Every memory carries the code version it was learned on
(version_scope). Queries state the version they are working on:
memories learned on that version rank ahead of equally-scoring
others (ordering, never a filter), and every result carries
version_scope + is_current_scope so non-current entries can be
labeled.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.memory.memory_store import MemoryStore
from _builder.kb.kb_index import rebuild_kb_index, query_kb


class _ScopeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph_dir = os.path.join(self.tmp.name, "store")
        os.makedirs(self.graph_dir)
        self.store = MemoryStore(self.graph_dir)


class TestVersionScopeColumn(_ScopeBase):
    def test_column_present_and_indexed(self):
        import sqlite3
        conn = sqlite3.connect(self.store.db_path)
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(memories)")}
            indexes = {r[1] for r in conn.execute(
                "PRAGMA index_list(memories)")}
        finally:
            conn.close()
        self.assertIn("version_scope", cols)
        self.assertIn("idx_memories_version_scope", indexes)

    def test_legacy_store_gains_column_on_open(self):
        # A store created before the column existed gets it via the
        # idempotent schema pass.
        import shutil
        legacy_dir = os.path.join(self.tmp.name, "legacy")
        os.makedirs(os.path.join(legacy_dir, "memory"), exist_ok=True)
        db = os.path.join(legacy_dir, "memory", "memory.db")
        import sqlite3
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                answer TEXT DEFAULT '',
                category_id INTEGER,
                status TEXT NOT NULL DEFAULT 'active',
                tags TEXT DEFAULT '[]',
                node_ids TEXT DEFAULT '[]',
                chains TEXT DEFAULT '[]',
                knowledge_refs TEXT DEFAULT '[]',
                symbols TEXT DEFAULT '[]',
                author TEXT DEFAULT '',
                root_id INTEGER DEFAULT 0,
                merged_into INTEGER DEFAULT 0,
                split_from INTEGER DEFAULT 0,
                merged_count INTEGER DEFAULT 0,
                reshaped_count INTEGER DEFAULT 0,
                access_count INTEGER DEFAULT 0,
                weight REAL DEFAULT 1.0,
                boost REAL DEFAULT 0.0,
                versions_json TEXT DEFAULT '[]',
                promoted_from TEXT DEFAULT '',
                param_bindings TEXT DEFAULT '{}',
                created TEXT NOT NULL,
                last_accessed TEXT NOT NULL,
                validated_at TEXT NOT NULL,
                invalidated_reason TEXT DEFAULT '',
                invalidated_at TEXT DEFAULT '',
                archived_at TEXT DEFAULT ''
            );
            INSERT INTO memories (question, answer, root_id, created,
                last_accessed, validated_at)
                VALUES ('legacy q', 'legacy a', 1,
                        '2026-01-01', '2026-01-01', '2026-01-01');
        """)
        conn.commit()
        conn.close()
        store = MemoryStore(legacy_dir)
        row = store.get(1)
        self.assertEqual(row["version_scope"], "default")

    def test_add_records_scope(self):
        mid = self.store.add("q main", "a", no_merge=True,
                             version_scope="release/2.0")
        row = self.store.get(mid)
        self.assertEqual(row["version_scope"], "release/2.0")
        # default when omitted
        mid2 = self.store.add("q other", "a", no_merge=True)
        self.assertEqual(self.store.get(mid2)["version_scope"],
                         "default")


class TestSearchScopePriority(_ScopeBase):
    def setUp(self):
        super().setUp()
        # Same-topic memories on two versions with equal weight.
        self.store.add("how does the doorbell ring path work",
                       "answer on main", no_merge=True,
                       version_scope="main")
        self.store.add("how does the doorbell ring path work",
                       "answer on release 2 0", no_merge=True,
                       version_scope="release/2.0")

    def test_current_version_ranks_first(self):
        results = self.store.search("doorbell ring path",
                                    version_scope="release/2.0")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["version_scope"], "release/2.0")
        self.assertTrue(results[0]["is_current_scope"])
        self.assertEqual(results[1]["version_scope"], "main")
        self.assertFalse(results[1]["is_current_scope"])

    def test_without_scope_no_annotation(self):
        results = self.store.search("doorbell ring path")
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertIn("version_scope", r)
            self.assertFalse(r["is_current_scope"])

    def test_other_version_entries_still_returned(self):
        # Ordering only — never a filter.
        results = self.store.search("doorbell ring path",
                                    version_scope="feature/x")
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not r["is_current_scope"] for r in results))


class TestKbQueryScopePriority(_ScopeBase):
    def setUp(self):
        super().setUp()
        self.store.add("how does the doorbell ring path work",
                       "answer on main", no_merge=True,
                       version_scope="main")
        self.store.add("how does the doorbell ring path work",
                       "answer on release 2 0", no_merge=True,
                       version_scope="release/2.0")
        rebuild_kb_index(self.graph_dir, verbose=False)

    def test_kb_query_prefers_current_scope(self):
        results = query_kb(self.graph_dir, "doorbell ring path",
                           version_scope="release/2.0")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["version_scope"], "release/2.0")
        self.assertTrue(results[0]["is_current_scope"])
        self.assertEqual(results[1]["version_scope"], "main")
        self.assertFalse(results[1]["is_current_scope"])

    def test_kb_query_cjk_channel_keeps_scope_order(self):
        results = query_kb(self.graph_dir, "门铃 响铃 路径 如何工作",
                           version_scope="main")
        # The CJK similarity channel runs (no FTS token overlap); the
        # merged set must still honor current-version-first ordering.
        scopes = [r["version_scope"] for r in results]
        if "main" in scopes and "release/2.0" in scopes:
            self.assertEqual(scopes.index("main"),
                             min(scopes.index("main"),
                                 scopes.index("release/2.0")))

    def test_sync_carries_scope(self):
        mid = self.store.add("how is the queue drained", "answer",
                             no_merge=True, version_scope="feature/y")
        from _builder.kb.kb_index import sync_memory_entries
        self.assertEqual(sync_memory_entries(self.graph_dir, [mid]), 1)
        hits = query_kb(self.graph_dir, "queue drained",
                        version_scope="feature/y")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["version_scope"], "feature/y")

    def test_old_kb_store_gains_paragraph_column(self):
        # kb_index.db created before the column: _kb_connect adds it.
        import sqlite3
        db = os.path.join(self.graph_dir, "kb_index.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE kb_paragraphs_old AS "
            "SELECT id, source_kind, source_file, para_index, title, "
            "body, tags, node_ids, weight, confidence, kind, "
            "graph_version, created_at, accessed_at, access_count, "
            "scope_id, canonical_id, principle_ref, embedding "
            "FROM kb_paragraphs")
        conn.execute("DROP TABLE kb_paragraphs")
        conn.execute("ALTER TABLE kb_paragraphs_old "
                     "RENAME TO kb_paragraphs")
        conn.commit()
        conn.close()
        results = query_kb(self.graph_dir, "doorbell")
        # rows survive; scope defaults apply
        for r in results:
            self.assertEqual(r["version_scope"], "default")


if __name__ == "__main__":
    unittest.main()
