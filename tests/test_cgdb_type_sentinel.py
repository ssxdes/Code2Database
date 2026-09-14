"""cgdb_nodes.type_id sentinel handling (clang add_type fallback).

clang_scanner.add_type() returns 0 when cursor.type is None or the type
walk raised. cgdb_nodes.type_id carries a FOREIGN KEY to cgdb_types(id),
and type ids are hash-based (>=1) so id 0 never exists — a 0 written
through to the table rejects the whole bulk load at COMMIT (the store
runs with PRAGMA defer_foreign_keys=ON during bulk loads). The ingest
layer converts the sentinel to None, and the store coerces defensively
for any other NodeRecord producer.
"""

import os
import sys
import sqlite3
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.cgdb.cgdb_records import (  # noqa: E402
    IngestBatch, NodeRecord, TypeRecord, FileRecord)
from _builder.cgdb.cgdb_store import SQLiteCGDBStore  # noqa: E402


def _node_with_type(type_id, spelling=""):
    return NodeRecord(id=1001, kind="stmt", name="expr", fqn="expr",
                      file_id=1, line=10, col=1, byte_start=100,
                      byte_end=200, type_spelling=spelling,
                      type_id=type_id)


def _batch(nodes):
    return IngestBatch(
        file=FileRecord(id=1, path="test.c", language="c", sha256="x",
                        content_hash="x"),
        nodes=nodes,
        types=[TypeRecord(id=2001, spelling="int",
                          canonical_spelling="int", kind="builtin")],
    )


class TestTypeSentinelInStore(unittest.TestCase):
    def test_write_batch_coerces_zero_type_id(self):
        """A NodeRecord carrying type_id=0 must be stored as NULL —
        previously the 0 survived to the row and the deferred FK check
        rejected the COMMIT."""
        with tempfile.TemporaryDirectory() as d:
            store = SQLiteCGDBStore(os.path.join(d, "cgdb.db"))
            try:
                store.create_schema()
                store.write_batch(_batch([
                    _node_with_type(0),
                    _node_with_type(2001),
                    _node_with_type(None),
                ]))
            finally:
                store.close()
            conn = sqlite3.connect(os.path.join(d, "cgdb.db"))
            try:
                rows = dict(conn.execute(
                    "SELECT id, type_id FROM cgdb_nodes").fetchall())
                self.assertIsNone(rows[1001])
            finally:
                conn.close()

    def test_write_batch_rejects_dangling_nonzero_type_id(self):
        """Sanity: a genuinely dangling type id still fails the COMMIT —
        the coercion must only swallow the 0 sentinel."""
        with tempfile.TemporaryDirectory() as d:
            store = SQLiteCGDBStore(os.path.join(d, "cgdb.db"))
            try:
                store.create_schema()
                with self.assertRaises(sqlite3.IntegrityError):
                    store.write_batch(_batch([_node_with_type(999999)]))
            finally:
                store.close()


class TestTypeSentinelInIngest(unittest.TestCase):
    def test_extract_converts_sentinel_to_none(self):
        from _builder.cgdb.cgdb_ingest import extract_cgdb_batch
        scan = {
            "file": os.path.join(self._tmp, "test.c"),
            "cgdb_nodes": [
                # clang add_type failure: type_id=0, no spelling
                {"id": 5001, "kind": "stmt", "name": "e1", "fqn": "e1",
                 "line": 1, "col": 1, "byte_start": 0, "byte_end": 5,
                 "type_id": 0, "type_spelling": ""},
                # type_id=0 but a spelling exists -> backfill registers
                # a fresh type and the node must point at it (not 0).
                {"id": 5002, "kind": "var", "name": "v", "fqn": "v",
                 "line": 2, "col": 1, "byte_start": 6, "byte_end": 9,
                 "type_id": 0, "type_spelling": "struct foo *"},
                # healthy reference unchanged
                {"id": 5003, "kind": "parm", "name": "p", "fqn": "p",
                 "line": 3, "col": 1, "byte_start": 10, "byte_end": 12,
                 "type_id": 2001, "type_spelling": "int"},
            ],
            "cgdb_types": [
                {"id": 2001, "spelling": "int", "canonical_spelling": "int",
                 "kind": "builtin"},
            ],
        }
        batch = extract_cgdb_batch(scan, commit_hash="", version_id=1)
        by_id = {n.id: n for n in batch.nodes}
        self.assertIsNone(by_id[5001].type_id)
        self.assertIsNotNone(by_id[5002].type_id)
        self.assertNotEqual(by_id[5002].type_id, 0)
        self.assertIn(by_id[5002].type_id,
                      {t.id for t in batch.types})
        self.assertEqual(by_id[5003].type_id, 2001)

    def test_extracted_batch_commits_with_sentinel_nodes(self):
        from _builder.cgdb.cgdb_ingest import extract_cgdb_batch
        scan = {
            "file": os.path.join(self._tmp, "test.c"),
            "cgdb_nodes": [
                {"id": 5001, "kind": "stmt", "name": "e1", "fqn": "e1",
                 "line": 1, "col": 1, "byte_start": 0, "byte_end": 5,
                 "type_id": 0, "type_spelling": ""},
            ],
            "cgdb_types": [],
        }
        batch = extract_cgdb_batch(scan, commit_hash="", version_id=1)
        with tempfile.TemporaryDirectory() as d:
            store = SQLiteCGDBStore(os.path.join(d, "cgdb.db"))
            try:
                store.create_schema()
                store.write_batch(batch)  # must not raise at COMMIT
            finally:
                store.close()

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="type_sentinel_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
