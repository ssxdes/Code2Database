"""Regression tests: extract-knowledge must not clobber curated files.

Pins the extract -> apply -> re-extract lifecycle:
  - extract (auto_extracted) writes graph-inferred templates
  - apply-knowledge (llm_generated) curates them
  - a later extract-knowledge run must NOT overwrite the curated
    content with regenerated templates (it used to — silently, since
    cmd_extract_knowledge also dropped the _source tag and
    write_knowledge_files wrote unconditionally)

Also pins the provenance precedence fix: an intentional batch that
WRITES a file upgrades its recorded source (writer's provenance),
while files it didn't touch keep their records; explicit per-file
overrides beat everything.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.knowledge_manager import KnowledgeManager


class _TmpKm(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mgr = KnowledgeManager(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read(self, fname):
        return Path(self.tmpdir, "knowledge", fname).read_text(encoding="utf-8")

    def _sources(self):
        meta = Path(self.tmpdir, "knowledge", "_meta.json")
        return json.loads(meta.read_text(encoding="utf-8"))["file_sources"]

    def _write(self, knowledge):
        with contextlib.redirect_stderr(io.StringIO()):
            self.mgr.write_knowledge_files(knowledge)


class TestCuratedContentProtection(_TmpKm):
    def test_extract_apply_reextract_lifecycle(self):
        # 1. extract writes auto template
        self._write({"_source": "auto_extracted",
                     "architecture": "# auto arch v1"})
        self.assertEqual(self._read("architecture.md"), "# auto arch v1")
        self.assertEqual(self._sources()["architecture.md"],
                         "auto_extracted")
        # 2. apply curates
        self._write({"_source": "llm_generated",
                     "architecture": "# CURATED arch (hours of work)"})
        self.assertEqual(self._read("architecture.md"),
                         "# CURATED arch (hours of work)")
        self.assertEqual(self._sources()["architecture.md"],
                         "llm_generated")
        # 3. re-extract must not clobber
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.mgr.write_knowledge_files({
                "_source": "auto_extracted",
                "architecture": "# auto arch v2 REGENERATED"})
        self.assertEqual(self._read("architecture.md"),
                         "# CURATED arch (hours of work)")
        self.assertIn("Skipping architecture.md", err.getvalue())

    def test_auto_regenerates_auto_files(self):
        self._write({"_source": "auto_extracted",
                     "architecture": "# auto v1"})
        self._write({"_source": "auto_extracted",
                     "architecture": "# auto v2"})
        self.assertEqual(self._read("architecture.md"), "# auto v2")

    def test_unattributed_nonempty_file_protected(self):
        # A hand-written file with NO provenance record must not be
        # clobbered by an auto batch either.
        Path(self.tmpdir, "knowledge",
             "constraints.md").write_text("# hand-written", encoding="utf-8")
        self._write({"_source": "auto_extracted",
                     "constraints": "# auto c"})
        self.assertEqual(self._read("constraints.md"), "# hand-written")

    def test_intentional_batch_upgrades_only_written_files(self):
        self._write({"_source": "auto_extracted",
                     "architecture": "# auto a", "glossary": "# auto g"})
        # intentional batch writes ONLY architecture
        self._write({"_source": "llm_generated",
                     "architecture": "# curated a"})
        self.assertEqual(self._sources()["architecture.md"],
                         "llm_generated")
        # untouched glossary keeps its record
        self.assertEqual(self._sources()["glossary.md"], "auto_extracted")

    def test_explicit_override_beats_saved_record(self):
        self._write({"_source": "auto_extracted", "glossary": "# auto g"})
        self._write({"_source": "manual",
                     "_file_sources": {"glossary.md": "manual"},
                     "glossary": "# curated g"})
        self.assertEqual(self._sources()["glossary.md"], "manual")
        # and now auto batches skip it
        with contextlib.redirect_stderr(io.StringIO()):
            self.mgr.write_knowledge_files({
                "_source": "auto_extracted", "glossary": "# auto g2"})
        self.assertEqual(self._read("glossary.md"), "# curated g")


if __name__ == "__main__":
    unittest.main()
