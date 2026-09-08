"""Cross-project global knowledge base.

Stores project-agnostic knowledge (principles, debugging methodology,
tool usage) at ~/.code2database_global_kb/ so it can be shared across
all projects. Project KBs reference global entries via the
`global_ref` column.

A separate SQLite db at ~/.code2database_global_kb/global.db holds
the global knowledge; project KBs do a fallback query when the
project-level query returns no matches.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

from _builder.kb.kb_index import _fts5_escape
import logging


def _global_kb_dir() -> str:
    """Return ~/.code2database_global_kb/ — created if missing."""
    home = os.path.expanduser("~")
    d = os.path.join(home, ".code2database_global_kb")
    os.makedirs(d, exist_ok=True)
    return d


def _global_kb_db_path() -> str:
    return os.path.join(_global_kb_dir(), "global.db")


def _global_kb_connect() -> sqlite3.Connection:
    """Open connection to the global KB SQLite db (creates if missing)."""
    db_path = _global_kb_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS kb_global (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            tags TEXT,
            kind TEXT NOT NULL DEFAULT 'principle',
            weight REAL NOT NULL DEFAULT 1.0,
            confidence REAL NOT NULL DEFAULT 1.0,
            source_project TEXT,
            source_file TEXT,
            created_at TEXT NOT NULL,
            accessed_at TEXT,
            access_count INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_kb_global_kind ON kb_global(kind);
        CREATE INDEX IF NOT EXISTS idx_kb_global_weight
            ON kb_global(weight DESC);
        CREATE VIRTUAL TABLE IF NOT EXISTS kb_global_fts USING fts5(
            title, body, tags,
            content='kb_global', content_rowid='id',
            tokenize='porter unicode61'
        );
        CREATE TRIGGER IF NOT EXISTS kb_global_ai AFTER INSERT ON kb_global BEGIN
            INSERT INTO kb_global_fts(rowid, title, body, tags)
            VALUES (new.id, new.title, new.body, COALESCE(new.tags, ''));
        END;
        CREATE TRIGGER IF NOT EXISTS kb_global_ad AFTER DELETE ON kb_global BEGIN
            INSERT INTO kb_global_fts(kb_global_fts, rowid, title, body, tags)
            VALUES ('delete', old.id, old.title, old.body, COALESCE(old.tags, ''));
        END;
        CREATE TRIGGER IF NOT EXISTS kb_global_au AFTER UPDATE ON kb_global BEGIN
            INSERT INTO kb_global_fts(kb_global_fts, rowid, title, body, tags)
            VALUES ('delete', old.id, old.title, old.body, COALESCE(old.tags, ''));
            INSERT INTO kb_global_fts(rowid, title, body, tags)
            VALUES (new.id, new.title, new.body, COALESCE(new.tags, ''));
        END;
    """)
    return conn


def global_add(title: str, body: str, tags: List[str] = None,
               kind: str = "principle", source_project: str = "",
               source_file: str = "", weight: float = 1.0,
               confidence: float = 1.0) -> int:
    """Add an entry to the global KB. Returns the entry id."""
    conn = _global_kb_connect()
    try:
        tags_json = json.dumps(tags, ensure_ascii=False) if tags else None
        cur = conn.execute(
            "INSERT INTO kb_global (title, body, tags, kind, weight, "
            "confidence, source_project, source_file, created_at, access_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (title, body, tags_json, kind, weight, confidence,
             source_project, source_file, datetime.now().isoformat())
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def global_search(query: str, top_n: int = 10, kinds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Search the global KB by FTS5 + BM25."""
    conn = _global_kb_connect()
    try:
        match_expr = _fts5_escape(query)
        sql = (
            "SELECT kb_global.id, kb_global.title, kb_global.body, "
            "       kb_global.tags, kb_global.kind, kb_global.weight, "
            "       kb_global.confidence, kb_global.source_project, "
            "       -bm25(kb_global_fts) AS score "
            "FROM kb_global_fts JOIN kb_global ON kb_global.id = kb_global_fts.rowid "
            "WHERE kb_global_fts MATCH ? "
        )
        params: list = [match_expr]
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            sql += f"  AND kb_global.kind IN ({placeholders}) "
            params.extend(kinds)
        sql += "ORDER BY score DESC LIMIT ?"
        params.append(top_n)
        rows = conn.execute(sql, params).fetchall()
        results = []
        for r in rows:
            tags = r["tags"]
            if tags:
                try:
                    tags = json.loads(tags)
                except (json.JSONDecodeError, TypeError):
                    tags = []
            results.append({
                "id": r["id"],
                "title": r["title"],
                "body": r["body"],
                "tags": tags or [],
                "kind": r["kind"],
                "weight": round(r["weight"], 4),
                "confidence": round(r["confidence"], 4),
                "source_project": r["source_project"] or "",
                "score": round(r["score"], 4),
                "global": True,
            })
        # Best-effort access_count bump
        try:
            for r in results:
                conn.execute(
                    "UPDATE kb_global SET access_count = access_count + 1, "
                    "accessed_at = ? WHERE id = ?",
                    (datetime.now().isoformat(), r["id"])
                )
            conn.commit()
        except sqlite3.Error:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        return results
    finally:
        conn.close()


def global_share(output_path: str) -> str:
    """Export the global KB to a portable JSON file for sharing.

    S1: rejects paths containing '..' (path traversal) and requires
    either an absolute path or a path under the current working directory.
    """
    # S1: path traversal check — resolve relative paths against CWD and
    # reject '..' components (via normpath comparison), without rejecting
    # legitimate absolute paths outside CWD (e.g., a tempfile export dir).
    # The old substring '..' check false-positived on 'file..name.json';
    # the realpath+startswith(CWD) variant wrongly rejected absolute paths.
    if not os.path.isabs(output_path):
        _resolved = os.path.realpath(os.path.join(os.getcwd(), output_path))
    else:
        _resolved = os.path.realpath(output_path)
    _base = os.path.realpath(os.getcwd())
    _rel = os.path.relpath(_resolved, _base)
    if _rel.startswith("..") and os.path.isabs(os.path.normpath(output_path)) is False:
        print(f"[kb-global-share] rejecting relative path escaping CWD: {output_path}",
              file=sys.stderr)
        return ""
    output_path = _resolved
    # Ensure parent dir exists
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    conn = _global_kb_connect()
    try:
        rows = conn.execute(
            "SELECT id, title, body, tags, kind, weight, confidence, "
            "source_project, source_file, created_at FROM kb_global "
            "ORDER BY id"
        ).fetchall()
        entries = []
        for r in rows:
            tags = r["tags"]
            if tags:
                try:
                    tags = json.loads(tags)
                except (json.JSONDecodeError, TypeError):
                    tags = []
            entries.append({
                "id": r["id"],
                "title": r["title"],
                "body": r["body"],
                "tags": tags or [],
                "kind": r["kind"],
                "weight": round(r["weight"], 4),
                "confidence": round(r["confidence"], 4),
                "source_project": r["source_project"] or "",
                "source_file": r["source_file"] or "",
                "created_at": r["created_at"],
            })
        Path(output_path).write_text(
            json.dumps({"format": "code2database_global_kb",
                        "version": 1,
                        "exported_at": datetime.now().isoformat(),
                        "entries": entries},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        return output_path
    finally:
        conn.close()


def global_import(input_path: str) -> int:
    """Import a global KB JSON file (e.g., shared by a teammate).

    Defensive against JSON bombing: rejects files > 50MB.
    S2: also guards against deeply nested JSON (RecursionError).
    """
    # Size guard against JSON bombing (deeply nested or huge files)
    try:
        size = os.path.getsize(input_path)
        if size > 50 * 1024 * 1024:  # 50 MB
            print(f"[kb-global-import] rejecting {input_path}: "
                  f"file too large ({size} bytes > 50MB limit)",
                  file=sys.stderr)
            return 0
    except OSError:
        return 0
    try:
        # S2: guard against deeply nested JSON (stack overflow)
        data = json.loads(Path(input_path).read_text(encoding="utf-8"))
    except RecursionError:
        print(f"[kb-global-import] rejecting {input_path}: "
              f"JSON nesting too deep (possible DoS)",
              file=sys.stderr)
        return 0
    except (OSError, json.JSONDecodeError) as e:
        print(f"[kb-global-import] failed to parse {input_path}: {e}",
              file=sys.stderr)
        return 0
    entries = data.get("entries", []) if isinstance(data, dict) else []
    imported = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        # Per-entry size guard: skip overly large single entries
        body = e.get("body", "")
        if isinstance(body, str) and len(body) > 100_000:
            continue
        global_add(
            title=e.get("title", ""),
            body=body,
            tags=e.get("tags", []),
            kind=e.get("kind", "principle"),
            source_project=e.get("source_project", ""),
            source_file=e.get("source_file", ""),
            weight=e.get("weight", 1.0),
            confidence=e.get("confidence", 1.0),
        )
        imported += 1
    return imported


def query_with_global_fallback(graph_dir: str, query: str,
                                top_n: int = 10) -> List[Dict[str, Any]]:
    """Query project KB first; on no matches, fall back to global KB.

    Marks results with `global: True` so the caller can distinguish.
    """
    from _builder.kb.kb_index import query_kb
    results = query_kb(graph_dir, query, top_n=top_n)
    if results:
        return results
    # Fallback to global KB
    return global_search(query, top_n=top_n)


# ---------------------------------------------------------------------------
# Cross-project Memory Q&A sharing (kind='memory_qa')
# ---------------------------------------------------------------------------

def global_share_memory(graph_dir: str, min_weight: float = 1.0,
                        top_n: int = 50,
                        source_project: str = "") -> int:
    """Export high-weight project memory entries to the global KB.

    Each qualifying memory entry is stored in the global KB with
    kind='memory_qa', enabling cross-project veteran-experience
    sharing: if project A solved 'deadlock pattern X', project B can
    find it via global_search_memory().

    Args:
        graph_dir: project graph directory (contains memory/memory.db)
        min_weight: only export entries with weight >= this threshold
        top_n: max entries to export (by weight descending)
        source_project: project name tag (auto-detected if empty)

    Returns the number of entries exported.
    """
    import sqlite3 as _sqlite3
    mem_db = os.path.join(graph_dir, "memory", "memory.db")
    if not os.path.exists(mem_db):
        return 0
    if not source_project:
        master_path = os.path.join(graph_dir, "code2database_master.json")
        if os.path.exists(master_path):
            try:
                master = json.loads(Path(master_path).read_text(encoding="utf-8"))
                source_project = master.get("project_name", "") or \
                    os.path.basename(master.get("source_root", "") or "") or \
                    os.path.basename(graph_dir.rstrip("/"))
            except (json.JSONDecodeError, OSError):
                pass
        if not source_project:
            source_project = os.path.basename(graph_dir.rstrip("/"))

    conn = _sqlite3.connect(mem_db, timeout=5)
    exported = 0
    try:
        rows = conn.execute(
            "SELECT id, question, answer, tags, weight, author, category_id "
            "FROM memories WHERE status = 'active' AND weight >= ? "
            "ORDER BY weight DESC LIMIT ?",
            (min_weight, top_n)
        ).fetchall()
        # Resolve category paths
        cat_map = {}
        for cr in conn.execute(
                "SELECT id, path FROM categories").fetchall():
            cat_map[cr[0]] = cr[1]
        for r in rows:
            mem_id, question, answer, tags_json, weight, author, cat_id = r
            tags = []
            if tags_json:
                try:
                    tags = json.loads(tags_json)
                except (json.JSONDecodeError, TypeError):
                    tags = []
            category = cat_map.get(cat_id, "") if cat_id else ""
            tag_list = list(tags or [])
            if author and author != "anonymous":
                tag_list.append(f"author:{author}")
            if category:
                tag_list.append(f"cat:{category}")
            global_add(
                title=question,
                body=answer or "",
                tags=tag_list,
                kind="memory_qa",
                source_project=source_project,
                source_file="",
                weight=float(weight),
                confidence=min(1.0, float(weight) / 5.0),
            )
            exported += 1
    except _sqlite3.OperationalError as exc:
        logging.getLogger(__name__).debug(
            "global_share_memory: query failed: %s", exc)
    finally:
        conn.close()
    return exported


def global_search_memory(query: str, top_n: int = 10) -> List[Dict[str, Any]]:
    """Search the global KB for cross-project memory Q&A entries only."""
    return global_search(query, top_n=top_n, kinds=["memory_qa"])


def global_import_memory(graph_dir: str, query: str, top_n: int = 5,
                         auto_merge: bool = True) -> Dict[str, Any]:
    """Import similar Q&A from the global KB into project memory.

    Searches the global KB for kind='memory_qa' entries matching the
    query. For each result:
    - If the project memory already has a similar entry (question
      similarity >= 0.7), and auto_merge is True, the global answer is
      merged as a variant if it's stronger (higher weight).
    - If no similar entry exists, the global Q&A is imported as a new
      memory entry tagged with 'global_import' + source_project.

    Returns {imported: N, merged: N, skipped: N, details: [...]}.
    """
    from _builder.memory.memory_store import MemoryStore
    from _builder.utils import _similarity_score, _simple_tokenize

    results = global_search_memory(query, top_n=top_n)
    if not results:
        return {"imported": 0, "merged": 0, "skipped": 0, "details": []}

    store = MemoryStore(graph_dir)
    imported = 0
    merged = 0
    skipped = 0
    details = []

    for r in results:
        question = r.get("title", "")
        answer = r.get("body", "")
        source_project = r.get("source_project", "global")
        weight = r.get("weight", 1.0)
        tags = r.get("tags", [])
        if not question:
            skipped += 1
            continue

        # Check for similar existing memory in project
        existing = store.search(question, top_n=1)
        is_similar = False
        if existing:
            sim = _similarity_score(
                _simple_tokenize(question),
                _simple_tokenize(existing[0].get("question", "")),
            )
            if sim >= 0.7:
                is_similar = True

        if is_similar and auto_merge:
            # Merge as a variant — the store.add() merge path handles
            # root/leaf logic automatically
            entry_id = store.add(
                question=question,
                answer=answer,
                tags=tags + ["global_import", f"from:{source_project}"],
                category=f"global/{source_project}",
                author="global_import",
                no_merge=False,
            )
            merged += 1
            details.append({
                "action": "merged", "id": entry_id,
                "question": question[:60],
                "source_project": source_project,
            })
        elif not is_similar:
            entry_id = store.add(
                question=question,
                answer=answer,
                tags=tags + ["global_import", f"from:{source_project}"],
                category=f"global/{source_project}",
                author="global_import",
                no_merge=True,
            )
            imported += 1
            details.append({
                "action": "imported", "id": entry_id,
                "question": question[:60],
                "source_project": source_project,
            })
        else:
            skipped += 1
            details.append({
                "action": "skipped", "question": question[:60],
                "reason": "similar entry exists, auto_merge=False",
            })

    return {
        "imported": imported,
        "merged": merged,
        "skipped": skipped,
        "details": details,
    }
