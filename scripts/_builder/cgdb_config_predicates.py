"""cgdb_config_predicates — L3.5 #ifdef predicate extraction.

Per cgdb-architecture-and-poc-report.md 5.2.3, three-pass pipeline:
  Pass 1 (preprocess):  clang -E -dM -P source.c → macro universe
  Pass 2 (predicate):   regex PPCallbacks-style #ifdef tracking
                        → range_to_predicate map
  Pass 3 (AST walk):    each AST node queries Pass 2 by source range
                        → per-node config_predicate_id

The MVP uses Python regex for Pass 2 (libclang Python bindings don't expose
PPCallbacks directly). The production C++ clang plugin can do single-pass.

Predicate representation (per cdb 5.2.2):
- text_form: human-readable, e.g. '(defined(CONFIG_X) && !defined(CONFIG_Y))'
- z3_form:   SMT-LIB form, e.g. '(and (defined CONFIG_X) (not (defined CONFIG_Y)))'
             — only filled when z3-solver is available
- bdd_serialized: JSON-serialized BDD
             — only filled when `dd` library is available
- config_macros: list of macros referenced, e.g. ['CONFIG_X', 'CONFIG_Y']
- is_unconditional: True for code outside any #ifdef
- is_contradictory: True for #if 0 blocks (still indexed, marked)

Predicate IDs are stable hashes of text_form, so the same predicate in
different files deduplicates to the same row. We use a 60-bit hash to
fit in SQLite's signed 64-bit INTEGER.
"""
import hashlib
import json
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from _builder.cgdb_records import ConfigPredicateRecord


# Regex for preprocessor conditionals (matches #if, #ifdef, #ifndef, #elif, #else, #endif)
_PP_COND_RE = re.compile(
    r'^[ \t]*#[ \t]*(if|ifdef|ifndef|elif|else|endif)\b[^\S\n]*(.*)',
    re.MULTILINE,
)

# Pattern to extract CONFIG_* macros from a condition expression
_CONFIG_MACRO_RE = re.compile(r'\b([A-Z][A-Z0-9_]*)\b')


def predicate_id_for(text_form: str) -> int:
    """Stable 60-bit hash of text_form. Same text → same ID across files."""
    h = hashlib.sha256(text_form.encode('utf-8')).hexdigest()[:15]
    return int(h, 16) & 0x0FFF_FFFF_FFFF_FFFF


def _extract_config_macros(condition: str) -> List[str]:
    """Extract CONFIG_* style macro names from a condition expression.

    Returns sorted unique list of macro names. Excludes C keywords
    like 'defined' and numeric literals.
    """
    macros = set()
    # Strip out 'defined(...)' and 'defined X' to get the macro names inside
    cleaned = re.sub(r'\bdefined\b\s*[\(\s]*', ' ', condition)
    for m in _CONFIG_MACRO_RE.finditer(cleaned):
        name = m.group(1)
        # Skip pure C keywords
        if name in ('NULL', 'TRUE', 'FALSE', 'VOID', 'INT', 'CHAR'):
            continue
        macros.add(name)
    return sorted(macros)


def _to_z3_form(text_form: str) -> str:
    """Convert text_form to Z3 SMT-LIB form.

    Emit a real SMT-LIB2 string that Z3 can parse with
    `z3.Solver().from_string(...)`. Translation rules:
      - Bare identifier CONFIG_X  → Bool constant CONFIG_X
      - defined(CONFIG_X)         → CONFIG_X (Bool)
      - !X                        → (not X)
      - X && Y                    → (and X Y)
      - X || Y                    → (or X Y)
      - AND/OR/NOT tokens (from cross-language predicates) → and/or/not
      - CONFIG_FOO == 1 / CONFIG_FOO != 0  → CONFIG_FOO
      - CONFIG_FOO == 0 / CONFIG_FOO != 1  → (not CONFIG_FOO)

    Returns empty string if text_form is empty.
    """
    if not text_form:
        return ''
    s = text_form
    # Strip defined(...) wrappers — Z3 treats CONFIG_X as a Bool constant
    s = re.sub(r'\bdefined\s*\(\s*([A-Z_][A-Z0-9_]*)\s*\)', r'\1', s)
    s = re.sub(r'\bdefined\s+([A-Z_][A-Z0-9_]*)', r'\1', s)
    # Replace AND/OR/NOT tokens (used by cross-language predicates) → and/or/not
    s = re.sub(r'\bAND\b', 'and', s)
    s = re.sub(r'\bOR\b', 'or', s)
    s = re.sub(r'\bNOT\b(?!\s*=)', 'not', s)
    # Replace C-style operators
    s = s.replace('&&', 'and').replace('||', 'or')
    # ! that's not part of != → not
    s = re.sub(r'!(?!=)', 'not ', s)
    # Replace CONFIG_X == 1 / != 0  → CONFIG_X
    s = re.sub(r'\b([A-Z_][A-Z0-9_]*)\s*==\s*1\b', r'\1', s)
    s = re.sub(r'\b([A-Z_][A-Z0-9_]*)\s*!=\s*0\b', r'\1', s)
    # Replace CONFIG_X == 0 / != 1  → (not CONFIG_X)
    s = re.sub(r'\b([A-Z_][A-Z0-9_]*)\s*==\s*0\b', r'(not \1)', s)
    s = re.sub(r'\b([A-Z_][A-Z0-9_]*)\s*!=\s*1\b', r'(not \1)', s)
    return s.strip()


def _to_bdd_serialized(text_form: str) -> str:
    """Serialize a BDD-like representation of text_form.

    When `dd` library is available, build a real reduced BDD
    via `dd.autoref.BDD` and serialize as JSON {vars, nodes, root}. This
    enables O(1) equivalence checks and efficient satisfiability queries.

    When `dd` is unavailable, falls back to a JSON wrapper around text_form
    with type='text' — sufficient for indexing/dedup, but not for
    equivalence/satisfiability. Callers that need real BDD should check
    the `type` field.
    """
    if not text_form:
        return ''
    try:
        from dd.autoref import BDD
    except ImportError:
        # Fallback: text-form wrapper (same as before, but now with type tag)
        return json.dumps({'type': 'text', 'form': text_form},
                          ensure_ascii=False)
    try:
        bdd = BDD()
        # Collect variable names from text_form (CONFIG_*)
        varnames = sorted(set(re.findall(r'\b([A-Z_][A-Z0-9_]*)\b', text_form))
                          - {'AND', 'OR', 'NOT', 'defined', 'TRUE', 'FALSE',
                             'NULL', 'VOID', 'INT', 'CHAR'})
        # dd requires lowercase var names; remap CONFIG_X → config_x
        vmap = {v: v.lower() for v in varnames}
        for vlower in vmap.values():
            bdd.declare(vlower)
        # Translate text_form → Python expression that dd can eval
        expr = _text_form_to_bdd_expr(text_form, vmap)
        if not expr:
            return json.dumps({'type': 'text', 'form': text_form},
                              ensure_ascii=False)
        root = bdd.add_expr(expr)
        # Serialize: dump as dict {vars, root_node_id, support}
        # dd.BDD.to_expr gives a human-readable form; we store that plus
        # the var list so consumers can rebuild the BDD if needed.
        return json.dumps({
            'type': 'bdd',
            'vars': list(vmap.values()),
            'root_expr': bdd.to_expr(root),
            'text_form': text_form,
        }, ensure_ascii=False)
    except Exception:
        # Any error in BDD construction → fall back to text wrapper
        return json.dumps({'type': 'text', 'form': text_form},
                          ensure_ascii=False)


def _text_form_to_bdd_expr(text_form: str, vmap: Dict[str, str]) -> str:
    """Translate a config-predicate text_form into a Python boolean expression
    string that dd.autoref.BDD.add_expr can evaluate.

    Maps:
      CONFIG_X    → config_x (lowercase, via vmap)
      defined(X)  → X
      && → and, || → or, ! → not
      AND/OR/NOT  → and/or/not
      == 1 / != 0 → (var),  == 0 / != 1 → (~ var)
    """
    s = text_form
    # Strip defined(...) wrappers
    s = re.sub(r'\bdefined\s*\(\s*([A-Z_][A-Z0-9_]*)\s*\)', r'\1', s)
    s = re.sub(r'\bdefined\s+([A-Z_][A-Z0-9_]*)', r'\1', s)
    # Replace AND/OR/NOT tokens → and/or/not (dd wants lowercase Python)
    s = re.sub(r'\bAND\b', 'and', s)
    s = re.sub(r'\bOR\b', 'or', s)
    s = re.sub(r'\bNOT\b(?!\s*=)', 'not', s)
    # C-style operators
    s = s.replace('&&', 'and').replace('||', 'or')
    s = re.sub(r'!(?!=)', 'not ', s)
    # == 1 / != 0 → var; == 0 / != 1 → (not var)
    s = re.sub(r'\b([A-Z_][A-Z0-9_]*)\s*==\s*1\b', r'\1', s)
    s = re.sub(r'\b([A-Z_][A-Z0-9_]*)\s*!=\s*0\b', r'\1', s)
    s = re.sub(r'\b([A-Z_][A-Z0-9_]*)\s*==\s*0\b', r'(not \1)', s)
    s = re.sub(r'\b([A-Z_][A-Z0-9_]*)\s*!=\s*1\b', r'(not \1)', s)
    # Map CONFIG_X → config_x via vmap
    for vupper, vlower in vmap.items():
        s = re.sub(rf'\b{re.escape(vupper)}\b', vlower, s)
    return s


def evaluate_predicate(text_form: str, macro_bindings: Dict[str, bool]) -> Optional[bool]:
    """Evaluate a config predicate against a set of macro bindings.

    Prefer Z3 if available (handles arbitrary boolean formulas
    correctly); fall back to a Python-eval-based heuristic.

    Args:
      text_form: the predicate's text form (e.g., 'CONFIG_X && !CONFIG_Y')
      macro_bindings: dict mapping macro name → True/False

    Returns:
      True/False if the predicate can be evaluated, None if undecidable
      (e.g., a referenced macro is missing from macro_bindings).
    """
    if not text_form:
        return True  # empty predicate = unconditional = True
    # Try Z3 path first
    try:
        from z3 import Bool, Solver, sat, unsat, And, Or, Not, Implies
        # Build Z3 expression by string parsing
        z3_str = _to_z3_form(text_form)
        if not z3_str:
            return None
        # Replace each CONFIG_X with Bool('CONFIG_X')
        # Create Z3 bools for all referenced macros
        macros_in_form = set(re.findall(r'\bCONFIG_[A-Z0-9_]+\b', text_form))
        # Also include macros from the bindings dict that aren't in form
        all_macros = macros_in_form | set(macro_bindings.keys())
        # Build a Python expression for Z3 eval
        z3_vars = {m: Bool(m) for m in all_macros}
        # Translate text_form to Python expression
        expr_str = _text_form_to_python_expr(text_form, z3_vars)
        if not expr_str:
            return None
        try:
            expr = eval(expr_str, {"__builtins__": {}}, z3_vars)
        except Exception:
            return None
        solver = Solver()
        # Add macro bindings as constraints
        for m, val in macro_bindings.items():
            if m in z3_vars:
                if val:
                    solver.add(z3_vars[m])
                else:
                    solver.add(Not(z3_vars[m]))
        solver.add(expr)
        result = solver.check()
        if result == sat:
            # Also check negation — if both sat, it's not determined by bindings
            solver2 = Solver()
            for m, val in macro_bindings.items():
                if m in z3_vars:
                    if val:
                        solver2.add(z3_vars[m])
                    else:
                        solver2.add(Not(z3_vars[m]))
            solver2.add(Not(expr))
            result2 = solver2.check()
            if result2 == unsat:
                return True  # always true under bindings
            elif result2 == sat:
                # both expr and not expr are sat → undecidable
                return None
            return True
        elif result == unsat:
            return False
        return None
    except ImportError:
        pass
    # Fallback: Python eval
    try:
        py_expr = _text_form_to_python_expr_simple(text_form, macro_bindings)
        if py_expr is None:
            return None
        return bool(eval(py_expr, {"__builtins__": {}}, {}))
    except Exception:
        return None


def _text_form_to_python_expr(text_form: str, z3_vars: Dict[str, Any]) -> str:
    """Translate text_form to a Python expression string with Z3 vars inlined.
    Used by evaluate_predicate's Z3 path.
    """
    s = text_form
    s = re.sub(r'\bdefined\s*\(\s*([A-Z_][A-Z0-9_]*)\s*\)', r'\1', s)
    s = re.sub(r'\bdefined\s+([A-Z_][A-Z0-9_]*)', r'\1', s)
    s = re.sub(r'\bAND\b', 'and', s)
    s = re.sub(r'\bOR\b', 'or', s)
    s = re.sub(r'\bNOT\b(?!\s*=)', 'not', s)
    s = s.replace('&&', 'and').replace('||', 'or')
    s = re.sub(r'!(?!=)', 'not ', s)
    s = re.sub(r'\b([A-Z_][A-Z0-9_]*)\s*==\s*1\b', r'\1', s)
    s = re.sub(r'\b([A-Z_][A-Z0-9_]*)\s*!=\s*0\b', r'\1', s)
    s = re.sub(r'\b([A-Z_][A-Z0-9_]*)\s*==\s*0\b', r'(not \1)', s)
    s = re.sub(r'\b([A-Z_][A-Z0-9_]*)\s*!=\s*1\b', r'(not \1)', s)
    return s


def _text_form_to_python_expr_simple(text_form: str,
                                      macro_bindings: Dict[str, bool]) -> Optional[str]:
    """Translate text_form to a Python expression using True/False literals
    for bound macros. Returns None if a referenced macro is unbound.
    """
    s = text_form
    s = re.sub(r'\bdefined\s*\(\s*([A-Z_][A-Z0-9_]*)\s*\)', r'\1', s)
    s = re.sub(r'\bdefined\s+([A-Z_][A-Z0-9_]*)', r'\1', s)
    s = re.sub(r'\bAND\b', 'and', s)
    s = re.sub(r'\bOR\b', 'or', s)
    s = re.sub(r'\bNOT\b(?!\s*=)', 'not', s)
    s = s.replace('&&', 'and').replace('||', 'or')
    s = re.sub(r'!(?!=)', 'not ', s)
    s = re.sub(r'\b([A-Z_][A-Z0-9_]*)\s*==\s*1\b', r'\1', s)
    s = re.sub(r'\b([A-Z_][A-Z0-9_]*)\s*!=\s*0\b', r'\1', s)
    s = re.sub(r'\b([A-Z_][A-Z0-9_]*)\s*==\s*0\b', r'(not \1)', s)
    s = re.sub(r'\b([A-Z_][A-Z0-9_]*)\s*!=\s*1\b', r'(not \1)', s)
    # Substitute bound macros with True/False; unbound → return None
    def _macro_repl(m):
        name = m.group(0)
        if name in macro_bindings:
            return 'True' if macro_bindings[name] else 'False'
        return None
    # Find all CONFIG_* identifiers and replace
    parts = re.split(r'\b(CONFIG_[A-Z0-9_]+)\b', s)
    out_parts = []
    for i, part in enumerate(parts):
        if i % 2 == 1:  # macro name
            if part not in macro_bindings:
                return None
            out_parts.append('True' if macro_bindings[part] else 'False')
        else:
            out_parts.append(part)
    return ''.join(out_parts)


class ConfigPredicate:
    """An intermediate predicate representation during extraction.

    Deduplicates by text_form; emits ConfigPredicateRecord on finalize().
    """

    def __init__(self, text_form: str, is_unconditional: bool = False,
                 is_contradictory: bool = False):
        self.text_form = text_form
        self.z3_form = _to_z3_form(text_form) if text_form else ''
        self.bdd_serialized = _to_bdd_serialized(text_form) if text_form else ''
        self.config_macros = _extract_config_macros(text_form) if text_form else []
        self.is_unconditional = is_unconditional
        self.is_contradictory = is_contradictory
        self.id = predicate_id_for(text_form) if text_form else 0

    def to_record(self) -> ConfigPredicateRecord:
        return ConfigPredicateRecord(
            id=self.id,
            root_expr_id=None,
            text_form=self.text_form,
            z3_form=self.z3_form,
            bdd_serialized=self.bdd_serialized,
            config_macros=self.config_macros,
            is_unconditional=self.is_unconditional,
            is_contradictory=self.is_contradictory,
        )


# Sentinel predicates
UNCONDITIONAL = ConfigPredicate(text_form="", is_unconditional=True)
# For #if 0 blocks: still indexed (so we can find them) but marked contradictory
CONTRADICTORY = ConfigPredicate(text_form="0", is_contradictory=True)


class ConfigPredicateExtractor:
    """Three-pass config predicate extractor per cdb 5.2.3.

    Usage:
        extractor = ConfigPredicateExtractor()
        macro_universe = extractor.pass1_macro_universe(source_path)  # optional
        range_to_pred = extractor.pass2_range_to_predicate(source_text)
        # Then in AST walk:
        for node in ast_nodes:
            node.config_predicate_id = extractor.pass3_predicate_for_range(
                range_to_pred, node.byte_start, node.byte_end)
    """

    def __init__(self, macro_bindings: Optional[Dict[str, str]] = None):
        """Optionally takes macro_bindings (from build system detection) to
        evaluate #ifdef liveness — but for predicate *extraction*, we want
        the full predicate tree regardless of liveness (per user requirement:
        index ALL configs, not just one defconfig)."""
        self._macro_bindings = macro_bindings or {}

    # ── Pass 1: Macro Universe ─────────────────────────────────────────

    def pass1_macro_universe(self, source_path: str) -> Dict[str, str]:
        """Run clang -E -dM -P to extract all defined macros.

        Returns dict {macro_name: value}. For CONFIG_* macros, this gives
        the "macro universe" used by Z3 to constrain config queries.

        Falls back to empty dict if clang fails (e.g., missing header).
        """
        if not os.path.exists(source_path):
            return {}
        try:
            result = subprocess.run(
                ['clang', '-E', '-dM', '-P', source_path],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return {}
            macros = {}
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line.startswith('#define '):
                    continue
                rest = line[len('#define '):]
                parts = rest.split(None, 1)
                if len(parts) == 1:
                    macros[parts[0]] = ''
                elif len(parts) == 2:
                    macros[parts[0]] = parts[1].strip()
            return macros
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            return {}

    # ── Pass 2: Range → Predicate ──────────────────────────────────────

    def pass2_range_to_predicate(self, source_text: str) -> List[Tuple[int, int, ConfigPredicate]]:
        """Walk #ifdef directives, build a list of (byte_start, byte_end, predicate) ranges.

        Each range covers a region of source code where a particular
        config predicate holds. Ranges are non-overlapping; the outermost
        predicate wins (a node in nested #ifdefs gets the AND-combined
        predicate of all enclosing conditions).

        Implementation: maintain a stack of [start_pos, predicate] entries.
        On #if/#ifdef/#ifndef, push. On #elif/#else, emit a range for the
        current branch (start_pos → directive_pos) and replace the top with
        a new branch. On #endif, pop and emit a range for the closing branch.
        """
        ranges: List[Tuple[int, int, ConfigPredicate]] = []
        stack: List[List] = []  # each entry: [start_pos, predicate]

        # Find all preprocessor directives in source order
        directives = []
        for m in _PP_COND_RE.finditer(source_text):
            directives.append((m.start(), m.end(), m.group(1), m.group(2).strip()))
        directives.sort(key=lambda x: x[0])

        for pos, _end, directive, condition in directives:
            if directive in ('if', 'ifdef', 'ifndef'):
                # Build predicate for this branch
                pred = self._build_predicate(directive, condition, stack)
                # Branch starts AFTER the directive line — find the next newline
                # and start the range there, so the directive itself isn't
                # included in the range's "live" region.
                branch_start = source_text.find('\n', pos)
                if branch_start == -1:
                    branch_start = pos
                else:
                    branch_start += 1
                stack.append([branch_start, pred])
            elif directive in ('elif', 'else'):
                if not stack:
                    continue
                # Emit range for the current branch (from branch_start to this
                # directive's position).
                top = stack[-1]
                if top[0] < pos:
                    ranges.append((top[0], pos, top[1]))
                # Build the new branch's predicate
                if directive == 'elif':
                    new_pred = self._build_elif_predicate(condition, top[1])
                else:
                    new_pred = self._negate_predicate(top[1])
                # New branch starts AFTER this directive's line
                branch_start = source_text.find('\n', pos)
                if branch_start == -1:
                    branch_start = pos
                else:
                    branch_start += 1
                top[0] = branch_start
                top[1] = new_pred
            elif directive == 'endif':
                if not stack:
                    continue
                top = stack.pop()
                # Emit range for the closing branch
                if top[0] < pos:
                    ranges.append((top[0], pos, top[1]))

        # Close any unclosed ranges (unterminated #if — shouldn't happen in
        # valid C, but be defensive)
        end_pos = len(source_text)
        while stack:
            top = stack.pop()
            if top[0] < end_pos:
                ranges.append((top[0], end_pos, top[1]))

        # Sort ranges by start position
        ranges.sort(key=lambda r: r[0])
        return ranges

    def _build_predicate(self, directive: str, condition: str,
                         stack: List) -> ConfigPredicate:
        """Build a ConfigPredicate for an #if/#ifdef/#ifndef directive."""
        if directive == 'ifdef':
            text = f'defined({condition})' if condition else ''
        elif directive == 'ifndef':
            text = f'!defined({condition})' if condition else ''
        elif directive == 'if':
            # Handle #if 0 → contradictory
            if condition.strip() == '0':
                return ConfigPredicate(text_form='0', is_contradictory=True)
            # Handle #if 1 → unconditional (within the parent's predicate)
            if condition.strip() == '1':
                text = ''
            else:
                text = condition
        else:
            text = condition

        # Combine with parent predicate (AND)
        if stack:
            parent_pred = stack[-1][1]
            if parent_pred.text_form and parent_pred.is_unconditional is False:
                if parent_pred.is_contradictory:
                    # Parent is #if 0 → child is also contradictory
                    return ConfigPredicate(
                        text_form=f'0 /* under contradictory parent */',
                        is_contradictory=True,
                    )
                if text:
                    text = f'({parent_pred.text_form}) && ({text})'
                else:
                    text = parent_pred.text_form
            elif parent_pred.is_contradictory:
                return ConfigPredicate(
                    text_form='0 /* under contradictory parent */',
                    is_contradictory=True,
                )

        if not text:
            return UNCONDITIONAL
        return ConfigPredicate(text_form=text)

    def _build_elif_predicate(self, condition: str,
                              prev_pred: ConfigPredicate) -> ConfigPredicate:
        """Build predicate for #elif branch: NOT(prev) AND condition."""
        if condition.strip() == '0':
            return ConfigPredicate(text_form='0', is_contradictory=True)
        if condition.strip() == '1':
            # #elif 1 → just NOT(prev)
            if prev_pred.text_form:
                return ConfigPredicate(text_form=f'!({prev_pred.text_form})')
            return UNCONDITIONAL
        if prev_pred.text_form:
            text = f'!({prev_pred.text_form}) && ({condition})'
        else:
            text = condition
        return ConfigPredicate(text_form=text)

    def _negate_predicate(self, pred: ConfigPredicate) -> ConfigPredicate:
        """Build predicate for #else branch: NOT(prev)."""
        if pred.is_unconditional and not pred.text_form:
            # Parent was unconditional → #else branch is contradictory
            return ConfigPredicate(text_form='0', is_contradictory=True)
        if not pred.text_form:
            return UNCONDITIONAL
        if pred.is_contradictory:
            # Parent was #if 0 → #else is unconditional (or under parent's
            # parent's predicate, which we already combined in)
            return UNCONDITIONAL
        return ConfigPredicate(text_form=f'!({pred.text_form})')

    # ── Pass 3: AST node annotation ────────────────────────────────────

    def pass3_predicate_for_range(
        self,
        ranges: List[Tuple[int, int, ConfigPredicate]],
        byte_start: int,
        byte_end: int,
    ) -> ConfigPredicate:
        """Find the predicate that applies to a source range.

        A node's predicate is the predicate of the innermost range that
        fully contains [byte_start, byte_end). If no range contains it,
        returns UNCONDITIONAL.

        For overlapping ranges (nested #ifdefs), we want the innermost
        (most specific) range. Since ranges are sorted by start, we
        iterate and pick the smallest containing range.
        """
        if not ranges:
            return UNCONDITIONAL
        # Find all ranges that contain this node's source range
        candidates = []
        for r_start, r_end, pred in ranges:
            if r_start <= byte_start and byte_end <= r_end:
                candidates.append((r_end - r_start, pred))
        if not candidates:
            return UNCONDITIONAL
        # Pick the smallest containing range (innermost #ifdef)
        candidates.sort(key=lambda c: c[0])
        return candidates[0][1]

    # ── High-level: extract all predicates from a source file ──────────

    def extract_predicates(self, source_text: str) -> List[ConfigPredicate]:
        """Extract the set of unique predicates from a source file.

        Returns a list of ConfigPredicate objects, deduplicated by
        text_form. Useful for populating the config_predicates table
        without needing to annotate individual nodes.
        """
        ranges = self.pass2_range_to_predicate(source_text)
        seen = {}
        for _start, _end, pred in ranges:
            if pred.id not in seen:
                seen[pred.id] = pred
        # Always include UNCONDITIONAL
        if UNCONDITIONAL.id not in seen:
            seen[UNCONDITIONAL.id] = UNCONDITIONAL
        return list(seen.values())

    def annotate_nodes(self, source_text: str,
                       nodes: List[dict]) -> Tuple[List[ConfigPredicate], List[dict]]:
        """Annotate a list of node dicts with config_predicate_id.

        Each node dict should have 'byte_start' and 'byte_end' keys.
        Returns (predicates, nodes_with_pred_ids) where:
          - predicates is the deduplicated list of unique predicates
          - nodes_with_pred_ids is the same list with 'config_predicate_id'
            key added to each node

        Nodes that fall outside any #ifdef get UNCONDITIONAL's id.
        """
        ranges = self.pass2_range_to_predicate(source_text)
        seen_preds = {UNCONDITIONAL.id: UNCONDITIONAL}
        for node in nodes:
            bs = node.get('byte_start', 0)
            be = node.get('byte_end', bs)
            pred = self.pass3_predicate_for_range(ranges, bs, be)
            node['config_predicate_id'] = pred.id
            if pred.id not in seen_preds:
                seen_preds[pred.id] = pred
        return list(seen_preds.values()), nodes
