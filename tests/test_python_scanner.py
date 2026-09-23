"""Python scanner direct coverage.

The scanner is a first-class language (ctypes FFI source, threading
detection) but had zero direct tests — only indirect API-entry
coverage. These tests pin class nodes, decorator labels, ctor/dtor
labels, threading detection, IMPLEMENTS edges and lambda visibility.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


def _scan_py(code):
    from _scanner.python_scanner import PythonTreeSitterScanner
    scanner = PythonTreeSitterScanner()
    with tempfile.NamedTemporaryFile(suffix='.py', mode='w',
                                     delete=False) as f:
        f.write(code)
        f.flush()
        result = scanner.scan_file(f.name, source_root=os.path.dirname(f.name))
    os.unlink(f.name)
    return result


_CODE = '''\
import threading

class Base:
    pass

class Service(Base):
    def __init__(self, name: str):
        self.name = name

    def __del__(self):
        close_all()

    @staticmethod
    def util():
        pass

    @property
    def count(self) -> int:
        return 1

    def run(self):
        t = threading.Thread(target=self.worker)
        t.start()
        alias = lambda: nested_call()

    def worker(self):
        pass

def helper(x):
    return x

def close_all():
    pass

def nested_call():
    pass
'''


class TestPythonClassModel(unittest.TestCase):

    def test_class_nodes(self):
        result = _scan_py(_CODE)
        by_name = {f["name"]: f for f in result["functions"]}
        self.assertEqual(by_name["Base"].get("labels"), ["class"])
        self.assertIn("class", by_name["Service"]["labels"])

    def test_extends_emits_implements(self):
        result = _scan_py(_CODE)
        impl = [e for e in result.get("edges", [])
                if e.get("relation") == "IMPLEMENTS"]
        pairs = {(e.get("source"), e.get("target")) for e in impl}
        self.assertIn(("root_service", "base"), pairs)

    def test_ctor_dtor_labels(self):
        result = _scan_py(_CODE)
        by_name = {f["name"]: f for f in result["functions"]}
        self.assertIn("constructor", by_name["Service.__init__"]["labels"])
        self.assertIn("destructor", by_name["Service.__del__"]["labels"])

    def test_decorator_labels(self):
        result = _scan_py(_CODE)
        by_name = {f["name"]: f for f in result["functions"]}
        self.assertIn("@staticmethod", by_name["Service.util"]["labels"])
        self.assertIn("static_method", by_name["Service.util"]["labels"])
        self.assertIn("@property", by_name["Service.count"]["labels"])

    def test_return_annotation_in_signature(self):
        result = _scan_py(_CODE)
        by_name = {f["name"]: f for f in result["functions"]}
        self.assertIn("int", by_name["Service.count"].get("signature", ""))


class TestPythonCallsAndConcurrency(unittest.TestCase):

    def test_threading_labels_the_starter(self):
        result = _scan_py(_CODE)
        by_name = {f["name"]: f for f in result["functions"]}
        self.assertIn("thread_processor", by_name["Service.run"]["labels"])

    def test_calls_extracted_with_lambda_body(self):
        result = _scan_py(_CODE)
        by_name = {f["name"]: f for f in result["functions"]}
        run = by_name["Service.run"]
        callees = {a["callee"] for a in run.get("callee_args", [])}
        # direct calls + the lambda's call, all attributed to run
        self.assertIn("Thread", callees)
        self.assertIn("start", callees)
        self.assertIn("nested_call", callees)

    def test_import_edge(self):
        result = _scan_py(_CODE)
        imports = [e for e in result.get("import_edges", [])
                   if e.get("relation") == "IMPORTS"]
        targets = {e.get("target") for e in imports}
        self.assertIn("threading", targets)

    def test_nested_function_definitions_recursed(self):
        code = ("def outer():\n"
                "    def inner():\n"
                "        deep()\n"
                "    return inner\n"
                "def deep():\n"
                "    pass\n")
        result = _scan_py(code)
        names = {f["name"] for f in result["functions"]}
        self.assertIn("outer", names)
        self.assertIn("inner", names)
        inner = next(f for f in result["functions"] if f["name"] == "inner")
        callees = {a["callee"] for a in inner.get("callee_args", [])}
        self.assertIn("deep", callees)


if __name__ == "__main__":
    unittest.main()
