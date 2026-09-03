"""Tests for the MemoryManager facade over the SQLite MemoryStore.

Covers: add (root/variant merge/no-merge/tags), correct (versions),
reshape, decay, promote, query, search with filters, split/merge/move
governance, packs, scratch sessions, refine, consolidate,
export/import.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.memory_manager import MemoryManager


class MemoryManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.graph_dir = os.path.join(self.tmp.name, "graph")
        os.makedirs(self.graph_dir, exist_ok=True)
        self.mgr = MemoryManager(self.graph_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def _db(self):
        import sqlite3
        return sqlite3.connect(os.path.join(self.graph_dir,
                                            "memory", "memory.db"))

    def _age(self, mem_id, days):
        past = (datetime.now() - timedelta(days=days)).isoformat()
        conn = self._db()
        conn.execute(
            "UPDATE memories SET created = ?, last_accessed = ?, "
            "validated_at = ? WHERE id = ?",
            (past, past, past, mem_id))
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # add
    # ------------------------------------------------------------------

    def test_add_creates_root(self):
        mid = self.mgr.add("What is a bdev?", "block device abstraction")
        entry = self.mgr.get(mid)
        self.assertEqual(entry["root_id"], mid)
        self.assertEqual(entry["status"], "active")

    def test_add_merge_similar(self):
        r = self.mgr.add("How does nvme driver submit IO?",
                         "answer A " * 20)
        v = self.mgr.add("How does nvme driver submit IO requests?",
                         "answer B")
        self.assertEqual(self.mgr.get(v)["root_id"], r)
        self.assertEqual(self.mgr.get(r)["merged_count"], 1)

    def test_add_no_merge(self):
        self.mgr.add("How does nvme submit IO?", "a")
        v = self.mgr.add("How does nvme submit IO?", "b", no_merge=True)
        self.assertEqual(self.mgr.get(v)["root_id"], v)

    def test_add_with_tags_and_nodes(self):
        mid = self.mgr.add("q", "a", tags=["x", "y"],
                           node_ids=["n1", "n2"], category="bdev/nvme")
        entry = self.mgr.get(mid)
        self.assertEqual(entry["tags"], ["x", "y"])
        self.assertEqual(entry["node_ids"], ["n1", "n2"])
        self.assertEqual(entry["chains"], [])

    def test_add_category_autocreated(self):
        self.mgr.add("q", "a", category="bdev/nvme/pcie")
        paths = [c["path"] for c in
                 _flatten(self.mgr.categories())]
        self.assertIn("bdev/nvme/pcie", paths)

    # ------------------------------------------------------------------
    # correct / reshape / promote
    # ------------------------------------------------------------------

    def test_correct_field(self):
        self.mgr.add("q", "old")
        self.mgr.correct(1, "answer", "new")
        self.assertEqual(self.mgr.get(1)["answer"], "new")

    def test_correct_preserves_version(self):
        self.mgr.add("q", "old")
        self.mgr.correct(1, "answer", "new")
        entry = self.mgr.get(1)
        self.assertEqual(entry["versions"][0]["old_value"], "old")
        self.assertEqual(entry["versions"][0]["field"], "answer")

    def test_reshape_replaces_answer(self):
        self.mgr.add("q", "old answer")
        self.mgr.reshape(1, "better answer")
        self.assertEqual(self.mgr.get(1)["answer"], "better answer")

    def test_reshape_preserves_history(self):
        self.mgr.add("q", "old answer")
        self.mgr.reshape(1, "better answer")
        self.assertEqual(self.mgr.get(1)["versions"][0]["answer"],
                         "old answer")

    def test_promote_boosts_weight(self):
        self.mgr.add("q", "a")
        self.mgr.promote(1, boost=2.0)
        self.assertGreater(self.mgr.get(1)["weight"], 1.0)

    # ------------------------------------------------------------------
    # decay
    # ------------------------------------------------------------------

    def test_decay_updates_weights(self):
        mid = self.mgr.add("q", "a")
        self._age(mid, 30)
        self.mgr.decay()
        self.assertLess(self.mgr.get(mid)["weight"], 1.0)

    def test_decay_archives_low_weight(self):
        mid = self.mgr.add("q", "a")
        self._age(mid, 90)
        self.mgr.decay()
        self.assertEqual(self.mgr.get(mid)["status"], "experience")

    # ------------------------------------------------------------------
    # query / search
    # ------------------------------------------------------------------

    def test_query_returns_results(self):
        self.mgr.add("nvme submission queue", "doorbell")
        results = self.mgr.query("nvme submission")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["answer"], "doorbell")

    def test_query_ranks_similar_higher(self):
        self.mgr.add("how to register a bdev", "via bdev lib")
        self.mgr.add("unrelated cooking recipe", "eggs")
        results = self.mgr.query("how to register bdev module")
        self.assertEqual(results[0]["question"], "how to register a bdev")

    def test_search_category_prefix(self):
        self.mgr.add("q1", "a", category="bdev/nvme/pcie")
        self.mgr.add("q2", "a", category="event/reactor")
        results = self.mgr.search("queue", category="bdev")
        # no FTS hit for "queue" — fallback similarity on "q1" question
        self.assertTrue(all(r["category"].startswith("bdev")
                            for r in results))

    # ------------------------------------------------------------------
    # governance
    # ------------------------------------------------------------------

    def test_split_and_merge_and_move(self):
        parent = self.mgr.add("broad question", "broad answer",
                              tags=["t"], node_ids=["n1"])
        children = self.mgr.split(parent, [
            {"question": "narrow one", "answer": "a1"},
            {"question": "narrow two", "answer": "a2"},
        ])
        self.assertEqual(self.mgr.get(parent)["status"], "split")
        self.assertEqual(len(children), 2)

        a = self.mgr.add("duplicate q", "a", no_merge=True)
        b = self.mgr.add("duplicate q", "b", no_merge=True)
        canon = self.mgr.merge([a, b])
        self.assertEqual(self.mgr.get(canon)["merged_count"], 1)

        self.mgr.move(children[0], "moved/category")
        self.assertEqual(
            self.mgr.get(children[0])["category_id"],
            self.mgr.store.category_id("moved/category"))

    # ------------------------------------------------------------------
    # scratch
    # ------------------------------------------------------------------

    def test_save_and_load_scratch(self):
        self.mgr.save_scratch("s1", chain_context={"chains": [["a", "b"]]})
        loaded = self.mgr.load_scratch("s1")
        self.assertEqual(loaded["chain_context"]["chains"],
                         [["a", "b"]])

    def test_refine_scratch_to_persistent(self):
        # graph fixture so node verification passes
        nodes = [{"id": "a", "name": "a", "source_file": "/tmp/x.c",
                  "line": 1, "domain": "test", "labels": [],
                  "is_empty": False},
                 {"id": "b", "name": "b", "source_file": "/tmp/x.c",
                  "line": 2, "domain": "test", "labels": [],
                  "is_empty": False}]
        with open(os.path.join(self.graph_dir, "domain_test.json"), "w") as f:
            json.dump({"nodes": nodes, "edges": []}, f)
        with open(os.path.join(self.graph_dir,
                               "code2database_master.json"), "w") as f:
            json.dump({"source_root": "/tmp",
                       "domains": {"test": "domain_test.json"}}, f)
        self.mgr.save_scratch("s2", chain_context={
            "chains": [{"steps": [{"id": "a"}, {"id": "b"}],
                        "from": "a", "to": "b"}]})
        mid = self.mgr.refine_scratch(
            "s2", question="refined q", answer="refined a",
            graph_dir=self.graph_dir)
        # No graph fixture — node verification needs master file; the
        # chains have no node ids so no verification is attempted.
        self.assertGreaterEqual(mid, 1)
        entry = self.mgr.get(mid)
        self.assertEqual(entry["promoted_from"], "s2")

    def test_session_expiry(self):
        self.mgr.save_session("s3", call_chains=[], ttl_hours=1e-5)
        self.assertIn("session_id", self.mgr.restore_session("s3"))
        # expired sessions restore as empty dict (after TTL)
        import time
        time.sleep(0.05)
        self.assertEqual(self.mgr.restore_session("s3"), {})

    # ------------------------------------------------------------------
    # packs / consolidate / export-import
    # ------------------------------------------------------------------

    def test_lite_pack(self):
        self.mgr.add("q", "a")
        pack = self.mgr.generate_pack("lite")
        self.assertIn("top_questions", pack)
        self.assertTrue(os.path.exists(os.path.join(
            self.graph_dir, ".memory_pack_lite.json")))

    def test_standard_pack(self):
        self.mgr.add("q", "a")
        pack = self.mgr.generate_pack("standard")
        self.assertIn("all_hot", pack)

    def test_consolidate(self):
        self.mgr.add("q", "a")
        summary = self.mgr.consolidate()
        self.assertEqual(summary["trusted"], 1)

    def test_export_import(self):
        self.mgr.add("q", "a", category="cat1")
        out = os.path.join(self.tmp.name, "export.json")
        self.mgr.export_for_debug(out)
        n = self.mgr.import_from_json(out, merge=True)
        self.assertEqual(n, 0)  # duplicate skipped


def _flatten(nodes):
    out = []
    for n in nodes:
        out.append(n)
        out.extend(_flatten(n["children"]))
    return out


if __name__ == "__main__":
    unittest.main()
