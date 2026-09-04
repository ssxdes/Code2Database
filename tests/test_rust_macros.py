"""Rust macro support — definitions, invocations, in-argument calls,
derive attributes.

The Rust scanner had ZERO macro handling: macro_rules! definitions
were invisible, calls inside macro arguments (my_try!(compute(x)))
were invisible (token_tree contents are not parsed into expression
nodes), and #[derive]/attributes were dropped.
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
macro_rules! my_try {
    ($e:expr) => { match $e { Ok(v) => v, Err(e) => return Err(e) } };
}

#[derive(Debug, Clone)]
pub struct Point { x: i32, y: i32 }

pub fn process(p: Point) -> i32 {
    println!("point {} {}", p.x, p.y);
    let v = my_try!(wrap(compute(p.x)));
    v + 1
}

fn wrap(x: i32) -> i32 { x }
fn compute(x: i32) -> i32 { x }
"""


class TestRustMacroDefinition(unittest.TestCase):

    def test_macro_rules_creates_node(self):
        result = _scan_rust(_CODE)
        macros = [f for f in result["functions"]
                  if f.get("node_type") == "macro"]
        self.assertEqual([m["name"] for m in macros], ["my_try"])
        self.assertIn("macro_rules", macros[0]["signature"])


class TestRustMacroInvocations(unittest.TestCase):

    def test_invocation_and_nested_calls_extracted(self):
        result = _scan_rust(_CODE)
        process = next(f for f in result["functions"]
                       if f["name"] == "process")
        callees = [a["callee"] for a in process.get("callee_args", [])]
        # the macro invocation itself...
        self.assertIn("my_try", callees)
        # ...and the calls inside its arguments (token_tree contents
        # are NOT parsed into expression nodes — pattern-matched)
        self.assertIn("wrap", callees)
        self.assertIn("compute", callees)
        # format args are field accesses, not calls
        self.assertNotIn("x", callees)
        targets = {e["target"] for e in result["edges"]}
        self.assertIn("my_try", targets)
        self.assertIn("compute", targets)

    def test_std_macro_with_only_format_args_no_call_edges(self):
        result = _scan_rust("""\
fn show(x: i32) {
    println!("value {}", x);
    let v = vec![x, x];
    v.len() as i32
}
""")
        show = next(f for f in result["functions"] if f["name"] == "show")
        callees = [a["callee"] for a in show.get("callee_args", [])]
        self.assertNotIn("x", callees, "field/format args must not be calls")


class TestRustAttributes(unittest.TestCase):

    def test_derive_attributes_recorded(self):
        result = _scan_rust(_CODE)
        point = next(f for f in result["functions"] if f["name"] == "Point")
        self.assertIn("derive:Debug", point.get("attributes", []))
        self.assertIn("derive:Clone", point.get("attributes", []))

    def test_plain_attribute_recorded_on_function(self):
        result = _scan_rust("""\
#[inline]
#[allow(dead_code)]
fn helper(x: i32) -> i32 { x }
""")
        helper = next(f for f in result["functions"]
                      if f["name"] == "helper")
        attrs = helper.get("attributes", [])
        self.assertIn("inline", attrs)
        self.assertIn("allow", attrs)


if __name__ == "__main__":
    unittest.main()
