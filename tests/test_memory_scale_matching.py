"""Bounded similarity matching at scale.

The similarity paths (merge-on-save, correct_similar, compact's star
clustering, the CJK search channel) must stay correct as the store
grows: candidates come from the FTS top-K with a bounded
weight-ordered fallback instead of scanning every row.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.memory.memory_store import MemoryStore


class TestMergeMatchingAtScale(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph_dir = os.path.join(self.tmp.name, "store")
        os.makedirs(self.graph_dir)
        self.store = MemoryStore(self.graph_dir)

    def _fill(self, n=200):
        # Distinct-topic memories so nothing merges during the fill.
        ids = []
        for i in range(n):
            ids.append(self.store.add(
                f"topic number {i} covers allocator shard {i} policy",
                f"answer about shard {i}",
                no_merge=False))
        return ids

    def test_near_duplicate_of_early_entry_still_merges(self):
        first = self._fill(200)[0]
        dup = self.store.add(
            "topic number 0 covers allocator shard 0 policy",
            "a better answer about shard 0")
        row = self.store.get(dup)
        self.assertEqual(row["root_id"], first,
                         "near-duplicate of an early entry must merge "
                         "into its root even with 200 rows in between")

    def test_cjk_near_duplicate_merges(self):
        self._fill(50)
        base = self.store.add("队列门铃寄存器如何触发写入",
                              "写提交尾部即可", no_merge=True)
        dup = self.store.add("队列门铃寄存器如何触发写入",
                             "更详细的写法说明")
        self.assertEqual(self.store.get(dup)["root_id"], base)

    def test_correct_similar_finds_target_at_scale(self):
        self._fill(200)
        result = self.store.correct_similar(
            question="topic number 7 covers allocator shard 7 policy",
            answer="the corrected answer")
        self.assertEqual(result["action"], "corrected")
        self.assertIn("shard 7", result["matched_question"])


class TestCompactAtScale(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph_dir = os.path.join(self.tmp.name, "store")
        os.makedirs(self.graph_dir)
        self.store = MemoryStore(self.graph_dir)

    def test_three_groups_converge(self):
        # 3 topics × 8 near-duplicates each; roots created no_merge so
        # only compact can group them.
        topics = ["nvme submission queue doorbell policy",
                  "tcp retransmit timer restart path",
                  "rpc client connection retry backoff"]
        for t in topics:
            for i in range(8):
                self.store.add(f"{t} variant {i}",
                               f"answer {i} " + "x" * (i * 10),
                               no_merge=True)
        report = self.store.compact()
        self.assertEqual(report["merged_groups"], 3, report)
        self.assertEqual(len(report["merged_ids"]), 21)
        # Idempotent: a second run merges nothing.
        second = self.store.compact()
        self.assertEqual(second["merged_groups"], 0)

    def test_cjk_group_merges(self):
        for i in range(6):
            self.store.add(f"队列门铃寄存器触发写入方式 {i}",
                           f"答案 {i}", no_merge=True)
        report = self.store.compact()
        self.assertEqual(report["merged_groups"], 1, report)


class TestSearchBoundedChannel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph_dir = os.path.join(self.tmp.name, "store")
        os.makedirs(self.graph_dir)
        self.store = MemoryStore(self.graph_dir)

    def test_cjk_hit_found_among_many(self):
        for i in range(120):
            self.store.add(f"主题编号 {i} 涉及分配器分片策略 {i}",
                           f"关于分片 {i} 的回答", no_merge=True)
        target = self.store.add("队列门铃寄存器如何触发写入",
                                "写提交尾部即可", no_merge=True)
        results = self.store.search("门铃 寄存器 触发")
        self.assertTrue(results)
        self.assertEqual(results[0]["id"], target)


if __name__ == "__main__":
    unittest.main()
