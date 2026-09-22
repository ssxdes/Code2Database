"""Rust scanner: impl-trait relationships and type nodes.

`impl Writer for DiskWriter` must emit an IMPLEMENTS edge that reaches
the graph's import-edge channel — the scan-result tuple classifier used
to route a leading IMPLEMENTS record into the vtable-registration slot
whenever the file had no `use` declarations, so trait relationships
only survived in files that happened to import something.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


def _scan_rust(code):
    from _scanner.rust_scanner import RustTreeSitterScanner
    scanner = RustTreeSitterScanner()
    with tempfile.NamedTemporaryFile(suffix='.rs', mode='w',
                                     delete=False) as f:
        f.write(code)
        f.flush()
        result = scanner.scan_file(f.name, source_root=os.path.dirname(f.name))
    os.unlink(f.name)
    return result


_CODE = """\
pub trait Writer {
    fn write(&self, buf: &[u8]) -> usize;
}

pub struct DiskWriter {
    path: String,
}

impl Writer for DiskWriter {
    fn write(&self, buf: &[u8]) -> usize {
        inner_write(buf)
    }
}

impl DiskWriter {
    pub fn new(p: String) -> Self {
        DiskWriter { path: p }
    }
}

fn inner_write(b: &[u8]) -> usize { 0 }
"""


class TestRustImplEdges(unittest.TestCase):

    def test_impl_trait_emits_implements_edge(self):
        result = _scan_rust(_CODE)
        impl = [e for e in result.get("import_edges", [])
                if e.get("relation") == "IMPLEMENTS"]
        pairs = {(e.get("source"), e.get("target")) for e in impl}
        self.assertIn(("root_diskwriter", "writer"), pairs)

    def test_implements_edge_not_misrouted_without_imports(self):
        # A file with no `use` declarations still routes its
        # IMPLEMENTS record to the import-edge channel (the vtable
        # slot must stay empty).
        result = _scan_rust(_CODE)
        self.assertEqual(result.get("vtable_registrations"), [])

    def test_trait_and_struct_type_nodes(self):
        result = _scan_rust(_CODE)
        by_name = {f["name"]: f for f in result["functions"]}
        self.assertEqual(by_name["Writer"].get("node_type"), "trait")
        self.assertEqual(by_name["DiskWriter"].get("node_type"), "struct")

    def test_impl_methods_qualified_by_type(self):
        result = _scan_rust(_CODE)
        names = {f["name"] for f in result["functions"]}
        self.assertIn("DiskWriter::write", names)
        self.assertIn("DiskWriter::new", names)

    def test_impl_method_calls_extracted(self):
        result = _scan_rust(_CODE)
        write_fn = next(f for f in result["functions"]
                        if f["name"] == "DiskWriter::write")
        callees = {a["callee"] for a in write_fn.get("callee_args", [])}
        self.assertIn("inner_write", callees)

    def test_use_imports_coexist_with_implements(self):
        code = ("use std::io;\n"
                "pub trait T { fn t(&self); }\n"
                "pub struct S;\n"
                "impl T for S { fn t(&self) {} }\n")
        result = _scan_rust(code)
        relations = {(e.get("relation"), e.get("target"))
                     for e in result.get("import_edges", [])}
        self.assertIn(("IMPLEMENTS", "t"), relations)
        self.assertIn(("IMPORTS", "std::io"), relations)


if __name__ == "__main__":
    unittest.main()
