"""brief-suggest: the memory→knowledge graduation gate.

High-value memories (weight, absorbed variants) are proposed as brief
additions with ready-to-run brief-update commands. Suggestive only —
the brief is deliberately hand-curated and size-budgeted; overflow
belongs in memory.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.brief import brief_suggest, save_brief
from _builder.memory_store import MemoryStore


def _weight_store(graph_dir):
    """Store whose adds all land as separate roots with real weights."""
    return MemoryStore(graph_dir)


class TestBriefSuggest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph_dir = os.path.join(self.tmp.name, "graph")
        os.makedirs(self.graph_dir, exist_ok=True)
        self.store = _weight_store(self.graph_dir)

    def _boost(self, mem_id, weight):
        """Force a weight so eligibility is deterministic."""
        import sqlite3
        conn = sqlite3.connect(self.store.db_path)
        conn.execute("UPDATE memories SET weight = ? WHERE id = ?",
                     (weight, mem_id))
        conn.commit()
        conn.close()

    def test_imperative_memory_suggests_hard_rule(self):
        mid = self.store.add("must hold the bdev lock before calling submit",
                             "call bdev_lock first or io crashes", no_merge=True)
        self._boost(mid, 3.0)
        result = brief_suggest(self.graph_dir)
        matches = [s for s in result["suggestions"] if s["memory_id"] == mid]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["section"], "hard_rules")
        self.assertEqual(matches[0]["item"]["rule"],
                         "must hold the bdev lock before calling submit")
        self.assertIn("brief-update", matches[0]["command"])
        self.assertIn("--add hard_rules", matches[0]["command"])

    def test_pitfall_how_and_where_routing(self):
        a = self.store.add("common pitfall: forgetting to free io_channel",
                           "pair every alloc with free", no_merge=True)
        b = self.store.add("how does the bdev reset flow work",
                           "reset propagates via bdev_reset_recursive", no_merge=True)
        c = self.store.add("where is the queue depth configured",
                           "see bdev.h NVME_QDEPTH", no_merge=True)
        for mid in (a, b, c):
            self._boost(mid, 3.0)
        result = brief_suggest(self.graph_dir)
        sections = {s["memory_id"]: s["section"] for s in result["suggestions"]}
        self.assertEqual(sections[a], "pitfalls")
        self.assertEqual(sections[b], "key_abstractions")
        self.assertEqual(sections[c], "query_paths")

    def test_low_weight_memory_not_suggested(self):
        self.store.add("minor question", "minor answer", no_merge=True)
        result = brief_suggest(self.graph_dir)
        self.assertEqual(result["suggestions"], [])

    def test_covered_memory_marked_not_duplicated(self):
        mid = self.store.add("must hold the bdev lock before calling submit",
                             "call bdev_lock first", no_merge=True)
        self._boost(mid, 3.0)
        save_brief(self.graph_dir, {
            "schema_version": 1, "project": "p", "one_liner": "",
            "description": "", "must_know": "",
            "hard_rules": [{"rule": "hold the bdev lock before calling "
                                    "submit", "type": "api", "detail": "",
                            "evidence": ""}],
            "modes": [], "key_abstractions": [], "conventions": [],
            "pitfalls": [], "query_paths": [], "graph_stats": {},
            "updated_at": "",
        })
        result = brief_suggest(self.graph_dir)
        self.assertEqual(result["suggestions"], [])
        covered = [c for c in result["covered"] if c["memory_id"] == mid]
        self.assertEqual(len(covered), 1)

    def test_no_memory_store_returns_empty(self):
        result = brief_suggest(self.graph_dir)
        self.assertEqual(result["suggestions"], [])
        self.assertEqual(result["candidates_considered"], 0)


if __name__ == "__main__":
    unittest.main()
