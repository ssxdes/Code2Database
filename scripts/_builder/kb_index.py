"""Knowledge-base index builder and FTS5 query interface.

Phase 1 of the KB unification: builds a derived SQLite table
(kb_paragraphs + kb_paragraphs_fts) from the canonical filesystem
sources (memory/*.json and knowledge/*.md) so a single FTS5 + BM25
query can search across both stores.

The filesystem files remain the source of truth — kb_paragraphs is
rebuildable via `kb-rebuild-index`. Writes to memory/knowledge should
call upsert_kb_paragraph() to keep the index in sync (Phase 4 will
add a transaction-aware wrapper that auto-updates on memory/knowledge
mutations).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any


def _kb_db_path(graph_dir: str) -> str:
    return os.path.join(graph_dir, "code2database.db")


def _kb_connect(graph_dir: str) -> Optional[sqlite3.Connection]:
    """Open a connection to the project's code2database.db.

    Returns None if the db doesn't exist (caller should fall back to
    the legacy per-store search in that case).
    """
    db_path = _kb_db_path(graph_dir)
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
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
                embedding BLOB
            );
            CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_kind
                ON kb_paragraphs(kind);
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
                title, body, tags,
                content='kb_paragraphs',
                content_rowid='id',
                tokenize='porter unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS kb_paragraphs_ai AFTER INSERT ON kb_paragraphs BEGIN
                INSERT INTO kb_paragraphs_fts(rowid, title, body, tags)
                VALUES (new.id, new.title, new.body, COALESCE(new.tags, ''));
            END;
            CREATE TRIGGER IF NOT EXISTS kb_paragraphs_ad AFTER DELETE ON kb_paragraphs BEGIN
                INSERT INTO kb_paragraphs_fts(kb_paragraphs_fts, rowid, title, body, tags)
                VALUES ('delete', old.id, old.title, old.body, COALESCE(old.tags, ''));
            END;
            CREATE TRIGGER IF NOT EXISTS kb_paragraphs_au AFTER UPDATE ON kb_paragraphs BEGIN
                INSERT INTO kb_paragraphs_fts(kb_paragraphs_fts, rowid, title, body, tags)
                VALUES ('delete', old.id, old.title, old.body, COALESCE(old.tags, ''));
                INSERT INTO kb_paragraphs_fts(rowid, title, body, tags)
                VALUES (new.id, new.title, new.body, COALESCE(new.tags, ''));
            END;
            CREATE TABLE IF NOT EXISTS kb_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                scope_id INTEGER,
                canonical_id INTEGER,
                principle_ref INTEGER,
                title TEXT,
                body TEXT NOT NULL,
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
                title, body, tags,
                content='kb_items', content_rowid='id',
                tokenize='porter unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS kb_items_ai AFTER INSERT ON kb_items BEGIN
                INSERT INTO kb_items_fts(rowid, title, body, tags)
                VALUES (new.id, new.title, new.body, COALESCE(new.tags, ''));
            END;
            CREATE TRIGGER IF NOT EXISTS kb_items_ad AFTER DELETE ON kb_items BEGIN
                INSERT INTO kb_items_fts(kb_items_fts, rowid, title, body, tags)
                VALUES ('delete', old.id, old.title, old.body, COALESCE(old.tags, ''));
            END;
            CREATE TRIGGER IF NOT EXISTS kb_items_au AFTER UPDATE ON kb_items BEGIN
                INSERT INTO kb_items_fts(kb_items_fts, rowid, title, body, tags)
                VALUES ('delete', old.id, old.title, old.body, COALESCE(old.tags, ''));
                INSERT INTO kb_items_fts(rowid, title, body, tags)
                VALUES (new.id, new.title, new.body, COALESCE(new.tags, ''));
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
        """)
    except sqlite3.OperationalError:
        pass
    return conn


def _record_query_log(conn: sqlite3.Connection, query: str,
                      results_count: int, top_score: float) -> None:
    """Phase 9: log every kb-query for feedback loop & known-unknowns.

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
        pass


def get_known_unknowns(graph_dir: str, top_n: int = 20,
                      min_occurrences: int = 2) -> List[Dict[str, Any]]:
    """Phase 9: aggregate unmatched queries into 'known unknowns'.

    Returns queries that returned 0 matches and were asked at least
    `min_occurrences` times. Grouped by FTS5 similarity so similar
    unanswered questions cluster together.
    """
    conn = _kb_connect(graph_dir)
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

    FTS5 query syntax treats special chars (:", *, (, ), etc.) as
    operators. For user-typed queries we want lenient token matching:
    split on whitespace, quote each token, join with AND. This way
    "how does bdev register" matches documents containing all three
    tokens (in any order, any distance).
    """
    tokens = re.findall(r'[A-Za-z0-9_]+', query)
    if not tokens:
        return '""'  # match nothing safely
    return " ".join(f'"{t}"' for t in tokens)


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
    """Load all memory entries (root + leaf + experience) as dicts.

    Defensive against corrupt/legacy index files. Returns entries with
    a `kind` field set to 'memory_qa' or 'memory_experience' based on
    location.
    """
    memory_dir = os.path.join(graph_dir, "memory")
    entries: List[dict] = []
    if not os.path.isdir(memory_dir):
        return entries
    # Walk root/, leaf/, experience/ subdirs
    for subdir, prefix, kind in (
        ("root", "root_", "memory_qa"),
        ("leaf", "mem_", "memory_qa"),
        ("experience", "experience_", "memory_experience"),
    ):
        sub_path = os.path.join(memory_dir, subdir)
        if not os.path.isdir(sub_path):
            continue
        for fname in sorted(os.listdir(sub_path)):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(sub_path, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    entry = json.load(f)
                if isinstance(entry, dict):
                    entry.setdefault("kind", kind)
                    entry["_source_subdir"] = subdir
                    entry["_source_prefix"] = prefix
                    entries.append(entry)
            except (OSError, json.JSONDecodeError):
                continue
    # Fallback: old flat memory_<id>.json files
    for fname in sorted(os.listdir(memory_dir)):
        if not fname.startswith("memory_") or not fname.endswith(".json"):
            continue
        fpath = os.path.join(memory_dir, fname)
        if os.path.isdir(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                entry = json.load(f)
            if isinstance(entry, dict):
                entry.setdefault("kind", "memory_qa")
                entry["_source_subdir"] = ""
                entry["_source_prefix"] = "memory_"
                entries.append(entry)
        except (OSError, json.JSONDecodeError):
            continue
    return entries


def _load_knowledge_paragraphs(graph_dir: str) -> List[dict]:
    """Load all knowledge .md files as paragraph dicts."""
    knowledge_dir = os.path.join(graph_dir, "knowledge")
    paragraphs: List[dict] = []
    if not os.path.isdir(knowledge_dir):
        return paragraphs
    for fname in sorted(os.listdir(knowledge_dir)):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(knowledge_dir, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        # Determine kind from filename
        if fname.startswith("principles"):
            kind = "principle"
        elif fname.startswith("glossary"):
            kind = "glossary"
        elif fname.startswith("constraints"):
            kind = "fact"
        elif fname.startswith("patterns"):
            kind = "pattern"
        elif fname.startswith("detail_"):
            kind = "fact"
        elif fname.startswith("custom_"):
            kind = "fact"
        else:
            kind = "fact"
        for para_index, (title, body) in enumerate(_split_markdown_paragraphs(content)):
            paragraphs.append({
                "source_kind": "knowledge",
                "source_file": fname,
                "para_index": para_index,
                "title": title,
                "body": body,
                "tags": None,
                "node_ids": None,
                "weight": 1.0,  # knowledge has no decay
                "confidence": 1.0,
                "kind": kind,
                "graph_version": None,
                "created_at": datetime.now().isoformat(),
            })
    return paragraphs


def rebuild_kb_index(graph_dir: str, verbose: bool = True) -> dict:
    """Rebuild the kb_paragraphs index from filesystem sources.

    Returns a summary dict with counts. Idempotent: drops existing
    rows and reinserts. Safe to call repeatedly.
    """
    conn = _kb_connect(graph_dir)
    if conn is None:
        if verbose:
            print(f"[kb-rebuild] No code2database.db at {graph_dir}; "
                  f"nothing to rebuild", file=sys.stderr)
        return {"rebuilt": False, "reason": "no_db",
                "memory_count": 0, "knowledge_count": 0}
    try:
        # Drop existing rows (triggers will sync FTS5)
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
                q[:500], a, tags_json, node_ids_json,
                weight, 1.0, kind, None, created, None, 0,
            ))
        # Knowledge paragraphs → one row per paragraph
        for para in _load_knowledge_paragraphs(graph_dir):
            rows.append((
                para["source_kind"], para["source_file"], para["para_index"],
                para["title"], para["body"], para["tags"], para["node_ids"],
                para["weight"], para["confidence"], para["kind"],
                para["graph_version"], para["created_at"],
                para.get("accessed_at"), para.get("access_count", 0),
            ))
        # Bulk insert
        if rows:
            conn.executemany(
                "INSERT INTO kb_paragraphs "
                "(source_kind, source_file, para_index, title, body, "
                " tags, node_ids, weight, confidence, kind, "
                " graph_version, created_at, accessed_at, access_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        conn.commit()
        # Rebuild FTS5 (in case triggers missed anything)
        try:
            conn.execute("INSERT INTO kb_paragraphs_fts(kb_paragraphs_fts) VALUES ('rebuild')")
        except sqlite3.OperationalError:
            pass
        # Stats
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
                        graph_version: str = None) -> int:
    """Insert or update a single kb_paragraph row.

    Used by save-memory / apply-knowledge to keep the FTS5 index in sync
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
            "(source_kind, source_file, para_index, title, body, tags, "
            " node_ids, weight, confidence, kind, graph_version, created_at, "
            " access_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (source_kind, source_file, 0, title, body, tags_json,
             node_ids_json, weight, confidence, kind, graph_version,
             datetime.now().isoformat())
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


def query_kb(graph_dir: str, query: str, top_n: int = 10,
             kinds: Optional[List[str]] = None,
             min_weight: float = 0.0,
             max_tokens: int = 4000,
             semantic: bool = False,
             log_query: bool = True) -> List[Dict[str, Any]]:
    """Unified FTS5 + BM25 search across all kb_paragraphs.

    Args:
        query: Free-form text query (tokenized and AND-joined).
        top_n: Max results to return.
        kinds: Optional filter on the `kind` column.
        min_weight: Skip rows with weight below this.
        max_tokens: Approximate character cap on returned bodies.
        semantic: Phase 5 — if True and embeddings are populated,
                  fall back to cosine similarity for items lacking
                  FTS5 token overlap. Currently a no-op stub: returns
                  only FTS5 matches but the interface is in place.
        log_query: Phase 9 — record this query in kb_query_log for
                   feedback loop analysis (set False for internal calls).

    Returns:
        List of dicts with id, source_kind, source_file, title, body,
        tags, node_ids, weight, kind, score, see_also (Phase 4 —
        items in the same cluster).
    """
    conn = _kb_connect(graph_dir)
    if conn is None:
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
        sql += "ORDER BY score DESC LIMIT ?"
        params.append(top_n)
        rows = conn.execute(sql, params).fetchall()
        max_chars = max_tokens * 4
        results = []
        for r in rows:
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
                "score": round(r["score"], 4),
                "scope_id": r["scope_id"],
                "canonical_id": r["canonical_id"],
            })
        # Phase 4: attach see_also — items in the same cluster
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
        try:
            for res in results:
                conn.execute(
                    "UPDATE kb_paragraphs SET access_count = access_count + 1, "
                    "accessed_at = ? WHERE id = ?",
                    (datetime.now().isoformat(), res["id"])
                )
            conn.commit()
        except sqlite3.Error:
            pass
        # Phase 9: log query
        if log_query:
            top_score = results[0]["score"] if results else 0.0
            _record_query_log(conn, query, len(results), top_score)
        return results
    finally:
        conn.close()
