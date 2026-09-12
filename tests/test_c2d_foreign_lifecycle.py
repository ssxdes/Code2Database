"""Tests for the c2d foreign C2D lifecycle (c2d_foreign + check_compat).

Covers the commands registered in c2d_foreign.py and c2d_phase2.py that
had no test references before:

- c2d-add-foreign: registration + unresolved-edge resolution strategies
  (exact name with project prefix, suffix, ambiguity -> unresolved,
  empty-name skip, dangling ids), self-reference / missing db /
  non-SQLite db rejection, re-run replaces instead of duplicating
- c2d-sync-foreign: unchanged signature, removed-function -> deleted,
  newly added foreign function -> re-resolve, vanished db -> orphaning
- c2d-list-foreign: watched entry with per-status ref counts
- c2d-resolve-foreign: forced re-resolve regardless of mtime
- c2d-pin-foreign / c2d-unpin-foreign: pinning a resolved ref, refusing
  to pin an unresolved one, unpin restoring auto-update
- c2d-prune-foreign: age + status window (old deleted/orphaned pruned,
  recent and resolved kept, status filter respected)
- c2d-remove-foreign: refs marked orphaned, watcher deregistered
- c2d-check-compat: broken / signature-changed / ok edges against a new
  foreign version, missing db rejection
- builder-level cmd_c2d_* wrappers (stdout JSON contract)
- with_foreign_attached: guaranteed DETACH
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
from datetime import datetime
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.scanner_bridge.c2d_foreign import (
    add_foreign,
    list_foreign,
    pin_foreign_ref,
    prune_foreign,
    remove_foreign,
    resolve_foreign_by_name,
    sync_foreign,
    unpin_foreign_ref,
    with_foreign_attached,
    _connect,
    _resolve_by_exact_name,
)
from _builder.scanner_bridge.c2d_phase2 import check_compat


def _make_foreign_db(db_path, functions):
    """Create a foreign project db (DELETE journal so a read-only
    ATTACH from a WAL connection cannot deadlock on a WAL sidecar)."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS functions (
            id TEXT PRIMARY KEY, name TEXT, domain TEXT,
            source_file TEXT, line_number INTEGER, signature TEXT,
            labels TEXT, body_text_compressed BLOB, extra_json TEXT
        );
    """)
    for fn in functions:
        conn.execute(
            "INSERT OR IGNORE INTO functions (id, name, domain, "
            "source_file, line_number, signature) VALUES (?, ?, ?, ?, ?, ?)",
            (fn["id"], fn["name"], fn.get("domain", "A"),
             fn.get("source_file", "a.c"), fn.get("line", 1),
             fn.get("signature", "")))
    conn.commit()
    conn.close()


class _ForeignFixture(unittest.TestCase):
    """B graph with unresolved calls + foreign project A."""

    # B's unresolved edges: external_<name> calls + dangling + empty
    B_FUNCTIONS = [{"id": "B_main", "name": "main"}]
    B_EDGES = [
        {"invoker": "B_main", "invoked": "external_sdk_open"},
        {"invoker": "B_main", "invoked": "external_missing"},
        {"invoker": "B_main", "invoked": "external_parse"},
        {"invoker": "B_main", "invoked": "dangling_fn"},
        {"invoker": "B_main", "invoked": ""},
    ]
    A_FUNCTIONS = [
        {"id": "A_sdk_open", "name": "sdk_open",
         "signature": "int sdk_open(void)"},
        {"id": "A_json_parse", "name": "json_parse",
         "signature": "int json_parse(char *)"},
        {"id": "A_other", "name": "other"},
    ]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_foreign_lc_")
        self.b_dir = os.path.join(self.tmp, "B")
        self.a_dir = os.path.join(self.tmp, "A")
        os.makedirs(self.b_dir)
        os.makedirs(self.a_dir)
        self._make_b_db()
        _make_foreign_db(os.path.join(self.a_dir, "code2database.db"),
                         self.A_FUNCTIONS)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_b_db(self):
        from _builder.kb.kb_index import _kb_connect
        conn = _kb_connect(self.b_dir, create_if_missing=True)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS functions "
            "(id TEXT PRIMARY KEY, name TEXT, domain TEXT, "
            "source_file TEXT, line_number INTEGER, signature TEXT, "
            "labels TEXT, body_text_compressed BLOB, extra_json TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS edges "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "invoker_id TEXT NOT NULL, invoked_id TEXT NOT NULL, "
            "relation TEXT, call_order INTEGER, call_condition TEXT, "
            "concurrency TEXT, confidence TEXT, confidence_score REAL, "
            "source TEXT, evidence TEXT, invoked_arg_json TEXT, "
            "reg_args_json TEXT, vtable_type TEXT, vtable_bound_module TEXT)")
        for fn in self.B_FUNCTIONS:
            conn.execute(
                "INSERT OR IGNORE INTO functions (id, name, domain) "
                "VALUES (?, ?, ?)", (fn["id"], fn["name"], "B"))
        for e in self.B_EDGES:
            conn.execute(
                "INSERT INTO edges (invoker_id, invoked_id, relation, "
                "call_order) VALUES (?, ?, ?, ?)",
                (e["invoker"], e["invoked"], "CALL", 0))
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
        except sqlite3.Error:
            pass
        conn.commit()
        conn.close()

    def _add_a_function(self, fn):
        """Insert a function into A's db (changes size/count/mtime)."""
        db = os.path.join(self.a_dir, "code2database.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT OR IGNORE INTO functions (id, name, domain, "
            "source_file, line_number, signature) VALUES (?, ?, ?, ?, ?, ?)",
            (fn["id"], fn["name"], fn.get("domain", "A"),
             fn.get("source_file", "a.c"), fn.get("line", 1),
             fn.get("signature", "")))
        conn.commit()
        conn.close()

    def _refs(self, b_dir=None):
        """Read foreign_refs rows as dicts."""
        conn = _connect(b_dir or self.b_dir)
        try:
            rows = conn.execute(
                "SELECT * FROM foreign_refs").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


class TestAddForeignResolution(_ForeignFixture):

    def test_resolution_strategies_and_counts(self):
        summary = add_foreign(self.b_dir, self.a_dir, project_name="A")
        self.assertNotIn("error", summary)
        self.assertEqual(summary["added"], True)
        self.assertEqual(summary["resolved_count"], 2,
                         "sdk_open (exact name) + parse (suffix)")
        self.assertEqual(summary["unresolved_count"], 2,
                         "missing + dangling_fn")
        self.assertEqual(summary["skipped_empty_name"], 1)
        self.assertEqual(summary["total_foreign_refs"], 4)
        refs = {r["invoked_name"]: r for r in self._refs()}
        # exact-name hit via the project-prefixed id
        sdk = refs["sdk_open"]
        self.assertEqual(sdk["status"], "resolved")
        self.assertEqual(sdk["resolution_strategy"], "exact_name")
        self.assertEqual(sdk["foreign_node_id"], "A_sdk_open")
        self.assertEqual(sdk["foreign_name"], "sdk_open")
        # suffix hit: only one foreign id ends in _parse
        parse = refs["parse"]
        self.assertEqual(parse["status"], "resolved")
        self.assertEqual(parse["resolution_strategy"], "suffix")
        self.assertEqual(parse["foreign_node_id"], "A_json_parse")
        # unresolved recorded
        self.assertEqual(refs["missing"]["status"], "unresolved")
        self.assertEqual(refs["dangling_fn"]["status"], "unresolved")
        # the empty-invoked_id edge produced no ref at all
        self.assertNotIn("", refs)

    def test_suffix_ambiguity_is_recorded_unresolved(self):
        self._add_a_function({"id": "A_xml_parse", "name": "xml_parse"})
        summary = add_foreign(self.b_dir, self.a_dir)
        self.assertEqual(summary["unresolved_count"], 3,
                         "two *_parse candidates must not auto-resolve")
        refs = {r["invoked_name"]: r for r in self._refs()}
        self.assertEqual(refs["parse"]["status"], "unresolved")

    def test_rerun_replaces_instead_of_duplicating(self):
        add_foreign(self.b_dir, self.a_dir, project_name="A")
        add_foreign(self.b_dir, self.a_dir, project_name="A")
        refs = self._refs()
        keys = [(r["local_node_id"], r["invoked_name"],
                 r["foreign_c2d_path"]) for r in refs]
        self.assertEqual(len(keys), len(set(keys)),
                         "natural key must stay unique across re-runs")
        self.assertEqual(len(refs), 4)

    def test_self_reference_rejected(self):
        summary = add_foreign(self.b_dir, self.b_dir)
        self.assertIn("error", summary)
        self.assertIn("self-reference", summary["error"])

    def test_missing_foreign_db_rejected(self):
        empty_dir = os.path.join(self.tmp, "nowhere")
        os.makedirs(empty_dir)
        summary = add_foreign(self.b_dir, empty_dir)
        self.assertIn("error", summary)
        self.assertIn("not found", summary["error"])

    def test_non_sqlite_foreign_db_rejected(self):
        bad_dir = os.path.join(self.tmp, "badA")
        os.makedirs(bad_dir)
        with open(os.path.join(bad_dir, "code2database.db"),
                  "w", encoding="utf-8") as f:
            f.write("this is not a database\n")
        summary = add_foreign(self.b_dir, bad_dir)
        self.assertIn("error", summary)
        self.assertIn("not a valid SQLite", summary["error"])


class TestSyncForeign(_ForeignFixture):

    def test_unchanged_signature_reports_unchanged(self):
        add_foreign(self.b_dir, self.a_dir, project_name="A")
        summary = sync_foreign(self.b_dir, self.a_dir)
        self.assertNotIn("error", summary)
        self.assertEqual(summary["synced_c2ds"],
                         [{"c2d_path": self.a_dir, "status": "unchanged"}])
        self.assertEqual(summary["deleted_marked"], 0)
        self.assertEqual(summary["newly_resolved"], 0)

    def test_removed_function_marks_ref_deleted(self):
        add_foreign(self.b_dir, self.a_dir, project_name="A")
        db = os.path.join(self.a_dir, "code2database.db")
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM functions WHERE id = 'A_sdk_open'")
        conn.commit()
        conn.close()
        summary = sync_foreign(self.b_dir, self.a_dir)
        self.assertEqual(summary["deleted_marked"], 1)
        refs = {r["invoked_name"]: r for r in self._refs()}
        self.assertEqual(refs["sdk_open"]["status"], "deleted")
        # parse + missing/dangling unaffected
        self.assertEqual(refs["parse"]["status"], "resolved")
        self.assertEqual(refs["missing"]["status"], "unresolved")

    def test_new_function_re_resolves_unresolved(self):
        add_foreign(self.b_dir, self.a_dir, project_name="A")
        self._add_a_function({"id": "A_missing", "name": "missing"})
        summary = sync_foreign(self.b_dir, self.a_dir)
        self.assertEqual(summary["newly_resolved"], 1)
        refs = {r["invoked_name"]: r for r in self._refs()}
        self.assertEqual(refs["missing"]["status"], "resolved")
        self.assertEqual(refs["missing"]["foreign_node_id"], "A_missing")

    def test_vanished_db_marks_all_deleted(self):
        add_foreign(self.b_dir, self.a_dir, project_name="A")
        os.remove(os.path.join(self.a_dir, "code2database.db"))
        summary = sync_foreign(self.b_dir, self.a_dir)
        self.assertEqual(summary["deleted_marked"], 2,
                         "both resolved refs (sdk_open, parse) go deleted")
        self.assertEqual(summary["synced_c2ds"][0]["status"], "missing")
        conn = _connect(self.b_dir)
        try:
            status = conn.execute(
                "SELECT sync_status FROM watched_c2ds").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(status, "missing")

    def test_no_watched_c2ds_is_a_noop(self):
        summary = sync_foreign(self.b_dir)
        self.assertEqual(summary["synced_c2ds"], [])
        self.assertIn("message", summary)

    def test_two_changed_foreign_projects_both_sync(self):
        # A second foreign project C; both A and C change before one
        # sync call. The ATTACH alias is shared across iterations, so
        # a leaked DETACH on the first project used to abort the whole
        # sync (and roll back the first project's updates too).
        c_dir = os.path.join(self.tmp, "C")
        os.makedirs(c_dir)
        _make_foreign_db(os.path.join(c_dir, "code2database.db"), [
            {"id": "C_sdk_open", "name": "sdk_open",
             "signature": "int sdk_open(void)"},
        ])
        add_foreign(self.b_dir, self.a_dir, project_name="A")
        add_foreign(self.b_dir, c_dir, project_name="C")
        for d in (self.a_dir, c_dir):
            conn = sqlite3.connect(os.path.join(d, "code2database.db"))
            conn.execute("DELETE FROM functions WHERE id = ?",
                         ("A_sdk_open" if d == self.a_dir else "C_sdk_open",))
            conn.commit()
            conn.close()
        summary = sync_foreign(self.b_dir)
        self.assertNotIn("error", summary)
        self.assertEqual(
            [e["status"] for e in summary["synced_c2ds"]],
            ["synced", "synced"])
        self.assertEqual(summary["deleted_marked"], 2)
        # both projects' deletions persisted
        by_path = {}
        for r in self._refs():
            by_path.setdefault(r["foreign_c2d_path"], set()).add(r["status"])
        self.assertEqual(by_path[self.a_dir],
                         {"deleted", "resolved", "unresolved"},
                         "A: sdk_open deleted, parse stays resolved")
        self.assertIn("deleted", by_path[c_dir],
                      "C: its only resolved ref went deleted")

    def test_forced_re_resolve_covers_all_watched(self):
        c_dir = os.path.join(self.tmp, "C")
        os.makedirs(c_dir)
        _make_foreign_db(os.path.join(c_dir, "code2database.db"), [
            {"id": "C_missing", "name": "missing"},
        ])
        # register C first so B's unresolved "missing" edge resolves
        # against A (exact name) — then remove it from A so only the
        # manual resolve against C can recover it.
        add_foreign(self.b_dir, self.a_dir, project_name="A")
        conn = sqlite3.connect(os.path.join(self.a_dir, "code2database.db"))
        conn.execute("DELETE FROM functions WHERE id = 'A_sdk_open'")
        conn.commit()
        conn.close()
        add_foreign(self.b_dir, c_dir, project_name="C")
        self._add_a_function({"id": "A_sdk_open", "name": "sdk_open",
                              "signature": "int sdk_open(void)"})
        summary = resolve_foreign_by_name(self.b_dir)
        self.assertNotIn("error", summary)
        self.assertGreater(summary["total_checked"], 0)


class TestListRemoveResolveForeign(_ForeignFixture):

    def test_list_reports_per_status_counts(self):
        add_foreign(self.b_dir, self.a_dir, project_name="A")
        entries = list_foreign(self.b_dir)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["c2d_path"], self.a_dir)
        self.assertEqual(e["project_name"], "A")
        self.assertEqual(e["foreign_refs_count"], 4)
        self.assertEqual(e["resolved_count"], 2)
        self.assertEqual(e["unresolved_count"], 2)
        self.assertEqual(e["stale_count"], 0)
        self.assertEqual(e["deleted_count"], 0)

    def test_remove_marks_orphaned_and_deregisters(self):
        add_foreign(self.b_dir, self.a_dir, project_name="A")
        summary = remove_foreign(self.b_dir, self.a_dir)
        self.assertTrue(summary["removed"])
        self.assertEqual(summary["orphaned_refs"], 4)
        self.assertEqual(list_foreign(self.b_dir), [])
        for r in self._refs():
            self.assertEqual(r["status"], "orphaned",
                             "refs must be preserved for the trail, "
                             "not deleted")

    def test_forced_re_resolve_ignores_mtime(self):
        add_foreign(self.b_dir, self.a_dir, project_name="A")
        # A gains the function but B never re-synced
        self._add_a_function({"id": "A_missing", "name": "missing"})
        summary = resolve_foreign_by_name(self.b_dir, self.a_dir)
        self.assertEqual(summary["re_resolved"], 1)
        self.assertEqual(summary["still_unresolved"], 1,
                         "dangling_fn stays unresolved")
        refs = {r["invoked_name"]: r for r in self._refs()}
        self.assertEqual(refs["missing"]["status"], "resolved")
        self.assertEqual(refs["missing"]["resolution_strategy"],
                         "manual_resolve")


class TestPinUnpinForeignRef(_ForeignFixture):

    def _one_resolved_ref_id(self):
        conn = _connect(self.b_dir)
        try:
            return conn.execute(
                "SELECT id FROM foreign_refs WHERE status = 'resolved' "
                "LIMIT 1").fetchone()[0]
        finally:
            conn.close()

    def test_pin_and_unpin_roundtrip(self):
        add_foreign(self.b_dir, self.a_dir, project_name="A")
        ref_id = self._one_resolved_ref_id()
        summary = pin_foreign_ref(self.b_dir, ref_id)
        self.assertTrue(summary["pinned"])
        conn = _connect(self.b_dir)
        try:
            strategy = conn.execute(
                "SELECT resolution_strategy FROM foreign_refs "
                "WHERE id = ?", (ref_id,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(strategy, "pinned")
        summary = unpin_foreign_ref(self.b_dir, ref_id)
        self.assertTrue(summary["unpinned"])
        conn = _connect(self.b_dir)
        try:
            strategy = conn.execute(
                "SELECT resolution_strategy FROM foreign_refs "
                "WHERE id = ?", (ref_id,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(strategy, "exact_name")

    def test_pin_rejects_unresolved_ref(self):
        add_foreign(self.b_dir, self.a_dir, project_name="A")
        conn = _connect(self.b_dir)
        try:
            ref_id = conn.execute(
                "SELECT id FROM foreign_refs WHERE status = 'unresolved' "
                "LIMIT 1").fetchone()[0]
        finally:
            conn.close()
        summary = pin_foreign_ref(self.b_dir, ref_id)
        self.assertFalse(summary["pinned"])
        self.assertIn("error", summary)

    def test_unpin_non_pinned_is_reported(self):
        add_foreign(self.b_dir, self.a_dir, project_name="A")
        ref_id = self._one_resolved_ref_id()
        summary = unpin_foreign_ref(self.b_dir, ref_id)
        self.assertFalse(summary["unpinned"])


class TestPruneForeign(_ForeignFixture):

    OLD_TS = "2020-01-01T00:00:00"
    RECENT_TS = datetime.now().isoformat()

    def _insert_ref(self, status, last_resolved_at, tag=None):
        conn = _connect(self.b_dir)
        try:
            conn.execute(
                "INSERT INTO foreign_refs (local_node_id, invoked_name, "
                "foreign_c2d_path, status, last_resolved_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("B_main", tag or ("fn_%s" % status),
                 self.a_dir, status, last_resolved_at))
            conn.commit()
        finally:
            conn.close()

    def test_prunes_old_deleted_and_orphaned_only(self):
        self._insert_ref("deleted", self.OLD_TS)
        self._insert_ref("orphaned", self.OLD_TS)
        self._insert_ref("deleted", self.RECENT_TS, tag="fn_deleted_new")
        self._insert_ref("resolved", self.OLD_TS)
        summary = prune_foreign(self.b_dir, max_age_days=30)
        self.assertEqual(summary["pruned"], 2)
        statuses = {r["status"] for r in self._refs()}
        self.assertEqual(statuses, {"deleted", "resolved"})

    def test_never_resolved_rows_are_prunable(self):
        # last_resolved_at IS NULL means the ref never resolved — as
        # old as it can get, so the default window prunes it.
        self._insert_ref("deleted", None)
        summary = prune_foreign(self.b_dir, max_age_days=30)
        self.assertEqual(summary["pruned"], 1)

    def test_status_window_respected(self):
        self._insert_ref("deleted", self.OLD_TS)
        self._insert_ref("unresolved", self.OLD_TS)
        summary = prune_foreign(self.b_dir, max_age_days=30,
                                prune_statuses="deleted,orphaned")
        self.assertEqual(summary["pruned"], 1)
        statuses = {r["status"] for r in self._refs()}
        self.assertEqual(statuses, {"unresolved"})


class TestCheckCompat(_ForeignFixture):

    def _make_v2(self, functions, name="A_v2"):
        v2_dir = os.path.join(self.tmp, name)
        os.makedirs(v2_dir, exist_ok=True)
        _make_foreign_db(os.path.join(v2_dir, "code2database.db"),
                         functions)
        return v2_dir

    def test_ok_and_broken_and_signature_changed(self):
        add_foreign(self.b_dir, self.a_dir, project_name="A")
        # v2: sdk_open unchanged, json_parse signature changed,
        # other removed (parse ref -> broken)
        v2 = self._make_v2([
            {"id": "A_sdk_open", "name": "sdk_open",
             "signature": "int sdk_open(void)"},
            {"id": "A_json_parse", "name": "json_parse",
             "signature": "int json_parse(const char *)"},
        ])
        result = check_compat(self.b_dir, v2)
        self.assertNotIn("error", result)
        self.assertEqual(result["total_checked"], 2)
        self.assertEqual(result["ok_edges"], 1)
        self.assertEqual(result["signature_changed"], 1)
        self.assertEqual(result["broken_edges"], 0)
        # rename json_parse away entirely -> the parse ref breaks too
        v3 = self._make_v2([
            {"id": "A_sdk_open", "name": "sdk_open",
             "signature": "int sdk_open(void)"},
        ], name="A_v3")
        result = check_compat(self.b_dir, v3)
        self.assertEqual(result["broken_edges"], 1)
        self.assertEqual(result["signature_changed"], 0)
        self.assertTrue(result["broken_details"])

    def test_missing_against_db_rejected(self):
        add_foreign(self.b_dir, self.a_dir, project_name="A")
        nowhere = os.path.join(self.tmp, "nowhere")
        os.makedirs(nowhere)
        result = check_compat(self.b_dir, nowhere)
        self.assertIn("error", result)


class TestAttachContextManager(unittest.TestCase):

    def test_detach_guaranteed_even_on_exception(self):
        tmp = tempfile.mkdtemp(prefix="c2d_attach_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        db = os.path.join(tmp, "foreign.db")
        conn = sqlite3.connect(os.path.join(tmp, "main.db"))
        _make_foreign_db(db, [{"id": "X_f", "name": "f"}])
        try:
            with self.assertRaises(RuntimeError):
                with with_foreign_attached(conn, db, "probe_db"):
                    names = conn.execute(
                        "SELECT name FROM probe_db.functions").fetchall()
                    self.assertEqual(names[0][0], "f")
                    raise RuntimeError("boom")
        finally:
            attached = [r[1] for r in
                        conn.execute("PRAGMA database_list").fetchall()]
            conn.close()
        self.assertNotIn("probe_db", attached,
                         "DETACH must run even when the body raises")


class TestResolveByExactNameOverloads(unittest.TestCase):

    def test_signature_disambiguates_overloads(self):
        tmp = tempfile.mkdtemp(prefix="c2d_overload_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        db = os.path.join(tmp, "overload.db")
        _make_foreign_db(db, [
            {"id": "A_draw", "name": "draw",
             "signature": "void draw(int)"},
            {"id": "A_draw2", "name": "draw",
             "signature": "void draw(double)"},
        ])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            # No project prefix: both same-name overloads become
            # candidates so the signature can disambiguate (a project
            # prefix would short-circuit on the prefixed id).
            row = _resolve_by_exact_name(conn, "draw", "",
                                         invoked_signature="void draw(double)")
            self.assertIsNotNone(row)
            self.assertEqual(row["id"], "A_draw2")
            row = _resolve_by_exact_name(conn, "draw", "",
                                         invoked_signature="void draw(int)")
            self.assertIsNotNone(row)
            self.assertEqual(row["id"], "A_draw")
        finally:
            conn.close()


class TestBuilderWrappers(_ForeignFixture):
    """The cmd_c2d_* wrappers in code2database_builder print the JSON
    summary on stdout — the contract agents parse."""

    def _run(self, fn, **kw):
        import code2database_builder as builder
        out = io.StringIO()
        with redirect_stdout(out):
            fn(argparse.Namespace(**kw))
        return out.getvalue()

    def test_add_list_remove_wrappers_emit_json(self):
        out = self._run(builder_fn("cmd_c2d_add_foreign"),
                        graph=self.b_dir, foreign_c2d=self.a_dir,
                        project_name="A", rescan_unresolved=False)
        summary = json.loads(out)
        self.assertEqual(summary["resolved_count"], 2)
        out = self._run(builder_fn("cmd_c2d_list_foreign"), graph=self.b_dir)
        entries = json.loads(out)
        self.assertEqual(entries[0]["project_name"], "A")
        out = self._run(builder_fn("cmd_c2d_remove_foreign"),
                        graph=self.b_dir, foreign_c2d=self.a_dir)
        summary = json.loads(out)
        self.assertTrue(summary["removed"])

    def test_sync_wrapper_emits_json(self):
        self._run(builder_fn("cmd_c2d_add_foreign"),
                  graph=self.b_dir, foreign_c2d=self.a_dir,
                  project_name="A", rescan_unresolved=False)
        out = self._run(builder_fn("cmd_c2d_sync_foreign"),
                        graph=self.b_dir, foreign_c2d="")
        summary = json.loads(out)
        self.assertEqual(summary["synced_c2ds"][0]["status"], "unchanged")


def builder_fn(name):
    import code2database_builder as builder
    return getattr(builder, name)


if __name__ == "__main__":
    unittest.main()
