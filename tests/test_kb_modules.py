"""Unit tests for kb_index, kb_cluster, kb_global, kb_audit, kb_conflict.

Phase 1-11 unified knowledge base modules.

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
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.kb_index import (
    rebuild_kb_index,
    query_kb,
    upsert_kb_paragraph,
    delete_kb_paragraphs_by_source,
    get_known_unknowns,
    _fts5_escape,
    _split_markdown_paragraphs,
)
from _builder.kb_cluster import cluster_kb
from _builder.kb_audit import audit_kb, write_audit_log_entry
from _builder.kb_conflict import detect_conflicts, forget_kb_paragraph, rollback_kb_item


def _make_memory_entry(graph_dir, entry_id, question, answer, tags=None,
                       status="trusted", subdir="root"):
    """Add a memory entry to graph_dir/memory/memory.db (SQLite store).

    entry_id is informational — the store assigns ids sequentially
    (fresh dirs match the requested ids). status maps to the store's
    lifecycle ('trusted' → active).
    """
    from _builder.memory_store import MemoryStore
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
        from _builder.sqlite_store import SQLiteStore
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

    def test_query_no_match_returns_empty(self):
        _make_memory_entry(self.graph_dir, 1,
                           "unrelated question", "unrelated answer")
        rebuild_kb_index(self.graph_dir, verbose=False)
        results = query_kb(self.graph_dir, "completely_unrelated_topic_xyzzy", top_n=10)
        self.assertEqual(len(results), 0)

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


class TestKbCluster(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="kb_cluster_test_")
        self.graph_dir = os.path.join(self.tmpdir, "code2db-out")
        os.makedirs(self.graph_dir, exist_ok=True)
        from _builder.sqlite_store import SQLiteStore
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
        self.assertGreater(summary["cluster_count"], 0)


class TestKbAudit(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="kb_audit_test_")
        self.graph_dir = os.path.join(self.tmpdir, "code2db-out")
        os.makedirs(self.graph_dir, exist_ok=True)
        from _builder.sqlite_store import SQLiteStore
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
        from _builder.sqlite_store import SQLiteStore
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
        from _builder.kb_index import _kb_connect
        conn = _kb_connect(self.graph_dir)
        conn.execute("UPDATE kb_paragraphs SET scope_id = 1 WHERE id IN (?, ?)",
                      (rid1, rid2))
        conn.commit()
        conn.close()
        conflicts = detect_conflicts(self.graph_dir)
        self.assertGreater(len(conflicts), 0)
        self.assertEqual(conflicts[0]["contradiction"], ("yes", "no"))

    def test_forget_immediately_deletes(self):
        rid = upsert_kb_paragraph(self.graph_dir, "memory", "f.json",
                                    "title", "body", kind="memory_qa")
        result = forget_kb_paragraph(self.graph_dir, rid, reason="test")
        self.assertTrue(result["forgotten"])
        self.assertEqual(result["item_id"], rid)
        # Verify gone
        results = query_kb(self.graph_dir, "title", top_n=5)
        self.assertEqual(len(results), 0)


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
        from _builder.kb_global import global_add, global_search
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

    def test_global_share_and_import_roundtrip(self):
        from _builder.kb_global import global_add, global_share, global_import, global_search
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


if __name__ == "__main__":
    unittest.main()
