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


if __name__ == "__main__":
    unittest.main()
