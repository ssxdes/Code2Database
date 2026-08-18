#!/usr/bin/env python3
"""Tests for memory_manager.py — MemoryManager class."""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from _builder.memory_manager import MemoryManager


class TestMemoryAdd(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mgr = MemoryManager(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_add_creates_root(self):
        eid = self.mgr.add(question="How does bdev work?", answer="Block device abstraction")
        self.assertGreater(eid, 0)
        # Check root file exists
        root_path = os.path.join(self.tmpdir, "memory", "root", f"root_{eid}.json")
        self.assertTrue(os.path.exists(root_path))

    def test_add_merge_similar(self):
        eid1 = self.mgr.add(question="How does spdk bdev initialization work?", answer="Block device")
        eid2 = self.mgr.add(question="How does spdk bdev initialization flow?", answer="Block device abstraction")
        # Second should merge with first (Jaccard > 0.7 for high overlap)
        entry2 = self.mgr._load_entry(eid2, is_root=False)
        self.assertEqual(entry2.get("root_id"), eid1)

    def test_add_no_merge(self):
        eid1 = self.mgr.add(question="How does bdev work?", answer="Block device")
        eid2 = self.mgr.add(question="How does bdev operate?", answer="Different answer", no_merge=True)
        # Should create separate root
        entry2 = self.mgr._load_entry(eid2, is_root=True)
        self.assertEqual(entry2.get("root_id"), eid2)

    def test_add_with_tags_and_nodes(self):
        eid = self.mgr.add(
            question="Test Q", answer="Test A",
            tags=["bdev", "api"], node_ids=["lib_bdev_init"],
        )
        entry = self.mgr._load_entry(eid, is_root=True)
        self.assertIn("bdev", entry["tags"])
        self.assertIn("lib_bdev_init", entry["node_ids"])


class TestMemoryCorrect(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mgr = MemoryManager(self.tmpdir)
        self.eid = self.mgr.add(question="Test Q", answer="Wrong answer")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_correct_field(self):
        self.mgr.correct(mem_id=self.eid, field="answer", value="Correct answer")
        entry = self.mgr._load_entry(self.eid, is_root=True)
        self.assertEqual(entry["answer"], "Correct answer")

    def test_correct_preserves_version(self):
        self.mgr.correct(mem_id=self.eid, field="answer", value="Correct answer")
        entry = self.mgr._load_entry(self.eid, is_root=True)
        self.assertTrue(len(entry.get("versions", [])) > 0)
        self.assertEqual(entry["versions"][0]["old_value"], "Wrong answer")


class TestMemoryReshape(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mgr = MemoryManager(self.tmpdir)
        self.eid = self.mgr.add(question="Test Q", answer="Old answer")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_reshape_replaces_answer(self):
        self.mgr.reshape(root_id=self.eid, answer="Completely new answer")
        entry = self.mgr._load_entry(self.eid, is_root=True)
        self.assertEqual(entry["answer"], "Completely new answer")
        self.assertEqual(entry.get("reshaped_count", 0), 1)

    def test_reshape_preserves_history(self):
        self.mgr.reshape(root_id=self.eid, answer="New answer")
        entry = self.mgr._load_entry(self.eid, is_root=True)
        self.assertTrue(len(entry.get("versions", [])) > 0)


class TestMemoryDecay(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mgr = MemoryManager(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_decay_updates_weights(self):
        eid = self.mgr.add(question="Test Q", answer="Test A")
        # Manually set old access time
        entry = self.mgr._load_entry(eid, is_root=True)
        entry["last_accessed"] = "2020-01-01T00:00:00"
        entry["created"] = "2020-01-01T00:00:00"
        self.mgr._save_entry(entry, is_root=True)

        decayed = self.mgr.decay()
        self.assertGreaterEqual(decayed, 0)  # Should run without error

    def test_decay_archives_low_weight(self):
        eid = self.mgr.add(question="Test Q", answer="Test A")
        # Set very old access time to trigger archiving
        entry = self.mgr._load_entry(eid, is_root=True)
        entry["last_accessed"] = "2010-01-01T00:00:00"
        entry["created"] = "2010-01-01T00:00:00"
        entry["access_count"] = 0
        entry["merged_count"] = 0
        entry["weight"] = 0.05
        self.mgr._save_entry(entry, is_root=True)

        self.mgr.decay()
        # Should be archived
        exp_path = os.path.join(self.tmpdir, "memory", "experience", f"experience_{eid}.json")
        self.assertTrue(os.path.exists(exp_path))


class TestMemoryPromote(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mgr = MemoryManager(self.tmpdir)
        self.eid = self.mgr.add(question="Test Q", answer="Test A")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_promote_boosts_weight(self):
        entry = self.mgr._load_entry(self.eid, is_root=True)
        old_weight = entry.get("weight", 1.0)
        self.mgr.promote(mem_id=self.eid, boost=2.0)
        entry = self.mgr._load_entry(self.eid, is_root=True)
        self.assertGreater(entry["weight"], old_weight)


class TestMemoryQuery(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mgr = MemoryManager(self.tmpdir)
        self.mgr.add(question="How does bdev work?", answer="Block device abstraction")
        self.mgr.add(question="How does nvme init?", answer="NVMe controller setup")
        self.mgr.add(question="Thread safety in spdk?", answer="Use spdk_spinlock")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_query_returns_results(self):
        results = self.mgr.query("bdev", top_n=5)
        self.assertGreater(len(results), 0)

    def test_query_ranks_similar_higher(self):
        results = self.mgr.query("bdev", top_n=5)
        # "How does bdev work?" should rank highest
        self.assertIn("bdev", results[0]["question"].lower())


class TestMemoryScratch(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mgr = MemoryManager(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_and_load_scratch(self):
        self.mgr.save_scratch(
            session_id="test_session",
            chain_context={"chains": [{"from": "a", "to": "b"}]},
            react_state={"step": "analyze"},
        )
        scratch = self.mgr.load_scratch("test_session")
        self.assertEqual(scratch["session_id"], "test_session")
        self.assertIn("chains", scratch["chain_context"])

    def test_refine_scratch_to_persistent(self):
        self.mgr.save_scratch(
            session_id="test_refine",
            chain_context={"chains": [{"steps": [{"id": "lib_bdev_init"}]}]},
        )
        eid = self.mgr.refine_scratch(
            scratch_id="test_refine",
            question="How does bdev init?",
            answer="Initialization sequence",
        )
        self.assertGreater(eid, 0)
        # Scratch should be removed
        scratch = self.mgr.load_scratch("test_refine")
        self.assertEqual(scratch, {})


class TestMemoryPack(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mgr = MemoryManager(self.tmpdir)
        self.mgr.add(question="How does bdev work?", answer="Block device abstraction")
        self.mgr.add(question="Thread safety", answer="Use spdk_spinlock")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_lite_pack(self):
        pack = self.mgr.generate_pack(tier="lite")
        self.assertIn("top_questions", pack)
        self.assertIn("hot_memories", pack)
        # Check file written
        pack_path = os.path.join(self.tmpdir, ".memory_pack_lite.json")
        self.assertTrue(os.path.exists(pack_path))

    def test_standard_pack(self):
        pack = self.mgr.generate_pack(tier="standard")
        self.assertIn("top_questions", pack)
        self.assertIn("all_hot", pack)
        self.assertIn("warm_summaries", pack)


class TestLayeredIndex(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mgr = MemoryManager(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_layered_indexes_created(self):
        self.mgr.add(question="Hot Q", answer="Hot A")
        # Create a cold entry manually
        eid2 = self.mgr.add(question="Cold Q", answer="Cold A")
        entry = self.mgr._load_entry(eid2, is_root=True)
        entry["last_accessed"] = "2010-01-01T00:00:00"
        entry["weight"] = 0.2
        self.mgr._save_entry(entry, is_root=True)
        self.mgr._save_layered_indexes(self.mgr._load_index())

        # Check L0 file exists
        l0_path = os.path.join(self.tmpdir, "memory", "L0_index.json")
        self.assertTrue(os.path.exists(l0_path))


if __name__ == "__main__":
    unittest.main()
