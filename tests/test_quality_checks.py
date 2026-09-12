"""Unit tests for quality_checks.py — cycle detection.

check_cycles enumerates elementary cycles in the call graph (or
IMPORTS cycles between file nodes) with:
- exact-once reporting (DFS from each cycle's lexicographically
  smallest node, visiting only nodes that sort >= start)
- self-loops as length-1 cycles
- max-length / scope / limit filters
- truncation flag when the enumeration budget is hit

Test coverage:
- 3-node call cycle, acyclic graph, self-loop
- overlapping cycles each reported once
- max_length cutoff at exact boundary
- limit truncation + truncated flag
- scope filter (both endpoints must match)
- includes kind over IMPORTS edges between file nodes
- DATA_FLOW edges do not fabricate call cycles
- longest-first ordering
- unknown kind raises ValueError
- graph directory missing raises FileNotFoundError
"""
import json
import os
import tempfile
import unittest


def _make_quality_graph(nodes_spec, edges_spec) -> str:
    """Build a graph fixture from explicit node/edge lists.

    nodes_spec: list of dicts; id/name/source_file/body_text/labels/
                node_type/signature/domain are normalized, and any
                other keys (globals_read, thread_entry, ...) pass
                through to the node attributes verbatim
    edges_spec: list of dicts with keys source, target, and optionally
                relation, confidence, concurrency (extra keys pass
                through verbatim)
    """
    tmp = tempfile.mkdtemp(prefix="c2d_quality_test_")
    nodes = []
    _known = {"id", "name", "source_file", "line", "labels", "domain",
              "body_text", "node_type", "signature"}
    for spec in nodes_spec:
        node = {"id": spec["id"], "name": spec.get("name", spec["id"]),
                "source_file": spec.get("source_file", "/x.c"),
                "line": spec.get("line", 1), "labels": spec.get("labels", []),
                "is_empty": False, "domain": spec.get("domain", "test"),
                "body_text": spec.get("body_text", ""),
                "node_type": spec.get("node_type", ""),
                "signature": spec.get("signature", "")}
        for k, v in spec.items():
            if k not in _known:
                node[k] = v
        nodes.append(node)
    edges = []
    for spec in edges_spec:
        edges.append({"source": spec["source"], "target": spec["target"],
                      "relation": spec.get("relation", ""),
                      "confidence": spec.get("confidence", "EXTRACTED"),
                      "concurrency": spec.get("concurrency", "")})
    domain_data = {"nodes": nodes, "edges": edges}
    domain_filename = "domain_test.json"
    with open(os.path.join(tmp, domain_filename), "w") as f:
        json.dump(domain_data, f)
    master = {"source_root": "/tmp", "domains": {"test": domain_filename}}
    with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
        json.dump(master, f)
    return tmp


def _names(result):
    return sorted(tuple(c["names"]) for c in result["cycles"])


class TestCheckCycles(unittest.TestCase):

    def test_three_node_call_cycle(self):
        from _builder.analysis.quality_checks import check_cycles
        g = _make_quality_graph(
            [{"id": "a", "name": "fn_a"}, {"id": "b", "name": "fn_b"},
             {"id": "c", "name": "fn_c"}],
            [{"source": "a", "target": "b"}, {"source": "b", "target": "c"},
             {"source": "c", "target": "a"}],
        )
        result = check_cycles(g)
        self.assertEqual(result["total_cycles"], 1)
        self.assertEqual(result["kind"], "calls")
        cyc = result["cycles"][0]
        self.assertEqual(cyc["length"], 3)
        self.assertEqual(cyc["nodes"], ["a", "b", "c"])
        self.assertEqual(cyc["names"], ["fn_a", "fn_b", "fn_c"])
        self.assertFalse(result["truncated"])

    def test_acyclic_graph_reports_nothing(self):
        from _builder.analysis.quality_checks import check_cycles
        g = _make_quality_graph(
            [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            [{"source": "a", "target": "b"}, {"source": "b", "target": "c"}],
        )
        result = check_cycles(g)
        self.assertEqual(result["total_cycles"], 0)
        self.assertEqual(result["cycles"], [])

    def test_self_loop_is_length_one_cycle(self):
        from _builder.analysis.quality_checks import check_cycles
        g = _make_quality_graph(
            [{"id": "a"}, {"id": "b"}],
            [{"source": "a", "target": "a"}, {"source": "a", "target": "b"}],
        )
        result = check_cycles(g)
        self.assertEqual(result["total_cycles"], 1)
        self.assertEqual(result["cycles"][0]["length"], 1)
        self.assertEqual(result["cycles"][0]["nodes"], ["a"])

    def test_overlapping_cycles_each_reported_once(self):
        """a->b->c->a and b->d->b share node b; both must appear once."""
        from _builder.analysis.quality_checks import check_cycles
        g = _make_quality_graph(
            [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}],
            [{"source": "a", "target": "b"}, {"source": "b", "target": "c"},
             {"source": "c", "target": "a"}, {"source": "b", "target": "d"},
             {"source": "d", "target": "b"}],
        )
        result = check_cycles(g)
        self.assertEqual(result["total_cycles"], 2)
        self.assertEqual(_names(result), [("a", "b", "c"), ("b", "d")])

    def test_max_length_boundary(self):
        """A 4-node cycle needs max_length >= 4 to be reported."""
        from _builder.analysis.quality_checks import check_cycles
        g = _make_quality_graph(
            [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}],
            [{"source": "a", "target": "b"}, {"source": "b", "target": "c"},
             {"source": "c", "target": "d"}, {"source": "d", "target": "a"}],
        )
        self.assertEqual(check_cycles(g, max_length=3)["total_cycles"], 0)
        result = check_cycles(g, max_length=4)
        self.assertEqual(result["total_cycles"], 1)
        self.assertEqual(result["cycles"][0]["length"], 4)

    def test_limit_truncation(self):
        from _builder.analysis.quality_checks import check_cycles
        g = _make_quality_graph(
            [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            [{"source": "a", "target": "a"}, {"source": "b", "target": "b"},
             {"source": "c", "target": "c"}],
        )
        result = check_cycles(g, limit=2)
        self.assertEqual(result["total_cycles"], 2)
        self.assertTrue(result["truncated"])

    def test_scope_filters_both_endpoints(self):
        from _builder.analysis.quality_checks import check_cycles
        g = _make_quality_graph(
            [{"id": "a", "name": "kernel_start"},
             {"id": "b", "name": "kernel_stop"},
             {"id": "c", "name": "userspace_run"},
             {"id": "d", "name": "userspace_walk"}],
            [{"source": "a", "target": "b"}, {"source": "b", "target": "a"},
             {"source": "c", "target": "d"}, {"source": "d", "target": "c"}],
        )
        result = check_cycles(g, scope="kernel")
        self.assertEqual(result["total_cycles"], 1)
        self.assertEqual(result["cycles"][0]["nodes"], ["a", "b"])

    def test_scope_matches_source_file(self):
        from _builder.analysis.quality_checks import check_cycles
        g = _make_quality_graph(
            [{"id": "a", "source_file": "/src/driver.c"},
             {"id": "b", "source_file": "/src/driver.c"},
             {"id": "c", "source_file": "/lib/util.c"},
             {"id": "d", "source_file": "/lib/util.c"}],
            [{"source": "a", "target": "b"}, {"source": "b", "target": "a"},
             {"source": "c", "target": "d"}, {"source": "d", "target": "c"}],
        )
        result = check_cycles(g, scope="driver")
        self.assertEqual(result["total_cycles"], 1)
        self.assertEqual(result["cycles"][0]["nodes"], ["a", "b"])

    def test_includes_kind_over_imports_edges(self):
        from _builder.analysis.quality_checks import check_cycles
        g = _make_quality_graph(
            [{"id": "f_hdr_a", "name": "a.h", "node_type": "file",
              "labels": ["file"]},
             {"id": "f_hdr_b", "name": "b.h", "node_type": "file",
              "labels": ["file"]},
             {"id": "fn_x", "name": "fn_x"}],
            [{"source": "f_hdr_a", "target": "f_hdr_b", "relation": "IMPORTS"},
             {"source": "f_hdr_b", "target": "f_hdr_a", "relation": "IMPORTS"},
             {"source": "fn_x", "target": "f_hdr_a", "relation": "CONTAINS"}],
        )
        result = check_cycles(g, kind="includes")
        self.assertEqual(result["kind"], "includes")
        self.assertEqual(result["total_cycles"], 1)
        self.assertEqual(result["cycles"][0]["length"], 2)

    def test_data_flow_edges_do_not_count_as_calls(self):
        from _builder.analysis.quality_checks import check_cycles
        g = _make_quality_graph(
            [{"id": "a"}, {"id": "b"}],
            [{"source": "a", "target": "b", "relation": "DATA_FLOW"},
             {"source": "b", "target": "a", "relation": "DATA_DEP"}],
        )
        result = check_cycles(g)
        self.assertEqual(result["total_cycles"], 0)

    def test_dispatch_edges_count_as_calls(self):
        from _builder.analysis.quality_checks import check_cycles
        g = _make_quality_graph(
            [{"id": "a"}, {"id": "b"}],
            [{"source": "a", "target": "b", "relation": "DISPATCH"},
             {"source": "b", "target": "a", "relation": ""}],
        )
        result = check_cycles(g)
        self.assertEqual(result["total_cycles"], 1)

    def test_longest_cycles_first(self):
        from _builder.analysis.quality_checks import check_cycles
        g = _make_quality_graph(
            [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"},
             {"id": "e"}],
            [{"source": "a", "target": "b"}, {"source": "b", "target": "a"},
             {"source": "a", "target": "c"}, {"source": "c", "target": "d"},
             {"source": "d", "target": "e"}, {"source": "e", "target": "a"}],
        )
        result = check_cycles(g)
        lengths = [c["length"] for c in result["cycles"]]
        self.assertEqual(lengths, sorted(lengths, reverse=True))
        self.assertEqual(lengths[0], 4)

    def test_unknown_kind_raises(self):
        from _builder.analysis.quality_checks import check_cycles
        g = _make_quality_graph([], [])
        with self.assertRaises(ValueError):
            check_cycles(g, kind="nonsense")

    def test_missing_graph_dir_raises(self):
        from _builder.analysis.quality_checks import check_cycles
        with self.assertRaises(FileNotFoundError):
            check_cycles("/nonexistent_graph_dir_xyz")

    def test_result_is_json_serializable(self):
        from _builder.analysis.quality_checks import check_cycles
        g = _make_quality_graph(
            [{"id": "a"}, {"id": "b"}],
            [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
        )
        result = check_cycles(g)
        json.dumps(result)


class TestCheckCyclesCLI(unittest.TestCase):

    def test_cli_prints_json(self):
        from _builder.analysis.quality_checks import cmd_check_cycles
        import io
        from contextlib import redirect_stdout
        g = _make_quality_graph(
            [{"id": "a"}, {"id": "b"}],
            [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
        )

        class Args:
            graph = g
            kind = "calls"
            max_length = 10
            scope = None
            limit = 50

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_check_cycles(Args())
        data = json.loads(buf.getvalue())
        self.assertEqual(data["total_cycles"], 1)


class TestCheckRecursion(unittest.TestCase):

    def test_direct_recursion_with_base_case_is_safe(self):
        from _builder.analysis.quality_checks import check_recursion
        body = ("int fact(int n) {\n"
                "  if (n <= 1)\n"
                "    return 1;\n"
                "  return fact(n - 1);\n"
                "}\n")
        g = _make_quality_graph(
            [{"id": "a", "name": "fact", "body_text": body}],
            [{"source": "a", "target": "a"}],
        )
        result = check_recursion(g)
        self.assertEqual(result["total_recursive_functions"], 1)
        f = result["findings"][0]
        self.assertEqual(f["kind"], "direct")
        self.assertEqual(f["termination"], "safe")
        self.assertTrue(f["has_base_return"])
        self.assertEqual(f["recursive_call_lines"], [4])
        self.assertEqual(f["guarded_call_lines"], [4])

    def test_unconditional_recursion_is_risky(self):
        from _builder.analysis.quality_checks import check_recursion
        body = ("void spin(void) {\n"
                "  spin();\n"
                "}\n")
        g = _make_quality_graph(
            [{"id": "a", "name": "spin", "body_text": body}],
            [{"source": "a", "target": "a"}],
        )
        result = check_recursion(g)
        f = result["findings"][0]
        self.assertEqual(f["termination"], "risky")
        self.assertEqual(f["guarded_call_lines"], [])

    def test_conditional_recursion_without_base_is_caution(self):
        from _builder.analysis.quality_checks import check_recursion
        body = ("void walk(int n) {\n"
                "  if (n > 0)\n"
                "    walk(n - 1);\n"
                "}\n")
        g = _make_quality_graph(
            [{"id": "a", "name": "walk", "body_text": body}],
            [{"source": "a", "target": "a"}],
        )
        result = check_recursion(g)
        f = result["findings"][0]
        self.assertEqual(f["termination"], "caution")
        self.assertFalse(f["has_base_return"])
        self.assertEqual(f["guarded_call_lines"], [3])

    def test_missing_body_is_unknown(self):
        from _builder.analysis.quality_checks import check_recursion
        g = _make_quality_graph(
            [{"id": "a", "name": "opaque"}],
            [{"source": "a", "target": "a"}],
        )
        result = check_recursion(g)
        self.assertEqual(result["findings"][0]["termination"], "unknown")

    def test_indirect_recursion_reports_both_functions(self):
        from _builder.analysis.quality_checks import check_recursion
        body_a = ("int is_even(int n) {\n"
                  "  if (n == 0)\n"
                  "    return 1;\n"
                  "  return is_odd(n - 1);\n"
                  "}\n")
        body_b = ("int is_odd(int n) {\n"
                  "  if (n == 0)\n"
                  "    return 0;\n"
                  "  return is_even(n - 1);\n"
                  "}\n")
        g = _make_quality_graph(
            [{"id": "a", "name": "is_even", "body_text": body_a},
             {"id": "b", "name": "is_odd", "body_text": body_b}],
            [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
        )
        result = check_recursion(g)
        self.assertEqual(result["total_recursive_functions"], 2)
        self.assertEqual(result["indirect"], 2)
        self.assertEqual(result["direct"], 0)
        for f in result["findings"]:
            self.assertEqual(f["kind"], "indirect")
            self.assertEqual(f["termination"], "safe")
            self.assertEqual(sorted(f["cycle"]), ["is_even", "is_odd"])

    def test_risky_findings_sort_first(self):
        from _builder.analysis.quality_checks import check_recursion
        body_safe = ("int f(int n) {\n  if (n)\n    return f(n-1);\n"
                     "  return 0;\n}\n")
        body_risky = "void g(void) {\n  g();\n}\n"
        g = _make_quality_graph(
            [{"id": "f", "name": "f", "body_text": body_safe},
             {"id": "g", "name": "g", "body_text": body_risky}],
            [{"source": "f", "target": "f"}, {"source": "g", "target": "g"}],
        )
        result = check_recursion(g)
        self.assertEqual([f["termination"] for f in result["findings"]],
                         ["risky", "safe"])

    def test_brace_nesting_conditional_tracking(self):
        """Recursive call nested in plain block inside an if is guarded."""
        from _builder.analysis.quality_checks import check_recursion
        body = ("void h(int n) {\n"
                "  if (n > 0) {\n"
                "    {\n"
                "      h(n - 1);\n"
                "    }\n"
                "  }\n"
                "  return;\n"
                "}\n")
        g = _make_quality_graph(
            [{"id": "a", "name": "h", "body_text": body}],
            [{"source": "a", "target": "a"}],
        )
        result = check_recursion(g)
        f = result["findings"][0]
        self.assertEqual(f["termination"], "safe")
        self.assertEqual(f["recursive_call_lines"], [4])
        self.assertEqual(f["guarded_call_lines"], [4])

    def test_short_circuit_guard_counts(self):
        from _builder.analysis.quality_checks import check_recursion
        body = ("int k(int n) {\n"
                "  return n > 0 && k(n - 1);\n"
                "}\n")
        g = _make_quality_graph(
            [{"id": "a", "name": "k", "body_text": body}],
            [{"source": "a", "target": "a"}],
        )
        result = check_recursion(g)
        f = result["findings"][0]
        self.assertEqual(f["guarded_call_lines"], [2])
        self.assertNotEqual(f["termination"], "risky")

    def test_while_loop_header_guards_body(self):
        from _builder.analysis.quality_checks import check_recursion
        body = ("void w(int n) {\n"
                "  while (n > 0) {\n"
                "    w(n - 1);\n"
                "  }\n"
                "  return;\n"
                "}\n")
        g = _make_quality_graph(
            [{"id": "a", "name": "w", "body_text": body}],
            [{"source": "a", "target": "a"}],
        )
        result = check_recursion(g)
        f = result["findings"][0]
        self.assertEqual(f["guarded_call_lines"], [3])
        self.assertEqual(f["termination"], "safe")

    def test_scope_filter(self):
        from _builder.analysis.quality_checks import check_recursion
        g = _make_quality_graph(
            [{"id": "a", "name": "keep_me"}, {"id": "b", "name": "drop_me"}],
            [{"source": "a", "target": "a"}, {"source": "b", "target": "b"}],
        )
        result = check_recursion(g, scope="keep")
        self.assertEqual(result["total_recursive_functions"], 1)
        self.assertEqual(result["findings"][0]["name"], "keep_me")

    def test_data_flow_self_loop_is_not_recursion(self):
        from _builder.analysis.quality_checks import check_recursion
        g = _make_quality_graph(
            [{"id": "a"}],
            [{"source": "a", "target": "a", "relation": "DATA_FLOW"}],
        )
        result = check_recursion(g)
        self.assertEqual(result["total_recursive_functions"], 0)

    def test_result_is_json_serializable(self):
        from _builder.analysis.quality_checks import check_recursion
        body = "void z(void) {\n  z();\n}\n"
        g = _make_quality_graph(
            [{"id": "a", "name": "z", "body_text": body}],
            [{"source": "a", "target": "a"}],
        )
        json.dumps(check_recursion(g))


class TestCheckBounds(unittest.TestCase):

    def test_unguarded_variable_index_is_risky(self):
        from _builder.analysis.quality_checks import check_bounds
        body = ("int get(int *buf, int i) {\n"
                "  return buf[i];\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "get", "body_text": body}], [])
        result = check_bounds(g)
        self.assertEqual(result["risky"], 1)
        f = result["findings"][0]
        self.assertEqual((f["var"], f["index"]), ("buf", "i"))
        self.assertEqual(f["line"], 2)
        self.assertIsNone(f["guard_line"])

    def test_preceding_if_guard_makes_safe(self):
        from _builder.analysis.quality_checks import check_bounds
        body = ("int get(int *buf, int i) {\n"
                "  if (i >= 32)\n"
                "    return -1;\n"
                "  return buf[i];\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "get", "body_text": body}], [])
        result = check_bounds(g)
        self.assertEqual(result["safe"], 1)
        self.assertEqual(result["risky"], 0)
        f = result["findings"][0]
        self.assertEqual(f["guard_kind"], "condition")
        self.assertEqual(f["guard_line"], 2)

    def test_assert_guard_makes_safe(self):
        from _builder.analysis.quality_checks import check_bounds
        body = ("int get(int *buf, int i) {\n"
                "  assert(i < 32);\n"
                "  return buf[i];\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "get", "body_text": body}], [])
        result = check_bounds(g)
        self.assertEqual(result["safe"], 1)

    def test_guard_for_different_variable_does_not_count(self):
        from _builder.analysis.quality_checks import check_bounds
        body = ("int get(int *buf, int i, int j) {\n"
                "  if (j >= 32)\n"
                "    return -1;\n"
                "  return buf[i];\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "get", "body_text": body}], [])
        result = check_bounds(g)
        self.assertEqual(result["risky"], 1)

    def test_single_letter_index_does_not_match_if_keyword(self):
        """`i` must not be considered guarded by `if (j < n)` line."""
        from _builder.analysis.quality_checks import check_bounds
        body = ("int get(int *b, int i) {\n"
                "  return b[i];\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "get", "body_text": body}], [])
        result = check_bounds(g)
        self.assertEqual(result["risky"], 1)

    def test_constant_indices_skipped(self):
        from _builder.analysis.quality_checks import check_bounds
        body = ("int get(int *buf) {\n"
                "  int x = buf[0] + buf[1] + buf[0x10];\n"
                "  x += buf[sizeof(int)];\n"
                "  x += buf[MAX_LEN];\n"
                "  return x;\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "get", "body_text": body}], [])
        result = check_bounds(g)
        self.assertEqual(result["total_accesses"], 0)

    def test_string_key_skipped(self):
        from _builder.analysis.quality_checks import check_bounds
        body = ("int get(config_t *c) {\n"
                "  return c->table[\"mode\"];\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "get", "body_text": body}], [])
        result = check_bounds(g)
        self.assertEqual(result["total_accesses"], 0)

    def test_map_variable_skipped(self):
        from _builder.analysis.quality_checks import check_bounds
        body = ("int get(void) {\n"
                "  std::map<int,int> m;\n"
                "  return m[k];\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "get", "body_text": body}], [])
        result = check_bounds(g)
        self.assertEqual(result["total_accesses"], 0)

    def test_comment_lines_ignored(self):
        from _builder.analysis.quality_checks import check_bounds
        body = ("int get(int *buf, int i) {\n"
                "  // buf[i] legacy access\n"
                "  /* buf[i] also comment */\n"
                "  return 0;\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "get", "body_text": body}], [])
        result = check_bounds(g)
        self.assertEqual(result["total_accesses"], 0)

    def test_guard_outside_window_counts_as_risky(self):
        from _builder.analysis.quality_checks import check_bounds
        lines = ["int get(int *buf, int i) {", "  if (i < 32)", "    return -1;"]
        lines += ["  int pad = 0;"] * 25
        lines += ["  return buf[i];", "}"]
        body = "\n".join(lines) + "\n"
        g = _make_quality_graph([{"id": "a", "name": "get", "body_text": body}], [])
        result = check_bounds(g, window=10)
        self.assertEqual(result["risky"], 1)

    def test_risky_sorted_first(self):
        from _builder.analysis.quality_checks import check_bounds
        body_risky = "int a1(int *buf, int i) {\n  return buf[i];\n}\n"
        body_safe = ("int a2(int *buf, int i) {\n"
                     "  if (i < 32) return -1;\n"
                     "  return buf[i];\n}\n")
        g = _make_quality_graph(
            [{"id": "a", "name": "a1", "source_file": "/a.c", "body_text": body_risky},
             {"id": "b", "name": "a2", "source_file": "/a.c", "body_text": body_safe}], [])
        result = check_bounds(g)
        self.assertEqual([f["classification"] for f in result["findings"]],
                         ["risky", "safe"])

    def test_limit_truncates(self):
        from _builder.analysis.quality_checks import check_bounds
        body = "int get(int *buf, int i, int j) {\n  return buf[i] + buf[j];\n}\n"
        g = _make_quality_graph([{"id": "a", "name": "get", "body_text": body}], [])
        result = check_bounds(g, limit=1)
        self.assertEqual(result["total_accesses"], 1)
        self.assertTrue(result["truncated"])

    def test_scope_filter(self):
        from _builder.analysis.quality_checks import check_bounds
        body = "int f(int *buf, int i) {\n  return buf[i];\n}\n"
        g = _make_quality_graph(
            [{"id": "a", "name": "keep_me", "body_text": body},
             {"id": "b", "name": "drop_me", "body_text": body}], [])
        result = check_bounds(g, scope="keep")
        self.assertEqual(result["total_accesses"], 1)
        self.assertEqual(result["findings"][0]["name"], "keep_me")

    def test_safe_access_pattern(self):
        from _builder.analysis.quality_checks import check_bounds
        body = ("int get(int *buf, int i) {\n"
                "  int v = lookup.at(i);\n"
                "  return buf[i] + v;\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "get", "body_text": body}], [])
        result = check_bounds(g)
        self.assertEqual(result["safe"], 1)
        self.assertEqual(result["findings"][0]["guard_kind"], "safe_access")

    def test_result_is_json_serializable(self):
        from _builder.analysis.quality_checks import check_bounds
        body = "int get(int *buf, int i) {\n  return buf[i];\n}\n"
        g = _make_quality_graph([{"id": "a", "name": "get", "body_text": body}], [])
        json.dumps(check_bounds(g))


class TestCheckInfiniteLoop(unittest.TestCase):

    def test_while_true_with_break_is_safe(self):
        from _builder.analysis.quality_checks import check_infinite_loop
        body = ("void pump(void) {\n"
                "  while (true) {\n"
                "    if (queue_empty())\n"
                "      break;\n"
                "  }\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "pump", "body_text": body}], [])
        result = check_infinite_loop(g)
        self.assertEqual(result["total_loops"], 1)
        f = result["findings"][0]
        self.assertEqual(f["pattern"], "while_true")
        self.assertEqual(f["classification"], "safe")
        self.assertIn("break", f["exits"])
        self.assertFalse(f["unparsed"])
        self.assertEqual(f["line"], 2)

    def test_while_true_without_exit_is_risky(self):
        from _builder.analysis.quality_checks import check_infinite_loop
        body = ("void spin(void) {\n"
                "  while (true) {\n"
                "    do_work();\n"
                "  }\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "spin", "body_text": body}], [])
        result = check_infinite_loop(g)
        self.assertEqual(result["risky"], 1)
        f = result["findings"][0]
        self.assertEqual(f["exits"], [])
        self.assertEqual(f["classification"], "risky")

    def test_for_empty_header(self):
        from _builder.analysis.quality_checks import check_infinite_loop
        body = ("void loop(void) {\n"
                "  for (;;) {\n"
                "    if (done())\n"
                "      return;\n"
                "  }\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "loop", "body_text": body}], [])
        result = check_infinite_loop(g)
        self.assertEqual(result["total_loops"], 1)
        f = result["findings"][0]
        self.assertEqual(f["pattern"], "for_empty")
        self.assertIn("return", f["exits"])

    def test_do_while_true(self):
        from _builder.analysis.quality_checks import check_infinite_loop
        body = ("void retry(void) {\n"
                "  do {\n"
                "    if (try_send())\n"
                "      break;\n"
                "  } while (1);\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "retry", "body_text": body}], [])
        result = check_infinite_loop(g)
        self.assertEqual(result["total_loops"], 1)
        f = result["findings"][0]
        self.assertEqual(f["pattern"], "do_while_true")
        self.assertIn("break", f["exits"])

    def test_do_while_false_condition_not_reported(self):
        from _builder.analysis.quality_checks import check_infinite_loop
        body = ("void once(void) {\n"
                "  do {\n"
                "    step();\n"
                "  } while (0);\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "once", "body_text": body}], [])
        result = check_infinite_loop(g)
        self.assertEqual(result["total_loops"], 0)

    def test_normal_condition_loop_not_reported(self):
        from _builder.analysis.quality_checks import check_infinite_loop
        body = ("void count(void) {\n"
                "  while (n < 10) {\n"
                "    n++;\n"
                "  }\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "count", "body_text": body}], [])
        result = check_infinite_loop(g)
        self.assertEqual(result["total_loops"], 0)

    def test_single_statement_body(self):
        from _builder.analysis.quality_checks import check_infinite_loop
        body = ("void idle(void) {\n"
                "  while (1) poll();\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "idle", "body_text": body}], [])
        result = check_infinite_loop(g)
        self.assertEqual(result["total_loops"], 1)
        self.assertEqual(result["risky"], 1)

    def test_single_statement_body_with_return(self):
        from _builder.analysis.quality_checks import check_infinite_loop
        body = ("void guard(void) {\n"
                "  while (1) return;\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "guard", "body_text": body}], [])
        result = check_infinite_loop(g)
        self.assertEqual(result["safe"], 1)

    def test_goto_and_throw_counted(self):
        from _builder.analysis.quality_checks import check_infinite_loop
        body = ("void g1(void) {\n"
                "  while (true) {\n"
                "    if (x) goto out;\n"
                "  }\n"
                "out:;\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "g1", "body_text": body}], [])
        result = check_infinite_loop(g)
        self.assertEqual(result["safe"], 1)
        self.assertIn("goto", result["findings"][0]["exits"])

    def test_braces_in_string_literals_do_not_break_extraction(self):
        from _builder.analysis.quality_checks import check_infinite_loop
        body = ("void s(void) {\n"
                "  while (true) {\n"
                "    log(\"}{ tricky\");\n"
                "    if (x) break;\n"
                "  }\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "s", "body_text": body}], [])
        result = check_infinite_loop(g)
        self.assertEqual(result["safe"], 1)

    def test_commented_loop_ignored(self):
        from _builder.analysis.quality_checks import check_infinite_loop
        body = ("void c(void) {\n"
                "  // while (true) { do_work(); }\n"
                "  /* while (1) */\n"
                "  return;\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "c", "body_text": body}], [])
        result = check_infinite_loop(g)
        self.assertEqual(result["total_loops"], 0)

    def test_unbalanced_braces_reported_safe_unparsed(self):
        from _builder.analysis.quality_checks import check_infinite_loop
        body = "void u(void) {\n  while (true) {\n    do_work();\n"
        g = _make_quality_graph([{"id": "a", "name": "u", "body_text": body}], [])
        result = check_infinite_loop(g)
        self.assertEqual(result["total_loops"], 1)
        f = result["findings"][0]
        self.assertTrue(f["unparsed"])
        self.assertEqual(f["classification"], "safe")

    def test_continue_flagged_but_not_exit(self):
        from _builder.analysis.quality_checks import check_infinite_loop
        body = ("void cc(void) {\n"
                "  while (1) {\n"
                "    continue;\n"
                "  }\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "cc", "body_text": body}], [])
        result = check_infinite_loop(g)
        f = result["findings"][0]
        self.assertTrue(f["has_continue"])
        self.assertEqual(f["classification"], "risky")

    def test_multiple_loops_and_risky_first_sorting(self):
        from _builder.analysis.quality_checks import check_infinite_loop
        body = ("void m(void) {\n"
                "  while (true) {\n"
                "    if (a) break;\n"
                "  }\n"
                "  while (1) {\n"
                "    work();\n"
                "  }\n"
                "}\n")
        g = _make_quality_graph([{"id": "a", "name": "m", "body_text": body}], [])
        result = check_infinite_loop(g)
        self.assertEqual(result["total_loops"], 2)
        self.assertEqual([f["classification"] for f in result["findings"]],
                         ["risky", "safe"])

    def test_scope_filter(self):
        from _builder.analysis.quality_checks import check_infinite_loop
        body = "void f(void) {\n  while (1) {}\n}\n"
        g = _make_quality_graph(
            [{"id": "a", "name": "keep_me", "body_text": body},
             {"id": "b", "name": "drop_me", "body_text": body}], [])
        result = check_infinite_loop(g, scope="keep")
        self.assertEqual(result["total_loops"], 1)
        self.assertEqual(result["findings"][0]["name"], "keep_me")

    def test_result_is_json_serializable(self):
        from _builder.analysis.quality_checks import check_infinite_loop
        body = "void f(void) {\n  while (1) {}\n}\n"
        g = _make_quality_graph([{"id": "a", "name": "f", "body_text": body}], [])
        json.dumps(check_infinite_loop(g))


class TestCheckClones(unittest.TestCase):

    CLONE_BODY = ("int calc(int a, int b) {\n"
                  "  int total = 0;\n"
                  "  for (int i = 0; i < b; i++) {\n"
                  "    total += a * factor[i];\n"
                  "  }\n"
                  "  if (total > ceiling) {\n"
                  "    total = ceiling;\n"
                  "  }\n"
                  "  return total;\n"
                  "}\n")

    def test_identical_bodies_detected(self):
        from _builder.analysis.quality_checks import check_clones
        g = _make_quality_graph(
            [{"id": "a", "name": "calc_v1", "source_file": "/x.c",
              "body_text": self.CLONE_BODY},
             {"id": "b", "name": "calc_v2", "source_file": "/y.c",
              "body_text": self.CLONE_BODY},
             {"id": "c", "name": "unrelated", "source_file": "/z.c",
              "body_text": "int unrelated(void) {\n  return 42;\n}\n"}],
            [])
        result = check_clones(g)
        self.assertEqual(result["clone_groups"], 1)
        group = result["groups"][0]
        self.assertEqual(group["size"], 2)
        names = sorted(m["name"] for m in group["members"])
        self.assertEqual(names, ["calc_v1", "calc_v2"])
        self.assertGreaterEqual(group["similarity"], 0.95)
        self.assertEqual(result["total_functions_scanned"], 2)

    def test_whitespace_and_comment_differences_still_clone(self):
        from _builder.analysis.quality_checks import check_clones
        variant = ("int calc(int a, int b) {\n"
                   "  // leading remark\n"
                   "  int total = 0;\n"
                   "\n"
                   "  for (int i = 0; i < b; i++) {\n"
                   "    total += a * factor[i];\n"
                   "  }\n"
                   "  /* clamp */\n"
                   "  if (total > ceiling) {\n"
                   "    total = ceiling;\n"
                   "  }\n"
                   "  return total;\n"
                   "}\n")
        g = _make_quality_graph(
            [{"id": "a", "name": "f1", "body_text": self.CLONE_BODY},
             {"id": "b", "name": "f2", "body_text": variant}], [])
        result = check_clones(g)
        self.assertEqual(result["clone_groups"], 1)

    def test_different_bodies_not_paired(self):
        from _builder.analysis.quality_checks import check_clones
        other = ("int other(int a, int b) {\n"
                 "  int diff = a - b;\n"
                 "  while (diff > 0) {\n"
                 "    diff -= step_size;\n"
                 "  }\n"
                 "  if (diff < floor_val) {\n"
                 "    diff = floor_val;\n"
                 "  }\n"
                 "  return diff;\n"
                 "}\n")
        g = _make_quality_graph(
            [{"id": "a", "name": "f1", "body_text": self.CLONE_BODY},
             {"id": "b", "name": "f2", "body_text": other}], [])
        result = check_clones(g)
        self.assertEqual(result["clone_groups"], 0)

    def test_min_lines_filter(self):
        from _builder.analysis.quality_checks import check_clones
        tiny = "int t(void) {\n  return 1;\n}\n"
        g = _make_quality_graph(
            [{"id": "a", "name": "t1", "body_text": tiny},
             {"id": "b", "name": "t2", "body_text": tiny}], [])
        result = check_clones(g)
        self.assertEqual(result["total_functions_scanned"], 0)
        self.assertEqual(result["clone_groups"], 0)

    def test_three_way_clone_group(self):
        from _builder.analysis.quality_checks import check_clones
        g = _make_quality_graph(
            [{"id": "a", "name": "m1", "body_text": self.CLONE_BODY},
             {"id": "b", "name": "m2", "body_text": self.CLONE_BODY},
             {"id": "c", "name": "m3", "body_text": self.CLONE_BODY}], [])
        result = check_clones(g)
        self.assertEqual(result["clone_groups"], 1)
        self.assertEqual(result["groups"][0]["size"], 3)

    def test_threshold_gate(self):
        from _builder.analysis.quality_checks import check_clones
        g = _make_quality_graph(
            [{"id": "a", "name": "m1", "body_text": self.CLONE_BODY},
             {"id": "b", "name": "m2", "body_text": self.CLONE_BODY}], [])
        result = check_clones(g, threshold=1.01)
        self.assertEqual(result["clone_groups"], 0)

    def test_bucket_cap_suppresses_boilerplate(self):
        from _builder.analysis.quality_checks import check_clones
        nodes = [{"id": "n%03d" % i, "name": "dup%03d" % i,
                  "body_text": self.CLONE_BODY} for i in range(250)]
        g = _make_quality_graph(nodes, [])
        result = check_clones(g)
        self.assertEqual(result["clone_groups"], 0)

    def test_scope_filter(self):
        from _builder.analysis.quality_checks import check_clones
        g = _make_quality_graph(
            [{"id": "a", "name": "keep_me", "body_text": self.CLONE_BODY},
             {"id": "b", "name": "keep_too", "body_text": self.CLONE_BODY},
             {"id": "c", "name": "drop_me", "body_text": self.CLONE_BODY}], [])
        result = check_clones(g, scope="keep")
        self.assertEqual(result["groups"][0]["size"], 2)

    def test_limit_truncates_groups(self):
        from _builder.analysis.quality_checks import check_clones
        other = ("int alt(int q, int r) {\n"
                 "  int acc = 0;\n"
                 "  for (int j = 0; j < r; j++) {\n"
                 "    acc += q * weights[j];\n"
                 "  }\n"
                 "  if (acc > cap) {\n"
                 "    acc = cap;\n"
                 "  }\n"
                 "  return acc;\n"
                 "}\n")
        g = _make_quality_graph(
            [{"id": "a", "name": "g1a", "body_text": self.CLONE_BODY},
             {"id": "b", "name": "g1b", "body_text": self.CLONE_BODY},
             {"id": "c", "name": "g2a", "body_text": other},
             {"id": "d", "name": "g2b", "body_text": other}], [])
        result = check_clones(g, limit=1)
        self.assertEqual(result["clone_groups"], 1)
        self.assertTrue(result["truncated"])

    def test_result_is_json_serializable(self):
        from _builder.analysis.quality_checks import check_clones
        g = _make_quality_graph(
            [{"id": "a", "name": "f1", "body_text": self.CLONE_BODY},
             {"id": "b", "name": "f2", "body_text": self.CLONE_BODY}], [])
        json.dumps(check_clones(g))


if __name__ == "__main__":
    unittest.main()
