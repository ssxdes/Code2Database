"""End-to-end smoke tests for _builder.l1_ingest.ingest_l1().

The L1 ingest layer requires libclang to populate the tokens / macros /
pp_branches tables (per design report §2.1.1). When libclang is not
installed the function falls back to a no-op path that only computes the
disk sha256 and updates source_files_meta — that path is exercised
unconditionally.

The full token-stream path is gated behind @unittest.skipUnless on
l1_ingest.is_libclang_available().
"""
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.cgdb_schema import apply_cgdb_schema
from _builder.l1_ingest import (
    ingest_l1,
    is_libclang_available,
    _refine_literal_kind,
    _classify_comment_kind,
    _split_pp_directive,
    _compute_security_flags,
)


_SAMPLE_C = b"""#include <linux/mm.h>
#define FOO(x) ((x) + 1)

/* block comment */
int kmalloc(int sz) {
    const char *msg = "hello";
    return sz;
}
"""


class TestL1IngestImport(unittest.TestCase):
    """Verify the module and helpers import cleanly."""

    def test_module_imports_cleanly(self):
        import _builder.l1_ingest as l1
        self.assertTrue(hasattr(l1, 'ingest_l1'))
        self.assertTrue(callable(l1.ingest_l1))

    def test_is_libclang_available_returns_bool(self):
        self.assertIsInstance(is_libclang_available(), bool)

    def test_refine_literal_kind(self):
        self.assertEqual(_refine_literal_kind('42'), 'int_literal')
        self.assertEqual(_refine_literal_kind('3.14'), 'float_literal')
        self.assertEqual(_refine_literal_kind('"hi"'), 'string_literal')
        self.assertEqual(_refine_literal_kind("'a'"), 'char_literal')

    def test_classify_comment_kind(self):
        self.assertEqual(_classify_comment_kind('/* hi */'), 'block')
        self.assertEqual(_classify_comment_kind('// hi'), 'line')
        self.assertEqual(_classify_comment_kind('/// doc'), 'doc')

    def test_split_pp_directive(self):
        self.assertEqual(_split_pp_directive('#ifdef CONFIG_MM'), ('ifdef', 'CONFIG_MM'))
        self.assertEqual(_split_pp_directive('#include <x.h>')[0], 'include')
        self.assertIsNone(_split_pp_directive('int x = 0;'))

    def test_compute_security_flags_detects_format_string(self):
        flags_json = _compute_security_flags('"format %s %d"')
        self.assertIn('format_string', flags_json)


class TestIngestL1Fallback(unittest.TestCase):
    """When libclang is unavailable, ingest_l1 falls back to disk-sha only."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        db_path = os.path.join(self.tmpdir, 'code2database.db')
        self.conn = sqlite3.connect(db_path)
        apply_cgdb_schema(self.conn)
        self.src_path = os.path.join(self.tmpdir, 'test.c')
        with open(self.src_path, 'wb') as f:
            f.write(_SAMPLE_C)
        cur = self.conn.execute(
            "INSERT INTO cgdb_files (path, is_system, language, sha256, "
            "line_count, byte_count, commit_hash) "
            "VALUES (?, 0, 'c', 'x', 10, ?, 'test')",
            (self.src_path, len(_SAMPLE_C))
        )
        self.file_id = cur.lastrowid
        self.conn.commit()
        self.disk_sha = hashlib.sha256(_SAMPLE_C).hexdigest()

    def _cleanup(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @unittest.skipUnless(not is_libclang_available(),
                         "libclang installed — fallback path not exercised")
    def test_fallback_writes_disk_sha(self):
        stats = ingest_l1(self.conn, self.src_path, self.file_id,
                          commit_hash='test')
        self.assertEqual(stats['disk_sha256'], self.disk_sha)
        # Fallback sets the libclang-missing error marker
        self.assertIn('error', stats)
        self.assertIn('libclang not installed', stats['error'])
        # source_files_meta should be populated with the disk sha
        row = self.conn.execute(
            "SELECT disk_sha256, line_ending, has_bom FROM source_files_meta "
            "WHERE file_id = ?", (self.file_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], self.disk_sha)
        self.assertEqual(row[1], 'LF')
        self.assertEqual(row[2], 0)


class TestIngestL1WithLibclang(unittest.TestCase):
    """Full token-stream ingest path. Requires libclang."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        db_path = os.path.join(self.tmpdir, 'code2database.db')
        self.conn = sqlite3.connect(db_path)
        apply_cgdb_schema(self.conn)
        self.src_path = os.path.join(self.tmpdir, 'test.c')
        with open(self.src_path, 'wb') as f:
            f.write(_SAMPLE_C)
        cur = self.conn.execute(
            "INSERT INTO cgdb_files (path, is_system, language, sha256, "
            "line_count, byte_count, commit_hash) "
            "VALUES (?, 0, 'c', 'x', 10, ?, 'test')",
            (self.src_path, len(_SAMPLE_C))
        )
        self.file_id = cur.lastrowid
        self.conn.commit()

    def _cleanup(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @unittest.skipUnless(is_libclang_available(),
                         "libclang not installed — token-stream ingest skipped")
    def test_full_ingest_populates_tokens(self):
        stats = ingest_l1(self.conn, self.src_path, self.file_id,
                          commit_hash='test')
        self.assertGreater(stats['tokens'], 0)
        # Token stream should include the function name 'kmalloc'
        row = self.conn.execute(
            "SELECT COUNT(*) FROM tokens WHERE file_id = ? AND spelling = 'kmalloc'",
            (self.file_id,)
        ).fetchone()
        self.assertGreater(row[0], 0)
        # Comments table should have at least one row for '/* block comment */'
        comm = self.conn.execute(
            "SELECT COUNT(*) FROM comments_freeform WHERE file_id = ?",
            (self.file_id,)
        ).fetchone()
        self.assertGreater(comm[0], 0)
        # source_files_meta should be populated
        meta = self.conn.execute(
            "SELECT disk_sha256, line_ending, encoding FROM source_files_meta "
            "WHERE file_id = ?", (self.file_id,)
        ).fetchone()
        self.assertIsNotNone(meta)


if __name__ == '__main__':
    unittest.main()
