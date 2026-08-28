"""AST pattern matching engine — structural code search with metavariables.

Inspired by semgrep and ast-grep: write patterns AS code, match AST nodes.
Uses tree-sitter AST (already extracted by C2D) + metavariable binding.

Pattern syntax:
  $NAME    — bind any single AST node
  ...      — match any number of sibling nodes
  $$BODY   — bind any subtree (deeper match)

Example:
  pattern: "$X == $X"         — find all self-comparisons
  pattern: "mutex_lock($L); ... mutex_unlock($L);" — find lock pairs
  pattern: "free($P); ... *$P" — find use-after-free patterns
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Metavariable pattern: $NAME (alphanumeric, starts with letter)
_METAVAR_RE = re.compile(r'\$([A-Za-z_]\w*)')

# Ellipsis: standalone "..."
_ELLIPSIS = '...'

# Deep metavariable: $$NAME (matches any subtree depth)
_DEEP_METAVAR_RE = re.compile(r'\$\$([A-Za-z_]\w*)')


def _is_metavar(token: str) -> bool:
    return bool(_METAVAR_RE.match(token.strip()))

def _is_deep_metavar(token: str) -> bool:
    return bool(_DEEP_METAVAR_RE.match(token.strip()))

def _is_ellipsis(token: str) -> bool:
    return token.strip() == _ELLIPSIS


def _tokenize_pattern(pattern: str) -> List[str]:
    """Split a pattern string into tokens (identifiers, operators, metavars).

    Tokenization must match _tokenize_source's so that pattern tokens align
    with source tokens for _match_tokens. Multi-char operators (==, !=, ->,
    &&, ||, etc.) are recognized as single tokens here — without this, a
    pattern like '$X == $X' tokenizes to ['$', '=', '=', '$'] and never
    matches source 'a == a' (which tokenizes to ['a', '==', 'a']).
    """
    # Multi-char operators (must match the list in _tokenize_source).
    _MULTICHAR_OPS = ['==', '!=', '<=', '>=', '&&', '||', '->', '++', '--',
                      '+=', '-=', '*=', '/=']
    tokens = []
    pos = 0
    while pos < len(pattern):
        # Skip whitespace
        if pattern[pos].isspace():
            pos += 1
            continue
        # Check for deep metavar $$NAME
        m = _DEEP_METAVAR_RE.match(pattern, pos)
        if m:
            tokens.append(m.group(0))
            pos = m.end()
            continue
        # Check for regular metavar $NAME
        m = _METAVAR_RE.match(pattern, pos)
        if m:
            tokens.append(m.group(0))
            pos = m.end()
            continue
        # Check for ellipsis
        if pattern[pos:pos+3] == _ELLIPSIS:
            tokens.append(_ELLIPSIS)
            pos += 3
            continue
        # Check for multi-char operators BEFORE single-char fallback.
        # This is the fix: previously, '==' fell through to the single-char
        # case and was tokenized as two '=' tokens, breaking patterns with
        # any comparison / compound-assignment operator.
        matched_op = False
        for op in _MULTICHAR_OPS:
            if pattern[pos:pos+len(op)] == op:
                tokens.append(op)
                pos += len(op)
                matched_op = True
                break
        if matched_op:
            continue
        # Check for identifier
        m = re.match(r'[A-Za-z_]\w*', pattern[pos:])
        if m:
            tokens.append(m.group(0))
            pos += m.end()
            continue
        # Single-char token (operators, punctuation)
        tokens.append(pattern[pos])
        pos += 1
    return tokens


def _match_tokens(pattern_tokens: List[str], source_tokens: List[str],
                  bindings: Optional[Dict[str, str]] = None) -> Optional[Dict[str, str]]:
    """Match pattern tokens against source tokens with metavariable binding.

    Returns the bindings dict if match succeeds, None if no match.
    Supports $NAME (bind any single token) and ... (skip any tokens).
    """
    if bindings is None:
        bindings = {}
    else:
        bindings = dict(bindings)  # copy

    pi = 0  # pattern index
    si = 0   # source index

    while pi < len(pattern_tokens) and si < len(source_tokens):
        pt = pattern_tokens[pi]

        if _is_ellipsis(pt):
            # Skip any number of source tokens until next pattern token matches
            if pi + 1 >= len(pattern_tokens):
                # Ellipsis at end — matches everything remaining
                return bindings
            next_pt = pattern_tokens[pi + 1]
            found = False
            while si < len(source_tokens):
                if _match_single(next_pt, source_tokens[si], bindings):
                    found = True
                    break
                si += 1
            if not found:
                return None
            pi += 1  # move past ellipsis, stay at current source token
            continue

        if _is_metavar(pt) or _is_deep_metavar(pt):
            var_name = pt.lstrip('$')
            source_val = source_tokens[si]
            if var_name in bindings:
                # Metavariable already bound — must match previous binding
                if bindings[var_name] != source_val:
                    return None
            else:
                bindings[var_name] = source_val
            pi += 1
            si += 1
            continue

        # Literal token — must match exactly (case-insensitive for keywords)
        if not _match_single(pt, source_tokens[si], bindings):
            return None
        pi += 1
        si += 1

    # Pattern exhausted — check if all remaining pattern tokens are ellipsis
    while pi < len(pattern_tokens):
        if not _is_ellipsis(pattern_tokens[pi]):
            return None  # non-ellipsis token left unmatched
        pi += 1

    return bindings


def _match_single(pattern_token: str, source_token: str,
                  bindings: Dict[str, str]) -> bool:
    """Match a single non-metavariable pattern token against source."""
    return pattern_token.lower() == source_token.lower()


def _tokenize_source(text: str) -> List[str]:
    """Tokenize source code text into tokens for matching."""
    # Similar to pattern tokenization but for actual source code
    tokens = []
    pos = 0
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        # String literals
        if text[pos] in '"\'':
            end = text.find(text[pos], pos + 1)
            if end == -1:
                tokens.append(text[pos:])
                break
            tokens.append(text[pos:end+1])
            pos = end + 1
            continue
        # Identifiers
        m = re.match(r'[A-Za-z_]\w*', text[pos:])
        if m:
            tokens.append(m.group(0))
            pos += m.end()
            continue
        # Numbers
        m = re.match(r'\d+\.?\d*', text[pos:])
        if m:
            tokens.append(m.group(0))
            pos += m.end()
            continue
        # Multi-char operators
        for op in ['==', '!=', '<=', '>=', '&&', '||', '->', '++', '--', '+=', '-=', '*=', '/=']:
            if text[pos:pos+len(op)] == op:
                tokens.append(op)
                pos += len(op)
                break
        else:
            tokens.append(text[pos])
            pos += 1
    return tokens


def search_pattern(graph_dir: str, pattern: str, limit: int = 50) -> List[Dict]:
    """Search all function bodies for a pattern match.

    The pattern is matched at every starting position within each
    function's body (sliding-window match), not just at the function's
    first token. This means a pattern like ``$X == $X`` will find
    self-comparisons anywhere in the body, not only when the body
    starts with the comparison.

    Args:
        graph_dir: C2D graph directory.
        pattern: Code pattern with $metavars and ... ellipsis.
        limit: Max results.

    Returns:
        List of {function, file, line, bindings} matches. If a single
        function matches multiple times, only the first match is kept
        (to bound output size and avoid one noisy function flooding
        results).
    """
    from _builder.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)
    pattern_tokens = _tokenize_pattern(pattern)
    results = []

    for nid, nd in G.nodes(data=True):
        if nd.get("is_empty", False) or nd.get("node_type") == "file":
            continue
        body = nd.get("body_text", "") or nd.get("body_text_compressed", b"")
        if isinstance(body, bytes):
            import zlib
            try:
                body = zlib.decompress(body).decode("utf-8", errors="replace")
            except Exception:
                body = ""
        if not body:
            continue
        source_tokens = _tokenize_source(body)
        # Sliding-window match: try matching the pattern at every
        # starting position. Empty pattern matches once (at position 0).
        if not pattern_tokens:
            bindings = {}
        else:
            bindings = None
            for start in range(len(source_tokens) + 1):
                # _match_tokens matches pattern_tokens against
                # source_tokens[start:]; pass a slice to anchor the
                # matching engine at the desired offset.
                candidate = _match_tokens(
                    pattern_tokens, source_tokens[start:])
                if candidate is not None:
                    bindings = candidate
                    break
        if bindings is not None:
            results.append({
                "function": nd.get("name", nid),
                "node_id": nid,
                "source_file": nd.get("source_file", ""),
                "line": nd.get("line", 0),
                "bindings": bindings,
            })
            if len(results) >= limit:
                break
    return results


def cmd_ast_search(args):
    """CLI handler: ast-search."""
    results = search_pattern(
        args.graph, args.pattern,
        limit=getattr(args, "limit", 50),
    )
    print(json.dumps({"pattern": args.pattern, "matches": len(results),
                       "results": results}, ensure_ascii=False, indent=2, default=str))
