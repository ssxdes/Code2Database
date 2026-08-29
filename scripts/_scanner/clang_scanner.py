"""ClangScanner — libclang 17-based AST extraction for cgdb (code graph database).

Per cgdb-architecture-and-poc-report.md Phase 1 (L1-L2 + FTS5):
- Walks clang AST via libclang Python bindings
- Extracts multi-kind nodes: function, method, var, parm, field, struct, enum, typedef, decl_ref, member_ref
- Extracts type records into cgdb_types (independent type system)
- Extracts INVOKES / HAS_FIELD / READS / WRITES edges
- Produces cgdb_nodes / cgdb_types / cgdb_edges / cgdb_invoke_sites lists
  (alongside the legacy functions/edges shape used by tree-sitter scanners)

Coexists with CTreeSitterScanner via DualBackendScanner (dual_scanner.py).
Falls back gracefully if libclang is unavailable — returns empty cgdb_* lists.

Node ID strategy (per cgdb 5.6): int(sha256(usr)[:16], 16) for AST nodes,
with bit-range prefix to keep AST nodes < 0x8000_0000_0000_0000.
"""
import hashlib
import os

from _scanner.base import BaseScanner
import logging


# Try importing libclang; keep going if unavailable (caller handles empty output).
try:
    import clang.cindex as _ci
    _LIBCLANG_AVAILABLE = True
except ImportError:
    _ci = None
    _LIBCLANG_AVAILABLE = False


# Try to locate libclang.so — distribution-dependent.
# Includes both clang-17 and clang-18 paths for portability.
_LIBCLANG_PATHS = [
    # clang-18 (Ubuntu 24.04 / Debian 13)
    '/usr/lib/x86_64-linux-gnu/libclang-18.so.1',
    '/usr/lib/x86_64-linux-gnu/libclang-18.so.18',
    '/usr/lib/llvm-18/lib/libclang.so.1',
    '/usr/lib/llvm-18/lib/libclang-18.so.1',
    # clang-17 (Ubuntu 24.04 / Debian 12)
    '/usr/lib64/libclang.so.17',
    '/usr/lib64/libclang.so',
    '/usr/lib/llvm/17/lib/libclang.so.17',
    '/usr/lib/llvm/17/lib/libclang.so',
    '/usr/lib/x86_64-linux-gnu/libclang-17.so.1',
    '/usr/lib/aarch64-linux-gnu/libclang-17.so.1',
    '/usr/lib/libclang.so.17',
    '/usr/lib/libclang.so',
    # Generic fallback (symlink without version)
    '/usr/lib/x86_64-linux-gnu/libclang.so',
]


def _configure_libclang():
    """Configure libclang library path. Idempotent."""
    if not _LIBCLANG_AVAILABLE:
        return False
    # If a library file is already configured, don't override.
    if getattr(_ci.Config, 'library_file', None):
        return True
    for path in _LIBCLANG_PATHS:
        if os.path.exists(path):
            try:
                _ci.Config.set_library_file(path)
                return True
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                continue
    return True


# Node ID bit-range prefixes (per cgdb 5.6).
# AST nodes use 0x0000..0x7FFF (no prefix needed, just hash truncation).
_AST_NODE_MASK = 0x7FFF_FFFF_FFFF_FFFF

# Module-level libclang Index singleton.
# Creating a new Index per scan_file is wasteful — the Index is a
# long-lived object that manages translation-unit caching internally.
_clang_index = None


def _get_clang_index():
    """Return the process-level libclang Index singleton."""
    global _clang_index
    if _clang_index is None:
        _clang_index = _ci.Index.create()
    return _clang_index


def cgdb_node_id(usr: str, fallback: str = "") -> int:
    """Compute a stable cross-TU node ID from a clang USR.

    Per cgdb 5.6: int(sha256(usr)[:16], 16) for AST nodes (high bit clear).
    Falls back to sha256(fallback) if USR is empty.
    """
    src = usr if usr else fallback
    if not src:
        return 0
    h = hashlib.sha256(src.encode('utf-8')).hexdigest()[:16]
    return int(h, 16) & _AST_NODE_MASK


# GCC-specific -W* / -Wno-* flags that clang doesn't recognize.
# When compile_commands.json comes from a GCC build (e.g. Linux kernel's Makefiles),
# these emit `warning: unknown warning option '-Wno-foo' [-Wunknown-warning-option]`
# on every TU. Strip them at the compile-db cache boundary so the filter runs once
# per entry instead of per parse.
#
# Listed as exact-match tokens; tokens with `=`-suffix args (e.g. `-Wimplicit-fallthrough=5`)
# are matched by prefix.
_GCC_ONLY_WARNING_FLAG_PREFIXES = (
    '-Wno-packed-not-aligned',
    '-Wpacked-not-aligned',
    '-Wno-format-overflow',
    '-Wno-format-truncation',
    '-Wno-stringop-overflow',
    '-Wno-stringop-truncation',
    '-Wstringop-truncation',
    '-Wno-maybe-uninitialized',
    '-Wno-dangling-pointer',
    '-Wno-alloc-size-larger-than',
    '-Wno-restrict',
    '-Werror=designated-init',
    '-Wimplicit-fallthrough=',
)

# Exact-match GCC-only flags (no `=` suffix, no param). Mostly -Wno-* variants
# that clang's diagnostics don't know about.
_GCC_ONLY_WARNING_FLAG_EXACT = frozenset(
    flag for flag in _GCC_ONLY_WARNING_FLAG_PREFIXES
    if '=' not in flag
)
# Prefix-match GCC-only flags (the `=` variants, matched by prefix).
_GCC_ONLY_WARNING_FLAG_PREFIX_ONLY = tuple(
    flag for flag in _GCC_ONLY_WARNING_FLAG_PREFIXES
    if '=' in flag
)


def _is_gcc_only_warning_flag(tok: str) -> bool:
    """True if tok is a GCC-specific -W* flag clang won't recognize."""
    if tok in _GCC_ONLY_WARNING_FLAG_EXACT:
        return True
    for prefix in _GCC_ONLY_WARNING_FLAG_PREFIX_ONLY:
        if tok.startswith(prefix):
            return True
    return False


def _filter_gcc_only_warning_flags(args):
    """Strip GCC-specific -W* flags clang doesn't understand.

    Applied to compile_commands.json args (which come from a GCC build) and to
    user-supplied extra_args. Keeps clang-recognized -W* flags intact.
    """
    if not args:
        return args
    return [tok for tok in args if not _is_gcc_only_warning_flag(tok)]


# Map clang CursorKind → cgdb_nodes.kind values (per cgdb_schema CHECK constraint).
_CURSOR_KIND_MAP = {
    # function-like
    'FUNCTION_DECL': 'function',
    'CXX_METHOD': 'method',
    'CONSTRUCTOR': 'constructor',
    'DESTRUCTOR': 'destructor',
    # variables
    'VAR_DECL': 'var',
    'PARM_VAR_DECL': 'parm',
    'FIELD_DECL': 'field',
    'ENUM_CONSTANT_DECL': 'enum_constant',
    'TYPEDEF_DECL': 'typedef',
    # types (as nodes when they're also symbols)
    'STRUCT_DECL': 'struct',
    'CLASS_DECL': 'class',
    'UNION_DECL': 'union',
    'ENUM_DECL': 'enum',
    # expressions / references
    'DECL_REF_EXPR': 'decl_ref',
    'MEMBER_REF_EXPR': 'member_ref',
    'LABEL_STMT': 'label',
    'NAMESPACE': 'namespace',
    # macros / includes
    'MACRO_DEFINITION': 'macro',
    'MACRO_INSTANTIATION': 'macro',
    'INCLUDE_DIRECTIVE': 'include',
    # future-proof
    'TEMPLATE_TYPE_PARAMETER': 'template',
    'CONCEPT_DECL': 'concept',
    'USING_DIRECTIVE': 'include',
}


def _kind_from_cursor(cursor) -> str:
    """Map a clang CursorKind to a cgdb_nodes.kind string."""
    kind_name = cursor.kind.name if cursor.kind else ''
    return _CURSOR_KIND_MAP.get(kind_name, 'var')  # default to var for unknown


class ClangScanner(BaseScanner):
    """libclang 17-based scanner for C/C++ — produces cgdb records.

    Does NOT replace CTreeSitterScanner — produces complementary cgdb_nodes/
    cgdb_types/cgdb_edges lists that the builder writes to cgdb tables.

    The scan_file() return dict has the standard BaseScanner shape (functions/
    edges/globals/...) PLUS three extra keys:
      - cgdb_nodes: list of dicts with {id, kind, name, fqn, file_id, line, col,
                    byte_start, byte_end, type_spelling, signature, body_text, attrs}
      - cgdb_types: list of dicts with {id, spelling, canonical_spelling, kind, ...}
      - cgdb_edges: list of dicts with {src_id, dst_id, kind, file_id, line, col, attrs}
      - cgdb_invoke_sites: list of dicts with {invoker_id, invoked_id, invoke_kind, ...}
    """

    def __init__(self, is_cpp: bool = False, extra_clang_args: list = None,
                 compile_commands_path: str = None):
        self.is_cpp = is_cpp
        self._extra_args = extra_clang_args or []
        self._compile_commands_path = compile_commands_path or ''
        # Per-file compile args cache: filepath → list of clang args
        # Populated lazily from compile_commands.json on first lookup.
        self._compile_db_cache: dict = {}
        self._compile_db_loaded = False
        # Fallback directory-level args (when no per-file entry exists):
        # directory prefix → list of clang args (longest-prefix match).
        self._compile_db_dir_cache: dict = {}
        self._tu = None  # holds the TranslationUnit for the current scan
        self._file_id = 0  # set by caller via cgdb_ingest
        self._macro_bindings = {}
        self._callback_patterns = {}
        self._api_prefixes = []
        self._export_macros = []
        self._struct_op_types = []
        self._macro_dispatch_patterns = {}

    def _parse(self, source_bytes: bytes):
        """Parse source with libclang and return the TranslationUnit.

        Note: this overrides BaseScanner._parse but the return value is a
        TranslationUnit, not a tree-sitter Tree. _extract handles it.
        """
        if not _configure_libclang():
            return None
        # Caller writes source to a temp file before scan_file, so we get a path.
        # But BaseScanner.scan_file() reads bytes and calls _parse(bytes).
        # We need the file path for clang parsing, so we'll handle parse in
        # scan_file itself via _parse_path, and make _parse a no-op fallback.
        return None

    def _load_compile_commands(self):
        """Load and cache compile_commands.json entries.

        Schema (Clang Compilation Database):
          [
            {
              "directory": "/abs/build/dir",
              "command": "clang -c -I/foo -DCONFIG_X=1 file.c -o file.o",
              "file": "/abs/path/to/file.c",
              "arguments": ["clang", "-c", "-I/foo", "-DCONFIG_X=1", "file.c", "-o", "file.o"],
              "output": "file.o"  # optional
            },
            ...
          ]

        Populates:
          self._compile_db_cache: file_path (abs) → list of clang args
          self._compile_db_dir_cache: dir prefix → list of clang args
        """
        if self._compile_db_loaded:
            return
        self._compile_db_loaded = True
        if not self._compile_commands_path:
            return
        if not os.path.isfile(self._compile_commands_path):
            return
        import json as _json
        try:
            with open(self._compile_commands_path, 'r', encoding='utf-8') as f:
                entries = _json.load(f)
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            return
        if not isinstance(entries, list):
            return
        for entry in entries:
            try:
                file_path = entry.get('file', '') or ''
                if not file_path:
                    continue
                file_path = os.path.abspath(file_path)
                directory = entry.get('directory', '') or ''
                # Prefer 'arguments' (list form, no shell splitting needed);
                # fall back to 'command' (string form, requires splitting).
                args_list = entry.get('arguments')
                if not args_list:
                    cmd_str = entry.get('command', '') or ''
                    if cmd_str:
                        import shlex
                        args_list = shlex.split(cmd_str)
                if not args_list:
                    continue
                # Strip the compiler invocation (first token like 'clang',
                # 'gcc', 'cc', 'c++') and the source file path itself.
                # Also strip '-o <output>' and other flags irrelevant to
                # libclang parsing.
                clang_args = []
                skip_next = False
                for i, tok in enumerate(args_list):
                    if skip_next:
                        skip_next = False
                        continue
                    if i == 0:
                        # Compiler name — skip
                        continue
                    if tok == file_path or tok == os.path.basename(file_path):
                        continue
                    # Output/dependency-generation flags — irrelevant for parsing
                    if tok == '-o':
                        skip_next = True
                        continue
                    if tok.startswith('-o'):
                        continue
                    if tok == '-c':
                        continue
                    # Dependency file generation — keep libclang happy
                    if tok in ('-MMD', '-MP', '-M', '-MM', '-MD', '-MMD-phony'):
                        continue
                    if tok in ('-MF', '-MT', '-MQ'):
                        skip_next = True
                        continue
                    if tok.startswith('-MF') or tok.startswith('-MT') or tok.startswith('-MQ'):
                        continue
                    # Shell command separators (shouldn't appear, but be defensive)
                    if tok in ('&&', ';', '|', '||', '&'):
                        break  # stop here — anything after is post-link
                    if tok.startswith('-M'):
                        continue
                    # GCC-specific tuning flags libclang doesn't understand
                    if tok == '-moutline-atomics':
                        continue
                    if tok == '-mno-outline-atomics':
                        continue
                    # -march=native is host-specific and may fail in libclang;
                    # keep -march=<arch> with explicit arch, drop 'native'
                    if tok == '-march=native' or tok == '-mtune=native':
                        continue
                    # -fstack-protector-strong is fine for libclang (it ignores
                    # codegen-only flags), but -fstack-protector* with -fno-PIE
                    # combinations sometimes cause issues. Keep by default.
                    # GCC-only -W* / -Wno-* flags (kernel Makefile uses many) —
                    # strip at cache time so clang doesn't emit
                    # `[-Wunknown-warning-option]` warnings on every TU.
                    if _is_gcc_only_warning_flag(tok):
                        continue
                    clang_args.append(tok)
                if clang_args:
                    self._compile_db_cache[file_path] = clang_args
                    if directory:
                        dir_abs = os.path.abspath(directory)
                        # Longest-prefix match: only set if not already a longer prefix
                        existing = self._compile_db_dir_cache.get(dir_abs)
                        if existing is None or len(existing) < len(clang_args):
                            self._compile_db_dir_cache[dir_abs] = clang_args
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                continue
    def _lookup_compile_args(self, filepath: str) -> list:
        """Look up per-file compile args from compile_commands.json.

        Returns a list of clang args, or [] if no entry matches. Falls back
        to directory-level args via longest-prefix match.
        """
        if not self._compile_commands_path:
            return []
        self._load_compile_commands()
        try:
            file_abs = os.path.abspath(filepath)
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            return []
        # Direct file match
        if file_abs in self._compile_db_cache:
            return list(self._compile_db_cache[file_abs])
        # Directory longest-prefix match
        best_prefix = ''
        best_args = []
        for dir_prefix, args in self._compile_db_dir_cache.items():
            if file_abs.startswith(dir_prefix + os.sep) or file_abs == dir_prefix:
                if len(dir_prefix) > len(best_prefix):
                    best_prefix = dir_prefix
                    best_args = args
        return list(best_args) if best_args else []

    def _parse_path(self, filepath: str):
        """Parse source file by path with libclang. Returns TranslationUnit or None."""
        if not _configure_libclang():
            return None
        # Per-file compile args from compile_commands.json take precedence
        # (they include -I, -D, -std, etc. that match the project's build).
        # Note: _load_compile_commands already strips GCC-only -W* flags at cache
        # time, so file_args should be clean. The belt-and-suspenders filter
        # below handles any user-supplied -W* flags in _extra_args.
        file_args = self._lookup_compile_args(filepath)
        args = []
        if file_args:
            # Use compile_commands args as the base; ensure language is set
            # (some entries omit -x; libclang defaults to C, which is wrong for .cpp)
            args += file_args
            if not any(a.startswith('-x') for a in args):
                if self.is_cpp:
                    args = ['-x', 'c++'] + args
                else:
                    args = ['-x', 'c'] + args
        else:
            # Fallback: defaults + system includes + extra args from CLI
            if self.is_cpp:
                args += ['-x', 'c++', '-std=c++17']
            else:
                args += ['-x', 'c']
            # Standard system includes — clang 17 default search paths.
            for inc in ('/usr/local/include', '/usr/include',
                        '/usr/lib64/clang/17/include',
                        '/usr/lib/clang/17/include'):
                if os.path.isdir(inc):
                    args += ['-isystem', inc]
            # User-supplied extra args may include GCC-only -W* flags (e.g. when
            # the CLI is invoked with the project's CFLAGS). Strip them so clang
            # doesn't emit `[-Wunknown-warning-option]` warnings.
            args += _filter_gcc_only_warning_flags(self._extra_args)
        # Belt-and-suspenders: tell clang to silently drop any -W* flag it
        # doesn't recognize. This catches GCC-only flags we haven't enumerated
        # (the denylist above is conservative and only covers flags observed in
        # the wild on Linux kernel / glibc / busybox builds).
        args = ['-Wno-unknown-warning-option'] + args
        try:
            idx = _get_clang_index()
            tu = idx.parse(filepath, args=args,
                           options=_ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            return None
        # Check for fatal diagnostics — return None if parse totally failed.
        for d in tu.diagnostics:
            if d.severity >= _ci.Diagnostic.Fatal:
                # Fatal doesn't always mean unusable; keep going but log.
                break
        return tu

    def _extract(self, tree, source_bytes: bytes, filepath: str,
                 source_root: str, domain: str):
        """Extract from a libclang TranslationUnit. Returns (functions, edges, extra).

        The 'tree' arg here is actually a TranslationUnit (set by scan_file).
        Returns empty functions/edges (legacy shape) — cgdb output is stored
        on self._cgdb_* and read by scan_file to populate the result dict.
        """
        # This path is not used — scan_file calls _extract_from_tu directly.
        return [], [], []

    def _extract_from_tu(self, tu, filepath: str, source_root: str, domain: str) -> dict:
        """Extract cgdb records from a parsed TranslationUnit.

        Returns dict with cgdb_nodes / cgdb_types / cgdb_edges / cgdb_invoke_sites.
        Also returns minimal legacy 'functions'/'edges' so this scanner can
        be used standalone (without tree-sitter) if needed.
        """
        cgdb_nodes = []
        cgdb_types = []
        cgdb_edges = []
        cgdb_invoke_sites = []
        legacy_functions = []
        legacy_edges = []
        # De-dup node IDs across this TU.
        seen_node_ids = set()

        # Read source bytes once for snippet extraction (avoids re-reading
        # the file per node). Falls back to empty bytes on I/O error.
        _source_bytes = b''
        try:
            with open(filepath, 'rb') as _f:
                _source_bytes = _f.read()
        except (IOError, OSError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        _SNIPPET_MAX = 4096

        def _snippet(byte_start: int, byte_end: int) -> str:
            if not byte_start or not byte_end or byte_end <= byte_start:
                return ''
            if byte_start < 0 or byte_end > len(_source_bytes):
                return ''
            text = _source_bytes[byte_start:byte_end].decode(
                'utf-8', errors='replace'
            )
            if len(text) > _SNIPPET_MAX:
                text = text[:_SNIPPET_MAX] + '…'
            return text

        def add_node(cursor, kind_override=None,
                     enclosing_func_id: int = 0) -> int:
            """Insert a node for the cursor, return its ID. De-dups by ID.

            enclosing_func_id is the node_id of the enclosing FunctionDecl,
            derived via cursor.semantic_parent walk (per doc 5.4.2). 0 means
            the node is at file/translation-unit scope (no enclosing function).
            """
            usr = cursor.get_usr() or ''
            loc = cursor.location
            file_name = loc.file.name if loc.file else ''
            # For cursors without USR, fall back to file:byte_offset.
            fallback = f"{file_name}:{cursor.extent.start.offset}" if file_name else ""
            nid = cgdb_node_id(usr, fallback)
            if nid in seen_node_ids:
                return nid
            seen_node_ids.add(nid)
            kind = kind_override or _kind_from_cursor(cursor)
            type_spelling = ''
            try:
                if cursor.type and cursor.type.spelling:
                    type_spelling = cursor.type.spelling
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
            signature = ''
            body_text = ''
            if kind == 'function':
                # Signature: cursor type + name + params (clang gives this via displayname)
                try:
                    signature = cursor.displayname or ''
                except Exception:
                    signature = ''
                # Body text: get tokens between braces if available
                try:
                    if cursor.is_definition():
                        body_text = self._extract_function_body(cursor)
                except Exception:
                    body_text = ''
            # enclosing_symbol_id derivation per cgdb-architecture doc 5.4.2:
            # if caller didn't pass an enclosing_func_id, walk semantic_parent
            # to find the enclosing FunctionDecl. This is O(1) and precise
            # (clang native advantage vs. KGraph's range matching).
            if not enclosing_func_id and kind != 'function':
                try:
                    parent = cursor.semantic_parent
                    while parent is not None:
                        if parent.kind and parent.kind.name == 'FUNCTION_DECL':
                            # Recursively call add_node to ensure the parent
                            # function has a node entry; pass kind_override
                            # to avoid double-walking.
                            enclosing_func_id = add_node(
                                parent, kind_override='function'
                            )
                            break
                        parent = parent.semantic_parent
                except Exception:
                    enclosing_func_id = 0
            _bs = cursor.extent.start.offset if cursor.extent else 0
            _be = cursor.extent.end.offset if cursor.extent else 0
            cgdb_nodes.append({
                'id': nid,
                'kind': kind,
                'name': cursor.spelling or '',
                'fqn': usr or fallback,
                'file_path': file_name,
                'line': loc.line or 0,
                'col': loc.column or 0,
                'byte_start': _bs,
                'byte_end': _be,
                'type_spelling': type_spelling,
                'signature': signature,
                'body_text': body_text,
                'enclosing_symbol_id': enclosing_func_id,
                'attrs': {},
                'source_snippet': _snippet(_bs, _be),
            })
            return nid

        # Set-based type de-duplication.
        _seen_type_ids: set = set()

        def add_type(cursor) -> int:
            """Insert a type record for the cursor's type. Returns type_id."""
            try:
                t = cursor.type
                if t is None:
                    return 0
                spelling = t.spelling or ''
                try:
                    canonical = t.get_canonical().spelling
                except Exception:
                    canonical = spelling
                usr = cursor.get_usr() or spelling
                tid = cgdb_node_id(usr, spelling)
                if tid in _seen_type_ids:
                    return tid
                _seen_type_ids.add(tid)
                kind_str = 'builtin'
                try:
                    kind_map = {
                        _ci.TypeKind.VOID: 'builtin', _ci.TypeKind.BOOL: 'builtin',
                        _ci.TypeKind.CHAR_U: 'builtin', _ci.TypeKind.UCHAR: 'builtin',
                        _ci.TypeKind.CHAR16: 'builtin', _ci.TypeKind.CHAR32: 'builtin',
                        _ci.TypeKind.USHORT: 'builtin', _ci.TypeKind.UINT: 'builtin',
                        _ci.TypeKind.ULONG: 'builtin', _ci.TypeKind.ULONGLONG: 'builtin',
                        _ci.TypeKind.CHAR_S: 'builtin', _ci.TypeKind.SCHAR: 'builtin',
                        _ci.TypeKind.WCHAR: 'builtin', _ci.TypeKind.SHORT: 'builtin',
                        _ci.TypeKind.INT: 'builtin', _ci.TypeKind.LONG: 'builtin',
                        _ci.TypeKind.LONGLONG: 'builtin', _ci.TypeKind.FLOAT: 'builtin',
                        _ci.TypeKind.DOUBLE: 'builtin', _ci.TypeKind.LONGDOUBLE: 'builtin',
                        _ci.TypeKind.POINTER: 'pointer',
                        _ci.TypeKind.LVALUEREFERENCE: 'reference',
                        _ci.TypeKind.RVALUEREFERENCE: 'reference',
                        _ci.TypeKind.CONSTANTARRAY: 'array',
                        _ci.TypeKind.INCOMPLETEARRAY: 'array',
                        _ci.TypeKind.RECORD: 'record', _ci.TypeKind.ENUM: 'enum',
                        _ci.TypeKind.FUNCTIONPROTO: 'function',
                        _ci.TypeKind.FUNCTIONNOPROTO: 'function',
                        _ci.TypeKind.TYPEDEF: 'typedef', _ci.TypeKind.TEMPLATE: 'template',
                        _ci.TypeKind.ELABORATED: 'record',
                    }
                    kind_str = kind_map.get(t.kind, 'builtin')
                except Exception:
                    kind_str = 'builtin'
                size_bytes = None
                try:
                    size_bytes = t.get_size()
                    if size_bytes < 0:
                        size_bytes = None
                except Exception:
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
                is_const = False
                is_volatile = False
                # libclang Type has get_const/get_volatile methods in some versions
                try:
                    is_const = bool(t.is_const())
                except Exception:
                    is_const = False
                try:
                    is_volatile = bool(t.is_volatile())
                except Exception:
                    is_volatile = False
                cgdb_types.append({
                    'id': tid,
                    'spelling': spelling,
                    'canonical_spelling': canonical,
                    'kind': kind_str,
                    'size_bytes': size_bytes,
                    'is_const': is_const,
                    'is_volatile': is_volatile,
                    'attrs': {},
                })
                return tid
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                return 0

        # Walk top-level cursors of the main file only.
        if tu is None or tu.cursor is None:
            return {
                'cgdb_nodes': [], 'cgdb_types': [], 'cgdb_edges': [],
                'cgdb_invoke_sites': [], 'functions': [], 'edges': [],
            }

        # Track (func_cursor, func_node_id) for L4/L5/L8 per-function extraction.
        function_cursors = []  # list of (cursor, node_id)
        for top in tu.cursor.get_children():
            # Skip cursors from system headers — only emit main-file nodes.
            if top.location.file is None:
                continue
            if top.location.file.name != filepath:
                continue
            kind_name = top.kind.name if top.kind else ''
            if kind_name == 'FUNCTION_DECL' and top.is_definition():
                fid = add_node(top, 'function')
                function_cursors.append((top, fid))
                # Add parameter nodes
                for arg in top.get_arguments():
                    add_node(arg, 'parm')
                # Walk body for calls, var decls, decl refs, member refs
                self._walk_function_body(top, fid, add_node, cgdb_edges, cgdb_invoke_sites)
                # Legacy function record (minimal, for backward compat)
                legacy_functions.append({
                    'id': f"{domain}.{top.spelling}" if top.spelling else f"{domain}.anon_{fid}",
                    'name': top.spelling or '',
                    'domain': domain,
                    'source_file': filepath,
                    'line_number': top.location.line or 0,
                    'signature': top.displayname or '',
                    'body_text': self._extract_function_body(top),
                })
            elif kind_name in ('STRUCT_DECL', 'UNION_DECL', 'CLASS_DECL', 'ENUM_DECL'):
                tid = add_node(top)
                add_type(top)
                # Add field nodes + HAS_FIELD edges
                for child in top.get_children():
                    if child.kind and child.kind.name == 'FIELD_DECL':
                        field_id = add_node(child, 'field')
                        if field_id and tid:
                            cgdb_edges.append({
                                'src_id': tid, 'dst_id': field_id, 'kind': 'HAS_FIELD',
                                'file_path': filepath,
                                'line': child.location.line if child.location else 0,
                                'col': child.location.column if child.location else 0,
                                'attrs': {},
                            })
                    elif child.kind and child.kind.name == 'ENUM_CONSTANT_DECL':
                        add_node(child, 'enum_constant')
            elif kind_name == 'TYPEDEF_DECL':
                add_node(top, 'typedef')
                add_type(top)
            elif kind_name == 'VAR_DECL':
                add_node(top, 'var')

        # L3.5: Annotate nodes with config_predicate_id based on #ifdef ranges.
        # Read source text, build range_to_predicate map,
        # then for each node, find the innermost containing range's predicate.
        cgdb_predicates = []
        try:
            from _builder.cgdb_config_predicates import (
                ConfigPredicateExtractor, UNCONDITIONAL,
            )
            source_text = ''
            try:
                with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                    source_text = f.read()
            except (IOError, OSError):
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
            if source_text:
                extractor = ConfigPredicateExtractor(
                    macro_bindings=self._macro_bindings
                )
                ranges = extractor.pass2_range_to_predicate(source_text)
                seen_pred_ids = {UNCONDITIONAL.id: UNCONDITIONAL}
                for node in cgdb_nodes:
                    bs = node.get('byte_start', 0)
                    be = node.get('byte_end', bs)
                    pred = extractor.pass3_predicate_for_range(ranges, bs, be)
                    node['config_predicate_id'] = pred.id
                    if pred.id not in seen_pred_ids:
                        seen_pred_ids[pred.id] = pred
                # Serialize predicates to plain dicts (JSON-safe) so they
                # flow through the scanner → JSON → builder → IngestBatch.
                cgdb_predicates = [
                    {
                        'id': p.id,
                        'text_form': p.text_form,
                        'z3_form': p.z3_form,
                        'bdd_serialized': p.bdd_serialized,
                        'config_macros': list(p.config_macros),
                        'is_unconditional': bool(p.is_unconditional),
                        'is_contradictory': bool(p.is_contradictory),
                    }
                    for p in seen_pred_ids.values()
                ]
                # Also annotate edges with the predicate of their source location
                for edge in cgdb_edges:
                    # Use edge's line/col to find a position — but we need
                    # byte offsets. For MVP, we skip edge-level predicate
                    # annotation; nodes are the primary target.
                    pass
        except Exception:
            # Predicate extraction is best-effort; don't fail the scan.
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass

        # L7: Type-based ops_bindings detection — find VarDecls with struct
        # type containing function-pointer fields, then walk their InitListExpr
        # initializers for `.field = function` designators.
        cgdb_ops_bindings = []
        try:
            from _builder.cgdb_ops_bind import OpsBindDeriver
            deriver = OpsBindDeriver()
            deriver.derive_from_tu(
                tu.cursor, add_node, cgdb_edges, cgdb_ops_bindings,
                filepath=filepath,
            )
            # Derive indirect INVOKES edges from ops_bind bindings: for each
            # `ops->field(...)` call site in any function body, emit a INVOKES
            # edge to the impl function bound to that field.
            deriver.derive_call_candidates(
                tu.cursor, cgdb_ops_bindings, cgdb_edges, add_node,
                filepath=filepath,
            )
        except Exception:
            # ops_bind derivation is best-effort.
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass

        # L4/L5/L6/L8: CFG + data_flow + alias_sets + sync_primitives, per function.
        # Walk each function body for basic blocks,
        # def-use chains, alias sets, and sync primitive calls.
        cgdb_basic_blocks = []
        cgdb_cfg_edges = []
        cgdb_data_flow = []
        cgdb_alias_sets = []
        cgdb_sync_primitives = []
        cgdb_happens_before = []
        cgdb_conditions = []
        try:
            from _builder.cgdb_analysis import (
                CFGExtractor, DataFlowExtractor, AliasExtractor,
                ConditionExtractor,
            )
            from _builder.cgdb_sync import SyncPrimitiveWriter
            cfg_ext = CFGExtractor()
            df_ext = DataFlowExtractor()
            alias_ext = AliasExtractor()
            sync_ext = SyncPrimitiveWriter()
            cond_ext = ConditionExtractor()
            for func_cursor, func_node_id in function_cursors:
                # L4: CFG (basic blocks + edges) via clang static analyzer
                try:
                    func_name = func_cursor.spelling or ''
                    if func_name:
                        blocks, edges = cfg_ext.extract(
                            filepath, func_name, func_node_id,
                            tu_cursor=tu.cursor,
                            func_cursor=func_cursor,
                        )
                        for b in blocks:
                            cgdb_basic_blocks.append({
                                'id': b.id,
                                'function_id': b.function_id,
                                'block_index': b.block_index,
                                'is_entry': bool(b.is_entry),
                                'is_exit': bool(b.is_exit),
                            })
                        for e in edges:
                            cgdb_cfg_edges.append({
                                'src_block_id': e.src_block_id,
                                'dst_block_id': e.dst_block_id,
                                'kind': e.kind,
                                'function_id': e.function_id,
                            })
                except Exception:
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
                try:
                    df_records = df_ext.extract_from_ast(
                        func_cursor, func_node_id, add_node, filepath,
                    )
                    for r in df_records:
                        cgdb_data_flow.append({
                            'var_id': r.var_id,
                            'def_stmt_id': r.def_stmt_id,
                            'use_stmt_id': r.use_stmt_id,
                            'function_id': r.function_id,
                            'kind': r.kind,
                        })
                except Exception:
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
                try:
                    alias_records = alias_ext.extract_from_ast(
                        func_cursor, func_node_id, add_node,
                    )
                    for a in alias_records:
                        cgdb_alias_sets.append({
                            'ptr1_node_id': a.ptr1_node_id,
                            'ptr2_node_id': a.ptr2_node_id,
                            'kind': a.kind,
                            'confidence': a.confidence,
                        })
                except Exception:
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
                try:
                    sync_records, hb_records = sync_ext.extract_from_function(
                        func_cursor, func_node_id, add_node,
                    )
                    for s in sync_records:
                        cgdb_sync_primitives.append({
                            'function_id': s.function_id,
                            'kind': s.kind,
                            'sync_var_id': s.sync_var_id,
                            'acquire_stmt_id': s.acquire_stmt_id,
                            'release_stmt_id': s.release_stmt_id,
                        })
                    for h in hb_records:
                        cgdb_happens_before.append({
                            'write_event_id': h.write_event_id,
                            'read_event_id': h.read_event_id,
                            'reason': h.reason,
                            'confidence': h.confidence,
                        })
                except Exception:
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
                try:
                    cond_records = cond_ext.extract_from_ast(
                        func_cursor, func_node_id,
                    )
                    for c in cond_records:
                        cgdb_conditions.append({
                            'id': c.id,
                            'root_expr_id': c.root_expr_id,
                            'kind': c.kind,
                            'operator': c.operator,
                            'left_expr_id': c.left_expr_id,
                            'right_expr_id': c.right_expr_id,
                            'text_form': c.text_form,
                            'z3_form': c.z3_form,
                            'attrs': c.attrs,
                            'file_path': filepath,
                        })
                except Exception:
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
        except Exception:
            # L4-L8 extraction is best-effort; don't fail the scan.
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass

        # L10: doc comments — extract via clang's raw_comment for each
        # function/var/type decl we've registered as a node.
        cgdb_doc_comments = []
        cgdb_metadata = []
        try:
            from clang.cindex import CursorKind
            for top in tu.cursor.walk_preorder():
                try:
                    if not top.kind:
                        continue
                    kn = top.kind.name
                    if kn not in ('FUNCTION_DECL', 'VAR_DECL', 'FIELD_DECL',
                                   'STRUCT_DECL', 'UNION_DECL', 'ENUM_DECL',
                                   'TYPEDEF_DECL', 'CLASS_DECL'):
                        continue
                    raw = top.raw_comment
                    if not raw:
                        continue
                    # Resolve the node id (must already be in cgdb_nodes)
                    node_id = add_node(top, None)
                    if not node_id:
                        continue
                    comment_kind = self._classify_doc_comment(raw)
                    cleaned = self._clean_doc_comment(raw)
                    tags = self._parse_doc_tags(cleaned)
                    try:
                        loc = top.location
                        line = loc.line if loc else 0
                        col = loc.column if loc else 0
                    except Exception:
                        line = col = 0
                    cgdb_doc_comments.append({
                        'node_id': node_id,
                        'file_id': fid,
                        'line': line,
                        'col': col,
                        'comment_kind': comment_kind,
                        'raw_text': raw,
                        'cleaned_text': cleaned,
                        'tags': tags,
                        'byte_start': 0,
                        'byte_end': 0,
                    })
                except Exception:
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
            # can identify which scanner version produced a given batch.
            scanner_version = self._scanner_version()
            file_node_id = 0  # file metadata uses 0 as a placeholder target_id
            cgdb_metadata.append({
                'target_id': fid,
                'target_kind': 'file',
                'key': 'scanner_version',
                'value': scanner_version,
                'value_type': 'str',
                'source': 'scanner',
            })
            cgdb_metadata.append({
                'target_id': fid,
                'target_kind': 'file',
                'key': 'language',
                'value': 'c' if filepath.endswith('.c') else 'cpp',
                'value_type': 'str',
                'source': 'scanner',
            })
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        return {
            'cgdb_nodes': cgdb_nodes,
            'cgdb_types': cgdb_types,
            'cgdb_edges': cgdb_edges,
            'cgdb_invoke_sites': cgdb_invoke_sites,
            'cgdb_predicates': cgdb_predicates,
            'cgdb_ops_bindings': cgdb_ops_bindings,
            'cgdb_basic_blocks': cgdb_basic_blocks,
            'cgdb_cfg_edges': cgdb_cfg_edges,
            'cgdb_data_flow': cgdb_data_flow,
            'cgdb_alias_sets': cgdb_alias_sets,
            'cgdb_sync_primitives': cgdb_sync_primitives,
            'cgdb_happens_before': cgdb_happens_before,
            'cgdb_doc_comments': cgdb_doc_comments,
            'cgdb_metadata': cgdb_metadata,
            'conditions': cgdb_conditions,
            'functions': legacy_functions,
            'edges': legacy_edges,
        }

    def _walk_function_body(self, func_cursor, func_id: int, add_node, cgdb_edges: list, cgdb_invoke_sites: list):
        """Walk a function body, emitting INVOKES edges + body-level nodes.

        Each emitted node receives func_id as its enclosing_symbol_id
        (per cgdb-architecture doc 5.4.2). This avoids the need for
        semantic_parent walks in the inner loop — we already know the
        enclosing function contextually.
        """
        try:
            for child in func_cursor.walk_preorder():
                k = child.kind.name if child.kind else ''
                if k == 'CALL_EXPR' and child.spelling:
                    # Resolve callee definition via referenced
                    try:
                        defn = child.referenced or child
                    except Exception:
                        defn = child
                    # Callee function node: enclosing is the calling function
                    invoked_id = add_node(defn, 'function',
                                         enclosing_func_id=func_id)
                    loc = child.location
                    cgdb_edges.append({
                        'src_id': func_id, 'dst_id': invoked_id, 'kind': 'INVOKES',
                        'file_path': loc.file.name if loc.file else '',
                        'line': loc.line or 0, 'col': loc.column or 0,
                        'enclosing_symbol_id': func_id,
                        'attrs': {},
                    })
                    cgdb_invoke_sites.append({
                        'invoker_id': func_id, 'invoked_id': invoked_id,
                        'invoke_kind': 'direct', 'invoke_expr_id': 0,
                    })
                elif k == 'VAR_DECL':
                    add_node(child, 'var', enclosing_func_id=func_id)
                elif k == 'DECL_REF_EXPR':
                    add_node(child, 'decl_ref', enclosing_func_id=func_id)
                elif k == 'MEMBER_REF_EXPR':
                    add_node(child, 'member_ref', enclosing_func_id=func_id)
        except Exception:
            # walk_preorder can fail on broken ASTs — skip silently.
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass

    def _extract_function_body(self, func_cursor) -> str:
        """Extract the function body source text from a clang cursor."""
        try:
            # Get tokens for the body range. walk_preorder finds the compound stmt.
            for child in func_cursor.get_children():
                if child.kind and child.kind.name == 'COMPOUND_STMT':
                    # Use source range from extent
                    start = child.extent.start.offset
                    end = child.extent.end.offset
                    # We need the source bytes — read from file.
                    if child.location and child.location.file:
                        try:
                            with open(child.location.file.name, 'rb') as f:
                                f.seek(start)
                                return f.read(end - start).decode('utf-8', errors='replace')
                        except Exception:
                            logging.getLogger(__name__).debug("silent exception", exc_info=True)
                            return ''
            return ''
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            return ''

    def _classify_doc_comment(self, raw: str) -> str:
        """Classify a raw comment into 'doxygen_block', 'javadoc',
        'line', or 'block'.
        """
        if not raw:
            return ''
        s = raw.strip()
        if s.startswith('/**') or s.startswith('/*!'):
            return 'doxygen_block'
        if s.startswith('/**') or s.startswith('/*'):
            return 'block'
        if s.startswith('///') or s.startswith('//!'):
            return 'doxygen_block'
        if s.startswith('//'):
            return 'line'
        if s.startswith('--') or s.startswith('#'):
            return 'line'
        return 'block'

    def _clean_doc_comment(self, raw: str) -> str:
        """Strip comment markers and leading * characters from a raw comment.

        Returns the cleaned text suitable for LLM context or doc-code
        alignment comparison.
        """
        if not raw:
            return ''
        lines = raw.splitlines()
        cleaned = []
        for line in lines:
            s = line.strip()
            if s.startswith('/**') or s.startswith('/*!'):
                s = s[3:]
            elif s.startswith('/*'):
                s = s[2:]
            elif s.startswith('//'):
                s = s[2:]
                if s.startswith('!') or s.startswith('/'):
                    s = s[1:]
            if s.endswith('*/'):
                s = s[:-2].rstrip()
            # Remove leading "* " from doxygen block continuation lines
            if s.startswith('*'):
                s = s[1:].lstrip()
            cleaned.append(s)
        return '\n'.join(cleaned).strip()

    def _parse_doc_tags(self, cleaned: str) -> dict:
        """Parse Doxygen-style tags from cleaned comment text.

        Returns dict like {'param': [...], 'return': ..., 'note': ...,
        'see': [...], 'warning': [...]}.
        """
        if not cleaned:
            return {}
        tags: dict = {}
        cur_tag = None
        cur_text: list = []
        for line in cleaned.splitlines():
            s = line.strip()
            if not s:
                if cur_tag:
                    cur_text.append('')
                continue
            if s.startswith('@') or s.startswith('\\'):
                # Flush previous tag
                if cur_tag:
                    text = '\n'.join(cur_text).strip()
                    if cur_tag in ('param', 'see', 'warning', 'throws', 'throw'):
                        tags.setdefault(cur_tag, []).append(text)
                    else:
                        tags[cur_tag] = text
                # Parse new tag
                rest = s[1:].split(None, 1)
                if not rest:
                    cur_tag = None
                    cur_text = []
                    continue
                cur_tag = rest[0]
                cur_text = [rest[1]] if len(rest) > 1 else []
            else:
                if cur_tag:
                    cur_text.append(s)
                else:
                    # Description text — accumulate under 'description'
                    tags.setdefault('description', [])
                    if isinstance(tags['description'], list):
                        tags['description'].append(s)
        # Flush final tag
        if cur_tag:
            text = '\n'.join(cur_text).strip()
            if cur_tag in ('param', 'see', 'warning', 'throws', 'throw'):
                tags.setdefault(cur_tag, []).append(text)
            else:
                tags[cur_tag] = text
        # Convert description list to a single string
        if 'description' in tags and isinstance(tags['description'], list):
            tags['description'] = '\n'.join(tags['description']).strip()
        return tags

    def _scanner_version(self) -> str:
        """Return a version string for this scanner (for metadata records)."""
        return 'clang_scanner-1.0'

    def scan_file(self, filepath: str, source_root: str,
                  macro_bindings: dict = None) -> dict:
        """Override scan_file to use libclang parsing instead of tree-sitter.

        Returns the standard BaseScanner result shape PLUS cgdb_* keys.
        """
        if not _LIBCLANG_AVAILABLE or not _configure_libclang():
            return self._empty_result(filepath, source_root,
                                       error="libclang not available",
                                       error_kind="dependency")
        self._macro_bindings = macro_bindings or {}
        # Parse with libclang
        tu = self._parse_path(filepath)
        if tu is None:
            return self._empty_result(filepath, source_root,
                                       error="libclang parse failed",
                                       error_kind="parse")
        from _scanner.utils import classify_domain
        domain = classify_domain(filepath, source_root)
        try:
            result = self._extract_from_tu(tu, filepath, source_root, domain)
        except Exception as e:
            return self._empty_result(filepath, source_root,
                                       error=f"ExtractError: {e}",
                                       error_kind="extract")
        # Compose the final result dict — merge legacy + cgdb
        return {
            'file': filepath,
            'domain': domain,
            'functions': result.get('functions', []),
            'edges': result.get('edges', []),
            'globals': {'enums': [], 'constants': [], 'typedefs': [], 'global_vars': []},
            'vtable_registrations': [],
            'import_edges': [],
            'fn_ptr_calls': {},
            'macro_registrations': [],
            'cgdb_nodes': result.get('cgdb_nodes', []),
            'cgdb_types': result.get('cgdb_types', []),
            'cgdb_edges': result.get('cgdb_edges', []),
            'cgdb_invoke_sites': result.get('cgdb_invoke_sites', []),
            'cgdb_predicates': result.get('cgdb_predicates', []),
            'cgdb_ops_bindings': result.get('cgdb_ops_bindings', []),
            'cgdb_basic_blocks': result.get('cgdb_basic_blocks', []),
            'cgdb_cfg_edges': result.get('cgdb_cfg_edges', []),
            'cgdb_data_flow': result.get('cgdb_data_flow', []),
            'cgdb_sync_primitives': result.get('cgdb_sync_primitives', []),
            'cgdb_happens_before': result.get('cgdb_happens_before', []),
        }

    def _empty_result(self, filepath: str, source_root: str,
                       error: str = "", error_kind: str = "") -> dict:
        from _scanner.utils import classify_domain
        return {
            'file': filepath,
            'domain': classify_domain(filepath, source_root),
            'functions': [], 'edges': [],
            'globals': {'enums': [], 'constants': [], 'typedefs': [], 'global_vars': []},
            'vtable_registrations': [], 'import_edges': [],
            'fn_ptr_calls': {}, 'macro_registrations': [],
            'cgdb_nodes': [], 'cgdb_types': [],
            'cgdb_edges': [], 'cgdb_invoke_sites': [],
            'cgdb_predicates': [],
            'cgdb_ops_bindings': [],
            'cgdb_basic_blocks': [], 'cgdb_cfg_edges': [],
            'cgdb_data_flow': [], 'cgdb_sync_primitives': [],
            'cgdb_happens_before': [],
            'error': error, 'error_kind': error_kind,
        }


def is_clang_available() -> bool:
    """Return True if libclang is importable and configuable."""
    return _LIBCLANG_AVAILABLE and _configure_libclang()
