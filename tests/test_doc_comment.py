"""Tests for doc comment extraction and doc_code_align doc_comment fallback."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _scanner.base import BaseScanner


class _FakeNode:
    """Minimal tree-sitter-like node for testing."""
    def __init__(self, start_byte=0, end_byte=0, start_point=(0, 0),
                 end_point=(0, 0), children=None, node_type="function_definition"):
        self.start_byte = start_byte
        self.end_byte = end_byte
        self.start_point = start_point
        self.end_point = end_point
        self.children = children or []
        self.type = node_type


class _FakeStringNode:
    """A string-like child node for Python docstring testing."""
    def __init__(self, text, node_type="string"):
        self.start_byte = 0
        self.end_byte = len(text)
        self.start_point = (1, 0)
        self.end_point = (1, len(text))
        self.type = node_type
        self.children = []
        self._text = text


class _FakeExpressionStatement:
    """An expression_statement containing a string child."""
    def __init__(self, string_child):
        self.start_byte = 0
        self.end_byte = string_child.end_byte
        self.start_point = (1, 0)
        self.end_point = (1, 0)
        self.type = "expression_statement"
        self.children = [string_child]


class _FakeBody:
    """A function body containing statements."""
    def __init__(self, children):
        self.start_byte = 0
        self.end_byte = 0
        self.start_point = (1, 0)
        self.end_point = (1, 0)
        self.type = "block"
        self.children = children


class TestExtractDocComment(unittest.TestCase):
    """Test the _extract_doc_comment static method on BaseScanner."""

    def test_javadoc_block_comment(self):
        """/** ... */ block comment immediately before function is extracted."""
        source = b"""int previous_var;

/**
 * This function does useful work.
 * @param x input value
 * @return 0 on success
 */
int do_work(int x) {
    return 0;
}
"""
        # Find function position
        marker = b"int do_work"
        start_byte = source.find(marker)
        func_node = _FakeNode(start_byte=start_byte, end_byte=len(source))
        doc = BaseScanner._extract_doc_comment(func_node, source)
        self.assertIn("This function does useful work", doc)
        self.assertIn("@param x input value", doc)
        self.assertIn("@return 0 on success", doc)

    def test_triple_slash_line_comment(self):
        """/// line comments immediately before function are extracted."""
        source = b"""int previous_var;

/// First line of doc.
/// Second line of doc.
int do_work(int x) {
    return 0;
}
"""
        marker = b"int do_work"
        start_byte = source.find(marker)
        func_node = _FakeNode(start_byte=start_byte, end_byte=len(source))
        doc = BaseScanner._extract_doc_comment(func_node, source)
        self.assertIn("First line of doc.", doc)
        self.assertIn("Second line of doc.", doc)

    def test_no_doc_comment(self):
        """No comment before function returns empty string."""
        source = b"""int previous_var;
int do_work(int x) {
    return 0;
}
"""
        marker = b"int do_work"
        start_byte = source.find(marker)
        func_node = _FakeNode(start_byte=start_byte, end_byte=len(source))
        doc = BaseScanner._extract_doc_comment(func_node, source)
        self.assertEqual(doc, "")

    def test_blank_line_between_breaks_extraction(self):
        """A blank line between /// comment and function: doc still extracted (rstrip forgives trailing whitespace)."""
        source = b"""/// Doc comment.

int do_work(int x) {
    return 0;
}
"""
        marker = b"int do_work"
        start_byte = source.find(marker)
        func_node = _FakeNode(start_byte=start_byte, end_byte=len(source))
        doc = BaseScanner._extract_doc_comment(func_node, source)
        # The rstrip() in the helper removes trailing blank lines before the
        # function, so /// comments just above are still recognized.
        self.assertIn("Doc comment.", doc)

    def test_javadoc_with_blank_line_still_extracted(self):
        """/** ... */ block with trailing blank line is still extracted (Javadoc style)."""
        source = b"""/** Doc text. */

int do_work(int x) {
    return 0;
}
"""
        marker = b"int do_work"
        start_byte = source.find(marker)
        func_node = _FakeNode(start_byte=start_byte, end_byte=len(source))
        doc = BaseScanner._extract_doc_comment(func_node, source)
        self.assertIn("Doc text.", doc)

    def test_strips_leading_asterisk_javadoc(self):
        """Leading * on each line is stripped for Javadoc-style block comments."""
        source = b"""/**
 * Line one.
 * Line two.
 */
int foo() { return 0; }
"""
        marker = b"int foo"
        start_byte = source.find(marker)
        func_node = _FakeNode(start_byte=start_byte, end_byte=len(source))
        doc = BaseScanner._extract_doc_comment(func_node, source)
        self.assertIn("Line one.", doc)
        self.assertIn("Line two.", doc)
        # No leading * in output
        self.assertFalse("* Line" in doc)

    def test_none_node_returns_empty(self):
        """None func_node returns empty string."""
        doc = BaseScanner._extract_doc_comment(None, b"")
        self.assertEqual(doc, "")


class TestExtractPythonDocstring(unittest.TestCase):
    """Test the _extract_python_docstring static method."""

    def test_triple_quoted_docstring(self):
        """Triple-quoted docstring as first statement is extracted."""
        # Build: func with body containing expression_statement with string
        string_node = _FakeStringNode('"""This is a docstring."""')
        expr_stmt = _FakeExpressionStatement(string_node)
        body = _FakeBody([expr_stmt])
        func_node = _FakeNode(children=[body], node_type="function_definition")
        doc = BaseScanner._extract_python_docstring(func_node, b'"""This is a docstring."""')
        self.assertEqual(doc, "This is a docstring.")

    def test_single_quoted_docstring(self):
        """Single-line triple-single-quote docstring is extracted."""
        string_node = _FakeStringNode("'''Single line doc.'''")
        expr_stmt = _FakeExpressionStatement(string_node)
        body = _FakeBody([expr_stmt])
        func_node = _FakeNode(children=[body], node_type="function_definition")
        doc = BaseScanner._extract_python_docstring(func_node, b"'''Single line doc.'''")
        self.assertEqual(doc, "Single line doc.")

    def test_no_docstring(self):
        """Function without docstring returns empty string."""
        body = _FakeBody([])  # No expression_statement
        func_node = _FakeNode(children=[body], node_type="function_definition")
        doc = BaseScanner._extract_python_docstring(func_node, b"")
        self.assertEqual(doc, "")

    def test_none_node_returns_empty(self):
        """None func_node returns empty string."""
        doc = BaseScanner._extract_python_docstring(None, b"")
        self.assertEqual(doc, "")


class TestDocCodeAlignDocCommentFallback(unittest.TestCase):
    """Test that doc_code_align uses doc_comment when semantic_desc is unavailable."""

    def test_doc_comment_provides_return_value_claim(self):
        """A doc_comment with return-value claim is used when semantic_desc is missing."""
        from _builder.doc_code_align import _check_return_value_mismatch
        node_data = {
            "name": "do_work",
            "body_text": "int do_work(int x) { return 1; }",
            "signature": "int do_work(int x)",
            "params": ["x"],
            "doc_comment": "Returns 0 on success, 1 on failure.",
            # No semantic_desc / external_desc / api_constraints
        }
        mismatches = _check_return_value_mismatch("node1", node_data)
        # Doc says "returns 0 on success" but code only returns 1 — should flag
        self.assertTrue(any(m.kind == "return_value" and "0" in m.doc_claim
                            for m in mismatches))

    def test_doc_comment_provides_param_mention(self):
        """A doc_comment mentioning a param is used for param-mismatch check."""
        from _builder.doc_code_align import _check_param_mismatch
        node_data = {
            "name": "do_work",
            "body_text": "int do_work(int x) { return x; }",
            "signature": "int do_work(int x)",
            "params": ["x"],
            "doc_comment": "@param y the input value to use",
        }
        mismatches = _check_param_mismatch("node1", node_data)
        # Doc mentions 'y' but actual params are ['x']
        self.assertTrue(any(m.kind == "param_name" and "y" in m.doc_claim
                            for m in mismatches))

    def test_doc_comment_provides_signature(self):
        """A doc_comment with function signature is used for signature-change check."""
        from _builder.doc_code_align import _check_signature_change
        node_data = {
            "name": "do_work",
            "signature": "int do_work(int x, int y)",
            "doc_comment": "int do_work(int x)",
        }
        mismatches = _check_signature_change("node1", node_data)
        # Doc signature differs from actual signature
        self.assertTrue(len(mismatches) > 0)

    def test_check_doc_code_alignment_includes_doc_comment_only_nodes(self):
        """check_doc_code_alignment processes nodes with only doc_comment field."""
        import networkx as nx
        from _builder.doc_code_align import check_doc_code_alignment
        # Build a minimal graph
        G = nx.DiGraph()
        G.add_node("n1", name="do_work", body_text="int do_work(int x) { return 1; }",
                   signature="int do_work(int x)", params=["x"],
                   doc_comment="Returns 0 on success, 1 on failure.")
        # We need to monkeypatch _load_full_graph to return our test graph
        import _builder.doc_code_align as dca
        original_load = dca._builder.graph_build._load_full_graph if hasattr(
            dca, '_builder') else None
        # Use direct patch
        from _builder import graph_build
        original = graph_build._load_full_graph
        graph_build._load_full_graph = lambda gd: G
        try:
            result = check_doc_code_alignment("/fake/path")
            # Should have checked the node
            self.assertEqual(result["checked_count"], 1)
            # Should have detected the return value mismatch
            self.assertGreater(result["mismatched_count"], 0)
            self.assertIn("return_value", result["by_kind"])
        finally:
            graph_build._load_full_graph = original

    def test_check_doc_code_alignment_skips_nodes_without_any_doc(self):
        """check_doc_code_alignment skips nodes that have no doc fields at all."""
        import networkx as nx
        from _builder.doc_code_align import check_doc_code_alignment
        G = nx.DiGraph()
        G.add_node("n1", name="bare_func", body_text="int bare_func() { return 0; }",
                   signature="int bare_func()", params=[])
        # No semantic_desc, external_desc, api_constraints, OR doc_comment
        from _builder import graph_build
        original = graph_build._load_full_graph
        graph_build._load_full_graph = lambda gd: G
        try:
            result = check_doc_code_alignment("/fake/path")
            self.assertEqual(result["checked_count"], 0)
            self.assertEqual(result["mismatched_count"], 0)
        finally:
            graph_build._load_full_graph = original


if __name__ == "__main__":
    unittest.main()
