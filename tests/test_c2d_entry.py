"""Tests for the c2d umbrella command (flow layer).

Covers three layers:
1. Registry integrity — every recipe step targets a real builder
   subcommand, no step is a write command, placeholders are covered,
   names/examples/patterns are present and unique.
2. Classification — question -> recipe mapping with positional param
   extraction, deterministic tie-breaking, no-match returns None.
3. Engine + CLI — placeholder resolution, optional-step skipping,
   write-command refusal, dry-run translation, JSON summary, and the
   verbs/recipes listing actions.
"""
import argparse
import io
import json
import os
import re
import sys
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.flow import entry, recipes  # noqa: E402
from _builder.flow.recipes import RECIPES, classify_question, get_recipe  # noqa: E402


def _ns(**kw):
    base = dict(action="ask", graph="code2db-out", question="", recipe="",
                target="", from_node="", to_node="", query="", source="",
                dry_run=False, json=False)
    base.update(kw)
    return argparse.Namespace(**base)


class TestRegistryIntegrity(unittest.TestCase):
    """The recipe registry must stay wired to the real CLI."""

    @classmethod
    def setUpClass(cls):
        from tests.test_skill_manifest import _builder_commands
        cls.builder_commands = _builder_commands()

    def test_recipe_names_unique_and_present(self):
        names = [r["name"] for r in RECIPES]
        self.assertEqual(len(names), len(set(names)),
                         "duplicate recipe names")
        for r in RECIPES:
            self.assertTrue(r.get("summary"),
                            "recipe %s missing summary" % r["name"])
            self.assertTrue(r.get("example"),
                            "recipe %s missing example" % r["name"])
            self.assertTrue(r.get("patterns"),
                            "recipe %s missing patterns" % r["name"])
            self.assertTrue(r.get("steps"),
                            "recipe %s missing steps" % r["name"])

    def test_every_step_targets_a_real_command(self):
        ghosts = set()
        for r in RECIPES:
            for step in r["steps"]:
                if step["cmd"] not in self.builder_commands:
                    ghosts.add(step["cmd"])
        self.assertEqual(ghosts, set(),
                         "recipe steps reference non-existent commands: %s"
                         % sorted(ghosts))

    def test_no_step_is_a_write_command(self):
        writers = set()
        for r in RECIPES:
            for step in r["steps"]:
                if step["cmd"] in entry.WRITE_COMMANDS:
                    writers.add("%s:%s" % (r["name"], step["cmd"]))
        self.assertEqual(writers, set(),
                         "recipe steps include write commands: %s"
                         % sorted(writers))

    def test_no_step_carries_a_write_flag(self):
        offenders = []
        for r in RECIPES:
            for step in r["steps"]:
                for a in step.get("args", []):
                    if a in entry.WRITE_FLAGS:
                        offenders.append("%s:%s:%s"
                                         % (r["name"], step["cmd"], a))
        self.assertEqual(offenders, [],
                         "recipe steps carry write flags: %s" % offenders)

    def test_placeholders_are_known_and_covered(self):
        for r in RECIPES:
            requires = set(r["requires"])
            for p in requires:
                self.assertIn(p, recipes.KNOWN_PARAMS,
                              "recipe %s requires unknown param %r"
                              % (r["name"], p))
            for step in r["steps"]:
                for token in step.get("args", []):
                    if token.startswith("{") and token.endswith("}"):
                        name = token[1:-1]
                        self.assertIn(name, recipes.KNOWN_PARAMS,
                                      "recipe %s uses unknown placeholder %r"
                                      % (r["name"], name))
                        # Non-optional steps may only use placeholders
                        # covered by requires; optional steps may use
                        # enrichment placeholders beyond it.
                        if not step.get("optional"):
                            self.assertIn(
                                name, requires,
                                "recipe %s step %s uses %r outside requires"
                                % (r["name"], step["cmd"], name))

    def test_patterns_compile(self):
        for r in RECIPES:
            for pat in r["patterns"]:
                try:
                    re.compile(pat)
                except re.error as exc:
                    self.fail("recipe %s pattern %r: %s"
                              % (r["name"], pat, exc))

    def test_get_recipe(self):
        self.assertIsNone(get_recipe("no-such-recipe"))
        self.assertIsNotNone(get_recipe("thread-safety"))


class TestClassification(unittest.TestCase):

    def test_thread_safety(self):
        recipe, params = classify_question("is bdev_start thread safe?")
        self.assertEqual(recipe["name"], "thread-safety")
        self.assertEqual(params["target"], "bdev_start")

    def test_thread_safety_variant(self):
        recipe, params = classify_question(
            "check the thread safety of nvme_submit_cmd")
        self.assertEqual(recipe["name"], "thread-safety")
        self.assertEqual(params["target"], "nvme_submit_cmd")

    def test_race_scan(self):
        recipe, params = classify_question("are there any data races?")
        self.assertEqual(recipe["name"], "race-scan")
        self.assertEqual(params, {})

    def test_impact(self):
        recipe, params = classify_question(
            "what breaks if I change util_sum?")
        self.assertEqual(recipe["name"], "impact")
        self.assertEqual(params["target"], "util_sum")

    def test_call_path(self):
        recipe, params = classify_question(
            "call chain from spdk_app_start to bdev_start")
        self.assertEqual(recipe["name"], "call-path")
        self.assertEqual(params, {"from": "spdk_app_start",
                                  "to": "bdev_start"})

    def test_value_origin(self):
        recipe, params = classify_question(
            "where does the req variable come from")
        self.assertEqual(recipe["name"], "value-origin")
        self.assertEqual(params["target"], "req")

    def test_invariants(self):
        recipe, params = classify_question(
            "what invariants does bdev_start enforce?")
        self.assertEqual(recipe["name"], "invariants")
        self.assertEqual(params["target"], "bdev_start")

    def test_provenance(self):
        recipe, params = classify_question(
            "which commit introduced bdev_start?")
        self.assertEqual(recipe["name"], "provenance")
        self.assertEqual(params["target"], "bdev_start")

    def test_resource(self):
        recipe, params = classify_question("who frees the io_buffer?")
        self.assertEqual(recipe["name"], "resource")
        self.assertEqual(params["target"], "io_buffer")

    def test_quality(self):
        recipe, _ = classify_question("run a quality scan for cycles")
        self.assertEqual(recipe["name"], "quality")

    def test_doc_alignment(self):
        recipe, _ = classify_question("is the documentation stale?")
        self.assertEqual(recipe["name"], "doc-alignment")

    def test_explore(self):
        recipe, params = classify_question(
            "explore the nvme queue handling architecture")
        self.assertEqual(recipe["name"], "explore")
        self.assertEqual(params["query"],
                         "nvme queue handling architecture")

    def test_no_match(self):
        recipe, params = classify_question("asdf jkl 12345 random")
        self.assertIsNone(recipe)
        self.assertEqual(params, {})

    def test_empty_question(self):
        self.assertEqual(classify_question(""), (None, {}))
        self.assertEqual(classify_question("   "), (None, {}))

    def test_deterministic_ties_prefer_earlier_recipe(self):
        # "race" wording could tempt several recipes; the same input
        # must always classify identically.
        for _ in range(3):
            recipe, _ = classify_question("concurrency risks overview")
            self.assertEqual(recipe["name"], "race-scan")


class TestResolveStepArgs(unittest.TestCase):

    def test_resolves_known_params(self):
        argv, unresolved = entry.resolve_step_args(
            ["--node", "{target}"], {"target": "foo"})
        self.assertEqual(argv, ["--node", "foo"])
        self.assertEqual(unresolved, [])

    def test_unresolved_kept_and_reported(self):
        argv, unresolved = entry.resolve_step_args(
            ["--node", "{target}", "--from", "{from}"],
            {"target": "foo"})
        self.assertEqual(argv, ["--node", "foo", "--from", "{from}"])
        self.assertEqual(unresolved, ["from"])

    def test_plain_tokens_untouched(self):
        argv, unresolved = entry.resolve_step_args(
            ["--direction", "reverse"], {})
        self.assertEqual(argv, ["--direction", "reverse"])
        self.assertEqual(unresolved, [])


class TestExecuteRecipe(unittest.TestCase):

    def _recipe(self, name):
        r = get_recipe(name)
        self.assertIsNotNone(r)
        return r

    def test_dry_run_prints_commands_without_executing(self):
        buf = io.StringIO()
        with mock.patch.object(entry.subprocess, "run") as run:
            with redirect_stdout(buf):
                rep = entry.execute_recipe(
                    self._recipe("thread-safety"),
                    {"target": "bdev_start"}, graph="gdir", dry_run=True)
        run.assert_not_called()
        self.assertEqual(rep["run"], 4)
        self.assertEqual(rep["failed"], 0)
        self.assertEqual(rep["skipped"], 0)
        out = buf.getvalue()
        self.assertIn("concurrency-analyze --node bdev_start --graph gdir",
                      out)
        self.assertIn("recipe complete: 4 steps", out)

    def test_optional_step_skipped_when_param_missing(self):
        buf = io.StringIO()
        with mock.patch.object(entry.subprocess, "run") as run:
            with redirect_stdout(buf):
                rep = entry.execute_recipe(
                    self._recipe("ffi"), {}, graph="gdir", dry_run=True)
        run.assert_not_called()
        # ffi-detect + ffi-list run; ffi-trace is optional and skipped
        self.assertEqual(rep["run"], 2)
        self.assertEqual(rep["skipped"], 1)
        skipped = [s for s in rep["steps"] if s.get("skipped")]
        self.assertIn("target", skipped[0]["skipped"])

    def test_write_command_step_refused(self):
        evil = {"name": "evil", "summary": "s", "requires": [],
                "example": "e", "patterns": [],
                "steps": [{"cmd": "update-node", "args": [],
                           "note": "should never run"}]}
        buf = io.StringIO()
        with mock.patch.object(entry.subprocess, "run") as run:
            with redirect_stdout(buf):
                rep = entry.execute_recipe(evil, {}, graph="gdir")
        run.assert_not_called()
        self.assertEqual(rep["skipped"], 1)
        self.assertIn("not read-only", rep["steps"][0]["skipped"])

    def test_write_flag_step_refused(self):
        evil = {"name": "evil2", "summary": "s", "requires": [],
                "example": "e", "patterns": [],
                "steps": [{"cmd": "extract-invariants",
                           "args": ["--apply"], "note": "write flag"}]}
        with mock.patch.object(entry.subprocess, "run") as run:
            rep = entry.execute_recipe(evil, {}, graph="gdir")
        run.assert_not_called()
        self.assertEqual(rep["skipped"], 1)

    def test_failed_step_warns_and_continues(self):
        recipe = {"name": "two", "summary": "s", "requires": [],
                  "example": "e", "patterns": [],
                  "steps": [
                      {"cmd": "impact", "args": ["--node", "x"],
                       "note": "fails"},
                      {"cmd": "neighbors", "args": ["--node", "x"],
                       "note": "still runs"},
                  ]}
        results = [mock.Mock(returncode=3), mock.Mock(returncode=0)]
        with mock.patch.object(entry.subprocess, "run",
                               side_effect=results) as run:
            rep = entry.execute_recipe(recipe, {}, graph="gdir")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(rep["failed"], 1)
        self.assertEqual(rep["run"], 1)

    def test_json_summary(self):
        recipe = {"name": "one", "summary": "s", "requires": [],
                  "example": "e", "patterns": [],
                  "steps": [{"cmd": "neighbors", "args": ["--node", "x"],
                             "note": "n"}]}
        buf = io.StringIO()
        with mock.patch.object(entry.subprocess, "run",
                               return_value=mock.Mock(returncode=0)):
            with redirect_stdout(buf):
                entry.execute_recipe(recipe, {}, graph="gdir",
                                     json_out=True)
        text = buf.getvalue()
        lines = text.splitlines()
        starts = [i for i, ln in enumerate(lines) if ln == "{"]
        parsed = json.loads("\n".join(lines[starts[-1]:]))
        self.assertEqual(parsed["recipe"], "one")
        self.assertEqual(parsed["run"], 1)


class TestActionAsk(unittest.TestCase):

    def test_explicit_recipe_with_flags(self):
        buf = io.StringIO()
        with mock.patch.object(entry, "execute_recipe") as ex:
            with redirect_stdout(buf):
                rc = entry._action_ask(
                    _ns(recipe="impact", target="util_sum"))
        self.assertEqual(rc, 0)
        ex.assert_called_once()
        recipe, params = ex.call_args[0][:2]
        self.assertEqual(recipe["name"], "impact")
        self.assertEqual(params["target"], "util_sum")

    def test_question_classification_and_explicit_override(self):
        with mock.patch.object(entry, "execute_recipe") as ex:
            rc = entry._action_ask(
                _ns(question="is bdev_start thread safe?",
                    target="other_fn"))
        self.assertEqual(rc, 0)
        params = ex.call_args[0][1]
        # explicit flag wins over extraction
        self.assertEqual(params["target"], "other_fn")

    def test_unknown_recipe_lists_available(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            rc = entry._action_ask(_ns(recipe="nope"))
        self.assertEqual(rc, 2)
        self.assertIn("unknown recipe", buf.getvalue())
        self.assertIn("thread-safety", buf.getvalue())

    def test_missing_required_params_reports_example(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            rc = entry._action_ask(_ns(recipe="impact"))
        self.assertEqual(rc, 2)
        err = buf.getvalue()
        self.assertIn("needs: target", err)
        self.assertIn("--target <value>", err)

    def test_no_question_no_recipe_is_usage_error(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            rc = entry._action_ask(_ns())
        self.assertEqual(rc, 2)
        self.assertIn("--question", buf.getvalue())

    def test_no_match_falls_back_to_intent_router(self):
        buf = io.StringIO()
        with mock.patch.object(entry.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            with redirect_stdout(buf):
                rc = entry._action_ask(_ns(question="who calls util_sum?"))
        self.assertEqual(rc, 0)
        run.assert_called_once()
        argv = run.call_args[0][0]
        self.assertIn("impact", argv)
        self.assertIn("--direction", argv)
        self.assertIn("reverse", argv)

    def test_no_match_at_all_points_to_recipes(self):
        from _builder.misc import intent_router
        buf = io.StringIO()
        with mock.patch.object(intent_router, "classify_intent",
                               return_value=None):
            with mock.patch.object(sys, "stderr", io.StringIO()):
                with redirect_stdout(buf):
                    rc = entry._action_ask(
                        _ns(question="zzz qqq xxx"))
        self.assertEqual(rc, 1)
        self.assertIn("c2d recipes", buf.getvalue())


class TestListingActions(unittest.TestCase):

    def test_recipes_listing_names_all(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = entry._action_recipes(_ns(action="recipes"))
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        for r in RECIPES:
            self.assertIn(r["name"], out)

    def test_recipes_detail_for_one(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = entry._action_recipes(
                _ns(action="recipes", recipe="thread-safety"))
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("recipe: thread-safety", out)
        self.assertIn("requires: target", out)
        self.assertIn("lock-coverage", out)

    def test_recipes_detail_unknown(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            rc = entry._action_recipes(
                _ns(action="recipes", recipe="nope"))
        self.assertEqual(rc, 2)

    def test_verbs_prints_lifecycle(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            entry.cmd_c2d(_ns(action="verbs"))
        out = buf.getvalue()
        self.assertIn("ask", out)
        self.assertIn("recipes", out)
        self.assertIn("c2d ask --question", out)

    def test_default_action_is_verbs(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            entry.cmd_c2d(SimpleNamespace(action=None))
        self.assertIn("lifecycle", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
