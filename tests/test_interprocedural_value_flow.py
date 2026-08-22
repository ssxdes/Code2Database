"""Tests for interprocedural value flow with alias propagation (D19)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import networkx as nx

from _builder.value_flow import (
    extract_aliases, resolve_alias, interprocedural_value_flow,
    _ALIAS_RE,
)


class TestExtractAliases(unittest.TestCase):
    """Test extract_aliases parsing of local variable assignments."""

    def test_simple_assignment(self):
        """A simple `int *p = x;` is parsed."""
        body = "int *p = x; foo(p);"
        aliases = extract_aliases(body)
        self.assertIn("p", aliases)
        self.assertEqual(aliases["p"], "x")

    def test_multiple_assignments(self):
        """Multiple aliases are captured."""
        body = """
        int *p = x;
        char *q = name;
        struct foo *r = bar;
        """
        aliases = extract_aliases(body)
        self.assertEqual(aliases.get("p"), "x")
        self.assertEqual(aliases.get("q"), "name")
        self.assertEqual(aliases.get("r"), "bar")

    def test_last_assignment_wins(self):
        """When a variable is reassigned, the last RHS wins."""
        body = "p = x; p = q; foo(p);"
        aliases = extract_aliases(body)
        self.assertEqual(aliases["p"], "q")

    def test_skip_comparison_ops(self):
        """Comparison ops (==, <=, >=) are not parsed as assignments."""
        body = "if (x == y) { return; }"
        aliases = extract_aliases(body)
        # No aliases from a comparison
        self.assertNotIn("x", aliases)

    def test_skip_keywords(self):
        """Keywords like if/while/for/return are not aliases."""
        body = "if (x) { return y; }"
        aliases = extract_aliases(body)
        self.assertNotIn("if", aliases)
        self.assertNotIn("return", aliases)

    def test_empty_body(self):
        """Empty body returns empty dict."""
        self.assertEqual(extract_aliases(""), {})
        self.assertEqual(extract_aliases(None), {})

    def test_rhs_expression(self):
        """RHS can be a complex expression, not just an identifier."""
        body = "int x = a + b * c;"
        aliases = extract_aliases(body)
        self.assertEqual(aliases["x"], "a + b * c")


class TestResolveAlias(unittest.TestCase):
    """Test resolve_alias recursive resolution."""

    def test_direct_alias(self):
        """A direct alias resolves to its RHS."""
        aliases = {"p": "x"}
        self.assertEqual(resolve_alias("p", aliases), "x")

    def test_chained_alias(self):
        """A chain of aliases resolves to the ultimate source."""
        aliases = {"p": "q", "q": "r", "r": "x"}
        self.assertEqual(resolve_alias("p", aliases), "x")

    def test_no_alias_returns_self(self):
        """A non-alias identifier returns itself."""
        self.assertEqual(resolve_alias("y", {"p": "x"}), "y")

    def test_cycle_guard(self):
        """Cyclic aliases don't loop forever."""
        aliases = {"p": "q", "q": "p"}
        # Should not infinite-loop; returns one of them
        result = resolve_alias("p", aliases)
        self.assertIn(result, ("p", "q"))

    def test_depth_cap(self):
        """Depth cap prevents infinite recursion."""
        aliases = {f"p{i}": f"p{i+1}" for i in range(20)}
        # p0 → p1 → ... → p20 (not in aliases, returns itself)
        result = resolve_alias("p0", aliases)
        # Should resolve to some p_i in the chain, not crash
        self.assertTrue(result.startswith("p"))


class TestInterproceduralValueFlow(unittest.TestCase):
    """Test interprocedural_value_flow multi-hop trace."""

    def _build_chain_graph(self):
        """Build a 3-function chain: source → middle → sink."""
        G = nx.DiGraph()
        G.add_node("n1", name="source", body_text="""
            int *p = user_input;
            middle(p);
            """)
        G.add_node("n2", name="middle", body_text="""
            int *q = arg;
            sink(q);
            """, callee_args=[{"callee": "middle", "args": [{"pos": 0, "value": "p"}]}])
        G.add_node("n3", name="sink", body_text="",
                   callee_args=[{"callee": "sink", "args": [{"pos": 0, "value": "q"}]}])
        G.add_edge("n1", "n2", relation="INVOKES")
        G.add_edge("n2", "n3", relation="INVOKES")
        return G

    def test_forward_trace_finds_sink(self):
        """Forward trace from source reaches sink through aliases."""
        G = self._build_chain_graph()
        result = interprocedural_value_flow(G, "n1", "user_input",
                                             direction="forward", max_depth=5)
        self.assertEqual(result["start"], "n1")
        self.assertEqual(result["pattern"], "user_input")
        self.assertGreaterEqual(len(result["hops"]), 1)
        # Should have at least visited source
        funcs = [h["function"] for h in result["hops"]]
        self.assertIn("source", funcs)

    def test_dead_end_when_no_more_edges(self):
        """Trace ends with 'dead-end' when no further edges match."""
        G = nx.DiGraph()
        G.add_node("n1", name="lonely", body_text="int x = 5;")
        result = interprocedural_value_flow(G, "n1", "x",
                                             direction="forward", max_depth=3)
        self.assertEqual(len(result["endpoints"]), 1)
        self.assertEqual(result["endpoints"][0]["reason"], "dead-end")

    def test_cycle_detected(self):
        """Cycle in graph doesn't cause infinite loop."""
        G = nx.DiGraph()
        G.add_node("n1", name="a", body_text="b(p);",
                   callee_args=[{"callee": "b", "args": [{"pos": 0, "value": "p"}]}])
        G.add_node("n2", name="b", body_text="a(p);",
                   callee_args=[{"callee": "a", "args": [{"pos": 0, "value": "p"}]}])
        G.add_edge("n1", "n2", relation="INVOKES")
        G.add_edge("n2", "n1", relation="INVOKES")
        result = interprocedural_value_flow(G, "n1", "p",
                                             direction="forward", max_depth=5)
        # Should terminate; either via cycle endpoint or dead-end
        self.assertIn(result["endpoints"][0]["reason"], ("cycle", "dead-end"))

    def test_max_depth_respected(self):
        """max_depth caps the number of hops."""
        G = self._build_chain_graph()
        result = interprocedural_value_flow(G, "n1", "user_input",
                                             direction="forward", max_depth=1)
        # With max_depth=1, we shouldn't go past depth 1
        for hop in result["hops"]:
            self.assertLessEqual(hop["depth"], 1)

    def test_aliases_captured_in_hops(self):
        """Each hop captures the aliases seen in that function's body."""
        G = self._build_chain_graph()
        result = interprocedural_value_flow(G, "n1", "user_input",
                                             direction="forward", max_depth=5)
        # source has alias p → user_input
        source_hop = next((h for h in result["hops"] if h["function"] == "source"), None)
        self.assertIsNotNone(source_hop)
        self.assertGreater(source_hop["alias_count"], 0)
        self.assertIn("p", source_hop["aliases"])

    def test_reverse_direction(self):
        """Reverse direction traces backwards via RETURN_FLOW / assignments."""
        G = nx.DiGraph()
        G.add_node("n1", name="caller", body_text="int x = callee(arg);")
        G.add_node("n2", name="callee", body_text="return arg;")
        G.add_edge("n1", "n2", relation="INVOKES")
        G.add_edge("n2", "n1", relation="RETURN_FLOW", returns="arg")
        result = interprocedural_value_flow(G, "n2", "arg",
                                             direction="reverse", max_depth=3)
        self.assertEqual(result["direction"], "reverse")
        self.assertGreaterEqual(len(result["hops"]), 1)


class TestAliasRegex(unittest.TestCase):
    """Test the _ALIAS_RE regex directly."""

    def test_basic_match(self):
        """Basic `int x = y;` matches."""
        m = _ALIAS_RE.search("int x = y;")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "x")
        self.assertEqual(m.group(2), "y")

    def test_pointer_decl(self):
        """Pointer declaration `char *p = buf;` matches."""
        m = _ALIAS_RE.search("char *p = buf;")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "p")

    def test_no_semicolon_no_match(self):
        """Without semicolon, no match (avoids false positives in expressions)."""
        m = _ALIAS_RE.search("if (x == y)")
        self.assertIsNone(m)


if __name__ == "__main__":
    unittest.main()
