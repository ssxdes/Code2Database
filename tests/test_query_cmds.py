"""Unit tests for query.py CLI command surfaces.

Covers expected inputs/outputs for:
- cmd_describe_node: callers/callees/key_conditions/branches, brief vs
  full location, empty-node early return, missing node error
- cmd_trace_chain: from→to annotated path, no-to BFS order, vtable
  dispatch pruning via --bindings, macro filtering via --macros
- cmd_resolve_chain: dead-branch pruning via --bindings (#ifdef MACRO=0)
- cmd_diff_chains: only_in_a/only_in_b/common + summary counts
- cmd_blast_radius: depth-limited reverse reach, API/test detection,
  CONTAINS exclusion, domain collection
- cmd_field_access: writers/readers from fields_read/written, struct
  filter, NULL-form --value filter
- cmd_param_flow: callee_args propagation, word-boundary arg match,
  reached_end flag
- cmd_get_code_snippet: real source read + context window, missing
  source file error, missing node error
- cmd_graph_provenance: manifest source_commit, missing manifest,
  missing source_commit
- cmd_blame_node: non-git source_root falls back to type=none meta
- cmd_reverse_trace: crash-point required, path enumeration with
  entry-first ordering
"""
import argparse
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder import query


def _ns(**kw):
    kw.setdefault("no_cache", True)
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
    tmp = tempfile.mkdtemp(prefix="c2d_query_test_")
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
                         if k not in ("source", "target", "relation", "confidence")}})
    with open(os.path.join(tmp, "domain_test.json"), "w") as f:
        json.dump({"nodes": nodes, "edges": edges}, f)
    master = {"source_root": "/tmp", "domains": {"test": "domain_test.json"}}
    if master_extra:
        master.update(master_extra)
    with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
        json.dump(master, f)
    return tmp


class TestCmdDescribeNode(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph(
            [{"id": "core_api", "name": "core_api",
              "signature": "int core_api(struct ctx *c)",
              "labels": ["API_entry"],
              "params": [{"name": "c", "type": "struct ctx *"}],
              "semantic_desc": "Entry point that dispatches work."},
             {"id": "helper", "name": "helper", "line": 5},
             {"id": "dbg_path", "name": "dbg_path", "line": 9}],
            [{"source": "core_api", "target": "helper", "call_order": 1},
             {"source": "core_api", "target": "dbg_path", "call_order": 2,
              "call_condition": "CONFIG_DEBUG"}])

    def _args(self, **kw):
        kw.setdefault("graph", self.graph_dir)
        kw.setdefault("node", "core_api")
        kw.setdefault("detail", "full")
        kw.setdefault("context", False)
        kw.setdefault("include_body", False)
        kw.setdefault("snippet", 0)
        return _ns(**kw)

    def test_full_view_lists_callers_callees_and_conditions(self):
        ret, _, _ = _run(query.cmd_describe_node, self._args())
        r = _json(ret)
        self.assertEqual(r["id"], "core_api")
        self.assertEqual(r["name"], "core_api")
        self.assertEqual(r["signature"], "int core_api(struct ctx *c)")
        # full detail: callers/callees are rich dicts
        self.assertEqual(r["callers"], [])
        self.assertEqual({c["id"] for c in r["callees"]}, {"helper", "dbg_path"})
        dbg = next(c for c in r["callees"] if c["id"] == "dbg_path")
        self.assertEqual(dbg["call_condition"], "CONFIG_DEBUG")
        self.assertEqual(dbg["call_order"], 2)
        self.assertEqual(r["key_conditions"], ["CONFIG_DEBUG"])
        self.assertIn("CONFIG_DEBUG", r["conditional_compilation"]["conditions"])
        self.assertIn("exec_summary", r)

    def test_callers_listed_with_location(self):
        ret, _, _ = _run(query.cmd_describe_node, self._args(node="helper"))
        r = _json(ret)
        self.assertEqual(r["callers"][0]["id"], "core_api")
        self.assertEqual(r["callers"][0]["location"], "/tmp/x.c:1")

    def test_brief_location_uses_domain_prefix(self):
        ret, _, _ = _run(query.cmd_describe_node,
                         self._args(node="helper", detail="brief"))
        r = _json(ret)
        self.assertEqual(r["location"], "test:5")

    def test_full_location_uses_source_path(self):
        ret, _, _ = _run(query.cmd_describe_node,
                         self._args(node="helper", detail="full"))
        r = _json(ret)
        self.assertEqual(r["location"], "/tmp/x.c:5")

    def test_empty_node_returns_condition_early(self):
        gd = _make_graph(
            [{"id": "cond_node", "name": "cond_node", "is_empty": True,
              "condition": "CONFIG_X"}],
            [{"source": "cond_node", "target": "helper"}])
        ret, _, _ = _run(query.cmd_describe_node,
                         self._args(graph=gd, node="cond_node"))
        r = _json(ret)
        self.assertTrue(r["is_empty"])
        self.assertEqual(r["condition"], "CONFIG_X")

    def test_missing_node_exits_1(self):
        ret, out, err, code = _capture_call(
            query.cmd_describe_node, self._args(node="nope"))
        self.assertEqual(code, 1)
        self.assertIn("not found", err)

    def test_node_lookup_requires_exact_id(self):
        """describe-node resolves --node by exact ID only (no fuzzy name
        match, unlike trace-chain/io-path which use _find_node_id)."""
        gd = _make_graph(
            [{"id": "id_abc", "name": "pretty_name", "line": 3}], [])
        ret, _, _ = _run(query.cmd_describe_node,
                         self._args(graph=gd, node="id_abc"))
        r = _json(ret)
        self.assertEqual(r["id"], "id_abc")
        self.assertEqual(r["name"], "pretty_name")
        # the display name alone is NOT resolvable
        ret2, out, err, code = _capture_call(
            query.cmd_describe_node, self._args(graph=gd, node="pretty_name"))
        self.assertEqual(code, 1)


class TestCmdTraceChain(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph(
            [{"id": "a", "name": "a"}, {"id": "b", "name": "b"},
             {"id": "c", "name": "c"}, {"id": "dead", "name": "dead"}],
            [{"source": "a", "target": "b", "call_order": 1},
             {"source": "b", "target": "c", "call_order": 1,
              "call_condition": "#ifdef CONFIG_X"},
             {"source": "a", "target": "dead", "call_order": 2,
              "call_condition": "#if 0"}])

    def _args(self, **kw):
        kw.setdefault("graph", self.graph_dir)
        kw.setdefault("from_node", "a")
        kw.setdefault("to_node", "")
        kw.setdefault("bindings", "")
        kw.setdefault("macros", "")
        kw.setdefault("json", True)
        return _ns(**kw)

    def test_from_to_annotated_path(self):
        ret, _, _ = _run(query.cmd_trace_chain, self._args(to_node="c"))
        r = _json(ret)
        self.assertEqual([s["id"] for s in r["path"]], ["a", "b", "c"])
        self.assertEqual(r["total_steps"], 3)
        edge_steps = [s for s in r["path"] if s["id"] != "a"]
        self.assertEqual(edge_steps[1]["call_condition"], "#ifdef CONFIG_X")

    def test_no_to_node_returns_bfs_order_without_dead_edges(self):
        ret, _, _ = _run(query.cmd_trace_chain, self._args())
        r = _json(ret)
        ids = [s["id"] for s in r["path"]]
        self.assertEqual(ids[0], "a")
        self.assertIn("b", ids)
        self.assertIn("c", ids)
        self.assertNotIn("dead", ids)  # '#if 0' pruned

    def test_vtable_dispatch_pruned_by_module_binding(self):
        gd = _make_graph(
            [{"id": "src", "name": "src"}, {"id": "hub", "name": "hub"},
             {"id": "nvme_probe", "name": "nvme_probe"},
             {"id": "sata_probe", "name": "sata_probe"}],
            [{"source": "src", "target": "hub"},
             {"source": "hub", "target": "nvme_probe",
              "concurrency": "vtable_dispatch",
              "call_condition": "#vtable_module=nvme"},
             {"source": "hub", "target": "sata_probe",
              "concurrency": "vtable_dispatch",
              "call_condition": "#vtable_module=sata"}])
        ret, _, _ = _run(query.cmd_trace_chain, self._args(
            graph=gd, bindings="module=sata"))
        r = _json(ret)
        ids = [s["id"] for s in r["path"]]
        self.assertIn("sata_probe", ids)
        self.assertNotIn("nvme_probe", ids)

    def test_macro_filter_prunes_foreign_macro_edges(self):
        gd = _make_graph(
            [{"id": "a", "name": "a"}, {"id": "b", "name": "b"},
             {"id": "c", "name": "c"}],
            [{"source": "a", "target": "b", "call_condition": "#ifdef CONFIG_X"},
             {"source": "a", "target": "c", "call_condition": "#ifdef CONFIG_Y"}])
        ret, _, _ = _run(query.cmd_trace_chain, self._args(
            graph=gd, macros="CONFIG_Y"))
        r = _json(ret)
        ids = [s["id"] for s in r["path"]]
        self.assertIn("c", ids)
        self.assertNotIn("b", ids)
        self.assertEqual(r["macros"], ["CONFIG_Y"])

    def test_missing_from_node_exits_1(self):
        ret, out, err, code = _capture_call(
            query.cmd_trace_chain, self._args(from_node="zzz"))
        self.assertEqual(code, 1)
        self.assertIn("not found", err)


class TestCmdResolveChain(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph(
            [{"id": "a", "name": "a"}, {"id": "b_on", "name": "b_on"},
             {"id": "c_off", "name": "c_off"}],
            [{"source": "a", "target": "b_on", "call_condition": "#ifdef FEATURE_A"},
             {"source": "a", "target": "c_off", "call_condition": "#ifdef FEATURE_B"}])

    def _args(self, **kw):
        kw.setdefault("graph", self.graph_dir)
        kw.setdefault("node", "a")
        kw.setdefault("bindings", "")
        kw.setdefault("json", True)
        return _ns(**kw)

    def test_no_bindings_keeps_all_branches(self):
        _, out, _ = _run(query.cmd_resolve_chain, self._args())
        r = _json(out)
        step_ids = {s["id"] for s in r["resolved_steps"]}
        self.assertEqual(step_ids, {"a", "b_on", "c_off"})

    def test_disabled_macro_branch_pruned(self):
        _, out, _ = _run(query.cmd_resolve_chain,
                         self._args(bindings="FEATURE_A=0"))
        r = _json(out)
        step_ids = {s["id"] for s in r["resolved_steps"]}
        self.assertIn("c_off", step_ids)  # FEATURE_B unbound → conservative keep
        self.assertNotIn("b_on", step_ids)  # FEATURE_A=0 → pruned

    def test_missing_node_exits_1(self):
        ret, out, err, code = _capture_call(
            query.cmd_resolve_chain, self._args(node="zzz"))
        self.assertEqual(code, 1)


class TestCmdDiffChains(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph(
            [{"id": "a", "name": "a"}, {"id": "via_nvme", "name": "via_nvme"},
             {"id": "via_sata", "name": "via_sata"}, {"id": "sink", "name": "sink"}],
            [{"source": "a", "target": "via_nvme",
              "concurrency": "vtable_dispatch",
              "call_condition": "#vtable_module=nvme"},
             {"source": "a", "target": "via_sata",
              "concurrency": "vtable_dispatch",
              "call_condition": "#vtable_module=sata"},
             {"source": "via_nvme", "target": "sink"},
             {"source": "via_sata", "target": "sink"}])

    def _args(self, **kw):
        kw.setdefault("graph", self.graph_dir)
        kw.setdefault("node", "a")
        kw.setdefault("bindings_a", "")
        kw.setdefault("bindings_b", "")
        kw.setdefault("json", True)
        return _ns(**kw)

    def test_diff_between_two_module_bindings(self):
        _, out, _ = _run(query.cmd_diff_chains, self._args(
            bindings_a="module=nvme", bindings_b="module=sata"))
        r = _json(out)
        self.assertEqual([s["id"] for s in r["only_in_a"]], ["via_nvme"])
        self.assertEqual([s["id"] for s in r["only_in_b"]], ["via_sata"])
        common_ids = {s["id"] for s in r["common"]}
        self.assertEqual(common_ids, {"a", "sink"})
        s = r["summary"]
        self.assertEqual(s["only_a_count"], 1)
        self.assertEqual(s["only_b_count"], 1)
        self.assertEqual(s["common_count"], 2)

    def test_identical_bindings_have_no_diff(self):
        _, out, _ = _run(query.cmd_diff_chains, self._args(
            bindings_a="module=nvme", bindings_b="module=nvme"))
        r = _json(out)
        self.assertEqual(r["only_in_a"], [])
        self.assertEqual(r["only_in_b"], [])


class TestCmdBlastRadius(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph(
            [{"id": "lib_fn", "name": "lib_fn", "domain": "lib"},
             {"id": "mid", "name": "mid", "domain": "app"},
             {"id": "api_top", "name": "api_top", "domain": "app",
              "labels": ["API_entry"]},
             {"id": "test_lib_case", "name": "test_lib_case", "domain": "unit"},
             {"id": "file_node", "name": "file_node", "labels": ["file"]}],
            [{"source": "mid", "target": "lib_fn"},
             {"source": "api_top", "target": "mid"},
             {"source": "test_lib_case", "target": "lib_fn"},
             {"source": "file_node", "target": "lib_fn", "relation": "CONTAINS"}])

    def _args(self, **kw):
        kw.setdefault("graph", self.graph_dir)
        kw.setdefault("node", "lib_fn")
        kw.setdefault("depth", 3)
        kw.setdefault("json", True)
        return _ns(**kw)

    def test_reverse_reach_with_api_and_test_detection(self):
        _, out, _ = _run(query.cmd_blast_radius, self._args())
        r = _json(out)
        self.assertEqual(r["total_affected_functions"], 3)  # mid, api_top, test
        self.assertEqual([a["id"] for a in r["affected_apis"]], ["api_top"])
        self.assertEqual([t["id"] for t in r["affected_tests"]], ["test_lib_case"])
        self.assertEqual(r["affected_domains"], ["app", "unit"])

    def test_depth_1_limits_reverse_reach(self):
        _, out, _ = _run(query.cmd_blast_radius, self._args(depth=1))
        r = _json(out)
        # direct callers only: mid + test_lib_case (CONTAINS file_node excluded)
        self.assertEqual(r["total_affected_functions"], 2)
        self.assertEqual(r["affected_apis"], [])

    def test_contains_edges_excluded(self):
        _, out, _ = _run(query.cmd_blast_radius, self._args())
        r = _json(out)
        self.assertNotIn("file_node",
                         [a["id"] for a in r["affected_apis"]] +
                         [t["id"] for t in r["affected_tests"]])

    def test_missing_node_exits_1(self):
        ret, out, err, code = _capture_call(
            query.cmd_blast_radius, self._args(node="zzz"))
        self.assertEqual(code, 1)


class TestCmdFieldAccess(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph(
            [{"id": "writer", "name": "writer",
              "fields_written": [{"field_name": "state", "struct_chain": "ctx->state",
                                   "assigned_value": "NULL"}]},
             {"id": "init_writer", "name": "init_writer",
              "fields_written": [{"field_name": "state", "struct_chain": "ctx->state",
                                   "assigned_value": "1"}]},
             {"id": "reader", "name": "reader",
              "fields_read": [{"field_name": "state", "struct_chain": "ctx->state"}]},
             {"id": "other_fn", "name": "other_fn",
              "fields_read": [{"field_name": "count", "struct_chain": "ctx->count"}]}],
            [])

    def _args(self, **kw):
        kw.setdefault("graph", self.graph_dir)
        kw.setdefault("field", "state")
        kw.setdefault("struct", "")
        kw.setdefault("value", "")
        kw.setdefault("json", True)
        return _ns(**kw)

    def test_writers_and_readers_grouped(self):
        _, out, _ = _run(query.cmd_field_access, self._args())
        r = _json(out)
        self.assertEqual({w["function"] for w in r["writers"]},
                         {"writer", "init_writer"})
        self.assertEqual({rd["function"] for rd in r["readers"]}, {"reader"})

    def test_value_filter_null_forms(self):
        """--value NULL must match the literal NULL write only."""
        _, out, _ = _run(query.cmd_field_access, self._args(value="NULL"))
        r = _json(out)
        self.assertEqual({w["function"] for w in r["writers"]}, {"writer"})
        self.assertEqual(r["readers"], [])  # reads excluded under --value
        self.assertEqual(r["value_filter"], "NULL")

    def test_unknown_field_returns_empty(self):
        _, out, _ = _run(query.cmd_field_access, self._args(field="nonexistent"))
        r = _json(out)
        self.assertEqual(r["writers"], [])
        self.assertEqual(r["readers"], [])


class TestCmdParamFlow(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph(
            [{"id": "start", "name": "start",
              "params": [{"name": "ctx", "type": "struct ctx *"}],
              "callee_args": [{"callee": "middle", "call_order": 1,
                                "args": [{"pos": 1, "value": "ctx"}]}]},
             {"id": "middle", "name": "middle",
              "params": [{"name": "ctx", "type": "struct ctx *"}],
              "callee_args": [{"callee": "end", "call_order": 1,
                                "args": [{"pos": 1, "value": "ctx->sub"}]}]},
             {"id": "end", "name": "end", "params": [{"name": "sub"}],
              "callee_args": [{"callee": "unrelated", "args": [{"pos": 1, "value": "other"}]}]},
             {"id": "unrelated", "name": "unrelated"}],
            [])

    def _args(self, **kw):
        kw.setdefault("graph", self.graph_dir)
        kw.setdefault("from_node", "start")
        kw.setdefault("param", "ctx")
        kw.setdefault("max_depth", 10)
        kw.setdefault("json", True)
        return _ns(**kw)

    def test_param_propagates_through_chain(self):
        _, out, _ = _run(query.cmd_param_flow, self._args())
        r = _json(out)
        self.assertEqual(r["start_function"], "start")
        self.assertEqual(r["total_steps"], 3)  # start, middle, end
        first = r["flow_steps"][0]
        self.assertEqual(first["function"], "start")
        self.assertEqual(first["next_hops"][0]["callee"], "middle")
        self.assertEqual(first["next_hops"][0]["arg_value"], "ctx")
        last = r["flow_steps"][-1]
        self.assertEqual(last["next_hops"], [])
        self.assertTrue(r["reached_end"])

    def test_word_boundary_match_avoids_substring_false_positive(self):
        """A param named 'ctx' must not match an arg value 'ctxx'."""
        gd = _make_graph(
            [{"id": "f", "name": "f", "params": [{"name": "ctx"}],
              "callee_args": [{"callee": "g",
                                "args": [{"pos": 1, "value": "ctxx"}]}]},
             {"id": "g", "name": "g"}], [])
        _, out, _ = _run(query.cmd_param_flow,
                         self._args(graph=gd, from_node="f"))
        r = _json(out)
        self.assertEqual(r["flow_steps"][0]["next_hops"], [])
        self.assertTrue(r["reached_end"])

    def test_missing_node_exits_1(self):
        ret, out, err, code = _capture_call(
            query.cmd_param_flow, self._args(from_node="zzz"))
        self.assertEqual(code, 1)


class TestCmdGetCodeSnippet(unittest.TestCase):
    def setUp(self):
        self.src_dir = tempfile.mkdtemp(prefix="c2d_snip_src_")
        src = os.path.join(self.src_dir, "mod.c")
        with open(src, "w") as f:
            f.write("int one(void);\n"      # 1
                    "int two(void);\n"      # 2
                    "int target(void) {\n"  # 3
                    "    return 1;\n"       # 4
                    "}\n"                   # 5
                    "int after(void);\n")   # 6
        self.graph_dir = _make_graph(
            [{"id": "t", "name": "target", "source_file": "mod.c", "line": 3,
              "signature": "int target(void)"}], [],
            master_extra={"source_root": self.src_dir})

    def _args(self, **kw):
        kw.setdefault("graph", self.graph_dir)
        kw.setdefault("node", "t")
        kw.setdefault("source", "")
        kw.setdefault("context", 1)
        kw.setdefault("persist", False)
        kw.setdefault("yes", False)
        return _ns(**kw)

    def test_snippet_reads_source_with_context(self):
        _, out, _ = _run(query.cmd_get_code_snippet, self._args())
        r = _json(out)
        self.assertEqual(r["line"], 3)
        self.assertIn("int target(void) {", r["snippet"])
        self.assertIn("int two(void);", r["snippet"])  # context line before
        # line numbers must be real file line numbers (target is line 3)
        self.assertIn("   2 | int two(void);", r["snippet"])
        self.assertIn("   3 | int target(void) {", r["snippet"])
        self.assertEqual(r["context_lines"], 1)

    def test_missing_source_file_returns_error(self):
        gd = _make_graph(
            [{"id": "t", "name": "t", "source_file": "gone.c", "line": 1}], [],
            master_extra={"source_root": self.src_dir})
        _, out, _ = _run(query.cmd_get_code_snippet, self._args(graph=gd))
        r = _json(out)
        self.assertIn("error", r)
        self.assertIn("not found", r["error"])

    def test_node_without_location_returns_error(self):
        gd = _make_graph([{"id": "t", "name": "t", "source_file": "", "line": 0}], [])
        _, out, _ = _run(query.cmd_get_code_snippet, self._args(graph=gd))
        r = _json(out)
        self.assertIn("error", r)

    def test_missing_node_exits_1(self):
        ret, out, err, code = _capture_call(
            query.cmd_get_code_snippet, self._args(node="zzz"))
        self.assertEqual(code, 1)


class TestCmdGraphProvenance(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph([], [])

    def _args(self, **kw):
        kw.setdefault("graph", self.graph_dir)
        kw.setdefault("json", True)
        return _ns(**kw)

    def test_manifest_source_commit_reported(self):
        manifest = {"source_root": "/src", "source_commit": "abc123def",
                    "files": {"a.c": 1, "b.c": 2}, "build_timestamp": "2026-01-01",
                    "schema_version": 3}
        with open(os.path.join(self.graph_dir,
                               ".code2database_manifest.json"), "w") as f:
            json.dump(manifest, f)
        _, out, _ = _run(query.cmd_graph_provenance, self._args())
        r = _json(out)
        self.assertEqual(r["source_commit"], "abc123def")
        self.assertEqual(r["file_count"], 2)

    def test_missing_manifest_exits_1(self):
        ret, out, err, code = _capture_call(
            query.cmd_graph_provenance, self._args())
        self.assertEqual(code, 1)
        self.assertIn("No manifest", err)

    def test_manifest_without_source_commit_exits_1(self):
        with open(os.path.join(self.graph_dir,
                               ".code2database_manifest.json"), "w") as f:
            json.dump({"source_root": "/src"}, f)
        ret, out, err, code = _capture_call(
            query.cmd_graph_provenance, self._args())
        self.assertEqual(code, 1)
        self.assertIn("no source_commit", err)


class TestCmdBlameNode(unittest.TestCase):
    def setUp(self):
        self.src_dir = tempfile.mkdtemp(prefix="c2d_blame_src_")  # NOT a git repo
        self.graph_dir = _make_graph(
            [{"id": "t", "name": "t", "source_file": "mod.c", "line": 3}], [],
            master_extra={"source_root": self.src_dir})
        with open(os.path.join(self.graph_dir,
                               ".code2database_manifest.json"), "w") as f:
            json.dump({"source_root": self.src_dir,
                       "source_commit": "deadbeef"}, f)

    def _args(self, **kw):
        kw.setdefault("graph", self.graph_dir)
        kw.setdefault("node", "t")
        kw.setdefault("json", True)
        return _ns(**kw)

    def test_non_git_root_falls_back_to_type_none(self):
        _, out, _ = _run(query.cmd_blame_node, self._args())
        r = _json(out)
        self.assertEqual(r["node"], "t")
        self.assertEqual(r["commit_meta"]["type"], "none")
        self.assertIn("warning", r["commit_meta"])

    def test_missing_manifest_exits_1(self):
        gd = _make_graph([{"id": "t", "name": "t"}], [])
        ret, out, err, code = _capture_call(query.cmd_blame_node,
                                            self._args(graph=gd))
        self.assertEqual(code, 1)
        self.assertIn("No manifest", err)

    def test_missing_node_exits_1(self):
        ret, out, err, code = _capture_call(
            query.cmd_blame_node, self._args(node="zzz"))
        self.assertEqual(code, 1)


class TestCmdReverseTrace(unittest.TestCase):
    def setUp(self):
        # api_entry → mid → crash ; other_entry → crash
        self.graph_dir = _make_graph(
            [{"id": "api_entry", "name": "api_entry", "labels": ["API_entry"]},
             {"id": "other_entry", "name": "other_entry"},
             {"id": "mid", "name": "mid"},
             {"id": "crash", "name": "crash"}],
            [{"source": "api_entry", "target": "mid"},
             {"source": "mid", "target": "crash"},
             {"source": "other_entry", "target": "crash",
              "call_condition": "#ifdef CONFIG_RARE"}])

    def _args(self, **kw):
        kw.setdefault("graph", self.graph_dir)
        kw.setdefault("crash_point", "crash")
        kw.setdefault("from_node", None)
        kw.setdefault("max_depth", 10)
        kw.setdefault("max_paths", 20)
        kw.setdefault("macros", "")
        kw.setdefault("json", True)
        return _ns(**kw)

    def test_crash_point_required(self):
        ret, out, err, code = _capture_call(
            query.cmd_reverse_trace, self._args(crash_point="", from_node=None))
        self.assertEqual(code, 1)
        self.assertIn("required", err)

    def test_paths_enumerated_entry_first(self):
        ret, _, _ = _run(query.cmd_reverse_trace, self._args())
        r = _json(ret)
        self.assertGreaterEqual(r["total_paths"], 2)
        first_path = r["paths"][0]
        # API_entry-originated path sorts first
        self.assertEqual(first_path["entry_point"], "api_entry")
        self.assertIn("entry_type", first_path)  # API_entry annotated
        # crash is the terminal callee of every path
        self.assertEqual(first_path["steps"][-1]["callee"], "crash")
        # first step's caller is the path entry point
        self.assertEqual(first_path["steps"][0]["caller"], "api_entry")

    def test_macro_filter_prunes_conditioned_callers(self):
        ret, _, _ = _run(query.cmd_reverse_trace, self._args(macros="CONFIG_OTHER"))
        r = _json(ret)
        for p in r["paths"]:
            callers = {s["caller"] for s in p["steps"]}
            self.assertNotIn("other_entry", callers)

    def test_missing_crash_point_exits_1(self):
        ret, out, err, code = _capture_call(
            query.cmd_reverse_trace, self._args(crash_point="zzz"))
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
