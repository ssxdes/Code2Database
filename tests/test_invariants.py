"""Unit tests for invariants.py.

AGENTS.md explicitly promises "Capability modules have dedicated unit tests
in tests/ covering: Invariant extraction (preconditions/postconditions/
loop_invariants/state_machine)". This file delivers that promise.

Coverage:
- extract_preconditions: NULL check, truthy check, range checks, value
  check; confidence downgrade when var isn't a known parameter
- extract_postconditions: state assignment before return, return-success
- extract_loop_invariants: for-loop header, while-loop header
- extract_state_machine: state var detection, transition list, from-state
  heuristic from preceding if-check
- extract_invariants_for_node: combined extraction on a node dict
- Confidence-label semantics: EXTRACTED for parameter matches, INFERRED
  for non-parameter locals (per AGENTS.md hard constraint)
"""
import unittest


class TestExtractPreconditions(unittest.TestCase):
    """Tests for extract_preconditions."""

    def test_null_check_precondition(self):
        """`if (ptr == NULL) return -EINVAL;` → precondition ptr != NULL."""
        from _builder.invariants import extract_preconditions
        body = "if (ptr == NULL) return -EINVAL;"
        params = [{"name": "ptr"}]
        results = extract_preconditions(body, params)
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r["condition"], "ptr != NULL")
        self.assertEqual(r["source"], "null_check")
        self.assertEqual(r["confidence"], "EXTRACTED")  # ptr is a known param
        self.assertIn("ptr == NULL", r["evidence"])

    def test_truthy_check_precondition(self):
        """`if (!flag) return -1;` → precondition flag is truthy."""
        from _builder.invariants import extract_preconditions
        body = "if (!flag) return -1;"
        params = [{"name": "flag"}]
        results = extract_preconditions(body, params)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["condition"], "flag is truthy")
        self.assertEqual(results[0]["source"], "truthy_check")

    def test_lower_bound_precondition(self):
        """`if (count < 0) return -EINVAL;` → precondition count >= 0."""
        from _builder.invariants import extract_preconditions
        body = "if (count < 0) return -EINVAL;"
        params = [{"name": "count"}]
        results = extract_preconditions(body, params)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["condition"], "count >= 0")
        self.assertEqual(results[0]["source"], "lower_bound")

    def test_upper_bound_precondition(self):
        """`if (size > 1024) return -EINVAL;` → precondition size <= 1024."""
        from _builder.invariants import extract_preconditions
        body = "if (size > 1024) return -EINVAL;"
        params = [{"name": "size"}]
        results = extract_preconditions(body, params)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["condition"], "size <= 1024")
        self.assertEqual(results[0]["source"], "upper_bound")

    def test_value_check_precondition(self):
        """`if (mode == 0) return -1;` → precondition mode != 0."""
        from _builder.invariants import extract_preconditions
        body = "if (mode == 0) return -1;"
        params = [{"name": "mode"}]
        results = extract_preconditions(body, params)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["condition"], "mode != 0")
        self.assertEqual(results[0]["source"], "value_check")

    def test_non_param_var_gets_inferred_confidence(self):
        """A var that's NOT in the params list gets INFERRED confidence
        (per AGENTS.md: 'INFERRED require user review')."""
        from _builder.invariants import extract_preconditions
        body = "if (local_var == NULL) return -EINVAL;"
        params = [{"name": "other_param"}]  # local_var not in params
        results = extract_preconditions(body, params)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["confidence"], "INFERRED")

    def test_empty_body_returns_empty(self):
        from _builder.invariants import extract_preconditions
        self.assertEqual(extract_preconditions(""), [])
        self.assertEqual(extract_preconditions("", params=[]), [])

    def test_no_precondition_patterns_returns_empty(self):
        from _builder.invariants import extract_preconditions
        body = "int x = 5; return x;"
        self.assertEqual(extract_preconditions(body, params=[{"name": "x"}]), [])

    def test_line_number_reported(self):
        from _builder.invariants import extract_preconditions
        # Multi-line body — the precondition is on line 2
        body = "int foo() {\n  if (ptr == NULL) return -EINVAL;\n  return 0;\n}"
        params = [{"name": "ptr"}]
        results = extract_preconditions(body, params)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["line"], 2)

    def test_multiple_preconditions_in_same_body(self):
        from _builder.invariants import extract_preconditions
        body = """
        if (ptr == NULL) return -EINVAL;
        if (count < 0) return -EINVAL;
        if (!flag) return -1;
        """
        params = [{"name": "ptr"}, {"name": "count"}, {"name": "flag"}]
        results = extract_preconditions(body, params)
        self.assertEqual(len(results), 3)
        sources = {r["source"] for r in results}
        self.assertEqual(sources, {"null_check", "lower_bound", "truthy_check"})


class TestExtractPostconditions(unittest.TestCase):
    """Tests for extract_postconditions."""

    def test_state_assign_before_return(self):
        """`ctx->state = READY; return 0;` → postcondition ctx->state == READY."""
        from _builder.invariants import extract_postconditions
        body = "ctx->state = READY; return 0;"
        results = extract_postconditions(body)
        state_posts = [r for r in results if r["source"] == "state_assign_before_return"]
        self.assertEqual(len(state_posts), 1)
        self.assertIn("READY", state_posts[0]["condition"])
        self.assertEqual(state_posts[0]["confidence"], "EXTRACTED")

    def test_return_success(self):
        """`return 0;` → postcondition 'returns success (0)'."""
        from _builder.invariants import extract_postconditions
        body = "return 0;"
        results = extract_postconditions(body)
        success_posts = [r for r in results if r["source"] == "return_success"]
        self.assertEqual(len(success_posts), 1)
        self.assertIn("success", success_posts[0]["condition"].lower())

    def test_empty_body_returns_empty(self):
        from _builder.invariants import extract_postconditions
        self.assertEqual(extract_postconditions(""), [])

    def test_no_postcondition_patterns_returns_empty(self):
        from _builder.invariants import extract_postconditions
        body = "int x = 5;"
        self.assertEqual(extract_postconditions(body), [])


class TestExtractLoopInvariants(unittest.TestCase):
    """Tests for extract_loop_invariants."""

    def test_for_loop_header(self):
        """`for (i = 0; i < n; i++)` → loop invariant 'i < n'."""
        from _builder.invariants import extract_loop_invariants
        body = "for (i = 0; i < n; i++) { sum += arr[i]; }"
        results = extract_loop_invariants(body)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["condition"], "i < n")
        self.assertEqual(results[0]["source"], "for_loop_header")
        self.assertEqual(results[0]["confidence"], "EXTRACTED")

    def test_while_loop_header(self):
        """`while (count > 0)` → loop invariant 'count > 0'."""
        from _builder.invariants import extract_loop_invariants
        body = "while (count > 0) { count--; }"
        results = extract_loop_invariants(body)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["condition"], "count > 0")
        self.assertEqual(results[0]["source"], "while_loop_header")

    def test_multiple_loops(self):
        from _builder.invariants import extract_loop_invariants
        body = """
        for (i = 0; i < n; i++) { foo(); }
        while (running) { bar(); }
        """
        results = extract_loop_invariants(body)
        self.assertEqual(len(results), 2)
        sources = {r["source"] for r in results}
        self.assertEqual(sources, {"for_loop_header", "while_loop_header"})

    def test_empty_body_returns_empty(self):
        from _builder.invariants import extract_loop_invariants
        self.assertEqual(extract_loop_invariants(""), [])

    def test_no_loops_returns_empty(self):
        from _builder.invariants import extract_loop_invariants
        body = "int x = 5; return x;"
        self.assertEqual(extract_loop_invariants(body), [])


class TestExtractStateMachine(unittest.TestCase):
    """Tests for extract_state_machine."""

    def test_state_machine_detected(self):
        """Multiple assignments to ctx->state → state machine extracted."""
        from _builder.invariants import extract_state_machine
        body = """
        ctx->state = INIT;
        ctx->state = READY;
        ctx->state = RUNNING;
        """
        sm = extract_state_machine(body, function_name="init")
        self.assertIsNotNone(sm)
        self.assertEqual(sm["var"], "ctx->state")
        self.assertIn("INIT", sm["states"])
        self.assertIn("READY", sm["states"])
        self.assertIn("RUNNING", sm["states"])
        self.assertEqual(len(sm["transitions"]), 3)
        # All transitions belong to the 'init' function
        for t in sm["transitions"]:
            self.assertEqual(t["function"], "init")

    def test_no_state_assignments_returns_none(self):
        from _builder.invariants import extract_state_machine
        body = "int x = 5; return x;"
        sm = extract_state_machine(body)
        self.assertIsNone(sm)

    def test_empty_body_returns_none(self):
        from _builder.invariants import extract_state_machine
        self.assertIsNone(extract_state_machine(""))

    def test_picks_most_assigned_variable(self):
        """When multiple state vars exist, pick the one with most assignments."""
        from _builder.invariants import extract_state_machine
        body = """
        a->state = X;
        b->mode = Y;
        b->mode = Z;
        b->mode = W;
        """
        sm = extract_state_machine(body)
        self.assertIsNotNone(sm)
        # b->mode has 3 assignments, a->state has 1 → pick b->mode
        self.assertEqual(sm["var"], "b->mode")
        self.assertEqual(len(sm["states"]), 3)  # Y, Z, W


class TestExtractInvariantsForNode(unittest.TestCase):
    """Tests for extract_invariants_for_node (combined extraction)."""

    def test_combined_extraction_returns_all_categories(self):
        from _builder.invariants import extract_invariants_for_node
        ndata = {
            "body_text": """
            if (ptr == NULL) return -EINVAL;
            for (i = 0; i < n; i++) { sum += arr[i]; }
            ctx->state = READY; return 0;
            """,
            "params": [{"name": "ptr"}],
            "name": "my_func",
        }
        result = extract_invariants_for_node(ndata)
        self.assertIn("preconditions", result)
        self.assertIn("postconditions", result)
        self.assertIn("loop_invariants", result)
        self.assertIn("state_machine", result)
        self.assertGreater(len(result["preconditions"]), 0)
        self.assertGreater(len(result["loop_invariants"]), 0)
        self.assertGreater(len(result["postconditions"]), 0)

    def test_empty_node_returns_empty_categories(self):
        from _builder.invariants import extract_invariants_for_node
        result = extract_invariants_for_node({"body_text": "", "name": "x"})
        self.assertEqual(result["preconditions"], [])
        self.assertEqual(result["postconditions"], [])
        self.assertEqual(result["loop_invariants"], [])
        # state_machine may be None when no state assignments
        self.assertIsNone(result["state_machine"])


if __name__ == "__main__":
    unittest.main()
