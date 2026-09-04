#!/usr/bin/env python3
"""SQLite-backed layered memory store for Code2Database.

Memory is the LLM's OWN persistent brain — and it is big and messy by
design: many people work on the same project and ask questions at many
different depths. That demands scalable retrieval (FTS5 + BM25), a
hierarchical category tree for indexing (e.g. bdev/nvme/pcie), and
governance operations (split an over-broad entry, merge duplicates,
move between categories) — not flat JSON files.

Storage: graph_dir/memory/memory.db (independent of the graph db, so a
graph rebuild never touches accumulated memory).

Schema:
    categories(id, parent_id, name, path UNIQUE, description)
    memories(id, question, answer, category_id, status, tags, node_ids,
             chains, knowledge_refs, author, root_id, merged_into,
             split_from, merged_count, access_count, weight, boost,
             versions_json, promoted_from, param_bindings, created,
             last_accessed, validated_at, invalidated_reason, archived_at)
    memories_fts(question, answer, tags)  -- FTS5, trigger-maintained

Status lifecycle:
    active → experience   (weight decayed < 0.1, or node_ids invalidated)
    active → split        (governance: entry was too broad)
    active → merged       (governance: absorbed into canonical entry)

Concurrency: WAL journal + busy_timeout for cross-process readers; a
flock (memory.lock) serializes multi-step read-modify-write cycles —
same protocol the JSON store used, so the WSL1 no-multi-process-sqlite
limitation is irrelevant (all writers go through the flock).
"""

import json
import math
import os
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

try:
    import fcntl  # POSIX only; gracefully degrade on non-POSIX
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

import logging

from _builder.utils import _simple_tokenize, _similarity_score, _has_cjk

# Weight formula constants — unchanged from the JSON store so behavior
# carries over exactly.
DECAY_LAMBDA = 0.05        # ~14 days to 50% weight
MERGE_BONUS = 0.10         # +0.10 per merge
LENGTH_BONUS_FACTOR = 0.05  # +0.05 per 1000 chars answer length
ACCESS_BONUS = 0.10        # +0.10 per access
MERGE_SIMILARITY_THRESHOLD = 0.7
WEIGHT_CAP = 10.0

MEMORY_SCHEMA_VERSION = 1

# Fields correct() may touch (DB columns, not free-form like the old
# JSON store where any key could be added).
CORRECTABLE_FIELDS = ("question", "answer", "author")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES categories(id),
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    answer TEXT DEFAULT '',
    category_id INTEGER REFERENCES categories(id),
    status TEXT NOT NULL DEFAULT 'active',
    tags TEXT DEFAULT '[]',
    node_ids TEXT DEFAULT '[]',
    chains TEXT DEFAULT '[]',
    knowledge_refs TEXT DEFAULT '[]',
    symbols TEXT DEFAULT '[]',
    author TEXT DEFAULT '',
    root_id INTEGER DEFAULT 0,
    merged_into INTEGER DEFAULT 0,
    split_from INTEGER DEFAULT 0,
    merged_count INTEGER DEFAULT 0,
    reshaped_count INTEGER DEFAULT 0,
    access_count INTEGER DEFAULT 0,
    weight REAL DEFAULT 1.0,
    boost REAL DEFAULT 0.0,
    versions_json TEXT DEFAULT '[]',
    promoted_from TEXT DEFAULT '',
    param_bindings TEXT DEFAULT '{}',
    created TEXT NOT NULL,
    last_accessed TEXT NOT NULL,
    validated_at TEXT NOT NULL,
    invalidated_reason TEXT DEFAULT '',
    invalidated_at TEXT DEFAULT '',
    archived_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category_id);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_memories_root ON memories(root_id)
    WHERE root_id != 0;
CREATE INDEX IF NOT EXISTS idx_memories_merged_into ON memories(merged_into)
    WHERE merged_into != 0;
CREATE INDEX IF NOT EXISTS idx_memories_split_from ON memories(split_from)
    WHERE split_from != 0;
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    question, answer, tags,
    content='memories', content_rowid='id',
    tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, question, answer, tags)
    VALUES (new.id, new.question, new.answer, COALESCE(new.tags, '[]'));
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, question, answer, tags)
    VALUES ('delete', old.id, old.question, old.answer, COALESCE(old.tags, '[]'));
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, question, answer, tags)
    VALUES ('delete', old.id, old.question, old.answer, COALESCE(old.tags, '[]'));
    INSERT INTO memories_fts(rowid, question, answer, tags)
    VALUES (new.id, new.question, new.answer, COALESCE(new.tags, '[]'));
END;
"""


@contextmanager
def _memory_lock(mem_dir: str, timeout: float = 10.0):
    """Exclusive flock around a whole memory RMW cycle.

    Same contract as the JSON store's lock: guards load -> mutate ->
    save sequences so concurrent adds never reuse ids or lose writes.
    Works across processes AND threads (flock conflicts are per
    open-file-description). Degrades to no locking on non-POSIX.
    """
    if not _HAS_FCNTL:
        yield
        return
    lock_path = os.path.join(mem_dir, "memory.lock")
    fd = open(lock_path, "a+")
    try:
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.time() >= deadline:
                    raise TimeoutError(
                        "could not acquire memory lock within "
                        f"{timeout}s — another memory operation holds it")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


def _fts5_escape(query: str) -> str:
    """Escape a free-form query for FTS5 MATCH (same policy as kb_index)."""
    tokens = re.findall(r'[A-Za-z0-9_]+', query)
    if not tokens:
        return '""'
    return " ".join(f'"{t}"' for t in tokens)


class MemoryStore:
    """SQLite-backed persistent memory with a hierarchical category tree."""

    def __init__(self, graph_dir: str, read_only: bool = False):
        self.graph_dir = graph_dir
        self.mem_dir = os.path.join(graph_dir, "memory")
        self.db_path = os.path.join(self.mem_dir, "memory.db")
        self.scratch_dir = os.path.join(graph_dir, ".scratch")
        self.read_only = read_only
        if read_only:
            # Read-only sharing (multi-user viewing): never create
            # directories, never init/patch the schema, never bump
            # access counters. Connections open mode=ro where possible
            # so a viewer on a read-only mount still gets full query
            # access without any risk of mutating the shared store.
            return
        os.makedirs(self.mem_dir, exist_ok=True)
        os.makedirs(self.scratch_dir, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            try:
                conn = sqlite3.connect(
                    f"file:{self.db_path}?mode=ro", uri=True,
                    timeout=10.0)
            except sqlite3.OperationalError:
                # e.g. WAL leftovers the reader cannot mmap — degrade
                # to a normal connection; the API layer still never
                # issues writes in read_only mode.
                conn = sqlite3.connect(self.db_path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            return conn
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self):
        with _memory_lock(self.mem_dir):
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                # Migration: pre-Round-24 stores lack the symbols
                # column (memory↔symbol grounding). ALTER is cheap and
                # idempotent-guarded via table_info.
                cols = {r[1] for r in
                        conn.execute("PRAGMA table_info(memories)")}
                if "symbols" not in cols:
                    conn.execute(
                        "ALTER TABLE memories "
                        "ADD COLUMN symbols TEXT DEFAULT '[]'")
                conn.execute(f"PRAGMA user_version = {MEMORY_SCHEMA_VERSION}")
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Row <-> dict conversion
    # ------------------------------------------------------------------

    _JSON_COLUMNS = ("tags", "node_ids", "chains", "knowledge_refs",
                     "symbols")

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        for col in MemoryStore._JSON_COLUMNS:
            val = d.get(col, "[]")
            if val is None:
                # pre-Round-24 read-only DBs lack the symbols column —
                # every JSON list column degrades to []
                d[col] = []
            elif isinstance(val, str):
                try:
                    d[col] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    d[col] = []
            elif not isinstance(val, list):
                d[col] = []
        if isinstance(d.get("versions_json"), str):
            try:
                d["versions"] = json.loads(d["versions_json"])
            except (json.JSONDecodeError, TypeError):
                d["versions"] = []
        else:
            d["versions"] = d.get("versions_json") or []
        d.pop("versions_json", None)
        if isinstance(d.get("param_bindings"), str):
            try:
                d["param_bindings"] = json.loads(d["param_bindings"])
            except (json.JSONDecodeError, TypeError):
                d["param_bindings"] = {}
        return d

    @staticmethod
    def _pack_versions(entry: dict) -> str:
        return json.dumps(entry.get("versions", []), ensure_ascii=False)

    # ------------------------------------------------------------------
    # Weight formula (unchanged from the JSON store)
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_weight(entry: dict) -> float:
        base = 1.0
        created = entry.get("created", "")
        last_access = entry.get("last_accessed", entry.get("validated_at", created))
        now = time.time()
        try:
            if last_access:
                dt = datetime.fromisoformat(last_access)
                days = (now - dt.timestamp()) / 86400
            else:
                days = 365
        except (ValueError, OSError):
            days = 365
        recency = math.exp(-DECAY_LAMBDA * max(0, days))
        merged_count = entry.get("merged_count", 0)
        answer_len = len(entry.get("answer", "") or "")
        importance = 1.0 + merged_count * MERGE_BONUS \
            + (answer_len / 1000) * LENGTH_BONUS_FACTOR
        access_count = entry.get("access_count", 0)
        access = 1.0 + ACCESS_BONUS * access_count
        boost = entry.get("boost", 0.0)
        weight = base * recency * importance * access * (1.0 + boost)
        return min(weight, WEIGHT_CAP)

    def _update_weight(self, entry: dict) -> dict:
        entry["weight"] = round(self._compute_weight(entry), 4)
        return entry

    # ------------------------------------------------------------------
    # Category tree
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_category(path: Optional[str]) -> str:
        if not path or not str(path).strip():
            return "uncategorized"
        path = str(path).strip().strip("/")
        path = re.sub(r'/+', '/', path)
        return path or "uncategorized"

    def ensure_category(self, path: Optional[str], description: str = "") -> int:
        """Get or create the category for a materialized path.

        Missing intermediate levels are created automatically
        ('bdev/nvme/pcie' creates bdev and bdev/nvme on the way).
        Returns the category id. Assumes caller holds the memory lock
        (or is the only writer).
        """
        path = self._normalize_category(path)
        conn = self._connect()
        try:
            parent_id = None
            accumulated = ""
            for segment in path.split("/"):
                accumulated = segment if not accumulated \
                    else f"{accumulated}/{segment}"
                row = conn.execute(
                    "SELECT id FROM categories WHERE path = ?",
                    (accumulated,)).fetchone()
                if row is not None:
                    parent_id = row["id"]
                    continue
                cur = conn.execute(
                    "INSERT INTO categories (parent_id, name, path, "
                    "description, created_at) VALUES (?, ?, ?, ?, ?)",
                    (parent_id, segment, accumulated, description,
                     datetime.now().isoformat()))
                parent_id = cur.lastrowid
            conn.commit()
            return parent_id
        finally:
            conn.close()

    def category_id(self, path: Optional[str]) -> Optional[int]:
        path = self._normalize_category(path)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id FROM categories WHERE path = ?", (path,)).fetchone()
            return row["id"] if row else None
        finally:
            conn.close()

    def categories(self) -> List[dict]:
        """Return the category tree with direct + subtree memory counts.

        Only active memories are counted (tombstones don't organize).
        """
        conn = self._connect()
        try:
            cats = [dict(r) for r in conn.execute(
                "SELECT id, parent_id, name, path, description "
                "FROM categories ORDER BY path").fetchall()]
            direct = {r["category_id"]: r["c"] for r in conn.execute(
                "SELECT category_id, COUNT(*) AS c FROM memories "
                "WHERE status = 'active' GROUP BY category_id").fetchall()}
        finally:
            conn.close()
        for c in cats:
            c["count"] = direct.get(c["id"], 0)
        by_id = {c["id"]: c for c in cats}
        roots: List[dict] = []
        for c in cats:
            c["children"] = []
            if c["parent_id"] is not None and c["parent_id"] in by_id:
                by_id[c["parent_id"]]["children"].append(c)
            else:
                roots.append(c)
        # Subtree counts: a category's subtree includes everything under
        # its path prefix.
        for c in cats:
            c["subtree_count"] = sum(
                s["count"] for s in cats
                if s["path"] == c["path"] or s["path"].startswith(c["path"] + "/"))
        return roots

    def _category_filter_sql(self, category_prefix: Optional[str],
                             params: list) -> str:
        """SQL fragment restricting memories to a category subtree."""
        if not category_prefix:
            return ""
        prefix = self._normalize_category(category_prefix)
        params.extend([prefix, prefix + "/%"])
        return (" AND m.category_id IN (SELECT id FROM categories "
                "WHERE path = ? OR path LIKE ?) ")

    # ------------------------------------------------------------------
    # Public API: add
    # ------------------------------------------------------------------

    def add(self, question: str, answer: str = "", tags: list = None,
            node_ids: list = None, chains: list = None,
            category: str = None, author: str = "",
            no_merge: bool = False, symbols: list = None) -> int:
        """Add a memory entry. Returns the new entry id.

        Similarity merge: if an existing active memory's question is
        >= MERGE_SIMILARITY_THRESHOLD similar, the new entry is stored
        as a variant (root_id points at the cluster root) and the root
        absorbs tags/node_ids/symbols; a strictly stronger answer
        replaces the root's (with a version record) — same semantics
        as the old JSON root/leaf merge.

        symbols: graph symbol names this experience is about (e.g.
        ["nvme_submit_cmd"]) — grounds memories to code so the UI can
        show veteran Q&A on the symbol's page.
        """
        tags = sorted(set(tags or []))
        node_ids = list(node_ids or [])
        chains = list(chains or [])
        symbols = sorted({str(s).strip() for s in (symbols or [])
                          if str(s).strip()})
        now = datetime.now().isoformat()

        with _memory_lock(self.mem_dir):
            conn = self._connect()
            try:
                cat_id = self.ensure_category(category)
                entry = {
                    "question": question, "answer": answer,
                    "merged_count": 0, "access_count": 0,
                    "boost": 0.0, "created": now, "last_accessed": now,
                    "validated_at": now, "answer_len": len(answer or ""),
                }
                entry["weight"] = round(self._compute_weight(entry), 4)

                root_id = 0
                if not no_merge:
                    root_id = self._find_root_match(conn, question)

                cur = conn.execute(
                    "INSERT INTO memories (question, answer, category_id, "
                    "status, tags, node_ids, chains, knowledge_refs, "
                    "symbols, author, "
                    "root_id, merged_count, access_count, weight, boost, "
                    "versions_json, created, last_accessed, validated_at) "
                    "VALUES (?, ?, ?, 'active', ?, ?, ?, '[]', ?, ?, "
                    "?, 0, 0, ?, 0.0, '[]', ?, ?, ?)",
                    (question, answer, cat_id,
                     json.dumps(tags, ensure_ascii=False),
                     json.dumps(node_ids, ensure_ascii=False),
                     json.dumps(chains, ensure_ascii=False),
                     json.dumps(symbols, ensure_ascii=False),
                     author, root_id, entry["weight"], now, now, now))
                new_id = cur.lastrowid

                if root_id:
                    self._merge_into_root(conn, root_id, new_id, entry,
                                          tags, node_ids, chains, symbols)
                    print(f"Merged with root #{root_id} as variant #{new_id}")
                else:
                    conn.execute(
                        "UPDATE memories SET root_id = ? WHERE id = ?",
                        (new_id, new_id))
                    print(f"Created memory #{new_id} (root)")
                conn.commit()
                return new_id
            finally:
                conn.close()

    def _find_root_match(self, conn: sqlite3.Connection,
                         question: str) -> int:
        """Find the cluster root for a similar question. 0 = no match."""
        return self._best_match(conn, question)[0]

    def _best_match(self, conn: sqlite3.Connection,
                    question: str) -> tuple:
        """Best active match for a question.

        Returns (root_id, score, matched_question); root_id 0 / score
        0.0 when nothing clears MERGE_SIMILARITY_THRESHOLD. Shared by
        the merge-on-add path and correct_similar (correction needs
        the score + matched text to report what it corrected).
        """
        q_tokens = _simple_tokenize(question)
        if not q_tokens:
            return 0, 0.0, None
        rows = conn.execute(
            "SELECT id, root_id, question, tags FROM memories "
            "WHERE status = 'active'").fetchall()
        best_id = 0
        best_score = 0.0
        best_q = None
        for r in rows:
            e_tokens = _simple_tokenize(r["question"])
            try:
                tags = json.loads(r["tags"] or "[]")
            except (json.JSONDecodeError, TypeError):
                tags = []
            for tag in tags:
                e_tokens |= _simple_tokenize(tag)
            score = _similarity_score(q_tokens, e_tokens)
            if score > best_score and score >= MERGE_SIMILARITY_THRESHOLD:
                best_score = score
                best_id = r["root_id"] or r["id"]
                best_q = r["question"]
        return best_id, best_score, best_q

    def _merge_into_root(self, conn: sqlite3.Connection, root_id: int,
                         variant_id: int, variant: dict, tags: list,
                         node_ids: list, chains: list, symbols: list = None):
        root = self._get_row(conn, root_id)
        if root is None or root["status"] != "active":
            return
        root_versions = json.loads(root["versions_json"] or "[]")
        root_versions.append({
            "answer": root["answer"], "created": root["created"],
            "merged_from": variant_id,
            "version": len(root_versions) + 1,
        })
        new_tags = sorted(set(root["tags_list"]) | set(tags))
        new_nodes = sorted(set(root["node_ids_list"]) | set(node_ids))
        new_chains = list(root["chains_list"]) + list(chains)
        new_symbols = sorted(set(root.get("symbols_list") or []) |
                             set(symbols or []))

        # Stronger answer replaces the root's (weight comparison, not
        # "any non-empty answer wins").
        answer = root["answer"]
        if variant.get("weight", 0.0) > root["weight"]:
            answer = variant.get("answer", answer)

        merged_entry = {
            "merged_count": root["merged_count"] + 1,
            "access_count": root["access_count"],
            "boost": root["boost"],
            "created": root["created"],
            "last_accessed": root["last_accessed"],
            "answer": answer or "",
        }
        new_weight = round(self._compute_weight(merged_entry), 4)
        conn.execute(
            "UPDATE memories SET answer = ?, tags = ?, node_ids = ?, "
            "chains = ?, symbols = ?, merged_count = ?, weight = ?, "
            "versions_json = ? WHERE id = ?",
            (answer,
             json.dumps(new_tags, ensure_ascii=False),
             json.dumps(new_nodes, ensure_ascii=False),
             json.dumps(new_chains, ensure_ascii=False),
             json.dumps(new_symbols, ensure_ascii=False),
             root["merged_count"] + 1, new_weight,
             json.dumps(root_versions, ensure_ascii=False), root_id))

    def _get_row(self, conn: sqlite3.Connection, mem_id: int) -> Optional[dict]:
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        for col in ("tags", "node_ids", "chains", "symbols"):
            try:
                d[col + "_list"] = json.loads(d.get(col) or "[]")
            except (json.JSONDecodeError, TypeError):
                d[col + "_list"] = []
        return d

    # ------------------------------------------------------------------
    # Public API: search
    # ------------------------------------------------------------------

    def search(self, query: str, top_n: int = 10,
               category: str = None, tags: List[str] = None,
               author: str = None, include_experience: bool = False,
               min_weight: float = 0.0,
               symbol: str = None) -> List[dict]:
        """Search memories via FTS5 BM25 × weight, with filters.

        Results are grouped by similarity cluster (root_id): the
        best-scoring member of each cluster is returned with a
        variant_count, so one popular Q&A can't flood the whole result
        list. CJK-aware: the FTS5 pass only sees Latin tokens (the
        unicode61 tokenizer folds each CJK run into one token), so for
        queries containing CJK the token-set similarity pass always
        runs alongside and both passes are merged by entry id (best
        score wins); pure-Latin queries use FTS5 alone and fall back
        to similarity only when FTS5 has no token overlap. Access
        counters of returned rows are bumped.
        """
        if not query or not query.strip():
            return []
        statuses = ("active", "experience") if include_experience \
            else ("active",)
        ph = ",".join("?" for _ in statuses)

        # --- FTS5 pass ---
        scored: List[dict] = []
        params: list = []
        cat_sql = self._category_filter_sql(category, params)
        author_sql = " AND m.author = ? " if author else ""
        fts_rows = []
        try:
            sql = (
                "SELECT m.*, -bm25(memories_fts) * "
                "(0.5 + 0.5 * MIN(m.weight / 2.0, 1.0)) AS score "
                "FROM memories_fts "
                "JOIN memories m ON m.id = memories_fts.rowid "
                "WHERE memories_fts MATCH ? "
                f"AND m.status IN ({ph}) "
                "AND m.weight >= ? "
                + cat_sql + author_sql +
                "ORDER BY score DESC LIMIT ?"
            )
            fts_params = [_fts5_escape(query)] + list(statuses) \
                + [min_weight] + params
            if author:
                fts_params.append(author)
            fts_params.append(max(top_n * 3, 30))
            conn = self._connect()
            try:
                fts_rows = conn.execute(sql, fts_params).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            fts_rows = []

        if fts_rows:
            for r in fts_rows:
                scored.append((r["score"], self._row_to_dict(r)))
        if not fts_rows or _has_cjk(query):
            # --- similarity pass (fallback for pure-Latin queries,
            # always-on merge channel for CJK queries: the FTS5 pass
            # above only ranks the Latin tokens of a mixed query) ---
            params2: list = []
            cat_sql2 = self._category_filter_sql(category, params2)
            author_sql2 = " AND m.author = ? " if author else ""
            sql = (
                "SELECT * FROM memories m WHERE m.status IN "
                f"({ph}) AND m.weight >= ? " + cat_sql2 + author_sql2
            )
            q_params = list(statuses) + [min_weight] + params2
            if author:
                q_params.append(author)
            conn = self._connect()
            try:
                rows = conn.execute(sql, q_params).fetchall()
            finally:
                conn.close()
            q_tokens = _simple_tokenize(query)
            for r in rows:
                e_tokens = _simple_tokenize(r["question"])
                try:
                    rtags = json.loads(r["tags"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    rtags = []
                for tag in rtags:
                    e_tokens |= _simple_tokenize(tag)
                try:
                    rsyms = json.loads(r["symbols"] or "[]") \
                        if "symbols" in r.keys() else []
                except (json.JSONDecodeError, TypeError):
                    rsyms = []
                for sym in rsyms:
                    e_tokens |= _simple_tokenize(sym)
                sim = _similarity_score(q_tokens, e_tokens)
                if sim > 0:
                    combined = sim * (0.5 + 0.5 * min(r["weight"] / 2.0, 1.0))
                    scored.append((combined, self._row_to_dict(r)))

        # --- merge the two passes by entry id (best score wins) ---
        merged: dict = {}
        for score, entry in scored:
            mid = entry.get("id")
            if mid not in merged or score > merged[mid][0]:
                merged[mid] = (score, entry)
        scored = list(merged.values())

        # --- tag post-filter (ALL requested tags must be present) ---
        if tags:
            wanted = {t.strip().lower() for t in tags if t.strip()}
            scored = [
                (s, e) for s, e in scored
                if wanted.issubset({t.lower() for t in e.get("tags", [])})
            ]

        # --- symbol post-filter (exact, case-insensitive) ---
        if symbol:
            wanted_sym = symbol.strip().lower()
            scored = [
                (s, e) for s, e in scored
                if wanted_sym in {str(x).lower()
                                  for x in e.get("symbols") or []}
            ]

        scored.sort(key=lambda x: -x[0])

        # --- group by cluster root ---
        results: List[dict] = []
        seen_roots = set()
        cat_paths = self._category_paths()
        for score, entry in scored:
            root = entry.get("root_id") or entry["id"]
            if root in seen_roots:
                # Count as a variant of the already-returned root
                for res in results:
                    if res["root_id"] == root:
                        res["variant_count"] += 1
                        break
                continue
            seen_roots.add(root)
            results.append({
                "id": entry["id"],
                "root_id": root,
                "score": round(score, 4),
                "weight": round(entry.get("weight", 1.0), 4),
                "question": entry["question"],
                "answer": entry.get("answer", ""),
                "tags": entry.get("tags", []),
                "symbols": entry.get("symbols") or [],
                "category": cat_paths.get(entry.get("category_id"), ""),
                "author": entry.get("author", ""),
                "status": entry.get("status", "active"),
                "variant_count": 0,
            })
            if len(results) >= top_n:
                break

        # --- bump access counters for returned rows (best-effort) ---
        if results and not self.read_only:
            now = datetime.now().isoformat()
            try:
                with _memory_lock(self.mem_dir):
                    conn = self._connect()
                    try:
                        for res in results:
                            conn.execute(
                                "UPDATE memories SET access_count = "
                                "access_count + 1, last_accessed = ? "
                                "WHERE id = ?", (now, res["id"]))
                        conn.commit()
                    finally:
                        conn.close()
            except TimeoutError:
                pass  # counter bump is best-effort

        # --- record zero-result searches as known-unknowns ---
        if not results and not self.read_only and query.strip():
            self._log_memory_miss(query)
        return results

    def _log_memory_miss(self, query: str):
        """Record a zero-result memory search into kb_query_log.

        session-init aggregates kb_query_log misses into its
        known-unknowns layer ("questions worth capturing next") —
        without this, misses at the memory layer were invisible to
        that signal. Never creates code2database.db just to log; a
        missing KB db simply means no feedback loop yet.
        """
        db_path = os.path.join(self.graph_dir, "code2database.db")
        if not os.path.exists(db_path):
            return
        try:
            from _builder.kb_index import _kb_connect
            conn = _kb_connect(self.graph_dir)
            if conn is None:
                return
            try:
                conn.execute(
                    "INSERT INTO kb_query_log (query, matched, "
                    "match_count, top_score, queried_at) "
                    "VALUES (?, 0, 0, 0.0, ?)",
                    (query.strip(), datetime.now().isoformat()))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent exception",
                                              exc_info=True)

    def entries_for_symbol(self, symbol: str, top: int = 3) -> List[dict]:
        """Active memories grounded to a graph symbol, by weight.

        The symbol-page view of veteran experience: what the UI shows
        next to a function so a newcomer sees its pitfalls while
        reading the code. Pure read; degrades to [] when the column
        is absent (pre-Round-24 read-only DBs).
        """
        if not symbol or not symbol.strip():
            return []
        wanted = symbol.strip().lower()
        conn = self._connect()
        try:
            if "symbols" in {r[1] for r in
                             conn.execute("PRAGMA table_info(memories)")}:
                rows = conn.execute(
                    "SELECT id, question, answer, author, weight, "
                    "category_id, symbols, root_id FROM memories "
                    "WHERE status = 'active' AND symbols LIKE ? "
                    "ORDER BY weight DESC LIMIT ?",
                    (f'%"{symbol.strip()}"%', top * 5)).fetchall()
            else:
                rows = []
        finally:
            conn.close()
        cat_paths = self._category_paths()
        # group by cluster root (like search): a variant and its root
        # may both carry the symbol — return the root once
        out = []
        seen_roots = set()
        for r in rows:
            try:
                syms = json.loads(r["symbols"] or "[]")
            except (json.JSONDecodeError, TypeError):
                syms = []
            if wanted not in {str(x).lower() for x in syms}:
                continue  # LIKE over-matched (e.g. substring of another)
            entry = {
                "id": r["id"],
                "root_id": r["root_id"] or r["id"],
                "question": r["question"],
                "answer": (r["answer"] or "")[:300],
                "author": r["author"] or "",
                "weight": round(r["weight"], 2),
                "category": cat_paths.get(r["category_id"], ""),
                "symbols": syms,
                "variant_count": 0,
            }
            root = entry["root_id"]
            if root in seen_roots:
                for existing in out:
                    if existing["root_id"] == root:
                        existing["variant_count"] += 1
                        break
                continue
            seen_roots.add(root)
            out.append(entry)
            if len(out) >= top:
                break
        return out

    def _category_paths(self) -> Dict[int, str]:
        conn = self._connect()
        try:
            return {r["id"]: r["path"] for r in conn.execute(
                "SELECT id, path FROM categories").fetchall()}
        finally:
            conn.close()

    def query(self, query_text: str, top_n: int = 5,
              min_weight: float = 0.3) -> List[dict]:
        """Legacy-compatible query (used by the MemoryManager facade)."""
        return self.search(query_text, top_n=top_n, min_weight=min_weight)

    # ------------------------------------------------------------------
    # Public API: get / correct / reshape / promote
    # ------------------------------------------------------------------

    def get(self, mem_id: int) -> Optional[dict]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            conn.close()

    def set_provenance(self, mem_id: int, promoted_from: str = "",
                       param_bindings: dict = None):
        """Attach scratch provenance to a promoted memory."""
        with _memory_lock(self.mem_dir):
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE memories SET promoted_from = ?, "
                    "param_bindings = ? WHERE id = ?",
                    (promoted_from,
                     json.dumps(param_bindings or {}, ensure_ascii=False),
                     mem_id))
                conn.commit()
            finally:
                conn.close()

    def correct(self, mem_id: int, field: str, value: str):
        """Correct a field (question/answer/author), recording a version."""
        if field not in CORRECTABLE_FIELDS:
            raise ValueError(
                f"field must be one of {CORRECTABLE_FIELDS}, got {field!r}")
        with _memory_lock(self.mem_dir):
            conn = self._connect()
            try:
                entry = self._get_row(conn, mem_id)
                if entry is None:
                    print(f"Memory #{mem_id} not found", file=sys.stderr)
                    return
                versions = json.loads(entry["versions_json"] or "[]")
                versions.append({
                    "field": field,
                    "old_value": entry[field],
                    "version": len(versions) + 1,
                    "corrected_at": datetime.now().isoformat(),
                })
                conn.execute(
                    f"UPDATE memories SET {field} = ?, versions_json = ? "
                    "WHERE id = ?",
                    (value, json.dumps(versions, ensure_ascii=False),
                     mem_id))
                conn.commit()
            finally:
                conn.close()
        print(f"Corrected #{mem_id}.{field}")

    def reshape(self, root_id: int, answer: str, corrected_by: str = "",
                symbols: list = None):
        """Replace a root memory's answer with a stronger one.

        symbols: when given (including []), also replace the entry's
        symbol list — a correction may re-ground the memory to the
        right function. None leaves symbols untouched.
        """
        with _memory_lock(self.mem_dir):
            conn = self._connect()
            try:
                entry = self._get_row(conn, root_id)
                if entry is None:
                    print(f"Memory #{root_id} not found", file=sys.stderr)
                    return
                versions = json.loads(entry["versions_json"] or "[]")
                version_record = {
                    "answer": entry["answer"], "version": len(versions) + 1,
                    "reshaped_at": datetime.now().isoformat(),
                }
                if corrected_by:
                    version_record["corrected_by"] = corrected_by
                versions.append(version_record)
                updated = dict(entry)
                updated["answer"] = answer
                updated["merged_count"] = entry["merged_count"]
                updated["reshaped_count"] = entry.get("reshaped_count", 0) + 1
                new_weight = round(self._compute_weight(updated), 4)
                if symbols is None:
                    conn.execute(
                        "UPDATE memories SET answer = ?, reshaped_count = ?, "
                        "weight = ?, versions_json = ? WHERE id = ?",
                        (answer, updated["reshaped_count"], new_weight,
                         json.dumps(versions, ensure_ascii=False), root_id))
                else:
                    norm = sorted({str(s).strip() for s in symbols
                                   if str(s).strip()})
                    conn.execute(
                        "UPDATE memories SET answer = ?, reshaped_count = ?, "
                        "weight = ?, symbols = ?, versions_json = ? "
                        "WHERE id = ?",
                        (answer, updated["reshaped_count"], new_weight,
                         json.dumps(norm, ensure_ascii=False),
                         json.dumps(versions, ensure_ascii=False), root_id))
                conn.commit()
            finally:
                conn.close()
        print(f"Reshaped memory #{root_id}")

    def promote(self, mem_id: int, boost: float = 1.0):
        """Boost a memory's weight (persisted boost survives decay)."""
        with _memory_lock(self.mem_dir):
            conn = self._connect()
            try:
                entry = self._get_row(conn, mem_id)
                if entry is None:
                    print(f"Memory #{mem_id} not found", file=sys.stderr)
                    return
                now = datetime.now().isoformat()
                new_boost = round(entry["boost"] + boost, 4)
                updated = dict(entry)
                updated["boost"] = new_boost
                updated["last_accessed"] = now
                updated["access_count"] = entry["access_count"] + 1
                new_weight = min(round(self._compute_weight(updated), 4),
                                 WEIGHT_CAP)
                conn.execute(
                    "UPDATE memories SET last_accessed = ?, access_count = ?, "
                    "boost = ?, weight = ? WHERE id = ?",
                    (now, updated["access_count"], new_boost, new_weight,
                     mem_id))
                conn.commit()
            finally:
                conn.close()
        print(f"Promoted #{mem_id}")

    def correct_similar(self, question: str, answer: str,
                        author: str = "", symbols: list = None) -> dict:
        """Correct-first save: reshape the best match, don't add a variant.

        The correction half of the correction protocol: when the answer
        to a question was WRONG, re-saving it verbatim would create a
        variant of the wrong root (and the merge path keeps the
        higher-weight — i.e. the wrong — answer on top). This finds
        the most similar active entry and reshapes it in place (old
        answer preserved in the version history), falling back to a
        normal add() when nothing is similar enough.

        Returns {"action": "corrected"|"created", "id", "score",
                 "matched_question"}.
        """
        with _memory_lock(self.mem_dir):
            conn = self._connect()
            try:
                root_id, score, matched_q = self._best_match(conn, question)
            finally:
                conn.close()
        if root_id:
            self.reshape(root_id, answer, corrected_by=author,
                         symbols=symbols)
            return {"action": "corrected", "id": root_id,
                    "score": round(score, 4),
                    "matched_question": matched_q}
        new_id = self.add(question, answer, author=author, symbols=symbols)
        return {"action": "created", "id": new_id, "score": 0.0,
                "matched_question": None}

    # ------------------------------------------------------------------
    # Public API: governance — split / merge / move
    # ------------------------------------------------------------------

    def split(self, mem_id: int, parts: List[dict]) -> List[int]:
        """Split an over-broad memory into focused sub-memories.

        parts: [{question, answer, category?, tags?}, ...] — category
        and tags default to the parent's. The parent becomes a
        'split' tombstone; children carry split_from = parent id and
        inherit node_ids/chains/author. Returns children ids.
        """
        if not parts:
            raise ValueError("split requires at least one part")
        with _memory_lock(self.mem_dir):
            conn = self._connect()
            try:
                parent = self._get_row(conn, mem_id)
                if parent is None:
                    raise ValueError(f"Memory #{mem_id} not found")
                if parent["status"] != "active":
                    raise ValueError(
                        f"Memory #{mem_id} is {parent['status']} — "
                        "only active memories can be split")
                now = datetime.now().isoformat()
                child_ids = []
                for part in parts:
                    q = part.get("question", "").strip()
                    if not q:
                        raise ValueError("each split part needs a question")
                    parent_path = self._path_for_category(
                        conn, parent["category_id"])
                    cat_id = self.ensure_category(
                        part.get("category") or parent_path)
                    part_tags = part.get("tags") or parent["tags_list"]
                    child = {
                        "merged_count": 0, "access_count": 0, "boost": 0.0,
                        "created": now, "last_accessed": now,
                        "validated_at": now, "answer": part.get("answer", ""),
                    }
                    child["weight"] = round(self._compute_weight(child), 4)
                    cur = conn.execute(
                        "INSERT INTO memories (question, answer, category_id, "
                        "status, tags, node_ids, chains, knowledge_refs, "
                        "symbols, author, root_id, split_from, weight, "
                        "created, last_accessed, validated_at) VALUES "
                        "(?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, 0, ?, ?, "
                        "?, ?, ?)",
                        (q, part.get("answer", ""), cat_id,
                         json.dumps(sorted(set(part_tags)),
                                    ensure_ascii=False),
                         parent["node_ids"], parent["chains"],
                         parent["knowledge_refs"],
                         parent.get("symbols") or "[]",
                         parent["author"],
                         mem_id, child["weight"], now, now, now))
                    cid = cur.lastrowid
                    conn.execute(
                        "UPDATE memories SET root_id = ? WHERE id = ?",
                        (cid, cid))
                    child_ids.append(cid)
                versions = json.loads(parent["versions_json"] or "[]")
                versions.append({
                    "split_into": child_ids,
                    "version": len(versions) + 1,
                    "split_at": now,
                })
                conn.execute(
                    "UPDATE memories SET status = 'split', "
                    "versions_json = ? WHERE id = ?",
                    (json.dumps(versions, ensure_ascii=False), mem_id))
                conn.commit()
                print(f"Split #{mem_id} into {len(child_ids)} memories: "
                      f"{child_ids}")
                return child_ids
            finally:
                conn.close()

    def _path_for_category(self, conn: sqlite3.Connection,
                           cat_id: Optional[int]) -> str:
        if cat_id is None:
            return "uncategorized"
        row = conn.execute(
            "SELECT path FROM categories WHERE id = ?", (cat_id,)).fetchone()
        return row["path"] if row else "uncategorized"

    def merge(self, mem_ids: List[int], canonical_id: int = None,
              question: str = None, answer: str = None) -> int:
        """Merge duplicate memories into one canonical entry.

        All other entries become 'merged' tombstones with merged_into =
        canonical id. The canonical absorbs tags/node_ids/chains and
        gains merged_count. Returns the canonical id.
        """
        if len(mem_ids) < 2:
            raise ValueError("merge requires at least two ids")
        with _memory_lock(self.mem_dir):
            conn = self._connect()
            try:
                entries = {}
                for mid in mem_ids:
                    e = self._get_row(conn, mid)
                    if e is None:
                        raise ValueError(f"Memory #{mid} not found")
                    if e["status"] != "active":
                        raise ValueError(
                            f"Memory #{mid} is {e['status']} — only active "
                            "memories can be merged")
                    entries[mid] = e
                canon = canonical_id if canonical_id is not None else mem_ids[0]
                if canon not in entries:
                    raise ValueError(
                        f"canonical id {canon} not among merged ids")
                others = [mid for mid in mem_ids if mid != canon]
                now = datetime.now().isoformat()

                c = entries[canon]
                all_tags = set(c["tags_list"])
                all_nodes = set(c["node_ids_list"])
                all_chains = list(c["chains_list"])
                for mid in others:
                    all_tags |= set(entries[mid]["tags_list"])
                    all_nodes |= set(entries[mid]["node_ids_list"])
                    all_chains += entries[mid]["chains_list"]
                versions = json.loads(c["versions_json"] or "[]")
                versions.append({
                    "merged_ids": others,
                    "version": len(versions) + 1,
                    "merged_at": now,
                })
                updated = dict(c)
                updated["question"] = question or c["question"]
                updated["answer"] = answer if answer is not None else c["answer"]
                updated["merged_count"] = c["merged_count"] + len(others)
                updated["answer_len"] = len(updated["answer"])
                new_weight = round(self._compute_weight(updated), 4)

                conn.execute(
                    "UPDATE memories SET question = ?, answer = ?, tags = ?, "
                    "node_ids = ?, chains = ?, merged_count = ?, weight = ?, "
                    "versions_json = ?, last_accessed = ? WHERE id = ?",
                    (updated["question"], updated["answer"],
                     json.dumps(sorted(all_tags), ensure_ascii=False),
                     json.dumps(sorted(all_nodes), ensure_ascii=False),
                     json.dumps(all_chains, ensure_ascii=False),
                     updated["merged_count"], new_weight,
                     json.dumps(versions, ensure_ascii=False), now, canon))
                for mid in others:
                    conn.execute(
                        "UPDATE memories SET status = 'merged', "
                        "merged_into = ? WHERE id = ?", (canon, mid))
                # Variants of absorbed entries re-point at the canonical.
                for mid in others:
                    conn.execute(
                        "UPDATE memories SET root_id = ? WHERE root_id = ? "
                        "AND id != ? AND status = 'active'",
                        (canon, mid, mid))
                conn.commit()
                print(f"Merged {others} into #{canon}")
                return canon
            finally:
                conn.close()

    def move(self, mem_id: int, new_category: str):
        """Move a memory to another category (created if missing)."""
        with _memory_lock(self.mem_dir):
            conn = self._connect()
            try:
                entry = self._get_row(conn, mem_id)
                if entry is None:
                    print(f"Memory #{mem_id} not found", file=sys.stderr)
                    return
                cat_id = self.ensure_category(new_category)
                conn.execute(
                    "UPDATE memories SET category_id = ? WHERE id = ?",
                    (cat_id, mem_id))
                conn.commit()
            finally:
                conn.close()
        print(f"Moved #{mem_id} → {new_category}")

    # ------------------------------------------------------------------
    # Public API: decay / validate / consolidate
    # ------------------------------------------------------------------

    def decay(self) -> int:
        """Recompute weights; archive <0.1 as experience. Returns count."""
        decayed = 0
        archived = 0
        with _memory_lock(self.mem_dir):
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM memories WHERE status = 'active'"
                ).fetchall()
                for r in rows:
                    entry = self._row_to_dict(r)
                    old_weight = entry["weight"]
                    self._update_weight(entry)
                    new_weight = entry["weight"]
                    update_needed = abs(old_weight - new_weight) > 0.01
                    if new_weight < 0.1:
                        # Archive as experience; if it was a cluster
                        # root, variants become their own roots so
                        # future merges still find a live target.
                        conn.execute(
                            "UPDATE memories SET status = 'experience', "
                            "weight = ?, archived_at = ?, "
                            "invalidated_reason = 'weight decayed below 0.1' "
                            "WHERE id = ?",
                            (new_weight, datetime.now().isoformat(),
                             entry["id"]))
                        if entry["root_id"] == entry["id"]:
                            conn.execute(
                                "UPDATE memories SET root_id = id "
                                "WHERE root_id = ? AND id != ? "
                                "AND status = 'active'",
                                (entry["id"], entry["id"]))
                        archived += 1
                    elif update_needed:
                        conn.execute(
                            "UPDATE memories SET weight = ? WHERE id = ?",
                            (new_weight, entry["id"]))
                        decayed += 1
                conn.commit()
            finally:
                conn.close()
        print(f"Decay: {decayed} updated, {archived} archived to experience")
        return decayed

    def validate_against_graph(self, current_nodes: set) -> dict:
        """Demote memories whose node_ids left the graph → experience.

        Returns {validated, invalidated, invalidated_ids}.
        """
        validated = 0
        invalidated_ids = []
        with _memory_lock(self.mem_dir):
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM memories WHERE status = 'active'"
                ).fetchall()
                for r in rows:
                    entry = self._row_to_dict(r)
                    node_ids = entry.get("node_ids", [])
                    if not node_ids:
                        validated += 1
                        continue
                    missing = [nid for nid in node_ids
                               if nid not in current_nodes]
                    if missing:
                        reason = (f"{len(missing)} node(s) no longer in "
                                  f"graph: {missing[:5]}")
                        conn.execute(
                            "UPDATE memories SET status = 'experience', "
                            "invalidated_reason = ?, invalidated_at = ?, "
                            "validated_at = ? WHERE id = ?",
                            (reason, datetime.now().isoformat(),
                             datetime.now().isoformat(), entry["id"]))
                        if entry["root_id"] == entry["id"]:
                            conn.execute(
                                "UPDATE memories SET root_id = id "
                                "WHERE root_id = ? AND id != ? "
                                "AND status = 'active'",
                                (entry["id"], entry["id"]))
                        invalidated_ids.append({
                            "id": entry["id"],
                            "question": entry["question"],
                            "missing_nodes": len(missing),
                        })
                    else:
                        conn.execute(
                            "UPDATE memories SET validated_at = ? "
                            "WHERE id = ?",
                            (datetime.now().isoformat(), entry["id"]))
                        validated += 1
                conn.commit()
            finally:
                conn.close()
        print(f"Memory validation: {validated} trusted, "
              f"{len(invalidated_ids)} → experience")
        return {"validated": validated,
                "invalidated": len(invalidated_ids),
                "invalidated_ids": invalidated_ids}

    def consolidate(self) -> dict:
        """One-pass decay + summary. Called after every build."""
        decayed = self.decay()
        conn = self._connect()
        try:
            counts = {r["status"]: r["c"] for r in conn.execute(
                "SELECT status, COUNT(*) AS c FROM memories "
                "GROUP BY status").fetchall()}
            roots = conn.execute(
                "SELECT COUNT(*) AS c FROM memories WHERE root_id = id "
                "AND status = 'active'").fetchone()["c"]
            cats = conn.execute(
                "SELECT COUNT(*) AS c FROM categories").fetchone()["c"]
        finally:
            conn.close()
        summary = {
            "decayed": decayed,
            "trusted": counts.get("active", 0),
            "experience": counts.get("experience", 0),
            "merged": counts.get("merged", 0),
            "split": counts.get("split", 0),
            "roots": roots,
            "categories": cats,
        }
        print(f"Consolidated: {summary}")
        return summary

    # ------------------------------------------------------------------
    # Public API: stats / export / import
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        conn = self._connect()
        try:
            counts = {r["status"]: r["c"] for r in conn.execute(
                "SELECT status, COUNT(*) AS c FROM memories "
                "GROUP BY status").fetchall()}
            l0 = conn.execute(
                "SELECT COUNT(*) AS c FROM memories WHERE status='active' "
                "AND weight > 0.7").fetchone()["c"]
            l1 = conn.execute(
                "SELECT COUNT(*) AS c FROM memories WHERE status='active' "
                "AND weight > 0.3 AND weight <= 0.7").fetchone()["c"]
            l2 = conn.execute(
                "SELECT COUNT(*) AS c FROM memories WHERE status='active' "
                "AND weight <= 0.3").fetchone()["c"]
            roots = conn.execute(
                "SELECT COUNT(*) AS c FROM memories WHERE root_id = id "
                "AND status = 'active'").fetchone()["c"]
            cats = conn.execute(
                "SELECT COUNT(*) AS c FROM categories").fetchone()["c"]
            oldest = conn.execute(
                "SELECT MIN(created) AS c FROM memories").fetchone()["c"]
            top_cats = [dict(r) for r in conn.execute(
                "SELECT ca.path, COUNT(*) AS c FROM memories m "
                "JOIN categories ca ON ca.id = m.category_id "
                "WHERE m.status = 'active' GROUP BY ca.path "
                "ORDER BY c DESC LIMIT 10").fetchall()]
        finally:
            conn.close()
        total = sum(counts.values())
        return {
            "total_entries": total,
            "active_entries": counts.get("active", 0),
            "experience_entries": counts.get("experience", 0),
            "merged_entries": counts.get("merged", 0),
            "split_entries": counts.get("split", 0),
            "layer_counts": {"L0": l0, "L1": l1, "L2": l2},
            "roots": roots,
            "categories": cats,
            "top_categories": top_cats,
            "oldest_entry_timestamp": oldest,
            "storage": "sqlite",
            "db_path": self.db_path,
        }

    def export_all(self) -> dict:
        conn = self._connect()
        try:
            memories = [self._row_to_dict(r) for r in conn.execute(
                "SELECT * FROM memories ORDER BY id").fetchall()]
            categories = [dict(r) for r in conn.execute(
                "SELECT * FROM categories ORDER BY path").fetchall()]
        finally:
            conn.close()
        return {"schema_version": MEMORY_SCHEMA_VERSION,
                "categories": categories, "memories": memories}

    def lineage(self) -> dict:
        """The governance lineage graph: who split from whom / merged
        into whom / which entries are variants of which root.

        Returns {"nodes": [...], "edges": [...]} where every memory is
        a node (id, question, status, category, author, weight) and
        lineage relations are edges:
          split        parent --split--> child   (child.split_from)
          merged_into  canonical <--merge-- absorbed tombstone
          variant      root --variant--> variant (active, root_id != id)
        Pure read — safe for the read-only web UI.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT m.id, m.question, m.status, m.author, m.weight, "
                "m.split_from, m.merged_into, m.root_id, ca.path AS "
                "category FROM memories m LEFT JOIN categories ca ON "
                "ca.id = m.category_id ORDER BY m.id").fetchall()
        finally:
            conn.close()
        nodes = []
        edges = []
        for r in rows:
            nodes.append({
                "id": r["id"], "question": r["question"],
                "status": r["status"], "author": r["author"] or "",
                "weight": r["weight"], "category": r["category"] or "",
            })
            if r["split_from"]:
                edges.append({"from": r["split_from"], "to": r["id"],
                              "type": "split"})
            if r["merged_into"]:
                edges.append({"from": r["merged_into"], "to": r["id"],
                              "type": "merged_into"})
            if (r["root_id"] and r["root_id"] != r["id"]
                    and r["status"] == "active"):
                edges.append({"from": r["root_id"], "to": r["id"],
                              "type": "variant"})
        return {"nodes": nodes, "edges": edges}

    def export_for_debug(self, output_path: str) -> str:
        Path(output_path).write_text(
            json.dumps(self.export_all(), ensure_ascii=False, indent=2)
            + "\n", encoding="utf-8")
        print(f"Exported memory state to {output_path}")
        return output_path

    def _similar_exists(self, question: str,
                        threshold: float = 0.8) -> bool:
        """Token-set similarity check (0-1 scale, not BM25 score)."""
        q_tokens = _simple_tokenize(question)
        if not q_tokens:
            return False
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT question, tags FROM memories "
                "WHERE status = 'active'").fetchall()
        finally:
            conn.close()
        for r in rows:
            e_tokens = _simple_tokenize(r["question"])
            try:
                tags = json.loads(r["tags"] or "[]")
            except (json.JSONDecodeError, TypeError):
                tags = []
            for tag in tags:
                e_tokens |= _simple_tokenize(tag)
            if _similarity_score(q_tokens, e_tokens) >= threshold:
                return True
        return False

    def import_from_json(self, input_path: str, merge: bool = True) -> int:
        """Import memories from a JSON list (or {entries: [...]})."""
        data = json.loads(Path(input_path).read_text(encoding="utf-8"))
        entries = data if isinstance(data, list) else data.get("entries", [])
        # export_all() uses the "memories" key
        if isinstance(data, dict) and not entries:
            entries = data.get("memories", [])
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            entries = list(data["entries"].values())
        imported = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            question = entry.get("question", "")
            if not question:
                continue
            if merge and self._similar_exists(question):
                continue  # already have something similar
            self.add(
                question=question,
                answer=entry.get("answer", ""),
                tags=entry.get("tags", []),
                node_ids=entry.get("node_ids", []),
                chains=entry.get("chains", []),
                category=entry.get("category", "uncategorized"),
                author=entry.get("author", ""),
            )
            imported += 1
        print(f"Imported {imported} memory entries from {input_path}")
        return imported

    # ------------------------------------------------------------------
    # Public API: memory packs (L0/L1/L2 tiers, unchanged shapes)
    # ------------------------------------------------------------------

    def _active_entries(self, limit: int = 0,
                        author: str = None) -> List[dict]:
        conn = self._connect()
        try:
            sql = "SELECT * FROM memories WHERE status = 'active'"
            params: list = []
            if author:
                sql += " AND author = ?"
                params.append(author)
            sql += " ORDER BY weight DESC"
            if limit:
                sql += f" LIMIT {int(limit)}"
            return [self._row_to_dict(r) for r in
                    conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def digest(self, limit: int = 10,
               author: str = None) -> List[dict]:
        """Top active memories by weight — the session-start digest.

        This is what a newcomer (or a fresh agent session) sees first:
        the questions veterans keep asking, with answers. author=
        restricts to one contributor (multi-user read-only view).
        """
        cat_paths = self._category_paths()
        entries = []
        for e in self._active_entries(limit=limit, author=author):
            entries.append({
                "id": e["id"],
                "question": e["question"],
                "answer": (e["answer"] or "")[:300],
                "category": cat_paths.get(e.get("category_id"), ""),
                "author": e.get("author", ""),
                "weight": round(e.get("weight", 1.0), 2),
                "access_count": e.get("access_count", 0),
                "tags": e.get("tags", [])[:5],
                "symbols": (e.get("symbols") or [])[:5],
            })
        return entries

    def authors(self) -> List[dict]:
        """Contributors with entry counts — the multi-user index.

        Groups by author (empty attributed as '(unattributed)'):
        [{author, entries, active}] ordered by total entries. Pure
        read; used by the read-only web UI author filter.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT COALESCE(NULLIF(author, ''), '(unattributed)') "
                "AS author, COUNT(*) AS entries, "
                "SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) "
                "AS active FROM memories GROUP BY author "
                "ORDER BY entries DESC").fetchall()
        finally:
            conn.close()
        return [{"author": r["author"], "entries": r["entries"],
                 "active": r["active"] or 0} for r in rows]

    def generate_pack(self, tier: str = "lite") -> dict:
        """Generate a memory pack for LLM consumption.

        tier: "lite" (~100 tokens), "standard" (~600 tokens),
              "deep" (~2000 tokens), "full" (complete dump)
        """
        if tier == "lite":
            return self._pack_lite()
        if tier == "deep":
            return self._pack_deep()
        if tier == "full":
            return self._pack_full()
        return self._pack_standard()

    def _write_pack(self, name: str, pack: dict) -> dict:
        pack_path = os.path.join(self.graph_dir, name)
        Path(pack_path).write_text(
            json.dumps(pack, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        return pack

    def _pack_lite(self) -> dict:
        entries = self._active_entries(limit=20)
        hot = [{"id": e["id"], "q": e["question"][:60],
                "w": round(e["weight"], 2)}
               for e in entries if e["weight"] > 0.7]
        top_q = [e["question"][:80] for e in entries[:5]]
        pack = {"top_questions": top_q,
                "hot_memories": hot[:10]}
        return self._write_pack(".memory_pack_lite.json", pack)

    def _pack_standard(self) -> dict:
        entries = self._active_entries(limit=30)
        hot = [{"id": e["id"], "q": e["question"],
                "a": e["answer"][:200], "w": round(e["weight"], 2),
                "tags": e["tags"][:3]}
               for e in entries if e["weight"] > 0.7]
        warm = [{"id": e["id"], "q": e["question"][:80],
                 "a": e["answer"][:100], "w": round(e["weight"], 2)}
                for e in entries if 0.3 < e["weight"] <= 0.7]
        pack = {
            "top_questions": [e["q"] for e in hot[:5]],
            "all_hot": hot,
            "warm_summaries": warm[:15],
        }
        return self._write_pack(".memory_pack_standard.json", pack)

    def _pack_deep(self) -> dict:
        cat_paths = self._category_paths()
        entries = []
        for e in self._active_entries():
            entries.append({
                "id": e["id"],
                "question": e["question"],
                "answer": e["answer"],
                "category": cat_paths.get(e.get("category_id"), ""),
                "chains": e["chains"][:10],
                "node_ids": e["node_ids"][:20],
                "tags": e["tags"],
                "weight": round(e["weight"], 4),
                "merged_count": e["merged_count"],
            })
        pack = {"total": len(entries), "entries": entries}
        return self._write_pack(".memory_pack_deep.json", pack)

    def _pack_full(self) -> dict:
        conn = self._connect()
        try:
            active = [self._row_to_dict(r) for r in conn.execute(
                "SELECT * FROM memories WHERE status = 'active' "
                "ORDER BY id").fetchall()]
            experience = [self._row_to_dict(r) for r in conn.execute(
                "SELECT * FROM memories WHERE status = 'experience' "
                "ORDER BY id").fetchall()]
            tombstones = [self._row_to_dict(r) for r in conn.execute(
                "SELECT * FROM memories WHERE status IN "
                "('merged', 'split') ORDER BY id").fetchall()]
            cats = [dict(r) for r in conn.execute(
                "SELECT * FROM categories ORDER BY path").fetchall()]
        finally:
            conn.close()
        pack = {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "categories": cats,
            "entries": active,
            "experience": experience,
            "tombstones": tombstones,
            "versions_included": True,
        }
        return self._write_pack(".memory_pack_full.json", pack)


# ---------------------------------------------------------------------------
# CLI command handlers
# ---------------------------------------------------------------------------

def cmd_memory_store_categories(args):
    """Print the category tree with counts."""
    store = MemoryStore(args.graph)
    tree = store.categories()

    def _render(nodes, indent=0):
        for n in nodes:
            print(f"{'  ' * indent}{n['name']}  ({n['count']} direct, "
                  f"{n['subtree_count']} subtree)  [{n['path']}]")
            _render(n["children"], indent + 1)

    if not tree:
        print("No categories yet.")
        return
    _render(tree)


def cmd_memory_store_split(args):
    """Split a memory into focused sub-memories."""
    store = MemoryStore(args.graph)
    parts = json.loads(args.parts) if isinstance(args.parts, str) \
        else args.parts
    if not isinstance(parts, list) or not parts:
        print("Error: --parts must be a non-empty JSON array of "
              "{question, answer, category?, tags?} objects", file=sys.stderr)
        sys.exit(1)
    try:
        store.split(int(args.id), parts)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_memory_store_merge(args):
    """Merge duplicate memories into one canonical entry."""
    store = MemoryStore(args.graph)
    ids = [int(x) for x in args.ids.split(",") if x.strip()]
    if len(ids) < 2:
        print("Error: --ids needs at least two comma-separated ids",
              file=sys.stderr)
        sys.exit(1)
    try:
        store.merge(ids, canonical_id=int(args.canonical) if args.canonical
                    else None,
                    question=args.question or None,
                    answer=args.answer or None)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_memory_store_move(args):
    """Move a memory to another category."""
    store = MemoryStore(args.graph)
    store.move(int(args.id), args.category)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Memory store")
    parser.add_argument("--graph", required=True)
    sub = parser.add_subparsers(dest="action")

    p_cat = sub.add_parser("categories")
    p_split = sub.add_parser("split")
    p_split.add_argument("--id", required=True)
    p_split.add_argument("--parts", required=True,
                         help="JSON array [{question, answer, ...}]")
    p_merge = sub.add_parser("merge")
    p_merge.add_argument("--ids", required=True, help="Comma-separated ids")
    p_merge.add_argument("--canonical", default="")
    p_merge.add_argument("--question", default="")
    p_merge.add_argument("--answer", default="")
    p_move = sub.add_parser("move")
    p_move.add_argument("--id", required=True)
    p_move.add_argument("--category", required=True)

    args = parser.parse_args()
    if not args.action:
        parser.print_help()
        sys.exit(1)
    {"categories": cmd_memory_store_categories,
     "split": cmd_memory_store_split,
     "merge": cmd_memory_store_merge,
     "move": cmd_memory_store_move}[args.action](args)
