"""Tests for cgdb_ops_bind (Phase 3: L7 typed vtable dispatch).

Verifies that the OpsBindDeriver detects `.field = function` designators
in struct initializer lists using clang's type system (not name patterns),
and emits OPS_BIND edges + OpsBindingRecord entries with:
  - field_node_id pointing to the FieldDecl node
  - impl_function_id pointing to the FunctionDecl node
  - signature_match set based on type comparison
  - edge_id deterministic and stable across runs
"""
import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


class TestOpsBindDeriverUnit(unittest.TestCase):
    """Unit tests for OpsBindDeriver using a fixture C source."""

    def setUp(self):
        try:
            from _scanner.clang_scanner import is_clang_available
        except ImportError:
            self.skipTest("ClangScanner not available")
        if not is_clang_available():
            self.skipTest("libclang not available")

        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.c_path = os.path.join(self.tmpdir, "ops_test.c")
        with open(self.c_path, 'w') as f:
            f.write(textwrap.dedent("""\
                struct file_operations {
                    int (*read_iter)(void *, char *, int);
                    int (*write_iter)(void *, const char *, int);
                    int (*open)(void);
                };

                int my_read(void *f, char *buf, int sz) { return 0; }
                int my_write(void *f, const char *buf, int sz) { return 0; }
                int my_open(void) { return 0; }

                struct file_operations my_fops = {
                    .read_iter = my_read,
                    .write_iter = my_write,
                    .open = my_open,
                };
            """))

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _scan(self):
        """Run ClangScanner on the fixture and return the result dict."""
        from _scanner.clang_scanner import ClangScanner
        scanner = ClangScanner(is_cpp=False)
        return scanner.scan_file(self.c_path, self.tmpdir)

    def test_emits_ops_bindings(self):
        """Scan produces a non-empty cgdb_ops_bindings list."""
        result = self._scan()
        self.assertGreater(len(result.get('cgdb_ops_bindings', [])), 0,
                           "expected at least one ops_binding")

    def test_emits_three_bindings(self):
        """Three .field = function designators → three ops_bindings."""
        result = self._scan()
        self.assertEqual(len(result['cgdb_ops_bindings']), 3)

    def test_binding_has_field_node_id(self):
        """Each binding's field_node_id points to a FieldDecl node."""
        result = self._scan()
        node_ids = {n['id'] for n in result['cgdb_nodes']}
        for b in result['cgdb_ops_bindings']:
            self.assertIn(b['field_node_id'], node_ids,
                          "field_node_id must reference an existing node")

    def test_binding_has_impl_function_id(self):
        """Each binding's impl_function_id points to a FunctionDecl node."""
        result = self._scan()
        node_ids = {n['id'] for n in result['cgdb_nodes']}
        for b in result['cgdb_ops_bindings']:
            self.assertIn(b['impl_function_id'], node_ids,
                          "impl_function_id must reference an existing node")

    def test_binding_has_ops_table_id(self):
        """Each binding's ops_table_id points to the VarDecl (my_fops) node."""
        result = self._scan()
        # Find the my_fops var node
        my_fops = next((n for n in result['cgdb_nodes']
                        if n['kind'] == 'var' and n['name'] == 'my_fops'), None)
        self.assertIsNotNone(my_fops, "my_fops VarDecl node must exist")
        for b in result['cgdb_ops_bindings']:
            self.assertEqual(b['ops_table_id'], my_fops['id'],
                             "all bindings should share the same ops_table_id (my_fops)")

    def test_signature_match_is_true(self):
        """All bindings have signature_match=True (correct signatures)."""
        result = self._scan()
        for b in result['cgdb_ops_bindings']:
            self.assertTrue(b['signature_match'],
                            f"signature_match should be True for binding: {b}")

    def test_emits_ops_bind_edges(self):
        """OPS_BIND edges are emitted in cgdb_edges with matching edge_id."""
        result = self._scan()
        ops_edges = [e for e in result['cgdb_edges'] if e['kind'] == 'OPS_BIND']
        self.assertEqual(len(ops_edges), 3,
                         "expected 3 OPS_BIND edges (one per binding)")
        # Each edge should have an edge_id that matches a binding
        binding_edge_ids = {b['edge_id'] for b in result['cgdb_ops_bindings']}
        edge_edge_ids = {e.get('edge_id') for e in ops_edges}
        self.assertEqual(binding_edge_ids, edge_edge_ids,
                         "edge_id must match between cgdb_edges and cgdb_ops_bindings")

    def test_edge_id_is_stable_across_runs(self):
        """The same source produces the same edge_ids across runs."""
        result1 = self._scan()
        result2 = self._scan()
        ids1 = sorted(b['edge_id'] for b in result1['cgdb_ops_bindings'])
        ids2 = sorted(b['edge_id'] for b in result2['cgdb_ops_bindings'])
        self.assertEqual(ids1, ids2,
                         "edge_ids must be deterministic across runs")

    def test_edge_id_fits_in_signed_64bit(self):
        """All edge_ids fit in SQLite signed 64-bit INTEGER."""
        result = self._scan()
        for b in result['cgdb_ops_bindings']:
            self.assertLess(b['edge_id'], 0x7FFF_FFFF_FFFF_FFFF)
            self.assertGreaterEqual(b['edge_id'], 0)

    def test_field_names_in_attrs(self):
        """Each OPS_BIND edge's attrs contain the field_name."""
        result = self._scan()
        ops_edges = [e for e in result['cgdb_edges'] if e['kind'] == 'OPS_BIND']
        field_names = {e.get('attrs', {}).get('field_name') for e in ops_edges}
        self.assertEqual(field_names, {'read_iter', 'write_iter', 'open'},
                         f"expected field_names read_iter/write_iter/open, got: {field_names}")


class TestOpsBindEndToEnd(unittest.TestCase):
    """End-to-end: scan → build → ops_bindings table populated in SQLite."""

    def setUp(self):
        try:
            from _scanner.clang_scanner import is_clang_available
        except ImportError:
            self.skipTest("ClangScanner not available")
        if not is_clang_available():
            self.skipTest("libclang not available")

        self.tmpdir = tempfile.mkdtemp(prefix="cgdb_ops_e2e_")
        self.addCleanup(self._cleanup)
        # Source fixture
        self.src_dir = os.path.join(self.tmpdir, "src")
        os.makedirs(self.src_dir, exist_ok=True)
        self.c_path = os.path.join(self.src_dir, "ops_e2e.c")
        with open(self.c_path, 'w') as f:
            f.write(textwrap.dedent("""\
                struct file_operations {
                    int (*read_iter)(void *, char *, int);
                    int (*open)(void);
                };

                int my_read(void *f, char *buf, int sz) { return 0; }
                int my_open(void) { return 0; }

                struct file_operations my_fops = {
                    .read_iter = my_read,
                    .open = my_open,
                };
            """))
        self.extraction_path = os.path.join(self.tmpdir, "extraction.json")
        self.outdir = os.path.join(self.tmpdir, "out")

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, cmd):
        import subprocess
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        )
        if result.returncode != 0:
            raise AssertionError(
                f"Command failed: {' '.join(cmd)}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result

    def test_build_populates_ops_bindings_table(self):
        """Scan + build → ops_bindings table has 2 rows in SQLite."""
        scripts = os.path.normpath(os.path.join(
            os.path.dirname(__file__), '..', 'scripts'
        ))
        # Scan
        self._run([
            sys.executable, os.path.join(scripts, "code2database_scanner.py"), "scan",
            "--source", self.src_dir,
            "--output", self.extraction_path,
            "--extraction-backend", "auto",
        ])
        # Build
        self._run([
            sys.executable, os.path.join(scripts, "code2database_builder.py"), "build",
            "--extraction", self.extraction_path,
            "--outdir", self.outdir,
            "--build-config", "auto",
            "--storage", "sqlite",
        ])
        # Verify SQLite
        import sqlite3
        db_path = os.path.join(self.outdir, "code2database.db")
        self.assertTrue(os.path.exists(db_path))
        conn = sqlite3.connect(db_path)
        try:
            # ops_bindings table has 2 rows
            count = conn.execute("SELECT COUNT(*) FROM ops_bindings").fetchone()[0]
            self.assertEqual(count, 2,
                             f"expected 2 ops_bindings, got {count}")

            # OPS_BIND edges in cgdb_edges
            edge_count = conn.execute(
                "SELECT COUNT(*) FROM cgdb_edges WHERE kind='OPS_BIND'"
            ).fetchone()[0]
            self.assertEqual(edge_count, 2,
                             f"expected 2 OPS_BIND edges, got {edge_count}")

            # Each ops_binding's edge_id exists in cgdb_edges
            mismatches = conn.execute(
                "SELECT ob.edge_id FROM ops_bindings ob "
                "LEFT JOIN cgdb_edges e ON ob.edge_id = e.id "
                "WHERE e.id IS NULL"
            ).fetchall()
            self.assertEqual(mismatches, [],
                             "every ops_binding.edge_id must exist in cgdb_edges")

            # All bindings have signature_match = 1
            non_matching = conn.execute(
                "SELECT COUNT(*) FROM ops_bindings WHERE signature_match = 0"
            ).fetchone()[0]
            self.assertEqual(non_matching, 0,
                             "expected all bindings to have signature_match=1")
        finally:
            conn.close()

    def test_find_ops_impls_reader(self):
        """SQLiteCGDBStore.find_ops_impls returns the impl function for a field."""
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
        from _builder.cgdb_store import SQLiteCGDBStore
        store = SQLiteCGDBStore(os.path.join(self.outdir, "code2database.db"))
        try:
            impls = store.find_ops_impls('read_iter', '')
            self.assertGreater(len(impls), 0,
                               "expected at least one impl for read_iter")
            impl = impls[0]
            self.assertEqual(impl['field_name'], 'read_iter')
            self.assertEqual(impl['impl_name'], 'my_read')
            self.assertTrue(impl['signature_match'])
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
