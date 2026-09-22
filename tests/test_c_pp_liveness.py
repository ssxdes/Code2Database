"""C scanner: preprocessor liveness with real macro bindings.

The dead-range logic (_build_pp_liveness) only runs when the scanner
receives non-empty macro bindings; every other test passes empty
bindings, so the stack-based #if/#elif/#else walk — and the
dead_code / preproc_alive annotations it produces — was never
asserted against a real file.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

_BINDINGS = {"FEATURE_A": 1, "MODE": 2}


def _scan_c(code, bindings=None):
    from _scanner.c_scanner import CTreeSitterScanner
    scanner = CTreeSitterScanner()
    with tempfile.NamedTemporaryFile(suffix='.c', mode='w',
                                     delete=False) as f:
        f.write(code)
        f.flush()
        result = scanner.scan_file(f.name, source_root=os.path.dirname(f.name),
                                   macro_bindings=_BINDINGS if bindings is None
                                   else bindings)
    os.unlink(f.name)
    return result


def _funcs_by_name(result):
    return {f["name"]: f for f in result["functions"]}


class TestBuildPpLivenessDeadRanges(unittest.TestCase):
    """Direct unit tests of the dead-range stack walk."""

    def _make_scanner(self):
        from _scanner.c_scanner import CTreeSitterScanner
        s = CTreeSitterScanner()
        s._macro_bindings = dict(_BINDINGS)
        return s

    def _ranges(self, code):
        from _scanner.c_scanner import _PP_COND_RE
        s = self._make_scanner()
        text = code
        pp_conds = [(m.start(), m.group(1), m.group(2).strip())
                    for m in _PP_COND_RE.finditer(text)]
        return s._build_pp_liveness(pp_conds, text)

    def test_dead_ifdef_produces_range(self):
        code = "int a;\n#ifdef FEATURE_B\nint b;\n#endif\nint c;\n"
        ranges = self._ranges(code)
        self.assertEqual(len(ranges), 1)
        (start, end), = ranges
        self.assertLess(start, code.index("int b"))
        self.assertGreater(end, code.index("int b"))
        self.assertLessEqual(start, code.index("#ifdef"))
        self.assertEqual(end, code.index("#endif"))

    def test_alive_ifdef_no_range(self):
        code = "int a;\n#ifdef FEATURE_A\nint b;\n#endif\n"
        self.assertEqual(self._ranges(code), [])

    def test_else_of_dead_ifdef_is_alive(self):
        code = "int a;\n#ifdef FEATURE_B\nint b;\n#else\nint c;\n#endif\n"
        (start, end), = self._ranges(code)
        # The dead range stops at the #else directive; the else body
        # must NOT be inside it.
        self.assertLessEqual(end, code.index("#else"))
        self.assertLess(start, code.index("int b"))

    def test_elif_chain_selects_alive_branch(self):
        code = ("#if 0\nint a;\n#elif 1\nint b;\n#elif 1\nint c;\n"
                "#else\nint d;\n#endif\n")
        ranges = self._ranges(code)
        joined = [(s, e) for s, e in ranges]
        # Branch A is dead until the first #elif takes the branch; arms
        # after the taken #elif are dead again — including the trailing
        # #else (the chain already selected an arm).
        self.assertTrue(all(e <= len(code) for s, e in joined))
        dead_text = "".join(code[s:e] for s, e in joined)
        self.assertIn("int a", dead_text)
        self.assertNotIn("int b", dead_text)
        self.assertIn("int c", dead_text)
        self.assertIn("int d", dead_text)

    def test_else_after_taken_elif_is_dead(self):
        code = ("#ifdef FEATURE_A\nint a;\n#elif 1\nint b;\n"
                "#else\nint c;\n#endif\n")
        ranges = self._ranges(code)
        dead_text = "".join(code[s:e] for s, e in ranges)
        self.assertNotIn("int a", dead_text)
        self.assertIn("int b", dead_text)
        self.assertIn("int c", dead_text)

    def test_second_elif_condition_not_evaluated_after_taken_arm(self):
        code = ("#if 0\nint a;\n#elif 1\nint b;\n#elif 1\nint c;\n#endif\n")
        ranges = self._ranges(code)
        dead_text = "".join(code[s:e] for s, e in ranges)
        self.assertNotIn("int b", dead_text)
        self.assertIn("int c", dead_text)

    def test_dead_parent_kills_alive_child(self):
        code = ("#ifdef FEATURE_B\n"
                "#ifdef FEATURE_A\nint nested;\n#endif\n"
                "#endif\nint after;\n")
        ranges = self._ranges(code)
        self.assertEqual(len(ranges), 1)
        (start, end), = ranges
        self.assertLess(start, code.index("int nested"))
        self.assertGreater(end, code.index("int nested"))
        # "int after" follows the outer #endif and must be alive
        self.assertLess(end, code.index("int after"))

    def test_unterminated_dead_if_extends_to_eof(self):
        code = "#ifdef FEATURE_B\nint a;\nint b;\n"
        (start, end), = self._ranges(code)
        self.assertEqual(end, len(code))

    def test_no_bindings_returns_empty(self):
        from _scanner.c_scanner import _PP_COND_RE, CTreeSitterScanner
        s = CTreeSitterScanner()
        s._macro_bindings = {}
        code = "#ifdef FEATURE_B\nint b;\n#endif\n"
        pp_conds = [(m.start(), m.group(1), m.group(2).strip())
                    for m in _PP_COND_RE.finditer(code)]
        self.assertEqual(s._build_pp_liveness(pp_conds, code), [])


class TestScanFilePpLiveness(unittest.TestCase):
    """End-to-end through scan_file with macro bindings."""

    def test_dead_function_gets_dead_code_label(self):
        code = (
            "#ifdef FEATURE_A\n"
            "void alive_fn(void) { helper_a(); }\n"
            "#endif\n"
            "#ifdef FEATURE_B\n"
            "void dead_fn(void) { helper_b(); }\n"
            "#endif\n"
        )
        result = _scan_c(code)
        funcs = _funcs_by_name(result)
        self.assertFalse(funcs["alive_fn"]["preproc_alive"] is False)
        self.assertNotIn("dead_code", funcs["alive_fn"]["labels"])
        self.assertFalse(funcs["dead_fn"]["preproc_alive"])
        self.assertIn("dead_code", funcs["dead_fn"]["labels"])
        self.assertEqual(funcs["dead_fn"]["labels_source"]["dead_code"],
                         "preproc_dead")

    def test_else_branch_function_is_alive(self):
        code = (
            "#ifdef FEATURE_B\n"
            "void in_dead_branch(void) {}\n"
            "#else\n"
            "void in_else_branch(void) {}\n"
            "#endif\n"
        )
        result = _scan_c(code)
        funcs = _funcs_by_name(result)
        self.assertFalse(funcs["in_dead_branch"]["preproc_alive"])
        self.assertTrue(funcs["in_else_branch"]["preproc_alive"])
        self.assertNotIn("dead_code", funcs["in_else_branch"]["labels"])

    def test_dead_call_edge_gets_zero_confidence(self):
        code = (
            "void caller(void) {\n"
            "#ifdef FEATURE_B\n"
            "  dead_target();\n"
            "#else\n"
            "  live_target();\n"
            "#endif\n"
            "}\n"
        )
        result = _scan_c(code)
        by_target = {}
        for e in result["edges"]:
            if e.get("target") in ("dead_target", "live_target"):
                by_target[e["target"]] = e
        dead = by_target.get("dead_target")
        self.assertIsNotNone(dead, "dead call edge must still be emitted")
        self.assertEqual(dead["confidence"], "AMBIGUOUS")
        self.assertEqual(dead["confidence_score"], 0.0)
        self.assertEqual(dead["source_tag"], "preproc_dead")
        self.assertFalse(dead["preproc_alive"])
        live = by_target.get("live_target")
        self.assertIsNotNone(live)
        self.assertNotEqual(live["confidence"], "AMBIGUOUS")
        self.assertNotEqual(live.get("source_tag"), "preproc_dead")

    def test_ifndef_dead_when_bound(self):
        code = (
            "#ifndef FEATURE_A\n"
            "void only_when_absent(void) {}\n"
            "#endif\n"
        )
        result = _scan_c(code)
        funcs = _funcs_by_name(result)
        self.assertFalse(funcs["only_when_absent"]["preproc_alive"])

    def test_value_comparison_in_if(self):
        code = (
            "#if MODE == 2\n"
            "void mode_two(void) {}\n"
            "#endif\n"
            "#if MODE == 9\n"
            "void mode_nine(void) {}\n"
            "#endif\n"
        )
        result = _scan_c(code, bindings={"MODE": "2"})
        funcs = _funcs_by_name(result)
        self.assertTrue(funcs["mode_two"]["preproc_alive"])
        self.assertFalse(funcs["mode_nine"]["preproc_alive"])

    def test_literal_one_branch_is_alive(self):
        code = (
            "#if 1\n"
            "void always_on(void) {}\n"
            "#endif\n"
            "#if 0\n"
            "void always_off(void) {}\n"
            "#endif\n"
        )
        result = _scan_c(code, bindings={"FEATURE_A": "1"})
        funcs = _funcs_by_name(result)
        self.assertTrue(funcs["always_on"]["preproc_alive"])
        self.assertFalse(funcs["always_off"]["preproc_alive"])


if __name__ == "__main__":
    unittest.main()
