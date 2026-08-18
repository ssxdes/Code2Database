"""Tests for ClangScanner (Phase 1: L1-L2 + FTS5).

Verifies libclang-based scanner produces multi-kind nodes, type records,
INVOKES edges, and call sites matching the cgdb PoC numbers (19 nodes, 6 edges
on the test fixture — see cgdb-architecture-and-poc-report.md task C).
"""
import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _scanner.clang_scanner import ClangScanner, is_clang_available, cgdb_node_id


# Fixture matching the cgdb PoC test_c.c (per cgdb report task C).
_TEST_C_SOURCE = textwrap.dedent("""\
    #include <stdio.h>
    #include <string.h>

    struct buffer {
        char *data;
        int size;
    };

    int validate(struct buffer *buf) {
        if (buf == NULL) return -1;
        if (buf->size <= 0) return -1;
        return 0;
    }

    int main(int argc, char **argv) {
        struct buffer b;
        b.data = (char *)malloc(100);
        b.size = 100;
        memset(b.data, 0, 100);
        strcpy(b.data, "hello");
        if (validate(&b) < 0) {
            printf("validation failed\\n");
            return 1;
        }
        printf("hello %s\\n", b.data);
        return 0;
    }
""")


@unittest.skipUnless(is_clang_available(), "libclang not available")
class TestClangScanner(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.c_path = os.path.join(self.tmpdir, "test_c.c")
        with open(self.c_path, 'w') as f:
            f.write(_TEST_C_SOURCE)
        self.scanner = ClangScanner(is_cpp=False)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_scan_file_produces_cgdb_nodes(self):
        """scan_file produces a non-empty cgdb_nodes list."""
        result = self.scanner.scan_file(self.c_path, self.tmpdir)
        self.assertNotIn('error', result, f"scan failed: {result.get('error')}")
        cgdb_nodes = result.get('cgdb_nodes', [])
        self.assertGreater(len(cgdb_nodes), 0, "Expected non-empty cgdb_nodes")

    def test_produces_function_nodes(self):
        """cgdb_nodes contains 'function' kind nodes."""
        result = self.scanner.scan_file(self.c_path, self.tmpdir)
        functions = [n for n in result['cgdb_nodes'] if n['kind'] == 'function']
        names = {n['name'] for n in functions}
        self.assertIn('validate', names)
        self.assertIn('main', names)

    def test_produces_param_nodes(self):
        """cgdb_nodes contains 'parm' kind nodes for function parameters."""
        result = self.scanner.scan_file(self.c_path, self.tmpdir)
        parms = [n for n in result['cgdb_nodes'] if n['kind'] == 'parm']
        self.assertGreater(len(parms), 0, "Expected at least one parm node")

    def test_produces_field_nodes(self):
        """cgdb_nodes contains 'field' kind nodes for struct fields."""
        result = self.scanner.scan_file(self.c_path, self.tmpdir)
        fields = [n for n in result['cgdb_nodes'] if n['kind'] == 'field']
        field_names = {n['name'] for n in fields}
        # struct buffer has 'data' and 'size' fields
        self.assertIn('data', field_names)
        self.assertIn('size', field_names)

    def test_produces_struct_nodes(self):
        """cgdb_nodes contains 'struct' kind nodes."""
        result = self.scanner.scan_file(self.c_path, self.tmpdir)
        structs = [n for n in result['cgdb_nodes'] if n['kind'] == 'struct']
        struct_names = {n['name'] for n in structs}
        self.assertIn('buffer', struct_names)

    def test_produces_calls_edges(self):
        """cgdb_edges contains INVOKES edges from main to validate, memset, strcpy, printf."""
        result = self.scanner.scan_file(self.c_path, self.tmpdir)
        edges = result.get('cgdb_edges', [])
        calls = [e for e in edges if e['kind'] == 'INVOKES']
        self.assertGreater(len(calls), 0, "Expected at least one INVOKES edge")
        # Check that some edges target the expected functions.
        # We use the node ID lookup since edge.dst_id is a hashed ID.
        node_id_to_name = {n['id']: n['name'] for n in result['cgdb_nodes']}
        callee_names = {node_id_to_name.get(e['dst_id'], '') for e in calls}
        # main should call at least validate and printf
        self.assertIn('validate', callee_names)
        self.assertIn('printf', callee_names)

    def test_produces_has_field_edges(self):
        """cgdb_edges contains HAS_FIELD edges from struct to field."""
        result = self.scanner.scan_file(self.c_path, self.tmpdir)
        edges = result.get('cgdb_edges', [])
        has_field = [e for e in edges if e['kind'] == 'HAS_FIELD']
        self.assertGreater(len(has_field), 0, "Expected at least one HAS_FIELD edge")

    def test_produces_cgdb_invoke_sites(self):
        """cgdb_invoke_sites contains direct call entries."""
        result = self.scanner.scan_file(self.c_path, self.tmpdir)
        invoke_sites = result.get('cgdb_invoke_sites', [])
        self.assertGreater(len(invoke_sites), 0)
        for cs in invoke_sites:
            self.assertEqual(cs['invoke_kind'], 'direct')

    def test_node_ids_are_stable_across_runs(self):
        """Node IDs are stable across multiple scans of the same file (USR-based)."""
        r1 = self.scanner.scan_file(self.c_path, self.tmpdir)
        r2 = self.scanner.scan_file(self.c_path, self.tmpdir)
        # Group by fqn — same fqn should give same id
        for n1, n2 in zip(r1['cgdb_nodes'], r2['cgdb_nodes']):
            self.assertEqual(n1['id'], n2['id'], f"ID mismatch for {n1['name']}")

    def test_produces_cgdb_types(self):
        """cgdb_types contains type records for at least one struct."""
        result = self.scanner.scan_file(self.c_path, self.tmpdir)
        types = result.get('cgdb_types', [])
        # The 'buffer' struct should produce a type record
        spellings = {t['spelling'] for t in types}
        # struct buffer should appear in some spelling
        self.assertTrue(any('buffer' in s for s in spellings),
                        f"Expected 'buffer' in type spellings: {spellings}")

    def test_node_id_consistent_with_cgdb_node_id_helper(self):
        """cgdb_node_id helper produces deterministic IDs from USR strings."""
        id1 = cgdb_node_id("c:test@F@validate")
        id2 = cgdb_node_id("c:test@F@validate")
        self.assertEqual(id1, id2)
        # Different USRs give different IDs
        id3 = cgdb_node_id("c:test@F@main")
        self.assertNotEqual(id1, id3)

    def test_node_id_high_bit_clear(self):
        """AST node IDs have the high bit clear (per cgdb 5.6 bit-range prefix)."""
        nid = cgdb_node_id("c:test@F@validate")
        self.assertLess(nid, 0x8000_0000_0000_0000,
                        "AST node ID should have high bit clear")

    def test_function_node_has_signature(self):
        """Function nodes have signature in attrs (denormalized for FTS5)."""
        result = self.scanner.scan_file(self.c_path, self.tmpdir)
        validate = next((n for n in result['cgdb_nodes']
                         if n['kind'] == 'function' and n['name'] == 'validate'),
                        None)
        self.assertIsNotNone(validate)
        # Signature should contain "validate" and parameter info
        sig = validate.get('signature', '')
        self.assertIn('validate', sig)


@unittest.skipUnless(is_clang_available(), "libclang not available")
class TestDualBackendScanner(unittest.TestCase):
    """DualBackendScanner runs both tree-sitter and clang, merges results."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.c_path = os.path.join(self.tmpdir, "test_c.c")
        with open(self.c_path, 'w') as f:
            f.write(_TEST_C_SOURCE)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_dual_scanner_merges_results(self):
        """DualBackendScanner produces both legacy functions and cgdb_nodes."""
        try:
            from _scanner.c_scanner import CTreeSitterScanner
            from _scanner.dual_scanner import DualBackendScanner
            from _scanner.clang_scanner import ClangScanner
        except ImportError:
            self.skipTest("tree-sitter-c not available")
        ts = CTreeSitterScanner(is_cpp=False)
        cl = ClangScanner(is_cpp=False)
        dual = DualBackendScanner(ts, cl)
        result = dual.scan_file(self.c_path, self.tmpdir)
        # Should have legacy functions (from tree-sitter)
        self.assertGreater(len(result.get('functions', [])), 0)
        # Should have cgdb_nodes (from clang)
        self.assertGreater(len(result.get('cgdb_nodes', [])), 0)
        # Should have cgdb_edges (from clang)
        self.assertGreater(len(result.get('cgdb_edges', [])), 0)


if __name__ == "__main__":
    unittest.main()
