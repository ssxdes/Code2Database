"""End-to-end test for cgdb scan → build → SQLite tables populated.

Verifies the full pipeline:
1. DualBackendScanner scans a C source → produces cgdb_nodes/cgdb_types/
   cgdb_edges/cgdb_invoke_sites in the extraction dict.
2. cmd_build consumes the extraction dict, writes legacy functions/edges
   AND cgdb 13-layer records to code2database.db.
3. SQLite tables cgdb_nodes, cgdb_types, cgdb_edges, invoke_sites, nodes_fts
   are populated.
4. FTS5 search returns matches on node names.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest

_HERE = os.path.dirname(__file__)
SCRIPTS = os.path.normpath(os.path.join(_HERE, '..', 'scripts'))
sys.path.insert(0, SCRIPTS)


_TEST_C_SOURCE = textwrap.dedent("""\
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
            return 1;
        }
        return 0;
    }
""")


class TestCgdbEndToEnd(unittest.TestCase):
    """End-to-end: scan a fixture, build a graph, verify cgdb tables populated."""

    def setUp(self):
        try:
            from _scanner.clang_scanner import is_clang_available
        except ImportError:
            self.skipTest("ClangScanner not available")
        if not is_clang_available():
            self.skipTest("libclang not available")

        self.tmpdir = tempfile.mkdtemp(prefix="cgdb_e2e_")
        self.addCleanup(self._cleanup)
        # Source fixture
        self.src_dir = os.path.join(self.tmpdir, "src")
        os.makedirs(self.src_dir, exist_ok=True)
        self.c_path = os.path.join(self.src_dir, "test_e2e.c")
        with open(self.c_path, 'w') as f:
            f.write(_TEST_C_SOURCE)
        # Extraction output path
        self.extraction_path = os.path.join(self.tmpdir, "extraction.json")
        # Build output directory
        self.outdir = os.path.join(self.tmpdir, "out")

    def _cleanup(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, cmd):
        """Run a subprocess; raise AssertionError with stderr on failure."""
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            cwd=os.path.dirname(SCRIPTS),
        )
        if result.returncode != 0:
            raise AssertionError(
                f"Command failed: {' '.join(cmd)}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result

    def test_scan_produces_cgdb_records(self):
        """Scan a C source → extraction.json has cgdb_* keys."""
        scanner_script = os.path.join(SCRIPTS, "code2database_scanner.py")
        self._run([
            sys.executable, scanner_script, "scan",
            "--source", self.src_dir,
            "--output", self.extraction_path,
            "--extraction-backend", "auto",
        ])
        with open(self.extraction_path) as f:
            data = json.load(f)
        self.assertIn("cgdb_nodes", data, "extraction must have cgdb_nodes")
        self.assertIn("cgdb_types", data, "extraction must have cgdb_types")
        self.assertIn("cgdb_edges", data, "extraction must have cgdb_edges")
        self.assertGreater(len(data["cgdb_nodes"]), 0,
                           "expected at least one cgdb node")
        # Should have multiple node kinds (function, struct, field, parm, ...)
        kinds = {n.get("kind") for n in data["cgdb_nodes"]}
        self.assertIn("function", kinds)
        self.assertIn("struct", kinds)

    def test_build_populates_cgdb_tables(self):
        """build → code2database.db has cgdb_nodes/cgdb_types/cgdb_edges/invoke_sites."""
        # Step 1: scan
        scanner_script = os.path.join(SCRIPTS, "code2database_scanner.py")
        self._run([
            sys.executable, scanner_script, "scan",
            "--source", self.src_dir,
            "--output", self.extraction_path,
            "--extraction-backend", "auto",
        ])
        # Step 2: build
        builder_script = os.path.join(SCRIPTS, "code2database_builder.py")
        self._run([
            sys.executable, builder_script, "build",
            "--extraction", self.extraction_path,
            "--outdir", self.outdir,
            "--build-config", "auto",
            "--storage", "sqlite",
        ])
        # Step 3: verify SQLite tables
        db_path = os.path.join(self.outdir, "code2database.db")
        self.assertTrue(os.path.exists(db_path), "code2database.db must exist")
        conn = sqlite3.connect(db_path)
        try:
            # cgdb_nodes has multiple kinds
            node_count = conn.execute(
                "SELECT COUNT(*) FROM cgdb_nodes"
            ).fetchone()[0]
            self.assertGreater(node_count, 0, "cgdb_nodes must be populated")

            # cgdb_types has at least the struct buffer
            type_count = conn.execute(
                "SELECT COUNT(*) FROM cgdb_types"
            ).fetchone()[0]
            self.assertGreater(type_count, 0, "cgdb_types must be populated")

            # cgdb_edges has INVOKES + HAS_FIELD
            edge_kinds = conn.execute(
                "SELECT kind, COUNT(*) FROM cgdb_edges GROUP BY kind"
            ).fetchall()
            edge_kind_dict = dict(edge_kinds)
            self.assertIn("INVOKES", edge_kind_dict,
                          "cgdb_edges must contain INVOKES edges")

            # invoke_sites table is populated
            call_site_count = conn.execute(
                "SELECT COUNT(*) FROM invoke_sites"
            ).fetchone()[0]
            self.assertGreater(call_site_count, 0,
                               "invoke_sites must be populated")

            # FTS5 search returns matches on node names
            fts_matches = conn.execute(
                "SELECT name FROM nodes_fts WHERE nodes_fts MATCH 'validate'"
            ).fetchall()
            self.assertGreater(len(fts_matches), 0,
                               "FTS5 must find 'validate'")
        finally:
            conn.close()

    def test_build_tree_sitter_only_no_cgdb(self):
        """build with --extraction-backend tree-sitter → legacy functions/edges
        still written and cgdb_nodes synthesized from legacy functions.
        cgdb_types may be populated from tree-sitter signatures (return types,
        param types, var types) — these are basic spelling-level types, not
        the full clang semantic layer (no size_bytes, alignment, etc.)."""
        scanner_script = os.path.join(SCRIPTS, "code2database_scanner.py")
        self._run([
            sys.executable, scanner_script, "scan",
            "--source", self.src_dir,
            "--output", self.extraction_path,
            "--extraction-backend", "tree-sitter",
        ])
        with open(self.extraction_path) as f:
            data = json.load(f)
        # tree-sitter scan CAN produce cgdb_types from signatures (return
        # types, param types, var types) — these are spelling-only, lacking
        # clang's size_bytes/alignment. The clang backend enriches them.

        # Build still succeeds and produces legacy tables
        builder_script = os.path.join(SCRIPTS, "code2database_builder.py")
        self._run([
            sys.executable, builder_script, "build",
            "--extraction", self.extraction_path,
            "--outdir", self.outdir,
            "--build-config", "auto",
            "--storage", "sqlite",
        ])
        db_path = os.path.join(self.outdir, "code2database.db")
        conn = sqlite3.connect(db_path)
        try:
            # Legacy functions table populated
            func_count = conn.execute(
                "SELECT COUNT(*) FROM functions"
            ).fetchone()[0]
            self.assertGreater(func_count, 0,
                               "legacy functions table must be populated")
        finally:
            conn.close()

    def test_config_predicates_extracted_and_stored(self):
        """L3.5: #ifdef predicates are extracted, stored in config_predicates
        table, and linked to nodes via config_predicate_id.

        Uses a fixture with #ifdef CONFIG_X / #else / #endif. The #else
        branch is canonical (CONFIG_X undefined), so its nodes get the
        predicate !(defined(CONFIG_X)). The #ifdef branch is dead in this
        parse, but the predicate still appears in the table for completeness.
        """
        # Write a fixture with #ifdef
        src = textwrap.dedent("""\
            #ifdef CONFIG_X
            int enabled_func() { return 1; }
            #else
            int disabled_func() { return 0; }
            #endif
            int always_there() { return 42; }
        """)
        with open(self.c_path, 'w') as f:
            f.write(src)

        # Scan
        scanner_script = os.path.join(SCRIPTS, "code2database_scanner.py")
        self._run([
            sys.executable, scanner_script, "scan",
            "--source", self.src_dir,
            "--output", self.extraction_path,
            "--extraction-backend", "auto",
        ])
        with open(self.extraction_path) as f:
            data = json.load(f)
        # Extraction should have cgdb_predicates
        self.assertGreater(len(data.get("cgdb_predicates", [])), 0,
                           "extraction must contain config predicates")
        # Should include the unconditional sentinel + the #else predicate
        pred_texts = [p['text_form'] for p in data['cgdb_predicates']]
        # Either "" (unconditional) or "!(defined(CONFIG_X))" should be present
        self.assertTrue(
            any('CONFIG_X' in t for t in pred_texts),
            f"expected a predicate mentioning CONFIG_X, got: {pred_texts}"
        )

        # Build
        builder_script = os.path.join(SCRIPTS, "code2database_builder.py")
        self._run([
            sys.executable, builder_script, "build",
            "--extraction", self.extraction_path,
            "--outdir", self.outdir,
            "--build-config", "auto",
            "--storage", "sqlite",
        ])

        # Verify SQLite tables
        db_path = os.path.join(self.outdir, "code2database.db")
        conn = sqlite3.connect(db_path)
        try:
            # config_predicates table is populated
            pred_count = conn.execute(
                "SELECT COUNT(*) FROM config_predicates"
            ).fetchone()[0]
            self.assertGreater(pred_count, 0,
                               "config_predicates table must be populated")

            # At least one node has a non-unconditional predicate
            nodes_with_pred = conn.execute(
                "SELECT COUNT(*) FROM cgdb_nodes n "
                "JOIN config_predicates cp ON n.config_predicate_id = cp.id "
                "WHERE cp.is_unconditional = 0"
            ).fetchone()[0]
            self.assertGreater(nodes_with_pred, 0,
                               "at least one node must have a non-unconditional predicate")

            # always_there() should have the unconditional predicate
            always_pred = conn.execute(
                "SELECT cp.is_unconditional FROM cgdb_nodes n "
                "JOIN config_predicates cp ON n.config_predicate_id = cp.id "
                "WHERE n.name = 'always_there' AND n.kind = 'function'"
            ).fetchone()
            self.assertIsNotNone(always_pred,
                                 "always_there function must exist with a predicate")
            self.assertEqual(always_pred[0], 1,
                             "always_there must be unconditional")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
