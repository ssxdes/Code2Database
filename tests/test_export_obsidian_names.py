"""Tests for Obsidian note-name sanitization in export.py.

Kernel inline-asm "functions" have names that are entire assembly
snippets — hundreds of bytes with tabs, quotes and newlines. Using them
directly as .md file names crashed export-obsidian with
OSError [Errno 36] (NAME_MAX = 255 bytes) and left Obsidian-illegal
characters (:, |, #, [, ]) in file names. Wiki-links must use the same
sanitized name, or every link to a renamed note would stop resolving.
"""
import argparse
import json
import os
import sys
import tempfile
import unittest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

from _builder.export.export import (  # noqa: E402
    _safe_note_name,
    _NOTE_NAME_MAX_BYTES,
)


ASM_SNIPPET_NAME = (
    '"addi\tsp, sp, -"riscv_szptr  "_n"\n'
    '\t\treg_s"  ra, (sp)\t\t_n"\n'
    '\t\t"__fentry__"  t0, (sp)\t\t_n"\n'
    + 'x' * 340
)


class TestSafeNoteName(unittest.TestCase):
    """_safe_note_name unit behavior."""

    def test_plain_identifier_unchanged(self):
        self.assertEqual(_safe_note_name("foo_bar42"), "foo_bar42")

    def test_path_separators_replaced(self):
        self.assertEqual(_safe_note_name("a/b\\c"), "a_b_c")

    def test_obidian_illegal_characters_replaced(self):
        # :, |, #, [, ], ^ are legal on Linux ext4 but rejected by
        # Obsidian and/or Windows filesystems.
        for ch in ":|#[]^":
            self.assertNotIn(ch, _safe_note_name(f"ns{ch}fn"))

    def test_control_characters_replaced(self):
        self.assertNotIn("\t", _safe_note_name("a\tb"))
        self.assertNotIn("\n", _safe_note_name("a\nb"))
        self.assertNotIn('"', _safe_note_name('a"b'))

    def test_unicode_word_chars_kept(self):
        # Python identifiers may be non-ASCII; CJK chars are 3 UTF-8
        # bytes each and must survive (and be counted as bytes).
        self.assertEqual(_safe_note_name("获取数据"), "获取数据")

    def test_long_asm_snippet_truncated_with_hash(self):
        safe = _safe_note_name(ASM_SNIPPET_NAME)
        self.assertLessEqual(len(safe.encode("utf-8")), _NOTE_NAME_MAX_BYTES)
        self.assertIn("_", safe)
        # The md5 suffix keeps distinct long names distinct.
        other = _safe_note_name(ASM_SNIPPET_NAME + "different tail")
        self.assertNotEqual(safe, other)

    def test_truncation_is_deterministic(self):
        self.assertEqual(_safe_note_name(ASM_SNIPPET_NAME),
                         _safe_note_name(ASM_SNIPPET_NAME))

    def test_cjk_name_over_byte_budget_truncated_safely(self):
        # 100 CJK chars = 300 UTF-8 bytes > budget: must truncate to a
        # valid char boundary (no partial multibyte char).
        name = "函" * 100
        safe = _safe_note_name(name)
        self.assertLessEqual(len(safe.encode("utf-8")), _NOTE_NAME_MAX_BYTES)
        # decode round-trip proves no char was split
        safe.encode("utf-8").decode("utf-8")

    def test_empty_name_maps_to_placeholder(self):
        self.assertEqual(_safe_note_name(""), "_")

    def test_dot_only_name_still_yields_valid_filename(self):
        # '.' is replaced (not dropped): '...' becomes '___', a valid
        # file name — no crash, no empty component.
        self.assertEqual(_safe_note_name("..."), "___")

    def test_file_name_fits_name_max(self):
        # Final path component = safe name + ".md"; NAME_MAX is 255.
        safe = _safe_note_name(ASM_SNIPPET_NAME)
        self.assertLessEqual(len(safe.encode("utf-8")) + len(".md"), 255)


def _make_graph(nodes, edges) -> str:
    tmp = tempfile.mkdtemp(prefix="c2d_obsidian_test_")
    defaulted = []
    for n in nodes:
        defaulted.append({
            "id": n["id"], "name": n.get("name", n["id"]),
            "source_file": "/tmp/x.c", "line": 1,
            "domain": n.get("domain", "test"), "labels": [],
            "is_empty": False,
        })
    domain_data = {"nodes": defaulted, "edges": edges}
    with open(os.path.join(tmp, "domain_test.json"), "w") as f:
        json.dump(domain_data, f)
    master = {"source_root": "/tmp", "domains": {"test": "domain_test.json"}}
    with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
        json.dump(master, f)
    return tmp


class TestObsidianExportWithHostileNames(unittest.TestCase):
    """export-obsidian must survive asm-snippet function names and keep
    wiki-links resolving to the sanitized file names."""

    def test_export_completes_and_links_match_files(self):
        from _builder.export.export import cmd_export_obsidian

        nodes = [
            {"id": "evil", "name": ASM_SNIPPET_NAME, "domain": "arch"},
            {"id": "normal", "name": "normal_fn", "domain": "arch"},
            {"id": "cpp", "name": "ns::Class::method", "domain": "arch"},
        ]
        edges = [
            {"source": "evil", "target": "normal", "relation": "INVOKES"},
            {"source": "normal", "target": "cpp", "relation": "INVOKES"},
        ]
        graph_dir = _make_graph(nodes, edges)
        with tempfile.TemporaryDirectory() as out:
            args = argparse.Namespace(graph=graph_dir, output=out)
            cmd_export_obsidian(args)  # must not raise OSError 36

            arch_dir = os.path.join(out, "arch")
            files = sorted(os.listdir(arch_dir))
            # every file name within budget
            for fn in files:
                self.assertLessEqual(len(fn.encode("utf-8")), 255, fn)
            self.assertIn("normal_fn.md", files)
            self.assertIn(_safe_note_name("ns::Class::method") + ".md", files)
            evil_file = _safe_note_name(ASM_SNIPPET_NAME) + ".md"
            self.assertIn(evil_file, files)

            # links inside notes use the sanitized names
            evil_note = open(os.path.join(arch_dir, evil_file),
                             encoding="utf-8").read()
            self.assertIn("[[normal_fn]]", evil_note)
            normal_note = open(os.path.join(arch_dir, "normal_fn.md"),
                               encoding="utf-8").read()
            self.assertIn(f"[[{_safe_note_name('ns::Class::method')}]]",
                          normal_note)


if __name__ == "__main__":
    unittest.main()
