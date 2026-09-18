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
every write, so existing brief consumers (session-init, web UI,
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

KNOWLEDGE_SCHEMA_VERSION = 1

_KINDS = ("description", "must_know", "hard_rule", "mode", "abstraction",
          "convention", "pitfall", "query_path")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT DEFAULT '',
    body TEXT NOT NULL,
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
    title, body, tags,
    content='knowledge_items', content_rowid='id',
    tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge_items BEGIN
    INSERT INTO knowledge_fts(rowid, title, body, tags)
    VALUES (new.id, new.title, new.body, COALESCE(new.tags, '[]'));
END;
CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge_items BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, body, tags)
    VALUES ('delete', old.id, old.title, old.body,
            COALESCE(old.tags, '[]'));
END;
CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER
    UPDATE OF title, body, tags ON knowledge_items BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, body, tags)
    VALUES ('delete', old.id, old.title, old.body,
            COALESCE(old.tags, '[]'));
    INSERT INTO knowledge_fts(rowid, title, body, tags)
    VALUES (new.id, new.title, new.body, COALESCE(new.tags, '[]'));
END;
"""


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
            logging.getLogger(__name__).debug(
                "silent exception", exc_info=True)
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
        cur = self._conn.execute(
            "INSERT INTO knowledge_items (kind, title, body, tags, "
            "extra_json, version_scope, status, origin, "
            "source_memory_id, weight, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item["kind"], item.get("title", ""), item["body"],
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
                })
            elif kind == "mode":
                brief["modes"].append({
                    "name": item.get("title", ""),
                    "when": extra.get("when", ""),
                    "differences": extra.get("differences", ""),
                })
            elif kind == "abstraction":
                brief["key_abstractions"].append({
                    "name": item.get("title", ""),
                    "role": extra.get("role", ""),
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
                })
        for m in brief.get("modes") or []:
            if isinstance(m, dict) and m.get("name"):
                rows.append({
                    "kind": "mode", "title": m["name"],
                    "body": f"use when {m.get('when', '')} — "
                            f"{m.get('differences', '')}",
                    "extra": {"when": m.get("when", ""),
                              "differences": m.get("differences", "")},
                })
        for ab in brief.get("key_abstractions") or []:
            if isinstance(ab, dict) and ab.get("name"):
                rows.append({
                    "kind": "abstraction", "title": ab["name"],
                    "body": ab.get("role", ""),
                    "extra": {"role": ab.get("role", "")},
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
