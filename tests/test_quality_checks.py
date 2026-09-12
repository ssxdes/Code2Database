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

    nodes_spec: list of dicts with keys id, name, source_file, and
                optionally body_text, labels, node_type
    edges_spec: list of dicts with keys source, target, and optionally
                relation, confidence
    """
    tmp = tempfile.mkdtemp(prefix="c2d_quality_test_")
    nodes = []
    for spec in nodes_spec:
        node = {"id": spec["id"], "name": spec.get("name", spec["id"]),
                "source_file": spec.get("source_file", "/x.c"),
                "line": spec.get("line", 1), "labels": spec.get("labels", []),
                "is_empty": False, "domain": spec.get("domain", "test"),
                "body_text": spec.get("body_text", ""),
                "node_type": spec.get("node_type", ""),
                "signature": spec.get("signature", "")}
        nodes.append(node)
    edges = []
    for spec in edges_spec:
        edges.append({"source": spec["source"], "target": spec["target"],
                      "relation": spec.get("relation", ""),
                      "confidence": spec.get("confidence", "EXTRACTED")})
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


if __name__ == "__main__":
    unittest.main()
