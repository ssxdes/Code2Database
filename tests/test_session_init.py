"""Tests for session-init — the one-shot session context loader."""

import argparse
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.session_init import (
    build_session_context, render_session_context, cmd_session_init,
)
from _builder.brief import brief_extract, brief_update, save_brief
from _builder.memory_store import MemoryStore


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


def _make_graph_dir(n_nodes=3, n_edges=2):
    tmp = tempfile.mkdtemp(prefix="c2d_sess_")
    nodes = [{"id": f"n{i}", "name": f"n{i}", "source_file": "/tmp/x.c",
              "line": i + 1, "domain": "test", "labels": [],
              "is_empty": False} for i in range(n_nodes)]
    edges = [{"source": f"n{i}", "target": f"n{i+1}",
              "relation": "INVOKES", "confidence": "EXTRACTED"}
             for i in range(n_edges)]
    with open(os.path.join(tmp, "domain_test.json"), "w") as f:
        json.dump({"nodes": nodes, "edges": edges}, f)
    with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
        json.dump({"source_root": "/tmp",
                   "domains": {"test": "domain_test.json"}}, f)
    return tmp


class TestSessionContextEmpty(unittest.TestCase):
    """A bare graph dir: every layer degrades to a hint, nothing raises."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.graph_dir = os.path.join(self.tmp.name, "graph")
        os.makedirs(self.graph_dir, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_context_shape(self):
        ctx = build_session_context(self.graph_dir)
        self.assertIsNone(ctx["brief"])
        self.assertIn("brief-extract", ctx["brief_rendered"])
        self.assertEqual(ctx["memory"]["digest"], [])
        self.assertEqual(ctx["graph"]["nodes"], 0)
        self.assertEqual(ctx["known_unknowns"], [])
        # hints point at the two bootstrap actions
        self.assertTrue(any("brief-extract" in h for h in ctx["hints"]))
        self.assertTrue(any("save the first Q&A" in h or
                            "save-memory" in h for h in ctx["hints"]))

    def test_render_contains_sections(self):
        ctx = build_session_context(self.graph_dir)
        rendered = render_session_context(ctx)
        for section in ("Project Brief", "Memory Digest", "Graph",
                        "Known Unknowns"):
            self.assertIn(section, rendered)

    def test_cli_text_and_json(self):
        graph_dir = os.path.join(self.tmp.name, "g2")
        os.makedirs(graph_dir, exist_ok=True)
        _, out, _ = _run(cmd_session_init, _ns(graph=graph_dir, top=5,
                                               json=False))
        self.assertIn("=== Code2Database Session Context ===", out)
        _, jout, _ = _run(cmd_session_init, _ns(graph=graph_dir, top=5,
                                                json=True))
        data = json.loads(jout)
        self.assertIn("brief", data)
        self.assertIn("memory", data)
        self.assertIn("graph", data)
        self.assertIn("hints", data)


class TestSessionContextFull(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph_dir(n_nodes=5, n_edges=4)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.graph_dir, ignore_errors=True)

    def _populate(self):
        brief = brief_extract(self.graph_dir)
        brief_update(self.graph_dir, set_field="project", set_value="P")
        brief_update(self.graph_dir, set_field="description",
                     set_value="A test project.")
        brief_update(self.graph_dir, add_section="hard_rules",
                     add_value='{"rule": "开启宏 X", "type": "macro"}')
        brief_update(self.graph_dir, add_section="query_paths",
                     add_value="查 bdev: search --domain-filter bdev")
        store = MemoryStore(self.graph_dir)
        store.add("How does nvme submit IO?", "doorbell",
                  category="bdev/nvme/pcie", author="alice", no_merge=True)
        store.add("What is a reactor?", "event loop",
                  category="event", author="bob", no_merge=True)
        return store

    def test_full_context(self):
        self._populate()
        ctx = build_session_context(self.graph_dir)
        self.assertIsNotNone(ctx["brief"])
        self.assertIn("Project Brief: P", ctx["brief_rendered"])
        self.assertEqual(ctx["graph"]["nodes"], 5)
        # memory digest: top by weight, with category
        digest = ctx["memory"]["digest"]
        self.assertEqual(len(digest), 2)
        questions = {d["question"] for d in digest}
        self.assertIn("How does nvme submit IO?", questions)
        cats = {d["category"] for d in digest}
        self.assertIn("bdev/nvme/pcie", cats)
        # stats attached
        self.assertEqual(ctx["memory"]["stats"]["active_entries"], 2)
        # hints carry the brief's query_paths
        self.assertTrue(any("search --domain-filter bdev" in h
                            for h in ctx["hints"]))

    def test_render_shows_digest_entries(self):
        self._populate()
        ctx = build_session_context(self.graph_dir)
        rendered = render_session_context(ctx)
        self.assertIn("[bdev/nvme/pcie]", rendered)
        self.assertIn("How does nvme submit IO?", rendered)
        self.assertIn("→ doorbell", rendered)
        self.assertIn("5 nodes | 4 edges", rendered)

    def test_drift_detection(self):
        self._populate()
        # Simulate drift: rewrite the graph much smaller
        nodes = [{"id": "n0", "name": "n0", "source_file": "/tmp/x.c",
                  "line": 1, "domain": "test", "labels": [],
                  "is_empty": False}]
        with open(os.path.join(self.graph_dir, "domain_test.json"), "w") as f:
            json.dump({"nodes": nodes, "edges": []}, f)
        ctx = build_session_context(self.graph_dir)
        self.assertIsNotNone(ctx["brief_drift"])
        self.assertIn("drifted", ctx["brief_drift"])
        self.assertTrue(any("refresh-stats" in h for h in ctx["hints"]))

    def test_known_unknowns_surface(self):
        self._populate()
        # Log some unmatched queries into kb_query_log
        from _builder.kb_index import _kb_connect
        conn = _kb_connect(self.graph_dir)
        try:
            for _ in range(3):
                conn.execute(
                    "INSERT INTO kb_query_log (query, matched, match_count, "
                    "top_score, queried_at) VALUES (?, 0, 0, 0.0, ?)",
                    ("how does rpc work", "2026-09-01T00:00:00"))
            conn.commit()
        finally:
            conn.close()
        ctx = build_session_context(self.graph_dir)
        self.assertEqual(len(ctx["known_unknowns"]), 1)
        self.assertEqual(ctx["known_unknowns"][0]["occurrences"], 3)
        rendered = render_session_context(ctx)
        self.assertIn("how does rpc work", rendered)
        self.assertIn("asked 3×", rendered)


class TestSessionFreshness(unittest.TestCase):
    """Layer 3b: source-vs-graph staleness must surface at session start."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # source tree with one file; graph dir beside it (dirname is
        # the source root, as check_freshness expects)
        src = os.path.join(self.tmp.name, "src")
        os.makedirs(src, exist_ok=True)
        self.src_file = os.path.join(src, "main.c")
        with open(self.src_file, "w") as f:
            f.write("int main(void) { return 0; }\n")
        self.graph_dir = os.path.join(self.tmp.name, "src", "graph")
        os.makedirs(self.graph_dir, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_manifest(self, fingerprint):
        st = os.stat(self.src_file)
        fp = fingerprint or f"{st.st_mtime_ns}:{st.st_size}"
        manifest = {"files": {"main.c": fp}}
        with open(os.path.join(self.graph_dir,
                               ".code2database_manifest.json"), "w") as f:
            json.dump(manifest, f)

    def test_fresh_graph_reported_clean(self):
        self._write_manifest(None)
        ctx = build_session_context(self.graph_dir)
        fr = ctx["freshness"]
        self.assertIsNotNone(fr)
        self.assertTrue(fr["is_fresh"])
        self.assertEqual(fr["changed_files"], 0)
        self.assertNotIn("STALE", render_session_context(ctx))
        self.assertFalse(any("STALE" in h for h in ctx["hints"]))

    def test_stale_graph_warned(self):
        self._write_manifest("1:1")  # mismatched fingerprint
        ctx = build_session_context(self.graph_dir)
        fr = ctx["freshness"]
        self.assertIsNotNone(fr)
        self.assertFalse(fr["is_fresh"])
        self.assertEqual(fr["changed_files"], 1)
        self.assertEqual(fr["samples"], ["main.c"])
        rendered = render_session_context(ctx)
        self.assertIn("graph STALE", rendered)
        self.assertIn("main.c", rendered)
        self.assertTrue(any("STALE" in h for h in ctx["hints"]))

    def test_new_file_counts_as_stale(self):
        self._write_manifest(None)
        with open(os.path.join(self.tmp.name, "src", "extra.c"), "w") as f:
            f.write("int extra(void) { return 1; }\n")
        ctx = build_session_context(self.graph_dir)
        self.assertFalse(ctx["freshness"]["is_fresh"])
        self.assertEqual(ctx["freshness"]["new_files"], 1)

    def test_no_manifest_degrades_to_not_fresh(self):
        ctx = build_session_context(self.graph_dir)
        self.assertIsNotNone(ctx["freshness"])
        self.assertFalse(ctx["freshness"]["is_fresh"])
        # recommendation guides the agent to scan
        self.assertTrue(any("STALE" in h for h in ctx["hints"]))

    def test_mcp_tool_passes_freshness_through(self):
        self._write_manifest("1:1")
        from _builder.mcp_server import _tool_session_init
        out = _tool_session_init({}, self.graph_dir)
        self.assertIn("freshness", out)
        self.assertFalse(out["freshness"]["is_fresh"])
        self.assertIn("graph STALE", out["rendered"])


class TestContextPackSummaries(unittest.TestCase):
    """context_pack must embed brief + fresh memory summaries."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.graph_dir = os.path.join(self.tmp.name, "graph")
        os.makedirs(self.graph_dir, exist_ok=True)
        nodes = [{"id": "a", "name": "a", "source_file": "/tmp/x.c",
                  "line": 1, "domain": "test", "labels": [],
                  "is_empty": False}]
        with open(os.path.join(self.graph_dir, "domain_test.json"), "w") as f:
            json.dump({"nodes": nodes, "edges": []}, f)
        with open(os.path.join(self.graph_dir,
                               "code2database_master.json"), "w") as f:
            json.dump({"source_root": "/tmp",
                       "domains": {"test": "domain_test.json"}}, f)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pack_carries_brief_and_memory(self):
        import networkx as nx
        from _builder.index_pack import _build_context_pack
        # Populate brief + memory
        brief_extract(self.graph_dir)
        brief_update(self.graph_dir, set_field="project", set_value="PX")
        brief_update(self.graph_dir, add_section="hard_rules",
                     add_value='{"rule": "开启宏 Y", "type": "macro"}')
        store = MemoryStore(self.graph_dir)
        store.add("mem question one", "mem answer",
                  category="cat", no_merge=True)
        G = nx.DiGraph()
        G.add_node("a", name="a", domain="test", labels=[],
                   is_empty=False)
        _build_context_pack(G, self.graph_dir, "/tmp")
        pack_path = os.path.join(self.graph_dir,
                                 ".code2database_context_pack.json")
        pack = json.loads(open(pack_path, encoding="utf-8").read())
        # knowledge_summary from the brief (not the removed MD packs)
        self.assertIn("knowledge_summary", pack)
        self.assertEqual(pack["knowledge_summary"]["project"], "PX")
        self.assertIn("开启宏 Y", pack["knowledge_summary"]["hard_rules"])
        # memory_summary fresh from memory.db
        self.assertIn("memory_summary", pack)
        self.assertIn("mem question one",
                      pack["memory_summary"]["top_questions"][0])

    def test_pack_without_brief_omits_summary(self):
        import networkx as nx
        from _builder.index_pack import _build_context_pack
        G = nx.DiGraph()
        G.add_node("a", name="a", domain="test", labels=[],
                   is_empty=False)
        _build_context_pack(G, self.graph_dir, "/tmp")
        pack_path = os.path.join(self.graph_dir,
                                 ".code2database_context_pack.json")
        pack = json.loads(open(pack_path, encoding="utf-8").read())
        self.assertNotIn("knowledge_summary", pack)


if __name__ == "__main__":
    unittest.main()
