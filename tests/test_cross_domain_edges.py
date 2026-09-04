"""Real-scenario tests: cross-domain INVOKES edges must survive the split.

Regression guard for a bug where 70% of a real project's call edges
(67,857 of 97,277 in a multi-project libstorage build) were silently
dropped: split_by_domain() filtered cross-domain edges with
``'.' not in v``, but real scanner node IDs NEVER contain dots
(_make_func_id builds them as domain.replace('.', '_') + '_' + name),
so every cross-domain call edge was discarded. Downstream, the Web UI
loads domain JSONs + master cross_domain_edges — with them dropped, a
node like spdk_nvme_qpair_process_completions rendered as an isolated
node with no call edges.

The pre-existing unit test (test_builder_helpers.TestSplitByDomain)
hand-crafted DOTTED ids ("lib.bdev.bdev_start") that real scanners
never emit, which is why the bug survived a 2200-test green suite.
These tests pin the REAL id format (underscore form) and additionally
drive a real scanner+builder+load round trip on a two-library project
that calls across directories.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
SCANNER = os.path.join(SCRIPTS_DIR, "code2database_scanner.py")
BUILDER = os.path.join(SCRIPTS_DIR, "code2database_builder.py")

# MemoryGuard reads SYSTEM memory (not project size), so on a busy
# machine it can cancel even a 4-file scan: --memory-limit 0 (the
# default) auto-derives a GB cap from total RAM (total * 0.8), and
# percentage thresholds can fire too. Pin both to keep tests hermetic.
# (The builder has no --memory-limit flag; only percentage thresholds.)
_SCAN_MEM_FLAGS = ["--memory-limit", "9999",
                   "--memory-warn-threshold", "0.99",
                   "--memory-crit-threshold", "0.999"]
_BUILD_MEM_FLAGS = ["--memory-warn-threshold", "0.99",
                    "--memory-crit-threshold", "0.999"]

_A_C = """#include "a.h"
#include "../libb/b.h"
int a_helper(int x) { return x + 1; }
int a_public(int x) { return b_public(a_helper(x)); }
"""
_A_H = "int a_public(int x);\n"
_B_C = """#include "b.h"
#include "../liba/a.h"
int b_public(int x) { return x * 2; }
int b_entry(int x) { return a_public(x) + b_public(x); }
"""
_B_H = "int b_public(int x);\nint b_entry(int x);\n"


def _write(proj_root, rel, text):
    path = os.path.join(proj_root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class TestSplitByDomainRealIDs(unittest.TestCase):
    """split_by_domain must keep cross-domain edges for real (underscore) ids.

    Node ids come from _make_func_id: domain.replace('.', '_') + '_' + name.
    """

    def _build(self):
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "lib_a_a_pub", "name": "a_pub",
                 "source_file": "lib/a/a.c", "line": 10,
                 "domain": "lib.a", "labels": ["API_entry"]},
                {"id": "lib_a_a_helper", "name": "a_helper",
                 "source_file": "lib/a/a.c", "line": 9,
                 "domain": "lib.a", "labels": []},
                {"id": "lib_b_b_pub", "name": "b_pub",
                 "source_file": "lib/b/b.c", "line": 9,
                 "domain": "lib.b", "labels": []},
                {"id": "lib_b_b_entry", "name": "b_entry",
                 "source_file": "lib/b/b.c", "line": 10,
                 "domain": "lib.b", "labels": []},
            ],
            "edges": [
                {"source": "lib_a_a_pub", "target": "lib_a_a_helper",
                 "call_order": 1, "call_condition": "", "concurrency": "",
                 "confidence": "EXTRACTED", "source_tag": "ast",
                 "confidence_score": 1.0},
                {"source": "lib_b_b_entry", "target": "lib_b_b_pub",
                 "call_order": 1, "call_condition": "", "concurrency": "",
                 "confidence": "EXTRACTED", "source_tag": "ast",
                 "confidence_score": 1.0},
                # The two cross-domain calls — the user-visible scenario
                {"source": "lib_a_a_pub", "target": "lib_b_b_pub",
                 "call_order": 2, "call_condition": "", "concurrency": "",
                 "confidence": "EXTRACTED", "source_tag": "ast",
                 "confidence_score": 1.0},
                {"source": "lib_b_b_entry", "target": "lib_a_a_pub",
                 "call_order": 2, "call_condition": "", "concurrency": "",
                 "confidence": "EXTRACTED", "source_tag": "ast",
                 "confidence_score": 1.0},
            ],
            "domains": ["lib.a", "lib.b"],
            "lang_stats": {"c": 2},
        }
        return build_graph(extraction)

    def test_cross_domain_edges_kept_in_master(self):
        from _builder.graph_build import split_by_domain
        G, _ = self._build()
        with tempfile.TemporaryDirectory() as tmpdir:
            master_path = split_by_domain(G, tmpdir, source_root="/src")
            with open(master_path) as f:
                master = json.load(f)
            cross = master.get("cross_domain_edges", [])
            pairs = {(e.get("source"), e.get("target")) for e in cross}
            self.assertIn(("lib_a_a_pub", "lib_b_b_pub"), pairs,
                          "cross-domain call a_pub -> b_pub missing from "
                          "master cross_domain_edges: %r" % pairs)
            self.assertIn(("lib_b_b_entry", "lib_a_a_pub"), pairs,
                          "cross-domain call b_entry -> a_pub missing from "
                          "master cross_domain_edges: %r" % pairs)

    def test_domain_files_keep_only_intra_domain_calls(self):
        from _builder.graph_build import split_by_domain
        G, _ = self._build()
        with tempfile.TemporaryDirectory() as tmpdir:
            master_path = split_by_domain(G, tmpdir, source_root="/src")
            with open(master_path) as f:
                master = json.load(f)
            for domain, rel in master["domains"].items():
                with open(os.path.join(tmpdir, rel)) as f:
                    data = json.load(f)
                fields = data.get("edge_fields",
                                  ["source", "target", "call_order",
                                   "call_condition", "concurrency",
                                   "confidence", "source_tag",
                                   "confidence_score"])
                # Node ids are domain.replace('.', '_') + '_' + name, so a
                # same-domain edge must have both ids prefixed with the
                # domain's underscore form.
                prefix = domain.replace(".", "_") + "_"
                for row in data.get("edges", []):
                    src = row[fields.index("source")]
                    tgt = row[fields.index("target")]
                    if src.startswith("file:"):
                        # CONTAINS edges (file -> its functions) are
                        # legitimately intra-domain.
                        continue
                    self.assertTrue(
                        src.startswith(prefix) and tgt.startswith(prefix),
                        "cross-domain edge %s -> %s leaked into domain "
                        "file for %s" % (src, tgt, domain))
            # Both intra-domain calls must still be in their domain files
            all_pairs = set()
            for rel in master["domains"].values():
                with open(os.path.join(tmpdir, rel)) as f:
                    data = json.load(f)
                fields = data.get("edge_fields",
                                  ["source", "target", "call_order",
                                   "call_condition", "concurrency",
                                   "confidence", "source_tag",
                                   "confidence_score"])
                for row in data.get("edges", []):
                    all_pairs.add((row[fields.index("source")],
                                   row[fields.index("target")]))
            self.assertIn(("lib_a_a_pub", "lib_a_a_helper"), all_pairs)
            self.assertIn(("lib_b_b_entry", "lib_b_b_pub"), all_pairs)


class TestCrossDomainEndToEnd(unittest.TestCase):
    """Real scan -> build -> load round trip on a two-library project.

    a_public (liba) calls b_public (libb) and b_entry (libb) calls
    a_public (liba): both calls cross domains, so they must appear in
    master cross_domain_edges, and load() must reconstruct them so
    neighbor queries can walk across domains (what the Web UI does).
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = cls._tmp.name
        cls.source = os.path.join(root, "proj")
        cls.graph = os.path.join(root, "out")
        os.makedirs(cls.graph)
        _write(cls.source, "src/liba/a.c", _A_C)
        _write(cls.source, "src/liba/a.h", _A_H)
        _write(cls.source, "src/libb/b.c", _B_C)
        _write(cls.source, "src/libb/b.h", _B_H)

        env = dict(os.environ, PYTHONPATH=SCRIPTS_DIR)
        scan = subprocess.run(
            [sys.executable, SCANNER, "scan",
             "--source", cls.source,
             "--output", os.path.join(cls.graph, "extraction.json"),
             "--extraction-backend", "tree-sitter",
             "--no-interactive", "--auto-profile"] + _SCAN_MEM_FLAGS,
            capture_output=True, text=True, timeout=300, env=env)
        if scan.returncode != 0:
            raise AssertionError("scan failed:\n%s\n%s"
                                 % (scan.stdout[-2000:], scan.stderr[-2000:]))
        build = subprocess.run(
            [sys.executable, BUILDER, "build",
             "--extraction", os.path.join(cls.graph, "extraction.json"),
             "--outdir", cls.graph] + _BUILD_MEM_FLAGS,
            capture_output=True, text=True, timeout=300, env=env)
        if build.returncode != 0:
            raise AssertionError("build failed:\n%s\n%s"
                                 % (build.stdout[-2000:], build.stderr[-2000:]))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _master(self):
        with open(os.path.join(self.graph,
                               "code2database_master.json")) as f:
            return json.load(f)

    def test_scan_extracted_all_functions(self):
        with open(os.path.join(self.graph, "extraction.json")) as f:
            ext = json.load(f)
        names = {fn.get("name") for fn in ext.get("functions", [])}
        self.assertTrue({"a_public", "a_helper", "b_public", "b_entry"}
                        <= names, "scanner missed functions: %r" % names)

    def test_master_has_both_cross_domain_calls(self):
        cross = self._master().get("cross_domain_edges", [])
        pairs = {(e.get("source", ""), e.get("target", "")) for e in cross}
        a_to_b = [p for p in pairs
                  if "a_public" in p[0] and "b_public" in p[1]]
        b_to_a = [p for p in pairs
                  if "b_entry" in p[0] and "a_public" in p[1]]
        self.assertTrue(a_to_b,
                        "a_public -> b_public missing (cross=%r)" % (pairs,))
        self.assertTrue(b_to_a,
                        "b_entry -> a_public missing (cross=%r)" % (pairs,))

    def test_load_restores_cross_domain_edges_for_neighbors(self):
        from _builder.graph_build import _load_full_graph
        G = _load_full_graph(self.graph)
        a_pub = [n for n in G.nodes if n.endswith("a_public")]
        b_pub = [n for n in G.nodes if n.endswith("b_public")]
        self.assertTrue(a_pub and b_pub, "nodes missing after load")
        self.assertTrue(G.has_edge(a_pub[0], b_pub[0]),
                        "load() lost the a_public -> b_public edge; "
                        "neighbors/Web UI would show an isolated node")


if __name__ == "__main__":
    unittest.main()
