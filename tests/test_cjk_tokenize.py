"""CJK pre-tokenization: FTS5 MATCH works natively for Chinese text.

The unicode61 tokenizer folds contiguous CJK runs into single tokens,
making pure-CJK queries invisible to FTS5.  The pre-tokenize helpers
(space-join jieba segments on the write side, segment+quote on the
query side) make FTS5 MATCH work natively.  This test verifies both
sides and the graceful degradation when jieba is unavailable.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from _builder.utils import _fts5_escape, _cjk_pre_tokenize, _has_cjk
from _builder.kb.kb_index import _kb_connect, query_kb, upsert_kb_paragraph


class TestCjkPreTokenize(unittest.TestCase):

    def test_non_cjk_passthrough(self):
        self.assertEqual(_cjk_pre_tokenize("release memory"), "release memory")

    def test_cjk_segmented(self):
        result = _cjk_pre_tokenize("释放内存释放资源")
        tokens = result.split()
        self.assertIn("释放", tokens)
        self.assertIn("内存", tokens)

    def test_empty_string(self):
        self.assertEqual(_cjk_pre_tokenize(""), "")

    def test_stopwords_filtered(self):
        result = _cjk_pre_tokenize("的释放和内存")
        tokens = result.split()
        self.assertIn("释放", tokens)
        self.assertIn("内存", tokens)

    def test_char_fallback_when_jieba_unavailable(self):
        result = _cjk_pre_tokenize("释放内存", tokenizer="char")
        tokens = result.split()
        self.assertIn("释", tokens)
        self.assertIn("放", tokens)


class TestFts5EscapeCjk(unittest.TestCase):

    def test_latin_query_unchanged(self):
        result = _fts5_escape("how does bdev register")
        self.assertIn('"bdev"', result)
        self.assertIn('"register"', result)

    def test_cjk_query_segmented(self):
        result = _fts5_escape("释放内存")
        self.assertIn('"释放"', result)
        self.assertIn('"内存"', result)

    def test_pure_cjk_single_word(self):
        result = _fts5_escape("释放")
        self.assertIn('"释放"', result)

    def test_empty_query(self):
        self.assertEqual(_fts5_escape(""), '""')

    def test_mixed_cjk_latin(self):
        result = _fts5_escape("释放 memory")
        self.assertIn('"释放"', result)
        self.assertIn('"memory"', result)

    def test_cjk_stopwords_filtered_on_query_side(self):
        result = _fts5_escape("释放的内存")
        self.assertIn('"释放"', result)
        self.assertIn('"内存"', result)
        self.assertNotIn('"的"', result)

    def test_cjk_query_stopword_consistency_with_write_side(self):
        body = "释放的内存"
        tokenized = _cjk_pre_tokenize(body)
        q = _fts5_escape(body)
        tokenized_tokens = set(tokenized.split())
        q_tokens = set(t.strip('"') for t in q.split())
        self.assertEqual(tokenized_tokens, q_tokens,
                         f"write side={tokenized_tokens} != query side={q_tokens}")

    def test_no_duplicate_tokens(self):
        result = _fts5_escape("释放 memory 释放")
        tokens = [t.strip('"') for t in result.split()]
        self.assertEqual(len(tokens), len(set(tokens)),
                         f"duplicate tokens in: {result}")


class TestKbQueryCjkMatch(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.graph_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _insert(self, title, body, weight=1.0, kind="memory_qa"):
        return upsert_kb_paragraph(
            self.graph_dir, "memory", "test.json", title, body,
            weight=weight, kind=kind)

    def test_single_cjk_word_match(self):
        self._insert("释放内存", "释放内存释放资源线程安全", weight=2.0)
        results = query_kb(self.graph_dir, "释放", top_n=5)
        self.assertGreaterEqual(len(results), 1)
        self.assertIn("释放", results[0]["body"])

    def test_multi_cjk_word_match(self):
        self._insert("释放内存", "释放内存释放资源线程安全", weight=2.0)
        results = query_kb(self.graph_dir, "内存", top_n=5)
        self.assertGreaterEqual(len(results), 1)

    def test_cjk_phrase_match(self):
        self._insert("线程安全", "确保线程安全需要加锁", weight=2.0)
        results = query_kb(self.graph_dir, "线程安全", top_n=5)
        self.assertGreaterEqual(len(results), 1)

    def test_latin_still_works(self):
        self._insert("API Entry", "always drain the completion queue", weight=2.0)
        results = query_kb(self.graph_dir, "completion queue", top_n=5)
        self.assertGreaterEqual(len(results), 1)

    def test_mixed_cjk_latin_match(self):
        self._insert("混合查询", "释放 memory release 释放资源", weight=2.0)
        results = query_kb(self.graph_dir, "释放", top_n=5)
        self.assertGreaterEqual(len(results), 1)

    def test_cjk_stopword_query_matches_filtered_index(self):
        self._insert("停止词测试", "释放的内存和资源", weight=2.0)
        results = query_kb(self.graph_dir, "释放的内存", top_n=5)
        self.assertGreaterEqual(len(results), 1,
                                "query with stopwords must match write-side-filtered index")

    def test_multiple_cjk_entries(self):
        self._insert("释放内存", "释放内存释放资源", weight=2.0)
        self._insert("线程安全", "确保线程安全需要加锁", weight=1.5)
        self._insert("数据竞争", "数据竞争检测器race condition", weight=1.8)

        for q in ["释放", "内存", "线程安全", "数据竞争"]:
            results = query_kb(self.graph_dir, q, top_n=5)
            self.assertGreaterEqual(len(results), 1,
                                    f"query {q!r} returned {len(results)}")

    def test_weighted_ranking(self):
        self._insert("低权重", "释放内存释放资源", weight=0.5)
        self._insert("高权重", "释放内存释放资源线程安全", weight=3.0)
        results = query_kb(self.graph_dir, "释放", top_n=5)
        self.assertGreaterEqual(len(results), 2)
        top = results[0]
        self.assertGreaterEqual(top["weight"], 3.0)

    def test_body_tokenized_column_exists(self):
        conn = _kb_connect(self.graph_dir)
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(kb_paragraphs)")}
            self.assertIn("body_tokenized", cols)
        finally:
            conn.close()

    def test_fts5_indexes_body_tokenized(self):
        conn = _kb_connect(self.graph_dir)
        try:
            info = conn.execute(
                "PRAGMA table_info(kb_paragraphs_fts)").fetchall()
            fts_cols = {r[1] for r in info}
            self.assertIn("body_tokenized", fts_cols)
            self.assertNotIn("body", fts_cols)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
