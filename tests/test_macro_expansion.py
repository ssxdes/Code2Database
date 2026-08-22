"""Tests for C/C++ macro expansion (D12: MACRO_EXPANDS_TO)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


class TestMacroDefinitionExtraction(unittest.TestCase):
    """Test _extract_macro_definitions."""

    def _make_scanner(self):
        from _scanner.c_scanner import CTreeSitterScanner
        return CTreeSitterScanner()

    def test_no_macros_returns_empty(self):
        """No #define → empty dict."""
        s = self._make_scanner()
        # Parse a simple file with no macros
        src = b"int main() { return 0; }\n"
        tree = s.parser.parse(src)
        macros = s._extract_macro_definitions(tree.root_node, src)
        self.assertEqual(macros, {})

    def test_object_like_macro(self):
        """#define MAX 100 → object-like macro with no params."""
        s = self._make_scanner()
        src = b"#define MAX 100\nint main() { return MAX; }\n"
        tree = s.parser.parse(src)
        macros = s._extract_macro_definitions(tree.root_node, src)
        self.assertIn("MAX", macros)
        self.assertEqual(macros["MAX"]["params"], [])
        self.assertIn("100", macros["MAX"]["body"])
        self.assertFalse(macros["MAX"]["is_function"])

    def test_function_like_macro(self):
        """#define FOO(x) bar(x) → function-like macro with params."""
        s = self._make_scanner()
        src = b"#define FOO(x) bar(x)\nint main() { return FOO(42); }\n"
        tree = s.parser.parse(src)
        macros = s._extract_macro_definitions(tree.root_node, src)
        self.assertIn("FOO", macros)
        self.assertEqual(macros["FOO"]["params"], ["x"])
        self.assertIn("bar", macros["FOO"]["body"])
        self.assertTrue(macros["FOO"]["is_function"])

    def test_multiple_params(self):
        """#define ADD(a,b) ((a)+(b)) → params=[a,b]."""
        s = self._make_scanner()
        src = b"#define ADD(a, b) ((a) + (b))\nint main() { return ADD(1, 2); }\n"
        tree = s.parser.parse(src)
        macros = s._extract_macro_definitions(tree.root_node, src)
        self.assertEqual(macros["ADD"]["params"], ["a", "b"])

    def test_macro_line_number(self):
        """Macro line number is correctly recorded."""
        s = self._make_scanner()
        src = b"\n\n#define FOO 1\n"
        tree = s.parser.parse(src)
        macros = s._extract_macro_definitions(tree.root_node, src)
        self.assertEqual(macros["FOO"]["line"], 3)


class TestMacroExpansion(unittest.TestCase):
    """Test _expand_macro substitution."""

    def _make_scanner(self):
        from _scanner.c_scanner import CTreeSitterScanner
        return CTreeSitterScanner()

    def test_object_like_returns_body(self):
        """Object-like macro returns body unchanged."""
        s = self._make_scanner()
        mdef = {"params": [], "body": "100", "is_function": False}
        self.assertEqual(s._expand_macro(mdef, []), "100")

    def test_single_param_substitution(self):
        """Function-like macro substitutes single param."""
        s = self._make_scanner()
        mdef = {"params": ["x"], "body": "bar(x)", "is_function": True}
        self.assertEqual(s._expand_macro(mdef, ["42"]), "bar(42)")

    def test_multi_param_substitution(self):
        """Function-like macro substitutes multiple params."""
        s = self._make_scanner()
        mdef = {"params": ["a", "b"], "body": "((a) + (b))", "is_function": True}
        result = s._expand_macro(mdef, ["1", "2"])
        self.assertEqual(result, "((1) + (2))")

    def test_word_boundary_no_partial_match(self):
        """Param substitution respects word boundaries (no partial matches)."""
        s = self._make_scanner()
        mdef = {"params": ["x"], "body": "x + x_max", "is_function": True}
        result = s._expand_macro(mdef, ["1"])
        self.assertEqual(result, "1 + x_max")  # x_max NOT replaced

    def test_empty_body_returns_empty(self):
        """Empty body returns empty string."""
        s = self._make_scanner()
        mdef = {"params": ["x"], "body": "", "is_function": True}
        self.assertEqual(s._expand_macro(mdef, ["1"]), "")

    def test_no_params_returns_body(self):
        """Empty params list returns body unchanged."""
        s = self._make_scanner()
        mdef = {"params": [], "body": "FOO_BODY", "is_function": False}
        self.assertEqual(s._expand_macro(mdef, ["1"]), "FOO_BODY")


class TestExtractMacroCallsFromBody(unittest.TestCase):
    """Test _extract_macro_calls_from_body."""

    def _make_scanner(self):
        from _scanner.c_scanner import CTreeSitterScanner
        return CTreeSitterScanner()

    def test_simple_call(self):
        """Body with one call returns that callee."""
        s = self._make_scanner()
        self.assertEqual(s._extract_macro_calls_from_body("bar(42)"), ["bar"])

    def test_multiple_calls(self):
        """Body with multiple calls returns all unique callees."""
        s = self._make_scanner()
        result = s._extract_macro_calls_from_body("foo(1); bar(2); foo(3);")
        self.assertEqual(result, ["foo", "bar"])

    def test_excludes_keywords(self):
        """Control keywords are NOT extracted as callees."""
        s = self._make_scanner()
        result = s._extract_macro_calls_from_body("if (x) foo();")
        self.assertEqual(result, ["foo"])
        self.assertNotIn("if", result)

    def test_returns_lowercased(self):
        """Callee names are lowercased."""
        s = self._make_scanner()
        result = s._extract_macro_calls_from_body("Bar(42);")
        self.assertEqual(result, ["bar"])

    def test_empty_body(self):
        """Empty body returns empty list."""
        s = self._make_scanner()
        self.assertEqual(s._extract_macro_calls_from_body(""), [])

    def test_no_calls(self):
        """Body without calls returns empty list."""
        s = self._make_scanner()
        self.assertEqual(s._extract_macro_calls_from_body("int x = 42;"), [])


class TestMacroExpansionIntegration(unittest.TestCase):
    """End-to-end: scan a file with macros and verify MACRO_EXPANDS_TO edges."""

    def _make_scanner(self):
        from _scanner.c_scanner import CTreeSitterScanner
        s = CTreeSitterScanner()
        # _extract uses _macro_bindings for pp_liveness — initialize empty
        s._macro_bindings = {}
        s._current_filepath = '/src/test.c'
        return s

    def test_macro_call_creates_expand_edge(self):
        """FOO(42) where #define FOO(x) bar(x) creates edge to bar."""
        s = self._make_scanner()
        src = b"#define FOO(x) bar(x)\nint main() { return FOO(42); }\n"
        tree = s.parser.parse(src)
        functions, edges, _, _, _ = s._extract(
            tree, src, '/src/test.c', '/src', 'test')
        # Find the macro_expand edge
        macro_edges = [e for e in edges
                        if e.get("source_tag") == "macro_expand"]
        self.assertTrue(len(macro_edges) >= 1)
        self.assertEqual(macro_edges[0]["target"], "bar")
        self.assertEqual(macro_edges[0]["source"], "test_main")

    def test_object_macro_no_expand_edge_for_non_call(self):
        """Object-like macro used as a value (not a call) doesn't create edges."""
        s = self._make_scanner()
        src = b"#define MAX 100\nint main() { return MAX; }\n"
        tree = s.parser.parse(src)
        functions, edges, _, _, _ = s._extract(
            tree, src, '/src/test.c', '/src', 'test')
        macro_edges = [e for e in edges
                        if e.get("source_tag") == "macro_expand"]
        self.assertEqual(macro_edges, [])

    def test_macro_with_multiple_inner_calls(self):
        """#define FOO(x) bar(x); baz(x) creates edges to bar AND baz."""
        s = self._make_scanner()
        src = b"#define FOO(x) do { bar(x); baz(x); } while(0)\n" \
              b"int main() { FOO(42); return 0; }\n"
        tree = s.parser.parse(src)
        functions, edges, _, _, _ = s._extract(
            tree, src, '/src/test.c', '/src', 'test')
        macro_edges = [e for e in edges
                        if e.get("source_tag") == "macro_expand"]
        targets = {e["target"] for e in macro_edges}
        self.assertIn("bar", targets)
        self.assertIn("baz", targets)

    def test_macro_edge_has_inferred_confidence(self):
        """MACRO_EXPANDS_TO edges are tagged INFERRED with confidence 0.7."""
        s = self._make_scanner()
        src = b"#define FOO(x) bar(x)\nint main() { return FOO(42); }\n"
        tree = s.parser.parse(src)
        functions, edges, _, _, _ = s._extract(
            tree, src, '/src/test.c', '/src', 'test')
        macro_edges = [e for e in edges
                        if e.get("source_tag") == "macro_expand"]
        self.assertEqual(macro_edges[0]["confidence"], "INFERRED")
        self.assertAlmostEqual(macro_edges[0]["confidence_score"], 0.7)


if __name__ == "__main__":
    unittest.main()
