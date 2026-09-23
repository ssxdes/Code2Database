"""Scanner file boundaries: empty/comment-only/BOM/GBK/CRLF/deep chains.

The C scanner's tree walks used to recurse per AST level — a single
long expression line (macro-expanded arithmetic, receiver chains)
raised RecursionError inside extract, and scan_file's error handling
dropped the WHOLE file from the scan with only an error field left
behind. All walks are iterative now; these tests pin that guarantee
alongside the encoding boundaries (BOM, non-UTF-8 bytes, CRLF) the
byte-offset rebinding logic has to survive.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


def _scan_bytes(data, suffix='.c'):
    from _scanner.c_scanner import CTreeSitterScanner
    scanner = CTreeSitterScanner()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(data)
        f.flush()
        result = scanner.scan_file(f.name, source_root=os.path.dirname(f.name))
    os.unlink(f.name)
    return result


def _names(result):
    return [f["name"] for f in result["functions"]]


class TestFileBoundaries(unittest.TestCase):

    def test_empty_file(self):
        result = _scan_bytes(b"")
        self.assertEqual(result["functions"], [])
        self.assertNotIn("error", result)

    def test_comment_only_file(self):
        result = _scan_bytes(b"/* nothing here */\n// nor here\n")
        self.assertEqual(result["functions"], [])
        self.assertNotIn("error", result)

    def test_utf8_bom_and_multibyte_comment(self):
        code = ("/* 注释: 中文 */\n"
                "int caf\xc3\xa9_fn(void) { helper(); return 0; }\n"
                "void helper(void) {}\n").encode("latin-1", errors="ignore")
        # use a clean UTF-8 body with a BOM prefix
        body = ("/* 注释: 中文 */\n"
                "int bom_fn(void) { helper(); return 0; }\n"
                "void helper(void) {}\n").encode("utf-8")
        result = _scan_bytes(b"\xef\xbb\xbf" + body)
        self.assertNotIn("error", result)
        self.assertEqual(set(_names(result)), {"bom_fn", "helper"})
        edges = {(e.get("source"), e.get("target")) for e in result["edges"]}
        self.assertIn(("root_bom_fn", "helper"), edges)

    def test_gbk_comment_bytes_survive(self):
        body = ("int g_fn(void) { helper2(); return 0; }\n"
                "/* 中文注释 */\n"
                "void helper2(void) {}\n").encode("gbk")
        result = _scan_bytes(body)
        self.assertNotIn("error", result)
        self.assertEqual(set(_names(result)), {"g_fn", "helper2"})
        edges = {(e.get("source"), e.get("target")) for e in result["edges"]}
        self.assertIn(("root_g_fn", "helper2"), edges)

    def test_crlf_line_endings(self):
        result = _scan_bytes(
            b"int crlf_fn(void) { helper3(); }\r\nvoid helper3(void) {}\r\n")
        self.assertNotIn("error", result)
        self.assertEqual(set(_names(result)), {"crlf_fn", "helper3"})
        edges = {(e.get("source"), e.get("target")) for e in result["edges"]}
        self.assertIn(("root_crlf_fn", "helper3"), edges)
        # line numbers count CRLF lines correctly
        by_name = {f["name"]: f for f in result["functions"]}
        self.assertEqual(by_name["crlf_fn"]["line"], 1)
        self.assertEqual(by_name["helper3"]["line"], 2)


class TestDeepExpressionChains(unittest.TestCase):
    """One long line must not cost the file."""

    def test_top_level_long_initializer(self):
        result = _scan_bytes(
            b"int pad = " + b"1+" * 5000 + b"1;\n"
            b"int long_fn(void) { helper(); }\n"
            b"void helper(void) {}\n")
        self.assertNotIn("error", result)
        self.assertEqual(set(_names(result)), {"long_fn", "helper"})

    def test_in_body_long_expression(self):
        result = _scan_bytes(
            b"int f(void) { int pad = " + b"1+" * 5000 +
            b"1; helper(); }\nvoid helper(void) {}\n")
        self.assertNotIn("error", result)
        self.assertEqual(set(_names(result)), {"f", "helper"})
        edges = {(e.get("source"), e.get("target")) for e in result["edges"]}
        self.assertIn(("root_f", "helper"), edges)

    def test_receiver_chain_calls(self):
        result = _scan_bytes(
            b"int g(void) { obj.a().b().c()" + b".d()" * 500 +
            b"; helper5(); }\nvoid helper5(void) {}\n")
        self.assertNotIn("error", result)
        self.assertEqual(set(_names(result)), {"g", "helper5"})
        targets = [e.get("target") for e in result["edges"]]
        self.assertIn("d", targets)
        self.assertIn("helper5", targets)
        # call_order stays monotonic across the chain
        orders = [e["call_order"] for e in result["edges"]
                  if e.get("call_order") is not None]
        self.assertEqual(orders, sorted(orders))

    def test_nested_mixed_calls_keep_source_order(self):
        result = _scan_bytes(
            b"int h(void) { foo(bar(x()), qux(z())); helper6(); }\n")
        self.assertNotIn("error", result)
        seq = [(e.get("target"), e.get("call_order"))
               for e in result["edges"]]
        self.assertEqual(
            [t for t, _ in seq],
            ["foo", "bar", "x", "qux", "z", "helper6"])


if __name__ == "__main__":
    unittest.main()
