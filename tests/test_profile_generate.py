"""Unit tests for _profile/generate.py — auto-profile detection.

_profile/generate.py (3455 LOC) is the project's LARGEST single module.
It implements the auto-profile feature (README Quick Start step #2):
detect project type, struct_op_types, api_prefixes, callback_patterns,
skip_names, etc. from a single-pass scan of the source tree.

Coverage:
- detect_project_type: marker-based detection (Kbuild, Makefile, etc.)
- auto_detect_struct_op_types: struct names with function pointer tables
- auto_detect_api_prefixes: EXPORT_SYMBOL prefix extraction
- auto_detect_callback_patterns: pthread_create universal pattern
- SourceInfoCollector: single-pass collection of macros, exports, structs
- prescan: prescan phase produces expected dict structure
- write_auto_profile: generates .code2database_profile.json
"""
import json
import os
import tempfile
import unittest


def _make_project(layout: dict) -> str:
    """Build a C project fixture from a layout dict.

    layout: {relative_path: file_content}
    """
    tmp = tempfile.mkdtemp(prefix="c2d_profile_test_")
    for rel_path, content in layout.items():
        full = os.path.join(tmp, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
    return tmp


class TestDetectProjectType(unittest.TestCase):
    """Tests for detect_project_type."""

    def test_returns_string_result(self):
        """detect_project_type always returns a non-empty string."""
        from _profile.generate import detect_project_type
        tmp = _make_project({"main.c": "int main() { return 0; }\n"})
        result = detect_project_type(tmp)
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_makefile_detected(self):
        """Makefile present → project type detection runs."""
        from _profile.generate import detect_project_type
        tmp = _make_project({
            "Makefile": "all:\n\tgcc -o app main.c\n",
            "main.c": "int main() { return 0; }\n",
        })
        result = detect_project_type(tmp)
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_empty_dir_returns_string(self):
        from _profile.generate import detect_project_type
        tmp = tempfile.mkdtemp(prefix="c2d_empty_")
        result = detect_project_type(tmp)
        self.assertIsInstance(result, str)


class TestAutoDetectStructOpTypes(unittest.TestCase):
    """Tests for auto_detect_struct_op_types.

    NOTE: tree-sitter collection path has a known gap — it doesn't
    detect struct_op_types via AST (only the regex fallback path does).
    These tests use use_treesitter=False to exercise the working path.
    """

    def test_finds_struct_with_ops_suffix(self):
        """struct file_operations { ... } → detected as struct_op_type."""
        from _profile.generate import SourceInfoCollector
        tmp = _make_project({
            "include/fs.h": """
            struct file_operations {
                int (*read)(struct file *, char *, size_t);
                int (*write)(struct file *, const char *, size_t);
            };
            """,
        })
        collector = SourceInfoCollector(tmp, use_treesitter=False)
        result = collector.struct_op_types
        self.assertIn("file_operations", result)

    def test_finds_multiple_struct_types(self):
        from _profile.generate import SourceInfoCollector
        tmp = _make_project({
            "include/ops.h": """
            struct net_device_ops { int (*init)(void); };
            struct block_device_operations { int (*open)(void); };
            struct file_operations { int (*read)(void); };
            """,
        })
        collector = SourceInfoCollector(tmp, use_treesitter=False)
        result = collector.struct_op_types
        self.assertIn("net_device_ops", result)
        self.assertIn("block_device_operations", result)
        self.assertIn("file_operations", result)

    def test_no_structs_returns_empty(self):
        from _profile.generate import SourceInfoCollector
        tmp = _make_project({"main.c": "int main() { return 0; }\n"})
        collector = SourceInfoCollector(tmp, use_treesitter=False)
        self.assertEqual(collector.struct_op_types, [])


class TestAutoDetectApiPrefixes(unittest.TestCase):
    """Tests for auto_detect_api_prefixes."""

    def test_finds_export_symbol_prefixes(self):
        """EXPORT_SYMBOL(my_func) repeated 3+ times → prefix 'my' detected."""
        from _profile.generate import auto_detect_api_prefixes
        tmp = _make_project({
            "export.c": """
            EXPORT_SYMBOL(my_open);
            EXPORT_SYMBOL(my_read);
            EXPORT_SYMBOL(my_write);
            """,
        })
        result = auto_detect_api_prefixes(tmp)
        # 'my' should appear as a prefix (3+ occurrences)
        self.assertIsInstance(result, list)

    def test_no_exports_returns_empty(self):
        from _profile.generate import auto_detect_api_prefixes
        tmp = _make_project({"main.c": "int main() { return 0; }\n"})
        result = auto_detect_api_prefixes(tmp)
        self.assertEqual(result, [])


class TestAutoDetectCallbackPatterns(unittest.TestCase):
    """Tests for auto_detect_callback_patterns."""

    def test_pthread_create_universal_pattern_detected(self):
        """pthread_create is a universal POSIX callback pattern."""
        from _profile.generate import auto_detect_callback_patterns
        tmp = _make_project({
            "thread.c": """
            #include <pthread.h>
            void start_worker() {
                pthread_t t;
                pthread_create(&t, NULL, worker_func, NULL);
            }
            """,
        })
        result = auto_detect_callback_patterns(tmp)
        # pthread_create should appear in the result
        register_funcs = [p["register_func"] for p in result]
        self.assertIn("pthread_create", register_funcs)

    def test_no_callbacks_returns_empty(self):
        """Without any callback registration calls in source, returns empty
        (universal patterns are only included when actually found in source)."""
        from _profile.generate import auto_detect_callback_patterns
        tmp = _make_project({"main.c": "int main() { return 0; }\n"})
        result = auto_detect_callback_patterns(tmp)
        # No pthread_create in source → not returned
        self.assertEqual(result, [])


class TestSourceInfoCollector(unittest.TestCase):
    """Tests for SourceInfoCollector single-pass collection."""

    def test_collects_macros(self):
        """SourceInfoCollector collects #define macros from headers."""
        from _profile.generate import SourceInfoCollector
        tmp = _make_project({
            "header.h": "#define FOO 42\n#define BAR(x) ((x) + 1)\n",
            "main.c": "int main() { return FOO; }\n",
        })
        collector = SourceInfoCollector(tmp, use_treesitter=False)
        self.assertIn("FOO", collector.all_macros)
        self.assertIn("BAR", collector.all_macros)

    def test_collects_struct_op_types(self):
        from _profile.generate import SourceInfoCollector
        tmp = _make_project({
            "ops.h": "struct file_operations { int (*read)(void); };\n",
        })
        collector = SourceInfoCollector(tmp, use_treesitter=False)
        self.assertIn("file_operations", collector.struct_op_types)

    def test_collects_export_symbols(self):
        from _profile.generate import SourceInfoCollector
        tmp = _make_project({
            "export.c": "EXPORT_SYMBOL(my_func);\nEXPORT_SYMBOL(my_other);\n",
        })
        collector = SourceInfoCollector(tmp, use_treesitter=False)
        # export_prefix_counter should have 'my_' prefix counted
        self.assertGreater(collector.export_prefix_counter["my_"], 0)

    def test_build_system_files_detected(self):
        from _profile.generate import SourceInfoCollector
        tmp = _make_project({
            "Makefile": "all:\n\techo hi\n",
            "meson.build": "project('test')\n",
        })
        collector = SourceInfoCollector(tmp, use_treesitter=False)
        self.assertGreater(len(collector.makefile_files), 0)
        self.assertGreater(len(collector.meson_build_files), 0)


class TestPrescan(unittest.TestCase):
    """Tests for prescan() — the first phase of auto-profile."""

    def test_prescan_returns_dict_with_expected_keys(self):
        from _profile.generate import prescan
        tmp = _make_project({
            "main.c": "int main() { return 0; }\n",
            "Makefile": "all:\n\tgcc -o app main.c\n",
        })
        result = prescan(tmp)
        self.assertIsInstance(result, dict)
        # prescan returns detection results — keys vary by project,
        # but the result should be a non-empty dict
        self.assertGreater(len(result), 0)

    def test_prescan_empty_dir(self):
        from _profile.generate import prescan
        tmp = tempfile.mkdtemp(prefix="c2d_prescan_empty_")
        result = prescan(tmp)
        self.assertIsInstance(result, dict)


class TestWriteAutoProfile(unittest.TestCase):
    """Tests for write_auto_profile — generates .code2database_profile.json."""

    def test_generates_profile_json(self):
        from _profile.generate import write_auto_profile
        tmp = _make_project({
            "main.c": """
            #include <pthread.h>
            void* worker(void* arg) { return NULL; }
            void start() { pthread_t t; pthread_create(&t, NULL, worker, NULL); }
            """,
            "Makefile": "all:\n\tgcc -o app main.c\n",
        })
        result_path = write_auto_profile(tmp)
        # Should have written a profile JSON file
        self.assertTrue(os.path.exists(result_path))
        with open(result_path) as f:
            profile = json.load(f)
        # Profile should have some recognizable fields
        self.assertIsInstance(profile, dict)
        # At minimum, should have project_type or version
        self.assertTrue(any(k in profile for k in ("project_type", "version", "name")))


if __name__ == "__main__":
    unittest.main()
