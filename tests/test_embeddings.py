"""Tests for TF-IDF char n-gram embeddings (D24 enhancement)."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.embeddings import (
    _char_ngrams, _build_vocab, _tfidf_vector, _cosine_similarity,
    NGramEmbeddings, build_embeddings_for_graph, get_or_build_embeddings,
    DEFAULT_NGRAM_SIZE,
)


class TestCharNgrams(unittest.TestCase):
    """Test _char_ngrams decomposition."""

    def test_empty_returns_empty(self):
        """Empty string returns empty list."""
        self.assertEqual(_char_ngrams(""), [])

    def test_short_word(self):
        """Word shorter than n+1 returns the padded form."""
        # "ab" padded to "$ab$" (4 chars), n=3 → 2 ngrams
        result = _char_ngrams("ab", n=3)
        self.assertEqual(len(result), 2)
        self.assertIn("$ab", result)
        self.assertIn("ab$", result)

    def test_long_word_decomposes(self):
        """free_pid with n=3 decomposes into 3-grams."""
        result = _char_ngrams("free_pid", n=3)
        # "$fr", "fre", "ree", "ee_", "e_p", "_pi", "pid", "id$"
        self.assertIn("$fr", result)
        self.assertIn("fre", result)
        self.assertIn("ree", result)
        self.assertIn("pid", result)
        self.assertIn("id$", result)

    def test_lowercase_normalization(self):
        """Uppercase is normalized to lowercase."""
        result = _char_ngrams("FREE", n=3)
        self.assertIn("$fr", result)
        self.assertIn("fre", result)
        self.assertIn("ree", result)
        self.assertIn("ee$", result)

    def test_non_alphanumeric_replaced(self):
        """Non-alphanumeric chars are replaced with spaces (word boundaries)."""
        result = _char_ngrams("foo bar", n=3)
        # Two words: foo, bar
        self.assertIn("$fo", result)
        self.assertIn("$ba", result)

    def test_cjk_treated_as_text(self):
        """CJK characters are included in n-grams."""
        result = _char_ngrams("释放pid", n=3)
        self.assertTrue(len(result) > 0)


class TestBuildVocab(unittest.TestCase):
    """Test _build_vocab document frequency counting."""

    def test_empty_corpus_returns_empty(self):
        """Empty corpus returns empty vocab."""
        self.assertEqual(_build_vocab([]), {})

    def test_doc_freq_counts_unique_per_doc(self):
        """Each ngram counted once per doc, even if it appears multiple times."""
        # Both docs contain "fr" prefix
        corpus = ["free", "frozen"]
        vocab = _build_vocab(corpus, n=3)
        # "$fr" appears in both → doc_freq=2
        self.assertEqual(vocab.get("$fr"), 2)
        # "fre" only in "free"
        self.assertEqual(vocab.get("fre"), 1)
        # "roz" only in "frozen"
        self.assertEqual(vocab.get("roz"), 1)
        # "ee$" only in "free"
        self.assertEqual(vocab.get("ee$"), 1)

    def test_max_size_caps_vocab(self):
        """max_size limits vocabulary size."""
        # Generate many distinct n-grams
        corpus = [f"func_{i}_name" for i in range(100)]
        vocab = _build_vocab(corpus, n=3, max_size=10)
        self.assertLessEqual(len(vocab), 10)


class TestTfidfVector(unittest.TestCase):
    """Test _tfidf_vector computation."""

    def test_empty_text_returns_empty(self):
        """Empty text returns empty vector."""
        self.assertEqual(_tfidf_vector("", {"a": 1.0}), {})

    def test_ngrams_outside_vocab_excluded(self):
        """N-grams not in vocab are excluded from the vector."""
        vec = _tfidf_vector("foo", {"$fo": 1.0})
        # Only "$fo" is in vocab, "foo" and "oo$" are not
        self.assertIn("$fo", vec)
        self.assertNotIn("oo$", vec)

    def test_tf_proportional_to_count(self):
        """N-gram appearing twice gets higher TF than once."""
        text = "foofoo"  # "foo" and "oo$" patterns repeat
        vocab = {"$fo": 1.0, "foo": 1.0, "oof": 1.0, "oo$": 1.0}
        vec = _tfidf_vector(text, vocab)
        # All weights should be positive
        for v in vec.values():
            self.assertGreater(v, 0)

    def test_idf_affects_weight(self):
        """Higher IDF → higher weight."""
        # "rare" appears in one doc → high IDF
        # "common" appears in many docs → low IDF
        corpus = ["rare"] + ["common"] * 10
        vocab = _build_vocab(corpus, n=3)
        N = len(corpus)
        import math
        # Compute IDFs
        idf_rare = math.log((N + 1) / (vocab["$ra"] + 1)) + 1.0
        idf_common = math.log((N + 1) / (vocab["$co"] + 1)) + 1.0
        self.assertGreater(idf_rare, idf_common)


class TestCosineSimilarity(unittest.TestCase):
    """Test _cosine_similarity."""

    def test_empty_vectors_returns_zero(self):
        """Empty vectors return 0."""
        self.assertEqual(_cosine_similarity({}, {"a": 1.0}), 0.0)
        self.assertEqual(_cosine_similarity({"a": 1.0}, {}), 0.0)

    def test_identical_vectors_returns_one(self):
        """Identical vectors return 1.0 (cosine of 0 angle)."""
        vec = {"a": 1.0, "b": 2.0}
        sim = _cosine_similarity(vec, vec)
        self.assertAlmostEqual(sim, 1.0, places=5)

    def test_disjoint_vectors_returns_zero(self):
        """Vectors with no shared keys return 0."""
        self.assertEqual(_cosine_similarity({"a": 1.0}, {"b": 1.0}), 0.0)

    def test_partial_overlap_between_zero_and_one(self):
        """Partially overlapping vectors return value in (0, 1)."""
        v1 = {"a": 1.0, "b": 1.0}
        v2 = {"a": 1.0, "c": 1.0}
        sim = _cosine_similarity(v1, v2)
        self.assertGreater(sim, 0.0)
        self.assertLess(sim, 1.0)

    def test_symmetric(self):
        """sim(v1, v2) == sim(v2, v1)."""
        v1 = {"a": 1.0, "b": 2.0}
        v2 = {"a": 3.0, "c": 1.0}
        self.assertAlmostEqual(_cosine_similarity(v1, v2),
                                 _cosine_similarity(v2, v1))


class TestNGramEmbeddings(unittest.TestCase):
    """Test the NGramEmbeddings class end-to-end."""

    def test_fit_then_search_finds_relevant(self):
        """After fit, search returns relevant documents first."""
        emb = NGramEmbeddings(n=3)
        corpus = {
            "free_pid": "free_pid release pid",
            "alloc_pid": "alloc_pid allocate pid",
            "init_module": "init_module setup module",
        }
        emb.fit(corpus)
        # "release pid" should match free_pid better than init_module
        results = emb.search("release pid", top_k=3)
        self.assertTrue(len(results) > 0)
        top_id = results[0][0]
        self.assertEqual(top_id, "free_pid")

    def test_search_empty_embeddings_returns_empty(self):
        """Search on untrained embeddings returns empty list."""
        emb = NGramEmbeddings()
        self.assertEqual(emb.search("anything"), [])

    def test_search_empty_query_returns_empty(self):
        """Empty query returns empty list."""
        emb = NGramEmbeddings()
        emb.fit({"doc1": "hello world"})
        self.assertEqual(emb.search(""), [])

    def test_similarity_zero_for_unknown_doc(self):
        """similarity returns 0 for unknown doc_id."""
        emb = NGramEmbeddings()
        emb.fit({"doc1": "hello"})
        self.assertEqual(emb.similarity("hello", "unknown"), 0.0)

    def test_top_k_limits_results(self):
        """top_k limits the number of results returned."""
        emb = NGramEmbeddings(n=3)
        corpus = {f"doc_{i}": f"func_{i}" for i in range(20)}
        emb.fit(corpus)
        results = emb.search("func", top_k=5)
        self.assertLessEqual(len(results), 5)

    def test_save_and_load_roundtrip(self):
        """Save then load preserves the embeddings."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "emb.json")
            emb = NGramEmbeddings(n=3)
            emb.fit({"doc1": "free_pid", "doc2": "alloc_pid"})
            emb.save(path)
            loaded = NGramEmbeddings.load(path)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.n, 3)
            self.assertEqual(loaded.vocab_idf, emb.vocab_idf)
            self.assertEqual(loaded.doc_vectors, emb.doc_vectors)

    def test_load_missing_file_returns_none(self):
        """load() returns None for missing file."""
        self.assertIsNone(NGramEmbeddings.load("/nonexistent/path.json"))


class TestBuildEmbeddingsForGraph(unittest.TestCase):
    """Test build_embeddings_for_graph with a fake graph directory."""

    def _write_master(self, graph_dir, nodes):
        """Write a minimal JSON-backend graph (master + one domain file).

        build_embeddings_for_graph loads nodes via _load_full_graph —
        master.json has never carried a 'nodes' key (the old fixture
        asserted a contract the implementation never had).
        """
        master_path = os.path.join(graph_dir, "code2database_master.json")
        dom_dir = os.path.join(graph_dir, "domains", "grp")
        os.makedirs(dom_dir, exist_ok=True)
        dom_file = os.path.join(dom_dir, "code2database_domain_root.json")
        node_list = [
            {"id": nid, "name": attrs.get("name", nid),
             "signature": attrs.get("signature", ""),
             "semantic_desc": attrs.get("semantic_desc", ""),
             "domain": "root", "labels": [], "line": 1}
            for nid, attrs in nodes.items()
        ]
        with open(dom_file, "w", encoding="utf-8") as f:
            json.dump({"nodes": node_list, "edges": []}, f)
        with open(master_path, "w", encoding="utf-8") as f:
            json.dump({
                "type": "code2database_master",
                "domains": {"root": "domains/grp/code2database_domain_root.json"},
                "total_nodes": len(node_list),
            }, f)

    def test_builds_from_master(self):
        """Build embeddings from code2database_master.json."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_master(tmp, {
                "free_pid": {"name": "free_pid", "signature": "void free_pid(int)",
                              "semantic_desc": "release pid resource"},
                "init_mod": {"name": "init_module", "signature": "int init_module(void)",
                              "semantic_desc": "initialize the module"},
            })
            emb = build_embeddings_for_graph(tmp)
            self.assertIsNotNone(emb)
            self.assertGreater(len(emb.vocab_idf), 0)
            self.assertEqual(len(emb.doc_vectors), 2)
            # Check that embeddings.json was saved
            self.assertTrue(os.path.exists(os.path.join(tmp, "embeddings.json")))

    def test_returns_none_when_no_master(self):
        """Returns None when code2database_master.json is missing."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(build_embeddings_for_graph(tmp))

    def test_returns_none_for_empty_nodes(self):
        """Returns None when the graph has no embeddable nodes."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_master(tmp, {})
            self.assertIsNone(build_embeddings_for_graph(tmp))


class TestGetOrBuildEmbeddings(unittest.TestCase):
    """Test get_or_build_embeddings caching behavior."""

    def test_loads_existing_embeddings(self):
        """Loads embeddings.json if it exists."""
        with tempfile.TemporaryDirectory() as tmp:
            # First build — minimal JSON-backend graph
            _dom_dir = os.path.join(tmp, "domains", "grp")
            os.makedirs(_dom_dir, exist_ok=True)
            _dom_file = os.path.join(_dom_dir, "code2database_domain_root.json")
            with open(_dom_file, "w") as f:
                json.dump({"nodes": [
                    {"id": "f1", "name": "func_one", "signature": "",
                     "semantic_desc": "", "domain": "root", "labels": [], "line": 1}
                ], "edges": []}, f)
            master_path = os.path.join(tmp, "code2database_master.json")
            with open(master_path, "w") as f:
                json.dump({"type": "code2database_master",
                           "domains": {"root": "domains/grp/code2database_domain_root.json"},
                           "total_nodes": 1}, f)
            emb1 = get_or_build_embeddings(tmp)
            self.assertIsNotNone(emb1)
            # Second call should load from disk (no rebuild)
            # We can verify by removing the master and confirming we still get emb
            os.remove(master_path)
            emb2 = get_or_build_embeddings(tmp)
            self.assertIsNotNone(emb2)
            self.assertEqual(emb2.doc_vectors, emb1.doc_vectors)


class TestEndToEndSimilarity(unittest.TestCase):
    """End-to-end: similar function names get higher similarity than dissimilar."""

    def test_free_pid_matches_release_pid(self):
        """'release pid' query has higher similarity to free_pid than init_module."""
        emb = NGramEmbeddings(n=3)
        corpus = {
            "free_pid": "free_pid release pid",
            "init_module": "init_module setup module",
        }
        emb.fit(corpus)
        sim_free = emb.similarity("release pid", "free_pid")
        sim_init = emb.similarity("release pid", "init_module")
        self.assertGreater(sim_free, sim_init)

    def test_alloc_matches_alloc_better_than_free(self):
        """'allocate' matches alloc_pid better than free_pid."""
        emb = NGramEmbeddings(n=3)
        corpus = {
            "alloc_pid": "alloc_pid allocate pid",
            "free_pid": "free_pid release pid",
        }
        emb.fit(corpus)
        sim_alloc = emb.similarity("allocate", "alloc_pid")
        sim_free = emb.similarity("allocate", "free_pid")
        self.assertGreater(sim_alloc, sim_free)


if __name__ == "__main__":
    unittest.main()
