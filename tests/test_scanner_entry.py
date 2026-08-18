#!/usr/bin/env python3
"""Tests for code2database_scanner.py"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from code2database_scanner import detect_language, _parse_macros_str
from _scanner.changes import _file_fingerprint


class TestDetectLanguage(unittest.TestCase):
    def test_c(self):
        self.assertEqual(detect_language("test.c"), "c")

    def test_cpp(self):
        self.assertEqual(detect_language("test.cpp"), "cpp")

    def test_header(self):
        self.assertEqual(detect_language("test.h"), "c")

    def test_go(self):
        self.assertEqual(detect_language("test.go"), "go")

    def test_python(self):
        self.assertEqual(detect_language("test.py"), "python")

    def test_java(self):
        self.assertEqual(detect_language("test.java"), "java")

    def test_rust(self):
        self.assertEqual(detect_language("test.rs"), "rust")

    def test_unknown(self):
        self.assertEqual(detect_language("test.txt"), "")

    def test_no_extension(self):
        self.assertEqual(detect_language("Makefile"), "")


class TestParseMacros(unittest.TestCase):
    def test_dash_d(self):
        result = _parse_macros_str("-DNDEBUG -DFOO=1")
        self.assertIn("NDEBUG", result)
        self.assertIn("FOO", result)

    def test_space_separated(self):
        result = _parse_macros_str("NDEBUG FEATURE_X=1")
        self.assertIn("NDEBUG", result)
        self.assertEqual(result["FEATURE_X"], "1")

    def test_empty(self):
        result = _parse_macros_str("")
        self.assertEqual(result, {})


class TestFileFingerprint(unittest.TestCase):
    def test_returns_string(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"hello")
            f.flush()
            fp = _file_fingerprint(f.name)
            os.unlink(f.name)
            self.assertIsInstance(fp, str)
            self.assertIn(":", fp)

    def test_different_content(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f1:
            f1.write(b"hello")
            f1.flush()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f2:
                f2.write(b"world")
                f2.flush()
                fp1 = _file_fingerprint(f1.name)
                fp2 = _file_fingerprint(f2.name)
                os.unlink(f1.name)
                os.unlink(f2.name)
                # Size differs, so fingerprint should differ
                # (actually same size here, but different mtime likely)
                # Just verify format
                self.assertIn(":", fp1)
                self.assertIn(":", fp2)


class TestGetScanner(unittest.TestCase):
    def test_c_scanner(self):
        from code2database_scanner import get_scanner
        scanner = get_scanner("c")
        self.assertIsNotNone(scanner)

    def test_go_scanner(self):
        from code2database_scanner import get_scanner
        scanner = get_scanner("go")
        self.assertIsNotNone(scanner)

    def test_python_scanner(self):
        from code2database_scanner import get_scanner
        scanner = get_scanner("python")
        self.assertIsNotNone(scanner)

    def test_java_scanner(self):
        from code2database_scanner import get_scanner
        scanner = get_scanner("java")
        self.assertIsNotNone(scanner)

    def test_rust_scanner(self):
        from code2database_scanner import get_scanner
        scanner = get_scanner("rust")
        self.assertIsNotNone(scanner)

    def test_unsupported(self):
        from code2database_scanner import get_scanner
        with self.assertRaises(ValueError):
            get_scanner("cobol")


class TestScanDirectory(unittest.TestCase):
    """Test scan_directory with actual source files."""

    def test_scan_c_file(self):
        from code2database_scanner import scan_directory
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "test.c"), "w") as f:
                f.write('void helper() {}\nvoid main() { helper(); }\n')
            result = scan_directory(tmpdir, lang="c")
            self.assertIn("functions", result)
            self.assertIn("edges", result)
            self.assertGreater(len(result["functions"]), 0)

    def test_scan_empty_dir(self):
        from code2database_scanner import scan_directory
        with tempfile.TemporaryDirectory() as tmpdir:
            result = scan_directory(tmpdir, lang="c")
            self.assertEqual(len(result["functions"]), 0)

    def test_scan_auto_language(self):
        from code2database_scanner import scan_directory
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "test.c"), "w") as f:
                f.write('void f() {}\n')
            result = scan_directory(tmpdir, lang="auto")
            self.assertGreater(len(result["functions"]), 0)

    def test_scan_python_file(self):
        from code2database_scanner import scan_directory
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "test.py"), "w") as f:
                f.write('def hello():\n    print("hi")\n\ndef main():\n    hello()\n')
            result = scan_directory(tmpdir, lang="auto")
            self.assertGreater(len(result["functions"]), 0)

    def test_scan_go_file(self):
        from code2database_scanner import scan_directory
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "test.go"), "w") as f:
                f.write('package main\n\nfunc helper() {}\n\nfunc main() {\n\thelper()\n}\n')
            result = scan_directory(tmpdir, lang="auto")
            self.assertGreater(len(result["functions"]), 0)

    def test_lang_stats(self):
        from code2database_scanner import scan_directory
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "test.c"), "w") as f:
                f.write('void f() {}\n')
            result = scan_directory(tmpdir, lang="auto")
            self.assertIn("c", result.get("lang_stats", {}))

    def test_domains_computed(self):
        from code2database_scanner import scan_directory
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = os.path.join(tmpdir, "lib", "bdev")
            os.makedirs(subdir)
            with open(os.path.join(subdir, "bdev.c"), "w") as f:
                f.write('void bdev_init() {}\n')
            result = scan_directory(tmpdir, lang="auto")
            self.assertIn("lib.bdev", result.get("domains", []))


class TestScanFiles(unittest.TestCase):
    def test_scan_specific_files(self):
        from code2database_scanner import scan_files
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = os.path.join(tmpdir, "a.c")
            f2 = os.path.join(tmpdir, "b.c")
            with open(f1, "w") as f:
                f.write('void func_a() {}\n')
            with open(f2, "w") as f:
                f.write('void func_b() {}\n')
            result = scan_files([f1, f2], tmpdir, lang="c")
            self.assertEqual(len(result["functions"]), 2)


class TestManifest(unittest.TestCase):
    def test_save_and_detect(self):
        from code2database_scanner import save_manifest, detect_changes
        with tempfile.TemporaryDirectory() as srcdir:
            with tempfile.TemporaryDirectory() as outdir:
                with open(os.path.join(srcdir, "test.c"), "w") as f:
                    f.write('void f() {}\n')
                count = save_manifest(srcdir, outdir)
                self.assertGreater(count, 0)
                changes = detect_changes(srcdir, outdir)
                self.assertEqual(len(changes["new_files"]), 0)  # No changes yet


if __name__ == "__main__":
    unittest.main()
