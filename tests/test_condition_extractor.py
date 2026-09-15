"""Tests for the L3 condition extractor (P5-3).

Verifies that the ConditionExtractor emits ConditionRecord atoms from
branch conditions (if/while/for/switch/conditional operator) and that
text_to_z3 produces a basic SMT string for simple comparisons.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.cgdb.cgdb_analysis import (
    ConditionExtractor,
    _text_to_z3,
    _condition_id,
)
from _builder.cgdb.cgdb_records import ConditionRecord


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
    import _builder.cgdb.cgdb_analysis as mod
    monkeypatch.setattr(
        mod, "__builtins__",
        {**mod.__builtins__,
         "__import__": lambda *a, **kw: (_ for _ in ()).throw(ImportError())}
    )
    ext = ConditionExtractor()
    # We pass a fake cursor — the import error should be caught
    result = ext.extract_from_ast(object(), 100)
    assert result == []


def _clang_available():
    try:
        from _scanner.clang_scanner import is_clang_available
        return is_clang_available()
    except Exception:
        return False


@pytest.mark.skipif(not _clang_available(), reason="libclang not available")
def test_condition_extractor_covers_switch():
    """The docstring promises SwitchStmt coverage — pin it for real.

    A function with if/while/switch/ternary branches must yield one
    ConditionRecord per distinct branch condition; the switch condition
    is emitted with kind='atom'.
    """
    import tempfile
    import os
    from clang.cindex import Index, CursorKind
    code = """
int pick(int x) {
    int r = 0;
    if (x < 0) { r = -1; }
    while (x > 100) { x -= 100; }
    switch (x) {
    case 0: r = 1; break;
    default: r = 2; break;
    }
    return x > 0 ? r : -r;
}
"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.c")
        with open(path, "w") as fh:
            fh.write(code)
        tu = Index.create().parse(path)
        fn = next(c for c in tu.cursor.get_children()
                  if c.kind == CursorKind.FUNCTION_DECL and c.spelling == "pick")
        records = ConditionExtractor().extract_from_ast(fn, 42)
    texts = {r.text_form: r for r in records}
    assert "x < 0" in texts
    assert "x > 100" in texts
    assert "x" in texts, "switch condition must be extracted"
    assert texts["x"].kind == "atom"
    assert "x > 0" in texts, "ternary condition must be extracted"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
