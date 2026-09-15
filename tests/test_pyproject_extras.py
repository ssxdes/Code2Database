"""Wheel extras: every optional capability is installable by name.

The core wheel keeps a minimal dependency tree; each optional runtime
capability (clang backend, path solver, communities, daemon watchers,
memory telemetry, streaming parse, neural embeddings) declares its own
extra. These pins keep the extras present, non-empty and consistent
with the requirements.txt optional section.
"""
import os
import re
import tomllib
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_EXTRAS = ("clang", "solver", "community", "daemon",
           "resources", "streaming", "neural")


class TestWheelExtras(unittest.TestCase):

    def setUp(self):
        with open(os.path.join(REPO, "pyproject.toml"), "rb") as fh:
            self.data = tomllib.load(fh)

    def test_all_extras_declared(self):
        extras = self.data["project"]["optional-dependencies"]
        for name in _EXTRAS:
            self.assertIn(name, extras)
            self.assertGreater(len(extras[name]), 0, name)

    def test_requirement_syntax(self):
        extras = self.data["project"]["optional-dependencies"]
        pattern = re.compile(r"^[A-Za-z0-9_.\-]+(>=[0-9][0-9.]*)?$")
        for name, deps in extras.items():
            for dep in deps:
                self.assertRegex(
                    dep, pattern, f"extra {name!r}: unpinned or malformed {dep!r}")

    def test_requirements_lists_the_same_capabilities(self):
        reqs = open(os.path.join(REPO, "scripts", "requirements.txt"),
                    encoding="utf-8").read()
        optional_block = reqs.split("Optional advanced features", 1)[1]
        for name in _EXTRAS:
            self.assertIn(name, optional_block,
                          f"extra {name!r} must appear in the requirements "
                          f"optional section")

    def test_core_dependencies_unchanged_by_extras(self):
        core = self.data["project"]["dependencies"]
        self.assertIn("networkx>=3.0", core)
        self.assertIn("tree-sitter>=0.22", core)
        optional_names = {
            d.split(">=")[0].strip()
            for deps in self.data["project"]["optional-dependencies"].values()
            for d in deps}
        self.assertNotIn("networkx", optional_names)
        self.assertNotIn("tree-sitter", optional_names)


if __name__ == "__main__":
    unittest.main()
