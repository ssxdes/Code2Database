"""Unit tests for memory_cmd.py + memory_manager cmd surfaces (SQLite).

Covers: cmd_save_memory (DB entry creation, category/author flags,
--no-merge), cmd_search_memory (FTS search, category/author filters,
no-match message), cmd_validate_memory (stale node_ids → experience),
_auto_validate_memory (daemon path), cmd_memory_health (stats
structure), cmd_manage_memory (add/query/search/get/categories/
split/merge/move/pack actions).
"""
import argparse
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder import memory_cmd
from _builder.memory_cmd import (
    cmd_save_memory, cmd_search_memory, cmd_validate_memory,
    _auto_validate_memory,
)
from _builder.memory_manager import cmd_manage_memory, cmd_memory_health


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


def _db_rows(graph_dir):
    conn = sqlite3.connect(os.path.join(graph_dir, "memory", "memory.db"))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM memories ORDER BY id").fetchall()]
    finally:
        conn.close()


class TestSaveMemory(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph_dir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.graph_dir, ignore_errors=True)

    def test_save_creates_db_entry(self):
        _run(cmd_save_memory, _ns(graph=self.graph_dir,
                                  question="What does a do?",
                                  answer="calls b",
                                  chains="", tags="", node_ids="",
                                  category="", author="",
                                  no_merge=False))
        rows = _db_rows(self.graph_dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["question"], "What does a do?")
        self.assertEqual(rows[0]["answer"], "calls b")
        self.assertEqual(rows[0]["root_id"], rows[0]["id"])

    def test_save_with_category_and_author(self):
        _run(cmd_save_memory, _ns(graph=self.graph_dir,
                                  question="q", answer="a",
                                  chains="", tags="io,nvme", node_ids="",
                                  category="bdev/nvme/pcie",
                                  author="alice", no_merge=False))
        rows = _db_rows(self.graph_dir)
        self.assertEqual(json.loads(rows[0]["tags"]), ["io", "nvme"])
        self.assertEqual(rows[0]["author"], "alice")
        conn = sqlite3.connect(
            os.path.join(self.graph_dir, "memory", "memory.db"))
        paths = {r[0] for r in conn.execute(
            "SELECT path FROM categories").fetchall()}
        conn.close()
        self.assertEqual(paths, {"bdev", "bdev/nvme", "bdev/nvme/pcie"})

    def test_save_node_ids_from_chains(self):
        chains = json.dumps([{"steps": [{"id": "a"}, {"id": "b"}],
                              "from": "a", "to": "b"}])
        _run(cmd_save_memory, _ns(graph=self.graph_dir,
                                  question="q", answer="a",
                                  chains=chains, tags="", node_ids="",
                                  category="", author="", no_merge=False))
        rows = _db_rows(self.graph_dir)
        self.assertEqual(json.loads(rows[0]["node_ids"]), ["a", "b"])

    def test_save_prints_confirmation(self):
        _, out, _ = _run(cmd_save_memory, _ns(
            graph=self.graph_dir, question="q", answer="a",
            chains="", tags="", node_ids="",
            category="bdev/nvme", author="", no_merge=False))
        self.assertIn("Saved memory #1", out)
        self.assertIn("bdev/nvme", out)


class TestSearchMemory(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph_dir()
        _run(cmd_save_memory, _ns(
            graph=self.graph_dir,
            question="How does nvme submit IO?",
            answer="submission queue doorbell",
            chains="", tags="nvme", node_ids="",
            category="bdev/nvme/pcie", author="alice", no_merge=False))
        _run(cmd_save_memory, _ns(
            graph=self.graph_dir,
            question="How to configure tcp transport?",
            answer="config file",
            chains="", tags="", node_ids="",
            category="bdev/nvme/tcp", author="bob", no_merge=True))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.graph_dir, ignore_errors=True)

    def test_search_returns_json_results(self):
        _, out, _ = _run(cmd_search_memory, _ns(
            graph=self.graph_dir, query="nvme submit", top=5,
            category="", tags="", author="",
            include_experience=False))
        results = json.loads(out)
        self.assertEqual(len(results), 1)
        self.assertIn("doorbell", results[0]["answer"])

    def test_search_category_filter(self):
        _, out, _ = _run(cmd_search_memory, _ns(
            graph=self.graph_dir, query="transport", top=5,
            category="bdev/nvme/pcie", tags="", author="",
            include_experience=False))
        # "transport" only lives under bdev/nvme/tcp — filtered out
        self.assertIn("No similar memories found.", out)
        _, out2, _ = _run(cmd_search_memory, _ns(
            graph=self.graph_dir, query="transport", top=5,
            category="bdev", tags="", author="",
            include_experience=False))
        self.assertEqual(len(json.loads(out2)), 1)

    def test_search_author_filter(self):
        _, out, _ = _run(cmd_search_memory, _ns(
            graph=self.graph_dir, query="nvme", top=5,
            category="", tags="", author="bob",
            include_experience=False))
        # nvme entry is alice's; bob only wrote the transport one
        self.assertIn("No similar memories found.", out)

    def test_search_no_match_message(self):
        _, out, _ = _run(cmd_search_memory, _ns(
            graph=self.graph_dir, query="zzz unmatched", top=5,
            category="", tags="", author="",
            include_experience=False))
        self.assertIn("No similar memories found.", out)


class TestValidateMemory(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph_dir()
        _run(cmd_save_memory, _ns(
            graph=self.graph_dir, question="q1", answer="a",
            chains="", tags="", node_ids="a,b",
            category="", author="", no_merge=False))
        _run(cmd_save_memory, _ns(
            graph=self.graph_dir, question="q2", answer="a",
            chains="", tags="", node_ids="a,ghost",
            category="", author="", no_merge=True))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.graph_dir, ignore_errors=True)

    def test_validate_demotes_stale(self):
        _, out, _ = _run(cmd_validate_memory, _ns(graph=self.graph_dir))
        self.assertIn("1 trusted", out)
        rows = {r["question"]: r for r in _db_rows(self.graph_dir)}
        self.assertEqual(rows["q1"]["status"], "active")
        self.assertEqual(rows["q2"]["status"], "experience")
        self.assertIn("no longer in graph",
                      rows["q2"]["invalidated_reason"])

    def test_auto_validate_daemon_path(self):
        import networkx as nx
        G = nx.DiGraph()
        G.add_node("a")
        _auto_validate_memory(G, os.path.join(self.graph_dir, "memory"),
                              self.graph_dir)
        rows = {r["question"]: r for r in _db_rows(self.graph_dir)}
        self.assertEqual(rows["q2"]["status"], "experience")


class TestMemoryHealth(unittest.TestCase):
    def test_health_stats_structure(self):
        graph_dir = _make_graph_dir()
        _run(cmd_save_memory, _ns(
            graph=graph_dir, question="q", answer="a",
            chains="", tags="", node_ids="",
            category="cat", author="", no_merge=False))
        ret, out, _ = _run(cmd_memory_health, _ns(graph=graph_dir))
        stats = json.loads(out)
        self.assertEqual(stats["total_entries"], 1)
        self.assertEqual(stats["active_entries"], 1)
        self.assertEqual(stats["categories"], 1)
        self.assertEqual(stats["storage"], "sqlite")
        self.assertIn("scratch_sessions", stats)
        import shutil
        shutil.rmtree(graph_dir, ignore_errors=True)


class TestManageMemory(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph_dir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.graph_dir, ignore_errors=True)

    def _mgmt(self, action, **kw):
        defaults = dict(graph=self.graph_dir, question="", answer="",
                        tags="", node_ids="", id="0", field="", value="",
                        root_id="0", scratch_id="", boost="1.0", top="5",
                        min_weight="0.3", tier="lite", query="", output="",
                        input="", session_id="", chains="", params="",
                        react="", ttl="24", merge=True, category="",
                        author="", include_experience=False, parts="",
                        ids="", canonical="")
        defaults.update(kw)
        return _run(cmd_manage_memory, _ns(action=action, **defaults))

    def test_add_action(self):
        self._mgmt("add", question="q", answer="a", tags="x",
                   category="bdev")
        rows = _db_rows(self.graph_dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["tags"]), ["x"])

    def test_query_action(self):
        self._mgmt("add", question="nvme question", answer="a")
        _, out, _ = self._mgmt("query", query="nvme")
        results = json.loads(out)
        self.assertEqual(len(results), 1)

    def test_get_action(self):
        self._mgmt("add", question="q", answer="a")
        _, out, _ = self._mgmt("get", id="1")
        entry = json.loads(out)
        self.assertEqual(entry["question"], "q")

    def test_get_missing_exits_1(self):
        _, _, _, code = _capture_call(
            cmd_manage_memory, _ns(action="get", graph=self.graph_dir,
                                   id="99", question="", answer="",
                                   tags="", node_ids="", field="", value="",
                                   root_id="0", scratch_id="", boost="1.0",
                                   top="5", min_weight="0.3", tier="lite",
                                   query="", output="", input="",
                                   session_id="", chains="", params="",
                                   react="", ttl="24", merge=True,
                                   category="", author="",
                                   include_experience=False, parts="",
                                   ids="", canonical=""))
        self.assertEqual(code, 1)

    def test_categories_action(self):
        self._mgmt("add", question="q", answer="a", category="bdev/nvme")
        _, out, _ = self._mgmt("categories")
        self.assertIn("bdev", out)
        self.assertIn("nvme", out)
        self.assertIn("1 subtree", out)

    def test_split_merge_move_actions(self):
        self._mgmt("add", question="broad q", answer="a", tags="t")
        parts = json.dumps([{"question": "narrow q1", "answer": "a1"},
                            {"question": "narrow q2", "answer": "a2"}])
        self._mgmt("split", id="1", parts=parts)
        self.assertEqual(_db_rows(self.graph_dir)[0]["status"], "split")

        self._mgmt("add", question="dup q", answer="x")
        self._mgmt("add", question="dup q again", answer="y")
        self._mgmt("merge", ids="2,3", canonical="2")
        rows = _db_rows(self.graph_dir)
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(by_id[3]["status"], "merged")
        self.assertEqual(by_id[3]["merged_into"], 2)

        self._mgmt("move", id="2", category="moved/to/here")
        self.assertEqual(
            by_id_move_check(self.graph_dir, 2), "moved/to/here")

    def test_pack_action(self):
        self._mgmt("add", question="q", answer="a")
        _, out, _ = self._mgmt("pack", tier="lite")
        pack = json.loads(out)
        self.assertIn("top_questions", pack)

    def test_search_action(self):
        self._mgmt("add", question="nvme q", answer="a",
                   category="bdev/nvme", author="alice")
        _, out, _ = self._mgmt("search", query="nvme",
                               category="bdev", author="alice")
        results = json.loads(out)
        self.assertEqual(len(results), 1)


def by_id_move_check(graph_dir, mem_id):
    conn = sqlite3.connect(
        os.path.join(graph_dir, "memory", "memory.db"))
    try:
        cat_id = conn.execute(
            "SELECT category_id FROM memories WHERE id = ?",
            (mem_id,)).fetchone()[0]
        return conn.execute(
            "SELECT path FROM categories WHERE id = ?",
            (cat_id,)).fetchone()[0]
    finally:
        conn.close()


if __name__ == "__main__":
    unittest.main()
