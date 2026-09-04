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
        pairs = {(e.get("source"), e.get("target"), e.get("relation"))
                 for e in result["edges"]}
        self.assertTrue(
            any(s.endswith("Service.compute") and t.endswith("Service.helper")
                and r == "INVOKES" for s, t, r in pairs),
            f"compute -> helper INVOKES edge missing: {pairs}")

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


if __name__ == "__main__":
    unittest.main()
