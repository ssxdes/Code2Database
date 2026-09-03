"""Regression tests for SQLiteStore / _wipe_cgdb_data resilience against
broken or half-built code2database.db files.

Pins the guards introduced for two real-world crashes:
  1. sqlite_store._migrate_schema queried `meta` without checking it
     exists — a DB file left incomplete by a killed build crashed every
     later `connect()` with OperationalError: no such table: meta.
  2. graph_build._wipe_cgdb_data ran `DELETE FROM predicates` (and 18
     more cgdb tables) without checking they exist — same crash class
     with 'no such table: predicates'.

Also pins the actionable error for a corrupt (non-SQLite) db file, and
the legacy-schema upgrade path.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.sqlite_store import SQLiteStore
from _builder.graph_build import _wipe_cgdb_data


def _tmp_db():
    d = tempfile.mkdtemp()
    return d, os.path.join(d, "code2database.db")


class TestConnectOnBrokenDb(unittest.TestCase):
    def _assert_healthy(self, store):
        # After connect() the key tables must exist regardless of what
        # the db file looked like before.
        names = {
            r[0] for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("meta", names)
        self.assertIn("functions", names)
        self.assertIn("config_predicates", names)  # cgdb layer applied too

    def test_connect_fresh_dir_no_db_file(self):
        d, db = _tmp_db()
        store = SQLiteStore(db)
        store.connect()
        self._assert_healthy(store)
        store.close()

    def test_connect_zero_byte_file(self):
        # A 0-byte file is what a killed process leaves behind most
        # often (created but never written). SQLite treats it as a fresh
        # database — connect() must succeed, not raise about `meta`.
        d, db = _tmp_db()
        open(db, "w").close()
        store = SQLiteStore(db)
        store.connect()
        self._assert_healthy(store)
        store.close()

    def test_connect_partial_db_without_meta(self):
        # Incomplete build: some tables exist but `meta` was never
        # created. This is the exact 'no such table: meta' crash.
        d, db = _tmp_db()
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE functions (id TEXT PRIMARY KEY, name TEXT, "
            "domain TEXT, source_file TEXT, line_number INTEGER, "
            "signature TEXT, labels TEXT)")
        conn.commit()
        conn.close()
        store = SQLiteStore(db)
        store.connect()
        self._assert_healthy(store)
        store.close()

    def test_connect_corrupt_file_raises_actionable_error(self):
        # Random bytes: not a SQLite database at all. The user must get
        # a message that says WHAT to do, not a bare traceback.
        d, db = _tmp_db()
        with open(db, "wb") as f:
            f.write(os.urandom(4096))
        store = SQLiteStore(db)
        with self.assertRaises(RuntimeError) as ctx:
            store.connect()
        msg = str(ctx.exception)
        self.assertIn("not a valid SQLite database", msg)
        self.assertIn(db, msg)
        # original error chained for debugging
        self.assertIsInstance(ctx.exception.__cause__,
                              sqlite3.DatabaseError)

    def test_connect_upgrades_legacy_v1_schema(self):
        # Old db at schema_version=1 with fewer columns: migration must
        # bring `functions` up to the current column set.
        d, db = _tmp_db()
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO meta VALUES ('schema_version', '1');
            CREATE TABLE functions (id TEXT PRIMARY KEY, name TEXT,
                domain TEXT, source_file TEXT, line_number INTEGER,
                signature TEXT, labels TEXT);
        """)
        conn.commit()
        conn.close()
        store = SQLiteStore(db)
        store.connect()
        cols = {r[1] for r in store._conn.execute(
            "PRAGMA table_info(functions)")}
        self.assertIn("is_api_entry", cols)
        store.close()


class TestWipeCgdbDataOnMissingTables(unittest.TestCase):
    def test_wipe_on_schemaless_connection(self):
        # A raw connection to an EMPTY db (no cgdb tables at all): the
        # wipe must skip missing tables instead of raising — this is the
        # exact 'no such table' crash family the guards fixed.
        d, db = _tmp_db()
        conn = sqlite3.connect(db)
        try:
            _wipe_cgdb_data(conn)  # must not raise
            # and must not have created anything
            names = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(names, set())
        finally:
            conn.close()

    def test_wipe_list_names_all_exist_in_schema(self):
        # Every table in _CGDB_WIPE_TABLES must be a real schema table —
        # a dead name (like the pre-fix "predicates") makes every build
        # log a spurious warning/error.
        from _builder.cgdb_schema import apply_cgdb_schema
        from _builder.graph_build import _CGDB_WIPE_TABLES
        d, db = _tmp_db()
        conn = sqlite3.connect(db)
        try:
            apply_cgdb_schema(conn)
            names = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            for tbl in _CGDB_WIPE_TABLES:
                self.assertIn(tbl, names,
                              f"_CGDB_WIPE_TABLES entry {tbl!r} is not a "
                              f"real schema table")
        finally:
            conn.close()

    def test_wipe_after_full_connect(self):
        d, db = _tmp_db()
        store = SQLiteStore(db)
        store.connect()
        store._conn.execute(
            "INSERT INTO config_predicates (root_expr_id, text_form) "
            "VALUES (1, 'CONFIG_X')")
        store._conn.commit()
        _wipe_cgdb_data(store._conn)
        n = store._conn.execute(
            "SELECT COUNT(*) FROM config_predicates").fetchone()[0]
        self.assertEqual(n, 0)
        store.close()


class TestDeleteFileRecordsWithL1(unittest.TestCase):
    def test_delete_cleans_l1_rows_without_fk_violation(self):
        # delete_file_records used to roll back with 'FOREIGN KEY
        # constraint failed' for any L1-ingested file: with
        # foreign_keys=ON, deleting cgdb_files cascaded tokens, which
        # tripped literals' NO ACTION FK. It also never cleaned the L1
        # tables at all.
        from _builder.cgdb_schema import apply_cgdb_schema
        from _builder.cgdb_store import SQLiteCGDBStore
        d, db = _tmp_db()
        conn = sqlite3.connect(db)
        apply_cgdb_schema(conn)
        conn.execute(
            "INSERT INTO cgdb_files (id, path, language, sha256) "
            "VALUES (1, '/a.c', 'c', 'x')")
        conn.execute(
            "INSERT INTO tokens (id, file_id, seq, kind, spelling, "
            "line, col) VALUES (10, 1, 1, 'identifier', 'foo', 1, 1)")
        conn.execute(
            "INSERT INTO literals (id, kind, raw_text, token_id) "
            "VALUES (20, 'int', '0', 10)")
        conn.execute(
            "INSERT INTO string_literals (id, literal_id, decoded) "
            "VALUES (30, 20, 's')")
        conn.execute(
            "INSERT INTO macros (id, name, file_id, line) "
            "VALUES (40, 'M', 1, 1)")
        conn.commit()
        conn.close()

        store = SQLiteCGDBStore(db)  # conn with foreign_keys=ON
        try:
            nodes, edges = store.delete_file_records("/a.c")
            self.assertEqual(nodes, 0)
            conn = store._ensure_conn()
            for tbl in ("cgdb_files", "tokens", "literals",
                        "string_literals", "macros"):
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                self.assertEqual(n, 0, f"{tbl} not cleaned")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
