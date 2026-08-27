"""Smoke tests for _builder.coverage_report.

Covers write_coverage_report() (writes .code2database_coverage_report.json
from cgdb_files) and query_coverage() (returns subsystem summary / function
lookups / file lookups). Smoke-test level — verifies the module imports and
the API does not crash on representative inputs.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.cgdb_schema import apply_cgdb_schema
from _builder.coverage_report import (
    write_coverage_report,
    write_file_coverage,
    query_coverage,
    path_not_found_hints,
    _subsystem_from_path,
    _guess_subsystem_for_name,
)


def _populate_files(conn, rows):
    """Insert rows into cgdb_files. rows is list of (path, language, line_count)."""
    for path, lang, lc in rows:
        conn.execute(
            "INSERT INTO cgdb_files (path, is_system, language, sha256, "
            "line_count, byte_count, commit_hash) VALUES (?, 0, ?, 'x', ?, ?, 't')",
            (path, lang, lc, lc)
        )
    conn.commit()


class TestCoverageReportImport(unittest.TestCase):
    """Verify module and primary entry points are importable."""

    def test_module_imports_cleanly(self):
        import _builder.coverage_report as cr
        self.assertTrue(hasattr(cr, 'write_coverage_report'))
        self.assertTrue(hasattr(cr, 'query_coverage'))

    def test_subsystem_from_path(self):
        self.assertEqual(_subsystem_from_path('mm/page_alloc.c'), 'mm')
        self.assertEqual(_subsystem_from_path('kernel/sched/core.c'), 'kernel')
        self.assertEqual(_subsystem_from_path(''), 'root')

    def test_guess_subsystem_for_name(self):
        self.assertIn('mm', _guess_subsystem_for_name('kmalloc'))
        self.assertIn('block', _guess_subsystem_for_name('blk_init'))
        self.assertIn('net', _guess_subsystem_for_name('skb_alloc'))


class TestWriteCoverageReport(unittest.TestCase):
    """write_coverage_report() produces a JSON file from cgdb_files."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        db_path = os.path.join(self.tmpdir, 'code2database.db')
        self.conn = sqlite3.connect(db_path)
        apply_cgdb_schema(self.conn)
        _populate_files(self.conn, [
            ('mm/page_alloc.c', 'c', 1000),
            ('kernel/sched/core.c', 'c', 2000),
            ('net/ipv4/tcp.c', 'c', 500),
            ('custom/sub/x.c', 'c', 100),
        ])

    def _cleanup(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_none_when_no_db(self):
        empty = tempfile.mkdtemp()
        try:
            self.assertIsNone(write_coverage_report(empty))
        finally:
            import shutil
            shutil.rmtree(empty, ignore_errors=True)

    def test_writes_coverage_report_json(self):
        out = write_coverage_report(self.tmpdir)
        self.assertIsNotNone(out)
        self.assertTrue(os.path.exists(out))
        with open(out) as f:
            data = json.load(f)
        self.assertEqual(data['type'], 'code2database_coverage_report')
        self.assertIn('scanned_subsystems', data)
        self.assertIn('mm', data['scanned_subsystems'])
        self.assertIn('kernel', data['scanned_subsystems'])
        self.assertEqual(data['total_files'], 4)

    def test_write_file_coverage_json(self):
        out = write_file_coverage(self.tmpdir)
        self.assertIsNotNone(out)
        with open(out) as f:
            data = json.load(f)
        self.assertEqual(data['total_files'], 4)
        self.assertEqual(data['total_lines'], 3600)


class TestQueryCoverage(unittest.TestCase):
    """query_coverage() with function, file, and no-args modes."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        db_path = os.path.join(self.tmpdir, 'code2database.db')
        self.conn = sqlite3.connect(db_path)
        apply_cgdb_schema(self.conn)
        # Insert file + nodes so query_coverage function/file lookups work
        cur = self.conn.execute(
            "INSERT INTO cgdb_files (path, is_system, language, sha256, "
            "line_count, byte_count, commit_hash) "
            "VALUES (?, 0, 'c', 'x', 100, 100, 't')",
            ('mm/page_alloc.c',)
        )
        file_id = cur.lastrowid
        self.conn.execute(
            "INSERT INTO cgdb_nodes (kind, name, fqn, file_id, line, col, "
            "byte_start, byte_end) VALUES "
            "('function', 'kmalloc', 'kmalloc', ?, 10, 1, 0, 50)",
            (file_id,)
        )
        self.conn.commit()
        self.file_id = file_id

    def _cleanup(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_query_missing_db_returns_error(self):
        empty = tempfile.mkdtemp()
        try:
            r = query_coverage(empty, function_name='kmalloc')
            self.assertEqual(r['status'], 'error')
        finally:
            import shutil
            shutil.rmtree(empty, ignore_errors=True)

    def test_query_function_found(self):
        r = query_coverage(self.tmpdir, function_name='kmalloc')
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(r['match_count'], 1)
        self.assertEqual(r['matches'][0]['name'], 'kmalloc')

    def test_query_function_not_found_returns_hints(self):
        r = query_coverage(self.tmpdir, function_name='blk_submit')
        self.assertEqual(r['status'], 'ok')
        self.assertIn('block', r['subsystem_hints'])

    def test_query_file_found(self):
        r = query_coverage(self.tmpdir, file_path='mm/page_alloc.c')
        self.assertEqual(r['status'], 'ok')
        self.assertTrue(r['scanned'])
        self.assertEqual(r['function_count'], 1)

    def test_query_summary_mode(self):
        r = query_coverage(self.tmpdir)
        self.assertEqual(r['status'], 'ok')
        self.assertIn('mm', r['scanned_subsystems'])


class TestPathNotFoundHints(unittest.TestCase):
    """path_not_found_hints() surfaces scan suggestions."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        db_path = os.path.join(self.tmpdir, 'code2database.db')
        self.conn = sqlite3.connect(db_path)
        apply_cgdb_schema(self.conn)
        self.conn.execute(
            "INSERT INTO cgdb_files (path, is_system, language, sha256, "
            "line_count, byte_count, commit_hash) "
            "VALUES ('mm/page_alloc.c', 0, 'c', 'x', 100, 100, 't')"
        )
        self.conn.commit()

    def _cleanup(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_dict_with_required_keys(self):
        r = path_not_found_hints(self.tmpdir, missing_src='blk_submit_bio')
        self.assertIn('suggestion', r)
        self.assertIn('scanned_subsystems', r)
        self.assertIn('mm', r['scanned_subsystems'])

    def test_suggestion_mentions_unscanned_subsystem(self):
        r = path_not_found_hints(self.tmpdir, missing_src='blk_submit_bio')
        self.assertIn('block', r['suggestion'])


if __name__ == '__main__':
    unittest.main()
