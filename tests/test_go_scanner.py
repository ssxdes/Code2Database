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
        from _builder.graph_build import build_graph
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
                {"id": "root_store_memorywriter_write", "name": "MemoryWriter.Write",
                 "domain": "store", "source_file": "store/w.go", "line": 19,
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


if __name__ == "__main__":
    unittest.main()
