"""Phase 4: KB clustering + principle_ref linking.

Groups similar kb_paragraphs items into clusters via union-find on
FTS5 BM25 similarity. Each cluster has a canonical_id pointing to
the highest weight × confidence item. Cross-kind links memory_qa →
knowledge_principle via principle_ref.

Idempotent — re-running recomputes scope_id and canonical_id from
scratch. Safe to call repeatedly.
"""
from __future__ import annotations

import sqlite3
import sys
from typing import Dict

from _builder.kb_index import _kb_connect, _fts5_escape
import re as _re
import logging


# Threshold for Jaccard similarity — two items with Jaccard >= this
# are considered "the same cluster". 0.3 is moderate (catches
# rephrased versions of the same question); 0.5 is strict.
CLUSTER_SIMILARITY_THRESHOLD = 0.3


class _UnionFind:
    """Plain union-find on integer IDs."""

    def __init__(self):
        self._parent: Dict[int, int] = {}

    def find(self, x: int) -> int:
        if x not in self._parent:
            self._parent[x] = x
            return x
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


def _tokenize_for_jaccard(text: str) -> set:
    """Tokenize text for Jaccard similarity (lowercased alphanumeric)."""
    return set(_re.findall(r'[a-z0-9_]+', (text or "").lower()))


def _jaccard(a: set, b: set) -> float:
    """Jaccard similarity = |A∩B| / |A∪B|."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def cluster_kb(graph_dir: str, threshold: float = CLUSTER_SIMILARITY_THRESHOLD,
               verbose: bool = True) -> dict:
    """Cluster kb_paragraphs by Jaccard token-set similarity.

    C1 fix: previously used FTS5 BM25 score as similarity metric, but
    BM25 is a relevance ranking, not a similarity measure. Two docs with
    high BM25 just share tokens — they're not necessarily "the same
    question". Jaccard on token sets is the correct similarity metric.

    Performance: uses FTS5 to get top-K candidates per item (K=20),
    then computes Jaccard on those candidates only. This avoids O(N²)
    pairwise comparisons while still catching the most similar pairs.

    Returns summary dict with cluster_count, items_clustered,
    principle_refs_linked.
    """
    conn = _kb_connect(graph_dir)
    if conn is None:
        return {"clustered": False, "reason": "no_db"}
    try:
        # Reset existing clusters
        conn.execute("UPDATE kb_paragraphs SET scope_id = NULL, canonical_id = NULL")
        # Load all items
        rows = conn.execute(
            "SELECT id, title, body, tags, weight, confidence, kind "
            "FROM kb_paragraphs"
        ).fetchall()
        if not rows:
            return {"clustered": True, "cluster_count": 0, "items_clustered": 0,
                    "principle_refs_linked": 0}
        uf = _UnionFind()
        items = [(r["id"], r["title"] or "", r["body"] or "",
                  r["tags"] or "", float(r["weight"]),
                  float(r["confidence"]), r["kind"]) for r in rows]
        items_skipped = 0
        # Precompute token sets for all items
        token_sets: Dict[int, set] = {}
        for iid, title, body, tags, *_ in items:
            token_sets[iid] = _tokenize_for_jaccard(title + " " + body)
        # For each item, find similar items via FTS5 (candidates only),
        # then verify with Jaccard
        for i, (iid, title, body, tags, weight, conf, kind) in enumerate(items):
            ts = token_sets.get(iid, set())
            if not ts:
                continue
            query_text = (title + " " + body)[:500]
            if not query_text.strip():
                continue
            try:
                match_expr = _fts5_escape(query_text)
                # Get top-20 FTS5 candidates (sampling for performance)
                cand_rows = conn.execute(
                    "SELECT id FROM kb_paragraphs_fts "
                    "WHERE kb_paragraphs_fts MATCH ? AND id != ? "
                    "ORDER BY bm25(kb_paragraphs_fts) LIMIT 20",
                    (match_expr, iid)
                ).fetchall()
                for cr in cand_rows:
                    cand_id = cr["id"]
                    cand_ts = token_sets.get(cand_id, set())
                    # C1: use Jaccard (correct similarity metric)
                    sim = _jaccard(ts, cand_ts)
                    if sim >= threshold:
                        uf.union(iid, cand_id)
            except sqlite3.Error:
                items_skipped += 1
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                continue
        scope_map: Dict[int, int] = {}
        next_scope_id = 1
        for iid, *_ in items:
            root = uf.find(iid)
            if root not in scope_map:
                scope_map[root] = next_scope_id
                next_scope_id += 1
            conn.execute(
                "UPDATE kb_paragraphs SET scope_id = ? WHERE id = ?",
                (scope_map[root], iid)
            )
        # For each cluster, pick canonical (highest weight × confidence)
        for root, scope_id in scope_map.items():
            members = conn.execute(
                "SELECT id, weight, confidence FROM kb_paragraphs "
                "WHERE scope_id = ? ORDER BY weight * confidence DESC LIMIT 1",
                (scope_id,)
            ).fetchall()
            if members:
                canonical_id = members[0]["id"]
                conn.execute(
                    "UPDATE kb_paragraphs SET canonical_id = ? WHERE scope_id = ?",
                    (canonical_id, scope_id)
                )
        # Phase 4 part 2: link memory_qa → knowledge_principle
        # For each memory_qa, find the best-matching knowledge_principle
        # via FTS5 and set principle_ref.
        principle_refs_linked = 0
        mem_rows = conn.execute(
            "SELECT id, title, body FROM kb_paragraphs WHERE kind = 'memory_qa'"
        ).fetchall()
        for mr in mem_rows:
            query_text = (mr["title"] or "") + " " + (mr["body"] or "")
            if not query_text.strip():
                continue
            try:
                match_expr = _fts5_escape(query_text[:500])
                pr_rows = conn.execute(
                    "SELECT kb_paragraphs.id, -bm25(kb_paragraphs_fts) AS score "
                    "FROM kb_paragraphs_fts JOIN kb_paragraphs "
                    "  ON kb_paragraphs.id = kb_paragraphs_fts.rowid "
                    "WHERE kb_paragraphs_fts MATCH ? "
                    "  AND kb_paragraphs.kind IN ('principle', 'fact') "
                    "ORDER BY score DESC LIMIT 1",
                    (match_expr,)
                ).fetchall()
                if pr_rows and pr_rows[0]["score"] >= threshold:
                    conn.execute(
                        "UPDATE kb_paragraphs SET principle_ref = ? WHERE id = ?",
                        (pr_rows[0]["id"], mr["id"])
                    )
                    principle_refs_linked += 1
            except sqlite3.Error:
                items_skipped += 1
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                continue
        conn.commit()
        cluster_count = len(scope_map)
        items_clustered = sum(1 for r in items)
        if verbose:
            print(f"[kb-cluster] {cluster_count} clusters, "
                  f"{items_clustered} items clustered, "
                  f"{principle_refs_linked} principle_refs linked",
                  file=sys.stderr)
        return {
            "clustered": True,
            "cluster_count": cluster_count,
            "items_clustered": items_clustered,
            "principle_refs_linked": principle_refs_linked,
            "items_skipped": items_skipped,
        }
    finally:
        conn.close()
