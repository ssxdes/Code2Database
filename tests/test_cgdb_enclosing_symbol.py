"""Tests for enclosing_symbol_id derivation (cgdb-architecture doc 5.4.2).

Verifies that ClangScanner attaches `enclosing_symbol_id` to nodes that
live inside a function body (VAR_DECL, DECL_REF_EXPR, CALL_EXPR, etc.),
pointing to the node_id of the enclosing FunctionDecl. Top-level nodes
(file-scope structs, typedefs, var decls) should have enclosing_symbol_id
= 0/None.
"""
import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _scanner.clang_scanner import ClangScanner, is_clang_available


_TEST_C_SOURCE = textwrap.dedent("""\
    #include <stdio.h>

    struct buffer {
        char *data;
        int size;
    };

    static int global_counter = 0;

    int validate(struct buffer *buf) {
        int local_x = 0;
        if (buf == NULL) return -1;
        return local_x;
    }

    int main(int argc, char **argv) {
        struct buffer b;
        int result = validate(&b);
        return result;
    }
""")


@unittest.skipUnless(is_clang_available(), "libclang not available")
class TestEnclosingSymbol(unittest.TestCase):
    """Test that ClangScanner attaches enclosing_symbol_id to body nodes."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.c_path = os.path.join(self.tmpdir, "test_enclosing.c")
        with open(self.c_path, 'w') as f:
            f.write(_TEST_C_SOURCE)
        self.scanner = ClangScanner(is_cpp=False)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _scan(self):
        result = self.scanner.scan_file(self.c_path, self.tmpdir)
        self.assertNotIn('error', result, f"scan failed: {result.get('error')}")
        return result

    def test_function_node_has_no_enclosing(self):
        """Top-level function nodes have enclosing_symbol_id = 0/None."""
        result = self._scan()
        functions = [n for n in result['cgdb_nodes'] if n['kind'] == 'function']
        self.assertGreater(len(functions), 0)
        for func in functions:
            # Functions at file scope should have no enclosing function
            enclosing = func.get('enclosing_symbol_id', 0) or 0
            self.assertEqual(enclosing, 0,
                             f"function {func['name']} should have no enclosing")

    def test_struct_node_has_no_enclosing(self):
        """Top-level struct nodes have enclosing_symbol_id = 0/None."""
        result = self._scan()
        structs = [n for n in result['cgdb_nodes'] if n['kind'] == 'struct']
        self.assertGreater(len(structs), 0)
        for s in structs:
            enclosing = s.get('enclosing_symbol_id', 0) or 0
            self.assertEqual(enclosing, 0,
                             f"struct {s['name']} should have no enclosing")

    def test_local_var_has_enclosing_function(self):
        """VAR_DECLs inside a function body have enclosing_symbol_id set
        to the containing function's node_id."""
        result = self._scan()
        # Find the validate function's node_id
        functions = {n['name']: n for n in result['cgdb_nodes']
                     if n['kind'] == 'function'}
        self.assertIn('validate', functions)
        validate_id = functions['validate']['id']
        # Find local vars inside validate
        local_vars = [n for n in result['cgdb_nodes']
                      if n['kind'] == 'var' and n['name'] == 'local_x']
        self.assertGreater(len(local_vars), 0,
                           "Expected at least one local_x var node")
        # At least one local_x should be enclosed by validate
        enclosed = [v for v in local_vars
                    if v.get('enclosing_symbol_id') == validate_id]
        self.assertGreater(len(enclosed), 0,
                           "Expected local_x to be enclosed by validate")

    def test_calls_edge_has_enclosing(self):
        """CALL_EXPR-derived edges have enclosing_symbol_id set to caller."""
        result = self._scan()
        functions = {n['name']: n for n in result['cgdb_nodes']
                     if n['kind'] == 'function'}
        main_id = functions['main']['id']
        # Find INVOKES edges from main → validate
        calls_to_validate = [
            e for e in result['cgdb_edges']
            if e['kind'] == 'INVOKES' and e['src_id'] == main_id
        ]
        self.assertGreater(len(calls_to_validate), 0,
                           "Expected main to call something")
        # All INVOKES from main should have enclosing = main
        for edge in calls_to_validate:
            self.assertEqual(
                edge.get('enclosing_symbol_id'), main_id,
                f"INVOKES edge from main should have enclosing_symbol_id=main"
            )

    def test_file_scope_var_has_no_enclosing(self):
        """File-scope VAR_DECL (global_counter) has no enclosing function."""
        result = self._scan()
        global_vars = [n for n in result['cgdb_nodes']
                       if n['kind'] == 'var' and n['name'] == 'global_counter']
        self.assertGreater(len(global_vars), 0,
                           "Expected global_counter var node")
        for v in global_vars:
            enclosing = v.get('enclosing_symbol_id', 0) or 0
            self.assertEqual(enclosing, 0,
                             "global_counter should have no enclosing function")


@unittest.skipUnless(is_clang_available(), "libclang not available")
class TestEnclosingSymbolEndToEnd(unittest.TestCase):
    """End-to-end: build a graph and verify enclosing_symbol_id persists
    to the cgdb_nodes table."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.c_path = os.path.join(self.tmpdir, "test_enclosing_e2e.c")
        with open(self.c_path, 'w') as f:
            f.write(_TEST_C_SOURCE)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_enclosing_persists_to_db(self):
        """Build a graph and verify enclosing_symbol_id is in cgdb_nodes."""
        import sqlite3
        from _builder.cgdb_store import SQLiteCGDBStore
        from _builder.cgdb_ingest import extract_cgdb_batch
        from _scanner.clang_scanner import ClangScanner

        scanner = ClangScanner(is_cpp=False)
        result = scanner.scan_file(self.c_path, self.tmpdir)
        self.assertNotIn('error', result)

        db_path = os.path.join(self.tmpdir, "test.db")
        store = SQLiteCGDBStore(db_path)
        try:
            # Create schema (cgdb tables + initial graph_versions row)
            store.create_schema()
            batch = extract_cgdb_batch(
                scan_result=result,
                commit_hash="abc123",
                version_id=1,
            )
            store.write_batch(batch)
            # Query the DB for nodes with enclosing_symbol_id set
            conn = store._ensure_conn()
            rows = conn.execute(
                "SELECT id, name, kind, enclosing_symbol_id FROM cgdb_nodes "
                "WHERE enclosing_symbol_id IS NOT NULL "
                "AND enclosing_symbol_id > 0 LIMIT 10"
            ).fetchall()
            self.assertGreater(len(rows), 0,
                               "Expected at least one node with enclosing_symbol_id set")
            # Verify at least one is a var or decl_ref inside a function
            kinds_with_enclosing = {r[2] for r in rows}
            self.assertTrue(
                'var' in kinds_with_enclosing or 'decl_ref' in kinds_with_enclosing,
                f"Expected var or decl_ref nodes with enclosing, got {kinds_with_enclosing}"
            )
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
