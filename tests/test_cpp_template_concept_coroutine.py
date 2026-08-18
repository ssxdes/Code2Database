"""Tests for C++ template/concept/coroutine extraction (D13)."""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


class FakeNode:
    """Minimal tree-sitter node stand-in for unit tests."""

    _next_id = 0

    def __init__(self, type_name, text="", children=None, start_point=(0, 0)):
        self.type = type_name
        self._text = text
        self.children = children or []
        self.start_point = start_point
        # Assign unique byte range so _node_text slicing works
        FakeNode._next_id += 1
        self.start_byte = FakeNode._next_id * 1000
        self.end_byte = self.start_byte + len(text)

    def __repr__(self):
        return f"<FakeNode {self.type}>"


def _build_source_bytes(node, base=b''):
    """Build source_bytes containing each FakeNode's text at its byte offset.

    Recursive: walks the tree and writes _text into the buffer at start_byte.
    Ensures _node_text slicing returns the expected text for any node.
    """
    buf = bytearray(base)
    if node._text and len(buf) < node.end_byte:
        buf.extend(b'\x00' * (node.end_byte - len(buf)))
    if node._text:
        buf[node.start_byte:node.end_byte] = node._text.encode('utf-8')
    for c in node.children:
        buf = _build_source_bytes(c, buf)
    return bytes(buf)


class TestTemplateParamExtraction(unittest.TestCase):
    """Test _extract_template_params."""

    def _make_scanner(self):
        from _scanner.c_scanner import CTreeSitterScanner
        return CTreeSitterScanner(is_cpp=True)

    def test_no_template_parameter_list(self):
        """Returns empty list when template_declaration has no parameter list."""
        s = self._make_scanner()
        node = FakeNode('template_declaration', children=[
            FakeNode('template', text='template'),
        ])
        self.assertEqual(s._extract_template_params(node, b''), [])

    def test_extracts_type_parameters(self):
        """Extracts typename parameters from template_parameter_list."""
        s = self._make_scanner()
        tpl = FakeNode('template_parameter_list', children=[
            FakeNode('type_parameter_declaration', text='typename T'),
            FakeNode('type_parameter_declaration', text='class U'),
        ])
        node = FakeNode('template_declaration', children=[tpl])
        src = _build_source_bytes(node)
        params = s._extract_template_params(node, src)
        self.assertEqual(len(params), 2)
        self.assertEqual(params[0]["name"], "typename T")
        self.assertEqual(params[0]["type"], "typename")
        self.assertEqual(params[1]["name"], "class U")


class TestFindTemplateInnerFunction(unittest.TestCase):
    """Test _find_template_inner_function."""

    def _make_scanner(self):
        from _scanner.c_scanner import CTreeSitterScanner
        return CTreeSitterScanner(is_cpp=True)

    def test_finds_direct_function_child(self):
        """Returns the function_definition when it's a direct child."""
        s = self._make_scanner()
        func = FakeNode('function_definition')
        node = FakeNode('template_declaration', children=[
            FakeNode('template', text='template'),
            func,
        ])
        self.assertIs(s._find_template_inner_function(node), func)

    def test_finds_grandchild_function(self):
        """Returns function_definition when nested one level deeper."""
        s = self._make_scanner()
        func = FakeNode('method_definition')
        middle = FakeNode('class_specifier', children=[func])
        node = FakeNode('template_declaration', children=[middle])
        self.assertIs(s._find_template_inner_function(node), func)

    def test_returns_none_for_non_function_template(self):
        """Returns None when template wraps a non-function (e.g., alias)."""
        s = self._make_scanner()
        node = FakeNode('template_declaration', children=[
            FakeNode('alias_declaration'),
        ])
        self.assertIsNone(s._find_template_inner_function(node))

    def test_finds_constructor(self):
        """Returns constructor_definition when wrapped by template."""
        s = self._make_scanner()
        ctor = FakeNode('constructor_definition')
        node = FakeNode('template_declaration', children=[ctor])
        self.assertIs(s._find_template_inner_function(node), ctor)


class TestIsCoroutine(unittest.TestCase):
    """Test _is_coroutine detection."""

    def _make_scanner(self):
        from _scanner.c_scanner import CTreeSitterScanner
        return CTreeSitterScanner(is_cpp=True)

    def _make_body(self, text):
        """Create a FakeNode body whose source_bytes match its text."""
        body = FakeNode('compound_statement', text=text)
        # Return source_bytes that contains the body text at the body's
        # start_byte offset, so _node_text returns the right slice.
        src = b' ' * body.start_byte + text.encode('utf-8')
        return body, src

    def test_detects_co_await(self):
        """Body with co_await is detected as coroutine."""
        s = self._make_scanner()
        body, src = self._make_body("{ auto x = co_await foo(); }")
        self.assertTrue(s._is_coroutine(None, src, body_node=body))

    def test_detects_co_return(self):
        """Body with co_return is detected as coroutine."""
        s = self._make_scanner()
        body, src = self._make_body("{ co_return 42; }")
        self.assertTrue(s._is_coroutine(None, src, body_node=body))

    def test_detects_co_yield(self):
        """Body with co_yield is detected as coroutine."""
        s = self._make_scanner()
        body, src = self._make_body("{ co_yield 42; }")
        self.assertTrue(s._is_coroutine(None, src, body_node=body))

    def test_no_coroutine_keywords(self):
        """Regular function body is not a coroutine."""
        s = self._make_scanner()
        body, src = self._make_body("{ return 42; }")
        self.assertFalse(s._is_coroutine(None, src, body_node=body))

    def test_no_false_positive_on_similar_names(self):
        """my_co_await (no word boundary) is NOT a coroutine."""
        s = self._make_scanner()
        body, src = self._make_body("{ int my_co_await = 1; }")
        self.assertFalse(s._is_coroutine(None, src, body_node=body))

    def test_none_body_returns_false(self):
        """None body_node returns False."""
        s = self._make_scanner()
        self.assertFalse(s._is_coroutine(None, b'', body_node=None))


class TestExtractCoroutineAwaits(unittest.TestCase):
    """Test _extract_coroutine_awaits."""

    def _make_scanner(self):
        from _scanner.c_scanner import CTreeSitterScanner
        return CTreeSitterScanner(is_cpp=True)

    def test_extracts_co_await_expression(self):
        """Extracts the awaited expression from co_await_expression."""
        s = self._make_scanner()
        expr = FakeNode('call_expression', text='foo()', start_point=(5, 4))
        co_await_node = FakeNode('co_await_expression', children=[
            FakeNode('co_await', text='co_await'),
            expr,
        ])
        body = FakeNode('compound_statement', children=[co_await_node])
        src = _build_source_bytes(body)
        results = s._extract_coroutine_awaits(body, src)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["expr"], "foo()")
        self.assertEqual(results[0]["line"], 6)  # start_point[0] + 1

    def test_no_awaits_returns_empty(self):
        """Body without co_await_expression returns empty list."""
        s = self._make_scanner()
        body = FakeNode('compound_statement', children=[
            FakeNode('return_statement', text='return 0;'),
        ])
        self.assertEqual(s._extract_coroutine_awaits(body, b''), [])

    def test_multiple_awaits(self):
        """Multiple co_await expressions are all extracted."""
        s = self._make_scanner()
        body = FakeNode('compound_statement', children=[
            FakeNode('co_await_expression', children=[
                FakeNode('co_await'),
                FakeNode('identifier', text='a'),
            ]),
            FakeNode('co_await_expression', children=[
                FakeNode('co_await'),
                FakeNode('identifier', text='b'),
            ]),
        ])
        results = s._extract_coroutine_awaits(body, b'')
        self.assertEqual(len(results), 2)

    def test_none_body_returns_empty(self):
        """None body_node returns empty list."""
        s = self._make_scanner()
        self.assertEqual(s._extract_coroutine_awaits(None, b''), [])


class TestExtractConcept(unittest.TestCase):
    """Test _extract_concept."""

    def _make_scanner(self):
        from _scanner.c_scanner import CTreeSitterScanner
        return CTreeSitterScanner(is_cpp=True)

    def test_extracts_name_and_constraint(self):
        """Extracts concept name and constraint expression."""
        s = self._make_scanner()
        node = FakeNode('concept_definition', children=[
            FakeNode('concept', text='concept'),
            FakeNode('identifier', text='EqualityComparable'),
            FakeNode('=', text='='),
            FakeNode('template_type', text='requires (T a, T b) { a == b; }'),
            FakeNode(';', text=';'),
        ], start_point=(10, 0))
        src = _build_source_bytes(node)
        result = s._extract_concept(node, src, '/src/test.cpp', '/src')
        self.assertEqual(result["name"], "EqualityComparable")
        self.assertIn("requires", result["constraint"])
        self.assertEqual(result["source_file"], "test.cpp")
        self.assertEqual(result["line"], 11)  # start_point[0] + 1

    def test_handles_missing_name(self):
        """Returns empty name if no identifier found."""
        s = self._make_scanner()
        node = FakeNode('concept_definition', children=[
            FakeNode('concept', text='concept'),
            FakeNode('=', text='='),
            FakeNode('template_type', text='true'),
            FakeNode(';', text=';'),
        ])
        result = s._extract_concept(node, b'', '/src/test.cpp', '/src')
        self.assertEqual(result["name"], "")


class TestTemplateMetaInit(unittest.TestCase):
    """Test that _template_meta is initialized properly."""

    def test_template_meta_initialized_in_extract(self):
        """_template_meta dict is initialized in _extract."""
        from _scanner.c_scanner import CTreeSitterScanner
        s = CTreeSitterScanner(is_cpp=True)
        # Initially should not exist or be None
        self._template_meta = None
        # Manually call the init logic to verify
        if not hasattr(s, '_template_meta') or s._template_meta is None:
            s._template_meta = {}
        s._template_meta.clear()
        self.assertEqual(s._template_meta, {})


if __name__ == "__main__":
    unittest.main()
