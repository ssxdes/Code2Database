"""Hybrid search — RRF fusion of FTS5 keyword + optional vector embedding.

Fills the semantic search gap identified by comparing with codeseek
(BM25 + dense ANN + RRF fusion + cross-encoder reranker) and
SocratiCode (Qdrant + Ollama).

Design:
- Sparse channel: existing kb_paragraphs_fts FTS5 BM25 (always available)
- Dense channel: optional sentence-transformers embeddings (lazy, cached)
- Fusion: Reciprocal Rank Fusion (k=60, industry standard)
- Reranker: optional OpenAI-compatible /v1/rerank endpoint
- Graceful degradation: dense fails → sparse-only; reranker fails → RRF

No required new dependencies. sentence-transformers and requests
are optional (imported lazily, caught if missing).
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Dict, List, Optional

from _builder.kb_index import _kb_connect, query_kb
import logging

# RRF constant (industry standard, from Cormack et al. 2009)
RRF_K = 60

# Short-code penalty: down-rank candidates < 30 chars (from codeseek)
SHORT_CODE_THRESHOLD = 30
SHORT_CODE_PENALTY = 0.5


# Embedding cache: node_id → list[float]
_EMBEDDING_CACHE: Dict[str, List[float]] = {}


def _get_embedding(text: str) -> Optional[List[float]]:
    """Get vector embedding for text. Returns None if unavailable."""
    if text in _EMBEDDING_CACHE:
        return _EMBEDDING_CACHE[text]
    try:
        from sentence_transformers import SentenceTransformer
        model = _get_model()
        if model is None:
            return None
        emb = model.encode(text, normalize_embeddings=True).tolist()
        _EMBEDDING_CACHE[text] = emb
        return emb
    except ImportError:
        return None
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        return None


_MODEL = None


def _get_model():
    """Lazy-load sentence-transformers model."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    try:
        from sentence_transformers import SentenceTransformer
        # Use a small, fast model that's good for code search
        _MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        return _MODEL
    except ImportError:
        return None
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        return None


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two normalized vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return max(0.0, dot)  # Already normalized


def _rrf_fusion(sparse_results: List[Dict], dense_results: List[Dict],
                 limit: int = 20) -> List[Dict]:
    """Reciprocal Rank Fusion (RRF) of sparse + dense results.

    RRF score = sum(1 / (k + rank_i)) for each result list i.
    k=60 is the industry-standard constant.
    """
    rrf_scores: Dict[str, float] = defaultdict(float)
    best_result: Dict[str, Dict] = {}

    for rank, r in enumerate(sparse_results):
        key = r.get("id") or r.get("title", "")
        rrf_scores[key] += 1.0 / (RRF_K + rank + 1)
        if key not in best_result:
            best_result[key] = r

    for rank, r in enumerate(dense_results):
        key = r.get("id") or r.get("title", "")
        rrf_scores[key] += 1.0 / (RRF_K + rank + 1)
        if key not in best_result:
            best_result[key] = r

    # Sort by RRF score
    sorted_keys = sorted(rrf_scores.keys(), key=lambda k: -rrf_scores[k])
    results = []
    for key in sorted_keys[:limit]:
        r = best_result[key].copy()
        r["rrf_score"] = rrf_scores[key]
        # Apply short-code penalty
        body = r.get("body", "")
        if len(body) < SHORT_CODE_THRESHOLD:
            r["rrf_score"] *= SHORT_CODE_PENALTY
        results.append(r)
    return results


def hybrid_search(graph_dir: str, query: str, top_n: int = 20,
                  semantic: bool = True, reranker: str = "") -> Dict[str, Any]:
    """Hybrid search: FTS5 BM25 (sparse) + optional embedding (dense) + RRF fusion.

    Args:
        graph_dir: Path to the C2D graph directory.
        query: Free-form text query.
        top_n: Max results.
        semantic: Try dense (embedding) channel if available.
        reranker: Optional OpenAI-compatible reranker URL
                  (e.g., "http://localhost:8080/v1/rerank").

    Returns:
        {query, results: [...], engine: "hybrid"|"sparse_only"|"sparse+rerank",
         channels: {sparse: bool, dense: bool, reranker: bool}}
    """
    if not query or not query.strip():
        return {"error": "empty query"}

    channels = {"sparse": True, "dense": False, "reranker": False}

    # 1. Sparse channel: FTS5 BM25 (always available)
    sparse = query_kb(graph_dir, query, top_n=top_n * 3, log_query=False, max_tokens=2000)
    if not sparse:
        sparse = []

    # 2. Dense channel: optional embedding similarity
    dense = []
    if semantic:
        query_emb = _get_embedding(query)
        if query_emb is not None:
            channels["dense"] = True
            # Compute similarity against all kb_paragraphs
            conn = _kb_connect(graph_dir)
            if conn is not None:
                try:
                    rows = conn.execute(
                        "SELECT id, title, body, tags, weight, kind, "
                        "source_kind, source_file FROM kb_paragraphs LIMIT 10000"
                    ).fetchall()
                    scored = []
                    for r in rows:
                        # Build text to embed
                        text = (r["title"] or "") + " " + (r["body"] or "")
                        emb = _get_embedding(text)
                        if emb is not None:
                            sim = _cosine_similarity(query_emb, emb)
                            scored.append({
                                "id": r["id"],
                                "title": r["title"] or "",
                                "body": (r["body"] or "")[:500],
                                "source_kind": r["source_kind"],
                                "source_file": r["source_file"],
                                "weight": r["weight"],
                                "kind": r["kind"],
                                "score": sim,
                            })
                    scored.sort(key=lambda x: -x["score"])
                    dense = scored[:top_n * 3]
                finally:
                    conn.close()

    # 3. RRF fusion
    if channels["dense"]:
        fused = _rrf_fusion(sparse, dense, limit=top_n)
        engine = "hybrid"
    else:
        fused = sparse[:top_n]
        engine = "sparse_only"

    # 4. Optional reranker
    if reranker and fused:
        try:
            import urllib.request
            documents = [r.get("body", r.get("title", "")) for r in fused]
            payload = json.dumps({
                "model": "reranker",
                "query": query,
                "documents": documents,
                "top_n": top_n,
            }).encode("utf-8")
            req = urllib.request.Request(
                reranker, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                reranked_data = json.loads(resp.read())
            if "results" in reranked_data:
                # Re-order fused results by reranker scores
                score_map = {r["index"]: r["relevance_score"]
                             for r in reranked_data["results"]}
                for i, r in enumerate(fused):
                    r["rerank_score"] = score_map.get(i, 0)
                fused.sort(key=lambda x: -x.get("rerank_score", 0))
                fused = fused[:top_n]
                channels["reranker"] = True
                engine = "hybrid+rerank"
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass

    return {
        "query": query,
        "results": fused[:top_n],
        "total": len(fused),
        "engine": engine,
        "channels": channels,
    }


def cmd_hybrid_search(args):
    """CLI handler: hybrid-search."""
    result = hybrid_search(
        graph_dir=args.graph,
        query=args.query,
        top_n=getattr(args, "top", 20),
        semantic=getattr(args, "no_semantic", False) is False,
        reranker=getattr(args, "reranker", "") or "",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
