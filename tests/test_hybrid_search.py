"""Unit tests for hybrid_search.py.

Hybrid search combines FTS5 BM25 (sparse) with optional dense-vector
embedding similarity and fuses them via Reciprocal Rank Fusion (RRF).

This test suite covers:
- _cosine_similarity: correct values for known vectors + edge cases
- _rrf_fusion: RRF math correctness, short-code penalty, de-dup
- hybrid_search: end-to-end with sparse-only (no embedding provider)
  and with mocked dense channel (verifies RRF path is exercised)
- RRF_K constant: industry-standard value
- SHORT_CODE_PENALTY: down-ranking of < 30-char snippets

This test file exists specifically to prevent regression of the
defaultdict import bug (fixed in P0-1) which made the dense channel
throw NameError whenever an embedding was available.
"""
import json
import math
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


class TestCosineSimilarity(unittest.TestCase):
    """Tests for _cosine_similarity in hybrid_search.

    Note: hybrid_search._cosine_similarity assumes pre-normalized vectors
    (length 1). It returns the dot product clamped to [0, +inf). This is
    intentional — embedding providers (Ollama/ST/OpenAI) all return
    normalized vectors, so the full cosine formula is unnecessary.
    """

    def test_normalized_known_vectors(self):
        from _builder.hybrid_search import _cosine_similarity
        # Pre-normalized: [1,0,0] dot [1/sqrt(2), 1/sqrt(2), 0] = 1/sqrt(2)
        inv_sqrt2 = 1.0 / math.sqrt(2)
        result = _cosine_similarity([1, 0, 0], [inv_sqrt2, inv_sqrt2, 0])
        self.assertAlmostEqual(result, inv_sqrt2, places=6)

    def test_same_normalized_vector_returns_one(self):
        from _builder.hybrid_search import _cosine_similarity
        inv_sqrt3 = 1.0 / math.sqrt(3)
        v = [inv_sqrt3, inv_sqrt3, inv_sqrt3]
        self.assertAlmostEqual(_cosine_similarity(v, v), 1.0, places=6)

    def test_orthogonal_vectors_return_zero(self):
        from _builder.hybrid_search import _cosine_similarity
        # [1,0] and [0,1] are pre-normalized and orthogonal
        self.assertAlmostEqual(_cosine_similarity([1, 0], [0, 1]), 0.0, places=6)

    def test_zero_vector_returns_zero(self):
        from _builder.hybrid_search import _cosine_similarity
        # Zero numerator / zero denominator → return 0 (avoid div by zero)
        self.assertEqual(_cosine_similarity([0, 0, 0], [1, 1, 1]), 0.0)
        self.assertEqual(_cosine_similarity([1, 1, 1], [0, 0, 0]), 0.0)

    def test_mismatched_lengths_return_zero(self):
        from _builder.hybrid_search import _cosine_similarity
        self.assertEqual(_cosine_similarity([1, 2], [1, 2, 3]), 0.0)

    def test_empty_vectors_return_zero(self):
        from _builder.hybrid_search import _cosine_similarity
        self.assertEqual(_cosine_similarity([], [1, 2, 3]), 0.0)
        self.assertEqual(_cosine_similarity([1, 2, 3], []), 0.0)

    def test_negative_dot_product_clamped_to_zero(self):
        """If pre-normalized vectors point in opposite directions, return 0."""
        from _builder.hybrid_search import _cosine_similarity
        # [1,0] dot [-1,0] = -1 → max(0.0, -1) = 0
        self.assertEqual(_cosine_similarity([1, 0], [-1, 0]), 0.0)


class TestRRFFusion(unittest.TestCase):
    """Tests for Reciprocal Rank Fusion.

    RRF score = sum( 1 / (k + rank_i + 1) ) for each result list i.
    k=60 (industry standard from Cormack et al. 2009).
    """

    def test_rrf_constant_is_60(self):
        from _builder.hybrid_search import RRF_K
        self.assertEqual(RRF_K, 60)

    def test_short_code_constants(self):
        from _builder.hybrid_search import SHORT_CODE_THRESHOLD, SHORT_CODE_PENALTY
        self.assertEqual(SHORT_CODE_THRESHOLD, 30)
        self.assertEqual(SHORT_CODE_PENALTY, 0.5)

    def test_rrf_fusion_does_not_raise_nameerror(self):
        """Regression test for P0-1: defaultdict import was missing.

        Before the fix, _rrf_fusion would throw
        NameError: name 'defaultdict' is not defined
        whenever the dense channel returned any results. This test runs
        the fusion path with non-empty dense_results to verify it works.
        """
        from _builder.hybrid_search import _rrf_fusion
        sparse = [
            {"id": "a", "title": "alpha", "body": "x" * 100},
            {"id": "b", "title": "beta", "body": "y" * 100},
        ]
        dense = [
            {"id": "b", "title": "beta", "body": "y" * 100},
            {"id": "c", "title": "gamma", "body": "z" * 100},
        ]
        # Must not raise
        result = _rrf_fusion(sparse, dense, limit=10)
        self.assertEqual(len(result), 3)

    def test_rrf_fusion_score_computation(self):
        """Verify the RRF score formula: 1/(k+rank+1) summed across lists."""
        from _builder.hybrid_search import _rrf_fusion, RRF_K
        sparse = [{"id": "a", "body": "x" * 100}]   # rank 0
        dense = [{"id": "a", "body": "x" * 100}]    # rank 0
        result = _rrf_fusion(sparse, dense, limit=5)
        # a appears at rank 0 in both lists → 2 * (1/(60+0+1))
        expected = 2 * (1.0 / (RRF_K + 1))
        self.assertAlmostEqual(result[0]["rrf_score"], expected, places=6)

    def test_rrf_fusion_keeps_best_result_per_id(self):
        """When the same id appears in both lists, the first-seen result dict
        is kept (with attributes from whichever list was processed first)."""
        from _builder.hybrid_search import _rrf_fusion
        sparse = [{"id": "x", "title": "from_sparse", "body": "y" * 100}]
        dense = [{"id": "x", "title": "from_dense", "body": "z" * 100}]
        result = _rrf_fusion(sparse, dense, limit=5)
        self.assertEqual(len(result), 1)
        # Sparse was processed first → its title is kept
        self.assertEqual(result[0]["title"], "from_sparse")

    def test_rrf_fusion_short_code_penalty_applied(self):
        """Results with body < SHORT_CODE_THRESHOLD chars get penalized."""
        from _builder.hybrid_search import _rrf_fusion, SHORT_CODE_PENALTY
        sparse = [{"id": "short", "body": "abc"}]  # 3 chars < 30 threshold
        dense = [{"id": "long", "body": "x" * 100}]  # 100 chars
        result = _rrf_fusion(sparse, dense, limit=5)
        # Find each
        short_r = next(r for r in result if r["id"] == "short")
        long_r = next(r for r in result if r["id"] == "long")
        # Short result should have its score multiplied by 0.5
        # Both at rank 0 in their respective lists → same base score
        self.assertAlmostEqual(
            short_r["rrf_score"] / long_r["rrf_score"], SHORT_CODE_PENALTY, places=6)

    def test_rrf_fusion_limit_respected(self):
        from _builder.hybrid_search import _rrf_fusion
        sparse = [{"id": str(i), "body": "x" * 100} for i in range(10)]
        dense = [{"id": str(i), "body": "y" * 100} for i in range(10, 20)]
        result = _rrf_fusion(sparse, dense, limit=5)
        self.assertEqual(len(result), 5)

    def test_rrf_fusion_empty_inputs(self):
        from _builder.hybrid_search import _rrf_fusion
        self.assertEqual(_rrf_fusion([], [], limit=5), [])
        # Only sparse
        sparse = [{"id": "a", "body": "x" * 100}]
        result = _rrf_fusion(sparse, [], limit=5)
        self.assertEqual(len(result), 1)
        # Only dense
        result = _rrf_fusion([], sparse, limit=5)
        self.assertEqual(len(result), 1)


def _make_kb_db(graph_dir: str, paragraphs: list = None) -> str:
    """Create a real kb_paragraphs + FTS5 db at graph_dir/code2database.db.

    Uses the actual _kb_connect() helper so the schema matches production
    (triggers, indexes, FTS5 tokenizer settings, etc.). Avoids drift
    between the test fixture and the real schema.
    """
    from _builder.kb_index import _kb_connect
    os.makedirs(graph_dir, exist_ok=True)
    db_path = os.path.join(graph_dir, "code2database.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = _kb_connect(graph_dir, create_if_missing=True)
    if conn is None:
        raise RuntimeError("_kb_connect returned None — cannot create test db")
    try:
        if paragraphs is None:
            paragraphs = [
                # (source_kind, source_file, para_index, title, body, tags, kind)
                ("extracted", "stdlib.c", 0, "malloc function",
                 "void* malloc(size_t size) allocates memory of given size",
                 "alloc,memory", "function"),
                ("extracted", "stdlib.c", 1, "free function",
                 "void free(void* ptr) releases allocated memory",
                 "free,memory", "function"),
                ("extracted", "string.c", 0, "memcpy function",
                 "void* memcpy(void* dst, const void* src, size_t n) copies memory",
                 "copy,memory", "function"),
            ]
        for p in paragraphs:
            conn.execute(
                "INSERT INTO kb_paragraphs "
                "(source_kind, source_file, para_index, title, body, tags, "
                " weight, confidence, kind, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1.0, 1.0, ?, datetime('now'))",
                (p[0], p[1], p[2], p[3], p[4], p[5], p[6]),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


class TestHybridSearchEndToEnd(unittest.TestCase):
    """End-to-end tests for hybrid_search with a real FTS5 db."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_hybrid_test_")
        _make_kb_db(self.tmp)

    def test_empty_query_returns_error(self):
        from _builder.hybrid_search import hybrid_search
        result = hybrid_search(self.tmp, "")
        self.assertIn("error", result)
        self.assertEqual(result["error"], "empty query")

    def test_whitespace_query_returns_error(self):
        from _builder.hybrid_search import hybrid_search
        result = hybrid_search(self.tmp, "   ")
        self.assertIn("error", result)

    def test_sparse_only_when_no_embedding_provider(self):
        """When no embedding provider is available, hybrid_search degrades
        gracefully to sparse-only (FTS5 BM25) and returns results."""
        from _builder.hybrid_search import hybrid_search
        # Mock _get_embedding to return None (no provider available)
        with patch("_builder.hybrid_search._get_embedding", return_value=None):
            result = hybrid_search(self.tmp, "malloc", top_n=5)
        self.assertNotIn("error", result)
        self.assertIn("engine", result)
        self.assertEqual(result["engine"], "sparse_only")
        self.assertIn("results", result)
        self.assertGreater(len(result["results"]), 0)
        self.assertEqual(result["channels"]["sparse"], True)
        self.assertEqual(result["channels"]["dense"], False)

    def test_hybrid_when_dense_available(self):
        """When _get_embedding returns a vector, the dense channel is exercised
        and RRF fusion runs (regression test for P0-1 defaultdict bug)."""
        from _builder.hybrid_search import hybrid_search
        # Mock _get_embedding to return a fake 4-dim vector
        with patch("_builder.hybrid_search._get_embedding",
                   return_value=[1.0, 0.0, 0.0, 0.0]):
            result = hybrid_search(self.tmp, "malloc", top_n=5)
        self.assertEqual(result["engine"], "hybrid")
        self.assertTrue(result["channels"]["dense"])

    def test_top_n_respected(self):
        from _builder.hybrid_search import hybrid_search
        with patch("_builder.hybrid_search._get_embedding", return_value=None):
            result = hybrid_search(self.tmp, "memory", top_n=2)
        self.assertLessEqual(len(result["results"]), 2)


if __name__ == "__main__":
    unittest.main()
