"""The cgdb JSON fallbacks must read what real builds write.

master.json records domains as {name: "domains/<sub>/<file>.json"}
(string relpaths), and domain JSON stores functions as position-based
rows plus a separate function_details map. The fallback loaders in
cgdb_merge and cgdb_suggest assumed dict-shaped entries everywhere and
raised AttributeError on every real build, so the SQLite-less path
never returned any function.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.cgdb import cgdb_merge, cgdb_suggest


def _write_real_layout(root, functions, details):
    """Write a graph dir in the schema domain_split produces."""
    dom_rel = os.path.join("domains", "core.json")
    os.makedirs(os.path.join(root, "domains"), exist_ok=True)
    with open(os.path.join(root, dom_rel), "w", encoding="utf-8") as f:
        json.dump({"type": "code2database_domain", "domain": "core",
                   "functions": functions,
                   "function_details": details,
                   "empty_nodes": [], "edges": []}, f)
    with open(os.path.join(root, "code2database_master.json"),
              "w", encoding="utf-8") as f:
        # domains values are STRING relpaths in real builds
        json.dump({"type": "code2database_master",
                   "domains": {"core": dom_rel}}, f)


def _write_legacy_layout(root, functions):
    """Write a graph dir whose functions list still uses dict entries
    (the pre-position-row shape) and whose domains map is a dict."""
    with open(os.path.join(root, "domain_core.json"), "w",
              encoding="utf-8") as f:
        json.dump({"domain": "core", "functions": functions,
                   "edges": []}, f)
    with open(os.path.join(root, "code2database_master.json"),
              "w", encoding="utf-8") as f:
        json.dump({"domains": {"core": {"file": "domain_core.json"}}}, f)


class TestMergeLoader(unittest.TestCase):

    def test_real_layout_loads(self):
        functions = [
            ["core::f", "f", "a.c", 10, "[]", "void f()"],
            ["core::g", "g", "b.c", 20, "", "int g(void)"],
        ]
        with tempfile.TemporaryDirectory() as root:
            _write_real_layout(root, functions, {})
            funcs = cgdb_merge._load_graph_functions(root)
            self.assertEqual(len(funcs), 2)
            self.assertEqual(funcs["core::f"]["name"], "f")
            self.assertEqual(funcs["core::g"]["signature"], "int g(void)")
            self.assertEqual(funcs["core::f"]["domain"], "core")

    def test_legacy_dict_layout_still_loads(self):
        functions = [{"id": "core::f", "name": "f", "source_file": "a.c",
                      "signature": "void f()"}]
        with tempfile.TemporaryDirectory() as root:
            _write_legacy_layout(root, functions)
            funcs = cgdb_merge._load_graph_functions(root)
            self.assertEqual(len(funcs), 1)
            self.assertEqual(funcs["core::f"]["name"], "f")


class TestSuggestLoader(unittest.TestCase):

    def test_real_layout_loads(self):
        functions = [
            ["core::f", "f", "a.c", 10, '["API_entry"]', "void f()"],
            ["core::g", "g", "b.c", 20, "", "int g(void)"],
        ]
        details = {"core::f": {"semantic_desc": "main entry",
                               "is_empty": False}}
        with tempfile.TemporaryDirectory() as root:
            _write_real_layout(root, functions, details)
            funcs = cgdb_suggest._load_functions(root)
            self.assertEqual(len(funcs), 2)
            self.assertEqual(funcs["core::f"]["labels"], ["API_entry"])
            self.assertEqual(funcs["core::f"]["semantic_desc"],
                             "main entry")
            self.assertEqual(funcs["core::g"]["signature"], "int g(void)")

    def test_legacy_dict_layout_still_loads(self):
        functions = [{"id": "core::f", "name": "f", "source_file": "a.c",
                      "signature": "void f()", "labels": ["API_entry"],
                      "semantic_desc": "entry", "is_empty": False}]
        with tempfile.TemporaryDirectory() as root:
            _write_legacy_layout(root, functions)
            funcs = cgdb_suggest._load_functions(root)
            self.assertEqual(len(funcs), 1)
            self.assertEqual(funcs["core::f"]["labels"], ["API_entry"])
            self.assertEqual(funcs["core::f"]["semantic_desc"], "entry")


if __name__ == "__main__":
    unittest.main()
