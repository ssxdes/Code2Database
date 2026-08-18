"""Tests for the intent router (D45+D16 optimization)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.intent_router import (
    classify_intent, intent_query, cmd_intent_query, INTENT_RULES,
)


class TestClassifyIntent(unittest.TestCase):
    """Test classify_intent for various question patterns."""

    def test_empty_question_returns_none(self):
        """Empty question returns None."""
        self.assertIsNone(classify_intent(""))
        self.assertIsNone(classify_intent("   "))

    def test_why_dead_code(self):
        """'why is this function dead code?' routes to describe-node --explain-label dead_code."""
        result = classify_intent("why is this function dead code?")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "describe-node")
        self.assertEqual(result["args"]["explain-label"], "dead_code")
        self.assertGreaterEqual(result["confidence"], 0.8)
        self.assertEqual(result["matched_intent"], "why_dead_code")

    def test_why_ambiguous(self):
        """'why is this labeling ambiguous?' routes to describe-node --explain-label ambiguous."""
        result = classify_intent("why is this labeling ambiguous?")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "describe-node")
        self.assertEqual(result["args"]["explain-label"], "ambiguous")
        self.assertEqual(result["matched_intent"], "why_ambiguous")

    def test_who_calls(self):
        """'who calls my_function?' routes to callers with extracted node name."""
        result = classify_intent("who calls my_function?")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "callers")
        self.assertEqual(result["args"]["node"], "my_function")

    def test_what_does_call(self):
        """'what does my_function call?' routes to callees."""
        result = classify_intent("what does my_function call?")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "callees")
        self.assertEqual(result["args"]["node"], "my_function")

    def test_call_chain(self):
        """'call chain from foo to bar' routes to call-chain with from/to."""
        result = classify_intent("call chain from foo to bar")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "call-chain")
        self.assertEqual(result["args"]["from"], "foo")
        self.assertEqual(result["args"]["to"], "bar")

    def test_data_flow(self):
        """'data flow from src to sink' routes to value-flow."""
        result = classify_intent("data flow from src to sink")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "value-flow")
        self.assertEqual(result["args"]["from"], "src")
        self.assertEqual(result["args"]["to"], "sink")

    def test_find_locks(self):
        """'what locks does my_func hold?' routes to lock-coverage."""
        result = classify_intent("what locks does my_func hold?")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "lock-coverage")
        self.assertEqual(result["args"]["node"], "my_func")

    def test_race_condition(self):
        """'race condition' routes to race-detect."""
        result = classify_intent("show me race conditions in the code")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "race-detect")

    def test_find_invariants(self):
        """'invariants for my_function' routes to find-invariants."""
        result = classify_intent("invariants for my_function")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "find-invariants")
        self.assertEqual(result["args"]["node"], "my_function")

    def test_ffi_boundary(self):
        """'ffi calls' routes to ffi-list."""
        result = classify_intent("show me ffi calls in the project")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "ffi-list")

    def test_daemon_status(self):
        """'daemon status' routes to daemon-status."""
        result = classify_intent("daemon status")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "daemon-status")

    def test_path_feasible(self):
        """'is this path feasible?' routes to path-feasible."""
        result = classify_intent("is this path feasible?")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "path-feasible")

    def test_doc_code_check(self):
        """'doc code mismatch' routes to doc-code-check."""
        result = classify_intent("doc code mismatch check")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "doc-code-check")

    def test_explore_flow(self):
        """'explore initialization' routes to explore-flow with extracted query."""
        result = classify_intent("explore initialization")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "explore-flow")
        self.assertIn("initialization", result["args"]["query"])

    def test_describe_node_fallback(self):
        """'describe my_function' routes to describe-node."""
        result = classify_intent("describe my_function")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "describe-node")

    def test_no_match_returns_none(self):
        """Random unrelated text returns None."""
        result = classify_intent("asdfjkl 12345 random unrelated text")
        self.assertIsNone(result)

    def test_question_case_insensitive(self):
        """Patterns match case-insensitively."""
        result = classify_intent("WHY IS THIS FUNCTION DEAD CODE?")
        self.assertIsNotNone(result)
        self.assertEqual(result["matched_intent"], "why_dead_code")


class TestIntentQuery(unittest.TestCase):
    """Test intent_query top-level entry point."""

    def test_successful_routing(self):
        """Successful routing returns ok=True and suggestion string."""
        result = intent_query("who calls foo?")
        self.assertTrue(result["ok"])
        self.assertEqual(result["question"], "who calls foo?")
        self.assertIsNotNone(result["routing"])
        self.assertIn("code2database_builder.py callers", result["suggestion"])

    def test_no_match_returns_suggestion_message(self):
        """No-match returns ok=False with helpful message."""
        result = intent_query("asdf random 12345")
        self.assertFalse(result["ok"])
        self.assertIn("No matching intent", result["suggestion"])

    def test_suggestion_includes_args(self):
        """Suggestion string includes --node arg when applicable."""
        result = intent_query("who calls my_function?")
        self.assertTrue(result["ok"])
        self.assertIn("--node my_function", result["suggestion"])

    def test_suggestion_no_args_when_empty(self):
        """Suggestion string has no args when args dict is empty."""
        result = intent_query("daemon status")
        self.assertTrue(result["ok"])
        # daemon-status has empty args dict
        suggestion = result["suggestion"]
        # Should be just "code2database_builder.py daemon-status"
        self.assertTrue(suggestion.endswith("daemon-status"))


class TestIntentRules(unittest.TestCase):
    """Test INTENT_RULES structure."""

    def test_rules_have_required_fields(self):
        """Each rule has all required fields."""
        for rule in INTENT_RULES:
            self.assertIn("name", rule, f"Rule missing 'name': {rule}")
            self.assertIn("patterns", rule, f"Rule {rule.get('name')} missing 'patterns'")
            self.assertIn("command", rule, f"Rule {rule.get('name')} missing 'command'")
            self.assertIn("args", rule, f"Rule {rule.get('name')} missing 'args'")
            self.assertIn("description", rule, f"Rule {rule.get('name')} missing 'description'")
            self.assertIn("min_confidence", rule, f"Rule {rule.get('name')} missing 'min_confidence'")

    def test_patterns_are_valid_regex(self):
        """All patterns compile as valid regex."""
        import re
        for rule in INTENT_RULES:
            for pat in rule["patterns"]:
                try:
                    re.compile(pat)
                except re.error as e:
                    self.fail(f"Rule {rule['name']} has invalid regex {pat!r}: {e}")

    def test_min_confidence_in_range(self):
        """All min_confidence values are in [0, 1]."""
        for rule in INTENT_RULES:
            self.assertGreaterEqual(rule["min_confidence"], 0.0)
            self.assertLessEqual(rule["min_confidence"], 1.0)

    def test_at_least_one_rule_per_command(self):
        """Every command in the rules has at least one rule."""
        commands = {rule["command"] for rule in INTENT_RULES}
        self.assertGreater(len(commands), 5)


class TestCmdIntentQuery(unittest.TestCase):
    """Test the CLI handler cmd_intent_query."""

    def test_successful_command_returns_zero(self):
        """A successful intent classification returns exit code 0."""
        class FakeArgs:
            question = "who calls foo?"
            graph = ""
        rc = cmd_intent_query(FakeArgs())
        self.assertEqual(rc, 0)

    def test_no_match_returns_one(self):
        """No match returns exit code 1."""
        class FakeArgs:
            question = "asdf random 12345"
            graph = ""
        rc = cmd_intent_query(FakeArgs())
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
