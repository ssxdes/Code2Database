#!/usr/bin/env python3
"""Tests for code2database_patcher.py — diff parsing, patching, light scan."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from _builder.patcher import _parse_unified_diff, lazy_fill_node
from _builder.utils import _detect_language_from_path
import networkx as nx


class TestDiffParsing(unittest.TestCase):
    def test_simple_add(self):
        diff = """diff --git a/main.c b/main.c
index 1234567..abcdef 100644
--- a/main.c
+++ b/main.c
@@ -10,5 +10,8 @@ int main() {
     foo();
+    bar();
     return 0;
"""
        result = _parse_unified_diff(diff)
        self.assertIn("main.c", result["modified_files"])
        self.assertEqual(len(result["hunks"]), 1)
        # Added line "bar()" is at new file line 11 (10+1 context + 1 added)
        self.assertGreater(len(result["hunks"][0]["added_lines"]), 0)

    def test_new_file(self):
        diff = """diff --git a/newfile.c b/newfile.c
new file mode 100644
--- /dev/null
+++ b/newfile.c
@@ -0,0 +1,5 @@
+#include <stdio.h>
+int new_func() {
+    return 42;
+}
"""
        result = _parse_unified_diff(diff)
        self.assertIn("newfile.c", result["added_files"])

    def test_deleted_file(self):
        diff = """diff --git a/oldfile.c b/oldfile.c
deleted file mode 100644
--- a/oldfile.c
+++ /dev/null
@@ -1,3 +0,0 @@
-int old_func() {
-    return 0;
-}
"""
        result = _parse_unified_diff(diff)
        self.assertIn("oldfile.c", result["deleted_files"])

    def test_multiple_hunks(self):
        diff = """diff --git a/test.c b/test.c
--- a/test.c
+++ b/test.c
@@ -5,3 +5,4 @@ void foo() {
     bar();
+    baz();
 }
@@ -20,3 +21,4 @@ void qux() {
     quux();
+    corge();
 }
"""
        result = _parse_unified_diff(diff)
        self.assertEqual(len(result["hunks"]), 2)

    def test_empty_diff(self):
        result = _parse_unified_diff("")
        self.assertEqual(len(result["hunks"]), 0)


class TestDetectLanguage(unittest.TestCase):
    def test_c(self):
        self.assertEqual(_detect_language_from_path("main.c"), "c")

    def test_cpp(self):
        self.assertEqual(_detect_language_from_path("main.cpp"), "cpp")

    def test_go(self):
        self.assertEqual(_detect_language_from_path("main.go"), "go")

    def test_python(self):
        self.assertEqual(_detect_language_from_path("main.py"), "python")

    def test_java(self):
        self.assertEqual(_detect_language_from_path("Main.java"), "java")

    def test_rust(self):
        self.assertEqual(_detect_language_from_path("main.rs"), "rust")

    def test_unknown(self):
        self.assertEqual(_detect_language_from_path("data.json"), "")


class TestLazyFillNode(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sourcedir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        shutil.rmtree(self.sourcedir, ignore_errors=True)

    def test_fill_from_source(self):
        # Create a test source file
        src_path = os.path.join(self.sourcedir, "test.c")
        with open(src_path, "w") as f:
            f.write("#include <stdio.h>\nvoid my_func() {\n    printf(\"hello\");\n}\n")

        G = nx.DiGraph()
        G.add_node("root_my_func",
                    name="my_func", source_file="test.c", line=2,
                    stale=True, semantic_desc="", domain="root")

        result = lazy_fill_node(G, "root_my_func", self.sourcedir)
        self.assertIn("body_text", result)
        self.assertIn("my_func", result["body_text"])
        self.assertEqual(result["stale"], False)

    def test_no_fill_needed(self):
        G = nx.DiGraph()
        G.add_node("root_my_func",
                    name="my_func", source_file="test.c", line=2,
                    stale=False, semantic_desc="already described", domain="root")

        result = lazy_fill_node(G, "root_my_func", "/tmp")
        self.assertEqual(result, {})

    def test_missing_file(self):
        G = nx.DiGraph()
        G.add_node("root_my_func",
                    name="my_func", source_file="nonexistent.c", line=2,
                    stale=True, semantic_desc="", domain="root")

        result = lazy_fill_node(G, "root_my_func", "/tmp/nonexistent")
        self.assertEqual(result, {})

    def test_no_line_number(self):
        G = nx.DiGraph()
        G.add_node("root_my_func",
                    name="my_func", source_file="test.c", line=0,
                    stale=True, semantic_desc="", domain="root")

        result = lazy_fill_node(G, "root_my_func", self.sourcedir)
        self.assertEqual(result, {})


class TestCheckUpdateThreshold(unittest.TestCase):
    def test_empty_graph(self):
        # With no manifest, should return needs_full_scan or ratio 0
        tmpdir = tempfile.mkdtemp()
        try:
            from _builder.patcher import check_update_threshold
            result = check_update_threshold(self.sourcedir if hasattr(self, 'sourcedir') else "/tmp", tmpdir)
            self.assertIn("change_ratio", result)
            self.assertIn("needs_semantic_update", result)
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
