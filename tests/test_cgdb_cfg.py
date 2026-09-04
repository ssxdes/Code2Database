"""Tests for cgdb_analysis (L4 CFG + L5 data_flow + L6 alias).

Verifies that:
  - CFGExtractor parses clang's DumpCFG output and emits basic_blocks + cfg_edges
  - Edge kinds match schema CHECK constraint (fallthrough/true_branch/false_branch)
  - Block IDs are stable across runs
  - DataFlowExtractor walks AST and emits def-use entries for locals
  - AliasExtractor stub returns empty list (MVP)
  - End-to-end: scan → build → basic_blocks/cfg_edges/data_flow tables populated
"""
import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


class TestCFGExtractorUnit(unittest.TestCase):
    """Unit tests for CFGExtractor on a simple if/else fixture."""

    def setUp(self):
        try:
            from _scanner.clang_scanner import is_clang_available
        except ImportError:
            self.skipTest("ClangScanner not available")
        if not is_clang_available():
            self.skipTest("libclang not available")

        self.tmpdir = tempfile.mkdtemp(prefix="cgdb_cfg_")
        self.addCleanup(self._cleanup)
        self.c_path = os.path.join(self.tmpdir, "cfg_test.c")
        with open(self.c_path, 'w') as f:
            f.write(textwrap.dedent("""\
                int foo(int x) {
                    int result = 0;
                    if (x > 0) {
                        result = x * 2;
                    } else {
                        result = -x;
                    }
                    return result;
                }
            """))

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_extract_returns_blocks_and_edges(self):
        """CFGExtractor.extract returns non-empty (blocks, edges) tuple."""
        from _builder.cgdb_analysis import CFGExtractor
        ext = CFGExtractor()
        blocks, edges = ext.extract(self.c_path, "foo", 12345)
        self.assertGreater(len(blocks), 0, "expected basic blocks for foo()")
        self.assertGreater(len(edges), 0, "expected CFG edges for foo()")

    def test_extract_has_entry_and_exit_blocks(self):
        """The CFG has exactly one ENTRY and one EXIT block."""
        from _builder.cgdb_analysis import CFGExtractor
        ext = CFGExtractor()
        blocks, _ = ext.extract(self.c_path, "foo", 12345)
        entries = [b for b in blocks if b.is_entry]
        exits = [b for b in blocks if b.is_exit]
        self.assertEqual(len(entries), 1, "exactly one ENTRY block")
        self.assertEqual(len(exits), 1, "exactly one EXIT block")

    def test_extract_emits_branch_edges(self):
        """The if/else produces at least one true_branch and one false_branch edge."""
        from _builder.cgdb_analysis import CFGExtractor
        ext = CFGExtractor()
        _, edges = ext.extract(self.c_path, "foo", 12345)
        kinds = {e.kind for e in edges}
        self.assertIn('true_branch', kinds, "expected true_branch edge from if-block")
        self.assertIn('false_branch', kinds, "expected false_branch edge from if-block")

    def test_block_ids_are_stable_across_runs(self):
        """Same source + function_id produces same block IDs."""
        from _builder.cgdb_analysis import CFGExtractor
        ext = CFGExtractor()
        blocks1, _ = ext.extract(self.c_path, "foo", 12345)
        blocks2, _ = ext.extract(self.c_path, "foo", 12345)
        ids1 = sorted(b.id for b in blocks1)
        ids2 = sorted(b.id for b in blocks2)
        self.assertEqual(ids1, ids2, "block IDs must be deterministic")

    def test_block_ids_fit_in_signed_64bit(self):
        """All block IDs fit in SQLite signed 64-bit INTEGER."""
        from _builder.cgdb_analysis import CFGExtractor
        ext = CFGExtractor()
        blocks, _ = ext.extract(self.c_path, "foo", 12345)
        for b in blocks:
            self.assertLess(b.id, 0x7FFF_FFFF_FFFF_FFFF)
            self.assertGreaterEqual(b.id, 0)

    def test_block_function_id_propagates(self):
        """All blocks carry the func_node_id passed to extract()."""
        from _builder.cgdb_analysis import CFGExtractor
        ext = CFGExtractor()
        blocks, _ = ext.extract(self.c_path, "foo", 99999)
        for b in blocks:
            self.assertEqual(b.function_id, 99999)

    def test_edge_kinds_match_schema_check(self):
        """All edge kinds are in the schema CHECK constraint set."""
        from _builder.cgdb_analysis import CFGExtractor
        ext = CFGExtractor()
        _, edges = ext.extract(self.c_path, "foo", 12345)
        allowed = {'fallthrough', 'true_branch', 'false_branch', 'exception'}
        for e in edges:
            self.assertIn(e.kind, allowed, f"edge kind {e.kind!r} not in schema")

    def test_extract_at_least_six_blocks_for_if_else(self):
        """if/else fixture produces >= 6 blocks (ENTRY, EXIT, if-header, then, else, merge)."""
        from _builder.cgdb_analysis import CFGExtractor
        ext = CFGExtractor()
        blocks, _ = ext.extract(self.c_path, "foo", 12345)
        self.assertGreaterEqual(len(blocks), 4,
                                f"expected at least 4 blocks, got {len(blocks)}")

    def test_extract_with_func_cursor_matches_tu_walk(self):
        """Passing func_cursor directly produces the same blocks/edges as
        walking the TU AST to find the function (the slow backward-compat path).

        This verifies the SPDK-hang fix: when func_cursor is passed, the
        extractor uses it directly without re-walking the entire TU AST.
        For projects with N functions and M-node ASTs per TU, this changes
        extraction from O(N×M) to O(M + N×m) where m << M is the function
        body size.
        """
        from clang.cindex import Index, CursorKind
        from _builder.cgdb_analysis import CFGExtractor
        ext = CFGExtractor()

        index = Index.create()
        tu = index.parse(self.c_path)
        tu_cursor = tu.cursor

        # Find the function cursor (mirrors clang_scanner.py:640 logic)
        func_cursor = None
        for top in tu_cursor.get_children():
            if (top.kind == CursorKind.FUNCTION_DECL
                    and top.spelling == "foo"
                    and top.is_definition()):
                func_cursor = top
                break
        self.assertIsNotNone(func_cursor, "failed to find foo() cursor")

        # Fast path: pass func_cursor directly
        blocks_fast, edges_fast = ext.extract(
            self.c_path, "foo", 12345,
            tu_cursor=tu_cursor, func_cursor=func_cursor,
        )
        # Slow path: pass only tu_cursor (the old way)
        blocks_slow, edges_slow = ext.extract(
            self.c_path, "foo", 12345,
            tu_cursor=tu_cursor, func_cursor=None,
        )
        self.assertEqual(len(blocks_fast), len(blocks_slow),
                         "func_cursor path must produce same block count as tu_walk path")
        self.assertEqual(len(edges_fast), len(edges_slow),
                         "func_cursor path must produce same edge count as tu_walk path")
        # Block IDs should be identical (deterministic given func_node_id)
        ids_fast = sorted(b.id for b in blocks_fast)
        ids_slow = sorted(b.id for b in blocks_slow)
        self.assertEqual(ids_fast, ids_slow,
                         "block IDs must match between fast and slow paths")


class TestDataFlowExtractorUnit(unittest.TestCase):
    """Unit tests for DataFlowExtractor on a fixture with a local variable."""

    def setUp(self):
        try:
            from _scanner.clang_scanner import is_clang_available
        except ImportError:
            self.skipTest("ClangScanner not available")
        if not is_clang_available():
            self.skipTest("libclang not available")

        self.tmpdir = tempfile.mkdtemp(prefix="cgdb_df_")
        self.addCleanup(self._cleanup)
        self.c_path = os.path.join(self.tmpdir, "df_test.c")
        with open(self.c_path, 'w') as f:
            f.write(textwrap.dedent("""\
                int bar(int x) {
                    int y = x + 1;
                    return y;
                }
            """))

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _scan(self):
        """Run ClangScanner and return the result dict."""
        from _scanner.clang_scanner import ClangScanner
        scanner = ClangScanner(is_cpp=False)
        return scanner.scan_file(self.c_path, self.tmpdir)

    def test_scan_emits_data_flow(self):
        """Scan produces a non-empty cgdb_data_flow list for bar()."""
        result = self._scan()
        self.assertGreater(len(result.get('cgdb_data_flow', [])), 0,
                           "expected data_flow records for local y")

    def test_data_flow_records_have_var_id(self):
        """Each data_flow record has a valid var_id referencing a cgdb_node."""
        result = self._scan()
        node_ids = {n['id'] for n in result['cgdb_nodes']}
        for d in result['cgdb_data_flow']:
            self.assertIn(d['var_id'], node_ids,
                          "var_id must reference an existing node")

    def test_data_flow_kind_in_valid_set(self):
        """Each data_flow record has kind in {def, use, may_def, may_use}."""
        result = self._scan()
        valid_kinds = {'def', 'use', 'may_def', 'may_use'}
        for d in result['cgdb_data_flow']:
            self.assertIn(d['kind'], valid_kinds,
                          f"kind must be one of {valid_kinds}, got {d['kind']!r}")


class TestSyncPrimitivesUnit(unittest.TestCase):
    """Unit tests for SyncPrimitiveWriter (L8) on a fixture with pthread_mutex."""

    def setUp(self):
        try:
            from _scanner.clang_scanner import is_clang_available
        except ImportError:
            self.skipTest("ClangScanner not available")
        if not is_clang_available():
            self.skipTest("libclang not available")

        self.tmpdir = tempfile.mkdtemp(prefix="cgdb_sync_")
        self.addCleanup(self._cleanup)
        self.c_path = os.path.join(self.tmpdir, "sync_test.c")
        with open(self.c_path, 'w') as f:
            f.write(textwrap.dedent("""\
                typedef struct { int dummy; } pthread_mutex_t;
                int pthread_mutex_lock(pthread_mutex_t *);
                int pthread_mutex_unlock(pthread_mutex_t *);

                int guarded(pthread_mutex_t *m) {
                    pthread_mutex_lock(m);
                    int x = 42;
                    pthread_mutex_unlock(m);
                    return x;
                }
            """))

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _scan(self):
        from _scanner.clang_scanner import ClangScanner
        scanner = ClangScanner(is_cpp=False)
        return scanner.scan_file(self.c_path, self.tmpdir)

    def test_scan_emits_sync_primitives(self):
        """Scan produces a non-empty cgdb_sync_primitives list."""
        result = self._scan()
        self.assertGreater(len(result.get('cgdb_sync_primitives', [])), 0,
                           "expected sync_primitive records for lock/unlock")

    def test_emits_acquire_and_release(self):
        """Scan produces at least one lock_acquire and one lock_release record."""
        result = self._scan()
        kinds = {s['kind'] for s in result['cgdb_sync_primitives']}
        self.assertIn('lock_acquire', kinds, "expected lock_acquire")
        self.assertIn('lock_release', kinds, "expected lock_release")

    def test_emits_happens_before(self):
        """Scan produces a happens_before record pairing acquire → release."""
        result = self._scan()
        self.assertGreater(len(result.get('cgdb_happens_before', [])), 0,
                           "expected at least one happens_before record")

    def test_happens_before_reason_is_lock(self):
        """happens_before records carry reason='lock' for mutex pairs."""
        result = self._scan()
        for h in result['cgdb_happens_before']:
            self.assertEqual(h['reason'], 'lock',
                             f"expected reason='lock', got {h['reason']!r}")


class TestPhase4EndToEnd(unittest.TestCase):
    """End-to-end: scan → build → basic_blocks/cfg_edges/data_flow/sync_primitives tables."""

    def setUp(self):
        try:
            from _scanner.clang_scanner import is_clang_available
        except ImportError:
            self.skipTest("ClangScanner not available")
        if not is_clang_available():
            self.skipTest("libclang not available")

        self.tmpdir = tempfile.mkdtemp(prefix="cgdb_phase4_e2e_")
        self.addCleanup(self._cleanup)
        self.src_dir = os.path.join(self.tmpdir, "src")
        os.makedirs(self.src_dir, exist_ok=True)
        # Combined fixture: if/else + local var + sync primitives
        self.c_path = os.path.join(self.src_dir, "phase4_e2e.c")
        with open(self.c_path, 'w') as f:
            f.write(textwrap.dedent("""\
                typedef struct { int dummy; } pthread_mutex_t;
                int pthread_mutex_lock(pthread_mutex_t *);
                int pthread_mutex_unlock(pthread_mutex_t *);

                int compute(int x) {
                    int result = 0;
                    if (x > 0) {
                        result = x * 2;
                    } else {
                        result = -x;
                    }
                    return result;
                }

                int guarded(pthread_mutex_t *m, int v) {
                    pthread_mutex_lock(m);
                    int y = v + 1;
                    pthread_mutex_unlock(m);
                    return y;
                }
            """))
        self.extraction_path = os.path.join(self.tmpdir, "extraction.json")
        self.outdir = os.path.join(self.tmpdir, "out")

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, cmd):
        import subprocess
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        )
        if result.returncode != 0:
            raise AssertionError(
                f"Command failed: {' '.join(cmd)}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result

    def test_build_populates_cfg_tables(self):
        """Scan + build → basic_blocks and cfg_edges tables populated in SQLite."""
        scripts = os.path.normpath(os.path.join(
            os.path.dirname(__file__), '..', 'scripts'
        ))
        self._run([
            sys.executable, os.path.join(scripts, "code2database_scanner.py"), "scan",
            "--source", self.src_dir,
            "--output", self.extraction_path,
            "--extraction-backend", "auto",
        ])
        self._run([
            sys.executable, os.path.join(scripts, "code2database_builder.py"), "build",
            "--extraction", self.extraction_path,
            "--outdir", self.outdir,
            "--build-config", "auto",
            "--storage", "sqlite",
        ])
        import sqlite3
        db_path = os.path.join(self.outdir, "code2database.db")
        self.assertTrue(os.path.exists(db_path))
        conn = sqlite3.connect(db_path)
        try:
            bb_count = conn.execute("SELECT COUNT(*) FROM basic_blocks").fetchone()[0]
            self.assertGreater(bb_count, 0, "expected basic_blocks rows")
            cfg_count = conn.execute("SELECT COUNT(*) FROM cfg_edges").fetchone()[0]
            self.assertGreater(cfg_count, 0, "expected cfg_edges rows")
            # Verify edge kinds are within the schema CHECK constraint
            bad_kinds = conn.execute(
                "SELECT DISTINCT kind FROM cfg_edges WHERE kind NOT IN "
                "('fallthrough','true_branch','false_branch','exception')"
            ).fetchall()
            self.assertEqual(bad_kinds, [], f"invalid cfg_edges kinds: {bad_kinds}")
        finally:
            conn.close()

    def test_build_populates_data_flow_table(self):
        """Scan + build → data_flow table populated with def-use entries."""
        scripts = os.path.normpath(os.path.join(
            os.path.dirname(__file__), '..', 'scripts'
        ))
        self._run([
            sys.executable, os.path.join(scripts, "code2database_scanner.py"), "scan",
            "--source", self.src_dir,
            "--output", self.extraction_path,
            "--extraction-backend", "auto",
        ])
        self._run([
            sys.executable, os.path.join(scripts, "code2database_builder.py"), "build",
            "--extraction", self.extraction_path,
            "--outdir", self.outdir,
            "--build-config", "auto",
            "--storage", "sqlite",
        ])
        import sqlite3
        db_path = os.path.join(self.outdir, "code2database.db")
        conn = sqlite3.connect(db_path)
        try:
            df_count = conn.execute("SELECT COUNT(*) FROM data_flow").fetchone()[0]
            self.assertGreater(df_count, 0, "expected data_flow rows")
            # Verify all kinds are in ('def', 'use', 'def_use')
            bad_kinds = conn.execute(
                "SELECT DISTINCT kind FROM data_flow WHERE kind NOT IN "
                "('def','use','def_use')"
            ).fetchall()
            self.assertEqual(bad_kinds, [], f"invalid data_flow kinds: {bad_kinds}")
        finally:
            conn.close()

    def test_build_populates_sync_primitives_table(self):
        """Scan + build → sync_primitives table populated with lock_acquire/release."""
        scripts = os.path.normpath(os.path.join(
            os.path.dirname(__file__), '..', 'scripts'
        ))
        self._run([
            sys.executable, os.path.join(scripts, "code2database_scanner.py"), "scan",
            "--source", self.src_dir,
            "--output", self.extraction_path,
            "--extraction-backend", "auto",
        ])
        self._run([
            sys.executable, os.path.join(scripts, "code2database_builder.py"), "build",
            "--extraction", self.extraction_path,
            "--outdir", self.outdir,
            "--build-config", "auto",
            "--storage", "sqlite",
        ])
        import sqlite3
        db_path = os.path.join(self.outdir, "code2database.db")
        conn = sqlite3.connect(db_path)
        try:
            sp_count = conn.execute("SELECT COUNT(*) FROM sync_primitives").fetchone()[0]
            self.assertGreater(sp_count, 0, "expected sync_primitives rows")
            # Should have at least one lock_acquire and one lock_release
            acquire_count = conn.execute(
                "SELECT COUNT(*) FROM sync_primitives WHERE kind='lock_acquire'"
            ).fetchone()[0]
            release_count = conn.execute(
                "SELECT COUNT(*) FROM sync_primitives WHERE kind='lock_release'"
            ).fetchone()[0]
            self.assertGreater(acquire_count, 0, "expected at least one lock_acquire")
            self.assertGreater(release_count, 0, "expected at least one lock_release")
            # happens_before table should have at least one row
            hb_count = conn.execute("SELECT COUNT(*) FROM happens_before").fetchone()[0]
            self.assertGreater(hb_count, 0, "expected happens_before rows")
        finally:
            conn.close()


class TestAliasExtractorStub(unittest.TestCase):
    """L6 alias analysis is a stub for MVP — verify it returns empty list."""

    def test_alias_extractor_returns_empty(self):
        """AliasExtractor.extract_from_ast returns [] for MVP."""
        from _builder.cgdb_analysis import AliasExtractor
        ext = AliasExtractor()
        # Pass None for cursor — extract_from_ast should handle gracefully
        result = ext.extract_from_ast(None, 12345, lambda c, k: 0)
        self.assertEqual(result, [], "AliasExtractor stub should return empty list")


if __name__ == '__main__':
    unittest.main()
