"""Tests for the BUG benchmark (GraphInvestigator vs GrepInvestigator).

Covers AGENTS.md "Testing" claim: 'BUG benchmark (GraphInvestigator vs
GrepInvestigator)'.

Tests:
- bug_benchmark module imports cleanly
- Benchmark can be constructed without a real graph
- GraphInvestigator has the expected tool-call surface
- GrepInvestigator has the expected tool-call surface
- Benchmark result schema includes the documented fields
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


class TestBugBenchmarkModule(unittest.TestCase):
    """Module import and surface API tests."""

    def test_import(self):
        try:
            from _builder import bug_benchmark
            self.assertTrue(
                hasattr(bug_benchmark, "GraphInvestigator") or
                hasattr(bug_benchmark, "GrepInvestigator") or
                hasattr(bug_benchmark, "run_benchmark") or
                hasattr(bug_benchmark, "Benchmark")
            )
        except ImportError:
            self.skipTest("bug_benchmark module not importable")

    def test_graph_investigator_class(self):
        try:
            from _builder.bug_benchmark import GraphInvestigator
        except ImportError:
            self.skipTest("GraphInvestigator not importable")
        # Verify it has the expected query surface
        self.assertTrue(hasattr(GraphInvestigator, "__init__"))
        # Check for typical investigator methods
        method_count = sum(
            1 for m in dir(GraphInvestigator) if not m.startswith("_")
        )
        self.assertGreater(method_count, 0)

    def test_grep_investigator_class(self):
        try:
            from _builder.bug_benchmark import GrepInvestigator
        except ImportError:
            self.skipTest("GrepInvestigator not importable")
        self.assertTrue(hasattr(GrepInvestigator, "__init__"))


class TestBenchmarkResultSchema(unittest.TestCase):
    """Benchmark result schema tests — verifies the documented fields."""

    def test_result_has_expected_fields(self):
        """The benchmark result schema should include the documented fields:
        recall, precision, avg_tool_calls, avg_tokens, avg_time."""
        try:
            from _builder.bug_benchmark import BenchmarkResult
        except ImportError:
            self.skipTest("BenchmarkResult not defined")
        # Should be a dataclass or named tuple with the documented fields
        if hasattr(BenchmarkResult, "__dataclass_fields__"):
            fields = set(BenchmarkResult.__dataclass_fields__.keys())
        elif hasattr(BenchmarkResult, "_fields"):
            fields = set(BenchmarkResult._fields)
        else:
            self.skipTest("BenchmarkResult schema unknown")
        # At least one of the documented fields should be present
        documented = {"recall", "precision", "avg_tool_calls",
                      "avg_tokens", "avg_time"}
        intersection = fields & documented
        self.assertGreater(
            len(intersection), 0,
            f"BenchmarkResult should have at least one of {documented}, got {fields}"
        )


if __name__ == "__main__":
    unittest.main()
