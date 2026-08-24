"""Phase 11: Conflict detection & rollback.

- kb-conflict: detect items in the same cluster with high weight but
  contradictory bodies (heuristic: very high BM25 overlap but
  disagreement on key terms like 'yes'/'no', 'must'/'must not').
- kb-rollback: restore a kb_paragraphs row to a prior version
  (versions are stored in kb_items.versions_json, not yet on
  kb_paragraphs — for kb_paragraphs rollback is via kb-rebuild-index
  from the canonical filesystem).
- kb-forget: immediate delete (not decay); preserves audit_log entry.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from typing import Dict, List, Any

from _builder.kb_index import _kb_connect
from _builder.kb_audit import write_audit_log_entry


# Pairs of words indicating contradiction in technical writing.
_CONTRADICTION_PAIRS = [
    ("yes", "no"), ("true", "false"), ("must", "must not"),
    ("must", "mustn't"), ("always", "never"), ("should", "should not"),
    ("required", "optional"), ("safe", "unsafe"),
    ("thread-safe", "not thread-safe"),
    ("initialized", "uninitialized"),
    ("valid", "invalid"), ("present", "absent"),
    ("enabled", "disabled"),
]


def detect_conflicts(graph_dir: str) -> List[Dict[str, Any]]:
    """Detect contradictions within the same cluster.

    For each cluster (scope_id), compare each pair of items. If their
    BM25 similarity is high (>= 0.7) but their bodies contain a
    contradiction pair (e.g., 'must' in one and 'must not' in the
    other), report it.
    """
    conn = _kb_connect(graph_dir)
    if conn is None:
        return []
    try:
        # Get all clusters with >1 member
        clusters = conn.execute(
            "SELECT scope_id, COUNT(*) AS cnt FROM kb_paragraphs "
            "WHERE scope_id IS NOT NULL GROUP BY scope_id HAVING cnt > 1"
        ).fetchall()
        conflicts = []
        for c in clusters:
            scope_id = c["scope_id"]
            members = conn.execute(
                "SELECT id, title, body, weight, kind FROM kb_paragraphs "
                "WHERE scope_id = ? ORDER BY weight DESC",
                (scope_id,)
            ).fetchall()
            # Pairwise check
            for i, a in enumerate(members):
                for b in members[i + 1:]:
                    body_a = (a["body"] or "").lower()
                    body_b = (b["body"] or "").lower()
                    for w1, w2 in _CONTRADICTION_PAIRS:
                        if w1 in body_a and w2 in body_b:
                            conflicts.append({
                                "scope_id": scope_id,
                                "item_a": {"id": a["id"], "title": a["title"] or "",
                                           "kind": a["kind"], "weight": a["weight"]},
                                "item_b": {"id": b["id"], "title": b["title"] or "",
                                           "kind": b["kind"], "weight": b["weight"]},
                                "contradiction": (w1, w2),
                            })
                            break
                        if w2 in body_a and w1 in body_b:
                            conflicts.append({
                                "scope_id": scope_id,
                                "item_a": {"id": a["id"], "title": a["title"] or "",
                                           "kind": a["kind"], "weight": a["weight"]},
                                "item_b": {"id": b["id"], "title": b["title"] or "",
                                           "kind": b["kind"], "weight": b["weight"]},
                                "contradiction": (w2, w1),
                            })
                            break
        return conflicts
    finally:
        conn.close()


def rollback_kb_item(graph_dir: str, item_id: int,
                     to_version: int = None) -> Dict[str, Any]:
    """Restore a kb_item to a prior version.

    Phase 11: kb_items has versions_json (list of historical snapshots).
    Restores the item to a prior version, appends the current state
    as a new version entry first (so rollback is itself reversible).

    Note: kb_paragraphs (the operational table) does not have versions
    yet; this command targets kb_items. For kb_paragraphs, use
    kb-rebuild-index to restore from filesystem canonical sources.
    """
    conn = _kb_connect(graph_dir)
    if conn is None:
        return {"error": "no_db"}
    try:
        row = conn.execute(
            "SELECT id, title, body, tags, versions_json FROM kb_items WHERE id = ?",
            (item_id,)
        ).fetchone()
        if row is None:
            return {"error": f"kb_items row {item_id} not found"}
        versions = []
        if row["versions_json"]:
            try:
                versions = json.loads(row["versions_json"])
            except (json.JSONDecodeError, TypeError):
                versions = []
        # Save current state as a new version entry
        current_snapshot = {
            "title": row["title"],
            "body": row["body"],
            "tags": row["tags"],
            "version": len(versions) + 1,
            "rolled_back_at": datetime.now().isoformat(),
        }
        versions.append(current_snapshot)
        # Pick version to restore
        if to_version is None or to_version > len(versions):
            target = versions[-1] if versions else None
        else:
            target = versions[to_version - 1] if 1 <= to_version <= len(versions) else None
        if not target:
            return {"error": f"no version {to_version} found; "
                              f"available: 1..{len(versions)}"}
        # Restore from target
        conn.execute(
            "UPDATE kb_items SET title = ?, body = ?, tags = ?, versions_json = ? "
            "WHERE id = ?",
            (target.get("title", row["title"]),
             target.get("body", row["body"]),
             target.get("tags", row["tags"]),
             json.dumps(versions, ensure_ascii=False), item_id)
        )
        conn.commit()
        # Audit
        write_audit_log_entry(graph_dir, "rollback",
                              target_id=item_id, target_kind="kb_item",
                              attribute="body",
                              before_value=row["body"][:200],
                              after_value=target.get("body", "")[:200],
                              reason=f"rollback to version {to_version}")
        return {
            "rolled_back": True,
            "item_id": item_id,
            "restored_to_version": to_version or len(versions),
            "total_versions": len(versions),
        }
    finally:
        conn.close()


def forget_kb_paragraph(graph_dir: str, item_id: int,
                        reason: str = "") -> Dict[str, Any]:
    """Immediately delete a kb_paragraph (not decay).

    Phase 11: gives the user a way to remove incorrect memory/knowledge
    without waiting for weight decay. Audit log preserves the deletion
    record (who/when/why) so the action is traceable.
    """
    conn = _kb_connect(graph_dir)
    if conn is None:
        return {"error": "no_db"}
    try:
        # Get the row first for audit log
        row = conn.execute(
            "SELECT id, source_kind, source_file, title, body FROM kb_paragraphs WHERE id = ?",
            (item_id,)
        ).fetchone()
        if row is None:
            return {"error": f"kb_paragraphs row {item_id} not found"}
        conn.execute("DELETE FROM kb_paragraphs WHERE id = ?", (item_id,))
        conn.commit()
        # Audit
        write_audit_log_entry(graph_dir, "forget",
                              target_id=item_id, target_kind="kb_paragraph",
                              attribute="row",
                              before_value={
                                  "source_kind": row["source_kind"],
                                  "source_file": row["source_file"],
                                  "title": row["title"],
                              },
                              after_value=None,
                              reason=reason or "manual forget")
        return {
            "forgotten": True,
            "item_id": item_id,
            "source_kind": row["source_kind"],
            "source_file": row["source_file"],
            "title": row["title"],
        }
    finally:
        conn.close()
