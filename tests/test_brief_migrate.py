"""M1 (2026-09-07 review): legacy knowledge/*.md → brief.json migration.

Commit 5e106a2 removed knowledge_manager.py (the MD knowledge system)
and replaced it with brief.json, but provided no migration path —
existing knowledge/*.md files were silently ignored. Projects with
curated MD knowledge lost access to it.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.kb.brief import migrate_from_legacy_knowledge, load_brief, save_brief


_LEGACY_ARCH = """\
# Architecture

The system uses a layered design: transport → protocol → application.
Each layer exposes a stable API via function pointers registered at init.
"""

_LEGACY_CONSTRAINTS = """\
# Constraints

- Must hold the global lock before calling submit_io
- Never access the device registers directly — use the registered ops
- All callbacks must be reentrant
"""

_LEGACY_GLOSSARY = """\
# Glossary

## bdev
A block device abstraction layer. Provides read/write/reset ops.

## io_channel
A per-thread I/O context. Pairs with a poller for async completion.
"""


class TestMigrateLegacyKnowledge(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph_dir = os.path.join(self.tmp.name, "graph")
        self.knowledge_dir = os.path.join(self.graph_dir, "knowledge")
        os.makedirs(self.knowledge_dir)

    def _write_md(self, name, content):
        with open(os.path.join(self.knowledge_dir, name), "w") as f:
            f.write(content)

    def test_migrates_all_sections(self):
        self._write_md("architecture.md", _LEGACY_ARCH)
        self._write_md("constraints.md", _LEGACY_CONSTRAINTS)
        self._write_md("glossary.md", _LEGACY_GLOSSARY)
        report = migrate_from_legacy_knowledge(self.graph_dir)
        brief = load_brief(self.graph_dir)
        self.assertIsNotNone(brief)
        self.assertIn("layered design", brief.get("description", ""))
        rules = brief.get("hard_rules", [])
        self.assertGreaterEqual(len(rules), 3)
        rule_texts = " ".join(r.get("rule", "") for r in rules)
        self.assertIn("global lock", rule_texts)
        self.assertIn("reentrant", rule_texts)
        abstractions = brief.get("key_abstractions", [])
        names = {a.get("name", "") for a in abstractions}
        self.assertIn("bdev", names)
        self.assertIn("io_channel", names)

    def test_no_md_files_is_noop(self):
        # brief.json doesn't exist, no .md files → empty report
        report = migrate_from_legacy_knowledge(self.graph_dir)
        self.assertEqual(report["migrated_files"], 0)
        self.assertIsNone(load_brief(self.graph_dir))

    def test_preserves_existing_brief_content(self):
        save_brief(self.graph_dir, {
            "schema_version": 1, "project": "myproj", "one_liner": "curated",
            "description": "hand-written desc", "must_know": "",
            "hard_rules": [{"rule": "existing rule", "type": "api",
                            "detail": "", "evidence": ""}],
            "modes": [], "key_abstractions": [{"name": "existing", "role": "x"}],
            "conventions": ["curated convention"], "pitfalls": [],
            "query_paths": [], "graph_stats": {}, "updated_at": "",
        })
        self._write_md("constraints.md", "- new constraint from migration\n")
        migrate_from_legacy_knowledge(self.graph_dir)
        brief = load_brief(self.graph_dir)
        self.assertEqual(brief["one_liner"], "curated")
        self.assertIn("hand-written desc", brief["description"])
        rules = brief.get("hard_rules", [])
        self.assertGreaterEqual(len(rules), 2)  # existing + migrated
        self.assertEqual(rules[0]["rule"], "existing rule")
        self.assertTrue(any("convention" in c
                            for c in brief.get("conventions", [])))

    def test_idempotent(self):
        self._write_md("constraints.md", "- rule one\n- rule two\n")
        migrate_from_legacy_knowledge(self.graph_dir)
        first_rules = len(load_brief(self.graph_dir)["hard_rules"])
        migrate_from_legacy_knowledge(self.graph_dir)
        second_rules = len(load_brief(self.graph_dir)["hard_rules"])
        self.assertEqual(first_rules, second_rules,
                         "second migration must not duplicate")

    def test_report_counts(self):
        self._write_md("architecture.md", _LEGACY_ARCH)
        self._write_md("glossary.md", _LEGACY_GLOSSARY)
        report = migrate_from_legacy_knowledge(self.graph_dir)
        self.assertEqual(report["migrated_files"], 2)
        self.assertGreater(report["items_added"], 0)


if __name__ == "__main__":
    unittest.main()
