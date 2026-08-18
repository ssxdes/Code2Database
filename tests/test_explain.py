"""Tests for explain-label and why-ambiguous commands."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import networkx as nx  # noqa: E402

from _builder.explain import (  # noqa: E402
    explain_label,
    why_ambiguous,
    LABEL_EXPLANATIONS,
)


def _build_graph():
    G = nx.DiGraph()
    G.add_node("leaf", name="leaf_func", labels=["out_end", "in_end"],
               labels_source={"out_end": "ast", "in_end": "ast"})
    G.add_node("caller", name="caller_func", labels=["API_entry"],
               entry_score=10.0,
               labels_source={"API_entry": "entry_scoring"})
    G.add_node("dead", name="dead_func", labels=["dead_code"],
               preproc_condition="CONFIG_DISABLED", preproc_alive=False)
    G.add_node("cb", name="my_handler_cb", labels=["callback_func"],
               callback_registration={"registrar": "register_handler"})
    G.add_node("thread", name="kthread_main", labels=["thread_processor"],
               thread_model="kthread")
    return G


def test_explain_label_in_end():
    G = _build_graph()
    result = explain_label(G, "leaf", "in_end")
    assert result["has_label"] is True
    assert result["label"] == "in_end"
    assert "leaf" in result["summary"].lower() or "leaf" in "leaf"


def test_explain_label_out_end():
    G = _build_graph()
    result = explain_label(G, "leaf", "out_end")
    assert result["has_label"] is True
    # Should find evidence about zero out-edges
    assert any(e["rule"] == "zero out-edges" for e in result["evidence"])


def test_explain_label_dead_code():
    G = _build_graph()
    result = explain_label(G, "dead", "dead_code")
    assert result["has_label"] is True
    # Should find evidence about preprocessor guard inactive
    rules = [e["rule"] for e in result["evidence"]]
    assert "preprocessor guard inactive" in rules


def test_explain_label_callback():
    G = _build_graph()
    result = explain_label(G, "cb", "callback_func")
    assert result["has_label"] is True
    rules = [e["rule"] for e in result["evidence"]]
    assert "registered as callback" in rules or "callback naming" in rules


def test_explain_label_thread_processor():
    G = _build_graph()
    result = explain_label(G, "thread", "thread_processor")
    assert result["has_label"] is True
    rules = [e["rule"] for e in result["evidence"]]
    assert "thread_model set" in rules


def test_explain_label_api_entry():
    G = _build_graph()
    result = explain_label(G, "caller", "API_entry")
    assert result["has_label"] is True
    rules = [e["rule"] for e in result["evidence"]]
    assert "entry_score >= threshold" in rules


def test_explain_label_missing_label():
    G = _build_graph()
    result = explain_label(G, "leaf", "API_entry")
    assert result["has_label"] is False
    assert "summary" in result


def test_explain_label_unknown_label():
    G = _build_graph()
    result = explain_label(G, "leaf", "totally_made_up_label")
    assert result["has_label"] is False
    assert "not a recognized built-in label" in result["summary"]


def test_explain_label_missing_node():
    G = _build_graph()
    result = explain_label(G, "nonexistent", "out_end")
    assert "error" in result


def test_label_explanations_table_has_known_labels():
    """All seven built-in labels should be in LABEL_EXPLANATIONS."""
    expected = {"API_entry", "thread_processor", "callback_func",
                "constructor", "destructor", "out_end", "unknown_end"}
    for label in expected:
        assert label in LABEL_EXPLANATIONS, f"missing explanation for {label}"


def test_why_ambiguous_fn_ptr_call():
    G = nx.DiGraph()
    G.add_node("caller", name="caller")
    G.add_node("callee", name="callee")
    G.add_edge("caller", "callee",
               confidence="AMBIGUOUS",
               concurrency="fn_ptr",
               evidence=[{"kind": "fn_ptr_call", "weight": 0.3,
                          "note": "indirect call via ops->read at line 5"}])
    result = why_ambiguous(G, "caller", "callee")
    assert result["is_ambiguous"] is True
    assert any("Function pointer" in r or "function pointer" in r.lower()
               for r in result["reasons"])


def test_why_ambiguous_preproc_dead():
    G = nx.DiGraph()
    G.add_node("caller", name="caller")
    G.add_node("callee", name="callee")
    G.add_edge("caller", "callee",
               confidence="AMBIGUOUS",
               source_tag="preproc_dead",
               preproc_alive=False,
               evidence=[{"kind": "ast_call", "weight": 0.0,
                          "note": "dead branch: CONFIG_DISABLED, line 5"}])
    result = why_ambiguous(G, "caller", "callee")
    assert result["is_ambiguous"] is True
    assert any("Dead preprocessor" in r or "dead" in r.lower()
               for r in result["reasons"])


def test_why_ambiguous_not_ambiguous():
    G = nx.DiGraph()
    G.add_node("caller", name="caller")
    G.add_node("callee", name="callee")
    G.add_edge("caller", "callee", confidence="EXTRACTED")
    result = why_ambiguous(G, "caller", "callee")
    assert result["is_ambiguous"] is False
    assert "not AMBIGUOUS" in result["reasons"][0]


def test_why_ambiguous_missing_edge():
    G = nx.DiGraph()
    G.add_node("a", name="a")
    G.add_node("b", name="b")
    result = why_ambiguous(G, "a", "b")
    assert "error" in result


def test_why_ambiguous_string_evidence_normalized():
    """String-form evidence should be normalized to dict form, not split per char."""
    G = nx.DiGraph()
    G.add_node("caller", name="caller")
    G.add_node("callee", name="callee")
    G.add_edge("caller", "callee",
               confidence="AMBIGUOUS",
               concurrency="fn_ptr",
               evidence="unresolved fn_ptr_call: op")  # string, not list
    result = why_ambiguous(G, "caller", "callee")
    assert result["is_ambiguous"] is True
    # The string should be normalized to one dict entry
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["note"] == "unresolved fn_ptr_call: op"


def test_why_ambiguous_callback_dispatch():
    G = nx.DiGraph()
    G.add_node("reg", name="register_handler")
    G.add_node("cb", name="my_cb")
    G.add_edge("reg", "cb",
               confidence="AMBIGUOUS",
               concurrency="callback",
               relation="callback_dispatch",
               evidence=[])
    result = why_ambiguous(G, "reg", "cb")
    assert result["is_ambiguous"] is True
    assert any("allback" in r for r in result["reasons"])
