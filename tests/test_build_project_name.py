"""cmd_build persists the derived project name into master.json.

_derive_project_name computes the name inside build_graph for its FQN
prefix only; it never reached the split stage, so master.json carried
no project_name and downstream briefs rendered "(unnamed project)" on
build-multi projects whose source_root is empty.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts')
BUILDER = os.path.join(SCRIPTS_DIR, 'code2database_builder.py')

_BUILD_MEM_FLAGS = ["--memory-warn-threshold", "0.99",
                    "--memory-crit-threshold", "0.999"]


def _make_extraction(path, project, source_file="mod.c"):
    functions = [
        {"id": f"root_fn{i}", "name": f"fn{i}", "domain": "root",
         "source_file": source_file, "line_number": i + 1,
         "signature": f"int fn{i}(void)", "labels": [],
         "body_text": "return 0;"}
        for i in range(3)
    ]
    extraction = {
        "functions": functions,
        "edges": [],
        "source_root": "",
        "project": project,
        "files": [{"path": source_file, "language": "c"}],
        "stats": {"total_functions": len(functions)},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(extraction, f)


class TestBuildRecordsProjectName(unittest.TestCase):

    def _build(self, d, project, source_file="mod.c"):
        extraction = os.path.join(d, "extraction.json")
        _make_extraction(extraction, project, source_file)
        outdir = os.path.join(d, "out")
        env = dict(os.environ, PYTHONPATH=SCRIPTS_DIR)
        proc = subprocess.run(
            [sys.executable, BUILDER, "build",
             "--extraction", extraction,
             "--outdir", outdir,
             "--storage", "json",
             "--no-auto-enhance"] + _BUILD_MEM_FLAGS,
            capture_output=True, text=True, timeout=600, env=env)
        self.assertEqual(
            proc.returncode, 0,
            "build crashed:\n%s\n%s" % (proc.stdout[-3000:],
                                        proc.stderr[-3000:]))
        master_path = os.path.join(outdir, "code2database_master.json")
        self.assertTrue(os.path.exists(master_path), "master.json missing")
        with open(master_path, encoding="utf-8") as f:
            return json.load(f)

    def test_named_project_reaches_master(self):
        with tempfile.TemporaryDirectory() as d:
            master = self._build(d, "libstorage")
            self.assertEqual(master.get("project_name", None), "libstorage")

    def test_unnamed_project_stays_empty_string(self):
        """No extraction 'project' key and a root-level source file leave
        the field an empty string — the literal "project" fallback of
        _derive_project_name must not be persisted, so downstream
        fallbacks (source root, graph-dir parent) still apply."""
        with tempfile.TemporaryDirectory() as d:
            master = self._build(d, "")
            self.assertEqual(master.get("project_name", None), "")

    def test_directory_name_derived_from_source_file(self):
        """Without a 'project' key the name comes from the first source
        file's directory and is kept as-is."""
        with tempfile.TemporaryDirectory() as d:
            master = self._build(d, "", source_file="lib/storage/a.c")
            self.assertEqual(master.get("project_name", None), "storage")


if __name__ == "__main__":
    unittest.main()
