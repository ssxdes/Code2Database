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
            "SELECT title, body FROM knowledge_fts "
            "WHERE knowledge_fts MATCH 'doorbell'").fetchall()
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
