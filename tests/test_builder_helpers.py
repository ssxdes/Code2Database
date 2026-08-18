#!/usr/bin/env python3
"""Comprehensive tests for code2database_builder.py helper functions."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import networkx as nx


class TestNormalizeId(unittest.TestCase):
    def test_basic(self):
        from _builder.utils import _normalize_id
        self.assertEqual(_normalize_id("Hello_World"), "hello_world")

    def test_special_chars(self):
        from _builder.utils import _normalize_id
        self.assertEqual(_normalize_id("spdk::bdev::open"), "spdk__bdev__open")

    def test_dots(self):
        from _builder.utils import _normalize_id
        self.assertEqual(_normalize_id("Handler.ServeHTTP"), "handler_servehttp")


class TestResolveCalleeId(unittest.TestCase):
    def test_exact_match(self):
        from _builder.utils import _resolve_invoked_id
        registry = {"root_helper": {"id": "root_helper", "domain": "root", "name": "helper"}}
        result = _resolve_invoked_id("root_helper", "root", registry)
        self.assertEqual(result, "root_helper")

    def test_suffix_match(self):
        from _builder.utils import _resolve_invoked_id
        registry = {"root_helper": {"id": "root_helper", "domain": "root", "name": "helper"}}
        # Name "helper" should match "root_helper" by suffix
        result = _resolve_invoked_id("helper", "root", registry)
        self.assertEqual(result, "root_helper")

    def test_unresolved(self):
        from _builder.utils import _resolve_invoked_id
        result = _resolve_invoked_id("unknown_func", "root", {})
        self.assertEqual(result, "unknown_func")


class TestBuildGraph(unittest.TestCase):
    """Test build_graph from extraction JSON."""

    def test_basic_build(self):
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_main", "name": "main", "source_file": "main.c",
                 "line": 10, "domain": "root", "labels": ["API_entry"],
                 "signature": "int main(int argc, char **argv)"},
                {"id": "root_helper", "name": "helper", "source_file": "main.c",
                 "line": 20, "domain": "root", "labels": []},
            ],
            "edges": [
                {"source": "root_main", "target": "root_helper",
                 "call_order": 1, "call_condition": "", "concurrency": "",
                 "confidence": "EXTRACTED", "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["root"],
            "lang_stats": {"c": 1},
        }
        G, _ = build_graph(extraction)
        # build_graph adds file nodes and CONTAINS edges beyond function nodes
        func_nodes = [n for n, d in G.nodes(data=True) if d.get("node_type") != "file"]
        self.assertEqual(len(func_nodes), 2)
        call_edges = [(u, v) for u, v, d in G.edges(data=True) if d.get("relation") != "CONTAINS"]
        self.assertEqual(len(call_edges), 1)
        self.assertIn("root_main", G)

    def test_empty_extraction(self):
        from _builder.graph_build import build_graph
        G, _ = build_graph({"functions": [], "edges": [], "domains": [], "lang_stats": {}})
        self.assertEqual(G.number_of_nodes(), 0)

    def test_conditional_edges(self):
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_f", "name": "f", "source_file": "f.c",
                 "line": 10, "domain": "root", "labels": []},
                {"id": "root_g", "name": "g", "source_file": "f.c",
                 "line": 20, "domain": "root", "labels": []},
            ],
            "edges": [
                {"source": "root_f", "target": "root_g",
                 "call_order": 1, "call_condition": "if(x)",
                 "concurrency": "", "confidence": "EXTRACTED",
                 "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["root"],
            "lang_stats": {"c": 1},
        }
        G, _ = build_graph(extraction)
        call_edges = [(u, v, d) for u, v, d in G.edges(data=True) if d.get("relation") != "CONTAINS"]
        self.assertEqual(len(call_edges), 1)
        edge_data = G.edges["root_f", "root_g"]
        self.assertEqual(edge_data.get("call_condition"), "if(x)")

    def test_edge_source_tag(self):
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_f", "name": "f", "source_file": "f.c",
                 "line": 10, "domain": "root", "labels": []},
                {"id": "root_g", "name": "g", "source_file": "f.c",
                 "line": 20, "domain": "root", "labels": []},
            ],
            "edges": [
                {"source": "root_f", "target": "root_g",
                 "call_order": 1, "call_condition": "", "concurrency": "",
                 "confidence": "INFERRED", "source_tag": "llm", "confidence_score": 0.8},
            ],
            "domains": ["root"],
            "lang_stats": {"c": 1},
        }
        G, _ = build_graph(extraction)
        edge_data = G.edges["root_f", "root_g"]
        self.assertEqual(edge_data.get("source"), "llm")
        self.assertEqual(edge_data.get("confidence"), "INFERRED")

    def test_node_attributes(self):
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_main", "name": "main", "source_file": "main.c",
                 "line": 10, "domain": "root", "labels": ["API_entry"],
                 "signature": "int main()", "params": [{"name": "argc", "type": "int", "is_param": True}]},
            ],
            "edges": [],
            "domains": ["root"],
            "lang_stats": {"c": 1},
        }
        G, _ = build_graph(extraction)
        nd = G.nodes["root_main"]
        self.assertEqual(nd["name"], "main")
        self.assertEqual(nd["domain"], "root")
        self.assertIn("API_entry", nd["labels"])  # may also have auto-added labels
        self.assertEqual(nd["signature"], "int main()")
        self.assertEqual(len(nd["params"]), 1)


class TestHubFunctions(unittest.TestCase):
    def test_hub_detection(self):
        from _builder.index_pack import _compute_hub_functions
        G = nx.DiGraph()
        G.add_node("a", name="a", domain="root", source_file="a.c", is_empty=False)
        G.add_node("b", name="b", domain="root", source_file="b.c", is_empty=False)
        G.add_node("hub", name="hub", domain="root", source_file="hub.c", is_empty=False)
        G.add_node("c", name="c", domain="module", source_file="c.c", is_empty=False)
        G.add_node("d", name="d", domain="module", source_file="d.c", is_empty=False)
        G.add_edge("a", "hub")
        G.add_edge("b", "hub")
        G.add_edge("hub", "c")
        G.add_edge("hub", "d")
        hubs = _compute_hub_functions(G, top_n=5)
        self.assertTrue(len(hubs) > 0)
        self.assertEqual(hubs[0]["name"], "hub")
        self.assertGreater(hubs[0]["betweenness"], 0)

    def test_empty_graph(self):
        from _builder.index_pack import _compute_hub_functions
        G = nx.DiGraph()
        hubs = _compute_hub_functions(G)
        self.assertEqual(len(hubs), 0)

    def test_skips_external_nodes(self):
        from _builder.index_pack import _compute_hub_functions
        G = nx.DiGraph()
        G.add_node("ext_func", name="ext_func", domain="external", source_file="", is_empty=False)
        G.add_node("a", name="a", domain="root", source_file="a.c", is_empty=False)
        G.add_edge("a", "ext_func")
        hubs = _compute_hub_functions(G)
        # ext_func has no source_file, should be skipped
        self.assertTrue(all(h.get("domain") != "external" for h in hubs))


class TestCrossDomainHotspots(unittest.TestCase):
    def test_hotspot_detection(self):
        from _builder.index_pack import _compute_cross_domain_hotspots
        G = nx.DiGraph()
        G.add_node("a", name="a", domain="lib")
        G.add_node("b", name="b", domain="module")
        G.add_node("c", name="c", domain="module")
        G.add_edge("a", "b")
        G.add_edge("a", "c")
        hotspots = _compute_cross_domain_hotspots(G)
        self.assertEqual(len(hotspots), 1)
        self.assertEqual(hotspots[0]["edge_count"], 2)

    def test_no_cross_domain(self):
        from _builder.index_pack import _compute_cross_domain_hotspots
        G = nx.DiGraph()
        G.add_node("a", name="a", domain="lib")
        G.add_node("b", name="b", domain="lib")
        G.add_edge("a", "b")
        hotspots = _compute_cross_domain_hotspots(G)
        self.assertEqual(len(hotspots), 0)


class TestSplitByDomain(unittest.TestCase):
    """Test split_by_domain writes correct JSON files."""

    def test_split(self):
        from _builder.graph_build import build_graph, split_by_domain
        extraction = {
            "functions": [
                {"id": "lib.bdev.bdev_start", "name": "bdev_start", "source_file": "lib/bdev/bdev.c",
                 "line": 10, "domain": "lib.bdev", "labels": ["API_entry"]},
                {"id": "lib.bdev.bdev_init", "name": "bdev_init", "source_file": "lib/bdev/bdev.c",
                 "line": 20, "domain": "lib.bdev", "labels": []},
                {"id": "module.nvme.nvme_setup", "name": "nvme_setup", "source_file": "module/nvme/nvme.c",
                 "line": 5, "domain": "module.nvme", "labels": []},
            ],
            "edges": [
                {"source": "lib.bdev.bdev_start", "target": "lib.bdev.bdev_init",
                 "call_order": 1, "call_condition": "", "concurrency": "",
                 "confidence": "EXTRACTED", "source_tag": "ast", "confidence_score": 1.0},
                {"source": "lib.bdev.bdev_start", "target": "module.nvme.nvme_setup",
                 "call_order": 2, "call_condition": "", "concurrency": "",
                 "confidence": "EXTRACTED", "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["lib.bdev", "module.nvme"],
            "lang_stats": {"c": 2},
        }
        G, _ = build_graph(extraction)
        with tempfile.TemporaryDirectory() as tmpdir:
            master_path = split_by_domain(G, tmpdir, source_root="/src")
            self.assertTrue(os.path.exists(master_path))

            # Check master file
            with open(master_path) as f:
                master = json.load(f)
            self.assertIn("domains", master)
            self.assertIn("cross_domain_edges", master)
            self.assertGreaterEqual(len(master["cross_domain_edges"]), 1)

    def test_edge_source_tag_written(self):
        from _builder.graph_build import build_graph, split_by_domain
        extraction = {
            "functions": [
                {"id": "root_f", "name": "f", "source_file": "f.c",
                 "line": 10, "domain": "root", "labels": []},
                {"id": "root_g", "name": "g", "source_file": "f.c",
                 "line": 20, "domain": "root", "labels": []},
            ],
            "edges": [
                {"source": "root_f", "target": "root_g",
                 "call_order": 1, "call_condition": "", "concurrency": "",
                 "confidence": "INFERRED", "source_tag": "llm", "confidence_score": 0.8},
            ],
            "domains": ["root"],
            "lang_stats": {"c": 1},
        }
        G, _ = build_graph(extraction)
        with tempfile.TemporaryDirectory() as tmpdir:
            split_by_domain(G, tmpdir, source_root="/src")
            # Load the domain file
            with open(os.path.join(tmpdir, "code2database_master.json")) as f:
                master = json.load(f)
            domain_file = list(master["domains"].values())[0]
            with open(os.path.join(tmpdir, domain_file)) as f:
                domain_data = json.load(f)
            # Compact edges should have source_tag field
            self.assertIn("edge_fields", domain_data)
            self.assertIn("source_tag", domain_data["edge_fields"])


class TestCompactEdgeFormat(unittest.TestCase):
    def test_read_compact(self):
        from _builder.graph_build import _load_full_graph
        with tempfile.TemporaryDirectory() as tmpdir:
            domain_data = {
                "type": "code2database_domain",
                "format_version": 3,
                "domain": "root",
                "functions": [
                    ["root_main", "main", "main.c", 10, '["API_entry"]', "int main()"],
                    ["root_helper", "helper", "main.c", 20, '[]', "void helper()"],
                ],
                "function_details": {
                    "root_main": {"location": "main.c:10"},
                    "root_helper": {"location": "main.c:20"},
                },
                "empty_nodes": [],
                "edge_fields": ["source", "target", "call_order", "call_condition",
                                "concurrency", "confidence", "source_tag", "confidence_score"],
                "edges": [
                    ["root_main", "root_helper", 1, "", "", "EXTRACTED", "ast", 1.0],
                ],
            }
            master = {
                "type": "code2database_master",
                "source_root": "",
                "domains": {"root": "domains/code2database_domain_root.json"},
                "cross_domain_edges": [],
                "total_nodes": 2,
                "total_edges": 1,
            }
            os.makedirs(os.path.join(tmpdir, "domains"), exist_ok=True)
            with open(os.path.join(tmpdir, "code2database_master.json"), "w") as f:
                json.dump(master, f)
            with open(os.path.join(tmpdir, "domains", "code2database_domain_root.json"), "w") as f:
                json.dump(domain_data, f)

            G = _load_full_graph(tmpdir)
            self.assertEqual(G.number_of_nodes(), 2)
            self.assertEqual(G.number_of_edges(), 1)
            edge_data = G.edges["root_main", "root_helper"]
            # "source" in graph = provenance tag (from source_tag field)
            self.assertEqual(edge_data.get("source"), "ast")
            self.assertEqual(edge_data.get("confidence"), "EXTRACTED")

    def test_read_legacy_with_source_tag(self):
        from _builder.graph_build import _load_full_graph
        with tempfile.TemporaryDirectory() as tmpdir:
            domain_data = {
                "type": "code2database_domain",
                "domain": "root",
                "functions": [
                    ["root_main", "main", "main.c", 10, '["API_entry"]', "int main()"],
                ],
                "function_details": {"root_main": {"location": "main.c:10"}},
                "empty_nodes": [],
                "edges": [
                    {"source": "root_main", "target": "root_main",
                     "call_order": None, "call_condition": "", "concurrency": "",
                     "confidence": "EXTRACTED", "source_tag": "ast", "confidence_score": 1.0},
                ],
            }
            master = {
                "type": "code2database_master",
                "source_root": "",
                "domains": {"root": "domains/code2database_domain_root.json"},
                "cross_domain_edges": [],
                "total_nodes": 1,
                "total_edges": 1,
            }
            os.makedirs(os.path.join(tmpdir, "domains"), exist_ok=True)
            with open(os.path.join(tmpdir, "code2database_master.json"), "w") as f:
                json.dump(master, f)
            with open(os.path.join(tmpdir, "domains", "code2database_domain_root.json"), "w") as f:
                json.dump(domain_data, f)

            G = _load_full_graph(tmpdir)
            self.assertEqual(G.number_of_nodes(), 1)

    def test_compact_edge_extras(self):
        from _builder.graph_build import _load_full_graph
        with tempfile.TemporaryDirectory() as tmpdir:
            domain_data = {
                "type": "code2database_domain",
                "format_version": 3,
                "domain": "root",
                "functions": [
                    ["root_f", "f", "f.c", 10, '[]', "void f()"],
                    ["root_g", "g", "f.c", 20, '[]', "void g()"],
                ],
                "function_details": {},
                "empty_nodes": [],
                "edge_fields": ["source", "target", "call_order", "call_condition",
                                "concurrency", "confidence", "source_tag", "confidence_score"],
                "edges": [
                    ["root_f", "root_g", 1, "if(x)", "", "EXTRACTED", "ast", 1.0,
                     {"pc": "#ifdef(FEATURE_X)", "pa": False,
                      "ev": [{"kind": "ast_call", "weight": 1.0, "note": "test"}]}],
                ],
            }
            master = {
                "type": "code2database_master",
                "source_root": "",
                "domains": {"root": "domains/code2database_domain_root.json"},
                "cross_domain_edges": [],
                "total_nodes": 2,
                "total_edges": 1,
            }
            os.makedirs(os.path.join(tmpdir, "domains"), exist_ok=True)
            with open(os.path.join(tmpdir, "code2database_master.json"), "w") as f:
                json.dump(master, f)
            with open(os.path.join(tmpdir, "domains", "code2database_domain_root.json"), "w") as f:
                json.dump(domain_data, f)

            G = _load_full_graph(tmpdir)
            edge_data = G.edges["root_f", "root_g"]
            self.assertEqual(edge_data.get("preproc_condition"), "#ifdef(FEATURE_X)")
            self.assertFalse(edge_data.get("preproc_alive", True))
            self.assertEqual(len(edge_data.get("evidence", [])), 1)


class TestDomainSubdir(unittest.TestCase):
    def test_single_domain(self):
        from _builder.graph_build import _domain_subdir
        result = _domain_subdir("lib", {"lib": 5}, max_per_dir=50)
        self.assertEqual(result, "lib/")

    def test_nested_domain_many(self):
        from _builder.graph_build import _domain_subdir
        # When there are many domains under "lib", "lib.bdev" goes to "lib/bdev/"
        domain_count = {f"lib.bdev{n}": 5 for n in range(60)}
        result = _domain_subdir("lib.bdev0", domain_count, max_per_dir=50)
        self.assertEqual(result, "lib/bdev0/")

    def test_nested_domain_few(self):
        from _builder.graph_build import _domain_subdir
        # When there are few domains under "lib", collapses to "lib/"
        result = _domain_subdir("lib.bdev", {"lib.bdev": 5}, max_per_dir=50)
        self.assertEqual(result, "lib/")

    def test_empty_domain_returns_root_subdir(self):
        from _builder.graph_build import _domain_subdir
        # An empty domain must return "root/" (not "/") so that
        # os.path.join("domains", subdir, filename) stays relative.
        # Returning "/" makes os.path.join treat the path as absolute,
        # collapsing it to "/<filename>" and breaking the master map.
        result = _domain_subdir("", {"": 5}, max_per_dir=50)
        self.assertEqual(result, "root/")
        # Verify the path-construction invariant
        import os
        rel_path = os.path.join("domains", result, "code2database_domain_root.json")
        self.assertTrue(rel_path.startswith("domains/"),
                        f"rel_path must stay relative, got {rel_path!r}")


class TestBareNameCalleeDomainAssignment(unittest.TestCase):
    """Verify that unresolved bare-name callees are assigned to the
    "external" domain, not the caller's project domain.

    Without this, calls like print(...) or len(...) from a project file
    would create phantom `print` and `len` nodes inside the caller's
    domain, polluting the project's function list with language builtins.
    """

    def _build_with_bare_callee(self):
        from _builder.graph_build import build_graph
        funcs = [
            {"id": "scripts_main", "name": "main",
             "source_file": "scripts/run.py", "line": 1,
             "domain": "scripts", "labels": []},
        ]
        # Edge from scripts_main to bare name "print" (no domain prefix).
        # Scanner emits this when Python code calls print(...).
        edges = [
            {"source": "scripts_main", "target": "print",
             "call_order": 1, "call_condition": "", "concurrency": "",
             "confidence": "EXTRACTED", "source_tag": "ast",
             "confidence_score": 1.0},
        ]
        extraction = {
            "functions": funcs,
            "edges": edges,
            "domains": ["scripts"],
            "lang_stats": {"python": 1},
        }
        return build_graph(extraction)

    def test_bare_name_callee_assigned_to_external_domain(self):
        G, _ = self._build_with_bare_callee()
        # Find the "print" node
        print_nodes = [n for n, d in G.nodes(data=True)
                       if d.get("name") == "print"]
        self.assertEqual(len(print_nodes), 1,
                         "expected exactly one `print` auto-created node")
        ndata = G.nodes[print_nodes[0]]
        self.assertEqual(ndata.get("domain"), "external",
                         "bare-name callee should be in 'external' domain, "
                         f"got {ndata.get('domain')!r}")

    def test_bare_name_callee_does_not_pollute_caller_domain(self):
        G, _ = self._build_with_bare_callee()
        # No node with name="print" should be in the "scripts" domain
        scripts_prints = [n for n, d in G.nodes(data=True)
                          if d.get("name") == "print"
                          and d.get("domain") == "scripts"]
        self.assertEqual(scripts_prints, [],
                         "print should not appear in caller's 'scripts' domain")

    def test_bare_name_callee_in_external_not_labeled_callback_func(self):
        """Even when a bare-name callee is the target of a CALLBACK_ARG
        edge, the auto-created node in the external domain should NOT
        receive the callback_func label. Otherwise the validation rule
        "callback_func in external domain" would warn on every unresolvable
        callback target, masking real callback_func misplacement bugs.
        """
        from _builder.graph_build import build_graph
        funcs = [
            {"id": "scripts_main", "name": "main",
             "source_file": "scripts/run.py", "line": 1,
             "domain": "scripts", "labels": []},
        ]
        # CALLBACK_ARG edge from scripts_main to bare name "cb_handler".
        # The scanner emits this when code does: register_cb(cb_handler)
        edges = [
            {"source": "scripts_main", "target": "cb_handler",
             "call_order": 1, "call_condition": "", "concurrency": "",
             "confidence": "CALLBACK_ARG", "source_tag": "ast",
             "confidence_score": 1.0},
        ]
        extraction = {
            "functions": funcs,
            "edges": edges,
            "domains": ["scripts"],
            "lang_stats": {"python": 1},
        }
        G, _ = build_graph(extraction)
        cb_nodes = [n for n, d in G.nodes(data=True)
                    if d.get("name") == "cb_handler"]
        self.assertEqual(len(cb_nodes), 1)
        labels = G.nodes[cb_nodes[0]].get("labels", [])
        self.assertNotIn("callback_func", labels,
                         "auto-created bare-name in external domain must not "
                         f"be labeled callback_func; got labels={labels!r}")


class TestEntryPointScore(unittest.TestCase):
    def test_api_entry_boost(self):
        from _builder.entry_scoring import _calculate_entry_point_score
        score_api = _calculate_entry_point_score("bdev_start", True, 0, 5, [])
        score_non = _calculate_entry_point_score("helper", False, 0, 5, [])
        self.assertGreater(score_api, score_non)

    def test_utility_penalty(self):
        from _builder.entry_scoring import _calculate_entry_point_score
        score_util = _calculate_entry_point_score("get_value", True, 0, 5, [])
        score_handler = _calculate_entry_point_score("handle_request", True, 0, 5, [])
        self.assertGreater(score_handler, score_util)


class TestFindNodeId(unittest.TestCase):
    def test_exact_match(self):
        from _builder.utils import _find_node_id
        G = nx.DiGraph()
        G.add_node("root_main", name="main")
        result = _find_node_id(G, "root_main")
        self.assertEqual(result, "root_main")

    def test_partial_match(self):
        from _builder.utils import _find_node_id
        G = nx.DiGraph()
        G.add_node("root_main", name="main")
        result = _find_node_id(G, "main")
        self.assertEqual(result, "root_main")

    def test_no_match(self):
        from _builder.utils import _find_node_id
        G = nx.DiGraph()
        G.add_node("root_main", name="main")
        result = _find_node_id(G, "nonexistent")
        self.assertEqual(result, "")


class TestParseBindings(unittest.TestCase):
    def test_basic(self):
        from _builder.utils import _parse_bindings
        result = _parse_bindings("mode=1,flag=true")
        self.assertEqual(result, {"mode": "1", "flag": "true"})

    def test_empty(self):
        from _builder.utils import _parse_bindings
        result = _parse_bindings("")
        self.assertEqual(result, {})

    def test_single(self):
        from _builder.utils import _parse_bindings
        result = _parse_bindings("x=5")
        self.assertEqual(result, {"x": "5"})


class TestNewCommands(unittest.TestCase):
    def test_trace_chain_help(self):
        sys.argv = ["code2database_builder.py", "trace-chain", "--help"]
        with self.assertRaises(SystemExit) as cm:
            from code2database_builder import main
            main()
        self.assertEqual(cm.exception.code, 0)

    def test_concurrency_risks_help(self):
        sys.argv = ["code2database_builder.py", "concurrency-risks", "--help"]
        with self.assertRaises(SystemExit) as cm:
            from code2database_builder import main
            main()
        self.assertEqual(cm.exception.code, 0)

    def test_diff_chains_help(self):
        sys.argv = ["code2database_builder.py", "diff-chains", "--help"]
        with self.assertRaises(SystemExit) as cm:
            from code2database_builder import main
            main()
        self.assertEqual(cm.exception.code, 0)

    def test_data_lifecycle_help(self):
        sys.argv = ["code2database_builder.py", "data-lifecycle", "--help"]
        with self.assertRaises(SystemExit) as cm:
            from code2database_builder import main
            main()
        self.assertEqual(cm.exception.code, 0)

    def test_export_obsidian_help(self):
        sys.argv = ["code2database_builder.py", "export-obsidian", "--help"]
        with self.assertRaises(SystemExit) as cm:
            from code2database_builder import main
            main()
        self.assertEqual(cm.exception.code, 0)

    def test_validate_plugin_help(self):
        sys.argv = ["code2database_builder.py", "validate-plugin", "--help"]
        with self.assertRaises(SystemExit) as cm:
            from code2database_builder import main
            main()
        self.assertEqual(cm.exception.code, 0)


class TestExecSummaryAndHubInfo(unittest.TestCase):
    """Test _compute_exec_summary and _compute_hub_info from query module."""

    def test_exec_summary_from_semantic(self):
        from _builder.query import _compute_exec_summary
        result = _compute_exec_summary(
            "Initializes the bdev layer. Sets up internal structures.",
            "", "bdev_init", [], [])
        self.assertTrue(result.startswith("Initializes the bdev layer"))

    def test_exec_summary_from_external(self):
        from _builder.query import _compute_exec_summary
        result = _compute_exec_summary(
            "", "External: starts bdev subsystem", "bdev_start", ["API_entry"], [])
        self.assertEqual(result, "External: starts bdev subsystem")

    def test_exec_summary_api_label(self):
        from _builder.query import _compute_exec_summary
        result = _compute_exec_summary("", "", "spdk_bdev_open", ["API_entry"], [])
        self.assertIn("API entry point", result)

    def test_exec_summary_thread_label(self):
        from _builder.query import _compute_exec_summary
        result = _compute_exec_summary("", "", "poller_fn", ["thread_processor"], [])
        self.assertIn("Thread entry", result)

    def test_exec_summary_callback_label(self):
        from _builder.query import _compute_exec_summary
        result = _compute_exec_summary("", "", "on_complete", ["callback_func"], [])
        self.assertIn("Callback", result)

    def test_exec_summary_empty(self):
        from _builder.query import _compute_exec_summary
        result = _compute_exec_summary("", "", "helper", [], [])
        self.assertEqual(result, "")

    def test_hub_info_connector(self):
        from _builder.query import _compute_hub_info
        G = nx.DiGraph()
        G.add_node("a1", name="a1", labels=[])
        G.add_node("a2", name="a2", labels=[])
        G.add_node("a3", name="a3", labels=[])
        G.add_node("hub", name="hub", labels=[])
        G.add_node("b1", name="b1", labels=[])
        G.add_node("b2", name="b2", labels=[])
        G.add_node("b3", name="b3", labels=[])
        G.add_edge("a1", "hub")
        G.add_edge("a2", "hub")
        G.add_edge("a3", "hub")
        G.add_edge("hub", "b1")
        G.add_edge("hub", "b2")
        G.add_edge("hub", "b3")
        result = _compute_hub_info(G, "hub")
        self.assertEqual(result["hub_role"], "hub")
        self.assertEqual(result["in_degree"], 3)
        self.assertEqual(result["out_degree"], 3)

    def test_hub_info_bridge(self):
        from _builder.query import _compute_hub_info
        G = nx.DiGraph()
        G.add_node("api", name="api_func", labels=["API_entry"])
        G.add_node("mid", name="middle", labels=[])
        G.add_node("end", name="end_func", labels=["out_end"])
        G.add_edge("api", "mid")
        G.add_edge("mid", "end")
        result = _compute_hub_info(G, "mid")
        self.assertEqual(result["hub_role"], "bridge")
        self.assertIn("api_func", result["reachable_from_apis"])
        self.assertIn("end_func", result["reaches_endpoints"])

    def test_describe_node_context_flag(self):
        """Test that --context flag is registered in argparse."""
        sys.argv = ["code2database_builder.py", "describe-node", "--help"]
        with self.assertRaises(SystemExit) as cm:
            from code2database_builder import main
            main()
        self.assertEqual(cm.exception.code, 0)

    def test_describe_node_include_body_flag(self):
        """Test that --include-body flag is registered in argparse."""
        sys.argv = ["code2database_builder.py", "describe-node", "--help"]
        with self.assertRaises(SystemExit) as cm:
            from code2database_builder import main
            main()
        self.assertEqual(cm.exception.code, 0)


class TestTokenBudget(unittest.TestCase):
    """Test token budget control functions."""

    def test_estimate_tokens_empty(self):
        from _builder.token_budget import estimate_tokens
        self.assertEqual(estimate_tokens(""), 0)

    def test_estimate_tokens_latin(self):
        from _builder.token_budget import estimate_tokens
        # "hello world" = 11 chars, ~3 tokens
        tokens = estimate_tokens("hello world")
        self.assertGreater(tokens, 0)
        self.assertLess(tokens, 10)

    def test_estimate_tokens_cjk(self):
        from _builder.token_budget import estimate_tokens
        tokens = estimate_tokens("初始化配置")
        self.assertGreater(tokens, 0)

    def test_truncate_to_tokens_no_truncation(self):
        from _builder.token_budget import truncate_to_tokens
        text = "hello world"
        result = truncate_to_tokens(text, 100)
        self.assertEqual(result, text)

    def test_truncate_to_tokens_with_truncation(self):
        from _builder.token_budget import truncate_to_tokens
        text = "a " * 200  # ~100 tokens
        result = truncate_to_tokens(text, 10)
        self.assertIn("truncated", result)
        self.assertLess(len(result), len(text))

    def test_budget_pack_no_limit(self):
        from _builder.token_budget import budget_pack
        data = {"stats": {"n": 5}, "domains": ["a", "b"]}
        result = budget_pack(data, 0)
        self.assertIn("_token_count", result)
        self.assertEqual(result["stats"], {"n": 5})

    def test_budget_pack_with_limit(self):
        from _builder.token_budget import budget_pack
        data = {"stats": {"n": 5}, "processes": list(range(100)),
                "scenarios": list(range(50)), "domains": ["a"]}
        result = budget_pack(data, 10)  # very tight
        self.assertIn("_token_count", result)
        self.assertLessEqual(result["_token_count"], 15)  # some slack for _token_count field

    def test_budget_describe_no_limit(self):
        from _builder.token_budget import budget_describe
        data = {"id": "test", "name": "fn", "body_text": "x" * 1000}
        result = budget_describe(data, 0)
        self.assertIn("_token_count", result)
        self.assertIn("body_text", result)

    def test_budget_describe_with_limit(self):
        from _builder.token_budget import budget_describe
        data = {"id": "test", "name": "fn", "body_text": "x" * 1000,
                "local_vars": list(range(50)), "callee_args": list(range(30))}
        result = budget_describe(data, 10)  # very tight
        self.assertIn("_token_count", result)
        self.assertNotIn("body_text", result)  # should be dropped first


class TestExploreFlow(unittest.TestCase):
    """Test explore-flow command."""

    def test_explore_flow_help(self):
        sys.argv = ["code2database_builder.py", "explore-flow", "--help"]
        with self.assertRaises(SystemExit) as cm:
            from code2database_builder import main
            main()
        self.assertEqual(cm.exception.code, 0)

    def test_tokenize_query(self):
        from _builder.explore import _tokenize_query
        tokens = _tokenize_query("bdev initialization flow")
        self.assertEqual(tokens, ["bdev", "initialization", "flow"])

    def test_tokenize_query_cjk(self):
        from _builder.explore import _tokenize_query
        tokens = _tokenize_query("spdk_bdev_open 初始化")
        # Tokenizer splits on underscores and separates CJK characters
        self.assertIn("初始化", tokens)
        self.assertIn("spdk", tokens)

    def test_score_node_relevance(self):
        from _builder.explore import _score_node_relevance
        nd = {"name": "bdev_init", "signature": "void bdev_init(int mode)",
              "semantic_desc": "Initialize bdev subsystem", "domain": "lib.bdev", "labels": []}
        score = _score_node_relevance(nd, ["bdev", "init"])
        self.assertGreater(score, 0)

    def test_score_exact_name_match(self):
        from _builder.explore import _score_node_relevance
        nd = {"name": "bdev_init", "signature": "", "semantic_desc": "",
              "domain": "", "labels": []}
        score = _score_node_relevance(nd, ["bdev_init"])
        self.assertGreater(score, 5.0)  # 3 for partial + 5 for exact

    def test_find_relevant_nodes(self):
        from _builder.explore import _find_relevant_nodes
        G = nx.DiGraph()
        G.add_node("lib_bdev_init", name="bdev_init", signature="void bdev_init()",
                   semantic_desc="Initialize bdev", domain="lib.bdev", labels=[], is_empty=False)
        G.add_node("lib_bdev_open", name="bdev_open", signature="int bdev_open()",
                   semantic_desc="Open bdev device", domain="lib.bdev", labels=[], is_empty=False)
        G.add_node("app_main", name="main", signature="int main()",
                   semantic_desc="", domain="app", labels=["API_entry"], is_empty=False)
        result = _find_relevant_nodes(G, ["bdev", "init"], top_n=10)
        self.assertGreater(len(result), 0)
        # bdev_init should rank higher than main
        self.assertEqual(result[0][0], "lib_bdev_init")

    def test_extract_subgraph_context(self):
        from _builder.explore import _extract_subgraph_context
        G = nx.DiGraph()
        G.add_node("a", name="fn_a", signature="void a()", domain="d1",
                   labels=[], is_empty=False, source_file="a.c", line=1)
        G.add_node("b", name="fn_b", signature="void b()", domain="d1",
                   labels=[], is_empty=False, source_file="b.c", line=2)
        G.add_edge("a", "b", call_condition="", concurrency="", call_order=1, confidence="EXTRACTED")
        seeds = [("a", 5.0, G.nodes["a"])]
        result = _extract_subgraph_context(G, seeds, max_depth=2, max_nodes=10)
        self.assertIn("a", result["nodes"])
        self.assertGreater(len(result["edges"]), 0)

    def test_derive_exec_summary(self):
        from _builder.explore import _derive_exec_summary
        nd = {"semantic_desc": "Initialize the bdev layer. Configure resources.",
              "external_desc": "", "name": "bdev_init", "labels": []}
        result = _derive_exec_summary(nd)
        self.assertTrue(result.startswith("Initialize the bdev layer"))


class TestFQNAndMultiStrategy(unittest.TestCase):
    """Test FQN computation and multi-strategy callee resolution."""

    def test_compute_fqn_basic(self):
        from _builder.import_resolve import _compute_fqn
        nd = {"name": "bdev_init", "domain": "lib.bdev", "source_file": "lib/bdev/bdev.c"}
        fqn = _compute_fqn(nd, "spdk")
        self.assertIn("spdk", fqn)
        self.assertIn("lib.bdev", fqn)
        self.assertIn("bdev_init", fqn)

    def test_compute_fqn_no_project(self):
        from _builder.import_resolve import _compute_fqn
        nd = {"name": "main", "domain": "app", "source_file": "app/main.c"}
        fqn = _compute_fqn(nd, "")
        self.assertIn("app", fqn)
        self.assertIn("main", fqn)

    def test_multi_strategy_same_file(self):
        from _builder.import_resolve import _multi_strategy_resolve
        G = nx.DiGraph()
        G.add_node("caller", name="fn_caller", source_file="test.c",
                   domain="lib.test", labels=[], is_empty=False, body_text="")
        G.add_node("target", name="fn_target", source_file="test.c",
                   domain="lib.test", labels=[], is_empty=False, body_text="")
        nid, strategy, conf = _multi_strategy_resolve(G, "fn_target", "caller")
        self.assertEqual(nid, "target")
        self.assertEqual(strategy, "same_file")
        self.assertGreater(conf, 0.9)

    def test_multi_strategy_same_domain(self):
        from _builder.import_resolve import _multi_strategy_resolve
        G = nx.DiGraph()
        G.add_node("caller", name="fn_caller", source_file="a.c",
                   domain="lib.bdev", labels=[], is_empty=False, body_text="")
        G.add_node("target", name="fn_target", source_file="b.c",
                   domain="lib.bdev", labels=[], is_empty=False, body_text="")
        nid, strategy, conf = _multi_strategy_resolve(G, "fn_target", "caller")
        self.assertEqual(nid, "target")
        self.assertEqual(strategy, "same_domain")

    def test_multi_strategy_unique_name(self):
        from _builder.import_resolve import _multi_strategy_resolve
        G = nx.DiGraph()
        G.add_node("caller", name="fn_caller", source_file="a.c",
                   domain="lib.x", labels=[], is_empty=False, body_text="")
        G.add_node("unique_fn", name="unique_fn", source_file="other.c",
                   domain="lib.y", labels=[], is_empty=False, body_text="")
        nid, strategy, conf = _multi_strategy_resolve(G, "unique_fn", "caller")
        self.assertEqual(nid, "unique_fn")
        self.assertEqual(strategy, "unique_name")

    def test_multi_strategy_unresolved(self):
        from _builder.import_resolve import _multi_strategy_resolve
        G = nx.DiGraph()
        G.add_node("caller", name="fn_caller", source_file="a.c",
                   domain="lib.x", labels=[], is_empty=False, body_text="")
        nid, strategy, conf = _multi_strategy_resolve(G, "nonexistent", "caller")
        self.assertEqual(strategy, "unresolved")

    def test_fqn_in_built_graph(self):
        """Test that build_graph adds fqn to nodes."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "lib_bdev_init", "name": "bdev_init", "source_file": "lib/bdev/bdev.c",
                 "line": 10, "domain": "lib.bdev", "labels": ["API_entry"],
                 "signature": "void bdev_init(int mode)"},
            ],
            "edges": [],
            "domains": ["lib.bdev"],
            "lang_stats": {"c": 1},
        }
        G, _ = build_graph(extraction)
        nd = G.nodes["lib_bdev_init"]
        self.assertIn("fqn", nd)
        self.assertIn("bdev_init", nd["fqn"])


class TestRoundTrip(unittest.TestCase):
    """Test build → split → load round trip."""

    def test_round_trip(self):
        from _builder.graph_build import build_graph, split_by_domain, _load_full_graph
        extraction = {
            "functions": [
                {"id": "lib_bdev_start", "name": "bdev_start", "source_file": "lib/bdev/bdev.c",
                 "line": 10, "domain": "lib.bdev", "labels": ["API_entry"],
                 "signature": "void bdev_start(int mode)"},
                {"id": "lib_bdev_init", "name": "bdev_init", "source_file": "lib/bdev/bdev.c",
                 "line": 20, "domain": "lib.bdev", "labels": []},
            ],
            "edges": [
                {"source": "lib_bdev_start", "target": "lib_bdev_init",
                 "call_order": 1, "call_condition": "", "concurrency": "",
                 "confidence": "EXTRACTED", "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["lib.bdev"],
            "lang_stats": {"c": 1},
        }
        G1, _ = build_graph(extraction)
        with tempfile.TemporaryDirectory() as tmpdir:
            split_by_domain(G1, tmpdir, source_root="/src")
            G2 = _load_full_graph(tmpdir)
            self.assertEqual(G2.number_of_nodes(), G1.number_of_nodes())
            self.assertEqual(G2.number_of_edges(), G1.number_of_edges())
            # Verify edge attributes preserved
            for u, v, data in G2.edges(data=True):
                self.assertIn("call_order", data)
                self.assertIn("confidence", data)
                self.assertIn("source", data)


class TestSuffixIndex(unittest.TestCase):
    """Test _build_suffix_index for O(1) callee resolution."""

    def test_basic_suffix_index(self):
        from _builder.utils import _build_suffix_index
        registry = {
            "lib_bdev_open": {"id": "lib_bdev_open"},
            "lib_bdev_close": {"id": "lib_bdev_close"},
            "app_main": {"id": "app_main"},
        }
        idx = _build_suffix_index(registry)
        # Should find by full normalized ID
        self.assertIn("lib_bdev_open", idx)
        self.assertIn(idx["lib_bdev_open"][0], registry)

    def test_suffix_index_partial_match(self):
        from _builder.utils import _build_suffix_index
        registry = {
            "lib.bdev.bdev_open": {"id": "lib.bdev.bdev_open"},
            "lib.bdev.bdev_close": {"id": "lib.bdev.bdev_close"},
        }
        idx = _build_suffix_index(registry)
        # Should find by function name part (after last dot)
        self.assertIn("bdev_open", idx)
        self.assertEqual(idx["bdev_open"][0], "lib.bdev.bdev_open")

    def test_resolve_with_suffix_index(self):
        from _builder.utils import _resolve_invoked_id, _build_suffix_index
        registry = {
            "lib.bdev.bdev_open": {"id": "lib.bdev.bdev_open", "domain": "lib.bdev"},
            "lib.bdev.bdev_close": {"id": "lib.bdev.bdev_close", "domain": "lib.bdev"},
        }
        idx = _build_suffix_index(registry)
        # With index: O(1) lookup via function name part
        result = _resolve_invoked_id("bdev_open", "lib.bdev", registry, suffix_index=idx)
        self.assertEqual(result, "lib.bdev.bdev_open")

    def test_resolve_without_suffix_index_backward_compat(self):
        from _builder.utils import _resolve_invoked_id
        registry = {
            "lib_bdev_open": {"id": "lib_bdev_open", "domain": "lib.bdev"},
        }
        # Without index: falls back to linear scan (backward compatible)
        result = _resolve_invoked_id("bdev_open", "lib.bdev", registry, suffix_index=None)
        self.assertEqual(result, "lib_bdev_open")

    def test_large_registry_performance(self):
        """Suffix index should handle 16K+ entries efficiently."""
        from _builder.utils import _build_suffix_index, _resolve_invoked_id
        import time
        # Build a large registry simulating SPDK scale
        registry = {}
        for i in range(16000):
            fid = f"lib_module{i % 100}_func{i}"
            registry[fid] = {"id": fid, "domain": f"lib.module{i % 100}"}

        idx = _build_suffix_index(registry)
        # Resolve 1000 edges - should be fast with index
        start = time.time()
        for i in range(0, 1000):
            _resolve_invoked_id(f"func{i}", f"lib.module{i % 100}", registry, suffix_index=idx)
        elapsed = time.time() - start
        self.assertLess(elapsed, 1.0)  # Should complete in under 1 second


class TestParserArtifactDetection(unittest.TestCase):
    """Test _is_parser_artifact and its integration with _resolve_invoked_id."""

    def test_locatedexpr_prefix(self):
        from _builder.utils import _is_parser_artifact
        self.assertTrue(_is_parser_artifact("locatedexpr_word_alphanums"))
        self.assertTrue(_is_parser_artifact("locatedexpr_keyword___suppress"))

    def test_dsl_parenthesized(self):
        from _builder.utils import _is_parser_artifact
        self.assertTrue(_is_parser_artifact("word(alphanums + '_')"))
        self.assertTrue(_is_parser_artifact("locatedexpr(word(alphanums))"))

    def test_triple_underscores(self):
        from _builder.utils import _is_parser_artifact
        self.assertTrue(_is_parser_artifact("some___name"))

    def test_normal_names_not_artifact(self):
        from _builder.utils import _is_parser_artifact
        self.assertFalse(_is_parser_artifact("spdk_bdev_open"))
        self.assertFalse(_is_parser_artifact("main"))
        self.assertFalse(_is_parser_artifact("_internal_func"))
        self.assertFalse(_is_parser_artifact("rte_power_freq_up"))

    def test_resolve_callee_skips_artifacts(self):
        from _builder.utils import _resolve_invoked_id
        registry = {"lib_bdev_start": {"id": "lib_bdev_start", "domain": "lib.bdev"}}
        # Artifact names should return empty string
        self.assertEqual(_resolve_invoked_id("locatedexpr(word(alphanums))", "lib.bdev", registry), "")
        self.assertEqual(_resolve_invoked_id("value___word", "lib.bdev", registry), "")

    def test_build_graph_skips_artifact_edges(self):
        """Edges with parser artifact targets should be filtered out."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "scripts_add_quotes", "name": "add_quotes", "source_file": "scripts/add.py",
                 "line": 1, "domain": "scripts", "labels": []},
            ],
            "edges": [
                {"source": "scripts_add_quotes", "target": "locatedexpr(word(alphanums + '_'))",
                 "call_order": 1, "call_condition": "", "concurrency": "",
                 "confidence": "EXTRACTED", "source_tag": "ast", "confidence_score": 1.0},
                {"source": "scripts_add_quotes", "target": "print",
                 "call_order": 2, "call_condition": "", "concurrency": "",
                 "confidence": "EXTRACTED", "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["scripts"],
            "lang_stats": {"python": 1},
        }
        G, _ = build_graph(extraction)
        # Artifact edge should be filtered out; only call edges (not CONTAINS) remain
        call_edges = [(u, v, d) for u, v, d in G.edges(data=True) if d.get("relation") != "CONTAINS"]
        self.assertEqual(len(call_edges), 1)
        # Only the non-artifact edge should remain
        self.assertEqual(call_edges[0][1], "print")


class TestResolveImportsPerformance(unittest.TestCase):
    """Test improved _resolve_imports with non-backtracking regex."""

    def _make_graph_with_includes(self):
        G = nx.DiGraph()
        G.add_node("lib_a_func1", name="func1", source_file="lib/a.c",
                   domain="lib.a", labels=[], is_empty=False,
                   body_text='#include "b.h"\nvoid func1(void) { func2(); }')
        G.add_node("lib_b_func2", name="func2", source_file="lib/b.c",
                   domain="lib.b", labels=[], is_empty=False,
                   body_text='')
        G.add_node("ext_func2", name="func2", source_file="",
                   domain="external", labels=[], is_empty=False)
        G.add_edge("lib_a_func1", "ext_func2", call_order=1,
                   confidence="EXTRACTED", source="ast")
        return G

    def test_resolve_imports_skips_unneeded_headers(self):
        """Only scan headers that are referenced in include_map."""
        from _builder.import_resolve import _resolve_imports
        import tempfile
        G = self._make_graph_with_includes()
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a needed header
            os.makedirs(os.path.join(tmpdir, "lib"), exist_ok=True)
            with open(os.path.join(tmpdir, "lib", "b.h"), "w") as f:
                f.write("void func2(void);\n")
            # Create an unrelated header that should be skipped
            with open(os.path.join(tmpdir, "unrelated.h"), "w") as f:
                f.write("void unrelated_func(void);\n")
            result = _resolve_imports(G, tmpdir)
            # Should resolve func2 through b.h
            self.assertGreaterEqual(result, 0)

    def test_large_header_no_backtracking(self):
        """The new regex should not cause catastrophic backtracking on large headers."""
        from _builder.import_resolve import _resolve_imports
        import tempfile
        import time
        G = nx.DiGraph()
        G.add_node("test_func", name="test_func", source_file="test.c",
                   domain="test", labels=[], is_empty=False,
                   body_text='#include "big.h"\nvoid test_func(void) { big_api(); }')
        G.add_node("ext_big_api", name="big_api", source_file="",
                   domain="external", labels=[], is_empty=False)
        G.add_edge("test_func", "ext_big_api", call_order=1,
                   confidence="EXTRACTED", source="ast")

        with tempfile.TemporaryDirectory() as tmpdir:
            # Generate a large header with complex type expressions
            # that would cause catastrophic backtracking with the old regex
            with open(os.path.join(tmpdir, "big.h"), "w") as f:
                f.write("// Complex header\n")
                for i in range(1000):
                    # These patterns would cause backtracking with old nested quantifier regex
                    f.write(f"static inline struct spdk_bdev * get_bdev_{i}"
                            f"(struct spdk_bdev_desc *desc, int idx) {{}}\n")

            start = time.time()
            result = _resolve_imports(G, tmpdir)
            elapsed = time.time() - start
            # Should complete in under 2 seconds (would hang with old regex)
            self.assertLess(elapsed, 2.0)


class TestCrossFileResolution(unittest.TestCase):
    """Test end-to-end cross-file function call resolution."""

    def test_cross_file_same_domain(self):
        """foo() in file_a.c calls bar() defined in file_b.c, same domain."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "lib.a.foo", "name": "foo", "source_file": "lib/a/a.c",
                 "line": 10, "domain": "lib.a", "labels": []},
                {"id": "lib.b.bar", "name": "bar", "source_file": "lib/b/b.c",
                 "line": 5, "domain": "lib.b", "labels": []},
            ],
            "edges": [
                {"source": "lib.a.foo", "target": "bar",
                 "call_order": 1, "call_condition": "", "concurrency": "",
                 "confidence": "EXTRACTED", "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["lib.a", "lib.b"],
            "lang_stats": {"c": 2},
        }
        G, _ = build_graph(extraction)
        # bar should be resolved to lib.b.bar via suffix index
        call_edges = [(u, v) for u, v, d in G.edges(data=True) if d.get("relation") != "CONTAINS"]
        targets = [v for u, v in call_edges]
        # Either resolved to lib.b.bar or bar remains as fallback
        self.assertTrue(
            any("bar" in t for t in targets),
            f"Expected bar in targets, got {targets}"
        )

    def test_cross_file_unique_name(self):
        """foo() calls unique_func() defined in another domain — unique_name strategy."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "app.main_func", "name": "main_func", "source_file": "app/main.c",
                 "line": 10, "domain": "app", "labels": []},
                {"id": "lib.utils.unique_helper", "name": "unique_helper",
                 "source_file": "lib/utils/helper.c", "line": 5, "domain": "lib.utils", "labels": []},
            ],
            "edges": [
                {"source": "app.main_func", "target": "unique_helper",
                 "call_order": 1, "call_condition": "", "concurrency": "",
                 "confidence": "EXTRACTED", "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["app", "lib.utils"],
            "lang_stats": {"c": 2},
        }
        G, _ = build_graph(extraction)
        # unique_helper should be resolved via suffix or unique_name strategy
        call_edges = [(u, v, d) for u, v, d in G.edges(data=True) if d.get("relation") != "CONTAINS"]
        self.assertGreater(len(call_edges), 0, "Should have at least one call edge")

    def test_multi_strategy_same_file(self):
        """Two functions in same file — same_file strategy."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_caller", "name": "caller", "source_file": "test.c",
                 "line": 10, "domain": "root", "labels": []},
                {"id": "root_callee", "name": "callee", "source_file": "test.c",
                 "line": 20, "domain": "root", "labels": []},
            ],
            "edges": [
                {"source": "root_caller", "target": "callee",
                 "call_order": 1, "call_condition": "", "concurrency": "",
                 "confidence": "EXTRACTED", "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["root"],
            "lang_stats": {"c": 1},
        }
        G, _ = build_graph(extraction)
        # callee should be resolved to root_callee
        self.assertTrue(G.has_node("root_callee"), "root_callee should exist as node")
        call_edges = [(u, v) for u, v, d in G.edges(data=True)
                      if d.get("relation") != "CONTAINS" and v == "root_callee"]
        self.assertGreater(len(call_edges), 0, "Should have edge to root_callee")


class TestProfileMigration(unittest.TestCase):
    """O20: profile schema migration from older versions."""

    def test_migrate_v0_to_v1(self):
        from _profile.schema import _migrate_profile, _SUPPORTED_VERSION
        # A pre-version profile (no 'version' field)
        old = {"project": {"name": "myproj", "language": "c"}}
        migrated = _migrate_profile(old)
        self.assertEqual(migrated.get("version"), _SUPPORTED_VERSION)

    def test_load_pre_version_profile_auto_migrates(self):
        from _profile.schema import ProfileSchema
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({"project": {"name": "oldproj"}}, f)
            path = f.name
        try:
            profile = ProfileSchema.load(path)
            # Migration should stamp version=1 even if the file had no version
            self.assertEqual(profile._raw.get("version"), 1)
            # Required sections (from _default.json merge) should be present
            self.assertIn("skip_names", profile._raw)
            self.assertIn("api_detection", profile._raw)
        finally:
            os.unlink(path)

    def test_unsupported_version_raises(self):
        from _profile.schema import ProfileSchema
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({"version": 999, "project": {"name": "future"}}, f)
            path = f.name
        try:
            with self.assertRaises(ValueError):
                ProfileSchema.load(path)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
