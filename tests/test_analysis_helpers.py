"""Unit tests for the analysis helper modules (cluster G):

- code_slice: data_flow_slice (DATA_FLOW/DATA_DEP backward traversal,
  caps, missing sink), usage_slice (caller/callee kinds, CONTAINS skip),
  cmd_code_slice dispatch + unknown type
- co_change: extract_co_change_edges against a real temp git repo
  (co-change counting, min threshold, non-git → []), cmd_co_change JSON
- lock_coverage: analyze_lock_coverage (protected/unprotected regions,
  same-line event ordering, caller-lock inheritance, no-patterns
  warning, empty body), detect_races_with_lock_coverage (cross-thread
  disjoint locksets → race; same thread-model → no race; same function
  skipped), compute_caller_locks
- key_paths: cmd_key_paths output structure on a small chain graph
- explore: cmd_explore_flow empty-query error + exact-symbol match
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder import code_slice, co_change, lock_coverage, key_paths
from _builder.code_slice import data_flow_slice, usage_slice, cmd_code_slice
from _builder.co_change import extract_co_change_edges, cmd_co_change
from _builder.lock_coverage import (
    analyze_lock_coverage, detect_races_with_lock_coverage,
    compute_caller_locks,
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


def _json(text):
    return json.loads(text)


def _make_graph(nodes_spec, edges_spec, master_extra=None):
    tmp = tempfile.mkdtemp(prefix="c2d_clusterG_")
    nodes = []
    for n in nodes_spec:
        node = {"id": n["id"], "name": n.get("name", n["id"]),
                "source_file": n.get("source_file", "/tmp/x.c"),
                "line": n.get("line", 1), "domain": n.get("domain", "test"),
                "labels": n.get("labels", []),
                "is_empty": n.get("is_empty", False)}
        for k, v in n.items():
            if k not in ("id", "name"):
                node[k] = v
        nodes.append(node)
    edges = []
    for e in edges_spec:
        edges.append({"source": e["source"], "target": e["target"],
                      "relation": e.get("relation", "INVOKES"),
                      "confidence": e.get("confidence", "EXTRACTED"),
                      **{k: v for k, v in e.items()
                         if k not in ("source", "target", "relation",
                                      "confidence")}})
    with open(os.path.join(tmp, "domain_test.json"), "w") as f:
        json.dump({"nodes": nodes, "edges": edges}, f)
    master = {"source_root": "/tmp", "domains": {"test": "domain_test.json"}}
    if master_extra:
        master.update(master_extra)
    with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
        json.dump(master, f)
    return tmp


# ---------------------------------------------------------------------------
# code_slice
# ---------------------------------------------------------------------------

class TestDataFlowSlice(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph(
            [{"id": "producer", "name": "producer"},
             {"id": "transform", "name": "transform"},
             {"id": "sink", "name": "sink"}],
            [{"source": "producer", "target": "transform",
              "relation": "DATA_FLOW"},
             {"source": "transform", "target": "sink",
              "relation": "DATA_FLOW"},
             {"source": "producer", "target": "sink",
              "relation": "INVOKES"}])  # not a data edge

    def test_backward_traversal_collects_data_sources(self):
        r = data_flow_slice(self.graph_dir, "sink")
        self.assertIn("sink", r["sink"])
        ids = {n["id"] for n in r["nodes"]}
        self.assertEqual(ids, {"sink", "transform", "producer"})
        self.assertEqual(r["stats"]["node_count"], 3)
        self.assertEqual(r["stats"]["edge_count"], 2)

    def test_depth_cap_limits_traversal(self):
        r = data_flow_slice(self.graph_dir, "sink", max_depth=1)
        ids = {n["id"] for n in r["nodes"]}
        self.assertEqual(ids, {"sink", "transform"})

    def test_missing_sink_returns_error(self):
        r = data_flow_slice(self.graph_dir, "nope")
        self.assertIn("error", r)

    def test_data_dep_relation_also_traversed(self):
        gd = _make_graph(
            [{"id": "a", "name": "a"}, {"id": "b", "name": "b"}],
            [{"source": "a", "target": "b", "relation": "DATA_DEP"}])
        r = data_flow_slice(gd, "b")
        self.assertEqual({n["id"] for n in r["nodes"]}, {"a", "b"})


class TestUsageSlice(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph(
            [{"id": "fn", "name": "fn"},
             {"id": "callee1", "name": "callee1"},
             {"id": "caller1", "name": "caller1"},
             {"id": "file_node", "name": "file_node", "labels": ["file"]}],
            [{"source": "fn", "target": "callee1"},
             {"source": "caller1", "target": "fn"},
             {"source": "fn", "target": "file_node", "relation": "CONTAINS"}])

    def test_caller_and_callee_kinds(self):
        r = usage_slice(self.graph_dir, "fn")
        kinds = {n["id"]: n["kind"] for n in r["nodes"]}
        self.assertEqual(kinds["fn"], "self")
        self.assertEqual(kinds["callee1"], "callee")
        self.assertEqual(kinds["caller1"], "caller")
        # CONTAINS edge does not produce a node
        self.assertNotIn("file_node", kinds)

    def test_missing_function_returns_error(self):
        r = usage_slice(self.graph_dir, "nope")
        self.assertIn("error", r)


class TestCmdCodeSlice(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph(
            [{"id": "a", "name": "a"}, {"id": "b", "name": "b"}],
            [{"source": "a", "target": "b", "relation": "DATA_FLOW"}])

    def test_data_flow_dispatch(self):
        _, out, _ = _run(cmd_code_slice, _ns(
            graph=self.graph_dir, type="data-flow", node="b",
            max_depth=8, max_nodes=50))
        r = _json(out)
        self.assertEqual({n["id"] for n in r["nodes"]}, {"a", "b"})

    def test_usage_dispatch(self):
        _, out, _ = _run(cmd_code_slice, _ns(
            graph=self.graph_dir, type="usage", node="a",
            max_depth=3, max_nodes=30))
        r = _json(out)
        self.assertEqual(r["function"], "a")

    def test_unknown_type_error(self):
        _, out, _ = _run(cmd_code_slice, _ns(
            graph=self.graph_dir, type="bogus", node="a",
            max_depth=3, max_nodes=30))
        self.assertIn("unknown slice type", _json(out)["error"])


# ---------------------------------------------------------------------------
# co_change
# ---------------------------------------------------------------------------

class TestCoChange(unittest.TestCase):
    def _make_repo(self):
        repo = tempfile.mkdtemp(prefix="c2d_git_")
        def _git(*a, **kw):
            subprocess.run(["git", *a], cwd=repo, check=True,
                           capture_output=True, timeout=30,
                           env={**os.environ,
                                "GIT_AUTHOR_NAME": "t",
                                "GIT_AUTHOR_EMAIL": "t@t",
                                "GIT_COMMITTER_NAME": "t",
                                "GIT_COMMITTER_EMAIL": "t@t",
                                "GIT_CONFIG_GLOBAL": "/dev/null",
                                "GIT_CONFIG_SYSTEM": "/dev/null"})
        _git("init", "-q")
        # 3 commits touching a.c + b.c together (coupled)
        for i in range(3):
            for fn in ("a.c", "b.c"):
                with open(os.path.join(repo, fn), "a") as f:
                    f.write(f"// rev {i}\n")
            _git("add", "a.c", "b.c")
            _git("commit", "-q", "-m", f"couple {i}")
        # 1 commit touching only c.c
        with open(os.path.join(repo, "c.c"), "w") as f:
            f.write("// solo\n")
        _git("add", "c.c")
        _git("commit", "-q", "-m", "solo")
        return repo

    def test_coupled_files_detected(self):
        repo = self._make_repo()
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        graph_dir = tempfile.mkdtemp(prefix="c2d_cc_graph_")
        self.addCleanup(shutil.rmtree, graph_dir, ignore_errors=True)
        edges = extract_co_change_edges(repo, graph_dir, min_co_changes=3)
        self.assertTrue(edges)
        pair = edges[0]
        self.assertEqual(pair["edge_type"], "CO_CHANGE")
        self.assertEqual(pair["co_change_count"], 3)
        files = {pair["source_file"], pair["target_file"]}
        self.assertEqual(files, {"a.c", "b.c"})

    def test_min_threshold_filters_weak_pairs(self):
        repo = self._make_repo()
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        graph_dir = tempfile.mkdtemp(prefix="c2d_cc_graph2_")
        self.addCleanup(shutil.rmtree, graph_dir, ignore_errors=True)
        edges = extract_co_change_edges(repo, graph_dir, min_co_changes=5)
        self.assertEqual(edges, [])

    def test_non_git_dir_returns_empty(self):
        plain = tempfile.mkdtemp(prefix="c2d_nogit_")
        self.addCleanup(shutil.rmtree, plain, ignore_errors=True)
        self.assertEqual(extract_co_change_edges(plain, plain), [])

    def test_cmd_co_change_json_output(self):
        repo = self._make_repo()
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        _, out, _ = _run(cmd_co_change, _ns(
            source=repo, graph=repo, min_co_changes=3, max_commits=100))
        r = _json(out)
        self.assertEqual(r["source"], repo)
        self.assertEqual(r["total_edges"], len(r["edges"]))
        self.assertGreaterEqual(r["total_edges"], 1)


# ---------------------------------------------------------------------------
# lock_coverage
# ---------------------------------------------------------------------------

_PROFILE = {
    "concurrency_patterns": {
        "lock_acquire_patterns": [
            r"mutex_lock\(&?(\w+)\)",
            r"spin_lock\(&?(\w+)\)",
        ],
        "lock_release_patterns": [
            r"mutex_unlock\(&?(\w+)\)",
            r"spin_unlock\(&?(\w+)\)",
        ],
    }
}


class TestAnalyzeLockCoverage(unittest.TestCase):
    def _ndata(self, body, fields_written=(), fields_read=()):
        return {
            "id": "f", "name": "f", "body_text": body,
            "fields_written": [{"field_name": f, "struct_chain": f"ctx->{f}"}
                               for f in fields_written],
            "fields_read": [{"field_name": f, "struct_chain": f"ctx->{f}"}
                            for f in fields_read],
        }

    def test_second_access_after_unlock_unprotected(self):
        nd = self._ndata(
            "mutex_lock(&m);\n"
            "ctx->x = 1;\n"
            "mutex_unlock(&m);\n"
            "ctx->x = 2;\n",
            fields_written=("x",))
        cov = analyze_lock_coverage(nd, _PROFILE)
        self.assertEqual(len(cov.accesses), 2)
        self.assertTrue(cov.accesses[0].protected)
        self.assertEqual(cov.accesses[0].locks_held, {"m"})
        self.assertFalse(cov.accesses[1].protected)
        self.assertEqual(len(cov.unprotected_accesses), 1)

    def test_same_line_event_ordering(self):
        """mutex_lock(m); x=1; mutex_unlock(m) on ONE line: the access
        between acquire and release (by char position) is protected.
        Documented limitation: one access event per field per line — the
        second write on the same line is not separately reported."""
        nd = self._ndata(
            "mutex_lock(&m); ctx->x = 1; mutex_unlock(&m); ctx->x = 2;",
            fields_written=("x",))
        cov = analyze_lock_coverage(nd, _PROFILE)
        self.assertEqual(len(cov.accesses), 1)
        self.assertTrue(cov.accesses[0].protected)
        self.assertEqual(cov.accesses[0].locks_held, {"m"})

    def test_caller_locks_inherited(self):
        nd = self._ndata("ctx->x = 1;\n", fields_written=("x",))
        cov = analyze_lock_coverage(nd, _PROFILE, caller_locks={"outer"})
        self.assertEqual(len(cov.accesses), 1)
        self.assertTrue(cov.accesses[0].protected)
        self.assertEqual(cov.accesses[0].caller_locks, {"outer"})

    def test_no_patterns_configured_warns(self):
        nd = self._ndata("ctx->x = 1;\n", fields_written=("x",))
        cov = analyze_lock_coverage(nd, None)
        self.assertIn("no lock patterns", cov.over_approximation_warning)
        self.assertEqual(cov.accesses, [])

    def test_empty_body_returns_empty(self):
        cov = analyze_lock_coverage({"name": "f", "body_text": ""}, _PROFILE)
        self.assertEqual(cov.accesses, [])


class TestDetectRacesWithLockCoverage(unittest.TestCase):
    def _graph(self):
        return _make_graph(
            [{"id": "t1_fn", "name": "t1_fn", "thread_model": "irq",
              "body_text": "spin_lock(&l1); ctx->shared = 1; spin_unlock(&l1);",
              "fields_written": [{"field_name": "shared",
                                   "struct_chain": "ctx->shared"}]},
             {"id": "t2_fn", "name": "t2_fn", "thread_model": "worker",
              "body_text": "spin_lock(&l2); ctx->shared = 2; spin_unlock(&l2);",
              "fields_written": [{"field_name": "shared",
                                   "struct_chain": "ctx->shared"}]}],
            [])

    def test_disjoint_locksets_cross_thread_reported(self):
        G = None
        from _builder.graph_build import _load_full_graph
        G = _load_full_graph(self._graph())
        races = detect_races_with_lock_coverage(G, _PROFILE)
        self.assertTrue(races)
        # target is struct_chain + "->" + field_name
        self.assertEqual(races[0]["target"], "ctx->shared->shared")

    def test_same_thread_model_not_reported(self):
        gd = _make_graph(
            [{"id": "f1", "name": "f1", "thread_model": "irq",
              "body_text": "ctx->shared = 1;",
              "fields_written": [{"field_name": "shared",
                                   "struct_chain": "ctx->shared"}]},
             {"id": "f2", "name": "f2", "thread_model": "irq",
              "body_text": "ctx->shared = 2;",
              "fields_written": [{"field_name": "shared",
                                   "struct_chain": "ctx->shared"}]}],
            [])
        from _builder.graph_build import _load_full_graph
        races = detect_races_with_lock_coverage(_load_full_graph(gd), _PROFILE)
        self.assertEqual(races, [])

    def test_common_lock_not_reported(self):
        gd = _make_graph(
            [{"id": "f1", "name": "f1", "thread_model": "irq",
              "body_text": "mutex_lock(&m); ctx->shared = 1; mutex_unlock(&m);",
              "fields_written": [{"field_name": "shared",
                                   "struct_chain": "ctx->shared"}]},
             {"id": "f2", "name": "f2", "thread_model": "worker",
              "body_text": "mutex_lock(&m); ctx->shared = 2; mutex_unlock(&m);",
              "fields_written": [{"field_name": "shared",
                                   "struct_chain": "ctx->shared"}]}],
            [])
        from _builder.graph_build import _load_full_graph
        races = detect_races_with_lock_coverage(_load_full_graph(gd), _PROFILE)
        self.assertEqual(races, [])


class TestComputeCallerLocks(unittest.TestCase):
    def test_caller_locks_from_caller_body(self):
        # compute_caller_locks requires callee_args on the caller to
        # locate the call site
        gd = _make_graph(
            [{"id": "caller", "name": "caller",
              "body_text": "mutex_lock(&m); callee(); mutex_unlock(&m);",
              "callee_args": [{"callee": "callee", "call_line": 1}]},
             {"id": "callee", "name": "callee"}],
            [{"source": "caller", "target": "callee"}])
        from _builder.graph_build import _load_full_graph
        G = _load_full_graph(gd)
        locks = compute_caller_locks(G, "callee", _PROFILE)
        self.assertEqual(locks, {"m"})


# ---------------------------------------------------------------------------
# key_paths + explore
# ---------------------------------------------------------------------------

class TestCmdKeyPaths(unittest.TestCase):
    def test_output_structure(self):
        gd = _make_graph(
            [{"id": "api", "name": "api", "labels": ["API_entry"]},
             {"id": "mid", "name": "mid"},
             {"id": "end", "name": "end", "labels": ["out_end"]}],
            [{"source": "api", "target": "mid"},
             {"source": "mid", "target": "end"}])
        _, out, err = _run(key_paths.cmd_key_paths, _ns(
            graph=gd, top=3, from_entry=None, max_tokens=0))
        # must print something parseable (JSON doc or structured text)
        self.assertTrue(out.strip())


class TestCmdExploreFlow(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph(
            [{"id": "bdev_register", "name": "bdev_register",
              "labels": ["API_entry"]},
             {"id": "helper", "name": "helper"}],
            [{"source": "bdev_register", "target": "helper"}])

    def test_empty_query_returns_error_json(self):
        # cmd_explore_flow is @cached_query(capture_stdout=True) — the
        # wrapper RETURNS the captured JSON text
        from _builder.explore import cmd_explore_flow
        ret, _, _ = _run(cmd_explore_flow, _ns(
            graph=self.graph_dir, query="   ", max_tokens=100,
            max_nodes=5, focus_domain=None, no_cache=True))
        r = _json(ret)
        self.assertIn("error", r)
        self.assertIn("Empty query", r["error"])

    def test_exact_symbol_match_returns_node_context(self):
        from _builder.explore import cmd_explore_flow
        ret, _, _ = _run(cmd_explore_flow, _ns(
            graph=self.graph_dir, query="bdev_register", max_tokens=2000,
            max_nodes=10, focus_domain=None, no_cache=True))
        r = _json(ret)
        self.assertTrue(r["exact_match"])
        names = {n["name"] for n in r["nodes"]}
        self.assertIn("bdev_register", names)
        # the exact-match fast path must include the neighbor
        self.assertIn("helper", names)


if __name__ == "__main__":
    unittest.main()
