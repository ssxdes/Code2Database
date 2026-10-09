"""RAG enhancement layers for kb-query.

Each enhancement is a standalone function that takes the current
search context and returns augmented results.  When the required
external service (LLM, reranker, cgdb) is unavailable, the function
returns the input unchanged — so every enhancement degrades gracefully.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any, Dict, List, Optional
import logging


def _detect_llm_provider() -> str:
    """Detect available LLM provider for HyDE / multi-query."""
    base = os.environ.get("OPENAI_API_BASE") or os.environ.get("LLM_API_BASE")
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    if base and key:
        return "openai"
    return "none"


def _call_llm(prompt: str, max_tokens: int = 300) -> Optional[str]:
    """Call an OpenAI-compatible LLM. Returns None when unavailable."""
    base = os.environ.get("OPENAI_API_BASE") or os.environ.get("LLM_API_BASE",
              "https://api.openai.com/v1")
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    if not key:
        return None
    try:
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{base.rstrip('/')}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        return None


def hyde_expand(query: str) -> Optional[str]:
    """Generate a hypothetical answer for HyDE.

    Returns the LLM-generated text to embed instead of the raw query.
    Returns None when no LLM is available — caller uses the raw query.
    """
    if _detect_llm_provider() == "none":
        return None
    prompt = (
        "You are a code analysis assistant. Given the following question, "
        "write a brief hypothetical answer (2-3 sentences) that would "
        "appear in a knowledge base entry:\n\n"
        f"Question: {query}\n\nAnswer:"
    )
    return _call_llm(prompt, max_tokens=200)


def multi_query_decompose(query: str) -> List[str]:
    """Decompose a complex query into 3-5 sub-queries.

    Returns [query] (the original) when no LLM is available.
    """
    if _detect_llm_provider() == "none":
        return [query]
    prompt = (
        "Decompose the following search query into 3-5 simpler sub-queries, "
        "one per line, no numbering or bullets:\n\n"
        f"Query: {query}\n\nSub-queries:"
    )
    result = _call_llm(prompt, max_tokens=200)
    if not result:
        return [query]
    sub_queries = [line.strip() for line in result.strip().splitlines()
                   if line.strip()]
    return sub_queries if sub_queries else [query]


def rerank(results: List[Dict[str, Any]],
           query: str,
           top_n: int = 20) -> List[Dict[str, Any]]:
    """Re-score results with a cross-encoder reranker.

    Provider priority: bge-reranker-base (local) → /v1/rerank (remote) → skip.
    Returns results unchanged when no provider is available.
    """
    if not results:
        return results
    # Try local sentence-transformers reranker
    try:
        from sentence_transformers import CrossEncoder
        reranker = CrossEncoder("BAAI/bge-reranker-base")
        pairs = [(query, r.get("body", r.get("title", ""))[:500])
                 for r in results]
        scores = reranker.predict(pairs)
        for r, s in zip(results, scores):
            r["rerank_score"] = float(s)
        results.sort(key=lambda r: -r.get("rerank_score", 0))
        return results[:top_n]
    except Exception:
        pass
    # Try remote /v1/rerank
    rerank_url = os.environ.get("C2D_RERANK_URL", "")
    rerank_key = os.environ.get("C2D_RERANK_API_KEY",
                                os.environ.get("OPENAI_API_KEY", ""))
    if rerank_url and rerank_key:
        try:
            docs = [r.get("body", r.get("title", ""))[:500] for r in results]
            payload = json.dumps({"query": query, "documents": docs}).encode("utf-8")
            req = urllib.request.Request(
                rerank_url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {rerank_key}",
                },
                method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                scored = data.get("results", data.get("data", []))
                if isinstance(scored, list):
                    # Build (score, original_index) pairs. Most rerank
                    # APIs (Cohere, Jina, Voyage) return an ``index``
                    # field mapping back to the input position; some
                    # return ``relevance_score`` instead of ``score``.
                    # Fall back to positional correspondence only when
                    # ``index`` is absent.
                    scored_pairs = []
                    for pos, item in enumerate(scored):
                        score = item.get("score",
                                         item.get("relevance_score", 0))
                        orig_idx = item.get("index", pos)
                        if isinstance(orig_idx, int) and \
                                0 <= orig_idx < len(results):
                            scored_pairs.append((float(score), orig_idx))
                    scored_pairs.sort(key=lambda p: -p[0])
                    return [results[idx] for _, idx in scored_pairs[:top_n]]
        except Exception:
            pass
    # No reranker available — return unchanged
    return results[:top_n]


def graph_walk(results: List[Dict[str, Any]],
               graph_dir: str,
               max_hops: int = 2) -> List[Dict[str, Any]]:
    """Expand kb-query hits along the code graph.

    For each hit with ``node_ids``, traverses 1-2 hops in the cgdb
    graph and attaches the expanded node descriptions as
    ``graph_context`` on the result.  Returns results unchanged when
    no cgdb store is available.
    """
    c2d_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.exists(c2d_path):
        return results
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{c2d_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            for r in results:
                node_ids_raw = r.get("node_ids")
                if isinstance(node_ids_raw, str):
                    try:
                        node_ids = json.loads(node_ids_raw)
                    except (json.JSONDecodeError, TypeError):
                        node_ids = []
                elif isinstance(node_ids_raw, list):
                    node_ids = node_ids_raw
                else:
                    node_ids = []
                if not node_ids:
                    continue
                context_nodes = []
                for nid in node_ids[:5]:
                    row = conn.execute(
                        "SELECT name, description FROM cgdb_nodes "
                        "WHERE id = ? OR legacy_function_id = ?",
                        (nid, nid)).fetchone()
                    if row and row["description"]:
                        context_nodes.append({
                            "node": row["name"],
                            "description": row["description"][:200],
                        })
                if context_nodes:
                    r["graph_context"] = context_nodes
        finally:
            conn.close()
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
    return results


def two_stage_retrieve(conn, query_embedding: List[float],
                       sparse_results: List[Dict[str, Any]],
                       top_n: int = 20) -> Optional[List[Dict[str, Any]]]:
    """Two-stage retrieval: match cluster summaries first, then drill down.

    Returns None when kb_cluster_summaries table is empty or missing —
    caller falls back to single-stage retrieval.
    """
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM kb_cluster_summaries").fetchone()[0]
        if count == 0:
            return None
    except Exception:
        return None
    # Embed each cluster summary and find top-3 clusters
    from _builder.kb.kb_index import query_ann
    from _builder.kb.neural_embed import get_embedding, cosine_similarity
    if query_embedding is None:
        return None
    try:
        summaries = conn.execute(
            "SELECT cluster_id, summary FROM kb_cluster_summaries"
        ).fetchall()
        scored_clusters = []
        for s in summaries:
            emb = get_embedding(s["summary"][:500])
            if emb:
                sim = cosine_similarity(query_embedding, emb)
                scored_clusters.append((sim, s["cluster_id"]))
        scored_clusters.sort(key=lambda x: -x[0])
        top_clusters = [c[1] for c in scored_clusters[:3]]
        if not top_clusters:
            return None
        placeholders = ",".join("?" for _ in top_clusters)
        rows = conn.execute(
            f"SELECT id, source_kind, source_file, title, body, tags, "
            f"weight, kind, version_scope FROM kb_paragraphs "
            f"WHERE scope_id IN ({placeholders}) "
            f"ORDER BY weight DESC LIMIT ?",
            top_clusters + [top_n * 2]
        ).fetchall()
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
                "source_kind": r["source_kind"],
                "source_file": r["source_file"],
                "title": r["title"] or "",
                "body": r["body"] or "",
                "tags": tags or [],
                "weight": r["weight"],
                "kind": r["kind"],
                "version_scope": r["version_scope"],
                "two_stage": True,
            })
        return results if results else None
    except Exception:
        return None
