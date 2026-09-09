"""skill.json / skill_analysis.json must reference only runnable commands.

S1 of the 2026-09-07 review: the manifests still referenced the four
commands deleted with the MD knowledge system (extract-knowledge,
apply-knowledge, knowledge-query, knowledge-validate) and were missing
every command added since (make, session-init, brief-*, build-update,
federate-*/fed-*). These manifests are what skill marketplaces and
agents read — a referenced command that cannot run is a runtime crash.

The test introspects the REAL argparse trees (builder + scanner) and
holds the manifests to them, so command drift fails CI instead of
users.
"""
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))


def _subcommands(entry_script: str) -> set:
    """Instantiate the CLI's parser and capture subparser choices."""
    import argparse
    spec = importlib.util.spec_from_file_location(
        "_manifest_probe_" + Path(entry_script).stem, REPO / entry_script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    captured = {}
    orig = argparse.ArgumentParser.parse_known_args

    def sniff(self, args=None, namespace=None):
        for act in self._actions:
            if isinstance(act, argparse._SubParsersAction):
                captured.setdefault('subs', set()).update(act.choices)
        return orig(self, args, namespace)

    argparse.ArgumentParser.parse_known_args = sniff
    old_argv = sys.argv[:]
    try:
        sys.argv = [entry_script, "--help"]
        try:
            mod.main()
        except SystemExit:
            pass
    finally:
        argparse.ArgumentParser.parse_known_args = orig
        sys.argv = old_argv
    return captured.get('subs', set())


def _builder_commands() -> set:
    return _subcommands("scripts/code2database_builder.py")


def _scanner_commands() -> set:
    return _subcommands("scripts/code2database_scanner.py")


def _runnable() -> set:
    return _builder_commands() | _scanner_commands()


class TestSkillManifest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.skill = json.loads((REPO / "skill.json").read_text())
        cls.analysis = json.loads((REPO / "skill_analysis.json").read_text())
        cls.ops = json.loads((REPO / "skill_ops.json").read_text())
        cls.builder = _builder_commands()
        cls.scanner = _scanner_commands()
        cls.runnable = cls.builder | cls.scanner

    def test_parsers_were_introspected(self):
        self.assertGreater(len(self.builder), 200,
                           "builder introspection failed")
        self.assertGreaterEqual(len(self.scanner), 5,
                                "scanner introspection failed")

    def test_skill_json_commands_are_exactly_the_builder_cli(self):
        declared = set(self.skill["commands"])
        self.assertEqual(declared, self.builder,
                         "skill.json commands != builder subcommands; "
                         "ghosts: %s, missing: %s"
                         % (sorted(declared - self.builder),
                            sorted(self.builder - declared)))

    def test_skill_json_scanner_commands_match_scanner_cli(self):
        declared = set(self.skill.get("scanner_commands", []))
        self.assertEqual(declared, self.scanner,
                         "scanner_commands != scanner subcommands; "
                         "ghosts: %s, missing: %s"
                         % (sorted(declared - self.scanner),
                            sorted(self.scanner - declared)))

    def test_skill_json_tier_1_runnable(self):
        ghosts = set(self.skill["tier_1_commands"]) - self.runnable
        self.assertEqual(ghosts, set(), f"tier_1 ghosts: {sorted(ghosts)}")

    def test_skill_json_total_commands_matches(self):
        self.assertEqual(self.skill["total_commands"],
                         len(self.skill["commands"]))

    def test_analysis_manifest_commands_runnable(self):
        ghosts = set(self.analysis["commands"]) - self.runnable
        self.assertEqual(ghosts, set(), f"analysis ghosts: {sorted(ghosts)}")

    def test_analysis_tier_1_runnable(self):
        ghosts = set(self.analysis["tier_1_commands"]) - self.runnable
        self.assertEqual(ghosts, set(), f"analysis tier_1 ghosts: "
                                        f"{sorted(ghosts)}")

    def test_analysis_on_demand_commands_runnable(self):
        ghosts = set(self.analysis.get("on_demand_commands", [])) \
            - self.runnable
        self.assertEqual(ghosts, set(),
                         f"on_demand ghosts: {sorted(ghosts)}")

    def test_builder_command_count_is_241(self):
        """Pin the builder subcommand count — docs reference this number."""
        self.assertEqual(len(self.builder), 241,
                         "Builder subcommand count drifted from 241; "
                         "update SKILL.md/AGENTS.md to match: %d"
                         % len(self.builder))

    def test_scanner_command_count_is_8(self):
        """Pin the scanner subcommand count."""
        self.assertEqual(len(self.scanner), 8,
                         "Scanner subcommand count drifted from 8: %d"
                         % len(self.scanner))

    def test_mcp_tool_counts_match_docs(self):
        """MCP tool counts must match the documented 83 total."""
        from _builder.mcp.mcp_server import TOOLS, TOOLS_REPORT
        c2d = sum(1 for k in TOOLS if k.startswith("code2database_"))
        cgdb = sum(1 for k in TOOLS if k.startswith("cgdb_"))
        self.assertEqual(c2d, 36,
                         "code2database_* tool count drifted from 36: %d"
                         % c2d)
        self.assertEqual(cgdb, 19,
                         "cgdb_* tool count drifted from 19: %d"
                         % cgdb)
        self.assertEqual(len(TOOLS_REPORT), 28,
                         "report tool count drifted from 28: %d"
                         % len(TOOLS_REPORT))
        self.assertEqual(len(TOOLS), 83,
                         "total TOOLS count drifted from 83: %d"
                         % len(TOOLS))

    # ---- session-init must be in tier_1_commands ----
    def test_skill_json_tier_1_includes_session_init(self):
        """session-init is the mandatory first step; must be tier_1."""
        self.assertIn("session-init", self.skill["tier_1_commands"],
                      "session-init must be in tier_1_commands")

    def test_skill_json_tier_1_subset_of_commands(self):
        """tier_1_commands must be a subset of commands."""
        ghosts = set(self.skill["tier_1_commands"]) - set(self.skill["commands"])
        self.assertEqual(ghosts, set(),
                         f"skill.json tier_1 not in commands: {sorted(ghosts)}")

    # ---- ops manifest sync ----
    def test_ops_manifest_commands_runnable(self):
        ghosts = set(self.ops["commands"]) - self.runnable
        self.assertEqual(ghosts, set(), f"ops ghosts: {sorted(ghosts)}")

    def test_ops_tier_1_subset_of_commands(self):
        """tier_1_commands must be a subset of commands."""
        ghosts = set(self.ops["tier_1_commands"]) - set(self.ops["commands"])
        self.assertEqual(ghosts, set(),
                         f"ops tier_1 not in commands: {sorted(ghosts)}")

    def test_ops_tier_1_runnable(self):
        ghosts = set(self.ops["tier_1_commands"]) - self.runnable
        self.assertEqual(ghosts, set(), f"ops tier_1 ghosts: {sorted(ghosts)}")

    def test_ops_includes_brief_and_kb_commands(self):
        """brief-* and kb-* commands belong to ops domain."""
        ops_cmds = set(self.ops["commands"])
        for c in ["brief-extract", "brief-validate", "brief-suggest",
                  "brief-migrate-legacy", "brief-update",
                  "kb-migrate", "kb-audit", "kb-cluster", "kb-conflict",
                  "kb-forget", "kb-global-add", "kb-known-unknowns",
                  "kb-rebuild-index", "kb-rollback",
                  "kb-global-search", "kb-global-import",
                  "kb-global-share-memory", "kb-global-search-memory",
                  "kb-global-import-memory",
                  "build-update", "quick-update", "heuristic-enhance",
                  "apply-semantics", "apply-invariants", "ffi-types",
                  "rollback-db-transaction", "commit-db-transaction"]:
            self.assertIn(c, ops_cmds,
                          f"ops missing {c}")

    # ---- analysis manifest sync ----
    def test_analysis_includes_flow_and_search_commands(self):
        """analysis-domain commands must be in manifest."""
        ana_cmds = set(self.analysis["commands"])
        for c in ["field-flow", "null-source", "path-guards", "runtime-guards",
                  "taint-analysis", "explore-flow", "key-paths", "code-slice",
                  "hybrid-search", "semantic-search", "reverse-trace"]:
            self.assertIn(c, ana_cmds,
                          f"analysis missing {c}")


if __name__ == "__main__":
    unittest.main()
