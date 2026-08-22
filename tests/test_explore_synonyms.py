"""Tests for explore-flow synonym expansion."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.explore import (  # noqa: E402
    _tokenize_query,
    _expand_synonyms,
    _score_node_relevance,
)


def test_tokenize_camelcase():
    assert _tokenize_query("myApiInit") == ["my", "api", "init"]


def test_tokenize_snake_case():
    assert _tokenize_query("module_open") == ["module", "open"]


def test_tokenize_chinese():
    tokens = _tokenize_query("释放 task 内存")
    assert "释放" in tokens
    assert "task" in tokens
    assert "内存" in tokens


def test_expand_synonyms_chinese_release():
    """Chinese '释放' should expand to release/free/put/destroy/done/exit etc."""
    tokens = _tokenize_query("释放")
    expanded = _expand_synonyms(tokens)
    expanded_strs = [t for t, _ in expanded]
    # Original comes first
    assert expanded_strs[0] == "释放"
    # Synonyms are added
    assert "release" in expanded_strs
    assert "free" in expanded_strs
    assert "put" in expanded_strs
    assert "destroy" in expanded_strs
    # is_synonym flag is correctly set
    is_syn = [is_syn for _, is_syn in expanded]
    assert is_syn[0] is False  # original
    assert is_syn[1] is True   # first synonym


def test_expand_synonyms_english_alloc():
    """English 'alloc' should expand to alloc/new/create/make/get/init."""
    tokens = _tokenize_query("alloc")
    expanded = _expand_synonyms(tokens)
    expanded_strs = [t for t, _ in expanded]
    assert "alloc" in expanded_strs
    assert "new" in expanded_strs
    assert "create" in expanded_strs
    assert "make" in expanded_strs


def test_expand_synonyms_no_duplicates():
    """Repeated tokens should not produce duplicate synonyms."""
    tokens = _tokenize_query("alloc alloc")
    expanded = _expand_synonyms(tokens)
    expanded_strs = [t for t, _ in expanded]
    assert expanded_strs.count("alloc") == 1  # deduplicated
    assert expanded_strs.count("new") == 1


def test_score_with_synonyms_matches_release_task():
    """Scoring with '释放' synonym expansion should match release_task / free_task."""
    nd_release = {"name": "release_task", "domain": "test",
                  "signature": "", "semantic_desc": "", "labels": []}
    nd_free = {"name": "free_task", "domain": "test",
               "signature": "", "semantic_desc": "", "labels": []}
    nd_unrelated = {"name": "compute_hash", "domain": "test",
                    "signature": "", "semantic_desc": "", "labels": []}

    tokens = _tokenize_query("释放")
    expanded = _expand_synonyms(tokens)

    score_release = _score_node_relevance(nd_release, expanded)
    score_free = _score_node_relevance(nd_free, expanded)
    score_unrelated = _score_node_relevance(nd_unrelated, expanded)

    assert score_release > 0  # matched "release" synonym
    assert score_free > 0     # matched "free" synonym
    assert score_unrelated == 0  # no match


def test_original_token_gets_higher_score_than_synonym():
    """Original-token match should score higher than synonym-only match."""
    nd_release = {"name": "release_task", "domain": "test",
                  "signature": "", "semantic_desc": "", "labels": []}
    nd_free = {"name": "free_task", "domain": "test",
               "signature": "", "semantic_desc": "", "labels": []}

    # Query "release" — release_task matches original token, free_task matches synonym
    tokens = _tokenize_query("release")
    expanded = _expand_synonyms(tokens)

    score_release = _score_node_relevance(nd_release, expanded)
    score_free = _score_node_relevance(nd_free, expanded)
    # release_task matches the original "release" token (weight 1.0)
    # free_task matches a synonym (weight 0.5)
    # Both also match "task" but that's the same for both
    assert score_release > score_free


def test_score_with_chinese_query_matches_all_release_variants():
    """Chinese '释放 task' should score free_task/put_task/destroy_task/release_task."""
    nds = [
        {"name": "free_task", "domain": "t", "labels": []},
        {"name": "put_task", "domain": "t", "labels": []},
        {"name": "destroy_task", "domain": "t", "labels": []},
        {"name": "release_task", "domain": "t", "labels": []},
        {"name": "alloc_task", "domain": "t", "labels": []},
        {"name": "compute_hash", "domain": "t", "labels": []},
    ]
    tokens = _tokenize_query("释放 task")
    expanded = _expand_synonyms(tokens)

    scores = {nd["name"]: _score_node_relevance(nd, expanded) for nd in nds}
    # All four release-variants should match
    assert scores["free_task"] > 0
    assert scores["put_task"] > 0
    assert scores["destroy_task"] > 0
    assert scores["release_task"] > 0
    # alloc_task should match "task" only (not the release synonyms)
    assert scores["alloc_task"] > 0
    # compute_hash should not match
    assert scores["compute_hash"] == 0
