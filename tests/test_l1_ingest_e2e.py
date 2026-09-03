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
    _resolve_source_path,
    run_l1_ingest,
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


class TestResolveSourcePath(unittest.TestCase):
    """Verify _resolve_source_path resolves relative/moved paths so
    ingest_l1 can read source bytes regardless of the builder's cwd.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        os.makedirs(os.path.join(self.tmpdir, 'src', 'kernel', 'sched'))
        self.source_root = os.path.join(self.tmpdir, 'src')
        self.abs_path = os.path.join(self.source_root, 'kernel', 'sched', 'core.c')
        with open(self.abs_path, 'wb') as f:
            f.write(b'int x = 0;\n')
        self.rel_path = os.path.relpath(self.abs_path, self.source_root)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_as_is_when_path_exists(self):
        self.assertEqual(_resolve_source_path(self.abs_path, ""), self.abs_path)

    def test_resolves_relative_path_via_source_root(self):
        resolved = _resolve_source_path(self.rel_path, self.source_root)
        self.assertEqual(os.path.normpath(resolved), os.path.normpath(self.abs_path))

    def test_returns_original_when_no_resolution_succeeds(self):
        bogus = 'nonexistent/file.c'
        self.assertEqual(_resolve_source_path(bogus, self.source_root), bogus)

    def test_resolves_renamed_source_tree_via_basename(self):
        old_abs = os.path.join(self.tmpdir, 'oldlinux', 'kernel', 'sched', 'core.c')
        resolved = _resolve_source_path(old_abs, self.source_root)
        self.assertTrue(os.path.exists(resolved))
        self.assertEqual(os.path.basename(resolved), 'core.c')

    def test_empty_path_returned_unchanged(self):
        self.assertEqual(_resolve_source_path("", "/tmp"), "")

    def test_no_source_root_returns_original(self):
        bogus = '/nonexistent/abs/path.c'
        self.assertEqual(_resolve_source_path(bogus, ""), bogus)


class TestRunL1IngestOrchestrator(unittest.TestCase):
    """Tests for the parallel orchestrator (run_l1_ingest)."""

    def test_empty_tasks_returns_empty_list(self):
        self.assertEqual(run_l1_ingest([], "/tmp/x.db", "/src", "abc",
                                        workers=4, parallel_mode="process"), [])

    def test_serial_fallback_uses_provided_conn(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(tmpdir, ignore_errors=True))
        db_path = os.path.join(tmpdir, 'code2database.db')
        conn = sqlite3.connect(db_path)
        apply_cgdb_schema(conn)
        try:
            tasks = [('/nonexistent/foo.c', 12345)]
            results = run_l1_ingest(
                tasks=tasks,
                db_path=db_path,
                source_root="",
                commit_hash="test",
                workers=1,
                parallel_mode="thread",
                serial_conn=conn,
            )
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].get("disk_sha256", ""), "")
            self.assertIn("file_path", results[0])
        finally:
            conn.close()


def _l1_context_probe(task):
    """Module-level spawn-safe probe: report the child's view of the L1
    context globals (set by _l1_worker_init) WITHOUT touching the DB —
    concurrent multi-process SQLite is unusable on some environments
    (WSL1), which would obscure the pool-contract assertions below.
    """
    import _builder.l1_ingest as _l1
    fp, _fid = task
    return {
        "file_path": fp,
        "ctx": (_l1._L1_DB_PATH, _l1._L1_SOURCE_ROOT, _l1._L1_COMMIT_HASH),
        "pid": os.getpid(),
    }


class TestProcessPoolSpawnContext(unittest.TestCase):
    """run_l1_ingest's process pool must use the spawn start method with
    the context passed via the pool initializer.

    The old fork pool inherited the parent build process's full memory
    (the 86GB graph) as copy-on-write mappings — N workers touching
    pages multiplied memory demand past what the box had (2026-09-02
    OOM) — and forked children inherited corrupted libclang/ctypes state
    ("TypeError: cannot build parameter"). spawn children start clean
    and receive db_path/source_root/commit_hash via _l1_worker_init.
    """

    def test_pool_uses_spawn_and_initializer_sets_context(self):
        import multiprocessing as _mp
        import _builder.l1_ingest as _l1
        from unittest.mock import patch as _patch

        tasks = [(f"/fake/src/f{i}.c", i + 1) for i in range(120)]
        seen_methods = []
        _orig_get_context = _mp.get_context

        def _spy(name=None):
            seen_methods.append(name)
            return _orig_get_context(name)

        orig_worker = _l1._l1_ingest_proc_worker
        _l1._l1_ingest_proc_worker = _l1_context_probe
        try:
            with _patch.object(_mp, "get_context", _spy):
                results = run_l1_ingest(
                    tasks=tasks, db_path="/probe/db.sqlite",
                    source_root="/probe/src", commit_hash="cafe",
                    workers=3, parallel_mode="process")
        finally:
            _l1._l1_ingest_proc_worker = orig_worker

        # The pool must have been created with the spawn start method.
        self.assertIn("spawn", seen_methods)
        self.assertNotIn("fork", seen_methods)
        self.assertEqual(len(results), 120)
        child_pids = set()
        for r in results:
            self.assertEqual(
                r["ctx"], ("/probe/db.sqlite", "/probe/src", "cafe"),
                "spawned worker did not receive initializer context")
            child_pids.add(r["pid"])
        # Work really ran in child processes, not inline in the parent.
        self.assertNotIn(os.getpid(), child_pids)
        self.assertTrue(child_pids)
        # The parent's module globals were never populated — the new
        # code must not rely on fork COW inheritance.
        self.assertEqual(_l1._L1_DB_PATH, "")
        self.assertEqual(_l1._L1_SOURCE_ROOT, "")
        self.assertEqual(_l1._L1_COMMIT_HASH, "unknown")

    def test_worker_init_sets_globals(self):
        """_l1_worker_init directly sets the three context globals."""
        import _builder.l1_ingest as _l1
        old = (_l1._L1_DB_PATH, _l1._L1_SOURCE_ROOT, _l1._L1_COMMIT_HASH)
        try:
            _l1._l1_worker_init("/x/y.db", "/src", "abc123")
            self.assertEqual(_l1._L1_DB_PATH, "/x/y.db")
            self.assertEqual(_l1._L1_SOURCE_ROOT, "/src")
            self.assertEqual(_l1._L1_COMMIT_HASH, "abc123")
        finally:
            (_l1._L1_DB_PATH, _l1._L1_SOURCE_ROOT, _l1._L1_COMMIT_HASH) = old


class TestResolveSourceFileGraphDir(unittest.TestCase):
    """Tests for the shared utils.resolve_source_file helper."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.source_root = os.path.join(self.tmpdir, 'proj', 'src')
        os.makedirs(os.path.join(self.source_root, 'kernel'))
        self.abs_path = os.path.join(self.source_root, 'kernel', 'foo.c')
        with open(self.abs_path, 'wb') as f:
            f.write(b'int x = 0;\n')
        self.graph_dir = os.path.join(self.tmpdir, 'proj', '.code2database')
        os.makedirs(self.graph_dir, exist_ok=True)
        import json
        with open(os.path.join(self.graph_dir, 'code2database_master.json'), 'w') as f:
            json.dump({"source_root": self.source_root}, f)
        from _builder.utils import _SOURCE_ROOT_CACHE
        _SOURCE_ROOT_CACHE.pop(self.graph_dir, None)
        self.rel_path = os.path.relpath(self.abs_path, self.source_root)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        from _builder.utils import _SOURCE_ROOT_CACHE
        _SOURCE_ROOT_CACHE.pop(self.graph_dir, None)

    def test_resolves_relative_path_via_master_json(self):
        from _builder.utils import resolve_source_file
        resolved = resolve_source_file(self.rel_path, self.graph_dir)
        self.assertTrue(os.path.exists(resolved))
        self.assertEqual(os.path.normpath(resolved),
                         os.path.normpath(self.abs_path))

    def test_returns_absolute_existing_path_unchanged(self):
        from _builder.utils import resolve_source_file
        self.assertEqual(resolve_source_file(self.abs_path, self.graph_dir),
                          self.abs_path)

    def test_returns_original_when_master_missing(self):
        from _builder.utils import resolve_source_file, _SOURCE_ROOT_CACHE
        empty_graph_dir = os.path.join(self.tmpdir, 'empty_graph')
        os.makedirs(empty_graph_dir, exist_ok=True)
        _SOURCE_ROOT_CACHE.pop(empty_graph_dir, None)
        bogus = 'nonexistent.c'
        self.assertEqual(resolve_source_file(bogus, empty_graph_dir), bogus)


if __name__ == '__main__':
    unittest.main()
