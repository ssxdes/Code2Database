"""Go scanner: type system extraction + interface-typed call recording.

Go had NO type_declaration handling — no interface/struct nodes, no
IMPORTS edges, the receiver was dropped from every call target, and
nothing recorded that a call went through an interface-typed value
(precondition for dynamic-dispatch INFERRED edges in the builder).
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


def _scan_go(code):
    from _scanner.go_scanner import GoTreeSitterScanner
    scanner = GoTreeSitterScanner()
    with tempfile.NamedTemporaryFile(suffix='.go', mode='w',
                                     delete=False) as f:
        f.write(code)
        f.flush()
        result = scanner.scan_file(f.name, source_root=os.path.dirname(f.name))
    os.unlink(f.name)
    return result


_CODE = """\
package store

import (
    "io"
    "sync"
)

type Writer interface {
    Write(p []byte) (int, error)
    Flush() error
}

type DiskWriter struct {
    mu sync.Mutex
    buf []byte
}

type MemoryWriter struct {
    Base
}

func (d *DiskWriter) Write(p []byte) (int, error) {
    return len(p), nil
}

func (m *MemoryWriter) Write(p []byte) (int, error) {
    return len(p), nil
}

func use(w Writer) int {
    n, _ := w.Write(nil)
    return n
}
"""


class TestGoTypeNodes(unittest.TestCase):

    def test_interface_and_struct_nodes_created(self):
        result = _scan_go(_CODE)
        nodes = {f["name"]: f for f in result["functions"]}
        self.assertIn("Writer", nodes)
        self.assertEqual(nodes["Writer"].get("node_type"), "interface")
        self.assertEqual(nodes["Writer"].get("methods"),
                         ["Write", "Flush"])
        self.assertIn("DiskWriter", nodes)
        self.assertEqual(nodes["DiskWriter"].get("node_type"), "struct")
        self.assertIn("MemoryWriter", nodes)

    def test_struct_embedding_emits_implements(self):
        result = _scan_go(_CODE)
        impl = [e for e in result["edges"]
                if e.get("relation") == "IMPLEMENTS"]
        pairs = {(e.get("source"), e.get("target")) for e in impl}
        # Domain stays 'root' for a temp-dir scan (Go has no
        # package→domain override — existing behavior).
        self.assertIn(("root_memorywriter", "base"), pairs)

    def test_imports_edges(self):
        result = _scan_go(_CODE)
        imports = [e for e in result.get("import_edges") or []
                   if e.get("relation") == "IMPORTS"]
        targets = {e.get("target") for e in imports}
        self.assertIn("io", targets)
        self.assertIn("sync", targets)


class TestGoInterfaceCalls(unittest.TestCase):

    def test_full_callee_and_interface_call_recorded(self):
        result = _scan_go(_CODE)
        use = next(f for f in result["functions"] if f["name"] == "use")
        # full callee keeps the receiver; the edge target stays the
        # method name (existing resolution convention)
        args_with_full = [a for a in use.get("callee_args", [])
                          if a.get("full_callee")]
        self.assertTrue(any(a["full_callee"] == "w.Write"
                            for a in args_with_full),
                        f"full_callee missing: {use.get('callee_args')}")
        # statically-typed receiver through a local interface
        self.assertIn(
            {"line": 31, "iface": "Writer", "method": "Write", "receiver": "w"},
            use.get("interface_calls", []),
            f"interface_calls missing: {use.get('interface_calls')}")

    def test_struct_typed_receiver_not_recorded_as_interface_call(self):
        code = """\
package store

type DiskWriter struct{}

func (d *DiskWriter) Write(p []byte) int { return 0 }

func use(d *DiskWriter) int {
    return d.Write(nil)
}
"""
        result = _scan_go(code)
        use = next(f for f in result["functions"] if f["name"] == "use")
        # Candidate is recorded with its declared type; the BUILDER
        # resolves against the global interface registry (DiskWriter
        # is a struct there → no dispatch edge).
        self.assertEqual(
            use.get("interface_calls"),
            [{"line": 8, "iface": "DiskWriter", "method": "Write",
              "receiver": "d"}])


class TestGoInterfaceDispatchBuildPhase(unittest.TestCase):
    """Builder: interface_calls resolve to INFERRED DISPATCH edges to
    every implementor (Go structural satisfaction)."""

    def test_dispatch_edges_added(self):
        """Full interface satisfaction: both types implement Write +
        Flush → both get DISPATCH edges."""
        from _builder.graph.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_store_writer", "name": "Writer", "domain": "store",
                 "source_file": "store/w.go", "line": 5, "labels": [],
                 "is_empty": False, "node_type": "interface",
                 "methods": ["Write", "Flush"], "signature": "interface Writer",
                 "api_constraints": "", "body_text": "", "params": [],
                 "local_vars": [], "callee_args": [], "condition_vars": []},
                {"id": "root_store_diskwriter_write", "name": "DiskWriter.Write",
                 "domain": "store", "source_file": "store/w.go", "line": 15,
                 "labels": [], "is_empty": False, "signature": "",
                 "api_constraints": "", "body_text": "", "params": [],
                 "local_vars": [], "callee_args": [], "condition_vars": []},
                {"id": "root_store_diskwriter_flush", "name": "DiskWriter.Flush",
                 "domain": "store", "source_file": "store/w.go", "line": 16,
                 "labels": [], "is_empty": False, "signature": "",
                 "api_constraints": "", "body_text": "", "params": [],
                 "local_vars": [], "callee_args": [], "condition_vars": []},
                {"id": "root_store_memorywriter_write", "name": "MemoryWriter.Write",
                 "domain": "store", "source_file": "store/w.go", "line": 19,
                 "labels": [], "is_empty": False, "signature": "",
                 "api_constraints": "", "body_text": "", "params": [],
                 "local_vars": [], "callee_args": [], "condition_vars": []},
                {"id": "root_store_memorywriter_flush", "name": "MemoryWriter.Flush",
                 "domain": "store", "source_file": "store/w.go", "line": 20,
                 "labels": [], "is_empty": False, "signature": "",
                 "api_constraints": "", "body_text": "", "params": [],
                 "local_vars": [], "callee_args": [], "condition_vars": []},
                {"id": "root_store_use", "name": "use", "domain": "store",
                 "source_file": "store/w.go", "line": 23, "labels": [],
                 "is_empty": False, "signature": "",
                 "api_constraints": "", "body_text": "", "params": [],
                 "local_vars": [], "callee_args": [], "condition_vars": [],
                 "interface_calls": [
                     {"line": 24, "iface": "Writer", "method": "Write",
                      "receiver": "w"}]},
            ],
            "edges": [
                {"source": "root_store_use", "target": "write",
                 "call_order": 1, "call_condition": ""},
            ],
        }
        G, _ = build_graph(extraction)
        dispatch = [(u, v) for u, v, d in G.edges(data=True)
                    if d.get("relation") == "DISPATCH"]
        self.assertIn(("root_store_use", "root_store_diskwriter_write"),
                      dispatch)
        self.assertIn(("root_store_use", "root_store_memorywriter_write"),
                      dispatch)
        for u, v, d in G.edges(data=True):
            if d.get("relation") == "DISPATCH":
                self.assertEqual(d.get("confidence"), "INFERRED")
                self.assertIn("Writer", d.get("call_condition", ""))
                self.assertEqual(d.get("concurrency"), "dispatch")
                self.assertTrue(d.get("evidence", ""))

    def test_partial_implementor_gets_no_dispatch(self):
        """A type that has the method but doesn't satisfy the FULL
        interface must NOT get a DISPATCH edge (Go structural
        satisfaction requires all methods)."""
        from _builder.graph.graph_build import build_graph
        extraction = {
            "functions": [
                {"id": "root_store_writer", "name": "Writer", "domain": "store",
                 "source_file": "store/w.go", "line": 5, "labels": [],
                 "is_empty": False, "node_type": "interface",
                 "methods": ["Write", "Flush"], "signature": "interface Writer",
                 "api_constraints": "", "body_text": "", "params": [],
                 "local_vars": [], "callee_args": [], "condition_vars": []},
                # FullWriter implements both Write + Flush → dispatches
                {"id": "root_store_fullwriter_write", "name": "FullWriter.Write",
                 "domain": "store", "source_file": "store/w.go", "line": 10,
                 "labels": [], "is_empty": False, "signature": "",
                 "api_constraints": "", "body_text": "", "params": [],
                 "local_vars": [], "callee_args": [], "condition_vars": []},
                {"id": "root_store_fullwriter_flush", "name": "FullWriter.Flush",
                 "domain": "store", "source_file": "store/w.go", "line": 11,
                 "labels": [], "is_empty": False, "signature": "",
                 "api_constraints": "", "body_text": "", "params": [],
                 "local_vars": [], "callee_args": [], "condition_vars": []},
                # PartialWriter has only Write, not Flush → no dispatch
                {"id": "root_store_partialwriter_write", "name": "PartialWriter.Write",
                 "domain": "store", "source_file": "store/w.go", "line": 15,
                 "labels": [], "is_empty": False, "signature": "",
                 "api_constraints": "", "body_text": "", "params": [],
                 "local_vars": [], "callee_args": [], "condition_vars": []},
                {"id": "root_store_use", "name": "use", "domain": "store",
                 "source_file": "store/w.go", "line": 20, "labels": [],
                 "is_empty": False, "signature": "",
                 "api_constraints": "", "body_text": "", "params": [],
                 "local_vars": [], "callee_args": [], "condition_vars": [],
                 "interface_calls": [
                     {"line": 21, "iface": "Writer", "method": "Write",
                      "receiver": "w"}]},
            ],
            "edges": [
                {"source": "root_store_use", "target": "write",
                 "call_order": 1, "call_condition": ""},
            ],
        }
        G, _ = build_graph(extraction)
        dispatch = [(u, v) for u, v, d in G.edges(data=True)
                    if d.get("relation") == "DISPATCH"]
        targets = {v for u, v in dispatch}
        self.assertIn("root_store_fullwriter_write", targets,
                      "full implementor must get dispatch")
        self.assertNotIn("root_store_partialwriter_write", targets,
                         "partial implementor must NOT get dispatch")


class TestGoGoroutineExtraction(unittest.TestCase):
    """Goroutine launches: named-callee spawn edges and function-literal
    bodies (which become synthetic anonymous nodes so the spawn target
    resolves and the literal's calls stay in the graph)."""

    _GOROUTINE_CODE = """\
package main

func helper() {}

func launcher() {
    go worker(1)
    go func() { helper() }()
}

func worker(n int) {}
"""

    def test_named_callee_spawn_edge(self):
        result = _scan_go(self._GOROUTINE_CODE)
        spawn = [e for e in result["edges"]
                 if e.get("target") == "worker"]
        self.assertEqual(len(spawn), 1)
        self.assertEqual(spawn[0].get("concurrency"), "goroutine")
        self.assertEqual(spawn[0].get("source"), "root_launcher")

    def test_spawn_concurrency_info_recorded(self):
        result = _scan_go(self._GOROUTINE_CODE)
        launcher = next(f for f in result["functions"]
                        if f["name"] == "launcher")
        worker_call = next(a for a in launcher["callee_args"]
                           if a["callee"] == "worker")
        self.assertTrue(worker_call["concurrency_info"]["is_spawn"])
        self.assertEqual(
            worker_call["concurrency_info"]["concurrency_type"], "goroutine")
        self.assertEqual(
            worker_call["concurrency_info"]["spawn_target"], "worker")

    def test_goroutine_launcher_gets_thread_label(self):
        result = _scan_go(self._GOROUTINE_CODE)
        launcher = next(f for f in result["functions"]
                        if f["name"] == "launcher")
        self.assertIn("thread_processor", launcher["labels"])

    def test_func_literal_becomes_synthetic_node(self):
        result = _scan_go(self._GOROUTINE_CODE)
        anon = [f for f in result["functions"]
                if f.get("node_type") == "anonymous"]
        self.assertEqual(len(anon), 1)
        self.assertEqual(anon[0]["name"], "launcher_go_anon_7")
        self.assertIn("thread_processor", anon[0]["labels"])
        self.assertEqual(anon[0]["callee_args"][0]["callee"], "helper")

    def test_func_literal_spawn_edge_resolves_and_body_calls_kept(self):
        result = _scan_go(self._GOROUTINE_CODE)
        edges = {(e.get("source"), e.get("target")) for e in result["edges"]}
        self.assertIn(("root_launcher", "launcher_go_anon_7"), edges)
        self.assertIn(("root_launcher_go_anon_7", "helper"), edges)

    def test_nested_func_literals(self):
        code = """\
package main

func deep() {}

func launcher() {
    go func() {
        go func() { deep() }()
    }()
}
"""
        result = _scan_go(code)
        names = {f["name"] for f in result["functions"]}
        self.assertIn("launcher_go_anon_6", names)
        self.assertIn("launcher_go_anon_6_go_anon_7", names)
        edges = {(e.get("source"), e.get("target")) for e in result["edges"]}
        self.assertIn(("root_launcher", "launcher_go_anon_6"), edges)
        self.assertIn(("root_launcher_go_anon_6",
                       "launcher_go_anon_6_go_anon_7"), edges)
        self.assertIn(("root_launcher_go_anon_6_go_anon_7", "deep"), edges)
        nested = [e for e in result["edges"]
                  if e.get("target") == "launcher_go_anon_6_go_anon_7"]
        self.assertEqual(nested[0].get("concurrency"), "goroutine")


class TestGoConditionScopes(unittest.TestCase):
    """if/else branches route calls through synthetic condition nodes."""

    def test_if_else_scope_edges(self):
        code = """\
package main

func a() {}
func b() {}
func c() {}

func chooser(flag bool) {
    if flag {
        a()
    } else {
        b()
    }
    c()
}
"""
        result = _scan_go(code)
        chooser = next(f for f in result["functions"]
                       if f["name"] == "chooser")
        edges = [(e.get("source"), e.get("target"),
                  e.get("call_condition"), e.get("is_cond_child"))
                 for e in result["edges"]]
        # then-branch: cond node -> a
        self.assertIn(("root_chooser__cond_0", "a", "", True), edges)
        # else-branch: else cond node -> b
        self.assertIn(("root_chooser__cond_0_else", "b", "", True), edges)
        # scope wiring from the invoker
        self.assertIn(("root_chooser", "root_chooser__cond_0",
                       "if(flag)", None), edges)
        self.assertIn(("root_chooser", "root_chooser__cond_0_else",
                       "!(flag)", None), edges)
        # unconditional call bypasses the scope nodes
        self.assertIn(("root_chooser", "c", "", None), edges)
        self.assertIn(
            {"condition": "if(flag)", "vars": ["flag"]},
            chooser.get("condition_vars", []))


if __name__ == "__main__":
    unittest.main()
