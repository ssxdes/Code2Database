"""Unit tests for ast_pattern.py — structural code search with metavariables.

ast_pattern.py implements semgrep/ast-grep-style pattern matching:
- $NAME matches any single token (e.g., $X matches an identifier)
- ... matches any number of tokens (skip / wildcard)
- $$NAME matches any subtree depth (deep metavar)
- Same metavar used twice requires same binding (consistency check)

This test suite covers:
- _tokenize_pattern / _tokenize_source: tokenization correctness
- _is_metavar / _is_deep_metavar / _is_ellipsis: classifier predicates
- _match_tokens: full matching with binding, ellipsis, repeat-metavar
- search_pattern: end-to-end on a tiny graph fixture
"""
import json
import os
import tempfile
import unittest


class TestPredicates(unittest.TestCase):
    """Tests for _is_metavar / _is_deep_metavar / _is_ellipsis."""

    def test_is_metavar_basic(self):
        from _builder.ast_pattern import _is_metavar
        self.assertTrue(_is_metavar("$X"))
        self.assertTrue(_is_metavar("$NAME"))
        self.assertTrue(_is_metavar("$var_1"))
        self.assertTrue(_is_metavar("  $X  "))  # whitespace stripped

    def test_is_metavar_rejects_non_metavar(self):
        from _builder.ast_pattern import _is_metavar
        self.assertFalse(_is_metavar("foo"))
        self.assertFalse(_is_metavar("$"))  # bare $ without name
        self.assertFalse(_is_metavar("$1"))  # must start with letter/_
        self.assertFalse(_is_metavar("..."))
        self.assertFalse(_is_metavar(""))

    def test_is_deep_metavar(self):
        from _builder.ast_pattern import _is_deep_metavar
        self.assertTrue(_is_deep_metavar("$$X"))
        self.assertTrue(_is_deep_metavar("$$BODY"))
        self.assertTrue(_is_deep_metavar("  $$X  "))

    def test_is_deep_metavar_rejects_regular_metavar(self):
        from _builder.ast_pattern import _is_deep_metavar
        self.assertFalse(_is_deep_metavar("$X"))  # regular, not deep
        self.assertFalse(_is_deep_metavar("foo"))
        self.assertFalse(_is_deep_metavar("$$"))  # bare $$ without name

    def test_is_ellipsis(self):
        from _builder.ast_pattern import _is_ellipsis
        self.assertTrue(_is_ellipsis("..."))
        self.assertTrue(_is_ellipsis("  ...  "))
        self.assertFalse(_is_ellipsis(".."))
        self.assertFalse(_is_ellipsis("...."))
        self.assertFalse(_is_ellipsis("foo"))


class TestTokenizePattern(unittest.TestCase):
    """Tests for _tokenize_pattern."""

    def test_simple_pattern(self):
        from _builder.ast_pattern import _tokenize_pattern
        tokens = _tokenize_pattern("foo ( bar )")
        self.assertEqual(tokens, ["foo", "(", "bar", ")"])

    def test_metavar_tokenized(self):
        from _builder.ast_pattern import _tokenize_pattern
        tokens = _tokenize_pattern("$X + $Y")
        self.assertEqual(tokens, ["$X", "+", "$Y"])

    def test_deep_metavar_tokenized(self):
        from _builder.ast_pattern import _tokenize_pattern
        tokens = _tokenize_pattern("$$BODY")
        self.assertEqual(tokens, ["$$BODY"])

    def test_ellipsis_tokenized(self):
        from _builder.ast_pattern import _tokenize_pattern
        tokens = _tokenize_pattern("lock ( $L ) ; ... unlock ( $L ) ;")
        self.assertEqual(tokens, ["lock", "(", "$L", ")", ";", "...", "unlock", "(", "$L", ")", ";"])

    def test_operators_preserved(self):
        from _builder.ast_pattern import _tokenize_pattern
        tokens = _tokenize_pattern("$X == $X")
        self.assertEqual(tokens, ["$X", "==", "$X"])

    def test_whitespace_collapsed(self):
        from _builder.ast_pattern import _tokenize_pattern
        # Multiple whitespace runs collapse
        tokens = _tokenize_pattern("  foo    bar  ")
        self.assertEqual(tokens, ["foo", "bar"])


class TestTokenizeSource(unittest.TestCase):
    """Tests for _tokenize_source."""

    def test_identifiers_and_punctuation(self):
        from _builder.ast_pattern import _tokenize_source
        tokens = _tokenize_source("foo(bar, baz);")
        self.assertEqual(tokens, ["foo", "(", "bar", ",", "baz", ")", ";"])

    def test_string_literals(self):
        from _builder.ast_pattern import _tokenize_source
        tokens = _tokenize_source('printf("hello world")')
        self.assertIn('"hello world"', tokens)
        self.assertIn("printf", tokens)

    def test_numbers(self):
        from _builder.ast_pattern import _tokenize_source
        tokens = _tokenize_source("x = 42; y = 3.14;")
        self.assertIn("42", tokens)
        self.assertIn("3.14", tokens)

    def test_multichar_operators(self):
        from _builder.ast_pattern import _tokenize_source
        # == should be one token, not two '='
        tokens = _tokenize_source("a == b")
        self.assertEqual(tokens, ["a", "==", "b"])
        # -> should be one token
        tokens = _tokenize_source("p->next")
        self.assertEqual(tokens, ["p", "->", "next"])

    def test_empty_input(self):
        from _builder.ast_pattern import _tokenize_source
        self.assertEqual(_tokenize_source(""), [])
        self.assertEqual(_tokenize_source("   "), [])


class TestMatchTokens(unittest.TestCase):
    """Tests for _match_tokens — the core matching engine."""

    def test_exact_literal_match(self):
        from _builder.ast_pattern import _tokenize_pattern, _tokenize_source, _match_tokens
        pattern = _tokenize_pattern("foo ( )")
        source = _tokenize_source("foo ( )")
        result = _match_tokens(pattern, source)
        self.assertIsNotNone(result)
        self.assertEqual(result, {})  # no bindings

    def test_metavar_binds(self):
        from _builder.ast_pattern import _tokenize_pattern, _tokenize_source, _match_tokens
        pattern = _tokenize_pattern("$X ( )")
        source = _tokenize_source("foo ( )")
        result = _match_tokens(pattern, source)
        self.assertIsNotNone(result)
        self.assertEqual(result, {"X": "foo"})

    def test_repeated_metavar_must_match_same_value(self):
        """$X == $X requires both $X to bind the same token."""
        from _builder.ast_pattern import _tokenize_pattern, _tokenize_source, _match_tokens
        # Match: a == a (same)
        pattern = _tokenize_pattern("$X == $X")
        source = _tokenize_source("a == a")
        result = _match_tokens(pattern, source)
        self.assertIsNotNone(result)
        self.assertEqual(result, {"X": "a"})

        # No match: a == b (different)
        source = _tokenize_source("a == b")
        result = _match_tokens(pattern, source)
        self.assertIsNone(result)

    def test_ellipsis_matches_any_tokens(self):
        from _builder.ast_pattern import _tokenize_pattern, _tokenize_source, _match_tokens
        pattern = _tokenize_pattern("foo ( ... )")
        source = _tokenize_source("foo ( a , b , c )")
        result = _match_tokens(pattern, source)
        self.assertIsNotNone(result)

    def test_ellipsis_at_end_matches_everything(self):
        from _builder.ast_pattern import _tokenize_pattern, _tokenize_source, _match_tokens
        pattern = _tokenize_pattern("foo ...")
        source = _tokenize_source("foo bar baz qux")
        result = _match_tokens(pattern, source)
        self.assertIsNotNone(result)

    def test_ellipsis_with_trailing_metavar(self):
        """lock($L); ... unlock($L) — find lock/unlock pairs."""
        from _builder.ast_pattern import _tokenize_pattern, _tokenize_source, _match_tokens
        pattern = _tokenize_pattern("lock ( $L ) ; ... unlock ( $L ) ;")
        source = _tokenize_source("lock ( mutex ) ; do_something ( ) ; unlock ( mutex ) ;")
        result = _match_tokens(pattern, source)
        self.assertIsNotNone(result)
        self.assertEqual(result, {"L": "mutex"})

    def test_ellipsis_with_different_lock_target_fails(self):
        """lock(A); ... unlock(B) — should not match because L differs."""
        from _builder.ast_pattern import _tokenize_pattern, _tokenize_source, _match_tokens
        pattern = _tokenize_pattern("lock ( $L ) ; ... unlock ( $L ) ;")
        source = _tokenize_source("lock ( a ) ; unlock ( b ) ;")
        result = _match_tokens(pattern, source)
        self.assertIsNone(result)

    def test_no_match_when_pattern_longer_than_source(self):
        from _builder.ast_pattern import _tokenize_pattern, _tokenize_source, _match_tokens
        pattern = _tokenize_pattern("foo bar baz")
        source = _tokenize_source("foo bar")
        result = _match_tokens(pattern, source)
        self.assertIsNone(result)

    def test_trailing_ellipsis_in_pattern_when_source_exhausted(self):
        """Pattern: 'foo ...' should match source 'foo' (ellipsis matches 0 tokens)."""
        from _builder.ast_pattern import _tokenize_pattern, _tokenize_source, _match_tokens
        pattern = _tokenize_pattern("foo ...")
        source = _tokenize_source("foo")
        result = _match_tokens(pattern, source)
        self.assertIsNotNone(result)

    def test_pattern_with_only_ellipsis_matches_anything(self):
        from _builder.ast_pattern import _tokenize_pattern, _tokenize_source, _match_tokens
        pattern = _tokenize_pattern("...")
        source = _tokenize_source("foo bar baz")
        result = _match_tokens(pattern, source)
        self.assertIsNotNone(result)

    def test_empty_pattern_matches_empty_source(self):
        from _builder.ast_pattern import _match_tokens
        result = _match_tokens([], [])
        self.assertEqual(result, {})

    def test_keyword_case_insensitive(self):
        """Keywords like 'if' / 'IF' should match case-insensitively."""
        from _builder.ast_pattern import _tokenize_pattern, _tokenize_source, _match_tokens
        pattern = _tokenize_pattern("if ( $X )")
        source = _tokenize_source("IF ( cond )")
        result = _match_tokens(pattern, source)
        self.assertIsNotNone(result)
        self.assertEqual(result, {"X": "cond"})


def _make_pattern_graph_dir() -> str:
    """Build a tiny domain-split graph fixture for search_pattern tests."""
    tmp = tempfile.mkdtemp(prefix="c2d_ast_test_")
    domain_data = {
        "nodes": [
            {"id": "fn1", "name": "fn1", "source_file": "/tmp/x.c", "line": 1,
             "signature": "int fn1()", "body_text": "lock(mutex); do_work(); unlock(mutex);",
             "labels": [], "is_empty": False, "domain": "test"},
            {"id": "fn2", "name": "fn2", "source_file": "/tmp/y.c", "line": 2,
             "signature": "int fn2()", "body_text": "if (a == a) return 1;",
             "labels": [], "is_empty": False, "domain": "test"},
            {"id": "fn3", "name": "fn3", "source_file": "/tmp/z.c", "line": 3,
             "signature": "int fn3()", "body_text": "free(p); return *p;",
             "labels": [], "is_empty": False, "domain": "test"},
        ],
        "edges": [],
    }
    domain_filename = "domain_test.json"
    with open(os.path.join(tmp, domain_filename), "w") as f:
        json.dump(domain_data, f)
    master = {"source_root": "/tmp", "domains": {"test": domain_filename}}
    with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
        json.dump(master, f)
    return tmp


class TestSearchPatternEndToEnd(unittest.TestCase):
    """End-to-end tests for search_pattern on a tiny graph fixture."""

    def test_lock_unlock_pattern_finds_fn1(self):
        from _builder.ast_pattern import search_pattern
        graph_dir = _make_pattern_graph_dir()
        results = search_pattern(graph_dir, "lock ( $L ) ; ... unlock ( $L ) ;")
        # fn1 has lock(mutex); do_work(); unlock(mutex); — should match
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["function"], "fn1")
        self.assertEqual(results[0]["bindings"], {"L": "mutex"})

    def test_self_comparison_pattern_finds_fn2(self):
        from _builder.ast_pattern import search_pattern
        graph_dir = _make_pattern_graph_dir()
        results = search_pattern(graph_dir, "$X == $X")
        # fn2 has 'if (a == a) return 1;' — a==a is a self-comparison
        self.assertGreaterEqual(len(results), 1)
        fn2_match = next((r for r in results if r["function"] == "fn2"), None)
        self.assertIsNotNone(fn2_match)
        self.assertEqual(fn2_match["bindings"], {"X": "a"})

    def test_use_after_free_pattern_finds_fn3(self):
        """free($P); ... *$P — use-after-free pattern."""
        from _builder.ast_pattern import search_pattern
        graph_dir = _make_pattern_graph_dir()
        results = search_pattern(graph_dir, "free ( $P ) ;")
        # fn3 has 'free(p); return *p;'
        self.assertGreaterEqual(len(results), 1)
        fn3_match = next((r for r in results if r["function"] == "fn3"), None)
        self.assertIsNotNone(fn3_match)
        self.assertEqual(fn3_match["bindings"], {"P": "p"})

    def test_limit_respected(self):
        from _builder.ast_pattern import search_pattern
        graph_dir = _make_pattern_graph_dir()
        # Pattern that matches all 3 functions
        results = search_pattern(graph_dir, "...", limit=2)
        self.assertLessEqual(len(results), 2)

    def test_no_match_returns_empty(self):
        from _builder.ast_pattern import search_pattern
        graph_dir = _make_pattern_graph_dir()
        # Pattern with a token that doesn't appear anywhere
        results = search_pattern(graph_dir, "nonexistent_xyz_function_name")
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
