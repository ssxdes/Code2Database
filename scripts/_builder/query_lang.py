#!/usr/bin/env python3
"""Unified graph query language for Code2Database.

Provides a Cypher-subset query language so users/LLMs can express queries
declaratively instead of memorizing 25+ CLI commands. Existing commands
remain as "prepared statements" — this is a unified layer on top.

Supported syntax (Cypher-subset):
    MATCH (n:Function)
    WHERE n.name = 'foo'
    RETURN n.id, n.name, n.domain

    MATCH (a:Function)-[:INVOKES]->(b:Function)
    WHERE b.name = 'bar'
    RETURN a.name, a.source_file

    MATCH (a)-[:INVOKES*1..3]->(b)
    WHERE a.name = 'entry'
    RETURN a.name, b.name

    MATCH (n:Function)
    WHERE n.domain = 'kernel' AND 'thread_processor' IN n.labels
    RETURN n.name

Design goals:
- Tiny parser (hand-written, no external deps) — supports the most common
  query patterns engineers/LLMs use.
- Routes to SQLite SQL when available, falls back to NetworkX traversal.
- Output is always JSON (with optional Markdown rendering via --format md).
- Never breaks existing CLI — query command is additive.

Not supported (kept out of scope intentionally):
- WITH clauses (chained queries) — too complex for first iteration
- Pattern comprehensions — too complex
- CREATE/DELETE/MERGE — read-only language
- Optional matches (OPTIONAL MATCH) — add later if needed
"""

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple
import logging


# ---------------------------------------------------------------------------
# AST data classes
# ---------------------------------------------------------------------------

@dataclass
class NodePattern:
    variable: str
    label: Optional[str] = None
    properties: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RelPattern:
    variable: str = ""
    rel_type: str = ""
    direction: str = "->"  # "->", "<-", or "-" (undirected)
    min_hops: int = 1
    max_hops: int = 1


@dataclass
class PathPattern:
    nodes: List[NodePattern] = field(default_factory=list)
    rels: List[RelPattern] = field(default_factory=list)


@dataclass
class WhereClause:
    """A simple WHERE expression tree."""
    op: str  # 'AND', 'OR', 'NOT', '=', '!=', '<', '>', '<=', '>=', 'LIKE', 'IN', 'TRUE'
    left: Any = None  # str (attr path), WhereClause, or literal
    right: Any = None


@dataclass
class ReturnItem:
    expr: str  # e.g., "n.name" or "n.labels" or "count(*)" or "sum(n.line)"
    alias: Optional[str] = None
    is_aggregate: bool = False
    aggregate_func: Optional[str] = None  # 'count', 'sum', 'avg', 'min', 'max'
    aggregate_arg: Optional[str] = None   # the inner expression


@dataclass
class Query:
    match: List[PathPattern] = field(default_factory=list)
    where: Optional[WhereClause] = None
    return_items: List[ReturnItem] = field(default_factory=list)
    limit: Optional[int] = None
    order_by: Optional[str] = None
    order_desc: bool = False
    group_by: List[str] = field(default_factory=list)
    having: Optional[Any] = None


# Identifier validation — prevents SQL injection via Cypher property
# keys. Cypher allows string-quoted keys (`{"name OR 1=1": 'x'}`),
# and without validation the key is interpolated directly into the
# generated SQL via f-strings (`f"n0.{k} = ?"`), producing
# `n0.name OR 1=1 = ?`. We require keys to match a strict C-like
# identifier pattern; anything else falls back to the networkx path
# (where keys are used as dict lookups, not SQL).
import re as _re_module
_IDENT_RE = _re_module.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _is_safe_ident(name: str) -> bool:
    """Return True if *name* is a safe SQL identifier (no injection risk)."""
    return bool(_IDENT_RE.match(name))


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"""
    \s*(?:
        (?P<STRING>'[^']*'|"[^"]*") |
        (?P<NUMBER>-?\d+(?:\.\d+)?) |
        (?P<OP>->|<-|-|=~|=|!=|<=|>=|<|>|\*|\.\.) |
        (?P<PUNCT>[()\[\]{}:,.;]) |
        (?P<WORD>[A-Za-z_][A-Za-z0-9_]*)
    )
""", re.VERBOSE)


def _tokenize(s: str) -> List[Tuple[str, str]]:
    tokens = []
    pos = 0
    while pos < len(s):
        m = _TOKEN_RE.match(s, pos)
        if not m:
            if s[pos].isspace():
                pos += 1
                continue
            raise SyntaxError(f"Cannot tokenize at: {s[pos:pos+20]!r}")
        pos = m.end()
        for kind in ("STRING", "NUMBER", "OP", "PUNCT", "WORD"):
            val = m.group(kind)
            if val is not None:
                if kind == "STRING":
                    val = val[1:-1]  # strip quotes
                tokens.append((kind, val))
                break
        else:
            # whitespace only
            pass
    return tokens


# ---------------------------------------------------------------------------
# Parser (recursive descent)
# ---------------------------------------------------------------------------

class _Parser:
    # Nested-parenthesis cap: 'WHERE ((((...))))' recurses through
    # _parse_where -> _parse_atom per level; a few thousand levels blows
    # the Python stack with a bare RecursionError (the CLI only catches
    # SyntaxError). 100 is far beyond any sane hand-written query.
    MAX_PAREN_DEPTH = 100

    def __init__(self, tokens: List[Tuple[str, str]]):
        self.tokens = tokens
        self.pos = 0
        self._paren_depth = 0

    def _peek(self) -> Optional[Tuple[str, str]]:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def _next(self) -> Tuple[str, str]:
        tok = self._peek()
        if tok is None:
            raise SyntaxError("Unexpected end of input")
        self.pos += 1
        return tok

    def _expect(self, kind: str, value: Optional[str] = None) -> Tuple[str, str]:
        tok = self._next()
        if tok[0] != kind or (value is not None and tok[1].upper() != value.upper()):
            raise SyntaxError(f"Expected {kind} {value!r}, got {tok}")
        return tok

    def _accept(self, kind: str, value: Optional[str] = None) -> Optional[Tuple[str, str]]:
        tok = self._peek()
        if tok and tok[0] == kind and (value is None or tok[1].upper() == value.upper()):
            self.pos += 1
            return tok
        return None

    def parse(self) -> Query:
        q = Query()
        self._parse_match(q)
        if self._accept("WORD", "WHERE"):
            q.where = self._parse_where()
        self._expect("WORD", "RETURN")
        q.return_items = self._parse_return()
        # Optional GROUP BY (D25)
        if self._accept("WORD", "GROUP"):
            self._expect("WORD", "BY")
            while True:
                tok = self._next()
                attr = tok[1]
                while self._accept("PUNCT", "."):
                    attr += "." + self._next()[1]
                q.group_by.append(attr)
                if not self._accept("PUNCT", ","):
                    break
        # Optional HAVING (D25) — only meaningful with GROUP BY
        if self._accept("WORD", "HAVING"):
            q.having = self._parse_where()
        # Optional ORDER BY (with optional DESC/ASC)
        if self._accept("WORD", "ORDER"):
            self._expect("WORD", "BY")
            tok = self._next()
            order_attr = tok[1]
            while self._accept("PUNCT", "."):
                order_attr += "." + self._next()[1]
            q.order_by = order_attr
            # Optional DESC / ASC
            tok = self._peek()
            if tok and tok[0] == "WORD" and tok[1].upper() in ("DESC", "ASC"):
                self._next()
                q.order_desc = tok[1].upper() == "DESC"
        # Optional LIMIT
        if self._accept("WORD", "LIMIT"):
            tok = self._next()
            try:
                q.limit = int(tok[1])
            except (ValueError, TypeError):
                raise SyntaxError(
                    f"LIMIT must be an integer, got {tok[1]!r}")
            # SQLite semantics: a negative LIMIT means no limit.
            if q.limit < 0:
                q.limit = None
        return q

    def _parse_match(self, q: Query):
        self._expect("WORD", "MATCH")
        # Accept multiple path patterns separated by commas
        while True:
            path = self._parse_path()
            q.match.append(path)
            if not self._accept("PUNCT", ","):
                break

    def _parse_path(self) -> PathPattern:
        path = PathPattern()
        path.nodes.append(self._parse_node())
        while True:
            # Look for relationship: -[...]-> or <-[...]- or -
            tok = self._peek()
            if not tok or tok[0] != "OP" or tok[1] not in ("-", "<-"):
                break
            rel = self._parse_rel()
            path.rels.append(rel)
            path.nodes.append(self._parse_node())
        return path

    def _parse_node(self) -> NodePattern:
        self._expect("PUNCT", "(")
        variable = ""
        label = None
        props = {}
        tok = self._peek()
        if tok and tok[0] == "WORD":
            variable = self._next()[1]
        if self._accept("PUNCT", ":"):
            label = self._next()[1]
        if self._accept("PUNCT", "{"):
            props = self._parse_props()
            self._expect("PUNCT", "}")
        self._expect("PUNCT", ")")
        return NodePattern(variable=variable, label=label, properties=props)

    def _parse_props(self) -> Dict[str, Any]:
        props = {}
        while True:
            key = self._next()[1]
            self._expect("OP", "=")
            val = self._parse_literal()
            props[key] = val
            if not self._accept("PUNCT", ","):
                break
        return props

    def _parse_literal(self) -> Any:
        tok = self._next()
        if tok[0] == "STRING":
            return tok[1]
        if tok[0] == "NUMBER":
            return float(tok[1]) if "." in tok[1] else int(tok[1])
        if tok[0] == "WORD" and tok[1].upper() in ("TRUE", "FALSE"):
            return tok[1].upper() == "TRUE"
        raise SyntaxError(f"Expected literal, got {tok}")

    def _parse_rel(self) -> RelPattern:
        # Already peeked: OP "-" or "<-"
        direction = "->"
        if self._accept("OP", "<-"):
            direction = "<-"
        else:
            self._expect("OP", "-")
        rel = RelPattern(direction=direction)
        if self._accept("PUNCT", "["):
            tok = self._peek()
            if tok and tok[0] == "WORD":
                rel.variable = self._next()[1]
            if self._accept("PUNCT", ":"):
                rel.rel_type = self._next()[1]
            # *N..M for variable-length
            if self._accept("OP", "*"):
                tok = self._peek()
                if tok and tok[0] == "NUMBER":
                    rel.min_hops = int(self._next()[1])
                    if self._accept("OP", ".."):
                        tok2 = self._peek()
                        if tok2 and tok2[0] == "NUMBER":
                            rel.max_hops = int(self._next()[1])
                        else:
                            rel.max_hops = 0  # unbounded
                    else:
                        rel.max_hops = rel.min_hops
            self._expect("PUNCT", "]")
        # Closing direction
        if direction == "->":
            self._expect("OP", "->")
        elif direction == "<-":
            pass  # already consumed <- at start
        else:
            # could end with - (undirected) or ->
            self._accept("OP", "->")
        return rel

    def _parse_where(self) -> WhereClause:
        return self._parse_or()

    def _parse_or(self) -> WhereClause:
        left = self._parse_and()
        while self._accept("WORD", "OR"):
            right = self._parse_and()
            left = WhereClause(op="OR", left=left, right=right)
        return left

    def _parse_and(self) -> WhereClause:
        left = self._parse_not()
        while self._accept("WORD", "AND"):
            right = self._parse_not()
            left = WhereClause(op="AND", left=left, right=right)
        return left

    def _parse_not(self) -> WhereClause:
        if self._accept("WORD", "NOT"):
            inner = self._parse_comparison()
            return WhereClause(op="NOT", left=inner)
        return self._parse_comparison()

    def _parse_comparison(self) -> WhereClause:
        # CONFIG(var, 'predicate_text') — cgdb config predicate filter
        tok = self._peek()
        if tok and tok[0] == "WORD" and tok[1].upper() == "CONFIG":
            self._next()
            self._expect("PUNCT", "(")
            node_atom = self._parse_atom()
            self._expect("PUNCT", ",")
            pred_tok = self._next()
            if pred_tok[0] != "STRING":
                raise SyntaxError(
                    f"Expected string literal for CONFIG predicate, got {pred_tok}"
                )
            self._expect("PUNCT", ")")
            return WhereClause(op="CONFIG", left=node_atom,
                               right=("lit", pred_tok[1]))
        left = self._parse_atom()
        tok = self._peek()
        if tok and tok[0] == "OP" and tok[1] in ("=", "!=", "<", ">", "<=", ">=", "=~"):
            op = self._next()[1]
            right = self._parse_atom()
            return WhereClause(op=op, left=left, right=right)
        if tok and tok[0] == "WORD" and tok[1].upper() == "IN":
            self._next()
            # Support both `IN [literal, ...]` and `IN attr` (e.g., n.labels)
            if self._accept("PUNCT", "["):
                items = []
                while True:
                    items.append(self._parse_literal())
                    if not self._accept("PUNCT", ","):
                        break
                self._expect("PUNCT", "]")
                return WhereClause(op="IN", left=left, right=items)
            else:
                right = self._parse_atom()
                return WhereClause(op="IN_ATTR", left=left, right=right)
        if tok and tok[0] == "WORD" and tok[1].upper() == "LIKE":
            self._next()
            right = self._parse_literal()
            return WhereClause(op="LIKE", left=left, right=right)
        # bare atom as boolean (e.g., n.is_empty)
        return WhereClause(op="TRUE", left=left)

    def _parse_atom(self) -> Any:
        tok = self._peek()
        if tok and tok[0] == "PUNCT" and tok[1] == "(":
            self._paren_depth += 1
            if self._paren_depth > self.MAX_PAREN_DEPTH:
                raise SyntaxError(
                    f"WHERE expression nested more than "
                    f"{self.MAX_PAREN_DEPTH} parentheses deep")
            try:
                self._next()
                inner = self._parse_where()
                self._expect("PUNCT", ")")
            finally:
                self._paren_depth -= 1
            return inner
        if tok and tok[0] == "STRING":
            return ("lit", self._next()[1])
        if tok and tok[0] == "NUMBER":
            self._next()
            return ("lit", float(tok[1]) if "." in tok[1] else int(tok[1]))
        if tok and tok[0] == "WORD" and tok[1].upper() in ("TRUE", "FALSE"):
            self._next()
            return ("lit", tok[1].upper() == "TRUE")
        if tok and tok[0] == "WORD":
            # attribute path: var.attr or var.attr.attr2
            path = self._next()[1]
            while self._accept("PUNCT", "."):
                path += "." + self._next()[1]
            return ("attr", path)
        raise SyntaxError(f"Unexpected token in expression: {tok}")

    def _parse_return(self) -> List[ReturnItem]:
        items = []
        while True:
            tok = self._peek()
            if not tok:
                break
            if tok[0] == "OP" and tok[1] == "*":
                self._next()
                items.append(ReturnItem(expr="*", alias=None))
                if not self._accept("PUNCT", ","):
                    break
                continue

            # Check for aggregate function: COUNT(...), SUM(...), AVG(...), MIN(...), MAX(...)
            if tok[0] == "WORD" and tok[1].upper() in (
                    "COUNT", "SUM", "AVG", "MIN", "MAX"):
                func_name = tok[1].upper()
                self._next()
                self._expect("PUNCT", "(")
                # Argument can be * or an attribute path
                arg_tok = self._peek()
                if arg_tok and arg_tok[0] == "OP" and arg_tok[1] == "*":
                    self._next()
                    arg = "*"
                else:
                    arg = self._next()[1]
                    while self._accept("PUNCT", "."):
                        arg += "." + self._next()[1]
                self._expect("PUNCT", ")")
                alias = None
                if self._accept("WORD", "AS"):
                    alias = self._next()[1]
                expr = f"{func_name}({arg})"
                items.append(ReturnItem(
                    expr=expr, alias=alias or expr.lower(),
                    is_aggregate=True,
                    aggregate_func=func_name,
                    aggregate_arg=arg,
                ))
            else:
                expr = self._next()[1]
                while self._accept("PUNCT", "."):
                    expr += "." + self._next()[1]
                alias = None
                if self._accept("WORD", "AS"):
                    alias = self._next()[1]
                items.append(ReturnItem(expr=expr, alias=alias))
            if not self._accept("PUNCT", ","):
                break
        return items


def parse_query(query_str: str) -> Query:
    """Parse a Cypher-subset query string into a Query AST."""
    tokens = _tokenize(query_str)
    if not tokens:
        raise SyntaxError("Empty query")
    return _Parser(tokens).parse()


# ---------------------------------------------------------------------------
# Evaluator: executes the parsed Query against the graph
# ---------------------------------------------------------------------------

def _eval_where(where: Optional[WhereClause], binding: Dict[str, Any]) -> bool:
    """Evaluate a WHERE clause against a variable binding."""
    if where is None:
        return True
    if where.op == "AND":
        return _eval_where(where.left, binding) and _eval_where(where.right, binding)
    if where.op == "OR":
        return _eval_where(where.left, binding) or _eval_where(where.right, binding)
    if where.op == "NOT":
        return not _eval_where(where.left, binding)
    if where.op == "TRUE":
        return bool(_eval_atom(where.left, binding))
    # comparison ops
    left_val = _eval_atom(where.left, binding)
    right_val = _eval_atom(where.right, binding) if where.right is not None else None
    if where.op == "=":
        return left_val == right_val
    if where.op == "!=":
        return left_val != right_val
    # Ordering comparisons on mismatched types (e.g. n.line < 'abc'
    # where line is an int) raise TypeError in Python; SQL/Cypher
    # semantics treat them as non-matching (NULL) — never a crash.
    try:
        if where.op == "<":
            return left_val < right_val
        if where.op == ">":
            return left_val > right_val
        if where.op == "<=":
            return left_val <= right_val
        if where.op == ">=":
            return left_val >= right_val
    except TypeError:
        return False
    if where.op == "LIKE":
        # Convert SQL LIKE pattern to regex
        if not isinstance(left_val, str) or not isinstance(right_val, str):
            return False
        pattern = re.escape(right_val).replace("%", ".*").replace("_", ".")
        return bool(re.fullmatch(pattern, left_val))
    if where.op == "IN":
        if not isinstance(where.right, list):
            return False
        return left_val in where.right
    if where.op == "IN_ATTR":
        # left_val IN right_attr — right_attr is a list/collection attribute
        right_val = _eval_atom(where.right, binding) if where.right is not None else None
        if not isinstance(right_val, (list, tuple, set)):
            return False
        return left_val in right_val
    if where.op == "CONFIG":
        # CONFIG(node_var, 'predicate_text') — true if node's config_predicate
        # matches the given predicate text. Looks up via _config_lookup_fn if
        # set (cgdb path), else falls back to node's config_predicate attribute.
        node_atom = where.left
        pred_text = _eval_atom(where.right, binding)
        if not isinstance(pred_text, str):
            return False
        node_obj = _eval_atom(node_atom, binding) if not isinstance(node_atom, tuple) else None
        if node_atom and isinstance(node_atom, tuple) and node_atom[0] == "attr":
            # Get the node dict from binding
            var = node_atom[1].split(".", 1)[0]
            node_obj = binding.get(var)
        if node_obj is None:
            return False
        # Fast path: if node has a config_predicate_text attribute, compare directly
        node_pred_text = None
        if isinstance(node_obj, dict):
            node_pred_text = (
                node_obj.get("config_predicate_text")
                or node_obj.get("config_predicate")
            )
            node_id = node_obj.get("id") or node_obj.get("cgdb_id")
        else:
            node_id = getattr(node_obj, "id", None) or getattr(node_obj, "cgdb_id", None)
            node_pred_text = (
                getattr(node_obj, "config_predicate_text", None)
                or getattr(node_obj, "config_predicate", None)
            )
        if node_pred_text is not None:
            # Direct attribute comparison
            if node_pred_text == pred_text:
                return True
            # Compound predicate match: 'CONFIG_X AND CONFIG_Y' contains 'CONFIG_X'.
            # But 'NOT CONFIG_X' must not match a search for 'CONFIG_X' — check
            # that the match isn't preceded by 'NOT '.
            idx = node_pred_text.find(pred_text)
            while idx >= 0:
                # Check the chars before the match — if they end with 'NOT '
                # (case-insensitive, word-boundary), skip this occurrence.
                prefix = node_pred_text[:idx].rstrip()
                if not prefix.upper().endswith('NOT'):
                    return True
                # Look for next occurrence
                idx = node_pred_text.find(pred_text, idx + 1)
        # Slow path: query cgdb config_predicates via the registered lookup fn
        if _config_lookup_fn is not None and node_id is not None:
            try:
                preds = _config_lookup_fn(node_id)
            except Exception:
                preds = None
            if preds:
                # preds is a list of {text_form, config_macros, ...}
                for p in preds:
                    if not isinstance(p, dict):
                        continue
                    pf = p.get("text_form") or ""
                    if pf == pred_text:
                        return True
                    # Compound match with NOT-prefix guard (same logic as
                    # the fast path above).
                    idx = pf.find(pred_text)
                    while idx >= 0:
                        prefix = pf[:idx].rstrip()
                        if not prefix.upper().endswith("NOT"):
                            return True
                        idx = pf.find(pred_text, idx + 1)
                return False
            return False
        # No lookup fn and no attribute — no match
        return False
    raise ValueError(f"Unknown WHERE op: {where.op}")


# Module-level config predicate lookup function (set by callers that have
# access to a SQLiteCGDBStore). Signature: (node_id) -> List[Dict] where each
# Dict has at least 'text_form' and 'config_macros' keys. None means no cgdb
# store is available; CONFIG() falls back to node attributes only.
_config_lookup_fn: Optional[Any] = None


def set_config_lookup_fn(fn: Optional[Any]) -> None:
    """Register a config predicate lookup function for CONFIG() filters.

    Called by cmd_query when a cgdb store is available. Pass None to clear.
    """
    global _config_lookup_fn
    _config_lookup_fn = fn


def _eval_atom(atom: Any, binding: Dict[str, Any]) -> Any:
    if isinstance(atom, tuple):
        kind, val = atom
        if kind == "lit":
            return val
        if kind == "attr":
            # var.attr.attr2...
            parts = val.split(".", 1)
            var = parts[0]
            rest = parts[1] if len(parts) > 1 else ""
            obj = binding.get(var)
            if obj is None:
                return None
            return _get_attr(obj, rest)
    if isinstance(atom, WhereClause):
        return _eval_where(atom, binding)
    return atom


def _get_attr(obj: Any, path: str) -> Any:
    if not path:
        return obj
    parts = path.split(".")
    cur = obj
    for p in parts:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            cur = getattr(cur, p, None)
        if cur is None:
            return None
    return cur


def _node_to_dict(node_id: str, node_data: Dict) -> Dict:
    """Flatten a NetworkX node to a dict for query evaluation."""
    out = dict(node_data)
    out["id"] = node_id
    return out


def _matches_label(node: Dict, label: Optional[str]) -> bool:
    if not label:
        return True
    # 'Function' is a special label meaning "any function node" — matches
    # all call-graph nodes regardless of their assigned labels (since the
    # 7 fixed labels are semantic roles, not the Cypher-style node-type).
    if label == "Function":
        return True
    labels = node.get("labels", []) or []
    if isinstance(labels, list):
        return label in labels
    return False


def _matches_props(node: Dict, props: Dict) -> bool:
    for k, v in props.items():
        if node.get(k) != v:
            return False
    return True


def execute_query(query: Query, G) -> List[Dict]:
    """Execute a parsed Query against a NetworkX DiGraph.

    Returns a list of result rows (dicts keyed by return-item alias/expr).
    """
    # For now, support single-path MATCH with optional WHERE and RETURN
    if not query.match:
        return []
    path = query.match[0]

    # If post-processing (aggregate / group by / order by) is needed,
    # we must collect ALL matching rows first, then apply LIMIT last.
    needs_post = (any(item.is_aggregate for item in query.return_items)
                  or bool(query.group_by)
                  or query.order_by is not None)
    saved_limit = query.limit
    if needs_post:
        query.limit = None  # don't apply LIMIT inside executors

    try:
        if len(path.nodes) == 1 and not path.rels:
            rows = _execute_node_match(query, G, path.nodes[0])
        elif len(path.nodes) == 2 and len(path.rels) == 1:
            rel0 = path.rels[0]
            is_varlen = (rel0.min_hops != 1 or rel0.max_hops != 1)
            if is_varlen:
                rows = _execute_varlen_match(query, G, path)
            else:
                rows = _execute_rel_match(query, G, path.nodes[0], rel0, path.nodes[1])
        elif len(path.nodes) >= 2 and len(path.rels) >= 1:
            # Variable-length path: (a)-[:INVOKES*1..3]->(b)
            rows = _execute_varlen_match(query, G, path)
        else:
            raise ValueError(f"Unsupported path shape: {len(path.nodes)} nodes, {len(path.rels)} rels")
    finally:
        query.limit = saved_limit

    # D25: Apply GROUP BY + aggregates if any aggregate is in RETURN
    has_aggregate = any(item.is_aggregate for item in query.return_items)
    if has_aggregate or query.group_by:
        rows = _apply_group_by_and_aggregates(query, rows)

    # Apply ORDER BY (with optional DESC)
    if query.order_by:
        rows = _apply_order_by(rows, query.order_by, query.order_desc)

    # Apply LIMIT (post-aggregation, post-order). limit=0 must yield
    # zero rows (not "all rows" as the old truthiness check did).
    if saved_limit is not None and len(rows) > saved_limit:
        rows = rows[:saved_limit]
    return rows


def _resolve_row_key(row: Dict, attr: str) -> Any:
    """Look up attr in row, trying multiple key forms.

    Row keys may be: alias (e.g. "d"), stripped attr (e.g. "domain"),
    or full path (e.g. "n.domain"). We try each form to find the value.
    """
    if attr in row:
        return row[attr]
    if "." in attr:
        stripped = attr.split(".", 1)[1]
        if stripped in row:
            return row[stripped]
        last = attr.split(".")[-1]
        if last in row:
            return row[last]
    return None


def _apply_group_by_and_aggregates(query: Query, rows: List[Dict]) -> List[Dict]:
    """Apply GROUP BY and aggregate functions to rows.

    If group_by is set, group rows by the listed attributes. For each group,
    compute aggregates (count/sum/avg/min/max) over the rows in the group.

    If aggregates are present without GROUP BY, treat all rows as one group.
    """
    if not query.group_by:
        # Single-group aggregation
        if not any(item.is_aggregate for item in query.return_items):
            return rows
        out_row = {}
        for item in query.return_items:
            if item.is_aggregate:
                out_row[item.alias or item.expr] = _compute_aggregate(
                    item.aggregate_func, item.aggregate_arg, rows)
            else:
                # Non-aggregate in aggregate query — take first row's value
                if rows:
                    out_row[item.alias or item.expr] = _resolve_row_key(rows[0], item.expr)
                else:
                    out_row[item.alias or item.expr] = None
        return [out_row]

    # Group by the listed attributes
    groups: Dict[tuple, List[Dict]] = {}
    for row in rows:
        key = tuple(_resolve_row_key(row, attr) for attr in query.group_by)
        groups.setdefault(key, []).append(row)

    out_rows = []
    for key, group_rows in groups.items():
        out_row = {}
        for item in query.return_items:
            if item.is_aggregate:
                out_row[item.alias or item.expr] = _compute_aggregate(
                    item.aggregate_func, item.aggregate_arg, group_rows)
            else:
                # Non-aggregate must be in GROUP BY (Cypher requirement)
                out_row[item.alias or item.expr] = _resolve_row_key(group_rows[0], item.expr)
        out_rows.append(out_row)

    # Apply HAVING filter on grouped rows
    if query.having:
        out_rows = [r for r in out_rows if _eval_where(query.having, r)]
    return out_rows


def _compute_aggregate(func: Optional[str], arg: Optional[str],
                       rows: List[Dict]) -> float:
    """Compute an aggregate value over a list of rows."""
    if func == "COUNT":
        if arg == "*" or arg is None:
            return len(rows)
        # Count non-null values of attr
        return sum(1 for r in rows if _resolve_row_key(r, arg) is not None)
    if not arg or arg == "*":
        return 0
    raw_values = [_resolve_row_key(r, arg) for r in rows]
    numeric = []
    for v in raw_values:
        if v is None:
            continue
        if isinstance(v, (int, float)):
            numeric.append(float(v))
        elif isinstance(v, str):
            try:
                numeric.append(float(v))
            except ValueError:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
    if func == "SUM":
        return sum(numeric) if numeric else 0
    if func == "AVG":
        return (sum(numeric) / len(numeric)) if numeric else 0
    if func == "MIN":
        return min(numeric) if numeric else 0
    if func == "MAX":
        return max(numeric) if numeric else 0
    return 0


def _apply_order_by(rows: List[Dict], order_by: str, desc: bool) -> List[Dict]:
    """Sort rows by the given attribute, optionally descending."""
    try:
        return sorted(rows,
                      key=lambda r: (_resolve_row_key(r, order_by) is None,
                                     _resolve_row_key(r, order_by)),
                      reverse=desc)
    except TypeError:
        # Mixed types — sort by string representation
        return sorted(rows,
                      key=lambda r: str(_resolve_row_key(r, order_by)),
                      reverse=desc)


def _execute_node_match(query: Query, G, pattern: NodePattern) -> List[Dict]:
    rows = []
    for nid, nd in G.nodes(data=True):
        node_dict = _node_to_dict(nid, nd)
        if not _matches_label(node_dict, pattern.label):
            continue
        if not _matches_props(node_dict, pattern.properties):
            continue
        binding = {pattern.variable: node_dict} if pattern.variable else {}
        if not _eval_where(query.where, binding):
            continue
        row = _project_return(query.return_items, binding)
        rows.append(row)
        if query.limit is not None and len(rows) >= query.limit:
            break
    return rows


def _execute_rel_match(query: Query, G, left: NodePattern, rel: RelPattern,
                       right: NodePattern) -> List[Dict]:
    rows = []
    # Determine traversal direction
    if rel.direction == "->":
        src_pat, dst_pat = left, right
        edge_iter = G.out_edges
        swap = False
    elif rel.direction == "<-":
        src_pat, dst_pat = right, left
        edge_iter = G.in_edges
        swap = False
    else:
        # undirected — try both directions
        src_pat, dst_pat = left, right
        edge_iter = None
        swap = True

    seen_pairs = set()
    for nid, nd in G.nodes(data=True):
        src_dict = _node_to_dict(nid, nd)
        if not _matches_label(src_dict, src_pat.label):
            continue
        if not _matches_props(src_dict, src_pat.properties):
            continue
        # Get neighbors in correct direction
        if rel.direction == "->":
            neighbors = list(G.successors(nid))
        elif rel.direction == "<-":
            neighbors = list(G.predecessors(nid))
        else:
            neighbors = list(G.successors(nid)) + list(G.predecessors(nid))
        for nb_id in neighbors:
            nb_data = G.nodes[nb_id]
            nb_dict = _node_to_dict(nb_id, nb_data)
            if not _matches_label(nb_dict, dst_pat.label):
                continue
            if not _matches_props(nb_dict, dst_pat.properties):
                continue
            # Check edge relation type
            if rel.rel_type:
                ed = G.get_edge_data(nid, nb_id) or G.get_edge_data(nb_id, nid) or {}
                if ed.get("relation", "INVOKES") != rel.rel_type:
                    continue
            pair_key = (nid, nb_id) if not swap else (min(nid, nb_id), max(nid, nb_id))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            binding = {}
            if left.variable:
                binding[left.variable] = src_dict if rel.direction != "<-" else nb_dict
            if right.variable:
                binding[right.variable] = nb_dict if rel.direction != "<-" else src_dict
            if not _eval_where(query.where, binding):
                continue
            row = _project_return(query.return_items, binding)
            rows.append(row)
            if query.limit is not None and len(rows) >= query.limit:
                return rows
    return rows


def _execute_varlen_match(query: Query, G, path: PathPattern) -> List[Dict]:
    """Variable-length path: (a)-[:INVOKES*1..3]->(b)."""
    rows = []
    if not path.rels:
        return _execute_node_match(query, G, path.nodes[0])

    rel = path.rels[0]
    max_depth = rel.max_hops if rel.max_hops > 0 else 5  # cap at 5 for safety
    min_depth = rel.min_hops

    start_pat = path.nodes[0]
    end_pat = path.nodes[-1]

    for nid, nd in G.nodes(data=True):
        start_dict = _node_to_dict(nid, nd)
        if not _matches_label(start_dict, start_pat.label):
            continue
        if not _matches_props(start_dict, start_pat.properties):
            continue
        # BFS up to max_depth
        paths_found = _bfs_paths(G, nid, rel, max_depth, end_pat)
        for path_nodes, path_edges in paths_found:
            if len(path_nodes) - 1 < min_depth:
                continue
            end_dict = _node_to_dict(path_nodes[-1], G.nodes[path_nodes[-1]])
            binding = {}
            if start_pat.variable:
                binding[start_pat.variable] = start_dict
            if end_pat.variable:
                binding[end_pat.variable] = end_dict
            if not _eval_where(query.where, binding):
                continue
            row = _project_return(query.return_items, binding)
            rows.append(row)
            if query.limit is not None and len(rows) >= query.limit:
                return rows
    return rows


def _bfs_paths(G, start_id: str, rel: RelPattern, max_depth: int,
               end_pat: NodePattern):
    """BFS yielding (path_nodes, path_edges) up to max_depth.

    C2 (backport from cdb report 5.4.5): cycle protection via path-string
    membership check — a node already in the current path is not re-visited.
    This prevents infinite loops on cyclic graphs (A→B→A→B→...) without
    needing a separate global visited set.
    """
    from collections import deque
    queue = deque([(start_id, [start_id], [])])
    results = []
    while queue:
        cur, path_n, path_e = queue.popleft()
        if len(path_n) - 1 >= max_depth:
            continue
        # Get neighbors
        if rel.direction == "<-":
            neighbors = list(G.predecessors(cur))
        else:
            neighbors = list(G.successors(cur))
        for nb in neighbors:
            # Cycle protection: skip if nb is already in current path
            # (path-string check like cdb's `t.path NOT LIKE '%,id,%'`)
            if nb in path_n:
                continue
            ed = G.get_edge_data(cur, nb) or {}
            if rel.rel_type and ed.get("relation", "INVOKES") != rel.rel_type:
                # Try reverse direction too for undirected
                if rel.direction == "-":
                    ed_rev = G.get_edge_data(nb, cur) or {}
                    if ed_rev.get("relation", "INVOKES") != rel.rel_type:
                        continue
                    ed = ed_rev
                else:
                    continue
            new_path_n = path_n + [nb]
            new_path_e = path_e + [ed]
            nb_data = G.nodes[nb]
            nb_dict = _node_to_dict(nb, nb_data)
            if _matches_label(nb_dict, end_pat.label) and _matches_props(nb_dict, end_pat.properties):
                results.append((new_path_n, new_path_e))
            queue.append((nb, new_path_n, new_path_e))
    return results


def _project_return(items: List[ReturnItem], binding: Dict) -> Dict:
    row = {}
    for item in items:
        if item.is_aggregate:
            # Aggregates are computed later by _apply_group_by_and_aggregates;
            # skip projection here. Expose binding attrs that the aggregate
            # may need by their stripped/full key forms.
            if item.aggregate_arg and item.aggregate_arg != "*":
                val = _eval_atom(("attr", item.aggregate_arg), binding)
                key = item.alias or item.expr
                row.setdefault(key, None)  # placeholder, filled later
                if "." in item.aggregate_arg:
                    stripped = item.aggregate_arg.split(".", 1)[1]
                    row.setdefault(stripped, val)
                    row.setdefault(item.aggregate_arg, val)
            else:
                row.setdefault(item.alias or item.expr, None)
            continue
        if item.expr == "*":
            row.update(binding)
            continue
        val = _eval_atom(("attr", item.expr), binding)
        key = item.alias or item.expr
        row[key] = val
        # Also expose the stripped attr name and full path so that
        # downstream stages (GROUP BY / ORDER BY / aggregates) can find
        # the value regardless of which key form they look up by.
        if item.alias and "." in item.expr:
            stripped = item.expr.split(".", 1)[1]
            row.setdefault(stripped, val)
            row.setdefault(item.expr, val)
    return row


# ---------------------------------------------------------------------------
# SQLite recursive CTE compilation
# When cgdb store is available and the query is a varlen path pattern
# (a)-[:REL*min..max]->(b), compile to a recursive CTE for O(log N) traversal
# instead of networkx BFS in Python. Falls back to networkx otherwise.
# ---------------------------------------------------------------------------

def _hoist_where_to_sql(where: Optional[WhereClause],
                         start_var: Optional[str], end_var: Optional[str],
                         start_filters: List[str], end_filters: List[str],
                         start_params: List[Any], end_params: List[Any]
                         ) -> tuple:
    """Hoist simple `var.attr = literal` clauses from a WHERE tree into SQL
    filter lists. Returns (hoisted_count, residual_where).

    Only equality predicates on the start or end node variable are hoisted
    — they push down into the CTE's start_where / end_where so they apply
    BEFORE LIMIT, avoiding premature row truncation. Everything else is
    returned as a residual WhereClause to evaluate in Python after fetching.
    """
    if where is None:
        return 0, None
    # AND-tree: hoist from both sides, keep residual as AND of residuals.
    if where.op == "AND":
        left_hoisted, left_resid = _hoist_where_to_sql(
            where.left, start_var, end_var,
            start_filters, end_filters, start_params, end_params)
        right_hoisted, right_resid = _hoist_where_to_sql(
            where.right, start_var, end_var,
            start_filters, end_filters, start_params, end_params)
        total = left_hoisted + right_hoisted
        if left_resid is None:
            return total, right_resid
        if right_resid is None:
            return total, left_resid
        return total, WhereClause(op="AND", left=left_resid, right=right_resid)
    # Equality on var.attr = literal
    if where.op == "=" and isinstance(where.left, tuple) and where.left[0] == "attr":
        var_attr = where.left[1]
        parts = var_attr.split(".", 1)
        if len(parts) == 2:
            var, attr = parts[0], parts[1]
            # Only single-segment attr (e.g. n.name, n.kind) — deeper paths
            # can't map to a single column safely.
            # Validate the attr against a strict identifier pattern to
            # prevent SQL injection via crafted Cypher property keys.
            if "." not in attr and _is_safe_ident(attr) \
                    and isinstance(where.right, tuple) and where.right[0] == "lit":
                lit_val = where.right[1]
                if var == start_var:
                    start_filters.append(f"n0.{attr} = ?")
                    start_params.append(lit_val)
                    return 1, None
                if var == end_var:
                    end_filters.append(f"ndst.{attr} = ?")
                    end_params.append(lit_val)
                    return 1, None
    # Not hoist-able: keep as residual.
    return 0, where


def _try_cte_execution(query: Query, cgdb_store) -> Optional[List[Dict]]:
    """Attempt to compile query to a SQLite recursive CTE. Returns None if
    the query shape isn't supported by the CTE path (caller falls back to
    networkx); otherwise returns the result rows.

    Supported shape:
      MATCH (a:label {props}) -[:REL*min..max]-> (b:label {props})
      WHERE <filters on a/b>
      RETURN a, b   (or RETURN b, or RETURN a.name, b.name, ...)

    The CTE handles cycle protection via path-string membership check
    (same pattern as SQLiteCGDBStore.find_invokers).
    """
    if cgdb_store is None:
        return None
    if not query.match:
        return None
    path = query.match[0]
    if not path.rels:
        return None
    # Only single-rel varlen patterns supported by the CTE path
    if len(path.rels) != 1 or not path.rels[0].max_hops:
        return None
    rel = path.rels[0]
    if rel.min_hops < 1 or rel.max_hops > 20:
        return None  # safety cap
    start_pat = path.nodes[0]
    end_pat = path.nodes[-1] if len(path.nodes) > 1 else None
    if not start_pat or not end_pat:
        return None
    direction = rel.direction or "->"

    try:
        conn = cgdb_store._ensure_conn()
        edge_kind = rel.rel_type or "INVOKES"
        max_depth = rel.max_hops

        # Build start-node filter SQL
        start_filters: List[str] = []
        start_params: List[Any] = []
        if start_pat.label:
            start_filters.append("n0.kind = ?")
            start_params.append(start_pat.label)
        for k, v in (start_pat.properties or {}).items():
            # Validate k to prevent SQL injection via crafted property keys
            # (e.g., `{"name OR 1=1": 'x'}` would otherwise interpolate
            # into `n0.name OR 1=1 = ?`). Returning None forces the
            # networkx path, where property keys are dict lookups
            # (not SQL interpolation) and the property filter is
            # applied correctly.
            #
            # The previous `continue` here silently dropped the
            # property filter — the CTE returned all nodes matching
            # the label, and the residual WHERE (populated from
            # `query.where`, NOT from pattern properties) didn't
            # re-apply it.
            if not _is_safe_ident(k):
                return None
            start_filters.append(f"n0.{k} = ?")
            start_params.append(v)

        # Build end-node filter SQL
        end_filters: List[str] = []
        end_params: List[Any] = []
        if end_pat.label:
            end_filters.append("ndst.kind = ?")
            end_params.append(end_pat.label)
        for k, v in (end_pat.properties or {}).items():
            # See start_pat.properties comment — same injection guard.
            if not _is_safe_ident(k):
                return None
            end_filters.append(f"ndst.{k} = ?")
            end_params.append(v)

        # Fold simple WHERE equality filters on start/end nodes into SQL so
        # they apply BEFORE LIMIT. Only top-level `var.attr = literal` clauses
        # are hoisted; everything else is evaluated in Python after fetching.
        sql_start_filters: List[str] = []
        sql_end_filters: List[str] = []
        sql_start_params: List[Any] = []
        sql_end_params: List[Any] = []
        residual_where: Optional[WhereClause] = None
        if query.where is not None:
            hoisted, residual_where = _hoist_where_to_sql(
                query.where, start_pat.variable, end_pat.variable,
                sql_start_filters, sql_end_filters,
                sql_start_params, sql_end_params,
            )
            del hoisted  # filters are appended in-place above
        start_filters.extend(sql_start_filters)
        start_params.extend(sql_start_params)
        end_filters.extend(sql_end_filters)
        end_params.extend(sql_end_params)

        # Edge direction in recursive step
        if direction == "<-":
            edge_src, edge_dst = "dst_id", "src_id"
        else:
            edge_src, edge_dst = "src_id", "dst_id"

        start_where = (" WHERE " + " AND ".join(start_filters)) if start_filters else ""
        end_where = (" AND " + " AND ".join(end_filters)) if end_filters else ""

        sql = f"""
            WITH RECURSIVE traverse(depth, node_id, start_id, path_ids) AS (
                SELECT 0, n0.id, n0.id, ',' || n0.id || ','
                FROM cgdb_nodes n0{start_where}
                UNION ALL
                SELECT t.depth + 1, e.{edge_dst}, t.start_id,
                       t.path_ids || e.{edge_dst} || ','
                FROM cgdb_edges e
                JOIN traverse t ON e.{edge_src} = t.node_id
                WHERE t.depth < ?
                  AND e.kind = ?
                  AND t.path_ids NOT LIKE '%,' || e.{edge_dst} || ',%'
            )
            SELECT DISTINCT t.start_id, t.node_id, t.depth,
                            n_start.name, n_start.fqn, n_start.kind, n_start.line,
                            n_end.name, n_end.fqn, n_end.kind, n_end.line
            FROM traverse t
            JOIN cgdb_nodes n_start ON n_start.id = t.start_id
            JOIN cgdb_nodes n_end ON n_end.id = t.node_id
            WHERE t.depth >= ?{end_where}
            LIMIT ?
        """
        params = start_params + [max_depth, edge_kind, rel.min_hops] + end_params + [query.limit if query.limit is not None else 500]
        rows = conn.execute(sql, params).fetchall()
        results: List[Dict] = []
        for r in rows:
            start_dict = {
                "id": r[0], "depth": r[2], "name": r[3],
                "fqn": r[4], "kind": r[5], "line": r[6],
            }
            end_dict = {
                "id": r[1], "depth": r[2], "name": r[7],
                "fqn": r[8], "kind": r[9], "line": r[10],
            }
            binding = {}
            if start_pat.variable:
                binding[start_pat.variable] = start_dict
            if end_pat.variable:
                binding[end_pat.variable] = end_dict
            # Apply remaining WHERE filters (non-shape filters)
            if residual_where is not None and not _eval_where(residual_where, binding):
                continue
            row = _project_return(query.return_items, binding)
            results.append(row)
        return results
    except Exception:
        # Any error → fall back to networkx
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def cmd_query(args):
    """Unified graph query command.

    Parses a Cypher-subset query and executes it against the graph.
    Routes to SQLite when available, falls back to NetworkX.
    """
    query_str = args.query
    if not query_str:
        print("Error: --query is required", file=sys.stderr)
        sys.exit(1)

    try:
        q = parse_query(query_str)
    except SyntaxError as exc:
        print(f"Query parse error: {exc}", file=sys.stderr)
        sys.exit(1)

    from _builder.graph_build import _load_full_graph
    graph_dir = args.graph
    G = _load_full_graph(graph_dir)

    # If a cgdb store is available, register a CONFIG() lookup function so
    # WHERE CONFIG(n, 'CONFIG_X') filters can resolve node → predicate via
    # the config_predicates table.
    cgdb_store = None
    try:
        from _builder.cgdb_store import SQLiteCGDBStore
        db_path = os.path.join(graph_dir, "code2database.db")
        if os.path.exists(db_path):
            tmp_store = SQLiteCGDBStore(db_path)
            conn = tmp_store._ensure_conn()
            try:
                conn.execute("SELECT 1 FROM cgdb_nodes LIMIT 1").fetchone()
                cgdb_store = tmp_store
            except Exception:
                tmp_store.close()
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    if cgdb_store is not None:
        def _cgdb_config_lookup(node_id: int) -> List[Dict]:
            try:
                return cgdb_store.find_configs_for(int(node_id))
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                return []
        set_config_lookup_fn(_cgdb_config_lookup)
    else:
        set_config_lookup_fn(None)

    try:
        # Try recursive CTE path first when cgdb store is available
        cte_rows = _try_cte_execution(q, cgdb_store) if cgdb_store is not None else None
        if cte_rows is not None:
            rows = cte_rows
            # CTE path bypasses execute_query's post-processing — apply
            # aggregate / GROUP BY / ORDER BY here so the result matches.
            has_aggregate = any(item.is_aggregate for item in q.return_items)
            if has_aggregate or q.group_by:
                rows = _apply_group_by_and_aggregates(q, rows)
            if q.order_by:
                rows = _apply_order_by(rows, q.order_by, q.order_desc)
            if q.limit is not None and len(rows) > q.limit:
                rows = rows[:q.limit]
        else:
            rows = execute_query(q, G)
    finally:
        set_config_lookup_fn(None)
        if cgdb_store is not None:
            cgdb_store.close()

    if getattr(args, "format", "json") == "md":
        # Simple markdown table rendering
        if not rows:
            print("No results.")
            return
        cols = list(rows[0].keys())
        print("| " + " | ".join(cols) + " |")
        print("| " + " | ".join("---" for _ in cols) + " |")
        for row in rows:
            print("| " + " | ".join(str(row.get(c, "")) for c in cols) + " |")
    else:
        # Phase 3: consult memory/knowledge before printing graph rows.
        # This realizes the SKILL.md "Query Priority Chain:
        # memory → knowledge → graph → source" — when the user asks a
        # natural-language-shaped query, we surface top kb_paragraphs
        # hits as a `_hints` field alongside the graph rows.
        #
        # Backward-compat: by default stdout stays a flat rows list
        # (so existing consumers don't break). Hints go to stderr as a
        # `kb_hints: <json>` line. Pass --with-hints to wrap stdout as
        # {"rows": [...], "_hints": [...]}.
        hints: list = []
        try:
            from _builder.kb_index import query_kb
            hints = query_kb(
                graph_dir=graph_dir,
                query=query_str,
                top_n=3,
                kinds=None,  # any kind
                min_weight=0.0,
                max_tokens=1000,
                log_query=False,  # don't log cmd_query hints
            )
        except Exception:
            # Hints are best-effort; never fail the query because of them.
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        with_hints = getattr(args, "with_hints", False)
        if with_hints:
            output = {
                "rows": list(rows),
                "_hints": [
                    {
                        "source_kind": h["source_kind"],
                        "source_file": h["source_file"],
                        "title": h["title"],
                        "body": h["body"][:500],
                        "score": h["score"],
                        "kind": h["kind"],
                    }
                    for h in hints
                ],
            }
            print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
        else:
            # Backward-compat: stdout is a flat rows list.
            # Hints (if any) go to stderr so they don't corrupt stdout parsing.
            if hints:
                try:
                    import sys as _sys
                    _sys.stderr.write(
                        "kb_hints: " + json.dumps([
                            {"source_kind": h["source_kind"],
                             "title": h["title"],
                             "score": h["score"]}
                            for h in hints
                        ], ensure_ascii=False) + "\n"
                    )
                except Exception:
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
            print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
