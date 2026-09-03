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

from _builder.validate import (
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


if __name__ == "__main__":
    unittest.main()
