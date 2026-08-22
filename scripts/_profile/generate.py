"""Profile auto-generation: pre-scan, test scan, and auto-config phases.

Phases:
  1. Pre-scan: Scan doc/example/app dirs → discover naming conventions, external libs
  2. Test scan: Scan test dirs → discover test framework patterns, stub macros
  3. Auto-config: Merge findings into complete profile JSON
  4. Auto-detect: Detect project type, struct_op_types, api_prefixes, callback_patterns

Design principle: Project-specific knowledge (callback patterns, registration macros,
endpoint rules, etc.) lives in profile templates under config/profiles/. The tool
code only contains generic heuristics and template-loading logic.

Architecture: SourceInfoCollector performs a SINGLE os.walk traversal of the
source tree, collecting per-file data (macro definitions, function declarations,
ifdef prefixes, export symbols, struct declarations, etc.) into a structured
object. All auto-discovery functions then query this collector instead of doing
their own os.walk, reducing O(N×M) traversals to O(N).

Tree-sitter integration: For C/C++ projects with tree-sitter-c/cpp installed,
callback pattern detection uses AST-level parsing instead of regex, providing
much higher accuracy for function pointer parameter detection.
"""

import os
import re
import json
from collections import Counter, defaultdict
from pathlib import Path

# Built-in profile directory for loading project-type templates
_PROFILE_DIR = Path(__file__).resolve().parent.parent / "config" / "profiles"


# ---------------------------------------------------------------------------
# Template loading: all project-specific knowledge comes from here
# ---------------------------------------------------------------------------

def _load_all_templates() -> dict:
    """Load all profile templates from config/profiles/ directory.

    Returns dict mapping project_type → template dict.
    Only loads files with a 'detection' section (project-type templates).
    """
    templates = {}
    if not _PROFILE_DIR.is_dir():
        return templates
    for fpath in _PROFILE_DIR.glob("*.json"):
        if fpath.name.startswith("_"):
            continue
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            ptype = data.get("project", {}).get("project_type", "")
            if ptype and "detection" in data:
                templates[ptype] = data
        except (json.JSONDecodeError, IOError, OSError):
            continue
    return templates


# Template cache to avoid repeated disk reads (optimization I)
_template_cache = None


def _load_all_templates() -> dict:
    """Load all profile templates from config/profiles/ directory.

    Returns dict mapping project_type → template dict.
    Only loads files with a 'detection' section (project-type templates).
    Results are cached after first call.
    """
    global _template_cache
    if _template_cache is not None:
        return _template_cache
    templates = {}
    if not _PROFILE_DIR.is_dir():
        _template_cache = templates
        return templates
    for fpath in _PROFILE_DIR.glob("*.json"):
        if fpath.name.startswith("_"):
            continue
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            ptype = data.get("project", {}).get("project_type", "")
            if ptype and "detection" in data:
                templates[ptype] = data
        except (json.JSONDecodeError, IOError, OSError):
            continue
    _template_cache = templates
    return templates


def _clear_template_cache():
    """Clear the template cache (for testing or after profile updates)."""
    global _template_cache
    _template_cache = None


# ---------------------------------------------------------------------------
# Single-pass source info collector — replaces multiple os.walk traversals
# ---------------------------------------------------------------------------

# Directories to always skip during source tree walks
_SKIP_DIRS = frozenset({
    '__pycache__', 'node_modules', '.git', '.svn', '.hg',
    'build', 'dist', 'out', 'bin', 'obj',
    'venv', '.venv', '.env', '.tox', '.mypy_cache', '.pytest_cache',
    'CMakeFiles', 'cmake-build-debug', 'cmake-build-release',
    '.cache', 'third_party', 'vendor', 'external', '3rdparty', 'deps', 'contrib',
})


class SourceInfoCollector:
    """Single-pass collector that walks the source tree ONCE and gathers
    all information needed by auto-discovery functions.

    Instead of each function doing its own os.walk (O(N×M) total),
    this class does one os.walk (O(N)) and stores per-file data.
    Auto-discovery functions then query the collector's pre-built indices.

    Collected data per file:
      - content: full file text
      - macros: list of (name, params_str, body) from #define lines
      - func_decls: list of (return_type, name, params_str) from function declarations
      - ifdef_prefixes: list of prefix strings from #ifdef/#ifndef
      - export_symbols: list of symbol names from EXPORT_SYMBOL*
      - visibility_exports: list of (prefix, name) from __attribute__((visibility("default")))
      - struct_ops: list of struct names matching _ops/_operations/etc.
      - calls: list of callee names found in the file
      - is_header: bool
      - rel_path: relative path from source_root
    """

    # Regex patterns compiled once
    _DEFINE_RE = re.compile(
        r'#define\s+(\w+)(?:\s*\(([^)]*)\))?\s*((?:\\\n\s*)*[^\n]+)',
        re.MULTILINE,
    )
    _FUNC_DECL_RE = re.compile(
        r'(?:^|\n)\s*(?:static\s+)?(?:inline\s+)?(\w+)\s*\*?\s*(\w+)\s*\(([^)]*)\)',
        re.MULTILINE,
    )
    _IFDEF_RE = re.compile(r'#\s*if(?:n)?def\s+(\w+)')
    _EXPORT_SYMBOL_RE = re.compile(
        r'EXPORT_SYMBOL(?:_GPL|_GPL_FUTURE)?\s*\(\s*(\w+)'
    )
    _VISIBILITY_RE = re.compile(
        r'__attribute__\s*\(\s*\(\s*visibility\s*\(\s*"default"\s*\)\s*\)\s*\)'
        r'\s*\w+\s+(\w+)\s*\(',
    )
    _STRUCT_OPS_SUFFIXES = ('_ops', '_operations', '_callbacks', '_handlers', '_fns',
                            '_fn_table', '_function_table')
    _STRUCT_OPS_RE = re.compile(
        r'struct\s+(\w+(?:' + '|'.join(re.escape(s) for s in _STRUCT_OPS_SUFFIXES) + r'))\s*\{',
    )
    _CALL_RE = re.compile(r'(?:^|\n)\s*(\w+)\s*\(')
    _REGISTER_FUNC_RE = re.compile(
        r'(?:^|\n)\s*(?:static\s+)?(?:inline\s+)?\w+\s*\*?\s*'
        r'(\w+(?:_register|_add|_create|_set|_attach)\w*)\s*\(([^)]*)\)',
        re.MULTILINE,
    )

    def __init__(self, source_root: str, use_treesitter: bool = True):
        self.source_root = os.path.abspath(source_root)

        # Per-file data: rel_path → dict of collected info
        self._files = {}

        # Aggregated indices (built during collection)
        self.macro_name_counter = Counter()       # macro_name → count
        self.func_prefix_counter = Counter()       # func prefix → count
        self.ifdef_prefix_counter = Counter()      # ifdef prefix → count
        self.export_prefix_counter = Counter()     # export symbol prefix → count
        self.all_macros = {}                       # macro_name → body text (global)
        self.struct_op_types = []                  # list of struct names
        self._struct_op_seen = set()

        # Build system files
        self.meson_build_files = []     # list of meson.build paths
        self.cmake_files = []           # list of CMakeLists.txt paths
        self.version_map_files = []     # list of version.map / linker version scripts
        self.makefile_files = []        # list of Makefile paths

        # Run the single-pass collection (tree-sitter if available, regex fallback)
        if use_treesitter:
            parser, lang = _get_ts_parser()
            if parser is not None:
                self._collect_with_treesitter(parser, lang)
            else:
                self._collect()
        else:
            self._collect()

    def _should_skip_dir(self, dirname: str) -> bool:
        return dirname.startswith('.') or dirname in _SKIP_DIRS

    def _read_file(self, fpath: str) -> str | None:
        try:
            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                return f.read()
        except (IOError, OSError):
            return None

    def _collect(self):
        """Single os.walk traversal collecting all needed data."""
        for dirpath, dirnames, filenames in os.walk(self.source_root):
            dirnames[:] = [d for d in dirnames if not self._should_skip_dir(d)]

            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(fpath, self.source_root)

                # Track build system files
                if fname == 'meson.build':
                    self.meson_build_files.append(fpath)
                elif fname == 'CMakeLists.txt':
                    self.cmake_files.append(fpath)
                elif fname in ('version.map', 'Versions'):
                    self.version_map_files.append(fpath)
                elif fname in ('Makefile', 'makefile', 'GNUmakefile'):
                    self.makefile_files.append(fpath)

                # Only process source files
                ext = os.path.splitext(fname)[1]
                if ext not in ('.c', '.h', '.cpp', '.hpp'):
                    continue

                content = self._read_file(fpath)
                if content is None:
                    continue

                is_header = ext in ('.h', '.hpp')

                file_data = {
                    'content': content,
                    'macros': [],
                    'func_decls': [],
                    'ifdef_prefixes': [],
                    'export_symbols': [],
                    'visibility_exports': [],
                    'struct_ops': [],
                    'calls': [],
                    'register_funcs': [],
                    'is_header': is_header,
                    'rel_path': rel_path,
                }

                # Extract macros
                for m in self._DEFINE_RE.finditer(content):
                    macro_name = m.group(1)
                    params_str = m.group(2) or ''
                    body = m.group(3).strip()
                    file_data['macros'].append((macro_name, params_str, body))
                    self.all_macros[macro_name] = body
                    self.macro_name_counter[macro_name] += 1

                # Extract function declarations
                for m in self._FUNC_DECL_RE.finditer(content):
                    ret_type = m.group(1)
                    func_name = m.group(2)
                    params_str = m.group(3)
                    file_data['func_decls'].append((ret_type, func_name, params_str))
                    # Extract prefix for convention detection
                    if len(func_name) >= 3 and not func_name.isupper() and '_' in func_name:
                        prefix = func_name[:func_name.index('_') + 1]
                        self.func_prefix_counter[prefix] += 1

                # Extract #ifdef prefixes
                for m in self._IFDEF_RE.finditer(content):
                    macro = m.group(1)
                    if '_' in macro:
                        prefix = macro[:macro.index('_') + 1]
                        file_data['ifdef_prefixes'].append(prefix)
                        self.ifdef_prefix_counter[prefix] += 1

                # Extract EXPORT_SYMBOL
                for m in self._EXPORT_SYMBOL_RE.finditer(content):
                    name = m.group(1)
                    file_data['export_symbols'].append(name)
                    if '_' in name:
                        prefix = name[:name.index('_') + 1]
                        self.export_prefix_counter[prefix] += 1

                # Extract visibility attribute exports
                for m in self._VISIBILITY_RE.finditer(content):
                    name = m.group(1)
                    prefix = name[:name.index('_') + 1] if '_' in name else ''
                    file_data['visibility_exports'].append((prefix, name))
                    if prefix:
                        self.export_prefix_counter[prefix] += 1

                # Extract struct ops types
                for m in self._STRUCT_OPS_RE.finditer(content):
                    name = m.group(1)
                    file_data['struct_ops'].append(name)
                    if name not in self._struct_op_seen:
                        self._struct_op_seen.add(name)
                        self.struct_op_types.append(name)

                # Extract function calls (from .c/.cpp files only)
                if not is_header:
                    for m in self._CALL_RE.finditer(content):
                        callee = m.group(1)
                        if len(callee) >= 3 and not callee.isupper():
                            file_data['calls'].append(callee)

                # Extract registration function declarations (for callback discovery)
                for m in self._REGISTER_FUNC_RE.finditer(content):
                    func_name = m.group(1)
                    params_str = m.group(2)
                    file_data['register_funcs'].append((func_name, params_str))

                self._files[rel_path] = file_data

    def _collect_with_treesitter(self, parser, lang):
        """Single os.walk traversal using tree-sitter for C/C++ AST parsing.

        Replaces regex-based extraction with AST-level analysis for:
          - Function declarations (much more accurate)
          - Struct definitions with ops suffixes
          - Function call expressions
          - Registration function detection

        Still uses regex for:
          - Macro definitions (preprocessor is not in tree-sitter's AST)
          - #ifdef prefixes (preprocessor)
          - EXPORT_SYMBOL (preprocessor macro invocation)
        """
        _STRUCT_OPS_SUFFIXES = ('_ops', '_operations', '_callbacks', '_handlers', '_fns',
                                '_fn_table', '_function_table')

        for dirpath, dirnames, filenames in os.walk(self.source_root):
            dirnames[:] = [d for d in dirnames if not self._should_skip_dir(d)]

            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(fpath, self.source_root)

                # Track build system files
                if fname == 'meson.build':
                    self.meson_build_files.append(fpath)
                elif fname == 'CMakeLists.txt':
                    self.cmake_files.append(fpath)
                elif fname in ('version.map', 'Versions'):
                    self.version_map_files.append(fpath)
                elif fname in ('Makefile', 'makefile', 'GNUmakefile'):
                    self.makefile_files.append(fpath)

                ext = os.path.splitext(fname)[1]
                if ext not in ('.c', '.h', '.cpp', '.hpp'):
                    continue

                content = self._read_file(fpath)
                if content is None:
                    continue

                is_header = ext in ('.h', '.hpp')
                is_c = ext in ('.c', '.h')

                file_data = {
                    'content': content,
                    'macros': [],
                    'func_decls': [],
                    'ifdef_prefixes': [],
                    'export_symbols': [],
                    'visibility_exports': [],
                    'struct_ops': [],
                    'calls': [],
                    'register_funcs': [],
                    'is_header': is_header,
                    'rel_path': rel_path,
                }

                # Still use regex for preprocessor constructs
                # Macros
                for m in self._DEFINE_RE.finditer(content):
                    macro_name = m.group(1)
                    params_str = m.group(2) or ''
                    body = m.group(3).strip()
                    file_data['macros'].append((macro_name, params_str, body))
                    self.all_macros[macro_name] = body
                    self.macro_name_counter[macro_name] += 1

                # #ifdef prefixes
                for m in self._IFDEF_RE.finditer(content):
                    macro = m.group(1)
                    if '_' in macro:
                        prefix = macro[:macro.index('_') + 1]
                        file_data['ifdef_prefixes'].append(prefix)
                        self.ifdef_prefix_counter[prefix] += 1

                # EXPORT_SYMBOL
                for m in self._EXPORT_SYMBOL_RE.finditer(content):
                    name = m.group(1)
                    file_data['export_symbols'].append(name)
                    if '_' in name:
                        prefix = name[:name.index('_') + 1]
                        self.export_prefix_counter[prefix] += 1

                # Use tree-sitter for C/C++ files
                if is_c:
                    try:
                        tree = parser.parse(content.encode('utf-8'))
                        self._extract_from_ast(tree.root_node, content, file_data,
                                               _STRUCT_OPS_SUFFIXES, is_header)
                    except Exception:
                        # Fall back to regex for this file
                        self._extract_from_regex(content, file_data, _STRUCT_OPS_SUFFIXES, is_header)
                else:
                    # C++ files: use regex for now
                    self._extract_from_regex(content, file_data, _STRUCT_OPS_SUFFIXES, is_header)

                self._files[rel_path] = file_data

    def _extract_from_ast(self, root_node, content, file_data, ops_suffixes, is_header):
        """Extract information from tree-sitter AST for a C file."""
        content_bytes = content.encode('utf-8')
        self._walk_ast(root_node, content_bytes, file_data, ops_suffixes, is_header)

    def _walk_ast(self, node, source_bytes, file_data, ops_suffixes, is_header):
        """Recursively walk tree-sitter AST to extract declarations and calls."""
        # Function declarations and definitions
        if node.type == 'function_definition':
            self._process_func_def(node, source_bytes, file_data)
        elif node.type == 'declaration':
            self._process_declaration(node, source_bytes, file_data, ops_suffixes)

        # Call expressions (in function bodies)
        if node.type == 'call_expression':
            func_node = node.child_by_field_name('function')
            if func_node:
                callee = source_bytes[func_node.start_byte:func_node.end_byte].decode('utf-8', errors='replace')
                if len(callee) >= 3 and not callee.isupper():
                    file_data['calls'].append(callee)

        for child in node.children:
            self._walk_ast(child, source_bytes, file_data, ops_suffixes, is_header)

    def _process_func_def(self, node, source_bytes, file_data):
        """Process a function_definition AST node."""
        # Get the function declarator
        declarator = node.child_by_field_name('declarator')
        if not declarator:
            return

        func_name, params_str = self._parse_func_declarator(declarator, source_bytes)
        if not func_name:
            return

        ret_type = ''
        type_node = node.child_by_field_name('type')
        if type_node:
            ret_type = source_bytes[type_node.start_byte:type_node.end_byte].decode('utf-8', errors='replace')

        file_data['func_decls'].append((ret_type, func_name, params_str))

        # Extract prefix for convention detection
        if len(func_name) >= 3 and not func_name.isupper() and '_' in func_name:
            prefix = func_name[:func_name.index('_') + 1]
            self.func_prefix_counter[prefix] += 1

        # Check if this is a registration function
        _REG_SUFFIXES = ('_register', '_add', '_create', '_set', '_attach', '_install')
        if any(func_name.endswith(suf) for suf in _REG_SUFFIXES):
            file_data['register_funcs'].append((func_name, params_str))

    def _process_declaration(self, node, source_bytes, file_data, ops_suffixes):
        """Process a declaration AST node (structs, function declarations)."""
        # Check for struct declarations with ops suffixes
        for child in node.children:
            if child.type == 'struct_specifier':
                name_node = child.child_by_field_name('name')
                if name_node:
                    name = source_bytes[name_node.start_byte:name_node.end_byte].decode('utf-8', errors='replace')
                    if any(name.endswith(suf) for suf in ops_suffixes):
                        file_data['struct_ops'].append(name)
                        if name not in self._struct_op_seen:
                            self._struct_op_seen.add(name)
                            self.struct_op_types.append(name)

        # Check for function declarations (not definitions)
        for child in node.children:
            if child.type == 'function_declarator':
                func_name, params_str = self._parse_func_declarator(child, source_bytes)
                if func_name:
                    ret_type = ''
                    for tc in node.children:
                        if tc.type not in ('function_declarator', 'pointer_declarator'):
                            ret_type += source_bytes[tc.start_byte:tc.end_byte].decode('utf-8', errors='replace') + ' '
                    file_data['func_decls'].append((ret_type.strip(), func_name, params_str))
                    if len(func_name) >= 3 and not func_name.isupper() and '_' in func_name:
                        prefix = func_name[:func_name.index('_') + 1]
                        self.func_prefix_counter[prefix] += 1
                    _REG_SUFFIXES = ('_register', '_add', '_create', '_set', '_attach', '_install')
                    if any(func_name.endswith(suf) for suf in _REG_SUFFIXES):
                        file_data['register_funcs'].append((func_name, params_str))

    def _parse_func_declarator(self, declarator, source_bytes):
        """Parse a function_declarator node to extract name and params."""
        # Handle pointer_declarator wrapping function_declarator
        if declarator.type == 'pointer_declarator':
            for child in declarator.children:
                if child.type == 'function_declarator':
                    declarator = child
                    break

        func_name = ''
        params_str = ''

        # Get function name
        name_node = declarator.child_by_field_name('declarator')
        if name_node:
            func_name = source_bytes[name_node.start_byte:name_node.end_byte].decode('utf-8', errors='replace')

        # Get parameters
        params_node = declarator.child_by_field_name('parameters')
        if params_node:
            params_str = source_bytes[params_node.start_byte:params_node.end_byte].decode('utf-8', errors='replace')
            # Remove outer parens
            if params_str.startswith('(') and params_str.endswith(')'):
                params_str = params_str[1:-1]

        return func_name, params_str

    def _extract_from_regex(self, content, file_data, ops_suffixes, is_header):
        """Fallback regex extraction for non-C files or when tree-sitter fails."""
        # Function declarations
        for m in self._FUNC_DECL_RE.finditer(content):
            ret_type = m.group(1)
            func_name = m.group(2)
            params_str = m.group(3)
            file_data['func_decls'].append((ret_type, func_name, params_str))
            if len(func_name) >= 3 and not func_name.isupper() and '_' in func_name:
                prefix = func_name[:func_name.index('_') + 1]
                self.func_prefix_counter[prefix] += 1

        # Struct ops types
        for m in self._STRUCT_OPS_RE.finditer(content):
            name = m.group(1)
            file_data['struct_ops'].append(name)
            if name not in self._struct_op_seen:
                self._struct_op_seen.add(name)
                self.struct_op_types.append(name)

        # Function calls
        if not is_header:
            for m in self._CALL_RE.finditer(content):
                callee = m.group(1)
                if len(callee) >= 3 and not callee.isupper():
                    file_data['calls'].append(callee)

        # Registration function declarations
        for m in self._REGISTER_FUNC_RE.finditer(content):
            func_name = m.group(1)
            params_str = m.group(2)
            file_data['register_funcs'].append((func_name, params_str))

    # --- Query methods used by auto-discovery functions ---

    def iter_files(self, extensions=None, in_dirs=None):
        """Iterate over collected files, optionally filtering by extension and directory.

        Args:
            extensions: set of extensions to include (e.g., {'.h', '.c'})
            in_dirs: list of directory prefixes to include (e.g., ['include', 'lib'])

        Yields:
            (rel_path, file_data) tuples
        """
        for rel_path, data in self._files.items():
            if extensions and not any(rel_path.endswith(ext) for ext in extensions):
                continue
            if in_dirs and not any(rel_path.startswith(d + '/') or rel_path.startswith(d + os.sep) for d in in_dirs):
                continue
            yield rel_path, data

    def get_file_content(self, rel_path: str) -> str | None:
        """Get file content by relative path."""
        data = self._files.get(rel_path)
        return data['content'] if data else None

    def all_header_files(self):
        """Iterate over all header files."""
        return self.iter_files(extensions={'.h', '.hpp'})

    def all_source_files(self):
        """Iterate over all C/C++ source files."""
        return self.iter_files(extensions={'.c', '.cpp'})

    def all_c_files(self):
        """Iterate over all .c files."""
        return self.iter_files(extensions={'.c'})

    def all_h_files(self):
        """Iterate over all .h files."""
        return self.iter_files(extensions={'.h'})


# ---------------------------------------------------------------------------
# Phase 1: Pre-scan — discover naming conventions and external library usage
# ---------------------------------------------------------------------------

def prescan(source_root: str, collector: SourceInfoCollector = None) -> dict:
    """Scan doc/example/app directories to discover project conventions.

    Uses SourceInfoCollector for single-pass data when available.

    Returns a dict with discovered:
      - public_prefixes: likely public API prefixes
      - external_lib_prefixes: external library function prefixes
      - macro_condition_prefixes: #ifdef macro prefixes
      - detected_frameworks: framework names detected from paths
    """
    findings = {
        "public_prefixes": [],
        "external_lib_prefixes": {},
        "macro_condition_prefixes": [],
        "detected_frameworks": [],
        "naming_conventions": {},
    }

    # Universal external library prefixes (POSIX, C stdlib, widely-used libs).
    # Project-specific prefixes are NOT hardcoded here — they come from
    # profile JSON templates or are discovered by heuristic analysis below.
    _UNIVERSAL_EXTERNAL_PREFIXES = {
        'pthread_', 'SSL_', 'EVP_', 'OPENSSL', 'TLS_',
        'uuid_', 'numa_', 'aio_', 'fuse_', 'cuse_',
        'io_uring_', 'epoll_',
    }

    # Category mapping for universal external prefixes only.
    # Project-specific category mappings come from profile JSON.
    _UNIVERSAL_CATEGORY_MAP = {
        'pthread_': ('external_posix', False),
        'SSL_': ('external_openssl', True),
        'EVP_': ('external_openssl', True),
        'OPENSSL': ('external_openssl', True),
        'TLS_': ('external_openssl', True),
        'epoll_': ('external_posix', False),
        'uuid_': ('external_lib', False),
        'numa_': ('external_lib', False),
        'fuse_': ('external_fuse', True),
        'cuse_': ('external_fuse', True),
        'io_uring_': ('external_lib', True),
        'aio_': ('external_lib', True),
    }

    if collector is None:
        collector = SourceInfoCollector(source_root)

    # Analyze function prefixes from collector's aggregated data
    all_known_external = _UNIVERSAL_EXTERNAL_PREFIXES | set(findings.get("external_lib_prefixes", {}).keys())
    total_funcs = sum(collector.func_prefix_counter.values())
    if total_funcs > 0:
        for prefix, count in collector.func_prefix_counter.most_common(20):
            if count / total_funcs > 0.05 and prefix not in all_known_external:
                findings["public_prefixes"].append(prefix)

    # Analyze #ifdef prefixes from collector's aggregated data
    total_ifdefs = sum(collector.ifdef_prefix_counter.values())
    if total_ifdefs > 0:
        for prefix, count in collector.ifdef_prefix_counter.most_common(10):
            if count / total_ifdefs > 0.1:
                findings["macro_condition_prefixes"].append(prefix.rstrip('_') + '_')

    # Detect frameworks from directory structure using generic heuristics:
    # Count source files per top-level subdirectory of include/ and lib/
    include_dir = os.path.join(source_root, "include")
    lib_dir = os.path.join(source_root, "lib")
    for scan_base in [include_dir, lib_dir]:
        if not os.path.isdir(scan_base):
            continue
        for entry in sorted(os.listdir(scan_base)):
            entry_path = os.path.join(scan_base, entry)
            if not os.path.isdir(entry_path):
                continue
            # Count source files using collector data
            src_count = 0
            for rel_path, _ in collector.iter_files(in_dirs=[os.path.relpath(entry_path, source_root)]):
                src_count += 1
            if src_count >= 5:
                fw_name = entry.lower().replace('-', '_')
                if fw_name not in findings["detected_frameworks"]:
                    findings["detected_frameworks"].append(fw_name)

    # Detect external library usage from function calls in source files
    external_prefixes = set()
    scan_dirs = ["doc", "examples", "app", "include", "lib"]
    for rel_path, data in collector.iter_files(extensions={'.c', '.cpp'}, in_dirs=scan_dirs):
        for callee in data['calls']:
            for prefix in _UNIVERSAL_EXTERNAL_PREFIXES:
                if callee.startswith(prefix):
                    external_prefixes.add(prefix)

    # Build external_lib_prefixes from discovered usage.
    for prefix in sorted(external_prefixes):
        if prefix in _UNIVERSAL_CATEGORY_MAP:
            category, visible = _UNIVERSAL_CATEGORY_MAP[prefix]
            findings["external_lib_prefixes"][prefix] = {
                "category": category,
                "visible": visible,
            }
        else:
            base_name = prefix.rstrip('_')
            findings["external_lib_prefixes"][prefix] = {
                "category": f"external_{base_name}",
                "visible": True,
            }

    return findings


# ---------------------------------------------------------------------------
# Auto-detect project type from source tree markers (template-driven)
# ---------------------------------------------------------------------------

def _detect_project_name(source_root: str) -> str:
    """Detect the project name from directory names, signature files, and templates.

    Detection strategy (in priority order):
      1. Template project_name_aliases: check root directory name against
         known alias patterns loaded from profile templates.
      2. Signature files: check README.md, CMakeLists.txt for project name.
      3. Fallback: use root directory name, stripping common suffixes.

    Returns:
        Detected project name (e.g., "spdk", "dpdk", "linux").
    """
    root_name = os.path.basename(os.path.abspath(source_root))

    # Step 1: Build alias map from all templates
    templates = _load_all_templates()
    alias_map = {}
    for ptype, tmpl in templates.items():
        for alias, canonical in tmpl.get("project_name_aliases", {}).items():
            alias_map[alias.lower()] = canonical

    lower_root = root_name.lower()
    if lower_root in alias_map:
        return alias_map[lower_root]
    # Also check if root name directly matches a project type
    if lower_root in templates:
        return lower_root

    # Step 2: Check signature files for project identification
    readme_path = os.path.join(source_root, "README.md")
    if os.path.isfile(readme_path):
        try:
            with open(readme_path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    lower_line = line.lower()
                    # Check if any template's project name appears in the first line
                    for ptype in templates:
                        if ptype in lower_line:
                            return ptype
                    break  # Only check the first non-empty line
        except (IOError, OSError):
            pass

    # CMakeLists.txt — check for project() directive
    cmake_path = os.path.join(source_root, "CMakeLists.txt")
    if os.path.isfile(cmake_path):
        try:
            with open(cmake_path, 'r', encoding='utf-8', errors='replace') as f:
                for m in re.finditer(r'project\s*\(\s*(\w+)', f.read()):
                    return m.group(1).lower()
        except (IOError, OSError):
            pass

    # Step 3: Fallback — strip common suffixes from root directory name
    stripped = re.sub(r'-(?:source|src|master|main)$', '', root_name)
    return stripped.lower()


def detect_project_type(source_root: str) -> str:
    """Detect project type from build system markers, directory structure, and source files.

    Uses template-driven detection: loads all profile templates and evaluates
    their detection rules against the source tree. The template with the
    strongest match wins.

    Returns:
        One of the project types defined in profile templates, or "generic_c_cpp".
    """
    templates = _load_all_templates()

    # Evaluate each template's detection rules against the source tree
    best_type = "generic_c_cpp"
    best_score = 0

    for ptype, tmpl in templates.items():
        detection = tmpl.get("detection", {})
        score = 0

        # Check dir_markers: directories that must exist
        dir_markers = detection.get("dir_markers", [])
        for marker in dir_markers:
            if os.path.isdir(os.path.join(source_root, marker)):
                score += 10

        # Check file_markers: files that must exist
        file_markers = detection.get("file_markers", [])
        for marker in file_markers:
            if os.path.exists(os.path.join(source_root, marker)):
                score += 15

        # Check dir_structure: named directory paths that must exist
        dir_structure = detection.get("dir_structure", {})
        for key, path in dir_structure.items():
            if os.path.exists(os.path.join(source_root, path)):
                score += 10

        # Check build_system
        build_system = detection.get("build_system")
        if build_system:
            if build_system == "meson" and os.path.exists(os.path.join(source_root, "meson.build")):
                score += 10
            elif build_system == "kbuild":
                if os.path.exists(os.path.join(source_root, "Kbuild")) or \
                   os.path.exists(os.path.join(source_root, "Kconfig")):
                    score += 10
                    # Additional Kbuild signal: obj-$(CONFIG in Makefile
                    makefile_path = os.path.join(source_root, "Makefile")
                    if os.path.exists(makefile_path):
                        try:
                            with open(makefile_path, 'r', errors='replace') as f:
                                if "obj-$(CONFIG" in f.read():
                                    score += 5
                        except OSError:
                            pass

        # Check macro_prefixes: scan headers for macro prefixes like RTE_, SPDK_
        macro_prefixes = detection.get("macro_prefixes", [])
        if macro_prefixes:
            macro_found = False
            scan_dirs = detection.get("dir_markers", ["include", "lib"])
            for scan_dir in scan_dirs:
                dir_path = os.path.join(source_root, scan_dir)
                if not os.path.isdir(dir_path):
                    continue
                for dirpath, dirnames, filenames in os.walk(dir_path):
                    dirnames[:] = [d for d in dirnames if not d.startswith('.')]
                    for fname in filenames:
                        if not fname.endswith(('.c', '.h')):
                            continue
                        fpath = os.path.join(dirpath, fname)
                        try:
                            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                                content = f.read()
                        except (IOError, OSError):
                            continue
                        for mpfx in macro_prefixes:
                            if re.search(r'#\s*if(?:n)?def\s+(' + re.escape(mpfx) + r'\w+)', content):
                                macro_found = True
                                break
                        if macro_found:
                            break
                    if macro_found:
                        break
            if macro_found:
                score += 20

        # Check content_markers: specific strings that must appear in source
        content_markers = detection.get("content_markers", [])
        for cm in content_markers:
            pattern = cm.get("pattern", "")
            cm_dirs = cm.get("dirs", ["include", "lib"])
            min_hits = cm.get("min_hits", 1)
            hits = 0
            for scan_dir in cm_dirs:
                dir_path = os.path.join(source_root, scan_dir)
                if not os.path.isdir(dir_path):
                    continue
                for dirpath, dirnames, filenames in os.walk(dir_path):
                    dirnames[:] = [d for d in dirnames if not d.startswith('.')]
                    for fname in filenames:
                        if not fname.endswith(('.c', '.h')):
                            continue
                        fpath = os.path.join(dirpath, fname)
                        try:
                            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                                content = f.read()
                        except (IOError, OSError):
                            continue
                        if pattern in content:
                            hits += 1
                            if hits >= min_hits:
                                break
                    if hits >= min_hits:
                        break
            if hits >= min_hits:
                score += 20

        # Add priority bonus (lower priority number = higher preference)
        priority = detection.get("priority", 100)
        score += max(0, 100 - priority) // 10

        if score > best_score:
            best_score = score
            best_type = ptype

    return best_type


# ---------------------------------------------------------------------------
# Auto-generate struct_op_types from header file scanning
# ---------------------------------------------------------------------------

def auto_detect_struct_op_types(source_root: str, collector: SourceInfoCollector = None) -> list:
    """Scan header files for struct types that contain function pointer tables.

    Uses SourceInfoCollector for single-pass data when available.

    Returns:
        List of struct type names (e.g., ["file_operations", "net_device_ops"]).
    """
    if collector is None:
        collector = SourceInfoCollector(source_root)
    return collector.struct_op_types


# ---------------------------------------------------------------------------
# Auto-detect api_prefixes from EXPORT_SYMBOL / visibility attributes
# ---------------------------------------------------------------------------

def auto_detect_api_prefixes(source_root: str, collector: SourceInfoCollector = None) -> list:
    """Detect public API prefixes from EXPORT_SYMBOL declarations.

    Uses SourceInfoCollector for single-pass data when available.

    Returns:
        List of prefixes (e.g., ["rte_", "spdk_"]).
    """
    if collector is None:
        collector = SourceInfoCollector(source_root)

    # Return prefixes appearing 3+ times from collector's export_prefix_counter
    return [p for p, _ in collector.export_prefix_counter.most_common(200)
            if collector.export_prefix_counter[p] >= 3]


# ---------------------------------------------------------------------------
# Auto-detect skip_names: discover names that should be excluded from call edges
# ---------------------------------------------------------------------------

def auto_detect_skip_names(source_root: str, project_type: str = "",
                           collector: SourceInfoCollector = None) -> list:
    """Discover function/macro names that should be skipped during scanning.

    Uses SourceInfoCollector for single-pass data when available.

    Strategies:
      1. Scan header #define lines for expression-only macros (no semicolons,
         no function bodies, no control flow) — these produce no call edges.
      2. Detect printk-style wrapper macros (pr_*, dev_*) — no meaningful edges.
      3. Detect assertion/warning macros (WARN_ON, BUG_ON) — conditional traps.
      4. Load project-type-specific skip names from profile templates.
      5. Macro expansion: find macros that expand to other skip-able macros.
      6. Inline function body analysis: detect inline functions that are pure
         expressions (no function calls, no side effects).
      7. Frequency analysis: high-frequency identifiers that appear only in
         macros and never as actual function calls.

    Returns:
        List of names to add to skip_names.add.
    """
    skip_names = set()

    _CONTROL_FLOW_KW = frozenset({
        'if', 'else', 'for', 'while', 'do', 'switch', 'case',
        'return', 'goto', 'break', 'continue',
    })
    _STDLIB_SKIP = frozenset({
        'NULL', 'true', 'false', 'TRUE', 'FALSE',
        'offsetof', 'container_of', 'ARRAY_SIZE',
        'min', 'max', 'clamp', 'roundup', 'swap',
    })
    _UTILITY_SUFFIXES = ('_MIN', '_MAX', '_DIM', '_SIZE', '_MASK',
                         '_SHIFT', '_BITS', '_ALIGN', '_ROUND',
                         '_SWAP', '_CLAMP', '_ABS', '_DIV',
                         '_BIT', '_WORD')
    _PRINTK_WRAPPER_RE = re.compile(r'#define\s+(pr_\w+|dev_\w+)\s*\(')
    _ASSERT_MACRO_RE = re.compile(r'#define\s+(WARN(?:_ON)?(?:_ONCE)?|BUG(?:_ON)?)\s*\(')

    if collector is None:
        collector = SourceInfoCollector(source_root)

    # Single pass over all header files using collector data
    macro_name_counter = Counter()
    expr_macro_bodies = {}  # macro_name → body for expansion analysis

    for rel_path, data in collector.all_header_files():
        content = data['content']

        # Strategy 1: Detect expression-only macros from #define lines
        _EXPR_MACRO_RE = re.compile(
            r'#define\s+(\w+)\s*\(([^)]*)\)\s*'
            r'((?:\\\n\s*)*[^\n]*)'
        )
        for m in _EXPR_MACRO_RE.finditer(content):
            macro_name = m.group(1)
            params = m.group(2).strip()
            body = m.group(3).strip()

            if len(macro_name) < 3 or macro_name in _STDLIB_SKIP:
                continue
            if macro_name.startswith('_'):
                continue
            if ('{' in body or ';' in body or
                    any(kw in body.split() for kw in _CONTROL_FLOW_KW)):
                continue
            macro_name_counter[macro_name] += 1
            expr_macro_bodies[macro_name] = body

        # Strategy 2: Detect printk-style wrapper macros
        for m in _PRINTK_WRAPPER_RE.finditer(content):
            name = m.group(1)
            if len(name) >= 3:
                skip_names.add(name)

        # Strategy 3: Detect assertion/warning macros
        for m in _ASSERT_MACRO_RE.finditer(content):
            skip_names.add(m.group(1))

    # Only include expression-only macros that appear multiple times
    for name, count in macro_name_counter.items():
        if count >= 3 or any(name.endswith(suf) for suf in _UTILITY_SUFFIXES):
            skip_names.add(name)

    # Strategy 4: Load project-type-specific skip names from template
    templates = _load_all_templates()
    if project_type in templates:
        tmpl = templates[project_type]
        for name in tmpl.get("skip_names", {}).get("add", []):
            skip_names.add(name)

    # Strategy 5: Macro expansion — find macros whose bodies expand to
    # other skip-able macros. E.g., if RTE_LOG is skipped and
    # #define MY_LOG(...) RTE_LOG(...), then MY_LOG should also be skipped.
    # Build a reverse index: for each macro body, which macro names does it reference?
    # Then BFS from initial skip_names through the reverse index.
    # This is O(M + E) where E = total macro references, instead of O(M * S).
    _macro_body_refs = {}  # macro_name -> set of macro names referenced in its body
    for macro_name, body in collector.all_macros.items():
        # Find all identifier-like tokens in the body that match known macro names
        refs = set()
        for token in re.findall(r'\b\w+\b', body):
            if token in collector.all_macros and token != macro_name:
                refs.add(token)
        if refs:
            _macro_body_refs[macro_name] = refs

    # BFS expansion from initial skip_names
    newly_discovered = set()
    frontier = set(skip_names)
    visited = set(skip_names)
    while frontier:
        # Find all macros whose body references any name in frontier
        next_frontier = set()
        for macro_name, refs in _macro_body_refs.items():
            if macro_name in visited:
                continue
            if refs & frontier:  # body references at least one name in current frontier
                next_frontier.add(macro_name)
                newly_discovered.add(macro_name)
        if not next_frontier:
            break
        visited |= next_frontier
        frontier = next_frontier
    skip_names |= newly_discovered

    # Strategy 6: Inline function body analysis
    # Scan for static inline functions whose bodies contain no function calls
    # (pure expressions/returns only) — these produce no call edges
    _INLINE_FUNC_RE = re.compile(
        r'static\s+inline\s+\w+\s+(\w+)\s*\([^)]*\)\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}',
        re.MULTILINE | re.DOTALL
    )
    for rel_path, data in collector.all_header_files():
        for m in _INLINE_FUNC_RE.finditer(data['content']):
            func_name = m.group(1)
            body = m.group(2)
            # Check if body contains any function calls (identifier followed by '(')
            if not re.search(r'\b\w+\s*\(', body):
                # Pure expression — no call edges
                if len(func_name) >= 3:
                    skip_names.add(func_name)

    # Strategy 7: Frequency analysis — find identifiers that appear very
    # frequently as macro names but never (or rarely) as actual function calls
    total_call_count = Counter()
    for rel_path, data in collector.all_source_files():
        for callee in data['calls']:
            total_call_count[callee] += 1

    for macro_name, macro_count in macro_name_counter.items():
        if macro_name in skip_names:
            continue
        if macro_count >= 20 and total_call_count.get(macro_name, 0) == 0:
            # Very frequently defined as macro but never called as function
            # — likely a utility/constant macro that should be skipped
            skip_names.add(macro_name)

    return sorted(skip_names)


# ---------------------------------------------------------------------------
# Build system integration — extract project knowledge from build files
# ---------------------------------------------------------------------------

def _parse_meson_build(fpath: str) -> dict:
    """Extract project knowledge from a meson.build file.

    Extracts:
      - install_headers() calls → public header paths
      - dependency() calls → external library dependencies
      - project() call → project name, languages, version

    Returns dict with extracted data.
    """
    result = {
        "public_headers": [],
        "dependencies": [],
        "project_name": "",
        "languages": [],
    }

    try:
        with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except (IOError, OSError):
        return result

    # Extract project() call
    m = re.search(r'project\s*\(\s*["\'](\w+)["\']', content)
    if m:
        result["project_name"] = m.group(1)

    # Extract languages from project()
    m = re.search(r'project\s*\([^)]*languages\s*:\s*\[([^\]]*)\]', content)
    if m:
        langs_str = m.group(1)
        result["languages"] = re.findall(r'["\'](\w+)["\']', langs_str)

    # Extract install_headers() calls — these indicate public API headers
    for m in re.finditer(r'install_headers\s*\(([^)]*)\)', content):
        args_str = m.group(1)
        # Extract header file names
        headers = re.findall(r'["\']([^"\']+\.h)["\']', args_str)
        result["public_headers"].extend(headers)

    # Extract dependency() calls — these indicate external library usage
    for m in re.finditer(r'dependency\s*\(\s*["\'](\w+)["\']', content):
        result["dependencies"].append(m.group(1))

    return result


def _parse_version_map(fpath: str) -> dict:
    """Extract exported symbols from a version.map / linker version script.

    These files define the public API symbols exported by a library,
    providing authoritative data for api_prefixes and public_header_paths.

    Returns dict with:
      - exported_symbols: list of symbol names
      - version_tags: list of version tag strings
    """
    result = {
        "exported_symbols": [],
        "version_tags": [],
    }

    try:
        with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except (IOError, OSError):
        return result

    # Parse version script format:
    # VERSION_TAG {
    #     global:
    #         symbol_name;
    #         symbol_name;
    #     local:
    #         *;
    # };

    # Extract version tags
    for m in re.finditer(r'^(\w+(?:_\w+)*)\s*\{', content, re.MULTILINE):
        tag = m.group(1)
        if tag not in ('global', 'local', 'EXTERN'):
            result["version_tags"].append(tag)

    # Extract symbol names (lines ending with ; inside global sections)
    in_global = False
    for line in content.split('\n'):
        stripped = line.strip()
        if stripped == 'global:':
            in_global = True
            continue
        if stripped == 'local:' or stripped.startswith('}'):
            in_global = False
            continue
        if in_global and stripped.endswith(';'):
            symbol = stripped.rstrip(';').strip()
            if symbol and symbol != '*':
                result["exported_symbols"].append(symbol)

    return result


def _parse_cmake_lists(fpath: str) -> dict:
    """Extract project knowledge from CMakeLists.txt.

    Extracts:
      - project() name
      - add_library() names and types
      - target_link_libraries() → external dependencies
      - install(TARGETS/FILES) → public targets and headers

    Returns dict with extracted data.
    """
    result = {
        "project_name": "",
        "libraries": [],
        "dependencies": [],
        "install_files": [],
    }

    try:
        with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except (IOError, OSError):
        return result

    # Extract project() name
    m = re.search(r'project\s*\(\s*(\w+)', content)
    if m:
        result["project_name"] = m.group(1)

    # Extract add_library() calls
    for m in re.finditer(r'add_library\s*\(\s*(\w+)', content):
        result["libraries"].append(m.group(1))

    # Extract target_link_libraries
    for m in re.finditer(r'target_link_libraries\s*\(\s*\w+\s+([^)]*)\)', content):
        deps_str = m.group(1)
        # Filter out keywords
        for dep in re.findall(r'(\w+)', deps_str):
            if dep not in ('PUBLIC', 'PRIVATE', 'INTERFACE'):
                result["dependencies"].append(dep)

    # Extract install(FILES ...) headers
    for m in re.finditer(r'install\s*\(\s*FILES\s+([^)]*)\)', content):
        files_str = m.group(1)
        result["install_files"].extend(re.findall(r'["\']([^"\']+\.h)["\']', files_str))

    return result


def auto_discover_from_build_system(source_root: str,
                                     collector: SourceInfoCollector = None) -> dict:
    """Extract project knowledge from build system files.

    Parses meson.build, version.map, and CMakeLists.txt to discover:
      - Public API symbols (from version.map exported symbols)
      - Public header paths (from install_headers in meson.build)
      - External library dependencies
      - Project name and languages

    Uses SourceInfoCollector for build file discovery.

    Returns:
        Dict with build_system_info merged into profile sections.
    """
    if collector is None:
        collector = SourceInfoCollector(source_root)

    result = {
        "api_prefixes": [],           # prefixes from exported symbols
        "exported_symbols": [],       # all exported symbol names
        "public_header_dirs": [],     # directories with public headers
        "external_dependencies": [],  # external library names
    }

    # Parse version.map files for exported symbols
    for vmap_path in collector.version_map_files:
        vmap_data = _parse_version_map(vmap_path)
        result["exported_symbols"].extend(vmap_data["exported_symbols"])

        # Extract prefixes from exported symbols
        prefix_counter = Counter()
        for sym in vmap_data["exported_symbols"]:
            if '_' in sym:
                prefix = sym[:sym.index('_') + 1]
                prefix_counter[prefix] += 1
        for prefix, count in prefix_counter.most_common(20):
            if count >= 3:
                result["api_prefixes"].append(prefix)

    # Parse meson.build files
    for meson_path in collector.meson_build_files:
        meson_data = _parse_meson_build(meson_path)
        result["external_dependencies"].extend(meson_data["dependencies"])

        # Public headers from install_headers
        for header in meson_data["public_headers"]:
            header_dir = os.path.dirname(header)
            if header_dir and header_dir not in result["public_header_dirs"]:
                result["public_header_dirs"].append(header_dir)

    # Parse CMakeLists.txt files
    for cmake_path in collector.cmake_files:
        cmake_data = _parse_cmake_lists(cmake_path)
        result["external_dependencies"].extend(cmake_data["dependencies"])

    # Deduplicate
    result["api_prefixes"] = list(dict.fromkeys(result["api_prefixes"]))
    result["exported_symbols"] = list(dict.fromkeys(result["exported_symbols"]))
    result["public_header_dirs"] = list(dict.fromkeys(result["public_header_dirs"]))
    result["external_dependencies"] = list(dict.fromkeys(result["external_dependencies"]))

    return result


# ---------------------------------------------------------------------------
# Auto-discover public header paths
# ---------------------------------------------------------------------------

def auto_discover_public_header_paths(source_root: str, project_type: str = "",
                                      collector: SourceInfoCollector = None) -> list:
    """Discover directories containing public API headers.

    Uses SourceInfoCollector for single-pass data when available.

    Strategy:
      1. Scan include/ top-level directory
      2. Scan lib/*/include/ patterns (common in DPDK-style projects)
      3. Load project-type-specific paths from template
      4. If public_prefixes are known, find dirs with matching header files

    Returns:
        List of directory paths relative to source_root.
    """
    header_paths = set()

    if collector is None:
        collector = SourceInfoCollector(source_root)

    # Strategy 1: include/ directory
    include_dir = os.path.join(source_root, "include")
    if os.path.isdir(include_dir):
        header_paths.add("include")

    # Strategy 2: lib/*/include/ pattern
    lib_dir = os.path.join(source_root, "lib")
    if os.path.isdir(lib_dir):
        for entry in sorted(os.listdir(lib_dir)):
            sub_include = os.path.join(lib_dir, entry, "include")
            if os.path.isdir(sub_include):
                header_paths.add(f"lib/{entry}/include")

    # Strategy 3: Template-provided paths
    templates = _load_all_templates()
    if project_type in templates:
        for p in templates[project_type].get("api_detection", {}).get("public_header_paths", []):
            header_paths.add(p)

    # Strategy 4: Find dirs containing headers matching public_prefixes
    api_prefixes = auto_detect_api_prefixes(source_root, collector)
    for prefix in api_prefixes[:3]:
        prefix_base = prefix.rstrip('_')
        for rel_path, data in collector.all_header_files():
            # Check if any header in this dir matches the prefix
            fname = os.path.basename(rel_path)
            if fname.endswith('.h') and fname.startswith(prefix_base):
                dir_rel = os.path.dirname(rel_path)
                if dir_rel:
                    header_paths.add(dir_rel)

    return sorted(header_paths)


# ---------------------------------------------------------------------------
# Auto-infer internal_patterns from export macros
# ---------------------------------------------------------------------------

def auto_infer_internal_patterns(source_root: str, export_macros: list = None) -> list:
    """Infer internal marker patterns from detected export macros.

    If export macros like __rte_experimental or __rte_internal are found,
    their common prefix (e.g., __rte_) should be added to internal_patterns.

    Also loads project-type-specific internal_patterns from templates.

    Returns:
        List of internal pattern strings.
    """
    patterns = set()

    # Default internal patterns (from _DEFAULT_PROFILE)
    _DEFAULT_PATTERNS = ["_unit_", "_ut_", "_test_", "_perf_",
                          "_verify_", "_example_", "_internal",
                          "_priv", "_stub", "_mock"]
    patterns.update(_DEFAULT_PATTERNS)

    # Infer from export macros
    if export_macros:
        for macro in export_macros:
            # __rte_experimental → __rte_
            if macro.startswith('__') and '_' in macro[2:]:
                prefix = macro[:macro.index('_', 2) + 1]
                patterns.add(prefix)
            # _rte_internal → _rte_
            elif macro.startswith('_') and not macro.startswith('__') and '_' in macro[1:]:
                prefix = macro[:macro.index('_', 1) + 1]
                patterns.add(prefix)

    return sorted(patterns)


# ---------------------------------------------------------------------------
# Function name prefix clustering — detect significant naming domains
# ---------------------------------------------------------------------------

def auto_cluster_function_prefixes(source_root: str, collector: SourceInfoCollector = None) -> list:
    """Detect function name prefix clusters and generate domain rules.

    Extracts all function name prefixes, finds significant ones (≥5
    occurrences), and generates domain rules based on prefix patterns.
    This discovers domain boundaries purely from naming conventions,
    complementing directory-based domain detection.

    For example, if many functions start with "rte_eal_" and "rte_mbuf_",
    these form natural domains "eal" and "mbuf" even if they share a
    common parent directory.

    Args:
        source_root: Project source root directory.
        collector: Optional SourceInfoCollector for single-pass data.

    Returns:
        List of domain rule dicts with "pattern" and "domain_suffix".
    """
    if collector is None:
        collector = SourceInfoCollector(source_root)

    rules = []
    existing_patterns = set()

    # Build prefix frequency map from collector's func_prefix_counter
    # We need two-level prefixes: e.g., rte_eal_ and rte_mbuf_ as
    # separate domains, not just rte_
    prefix_counter = Counter()
    two_level_counter = Counter()

    for rel_path, data in collector.iter_files(extensions={'.c', '.h'}):
        for ret_type, func_name, params_str in data['func_decls']:
            if len(func_name) < 4 or func_name.isupper():
                continue
            # First-level prefix
            if '_' in func_name:
                parts = func_name.split('_')
                if len(parts) >= 2 and parts[0]:
                    prefix1 = parts[0] + '_'
                    prefix_counter[prefix1] += 1
                # Two-level prefix (e.g., rte_eal_)
                if len(parts) >= 3 and parts[0] and parts[1]:
                    prefix2 = parts[0] + '_' + parts[1] + '_'
                    two_level_counter[prefix2] += 1

    # Find significant two-level prefixes (≥5 occurrences)
    # These form more specific domain boundaries than one-level prefixes
    for prefix, count in two_level_counter.most_common(50):
        if count < 5:
            break
        # Extract domain suffix from the second component
        parts = prefix.rstrip('_').split('_')
        if len(parts) >= 2:
            domain = parts[1]  # e.g., "eal" from "rte_eal_"
            pat = prefix + r'\w+'
            if pat not in existing_patterns:
                rules.append({
                    "pattern": pat,
                    "domain_suffix": domain,
                })
                existing_patterns.add(pat)

    # Find significant one-level prefixes (≥20 occurrences)
    # Only add if no two-level rule already covers this prefix
    covered_prefixes = set()
    for rule in rules:
        pat = rule["pattern"]
        # Extract the one-level prefix from the two-level pattern
        m = re.match(r'(\w+_)', pat)
        if m:
            covered_prefixes.add(m.group(1))

    for prefix, count in prefix_counter.most_common(30):
        if count < 20:
            break
        if prefix in covered_prefixes:
            continue
        domain = prefix.rstrip('_')
        pat = prefix + r'\w+'
        if pat not in existing_patterns:
            rules.append({
                "pattern": pat,
                "domain_suffix": domain,
            })
            existing_patterns.add(pat)

    return rules


# ---------------------------------------------------------------------------
# Auto-infer domain_rules from directory structure
# ---------------------------------------------------------------------------

def auto_infer_domain_rules(source_root: str, project_type: str = "",
                            collector: SourceInfoCollector = None) -> list:
    """Infer domain consolidation rules from directory structure and naming.

    Strategy:
      1. Load project-type-specific domain_rules from template
      2. Detect domain fragmentation: if directories like lib/eal/common/,
         lib/eal/linux/, lib/eal/freebsd/ exist, merge them to domain "eal"
      3. Detect sub-directory domains: if many functions share a common prefix
         followed by a sub-prefix, create a domain rule
      4. Recognize common kernel subsystem directories and map to semantic tags

    Returns:
        List of domain rule dicts with "pattern" and "domain_suffix".
    """
    rules = []
    existing_patterns = set()

    # Strategy 1: Load template rules
    templates = _load_all_templates()
    if project_type in templates:
        for rule in templates[project_type].get("scan_hints", {}).get("domain_rules", []):
            rules.append(rule)
            existing_patterns.add(rule.get("pattern", ""))

    # Strategy 2: Detect lib/*/sub-platform/ fragmentation
    lib_dir = os.path.join(source_root, "lib")
    if os.path.isdir(lib_dir):
        for entry in sorted(os.listdir(lib_dir)):
            entry_path = os.path.join(lib_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            subdirs = [d for d in os.listdir(entry_path)
                       if os.path.isdir(os.path.join(entry_path, d))
                       and not d.startswith('.')]
            # If a lib directory has platform-like subdirs (common, linux, freebsd, etc.)
            _PLATFORM_DIRS = {'common', 'linux', 'freebsd', 'bsd', 'windows', 'unix', 'posix'}
            platform_subs = [s for s in subdirs if s in _PLATFORM_DIRS]
            if len(platform_subs) >= 2:
                # These should be merged into the parent domain
                for plat in platform_subs:
                    pat = f"{entry}_{plat}_"
                    if pat not in existing_patterns:
                        rules.append({
                            "pattern": pat,
                            "domain_suffix": entry,
                        })
                        existing_patterns.add(pat)

    # Strategy 3: Detect drivers/*/ sub-domains
    drivers_dir = os.path.join(source_root, "drivers")
    if os.path.isdir(drivers_dir):
        for entry in sorted(os.listdir(drivers_dir)):
            entry_path = os.path.join(drivers_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            # If there's a common prefix pattern in driver subdirectories
            subdirs = [d for d in os.listdir(entry_path)
                       if os.path.isdir(os.path.join(entry_path, d))
                       and not d.startswith('.')]
            if len(subdirs) >= 3:
                # Create a domain rule for the driver category
                pat = f"{entry}_\\w+_"
                if pat not in existing_patterns:
                    rules.append({
                        "pattern": pat,
                        "domain_suffix": entry,
                    })
                    existing_patterns.add(pat)

    # Strategy 4: Recognize common kernel subsystem directories
    # These are standard Linux kernel top-level directories that map to
    # semantic domain tags rather than using the raw directory name.
    _KERNEL_SUBSYSTEM_DIRS = {
        "sound": "sound",
        "fs": "filesystem",
        "net": "networking",
        "mm": "memory",
        "kernel": "kernel_core",
        "security": "security",
        "crypto": "crypto",
        "block": "block_layer",
    }
    for dirname, domain_tag in _KERNEL_SUBSYSTEM_DIRS.items():
        dir_path = os.path.join(source_root, dirname)
        if os.path.isdir(dir_path):
            pat = f"^{dirname}/"
            if pat not in existing_patterns:
                rules.append({
                    "pattern": pat,
                    "domain_tag": domain_tag,
                })
                existing_patterns.add(pat)

    # Strategy 5: Add prefix-based domain rules from function name clustering
    if collector is None:
        collector = SourceInfoCollector(source_root)
    prefix_rules = auto_cluster_function_prefixes(source_root, collector)
    for rule in prefix_rules:
        if rule["pattern"] not in existing_patterns:
            rules.append(rule)
            existing_patterns.add(rule["pattern"])

    return rules


# ---------------------------------------------------------------------------
# Auto-infer endpoint_rules from project type
# ---------------------------------------------------------------------------

def auto_infer_endpoint_rules(project_type: str = "",
                              source_root: str = "",
                              collector: SourceInfoCollector = None) -> list:
    """Infer endpoint classification rules based on project type and source analysis.

    Strategies:
      1. Universal rule: main() is always a program entry
      2. Project-type-specific rules from template
      3. Constructor functions: detect __attribute__((constructor)) and
         section-based init functions as library_entry endpoints
      4. Signal handler registration: detect signal()/sigaction() patterns
      5. Thread entry functions: detect functions passed to pthread_create

    Returns:
        List of endpoint rule dicts with "pattern" and "endpoint_type".
    """
    rules = []

    # Universal rule: main() is always a program entry
    rules.append({
        "pattern": r"^main$",
        "endpoint_type": "program_entry",
    })

    # Project-type-specific rules from template
    templates = _load_all_templates()
    if project_type in templates:
        for rule in templates[project_type].get("endpoint_classification", {}).get("endpoint_rules", []):
            if rule.get("pattern") == r"^main$":
                continue
            rules.append(rule)

    # Strategy 3: Detect constructor functions as library_entry endpoints
    # These are functions marked with __attribute__((constructor)) or
    # placed in .init sections — they run at library load time.
    if source_root:
        if collector is None:
            collector = SourceInfoCollector(source_root)

        _CONSTRUCTOR_FUNC_RE = re.compile(
            r'__attribute__\s*\(\s*\(\s*constructor(?:\s*\(\s*\d+\s*\))?\s*\)\s*\)'
            r'\s*(?:static\s+)?(?:void)\s+(\w+)\s*\(',
        )
        _INIT_SECTION_RE = re.compile(
            r'__attribute__\s*\(\s*\(\s*section\s*\(\s*"\.init(?:\w*)"\s*\)\s*\)\s*\)'
            r'\s*(?:static\s+)?(?:void)\s+(\w+)\s*\(',
        )

        constructor_names = set()
        for rel_path, data in collector.iter_files(extensions={'.c', '.h'}):
            content = data['content']
            for m in _CONSTRUCTOR_FUNC_RE.finditer(content):
                constructor_names.add(m.group(1))
            for m in _INIT_SECTION_RE.finditer(content):
                constructor_names.add(m.group(1))

        # Add constructor functions as library_entry endpoints (up to 20)
        for name in sorted(constructor_names)[:20]:
            rules.append({
                "pattern": f"^{re.escape(name)}$",
                "endpoint_type": "library_entry",
            })

    # Strategy 4: Signal handler registration patterns
    # These are always entry points since they can be called asynchronously
    signal_handler_names = set()
    if source_root:
        # Build set of defined function names for validation
        defined_funcs = set()
        for rel_path, data in collector.iter_files(extensions={'.c', '.h'}):
            for _, func_name, _ in data['func_decls']:
                defined_funcs.add(func_name)

        for rel_path, data in collector.iter_files(extensions={'.c'}):
            content = data['content']
            # Detect signal(sig, handler) / sigaction(..., &handler, ...)
            for m in re.finditer(r'(?:signal|sigaction)\s*\([^,]*,\s*(?:&)?\s*(\w+)', content):
                handler_name = m.group(1)
                # Filter out constants, types, and very short names
                if handler_name in ('SIG_IGN', 'SIG_DFL', 'NULL', '0',
                                     'int', 'void', 'struct', 'const'):
                    continue
                # Only include if it looks like a function name (lowercase, >=3 chars, defined)
                if len(handler_name) >= 3 and handler_name in defined_funcs:
                    signal_handler_names.add(handler_name)

        for name in sorted(signal_handler_names)[:20]:
            rules.append({
                "pattern": f"^{re.escape(name)}$",
                "endpoint_type": "signal_handler",
            })

    return rules


# ---------------------------------------------------------------------------
# Callsite verification for callback candidates
# ---------------------------------------------------------------------------

def _verify_callback_candidates(candidates: list, collector: SourceInfoCollector) -> list:
    """Verify callback candidates by checking if they're actually used at call sites.

    For each candidate registration function, check if it's actually called
    anywhere in the codebase with a function pointer argument at the expected
    position. This filters out false positives where the registration function
    exists but takes non-callback arguments.

    Also verifies by checking if any function matching the cb_arg pattern
    actually exists as a function definition.

    Args:
        candidates: List of callback pattern dicts with register_func, regex, etc.
        collector: SourceInfoCollector with pre-collected data.

    Returns:
        Filtered list of candidates with verified entries, plus _verified flag.
    """
    verified = []
    # Build set of all function names defined in the codebase
    defined_funcs = set()
    for rel_path, data in collector.iter_files(extensions={'.c', '.h'}):
        for _, func_name, _ in data['func_decls']:
            defined_funcs.add(func_name)

    for candidate in candidates:
        func_name = candidate["register_func"]
        regex = candidate.get("regex", "")
        cb_arg_index = candidate.get("cb_arg_index", 0)

        # Verification 1: Is the registration function actually called?
        is_called = False
        for rel_path, data in collector.iter_files(extensions={'.c'}):
            for callee in data['calls']:
                if callee == func_name:
                    is_called = True
                    break
            if is_called:
                break

        # Verification 2: Does the regex match any actual usage?
        regex_matches = 0
        if regex:
            for rel_path, data in collector.iter_files(extensions={'.c', '.h'}):
                content = data['content']
                regex_matches += len(re.findall(regex, content))
                if regex_matches >= 3:
                    break

        # Decision: include if called OR regex matches, mark confidence
        if is_called or regex_matches > 0:
            candidate["_verified"] = True
            if regex_matches >= 3:
                candidate["_confidence"] = candidate.get("_confidence", "medium")
            elif is_called:
                candidate["_confidence"] = "low"
            verified.append(candidate)
        else:
            # Not verified — include with very low confidence but mark as unverified
            # Still include because the function might be called from files we didn't scan
            candidate["_verified"] = False
            candidate["_confidence"] = "low"
            verified.append(candidate)

    return verified


# ---------------------------------------------------------------------------
# Auto-detect export macros
# ---------------------------------------------------------------------------

def _auto_detect_export_macros(source_root: str, collector: SourceInfoCollector = None) -> list:
    """Detect export macro names from #define lines in headers.

    Uses SourceInfoCollector for single-pass data when available.

    Returns:
        List of macro names.
    """
    macros = set()
    # Generic export macro pattern — matches any #define starting with EXPORT
    _EXPORT_MACRO_RE = re.compile(r'#define\s+(EXPORT\w+)\s*\(')

    # Collect project-specific export macro names from all templates.
    _template_export_macro_names = set()
    templates = _load_all_templates()
    for ptype, tmpl in templates.items():
        for em in tmpl.get("api_detection", {}).get("export_macros", []):
            if em:
                _template_export_macro_names.add(em)

    # Build combined regex for both generic EXPORT* and template-provided names
    if _template_export_macro_names:
        alt_pattern = '|'.join(re.escape(em) for em in sorted(_template_export_macro_names))
        _EXPORT_MACRO_RE = re.compile(
            r'#define\s+(EXPORT\w+|' + alt_pattern + r')\s*\('
        )

    if collector is None:
        collector = SourceInfoCollector(source_root)

    for rel_path, data in collector.all_header_files():
        content = data['content']
        for m in _EXPORT_MACRO_RE.finditer(content):
            macros.add(m.group(1))

    return sorted(macros)


# ---------------------------------------------------------------------------
# Auto-detect callback_patterns from common registration APIs
# ---------------------------------------------------------------------------

def auto_detect_callback_patterns(source_root: str, project_type: str = "",
                                   collector: SourceInfoCollector = None) -> list:
    """Detect callback registration patterns from source code.

    Uses SourceInfoCollector for single-pass data when available.

    Two strategies:
      1. Template-provided patterns: Load callback patterns from the matching
         profile template and check if those APIs are actually used in the source.
      2. Heuristic discovery: Scan header declarations for function-pointer
         parameters with callback naming patterns (*_cb, *_handler, *_fn).

    Only pthread_create is hardcoded as a universal POSIX pattern.
    All project-specific patterns come from profile templates.

    Args:
        source_root: Project source root directory.
        project_type: Detected project type (e.g., "dpdk", "spdk", "linux_kernel").
        collector: Optional SourceInfoCollector for single-pass data.

    Returns:
        List of dicts, each with "register_func", "regex", "cb_arg_index",
        "concurrency_type" matching the callback_detection.static_patterns schema.
    """
    # Universal callback patterns (POSIX only — project-specific ones come from templates)
    _UNIVERSAL_CALLBACK_APIS = {
        "pthread_create": {
            "regex": r"pthread_create\s*\(\s*[^,]*,\s*[^,]*,\s*(\w+)",
            "cb_arg_index": 2,
            "concurrency_type": "spawn_target",
        },
    }

    # Load template-provided callback patterns for this project type
    _template_callback_apis = {}
    templates = _load_all_templates()
    if project_type in templates:
        tmpl = templates[project_type]
        for pat in tmpl.get("callback_detection", {}).get("static_patterns", []):
            func_name = pat.get("register_func", "")
            if func_name and func_name not in _UNIVERSAL_CALLBACK_APIS:
                _template_callback_apis[func_name] = {
                    "regex": pat.get("regex", ""),
                    "cb_arg_index": pat.get("cb_arg_index", 0),
                    "concurrency_type": pat.get("concurrency_type", "callback_register"),
                }

    # Combine universal + template patterns for scanning
    _all_known_apis = {**_UNIVERSAL_CALLBACK_APIS, **_template_callback_apis}
    found_apis = set()

    if collector is None:
        collector = SourceInfoCollector(source_root)

    # Phase 1: Scan for known APIs in source files using collector data
    for rel_path, data in collector.iter_files(extensions={'.c', '.cpp', '.h', '.hpp'}):
        content = data['content']
        for api_name in _all_known_apis:
            if re.search(r'\b' + re.escape(api_name) + r'\s*\(', content):
                found_apis.add(api_name)

    # Phase 2: Heuristic discovery of project-specific callback registration
    # functions by scanning header declarations for function-pointer parameters
    # with callback naming patterns (*_cb, *_handler, *_fn, *_callback).
    _CB_PARAM_SUFFIXES = ('_cb', '_callback', '_handler', '_fn', '_func',
                          '_notif', '_notify', '_event_cb')

    heuristic_apis = {}  # register_func -> {regex, cb_arg_index, concurrency_type}

    # Only scan include/ and lib/ header files for heuristic discovery
    for rel_path, data in collector.iter_files(extensions={'.h'}, in_dirs=["include", "lib"]):
        content = data['content']
        for func_name, params_str in data['register_funcs']:
            # Skip if already known
            if func_name in _all_known_apis or func_name in heuristic_apis:
                continue
            # Look for callback-named parameter in the function signature
            params = [p.strip() for p in params_str.split(',') if p.strip()]
            cb_arg_index = -1
            for i, param in enumerate(params):
                param_name = param.split('=')[0].strip().rstrip(')')
                tokens = param_name.split()
                if tokens:
                    name = tokens[-1].lstrip('*&')
                    if any(name.endswith(suf) for suf in _CB_PARAM_SUFFIXES):
                        cb_arg_index = i
                        break
            if cb_arg_index >= 0:
                # Build regex pattern for this registration function
                regex_parts = [re.escape(func_name), r'\s*\(\s*']
                for i in range(len(params)):
                    if i > 0:
                        regex_parts.append(r'\s*,\s*')
                    if i == cb_arg_index:
                        regex_parts.append(r'(\w+)')
                    else:
                        regex_parts.append(r'[^,]*')
                regex_parts.append(r'\s*[,)]')
                heuristic_apis[func_name] = {
                    "register_func": func_name,
                    "regex": ''.join(regex_parts),
                    "cb_arg_index": cb_arg_index,
                    "concurrency_type": "callback_register",
                }

    # Build the result list from found known APIs + heuristic discoveries
    result = []
    for api_name in sorted(found_apis):
        info = _all_known_apis[api_name]
        result.append({
            "register_func": api_name,
            "regex": info["regex"],
            "cb_arg_index": info["cb_arg_index"],
            "concurrency_type": info["concurrency_type"],
        })

    # Add heuristic discoveries (limit to 30 to avoid noise)
    for func_name in sorted(heuristic_apis.keys())[:30]:
        info = heuristic_apis[func_name]
        result.append(info)

    return result


# ---------------------------------------------------------------------------
# Tree-sitter callback discovery — AST-level function pointer detection
# ---------------------------------------------------------------------------

def _try_init_treesitter_c():
    """Try to initialize tree-sitter C parser. Returns (parser, language) or (None, None)."""
    try:
        import tree_sitter_c as tsc
        from tree_sitter import Language, Parser
        lang = Language(tsc.language())
        parser = Parser(lang)
        return parser, lang
    except ImportError:
        return None, None


# Module-level tree-sitter objects (initialized lazily)
_ts_parser = None
_ts_lang = None


def _get_ts_parser():
    """Get or lazily initialize the tree-sitter C parser."""
    global _ts_parser, _ts_lang
    if _ts_parser is None:
        _ts_parser, _ts_lang = _try_init_treesitter_c()
    return _ts_parser, _ts_lang


def auto_discover_callbacks_treesitter(source_root: str,
                                        project_type: str = "",
                                        collector: SourceInfoCollector = None) -> list:
    """Discover callback patterns using tree-sitter AST analysis.

    Uses tree-sitter-c to parse C header files at the AST level, finding
    function declarations with function pointer parameters. This is much more
    accurate than regex-based detection because it properly handles:
      - Nested parentheses in parameter types
      - Complex type qualifiers (const, volatile, etc.)
      - Function pointer typedefs
      - Multiple levels of pointer indirection

    Falls back to regex-based detection if tree-sitter is not available.

    Args:
        source_root: Project source root directory.
        project_type: Detected project type.
        collector: Optional SourceInfoCollector for single-pass data.

    Returns:
        List of callback pattern dicts, same format as auto_detect_callback_patterns().
    """
    parser, lang = _get_ts_parser()
    if parser is None:
        # Fall back to regex-based detection
        return auto_detect_callback_patterns(source_root, project_type, collector)

    if collector is None:
        collector = SourceInfoCollector(source_root)

    _CB_PARAM_SUFFIXES = ('_cb', '_callback', '_handler', '_fn', '_func',
                          '_notif', '_notify', '_event_cb')

    heuristic_apis = {}  # register_func → {regex, cb_arg_index, concurrency_type}

    for rel_path, data in collector.iter_files(extensions={'.h'}, in_dirs=["include", "lib"]):
        content_bytes = data['content'].encode('utf-8')
        tree = parser.parse(content_bytes)
        root = tree.root_node

        # Walk the AST to find function declarations
        _visit_for_callbacks(root, content_bytes, heuristic_apis, _CB_PARAM_SUFFIXES)

    # Also scan .c files for callback registration function definitions
    for rel_path, data in collector.iter_files(extensions={'.c'}, in_dirs=["include", "lib"]):
        content_bytes = data['content'].encode('utf-8')
        tree = parser.parse(content_bytes)
        root = tree.root_node
        _visit_for_callbacks(root, content_bytes, heuristic_apis, _CB_PARAM_SUFFIXES)

    # Build result list, limiting to 50 entries to avoid noise
    result = []
    for func_name in sorted(heuristic_apis.keys())[:50]:
        result.append(heuristic_apis[func_name])

    # Verify candidates against actual call sites
    result = _verify_callback_candidates(result, collector)

    return result


def _visit_for_callbacks(node, source_bytes, heuristic_apis, cb_suffixes):
    """Recursively visit AST nodes to find function declarations with callback parameters."""
    # Check for function declaration or definition nodes
    if node.type in ('declaration', 'function_definition'):
        _extract_callback_from_decl(node, source_bytes, heuristic_apis, cb_suffixes)

    for child in node.children:
        _visit_for_callbacks(child, source_bytes, heuristic_apis, cb_suffixes)


def _extract_callback_from_decl(node, source_bytes, heuristic_apis, cb_suffixes):
    """Extract callback patterns from a function declaration AST node."""
    # For function_definition, the declarator is the function declarator
    # For declaration, we need to find the function declarator within
    func_declarator = None
    for child in node.children:
        if child.type == 'function_declarator':
            func_declarator = child
            break
        elif child.type == 'pointer_declarator':
            # Could be a function pointer declaration, check children
            for sub in child.children:
                if sub.type == 'function_declarator':
                    func_declarator = sub
                    break

    if func_declarator is None:
        return

    # Get the function name
    func_name = None
    for child in func_declarator.children:
        if child.type == 'identifier':
            func_name = source_bytes[child.start_byte:child.end_byte].decode('utf-8', errors='replace')
            break
        elif child.type == 'field_identifier':
            func_name = source_bytes[child.start_byte:child.end_byte].decode('utf-8', errors='replace')
            break

    if not func_name or func_name in heuristic_apis:
        return

    # Only consider functions with registration-like names
    _REG_SUFFIXES = ('_register', '_add', '_create', '_set', '_attach', '_install', '_subscribe')
    if not any(func_name.endswith(suf) for suf in _REG_SUFFIXES):
        return

    # Find parameter list
    param_list = None
    for child in func_declarator.children:
        if child.type == 'parameter_list':
            param_list = child
            break

    if param_list is None:
        return

    # Check each parameter for function pointer type
    cb_arg_index = -1
    params = []
    for i, param in enumerate(param_list.children):
        if param.type != 'parameter_declaration':
            continue
        params.append(param)

        # Check if this parameter is a function pointer
        is_func_ptr = _is_function_pointer_param(param)
        if is_func_ptr and cb_arg_index < 0:
            # Also check naming convention
            param_name = _get_param_name(param, source_bytes)
            if param_name and any(param_name.endswith(suf) for suf in cb_suffixes):
                cb_arg_index = len(params) - 1
            elif is_func_ptr:
                # Function pointer without callback naming — still a candidate
                cb_arg_index = len(params) - 1

    if cb_arg_index < 0:
        return

    # Build regex pattern
    n_params = len(params)
    regex_parts = [re.escape(func_name), r'\s*\(\s*']
    for i in range(n_params):
        if i > 0:
            regex_parts.append(r'\s*,\s*')
        if i == cb_arg_index:
            regex_parts.append(r'(\w+)')
        else:
            regex_parts.append(r'[^,]*')
    regex_parts.append(r'\s*[,)]')

    heuristic_apis[func_name] = {
        "register_func": func_name,
        "regex": ''.join(regex_parts),
        "cb_arg_index": cb_arg_index,
        "concurrency_type": "callback_register",
    }


def _is_function_pointer_param(param_node) -> bool:
    """Check if a parameter_declaration node is a function pointer."""
    for child in param_node.children:
        if child.type == 'pointer_declarator':
            for sub in child.children:
                if sub.type == 'function_declarator':
                    return True
        elif child.type == 'function_declarator':
            return True
    # Also check for parenthesized_declarator containing function_declarator
    for child in param_node.children:
        if child.type == 'parenthesized_declarator':
            for sub in child.children:
                if sub.type == 'pointer_declarator':
                    for ssub in sub.children:
                        if ssub.type == 'function_declarator':
                            return True
    return False


def _get_param_name(param_node, source_bytes) -> str | None:
    """Extract the parameter name from a parameter_declaration node."""
    # The name is usually the rightmost identifier in the declarator chain
    for child in reversed(param_node.children):
        if child.type in ('identifier', 'field_identifier'):
            return source_bytes[child.start_byte:child.end_byte].decode('utf-8', errors='replace')
        elif child.type in ('pointer_declarator', 'parenthesized_declarator'):
            for sub in reversed(child.children):
                if sub.type in ('identifier', 'field_identifier'):
                    return source_bytes[sub.start_byte:sub.end_byte].decode('utf-8', errors='replace')
    return None


# ---------------------------------------------------------------------------
# Phase 2: Test scan — discover test framework patterns and stub macros
# ---------------------------------------------------------------------------

def test_scan(source_root: str, collector: SourceInfoCollector = None) -> dict:
    """Scan test directories to discover test framework patterns.

    Uses SourceInfoCollector for single-pass data when available.

    Returns a dict with discovered:
      - test_framework_prefixes: test framework function prefixes
      - test_dirs_found: test directories that exist
    """
    findings = {
        "test_framework_prefixes": [],
        "test_dirs_found": [],
    }

    test_dirs = ["test", "ut", "tests", "unittest"]
    test_prefix_counter = Counter()

    if collector is None:
        collector = SourceInfoCollector(source_root)

    for test_dir in test_dirs:
        dir_path = os.path.join(source_root, test_dir)
        if not os.path.isdir(dir_path):
            continue
        findings["test_dirs_found"].append(test_dir)

        for rel_path, data in collector.iter_files(
                extensions={'.c', '.cpp', '.h'}, in_dirs=[test_dir]):
            content = data['content']

            # Detect CUnit
            if 'CU_' in content or 'CU_TEST' in content:
                if 'CU_' not in findings["test_framework_prefixes"]:
                    findings["test_framework_prefixes"].append('CU_')

            # Detect Google Test
            if 'TEST_F(' in content or 'TEST_P(' in content:
                pass  # C++ test framework, no prefix pattern

            # Detect custom test macros
            for macro_name, params_str, body in data['macros']:
                if 'TEST' in macro_name or macro_name == 'DEFINE_STUB':
                    if '_' in macro_name:
                        prefix = macro_name[:macro_name.index('_') + 1]
                        test_prefix_counter[prefix] += 1

    return findings


# ---------------------------------------------------------------------------
# Phase 3: Auto-config — merge findings into a profile JSON
# ---------------------------------------------------------------------------

def auto_config(source_root: str, prescan_result: dict = None,
                test_scan_result: dict = None,
                collector: SourceInfoCollector = None) -> dict:
    """Merge pre-scan and test scan findings into a profile JSON.

    Uses a single SourceInfoCollector for all auto-discovery functions,
    replacing the previous O(N×M) multi-traversal architecture with O(N).

    Project-specific configuration comes from profile templates; the tool code
    only provides generic heuristics and template merging logic.

    Args:
        source_root: Project source root directory.
        prescan_result: Output from prescan(). If None, runs prescan first.
        test_scan_result: Output from test_scan(). If None, runs test_scan first.
        collector: Optional SourceInfoCollector for single-pass data.

    Returns:
        A complete profile dict ready for ProfileSchema.from_dict().
    """
    # Create collector once for the entire auto_config pipeline
    if collector is None:
        collector = SourceInfoCollector(source_root)

    if prescan_result is None:
        prescan_result = prescan(source_root, collector)
    if test_scan_result is None:
        test_scan_result = test_scan(source_root, collector)

    # Detect project name using enhanced detection
    project_name = _detect_project_name(source_root)

    # Auto-detect project type
    project_type = detect_project_type(source_root)

    # Load project-type template if available
    template_data = None
    if project_type != "generic_c_cpp":
        template_path = _PROFILE_DIR / f"{project_type}.json"
        if template_path.exists():
            try:
                with open(template_path, 'r', encoding='utf-8') as f:
                    template_data = json.load(f)
            except (IOError, OSError, json.JSONDecodeError):
                template_data = None

    # Auto-detect struct_op_types (uses collector)
    struct_op_types = auto_detect_struct_op_types(source_root, collector)

    # Auto-detect api_prefixes from EXPORT_SYMBOL / visibility attributes (uses collector)
    auto_api_prefixes = auto_detect_api_prefixes(source_root, collector)

    # Auto-detect export macros (uses collector)
    export_macros = _auto_detect_export_macros(source_root, collector)
    # Merge with prefixes from prescan (prescan may find additional ones)
    prescan_prefixes = prescan_result.get("public_prefixes", [])
    merged_prefixes = list(dict.fromkeys(auto_api_prefixes + prescan_prefixes))

    # Auto-detect skip names from macros and project-type template (uses collector)
    auto_skip_names = auto_detect_skip_names(source_root, project_type, collector)

    # Auto-discover public header paths (uses collector)
    auto_header_paths = auto_discover_public_header_paths(source_root, project_type, collector)

    # Auto-infer internal patterns from export macros
    auto_internal_patterns = auto_infer_internal_patterns(source_root, export_macros)

    # Auto-infer domain rules from directory structure
    auto_domain_rules = auto_infer_domain_rules(source_root, project_type)

    # Auto-infer endpoint rules from project type
    auto_endpoint_rules = auto_infer_endpoint_rules(project_type, source_root, collector)

    # Auto-detect callback_patterns (uses tree-sitter if available, falls back to regex)
    auto_callback_patterns = auto_discover_callbacks_treesitter(source_root, project_type, collector)
    # Always include pthread_create as a base pattern; add any auto-detected ones
    base_callback_patterns = [
        {
            "register_func": "pthread_create",
            "regex": r"pthread_create\s*\(\s*[^,]*,\s*[^,]*,\s*(\w+)",
            "cb_arg_index": 2,
            "concurrency_type": "spawn_target",
        }
    ]
    # Merge: add auto-detected patterns not already in base
    base_funcs = {p["register_func"] for p in base_callback_patterns}
    for pat in auto_callback_patterns:
        if pat["register_func"] not in base_funcs:
            base_callback_patterns.append(pat)
            base_funcs.add(pat["register_func"])

    # Extract knowledge from build system files (meson.build, version.map, CMakeLists.txt)
    build_info = auto_discover_from_build_system(source_root, collector)
    # Merge build-system api_prefixes with auto-detected ones
    build_prefixes = build_info.get("api_prefixes", [])
    merged_prefixes = list(dict.fromkeys(merged_prefixes + build_prefixes))
    # Add build-system public header dirs
    build_header_dirs = build_info.get("public_header_dirs", [])
    auto_header_paths = list(dict.fromkeys(auto_header_paths + build_header_dirs))

    # If a template exists for this project type, use it as the base and
    # override with auto-detected findings. Otherwise build from scratch.
    if template_data:
        # Start from template (already has project-type-specific settings)
        profile = dict(template_data)
        # Override project name (template may have generic name)
        profile["project"] = dict(template_data.get("project", {}))
        profile["project"]["name"] = project_name
        profile["project"]["project_type"] = project_type
        profile["project"]["detected_frameworks"] = prescan_result.get("detected_frameworks", [])

        # Merge auto-detected struct_op_types into template
        template_structs = set(profile.get("api_detection", {}).get("struct_op_types", []))
        merged_structs = sorted(template_structs | set(struct_op_types))
        profile.setdefault("api_detection", {})["struct_op_types"] = merged_structs

        # Merge auto-detected api_prefixes
        template_prefixes = profile.get("api_detection", {}).get("public_prefixes", [])
        merged_api_prefixes = list(dict.fromkeys(template_prefixes + merged_prefixes))
        profile["api_detection"]["public_prefixes"] = merged_api_prefixes
        profile["api_detection"]["export_macros"] = list(set(
            profile.get("api_detection", {}).get("export_macros", []) + export_macros
        ))

        # Merge auto-detected skip_names
        template_skip = set(profile.get("skip_names", {}).get("add", []))
        profile["skip_names"]["add"] = sorted(template_skip | set(auto_skip_names))

        # Merge auto-detected public_header_paths
        template_hpaths = set(profile.get("api_detection", {}).get("public_header_paths", []))
        profile["api_detection"]["public_header_paths"] = sorted(template_hpaths | set(auto_header_paths))

        # Merge auto-inferred internal_patterns
        template_ip = set(profile.get("api_detection", {}).get("internal_patterns", []))
        profile["api_detection"]["internal_patterns"] = sorted(template_ip | set(auto_internal_patterns))

        # Merge auto-inferred domain_rules
        template_dr = profile.get("scan_hints", {}).get("domain_rules", [])
        existing_dr_patterns = {r["pattern"] for r in template_dr}
        for rule in auto_domain_rules:
            if rule["pattern"] not in existing_dr_patterns:
                template_dr.append(rule)
                existing_dr_patterns.add(rule["pattern"])
        profile["scan_hints"]["domain_rules"] = template_dr

        # Merge auto-inferred endpoint_rules
        template_er = profile.get("endpoint_classification", {}).get("endpoint_rules", [])
        existing_er_patterns = {r["pattern"] for r in template_er}
        for rule in auto_endpoint_rules:
            if rule["pattern"] not in existing_er_patterns:
                template_er.append(rule)
                existing_er_patterns.add(rule["pattern"])
        profile["endpoint_classification"]["endpoint_rules"] = template_er

        # Merge auto-detected callback_patterns
        template_cb_patterns = profile.get("callback_detection", {}).get("static_patterns", [])
        template_cb_funcs = {p["register_func"] for p in template_cb_patterns}
        for pat in auto_callback_patterns:
            if pat["register_func"] not in template_cb_funcs:
                template_cb_patterns.append(pat)
                template_cb_funcs.add(pat["register_func"])
        profile["callback_detection"]["static_patterns"] = template_cb_patterns

        # Merge auto-detected external_lib_prefixes
        template_ext = profile.get("skip_names", {}).get("external_lib_prefixes", {})
        for prefix, info in prescan_result.get("external_lib_prefixes", {}).items():
            if prefix not in template_ext:
                template_ext[prefix] = info
        profile["skip_names"]["external_lib_prefixes"] = template_ext

        # Merge macro_condition_prefixes
        template_macro_cond = set(profile.get("macro_heuristics", {}).get("macro_condition_prefixes", []))
        auto_macro_cond = set(prescan_result.get("macro_condition_prefixes", []))
        profile["macro_heuristics"]["macro_condition_prefixes"] = sorted(template_macro_cond | auto_macro_cond)

        # Merge endpoint_classification
        template_ec = profile.get("endpoint_classification", {})
        for prefix, info in prescan_result.get("external_lib_prefixes", {}).items():
            if prefix not in template_ec.get("lib_prefix_map", {}):
                template_ec.setdefault("lib_prefix_map", {})[prefix] = info["category"]
        profile["endpoint_classification"] = template_ec

        # Set phases
        profile["phases"] = {
            "prescan_completed": True,
            "test_scan_completed": True,
            "llm_header_analysis_completed": False,
            "llm_result_check_completed": False,
        }
    else:
        # No template — build from auto-detection results
        profile = {
            "version": 1,
            "project": {
                "name": project_name,
                "language": "c",
                "project_type": project_type,
                "detected_frameworks": prescan_result.get("detected_frameworks", []),
            },
            "skip_names": {
                "add": auto_skip_names,
                "external_lib_prefixes": prescan_result.get("external_lib_prefixes", {}),
                "test_framework_prefixes": test_scan_result.get("test_framework_prefixes", []),
            },
            "api_detection": {
                "public_prefixes": merged_prefixes,
                "internal_patterns": auto_internal_patterns,
                "public_header_paths": auto_header_paths,
                "struct_op_types": struct_op_types,
                "export_macros": export_macros,
                "auto_detect": True,
            },
            "callback_detection": {
                "static_patterns": base_callback_patterns,
                "generic_cb_suffixes": ["_cb", "_fn", "_handler", "_callback"],
            },
            "endpoint_classification": {
                "lib_prefix_map": {},
                "endpoint_rules": auto_endpoint_rules,
            },
            "macro_heuristics": {
                "macro_condition_prefixes": prescan_result.get("macro_condition_prefixes", []),
            },
            "scan_hints": {
                "domain_rules": auto_domain_rules,
                "header_priority_dirs": ["include"],
                "vtable_module_keys": [],
            },
            "phases": {
                "prescan_completed": True,
                "test_scan_completed": True,
                "llm_header_analysis_completed": False,
                "llm_result_check_completed": False,
            },
        }

    # Build endpoint_classification.lib_prefix_map from external_lib_prefixes
    for prefix, info in prescan_result.get("external_lib_prefixes", {}).items():
        profile["endpoint_classification"]["lib_prefix_map"][prefix] = info["category"]
    # Add universal POSIX prefixes
    profile["endpoint_classification"]["lib_prefix_map"]["pthread_"] = "external_posix"
    profile["endpoint_classification"]["lib_prefix_map"]["sem_"] = "external_posix"
    profile["endpoint_classification"]["lib_prefix_map"]["epoll_"] = "external_posix"

    # Auto-detect project_boundaries fields (non_api_paths, vendor prefixes)
    auto_non_api_paths = auto_detect_non_api_paths(source_root, collector)
    auto_vendor_prefixes = auto_detect_vendor_prefixes(source_root, collector)
    profile.setdefault("project_boundaries", {})
    template_pb = profile.get("project_boundaries", {})
    # Merge: template values first, then auto-detected (deduplicated)
    template_non_api = list(template_pb.get("non_api_paths", []))
    merged_non_api = list(dict.fromkeys(template_non_api + auto_non_api_paths))
    profile["project_boundaries"]["non_api_paths"] = merged_non_api
    template_vendor = list(template_pb.get("vendor_domain_prefixes", []))
    merged_vendor = list(dict.fromkeys(template_vendor + auto_vendor_prefixes))
    profile["project_boundaries"]["vendor_domain_prefixes"] = merged_vendor
    # Preserve any existing generic fields from template (test_path_patterns,
    # test_file_suffixes, test_domain_segments, external_dir_prefixes)
    for _field in ("test_path_patterns", "test_file_suffixes",
                   "test_domain_segments", "external_dir_prefixes"):
        if _field not in profile["project_boundaries"]:
            profile["project_boundaries"][_field] = template_pb.get(_field, [])

    # Auto-detect concurrency_patterns (lock acquire/release APIs)
    auto_lock_acquire, auto_lock_release = auto_detect_lock_patterns(source_root, collector)
    profile.setdefault("concurrency_patterns", {})
    template_cp = profile.get("concurrency_patterns", {})
    template_acquire = list(template_cp.get("lock_acquire_patterns", []))
    template_release = list(template_cp.get("lock_release_patterns", []))
    # Merge: template patterns first, then auto-detected (deduplicated)
    profile["concurrency_patterns"]["lock_acquire_patterns"] = \
        list(dict.fromkeys(template_acquire + auto_lock_acquire))
    profile["concurrency_patterns"]["lock_release_patterns"] = \
        list(dict.fromkeys(template_release + auto_lock_release))

    # Auto-detect io_classification keywords
    auto_io_main, auto_io_side = auto_detect_io_keywords(source_root, collector)
    profile.setdefault("io_classification", {})
    template_io = profile.get("io_classification", {})
    template_io_main = list(template_io.get("io_main_keywords", []))
    template_io_side = list(template_io.get("io_side_keywords", []))
    profile["io_classification"]["io_main_keywords"] = \
        list(dict.fromkeys(template_io_main + auto_io_main))
    profile["io_classification"]["io_side_keywords"] = \
        list(dict.fromkeys(template_io_side + auto_io_side))

    # Auto-detect scan_hints.vtable_module_keys (module-name binding keys)
    auto_vtable_keys = auto_detect_vtable_module_keys(source_root, collector)
    profile.setdefault("scan_hints", {})
    template_sh = profile.get("scan_hints", {})
    template_vtable_keys = list(template_sh.get("vtable_module_keys", []))
    # Merge: template keys first, then auto-detected (deduplicated)
    profile["scan_hints"]["vtable_module_keys"] = \
        list(dict.fromkeys(template_vtable_keys + auto_vtable_keys))
    # Preserve other scan_hints fields from template
    for _field in ("pre_scan_dirs", "test_dirs", "header_priority_dirs",
                   "domain_rules", "skip_dirs"):
        if _field not in profile["scan_hints"]:
            profile["scan_hints"][_field] = template_sh.get(_field, [])

    # Auto-detect threading_models
    auto_threading = auto_detect_threading_models(source_root, collector)
    profile.setdefault("threading_models", {})
    template_tm = profile.get("threading_models", {})
    # Merge: for each model, template patterns first then auto-detected
    merged_tm = {}
    all_models = set(template_tm.keys()) | set(auto_threading.keys())
    for model in all_models:
        template_patterns = list(template_tm.get(model, []))
        auto_patterns = list(auto_threading.get(model, []))
        # Deduplicate by pattern string
        seen = set()
        merged = []
        for p in template_patterns + auto_patterns:
            ps = p.get("pattern", "") if isinstance(p, dict) else str(p)
            if ps in seen:
                continue
            seen.add(ps)
            merged.append(p)
        if merged:
            merged_tm[model] = merged
    profile["threading_models"] = merged_tm

    # Discover macro dispatch patterns from headers
    # Registration macros come from the template; heuristic discovery adds more
    macro_discovery = discover_macro_dispatch(source_root, project_type, collector)
    profile["macro_dispatch"] = macro_discovery

    return profile


# ---------------------------------------------------------------------------
# Auto-detect project_boundaries.non_api_paths
# ---------------------------------------------------------------------------

# Generic directory name stems that typically hold non-API code (tools, tests,
# docs, samples). These are project-agnostic: a directory whose name contains
# one of these stems is very likely NOT on the project's public API surface.
# The detected paths are returned relative to source_root.
_NON_API_DIR_STEMS = (
    'tools', 'scripts', 'samples', 'examples', 'docs', 'documentation',
    'selftests', 'testing', 'test', 'tests', 'bench', 'benchmark',
    'demos', 'demo',
)


def auto_detect_non_api_paths(source_root: str, collector: SourceInfoCollector = None) -> list:
    """Detect directories that are NOT on the project's public API surface.

    Walks the source tree and identifies directories whose names match generic
    non-API stems (tools/, scripts/, samples/, docs/, selftests/, etc.). The
    returned paths are relative to source_root and use forward slashes.

    Project-agnostic: the heuristic is "directory name contains a non-API
    stem"; it does not hardcode any project-specific path.

    Returns:
        Sorted list of relative paths (e.g., ["scripts/", "tools/testing/"]).
    """
    non_api_paths = set()
    source_root_abs = os.path.abspath(source_root)
    # Single os.walk; we don't read file contents (collector skips these dirs,
    # so we enumerate them ourselves).
    for dirpath, dirnames, _ in os.walk(source_root_abs):
        # Don't descend into skip dirs (build artifacts, VCS, etc.)
        dirnames[:] = [d for d in dirnames
                       if not d.startswith('.') and d not in _SKIP_DIRS]
        for d in dirnames:
            d_lower = d.lower()
            if any(stem == d_lower or stem in d_lower for stem in _NON_API_DIR_STEMS):
                rel = os.path.relpath(os.path.join(dirpath, d), source_root_abs)
                # Normalize to forward slashes for cross-platform consistency
                rel = rel.replace(os.sep, '/') + '/'
                non_api_paths.add(rel)
    return sorted(non_api_paths)


# ---------------------------------------------------------------------------
# Auto-detect project_boundaries.vendor_domain_prefixes
# ---------------------------------------------------------------------------

# Generic vendor/external directory name stems. A top-level or near-top-level
# directory matching one of these stems is a vendor domain.
_VENDOR_DIR_STEMS = (
    'vendor', 'third_party', 'thirdparty', 'external', '3rdparty',
    'contrib', 'deps', 'dependencies',
)


def auto_detect_vendor_prefixes(source_root: str, collector: SourceInfoCollector = None) -> list:
    """Detect vendor/external directory prefixes in the source tree.

    Walks the source tree and identifies directories whose names match generic
    vendor stems (vendor/, third_party/, external/, etc.). The returned values
    are the directory names (not full paths) suitable for use as
    ``vendor_domain_prefixes`` in the profile (matched against domain prefixes
    during build).

    Project-agnostic: only generic vendor stems are matched.

    Returns:
        Sorted list of unique vendor directory names (e.g., ["vendor", "third_party"]).
    """
    vendor_prefixes = set()
    source_root_abs = os.path.abspath(source_root)
    for dirpath, dirnames, _ in os.walk(source_root_abs):
        # Don't descend into skip dirs (includes vendor dirs themselves —
        # we want to find them, not read their contents)
        for d in list(dirnames):
            d_lower = d.lower()
            if any(stem == d_lower for stem in _VENDOR_DIR_STEMS):
                vendor_prefixes.add(d_lower)
        # Prune so we don't recurse into vendor dirs
        dirnames[:] = [d for d in dirnames
                       if not d.startswith('.') and d not in _SKIP_DIRS
                       and d.lower() not in _VENDOR_DIR_STEMS]
    return sorted(vendor_prefixes)


# ---------------------------------------------------------------------------
# Auto-detect concurrency_patterns.lock_acquire_patterns / lock_release_patterns
# ---------------------------------------------------------------------------

# Generic function-name stems that indicate a lock-acquire API.
# Matched as substrings against callee names collected by SourceInfoCollector.
# Project-agnostic: these are naming conventions shared across POSIX, Linux
# kernel, SPDK, DPDK, FreeRTOS, etc.
#
# IMPORTANT: 'up' alone is intentionally omitted — it matches too many unrelated
# functions (setup, startup, cleanup, cup, update). Use 'up_read' / 'up_write'
# or anchored forms instead.
_LOCK_ACQUIRE_STEMS = (
    'mutex_lock', 'mutex_trylock', 'spin_lock', 'spin_trylock',
    'rw_lock', 'rwlock_', 'read_lock', 'write_lock',
    'rcu_read_lock', 'rcu_read_lock_bh', 'rcu_read_lock_sched',
    'down_read', 'down_write', 'down_read_trylock', 'down_write_trylock',
    'lock_acquire', '_acquire_lock', '_trylock',
    'pthread_mutex_lock', 'pthread_mutex_trylock',
    'pthread_rwlock_rdlock', 'pthread_rwlock_wrlock',
    'pthread_spin_lock', 'pthread_spin_trylock',
    'sem_wait', 'sem_trywait',
)
_LOCK_RELEASE_STEMS = (
    'mutex_unlock', 'spin_unlock', 'rw_unlock', 'rwlock_unlock',
    'read_unlock', 'write_unlock',
    'rcu_read_unlock', 'rcu_read_unlock_bh', 'rcu_read_unlock_sched',
    'up_read', 'up_write',
    'lock_release', '_release_lock', '_unlock',
    'pthread_mutex_unlock', 'pthread_rwlock_unlock',
    'pthread_spin_unlock',
    'sem_post', 'sem_release',
)

# Name fragments that disqualify a candidate from being a lock-acquire API
# (these indicate init, assertion, query, or state-check helpers, not lock
# acquisition). Used to filter false positives.
_LOCK_ACQUIRE_EXCLUDE = (
    '_init', '_assert', '_is_', '_can_', '_may_', '_locked', '_check',
    '_debug', '_print', '_log', '_warn', '_bug',
)
# Name fragments that disqualify a candidate from being a lock-release API.
_LOCK_RELEASE_EXCLUDE = (
    '_init', '_assert', '_is_', '_can_', '_may_', '_check',
    '_debug', '_print', '_log', '_warn', '_bug',
)


def _is_indirect_call(name: str) -> bool:
    """Return True if the callee name is an indirect call (member access)."""
    # Tree-sitter captures indirect calls like 'a->b' or 'a.b' as the callee
    # text. These are instance-specific and should not become generic keywords.
    return '->' in name or '.' in name


def auto_detect_lock_patterns(source_root: str, collector: SourceInfoCollector = None) -> tuple:
    """Detect lock acquire/release API patterns by scanning function calls.

    Examines the aggregated function-call data collected by
    SourceInfoCollector and identifies callee names that match generic
    lock-naming stems. Returns regex pattern strings suitable for use as
    ``concurrency_patterns.lock_acquire_patterns`` / ``lock_release_patterns``.

    Project-agnostic: only generic lock-naming stems are matched. The detected
    APIs are project-specific (whichever lock APIs the project actually calls).

    Conservative heuristic: indirect calls (a->b) and names matching exclude
    fragments (init, assert, is_, check, etc.) are filtered out. Accuracy is
    preferred over completeness — the profile can be supplemented later.

    Returns:
        (acquire_patterns, release_patterns) — each a sorted list of regex
        strings. Returns ([], []) if no lock APIs are found.
    """
    if collector is None:
        collector = SourceInfoCollector(source_root)

    # Aggregate all callee names across the project into a single set
    callee_names = set()
    for _, file_data in collector.iter_files(extensions={'.c', '.cpp'}):
        for call in file_data.get('calls', []):
            callee_names.add(call)

    acquire_apis = set()
    release_apis = set()
    for name in callee_names:
        if _is_indirect_call(name):
            continue
        if len(name) < 4:
            continue
        name_lower = name.lower()
        # Skip names that look like init/assert/check helpers
        if any(excl in name_lower for excl in _LOCK_ACQUIRE_EXCLUDE):
            # But still allow pure '_trylock' / 'mutex_lock' even if excluded
            # fragments appear — only skip if the acquire stem is weak.
            pass
        for stem in _LOCK_ACQUIRE_STEMS:
            if stem in name_lower:
                # Apply exclude filter: skip init/assert/check helpers
                if any(excl in name_lower for excl in _LOCK_ACQUIRE_EXCLUDE):
                    continue
                # Anchor to the function name boundary to avoid matching
                # unrelated substrings. Pattern: \b<name>\s*\(  — matches the
                # call site.
                acquire_apis.add(r'\b' + re.escape(name) + r'\s*\(')
                break
        for stem in _LOCK_RELEASE_STEMS:
            if stem in name_lower:
                if any(excl in name_lower for excl in _LOCK_RELEASE_EXCLUDE):
                    continue
                release_apis.add(r'\b' + re.escape(name) + r'\s*\(')
                break

    return sorted(acquire_apis), sorted(release_apis)


# ---------------------------------------------------------------------------
# Auto-detect io_classification.io_main_keywords / io_side_keywords
# ---------------------------------------------------------------------------

# Generic function-name stems that indicate a main IO data-path function.
_IO_MAIN_STEMS = (
    'submit', 'queue', 'ring', 'doorbell', 'write', 'read', 'send', 'recv',
    'transfer', 'dispatch', 'process_request', 'build_request',
    'cmd_', 'command', 'execute', 'xfer', 'io_path', 'io_submit',
    'post', 'rx_', 'tx_', 'enqueue', 'dequeue',
)
# Generic function-name stems that indicate a side-path (non-main-IO) function.
_IO_SIDE_STEMS = (
    'error', 'fail', 'abort', 'err_', '_err', 'retry', 'recover', 'resubmit',
    'timeout', 'watchdog', 'reset_', 'cleanup', 'clean_', 'destroy', 'destruct',
    'fini', 'dealloc', 'free_', '_free', '_stat', 'stat_', 'iostat', 'stats',
    'debug', 'log_', 'dump_', 'trace_', 'test_', '_test', 'unit_', '_ut', 'ut_',
    'validate', 'verify_', 'check_', 'poll_', '_poll', 'config', 'ioctl',
)


def auto_detect_io_keywords(source_root: str, collector: SourceInfoCollector = None) -> tuple:
    """Detect IO main/side keywords by analyzing function-name patterns.

    Examines the aggregated function-call data collected by
    SourceInfoCollector and identifies callee names that match generic
    IO-naming stems. Returns keyword lists suitable for use as
    ``io_classification.io_main_keywords`` / ``io_side_keywords``.

    Project-agnostic: only generic IO-naming stems are matched. The detected
    keywords are project-specific (whichever IO APIs the project actually uses).

    Conservative heuristic: indirect calls (a->b), names shorter than 4 chars,
    and one-off calls (count < 2) are filtered out. Accuracy is preferred over
    completeness — the profile can be supplemented later via LLM.

    Returns:
        (io_main_keywords, io_side_keywords) — each a sorted list of strings.
        Returns ([], []) if no IO-related functions are found.
    """
    if collector is None:
        collector = SourceInfoCollector(source_root)

    # Aggregate callee names and apply a frequency floor to filter noise
    from collections import Counter as _Counter
    callee_counter = _Counter()
    for _, file_data in collector.iter_files(extensions={'.c', '.cpp'}):
        for call in file_data.get('calls', []):
            if _is_indirect_call(call):
                continue
            if len(call) < 4:
                continue
            callee_counter[call] += 1

    io_main_keywords = set()
    io_side_keywords = set()
    for name, count in callee_counter.items():
        if count < 2:
            # Skip one-off calls — likely noise
            continue
        name_lower = name.lower()
        for stem in _IO_MAIN_STEMS:
            if stem in name_lower:
                io_main_keywords.add(name_lower)
                break
        for stem in _IO_SIDE_STEMS:
            if stem in name_lower:
                io_side_keywords.add(name_lower)
                break

    return sorted(io_main_keywords), sorted(io_side_keywords)


# ---------------------------------------------------------------------------
# vtable_module_keys auto-detection
# ---------------------------------------------------------------------------

# Common C/C++ macros and variables that hold the current module name.
# These are binding keys (looked up in `bindings` dict at query time), not
# module names themselves. Project-agnostic: any project using the standard
# MODULE_NAME / KBUILD_MODNAME / __MODULE_NAME convention will match.
_VTABLE_MODULE_KEY_CANDIDATES = (
    "module",                # Generic binding name
    "MODULE_NAME",           # Common macro
    "KBUILD_MODNAME",        # Linux kernel build system
    "__MODULE_NAME",         # Some projects use double-underscore form
    "THIS_MODULE",           # Linux kernel struct module*
)


def auto_detect_vtable_module_keys(source_root: str,
                                    collector: SourceInfoCollector = None) -> list:
    """Detect which module-name binding keys the project uses.

    Scans source files for references to common module-name macros/variables
    (MODULE_NAME, KBUILD_MODNAME, THIS_MODULE, etc.). Returns the subset
    actually referenced in the project, sorted by frequency (most frequent
    first). Used to populate ``scan_hints.vtable_module_keys``.

    Project-agnostic: only generic macro names are matched. The detected
    subset is project-specific (whichever macros the project actually uses).
    """
    if collector is None:
        collector = SourceInfoCollector(source_root)

    from collections import Counter as _Counter
    key_counter = _Counter()
    for rel_path, file_data in collector.iter_files(
            extensions={'.c', '.cpp', '.h', '.hpp'}):
        # Prefer content cached by collector; fall back to disk read
        text = file_data.get('content') if file_data else None
        if text is None:
            abs_path = os.path.join(source_root, rel_path)
            try:
                with open(abs_path, 'r', encoding='utf-8', errors='ignore') as f:
                    text = f.read()
            except (OSError, IOError):
                continue
        for key in _VTABLE_MODULE_KEY_CANDIDATES:
            # Word-boundary search to avoid false positives
            pattern = r'\b' + re.escape(key) + r'\b'
            count = len(re.findall(pattern, text))
            if count > 0:
                key_counter[key] += count

    # Return keys sorted by frequency (most frequent first), filter to those
    # that appeared at least once. "module" is always included as a generic
    # default if any module-related activity is detected.
    if not key_counter:
        return []
    result = [k for k, _ in key_counter.most_common()]
    # Always put "module" first if present (it's the canonical binding name)
    if "module" in result:
        result.remove("module")
        result.insert(0, "module")
    return result


# ---------------------------------------------------------------------------
# threading_models auto-detection
# ---------------------------------------------------------------------------

# Generic thread-creation function names grouped by threading model.
# Project-agnostic: matches common thread-spawn APIs across C/C++.
# Each stem is matched as a substring in callee names (case-insensitive).
_THREADING_MODEL_STEMS = {
    "kernel_thread": (
        "kthread_create", "kthread_run", "kthread_bind",
        "INIT_WORK", "INIT_DELAYED_WORK", "INIT_DEFERRABLE_WORK",
        "tasklet_init", "schedule_work", "schedule_delayed_work",
        "queue_work",
    ),
    "posix_thread": (
        "pthread_create", "pthread_spawn",
    ),
    "cpp_thread": (
        "std::thread", "std::async", "std::launch",
        "make_thread",
    ),
    "user_thread": (
        "clone", "vfork", "spawn_thread",
    ),
}


def auto_detect_threading_models(source_root: str,
                                  collector: SourceInfoCollector = None) -> dict:
    """Detect threading models used by the project.

    Scans aggregated call data for thread-creation function names. Returns a
    dict suitable for ``threading_models`` field: {model_name: [{pattern: r"..."}]}.

    Project-agnostic: only generic thread-spawn stems are matched. The
    detected models are project-specific (whichever thread APIs the project
    actually uses).

    Returns:
        {model_name: [{"pattern": r"\\b<func>\\b"}, ...]} — empty dict if
        no thread-creation calls found.
    """
    if collector is None:
        collector = SourceInfoCollector(source_root)

    from collections import Counter as _Counter
    callee_counter = _Counter()
    for _, file_data in collector.iter_files(extensions={'.c', '.cpp'}):
        for call in file_data.get('calls', []):
            if _is_indirect_call(call):
                continue
            if len(call) < 4:
                continue
            callee_counter[call] += 1

    detected = {}
    for model_name, stems in _THREADING_MODEL_STEMS.items():
        patterns = []
        seen = set()
        for name, count in callee_counter.items():
            if count < 1:
                continue
            # Match by exact name OR by stem-in-name (case-sensitive for
            # C macros like INIT_WORK which are uppercase)
            matched = False
            for stem in stems:
                if stem == name or stem in name:
                    matched = True
                    break
            if not matched:
                continue
            pattern_str = r'\b' + re.escape(name) + r'\b'
            if pattern_str in seen:
                continue
            seen.add(pattern_str)
            patterns.append({"pattern": pattern_str})
        if patterns:
            detected[model_name] = patterns

    return detected


# ---------------------------------------------------------------------------
# Macro dispatch discovery: find constructor macros from headers
# ---------------------------------------------------------------------------

def discover_macro_dispatch(source_root: str, project_type: str = "",
                            collector: SourceInfoCollector = None) -> dict:
    """Scan header files for constructor-based registration macros.

    Uses SourceInfoCollector for single-pass data when available.

    Looks for patterns like:
        #define PROJ_REGISTER(name) \\
            __attribute__((constructor)) static void name##_register(void) { \\
                proj_add_module(&name); \\
            }

    Also detects registration macros from the project-type template and via
    heuristic naming patterns (*_REGISTER*, *_INIT* naming patterns).

    Only truly generic macro naming patterns are hardcoded here.
    Project-specific macros (RTE_INIT, module_init, etc.) come from
    profile templates.

    Returns:
        {
            "registration_macros": [...],
            "token_paste_macros": [...],
        }
    """
    registration_macros = []
    token_paste_macros = []

    # Regex patterns for discovery
    _CONSTRUCTOR_MACRO_RE = re.compile(
        r'#define\s+(\w+)\s*\(([^)]*)\)\s*'      # #define MACRO(params)
        r'(?:\\\n\s*)*'                            # line continuations
        r'(?:static\s+void\s+)?'                   # optional "static void" prefix
        r'__attribute__\s*\(\s*\(\s*constructor'   # __attribute__((constructor
        r'(?:\s*\(\s*\d+\s*\))?'                   # optional priority like (1000)
    )
    _TOKEN_PASTE_RE = re.compile(r'(\w+)##(\w*)')
    _FUNC_CALL_RE = re.compile(r'\b(\w+)\s*\(')

    # Load known registration macros from template
    _template_known_macros = {}
    templates = _load_all_templates()
    if project_type in templates:
        tmpl = templates[project_type]
        for rm in tmpl.get("macro_dispatch", {}).get("registration_macros", []):
            macro_name = rm.get("macro_name", "")
            if macro_name:
                _template_known_macros[macro_name] = {
                    "n_params": rm.get("pattern", "").count("([^,)]+)") or 1,
                    "struct_arg_index": rm.get("struct_arg_index", 0),
                    "register_func": rm.get("register_func", ""),
                    "generates": rm.get("generates", "constructor"),
                    "_confidence": "high",
                }

        # Also load token_paste_macros from template
        for tp in tmpl.get("macro_dispatch", {}).get("token_paste_macros", []):
            token_paste_macros.append(tp)

    # Generic regex for detecting registration macro naming patterns in #define lines
    _REG_MACRO_NAME_RE = re.compile(
        r'#define\s+(\w*(?:_REGISTER|_INIT(?:_PRIO)?|_REGISTER_PCI|_REGISTER_VDEV|_REGISTER_DRIVER|_REGISTER_BUS|_REGISTER_AUX|_initcall|module_init|module_exit)\w*)\s*[\(|\n]'
    )

    if collector is None:
        collector = SourceInfoCollector(source_root)

    # Scan header files using collector data
    for rel_path, data in collector.all_header_files():
        source = data['content']

        # Phase 1: Find constructor macros
        for m in _CONSTRUCTOR_MACRO_RE.finditer(source):
            macro_name = m.group(1)
            params_str = m.group(2).strip()

            # Parse parameters
            params = [p.strip() for p in params_str.split(',') if p.strip()]
            if not params:
                continue

            # Skip if already in known macros
            if macro_name in _template_known_macros:
                continue

            # Extract the macro body (from #define to end of statement)
            body_start = m.end()
            body_lines = []
            pos = source.find('#define ' + macro_name, m.start())
            lines = source[pos:].split('\n')
            for line in lines[1:]:
                stripped = line.rstrip()
                body_lines.append(stripped)
                if not stripped.endswith('\\'):
                    break

            body_text = '\n'.join(body_lines)

            # Find register function call in constructor body
            register_func = ""
            brace_start = body_text.find('{')
            brace_body = body_text[brace_start + 1:] if brace_start >= 0 else body_text
            for cm in _FUNC_CALL_RE.finditer(brace_body):
                fname_candidate = cm.group(1)
                if fname_candidate in ('__attribute__', 'static', 'void',
                                       'return', 'if', 'sizeof'):
                    continue
                is_param_ref = False
                for param in params:
                    clean_param = param.lstrip('_')
                    if clean_param and fname_candidate.endswith(clean_param):
                        is_param_ref = True
                        break
                if is_param_ref:
                    continue
                register_func = fname_candidate
                break

            # Find ## token-paste expressions
            paste_exprs = _TOKEN_PASTE_RE.findall(body_text)

            struct_arg_index = 0  # Default to first param

            reg_entry = {
                "macro_name": macro_name,
                "pattern": _build_invocation_regex(macro_name, len(params)),
                "struct_arg_index": struct_arg_index,
            }
            if register_func:
                reg_entry["register_func"] = register_func

            if register_func:
                reg_entry["_confidence"] = "low"
                reg_entry["_needs_review"] = True

            registration_macros.append(reg_entry)

            # Build token_paste_macros entry if ## found
            if paste_exprs:
                template = body_text
                for param in params:
                    clean_param = param.lstrip('_')
                    template = template.replace(param, clean_param)

                for left, right in paste_exprs:
                    tp_entry = {
                        "macro_name": macro_name,
                        "template": f"{left}##{right}",
                        "param_names": [p.lstrip('_') for p in params],
                        "generates": "constructor",
                    }
                    token_paste_macros.append(tp_entry)
                    break

        # Phase 2: Detect known registration macros by name pattern
        for m in _REG_MACRO_NAME_RE.finditer(source):
            macro_name = m.group(1)
            if macro_name in _template_known_macros:
                info = _template_known_macros[macro_name]
                if any(r["macro_name"] == macro_name for r in registration_macros):
                    for r in registration_macros:
                        if r["macro_name"] == macro_name:
                            r["_confidence"] = info["_confidence"]
                            r["generates"] = info.get("generates", "")
                    continue

                n_params = info["n_params"]
                reg_entry = {
                    "macro_name": macro_name,
                    "pattern": _build_invocation_regex(macro_name, n_params),
                    "struct_arg_index": info["struct_arg_index"],
                    "_confidence": info["_confidence"],
                    "generates": info.get("generates", ""),
                }
                if info.get("register_func"):
                    reg_entry["register_func"] = info["register_func"]
                registration_macros.append(reg_entry)

        # Phase 3: Heuristic - detect macros that reference other
        # registration macros (chain expansion).
        # Use collector's all_macros for transitive checks instead of
        # re-parsing the file.
        _CHAIN_MACRO_RE = re.compile(
            r'#define\s+(\w+)\s*\(([^)]*)\)\s*(?:\\\n\s*)*\s*(\w+)\s*\(',
            re.MULTILINE
        )
        for m in _CHAIN_MACRO_RE.finditer(source):
            macro_name = m.group(1)
            delegating_to = m.group(3)
            if any(r["macro_name"] == macro_name for r in registration_macros):
                continue
            is_registration_delegate = (
                delegating_to in _template_known_macros or
                any(r["macro_name"] == delegating_to for r in registration_macros) or
                _is_transitive_registration(delegating_to, collector.all_macros,
                                            _template_known_macros, registration_macros, depth=0)
            )
            if is_registration_delegate:
                params_str = m.group(2).strip()
                params = [p.strip() for p in params_str.split(',') if p.strip()]
                n_params = len(params) if params else 1
                reg_entry = {
                    "macro_name": macro_name,
                    "pattern": _build_invocation_regex(macro_name, n_params),
                    "struct_arg_index": 0,
                    "_confidence": "medium",
                    "generates": "constructor",
                }
                registration_macros.append(reg_entry)

    # Phase 4: Detect macros that use __attribute__((section(...))) for registration.
    # These are common in Linux kernel (initcall macros, module_init, etc.)
    section_macros = _detect_section_attribute_macros(collector.all_macros)
    for macro_name in section_macros:
        if any(r["macro_name"] == macro_name for r in registration_macros):
            continue
        # Find the macro definition to get param count
        params_str = ""
        for mn, ps, body in [(mn, ps, body)
                             for rel_p, fd in collector.all_header_files()
                             for mn, ps, body in fd['macros']
                             if mn == macro_name]:
            params_str = ps
            break
        params = [p.strip() for p in params_str.split(',') if p.strip()]
        n_params = len(params) if params else 1
        reg_entry = {
            "macro_name": macro_name,
            "pattern": _build_invocation_regex(macro_name, n_params),
            "struct_arg_index": 0,
            "_confidence": "medium",
            "generates": "section_init",
        }
        registration_macros.append(reg_entry)

    # Phase 5: Full macro graph BFS expansion — find ALL macros that
    # transitively reference any known registration macro, including those
    # discovered in Phases 1-4. Uses the global macro graph from collector.
    _macro_graph = _build_macro_dependency_graph(collector.all_macros)
    known_reg_names = set(_template_known_macros.keys()) | {r["macro_name"] for r in registration_macros}
    # BFS from each known registration macro to find all macros that reference them
    reachable_to_known = set()
    # Reverse the graph: for each macro, which macros reference it?
    reverse_graph = defaultdict(set)
    for src, targets in _macro_graph.items():
        for tgt in targets:
            reverse_graph[tgt].add(src)
    # BFS from known registration macros through reverse graph
    queue = list(known_reg_names)
    visited_bfs = set(known_reg_names)
    while queue:
        current = queue.pop(0)
        for caller in reverse_graph.get(current, []):
            if caller not in visited_bfs:
                visited_bfs.add(caller)
                queue.append(caller)
                # This caller is transitively a registration macro
                if not any(r["macro_name"] == caller for r in registration_macros):
                    # Find param count for this macro
                    params_str = ""
                    for mn, ps, body in [(mn, ps, body)
                                         for rel_p, fd in collector.all_header_files()
                                         for mn, ps, body in fd['macros']
                                         if mn == caller]:
                        params_str = ps
                        break
                    params = [p.strip() for p in params_str.split(',') if p.strip()]
                    n_params = len(params) if params else 1
                    reg_entry = {
                        "macro_name": caller,
                        "pattern": _build_invocation_regex(caller, n_params),
                        "struct_arg_index": 0,
                        "_confidence": "medium",
                        "generates": "constructor",
                    }
                    registration_macros.append(reg_entry)

    # Add template-provided registration macros (those not already discovered by scanning)
    discovered_names = {r["macro_name"] for r in registration_macros}
    for macro_name, info in _template_known_macros.items():
        if macro_name not in discovered_names:
            reg_entry = {
                "macro_name": macro_name,
                "pattern": _build_invocation_regex(macro_name, info["n_params"]),
                "struct_arg_index": info["struct_arg_index"],
                "_confidence": info["_confidence"],
                "generates": info.get("generates", ""),
            }
            if info.get("register_func"):
                reg_entry["register_func"] = info["register_func"]
            registration_macros.append(reg_entry)

    # Deduplicate registration_macros by macro_name (keep highest confidence)
    seen_macros = {}
    for entry in registration_macros:
        name = entry["macro_name"]
        if name not in seen_macros:
            seen_macros[name] = entry
        else:
            existing = seen_macros[name]
            _CONF_ORDER = {"high": 3, "medium": 2, "low": 1}
            if _CONF_ORDER.get(entry.get("_confidence", "low"), 0) > _CONF_ORDER.get(existing.get("_confidence", "low"), 0):
                seen_macros[name] = entry

    return {
        "registration_macros": list(seen_macros.values()),
        "token_paste_macros": token_paste_macros,
    }


def _build_invocation_regex(macro_name: str, n_params: int) -> str:
    """Build a regex pattern for matching a macro invocation with n_params args.

    Example: _build_invocation_regex("FOO", 2) → "FOO\\s*\\(\\s*([^,)]+)\\s*,\\s*([^,)]+)\\s*\\)"
    """
    parts = [re.escape(macro_name), r'\s*\(\s*']
    for i in range(n_params):
        if i > 0:
            parts.append(r'\s*,\s*')
        parts.append(r'([^,)]+)')
    parts.append(r'\s*\)')
    return ''.join(parts)


def _is_transitive_registration(macro_name: str, all_macros: dict,
                                 template_known: dict, discovered: list,
                                 depth: int = 0) -> bool:
    """Check if a macro transitively references a known registration macro.

    Uses BFS with unlimited depth (bounded by visited set) on the global
    macro dependency graph. A macro is transitively a registration macro
    if its body references a macro that is either in template_known or
    already in the discovered list, or if any macro it references
    transitively qualifies.

    Args:
        macro_name: The macro to check.
        all_macros: Dict of macro_name → body text (global, from collector).
        template_known: Template-provided known registration macros.
        discovered: List of already-discovered registration macro dicts.
        depth: Recursion depth (kept for backward compat, but BFS is unbounded).

    Returns:
        True if the macro transitively references a registration macro.
    """
    # Build seed set of known registration macro names
    known_names = set(template_known.keys()) | {e["macro_name"] for e in discovered}

    # BFS from macro_name through the macro dependency graph
    visited = set()
    queue = [macro_name]
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        body = all_macros.get(current, "")
        if not body:
            continue

        # Check if body directly references any known registration macro
        for known_name in known_names:
            if known_name in body:
                return True

        # Find all macro names referenced in body and add to queue
        for other_name in all_macros:
            if other_name != current and other_name in body and other_name not in visited:
                queue.append(other_name)

    return False


def _build_macro_dependency_graph(all_macros: dict) -> dict:
    """Build a directed dependency graph from macro definitions.

    For each macro, find all other macro names referenced in its body.
    Returns adjacency dict: macro_name → set of referenced macro names.
    """
    graph = defaultdict(set)
    all_names = set(all_macros.keys())
    for macro_name, body in all_macros.items():
        for other in all_names:
            if other != macro_name and other in body:
                graph[macro_name].add(other)
    return dict(graph)


def _detect_section_attribute_macros(all_macros: dict) -> list:
    """Detect macros that use __attribute__((section(...))) for registration.

    In Linux kernel and similar projects, registration is often done via
    section attributes that place function pointers into special linker
    sections (e.g., .initcall, .data..percpu).

    Returns:
        List of macro names that use section attributes.
    """
    section_macros = []
    _SECTION_RE = re.compile(r'__attribute__\s*\(\s*\(\s*section\s*\(\s*"([^"]*)"')
    _SECTION_REGISTRATION_PATTERNS = {
        'initcall', '.init.', '.exit.', 'module', '.data..percpu',
        '.rodata', '.modinfo',
    }

    for macro_name, body in all_macros.items():
        for m in _SECTION_RE.finditer(body):
            section_name = m.group(1)
            if any(pat in section_name for pat in _SECTION_REGISTRATION_PATTERNS):
                section_macros.append(macro_name)
                break

    return section_macros


# ---------------------------------------------------------------------------
# Write auto-generated profile to .code2database_profile.json
# ---------------------------------------------------------------------------

def write_auto_profile(source_root: str, output_dir: str = None) -> str:
    """Run full auto-detection and write profile to .code2database_profile.json.

    This is the main entry point for --auto-profile: runs prescan, test_scan,
    and auto_config (which includes project type, struct_op_types, api_prefixes,
    and callback_patterns detection), then writes the resulting profile.

    Args:
        source_root: Project source root directory.
        output_dir: Directory to write the profile file. Defaults to source_root.

    Returns:
        Path to the written profile file.
    """
    profile_dict = auto_config(source_root)

    # Determine output path
    if output_dir is None:
        output_dir = source_root
    os.makedirs(output_dir, exist_ok=True)
    profile_path = os.path.join(output_dir, ".code2database_profile.json")

    with open(profile_path, 'w', encoding='utf-8') as f:
        json.dump(profile_dict, f, indent=2, ensure_ascii=False)

    return profile_path
