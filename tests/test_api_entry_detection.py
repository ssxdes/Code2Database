#!/usr/bin/env python3
"""Tests for API_entry detection across C, Python, and Rust scanners."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))


class TestCApiEntryDetection(unittest.TestCase):
    """Test C scanner API_entry detection with profile-driven config."""

    def _scan_c(self, code, filepath_suffix='.c', api_prefixes=None,
                export_macros=None, public_header_paths=None, source_root=None):
        """Scan C code and return result dict."""
        from _scanner.c_scanner import CTreeSitterScanner
        scanner = CTreeSitterScanner(is_cpp=False)
        if api_prefixes:
            scanner._api_prefixes = api_prefixes
        if export_macros:
            scanner._export_macros = export_macros
        if public_header_paths:
            scanner._public_header_paths = public_header_paths

        with tempfile.NamedTemporaryFile(suffix=filepath_suffix, mode='w',
                                         delete=False) as f:
            f.write(code)
            f.flush()
            if source_root is None:
                source_root = os.path.dirname(f.name)
            result = scanner.scan_file(f.name, source_root=source_root)
        os.unlink(f.name)
        return result

    def test_default_public_header_patterns_match_lib(self):
        """Without profile, lib/ path functions are marked API_entry (legacy behavior)."""
        code = """
int lib_func(void) {
    return 42;
}
"""
        # Use a source_root such that file appears under lib/
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_dir = os.path.join(tmpdir, "lib")
            os.makedirs(lib_dir)
            fpath = os.path.join(lib_dir, "test.c")
            with open(fpath, 'w') as f:
                f.write(code)
            from _scanner.c_scanner import CTreeSitterScanner
            scanner = CTreeSitterScanner(is_cpp=False)
            result = scanner.scan_file(fpath, source_root=tmpdir)
        func = next((f for f in result["functions"] if f["name"] == "lib_func"), None)
        self.assertIsNotNone(func)
        self.assertIn("API_entry", func["labels"])

    def test_profile_public_header_paths_overrides_default(self):
        """With profile public_header_paths=['include'], lib/ path is NOT API."""
        code = """
int lib_func(void) {
    return 42;
}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_dir = os.path.join(tmpdir, "lib")
            os.makedirs(lib_dir)
            fpath = os.path.join(lib_dir, "test.c")
            with open(fpath, 'w') as f:
                f.write(code)
            from _scanner.c_scanner import CTreeSitterScanner
            scanner = CTreeSitterScanner(is_cpp=False)
            scanner._public_header_paths = ["include"]
            result = scanner.scan_file(fpath, source_root=tmpdir)
        func = next((f for f in result["functions"] if f["name"] == "lib_func"), None)
        self.assertIsNotNone(func)
        self.assertNotIn("API_entry", func["labels"])

    def test_profile_public_header_paths_include_matches(self):
        """With profile public_header_paths=['include'], include/ path IS API."""
        code = """
int exported_func(void) {
    return 42;
}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            inc_dir = os.path.join(tmpdir, "include")
            os.makedirs(inc_dir)
            fpath = os.path.join(inc_dir, "test.h")
            with open(fpath, 'w') as f:
                f.write(code)
            from _scanner.c_scanner import CTreeSitterScanner
            scanner = CTreeSitterScanner(is_cpp=False)
            scanner._public_header_paths = ["include"]
            result = scanner.scan_file(fpath, source_root=tmpdir)
        func = next((f for f in result["functions"] if f["name"] == "exported_func"), None)
        self.assertIsNotNone(func)
        self.assertIn("API_entry", func["labels"])

    def test_static_function_never_api(self):
        """Static functions are never API_entry regardless of path."""
        code = """
static int internal_func(void) {
    return 1;
}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            inc_dir = os.path.join(tmpdir, "include")
            os.makedirs(inc_dir)
            fpath = os.path.join(inc_dir, "test.c")
            with open(fpath, 'w') as f:
                f.write(code)
            from _scanner.c_scanner import CTreeSitterScanner
            scanner = CTreeSitterScanner(is_cpp=False)
            scanner._public_header_paths = ["include"]
            result = scanner.scan_file(fpath, source_root=tmpdir)
        func = next((f for f in result["functions"] if f["name"] == "internal_func"), None)
        self.assertIsNotNone(func)
        self.assertNotIn("API_entry", func["labels"])

    def test_export_macro_overrides(self):
        """EXPORT_SYMBOL marks function as API_entry regardless of other rules."""
        code = """
static int __init_func(void) {
    return 1;
}
EXPORT_SYMBOL(__init_func);
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, "test.c")
            with open(fpath, 'w') as f:
                f.write(code)
            from _scanner.c_scanner import CTreeSitterScanner
            scanner = CTreeSitterScanner(is_cpp=False)
            scanner._export_macros = ["EXPORT_SYMBOL"]
            result = scanner.scan_file(fpath, source_root=tmpdir)
        func = next((f for f in result["functions"] if f["name"] == "__init_func"), None)
        self.assertIsNotNone(func)
        self.assertIn("API_entry", func["labels"])

    def test_zstd_internal_not_api_with_profile(self):
        """zstd BIT_* functions in lib/zstd/ are NOT API with profile override."""
        code = """
int BIT_initCStream(void) {
    return 0;
}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            zstd_dir = os.path.join(tmpdir, "lib", "zstd", "common")
            os.makedirs(zstd_dir)
            fpath = os.path.join(zstd_dir, "bitstream.h")
            with open(fpath, 'w') as f:
                f.write(code)
            from _scanner.c_scanner import CTreeSitterScanner
            scanner = CTreeSitterScanner(is_cpp=False)
            scanner._public_header_paths = ["include"]
            result = scanner.scan_file(fpath, source_root=tmpdir)
        func = next((f for f in result["functions"] if f["name"] == "BIT_initCStream"), None)
        self.assertIsNotNone(func)
        self.assertNotIn("API_entry", func["labels"])

    def test_tools_path_not_api(self):
        """Functions in tools/ directory are NOT API entries."""
        code = """
int perf_event_open(void) {
    return 0;
}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tools_dir = os.path.join(tmpdir, "tools", "perf", "util")
            os.makedirs(tools_dir)
            fpath = os.path.join(tools_dir, "evsel.c")
            with open(fpath, 'w') as f:
                f.write(code)
            from _scanner.c_scanner import CTreeSitterScanner
            scanner = CTreeSitterScanner(is_cpp=False)
            result = scanner.scan_file(fpath, source_root=tmpdir)
        func = next((f for f in result["functions"] if f["name"] == "perf_event_open"), None)
        self.assertIsNotNone(func)
        self.assertNotIn("API_entry", func["labels"])

    def test_selftests_path_not_api(self):
        """Functions in selftests/ directory are NOT API entries."""
        code = """
int test_function(void) {
    return 0;
}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = os.path.join(tmpdir, "tools", "testing", "selftests", "net")
            os.makedirs(test_dir)
            fpath = os.path.join(test_dir, "test.c")
            with open(fpath, 'w') as f:
                f.write(code)
            from _scanner.c_scanner import CTreeSitterScanner
            scanner = CTreeSitterScanner(is_cpp=False)
            result = scanner.scan_file(fpath, source_root=tmpdir)
        func = next((f for f in result["functions"] if f["name"] == "test_function"), None)
        self.assertIsNotNone(func)
        self.assertNotIn("API_entry", func["labels"])

    def test_profile_non_api_paths_excludes_test_dir(self):
        """Profile-declared non_api_paths (e.g., 'test/') exclude API_entry tagging.

        Reproduces the SPDK bug where main() in test/unit/.../foo_ut.c was
        wrongly tagged API_entry because the scanner's hardcoded non-API list
        only covers tools/, scripts/, samples/, etc. — not project-specific
        test/ paths. The profile's project_boundaries.non_api_paths must
        propagate to the scanner via _non_api_paths.
        """
        code = """
int rpc_test_helper(void) {
    return 0;
}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = os.path.join(tmpdir, "test", "unit", "lib", "foo")
            os.makedirs(test_dir)
            fpath = os.path.join(test_dir, "foo_ut.c")
            with open(fpath, 'w') as f:
                f.write(code)
            from _scanner.c_scanner import CTreeSitterScanner
            scanner = CTreeSitterScanner(is_cpp=False)
            # Simulate profile propagation done by scanner factory:
            scanner._api_prefixes = ["rpc_"]
            scanner._public_header_paths = ["include/spdk"]
            scanner._non_api_paths = ["test/", "examples/", "app/"]
            result = scanner.scan_file(fpath, source_root=tmpdir)
        func = next((f for f in result["functions"] if f["name"] == "rpc_test_helper"), None)
        self.assertIsNotNone(func, "rpc_test_helper should be extracted")
        self.assertNotIn("API_entry", func["labels"],
                         "rpc_test_helper in test/ path must NOT be API_entry when "
                         "profile.project_boundaries.non_api_paths contains 'test/'")

    def test_macro_body_with_token_paste_is_skipped(self):
        """Functions whose signature contains token-paste (##) are macro-body
        artifacts and must be skipped — never tagged API_entry.

        Reproduces the SPDK bug where _SPLAY_MINMAX (a BSD tree.h macro
        defined inside #define SPLAY_PROTOTYPE) was extracted as a function
        and tagged API_entry because it lives in include/spdk/tree.h.
        Tree-sitter parses the macro body as a function_definition because
        the preprocessor body has C-like syntax, but the `##` token paste
        in `void name##_SPLAY_MINMAX(...)` is a definitive marker that
        this is not real code.
        """
        code = """
#ifndef TREE_H
#define TREE_H

#define SPLAY_PROTOTYPE(name, type, field, cmp) \\
void name##_SPLAY_MINMAX(struct name *head, int __comp) \\
{ \\
    struct type __node, *__left, *__right; \\
    __left = __right = &__node; \\
    while (1) { \\
        if (__comp < 0) { \\
            __tmp = SPLAY_LEFT((head)->sph_root, field); \\
        } \\
    } \\
}

#endif
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            inc_dir = os.path.join(tmpdir, "include", "spdk")
            os.makedirs(inc_dir)
            fpath = os.path.join(inc_dir, "tree.h")
            with open(fpath, 'w') as f:
                f.write(code)
            from _scanner.c_scanner import CTreeSitterScanner
            scanner = CTreeSitterScanner(is_cpp=False)
            scanner._public_header_paths = ["include/spdk"]
            result = scanner.scan_file(fpath, source_root=tmpdir)
        func = next((f for f in result["functions"]
                     if f.get("name") == "_SPLAY_MINMAX"), None)
        self.assertIsNone(func,
                           "_SPLAY_MINMAX (a macro-body artifact with `##` in "
                           "the signature) must NOT be extracted as a function")


class TestPythonApiEntryDetection(unittest.TestCase):
    """Test Python scanner API_entry detection."""

    def _scan_py(self, code, filepath_suffix='.py', source_root_override=None):
        """Scan Python code and return result dict."""
        from _scanner.python_scanner import PythonTreeSitterScanner
        scanner = PythonTreeSitterScanner()
        with tempfile.NamedTemporaryFile(suffix=filepath_suffix, mode='w',
                                         delete=False) as f:
            f.write(code)
            f.flush()
            sr = source_root_override if source_root_override else os.path.dirname(f.name)
            result = scanner.scan_file(f.name, source_root=sr)
        os.unlink(f.name)
        return result

    def test_module_level_function_is_api(self):
        """Module-level public functions are API entries."""
        code = """
def process_data():
    pass
"""
        result = self._scan_py(code)
        func = next((f for f in result["functions"] if f["name"] == "process_data"), None)
        self.assertIsNotNone(func)
        self.assertIn("API_entry", func["labels"])

    def test_private_function_not_api(self):
        """Private functions (starting with _) are not API entries."""
        code = """
def _internal_helper():
    pass
"""
        result = self._scan_py(code)
        func = next((f for f in result["functions"] if f["name"] == "_internal_helper"), None)
        self.assertIsNotNone(func)
        self.assertNotIn("API_entry", func["labels"])

    def test_class_method_not_api(self):
        """Class methods are NOT API entries (changed: only module-level functions)."""
        code = """
class MyHandler:
    def process(self):
        pass

    def handle_event(self):
        pass
"""
        result = self._scan_py(code)
        for func in result["functions"]:
            self.assertNotIn("API_entry", func["labels"],
                             f"Class method {func['name']} should not be API_entry")

    def test_tools_path_excluded(self):
        """Functions in tools/ directory are not API entries."""
        code = """
def AddSubWindow():
    pass
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tools_dir = os.path.join(tmpdir, "tools", "perf", "scripts")
            os.makedirs(tools_dir)
            fpath = os.path.join(tools_dir, "test.py")
            with open(fpath, 'w') as f:
                f.write(code)
            from _scanner.python_scanner import PythonTreeSitterScanner
            scanner = PythonTreeSitterScanner()
            result = scanner.scan_file(fpath, source_root=tmpdir)
        func = next((f for f in result["functions"] if f["name"] == "AddSubWindow"), None)
        self.assertIsNotNone(func)
        self.assertNotIn("API_entry", func["labels"])

    def test_selftests_path_excluded(self):
        """Functions in selftests/ directory are not API entries."""
        code = """
class ArrayParser:
    def parse(self):
        pass
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = os.path.join(tmpdir, "tools", "testing", "selftests", "bpf")
            os.makedirs(test_dir)
            fpath = os.path.join(test_dir, "test.py")
            with open(fpath, 'w') as f:
                f.write(code)
            from _scanner.python_scanner import PythonTreeSitterScanner
            scanner = PythonTreeSitterScanner()
            result = scanner.scan_file(fpath, source_root=tmpdir)
        for func in result["functions"]:
            self.assertNotIn("API_entry", func["labels"])

    def test_scripts_path_excluded(self):
        """Functions in scripts/ directory are not API entries."""
        code = """
def build_project():
    pass
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            scripts_dir = os.path.join(tmpdir, "scripts", "build")
            os.makedirs(scripts_dir)
            fpath = os.path.join(scripts_dir, "builder.py")
            with open(fpath, 'w') as f:
                f.write(code)
            from _scanner.python_scanner import PythonTreeSitterScanner
            scanner = PythonTreeSitterScanner()
            result = scanner.scan_file(fpath, source_root=tmpdir)
        func = next((f for f in result["functions"] if f["name"] == "build_project"), None)
        self.assertIsNotNone(func)
        self.assertNotIn("API_entry", func["labels"])

    def test_normal_path_module_function_still_api(self):
        """Normal path module-level functions are still API entries."""
        code = """
def calculate(x, y):
    return x + y
"""
        result = self._scan_py(code)
        func = next((f for f in result["functions"] if f["name"] == "calculate"), None)
        self.assertIsNotNone(func)
        self.assertIn("API_entry", func["labels"])


class TestRustApiEntryDetection(unittest.TestCase):
    """Test Rust scanner API_entry detection."""

    def _scan_rust(self, code, filepath_suffix='.rs', source_root_override=None,
                   api_prefixes=None):
        """Scan Rust code and return result dict."""
        from _scanner.rust_scanner import RustTreeSitterScanner
        scanner = RustTreeSitterScanner()
        if api_prefixes:
            scanner._api_prefixes = api_prefixes
        with tempfile.NamedTemporaryFile(suffix=filepath_suffix, mode='w',
                                         delete=False) as f:
            f.write(code)
            f.flush()
            sr = source_root_override if source_root_override else os.path.dirname(f.name)
            result = scanner.scan_file(f.name, source_root=sr)
        os.unlink(f.name)
        return result

    def test_pub_fn_is_api(self):
        """pub fn is API entry (normal case)."""
        code = """pub fn process_data() -> i32 {
    42
}
"""
        result = self._scan_rust(code)
        func = next((f for f in result["functions"] if f["name"] == "process_data"), None)
        self.assertIsNotNone(func)
        self.assertIn("API_entry", func["labels"])

    def test_private_fn_not_api(self):
        """Non-pub fn is NOT API entry."""
        code = """fn internal_helper() -> i32 {
    0
}
"""
        result = self._scan_rust(code)
        func = next((f for f in result["functions"] if f["name"] == "internal_helper"), None)
        self.assertIsNotNone(func)
        self.assertNotIn("API_entry", func["labels"])

    def test_pub_crate_not_api(self):
        """pub(crate) fn is NOT API entry (restricted visibility)."""
        code = """pub(crate) fn crate_only() -> i32 {
    1
}
"""
        result = self._scan_rust(code)
        func = next((f for f in result["functions"] if f["name"] == "crate_only"), None)
        self.assertIsNotNone(func)
        self.assertNotIn("API_entry", func["labels"])

    def test_std_crate_excluded(self):
        """pub fn in std/ directory is NOT API (standard library)."""
        code = """pub fn from_raw(ptr: *const T) -> Self {
    Self { ptr }
}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            std_dir = os.path.join(tmpdir, "rust", "std", "sync")
            os.makedirs(std_dir)
            fpath = os.path.join(std_dir, "arc.rs")
            with open(fpath, 'w') as f:
                f.write(code)
            from _scanner.rust_scanner import RustTreeSitterScanner
            scanner = RustTreeSitterScanner()
            result = scanner.scan_file(fpath, source_root=tmpdir)
        func = next((f for f in result["functions"] if f["name"] == "from_raw"), None)
        self.assertIsNotNone(func)
        self.assertNotIn("API_entry", func["labels"])

    def test_generic_impl_std_container_excluded(self):
        """Arc<T>::method is NOT API (standard container pattern)."""
        code = """pub fn try_new() -> Result<Self, AllocError> {
    Ok(Self { inner: Box::try_new(Inner { data: T::init() })? })
}
"""
        # This should be detected as API_entry if NOT a std container name
        # But if the name matches Arc<T>::, Box<T>::, etc., it should be excluded
        # Simulate by scanning normally - the func_name will just be "try_new"
        # We need to test with impl blocks
        code2 = """pub struct Arc<T: ?Sized> {}

impl<T: ?Sized> Arc<T> {
    pub fn as_arc_borrow(&self) -> &T {
        &self.inner
    }
}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, "arc.rs")
            with open(fpath, 'w') as f:
                f.write(code2)
            from _scanner.rust_scanner import RustTreeSitterScanner
            scanner = RustTreeSitterScanner()
            result = scanner.scan_file(fpath, source_root=tmpdir)
        func = next((f for f in result["functions"]
                     if "Arc" in f["name"] and "as_arc_borrow" in f["name"]), None)
        if func:
            self.assertNotIn("API_entry", func["labels"],
                             "Arc<T>::method should not be API_entry")

    def test_test_path_excluded(self):
        """pub fn in tests/ directory is NOT API."""
        code = """pub fn test_something() {
    assert_eq!(1 + 1, 2);
}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = os.path.join(tmpdir, "tests", "unit")
            os.makedirs(test_dir)
            fpath = os.path.join(test_dir, "test.rs")
            with open(fpath, 'w') as f:
                f.write(code)
            from _scanner.rust_scanner import RustTreeSitterScanner
            scanner = RustTreeSitterScanner()
            result = scanner.scan_file(fpath, source_root=tmpdir)
        func = next((f for f in result["functions"] if f["name"] == "test_something"), None)
        self.assertIsNotNone(func)
        self.assertNotIn("API_entry", func["labels"])

    def test_api_prefixes_filter(self):
        """With api_prefixes configured, only matching functions are API."""
        code = """pub fn sys_open() -> i32 { 0 }

pub fn random_func() -> i32 { 1 }
"""
        from _scanner.rust_scanner import RustTreeSitterScanner
        scanner = RustTreeSitterScanner()
        scanner._api_prefixes = ["sys_"]
        result = self._scan_rust(code, api_prefixes=["sys_"])
        sys_func = next((f for f in result["functions"] if f["name"] == "sys_open"), None)
        other_func = next((f for f in result["functions"] if f["name"] == "random_func"), None)
        self.assertIsNotNone(sys_func)
        self.assertIsNotNone(other_func)
        self.assertIn("API_entry", sys_func["labels"])
        self.assertNotIn("API_entry", other_func["labels"])


class TestApiEntryIntegration(unittest.TestCase):
    """Integration: verify get_scanner passes profile config to all scanners."""

    def test_get_scanner_passes_public_header_paths_to_c(self):
        """get_scanner sets _public_header_paths on C scanner from profile."""
        from code2database_scanner import get_scanner
        scanner = get_scanner("c", api_prefixes=["sys_"],
                              export_macros=["EXPORT_SYMBOL"],
                              profile={"public_header_paths": ["include"]})
        self.assertEqual(scanner._api_prefixes, ["sys_"])
        self.assertEqual(scanner._export_macros, ["EXPORT_SYMBOL"])
        self.assertEqual(scanner._public_header_paths, ["include"])

    def test_get_scanner_passes_api_prefixes_to_rust(self):
        """get_scanner sets _api_prefixes on Rust scanner from profile."""
        from code2database_scanner import get_scanner
        scanner = get_scanner("rust", api_prefixes=["sys_"],
                              profile={})
        self.assertEqual(scanner._api_prefixes, ["sys_"])

    def test_get_scanner_no_public_header_paths_without_profile(self):
        """Without profile, C scanner uses default _PUBLIC_HEADER_PATTERNS (empty list)."""
        from code2database_scanner import get_scanner
        scanner = get_scanner("c")
        # _public_header_paths is initialized as empty list;
        # the C scanner's _detect_api_entry falls back to hardcoded defaults
        self.assertEqual(scanner._public_header_paths, [])

    def test_get_scanner_passes_non_api_paths_to_c(self):
        """get_scanner propagates project_boundaries.non_api_paths to C scanner."""
        from code2database_scanner import get_scanner
        scanner = get_scanner(
            "c",
            profile={
                "public_header_paths": ["include/spdk"],
                "non_api_paths": ["test/", "examples/", "app/"],
            },
        )
        self.assertEqual(scanner._non_api_paths, ["test/", "examples/", "app/"])

    def test_get_scanner_passes_non_api_paths_to_go(self):
        """get_scanner propagates non_api_paths to Go scanner too.

        Reproduces the SPDK bug where examples/go/hello_gorpc/hello_gorpc.go
        had its main() tagged API_entry because the Go scanner never saw
        the profile's non_api_paths.
        """
        try:
            from code2database_scanner import get_scanner
            scanner = get_scanner(
                "go",
                profile={"non_api_paths": ["test/", "examples/"]},
            )
        except ImportError:
            self.skipTest("tree-sitter-go not installed")
        self.assertEqual(scanner._non_api_paths, ["test/", "examples/"])


class TestGoApiEntryDetection(unittest.TestCase):
    """Test Go scanner API_entry detection with profile non_api_paths."""

    def test_main_in_examples_dir_not_api_with_profile(self):
        """main() in examples/ path is NOT API_entry when profile excludes examples/."""
        try:
            from _scanner.go_scanner import GoTreeSitterScanner
        except ImportError:
            self.skipTest("tree-sitter-go not installed")
        code = """package main

func main() {
}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            go_dir = os.path.join(tmpdir, "examples", "go", "hello_gorpc")
            os.makedirs(go_dir)
            fpath = os.path.join(go_dir, "hello_gorpc.go")
            with open(fpath, 'w') as f:
                f.write(code)
            scanner = GoTreeSitterScanner()
            scanner._non_api_paths = ["test/", "examples/", "app/"]
            result = scanner.scan_file(fpath, source_root=tmpdir)
        func = next((f for f in result["functions"] if f["name"] == "main"), None)
        self.assertIsNotNone(func)
        self.assertNotIn("API_entry", func["labels"],
                         "main() in examples/ must NOT be API_entry when profile "
                         "declares examples/ as a non-API path")

    def test_test_file_function_not_api_even_without_profile(self):
        """Functions in *_test.go files are NOT API_entry regardless of profile.

        Go has a universal convention: files ending in _test.go are test
        files. The tool must respect this without requiring per-project
        profile configuration.
        """
        try:
            from _scanner.go_scanner import GoTreeSitterScanner
        except ImportError:
            self.skipTest("tree-sitter-go not installed")
        code = """package client

import "testing"

func Test_createRequest(t *testing.T) {
    t.Run("sub", func(t *testing.T) {})
}

func HelperFunction(x int) int {
    return x * 2
}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            go_dir = os.path.join(tmpdir, "go", "rpc", "client")
            os.makedirs(go_dir)
            fpath = os.path.join(go_dir, "client_test.go")
            with open(fpath, 'w') as f:
                f.write(code)
            scanner = GoTreeSitterScanner()
            scanner._non_api_paths = []  # no profile exclusions
            result = scanner.scan_file(fpath, source_root=tmpdir)
        # Test_createRequest must NOT be API_entry (it's in a _test.go file)
        func = next((f for f in result["functions"] if f["name"] == "Test_createRequest"), None)
        self.assertIsNotNone(func, "Test_createRequest should be extracted")
        self.assertNotIn("API_entry", func["labels"],
                         "Functions in _test.go files must NOT be API_entry")
        # HelperFunction in the same _test.go file is a test helper, not API
        helper = next((f for f in result["functions"] if f["name"] == "HelperFunction"), None)
        if helper is not None:
            self.assertNotIn("API_entry", helper["labels"],
                             "Helper functions in _test.go files must NOT be API_entry")

    def test_test_function_name_pattern_not_api_in_non_test_file(self):
        """Functions named Test*/Benchmark*/Example*/Fuzz* with test runner
        signatures are NOT API_entry even in non-_test.go files.

        This guards against false positives where a real exported function
        happens to start with "Test" — the signature check ensures only
        actual test entry points (TestXxx(*testing.T) etc.) are filtered.
        """
        try:
            from _scanner.go_scanner import GoTreeSitterScanner
        except ImportError:
            self.skipTest("tree-sitter-go not installed")
        code = """package client

import "testing"

func TestSomething(t *testing.T) {
}

func BenchmarkSomething(b *testing.B) {
}

func TestController() int {
    return 42
}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            go_dir = os.path.join(tmpdir, "pkg", "control")
            os.makedirs(go_dir)
            fpath = os.path.join(go_dir, "control.go")  # NOT _test.go
            with open(fpath, 'w') as f:
                f.write(code)
            scanner = GoTreeSitterScanner()
            scanner._non_api_paths = []
            result = scanner.scan_file(fpath, source_root=tmpdir)
        # TestSomething(t *testing.T) and BenchmarkSomething(b *testing.B)
        # are test entry points — must NOT be API_entry
        test_fn = next((f for f in result["functions"] if f["name"] == "TestSomething"), None)
        self.assertIsNotNone(test_fn)
        self.assertNotIn("API_entry", test_fn["labels"],
                         "TestXxx(*testing.T) must NOT be API_entry")
        bench_fn = next((f for f in result["functions"] if f["name"] == "BenchmarkSomething"), None)
        self.assertIsNotNone(bench_fn)
        self.assertNotIn("API_entry", bench_fn["labels"],
                         "BenchmarkXxx(*testing.B) must NOT be API_entry")
        # TestController() returns int — doesn't match test signature,
        # should remain API_entry (uppercase exported function)
        ctrl_fn = next((f for f in result["functions"] if f["name"] == "TestController"), None)
        if ctrl_fn is not None:
            self.assertIn("API_entry", ctrl_fn["labels"],
                          "TestController() with no *testing.T param is a real "
                          "exported function, should remain API_entry")


if __name__ == "__main__":
    unittest.main()
