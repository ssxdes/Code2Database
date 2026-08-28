"""Phase 10: Knowledge audit.

Answers "what do we know about X" across memory + knowledge + graph.
Records every kb save/correct/forget into the existing audit_log
table so there's a full audit trail.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Dict, Any

from _builder.kb_index import _kb_connect, query_kb


def audit_kb(graph_dir: str, topic: str = "") -> Dict[str, Any]:
    """Audit the project KB.

    Returns a summary with:
      - total items by kind (memory_qa, knowledge_principle, etc.)
      - stale items (created > 90 days ago, never accessed since)
      - low-confidence items (confidence < 0.5)
      - high-citation items (top 5 by access_count)
      - principle coverage (which principles are most-linked from Q&A)
      - if topic given: what we know about X (top 5 matches)
    """
    conn = _kb_connect(graph_dir)
    if conn is None:
        return {"error": "no_db", "topic": topic}
    try:
        result: Dict[str, Any] = {"topic": topic, "audited_at": datetime.now().isoformat()}

        # Counts by kind
        rows = conn.execute(
            "SELECT kind, COUNT(*) AS cnt FROM kb_paragraphs GROUP BY kind"
        ).fetchall()
        result["by_kind"] = {r["kind"]: r["cnt"] for r in rows}
        result["total_items"] = sum(r["cnt"] for r in rows)

        # Stale items: created > 90 days ago, access_count = 0
        rows = conn.execute(
            "SELECT id, title, kind, created_at FROM kb_paragraphs "
            "WHERE access_count = 0 AND created_at < ?",
            (datetime.fromtimestamp(
                datetime.now().timestamp() - 90 * 86400).isoformat(),)
        ).fetchall()
        result["stale_items"] = [{
            "id": r["id"], "title": r["title"] or "",
            "kind": r["kind"], "created_at": r["created_at"],
        } for r in rows][:20]

        # Low-confidence
        rows = conn.execute(
            "SELECT id, title, kind, confidence FROM kb_paragraphs "
            "WHERE confidence < 0.5 ORDER BY confidence ASC LIMIT 20"
        ).fetchall()
        result["low_confidence_items"] = [{
            "id": r["id"], "title": r["title"] or "",
            "kind": r["kind"], "confidence": round(r["confidence"], 3),
        } for r in rows]

        # High-citation (top by access_count)
        rows = conn.execute(
            "SELECT id, title, kind, access_count, weight FROM kb_paragraphs "
            "WHERE access_count > 0 ORDER BY access_count DESC LIMIT 10"
        ).fetchall()
        result["high_citation_items"] = [{
            "id": r["id"], "title": r["title"] or "",
            "kind": r["kind"], "access_count": r["access_count"],
            "weight": round(r["weight"], 4),
        } for r in rows]

        # Principle coverage: which principles are most-linked from Q&A
        rows = conn.execute(
            "SELECT principle_ref, COUNT(*) AS cnt FROM kb_paragraphs "
            "WHERE principle_ref IS NOT NULL GROUP BY principle_ref "
            "ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        result["most_linked_principles"] = [{
            "principle_id": r["principle_ref"],
            "linked_qa_count": r["cnt"],
        } for r in rows]

        # If topic provided, return top matches
        if topic:
            matches = query_kb(graph_dir, topic, top_n=5, log_query=False)
            result["what_we_know_about_X"] = [{
                "id": m["id"],
                "source_kind": m["source_kind"],
                "title": m["title"],
                "body": m["body"][:300],
                "score": m["score"],
                "kind": m["kind"],
            } for m in matches]

        return result
    finally:
        conn.close()


def write_audit_log_entry(graph_dir: str, action: str,
                           target_id: int = None, target_kind: str = None,
                           attribute: str = None, before_value: Any = None,
                           after_value: Any = None, reason: str = "") -> None:
    """Record a kb operation in the project's audit_log table.

    Reuses the existing audit_log table (Phase 10 integration).
    Best-effort — silently swallows errors.
    """
    conn = _kb_connect(graph_dir)
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT INTO audit_log (timestamp, operator, command, target_kind, "
            "target_id, action, attribute, before_value, after_value, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(),
             os.environ.get("USER", "kb-system"),
             "kb-" + action,
             target_kind or "kb_paragraph",
             str(target_id) if target_id else "",
             action,
             attribute or "",
             json.dumps(before_value, ensure_ascii=False) if before_value is not None else None,
             json.dumps(after_value, ensure_ascii=False) if after_value is not None else None,
             reason)
        )
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()
