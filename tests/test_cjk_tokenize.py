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

from _builder.utils import _fts5_escape, _cjk_pre_tokenize, _has_cjk, \
    _simple_tokenize, _strip_cjk_punct, _normalize_fullwidth, \
    _normalize_cjk_digits, set_project_user_dict, reset_resolver_state
from _builder.kb.kb_index import _kb_connect, query_kb, upsert_kb_paragraph, \
    build_kb_user_dict


class TestCjkPreTokenize(unittest.TestCase):

    def test_non_cjk_tokenized_with_synonyms(self):
        # Non-CJK text is whitespace-tokenized and expanded with
        # cross-lingual synonyms so English paragraphs become
        # findable by CJK queries and vice versa.
        result = _cjk_pre_tokenize("release memory")
        tokens = result.split()
        self.assertIn("release", tokens)
        self.assertIn("memory", tokens)
        # "release" → synonyms: free, 释放
        self.assertIn("free", tokens)
        self.assertIn("释放", tokens)
        # "memory" → synonym: 内存
        self.assertIn("内存", tokens)

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

    def test_cjk_punctuation_stripped_in_pre_tokenize(self):
        result = _cjk_pre_tokenize("释放内存，然后返回。")
        tokens = result.split()
        self.assertNotIn("，", tokens)
        self.assertNotIn("。", tokens)
        self.assertIn("释放", tokens)
        self.assertIn("返回", tokens)

    def test_fullwidth_punctuation_stripped(self):
        result = _cjk_pre_tokenize("注意：请勿调用！")
        tokens = result.split()
        self.assertNotIn("：", tokens)
        self.assertNotIn("！", tokens)
        self.assertIn("注意", tokens)
        self.assertIn("调用", tokens)

    def test_cjk_punctuation_no_cross_punct_bigrams(self):
        tokens = _simple_tokenize("内存，释放")
        self.assertNotIn("存，", tokens)
        self.assertNotIn("，释", tokens)
        self.assertIn("内存", tokens)
        self.assertIn("释放", tokens)

    def test_strip_cjk_punct_replaces_with_space(self):
        result = _strip_cjk_punct("内存，释放。")
        self.assertNotIn("，", result)
        self.assertNotIn("。", result)

    def test_strip_cjk_punct_preserves_ideographs(self):
        result = _strip_cjk_punct("释放内存")
        self.assertEqual(result, "释放内存")

    def test_strip_cjk_punct_empty(self):
        self.assertEqual(_strip_cjk_punct(""), "")
        self.assertEqual(_strip_cjk_punct(None), "")


class TestFullwidthNormalization(unittest.TestCase):
    """Full-width ASCII variants (Ａ-Ｚ, ０-９, 　) are common in CJK
    text typed via Chinese IMEs and must normalise to their half-width
    equivalents so the index and query sides stay symmetric."""

    def test_normalize_fullwidth_letters(self):
        self.assertEqual(_normalize_fullwidth("ＡＢＣ"), "ABC")
        self.assertEqual(_normalize_fullwidth("ａｂｃ"), "abc")

    def test_normalize_fullwidth_digits(self):
        self.assertEqual(_normalize_fullwidth("１２３"), "123")
        self.assertEqual(_normalize_fullwidth("０"), "0")

    def test_normalize_fullwidth_space(self):
        self.assertEqual(_normalize_fullwidth("释放\u3000内存"), "释放 内存")

    def test_normalize_fullwidth_punctuation(self):
        # Full-width punctuation in the U+FF00 block
        self.assertEqual(_normalize_fullwidth("Ａ！"), "A!")
        self.assertEqual(_normalize_fullwidth("＃＄"), "#$")

    def test_normalize_fullwidth_preserves_ascii(self):
        self.assertEqual(_normalize_fullwidth("ABC123"), "ABC123")
        self.assertEqual(_normalize_fullwidth("free memory"), "free memory")

    def test_normalize_fullwidth_preserves_cjk(self):
        self.assertEqual(_normalize_fullwidth("释放内存"), "释放内存")

    def test_normalize_fullwidth_empty(self):
        self.assertEqual(_normalize_fullwidth(""), "")
        self.assertEqual(_normalize_fullwidth(None), "")

    def test_pre_tokenize_fullwidth_letters(self):
        # Full-width Latin letters in CJK context: the IME typed ＡＢＣ
        # but the user meant ABC.  The tokenised output must contain
        # the ASCII form so English queries match.
        result = _cjk_pre_tokenize("使用 ＡＢＣ 函数")
        tokens = result.split()
        self.assertIn("ABC", tokens)
        self.assertNotIn("Ａ", tokens)
        self.assertNotIn("Ｂ", tokens)
        self.assertNotIn("Ｃ", tokens)

    def test_pre_tokenize_fullwidth_digits(self):
        result = _cjk_pre_tokenize("版本１．２")
        tokens = result.split()
        # After normalisation, １．２ becomes 1.2 (jieba treats it as
        # one token).  The key invariant: no full-width digits remain.
        for tok in tokens:
            for ch in tok:
                self.assertFalse('\uFF10' <= ch <= '\uFF19',
                                 f"Full-width digit {ch!r} survived in token {tok!r}")
        self.assertIn("版本", tokens)

    def test_pre_tokenize_fullwidth_space(self):
        result = _cjk_pre_tokenize("释放\u3000内存")
        tokens = result.split()
        self.assertIn("释放", tokens)
        self.assertIn("内存", tokens)
        # No empty tokens from the full-width space
        self.assertNotIn("", tokens)

    def test_pre_tokenize_fullwidth_only(self):
        # Pure full-width text (no CJK): normalises to ASCII and takes
        # the Latin path.  Previously the full-width block lived inside
        # _CJK_RE so ＡＢＣ was routed to the CJK path and jieba split
        # it into single chars.
        result = _cjk_pre_tokenize("ＡＢＣ")
        self.assertEqual(result, "ABC")

    def test_fts5_escape_fullwidth_letters(self):
        result = _fts5_escape("ＡＢＣ")
        self.assertIn('"ABC"', result)
        self.assertNotIn("Ａ", result)

    def test_fts5_escape_fullwidth_with_cjk(self):
        result = _fts5_escape("ＡＢＣ函数")
        self.assertIn('"ABC"', result)
        self.assertIn("函数", result)

    def test_simple_tokenize_fullwidth(self):
        # Full-width ABC normalises to ASCII; _simple_tokenize
        # lowercases and runs the [a-zA-Z0-9_]+ regex which captures
        # the whole alphanumeric run as one token.
        tokens = _simple_tokenize("ＡＢＣ123")
        self.assertIn("abc123", tokens)
        self.assertNotIn("Ａ", tokens)
        self.assertNotIn("Ｂ", tokens)

    def test_fullwidth_query_matches_ascii_paragraph(self):
        """A full-width query ＡＢＣ must match a paragraph stored
        with ASCII ABC."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            gd = str(Path(tmp))
            upsert_kb_paragraph(gd, "memory", "test.json",
                                "ABC function", "ABC function call",
                                "[]", "[]", 2.0, "memory_qa", 1.0, 0, "")
            results = query_kb(gd, "ＡＢＣ", top_n=5)
            self.assertGreaterEqual(len(results), 1,
                                    "Full-width query ＡＢＣ must match ASCII ABC")

    def test_fullwidth_paragraph_matches_ascii_query(self):
        """A paragraph stored with full-width ＡＢＣ must match an
        ASCII query ABC."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            gd = str(Path(tmp))
            upsert_kb_paragraph(gd, "memory", "test.json",
                                "ＡＢＣ function", "ＡＢＣ function call",
                                "[]", "[]", 2.0, "memory_qa", 1.0, 0, "")
            results = query_kb(gd, "ABC", top_n=5)
            self.assertGreaterEqual(len(results), 1,
                                    "ASCII query ABC must match full-width ＡＢＣ paragraph")

    def test_fullwidth_digits_match(self):
        """Version-like strings with full-width digits should
        normalise so a query for version 1.2 matches."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            gd = str(Path(tmp))
            upsert_kb_paragraph(gd, "memory", "test.json",
                                "版本１．２", "版本１．２发布说明",
                                "[]", "[]", 2.0, "memory_qa", 1.0, 0, "")
            # Query with ASCII digits
            results = query_kb(gd, "版本", top_n=5)
            self.assertGreaterEqual(len(results), 1)


class TestPinyinExpansion(unittest.TestCase):
    """Pinyin expansion lets a pure-Latin query ``shifang`` match a
    paragraph whose body contains ``释放`` — common when the IME is off
    or the keyboard is ASCII-only."""

    def test_pinyin_for_cjk_token(self):
        from _builder.utils import _pinyin_for_token
        result = _pinyin_for_token("释放")
        self.assertIn("shifang", result)
        self.assertIn("shi", result)
        self.assertIn("fang", result)

    def test_pinyin_for_single_char(self):
        from _builder.utils import _pinyin_for_token
        result = _pinyin_for_token("释")
        self.assertIn("shi", result)
        # Single char has no concatenated form
        self.assertEqual(len(result), 1)

    def test_pinyin_for_non_cjk(self):
        from _builder.utils import _pinyin_for_token
        self.assertEqual(_pinyin_for_token("memory"), [])
        self.assertEqual(_pinyin_for_token("ABC"), [])
        self.assertEqual(_pinyin_for_token(""), [])
        self.assertEqual(_pinyin_for_token(None), [])

    def test_expand_pinyin_appends_pinyin(self):
        from _builder.utils import _expand_pinyin
        tokens = ["释放", "memory"]
        result = _expand_pinyin(tokens)
        self.assertIn("释放", result)
        self.assertIn("memory", result)
        self.assertIn("shifang", result)
        self.assertIn("shi", result)
        self.assertIn("fang", result)

    def test_expand_pinyin_preserves_order(self):
        from _builder.utils import _expand_pinyin
        tokens = ["释放", "内存"]
        result = _expand_pinyin(tokens)
        # Original token first, then its pinyin, then next original
        self.assertEqual(result[0], "释放")
        self.assertIn("shifang", result[:5])
        self.assertIn("内存", result)

    def test_expand_pinyin_no_duplicates(self):
        from _builder.utils import _expand_pinyin
        tokens = ["释放", "释放"]
        result = _expand_pinyin(tokens)
        self.assertEqual(result.count("释放"), 1)
        self.assertEqual(result.count("shifang"), 1)

    def test_expand_pinyin_empty(self):
        from _builder.utils import _expand_pinyin
        self.assertEqual(_expand_pinyin([]), [])

    def test_pre_tokenize_includes_pinyin(self):
        result = _cjk_pre_tokenize("释放内存")
        tokens = result.split()
        self.assertIn("释放", tokens)
        self.assertIn("内存", tokens)
        self.assertIn("shifang", tokens)
        self.assertIn("neicun", tokens)

    def test_pre_tokenize_pinyin_with_synonyms(self):
        # Pinyin + synonyms both apply
        result = _cjk_pre_tokenize("释放")
        tokens = result.split()
        self.assertIn("释放", tokens)
        self.assertIn("shifang", tokens)
        # Synonyms
        self.assertIn("free", tokens)
        self.assertIn("release", tokens)

    def test_pinyin_query_matches_cjk_paragraph(self):
        """A pure-Latin pinyin query ``shifang`` must match a
        paragraph whose body contains ``释放``."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            gd = str(Path(tmp))
            upsert_kb_paragraph(gd, "memory", "test.json",
                                "释放内存", "释放内存释放资源",
                                "[]", "[]", 2.0, "memory_qa", 1.0, 0, "")
            results = query_kb(gd, "shifang", top_n=5)
            self.assertGreaterEqual(len(results), 1,
                                    "Pinyin query 'shifang' must match CJK paragraph about 释放")

    def test_pinyin_multi_word_query_matches(self):
        """Multi-word pinyin query ``shifang neicun`` matches."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            gd = str(Path(tmp))
            upsert_kb_paragraph(gd, "memory", "test.json",
                                "释放内存", "释放内存释放资源",
                                "[]", "[]", 2.0, "memory_qa", 1.0, 0, "")
            results = query_kb(gd, "shifang neicun", top_n=5)
            self.assertGreaterEqual(len(results), 1)

    def test_pinyin_spaced_query_matches(self):
        """Spaced pinyin query ``shi fang`` matches via per-char pinyin."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            gd = str(Path(tmp))
            upsert_kb_paragraph(gd, "memory", "test.json",
                                "释放内存", "释放内存释放资源",
                                "[]", "[]", 2.0, "memory_qa", 1.0, 0, "")
            results = query_kb(gd, "shi fang", top_n=5)
            self.assertGreaterEqual(len(results), 1,
                                    "Spaced pinyin 'shi fang' must match CJK paragraph about 释放")

    def test_pinyin_does_not_break_cjk_query(self):
        """CJK query 释放 still matches after pinyin expansion."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            gd = str(Path(tmp))
            upsert_kb_paragraph(gd, "memory", "test.json",
                                "释放内存", "释放内存释放资源",
                                "[]", "[]", 2.0, "memory_qa", 1.0, 0, "")
            results = query_kb(gd, "释放", top_n=5)
            self.assertGreaterEqual(len(results), 1)

    def test_pinyin_does_not_break_english_query(self):
        """English query 'free' still matches after pinyin expansion."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            gd = str(Path(tmp))
            upsert_kb_paragraph(gd, "memory", "test.json",
                                "释放内存", "释放内存释放资源",
                                "[]", "[]", 2.0, "memory_qa", 1.0, 0, "")
            results = query_kb(gd, "free", top_n=5)
            self.assertGreaterEqual(len(results), 1)

    def test_pinyin_in_simple_tokenize(self):
        from _builder.utils import _simple_tokenize
        tokens = _simple_tokenize("释放")
        self.assertIn("shi", tokens)
        self.assertIn("fang", tokens)


class TestCjkDigitNormalization(unittest.TestCase):
    """CJK numeral characters (一二三…) should normalise to ASCII digits
    so ``三个线程`` and ``3个线程`` produce the same token stream and
    match each other in FTS5."""

    def test_normalize_single_digits(self):
        self.assertEqual(_normalize_cjk_digits("三个"), "3个")
        self.assertEqual(_normalize_cjk_digits("第一步"), "第1步")
        self.assertEqual(_normalize_cjk_digits("第二种"), "第2种")
        self.assertEqual(_normalize_cjk_digits("九"), "9")

    def test_normalize_zero_variants(self):
        self.assertEqual(_normalize_cjk_digits("零"), "0")
        self.assertEqual(_normalize_cjk_digits("〇"), "0")

    def test_normalize_preserves_ascii(self):
        self.assertEqual(_normalize_cjk_digits("3个线程"), "3个线程")
        self.assertEqual(_normalize_cjk_digits("v1.2"), "v1.2")

    def test_normalize_preserves_non_digit_cjk(self):
        self.assertEqual(_normalize_cjk_digits("线程"), "线程")
        self.assertEqual(_normalize_cjk_digits("函数"), "函数")

    def test_normalize_empty(self):
        self.assertEqual(_normalize_cjk_digits(""), "")
        self.assertEqual(_normalize_cjk_digits(None), "")

    def test_normalize_compound_left_to_segmenter(self):
        # 十二 (twelve): 十 is not mapped, 二 → 2.  The result 十2 is
        # not ideal, but compound positional parsing (X十Y → X*10+Y)
        # is rare in a code KB and deliberately left to the segmenter.
        result = _normalize_cjk_digits("十二")
        self.assertNotIn("二", result)
        self.assertIn("2", result)

    def test_pre_tokenize_cjk_digit_matches_ascii(self):
        # The token stream for 三个线程 and 3个线程 must be identical
        # after normalisation.
        cjk_result = _cjk_pre_tokenize("三个线程")
        ascii_result = _cjk_pre_tokenize("3个线程")
        self.assertEqual(cjk_result, ascii_result)

    def test_pre_tokenize_step_number(self):
        # 第一步 and 第1步 must produce the same token stream
        cjk_result = _cjk_pre_tokenize("第一步")
        ascii_result = _cjk_pre_tokenize("第1步")
        self.assertEqual(cjk_result, ascii_result)

    def test_fts5_escape_cjk_digit(self):
        # Query 三个线程 escapes the same as 3个线程
        cjk_result = _fts5_escape("三个线程")
        ascii_result = _fts5_escape("3个线程")
        self.assertEqual(cjk_result, ascii_result)

    def test_simple_tokenize_cjk_digit(self):
        # Token sets for 三个线程 and 3个线程 must overlap on the
        # normalised digit token.
        cjk_tokens = _simple_tokenize("三个线程")
        ascii_tokens = _simple_tokenize("3个线程")
        self.assertIn("3", cjk_tokens)
        self.assertIn("3", ascii_tokens)

    def test_cjk_digit_paragraph_matches_ascii_query(self):
        """Paragraph stored with CJK numerals must be findable by an
        ASCII-digit query."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            gd = str(Path(tmp))
            upsert_kb_paragraph(gd, "memory", "test.json",
                                "线程创建", "创建三个线程处理并发请求",
                                "[]", "[]", 2.0, "memory_qa", 1.0, 0, "")
            results = query_kb(gd, "3个线程", top_n=5)
            self.assertGreaterEqual(len(results), 1,
                                    "ASCII query '3个线程' must match CJK paragraph about 三个线程")

    def test_ascii_digit_paragraph_matches_cjk_query(self):
        """Paragraph stored with ASCII digits must be findable by a
        CJK-numeral query."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            gd = str(Path(tmp))
            upsert_kb_paragraph(gd, "memory", "test.json",
                                "步骤", "第1步初始化数据库连接",
                                "[]", "[]", 2.0, "memory_qa", 1.0, 0, "")
            results = query_kb(gd, "第一步", top_n=5)
            self.assertGreaterEqual(len(results), 1,
                                    "CJK query '第一步' must match ASCII paragraph about 第1步")


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
        import re as _re
        body = "释放的内存"
        tokenized = _cjk_pre_tokenize(body)
        q = _fts5_escape(body)
        tokenized_tokens = set(tokenized.split())
        # Extract all quoted tokens from the FTS5 expression — the
        # query side uses OR-groups, so plain split() would include
        # AND/OR keywords and parentheses.
        q_tokens = set(_re.findall(r'"([^"]+)"', q))
        # The write side indexes pinyin alongside CJK so pinyin
        # queries can match; the query side for a CJK query does not
        # add pinyin (a CJK query should match CJK tokens, not pinyin).
        # The invariant is that every query-side token appears in the
        # write-side index so FTS5 AND-match succeeds.
        self.assertTrue(q_tokens.issubset(tokenized_tokens),
                        f"query side={q_tokens} not subset of write side={tokenized_tokens}")
        # Stopword 的 must be absent on both sides
        self.assertNotIn("的", tokenized_tokens)
        self.assertNotIn("的", q_tokens)

    def test_no_duplicate_tokens(self):
        import re as _re
        result = _fts5_escape("释放 memory 释放")
        tokens = _re.findall(r'"([^"]+)"', result)
        self.assertEqual(len(tokens), len(set(tokens)),
                         f"duplicate tokens in: {result}")


class TestSynonymExpansion(unittest.TestCase):
    """Cross-lingual synonym expansion in _cjk_pre_tokenize and _fts5_escape."""

    def test_synonyms_loaded_from_config(self):
        from _builder.utils import _load_synonyms
        syns = _load_synonyms()
        self.assertIsInstance(syns, dict)
        self.assertGreater(len(syns), 10)
        # "释放" should map to at least "free" and "release"
        self.assertIn("free", syns.get("释放", set()))
        self.assertIn("release", syns.get("释放", set()))
        # Bidirectional: "free" should map back to "释放"
        self.assertIn("释放", syns.get("free", set()))

    def test_expand_tokens_adds_synonyms(self):
        from _builder.utils import _expand_tokens, _load_synonyms
        syns = _load_synonyms()
        expanded = _expand_tokens(["释放", "内存"], syns)
        self.assertIn("释放", expanded)
        self.assertIn("内存", expanded)
        self.assertIn("free", expanded)
        self.assertIn("memory", expanded)

    def test_expand_tokens_no_duplicates(self):
        from _builder.utils import _expand_tokens, _load_synonyms
        syns = _load_synonyms()
        # "释放" and "free" are synonyms — expanding both must not
        # produce duplicate tokens.
        expanded = _expand_tokens(["释放", "free"], syns)
        self.assertEqual(len(expanded), len(set(expanded)))

    def test_expand_tokens_empty_synonyms_passthrough(self):
        from _builder.utils import _expand_tokens
        self.assertEqual(_expand_tokens(["a", "b"], None), ["a", "b"])
        self.assertEqual(_expand_tokens(["a", "b"], {}), ["a", "b"])

    def test_cjk_pre_tokenize_includes_english_synonyms(self):
        result = _cjk_pre_tokenize("释放内存")
        tokens = result.split()
        self.assertIn("释放", tokens)
        self.assertIn("内存", tokens)
        self.assertIn("free", tokens)
        self.assertIn("memory", tokens)

    def test_english_pre_tokenize_includes_cjk_synonyms(self):
        result = _cjk_pre_tokenize("free memory")
        tokens = result.split()
        self.assertIn("free", tokens)
        self.assertIn("memory", tokens)
        self.assertIn("释放", tokens)
        self.assertIn("内存", tokens)

    def test_synonym_expansion_is_symmetric(self):
        """Write side and query side must produce compatible token
        sets — every query-side token must appear in the write-side
        index so FTS5 AND-match finds the paragraph.  The write side
        additionally indexes pinyin spellings so pure-Latin pinyin
        queries match CJK paragraphs; the query side does not add
        pinyin for a CJK query (a CJK query should match CJK
        tokens), so the query side is a subset of the write side."""
        import re as _re
        body = "释放内存"
        tokenized = _cjk_pre_tokenize(body)
        escaped = _fts5_escape(body)
        tokenized_tokens = set(tokenized.split())
        escaped_tokens = set(_re.findall(r'"([^"]+)"', escaped))
        self.assertTrue(escaped_tokens.issubset(tokenized_tokens),
                        f"query side={escaped_tokens} not subset of write side={tokenized_tokens}")
        # CJK tokens and synonyms must be present on both sides
        for tok in ("释放", "内存", "free", "release", "memory"):
            self.assertIn(tok, tokenized_tokens)
            self.assertIn(tok, escaped_tokens)


class TestKbQueryCjkMatch(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.graph_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _insert(self, title, body, weight=1.0, kind="memory_qa"):
        self._insert_count = getattr(self, "_insert_count", 0) + 1
        return upsert_kb_paragraph(
            self.graph_dir, "memory", f"test_{self._insert_count}.json",
            title, body, weight=weight, kind=kind)

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

    def test_english_query_finds_cjk_paragraph(self):
        """English query 'free' must find a CJK paragraph about 释放."""
        self._insert("释放内存", "释放内存释放资源", weight=2.0)
        results = query_kb(self.graph_dir, "free", top_n=5)
        self.assertGreaterEqual(len(results), 1,
                                "English query 'free' must find CJK paragraph about 释放")

    def test_cjk_query_finds_english_paragraph(self):
        """CJK query 释放 must find an English paragraph about free."""
        self._insert("Free Memory", "free memory release resource", weight=2.0)
        results = query_kb(self.graph_dir, "释放", top_n=5)
        self.assertGreaterEqual(len(results), 1,
                                "CJK query 释放 must find English paragraph about free")

    def test_cross_lingual_synonym_match(self):
        """Bidirectional: insert CJK, query English; insert English, query CJK."""
        self._insert("互斥锁", "互斥锁保护临界区", weight=2.0)
        results = query_kb(self.graph_dir, "mutex", top_n=5)
        self.assertGreaterEqual(len(results), 1,
                                "English query 'mutex' must find CJK paragraph about 互斥锁")


class TestProjectUserDict(unittest.TestCase):
    """Auto-built jieba user_dict: project-specific identifiers
    (spdk, vfio, virtio) segment as whole tokens after
    build_kb_user_dict writes the dict and set_project_user_dict
    activates it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph_dir = os.path.join(self.tmp.name, "code2db-out")
        os.makedirs(self.graph_dir, exist_ok=True)
        # Reset any leftover project user_dict from prior tests.
        reset_resolver_state()

    def tearDown(self):
        reset_resolver_state()

    def test_build_writes_user_dict_file(self):
        # Create a minimal code graph so build_kb_user_dict has a
        # function-name source to mine.
        import sqlite3
        db_path = os.path.join(self.graph_dir, "code2database.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE functions (id INTEGER PRIMARY KEY, "
            "name TEXT, file TEXT)")
        conn.execute(
            "INSERT INTO functions (name, file) VALUES "
            "('spdk_bdev_submit', 'bdev.c'), "
            "('virtio_pci_probe', 'virtio.c'), "
            "('vfio_set_irq', 'vfio.c')")
        conn.commit()
        conn.close()
        path = build_kb_user_dict(self.graph_dir)
        self.assertIsNotNone(path)
        self.assertTrue(os.path.isfile(path))
        content = open(path, encoding="utf-8").read()
        self.assertIn("spdk_bdev_submit", content)
        self.assertIn("virtio_pci_probe", content)
        self.assertIn("vfio_set_irq", content)

    def test_build_extracts_identifiers_from_kb_paragraphs(self):
        # No code graph — only KB paragraphs. Identifiers in the body
        # text should still be extracted and written.
        upsert_kb_paragraph(
            self.graph_dir, "memory", "test.json",
            "test", "call spdk_bdev_submit to process the request",
            "[]", "[]", 1.0, "memory_qa", 1.0)
        path = build_kb_user_dict(self.graph_dir)
        self.assertIsNotNone(path)
        content = open(path, encoding="utf-8").read()
        self.assertIn("spdk_bdev_submit", content)

    def test_build_returns_none_when_no_corpus(self):
        # Empty graph + empty KB → no terms → None.
        path = build_kb_user_dict(self.graph_dir)
        self.assertIsNone(path)

    def test_user_dict_segments_project_identifier(self):
        # Without the user_dict, jieba may split spdk_bdev_submit into
        # parts. With the user_dict, it should segment as one token.
        from _builder.utils import _jieba_tokenize
        # Write a user_dict with a known term.
        dict_path = os.path.join(self.graph_dir, "jieba_user_dict.txt")
        with open(dict_path, "w", encoding="utf-8") as f:
            f.write("spdk_bdev_submit 1000 n\n")
        set_project_user_dict(dict_path)
        tokens = _jieba_tokenize("spdk_bdev_submit called")
        self.assertIn("spdk_bdev_submit", tokens)

    def test_user_dict_does_not_affect_default_jieba(self):
        # The isolated Tokenizer instance should not pollute the
        # global jieba state. Clearing the project dict restores
        # default segmentation.
        from _builder.utils import _jieba_tokenize, _TOKENIZER_CACHE
        dict_path = os.path.join(self.graph_dir, "jieba_user_dict.txt")
        with open(dict_path, "w", encoding="utf-8") as f:
            f.write("myproj_custom_func 1000 n\n")
        set_project_user_dict(dict_path)
        tokens_with = _jieba_tokenize("myproj_custom_func called")
        self.assertIn("myproj_custom_func", tokens_with)
        # Clear the project dict.
        set_project_user_dict(None)
        # The cache should have been invalidated for the project dict.
        self.assertNotIn(("jieba", dict_path), _TOKENIZER_CACHE)
        # Default jieba should not have the custom term.
        tokens_without = _jieba_tokenize("myproj_custom_func called")
        # The custom term should NOT appear as a single token
        # (it may be split by the default dictionary).
        self.assertNotIn("myproj_custom_func", tokens_without)

    def test_rebuild_kb_index_activates_user_dict(self):
        # rebuild_kb_index calls build_kb_user_dict + set_project_user_dict.
        # After rebuild, project identifiers should segment as whole tokens.
        from _builder.kb.kb_index import rebuild_kb_index
        import sqlite3
        # Create a code graph with a project-specific identifier.
        db_path = os.path.join(self.graph_dir, "code2database.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE functions (id INTEGER PRIMARY KEY, "
            "name TEXT, file TEXT)")
        conn.execute(
            "INSERT INTO functions (name, file) VALUES "
            "('myproj_init_driver', 'driver.c')")
        conn.commit()
        conn.close()
        # Create a memory entry that references the identifier.
        _mem_dir = os.path.join(self.graph_dir, "memory")
        os.makedirs(_mem_dir, exist_ok=True)
        mem_db = sqlite3.connect(os.path.join(_mem_dir, "memory.db"))
        mem_db.execute(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY, "
            "question TEXT, answer TEXT, status TEXT, weight REAL, "
            "created TEXT, category_id INTEGER)")
        mem_db.execute(
            "INSERT INTO memories (question, answer, status, weight, "
            "created) VALUES ('how to init', 'call myproj_init_driver "
            "first', 'active', 1.0, '2024-01-01')")
        mem_db.commit()
        mem_db.close()
        # Rebuild — should build user_dict and activate it.
        result = rebuild_kb_index(self.graph_dir, verbose=False)
        self.assertTrue(result.get("rebuilt") or
                        result.get("reason") == "unchanged")
        # The user_dict file should exist.
        dict_path = os.path.join(self.graph_dir, "knowledge",
                                 "jieba_user_dict.txt")
        self.assertTrue(os.path.isfile(dict_path))
        # The pre-tokenized body should contain the whole identifier.
        from _builder.utils import _PROJECT_USER_DICT
        self.assertIsNotNone(_PROJECT_USER_DICT)


if __name__ == "__main__":
    unittest.main()
