#!/usr/bin/env python3
"""Path feasibility analysis for Code2Database.

Auto-solves path feasibility so engineers don't have to manually specify
variable bindings like `mode=1,flag=true`. Three modes of operation:

1. With Z3 (optional dependency): full SMT-based feasibility checking.
   Given a path's accumulated conditions (if (mode==1) ... if (flag)),
   Z3 determines if there's a satisfying assignment, and if so, returns
   it as auto-derived bindings.

2. Without Z3 (graceful fallback): heuristic constraint propagation.
   Recognizes common patterns (==, !=, <, >, &&, ||, defined()) and
   propagates constraints syntactically. Less precise but covers the
   common cases.

3. Combined: try Z3 first, fall back to heuristic, mark confidence.

Engineer question this answers: "I see a path f→g→h in the graph, but
under what conditions does it actually execute?" — instead of asking the
engineer to specify bindings, we derive them.

Usage:
    from _builder.path_feasibility import solve_path_feasibility
    result = solve_path_feasibility(conditions=['mode == 1', 'flag', 'x > 0'])
    # result = {'feasible': True, 'bindings': {'mode': 1, 'flag': True, 'x': 'positive'},
    #           'solver': 'z3', 'confidence': 'EXTRACTED'}
"""

import os
import re
import sys
from typing import Optional, List, Dict, Any, Tuple

# Try to import Z3 (optional dependency)
try:
    from z3 import (
        Int, Bool, Real, String, Solver, And, Or, Not, sat, unsat, unknown,
        parse_smt2_string, simplify, BoolVal, IntVal
    )
    Z3_AVAILABLE = True
except ImportError:
    Z3_AVAILABLE = False


def is_z3_available() -> bool:
    """Return True if Z3 SMT solver is available."""
    return Z3_AVAILABLE


# ---------------------------------------------------------------------------
# Condition parsing — extract simple constraints from C-like expressions
# ---------------------------------------------------------------------------

# Recognized comparison patterns (C-like)
_COMPARISON_RE = re.compile(
    r'(\w+)\s*(==|!=|<=|>=|<|>)\s*(\d+|0x[0-9a-fA-F]+|\'\w\'|"[^"]*"|\w+)'
)

# Boolean variable references (e.g., `flag` or `!flag`)
_BOOL_RE = re.compile(r'(?<![\w.])(!?)\s*(\w+)')

# defined() macro checks
_DEFINED_RE = re.compile(r'defined\s*\(\s*(\w+)\s*\)')

# Negation prefix
_NEG_PREFIX_RE = re.compile(r'^!\s*')


def parse_condition(cond: str) -> List[Dict]:
    """Parse a C-like condition into a list of constraint atoms.

    Returns a list of dicts, each describing one atom:
        {var, op, value, type}  where type is 'int', 'bool', or 'macro'

    Example:
        parse_condition('mode == 1 && flag') →
        [{var: 'mode', op: '==', value: 1, type: 'int'},
         {var: 'flag', op: 'truthy', value: True, type: 'bool'}]
    """
    if not cond:
        return []
    atoms = []
    # Strip surrounding #ifdef / #if
    cond = re.sub(r'^#ifdef\s+', '', cond)
    cond = re.sub(r'^#if\s+', '', cond)
    cond = re.sub(r'^#elif\s+', '', cond)

    # Handle defined() macros
    for m in _DEFINED_RE.finditer(cond):
        atoms.append({"var": m.group(1), "op": "defined", "value": True,
                      "type": "macro"})

    # Handle negated defined() — !defined(X)
    for m in re.finditer(r'!\s*defined\s*\(\s*(\w+)\s*\)', cond):
        atoms.append({"var": m.group(1), "op": "defined", "value": False,
                      "type": "macro"})

    # Handle comparisons
    for m in _COMPARISON_RE.finditer(cond):
        var, op, val = m.group(1), m.group(2), m.group(3)
        # Skip if var is a C keyword
        if var in ('if', 'else', 'while', 'for', 'return', 'switch', 'case'):
            continue
        # Parse value
        if val.startswith('0x'):
            parsed_val = int(val, 16)
            vtype = 'int'
        elif val.isdigit() or (val.startswith('-') and val[1:].isdigit()):
            parsed_val = int(val)
            vtype = 'int'
        elif val.startswith("'") and val.endswith("'"):
            parsed_val = ord(val.strip("'"))
            vtype = 'int'
        else:
            parsed_val = val
            vtype = 'symbol'
        atoms.append({"var": var, "op": op, "value": parsed_val, "type": vtype})

    # Handle bare boolean variables (no comparison) — only if no comparison atom
    # already covers them
    covered_vars = {a["var"] for a in atoms}
    # Remove !defined() patterns first to avoid double-counting
    cleaned = re.sub(r'!\s*defined\s*\(\s*\w+\s*\)', ' ', cond)
    cleaned = re.sub(r'defined\s*\(\s*\w+\s*\)', ' ', cleaned)
    # Remove already-parsed comparisons
    cleaned = _COMPARISON_RE.sub(' ', cleaned)
    # Remove binary operators and standalone digits, but PRESERVE leading '!'
    # so that `!flag` is parsed as negated-truthy rather than as `flag`.
    cleaned = re.sub(r'(&&|\|\||[&|<>])(\d*)', ' ', cleaned)
    cleaned = re.sub(r'\b\d+\b', ' ', cleaned)
    for m in _BOOL_RE.finditer(cleaned):
        neg, var = m.group(1), m.group(2)
        if var in ('if', 'else', 'while', 'for', 'return', 'switch', 'case',
                   'defined', 'NULL', 'true', 'false'):
            continue
        if var in covered_vars:
            continue
        atoms.append({"var": var, "op": "truthy", "value": (not neg),
                      "type": "bool"})
        covered_vars.add(var)

    return atoms


# ---------------------------------------------------------------------------
# Z3-based feasibility solver
# ---------------------------------------------------------------------------

def _atoms_to_z3(atoms: List[Dict]):
    """Convert parsed atoms to a Z3 expression."""
    if not Z3_AVAILABLE:
        return None
    var_cache: Dict[str, Any] = {}

    def _get_var(name: str, vtype: str):
        if name not in var_cache:
            if vtype == 'int':
                var_cache[name] = Int(name)
            elif vtype == 'bool':
                var_cache[name] = Bool(name)
            else:
                var_cache[name] = Int(name)  # default to int
        return var_cache[name]

    constraints = []
    for atom in atoms:
        var = _get_var(atom["var"], atom["type"])
        op = atom["op"]
        val = atom["value"]
        if op == "==":
            constraints.append(var == val)
        elif op == "!=":
            constraints.append(var != val)
        elif op == "<":
            constraints.append(var < val)
        elif op == ">":
            constraints.append(var > val)
        elif op == "<=":
            constraints.append(var <= val)
        elif op == ">=":
            constraints.append(var >= val)
        elif op == "truthy":
            if val:
                constraints.append(var)
            else:
                constraints.append(Not(var))
        elif op == "defined":
            # Macros: treat as boolean. defined(X) being True means X is defined.
            constraints.append(Bool(atom["var"]) == val)
    return And(constraints) if constraints else BoolVal(True)


def solve_with_z3(conditions: List[str]) -> Optional[Dict]:
    """Use Z3 to solve path feasibility.

    Returns None if Z3 unavailable or solving fails.
    """
    if not Z3_AVAILABLE:
        return None

    all_atoms = []
    for cond in conditions:
        all_atoms.extend(parse_condition(cond))

    if not all_atoms:
        return {"feasible": True, "bindings": {}, "solver": "z3",
                "confidence": "EXTRACTED", "model": None}

    try:
        solver = Solver()
        formula = _atoms_to_z3(all_atoms)
        solver.add(formula)
        result = solver.check()
        if result == sat:
            model = solver.model()
            bindings = {}
            for decl in model.decls():
                name = decl.name()
                val = model[decl]
                # Convert Z3 value to Python value
                try:
                    if val.is_int():
                        bindings[name] = val.as_long()
                    else:
                        bindings[name] = str(val)
                except Exception:
                    bindings[name] = str(val)
            return {"feasible": True, "bindings": bindings, "solver": "z3",
                    "confidence": "EXTRACTED", "model": str(model)}
        elif result == unsat:
            return {"feasible": False, "bindings": {}, "solver": "z3",
                    "confidence": "EXTRACTED", "model": None,
                    "reason": "constraints unsatisfiable"}
        else:
            return {"feasible": None, "bindings": {}, "solver": "z3",
                    "confidence": "AMBIGUOUS", "model": None,
                    "reason": "solver returned unknown"}
    except Exception as exc:
        return {"feasible": None, "bindings": {}, "solver": "z3",
                "confidence": "AMBIGUOUS", "model": None,
                "reason": f"z3 error: {exc}"}


# ---------------------------------------------------------------------------
# Heuristic feasibility solver (fallback without Z3)
# ---------------------------------------------------------------------------

def _split_conjunctions(cond: str) -> List[str]:
    """Split a condition string on top-level && (not inside parentheses).

    Used to treat `a == 1 && b == 2` as two separate conjuncts, each of which
    must hold. Disjunctions (||) are kept inside a single conjunct so the
    solver can apply the "at-least-one-branch" rule.
    """
    if not cond:
        return []
    parts = []
    depth = 0
    buf = []
    i = 0
    while i < len(cond):
        c = cond[i]
        if c == '(':
            depth += 1
            buf.append(c)
        elif c == ')':
            depth = max(0, depth - 1)
            buf.append(c)
        elif depth == 0 and cond[i:i+2] == '&&':
            parts.append(''.join(buf).strip())
            buf = []
            i += 2
            continue
        else:
            buf.append(c)
        i += 1
    if buf:
        parts.append(''.join(buf).strip())
    return [p for p in parts if p]


def _split_disjunctions(conjunct: str) -> List[str]:
    """Split a conjunct on top-level || (not inside parentheses).

    Used to treat `a == 1 || b == 2` as two branches, at least one of which
    must hold. The solver tries each branch and accepts feasibility if any
    single branch is consistent with current bindings.
    """
    if not conjunct:
        return []
    parts = []
    depth = 0
    buf = []
    i = 0
    while i < len(conjunct):
        c = conjunct[i]
        if c == '(':
            depth += 1
            buf.append(c)
        elif c == ')':
            depth = max(0, depth - 1)
            buf.append(c)
        elif depth == 0 and conjunct[i:i+2] == '||':
            parts.append(''.join(buf).strip())
            buf = []
            i += 2
            continue
        else:
            buf.append(c)
        i += 1
    if buf:
        parts.append(''.join(buf).strip())
    return [p for p in parts if p]


def _merge_range_constraint(existing: Any, op: str, val: Any) -> Tuple[Optional[str], Optional[str]]:
    """Combine an existing range binding (e.g., '>5') with a new comparison
    into a tighter range string.

    Returns (new_binding, contradiction_msg):
        new_binding: the updated binding string (e.g., '>5,<10') or None if
                     no change needed.
        contradiction_msg: a string explaining a hard contradiction (e.g.,
                           'x > 10 conflicts with x < 5'), or None if consistent.
    """
    if existing is None:
        return f"{op}{val}", None
    # Existing is a concrete value — verify the new comparison holds
    if isinstance(existing, (int, float)):
        if op == '>' and not (existing > val):
            return None, f"x {op} {val} conflicts with existing {existing}"
        if op == '<' and not (existing < val):
            return None, f"x {op} {val} conflicts with existing {existing}"
        if op == '>=' and not (existing >= val):
            return None, f"x {op} {val} conflicts with existing {existing}"
        if op == '<=' and not (existing <= val):
            return None, f"x {op} {val} conflicts with existing {existing}"
        return None, None  # consistent, no new info
    # Existing is a range string — check for obvious range contradictions
    if isinstance(existing, str):
        # Parse existing range constraints
        parts = [p.strip() for p in existing.split(',') if p.strip()]
        for p in parts:
            m = re.match(r'^(>=|<=|>|<)(.+)$', p)
            if not m:
                continue
            ex_op, ex_val_s = m.group(1), m.group(2)
            try:
                ex_val = int(ex_val_s, 0) if ex_val_s.startswith('0x') else int(ex_val_s)
            except ValueError:
                continue
            # Lower bound (op or ex_op is > or >=) vs upper bound (< or <=)
            new_is_lower = op in ('>', '>=')
            ex_is_lower = ex_op in ('>', '>=')
            new_is_upper = op in ('<', '<=')
            ex_is_upper = ex_op in ('<', '<=')
            if new_is_lower and ex_is_upper:
                # new says x > val, existing says x < ex_val (or <=)
                if val >= ex_val:
                    return None, f"x {op} {val} conflicts with existing {p}"
            if new_is_upper and ex_is_lower:
                if val <= ex_val:
                    return None, f"x {op} {val} conflicts with existing {p}"
        return f"{existing},{op}{val}", None
    return f"{op}{val}", None


def _try_branch(bindings: Dict[str, Any], branch_cond: str,
                contradictions: List[str]) -> Dict[str, Any]:
    """Try a single disjunction branch against the current bindings.

    Returns the bindings that would result if this branch were taken.
    Mutates `contradictions` only if a hard contradiction is found within
    this branch (so caller can decide whether to keep or discard).
    """
    trial = dict(bindings)
    branch_contras = []
    atoms = parse_condition(branch_cond)
    for atom in atoms:
        var = atom["var"]
        op = atom["op"]
        val = atom["value"]
        if op == "==":
            if var in trial and trial[var] != val:
                branch_contras.append(f"{var}: {trial[var]} vs {val}")
            else:
                trial[var] = val
        elif op == "!=":
            if trial.get(var) == val:
                branch_contras.append(f"{var} != {val} but already = {val}")
            else:
                trial.setdefault(var, f"!={val}")
        elif op == "truthy":
            if val:
                if trial.get(var) is False:
                    branch_contras.append(f"{var} true vs false")
                else:
                    trial.setdefault(var, True)
            else:
                if trial.get(var) is True:
                    branch_contras.append(f"!{var} vs true")
                else:
                    trial.setdefault(var, False)
        elif op == "defined":
            if var in trial and trial[var] != val:
                branch_contras.append(
                    f"{var}: defined={trial[var]} vs defined={val}")
            else:
                trial.setdefault(var, val)
        elif op in ("<", ">", "<=", ">="):
            new_range, contra = _merge_range_constraint(trial.get(var), op, val)
            if contra:
                branch_contras.append(f"{var} {contra}")
            elif new_range is not None:
                trial[var] = new_range
    return {"bindings": trial, "contradictions": branch_contras}


def solve_with_heuristic(conditions: List[str]) -> Dict:
    """Heuristic feasibility solver — no Z3 required.

    Constraint propagation with conjunction and disjunction support:
    - Splits each condition on top-level `&&` into conjuncts (all must hold).
    - Splits each conjunct on top-level `||` into branches (at least one must hold).
    - For each conjunct: applies == / != / < / > / <= / >= / truthy / defined
      bindings, detects contradictions (e.g., `mode == 1` and `mode == 2`).
    - For disjunctions: tries each branch against accumulated bindings; the
      conjunct is feasible if at least one branch is consistent.
    - Range constraints accumulate (e.g., `x > 0 && x < 10` → `x = >0,<10`).

    Returns a dict with feasible/bindings/confidence/contradictions.
    """
    bindings: Dict[str, Any] = {}
    contradictions: List[str] = []

    for cond in conditions:
        for conjunct in _split_conjunctions(cond):
            branches = _split_disjunctions(conjunct)
            if len(branches) <= 1:
                # Pure conjunction — apply atoms directly
                atoms = parse_condition(conjunct)
                for atom in atoms:
                    var = atom["var"]
                    op = atom["op"]
                    val = atom["value"]
                    if op == "==":
                        if var in bindings and bindings[var] != val:
                            contradictions.append(f"{var}: {bindings[var]} vs {val}")
                        else:
                            bindings[var] = val
                    elif op == "!=":
                        if bindings.get(var) == val:
                            contradictions.append(f"{var} != {val} but already = {val}")
                        else:
                            bindings.setdefault(var, f"!={val}")
                    elif op == "truthy":
                        if val:
                            if bindings.get(var) is False:
                                contradictions.append(f"{var} true vs false")
                            else:
                                bindings.setdefault(var, True)
                        else:
                            if bindings.get(var) is True:
                                contradictions.append(f"!{var} vs true")
                            else:
                                bindings.setdefault(var, False)
                    elif op == "defined":
                        if var in bindings and bindings[var] != val:
                            contradictions.append(
                                f"{var}: defined={bindings[var]} vs defined={val}")
                        else:
                            bindings.setdefault(var, val)
                    elif op in ("<", ">", "<=", ">="):
                        new_range, contra = _merge_range_constraint(
                            bindings.get(var), op, val)
                        if contra:
                            contradictions.append(f"{var} {contra}")
                        elif new_range is not None:
                            bindings[var] = new_range
            else:
                # Disjunction — at least one branch must be consistent
                feasible_branches = []
                for branch in branches:
                    trial = _try_branch(bindings, branch, contradictions)
                    if not trial["contradictions"]:
                        feasible_branches.append((branch, trial["bindings"]))
                if not feasible_branches:
                    contradictions.append(
                        f"no feasible branch for disjunction: {conjunct}")
                else:
                    # Conservatively keep the first feasible branch's bindings
                    # for new variables (do not overwrite existing).
                    _, new_b = feasible_branches[0]
                    for k, v in new_b.items():
                        if k not in bindings:
                            bindings[k] = v

    if contradictions:
        return {"feasible": False, "bindings": bindings, "solver": "heuristic",
                "confidence": "INFERRED", "contradictions": contradictions}
    return {"feasible": True, "bindings": bindings, "solver": "heuristic",
            "confidence": "INFERRED"}


# ---------------------------------------------------------------------------
# Combined solver: try Z3 first, fall back to heuristic
# ---------------------------------------------------------------------------

def solve_path_feasibility(conditions: List[str],
                           prefer_z3: bool = True) -> Dict:
    """Solve path feasibility, auto-choosing the best available solver.

    Args:
        conditions: List of condition strings from a path (e.g., from
            edge call_condition attributes accumulated along the path).
        prefer_z3: If True and Z3 is available, use Z3 (more precise).
            Otherwise use heuristic.

    Returns:
        Dict with keys:
            feasible: True / False / None (unknown)
            bindings: dict of derived variable assignments
            solver: 'z3' / 'heuristic'
            confidence: 'EXTRACTED' (Z3) / 'INFERRED' (heuristic) / 'AMBIGUOUS'
            contradictions: list (heuristic only, when infeasible)
            reason: str (when infeasible or unknown)
    """
    if not conditions:
        return {"feasible": True, "bindings": {}, "solver": "none",
                "confidence": "EXTRACTED"}

    if prefer_z3 and Z3_AVAILABLE:
        result = solve_with_z3(conditions)
        if result is not None:
            return result

    return solve_with_heuristic(conditions)


def auto_solve_path_bindings(conditions: List[str]) -> Dict:
    """Convenience: return just the bindings, for use by resolve-chain.

    If infeasible, returns empty bindings (so the caller can prune the path).
    If feasible, returns the derived bindings as a string suitable for
    resolve-chain's --bindings argument.
    """
    result = solve_path_feasibility(conditions)
    if not result.get("feasible"):
        return {"bindings_str": "", "feasible": False, "raw": result}
    bindings = result.get("bindings", {})
    # Convert to "k=v,k=v" format
    parts = []
    for k, v in bindings.items():
        if isinstance(v, bool):
            parts.append(f"{k}={'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            parts.append(f"{k}={v}")
        else:
            # Skip symbolic/unknown
            pass
    return {"bindings_str": ",".join(parts), "feasible": True, "raw": result}


# ---------------------------------------------------------------------------
# Config-aware path feasibility (integrates with cgdb config predicates)
# ---------------------------------------------------------------------------

def check_config_feasible(config_predicates: List[str],
                          macro_bindings: Dict[str, bool],
                          prefer_z3: bool = True) -> Dict:
    """Check feasibility of accumulated config predicates under macro bindings.

    Combines two layers:
      1. Each predicate is evaluated against the macro bindings using
         evaluate_predicate (Z3-aware, falls back to Python eval).
      2. All predicates are AND-joined and solved as a single path
         feasibility problem (so local symbolic constraints like
         `mode==1` interact correctly with macro-config predicates).

    Args:
      config_predicates: list of predicate text forms, e.g.
          ['CONFIG_X86', 'CONFIG_X && !CONFIG_Y', 'CONFIG_Z || CONFIG_W']
      macro_bindings: dict mapping macro name to True/False, e.g.
          {'CONFIG_X86': True, 'CONFIG_X': True, 'CONFIG_Y': False}
      prefer_z3: if True and Z3 is available, use Z3 (more precise).

    Returns:
      Dict with keys:
        feasible: True / False / None (unknown)
        predicates: echo of input predicates
        macro_bindings: echo of input bindings
        per_predicate: list of per-predicate evaluation results
        solver: 'z3' / 'heuristic' / 'none'
        confidence: 'EXTRACTED' / 'INFERRED' / 'AMBIGUOUS'
        reason: str (when infeasible or unknown)
    """
    results = {
        "feasible": True,
        "predicates": list(config_predicates),
        "macro_bindings": dict(macro_bindings),
        "per_predicate": [],
        "solver": "none",
        "confidence": "EXTRACTED",
    }
    if not config_predicates:
        results["feasible"] = True
        results["solver"] = "none"
        return results

    try:
        from _builder.cgdb_config_predicates import evaluate_predicate
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        try:
            from _builder.cgdb_config_predicates import evaluate_predicate
        except ImportError:
            evaluate_predicate = None

    overall = True
    any_unknown = False
    solver = "heuristic"
    if prefer_z3 and Z3_AVAILABLE:
        solver = "z3"
    results["solver"] = solver

    for pred in config_predicates:
        if not pred:
            results["per_predicate"].append({
                "predicate": pred,
                "value": True,
                "decidable": True,
            })
            continue
        if evaluate_predicate is not None:
            val = evaluate_predicate(pred, macro_bindings)
        else:
            val = _evaluate_predicate_fallback(pred, macro_bindings)
        if val is None:
            any_unknown = True
            results["per_predicate"].append({
                "predicate": pred,
                "value": None,
                "decidable": False,
                "reason": "missing macro binding or undecidable",
            })
        else:
            results["per_predicate"].append({
                "predicate": pred,
                "value": bool(val),
                "decidable": True,
            })
            if val is False:
                overall = False

    if overall is False:
        results["feasible"] = False
        results["reason"] = "at least one config predicate evaluates to False under given macro bindings"
        results["confidence"] = "EXTRACTED"
    elif any_unknown:
        results["feasible"] = None
        results["reason"] = "at least one config predicate is undecidable under given macro bindings (missing macro)"
        results["confidence"] = "AMBIGUOUS"
    else:
        results["feasible"] = True
        results["confidence"] = "EXTRACTED"

    # Also try the combined solve to catch cross-predicate contradictions
    # that per-predicate evaluation might miss.
    try:
        combined = solve_path_feasibility(config_predicates, prefer_z3=prefer_z3)
        if combined.get("feasible") is False and overall is True:
            results["feasible"] = False
            results["reason"] = "combined predicate set is unsatisfiable"
            results["confidence"] = combined.get("confidence", "INFERRED")
        elif combined.get("feasible") is None and overall is True:
            results["feasible"] = None
            results["reason"] = "combined predicate set is undecidable"
            results["confidence"] = combined.get("confidence", "AMBIGUOUS")
    except Exception:
        pass
    return results


def _evaluate_predicate_fallback(text_form: str,
                                  macro_bindings: Dict[str, bool]) -> Optional[bool]:
    """Fallback predicate evaluation when cgdb_config_predicates is unavailable.

    Handles simple cases: a single macro name, possibly negated, possibly
    joined by && / || with other macros. Returns None if undecidable.
    """
    if not text_form:
        return True
    s = text_form.strip()
    # Strip outer parentheses
    while s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()
    # Single macro (possibly negated)
    if re.fullmatch(r'!?[A-Z_][A-Z0-9_]*', s):
        neg = s.startswith("!")
        name = s.lstrip("!")
        if name not in macro_bindings:
            return None
        val = bool(macro_bindings[name])
        return (not val) if neg else val
    # AND of macros
    if "&&" in s and "||" not in s:
        parts = [p.strip() for p in s.split("&&")]
        vals = []
        for p in parts:
            v = _evaluate_predicate_fallback(p, macro_bindings)
            if v is None:
                return None
            vals.append(v)
        return all(vals)
    # OR of macros
    if "||" in s and "&&" not in s:
        parts = [p.strip() for p in s.split("||")]
        vals = []
        for p in parts:
            v = _evaluate_predicate_fallback(p, macro_bindings)
            if v is None:
                return None
            vals.append(v)
        return any(vals)
    # defined(X) / !defined(X)
    m = re.fullmatch(r'!?defined\s*\(\s*([A-Z_][A-Z0-9_]*)\s*\)', s)
    if m:
        name = m.group(1)
        neg = s.startswith("!")
        if name not in macro_bindings:
            return None
        val = bool(macro_bindings[name])
        return (not val) if neg else val
    return None


def parse_macro_bindings(spec: str) -> Dict[str, bool]:
    """Parse a 'CONFIG_X=true,CONFIG_Y=false' string into a dict.

    Accepts true/false, 1/0, yes/no, on/off (case-insensitive).
    Bare names without =value are treated as =true.
    """
    bindings: Dict[str, bool] = {}
    if not spec:
        return bindings
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "=" in tok:
            name, val = tok.split("=", 1)
            name = name.strip()
            val = val.strip().lower()
            if val in ("true", "1", "yes", "on"):
                bindings[name] = True
            elif val in ("false", "0", "no", "off"):
                bindings[name] = False
            else:
                bindings[name] = True
        else:
            bindings[tok] = True
    return bindings


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def cmd_path_feasible(args):
    """Check feasibility of a path's conditions.

    Usage:
        path-feasible --graph <dir> --conditions "mode==1,flag,x>0"
        path-feasible --graph <dir> --node <id> --max-depth 5
        path-feasible --graph <dir> --node <id> --with-configs "CONFIG_X=true,CONFIG_Y=false"
    """
    import json

    graph_dir = args.graph
    with_configs_spec = getattr(args, "with_configs", "") or ""
    macro_bindings = parse_macro_bindings(with_configs_spec) if with_configs_spec else {}

    if getattr(args, "conditions", ""):
        # Direct condition list mode
        conds = [c.strip() for c in args.conditions.split(",") if c.strip()]
        result = solve_path_feasibility(conds)
        # If config bindings provided, also evaluate as config predicates
        if macro_bindings:
            cfg_result = check_config_feasible(conds, macro_bindings)
            result["config_feasibility"] = cfg_result
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    if getattr(args, "node", ""):
        # Walk a path from node and accumulate conditions
        try:
            from _builder.graph_build import _load_full_graph
        except ImportError:
            sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
            from _builder.graph_build import _load_full_graph
        G = _load_full_graph(graph_dir)
        from _builder.utils import _find_node_id
        node_id = _find_node_id(G, args.node)
        if not node_id:
            print(f"Node not found: {args.node}", file=sys.stderr)
            sys.exit(1)
        max_depth = getattr(args, "max_depth", 5)

        # If a cgdb store is available, also load config_predicate_id → text_form map
        cp_map = {}
        cgdb_store = None
        try:
            db_path = os.path.join(graph_dir, "code2database.db")
            if os.path.exists(db_path):
                try:
                    from _builder.cgdb_store import SQLiteCGDBStore
                except ImportError:
                    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
                    from _builder.cgdb_store import SQLiteCGDBStore
                try:
                    cgdb_store = SQLiteCGDBStore(db_path)
                    cp_map = cgdb_store.load_config_predicates_map()
                except Exception:
                    cgdb_store = None
        except Exception:
            pass

        # DFS collecting paths, their conditions, and config predicates
        paths = []
        def _walk(nid, path, conds, cfg_preds, depth):
            if depth >= max_depth:
                return
            for succ in G.successors(nid):
                ed = G.get_edge_data(nid, succ) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                cond = ed.get("call_condition", "")
                new_conds = conds + ([cond] if cond else [])
                # Accumulate config predicates from the edge
                new_cfg_preds = list(cfg_preds)
                cp_id = ed.get("config_predicate_id")
                if cp_id and cp_map and cp_id in cp_map:
                    tf = cp_map[cp_id].get("text_form", "")
                    if tf and tf not in new_cfg_preds:
                        new_cfg_preds.append(tf)
                new_path = path + [succ]
                if not list(G.successors(succ)):
                    paths.append((new_path, new_conds, new_cfg_preds))
                else:
                    _walk(succ, new_path, new_conds, new_cfg_preds, depth + 1)
        _walk(node_id, [node_id], [], [], 0)
        # RPT-KERNEL-D13: runtime guard analysis (bd_prepare_to_claim / sb_is_blkdev_sb /
        # bd_holder != / mutex_is_locked) — complements compile-time #ifdef analysis.
        try:
            from _builder.runtime_guards import check_runtime_guards
            _runtime_guards_available = True
        except ImportError:
            _runtime_guards_available = False
        results = []
        for path, conds, cfg_preds in paths[:50]:  # cap
            feasible = solve_path_feasibility(conds)
            entry = {
                "path": [G.nodes[n].get("name", n) for n in path],
                "conditions": conds,
                "feasibility": feasible,
            }
            if cfg_preds:
                entry["config_predicates"] = cfg_preds
                if macro_bindings:
                    entry["config_feasibility"] = check_config_feasible(
                        cfg_preds, macro_bindings)
                else:
                    entry["config_feasibility"] = {
                        "feasible": None,
                        "reason": "no --with-configs bindings provided",
                        "per_predicate": [],
                    }
            # RPT-KERNEL-D13: attach runtime guard analysis
            if _runtime_guards_available and conds:
                entry["runtime_guards"] = check_runtime_guards(conds)
            results.append(entry)
        if cgdb_store is not None:
            try:
                cgdb_store.close()
            except Exception:
                pass
        print(json.dumps({
            "start": G.nodes[node_id].get("name", node_id),
            "paths": results,
            "z3_available": Z3_AVAILABLE,
            "macro_bindings": macro_bindings,
        }, ensure_ascii=False, indent=2, default=str))
        return

    print("Specify --conditions or --node", file=sys.stderr)
    sys.exit(1)


def cmd_path_guards(args):
    """Prove reachability of a writer from an entry point, using guard conditions.

    IMPROVE-OPT4: Given an entry point (--from) and a writer function (--to),
    find all paths from entry to writer, accumulate guard conditions along
    each path (from edge call_condition + function-body guard_condition for
    the target field write), and use Z3/heuristic to prove whether the
    conjunction of guards is satisfiable.

    If ALL paths are infeasible (guards contradict) → writer is unreachable
    in scene → reachable_in_scene should be "guarded_out" (stronger than
    Optimization 1's "guarded" heuristic).

    Usage:
        path-guards --graph <dir> --from <entry> --to <writer>
                    [--field <field_name>] [--value <value>]
                    [--with-configs "CONFIG_X=true"]
    """
    import json
    from collections import deque

    graph_dir = args.graph
    from_node = getattr(args, "from_node", "") or ""
    to_node = getattr(args, "to_node", "") or ""
    field_name = getattr(args, "field", "") or ""
    value_filter = getattr(args, "value", "") or ""
    max_depth = getattr(args, "max_depth", 8)
    with_configs_spec = getattr(args, "with_configs", "") or ""
    macro_bindings = parse_macro_bindings(with_configs_spec) if with_configs_spec else {}

    if not from_node or not to_node:
        print("Specify --from and --to", file=sys.stderr)
        sys.exit(1)

    try:
        from _builder.graph_build import _load_full_graph
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from _builder.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)
    from _builder.utils import _find_node_id

    from_id = _find_node_id(G, from_node)
    to_id = _find_node_id(G, to_node)
    if not from_id:
        print(f"Entry node not found: {from_node}", file=sys.stderr)
        sys.exit(1)
    if not to_id:
        print(f"Writer node not found: {to_node}", file=sys.stderr)
        sys.exit(1)

    # Forward BFS from entry to writer, collecting all simple paths
    paths = []
    queue = deque([(from_id, [from_id], [])])  # (nid, path, edge_conds)
    seen_paths = set()
    while queue and len(paths) < 50:
        nid, path, conds = queue.popleft()
        if nid == to_id:
            key = tuple(path)
            if key not in seen_paths:
                seen_paths.add(key)
                paths.append((path, conds))
            continue
        if len(path) >= max_depth:
            continue
        for succ in G.successors(nid):
            if succ in path:
                continue
            ed = G.get_edge_data(nid, succ) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            cond = ed.get("call_condition", "")
            new_conds = conds + ([cond] if cond else [])
            queue.append((succ, path + [succ], new_conds))

    # For each path, also collect the writer's guard_condition for the target field
    to_ndata = G.nodes.get(to_id, {})
    writer_guards = []
    if field_name:
        for fw in to_ndata.get("fields_written", []):
            if fw.get("field_name", "") == field_name:
                g = fw.get("guard_condition", "")
                if g:
                    writer_guards.append(g)
                # If value filter, also check assigned_value
                if value_filter:
                    av = fw.get("assigned_value", "")
                    # Only consider this writer if its assigned_value matches
                    # (for NULL-form, use the helper)
                    from _builder.query import _value_is_null_form, _value_is_null_form_match
                    if av:
                        if value_filter and not (av == value_filter or _value_is_null_form_match(av, value_filter)):
                            continue
                break

    # Solve feasibility for each path
    results = []
    for path, edge_conds in paths:
        all_conds = edge_conds + writer_guards
        feasible = solve_path_feasibility(all_conds)
        entry = {
            "path": [G.nodes[n].get("name", n) for n in path],
            "edge_conditions": edge_conds,
            "writer_guards": writer_guards,
            "all_conditions": all_conds,
            "feasibility": feasible,
        }
        # If config bindings provided, also evaluate config feasibility
        if macro_bindings and all_conds:
            cfg_result = check_config_feasible(all_conds, macro_bindings)
            entry["config_feasibility"] = cfg_result
            # If config says infeasible, override verdict for this path
            if cfg_result.get("feasible") is False:
                entry["feasibility"]["feasible"] = False
                entry["feasibility"]["reason"] = "guarded_out_by_config"
        results.append(entry)

    # Overall verdict: writer is reachable if ANY path is feasible
    any_feasible = any(r["feasibility"].get("feasible") for r in results)
    all_infeasible = bool(results) and all(
        r["feasibility"].get("feasible") is False for r in results)
    if any_feasible:
        verdict = "reachable_in_scene"
    elif all_infeasible:
        verdict = "guarded_out_proven"
    else:
        verdict = "unknown"

    print(json.dumps({
        "from": G.nodes[from_id].get("name", from_id),
        "to": G.nodes[to_id].get("name", to_id),
        "field": field_name,
        "value_filter": value_filter,
        "paths_analyzed": len(results),
        "verdict": verdict,
        "reachable_in_scene": verdict == "reachable_in_scene",
        "paths": results,
        "z3_available": Z3_AVAILABLE,
        "macro_bindings": macro_bindings,
    }, ensure_ascii=False, indent=2, default=str))
