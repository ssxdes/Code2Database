"""Regression tests for memory_manager robustness fixes.

Pins three defects fixed in this round:
  1. A corrupt (truncated) index.json / mem_N.json / experience index
     escaped as JSONDecodeError and took down every memory operation
     until the file was deleted by hand. Now reported + bypassed.
  2. _merge_to_root's condition `new_weight > old_weight or
     entry.get("answer", "")` made the weight comparison dead code —
     any non-empty answer replaced the root, so a weak late merge
     overwrote a strong curated root answer.
  3. promote()'s `weight += boost` was erased by the next
     _update_weight() (decay/consolidate recompute weight from the
     formula, which didn't know about boosts). The boost is now a
     persistent field included in the formula.
"""
import io
import contextlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.memory_manager import MemoryManager


class _TmpMgr(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mgr = MemoryManager(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestCorruptFileTolerance(_TmpMgr):
    def test_corrupt_index_returns_default_not_crash(self):
        path = os.path.join(self.tmpdir, "memory", "index.json")
        with open(path, "w") as f:
            f.write('{"entries": [ TRUNC')
        idx = self.mgr._load_index()
        self.assertEqual(idx["entries"], [])
        self.assertEqual(idx["next_id"], 1)
        # and the system stays usable — add works over the corrupt file
        with contextlib.redirect_stdout(io.StringIO()):
            eid = self.mgr.add("q", "a")
        self.assertGreater(eid, 0)

    def test_corrupt_entry_returns_empty_not_crash(self):
        with contextlib.redirect_stdout(io.StringIO()):
            eid = self.mgr.add("q", "a")
        path = os.path.join(self.tmpdir, "memory", "root",
                            f"root_{eid}.json")
        with open(path, "w") as f:
            f.write('{"id": 3, "ans')
        self.assertEqual(self.mgr._load_entry(eid, is_root=True), {})

    def test_corrupt_experience_index_returns_default(self):
        os.makedirs(os.path.join(self.tmpdir, "memory", "experience"),
                    exist_ok=True)
        path = os.path.join(self.tmpdir, "memory", "experience",
                            "index.json")
        with open(path, "w") as f:
            f.write("[[[")
        self.assertEqual(self.mgr._load_exp_index(), {"entries": []})


class TestMergeStrongerWins(_TmpMgr):
    def test_weak_merge_does_not_overwrite_strong_root(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rid = self.mgr.add("how does locking work",
                               answer="curated detailed answer " * 20)
            # merge a weak sibling with the same question
            self.mgr.add("how does locking work", answer="x")
        root = self.mgr._load_entry(rid, is_root=True)
        self.assertTrue(root["answer"].startswith("curated detailed"),
                        f"weak merge overwrote strong root: "
                        f"{root['answer'][:40]!r}")

    def test_strong_merge_still_replaces(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rid = self.mgr.add("how does locking work", answer="weak")
            # fresh sibling with a much stronger answer (recency alone
            # makes its computed weight higher than the aged root)
            self.mgr.add("how does locking work",
                         answer="much more detailed answer " * 20)
        root = self.mgr._load_entry(rid, is_root=True)
        self.assertTrue(root["answer"].startswith("much more detailed"))


class TestPromoteBoostPersists(_TmpMgr):
    def test_boost_survives_decay(self):
        with contextlib.redirect_stdout(io.StringIO()):
            mid = self.mgr.add("promote me", answer="answer")
            self.mgr.promote(mid, boost=2.0)
            w_promoted = self.mgr._load_entry(mid, is_root=True)["weight"]
            self.mgr.decay()
            w_after = self.mgr._load_entry(mid, is_root=True)["weight"]
        self.assertGreaterEqual(w_promoted, 3.0)
        # before the fix, decay recomputed weight from the formula and
        # the boost vanished (weight dropped back to ~1.x)
        self.assertGreaterEqual(
            w_after, 2.0,
            f"promote boost erased by decay: {w_promoted} -> {w_after}")

    def test_boost_field_is_persisted(self):
        with contextlib.redirect_stdout(io.StringIO()):
            mid = self.mgr.add("promote me", answer="answer")
            self.mgr.promote(mid, boost=1.5)
        entry = self.mgr._load_entry(mid, is_root=True)
        self.assertAlmostEqual(entry["boost"], 1.5)


class TestConcurrentAdd(_TmpMgr):
    """add() must hold a lock across load->mutate->save of the index.

    Without it, concurrent adds both read next_id=N and silently
    clobbered each other's entry (lost memories / duplicate ids). The
    daemon's auto-consolidate racing a user save-memory hit this.
    """

    def test_threaded_adds_get_unique_ids(self):
        import threading
        errs = []

        def worker(i):
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.mgr.add(f"question {i}", f"answer {i}")
            except Exception as exc:  # pragma: no cover
                errs.append(exc)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errs, [])
        idx = self.mgr._load_index()
        ids = [e["id"] for e in idx["entries"]]
        self.assertEqual(len(ids), 12)
        self.assertEqual(len(set(ids)), 12)
        self.assertEqual(idx["next_id"], 13)

    def test_multiprocess_adds_get_unique_ids(self):
        import subprocess
        code = (
            "import sys, io, contextlib\n"
            "sys.path.insert(0, 'scripts')\n"
            "from _builder.memory_manager import MemoryManager\n"
            f"mgr = MemoryManager({self.tmpdir!r})\n"
            "with contextlib.redirect_stdout(io.StringIO()):\n"
            "    mgr.add('proc q', 'a')\n"
        )
        procs = [subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=os.path.join(os.path.dirname(__file__), ".."),
            env={**os.environ, "PYTHONPATH": "scripts"})
            for _ in range(4)]
        for p in procs:
            p.wait(timeout=30)
        idx = self.mgr._load_index()
        ids = [e["id"] for e in idx["entries"]]
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual(idx["next_id"], 5)


if __name__ == "__main__":
    unittest.main()
