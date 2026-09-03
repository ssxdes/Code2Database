"""Robustness tests for the SQLite-backed MemoryManager facade."""
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.memory_manager import MemoryManager


class MemoryRobustnessTest(unittest.TestCase):
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
            "UPDATE memories SET created = ?, last_accessed = ? "
            "WHERE id = ?", (past, past, mem_id))
        conn.commit()
        conn.close()

    def test_weak_merge_does_not_overwrite_strong_root(self):
        strong = "very thorough answer. " * 60
        r = self.mgr.add("How does nvme submit IO?", strong)
        self.mgr.add("How does nvme submit IO cmds?", "weak")
        self.assertEqual(self.mgr.get(r)["answer"], strong)

    def test_strong_merge_still_replaces(self):
        self.mgr.add("How does nvme submit IO?", "short")
        # A variant with a much longer answer outweighs the short root.
        self.mgr.add("How does nvme submit IO requests?",
                     "far more thorough answer. " * 60)
        self.assertIn("far more thorough",
                      self.mgr.get(1)["answer"])

    def test_boost_survives_decay(self):
        self.mgr.add("q", "a")
        self.mgr.promote(1, boost=3.0)
        boosted = self.mgr.get(1)["weight"]
        self._age(1, days=10)
        self.mgr.decay()
        after = self.mgr.get(1)["weight"]
        # weight decays a little but the boost factor keeps it boosted
        self.assertGreater(after, 1.5)
        self.assertLess(after, boosted * 1.01)

    def test_boost_field_is_persisted(self):
        self.mgr.add("q", "a")
        self.mgr.promote(1, boost=1.5)
        self.assertAlmostEqual(self.mgr.get(1)["boost"], 1.5)

    def test_threaded_adds_get_unique_ids(self):
        ids = []
        errors = []

        def worker(i):
            try:
                ids.append(self.mgr.add(f"thread {i} question",
                                        f"answer {i}"))
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(ids), 8)
        self.assertEqual(len(ids), len(set(ids)))
        # all entries present and queryable (id↔thread order is
        # nondeterministic — check the multiset of questions)
        questions = {self.mgr.get(mid)["question"] for mid in ids}
        self.assertEqual(questions,
                         {f"thread {i} question" for i in range(8)})

    def test_corrupt_db_handled_by_get(self):
        # A truncated db file should not crash _init_schema forever —
        # sqlite raises DatabaseError; the manager surfaces it at init.
        self.mgr.add("q", "a")
        db = os.path.join(self.graph_dir, "memory", "memory.db")
        # Write garbage AFTER closing all connections (setUp's store
        # holds none persistently).
        with open(db, "wb") as f:
            f.write(b"this is not a sqlite database" * 100)
        with self.assertRaises(Exception):
            MemoryManager(self.graph_dir)


if __name__ == "__main__":
    unittest.main()
