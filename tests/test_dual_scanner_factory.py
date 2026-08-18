"""Tests for the get_scanner() factory with extraction_backend (Phase 1).

Verifies that get_scanner() returns the correct scanner type for each
backend mode:
- extraction_backend='auto' (default) → DualBackendScanner for c/cpp
- extraction_backend='clang' → ClangScanner for c/cpp
- extraction_backend='tree-sitter' → CTreeSitterScanner for c/cpp
- Non-c/cpp languages ignore extraction_backend
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


class TestGetScannerExtractionBackend(unittest.TestCase):

    def test_default_returns_dual_scanner_for_c(self):
        """Default extraction_backend='auto' returns DualBackendScanner for c."""
        try:
            from _scanner.clang_scanner import is_clang_available
        except ImportError:
            self.skipTest("ClangScanner not available")
        if not is_clang_available():
            self.skipTest("libclang not available")
        from code2database_scanner import get_scanner
        from _scanner.dual_scanner import DualBackendScanner
        scanner = get_scanner("c", profile={"extraction_backend": "auto"})
        self.assertIsInstance(scanner, DualBackendScanner)

    def test_clang_backend_returns_clang_scanner_for_c(self):
        """extraction_backend='clang' returns ClangScanner for c."""
        try:
            from _scanner.clang_scanner import is_clang_available
        except ImportError:
            self.skipTest("ClangScanner not available")
        if not is_clang_available():
            self.skipTest("libclang not available")
        from code2database_scanner import get_scanner
        from _scanner.clang_scanner import ClangScanner
        scanner = get_scanner("c", extraction_backend="clang")
        self.assertIsInstance(scanner, ClangScanner)

    def test_tree_sitter_backend_returns_cts_scanner(self):
        """extraction_backend='tree-sitter' returns CTreeSitterScanner for c."""
        from code2database_scanner import get_scanner
        from _scanner.c_scanner import CTreeSitterScanner
        scanner = get_scanner("c", extraction_backend="tree-sitter")
        self.assertIsInstance(scanner, CTreeSitterScanner)

    def test_non_c_lang_ignores_extraction_backend(self):
        """Non-c/cpp languages ignore extraction_backend."""
        from code2database_scanner import get_scanner
        from _scanner.go_scanner import GoTreeSitterScanner
        scanner = get_scanner("go", extraction_backend="clang")
        self.assertIsInstance(scanner, GoTreeSitterScanner)

    def test_default_profile_extraction_backend_is_auto(self):
        """Default profile has extraction_backend='auto'."""
        from _profile.schema import ProfileSchema
        profile = ProfileSchema.defaults()
        self.assertEqual(profile.raw.get("extraction_backend"), "auto")

    def test_profile_extraction_backend_passed_through(self):
        """Profile extraction_backend is honored by get_scanner."""
        try:
            from _scanner.clang_scanner import is_clang_available
        except ImportError:
            self.skipTest("ClangScanner not available")
        if not is_clang_available():
            self.skipTest("libclang not available")
        from code2database_scanner import get_scanner
        from _scanner.clang_scanner import ClangScanner
        scanner = get_scanner("cpp", profile={"extraction_backend": "clang"})
        self.assertIsInstance(scanner, ClangScanner)


if __name__ == "__main__":
    unittest.main()
