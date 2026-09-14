"""Child-table cleanup before functions DELETE (FK ordering).

SQLiteStore connections run with PRAGMA foreign_keys=ON, so deleting a
functions row while child rows still reference it raises
IntegrityError at DELETE time. Two paths removed functions without
cleaning every child table:

- build_update._delete_legacy_rows cleaned edges/field_access/
  global_access but not entry_scores, so updating a file that contained
  entry-scored nodes crashed the whole build-update run.
- StreamingGraph.remove_node (deferred mode) deleted edges but no
  access/entry_scores rows, so removing a flushed node with access data
  crashed the build mid-way.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.graph.sqlite_store import SQLiteStore  # noqa: E402
from _builder.graph.streaming_graph import StreamingGraph  # noqa: E402


_FUNC = {"id": "src_util_add", "name": "add", "domain": "root",
         "source_file": "src/util/math.c", "line": 3,
         "fields_read": [{"struct_chain": "ops", "field_name": "pool",
                          "is_param": False}],
         "fields_written": [{"struct_chain": "ops", "field_name": "pool",
                             "is_param": False, "assigned_value": "NULL"}],
         "globals_read": [{"name": "g_counter"}]}


class TestDeleteLegacyRowsCleansEntryScores(unittest.TestCase):
    def test_entry_scores_removed_before_functions(self):
        from _builder.build.build_update import _delete_legacy_rows
        import sqlite3

        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "code2database.db")
            with SQLiteStore(db) as st:
                st.store_functions([dict(_FUNC)])
                st.store_entry_scores([
                    {"id": _FUNC["id"], "name": "add",
                     "score": 0.9, "domain": "root"}])
                st.store_field_access_batch([dict(_FUNC)])
                st.store_global_access_batch([dict(_FUNC)])

            conn = sqlite3.connect(db)
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                removed = _delete_legacy_rows(
                    conn, os.path.join(d, "src/util/math.c"), d)
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(removed, 1)
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM entry_scores")
                    .fetchone()[0], 0)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM field_access")
                    .fetchone()[0], 0)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM global_access")
                    .fetchone()[0], 0)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM functions")
                    .fetchone()[0], 0)
            finally:
                conn.close()


class TestStreamingGraphRemoveNodeCleansChildren(unittest.TestCase):
    def test_remove_flushed_node_with_access_rows(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "code2database.db")
            sg = StreamingGraph(db)
            try:
                sg.set_deferred(True)
                sg.add_node(_FUNC["id"], name=_FUNC["name"],
                            domain=_FUNC["domain"],
                            source_file=_FUNC["source_file"],
                            line=_FUNC["line"],
                            fields_read=_FUNC["fields_read"],
                            fields_written=_FUNC["fields_written"],
                            globals_read=_FUNC["globals_read"])
                # Force the node + access rows into SQLite (batch flush).
                sg._flush_functions()
                sg._store.store_field_access_batch(
                    [dict(_FUNC, id=_FUNC["id"])], autocommit=False)
                sg._store.store_global_access_batch(
                    [dict(_FUNC, id=_FUNC["id"])], autocommit=False)
                sg._store._conn.commit()
                with_entry = sg._store._conn.execute(
                    "SELECT COUNT(*) FROM field_access").fetchone()[0]
                # one read row + one write row
                self.assertEqual(with_entry, 2)

                # Must not raise IntegrityError.
                sg.remove_node(_FUNC["id"])
                sg._store._conn.commit()

                for tbl in ("functions", "edges", "field_access",
                            "global_access"):
                    left = sg._store._conn.execute(
                        f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                    self.assertEqual(left, 0, tbl)
            finally:
                sg.close()

    def test_remove_node_without_access_rows_still_works(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "code2database.db")
            sg = StreamingGraph(db)
            try:
                sg.set_deferred(True)
                sg.add_node("plain", name="plain", domain="root",
                            source_file="a.c", line=1)
                sg._flush_functions()
                sg.remove_node("plain")
                sg._store._conn.commit()
                left = sg._store._conn.execute(
                    "SELECT COUNT(*) FROM functions").fetchone()[0]
                self.assertEqual(left, 0)
            finally:
                sg.close()


if __name__ == "__main__":
    unittest.main()
