#!/usr/bin/env python3
"""Tests for scanner_base.py helper methods"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from _scanner.base import BaseScanner


class ConcreteScanner(BaseScanner):
    """Concrete implementation for testing abstract methods."""
    def _parse(self, source_bytes):
        return None

    def _extract(self, tree, source_bytes, filepath, source_root, domain):
        return [], []


class TestNormalizeName(unittest.TestCase):
    def test_basic(self):
        s = ConcreteScanner()
        self.assertEqual(s._normalize_name("hello_world"), "hello_world")

    def test_special_chars(self):
        s = ConcreteScanner()
        result = s._normalize_name("spdk::bdev::open")
        self.assertEqual(result, "spdk__bdev__open")

    def test_dots(self):
        s = ConcreteScanner()
        # _normalize_name does NOT lowercase; that's done in _make_func_id
        result = s._normalize_name("Handler.ServeHTTP")
        self.assertEqual(result, "Handler_ServeHTTP")


class TestMakeFuncId(unittest.TestCase):
    def test_basic(self):
        s = ConcreteScanner()
        result = s._make_func_id("lib.bdev", "spdk_bdev_register")
        self.assertEqual(result, "lib_bdev_spdk_bdev_register")

    def test_root_domain(self):
        s = ConcreteScanner()
        result = s._make_func_id("root", "main")
        self.assertEqual(result, "root_main")


class TestMakeEmptyId(unittest.TestCase):
    def test_basic(self):
        s = ConcreteScanner()
        result = s._make_empty_id("root_main", 0)
        self.assertEqual(result, "root_main__cond_0")

    def test_multiple(self):
        s = ConcreteScanner()
        result = s._make_empty_id("root_main", 2)
        self.assertEqual(result, "root_main__cond_2")


class TestDetectConcurrencyInfo(unittest.TestCase):
    def test_pthread_create(self):
        s = ConcreteScanner()
        result = s._detect_concurrency_info("pthread_create", [
            {"pos": 1, "value": "&tid"},
            {"pos": 2, "value": "NULL"},
            {"pos": 3, "value": "my_thread_fn"},
            {"pos": 4, "value": "NULL"},
        ])
        self.assertTrue(result.get("is_spawn"))
        self.assertEqual(result.get("spawn_target"), "my_thread_fn")

    def test_goroutine(self):
        s = ConcreteScanner()
        result = s._detect_concurrency_info("go", [])
        # "go" is not a direct spawn call name in this context
        # It would be detected differently in the Go scanner

    def test_regular_call(self):
        s = ConcreteScanner()
        result = s._detect_concurrency_info("printf", [
            {"pos": 1, "value": '"hello"'}
        ])
        self.assertFalse(result.get("is_spawn", False))


class TestExtractConditionVars(unittest.TestCase):
    def test_simple_var(self):
        s = ConcreteScanner()
        result = s._extract_condition_vars("mode == 1")
        self.assertIn("mode", result)

    def test_multi_char_vars(self):
        s = ConcreteScanner()
        result = s._extract_condition_vars("count > 0 && level < 10")
        self.assertIn("count", result)
        self.assertIn("level", result)

    def test_single_char_filtered(self):
        # Single-char vars are filtered out (common loop vars like i,j)
        s = ConcreteScanner()
        result = s._extract_condition_vars("a > 0 && b < 10")
        # a, b are single chars so they get filtered
        self.assertNotIn("a", result)

    def test_no_vars(self):
        s = ConcreteScanner()
        result = s._extract_condition_vars("1")
        self.assertEqual(result, [])


class TestConfidenceAndSource(unittest.TestCase):
    def test_confidence_tag(self):
        s = ConcreteScanner()
        self.assertEqual(s._confidence_tag(), "EXTRACTED")

    def test_source_tag(self):
        s = ConcreteScanner()
        self.assertEqual(s._source_tag(), "ast")


class TestDetectApiEntry(unittest.TestCase):
    def test_default_no_api(self):
        s = ConcreteScanner()
        is_api, constraints = s._detect_api_entry("some_func", None, b"")
        self.assertFalse(is_api)
        self.assertEqual(constraints, "")


if __name__ == "__main__":
    unittest.main()
