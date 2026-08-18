#!/usr/bin/env python3
"""Lightweight TF-IDF char n-gram embeddings for semantic search.

Replaces pure keyword matching with character n-gram similarity. This lets
"哪个函数释放了 pid 的内存" match `free_pid` even when no exact keyword
overlaps — the n-gram decomposition captures phonetic/shape similarity.

Design:
- Char n-grams (n=3 default) for subword matching.
- TF-IDF weighting: rare n-grams matter more.
- Cosine similarity for ranking.
- Sparse dict-based vectors (no numpy dependency).
- Persisted to <graph_dir>/embeddings.json for reuse.

CLI commands:
    embeddings-build --graph <dir>   # build and persist embeddings
    embeddings-search --graph <dir> --query <text>  # cosine similarity search
"""
import json
import math
import os
import re
from collections import defaultdict
from typing import Dict, List, Tuple, Optional


DEFAULT_NGRAM_SIZE = 3
DEFAULT_MAX_NGRAMS = 5000  # cap vocabulary size to bound memory


def _char_ngrams(text: str, n: int = DEFAULT_NGRAM_SIZE) -> List[str]:
    """Decompose text into char n-grams (with word boundaries).

    "free_pid" with n=3 → ["$fr", "fre", "ree", "ee_", "e_p", "_pi", "pid", "id$"]
    where $ marks word boundaries to distinguish start/end n-grams.
    """
    if not text:
        return []
    # Normalize: lowercase, replace non-alphanumeric with space, collapse spaces
    norm = re.sub(r'[^a-zA-Z0-9_]+', ' ', text.lower()).strip()
    if not norm:
        return []
    # Add boundary markers per word
    ngrams = []
    for word in norm.split():
        if not word:
            continue
        # Pad with boundary chars
        padded = "$" + word + "$"
        if len(padded) < n + 1:
            # Short word — just use the whole padded form
            ngrams.append(padded)
            continue
        for i in range(len(padded) - n + 1):
            ngrams.append(padded[i:i + n])
    return ngrams


def _build_vocab(corpus: List[str], n: int = DEFAULT_NGRAM_SIZE,
                  max_size: int = DEFAULT_MAX_NGRAMS) -> Dict[str, int]:
    """Build a vocabulary of n-grams with document frequencies.

    Returns {ngram: doc_freq} where doc_freq is the number of documents
    containing the ngram at least once.
    """
    doc_freq = defaultdict(int)
    for doc in corpus:
        seen = set()
        for ng in _char_ngrams(doc, n):
            if ng not in seen:
                seen.add(ng)
                doc_freq[ng] += 1
    # Sort by doc_freq descending and cap vocabulary
    sorted_ng = sorted(doc_freq.items(), key=lambda x: -x[1])
    if max_size > 0 and len(sorted_ng) > max_size:
        sorted_ng = sorted_ng[:max_size]
    return dict(sorted_ng)


def _tfidf_vector(text: str, vocab_idf: Dict[str, float],
                    n: int = DEFAULT_NGRAM_SIZE) -> Dict[str, float]:
    """Compute the TF-IDF vector for a single text.

    Returns a sparse dict {ngram: weight}.
    """
    ngrams = _char_ngrams(text, n)
    if not ngrams:
        return {}
    # Term frequency
    tf = defaultdict(int)
    for ng in ngrams:
        tf[ng] += 1
    # TF-IDF: tf * idf (only for ngrams in vocab)
    vec = {}
    total = len(ngrams)
    for ng, count in tf.items():
        idf = vocab_idf.get(ng)
        if idf is None:
            continue
        # L1-normalized TF * IDF
        vec[ng] = (count / total) * idf
    return vec


def _cosine_similarity(v1: Dict[str, float],
                        v2: Dict[str, float]) -> float:
    """Compute cosine similarity between two sparse dict vectors."""
    if not v1 or not v2:
        return 0.0
    # Iterate over the smaller vector
    if len(v1) > len(v2):
        v1, v2 = v2, v1
    dot = 0.0
    for k, val in v1.items():
        if k in v2:
            dot += val * v2[k]
    if dot == 0.0:
        return 0.0
    norm1 = math.sqrt(sum(v * v for v in v1.values()))
    norm2 = math.sqrt(sum(v * v for v in v2.values()))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    return dot / (norm1 * norm2)


class NGramEmbeddings:
    """Manages TF-IDF char n-gram embeddings for a corpus."""

    def __init__(self, n: int = DEFAULT_NGRAM_SIZE,
                 max_vocab: int = DEFAULT_MAX_NGRAMS):
        self.n = n
        self.max_vocab = max_vocab
        self.vocab_idf: Dict[str, float] = {}
        self.doc_vectors: Dict[str, Dict[str, float]] = {}

    def fit(self, corpus: Dict[str, str]) -> None:
        """Build the vocabulary and document vectors.

        Args:
            corpus: {doc_id: text} where text is the searchable content
                    (e.g., function name + description).
        """
        if not corpus:
            return
        texts = list(corpus.values())
        doc_freq = _build_vocab(texts, n=self.n, max_size=self.max_vocab)
        N = len(texts)
        # IDF = log(N / df) with smoothing
        self.vocab_idf = {
            ng: math.log((N + 1) / (df + 1)) + 1.0
            for ng, df in doc_freq.items()
        }
        self.doc_vectors = {}
        for doc_id, text in corpus.items():
            self.doc_vectors[doc_id] = _tfidf_vector(text, self.vocab_idf, self.n)

    def search(self, query: str, top_k: int = 10) -> List[Tuple[str, float]]:
        """Return top_k (doc_id, similarity) pairs for the query."""
        if not self.vocab_idf or not self.doc_vectors:
            return []
        q_vec = _tfidf_vector(query, self.vocab_idf, self.n)
        if not q_vec:
            return []
        scores = []
        for doc_id, doc_vec in self.doc_vectors.items():
            sim = _cosine_similarity(q_vec, doc_vec)
            if sim > 0:
                scores.append((doc_id, sim))
        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]

    def similarity(self, query: str, doc_id: str) -> float:
        """Cosine similarity between query and a specific document."""
        if doc_id not in self.doc_vectors:
            return 0.0
        q_vec = _tfidf_vector(query, self.vocab_idf, self.n)
        return _cosine_similarity(q_vec, self.doc_vectors[doc_id])

    def save(self, path: str) -> None:
        """Save embeddings to a JSON file."""
        data = {
            "n": self.n,
            "max_vocab": self.max_vocab,
            "vocab_idf": self.vocab_idf,
            "doc_vectors": self.doc_vectors,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    @classmethod
    def load(cls, path: str) -> Optional["NGramEmbeddings"]:
        """Load embeddings from a JSON file. Returns None if file missing."""
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            emb = cls(n=data.get("n", DEFAULT_NGRAM_SIZE),
                       max_vocab=data.get("max_vocab", DEFAULT_MAX_NGRAMS))
            emb.vocab_idf = data.get("vocab_idf", {})
            emb.doc_vectors = data.get("doc_vectors", {})
            return emb
        except (json.JSONDecodeError, KeyError, ValueError):
            return None


def build_embeddings_for_graph(graph_dir: str) -> Optional[NGramEmbeddings]:
    """Build TF-IDF embeddings from the invocation graph's nodes.

    Reads nodes from the master JSON and builds a corpus keyed by node ID
    with text = name + signature + semantic_desc.
    """
    master_path = os.path.join(graph_dir, "code2database_master.json")
    if not os.path.exists(master_path):
        return None
    try:
        with open(master_path, "r", encoding="utf-8") as f:
            master = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    corpus = {}
    for node_id, nd in master.get("nodes", {}).items():
        parts = [
            nd.get("name", ""),
            nd.get("signature", ""),
            nd.get("semantic_desc", "") or nd.get("external_desc", ""),
        ]
        text = " ".join(p for p in parts if p)
        if text:
            corpus[node_id] = text
    if not corpus:
        return None
    emb = NGramEmbeddings()
    emb.fit(corpus)
    # Persist to embeddings.json
    emb.save(os.path.join(graph_dir, "embeddings.json"))
    return emb


def get_or_build_embeddings(graph_dir: str) -> Optional[NGramEmbeddings]:
    """Load embeddings from disk, or build them if missing."""
    emb_path = os.path.join(graph_dir, "embeddings.json")
    emb = NGramEmbeddings.load(emb_path)
    if emb is not None:
        return emb
    return build_embeddings_for_graph(graph_dir)


def cmd_embeddings_build(args):
    """CLI handler for embeddings-build."""
    graph_dir = args.graph
    emb = build_embeddings_for_graph(graph_dir)
    if emb is None:
        print(f"Failed to build embeddings: no master graph at {graph_dir}")
        return 1
    print(f"Built embeddings: {len(emb.vocab_idf)} ngrams, "
          f"{len(emb.doc_vectors)} documents")
    print(f"Saved to {os.path.join(graph_dir, 'embeddings.json')}")
    return 0


def cmd_embeddings_search(args):
    """CLI handler for embeddings-search."""
    graph_dir = args.graph
    query = args.query
    top_k = args.top_k
    emb = get_or_build_embeddings(graph_dir)
    if emb is None:
        print(f"No embeddings at {graph_dir}. Run 'embeddings-build' first.")
        return 1
    results = emb.search(query, top_k=top_k)
    if not results:
        print(f"No matches for query: {query!r}")
        return 0
    print(f"Top {len(results)} matches for {query!r}:")
    for doc_id, score in results:
        print(f"  {score:.4f}  {doc_id}")
    return 0
