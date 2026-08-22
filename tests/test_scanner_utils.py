#!/usr/bin/env python3
"""Tests for scanner_utils.py"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from _scanner.utils import classify_domain, EXTENSION_MAP, LANG_EXTENSIONS


class TestClassifyDomain(unittest.TestCase):
    """Test domain classification from file paths."""

    def test_nested_path(self):
        self.assertEqual(classify_domain("/src/lib/bdev/bdev.c", "/src"), "lib.bdev")

    def test_single_dir(self):
        self.assertEqual(classify_domain("/src/lib/utils.c", "/src"), "lib")

    def test_root_file(self):
        self.assertEqual(classify_domain("/src/main.c", "/src"), "root")

    def test_deep_path(self):
        self.assertEqual(classify_domain("/src/module/bdev/nvme/bdev_nvme.c", "/src"), "module.bdev.nvme")

    def test_go_path(self):
        self.assertEqual(classify_domain("/src/pkg/server/handler.go", "/src"), "pkg.server")

    def test_python_path(self):
        self.assertEqual(classify_domain("/src/app/models/user.py", "/src"), "app.models")

    def test_java_path(self):
        self.assertEqual(classify_domain("/src/src/main/java/com/app/App.java", "/src"), "src.main.java.com.app")

    def test_no_source_root(self):
        # When source_root is empty, uses os.path.relpath
        result = classify_domain("lib/bdev/bdev.c", "")
        # Should produce a domain based on the relative path
        self.assertIn("bdev", result)


class TestExtensionMap(unittest.TestCase):
    """Test extension-to-language mapping."""

    def test_c_extension(self):
        self.assertEqual(EXTENSION_MAP.get(".c"), "c")

    def test_cpp_extension(self):
        self.assertIn(".cpp", EXTENSION_MAP)

    def test_go_extension(self):
        self.assertEqual(EXTENSION_MAP.get(".go"), "go")

    def test_python_extension(self):
        self.assertEqual(EXTENSION_MAP.get(".py"), "python")

    def test_java_extension(self):
        self.assertEqual(EXTENSION_MAP.get(".java"), "java")

    def test_rust_extension(self):
        self.assertEqual(EXTENSION_MAP.get(".rs"), "rust")


class TestLangExtensions(unittest.TestCase):
    """Test language-to-extensions mapping."""

    def test_c_has_h(self):
        self.assertIn(".h", LANG_EXTENSIONS.get("c", set()))

    def test_python_has_py(self):
        self.assertIn(".py", LANG_EXTENSIONS.get("python", set()))


if __name__ == "__main__":
    unittest.main()
