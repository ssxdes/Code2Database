"""config_predicates_lang — cross-language config predicate extraction.

Per cgdb-architecture-and-poc-report.md 5.5.5 (L3.5 config layer), every
node should carry a config_predicate_id. The C/C++ clang_scanner uses
ConfigPredicateExtractor for #ifdef tracking; this module provides
language-specific predicates for Go, Python, Java, Rust, ASM:

  - Go: //go:build tags  →  CONFIG_GO_TAG_<NAME>
  - Rust: #[cfg(...)]     →  CONFIG_CFG_<NAME> / CONFIG_FEATURE_<NAME>
  - Python: sys.platform / os.name checks → CONFIG_PY_PLATFORM_<VAL>
  - Java: @Conditional / @Profile → CONFIG_JAVA_PROFILE_<NAME>
  - ASM: #ifdef / #ifndef / #if (already tracked in pp_conds)

The module returns ConfigPredicate objects (reuses the existing
ConfigPredicate class from cgdb_config_predicates.py) and a per-node
predicate_id mapping based on byte ranges.

This is the bridge that brings non-C/C++ languages onto the same L3.5
config layer as the clang path.
"""
import bisect
import re
from typing import Any, Dict, List, Optional, Tuple

from _builder.cgdb_config_predicates import (
    ConfigPredicate, UNCONDITIONAL, CONTRADICTORY,
)


# ---- Go: //go:build tag extraction ----

_GO_BUILD_RE = re.compile(
    r'^\s*//go:build\s+(.+?)\s*$', re.MULTILINE
)


def extract_go_build_predicates(source_text: str) -> List[Tuple[int, int, ConfigPredicate]]:
    """Parse //go:build directives and return list of (start_byte, end_byte,
    ConfigPredicate) tuples covering the file body that follows each directive.

    Go convention: a //go:build line at the top of the file gates the entire
    file. Multiple //go:build lines can be OR'd (rare) but we treat each as
    gating from its line until the next //go:build line or EOF.
    """
    results: List[Tuple[int, int, ConfigPredicate]] = []
    matches = list(_GO_BUILD_RE.finditer(source_text))
    if not matches:
        return results
    for i, m in enumerate(matches):
        expr = m.group(1).strip()
        text_form = _go_expr_to_text(expr)
        if not text_form:
            continue
        pred = ConfigPredicate(text_form=text_form)
        start_byte = m.end()
        end_byte = matches[i+1].start() if i+1 < len(matches) else len(source_text)
        results.append((start_byte, end_byte, pred))
    return results


def _go_expr_to_text(expr: str) -> str:
    """Convert a Go build expression to a config-predicate text_form.

    Maps identifiers to CONFIG_GO_TAG_<NAME>, preserves operators
    (&&, ||, !) by translating to AND/OR/NOT.
    """
    tokens = re.findall(r'\(|\)|&&|\|\||!|[A-Za-z_][A-Za-z0-9_]*', expr)
    out: List[str] = []
    for tok in tokens:
        if tok in ('(', ')'):
            out.append(tok)
        elif tok == '&&':
            out.append('AND')
        elif tok == '||':
            out.append('OR')
        elif tok == '!':
            out.append('NOT')
        elif tok[0].isalpha() or tok[0] == '_':
            out.append(f'CONFIG_GO_TAG_{tok.upper()}')
        else:
            out.append(tok)
    return ' '.join(out)


# ---- Rust: #[cfg(...)] / #[cfg_attr(...)] / #[feature(...)] ----

_RUST_CFG_RE = re.compile(
    r'#\[\s*cfg\s*\(\s*(.+?)\s*\)\s*\]', re.DOTALL
)
_RUST_FEATURE_RE = re.compile(
    r'#\[\s*feature\s*=\s*"([^"]+)"\s*\]', re.DOTALL
)


def extract_rust_cfg_predicates(source_text: str) -> List[Tuple[int, int, ConfigPredicate]]:
    """Parse #[cfg(...)] attributes and return list of (start_byte, end_byte,
    ConfigPredicate) tuples covering the item the attribute decorates.

    The byte range is the attribute's start to the next attribute or EOF —
    a coarse heuristic. For MVP, this captures "this region is gated by cfg".
    """
    results: List[Tuple[int, int, ConfigPredicate]] = []
    cfg_matches = list(_RUST_CFG_RE.finditer(source_text))
    feature_matches = list(_RUST_FEATURE_RE.finditer(source_text))
    all_matches = [(m.start(), m.end(), m.group(1), 'cfg') for m in cfg_matches]
    all_matches += [(m.start(), m.end(), m.group(1), 'feature') for m in feature_matches]
    all_matches.sort()
    for i, (start, end, expr, kind) in enumerate(all_matches):
        if kind == 'cfg':
            text_form = _rust_cfg_to_text(expr)
        else:
            text_form = f'CONFIG_FEATURE_{expr.upper()}'
        if not text_form:
            continue
        pred = ConfigPredicate(text_form=text_form)
        # The cfg gates from the end of the attribute to the next attribute
        # or EOF — coarse, but enough for "this function is cfg-gated" detection.
        range_start = end
        range_end = all_matches[i+1][0] if i+1 < len(all_matches) else len(source_text)
        results.append((range_start, range_end, pred))
    return results


def _rust_cfg_to_text(expr: str) -> str:
    """Convert a Rust cfg expression to text_form.

    Rust cfg syntax: `target_os = "linux"`, `feature = "serde"`,
    `not(debug_assertions)`, `any(a, b)`, `all(a, b)`.

    Maps:
      target_os = "linux"      → CONFIG_CFG_TARGET_OS_LINUX
      feature = "serde"        → CONFIG_FEATURE_SERDE
      not(...)                 → NOT (...)
      any(...)                 → OR(...)
      all(...)                 → AND(...)
    """
    expr = expr.strip()
    m = re.match(r'^not\s*\((.+)\)$', expr, re.DOTALL)
    if m:
        return f'NOT ({_rust_cfg_to_text(m.group(1))})'
    m = re.match(r'^any\s*\((.+)\)$', expr, re.DOTALL)
    if m:
        parts = _split_commas(m.group(1))
        return '(' + ' OR '.join(_rust_cfg_to_text(p) for p in parts) + ')'
    m = re.match(r'^all\s*\((.+)\)$', expr, re.DOTALL)
    if m:
        parts = _split_commas(m.group(1))
        return '(' + ' AND '.join(_rust_cfg_to_text(p) for p in parts) + ')'
    m = re.match(r'^(\w+)\s*=\s*"([^"]+)"$', expr)
    if m:
        key, val = m.group(1), m.group(2)
        if key == 'feature':
            return f'CONFIG_FEATURE_{val.upper()}'
        return f'CONFIG_CFG_{key.upper()}_{val.upper()}'
    m = re.match(r'^(\w+)$', expr)
    if m:
        return f'CONFIG_CFG_{m.group(1).upper()}'
    return ''


def _split_commas(s: str) -> List[str]:
    """Split a string by commas, respecting nested parens."""
    parts: List[str] = []
    depth = 0
    cur: List[str] = []
    for ch in s:
        if ch == '(':
            depth += 1
            cur.append(ch)
        elif ch == ')':
            depth -= 1
            cur.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append(''.join(cur).strip())
    return parts


# ---- Python: sys.platform / os.name / platform.python_version checks ----

_PY_PLATFORM_RE = re.compile(
    r"sys\.platform\s*(==|!=)\s*['\"]([^'\"]+)['\"]"
)
_PY_OS_NAME_RE = re.compile(
    r"os\.name\s*(==|!=)\s*['\"]([^'\"]+)['\"]"
)


def extract_python_platform_predicates(source_text: str) -> List[Tuple[int, int, ConfigPredicate]]:
    """Find sys.platform / os.name checks and emit predicates gating the
    guarded branch (if/elif block). Returns (start_byte, end_byte, pred) list.

    For MVP, we emit a coarse range from the check to EOF. A more precise
    implementation would parse the if-block boundaries, but that requires
    AST traversal. Coarse is enough to flag platform-gated modules.
    """
    results: List[Tuple[int, int, ConfigPredicate]] = []
    matches: List[Tuple[int, int, str, str, str]] = []
    for m in _PY_PLATFORM_RE.finditer(source_text):
        matches.append((m.start(), m.end(), m.group(1), m.group(2), 'platform'))
    for m in _PY_OS_NAME_RE.finditer(source_text):
        matches.append((m.start(), m.end(), m.group(1), m.group(2), 'os_name'))
    matches.sort()
    for i, (start, end, op, val, kind) in enumerate(matches):
        if op == '==':
            text_form = f'CONFIG_PY_{kind.upper()}_{val.upper()}'
        else:  # !=
            text_form = f'NOT CONFIG_PY_{kind.upper()}_{val.upper()}'
        pred = ConfigPredicate(text_form=text_form)
        range_end = matches[i+1][0] if i+1 < len(matches) else len(source_text)
        results.append((end, range_end, pred))
    return results


# ---- Java: @Conditional / @Profile annotations ----

_JAVA_CONDITIONAL_RE = re.compile(
    r'@Conditional\s*\(\s*(?:[A-Za-z_][\w\.]*)\.class\s*(?:,\s*[A-Za-z_][\w\.]*\.class\s*)*\)'
)
_JAVA_PROFILE_RE = re.compile(
    r'@Profile\s*\(\s*(?:"([^"]+)"|\{([^}]*)\})\s*\)'
)


def extract_java_profile_predicates(source_text: str) -> List[Tuple[int, int, ConfigPredicate]]:
    """Find @Conditional and @Profile annotations and emit predicates
    gating the class or method they decorate.
    """
    results: List[Tuple[int, int, ConfigPredicate]] = []
    matches: List[Tuple[int, int, str]] = []
    for m in _JAVA_CONDITIONAL_RE.finditer(source_text):
        names = re.findall(r'([A-Za-z_][\w\.]*)\.class', m.group(0))
        text = ' AND '.join(f'CONFIG_JAVA_CONDITIONAL_{n.split(".")[-1].upper()}'
                            for n in names)
        matches.append((m.start(), m.end(), text))
    for m in _JAVA_PROFILE_RE.finditer(source_text):
        if m.group(1):
            profiles = [m.group(1)]
        else:
            profiles = [p.strip().strip('"') for p in m.group(2).split(',') if p.strip()]
        text = ' OR '.join(f'CONFIG_JAVA_PROFILE_{p.upper()}' for p in profiles)
        matches.append((m.start(), m.end(), text))
    matches.sort()
    for i, (start, end, text_form) in enumerate(matches):
        if not text_form:
            continue
        pred = ConfigPredicate(text_form=text_form)
        range_end = matches[i+1][0] if i+1 < len(matches) else len(source_text)
        results.append((end, range_end, pred))
    return results


# ---- ASM: reuse #ifdef tracking from c_scanner's regex, emit predicates ----

_PP_COND_RE = re.compile(
    r'^\s*#\s*(ifdef|ifndef|if|elif|else|endif)\b(.*)$',
    re.MULTILINE
)


def extract_asm_ifdef_predicates(source_text: str) -> List[Tuple[int, int, ConfigPredicate]]:
    """For ASM files: track #ifdef/#if/#ifndef directives and emit predicates
    covering their byte ranges. Same regex as c_scanner._PP_COND_RE.
    """
    results: List[Tuple[int, int, ConfigPredicate]] = []
    stack: List[Tuple[int, str, ConfigPredicate]] = []
    for m in _PP_COND_RE.finditer(source_text):
        directive = m.group(1)
        cond_text = m.group(2).strip()
        if directive in ('ifdef', 'ifndef', 'if'):
            text_form = _asm_cond_to_text(directive, cond_text)
            if not text_form:
                continue
            pred = ConfigPredicate(text_form=text_form)
            stack.append((m.end(), directive, pred))
        elif directive == 'elif' and stack:
            start, _, prev_pred = stack.pop()
            text_form = _asm_cond_to_text('if', cond_text)
            if text_form:
                new_pred = ConfigPredicate(text_form=text_form)
                results.append((start, m.start(), prev_pred))
                stack.append((m.end(), 'if', new_pred))
        elif directive == 'else' and stack:
            start, dir_prev, prev_pred = stack.pop()
            results.append((start, m.start(), prev_pred))
            not_text = f'NOT ({prev_pred.text_form})'
            not_pred = ConfigPredicate(text_form=not_text)
            stack.append((m.end(), 'else', not_pred))
        elif directive == 'endif' and stack:
            start, _, pred = stack.pop()
            results.append((start, m.start(), pred))
    # Close any unclosed #ifdef ranges at EOF
    for start, _, pred in stack:
        results.append((start, len(source_text), pred))
    return results


def _asm_cond_to_text(directive: str, cond_text: str) -> str:
    """Convert an ASM/C #ifdef/#if condition to a config-predicate text_form.

    For #ifdef FOO      → CONFIG_FOO
    For #ifndef FOO     → NOT CONFIG_FOO
    For #if defined(FOO) → CONFIG_FOO
    For #if FOO         → CONFIG_FOO
    For #if FOO == 1    → CONFIG_FOO
    For #if defined(FOO) && defined(BAR) → CONFIG_FOO AND CONFIG_BAR
    """
    cond_text = cond_text.strip()
    if directive == 'ifdef':
        ident = re.match(r'^([A-Za-z_]\w*)', cond_text)
        if ident:
            name = ident.group(1)
            if name.startswith('CONFIG_'):
                return name
            return f'CONFIG_{name}'
    elif directive == 'ifndef':
        ident = re.match(r'^([A-Za-z_]\w*)', cond_text)
        if ident:
            name = ident.group(1)
            if name.startswith('CONFIG_'):
                return f'NOT {name}'
            return f'NOT CONFIG_{name}'
    elif directive == 'if':
        # Replace defined(FOO) → CONFIG_FOO (skip if already CONFIG_*)
        def _defined_repl(m):
            name = m.group(1)
            if name.startswith('CONFIG_'):
                return name
            return f'CONFIG_{name}'
        text = re.sub(r'defined\s*\(\s*([A-Za-z_]\w*)\s*\)',
                       _defined_repl, cond_text)
        # Replace bare identifiers → CONFIG_<ident> (heuristic), but skip
        # identifiers that are already CONFIG_* or are reserved words.
        def _ident_repl(m):
            ident = m.group(1)
            if ident in ('CONFIG', 'NOT', 'AND', 'OR', 'defined'):
                return ident
            if ident.startswith('CONFIG_'):
                return ident
            return f'CONFIG_{ident}'
        text = re.sub(r'\b([A-Za-z_]\w*)\b(?!\s*=|\s*\)|\s*&&|\s*\|\|)',
                       _ident_repl, text)
        text = text.replace('&&', 'AND').replace('||', 'OR')
        text = re.sub(r'\s*==\s*1\b', '', text)
        text = re.sub(r'\s*!=\s*0\b', '', text)
        return text.strip()
    return ''


# ---- Common helpers ----

def annotate_nodes_with_predicates(
        nodes: List[Dict[str, Any]],
        ranges: List[Tuple[int, int, ConfigPredicate]],
        byte_start_key: str = 'byte_start',
        byte_end_key: str = 'byte_end',
) -> Tuple[List[Dict[str, Any]], List[ConfigPredicate]]:
    """Given a list of node dicts (with byte_start/byte_end keys) and a list
    of (start, end, pred) ranges, annotate each node with the predicate of
    the innermost containing range. Returns (nodes_with_pred_id, unique_preds).

    Falls back to UNCONDITIONAL.id for nodes not in any range.

    Uses sorted ranges + bisect for O(N log R) instead of O(N × R).
    For practical inputs (#ifdef, #[cfg], @Profile, //go:build), ranges
    are either strictly nested or non-overlapping, so the first
    containing range found via backward scan from the bisect position
    is the innermost.
    """
    seen: Dict[Any, ConfigPredicate] = {UNCONDITIONAL.id: UNCONDITIONAL}
    if not ranges:
        for n in nodes:
            n['config_predicate_id'] = UNCONDITIONAL.id
        return nodes, list(seen.values())

    # Sort ranges by (start ASC, end ASC). For nested ranges, outer ranges
    # come first; for non-overlapping ranges, earlier positions come first.
    sorted_ranges = sorted(ranges, key=lambda r: (r[0], r[1]))
    range_starts = [r[0] for r in sorted_ranges]

    for n in nodes:
        bs = n.get(byte_start_key, 0)
        be = n.get(byte_end_key, bs)
        # bisect_right finds the insertion point after all ranges with start <= bs.
        # We scan backward from there, checking containment (start <= bs and be <= end).
        # For nested/non-overlapping ranges (the practical case), the first
        # containing range is the innermost, so we can break early.
        best_pred: Optional[ConfigPredicate] = None
        best_len = -1
        idx = bisect.bisect_right(range_starts, bs)
        while idx > 0:
            idx -= 1
            rs, re_, pred = sorted_ranges[idx]
            if rs <= bs and be <= re_:
                rlen = re_ - rs
                if rlen < best_len or best_len < 0:
                    best_pred = pred
                    best_len = rlen
                    # For nested/non-overlapping ranges, first match is innermost.
                    break
            # If this range's start is before bs but it doesn't contain the
            # node, earlier ranges (with smaller start) are even more outer,
            # so they might still contain it. Continue backward scan.
        if best_pred is None:
            n['config_predicate_id'] = UNCONDITIONAL.id
        else:
            n['config_predicate_id'] = best_pred.id
            if best_pred.id not in seen:
                seen[best_pred.id] = best_pred
    return nodes, list(seen.values())


def serialize_predicates(preds: List[ConfigPredicate]) -> List[Dict[str, Any]]:
    """Convert ConfigPredicate objects to JSON-safe dicts (same shape as
    clang_scanner's serialization)."""
    return [
        {
            'id': p.id,
            'text_form': p.text_form,
            'z3_form': p.z3_form,
            'bdd_serialized': p.bdd_serialized,
            'config_macros': list(p.config_macros),
            'is_unconditional': bool(p.is_unconditional),
            'is_contradictory': bool(p.is_contradictory),
        }
        for p in preds
    ]
