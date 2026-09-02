#!/usr/bin/env python3
"""C/C++ scanner for invocation graph extraction using tree-sitter."""

import os
import re
from _scanner.base import BaseScanner
from _detector.build_detector import evaluate_pp_condition
import logging

# Module-level compiled regexes (hoisted from function bodies)
_MACRO_ASM_RE = re.compile(
    r'__asm__\s*(?:__volatile__\s*)?\(|'
    r'asm\s+(?:inline\s+)?(?:volatile\s+)?\(|'
    r'asm\s*\(',
    re.IGNORECASE
)
_REG_DECL_RE = re.compile(
    r'\bregister\s+\w+\s+\*?\s*'
    r'(\w+)\s+'
    r'__asm__\s*\(\s*"([xw]\d+|[a-z]{2,4}\d*)"\s*\)'
    r'\s*=\s*'
    r'(\([^)]*\)\s*)?'
    r'(\w+)',
)
_STATIC_ARRAY_RE = re.compile(
    r'\bstatic\s+(?:const\s+)?'
    r'\w+\s+\*?\s*(?:const\s+)?'
    r'(\w+)\s*\[\s*\]\s*=\s*\{([^}]+)\}',
    re.MULTILINE
)

# Pre-compiled coroutine keyword + static-prefix patterns.
_COROUTINE_KEYWORD_RES = tuple(
    re.compile(rf'\b{kw}\b') for kw in ('co_await', 'co_yield', 'co_return')
)
_STATIC_PREFIX_RE = re.compile(r'\s*static\s+')


try:
    import tree_sitter_c as tsc
    import tree_sitter_cpp as tscpp
    from tree_sitter import Language, Parser
    _HAS_C = True
except ImportError:
    _HAS_C = False


# Preprocessor condition regex (tree-sitter doesn't parse #ifdef well)
# Use [^\S\n]* (match whitespace except newline) to prevent \s* from crossing line boundaries
# This avoids #endif\n\treturn foo() being parsed as endif with condition "return foo()"
_PP_COND_RE = re.compile(r'^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b[^\S\n]*(.*)', re.MULTILINE)
_PP_ELSE_RE = re.compile(r'^\s*#\s*(else|elif)\b[^\S\n]*(.*)', re.MULTILINE)
_PP_ENDIF_RE = re.compile(r'^\s*#\s*endif\b', re.MULTILINE)
# Static patterns used inside methods — hoisted to avoid recompiling
# on every call (1.5M+ calls on kernel-scale projects).
_FUNC_DECL_RE = re.compile(r'\b(\w+)\s*\([^)]*\)\s*\{', re.MULTILINE)
_ARR_ELEM_RE = re.compile(r'\b(\w+)\s*=\s*\(.*?\)\s*(\w+)\s*\[', re.MULTILINE)

# Pre-compiled label detection patterns (avoid re-compiling per function)
_THREAD_PATTERNS = [re.compile(p) for p in [
    r'\bpthread_create\s*\(', r'\bstd::thread\s*\(', r'\bspawn_thread\s*\(']]
_CALLBACK_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
    r'\bcallback\s*[\(=]', r'\bregister_callback\s*\(',
    r'\bfn_arg\s*[\(=]', r'\b\.cb\s*[\(=]']]
_CTOR_NAME_RE = re.compile(r'\b(?:__init__|__ctor__|_init\b|constructor\b)', re.IGNORECASE)
_DTOR_NAME_RE = re.compile(r'\b(?:__fini__|__dtor__|_fini\b|_destroy\b|destructor\b)', re.IGNORECASE)

# Inline-asm call/jump/syscall patterns extracted from #define macro bodies.
# Each tuple: (compiled_regex, confidence, score, special_tag).
_MACRO_ASM_CALL_RES = [
    (re.compile(r'\bcall\s+([a-zA-Z_]\w*)'), "INFERRED", 0.7, ""),
    (re.compile(r'\bjmp\s+([a-zA-Z_]\w*)'), "INFERRED", 0.6, ""),
    (re.compile(r'\bbl\s+([a-zA-Z_]\w*)'), "INFERRED", 0.7, ""),
    (re.compile(r'\bjal\s+([a-zA-Z_]\w*)'), "INFERRED", 0.7, ""),
    (re.compile(r'\bbrasl\s+([a-zA-Z_]\w*)'), "INFERRED", 0.7, ""),
    (re.compile(r'\bsyscall\b'), "INFERRED", 0.5, "syscall"),
    (re.compile(r'\bsvc\s+#0\b'), "INFERRED", 0.5, "svc"),
    (re.compile(r'\becall\b'), "INFERRED", 0.5, "ecall"),
    (re.compile(r'\bvmcall\b'), "INFERRED", 0.5, "vmcall"),
    (re.compile(r'\bvmmcall\b'), "INFERRED", 0.5, "vmmcall"),
    (re.compile(r'\blcallw?\s+\*'), "AMBIGUOUS", 0.3, "lcall"),
    (re.compile(r'\bcall\s+\*\s*%([a-zA-Z_]\w*)'), "AMBIGUOUS", 0.3, "indirect_call"),
]

# Pre-compiled inline-asm instruction patterns, reusing the compiled regex
# objects from _MACRO_ASM_CALL_RES so the inline-asm path never recompiles
# the same instruction patterns per call. Each named handle aliases the
# compiled regex at the matching list index.
_ASM_DIRECT_CALL_RE = _MACRO_ASM_CALL_RES[0][0]          # call <label>
_ASM_TAIL_JMP_RE = _MACRO_ASM_CALL_RES[1][0]              # jmp <label>
_ASM_DIRECT_BL_RE = _MACRO_ASM_CALL_RES[2][0]             # bl <label> (ARM)
_ASM_SYSCALL_RE = _MACRO_ASM_CALL_RES[5][0]                # syscall
_ASM_INDIRECT_CALL_REG_RE = _MACRO_ASM_CALL_RES[11][0]    # call *%reg

# Identifier-followed-by-call-paren pattern for macro body call extraction.
_IDENT_CALL_RE = re.compile(r'\b([a-zA-Z_]\w*)\s*\(')


def _char_offsets_to_bytes(source_text: str, offsets):
    """Convert char offsets in source_text to byte offsets in its UTF-8 form.

    Tree-sitter node offsets are BYTE offsets into the parse input, while
    regex matches over the decoded text yield CHAR offsets. For pure-ASCII
    text both are identical (fast path — no conversion cost). For text with
    multi-byte characters (valid UTF-8 content, or U+FFFD from
    errors='replace' decoding), the two diverge and every comparison of a
    node offset against a regex offset after the first multi-byte char is
    wrong without this conversion.
    """
    offsets = list(offsets)
    if not offsets:
        return offsets
    if source_text.isascii():
        return offsets
    uniq = sorted(set(offsets))
    byte_map = {}
    byte_pos = 0
    char_pos = 0
    for u in uniq:
        byte_pos += len(source_text[char_pos:u].encode('utf-8'))
        char_pos = u
        byte_map[u] = byte_pos
    return [byte_map[o] for o in offsets]


def _collect_call_expressions(expr_node, out):
    """Collect call_expression nodes at any depth of an expression subtree.

    Pre-order traversal in child order (= source order of call sites).
    Does NOT descend into found call_expressions — the caller dispatches
    each to _process_node, which handles that call's own arguments
    recursively — nor into lambda bodies (calls there belong to the
    lambda's own function, not the enclosing one).
    """
    if expr_node is None:
        return
    _t = expr_node.type
    if _t == 'call_expression':
        out.append(expr_node)
        return
    if _t == 'lambda_expression':
        return
    for ch in expr_node.children:
        _collect_call_expressions(ch, out)


class CTreeSitterScanner(BaseScanner):
    """Scanner for C and C++ using tree-sitter."""

    def __init__(self, is_cpp: bool = False):
        if not _HAS_C:
            raise ImportError("C/C++ scanner requires tree-sitter-c and tree-sitter-cpp. Install: pip install tree-sitter-c tree-sitter-cpp")
        lang_mod = tscpp if is_cpp else tsc
        self.lang = Language(lang_mod.language())
        self.parser = Parser(self.lang)
        self.is_cpp = is_cpp
        self._struct_op_types = []  # Set by scanner factory from profile
        self._macro_dispatch_patterns = {}  # Set by scanner factory from profile macro_dispatch.registration_macros
        self._callback_patterns = {}  # Set by scanner factory from profile callback_patterns: {register_func: (cb_arg_index, concurrency_type)}
        self._api_prefixes = []  # Set by scanner factory from profile api_detection.public_prefixes
        self._export_macros = []  # Set by scanner factory from profile api_detection.export_macros
        self._public_header_paths = []  # Set by scanner factory from profile api_detection.public_header_paths
        self._non_api_paths = []  # Set by scanner factory from profile project_boundaries.non_api_paths
        # O8: when True, the identifier-name heuristic for fn_ptr_calls is
        # disabled — only explicit field_expression / pointer_expression
        # calleys count as fn_ptr evidence. Set from profile.dispatch_tuning.
        self._fn_ptr_call_require_evidence = False
        self._ifdef_stack = []  # Stack of preprocessor condition strings for current nesting
        self._export_macro_re_cache: dict = {}  # sorted tuple → compiled regex

    def _get_export_macro_re(self, macros_key: tuple):
        """Return a compiled regex for the given sorted tuple of export macro
        names. Cached per-instance so repeated scan_file calls with the same
        profile don't recompile the regex."""
        cached = self._export_macro_re_cache.get(macros_key)
        if cached is not None:
            return cached
        pat = re.compile(
            r'(?:' + '|'.join(re.escape(m) for m in macros_key) + r')\s*\(\s*(\w+)\s*\)'
        )
        self._export_macro_re_cache[macros_key] = pat
        return pat

    # Regex to strip __attribute__((...)) that confuses tree-sitter-c
    # Handles nested parentheses: __attribute__((packed)), __attribute__((naked)), etc.
    # Nested-paren-safe: handles one nesting level, which covers all
    # real attributes (aligned(8), format(printf,1,2), section("...")).
    # The old [^)]* stopped at the FIRST ')' — __attribute__((aligned(8)))
    # left a stray ')' in the text fed to tree-sitter (ERROR nodes, lost
    # functions).
    _ATTR_STRIP_RE = re.compile(
        r'__attribute__\s*\(\((?:[^()]|\([^()]*\))*\)\)')
    # Regex to find MSVC __asm { body } blocks — store for post-processing
    _MSVC_ASM_RE = re.compile(r'__asm\s*\{([^}]*)\}', re.MULTILINE)
    # Linux 5.11+ uses asm_inline as a keyword equivalent to asm
    _ASM_INLINE_RE = re.compile(r'\basm_inline\b')

    def _parse(self, source_bytes: bytes):
        source_text = source_bytes.decode('utf-8', errors='replace')
        source_text = self._ATTR_STRIP_RE.sub(lambda m: ' ' * len(m.group(0)), source_text)
        source_text = self._ASM_INLINE_RE.sub('__asm__   ', source_text)
        self._msvc_asm_blocks = []
        for m in self._MSVC_ASM_RE.finditer(source_text):
            self._msvc_asm_blocks.append({
                "body": m.group(1).strip(),
                # m.start()/m.end() are CHAR offsets in the decoded text,
                # but consumers compare these against tree node start_byte
                # (byte offsets) — convert so the containment check works
                # on files with multi-byte characters.
                "start_byte": _char_offsets_to_bytes(
                    source_text, [m.start()])[0],
                "end_byte": _char_offsets_to_bytes(
                    source_text, [m.end()])[0],
            })
        source_text = self._MSVC_ASM_RE.sub(lambda m: ' ' * len(m.group(0)), source_text)
        # Cache the preprocessed text so _extract can reuse it
        # without decoding source_bytes a second time.
        self._cached_source_text = source_text
        # The EXACT bytes the tree is parsed over. Node byte offsets refer
        # to these bytes, NOT the raw file bytes: invalid UTF-8 becomes
        # U+FFFD (3 bytes when re-encoded), and attr/asm spans containing
        # multi-byte chars are replaced by a CHAR-count of spaces. Slicing
        # the raw file bytes with tree offsets produced garbage names for
        # everything after the first divergence point. _extract rebinds its
        # source_bytes to these so all _node_text slicing stays aligned.
        self._parse_bytes = source_text.encode('utf-8')
        return self.parser.parse(self._parse_bytes)

    def _extract_vtable_registrations(self, tree, source_bytes: bytes,
                                       filepath: str, source_root: str) -> list:
        """Extract vtable (function pointer table) struct initializers.

        Finds struct initializers like:
            static const struct file_operations ext4_file_operations = {
                .read_iter = ext4_file_read_iter,
                .write_iter = ext4_file_write_iter,
            };

        Only detects structs matching profile's struct_op_types or common
        vtable naming conventions (ops, fn_table, etc.).

        Returns list of dicts matching the extraction format.
        """
        source_text = source_bytes.decode('utf-8', errors='replace')
        _struct_op_types = set(getattr(self, '_struct_op_types', []))

        # Base keywords that indicate a vtable struct type
        _BASE_VTABLE_KEYWORDS = {'fn_table', 'ops', 'operations', 'module', 'impl',
                                  'scheduler', 'driver', 'callbacks',
                                  'handlers', 'dispatch', 'interface'}
        _vtable_keywords = _BASE_VTABLE_KEYWORDS | _struct_op_types

        results = []
        root = tree.root_node

        # Walk the tree to find struct initializer lists
        # Pattern: struct type_name var_name = { ... }
        # tree-sitter: type_declaration > struct_specifier, init_declarator
        stack = [root]
        while stack:
            node = stack.pop()
            # Look for declaration nodes that might contain struct initializers
            if node.type == 'declaration':
                # Check if this is a struct declaration with initialization
                type_node = None
                for child in node.children:
                    if child.type == 'struct_specifier':
                        type_node = child
                        break

                if type_node is None:
                    for child in node.children:
                        stack.append(child)
                    continue

                # Extract struct type name
                struct_name = ""
                for child in type_node.children:
                    if child.type == 'type_identifier':
                        struct_name = self._node_text(child, source_bytes)
                        break

                # Check if it's a vtable type
                if not struct_name:
                    for child in node.children:
                        stack.append(child)
                    continue

                struct_lower = struct_name.lower()
                is_vtable = (struct_name in _vtable_keywords or
                             any(kw in struct_lower for kw in _BASE_VTABLE_KEYWORDS))

                if not is_vtable:
                    for child in node.children:
                        stack.append(child)
                    continue

                # Find variable name from init_declarator
                var_name = ""
                initializer_node = None
                for child in node.children:
                    if child.type == 'init_declarator':
                        # init_declarator has declarator + initializer
                        for sub in child.children:
                            if sub.type in ('identifier', 'pointer_declarator'):
                                if sub.type == 'pointer_declarator':
                                    for ss in sub.children:
                                        if ss.type == 'identifier':
                                            var_name = self._node_text(ss, source_bytes)
                                            break
                                else:
                                    var_name = self._node_text(sub, source_bytes)
                            if sub.type == 'initializer_list':
                                initializer_node = sub
                                break
                        break

                if not initializer_node:
                    for child in node.children:
                        stack.append(child)
                    continue

                # Extract .field = value pairs from initializer_list
                registrations = []
                for field_init in initializer_node.children:
                    # tree-sitter uses 'initializer_pair' for .field = value
                    # and 'field_initializer' in some language versions
                    if field_init.type not in ('initializer_pair', 'field_initializer'):
                        continue
                    field_name = ""
                    func_name = ""
                    for child in field_init.children:
                        # Field name from field_designator (.name) or field_identifier
                        if child.type == 'field_designator':
                            for fc in child.children:
                                if fc.type == 'field_identifier':
                                    field_name = self._node_text(fc, source_bytes)
                        elif child.type == 'field_identifier':
                            field_name = self._node_text(child, source_bytes)
                        elif child.type == 'identifier':
                            fn = self._node_text(child, source_bytes)
                            # Skip NULL, keywords, short names, ALL_CAPS
                            if fn != 'NULL' and len(fn) > 3 and not fn.isupper():
                                func_name = fn
                        elif child.type == 'cast_expression':
                            # Handle (type *)func_name patterns
                            for cc in child.children:
                                if cc.type == 'identifier':
                                    fn = self._node_text(cc, source_bytes)
                                    if fn != 'NULL' and len(fn) > 3 and not fn.isupper():
                                        func_name = fn
                    if field_name and func_name:
                        registrations.append({
                            "field": field_name,
                            "func_name": func_name,
                            "condition": "",
                            "line": field_init.start_point[0] + 1,
                            "column": field_init.start_point[1] + 1,
                            "start_byte": field_init.start_byte,
                            "end_byte": field_init.end_byte,
                        })

                if registrations:
                    # Get pp condition for the vtable definition
                    # NOTE: m.start() is a character offset into source_text;
                    # convert to a line number to compare with node.start_point[0].
                    pp_conds = [(m.start(), m.group(1), m.group(2).strip())
                                for m in _PP_COND_RE.finditer(source_text)]
                    vtable_condition = ""
                    node_start_line = node.start_point[0]
                    for cond_offset, _cond_dir, cond_text in pp_conds:
                        # Convert character offset → 0-based line number
                        cond_line = source_text.count('\n', 0, cond_offset)
                        # Approximate: check if vtable start is within condition range
                        if cond_line <= node_start_line:
                            # Find the matching #endif
                            vtable_condition = cond_text
                            break

                    results.append({
                        "struct_type": struct_name,
                        "var_name": var_name,
                        "registrations": registrations,
                        "source_file": os.path.relpath(filepath, source_root),
                        "condition": vtable_condition,
                    })
            else:
                for child in node.children:
                    stack.append(child)

        return results

    def _extract(self, tree, source_bytes: bytes, filepath: str,
                 source_root: str, domain: str):
        functions = []
        edges = []
        # The tree was parsed over _parse()'s transformed bytes (attr/asm
        # stripping, 'replace'-decoding re-encoded to UTF-8) — its byte
        # offsets refer to THOSE bytes, not the raw file bytes. Rebind so
        # every _node_text slice and byte-offset comparison below uses the
        # parse-time bytes; without this, anything after an invalid-UTF-8
        # byte or a stripped multi-byte __attribute__ span is shifted and
        # names/edges turn to garbage.
        source_bytes = getattr(self, '_parse_bytes', source_bytes)
        # Reuse the preprocessed text from _parse if available (avoids
        # re-decoding source_bytes). The _parse method already decoded
        # source_bytes and applied attribute stripping / asm_inline
        # replacement / MSVC __asm extraction. The _extract method needs
        # the same preprocessed text for PP condition extraction and
        # export macro scanning.
        source_text = getattr(self, '_cached_source_text', None)
        if source_text is None:
            source_text = source_bytes.decode('utf-8', errors='replace')

        # Extract export macro usages (e.g., EXPORT_SYMBOL(func_name))
        # Functions wrapped in export macros are public API entries regardless
        # of naming convention — the macro is the authoritative signal.
        _exported_names = set()
        _export_macros = getattr(self, '_export_macros', [])
        if _export_macros:
            _EXPORT_MACRO_RE = self._get_export_macro_re(tuple(sorted(_export_macros)))
            for m in _EXPORT_MACRO_RE.finditer(source_text):
                _exported_names.add(m.group(1))

        # Extract preprocessor conditions for the whole file
        pp_conds = [(m.start(), m.group(1), m.group(2).strip()) for m in _PP_COND_RE.finditer(source_text)]

        # Build pp condition liveness map: for each pp condition, whether the branch is alive
        pp_liveness = self._build_pp_liveness(pp_conds, source_text)

        root = tree.root_node

        # DFS to find function definitions and class specifications,
        # tracking preprocessor condition nesting via _ifdef_stack.
        # We must use DFS (not BFS) because _ifdef_stack is managed by
        # push/pop around the recursive descent into children.
        func_nodes = []
        class_nodes = []
        concept_nodes = []  # D13: C++20 concepts
        self._ifdef_stack = []  # Reset for this file
        # D13: per-function template metadata (keyed by id(node))
        if not hasattr(self, '_template_meta') or self._template_meta is None:
            self._template_meta = {}
        self._template_meta.clear()
        # D12: extract macro definitions for MACRO_EXPANDS_TO edges
        self._macro_defs = self._extract_macro_definitions(root, source_bytes)

        def _collect_nodes(node):
            """DFS walk collecting function/class nodes with ifdef context."""
            if node.type in ('preproc_ifdef', 'preproc_if'):
                condition = self._extract_pp_condition_from_node(node, source_bytes)
                self._ifdef_stack.append(condition)
                # Visit all children; preproc_elif/preproc_else are also
                # children of this node, so they will be visited inside
                # this push/pop scope.
                for child in node.children:
                    _collect_nodes(child)
                self._ifdef_stack.pop()
            elif node.type == 'preproc_elif':
                # #elif: the body executes when the parent condition is false
                # AND the elif condition is true.
                saved_top = self._ifdef_stack[-1] if self._ifdef_stack else ""
                elif_cond = self._extract_pp_condition_from_node(node, source_bytes)
                if self._ifdef_stack:
                    self._ifdef_stack[-1] = f"!({saved_top}) && {elif_cond}"
                for child in node.children:
                    _collect_nodes(child)
                # Restore the original condition so subsequent siblings
                # (next #elif or #else) see the parent condition.
                if self._ifdef_stack:
                    self._ifdef_stack[-1] = saved_top
            elif node.type == 'preproc_else':
                # #else: the body executes when the parent condition is false.
                saved_top = self._ifdef_stack[-1] if self._ifdef_stack else ""
                if self._ifdef_stack:
                    self._ifdef_stack[-1] = f"!({saved_top})"
                for child in node.children:
                    _collect_nodes(child)
                # Restore the original condition for subsequent siblings.
                if self._ifdef_stack:
                    self._ifdef_stack[-1] = saved_top
            elif node.type == 'function_definition':
                func_nodes.append((node, list(self._ifdef_stack)))
            elif self.is_cpp and node.type == 'method_definition':
                func_nodes.append((node, list(self._ifdef_stack)))
            elif self.is_cpp and node.type == 'constructor_definition':
                func_nodes.append((node, list(self._ifdef_stack)))
            elif self.is_cpp and node.type == 'destructor_definition':
                func_nodes.append((node, list(self._ifdef_stack)))
            elif self.is_cpp and node.type == 'class_specifier':
                class_nodes.append(node)
            elif self.is_cpp and node.type == 'struct_specifier':
                class_nodes.append(node)
            # D13: C++ templates, concepts, coroutines
            elif self.is_cpp and node.type == 'template_declaration':
                # template_declaration wraps a function_definition or
                # class_specifier — descend to find the inner declaration.
                inner_func = self._find_template_inner_function(node)
                if inner_func is not None:
                    func_nodes.append((inner_func, list(self._ifdef_stack)))
                    # Record template metadata for later annotation
                    self._template_meta[id(inner_func)] = {
                        "is_template": True,
                        "template_params": self._extract_template_params(
                            node, source_bytes),
                    }
            elif self.is_cpp and node.type == 'concept_definition':
                concept_nodes.append(node)
            else:
                for child in node.children:
                    _collect_nodes(child)

        _collect_nodes(root)

        all_fn_ptr_calls = []  # Collect fn_ptr_calls from all functions
        all_macro_regs = []   # Collect macro_registrations from all functions

        # Detect top-level macro invocations (e.g., module_platform_driver(x))
        # These appear as call_expression (possibly wrapped in expression_statement)
        # at the translation_unit level but are NOT inside any function,
        # so _process_function never sees them.
        if self._macro_dispatch_patterns or self._callback_patterns:
            for child in root.children:
                # Unwrap expression_statement to get the call_expression
                _node = child
                if _node.type == 'expression_statement' and _node.children:
                    _node = _node.children[0]
                if _node.type != 'call_expression':
                    continue
                callee_name = self._extract_callee_name(_node, source_bytes)

                # Macro dispatch detection
                if self._macro_dispatch_patterns and callee_name in self._macro_dispatch_patterns:
                    _md_pat = self._macro_dispatch_patterns[callee_name]
                    _struct_arg_idx = _md_pat.get("struct_arg_index", 0)
                    _arg_pos = _struct_arg_idx + 1
                    _struct_var = ""
                    args_structured = self._extract_callee_args_structured(_node, source_bytes)
                    for a in args_structured:
                        if a.get("pos") == _arg_pos:
                            _struct_var = a.get("value", "").lstrip('*& ')
                            break
                    if _struct_var:
                        all_macro_regs.append({
                            "macro_name": callee_name,
                            "struct_var": _struct_var,
                            "source_file": filepath,
                            "line": child.start_point[0] + 1,
                        })

                # Callback pattern detection at top level
                if self._callback_patterns and callee_name in self._callback_patterns:
                    _cb_arg_idx, _cb_concurrency = self._callback_patterns[callee_name]
                    if _cb_arg_idx >= 0:
                        _cb_arg_pos = _cb_arg_idx + 1
                        _cb_target = ""
                        args_structured = self._extract_callee_args_structured(_node, source_bytes)
                        for a in args_structured:
                            if a.get("pos") == _cb_arg_pos:
                                _cb_target = a.get("value", "").lstrip('*& ')
                                break
                        if _cb_target:
                            # For top-level callbacks, create edge from a synthetic
                            # source based on the file. The builder will resolve the
                            # target function node.
                            edges.append({
                                "source": f"{domain}::__toplevel_{callee_name}",
                                "target": _cb_target,
                                "call_order": 0,
                                "call_condition": "",
                                "confidence": "CALLBACK_ARG",
                                "concurrency": _cb_concurrency,
                                "source_tag": "callback_arg",
                                "preproc_condition": "",
                                "preproc_alive": True,
                                "evidence": f"callback_arg: {callee_name}() arg#{_cb_arg_idx}={_cb_target} (toplevel)",
                            })
        for func_node, ifdef_conds in func_nodes:
            self._process_function(func_node, source_bytes, source_root,
                                    filepath, domain, edges, functions,
                                    pp_conds, source_text, pp_liveness,
                                    ifdef_conds, all_fn_ptr_calls,
                                    macro_regs_global=all_macro_regs)

        # ERROR node fallback: recover inline asm from nodes that escaped
        # function_definition due to tree-sitter parsing errors.
        # When tree-sitter-c encounters `register void *rax __asm__("rax") = ...`,
        # it produces ERROR nodes that prevent the function from being recognized.
        # The gnu_asm_expression nodes end up as siblings at the translation_unit level.
        # We detect these orphaned asm nodes and recover the caller function name
        # via regex from the surrounding source text.
        _processed_asm_ranges = set()  # Track byte ranges already processed by _process_function
        for func_node, _ in func_nodes:
            _processed_asm_ranges.add((func_node.start_byte, func_node.end_byte))

        def _find_orphan_asm_nodes(node, depth=0):
            """Find gnu_asm_expression nodes not inside any function_definition."""
            orphans = []
            # Check if this node is inside a known function
            def _is_in_function(n):
                parent = n.parent
                while parent:
                    if parent.type == 'function_definition':
                        for fn_node, _ in func_nodes:
                            if parent.start_byte == fn_node.start_byte:
                                return True
                    parent = parent.parent
                return False

            if node.type == 'gnu_asm_expression' and not _is_in_function(node):
                orphans.append(node)
            elif node.type == 'ERROR':
                # Search inside ERROR nodes for gnu_asm_expression children
                for child in node.children:
                    orphans.extend(_find_orphan_asm_nodes(child, depth + 1))
            elif node.type not in ('function_definition',):
                for child in node.children:
                    orphans.extend(_find_orphan_asm_nodes(child, depth + 1))
            return orphans

        orphan_asms = _find_orphan_asm_nodes(root)
        if orphan_asms:
            # Group orphan asm nodes by their enclosing function (determined by regex)
            # to avoid creating duplicate function entries
            _synthetic_funcs = {}  # func_name → func_info
            for asm_node in orphan_asms:
                # Find the enclosing function name by regex-scanning backwards
                # from the asm node's start position in the source text
                asm_start = asm_node.start_byte
                # Look for a function-like pattern before this asm node
                # Pattern: type [*] name(params) { — we only need the part up to {
                # because the body may contain } from ERROR nodes
                func_re = _FUNC_DECL_RE
                # Search in the text before the asm node
                search_text = source_text[:asm_start]
                func_match = None
                for m in func_re.finditer(search_text):
                    func_match = m  # Take the last match (closest to asm)
                if func_match:
                    func_name = func_match.group(1)
                    # Skip C keywords that could look like function names
                    _KEYWORDS = {'if', 'while', 'for', 'switch', 'do', 'return',
                                 'sizeof', 'typedef', 'struct', 'enum', 'union'}
                    if func_name in _KEYWORDS:
                        continue
                    # Skip if this function was already detected by tree-sitter
                    if any(f["name"] == func_name for f in functions):
                        continue
                    # Create synthetic function entry if not yet created
                    if func_name not in _synthetic_funcs:
                        func_id = self._make_func_id(domain, func_name)
                        func_line = source_text[:asm_start].count('\n') + 1
                        _synthetic_funcs[func_name] = {
                            "id": func_id, "name": func_name,
                            "source_file": os.path.relpath(filepath, source_root),
                            "line": func_line, "domain": domain,
                            "labels": [],
                            "labels_source": {},
                            "is_empty": False,
                            "api_constraints": [],
                            "body_text": "",
                            "signature": f"{func_name}()",
                            "params": [],
                            "local_vars": [],
                            "callee_args": [],
                            "condition_vars": [],
                            "ifdef_conditions": [],
                            "preproc_alive": True,
                        }
                    # Process the inline asm with this synthetic caller
                    call_order = [0]
                    callee_args_list = []
                    self._process_inline_asm(asm_node, source_bytes,
                                             _synthetic_funcs[func_name]["id"],
                                             domain, edges, call_order,
                                             callee_args_list)

            # Add all synthetic functions to the functions list
            functions.extend(_synthetic_funcs.values())

        # Process MSVC __asm { } blocks that were stripped during preprocessing
        # These blocks were replaced with whitespace but their content was saved
        # in self._msvc_asm_blocks. We need to find which function contains
        # each block and process its content as inline asm.
        msvc_blocks = getattr(self, '_msvc_asm_blocks', [])
        if msvc_blocks:
            for block_info in msvc_blocks:
                block_start = block_info["start_byte"]
                block_end = block_info["end_byte"]
                body = block_info["body"]
                # Find which function this block belongs to
                containing_func = None
                for func in functions:
                    # Approximate: check if block is within function's line range
                    func_line = func.get("line", 0)
                    func_start_byte = None
                    # Find the function node's byte range
                    for fn_node, _ in func_nodes:
                        if fn_node.start_point[0] + 1 == func_line:
                            func_start_byte = fn_node.start_byte
                            func_end_byte = fn_node.end_byte
                            break
                    if func_start_byte is not None and func_start_byte <= block_start <= func_end_byte:
                        containing_func = func
                        break
                if containing_func:
                    # Create a synthetic asm text and process it
                    asm_text = body.replace('\\n', '\n').replace('\\t', '\t')
                    call_order = [0]
                    callee_args_list = []
                    # Build a fake gnu_asm_expression-like processing using regex
                    # We can't call _process_inline_asm directly because it expects a node,
                    # so we inline the relevant logic here
                    for m in re.finditer(r'\bcall\s+([a-zA-Z_]\w*)', asm_text):
                        callee_name = m.group(1)
                        call_order[0] += 1
                        edges.append({
                            "source": containing_func["id"],
                            "target": callee_name.lower(),
                            "call_order": call_order[0],
                            "call_condition": "",
                            "confidence": "INFERRED",
                            "source_tag": "inline_asm",
                            "confidence_score": 0.7,
                        })
                    for m in re.finditer(r'\bjmp\s+([a-zA-Z_]\w*)', asm_text):
                        target = m.group(1)
                        call_order[0] += 1
                        edges.append({
                            "source": containing_func["id"],
                            "target": target.lower(),
                            "call_order": call_order[0],
                            "call_condition": "",
                            "confidence": "INFERRED",
                            "source_tag": "inline_asm",
                            "confidence_score": 0.6,
                        })
                    for m in re.finditer(r'\bbl\s+([a-zA-Z_]\w*)', asm_text):
                        callee_name = m.group(1)
                        call_order[0] += 1
                        edges.append({
                            "source": containing_func["id"],
                            "target": callee_name.lower(),
                            "call_order": call_order[0],
                            "call_condition": "",
                            "confidence": "INFERRED",
                            "source_tag": "inline_asm",
                            "confidence_score": 0.7,
                        })

        # Extract inline asm calls from #define macros
        # tree-sitter does not expand macros, so asm calls inside #define bodies
        # are invisible to the normal gnu_asm_expression path. We scan
        # preproc_function_def and preproc_def nodes for asm patterns.
        # (hoisted to module level as _MACRO_ASM_RE)
        _macro_asm_edges = []  # (macro_name, edge_dict)
        _macro_asm_funcs = {}  # macro_name → synthetic function entry

        def _collect_preproc_defs(node):
            """Walk tree to find preproc_function_def / preproc_def with asm bodies."""
            results = []
            if node.type in ('preproc_function_def', 'preproc_def'):
                # Get macro name
                macro_name = None
                macro_body = ""
                for child in node.children:
                    if child.type == 'identifier':
                        macro_name = code_slice(child.start_byte, child.end_byte)
                    elif child.type == 'preproc_arg':
                        macro_body = code_slice(child.start_byte, child.end_byte)
                if macro_name and macro_body and _MACRO_ASM_RE.search(macro_body):
                    results.append((macro_name, macro_body))
            for child in node.children:
                if child.type not in ('function_definition',):
                    results.extend(_collect_preproc_defs(child))
            return results

        # Slice the parse-time BYTES with tree byte offsets (then decode),
        # not the decoded text — char offsets and byte offsets diverge on
        # files with multi-byte characters.
        code_slice = lambda s, e: source_bytes[s:e].decode('utf-8', errors='replace')
        preproc_macros = _collect_preproc_defs(root)

        if preproc_macros:
            for macro_name, macro_body in preproc_macros:
                # Create synthetic function entry for the macro
                if macro_name not in _macro_asm_funcs:
                    func_id = f"{domain}__macro_{macro_name}" if domain else f"__macro_{macro_name}"
                    _macro_asm_funcs[macro_name] = {
                        "name": macro_name,
                        "id": func_id,
                        "domain": domain,
                        "file": filepath,
                        "line": 0,
                        "labels": ["unknown_end"],
                        "body_text": "",
                        "source_tag": "inline_asm_macro",
                    }
                func_id = _macro_asm_funcs[macro_name]["id"]
                call_order = [0]

                for pat, confidence, score, special in _MACRO_ASM_CALL_RES:
                    for m in pat.finditer(macro_body):
                        call_order[0] += 1
                        if special:
                            target = special
                        else:
                            target = m.group(1).lower() if m.lastindex else special
                        edges.append({
                            "source": func_id,
                            "target": target,
                            "call_order": call_order[0],
                            "call_condition": "",
                            "confidence": confidence,
                            "source_tag": "inline_asm_macro",
                            "confidence_score": score,
                        })

                # Also check for ALTERNATIVE(old, new, feature) with call targets
                for alt_m in re.finditer(
                    r'ALTERNATIVE\s*\(\s*"([^"]*)"\s*,\s*"([^"]*)"', macro_body
                ):
                    for group_idx in (1, 2):
                        alt_instr = alt_m.group(group_idx)
                        for m in re.finditer(r'\bcall\s+([a-zA-Z_]\w*)', alt_instr):
                            call_order[0] += 1
                            edges.append({
                                "source": func_id,
                                "target": m.group(1).lower(),
                                "call_order": call_order[0],
                                "call_condition": "alternative" if group_idx == 2 else "",
                                "confidence": "INFERRED",
                                "source_tag": "inline_asm_macro",
                                "confidence_score": 0.6,
                            })
                        for m in re.finditer(r'\bjmp\s+([a-zA-Z_]\w*)', alt_instr):
                            call_order[0] += 1
                            edges.append({
                                "source": func_id,
                                "target": m.group(1).lower(),
                                "call_order": call_order[0],
                                "call_condition": "alternative" if group_idx == 2 else "",
                                "confidence": "INFERRED",
                                "source_tag": "inline_asm_macro",
                                "confidence_score": 0.5,
                            })

                # Also check for static_call trampoline patterns
                # ".byte 0xe9; .long " #func " - (. + 4)" → func is the target
                for sc_m in re.finditer(
                    r'\.byte\s+0xe9\s*;\s*\.long\s+"?\s*#(\w+)', macro_body
                ):
                    call_order[0] += 1
                    edges.append({
                        "source": func_id,
                        "target": sc_m.group(1).lower(),
                        "call_order": call_order[0],
                        "call_condition": "static_call",
                        "confidence": "INFERRED",
                        "source_tag": "inline_asm_macro",
                        "confidence_score": 0.7,
                    })

                # "call " #func → func is the target (macro stringification)
                for sc_m in re.finditer(r'\bcall\s+"?\s*#(\w+)', macro_body):
                    target = sc_m.group(1).lower()
                    # Avoid duplicate with direct call match above
                    if not any(e["target"] == target and e["source"] == func_id for e in edges[-call_order[0]:]):
                        call_order[0] += 1
                        edges.append({
                            "source": func_id,
                            "target": target,
                            "call_order": call_order[0],
                            "call_condition": "static_call",
                            "confidence": "INFERRED",
                            "source_tag": "inline_asm_macro",
                            "confidence_score": 0.7,
                        })

            # Add synthetic function entries
            functions.extend(_macro_asm_funcs.values())

        # Extract C++ class/struct inheritance edges
        if self.is_cpp:
            for class_node in class_nodes:
                self._extract_inheritance(class_node, source_bytes, source_root,
                                          filepath, domain, edges)
            # D13: extract C++20 concept definitions
            for concept_node in concept_nodes:
                concept_info = self._extract_concept(
                    concept_node, source_bytes, filepath, source_root)
                # Store as a special function-like node so it shows up in
                # the graph and can be referenced by SATISFIES_CONCEPT edges.
                func_id = self._make_func_id(domain, f"concept::{concept_info['name']}")
                functions.append({
                    "id": func_id,
                    "name": f"concept::{concept_info['name']}",
                    "source_file": concept_info["source_file"],
                    "line": concept_info["line"],
                    "domain": domain,
                    "labels": ["concept"],
                    "labels_source": {"concept": self._source_tag()},
                    "is_empty": True,
                    "is_concept": True,
                    "constraint": concept_info["constraint"],
                    "body_text": "",
                    "signature": f"concept {concept_info['name']} = {concept_info['constraint']}",
                    "params": [],
                    "local_vars": [],
                    "callee_args": [],
                    "condition_vars": [],
                    "ifdef_conditions": [],
                    "preproc_alive": True,
                    "doc_comment": "",
                })

        # Post-process: mark functions wrapped in export macros as API_entry
        # But skip functions in non-API paths (tools/, testing/, samples/, etc.,
        # plus project-declared non_api_paths from the profile).
        if _exported_names:
            _NON_API_PATHS_CHECK = ('tools/', 'scripts/', 'selftests/', 'testing/',
                                     'documentation/', 'samples/', 'examples/'
                                     ) + tuple(getattr(self, '_non_api_paths', []) or [])
            for func in functions:
                if func.get("name") in _exported_names:
                    src = func.get("source_file", "").replace(os.sep, '/')
                    if any(p and p in src for p in _NON_API_PATHS_CHECK):
                        continue
                    if "API_entry" not in func.get("labels", []):
                        func.setdefault("labels", []).append("API_entry")

        # Extract vtable registrations from struct initializers
        vtable_registrations = self._extract_vtable_registrations(
            tree, source_bytes, filepath, source_root)

        # Convert fn_ptr_calls list to dict keyed by caller function NAME (not ID)
        # The builder's _name_to_nid index uses function names, so we must key by name.
        # Build a func_id → func_name lookup from the functions we've extracted.
        _id_to_name = {f["id"]: f["name"] for f in functions if "id" in f and "name" in f}
        fn_ptr_calls_dict = {}
        for entry in all_fn_ptr_calls:
            invoker_id = entry["caller"]
            caller_name = _id_to_name.get(invoker_id, invoker_id)
            fn_ptr_calls_dict.setdefault(caller_name, []).append({
                "callee_name": entry["callee_name"],
                "fn_ptr_expr": entry["fn_ptr_expr"],
                "field_name": entry.get("field_name", entry["callee_name"]),
                "struct_chain": entry.get("struct_chain", ""),
                "call_order": entry["call_order"],
                "line": entry["line"],
            })

        return functions, edges, vtable_registrations, fn_ptr_calls_dict, all_macro_regs

    def _evaluate_preproc_condition(self, condition: str, directive: str) -> bool:
        """C/C++: evaluate #ifdef/#ifndef/#if with macro bindings."""
        return evaluate_pp_condition(condition, directive, self._macro_bindings)

    def _extract_pp_condition_from_node(self, pp_node, source_bytes: bytes) -> str:
        """Extract the condition text from a preproc_ifdef/preproc_if/preproc_elif node.

        In tree-sitter's C grammar:
          - preproc_ifdef: children are #ifdef, identifier, body..., #endif
          - preproc_if: children are #if, condition expression, body..., #endif
          - preproc_elif: children are #elif, condition expression, body...
        Returns the condition string (e.g., "SPDK_CONFIG_APP_RW").
        """
        # Skip the directive keyword tokens (#ifdef, #if, #elif, #endif)
        # and preproc_else body nodes, then the first remaining child is
        # the condition.
        _SKIP_TYPES = {'#ifdef', '#ifndef', '#if', '#elif', '#else', '#endif',
                        'preproc_else', 'preproc_elif'}
        for child in pp_node.children:
            if child.type in _SKIP_TYPES:
                continue
            text = self._node_text(child, source_bytes).strip()
            if text:
                return text
        return ""

    def _build_pp_liveness(self, pp_conds, source_text):
        """Build a mapping of byte_offset → (is_alive, condition_text) for pp conditions.

        Returns list of (byte_start, byte_end, is_alive, condition_text) ranges.
        Each range describes a preprocessor conditional block with its liveness.
        """
        if not self._macro_bindings:
            return []  # No bindings — everything is alive

        # pp_conds is already extracted by the caller via _PP_COND_RE,
        # which matches if|ifdef|ifndef|elif|else|endif in source order.
        # Use it directly — re-extracting from source_text with a narrower
        # regex would miss #if/#ifdef/#ifndef directives and duplicate
        # else/elif entries (the previous implementation's bug).
        all_pp = list(pp_conds)
        all_pp.sort(key=lambda x: x[0])

        # Walk directives to build dead ranges
        dead_ranges = []
        pp_stack2 = []
        cur_range_start = None

        for pos, directive, condition in all_pp:
            # Close any previous dead range
            if directive in ('if', 'ifdef', 'ifndef'):
                is_alive = self._evaluate_preproc_condition(condition, directive)
                parent_dead = pp_stack2 and not pp_stack2[-1][3]
                if parent_dead:
                    is_alive = False
                if not is_alive and cur_range_start is None:
                    cur_range_start = pos
                elif is_alive and cur_range_start is not None:
                    # Nested: don't close yet — parent still dead
                    pass
                pp_stack2.append((pos, directive, condition, is_alive))
            elif directive in ('elif', 'else'):
                if pp_stack2:
                    prev_start, prev_dir, prev_cond, prev_alive = pp_stack2[-1]
                    parent_dead = len(pp_stack2) >= 2 and not pp_stack2[-2][3]
                    if parent_dead:
                        is_alive = False
                    elif prev_alive:
                        is_alive = False
                    elif directive == 'elif':
                        is_alive = self._evaluate_preproc_condition(condition, 'elif')
                    else:
                        is_alive = True
                    # If transitioning from dead to alive, record dead range
                    if not prev_alive and is_alive and cur_range_start is not None:
                        dead_ranges.append((cur_range_start, pos))
                        cur_range_start = None
                    elif prev_alive and not is_alive:
                        cur_range_start = pos
                    pp_stack2[-1] = (prev_start, directive, condition, is_alive)
            elif directive == 'endif':
                if pp_stack2:
                    prev_start, prev_dir, prev_cond, prev_alive = pp_stack2[-1]
                    pp_stack2.pop()
                    # If popping a dead range and no parent is dead
                    if not prev_alive and cur_range_start is not None:
                        parent_dead = pp_stack2 and not pp_stack2[-1][3]
                        if not parent_dead:
                            dead_ranges.append((cur_range_start, pos))
                            cur_range_start = None

        # If still in a dead range at end of file, close it
        if cur_range_start is not None:
            dead_ranges.append((cur_range_start, len(source_text)))

        # pp_conds offsets are CHAR offsets in the decoded source_text, but
        # _is_in_dead_range consumers pass tree BYTE offsets. Convert the
        # range endpoints once (no-op for pure-ASCII text).
        if dead_ranges:
            _flat = _char_offsets_to_bytes(
                source_text, [p for r in dead_ranges for p in r])
            dead_ranges = [(_flat[i * 2], _flat[i * 2 + 1])
                           for i in range(len(dead_ranges))]
        return dead_ranges

    def _is_in_dead_range(self, byte_offset: int, dead_ranges: list) -> bool:
        """Check if a byte offset falls within a dead preprocessor range."""
        if not dead_ranges:
            return False
        # Binary search: dead_ranges is sorted by start (guaranteed by
        # _build_pp_liveness which appends in source order). Find the
        # last range with start <= byte_offset, then check if byte_offset
        # is within [start, end).
        import bisect
        starts = [r[0] for r in dead_ranges]
        idx = bisect.bisect_right(starts, byte_offset) - 1
        if idx >= 0:
            start, end = dead_ranges[idx]
            if start <= byte_offset < end:
                return True
        return False

    def _process_function(self, func_node, source_bytes, source_root,
                          filepath, domain, edges, functions, pp_conds,
                          source_text, pp_liveness=None, ifdef_conds=None,
                          fn_ptr_calls_global=None, macro_regs_global=None):
        # Set current filepath for API detection
        self._current_filepath = filepath
        # Extract function name
        func_name = self._extract_func_name(func_node, source_bytes)
        if not func_name:
            return

        # Reject functions whose signature contains token-paste (##) — these
        # are macro-body artifacts (e.g., BSD tree.h SPLAY_PROTOTYPE expands
        # to `void name##_SPLAY_MINMAX(...)` inside a #define). Tree-sitter
        # parses the macro body as a function_definition because the
        # preprocessor body has C-like syntax, but the `##` token paste in
        # the signature is a definitive marker that this is not real code.
        # Inspect only the declarator (signature), not the body, so that
        # real functions whose bodies happen to contain `##` in comments or
        # string literals are not affected.
        _decl = next((c for c in func_node.children if c.type in
                      ('function_declarator', 'pointer_declarator',
                       'reference_declarator')), None)
        if _decl is not None and '##' in self._node_text(_decl, source_bytes):
            return

        func_line = func_node.start_point[0] + 1
        func_id = self._make_func_id(domain, func_name)

        # Check if entire function is in a dead preprocessor range
        is_dead = pp_liveness and self._is_in_dead_range(func_node.start_byte, pp_liveness)

        # ifdef_conditions from the AST-level preprocessor nesting
        ifdef_conditions = ifdef_conds if ifdef_conds else []

        # Find the body (compound_statement)
        body_node = None
        for child in func_node.children:
            if child.type == 'compound_statement':
                body_node = child
                break
        if body_node is None:
            # Declaration only, no body
            labels = self._detect_labels(func_name, func_node, source_bytes)
            is_api, constraints = self._detect_api_entry(func_name, func_node, source_bytes)
            if is_api:
                labels.append("API_entry")
            if is_dead:
                labels.append("dead_code")
            # D13: template/concept/coroutine annotations
            tpl_meta = self._template_meta.get(id(func_node), {})
            is_coro = self._is_coroutine(func_node, source_bytes, body_node)
            functions.append({
                "id": func_id, "name": func_name,
                "source_file": os.path.relpath(filepath, source_root),
                "line": func_line, "domain": domain,
                "labels": labels,
                "labels_source": {l: ("preproc_dead" if l == "dead_code" else self._source_tag()) for l in labels},
                "is_empty": False,
                "api_constraints": constraints,
                "body_text": "",
                "signature": self._extract_signature(func_node, source_bytes),
                "params": self._extract_params(func_node, source_bytes),
                "local_vars": [],
                "callee_args": [],
                "condition_vars": [],
                "ifdef_conditions": ifdef_conditions,
                "preproc_alive": not is_dead,
                "doc_comment": self._extract_doc_comment(func_node, source_bytes),
                "is_template": tpl_meta.get("is_template", False),
                "template_params": tpl_meta.get("template_params", []),
                "is_coroutine": is_coro,
            })
            return

        params_list = self._extract_params(func_node, source_bytes)

        labels = self._detect_labels(func_name, func_node, source_bytes)
        is_api, constraints = self._detect_api_entry(func_name, func_node, source_bytes)
        if is_api:
            labels.append("API_entry")
        if is_dead:
            labels.append("dead_code")

        # D13: template/concept/coroutine annotations
        tpl_meta = self._template_meta.get(id(func_node), {})
        is_coro = self._is_coroutine(func_node, source_bytes, body_node)
        # D13: detect co_await/co_return calls in body for coroutine edges
        coroutine_awaits = self._extract_coroutine_awaits(
            body_node, source_bytes) if is_coro else []

        functions.append({
            "id": func_id, "name": func_name,
            "source_file": os.path.relpath(filepath, source_root),
            "line": func_line, "domain": domain,
            "labels": labels,
            "labels_source": {l: ("preproc_dead" if l == "dead_code" else self._source_tag()) for l in labels},
            "is_empty": False,
            "api_constraints": constraints,
            "body_text": self._extract_body_text(func_node, source_bytes),
            "signature": self._extract_signature(func_node, source_bytes),
            "params": params_list,
            "local_vars": self._extract_local_vars(body_node, source_bytes, params_list),
            "callee_args": [],
            "condition_vars": [],
            "ifdef_conditions": ifdef_conditions,
            "preproc_alive": not is_dead,
            "doc_comment": self._extract_doc_comment(func_node, source_bytes),
            "is_template": tpl_meta.get("is_template", False),
            "template_params": tpl_meta.get("template_params", []),
            "is_coroutine": is_coro,
            "coroutine_awaits": coroutine_awaits,
            "start_byte": int(func_node.start_byte),
            "end_byte": int(func_node.end_byte),
        })

        # Extract calls from body with ordering and conditions
        callee_args_list = []
        cond_vars_list = []
        goto_jumps_list = []
        goto_labels_list = []
        self._extract_calls(body_node, source_bytes, func_id, domain,
                            edges, pp_conds, source_text, func_node.start_byte,
                            callee_args_list, cond_vars_list, pp_liveness,
                            ifdef_conds, fn_ptr_calls_global, macro_regs_global,
                            goto_jumps_list, goto_labels_list)

        # Update function entry with extracted callee_args and condition_vars
        if callee_args_list:
            functions[-1]["callee_args"] = callee_args_list
        if cond_vars_list:
            functions[-1]["condition_vars"] = cond_vars_list
        if goto_jumps_list:
            functions[-1]["goto_jumps"] = goto_jumps_list
        if goto_labels_list:
            functions[-1]["goto_labels"] = goto_labels_list

    def _extract_func_name(self, func_node, source_bytes: bytes) -> str:
        # Navigate: function_definition > declarator > [pointer_declarator >] function_declarator > identifier
        declarator = next((c for c in func_node.children if c.type in
                           ('function_declarator', 'pointer_declarator',
                            'reference_declarator')), None)
        if declarator is None:
            return ""

        # Descend through nested pointer/reference declarators (e.g., char **func())
        # until we reach the function_declarator. Tree-sitter nests one
        # pointer_declarator per '*' so a 'char **' return produces two
        # levels of pointer_declarator wrapping the function_declarator.
        while declarator.type in ('pointer_declarator', 'reference_declarator'):
            inner = next((c for c in declarator.children
                          if c.type in ('function_declarator',
                                        'pointer_declarator',
                                        'reference_declarator')), None)
            if inner is None or inner is declarator:
                break
            declarator = inner

        if declarator.type != 'function_declarator':
            return self._node_text(declarator, source_bytes).strip().lstrip('*& \n\t')

        # Find identifier or field_identifier
        for child in declarator.children:
            if child.type in ('identifier', 'field_identifier'):
                return self._node_text(child, source_bytes)

        # Constructor/destructor: declarator has the class name
        if self.is_cpp and func_node.type in ('constructor_definition', 'destructor_definition'):
            for child in declarator.children:
                if child.type == 'identifier':
                    prefix = "~" if func_node.type == 'destructor_definition' else ""
                    return prefix + self._node_text(child, source_bytes)

        return ""

    # ------------------------------------------------------------------
    # D13: C++ template / concept / coroutine extraction
    # ------------------------------------------------------------------

    def _find_template_inner_function(self, template_node):
        """Find the function_definition (or method/constructor/destructor)
        wrapped by a template_declaration node. Returns None if the
        template wraps a non-function declaration (e.g., a class).
        """
        func_types = ('function_definition', 'method_definition',
                       'constructor_definition', 'destructor_definition')
        # Direct children first
        for child in template_node.children:
            if child.type in func_types:
                return child
        # Then descendants (one level down)
        for child in template_node.children:
            for grandchild in child.children:
                if grandchild.type in func_types:
                    return grandchild
        return None

    def _extract_template_params(self, template_node,
                                  source_bytes: bytes = b'') -> list:
        """Extract template parameter list from a template_declaration.

        Returns a list of dicts: [{"name": "T", "type": "typename",
                                    "default": ""}, ...]
        """
        params = []
        # The template_parameter_list is a child of template_declaration
        tpl = next((c for c in template_node.children
                    if c.type == 'template_parameter_list'), None)
        if tpl is None:
            return params
        for p in tpl.children:
            if p.type in ('type_parameter_declaration',):
                # typename T or class T
                text = self._node_text(p, source_bytes)
                params.append({"name": text.strip(), "type": "typename",
                                "default": ""})
            elif p.type == 'non_type_parameter_declaration':
                text = self._node_text(p, source_bytes)
                params.append({"name": text.strip(), "type": "non_type",
                                "default": ""})
        return params

    def _is_coroutine(self, func_node, source_bytes: bytes,
                       body_node=None) -> bool:
        """Detect if a function is a C++20 coroutine.

        A function is a coroutine if its body contains `co_await`,
        `co_yield`, or `co_return`. We check the body text for these
        keywords (faster than walking the AST for every function).
        """
        if body_node is None:
            return False
        body_text = self._node_text(body_node, source_bytes)
        for pat in _COROUTINE_KEYWORD_RES:
            if pat.search(body_text):
                return True
        return False

    def _extract_coroutine_awaits(self, body_node,
                                    source_bytes: bytes) -> list:
        """Extract co_await expressions from a coroutine body.

        Returns a list of dicts: [{"expr": "some_awaitable", "line": N}, ...]
        """
        results = []
        if body_node is None:
            return results

        def _walk(node):
            if node.type == 'co_await_expression':
                # co_await_expression children: [co_await, expression]
                expr_node = next((c for c in node.children
                                   if c.type != 'co_await'), None)
                if expr_node is not None:
                    results.append({
                        "expr": self._node_text(expr_node, source_bytes).strip(),
                        "line": expr_node.start_point[0] + 1,
                    })
            for child in node.children:
                _walk(child)

        _walk(body_node)
        return results

    def _extract_concept(self, concept_node, source_bytes: bytes,
                          filepath: str, source_root: str) -> dict:
        """Extract a C++20 concept definition.

        Returns a dict with name, constraints, and source location.
        """
        # concept_definition children: [concept, name, '=', constraint_expr, ';']
        name = ""
        for child in concept_node.children:
            if child.type == 'identifier':
                name = self._node_text(child, source_bytes)
                break
        # The constraint expression is everything after '='
        constraint_text = ""
        found_eq = False
        for child in concept_node.children:
            if found_eq:
                if child.type == ';':
                    break
                constraint_text += self._node_text(child, source_bytes)
            if child.type == '=':
                found_eq = True
        return {
            "name": name,
            "constraint": constraint_text.strip(),
            "source_file": os.path.relpath(filepath, source_root),
            "line": concept_node.start_point[0] + 1,
        }

    def _extract_macro_definitions(self, root, source_bytes: bytes) -> dict:
        """Walk the tree to collect #define macro definitions.

        Returns dict: {macro_name: {"params": [...], "body": str,
                                    "line": int, "is_function": bool,
                                    "source_file": str}}
        """
        macros = {}

        def _walk(node):
            if node.type in ('preproc_function_def', 'preproc_def'):
                name = ""
                params = []
                body = ""
                is_function = (node.type == 'preproc_function_def')
                for child in node.children:
                    if child.type == 'identifier' and not name:
                        name = self._node_text(child, source_bytes)
                    elif child.type == 'preproc_params':
                        # params are identifiers inside preproc_params
                        for pc in child.children:
                            if pc.type == 'identifier':
                                params.append(self._node_text(pc, source_bytes))
                    elif child.type == 'preproc_arg':
                        body = self._node_text(child, source_bytes)
                if name:
                    macros[name] = {
                        "params": params,
                        "body": body,
                        "line": node.start_point[0] + 1,
                        "is_function": is_function,
                    }
            # Don't recurse into function_definition bodies — #define inside
            # a function is rare and tree-sitter usually doesn't nest them.
            if node.type not in ('function_definition',):
                for child in node.children:
                    _walk(child)
        _walk(root)
        return macros

    def _expand_macro(self, macro_def: dict, call_args: list) -> str:
        """Expand a function-like macro by substituting parameters.

        Returns the expanded body text with params replaced by actual args.
        For object-like macros (no params), returns the body as-is.
        """
        body = macro_def.get("body", "")
        params = macro_def.get("params", [])
        if not params or not body:
            return body
        # Single-pass substitution: combine all params into one alternation
        # regex so each position is only substituted once. The previous
        # per-param loop carried 'result' forward, so if one arg's value
        # contained another param name, it was substituted twice
        # (e.g., #define FOO(a,b) a+b called as FOO(b,5) produced 5+5
        # instead of b+5).
        #
        # We also skip substitution inside string and char literals
        # (basic heuristic: split on "..." and '...' and only substitute
        # in odd-indexed segments — the non-literal parts).
        # Missing/empty call args (e.g. F() for #define F(a) do_a(a), or
        # token-paste trickery in #if 0 regions tree-sitter still parses)
        # must NOT raise KeyError — that propagated up scan_file and made
        # the ENTIRE file vanish from the graph (0 functions, 0 edges).
        # Leave unbound params as their own name instead.
        mapping = dict(zip(params, call_args))
        param_pat = re.compile(
            r'\b(' + '|'.join(re.escape(p) for p in params) + r')\b')

        # Split body into literal / non-literal segments to avoid
        # substituting inside "..." and '...' (C standard: macro params
        # are NOT substituted inside string/char literals).
        segments = re.split(r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')', body)
        for i, seg in enumerate(segments):
            # Even indices = non-literal code; odd = string/char literals
            if i % 2 == 0:
                segments[i] = param_pat.sub(
                    lambda m: mapping.get(m.group(1), m.group(1)), seg)
        return ''.join(segments)

    def _extract_macro_calls_from_body(self, expanded_text: str) -> list:
        """Extract function call names from an expanded macro body.

        Returns a list of callee names that look like direct function
        calls — used to create MACRO_EXPANDS_TO edges. Case is preserved
        (C is case-sensitive; lowercasing breaks cross-reference
        resolution against graph nodes whose names use the original case).
        """
        # Match identifier( patterns, excluding control keywords
        _KEYWORDS = {'if', 'while', 'for', 'switch', 'do', 'return',
                     'sizeof', 'typeof', 'offsetof', 'container_of'}
        callees = []
        seen = set()
        for m in _IDENT_CALL_RE.finditer(expanded_text):
            name = m.group(1)
            if name in _KEYWORDS:
                continue
            if name not in seen:
                seen.add(name)
                callees.append(name)
        return callees

    def _detect_labels(self, func_name: str, func_node, source_bytes: bytes) -> list:
        labels = []
        body_text = self._node_text(func_node, source_bytes)

        # Thread
        if any(p.search(body_text) for p in _THREAD_PATTERNS):
            labels.append("thread_processor")

        # Callback
        if any(p.search(body_text) for p in _CALLBACK_PATTERNS):
            labels.append("callback_func")
        # Callback by naming convention (shared suffixes from BaseScanner)
        if self._is_callback_by_name(func_name):
            if "callback_func" not in labels:
                labels.append("callback_func")

        # Constructor
        parts = func_name.split("::")
        if self.is_cpp:
            if len(parts) == 2 and parts[0] == parts[1]:
                labels.append("constructor")
            if len(parts) == 2 and parts[1].startswith("~"):
                labels.append("destructor")
        if _CTOR_NAME_RE.search(func_name):
            if "constructor" not in labels:
                labels.append("constructor")
        if _DTOR_NAME_RE.search(func_name):
            if "destructor" not in labels:
                labels.append("destructor")

        # GCC __attribute__((constructor)) / __attribute__((destructor))
        # These functions execute before main() and often register callbacks/subsystems.
        if not self.is_cpp:  # C only — C++ uses constructors
            attr = self._detect_gcc_attributes(func_node, source_bytes)
            if attr.get("constructor"):
                if "constructor" not in labels:
                    labels.append("constructor")
            if attr.get("destructor"):
                if "destructor" not in labels:
                    labels.append("destructor")

        return labels

    def _detect_gcc_attributes(self, func_node, source_bytes: bytes) -> dict:
        """Detect GCC __attribute__ annotations on a function declaration.

        In tree-sitter C grammar, __attribute__((constructor)) appears as:
        - attribute_specifier -> attributed_declaration -> function_definition
        - attribute_specifier -> declaration -> declarator -> function_declarator
        Returns dict with 'constructor' and 'destructor' bool flags.

        Only scans the declaration prefix (before the body) for efficiency.
        """
        result = {"constructor": False, "destructor": False}

        def _scan_node(node, depth=0):
            if depth > 4:  # Don't recurse deep into body
                return
            if node.type in ('compound_statement',):  # Skip function body
                return
            if node.type in ('attribute_specifier', 'gnu_attribute_specifier',
                            '__attribute__'):
                text = self._node_text(node, source_bytes)
                if 'constructor' in text:
                    result["constructor"] = True
                if 'destructor' in text:
                    result["destructor"] = True
            for child in node.children:
                _scan_node(child, depth + 1)

        _scan_node(func_node)
        return result

    def _detect_api_entry(self, func_name: str, func_node, source_bytes: bytes) -> tuple:
        """C/C++: non-static top-level functions with public naming are API candidates.

        Strict filtering to avoid over-tagging:
        - Must be non-static
        - Must have a recognized public prefix (from --api-prefixes) OR
        - Must be in a recognized public header file pattern (lib/*.h, include/*.h, etc.)
        """
        # Check for 'static' in the function's type qualifiers
        is_static = False
        for child in func_node.children:
            if child.type == 'storage_class_specifier':
                if self._node_text(child, source_bytes) == 'static':
                    is_static = True
                    break
        # Check the full text for 'static' keyword before the function name
        func_text = self._node_text(func_node, source_bytes)
        first_line = func_text.split('\n')[0]
        if _STATIC_PREFIX_RE.match(first_line):
            is_static = True

        if is_static:
            return False, ""

        # Public naming patterns — project-specific from --api-prefixes or profile
        _DEFAULT_PUBLIC_PREFIXES = ()
        # Merge with project-specific prefixes from scanner config
        extra_prefixes = tuple(getattr(self, '_api_prefixes', []))
        _STRICT_PUBLIC_PREFIXES = _DEFAULT_PUBLIC_PREFIXES + extra_prefixes
        _INTERNAL_PATTERNS = (
            '_unit_', '_ut_', '_test_', '_perf_', '_verify_', '_example_',
            '_internal', '_priv', '_stub', '_mock',
        )
        _CALLBACK_SUFFIXES = (
            '_cb', '_done', '_completion', '_cpl', '_event',
        )
        # Public header paths: prefer profile config, fall back to defaults.
        # Profile's api_detection.public_header_paths is authoritative —
        # it avoids false positives like lib/zstd/ internal functions.
        _profile_header_paths = getattr(self, '_public_header_paths', None)
        if _profile_header_paths:
            _PUBLIC_HEADER_PATTERNS = tuple(p.rstrip('/') + '/' for p in _profile_header_paths)
        else:
            _PUBLIC_HEADER_PATTERNS = (
                'include/lib/', 'lib/', 'public/',
            )

        func_lower = func_name.lower()
        has_public_prefix = any(func_lower.startswith(p) for p in _STRICT_PUBLIC_PREFIXES)
        has_internal_pattern = any(p in func_lower for p in _INTERNAL_PATTERNS)
        # Callback-named functions (e.g., disk_readv_cb) are NOT public API
        # Use shared method from BaseScanner for consistency
        is_callback_name = self._is_callback_by_name(func_name)

        # Determine if this looks like a public header file
        # (use _current_filepath set by _extract_func before calling this)
        current_fp = getattr(self, '_current_filepath', '')

        # Paths that should never produce API_entry functions.
        # Baseline is a generic, project-agnostic list (tools/, samples/, etc.).
        # Project-specific paths (test/, examples/, app/, scripts/, doc/, ...) come
        # from profile.project_boundaries.non_api_paths via scanner._non_api_paths.
        _NON_API_PATHS = (
            'tools/', 'scripts/', 'selftests/', 'testing/',
            'documentation/', 'samples/', 'examples/',
        ) + tuple(getattr(self, '_non_api_paths', []) or [])
        current_fp_normalized = current_fp.replace(os.sep, '/')
        for nap in _NON_API_PATHS:
            if nap and nap in current_fp_normalized:
                return False, ""

        is_public_header = False
        for pat in _PUBLIC_HEADER_PATTERNS:
            if pat in current_fp.replace(os.sep, '/'):
                is_public_header = True
                break

        # Rule: tag as API only if it's named like public API or in a public header
        # AND it doesn't look like internal test/perf code or callback
        if has_internal_pattern or is_callback_name:
            return False, ""

        if not (has_public_prefix or is_public_header):
            return False, ""

        # This is a public function — extract parameter constraints
        params_node = next((c for c in func_node.children if c.type == 'parameter_list'), None)
        constraints = self._extract_param_constraints(
            self._node_text(params_node, source_bytes) if params_node else "()"
        )
        return True, constraints

    def _extract_inheritance(self, class_node, source_bytes: bytes, source_root: str,
                             filepath: str, domain: str, edges: list):
        """Extract C++ class/struct inheritance relationships as INHERITS edges.

        For a class like `class Foo : public Bar, private Baz {}`,
        creates edges: Foo --INHERITS--> Bar, Foo --INHERITS--> Baz
        """
        # Get class name
        name_node = next((c for c in class_node.children
                          if c.type == 'type_identifier'), None)
        if not name_node:
            return
        class_name = self._node_text(name_node, source_bytes)
        class_id = self._make_func_id(domain, class_name)

        # Find base_class_clause
        base_clause = next((c for c in class_node.children
                            if c.type == 'base_class_clause'), None)
        if not base_clause:
            return

        # Each child of base_class_clause that is a type_identifier or qualified_identifier
        for child in base_clause.children:
            if child.type in ('type_identifier', 'qualified_identifier'):
                base_name = self._node_text(child, source_bytes)
                base_id = self._make_func_id("external", base_name)
                # Check if base class is in same domain (same file or directory pattern)
                base_source = os.path.relpath(filepath, source_root)
                base_dir = os.path.dirname(base_source)
                # Try to resolve base class to a local domain
                for d in [domain, domain.rsplit(".", 1)[0] if "." in domain else domain]:
                    test_id = self._make_func_id(d, base_name)
                    # We'll resolve these later; for now use external as placeholder
                    break
                edges.append({
                    "source": class_id,
                    "target": base_id,
                    "relation": "INHERITS",
                    "access": "",  # public/protected/private — would need to parse
                    "source_file": os.path.relpath(filepath, source_root),
                    "confidence": "EXTRACTED",
                })

    def _extract_calls(self, body_node, source_bytes, invoker_id, domain,
                       edges, pp_conds, source_text, func_start_byte,
                       callee_args_list, cond_vars_list, pp_liveness=None,
                       ifdef_conds=None, fn_ptr_calls_global=None,
                       macro_regs_global=None,
                       goto_jumps_list=None, goto_labels_list=None):
        """Walk the body AST to extract call sites with order and conditions."""
        cond_stack = []  # list of {"condition": str, "empty_id": str, "has_calls": bool}

        # Collect all call_expressions and if/switch statements in body order
        # Walk the body's direct children in order (statement by statement)
        self._walk_body(body_node, source_bytes, invoker_id, domain, edges,
                        cond_stack, pp_conds, source_text,
                        func_start_byte, callee_args_list, cond_vars_list,
                        pp_liveness, ifdef_conds, fn_ptr_calls_global,
                        macro_regs_global, goto_jumps_list, goto_labels_list)

    def _walk_body(self, body_node, source_bytes, invoker_id, domain, edges,
                   cond_stack, pp_conds, source_text,
                   func_start_byte, callee_args_list, cond_vars_list,
                   pp_liveness=None, ifdef_conds=None, fn_ptr_calls_global=None,
                   macro_regs_global=None,
                   goto_jumps_list=None, goto_labels_list=None):
        """Recursively walk statement nodes to preserve order."""
        call_order = [0]  # Use list for mutability in nested function
        if fn_ptr_calls_global is None:
            fn_ptr_calls_global = []
        _macro_regs_list = macro_regs_global if macro_regs_global is not None else []
        ifdef_conds = ifdef_conds or []  # Function-level preprocessor conditions
        _goto_jumps = goto_jumps_list if goto_jumps_list is not None else []
        _goto_labels = goto_labels_list if goto_labels_list is not None else []

        # Pre-scan: collect ALL label positions in the body so that
        # forward goto direction can be determined correctly.
        def _collect_labels(node):
            """Recursively collect labeled_statement positions."""
            if node.type == 'labeled_statement':
                for child in node.children:
                    if child.type == 'statement_identifier':
                        _goto_labels.append({
                            "label": self._node_text(child, source_bytes),
                            "line": node.start_point[0] + 1,
                        })
                        break
            for child in node.children:
                _collect_labels(child)
        _collect_labels(body_node)

        # Pre-compute active condition at each pp_cond position for O(log P)
        # lookup per call_expression, replacing the O(P) linear scan that
        # was inside _get_pp_condition.
        _pp_cond_positions = []
        _pp_cond_snapshots = []  # (position, last_active_cond_text_or_None)
        _active_stack = []
        for _pos, _directive, _cond in pp_conds:
            if _directive in ('if', 'ifdef', 'ifndef'):
                _active_stack.append((_directive, _cond))
            elif _directive == 'elif' and _active_stack:
                _active_stack[-1] = ('elif', _cond)
            elif _directive == 'else' and _active_stack:
                _prev_d, _prev_c = _active_stack[-1]
                if _prev_d == 'ifdef':
                    _active_stack[-1] = ('ifndef', _prev_c)
                elif _prev_d == 'ifndef':
                    _active_stack[-1] = ('ifdef', _prev_c)
                elif _prev_d in ('if', 'elif'):
                    _active_stack[-1] = ('ifndef', _prev_c)
            elif _directive == 'endif' and _active_stack:
                _active_stack.pop()
            _last_cond = None
            if _active_stack:
                _d, _c = _active_stack[-1]
                _last_cond = _c.strip() if _c else _d
            _pp_cond_positions.append(_pos)
            _pp_cond_snapshots.append((_pos, _last_cond))

        # _pos values are CHAR offsets in the decoded source_text, but
        # _get_pp_condition receives tree BYTE offsets (node.start_byte).
        # Convert both parallel arrays (no-op for pure-ASCII text).
        _byte_pos = _char_offsets_to_bytes(source_text, _pp_cond_positions)
        _pp_cond_positions = _byte_pos
        _pp_cond_snapshots = [
            (_byte_pos[i], _snap[1]) for i, _snap in enumerate(_pp_cond_snapshots)]

        def _get_pp_condition(byte_offset):
            """Find active preprocessor condition at a byte offset."""
            if not _pp_cond_positions:
                parts = list(ifdef_conds) if ifdef_conds else []
                return " && ".join(parts) if parts else None
            import bisect
            idx = bisect.bisect_left(_pp_cond_positions, byte_offset)
            if idx > 0:
                _pos, _last_cond = _pp_cond_snapshots[idx - 1]
            else:
                _last_cond = None
            parts = []
            if ifdef_conds:
                parts.extend(ifdef_conds)
            if _last_cond:
                already_present = any(_last_cond in p for p in parts)
                if not already_present:
                    parts.append(_last_cond)
            if parts:
                return " && ".join(parts)
            return None

        def _process_node(node):
            if node.type == 'call_expression':
                callee_name = self._extract_callee_name(node, source_bytes)
                # Detect indirect calls through function pointers (field_expression or pointer_expression)
                # Handles patterns: ops->read(...), (*func_ptr)(...), (*ops->read)(...)
                # Also detects bare callback identifiers: cb_func(...), handler(...), etc.
                is_fn_ptr_call = False
                fn_ptr_expr = ""
                func_node = node.child_by_field_name('function')
                if func_node is None:
                    func_node = node.children[0] if node.children else None
                # Unwrap parenthesized_expression to get the actual callee
                _fn = func_node
                while _fn and _fn.type == 'parenthesized_expression':
                    _fn = _fn.children[1] if len(_fn.children) >= 3 else (_fn.children[0] if _fn.children else None)
                if _fn and _fn.type == 'field_expression':
                    is_fn_ptr_call = True
                    fn_ptr_expr = self._node_text(_fn, source_bytes)  # e.g. "ops->read"
                elif _fn and _fn.type == 'pointer_expression':
                    is_fn_ptr_call = True
                    # For (*ops->read), show the full expression including the inner field
                    inner = _fn.child_by_field_name('value') or (_fn.children[1] if len(_fn.children) > 1 else None)
                    if inner and inner.type == 'field_expression':
                        fn_ptr_expr = self._node_text(inner, source_bytes)  # e.g. "ops->read"
                    else:
                        fn_ptr_expr = self._node_text(_fn, source_bytes)  # e.g. "(*callback)"
                elif _fn and _fn.type == 'identifier':
                    # Heuristic: detect function pointer calls where the identifier
                    # name suggests it's a callback/function pointer variable.
                    # This catches patterns like: callback(args), handler(args),
                    # fn(args), cb(args), notify(args), etc.
                    # O8: when profile.dispatch_tuning.fn_ptr_call_require_evidence
                    # is True, skip this name-based heuristic — it produces false
                    # positives on projects where regular functions happen to
                    # match a callback-ish name (e.g., a function named "notify").
                    # Only explicit field_expression/pointer_expression indirect
                    # calls count as fn_ptr evidence in that mode.
                    if not self._fn_ptr_call_require_evidence:
                        _id_name = self._node_text(_fn, source_bytes)
                        _cb_suffixes = ('_cb', '_callback', '_handler', '_func', '_fn',
                                         '_notify', '_hook', '_dispatch', '_listener',
                                         'callback', 'handler', 'cb_fn', 'cb_func')
                        _cb_prefixes = ('cb_', 'fn_', 'notify_', 'hook_', 'dispatch_')
                        _id_lower = _id_name.lower()
                        if any(_id_lower.endswith(s) for s in _cb_suffixes) or \
                           any(_id_lower.startswith(p) for p in _cb_prefixes):
                            is_fn_ptr_call = True
                            fn_ptr_expr = _id_name
                if callee_name:
                    call_order[0] += 1

                    # Capture call arguments
                    args_text = self._extract_callee_args(node, source_bytes)
                    args_structured = self._extract_callee_args_structured(node, source_bytes)
                    concurrency_info = self._detect_concurrency_info(callee_name, args_structured)
                    callee_args_list.append({
                        "call_order": call_order[0],
                        "callee": callee_name,
                        "args_snippet": args_text,
                        "args": args_structured,
                        "concurrency_info": concurrency_info,
                        "line": node.start_point[0] + 1,
                        "column": node.start_point[1] + 1,
                        "start_byte": node.start_byte,
                        "end_byte": node.end_byte,
                    })

                    # Detect macro registration calls (e.g., module_init, module_platform_driver)
                    # These are function-like macros that register driver/callback handlers.
                    # The builder's macro_dispatch logic uses macro_registrations to create
                    # dispatch edges, so we must capture them here.
                    if self._macro_dispatch_patterns and callee_name in self._macro_dispatch_patterns:
                        _md_pat = self._macro_dispatch_patterns[callee_name]
                        _struct_arg_idx = _md_pat.get("struct_arg_index", 0)
                        # struct_arg_index is 0-based; args_structured pos is 1-based
                        _arg_pos = _struct_arg_idx + 1
                        _struct_var = ""
                        for a in args_structured:
                            if a.get("pos") == _arg_pos:
                                _struct_var = a.get("value", "").lstrip('*& ')
                                break
                        if _struct_var:
                            _macro_regs_list.append({
                                "macro_name": callee_name,
                                "struct_var": _struct_var,
                                "source_file": self._current_filepath,
                                "line": node.start_point[0] + 1,
                            })

                    # Detect callback arguments passed to registration functions.
                    # Profile callback_patterns maps register_func -> (cb_arg_index, concurrency_type).
                    # When a call to a known registration function is found, extract the
                    # callback function name from the specified argument position and
                    # create a CALLBACK_ARG edge.
                    # cb_arg_index of -1 means the function is a submit (not a callback arg).
                    if self._callback_patterns and callee_name in self._callback_patterns:
                        _cb_arg_idx, _cb_concurrency = self._callback_patterns[callee_name]
                        if _cb_arg_idx >= 0:
                            # cb_arg_index is 0-based; args_structured pos is 1-based
                            _cb_arg_pos = _cb_arg_idx + 1
                            _cb_target = ""
                            for a in args_structured:
                                if a.get("pos") == _cb_arg_pos:
                                    _cb_target = a.get("value", "").lstrip('*& ')
                                    break
                            # Filter: target must be a simple identifier (not NULL,
                            # not a field expression like notify->work, not a number)
                            if _cb_target and _cb_target != callee_name:
                                if _cb_target in ('NULL', 'null', '0') or '->' in _cb_target or '.' in _cb_target:
                                    _cb_target = ""
                            if _cb_target and re.match(r'^[a-zA-Z_]\w*$', _cb_target):
                                # Create CALLBACK_ARG edge
                                edges.append({
                                    "source": invoker_id,
                                    "target": _cb_target,
                                    "call_order": call_order[0],
                                    "call_condition": "",
                                    "confidence": "CALLBACK_ARG",
                                    "concurrency": _cb_concurrency,
                                    "source_tag": "callback_arg",
                                    "preproc_condition": "",
                                    "preproc_alive": True,
                                    "evidence": f"callback_arg: {callee_name}() arg#{_cb_arg_idx}={_cb_target}",
                                })

                    current_condition = ""
                    target_empty = None

                    if cond_stack:
                        scope = cond_stack[-1]
                        scope["has_calls"] = True
                        current_condition = scope["condition"]
                        target_empty = scope["empty_id"]

                    # Check preprocessor condition from regex as fallback.
                    # The cond_stack already includes AST-level preproc conditions
                    # (from preproc_ifdef/preproc_if in _process_node), so only
                    # use the regex result if cond_stack is empty.
                    pp_cond = _get_pp_condition(node.start_byte)
                    if pp_cond and not current_condition:
                        current_condition = pp_cond

                    # Check if this call is in a dead preprocessor range
                    call_in_dead_pp = pp_liveness and self._is_in_dead_range(
                        node.start_byte, pp_liveness)

                    # Build edge with concurrency info
                    line_no = node.start_point[0] + 1
                    col_no = node.start_point[1] + 1
                    byte_start = node.start_byte
                    byte_end = node.end_byte

                    # For indirect calls through function pointers, use AMBIGUOUS confidence
                    # and record as fn_ptr_call for vtable dispatch resolution
                    if is_fn_ptr_call and not call_in_dead_pp:
                        edge_base = {
                            "target": callee_name.lower(),
                            "call_order": call_order[0],
                            "call_condition": current_condition,
                            "confidence": "AMBIGUOUS",
                            "source_tag": self._source_tag(),
                            "confidence_score": 0.3,
                            "concurrency": "fn_ptr",
                            "line": line_no,
                            "column": col_no,
                            "start_byte": byte_start,
                            "end_byte": byte_end,
                            "evidence": [{"kind": "fn_ptr_call", "weight": 0.3,
                                          "note": f"indirect call via {fn_ptr_expr} at line {line_no}"}],
                        }
                        if pp_cond:
                            edge_base["preproc_condition"] = pp_cond
                            edge_base["preproc_alive"] = True
                        # Record as fn_ptr_call for vtable dispatch resolution
                        # Parse struct_chain and field_name from fn_ptr_expr
                        # e.g. "file->f_op->write" → struct_chain="file->f_op", field_name="write"
                        # e.g. "ops->read" → struct_chain="ops", field_name="read"
                        # e.g. "obj.method" → struct_chain="obj", field_name="method"
                        # e.g. "*func_ptr" → struct_chain="", field_name="func_ptr"
                        # IMPORTANT: Must match multi-level arrow chains like a->b->c
                        # where field_name is the LAST segment (the actual method name)
                        _struct_chain = ""
                        _field_name = callee_name.lower()
                        _arrow_m = re.match(r'(.+?)\s*->\s*(\w+)$', fn_ptr_expr)
                        _dot_m = re.match(r'(\w+)\.(\w+)', fn_ptr_expr)
                        if _arrow_m:
                            _struct_chain = _arrow_m.group(1)
                            _field_name = _arrow_m.group(2)
                        elif _dot_m:
                            _struct_chain = _dot_m.group(1)
                            _field_name = _dot_m.group(2)
                        fn_ptr_calls_global.append({
                            "caller": invoker_id,
                            "callee_name": callee_name.lower(),
                            "fn_ptr_expr": fn_ptr_expr,
                            "field_name": _field_name,
                            "struct_chain": _struct_chain,
                            "call_order": call_order[0],
                            "line": line_no,
                            "column": col_no,
                            "start_byte": byte_start,
                            "end_byte": byte_end,
                        })
                    elif call_in_dead_pp:
                        edge_base = {
                            "target": callee_name.lower(),
                            "call_order": call_order[0],
                            "call_condition": current_condition,
                            "confidence": "AMBIGUOUS",
                            "source_tag": "preproc_dead",
                            "confidence_score": 0.0,
                            "preproc_condition": pp_cond or "",
                            "preproc_alive": False,
                            "line": line_no,
                            "column": col_no,
                            "start_byte": byte_start,
                            "end_byte": byte_end,
                            "evidence": [{"kind": "ast_call", "weight": 0.0,
                                          "note": f"dead branch: {pp_cond or '#ifdef'}, line {line_no}"}],
                        }
                    else:
                        edge_base = {
                            "target": callee_name.lower(),
                            "call_order": call_order[0],
                            "call_condition": current_condition,
                            "confidence": self._confidence_tag(),
                            "source_tag": self._source_tag(),
                            "confidence_score": 1.0,
                            "line": line_no,
                            "column": col_no,
                            "start_byte": byte_start,
                            "end_byte": byte_end,
                            "evidence": [{"kind": "ast_call", "weight": 1.0,
                                          "note": f"direct call at line {line_no}"}],
                        }
                        if pp_cond:
                            edge_base["preproc_condition"] = pp_cond
                            edge_base["preproc_alive"] = True
                    # Add concurrency attribute for thread spawn / callback
                    if concurrency_info.get("is_spawn"):
                        edge_base["concurrency"] = "thread_spawn"
                    elif concurrency_info.get("concurrency_type") == "callback_register":
                        edge_base["concurrency"] = "callback"

                    if target_empty and current_condition:
                        edges.append({
                            **edge_base,
                            "source": target_empty,
                            "is_cond_child": True,
                            "call_condition": current_condition,
                        })
                    else:
                        edges.append({
                            **edge_base,
                            "source": invoker_id,
                            "call_condition": current_condition,
                        })

                    # If this is a thread spawn, also add an edge to the spawned function
                    # and add callback_target to callee_args_list for the spawned function
                    if concurrency_info.get("spawn_target"):
                        spawn_target_name = concurrency_info["spawn_target"]
                        spawn_conf = "AMBIGUOUS" if call_in_dead_pp else "INFERRED"
                        spawn_score = 0.0 if call_in_dead_pp else 0.85
                        spawn_source = "preproc_dead" if call_in_dead_pp else self._source_tag()
                        spawn_evidence = [{"kind": "thread_spawn", "weight": spawn_score,
                                           "note": f"spawn target: {spawn_target_name}, line {line_no}"}]
                        spawn_edge = {
                            "source": invoker_id,
                            "target": spawn_target_name.lower(),
                            "call_order": call_order[0],
                            "call_condition": current_condition,
                            "concurrency": "spawn_target",
                            "confidence": spawn_conf,
                            "source_tag": spawn_source,
                            "confidence_score": spawn_score,
                            "evidence": spawn_evidence,
                        }
                        edges.append(spawn_edge)
                        # Record the spawn relationship in callee_args
                        callee_args_list[-1]["callback_target"] = spawn_target_name

                    # D12: MACRO_EXPANDS_TO edges — when the callee is a
                    # function-like macro defined in this file, expand it
                    # and add edges to the calls inside the macro body.
                    _macro_defs = getattr(self, '_macro_defs', {}) or {}
                    if callee_name in _macro_defs:
                        _mdef = _macro_defs[callee_name]
                        # Get the actual arguments as text for substitution
                        _m_args = []
                        for a in args_structured:
                            _m_args.append(a.get("value", ""))
                        _expanded = self._expand_macro(_mdef, _m_args)
                        _inner_callees = self._extract_macro_calls_from_body(_expanded)
                        for _ic in _inner_callees:
                            edges.append({
                                "source": invoker_id,
                                "target": _ic,
                                "call_order": call_order[0],
                                "call_condition": current_condition,
                                "confidence": "INFERRED",
                                "source_tag": "macro_expand",
                                "confidence_score": 0.7,
                                "line": line_no,
                                "column": col_no,
                                "evidence": [{
                                    "kind": "macro_expand",
                                    "weight": 0.7,
                                    "note": f"macro {callee_name}() expands to call {_ic}() at line {line_no}",
                                }],
                            })

                # Recurse into the callee chain and the FULL argument list
                # to find nested call expressions at ANY depth. The old
                # depth-1 (plus one cast level) scan lost calls wrapped in
                # intermediate expressions — foo(a + bar(x)),
                # foo((bar(x))), foo(c ? bar(x) : baz(x)), foo(v = bar(x)),
                # foo(-bar(x)), foo(arr[bar(x)]), foo(sizeof(bar(x))) — and
                # always lost calls inside the callee chain
                # (get_ops()->handler(x) never produced a get_ops edge).
                # Each found call is dispatched to _process_node, which
                # recurses into ITS arguments the same way, so arbitrarily
                # deep nesting is covered.
                _nested_calls = []
                _callee_node = node.child_by_field_name('function')
                if _callee_node is not None:
                    if _callee_node.type == 'call_expression':
                        # bar(x)(y) / bar(baz(x))(y): the outer call's
                        # fallback already resolved the callee name from
                        # the call text — collect from the callee-call's
                        # own callee/args instead of the callee itself to
                        # avoid a duplicate edge for the same name.
                        _collect_call_expressions(
                            _callee_node.child_by_field_name('function'),
                            _nested_calls)
                        _collect_call_expressions(
                            _callee_node.child_by_field_name('arguments'),
                            _nested_calls)
                    else:
                        _collect_call_expressions(_callee_node, _nested_calls)
                _collect_call_expressions(
                    node.child_by_field_name('arguments'), _nested_calls)
                for _nc in _nested_calls:
                    _process_node(_nc)
                return  # Don't recurse further into call_expression structure

            # === gnu_asm_expression: extract call instructions from inline asm ===
            if node.type == 'gnu_asm_expression':
                self._process_inline_asm(node, source_bytes, invoker_id, domain,
                                         edges, call_order, callee_args_list)
                return

            if node.type == 'if_statement':
                cond_expr = self._extract_if_condition(node, source_bytes)
                cond_label = f"if({cond_expr})" if cond_expr else "if"
                empty_id = self._make_empty_id(invoker_id, len(cond_stack))

                # Capture condition variables
                cvars = self._extract_condition_vars(cond_expr)
                if cvars:
                    cond_vars_list.append({"condition": cond_label, "vars": cvars})

                cond_stack.append({"condition": cond_label, "empty_id": empty_id, "has_calls": False})

                # Process condition subtree — calls inside if-condition (e.g., if (validate() && process()))
                # must be extracted. Use a scope that marks calls with the condition itself.
                condition_node = node.child_by_field_name('condition')
                if condition_node:
                    # Temporarily push a condition scope for calls within the condition expression
                    cond_cond_label = f"if_cond({cond_expr})" if cond_expr else "if_cond"
                    cond_cond_stack_entry = {"condition": cond_cond_label, "empty_id": empty_id + "_cond", "has_calls": False}
                    cond_stack.append(cond_cond_stack_entry)
                    _process_node(condition_node)
                    cond_scope = cond_stack.pop()
                    if cond_scope["has_calls"]:
                        edges.append({
                            "source": invoker_id,
                            "target": cond_scope["empty_id"],
                            "call_order": None,
                            "call_condition": cond_scope["condition"],
                            "confidence": self._confidence_tag(),
                            "source_tag": self._source_tag(),
                            "confidence_score": 1.0,
                        })

                # Process consequence
                consequence = node.child_by_field_name('consequence')
                if consequence:
                    _process_node(consequence)

                # Process alternative (else / else if)
                alternative = node.child_by_field_name('alternative')
                if alternative:
                    # Close the if-scope
                    scope = cond_stack[-1]
                    if scope["has_calls"]:
                        edges.append({
                            "source": invoker_id,
                            "target": scope["empty_id"],
                            "call_order": None,
                            "call_condition": scope["condition"],
                            "confidence": self._confidence_tag(),
                            "source_tag": self._source_tag(),
                            "confidence_score": 1.0,
                        })
                    # else scope
                    else_cond = f"!({cond_expr})" if cond_expr else "else"
                    cond_stack[-1] = {"condition": else_cond,
                                      "empty_id": scope["empty_id"] + "_else",
                                      "has_calls": False}
                    _process_node(alternative)

                # Pop condition scope
                scope = cond_stack.pop()
                if scope["has_calls"]:
                    edges.append({
                        "source": invoker_id,
                        "target": scope["empty_id"],
                        "call_order": None,
                        "call_condition": scope["condition"],
                        "confidence": self._confidence_tag(),
                        "source_tag": self._source_tag(),
                        "confidence_score": 1.0,
                    })
                return

            if node.type == 'switch_statement':
                # Process condition expression (e.g., switch(get_key()) → extract get_key() call)
                cond_node = node.child_by_field_name('condition')
                if cond_node:
                    cond_text = self._node_text(cond_node, source_bytes).strip()
                    cond_stack.append({"condition": f"switch({cond_text})",
                                       "empty_id": self._make_empty_id(invoker_id, len(cond_stack)),
                                       "has_calls": False})
                    _process_node(cond_node)
                    scope = cond_stack.pop()
                    if scope["has_calls"]:
                        edges.append({
                            "source": invoker_id,
                            "target": scope["empty_id"],
                            "call_order": None,
                            "call_condition": scope["condition"],
                            "confidence": self._confidence_tag(),
                            "source_tag": self._source_tag(),
                            "confidence_score": 1.0,
                        })
                # Process body which contains case statements
                body = node.child_by_field_name('body')
                if body:
                    for child in body.children:
                        if child.type in ('case_statement',):
                            case_cond = self._node_text(child, source_bytes).strip()
                            empty_id = self._make_empty_id(invoker_id, len(cond_stack))
                            cond_stack.append({"condition": case_cond, "empty_id": empty_id, "has_calls": False})
                            for stmt in child.children:
                                if stmt.type not in ('case', 'default', ':'):
                                    _process_node(stmt)
                            scope = cond_stack.pop()
                            if scope["has_calls"]:
                                edges.append({
                                    "source": invoker_id,
                                    "target": scope["empty_id"],
                                    "call_order": None,
                                    "call_condition": scope["condition"],
                                    "confidence": self._confidence_tag(),
                                    "source_tag": self._source_tag(),
                                    "confidence_score": 1.0,
                                })
                return

            if node.type in ('preproc_ifdef', 'preproc_if'):
                # Preprocessor conditional inside the function body
                condition = self._extract_pp_condition_from_node(node, source_bytes)
                cond_label = condition if condition else f"#{node.type}"
                empty_id = self._make_empty_id(invoker_id, len(cond_stack))

                cond_stack.append({"condition": cond_label, "empty_id": empty_id, "has_calls": False})

                # Process all children (includes body and possibly preproc_elif/preproc_else)
                for child in node.children:
                    _process_node(child)

                # Pop the ifdef/if scope
                scope = cond_stack.pop()
                if scope["has_calls"]:
                    edges.append({
                        "source": invoker_id,
                        "target": scope["empty_id"],
                        "call_order": None,
                        "call_condition": scope["condition"],
                        "confidence": self._confidence_tag(),
                        "source_tag": self._source_tag(),
                        "confidence_score": 1.0,
                    })
                return

            if node.type == 'preproc_elif':
                # elif inside a function body: replaces current cond_stack top
                if cond_stack:
                    prev_scope = cond_stack[-1]
                    # Close previous branch scope if it had calls
                    if prev_scope["has_calls"]:
                        edges.append({
                            "source": invoker_id,
                            "target": prev_scope["empty_id"],
                            "call_order": None,
                            "call_condition": prev_scope["condition"],
                            "confidence": self._confidence_tag(),
                            "source_tag": self._source_tag(),
                            "confidence_score": 1.0,
                        })
                    elif_cond = self._extract_pp_condition_from_node(node, source_bytes)
                    # The elif condition: parent condition negated AND elif condition true
                    parent_cond = prev_scope["condition"]
                    elif_label = f"!({parent_cond}) && {elif_cond}" if elif_cond else f"!({parent_cond})"
                    cond_stack[-1] = {"condition": elif_label,
                                      "empty_id": prev_scope["empty_id"] + "_elif",
                                      "has_calls": False}
                    for child in node.children:
                        _process_node(child)
                return

            if node.type == 'preproc_else':
                # else inside a function body: negates current cond_stack top
                if cond_stack:
                    prev_scope = cond_stack[-1]
                    # Close previous branch scope if it had calls
                    if prev_scope["has_calls"]:
                        edges.append({
                            "source": invoker_id,
                            "target": prev_scope["empty_id"],
                            "call_order": None,
                            "call_condition": prev_scope["condition"],
                            "confidence": self._confidence_tag(),
                            "source_tag": self._source_tag(),
                            "confidence_score": 1.0,
                        })
                    parent_cond = prev_scope["condition"]
                    else_label = f"!({parent_cond})"
                    cond_stack[-1] = {"condition": else_label,
                                      "empty_id": prev_scope["empty_id"] + "_else",
                                      "has_calls": False}
                    for child in node.children:
                        _process_node(child)
                return

            # while/for/do loops: track loop condition for calls in condition and body
            if node.type in ('while_statement', 'do_statement'):
                loop_cond = ""
                cond_node = node.child_by_field_name('condition')
                if cond_node:
                    loop_cond = self._node_text(cond_node, source_bytes).strip('() ')
                cond_label = f"while({loop_cond})" if loop_cond else "while"
                empty_id = self._make_empty_id(invoker_id, len(cond_stack))
                cond_stack.append({"condition": cond_label, "empty_id": empty_id, "has_calls": False})

                # Process condition subtree (e.g., while(check()) — extract check())
                if cond_node:
                    cond_cond_label = f"while_cond({loop_cond})" if loop_cond else "while_cond"
                    cond_cond_entry = {"condition": cond_cond_label, "empty_id": empty_id + "_cond", "has_calls": False}
                    cond_stack.append(cond_cond_entry)
                    _process_node(cond_node)
                    cond_scope = cond_stack.pop()
                    if cond_scope["has_calls"]:
                        edges.append({
                            "source": invoker_id,
                            "target": cond_scope["empty_id"],
                            "call_order": None,
                            "call_condition": cond_scope["condition"],
                            "confidence": self._confidence_tag(),
                            "source_tag": self._source_tag(),
                            "confidence_score": 1.0,
                        })

                # Process body
                body = node.child_by_field_name('body')
                if body:
                    _process_node(body)

                scope = cond_stack.pop()
                if scope["has_calls"]:
                    edges.append({
                        "source": invoker_id,
                        "target": scope["empty_id"],
                        "call_order": None,
                        "call_condition": scope["condition"],
                        "confidence": self._confidence_tag(),
                        "source_tag": self._source_tag(),
                        "confidence_score": 1.0,
                    })
                return

            if node.type == 'for_statement':
                # for(init; cond; update) — extract condition and body calls
                # tree-sitter-c provides: initializer, condition, update, body via field names
                loop_cond = ""
                cond_node = node.child_by_field_name('condition')
                init_node = node.child_by_field_name('initializer')
                update_node = node.child_by_field_name('update')

                if cond_node:
                    loop_cond = self._node_text(cond_node, source_bytes).strip('() ')

                cond_label = f"for({loop_cond})" if loop_cond else "for"
                empty_id = self._make_empty_id(invoker_id, len(cond_stack))
                cond_stack.append({"condition": cond_label, "empty_id": empty_id, "has_calls": False})

                # Process condition subtree
                if cond_node:
                    cond_cond_label = f"for_cond({loop_cond})" if loop_cond else "for_cond"
                    cond_cond_entry = {"condition": cond_cond_label, "empty_id": empty_id + "_cond", "has_calls": False}
                    cond_stack.append(cond_cond_entry)
                    _process_node(cond_node)
                    cond_scope = cond_stack.pop()
                    if cond_scope["has_calls"]:
                        edges.append({
                            "source": invoker_id,
                            "target": cond_scope["empty_id"],
                            "call_order": None,
                            "call_condition": cond_scope["condition"],
                            "confidence": self._confidence_tag(),
                            "source_tag": self._source_tag(),
                            "confidence_score": 1.0,
                        })

                # Process init, update, and body
                if init_node:
                    _process_node(init_node)
                if update_node:
                    _process_node(update_node)
                body_node = node.child_by_field_name('body')
                if body_node:
                    _process_node(body_node)

                scope = cond_stack.pop()
                if scope["has_calls"]:
                    edges.append({
                        "source": invoker_id,
                        "target": scope["empty_id"],
                        "call_order": None,
                        "call_condition": scope["condition"],
                        "confidence": self._confidence_tag(),
                        "source_tag": self._source_tag(),
                        "confidence_score": 1.0,
                    })
                return

            if node.type == 'conditional_expression':
                # Ternary: cond ? true_expr : false_expr
                cond_node = node.child_by_field_name('condition')
                cond_text = ""
                if cond_node:
                    cond_text = self._node_text(cond_node, source_bytes).strip('() ')

                # Process condition subtree (may contain calls)
                if cond_node:
                    cond_cond_label = f"ternary_cond({cond_text})" if cond_text else "ternary_cond"
                    cond_cond_entry = {"condition": cond_cond_label, "empty_id": self._make_empty_id(invoker_id, len(cond_stack)) + "_cond", "has_calls": False}
                    cond_stack.append(cond_cond_entry)
                    _process_node(cond_node)
                    cond_scope = cond_stack.pop()
                    if cond_scope["has_calls"]:
                        edges.append({
                            "source": invoker_id,
                            "target": cond_scope["empty_id"],
                            "call_order": None,
                            "call_condition": cond_scope["condition"],
                            "confidence": self._confidence_tag(),
                            "source_tag": self._source_tag(),
                            "confidence_score": 1.0,
                        })

                # Process consequence (true branch)
                consequence = node.child_by_field_name('consequence')
                if consequence:
                    cond_label = f"ternary_true({cond_text})" if cond_text else "ternary_true"
                    empty_id = self._make_empty_id(invoker_id, len(cond_stack))
                    cond_stack.append({"condition": cond_label, "empty_id": empty_id, "has_calls": False})
                    _process_node(consequence)
                    scope = cond_stack.pop()
                    if scope["has_calls"]:
                        edges.append({
                            "source": invoker_id,
                            "target": scope["empty_id"],
                            "call_order": None,
                            "call_condition": scope["condition"],
                            "confidence": self._confidence_tag(),
                            "source_tag": self._source_tag(),
                            "confidence_score": 1.0,
                        })

                # Process alternative (false branch)
                alternative = node.child_by_field_name('alternative')
                if alternative:
                    alt_cond = f"!ternary({cond_text})" if cond_text else "ternary_false"
                    empty_id = self._make_empty_id(invoker_id, len(cond_stack))
                    cond_stack.append({"condition": alt_cond, "empty_id": empty_id, "has_calls": False})
                    _process_node(alternative)
                    scope = cond_stack.pop()
                    if scope["has_calls"]:
                        edges.append({
                            "source": invoker_id,
                            "target": scope["empty_id"],
                            "call_order": None,
                            "call_condition": scope["condition"],
                            "confidence": self._confidence_tag(),
                            "source_tag": self._source_tag(),
                            "confidence_score": 1.0,
                        })
                return

            # === goto_statement: extract goto target label ===
            if node.type == 'goto_statement':
                _target_child = None
                for child in node.children:
                    if child.type == 'statement_identifier':
                        _target_child = child
                        break
                if _target_child:
                    _label_name = self._node_text(_target_child, source_bytes)
                    _goto_line = node.start_point[0] + 1
                    # Determine if this is a backward or forward goto
                    # by comparing line numbers with known labels
                    _direction = "unknown"
                    for _lbl in _goto_labels:
                        if _lbl["label"] == _label_name:
                            if _lbl["line"] < _goto_line:
                                _direction = "backward"
                            elif _lbl["line"] > _goto_line:
                                _direction = "forward"
                            break
                    _goto_jumps.append({
                        "label": _label_name,
                        "line": _goto_line,
                        "direction": _direction,
                    })
                return

            # === labeled_statement: process the labeled body ===
            # Labels were already collected in the pre-scan pass, so
            # we only need to process the body of the labeled statement.
            if node.type == 'labeled_statement':
                # Continue processing the labeled statement's body
                # (a label can precede a call: label: func();)
                for child in node.children:
                    if child.type not in ('statement_identifier', ':'):
                        _process_node(child)
                return

            # Recurse into children for other node types
            for child in node.children:
                _process_node(child)

        for child in body_node.children:
            _process_node(child)

    def _collect_static_fn_ptr_arrays(self, source_text):
        """Collect static function pointer arrays from the source file.

        Finds patterns like:
          static const void * const ops[] = {add_asm, sub_asm, multiply_asm};

        Returns dict: array_name → list of function names in the initializer.
        """
        arrays = {}
        # Match: static [const] type [*] [const] name[] = { func1, func2, ... };
        # (hoisted to module level as _STATIC_ARRAY_RE)
        for m in _STATIC_ARRAY_RE.finditer(source_text):
            arr_name = m.group(1)
            init_list = m.group(2)
            # Extract identifiers from the initializer list
            funcs = re.findall(r'\b([a-zA-Z_]\w*)\b', init_list)
            # Filter out keywords and short names that are clearly not function names
            _C_KEYWORDS = {'const', 'void', 'static', 'extern', 'NULL', '0'}
            funcs = [f for f in funcs if f not in _C_KEYWORDS and len(f) > 2]
            if funcs:
                arrays[arr_name] = funcs
        return arrays

    def _collect_register_bindings(self, func_node, source_bytes):
        """Collect register-bound variable declarations from a function body.

        Finds patterns like:
          register void *fn9 __asm__("x9") = (void *)multiply_asm;
          register int w0 __asm__("w0") = x;

        Also traces variables loaded from static function pointer arrays:
          void *fn = (void *)ops[op];  → fn maps to all functions in ops[]

        Uses regex on source text because tree-sitter-c doesn't fully support
        the 'register type name __asm__("reg") = value' syntax (parses '= value'
        as ERROR nodes).

        Returns dict: variable_name → {"register": "x9", "value": "multiply_asm"}
        or variable_name → {"register": "x9", "candidates": ["add_asm", "sub_asm", ...]}
        for array-backed dispatch. "value" is set only for direct function bindings
        with a cast; "candidates" is set for array-element dispatch.
        """
        bindings = {}

        # Get the source text for the function body
        source_text = source_bytes.decode('utf-8', errors='replace')
        body = func_node.child_by_field_name('body')
        if body is None:
            return bindings

        body_text = source_text[body.start_byte:body.end_byte]

        # Regex to match: register [type] [*]name __asm__("reg") = [(cast)] value;
        # Group 1: variable name, Group 2: register, Group 3: cast (if any), Group 4: value
        # Supports both ARM (x0-x30, w0-w30) and x86 (rax, rdi, rsi, etc.) register names.
        # Re-use the module-level _REG_DECL_RE (line 17) instead of re-defining
        # a duplicate local copy per call.
        for m in _REG_DECL_RE.finditer(body_text):
            var_name = m.group(1)
            asm_reg = m.group(2)
            cast = m.group(3)  # None if no cast
            init_value = m.group(4)
            entry = {"register": asm_reg}
            # Only record value if it looks like a function address (has a cast)
            # Simple variable assignments like "= x" are not function addresses
            if cast:
                entry["value"] = init_value
            bindings[var_name] = entry

        # Collect static fn ptr arrays from the full source text
        fn_ptr_arrays = self._collect_static_fn_ptr_arrays(source_text)

        # Trace variables assigned from array elements: void *fn = (void *)ops[op];
        # Also: type *fn = (type *)ops[idx];
        for m in _ARR_ELEM_RE.finditer(body_text):
            var_name = m.group(1)
            arr_name = m.group(2)
            if arr_name in fn_ptr_arrays and var_name not in bindings:
                bindings[var_name] = {
                    "register": None,
                    "candidates": fn_ptr_arrays[arr_name],
                }

        # If a register-bound variable has no direct value but traces to
        # a variable with candidates, propagate the candidates
        for var_name, info in bindings.items():
            if "value" not in info and "candidates" not in info:
                # Check if value is a variable name that has candidates
                # (register void *x9 __asm__("x9") = fn; where fn has candidates)
                init_match = re.search(
                    r'\bregister\s+\w+\s+\*?\s*' + re.escape(var_name) +
                    r'\s+__asm__\s*\(\s*"[xw]\d+"\s*\)\s*=\s*(\w+)',
                    body_text
                )
                if init_match:
                    ref_var = init_match.group(1)
                    if ref_var in bindings and "candidates" in bindings[ref_var]:
                        info["candidates"] = bindings[ref_var]["candidates"]

        return bindings

    def _parse_asm_operands(self, node, source_bytes):
        """Parse GCC extended asm operand list to build %N → variable_name mapping.

        Supports both numeric (%0, %1, ...) and named (%[name]) operands.
        Returns dict with both numeric and named keys:
          numeric: {0: "w0", 1: "w1", 2: "fn9"}
          named:   {"func": "fn9"}  (for %[func] → variable_name)
        Both are merged into a single dict; numeric keys are ints, named keys are strings.
        """
        operand_map = {}
        named_map = {}  # name → variable_name for %[name] syntax
        idx = 0

        for child in node.children:
            if child.type in ('gnu_asm_output_operand_list', 'gnu_asm_input_operand_list'):
                for op_child in child.children:
                    if op_child.type in ('gnu_asm_output_operand', 'gnu_asm_input_operand'):
                        # Check for named operand: [name] "constraint"(variable)
                        op_name = None
                        var_name = None
                        in_brackets = False
                        for oc in op_child.children:
                            if oc.type == '[':
                                in_brackets = True
                            elif oc.type == ']':
                                in_brackets = False
                            elif oc.type == 'identifier':
                                if in_brackets:
                                    # This is the symbolic name [name]
                                    op_name = self._node_text(oc, source_bytes)
                                else:
                                    # This is the variable name
                                    var_name = self._node_text(oc, source_bytes)
                        if var_name:
                            operand_map[idx] = var_name
                            if op_name:
                                named_map[op_name] = var_name
                        idx += 1
                    # Skip commas and colons

        # Merge named_map into operand_map (string keys for named, int for numeric)
        operand_map.update(named_map)
        return operand_map

    def _process_inline_asm(self, node, source_bytes, invoker_id, domain,
                            edges, call_order, callee_args_list):
        """Extract call instructions from gnu_asm_expression nodes.

        Handles inline assembly like:
          __asm__ volatile("call __copy_user" ...);
          asm("bl func_name");

        Produces edges with:
          - confidence: INFERRED for direct calls, AMBIGUOUS for indirect
          - source_tag: "inline_asm" to distinguish from AST call_expression edges
        """
        # 1. Collect all string_literal children (asm template + alternatives)
        asm_parts = []
        for child in node.children:
            if child.type == 'string_literal':
                text = self._node_text(child, source_bytes)
                # Strip surrounding quotes
                if text.startswith('"') and text.endswith('"'):
                    text = text[1:-1]
                elif text.startswith('R"') and ')' in text:
                    # Raw string literal: R"delimiter(...)delimiter"
                    idx = text.index(')') + 1
                    end_delim = text[2:text.index(')')]
                    end_marker = ')' + end_delim + '"'
                    if text.endswith(end_marker):
                        text = text[idx:-(len(end_marker))]
                asm_parts.append(text)

        if not asm_parts:
            return

        asm_text = " ".join(asm_parts)

        # 1b. Collect register bindings from enclosing function
        # Walk up to find the function_definition parent
        func_node = node.parent
        while func_node and func_node.type != 'function_definition':
            func_node = func_node.parent
        reg_bindings = self._collect_register_bindings(func_node, source_bytes) if func_node else {}

        # 1c. Parse inline asm operand map (%N → variable name)
        operand_map = self._parse_asm_operands(node, source_bytes)

        # Build reverse map: variable_name → value (function name if known)
        var_to_func = {}
        var_to_candidates = {}  # For dispatch arrays: var → [func1, func2, ...]
        for var_name, info in reg_bindings.items():
            if "value" in info:
                var_to_func[var_name] = info["value"]
            if "candidates" in info:
                var_to_candidates[var_name] = info["candidates"]

        # Also build register → function map for direct register references (blr x9)
        reg_to_func = {}
        reg_to_candidates = {}  # register → [func1, func2, ...] for dispatch arrays
        for var_name, info in reg_bindings.items():
            if "register" in info and "value" in info:
                reg_to_func[info["register"]] = info["value"]
            if "register" in info and "candidates" in info:
                reg_to_candidates[info["register"]] = info["candidates"]

        # 2. Extract direct call targets from inline asm
        # x86: call func_name
        for m in _ASM_DIRECT_CALL_RE.finditer(asm_text):
            callee_name = m.group(1)
            call_order[0] += 1
            edges.append({
                "source": invoker_id,
                "target": callee_name.lower(),
                "call_order": call_order[0],
                "call_condition": "",
                "confidence": "INFERRED",
                "source_tag": "inline_asm",
                "confidence_score": 0.7,
            })

        # 3. ARM: bl instructions (direct call to label)
        for m in _ASM_DIRECT_BL_RE.finditer(asm_text):
            callee_name = m.group(1)
            call_order[0] += 1
            edges.append({
                "source": invoker_id,
                "target": callee_name.lower(),
                "call_order": call_order[0],
                "call_condition": "",
                "confidence": "INFERRED",
                "source_tag": "inline_asm",
                "confidence_score": 0.7,
            })

        # 3b. jmp tail call targets (x86 jmp label, ARM b label — not b.eq/b.ne)
        # Only match simple unconditional jumps, not conditional branches
        for m in _ASM_TAIL_JMP_RE.finditer(asm_text):
            target = m.group(1)
            call_order[0] += 1
            edges.append({
                "source": invoker_id,
                "target": target.lower(),
                "call_order": call_order[0],
                "call_condition": "",
                "confidence": "INFERRED",
                "source_tag": "inline_asm",
                "confidence_score": 0.6,
            })

        # ARM unconditional branch (b label) — not b.eq/b.ne/b.lt etc.
        for m in re.finditer(r'\bb\s+([a-zA-Z_.]\w*)', asm_text):
            target = m.group(1)
            # Skip if this is actually a conditional branch (b.eq, b.ne, etc.)
            # Check if preceded by a dot
            start = m.start()
            if start > 0 and asm_text[start - 1] == '.':
                continue
            call_order[0] += 1
            edges.append({
                "source": invoker_id,
                "target": target.lower(),
                "call_order": call_order[0],
                "call_condition": "",
                "confidence": "INFERRED",
                "source_tag": "inline_asm",
                "confidence_score": 0.6,
            })

        # 4. Indirect calls: call *%rax, blr %N, blr xN
        # Try to resolve to concrete function names via register bindings
        # Also handles dispatch_op patterns where a register holds one of several
        # candidates from a static function pointer array.
        unresolved_indirect = False

        def _emit_resolved_or_candidates(resolved, candidates_list, reg_name_hint=""):
            """Emit INFERRED edges for resolved target or candidate list."""
            nonlocal unresolved_indirect
            if resolved:
                call_order[0] += 1
                edges.append({
                    "source": invoker_id,
                    "target": resolved.lower(),
                    "call_order": call_order[0],
                    "call_condition": "",
                    "confidence": "INFERRED",
                    "source_tag": "inline_asm",
                    "confidence_score": 0.7,
                })
            elif candidates_list:
                # Dispatch via function pointer table — emit one INFERRED edge per candidate
                for cand in candidates_list:
                    call_order[0] += 1
                    edges.append({
                        "source": invoker_id,
                        "target": cand.lower(),
                        "call_order": call_order[0],
                        "call_condition": "dispatch_op",
                        "confidence": "INFERRED",
                        "source_tag": "inline_asm",
                        "confidence_score": 0.5,
                    })
            else:
                unresolved_indirect = True

        # 4a. x86: call *%rax or call *%N — indirect call via register or operand
        # Register form: call *%rax — check if rax was bound to a function or dispatch array
        for m in _ASM_INDIRECT_CALL_REG_RE.finditer(asm_text):
            reg_name = m.group(1).lower()
            resolved = reg_to_func.get(reg_name)
            candidates = reg_to_candidates.get(reg_name, [])
            _emit_resolved_or_candidates(resolved, candidates, reg_name)

        # Operand form: call *%N — resolve via operand map → variable → function
        for m in re.finditer(r'\bcall\s+\*\s*%(\[\w+\]|\d+)', asm_text):
            op_key_str = m.group(1)
            if op_key_str.startswith('[') and op_key_str.endswith(']'):
                op_key = op_key_str[1:-1]
            else:
                op_key = int(op_key_str)
            resolved = None
            candidates = []
            if op_key in operand_map:
                var_name = operand_map[op_key]
                resolved = var_to_func.get(var_name)
                candidates = var_to_candidates.get(var_name, [])
            _emit_resolved_or_candidates(resolved, candidates)

        # 4b. ARM: blr %N or blr %[name] — resolve via operand map → variable → function
        for m in re.finditer(r'\bblr\s+%(\[\w+\]|\d+)', asm_text):
            op_key_str = m.group(1)
            if op_key_str.startswith('[') and op_key_str.endswith(']'):
                # Named operand: %[name]
                op_key = op_key_str[1:-1]
            else:
                op_key = int(op_key_str)
            resolved = None
            candidates = []
            if op_key in operand_map:
                var_name = operand_map[op_key]
                resolved = var_to_func.get(var_name)
                candidates = var_to_candidates.get(var_name, [])
            _emit_resolved_or_candidates(resolved, candidates)

        # 4c. ARM: blr xN (direct register reference, not %N operand syntax)
        for m in re.finditer(r'\bblr\s+([xw]\d+)\b', asm_text):
            reg_name = m.group(1).lower()
            resolved = reg_to_func.get(reg_name)
            candidates = reg_to_candidates.get(reg_name, [])
            _emit_resolved_or_candidates(resolved, candidates, reg_name)

        # 4d. Generic AMBIGUOUS edge for unresolved indirect calls
        if unresolved_indirect:
            call_order[0] += 1
            edges.append({
                "source": invoker_id,
                "target": "indirect_call",
                "call_order": call_order[0],
                "call_condition": "",
                "confidence": "AMBIGUOUS",
                "source_tag": "inline_asm",
                "confidence_score": 0.2,
            })

        # 5. Syscall/svc instructions from inline asm
        if _ASM_SYSCALL_RE.search(asm_text):
            call_order[0] += 1
            # Try to resolve syscall name from mov to rax/eax before syscall
            syscall_target = "syscall_inline"
            rax_match = re.search(r'\bmov[a-z]*\s+\$?(\d+)\s*,\s*%?(?:rax|eax)\b', asm_text)
            if not rax_match:
                rax_match = re.search(r'%?(?:rax|eax)\s*,\s*\$?(\d+)', asm_text)
            if rax_match:
                try:
                    from _scanner.asm_scanner import _SYSCALL_NAMES
                    nr = int(rax_match.group(1))
                    syscall_name = _SYSCALL_NAMES.get(nr, f"syscall_{nr}")
                    syscall_target = f"syscall_{syscall_name}"
                except (ValueError, ImportError):
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
            edges.append({
                "source": invoker_id,
                "target": syscall_target,
                "call_order": call_order[0],
                "call_condition": "",
                "confidence": "INFERRED",
                "source_tag": "inline_asm",
                "confidence_score": 0.5,
            })

        if re.search(r'\bsvc\s+#0\b', asm_text, re.IGNORECASE):
            call_order[0] += 1
            # Try to resolve AArch64 syscall name from mov to x8 before svc
            svc_target = "svc_inline"
            x8_match = re.search(r'\bmov[zk]?\s+[xw]8\s*,\s*#?(\d+)', asm_text, re.IGNORECASE)
            if x8_match:
                try:
                    from _scanner.asm_scanner import _AARCH64_SYSCALL_NAMES
                    nr = int(x8_match.group(1))
                    svc_name = _AARCH64_SYSCALL_NAMES.get(nr, f"svc_{nr}")
                    svc_target = f"syscall_{svc_name}"
                except (ValueError, ImportError):
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
            edges.append({
                "source": invoker_id,
                "target": svc_target,
                "call_order": call_order[0],
                "call_condition": "",
                "confidence": "INFERRED",
                "source_tag": "inline_asm",
                "confidence_score": 0.5,
            })

        # 5b. RISC-V ecall from inline asm
        if re.search(r'\becall\b', asm_text, re.IGNORECASE):
            call_order[0] += 1
            ecall_target = "ecall_inline"
            # RISC-V: a7 = syscall number (register binding or inline mov)
            a7_match = re.search(r'\bmov[zk]?\s+[xa]7\s*,\s*#?(\d+)', asm_text, re.IGNORECASE)
            if not a7_match:
                a7_match = re.search(r'%?(?:a7|x17)\s*,\s*\$?(\d+)', asm_text, re.IGNORECASE)
            if a7_match:
                try:
                    from _scanner.asm_scanner import _RISCV_SYSCALL_NAMES
                    nr = int(a7_match.group(1))
                    ecall_name = _RISCV_SYSCALL_NAMES.get(nr, f"ecall_{nr}")
                    ecall_target = f"syscall_{ecall_name}"
                except (ValueError, ImportError):
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
            edges.append({
                "source": invoker_id,
                "target": ecall_target,
                "call_order": call_order[0],
                "call_condition": "",
                "confidence": "INFERRED",
                "source_tag": "inline_asm",
                "confidence_score": 0.5,
            })

        # 5c. RISC-V jal from inline asm (direct call)
        for m in re.finditer(r'\bjal\s+(?:[a-zA-Z_]\w*\s*,\s*)?([a-zA-Z_.]\w*)', asm_text, re.IGNORECASE):
            callee_name = m.group(1)
            call_order[0] += 1
            edges.append({
                "source": invoker_id,
                "target": callee_name.lower(),
                "call_order": call_order[0],
                "call_condition": "",
                "confidence": "INFERRED",
                "source_tag": "inline_asm",
                "confidence_score": 0.7,
            })

        # 5d. RISC-V jalr from inline asm (indirect call)
        for m in re.finditer(r'\bjalr\s+(?:[a-zA-Z_]\w*\s*,\s*)?([a-zA-Z_]\w*)', asm_text, re.IGNORECASE):
            reg_name = m.group(1).lower()
            resolved = reg_to_func.get(reg_name)
            candidates = reg_to_candidates.get(reg_name, [])
            _emit_resolved_or_candidates(resolved, candidates, reg_name)

        # 5e. LoongArch bl from inline asm (direct call)
        for m in re.finditer(r'\bbl\s+([a-zA-Z_.]\w*)', asm_text, re.IGNORECASE):
            callee_name = m.group(1)
            call_order[0] += 1
            edges.append({
                "source": invoker_id,
                "target": callee_name.lower(),
                "call_order": call_order[0],
                "call_condition": "",
                "confidence": "INFERRED",
                "source_tag": "inline_asm",
                "confidence_score": 0.7,
            })

        # 5f. LoongArch jirl from inline asm (indirect call)
        for m in re.finditer(r'\bjirl\s+(?:[a-zA-Z_]\w*\s*,\s*)?([a-zA-Z_]\w*)', asm_text, re.IGNORECASE):
            reg_name = m.group(1).lower()
            resolved = reg_to_func.get(reg_name)
            candidates = reg_to_candidates.get(reg_name, [])
            _emit_resolved_or_candidates(resolved, candidates, reg_name)

        # 5g. s390 brasl from inline asm (direct call)
        for m in re.finditer(r'\bbrasl\s+%r\d+\s*,\s*([a-zA-Z_]\w*)', asm_text, re.IGNORECASE):
            callee_name = m.group(1)
            call_order[0] += 1
            edges.append({
                "source": invoker_id,
                "target": callee_name.lower(),
                "call_order": call_order[0],
                "call_condition": "",
                "confidence": "INFERRED",
                "source_tag": "inline_asm",
                "confidence_score": 0.7,
            })

        # 5h. s390 basr from inline asm (indirect call)
        for m in re.finditer(r'\bbasr\s+%r\d+\s*,\s*%r(\d+)', asm_text, re.IGNORECASE):
            reg_name = f"r{m.group(1)}"
            resolved = reg_to_func.get(reg_name)
            candidates = reg_to_candidates.get(reg_name, [])
            _emit_resolved_or_candidates(resolved, candidates, reg_name)

        # 5i. s390 svc from inline asm
        if re.search(r'\bsvc\s+\d+\b', asm_text, re.IGNORECASE):
            call_order[0] += 1
            edges.append({
                "source": invoker_id,
                "target": "syscall_s390",
                "call_order": call_order[0],
                "call_condition": "",
                "confidence": "INFERRED",
                "source_tag": "inline_asm",
                "confidence_score": 0.5,
            })

        # 5j. vmcall/vmmcall from inline asm (virtualization hypercalls)
        if re.search(r'\bvmcall\b', asm_text, re.IGNORECASE):
            call_order[0] += 1
            edges.append({
                "source": invoker_id,
                "target": "syscall_vmcall",
                "call_order": call_order[0],
                "call_condition": "",
                "confidence": "INFERRED",
                "source_tag": "inline_asm",
                "confidence_score": 0.5,
            })
        if re.search(r'\bvmmcall\b', asm_text, re.IGNORECASE):
            call_order[0] += 1
            edges.append({
                "source": invoker_id,
                "target": "syscall_vmmcall",
                "call_order": call_order[0],
                "call_condition": "",
                "confidence": "INFERRED",
                "source_tag": "inline_asm",
                "confidence_score": 0.5,
            })

        # 5k. x86 lcall/lcallw from inline asm (far calls)
        if re.search(r'\blcallw?\s+\*', asm_text, re.IGNORECASE):
            call_order[0] += 1
            edges.append({
                "source": invoker_id,
                "target": "indirect_call",
                "call_order": call_order[0],
                "call_condition": "",
                "confidence": "AMBIGUOUS",
                "source_tag": "inline_asm",
                "confidence_score": 0.3,
            })

        # 6. asm goto — detect jump targets from the goto label list
        # asm goto("jmp %l0" : : : : target_label);
        # The label list is the 5th colon-separated section of the asm statement.
        # tree-sitter-c represents goto labels as gnu_asm_goto_list children.
        for child in node.children:
            if child.type == 'gnu_asm_goto_list':
                for label_child in child.children:
                    if label_child.type == 'identifier':
                        label_name = self._node_text(label_child, source_bytes)
                        # Check if label_name is actually a function (global symbol)
                        # If it's a local label (C label within function), skip it
                        # as it's an intra-function jump, not a call.
                        # We can't easily distinguish, so we record it as a
                        # conditional branch target with low confidence.
                        call_order[0] += 1
                        edges.append({
                            "source": invoker_id,
                            "target": label_name.lower(),
                            "call_order": call_order[0],
                            "call_condition": "asm_goto",
                            "confidence": "AMBIGUOUS",
                            "source_tag": "inline_asm",
                            "confidence_score": 0.3,
                        })

    def _extract_callee_name(self, call_node, source_bytes: bytes) -> str:
        """Extract called function name from a call_expression node."""
        # call_expression > identifier / field_expression / pointer_expression
        func_node = call_node.child_by_field_name('function')
        if func_node is None:
            func_node = call_node.children[0] if call_node.children else None
        if func_node is None:
            return ""

        if func_node.type == 'identifier':
            return self._node_text(func_node, source_bytes)
        if func_node.type == 'qualified_identifier':
            # C++ ns::func / MyClass::staticMethod / ns::inner::deep —
            # take the LAST segment (the function name), matching the
            # field_expression convention below. Without this branch the
            # generic first-identifier fallback resolved ns::func(1) to
            # "ns" — every namespace/class-qualified static call edge
            # pointed at the wrong node.
            _qtext = self._node_text(func_node, source_bytes)
            _last = _qtext.split('::')[-1].strip()
            if _last:
                return _last
            for child in reversed(func_node.children):
                if child.type in ('identifier', 'namespace_identifier'):
                    return self._node_text(child, source_bytes)
            return _qtext
        if func_node.type == 'field_expression':
            # obj->method or Class::method → extract the field identifier
            for child in func_node.children:
                if child.type == 'field_identifier':
                    return self._node_text(child, source_bytes)
            # Fallback: last identifier
            return self._node_text(func_node, source_bytes).split('->')[-1].split('.')[-1].split('::')[-1].strip()
        if func_node.type == 'pointer_expression':
            for child in func_node.children:
                if child.type == 'identifier':
                    return self._node_text(child, source_bytes)
        if func_node.type == 'parenthesized_expression':
            # (*func_ptr)() or (*ops->read)()
            # Unwrap parentheses to find the actual callee expression
            inner = func_node.children[1] if len(func_node.children) >= 3 else func_node.children[0] if func_node.children else None
            if inner and inner.type == 'pointer_expression':
                ptr_val = inner.child_by_field_name('value') or (inner.children[1] if len(inner.children) > 1 else None)
                if ptr_val and ptr_val.type == 'field_expression':
                    # (*ops->read)() → extract field name "read"
                    for child in ptr_val.children:
                        if child.type == 'field_identifier':
                            return self._node_text(child, source_bytes)
                elif ptr_val and ptr_val.type == 'identifier':
                    # (*func_ptr)() → return identifier name
                    return self._node_text(ptr_val, source_bytes)
            # Fallback: try regex
            inner_text = self._node_text(func_node, source_bytes).strip('()')
            m = re.match(r'\*\s*(\w+(?:->\w+|::\w+|\.\w+)*)', inner_text)
            if m:
                # Return the last part after -> or :: or .
                parts = m.group(1).replace('->', '.').replace('::', '.').split('.')
                return parts[-1]

        # Fallback: first identifier-like token
        text = self._node_text(func_node, source_bytes)
        m = re.match(r'([A-Za-z_]\w*)', text)
        return m.group(1) if m else text

    def _extract_if_condition(self, if_node, source_bytes: bytes) -> str:
        cond = if_node.child_by_field_name('condition')
        if cond:
            return self._node_text(cond, source_bytes).strip('() ')
        return ""


# ---------------------------------------------------------------------------
# Go Scanner
# ---------------------------------------------------------------------------

