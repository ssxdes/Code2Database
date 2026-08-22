"""cgdb_versions — layered version control with first_seen/last_seen soft delete.

Per cgdb-architecture-and-poc-report.md 5.5.9:
- graph_versions: one row per commit (low cost)
- node_versions / edge_versions: only changed rows (medium cost)
- first_seen_version + last_seen_version = soft delete (no hard delete)

Time-travel query:
    SELECT * FROM cgdb_nodes
    WHERE name = 'foo'
      AND first_seen_version <= (SELECT version_id FROM graph_versions WHERE commit_hash = ?)
      AND (last_seen_version > (SELECT version_id FROM graph_versions WHERE commit_hash = ?)
           OR last_seen_version = (SELECT MAX(version_id) FROM graph_versions));

This module provides:
  - record_version(commit_hash, commit_subject, parent_version_id) → version_id
  - time_travel_query(node_id, version_id) → node state at that version
  - soft_delete_node(node_id, version_id) — set last_seen_version
  - soft_delete_edge(edge_id, version_id)
  - list_versions(limit) — recent graph_versions rows
  - get_version_by_commit(commit_hash) → version_id
"""
import sqlite3
import time
from typing import Any, Dict, List, Optional


class VersionController:
    """Manages the graph_versions table + first_seen/last_seen on nodes/edges.

    Wraps a SQLite connection (shared with SQLiteCGDBStore for transactional
    consistency). All methods are idempotent.
    """

    def __init__(self, db_path: str, conn: Optional[sqlite3.Connection] = None):
        self._db_path = db_path
        self._conn = conn
        self._owns_conn = (conn is None)

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.execute("PRAGMA foreign_keys = ON")
        return self._conn

    def close(self) -> None:
        if self._owns_conn and self._conn is not None:
            self._conn.close()
            self._conn = None

    def record_version(self, commit_hash: str, commit_subject: str = "",
                       parent_version_id: Optional[int] = None,
                       compiled_at: Optional[int] = None,
                       force_insert: bool = False) -> int:
        """Insert a new graph_versions row, return the new version_id.

        If a row with the same commit_hash already exists, return its
        version_id (idempotent). Set force_insert=True to always insert a
        new row (used for pre-build / post-enhance snapshots that share the
        same commit hash but represent different graph states).
        """
        conn = self._ensure_conn()
        # Idempotent: if commit_hash exists, return existing version_id
        if commit_hash and not force_insert:
            row = conn.execute(
                "SELECT version_id FROM graph_versions WHERE commit_hash = ?",
                (commit_hash,)
            ).fetchone()
            if row:
                return row[0]
        if compiled_at is None:
            compiled_at = int(time.time())
        cur = conn.execute(
            "INSERT INTO graph_versions "
            "(commit_hash, commit_subject, compiled_at, parent_version_id) "
            "VALUES (?, ?, ?, ?)",
            (commit_hash, commit_subject, compiled_at, parent_version_id)
        )
        conn.commit()
        return cur.lastrowid

    def get_version_by_commit(self, commit_hash: str) -> Optional[int]:
        """Return version_id for a commit_hash, or None if not found."""
        conn = self._ensure_conn()
        row = conn.execute(
            "SELECT version_id FROM graph_versions WHERE commit_hash = ?",
            (commit_hash,)
        ).fetchone()
        return row[0] if row else None

    def get_version(self, version_id: int) -> Optional[Dict[str, Any]]:
        """Return the graph_versions row for a version_id."""
        conn = self._ensure_conn()
        row = conn.execute(
            "SELECT version_id, commit_hash, commit_subject, compiled_at, "
            "parent_version_id FROM graph_versions WHERE version_id = ?",
            (version_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "version_id": row[0], "commit_hash": row[1],
            "commit_subject": row[2], "compiled_at": row[3],
            "parent_version_id": row[4],
        }

    def list_versions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return recent graph_versions rows, newest first."""
        conn = self._ensure_conn()
        rows = conn.execute(
            "SELECT version_id, commit_hash, commit_subject, compiled_at, "
            "parent_version_id FROM graph_versions "
            "ORDER BY version_id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [{"version_id": r[0], "commit_hash": r[1],
                 "commit_subject": r[2], "compiled_at": r[3],
                 "parent_version_id": r[4]} for r in rows]

    def get_latest_version_id(self) -> int:
        """Return the highest version_id, or 1 if table is empty."""
        conn = self._ensure_conn()
        row = conn.execute(
            "SELECT MAX(version_id) FROM graph_versions"
        ).fetchone()
        if not row or row[0] is None:
            return 1
        return int(row[0])

    def time_travel_query_node(self, node_id: int,
                                version_id: int) -> Optional[Dict[str, Any]]:
        """Return the state of a node at a specific version_id, or None if
        the node didn't exist or had been soft-deleted by that version.

        A node is "alive" at version_id V if:
          first_seen_version <= V AND (last_seen_version > V OR
          last_seen_version = MAX(version_id))
        """
        conn = self._ensure_conn()
        row = conn.execute(
            "SELECT id, kind, name, fqn, file_id, line, col, byte_start, "
            "byte_end, type_spelling, config_predicate_id, attrs, "
            "first_seen_version, last_seen_version, commit_hash "
            "FROM cgdb_nodes WHERE id = ? "
            "AND first_seen_version <= ? "
            "AND (last_seen_version > ? OR last_seen_version = "
            "    (SELECT MAX(version_id) FROM graph_versions))",
            (node_id, version_id, version_id)
        ).fetchone()
        if not row:
            return None
        import json
        return {
            "id": row[0], "kind": row[1], "name": row[2], "fqn": row[3],
            "file_id": row[4], "line": row[5], "col": row[6],
            "byte_start": row[7], "byte_end": row[8],
            "type_spelling": row[9], "config_predicate_id": row[10],
            "attrs": json.loads(row[11] or "{}"),
            "first_seen_version": row[12], "last_seen_version": row[13],
            "commit_hash": row[14],
        }

    def time_travel_query_edge(self, edge_id: int,
                                version_id: int) -> Optional[Dict[str, Any]]:
        """Return the state of an edge at a specific version_id, or None."""
        conn = self._ensure_conn()
        row = conn.execute(
            "SELECT id, src_id, dst_id, kind, file_id, line, col, "
            "first_seen_version, last_seen_version, commit_hash "
            "FROM cgdb_edges WHERE id = ? "
            "AND first_seen_version <= ? "
            "AND (last_seen_version > ? OR last_seen_version = "
            "    (SELECT MAX(version_id) FROM graph_versions))",
            (edge_id, version_id, version_id)
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "src_id": row[1], "dst_id": row[2], "kind": row[3],
            "file_id": row[4], "line": row[5], "col": row[6],
            "first_seen_version": row[7], "last_seen_version": row[8],
            "commit_hash": row[9],
        }

    def soft_delete_node(self, node_id: int, version_id: int) -> int:
        """Mark a node as deleted at version_id by setting
        last_seen_version = version_id. Returns rows affected (0 or 1).
        """
        conn = self._ensure_conn()
        cur = conn.execute(
            "UPDATE cgdb_nodes SET last_seen_version = ? "
            "WHERE id = ? AND last_seen_version > ?",
            (version_id, node_id, version_id)
        )
        conn.commit()
        return cur.rowcount

    def soft_delete_edge(self, edge_id: int, version_id: int) -> int:
        """Mark an edge as deleted at version_id by setting
        last_seen_version = version_id. Returns rows affected (0 or 1).
        """
        conn = self._ensure_conn()
        cur = conn.execute(
            "UPDATE cgdb_edges SET last_seen_version = ? "
            "WHERE id = ? AND last_seen_version > ?",
            (version_id, edge_id, version_id)
        )
        conn.commit()
        return cur.rowcount

    def soft_delete_file_records(self, file_path: str, version_id: int) -> int:
        """Soft-delete all nodes/edges associated with a file path.

        Sets last_seen_version = version_id on:
          - all cgdb_nodes with file_id matching cgdb_files.path
          - all cgdb_edges with file_id matching cgdb_files.path
        Returns total rows soft-deleted.
        """
        conn = self._ensure_conn()
        conn.execute("BEGIN")
        try:
            file_row = conn.execute(
                "SELECT id FROM cgdb_files WHERE path = ?", (file_path,)
            ).fetchone()
            if not file_row:
                conn.execute("ROLLBACK")
                return 0
            file_id = file_row[0]
            cur1 = conn.execute(
                "UPDATE cgdb_nodes SET last_seen_version = ? "
                "WHERE file_id = ? AND last_seen_version > ?",
                (version_id, file_id, version_id)
            )
            cur2 = conn.execute(
                "UPDATE cgdb_edges SET last_seen_version = ? "
                "WHERE file_id = ? AND last_seen_version > ?",
                (version_id, file_id, version_id)
            )
            conn.execute("COMMIT")
            return cur1.rowcount + cur2.rowcount
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def list_nodes_at_version(self, version_id: int, limit: int = 100) -> List[int]:
        """Return node IDs that were alive at version_id."""
        conn = self._ensure_conn()
        rows = conn.execute(
            "SELECT id FROM cgdb_nodes "
            "WHERE first_seen_version <= ? "
            "AND (last_seen_version > ? OR last_seen_version = "
            "    (SELECT MAX(version_id) FROM graph_versions)) "
            "LIMIT ?",
            (version_id, version_id, limit)
        ).fetchall()
        return [r[0] for r in rows]

    def diff_versions(self, v1: int, v2: int, limit: int = 1000) -> Dict[str, List[int]]:
        """Return nodes/edges added (in v2 not in v1) and removed (in v1 not in v2).

        Returns: {"added_nodes": [...], "removed_nodes": [...],
                  "added_edges": [...], "removed_edges": [...]}
        """
        conn = self._ensure_conn()
        # Nodes added between v1 and v2: first_seen_version > v1 AND first_seen <= v2
        added_nodes = [r[0] for r in conn.execute(
            "SELECT id FROM cgdb_nodes "
            "WHERE first_seen_version > ? AND first_seen_version <= ? "
            "ORDER BY id LIMIT ?",
            (v1, v2, limit)
        ).fetchall()]
        # Nodes removed between v1 and v2: last_seen <= v2 AND last_seen > v1
        removed_nodes = [r[0] for r in conn.execute(
            "SELECT id FROM cgdb_nodes "
            "WHERE last_seen_version > ? AND last_seen_version <= ? "
            "ORDER BY id LIMIT ?",
            (v1, v2, limit)
        ).fetchall()]
        added_edges = [r[0] for r in conn.execute(
            "SELECT id FROM cgdb_edges "
            "WHERE first_seen_version > ? AND first_seen_version <= ? "
            "ORDER BY id LIMIT ?",
            (v1, v2, limit)
        ).fetchall()]
        removed_edges = [r[0] for r in conn.execute(
            "SELECT id FROM cgdb_edges "
            "WHERE last_seen_version > ? AND last_seen_version <= ? "
            "ORDER BY id LIMIT ?",
            (v1, v2, limit)
        ).fetchall()]
        return {
            "added_nodes": added_nodes,
            "removed_nodes": removed_nodes,
            "added_edges": added_edges,
            "removed_edges": removed_edges,
        }
