"""Tests for the cgdb commands that had no test references.

Store-backed read commands (fixture = one custom IngestBatch covering
struct/field HAS_FIELD edges and a config-gated function):

- cgdb-definition: symbol lookup across function/var/field kinds
- cgdb-function-body: body_text of a function
- cgdb-struct-layout: struct node -> ordered HAS_FIELD layout
- cgdb-type-definition: type lookup by spelling
- cgdb-nodes-under-config: exact + substring config gating
- cgdb-path-feasible: CFG path feasibility (no-condition path)
- cgdb-cfg-paths: entry->exit path enumeration
- cgdb-coverage: --function / --file / summary modes
- cgdb-write-coverage: report files written into the graph dir

Cross-graph commands:

- cgdb-compare: new/removed/signature-changed functions + edge diff
- cgdb-merge-knowledge: memory entries transferred (dry-run vs write)
- cgdb-suggest: structured suggestions, error entry for empty graphs
- cgdb-tour: tour markdown written with function names
"""
import argparse
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.cgdb import cgdb_commands as cc
from _builder.cgdb.cgdb_store import SQLiteCGDBStore
from _builder.cgdb.cgdb_records import (
    IngestBatch, NodeRecord, EdgeRecord, TypeRecord, FileRecord,
    ConfigPredicateRecord, InvokeSiteRecord, OpsBindingRecord,
    BasicBlockRecord, CFGEdgeRecord, DataFlowRecord, AliasSetRecord,
    SyncPrimitiveRecord, HappensBeforeRecord, IncludeRecord,
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


def _json(out):
    return json.loads(out)


def _make_batch() -> IngestBatch:
    """Batch covering struct layout + config gating + CFG."""
    return IngestBatch(
        file=FileRecord(id=1, path='test.c', language='c', sha256='abc123',
                        content_hash='abc123'),
        nodes=[
            NodeRecord(id=1001, kind='function', name='foo', fqn='foo',
                       file_id=1, line=10, col=1, byte_start=100, byte_end=200,
                       attrs={'signature': 'int foo()', 'body_text': 'return 0;'}),
            NodeRecord(id=1002, kind='function', name='bar', fqn='bar',
                       file_id=1, line=20, col=1, byte_start=300, byte_end=400,
                       attrs={'signature': 'int bar()'}),
            NodeRecord(id=1003, kind='field', name='read_iter',
                       fqn='file_operations.read_iter',
                       file_id=1, line=5, col=12, byte_start=50, byte_end=70,
                       type_spelling='ssize_t (*)(struct file *, struct kiocb *, struct iovec *)'),
            NodeRecord(id=1004, kind='var', name='ext4_fop', fqn='ext4_fop',
                       file_id=1, line=8, col=20, byte_start=80, byte_end=90,
                       attrs={'struct_type': 'file_operations'}),
            NodeRecord(id=1005, kind='struct', name='file_operations',
                       fqn='file_operations',
                       file_id=1, line=1, col=8, byte_start=10, byte_end=40),
            NodeRecord(id=1006, kind='field', name='llseek',
                       fqn='file_operations.llseek',
                       file_id=1, line=3, col=12, byte_start=20, byte_end=45,
                       type_spelling='loff_t (*)(struct file *, loff_t, int)'),
            NodeRecord(id=1007, kind='function', name='gated_init',
                       fqn='gated_init', file_id=1, line=30, col=1,
                       byte_start=500, byte_end=600,
                       config_predicate_id=3001,
                       attrs={'signature': 'int gated_init(void)'}),
        ],
        edges=[
            EdgeRecord(src_id=1001, dst_id=1002, kind='INVOKES',
                       file_id=1, line=12, col=5),
            EdgeRecord(src_id=1003, dst_id=1001, kind='OPS_BIND',
                       file_id=1, line=8, col=20),
            EdgeRecord(src_id=1005, dst_id=1006, kind='HAS_FIELD',
                       file_id=1, line=3),
            EdgeRecord(src_id=1005, dst_id=1003, kind='HAS_FIELD',
                       file_id=1, line=5),
        ],
        types=[
            TypeRecord(id=2001, spelling='int', canonical_spelling='int',
                       kind='builtin', size_bytes=4),
            TypeRecord(id=2002, spelling='ssize_t',
                       canonical_spelling='long', kind='typedef',
                       size_bytes=8),
        ],
        config_predicates=[
            ConfigPredicateRecord(id=3001, text_form='defined(CONFIG_EXT4_FS)',
                                   z3_form='(defined CONFIG_EXT4_FS)',
                                   config_macros=['CONFIG_EXT4_FS']),
        ],
        ops_bindings=[
            OpsBindingRecord(edge_id=2, ops_table_id=1004,
                             field_node_id=1003, impl_function_id=1001,
                             signature_match=True),
        ],
        invoke_sites=[
            InvokeSiteRecord(invoker_id=1001, invoked_id=1002, invoke_kind='direct'),
        ],
        basic_blocks=[
            BasicBlockRecord(id=4001, function_id=1001, block_index=0,
                             is_entry=True),
            BasicBlockRecord(id=4002, function_id=1001, block_index=1,
                             is_exit=True),
        ],
        cfg_edges=[
            CFGEdgeRecord(function_id=1001, src_block_id=4001,
                          dst_block_id=4002, kind='fallthrough'),
        ],
        data_flow=[
            DataFlowRecord(function_id=1001, var_id=1004,
                           def_block_id=4001, use_block_id=4002,
                           kind='def_use'),
        ],
        alias_sets=[
            AliasSetRecord(ptr1_node_id=1004, ptr2_node_id=1004,
                           kind='must_alias'),
        ],
        sync_primitives=[
            SyncPrimitiveRecord(function_id=1001, kind='lock_acquire',
                                sync_var_id=1004),
        ],
        happens_before=[
            HappensBeforeRecord(write_event_id=1001, read_event_id=1002,
                                reason='lock'),
        ],
        includes=[
            IncludeRecord(source_file_id=1, included_path='stdio.h',
                          is_system=True),
        ],
    )


class _StoreFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph_dir = tempfile.mkdtemp(prefix="c2d_cgdbunc_")
        db_path = os.path.join(cls.graph_dir, "code2database.db")
        store = SQLiteCGDBStore(db_path)
        store.create_schema()
        store.write_batch(_make_batch())
        store.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.graph_dir, ignore_errors=True)


class TestCgdbDefinition(_StoreFixture):

    def test_function_definition(self):
        _, out, _ = _run(cc.cmd_cgdb_definition, _ns(
            graph=self.graph_dir, symbol="foo", limit=10))
        rows = _json(out)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 1001)
        self.assertEqual(rows[0]["kind"], "function")
        self.assertEqual(rows[0]["name"], "foo")

    def test_field_definition(self):
        _, out, _ = _run(cc.cmd_cgdb_definition, _ns(
            graph=self.graph_dir, symbol="read_iter", limit=10))
        rows = _json(out)
        self.assertEqual(rows[0]["id"], 1003)
        self.assertEqual(rows[0]["kind"], "field")

    def test_unknown_symbol_returns_empty(self):
        _, out, _ = _run(cc.cmd_cgdb_definition, _ns(
            graph=self.graph_dir, symbol="nope", limit=10))
        self.assertEqual(_json(out), [])

    def test_limit_respected(self):
        _, out, _ = _run(cc.cmd_cgdb_definition, _ns(
            graph=self.graph_dir, symbol="foo", limit=0))
        self.assertEqual(_json(out), [])


class TestCgdbFunctionBody(_StoreFixture):

    def test_body_text_returned(self):
        _, out, _ = _run(cc.cmd_cgdb_function_body, _ns(
            graph=self.graph_dir, function="foo"))
        result = _json(out)
        self.assertEqual(result["body_text"], "return 0;")
        self.assertEqual(result["name"], "foo")

    def test_unknown_function_returns_null(self):
        _, out, _ = _run(cc.cmd_cgdb_function_body, _ns(
            graph=self.graph_dir, function="nope"))
        self.assertIsNone(_json(out))


class TestCgdbStructLayout(_StoreFixture):

    def test_layout_orders_fields_by_byte_offset(self):
        _, out, _ = _run(cc.cmd_cgdb_struct_layout, _ns(
            graph=self.graph_dir, struct="file_operations"))
        result = _json(out)
        self.assertEqual(result["id"], 1005)
        self.assertEqual(result["name"], "file_operations")
        self.assertEqual([f["name"] for f in result["fields"]],
                         ["llseek", "read_iter"],
                         "fields must be ordered by byte_start")
        self.assertEqual(result["fields"][0]["type_spelling"].startswith(
            "loff_t"), True)

    def test_unknown_struct_returns_null(self):
        _, out, _ = _run(cc.cmd_cgdb_struct_layout, _ns(
            graph=self.graph_dir, struct="nope"))
        self.assertIsNone(_json(out))


class TestCgdbTypeDefinition(_StoreFixture):

    def test_struct_kind_node_found(self):
        # find_type_definition searches cgdb_nodes (struct/union/enum/
        # typedef/class kinds), not the cgdb_types table.
        _, out, _ = _run(cc.cmd_cgdb_type_definition, _ns(
            graph=self.graph_dir, type_name="file_operations", limit=10))
        rows = _json(out)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 1005)
        self.assertEqual(rows[0]["kind"], "struct")

    def test_unknown_type_returns_empty(self):
        _, out, _ = _run(cc.cmd_cgdb_type_definition, _ns(
            graph=self.graph_dir, type_name="nope_t", limit=10))
        self.assertEqual(_json(out), [])


class TestCgdbNodesUnderConfig(_StoreFixture):

    def test_exact_predicate_match(self):
        _, out, _ = _run(cc.cmd_cgdb_nodes_under_config, _ns(
            graph=self.graph_dir, config="defined(CONFIG_EXT4_FS)",
            limit=500))
        self.assertEqual(_json(out), [1007])

    def test_substring_match(self):
        _, out, _ = _run(cc.cmd_cgdb_nodes_under_config, _ns(
            graph=self.graph_dir, config="CONFIG_EXT4_FS", limit=500))
        self.assertEqual(_json(out), [1007])

    def test_unknown_config_returns_empty(self):
        _, out, _ = _run(cc.cmd_cgdb_nodes_under_config, _ns(
            graph=self.graph_dir, config="CONFIG_NOPE", limit=500))
        self.assertEqual(_json(out), [])


class TestCgdbPathFeasible(_StoreFixture):

    def test_two_block_path_without_conditions(self):
        _, out, _ = _run(cc.cmd_cgdb_path_feasible, _ns(
            graph=self.graph_dir, path="4001,4002", with_configs=""))
        result = _json(out)
        self.assertTrue(result["feasible"])
        self.assertEqual(result["unsatisfiable_conditions"], [])

    def test_single_block_path_is_trivially_feasible(self):
        _, out, _ = _run(cc.cmd_cgdb_path_feasible, _ns(
            graph=self.graph_dir, path="4001", with_configs=""))
        self.assertTrue(_json(out)["feasible"])

    def test_with_configs_attaches_predicate_layer(self):
        _, out, _ = _run(cc.cmd_cgdb_path_feasible, _ns(
            graph=self.graph_dir, path="4001,4002",
            with_configs="CONFIG_EXT4_FS=1"))
        result = _json(out)
        self.assertIn("macro_bindings", result)
        self.assertIn("config_feasibility", result)


class TestCgdbCfgPaths(_StoreFixture):

    def test_entry_to_exit_path_enumerated(self):
        _, out, _ = _run(cc.cmd_cgdb_cfg_paths, _ns(
            graph=self.graph_dir, function="foo", max_len=10))
        paths = {tuple(p["block_path"]) for p in _json(out)}
        self.assertIn((4001, 4002), paths)
        self.assertIn((4001,), paths,
                      "the entry block alone is a prefix path")

    def test_unknown_function_exits_1(self):
        ret, out, err, code = _capture_call(cc.cmd_cgdb_cfg_paths, _ns(
            graph=self.graph_dir, function="nope", max_len=10))
        self.assertEqual(code, 1)
        self.assertIn("not found", err)


class TestCgdbCoverage(_StoreFixture):

    def test_function_mode_matches(self):
        _, out, _ = _run(cc.cmd_cgdb_coverage, _ns(
            graph=self.graph_dir, function="foo", file=None))
        result = _json(out)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["match_count"], 1)
        m = result["matches"][0]
        self.assertEqual(m["name"], "foo")
        self.assertEqual(m["file_path"], "test.c")

    def test_file_mode_reports_scanned(self):
        _, out, _ = _run(cc.cmd_cgdb_coverage, _ns(
            graph=self.graph_dir, function=None, file="test.c"))
        result = _json(out)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["scanned"])
        matches = result["file_query"]["matches"]
        self.assertEqual(matches[0]["path"], "test.c")
        fn_names = {f["name"]
                    for f in result["file_query"]["functions_in_file"]}
        self.assertIn("foo", fn_names)

    def test_summary_mode(self):
        _, out, _ = _run(cc.cmd_cgdb_coverage, _ns(
            graph=self.graph_dir, function=None, file=None))
        result = _json(out)
        self.assertEqual(result["status"], "ok")
        self.assertIn("scanned_subsystems", result)
        self.assertIn("summary", result)

    def test_missing_db_reports_error(self):
        empty = tempfile.mkdtemp(prefix="c2d_cgdbunc_none_")
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        ret, out, err, code = _capture_call(cc.cmd_cgdb_coverage, _ns(
            graph=empty, function="foo", file=None))
        self.assertEqual(code, 1)
        self.assertEqual(_json(out)["status"], "error")


class TestCgdbWriteCoverage(_StoreFixture):

    def test_reports_written(self):
        _, out, _ = _run(cc.cmd_cgdb_write_coverage, _ns(
            graph=self.graph_dir))
        result = _json(out)
        self.assertEqual(result["status"], "ok")
        for key in ("coverage_report_path", "file_coverage_path"):
            self.assertTrue(os.path.exists(result[key]),
                            "%s not written" % key)
        self.assertTrue(result["coverage_report_path"].endswith(
            ".code2database_coverage_report.json"))
        self.assertTrue(result["file_coverage_path"].endswith(
            ".code2database_file_coverage.json"))


# ---------------------------------------------------------------------------
# Cross-graph commands (plain functions/edges fixtures)
# ---------------------------------------------------------------------------

def _make_plain_graph(graph_dir, functions, edges):
    """Legacy-style graph dir with functions + edges tables."""
    os.makedirs(graph_dir, exist_ok=True)
    db = os.path.join(graph_dir, "code2database.db")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS functions (
            id TEXT PRIMARY KEY, name TEXT, domain TEXT,
            source_file TEXT, line_number INTEGER, signature TEXT,
            labels TEXT, body_text_compressed BLOB, extra_json TEXT);
        CREATE TABLE IF NOT EXISTS edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoker_id TEXT NOT NULL, invoked_id TEXT NOT NULL,
            relation TEXT, call_order INTEGER, call_condition TEXT);
    """)
    for fn in functions:
        conn.execute(
            "INSERT INTO functions (id, name, domain, source_file, "
            "line_number, signature) VALUES (?, ?, ?, ?, ?, ?)",
            (fn["id"], fn["name"], fn.get("domain", "src"),
             fn.get("source_file", "a.c"), fn.get("line", 1),
             fn.get("signature", "")))
    for e in edges:
        conn.execute(
            "INSERT INTO edges (invoker_id, invoked_id, relation) "
            "VALUES (?, ?, ?)", (e[0], e[1], "CALL"))
    conn.commit()
    conn.close()


class TestCgdbCompare(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_cgdbcmp_")
        self.source = os.path.join(self.tmp, "src")
        self.target = os.path.join(self.tmp, "tgt")
        _make_plain_graph(self.source, [
            {"id": "alpha", "name": "alpha", "signature": "int alpha(int)"},
            {"id": "beta", "name": "beta", "signature": "int beta(void)"},
        ], [("alpha", "beta")])
        _make_plain_graph(self.target, [
            {"id": "alpha", "name": "alpha", "signature": "int alpha(long)"},
            {"id": "gamma", "name": "gamma", "signature": "int gamma(void)"},
        ], [("alpha", "gamma")])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_compare_graphs_diff(self):
        from _builder.cgdb.cgdb_compare import compare_graphs
        result = compare_graphs(self.source, self.target)
        s = result["summary"]
        self.assertEqual(s["source_functions"], 2)
        self.assertEqual(s["target_functions"], 2)
        self.assertEqual(s["new_functions"], 1)      # beta only in source
        self.assertEqual(s["removed_functions"], 1)  # gamma only in target
        self.assertEqual(s["signature_changed"], 1)  # alpha
        names_in_source = {f["name"] for f in result["only_in_source"]}
        names_in_target = {f["name"] for f in result["only_in_target"]}
        self.assertEqual(names_in_source, {"beta"})
        self.assertEqual(names_in_target, {"gamma"})

    def test_cmd_smoke(self):
        from _builder.cgdb.cgdb_compare import cmd_cgdb_compare
        ret, out, err, code = _capture_call(cmd_cgdb_compare, _ns(
            graph=self.target, source_graph=self.source))
        self.assertIsNone(code)
        self.assertIn("New functions", out)
        self.assertIn("beta", out)
        self.assertIn("Removed functions", out)
        self.assertIn("gamma", out)


class TestCgdbMergeKnowledge(unittest.TestCase):

    def setUp(self):
        from _builder.memory.memory_store import MemoryStore
        self.tmp = tempfile.mkdtemp(prefix="c2d_cgdbmrg_")
        self.source = os.path.join(self.tmp, "src")
        self.target = os.path.join(self.tmp, "tgt")
        _make_plain_graph(self.source,
                          [{"id": "s1", "name": "do_work"}], [])
        _make_plain_graph(self.target,
                          [{"id": "t1", "name": "do_work"}], [])
        src_store = MemoryStore(self.source)
        src_store.add(question="How does do_work lock?",
                      answer="Takes the global mutex.",
                      tags=["locking"], category="src-q")
        self.target_before = self._target_count()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _target_count(self):
        db = os.path.join(self.target, "memory", "memory.db")
        if not os.path.exists(db):
            return 0
        conn = sqlite3.connect(db)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM memories").fetchone()[0]
        finally:
            conn.close()

    def test_dry_run_counts_without_writing(self):
        from _builder.cgdb.cgdb_merge import merge_cross_graph
        result = merge_cross_graph(self.source, self.target, dry_run=True)
        self.assertEqual(result.memory_entries_merged, 1)
        self.assertEqual(self._target_count(), self.target_before)

    def test_merge_transfers_memory(self):
        from _builder.cgdb.cgdb_merge import merge_cross_graph
        result = merge_cross_graph(self.source, self.target, dry_run=False)
        self.assertEqual(result.memory_entries_merged, 1)
        self.assertEqual(result.errors, [])
        self.assertEqual(self._target_count(), self.target_before + 1)

    def test_cmd_smoke_dry_run(self):
        from _builder.cgdb.cgdb_merge import cmd_cgdb_merge_knowledge
        ret, out, err, code = _capture_call(cmd_cgdb_merge_knowledge, _ns(
            graph=self.target, source_graph=self.source, dry_run=True,
            no_knowledge=False, no_memory=False))
        self.assertIsNone(code)
        self.assertIn("dry run", out)
        self.assertIn("Memory entries merged", out)


class TestCgdbSuggest(unittest.TestCase):

    def test_empty_graph_yields_error_entry(self):
        from _builder.cgdb.cgdb_suggest import analyze_and_suggest
        empty = tempfile.mkdtemp(prefix="c2d_cgdbsug_")
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        suggestions = analyze_and_suggest(empty)
        self.assertTrue(suggestions)
        self.assertEqual(suggestions[0]["category"], "error")
        self.assertIn("Could not load graph", suggestions[0]["message"])
        self.assertTrue(suggestions[0]["action"])

    def test_populated_graph_returns_structured_entries(self):
        from _builder.cgdb.cgdb_suggest import analyze_and_suggest
        tmp = tempfile.mkdtemp(prefix="c2d_cgdbsug2_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _make_plain_graph(tmp, [
            {"id": "hub", "name": "hub", "signature": "int hub(void)"},
            {"id": "a", "name": "a", "signature": "int a(void)"},
            {"id": "b", "name": "b", "signature": "int b(void)"},
        ], [("a", "hub"), ("b", "hub")])
        suggestions = analyze_and_suggest(tmp)
        known_categories = ("invariants", "duplicates", "deadlock",
                            "docs", "stale", "complexity", "testing")
        for s in suggestions:
            self.assertIn("priority", s)
            self.assertTrue(s["priority"])
            self.assertIn(s["category"], known_categories)
            self.assertIn("message", s)

    def test_cmd_smoke(self):
        from _builder.cgdb.cgdb_suggest import cmd_cgdb_suggest
        tmp = tempfile.mkdtemp(prefix="c2d_cgdbsug3_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _make_plain_graph(tmp, [{"id": "solo", "name": "solo"}], [])
        ret, out, err, code = _capture_call(cmd_cgdb_suggest, _ns(
            graph=tmp, top=20))
        self.assertIsNone(code)
        self.assertTrue(out.strip())


class TestCgdbTour(unittest.TestCase):

    def test_tour_written_with_function_names(self):
        from _builder.cgdb.cgdb_tour import generate_tour
        tmp = tempfile.mkdtemp(prefix="c2d_cgdbtour_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _make_plain_graph(tmp, [
            {"id": "alpha", "name": "alpha", "domain": "net"},
            {"id": "beta", "name": "beta", "domain": "net"},
        ], [("alpha", "beta")])
        path = generate_tour(tmp)
        self.assertEqual(path, os.path.join(tmp, "CODEBASE_TOUR.md"))
        self.assertTrue(os.path.exists(path))
        content = open(path, encoding="utf-8").read()
        self.assertIn("alpha", content)
        self.assertIn("beta", content)

    def test_cmd_smoke(self):
        from _builder.cgdb.cgdb_tour import cmd_cgdb_tour
        tmp = tempfile.mkdtemp(prefix="c2d_cgdbtour2_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _make_plain_graph(tmp, [{"id": "solo", "name": "solo"}], [])
        ret, out, err, code = _capture_call(cmd_cgdb_tour, _ns(
            graph=tmp, output=None))
        self.assertIsNone(code)
        self.assertIn("Tour written to", out)
        self.assertTrue(os.path.exists(
            os.path.join(tmp, "CODEBASE_TOUR.md")))


if __name__ == "__main__":
    unittest.main()
