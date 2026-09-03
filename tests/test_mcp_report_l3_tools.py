"""Regression tests for the design-report L3 IR tools.

These tables (ssa_values / data_deps / indirect_calls / ir_functions /
path_states) currently have no build-side writers — the tools are
exercised against synthetic cgdb-schema DBs so their SQL is correct the
day the IR layer starts populating them:

  - trace_data_flow: the recursive CTE only expanded chains that had
    already reached the TARGET — multi-hop paths were never walked
    (result was from's direct deps ∪ target's deps).
  - indirect_targets: filtered by line only — same line in every file
    matched (cross-file false positives). Now joins cgdb_edges for the
    file filter.
  - path_sensitive_states: fed a cgdb_nodes.id into
    path_states.function_id, which references ir_functions(id).
  - find_macros: N+1 macro_invocations queries (up to 501 per call).
  - cgdb schema v5: idx_tokens_ast_node (find_symbol/delete_node full
    scans on the largest table otherwise) + v4→v5 migration.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder import cgdb_schema
from _builder import mcp_report_tools as m


def _make_db():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "code2database.db")
    conn = sqlite3.connect(db)
    cgdb_schema.apply_cgdb_schema(conn)
    conn.commit()
    return d, db, conn


class TestTraceDataFlow(unittest.TestCase):
    def test_multi_hop_reachability(self):
        d, db, conn = _make_db()
        conn.execute(
            "INSERT INTO ssa_values (id, function_id, value_name, "
            "def_kind, def_line) VALUES "
            "(1, 10, 'src', 'instruction', 1), "
            "(2, 10, 'mid', 'instruction', 2), "
            "(3, 10, 'dst', 'instruction', 3)")
        # src -> mid -> dst (2 hops)
        conn.execute(
            "INSERT INTO data_deps (from_ssa_id, to_ssa_id, kind, "
            "function_id) VALUES "
            "(1, 2, 'def-use', 10), (2, 3, 'def-use', 10)")
        conn.commit()
        conn.close()
        res = m._tool_trace_data_flow(
            {"from_var": "src", "to_var": "dst"}, d)
        self.assertNotIn("error", res, res)
        self.assertTrue(res["reached"])
        self.assertEqual(len(res["path"]), 2)

    def test_unreachable_target(self):
        d, db, conn = _make_db()
        conn.execute(
            "INSERT INTO ssa_values (id, function_id, value_name, "
            "def_kind) VALUES (1, 10, 'src', 'instruction'), "
            "(2, 10, 'other', 'instruction'), "
            "(3, 10, 'dst', 'instruction')")
        conn.execute(
            "INSERT INTO data_deps (from_ssa_id, to_ssa_id, kind, "
            "function_id) VALUES (1, 2, 'def-use', 10)")
        conn.commit()
        conn.close()
        res = m._tool_trace_data_flow(
            {"from_var": "src", "to_var": "dst"}, d)
        self.assertNotIn("error", res, res)
        self.assertFalse(res["reached"])

    def test_missing_to_var_is_an_error(self):
        d, db, conn = _make_db()
        conn.execute(
            "INSERT INTO ssa_values (id, function_id, value_name, "
            "def_kind) VALUES (1, 10, 'src', 'instruction')")
        conn.commit()
        conn.close()
        res = m._tool_trace_data_flow(
            {"from_var": "src", "to_var": "nothere"}, d)
        self.assertIn("error", res)
        self.assertIn("nothere", res["error"])


class TestIndirectTargetsFileFilter(unittest.TestCase):
    def test_same_line_other_file_excluded(self):
        d, db, conn = _make_db()
        conn.execute(
            "INSERT INTO cgdb_files (id, path, language, sha256) "
            "VALUES (11, '/a.c', 'c', 'x'), (12, '/b.c', 'c', 'x')")
        conn.execute(
            "INSERT INTO cgdb_nodes (id, kind, name, fqn, file_id, "
            "line, col, byte_start, byte_end) VALUES "
            "(50, 'function', 'target', 'target', 11, 1, 1, 0, 0)")
        conn.execute(
            "INSERT INTO cgdb_edges (id, src_id, dst_id, kind, file_id) "
            "VALUES (70, 50, 50, 'INVOKES', 11), "
            "(71, 50, 50, 'INVOKES', 12)")
        conn.execute(
            "INSERT INTO indirect_calls (call_site_id, call_edge_id, "
            "function_id, line, col, possible_target_symbol_id, "
            "confidence, analysis) VALUES "
            "(1, 70, 1, 42, 1, 50, 0.9, 'heuristic'), "
            "(2, 71, 1, 42, 1, 50, 0.9, 'heuristic')")
        conn.commit()
        conn.close()
        for fid in (11, 12):
            res = m._tool_indirect_targets(
                {"call_site_loc": {"file_id": fid, "line": 42}}, d)
            self.assertEqual(len(res), 1, f"file {fid}: {res}")


class TestPathSensitiveStatesIdSpace(unittest.TestCase):
    def test_resolves_via_ir_functions(self):
        d, db, conn = _make_db()
        conn.execute(
            "INSERT INTO cgdb_files (id, path, language, sha256) "
            "VALUES (11, '/a.c', 'c', 'x')")
        conn.execute(
            "INSERT INTO cgdb_nodes (id, kind, name, fqn, file_id, "
            "line, col, byte_start, byte_end) VALUES "
            "(50, 'function', 'target', 'target', 11, 1, 1, 0, 0)")
        # ir_functions.id deliberately != cgdb_nodes.id
        conn.execute(
            "INSERT INTO ir_functions (id, symbol_id, ir_name) "
            "VALUES (90, 50, 'target')")
        conn.execute(
            "INSERT INTO path_states (id, function_id, block_id, "
            "path_id, constraints, state, line) VALUES "
            "(1, 90, 1, 'p1', '{}', '{}', 5)")
        conn.commit()
        conn.close()
        res = m._tool_path_sensitive_states({"fn": "target"}, d)
        self.assertNotIn("error", res, res)
        self.assertEqual(len(res["paths"]), 1)


class TestFindMacrosBatched(unittest.TestCase):
    def test_uses_collected_for_all_macros(self):
        d, db, conn = _make_db()
        conn.execute(
            "INSERT INTO cgdb_files (id, path, language, sha256) "
            "VALUES (11, '/a.c', 'c', 'x')")
        for mid, name in ((100, "MAC_A"), (101, "MAC_B")):
            conn.execute(
                "INSERT INTO macros (id, name, file_id, line, params, "
                "body_text) VALUES (?, ?, 11, 1, '[]', 'x')",
                (mid, name))
            for i in range(3):
                conn.execute(
                    "INSERT INTO macro_invocations (macro_id, file_id, "
                    "line, col) VALUES (?, 11, ?, 1)", (mid, i + 1))
        conn.commit()
        conn.close()
        res = m._tool_find_macros({}, d)  # no name -> all macros
        self.assertEqual(len(res), 2)
        for entry in res:
            self.assertEqual(len(entry["used_at"]), 3, entry)


class TestSchemaV5AstNodeIndex(unittest.TestCase):
    def test_fresh_db_has_index(self):
        d, db, conn = _make_db()
        idx = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_tokens_ast_node'").fetchone()
        ver = conn.execute(
            "SELECT value FROM meta WHERE key='cgdb_schema_version'"
        ).fetchone()[0]
        conn.close()
        self.assertIsNotNone(idx)
        self.assertEqual(ver, "5")

    def test_v4_db_migrates_to_v5(self):
        d, db, conn = _make_db()
        # simulate a v4 db: version marker + index absent
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) "
            "VALUES ('cgdb_schema_version', '4')")
        conn.execute("DROP INDEX idx_tokens_ast_node")
        conn.commit()
        conn.close()
        conn = sqlite3.connect(db)
        cgdb_schema.apply_cgdb_schema(conn)
        idx = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_tokens_ast_node'").fetchone()
        ver = conn.execute(
            "SELECT value FROM meta WHERE key='cgdb_schema_version'"
        ).fetchone()[0]
        conn.close()
        self.assertIsNotNone(idx)
        self.assertEqual(ver, "5")


if __name__ == "__main__":
    unittest.main()
