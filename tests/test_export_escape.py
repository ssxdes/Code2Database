"""Tests for _esc() list-safe HTML escape helper in export.py.

Regression: export.py:284/530 passed node_data['api_constraints']
(a list like ["no_preempt", "irq_safe"]) directly to html.escape(),
which raised AttributeError: 'list' object has no attribute 'replace'.

Also tests normalize_str_field() — the shared helper that coerces
list/tuple/None node-data values to strings for downstream consumers
(search_cmd .lower(), doc_code_align " ".join(), index_pack .replace()).
"""
import os
import sys
import unittest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

from _builder.export.export import _esc  # noqa: E402
from _builder.utils import normalize_str_field  # noqa: E402


class TestEscHelper(unittest.TestCase):
    """_esc() must handle str, list, tuple, None, and other types."""

    def test_plain_string(self):
        self.assertEqual(_esc("hello"), "hello")

    def test_html_chars_escaped(self):
        self.assertEqual(_esc("<script>"), "&lt;script&gt;")

    def test_list_joined_and_escaped(self):
        result = _esc(["no_preempt", "irq_safe"])
        self.assertEqual(result, "no_preempt, irq_safe")

    def test_list_with_html_chars(self):
        result = _esc(["a<b", "c>d"])
        self.assertEqual(result, "a&lt;b, c&gt;d")

    def test_empty_list(self):
        self.assertEqual(_esc([]), "")

    def test_single_element_list(self):
        self.assertEqual(_esc(["only"]), "only")

    def test_tuple_joined(self):
        result = _esc(("alpha", "beta"))
        self.assertEqual(result, "alpha, beta")

    def test_none_returns_empty(self):
        self.assertEqual(_esc(None), "")

    def test_int_coerced(self):
        self.assertEqual(_esc(42), "42")

    def test_dict_coerced(self):
        # dict is not str/list/tuple/None — falls through to str()
        result = _esc({"key": "val"})
        self.assertIn("key", result)
        self.assertIn("val", result)

    def test_quotes_escaped(self):
        result = _esc('say "hi"')
        self.assertIn("&quot;", result)


class TestNormalizeStrField(unittest.TestCase):
    """normalize_str_field() must handle all node-data value types."""

    def test_string_passthrough(self):
        self.assertEqual(normalize_str_field("hello"), "hello")

    def test_empty_string(self):
        self.assertEqual(normalize_str_field(""), "")

    def test_none_returns_empty(self):
        self.assertEqual(normalize_str_field(None), "")

    def test_empty_list_returns_empty(self):
        self.assertEqual(normalize_str_field([]), "")

    def test_non_empty_list_joined(self):
        self.assertEqual(normalize_str_field(["a", "b"]), "a, b")

    def test_single_element_list(self):
        self.assertEqual(normalize_str_field(["x"]), "x")

    def test_tuple_joined(self):
        self.assertEqual(normalize_str_field(("x", "y")), "x, y")

    def test_int_coerced(self):
        self.assertEqual(normalize_str_field(42), "42")

    def test_zero_coerced(self):
        self.assertEqual(normalize_str_field(0), "0")

    def test_false_coerced(self):
        self.assertEqual(normalize_str_field(False), "False")

    def test_nested_list_flattened_as_str(self):
        result = normalize_str_field(["a", ["b", "c"]])
        self.assertIn("a", result)
        self.assertIn("b", result)
        self.assertIn("c", result)

    def test_then_lower_is_safe(self):
        """The downstream pattern: normalize_str_field(x).lower() must
        never raise regardless of input type."""
        for val in [None, [], "", "Hello", ["A", "B"], (1, 2), 42]:
            result = normalize_str_field(val).lower()
            self.assertIsInstance(result, str)

    def test_then_replace_is_safe(self):
        """The downstream pattern: normalize_str_field(x).replace(...) must
        never raise regardless of input type."""
        for val in [None, [], "", "a|b", ["a|b", "c|d"]]:
            result = normalize_str_field(val).replace("|", "\\|")
            self.assertIsInstance(result, str)


if __name__ == "__main__":
    unittest.main()
