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

# Module-level globals for fork COW parallelism (set by graph_build when
# spawning ProcessPoolExecutor workers). Each forked child inherits these
# via copy-on-write — no pickling needed. Empty by default; only populated
# when _l1_ingest_proc_worker is invoked from a ProcessPoolExecutor.
_L1_DB_PATH: str = ""
_L1_SOURCE_ROOT: str = ""
_L1_COMMIT_HASH: str = "unknown"


def _resolve_source_path(file_path: str, source_root: str = "") -> str:
    """Resolve a possibly-relative or moved source path to an existing file.

    cgdb_nodes.file_path may be
      - absolute (clang_scanner uses cursor.location.file.name which
        preserves the path passed to index.parse)
      - relative (base._emit_cgdb_records stores relpath for non-C/C++ files)

    When ingest_l1 is invoked later from cmd_build, the cwd may differ from
    source_root, causing `open(file_path, 'rb')` to fail with OSError and
    leaving disk_sha256 empty.

    Resolution order:
      1. If file_path exists as-is, use it.
      2. If source_root given and file_path is relative,
         try os.path.join(source_root, file_path).
      3. If source_root given and file_path is absolute (e.g. the source
         tree was renamed after scan), try joining source_root with the
         tail of file_path (preserving subdirectories like kernel/sched/).

    Returns the original file_path if no candidate exists (caller's open()
    will then raise OSError naturally, preserving existing error handling).
    """
    if not file_path:
        return file_path
    if os.path.exists(file_path):
        return file_path
    if not source_root:
        return file_path
    if not os.path.isabs(file_path):
        cand = os.path.join(source_root, file_path)
        if os.path.exists(cand):
            return cand
    else:
        parts = file_path.replace("\\", "/").split("/")
        for start in range(1, len(parts)):
            tail = os.path.join(source_root, *parts[start:])
            if os.path.exists(tail):
                return tail
    return file_path


def _l1_ingest_proc_worker(task):
    """Module-level worker for ProcessPoolExecutor (fork COW).

    Each forked child inherits _L1_DB_PATH, _L1_SOURCE_ROOT, _L1_COMMIT_HASH
    via copy-on-write — no pickling needed for these globals. Opens its own
    SQLite connection in WAL mode so the libclang parse (CPU-bound) runs in
    parallel across N processes while SQLite handles write contention via
    busy_timeout (60s). The libclang Index is created per-worker (libclang
    is not thread/process-safe across fork without re-init).

    Args:
        task: (file_path, file_id) tuple

    Returns:
        stats dict from ingest_l1, plus 'file_path' for caller reporting.
    """
    fp, fid = task
    conn = sqlite3.connect(_L1_DB_PATH, timeout=120.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-32000")  # 32MB per worker
        conn.execute("PRAGMA temp_store=MEMORY")
        result = ingest_l1(
            conn=conn,
            file_path=fp,
            file_id=fid,
            commit_hash=_L1_COMMIT_HASH,
            source_root=_L1_SOURCE_ROOT,
        )
        result.setdefault("file_path", fp)
        return result
    except Exception as exc:
        return {
            "file_path": fp,
            "error": f"worker crashed: {exc}",
            "tokens": 0, "macros": 0, "macro_invocations": 0,
            "pp_branches": 0, "pp_directives": 0, "pragmas": 0,
            "attributes": 0, "literals": 0, "string_literals": 0,
            "comments": 0, "disk_sha256": "", "rendered_sha256": "",
            "consistency_ok": False,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_l1_ingest(tasks, db_path, source_root, commit_hash,
                  workers, parallel_mode, serial_conn=None):
    """Run L1 ingest over a list of (file_path, file_id) tasks.

    When parallel_mode == 'process' and workers > 1 and len(tasks) > 100,
    use ProcessPoolExecutor with fork COW (each worker opens its own WAL
    connection). Otherwise fall back to serial execution on serial_conn.

    The caller is responsible for committing any pending transaction on
    serial_conn BEFORE calling this function (Phase 1 must be flushed so
    parallel workers can read cgdb_nodes via their own connections).
    """
    if not tasks:
        return []
    use_process = (parallel_mode == "process" and workers > 1
                    and len(tasks) > 100)
    if use_process:
        global _L1_DB_PATH, _L1_SOURCE_ROOT, _L1_COMMIT_HASH
        _L1_DB_PATH = db_path
        _L1_SOURCE_ROOT = source_root or ""
        _L1_COMMIT_HASH = commit_hash
        try:
            from concurrent.futures import ProcessPoolExecutor
            import multiprocessing as _mp
            _ctx = _mp.get_context("fork")
            with ProcessPoolExecutor(max_workers=workers, mp_context=_ctx) as _pool:
                results = list(_pool.map(
                    _l1_ingest_proc_worker,
                    tasks,
                    chunksize=max(1, len(tasks) // (workers * 4)),
                ))
            return results
        except (ImportError, OSError, BrokenPipeError, ValueError):
            # ValueError: fork context unavailable on Windows
            pass
        finally:
            _L1_DB_PATH = ""
            _L1_SOURCE_ROOT = ""
            _L1_COMMIT_HASH = "unknown"
    conn = serial_conn if serial_conn is not None else sqlite3.connect(
        db_path, timeout=120.0)
    own_conn = serial_conn is None
    results = []
    try:
        for fp, fid in tasks:
            try:
                stats = ingest_l1(
                    conn=conn,
                    file_path=fp,
                    file_id=fid,
                    commit_hash=commit_hash,
                    source_root=source_root,
                )
                stats.setdefault("file_path", fp)
                results.append(stats)
            except Exception as exc:
                results.append({
                    "file_path": fp,
                    "error": f"ingest failed: {exc}",
                    "tokens": 0, "macros": 0, "macro_invocations": 0,
                    "pp_branches": 0, "pp_directives": 0, "pragmas": 0,
                    "attributes": 0, "literals": 0, "string_literals": 0,
                    "comments": 0, "disk_sha256": "", "rendered_sha256": "",
                    "consistency_ok": False,
                })
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass
    return results

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
    """Check if libclang (Python bindings + the native libclang.so) is
    actually usable.

    Importing the `clang` Python package succeeds even when the underlying
    libclang.so shared library is missing — Index.create() only fails at
    first use, which previously caused ingest_l1 to silently return 0
    tokens. This helper actually attempts Index.create() so callers can
    decide between the libclang path and the fallback path up-front.

    Before probing, it calls clang_scanner._configure_libclang() which
    searches common distribution paths (e.g. /usr/lib/x86_64-linux-gnu/
    libclang-18.so.1) and sets Config.set_library_file() so the probe
    can find the shared library even when libclang.so (without version
    suffix) is absent.

    The result is cached after the first call to avoid repeated dlopen
    probes on hot paths (e.g., cmd_build's per-file loop).
    """
    global _LIBCLANG_USABLE_CACHE
    if _LIBCLANG_USABLE_CACHE is not None:
        return _LIBCLANG_USABLE_CACHE
    if not _LIBCLANG_AVAILABLE:
        _LIBCLANG_USABLE_CACHE = False
        return False
    try:
        # Configure the library path before probing. clang_scanner has
        # a comprehensive search list for libclang.so variants across
        # distributions (Ubuntu, Debian, Fedora, etc.). Without this,
        # the clang Python binding only looks for libclang.so (no version
        # suffix) which is typically absent.
        try:
            from _scanner.clang_scanner import _configure_libclang
            _configure_libclang()
        except ImportError:
            pass
        Index.create()
        _LIBCLANG_USABLE_CACHE = True
    except Exception:
        _LIBCLANG_USABLE_CACHE = False
        import sys as _sys
        print(
            "[l1] WARNING: clang Python package imports but libclang.so "
            "could not be loaded — L1 token-stream ingest will fall back "
            "to sha256-only mode. Install libclang (e.g. apt install "
            "libclang-18-dev or dnf install clang-libs) to enable L1.",
            file=_sys.stderr)
    return _LIBCLANG_USABLE_CACHE


# Cache for is_libclang_available() — None = not yet probed.
_LIBCLANG_USABLE_CACHE: Optional[bool] = None


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

    if not is_libclang_available():
        # Fallback: just compute disk sha256 and source_files_meta.
        # is_libclang_available() probes Index.create() so this also
        # catches the missing-libclang.so case (the Python binding
        # imports OK but the actual shared library can't be loaded).
        return _ingest_l1_fallback(conn, file_path, file_id, commit_hash, stats,
                                    source_root=source_root)

    # Resolve file path: cgdb_nodes.file_path may be relative or may point
    # to a renamed/relocated source tree. _resolve_source_path falls back
    # to source_root join candidates. Without this, disk_sha256 ends up
    # empty and consistency_ok is False for every file.
    resolved_path = _resolve_source_path(file_path, source_root or "")
    if resolved_path != file_path:
        file_path = resolved_path

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

    # ── Phase 1: CPU-intensive work (no DB writes) ──
    # All parsing and data collection happens here.  No SQLite writes are
    # performed so the write lock is NOT held during the expensive libclang
    # parse.  This prevents "database is locked" errors when multiple
    # ProcessPoolExecutor workers parse simultaneously.

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

    # Walk tokens — collect data into in-memory lists for batch INSERT
    tokens = list(tu.cursor.get_tokens())
    _token_rows = []      # rows for executemany("INSERT INTO tokens")
    _literal_rows = []    # rows for executemany("INSERT INTO literals")
    _strlit_rows = []     # rows for executemany("INSERT INTO string_literals")
    # Parallel to _strlit_rows: stores the _literal_rows index for each
    # string literal, so Phase 2 can look up the correct literal_id.
    _strlit_literal_indices = []
    _comment_rows = []    # rows for executemany("INSERT INTO comments_freeform")
    # Track literal_id back-references: (token_seq, literal_kind, literal_row_idx)
    _literal_backrefs = []
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

            # Refine literal kinds — collect into lists (no DB writes)
            literal_placeholder = None  # will be replaced after executemany
            if db_kind == "literal":
                refined = _refine_literal_kind(spelling)
                if refined == "string_literal":
                    db_kind = "string_literal"
                    sec_flags = _compute_security_flags(spelling)
                    _literal_rows.append(
                        ("string", spelling, None)  # token_id filled later
                    )
                    _strlit_literal_indices.append(len(_literal_rows) - 1)
                    _strlit_rows.append(
                        (spelling.encode("utf-8"),
                         _decode_string_literal(spelling),
                         "utf-8" if not spelling.startswith("L") else "wide",
                         1 if spelling.startswith("L") else 0,
                         sec_flags,
                         None)  # token_id filled later
                    )
                    _literal_backrefs.append((seq, "string", len(_literal_rows) - 1))
                    stats["string_literals"] += 1
                elif refined == "char_literal":
                    db_kind = "char_literal"
                    _literal_rows.append(("char", spelling, None))
                    _literal_backrefs.append((seq, "char", len(_literal_rows) - 1))
                elif refined == "int_literal":
                    db_kind = "int_literal"
                    _literal_rows.append(
                        ("int", spelling, spelling, 10,
                         _extract_literal_suffix(spelling), None)
                    )
                    _literal_backrefs.append((seq, "int", len(_literal_rows) - 1))
                elif refined == "float_literal":
                    db_kind = "float_literal"
                    _literal_rows.append(
                        ("float", spelling, spelling,
                         _extract_literal_suffix(spelling), None)
                    )
                    _literal_backrefs.append((seq, "float", len(_literal_rows) - 1))
                stats["literals"] += 1

            # Collect token row
            _token_rows.append(
                (file_id, seq, db_kind, spelling, line, col, end_line, end_col,
                 start_off, end_off - start_off, preceding_ws,
                 literal_placeholder, 'c')
            )

            # Handle comments
            if tok_kind_name == "COMMENT":
                comment_kind = _classify_comment_kind(spelling)
                _comment_rows.append(
                    (file_id, line, end_line, col, end_col,
                     spelling, comment_kind, None, 'c')
                )
                stats["comments"] += 1

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

    # Collect PP info into lists (walk cursors + pp_branches)
    _macro_rows = []
    _macro_inv_rows = []
    _pp_directive_rows = []
    _pragma_rows = []
    _pp_branch_rows = []
    _collect_pp_info(tu.cursor, file_path, file_id, source_bytes,
                     _macro_rows, _macro_inv_rows, _pp_directive_rows,
                     _pragma_rows)
    _collect_pp_branch_tree(file_id, source_bytes, _pp_branch_rows)
    stats["macros"] = len(_macro_rows)
    stats["macro_invocations"] = len(_macro_inv_rows)
    stats["pp_directives"] = len(_pp_directive_rows)
    stats["pragmas"] = len(_pragma_rows)
    stats["attributes"] = 0  # attributes require ast_node_id (L2), not populated in L1
    stats["pp_branches"] = len(_pp_branch_rows)

    # Compute trailing_whitespace
    trailing_ws = ""
    if prev_end_offset < len(source_bytes):
        trailing_ws = source_bytes[prev_end_offset:].decode("utf-8", errors="replace")

    # ── Phase 2: DB writes (minimize write lock hold time) ──
    # All DB writes are batched and committed in a single transaction.
    # The write lock is held only for the duration of these writes, NOT
    # during the CPU-intensive parsing phase above.

    # Upsert source_files_meta
    conn.execute(
        "INSERT INTO source_files_meta (file_id, encoding, line_ending, "
        "has_bom, disk_sha256) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(file_id) DO UPDATE SET encoding=?, line_ending=?, "
        "has_bom=?, disk_sha256=?",
        (file_id, encoding, line_ending, int(has_bom), disk_sha,
         encoding, line_ending, int(has_bom), disk_sha)
    )

    # Batch INSERT tokens
    if _token_rows:
        conn.executemany(
            "INSERT INTO tokens (file_id, seq, kind, spelling, line, col, "
            "end_line, end_col, byte_offset, byte_length, "
            "preceding_whitespace, literal_id, language) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _token_rows
        )

    # Batch INSERT literals + resolve back-references
    if _literal_rows:
        # For string literals: (kind, raw_text, token_id)
        # For char literals: (kind, raw_text, token_id)
        # For int literals: (kind, value, raw_text, base, suffix, token_id)
        # For float literals: (kind, value, raw_text, suffix, token_id)
        # We need to INSERT them in the same order and get lastrowid for each.
        # executemany doesn't return per-row IDs, so we fall back to
        # individual INSERTs for literals (typically few per file).
        _literal_ids = {}  # row_idx → literal_id
        for _li, _lrow in enumerate(_literal_rows):
            if _lrow[0] == "string":
                cur = conn.execute(
                    "INSERT INTO literals (kind, raw_text, token_id) "
                    "VALUES (?, ?, NULL)",
                    (_lrow[0], _lrow[1])
                )
            elif _lrow[0] == "char":
                cur = conn.execute(
                    "INSERT INTO literals (kind, raw_text, token_id) "
                    "VALUES (?, ?, NULL)",
                    (_lrow[0], _lrow[1])
                )
            elif _lrow[0] == "int":
                cur = conn.execute(
                    "INSERT INTO literals (kind, value, raw_text, base, suffix, token_id) "
                    "VALUES (?, ?, ?, ?, ?, NULL)",
                    (_lrow[0], _lrow[1], _lrow[2], _lrow[3], _lrow[4])
                )
            elif _lrow[0] == "float":
                cur = conn.execute(
                    "INSERT INTO literals (kind, value, raw_text, suffix, token_id) "
                    "VALUES (?, ?, ?, ?, NULL)",
                    (_lrow[0], _lrow[1], _lrow[2], _lrow[3])
                )
            else:
                continue
            _literal_ids[_li] = cur.lastrowid

        # Batch INSERT string_literals
        if _strlit_rows:
            for _si, _srow in enumerate(_strlit_rows):
                _lidx = _strlit_literal_indices[_si]
                _lid = _literal_ids.get(_lidx)
                if _lid is not None:
                    # _srow is (raw_bytes, decoded, encoding, is_wide,
                    #           security_flags, token_id_placeholder)
                    conn.execute(
                        "INSERT INTO string_literals "
                        "(literal_id, raw_bytes, decoded, encoding, is_wide, "
                        "security_flags, token_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                        (_lid, _srow[0], _srow[1], _srow[2],
                         _srow[3], _srow[4])
                    )

    # Resolve literal_id back-references in tokens
    if _literal_backrefs:
        # Build a map from seq → token_id for this file
        _seq_to_token_id = {}
        for _trow in conn.execute(
            "SELECT id, seq FROM tokens WHERE file_id=?", (file_id,)
        ):
            _seq_to_token_id[_trow[1]] = _trow[0]

        for _tseq, _lkind, _lidx in _literal_backrefs:
            _lid = _literal_ids.get(_lidx)
            if _lid is not None:
                _tok_id = _seq_to_token_id.get(_tseq)
                conn.execute(
                    "UPDATE tokens SET literal_id=? WHERE file_id=? AND seq=?",
                    (_lid, file_id, _tseq)
                )
                # Set literals.token_id back-reference
                if _tok_id is not None:
                    conn.execute(
                        "UPDATE literals SET token_id=? WHERE id=?",
                        (_tok_id, _lid)
                    )
                # Set string_literals.token_id back-reference
                if _lkind == "string" and _tok_id is not None:
                    conn.execute(
                        "UPDATE string_literals SET token_id=? "
                        "WHERE literal_id=?",
                        (_tok_id, _lid)
                    )

    # Batch INSERT comments
    if _comment_rows:
        conn.executemany(
            "INSERT INTO comments_freeform (file_id, line, end_line, col, end_col, "
            "text, kind, attached_symbol_id, language) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _comment_rows
        )

    # Batch INSERT macros, macro_invocations, pp_directives, pragmas
    if _macro_rows:
        conn.executemany(
            "INSERT INTO macros (name, file_id, line, col, is_function_like, "
            "is_variadic, params, body_text, is_undef, language) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _macro_rows
        )
    if _macro_inv_rows:
        # Resolve macro_id by name from just-inserted macros for this file
        _macro_name_to_id = {}
        for _mrow in conn.execute(
            "SELECT id, name FROM macros WHERE file_id=? ORDER BY id",
            (file_id,)
        ):
            _macro_name_to_id[_mrow[1]] = _mrow[0]  # last wins for duplicates
        for _inv_file_id, _inv_name, _inv_line, _inv_col in _macro_inv_rows:
            _mid = _macro_name_to_id.get(_inv_name)
            if _mid is not None:
                conn.execute(
                    "INSERT INTO macro_invocations (macro_id, file_id, line, col, "
                    "arg_token_ids, expanded_text) VALUES (?, ?, ?, ?, '[]', NULL)",
                    (_mid, _inv_file_id, _inv_line, _inv_col)
                )
    if _pp_directive_rows:
        conn.executemany(
            "INSERT INTO pp_directives (file_id, kind, line, col, raw_text, "
            "parsed_payload) VALUES (?, ?, ?, ?, ?, ?)",
            _pp_directive_rows
        )
    if _pragma_rows:
        conn.executemany(
            "INSERT INTO pragmas (file_id, line, col, pragma_kind, raw_text, "
            "parsed_payload) VALUES (?, ?, ?, ?, ?, ?)",
            _pragma_rows
        )

    # Batch INSERT pp_branches (need parent_id resolution)
    if _pp_branch_rows:
        # pp_branches have parent_id which references other pp_branches rows.
        # We need to INSERT in order and track IDs.
        _branch_ids = []
        for _brow in _pp_branch_rows:
            _parent_id = _branch_ids[_brow[1]] if _brow[1] is not None and _brow[1] < len(_branch_ids) else None
            cur = conn.execute(
                "INSERT INTO pp_branches (file_id, parent_id, kind, condition, "
                "start_line, end_line, is_active, language) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (file_id, _parent_id, _brow[2], _brow[3],
                 _brow[4], _brow[5], _brow[6], _brow[7])
            )
            _branch_ids.append(cur.lastrowid)

    # Update trailing_whitespace on source_files_meta
    if trailing_ws:
        conn.execute(
            "UPDATE source_files_meta SET trailing_whitespace=? WHERE file_id=?",
            (trailing_ws, file_id)
        )

    # Commit all writes in one transaction
    conn.commit()

    # RPT-P0-21: L1↔L2 alignment — link identifier tokens to cgdb_nodes
    # This runs AFTER the main commit so it doesn't hold the write lock
    # during the CPU-intensive parsing phase.
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
        if linked:
            conn.commit()
    except Exception as _align_exc:
        # Best-effort — alignment is a P0-21 enhancement, not a correctness
        # requirement for L1 ingest itself.
        stats["l1_l2_linked"] = 0
        import sys as _sys
        print(f"[l1] WARNING: L1↔L2 alignment failed for "
              f"{os.path.basename(file_path)}: {_align_exc}", file=_sys.stderr)

    # Verify consistency: render from tokens and compare to disk
    from _builder.source_renderer import SourceRenderer
    renderer = SourceRenderer(conn, source_root=source_root or "")
    cr = renderer.verify_consistency(file_id)
    stats["rendered_sha256"] = cr.db_sha256
    stats["consistency_ok"] = cr.ok

    return stats


def _ingest_l1_fallback(
    conn: sqlite3.Connection,
    file_path: str,
    file_id: int,
    commit_hash: str,
    stats: dict,
    source_root: str = "",
) -> dict:
    """Fallback path when libclang is not available.

    Reads the source file as bytes, computes sha256, populates source_files_meta
    with encoding/line_ending/has_bom/disk_sha256. Does NOT populate tokens
    table — L1 queries will return empty (or use the disk-read fallback in
    source_renderer.render()).
    """
    # Resolve relative/moved paths before reading (same logic as the
    # libclang path in ingest_l1).
    resolved_path = _resolve_source_path(file_path, source_root or "")
    if resolved_path != file_path:
        file_path = resolved_path
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


def _collect_pp_info(cursor, file_path, file_id, source_bytes,
                    _macro_rows, _macro_inv_rows, _pp_directive_rows,
                    _pragma_rows):
    """Walk cursor tree for MACRO_DEFINITION / MACRO_INSTANTIATION /
    INCLUDE_DIRECTIVE / PREPROCESSING_DIRECTIVE — collect data into lists
    for later batch INSERT.

    This is the two-phase (collect-then-write) version of
    _walk_cursors_for_pp_info: no DB writes happen here; all data is
    appended to the supplied lists.

    Row formats match the actual cgdb_schema.py definitions:
      _macro_rows:     (name, file_id, line, col, is_function_like,
                        is_variadic, params, body_text, is_undef, language)
      _macro_inv_rows: (file_id, name, line, col)
                        macro_id resolved in Phase 2 after macros are inserted
      _pp_directive_rows: (file_id, kind, line, col, raw_text, parsed_payload)
      _pragma_rows:    (file_id, line, col, pragma_kind, raw_text, parsed_payload)
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
                name = c.spelling or ""
                is_function_like = _is_macro_function_like(c, source_bytes)
                params, body_text = _extract_macro_params_and_body(c, source_bytes)
                line = c.location.line
                col = c.location.column
                _macro_rows.append(
                    (name, file_id, line, col, int(is_function_like),
                     int(name.endswith("...")),
                     str(params) if params else "[]",
                     body_text or "", 0, 'c')
                )

            elif kind == CursorKind.MACRO_INSTANTIATION:
                name = c.spelling or ""
                line = c.location.line
                col = c.location.column
                # macro_id resolved in Phase 2 after macros are inserted
                _macro_inv_rows.append((file_id, name, line, col))

            elif kind == CursorKind.INCLUDE_DIRECTIVE:
                line = c.location.line
                col = c.location.column
                included_path = c.spelling or ""
                _pp_directive_rows.append(
                    (file_id, 'include', line, col,
                     f"#include {included_path}",
                     f'{{"path":"{included_path}"}}')
                )

            elif kind == CursorKind.PREPROCESSING_DIRECTIVE:
                line = c.location.line
                col = c.location.column
                raw = _extract_cursor_text(c, source_bytes)
                if raw and raw.strip().startswith("#"):
                    directive_info = _split_pp_directive(raw)
                    if directive_info:
                        d_kind, d_rest = directive_info
                        if d_kind == "pragma":
                            p_kind = d_rest.split()[0] if d_rest.split() else "unknown"
                            # Collect pragma row (pragmas table)
                            _pragma_rows.append(
                                (file_id, line, col, p_kind, raw, '{}')
                            )
                            # Also add to pp_directives (kind='pragma')
                            _pp_directive_rows.append(
                                (file_id, 'pragma', line, col, raw, '{}')
                            )
                        else:
                            _pp_directive_rows.append(
                                (file_id, d_kind, line, col, raw, '{}')
                            )

        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue


def _collect_pp_branch_tree(file_id, source_bytes, _pp_branch_rows):
    """Build pp_branches data from regex on source lines.

    Walks the source code line by line, detects #ifdef/#ifndef/#if/#elif/
    #else/#endif, and appends rows to _pp_branch_rows.  parent_id is stored
    as a stack index that will be resolved to actual DB IDs during batch INSERT.

    Row format: (file_id, parent_idx, kind, condition,
                  start_line, end_line, is_active, language)
    - parent_idx: index into _pp_branch_rows (or None for root), resolved
      to real parent_id in Phase 2.
    """
    text = source_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=False)

    stack = []  # stack of row indices into _pp_branch_rows

    for line_idx, line in enumerate(lines, start=1):
        directive = _split_pp_directive(line)
        if directive is None:
            continue
        d_kind, d_rest = directive
        parent_idx = stack[-1] if stack else None

        if d_kind in ("ifdef", "ifndef", "if"):
            row_idx = len(_pp_branch_rows)
            _pp_branch_rows.append(
                (file_id, parent_idx, d_kind, d_rest, line_idx, 0, 1, 'c')
            )
            stack.append(row_idx)

        elif d_kind == "elif":
            if stack:
                prev_idx = stack.pop()
                # Update end_line of the previous branch
                _prev = _pp_branch_rows[prev_idx]
                _pp_branch_rows[prev_idx] = (
                    _prev[0], _prev[1], _prev[2], _prev[3],
                    _prev[4], line_idx - 1, _prev[6], _prev[7]
                )
                parent_idx = stack[-1] if stack else None
                row_idx = len(_pp_branch_rows)
                _pp_branch_rows.append(
                    (file_id, parent_idx, "elif", d_rest, line_idx, 0, 1, 'c')
                )
                stack.append(row_idx)

        elif d_kind == "else":
            if stack:
                prev_idx = stack.pop()
                _prev = _pp_branch_rows[prev_idx]
                _pp_branch_rows[prev_idx] = (
                    _prev[0], _prev[1], _prev[2], _prev[3],
                    _prev[4], line_idx - 1, _prev[6], _prev[7]
                )
                parent_idx = stack[-1] if stack else None
                row_idx = len(_pp_branch_rows)
                _pp_branch_rows.append(
                    (file_id, parent_idx, "else", "", line_idx, 0, 1, 'c')
                )
                stack.append(row_idx)

        elif d_kind == "endif":
            if stack:
                prev_idx = stack.pop()
                _prev = _pp_branch_rows[prev_idx]
                _pp_branch_rows[prev_idx] = (
                    _prev[0], _prev[1], _prev[2], _prev[3],
                    _prev[4], line_idx, _prev[6], _prev[7]
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
