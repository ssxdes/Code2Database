"""Tests for the ops wrapper commands without coverage.

- light-scan: changed-file AST scan merging skeleton nodes (stale=True)
  into the graph, existing nodes marked stale, git auto-detection
- patch-from-git: git diff plumbing (non-git source, no changes,
  uncommitted deletions patching the graph), transactional and
  --no-transaction paths
- plugins: discovery of .code2database_plugins/*.py with docstring
  extraction, explicit --plugin paths, empty listing
- install-hook: post-commit hook installation into a git repo,
  idempotent re-install, append to a foreign existing hook, non-git
  rejection
"""
import argparse
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.ops.patcher import cmd_light_scan, cmd_patch_from_git
from _builder.ops.plugins import cmd_plugins
import code2database_builder as builder
from _builder.graph.graph_loader import _load_full_graph

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def _current_details(graph_dir):
    master = json.loads(open(
        os.path.join(graph_dir, "code2database_master.json"),
        encoding="utf-8").read())
    rows, details = [], {}
    for rel in master.get("domains", {}).values():
        path = os.path.join(graph_dir, rel)
        if not os.path.exists(path):
            continue
        data = json.loads(open(path, encoding="utf-8").read())
        rows.extend(data.get("functions", []))
        details.update(data.get("function_details", {}))
    return rows, details


class _GitFixture(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_ops_")
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

    def _git(self, *argv):
        return subprocess.run(["git", *argv], cwd=self.repo, check=True,
                              capture_output=True)


class TestLightScan(_GitFixture):

    def setUp(self):
        super().setUp()
        _make_json_graph(self.graph_dir, [
            _node("app_alpha", "alpha", "a.c", 1,
                  signature="int alpha(void)"),
        ], source_root=self.repo)

    def test_new_function_added_as_stale_skeleton(self):
        with open(os.path.join(self.repo, "a.c"), "w",
                  encoding="utf-8") as f:
            f.write("int alpha(void) { return 1; }\n"
                    "int beta(void) { return 2; }\n")
        _, out, _ = _run(cmd_light_scan, _ns(
            source=self.repo, graph=self.graph_dir,
            files=os.path.join(self.repo, "a.c")))
        G = _load_full_graph(self.graph_dir)
        beta_ids = [n for n, d in G.nodes(data=True)
                    if d.get("name") == "beta"]
        self.assertTrue(beta_ids, "beta skeleton must be added")
        self.assertTrue(G.nodes[beta_ids[0]].get("stale", False),
                        "skeleton nodes must carry the stale flag")
        self.assertTrue(G.nodes["app_alpha"].get("stale", False),
                        "existing alpha must be marked stale after edit")
        # persisted through the domain rewrite
        _, details = _current_details(self.graph_dir)
        self.assertTrue(details.get(beta_ids[0], {}).get("stale"))

    def test_git_autodetects_changed_files(self):
        with open(os.path.join(self.repo, "a.c"), "w",
                  encoding="utf-8") as f:
            f.write("int alpha(void) { return 3; }\n")
        _, out, _ = _run(cmd_light_scan, _ns(
            source=self.repo, graph=self.graph_dir, files=""))
        G = _load_full_graph(self.graph_dir)
        self.assertTrue(G.nodes["app_alpha"].get("stale", False))

    def test_no_changes_reports_and_skips(self):
        _, out, _ = _run(cmd_light_scan, _ns(
            source=self.repo, graph=self.graph_dir, files=""))
        self.assertIn("No changed files to scan", out)


class TestPatchFromGit(_GitFixture):

    def setUp(self):
        super().setUp()
        _make_json_graph(self.graph_dir, [
            _node("app_alpha", "alpha", "a.c", 1),
            _node("app_victim", "victim", "b.c", 1),
        ], source_root=self.repo)
        with open(os.path.join(self.repo, "b.c"), "w",
                  encoding="utf-8") as f:
            f.write("int victim(void) { return 9; }\n")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "add b")

    def test_non_git_source_reports_error(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        _, out, err = _run(cmd_patch_from_git, _ns(
            graph=self.graph_dir, source=plain, commit_range=None,
            no_transaction=True))
        self.assertIn("not a git repository", err)

    def test_no_changes_detected(self):
        _, out, _ = _run(cmd_patch_from_git, _ns(
            graph=self.graph_dir, source=self.repo, commit_range=None,
            no_transaction=True))
        self.assertIn("No changes detected", out)

    def test_uncommitted_deletion_removes_nodes(self):
        os.remove(os.path.join(self.repo, "b.c"))
        _, out, err = _run(cmd_patch_from_git, _ns(
            graph=self.graph_dir, source=self.repo, commit_range=None,
            no_transaction=True))
        G = _load_full_graph(self.graph_dir)
        self.assertNotIn("app_victim", G)
        self.assertIn("app_alpha", G)

    def test_transactional_run_commits(self):
        os.remove(os.path.join(self.repo, "b.c"))
        _, out, err = _run(cmd_patch_from_git, _ns(
            graph=self.graph_dir, source=self.repo, commit_range=None,
            no_transaction=False))
        self.assertIn("patch-from-git committed", err)
        G = _load_full_graph(self.graph_dir)
        self.assertNotIn("app_victim", G)


class TestPlugins(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_plugins_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.source = os.path.join(self.tmp, "src")
        os.makedirs(self.source)

    def test_no_plugins_reports_hint(self):
        _, out, _ = _run(cmd_plugins, _ns(source=self.source, plugin=[]))
        self.assertIn("No plugins found", out)

    def test_discovers_plugins_with_description(self):
        plug_dir = os.path.join(self.source, ".code2database_plugins")
        os.makedirs(plug_dir)
        with open(os.path.join(plug_dir, "my_hook.py"), "w",
                  encoding="utf-8") as f:
            f.write('"""Count calls per domain."""\n'
                    'def register(pg):\n    pass\n')
        with open(os.path.join(plug_dir, "_hidden.py"), "w",
                  encoding="utf-8") as f:
            f.write("def register(pg):\n    pass\n")
        _, out, _ = _run(cmd_plugins, _ns(source=self.source, plugin=[]))
        found = json.loads(out)
        self.assertEqual(len(found), 1, "underscore files must be skipped")
        self.assertEqual(found[0]["file"], "my_hook.py")
        self.assertIn("Count calls per domain",
                      found[0]["description"])

    def test_explicit_plugin_path(self):
        extra = os.path.join(self.tmp, "extra_plugin.py")
        with open(extra, "w", encoding="utf-8") as f:
            f.write("# does nothing\n")
        _, out, _ = _run(cmd_plugins, _ns(source=self.source,
                                          plugin=[extra]))
        found = json.loads(out)
        self.assertEqual(found[0]["file"], "extra_plugin.py")
        self.assertEqual(found[0]["description"], "explicitly specified")


class TestInstallHook(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_hook_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True,
                       capture_output=True)
        self.hook_path = os.path.join(self.repo, ".git", "hooks",
                                      "post-commit")

    def test_installs_executable_hook(self):
        _, out, _ = _run(builder.cmd_install_hook, _ns(
            source=self.repo, graph_dir=".callgraph"))
        self.assertIn("Installed post-commit hook", out)
        self.assertTrue(os.path.exists(self.hook_path))
        mode = stat.S_IMODE(os.stat(self.hook_path).st_mode)
        self.assertTrue(mode & stat.S_IXUSR, "hook must be executable")
        content = open(self.hook_path, encoding="utf-8").read()
        self.assertIn("code2database_builder", content)

    def test_reinstall_is_idempotent(self):
        _run(builder.cmd_install_hook, _ns(source=self.repo,
                                           graph_dir=".callgraph"))
        _, out, _ = _run(builder.cmd_install_hook, _ns(
            source=self.repo, graph_dir=".callgraph"))
        self.assertIn("already installed", out)

    def test_appends_to_foreign_hook(self):
        os.makedirs(os.path.dirname(self.hook_path), exist_ok=True)
        with open(self.hook_path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho custom hook\n")
        _, out, _ = _run(builder.cmd_install_hook, _ns(
            source=self.repo, graph_dir=".callgraph"))
        self.assertIn("Appended", out)
        content = open(self.hook_path, encoding="utf-8").read()
        self.assertIn("custom hook", content)
        self.assertIn("code2database_builder", content)

    def test_non_git_repo_exits_1(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        ret, out, err, code = _capture_call(builder.cmd_install_hook, _ns(
            source=plain, graph_dir=".callgraph"))
        self.assertEqual(code, 1)
        self.assertIn("not found", err)


if __name__ == "__main__":
    unittest.main()
