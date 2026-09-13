"""clang_scanner compile-commands handling and libclang gating.

Covers:
- relative include flags in compile_commands.json resolve against the
  entry's 'directory' field (spec-compliant), not the process CWD
- libclang availability probing must not claim availability when the
  shared library cannot actually be loaded
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _scanner.clang_scanner import (
    ClangScanner, _LIBCLANG_AVAILABLE,
)


class TestCompileDbIncludeResolution(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="c2d_ccdb_")
        self.build_dir = os.path.join(self.tmpdir, "build")
        os.makedirs(self.build_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_db(self, entry):
        db_path = os.path.join(self.tmpdir, "compile_commands.json")
        with open(db_path, "w", encoding="utf-8") as f:
            json.dump([entry], f)
        return db_path

    def test_attached_relative_include_resolved(self):
        db = self._write_db({
            "directory": self.build_dir,
            "file": "src/foo.c",
            "arguments": ["cc", "-Iinc", "-c", "src/foo.c"],
        })
        scanner = ClangScanner(compile_commands_path=db)
        scanner._load_compile_commands()
        cached = list(scanner._compile_db_cache.values())
        self.assertEqual(len(cached), 1)
        self.assertIn("-I" + os.path.join(self.build_dir, "inc"),
                      cached[0],
                      "attached -I<rel> must resolve against 'directory'")
        self.assertNotIn("-Iinc", cached[0])

    def test_separate_relative_include_resolved(self):
        db = self._write_db({
            "directory": self.build_dir,
            "file": "src/foo.c",
            "arguments": ["cc", "-I", "inc", "-c", "src/foo.c"],
        })
        scanner = ClangScanner(compile_commands_path=db)
        scanner._load_compile_commands()
        cached = list(scanner._compile_db_cache.values())
        self.assertEqual(len(cached), 1)
        self.assertIn(os.path.join(self.build_dir, "inc"), cached[0],
                      "separate '-I <rel>' must resolve against 'directory'")
        self.assertNotIn("inc", cached[0])

    def test_isystem_and_absolute_untouched(self):
        db = self._write_db({
            "directory": self.build_dir,
            "file": "src/foo.c",
            "arguments": ["cc", "-isystem", "sys", "-I/abs/inc",
                          "-c", "src/foo.c"],
        })
        scanner = ClangScanner(compile_commands_path=db)
        scanner._load_compile_commands()
        cached = list(scanner._compile_db_cache.values())[0]
        self.assertIn(os.path.join(self.build_dir, "sys"), cached)
        self.assertIn("-I/abs/inc", cached,
                      "absolute include paths must pass through unchanged")

    @unittest.skipUnless(_LIBCLANG_AVAILABLE, "libclang bindings not installed")
    def test_is_clang_available_is_honest(self):
        """Availability must mean the Index can actually be created.

        A True answer with a broken library sends the pipeline down the
        clang path where every file fails to parse.
        """
        from _scanner import clang_scanner as _cs
        import clang.cindex as _ci
        self.assertTrue(_cs._configure_libclang())
        try:
            _ci.Index.create()
        except Exception as exc:  # pragma: no cover - environment signal
            self.fail("configure returned True but Index.create() raised: %s"
                      % exc)

    @unittest.skipUnless(_LIBCLANG_AVAILABLE, "libclang bindings not installed")
    def test_probe_result_is_cached_and_reset_safe(self):
        """With no configured library the probe runs once and is cached."""
        from _scanner import clang_scanner as _cs
        import clang.cindex as _ci
        _old_paths = _cs._LIBCLANG_PATHS
        _old_lib = getattr(_ci.Config, "library_file", None)
        _cs._config_probe_result = None
        try:
            _cs._LIBCLANG_PATHS = []
            _ci.Config.library_file = None
            first = _cs._configure_libclang()
            self.assertIsNotNone(
                _cs._config_probe_result,
                "the Index probe must run when no library path is set")
            self.assertIsInstance(first, bool)
            self.assertEqual(_cs._configure_libclang(), first,
                             "the second call must reuse the cached probe")
        finally:
            _cs._LIBCLANG_PATHS = _old_paths
            _cs._config_probe_result = None
            if _old_lib:
                # set_library_file refuses once the library is loaded;
                # restore the plain attribute instead.
                _ci.Config.library_file = _old_lib


@unittest.skipUnless(_LIBCLANG_AVAILABLE, "libclang bindings not installed")
class TestClangScanFileLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="c2d_clangfile_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_operator_call_produces_invokes_edge(self):
        """Empty-spelling CALL_EXPRs must still create INVOKES edges.

        ``f(1)`` through an overloaded operator() has an empty spelling
        on the CALL_EXPR — the old guard dropped those calls entirely.
        """
        from _scanner.clang_scanner import ClangScanner
        src = os.path.join(self.tmpdir, "ops.cpp")
        with open(src, "w") as f:
            f.write("struct F {\n"
                    "    int operator()(int x) { return x + 1; }\n"
                    "};\n"
                    "int run(void) {\n"
                    "    F f;\n"
                    "    return f(1);\n"
                    "}\n")
        scanner = ClangScanner(is_cpp=True)
        result = scanner.scan_file(src, self.tmpdir)
        self.assertNotIn("error", result,
                         "scan failed: %s" % result.get("error"))
        names = [n.get("name", "") for n in result.get("cgdb_nodes", [])]
        self.assertTrue(any("operator()" in n for n in names),
                        "operator() callee must be recorded, got %s" % names)
        invokes = [e for e in result.get("cgdb_edges", [])
                   if e.get("kind") == "INVOKES"]
        self.assertGreaterEqual(
            len(invokes), 1,
            "the empty-spelling call f(1) must produce an INVOKES edge")

    def test_tu_disposed_after_scan(self):
        """The TranslationUnit must be disposed deterministically."""
        from _scanner import clang_scanner as _cs
        from _scanner.clang_scanner import ClangScanner
        src = os.path.join(self.tmpdir, "plain.c")
        with open(src, "w") as f:
            f.write("int twice(int x) { return x * 2; }\n"
                    "int main(void) { return twice(1); }\n")

        class _TrackedTU:
            def __init__(self, tu):
                self._tu = tu
                self.disposed = False

            def dispose(self):
                self.disposed = True
                try:
                    self._tu.dispose()
                except Exception:
                    pass

            def __getattr__(self, name):
                return getattr(self._tu, name)

        _real_parse = ClangScanner._parse_path
        tracked = []

        def _tracked_parse(self, filepath):
            tu = _real_parse(self, filepath)
            if tu is not None:
                tu = _TrackedTU(tu)
                tracked.append(tu)
            return tu

        ClangScanner._parse_path = _tracked_parse
        try:
            scanner = ClangScanner()
            result = scanner.scan_file(src, self.tmpdir)
        finally:
            ClangScanner._parse_path = _real_parse
        self.assertNotIn("error", result)
        self.assertEqual(len(tracked), 1)
        self.assertTrue(tracked[0].disposed,
                        "scan_file must dispose the TU it parsed")


if __name__ == "__main__":
    unittest.main()
