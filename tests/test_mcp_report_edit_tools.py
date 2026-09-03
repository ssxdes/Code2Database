"""Functional tests for the design-report edit/query tools in
mcp_report_tools.py.

The existing test_report_cli_commands.py only smoke-tests the CLI
wrappers (no-db -> graceful error).  These tests exercise the tool
handlers against a synthetic cgdb schema DB, covering:
  - insert_token / delete_token seq-shift correctness under
    UNIQUE(file_id, seq) with scrambled rowid order
  - alias_set against the real alias_sets schema
  - add_function with body_tokens
  - the writeback transaction workflow (begin -> edit -> commit /
    rollback) using the real transaction ids
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder import cgdb_schema
from _builder import mcp_report_tools as m


def _make_graph_dir(tokens=("t1", "t2", "t3", "t4")):
    """Create a temp graph dir with a minimal cgdb DB and return
    (graph_dir, db_path, file_id)."""
    d = tempfile.mkdtemp()
    db = os.path.join(d, "code2database.db")
    conn = sqlite3.connect(db)
    cgdb_schema.apply_cgdb_schema(conn)
    conn.commit()
    conn.execute(
        "INSERT INTO cgdb_files (path, language, sha256) "
        "VALUES ('/a.c', 'c', ?)", ("x" * 64,))
    conn.commit()
    fid = conn.execute("SELECT id FROM cgdb_files LIMIT 1").fetchone()[0]
    # Insert tokens with rowids in a different order than seq, so any
    # scan-order-dependent UPDATE bug is exposed.
    order = list(range(len(tokens)))
    order = order[1::2] + order[0::2]  # e.g. 1,3,0,2
    for rid in order:
        seq = rid + 1
        conn.execute(
            "INSERT INTO tokens (id, file_id, seq, kind, spelling, line, "
            "col, byte_offset, byte_length) VALUES (?, ?, ?, 'identifier', "
            "?, 1, ?, ?, 2)",
            (rid + 100, fid, seq, tokens[rid], seq, seq))
    conn.commit()
    conn.close()
    return d, db, fid


def _token_ids(db, spellings):
    conn = sqlite3.connect(db)
    out = []
    for sp in spellings:
        row = conn.execute(
            "SELECT id FROM tokens WHERE file_id = "
            "(SELECT id FROM cgdb_files LIMIT 1) AND spelling = ?",
            (sp,)).fetchone()
        out.append(row[0] if row else None)
    conn.close()
    return out


def _seqs(db):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT seq, spelling FROM tokens WHERE file_id = "
        "(SELECT id FROM cgdb_files LIMIT 1) ORDER BY seq").fetchall()
    conn.close()
    return [r[1] for r in rows]


class TestInsertTokenSeqShift(unittest.TestCase):
    def test_insert_midfile_shifts_correctly(self):
        d, db, fid = _make_graph_dir()
        _, t2, _, _ = _token_ids(db, ("t1", "t2", "t3", "t4"))
        res = m._tool_insert_token(
            {"after_token_id": t2,
             "tokens": [{"kind": "punct", "spelling": "+"},
                        {"kind": "identifier", "spelling": "z"}]},
            d)
        self.assertNotIn("error", res, res)
        # New tokens land right after the anchor; the rest shift up.
        self.assertEqual(_seqs(db), ["t1", "t2", "+", "z", "t3", "t4"])

    def test_insert_at_file_end(self):
        d, db, fid = _make_graph_dir()
        _, _, _, t4 = _token_ids(db, ("t1", "t2", "t3", "t4"))
        res = m._tool_insert_token(
            {"after_token_id": t4,
             "tokens": [{"kind": "punct", "spelling": ";"}]},
            d)
        self.assertNotIn("error", res, res)
        self.assertEqual(_seqs(db), ["t1", "t2", "t3", "t4", ";"])

class TestDeleteTokenSeqShift(unittest.TestCase):
    def test_delete_midfile_renumbers(self):
        d, db, fid = _make_graph_dir()
        _, _, t3, _ = _token_ids(db, ("t1", "t2", "t3", "t4"))
        res = m._tool_delete_token({"token_id": t3}, d)
        self.assertNotIn("error", res, res)
        self.assertEqual(_seqs(db), ["t1", "t2", "t4"])


class TestAliasSet(unittest.TestCase):
    def _seed_aliases(self, db):
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO cgdb_nodes (kind, name, fqn, line, col, byte_start, "
            "byte_end) VALUES "
            "('function', 'myfn', 'myfn', 1, 1, 0, 0),"
            "('function', 'otherfn', 'otherfn', 1, 1, 0, 0)")
        conn.commit()
        conn.execute(
            "INSERT INTO cgdb_nodes (kind, name, fqn, line, col, byte_start, "
            "byte_end, enclosing_symbol_id) VALUES "
            "('var', 'myvar', 'myvar', 2, 1, 0, 0, "
            "(SELECT id FROM cgdb_nodes WHERE name='myfn')),"
            "('var', 'infn', 'infn', 3, 1, 0, 0, "
            "(SELECT id FROM cgdb_nodes WHERE name='myfn')),"
            "('var', 'outfn', 'outfn', 4, 1, 0, 0, "
            "(SELECT id FROM cgdb_nodes WHERE name='otherfn'))")
        conn.commit()
        conn.execute(
            "INSERT INTO alias_sets (ptr1_node_id, ptr2_node_id, kind, "
            "confidence) VALUES ("
            "(SELECT id FROM cgdb_nodes WHERE name='myvar'), "
            "(SELECT id FROM cgdb_nodes WHERE name='infn'), "
            "'may_alias', 0.9), ("
            "(SELECT id FROM cgdb_nodes WHERE name='myvar'), "
            "(SELECT id FROM cgdb_nodes WHERE name='outfn'), "
            "'may_alias', 0.5)")
        conn.commit()
        conn.close()

    def test_alias_set_returns_aliases(self):
        d, db, fid = _make_graph_dir()
        self._seed_aliases(db)
        res = m._tool_alias_set({"variable": "myvar"}, d)
        self.assertEqual(len(res), 2, res)
        names = {r["alias_var"] for r in res}
        self.assertEqual(names, {"infn", "outfn"})
        for r in res:
            self.assertNotIn("error", r)
            self.assertIn(r["kind"], ("may_alias", "must_alias", "no_alias"))

    def test_alias_set_scope_filters_by_enclosing_function(self):
        d, db, fid = _make_graph_dir()
        self._seed_aliases(db)
        res = m._tool_alias_set({"variable": "myvar", "scope": "myfn"}, d)
        self.assertEqual(len(res), 1, res)
        self.assertEqual(res[0]["alias_var"], "infn")

    def test_alias_set_unknown_scope_returns_empty(self):
        d, db, fid = _make_graph_dir()
        self._seed_aliases(db)
        res = m._tool_alias_set(
            {"variable": "myvar", "scope": "no_such_fn"}, d)
        self.assertEqual(res, [])

    def test_alias_set_unknown_variable_returns_empty(self):
        d, db, fid = _make_graph_dir()
        res = m._tool_alias_set({"variable": "no_such_var"}, d)
        self.assertEqual(res, [])


class TestAddFunction(unittest.TestCase):
    def test_body_tokens_without_file_id_is_rejected(self):
        d, db, fid = _make_graph_dir()
        res = m._tool_add_function(
            {"signature": "int newfn(void)",
             "body_tokens": [{"kind": "identifier", "spelling": "x"}]},
            d)
        self.assertIn("error", res)
        self.assertIn("file_id", res["error"])

    def test_body_tokens_with_file_id_append_to_stream(self):
        d, db, fid = _make_graph_dir()
        res = m._tool_add_function(
            {"signature": "int newfn(void)", "file_id": fid,
             "body_tokens": [{"kind": "identifier", "spelling": "x"},
                             {"kind": "punct", "spelling": ";"}]},
            d)
        self.assertNotIn("error", res, res)
        self.assertEqual(len(res["token_ids"]), 2)
        conn = sqlite3.connect(db)
        node = conn.execute(
            "SELECT file_id FROM cgdb_nodes WHERE id = ?",
            (res["symbol_id"],)).fetchone()
        self.assertEqual(node[0], fid)
        seqs = [r[0] for r in conn.execute(
            "SELECT seq FROM tokens WHERE spelling IN ('x',';') "
            "ORDER BY seq").fetchall()]
        self.assertEqual(seqs, [5, 6])  # appended after existing t1..t4
        # uniqueness intact
        n = conn.execute(
            "SELECT COUNT(*) = COUNT(DISTINCT seq) FROM tokens "
            "WHERE file_id = ?", (fid,)).fetchone()[0]
        self.assertEqual(n, 1)
        conn.close()

    def test_unknown_file_id_is_rejected(self):
        d, db, fid = _make_graph_dir()
        res = m._tool_add_function(
            {"signature": "int newfn(void)", "file_id": 999,
             "body_tokens": [{"kind": "identifier", "spelling": "x"}]},
            d)
        self.assertIn("error", res)

    def test_signature_only_still_works(self):
        d, db, fid = _make_graph_dir()
        res = m._tool_add_function({"signature": "int newfn(void)"}, d)
        self.assertNotIn("error", res, res)
        self.assertEqual(res["token_ids"], [])


class TestWritebackTxIntegration(unittest.TestCase):
    """Edit tools must return REAL writeback transaction ids that
    commit_db_transaction / rollback_db_transaction recognize."""

    def test_edit_token_rollback_restores_previous_state(self):
        d, db, fid = _make_graph_dir()
        t2 = _token_ids(db, ("t1", "t2", "t3", "t4"))[1]
        res = m._tool_edit_token({"token_id": t2, "new_text": "REPLACED"}, d)
        self.assertNotIn("error", res, res)
        tx_id = res["transaction_id"]
        # real ids are uuids, not the old fake "edit_token_<id>" strings
        self.assertFalse(tx_id.startswith("edit_token_"))
        conn = sqlite3.connect(db)
        self.assertEqual(
            conn.execute("SELECT spelling FROM tokens WHERE id = ?",
                         (t2,)).fetchone()[0], "REPLACED")
        conn.close()
        rb = m._tool_rollback_db_transaction({"transaction_id": tx_id}, d)
        self.assertTrue(rb.get("rolled_back"), rb)
        conn = sqlite3.connect(db)
        self.assertEqual(
            conn.execute("SELECT spelling FROM tokens WHERE id = ?",
                         (t2,)).fetchone()[0], "t2")
        conn.close()

    def test_edit_token_tx_resolves_in_commit(self):
        d, db, fid = _make_graph_dir()
        t1 = _token_ids(db, ("t1", "t2", "t3", "t4"))[0]
        res = m._tool_edit_token({"token_id": t1, "new_text": "zz"}, d)
        tx_id = res["transaction_id"]
        # The synthetic fixture has no usable source_root / token layout,
        # so rendering may legitimately fail — but the tx must RESOLVE
        # (failure_stage != begin) instead of "no such transaction_id".
        out = m._tool_commit_db_transaction(
            {"transaction_id": tx_id, "run_compile": False}, d)
        self.assertNotIn("no such transaction_id", str(out), out)
        self.assertNotEqual(out.get("failure_stage"), "begin", out)

    def test_insert_and_delete_token_return_real_tx(self):
        d, db, fid = _make_graph_dir()
        ids = _token_ids(db, ("t1", "t2", "t3", "t4"))
        r1 = m._tool_insert_token(
            {"after_token_id": ids[1],
             "tokens": [{"kind": "punct", "spelling": "+"}]}, d)
        self.assertFalse(
            r1["transaction_id"].startswith("insert_token_"), r1)
        r2 = m._tool_delete_token({"token_id": ids[3]}, d)
        self.assertFalse(
            r2["transaction_id"].startswith("delete_token_"), r2)

    def test_add_function_tx_only_with_file(self):
        d, db, fid = _make_graph_dir()
        r = m._tool_add_function({"signature": "int f(void)"}, d)
        self.assertIsNone(r["transaction_id"])
        r = m._tool_add_function(
            {"signature": "int f(void)", "file_id": fid,
             "body_tokens": [{"kind": "identifier", "spelling": "y"}]}, d)
        self.assertIsNotNone(r["transaction_id"])
        self.assertFalse(r["transaction_id"].startswith("add_function_"))


if __name__ == "__main__":
    unittest.main()
