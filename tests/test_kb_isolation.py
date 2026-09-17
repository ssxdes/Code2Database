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
        from _builder.mcp.session_init import build_session_context
        self.store.add("how does the nvme queue doorbell work",
                       "write to the submission tail", no_merge=True)
        ctx = build_session_context(self.graph_dir)
        self.assertEqual(ctx["graph"].get("nodes"), 0)
        self.assertEqual(ctx["memory"]["stats"]["active_entries"], 1)
        self.assertEqual(ctx["known_unknowns"], [])
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


if __name__ == "__main__":
    unittest.main()
