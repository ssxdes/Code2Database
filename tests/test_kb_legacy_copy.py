"""One-time import of kb tables from the pre-isolation home.

Projects built before the kb index moved to kb_index.db keep their
kb_* tables inside code2database.db. The first _kb_connect after the
move must copy them across (once, without duplicating on later runs,
and without touching graph tables), so kb-query / known-unknowns /
watched foreign C2Ds keep working on an existing project.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.kb.kb_index import (
    _kb_connect,
    _kb_db_path,
    _legacy_kb_db_path,
    get_known_unknowns,
    query_kb,
)


class _LegacyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph_dir = os.path.join(self.tmp.name, "code2db-out")
        os.makedirs(self.graph_dir)

    def _populate_then_relocate(self):
        """Build a populated kb store, then relabel it as the legacy
        code2database.db layout (identical schema by construction)."""
        conn = _kb_connect(self.graph_dir)
        try:
            conn.execute(
                "INSERT INTO kb_paragraphs (source_kind, source_file, "
                "para_index, title, body, tags, weight, confidence, "
                "kind, created_at) VALUES "
                "('memory', 'db/mem_1.json', 0, 'how does the doorbell "
                "work', 'write the submission tail', NULL, 1.5, 1.0, "
                "'memory_qa', '2026-01-01T00:00:00')")
            conn.execute(
                "INSERT INTO kb_paragraphs (source_kind, source_file, "
                "para_index, title, body, tags, weight, confidence, "
                "kind, created_at) VALUES "
                "('knowledge', 'brief.json', 0, 'Hard Rule', "
                "'lock the queue before touching the tail', NULL, 1.0, "
                "1.0, 'hard_rule', '2026-01-01T00:00:00')")
            for _ in range(2):
                conn.execute(
                    "INSERT INTO kb_query_log (query, matched, "
                    "match_count, top_score, queried_at) VALUES "
                    "('what gates the reset path', 0, 0, 0.0, "
                    "'2026-01-01T00:00:00')")
            conn.execute(
                "INSERT INTO watched_c2ds (c2d_path, project_name, "
                "last_synced_at, sync_status) VALUES "
                "('/elsewhere/other-out', 'other', "
                "'2026-01-01T00:00:00', 'ok')")
            conn.commit()
        finally:
            conn.close()
        # Relocate: the populated store becomes the legacy home.
        shutil.move(_kb_db_path(self.graph_dir),
                    _legacy_kb_db_path(self.graph_dir))
        for suffix in ("-wal", "-shm"):
            wal = _kb_db_path(self.graph_dir) + suffix
            if os.path.exists(wal):
                os.remove(wal)

    def _marker(self):
        conn = sqlite3.connect(_kb_db_path(self.graph_dir))
        try:
            row = conn.execute(
                "SELECT value FROM kb_meta "
                "WHERE key = 'legacy_import_done'").fetchone()
            return row[0] if row else None
        finally:
            conn.close()


class TestLegacyCopy(_LegacyBase):
    def test_first_connect_copies_rows_once(self):
        self._populate_then_relocate()
        conn = _kb_connect(self.graph_dir)
        conn.close()
        self.assertTrue(os.path.isfile(_kb_db_path(self.graph_dir)))
        conn = sqlite3.connect(_kb_db_path(self.graph_dir))
        try:
            paragraphs = conn.execute(
                "SELECT COUNT(*) FROM kb_paragraphs").fetchone()[0]
            logs = conn.execute(
                "SELECT COUNT(*) FROM kb_query_log").fetchone()[0]
            watched = conn.execute(
                "SELECT COUNT(*) FROM watched_c2ds").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(paragraphs, 2)
        self.assertEqual(logs, 2)
        self.assertEqual(watched, 1)
        self.assertEqual(self._marker(), "1")

        # Second connect: no duplication, marker holds.
        conn = _kb_connect(self.graph_dir)
        conn.close()
        conn = sqlite3.connect(_kb_db_path(self.graph_dir))
        try:
            paragraphs = conn.execute(
                "SELECT COUNT(*) FROM kb_paragraphs").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(paragraphs, 2)

    def test_copied_index_serves_queries(self):
        self._populate_then_relocate()
        hits = query_kb(self.graph_dir, "doorbell submission")
        self.assertTrue(any("doorbell" in (h.get("title") or "")
                            for h in hits))
        kus = get_known_unknowns(self.graph_dir)
        self.assertEqual(len(kus), 1)
        self.assertEqual(kus[0]["occurrences"], 2)

    def test_legacy_home_left_untouched(self):
        self._populate_then_relocate()
        before = os.path.getmtime(_legacy_kb_db_path(self.graph_dir))
        conn = _kb_connect(self.graph_dir)
        conn.close()
        conn = sqlite3.connect(_legacy_kb_db_path(self.graph_dir))
        try:
            still_there = conn.execute(
                "SELECT COUNT(*) FROM kb_paragraphs").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(still_there, 2)
        self.assertEqual(os.path.getmtime(
            _legacy_kb_db_path(self.graph_dir)), before)

    def test_no_legacy_db_sets_marker_without_copies(self):
        conn = _kb_connect(self.graph_dir)
        conn.close()
        self.assertEqual(self._marker(), "1")
        conn = sqlite3.connect(_kb_db_path(self.graph_dir))
        try:
            paragraphs = conn.execute(
                "SELECT COUNT(*) FROM kb_paragraphs").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(paragraphs, 0)

    def test_graph_db_without_kb_tables_marks_done(self):
        # A code2database.db that only ever held graph tables: the
        # import must not crash and must not copy anything.
        conn = sqlite3.connect(_legacy_kb_db_path(self.graph_dir))
        conn.execute("CREATE TABLE functions (id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        conn = _kb_connect(self.graph_dir)
        conn.close()
        self.assertEqual(self._marker(), "1")
        conn = sqlite3.connect(_kb_db_path(self.graph_dir))
        try:
            paragraphs = conn.execute(
                "SELECT COUNT(*) FROM kb_paragraphs").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(paragraphs, 0)

    def test_nonempty_target_not_overwritten(self):
        # A kb_index.db that already has rows (e.g. a rebuild ran
        # before the first legacy connect) must not be merged into.
        self._populate_then_relocate()
        conn = _kb_connect(self.graph_dir)  # import happens here
        conn.close()
        conn = sqlite3.connect(_kb_db_path(self.graph_dir))
        try:
            conn.execute("DELETE FROM kb_paragraphs")
            conn.execute("INSERT INTO kb_paragraphs (source_kind, "
                         "source_file, para_index, title, body, weight, "
                         "confidence, kind, created_at) VALUES "
                         "('memory', 'db/mem_9.json', 0, 'local row', "
                         "'body', 1.0, 1.0, 'memory_qa', "
                         "'2026-02-01T00:00:00')")
            conn.execute("DELETE FROM kb_meta "
                         "WHERE key = 'legacy_import_done'")
            conn.commit()
        finally:
            conn.close()
        conn = _kb_connect(self.graph_dir)
        conn.close()
        conn = sqlite3.connect(_kb_db_path(self.graph_dir))
        try:
            titles = [r[0] for r in conn.execute(
                "SELECT title FROM kb_paragraphs")]
        finally:
            conn.close()
        self.assertEqual(titles, ["local row"])


if __name__ == "__main__":
    unittest.main()
