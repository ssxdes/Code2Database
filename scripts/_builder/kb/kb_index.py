"""Knowledge-base index builder and FTS5 query interface.

Builds a derived SQLite index
(kb_paragraphs + kb_paragraphs_fts) from the canonical filesystem
sources (memory/memory.db entries and knowledge/brief.json sections)
so a single FTS5 + BM25 query can search across both stores.

The index lives in its own store file, kb_index.db, next to the
memory/ and knowledge/ directories — deliberately separate from the
graph's code2database.db so the knowledge base works with or without
a built graph and never creates graph artifacts as a side effect.

The filesystem files remain the source of truth — kb_paragraphs is
rebuildable via `kb-rebuild-index`. Writes to memory/knowledge should
call upsert_kb_paragraph() to keep the index in sync.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from _builder.scanner_bridge.c2d_foreign import _escape_sql_path
from _builder.utils import (_simple_tokenize, _similarity_score, _has_cjk,
                           _cjk_pre_tokenize)
from datetime import datetime
from typing import Optional, List, Dict, Any
import logging


def _kb_db_path(graph_dir: str) -> str:
    """Path of the knowledge base's own index store (kb_index.db)."""
    return os.path.join(graph_dir, "kb_index.db")


def _legacy_kb_db_path(graph_dir: str) -> str:
    """Path of the pre-isolation kb index (inside the graph db).

    Older releases kept the kb_* tables inside code2database.db; guards
    and the one-time copy still recognize that layout.
    """
    return os.path.join(graph_dir, "code2database.db")


def _kb_connect(graph_dir: str, create_if_missing: bool = True) -> Optional[sqlite3.Connection]:
    """Open a connection to the knowledge base index store (kb_index.db).

    By default, creates the store file (and kb_* tables) if missing —
    the kb index is the knowledge base's own artifact, so creating it
    never touches or creates the graph's code2database.db. This lets
    `kb-rebuild-index` and friends work on a store dir that has no
    graph at all (knowledge/memory only). Pass `create_if_missing=False`
    to instead return None when the store doesn't exist yet.
    """
    db_path = _kb_db_path(graph_dir)
    if not os.path.exists(db_path):
        if not create_if_missing:
            return None
        # else: create the db by connecting (SQLite creates the file)
    # uri=True so that subsequent ATTACH DATABASE 'file:...?mode=ro'
    # statements work — SQLite requires the main connection to be opened
    # with uri=True for ATTACH to honor file: URIs. Plain paths still
    # work as filenames (only strings starting with 'file:' are treated
    # as URIs).
    conn = sqlite3.connect(db_path, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")  # 5s retry on locked db
    # Ensure kb_paragraphs + kb_items + kb_query_log tables exist
    # (idempotent — mirrors sqlite_store.py schema v9-v12).
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS kb_paragraphs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_kind TEXT NOT NULL,
                source_file TEXT NOT NULL,
                para_index INTEGER NOT NULL,
                title TEXT,
                body TEXT NOT NULL,
                body_tokenized TEXT,
                tags TEXT,
                node_ids TEXT,
                weight REAL NOT NULL DEFAULT 1.0,
                confidence REAL NOT NULL DEFAULT 1.0,
                kind TEXT NOT NULL,
                graph_version TEXT,
                created_at TEXT NOT NULL,
                accessed_at TEXT,
                access_count INTEGER DEFAULT 0,
                scope_id INTEGER,
                canonical_id INTEGER,
                principle_ref INTEGER,
                embedding BLOB,
                version_scope TEXT NOT NULL DEFAULT 'default'
            );
            CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_kind
                ON kb_paragraphs(kind);
            CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_version_scope
                ON kb_paragraphs(version_scope);
            CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_source
                ON kb_paragraphs(source_kind, source_file);
            CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_weight
                ON kb_paragraphs(weight DESC);
            CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_scope
                ON kb_paragraphs(scope_id) WHERE scope_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_canonical
                ON kb_paragraphs(canonical_id) WHERE canonical_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_principle_ref
                ON kb_paragraphs(principle_ref) WHERE principle_ref IS NOT NULL;
            CREATE VIRTUAL TABLE IF NOT EXISTS kb_paragraphs_fts USING fts5(
                title, body_tokenized, tags,
                content='kb_paragraphs', content_rowid='id',
                tokenize='porter unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS kb_paragraphs_ai AFTER INSERT ON kb_paragraphs BEGIN
                INSERT INTO kb_paragraphs_fts(rowid, title, body_tokenized, tags)
                VALUES (new.id, new.title, COALESCE(new.body_tokenized, new.body), COALESCE(new.tags, ''));
            END;
            CREATE TRIGGER IF NOT EXISTS kb_paragraphs_ad AFTER DELETE ON kb_paragraphs BEGIN
                INSERT INTO kb_paragraphs_fts(kb_paragraphs_fts, rowid, title, body_tokenized, tags)
                VALUES ('delete', old.id, old.title, COALESCE(old.body_tokenized, old.body), COALESCE(old.tags, ''));
            END;
            CREATE TRIGGER IF NOT EXISTS kb_paragraphs_au AFTER UPDATE ON kb_paragraphs BEGIN
                INSERT INTO kb_paragraphs_fts(kb_paragraphs_fts, rowid, title, body_tokenized, tags)
                VALUES ('delete', old.id, old.title, COALESCE(old.body_tokenized, old.body), COALESCE(old.tags, ''));
                INSERT INTO kb_paragraphs_fts(rowid, title, body_tokenized, tags)
                VALUES (new.id, new.title, COALESCE(new.body_tokenized, new.body), COALESCE(new.tags, ''));
            END;
            CREATE TABLE IF NOT EXISTS kb_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                scope_id INTEGER,
                canonical_id INTEGER,
                principle_ref INTEGER,
                    title TEXT,
                    body TEXT NOT NULL,
                    body_tokenized TEXT,
                    tags TEXT,
                    node_ids TEXT,
                    source_refs TEXT,
                    weight REAL NOT NULL DEFAULT 1.0,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    decay_class TEXT NOT NULL DEFAULT 'soft',
                    graph_version TEXT,
                    embedding BLOB,
                    versions_json TEXT,
                    created_at TEXT NOT NULL,
                    accessed_at TEXT,
                    access_count INTEGER DEFAULT 0,
                    provenance_commit TEXT,
                    provenance_operator TEXT
                );
            CREATE INDEX IF NOT EXISTS idx_kb_items_kind ON kb_items(kind);
            CREATE INDEX IF NOT EXISTS idx_kb_items_scope
                ON kb_items(scope_id) WHERE scope_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_kb_items_canonical
                ON kb_items(canonical_id) WHERE canonical_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_kb_items_weight
                ON kb_items(weight DESC);
            CREATE VIRTUAL TABLE IF NOT EXISTS kb_items_fts USING fts5(
                title, body_tokenized, tags,
                content='kb_items', content_rowid='id',
                tokenize='porter unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS kb_items_ai AFTER INSERT ON kb_items BEGIN
                INSERT INTO kb_items_fts(rowid, title, body_tokenized, tags)
                VALUES (new.id, new.title, COALESCE(new.body_tokenized, new.body), COALESCE(new.tags, ''));
            END;
            CREATE TRIGGER IF NOT EXISTS kb_items_ad AFTER DELETE ON kb_items BEGIN
                INSERT INTO kb_items_fts(kb_items_fts, rowid, title, body_tokenized, tags)
                VALUES ('delete', old.id, old.title, COALESCE(old.body_tokenized, old.body), COALESCE(old.tags, ''));
            END;
            CREATE TRIGGER IF NOT EXISTS kb_items_au AFTER UPDATE ON kb_items BEGIN
                INSERT INTO kb_items_fts(kb_items_fts, rowid, title, body_tokenized, tags)
                VALUES ('delete', old.id, old.title, COALESCE(old.body_tokenized, old.body), COALESCE(old.tags, ''));
                INSERT INTO kb_items_fts(rowid, title, body_tokenized, tags)
                VALUES (new.id, new.title, COALESCE(new.body_tokenized, new.body), COALESCE(new.tags, ''));
            END;
            CREATE TABLE IF NOT EXISTS kb_query_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                matched INTEGER NOT NULL,
                match_count INTEGER DEFAULT 0,
                top_score REAL,
                queried_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kb_query_log_matched
                ON kb_query_log(matched, queried_at);
            CREATE INDEX IF NOT EXISTS idx_kb_query_log_query
                ON kb_query_log(query);
            -- kb_meta holds small bookkeeping keys (e.g. the source-mtime
            -- marker the incremental rebuild skip compares against).
            CREATE TABLE IF NOT EXISTS kb_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            -- audit_log table (mirrors SQLiteStore schema; needed when
            -- _kb_connect creates a fresh db without prior `build`.
            -- kb_audit.write_audit_log_entry writes here.)
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                operator TEXT,
                command TEXT,
                target_kind TEXT,
                target_id TEXT,
                action TEXT,
                attribute TEXT,
                before_value TEXT,
                after_value TEXT,
                reason TEXT,
                tx_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_audit_log_target
                ON audit_log(target_kind, target_id);
            CREATE INDEX IF NOT EXISTS idx_audit_log_command
                ON audit_log(command);
            CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp
                ON audit_log(timestamp);
            -- foreign_refs + watched_c2ds (needed by _query_foreign_kb
            -- for F2 cross-C2D kb-query fallback). If these tables don't
            -- exist on a fresh kb-only db, _query_foreign_kb silently
            -- returns [] — creating them here makes F2 actually work.
            CREATE TABLE IF NOT EXISTS foreign_refs (
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
            CREATE INDEX IF NOT EXISTS idx_foreign_refs_local
                ON foreign_refs(local_node_id);
            CREATE INDEX IF NOT EXISTS idx_foreign_refs_status
                ON foreign_refs(status);
            CREATE TABLE IF NOT EXISTS watched_c2ds (
                c2d_path TEXT PRIMARY KEY,
                project_name TEXT,
                db_mtime_at_sync TEXT,
                db_size_at_sync INTEGER,
                functions_count_at_sync INTEGER,
                last_synced_at TEXT NOT NULL,
                sync_status TEXT NOT NULL DEFAULT 'unknown'
            );
            -- Cross-KB domains: other knowledge bases this store may
            -- query. Each kb store views itself as one domain; every
            -- watched .db is another domain.
            CREATE TABLE IF NOT EXISTS watched_kbs (
                kb_path TEXT PRIMARY KEY,
                domain_name TEXT,
                db_mtime_at_sync TEXT,
                last_synced_at TEXT NOT NULL
            );
        """)
    except sqlite3.OperationalError:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(kb_paragraphs)")}
        if "version_scope" not in cols:
            # Version identity for indexed paragraphs (branch / release
            # tag of the memory or knowledge item they mirror).
            conn.execute(
                "ALTER TABLE kb_paragraphs ADD COLUMN version_scope "
                "TEXT NOT NULL DEFAULT 'default'")
            conn.commit()
    except sqlite3.Error:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
    # Migrate to body_tokenized column (CJK pre-tokenization for FTS5).
    # Existing stores have FTS5 on `body`; new stores get FTS5 on
    # `body_tokenized` directly from the schema above. This migration
    # adds the column, backfills from `body` (raw text, no CJK
    # segmentation yet — that happens on the next rebuild-index), drops
    # the old FTS5 table + triggers, recreates them on `body_tokenized`,
    # and rebuilds the index.
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(kb_paragraphs)")}
        if "body_tokenized" not in cols:
            conn.execute(
                "ALTER TABLE kb_paragraphs ADD COLUMN body_tokenized TEXT")
            conn.execute(
                "UPDATE kb_paragraphs SET body_tokenized = body "
                "WHERE body_tokenized IS NULL")
            conn.execute("DROP TRIGGER IF EXISTS kb_paragraphs_ai")
            conn.execute("DROP TRIGGER IF EXISTS kb_paragraphs_ad")
            conn.execute("DROP TRIGGER IF EXISTS kb_paragraphs_au")
            conn.execute("DROP TABLE IF EXISTS kb_paragraphs_fts")
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS kb_paragraphs_fts "
                "USING fts5(title, body_tokenized, tags, "
                "content='kb_paragraphs', content_rowid='id', "
                "tokenize='porter unicode61')")
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS kb_paragraphs_ai "
                "AFTER INSERT ON kb_paragraphs BEGIN "
                "INSERT INTO kb_paragraphs_fts(rowid, title, body_tokenized, tags) "
                "VALUES (new.id, new.title, COALESCE(new.body_tokenized, new.body), COALESCE(new.tags, '')); END")
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS kb_paragraphs_ad "
                "AFTER DELETE ON kb_paragraphs BEGIN "
                "INSERT INTO kb_paragraphs_fts(kb_paragraphs_fts, rowid, title, body_tokenized, tags) "
                "VALUES ('delete', old.id, old.title, COALESCE(old.body_tokenized, old.body), COALESCE(old.tags, '')); END")
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS kb_paragraphs_au "
                "AFTER UPDATE ON kb_paragraphs BEGIN "
                "INSERT INTO kb_paragraphs_fts(kb_paragraphs_fts, rowid, title, body_tokenized, tags) "
                "VALUES ('delete', old.id, old.title, COALESCE(old.body_tokenized, old.body), COALESCE(old.tags, '')); "
                "INSERT INTO kb_paragraphs_fts(rowid, title, body_tokenized, tags) "
                "VALUES (new.id, new.title, COALESCE(new.body_tokenized, new.body), COALESCE(new.tags, '')); END")
            try:
                conn.execute(
                    "INSERT INTO kb_paragraphs_fts(kb_paragraphs_fts) "
                    "VALUES ('rebuild')")
            except sqlite3.OperationalError:
                pass
            conn.commit()
    except sqlite3.Error:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
    # Same migration for kb_items
    try:
        icols = {r[1] for r in conn.execute(
            "PRAGMA table_info(kb_items)")}
        if "body_tokenized" not in icols:
            conn.execute(
                "ALTER TABLE kb_items ADD COLUMN body_tokenized TEXT")
            conn.execute(
                "UPDATE kb_items SET body_tokenized = body "
                "WHERE body_tokenized IS NULL")
            conn.execute("DROP TRIGGER IF EXISTS kb_items_ai")
            conn.execute("DROP TRIGGER IF EXISTS kb_items_ad")
            conn.execute("DROP TRIGGER IF EXISTS kb_items_au")
            conn.execute("DROP TABLE IF EXISTS kb_items_fts")
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS kb_items_fts "
                "USING fts5(title, body_tokenized, tags, "
                "content='kb_items', content_rowid='id', "
                "tokenize='porter unicode61')")
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS kb_items_ai "
                "AFTER INSERT ON kb_items BEGIN "
                "INSERT INTO kb_items_fts(rowid, title, body_tokenized, tags) "
                "VALUES (new.id, new.title, COALESCE(new.body_tokenized, new.body), COALESCE(new.tags, '')); END")
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS kb_items_ad "
                "AFTER DELETE ON kb_items BEGIN "
                "INSERT INTO kb_items_fts(kb_items_fts, rowid, title, body_tokenized, tags) "
                "VALUES ('delete', old.id, old.title, COALESCE(old.body_tokenized, old.body), COALESCE(old.tags, '')); END")
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS kb_items_au "
                "AFTER UPDATE ON kb_items BEGIN "
                "INSERT INTO kb_items_fts(kb_items_fts, rowid, title, body_tokenized, tags) "
                "VALUES ('delete', old.id, old.title, COALESCE(old.body_tokenized, old.body), COALESCE(old.tags, '')); "
                "INSERT INTO kb_items_fts(rowid, title, body_tokenized, tags) "
                "VALUES (new.id, new.title, COALESCE(new.body_tokenized, new.body), COALESCE(new.tags, '')); END")
            try:
                conn.execute(
                    "INSERT INTO kb_items_fts(kb_items_fts) "
                    "VALUES ('rebuild')")
            except sqlite3.OperationalError:
                pass
            conn.commit()
    except sqlite3.Error:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
    if conn is not None:
        _import_legacy_kb(conn, graph_dir)
    return conn


_LEGACY_COPY_TABLES = ("kb_paragraphs", "kb_items", "kb_query_log",
                      "kb_meta", "watched_c2ds", "foreign_refs")


def _import_legacy_kb(conn: sqlite3.Connection, graph_dir: str) -> None:
    """One-time copy of kb tables from the pre-isolation home.

    Older releases kept the kb_* tables inside the graph's
    code2database.db. The first _kb_connect after the move copies each
    table's rows into kb_index.db (only into empty targets, so a
    partially imported store is never duplicated) and records a marker
    in kb_meta so the pass never repeats. Graph tables are never
    touched; a legacy db without kb tables simply sets the marker.
    """
    _log = logging.getLogger(__name__)
    try:
        done = conn.execute(
            "SELECT value FROM kb_meta WHERE key = 'legacy_import_done'"
        ).fetchone()
    except sqlite3.Error:
        return
    if done is not None:
        return
    legacy = _legacy_kb_db_path(graph_dir)
    copied: Dict[str, int] = {}
    if os.path.exists(legacy):
        alias = "kb_legacy_src"
        try:
            conn.execute(
                f"ATTACH DATABASE "
                f"'file:{_escape_sql_path(legacy)}?mode=ro' AS {alias}")
            try:
                legacy_tables = {r[0] for r in conn.execute(
                    f"SELECT name FROM {alias}.sqlite_master "
                    "WHERE type='table'")}
                for table in _LEGACY_COPY_TABLES:
                    if table not in legacy_tables:
                        continue
                    try:
                        target_cols = [r[1] for r in conn.execute(
                            f"PRAGMA table_info({table})")]
                        legacy_cols = {r[1] for r in conn.execute(
                            f"PRAGMA {alias}.table_info({table})")}
                        shared = [c for c in target_cols if c in legacy_cols]
                        if not shared:
                            continue
                        target_count = conn.execute(
                            f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        if target_count:
                            continue
                        col_list = ", ".join(shared)
                        cur = conn.execute(
                            f"INSERT OR REPLACE INTO {table} ({col_list}) "
                            f"SELECT {col_list} FROM {alias}.{table}")
                        if cur.rowcount and cur.rowcount > 0:
                            copied[table] = cur.rowcount
                    except sqlite3.Error:
                        # Schema drift between releases for this table —
                        # skip it; kb_paragraphs is rebuildable anyway.
                        _log.debug("legacy copy of %s skipped", table,
                                   exc_info=True)
            finally:
                try:
                    conn.execute(f"DETACH DATABASE {alias}")
                except sqlite3.Error:
                    _log.debug("silent exception", exc_info=True)
        except sqlite3.Error:
            # ATTACH failed (locked / unreadable) — retry on a later
            # connect rather than recording a half-done marker.
            _log.debug("legacy kb import deferred", exc_info=True)
            return
    try:
        conn.execute(
            "INSERT OR REPLACE INTO kb_meta (key, value) "
            "VALUES ('legacy_import_done', '1')")
        conn.commit()
    except sqlite3.Error:
        _log.debug("silent exception", exc_info=True)
    if copied:
        _log.info("imported legacy kb tables from %s: %s", legacy, copied)


def _record_query_log(conn: sqlite3.Connection, query: str,
                      results_count: int, top_score: float) -> None:
    """Log every kb-query for feedback loop & known-unknowns.

    Best-effort — silently swallows errors. Used by kb-known-unknowns
    to aggregate queries that returned no results (the user should
    write knowledge to fill those gaps).
    """
    try:
        conn.execute(
            "INSERT INTO kb_query_log (query, matched, match_count, "
            "top_score, queried_at) VALUES (?, ?, ?, ?, ?)",
            (query, 1 if results_count > 0 else 0, results_count,
             top_score, datetime.now().isoformat())
        )
        conn.commit()
    except sqlite3.Error:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
def get_known_unknowns(graph_dir: str, top_n: int = 20,
                       min_occurrences: int = 2) -> List[Dict[str, Any]]:
    """Aggregate unmatched queries into 'known unknowns'.

    Returns queries that returned 0 matches and were asked at least
    `min_occurrences` times. Grouped by FTS5 similarity so similar
    unanswered questions cluster together. Read-only: returns [] when
    the kb index store doesn't exist yet (never creates one).
    """
    conn = _kb_connect(graph_dir, create_if_missing=False)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT query, COUNT(*) AS occurrences, "
            "MAX(queried_at) AS last_asked "
            "FROM kb_query_log WHERE matched = 0 "
            "GROUP BY query HAVING occurrences >= ? "
            "ORDER BY occurrences DESC, last_asked DESC LIMIT ?",
            (min_occurrences, top_n)
        ).fetchall()
        return [{
            "query": r["query"],
            "occurrences": r["occurrences"],
            "last_asked": r["last_asked"],
        } for r in rows]
    finally:
        conn.close()


def _fts5_escape(query: str) -> str:
    """Escape a free-form query string for FTS5 MATCH.

    Re-exported from _builder.utils so the kb and cgdb search paths share
    one implementation. See utils._fts5_escape for the full docstring.
    """
    from _builder.utils import _fts5_escape as _impl
    return _impl(query)


def _split_markdown_paragraphs(text: str) -> List[tuple]:
    """Split a Markdown file into (title, body) paragraph tuples.

    Each `## ` heading starts a new section; the heading text becomes
    the title and the body is the content until the next heading.
    Content before the first `##` heading is treated as a preamble
    with title = first `#` heading or filename stem.
    """
    paragraphs = []
    current_title = None
    current_lines: List[str] = []
    file_title = None
    for line in text.split("\n"):
        m_h1 = re.match(r'^#\s+(.+)$', line)
        m_h2 = re.match(r'^##\s+(.+)$', line)
        if m_h2:
            if current_lines and (current_title or file_title):
                body = "\n".join(current_lines).strip()
                if body:
                    paragraphs.append((current_title or file_title, body))
            current_title = m_h2.group(1).strip()
            current_lines = []
        elif m_h1 and file_title is None:
            file_title = m_h1.group(1).strip()
        else:
            current_lines.append(line)
    if current_lines and (current_title or file_title):
        body = "\n".join(current_lines).strip()
        if body:
            paragraphs.append((current_title or file_title, body))
    # If no headings found, treat whole file as one paragraph
    if not paragraphs and text.strip():
        paragraphs.append((file_title or "untitled", text.strip()))
    return paragraphs


def _load_memory_entries(graph_dir: str) -> List[dict]:
    """Load all memory entries from memory/memory.db (SQLite store).

    Returns active + experience rows as dicts with kind set to
    'memory_qa' or 'memory_experience' based on status. Returns []
    when no memory store exists yet.
    """
    db_path = os.path.join(graph_dir, "memory", "memory.db")
    if not os.path.exists(db_path):
        return []
    import sqlite3 as _sqlite3
    entries: List[dict] = []
    conn = None
    try:
        conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = _sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM memories WHERE status IN "
            "('active', 'experience')").fetchall()
    except (_sqlite3.Error, OSError):
        if conn is not None:
            conn.close()
        return []
    for r in rows:
        try:
            entry = dict(r)
        except Exception:
            continue
        entry["kind"] = ("memory_qa" if entry.get("status") == "active"
                         else "memory_experience")
        entry["_source_subdir"] = "db"
        entry["_source_prefix"] = "mem_"
        entries.append(entry)
    conn.close()
    return entries


def _brief_sections_as_paragraphs(brief: dict, source_file: str,
                                  project: str = "") -> List[dict]:
    """Flatten a brief (or foreign brief) into kb paragraph dicts."""
    paragraphs: List[dict] = []
    section_map = [
        ("description", "Description", brief.get("description", "")),
        ("must_know", "Must Know", brief.get("must_know", "")),
    ]
    for hr in brief.get("hard_rules") or []:
        if isinstance(hr, dict) and hr.get("rule"):
            section_map.append(
                ("hard_rule", "Hard Rule", hr["rule"],
                 hr.get("version_scope", "default")))
    for m in brief.get("modes") or []:
        if isinstance(m, dict) and m.get("name"):
            body = f"use when {m.get('when', '')} — " \
                   f"{m.get('differences', '')}"
            section_map.append(
                ("mode", f"Mode: {m['name']}", body,
                 m.get("version_scope", "default")))
    for ab in brief.get("key_abstractions") or []:
        if isinstance(ab, dict) and ab.get("name"):
            section_map.append(
                ("abstraction", ab["name"], ab.get("role", ""),
                 ab.get("version_scope", "default")))
    for key, title in (("conventions", "Convention"),
                       ("pitfalls", "Pitfall"),
                       ("query_paths", "Query Path")):
        for item in brief.get(key) or []:
            if item:
                section_map.append((key, title, str(item), "default"))

    for para_index, entry in enumerate(section_map):
        kind, title, body = entry[0], entry[1], entry[2]
        version_scope = entry[3] if len(entry) > 3 else "default"
        if not body or not str(body).strip():
            continue
        paragraphs.append({
            "source_kind": "knowledge",
            "source_file": source_file,
            "para_index": para_index,
            "title": f"[{project}] {title}" if project else title,
            "body": str(body),
            "tags": None,
            "node_ids": None,
            "weight": 1.0,  # knowledge has no decay
            "confidence": 1.0,
            "kind": kind,
            "graph_version": None,
            "created_at": datetime.now().isoformat(),
            "version_scope": version_scope or "default",
        })
    return paragraphs


def _load_knowledge_paragraphs(graph_dir: str) -> List[dict]:
    """Load knowledge paragraphs from the knowledge store (+ foreign
    briefs).

    The knowledge store (knowledge/knowledge.db) is the source of
    truth when present — its rows are flattened through the same
    brief-section mapping, so paragraphs match sync_brief_to_kb
    exactly. Foreign briefs shared via import-foreign-knowledge land
    as knowledge/foreign_<project>_brief.json and are indexed too.
    Legacy stores without the database fall back to brief.json.
    """
    from _builder.kb.knowledge_store import open_knowledge
    knowledge_dir = os.path.join(graph_dir, "knowledge")
    paragraphs: List[dict] = []
    store = open_knowledge(graph_dir)  # read path: never creates
    store_brief = None
    if store is not None:
        try:
            store_brief = store.to_brief()
        finally:
            store.close()
    if store_brief is not None:
        project = store_brief.get("project", "") or "this project"
        paragraphs.extend(_brief_sections_as_paragraphs(
            store_brief, "brief.json", project))
    if not os.path.isdir(knowledge_dir):
        return paragraphs
    for fname in sorted(os.listdir(knowledge_dir)):
        if not fname.endswith(".json"):
            continue
        if store_brief is not None and fname == "brief.json":
            # Already indexed from the store rows — scanning the
            # derived file would double every paragraph.
            continue
        fpath = os.path.join(knowledge_dir, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                brief = json.load(f)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(brief, dict):
            continue
        project = brief.get("project", "")
        if fname == "brief.json":
            project = project or "this project"
        else:
            m = re.match(r'foreign_(\w+)_brief\.json$', fname)
            if m:
                project = project or m.group(1)
        paragraphs.extend(
            _brief_sections_as_paragraphs(brief, fname, project))
    return paragraphs


def _is_transient_store_artifact(fname: str, fpath: str) -> bool:
    """True for bookkeeping files sqlite or the flock guard drops next
    to the real sources in memory/ and knowledge/.

    The incremental-skip signature compares mtimes of the source dirs.
    A -shm, -journal or .lock sidecar appears and vanishes with connection
    lifecycle — a reader or writer that was open while the scan ran can
    leave one behind with the newest mtime in the dir, which made an
    unchanged store look changed and defeated the skip. A -wal bigger
    than its 32-byte header carries real frames (a crashed or still
    active writer) and stays part of the signature so those states
    still trigger a rebuild.
    """
    if fname.endswith(("-shm", "-journal")) or fname.endswith(".lock"):
        return True
    if fname.endswith("-wal"):
        try:
            return os.path.getsize(fpath) <= 32
        except OSError:
            return False
    return False


def rebuild_kb_index(graph_dir: str, verbose: bool = True) -> dict:
    """Rebuild the kb_paragraphs index from filesystem sources.

    Returns a summary dict with counts. Idempotent: drops existing
    rows and reinserts. Safe to call repeatedly.

    P4 optimization: if no source files changed since last rebuild
    (compared via max mtime), the rebuild is skipped entirely.
    """
    conn = _kb_connect(graph_dir)
    if conn is None:
        if verbose:
            print(f"[kb-rebuild] No kb store could be opened at "
                  f"{graph_dir}; nothing to rebuild", file=sys.stderr)
        return {"rebuilt": False, "reason": "no_store",
                "memory_count": 0, "knowledge_count": 0}
    try:
        # P4: Incremental skip — compute max mtime of all source files
        # and compare against stored value. If unchanged, skip rebuild.
        _max_mtime = 0.0
        _mem_dir = os.path.join(graph_dir, "memory")
        _know_dir = os.path.join(graph_dir, "knowledge")
        for _d in (_mem_dir, _know_dir):
            if os.path.isdir(_d):
                for _fname in os.listdir(_d):
                    _fpath = os.path.join(_d, _fname)
                    if os.path.isfile(_fpath) and \
                            not _is_transient_store_artifact(_fname, _fpath):
                        try:
                            _mt = os.path.getmtime(_fpath)
                            if _mt > _max_mtime:
                                _max_mtime = _mt
                        except OSError as _e:
                            logging.getLogger(__name__).debug(
                                "getmtime failed for %s: %s", _fpath, _e)
        try:
            _row = conn.execute(
                "SELECT value FROM kb_meta WHERE key = 'last_rebuild_mtime'"
            ).fetchone()
            # Use strict equality (==), not >=. >= wrongly skips when:
            #   - A file was deleted → new max < stored → stored >= new_max
            #     is True, but paragraphs from the deleted file are stale.
            #   - The directory became empty → new max = 0, stored is from
            #     a previous non-empty build → stored >= 0 is True, but
            #     paragraphs are stale.
            # Strict equality only matches when the set of files is exactly
            # the same AND no file was modified (mtime is the highest it
            # was last time). Otherwise, rebuild to drop stale paragraphs.
            if _row is not None and _max_mtime > 0 \
                    and float(_row["value"]) == _max_mtime:
                # Nothing changed — skip rebuild
                _count_row = conn.execute(
                    "SELECT COUNT(*) AS c FROM kb_paragraphs"
                ).fetchone()
                return {
                    "rebuilt": False, "reason": "unchanged",
                    "memory_count": 0, "knowledge_count": 0,
                    "total_paragraphs": _count_row["c"] if _count_row else 0,
                }
        except sqlite3.OperationalError:
            # kb_meta table may not exist yet — proceed with full rebuild
            pass
        # Clear FTS5 index first (before DELETE) so triggers don't do
        # redundant work during the DELETE + INSERT cycle. The 'deleteall'
        # command clears the entire FTS5 index in one shot — much faster
        # than letting AD triggers fire row-by-row during DELETE.
        try:
            conn.execute("INSERT INTO kb_paragraphs_fts(kb_paragraphs_fts) VALUES ('deleteall')")
        except sqlite3.OperationalError:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        # Drop existing rows
        conn.execute("DELETE FROM kb_paragraphs")
        # Build batch
        rows: List[tuple] = []
        # Memory entries → one row per entry (question=title, answer=body)
        for entry in _load_memory_entries(graph_dir):
            q = entry.get("question", "")
            a = entry.get("answer", "")
            if not q and not a:
                continue
            tags = entry.get("tags", [])
            tags_json = json.dumps(tags, ensure_ascii=False) if tags else None
            node_ids = entry.get("node_ids", [])
            node_ids_json = json.dumps(node_ids, ensure_ascii=False) if node_ids else None
            weight = float(entry.get("weight", 1.0))
            created = entry.get("created", entry.get("validated_at",
                              datetime.now().isoformat()))
            kind = entry.get("kind", "memory_qa")
            source_file = entry.get("_source_subdir", "") + "/" + \
                          (entry.get("_source_prefix", "mem_") +
                           str(entry.get("id", "")) + ".json")
            rows.append((
                "memory", source_file, 0,
                q[:500], a, _cjk_pre_tokenize(a), tags_json, node_ids_json,
                weight, 1.0, kind, None, created, None, 0,
                entry.get("version_scope") or "default",
            ))
        # Knowledge paragraphs → one row per paragraph
        for para in _load_knowledge_paragraphs(graph_dir):
            pbody = para["body"]
            rows.append((
                para["source_kind"], para["source_file"], para["para_index"],
                para["title"], pbody, _cjk_pre_tokenize(pbody),
                para["tags"], para["node_ids"],
                para["weight"], para["confidence"], para["kind"],
                para["graph_version"], para["created_at"],
                para.get("accessed_at"), para.get("access_count", 0),
                para.get("version_scope") or "default",
            ))
        # Bulk insert
        if rows:
            conn.executemany(
                "INSERT INTO kb_paragraphs "
                "(source_kind, source_file, para_index, title, body, "
                " body_tokenized, tags, node_ids, weight, confidence, kind, "
                " graph_version, created_at, accessed_at, access_count, "
                " version_scope) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        conn.commit()
        # Rebuild FTS5 (in case triggers missed anything)
        try:
            conn.execute("INSERT INTO kb_paragraphs_fts(kb_paragraphs_fts) VALUES ('rebuild')")
        except sqlite3.OperationalError:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        # P4: Store max source mtime for incremental skip on next rebuild
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS kb_meta (key TEXT PRIMARY KEY, value TEXT)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO kb_meta (key, value) "
                "VALUES ('last_rebuild_mtime', ?)",
                (str(_max_mtime),)
            )
        except sqlite3.Error:
            pass
        # The FTS 'rebuild' command and the mtime marker above run after the
        # data commit, in their own implicit transaction — persist them
        # before the summary read, otherwise close() discards them.
        conn.commit()
        cur = conn.execute(
            "SELECT source_kind, COUNT(*) FROM kb_paragraphs GROUP BY source_kind"
        )
        counts = {row[0]: row[1] for row in cur.fetchall()}
        if verbose:
            print(f"[kb-rebuild] Rebuilt {sum(counts.values())} rows: "
                  f"{counts}", file=sys.stderr)
        return {
            "rebuilt": True,
            "memory_count": counts.get("memory", 0),
            "knowledge_count": counts.get("knowledge", 0),
            "total": sum(counts.values()),
            "by_kind": counts,
        }
    finally:
        conn.close()


def upsert_kb_paragraph(graph_dir: str, source_kind: str, source_file: str,
                        title: str, body: str, tags: List[str] = None,
                        node_ids: List[str] = None, weight: float = 1.0,
                        kind: str = "qa", confidence: float = 1.0,
                        graph_version: str = None,
                        version_scope: str = "default") -> int:
    """Insert or update a single kb_paragraph row.

    Used to keep the FTS5 index in sync after direct kb writes
    after a filesystem write. Returns the row id.
    """
    conn = _kb_connect(graph_dir)
    if conn is None:
        return -1
    try:
        tags_json = json.dumps(tags, ensure_ascii=False) if tags else None
        node_ids_json = json.dumps(node_ids, ensure_ascii=False) if node_ids else None
        cur = conn.execute(
            "INSERT INTO kb_paragraphs "
            "(source_kind, source_file, para_index, title, body, "
            " body_tokenized, tags, "
            " node_ids, weight, confidence, kind, graph_version, created_at, "
            " access_count, version_scope) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (source_kind, source_file, 0, title, body,
             _cjk_pre_tokenize(body), tags_json,
             node_ids_json, weight, confidence, kind, graph_version,
             datetime.now().isoformat(),
             version_scope or "default")
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def delete_kb_paragraphs_by_source(graph_dir: str, source_file: str) -> int:
    """Delete all kb_paragraphs rows matching a source file.

    Used by memory-correct / knowledge-rewrite when the underlying file
    is replaced; caller re-inserts via upsert_kb_paragraph.
    """
    conn = _kb_connect(graph_dir)
    if conn is None:
        return 0
    try:
        cur = conn.execute(
            "DELETE FROM kb_paragraphs WHERE source_file = ?",
            (source_file,)
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def sync_brief_to_kb(graph_dir: str, brief: dict) -> int:
    """Incrementally sync brief content into kb_paragraphs.

    Drops existing rows where source_file='brief.json' (the canonical
    brief path) and re-inserts from _brief_sections_as_paragraphs.
    Called by save_brief() so kb-query / describe-node see the updated
    knowledge immediately, without waiting for a full kb-rebuild-index.

    save_brief wrote brief.json but didn't
    trigger kb_index sync — new paragraphs didn't appear in FTS5 until
    a manual kb-rebuild-index run.
    """
    conn = _kb_connect(graph_dir)
    if conn is None:
        return 0
    try:
        project = brief.get("project", "") if isinstance(brief, dict) else ""
        paragraphs = _brief_sections_as_paragraphs(
            brief or {}, "brief.json", project)
        # Drop existing rows for the canonical brief path.
        conn.execute(
            "DELETE FROM kb_paragraphs WHERE source_file = ?",
            ("brief.json",))
        # Insert the new paragraphs.
        for p in paragraphs:
            pbody = p["body"]
            conn.execute(
                "INSERT INTO kb_paragraphs "
                "(source_kind, source_file, para_index, title, body, "
                " body_tokenized, tags, "
                " node_ids, weight, confidence, kind, graph_version, "
                " created_at, access_count, version_scope) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (p["source_kind"], p["source_file"], p["para_index"],
                 p["title"], pbody, _cjk_pre_tokenize(pbody),
                 p.get("tags"),
                 p.get("node_ids"), p.get("weight", 1.0),
                 p.get("confidence", 1.0), p.get("kind", "knowledge"),
                 p.get("graph_version"),
                 datetime.now().isoformat(),
                 p.get("version_scope") or "default"))
        conn.commit()
        return len(paragraphs)
    except sqlite3.Error as exc:
        logging.getLogger(__name__).warning(
            "sync_brief_to_kb failed: %s", exc, exc_info=True)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return 0
    finally:
        conn.close()


def sync_memory_entries(graph_dir: str, mem_ids: List[int]) -> int:
    """Incrementally sync memory entries into kb_paragraphs.

    For each id: drop its indexed rows, then re-insert from
    memory/memory.db while the entry is still active/experience
    (tombstoned 'merged' entries simply lose their rows). Uses the
    exact field mapping of rebuild_kb_index, so a synced entry is
    indistinguishable from a full-rebuild row. Keeps memory edits
    searchable between full rebuilds. The kb store is the knowledge
    base's own artifact, so it is created when missing. Returns the
    number of entries re-inserted.
    """
    conn = _kb_connect(graph_dir)
    db_path = os.path.join(graph_dir, "memory", "memory.db")
    if not os.path.exists(db_path):
        if conn is not None:
            conn.close()
        return 0
    mem = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    mem.row_factory = sqlite3.Row
    synced = 0
    try:
        for mid in mem_ids:
            source_file = f"db/mem_{mid}.json"
            conn.execute(
                "DELETE FROM kb_paragraphs WHERE source_kind = 'memory' "
                "AND source_file = ?", (source_file,))
            r = mem.execute(
                "SELECT * FROM memories WHERE id = ?", (mid,)).fetchone()
            if r is None or r["status"] not in ("active", "experience"):
                continue
            q = r["question"] or ""
            a = r["answer"] or ""
            if not q and not a:
                continue
            try:
                tags = json.loads(r["tags"] or "[]")
            except (json.JSONDecodeError, TypeError):
                tags = []
            try:
                node_ids = json.loads(r["node_ids"] or "[]")
            except (json.JSONDecodeError, TypeError):
                node_ids = []
            kind = ("memory_qa" if r["status"] == "active"
                    else "memory_experience")
            created = (r["created"] or r["validated_at"]
                       or datetime.now().isoformat())
            conn.execute(
                "INSERT INTO kb_paragraphs "
                "(source_kind, source_file, para_index, title, body, "
                " body_tokenized, tags, node_ids, weight, confidence, kind, "
                " graph_version, created_at, accessed_at, access_count, "
                " version_scope) "
                "VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, 1.0, ?, NULL, ?, NULL, "
                "0, ?)",
                ("memory", source_file, q[:500], a,
                 _cjk_pre_tokenize(a),
                 json.dumps(tags, ensure_ascii=False) if tags else None,
                 json.dumps(node_ids, ensure_ascii=False) if node_ids else None,
                 float(r["weight"] or 1.0), kind, created,
                 r["version_scope"] if "version_scope" in r.keys()
                 else "default"))
            synced += 1
        conn.commit()
    finally:
        mem.close()
        conn.close()
    return synced


def query_kb(graph_dir: str, query: str, top_n: int = 10,
             kinds: Optional[List[str]] = None,
             min_weight: float = 0.0,
             max_tokens: int = 4000,
             semantic: bool = False,
             log_query: bool = True,
             update_access: bool = True,
             version_scope: str = None,
             cross: bool = False) -> List[Dict[str, Any]]:
    """Unified FTS5 + BM25 search across all kb_paragraphs.

    Args:
        query: Free-form text query (tokenized and AND-joined).
        top_n: Max results to return.
        kinds: Optional filter on the `kind` column.
        min_weight: Skip rows with weight below this.
        max_tokens: Approximate character cap on returned bodies.
        semantic: if True and embeddings are populated,
                  fall back to cosine similarity for items lacking
                  FTS5 token overlap. Currently a no-op stub: returns
                  only FTS5 matches but the interface is in place.
        log_query: record this query in kb_query_log for
                   feedback loop analysis (set False for internal calls).
        version_scope: the code version (branch / release tag) the
                   caller is working on. When given, entries learned on
                   that version rank ahead of the rest (ordering only —
                   other-version entries are still returned, labeled
                   via is_current_scope on each result).
        cross: also search every watched kb store (cross-domain
                   query). Hits from other domains carry source_domain
                   and source_kb.

    Returns:
        List of dicts with id, source_kind, source_file, title, body,
        tags, node_ids, weight, kind, score, see_also (items in the same
        cluster), version_scope, is_current_scope.
    """
    conn = _kb_connect(graph_dir)
    if conn is None:
        return []
    # Validate non-empty query — FTS5 MATCH on empty string matches ALL
    # rows (no relevance filtering), which is almost never what the user wants.
    if not query or not query.strip():
        return []
    try:
        match_expr = _fts5_escape(query)
        sql = (
            "SELECT kb_paragraphs.id, kb_paragraphs.source_kind, "
            "       kb_paragraphs.source_file, kb_paragraphs.title, "
            "       kb_paragraphs.body, kb_paragraphs.tags, "
            "       kb_paragraphs.node_ids, kb_paragraphs.weight, "
            "       kb_paragraphs.kind, kb_paragraphs.scope_id, "
            "       kb_paragraphs.canonical_id, "
            "       kb_paragraphs.version_scope, "
            "       -bm25(kb_paragraphs_fts) * "
            "       (0.5 + 0.5 * MIN(kb_paragraphs.weight / 2.0, 1.0)) AS score "
            "FROM kb_paragraphs_fts "
            "JOIN kb_paragraphs ON kb_paragraphs.id = kb_paragraphs_fts.rowid "
            "WHERE kb_paragraphs_fts MATCH ? "
            "  AND kb_paragraphs.weight >= ? "
        )
        params: list = [match_expr, min_weight]
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            sql += f"  AND kb_paragraphs.kind IN ({placeholders}) "
            params.extend(kinds)
        scope = (version_scope or "").strip() or None
        if scope:
            # Current-version entries first, then the rest — each group
            # by score. Ordering only; other versions stay retrievable.
            sql += "ORDER BY (kb_paragraphs.version_scope = ?) DESC, "
            sql += "score DESC LIMIT ?"
            params.extend([scope, top_n])
        else:
            sql += "ORDER BY score DESC LIMIT ?"
            params.append(top_n)
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            rows = []
        sim_scores: Dict[int, float] = {}
        # CJK fallback previously only triggered
        # when FTS5 returned ZERO results (`not rows`). For CJK+Latin
        # mixed queries, the Latin token could produce partial FTS5
        # hits, suppressing the CJK similarity scan — missing paragraphs
        # whose content was purely CJK. The memory_store already uses
        # `not fts_rows or _has_cjk(query)` (always runs similarity for
        # CJK queries); kb_index now matches that behavior.
        # Also added a LIMIT to the candidate scan so large knowledge
        # bases don't do a full table scan.
        if not rows or _has_cjk(query):
            # CJK fallback: the unicode61 tokenizer folds each CJK run
            # into one token, so a Chinese query never token-matches
            # (pure-CJK queries even escape to an empty FTS phrase and
            # return nothing). Scan candidates with token-set similarity
            # (CJK chars + bigrams) instead.
            sql2 = "SELECT kb_paragraphs.* FROM kb_paragraphs " \
                   "WHERE kb_paragraphs.weight >= ? "
            params2: list = [min_weight]
            if kinds:
                placeholders = ",".join("?" for _ in kinds)
                sql2 += f"AND kb_paragraphs.kind IN ({placeholders}) "
                params2.extend(kinds)
            # Cap candidates to avoid full table scan on large KBs.
            # Scored and sorted below; top_n limits final output.
            sql2 += "ORDER BY kb_paragraphs.weight DESC LIMIT 500"
            cand = conn.execute(sql2, params2).fetchall()
            q_tokens = _simple_tokenize(query)
            scored_cand = []
            for r in cand:
                e_tokens = _simple_tokenize(" ".join(
                    [r["title"] or "", r["body"] or "", r["tags"] or ""]))
                sim = _similarity_score(q_tokens, e_tokens)
                if sim > 0:
                    scored_cand.append(
                        (sim * (0.5 + 0.5 * min(r["weight"] / 2.0, 1.0)), r))
            scored_cand.sort(key=lambda x: -x[0])
            for s, r in scored_cand[:top_n]:
                sim_scores[r["id"]] = s
            # Merge: prefer FTS5 hits (already in `rows`), then add
            # similarity hits not already present. scored_cand holds
            # (score, row) tuples, so unpack to get the row.
            existing_ids = {r["id"] for r in rows}
            for _, r in scored_cand[:top_n]:
                if r["id"] not in existing_ids:
                    rows.append(r)
                    if len(rows) >= top_n:
                        break
        if scope:
            # The similarity channel appends after FTS rows without
            # scope awareness — restore current-version-first ordering
            # across the merged set before building results.
            def _row_scope(r):
                try:
                    return r["version_scope"] or "default"
                except (IndexError, KeyError):
                    return "default"

            def _row_score(r):
                if "score" in r.keys():
                    return r["score"]
                return sim_scores.get(r["id"], 0.0)

            rows.sort(key=lambda r: (0 if _row_scope(r) == scope else 1,
                                     -_row_score(r)))
        max_chars = max_tokens * 4
        results = []
        for r in rows:
            if "score" in r.keys():
                score = r["score"]
            else:
                score = sim_scores.get(r["id"], 0.0)
            tags = r["tags"]
            if tags:
                try:
                    tags = json.loads(tags)
                except (json.JSONDecodeError, TypeError):
                    tags = []
            node_ids = r["node_ids"]
            if node_ids:
                try:
                    node_ids = json.loads(node_ids)
                except (json.JSONDecodeError, TypeError):
                    node_ids = []
            body = r["body"] or ""
            if len(body) > max_chars:
                body = body[:max_chars] + "\n... (truncated)"
            try:
                entry_scope = r["version_scope"] or "default"
            except (IndexError, KeyError):
                entry_scope = "default"
            results.append({
                "id": r["id"],
                "source_kind": r["source_kind"],
                "source_file": r["source_file"],
                "title": r["title"] or "",
                "body": body,
                "tags": tags or [],
                "node_ids": node_ids or [],
                "weight": round(r["weight"], 4),
                "kind": r["kind"],
                "score": round(score, 4),
                "scope_id": r["scope_id"],
                "canonical_id": r["canonical_id"],
                "version_scope": entry_scope,
                "is_current_scope": bool(scope)
                and entry_scope == scope,
            })
        # attach see_also — items in the same cluster
        # (same scope_id) ranked by weight × confidence.
        for res in results:
            scope_id = res.get("scope_id")
            if scope_id is None:
                res["see_also"] = []
                continue
            try:
                see_rows = conn.execute(
                    "SELECT id, source_kind, source_file, title, weight, kind "
                    "FROM kb_paragraphs WHERE scope_id = ? AND id != ? "
                    "ORDER BY weight DESC LIMIT 5",
                    (scope_id, res["id"])
                ).fetchall()
                res["see_also"] = [{
                    "id": sr["id"],
                    "source_kind": sr["source_kind"],
                    "source_file": sr["source_file"],
                    "title": sr["title"] or "",
                    "weight": round(sr["weight"], 4),
                    "kind": sr["kind"],
                } for sr in see_rows]
            except sqlite3.Error:
                res["see_also"] = []
        # Update access_count on returned rows (best-effort)
        # Update access_count on returned rows (best-effort). Skipped in
        # read-only mode (a --read-only MCP server must not mutate the
        # production DB on every query — including these bookkeeping writes).
        if update_access:
            try:
                for res in results:
                    conn.execute(
                        "UPDATE kb_paragraphs SET access_count = access_count + 1, "
                        "accessed_at = ? WHERE id = ?",
                        (datetime.now().isoformat(), res["id"])
                    )
                conn.commit()
            except sqlite3.Error:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        if log_query:
            top_score = results[0]["score"] if results else 0.0
            _record_query_log(conn, query, len(results), top_score)
        # if local results are thin, also search foreign C2Ds'
        # kb_paragraphs (via watched_c2ds + ATTACH). This lets B's
        # kb-query see A's knowledge.md content when local KB is thin.
        if len(results) < top_n:
            try:
                foreign_hits = _query_foreign_kb(conn, query, top_n - len(results),
                                                  min_weight, max_tokens)
                results.extend(foreign_hits)
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        if cross:
            # Cross-domain: search every watched kb store's index and
            # label the hits with their source domain.
            try:
                domain_hits = _query_watched_kbs(
                    conn, query, top_n, min_weight, max_tokens,
                    version_scope=version_scope)
                results.extend(domain_hits)
            except Exception:
                logging.getLogger(__name__).debug(
                    "silent exception", exc_info=True)
        return results
    finally:
        conn.close()


def _foreign_kb_db_path(c2d_path: str) -> Optional[str]:
    """Locate a watched C2D's kb index, new home first.

    The kb index lives in kb_index.db; projects from before the
    relocation keep it inside code2database.db. Returns None when
    neither exists.
    """
    for name in ("kb_index.db", "code2database.db"):
        path = os.path.join(c2d_path, name)
        if os.path.exists(path):
            return path
    return None


def _query_foreign_kb(conn: sqlite3.Connection, query: str, top_n: int,
                      min_weight: float, max_tokens: int) -> List[Dict[str, Any]]:
    """Search kb_paragraphs in all watched foreign C2Ds.

    ATTACHes each foreign db read-only and queries its kb_paragraphs_fts.
    Returns results tagged with source_db so consumers know which C2D
    each hit came from.
    """
    match_expr = _fts5_escape(query)
    foreign_results: List[Dict[str, Any]] = []
    try:
        watched = conn.execute(
            "SELECT c2d_path, project_name FROM watched_c2ds "
            "WHERE sync_status IN ('ok', 'stub')"
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # watched_c2ds table doesn't exist
    for w in watched:
        c2d_path = w["c2d_path"]
        project_name = w["project_name"] or ""
        fdb_path = _foreign_kb_db_path(c2d_path)
        if fdb_path is None:
            continue
        alias = f"fkb_{abs(hash(c2d_path)) % 100000}"
        try:
            conn.execute(
                f"ATTACH DATABASE 'file:{_escape_sql_path(fdb_path)}?mode=ro' AS {alias}"
            )
            rows = conn.execute(
                # MATCH and bm25() take the FTS table name unqualified:
                # an alias-qualified operand ("<alias>.kb_paragraphs_fts
                # MATCH ...") is rejected by SQLite as an unknown column,
                # and the one FTS table in this FROM clause is the
                # attached one, so resolution is unambiguous.
                f"SELECT p.id, p.source_kind, p.source_file, p.title, "
                f"p.body, p.tags, p.weight, p.kind, "
                f"-bm25(kb_paragraphs_fts) AS score "
                f"FROM {alias}.kb_paragraphs_fts "
                f"JOIN {alias}.kb_paragraphs p ON p.id = {alias}.kb_paragraphs_fts.rowid "
                f"WHERE kb_paragraphs_fts MATCH ? "
                f"AND p.weight >= ? "
                f"ORDER BY score DESC LIMIT ?",
                (match_expr, min_weight, top_n)
            ).fetchall()
            max_chars = max_tokens * 4
            for r in rows:
                body = r["body"] or ""
                if len(body) > max_chars:
                    body = body[:max_chars] + "\n... (truncated)"
                tags = r["tags"]
                if tags:
                    try:
                        tags = json.loads(tags)
                    except (json.JSONDecodeError, TypeError):
                        tags = []
                foreign_results.append({
                    "id": r["id"],
                    "source_kind": r["source_kind"],
                    "source_file": r["source_file"],
                    "title": r["title"] or "",
                    "body": body,
                    "tags": tags or [],
                    "weight": round(r["weight"], 4),
                    "kind": r["kind"],
                    "score": round(r["score"], 4),
                    "source_db": c2d_path,
                    "foreign_project": project_name,
                })
            conn.execute(f"DETACH DATABASE {alias}")
        except sqlite3.Error:
            try:
                conn.execute(f"DETACH DATABASE {alias}")
            except sqlite3.Error:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
            continue
    return foreign_results


# ---------------------------------------------------------------------------
# Cross-KB domains
# ---------------------------------------------------------------------------

def _default_domain_name(graph_dir: str) -> str:
    """Domain name fallback: the store directory's parent name."""
    parent = os.path.basename(os.path.dirname(os.path.abspath(graph_dir)))
    return parent or os.path.basename(os.path.abspath(graph_dir)) or "local"


def get_domain_name(graph_dir: str) -> str:
    """This store's domain identity (kb_meta domain_name, or the
    directory-derived fallback)."""
    conn = _kb_connect(graph_dir, create_if_missing=False)
    if conn is None:
        return _default_domain_name(graph_dir)
    try:
        row = conn.execute(
            "SELECT value FROM kb_meta WHERE key = 'domain_name'"
        ).fetchone()
        if row is not None and str(row[0]).strip():
            return str(row[0]).strip()
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return _default_domain_name(graph_dir)


def set_domain_name(graph_dir: str, name: str) -> str:
    """Record this store's domain identity."""
    clean = (name or "").strip()
    if not clean:
        raise ValueError("domain name must be non-empty")
    conn = _kb_connect(graph_dir)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO kb_meta (key, value) "
            "VALUES ('domain_name', ?)", (clean,))
        conn.commit()
    finally:
        conn.close()
    return clean


def watch_kb(graph_dir: str, kb_path: str, domain_name: str = "") -> dict:
    """Register another knowledge base as a queryable domain."""
    kb_path = os.path.abspath(kb_path)
    if not os.path.isfile(_kb_db_path(kb_path)):
        return {"error": f"no kb store at {kb_path} "
                         f"(expected kb_index.db)"}
    if not domain_name:
        domain_name = get_domain_name(kb_path)
    conn = _kb_connect(graph_dir)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO watched_kbs "
            "(kb_path, domain_name, db_mtime_at_sync, last_synced_at) "
            "VALUES (?, ?, ?, ?)",
            (kb_path, domain_name,
             str(os.path.getmtime(_kb_db_path(kb_path))),
             datetime.now().isoformat()))
        conn.commit()
    finally:
        conn.close()
    return {"watched": True, "kb_path": kb_path,
            "domain_name": domain_name}


def unwatch_kb(graph_dir: str, kb_path: str) -> dict:
    conn = _kb_connect(graph_dir)
    try:
        cur = conn.execute("DELETE FROM watched_kbs WHERE kb_path = ?",
                           (os.path.abspath(kb_path),))
        conn.commit()
        removed = bool(cur.rowcount)
    finally:
        conn.close()
    return {"removed": removed,
            "kb_path": os.path.abspath(kb_path)}


def list_watched_kbs(graph_dir: str) -> List[Dict[str, Any]]:
    conn = _kb_connect(graph_dir, create_if_missing=False)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT kb_path, domain_name, db_mtime_at_sync, "
            "last_synced_at FROM watched_kbs ORDER BY domain_name"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _query_watched_kbs(conn: sqlite3.Connection, query: str, top_n: int,
                       min_weight: float, max_tokens: int,
                       version_scope: str = None
                       ) -> List[Dict[str, Any]]:
    """Search the FTS index of every watched kb store, read-only.

    Results are tagged with source_domain / source_kb so consumers can
    tell which domain each hit came from. MATCH and bm25() stay
    unqualified (an alias-qualified operand is rejected by SQLite).
    """
    match_expr = _fts5_escape(query)
    out: List[Dict[str, Any]] = []
    try:
        watched = conn.execute(
            "SELECT kb_path, domain_name FROM watched_kbs"
        ).fetchall()
    except sqlite3.OperationalError:
        return out
    for w in watched:
        kb_path = w["kb_path"]
        domain = w["domain_name"] or _default_domain_name(kb_path)
        fdb = _kb_db_path(kb_path)
        if not os.path.exists(fdb):
            continue
        alias = f"kkb_{abs(hash(kb_path)) % 100000}"
        try:
            conn.execute(
                f"ATTACH DATABASE "
                f"'file:{_escape_sql_path(fdb)}?mode=ro' AS {alias}")
            sql = (
                f"SELECT p.id, p.source_kind, p.source_file, p.title, "
                f"p.body, p.tags, p.weight, p.kind, p.version_scope, "
                f"-bm25(kb_paragraphs_fts) AS score "
                f"FROM {alias}.kb_paragraphs_fts "
                f"JOIN {alias}.kb_paragraphs p "
                f"ON p.id = {alias}.kb_paragraphs_fts.rowid "
                f"WHERE kb_paragraphs_fts MATCH ? "
                f"AND p.weight >= ? "
                f"ORDER BY score DESC LIMIT ?"
            )
            rows = conn.execute(sql, (match_expr, min_weight, top_n)
                                ).fetchall()
            max_chars = max_tokens * 4
            for r in rows:
                body = r["body"] or ""
                if len(body) > max_chars:
                    body = body[:max_chars] + "\n... (truncated)"
                out.append({
                    "id": r["id"],
                    "source_kind": r["source_kind"],
                    "source_file": r["source_file"],
                    "title": r["title"] or "",
                    "body": body,
                    "tags": r["tags"] or [],
                    "weight": round(r["weight"], 4),
                    "kind": r["kind"],
                    "score": round(r["score"], 4),
                    "version_scope": r["version_scope"] or "default",
                    "source_domain": domain,
                    "source_kb": kb_path,
                })
            conn.execute(f"DETACH DATABASE {alias}")
        except sqlite3.Error:
            try:
                conn.execute(f"DETACH DATABASE {alias}")
            except sqlite3.Error:
                logging.getLogger(__name__).debug(
                    "silent exception", exc_info=True)
            continue
    if version_scope:
        # Within each domain, prefer entries learned on the caller's
        # version (ordering only, same rule as the local store).
        out.sort(key=lambda r: (0 if r.get("version_scope")
                                == version_scope else 1, -r["score"]))
    return out
