"""Unit tests for taint_analysis.py.

Taint analysis propagates untrusted data through DATA_FLOW/DATA_DEP/
INVOKES/FFI edges, tracking sanitizer functions on the path. A flow
is reported when source → sink is reachable; the `sanitized` flag
distinguishes safe (sanitized) from unsafe (unsanitized) flows.

Test coverage:
- fnmatch: wildcard matching (translates * to .* and ? to .)
- taint_analysis: source→sink direct flow, sanitizer on path, no
  source/sink present, max_depth cutoff, edge-type filter
- Output schema: sources, sinks, sanitizers, flows, total_flows,
  unsanitized_flows, sanitized_flows
"""
import json
import os
import tempfile
import unittest


def _make_taint_graph(nodes_spec, edges_spec) -> str:
    """Build a domain-split graph fixture from explicit node/edge lists.

    nodes_spec: list of (id, name, source_file) tuples
    edges_spec: list of (source_id, target_id, relation) tuples
    """
    tmp = tempfile.mkdtemp(prefix="c2d_taint_test_")
    domain_data = {
        "nodes": [
            {"id": nid, "name": name, "source_file": sf, "line": 1,
             "labels": [], "is_empty": False, "domain": "test"}
            for nid, name, sf in nodes_spec
        ],
        "edges": [
            {"source": src, "target": tgt, "relation": rel,
             "confidence": "EXTRACTED"}
            for src, tgt, rel in edges_spec
        ],
    }
    domain_filename = "domain_test.json"
    with open(os.path.join(tmp, domain_filename), "w") as f:
        json.dump(domain_data, f)
    master = {"source_root": "/tmp", "domains": {"test": domain_filename}}
    with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
        json.dump(master, f)
    return tmp


class TestFnmatch(unittest.TestCase):
    """Tests for the local fnmatch wildcard matcher."""

    def test_exact_match(self):
        from _builder.taint_analysis import fnmatch
        self.assertTrue(fnmatch("foo", "foo"))

    def test_star_matches_any_suffix(self):
        from _builder.taint_analysis import fnmatch
        self.assertTrue(fnmatch("recv_msg", "recv*"))
        self.assertTrue(fnmatch("recv", "recv*"))
        self.assertFalse(fnmatch("send_msg", "recv*"))

    def test_question_matches_single_char(self):
        from _builder.taint_analysis import fnmatch
        self.assertTrue(fnmatch("recv1", "recv?"))
        self.assertTrue(fnmatch("recvA", "recv?"))
        # ? matches exactly one char, not zero
        self.assertFalse(fnmatch("recv", "recv?"))
        # ? matches exactly one char, not two
        self.assertFalse(fnmatch("recv12", "recv?"))

    def test_invalid_regex_falls_back_to_exact_match(self):
        """Invalid regex pattern (unbalanced parens) falls back to == comparison."""
        from _builder.taint_analysis import fnmatch
        # Pattern with unbalanced paren would break re.fullmatch
        self.assertFalse(fnmatch("foo", "foo(bar"))
        self.assertTrue(fnmatch("foo(bar", "foo(bar"))


class TestTaintAnalysisBasic(unittest.TestCase):
    """Tests for the taint_analysis core algorithm."""

    def setUp(self):
        # Build a graph: source -> intermediate -> sink
        #  recv -> process -> memcpy
        self.graph_dir = _make_taint_graph(
            [("recv", "recv", "/x.c"), ("process", "process", "/x.c"),
             ("memcpy", "memcpy", "/x.c")],
            [("recv", "process", "DATA_FLOW"),
             ("process", "memcpy", "DATA_FLOW")],
        )

    def test_direct_source_to_sink_flow(self):
        """Source→intermediate→sink: 2-hop DATA_FLOW path is found."""
        from _builder.taint_analysis import taint_analysis
        result = taint_analysis(self.graph_dir, ["recv"], ["memcpy"])
        self.assertEqual(result["total_flows"], 1)
        flow = result["flows"][0]
        self.assertEqual(flow["source"], "recv")
        self.assertEqual(flow["sink"], "memcpy")
        self.assertFalse(flow["sanitized"])
        self.assertEqual(flow["depth"], 2)
        self.assertEqual(flow["path"], ["recv", "process", "memcpy"])

    def test_no_sink_present_returns_empty(self):
        from _builder.taint_analysis import taint_analysis
        result = taint_analysis(self.graph_dir, ["recv"], ["nonexistent_sink"])
        self.assertEqual(result["total_flows"], 0)
        self.assertEqual(result["flows"], [])

    def test_no_source_present_returns_empty(self):
        from _builder.taint_analysis import taint_analysis
        result = taint_analysis(self.graph_dir, ["nonexistent_src"], ["memcpy"])
        self.assertEqual(result["total_flows"], 0)

    def test_output_schema(self):
        """Verify the result dict has all expected keys."""
        from _builder.taint_analysis import taint_analysis
        result = taint_analysis(self.graph_dir, ["recv"], ["memcpy"])
        for key in ("sources", "sinks", "sanitizers", "flows",
                    "total_flows", "unsanitized_flows", "sanitized_flows"):
            self.assertIn(key, result, f"missing key: {key}")
        self.assertEqual(result["sources"], ["recv"])
        self.assertEqual(result["sinks"], ["memcpy"])
        self.assertEqual(result["sanitizers"], [])
        self.assertEqual(result["unsanitized_flows"], 1)
        self.assertEqual(result["sanitized_flows"], 0)


class TestTaintAnalysisSanitizer(unittest.TestCase):
    """Tests for sanitizer-on-path detection."""

    def setUp(self):
        # Two paths: source → sanitizer → sink (clean)
        #           source → bypass → sink (unsafe)
        self.graph_dir = _make_taint_graph(
            [("src", "src", "/x.c"), ("sanitize", "sanitize", "/x.c"),
             ("bypass", "bypass", "/x.c"), ("sink", "sink", "/x.c")],
            [("src", "sanitize", "DATA_FLOW"),
             ("sanitize", "sink", "DATA_FLOW"),
             ("src", "bypass", "DATA_FLOW"),
             ("bypass", "sink", "DATA_FLOW")],
        )

    def test_sanitized_path_marked_sanitized(self):
        """Flow through 'sanitize' node should be flagged sanitized."""
        from _builder.taint_analysis import taint_analysis
        result = taint_analysis(
            self.graph_dir, ["src"], ["sink"], sanitizers=["sanitize"])
        self.assertEqual(result["total_flows"], 2)
        # One sanitized, one not
        self.assertEqual(result["sanitized_flows"], 1)
        self.assertEqual(result["unsanitized_flows"], 1)
        # Find the sanitized flow
        sanitized_flows = [f for f in result["flows"] if f["sanitized"]]
        self.assertEqual(len(sanitized_flows), 1)
        self.assertIn("sanitize", sanitized_flows[0]["path"])

    def test_no_sanitizers_all_unsanitized(self):
        from _builder.taint_analysis import taint_analysis
        result = taint_analysis(self.graph_dir, ["src"], ["sink"])
        self.assertEqual(result["total_flows"], 2)
        self.assertEqual(result["unsanitized_flows"], 2)
        self.assertEqual(result["sanitized_flows"], 0)


class TestTaintAnalysisEdgeTypes(unittest.TestCase):
    """Tests for edge-type filtering (DATA_FLOW/DATA_DEP/INVOKES/FFI)."""

    def setUp(self):
        # Build a graph with mixed edge types
        self.graph_dir = _make_taint_graph(
            [("src", "src", "/x.c"), ("mid", "mid", "/x.c"),
             ("sink", "sink", "/x.c")],
            [("src", "mid", "CONTAINS"),  # NOT a taint edge
             ("mid", "sink", "INVOKES")],  # IS a taint edge
        )

    def test_only_taint_edges_followed(self):
        """CONTAINS edges should NOT propagate taint; INVOKES should."""
        from _builder.taint_analysis import taint_analysis
        result = taint_analysis(self.graph_dir, ["src"], ["sink"])
        # src → mid (CONTAINS) is NOT followed, so no path to sink via src
        # But mid → sink (INVOKES) is followed IF we reach mid somehow.
        # Since src only has a CONTAINS edge to mid, taint doesn't propagate.
        self.assertEqual(result["total_flows"], 0)


class TestTaintAnalysisMaxDepth(unittest.TestCase):
    """Tests for max_depth cutoff."""

    def test_max_depth_truncates_long_paths(self):
        from _builder.taint_analysis import taint_analysis
        # Build a 5-hop chain: a → b → c → d → e → sink
        graph_dir = _make_taint_graph(
            [("a", "a", "/x.c"), ("b", "b", "/x.c"), ("c", "c", "/x.c"),
             ("d", "d", "/x.c"), ("e", "e", "/x.c"), ("sink", "sink", "/x.c")],
            [("a", "b", "DATA_FLOW"), ("b", "c", "DATA_FLOW"),
             ("c", "d", "DATA_FLOW"), ("d", "e", "DATA_FLOW"),
             ("e", "sink", "DATA_FLOW")],
        )
        # depth=5 means 5 hops max (path length 6) → a→b→c→d→e→sink has 5 hops, just fits
        result = taint_analysis(graph_dir, ["a"], ["sink"], max_depth=5)
        self.assertEqual(result["total_flows"], 1)
        # depth=4 cuts off the 5-hop path
        result = taint_analysis(graph_dir, ["a"], ["sink"], max_depth=4)
        self.assertEqual(result["total_flows"], 0)


class TestTaintAnalysisSourceIsSink(unittest.TestCase):
    """Tests for the edge case where source == sink (excluded by `node != source_id`)."""

    def test_source_equal_to_sink_not_reported(self):
        from _builder.taint_analysis import taint_analysis
        graph_dir = _make_taint_graph(
            [("x", "x", "/x.c")],  # x is both source AND sink
            [],
        )
        result = taint_analysis(graph_dir, ["x"], ["x"])
        # source==sink should NOT be reported as a flow
        self.assertEqual(result["total_flows"], 0)


class TestTaintAnalysisWildcard(unittest.TestCase):
    """Tests for wildcard source/sink patterns."""

    def test_wildcard_source_matches_multiple(self):
        from _builder.taint_analysis import taint_analysis
        graph_dir = _make_taint_graph(
            [("recv_msg", "recv_msg", "/x.c"),
             ("recv_data", "recv_data", "/x.c"),
             ("sink", "sink", "/x.c")],
            [("recv_msg", "sink", "DATA_FLOW"),
             ("recv_data", "sink", "DATA_FLOW")],
        )
        result = taint_analysis(graph_dir, ["recv*"], ["sink"])
        self.assertEqual(result["total_flows"], 2)
        self.assertEqual(sorted(result["sources"]), ["recv_data", "recv_msg"])


if __name__ == "__main__":
    unittest.main()
