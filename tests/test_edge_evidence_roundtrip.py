"""Edge evidence round-trip through the SQLite backends.

store_edges() writes plain strings verbatim and json.dumps()es list/dict
values. Every reader must invert that exact contract: a plain-text
evidence string (the common producer form, e.g. "vtable_dispatch: X.y=Z")
has to come back as the same string, and only genuine JSON arrays/objects
come back as list/dict. The readers previously initialized ``[]`` and kept
it whenever json.loads failed, silently destroying every plain-text
evidence string read back from SQLite — split_by_domain then serialized
the empty list into the master JSON, and validators lost the evidence
text entirely (or crashed on re.match with a list).
"""

import os
import sys
import json
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.graph.streaming_graph import (  # noqa: E402
    LazySQLiteGraph, decode_edge_evidence)
from _builder.graph.sqlite_store import SQLiteStore  # noqa: E402


class TestDecodeEdgeEvidenceHelper(unittest.TestCase):
    """Unit checks for the shared decoder."""

    def test_none_and_empty_stay_empty_list(self):
        self.assertEqual(decode_edge_evidence(None), [])
        self.assertEqual(decode_edge_evidence(""), [])

    def test_plain_text_returns_original_string(self):
        ev = "vtable_dispatch: ops.pool.run=handler"
        self.assertEqual(decode_edge_evidence(ev), ev)

    def test_square_bracket_text_that_is_not_json_kept(self):
        ev = "[kernel] not json at all"
        self.assertEqual(decode_edge_evidence(ev), ev)

    def test_scalar_json_kept_as_string(self):
        # "123" parses as JSON but only list/dict round-trip; a plain
        # string that happens to be valid scalar JSON must stay a string.
        self.assertEqual(decode_edge_evidence("123"), "123")
        self.assertEqual(decode_edge_evidence("null"), "null")

    def test_json_array_parses_to_list(self):
        self.assertEqual(decode_edge_evidence('["a", "b"]'), ["a", "b"])

    def test_json_object_parses_to_dict(self):
        self.assertEqual(decode_edge_evidence('{"k": 1}'), {"k": 1})

    def test_non_string_passthrough(self):
        self.assertEqual(decode_edge_evidence(["x"]), ["x"])


class _GraphDirMixin:
    """Build a db with one plain-string edge and one JSON-array edge."""

    def _make_db(self, d):
        db = os.path.join(d, "code2database.db")
        with SQLiteStore(db) as st:
            st.store_functions([
                {"id": "f1", "name": "f1", "domain": "root",
                 "source_file": "a.c", "line": 1},
                {"id": "f2", "name": "f2", "domain": "root",
                 "source_file": "a.c", "line": 2},
            ])
            st.store_edges([
                {"invoker": "f1", "invoked": "f2",
                 "evidence": "vtable_dispatch: ops.pool.run=handler"},
                {"invoker": "f2", "invoked": "f1",
                 "evidence": ["multi", "site"]},
            ])
        return db


class TestLazySQLiteGraphEvidence(_GraphDirMixin, unittest.TestCase):
    """LazySQLiteGraph read paths preserve the stored form."""

    def test_get_edge_data(self):
        with tempfile.TemporaryDirectory() as d:
            g = LazySQLiteGraph(self._make_db(d))
            try:
                self.assertEqual(
                    g.get_edge_data("f1", "f2")["evidence"],
                    "vtable_dispatch: ops.pool.run=handler")
                self.assertEqual(
                    g.get_edge_data("f2", "f1")["evidence"],
                    ["multi", "site"])
            finally:
                g.close()

    def test_edges_data_iteration(self):
        with tempfile.TemporaryDirectory() as d:
            g = LazySQLiteGraph(self._make_db(d))
            try:
                ev = {(u, v): e["evidence"] for u, v, e in g.edges(data=True)}
                self.assertEqual(
                    ev[("f1", "f2")], "vtable_dispatch: ops.pool.run=handler")
                self.assertEqual(ev[("f2", "f1")], ["multi", "site"])
            finally:
                g.close()

    def test_in_and_out_edges(self):
        with tempfile.TemporaryDirectory() as d:
            g = LazySQLiteGraph(self._make_db(d))
            try:
                oe = list(g.out_edges("f1", data=True))
                self.assertEqual(len(oe), 1)
                self.assertEqual(
                    oe[0][2]["evidence"], "vtable_dispatch: ops.pool.run=handler")
                ie = list(g.in_edges("f1", data=True))
                self.assertEqual(len(ie), 1)
                self.assertEqual(ie[0][2]["evidence"], ["multi", "site"])
            finally:
                g.close()

    def test_split_by_domain_writes_string_evidence_to_master(self):
        """End-to-end downstream scenario: the master JSON built from a
        lazy graph must carry the original evidence text (not "[]"), and
        validate_struct_embeddings must be able to extract struct types
        from it."""
        from _builder.graph.domain_split import split_by_domain

        with tempfile.TemporaryDirectory() as d:
            db = self._make_db(d)
            # master.json + db + >=50K function count -> lazy load
            master = os.path.join(d, "code2database_master.json")
            with open(master, "w", encoding="utf-8") as f:
                json.dump({"stats": {"total_functions": 60000},
                           "source_root": d}, f)
            from _builder.graph.graph_build import _load_full_graph
            G = _load_full_graph(d)
            try:
                self.assertEqual(type(G).__name__, "LazySQLiteGraph")
                split_by_domain(G, d, d)
            finally:
                G.close()
            # Rebuild master from the split (split_by_domain writes the
            # domain files; the edge evidence must survive as text).
            found = []
            for root, _dirs, files in os.walk(d):
                for fn in files:
                    if fn.startswith("code2database_domain_"):
                        with open(os.path.join(root, fn), encoding="utf-8") as f:
                            data = json.load(f)
                        for e in data.get("edges", []):
                            extras = e[8] if len(e) > 8 else {}
                            if extras.get("ev"):
                                found.append(extras["ev"])
            self.assertIn("vtable_dispatch: ops.pool.run=handler", found)

            # And the validator can consume it without a TypeError.
            from _builder.ops.validate import validate_struct_embeddings
            from _builder.ops.validate import ValidationResult
            master_data = json.load(open(master, encoding="utf-8"))
            cross = master_data.get("cross_domain_edges", [])
            result = ValidationResult()
            # Feed an edge record carrying string evidence through the
            # same shape validate reads from master JSON.
            rec = {"evidence": "vtable_dispatch: ops.pool.run=handler"}
            validate_struct_embeddings(
                {"cross_domain_edges": [rec]}, result)
            # No exception and no crash is the contract here.


class TestEagerSQLiteLoaderEvidence(_GraphDirMixin, unittest.TestCase):
    """_load_full_graph_from_sqlite / _load_neighbors_from_sqlite keep
    plain-text evidence as strings too."""

    def test_full_load(self):
        from _builder.graph.graph_loader import _load_full_graph_from_sqlite
        with tempfile.TemporaryDirectory() as d:
            G = _load_full_graph_from_sqlite(self._make_db(d))
            self.assertEqual(
                G["f1"]["f2"]["evidence"],
                "vtable_dispatch: ops.pool.run=handler")
            self.assertEqual(G["f2"]["f1"]["evidence"], ["multi", "site"])

    def test_neighbors_load(self):
        from _builder.graph.graph_loader import _load_neighbors_from_sqlite
        with tempfile.TemporaryDirectory() as d:
            preds, succs = _load_neighbors_from_sqlite(
                self._make_db(d), "f1")
            self.assertEqual(
                succs[0][1]["evidence"],
                "vtable_dispatch: ops.pool.run=handler")
            self.assertEqual(preds[0][1]["evidence"], ["multi", "site"])


class TestDomainSplitEvidenceCoercion(unittest.TestCase):
    """split_by_domain serializes list evidence to a JSON string so the
    master JSON stays string-typed (downstream defensive layer)."""

    def test_list_evidence_serialized_as_string(self):
        from _builder.graph.domain_split import split_by_domain
        import networkx as nx
        with tempfile.TemporaryDirectory() as d:
            G = nx.DiGraph()
            G.add_node("f1", name="f1", domain="root", source_file="a.c",
                       line=1, labels=[])
            G.add_node("f2", name="f2", domain="root", source_file="a.c",
                       line=2, labels=[])
            G.add_edge("f1", "f2", evidence=["multi", "site"])
            split_by_domain(G, d, "")
            for root, _dirs, files in os.walk(d):
                for fn in files:
                    if fn.startswith("code2database_domain_"):
                        data = json.load(
                            open(os.path.join(root, fn), encoding="utf-8"))
                        for e in data.get("edges", []):
                            extras = e[8] if len(e) > 8 else {}
                            if extras.get("ev"):
                                self.assertIsInstance(extras["ev"], str)


class TestValidateEvidenceCoercion(unittest.TestCase):
    """validate_struct_embeddings tolerates non-string evidence."""

    def test_list_evidence_does_not_crash(self):
        from _builder.ops.validate import (validate_struct_embeddings,
                                           ValidationResult)
        master = {"cross_domain_edges": [
            {"evidence": ["vtable_dispatch: Foo.bar"]},
            {"evidence": {"k": "field_dispatch: Baz.qux"}},
            {"evidence": 42},
        ]}
        result = ValidationResult()
        validate_struct_embeddings(master, result)
        # No TypeError raised is the contract.


if __name__ == "__main__":
    unittest.main()
