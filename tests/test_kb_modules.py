"""Unit tests for kb_index, kb_cluster, kb_global, kb_audit, kb_conflict.

Unified knowledge base modules.

These tests use a temporary code2database.db to verify:
- FTS5 table creation + triggers
- rebuild_kb_index from synthetic memory/*.json + knowledge/*.md files
- query_kb returns ranked hits
- kb_cluster union-find clustering
- kb_global add/search/share/import
- kb_audit reports
- kb_conflict contradiction detection
- kb_conflict forget + rollback
"""
import json
import os
import io
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.kb.kb_index import (
    _kb_connect,
    rebuild_kb_index,
    query_kb,
    upsert_kb_paragraph,
    delete_kb_paragraphs_by_source,
    get_known_unknowns,
    _fts5_escape,
    _split_markdown_paragraphs,
    _try_load_vec_ext,
    build_ann_index,
    query_ann,
)
from _builder.kb.kb_cluster import cluster_kb
from _builder.kb.kb_audit import audit_kb, kb_stats, write_audit_log_entry
from _builder.kb.kb_conflict import detect_conflicts, forget_kb_paragraph, rollback_kb_item


def _has_sqlite_vec():
    try:
        import sqlite_vec  # noqa: F401
        return True
    except ImportError:
        return False


requires_sqlite_vec = unittest.skipUnless(_has_sqlite_vec(),
                                          "sqlite-vec not installed")


def _make_memory_entry(graph_dir, entry_id, question, answer, tags=None,
                       status="trusted", subdir="root"):
    """Add a memory entry to graph_dir/memory/memory.db (SQLite store).

    entry_id is informational — the store assigns ids sequentially
    (fresh dirs match the requested ids). status maps to the store's
    lifecycle ('trusted' → active).
    """
    from _builder.memory.memory_store import MemoryStore
    store = MemoryStore(graph_dir)
    return store.add(question=question, answer=answer, tags=tags or [],
                     no_merge=True)


def _make_knowledge_md(graph_dir, fname, content):
    """Write a brief-like knowledge/brief.json (name kept for history).

    The 'content' markdown is stored as the brief description so kb
    indexing picks it up as a knowledge paragraph.
    """
    know_dir = os.path.join(graph_dir, "knowledge")
    os.makedirs(know_dir, exist_ok=True)
    brief = {
        "schema_version": 1,
        "project": "testproj",
        "one_liner": "test project",
        "description": content,
        "hard_rules": [
            {"rule": "All bdev modules must call bdev_register() "
                     "before use.", "type": "api"},
        ],
        "modes": [], "key_abstractions": [], "conventions": [],
        "pitfalls": [], "query_paths": [], "must_know": "",
        "graph_stats": {},
    }
    with open(os.path.join(know_dir, "brief.json"), "w", encoding="utf-8") as f:
        json.dump(brief, f, ensure_ascii=False, indent=2)


def _settle_sqlite_store(db_path):
    """Fold any transient -wal/-shm sidecars into the main db file.

    A cleanly closed store is settled on most filesystems, but
    connection lifecycle on a shared runner can leave sidecars behind
    with the newest mtime in the dir. Tests that pin the skip decision
    exercise marker logic, not sidecar lifecycle, so they start from a
    checkpointed store.
    """
    if not os.path.exists(db_path):
        return
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def _signature_state(graph_dir):
    """Diagnostic context for skip assertions: the stored markers plus
    every file in the scanned dirs with its mtime, so a skip that did
    not engage names the file whose timestamp moved."""
    lines = []
    conn = _kb_connect(graph_dir)
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT value FROM kb_meta WHERE key = 'last_rebuild_mtime'"
            ).fetchone()
            lines.append(f"marker={row[0] if row else None}")
            frow = conn.execute(
                "SELECT value FROM kb_meta WHERE key = 'last_rebuild_file_count'"
            ).fetchone()
            lines.append(f"file_count_marker={frow[0] if frow else None}")
        finally:
            conn.close()
    for d in ("memory", "knowledge"):
        p = os.path.join(graph_dir, d)
        if not os.path.isdir(p):
            lines.append(f"{d}/ absent")
            continue
        for fname in sorted(os.listdir(p)):
            fp = os.path.join(p, fname)
            if os.path.isfile(fp):
                lines.append(f"{d}/{fname} mtime={os.path.getmtime(fp)!r}")
    return "\n".join(lines)


def _drop_sidecar_files(graph_dir, names, size, mtime):
    """Create sqlite sidecar look-alikes with a chosen size and mtime."""
    mem_dir = os.path.join(graph_dir, "memory")
    for name in names:
        fp = os.path.join(mem_dir, name)
        with open(fp, "wb") as f:
            f.write(b"\0" * size)
        os.utime(fp, (mtime, mtime))


class TestFTS5Escape(unittest.TestCase):
    def test_simple_query(self):
        result = _fts5_escape("hello world")
        self.assertIn('"hello"', result)
        self.assertIn('"world"', result)

    def test_special_chars_stripped(self):
        result = _fts5_escape("hello; DROP TABLE--world")
        # Alphanumeric tokens are preserved (DROP, TABLE are alphanumeric)
        # but punctuation/SQL syntax chars are stripped
        self.assertNotIn(";", result)
        self.assertNotIn("--", result)
        self.assertIn('"hello"', result)
        self.assertIn('"world"', result)
        # DROP and TABLE are valid alphanumeric tokens, so they're kept
        self.assertIn('"DROP"', result)
        self.assertIn('"TABLE"', result)

    def test_empty_query(self):
        result = _fts5_escape("")
        self.assertEqual(result, '""')


class TestMarkdownParagraphSplit(unittest.TestCase):
    def test_split_by_h2_headings(self):
        text = "# Title\n\nIntro\n\n## First\n\nBody 1\n\n## Second\n\nBody 2"
        paras = _split_markdown_paragraphs(text)
        # 3 paragraphs: preamble ("Title"), "First", "Second"
        # (the preamble before the first ## heading is captured too)
        self.assertEqual(len(paras), 3)
        self.assertEqual(paras[1][0], "First")
        self.assertIn("Body 1", paras[1][1])
        self.assertEqual(paras[2][0], "Second")

    def test_no_headings_returns_whole_file(self):
        text = "Just some content without headings."
        paras = _split_markdown_paragraphs(text)
        self.assertEqual(len(paras), 1)


class TestRebuildAndQueryKB(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="kb_test_")
        self.graph_dir = os.path.join(self.tmpdir, "code2db-out")
        os.makedirs(self.graph_dir, exist_ok=True)
        # Create a synthetic SQLiteStore-compatible db by calling connect
        # via kb_index._kb_connect (which creates the table idempotently)
        # But we need the db file to exist first; create empty.
        from _builder.graph.sqlite_store import SQLiteStore
        store = SQLiteStore(os.path.join(self.graph_dir, "code2database.db"))
        store.connect()
        store.close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_rebuild_from_memory_and_knowledge(self):
        # Synthesize memory entries
        _make_memory_entry(self.graph_dir, 1,
                           "How does bdev register io_device?",
                           "bdev_register() calls io_device_register()",
                           tags=["bdev", "io_device"])
        _make_memory_entry(self.graph_dir, 2,
                           "What does bdev_unregister do?",
                           "Calls io_device_unregister()",
                           tags=["bdev"])
        # Synthesize knowledge .md
        _make_knowledge_md(self.graph_dir, "principles.md",
                           "# Principles\n\n## bdev registration\n\n"
                           "All bdev modules must call bdev_register() before use.\n\n"
                           "## thread safety\n\n"
                           "Per-thread event loops; no locks needed within thread.\n")
        # Rebuild
        summary = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertTrue(summary["rebuilt"])
        self.assertEqual(summary["memory_count"], 2)
        self.assertGreaterEqual(summary["knowledge_count"], 2)  # at least 2 paragraphs

    def test_rebuild_persists_marker_and_skips_when_unchanged(self):
        # The post-data work (FTS rebuild command + last_rebuild_mtime
        # marker) runs in its own transaction after the data commit; it
        # must be persisted or every rebuild redoes the full work.
        _make_memory_entry(self.graph_dir, 1,
                           "How does bdev register io_device?",
                           "bdev_register() calls io_device_register()")
        _settle_sqlite_store(
            os.path.join(self.graph_dir, "memory", "memory.db"))
        first = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertTrue(first["rebuilt"])
        conn = _kb_connect(self.graph_dir)
        try:
            row = conn.execute(
                "SELECT value FROM kb_meta WHERE key = 'last_rebuild_mtime'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        stored = row[0]
        second = rebuild_kb_index(self.graph_dir, verbose=False)
        ctx = (f"stored marker {stored}; second={second}; "
               f"scan state now:\n{_signature_state(self.graph_dir)}")
        self.assertFalse(second["rebuilt"], ctx)
        self.assertEqual(second.get("reason"), "unchanged", ctx)

    def test_query_returns_memory_and_knowledge(self):
        _make_memory_entry(self.graph_dir, 1,
                           "How does bdev register io_device?",
                           "bdev_register() calls io_device_register()",
                           tags=["bdev"])
        _make_knowledge_md(self.graph_dir, "principles.md",
                           "## bdev registration\n\nAll bdev modules must call bdev_register.\n")
        rebuild_kb_index(self.graph_dir, verbose=False)
        # Query
        results = query_kb(self.graph_dir, "bdev register", top_n=10)
        self.assertGreater(len(results), 0)
        # Should find both memory and knowledge entries
        source_kinds = {r["source_kind"] for r in results}
        self.assertIn("memory", source_kinds)
        self.assertIn("knowledge", source_kinds)
        # Top result should be returned (BM25 score may be 0 for very
        # small corpora, but the result is still ranked)
        self.assertIn("score", results[0])

    def test_query_with_kinds_filter(self):
        _make_memory_entry(self.graph_dir, 1,
                           "bdev question", "bdev answer",
                           tags=["bdev"])
        _make_knowledge_md(self.graph_dir, "principles.md",
                           "## bdev principle\n\nbdev principle body.\n")
        rebuild_kb_index(self.graph_dir, verbose=False)
        # Filter to only memory
        results = query_kb(self.graph_dir, "bdev", kinds=["memory_qa"])
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertEqual(r["source_kind"], "memory")
        # Filter to only knowledge
        results = query_kb(self.graph_dir, "bdev", kinds=["hard_rule", "description"])
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertEqual(r["source_kind"], "knowledge")

    def test_query_pure_cjk_falls_back_to_similarity(self):
        # unicode61 FTS cannot match CJK runs — before the fallback a
        # pure-Chinese query escaped to an empty FTS phrase and
        # silently returned zero results.
        _make_memory_entry(self.graph_dir, 1,
                           "登录失败如何处理", "检查 session 状态后重试")
        _make_knowledge_md(self.graph_dir, "principles.md",
                           "## 线程安全\n\n每个线程运行独立的事件循环。\n")
        rebuild_kb_index(self.graph_dir, verbose=False)
        results = query_kb(self.graph_dir, "登录失败", top_n=10)
        self.assertTrue(results)
        self.assertEqual(results[0]["source_kind"], "memory")
        self.assertGreater(results[0]["score"], 0.0)

    def test_query_mixed_cjk_keeps_fts_latin_hits(self):
        # A mixed query must still return its Latin FTS matches.
        _make_memory_entry(self.graph_dir, 1,
                           "How does bdev register io_device?",
                           "bdev_register() calls io_device_register()")
        rebuild_kb_index(self.graph_dir, verbose=False)
        results = query_kb(self.graph_dir, "bdev 登录", top_n=10)
        self.assertTrue(results)
        self.assertIn("bdev", results[0]["title"] + results[0]["body"])

    def test_query_no_match_returns_empty(self):
        _make_memory_entry(self.graph_dir, 1,
                           "unrelated question", "unrelated answer")
        rebuild_kb_index(self.graph_dir, verbose=False)
        results = query_kb(self.graph_dir, "completely_unrelated_topic_xyzzy", top_n=10)
        self.assertEqual(len(results), 0)

    def test_unmatched_queries_surface_as_known_unknowns(self):
        # The query log feeds the feedback loop: a question asked
        # repeatedly with zero hits is a gap to fill; a matched query
        # never counts as one.
        _make_memory_entry(self.graph_dir, 1, "bdev question", "bdev answer")
        rebuild_kb_index(self.graph_dir, verbose=False)
        for _ in range(2):
            query_kb(self.graph_dir, "completely_unrelated_topic_xyzzy",
                     top_n=5)
        query_kb(self.graph_dir, "bdev", top_n=5)
        kus = get_known_unknowns(self.graph_dir, min_occurrences=2)
        self.assertTrue(any(k["query"] == "completely_unrelated_topic_xyzzy"
                            for k in kus))
        self.assertFalse(any(k["query"] == "bdev" for k in kus))

    def test_upsert_and_delete(self):
        rid = upsert_kb_paragraph(self.graph_dir, "memory", "test.json",
                                   "test title", "test body", tags=["t1"])
        self.assertGreater(rid, 0)
        results = query_kb(self.graph_dir, "test", top_n=5)
        self.assertGreater(len(results), 0)
        # Delete
        deleted = delete_kb_paragraphs_by_source(self.graph_dir, "test.json")
        self.assertGreaterEqual(deleted, 1)
        results = query_kb(self.graph_dir, "test", top_n=5)
        self.assertEqual(len(results), 0)

    def test_upsert_dedup_updates_existing_row(self):
        """upsert_kb_paragraph with the same (source_kind, source_file)
        updates the existing row instead of creating a duplicate."""
        rid1 = upsert_kb_paragraph(self.graph_dir, "memory", "dedup.json",
                                    "original title", "original body")
        rid2 = upsert_kb_paragraph(self.graph_dir, "memory", "dedup.json",
                                    "updated title", "updated body")
        self.assertEqual(rid1, rid2, "second upsert should return same id")
        conn = _kb_connect(self.graph_dir)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM kb_paragraphs "
                "WHERE source_file = 'dedup.json'"
            ).fetchone()[0]
            self.assertEqual(count, 1, "should have exactly 1 row, not duplicates")
            row = conn.execute(
                "SELECT title, body FROM kb_paragraphs WHERE id = ?",
                (rid1,)).fetchone()
            self.assertEqual(row["title"], "updated title")
            self.assertEqual(row["body"], "updated body")
        finally:
            conn.close()

    def test_see_also_populated_after_clustering(self):
        """Results with scope_id carry see_also siblings."""
        upsert_kb_paragraph(self.graph_dir, "memory", "a.json",
                            "bdev register", "bdev_register calls io_device_register")
        upsert_kb_paragraph(self.graph_dir, "memory", "b.json",
                            "bdev registration flow", "bdev_register registers io_device")
        upsert_kb_paragraph(self.graph_dir, "memory", "c.json",
                            "unrelated topic", "completely different content here")
        # Cluster to assign scope_ids
        cluster_kb(self.graph_dir, threshold=0.15, verbose=False)
        results = query_kb(self.graph_dir, "bdev", top_n=10)
        self.assertGreater(len(results), 0)
        # At least one result should have a scope_id and see_also
        has_see_also = any(
            r.get("scope_id") is not None and len(r.get("see_also", [])) > 0
            for r in results
        )
        self.assertTrue(has_see_also,
                        f"no result had see_also: {[(r.get('scope_id'), r.get('see_also')) for r in results]}")
        # The see_also entries must exclude the result itself
        for r in results:
            for sa in r.get("see_also", []):
                self.assertNotEqual(sa["id"], r["id"])

    def test_update_access_false_skips_access_count_increment(self):
        """update_access=False must not mutate the DB."""
        upsert_kb_paragraph(self.graph_dir, "memory", "test.json",
                            "access test", "access test body content")
        # Query with update_access=False
        results = query_kb(self.graph_dir, "access", top_n=5,
                           update_access=False, log_query=False)
        self.assertGreater(len(results), 0)
        rid = results[0]["id"]
        # Check access_count is still 0
        conn = _kb_connect(self.graph_dir)
        try:
            row = conn.execute(
                "SELECT access_count FROM kb_paragraphs WHERE id = ?",
                (rid,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["access_count"], 0,
                         "access_count must not change when update_access=False")

    def test_update_access_true_increments_access_count(self):
        """update_access=True (default) increments access_count."""
        upsert_kb_paragraph(self.graph_dir, "memory", "test.json",
                            "access test", "access test body content")
        results = query_kb(self.graph_dir, "access", top_n=5,
                           update_access=True, log_query=False)
        self.assertGreater(len(results), 0)
        rid = results[0]["id"]
        conn = _kb_connect(self.graph_dir)
        try:
            row = conn.execute(
                "SELECT access_count FROM kb_paragraphs WHERE id = ?",
                (rid,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertGreater(row["access_count"], 0,
                           "access_count must increment when update_access=True")

    def test_min_weight_filter(self):
        """min_weight filters out low-weight entries."""
        upsert_kb_paragraph(self.graph_dir, "memory", "low.json",
                            "weight test", "weight test body", weight=0.3)
        upsert_kb_paragraph(self.graph_dir, "memory", "high.json",
                            "weight test high", "weight test body high", weight=2.0)
        results = query_kb(self.graph_dir, "weight", top_n=10, min_weight=1.0)
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertGreaterEqual(r["weight"], 1.0)

    def test_max_tokens_truncates_body(self):
        """max_tokens truncates very long body text."""
        long_body = "release memory " * 500  # ~8000 chars
        upsert_kb_paragraph(self.graph_dir, "memory", "long.json",
                            "truncation test", long_body)
        results = query_kb(self.graph_dir, "truncation", top_n=5,
                           max_tokens=100, update_access=False, log_query=False)
        self.assertGreater(len(results), 0)
        for r in results:
            if "truncation" in r["title"].lower():
                self.assertIn("...", r["body"])

    def test_empty_query_returns_empty(self):
        results = query_kb(self.graph_dir, "", top_n=5)
        self.assertEqual(len(results), 0)

    def test_whitespace_query_returns_empty(self):
        results = query_kb(self.graph_dir, "   ", top_n=5)
        self.assertEqual(len(results), 0)


class TestRebuildSkipSignature(unittest.TestCase):
    """The incremental skip decides 'unchanged' from the mtimes of the
    source dirs; these tests pin which files count toward it."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="kb_skip_test_")
        self.graph_dir = os.path.join(self.tmpdir, "code2db-out")
        os.makedirs(self.graph_dir, exist_ok=True)
        from _builder.graph.sqlite_store import SQLiteStore
        store = SQLiteStore(os.path.join(self.graph_dir, "code2database.db"))
        store.connect()
        store.close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed_memory(self):
        _make_memory_entry(self.graph_dir, 1,
                           "How does bdev register io_device?",
                           "bdev_register() calls io_device_register()")
        _settle_sqlite_store(
            os.path.join(self.graph_dir, "memory", "memory.db"))

    def test_new_content_flips_the_decision_back_to_rebuild(self):
        self._seed_memory()
        first = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertTrue(first["rebuilt"])
        second = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertFalse(
            second["rebuilt"], _signature_state(self.graph_dir))
        _make_memory_entry(self.graph_dir, 2,
                           "What does bdev_unregister do?",
                           "Calls io_device_unregister()")
        third = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertTrue(third["rebuilt"])
        self.assertEqual(third["memory_count"], 2)

    def test_sidecars_appearing_after_a_rebuild_do_not_defeat_the_skip(self):
        self._seed_memory()
        first = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertTrue(first["rebuilt"])
        # Bookkeeping a lingering connection leaves behind: newest
        # mtimes in the dir, zero content signal (the -wal stays at its
        # 32-byte header, i.e. frameless).
        _future = time.time() + 60
        _drop_sidecar_files(
            self.graph_dir,
            ("memory.db-shm", "memory.db-journal", "memory.lock"),
            32768, _future)
        _drop_sidecar_files(
            self.graph_dir, ("memory.db-wal",), 32, _future)
        second = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertFalse(
            second["rebuilt"], _signature_state(self.graph_dir))
        self.assertEqual(second.get("reason"), "unchanged")

    def test_sidecars_vanishing_between_rebuilds_still_skips(self):
        # The shape a shared runner produced: sidecars present when the
        # marker was stored, cleaned up before the next scan. (The ro
        # reader inside the first rebuild may clean them up on its own
        # already, so the removal below is best-effort.)
        self._seed_memory()
        _future = time.time() + 60
        _drop_sidecar_files(
            self.graph_dir, ("memory.db-shm",), 32768, _future)
        _drop_sidecar_files(
            self.graph_dir, ("memory.db-wal",), 32, _future)
        first = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertTrue(first["rebuilt"])
        for name in ("memory.db-shm", "memory.db-wal"):
            fp = os.path.join(self.graph_dir, "memory", name)
            if os.path.exists(fp):
                os.remove(fp)
        second = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertFalse(
            second["rebuilt"], _signature_state(self.graph_dir))
        self.assertEqual(second.get("reason"), "unchanged")

    def test_frameless_wal_ignored_but_framed_wal_rebuilds(self):
        # A -wal at or under its 32-byte header holds no frames and is
        # bookkeeping; one frame already makes it bigger than 32 bytes,
        # and that state (crashed or active writer) must rebuild.
        self._seed_memory()
        first = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertTrue(first["rebuilt"])
        _drop_sidecar_files(
            self.graph_dir, ("memory.db-wal",), 32, time.time() + 60)
        skipped = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertFalse(
            skipped["rebuilt"], _signature_state(self.graph_dir))
        _drop_sidecar_files(
            self.graph_dir, ("memory.db-wal",), 33, time.time() + 120)
        rebuilt = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertTrue(
            rebuilt["rebuilt"], _signature_state(self.graph_dir))

    def test_deleted_source_file_rebuilds_and_drops_its_paragraphs(self):
        # Strict marker equality (not >=): a shrunken max means a file
        # went away, and its paragraphs are stale until a rebuild drops
        # them.
        _make_memory_entry(self.graph_dir, 1, "mem question", "mem answer")
        _make_knowledge_md(self.graph_dir, "principles.md",
                           "## knowledge section\n\nbody text\n")
        _settle_sqlite_store(
            os.path.join(self.graph_dir, "memory", "memory.db"))
        first = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertTrue(first["rebuilt"])
        self.assertGreaterEqual(first["knowledge_count"], 1)
        os.remove(os.path.join(self.graph_dir, "knowledge", "brief.json"))
        second = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertTrue(
            second["rebuilt"], _signature_state(self.graph_dir))
        self.assertEqual(second["knowledge_count"], 0)
        self.assertEqual(second["memory_count"], 1)

    def test_deleted_non_newest_file_still_triggers_rebuild(self):
        # When the deleted file was NOT the one with the highest mtime,
        # max_mtime alone cannot detect the deletion. The file-count
        # component of the signature must catch it.
        _make_memory_entry(self.graph_dir, 1, "q1", "a1")
        _settle_sqlite_store(
            os.path.join(self.graph_dir, "memory", "memory.db"))
        # Create knowledge file with an OLDER mtime than memory.db.
        _make_knowledge_md(self.graph_dir, "principles.md",
                           "## section\n\nbody\n")
        _old_mtime = time.time() - 3600
        os.utime(os.path.join(self.graph_dir, "knowledge", "brief.json"),
                 (_old_mtime, _old_mtime))
        first = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertTrue(first["rebuilt"])
        self.assertGreaterEqual(first["knowledge_count"], 1)
        # Delete the non-newest file — max_mtime unchanged, but count drops.
        os.remove(os.path.join(self.graph_dir, "knowledge", "brief.json"))
        second = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertTrue(
            second["rebuilt"], _signature_state(self.graph_dir))
        self.assertEqual(second["knowledge_count"], 0)

    def test_rebuild_without_any_sources_reports_empty(self):
        # No memory/ and no knowledge/ at all: the max-mtime guard (0
        # means "nothing scannable") must keep the rebuild on the full
        # path instead of skipping.
        summary = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertTrue(summary["rebuilt"])
        self.assertEqual(summary["total"], 0)


class TestCodeIdentifierExtraction(unittest.TestCase):
    """Auto-extraction of code identifiers from KB body text."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="kb_id_test_")
        self.graph_dir = os.path.join(self.tmpdir, "code2db-out")
        os.makedirs(self.graph_dir, exist_ok=True)
        from _builder.graph.sqlite_store import SQLiteStore
        store = SQLiteStore(os.path.join(self.graph_dir, "code2database.db"))
        store.connect()
        store.close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_extract_backtick_identifiers(self):
        from _builder.utils import _extract_code_identifiers
        ids = _extract_code_identifiers("use `pthread_mutex_lock` to lock")
        self.assertIn("pthread_mutex_lock", ids)

    def test_extract_function_call_identifiers(self):
        from _builder.utils import _extract_code_identifiers
        ids = _extract_code_identifiers("call free_pid() to release")
        self.assertIn("free_pid", ids)

    def test_extract_member_access_identifiers(self):
        from _builder.utils import _extract_code_identifiers
        ids = _extract_code_identifiers("access task->pid and task.state")
        self.assertIn("task", ids)

    def test_extract_snake_case_identifiers(self):
        from _builder.utils import _extract_code_identifiers
        ids = _extract_code_identifiers("the bdev_register function")
        self.assertIn("bdev_register", ids)

    def test_skip_english_words(self):
        from _builder.utils import _extract_code_identifiers
        ids = _extract_code_identifiers("the quick brown fox jumps")
        # No underscores or mixed case → no extraction
        self.assertEqual(ids, [])

    def test_max_count_limit(self):
        from _builder.utils import _extract_code_identifiers
        text = " ".join(f"`func_{i}`" for i in range(50))
        ids = _extract_code_identifiers(text, max_count=10)
        self.assertLessEqual(len(ids), 10)

    def test_upsert_auto_extracts_identifiers(self):
        """upsert_kb_paragraph auto-extracts identifiers from body text
        and merges them into node_ids."""
        rid = upsert_kb_paragraph(
            self.graph_dir, "knowledge", "test.json",
            "Locking rule", "Use `pthread_mutex_lock` before accessing shared data",
        )
        conn = _kb_connect(self.graph_dir)
        try:
            row = conn.execute(
                "SELECT node_ids FROM kb_paragraphs WHERE id = ?",
                (rid,)).fetchone()
        finally:
            conn.close()
        import json
        node_ids = json.loads(row["node_ids"]) if row["node_ids"] else []
        self.assertIn("pthread_mutex_lock", node_ids)

    def test_upsert_merges_explicit_and_extracted_ids(self):
        """Explicit node_ids are preserved; extracted identifiers are
        appended without duplicates."""
        rid = upsert_kb_paragraph(
            self.graph_dir, "knowledge", "test.json",
            "Test", "Call `free_pid()` and `alloc_resource()`",
            node_ids=["explicit_node"],
        )
        conn = _kb_connect(self.graph_dir)
        try:
            row = conn.execute(
                "SELECT node_ids FROM kb_paragraphs WHERE id = ?",
                (rid,)).fetchone()
        finally:
            conn.close()
        import json
        node_ids = json.loads(row["node_ids"]) if row["node_ids"] else []
        self.assertIn("explicit_node", node_ids)
        self.assertIn("free_pid", node_ids)
        self.assertIn("alloc_resource", node_ids)


class TestKbCluster(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="kb_cluster_test_")
        self.graph_dir = os.path.join(self.tmpdir, "code2db-out")
        os.makedirs(self.graph_dir, exist_ok=True)
        from _builder.graph.sqlite_store import SQLiteStore
        store = SQLiteStore(os.path.join(self.graph_dir, "code2database.db"))
        store.connect()
        store.close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cluster_similar_items(self):
        # Two very similar memory entries should cluster together
        _make_memory_entry(self.graph_dir, 1,
                           "how does bdev register io_device",
                           "bdev_register calls io_device_register",
                           tags=["bdev"])
        _make_memory_entry(self.graph_dir, 2,
                           "how does bdev register io_device",
                           "bdev_register calls io_device_register differently",
                           tags=["bdev"])
        rebuild_kb_index(self.graph_dir, verbose=False)
        summary = cluster_kb(self.graph_dir, threshold=0.1, verbose=False)
        self.assertTrue(summary["clustered"])
        # Two near-duplicate entries must union into one cluster; a
        # singleton-per-item result (cluster_count == item count) means
        # the FTS candidate query failed and clustering was a no-op.
        self.assertEqual(summary["items_clustered"], 2)
        self.assertEqual(summary["cluster_count"], 1)

    def test_cluster_cjk_items(self):
        """Two CJK entries with overlapping content should cluster."""
        _make_memory_entry(self.graph_dir, 1,
                           "如何释放内存",
                           "调用free函数释放分配的内存",
                           tags=[])
        _make_memory_entry(self.graph_dir, 2,
                           "怎样释放内存",
                           "使用free释放已分配的内存块",
                           tags=[])
        rebuild_kb_index(self.graph_dir, verbose=False)
        summary = cluster_kb(self.graph_dir, threshold=0.15, verbose=False)
        self.assertTrue(summary["clustered"])
        self.assertEqual(summary["items_clustered"], 2)
        self.assertEqual(summary["cluster_count"], 1)

    def test_cluster_assigns_canonical_id(self):
        """Each cluster's highest weight x confidence member becomes canonical."""
        _make_memory_entry(self.graph_dir, 1,
                           "how does bdev register io_device",
                           "bdev_register calls io_device_register",
                           tags=["bdev"])
        _make_memory_entry(self.graph_dir, 2,
                           "how does bdev register io_device",
                           "bdev_register calls io_device_register differently",
                           tags=["bdev"])
        rebuild_kb_index(self.graph_dir, verbose=False)
        cluster_kb(self.graph_dir, threshold=0.1, verbose=False)
        conn = _kb_connect(self.graph_dir)
        try:
            rows = conn.execute(
                "SELECT id, scope_id, canonical_id, weight "
                "FROM kb_paragraphs WHERE scope_id IS NOT NULL "
                "ORDER BY weight DESC"
            ).fetchall()
            self.assertGreater(len(rows), 0)
            # All rows in the same cluster must have the same canonical_id
            scope_ids = {r["scope_id"] for r in rows}
            for sid in scope_ids:
                members = [r for r in rows if r["scope_id"] == sid]
                canonicals = {r["canonical_id"] for r in members}
                self.assertEqual(len(canonicals), 1,
                                 f"cluster {sid} has multiple canonical_ids: {canonicals}")
                # The canonical_id must point to an actual member
                member_ids = {r["id"] for r in members}
                self.assertIn(list(canonicals)[0], member_ids)
        finally:
            conn.close()

    def test_cluster_links_principle_ref(self):
        """Memory entries linked to knowledge principles via principle_ref."""
        # Create several memory entries whose text overlaps with a hard_rule.
        # BM25 scoring on a tiny corpus can produce near-zero scores, so
        # we seed enough paragraphs for the FTS5 match to score above 1.0.
        for i in range(5):
            _make_memory_entry(self.graph_dir, i + 1,
                               f"bdev register rule {i}",
                               f"All bdev modules must call bdev_register "
                               f"before use variant {i}",
                               tags=["bdev"])
        # A knowledge brief with a matching hard_rule
        _make_knowledge_md(self.graph_dir, "principles.md",
                           "## bdev registration\n\n"
                           "All bdev modules must call bdev_register before use.\n")
        rebuild_kb_index(self.graph_dir, verbose=False)
        summary = cluster_kb(self.graph_dir, threshold=0.1, verbose=False)
        self.assertTrue(summary["clustered"])
        # At least one principle_ref should be linked when memory text
        # overlaps with a knowledge principle
        self.assertGreater(summary["principle_refs_linked"], 0,
                           f"no principle_refs linked: {summary}")
        conn = _kb_connect(self.graph_dir)
        try:
            rows = conn.execute(
                "SELECT id, kind, principle_ref FROM kb_paragraphs "
                "WHERE kind = 'memory_qa' AND principle_ref IS NOT NULL"
            ).fetchall()
            self.assertGreater(len(rows), 0,
                               "no memory_qa with principle_ref set")
            for r in rows:
                self.assertIsNotNone(r["principle_ref"])
        finally:
            conn.close()

    def test_cluster_empty_kb(self):
        """Empty kb store returns zero clusters."""
        summary = cluster_kb(self.graph_dir, threshold=0.1, verbose=False)
        self.assertTrue(summary["clustered"])
        self.assertEqual(summary["cluster_count"], 0)
        self.assertEqual(summary["items_clustered"], 0)

    def test_cluster_summaries_populated(self):
        """cluster_kb populates kb_cluster_summaries for two-stage retrieval."""
        _make_memory_entry(self.graph_dir, 1,
                           "how does bdev register io_device",
                           "bdev_register calls io_device_register",
                           tags=["bdev"])
        _make_memory_entry(self.graph_dir, 2,
                           "how does bdev register io_device",
                           "bdev_register calls io_device_register differently",
                           tags=["bdev"])
        rebuild_kb_index(self.graph_dir, verbose=False)
        cluster_kb(self.graph_dir, threshold=0.1, verbose=False)
        conn = _kb_connect(self.graph_dir)
        try:
            rows = conn.execute(
                "SELECT cluster_id, summary, token_count "
                "FROM kb_cluster_summaries ORDER BY cluster_id"
            ).fetchall()
            self.assertGreaterEqual(len(rows), 1)
            for r in rows:
                self.assertTrue(r["summary"])
                self.assertGreater(r["token_count"], 0)
        finally:
            conn.close()

    def test_cluster_summaries_cjk_content(self):
        """CJK cluster summaries are generated with CJK content intact."""
        _make_memory_entry(self.graph_dir, 1,
                           "如何释放内存",
                           "调用free函数释放分配的内存",
                           tags=[])
        _make_memory_entry(self.graph_dir, 2,
                           "怎样释放内存",
                           "使用free释放已分配的内存块",
                           tags=[])
        rebuild_kb_index(self.graph_dir, verbose=False)
        cluster_kb(self.graph_dir, threshold=0.15, verbose=False)
        conn = _kb_connect(self.graph_dir)
        try:
            rows = conn.execute(
                "SELECT summary FROM kb_cluster_summaries").fetchall()
            self.assertGreaterEqual(len(rows), 1)
            self.assertIn("释放", rows[0]["summary"])
        finally:
            conn.close()

    def test_cluster_single_paragraph(self):
        """A single paragraph forms its own singleton cluster."""
        _make_memory_entry(self.graph_dir, 1,
                           "unique topic", "unique body content")
        rebuild_kb_index(self.graph_dir, verbose=False)
        summary = cluster_kb(self.graph_dir, threshold=0.3, verbose=False)
        self.assertTrue(summary["clustered"])
        self.assertEqual(summary["cluster_count"], 1)
        self.assertEqual(summary["items_clustered"], 1)
        conn = _kb_connect(self.graph_dir)
        try:
            row = conn.execute(
                "SELECT scope_id, canonical_id FROM kb_paragraphs "
                "WHERE scope_id IS NOT NULL"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["scope_id"], row["canonical_id"])
        finally:
            conn.close()

    def test_cluster_idempotent_on_rerun(self):
        """Re-running cluster_kb recomputes scope_id and canonical_id
        from scratch — the reset UPDATE at the start must clear all
        prior assignments so stale clusters don't leak."""
        _make_memory_entry(self.graph_dir, 1,
                           "how does bdev register",
                           "bdev_register calls io_device_register")
        _make_memory_entry(self.graph_dir, 2,
                           "how does bdev register",
                           "bdev_register calls io_device_register")
        rebuild_kb_index(self.graph_dir, verbose=False)
        # First run
        cluster_kb(self.graph_dir, threshold=0.1, verbose=False)
        conn = _kb_connect(self.graph_dir)
        try:
            first_scope = conn.execute(
                "SELECT DISTINCT scope_id FROM kb_paragraphs "
                "WHERE scope_id IS NOT NULL"
            ).fetchall()
            first_count = len(first_scope)
        finally:
            conn.close()
        # Second run — must produce the same number of clusters
        cluster_kb(self.graph_dir, threshold=0.1, verbose=False)
        conn = _kb_connect(self.graph_dir)
        try:
            second_scope = conn.execute(
                "SELECT DISTINCT scope_id FROM kb_paragraphs "
                "WHERE scope_id IS NOT NULL"
            ).fetchall()
            second_count = len(second_scope)
            # No stale scope_ids from the first run
            stale = conn.execute(
                "SELECT COUNT(*) FROM kb_paragraphs "
                "WHERE scope_id IS NULL AND canonical_id IS NOT NULL"
            ).fetchone()[0]
            self.assertEqual(stale, 0)
        finally:
            conn.close()
        self.assertEqual(first_count, second_count)

    def test_cluster_multiple_distinct_clusters(self):
        """Two groups of similar items form two distinct clusters."""
        # Group 1: bdev registration
        _make_memory_entry(self.graph_dir, 1,
                           "bdev register", "bdev_register io_device_register")
        _make_memory_entry(self.graph_dir, 2,
                           "bdev register", "bdev_register io_device_register")
        # Group 2: thread sync (distinct topic)
        _make_memory_entry(self.graph_dir, 3,
                           "thread sync", "mutex_lock mutex_unlock thread")
        _make_memory_entry(self.graph_dir, 4,
                           "thread sync", "mutex_lock mutex_unlock thread")
        rebuild_kb_index(self.graph_dir, verbose=False)
        summary = cluster_kb(self.graph_dir, threshold=0.3, verbose=False)
        self.assertTrue(summary["clustered"])
        self.assertGreaterEqual(summary["cluster_count"], 2)

    def test_cluster_summary_truncation(self):
        """Cluster summaries are capped at 500 characters."""
        # Create a paragraph with a very long body
        long_body = "bdev_register calls io_device_register " * 50
        _make_memory_entry(self.graph_dir, 1,
                           "bdev register long", long_body)
        _make_memory_entry(self.graph_dir, 2,
                           "bdev register long", long_body)
        rebuild_kb_index(self.graph_dir, verbose=False)
        cluster_kb(self.graph_dir, threshold=0.1, verbose=False)
        conn = _kb_connect(self.graph_dir)
        try:
            row = conn.execute(
                "SELECT summary FROM kb_cluster_summaries"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertLessEqual(len(row["summary"]), 500)
        finally:
            conn.close()

    def test_cluster_summary_no_related_when_singleton(self):
        """A singleton cluster summary has no 'Related:' suffix."""
        _make_memory_entry(self.graph_dir, 1,
                           "unique singleton", "unique singleton body")
        rebuild_kb_index(self.graph_dir, verbose=False)
        cluster_kb(self.graph_dir, threshold=0.5, verbose=False)
        conn = _kb_connect(self.graph_dir)
        try:
            row = conn.execute(
                "SELECT summary FROM kb_cluster_summaries"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertNotIn("Related:", row["summary"])
        finally:
            conn.close()


class TestKbAudit(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="kb_audit_test_")
        self.graph_dir = os.path.join(self.tmpdir, "code2db-out")
        os.makedirs(self.graph_dir, exist_ok=True)
        from _builder.graph.sqlite_store import SQLiteStore
        store = SQLiteStore(os.path.join(self.graph_dir, "code2database.db"))
        store.connect()
        store.close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_audit_empty_kb(self):
        result = audit_kb(self.graph_dir)
        self.assertNotIn("error", result)
        self.assertEqual(result["total_items"], 0)

    def test_audit_with_items(self):
        _make_memory_entry(self.graph_dir, 1, "test q", "test a")
        _make_knowledge_md(self.graph_dir, "principles.md",
                           "## test principle\n\nbody\n")
        rebuild_kb_index(self.graph_dir, verbose=False)
        result = audit_kb(self.graph_dir)
        self.assertGreater(result["total_items"], 0)
        # by_kind should have entries
        self.assertGreater(len(result["by_kind"]), 0)

    def test_audit_with_topic_returns_matches(self):
        _make_memory_entry(self.graph_dir, 1, "bdev question",
                           "bdev answer about registration")
        _make_knowledge_md(self.graph_dir, "principles.md",
                           "## bdev principle\n\nbdev registration rules\n")
        rebuild_kb_index(self.graph_dir, verbose=False)
        result = audit_kb(self.graph_dir, topic="bdev")
        self.assertIn("what_we_know_about_X", result)
        self.assertGreater(len(result["what_we_know_about_X"]), 0)
        for m in result["what_we_know_about_X"]:
            self.assertIn("id", m)
            self.assertIn("source_kind", m)
            self.assertIn("body", m)
            self.assertIn("score", m)
            self.assertLessEqual(len(m["body"]), 300)

    def test_write_audit_log_entry(self):
        # Should not raise even on fresh db
        write_audit_log_entry(self.graph_dir, "test_action", target_id=1,
                              target_kind="kb_paragraph",
                              attribute="body",
                              before_value="old",
                              after_value="new",
                              reason="unit test")


class TestKbStats(unittest.TestCase):
    """kb_stats reports storage, cluster, query-log, and access stats
    that an enterprise maintainer needs to monitor KB health."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="kb_stats_test_")
        self.graph_dir = os.path.join(self.tmpdir, "code2db-out")
        os.makedirs(self.graph_dir, exist_ok=True)
        from _builder.graph.sqlite_store import SQLiteStore
        store = SQLiteStore(
            os.path.join(self.graph_dir, "code2database.db"))
        store.connect()
        store.close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_stats_empty_kb(self):
        result = kb_stats(self.graph_dir)
        self.assertNotIn("error", result)
        self.assertEqual(result["total_items"], 0)
        self.assertEqual(result["by_kind"], {})
        self.assertEqual(result["cluster_count"], 0)

    def test_stats_with_items(self):
        _make_memory_entry(self.graph_dir, 1, "test q", "test a")
        _make_knowledge_md(self.graph_dir, "principles.md",
                           "## test principle\n\nbody\n")
        rebuild_kb_index(self.graph_dir, verbose=False)
        result = kb_stats(self.graph_dir)
        self.assertGreater(result["total_items"], 0)
        self.assertGreater(len(result["by_kind"]), 0)
        self.assertGreater(len(result["by_source_kind"]), 0)

    def test_stats_query_log(self):
        _make_memory_entry(self.graph_dir, 1, "test query", "test answer")
        rebuild_kb_index(self.graph_dir, verbose=False)
        # Run some queries
        query_kb(self.graph_dir, "test", top_n=5)
        query_kb(self.graph_dir, "test", top_n=5)
        query_kb(self.graph_dir, "nonexistent_xyz", top_n=5)
        result = kb_stats(self.graph_dir)
        qlog = result["query_log"]
        self.assertEqual(qlog["total"], 3)
        self.assertEqual(qlog["matched"], 2)
        self.assertEqual(qlog["zero_hit"], 1)
        self.assertGreater(qlog["match_rate"], 0.0)
        self.assertGreater(len(qlog["top_queries"]), 0)
        self.assertGreater(len(qlog["recent_zero_hit"]), 0)

    def test_stats_access_patterns(self):
        _make_memory_entry(self.graph_dir, 1, "test q", "test a")
        rebuild_kb_index(self.graph_dir, verbose=False)
        query_kb(self.graph_dir, "test", top_n=5)
        result = kb_stats(self.graph_dir)
        self.assertGreater(len(result["most_accessed"]), 0)
        self.assertEqual(result["most_accessed"][0]["access_count"], 1)

    def test_stats_version_scope(self):
        upsert_kb_paragraph(self.graph_dir, "memory", "test1.json",
                            "title1", "body1", "[]", "[]", 2.0,
                            "memory_qa", 1.0, 0, "main")
        upsert_kb_paragraph(self.graph_dir, "memory", "test2.json",
                            "title2", "body2", "[]", "[]", 2.0,
                            "memory_qa", 1.0, 0, "release-1.0")
        result = kb_stats(self.graph_dir)
        self.assertIn("main", result["by_version_scope"])
        self.assertIn("release-1.0", result["by_version_scope"])

    def test_stats_ann_index_status(self):
        result = kb_stats(self.graph_dir)
        self.assertIn("ann_index", result)
        self.assertIn("dimension", result["ann_index"])
        self.assertIn("model", result["ann_index"])

    def test_stats_domain_name(self):
        result = kb_stats(self.graph_dir)
        self.assertIn("domain_name", result)

    def test_stats_last_rebuild(self):
        result = kb_stats(self.graph_dir)
        self.assertIn("last_rebuild", result)
        self.assertIn("file_count", result["last_rebuild"])

    def test_stats_access_windows(self):
        # kb_stats reports 7d/30d/90d access windows so a maintainer
        # can spot recently-active vs stale knowledge.
        _make_memory_entry(self.graph_dir, 1, "test q", "test a")
        rebuild_kb_index(self.graph_dir, verbose=False)
        query_kb(self.graph_dir, "test", top_n=5)
        result = kb_stats(self.graph_dir)
        self.assertIn("access_windows", result)
        self.assertIn("7d", result["access_windows"])
        self.assertIn("30d", result["access_windows"])
        self.assertIn("90d", result["access_windows"])
        # The query we just ran should count toward the 7d window.
        self.assertGreater(result["access_windows"]["7d"], 0)

    def test_stats_query_log_windows(self):
        _make_memory_entry(self.graph_dir, 1, "test query", "test answer")
        rebuild_kb_index(self.graph_dir, verbose=False)
        query_kb(self.graph_dir, "test", top_n=5)
        query_kb(self.graph_dir, "nonexistent_xyz", top_n=5)
        result = kb_stats(self.graph_dir)
        self.assertIn("query_log_windows", result)
        win = result["query_log_windows"]["7d"]
        self.assertEqual(win["total"], 2)
        self.assertEqual(win["matched"], 1)
        self.assertEqual(win["zero_hit"], 1)
        self.assertGreater(win["match_rate"], 0.0)

    def test_stats_stale_accessed_count(self):
        # Items accessed but not in 90+ days are "stale" — still
        # relevant enough to have been accessed, but no longer active.
        _make_memory_entry(self.graph_dir, 1, "stale q", "stale a")
        rebuild_kb_index(self.graph_dir, verbose=False)
        query_kb(self.graph_dir, "stale", top_n=5)
        result = kb_stats(self.graph_dir)
        self.assertIn("stale_accessed_count", result)
        # Fresh access — stale count should be 0.
        self.assertEqual(result["stale_accessed_count"], 0)

    def test_stats_reports_schema_version(self):
        # kb_meta carries a schema_version key after _kb_connect;
        # kb_stats surfaces it so enterprise maintainers can verify
        # every store is on the expected schema.
        result = kb_stats(self.graph_dir)
        self.assertIn("schema_version", result)
        self.assertIsInstance(result["schema_version"], int)
        self.assertGreater(result["schema_version"], 0)

    def test_stats_reports_knowledge_schema_version(self):
        # The knowledge store has its own schema_version in
        # knowledge_meta; kb_stats reports it alongside the kb_index
        # version so a maintainer sees both at once.
        from _builder.kb.knowledge_store import KnowledgeStore
        ks = KnowledgeStore(self.graph_dir)
        ks.add("hard_rule", "test rule")
        ks.close()
        result = kb_stats(self.graph_dir)
        self.assertIn("knowledge_schema_version", result)
        self.assertIsInstance(result["knowledge_schema_version"], int)
        self.assertGreater(result["knowledge_schema_version"], 0)

    def test_schema_version_persisted_in_kb_meta(self):
        from _builder.kb.kb_index import KB_SCHEMA_VERSION
        conn = _kb_connect(self.graph_dir)
        try:
            row = conn.execute(
                "SELECT value FROM kb_meta WHERE key = 'schema_version'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(int(row[0]), KB_SCHEMA_VERSION)

    def test_schema_version_never_lowered(self):
        # Pre-set a higher version; _ensure_schema_version must not
        # lower it (the constant tracks the latest migration applied
        # by this codebase, not a ceiling).
        conn = _kb_connect(self.graph_dir)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO kb_meta (key, value) "
                "VALUES ('schema_version', '99')")
            conn.commit()
        finally:
            conn.close()
        # Re-open — the existing version (99) must be preserved.
        conn = _kb_connect(self.graph_dir)
        try:
            row = conn.execute(
                "SELECT value FROM kb_meta WHERE key = 'schema_version'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(int(row[0]), 99)

    def test_knowledge_schema_version_persisted(self):
        from _builder.kb.knowledge_store import (
            KnowledgeStore, KNOWLEDGE_SCHEMA_VERSION, knowledge_db_path)
        ks = KnowledgeStore(self.graph_dir)
        ks.close()
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(knowledge_db_path(self.graph_dir))
        try:
            row = conn.execute(
                "SELECT value FROM knowledge_meta "
                "WHERE key = 'schema_version'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(int(row[0]), KNOWLEDGE_SCHEMA_VERSION)


class TestKbConflict(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="kb_conflict_test_")
        self.graph_dir = os.path.join(self.tmpdir, "code2db-out")
        os.makedirs(self.graph_dir, exist_ok=True)
        from _builder.graph.sqlite_store import SQLiteStore
        store = SQLiteStore(os.path.join(self.graph_dir, "code2database.db"))
        store.connect()
        store.close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_detect_conflicts_with_yes_no(self):
        # Two items in same cluster with "yes" / "no"
        rid1 = upsert_kb_paragraph(self.graph_dir, "memory", "f1.json",
                                    "Is X safe?", "yes it is safe",
                                    kind="memory_qa")
        rid2 = upsert_kb_paragraph(self.graph_dir, "memory", "f2.json",
                                    "Is X safe?", "no it is not safe",
                                    kind="memory_qa")
        # Manually cluster them (same scope_id)
        from _builder.kb.kb_index import _kb_connect
        conn = _kb_connect(self.graph_dir)
        conn.execute("UPDATE kb_paragraphs SET scope_id = 1 WHERE id IN (?, ?)",
                      (rid1, rid2))
        conn.commit()
        conn.close()
        conflicts = detect_conflicts(self.graph_dir)
        self.assertGreater(len(conflicts), 0)
        self.assertEqual(conflicts[0]["contradiction"], ("yes", "no"))

    def test_detect_conflicts_ignores_substring_hits(self):
        # Contradiction pairs match whole words only: 'open' inside
        # 'openfd' is not the antonym of 'close', and the removed
        # single-letter ('y','n') pair used to flag nearly any two
        # English bodies as contradictory.
        rid1 = upsert_kb_paragraph(self.graph_dir, "memory", "f1.json",
                                    "openfd wrapper", "the openfd wrapper "
                                    "must be initialized",
                                    kind="memory_qa")
        rid2 = upsert_kb_paragraph(self.graph_dir, "memory", "f2.json",
                                    "close path", "the close path is "
                                    "separate from any n-of-m retry",
                                    kind="memory_qa")
        from _builder.kb.kb_index import _kb_connect
        conn = _kb_connect(self.graph_dir)
        conn.execute("UPDATE kb_paragraphs SET scope_id = 2 WHERE id IN (?, ?)",
                      (rid1, rid2))
        conn.commit()
        conn.close()
        conflicts = detect_conflicts(self.graph_dir)
        self.assertEqual(len(conflicts), 0)

    def test_detect_conflicts_cjk(self):
        """Chinese contradiction pairs (必须/禁止) are detected."""
        rid1 = upsert_kb_paragraph(self.graph_dir, "memory", "f1.json",
                                    "内存释放规则", "必须调用free释放内存",
                                    kind="memory_qa")
        rid2 = upsert_kb_paragraph(self.graph_dir, "memory", "f2.json",
                                    "内存释放规则", "禁止直接调用free释放",
                                    kind="memory_qa")
        from _builder.kb.kb_index import _kb_connect
        conn = _kb_connect(self.graph_dir)
        conn.execute("UPDATE kb_paragraphs SET scope_id = 3 WHERE id IN (?, ?)",
                      (rid1, rid2))
        conn.commit()
        conn.close()
        conflicts = detect_conflicts(self.graph_dir)
        self.assertGreater(len(conflicts), 0)
        pair = conflicts[0]["contradiction"]
        self.assertIn("必须", pair)
        self.assertIn("禁止", pair)

    def test_forget_immediately_deletes(self):
        rid = upsert_kb_paragraph(self.graph_dir, "memory", "f.json",
                                    "title", "body", kind="memory_qa")
        result = forget_kb_paragraph(self.graph_dir, rid, reason="test")
        self.assertTrue(result["forgotten"])
        self.assertEqual(result["item_id"], rid)
        # Verify gone
        results = query_kb(self.graph_dir, "title", top_n=5)
        self.assertEqual(len(results), 0)

    def test_rollback_default_restores_prior_version(self):
        """rollback_kb_item with no to_version must restore the PRIOR
        version, not the current state. The default used to pick the
        just-appended current snapshot and report success without
        changing anything."""
        from _builder.kb.kb_index import _kb_connect
        import json as _json
        conn = _kb_connect(self.graph_dir)
        # Seed a kb_item with one prior version; current state is "v2".
        conn.execute(
            "INSERT INTO kb_items (kind, title, body, tags, versions_json, "
            "created_at) "
            "VALUES ('memory_qa', 'current title', 'current body', '[]', ?, ?)",
            (_json.dumps([{"title": "old title", "body": "old body",
                           "tags": "[]", "version": 1}]),
             "2026-01-01T00:00:00"))
        conn.commit()
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        result = rollback_kb_item(self.graph_dir, item_id)
        self.assertTrue(result.get("rolled_back"), result)
        # The restored title must be the prior version, not the current.
        conn = _kb_connect(self.graph_dir)
        row = conn.execute("SELECT title, body FROM kb_items WHERE id=?",
                           (item_id,)).fetchone()
        conn.close()
        self.assertEqual(row["title"], "old title")
        self.assertEqual(row["body"], "old body")

    def test_rollback_explicit_to_version(self):
        """rollback_kb_item with explicit to_version restores that version."""
        from _builder.kb.kb_index import _kb_connect
        import json as _json
        conn = _kb_connect(self.graph_dir)
        conn.execute(
            "INSERT INTO kb_items (kind, title, body, tags, versions_json, "
            "created_at) "
            "VALUES ('memory_qa', 'v3 title', 'v3 body', '[]', ?, ?)",
            (_json.dumps([
                {"title": "v1 title", "body": "v1 body",
                 "tags": "[]", "version": 1},
                {"title": "v2 title", "body": "v2 body",
                 "tags": "[]", "version": 2},
            ]),
             "2026-01-01T00:00:00"))
        conn.commit()
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        result = rollback_kb_item(self.graph_dir, item_id, to_version=1)
        self.assertTrue(result.get("rolled_back"), result)
        self.assertEqual(result["restored_to_version"], 1)
        conn = _kb_connect(self.graph_dir)
        row = conn.execute("SELECT title, body FROM kb_items WHERE id=?",
                           (item_id,)).fetchone()
        conn.close()
        self.assertEqual(row["title"], "v1 title")
        self.assertEqual(row["body"], "v1 body")

    def test_rollback_invalid_to_version(self):
        """rollback_kb_item with out-of-range to_version returns error."""
        from _builder.kb.kb_index import _kb_connect
        import json as _json
        conn = _kb_connect(self.graph_dir)
        conn.execute(
            "INSERT INTO kb_items (kind, title, body, tags, versions_json, "
            "created_at) "
            "VALUES ('memory_qa', 'current', 'body', '[]', ?, ?)",
            (_json.dumps([{"title": "v1", "body": "b1", "version": 1}]),
             "2026-01-01T00:00:00"))
        conn.commit()
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        result = rollback_kb_item(self.graph_dir, item_id, to_version=99)
        self.assertIn("error", result)
        self.assertIn("no version", result["error"])

    def test_rollback_nonexistent_item(self):
        """rollback_kb_item on nonexistent id returns error."""
        result = rollback_kb_item(self.graph_dir, 999999)
        self.assertIn("error", result)

    def test_forget_nonexistent_item(self):
        """forget_kb_paragraph on nonexistent id returns error."""
        result = forget_kb_paragraph(self.graph_dir, 999999, "test")
        self.assertIn("error", result)

    def test_detect_conflicts_empty_kb(self):
        """detect_conflicts on an empty KB returns [] (no clusters,
        no contradictions)."""
        conflicts = detect_conflicts(self.graph_dir)
        self.assertEqual(conflicts, [])

    def test_detect_conflicts_singletons_no_conflicts(self):
        """All singletons (no cluster >1) → no contradictions."""
        upsert_kb_paragraph(self.graph_dir, "memory", "f1.json",
                            "title1", "must always lock", kind="memory_qa")
        upsert_kb_paragraph(self.graph_dir, "memory", "f2.json",
                            "title2", "must never lock", kind="memory_qa")
        # No scope_id set → all singletons
        conflicts = detect_conflicts(self.graph_dir)
        self.assertEqual(conflicts, [])

    def test_detect_conflicts_must_must_not(self):
        """English modal pair must/must not is detected."""
        rid1 = upsert_kb_paragraph(self.graph_dir, "memory", "f1.json",
                                    "lock rule", "you must lock first",
                                    kind="memory_qa")
        rid2 = upsert_kb_paragraph(self.graph_dir, "memory", "f2.json",
                                    "lock rule", "you must not lock here",
                                    kind="memory_qa")
        from _builder.kb.kb_index import _kb_connect
        conn = _kb_connect(self.graph_dir)
        conn.execute("UPDATE kb_paragraphs SET scope_id = 10 WHERE id IN (?, ?)",
                      (rid1, rid2))
        conn.commit()
        conn.close()
        conflicts = detect_conflicts(self.graph_dir)
        self.assertGreater(len(conflicts), 0)
        pair = conflicts[0]["contradiction"]
        self.assertIn("must", pair)
        self.assertIn("must not", pair)

    def test_detect_conflicts_true_false(self):
        """English boolean pair true/false is detected."""
        rid1 = upsert_kb_paragraph(self.graph_dir, "memory", "f1.json",
                                    "flag check", "the flag is true",
                                    kind="memory_qa")
        rid2 = upsert_kb_paragraph(self.graph_dir, "memory", "f2.json",
                                    "flag check", "the flag is false",
                                    kind="memory_qa")
        from _builder.kb.kb_index import _kb_connect
        conn = _kb_connect(self.graph_dir)
        conn.execute("UPDATE kb_paragraphs SET scope_id = 11 WHERE id IN (?, ?)",
                      (rid1, rid2))
        conn.commit()
        conn.close()
        conflicts = detect_conflicts(self.graph_dir)
        self.assertGreater(len(conflicts), 0)
        pair = conflicts[0]["contradiction"]
        self.assertIn("true", pair)
        self.assertIn("false", pair)

    def test_detect_conflicts_cjk_thread_safety(self):
        """CJK pair 线程安全/线程不安全 is detected.  Note: the
        安全/不安全 pair may match first since 安全 is a substring of
        线程安全 — both are valid contradictions."""
        rid1 = upsert_kb_paragraph(self.graph_dir, "memory", "f1.json",
                                    "同步规则", "此函数是线程安全的",
                                    kind="memory_qa")
        rid2 = upsert_kb_paragraph(self.graph_dir, "memory", "f2.json",
                                    "同步规则", "此函数是线程不安全的",
                                    kind="memory_qa")
        from _builder.kb.kb_index import _kb_connect
        conn = _kb_connect(self.graph_dir)
        conn.execute("UPDATE kb_paragraphs SET scope_id = 12 WHERE id IN (?, ?)",
                      (rid1, rid2))
        conn.commit()
        conn.close()
        conflicts = detect_conflicts(self.graph_dir)
        self.assertGreater(len(conflicts), 0)
        pair = conflicts[0]["contradiction"]
        # The pair is either (线程安全, 线程不安全) or the shorter
        # (安全, 不安全) — both correctly flag the contradiction.
        self.assertTrue(
            ("安全" in pair[0] and "不安全" in pair[1])
            or ("安全" in pair[1] and "不安全" in pair[0]),
            f"Expected safety contradiction, got {pair}")

    def test_detect_conflicts_cjk_reverse_direction(self):
        """CJK contradiction checked in both directions: w1∈a∧w2∈b
        and w2∈a∧w1∈b.  This test puts 必须 in item B and 禁止 in
        item A (reverse of the existing test)."""
        rid1 = upsert_kb_paragraph(self.graph_dir, "memory", "f1.json",
                                    "规则A", "禁止直接调用free",
                                    kind="memory_qa")
        rid2 = upsert_kb_paragraph(self.graph_dir, "memory", "f2.json",
                                    "规则B", "必须通过包装函数调用",
                                    kind="memory_qa")
        from _builder.kb.kb_index import _kb_connect
        conn = _kb_connect(self.graph_dir)
        conn.execute("UPDATE kb_paragraphs SET scope_id = 13 WHERE id IN (?, ?)",
                      (rid1, rid2))
        conn.commit()
        conn.close()
        conflicts = detect_conflicts(self.graph_dir)
        self.assertGreater(len(conflicts), 0)

    def test_detect_conflicts_similar_but_not_contradictory(self):
        """Two paragraphs with high token overlap but no antonym pair
        must NOT be flagged as contradictory."""
        rid1 = upsert_kb_paragraph(self.graph_dir, "memory", "f1.json",
                                    "memory release",
                                    "use free to release allocated memory",
                                    kind="memory_qa")
        rid2 = upsert_kb_paragraph(self.graph_dir, "memory", "f2.json",
                                    "memory allocation",
                                    "use malloc to allocate memory blocks",
                                    kind="memory_qa")
        from _builder.kb.kb_index import _kb_connect
        conn = _kb_connect(self.graph_dir)
        conn.execute("UPDATE kb_paragraphs SET scope_id = 14 WHERE id IN (?, ?)",
                      (rid1, rid2))
        conn.commit()
        conn.close()
        conflicts = detect_conflicts(self.graph_dir)
        self.assertEqual(len(conflicts), 0)

    def test_detect_conflicts_multiple_clusters(self):
        """Two separate clusters, each with a contradiction, produce
        two conflict entries."""
        # Cluster 1: yes/no
        r1 = upsert_kb_paragraph(self.graph_dir, "memory", "f1.json",
                                  "q1", "yes it is", kind="memory_qa")
        r2 = upsert_kb_paragraph(self.graph_dir, "memory", "f2.json",
                                  "q1", "no it is not", kind="memory_qa")
        # Cluster 2: true/false
        r3 = upsert_kb_paragraph(self.graph_dir, "memory", "f3.json",
                                  "q2", "the flag is true", kind="memory_qa")
        r4 = upsert_kb_paragraph(self.graph_dir, "memory", "f4.json",
                                  "q2", "the flag is false", kind="memory_qa")
        from _builder.kb.kb_index import _kb_connect
        conn = _kb_connect(self.graph_dir)
        conn.execute("UPDATE kb_paragraphs SET scope_id = 20 WHERE id IN (?, ?)",
                      (r1, r2))
        conn.execute("UPDATE kb_paragraphs SET scope_id = 21 WHERE id IN (?, ?)",
                      (r3, r4))
        conn.commit()
        conn.close()
        conflicts = detect_conflicts(self.graph_dir)
        self.assertGreaterEqual(len(conflicts), 2)
        scope_ids = {c["scope_id"] for c in conflicts}
        self.assertGreaterEqual(len(scope_ids), 2)

    def test_forget_returns_metadata(self):
        """forget_kb_paragraph returns source_kind, source_file, title
        of the forgotten paragraph."""
        rid = upsert_kb_paragraph(self.graph_dir, "memory", "meta.json",
                                    "meta title", "meta body",
                                    kind="memory_qa")
        result = forget_kb_paragraph(self.graph_dir, rid, reason="meta test")
        self.assertTrue(result["forgotten"])
        self.assertEqual(result["source_kind"], "memory")
        self.assertEqual(result["source_file"], "meta.json")
        self.assertEqual(result["title"], "meta title")

    def test_forget_default_reason(self):
        """forget_kb_paragraph with empty reason defaults to
        'manual forget' in the audit log entry."""
        rid = upsert_kb_paragraph(self.graph_dir, "memory", "def.json",
                                    "default reason", "body",
                                    kind="memory_qa")
        result = forget_kb_paragraph(self.graph_dir, rid)
        self.assertTrue(result["forgotten"])
        # Verify audit log entry was written
        from _builder.kb.kb_index import _kb_connect
        conn = _kb_connect(self.graph_dir)
        try:
            log = conn.execute(
                "SELECT reason FROM audit_log WHERE target_id = ? "
                "AND command = 'kb-forget'",
                (str(rid),)
            ).fetchone()
            self.assertIsNotNone(log)
        finally:
            conn.close()

    def test_rollback_with_invalid_versions_json(self):
        """rollback_kb_item with corrupt versions_json treats it as
        empty list (JSONDecodeError → [])."""
        from _builder.kb.kb_index import _kb_connect
        conn = _kb_connect(self.graph_dir)
        conn.execute(
            "INSERT INTO kb_items (kind, title, body, tags, versions_json, "
            "created_at) "
            "VALUES ('memory_qa', 'cur', 'body', '[]', ?, ?)",
            ("not valid json{{{", "2026-01-01T00:00:00"))
        conn.commit()
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        # With empty versions and to_version=None, should return error
        result = rollback_kb_item(self.graph_dir, item_id)
        self.assertIn("error", result)

    def test_rollback_empty_versions_to_version_none(self):
        """rollback_kb_item with empty versions and to_version=None
        returns an error (no version to restore)."""
        from _builder.kb.kb_index import _kb_connect
        conn = _kb_connect(self.graph_dir)
        conn.execute(
            "INSERT INTO kb_items (kind, title, body, tags, versions_json, "
            "created_at) "
            "VALUES ('memory_qa', 'cur', 'body', '[]', ?, ?)",
            ("[]", "2026-01-01T00:00:00"))
        conn.commit()
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        result = rollback_kb_item(self.graph_dir, item_id)
        self.assertIn("error", result)


class TestKbGlobal(unittest.TestCase):
    def setUp(self):
        # Override HOME to a tmpdir so global db is isolated
        self.tmpdir = tempfile.mkdtemp(prefix="kb_global_test_")
        self._orig_home = os.environ.get("HOME", "")
        os.environ["HOME"] = self.tmpdir

    def tearDown(self):
        os.environ["HOME"] = self._orig_home
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_global_add_and_search(self):
        from _builder.kb.kb_global import global_add, global_search
        entry_id = global_add(
            title="test principle",
            body="this is a test principle about bdev registration",
            tags=["test", "bdev"],
            kind="principle",
        )
        self.assertGreater(entry_id, 0)
        results = global_search("bdev registration", top_n=10)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0]["title"], "test principle")

    def test_global_search_cjk_fallback(self):
        """Global KB search finds CJK entries via similarity fallback
        when FTS5 tokenization differs between query and stored text."""
        from _builder.kb.kb_global import global_add, global_search
        global_add(
            title="内存释放原则",
            body="当不再需要分配的内存时必须调用free函数释放",
            tags=["内存", "释放"],
            kind="principle",
        )
        # Query with different CJK phrasing — FTS5 AND-match may miss,
        # but the similarity pass should find the entry via shared tokens
        results = global_search("怎样释放内存", top_n=10)
        self.assertGreater(len(results), 0)
        self.assertIn("内存", results[0]["title"])

    def test_global_migration_segments_cjk_body(self):
        """Global KB migration backfills body_tokenized with CJK
        segmentation, not raw body — otherwise CJK entries from before
        the migration stay as single-token FTS5 blobs forever."""
        from _builder.kb.kb_global import _global_kb_db_path, _global_kb_connect
        db_path = _global_kb_db_path()
        # Create old-format db (no body_tokenized column)
        old_conn = sqlite3.connect(db_path)
        old_conn.execute(
            "CREATE TABLE kb_global (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "title TEXT NOT NULL, body TEXT NOT NULL, tags TEXT, "
            "kind TEXT NOT NULL DEFAULT 'principle', weight REAL DEFAULT 1.0, "
            "confidence REAL DEFAULT 1.0, source_project TEXT, source_file TEXT, "
            "created_at TEXT NOT NULL, accessed_at TEXT, access_count INTEGER DEFAULT 0)")
        old_conn.execute(
            "CREATE VIRTUAL TABLE kb_global_fts USING fts5("
            "title, body, tags, content='kb_global', content_rowid='id', "
            "tokenize='porter unicode61')")
        old_conn.execute(
            "CREATE TRIGGER kb_global_ai AFTER INSERT ON kb_global BEGIN "
            "INSERT INTO kb_global_fts(rowid, title, body, tags) "
            "VALUES (new.id, new.title, new.body, COALESCE(new.tags, '')); END")
        old_conn.execute(
            "INSERT INTO kb_global (title, body, tags, kind, created_at) "
            "VALUES ('释放内存', '释放内存释放资源线程安全', '[]', 'principle', '2024-01-01')")
        old_conn.commit()
        old_conn.close()
        # Re-open via _global_kb_connect → triggers migration
        conn = _global_kb_connect()
        try:
            row = conn.execute(
                "SELECT body, body_tokenized FROM kb_global WHERE title = '释放内存'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertNotEqual(row["body_tokenized"], row["body"],
                                "body_tokenized should be segmented, not raw body")
            # Segmented text should have spaces between CJK words
            self.assertIn(" ", row["body_tokenized"])
        finally:
            conn.close()

    def test_global_share_and_import_roundtrip(self):
        from _builder.kb.kb_global import global_add, global_share, global_import, global_search
        global_add(title="share test", body="body to share", kind="principle")
        out_path = os.path.join(self.tmpdir, "export.json")
        global_share(out_path)
        self.assertTrue(os.path.exists(out_path))
        # Verify content
        with open(out_path) as f:
            data = json.load(f)
        self.assertGreater(len(data["entries"]), 0)
        # Reset HOME to a different subdir to simulate fresh global KB
        new_home = os.path.join(self.tmpdir, "home2")
        os.makedirs(new_home, exist_ok=True)
        os.environ["HOME"] = new_home
        imported = global_import(out_path)
        self.assertGreater(imported, 0)
        results = global_search("share", top_n=10)
        self.assertGreater(len(results), 0)

    def test_global_memory_qa_share_and_search(self):
        """Cross-project memory Q&A sharing (kind='memory_qa')."""
        from _builder.kb.kb_global import (
            global_share_memory, global_search_memory,
            global_search,
        )
        from _builder.memory.memory_store import MemoryStore
        import tempfile as _tf

        # Create a fake project with memory entries
        with _tf.TemporaryDirectory(prefix="c2d_mem_test_") as proj_dir:
            graph_dir = os.path.join(proj_dir, "code2db-out")
            os.makedirs(graph_dir, exist_ok=True)
            # Create master.json for project name detection
            with open(os.path.join(graph_dir,
                      "code2database_master.json"), "w") as f:
                json.dump({"project_name": "TestProj",
                           "source_root": "/tmp/testproj"}, f)
            store = MemoryStore(graph_dir)
            store.add("how to handle deadlock in bdev",
                      "use trylock and retry with backoff",
                      author="alice", no_merge=True)
            store.add("how to init NVMe driver",
                      "call spdk_nvme_probe first",
                      author="bob", no_merge=True)

            # Share memories to global KB
            exported = global_share_memory(graph_dir, min_weight=0.5)
            self.assertGreaterEqual(exported, 2)

            # Search global KB for memory Q&A
            results = global_search_memory("deadlock bdev", top_n=10)
            self.assertGreater(len(results), 0)
            self.assertTrue(any("deadlock" in r["title"].lower()
                                for r in results))

            # Verify kind filter: principle entries should NOT appear
            from _builder.kb.kb_global import global_add
            global_add(title="principle entry", body="not memory",
                       kind="principle")
            results2 = global_search_memory("principle", top_n=10)
            # Should NOT find the principle entry
            self.assertFalse(any("principle entry" == r["title"]
                                 for r in results2))

    def test_global_share_rejects_path_traversal(self):
        """global_share must reject relative paths escaping CWD."""
        from _builder.kb.kb_global import global_add, global_share
        global_add(title="traversal test", body="body", kind="principle")
        result = global_share("../../etc/passwd")
        self.assertEqual(result, "")

    def test_global_share_absolute_path_outside_cwd(self):
        """global_share accepts absolute paths outside CWD."""
        from _builder.kb.kb_global import global_add, global_share
        global_add(title="abs path test", body="body", kind="principle")
        out_path = os.path.join(self.tmpdir, "sub", "export.json")
        result = global_share(out_path)
        self.assertNotEqual(result, "")
        self.assertTrue(os.path.exists(result))

    def test_global_import_rejects_oversized_file(self):
        """global_import rejects files > 50MB."""
        from _builder.kb.kb_global import global_import
        big_path = os.path.join(self.tmpdir, "big.json")
        with open(big_path, "wb") as f:
            f.write(b'{"entries":[]}' + b' ' * (51 * 1024 * 1024))
        imported = global_import(big_path)
        self.assertEqual(imported, 0)

    def test_global_import_rejects_nonexistent_file(self):
        """global_import returns 0 for nonexistent file."""
        from _builder.kb.kb_global import global_import
        result = global_import("/nonexistent/path/file.json")
        self.assertEqual(result, 0)

    def test_global_import_rejects_corrupt_json(self):
        """global_import returns 0 for corrupt JSON."""
        from _builder.kb.kb_global import global_import
        bad_path = os.path.join(self.tmpdir, "bad.json")
        with open(bad_path, "w") as f:
            f.write("{not valid json")
        result = global_import(bad_path)
        self.assertEqual(result, 0)

    def test_global_import_skips_oversized_entries(self):
        """global_import skips individual entries > 100KB body."""
        from _builder.kb.kb_global import global_add, global_share, global_import, global_search
        global_add(title="normal", body="small body", kind="principle")
        export_path = os.path.join(self.tmpdir, "export.json")
        global_share(export_path)
        # Manually add a huge entry to the export
        with open(export_path) as f:
            data = json.load(f)
        data["entries"].append({
            "title": "huge", "body": "x" * 200_000,
            "kind": "principle", "tags": [],
        })
        with open(export_path, "w") as f:
            json.dump(data, f)
        # Reset to fresh global KB
        new_home = os.path.join(self.tmpdir, "home3")
        os.makedirs(new_home, exist_ok=True)
        os.environ["HOME"] = new_home
        imported = global_import(export_path)
        # The normal entry should be imported, the huge one skipped
        self.assertGreaterEqual(imported, 1)
        self.assertFalse(any(r["title"] == "huge"
                             for r in global_search("huge", top_n=10)))

    def test_query_with_global_fallback_local_first(self):
        """query_with_global_fallback returns local results when available."""
        from _builder.kb.kb_global import (
            global_add, query_with_global_fallback,
        )
        from _builder.kb.kb_index import upsert_kb_paragraph
        import tempfile as _tf
        with _tf.TemporaryDirectory(prefix="c2d_fallback_") as proj_dir:
            g = os.path.join(proj_dir, "c2d-out")
            os.makedirs(g, exist_ok=True)
            from _builder.graph.sqlite_store import SQLiteStore
            s = SQLiteStore(os.path.join(g, "code2database.db"))
            s.connect(); s.close()
            upsert_kb_paragraph(g, "memory", "test.json",
                                "local result", "local body about bdev")
            # Add a global entry too
            global_add(title="global result", body="global body about bdev",
                       kind="principle")
            results = query_with_global_fallback(g, "bdev", top_n=10)
            self.assertGreater(len(results), 0)
            # Should have local results (not global)
            self.assertTrue(any(r.get("source_kind") == "memory"
                                for r in results))

    def test_query_with_global_fallback_to_global(self):
        """query_with_global_fallback falls back to global KB when local has no matches."""
        from _builder.kb.kb_global import (
            global_add, query_with_global_fallback,
        )
        import tempfile as _tf
        with _tf.TemporaryDirectory(prefix="c2d_fallback_") as proj_dir:
            g = os.path.join(proj_dir, "c2d-out")
            os.makedirs(g, exist_ok=True)
            from _builder.graph.sqlite_store import SQLiteStore
            s = SQLiteStore(os.path.join(g, "code2database.db"))
            s.connect(); s.close()
            # Add only a global entry
            global_add(title="fallback global", body="global knowledge about nvme",
                       kind="principle")
            results = query_with_global_fallback(g, "nvme", top_n=10)
            self.assertGreater(len(results), 0)
            # Should have global results
            self.assertTrue(any("global" in r.get("title", "").lower()
                                or "nvme" in r.get("body", "").lower()
                                for r in results))

    def test_global_import_memory_imports_new(self):
        """global_import_memory imports new Q&A when no similar exists."""
        from _builder.kb.kb_global import (
            global_add, global_import_memory,
        )
        from _builder.memory.memory_store import MemoryStore
        import tempfile as _tf
        with _tf.TemporaryDirectory(prefix="c2d_gim_") as proj_dir:
            g = os.path.join(proj_dir, "c2d-out")
            os.makedirs(g, exist_ok=True)
            from _builder.graph.sqlite_store import SQLiteStore
            s = SQLiteStore(os.path.join(g, "code2database.db"))
            s.connect(); s.close()
            # Add a global memory_qa entry
            global_add(title="how to configure dma",
                       body="call dma_config() with channel id",
                       kind="memory_qa",
                       source_project="other_project",
                       weight=2.0)
            # Import into project
            result = global_import_memory(g, "configure dma", top_n=5)
            self.assertGreater(result["imported"], 0)
            # Verify it was imported
            store = MemoryStore(g)
            entries = store.search("dma")
            self.assertGreater(len(entries), 0)

    def test_global_import_memory_skips_when_no_merge(self):
        """global_import_memory skips similar entries when auto_merge=False."""
        from _builder.kb.kb_global import (
            global_add, global_import_memory,
        )
        from _builder.memory.memory_store import MemoryStore
        import tempfile as _tf
        with _tf.TemporaryDirectory(prefix="c2d_gim_") as proj_dir:
            g = os.path.join(proj_dir, "c2d-out")
            os.makedirs(g, exist_ok=True)
            from _builder.graph.sqlite_store import SQLiteStore
            s = SQLiteStore(os.path.join(g, "code2database.db"))
            s.connect(); s.close()
            # Add a local similar memory entry
            store = MemoryStore(g)
            store.add("how to configure dma controller",
                      "call dma_setup with channel",
                      no_merge=True)
            # Add a global memory_qa entry with very similar question
            global_add(title="how to configure dma controller",
                       body="call dma_config with channel id",
                       kind="memory_qa",
                       source_project="other_project",
                       weight=2.0)
            # Import with auto_merge=False — should skip
            result = global_import_memory(g, "configure dma controller",
                                          top_n=5, auto_merge=False)
            self.assertGreater(result["skipped"], 0)


class TestAnnIndex(unittest.TestCase):
    """build_ann_index and query_ann: vector index build and KNN search."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="kb_ann_test_")
        self.graph_dir = os.path.join(self.tmpdir, "code2db-out")
        os.makedirs(self.graph_dir, exist_ok=True)
        from _builder.graph.sqlite_store import SQLiteStore
        store = SQLiteStore(os.path.join(self.graph_dir, "code2database.db"))
        store.connect()
        store.close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed_paragraphs(self, n=3):
        for i in range(n):
            upsert_kb_paragraph(
                self.graph_dir, "memory", f"test_{i}.json",
                f"test title {i}", f"test body content number {i}")

    def test_try_load_vec_ext_returns_bool(self):
        conn = _kb_connect(self.graph_dir)
        try:
            result = _try_load_vec_ext(conn)
            self.assertIsInstance(result, bool)
        finally:
            conn.close()

    def test_build_ann_no_kb_store(self):
        # _kb_connect returns None when create_if_missing=False
        # and the db doesn't exist
        result = build_ann_index("/nonexistent/path/that/does/not/exist")
        self.assertFalse(result["built"])
        self.assertEqual(result.get("reason"), "no kb store")

    @requires_sqlite_vec
    def test_build_ann_no_provider(self):
        self._seed_paragraphs(2)
        import _builder.kb.neural_embed as ne
        ne._PROVIDER_CACHE = None
        old = os.environ.pop("OPENAI_API_KEY", None)
        old_base = os.environ.pop("OPENAI_BASE_URL", None)
        old_ollama = os.environ.pop("C2D_OLLAMA_URL", None)
        try:
            result = build_ann_index(self.graph_dir, provider="auto")
            self.assertFalse(result["built"])
            self.assertEqual(result["reason"], "no embedding provider")
        finally:
            if old:
                os.environ["OPENAI_API_KEY"] = old
            if old_base:
                os.environ["OPENAI_BASE_URL"] = old_base
            if old_ollama:
                os.environ["C2D_OLLAMA_URL"] = old_ollama
            ne._PROVIDER_CACHE = None

    @requires_sqlite_vec
    def test_build_ann_with_mocked_embeddings(self):
        self._seed_paragraphs(3)
        import _builder.kb.neural_embed as ne
        import _builder.kb.kb_index as ki

        def fake_get_embedding(text):
            return [0.1, 0.2, 0.3, 0.4]

        def fake_get_batch(texts):
            return [[0.1 + i * 0.01, 0.2, 0.3, 0.4] for i in range(len(texts))]

        def fake_detect():
            return "mock"

        orig_ge = ne.get_embedding
        orig_geb = ne.get_embedding_batch
        orig_dp = ne._detect_provider
        orig_vec = ki._VEC_EXT_LOADED
        ne.get_embedding = fake_get_embedding
        ne.get_embedding_batch = fake_get_batch
        ne._detect_provider = fake_detect
        ne._PROVIDER_CACHE = "mock"
        ki._VEC_EXT_LOADED = None
        try:
            result = build_ann_index(self.graph_dir, provider="mock")
            self.assertTrue(result["built"], f"build failed: {result}")
            self.assertEqual(result["embedded"], 3)
            self.assertEqual(result["dim"], 4)
            self.assertEqual(result["provider"], "mock")
        finally:
            ne.get_embedding = orig_ge
            ne.get_embedding_batch = orig_geb
            ne._detect_provider = orig_dp
            ne._PROVIDER_CACHE = None
            ki._VEC_EXT_LOADED = orig_vec

    @requires_sqlite_vec
    def test_build_ann_idempotent_skip(self):
        self._seed_paragraphs(2)
        import _builder.kb.neural_embed as ne
        import _builder.kb.kb_index as ki

        orig_ge = ne.get_embedding
        orig_geb = ne.get_embedding_batch
        orig_dp = ne._detect_provider

        def fake_get_batch(texts):
            return [[0.1, 0.2, 0.3, 0.4] for _ in range(len(texts))]

        def fake_detect():
            return "mock"

        ne.get_embedding_batch = fake_get_batch
        ne.get_embedding = lambda t: [0.1, 0.2, 0.3, 0.4]
        ne._detect_provider = fake_detect
        ne._PROVIDER_CACHE = "mock"
        ki._VEC_EXT_LOADED = None
        try:
            r1 = build_ann_index(self.graph_dir, provider="mock")
            self.assertTrue(r1["built"])
            r2 = build_ann_index(self.graph_dir, provider="mock")
            self.assertTrue(r2["built"])
            self.assertEqual(r2["reason"], "all rows already embedded")
            self.assertEqual(r2["embedded"], 0)
        finally:
            ne.get_embedding = orig_ge
            ne.get_embedding_batch = orig_geb
            ne._detect_provider = orig_dp
            ne._PROVIDER_CACHE = None
            ki._VEC_EXT_LOADED = None

    @requires_sqlite_vec
    def test_query_ann_returns_results(self):
        self._seed_paragraphs(3)
        import _builder.kb.neural_embed as ne
        import _builder.kb.kb_index as ki

        orig_ge = ne.get_embedding
        orig_geb = ne.get_embedding_batch
        orig_dp = ne._detect_provider

        def fake_get_batch(texts):
            return [[float(i), 0.0, 0.0, 0.0] for i in range(len(texts))]

        def fake_detect():
            return "mock"

        ne.get_embedding_batch = fake_get_batch
        ne.get_embedding = lambda t: [0.0, 0.0, 0.0, 0.0]
        ne._detect_provider = fake_detect
        ne._PROVIDER_CACHE = "mock"
        ki._VEC_EXT_LOADED = None
        try:
            build_ann_index(self.graph_dir, provider="mock")
            conn = _kb_connect(self.graph_dir)
            try:
                _try_load_vec_ext(conn)
                results = query_ann(conn, [0.0, 0.0, 0.0, 0.0], top_n=5)
                self.assertGreater(len(results), 0)
                for r in results:
                    self.assertIn("id", r)
                    self.assertIn("score", r)
                    self.assertIn("body", r)
            finally:
                conn.close()
        finally:
            ne.get_embedding = orig_ge
            ne.get_embedding_batch = orig_geb
            ne._detect_provider = orig_dp
            ne._PROVIDER_CACHE = None
            ki._VEC_EXT_LOADED = None

    def test_query_ann_no_vec_table_returns_empty(self):
        self._seed_paragraphs(1)
        conn = _kb_connect(self.graph_dir)
        try:
            results = query_ann(conn, [0.1, 0.2], top_n=5)
            self.assertEqual(len(results), 0)
        finally:
            conn.close()

    @requires_sqlite_vec
    def test_build_ann_incremental_inserts_only_new_rows(self):
        """Second build_ann_index call with new paragraphs only inserts
        the new rows into kb_vec — no full rebuild."""
        self._seed_paragraphs(3)
        import _builder.kb.neural_embed as ne
        import _builder.kb.kb_index as ki

        orig_ge = ne.get_embedding
        orig_geb = ne.get_embedding_batch
        orig_dp = ne._detect_provider

        def fake_get_batch(texts):
            return [[float(i), 0.0, 0.0, 0.0] for i in range(len(texts))]

        def fake_detect():
            return "mock"

        ne.get_embedding_batch = fake_get_batch
        ne.get_embedding = lambda t: [0.0, 0.0, 0.0, 0.0]
        ne._detect_provider = fake_detect
        ne._PROVIDER_CACHE = "mock"
        ki._VEC_EXT_LOADED = None
        try:
            # First build: full rebuild
            r1 = build_ann_index(self.graph_dir, provider="mock")
            self.assertTrue(r1["built"])
            self.assertFalse(r1.get("incremental", False))
            self.assertEqual(r1["embedded"], 3)
            self.assertEqual(r1["total_indexed"], 3)
            # Add 2 more paragraphs
            upsert_kb_paragraph(
                self.graph_dir, "memory", "test_3.json",
                "test title 3", "test body content number 3")
            upsert_kb_paragraph(
                self.graph_dir, "memory", "test_4.json",
                "test title 4", "test body content number 4")
            # Second build: incremental — only 2 new rows
            r2 = build_ann_index(self.graph_dir, provider="mock")
            self.assertTrue(r2["built"])
            self.assertTrue(r2.get("incremental", False),
                            f"expected incremental=True, got {r2}")
            self.assertEqual(r2["embedded"], 2)
            self.assertEqual(r2["total_indexed"], 5)
            # Verify all 5 rows are in kb_vec
            conn = _kb_connect(self.graph_dir)
            try:
                _try_load_vec_ext(conn)
                count = conn.execute(
                    "SELECT COUNT(*) FROM kb_vec").fetchone()[0]
                self.assertEqual(count, 5)
            finally:
                conn.close()
        finally:
            ne.get_embedding = orig_ge
            ne.get_embedding_batch = orig_geb
            ne._detect_provider = orig_dp
            ne._PROVIDER_CACHE = None
            ki._VEC_EXT_LOADED = None


class TestMmrRerank(unittest.TestCase):
    """Maximal Marginal Relevance diversifies result lists by
    penalising near-duplicate paragraphs."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="kb_mmr_test_")
        self.graph_dir = os.path.join(self.tmpdir, "code2db-out")
        os.makedirs(self.graph_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _insert(self, title, body, weight=2.0):
        upsert_kb_paragraph(self.graph_dir, "memory", f"{title}.json",
                            title, body, "[]", "[]", weight,
                            "memory_qa", 1.0, 0, "")

    def test_mmr_rerank_preserves_score(self):
        from _builder.utils import _mmr_rerank
        results = [
            {"title": "a", "body": "free memory release", "score": 1.0},
            {"title": "b", "body": "free memory release", "score": 0.9},
            {"title": "c", "body": "thread sync mutex", "score": 0.8},
        ]
        reranked = _mmr_rerank(results, "memory", lambda_param=0.7, top_n=3)
        # Scores must be unchanged
        scores = {r["title"]: r["score"] for r in reranked}
        self.assertEqual(scores["a"], 1.0)
        self.assertEqual(scores["b"], 0.9)
        self.assertEqual(scores["c"], 0.8)

    def test_mmr_rerank_deprioritises_duplicates(self):
        from _builder.utils import _mmr_rerank
        # Two near-duplicate paragraphs (a, b) and one distinct (c)
        results = [
            {"title": "a", "body": "free memory release allocated", "score": 1.0},
            {"title": "b", "body": "release memory free allocated", "score": 0.9},
            {"title": "c", "body": "thread sync mutex lock", "score": 0.8},
        ]
        reranked = _mmr_rerank(results, "memory", lambda_param=0.7, top_n=3)
        # The distinct result c should rank above the duplicate b
        titles = [r["title"] for r in reranked]
        self.assertEqual(titles[0], "a")  # highest relevance
        self.assertLess(titles.index("c"), titles.index("b"),
                        "Distinct result c should rank above near-duplicate b")

    def test_mmr_rerank_lambda_1_is_pure_relevance(self):
        from _builder.utils import _mmr_rerank
        results = [
            {"title": "a", "body": "free memory", "score": 1.0},
            {"title": "b", "body": "free memory", "score": 0.9},
            {"title": "c", "body": "thread sync", "score": 0.8},
        ]
        reranked = _mmr_rerank(results, "memory", lambda_param=1.0, top_n=3)
        # With λ=1.0, MMR = pure relevance → original order preserved
        titles = [r["title"] for r in reranked]
        self.assertEqual(titles, ["a", "b", "c"])

    def test_mmr_rerank_empty(self):
        from _builder.utils import _mmr_rerank
        self.assertEqual(_mmr_rerank([], "query"), [])

    def test_mmr_rerank_single(self):
        from _builder.utils import _mmr_rerank
        results = [{"title": "a", "body": "test", "score": 1.0}]
        reranked = _mmr_rerank(results, "query", top_n=1)
        self.assertEqual(len(reranked), 1)
        self.assertEqual(reranked[0]["title"], "a")

    def test_mmr_rerank_top_n_limit(self):
        from _builder.utils import _mmr_rerank
        results = [
            {"title": "a", "body": "free memory", "score": 1.0},
            {"title": "b", "body": "thread sync", "score": 0.9},
            {"title": "c", "body": "file io", "score": 0.8},
        ]
        reranked = _mmr_rerank(results, "query", top_n=2)
        self.assertEqual(len(reranked), 2)

    def test_mmr_rerank_cleans_temp_fields(self):
        from _builder.utils import _mmr_rerank
        results = [
            {"title": "a", "body": "free memory", "score": 1.0},
            {"title": "b", "body": "thread sync", "score": 0.9},
        ]
        reranked = _mmr_rerank(results, "query", top_n=2)
        for r in reranked:
            self.assertNotIn("_rel", r)
            self.assertNotIn("_toks", r)

    def test_query_kb_diversify_changes_order(self):
        # Insert near-duplicate paragraphs and a distinct one, all
        # matching the query terms but with different additional
        # content.
        self._insert("mem-free", "free memory release allocated blocks")
        self._insert("mem-release", "release memory free allocated blocks")
        self._insert("mem-sync", "free memory sync mutex thread safety")
        plain = query_kb(self.graph_dir, "memory free", top_n=3,
                         diversify=False)
        diverse = query_kb(self.graph_dir, "memory free", top_n=3,
                           diversify=True)
        # Both should return 3 results
        self.assertEqual(len(plain), 3)
        self.assertEqual(len(diverse), 3)
        # The diversified list should not be identical to the plain list
        plain_titles = [r["title"] for r in plain]
        diverse_titles = [r["title"] for r in diverse]
        # mem-sync (distinct from the duplicate pair) should rank
        # at least as high in the diversified list
        if "mem-sync" in plain_titles and "mem-sync" in diverse_titles:
            self.assertLessEqual(
                diverse_titles.index("mem-sync"),
                plain_titles.index("mem-sync"),
                "Diversified list should rank mem-sync at least as high")

    def test_query_kb_diversify_default_off(self):
        # By default, diversify=False → pure relevance ordering
        self._insert("a", "free memory release")
        self._insert("b", "free memory release")
        results = query_kb(self.graph_dir, "memory", top_n=2)
        self.assertEqual(len(results), 2)
        # No temp fields should leak
        for r in results:
            self.assertNotIn("_rel", r)
            self.assertNotIn("_toks", r)


class TestKbBackupRestore(unittest.TestCase):
    """kb-backup and kb-restore: single-file tar.gz archive of every
    KB store file with a manifest carrying schema versions and sha256
    hashes."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="kb_backup_test_")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.graph_dir = os.path.join(self.tmpdir, "code2db-out")
        os.makedirs(self.graph_dir, exist_ok=True)

    def _populate(self):
        """Create a minimal KB with one memory entry and rebuild."""
        _make_memory_entry(self.graph_dir, 1, "backup test q", "backup test a")
        rebuild_kb_index(self.graph_dir, verbose=False)

    def test_backup_creates_archive(self):
        from _builder.kb.kb_backup import kb_backup
        self._populate()
        out = os.path.join(self.tmpdir, "backup.tar.gz")
        path = kb_backup(self.graph_dir, out)
        self.assertTrue(os.path.isfile(path))
        # The archive must be a valid tar.gz
        import tarfile
        with tarfile.open(path, "r:gz") as tar:
            names = tar.getnames()
        self.assertIn("manifest.json", names)
        self.assertIn("kb_index.db", names)

    def test_backup_manifest_has_schema_versions(self):
        from _builder.kb.kb_backup import kb_backup
        self._populate()
        out = os.path.join(self.tmpdir, "backup.tar.gz")
        kb_backup(self.graph_dir, out)
        import tarfile, json
        with tarfile.open(out, "r:gz") as tar:
            manifest = json.loads(
                tar.extractfile("manifest.json").read().decode("utf-8"))
        self.assertEqual(manifest["format"], "code2database-kb-backup")
        self.assertIn("schema_versions", manifest)
        self.assertIn("kb_index", manifest["schema_versions"])
        self.assertIn("files", manifest)
        self.assertGreater(len(manifest["files"]), 0)

    def test_restore_round_trip(self):
        from _builder.kb.kb_backup import kb_backup, kb_restore
        self._populate()
        out = os.path.join(self.tmpdir, "backup.tar.gz")
        kb_backup(self.graph_dir, out)
        # Restore to a new directory
        new_dir = os.path.join(self.tmpdir, "restored")
        result = kb_restore(new_dir, out)
        self.assertGreater(result["restored_count"], 0)
        self.assertEqual(result["skipped_count"], 0)
        # The restored KB should be queryable
        hits = query_kb(new_dir, "backup test")
        self.assertTrue(any("backup" in h["body"] for h in hits))

    def test_restore_skips_existing_without_force(self):
        from _builder.kb.kb_backup import kb_backup, kb_restore
        self._populate()
        out = os.path.join(self.tmpdir, "backup.tar.gz")
        kb_backup(self.graph_dir, out)
        # Restore to the same directory — existing files should be skipped
        result = kb_restore(self.graph_dir, out, force=False)
        self.assertGreater(result["skipped_count"], 0)

    def test_restore_force_overwrites(self):
        from _builder.kb.kb_backup import kb_backup, kb_restore
        self._populate()
        out = os.path.join(self.tmpdir, "backup.tar.gz")
        kb_backup(self.graph_dir, out)
        result = kb_restore(self.graph_dir, out, force=True)
        self.assertEqual(result["skipped_count"], 0)
        self.assertGreater(result["restored_count"], 0)

    def test_backup_missing_dir_raises(self):
        from _builder.kb.kb_backup import kb_backup
        with self.assertRaises(FileNotFoundError):
            kb_backup("/nonexistent/path", "/tmp/out.tar.gz")

    def test_backup_empty_dir_raises(self):
        from _builder.kb.kb_backup import kb_backup
        empty = os.path.join(self.tmpdir, "empty")
        os.makedirs(empty)
        with self.assertRaises(FileNotFoundError):
            kb_backup(empty, "/tmp/out.tar.gz")

    def test_restore_missing_archive_raises(self):
        from _builder.kb.kb_backup import kb_restore
        with self.assertRaises(FileNotFoundError):
            kb_restore(self.graph_dir, "/nonexistent/archive.tar.gz")

    def test_restore_rejects_path_traversal(self):
        """kb_restore must skip archive members that escape graph_dir."""
        import tarfile
        from _builder.kb.kb_backup import kb_restore
        # Craft a malicious archive with a path-traversal member.
        evil_path = os.path.join(self.tmpdir, "evil.tar.gz")
        with tarfile.open(evil_path, "w:gz") as tar:
            # A safe member so the archive is not empty.
            info = tarfile.TarInfo("memory/memory.db")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"\x00"))
            # A traversal member.
            evil = tarfile.TarInfo("../../../evil_traversal_test.txt")
            evil.size = 4
            tar.addfile(evil, io.BytesIO(b"evil"))
        result = kb_restore(self.graph_dir, evil_path)
        # The traversal file must NOT exist outside graph_dir.
        self.assertFalse(os.path.exists(
            os.path.join(self.tmpdir, "evil_traversal_test.txt")))
        self.assertFalse(os.path.exists(
            os.path.join(self.tmpdir, "..", "evil_traversal_test.txt")))

    def test_restore_archive_without_manifest(self):
        """kb_restore works when the archive has no manifest.json."""
        import tarfile
        from _builder.kb.kb_backup import kb_restore
        archive = os.path.join(self.tmpdir, "nomanifest.tar.gz")
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("memory/memory.db")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"\x00"))
        target = os.path.join(self.tmpdir, "restored_nom")
        result = kb_restore(target, archive)
        self.assertIsNone(result["manifest"])
        self.assertEqual(result["schema_versions"], {})

    @requires_sqlite_vec
    def test_ann_dimension_mismatch_triggers_full_rebuild(self):
        """build_ann_index with a stale ann_vec_dim falls through to
        full rebuild instead of incremental insert."""
        # Seed paragraphs
        for i in range(2):
            upsert_kb_paragraph(self.graph_dir, "memory", f"t_{i}.json",
                                f"title {i}", f"body {i}")
        import _builder.kb.neural_embed as ne
        import _builder.kb.kb_index as ki
        def fake_get_batch(texts):
            return [[0.1, 0.2, 0.3, 0.4] for _ in range(len(texts))]
        def fake_detect():
            return "mock"
        orig_ge = ne.get_embedding
        orig_geb = ne.get_embedding_batch
        orig_dp = ne._detect_provider
        ne.get_embedding_batch = fake_get_batch
        ne.get_embedding = lambda t: [0.1, 0.2, 0.3, 0.4]
        ne._detect_provider = fake_detect
        ne._PROVIDER_CACHE = "mock"
        ki._VEC_EXT_LOADED = None
        try:
            # First build
            r1 = build_ann_index(self.graph_dir, provider="mock")
            self.assertTrue(r1["built"])
            # Add a new paragraph so the next build has work to do
            upsert_kb_paragraph(self.graph_dir, "memory", "t_new.json",
                                "new title", "new body")
            # Stale dim in meta → full rebuild (not incremental)
            conn = _kb_connect(self.graph_dir)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO kb_meta (key, value) "
                    "VALUES ('ann_vec_dim', '999')")
                conn.commit()
            finally:
                conn.close()
            ki._VEC_EXT_LOADED = None
            r2 = build_ann_index(self.graph_dir, provider="mock")
            self.assertTrue(r2["built"])
            self.assertFalse(r2.get("incremental", True),
                             f"expected full rebuild, got {r2}")
        finally:
            ne.get_embedding = orig_ge
            ne.get_embedding_batch = orig_geb
            ne._detect_provider = orig_dp
            ne._PROVIDER_CACHE = None
            ki._VEC_EXT_LOADED = None


if __name__ == "__main__":
    unittest.main()
