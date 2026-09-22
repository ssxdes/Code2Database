"""Family umbrellas: `tx begin` == `tx-begin`, legacy stays parseable.

The 263-command surface was collapsed into ~120 visible commands via
umbrella families (tx, kb, cgdb, daemon, ...). Two invariants must hold
forever:

1. **Shim equivalence**: `<family> <action> --help` must print exactly
   what the legacy `<legacy-command> --help` prints — the rewrite in
   main() must be transparent for every one of the 155 legacy names.
2. **Hidden but alive**: every legacy spelling still parses (choices
   membership) even though --help no longer lists it.

A regression in either direction (a rename that drops a legacy name, or
an umbrella action pointing at a ghost) fails here before it reaches
users' scripts.
"""
import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO / "scripts"
BUILDER = SCRIPTS_DIR / "code2database_builder.py"
sys.path.insert(0, str(SCRIPTS_DIR))


def _load_builder_module():
    spec = importlib.util.spec_from_file_location(
        "_umbrella_probe_builder", BUILDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestFamilyTables(unittest.TestCase):
    """Static invariants of the umbrella tables themselves."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_builder_module()
        cls.families = cls.mod._FAMILY_UMBRELLAS
        cls.legacy = cls.mod._umbrella_legacy_names()

    def test_every_family_has_actions(self):
        for fam, actions in self.families.items():
            self.assertTrue(actions, f"family {fam!r} has no actions")

    def test_no_family_collides_with_a_legacy_target(self):
        targets = set()
        for actions in self.families.values():
            targets.update(actions.values())
        collisions = set(self.families) & targets
        self.assertEqual(collisions, set(),
                         f"family name doubles as a legacy target: {collisions}")

    def test_shim_rewrites_every_action(self):
        apply = self.mod._apply_family_umbrella
        for fam, actions in self.families.items():
            for action, legacy in actions.items():
                got = apply([fam, action, "--graph", "X"])
                self.assertEqual(
                    got, [legacy, "--graph", "X"],
                    f"shim mismatch: {fam} {action} -> {got}")

    def test_umbrella_display_roundtrip_for_every_legacy_name(self):
        """Display form must execute identically to the legacy spelling.

        For every hidden name n: umbrella_argv(n) tokens, when passed
        through the argv rewrite, collapse back to exactly [n]. This is
        what lets the c2d recipes and the intent router render AND
        execute the umbrella spelling.
        """
        from _builder.umbrella import umbrella_argv, umbrella_display
        apply = self.mod._apply_family_umbrella
        for name in sorted(self.legacy):
            display = umbrella_display(name)
            self.assertNotEqual(display, name,
                                f"{name} has no umbrella spelling")
            self.assertEqual(
                apply(umbrella_argv(name) + ["--graph", "X"]),
                [name, "--graph", "X"],
                f"umbrella roundtrip failed for {name} ({display})")

    def test_umbrella_display_leaves_visible_commands_alone(self):
        from _builder.umbrella import umbrella_display
        for visible in ["impact", "path", "query", "describe-node",
                        "session-init", "make", "value-flow",
                        "lock-coverage", "path-feasible", "blast-radius",
                        "blame-node", "node-history", "find-commits",
                        "explore-flow", "key-paths", "field-access",
                        "code-slice", "doctor", "serve"]:
            self.assertEqual(umbrella_display(visible), visible)

    def test_shim_skips_global_flags_correctly(self):
        apply = self.mod._apply_family_umbrella
        # value-taking global flags before the family pair
        self.assertEqual(
            apply(["--log-level", "DEBUG", "kb", "query", "--q", "x"]),
            ["--log-level", "DEBUG", "kb-query", "--q", "x"])
        self.assertEqual(
            apply(["--log-file", "f.log", "--log-json", "daemon", "logs"]),
            ["--log-file", "f.log", "--log-json", "daemon-logs"])
        # non-family command untouched, even with family-like words after
        self.assertEqual(
            apply(["describe-node", "--name", "tx"]),
            ["describe-node", "--name", "tx"])
        # family primary without an action is left for the umbrella parser
        self.assertEqual(apply(["tx"]), ["tx"])
        # unknown action is left for the umbrella parser to reject
        self.assertEqual(apply(["tx", "bogus"]), ["tx", "bogus"])
        # action-looking flag value must not trigger a rewrite
        self.assertEqual(
            apply(["search", "--keyword", "hybrid"]),
            ["search", "--keyword", "hybrid"])


class TestUmbrellaCliBehaviour(unittest.TestCase):
    """End-to-end subprocess checks (one per mechanism, not per name)."""

    def _run(self, *argv):
        return subprocess.run(
            [sys.executable, str(BUILDER), *argv],
            capture_output=True, text=True, timeout=120)

    def test_shim_equivalence_sample(self):
        """`<family> <action> --help` matches the legacy command's help."""
        # one new-primary family, one alias-primary family, one with
        # multi-word actions, one whose action name equals a flag value
        # elsewhere (import), and the biggest family (cgdb).
        for fam, action, legacy in [
            ("tx", "begin", "tx-begin"),
            ("daemon", "status", "daemon-status"),
            ("kb-global", "import-memory", "kb-global-import-memory"),
            ("kb-global", "import", "kb-global-import"),
            ("cgdb", "find-invokers", "cgdb-find-invokers"),
            ("memory", "manage", "manage-memory"),
            ("build", "update", "build-update"),
        ]:
            fam_proc = self._run(fam, action, "--help")
            legacy_proc = self._run(legacy, "--help")
            self.assertEqual(fam_proc.returncode, 0, fam_proc.stderr[:200])
            self.assertEqual(legacy_proc.returncode, 0, legacy_proc.stderr[:200])
            self.assertEqual(fam_proc.stdout, legacy_proc.stdout,
                             f"{fam} {action} --help != {legacy} --help")

    def test_global_flag_prefix_still_rewrites(self):
        proc = self._run("--log-level", "CRITICAL", "tx", "status")
        # tx-status has a required --graph, so argparse rejects with
        # rc=2 — but the error names the LEGACY command, proving the
        # rewrite fired (without it the umbrella parser would have
        # rejected the positional 'status' instead).
        self.assertIn("tx-status", proc.stdout + proc.stderr,
                      "rewrite did not fire past global flags")
        self.assertNotIn("umbrella", proc.stdout + proc.stderr)

    def test_bare_umbrella_prints_action_table(self):
        proc = self._run("tx")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("usage: tx <action>", proc.stderr)
        self.assertIn("tx begin", proc.stderr)
        self.assertIn("tx replay-wal", proc.stderr)
        self.assertIn("missing tx action", proc.stderr)

    def test_unknown_umbrella_action_rejected(self):
        proc = self._run("cgdb", "definitely-not-an-action")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unknown cgdb action", proc.stderr)
        self.assertIn("cgdb sql", proc.stderr)

    def test_hidden_legacy_still_parses(self):
        """Hidden names are invisible in --help but fully functional."""
        for legacy in ["tx-begin", "kb-query", "cgdb-get-source",
                       "daemon-status", "who-allocates", "ffi-trace"]:
            proc = self._run(legacy, "--help")
            self.assertEqual(proc.returncode, 0,
                             f"hidden legacy {legacy} no longer parses")

    def test_main_help_hides_legacy_and_lists_umbrellas(self):
        proc = self._run("--help")
        self.assertEqual(proc.returncode, 0)
        for fam in ["tx", "kb", "kb-global", "kb-domain", "foreign", "check",
                    "doc", "ffi", "who", "graph", "cgdb", "profile",
                    "embeddings", "memory", "pp", "token", "node", "writeback",
                    "fed", "invariants", "daemon", "brief", "trace",
                    "concurrency", "export", "build", "search"]:
            self.assertIn(f"\n    {fam} ", proc.stdout,
                          f"umbrella {fam} missing from --help")
        for legacy in ["tx-begin", "kb-query", "cgdb-query", "daemon-start",
                       "profile-health", "hybrid-search", "knowledge-brief",
                       "save-memory", "manage-memory", "find-macros"]:
            self.assertNotIn(f"\n    {legacy} ", proc.stdout,
                             f"legacy {legacy} should be hidden from --help")


if __name__ == "__main__":
    unittest.main()
