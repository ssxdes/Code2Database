#!/usr/bin/env python3
"""Knowledge store — the curated project facts, in SQLite.

Knowledge is the SMALL, curated, stable description of THIS project:
architecture rules, modes, abstractions, conventions, pitfalls,
query routes. It is deliberately separate from the memory store
(memory/memory.db, episodic Q&A that decays and merges): different
definition, different logical model (typed rows vs clustered
question/answer entries), different physical file
(knowledge/knowledge.db).

The knowledge base is the source of truth. knowledge/brief.json
stays as a derived, size-budgeted prompt view regenerated after
every write, so existing brief consumers (session-init,
foreign import) keep working unchanged.

Row kinds map onto the brief sections:
    description / must_know       — scalar rows
    hard_rule                     — {rule, type, detail, evidence}
    mode                          — {name, when, differences}
    abstraction                   — {name, role}
    convention / pitfall / query_path — plain strings

Every item carries the code version it applies to (version_scope)
and a lifecycle (active/retired) with an origin tag (curated /
graduated from memory / auto-extracted) plus graduation lineage
(source_memory_id).
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

import logging

from _builder.utils import _cjk_pre_tokenize, _fts5_escape, _has_cjk, \
    _simple_tokenize, _similarity_score

KNOWLEDGE_SCHEMA_VERSION = 2

_KINDS = ("description", "must_know", "hard_rule", "mode", "abstraction",
          "convention", "pitfall", "query_path")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT DEFAULT '',
    body TEXT NOT NULL,
    body_tokenized TEXT,
    tags TEXT DEFAULT '[]',
    extra_json TEXT DEFAULT '{}',
    version_scope TEXT NOT NULL DEFAULT 'default',
    status TEXT NOT NULL DEFAULT 'active',
    origin TEXT NOT NULL DEFAULT 'curated',
    source_memory_id INTEGER,
    weight REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    revised_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_knowledge_kind ON knowledge_items(kind);
CREATE INDEX IF NOT EXISTS idx_knowledge_status ON knowledge_items(status);
CREATE INDEX IF NOT EXISTS idx_knowledge_scope
    ON knowledge_items(version_scope);
CREATE TABLE IF NOT EXISTS knowledge_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    title, body_tokenized, tags,
    content='knowledge_items', content_rowid='id',
    tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge_items BEGIN
    INSERT INTO knowledge_fts(rowid, title, body_tokenized, tags)
    VALUES (new.id, new.title,
            COALESCE(new.body_tokenized, new.body),
            COALESCE(new.tags, '[]'));
END;
CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge_items BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, body_tokenized, tags)
    VALUES ('delete', old.id, old.title,
            COALESCE(old.body_tokenized, old.body),
            COALESCE(old.tags, '[]'));
END;
CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER
    UPDATE OF title, body, body_tokenized, tags ON knowledge_items BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, body_tokenized, tags)
    VALUES ('delete', old.id, old.title,
            COALESCE(old.body_tokenized, old.body),
            COALESCE(old.tags, '[]'));
    INSERT INTO knowledge_fts(rowid, title, body_tokenized, tags)
    VALUES (new.id, new.title,
            COALESCE(new.body_tokenized, new.body),
            COALESCE(new.tags, '[]'));
END;
"""


def _migrate_to_body_tokenized(conn: sqlite3.Connection) -> None:
    """One-time migration: add body_tokenized column to stores that
    predate CJK pre-tokenization, backfill from existing rows via the
    shared _cjk_pre_tokenize helper, and rebuild the FTS5 index +
    triggers so the index reflects segmented tokens instead of raw
    unicode61-folded CJK runs.

    Mirrors the migration kb_index.py applies to kb_paragraphs, so
    both stores stay symmetric on the body_tokenized contract.

    Ordering matters: the schema script may have created new triggers
    that reference body_tokenized, but the FTS5 table still has the
    old `body` column (CREATE VIRTUAL TABLE IF NOT EXISTS is a no-op
    when the table exists). Those triggers fire on the backfill UPDATE
    and fail because knowledge_fts lacks body_tokenized. So triggers
    must be dropped BEFORE the backfill, then recreated after the FTS5
    table is rebuilt on the new column.
    """
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(knowledge_items)")}
    except sqlite3.Error:
        return
    if "body_tokenized" not in cols:
        # Drop any existing triggers BEFORE the backfill so they
        # cannot fire on the UPDATE and fail against the old FTS5
        # schema. The schema script created new-schema triggers; the
        # pre-migration database had old-schema triggers. Drop both.
        try:
            conn.execute("DROP TRIGGER IF EXISTS knowledge_ai")
            conn.execute("DROP TRIGGER IF EXISTS knowledge_ad")
            conn.execute("DROP TRIGGER IF EXISTS knowledge_au")
        except sqlite3.Error:
            logging.getLogger(__name__).debug(
                "silent exception", exc_info=True)
        try:
            conn.execute(
                "ALTER TABLE knowledge_items ADD COLUMN body_tokenized TEXT")
        except sqlite3.OperationalError:
            # Column may have been added concurrently by another
            # connection; the backfill below is still safe.
            pass
        # Backfill via Python so the FTS5 index reflects segmented
        # tokens immediately. knowledge_items is a small curated
        # table so the per-row cost is negligible. Triggers are gone,
        # so the UPDATE cannot fire a stale trigger.
        try:
            rows = conn.execute(
                "SELECT id, body FROM knowledge_items").fetchall()
            for rid, body in rows:
                tokenized = _cjk_pre_tokenize(body or "")
                conn.execute(
                    "UPDATE knowledge_items SET body_tokenized = ? "
                    "WHERE id = ?", (tokenized, rid))
            conn.commit()
        except sqlite3.Error:
            logging.getLogger(__name__).debug(
                "silent exception", exc_info=True)
        # Drop and recreate FTS5 + triggers so the virtual table
        # indexes body_tokenized (not body). Same pattern as the
        # kb_paragraphs migration in kb_index.py.
        try:
            conn.execute("DROP TABLE IF EXISTS knowledge_fts")
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts "
                "USING fts5(title, body_tokenized, tags, "
                "content='knowledge_items', content_rowid='id', "
                "tokenize='porter unicode61')")
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS knowledge_ai "
                "AFTER INSERT ON knowledge_items BEGIN "
                "INSERT INTO knowledge_fts(rowid, title, body_tokenized, tags) "
                "VALUES (new.id, new.title, "
                "COALESCE(new.body_tokenized, new.body), "
                "COALESCE(new.tags, '[]')); END")
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS knowledge_ad "
                "AFTER DELETE ON knowledge_items BEGIN "
                "INSERT INTO knowledge_fts(knowledge_fts, rowid, title, "
                "body_tokenized, tags) "
                "VALUES ('delete', old.id, old.title, "
                "COALESCE(old.body_tokenized, old.body), "
                "COALESCE(old.tags, '[]')); END")
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS knowledge_au "
                "AFTER UPDATE OF title, body, body_tokenized, tags "
                "ON knowledge_items BEGIN "
                "INSERT INTO knowledge_fts(knowledge_fts, rowid, title, "
                "body_tokenized, tags) "
                "VALUES ('delete', old.id, old.title, "
                "COALESCE(old.body_tokenized, old.body), "
                "COALESCE(old.tags, '[]')); "
                "INSERT INTO knowledge_fts(rowid, title, body_tokenized, tags) "
                "VALUES (new.id, new.title, "
                "COALESCE(new.body_tokenized, new.body), "
                "COALESCE(new.tags, '[]')); END")
            try:
                conn.execute(
                    "INSERT INTO knowledge_fts(knowledge_fts) "
                    "VALUES ('rebuild')")
            except sqlite3.OperationalError:
                pass
            conn.commit()
        except sqlite3.Error:
            logging.getLogger(__name__).debug(
                "silent exception", exc_info=True)


def knowledge_db_path(graph_dir: str) -> str:
    return os.path.join(graph_dir, "knowledge", "knowledge.db")


class KnowledgeStore:
    """SQLite-backed curated knowledge, one row per item."""

    def __init__(self, graph_dir: str, read_only: bool = False):
        self.graph_dir = graph_dir
        self.db_path = knowledge_db_path(graph_dir)
        self.read_only = read_only
        if read_only:
            if not os.path.exists(self.db_path):
                raise sqlite3.OperationalError(
                    f"no knowledge store at {self.db_path}")
            self._conn = self._connect()
            return
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = self._connect()
        try:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except sqlite3.Error:
            # executescript may fail partway when migrating from an
            # older schema (e.g. triggers referencing body_tokenized
            # before the migration adds that column). Roll back any
            # pending transaction so _migrate_to_body_tokenized can
            # run cleanly.
            try:
                self._conn.rollback()
            except sqlite3.Error:
                pass
            logging.getLogger(__name__).debug(
                "silent exception", exc_info=True)
        _migrate_to_body_tokenized(self._conn)
        self._import_legacy_brief()

    # -- connection ---------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{self.db_path}?mode=ro" if self.read_only \
            else self.db_path
        conn = sqlite3.connect(uri, uri=self.read_only)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def close(self):
        try:
            self._conn.close()
        except sqlite3.Error:
            logging.getLogger(__name__).debug("silent exception",
                                              exc_info=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- one-time legacy import ----------------------------------------

    def _import_legacy_brief(self):
        """Seed rows from a pre-database brief.json, once.

        Projects built before the knowledge store kept their curated
        content in knowledge/brief.json. On the first write-side open,
        its sections become rows so the database takes over as the
        source of truth; the file is regenerated as a derived view
        afterwards.
        """
        try:
            done = self._conn.execute(
                "SELECT value FROM knowledge_meta "
                "WHERE key = 'brief_import_done'").fetchone()
        except sqlite3.Error:
            return
        if done is not None:
            return
        brief_path = os.path.join(self.graph_dir, "knowledge",
                                  "brief.json")
        imported = 0
        if os.path.exists(brief_path):
            try:
                with open(brief_path, "r", encoding="utf-8") as f:
                    brief = json.load(f)
            except (OSError, json.JSONDecodeError):
                brief = None
            if isinstance(brief, dict):
                for item in self._rows_from_brief(brief):
                    self._insert_row(item)
                    imported += 1
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO knowledge_meta (key, value) "
                "VALUES ('brief_import_done', ?)", (str(imported),))
            self._conn.commit()
        except sqlite3.Error:
            logging.getLogger(__name__).debug("silent exception",
                                              exc_info=True)
        if imported:
            logging.getLogger(__name__).info(
                "imported %d knowledge item(s) from brief.json", imported)

    # -- row helpers ----------------------------------------------------

    @staticmethod
    def _row_to_dict(r) -> dict:
        d = dict(r)
        try:
            d["tags"] = json.loads(d.get("tags") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["tags"] = []
        try:
            d["extra"] = json.loads(d.get("extra_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["extra"] = {}
        return d

    def _insert_row(self, item: dict) -> int:
        body = item["body"]
        body_tokenized = _cjk_pre_tokenize(body) if body else ""
        cur = self._conn.execute(
            "INSERT INTO knowledge_items (kind, title, body, body_tokenized, "
            "tags, extra_json, version_scope, status, origin, "
            "source_memory_id, weight, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item["kind"], item.get("title", ""), body, body_tokenized,
             json.dumps(item.get("tags") or [], ensure_ascii=False),
             json.dumps(item.get("extra") or {}, ensure_ascii=False),
             item.get("version_scope", "default"),
             item.get("status", "active"),
             item.get("origin", "curated"),
             item.get("source_memory_id"),
             item.get("weight", 1.0),
             item.get("created_at", datetime.now().isoformat())))
        return cur.lastrowid

    # -- public API ------------------------------------------------------

    def add(self, kind: str, body: str, title: str = "",
            tags: List[str] = None, extra: dict = None,
            version_scope: str = "default", origin: str = "curated",
            source_memory_id: int = None) -> int:
        if kind not in _KINDS:
            raise ValueError(f"unknown knowledge kind {kind!r}; "
                             f"expected one of {_KINDS}")
        if not body or not str(body).strip():
            raise ValueError("knowledge body must be non-empty")
        with self._conn:
            return self._insert_row({
                "kind": kind, "title": title, "body": str(body),
                "tags": tags, "extra": extra,
                "version_scope": (version_scope or "").strip()
                or "default",
                "origin": origin, "source_memory_id": source_memory_id,
            })

    def revise(self, item_id: int, body: str = None, title: str = None,
                tags: List[str] = None, extra: dict = None) -> bool:
        """Improve an item's description in place (revised_at stamp)."""
        sets, params = [], []
        if body is not None:
            if not str(body).strip():
                raise ValueError("knowledge body must be non-empty")
            sets.append("body = ?")
            params.append(str(body))
            sets.append("body_tokenized = ?")
            params.append(_cjk_pre_tokenize(str(body)))
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if tags is not None:
            sets.append("tags = ?")
            params.append(json.dumps(tags, ensure_ascii=False))
        if extra is not None:
            sets.append("extra_json = ?")
            params.append(json.dumps(extra, ensure_ascii=False))
        if not sets:
            return False
        sets.append("revised_at = ?")
        params.append(datetime.now().isoformat())
        params.append(item_id)
        with self._conn:
            cur = self._conn.execute(
                f"UPDATE knowledge_items SET {', '.join(sets)} "
                f"WHERE id = ?", params)
        return bool(cur.rowcount)

    def retire(self, item_id: int) -> bool:
        """Mark an item retired (kept for lineage, hidden from views)."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE knowledge_items SET status = 'retired', "
                "revised_at = ? WHERE id = ? AND status = 'active'",
                (datetime.now().isoformat(), item_id))
        return bool(cur.rowcount)

    def remove(self, item_id: int) -> bool:
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM knowledge_items WHERE id = ?", (item_id,))
        return bool(cur.rowcount)

    def get(self, item_id: int) -> Optional[dict]:
        r = self._conn.execute(
            "SELECT * FROM knowledge_items WHERE id = ?",
            (item_id,)).fetchone()
        return self._row_to_dict(r) if r is not None else None

    def list_items(self, kind: str = None,
                   version_scope: str = None,
                   include_retired: bool = False) -> List[dict]:
        sql = "SELECT * FROM knowledge_items"
        conds, params = [], []
        if not include_retired:
            conds.append("status = 'active'")
        if kind:
            conds.append("kind = ?")
            params.append(kind)
        if version_scope:
            conds.append("version_scope = ?")
            params.append(version_scope)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY kind, id"
        return [self._row_to_dict(r) for r in
                self._conn.execute(sql, params).fetchall()]

    def counts(self) -> Dict[str, int]:
        out = {}
        for r in self._conn.execute(
                "SELECT kind, COUNT(*) AS c FROM knowledge_items "
                "WHERE status = 'active' GROUP BY kind"):
            out[r["kind"]] = r["c"]
        out["_total"] = sum(out.values())
        return out

    def search(self, query: str, top_n: int = 10,
               kinds: Optional[List[str]] = None) -> List[dict]:
        """FTS5 + BM25 search over curated knowledge rows.

        Mirrors the contract of kb_index.query_kb: AND-joined FTS5
        MATCH (CJK pre-tokenized on write, so pure-CJK queries match
        segmented tokens) with weight-scaled BM25 score, optional
        kinds filter, and a CJK similarity fallback when FTS5 yields
        no rows for a CJK query.
        """
        if not query or not str(query).strip():
            return []
        match_expr = _fts5_escape(query)
        sql = (
            "SELECT ki.id, ki.kind, ki.title, ki.body, ki.tags, "
            "ki.weight, ki.version_scope, "
            "bm25(knowledge_fts) AS score "
            "FROM knowledge_fts "
            "JOIN knowledge_items ki ON ki.id = knowledge_fts.rowid "
            "WHERE knowledge_fts MATCH ? AND ki.status = 'active'")
        params: list = [match_expr]
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            sql += f" AND ki.kind IN ({placeholders})"
            params.extend(kinds)
        sql += " ORDER BY score ASC LIMIT ?"
        params.append(top_n)
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            rows = []
        results = []
        for r in rows:
            d = self._row_to_dict(r)
            d["score"] = r["score"]
            results.append(d)
        # CJK fallback: when FTS5 yields nothing (or the query is CJK
        # and may have segmented differently), scan top-500 by weight
        # and rank by token-set similarity. Mirrors kb_index.query_kb.
        if not results or _has_cjk(query):
            q_tokens = _simple_tokenize(query)
            if q_tokens:
                fsql = (
                    "SELECT id, kind, title, body, tags, weight, "
                    "version_scope FROM knowledge_items "
                    "WHERE status = 'active'")
                fparams: list = []
                if kinds:
                    placeholders = ",".join("?" for _ in kinds)
                    fsql += f" AND kind IN ({placeholders})"
                    fparams.extend(kinds)
                fsql += " ORDER BY weight DESC LIMIT 500"
                try:
                    cands = self._conn.execute(fsql, fparams).fetchall()
                except sqlite3.OperationalError:
                    cands = []
                seen_ids = {r["id"] for r in results}
                fallback = []
                for c in cands:
                    if c["id"] in seen_ids:
                        continue
                    c_tokens = _simple_tokenize(
                        (c["title"] or "") + " " + (c["body"] or ""))
                    sim = _similarity_score(q_tokens, c_tokens)
                    if sim > 0:
                        d = self._row_to_dict(c)
                        d["score"] = -sim
                        fallback.append(d)
                fallback.sort(key=lambda x: x["score"])
                results.extend(fallback[:top_n])
                results = results[:top_n]
        return results

    def rebuild_tokenized(self) -> int:
        """Re-tokenize every row's body and refresh the FTS5 index.

        Useful after a tokenizer upgrade (new synonyms, new stopword
        list, new jieba user_dict) so existing rows reflect the new
        tokenization without requiring a per-row revise.
        """
        n = 0
        with self._conn:
            rows = self._conn.execute(
                "SELECT id, body FROM knowledge_items").fetchall()
            for rid, body in rows:
                tokenized = _cjk_pre_tokenize(body or "")
                self._conn.execute(
                    "UPDATE knowledge_items SET body_tokenized = ? "
                    "WHERE id = ?", (tokenized, rid))
                n += 1
            try:
                self._conn.execute(
                    "INSERT INTO knowledge_fts(knowledge_fts) "
                    "VALUES ('rebuild')")
            except sqlite3.OperationalError:
                pass
        return n

    # -- brief interop ----------------------------------------------------

    def to_brief(self) -> dict:
        """Assemble the brief-dict shape from active rows."""
        brief = {
            "schema_version": KNOWLEDGE_SCHEMA_VERSION,
            "project": "", "one_liner": "", "description": "",
            "must_know": "",
            "hard_rules": [], "modes": [], "key_abstractions": [],
            "conventions": [], "pitfalls": [], "query_paths": [],
            "graph_stats": {}, "updated_at": "",
        }
        try:
            meta = {r["key"]: r["value"] for r in self._conn.execute(
                "SELECT key, value FROM knowledge_meta")}
            brief["project"] = meta.get("project", "")
            brief["one_liner"] = meta.get("one_liner", "")
        except sqlite3.Error:
            pass
        for item in self.list_items():
            kind, body = item["kind"], item["body"]
            extra = item.get("extra") or {}
            if kind == "description":
                brief["description"] = body
            elif kind == "must_know":
                brief["must_know"] = body
            elif kind == "hard_rule":
                brief["hard_rules"].append({
                    "rule": body, "type": extra.get("type", ""),
                    "detail": extra.get("detail", ""),
                    "evidence": extra.get("evidence", ""),
                    "version_scope": item.get("version_scope", "default"),
                })
            elif kind == "mode":
                brief["modes"].append({
                    "name": item.get("title", ""),
                    "when": extra.get("when", ""),
                    "differences": extra.get("differences", ""),
                    "version_scope": item.get("version_scope", "default"),
                })
            elif kind == "abstraction":
                brief["key_abstractions"].append({
                    "name": item.get("title", ""),
                    "role": extra.get("role", ""),
                    "version_scope": item.get("version_scope", "default"),
                })
            elif kind in ("convention", "pitfall", "query_path"):
                brief[kind + "s"].append(body)
        return brief

    def replace_from_brief(self, brief: dict) -> int:
        """Replace all rows with the brief's sections (write path for
        the brief-* commands). project/one_liner persist in meta so a
        later load reconstructs them. Returns the number of rows
        written."""
        with self._conn:
            self._conn.execute("DELETE FROM knowledge_items")
            n = 0
            for item in self._rows_from_brief(brief or {}):
                self._insert_row(item)
                n += 1
            self._conn.execute(
                "INSERT OR REPLACE INTO knowledge_meta (key, value) "
                "VALUES ('project', ?)",
                ((brief or {}).get("project", "") or "",))
            self._conn.execute(
                "INSERT OR REPLACE INTO knowledge_meta (key, value) "
                "VALUES ('one_liner', ?)",
                ((brief or {}).get("one_liner", "") or "",))
        return n

    @staticmethod
    def _rows_from_brief(brief: dict) -> List[dict]:
        rows: List[dict] = []
        if brief.get("description"):
            rows.append({"kind": "description", "body":
                         brief["description"]})
        if brief.get("must_know"):
            rows.append({"kind": "must_know", "body": brief["must_know"]})
        for hr in brief.get("hard_rules") or []:
            if isinstance(hr, dict) and hr.get("rule"):
                rows.append({
                    "kind": "hard_rule", "body": hr["rule"],
                    "extra": {"type": hr.get("type", ""),
                              "detail": hr.get("detail", ""),
                              "evidence": hr.get("evidence", "")},
                    "version_scope": hr.get("version_scope", "default"),
                })
        for m in brief.get("modes") or []:
            if isinstance(m, dict) and m.get("name"):
                rows.append({
                    "kind": "mode", "title": m["name"],
                    "body": f"use when {m.get('when', '')} — "
                            f"{m.get('differences', '')}",
                    "extra": {"when": m.get("when", ""),
                              "differences": m.get("differences", "")},
                    "version_scope": m.get("version_scope", "default"),
                })
        for ab in brief.get("key_abstractions") or []:
            if isinstance(ab, dict) and ab.get("name"):
                rows.append({
                    "kind": "abstraction", "title": ab["name"],
                    "body": ab.get("role", ""),
                    "extra": {"role": ab.get("role", "")},
                    "version_scope": ab.get("version_scope", "default"),
                })
        for kind in ("convention", "pitfall", "query_path"):
            for text in brief.get(kind + "s") or []:
                if text:
                    rows.append({"kind": kind, "body": str(text)})
        return rows


def open_knowledge(graph_dir: str, create_if_missing: bool = False) \
        -> Optional[KnowledgeStore]:
    """Open the knowledge store; None when it doesn't exist yet.

    Read paths use create_if_missing=False so a knowledge-only status
    query never creates files; write paths create (and run the
    one-time brief.json import).
    """
    if not os.path.exists(knowledge_db_path(graph_dir)):
        if not create_if_missing:
            return None
    try:
        return KnowledgeStore(graph_dir)
    except sqlite3.OperationalError:
        logging.getLogger(__name__).debug("silent exception",
                                          exc_info=True)
        return None
