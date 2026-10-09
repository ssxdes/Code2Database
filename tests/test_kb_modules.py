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
)
from _builder.kb.kb_cluster import cluster_kb
from _builder.kb.kb_audit import audit_kb, write_audit_log_entry
from _builder.kb.kb_conflict import detect_conflicts, forget_kb_paragraph, rollback_kb_item


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
    """Diagnostic context for skip assertions: the stored marker plus
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

    def test_rebuild_without_any_sources_reports_empty(self):
        # No memory/ and no knowledge/ at all: the max-mtime guard (0
        # means "nothing scannable") must keep the rebuild on the full
        # path instead of skipping.
        summary = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertTrue(summary["rebuilt"])
        self.assertEqual(summary["total"], 0)


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

    def test_write_audit_log_entry(self):
        # Should not raise even on fresh db
        write_audit_log_entry(self.graph_dir, "test_action", target_id=1,
                              target_kind="kb_paragraph",
                              attribute="body",
                              before_value="old",
                              after_value="new",
                              reason="unit test")


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


if __name__ == "__main__":
    unittest.main()
