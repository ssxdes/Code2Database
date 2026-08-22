"""Tests for path feasibility heuristic solver.

Covers conjunction (&&), disjunction (||), negation (!), range constraints
(<, >, <=, >=), defined()/!defined() and their contradictions.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.path_feasibility import (  # noqa: E402
    parse_condition,
    solve_with_heuristic,
    _split_conjunctions,
    _split_disjunctions,
)


def test_parse_simple_equality():
    atoms = parse_condition('mode == 1')
    assert len(atoms) == 1
    assert atoms[0]['var'] == 'mode'
    assert atoms[0]['op'] == '=='
    assert atoms[0]['value'] == 1


def test_parse_conjunction_atoms():
    atoms = parse_condition('mode == 1 && flag')
    var_ops = {(a['var'], a['op']) for a in atoms}
    assert ('mode', '==') in var_ops
    assert ('flag', 'truthy') in var_ops


def test_parse_negated_bool():
    atoms = parse_condition('!flag')
    assert len(atoms) == 1
    assert atoms[0]['var'] == 'flag'
    assert atoms[0]['op'] == 'truthy'
    assert atoms[0]['value'] is False


def test_split_conjunctions():
    parts = _split_conjunctions('a == 1 && b == 2 && c == 3')
    assert parts == ['a == 1', 'b == 2', 'c == 3']


def test_split_conjunctions_with_parens():
    parts = _split_conjunctions('(a == 1 || b == 2) && c == 3')
    assert parts == ['(a == 1 || b == 2)', 'c == 3']


def test_split_disjunctions():
    parts = _split_disjunctions('a == 1 || b == 2 || c == 3')
    assert parts == ['a == 1', 'b == 2', 'c == 3']


def test_split_disjunctions_with_parens():
    parts = _split_disjunctions('(a == 1) || (b == 2)')
    assert parts == ['(a == 1)', '(b == 2)']


def test_heuristic_simple_equality():
    r = solve_with_heuristic(['mode == 1'])
    assert r['feasible'] is True
    assert r['bindings']['mode'] == 1


def test_heuristic_contradiction():
    r = solve_with_heuristic(['mode == 1', 'mode == 2'])
    assert r['feasible'] is False
    assert r['contradictions']


def test_heuristic_disjunction_feasible():
    r = solve_with_heuristic(['mode == 1 || mode == 2'])
    assert r['feasible'] is True


def test_heuristic_disjunction_with_existing_binding():
    r = solve_with_heuristic(['mode == 1', 'mode == 1 || mode == 2'])
    assert r['feasible'] is True
    assert r['bindings']['mode'] == 1


def test_heuristic_disjunction_all_branches_contradict():
    r = solve_with_heuristic(['mode == 1', 'mode == 2 || mode == 3'])
    assert r['feasible'] is False


def test_heuristic_range_constraint_combined():
    r = solve_with_heuristic(['x > 0 && x < 10'])
    assert r['feasible'] is True
    assert '>0' in r['bindings']['x']
    assert '<10' in r['bindings']['x']


def test_heuristic_range_contradiction():
    r = solve_with_heuristic(['x > 10', 'x < 5'])
    assert r['feasible'] is False


def test_heuristic_negation_contradiction():
    r = solve_with_heuristic(['!flag', 'flag'])
    assert r['feasible'] is False


def test_heuristic_defined_contradiction():
    r = solve_with_heuristic(['defined(CONFIG_X)', '!defined(CONFIG_X)'])
    assert r['feasible'] is False


def test_heuristic_defined_consistent():
    r = solve_with_heuristic(['defined(CONFIG_X)', 'defined(CONFIG_X)'])
    assert r['feasible'] is True
    assert r['bindings']['CONFIG_X'] is True


def test_heuristic_not_equal_consistent():
    r = solve_with_heuristic(['x != 5'])
    assert r['feasible'] is True


def test_heuristic_not_equal_contradiction_with_equal():
    r = solve_with_heuristic(['x == 5', 'x != 5'])
    assert r['feasible'] is False


def test_heuristic_empty_conditions():
    r = solve_with_heuristic([])
    assert r['feasible'] is True
    assert r['bindings'] == {}


def test_heuristic_compound_condition_with_parens():
    r = solve_with_heuristic(['(a == 1 || b == 2) && c == 3'])
    # Conservative over-approximation: feasible (both a and b bound, c bound)
    assert r['feasible'] is True
    assert r['bindings']['c'] == 3


def test_heuristic_negation_with_conjunction():
    r = solve_with_heuristic(['!flag && mode == 0'])
    assert r['feasible'] is True
    assert r['bindings']['flag'] is False
    assert r['bindings']['mode'] == 0
