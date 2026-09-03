"""Tests for the project brief (knowledge/brief.json) module."""

import argparse
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.brief import (
    brief_path, load_brief, save_brief, compute_graph_stats,
    refresh_graph_stats, render_brief_prompt, brief_update,
    brief_extract, validate_brief,
    cmd_knowledge_brief, cmd_brief_update, cmd_brief_extract,
    cmd_brief_validate,
    SIZE_WARN_CHARS,
)


def _ns(**kw):
    return argparse.Namespace(**kw)


def _capture_call(fn, args):
    out, err = io.StringIO(), io.StringIO()
    ret, code = None, None
    with redirect_stdout(out), redirect_stderr(err):
        try:
            ret = fn(args)
        except SystemExit as e:
            code = e.code
    return ret, out.getvalue(), err.getvalue(), code


def _run(fn, args):
    ret, out, err, code = _capture_call(fn, args)
    assert code is None, f"unexpected SystemExit({code}); stderr={err}"
    return ret, out, err


def _make_graph_dir(n_nodes=3, n_edges=2):
    """Graph dir with a small JSON graph."""
    tmp = tempfile.mkdtemp(prefix="c2d_brief_")
    nodes = [{"id": f"n{i}", "name": f"n{i}", "source_file": "/tmp/x.c",
              "line": i + 1, "domain": "test", "labels": [],
              "is_empty": False} for i in range(n_nodes)]
    nodes.append({"id": "empty", "name": "empty", "source_file": "/tmp/x.c",
                  "line": 99, "domain": "test", "labels": [],
                  "is_empty": True})
    edges = [{"source": f"n{i}", "target": f"n{i+1}",
              "relation": "INVOKES", "confidence": "EXTRACTED"}
             for i in range(n_edges)]
    with open(os.path.join(tmp, "domain_test.json"), "w") as f:
        json.dump({"nodes": nodes, "edges": edges}, f)
    with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
        json.dump({"source_root": "/tmp",
                   "domains": {"test": "domain_test.json"}}, f)
    return tmp


class TestBriefIO(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.graph_dir = os.path.join(self.tmp.name, "graph")
        os.makedirs(self.graph_dir, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_missing_returns_none(self):
        self.assertIsNone(load_brief(self.graph_dir))

    def test_load_corrupt_returns_default_shape(self):
        os.makedirs(os.path.join(self.graph_dir, "knowledge"))
        Path_write = os.path.join(self.graph_dir, "knowledge", "brief.json")
        with open(Path_write, "w") as f:
            f.write("{not json")
        brief = load_brief(self.graph_dir)
        self.assertIsNotNone(brief)
        self.assertEqual(brief["hard_rules"], [])

    def test_save_load_roundtrip(self):
        brief = {"project": "SPDK", "one_liner": "storage SDK",
                 "description": "desc", "hard_rules": [
                     {"rule": "开启宏 X", "type": "macro"}],
                 "modes": [], "key_abstractions": [], "conventions": [],
                 "pitfalls": [], "query_paths": [], "must_know": "",
                 "graph_stats": {}}
        save_brief(self.graph_dir, brief)
        loaded = load_brief(self.graph_dir)
        self.assertEqual(loaded["project"], "SPDK")
        self.assertEqual(loaded["schema_version"], 1)
        self.assertTrue(loaded["updated_at"])


class TestGraphStats(unittest.TestCase):
    def test_compute_from_graph(self):
        graph_dir = _make_graph_dir(n_nodes=3, n_edges=2)
        stats = compute_graph_stats(graph_dir)
        # 3 non-empty nodes (is_empty excluded), 2 edges, 1 domain
        self.assertEqual(stats["nodes"], 3)
        self.assertEqual(stats["edges"], 2)
        self.assertEqual(stats["domains"], 1)
        import shutil
        shutil.rmtree(graph_dir, ignore_errors=True)

    def test_missing_graph(self):
        with tempfile.TemporaryDirectory() as td:
            stats = compute_graph_stats(td)
            self.assertEqual(stats["nodes"], 0)

    def test_refresh_updates_and_saves(self):
        graph_dir = _make_graph_dir()
        refresh_graph_stats(graph_dir)
        brief = load_brief(graph_dir)
        self.assertEqual(brief["graph_stats"]["nodes"], 3)
        import shutil
        shutil.rmtree(graph_dir, ignore_errors=True)


class TestRender(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.graph_dir = os.path.join(self.tmp.name, "graph")
        os.makedirs(self.graph_dir, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_render_missing_brief_message(self):
        rendered = render_brief_prompt(self.graph_dir)
        self.assertIn("No project brief found", rendered)

    def test_render_all_sections(self):
        brief = {
            "project": "SPDK", "one_liner": "storage perf kit",
            "description": "Userspace storage SDK.",
            "hard_rules": [{"rule": "强制开启 SPDK_CONFIG_PCI",
                            "type": "macro", "detail": "all bdevs need it",
                            "evidence": "meson.build:12"}],
            "modes": [{"name": "pcie", "when": "本地 NVMe 盘",
                       "differences": "kernel-bypass DMA"}],
            "key_abstractions": [{"name": "bdev", "role": "块设备抽象"}],
            "conventions": ["函数前缀 spdk_"],
            "pitfalls": ["不要在回调里阻塞"],
            "query_paths": ["查 bdev 注册: search --domain-filter bdev"],
            "must_know": "reactor 是单线程事件循环",
        }
        rendered = render_brief_prompt(self.graph_dir, brief)
        self.assertIn("# Project Brief: SPDK", rendered)
        self.assertIn("storage perf kit", rendered)
        self.assertIn("## Hard Rules (MUST follow)", rendered)
        self.assertIn("[macro] 强制开启 SPDK_CONFIG_PCI", rendered)
        self.assertIn("evidence: meson.build:12", rendered)
        self.assertIn("## Usage Modes (pick by scenario)", rendered)
        self.assertIn("**pcie**: use when 本地 NVMe 盘", rendered)
        self.assertIn("## Key Abstractions", rendered)
        self.assertIn("**bdev**: 块设备抽象", rendered)
        self.assertIn("## Conventions", rendered)
        self.assertIn("## Pitfalls", rendered)
        self.assertIn("## Suggested Query Paths", rendered)
        self.assertIn("## Must Know", rendered)

    def test_render_empty_sections_omitted(self):
        brief = {"project": "X", "one_liner": "", "description": "",
                 "hard_rules": [], "modes": [], "key_abstractions": [],
                 "conventions": [], "pitfalls": [], "query_paths": [],
                 "must_know": ""}
        rendered = render_brief_prompt(self.graph_dir, brief)
        self.assertNotIn("## Hard Rules", rendered)
        self.assertNotIn("## Usage Modes", rendered)


class TestBriefUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.graph_dir = os.path.join(self.tmp.name, "graph")
        os.makedirs(self.graph_dir, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_set_scalar(self):
        brief_update(self.graph_dir, set_field="one_liner",
                     set_value="my project")
        self.assertEqual(load_brief(self.graph_dir)["one_liner"],
                         "my project")

    def test_set_rejects_item_section(self):
        with self.assertRaises(ValueError):
            brief_update(self.graph_dir, set_field="hard_rules",
                         set_value="x")

    def test_add_dict_item(self):
        brief_update(self.graph_dir, add_section="hard_rules",
                     add_value='{"rule": "开启宏", "type": "macro"}')
        brief = load_brief(self.graph_dir)
        self.assertEqual(brief["hard_rules"][0]["rule"], "开启宏")

    def test_add_string_item(self):
        brief_update(self.graph_dir, add_section="pitfalls",
                     add_value="不要阻塞 reactor")
        self.assertEqual(load_brief(self.graph_dir)["pitfalls"],
                         ["不要阻塞 reactor"])

    def test_add_rejects_scalar_section(self):
        with self.assertRaises(ValueError):
            brief_update(self.graph_dir, add_section="project",
                         add_value="x")

    def test_add_rejects_non_object_for_dict_section(self):
        with self.assertRaises(ValueError):
            brief_update(self.graph_dir, add_section="modes",
                         add_value="just a string")

    def test_remove_by_index(self):
        brief_update(self.graph_dir, add_section="conventions",
                     add_value="c1")
        brief_update(self.graph_dir, add_section="conventions",
                     add_value="c2")
        brief_update(self.graph_dir, remove_section="conventions",
                     remove_index=0)
        self.assertEqual(load_brief(self.graph_dir)["conventions"], ["c2"])

    def test_remove_bad_index(self):
        with self.assertRaises(ValueError):
            brief_update(self.graph_dir, remove_section="conventions",
                         remove_index=5)

    def test_no_operation_raises(self):
        with self.assertRaises(ValueError):
            brief_update(self.graph_dir)

    def test_update_initializes_missing_brief(self):
        # update on a graph dir without brief creates it (with stderr note)
        brief_update(self.graph_dir, add_section="pitfalls",
                     add_value="p")
        self.assertEqual(load_brief(self.graph_dir)["pitfalls"], ["p"])


class TestExtractAndValidate(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph_dir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.graph_dir, ignore_errors=True)

    def test_extract_initializes_with_stats(self):
        brief = brief_extract(self.graph_dir)
        self.assertEqual(brief["graph_stats"]["nodes"], 3)
        self.assertTrue(os.path.exists(brief_path(self.graph_dir)))

    def test_extract_preserves_curated_content(self):
        brief_extract(self.graph_dir)
        brief_update(self.graph_dir, set_field="project",
                     set_value="SPDK")
        brief_extract(self.graph_dir)  # re-extract must not clobber
        self.assertEqual(load_brief(self.graph_dir)["project"], "SPDK")

    def test_validate_missing_brief(self):
        result = validate_brief(self.graph_dir)
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["errors"][0])

    def test_validate_ok(self):
        brief_extract(self.graph_dir)
        brief_update(self.graph_dir, set_field="project", set_value="P")
        brief_update(self.graph_dir, set_field="description",
                     set_value="A project.")
        brief_update(self.graph_dir, add_section="hard_rules",
                     add_value='{"rule": "r", "type": "macro"}')
        result = validate_brief(self.graph_dir)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["errors"], [])

    def test_validate_schema_errors(self):
        brief_extract(self.graph_dir)
        brief = load_brief(self.graph_dir)
        brief["hard_rules"] = [{"type": "macro"}]  # missing 'rule'
        brief["modes"] = [{"when": "x"}]           # missing 'name'
        save_brief(self.graph_dir, brief)
        result = validate_brief(self.graph_dir)
        self.assertFalse(result["ok"])
        self.assertTrue(any("hard_rules[0]" in e for e in result["errors"]))
        self.assertTrue(any("modes[0]" in e for e in result["errors"]))

    def test_validate_size_budget(self):
        brief_extract(self.graph_dir)
        brief_update(self.graph_dir, set_field="description",
                     set_value="x" * (SIZE_WARN_CHARS + 10))
        result = validate_brief(self.graph_dir)
        self.assertTrue(any("chars" in w for w in result["warnings"]))
        # over the error threshold
        brief_update(self.graph_dir, set_field="must_know",
                     set_value="y" * 4000)
        result = validate_brief(self.graph_dir)
        self.assertFalse(result["ok"])
        self.assertTrue(any("lean" in e for e in result["errors"]))

    def test_validate_graph_drift(self):
        brief_extract(self.graph_dir)
        # simulate drift: rewrite the graph much larger
        nodes = [{"id": f"n{i}", "name": f"n{i}",
                  "source_file": "/tmp/x.c", "line": i, "domain": "test",
                  "labels": [], "is_empty": False}
                 for i in range(50)]
        with open(os.path.join(self.graph_dir, "domain_test.json"), "w") as f:
            json.dump({"nodes": nodes, "edges": []}, f)
        result = validate_brief(self.graph_dir)
        self.assertTrue(any("drifted" in w for w in result["warnings"]))

    def test_validate_empty_content_warnings(self):
        brief_extract(self.graph_dir)
        result = validate_brief(self.graph_dir)
        self.assertTrue(any("'project' is empty" in w
                            for w in result["warnings"]))


class TestCliHandlers(unittest.TestCase):
    def setUp(self):
        self.graph_dir = _make_graph_dir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.graph_dir, ignore_errors=True)

    def test_extract_then_show(self):
        _, out, _ = _run(cmd_brief_extract, _ns(graph=self.graph_dir))
        self.assertIn("Brief template ready", out)
        _, out, _ = _run(cmd_knowledge_brief, _ns(graph=self.graph_dir,
                                                  json=False))
        self.assertIn("# Project Brief:", out)

    def test_show_json_flag(self):
        brief_extract(self.graph_dir)
        brief_update(self.graph_dir, set_field="project", set_value="PX")
        _, out, _ = _run(cmd_knowledge_brief, _ns(graph=self.graph_dir,
                                                  json=True))
        data = json.loads(out)
        self.assertEqual(data["project"], "PX")

    def test_show_missing_json_exits_1(self):
        _, _, _, code = _capture_call(
            cmd_knowledge_brief, _ns(graph=self.graph_dir, json=True))
        self.assertEqual(code, 1)

    def test_update_cli(self):
        brief_extract(self.graph_dir)
        _, out, _ = _run(cmd_brief_update, _ns(
            graph=self.graph_dir, set="one_liner", add=None, remove=None,
            index=None, value="one-liner text", refresh_stats=False))
        self.assertIn("Brief updated", out)
        self.assertEqual(load_brief(self.graph_dir)["one_liner"],
                         "one-liner text")

    def test_update_cli_add_json_item(self):
        brief_extract(self.graph_dir)
        _run(cmd_brief_update, _ns(
            graph=self.graph_dir, set=None,
            add="hard_rules", remove=None, index=None,
            value='{"rule": "宏 X 必须开", "type": "macro"}',
            refresh_stats=False))
        brief = load_brief(self.graph_dir)
        self.assertEqual(brief["hard_rules"][0]["rule"], "宏 X 必须开")

    def test_update_cli_bad_field_exits_1(self):
        brief_extract(self.graph_dir)
        _, _, _, code = _capture_call(cmd_brief_update, _ns(
            graph=self.graph_dir, set="bad_field", add=None, remove=None,
            index=None, value="x", refresh_stats=False))
        self.assertEqual(code, 1)

    def test_validate_cli_exit_codes(self):
        _, _, _, code = _capture_call(cmd_brief_validate,
                                      _ns(graph=self.graph_dir))
        self.assertEqual(code, 1)  # missing brief
        brief_extract(self.graph_dir)
        brief_update(self.graph_dir, set_field="project", set_value="P")
        brief_update(self.graph_dir, set_field="description",
                     set_value="d")
        _, out, _, code2 = _capture_call(cmd_brief_validate,
                                         _ns(graph=self.graph_dir))
        self.assertEqual(code2, None)
        self.assertIn("VALID", out)


if __name__ == "__main__":
    unittest.main()
