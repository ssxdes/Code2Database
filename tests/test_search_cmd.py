"""Unit tests for search_cmd.py — the 6 core browse commands.

Covers expected inputs/outputs for:
- cmd_search: JSON-graph mode (scoring, empty-node skip, dedup, top),
  SQLite fallback mode (LIKE escaping, degree join, extra_json is_empty),
  no-graph error
- cmd_path: shortest path, --source-file disambiguation, ambiguity
  warning, spawn_target/#if 0 edge filtering + --no-condition-filter,
  --domain-filter, vtable_dispatch domain preference, no-path errors.
  NOTE: cmd_path is wrapped by @cached_query(capture_stdout=True) — the
  wrapper RETURNS the captured JSON text instead of printing it.
- cmd_neighbors: calls/called_by directions, depth-2 BFS, CONTAINS/file
  skip, max_results truncation
- cmd_impact: forward/reverse, second_order via, lite mode, CONTAINS skip
- cmd_domain: exact + substring match, missing domain error, SQLite fallback
- cmd_load: default counts JSON + --summary format
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

from _builder import search_cmd


def _ns(**kw):
    """argparse.Namespace with no_cache default True (deterministic tests)."""
    kw.setdefault("no_cache", True)
    return argparse.Namespace(**kw)


def _capture_call(fn, args):
    """Run fn(args) capturing stdout/stderr and SystemExit code.

    Returns (ret, stdout, stderr, exit_code). exit_code is None when fn
    returned normally.
    """
    out, err = io.StringIO(), io.StringIO()
    ret, code = None, None
    with redirect_stdout(out), redirect_stderr(err):
        try:
            ret = fn(args)
        except SystemExit as e:
            code = e.code
    return ret, out.getvalue(), err.getvalue(), code


def _run(fn, args):
    """Run a cmd fn expecting normal return; returns (ret, stdout, stderr)."""
    ret, out, err, code = _capture_call(fn, args)
    assert code is None, f"unexpected SystemExit({code})"
    return ret, out, err


def _json(text):
    return json.loads(text)


def _make_graph(nodes_spec, edges_spec, domains=None):
    """Build a domain-split JSON graph fixture. Returns graph_dir."""
    tmp = tempfile.mkdtemp(prefix="c2d_search_test_")
    domain_map = {}
    if domains:
        for dname, (ns_, es_) in domains.items():
            fn = f"domain_{dname}.json"
            domain_map[dname] = fn
            nodes, edges = _defaulted(ns_, es_)
            with open(os.path.join(tmp, fn), "w") as f:
                json.dump({"nodes": nodes, "edges": edges}, f)
    else:
        domain_map["test"] = "domain_test.json"
        nodes, edges = _defaulted(nodes_spec, edges_spec)
        with open(os.path.join(tmp, "domain_test.json"), "w") as f:
            json.dump({"nodes": nodes, "edges": edges}, f)
    with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
        json.dump({"source_root": "/tmp", "domains": domain_map}, f)
    return tmp


def _defaulted(nodes_spec, edges_spec):
    nodes = []
    for n in nodes_spec or []:
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
    for e in edges_spec or []:
        edges.append({"source": e["source"], "target": e["target"],
                      "relation": e.get("relation", "INVOKES"),
                      "confidence": e.get("confidence", "EXTRACTED"),
                      **{k: v for k, v in e.items()
                         if k not in ("source", "target", "relation", "confidence")}})
    return nodes, edges


class TestCmdSearchJsonMode(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph(
            [{"id": "init_task", "name": "init_task"},
             {"id": "init_helper", "name": "init_helper",
              "semantic_desc": "initializes the task registry"},
             {"id": "other", "name": "other", "source_file": "/tmp/init_dir/x.c"},
             {"id": "empty_init", "name": "empty_init", "is_empty": True},
             {"id": "caller", "name": "caller",
              "callee_args": [{"callee": "init_task", "args_snippet": "TASK_INIT_FLAG"}]}],
            [])

    def test_name_match_ranks_first_with_score_3(self):
        _, out, err = _run(search_cmd.cmd_search,
                           _ns(graph=self.graph_dir, keywords="init", top=10))
        results = _json(out)
        self.assertGreater(len(results), 0)
        # init_helper: name(3)+id(2)+semantic_desc 'initializes...'(2)=7
        # init_task: name(3)+id(2)=5
        self.assertEqual(results[0]["id"], "init_helper")
        self.assertEqual(results[0]["score"], 7)
        by_id = {r["id"]: r for r in results}
        self.assertEqual(by_id["init_task"]["score"], 5)
        self.assertIn("Found", err)

    def test_semantic_desc_scores_2(self):
        _, out, _ = _run(search_cmd.cmd_search,
                         _ns(graph=self.graph_dir, keywords="registry", top=10))
        results = _json(out)
        self.assertEqual([r["id"] for r in results], ["init_helper"])
        self.assertEqual(results[0]["score"], 2)

    def test_is_empty_node_skipped(self):
        _, out, _ = _run(search_cmd.cmd_search,
                         _ns(graph=self.graph_dir, keywords="empty_init", top=10))
        self.assertEqual(_json(out), [])

    def test_top_truncates_result_list(self):
        # 'init' matches: init_task(5), init_helper(5), other(source=1),
        # caller(callee_args snippet=1) → 4 total
        _, out, err = _run(search_cmd.cmd_search,
                           _ns(graph=self.graph_dir, keywords="init", top=1))
        results = _json(out)
        self.assertEqual(len(results), 1)
        self.assertIn("Found 4 matches, showing top 1", err)

    def test_callee_args_snippet_searched(self):
        _, out, _ = _run(search_cmd.cmd_search,
                         _ns(graph=self.graph_dir, keywords="TASK_INIT_FLAG", top=10))
        results = _json(out)
        self.assertEqual([r["id"] for r in results], ["caller"])

    def test_no_match_returns_empty_list(self):
        _, out, _ = _run(search_cmd.cmd_search,
                         _ns(graph=self.graph_dir, keywords="zzz_no_match", top=10))
        self.assertEqual(_json(out), [])

    def test_source_only_match_scores_1(self):
        _, out, _ = _run(search_cmd.cmd_search,
                         _ns(graph=self.graph_dir, keywords="init_dir", top=10))
        results = _json(out)
        self.assertEqual([r["id"] for r in results], ["other"])
        self.assertEqual(results[0]["score"], 1)


class TestCmdSearchSqliteMode(unittest.TestCase):
    """SQLite fallback when code2database_master.json is absent."""

    def setUp(self):
        self.graph_dir = tempfile.mkdtemp(prefix="c2d_search_sql_")
        db = os.path.join(self.graph_dir, "code2database.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE functions (id TEXT PRIMARY KEY, name TEXT, "
            "source_file TEXT, domain TEXT, line_number INTEGER, labels TEXT, "
            "signature TEXT, extra_json TEXT)")
        conn.execute(
            "CREATE TABLE edges (invoker_id TEXT, invoked_id TEXT, "
            "relation TEXT, call_order INTEGER, call_condition TEXT, "
            "confidence TEXT, source TEXT)")
        rows = [
            ("init_task", "init_task", "/tmp/a.c", "core", 10, "[]", "void init_task(void)", None),
            ("initXtask", "initXtask", "/tmp/b.c", "core", 20, "[]", "", None),
            ("ghost", "ghost", "/tmp/c.c", "core", 30, "[]", "",
             json.dumps({"is_empty": True})),
            ("boot", "boot", "/tmp/d.c", "core", 40, '["API_entry"]', "", None),
        ]
        conn.executemany("INSERT INTO functions VALUES (?,?,?,?,?,?,?,?)", rows)
        edges = [
            ("boot", "init_task", "INVOKES", 1, "", "EXTRACTED", "ast"),
            ("boot", "init_task", "INVOKES", 2, "", "EXTRACTED", "ast"),
            ("boot", "init_task", "CONTAINS", None, "", "EXTRACTED", "ast"),
            ("boot", "ghost", "CONTAINS", None, "", "EXTRACTED", "ast"),
        ]
        conn.executemany("INSERT INTO edges VALUES (?,?,?,?,?,?,?)", edges)
        conn.commit()
        conn.close()

    def test_underscore_keyword_does_not_match_similar_names(self):
        """LIKE escaping: 'init_task' must NOT match 'initXtask'."""
        _, out, _ = _run(search_cmd.cmd_search,
                         _ns(graph=self.graph_dir, keywords="init_task", top=10))
        ids = {r["id"] for r in _json(out)}
        self.assertIn("init_task", ids)
        self.assertNotIn("initXtask", ids)

    def test_percent_keyword_does_not_match_everything(self):
        """'50%' must not act as a wildcard matching all rows."""
        _, out, _ = _run(search_cmd.cmd_search,
                         _ns(graph=self.graph_dir, keywords="50%", top=10))
        self.assertEqual(_json(out), [])

    def test_degree_counts_only_invokes_edges(self):
        _, out, _ = _run(search_cmd.cmd_search,
                         _ns(graph=self.graph_dir, keywords="init_task", top=10))
        target = next(r for r in _json(out) if r["id"] == "init_task")
        # 2 INVOKES in-edges; CONTAINS edge excluded by the join
        self.assertEqual(target["degree"], 2)

    def test_is_empty_from_extra_json_skipped(self):
        _, out, _ = _run(search_cmd.cmd_search,
                         _ns(graph=self.graph_dir, keywords="ghost", top=10))
        self.assertEqual(_json(out), [])

    def test_labels_parsed_from_db(self):
        _, out, _ = _run(search_cmd.cmd_search,
                         _ns(graph=self.graph_dir, keywords="boot", top=10))
        self.assertEqual(_json(out)[0]["labels"], ["API_entry"])


class TestCmdSearchNoGraph(unittest.TestCase):
    def test_missing_master_and_db_exits_1(self):
        tmp = tempfile.mkdtemp(prefix="c2d_search_none_")
        ret, out, err, code = _capture_call(
            search_cmd.cmd_search, _ns(graph=tmp, keywords="x", top=5))
        self.assertEqual(code, 1)
        self.assertIn("not found", err)


class TestCmdPath(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph(
            [{"id": "a", "name": "a", "domain": "core"},
             {"id": "b", "name": "b", "domain": "core"},
             {"id": "c", "name": "c", "domain": "core"},
             {"id": "d", "name": "d", "domain": "fs"}],
            [{"source": "a", "target": "b", "call_order": 1},
             {"source": "b", "target": "c", "call_condition": "CONFIG_X",
              "call_order": 2}])

    def _path_args(self, **kw):
        kw.setdefault("graph", self.graph_dir)
        kw.setdefault("from_node", "a")
        kw.setdefault("to_node", "c")
        kw.setdefault("source_file", "")
        kw.setdefault("no_condition_filter", False)
        kw.setdefault("prefer_same_domain", True)
        kw.setdefault("strict_vtable_domain", False)
        kw.setdefault("vtable_bind", "")
        kw.setdefault("domain_filter", "")
        return _ns(**kw)

    def test_shortest_path_with_edges_and_length(self):
        ret, _, _ = _run(search_cmd.cmd_path, self._path_args())
        result = _json(ret)
        self.assertEqual([p["id"] for p in result["path"]], ["a", "b", "c"])
        self.assertEqual(result["length"], 2)
        self.assertEqual(len(result["edges"]), 2)
        self.assertEqual(result["edges"][1]["call_condition"], "CONFIG_X")

    def test_missing_node_exits_1_with_hint(self):
        ret, out, err, code = _capture_call(
            search_cmd.cmd_path, self._path_args(from_node="zzz"))
        self.assertEqual(code, 1)
        self.assertIn("not found", err)

    def test_no_path_exits_1(self):
        ret, out, err, code = _capture_call(
            search_cmd.cmd_path, self._path_args(from_node="c", to_node="a"))
        self.assertEqual(code, 1)

    def test_if0_condition_edge_filtered_by_default(self):
        gd = _make_graph(
            [{"id": "a", "name": "a"}, {"id": "dead", "name": "dead"},
             {"id": "c", "name": "c"}],
            [{"source": "a", "target": "dead", "call_condition": "#if 0 DEBUG"},
             {"source": "dead", "target": "c"}])
        ret, out, err, code = _capture_call(
            search_cmd.cmd_path, self._path_args(graph=gd))
        self.assertEqual(code, 1)
        # no_condition_filter bypasses the filter and finds the path
        ret2, _, _ = _run(search_cmd.cmd_path,
                          self._path_args(graph=gd, no_condition_filter=True))
        result = _json(ret2)
        self.assertEqual([p["id"] for p in result["path"]], ["a", "dead", "c"])

    def test_source_file_disambiguation(self):
        gd = _make_graph(
            [{"id": "dup1", "name": "drop_buffers", "source_file": "/src/fs/a.c"},
             {"id": "dup2", "name": "drop_buffers", "source_file": "/src/mm/b.c"},
             {"id": "target", "name": "target", "source_file": "/src/fs/a.c"}],
            [{"source": "dup1", "target": "target"},
             {"source": "dup2", "target": "target"}])
        ret, _, _ = _run(search_cmd.cmd_path, self._path_args(
            graph=gd, source_file="a.c", from_node="drop_buffers", to_node="target"))
        result = _json(ret)
        self.assertEqual(result["path"][0]["id"], "dup1")

    def test_ambiguous_name_without_source_file_warns_and_exits(self):
        gd = _make_graph(
            [{"id": "dup1", "name": "drop_buffers", "source_file": "/src/fs/a.c"},
             {"id": "dup2", "name": "drop_buffers", "source_file": "/src/mm/b.c"},
             {"id": "target", "name": "target"}],
            [{"source": "dup1", "target": "target"},
             {"source": "dup2", "target": "target"}])
        ret, out, err, code = _capture_call(
            search_cmd.cmd_path, self._path_args(
                graph=gd, from_node="drop_buffers", to_node="target"))
        self.assertEqual(code, 1)
        self.assertIn("ambiguous", err)
        self.assertIn("--source-file", err)

    def test_domain_filter_blocks_cross_domain_path(self):
        # d is in domain 'fs'; filtering to 'core' removes d → no path a→d
        ret, out, err, code = _capture_call(
            search_cmd.cmd_path,
            self._path_args(from_node="a", to_node="d", domain_filter="core"))
        self.assertEqual(code, 1)
        self.assertIn("not in --domain-filter", err)

    def test_domain_filter_allows_in_domain_path(self):
        ret, _, _ = _run(search_cmd.cmd_path,
                         self._path_args(domain_filter="core"))
        result = _json(ret)
        self.assertEqual([p["id"] for p in result["path"]], ["a", "b", "c"])

    def test_vtable_soft_preference_keeps_cross_domain_when_only_option(self):
        gd = _make_graph(
            [{"id": "src", "name": "src", "domain": "ext4"},
             {"id": "hub", "name": "hub", "domain": "ext4"},
             {"id": "impl_other", "name": "impl_other", "domain": "xfs"},
             {"id": "sink", "name": "sink", "domain": "xfs"}],
            [{"source": "src", "target": "hub"},
             {"source": "hub", "target": "impl_other", "concurrency": "vtable_dispatch"},
             {"source": "impl_other", "target": "sink"}])
        ret, _, _ = _run(search_cmd.cmd_path, self._path_args(
            graph=gd, from_node="src", to_node="sink"))
        result = _json(ret)
        self.assertEqual([p["id"] for p in result["path"]],
                         ["src", "hub", "impl_other", "sink"])

    def test_vtable_soft_preference_removes_cross_domain_when_same_domain_exists(self):
        gd = _make_graph(
            [{"id": "src", "name": "src", "domain": "ext4"},
             {"id": "hub", "name": "hub", "domain": "ext4"},
             {"id": "impl_same", "name": "impl_same", "domain": "ext4"},
             {"id": "impl_other", "name": "impl_other", "domain": "xfs"},
             {"id": "sink", "name": "sink", "domain": "xfs"}],
            [{"source": "src", "target": "hub"},
             {"source": "hub", "target": "impl_same", "concurrency": "vtable_dispatch"},
             {"source": "hub", "target": "impl_other", "concurrency": "vtable_dispatch"},
             {"source": "impl_same", "target": "sink"},
             {"source": "impl_other", "target": "sink"}])
        ret, _, _ = _run(search_cmd.cmd_path, self._path_args(
            graph=gd, from_node="src", to_node="sink"))
        result = _json(ret)
        self.assertIn("impl_same", [p["id"] for p in result["path"]])

    def test_result_cache_roundtrip_replays_identical_output(self):
        args = self._path_args()
        args.no_cache = False
        ret1, _, _ = _run(search_cmd.cmd_path, args)
        ret2, _, _ = _run(search_cmd.cmd_path, args)
        self.assertEqual(_json(ret1), _json(ret2))


class TestCmdNeighbors(unittest.TestCase):
    def setUp(self):
        # a→b→c chain, x→a caller, b CONTAINS file node f.c
        self.graph_dir = _make_graph(
            [{"id": "a", "name": "a"},
             {"id": "b", "name": "b"},
             {"id": "c", "name": "c"},
             {"id": "x", "name": "x"},
             {"id": "f.c", "name": "f.c", "labels": ["file"], "node_type": "file"}],
            [{"source": "a", "target": "b"},
             {"source": "b", "target": "c"},
             {"source": "x", "target": "a"},
             {"source": "b", "target": "f.c", "relation": "CONTAINS"}])

    def _nb_args(self, **kw):
        kw.setdefault("graph", self.graph_dir)
        kw.setdefault("node", "a")
        kw.setdefault("depth", 1)
        kw.setdefault("max_results", 200)
        return _ns(**kw)

    def test_depth1_calls_and_called_by_directions(self):
        _, out, _ = _run(search_cmd.cmd_neighbors, self._nb_args())
        by_id = {r["id"]: r for r in _json(out)}
        self.assertEqual(by_id["b"]["direction"], "calls")
        self.assertEqual(by_id["x"]["direction"], "called_by")
        self.assertEqual(by_id["b"]["distance"], 1)

    def test_depth2_reaches_second_ring_with_distance(self):
        _, out, _ = _run(search_cmd.cmd_neighbors, self._nb_args(depth=2))
        by_id = {r["id"]: r for r in _json(out)}
        self.assertIn("c", by_id)
        self.assertEqual(by_id["c"]["distance"], 2)
        self.assertEqual(by_id["c"]["direction"], "calls")

    def test_contains_edge_and_file_nodes_skipped(self):
        _, out, _ = _run(search_cmd.cmd_neighbors,
                         self._nb_args(node="b", depth=1))
        ids = {r["id"] for r in _json(out)}
        self.assertNotIn("f.c", ids)
        self.assertEqual(ids, {"a", "c"})

    def test_max_results_truncation_flagged_on_stderr(self):
        _, out, err = _run(search_cmd.cmd_neighbors,
                           self._nb_args(depth=2, max_results=1))
        self.assertEqual(len(_json(out)), 1)
        self.assertIn("truncated", err)

    def test_missing_node_exits_1(self):
        ret, out, err, code = _capture_call(
            search_cmd.cmd_neighbors, self._nb_args(node="nope"))
        self.assertEqual(code, 1)

    def test_isolated_node_returns_empty(self):
        gd = _make_graph([{"id": "lonely", "name": "lonely"},
                          {"id": "other", "name": "other"}], [])
        _, out, err = _run(search_cmd.cmd_neighbors,
                           self._nb_args(graph=gd, node="lonely"))
        self.assertEqual(_json(out), [])
        self.assertIn("0 neighbors", err)


class TestCmdImpact(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph(
            [{"id": "lib_api", "name": "lib_api", "domain": "lib"},
             {"id": "mid1", "name": "mid1", "domain": "lib"},
             {"id": "mid2", "name": "mid2", "domain": "drv"},
             {"id": "leaf", "name": "leaf", "domain": "drv"},
             {"id": "f.c", "name": "f.c", "labels": ["file"]}],
            [{"source": "lib_api", "target": "mid1"},
             {"source": "lib_api", "target": "mid2"},
             {"source": "mid1", "target": "leaf"},
             {"source": "lib_api", "target": "f.c", "relation": "CONTAINS"},
             # cycle: leaf calls back into lib_api (must not self-list)
             {"source": "leaf", "target": "lib_api"}])

    def _impact_args(self, **kw):
        kw.setdefault("graph", self.graph_dir)
        kw.setdefault("node", "lib_api")
        kw.setdefault("direction", "forward")
        kw.setdefault("lite", False)
        return _ns(**kw)

    def test_forward_direct_and_second_order(self):
        _, out, _ = _run(search_cmd.cmd_impact, self._impact_args())
        r = _json(out)
        self.assertEqual(r["direct_impact"], 2)  # mid1, mid2 (CONTAINS skipped)
        self.assertEqual(r["second_order_impact"], 1)  # leaf via mid1
        self.assertEqual({d["id"] for d in r["direct"]}, {"mid1", "mid2"})
        so = r["second_order"][0]
        self.assertEqual(so["id"], "leaf")
        self.assertEqual(so["via"], "mid1")

    def test_forward_excludes_node_itself_from_second_order(self):
        _, out, _ = _run(search_cmd.cmd_impact, self._impact_args())
        so_ids = {d["id"] for d in _json(out)["second_order"]}
        self.assertNotIn("lib_api", so_ids)  # cycle edge leaf→lib_api

    def test_reverse_lists_callers(self):
        _, out, _ = _run(search_cmd.cmd_impact,
                         self._impact_args(node="mid1", direction="reverse"))
        r = _json(out)
        self.assertEqual({d["id"] for d in r["direct"]}, {"lib_api"})

    def test_lite_mode_omits_node_lists(self):
        _, out, _ = _run(search_cmd.cmd_impact, self._impact_args(lite=True))
        r = _json(out)
        self.assertNotIn("direct", r)
        self.assertNotIn("second_order", r)
        self.assertEqual(r["direct_impact"], 2)

    def test_domains_affected_collected(self):
        _, out, _ = _run(search_cmd.cmd_impact, self._impact_args())
        self.assertEqual(sorted(_json(out)["domains_affected"]), ["drv", "lib"])

    def test_missing_node_exits_1(self):
        ret, out, err, code = _capture_call(
            search_cmd.cmd_impact, self._impact_args(node="zzz"))
        self.assertEqual(code, 1)
        self.assertIn("not found", err)


class TestCmdDomain(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph(None, None, domains={
            "filesystem": ([{"id": "fs_a", "name": "fs_a", "domain": "filesystem"}],
                           [{"source": "fs_a", "target": "fs_a"}]),
            "memory": ([{"id": "mm_a", "name": "mm_a", "domain": "memory"}], []),
        })

    def test_exact_domain_match_outputs_nodes_and_edges(self):
        _, out, _ = _run(search_cmd.cmd_domain,
                         _ns(graph=self.graph_dir, name="filesystem"))
        data = _json(out)
        self.assertEqual([n["id"] for n in data["nodes"]], ["fs_a"])
        self.assertEqual(len(data["edges"]), 1)

    def test_case_insensitive_substring_match(self):
        _, out, _ = _run(search_cmd.cmd_domain,
                         _ns(graph=self.graph_dir, name="FILE"))
        self.assertEqual(len(_json(out)["nodes"]), 1)

    def test_missing_domain_exits_1_lists_available(self):
        ret, out, err, code = _capture_call(
            search_cmd.cmd_domain, _ns(graph=self.graph_dir, name="gpu"))
        self.assertEqual(code, 1)
        self.assertIn("Available", err)

    def test_sqlite_fallback_when_master_absent(self):
        tmp = tempfile.mkdtemp(prefix="c2d_domain_sql_")
        db = os.path.join(tmp, "code2database.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE functions (id TEXT, name TEXT, source_file TEXT,"
                     " domain TEXT, line_number INTEGER, labels TEXT, signature TEXT,"
                     " extra_json TEXT)")
        conn.execute("CREATE TABLE edges (invoker_id TEXT, invoked_id TEXT,"
                     " relation TEXT, call_order INTEGER, call_condition TEXT,"
                     " confidence TEXT, source TEXT)")
        conn.execute("INSERT INTO functions VALUES ('n1','f1','/a.c','kernel',1,'[]','',NULL)")
        conn.execute("INSERT INTO functions VALUES ('n2','f2','/b.c','drivers',2,'[]','',NULL)")
        conn.execute("INSERT INTO edges VALUES ('n1','n2','INVOKES',1,'','EXTRACTED','ast')")
        conn.commit()
        conn.close()
        _, out, _ = _run(search_cmd.cmd_domain, _ns(graph=tmp, name="kernel"))
        data = _json(out)
        self.assertEqual(data["domain"], "kernel")
        self.assertEqual(data["node_count"], 1)
        self.assertEqual(data["edge_count"], 1)

    def test_sqlite_fallback_missing_domain_exits_1(self):
        tmp = tempfile.mkdtemp(prefix="c2d_domain_sql2_")
        db = os.path.join(tmp, "code2database.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE functions (id TEXT, name TEXT, source_file TEXT,"
                     " domain TEXT, line_number INTEGER, labels TEXT, signature TEXT,"
                     " extra_json TEXT)")
        conn.execute("CREATE TABLE edges (invoker_id TEXT, invoked_id TEXT,"
                     " relation TEXT, call_order INTEGER, call_condition TEXT,"
                     " confidence TEXT, source TEXT)")
        conn.execute("INSERT INTO functions VALUES ('n1','f1','/a.c','kernel',1,'[]','',NULL)")
        conn.commit()
        conn.close()
        ret, out, err, code = _capture_call(
            search_cmd.cmd_domain, _ns(graph=tmp, name="gpu"))
        self.assertEqual(code, 1)
        self.assertIn("not found", err)

    def test_neither_master_nor_db_exits_1(self):
        tmp = tempfile.mkdtemp(prefix="c2d_domain_none_")
        ret, out, err, code = _capture_call(
            search_cmd.cmd_domain, _ns(graph=tmp, name="x"))
        self.assertEqual(code, 1)


class TestCmdLoad(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph(
            [{"id": "n1", "name": "n1", "labels": ["API_entry"]},
             {"id": "n2", "name": "n2", "is_empty": True}],
            [{"source": "n1", "target": "n2"}])

    def test_default_outputs_node_edge_counts_json(self):
        _, out, _ = _run(search_cmd.cmd_load,
                         _ns(graph=self.graph_dir, summary=False))
        self.assertEqual(_json(out), {"nodes": 2, "edges": 1})

    def test_summary_prints_counts_domains_labels(self):
        _, out, _ = _run(search_cmd.cmd_load,
                         _ns(graph=self.graph_dir, summary=True))
        self.assertIn("Nodes: 2 (including 1 empty/conditional nodes)", out)
        self.assertIn("Edges: 1", out)
        self.assertIn("Labels: {'API_entry': 1}", out)
        self.assertIn("Domains list: test", out)


if __name__ == "__main__":
    unittest.main()
