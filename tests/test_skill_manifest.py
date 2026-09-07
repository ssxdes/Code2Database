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


if __name__ == "__main__":
    unittest.main()
