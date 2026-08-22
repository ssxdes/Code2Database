"""Tests for LLM-assisted invariant extraction with consensus."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.llm_invariants import (
    _normalize_condition, _parse_llm_invariants, _build_rule_invariant_index,
    _merge_invariants, extract_invariants_with_llm,
)


class TestNormalizeCondition(unittest.TestCase):
    """Test condition normalization."""

    def test_lowercases_and_strips(self):
        self.assertEqual(_normalize_condition("  X != NULL  "),
                         "x != null")

    def test_collapses_whitespace(self):
        self.assertEqual(_normalize_condition("x   >=   10"),
                         "x >= 10")

    def test_empty_returns_empty(self):
        self.assertEqual(_normalize_condition(""), "")
        self.assertEqual(_normalize_condition(None), "")


class TestParseLLMInvariants(unittest.TestCase):
    """Test parsing LLM responses."""

    def test_parses_json_response(self):
        """JSON-formatted responses are parsed into invariant dicts."""
        response = '''{"invariants": [
            {"kind": "precondition", "condition": "ctx != NULL",
             "evidence": "if (!ctx) return -EINVAL;"},
            {"kind": "postcondition", "condition": "returns 0 on success",
             "evidence": "return 0;"}
        ]}'''
        result = _parse_llm_invariants(response)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["kind"], "precondition")
        self.assertEqual(result[0]["condition"], "ctx != NULL")
        self.assertEqual(result[1]["kind"], "postcondition")

    def test_parses_line_based_response(self):
        """Line-based responses (precondition: ...) are parsed as fallback."""
        response = """precondition: x != NULL
postcondition: returns 0 on success
loop_invariant: i < n"""
        result = _parse_llm_invariants(response)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["kind"], "precondition")
        self.assertEqual(result[0]["condition"], "x != NULL")
        self.assertEqual(result[2]["kind"], "loop_invariant")

    def test_empty_response(self):
        """Empty response returns empty list."""
        self.assertEqual(_parse_llm_invariants(""), [])
        self.assertEqual(_parse_llm_invariants(None), [])

    def test_invalid_json_falls_back_to_lines(self):
        """Invalid JSON falls back to line parsing."""
        response = """not valid json
precondition: x > 0"""
        result = _parse_llm_invariants(response)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["condition"], "x > 0")


class TestBuildRuleInvariantIndex(unittest.TestCase):
    """Test rule-based invariant indexing."""

    def test_indexes_by_kind_and_condition(self):
        """Rule invariants are indexed by (kind, normalized_condition)."""
        rule_inv = {
            "preconditions": [
                {"condition": "ctx != NULL", "confidence": "EXTRACTED"},
                {"condition": "x >= 0", "confidence": "EXTRACTED"},
            ],
            "postconditions": [
                {"condition": "returns 0", "confidence": "EXTRACTED"},
            ],
            "loop_invariants": [],
            "state_machine": None,
        }
        index = _build_rule_invariant_index(rule_inv)
        self.assertIn(("precondition", "ctx != null"), index)
        self.assertIn(("precondition", "x >= 0"), index)
        self.assertIn(("postcondition", "returns 0"), index)


class TestMergeInvariants(unittest.TestCase):
    """Test merging rule-based and LLM invariants."""

    def test_merges_non_duplicates(self):
        """LLM invariants not in rule-based are appended."""
        rule_inv = {
            "preconditions": [{"condition": "ctx != NULL",
                               "confidence": "EXTRACTED"}],
            "postconditions": [],
            "loop_invariants": [],
            "state_machine": None,
        }
        llm_accepted = [
            {"kind": "precondition", "condition": "ctx != NULL",
             "confidence_score": 0.9, "source": "llm_consensus"},
            {"kind": "postcondition", "condition": "returns 0 on success",
             "confidence_score": 0.9, "source": "llm_consensus"},
        ]
        merged = _merge_invariants(rule_inv, llm_accepted)
        # Should have 1 precondition (dedup) + 1 postcondition (new)
        self.assertEqual(len(merged["preconditions"]), 1)
        self.assertEqual(len(merged["postconditions"]), 1)
        self.assertEqual(merged["postconditions"][0]["condition"],
                         "returns 0 on success")

    def test_state_machine_preserved(self):
        """State machine from rule-based is preserved through merge."""
        rule_inv = {
            "preconditions": [],
            "postconditions": [],
            "loop_invariants": [],
            "state_machine": {"states": ["A", "B"]},
        }
        merged = _merge_invariants(rule_inv, [])
        self.assertIsNotNone(merged["state_machine"])
        self.assertEqual(merged["state_machine"]["states"], ["A", "B"])


class TestExtractInvariantsWithLLM(unittest.TestCase):
    """Test the full LLM extraction flow with a mock LLM client."""

    def test_llm_unavailable_returns_rule_based(self):
        """When LLM client returns None, only rule-based invariants are returned."""
        node_data = {
            "name": "my_func",
            "signature": "int my_func(int x)",
            "body_text": "int my_func(int x) { if (x < 0) return -1; return 0; }",
            "params": [{"name": "x"}],
        }
        result = extract_invariants_with_llm(
            node_data, num_calls=3, llm_client=lambda prompt: None)
        # Should have llm_consensus indicating 0 successful calls
        self.assertEqual(result["llm_consensus"]["num_calls"], 0)
        self.assertIn("error", result["llm_consensus"])
        # Should still have rule-based preconditions (x < 0 -> x >= 0)
        self.assertGreater(len(result["preconditions"]), 0)

    def test_consensus_accepts_repeated_invariants(self):
        """Invariants appearing in >= 2 of 3 LLM responses are accepted."""
        node_data = {
            "name": "my_func",
            "signature": "int my_func(int x)",
            "body_text": "int my_func(int x) { if (!x) return -1; return 0; }",
            "params": [{"name": "x"}],
        }
        # Mock LLM that always returns the same invariant
        responses = [
            json.dumps({"invariants": [
                {"kind": "precondition", "condition": "x != NULL"},
                {"kind": "postcondition", "condition": "returns 0 on success"},
            ]}) for _ in range(3)
        ]
        call_count = [0]
        def mock_client(prompt):
            r = responses[call_count[0] % 3]
            call_count[0] += 1
            return r
        result = extract_invariants_with_llm(
            node_data, num_calls=3, llm_client=mock_client)
        # Should have accepted both invariants (3/3 agreement)
        self.assertEqual(result["llm_consensus"]["accepted_count"], 2)
        # All accepted invariants should have high confidence_score
        for inv in result["preconditions"]:
            if inv.get("source") == "llm_consensus":
                self.assertGreaterEqual(inv["confidence_score"], 0.9)
        for inv in result["postconditions"]:
            if inv.get("source") == "llm_consensus":
                self.assertGreaterEqual(inv["confidence_score"], 0.9)

    def test_consensus_rejects_single_response_invariant(self):
        """Invariants appearing in only 1 of 3 LLM responses are rejected."""
        node_data = {
            "name": "my_func",
            "signature": "int my_func(int x)",
            "body_text": "int my_func(int x) { return 0; }",
            "params": [{"name": "x"}],
        }
        # First call returns one invariant, others return a different one
        responses = [
            json.dumps({"invariants": [
                {"kind": "precondition", "condition": "x > 0"},
            ]}),
            json.dumps({"invariants": [
                {"kind": "precondition", "condition": "x < 100"},
            ]}),
            json.dumps({"invariants": [
                {"kind": "precondition", "condition": "x < 100"},
            ]}),
        ]
        call_count = [0]
        def mock_client(prompt):
            r = responses[call_count[0] % 3]
            call_count[0] += 1
            return r
        result = extract_invariants_with_llm(
            node_data, num_calls=3, llm_client=mock_client)
        # Only "x < 100" should be accepted (2/3 agreement)
        accepted_conds = [inv["condition"] for inv in result["preconditions"]
                          if inv.get("source") == "llm_consensus"]
        self.assertIn("x < 100", accepted_conds)
        self.assertNotIn("x > 0", accepted_conds)

    def test_corroboration_with_rules_boosts_confidence(self):
        """LLM invariants matching rule-based invariants get a bonus."""
        node_data = {
            "name": "my_func",
            "signature": "int my_func(int x)",
            # Rule-based extraction will find "x is truthy" from `if (!x)`
            "body_text": "int my_func(int x) { if (!x) return -1; return 0; }",
            "params": [{"name": "x"}],
        }
        # LLM also returns "x is truthy" — should be corroborated
        def mock_client(prompt):
            return json.dumps({"invariants": [
                {"kind": "precondition", "condition": "x is truthy"},
            ]})
        result = extract_invariants_with_llm(
            node_data, num_calls=3, llm_client=mock_client)
        # Should report corroboration
        self.assertGreater(result["llm_consensus"]["corroborated_with_rules"], 0)


import json  # noqa: E402


if __name__ == "__main__":
    unittest.main()
