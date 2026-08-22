"""Tests for dynamic LLM context budget allocation (D36)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.token_budget import (
    evaluate_query_complexity, allocate_budget, adaptive_upgrade,
    BUDGET_TIERS,
)


class TestEvaluateQueryComplexity(unittest.TestCase):
    """Test query complexity evaluation."""

    def test_short_keyword_query_low_complexity(self):
        """A short keyword query has low complexity."""
        result = evaluate_query_complexity("foo", related_nodes_count=2)
        self.assertLess(result["score"], 0.4)
        self.assertIn(result["recommended_budget"], ("micro", "lite"))

    def test_cypher_query_higher_complexity(self):
        """A Cypher query has higher complexity than a keyword search."""
        result = evaluate_query_complexity(
            "MATCH (n) WHERE n.name =~ 'foo.*' RETURN n", related_nodes_count=30,
            max_depth=5)
        self.assertGreater(result["score"], 0.3)
        self.assertEqual(result["factors"]["query_type"], 0.7)

    def test_natural_language_question(self):
        """A 'why' question has higher query_type factor."""
        result = evaluate_query_complexity("why doesn't my_func work?")
        self.assertEqual(result["factors"]["query_type"], 0.8)
        # Negation present
        self.assertGreater(result["factors"]["negation"], 0)

    def test_aggregation_detected(self):
        """Aggregation keywords boost complexity."""
        result = evaluate_query_complexity("count all functions in domain")
        self.assertGreater(result["factors"]["aggregation"], 0)

    def test_deep_traversal_high_complexity(self):
        """Deep traversal (max_depth=10) gives high depth factor."""
        result = evaluate_query_complexity("trace call chain", max_depth=10)
        self.assertGreaterEqual(result["factors"]["depth"], 0.9)

    def test_many_nodes_high_complexity(self):
        """Many related nodes give high nodes_count factor."""
        result = evaluate_query_complexity("find hubs", related_nodes_count=80)
        self.assertGreaterEqual(result["factors"]["nodes_count"], 0.9)

    def test_score_in_unit_interval(self):
        """Complexity score is always in [0, 1]."""
        for q in ["", "a", "x" * 1000]:
            for n in [0, 5, 100]:
                for d in [0, 5, 20]:
                    result = evaluate_query_complexity(q, n, d)
                    self.assertGreaterEqual(result["score"], 0.0)
                    self.assertLessEqual(result["score"], 1.0)


class TestAllocateBudget(unittest.TestCase):
    """Test dynamic budget allocation."""

    def test_low_complexity_gets_min_budget(self):
        """Low complexity allocates near base_budget."""
        complexity = {"score": 0.1, "recommended_budget": "micro", "factors": {}}
        alloc = allocate_budget(complexity, base_budget=500, max_budget=10000)
        self.assertLessEqual(alloc["tokens"], 1500)
        self.assertEqual(alloc["tier"], "micro")
        self.assertIn("micro", alloc["pack_layers"])

    def test_high_complexity_gets_max_budget(self):
        """High complexity allocates near max_budget."""
        complexity = {"score": 0.95, "recommended_budget": "deep", "factors": {}}
        alloc = allocate_budget(complexity, base_budget=500, max_budget=10000)
        self.assertGreater(alloc["tokens"], 8000)
        self.assertEqual(alloc["tier"], "deep")
        self.assertIn("explore_flow", alloc["pack_layers"])

    def test_budget_clamped_to_max(self):
        """Budget never exceeds max_budget."""
        complexity = {"score": 1.0, "recommended_budget": "deep", "factors": {}}
        alloc = allocate_budget(complexity, base_budget=500, max_budget=2000)
        self.assertLessEqual(alloc["tokens"], 2000)

    def test_budget_at_least_base(self):
        """Budget is at least base_budget."""
        complexity = {"score": 0.0, "recommended_budget": "micro", "factors": {}}
        alloc = allocate_budget(complexity, base_budget=1000, max_budget=5000)
        self.assertGreaterEqual(alloc["tokens"], 1000)

    def test_pack_layers_grow_with_tier(self):
        """Deeper tiers include more pack layers."""
        layers_micro = allocate_budget(
            {"score": 0.1, "recommended_budget": "micro", "factors": {}}
        )["pack_layers"]
        layers_deep = allocate_budget(
            {"score": 0.95, "recommended_budget": "deep", "factors": {}}
        )["pack_layers"]
        self.assertLess(len(layers_micro), len(layers_deep))

    def test_rationale_string_present(self):
        """The rationale string explains the allocation."""
        complexity = {"score": 0.5, "recommended_budget": "lite",
                      "factors": {"query_type": 0.7}}
        alloc = allocate_budget(complexity)
        self.assertIn("rationale", alloc)
        self.assertIn("complexity_score", alloc["rationale"])


class TestAdaptiveUpgrade(unittest.TestCase):
    """Test the adaptive upgrade decision."""

    def test_truncation_triggers_upgrade(self):
        """A truncation marker in the response triggers upgrade."""
        result = adaptive_upgrade(
            current_response_tokens=2000, max_tokens=2000,
            truncation_marker="... [truncated at ~2000 tokens]")
        self.assertTrue(result["should_upgrade"])
        self.assertIn("truncation", result["reason"])

    def test_near_budget_triggers_upgrade(self):
        """Using >90% of budget triggers upgrade."""
        result = adaptive_upgrade(
            current_response_tokens=950, max_tokens=1000)
        self.assertTrue(result["should_upgrade"])

    def test_under_budget_no_upgrade(self):
        """Using <90% of budget doesn't trigger upgrade."""
        result = adaptive_upgrade(
            current_response_tokens=500, max_tokens=1000)
        self.assertFalse(result["should_upgrade"])


class TestBudgetTiers(unittest.TestCase):
    """Test the BUDGET_TIERS constant."""

    def test_tiers_present(self):
        """All expected tiers are defined."""
        for tier in ("micro", "lite", "standard", "deep", "unlimited"):
            self.assertIn(tier, BUDGET_TIERS)

    def test_tiers_ordered(self):
        """Tier token counts increase monotonically."""
        self.assertLess(BUDGET_TIERS["micro"], BUDGET_TIERS["lite"])
        self.assertLess(BUDGET_TIERS["lite"], BUDGET_TIERS["standard"])
        self.assertLess(BUDGET_TIERS["standard"], BUDGET_TIERS["deep"])


if __name__ == "__main__":
    unittest.main()
