"""The knowledge base runs on a store dir that has no graph.

The kb index (kb_paragraphs + FTS5) lives in its own store file,
kb_index.db, next to memory/ and knowledge/. A dir populated with
only those stores must support the full kb workflow — query, rebuild,
capture, session context — and must never sprout code2database.db as
a side effect (a graph db in a graph-less dir poisons graph loaders
with misleading errors).
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.kb.kb_index import (
    _kb_db_path,
    _legacy_kb_db_path,
    rebuild_kb_index,
    query_kb,
    get_known_unknowns,
)
from _builder.memory.memory_store import MemoryStore


class _KbOnlyBase(unittest.TestCase):
    """A store dir with memory + knowledge but no graph artifacts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph_dir = os.path.join(self.tmp.name, "code2db-out")
        os.makedirs(self.graph_dir)
        self.store = MemoryStore(self.graph_dir)

    def _assert_no_graph_artifacts(self):
        for rel in ("code2database.db", "code2database_master.json"):
            self.assertFalse(
                os.path.exists(os.path.join(self.graph_dir, rel)),
                f"graph artifact {rel} must not be created by kb commands")


class TestKbOnlyStore(_KbOnlyBase):
    def test_query_creates_only_the_kb_store(self):
        self.store.add("how does the nvme queue doorbell work",
                       "write to the submission tail",
                       no_merge=True)
        rebuild_kb_index(self.graph_dir, verbose=False)
        results = query_kb(self.graph_dir, "nvme doorbell")
        self.assertTrue(results)
        self.assertTrue(os.path.isfile(_kb_db_path(self.graph_dir)))
        self._assert_no_graph_artifacts()

    def test_rebuild_then_query_on_kb_only_dir(self):
        self.store.add("how does the nvme queue doorbell work",
                       "write to the submission tail", no_merge=True)
        self.store.add("where is the admin passthrough handled",
                       "see the admin opcode switch", no_merge=True)
        summary = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertTrue(summary.get("rebuilt"))
        self.assertEqual(summary.get("memory_count"), 2)
        hits = query_kb(self.graph_dir, "admin passthrough")
        self.assertTrue(any("admin" in (h.get("title") or "")
                            for h in hits))
        self._assert_no_graph_artifacts()

    def test_known_unknowns_round_trip(self):
        from _builder.kb.kb_index import _kb_connect
        conn = _kb_connect(self.graph_dir)
        try:
            for _ in range(3):
                conn.execute(
                    "INSERT INTO kb_query_log (query, matched, "
                    "match_count, top_score, queried_at) "
                    "VALUES ('what gates the reset path', 0, 0, 0.0, "
                    "'2026-01-01T00:00:00')")
            conn.commit()
        finally:
            conn.close()
        kus = get_known_unknowns(self.graph_dir)
        self.assertEqual(len(kus), 1)
        self.assertEqual(kus[0]["occurrences"], 3)

    def test_known_unknowns_empty_without_store(self):
        # No kb activity yet: the read must not create the store file.
        self.assertFalse(os.path.exists(_kb_db_path(self.graph_dir)))
        self.assertEqual(get_known_unknowns(self.graph_dir), [])
        self.assertFalse(os.path.exists(_kb_db_path(self.graph_dir)))

    def test_memory_miss_does_not_create_graph_db(self):
        self.store.search("a question with no matching entry")
        self._assert_no_graph_artifacts()

    def test_legacy_helper_points_at_graph_db(self):
        self.assertEqual(
            os.path.basename(_legacy_kb_db_path(self.graph_dir)),
            "code2database.db")


class TestSessionInitOnKbOnly(_KbOnlyBase):
    def test_session_context_renders_without_graph(self):
        from _builder.mcp.session_init import build_session_context, \
            render_session_context
        self.store.add("how does the nvme queue doorbell work",
                       "write to the submission tail", no_merge=True)
        ctx = build_session_context(self.graph_dir)
        self.assertEqual(ctx["graph"].get("nodes"), 0)
        self.assertFalse(ctx["graph_present"])
        self.assertTrue(ctx["kb_present"])
        self.assertIsNone(ctx["freshness"])  # nothing to be stale
        self.assertEqual(ctx["memory"]["stats"]["active_entries"], 1)
        self.assertEqual(ctx["known_unknowns"], [])
        rendered = render_session_context(ctx)
        self.assertIn("knowledge/memory-only store", rendered)
        self.assertNotIn("STALE", rendered)
        self.assertTrue(any("fully usable" in h for h in ctx["hints"]))
        self._assert_no_graph_artifacts()

    def test_known_unknowns_surface_via_kb_store(self):
        from _builder.mcp.session_init import build_session_context
        from _builder.kb.kb_index import _kb_connect
        conn = _kb_connect(self.graph_dir)
        try:
            for _ in range(2):
                conn.execute(
                    "INSERT INTO kb_query_log (query, matched, "
                    "match_count, top_score, queried_at) "
                    "VALUES ('what gates the reset path', 0, 0, 0.0, "
                    "'2026-01-01T00:00:00')")
            conn.commit()
        finally:
            conn.close()
        ctx = build_session_context(self.graph_dir)
        self.assertEqual(len(ctx["known_unknowns"]), 1)


class TestLegacyStoreStillReadable(_KbOnlyBase):
    """A pre-isolation dir keeps its kb tables inside code2database.db."""

    def _make_legacy_kb_db(self):
        # Simulate the old layout: kb tables in the graph db. Build a
        # minimal kb_query_log by hand; the one-time copy tests in
        # test_kb_legacy_copy cover full-table migration.
        path = _legacy_kb_db_path(self.graph_dir)
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE kb_query_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                matched INTEGER NOT NULL,
                match_count INTEGER DEFAULT 0,
                top_score REAL,
                queried_at TEXT NOT NULL
            );
            INSERT INTO kb_query_log (query, matched, match_count,
                top_score, queried_at)
                VALUES ('legacy miss', 0, 0, 0.0, '2026-01-01T00:00:00');
        """)
        conn.commit()
        conn.close()
        return path

    def test_session_guard_accepts_legacy_layout(self):
        # The session-init guard must recognize the old home so an
        # existing project keeps attempting its known-unknowns layer
        # instead of silently skipping it.
        self._make_legacy_kb_db()
        from _builder.mcp.session_init import build_session_context
        ctx = build_session_context(self.graph_dir)  # must not raise
        self.assertIn("known_unknowns", ctx)


class TestForeignKbAttach(_KbOnlyBase):
    """A watched foreign C2D is searched through its kb index, new
    home first, pre-isolation home as fallback."""

    def _watch(self, foreign_dir, status="ok"):
        from _builder.kb.kb_index import _kb_connect
        conn = _kb_connect(self.graph_dir)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO watched_c2ds "
                "(c2d_path, project_name, last_synced_at, sync_status) "
                "VALUES (?, 'foreign', '2026-01-01T00:00:00', ?)",
                (foreign_dir, status))
            conn.commit()
        finally:
            conn.close()

    def _foreign_with_new_home(self):
        foreign = os.path.join(self.tmp.name, "other-out")
        os.makedirs(foreign, exist_ok=True)
        from _builder.kb.kb_index import _kb_connect
        conn = _kb_connect(foreign)
        try:
            conn.execute(
                "INSERT INTO kb_paragraphs (source_kind, source_file, "
                "para_index, title, body, weight, confidence, kind, "
                "created_at) VALUES ('knowledge', 'brief.json', 0, "
                "'foreign rule', 'always drain the completion queue "
                "before freeing the ring', 1.0, 1.0, "
                "'hard_rule', '2026-01-01T00:00:00')")
            conn.commit()
        finally:
            conn.close()
        return foreign

    def test_foreign_hits_via_new_home(self):
        foreign = self._foreign_with_new_home()
        self._watch(foreign)
        results = query_kb(self.graph_dir, "completion queue ring",
                           top_n=5)
        self.assertTrue(any(r.get("source_db") == foreign
                            for r in results), results)
        self._assert_no_graph_artifacts()

    def test_foreign_hits_via_legacy_home(self):
        foreign = self._foreign_with_new_home()
        # Relocate the populated store to the pre-isolation layout.
        os.rename(os.path.join(foreign, "kb_index.db"),
                  os.path.join(foreign, "code2database.db"))
        self._watch(foreign)
        results = query_kb(self.graph_dir, "completion queue ring",
                           top_n=5)
        self.assertTrue(any(r.get("source_db") == foreign
                            for r in results), results)

    def test_foreign_without_any_kb_home_is_skipped(self):
        foreign = os.path.join(self.tmp.name, "empty-out")
        os.makedirs(foreign, exist_ok=True)
        self._watch(foreign)
        results = query_kb(self.graph_dir, "completion queue ring",
                           top_n=5)
        self.assertEqual(results, [])


class TestForeignTablesHome(_KbOnlyBase):
    """foreign_refs / watched_c2ds are written to the kb store; every
    reader follows them there, with the pre-relocation graph db as a
    fallback."""

    NODE = "src_main"

    def _write_ref(self, conn, status="resolved"):
        conn.execute(
            "INSERT OR REPLACE INTO foreign_refs (local_node_id, "
            "invoked_name, foreign_c2d_path, foreign_project_name, "
            "foreign_node_id, foreign_name, status, last_resolved_at) "
            "VALUES (?, 'util_sum', '/other-out', 'other', "
            "'other_util_sum', 'util_sum', ?, "
            "'2026-01-01T00:00:00')", (self.NODE, status))
        conn.commit()

    def test_reader_finds_refs_in_kb_store(self):
        # Cross-C2D refs in a dir with no graph db at all.
        from _builder.kb.kb_index import _kb_connect
        conn = _kb_connect(self.graph_dir)
        try:
            self._write_ref(conn)
        finally:
            conn.close()
        from _builder.query.query_helpers import _fetch_foreign_refs_for_node
        refs = _fetch_foreign_refs_for_node(self.graph_dir, self.NODE)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["foreign_name"], "util_sum")
        self._assert_no_graph_artifacts()

    def test_mcp_tool_reads_kb_store(self):
        from _builder.kb.kb_index import _kb_connect
        conn = _kb_connect(self.graph_dir)
        try:
            self._write_ref(conn)
        finally:
            conn.close()
        from _builder.mcp.mcp_c2d_tools import _tool_foreign_refs
        out = _tool_foreign_refs({"node": self.NODE}, self.graph_dir)
        self.assertEqual(out["foreign_refs_count"], 1)
        self.assertEqual(out["foreign_refs"][0]["foreign_c2d_path"],
                         "/other-out")

    def test_reader_falls_back_to_legacy_home(self):
        # Pre-relocation layout: refs inside code2database.db, no
        # kb_index.db yet — the reader must still see them.
        conn = sqlite3.connect(
            os.path.join(self.graph_dir, "code2database.db"))
        conn.executescript("""
            CREATE TABLE foreign_refs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                local_node_id TEXT NOT NULL,
                invoked_name TEXT NOT NULL,
                invoked_signature TEXT,
                foreign_c2d_path TEXT NOT NULL,
                foreign_project_name TEXT,
                foreign_node_id TEXT,
                foreign_name TEXT,
                foreign_domain TEXT,
                foreign_source_file TEXT,
                foreign_signature TEXT,
                status TEXT NOT NULL DEFAULT 'unresolved',
                resolution_strategy TEXT,
                last_resolved_at TEXT,
                call_order INTEGER,
                call_condition TEXT
            );
            CREATE TABLE watched_c2ds (
                c2d_path TEXT PRIMARY KEY,
                project_name TEXT,
                db_mtime_at_sync TEXT,
                db_size_at_sync INTEGER,
                functions_count_at_sync INTEGER,
                last_synced_at TEXT NOT NULL,
                sync_status TEXT NOT NULL DEFAULT 'unknown'
            );
        """)
        self._write_ref(conn)
        conn.close()
        from _builder.query.query_helpers import _fetch_foreign_refs_for_node
        refs = _fetch_foreign_refs_for_node(self.graph_dir, self.NODE)
        self.assertEqual(len(refs), 1)

    def test_reader_returns_empty_without_any_store(self):
        from _builder.query.query_helpers import _fetch_foreign_refs_for_node
        self.assertEqual(
            _fetch_foreign_refs_for_node(self.graph_dir, self.NODE), [])


    def test_fresh_graph_db_has_no_kb_tables(self):
        # A newly built graph db hosts only graph tables; the kb and
        # cross-C2D tables belong to the kb store.
        from _builder.graph.sqlite_store import SQLiteStore
        db_path = os.path.join(self.tmp.name, "graph",
                               "code2database.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with SQLiteStore(db_path) as store:
            store.store_functions([
                {"id": "src_main", "name": "main", "domain": "root",
                 "source_file": "src/main.c", "line_number": 1}])
        conn = sqlite3.connect(db_path)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        for t in ("kb_paragraphs", "kb_items", "kb_query_log",
                  "foreign_refs", "watched_c2ds"):
            self.assertNotIn(t, tables)
        self.assertIn("functions", tables)


class TestSaveSearchableImmediately(_KbOnlyBase):
    """A saved memory must be searchable through the unified index
    without a manual kb-rebuild-index — capture then query is the core
    kb-only workflow."""

    def _save(self, question, answer, **kw):
        from types import SimpleNamespace
        from _builder.memory.memory_cmd import cmd_save_memory
        args = SimpleNamespace(
            graph=self.graph_dir, question=question, answer=answer,
            chains=None, tags="", node_ids="", category="",
            author="tester", symbol=None, no_merge=False, correct=False)
        for k, v in kw.items():
            setattr(args, k, v)
        cmd_save_memory(args)

    def test_save_then_query_without_rebuild(self):
        self._save("how does the nvme queue doorbell work",
                   "write to the submission tail")
        hits = query_kb(self.graph_dir, "nvme doorbell")
        self.assertTrue(any("doorbell" in (h.get("title") or "")
                            for h in hits), hits)
        self._assert_no_graph_artifacts()

    def test_merged_root_stays_searchable(self):
        self._save("how does the nvme queue doorbell work",
                   "write to the submission tail")
        self._save("how does the nvme queue doorbell work",
                   "a much longer and more detailed answer about "
                   "doorbells that outranks the first one " * 3)
        hits = query_kb(self.graph_dir, "nvme doorbell")
        titles = [h.get("title") or "" for h in hits]
        self.assertTrue(any("doorbell" in t for t in titles), hits)

    def test_correct_path_reindexes(self):
        self._save("how does the nvme queue doorbell work",
                   "wrong answer")
        from types import SimpleNamespace
        from _builder.memory.memory_cmd import cmd_save_memory
        cmd_save_memory(SimpleNamespace(
            graph=self.graph_dir,
            question="how does the nvme queue doorbell work",
            answer="write to the submission tail doorbell register",
            chains=None, tags="", node_ids="", category="",
            author="tester", symbol=None, no_merge=False, correct=True))
        hits = query_kb(self.graph_dir, "submission tail doorbell")
        self.assertTrue(hits)
        self.assertIn("write to the submission tail doorbell register",
                      hits[0]["body"])


if __name__ == "__main__":
    unittest.main()
