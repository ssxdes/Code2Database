"""Unit tests for validate.py — post-build output validation.

Covers ValidationResult accumulation (error/warn/info, ok, summary),
validate_edge_logic (missing concurrency warn, cross-domain structural
relation error, structural missing relation, OPS_BIND exemption, clean
pass), validate_call_chain_accuracy (duplicate edge error, unjustified
self-loop warn, callback-justified self-loop, evidence aggregation,
AMBIGUOUS as info), validate_data_consistency (endpoint count mismatch
error, edge count mismatch warn), validate_all on a fixture graph dir
(all checks run, io error on empty dir), cmd_validate exit codes.
"""
import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.ops.validate import (
    ValidationResult, validate_edge_logic, validate_call_chain_accuracy,
    validate_data_consistency, validate_all, cmd_validate,
)


def _ns(**kw):
    return argparse.Namespace(**kw)


def _capture_call(fn, args):
    out, err = io.StringIO(), io.StringIO()
    ret, code = None, None
    with redirect_stdout(out), redirect_stderr(err):
        try:
            ret = fn(args)
        except SystemExit as e:
            code = e.code
    return ret, out.getvalue(), err.getvalue(), code


class TestValidationResult(unittest.TestCase):
    def test_accumulation_and_ok(self):
        r = ValidationResult()
        self.assertTrue(r.ok)
        r.error("cat", "boom")
        self.assertFalse(r.ok)
        r.warn("cat", "hmm")
        r.add_info("cat", "fyi")
        self.assertEqual(len(r.errors), 1)
        self.assertEqual(len(r.warnings), 1)
        self.assertEqual(len(r.infos), 1)

    def test_summary_lists_categories(self):
        r = ValidationResult()
        r.error("edge_logic", "E1", "detail-E1")
        r.warn("call_chain", "W1")
        text = r.summary()
        self.assertIn("ERRORS (1)", text)
        self.assertIn("[edge_logic] E1", text)
        self.assertIn("detail-E1", text)
        self.assertIn("WARNINGS (1)", text)
        self.assertIn("[call_chain] W1", text)


class TestValidateEdgeLogic(unittest.TestCase):
    def test_clean_master_passes(self):
        master = {
            "cross_domain_edges": [
                {"source": "a", "target": "b", "concurrency": "sync"},
            ],
            "structural_edges": [
                {"source": "f.c", "target": "a", "relation": "CONTAINS"},
            ],
        }
        r = ValidationResult()
        validate_edge_logic(master, r)
        self.assertTrue(r.ok)
        self.assertEqual(r.warnings, [])

    def test_missing_concurrency_aggregates_to_warn(self):
        master = {
            "cross_domain_edges": [
                {"source": f"a{i}", "target": f"b{i}"} for i in range(7)
            ],
            "structural_edges": [],
        }
        r = ValidationResult()
        validate_edge_logic(master, r)
        self.assertTrue(r.ok)  # warn, not error
        self.assertEqual(len(r.warnings), 1)
        self.assertIn("missing 'concurrency'", r.warnings[0]["message"])

    def test_cross_domain_structural_relation_is_error(self):
        master = {
            "cross_domain_edges": [
                {"source": "a", "target": "b", "concurrency": "sync",
                 "relation": "CONTAINS"},
            ],
            "structural_edges": [],
        }
        r = ValidationResult()
        validate_edge_logic(master, r)
        self.assertFalse(r.ok)
        self.assertIn("structural relation", r.errors[0]["message"])

    def test_structural_missing_relation_is_error(self):
        master = {
            "cross_domain_edges": [],
            "structural_edges": [{"source": "f.c", "target": "a"}],
        }
        r = ValidationResult()
        validate_edge_logic(master, r)
        self.assertFalse(r.ok)

    def test_ops_bind_exempt_from_concurrency_requirement(self):
        master = {
            "cross_domain_edges": [
                {"source": "vt", "target": "impl", "relation": "OPS_BIND"},
            ],
            "structural_edges": [],
        }
        r = ValidationResult()
        validate_edge_logic(master, r)
        self.assertEqual(r.warnings, [])
        self.assertTrue(r.ok)


class TestValidateCallChainAccuracy(unittest.TestCase):
    def test_duplicate_edges_reported(self):
        edge = {"source": "a", "target": "b", "concurrency": "sync",
                "evidence": "call site"}
        master = {"cross_domain_edges": [dict(edge), dict(edge)],
                  "structural_edges": []}
        r = ValidationResult()
        validate_call_chain_accuracy(master, r)
        self.assertFalse(r.ok)
        self.assertIn("Duplicate edge", r.errors[0]["message"])

    def test_unjustified_self_loop_warns(self):
        master = {
            "cross_domain_edges": [
                {"source": "f", "target": "f", "concurrency": "sync",
                 "evidence": "x", "call_condition": ""},
            ],
            "structural_edges": [],
        }
        r = ValidationResult()
        validate_call_chain_accuracy(master, r)
        self.assertTrue(any("Self-loop" in w["message"] for w in r.warnings))

    def test_callback_justified_self_loop_no_warn(self):
        master = {
            "cross_domain_edges": [
                {"source": "f", "target": "f", "concurrency": "sync",
                 "evidence": "x", "call_condition": "callback self-reschedule"},
            ],
            "structural_edges": [],
        }
        r = ValidationResult()
        validate_call_chain_accuracy(master, r)
        self.assertFalse(any("Self-loop" in w["message"] for w in r.warnings))

    def test_missing_evidence_aggregates_to_warn(self):
        master = {
            "cross_domain_edges": [
                {"source": "a", "target": "b", "concurrency": "sync"}
                for _ in range(3)
            ],
            "structural_edges": [],
        }
        r = ValidationResult()
        validate_call_chain_accuracy(master, r)
        self.assertTrue(any("without evidence" in w["message"]
                            for w in r.warnings))

    def test_ambiguous_edges_reported_as_info(self):
        master = {
            "cross_domain_edges": [
                {"source": "a", "target": "b", "concurrency": "fn_ptr",
                 "confidence": "AMBIGUOUS", "evidence": "x"},
            ],
            "structural_edges": [],
        }
        r = ValidationResult()
        validate_call_chain_accuracy(master, r)
        self.assertTrue(r.ok)
        self.assertTrue(any("AMBIGUOUS" in i["message"] for i in r.infos))

    def test_edge_type_counts_surfaced_in_info(self):
        master = {
            "cross_domain_edges": [],
            "structural_edges": [],
            "edge_type_counts": {"concurrency:fn_ptr": 4,
                                 "concurrency:vtable_dispatch": 2},
        }
        r = ValidationResult()
        validate_call_chain_accuracy(master, r)
        joined = " ".join(i["message"] for i in r.infos)
        self.assertIn("fn_ptr=4", joined)
        self.assertIn("vtable_dispatch=2", joined)


class TestValidateDataConsistency(unittest.TestCase):
    def test_endpoint_count_mismatch_is_error(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".code2database_endpoints.json"), "w") as f:
                json.dump({"total_endpoints": 5, "endpoints": [
                    {"name": f"e{i}"} for i in range(3)]}, f)
            master = {"cross_domain_edges": [], "structural_edges": [],
                      "total_edges": 0}
            r = ValidationResult()
            validate_data_consistency(master, r, d)
            self.assertFalse(r.ok)
            self.assertIn("Endpoint count mismatch", r.errors[0]["message"])

    def test_edge_count_mismatch_warns(self):
        with tempfile.TemporaryDirectory() as d:
            master = {"cross_domain_edges": [
                          {"source": "a", "target": "b", "concurrency": "s"}],
                      "structural_edges": [
                          {"source": "f", "target": "a", "relation": "CONTAINS"}],
                      "total_edges": 0}  # less than cross+struct → negative
            r = ValidationResult()
            validate_data_consistency(master, r, d)
            self.assertTrue(any("Edge count mismatch" in w["message"]
                                for w in r.warnings))

    def test_consistent_counts_pass(self):
        with tempfile.TemporaryDirectory() as d:
            master = {"cross_domain_edges": [
                          {"source": "a", "target": "b", "concurrency": "s"}],
                      "structural_edges": [],
                      "total_edges": 5}  # 1 cross + 4 intra
            r = ValidationResult()
            validate_data_consistency(master, r, d)
            self.assertFalse(any("mismatch" in e["message"].lower()
                                 for e in r.errors))


class TestValidateAllAndCmd(unittest.TestCase):
    def _make_outdir(self):
        tmp = tempfile.mkdtemp(prefix="c2d_validate_")
        nodes = [{"id": "api", "name": "api", "labels": ["API_entry"],
                  "source_file": "/tmp/x.c", "line": 1, "domain": "test",
                  "is_empty": False},
                 {"id": "leaf", "name": "leaf", "labels": ["out_end"],
                  "source_file": "/tmp/x.c", "line": 2, "domain": "test",
                  "is_empty": False}]
        edges = [{"source": "api", "target": "leaf", "concurrency": "sync",
                  "confidence": "EXTRACTED", "evidence": "call site"}]
        with open(os.path.join(tmp, "domain_test.json"), "w") as f:
            json.dump({"nodes": nodes, "edges": edges}, f)
        master = {"source_root": "/tmp", "domains": {"test": "domain_test.json"},
                  "cross_domain_edges": [], "structural_edges": [],
                  "total_edges": 1, "total_nodes": 2,
                  "stats": {"total_functions": 2}}
        with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
            json.dump(master, f)
        return tmp

    def test_validate_all_runs_all_checks(self):
        d = self._make_outdir()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        result = validate_all(d)
        self.assertIsInstance(result, ValidationResult)
        # structural invariants of the run itself
        self.assertTrue(result.infos or result.warnings or result.errors)

    def test_validate_all_missing_files_reports_io_error(self):
        empty = tempfile.mkdtemp(prefix="c2d_validate_none_")
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        result = validate_all(empty)
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0]["category"], "io")

    def test_cmd_validate_exit_code_reflects_ok(self):
        d = self._make_outdir()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # introduce a must-fix error: duplicate cross-domain edge
        master_path = os.path.join(d, "code2database_master.json")
        master = json.loads(open(master_path).read())
        edge = {"source": "x", "target": "y", "concurrency": "s",
                "evidence": "e"}
        master["cross_domain_edges"] = [dict(edge), dict(edge)]
        master["total_edges"] = 3
        open(master_path, "w").write(json.dumps(master))

        ret, out, err, code = _capture_call(
            cmd_validate, _ns(outdir=d, profile=None))
        self.assertEqual(code, 1)
        self.assertIn("Duplicate edge", out)

    def test_cmd_validate_clean_fixture_exits_zero(self):
        d = self._make_outdir()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        ret, out, err, code = _capture_call(
            cmd_validate, _ns(outdir=d, profile=None))
        # exit 0 (None from sys.exit(0) is normalized to None/0)
        self.assertIn(code, (None, 0))
        self.assertNotIn("ERRORS", out)


class TestSqliteFallbackMaster(unittest.TestCase):
    """The SQLite fallback must synthesize a master the checks can read.

    Validators consume domains[].functions, cross_domain_edges and
    structural_edges. A fallback that stores rows under other keys lets
    every check silently pass on SQLite-only builds.
    """

    def _make_sqlite_outdir(self, duplicate_edge=False):
        import sqlite3
        from _builder.graph.sqlite_store import SQLiteStore
        d = tempfile.mkdtemp(prefix="c2d_validate_sqlite_")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        db = os.path.join(d, "code2database.db")
        with SQLiteStore(db) as store:
            store.store_functions([
                {"id": "dom::f1", "name": "f1", "domain": "dom",
                 "source_file": "a.c", "line_number": 10,
                 "signature": "int f1()", "labels": ["API_entry"]},
                {"id": "dom::f2", "name": "f2", "domain": "dom",
                 "source_file": "a.c", "line_number": 20,
                 "signature": "void f2()", "labels": []},
            ])
            edge = {"invoker": "dom::f1", "invoked": "dom::f2",
                    "relation": "INVOKES", "concurrency": "direct_call",
                    "confidence": "EXTRACTED", "evidence": "direct call"}
            edges = [dict(edge)]
            if duplicate_edge:
                edges.append(dict(edge))
            store.store_edges(edges)
        return d

    def test_fallback_loads_functions_and_edges(self):
        from _builder.ops.validate import _load_master_from_sqlite
        d = self._make_sqlite_outdir()
        master = _load_master_from_sqlite(os.path.join(d, "code2database.db"),
                                          d)
        self.assertIsNotNone(master)
        funcs = master["domains"]["all"]["functions"]
        self.assertEqual(len(funcs), 2)
        by_id = {f["id"]: f for f in funcs}
        self.assertEqual(by_id["dom::f1"]["labels"], ["API_entry"])
        self.assertEqual(by_id["dom::f1"]["line"], 10)
        self.assertEqual(len(master["cross_domain_edges"]), 1)
        self.assertEqual(master["cross_domain_edges"][0]["source"],
                         "dom::f1")
        self.assertEqual(master["cross_domain_edges"][0]["target"],
                         "dom::f2")
        self.assertEqual(master["structural_edges"], [])

    def test_validate_all_on_sqlite_dir_runs_checks(self):
        d = self._make_sqlite_outdir()
        result = validate_all(d)
        # The fallback loaded real data — the run must report something
        # derived from it, not an io error and not an empty pass.
        self.assertFalse(
            any(e["category"] == "io" for e in result.errors),
            f"SQLite fallback failed to load: {result.errors}")
        self.assertTrue(result.infos or result.warnings or result.errors,
                        "all checks were silently no-op on SQLite fallback")

    def test_validate_all_on_sqlite_dir_detects_duplicates(self):
        d = self._make_sqlite_outdir(duplicate_edge=True)
        result = validate_all(d)
        dupes = [e for e in result.errors
                 if "Duplicate edge" in e.get("message", "")]
        self.assertTrue(dupes,
                        "duplicate edge in SQLite graph was not detected; "
                        f"result: {result.errors + result.warnings}")


class TestValidateSemanticMatching(unittest.TestCase):
    """main() test-path warnings must follow the classifier's segment set:
    generic defaults plus profile-declared test_domain_segments."""

    def _make_outdir_with_main(self, src, labels):
        tmp = tempfile.mkdtemp(prefix="c2d_validate_sem_")
        func = {"id": "app.main", "name": "main", "source_file": src,
                "line": 1, "labels": labels, "is_empty": False}
        with open(os.path.join(tmp, "domain_app.json"), "w") as f:
            json.dump({"nodes": [func], "edges": [], "functions": [func],
                       "function_details": {}}, f)
        master = {"source_root": "/tmp", "domains": {"app": "domain_app.json"},
                  "cross_domain_edges": [], "structural_edges": [],
                  "total_edges": 0, "total_nodes": 1,
                  "stats": {"total_functions": 1}}
        with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
            json.dump(master, f)
        return tmp

    def _warnings(self, src, labels, profile=None):
        from _builder.ops.validate import validate_semantic_matching
        d = self._make_outdir_with_main(src, labels)
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        master = {"domains": {"app": "domain_app.json"}}
        r = ValidationResult()
        validate_semantic_matching(master, r, outdir=d, profile=profile)
        return [w["message"] for w in r.warnings]

    def test_profile_segment_main_with_entry_point_label_warns(self):
        warns = self._warnings(
            "ztest/foo.c", ["entry_point"],
            profile={"project_boundaries": {"test_domain_segments": ["ztest"]}})
        self.assertTrue(any("should be test_entry" in m for m in warns), warns)

    def test_profile_segment_absent_no_warning(self):
        """Without the profile declaration the same path is production —
        an entry_point label there is correct and must not warn."""
        warns = self._warnings("ztest/foo.c", ["entry_point"], profile=None)
        self.assertFalse(any("should be test_entry" in m for m in warns), warns)

    def test_generic_doc_segment_warns(self):
        """Generic segments the classifier honors (doc/, tools/, scripts/)
        must also trigger the validator — the two sets must stay in sync."""
        for seg in ("doc", "tools", "scripts", "documentation"):
            warns = self._warnings(f"{seg}/foo.c", ["entry_point"])
            self.assertTrue(any("should be test_entry" in m for m in warns),
                            f"segment {seg!r} did not warn: {warns}")

    def test_test_entry_label_does_not_warn(self):
        warns = self._warnings(
            "ztest/foo.c", ["test_entry"],
            profile={"project_boundaries": {"test_domain_segments": ["ztest"]}})
        self.assertFalse(any("should be test_entry" in m for m in warns), warns)


if __name__ == "__main__":
    unittest.main()
