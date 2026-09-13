"""Tests for the changelog and doc-alignment commands without coverage.

- export-changes: git-driven change graph export (uncommitted edits and
  explicit commit ranges), deleted files, non-VCS sources
- merge-changes: added/removed/modified functions + added edges merged
  into the graph and persisted (incl. the stale skeleton contract)
- doc-mark-stale: JSON backend persistence (doc_stale trio must survive
  the domain rewrite), SQLite backend persistence (extra_json), unknown
  node rejection
- doc-alignment-report: Markdown report from a graph with doc fields
- doc-signature-diff: signature changes between two graph versions

The stale/doc_stale persistence is pinned here because the JSON backend
silently dropped these write-side flags through the domain rewrite:
doc-mark-stale printed ok:true while persisting nothing, and
merge-changes' stale=True skeleton nodes lost their flag.
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

from _builder.build.changelog_update import (
    cmd_export_changes,
    cmd_merge_changes,
)
from _builder.misc.doc_code_align import (
    cmd_doc_alignment_report,
    cmd_doc_mark_stale,
    cmd_doc_signature_diff,
)
from _builder.graph.graph_loader import _load_full_graph


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


def _make_json_graph(graph_dir, nodes, edges=None, source_root=""):
    os.makedirs(graph_dir, exist_ok=True)
    master = {
        "domains": {"app": "code2database_app.json"},
        "source_root": source_root,
        "stats": {"total_functions": len(nodes)},
    }
    with open(os.path.join(graph_dir, "code2database_master.json"),
              "w", encoding="utf-8") as f:
        json.dump(master, f)
    with open(os.path.join(graph_dir, "code2database_app.json"), "w",
              encoding="utf-8") as f:
        json.dump({"domain": "app", "nodes": nodes,
                   "edges": edges or []}, f)


def _node(nid, name, source_file, line, **extra):
    return {"id": nid, "name": name, "labels": extra.pop("labels", []),
            "source_file": source_file, "line": line, "domain": "app",
            **extra}


def _current_domain_data(graph_dir):
    """Read the CURRENT master layout (after any split_by_domain
    rewrite) and return (function_rows, function_details, edge_rows)."""
    master = json.loads(open(
        os.path.join(graph_dir, "code2database_master.json"),
        encoding="utf-8").read())
    rows, details, edges = [], {}, []
    for rel in master.get("domains", {}).values():
        path = os.path.join(graph_dir, rel)
        if not os.path.exists(path):
            continue
        data = json.loads(open(path, encoding="utf-8").read())
        rows.extend(data.get("functions", []))
        details.update(data.get("function_details", {}))
        edges.extend(data.get("edges", []))
    return rows, details, edges


class TestExportChanges(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_exportchg_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "Tester")
        with open(os.path.join(self.repo, "a.c"), "w",
                  encoding="utf-8") as f:
            f.write("int alpha(void) { return 1; }\n")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "initial")
        self.graph_dir = os.path.join(self.tmp, "graph")
        _make_json_graph(self.graph_dir, [
            _node("app_alpha", "alpha", "a.c", 1,
                  signature="int alpha(void)"),
        ])

    def _git(self, *argv):
        subprocess.run(["git", *argv], cwd=self.repo, check=True,
                       capture_output=True)

    def test_uncommitted_modification_exported(self):
        with open(os.path.join(self.repo, "a.c"), "w",
                  encoding="utf-8") as f:
            f.write("int alpha(void) { return 2; }\n"
                    "int beta(void) { return 3; }\n")
        _, out, _ = _run(cmd_export_changes, _ns(
            source=self.repo, graph=self.graph_dir,
            commit_range=None, output=""))
        self.assertIn("Change graph exported", out)
        changes = json.loads(open(os.path.join(
            self.graph_dir, ".code2database_changes.json"),
            encoding="utf-8").read())
        self.assertEqual(changes["vcs"], "git")
        self.assertEqual(changes["changed_files"], ["a.c"])
        modified_names = {f["name"]
                          for f in changes["modified_functions"]}
        added_names = {f["name"] for f in changes["added_functions"]}
        self.assertEqual(modified_names, {"alpha"})
        self.assertEqual(added_names, {"beta"})

    def test_commit_range_exported(self):
        with open(os.path.join(self.repo, "a.c"), "w",
                  encoding="utf-8") as f:
            f.write("int alpha(void) { return 2; }\n")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "tune alpha")
        _, out, _ = _run(cmd_export_changes, _ns(
            source=self.repo, graph=self.graph_dir,
            commit_range="HEAD~1..HEAD", output=""))
        changes = json.loads(open(os.path.join(
            self.graph_dir, ".code2database_changes.json"),
            encoding="utf-8").read())
        self.assertEqual(changes["changed_files"], ["a.c"])
        self.assertEqual(
            {f["name"] for f in changes["modified_functions"]}, {"alpha"})

    def test_deleted_file_marks_removal(self):
        os.remove(os.path.join(self.repo, "a.c"))
        _, out, _ = _run(cmd_export_changes, _ns(
            source=self.repo, graph=self.graph_dir,
            commit_range=None, output=""))
        changes = json.loads(open(os.path.join(
            self.graph_dir, ".code2database_changes.json"),
            encoding="utf-8").read())
        self.assertEqual(
            {f["name"] for f in changes["removed_functions"]}, {"alpha"})

    def test_non_vcs_source_is_empty_export(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        _, out, _ = _run(cmd_export_changes, _ns(
            source=plain, graph=self.graph_dir,
            commit_range=None, output=""))
        changes = json.loads(open(os.path.join(
            self.graph_dir, ".code2database_changes.json"),
            encoding="utf-8").read())
        self.assertEqual(changes["vcs"], "")
        self.assertEqual(changes["changed_files"], [])
        self.assertEqual(changes["added_functions"], [])


class TestMergeChanges(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_mergechg_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.graph_dir = os.path.join(self.tmp, "graph")
        _make_json_graph(self.graph_dir, [
            _node("app_alpha", "alpha", "a.c", 1,
                  signature="int alpha(void)"),
            _node("app_gone", "gone", "a.c", 10),
        ], [{"source": "app_alpha", "target": "app_gone",
             "relation": "CALLS"}])

    def _write_changes(self, payload):
        path = os.path.join(self.tmp, "changes.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return path

    def test_merge_adds_removes_modifies_and_edges(self):
        changes_path = self._write_changes({
            "added_functions": [
                {"id": "app_new", "name": "new_fn", "source_file": "a.c",
                 "line": 20, "signature": "int new_fn(void)",
                 "domain": "app"},
            ],
            "removed_functions": [{"id": "app_gone"}],
            "modified_functions": [
                {"id": "app_alpha", "name": "alpha", "source_file": "a.c",
                 "line": 2},
            ],
            "added_edges": [{"source": "app_alpha", "target": "new_fn"}],
        })
        _, out, _ = _run(cmd_merge_changes, _ns(
            graph=self.graph_dir, changes=changes_path, source=""))
        self.assertIn("Merged: 1 added, 1 removed, 1 modified, 1 edges added",
                      out)
        rows, details, edges = _current_domain_data(self.graph_dir)
        names = {row[1] for row in rows}
        self.assertEqual(names, {"alpha", "new_fn"})
        alpha_row = next(r for r in rows if r[1] == "alpha")
        self.assertEqual(alpha_row[3], 2, "line number updated")
        # stale skeleton contract: new + modified nodes persist stale
        self.assertTrue(details.get("app_new", {}).get("stale"))
        self.assertTrue(details.get("app_alpha", {}).get("stale"))
        edge_pairs = {(e[0], e[1]) for e in edges}
        self.assertIn(("app_alpha", "app_new"), edge_pairs)
        self.assertNotIn(("app_alpha", "app_gone"), edge_pairs,
                         "edges of removed nodes must disappear")

    def test_merge_stale_survives_reload(self):
        changes_path = self._write_changes({
            "added_functions": [
                {"id": "app_new", "name": "new_fn", "source_file": "a.c",
                 "line": 20, "domain": "app"},
            ],
        })
        _run(cmd_merge_changes, _ns(graph=self.graph_dir,
                                    changes=changes_path, source=""))
        G = _load_full_graph(self.graph_dir)
        self.assertTrue(G.nodes["app_new"].get("stale", False),
                        "skeleton stale flag must survive a reload")

    def test_merge_noop_changes_leaves_graph(self):
        changes_path = self._write_changes({
            "added_functions": [],
            "removed_functions": [],
            "modified_functions": [],
            "added_edges": [],
        })
        _, out, _ = _run(cmd_merge_changes, _ns(
            graph=self.graph_dir, changes=changes_path, source=""))
        self.assertIn("0 added, 0 removed", out)
        G = _load_full_graph(self.graph_dir)
        self.assertIn("app_alpha", G)
        self.assertIn("app_gone", G)


class TestDocMarkStale(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_docstale_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.graph_dir = os.path.join(self.tmp, "graph")

    def test_json_backend_persists_marker(self):
        _make_json_graph(self.graph_dir, [
            _node("app_alpha", "alpha", "a.c", 1),
        ])
        _, out, _ = _run(cmd_doc_mark_stale, _ns(
            graph=self.graph_dir, node="app_alpha", reason="desc outdated"))
        self.assertIn('"ok": true', out)
        _, details, _ = _current_domain_data(self.graph_dir)
        self.assertTrue(details["app_alpha"].get("doc_stale"))
        self.assertEqual(details["app_alpha"].get("doc_stale_reason"),
                         "desc outdated")
        self.assertTrue(details["app_alpha"].get("doc_stale_at"))
        # and it must survive a reload
        G = _load_full_graph(self.graph_dir)
        self.assertTrue(G.nodes["app_alpha"].get("doc_stale"))
        self.assertEqual(G.nodes["app_alpha"].get("doc_stale_reason"),
                         "desc outdated")

    def test_sqlite_backend_persists_marker(self):
        os.makedirs(self.graph_dir, exist_ok=True)
        db = os.path.join(self.graph_dir, "code2database.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE functions (id TEXT PRIMARY KEY, name TEXT, "
            "domain TEXT, source_file TEXT, line_number INTEGER, "
            "signature TEXT, labels TEXT, body_text_compressed BLOB, "
            "extra_json TEXT)")
        conn.execute(
            "INSERT INTO functions (id, name) VALUES ('app_alpha', 'alpha')")
        conn.commit()
        conn.close()
        _, out, _ = _run(cmd_doc_mark_stale, _ns(
            graph=self.graph_dir, node="app_alpha", reason="sig drifted"))
        self.assertIn('"ok": true', out)
        conn = sqlite3.connect(db)
        try:
            extra = json.loads(conn.execute(
                "SELECT extra_json FROM functions WHERE id='app_alpha'"
            ).fetchone()[0])
        finally:
            conn.close()
        self.assertTrue(extra["doc_stale"])
        self.assertEqual(extra["doc_stale_reason"], "sig drifted")

    def test_unknown_node_exits_1(self):
        _make_json_graph(self.graph_dir, [
            _node("app_alpha", "alpha", "a.c", 1),
        ])
        ret, out, err, code = _capture_call(
            cmd_doc_mark_stale, _ns(graph=self.graph_dir,
                                    node="nope", reason="x"))
        self.assertEqual(code, 1)
        self.assertIn("node not found", err)


class TestDocAlignmentReport(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_docalign_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.graph_dir = os.path.join(self.tmp, "graph")
        self.src_dir = os.path.join(self.tmp, "src")
        os.makedirs(self.src_dir)
        with open(os.path.join(self.src_dir, "a.c"), "w",
                  encoding="utf-8") as f:
            f.write("int alpha(void) { return 1; }\n")
        _make_json_graph(self.graph_dir, [
            _node("app_alpha", "alpha", "a.c", 1,
                  signature="int alpha(void)",
                  semantic_desc="adds one and returns"),
        ], source_root=self.src_dir)

    def test_report_renders_counts(self):
        _, out, _ = _run(cmd_doc_alignment_report, _ns(
            graph=self.graph_dir, source=self.src_dir, output=""))
        self.assertIn("# Doc-Code Alignment Report", out)
        self.assertIn("Checked nodes: **1**", out)
        self.assertIn("**No mismatches detected.**", out)

    def test_report_written_to_file(self):
        out_path = os.path.join(self.tmp, "report.md")
        _, out, err = _run(cmd_doc_alignment_report, _ns(
            graph=self.graph_dir, source=self.src_dir, output=out_path))
        self.assertTrue(os.path.exists(out_path))
        content = open(out_path, encoding="utf-8").read()
        self.assertIn("# Doc-Code Alignment Report", content)


class TestDocSignatureDiff(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_docsigdiff_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.old_dir = os.path.join(self.tmp, "old")
        self.new_dir = os.path.join(self.tmp, "new")

    def test_detects_signature_change(self):
        _make_json_graph(self.old_dir, [
            _node("app_alpha", "alpha", "a.c", 1,
                  signature="int alpha(void)"),
        ])
        _make_json_graph(self.new_dir, [
            _node("app_alpha", "alpha", "a.c", 1,
                  signature="int alpha(int x)"),
            _node("app_beta", "beta", "a.c", 9,
                  signature="int beta(void)"),
        ])
        _, out, _ = _run(cmd_doc_signature_diff, _ns(
            old_graph=self.old_dir, new_graph=self.new_dir))
        result = json.loads(out)
        self.assertEqual(result["change_count"], 1)
        change = result["changes"][0]
        self.assertEqual(change["node_id"], "app_alpha")
        self.assertEqual(change["old_signature"], "int alpha(void)")
        self.assertEqual(change["new_signature"], "int alpha(int x)")

    def test_identical_graphs_report_zero(self):
        _make_json_graph(self.old_dir, [
            _node("app_alpha", "alpha", "a.c", 1,
                  signature="int alpha(void)"),
        ])
        _make_json_graph(self.new_dir, [
            _node("app_alpha", "alpha", "a.c", 1,
                  signature="int alpha(void)"),
        ])
        _, out, _ = _run(cmd_doc_signature_diff, _ns(
            old_graph=self.old_dir, new_graph=self.new_dir))
        self.assertEqual(json.loads(out)["change_count"], 0)


if __name__ == "__main__":
    unittest.main()
