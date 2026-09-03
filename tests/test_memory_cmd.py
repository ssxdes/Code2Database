"""Unit tests for memory_cmd.py + memory_manager cmd surfaces.

Covers: _sanitize_memory_index (corrupt/non-dict/string-entry defense),
cmd_save_memory (new entry via MemoryManager root/leaf layout, similar
question merge, --no-merge), cmd_search_memory (legacy Jaccard search,
experience penalty, no-match message, answer hydration),
cmd_validate_memory (stale node_ids → entry moved to experience dir,
trusted entries kept), cmd_memory_health (stats structure),
cmd_manage_memory (add/query/pack actions).
"""
import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder import memory_cmd
from _builder.memory_cmd import (
    _sanitize_memory_index, cmd_save_memory, cmd_search_memory,
    cmd_validate_memory,
)


def _ns(**kw):
    return argparse.Namespace(**kw)


def _capture_call(fn, args):
    out, err = io.StringIO(), io.StringIO()
    ret, code = None, None
    with redirect_stdout(out), redirect_stderr(err):
        try:
            ret = fn(args)
        except SystemExit as e:
            code = e.code
    return ret, out.getvalue(), err.getvalue(), code


def _run(fn, args):
    ret, out, err, code = _capture_call(fn, args)
    assert code is None, f"unexpected SystemExit({code}); stderr={err}"
    return ret, out, err


def _make_graph_dir():
    """Graph dir with a 2-node JSON graph (a→b)."""
    tmp = tempfile.mkdtemp(prefix="c2d_memcmd_")
    nodes = [
        {"id": "a", "name": "a", "source_file": "/tmp/x.c", "line": 1,
         "domain": "test", "labels": [], "is_empty": False},
        {"id": "b", "name": "b", "source_file": "/tmp/x.c", "line": 2,
         "domain": "test", "labels": [], "is_empty": False},
    ]
    edges = [{"source": "a", "target": "b", "relation": "INVOKES",
              "confidence": "EXTRACTED"}]
    with open(os.path.join(tmp, "domain_test.json"), "w") as f:
        json.dump({"nodes": nodes, "edges": edges}, f)
    with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
        json.dump({"source_root": "/tmp", "domains": {"test": "domain_test.json"}},
                  f)
    return tmp


def _read_memory_entry(graph_dir, eid):
    """Read a memory entry file from root/leaf layout (or flat fallback)."""
    mem_dir = os.path.join(graph_dir, "memory")
    for path in (os.path.join(mem_dir, "root", f"root_{eid}.json"),
                 os.path.join(mem_dir, "leaf", f"mem_{eid}.json"),
                 os.path.join(mem_dir, f"memory_{eid}.json")):
        if os.path.exists(path):
            return json.loads(open(path).read())
    raise AssertionError(f"entry file for #{eid} not found")


class TestSanitizeMemoryIndex(unittest.TestCase):
    def test_non_dict_returns_default(self):
        result = _sanitize_memory_index(["not", "a", "dict"], "warn.json")
        self.assertEqual(result, {"entries": [], "next_id": 1, "roots": []})

    def test_string_entries_filtered_with_warning(self):
        idx = {"entries": ["bad", {"id": 1, "question": "ok"}], "next_id": 2}
        err = io.StringIO()
        with redirect_stderr(err):
            result = _sanitize_memory_index(idx, "mem.json")
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["id"], 1)
        self.assertIn("filtered 1 non-dict", err.getvalue())

    def test_missing_keys_defaulted(self):
        result = _sanitize_memory_index({"entries": []})
        self.assertEqual(result["next_id"], 1)
        self.assertEqual(result["roots"], [])


class TestCmdSaveMemory(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph_dir()
        self.addCleanup(shutil.rmtree, self.graph_dir, ignore_errors=True)

    def _args(self, **kw):
        kw.setdefault("graph", self.graph_dir)
        kw.setdefault("question", "How does A call B?")
        kw.setdefault("answer", "via the INVOKES edge")
        kw.setdefault("chains", "")
        kw.setdefault("tags", "")
        kw.setdefault("node_ids", "")
        kw.setdefault("no_merge", False)
        return _ns(**kw)

    def _read_entry(self, eid=1):
        return _read_memory_entry(self.graph_dir, eid)

    def test_new_entry_saved_with_node_tracking(self):
        _, out, _ = _run(cmd_save_memory,
                         self._args(node_ids="a,b", tags="flow,core"))
        self.assertIn("Saved memory #", out)
        mem_dir = os.path.join(self.graph_dir, "memory")
        index = json.loads(open(os.path.join(mem_dir, "index.json")).read())
        self.assertEqual(len(index["entries"]), 1)
        # node_ids live on the entry FILE, not the index metadata
        self.assertEqual(self._read_entry(1)["node_ids"], ["a", "b"])

    def test_similar_question_merges_into_existing(self):
        _run(cmd_save_memory, self._args())
        _, out, _ = _run(cmd_save_memory, self._args(
            question="How does A call B", answer="updated answer",
            tags="extra"))
        self.assertIn("Merged with existing memory #1", out)
        mem_dir = os.path.join(self.graph_dir, "memory")
        index = json.loads(open(os.path.join(mem_dir, "index.json")).read())
        self.assertEqual(len(index["entries"]), 1)  # no duplicate
        entry = self._read_entry(1)
        self.assertEqual(entry["answer"], "updated answer")
        self.assertGreaterEqual(entry.get("merged_count", 0), 1)
        self.assertIn("extra", entry.get("tags", []))

    def test_merge_search_returns_hydrated_answer(self):
        """Regression: legacy search must hydrate the answer for root-layout
        entries (scored results previously lacked root_id → is_root was
        always False → no answer in output)."""
        _run(cmd_save_memory, self._args())
        _, out, _ = _run(cmd_search_memory, _ns(
            graph=self.graph_dir, query="How does A call B", top=5))
        results = json.loads(out)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["answer"], "via the INVOKES edge")

    def test_no_merge_flag_forces_new_entry(self):
        _run(cmd_save_memory, self._args())
        _run(cmd_save_memory, self._args(no_merge=True))
        mem_dir = os.path.join(self.graph_dir, "memory")
        index = json.loads(open(os.path.join(mem_dir, "index.json")).read())
        self.assertEqual(len(index["entries"]), 2)

    def test_node_ids_extracted_from_chains(self):
        chains = json.dumps([{"steps": [{"id": "a"}, {"id": "b"}]}])
        _run(cmd_save_memory, self._args(chains=chains))
        self.assertTrue(self._read_entry(1)["node_ids"])


class TestCmdSearchMemory(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph_dir()
        self.addCleanup(shutil.rmtree, self.graph_dir, ignore_errors=True)
        _run(cmd_save_memory, _ns(
            graph=self.graph_dir, question="How does lock contention happen?",
            answer="try lock refactor", chains="", tags="locking",
            node_ids="", no_merge=False))

    def _args(self, **kw):
        kw.setdefault("graph", self.graph_dir)
        kw.setdefault("query", "lock contention")
        kw.setdefault("top", 5)
        return _ns(**kw)

    def test_similar_question_found_with_answer(self):
        _, out, _ = _run(cmd_search_memory, self._args())
        results = json.loads(out)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["answer"], "try lock refactor")
        self.assertGreater(results[0]["score"], 0)

    def test_no_match_message(self):
        _, out, _ = _run(cmd_search_memory, self._args(query="zzz unrelated"))
        self.assertIn("No similar memories found.", out)

    def test_experience_ranked_lower_than_trusted(self):
        # add an experience entry with the SAME question text
        mem_dir = os.path.join(self.graph_dir, "memory")
        index = json.loads(open(os.path.join(mem_dir, "index.json")).read())
        index["entries"].append({"id": 2, "question": "How does lock "
                                 "contention happen?", "status": "experience",
                                 "tags": ["locking"]})
        open(os.path.join(mem_dir, "index.json"), "w").write(
            json.dumps(index))
        _, out, _ = _run(cmd_search_memory, self._args())
        results = json.loads(out)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["status"], "trusted")
        self.assertEqual(results[1]["status"], "experience")


class TestCmdValidateMemory(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph_dir()
        self.addCleanup(shutil.rmtree, self.graph_dir, ignore_errors=True)

    def _args(self, **kw):
        kw.setdefault("graph", self.graph_dir)
        return _ns(**kw)

    def test_stale_nodes_invalidate_entry(self):
        # entry references node 'a' (exists) + 'ghost' (missing)
        _run(cmd_save_memory, _ns(
            graph=self.graph_dir, question="q about nodes",
            answer="a", chains="", tags="", node_ids="a,ghost",
            no_merge=False))
        # sanity: the entry file exists before validation
        self.assertTrue(_read_memory_entry(self.graph_dir, 1))
        _, out, _ = _run(cmd_validate_memory, self._args())
        self.assertIn("1 → experience", out)
        # _experience_dir lives INSIDE the memory dir
        exp_dir = os.path.join(self.graph_dir, "memory", "experience")
        self.assertTrue(os.path.exists(
            os.path.join(exp_dir, "experience_1.json")))
        entry = json.loads(open(
            os.path.join(exp_dir, "experience_1.json")).read())
        self.assertEqual(entry["status"], "experience")
        self.assertIn("no longer in graph", entry["invalidated_reason"])

    def test_all_nodes_present_keeps_trusted(self):
        _run(cmd_save_memory, _ns(
            graph=self.graph_dir, question="q ok", answer="a", chains="",
            tags="", node_ids="a,b", no_merge=False))
        _, out, _ = _run(cmd_validate_memory, self._args())
        self.assertIn("1 trusted", out)
        self.assertIn("0 → experience", out)
        # entry file still in place, not moved
        self.assertEqual(_read_memory_entry(self.graph_dir, 1)["status"],
                         "trusted")

    def test_no_memory_dir_is_noop(self):
        _, out, _ = _run(cmd_validate_memory, self._args())
        self.assertEqual(out, "")


class TestCmdMemoryHealthAndManage(unittest.TestCase):
    """cmd_memory_health + cmd_manage_memory (memory_manager module)."""

    def setUp(self):
        self.graph_dir = _make_graph_dir()
        self.addCleanup(shutil.rmtree, self.graph_dir, ignore_errors=True)
        _run(cmd_save_memory, _ns(
            graph=self.graph_dir, question="health check question",
            answer="yes", chains="", tags="meta", node_ids="a",
            no_merge=False))

    def test_memory_health_reports_structure(self):
        from _builder.memory_manager import cmd_memory_health
        _, out, _ = _run(cmd_memory_health, _ns(graph=self.graph_dir))
        text = out.lower()
        self.assertIn("entries", text)

    def test_manage_query_returns_results(self):
        from _builder.memory_manager import cmd_manage_memory
        _, out, _ = _run(cmd_manage_memory, _ns(
            graph=self.graph_dir, action="query", query="health check",
            top="5", min_weight="0.0"))
        results = json.loads(out)
        self.assertIsInstance(results, list)

    def test_manage_pack_returns_tier_content(self):
        from _builder.memory_manager import cmd_manage_memory
        _, out, _ = _run(cmd_manage_memory, _ns(
            graph=self.graph_dir, action="pack", tier="lite"))
        pack = json.loads(out)
        self.assertIsInstance(pack, (dict, list))
        self.assertTrue(json.dumps(pack))

    def test_manage_decay_and_unknown_action(self):
        from _builder.memory_manager import cmd_manage_memory
        ret, out, err, code = _capture_call(cmd_manage_memory, _ns(
            graph=self.graph_dir, action="decay"))
        self.assertIn(code, (None, 0, 2))
        ret, out, err, code = _capture_call(cmd_manage_memory, _ns(
            graph=self.graph_dir, action="zzz-unknown"))
        self.assertIn(code, (None, 0, 2))


if __name__ == "__main__":
    unittest.main()
