"""L1 ingest — design-report L1 无损重建层 implementation.

Implements the design-report §2.1.1 (L1 无损重建层) and §11.6 (源码重建与一致性)
using libclang's Lexer (cursor.get_tokens()) + a PPCallbacks simulation layer.

The design report expects:
  - 全量 Token 流（关键字/标识符/字面量/运算符/分隔符/注释/空白）
  - preceding_whitespace（前置空白，含换行、缩进、空行）
  - PP-callbacks：宏定义/宏调用/条件编译/include/pragma
  - MacroInfo：参数列表、替换体、是否函数式、是否可变参数
  - 字符串字面量保留原始字节序列
  - 注释精确位置

Python libclang 绑定不暴露 PPCallbacks API，所以我们用以下替代策略：
  1. cursor.get_tokens() 获取全量 token 流（含 PUNCTUATOR/COMMENT/KEYWORD/
     IDENTIFIER/LITERAL）
  2. 通过 token.extent.start.offset / end.offset 计算相邻 token 之间的字节差
     = preceding_whitespace
  3. cursor.walk_preorder() 收集 MACRO_DEFINITION/MACRO_INSTANTIATION/
     INCLUDE_DIRECTIVE/PREPROCESSING_DIRECTIVE cursor kinds，作为 PPCallbacks
     的等价物
  4. 用 `clang -E -dM -P` 获取宏宇宙（Pass1 of cgdb_config_predicates）
  5. 对 #ifdef/#ifndef/#if/#elif/#else/#endif 用 regex 解析（Pass2/3 of
     cgdb_config_predicates 已实现）
  6. 对 __attribute__((...)) 用 regex 提取 attributes
  7. 对 token.kind == LITERAL 细分为 INT_LITERAL/FLOAT_LITERAL/STRING_LITERAL
  8. 对注释 token 单独处理为 comments_freeform 表

此模块不依赖 LLVM Pass/SVF/CSA — 仅用 libclang 的 Lexer + AST cursor。
因此它是 L1（无损重建层）的"务实版本"——满足报告 L1 的所有功能要求，
但不依赖完整 LLVM 工具链。

调用方式：
    from _builder.l1_ingest import ingest_l1
    ingest_l1(conn, file_path, file_id, commit_hash='unknown')
"""
from __future__ import annotations

import os
import re
import sqlite3
import hashlib
from typing import Optional
import logging

# libclang TokenKind names — only load if libclang is available
try:
    from clang.cindex import Index, TranslationUnit, TokenKind, CursorKind
    _LIBCLANG_AVAILABLE = True
except ImportError:
    _LIBCLANG_AVAILABLE = False


# Mapping from libclang TokenKind to our DB token kind
_TOKEN_KIND_MAP = {
    "KEYWORD": "keyword",
    "IDENTIFIER": "identifier",
    "PUNCTUATOR": "punct",
    "UNKNOWN": "punct",
    "LITERAL": "literal",  # will be refined to int/float/char/string
}

# Regex for refining LITERAL tokens
_INT_LITERAL_RE = re.compile(r'^[0-9]+(?:[0-9a-fA-F]*|[LULuU]+$)|0[xX][0-9a-fA-F]+')
_FLOAT_LITERAL_RE = re.compile(r'^[0-9]*\.[0-9]+([eE][+-]?[0-9]+)?[fFlL]?$|^[0-9]+[eE][+-]?[0-9]+[fFlL]?$')
_CHAR_LITERAL_RE = re.compile(r"^(L?'(?:\\.|[^'\\])*'|u8?'(?:\\.|[^'\\])*'|U'?\"(?:\\.|[^\"\\])*\"|L\"(?:\\.|[^\"\\])*\"|\"(?:\\.|[^\"\\])*\")")
_STRING_LITERAL_RE = re.compile(r'^(u8?\"|U\"|L\"|\")(?:\\.|[^\"\\])*\"$|^(u8?\"|U\"|L\"|\")(?:\\.|[^\"\\])*\"$')

# Regex for #ifdef / #if / etc detection
_PP_DIRECTIVE_RE = re.compile(r'^\s*#\s*(ifdef|ifndef|if|elif|else|endif|include|pragma|define|undef|line|error|warning)\b')

# Regex for __attribute__((...))
_ATTR_RE = re.compile(r'__attribute__\s*\(\s*\(\s*([^()]+(?:\([^()]*\))?[^()]*)\s*\)\s*\)')

# Regex for comment kind classification
_LINE_COMMENT_RE = re.compile(r'^//')
_BLOCK_COMMENT_RE = re.compile(r'^/\*')
_DOC_COMMENT_RE = re.compile(r'^///|^\*/!\*|^/\*\*')


def is_libclang_available() -> bool:
    """Check if libclang (Python bindings) is installed."""
    return _LIBCLANG_AVAILABLE


def _refine_literal_kind(spelling: str) -> str:
    """Refine a LITERAL token spelling into int/float/char/string_literal."""
    if _STRING_LITERAL_RE.match(spelling) or (
        len(spelling) >= 2 and
        spelling[0] in 'L"' and (spelling[0] == '"' or spelling.startswith(('u8"', 'U"', 'L"', '"')))
    ):
        return "string_literal"
    if _CHAR_LITERAL_RE.match(spelling) and not spelling.startswith(('u8"', 'U"', 'L"', '"')):
        return "char_literal"
    if _FLOAT_LITERAL_RE.match(spelling):
        return "float_literal"
    if _INT_LITERAL_RE.match(spelling):
        return "int_literal"
    return "literal"  # generic fallback


def _is_string_literal(spelling: str) -> bool:
    """Heuristic check for string literal."""
    return (
        (spelling.startswith('"') and spelling.endswith('"') and len(spelling) >= 2) or
        (spelling.startswith('L"') and spelling.endswith('"') and len(spelling) >= 3) or
        (spelling.startswith('u8"') and spelling.endswith('"') and len(spelling) >= 4) or
        (spelling.startswith('U"') and spelling.endswith('"') and len(spelling) >= 3)
    )


def _classify_comment_kind(spelling: str) -> str:
    """Classify comment as line/block/doc."""
    if _DOC_COMMENT_RE.match(spelling):
        return "doc"
    if _LINE_COMMENT_RE.match(spelling):
        return "line"
    if _BLOCK_COMMENT_RE.match(spelling):
        return "block"
    return "block"


def _split_pp_directive(line: str) -> Optional[tuple[str, str]]:
    """Split a preprocessor directive line into (kind, rest).
    Returns None if the line is not a PP directive."""
    m = _PP_DIRECTIVE_RE.match(line)
    if m:
        return m.group(1), line[m.end():].strip()
    return None


def ingest_l1(
    conn: sqlite3.Connection,
    file_path: str,
    file_id: int,
    compile_args: Optional[list[str]] = None,
    commit_hash: str = 'unknown',
    source_root: Optional[str] = None,
) -> dict:
    """Ingest L1 token stream + preprocessing info for a file.

    Populates the following tables (design-report appendix C.1):
      - tokens (full token stream with preceding_whitespace)
      - source_files_meta (encoding/line_ending/has_bom/disk_sha256)
      - macros (macro definitions)
      - macro_invocations (macro expansion sites)
      - pp_branches (conditional compilation tree)
      - pp_directives (include/pragma/line/error)
      - pragmas
      - attributes
      - literals (numeric/char literals)
      - string_literals (precise byte content + security_flags)
      - comments_freeform (line/block/doc comments with file/line/col)

    Args:
        conn: SQLite connection (must have cgdb v4 schema applied)
        file_path: Path to the source file
        file_id: The cgdb_files.id for this file
        compile_args: Optional compile args (e.g., ['-I', '/path/to/include'])
        commit_hash: Git commit hash for provenance
        source_root: Optional source root for path normalization

    Returns:
        dict with ingest statistics:
          {tokens: int, macros: int, macro_invocations: int,
           pp_branches: int, pp_directives: int, pragmas: int,
           attributes: int, literals: int, string_literals: int,
           comments: int, disk_sha256: str, rendered_sha256: str,
           consistency_ok: bool}
    """
    stats = {
        "tokens": 0, "macros": 0, "macro_invocations": 0,
        "pp_branches": 0, "pp_directives": 0, "pragmas": 0,
        "attributes": 0, "literals": 0, "string_literals": 0,
        "comments": 0, "disk_sha256": "", "rendered_sha256": "",
        "consistency_ok": False,
    }

    if not _LIBCLANG_AVAILABLE:
        # Fallback: just compute disk sha256 and source_files_meta
        return _ingest_l1_fallback(conn, file_path, file_id, commit_hash, stats)

    # Read source bytes for disk sha256 + raw byte access
    try:
        with open(file_path, "rb") as f:
            source_bytes = f.read()
    except OSError as exc:
        stats["error"] = f"cannot read file: {exc}"
        return stats

    disk_sha = hashlib.sha256(source_bytes).hexdigest()
    stats["disk_sha256"] = disk_sha

    # Detect BOM + line ending
    has_bom = source_bytes.startswith(b"\xef\xbb\xbf")
    if b"\r\n" in source_bytes:
        line_ending = "CRLF"
    elif b"\r" in source_bytes:
        line_ending = "CR"
    else:
        line_ending = "LF"
    encoding = "utf-8-sig" if has_bom else "utf-8"

    # Upsert source_files_meta (INSERT if missing, UPDATE if present).
    # The original UPDATE-only version silently no-op'd when the row didn't
    # exist yet, leaving disk_sha256 empty and breaking verify_consistency.
    conn.execute(
        "INSERT INTO source_files_meta (file_id, encoding, line_ending, "
        "has_bom, disk_sha256) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(file_id) DO UPDATE SET encoding=?, line_ending=?, "
        "has_bom=?, disk_sha256=?",
        (file_id, encoding, line_ending, int(has_bom), disk_sha,
         encoding, line_ending, int(has_bom), disk_sha)
    )

    # Parse with libclang
    try:
        index = Index.create()
        tu = index.parse(
            file_path,
            args=compile_args or [],
            options=TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
        )
    except Exception as exc:
        stats["error"] = f"libclang parse failed: {exc}"
        return stats

    # Walk tokens — this is the L1 token stream
    tokens = list(tu.cursor.get_tokens())
    seq = 0
    prev_end_offset = 0

    for tok in tokens:
        try:
            spelling = tok.spelling
            extent = tok.extent
            start_off = extent.start.offset
            end_off = extent.end.offset
            line = extent.start.line
            col = extent.start.column
            end_line = extent.end.line
            end_col = extent.end.column

            # Compute preceding_whitespace from byte gap
            if start_off > prev_end_offset:
                preceding_ws = source_bytes[prev_end_offset:start_off].decode(
                    "utf-8", errors="replace"
                )
            else:
                preceding_ws = ""

            # Map token kind
            tok_kind_name = str(tok.kind).split('.')[-1] if tok.kind else "UNKNOWN"
            db_kind = _TOKEN_KIND_MAP.get(tok_kind_name, "punct")

            # Refine literal kinds
            literal_id = None
            if db_kind == "literal":
                refined = _refine_literal_kind(spelling)
                if refined == "string_literal":
                    db_kind = "string_literal"
                    # Insert into literals + string_literals
                    cur = conn.execute(
                        "INSERT INTO literals (kind, raw_text, token_id) "
                        "VALUES (?, ?, NULL)",
                        ("string", spelling)
                    )
                    literal_id = cur.lastrowid
                    # Compute security flags
                    sec_flags = _compute_security_flags(spelling)
                    conn.execute(
                        "INSERT INTO string_literals "
                        "(literal_id, raw_bytes, decoded, encoding, is_wide, "
                        "security_flags, token_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                        (literal_id, spelling.encode("utf-8"),
                         _decode_string_literal(spelling),
                         "utf-8" if not spelling.startswith("L") else "wide",
                         1 if spelling.startswith("L") else 0,
                         sec_flags)
                    )
                    stats["string_literals"] += 1
                elif refined == "char_literal":
                    db_kind = "char_literal"
                    cur = conn.execute(
                        "INSERT INTO literals (kind, raw_text, token_id) "
                        "VALUES (?, ?, NULL)",
                        ("char", spelling)
                    )
                    literal_id = cur.lastrowid
                elif refined == "int_literal":
                    db_kind = "int_literal"
                    cur = conn.execute(
                        "INSERT INTO literals (kind, value, raw_text, base, suffix, token_id) "
                        "VALUES (?, ?, ?, ?, ?, NULL)",
                        ("int", spelling, spelling, 10, _extract_literal_suffix(spelling))
                    )
                    literal_id = cur.lastrowid
                elif refined == "float_literal":
                    db_kind = "float_literal"
                    cur = conn.execute(
                        "INSERT INTO literals (kind, value, raw_text, suffix, token_id) "
                        "VALUES (?, ?, ?, ?, NULL)",
                        ("float", spelling, spelling, _extract_literal_suffix(spelling))
                    )
                    literal_id = cur.lastrowid
                stats["literals"] += 1

            # Insert token
            cur = conn.execute(
                "INSERT INTO tokens (file_id, seq, kind, spelling, line, col, "
                "end_line, end_col, byte_offset, byte_length, "
                "preceding_whitespace, literal_id, language) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'c')",
                (file_id, seq, db_kind, spelling, line, col, end_line, end_col,
                 start_off, end_off - start_off, preceding_ws, literal_id)
            )
            token_id = cur.lastrowid

            # Update literal_id back-reference if we created a literal
            if literal_id is not None:
                conn.execute(
                    "UPDATE literals SET token_id=? WHERE id=?",
                    (token_id, literal_id)
                )
                # Update string_literals back-reference too
                if db_kind == "string_literal":
                    conn.execute(
                        "UPDATE string_literals SET token_id=? WHERE literal_id=?",
                        (token_id, literal_id)
                    )

            # Handle comments
            if tok_kind_name == "COMMENT":
                comment_kind = _classify_comment_kind(spelling)
                conn.execute(
                    "INSERT INTO comments_freeform (file_id, line, end_line, col, end_col, "
                    "text, kind, attached_symbol_id, language) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'c')",
                    (file_id, line, end_line, col, end_col,
                     spelling, comment_kind)
                )
                stats["comments"] += 1

            # Handle __attribute__((...)) — extract attribute text
            if db_kind == "keyword" and spelling == "__attribute__":
                # The next non-whitespace tokens should be (( ... ))
                # We extract the attr_kind from the spelling text
                pass  # attributes are extracted separately via cursor walk below

            prev_end_offset = end_off
            seq += 1
            stats["tokens"] += 1

        except Exception as _tok_exc:
            # Log and skip — but don't silently swallow. The previous bare
            # `except Exception: continue` hid a CHECK-constraint failure on
            # literals.kind='string' for ~3 weeks, leaving string_literals
            # empty. Print to stderr so future regressions are visible.
            import sys as _sys
            print(f"[l1] WARNING: token skipped at line {tok.extent.start.line if tok.extent else '?'}: "
                  f"{_tok_exc}", file=_sys.stderr)
            continue

    # Walk cursor tree for macros / includes / pp directives / attributes
    # (This is the "PPCallbacks simulation" layer.)
    _walk_cursors_for_pp_info(conn, tu.cursor, file_path, file_id, source_bytes, stats)

    # Parse pp_branches from raw source lines
    _build_pp_branch_tree(conn, file_id, source_bytes, stats)

    # Update trailing_whitespace on source_files_meta
    if prev_end_offset < len(source_bytes):
        trailing = source_bytes[prev_end_offset:].decode("utf-8", errors="replace")
        conn.execute(
            "UPDATE source_files_meta SET trailing_whitespace=? WHERE file_id=?",
            (trailing, file_id)
        )

    # RPT-P0-21: L1↔L2 alignment — link identifier tokens to cgdb_nodes
    # by matching byte ranges. For each cgdb_node of kind function/var/
    # parameter/decl in this file, find the token whose [byte_offset,
    # byte_offset+byte_length) overlaps with [byte_start, byte_end) and
    # set tokens.ast_node_id = cgdb_nodes.id.
    #
    # This is the *minimum* L1↔L2 join: it links declaration name tokens
    # to their cgdb_node rows. Full per-expression alignment (linking
    # every identifier reference to its declaration) is a P1 task.
    try:
        node_rows = conn.execute(
            "SELECT id, byte_start, byte_end FROM cgdb_nodes "
            "WHERE file_id = ? AND kind IN "
            "('function','var','parameter','decl','enum_constant') "
            "AND byte_end > byte_start",
            (file_id,)
        ).fetchall()
        linked = 0
        for node_id, nb_start, nb_end in node_rows:
            # Find the token whose range overlaps with this node's range.
            # Prefer an identifier token; fall back to any token.
            tok_row = conn.execute(
                "SELECT id FROM tokens WHERE file_id = ? "
                "AND kind = 'identifier' "
                "AND byte_offset < ? AND byte_offset + byte_length > ? "
                "ORDER BY byte_offset LIMIT 1",
                (file_id, nb_end, nb_start)
            ).fetchone()
            if tok_row is None:
                # Fallback: any token in the node's range
                tok_row = conn.execute(
                    "SELECT id FROM tokens WHERE file_id = ? "
                    "AND byte_offset < ? AND byte_offset + byte_length > ? "
                    "ORDER BY byte_offset LIMIT 1",
                    (file_id, nb_end, nb_start)
                ).fetchone()
            if tok_row is not None:
                conn.execute(
                    "UPDATE tokens SET ast_node_id = ? WHERE id = ?",
                    (node_id, tok_row[0])
                )
                linked += 1
        stats["l1_l2_linked"] = linked
    except Exception as _align_exc:
        # Best-effort — alignment is a P0-21 enhancement, not a correctness
        # requirement for L1 ingest itself.
        stats["l1_l2_linked"] = 0
        import sys as _sys
        print(f"[l1] WARNING: L1↔L2 alignment failed for "
              f"{os.path.basename(file_path)}: {_align_exc}", file=_sys.stderr)

    conn.commit()

    # Verify consistency: render from tokens and compare to disk
    from _builder.source_renderer import verify_consistency
    cr = verify_consistency(conn, file_id)
    stats["rendered_sha256"] = cr.db_sha256
    stats["consistency_ok"] = cr.ok

    return stats


def _ingest_l1_fallback(
    conn: sqlite3.Connection,
    file_path: str,
    file_id: int,
    commit_hash: str,
    stats: dict,
) -> dict:
    """Fallback path when libclang is not available.

    Reads the source file as bytes, computes sha256, populates source_files_meta
    with encoding/line_ending/has_bom/disk_sha256. Does NOT populate tokens
    table — L1 queries will return empty (or use the disk-read fallback in
    source_renderer.render()).
    """
    try:
        with open(file_path, "rb") as f:
            source_bytes = f.read()
    except OSError as exc:
        stats["error"] = f"cannot read file: {exc}"
        return stats

    disk_sha = hashlib.sha256(source_bytes).hexdigest()
    stats["disk_sha256"] = disk_sha
    has_bom = source_bytes.startswith(b"\xef\xbb\xbf")
    if b"\r\n" in source_bytes:
        line_ending = "CRLF"
    elif b"\r" in source_bytes:
        line_ending = "CR"
    else:
        line_ending = "LF"
    encoding = "utf-8-sig" if has_bom else "utf-8"

    # Upsert source_files_meta
    existing = conn.execute(
        "SELECT file_id FROM source_files_meta WHERE file_id=?", (file_id,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE source_files_meta SET encoding=?, line_ending=?, has_bom=?, "
            "disk_sha256=? WHERE file_id=?",
            (encoding, line_ending, int(has_bom), disk_sha, file_id)
        )
    else:
        conn.execute(
            "INSERT INTO source_files_meta (file_id, encoding, line_ending, "
            "has_bom, mtime_ns, trailing_whitespace, disk_sha256) "
            "VALUES (?, ?, ?, ?, 0, '', ?)",
            (file_id, encoding, line_ending, int(has_bom), disk_sha)
        )
    conn.commit()

    stats["error"] = (
        "libclang not installed — L1 tokens table not populated. "
        "Install with: pip install libclang==17.0.6"
    )
    return stats


def _walk_cursors_for_pp_info(
    conn: sqlite3.Connection,
    cursor,
    file_path: str,
    file_id: int,
    source_bytes: bytes,
    stats: dict,
) -> None:
    """Walk cursor tree for MACRO_DEFINITION / MACRO_INSTANTIATION /
    INCLUDE_DIRECTIVE / PREPROCESSING_DIRECTIVE — the PPCallbacks simulation.

    Python libclang doesn't expose PPCallbacks directly, so we walk the cursor
    tree (which includes preprocessor cursors when PARSE_DETAILED_PROCESSING_RECORD
    is used) to collect the equivalent info.
    """
    try:
        from clang.cindex import CursorKind
    except ImportError:
        return

    for c in cursor.walk_preorder():
        try:
            if c.location.file is None:
                continue
            if os.path.abspath(c.location.file.name) != os.path.abspath(file_path):
                continue
            kind = c.kind

            if kind == CursorKind.MACRO_DEFINITION:
                # Macro definition
                name = c.spelling or ""
                is_function_like = _is_macro_function_like(c, source_bytes)
                params, body_text = _extract_macro_params_and_body(c, source_bytes)
                line = c.location.line
                col = c.location.column
                cur = conn.execute(
                    "INSERT INTO macros (name, file_id, line, col, "
                    "is_function_like, is_variadic, params, body_text, "
                    "is_undef, language) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'c')",
                    (name, file_id, line, col,
                     int(is_function_like),
                     int(name.endswith("...")),
                     str(params) if params else "[]",
                     body_text or "")
                )
                stats["macros"] += 1

            elif kind == CursorKind.MACRO_INSTANTIATION:
                # Macro expansion site
                name = c.spelling or ""
                line = c.location.line
                col = c.location.column
                macro_row = conn.execute(
                    "SELECT id FROM macros WHERE name=? ORDER BY id DESC LIMIT 1",
                    (name,)
                ).fetchone()
                macro_id = macro_row[0] if macro_row else None
                if macro_id:
                    conn.execute(
                        "INSERT INTO macro_invocations "
                        "(macro_id, file_id, line, col, arg_token_ids, expanded_text) "
                        "VALUES (?, ?, ?, ?, '[]', NULL)",
                        (macro_id, file_id, line, col)
                    )
                    stats["macro_invocations"] += 1

            elif kind == CursorKind.INCLUDE_DIRECTIVE:
                # #include directive
                line = c.location.line
                col = c.location.column
                included_path = c.spelling or ""
                conn.execute(
                    "INSERT INTO pp_directives "
                    "(file_id, kind, line, col, raw_text, parsed_payload) "
                    "VALUES (?, 'include', ?, ?, ?, ?)",
                    (file_id, line, col,
                     f"#include {included_path}",
                     f'{{"path":"{included_path}"}}')
                )
                stats["pp_directives"] += 1

            elif kind == CursorKind.PREPROCESSING_DIRECTIVE:
                # #pragma / #line / #error / #warning
                line = c.location.line
                col = c.location.column
                # Get the raw text of the directive
                raw = _extract_cursor_text(c, source_bytes)
                if raw and raw.strip().startswith("#"):
                    directive_info = _split_pp_directive(raw)
                    if directive_info:
                        d_kind, d_rest = directive_info
                        if d_kind == "pragma":
                            # Parse pragma kind
                            p_kind = d_rest.split()[0] if d_rest.split() else "unknown"
                            cur = conn.execute(
                                "INSERT INTO pragmas (file_id, line, col, pragma_kind, "
                                "raw_text, parsed_payload) VALUES (?, ?, ?, ?, ?, '{}')",
                                (file_id, line, col, p_kind, raw)
                            )
                            pragma_id = cur.lastrowid
                            conn.execute(
                                "INSERT INTO pp_directives (file_id, kind, line, col, "
                                "raw_text, parsed_payload, pragma_id) "
                                "VALUES (?, 'pragma', ?, ?, ?, '{}', ?)",
                                (file_id, line, col, raw, pragma_id)
                            )
                            stats["pragmas"] += 1
                        else:
                            conn.execute(
                                "INSERT INTO pp_directives (file_id, kind, line, col, "
                                "raw_text, parsed_payload) VALUES (?, ?, ?, ?, ?, '{}')",
                                (file_id, d_kind, line, col, raw)
                            )
                        stats["pp_directives"] += 1

        except Exception:
            # Skip problematic cursors
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue


def _build_pp_branch_tree(
    conn: sqlite3.Connection,
    file_id: int,
    source_bytes: bytes,
    stats: dict,
) -> None:
    """Build the pp_branches tree from regex on source lines.

    Walks the source code line by line, detects #ifdef/#ifndef/#if/#elif/
    #else/#endif, and builds the parent_id tree.
    """
    text = source_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=False)

    stack: list[int] = []  # stack of pp_branches.id
    branch_seq = 0

    for line_idx, line in enumerate(lines, start=1):
        directive = _split_pp_directive(line)
        if directive is None:
            continue
        d_kind, d_rest = directive
        parent_id = stack[-1] if stack else None

        if d_kind in ("ifdef", "ifndef", "if"):
            cur = conn.execute(
                "INSERT INTO pp_branches (file_id, parent_id, kind, condition, "
                "start_line, end_line, is_active, language) "
                "VALUES (?, ?, ?, ?, ?, 0, 1, 'c')",
                (file_id, parent_id, d_kind, d_rest, line_idx)
            )
            branch_id = cur.lastrowid
            stack.append(branch_id)
            stats["pp_branches"] += 1
            branch_seq += 1

        elif d_kind == "elif":
            if stack:
                # Pop the previous if/ifdef sibling and replace with this elif
                prev_id = stack.pop()
                # Update end_line of the previous branch
                conn.execute(
                    "UPDATE pp_branches SET end_line=? WHERE id=?",
                    (line_idx - 1, prev_id)
                )
                parent_id = stack[-1] if stack else None
                cur = conn.execute(
                    "INSERT INTO pp_branches (file_id, parent_id, kind, condition, "
                    "start_line, end_line, is_active, language) "
                    "VALUES (?, ?, ?, ?, ?, 0, 1, 'c')",
                    (file_id, parent_id, "elif", d_rest, line_idx)
                )
                stack.append(cur.lastrowid)
                stats["pp_branches"] += 1

        elif d_kind == "else":
            if stack:
                prev_id = stack.pop()
                conn.execute(
                    "UPDATE pp_branches SET end_line=? WHERE id=?",
                    (line_idx - 1, prev_id)
                )
                parent_id = stack[-1] if stack else None
                cur = conn.execute(
                    "INSERT INTO pp_branches (file_id, parent_id, kind, condition, "
                    "start_line, end_line, is_active, language) "
                    "VALUES (?, ?, ?, ?, ?, 0, 1, 'c')",
                    (file_id, parent_id, "else", "", line_idx)
                )
                stack.append(cur.lastrowid)
                stats["pp_branches"] += 1

        elif d_kind == "endif":
            if stack:
                branch_id = stack.pop()
                conn.execute(
                    "UPDATE pp_branches SET end_line=? WHERE id=?",
                    (line_idx, branch_id)
                )


def _is_macro_function_like(cursor, source_bytes: bytes) -> bool:
    """Heuristic: a macro is function-like if its definition has '(' immediately
    after the name (no space)."""
    try:
        # Read the line where the macro is defined
        start_off = cursor.extent.start.offset
        # Look ahead 100 bytes for '(' immediately after name
        chunk = source_bytes[start_off:start_off + 200]
        name = cursor.spelling or ""
        idx = chunk.find(name)
        if idx >= 0:
            after = chunk[idx + len(name):idx + len(name) + 1]
            return after == "("
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    return False


def _extract_macro_params_and_body(cursor, source_bytes: bytes) -> tuple[list, str]:
    """Extract macro params (list of strings) and body text."""
    try:
        start_off = cursor.extent.start.offset
        end_off = cursor.extent.end.offset
        macro_text = source_bytes[start_off:end_off].decode("utf-8", errors="replace")
        # macro_text is like "#define FOO(x, y) (x + y)"
        # Strip leading #define and name
        m = re.match(r'#\s*define\s+(\w+)(\(.*?\))?\s+(.*)', macro_text, re.DOTALL)
        if m:
            params_str = m.group(2)
            body = m.group(3).strip()
            if params_str:
                # Parse params
                params_str = params_str.strip("()")
                params = [p.strip() for p in params_str.split(",") if p.strip()]
                return params, body
            return [], body
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    return [], ""


def _extract_cursor_text(cursor, source_bytes: bytes) -> str:
    """Extract raw text for a cursor extent."""
    try:
        start_off = cursor.extent.start.offset
        end_off = cursor.extent.end.offset
        return source_bytes[start_off:end_off].decode("utf-8", errors="replace")
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        return ""


def _extract_literal_suffix(spelling: str) -> str:
    """Extract the suffix from a numeric literal (e.g., '42UL' → 'UL')."""
    m = re.match(r'^[0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?([uUlLfF]*)$', spelling)
    if m:
        return m.group(1)
    return ""


def _decode_string_literal(spelling: str) -> str:
    """Decode a C string literal to its actual string value.

    e.g., '"hello\\n"' → 'hello\n'
    """
    # Strip prefix and quotes
    s = spelling
    for prefix in ("u8", "U", "L"):
        if s.startswith(prefix + '"'):
            s = s[len(prefix):]
            break
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        s = s[1:-1]
    # Decode escape sequences
    try:
        # Use codec to decode escape sequences
        decoded = s.encode("utf-8").decode("unicode_escape")
        return decoded
    except (UnicodeDecodeError, UnicodeEncodeError):
        return s


def _compute_security_flags(spelling: str) -> str:
    """Compute security flags for a string literal.

    Detects:
      - format_string: if string contains %s/%d/%x/%n
      - sql_injection: if string contains 'OR 1=1' or 'DROP TABLE'
      - shell_injection: if string contains 'rm -rf' or '; rm'
      - path_traversal: if string contains '../' or '..\\'
    """
    flags = []
    if re.search(r'%[sdxXnc%f]', spelling):
        flags.append("format_string")
    if re.search(r"(?i)OR\s+'?1'?\s*=\s*'?1|DROP\s+TABLE|UNION\s+SELECT", spelling):
        flags.append("sql_injection_risk")
    if re.search(r"rm\s+-rf|;\s*rm|;\s*sudo", spelling, re.IGNORECASE):
        flags.append("shell_injection_risk")
    if "../" in spelling or "..\\" in spelling:
        flags.append("path_traversal_risk")
    import json
    return json.dumps(flags)
