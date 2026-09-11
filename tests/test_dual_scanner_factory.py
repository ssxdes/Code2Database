"""Tests for the get_scanner() factory with extraction_backend.

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


class _FakeScanner:
    """Minimal scanner stand-in returning a canned scan_file result."""

    def __init__(self, result):
        self._result = result

    def scan_file(self, filepath, source_root, macro_bindings=None):
        return dict(self._result)


class TestDualScannerCgdbMerge(unittest.TestCase):
    """The dual-backend merge must cover every cgdb_* key either scanner
    produces, with clang canonical when it parsed the file."""

    def _merge(self, ts_result, clang_result):
        from _scanner.dual_scanner import DualBackendScanner
        dual = DualBackendScanner(_FakeScanner(ts_result),
                                  _FakeScanner(clang_result))
        return dual.scan_file("a.c", "/src")

    def test_clang_key_not_in_any_list_merges(self):
        """A cgdb_* key the merge never hard-listed (e.g., a newly added
        scanner layer) still flows into the merged result."""
        merged = self._merge(
            {"functions": [], "edges": []},
            {"cgdb_nodes": [{"id": 1}], "cgdb_macros": [{"name": "M"}]})
        self.assertEqual(merged.get("cgdb_macros"), [{"name": "M"}])

    def test_tree_sitter_only_key_kept_when_clang_succeeded(self):
        """Keys only tree-sitter produces (cgdb_includes) keep the
        tree-sitter value when clang parsed the file."""
        merged = self._merge(
            {"functions": [], "edges": [],
             "cgdb_includes": [{"line": 3, "path": "x.h"}]},
            {"cgdb_nodes": [{"id": 1}]})
        self.assertEqual(merged.get("cgdb_includes"),
                         [{"line": 3, "path": "x.h"}])

    def test_clang_overrides_tree_sitter_for_shared_key(self):
        merged = self._merge(
            {"functions": [], "edges": [],
             "cgdb_nodes": [{"id": "ts"}]},
            {"cgdb_nodes": [{"id": "clang"}]})
        self.assertEqual(merged.get("cgdb_nodes"), [{"id": "clang"}])

    def test_tree_sitter_kept_when_clang_failed(self):
        merged = self._merge(
            {"functions": [], "edges": [],
             "cgdb_nodes": [{"id": "ts"}],
             "cgdb_includes": [{"line": 1}]},
            {"error": "libclang parse failed", "cgdb_nodes": []})
        self.assertEqual(merged.get("cgdb_nodes"), [{"id": "ts"}])
        self.assertEqual(merged.get("cgdb_includes"), [{"line": 1}])

    def test_clang_base_used_when_tree_sitter_failed(self):
        merged = self._merge(
            {"error": "ts parse failed"},
            {"cgdb_nodes": [{"id": "clang"}], "cgdb_types": [{"t": 1}]})
        self.assertEqual(merged.get("cgdb_nodes"), [{"id": "clang"}])
        self.assertEqual(merged.get("cgdb_types"), [{"t": 1}])

    def test_warnings_recorded_and_not_merged_as_data(self):
        merged = self._merge(
            {"functions": [], "edges": []},
            {"error": "parse failed", "cgdb_nodes": []})
        self.assertEqual(merged.get("cgdb_warnings"), ["clang: parse failed"])


if __name__ == "__main__":
    unittest.main()
