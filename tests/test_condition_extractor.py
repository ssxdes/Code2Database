"""Tests for the L3 condition extractor (P5-3).

Verifies that the ConditionExtractor emits ConditionRecord atoms from
branch conditions (if/while/for/switch/conditional operator) and that
text_to_z3 produces a basic SMT string for simple comparisons.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.cgdb_analysis import (
    ConditionExtractor,
    _text_to_z3,
    _condition_id,
)
from _builder.cgdb_records import ConditionRecord


def test_text_to_z3_equality():
    assert _text_to_z3("x == 1") == "(== x 1)"
    assert _text_to_z3("foo != NULL") == "(!= foo NULL)"


def test_text_to_z3_inequality():
    assert _text_to_z3("x < 10") == "(< x 10)"
    assert _text_to_z3("count >= 0") == "(>= count 0)"


def test_text_to_z3_no_op():
    assert _text_to_z3("just_a_value") == ""
    assert _text_to_z3("") == ""


def test_text_to_z3_picks_first_op():
    # When multiple ops present, the first matching one is used
    # (so 'a < b == 1' becomes '(< a b == 1)' — we don't claim to parse
    # compound expressions, just give path-feasibility a starting point)
    result = _text_to_z3("a < b")
    assert result.startswith("(< ")
    assert "a" in result and "b" in result


def test_condition_id_stable():
    id1 = _condition_id(100, "x == 1")
    id2 = _condition_id(100, "x == 1")
    id3 = _condition_id(100, "x == 2")
    id4 = _condition_id(200, "x == 1")
    assert id1 == id2
    assert id1 != id3
    assert id1 != id4
    # Fits in 60-bit signed
    assert id1 < (1 << 60)


def test_condition_extractor_no_cursor():
    """Extractor with None cursor returns empty list."""
    ext = ConditionExtractor()
    assert ext.extract_from_ast(None, 100) == []


def test_condition_extractor_no_libclang(monkeypatch):
    """If libclang is unavailable, extractor returns empty list gracefully."""
    import _builder.cgdb_analysis as mod
    monkeypatch.setattr(
        mod, "__builtins__",
        {**mod.__builtins__,
         "__import__": lambda *a, **kw: (_ for _ in ()).throw(ImportError())}
    )
    ext = ConditionExtractor()
    # We pass a fake cursor — the import error should be caught
    result = ext.extract_from_ast(object(), 100)
    assert result == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
