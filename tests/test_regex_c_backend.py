"""Regex C backend smoke + backend agreement.

_vendor/_regex_c_scanner.py is the C/C++ fallback when tree-sitter is
not installed (3746 lines, previously zero tests). It feeds real builds
on machines without the wheel, so its extraction contract deserves at
least a smoke pin: functions, call edges with conditions, vtable
registrations, and function-set agreement with the tree-sitter backend
on the same file.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


_CODE = """\
#include <stdio.h>

static int helper(int x) { return x + 1; }

int compute(int a, int b) {
    if (a > b) {
        return helper(a);
    }
    return helper(b);
}

static const struct ops my_ops = {
    .start = compute,
};
"""


def _write(code):
    f = tempfile.NamedTemporaryFile(suffix='.c', mode='w', delete=False)
    f.write(code)
    f.flush()
    return f.name


class TestRegexCBackend(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from _vendor._regex_c_scanner import scan_c_file
        cls.path = _write(_CODE)
        cls.result = scan_c_file(cls.path, source_root=os.path.dirname(cls.path))

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.path)

    def test_functions_extracted(self):
        names = {f["name"] for f in self.result["functions"]}
        self.assertEqual(names, {"compute", "helper"})

    def test_call_edges_extracted(self):
        pairs = {(e.get("source"), e.get("target"))
                 for e in self.result["edges"]}
        self.assertIn(("root.compute", "helper"), pairs)

    def test_vtable_registration_extracted(self):
        regs = self.result.get("vtable_registrations") or []
        found = any(r.get("var_name") == "my_ops" and
                    any(x.get("field") == "start" and
                        x.get("func_name") == "compute"
                        for x in r.get("registrations", []))
                    for r in regs)
        self.assertTrue(found, f"start=compute registration missing: {regs}")

    def test_result_shape(self):
        for key in ("functions", "edges", "vtable_registrations",
                    "fn_ptr_calls"):
            self.assertIn(key, self.result)


class TestBackendAgreement(unittest.TestCase):
    """Both C backends must find the same function set and the same
    direct call edge on a plain file (they may differ in synthetic
    condition nodes and id punctuation)."""

    def test_function_set_agrees(self):
        from _vendor._regex_c_scanner import scan_c_file
        from _scanner.c_scanner import CTreeSitterScanner
        path = _write(_CODE)
        try:
            regex = scan_c_file(path, source_root=os.path.dirname(path))
            ts = CTreeSitterScanner().scan_file(
                path, source_root=os.path.dirname(path))
        finally:
            os.unlink(path)
        self.assertEqual({f["name"] for f in regex["functions"]},
                         {f["name"] for f in ts["functions"]})
        regex_calls = {(e.get("source").split(".")[-1], e.get("target"))
                       for e in regex["edges"]}
        ts_calls = {(e.get("source", "").split("_")[-1], e.get("target"))
                    for e in ts["edges"]}
        self.assertIn(("compute", "helper"), regex_calls)
        self.assertIn(("compute", "helper"), ts_calls)


if __name__ == "__main__":
    unittest.main()
