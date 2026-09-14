"""Build provenance — the database records how it was produced.

Every successful full build and every per-file sync stamps a small set
of facts into the ``meta`` table of ``code2database.db``:

- ``build_timestamp``     — ISO 8601 UTC, when the state was produced
- ``build_tool_version``  — the Code2Database version (scripts/_version.py)
- ``build_source_commit`` — the source repo commit the state reflects
- ``build_label``         — the producing path: ``build`` (full rebuild)
  or ``per-file-sync`` (the build-build path shared by the manual
  per-file command and the daemon's incremental sync)
- ``build_node_count`` / ``build_edge_count`` — content sizes at stamp time

This is knowledge an LLM cannot regenerate: given only the artifact, the
artifact must say when it was made, by which tool version and against
which source commit. ``graph-provenance`` reads the stamp back; the
stamp never participates in build control flow (best-effort by design).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

KEY_TIMESTAMP = "build_timestamp"
KEY_TOOL_VERSION = "build_tool_version"
KEY_SOURCE_COMMIT = "build_source_commit"
KEY_LABEL = "build_label"
KEY_NODE_COUNT = "build_node_count"
KEY_EDGE_COUNT = "build_edge_count"

_ALL_KEYS = (KEY_TIMESTAMP, KEY_TOOL_VERSION, KEY_SOURCE_COMMIT,
             KEY_LABEL, KEY_NODE_COUNT, KEY_EDGE_COUNT)

LABEL_BUILD = "build"
LABEL_PER_FILE_SYNC = "per-file-sync"


def read_build_provenance(graph_dir) -> dict:
    """Return the provenance stamped into the DB (empty dict when absent).

    Reads via a read-only URI connection so querying never opens the
    file for writing (safe against a graph held by another process).
    """
    db_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.isfile(db_path):
        return {}
    out: dict = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            rows = conn.execute(
                "SELECT key, value FROM meta WHERE key IN "
                "(?, ?, ?, ?, ?, ?)", _ALL_KEYS).fetchall()
            out = {str(k): v for k, v in rows}
        finally:
            conn.close()
    except sqlite3.Error:
        logger.debug("provenance read failed", exc_info=True)
    return out


def stamp_build_provenance(graph_dir, label: str = LABEL_BUILD,
                           node_count=None, edge_count=None) -> bool:
    """Stamp how-the-DB-was-produced facts into the meta table.

    Best-effort by design: a stamping failure is logged and swallowed —
    it must never fail the build that produced the content. Counts are
    recomputed from the tables when not supplied by the caller. Returns
    True when the stamp landed.
    """
    label = str(label or LABEL_BUILD).strip() or LABEL_BUILD
    db_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.isfile(db_path):
        return False
    if node_count is None or edge_count is None:
        n, e = _count_content(db_path)
        node_count = n if node_count is None else node_count
        edge_count = e if edge_count is None else edge_count
    values = {
        KEY_TIMESTAMP: datetime.now(timezone.utc).isoformat(),
        KEY_TOOL_VERSION: _tool_version(),
        KEY_SOURCE_COMMIT: _source_commit(graph_dir),
        KEY_LABEL: label,
        KEY_NODE_COUNT: str(node_count),
        KEY_EDGE_COUNT: str(edge_count),
    }
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meta "
                "(key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                sorted(values.items()))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.debug("provenance stamp failed", exc_info=True)
        return False
    return True


def _tool_version() -> str:
    try:
        from _version import __version__
        return str(__version__)
    except Exception:
        return "unknown"


def _source_commit(graph_dir) -> str:
    manifest = os.path.join(graph_dir, ".code2database_manifest.json")
    try:
        data = json.loads(Path(manifest).read_text(encoding="utf-8"))
        commit = data.get("source_commit") or {}
        return str(commit.get("head_short") or commit.get("head") or "")
    except Exception:
        return ""


def _count_content(db_path):
    """Return (functions, edges) row counts; -1 when a table is absent."""
    counts = {"functions": -1, "edges": -1}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            for table in counts:
                try:
                    counts[table] = conn.execute(
                        f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.Error:
                    counts[table] = -1
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return counts["functions"], counts["edges"]
