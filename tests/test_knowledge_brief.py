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

from _builder.kb.brief import (
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

    def test_save_brief_syncs_kb_paragraphs(self):
        """Audit issue 39 (MEDIUM): save_brief must sync the brief content
        to kb_paragraphs so kb-query / describe-node see the new knowledge
        immediately, without waiting for a manual kb-rebuild-index.
        """
        # Set up a graph_dir with a code2database.db so kb_index can
        # connect and create its tables.
        import sqlite3
        db_path = os.path.join(self.graph_dir, "code2database.db")
        conn = sqlite3.connect(db_path)
        conn.close()
        brief = {"project": "TestProj", "one_liner": "test",
                 "description": "a test project for kb sync",
                 "hard_rules": [
                     {"rule": "always lock before mutate", "type": "rule"}],
                 "modes": [], "key_abstractions": [], "conventions": [],
                 "pitfalls": ["don't free twice"], "query_paths": [],
                 "must_know": "critical invariant", "graph_stats": {}}
        save_brief(self.graph_dir, brief)
        # Verify the brief paragraphs landed in kb_paragraphs.
        from _builder.kb.kb_index import _kb_connect
        kb_conn = _kb_connect(self.graph_dir)
        self.assertIsNotNone(kb_conn, "kb_connect must succeed after save_brief")
        try:
            rows = kb_conn.execute(
                "SELECT title, body FROM kb_paragraphs "
                "WHERE source_file = 'brief.json' ORDER BY para_index"
            ).fetchall()
            titles = [r[0] for r in rows]
            bodies = [r[1] for r in rows]
            # Must include the description, must_know, hard_rule, pitfall
            # (titles are prefixed with [Project] by _brief_sections_as_paragraphs)
            self.assertTrue(any("Description" in t for t in titles),
                            f"Description missing from {titles}")
            self.assertTrue(any("Must Know" in t for t in titles),
                            f"Must Know missing from {titles}")
            self.assertTrue(any("Hard Rule" in t for t in titles),
                            f"Hard Rule missing from {titles}")
            self.assertTrue(any("Pitfall" in t for t in titles),
                            f"Pitfall missing from {titles}")
            self.assertIn("don't free twice", bodies)
        finally:
            kb_conn.close()

    def test_save_brief_replaces_stale_kb_paragraphs(self):
        """When save_brief is called a second time, the old paragraphs
        must be replaced (not duplicated). Audit issue 39 regression."""
        import sqlite3
        db_path = os.path.join(self.graph_dir, "code2database.db")
        conn = sqlite3.connect(db_path)
        conn.close()
        brief_v1 = {"project": "P", "description": "version 1",
                    "hard_rules": [], "modes": [], "key_abstractions": [],
                    "conventions": [], "pitfalls": [], "query_paths": [],
                    "must_know": "", "graph_stats": {}}
        save_brief(self.graph_dir, brief_v1)
        brief_v2 = {"project": "P", "description": "version 2",
                    "hard_rules": [], "modes": [], "key_abstractions": [],
                    "conventions": [], "pitfalls": [], "query_paths": [],
                    "must_know": "", "graph_stats": {}}
        save_brief(self.graph_dir, brief_v2)
        from _builder.kb.kb_index import _kb_connect
        kb_conn = _kb_connect(self.graph_dir)
        try:
            rows = kb_conn.execute(
                "SELECT body FROM kb_paragraphs "
                "WHERE source_file = 'brief.json' AND title LIKE '%Description%'"
            ).fetchall()
            # Must have exactly ONE Description row (v2 replaced v1).
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], "version 2")
        finally:
            kb_conn.close()


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

class TestAutoExtract(unittest.TestCase):
    """Tests for _auto_extract_from_graph — brief-extract auto-population."""

    def setUp(self):
        import sqlite3
        self.tmp = tempfile.TemporaryDirectory()
        self.graph_dir = os.path.join(self.tmp.name, "graph")
        os.makedirs(self.graph_dir, exist_ok=True)
        # Create a master.json (needed for project name + graph stats)
        with open(os.path.join(self.graph_dir,
                  "code2database_master.json"), "w") as f:
            json.dump({
                "source_root": "/tmp/testproj",
                "project_name": "TestProject",
                "domains": {"core": "domain_core.json"},
            }, f)
        # Create a domain JSON file for compute_graph_stats
        nodes = [
            {"id": "fn1", "name": "main_init", "source_file": "/tmp/x.c",
             "line": 1, "domain": "core", "labels": [],
             "is_empty": False},
            {"id": "fn2", "name": "dispatch", "source_file": "/tmp/x.c",
             "line": 10, "domain": "core", "labels": [],
             "is_empty": False},
            {"id": "fn3", "name": "handler", "source_file": "/tmp/x.c",
             "line": 20, "domain": "core", "labels": [],
             "is_empty": False},
        ]
        edges = [
            {"source": "fn1", "target": "fn2",
             "relation": "INVOKES", "confidence": "EXTRACTED"},
            {"source": "fn2", "target": "fn3",
             "relation": "INVOKES", "confidence": "EXTRACTED"},
        ]
        with open(os.path.join(self.graph_dir, "domain_core.json"), "w") as f:
            json.dump({"domain": "core", "nodes": nodes, "edges": edges}, f)
        # Create the SQLite DB with functions + edges tables
        db_path = os.path.join(self.graph_dir, "code2database.db")
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE functions (
                id TEXT PRIMARY KEY, name TEXT, domain TEXT,
                source_file TEXT, line_number INTEGER, signature TEXT,
                labels TEXT, body_text_compressed BLOB, extra_json TEXT,
                is_api_entry INTEGER DEFAULT 0,
                is_thread_processor INTEGER DEFAULT 0,
                is_callback_func INTEGER DEFAULT 0,
                is_out_end INTEGER DEFAULT 0,
                is_unknown_end INTEGER DEFAULT 0
            );
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoker_id TEXT, invoked_id TEXT, relation TEXT,
                call_order INTEGER, call_condition TEXT,
                concurrency TEXT, confidence TEXT,
                confidence_score REAL, source TEXT, evidence TEXT,
                invoked_arg_json TEXT, reg_args_json TEXT,
                vtable_type TEXT, vtable_bound_module TEXT
            );
        """)
        conn.execute(
            "INSERT INTO functions (id, name, domain, source_file, "
            "is_api_entry) VALUES (?, ?, ?, ?, ?)",
            ("fn1", "main_init", "core", "/tmp/x.c", 1))
        conn.execute(
            "INSERT INTO functions (id, name, domain, source_file, "
            "is_api_entry) VALUES (?, ?, ?, ?, ?)",
            ("fn2", "dispatch", "core", "/tmp/x.c", 0))
        conn.execute(
            "INSERT INTO functions (id, name, domain, source_file, "
            "is_api_entry) VALUES (?, ?, ?, ?, ?)",
            ("fn3", "handler", "core", "/tmp/x.c", 0))
        conn.execute(
            "INSERT INTO edges (invoker_id, invoked_id, relation) "
            "VALUES (?, ?, ?)", ("fn1", "fn2", "CALLS"))
        conn.execute(
            "INSERT INTO edges (invoker_id, invoked_id, relation) "
            "VALUES (?, ?, ?)", ("fn2", "fn3", "CALLS"))
        conn.execute(
            "INSERT INTO edges (invoker_id, invoked_id, relation, "
            "call_condition) VALUES (?, ?, ?, ?)",
            ("fn1", "fn3", "CALLS", "#ifdef CONFIG_X"))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_auto_extract_populates_project_and_description(self):
        brief = brief_extract(self.graph_dir)
        self.assertEqual(brief["project"], "TestProject")
        self.assertIn("[auto]", brief["one_liner"])
        self.assertIn("TestProject", brief["one_liner"])
        self.assertIn("[auto]", brief["description"])
        self.assertIn("3 functions", brief["description"])

    def test_auto_extract_key_abstractions(self):
        brief = brief_extract(self.graph_dir)
        # main_init is an API entry with 1 outgoing CALLS edge
        abstractions = brief.get("key_abstractions") or []
        self.assertTrue(len(abstractions) > 0)
        names = [a["name"] for a in abstractions]
        self.assertIn("main_init", names)

    def test_auto_extract_hard_rules_from_config(self):
        brief = brief_extract(self.graph_dir)
        rules = brief.get("hard_rules") or []
        self.assertTrue(any("CONFIG_X" in r["rule"] for r in rules))

    def test_auto_extract_query_paths(self):
        brief = brief_extract(self.graph_dir)
        paths = brief.get("query_paths") or []
        self.assertTrue(len(paths) > 0)
        self.assertTrue(any("main_init" in p for p in paths))

    def test_auto_extract_does_not_overwrite_existing(self):
        """Re-extracting on an existing brief must NOT clobber content."""
        brief_extract(self.graph_dir)  # creates with auto content
        brief_update(self.graph_dir, set_field="project",
                     set_value="MyCustomName")
        brief_extract(self.graph_dir)  # re-extract
        brief = load_brief(self.graph_dir)
        self.assertEqual(brief["project"], "MyCustomName")
        # Auto markers should be gone from one_liner since it was set
        # by the first extract and preserved
        self.assertNotIn("[auto]", brief.get("project", ""))


if __name__ == "__main__":
    unittest.main()
