"""Tests for the semantics / graph-history commands without coverage.

- classify-endpoints: applies LLM classifications from
  .code2database_endpoints.json (known -> out_end + external_desc,
  unclear -> unknown_end) and writes the graph back
- extract-semantics: exports nodes lacking semantic_desc + project doc
  files into the LLM template
- think-chain: enumerates API_entry -> endpoint chains into a JSON
  template with per-step call attributes
- build-diff (--before/--after): structural diff of two build dirs
  via _builder.graph.graph_diff (nodes/edges/communities)
- graph-diff (--from-path/--to-path, versioned): node/edge diff via
  graph_history, incl. --summary-only. This command was wired to the
  build-diff handler through a same-name shadow in the builder entry
  script (local def cmd_graph_diff overwrote the graph_history import,
  so 'graph-diff' read args.before and crashed) — the dispatch now
  points each command at its own handler and this file pins both.
- graph-record-version: records versions with counts and metadata
- find-commits: maps a function to its source file and returns the
  git history of that file (real temp git repo)
"""
import argparse
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.graph.semantics import (
    cmd_classify_endpoints,
    cmd_extract_semantics,
    cmd_think_chain,
)
from _builder.graph.graph_history import (
    cmd_graph_diff,
    cmd_graph_record_version,
)
from _builder.query.query_provenance import cmd_find_commits
import code2database_builder as builder


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


def _make_json_graph(graph_dir, nodes, edges, source_root=""):
    """master.json + domain JSON graph (legacy full-node format)."""
    os.makedirs(graph_dir, exist_ok=True)
    domain_file = "code2database_app.json"
    master = {
        "domains": {"app": domain_file},
        "source_root": source_root,
        "stats": {"total_functions": len(nodes)},
    }
    with open(os.path.join(graph_dir, "code2database_master.json"),
              "w", encoding="utf-8") as f:
        json.dump(master, f)
    with open(os.path.join(graph_dir, domain_file), "w",
              encoding="utf-8") as f:
        json.dump({"domain": "app", "nodes": nodes, "edges": edges}, f)


def _read_domain_nodes(graph_dir):
    """Read nodes from the CURRENT master layout (compact format:
    domains/<group>/code2database_domain_*.json with functions rows
    [id, name, source_file, line, labels_json, signature] plus
    function_details)."""
    master = json.loads(open(
        os.path.join(graph_dir, "code2database_master.json"),
        encoding="utf-8").read())
    result = {}
    for rel in master.get("domains", {}).values():
        path = os.path.join(graph_dir, rel)
        if not os.path.exists(path):
            continue
        data = json.loads(open(path, encoding="utf-8").read())
        details = data.get("function_details", {})
        for row in data.get("functions", []):
            nid = row[0]
            try:
                labels = json.loads(row[4]) if row[4] else []
            except (json.JSONDecodeError, TypeError):
                labels = []
            result[nid] = {"labels": labels,
                           "external_desc": details.get(nid, {}).get(
                               "external_desc", "")}
        for n in data.get("nodes", []):
            result[n["id"]] = {"labels": n.get("labels", []),
                               "external_desc": n.get("external_desc", "")}
    return result


def _node(nid, name, labels, source_file, line, **extra):
    return {"id": nid, "name": name, "labels": labels,
            "source_file": source_file, "line": line, "domain": "app",
            **extra}


def _default_nodes():
    return [
        _node("api_handler", "api_handler", ["API_entry"], "app/main.c", 10),
        _node("worker", "worker", [], "app/worker.c", 5),
        _node("ext_io", "ext_io", ["out_end"], "app/io.c", 1),
        _node("ext_net", "ext_net", [], "app/net.c", 2),
    ]


def _default_edges():
    return [
        {"source": "api_handler", "target": "worker", "relation": "CALLS",
         "call_order": 0, "call_condition": ""},
        {"source": "worker", "target": "ext_io", "relation": "CALLS",
         "call_order": 1, "call_condition": ""},
    ]


class _GraphFixture(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_semgraph_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.graph_dir = os.path.join(self.tmp, "graph")
        _make_json_graph(self.graph_dir, _default_nodes(),
                         _default_edges())


class TestClassifyEndpoints(_GraphFixture):

    def _write_endpoints(self, endpoints):
        path = os.path.join(self.graph_dir, ".code2database_endpoints.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"endpoints": endpoints}, f)

    def test_missing_endpoints_file_exits_1(self):
        ret, out, err, code = _capture_call(
            cmd_classify_endpoints, _ns(graph=self.graph_dir))
        self.assertEqual(code, 1)
        self.assertIn("Run 'build' first", err)

    def test_known_and_unclear_classification(self):
        self._write_endpoints([
            {"id": "ext_io", "classification": "known",
             "external_desc": "writes to stdout"},
            {"id": "ext_net", "classification": "known",
             "external_desc": ""},
        ])
        _, out, _ = _run(cmd_classify_endpoints, _ns(graph=self.graph_dir))
        self.assertIn("out_end (known): 1", out)
        self.assertIn("unknown_end (unclear): 1", out)
        nodes = _read_domain_nodes(self.graph_dir)
        io_labels = nodes["ext_io"]["labels"]
        net_labels = nodes["ext_net"]["labels"]
        self.assertIn("out_end", io_labels)
        self.assertNotIn("unknown_end", io_labels)
        self.assertIn("unknown_end", net_labels)
        self.assertNotIn("out_end", net_labels)
        self.assertEqual(nodes["ext_io"].get("external_desc"),
                         "writes to stdout")
        self.assertEqual(nodes["ext_net"].get("external_desc"), "")

    def test_unknown_node_ids_are_skipped(self):
        self._write_endpoints([
            {"id": "ghost_node", "classification": "known",
             "external_desc": "x"},
        ])
        _, out, _ = _run(cmd_classify_endpoints, _ns(graph=self.graph_dir))
        self.assertIn("Classified 0 endpoint(s)", out)


class TestExtractSemantics(_GraphFixture):

    def test_exports_nodes_lacking_semantic_desc(self):
        _, out, _ = _run(cmd_extract_semantics,
                         _ns(graph=self.graph_dir, docs=""))
        result_path = os.path.join(self.graph_dir,
                                   ".code2database_semantics.json")
        self.assertTrue(os.path.exists(result_path))
        data = json.loads(open(result_path, encoding="utf-8").read())
        ids = {n["id"] for n in data["nodes_to_describe"]}
        self.assertEqual(ids, {"api_handler", "worker", "ext_io", "ext_net"})

    def test_nodes_with_desc_are_excluded(self):
        nodes = _default_nodes()
        for n in nodes:
            if n["id"] == "worker":
                n["semantic_desc"] = "already described"
        _make_json_graph(self.graph_dir, nodes, _default_edges())
        _run(cmd_extract_semantics, _ns(graph=self.graph_dir, docs=""))
        data = json.loads(open(os.path.join(
            self.graph_dir, ".code2database_semantics.json"),
            encoding="utf-8").read())
        ids = {n["id"] for n in data["nodes_to_describe"]}
        self.assertNotIn("worker", ids)
        self.assertEqual(ids, {"api_handler", "ext_io", "ext_net"})

    def test_doc_files_collected(self):
        docs = os.path.join(self.tmp, "docs")
        os.makedirs(os.path.join(docs, "sub"))
        for rel in ("guide.md", "notes.txt", "code.py"):
            with open(os.path.join(docs, rel), "w", encoding="utf-8") as f:
                f.write("x\n")
        with open(os.path.join(docs, "sub", "more.rst"), "w",
                  encoding="utf-8") as f:
            f.write("x\n")
        _run(cmd_extract_semantics, _ns(graph=self.graph_dir, docs=docs))
        data = json.loads(open(os.path.join(
            self.graph_dir, ".code2database_semantics.json"),
            encoding="utf-8").read())
        self.assertEqual(sorted(data["doc_files"]),
                         ["guide.md", "notes.txt", "sub/more.rst"])


class TestThinkChain(_GraphFixture):

    def test_chain_from_api_to_endpoint(self):
        _, out, _ = _run(cmd_think_chain, _ns(
            graph=self.graph_dir, output=None, max_depth=10, max_chains=200))
        path = os.path.join(self.graph_dir,
                            ".code2database_think_chain.json")
        self.assertTrue(os.path.exists(path))
        self.assertIn("Call chains: 1", out)
        data = json.loads(open(path, encoding="utf-8").read())
        chains = data["chains"] if isinstance(data, dict) else data
        self.assertTrue(chains)
        chain = chains[0]
        self.assertEqual([s["id"] for s in chain["steps"]],
                         ["api_handler", "worker", "ext_io"])
        self.assertEqual(chain["steps"][1]["call_order"], 0)
        self.assertEqual(chain["conclusion"], "")

    def test_custom_output_path(self):
        out_path = os.path.join(self.tmp, "chain.json")
        _run(cmd_think_chain, _ns(graph=self.graph_dir, output=out_path,
                                  max_depth=10, max_chains=200))
        self.assertTrue(os.path.exists(out_path))


def _make_plain_db_graph(graph_dir, functions, edges):
    """SQLite functions/edges fixture (shared by diff commands)."""
    os.makedirs(graph_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(graph_dir, "code2database.db"))
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS functions (
            id TEXT PRIMARY KEY, name TEXT, domain TEXT,
            source_file TEXT, line_number INTEGER, signature TEXT,
            labels TEXT, body_text_compressed BLOB, extra_json TEXT);
        CREATE TABLE IF NOT EXISTS edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoker_id TEXT NOT NULL, invoked_id TEXT NOT NULL,
            relation TEXT, call_order INTEGER, call_condition TEXT);
    """)
    for fn in functions:
        conn.execute(
            "INSERT INTO functions (id, name, domain, source_file, "
            "line_number, signature) VALUES (?, ?, ?, ?, ?, ?)",
            (fn["id"], fn["name"], fn.get("domain", "d"),
             fn.get("source_file", "a.c"), fn.get("line", 1),
             fn.get("signature", "")))
    for e in edges:
        conn.execute(
            "INSERT INTO edges (invoker_id, invoked_id, relation) "
            "VALUES (?, ?, ?)", (e[0], e[1], "CALLS"))
    conn.commit()
    conn.close()


class TestBuildDiff(unittest.TestCase):
    """build-diff --before/--after via _builder.graph.graph_diff."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_bdiff_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.before = os.path.join(self.tmp, "before")
        self.after = os.path.join(self.tmp, "after")
        _make_plain_db_graph(self.before, [
            {"id": "alpha", "name": "alpha"},
            {"id": "beta", "name": "beta"},
        ], [("alpha", "beta")])
        _make_plain_db_graph(self.after, [
            {"id": "alpha", "name": "alpha"},
            {"id": "gamma", "name": "gamma"},
        ], [("alpha", "gamma")])

    def test_summary_counts(self):
        _, out, _ = _run(builder.cmd_build_diff, _ns(
            before=self.before, after=self.after, detail="summary"))
        result = json.loads(out)
        stats = result["stats"]
        self.assertEqual(stats["added_nodes"], 1)
        self.assertEqual(stats["removed_nodes"], 1)
        self.assertEqual(stats["added_edges"], 1)
        self.assertEqual(stats["removed_edges"], 1)

    def test_full_detail_lists_changes(self):
        _, out, _ = _run(builder.cmd_build_diff, _ns(
            before=self.before, after=self.after, detail="full"))
        result = json.loads(out)
        self.assertEqual([n["id"] for n in result["nodes"]["added"]],
                         ["gamma"])
        self.assertEqual([n["id"] for n in result["nodes"]["removed"]],
                         ["beta"])
        self.assertEqual([(e["source"], e["target"])
                          for e in result["edges"]["added"]],
                         [("alpha", "gamma")])
        self.assertEqual([(e["source"], e["target"])
                          for e in result["edges"]["removed"]],
                         [("alpha", "beta")])
        self.assertIn("communities", result)


class TestGraphDiffVersions(unittest.TestCase):
    """graph-diff --from-path/--to-path via graph_history."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_gdiff_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.v1 = os.path.join(self.tmp, "v1")
        self.v2 = os.path.join(self.tmp, "v2")
        _make_plain_db_graph(self.v1, [
            {"id": "alpha", "name": "alpha", "signature": "int alpha(void)"},
            {"id": "beta", "name": "beta"},
        ], [("alpha", "beta")])
        _make_plain_db_graph(self.v2, [
            {"id": "alpha", "name": "alpha",
             "signature": "int alpha(int)"},
            {"id": "gamma", "name": "gamma"},
        ], [("alpha", "gamma")])

    def test_diff_with_explicit_paths(self):
        _, out, _ = _run(cmd_graph_diff, _ns(
            graph=self.v1, from_version=None, to_version=None,
            from_path=self.v1, to_path=self.v2, summary_only=False))
        result = json.loads(out)
        self.assertEqual(result["summary"]["added_nodes"], 1)
        self.assertEqual(result["summary"]["removed_nodes"], 1)
        self.assertEqual(result["summary"]["changed_nodes"], 1,
                         "alpha's signature differs between versions")
        self.assertEqual(result["summary"]["added_edges"], 1)
        self.assertEqual(result["summary"]["removed_edges"], 1)

    def test_summary_only_prints_summary(self):
        _, out, _ = _run(cmd_graph_diff, _ns(
            graph=self.v1, from_version=None, to_version=None,
            from_path=self.v1, to_path=self.v2, summary_only=True))
        result = json.loads(out)
        self.assertEqual(set(result.keys()),
                         {"added_nodes", "removed_nodes", "changed_nodes",
                          "added_edges", "removed_edges"})


class TestGraphRecordVersion(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_recver_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.graph_dir = os.path.join(self.tmp, "graph")
        _make_plain_db_graph(self.graph_dir,
                             [{"id": "alpha", "name": "alpha"}], [])

    def test_records_sequential_versions_with_counts(self):
        _, out, _ = _run(cmd_graph_record_version, _ns(
            graph=self.graph_dir, description="first cut",
            commit_hash=None, commit_short=None, operator=None))
        self.assertIn("Recorded version v1", out)
        _, out, _ = _run(cmd_graph_record_version, _ns(
            graph=self.graph_dir, description="second cut",
            commit_hash="abc123", commit_short="abc1", operator="tester"))
        self.assertIn("Recorded version v2", out)
        db = os.path.join(self.graph_dir, "graph_versions.db")
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT version_id, description, commit_hash, operator, "
                "node_count FROM graph_versions ORDER BY version_id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], "first cut")
        self.assertEqual(rows[1][3], "tester")
        self.assertEqual(rows[1][4], 1, "node_count from the fixture db")


class TestFindCommits(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_findcmt_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        self.src_dir = os.path.join(self.repo, "app")
        os.makedirs(self.src_dir)
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "Tester")
        with open(os.path.join(self.src_dir, "worker.c"), "w",
                  encoding="utf-8") as f:
            f.write("int worker(void) { return 1; }\n")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "add worker")
        with open(os.path.join(self.src_dir, "worker.c"), "w",
                  encoding="utf-8") as f:
            f.write("int worker(void) { return 2; }\n")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "tune worker")
        # graph dir with manifest pointing at the repo
        self.graph_dir = os.path.join(self.tmp, "graph")
        os.makedirs(self.graph_dir)
        with open(os.path.join(self.graph_dir,
                               ".code2database_manifest.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"source_root": self.repo}, f)
        _make_json_graph(self.graph_dir, [
            _node("worker", "worker", [], "app/worker.c", 1),
        ], [])

    def _git(self, *argv):
        subprocess.run(["git", *argv], cwd=self.repo, check=True,
                       capture_output=True)

    def test_commits_for_function(self):
        _, out, _ = _run(cmd_find_commits, _ns(
            graph=self.graph_dir, function="worker", since="",
            limit=10, json=True))
        result = json.loads(out)
        self.assertEqual(result["function"], "worker")
        self.assertEqual(result["source_file"], "app/worker.c")
        self.assertEqual(result["_source"], "git")
        subjects = [c["subject"] for c in result["commits"]]
        self.assertEqual(subjects, ["tune worker", "add worker"],
                         "commits must be newest first")

    def test_missing_manifest_exits_1(self):
        empty = os.path.join(self.tmp, "empty_graph")
        os.makedirs(empty)
        ret, out, err, code = _capture_call(
            cmd_find_commits, _ns(graph=empty, function="worker",
                                  since="", limit=10, json=True))
        self.assertEqual(code, 1)
        self.assertIn("No manifest found", err)

    def test_unknown_function_exits_1(self):
        ret, out, err, code = _capture_call(
            cmd_find_commits, _ns(graph=self.graph_dir, function="nope",
                                  since="", limit=10, json=True))
        self.assertEqual(code, 1)
        self.assertIn("not found in graph", err)


if __name__ == "__main__":
    unittest.main()
