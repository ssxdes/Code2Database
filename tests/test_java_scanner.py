"""Java scanner correctness — call edges + class-qualified names.

The class_body/interface_body recursion in _walk_java passed
(functions, import_edges, class_name) positionally where the signature
expects (functions, edges, import_edges, class_name) — every argument
after `functions` shifted one slot. Net effect: method calls inside
class bodies appended their INVOKES edges into the import list that
_extract immediately reassigns away (edges lost forever), and methods
were never class-qualified. The Java graph was nodes-only.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


def _scan_java(code):
    from _scanner.java_scanner import JavaTreeSitterScanner
    scanner = JavaTreeSitterScanner()
    with tempfile.NamedTemporaryFile(suffix='.java', mode='w',
                                     delete=False) as f:
        f.write(code)
        f.flush()
        result = scanner.scan_file(f.name, source_root=os.path.dirname(f.name))
    os.unlink(f.name)
    return result


class TestJavaCallEdges(unittest.TestCase):

    def test_method_call_produces_invokes_edge(self):
        result = _scan_java("""\
public class Service {
    public int compute(int x) {
        return helper(x);
    }
    int helper(int x) { return x + 1; }
}
""")
        # Raw scanner edges carry (source, target) without relation —
        # the builder assigns INVOKES during resolution.
        pairs = {(e.get("source"), e.get("target"))
                 for e in result["edges"]}
        self.assertIn(
            ("root_service_compute", "helper"), pairs,
            f"compute -> helper call edge missing: {pairs}")

    def test_methods_are_class_qualified(self):
        result = _scan_java("""\
public class Service {
    public void run() { go(); }
    void go() {}
}
""")
        names = {f["name"] for f in result["functions"]}
        self.assertIn("Service.run", names)
        self.assertIn("Service.go", names)

    def test_import_edges_stay_imports(self):
        result = _scan_java("""\
import java.util.List;

public class Service {
    public void run() { go(); }
    void go() {}
}
""")
        for e in result.get("import_edges") or []:
            self.assertEqual(e.get("relation"), "IMPORTS",
                             f"call edge leaked into import_edges: {e}")


class TestJavaInterfaceDispatch(unittest.TestCase):
    """Java interface dispatch — mirrors Go's interface_calls mechanism.
    The builder phase (_add_go_interface_dispatch) is language-neutral
    (reads node_type='interface' + methods + interface_calls); the Java
    scanner must populate those same attrs."""

    _CODE = """\
package store;

interface Writer {
    void write();
    void flush();
}

class DiskWriter implements Writer {
    public void write() {}
    public void flush() {}
}

class MemWriter implements Writer {
    public void write() {}
    public void flush() {}
}

class Service {
    void use(Writer w) {
        w.write();
    }
}
"""

    def test_interface_node_has_methods(self):
        result = _scan_java(self._CODE)
        writer = next(f for f in result["functions"]
                      if f["name"] == "Writer" and f.get("node_type") == "interface")
        self.assertIn("write", writer.get("methods", []))
        self.assertIn("flush", writer.get("methods", []))

    def test_interface_call_recorded(self):
        result = _scan_java(self._CODE)
        use = next(f for f in result["functions"] if f["name"] == "Service.use")
        calls = use.get("interface_calls", [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["iface"], "Writer")
        self.assertEqual(calls[0]["method"], "write")
        self.assertEqual(calls[0]["receiver"], "w")

    def test_dispatch_edges_in_built_graph(self):
        from _builder.graph_build import build_graph
        result = _scan_java(self._CODE)
        G, _ = build_graph(result)
        dispatch = [(u, v) for u, v, d in G.edges(data=True)
                    if d.get("relation") == "DISPATCH"]
        targets = {v for u, v in dispatch}
        # Both DiskWriter.write and MemWriter.write should receive
        # DISPATCH edges from Service.use (both fully satisfy Writer).
        disk = [n for n, d in G.nodes(data=True) if d.get("name") == "DiskWriter.write"]
        mem = [n for n, d in G.nodes(data=True) if d.get("name") == "MemWriter.write"]
        self.assertEqual(len(disk), 1)
        self.assertEqual(len(mem), 1)
        self.assertIn(disk[0], targets, "DiskWriter.write must get dispatch edge")
        self.assertIn(mem[0], targets, "MemWriter.write must get dispatch edge")


if __name__ == "__main__":
    unittest.main()
