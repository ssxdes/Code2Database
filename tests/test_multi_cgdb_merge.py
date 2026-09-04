"""Real-scenario tests: build-multi must merge cgdb_* AST data.

Regression guard for a bug where multi-project builds silently produced
EMPTY cgdb tables (0 rows in cgdb_nodes/cgdb_edges for a real libstorage
build, vs 652,919/641,631 rows for the same code scanned as a single
project): scan_directory() returns 15 cgdb_* list keys (the AST 13-layer
records the builder writes into the cgdb schema tables), but
build_multi()'s joint_extraction only initialized 5 legacy keys
(functions/edges/globals/vtables/imports) and its merge loops only
copied those — every cgdb_* key was dropped on the floor, so the
downstream cmd_build saw no cgdb data and skipped the cgdb layer
entirely (graph_build gates on `if cgdb_nodes_data or cgdb_types_data
or cgdb_edges_data:`).
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


def _fake_scan_output(project_name, n):
    """Scan output shaped like scan_directory()'s real return value.

    Includes the 5 legacy keys plus a representative slice of the
    cgdb_* keys the scanner actually emits (see scan_directory's return
    dict: cgdb_nodes/types/edges/invoke_sites/predicates/ops_bindings/
    basic_blocks/cfg_edges/data_flow/sync_primitives/happens_before/
    alias_sets/doc_comments/metadata/includes + conditions).
    """
    return {
        "source_root": "/fake/%s" % project_name,
        "domains": ["lib"],
        "lang_stats": {"c": n},
        "functions": [
            {"id": "%s_lib_f%d" % (project_name, i),
             "name": "f%d" % i, "domain": "lib",
             "source_file": "f%d.c" % i, "line": i + 1, "labels": []}
            for i in range(n)
        ],
        "edges": [
            {"source": "%s_lib_f%d" % (project_name, i),
             "target": "%s_lib_f%d" % (project_name, i + 1),
             "call_order": 1, "call_condition": "", "concurrency": "",
             "confidence": "EXTRACTED", "source_tag": "ast",
             "confidence_score": 1.0}
            for i in range(n - 1)
        ],
        "globals": {"global_vars": ["g_%s" % project_name]},
        "vtables": [{"struct": "ops_%s" % project_name}],
        "imports": ["import_%s" % project_name],
        "cgdb_nodes": [
            {"usr": "c%d@%s" % (i, project_name), "kind": "function",
             "name": "f%d" % i, "file_path": "/fake/%s/f%d.c"
             % (project_name, i)}
            for i in range(n)
        ],
        "cgdb_types": [{"usr": "t@%s" % project_name, "kind": "struct"}],
        "cgdb_edges": [
            {"src_usr": "c%d@%s" % (i, project_name),
             "dst_usr": "c%d@%s" % (i + 1, project_name), "kind": "CALL"}
            for i in range(n - 1)
        ],
        "cgdb_conditions": [{"text": "defined(CONFIG_%s)" % project_name}],
        "cgdb_sync_primitives": [{"name": "mutex_%s" % project_name}],
    }


class TestMergeProjectData(unittest.TestCase):
    """Unit: the merge helper must copy legacy keys AND every cgdb_* key."""

    def test_merges_legacy_and_cgdb_keys(self):
        from _builder.build_multi import _merge_project_data
        joint = {"functions": [], "edges": [], "globals": {},
                 "vtables": [], "imports": []}
        data = _fake_scan_output("projA", 2)
        _merge_project_data(joint, data, "projA")
        self.assertEqual(len(joint["functions"]), 2)
        self.assertEqual(len(joint["edges"]), 1)
        self.assertEqual(joint["globals"], {"projA.global_vars": ["g_projA"]})
        self.assertEqual(joint["vtables"], [{"struct": "ops_projA"}])
        self.assertEqual(joint["imports"], ["import_projA"])
        # cgdb_* keys must all be merged, not just the big three
        self.assertEqual(len(joint["cgdb_nodes"]), 2)
        self.assertEqual(len(joint["cgdb_types"]), 1)
        self.assertEqual(len(joint["cgdb_edges"]), 1)
        self.assertEqual(joint["cgdb_conditions"],
                         [{"text": "defined(CONFIG_projA)"}])
        self.assertEqual(joint["cgdb_sync_primitives"],
                         [{"name": "mutex_projA"}])

    def test_second_project_appends_not_replaces(self):
        from _builder.build_multi import _merge_project_data
        joint = {"functions": [], "edges": [], "globals": {},
                 "vtables": [], "imports": []}
        _merge_project_data(joint, _fake_scan_output("projA", 2), "projA")
        _merge_project_data(joint, _fake_scan_output("projB", 3), "projB")
        self.assertEqual(len(joint["functions"]), 5)
        self.assertEqual(len(joint["cgdb_nodes"]), 5)
        self.assertEqual(len(joint["cgdb_edges"]), 3)
        # globals keys are namespaced per project — no clobbering
        self.assertIn("projA.global_vars", joint["globals"])
        self.assertIn("projB.global_vars", joint["globals"])

    def test_project_without_cgdb_keys_is_fine(self):
        from _builder.build_multi import _merge_project_data
        joint = {"functions": [], "edges": [], "globals": {},
                 "vtables": [], "imports": []}
        data = _fake_scan_output("plain", 1)
        for k in list(data):
            if k.startswith("cgdb_"):
                del data[k]
        _merge_project_data(joint, data, "plain")
        self.assertEqual(len(joint["functions"]), 1)
        self.assertNotIn("cgdb_nodes", joint)


class TestBuildMultiCgdbMerge(unittest.TestCase):
    """Integration: joint_extraction.json (what cmd_build consumes) must
    carry the merged cgdb_* data from every scanned project."""

    def _run_build_multi(self, capture):
        import _builder.build_multi as bm
        with tempfile.TemporaryDirectory() as tmp:
            src_a = os.path.join(tmp, "a"); os.makedirs(src_a)
            src_b = os.path.join(tmp, "b"); os.makedirs(src_b)
            outdir = os.path.join(tmp, "out")
            manifest = {
                "version": 1, "output": outdir,
                "projects": [
                    {"name": "projA", "source": src_a},
                    {"name": "projB", "source": src_b,
                     "depends_on": ["projA"]},
                ],
            }
            mpath = os.path.join(tmp, "manifest.json")
            with open(mpath, "w") as f:
                json.dump(manifest, f)

            def fake_scan_directory(**kwargs):
                root = kwargs.get("source_root", "")
                name = "projA" if root.endswith("a") else "projB"
                return _fake_scan_output(name, 2)

            def fake_cmd_build(args):
                with open(args.extraction) as f:
                    capture.update(json.load(f))

            with mock.patch("code2database_scanner.scan_directory",
                            side_effect=fake_scan_directory), \
                 mock.patch("_builder.graph_build.cmd_build",
                            side_effect=fake_cmd_build):
                summary = bm.build_multi(mpath, outdir, verbose=False)
            return summary

    def test_joint_extraction_contains_merged_cgdb_data(self):
        capture = {}
        summary = self._run_build_multi(capture)
        errors = [p for p in summary["projects"] if p.get("error")]
        self.assertFalse(errors, "scan errors: %r" % errors)
        self.assertEqual(len(capture.get("cgdb_nodes", [])), 4,
                         "cgdb_nodes from both projects must survive the "
                         "merge: %r" % capture.get("cgdb_nodes"))
        self.assertEqual(len(capture.get("cgdb_edges", [])), 2)
        self.assertEqual(len(capture.get("functions", [])), 4)
        names = {n["name"] for n in capture["cgdb_nodes"]}
        self.assertEqual(names, {"f0", "f1"})
        self.assertEqual(capture.get("cgdb_conditions"),
                         [{"text": "defined(CONFIG_projA)"},
                          {"text": "defined(CONFIG_projB)"}])
        self.assertIn("projA.global_vars",
                      capture.get("globals", {}))
        self.assertIn("projB.global_vars", capture.get("globals", {}))


if __name__ == "__main__":
    unittest.main()
