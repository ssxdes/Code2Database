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


if __name__ == "__main__":
    unittest.main()
