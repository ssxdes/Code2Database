"""Scanner-level vtable registration extraction.

Designated initializers (static const struct file_operations my_fops =
{ .open = my_open, ... }) are the flagship dispatch pattern; the
builder side had hand-crafted dict tests but the scanner extraction
(field names, positions, conditional arms) was never asserted against
a real file.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


def _scan_c(code):
    from _scanner.c_scanner import CTreeSitterScanner
    scanner = CTreeSitterScanner()
    with tempfile.NamedTemporaryFile(suffix='.c', mode='w',
                                     delete=False) as f:
        f.write(code)
        f.flush()
        result = scanner.scan_file(f.name, source_root=os.path.dirname(f.name))
    os.unlink(f.name)
    return result


_CODE = """\
static const struct file_operations my_fops = {
    .owner = THIS_MODULE,
    .open = my_open,
    .read = my_read,
    .write = my_write,
};

static int my_open(struct inode *i, struct file *f) { return 0; }
static ssize_t my_read(struct file *f, char *b, size_t n) { return 0; }
static ssize_t my_write(struct file *f, const char *b, size_t n) { return 0; }
"""

_COND_CODE = """\
static const struct ops cond_ops = {
#ifdef FEATURE_A
    .start = early_start,
#else
    .start = late_start,
#endif
    .stop = plain_stop,
};

void early_start(void) {}
void late_start(void) {}
void plain_stop(void) {}
"""


class TestVtableRegistrations(unittest.TestCase):

    def test_fields_and_functions_recorded(self):
        result = _scan_c(_CODE)
        regs = result.get("vtable_registrations")
        self.assertEqual(len(regs), 1)
        reg = regs[0]
        self.assertEqual(reg["struct_type"], "file_operations")
        self.assertEqual(reg["var_name"], "my_fops")
        by_field = {r["field"]: r["func_name"] for r in reg["registrations"]}
        self.assertEqual(by_field.get("open"), "my_open")
        self.assertEqual(by_field.get("read"), "my_read")
        self.assertEqual(by_field.get("write"), "my_write")
        # THIS_MODULE is not a function in this file — filtered out
        self.assertNotIn("owner", by_field)

    def test_registration_positions_recorded(self):
        result = _scan_c(_CODE)
        reg = result["vtable_registrations"][0]
        for r in reg["registrations"]:
            self.assertGreater(r["line"], 0)
            self.assertGreater(r["column"], 0)
            self.assertGreaterEqual(r["start_byte"], 0)
            self.assertGreater(r["end_byte"], r["start_byte"])

    def test_conditional_registration_arms(self):
        result = _scan_c(_COND_CODE)
        regs = result.get("vtable_registrations")
        self.assertEqual(len(regs), 1)
        reg = regs[0]
        by_field = {}
        for r in reg["registrations"]:
            by_field.setdefault(r["field"], []).append(
                (r["func_name"], r.get("condition", "")))
        # .start has two arms, one per preprocessor branch
        starts = by_field.get("start", [])
        self.assertEqual(
            {(fn, cond) for fn, cond in starts},
            {("early_start", "FEATURE_A"),
             ("late_start", "!(FEATURE_A)")})
        # unconditional field has no condition
        self.assertIn(("plain_stop", ""),
                      [(fn, cond) for fn, cond in by_field.get("stop", [])])


if __name__ == "__main__":
    unittest.main()
