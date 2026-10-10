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

    def test_graph_walk_attaches_context_when_cgdb_nodes_exist(self):
        """graph_walk attaches graph_context when cgdb_nodes has matching rows."""
        import sqlite3
        tmp = tempfile.TemporaryDirectory()
        try:
            db_path = os.path.join(tmp.name, "code2database.db")
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE cgdb_nodes "
                "(id INTEGER PRIMARY KEY, name TEXT, description TEXT, "
                " legacy_function_id INTEGER)")
            conn.execute(
                "INSERT INTO cgdb_nodes (id, name, description, legacy_function_id) "
                "VALUES (1, 'bdev_register', 'Registers a block device module', 10)")
            conn.execute(
                "INSERT INTO cgdb_nodes (id, name, description, legacy_function_id) "
                "VALUES (2, 'bdev_unregister', 'Unregisters a block device module', 11)")
            conn.commit()
            conn.close()
            results = [
                {"id": 1, "body": "a", "node_ids": [1]},
                {"id": 2, "body": "b", "node_ids": [2]},
                {"id": 3, "body": "c", "node_ids": []},
            ]
            out = graph_walk(results, tmp.name)
            self.assertEqual(len(out), 3)
            self.assertIn("graph_context", out[0])
            self.assertIn("graph_context", out[1])
            self.assertNotIn("graph_context", out[2])
            self.assertEqual(out[0]["graph_context"][0]["node"], "bdev_register")
        finally:
            tmp.cleanup()

    def test_graph_walk_handles_node_ids_as_json_string(self):
        """graph_walk handles node_ids stored as a JSON string."""
        import sqlite3
        tmp = tempfile.TemporaryDirectory()
        try:
            db_path = os.path.join(tmp.name, "code2database.db")
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE cgdb_nodes "
                "(id INTEGER PRIMARY KEY, name TEXT, description TEXT, "
                " legacy_function_id INTEGER)")
            conn.execute(
                "INSERT INTO cgdb_nodes (id, name, description) "
                "VALUES (5, 'test_fn', 'A test function description')")
            conn.commit()
            conn.close()
            results = [{"id": 1, "body": "a", "node_ids": "[5]"}]
            out = graph_walk(results, tmp.name)
            self.assertEqual(len(out), 1)
            self.assertIn("graph_context", out[0])
        finally:
            tmp.cleanup()

    def test_graph_walk_skips_node_not_found(self):
        """graph_walk skips node_ids that don't exist in cgdb_nodes."""
        import sqlite3
        tmp = tempfile.TemporaryDirectory()
        try:
            db_path = os.path.join(tmp.name, "code2database.db")
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE cgdb_nodes "
                "(id INTEGER PRIMARY KEY, name TEXT, description TEXT, "
                " legacy_function_id INTEGER)")
            conn.execute(
                "INSERT INTO cgdb_nodes (id, name, description) "
                "VALUES (1, 'found_fn', 'Found function description')")
            conn.commit()
            conn.close()
            results = [{"id": 1, "body": "a", "node_ids": [1, 999]}]
            out = graph_walk(results, tmp.name)
            self.assertEqual(len(out), 1)
            self.assertIn("graph_context", out[0])
            # Only node 1 was found
            self.assertEqual(len(out[0]["graph_context"]), 1)
        finally:
            tmp.cleanup()


class TestRerankLocalProvider(unittest.TestCase):
    """rerank with a local CrossEncoder reranker (mocked)."""

    def test_local_reranker_reorders_results(self):
        results = [
            {"id": 1, "body": "unrelated content"},
            {"id": 2, "body": "release memory with free()"},
            {"id": 3, "body": "random other text"},
        ]
        mock_reranker = MagicMock()
        mock_reranker.predict.return_value = [0.1, 0.95, 0.2]
        with patch("_builder.kb.rag_enhance._get_reranker",
                   return_value=mock_reranker):
            out = rerank(results, "how to release memory", top_n=3)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["id"], 2)  # highest score
        self.assertIn("rerank_score", out[0])


class TestHydeDegradation(unittest.TestCase):

    def test_returns_none_when_no_llm(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "",
                                      "LLM_API_KEY": "",
                                      "OPENAI_API_BASE": "",
                                      "LLM_API_BASE": ""}):
            self.assertIsNone(hyde_expand("how to release memory"))


class TestHydeHappyPath(unittest.TestCase):

    def test_returns_hypothetical_answer_when_llm_available(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key",
                                      "OPENAI_API_BASE": "http://mock"}):
            with patch("_builder.kb.rag_enhance._call_llm",
                       return_value="Call free() on the allocated pointer."):
                result = hyde_expand("how to release memory")
        self.assertIsNotNone(result)
        self.assertIn("free", result.lower())


class TestMultiQueryDecomposition(unittest.TestCase):

    def test_returns_original_when_no_llm(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "",
                                      "LLM_API_KEY": "",
                                      "OPENAI_API_BASE": "",
                                      "LLM_API_BASE": ""}):
            result = multi_query_decompose("how to release memory")
            self.assertEqual(result, ["how to release memory"])

    def test_decomposes_when_llm_available(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key",
                                      "OPENAI_API_BASE": "http://mock"}):
            with patch("_builder.kb.rag_enhance._call_llm",
                       return_value="how to free memory\n"
                                    "memory deallocation function\n"
                                    "release allocated resources"):
                result = multi_query_decompose("how to release memory")
        self.assertEqual(len(result), 3)
        self.assertIn("free memory", result[0])

    def test_returns_original_when_llm_returns_empty(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key",
                                      "OPENAI_API_BASE": "http://mock"}):
            with patch("_builder.kb.rag_enhance._call_llm",
                       return_value=""):
                result = multi_query_decompose("how to release memory")
        self.assertEqual(result, ["how to release memory"])

    def test_returns_original_when_llm_returns_whitespace(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key",
                                      "OPENAI_API_BASE": "http://mock"}):
            with patch("_builder.kb.rag_enhance._call_llm",
                       return_value="   \n  \n  "):
                result = multi_query_decompose("how to release memory")
        self.assertEqual(result, ["how to release memory"])


class TestCallLlm(unittest.TestCase):

    def test_returns_none_when_no_key(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "",
                                      "LLM_API_KEY": ""}):
            from _builder.kb.rag_enhance import _call_llm
            result = _call_llm("test prompt")
        self.assertIsNone(result)

    def test_returns_response_when_api_succeeds(self):
        from _builder.kb.rag_enhance import _call_llm
        api_response = {
            "choices": [{"message": {"content": "LLM response text"}}]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_response).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key",
                                      "OPENAI_API_BASE": "http://mock"}):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                result = _call_llm("test prompt")
        self.assertEqual(result, "LLM response text")

    def test_returns_none_on_api_error(self):
        from _builder.kb.rag_enhance import _call_llm
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key",
                                      "OPENAI_API_BASE": "http://mock"}):
            with patch("urllib.request.urlopen",
                       side_effect=Exception("network error")):
                result = _call_llm("test prompt")
        self.assertIsNone(result)


class TestDetectLlmProvider(unittest.TestCase):

    def test_returns_none_when_no_env(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "",
                                      "LLM_API_KEY": "",
                                      "OPENAI_API_BASE": "",
                                      "LLM_API_BASE": ""}):
            from _builder.kb.rag_enhance import _detect_llm_provider
            self.assertEqual(_detect_llm_provider(), "none")

    def test_returns_openai_when_base_and_key_set(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "key",
                                      "OPENAI_API_BASE": "http://mock"}):
            from _builder.kb.rag_enhance import _detect_llm_provider
            self.assertEqual(_detect_llm_provider(), "openai")


if __name__ == "__main__":
    unittest.main()
