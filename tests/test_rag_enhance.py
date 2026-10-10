"""Tests for rag_enhance.py — RAG enhancement layers.

Each enhancement must degrade gracefully when the required external
service is unavailable.  These tests verify both the happy path (when
the service is present) and the degradation path (when it is not).
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from _builder.kb.kb_index import _kb_connect, upsert_kb_paragraph
from _builder.kb.rag_enhance import (two_stage_retrieve, rerank,
                                      graph_walk, hyde_expand,
                                      multi_query_decompose)


class TestTwoStageRetrieve(unittest.TestCase):
    """two_stage_retrieve returns cluster-filtered results when
    kb_cluster_summaries has rows, None when the table is empty."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.graph_dir = self.tmp.name
        self.conn = _kb_connect(self.graph_dir)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _add_paragraph(self, title, body, scope_id=None, weight=1.0):
        cur = self.conn.execute(
            "INSERT INTO kb_paragraphs "
            "(source_kind, source_file, para_index, title, body, "
            " body_tokenized, weight, confidence, kind, created_at, "
            " access_count, version_scope) "
            "VALUES (?, 'test.json', 0, ?, ?, ?, ?, 1.0, 'knowledge', "
            " datetime('now'), 0, 'default')",
            ("memory", title, body, body, weight))
        if scope_id is not None:
            self.conn.execute(
                "UPDATE kb_paragraphs SET scope_id = ? WHERE id = ?",
                (scope_id, cur.lastrowid))
        self.conn.commit()
        return cur.lastrowid

    def _add_summary(self, cluster_id, summary):
        self.conn.execute(
            "INSERT INTO kb_cluster_summaries "
            "(cluster_id, summary, generated_at, model, token_count) "
            "VALUES (?, ?, datetime('now'), 'test', 10)",
            (cluster_id, summary))
        self.conn.commit()

    def test_returns_none_when_no_summaries(self):
        self._add_paragraph("release memory", "how to release memory", 1)
        result = two_stage_retrieve(self.conn, [0.1, 0.2, 0.3], [], top_n=10)
        self.assertIsNone(result)

    def test_returns_none_when_no_query_embedding(self):
        self._add_paragraph("release memory", "how to release memory", 1)
        self._add_summary(1, "memory management cluster")
        result = two_stage_retrieve(self.conn, None, [], top_n=10)
        self.assertIsNone(result)

    def test_returns_results_when_summaries_exist(self):
        self._add_paragraph("release memory", "how to release memory", 1)
        self._add_paragraph("thread safety", "ensure thread safety", 2)
        self._add_summary(1, "memory management")
        self._add_summary(2, "thread safety")
        fake_emb = [0.5] * 384
        with patch("_builder.kb.kb_index.query_ann", return_value=[]), \
             patch("_builder.kb.neural_embed.get_embedding",
                   return_value=fake_emb), \
             patch("_builder.kb.neural_embed.cosine_similarity",
                   return_value=0.9):
            result = two_stage_retrieve(self.conn, fake_emb, [], top_n=10)
        self.assertIsNotNone(result)
        self.assertGreater(len(result), 0)


class TestRerankGracefulDegradation(unittest.TestCase):
    """rerank returns results unchanged when no reranker is available."""

    def test_empty_results_unchanged(self):
        self.assertEqual(rerank([], "query"), [])

    def test_no_provider_returns_results_unchanged(self):
        results = [{"id": 1, "body": "a", "score": 0.5},
                   {"id": 2, "body": "b", "score": 0.3}]
        with patch.dict(os.environ, {"C2D_RERANK_URL": "",
                                      "C2D_RERANK_API_KEY": "",
                                      "OPENAI_API_KEY": ""}):
            out = rerank(results, "query", top_n=10)
        self.assertEqual(len(out), 2)

    def test_remote_api_uses_index_field_for_ordering(self):
        """The rerank API may return results in a different order than
        the input. The ``index`` field maps each scored item back to
        the original input position."""
        results = [{"id": 10, "body": "alpha"},
                   {"id": 20, "body": "beta"},
                   {"id": 30, "body": "gamma"}]
        # API returns gamma (index=2) as best, alpha (index=0) second
        api_response = {
            "results": [
                {"index": 2, "relevance_score": 0.95},
                {"index": 0, "relevance_score": 0.80},
                {"index": 1, "relevance_score": 0.30},
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_response).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch.dict(os.environ, {"C2D_RERANK_URL": "http://mock/rerank",
                                      "C2D_RERANK_API_KEY": "key"}), \
             patch("urllib.request.urlopen", return_value=mock_resp):
            out = rerank(results, "query", top_n=3)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["id"], 30)  # gamma was best
        self.assertEqual(out[1]["id"], 10)  # alpha was second
        self.assertEqual(out[2]["id"], 20)  # beta was third


class TestGraphWalkDegradation(unittest.TestCase):
    """graph_walk returns results unchanged when no cgdb store exists."""

    def test_no_cgdb_store(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            results = [{"id": 1, "body": "a", "node_ids": ["n1"]}]
            out = graph_walk(results, tmp.name)
            self.assertEqual(len(out), 1)
        finally:
            tmp.cleanup()

    def test_db_without_cgdb_nodes_table(self):
        """code2database.db exists (tree-sitter backend) but cgdb_nodes
        table is absent — graph_walk returns results unchanged."""
        import sqlite3
        tmp = tempfile.TemporaryDirectory()
        try:
            db_path = os.path.join(tmp.name, "code2database.db")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE functions(id INTEGER, name TEXT)")
            conn.commit()
            conn.close()
            results = [{"id": 1, "body": "a", "node_ids": [1]}]
            out = graph_walk(results, tmp.name)
            self.assertEqual(len(out), 1)
            self.assertNotIn("graph_context", out[0])
        finally:
            tmp.cleanup()


class TestHydeDegradation(unittest.TestCase):

    def test_returns_none_when_no_llm(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "",
                                      "LLM_API_KEY": "",
                                      "OPENAI_API_BASE": "",
                                      "LLM_API_BASE": ""}):
            self.assertIsNone(hyde_expand("how to release memory"))


class TestMultiQueryDecomposition(unittest.TestCase):

    def test_returns_original_when_no_llm(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "",
                                      "LLM_API_KEY": "",
                                      "OPENAI_API_BASE": "",
                                      "LLM_API_BASE": ""}):
            result = multi_query_decompose("how to release memory")
            self.assertEqual(result, ["how to release memory"])


if __name__ == "__main__":
    unittest.main()
