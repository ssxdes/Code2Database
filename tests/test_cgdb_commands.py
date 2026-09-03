"""Unit tests for cgdb_commands.py CLI command surfaces.

Builds a real SQLiteCGDBStore (all 13 layers, reusing the canonical
batch fixture) inside a temp graph dir, then drives each cmd_* handler
and asserts expected outputs: symbol query, ops-bind lookup, data flow,
race check, index status, read-only SQL guard + md format, predefined
views, schema version, invokers/invoked closures, invoke path, and the
_resolve_node_id resolution ladder.
"""
import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder import cgdb_commands as cc
from _builder.cgdb_store import SQLiteCGDBStore
from tests.test_cgdb_store import _make_batch


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


class _StoreFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph_dir = tempfile.mkdtemp(prefix="c2d_cgdbcmd_")
        db_path = os.path.join(cls.graph_dir, "code2database.db")
        store = SQLiteCGDBStore(db_path)
        store.create_schema()
        store.write_batch(_make_batch())
        store.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.graph_dir, ignore_errors=True)


class TestCmdCgdbQuery(_StoreFixture):
    def test_get_node_by_id(self):
        _, out, _ = _run(cc.cmd_cgdb_query, _ns(
            graph=self.graph_dir, node_id="1001", query="", kind=None, limit=50))
        node = _json(out)
        self.assertEqual(node["name"], "foo")
        self.assertEqual(node["kind"], "function")

    def test_fts_symbol_search(self):
        _, out, _ = _run(cc.cmd_cgdb_query, _ns(
            graph=self.graph_dir, node_id=None, query="foo", kind=None, limit=50))
        rows = _json(out)
        self.assertGreaterEqual(len(rows), 1)
        self.assertIn(1001, [r["id"] for r in rows])

    def test_kind_filter(self):
        # FTS5 does not accept bare '*' as a query — use a term
        _, out, _ = _run(cc.cmd_cgdb_query, _ns(
            graph=self.graph_dir, node_id=None, query="foo", kind="function",
            limit=50))
        for r in _json(out):
            self.assertEqual(r["kind"], "function")

    def test_missing_db_exits_1(self):
        empty = tempfile.mkdtemp(prefix="c2d_cgdbcmd_none_")
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        ret, out, err, code = _capture_call(
            cc.cmd_cgdb_query, _ns(graph=empty, node_id=None, query="x",
                                   kind=None, limit=5))
        self.assertEqual(code, 1)


class TestCmdCgdbOpsImpls(_StoreFixture):
    def test_find_impls_by_field(self):
        _, out, _ = _run(cc.cmd_cgdb_ops_impls, _ns(
            graph=self.graph_dir, field="read_iter", struct=""))
        rows = _json(out)
        self.assertTrue(rows)
        impls = {r.get("impl_function_id") or r.get("impl_id") for r in rows}
        self.assertIn(1001, impls)

    def test_unknown_field_returns_empty(self):
        _, out, _ = _run(cc.cmd_cgdb_ops_impls, _ns(
            graph=self.graph_dir, field="no_such_field", struct=""))
        self.assertEqual(_json(out), [])


class TestCmdCgdbDataFlow(_StoreFixture):
    def test_data_flow_for_var(self):
        _, out, _ = _run(cc.cmd_cgdb_data_flow, _ns(
            graph=self.graph_dir, var="ext4_fop"))
        r = _json(out)
        self.assertEqual(r["var_id"], 1004)
        self.assertGreaterEqual(len(r["entries"]), 1)
        self.assertEqual(r["entries"][0]["function_id"], 1001)

    def test_unknown_var_exits_1(self):
        ret, out, err, code = _capture_call(
            cc.cmd_cgdb_data_flow, _ns(graph=self.graph_dir, var="zzz"))
        self.assertEqual(code, 1)
        self.assertIn("not found", err)


class TestCmdCgdbRaceCheck(_StoreFixture):
    def test_race_check_returns_list_of_race_points(self):
        _, out, _ = _run(cc.cmd_cgdb_race_check, _ns(
            graph=self.graph_dir, function="foo"))
        races = _json(out)
        self.assertIsInstance(races, list)
        # ext4_fop is the lock var itself → its accesses are protected,
        # so the balanced acquire/release fixture may yield no races;
        # every reported point must carry the documented fields.
        for r in races:
            self.assertIn("var_id", r)
            self.assertIn("kind", r)

    def test_unknown_function_exits_1(self):
        ret, out, err, code = _capture_call(
            cc.cmd_cgdb_race_check, _ns(graph=self.graph_dir, function="zzz"))
        self.assertEqual(code, 1)


class TestCmdCgdbIndexStatus(_StoreFixture):
    def test_status_counts(self):
        _, out, _ = _run(cc.cmd_cgdb_index_status, _ns(graph=self.graph_dir))
        r = _json(out)
        self.assertIn("nodes", json.dumps(r).lower())
        self.assertGreaterEqual(r.get("total_nodes", r.get("nodes", 0)), 4)


class TestCmdCgdbSql(_StoreFixture):
    def test_select_returns_json_rows(self):
        _, out, _ = _run(cc.cmd_cgdb_sql, _ns(
            graph=self.graph_dir,
            sql="SELECT id, name FROM cgdb_nodes WHERE kind='function' "
                "ORDER BY id",
            format="json"))
        rows = _json(out)
        self.assertEqual([r["name"] for r in rows], ["foo", "bar"])

    def test_write_statements_rejected(self):
        for sql in ("DELETE FROM cgdb_nodes",
                    "INSERT INTO cgdb_nodes VALUES (1)",
                    "DROP TABLE cgdb_nodes",
                    "UPDATE cgdb_nodes SET name='x'"):
            ret, out, err, code = _capture_call(
                cc.cmd_cgdb_sql, _ns(graph=self.graph_dir, sql=sql,
                                     format="json"))
            self.assertEqual(code, 1, sql)
            self.assertIn("only SELECT/WITH/EXPLAIN/PRAGMA", err)

    def test_lowercase_select_allowed(self):
        _, out, _ = _run(cc.cmd_cgdb_sql, _ns(
            graph=self.graph_dir,
            sql="select count(*) as n from cgdb_nodes", format="json"))
        self.assertGreaterEqual(_json(out)[0]["n"], 4)

    def test_with_cte_allowed(self):
        _, out, _ = _run(cc.cmd_cgdb_sql, _ns(
            graph=self.graph_dir,
            sql="WITH f AS (SELECT id FROM cgdb_nodes WHERE name='foo') "
                "SELECT id FROM f",
            format="json"))
        self.assertEqual(_json(out)[0]["id"], 1001)

    def test_invalid_sql_reports_error_exits_1(self):
        ret, out, err, code = _capture_call(
            cc.cmd_cgdb_sql, _ns(graph=self.graph_dir,
                                 sql="SELECT FROM WHERE", format="json"))
        self.assertEqual(code, 1)
        self.assertIn("SQL error", err)

    def test_missing_db_exits_1(self):
        empty = tempfile.mkdtemp(prefix="c2d_cgdbsql_none_")
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        ret, out, err, code = _capture_call(
            cc.cmd_cgdb_sql, _ns(graph=empty, sql="SELECT 1", format="json"))
        self.assertEqual(code, 1)

    def test_markdown_format(self):
        _, out, _ = _run(cc.cmd_cgdb_sql, _ns(
            graph=self.graph_dir,
            sql="SELECT name FROM cgdb_nodes WHERE id=1001", format="md"))
        self.assertIn("| name |", out)
        self.assertIn("| foo |", out)

    def test_md_format_empty_result(self):
        _, out, _ = _run(cc.cmd_cgdb_sql, _ns(
            graph=self.graph_dir,
            sql="SELECT name FROM cgdb_nodes WHERE id=999999", format="md"))
        self.assertIn("No results.", out)


class TestCmdCgdbViews(_StoreFixture):
    def test_list_views_without_name(self):
        _, out, _ = _run(cc.cmd_cgdb_views, _ns(graph=self.graph_dir,
                                                name=None, format="json"))
        views = _json(out)
        names = {v["name"] for v in views}
        self.assertIn("hub-functions", names)
        self.assertIn("doc-coverage", names)
        for v in views:
            self.assertTrue(v["description"])

    def test_run_view_returns_rows(self):
        _, out, _ = _run(cc.cmd_cgdb_views, _ns(
            graph=self.graph_dir, name="hub-targets", format="json"))
        rows = _json(out)
        # bar (1002) is invoked by foo via cgdb_edges
        self.assertTrue(any(r["name"] == "bar" for r in rows))

    def test_unknown_view_exits_1(self):
        ret, out, err, code = _capture_call(
            cc.cmd_cgdb_views, _ns(graph=self.graph_dir, name="nope",
                                   format="json"))
        self.assertEqual(code, 1)
        self.assertIn("unknown view", err)
        self.assertIn("hub-functions", err)  # available list in error

    def test_md_format(self):
        _, out, _ = _run(cc.cmd_cgdb_views, _ns(
            graph=self.graph_dir, name="version-history", format="md"))
        # empty graph_versions → "No results." or header; must not crash
        self.assertTrue(out.strip())


class TestCmdCgdbSchemaVersion(_StoreFixture):
    def test_reports_current_and_latest(self):
        _, out, _ = _run(cc.cmd_cgdb_schema_version, _ns(graph=self.graph_dir))
        r = _json(out)
        from _builder.cgdb_schema import CGDB_SCHEMA_VERSION
        self.assertEqual(r["current_version"], CGDB_SCHEMA_VERSION)
        self.assertEqual(r["latest_version"], CGDB_SCHEMA_VERSION)
        self.assertFalse(r["needs_migration"])
        self.assertTrue(r["migrations_available"])

    def test_missing_db_exits_1(self):
        empty = tempfile.mkdtemp(prefix="c2d_cgdbsv_none_")
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        ret, out, err, code = _capture_call(
            cc.cmd_cgdb_schema_version, _ns(graph=empty))
        self.assertEqual(code, 1)


class TestCmdCgdbFindInvokersInvoked(_StoreFixture):
    def _inv_args(self, **kw):
        kw.setdefault("graph", self.graph_dir)
        kw.setdefault("node", "bar")
        kw.setdefault("depth", 5)
        kw.setdefault("edge_types", "")
        kw.setdefault("limit", 100)
        kw.setdefault("include_vtable_dispatch", False)
        return _ns(**kw)

    def test_find_invokers_reverse_closure(self):
        _, out, _ = _run(cc.cmd_cgdb_find_invokers, self._inv_args(node="bar"))
        rows = _json(out)
        self.assertTrue(any(r["id"] == 1001 or r["name"] == "foo"
                            for r in rows))

    def test_find_invoked_forward_closure(self):
        _, out, _ = _run(cc.cmd_cgdb_find_invoked, self._inv_args(node="foo"))
        rows = _json(out)
        self.assertTrue(any(r["id"] == 1002 or r["name"] == "bar"
                            for r in rows))

    def test_missing_node_exits_1(self):
        ret, out, err, code = _capture_call(
            cc.cmd_cgdb_find_invokers, self._inv_args(node="zzz"))
        self.assertEqual(code, 1)
        self.assertIn("not found", err)


class TestCmdCgdbPath(_StoreFixture):
    def test_path_src_to_dst(self):
        _, out, _ = _run(cc.cmd_cgdb_path, _ns(
            graph=self.graph_dir, src="foo", dst="bar", max_len=10))
        r = _json(out)
        found = r if isinstance(r, list) else r.get("path", r)
        text = json.dumps(r)
        self.assertIn("foo", text)
        self.assertIn("bar", text)

    def test_missing_endpoint_exits_1_with_hints(self):
        ret, out, err, code = _capture_call(
            cc.cmd_cgdb_path, _ns(graph=self.graph_dir, src="zzz",
                                  dst="bar", max_len=10))
        self.assertEqual(code, 1)
        self.assertIn("not found", err)


class TestResolveNodeId(_StoreFixture):
    def _resolve(self, value, prefer="in"):
        store = SQLiteCGDBStore(
            os.path.join(self.graph_dir, "code2database.db"))
        try:
            return cc._resolve_node_id(store, value, prefer_edges=prefer)
        finally:
            store.close()

    def test_numeric_passthrough(self):
        self.assertEqual(self._resolve("1001"), 1001)

    def test_by_name(self):
        self.assertEqual(self._resolve("foo"), 1001)

    def test_by_fqn(self):
        self.assertEqual(self._resolve("file_operations.read_iter"), 1003)

    def test_unknown_returns_none(self):
        self.assertIsNone(self._resolve("nope_nope"))


if __name__ == "__main__":
    unittest.main()
