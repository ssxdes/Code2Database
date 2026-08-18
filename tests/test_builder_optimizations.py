#!/usr/bin/env python3
"""Tests for P1-P5 builder optimizations in graph_build.py."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import networkx as nx


class TestP1MacroBridging(unittest.TestCase):
    """P1: Macro-to-function bridging (__name convention)."""

    def _build_with_macro_pair(self, macro_sf="", impl_sf="impl.c",
                               macro_domain="fs.ext4", impl_domain="fs.ext4"):
        """Build a graph with a macro wrapper and __implementation function."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_module_func", "name": "module_func",
                 "source_file": macro_sf, "line": 10,
                 "domain": macro_domain, "labels": []},
                {"id": "root___module_func", "name": "__module_func",
                 "source_file": impl_sf, "line": 20,
                 "domain": impl_domain, "labels": []},
            ],
            "edges": [],
            "domains": ["root"],
            "lang_stats": {"c": 2},
        }
        return build_graph(extraction)

    def test_macro_bridge_no_source_file(self):
        """Auto-created node with no source_file bridges to __implementation."""
        G, _ = self._build_with_macro_pair(macro_sf="")
        # Should have an edge from module_func to __module_func
        self.assertTrue(G.has_edge("root_module_func", "root___module_func"),
                        "Expected macro_bridge edge from module_func to __module_func")
        edge = G.edges["root_module_func", "root___module_func"]
        self.assertEqual(edge.get("concurrency"), "macro_bridge")
        self.assertEqual(edge.get("confidence"), "INFERRED")
        self.assertEqual(edge.get("source"), "macro_bridge")

    def test_macro_bridge_header_source(self):
        """Macro node with .h source_file bridges to __implementation."""
        G, _ = self._build_with_macro_pair(macro_sf="module.h")
        self.assertTrue(G.has_edge("root_module_func", "root___module_func"),
                        "Expected macro_bridge edge for .h source")

    def test_no_bridge_for_c_source(self):
        """Node with .c source_file does NOT get macro bridge."""
        G, _ = self._build_with_macro_pair(macro_sf="module.c")
        self.assertFalse(G.has_edge("root_module_func", "root___module_func"),
                         "Should not bridge .c source functions")

    def test_no_bridge_different_domain(self):
        """Macro and implementation in different top-level domains → no bridge."""
        G, _ = self._build_with_macro_pair(
            macro_domain="fs.ext4", impl_domain="drivers.net")
        self.assertFalse(G.has_edge("root_module_func", "root___module_func"),
                         "Should not bridge across different top-level domains")

    def test_no_bridge_double_underscore_source(self):
        """Node starting with __ is the implementation, not the macro."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root___journal_start", "name": "__journal_start",
                 "source_file": "", "line": 10, "domain": "root", "labels": []},
                {"id": "root____journal_start", "name": "___journal_start",
                 "source_file": "impl.c", "line": 20, "domain": "root", "labels": []},
            ],
            "edges": [],
            "domains": ["root"],
            "lang_stats": {"c": 2},
        }
        G, _ = build_graph(extraction)
        # __journal_start should NOT bridge to ___journal_start
        self.assertFalse(G.has_edge("root___journal_start", "root____journal_start"),
                         "Double-underscore node should not be bridged FROM")

    def test_no_duplicate_bridge(self):
        """If edge already exists, no duplicate macro_bridge edge."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_journal_start", "name": "journal_start",
                 "source_file": "", "line": 10, "domain": "root", "labels": []},
                {"id": "root___journal_start", "name": "__journal_start",
                 "source_file": "impl.c", "line": 20, "domain": "root", "labels": []},
            ],
            "edges": [
                {"source": "root_journal_start", "target": "root___journal_start",
                 "call_order": 1, "call_condition": "",
                 "concurrency": "INVOKES", "confidence": "EXTRACTED",
                 "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["root"],
            "lang_stats": {"c": 2},
        }
        G, _ = build_graph(extraction)
        # Count edges between the two nodes — should be exactly 1
        edge_count = sum(1 for u, v in G.edges()
                         if u == "root_journal_start" and v == "root___journal_start")
        self.assertEqual(edge_count, 1, "Should not create duplicate edges")


class TestP2InlineFnPtrDispatch(unittest.TestCase):
    """P2: Inline fn_ptr_call flattening and inline wrapper dispatch."""

    def test_inline_wrapper_call_pattern(self):
        """call_* named auto-created nodes get vtable dispatch edges."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_caller", "name": "caller",
                 "source_file": "caller.c", "line": 10,
                 "domain": "root", "labels": []},
                {"id": "root_call_method", "name": "call_method",
                 "source_file": "", "line": 0,
                 "domain": "root", "labels": []},
                {"id": "root_impl_method", "name": "impl_method",
                 "source_file": "impl.c", "line": 20,
                 "domain": "root", "labels": []},
            ],
            "edges": [
                {"source": "root_caller", "target": "root_call_method",
                 "call_order": 1, "call_condition": "",
                 "concurrency": "INVOKES", "confidence": "EXTRACTED",
                 "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["root"],
            "lang_stats": {"c": 2},
            "vtable_registrations": [
                {"struct_type": "my_ops", "var_name": "ops",
                 "source_file": "impl.c",
                 "registrations": [
                     {"field": "method", "func_name": "impl_method"}
                 ]},
            ],
        }
        G, _ = build_graph(extraction)
        # Should have a vtable_dispatch edge from caller to impl_method
        has_dispatch = any(
            u == "root_caller" and v == "root_impl_method"
            and d.get("source") == "inline_wrapper_dispatch"
            for u, v, d in G.edges(data=True)
        )
        self.assertTrue(has_dispatch,
                        "Expected inline_wrapper_dispatch edge from caller to impl_method")

    def test_inline_wrapper_with_h_source(self):
        """call_* node with .h source_file still gets dispatch edges."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_caller", "name": "caller",
                 "source_file": "caller.c", "line": 10,
                 "domain": "root", "labels": []},
                {"id": "root_call_method", "name": "call_method",
                 "source_file": "wrapper.h", "line": 5,
                 "domain": "root", "labels": []},
                {"id": "root_impl_method", "name": "impl_method",
                 "source_file": "impl.c", "line": 20,
                 "domain": "root", "labels": []},
            ],
            "edges": [
                {"source": "root_caller", "target": "root_call_method",
                 "call_order": 1, "call_condition": "",
                 "concurrency": "INVOKES", "confidence": "EXTRACTED",
                 "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["root"],
            "lang_stats": {"c": 2},
            "vtable_registrations": [
                {"struct_type": "my_ops", "var_name": "ops",
                 "source_file": "impl.c",
                 "registrations": [
                     {"field": "method", "func_name": "impl_method"}
                 ]},
            ],
        }
        G, _ = build_graph(extraction)
        has_dispatch = any(
            u == "root_caller" and v == "root_impl_method"
            and d.get("source") == "inline_wrapper_dispatch"
            for u, v, d in G.edges(data=True)
        )
        self.assertTrue(has_dispatch,
                        "Expected dispatch edge even with .h source_file")

    def test_no_dispatch_for_c_source_wrapper(self):
        """call_* node with .c source_file does NOT get inline dispatch."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_caller", "name": "caller",
                 "source_file": "caller.c", "line": 10,
                 "domain": "root", "labels": []},
                {"id": "root_call_method", "name": "call_method",
                 "source_file": "wrapper.c", "line": 5,
                 "domain": "root", "labels": []},
                {"id": "root_impl_method", "name": "impl_method",
                 "source_file": "impl.c", "line": 20,
                 "domain": "root", "labels": []},
            ],
            "edges": [
                {"source": "root_caller", "target": "root_call_method",
                 "call_order": 1, "call_condition": "",
                 "concurrency": "INVOKES", "confidence": "EXTRACTED",
                 "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["root"],
            "lang_stats": {"c": 2},
            "vtable_registrations": [
                {"struct_type": "my_ops", "var_name": "ops",
                 "source_file": "impl.c",
                 "registrations": [
                     {"field": "method", "func_name": "impl_method"}
                 ]},
            ],
        }
        G, _ = build_graph(extraction)
        has_dispatch = any(
            u == "root_caller" and v == "root_impl_method"
            and d.get("source") == "inline_wrapper_dispatch"
            for u, v, d in G.edges(data=True)
        )
        self.assertFalse(has_dispatch,
                         "Should not dispatch from .c source wrapper")

    def test_only_calls_predecessors_are_callers(self):
        """Inline wrapper dispatch only considers INVOKES edges, not other types."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_cb_setter", "name": "cb_setter",
                 "source_file": "setter.c", "line": 10,
                 "domain": "root", "labels": []},
                {"id": "root_caller", "name": "caller",
                 "source_file": "caller.c", "line": 20,
                 "domain": "root", "labels": []},
                {"id": "root_call_method", "name": "call_method",
                 "source_file": "", "line": 0,
                 "domain": "root", "labels": []},
                {"id": "root_impl_method", "name": "impl_method",
                 "source_file": "impl.c", "line": 30,
                 "domain": "root", "labels": []},
            ],
            "edges": [
                # cb_setter sets call_method as callback (not INVOKES)
                {"source": "root_cb_setter", "target": "root_call_method",
                 "call_order": 1, "call_condition": "",
                 "concurrency": "CALLBACK_ARG", "confidence": "EXTRACTED",
                 "source_tag": "ast", "confidence_score": 1.0},
                # caller actually calls call_method (INVOKES)
                {"source": "root_caller", "target": "root_call_method",
                 "call_order": 2, "call_condition": "",
                 "concurrency": "INVOKES", "confidence": "EXTRACTED",
                 "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["root"],
            "lang_stats": {"c": 2},
            "vtable_registrations": [
                {"struct_type": "my_ops", "var_name": "ops",
                 "source_file": "impl.c",
                 "registrations": [
                     {"field": "method", "func_name": "impl_method"}
                 ]},
            ],
        }
        G, _ = build_graph(extraction)
        # cb_setter should NOT get dispatch edges (only CALLBACK_ARG)
        dispatch_from_setter = any(
            u == "root_cb_setter" and d.get("source") == "inline_wrapper_dispatch"
            for u, v, d in G.edges(data=True)
        )
        self.assertFalse(dispatch_from_setter,
                         "CALLBACK_ARG predecessor should not be treated as caller")
        # caller should get dispatch edges (INVOKES)
        dispatch_from_caller = any(
            u == "root_caller" and d.get("source") == "inline_wrapper_dispatch"
            for u, v, d in G.edges(data=True)
        )
        self.assertTrue(dispatch_from_caller,
                        "INVOKES predecessor should get dispatch edges")


class TestP4ConditionalParentEdges(unittest.TestCase):
    """P4: Conditional node parent edges (__cond_N suffix)."""

    def test_cond_suffix_parent_edge(self):
        """Conditional node func__cond_0 gets edge from parent func."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_f", "name": "f", "source_file": "f.c",
                 "line": 10, "domain": "root", "labels": []},
                {"id": "root_f__cond_0", "name": "<conditional:if(x)>",
                 "source_file": "", "line": 11,
                 "domain": "root", "labels": []},
                {"id": "root_g", "name": "g", "source_file": "f.c",
                 "line": 12, "domain": "root", "labels": []},
            ],
            "edges": [
                {"source": "root_f__cond_0", "target": "root_g",
                 "call_order": 1, "call_condition": "if(x)",
                 "concurrency": "", "confidence": "EXTRACTED",
                 "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["root"],
            "lang_stats": {"c": 2},
        }
        G, _ = build_graph(extraction)
        # Should have a conditional_entry edge from f to f__cond_0
        has_parent = any(
            u == "root_f" and v == "root_f__cond_0"
            and d.get("concurrency") == "conditional_entry"
            for u, v, d in G.edges(data=True)
        )
        self.assertTrue(has_parent,
                        "Expected conditional_entry edge from f to f__cond_0")

    def test_cond_else_suffix(self):
        """Conditional node func__cond_0_else gets parent edge."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_f", "name": "f", "source_file": "f.c",
                 "line": 10, "domain": "root", "labels": []},
                {"id": "root_f__cond_0_else", "name": "<conditional:else>",
                 "source_file": "", "line": 13,
                 "domain": "root", "labels": []},
                {"id": "root_h", "name": "h", "source_file": "f.c",
                 "line": 14, "domain": "root", "labels": []},
            ],
            "edges": [
                {"source": "root_f__cond_0_else", "target": "root_h",
                 "call_order": 1, "call_condition": "else",
                 "concurrency": "", "confidence": "EXTRACTED",
                 "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["root"],
            "lang_stats": {"c": 2},
        }
        G, _ = build_graph(extraction)
        has_parent = any(
            u == "root_f" and v == "root_f__cond_0_else"
            and d.get("concurrency") == "conditional_entry"
            for u, v, d in G.edges(data=True)
        )
        self.assertTrue(has_parent,
                        "Expected conditional_entry edge for __cond_N_else")

    def test_no_duplicate_conditional_parent(self):
        """If edge already exists, no duplicate conditional_entry edge."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_f", "name": "f", "source_file": "f.c",
                 "line": 10, "domain": "root", "labels": []},
                {"id": "root_f__cond_0", "name": "<conditional:if(x)>",
                 "source_file": "", "line": 11,
                 "domain": "root", "labels": []},
            ],
            "edges": [
                {"source": "root_f", "target": "root_f__cond_0",
                 "call_order": 1, "call_condition": "",
                 "concurrency": "INVOKES", "confidence": "EXTRACTED",
                 "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["root"],
            "lang_stats": {"c": 1},
        }
        G, _ = build_graph(extraction)
        # Should have exactly 1 edge from f to f__cond_0 (the INVOKES edge)
        edge_count = sum(1 for u, v in G.edges()
                         if u == "root_f" and v == "root_f__cond_0")
        self.assertEqual(edge_count, 1, "Should not duplicate conditional parent edge")

    def test_named_conditional_with_empty_source_file(self):
        """<conditional:...> node with empty source_file gets parent via ID regex."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_my_func", "name": "my_func",
                 "source_file": "my_func.c", "line": 10,
                 "domain": "root", "labels": []},
                {"id": "root_my_func__cond_0", "name": "<conditional:if(ptr)>",
                 "source_file": "", "line": 11,
                 "domain": "root", "labels": []},
                {"id": "root_target", "name": "target",
                 "source_file": "my_func.c", "line": 12,
                 "domain": "root", "labels": []},
            ],
            "edges": [
                {"source": "root_my_func__cond_0", "target": "root_target",
                 "call_order": 1, "call_condition": "if(ptr)",
                 "concurrency": "", "confidence": "EXTRACTED",
                 "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["root"],
            "lang_stats": {"c": 2},
        }
        G, _ = build_graph(extraction)
        has_parent = any(
            u == "root_my_func" and v == "root_my_func__cond_0"
            and d.get("concurrency") == "conditional_entry"
            for u, v, d in G.edges(data=True)
        )
        self.assertTrue(has_parent,
                        "Expected parent edge via ID regex for empty-source conditional")


class TestP5VtableDispatchCap(unittest.TestCase):
    """P5: vtable_dispatch cap enforcement."""

    def test_vtable_dispatch_cap_truncates(self):
        """When more than cap registrations exist, only cap edges are created."""
        from _builder.graph_build import build_graph
        # Create 55 implementations (exceeds cap of 50)
        funcs = [
            {"id": f"root_caller", "name": "caller",
             "source_file": "caller.c", "line": 10, "domain": "root", "labels": []},
        ]
        regs = []
        for i in range(55):
            funcs.append({
                "id": f"root_impl_{i}", "name": f"impl_{i}",
                "source_file": f"impl_{i}.c", "line": 20 + i,
                "domain": "root", "labels": []
            })
            regs.append({"field": "method", "func_name": f"impl_{i}"})

        extraction = {
            "functions": funcs,
            "edges": [],
            "domains": ["root"],
            "lang_stats": {"c": 56},
            "fn_ptr_calls": {
                "caller": [{"field_name": "method", "struct_chain": "obj->ops"}]
            },
            "vtable_registrations": [
                {"struct_type": "my_ops", "var_name": "ops",
                 "source_file": "ops.c", "registrations": regs},
            ],
        }
        G, _ = build_graph(extraction)
        dispatch_edges = [
            (u, v, d) for u, v, d in G.edges(data=True)
            if u == "root_caller" and d.get("concurrency") == "vtable_dispatch"
        ]
        self.assertLessEqual(len(dispatch_edges), 50,
                             "Should not exceed vtable_dispatch cap")

    def test_conditional_dispatch_truncates_not_skips(self):
        """Conditional dispatch with >50 targets truncates, not skips entirely."""
        from _builder.graph_build import build_graph
        # Build extraction with a fn_ptr_call in a conditional
        # that has 55 possible dispatch targets
        funcs = [
            {"id": "root_f", "name": "f", "source_file": "f.c",
             "line": 10, "domain": "root", "labels": []},
        ]
        regs = []
        for i in range(55):
            funcs.append({
                "id": f"root_impl_{i}", "name": f"impl_{i}",
                "source_file": f"impl_{i}.c", "line": 20 + i,
                "domain": "root", "labels": []
            })
            regs.append({"field": "method", "func_name": f"impl_{i}"})

        extraction = {
            "functions": funcs,
            "edges": [],
            "domains": ["root"],
            "lang_stats": {"c": 56},
            "fn_ptr_calls": {
                "f": [{"field_name": "method", "struct_chain": "obj->ops"}]
            },
            "vtable_registrations": [
                {"struct_type": "my_ops", "var_name": "ops",
                 "source_file": "ops.c", "registrations": regs},
            ],
        }
        G, _ = build_graph(extraction)
        # Should have dispatch edges (not zero — the old code would skip entirely)
        dispatch_edges = [
            (u, v, d) for u, v, d in G.edges(data=True)
            if u == "root_f" and d.get("concurrency") in ("vtable_dispatch", "field_dispatch")
        ]
        self.assertGreater(len(dispatch_edges), 0,
                           "Conditional dispatch should not skip entirely when >50 targets")
        self.assertLessEqual(len(dispatch_edges), 50,
                             "Should truncate to cap, not exceed it")


class TestP2AInlineFnPtrFlattening(unittest.TestCase):
    """P2 Part A: Static inline fn_ptr_call flattening."""

    def test_inline_fn_ptr_call_flattened(self):
        """When caller has no node_id, edges go to callers of the inline."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_real_caller", "name": "real_caller",
                 "source_file": "caller.c", "line": 10,
                 "domain": "root", "labels": []},
                {"id": "root_impl_method", "name": "impl_method",
                 "source_file": "impl.c", "line": 20,
                 "domain": "root", "labels": []},
            ],
            "edges": [
                # real_caller calls the inline wrapper (not a node in the graph)
                {"source": "root_real_caller", "target": "inline_wrapper",
                 "call_order": 1, "call_condition": "",
                 "concurrency": "INVOKES", "confidence": "EXTRACTED",
                 "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["root"],
            "lang_stats": {"c": 2},
            "fn_ptr_calls": {
                # inline_wrapper has fn_ptr_call but no node_id in the graph
                "inline_wrapper": [
                    {"field_name": "method", "struct_chain": "obj->ops"}
                ]
            },
            "vtable_registrations": [
                {"struct_type": "my_ops", "var_name": "ops",
                 "source_file": "impl.c",
                 "registrations": [
                     {"field": "method", "func_name": "impl_method"}
                 ]},
            ],
        }
        G, _ = build_graph(extraction)
        # Should have a vtable_dispatch edge from real_caller to impl_method
        has_dispatch = any(
            u == "root_real_caller" and v == "root_impl_method"
            and d.get("source") == "vtable_analysis_inline"
            for u, v, d in G.edges(data=True)
        )
        self.assertTrue(has_dispatch,
                        "Expected flattened vtable_dispatch from real_caller to impl_method")


class TestP1ExtendedMacroBridging(unittest.TestCase):
    """Extended P1 tests for second-level domain and edge cases."""

    def test_no_bridge_different_second_level_domain(self):
        """Same first-level domain but different second-level: no bridge."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "fs_ext4_journal_start", "name": "journal_start",
                 "source_file": "", "line": 10,
                 "domain": "fs.ext4", "labels": []},
                {"id": "fs_btrfs___journal_start", "name": "__journal_start",
                 "source_file": "btrfs.c", "line": 20,
                 "domain": "fs.btrfs", "labels": []},
            ],
            "edges": [],
            "domains": ["root"],
            "lang_stats": {"c": 2},
        }
        G, _ = build_graph(extraction)
        # Same first-level (fs) but different second-level (ext4 vs btrfs)
        self.assertFalse(G.has_edge("fs_ext4_journal_start", "fs_btrfs___journal_start"),
                         "No bridge across different second-level domains")

    def test_no_bridge_when_no_dunder_pair_exists(self):
        """A function with no __ counterpart: no false positive bridge."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_standalone_func", "name": "standalone_func",
                 "source_file": "", "line": 10,
                 "domain": "fs.ext4", "labels": []},
                {"id": "root_other_func", "name": "other_func",
                 "source_file": "impl.c", "line": 20,
                 "domain": "fs.ext4", "labels": []},
            ],
            "edges": [],
            "domains": ["root"],
            "lang_stats": {"c": 2},
        }
        G, _ = build_graph(extraction)
        # standalone_func has no __standalone_func, so no bridge should be created
        self.assertFalse(G.has_edge("root_standalone_func", "root_other_func"))

    def test_no_bridge_empty_domain(self):
        """No bridge when one function has empty domain."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_func_a", "name": "func_a",
                 "source_file": "", "line": 10,
                 "domain": "", "labels": []},
                {"id": "root___func_a", "name": "__func_a",
                 "source_file": "impl.c", "line": 20,
                 "domain": "fs.ext4", "labels": []},
            ],
            "edges": [],
            "domains": ["root"],
            "lang_stats": {"c": 2},
        }
        G, _ = build_graph(extraction)
        self.assertFalse(G.has_edge("root_func_a", "root___func_a"),
                         "No bridge when one domain is empty")


class TestP2BExtendedInlineWrapper(unittest.TestCase):
    """Extended P2B tests for invoke_* pattern and case-insensitive matching."""

    def test_invoke_pattern_creates_dispatch(self):
        """invoke_method with no source_file creates vtable dispatch edges."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_caller_func", "name": "caller_func",
                 "source_file": "main.c", "line": 10,
                 "domain": "drivers.net", "labels": []},
                {"id": "root_invoke_read", "name": "invoke_read",
                 "source_file": "", "line": 0,
                 "domain": "drivers.net", "labels": []},
                {"id": "root_impl_read", "name": "impl_read",
                 "source_file": "driver.c", "line": 30,
                 "domain": "drivers.net", "labels": []},
            ],
            "edges": [
                {"source": "root_caller_func", "target": "root_invoke_read",
                 "call_order": 1, "call_condition": "",
                 "concurrency": "INVOKES", "confidence": "EXTRACTED",
                 "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["root"],
            "lang_stats": {"c": 3},
            "vtable_registrations": [
                {"struct_type": "net_ops", "var_name": "ops",
                 "source_file": "driver.c",
                 "registrations": [
                     {"field": "read", "func_name": "impl_read"}
                 ]},
            ],
        }
        G, _ = build_graph(extraction)
        has_dispatch = any(
            u == "root_caller_func" and v == "root_impl_read"
            and d.get("source") == "inline_wrapper_dispatch"
            for u, v, d in G.edges(data=True)
        )
        self.assertTrue(has_dispatch,
                        "invoke_* pattern should create inline_wrapper_dispatch edge")

    def test_dunder_invoke_pattern(self):
        """__invoke_method with no source_file creates vtable dispatch edges."""
        from _builder.graph_build import build_graph
        # Note: node ID must NOT contain '___' (triple underscore)
        # as that triggers the parser artifact filter
        extraction = {
            "functions": [
                {"id": "root_caller_func", "name": "caller_func",
                 "source_file": "main.c", "line": 10,
                 "domain": "drivers_net", "labels": []},
                {"id": "root_dunder_invoke_write", "name": "__invoke_write",
                 "source_file": "", "line": 0,
                 "domain": "drivers_net", "labels": []},
                {"id": "drivers_net_impl_write", "name": "impl_write",
                 "source_file": "driver.c", "line": 30,
                 "domain": "drivers_net", "labels": []},
            ],
            "edges": [
                {"source": "root_caller_func", "target": "root_dunder_invoke_write",
                 "call_order": 1, "call_condition": "",
                 "concurrency": "INVOKES", "confidence": "EXTRACTED",
                 "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["root"],
            "lang_stats": {"c": 3},
            "vtable_registrations": [
                {"struct_type": "net_ops", "var_name": "ops",
                 "source_file": "driver.c",
                 "registrations": [
                     {"field": "write", "func_name": "impl_write"}
                 ]},
            ],
        }
        G, _ = build_graph(extraction)
        has_dispatch = any(
            u == "root_caller_func" and d.get("source") == "inline_wrapper_dispatch"
            for u, v, d in G.edges(data=True)
        )
        self.assertTrue(has_dispatch,
                        "__invoke_* pattern should create inline_wrapper_dispatch edge")

    def test_case_insensitive_vtable_matching(self):
        """call_Method (capital M) matches vtable field 'method' (lowercase)."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_caller_func", "name": "caller_func",
                 "source_file": "main.c", "line": 10,
                 "domain": "drivers.net", "labels": []},
                {"id": "root_call_Method", "name": "call_Method",
                 "source_file": "", "line": 0,
                 "domain": "drivers.net", "labels": []},
                {"id": "root_impl_method", "name": "impl_method",
                 "source_file": "driver.c", "line": 30,
                 "domain": "drivers.net", "labels": []},
            ],
            "edges": [
                {"source": "root_caller_func", "target": "root_call_Method",
                 "call_order": 1, "call_condition": "",
                 "concurrency": "INVOKES", "confidence": "EXTRACTED",
                 "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["root"],
            "lang_stats": {"c": 3},
            "vtable_registrations": [
                {"struct_type": "net_ops", "var_name": "ops",
                 "source_file": "driver.c",
                 "registrations": [
                     {"field": "method", "func_name": "impl_method"}
                 ]},
            ],
        }
        G, _ = build_graph(extraction)
        has_dispatch = any(
            u == "root_caller_func" and v == "root_impl_method"
            and d.get("source") == "inline_wrapper_dispatch"
            for u, v, d in G.edges(data=True)
        )
        self.assertTrue(has_dispatch,
                        "Case-insensitive: call_Method should match 'method' field")

    def test_lazy_vtable_index_with_no_fn_ptr_calls(self):
        """P2B works when vtable_registrations exist but fn_ptr_calls is empty."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_caller_func", "name": "caller_func",
                 "source_file": "main.c", "line": 10,
                 "domain": "drivers.net", "labels": []},
                {"id": "root_call_ioctl", "name": "call_ioctl",
                 "source_file": "", "line": 0,
                 "domain": "drivers.net", "labels": []},
                {"id": "root_impl_ioctl", "name": "impl_ioctl",
                 "source_file": "driver.c", "line": 30,
                 "domain": "drivers.net", "labels": []},
            ],
            "edges": [
                {"source": "root_caller_func", "target": "root_call_ioctl",
                 "call_order": 1, "call_condition": "",
                 "concurrency": "INVOKES", "confidence": "EXTRACTED",
                 "source_tag": "ast", "confidence_score": 1.0},
            ],
            "domains": ["root"],
            "lang_stats": {"c": 3},
            "vtable_registrations": [
                {"struct_type": "net_ops", "var_name": "ops",
                 "source_file": "driver.c",
                 "registrations": [
                     {"field": "ioctl", "func_name": "impl_ioctl"}
                 ]},
            ],
            # No fn_ptr_calls — triggers lazy vtable_index build
            "fn_ptr_calls": {},
        }
        G, _ = build_graph(extraction)
        has_dispatch = any(
            u == "root_caller_func" and v == "root_impl_ioctl"
            and d.get("source") == "inline_wrapper_dispatch"
            for u, v, d in G.edges(data=True)
        )
        self.assertTrue(has_dispatch,
                        "P2B should work with lazy vtable_index when fn_ptr_calls is empty")


class TestP3ReturnExpressionCallExtraction(unittest.TestCase):
    """P3: Verify that calls in return expressions are extracted."""

    def test_return_call_c_scanner(self):
        """return func_call() is extracted as a call edge."""
        from _scanner.c_scanner import CTreeSitterScanner
        import tempfile
        scanner = CTreeSitterScanner(is_cpp=False)
        code = """
int get_value(void) {
    return compute_value();
}
"""
        with tempfile.NamedTemporaryFile(suffix='.c', mode='w', delete=False) as f:
            f.write(code)
            f.flush()
            result = scanner.scan_file(f.name, source_root=os.path.dirname(f.name))
        os.unlink(f.name)
        edges = result["edges"]
        callees = [e.get("target", "") for e in edges]
        self.assertIn("compute_value", callees,
                      "return func_call() should be extracted as a call edge")

    def test_return_call_cpp_scanner(self):
        """return method() is extracted in C++ scanner."""
        from _scanner.c_scanner import CTreeSitterScanner
        import tempfile
        scanner = CTreeSitterScanner(is_cpp=True)
        code = """
int getValue() {
    return compute();
}
"""
        with tempfile.NamedTemporaryFile(suffix='.cpp', mode='w', delete=False) as f:
            f.write(code)
            f.flush()
            result = scanner.scan_file(f.name, source_root=os.path.dirname(f.name))
        os.unlink(f.name)
        edges = result["edges"]
        callees = [e.get("target", "") for e in edges]
        self.assertIn("compute", callees,
                      "return method() should be extracted in C++")


class TestP4ExtendedConditionalParent(unittest.TestCase):
    """Extended P4 tests for source_file fallback and missing parent."""

    def test_conditional_with_source_file_fallback(self):
        """<conditional:...> node with source_file uses Strategy 2 when ID regex fails."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_my_func", "name": "my_func",
                 "source_file": "test.c", "line": 10,
                 "domain": "test", "labels": []},
                # A proper __cond_N child exists for the parent
                {"id": "root_my_func__cond_0", "name": "<conditional:err_check>",
                 "source_file": "", "line": 12,
                 "domain": "test", "labels": [], "is_empty": True,
                 "condition": "err_check"},
                # A <conditional:...> node with source_file but non-matching ID
                # (no __cond_N suffix) — triggers Strategy 2 fallback
                {"id": "root_alt_cond_check", "name": "<conditional:err_check>",
                 "source_file": "test.c", "line": 15,
                 "domain": "test", "labels": [], "is_empty": True,
                 "condition": "err_check"},
            ],
            "edges": [],
            "domains": ["test"],
            "lang_stats": {"c": 3},
        }
        G, _ = build_graph(extraction)
        # root_my_func should have edges to both conditional nodes
        # cond_0 gets edge via Strategy 1 (ID regex)
        self.assertTrue(G.has_edge("root_my_func", "root_my_func__cond_0"),
                        "cond_0 should get parent edge via ID regex")
        # alt_cond_check gets edge via Strategy 2 (source_file match + cond_0 cross-ref)
        has_alt_edge = any(
            u == "root_my_func" and v == "root_alt_cond_check"
            for u, v, d in G.edges(data=True)
        )
        self.assertTrue(has_alt_edge,
                        "alt_cond should get parent edge via source_file fallback")

    def test_conditional_no_matching_parent(self):
        """<conditional:...> node with no matching parent: no edge created."""
        from _builder.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_other_func", "name": "other_func",
                 "source_file": "other.c", "line": 10,
                 "domain": "test", "labels": []},
                # Conditional node in different file — no parent match
                {"id": "root_orphan_cond", "name": "<conditional:check>",
                 "source_file": "orphan.c", "line": 5,
                 "domain": "test", "labels": [], "is_empty": True,
                 "condition": "check"},
            ],
            "edges": [],
            "domains": ["test"],
            "lang_stats": {"c": 2},
        }
        G, _ = build_graph(extraction)
        # No edge from other_func to orphan_cond
        self.assertFalse(G.has_edge("root_other_func", "root_orphan_cond"),
                         "No edge when no parent matches")


class TestP5MacroDispatchCap(unittest.TestCase):
    """P5: macro_dispatch cap enforcement."""

    def test_macro_dispatch_respects_cap(self):
        """macro_dispatch edges are capped at _MAX_VTABLE_DISPATCH_PER_CALL."""
        from _builder.graph_build import build_graph
        # Create 60 registrations for the same macro
        regs = []
        for i in range(60):
            regs.append({
                "struct_var": f"driver_{i}_probe",
                "macro_name": "MODULE_DRIVER",
                "source_file": f"driver_{i}.c",
                "line": 1 + i,
            })
        # Create dispatcher function + 60 handler functions
        funcs = [
            {"id": "root_dispatch_caller", "name": "dispatch_caller",
             "source_file": "core.c", "line": 1,
             "domain": "drivers.net", "labels": []},
        ]
        for i in range(60):
            funcs.append({
                "id": f"root_driver_{i}_probe", "name": f"driver_{i}_probe",
                "source_file": f"driver_{i}.c", "line": 5 + i,
                "domain": "drivers.net", "labels": [],
            })
        extraction = {
            "functions": funcs,
            "edges": [],
            "domains": ["root"],
            "lang_stats": {"c": 61},
            "macro_registrations": regs,
        }
        G, _ = build_graph(extraction, profile={
            "macro_dispatch": {
                "registration_macros": [
                    {
                        "macro_name": "MODULE_DRIVER",
                        "handler_arg_index": 0,
                        "dispatch_caller": "dispatch_caller",
                    }
                ]
            }
        })
        # Count macro_dispatch edges from dispatch_caller
        macro_edges = [
            (u, v, d) for u, v, d in G.edges(data=True)
            if u == "root_dispatch_caller" and d.get("source") == "macro_dispatch"
        ]
        # Should be capped at _MAX_VTABLE_DISPATCH_PER_CALL (50)
        self.assertLessEqual(len(macro_edges), 50,
                             "macro_dispatch should be capped at 50")


if __name__ == "__main__":
    unittest.main()
