"""Unit tests for line_utils.py — O(log N) line-number lookup helpers."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.line_utils import build_line_starts, line_for_offset, lines_for_matches


class TestBuildLineStarts(unittest.TestCase):
    def test_empty_text(self):
        self.assertEqual(build_line_starts(""), [0])

    def test_single_line_no_newline(self):
        self.assertEqual(build_line_starts("hello"), [0])

    def test_three_lines(self):
        # 'a\nb\nc' → offsets 0, 2, 4
        self.assertEqual(build_line_starts("a\nb\nc"), [0, 2, 4])

    def test_trailing_newline_adds_empty_line(self):
        # 'a\n' → offsets 0 and 2 (the empty string after the newline)
        self.assertEqual(build_line_starts("a\n"), [0, 2])

    def test_empty_lines_counted(self):
        # '\n\n' → three (empty) lines starting at 0, 1, 2
        self.assertEqual(build_line_starts("\n\n"), [0, 1, 2])

    def test_length_matches_newline_count_plus_one(self):
        text = "line1\nline2\nline3\nline4"
        starts = build_line_starts(text)
        self.assertEqual(len(starts), text.count("\n") + 1)

    def test_starts_are_sorted_offsets_of_real_lines(self):
        text = "alpha\nbeta\ngamma\n"
        starts = build_line_starts(text)
        for i, off in enumerate(starts):
            # each line's text begins at its recorded offset
            end = starts[i + 1] - 1 if i + 1 < len(starts) else len(text)
            self.assertEqual(text[off:end], ["alpha", "beta", "gamma", ""][i])


class TestLineForOffset(unittest.TestCase):
    def setUp(self):
        # 'a\nb\nc' — line starts at 0, 2, 4
        self.starts = [0, 2, 4]

    def test_offset_zero_is_line_1(self):
        self.assertEqual(line_for_offset(self.starts, 0), 1)

    def test_mid_line_offsets(self):
        self.assertEqual(line_for_offset(self.starts, 1), 1)   # 'a'
        self.assertEqual(line_for_offset(self.starts, 2), 2)   # 'b'
        self.assertEqual(line_for_offset(self.starts, 3), 2)
        self.assertEqual(line_for_offset(self.starts, 4), 3)   # 'c'
        self.assertEqual(line_for_offset(self.starts, 5), 3)   # past end

    def test_empty_starts_returns_line_1(self):
        self.assertEqual(line_for_offset([], 42), 1)


class TestLinesForMatches(unittest.TestCase):
    def test_match_line_numbers_one_based(self):
        text = "int foo(void);\nint bar(void);\nint baz(void);"
        matches = list(re.finditer(r"int (\w+)", text))
        lines = lines_for_matches(text, matches)
        self.assertEqual(lines, [1, 2, 3])

    def test_multiple_matches_same_line(self):
        text = "foo(1); foo(2);\nfoo(3);"
        matches = list(re.finditer(r"foo", text))
        self.assertEqual(lines_for_matches(text, matches), [1, 1, 2])

    def test_no_matches(self):
        text = "nothing here"
        self.assertEqual(lines_for_matches(text, []), [])

    def test_matches_on_empty_lines(self):
        text = "\n\nx"
        matches = list(re.finditer("x", text))
        self.assertEqual(lines_for_matches(text, matches), [3])


if __name__ == "__main__":
    unittest.main()
