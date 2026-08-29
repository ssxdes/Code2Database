"""Neural embedding provider — extends embeddings.py with real semantic search.

Supports 3 providers, all optional:
1. Ollama (local server, default if running)
2. sentence-transformers (pip install, lazy import)
3. OpenAI-compatible API (remote)

Falls back to existing TF-IDF char-n-gram if no provider available.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Dict, List, Optional
import logging

# Provider type
EMBEDDING_PROVIDER = os.environ.get("C2D_EMBEDDING_PROVIDER", "auto")  # auto|ollama|st|openai|none

# Ollama config
OLLAMA_URL = os.environ.get("C2D_OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("C2D_OLLAMA_MODEL", "nomic-embed-text")

# OpenAI-compatible config
OPENAI_URL = os.environ.get("C2D_OPENAI_URL", "https://api.openai.com/v1/embeddings")
OPENAI_MODEL = os.environ.get("C2D_OPENAI_MODEL", "text-embedding-3-small")
OPENAI_API_KEY = os.environ.get("C2D_OPENAI_API_KEY", "")

# sentence-transformers model
ST_MODEL = os.environ.get("C2D_ST_MODEL", "all-MiniLM-L6-v2")

_ST_MODEL_CACHE = None


def _detect_provider() -> str:
    """Auto-detect available embedding provider."""
    if EMBEDDING_PROVIDER != "auto":
        return EMBEDDING_PROVIDER

    # Try Ollama first (local, no pip install needed)
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                return "ollama"
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    try:
        import sentence_transformers
        return "st"
    except ImportError:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    if OPENAI_API_KEY:
        return "openai"

    return "none"


def _ollama_embed(text: str) -> Optional[List[float]]:
    """Get embedding from local Ollama server."""
    try:
        payload = json.dumps({"model": OLLAMA_MODEL, "prompt": text}).encode("utf-8")
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data.get("embedding")
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        return None


def _st_embed(text: str) -> Optional[List[float]]:
    """Get embedding from sentence-transformers (local model)."""
    global _ST_MODEL_CACHE
    try:
        from sentence_transformers import SentenceTransformer
        if _ST_MODEL_CACHE is None:
            _ST_MODEL_CACHE = SentenceTransformer(ST_MODEL)
        emb = _ST_MODEL_CACHE.encode(text, normalize_embeddings=True)
        return emb.tolist()
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        return None


def _openai_embed(text: str) -> Optional[List[float]]:
    """Get embedding from OpenAI-compatible API."""
    try:
        payload = json.dumps({"model": OPENAI_MODEL, "input": text}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}",
        }
        req = urllib.request.Request(OPENAI_URL, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data["data"][0]["embedding"]
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        return None


def get_embedding(text: str) -> Optional[List[float]]:
    """Get neural embedding for text. Returns None if no provider available.

    Auto-detects provider: Ollama > sentence-transformers > OpenAI > None.
    """
    provider = _detect_provider()
    if provider == "ollama":
        return _ollama_embed(text)
    if provider == "st":
        return _st_embed(text)
    if provider == "openai":
        return _openai_embed(text)
    return None


def get_embedding_batch(texts: List[str]) -> List[Optional[List[float]]]:
    """Batch embedding. Falls back to per-item if batch API unavailable."""
    provider = _detect_provider()
    if provider == "st":
        try:
            global _ST_MODEL_CACHE
            from sentence_transformers import SentenceTransformer
            if _ST_MODEL_CACHE is None:
                _ST_MODEL_CACHE = SentenceTransformer(ST_MODEL)
            embs = _ST_MODEL_CACHE.encode(texts, normalize_embeddings=True, batch_size=32)
            return [e.tolist() for e in embs]
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    return [get_embedding(t) for t in texts]


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, dot / (na * nb))


def semantic_search(graph_dir: str, query: str, top_n: int = 20) -> Dict[str, Any]:
    """Semantic search using neural embeddings + RRF with FTS5.

    If no neural provider available, falls back to FTS5 BM25 only.
    """
    # Get query embedding
    query_emb = get_embedding(query)
    provider = _detect_provider() if query_emb else "none"

    # Sparse channel: FTS5 BM25
    from _builder.kb_index import query_kb
    sparse = query_kb(graph_dir, query, top_n=top_n * 3, log_query=False, max_tokens=2000)

    if query_emb is None:
        return {
            "query": query, "results": sparse[:top_n], "engine": "sparse_only",
            "provider": "none",
        }

    # Dense channel: cosine similarity against kb_paragraphs embeddings
    # (this is O(N) for now; k-d tree optimization is a future enhancement)
    from _builder.kb_index import _kb_connect
    conn = _kb_connect(graph_dir)
    dense = []
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT id, title, body, source_kind, source_file, weight, kind "
                "FROM kb_paragraphs LIMIT 10000"
            ).fetchall()
            for r in rows:
                text = (r["title"] or "") + " " + (r["body"] or "")
                emb = get_embedding(text[:500])  # truncate for speed
                if emb is not None:
                    sim = cosine_similarity(query_emb, emb)
                    if sim > 0.1:
                        dense.append({
                            "id": r["id"],
                            "source_kind": r["source_kind"],
                            "source_file": r["source_file"],
                            "title": r["title"] or "",
                            "body": (r["body"] or "")[:300],
                            "weight": r["weight"],
                            "kind": r["kind"],
                            "score": sim,
                        })
        finally:
            conn.close()

    # RRF fusion
    K = 60
    rrf: Dict[str, float] = {}
    best: Dict[str, dict] = {}
    for rank, r in enumerate(sparse):
        key = str(r.get("id", r.get("title", "")))
        rrf[key] = rrf.get(key, 0) + 1.0 / (K + rank + 1)
        if key not in best:
            best[key] = r
    for rank, r in enumerate(dense):
        key = str(r.get("id", r.get("title", "")))
        rrf[key] = rrf.get(key, 0) + 1.0 / (K + rank + 1)
        if key not in best:
            best[key] = r

    sorted_keys = sorted(rrf.keys(), key=lambda k: -rrf[k])
    results = []
    for key in sorted_keys[:top_n]:
        r = best[key].copy()
        r["rrf_score"] = rrf[key]
        results.append(r)

    return {
        "query": query, "results": results, "engine": "hybrid",
        "provider": provider,
        "channels": {"sparse": True, "dense": len(dense) > 0},
    }


def cmd_semantic_search(args):
    """CLI handler: semantic-search."""
    result = semantic_search(args.graph, args.query, top_n=getattr(args, "top", 20))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
