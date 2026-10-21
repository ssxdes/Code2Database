"""Knowledge store: curated project facts in SQLite.

knowledge/knowledge.db is the source of truth for knowledge (typed
rows: hard_rule / mode / abstraction / convention / pitfall /
query_path / description / must_know), physically and logically
separate from the memory store (memory/memory.db, episodic Q&A).
knowledge/brief.json remains as the derived, size-budgeted prompt
view, regenerated after every write; a pre-database brief.json is
imported once.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.kb.knowledge_store import (
    KnowledgeStore, knowledge_db_path, open_knowledge,
)
from _builder.kb.brief import load_brief, save_brief


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph_dir = os.path.join(self.tmp.name, "store")
        os.makedirs(self.graph_dir)

    def _open(self):
        store = KnowledgeStore(self.graph_dir)
        self.addCleanup(store.close)
        return store


class TestCrud(_Base):
    def test_add_list_get(self):
        store = self._open()
        kid = store.add("hard_rule", "lock the queue before touching "
                                    "the tail",
                        extra={"type": "api", "detail": "rq->lock"})
        self.assertGreater(kid, 0)
        items = store.list_items(kind="hard_rule")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "hard_rule")
        self.assertEqual(items[0]["extra"]["type"], "api")
        self.assertEqual(store.get(kid)["body"],
                         "lock the queue before touching the tail")

    def test_unknown_kind_rejected(self):
        store = self._open()
        with self.assertRaises(ValueError):
            store.add("nonsense", "body")

    def test_empty_body_rejected(self):
        store = self._open()
        with self.assertRaises(ValueError):
            store.add("pitfall", "   ")

    def test_revise_in_place(self):
        store = self._open()
        kid = store.add("convention", "old wording")
        self.assertTrue(store.revise(kid, body="better wording"))
        self.assertEqual(store.get(kid)["body"], "better wording")
        self.assertIsNotNone(store.get(kid)["revised_at"])
        self.assertFalse(store.revise(kid))  # nothing to change

    def test_retire_hides_from_views_but_keeps_row(self):
        store = self._open()
        kid = store.add("pitfall", "trap one")
        self.assertTrue(store.retire(kid))
        self.assertEqual(store.list_items(), [])
        self.assertEqual(store.list_items(include_retired=True)[0]["id"],
                         kid)
        self.assertEqual(store.get(kid)["status"], "retired")

    def test_version_scope_and_origin(self):
        store = self._open()
        store.add("hard_rule", "rule on branch", version_scope="feature/x")
        store.add("hard_rule", "rule everywhere")
        self.assertEqual(len(store.list_items(version_scope="feature/x")),
                         1)
        items = {i["body"]: i for i in store.list_items()}
        self.assertEqual(items["rule everywhere"]["origin"], "curated")

    def test_fts_indexes_content(self):
        store = self._open()
        store.add("pitfall", "the doorbell register wraps silently",
                  tags=["nvme"])
        rows = store._conn.execute(
            "SELECT title, body_tokenized FROM knowledge_fts "
            "WHERE knowledge_fts MATCH 'doorbell'").fetchall()
        self.assertEqual(len(rows), 1)


class TestCjkTokenization(_Base):
    """CJK pre-tokenization: knowledge_fts indexes segmented tokens
    instead of unicode61-folded CJK runs, so a pure-CJK query matches
    paragraphs whose body contains the segmented words."""

    def test_body_tokenized_column_populated_on_add(self):
        store = self._open()
        kid = store.add("hard_rule", "释放内存前必须持有互斥锁")
        row = store._conn.execute(
            "SELECT body_tokenized FROM knowledge_items "
            "WHERE id = ?", (kid,)).fetchone()
        self.assertIsNotNone(row["body_tokenized"])
        # The pre-tokenized form contains segmented words (jieba) or
        # character bigrams (fallback), not the raw folded run.
        self.assertIn("释放", row["body_tokenized"])

    def test_revise_updates_body_tokenized(self):
        store = self._open()
        kid = store.add("convention", "old english text")
        original = store._conn.execute(
            "SELECT body_tokenized FROM knowledge_items "
            "WHERE id = ?", (kid,)).fetchone()["body_tokenized"]
        store.revise(kid, body="新的中文内容")
        updated = store._conn.execute(
            "SELECT body_tokenized FROM knowledge_items "
            "WHERE id = ?", (kid,)).fetchone()["body_tokenized"]
        self.assertNotEqual(original, updated)
        self.assertIn("新", updated)

    def test_cjk_query_matches_via_fts5(self):
        store = self._open()
        store.add("hard_rule", "释放内存前必须持有互斥锁")
        store.add("pitfall", "不要在信号处理函数中调用非异步信号安全的函数")
        # A pure-CJK query for the verb must match the hard_rule row
        # whose body_tokenized contains the segmented token.
        rows = store._conn.execute(
            "SELECT knowledge_fts.rowid FROM knowledge_fts "
            "WHERE knowledge_fts MATCH '释放'").fetchall()
        self.assertEqual(len(rows), 1)

    def test_search_latin_query_returns_ranked_results(self):
        store = self._open()
        store.add("hard_rule", "lock the queue before touching the tail")
        store.add("pitfall", "the doorbell register wraps silently")
        hits = store.search("doorbell")
        self.assertTrue(hits)
        self.assertIn("doorbell", hits[0]["body"])

    def test_search_cjk_query_returns_ranked_results(self):
        store = self._open()
        store.add("hard_rule", "释放内存前必须持有互斥锁")
        store.add("pitfall", "不要在信号处理函数中调用非异步信号安全的函数")
        hits = store.search("释放内存")
        self.assertTrue(hits)
        self.assertIn("释放", hits[0]["body"])

    def test_search_kinds_filter(self):
        store = self._open()
        store.add("hard_rule", "lock the queue before touching the tail")
        store.add("pitfall", "the queue tail can race without a lock")
        hits = store.search("queue", kinds=["hard_rule"])
        self.assertTrue(hits)
        self.assertTrue(all(h["kind"] == "hard_rule" for h in hits))

    def test_search_empty_query_returns_empty(self):
        store = self._open()
        store.add("hard_rule", "anything")
        self.assertEqual(store.search(""), [])
        self.assertEqual(store.search("   "), [])

    def test_rebuild_tokenized_refreshes_index(self):
        store = self._open()
        store.add("abstraction", "first version of the abstraction")
        # Simulate a stale body_tokenized (e.g. pre-migration copy).
        store._conn.execute(
            "UPDATE knowledge_items SET body_tokenized = body")
        store._conn.commit()
        n = store.rebuild_tokenized()
        self.assertGreater(n, 0)
        row = store._conn.execute(
            "SELECT body_tokenized FROM knowledge_items").fetchone()
        # rebuild_tokenized calls _cjk_pre_tokenize, which for
        # non-CJK text returns the whitespace-joined tokens.
        self.assertNotEqual(row["body_tokenized"], "first version of the abstraction")

    def test_migration_backfills_existing_rows(self):
        """A store created with the old schema (no body_tokenized)
        must migrate on the next write-side open."""
        store = self._open()
        store.add("hard_rule", "释放内存前必须持有互斥锁")
        store.close()
        # Simulate the pre-migration state: drop body_tokenized
        # column and revert FTS5 to index body directly.
        conn = sqlite3.connect(knowledge_db_path(self.graph_dir))
        conn.execute("DROP TRIGGER IF EXISTS knowledge_ai")
        conn.execute("DROP TRIGGER IF EXISTS knowledge_ad")
        conn.execute("DROP TRIGGER IF EXISTS knowledge_au")
        conn.execute("DROP TABLE IF EXISTS knowledge_fts")
        # SQLite does not support DROP COLUMN before 3.35; recreate
        # the table without the column to model the old schema.
        conn.execute("ALTER TABLE knowledge_items RENAME TO knowledge_items_old")
        conn.execute(
            "CREATE TABLE knowledge_items (id INTEGER PRIMARY KEY, "
            "kind TEXT, title TEXT, body TEXT, tags TEXT, "
            "extra_json TEXT, version_scope TEXT, status TEXT, "
            "origin TEXT, source_memory_id INTEGER, weight REAL, "
            "created_at TEXT, revised_at TEXT)")
        conn.execute(
            "INSERT INTO knowledge_items SELECT id, kind, title, body, "
            "tags, extra_json, version_scope, status, origin, "
            "source_memory_id, weight, created_at, revised_at "
            "FROM knowledge_items_old")
        conn.execute("DROP TABLE knowledge_items_old")
        conn.execute(
            "CREATE VIRTUAL TABLE knowledge_fts USING fts5("
            "title, body, tags, content='knowledge_items', "
            "content_rowid='id', tokenize='porter unicode61')")
        conn.commit()
        conn.close()
        # Re-open: migration must add body_tokenized, backfill, and
        # rebuild FTS5 on the new column.
        store2 = KnowledgeStore(self.graph_dir)
        self.addCleanup(store2.close)
        row = store2._conn.execute(
            "SELECT body_tokenized FROM knowledge_items "
            "WHERE kind = 'hard_rule'").fetchone()
        self.assertIsNotNone(row["body_tokenized"])
        self.assertIn("释放", row["body_tokenized"])
        # FTS5 now indexes body_tokenized, so a CJK query matches.
        rows = store2._conn.execute(
            "SELECT rowid FROM knowledge_fts "
            "WHERE knowledge_fts MATCH '释放'").fetchall()
        self.assertEqual(len(rows), 1)


class TestBriefInterop(_Base):
    def _sample_brief(self):
        return {
            "project": "demo", "one_liner": "a demo project",
            "description": "It demos things.",
            "hard_rules": [{"rule": "lock first", "type": "api",
                            "detail": "", "evidence": ""}],
            "modes": [{"name": "pcie", "when": "hardware",
                       "differences": "faster"}],
            "key_abstractions": [{"name": "queue", "role": "io path"}],
            "conventions": ["snake_case"],
            "pitfalls": ["doorbell wraps"],
            "query_paths": ["describe-node queue"],
            "must_know": "read the docs",
        }

    def test_replace_and_to_brief_round_trip(self):
        store = self._open()
        n = store.replace_from_brief(self._sample_brief())
        self.assertEqual(n, 8)  # 1 desc + 1 must_know + 6 items
        out = store.to_brief()
        self.assertEqual(out["project"], "demo")
        self.assertEqual(out["one_liner"], "a demo project")
        self.assertEqual(out["description"], "It demos things.")
        self.assertEqual(out["hard_rules"][0]["rule"], "lock first")
        self.assertEqual(out["modes"][0]["name"], "pcie")
        self.assertEqual(out["key_abstractions"][0]["role"], "io path")
        self.assertEqual(out["conventions"], ["snake_case"])
        self.assertEqual(out["pitfalls"], ["doorbell wraps"])
        self.assertEqual(out["query_paths"], ["describe-node queue"])
        self.assertEqual(out["must_know"], "read the docs")

    def test_save_brief_routes_through_store_and_rewrites_view(self):
        save_brief(self.graph_dir, self._sample_brief())
        self.assertTrue(os.path.isfile(knowledge_db_path(self.graph_dir)))
        view = json.load(open(os.path.join(
            self.graph_dir, "knowledge", "brief.json"),
            encoding="utf-8"))
        self.assertEqual(view["project"], "demo")
        self.assertEqual(view["hard_rules"][0]["rule"], "lock first")

    def test_load_brief_prefers_store(self):
        save_brief(self.graph_dir, self._sample_brief())
        # Corrupt the derived view: the store must win.
        with open(os.path.join(self.graph_dir, "knowledge",
                               "brief.json"), "w") as f:
            f.write("{broken")
        brief = load_brief(self.graph_dir)
        self.assertEqual(brief["project"], "demo")
        self.assertEqual(brief["pitfalls"], ["doorbell wraps"])

    def test_load_brief_falls_back_to_file_without_store(self):
        know = os.path.join(self.graph_dir, "knowledge")
        os.makedirs(know)
        with open(os.path.join(know, "brief.json"), "w") as f:
            json.dump(self._sample_brief(), f)
        brief = load_brief(self.graph_dir)
        self.assertEqual(brief["project"], "demo")

    def test_legacy_brief_imported_once_on_store_creation(self):
        know = os.path.join(self.graph_dir, "knowledge")
        os.makedirs(know)
        with open(os.path.join(know, "brief.json"), "w") as f:
            json.dump(self._sample_brief(), f)
        store = self._open()  # creation imports the file
        self.assertEqual(len(store.list_items()), 8)
        # A second open must not duplicate (marker).
        store.close()
        store2 = KnowledgeStore(self.graph_dir)
        self.addCleanup(store2.close)
        self.assertEqual(len(store2.list_items()), 8)

    def test_read_path_never_creates_store(self):
        self.assertIsNone(load_brief(self.graph_dir))
        self.assertFalse(os.path.exists(knowledge_db_path(self.graph_dir)))


class TestKbIndexFromStore(_Base):
    def test_rebuild_reads_store_rows_not_derived_file(self):
        from _builder.kb.kb_index import rebuild_kb_index, query_kb
        save_brief(self.graph_dir, {
            "project": "demo",
            "pitfalls": ["doorbell wraps silently"],
            "must_know": "read the docs",
        })
        summary = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertEqual(summary.get("knowledge_count"), 2)
        hits = query_kb(self.graph_dir, "doorbell wraps")
        self.assertTrue(any("doorbell" in (h.get("title") or "") + h["body"]
                            for h in hits))

    def test_rebuild_carries_version_scope_through_brief(self):
        # A scoped knowledge item must reach kb_paragraphs with its
        # version_scope intact (both the full rebuild and the brief
        # sync path), or --version-scope ranking silently degrades to
        # memory-only.
        from _builder.kb.kb_index import (
            _kb_connect, rebuild_kb_index, query_kb, sync_brief_to_kb)
        store = self._open()
        store.add("hard_rule", "gasket rule for release two",
                  version_scope="release/2.0")
        store.add("hard_rule", "gasket rule that always applies")
        store.close()
        rebuild_kb_index(self.graph_dir, verbose=False)
        conn = _kb_connect(self.graph_dir)
        try:
            rows = conn.execute(
                "SELECT body, version_scope FROM kb_paragraphs "
                "WHERE source_kind = 'knowledge' AND kind = 'hard_rule'"
            ).fetchall()
        finally:
            conn.close()
        scopes = {r["body"]: r["version_scope"] for r in rows}
        self.assertEqual(scopes["gasket rule for release two"],
                         "release/2.0")
        self.assertEqual(scopes["gasket rule that always applies"],
                         "default")
        # The incremental sync path must carry it too.
        n = sync_brief_to_kb(self.graph_dir, load_brief(self.graph_dir))
        self.assertGreater(n, 0)
        conn = _kb_connect(self.graph_dir)
        try:
            again = conn.execute(
                "SELECT COUNT(*) FROM kb_paragraphs "
                "WHERE version_scope = 'release/2.0'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertGreater(again, 0)
        # Ranked query puts the current scope first and labels the rest.
        hits = query_kb(self.graph_dir, "gasket rule",
                        version_scope="release/2.0")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["version_scope"], "release/2.0")
        self.assertTrue(hits[0]["is_current_scope"])

    def test_brief_round_trip_keeps_scope(self):
        # replace_from_brief (brief-* commands) must not flatten a
        # scoped item back to 'default'.
        store = self._open()
        brief = {
            "project": "demo",
            "hard_rules": [{"rule": "scoped rule",
                            "version_scope": "release/9.9"}],
            "key_abstractions": [{"name": "gasket",
                                  "role": "seals things",
                                  "version_scope": "main"}],
        }
        self.assertEqual(store.replace_from_brief(brief), 2)
        out = store.to_brief()
        store.close()
        self.assertEqual(out["hard_rules"][0]["version_scope"],
                         "release/9.9")
        self.assertEqual(out["key_abstractions"][0]["version_scope"],
                         "main")

    def test_rebuild_skips_derived_brief_file_when_store_present(self):
        # With the store present, scanning brief.json would double
        # every knowledge paragraph.
        from _builder.kb.kb_index import rebuild_kb_index
        save_brief(self.graph_dir, {
            "project": "demo", "pitfalls": ["only once"],
        })
        summary = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertEqual(summary.get("knowledge_count"), 1)

    def test_foreign_briefs_still_indexed(self):
        from _builder.kb.kb_index import rebuild_kb_index, query_kb
        save_brief(self.graph_dir, {"project": "demo",
                                    "must_know": "local"})
        know = os.path.join(self.graph_dir, "knowledge")
        with open(os.path.join(know, "foreign_other_brief.json"),
                  "w") as f:
            json.dump({"must_know": "foreign wisdom about locks"}, f)
        summary = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertEqual(summary.get("knowledge_count"), 2)
        hits = query_kb(self.graph_dir, "foreign wisdom")
        self.assertTrue(any("[other]" in (h.get("title") or "")
                            or "foreign wisdom" in h["body"]
                            for h in hits))


if __name__ == "__main__":
    unittest.main()
