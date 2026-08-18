#!/usr/bin/env python3
"""Tests for P8: goto statement extraction in C/C++ and Go scanners."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))


class TestCGotoExtraction(unittest.TestCase):
    """Test C/C++ scanner extraction of goto statements."""

    def _scan_c(self, code: str):
        """Scan C code and return result dict."""
        from _scanner.c_scanner import CTreeSitterScanner
        scanner = CTreeSitterScanner(is_cpp=False)
        with tempfile.NamedTemporaryFile(suffix='.c', mode='w', delete=False) as f:
            f.write(code)
            f.flush()
            result = scanner.scan_file(f.name, source_root=os.path.dirname(f.name))
        os.unlink(f.name)
        return result

    def test_backward_goto_detected(self):
        """goto to a label defined earlier creates backward goto entry."""
        code = """
void loop_func(void) {
    repeat:
    process_item();
    if (has_more())
        goto repeat;
    cleanup();
}
"""
        result = self._scan_c(code)
        functions = result["functions"]
        self.assertEqual(len(functions), 1)
        func = functions[0]
        self.assertIn("goto_jumps", func)
        self.assertEqual(len(func["goto_jumps"]), 1)
        gj = func["goto_jumps"][0]
        self.assertEqual(gj["label"], "repeat")
        self.assertEqual(gj["direction"], "backward")

    def test_forward_goto_detected(self):
        """goto to a label defined later creates forward goto entry."""
        code = """
void skip_func(int err) {
    if (err)
        goto out;
    process_data();
out:
    return;
}
"""
        result = self._scan_c(code)
        functions = result["functions"]
        self.assertEqual(len(functions), 1)
        func = functions[0]
        self.assertIn("goto_jumps", func)
        self.assertEqual(len(func["goto_jumps"]), 1)
        gj = func["goto_jumps"][0]
        self.assertEqual(gj["label"], "out")
        self.assertEqual(gj["direction"], "forward")

    def test_goto_labels_recorded(self):
        """Labels within functions are recorded in goto_labels."""
        code = """
void labeled_func(void) {
retry:
    init();
    work();
done:
    return;
}
"""
        result = self._scan_c(code)
        functions = result["functions"]
        self.assertEqual(len(functions), 1)
        func = functions[0]
        self.assertIn("goto_labels", func)
        label_names = [l["label"] for l in func["goto_labels"]]
        self.assertIn("retry", label_names)
        self.assertIn("done", label_names)

    def test_no_goto_empty_fields(self):
        """Functions without goto have no goto_jumps/goto_labels fields."""
        code = """
void simple_func(void) {
    do_work();
}
"""
        result = self._scan_c(code)
        functions = result["functions"]
        self.assertEqual(len(functions), 1)
        func = functions[0]
        self.assertNotIn("goto_jumps", func)
        self.assertNotIn("goto_labels", func)

    def test_multiple_gotos(self):
        """Multiple goto statements in one function are all captured."""
        code = """
void multi_goto(void) {
    if (err1) goto err1_handler;
    step1();
    if (err2) goto err2_handler;
    step2();
    return;
err1_handler:
    handle_err1();
    return;
err2_handler:
    handle_err2();
    return;
}
"""
        result = self._scan_c(code)
        functions = result["functions"]
        self.assertEqual(len(functions), 1)
        func = functions[0]
        self.assertEqual(len(func["goto_jumps"]), 2)
        labels = [gj["label"] for gj in func["goto_jumps"]]
        self.assertIn("err1_handler", labels)
        self.assertIn("err2_handler", labels)

    def test_labeled_statement_callee_extracted(self):
        """Calls after a label are still extracted as normal edges."""
        code = """
void labeled_call(void) {
    init();
out:
    cleanup();
}
"""
        result = self._scan_c(code)
        edges = result["edges"]
        callees = [e.get("target", "") for e in edges]
        self.assertIn("init", callees)
        self.assertIn("cleanup", callees)

    def test_callee_args_has_line(self):
        """callee_args entries include line numbers."""
        code = """
void func(void) {
    step1();
    step2();
}
"""
        result = self._scan_c(code)
        functions = result["functions"]
        self.assertEqual(len(functions), 1)
        func = functions[0]
        for ca in func.get("callee_args", []):
            self.assertIn("line", ca)
            self.assertGreater(ca["line"], 0)


class TestGoGotoExtraction(unittest.TestCase):
    """Test Go scanner extraction of goto statements."""

    def _scan_go(self, code: str):
        """Scan Go code and return result dict."""
        from _scanner.go_scanner import GoTreeSitterScanner
        scanner = GoTreeSitterScanner()
        with tempfile.NamedTemporaryFile(suffix='.go', mode='w', delete=False) as f:
            f.write(code)
            f.flush()
            result = scanner.scan_file(f.name, source_root=os.path.dirname(f.name))
        os.unlink(f.name)
        return result

    def test_go_backward_goto(self):
        """Go backward goto is detected."""
        code = """package main

func retryLoop() {
retry:
	process()
	if hasMore() {
		goto retry
	}
}
"""
        result = self._scan_go(code)
        func = next((f for f in result["functions"] if f["name"] == "retryLoop"), None)
        self.assertIsNotNone(func)
        self.assertIn("goto_jumps", func)
        self.assertEqual(len(func["goto_jumps"]), 1)
        self.assertEqual(func["goto_jumps"][0]["label"], "retry")
        self.assertEqual(func["goto_jumps"][0]["direction"], "backward")

    def test_go_forward_goto(self):
        """Go forward goto is detected."""
        code = """package main

func skipFunc(err error) {
	if err != nil {
		goto done
	}
	doWork()
done:
	return
}
"""
        result = self._scan_go(code)
        func = next((f for f in result["functions"] if f["name"] == "skipFunc"), None)
        self.assertIsNotNone(func)
        self.assertIn("goto_jumps", func)
        self.assertEqual(func["goto_jumps"][0]["direction"], "forward")

    def test_go_labels_recorded(self):
        """Go labels are recorded in goto_labels."""
        code = """package main

func labeled() {
start:
	init()
end:
	return
}
"""
        result = self._scan_go(code)
        func = next((f for f in result["functions"] if f["name"] == "labeled"), None)
        self.assertIsNotNone(func)
        self.assertIn("goto_labels", func)
        label_names = [l["label"] for l in func["goto_labels"]]
        self.assertIn("start", label_names)
        self.assertIn("end", label_names)


class TestGotoBuilderAnnotation(unittest.TestCase):
    """Test builder annotation of call edges with goto control flow."""

    def _build_with_goto(self, goto_jumps, goto_labels, callee_args,
                         edges=None, func_name="test_func"):
        """Build a graph with goto metadata and check annotations."""
        from _builder.graph_build import build_graph
        # Build a complete extraction with callee functions defined
        all_functions = [
            {"id": "root_test_func", "name": func_name,
             "source_file": "test.c", "line": 1,
             "domain": "test", "labels": [],
             "goto_jumps": goto_jumps,
             "goto_labels": goto_labels,
             "callee_args": callee_args},
        ]
        # Add callee function nodes
        for ca in callee_args:
            callee_name = ca["callee"]
            invoked_id = f"test_{callee_name}"
            all_functions.append({
                "id": invoked_id, "name": callee_name,
                "source_file": "test.c", "line": ca.get("line", 1),
                "domain": "test", "labels": [],
            })
        if edges is None:
            edges = []
            for ca in callee_args:
                edges.append({
                    "source": "root_test_func",
                    "target": ca["callee"],
                    "call_order": ca["call_order"],
                    "call_condition": "",
                })
        extraction = {
            "functions": all_functions,
            "edges": edges,
            "domains": ["test"],
            "lang_stats": {"c": len(all_functions)},
        }
        return build_graph(extraction)

    def test_backward_goto_annotates_loop(self):
        """Backward goto annotates affected call edges with goto_loop."""
        G, _ = self._build_with_goto(
            goto_jumps=[{"label": "repeat", "line": 10, "direction": "backward"}],
            goto_labels=[{"label": "repeat", "line": 5}],
            callee_args=[
                {"call_order": 1, "callee": "init", "line": 3},
                {"call_order": 2, "callee": "process_item", "line": 6},
                {"call_order": 3, "callee": "has_more", "line": 7},
            ],
        )
        # init (line 3) is before the label (line 5), should NOT be annotated
        self.assertTrue(G.has_edge("root_test_func", "test_init"),
                        "Expected edge from test_func to init")
        edge_init = G["root_test_func"]["test_init"]
        self.assertNotIn("goto_loop", edge_init.get("call_condition", ""))
        # process_item (line 6) is between label(5) and goto(10), should be annotated
        self.assertTrue(G.has_edge("root_test_func", "test_process_item"),
                        "Expected edge from test_func to process_item")
        edge_process = G["root_test_func"]["test_process_item"]
        self.assertIn("goto_loop:repeat", edge_process.get("call_condition", ""))

    def test_forward_goto_annotates_skip(self):
        """Forward goto annotates affected call edges with goto_skip."""
        G, _ = self._build_with_goto(
            goto_jumps=[{"label": "out", "line": 3, "direction": "forward"}],
            goto_labels=[{"label": "out", "line": 7}],
            callee_args=[
                {"call_order": 1, "callee": "check", "line": 2},
                {"call_order": 2, "callee": "process", "line": 5},
                {"call_order": 3, "callee": "cleanup", "line": 8},
            ],
        )
        # check (line 2) is before goto (line 3), NOT annotated
        edge_check = G["root_test_func"]["test_check"]
        self.assertNotIn("goto_skip", edge_check.get("call_condition", ""))
        # process (line 5) is between goto(3) and label(7), should be annotated
        edge_process = G["root_test_func"]["test_process"]
        self.assertIn("goto_skip:out", edge_process.get("call_condition", ""))
        # cleanup (line 8) is after the label(7), NOT annotated as skip
        edge_cleanup = G["root_test_func"]["test_cleanup"]
        self.assertNotIn("goto_skip", edge_cleanup.get("call_condition", ""))

    def test_no_goto_no_annotation(self):
        """Functions without goto_jumps don't get any goto annotation."""
        G, _ = self._build_with_goto(
            goto_jumps=[],
            goto_labels=[{"label": "unused", "line": 5}],
            callee_args=[
                {"call_order": 1, "callee": "step1", "line": 3},
            ],
        )
        edge = G["root_test_func"]["test_step1"]
        self.assertEqual(edge.get("call_condition", ""), "")

    def test_goto_combined_with_existing_condition(self):
        """Goto annotation is appended to existing call_condition with &&."""
        G, _ = self._build_with_goto(
            goto_jumps=[{"label": "repeat", "line": 10, "direction": "backward"}],
            goto_labels=[{"label": "repeat", "line": 5}],
            callee_args=[
                {"call_order": 1, "callee": "process", "line": 6},
            ],
            edges=[
                {"source": "root_test_func", "target": "process",
                 "call_order": 1, "call_condition": "if(has_data)"},
            ],
        )
        edge = G["root_test_func"]["test_process"]
        cond = edge.get("call_condition", "")
        self.assertIn("if(has_data)", cond)
        self.assertIn("goto_loop:repeat", cond)
        self.assertIn("&&", cond)

    def test_unknown_direction_goto_no_annotation(self):
        """Goto with direction='unknown' does not annotate any edges."""
        G, _ = self._build_with_goto(
            goto_jumps=[{"label": "target", "line": 5, "direction": "unknown"}],
            goto_labels=[{"label": "target", "line": 3}],
            callee_args=[
                {"call_order": 1, "callee": "step1", "line": 4},
            ],
            edges=[
                {"source": "root_test_func", "target": "step1",
                 "call_order": 1, "call_condition": ""},
            ],
        )
        edge = G["root_test_func"]["test_step1"]
        self.assertEqual(edge.get("call_condition", ""), "")

    def test_goto_missing_target_label_no_annotation(self):
        """Goto referencing undefined label produces no annotation."""
        G, _ = self._build_with_goto(
            goto_jumps=[{"label": "nonexistent", "line": 5, "direction": "forward"}],
            goto_labels=[],  # No matching label
            callee_args=[
                {"call_order": 1, "callee": "step1", "line": 4},
            ],
            edges=[
                {"source": "root_test_func", "target": "step1",
                 "call_order": 1, "call_condition": ""},
            ],
        )
        edge = G["root_test_func"]["test_step1"]
        self.assertEqual(edge.get("call_condition", ""), "")

    def test_multiple_overlapping_gotos(self):
        """Multiple gotos affecting the same call produce compound annotations."""
        G, _ = self._build_with_goto(
            goto_jumps=[
                {"label": "retry", "line": 8, "direction": "backward"},
                {"label": "out", "line": 3, "direction": "forward"},
            ],
            goto_labels=[
                {"label": "retry", "line": 5},
                {"label": "out", "line": 10},
            ],
            callee_args=[
                {"call_order": 1, "callee": "process", "line": 6},
            ],
            edges=[
                {"source": "root_test_func", "target": "process",
                 "call_order": 1, "call_condition": ""},
            ],
        )
        edge = G["root_test_func"]["test_process"]
        cond = edge.get("call_condition", "")
        # process at line 6 is between retry(5) and goto(8) → goto_loop
        # process at line 6 is also between goto out(3) and label out(10) → goto_skip
        self.assertIn("goto_loop:retry", cond)
        self.assertIn("goto_skip:out", cond)


class TestGoGotoNoGoto(unittest.TestCase):
    """Go: functions without goto have no goto fields."""

    def test_go_no_goto_empty_fields(self):
        """Go function without goto has no goto_jumps/goto_labels."""
        from _scanner.go_scanner import GoTreeSitterScanner
        scanner = GoTreeSitterScanner()
        code = """package main

func simpleFunc() {
	doWork()
}
"""
        with tempfile.NamedTemporaryFile(suffix='.go', mode='w', delete=False) as f:
            f.write(code)
            f.flush()
            result = scanner.scan_file(f.name, source_root=os.path.dirname(f.name))
        os.unlink(f.name)
        func = next((f for f in result["functions"] if f["name"] == "simpleFunc"), None)
        self.assertIsNotNone(func)
        self.assertNotIn("goto_jumps", func)
        self.assertNotIn("goto_labels", func)


if __name__ == "__main__":
    unittest.main()
