"""Unit tests for update_cmd.py — update-node / update-edge / patch-profile.

AGENTS.md hard constraint: 'DB writes need user confirmation' — these
tests verify the confirmation gate logic, attribute parsing, value
preview formatting, and storage backend detection.

Coverage:
- _parse_attr_assignments: key=value parsing, JSON value detection,
  empty key rejection, missing '=' rejection
- _format_value_preview: None, dict/list, long string truncation
- _preview_node_changes: diff preview output shape
- _preview_edge_changes: diff preview output shape
- _confirm: auto_yes bypasses prompt, EOFError aborts, 'n' aborts,
  'y' approves
- _detect_backend: json vs sqlite vs missing (sys.exit)
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock


class TestParseAttrAssignments(unittest.TestCase):
    """Tests for _parse_attr_assignments."""

    def test_bare_string_value(self):
        from _builder.update_cmd import _parse_attr_assignments
        result = _parse_attr_assignments(["call_condition=#ifdef X"])
        self.assertEqual(result, {"call_condition": "#ifdef X"})

    def test_json_list_value(self):
        from _builder.update_cmd import _parse_attr_assignments
        result = _parse_attr_assignments(['params=[{"name":"ctx"}]'])
        self.assertEqual(result, {"params": [{"name": "ctx"}]})

    def test_json_dict_value(self):
        from _builder.update_cmd import _parse_attr_assignments
        result = _parse_attr_assignments(['meta={"a":1,"b":2}'])
        self.assertEqual(result, {"meta": {"a": 1, "b": 2}})

    def test_multiple_attrs(self):
        from _builder.update_cmd import _parse_attr_assignments
        result = _parse_attr_assignments(["a=1", "b=2", "c=3"])
        self.assertEqual(result, {"a": "1", "b": "2", "c": "3"})

    def test_missing_equals_raises(self):
        from _builder.update_cmd import _parse_attr_assignments
        with self.assertRaises(ValueError):
            _parse_attr_assignments(["no_equals_here"])

    def test_empty_key_raises(self):
        from _builder.update_cmd import _parse_attr_assignments
        with self.assertRaises(ValueError):
            _parse_attr_assignments(["=value"])

    def test_empty_input_returns_empty(self):
        from _builder.update_cmd import _parse_attr_assignments
        self.assertEqual(_parse_attr_assignments([]), {})
        self.assertEqual(_parse_attr_assignments(None), {})

    def test_whitespace_stripped(self):
        from _builder.update_cmd import _parse_attr_assignments
        result = _parse_attr_assignments(["  key  =  value  "])
        self.assertEqual(result, {"key": "value"})

    def test_invalid_json_falls_back_to_string(self):
        """Values starting with [ or { that aren't valid JSON fall back
        to being stored as bare strings."""
        from _builder.update_cmd import _parse_attr_assignments
        result = _parse_attr_assignments(['broken=[invalid json'])
        self.assertEqual(result, {"broken": "[invalid json"})


class TestFormatValuePreview(unittest.TestCase):
    """Tests for _format_value_preview."""

    def test_none_returns_none_label(self):
        from _builder.update_cmd import _format_value_preview
        self.assertEqual(_format_value_preview(None), "(none)")

    def test_dict_json_serialized(self):
        from _builder.update_cmd import _format_value_preview
        result = _format_value_preview({"a": 1})
        self.assertEqual(result, '{"a": 1}')

    def test_list_json_serialized(self):
        from _builder.update_cmd import _format_value_preview
        result = _format_value_preview([1, 2, 3])
        self.assertEqual(result, '[1, 2, 3]')

    def test_long_string_truncated(self):
        from _builder.update_cmd import _format_value_preview
        long_str = "x" * 300
        result = _format_value_preview(long_str, max_len=50)
        self.assertIn("+250 chars", result)
        self.assertTrue(result.startswith("x" * 50))

    def test_short_string_returned_as_is(self):
        from _builder.update_cmd import _format_value_preview
        self.assertEqual(_format_value_preview("hello"), "hello")


class TestPreviewNodeChanges(unittest.TestCase):
    """Tests for _preview_node_changes."""

    def test_returns_string_with_node_id(self):
        from _builder.update_cmd import _preview_node_changes
        result = _preview_node_changes(
            "my_func", {"old_attr": "x"}, {"new_attr": "y"},
            source="llm", confidence="INFERRED")
        self.assertIsInstance(result, str)
        self.assertIn("my_func", result)
        self.assertIn("llm", result)
        self.assertIn("INFERRED", result)

    def test_includes_old_and_new_values(self):
        from _builder.update_cmd import _preview_node_changes
        result = _preview_node_changes(
            "fn", {"semantic_desc_supplemented": "old_desc"},
            {"semantic_desc": "new_desc"},
            source="llm", confidence="EXTRACTED")
        self.assertIn("old_desc", result)
        self.assertIn("new_desc", result)

    def test_empty_new_attrs_returns_no_changes(self):
        from _builder.update_cmd import _preview_node_changes
        result = _preview_node_changes("fn", {}, {}, source="x", confidence="y")
        self.assertIn("(no attributes to change)", result)


class TestPreviewEdgeChanges(unittest.TestCase):
    """Tests for _preview_edge_changes."""

    def test_returns_string_with_edge(self):
        from _builder.update_cmd import _preview_edge_changes
        result = _preview_edge_changes(
            "caller", "callee", {"old": "x"}, {"new": "y"},
            source="llm", confidence="INFERRED")
        self.assertIsInstance(result, str)
        self.assertIn("caller", result)
        self.assertIn("callee", result)
        self.assertIn("->", result)


class TestConfirm(unittest.TestCase):
    """Tests for _confirm (the AGENTS.md 'DB writes need user
    confirmation' hard constraint enforcement)."""

    def test_auto_yes_bypasses_prompt(self):
        """--yes flag auto-approves without prompting."""
        from _builder.update_cmd import _confirm
        result = _confirm("proceed?", auto_yes=True)
        self.assertTrue(result)

    def test_y_approves(self):
        from _builder.update_cmd import _confirm
        with patch("builtins.input", return_value="y"):
            self.assertTrue(_confirm("proceed?", auto_yes=False))

    def test_yes_approves(self):
        from _builder.update_cmd import _confirm
        with patch("builtins.input", return_value="yes"):
            self.assertTrue(_confirm("proceed?", auto_yes=False))

    def test_n_aborts(self):
        from _builder.update_cmd import _confirm
        with patch("builtins.input", return_value="n"):
            self.assertFalse(_confirm("proceed?", auto_yes=False))

    def test_empty_input_aborts(self):
        from _builder.update_cmd import _confirm
        with patch("builtins.input", return_value=""):
            self.assertFalse(_confirm("proceed?", auto_yes=False))

    def test_eof_aborts_gracefully(self):
        """Non-interactive context (EOFError) → aborts write."""
        from _builder.update_cmd import _confirm
        with patch("builtins.input", side_effect=EOFError):
            self.assertFalse(_confirm("proceed?", auto_yes=False))


class TestDetectBackend(unittest.TestCase):
    """Tests for _detect_backend."""

    def test_json_backend_when_master_json_exists(self):
        from _builder.update_cmd import _detect_backend
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
                json.dump({}, f)
            self.assertEqual(_detect_backend(tmp), "json")

    def test_sqlite_backend_when_db_exists(self):
        from _builder.update_cmd import _detect_backend
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "code2database.db"), "w") as f:
                f.write("")  # empty placeholder
            self.assertEqual(_detect_backend(tmp), "sqlite")

    def test_missing_artifacts_exits(self):
        """When neither master.json nor .db exists, sys.exit(1) is called."""
        from _builder.update_cmd import _detect_backend
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                _detect_backend(tmp)

    def test_json_takes_precedence_over_sqlite(self):
        """When both exist, json backend is preferred."""
        from _builder.update_cmd import _detect_backend
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
                json.dump({}, f)
            with open(os.path.join(tmp, "code2database.db"), "w") as f:
                f.write("")
            self.assertEqual(_detect_backend(tmp), "json")


if __name__ == "__main__":
    unittest.main()
