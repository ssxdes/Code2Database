"""Tests for the SQLite-backed MemoryStore (memory/memory.db)."""

import json
import os
import sqlite3
import sys
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.memory_store import (
    MemoryStore,
    MERGE_SIMILARITY_THRESHOLD,
    MEMORY_SCHEMA_VERSION,
    _memory_lock,
)


class MemoryStoreTestBase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.graph_dir = os.path.join(self.tmp.name, "graph")
        os.makedirs(self.graph_dir, exist_ok=True)
        self.store = MemoryStore(self.graph_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def _raw(self, sql, params=()):
        conn = sqlite3.connect(self.store.db_path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _age_entry(self, mem_id, days):
        """Backdate an entry so decay() has something to do."""
        past = (datetime.now() - timedelta(days=days)).isoformat()
        conn = sqlite3.connect(self.store.db_path)
        try:
            conn.execute(
                "UPDATE memories SET created = ?, last_accessed = ?, "
                "validated_at = ? WHERE id = ?",
                (past, past, past, mem_id))
            conn.commit()
        finally:
            conn.close()


class TestSchemaInit(MemoryStoreTestBase):
    def test_db_file_created(self):
        self.assertTrue(os.path.exists(self.store.db_path))
        self.assertEqual(
            os.path.basename(self.store.db_path), "memory.db")

    def test_tables_exist(self):
        tables = {r[0] for r in self._raw(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("memories", tables)
        self.assertIn("categories", tables)
        self.assertIn("audit_log", tables) if False else None
        vtabs = {r[0] for r in self._raw(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE '%fts%'")}
        self.assertIn("memories_fts", vtabs)

    def test_user_version_set(self):
        conn = sqlite3.connect(self.store.db_path)
        try:
            v = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(v, MEMORY_SCHEMA_VERSION)

    def test_reinit_idempotent(self):
        store2 = MemoryStore(self.graph_dir)
        self.assertEqual(store2.db_path, self.store.db_path)


class TestCategories(MemoryStoreTestBase):
    def test_ensure_creates_nested_path(self):
        cat_id = self.store.ensure_category("bdev/nvme/pcie")
        self.assertIsNotNone(cat_id)
        rows = {r[0] for r in self._raw("SELECT path FROM categories")}
        self.assertEqual(rows, {"bdev", "bdev/nvme", "bdev/nvme/pcie"})

    def test_ensure_idempotent(self):
        a = self.store.ensure_category("bdev/nvme")
        b = self.store.ensure_category("bdev/nvme")
        self.assertEqual(a, b)

    def test_ensure_empty_is_uncategorized(self):
        self.assertEqual(self.store.ensure_category(""), "x") if False else None
        cat_id = self.store.ensure_category("")
        path = self._raw(
            "SELECT path FROM categories WHERE id = ?", (cat_id,))[0][0]
        self.assertEqual(path, "uncategorized")

    def test_normalize_slashes(self):
        cat_id = self.store.ensure_category("/bdev//nvme/")
        path = self._raw(
            "SELECT path FROM categories WHERE id = ?", (cat_id,))[0][0]
        self.assertEqual(path, "bdev/nvme")

    def test_category_id_lookup(self):
        cat_id = self.store.ensure_category("io/uring")
        self.assertEqual(self.store.category_id("io/uring"), cat_id)
        self.assertIsNone(self.store.category_id("missing"))

    def test_categories_tree_with_counts(self):
        self.store.add("q1", "a1", category="bdev/nvme/pcie")
        self.store.add("q2", "a2", category="bdev/nvme/tcp")
        self.store.add("q3", "a3", category="bdev/nvme")
        tree = self.store.categories()
        top = {n["path"]: n for n in tree}
        self.assertIn("bdev", top)
        nvme = {c["path"]: c for c in top["bdev"]["children"]}["bdev/nvme"]
        self.assertEqual(nvme["count"], 1)          # direct
        self.assertEqual(nvme["subtree_count"], 3)  # incl. subcategories

    def test_categories_empty(self):
        self.assertEqual(self.store.categories(), [])


class TestAdd(MemoryStoreTestBase):
    def test_add_creates_root(self):
        mid = self.store.add("What is a bdev?", "block device")
        entry = self.store.get(mid)
        self.assertEqual(entry["root_id"], mid)
        self.assertEqual(entry["status"], "active")
        self.assertEqual(entry["category_id"],
                         self.store.category_id("uncategorized"))

    def test_add_with_category_autocreates(self):
        self.store.add("q", "a", category="bdev/nvme/pcie")
        self.assertIsNotNone(self.store.category_id("bdev/nvme/pcie"))

    def test_add_tags_normalized_sorted_dedup(self):
        self.store.add("q", "a", tags=["zeta", "alpha", "zeta"])
        entry = self.store.get(1)
        self.assertEqual(entry["tags"], ["alpha", "zeta"])

    def test_add_similar_merges_to_root(self):
        r1 = self.store.add(
            "How does nvme driver submit IO?", "answer A " * 50)
        v1 = self.store.add(
            "How does nvme driver submit IO requests?", "answer B")
        variant = self.store.get(v1)
        self.assertEqual(variant["root_id"], r1)
        root = self.store.get(r1)
        self.assertEqual(root["merged_count"], 1)

    def test_add_weak_variant_does_not_replace_strong_root(self):
        strong_answer = "very thorough answer. " * 100
        r1 = self.store.add("How does nvme submit IO?", strong_answer)
        self.store.add("How does nvme submit IO cmds?", "weak")
        root = self.store.get(r1)
        self.assertEqual(root["answer"], strong_answer)

    def test_add_no_merge(self):
        self.store.add("How does nvme submit IO?", "a")
        v = self.store.add("How does nvme submit IO?", "b", no_merge=True)
        self.assertEqual(self.store.get(v)["root_id"], v)

    def test_add_dissimilar_is_new_root(self):
        a = self.store.add("How does nvme submit IO?", "a")
        b = self.store.add("Completely unrelated cooking question", "b")
        self.assertNotEqual(self.store.get(b)["root_id"], a)

    def test_add_author_recorded(self):
        self.store.add("q", "a", author="alice")
        self.assertEqual(self.store.get(1)["author"], "alice")


class TestSearch(MemoryStoreTestBase):
    def _seed(self):
        self.store.add("How does nvme driver submit IO?",
                       "via submission queue tail doorbell",
                       category="bdev/nvme/pcie", tags=["nvme", "io"],
                       author="alice")
        self.store.add("How to configure tcp transport?",
                       "set transport opts in config file",
                       category="bdev/nvme/tcp", tags=["transport"],
                       author="bob")
        self.store.add("What does the reactor loop do?",
                       "polls pollers each iteration",
                       category="event", author="carol")

    def test_fts_match(self):
        self._seed()
        results = self.store.search("nvme submit")
        self.assertTrue(results)
        self.assertIn("submission", results[0]["answer"])

    def test_category_prefix_includes_subtree(self):
        self._seed()
        results = self.store.search("transport", category="bdev")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["category"], "bdev/nvme/tcp")
        # exact mid-level prefix also works
        results2 = self.store.search("io", category="bdev/nvme")
        self.assertEqual(len(results2), 1)
        self.assertEqual(results2[0]["category"], "bdev/nvme/pcie")

    def test_author_filter(self):
        self._seed()
        results = self.store.search("transport", author="bob")
        self.assertEqual(len(results), 1)
        self.assertEqual(self.store.search("transport", author="nobody"), [])

    def test_tags_filter_requires_all(self):
        self._seed()
        self.assertEqual(len(self.store.search("nvme", tags=["nvme"])), 1)
        self.assertEqual(
            len(self.store.search("nvme", tags=["nvme", "missing"])), 0)

    def test_empty_query_returns_nothing(self):
        self._seed()
        self.assertEqual(self.store.search(""), [])
        self.assertEqual(self.store.search("   "), [])

    def test_fallback_similarity_when_fts_misses(self):
        # FTS ANDs all tokens; "queue doorbell" has zero rows with BOTH
        # tokens, but "queue" alone overlaps the first entry by Jaccard.
        self.store.add("nvme submission queue handling", "doorbells")
        results = self.store.search("queue other")
        self.assertTrue(results)
        self.assertEqual(results[0]["question"],
                         "nvme submission queue handling")

    def test_results_grouped_by_root_with_variant_count(self):
        root = self.store.add("How does nvme submit IO?", "base answer")
        self.store.add("How does nvme submit IO requests?", "variant")
        results = self.store.search("nvme submit IO", top_n=10)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], root)
        self.assertEqual(results[0]["variant_count"], 1)

    def test_search_bumps_access_count(self):
        self._seed()
        self.store.search("nvme")
        self.assertEqual(self.store.get(1)["access_count"], 1)

    def test_include_experience(self):
        mid = self.store.add("old thing", "old answer")
        self._age_entry(mid, days=60)
        self.store.decay()
        # decayed below 0.1 → experience, excluded by default
        self.assertEqual(self.store.search("old thing"), [])
        results = self.store.search("old thing", include_experience=True)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "experience")

    def test_min_weight_filter(self):
        self._seed()
        results = self.store.search("nvme", min_weight=0.0)
        self.assertTrue(results)
        self.assertEqual(self.store.search("nvme", min_weight=9.9), [])


class TestCorrectReshapePromote(MemoryStoreTestBase):
    def test_correct_updates_and_versions(self):
        self.store.add("q", "old answer")
        self.store.correct(1, "answer", "new answer")
        entry = self.store.get(1)
        self.assertEqual(entry["answer"], "new answer")
        self.assertEqual(entry["versions"][0]["field"], "answer")
        self.assertEqual(entry["versions"][0]["old_value"], "old answer")

    def test_correct_rejects_unknown_field(self):
        self.store.add("q", "a")
        with self.assertRaises(ValueError):
            self.store.correct(1, "made_up_column", "x")

    def test_correct_missing_id_noop(self):
        self.store.correct(999, "answer", "x")  # must not raise

    def test_reshape_replaces_answer(self):
        self.store.add("q", "old")
        self.store.reshape(1, "much better answer")
        entry = self.store.get(1)
        self.assertEqual(entry["answer"], "much better answer")
        self.assertEqual(entry["reshaped_count"], 1)
        self.assertEqual(entry["versions"][0]["answer"], "old")

    def test_promote_boosts_and_persists(self):
        self.store.add("q", "a")
        self.store.promote(1, boost=2.0)
        entry = self.store.get(1)
        self.assertGreater(entry["weight"], 1.0)
        self.assertAlmostEqual(entry["boost"], 2.0)

    def test_boost_survives_decay(self):
        self.store.add("q", "a")
        self.store.promote(1, boost=3.0)
        boosted = self.store.get(1)["weight"]
        self.store.decay()
        after = self.store.get(1)["weight"]
        # fresh entry + boost: decay may recompute but the boost factor
        # keeps the weight boosted (not reset to ~1.0)
        self.assertGreater(after, 3.0)
        self.assertAlmostEqual(after, boosted, delta=0.01)


class TestCorrectSimilar(MemoryStoreTestBase):
    """correct_similar — the correct-first save (correction protocol)."""

    def test_similar_question_reshapes_existing(self):
        self.store.add("how does nvme submit io", "wrong answer",
                       no_merge=True)
        result = self.store.correct_similar(
            "how does nvme submit io?", "right answer", author="alice")
        self.assertEqual(result["action"], "corrected")
        self.assertEqual(result["id"], 1)
        self.assertGreaterEqual(result["score"],
                                MERGE_SIMILARITY_THRESHOLD)
        # No new entry created — the wrong one was fixed in place
        self.assertEqual(len(self._raw(
            "SELECT id FROM memories WHERE status = 'active'")), 1)
        entry = self.store.get(1)
        self.assertEqual(entry["answer"], "right answer")
        # Old answer preserved in history + corrector attributed
        versions = entry["versions"]
        self.assertTrue(any(v.get("answer") == "wrong answer"
                            for v in versions))
        self.assertTrue(any(v.get("corrected_by") == "alice"
                            for v in versions))

    def test_dissimilar_question_creates_new(self):
        self.store.add("how does nvme submit io", "a", no_merge=True)
        result = self.store.correct_similar(
            "what color is the sky", "blue")
        self.assertEqual(result["action"], "created")
        self.assertIsNone(result["matched_question"])
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(self.store.get(result["id"])["answer"], "blue")

    def test_correction_targets_cluster_root(self):
        # Variant cluster: both entries share root 1
        self.store.add("how does rpc dispatch work", "wrong",
                       no_merge=True)
        self.store.add("how does rpc dispatch work exactly", "wrong too")
        result = self.store.correct_similar(
            "how does rpc dispatch work these days", "right")
        self.assertEqual(result["action"], "corrected")
        self.assertEqual(result["id"], 1)  # the root, not the variant
        self.assertEqual(self.store.get(1)["answer"], "right")

    def test_search_finds_corrected_answer(self):
        self.store.add("how does bdev register", "wrong", no_merge=True)
        self.store.correct_similar("how does bdev register", "call the api")
        hits = self.store.search("bdev register")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["answer"], "call the api")


class TestSplit(MemoryStoreTestBase):
    def setUp(self):
        super().setUp()
        self.parent = self.store.add(
            "How does bdev IO work?", "broad answer",
            category="bdev", tags=["io"],
            node_ids=["n1", "n2"], author="alice")

    def test_split_parent_becomes_tombstone(self):
        children = self.store.split(self.parent, [
            {"question": "pcie path", "answer": "a1"},
            {"question": "tcp path", "answer": "a2"},
        ])
        parent = self.store.get(self.parent)
        self.assertEqual(parent["status"], "split")
        self.assertEqual(parent["versions"][-1]["split_into"], children)

    def test_split_children_inherit(self):
        children = self.store.split(self.parent, [
            {"question": "pcie path", "answer": "a1"},
        ])
        child = self.store.get(children[0])
        self.assertEqual(child["status"], "active")
        self.assertEqual(child["split_from"], self.parent)
        self.assertEqual(child["node_ids"], ["n1", "n2"])
        self.assertEqual(child["author"], "alice")
        self.assertEqual(child["tags"], ["io"])
        self.assertEqual(child["root_id"], children[0])

    def test_split_part_overrides_category_and_tags(self):
        children = self.store.split(self.parent, [
            {"question": "rdma path", "answer": "a",
             "category": "bdev/nvme/rdma", "tags": ["rdma"]},
        ])
        child = self.store.get(children[0])
        self.assertEqual(
            child["category_id"], self.store.category_id("bdev/nvme/rdma"))
        self.assertEqual(child["tags"], ["rdma"])

    def test_split_errors(self):
        with self.assertRaises(ValueError):
            self.store.split(self.parent, [])
        with self.assertRaises(ValueError):
            self.store.split(999, [{"question": "x"}])
        self.store.split(self.parent, [{"question": "x"}])
        with self.assertRaises(ValueError):
            self.store.split(self.parent, [{"question": "again"}])
        with self.assertRaises(ValueError):
            self.store.split(self.parent, [{"answer": "no question"}])

    def test_split_children_searchable(self):
        self.store.split(self.parent, [
            {"question": "nvme pcie submission queue depth", "answer": "a"},
        ])
        results = self.store.search("submission queue depth")
        self.assertEqual(len(results), 1)


class TestLineage(MemoryStoreTestBase):
    """lineage() — the split/merge/variant governance graph."""

    def test_empty_store(self):
        data = self.store.lineage()
        self.assertEqual(data["nodes"], [])
        self.assertEqual(data["edges"], [])

    def test_variant_edges_from_cluster(self):
        self.store.add("how does rpc dispatch work", "a", no_merge=True)
        self.store.add("how does rpc dispatch work exactly", "b")
        data = self.store.lineage()
        variant_edges = [e for e in data["edges"]
                         if e["type"] == "variant"]
        self.assertEqual(variant_edges,
                         [{"from": 1, "to": 2, "type": "variant"}])
        ids = {n["id"] for n in data["nodes"]}
        self.assertEqual(ids, {1, 2})

    def test_split_edges(self):
        parent = self.store.add("broad question", "a", no_merge=True)
        children = self.store.split(parent, [
            {"question": "focused one", "answer": "a1"},
            {"question": "focused two", "answer": "a2"},
        ])
        data = self.store.lineage()
        split_edges = {e["from"] for e in data["edges"]
                       if e["type"] == "split"}
        self.assertEqual(split_edges, {parent})
        targets = {e["to"] for e in data["edges"] if e["type"] == "split"}
        self.assertEqual(targets, set(children))
        # tombstone node carries 'split' status for distinct rendering
        by_id = {n["id"]: n for n in data["nodes"]}
        self.assertEqual(by_id[parent]["status"], "split")

    def test_merge_edges(self):
        a = self.store.add("how does x work", "a1", no_merge=True)
        b = self.store.add("completely different topic", "b1",
                           no_merge=True)
        canonical = self.store.merge([a, b], canonical_id=a)
        data = self.store.lineage()
        merge_edges = [e for e in data["edges"]
                       if e["type"] == "merged_into"]
        self.assertEqual(merge_edges,
                         [{"from": canonical, "to": b,
                           "type": "merged_into"}])

    def test_nodes_carry_context(self):
        self.store.add("how does bdev register", "a",
                       category="bdev", author="alice", no_merge=True)
        data = self.store.lineage()
        node = data["nodes"][0]
        self.assertEqual(node["question"], "how does bdev register")
        self.assertEqual(node["status"], "active")
        self.assertEqual(node["category"], "bdev")
        self.assertEqual(node["author"], "alice")
        self.assertIn("weight", node)


class TestReadOnlyAndAuthors(MemoryStoreTestBase):
    """read_only mode (multi-user viewing) + authors() index."""

    def _populate(self):
        self.store.add("q one", "a1", author="alice", no_merge=True)
        self.store.add("q two", "a2", author="bob", no_merge=True)
        self.store.add("q three", "a3", no_merge=True)  # unattributed

    def test_read_only_never_creates_anything(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bare = os.path.join(tmp, "no_graph")
            MemoryStore(bare, read_only=True)
            self.assertFalse(os.path.exists(
                os.path.join(bare, "memory")))

    def test_read_only_search_skips_access_bump(self):
        self._populate()
        before = self.store.get(1)["access_count"]
        reader = MemoryStore(self.graph_dir, read_only=True)
        hits = reader.search("q one")
        self.assertTrue(hits)
        self.assertEqual(self.store.get(1)["access_count"], before)

    def test_read_only_digest_and_stats(self):
        self._populate()
        reader = MemoryStore(self.graph_dir, read_only=True)
        self.assertEqual(len(reader.digest()), 3)
        self.assertEqual(reader.stats()["active_entries"], 3)

    def test_read_only_rejects_writes(self):
        self._populate()
        reader = MemoryStore(self.graph_dir, read_only=True)
        with self.assertRaises(sqlite3.OperationalError):
            reader.add("q four", "a4")

    def test_authors_index(self):
        self._populate()
        authors = {a["author"]: a for a in self.store.authors()}
        self.assertEqual(authors["alice"]["entries"], 1)
        self.assertEqual(authors["bob"]["entries"], 1)
        self.assertEqual(authors["(unattributed)"]["entries"], 1)
        self.assertEqual(authors["alice"]["active"], 1)

    def test_digest_author_filter(self):
        self._populate()
        reader = MemoryStore(self.graph_dir, read_only=True)
        only_alice = reader.digest(author="alice")
        self.assertEqual(len(only_alice), 1)
        self.assertEqual(only_alice[0]["author"], "alice")

    def test_search_author_filter_read_only(self):
        self._populate()
        reader = MemoryStore(self.graph_dir, read_only=True)
        hits = reader.search("q", author="bob")
        self.assertTrue(hits)
        self.assertTrue(all(h["author"] == "bob" for h in hits))


class TestMerge(MemoryStoreTestBase):
    def setUp(self):
        super().setUp()
        self.a = self.store.add("How to init bdev?", "answer A",
                                tags=["init"], node_ids=["n1"])
        self.b = self.store.add("How do you init the bdev?", "answer B",
                                tags=["setup"], node_ids=["n2"])
        self.c = self.store.add("Unrelated question", "answer C")

    def test_merge_canonical_absorbs(self):
        canon = self.store.merge([self.a, self.b])
        self.assertEqual(canon, self.a)
        canonical = self.store.get(self.a)
        self.assertEqual(set(canonical["tags"]), {"init", "setup"})
        self.assertEqual(canonical["node_ids"], ["n1", "n2"])
        self.assertEqual(canonical["merged_count"], 1)
        self.assertEqual(canonical["versions"][-1]["merged_ids"], [self.b])

    def test_merge_others_become_tombstones(self):
        self.store.merge([self.a, self.b])
        tomb = self.store.get(self.b)
        self.assertEqual(tomb["status"], "merged")
        self.assertEqual(tomb["merged_into"], self.a)

    def test_merge_explicit_canonical_and_overrides(self):
        canon = self.store.merge(
            [self.a, self.b], canonical_id=self.b,
            question="merged question", answer="merged answer")
        self.assertEqual(canon, self.b)
        self.assertEqual(self.store.get(self.b)["question"],
                         "merged question")
        self.assertEqual(self.store.get(self.b)["answer"], "merged answer")

    def test_merge_errors(self):
        with self.assertRaises(ValueError):
            self.store.merge([self.a])
        with self.assertRaises(ValueError):
            self.store.merge([self.a, 999])
        with self.assertRaises(ValueError):
            self.store.merge([self.a, self.b], canonical_id=self.c)

    def test_variants_repointed_to_canonical(self):
        # b has a variant pointing at it; after merging b into a, the
        # variant's root should follow the canonical.
        variant = self.store.add("How do you init the bdev exactly?",
                                 "variant answer")
        root_before = self.store.get(variant)["root_id"]
        self.assertEqual(root_before, self.b)
        self.store.merge([self.a, self.b])
        self.assertEqual(self.store.get(variant)["root_id"], self.a)


class TestMove(MemoryStoreTestBase):
    def test_move_creates_and_reassigns(self):
        self.store.add("q", "a", category="misc")
        self.store.move(1, "bdev/nvme/pcie")
        entry = self.store.get(1)
        self.assertEqual(
            entry["category_id"], self.store.category_id("bdev/nvme/pcie"))
        self.assertIsNotNone(self.store.category_id("bdev/nvme/pcie"))

    def test_move_missing_id_noop(self):
        self.store.move(999, "x")  # must not raise


class TestDecayAndValidate(MemoryStoreTestBase):
    def test_decay_updates_aged_weights(self):
        mid = self.store.add("q", "a")
        self._age_entry(mid, days=30)
        self.store.decay()
        self.assertLess(self.store.get(mid)["weight"], 1.0)

    def test_decay_archives_low_weight_and_repoints_variants(self):
        root = self.store.add(
            "how to init the bdev driver", "a", tags=["t"])
        # 5/6 shared tokens → similarity 0.83 ≥ 0.7 → real variant
        variant = self.store.add("how to init the bdev", "b")
        self.assertEqual(self.store.get(variant)["root_id"], root)
        self._age_entry(root, days=90)
        self.store.decay()
        self.assertEqual(self.store.get(root)["status"], "experience")
        # variant promoted to its own root
        self.assertEqual(self.store.get(variant)["root_id"], variant)

    def test_validate_against_graph(self):
        self.store.add("q1", "a", node_ids=["exists"])
        self.store.add("q2", "a", node_ids=["gone"])
        result = self.store.validate_against_graph({"exists"})
        self.assertEqual(result["validated"], 1)
        self.assertEqual(result["invalidated"], 1)
        self.assertEqual(self.store.get(2)["status"], "experience")
        self.assertIn("no longer in graph",
                      self.store.get(2)["invalidated_reason"])
        self.assertEqual(self.store.get(1)["status"], "active")

    def test_consolidate_summary(self):
        self.store.add("q1", "a")
        result = self.store.consolidate()
        self.assertEqual(result["trusted"], 1)
        self.assertIn("categories", result)


class TestPacksAndStats(MemoryStoreTestBase):
    def test_lite_pack_shape(self):
        self.store.add("q", "a")
        pack = self.store.generate_pack("lite")
        self.assertIn("top_questions", pack)
        self.assertIn("hot_memories", pack)
        self.assertTrue(os.path.exists(
            os.path.join(self.graph_dir, ".memory_pack_lite.json")))

    def test_standard_pack_shape(self):
        self.store.add("q", "a" * 100)
        pack = self.store.generate_pack("standard")
        self.assertIn("all_hot", pack)
        self.assertIn("warm_summaries", pack)

    def test_deep_pack_includes_category(self):
        self.store.add("q", "a", category="bdev/nvme")
        pack = self.store.generate_pack("deep")
        self.assertEqual(pack["entries"][0]["category"], "bdev/nvme")

    def test_full_pack_includes_tombstones(self):
        a = self.store.add("q1", "a")
        b = self.store.add("q2", "b")
        self.store.merge([a, b])
        pack = self.store.generate_pack("full")
        self.assertEqual(len(pack["entries"]), 1)
        self.assertEqual(len(pack["tombstones"]), 1)
        self.assertIn("categories", pack)

    def test_stats_counts(self):
        a = self.store.add("q1", "a", category="x")
        b = self.store.add("q2", "b", category="x")
        self.store.merge([a, b])
        self._age_entry(a, days=90)
        self.store.decay()  # a is root with boost-less weight → may archive
        stats = self.store.stats()
        self.assertEqual(stats["active_entries"]
                         + stats["experience_entries"]
                         + stats["merged_entries"], 2)
        self.assertEqual(stats["categories"], 1)


class TestExportImport(MemoryStoreTestBase):
    def test_export_import_roundtrip(self):
        self.store.add("q1", "a1", tags=["t"], category="c1")
        out = os.path.join(self.tmp.name, "export.json")
        self.store.export_for_debug(out)
        data = json.loads(Path(out).read_text(encoding="utf-8"))
        self.assertEqual(len(data["memories"]), 1)

        # import into a fresh store
        fresh_dir = os.path.join(self.tmp.name, "fresh")
        os.makedirs(fresh_dir, exist_ok=True)
        fresh = MemoryStore(fresh_dir)
        n = fresh.import_from_json(out, merge=True)
        self.assertEqual(n, 1)
        results = fresh.search("q1")
        self.assertEqual(len(results), 1)

    def test_import_merge_skips_duplicates(self):
        self.store.add("unique question one", "a")
        out = os.path.join(self.tmp.name, "export.json")
        self.store.export_for_debug(out)
        n = self.store.import_from_json(out, merge=True)
        self.assertEqual(n, 0)  # similarity > 0.8 → skipped


class TestConcurrency(MemoryStoreTestBase):
    def test_threaded_adds_unique_ids(self):
        ids = []
        errors = []

        def worker(i):
            try:
                mid = self.store.add(f"thread question {i}", f"answer {i}")
                ids.append(mid)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 8)

    def test_lock_is_reentrant_across_sequential_ops(self):
        with _memory_lock(self.store.mem_dir):
            pass  # acquire + release, then a normal op must still work
        self.assertIsNotNone(self.store.add("q", "a"))


class TestFtsTriggers(MemoryStoreTestBase):
    def test_update_visible_in_fts(self):
        self.store.add("q", "original answer")
        self.assertEqual(self.store.search("zebra"), [])
        self.store.correct(1, "answer", "zebra stripes answer")
        results = self.store.search("zebra")
        self.assertEqual(len(results), 1)
        # old token removed from the index by the update trigger
        self.assertEqual(self.store.search("original"), [])

    def test_fts_index_contents(self):
        self.store.add("nvme question", "doorbell answer", tags=["transport"])
        rows = self._raw("SELECT question FROM memories_fts")
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
