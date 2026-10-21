"""Knowledge audit.

Answers "what do we know about X" across memory + knowledge + graph.
Records every kb save/correct/forget into the existing audit_log
table so there's a full audit trail.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Dict, Any, Optional, List

from _builder.kb.kb_index import _kb_connect, query_kb
import logging


def kb_stats(graph_dir: str) -> Dict[str, Any]:
    """Return a comprehensive statistics snapshot of the project KB.

    Complements :func:`audit_kb` (which focuses on content review) by
    reporting storage, cluster, query-log, and access patterns that
    an enterprise maintainer needs to monitor KB health.
    """
    conn = _kb_connect(graph_dir)
    if conn is None:
        return {"error": "no_db"}
    try:
        result: Dict[str, Any] = {
            "snapshot_at": datetime.now().isoformat(),
        }
        # -- Storage stats --
        rows = conn.execute(
            "SELECT kind, COUNT(*) AS cnt FROM kb_paragraphs "
            "GROUP BY kind ORDER BY cnt DESC"
        ).fetchall()
        result["by_kind"] = {r["kind"]: r["cnt"] for r in rows}
        result["total_items"] = sum(r["cnt"] for r in rows)
        rows = conn.execute(
            "SELECT source_kind, COUNT(*) AS cnt FROM kb_paragraphs "
            "GROUP BY source_kind ORDER BY cnt DESC"
        ).fetchall()
        result["by_source_kind"] = {r["source_kind"]: r["cnt"]
                                     for r in rows}
        rows = conn.execute(
            "SELECT version_scope, COUNT(*) AS cnt FROM kb_paragraphs "
            "WHERE version_scope IS NOT NULL AND version_scope != '' "
            "GROUP BY version_scope ORDER BY cnt DESC"
        ).fetchall()
        result["by_version_scope"] = {r["version_scope"]: r["cnt"]
                                       for r in rows}
        # -- Cluster stats --
        rows = conn.execute(
            "SELECT scope_id, COUNT(*) AS cnt FROM kb_paragraphs "
            "WHERE scope_id IS NOT NULL GROUP BY scope_id "
            "ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        result["cluster_count"] = conn.execute(
            "SELECT COUNT(DISTINCT scope_id) FROM kb_paragraphs "
            "WHERE scope_id IS NOT NULL"
        ).fetchone()[0]
        result["largest_clusters"] = [{
            "scope_id": r["scope_id"], "size": r["cnt"],
        } for r in rows]
        # Cluster summaries coverage
        result["cluster_summaries_count"] = conn.execute(
            "SELECT COUNT(*) FROM kb_cluster_summaries"
        ).fetchone()[0]
        # -- Query log analytics --
        total_queries = conn.execute(
            "SELECT COUNT(*) FROM kb_query_log"
        ).fetchone()[0]
        matched_queries = conn.execute(
            "SELECT COUNT(*) FROM kb_query_log WHERE matched = 1"
        ).fetchone()[0]
        zero_hit = conn.execute(
            "SELECT COUNT(*) FROM kb_query_log WHERE matched = 0"
        ).fetchone()[0]
        result["query_log"] = {
            "total": total_queries,
            "matched": matched_queries,
            "zero_hit": zero_hit,
            "match_rate": round(matched_queries / total_queries, 4)
                           if total_queries else 0.0,
        }
        # Top queries (most frequent)
        rows = conn.execute(
            "SELECT query, COUNT(*) AS cnt FROM kb_query_log "
            "GROUP BY query ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        result["query_log"]["top_queries"] = [{
            "query": r["query"], "count": r["cnt"],
        } for r in rows]
        # Zero-hit queries (most valuable for content gap analysis)
        rows = conn.execute(
            "SELECT query, queried_at FROM kb_query_log "
            "WHERE matched = 0 ORDER BY queried_at DESC LIMIT 20"
        ).fetchall()
        result["query_log"]["recent_zero_hit"] = [{
            "query": r["query"], "queried_at": r["queried_at"],
        } for r in rows]
        # -- Access patterns --
        rows = conn.execute(
            "SELECT id, title, kind, access_count, weight "
            "FROM kb_paragraphs WHERE access_count > 0 "
            "ORDER BY access_count DESC LIMIT 10"
        ).fetchall()
        result["most_accessed"] = [{
            "id": r["id"], "title": r["title"] or "",
            "kind": r["kind"], "access_count": r["access_count"],
            "weight": round(r["weight"], 4),
        } for r in rows]
        never_accessed = conn.execute(
            "SELECT COUNT(*) FROM kb_paragraphs WHERE access_count = 0"
        ).fetchone()[0]
        result["never_accessed_count"] = never_accessed
        # -- Index status --
        meta = {r["key"]: r["value"] for r in conn.execute(
            "SELECT key, value FROM kb_meta"
        ).fetchall()}
        result["ann_index"] = {
            "dimension": int(meta.get("ann_dim", 0)),
            "model": meta.get("ann_model", ""),
            "built_at": meta.get("ann_built_at", ""),
        }
        result["last_rebuild"] = {
            "mtime": meta.get("last_rebuild_mtime", ""),
            "file_count": int(meta.get("last_rebuild_file_count", 0)),
        }
        result["domain_name"] = meta.get("domain_name", "")
        result["schema_version"] = int(meta.get("schema_version", 1))
        # Knowledge store schema version (separate database).
        try:
            from _builder.kb.knowledge_store import open_knowledge
            ks = open_knowledge(graph_dir, create_if_missing=False)
            if ks is not None:
                krow = ks._conn.execute(
                    "SELECT value FROM knowledge_meta "
                    "WHERE key = 'schema_version'").fetchone()
                result["knowledge_schema_version"] = (
                    int(krow[0]) if krow else 1)
                ks.close()
            else:
                result["knowledge_schema_version"] = None
        except Exception:
            result["knowledge_schema_version"] = None
        return result
    finally:
        conn.close()


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
                           target_id: Optional[int] = None,
                           target_kind: Optional[str] = None,
                           attribute: Optional[str] = None,
                           before_value: Any = None,
                           after_value: Any = None,
                           reason: str = "") -> None:
    """Record a kb operation in the project's audit_log table.

    Reuses the existing audit_log table.
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
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    finally:
        conn.close()
