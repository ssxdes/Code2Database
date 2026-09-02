"""Base scanner class for invocation graph extraction.

Provides shared methods for all language-specific scanners:
AST text extraction, parameter/local-var/concurrency detection,
condition variable extraction, global definition extraction, etc.

Subclasses must implement _parse() and _extract().
"""

import bisect
import hashlib
import os
import re
from abc import ABC, abstractmethod

from _scanner.utils import classify_domain
import logging


# ---------------------------------------------------------------------------
# Base scanner class
# ---------------------------------------------------------------------------

# Module-level constants used by _register_type / _infer_type_from_value.
# Hoisted out of the per-call path so they're constructed once per process
# instead of once per type registration or per local-var inference.
_BUILTIN_C_TYPES = frozenset({
    'void', 'char', 'short', 'int', 'long', 'float', 'double',
    'signed', 'unsigned', 'bool', '_Bool', 'size_t', 'ssize_t',
    'int8_t', 'int16_t', 'int32_t', 'int64_t',
    'uint8_t', 'uint16_t', 'uint32_t', 'uint64_t',
    'int_least8_t', 'int_least16_t', 'int_least32_t', 'int_least64_t',
    'uint_least8_t', 'uint_least16_t', 'uint_least32_t', 'uint_least64_t',
    'int_fast8_t', 'int_fast16_t', 'int_fast32_t', 'int_fast64_t',
    'uint_fast8_t', 'uint_fast16_t', 'uint_fast32_t', 'uint_fast64_t',
    'intptr_t', 'uintptr_t', 'intmax_t', 'uintmax_t',
    'char16_t', 'char32_t', 'wchar_t',
})
_C_STORAGE_kw_re = re.compile(r'\b(?:static|inline|extern|register|auto)\b')
_RETURN_TYPE_kw_re = re.compile(
    r'\b(?:static|inline|extern|register|async|pub|unsafe)\b'
)

# Pre-compiled regexes for _infer_type_from_value (module-level for O(1) lookup)
_INFER_INT_RE = re.compile(r'^-?\d[\d_]*L?$')
_INFER_FLOAT_RE_1 = re.compile(r'^-?\d*\.\d+([eE][+-]?\d+)?$')
_INFER_FLOAT_RE_2 = re.compile(r'^-?\d+\.\d*([eE][+-]?\d+)?$')
_INFER_FLOAT_RE_3 = re.compile(r'^-?\d+[eE][+-]?\d+$')
_INFER_COMPLEX_RE = re.compile(r'^-?\d*\.?\d+[jJ]$')
_INFER_TYPE_CAST_RE = re.compile(r'^(int|str|float|bool|list|dict|set|tuple|bytes|complex)\s*\(')
_INFER_PASCAL_CTOR_RE = re.compile(r'^([A-Z][A-Za-z0-9_]*)\s*\(')
_INFER_QUALIFIED_CTOR_RE = re.compile(r'^([a-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*\.([A-Z][A-Za-z0-9_]*))\s*\(')
_INFER_ARGPARSE_ATTR_RE = re.compile(r'^args\.\w+$')
_INFER_INDEX_RE = re.compile(r'^[A-Za-z_]\w*\s*\[')
_INFER_STR_METHOD_RE = re.compile(
    r'\.(lower|upper|strip|lstrip|rstrip|replace|split|join|format|'
    r'capitalize|title|swapcase|center|ljust|rjust|zfill|encode|decode|'
    r'expandtabs)\s*\('
)
_INFER_LOWER_FIND_RE = re.compile(r'\.(rfind|find|index|rindex|count)\s*\(')
_INFER_BOOL_PRED_RE = re.compile(
    r'\.(startswith|endswith|isalpha|isdigit|isnumeric|isalnum|isupper|'
    r'islower|isspace|isidentifier|isprintable)\s*\('
)
_INFER_ATTR_ANY_RE = re.compile(r'^[a-z_]\w*(\.[A-Za-z_]\w*)+$')
_INFER_METHOD_ANY_RE = re.compile(r'^[a-z_]\w*(\.[A-Za-z_]\w*)+\s*\(')
_INFER_EQ_RE = re.compile(r'(?<![\w.])==(?!=)')
_INFER_NE_RE = re.compile(r'(?<![\w.])!=(?!=)')
_INFER_BOOL_OPS_RE = re.compile(r'\b(and|or|not|in|is)\b')
_INFER_COMPARE_RELOP_RE = re.compile(r'^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*\s*(<|>|<=|>=)')
_INFER_STR_CONCAT_L_RE = re.compile(r'^[\'"].*[\'"]\s*\+\s*')
_INFER_STR_CONCAT_R_RE = re.compile(r'\+\s*[\'"].*[\'"]\s*$')
_INFER_GROUP_RE = re.compile(r'\.group\s*\(')
_INFER_GROUPS_RE = re.compile(r'\.groups\s*\(')
_INFER_PATH_DIV_RE = re.compile(r'^[A-Za-z_]\w*\s*/\s*')
_INFER_PATH_DIV_NUM_RE = re.compile(r'^[A-Za-z_]\w*\s*/\s*\d')
_INFER_USER_FN_RE = re.compile(r'^[a-z_][A-Za-z0-9_]*\s*\(')
_INFER_ARITH_RE = re.compile(r'^\d+\s*[\*\+\-\/]\s*\d')
_INFER_MUL_IDENT_RE = re.compile(r'^[A-Za-z_]\w*\s*\*\s*[A-Za-z_]\w*')
_INFER_MUL_NUM_R_RE = re.compile(r'^[A-Za-z_]\w*\s*\*\s*\d')
_INFER_MUL_NUM_L_RE = re.compile(r'^\d+\s*\*\s*[A-Za-z_]\w*')
_INFER_TIME_DIFF_RE = re.compile(r'time\.\w+\s*\(\s*\)\s*-')
_INFER_TIME_START_DIFF_RE = re.compile(r'-\s*\w+\.\w*_start\b')
_INFER_TO_DICT_RE = re.compile(r'\.(to_dict|as_dict)\s*\(\s*\)\s*$')
_INFER_TO_LIST_RE = re.compile(r'\.(to_list|as_list)\s*\(\s*\)\s*$')
_INFER_TO_STR_RE = re.compile(r'\.(to_string|__str__)\s*\(\s*\)\s*$')
_INFER_TO_JSON_RE = re.compile(r'\.(to_json|dumps)\s*\(\s*\)\s*$')
_INFER_LIST_PLUS_RE = re.compile(r'^\w+\s*\+\s*\[')
_INFER_LIST_PLUS_R_RE = re.compile(r'\[\s*\+\s*\w+')
_INFER_START_POINT_RE = re.compile(r'\.start_point\[\d+\]')
_INFER_END_POINT_RE = re.compile(r'\.end_point\[\d+\]')
_INFER_ADD_INT_L_RE = re.compile(r'^[A-Za-z_]\w*\s*\+\s*\d+\s*$')
_INFER_ADD_INT_R_RE = re.compile(r'^\d+\s*\+\s*[A-Za-z_]\w*\s*$')
_INFER_GET_RE = re.compile(r'\.get\s*\(')
_INFER_SETDEFAULT_RE = re.compile(r'\.setdefault\s*\(')
_INFER_KEYS_RE = re.compile(r'\.keys\s*\(\s*\)\s*$')
_INFER_VALUES_RE = re.compile(r'\.values\s*\(\s*\)\s*$')
_INFER_ITEMS_RE = re.compile(r'\.items\s*\(\s*\)\s*$')
_INFER_LIST_MUT_RE = re.compile(r'\.(append|extend|pop|insert|remove|clear|copy)\s*\(')
_INFER_READ_RE = re.compile(r'\.read\s*\(\s*\)\s*$')
_INFER_READLINES_RE = re.compile(r'\.readlines\s*\(\s*\)\s*$')
_INFER_READLINE_RE = re.compile(r'\.readline\s*\(\s*\)\s*$')

# Dispatch table: first identifier token → handler function
def _infer_os_family(v: str) -> str:
    if v.startswith(('os.environ.get(', 'os.getenv(')):
        return 'str'
    if v.startswith(('os.path.join(', 'os.path.abspath(', 'os.path.realpath(',
                     'os.path.relpath(', 'os.path.normpath(', 'os.path.basename(',
                     'os.path.dirname(')):
        return 'str'
    if v.startswith('os.path.splitext('):
        return 'tuple'
    if v.startswith(('os.path.exists(', 'os.path.isfile(', 'os.path.isdir(',
                      'os.path.isabs(', 'os.path.islink(')):
        return 'bool'
    if v.startswith('os.path.getsize('):
        return 'int'
    if v.startswith('os.listdir('):
        return 'list'
    if v.startswith('os.walk('):
        return 'Generator'
    return ''


def _infer_re_family(v: str) -> str:
    if v.startswith('re.compile('):
        return 're.Pattern'
    if v.startswith(('re.match(', 're.search(')):
        return 'Match'
    if v.startswith('re.finditer('):
        return 'Iterator'
    if v.startswith('re.findall('):
        return 'list'
    if v.startswith(('re.sub(', 're.escape(')):
        return 'str'
    return ''


def _infer_json_family(v: str) -> str:
    if v.startswith(('json.load(', 'json.loads(')):
        return 'Any'
    if v.startswith('json.dumps('):
        return 'str'
    return ''


def _infer_subprocess_family(v: str) -> str:
    if v.startswith('subprocess.run('):
        return 'CompletedProcess'
    if v.startswith('subprocess.Popen('):
        return 'Popen'
    return ''


def _infer_time_family(v: str) -> str:
    if v.startswith(('time.time(', 'time.monotonic(', 'time.perf_counter(')):
        return 'float'
    if _INFER_TIME_DIFF_RE.search(v) or _INFER_TIME_START_DIFF_RE.search(v):
        return 'float'
    return ''


def _infer_collections_family(v: str) -> str:
    if v.startswith(('collections.defaultdict(', 'defaultdict(')):
        return 'defaultdict'
    if v.startswith(('collections.OrderedDict(', 'OrderedDict(')):
        return 'OrderedDict'
    if v.startswith(('collections.Counter(', 'Counter(')):
        return 'Counter'
    if v.startswith(('collections.deque(', 'deque(')):
        return 'deque'
    if v.startswith(('collections.namedtuple(', 'typing.NamedTuple(')):
        return 'NamedTuple'
    return ''


def _infer_importlib_family(v: str) -> str:
    if v.startswith('importlib.util.spec_from_file_location('):
        return 'ModuleSpec'
    if v.startswith('importlib.util.module_from_spec('):
        return 'Module'
    return ''


_INFER_DISPATCH = {
    'os': _infer_os_family,
    're': _infer_re_family,
    'json': _infer_json_family,
    'subprocess': _infer_subprocess_family,
    'time': _infer_time_family,
    'collections': _infer_collections_family,
    'importlib': _infer_importlib_family,
    'defaultdict': lambda v: 'defaultdict' if v.startswith('defaultdict(') else '',
    'OrderedDict': lambda v: 'OrderedDict' if v.startswith('OrderedDict(') else '',
    'Counter': lambda v: 'Counter' if v.startswith('Counter(') else '',
    'deque': lambda v: 'deque' if v.startswith('deque(') else '',
    'frozenset': lambda v: 'frozenset' if v.startswith('frozenset(') else '',
    'open': lambda v: 'IO' if v.startswith('open(') else '',
    'lambda': lambda v: 'Callable' if v.startswith('lambda') else '',
    'True': lambda v: 'bool' if v == 'True' else '',
    'False': lambda v: 'bool' if v == 'False' else '',
    'None': lambda v: 'None' if v == 'None' else '',
    'Path': lambda v: 'Path' if v.startswith('Path(') else '',
    'pathlib': lambda v: 'Path' if v.startswith('pathlib.Path(') else '',
    'getattr': lambda v: 'Any' if v.startswith('getattr(') else '',
    'setattr': lambda v: 'Any' if v.startswith('setattr(') else '',
}

_INFER_FIRST_TOKEN_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)')

# Pre-compiled patterns for per-function hot paths. Previously these were
# `re.match(r'...', text)` / `re.search(r'...', text)` calls inside
# per-function loops, causing ~50M+ redundant regex compilations on
# kernel-scale scans. Python's re cache (~512 entries) thrashes when
# distinct patterns exceed cache capacity.
_CB_DIGIT_SUFFIX_RES = tuple(
    re.compile(rf'.*{re.escape(s)}_\d+$') for s in
    ('_cb', '_callback', '_handler', '_fn', '_done', '_completion', '_cpl', '_event')
)
_HB_ACQUIRE_PAT = re.compile(
    r'\b(?:with\s+([A-Za-z_][A-Za-z0-9_\.]*)\s*:|'
    r'([A-Za-z_][A-Za-z0-9_]*)\.acquire\(\)|'
    r'([A-Za-z_][A-Za-z0-9_]*)\.Lock\(\)|'
    r'([A-Za-z_][A-Za-z0-9_]*)\.lock\(\)|'
    r'pthread_mutex_lock\s*\(\s*&?\s*([A-Za-z_][A-Za-z0-9_]*)|'
    r'([A-Za-z_][A-Za-z0-9_]*)\.lock\(\)\.unwrap\(\)|'
    r'synchronized\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\))'
)
_HB_RELEASE_PAT = re.compile(
    r'\b(?:([A-Za-z_][A-Za-z0-9_]*)\.release\(\)|'
    r'([A-Za-z_][A-Za-z0-9_]*)\.Unlock\(\)|'
    r'([A-Za-z_][A-Za-z0-9_]*)\.unlock\(\)|'
    r'pthread_mutex_unlock\s*\(\s*&?\s*([A-Za-z_][A-Za-z0-9_]*)\))'
)
_HB_BLOCK_ACQUIRE_RE = re.compile(r'\b(?:with\s+|synchronized\s*\()')
_ASSIGN_LHS_HB_BY_LANG = {
    'python': re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*[^=\n]+)?\s*=', re.MULTILINE),
    'go':     re.compile(r'^\s*(?:var\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?::?=\s*|=\s*)', re.MULTILINE),
    'rust':   re.compile(r'^\s*(?:let\s+(?:mut\s+)?)?([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*[^=\n]+)?\s*=', re.MULTILINE),
    'java':   re.compile(r'^\s*(?:final\s+)?(?:[\w<>\[\],\s]+)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=', re.MULTILINE),
    'c':      re.compile(r'^\s*(?:[\w\s\*]+?)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=', re.MULTILINE),
    'cpp':    re.compile(r'^\s*(?:[\w\s\*:<>,]+?)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=', re.MULTILINE),
}
_CF_KEYWORDS_BY_LANG = {
    'python': re.compile(r'^\s*(?:if|elif|else|for|while|try|except|finally|with|match|case)\b'),
    'go': re.compile(r'^\s*(?:if|else|for|switch|case|select|defer|go)\b'),
    'rust': re.compile(r'^\s*(?:if|else|for|while|loop|match|return|break|continue)\b'),
    'java': re.compile(r'^\s*(?:if|else|for|while|do|switch|case|try|catch|finally|return|break|continue)\b'),
    'c': re.compile(r'^\s*(?:if|else|for|while|do|switch|case|return|break|continue|goto)\b'),
    'cpp': re.compile(r'^\s*(?:if|else|for|while|do|switch|case|return|break|continue|goto|try|catch|throw)\b'),
}
_LITERAL_NUM_RE_1 = re.compile(r'^[-+]?0[xXbBoO][0-9a-fA-F_]+$')
_LITERAL_NUM_RE_2 = re.compile(r'^[-+]?\d[\d_]*\.?\d*([eE][+-]?\d+)?[fFlLuU]*$')
_CALLABLE_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*([.:][A-Za-z_][A-Za-z0-9_]*)*$')
_LOCAL_DECL_RE = re.compile(
    r'(?:var\s+|let\s+(?:mut\s+)?)?(\w+)(?:\s*[:]\s*\w+)?\s*[=:]\s*(.{0,400})',
    re.DOTALL,
)
_LOCAL_DECL_TYPE_RE = re.compile(r'(?:var\s+|let\s+(?:mut\s+)?)?\w+\s*[:]\s*(\w+)')
_INFER_COMMENT_SPLIT_RE = re.compile(r'\s+#')
_DOC_COMMENT_BLOCK_RE = re.compile(r'/\*\*?(.*?)\*/', re.DOTALL)
_DOC_COMMENT_LINE_RE = re.compile(r'^\s*\*\s?')
_NORMALIZE_NAME_RE = re.compile(r'[^A-Za-z0-9_]')

# Pre-compiled patterns for global definition extraction (used by _extract_globals)
_GLOBAL_DEFINE_RE = re.compile(r'#define\s+(\w+)\s+(.*)')
_GLOBAL_TYPEDEF_RE = re.compile(r'typedef\s+.*?(\w+)\s*;')
_GLOBAL_VAR_RE = re.compile(
    r'(?:extern\s+)?(?:static\s+)?(?:const\s+)?(\w[\w\s*]+?)\s+(\w+)\s*(?:=\s*(.{0,80}))?;'
)

# Pre-compiled patterns for condition variable token cleaning (used by _extract_condition_vars)
_COND_TOKEN_SUBST_RE_1 = re.compile(r'[!=<>]=*')
_COND_TOKEN_SUBST_RE_2 = re.compile(r'[(){}\[\]|&^~+\-*/%,]')
_COND_TOKEN_SUBST_RE_3 = re.compile(
    r'\b(?:\d+|true|false|null|nil|None|NULL|void)\b', re.IGNORECASE
)
_COND_TOKEN_IDENT_RE = re.compile(r'^[A-Za-z_]\w*$')


class BaseScanner(ABC):
    """Abstract base for language-specific code graph scanners."""

    # Shared callback suffixes used across all language scanners.
    # Subclasses should reference this via self._CALLBACK_SUFFIXES
    # and may add language-specific suffixes by overriding.
    _CALLBACK_SUFFIXES = ('_cb', '_callback', '_handler', '_fn',
                          '_done', '_completion', '_cpl', '_event')


_CONDITION_PATTERNS_BY_LANG = {
    'c': (re.compile(r'\bif\s*\(([^)]+)\)', re.DOTALL),
          re.compile(r'\bswitch\s*\(([^)]+)\)', re.DOTALL)),
    'cpp': (re.compile(r'\bif\s*\(([^)]+)\)', re.DOTALL),
            re.compile(r'\bswitch\s*\(([^)]+)\)', re.DOTALL)),
    'python': (re.compile(r'^\s*if\s+(.+?):\s*$', re.MULTILINE),
               re.compile(r'^\s*elif\s+(.+?):\s*$', re.MULTILINE)),
    'go': (re.compile(r'\bif\s+(.+?)\s*\{', re.DOTALL),
           re.compile(r'\bswitch\s+(.+?)\s*\{', re.DOTALL)),
    'rust': (re.compile(r'\bif\s+(.+?)\s*\{', re.DOTALL),
             re.compile(r'\bmatch\s+(.+?)\s*\{', re.DOTALL)),
    'java': (re.compile(r'\bif\s*\(([^)]+)\)', re.DOTALL),
             re.compile(r'\bswitch\s*\(([^)]+)\)', re.DOTALL)),
}

_Z3_OP_MAP = {
    '&&': 'and', '||': 'or', '!': 'not',
    '==': '=', '!=': 'distinct',
    '<': '<', '>': '>', '<=': '<=', '>=': '>=',
}

_CONDITION_OP_SCAN = (
    ('&&', 'logical'), ('||', 'logical'),
    ('==', 'comparison'), ('!=', 'comparison'),
    ('<=', 'comparison'), ('>=', 'comparison'),
    ('<', 'comparison'), ('>', 'comparison'),
    ('!', 'unary'),
)


def _split_operand(expr: str, op: str) -> tuple:
    """Split expr on the first occurrence of op (not inside parens)."""
    depth = 0
    i = 0
    while i < len(expr) - len(op) + 1:
        c = expr[i]
        if c == '(':
            depth += 1
        elif c == ')':
            depth = max(0, depth - 1)  # clamp to prevent negative on unbalanced
        elif depth == 0 and expr[i:i + len(op)] == op:
            return expr[:i].strip(), expr[i + len(op):].strip()
        i += 1
    return None, None


def _make_z3_form(cond_text: str, kind: str, operator: str) -> str:
    """Generate a best-effort SMT-LIB prefix form."""
    t = cond_text.strip()
    if t.startswith('(') and t.endswith(')'):
        t = t[1:-1].strip()
    if kind == 'logical' and operator:
        left, right = _split_operand(t, operator)
        if left is not None and right is not None:
            z3_op = _Z3_OP_MAP.get(operator, operator)
            return f'({z3_op} {_make_z3_form(left, "atom", "")} {_make_z3_form(right, "atom", "")})'
    elif kind == 'comparison' and operator:
        left, right = _split_operand(t, operator)
        if left is not None and right is not None:
            z3_op = _Z3_OP_MAP.get(operator, operator)
            return f'({z3_op} {left} {right})'
    elif kind == 'unary' and operator == '!':
        inner = t.lstrip('!').strip()
        if inner:
            return f'(not {_make_z3_form(inner, "atom", "")})'
    t = ' '.join(t.split())
    if not t:
        return 'true'
    return t


class BaseScanner(ABC):
    """Abstract base for language-specific code graph scanners."""

    # Shared callback suffixes used across all language scanners.
    # Subclasses should reference this via self._CALLBACK_SUFFIXES
    # and may add language-specific suffixes by overriding.
    _CALLBACK_SUFFIXES = ('_cb', '_callback', '_handler', '_fn',
                          '_done', '_completion', '_cpl', '_event')

    def _is_callback_by_name(self, func_name: str) -> bool:
        """Check if a function name matches callback naming conventions."""
        if any(func_name.endswith(s) or pat.match(func_name)
               for s, pat in zip(self._CALLBACK_SUFFIXES, _CB_DIGIT_SUFFIX_RES)):
            return True
        if func_name.startswith('on_') or '_on_' in func_name:
            return True
        return False

    def _confidence_tag(self) -> str:
        """Confidence classification for scanner-extracted data.

        Returns 'EXTRACTED' — edges were directly observed in the AST.
        LLM-enhanced edges should use 'INFERRED'; uncertain edges 'AMBIGUOUS'.
        """
        return "EXTRACTED"

    def _source_tag(self) -> str:
        """Source origin for scanner-extracted data. Returns 'ast'."""
        return "ast"

    @staticmethod
    def _node_position(node) -> dict:
        """Return precise position info for a tree-sitter node.

        Provides line (1-based), column (1-based), start_byte, end_byte.
        Used by lock-coverage, field-access, and statement-level analysis
        to locate facts beyond line granularity.
        """
        if node is None:
            return {"line": 0, "column": 0, "start_byte": 0, "end_byte": 0}
        return {
            "line": node.start_point[0] + 1,
            "column": node.start_point[1] + 1,
            "start_byte": node.start_byte,
            "end_byte": node.end_byte,
        }

    def scan_file(self, filepath: str, source_root: str,
                  macro_bindings: dict = None) -> dict:
        try:
            with open(filepath, 'rb') as f:
                source_bytes = f.read()
        except (IOError, OSError) as e:
            return {"file": filepath, "domain": "", "functions": [], "edges": [], "globals": {}, "error": f"IOError: {e}", "error_kind": "io"}

        domain = classify_domain(filepath, source_root)
        try:
            tree = self._parse(source_bytes)
        except Exception as e:
            return {"file": filepath, "domain": domain, "functions": [], "edges": [], "globals": {}, "error": f"ParseError: {e}", "error_kind": "parse"}
        if tree is None:
            return {"file": filepath, "domain": domain, "functions": [], "edges": [], "globals": {}, "error": "ParseError: tree is None", "error_kind": "parse"}

        self._macro_bindings = macro_bindings or {}
        try:
            extract_result = self._extract(tree, source_bytes, filepath, source_root, domain)
        except Exception as e:
            return {"file": filepath, "domain": domain, "functions": [], "edges": [], "globals": {}, "error": f"ExtractError: {e}", "error_kind": "extract"}
        # Support 2-tuple (functions, edges), 3-tuple (functions, edges, extra),
        # 4-tuple (functions, edges, vtable_registrations, fn_ptr_calls),
        # and 5-tuple (functions, edges, vtable_registrations, fn_ptr_calls, macro_registrations).
        fn_ptr_calls = {}
        macro_registrations = []
        if len(extract_result) == 5:
            functions, edges, extra, fn_ptr_calls_dict, macro_regs_list = extract_result
            fn_ptr_calls = fn_ptr_calls_dict if isinstance(fn_ptr_calls_dict, dict) else {}
            macro_registrations = macro_regs_list if isinstance(macro_regs_list, list) else []
            if extra and isinstance(extra, list) and extra and extra[0].get("relation") == "IMPORTS":
                import_edges = extra
                vtable_registrations = []
            else:
                vtable_registrations = extra
                import_edges = []
        elif len(extract_result) == 4:
            functions, edges, extra, fn_ptr_calls_dict = extract_result
            # fn_ptr_calls_dict is a dict keyed by invoker_id with lists of fn_ptr_call entries
            fn_ptr_calls = fn_ptr_calls_dict if isinstance(fn_ptr_calls_dict, dict) else {}
            if extra and isinstance(extra, list) and extra and extra[0].get("relation") == "IMPORTS":
                import_edges = extra
                vtable_registrations = []
            else:
                vtable_registrations = extra
                import_edges = []
        elif len(extract_result) == 3:
            functions, edges, extra = extract_result
            if extra and isinstance(extra, list) and extra[0].get("relation") == "IMPORTS":
                import_edges = extra
                vtable_registrations = []
            else:
                vtable_registrations = extra
                import_edges = []
        else:
            functions, edges = extract_result
            vtable_registrations = []
            import_edges = []
        try:
            globals_data = self._extract_globals(tree, source_bytes, filepath, source_root)
        except Exception as e:
            globals_data = {"enums": [], "constants": [], "typedefs": [], "global_vars": []}
            # Non-fatal: globals extraction failure shouldn't drop the whole file,
            # but record a warning so the aggregator can surface it.
            return {"file": filepath, "domain": domain, "functions": functions, "edges": edges, "globals": globals_data, "vtable_registrations": vtable_registrations, "import_edges": import_edges, "fn_ptr_calls": fn_ptr_calls, "macro_registrations": macro_registrations, "warning": f"GlobalsExtractError: {e}"}

        # Cross-language config predicates (//go:build, #[cfg],
        # sys.platform, @Profile, #ifdef). Annotates each function with
        # config_predicate_id and emits a cgdb_predicates list on the result.
        cgdb_predicates = []
        try:
            cgdb_predicates = self._annotate_config_predicates(
                filepath, source_bytes, functions
            )
        except Exception:
            # Best-effort: predicate extraction should never block a scan.
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass

        # For non-C/C++ files, synthesize cgdb_* records from legacy
        # functions/edges so the cgdb tables get populated with stable
        # cross-language node IDs. C/C++ files have their own clang-based
        # cgdb extraction in ClangScanner.scan_file.
        cgdb_nodes = []
        cgdb_edges_list = []
        cgdb_invoke_sites = []
        cgdb_doc_comments = []
        cgdb_data_flow = []
        cgdb_sync_primitives = []
        cgdb_happens_before = []
        cgdb_alias_sets = []
        cgdb_basic_blocks = []
        cgdb_cfg_edges = []
        cgdb_includes = []
        cgdb_conditions = []
        cgdb_types = []
        cgdb_ops_bindings = []
        ext = os.path.splitext(filepath)[1].lower()
        if ext in ('.go', '.rs', '.py', '.java', '.s', '.S', '.asm',
                   '.c', '.cc', '.cpp', '.cxx', '.h', '.hpp', '.hxx'):
            try:
                cgdb_extra = self._emit_cgdb_records(
                    filepath, source_bytes, functions, edges, globals_data,
                    vtable_registrations, fn_ptr_calls,
                    source_root=source_root,
                )
                if cgdb_extra:
                    cgdb_nodes = cgdb_extra.get('cgdb_nodes', [])
                    cgdb_edges_list = cgdb_extra.get('cgdb_edges', [])
                    cgdb_invoke_sites = cgdb_extra.get('cgdb_invoke_sites', [])
                    cgdb_doc_comments = cgdb_extra.get('cgdb_doc_comments', [])
                    cgdb_data_flow = cgdb_extra.get('cgdb_data_flow', [])
                    cgdb_sync_primitives = cgdb_extra.get('cgdb_sync_primitives', [])
                    cgdb_happens_before = cgdb_extra.get('cgdb_happens_before', [])
                    cgdb_alias_sets = cgdb_extra.get('cgdb_alias_sets', [])
                    cgdb_basic_blocks = cgdb_extra.get('cgdb_basic_blocks', [])
                    cgdb_cfg_edges = cgdb_extra.get('cgdb_cfg_edges', [])
                    cgdb_includes = cgdb_extra.get('cgdb_includes', [])
                    cgdb_conditions = cgdb_extra.get('cgdb_conditions', [])
                    cgdb_types = cgdb_extra.get('cgdb_types', [])
                    cgdb_ops_bindings = cgdb_extra.get('cgdb_ops_bindings', [])
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        return {"file": filepath, "domain": domain, "functions": functions,
                "edges": edges, "globals": globals_data,
                "vtable_registrations": vtable_registrations,
                "import_edges": import_edges, "fn_ptr_calls": fn_ptr_calls,
                "macro_registrations": macro_registrations,
                "cgdb_predicates": cgdb_predicates,
                "cgdb_nodes": cgdb_nodes,
                "cgdb_edges": cgdb_edges_list,
                "cgdb_invoke_sites": cgdb_invoke_sites,
                "cgdb_doc_comments": cgdb_doc_comments,
                "cgdb_data_flow": cgdb_data_flow,
                "cgdb_sync_primitives": cgdb_sync_primitives,
                "cgdb_happens_before": cgdb_happens_before,
                "cgdb_alias_sets": cgdb_alias_sets,
                "cgdb_basic_blocks": cgdb_basic_blocks,
                "cgdb_cfg_edges": cgdb_cfg_edges,
                "cgdb_includes": cgdb_includes,
                "cgdb_conditions": cgdb_conditions,
                "cgdb_types": cgdb_types,
                "cgdb_ops_bindings": cgdb_ops_bindings,
                }

    @staticmethod
    def _mark_all_unconditional(functions: list) -> None:
        """Mark every function with the UNCONDITIONAL predicate id when no
        config predicates were extracted from the file (e.g. ImportError,
        decode failure, no ranges found, no node byte offsets). Ensures
        config_predicate_id is always populated on cgdb_nodes."""
        try:
            from _builder.cgdb_config_predicates import UNCONDITIONAL
        except ImportError:
            return
        for fn in functions:
            if 'config_predicate_id' not in fn:
                fn['config_predicate_id'] = UNCONDITIONAL.id

    def _annotate_config_predicates(self, filepath: str, source_bytes: bytes,
                                     functions: list) -> list:
        """Detect language-specific config predicates from source text and
        annotate each function with config_predicate_id. Returns the list of
        serialized predicates (JSON-safe dicts) for downstream ingestion.

        Per file extension:
          - Go (.go): //go:build tags
          - Rust (.rs): #[cfg(...)] / #[feature = "..."]
          - Python (.py): sys.platform / os.name comparisons
          - Java (.java): @Conditional / @Profile
          - ASM (.s/.S/.asm): #ifdef / #ifndef / #if
          - C/C++ (.c/.cc/.cpp/.h): tree-sitter fallback (clang_scanner handles
            this natively when extraction-backend is clang); reuses ASM tracker.

        Functions are annotated by byte-range containment: a function whose
        start_byte falls inside a predicate's range gets that predicate_id.
        """
        try:
            from _scanner.config_predicates_lang import (
                extract_go_build_predicates,
                extract_rust_cfg_predicates,
                extract_python_platform_predicates,
                extract_java_profile_predicates,
                extract_asm_ifdef_predicates,
                annotate_nodes_with_predicates,
                serialize_predicates,
            )
            from _builder.cgdb_config_predicates import UNCONDITIONAL
        except ImportError:
            self._mark_all_unconditional(functions)
            return []

        # Decode source for regex matching
        try:
            source_text = source_bytes.decode('utf-8', errors='replace')
        except Exception:
            self._mark_all_unconditional(functions)
            return []

        ext = os.path.splitext(filepath)[1].lower()
        ranges = []
        if ext == '.go':
            ranges = extract_go_build_predicates(source_text)
        elif ext == '.rs':
            ranges = extract_rust_cfg_predicates(source_text)
        elif ext == '.py':
            ranges = extract_python_platform_predicates(source_text)
        elif ext == '.java':
            ranges = extract_java_profile_predicates(source_text)
        elif ext in ('.s', '.S', '.asm'):
            ranges = extract_asm_ifdef_predicates(source_text)
        elif ext in ('.c', '.cc', '.cpp', '.cxx', '.h', '.hpp', '.hxx'):
            # Tree-sitter C/C++ fallback (clang_scanner handles this natively
            # when extraction-backend is clang). Reuse ASM #ifdef tracker.
            ranges = extract_asm_ifdef_predicates(source_text)
        else:
            self._mark_all_unconditional(functions)
            return []

        if not ranges:
            self._mark_all_unconditional(functions)
            return []

        # Build a node-like list of dicts with byte_start/byte_end for annotation
        node_dicts = []
        for fn in functions:
            bs = fn.get('start_byte') or fn.get('byte_start') or 0
            be = fn.get('end_byte') or fn.get('byte_end') or bs
            if not bs and not be:
                # Fall back to line-based approximation: skip — annotation
                # needs byte offsets. Functions without byte offsets are
                # left with UNCONDITIONAL predicate (default).
                continue
            node_dicts.append({
                'id': fn.get('id') or fn.get('name'),
                'byte_start': bs,
                'byte_end': be,
                '_fn_ref': fn,
            })

        if not node_dicts:
            self._mark_all_unconditional(functions)
            return serialize_predicates([p for _, _, p in ranges])

        _, unique_preds = annotate_nodes_with_predicates(node_dicts, ranges)
        # Copy config_predicate_id back to the function dicts
        for nd in node_dicts:
            fn = nd.get('_fn_ref')
            if fn is not None:
                fn['config_predicate_id'] = nd['config_predicate_id']
        # Also mark functions without byte offsets as UNCONDITIONAL
        for fn in functions:
            if 'config_predicate_id' not in fn:
                fn['config_predicate_id'] = UNCONDITIONAL.id
        return serialize_predicates(unique_preds)

    def _emit_cgdb_records(self, filepath: str, source_bytes: bytes,
                            functions: list, edges: list,
                            globals_data: dict,
                            vtable_registrations: list = None,
                            fn_ptr_calls: dict = None,
                            source_root: str = "") -> dict:
        """Synthesize cgdb_nodes/cgdb_edges/cgdb_invoke_sites/cgdb_doc_comments
        for non-C/C++ scanners (Go, Rust, Python, Java, ASM).

        Uses unified_node_id (language-aware SHA-256) for cross-language ID
        stability. Returns a dict with cgdb_* keys suitable for merging
        into the scan_file result.

        Called automatically by scan_file() for non-C/C++ files when the
        scanner itself doesn't emit cgdb_* keys.
        """
        try:
            from _scanner.unified_id import (
                unified_node_id, unified_edge_id, unified_file_id,
            )
        except ImportError:
            return {}
        ext = os.path.splitext(filepath)[1].lower()
        if ext in ('.go',):
            language = 'go'
        elif ext in ('.rs',):
            language = 'rust'
        elif ext in ('.py',):
            language = 'python'
        elif ext in ('.java',):
            language = 'java'
        elif ext in ('.s', '.S', '.asm'):
            language = 'asm'
        elif ext in ('.c', '.h'):
            language = 'c'
        elif ext in ('.cc', '.cpp', '.cxx', '.hpp', '.hxx'):
            language = 'cpp'
        else:
            return {}

        cgdb_nodes = []
        cgdb_edges = []
        cgdb_invoke_sites = []
        cgdb_doc_comments = []
        cgdb_data_flow = []
        cgdb_sync_primitives = []
        cgdb_alias_sets = []
        cgdb_basic_blocks = []
        cgdb_cfg_edges = []
        cgdb_types = []
        cgdb_ops_bindings = []
        seen_node_ids = set()
        fid = unified_file_id(filepath)
        # Use relative path for file_path to match functions.source_file.
        # Reassign filepath so all downstream 'file_path': filepath assignments
        # and child method calls (_emit_sync_primitives, _emit_conditions)
        # propagate the relative path consistently.
        filepath = os.path.relpath(filepath, source_root) if source_root else filepath
        rel_filepath = filepath

        # Pre-compute line-start byte offsets so we can derive byte ranges
        # from line numbers for edges and invoke_sites.
        line_offsets = [0]
        for m in re.finditer(rb'\n', source_bytes):
            line_offsets.append(m.end())
        line_offsets.append(len(source_bytes))

        def _byte_range_for_line(line: int) -> tuple:
            """Return (byte_start, byte_end) for a 1-based line number."""
            if not line or line < 1 or line >= len(line_offsets):
                return (0, 0)
            return (line_offsets[line - 1], line_offsets[line])

        # Map from legacy function id → unified node id, used for data_flow
        # function_id linking and sync_primitives.
        fn_legacy_to_nid: dict = {}

        # Type registry: canonical_spelling → type_id. Used to dedup types
        # across functions/files and to set cgdb_nodes.type_id back-references.
        type_registry: dict = {}

        def _register_type(spelling: str) -> int:
            """Register a type by spelling, returning a stable integer id.

            Categorizes into builtin/pointer/record/enum/function/typedef/array
            based on lexical cues. Deduplicates by canonical spelling.
            """
            if not spelling:
                return 0
            canon = " ".join(str(spelling).split())
            if not canon:
                return 0
            if canon in type_registry:
                return type_registry[canon]
            type_id = int(
                hashlib.sha256(f'type:{canon}'.encode('utf-8')).hexdigest()[:15],
                16,
            ) & 0x0FFF_FFFF_FFFF_FFFF
            type_registry[canon] = type_id
            # Categorize
            is_const = 'const ' in canon + ' '
            is_volatile = 'volatile ' in canon + ' '
            stripped = _C_STORAGE_kw_re.sub('', canon)
            stripped = " ".join(stripped.split())
            kind = 'builtin'
            if stripped.endswith('*') or '*' in stripped:
                kind = 'pointer'
            elif stripped.startswith('enum '):
                kind = 'enum'
            elif stripped.startswith('struct ') or stripped.startswith('union '):
                kind = 'record'
            elif stripped.startswith('typedef '):
                kind = 'typedef'
            elif '[' in stripped and stripped.endswith(']'):
                kind = 'array'
            elif '(' in stripped and ')' in stripped:
                kind = 'function'
            else:
                base = stripped.split()[0] if stripped.split() else ''
                if base in _BUILTIN_C_TYPES or base.endswith('_t') and base in _BUILTIN_C_TYPES:
                    kind = 'builtin'
                elif base and base[0].isupper():
                    kind = 'record'
            cgdb_types.append({
                'id': type_id,
                'spelling': canon,
                'canonical_spelling': canon,
                'kind': kind,
                'size_bytes': None,
                'alignment': None,
                'is_const': 1 if is_const else 0,
                'is_volatile': 1 if is_volatile else 0,
                'pointee_type_id': None,
                'element_type_id': None,
                'record_id': None,
                'attrs': {},
            })
            return type_id

        _SNIPPET_MAX = 4096  # cap snippet size to keep DB lean

        def _snippet(byte_start: int, byte_end: int) -> str:
            """Extract a source text snippet from source_bytes, capped at
            _SNIPPET_MAX chars. Returns '' if byte range is invalid."""
            if not byte_start or not byte_end or byte_end <= byte_start:
                return ''
            if byte_start < 0 or byte_end > len(source_bytes):
                return ''
            text = source_bytes[byte_start:byte_end].decode(
                'utf-8', errors='replace'
            )
            if len(text) > _SNIPPET_MAX:
                text = text[:_SNIPPET_MAX] + '…'
            return text

        # Emit function nodes
        for fn in functions:
            fn_id_str = fn.get('id', '') or fn.get('name', '')
            if not fn_id_str:
                continue
            signature = fn.get('signature', '') or ''
            nid = unified_node_id(language, fn_id_str)
            fn_legacy_to_nid[fn_id_str] = nid
            if nid in seen_node_ids:
                continue
            seen_node_ids.add(nid)
            attrs = {}
            if signature:
                attrs['signature'] = signature
            body_text = fn.get('body_text', '') or ''
            if body_text:
                attrs['body_text'] = body_text
            labels = fn.get('labels', []) or []
            if labels:
                attrs['labels'] = labels
            params = fn.get('params', []) or []
            if params:
                attrs['params'] = params
            # Extract return type from signature. For C/C++ the return type
            # is before the function name (int foo() → 'int'). For Python the
            # return type is after '->' (def foo() -> int: → 'int'). For Go
            # the return type is after the param list (func foo() int → 'int').
            # For Java/Rust the return type is before the name like C/C++.
            fn_name = fn.get('name', '') or ''
            return_type_spelling = ''
            if signature and fn_name:
                if signature.startswith('def '):
                    # Python: def foo(args) -> ReturnType:
                    m = re.search(r'\)\s*->\s*([^:]+?)\s*:', signature)
                    if m:
                        return_type_spelling = m.group(1).strip()
                else:
                    # C/C++/Java/Rust: return_type foo(args)
                    idx = signature.rfind(fn_name)
                    if idx > 0:
                        return_type_spelling = signature[:idx].strip()
                        return_type_spelling = _RETURN_TYPE_kw_re.sub(
                            '', return_type_spelling
                        ).strip()
                        return_type_spelling = " ".join(return_type_spelling.split())
            fn_type_id = _register_type(return_type_spelling) if return_type_spelling else None
            # Also register parameter types from params list
            if params:
                for p in params:
                    ptype = ''
                    if isinstance(p, dict):
                        ptype = p.get('type', '') or ''
                    elif isinstance(p, (list, tuple)) and len(p) >= 1:
                        ptype = str(p[0]) if p[0] else ''
                    if ptype:
                        _register_type(ptype)
            cgdb_nodes.append({
                'id': nid,
                'kind': 'function',
                'name': fn.get('name', '') or '',
                'fqn': fn_id_str,
                'file_id': fid,
                'file_path': filepath,
                'line': int(fn.get('line_number', 0) or fn.get('line', 0) or 0),
                'col': 0,
                'byte_start': int(fn.get('start_byte', 0) or fn.get('byte_start', 0) or 0),
                'byte_end': int(fn.get('end_byte', 0) or fn.get('byte_end', 0) or 0),
                'type_spelling': return_type_spelling or signature,
                'type_id': fn_type_id,
                'attrs': attrs,
                'source_layer': 'legacy',
                'confidence': 0.9,
                'enclosing_symbol_id': None,
                'legacy_function_id': fn_id_str,
                'config_predicate_id': fn.get('config_predicate_id'),
                'source_snippet': _snippet(
                    int(fn.get('start_byte', 0) or fn.get('byte_start', 0) or 0),
                    int(fn.get('end_byte', 0) or fn.get('byte_end', 0) or 0),
                ),
            })
            # Emit doc comment if present
            doc = fn.get('doc_comment', '') or ''
            if doc:
                cgdb_doc_comments.append({
                    'node_id': nid,
                    'file_id': fid,
                    'file_path': filepath,
                    'line': int(fn.get('line_number', 0) or 0),
                    'col': 0,
                    'comment_kind': self._classify_doc_kind(doc),
                    'raw_text': doc,
                    'cleaned_text': doc.strip(),
                    'tags': {},
                    'byte_start': 0,
                    'byte_end': 0,
                })

        # Emit local-var nodes per function, plus data_flow def records for
        # each variable initialization. Local vars are scoped to their
        # enclosing function, so we use a function-qualified FQN.
        for fn in functions:
            fn_id_str = fn.get('id', '') or ''
            fn_nid = fn_legacy_to_nid.get(fn_id_str)
            if fn_nid is None:
                continue
            local_vars = fn.get('local_vars', []) or []
            for lv in local_vars:
                lv_name = lv.get('name', '') or ''
                if not lv_name:
                    continue
                lv_fqn = f'{fn_id_str}::{lv_name}'
                lv_nid = unified_node_id(language, lv_fqn)
                lv_type = lv.get('type', '') or ''
                lv_type_id = _register_type(lv_type) if lv_type else None
                if lv_nid not in seen_node_ids:
                    seen_node_ids.add(lv_nid)
                    _lv_bs = int(lv.get('start_byte', 0) or 0)
                    _lv_be = int(lv.get('end_byte', 0) or 0)
                    cgdb_nodes.append({
                        'id': lv_nid,
                        'kind': 'var',
                        'name': lv_name,
                        'fqn': lv_fqn,
                        'file_id': fid,
                        'file_path': filepath,
                        'line': int(lv.get('line', 0) or 0),
                        'col': int(lv.get('column', 0) or 0),
                        'byte_start': _lv_bs,
                        'byte_end': _lv_be,
                        'type_spelling': lv_type,
                        'type_id': lv_type_id,
                        'attrs': {
                            'is_param': bool(lv.get('is_param', False)),
                            'value_snippet': lv.get('value_snippet', '') or '',
                        },
                        'source_layer': 'legacy',
                        'confidence': 0.9,
                        'enclosing_symbol_id': fn_nid,
                        'source_snippet': _snippet(_lv_bs, _lv_be),
                    })
                # Emit a 'def' record for the variable's initialization.
                # def_stmt_id and use_stmt_id both reference the var node
                # (we don't have separate stmt nodes without a CFG).
                is_param = bool(lv.get('is_param', False))
                cgdb_data_flow.append({
                    'var_id': lv_nid,
                    'def_stmt_id': lv_nid,
                    'use_stmt_id': lv_nid,
                    'function_id': fn_nid,
                    'kind': 'def' if not is_param else 'def',
                })
                # If the local var is a parameter, also emit a 'use' record
                # at function entry (parameters are live-on-entry).
                if is_param:
                    cgdb_data_flow.append({
                        'var_id': lv_nid,
                        'def_stmt_id': lv_nid,
                        'use_stmt_id': lv_nid,
                        'function_id': fn_nid,
                        'kind': 'use',
                    })

        # Heuristic alias-set extraction. For each function, build a
        # name → node_id map of local vars, then scan each var's
        # value_snippet: if it's a single identifier matching another
        # local var in the same function, emit a 'must_alias' record
        # (e.g., `y = x; z = y;` yields y↔x and z↔y).
        for fn in functions:
            fn_id_str = fn.get('id', '') or ''
            fn_nid = fn_legacy_to_nid.get(fn_id_str)
            if fn_nid is None:
                continue
            local_vars = fn.get('local_vars', []) or []
            if len(local_vars) < 2:
                continue
            lv_name_to_nid = {}
            for lv in local_vars:
                lv_name = lv.get('name', '') or ''
                if not lv_name:
                    continue
                lv_fqn = f'{fn_id_str}::{lv_name}'
                lv_name_to_nid[lv_name] = unified_node_id(language, lv_fqn)
            for lv in local_vars:
                lv_name = lv.get('name', '') or ''
                val_snip = (lv.get('value_snippet', '') or '').strip()
                if not lv_name or not val_snip:
                    continue
                # Only match a bare identifier (no operators, no calls).
                if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', val_snip):
                    continue
                if val_snip == lv_name:
                    continue
                target_nid = lv_name_to_nid.get(val_snip)
                if target_nid is None:
                    continue
                src_nid = lv_name_to_nid.get(lv_name)
                if src_nid is None:
                    continue
                cgdb_alias_sets.append({
                    'ptr1_node_id': src_nid,
                    'ptr2_node_id': target_nid,
                    'kind': 'must_alias',
                    'confidence': 0.7,
                })

        # Emit global var nodes
        if globals_data:
            for gv in globals_data.get('global_vars', []) or []:
                gv_name = gv.get('name', '') or ''
                if not gv_name:
                    continue
                fqn = f'{filepath}::{gv_name}'
                nid = unified_node_id(language, fqn)
                if nid in seen_node_ids:
                    continue
                seen_node_ids.add(nid)
                gv_type = gv.get('type', '') or ''
                gv_type_id = _register_type(gv_type) if gv_type else None
                _gv_bs = int(gv.get('start_byte', 0) or 0)
                _gv_be = int(gv.get('end_byte', 0) or 0)
                cgdb_nodes.append({
                    'id': nid,
                    'kind': 'var',
                    'name': gv_name,
                    'fqn': fqn,
                    'file_id': fid,
                    'file_path': filepath,
                    'line': int(gv.get('line', 0) or 0),
                    'col': 0,
                    'byte_start': _gv_bs,
                    'byte_end': _gv_be,
                    'type_spelling': gv_type,
                    'type_id': gv_type_id,
                    'source_snippet': _snippet(_gv_bs, _gv_be),
                    'attrs': {},
                    'source_layer': 'legacy',
                    'confidence': 0.9,
                })
            # Emit enum/type nodes
            for en in globals_data.get('enums', []) or []:
                en_name = en.get('name', '') or ''
                if not en_name:
                    continue
                fqn = f'{filepath}::{en_name}'
                nid = unified_node_id(language, fqn)
                if nid in seen_node_ids:
                    continue
                seen_node_ids.add(nid)
                # Register the enum type itself in cgdb_types
                enum_spelling = f'enum {en_name}' if en_name else 'enum'
                enum_type_id = _register_type(enum_spelling)
                _en_bs = int(en.get('start_byte', 0) or 0)
                _en_be = int(en.get('end_byte', 0) or 0)
                cgdb_nodes.append({
                    'id': nid,
                    'kind': 'enum',
                    'name': en_name,
                    'fqn': fqn,
                    'file_id': fid,
                    'file_path': filepath,
                    'line': int(en.get('line', 0) or 0),
                    'col': 0,
                    'byte_start': _en_bs,
                    'byte_end': _en_be,
                    'type_spelling': enum_spelling,
                    'type_id': enum_type_id,
                    'source_snippet': _snippet(_en_bs, _en_be),
                    'attrs': {'values': en.get('values', []) or []},
                    'source_layer': 'legacy',
                    'confidence': 0.9,
                })

        # Emit edges
        for e in edges:
            caller = e.get('invoker_id', '') or e.get('caller', '') or e.get('source', '')
            callee = e.get('invoked_id', '') or e.get('callee', '') or e.get('target', '')
            if not caller or not callee:
                continue
            caller_nid = unified_node_id(language, caller)
            callee_nid = unified_node_id(language, callee)
            # Ensure both endpoints exist as nodes. Edges often reference
            # external/builtin callees (set, list.append, module.func) that
            # have no function definition in this scan — synthesize a stub
            # node so the foreign-key relationship is satisfied.
            for nid, fqn, name in (
                (caller_nid, caller, caller.split('.')[-1] if '.' in caller else caller),
                (callee_nid, callee, callee.split('.')[-1] if '.' in callee else callee),
            ):
                if nid not in seen_node_ids:
                    seen_node_ids.add(nid)
                    cgdb_nodes.append({
                        'id': nid,
                        'kind': 'function',
                        'name': name,
                        'fqn': fqn,
                        'file_id': fid,
                        'file_path': filepath,
                        'line': 0,
                        'col': 0,
                        'byte_start': 0,
                        'byte_end': 0,
                        'type_spelling': '',
                        'attrs': {'external': True},
                        'source_layer': 'legacy',
                        'confidence': 0.5,
                        'enclosing_symbol_id': None,
                        'legacy_function_id': fqn,
                    })
            kind = 'INVOKES'
            rel = e.get('relation', '') or ''
            if rel == 'OPS_BIND':
                kind = 'OPS_BIND'
            elif rel == 'READS':
                kind = 'READS'
            elif rel == 'WRITES':
                kind = 'WRITES'
            elif rel == 'IMPLEMENTS':
                kind = 'IMPLEMENTS'
            elif rel == 'IMPORTS':
                kind = 'IMPORTS'
            elif rel == 'CONTAINS':
                kind = 'CONTAINS'
            line = int(e.get('line', 0) or 0) if e.get('line') else None
            edge_id = unified_edge_id(caller_nid, callee_nid, kind, line)
            bs, be = _byte_range_for_line(line or 0)
            cgdb_edges.append({
                'edge_id': edge_id,
                'src_id': caller_nid,
                'dst_id': callee_nid,
                'kind': kind,
                'file_id': fid,
                'file_path': filepath,
                'line': line,
                'byte_start': bs,
                'byte_end': be,
                'attrs': {},
                'source_layer': 'legacy',
                'confidence': 0.9,
            })
            # Emit a call_site entry for each edge
            cgdb_invoke_sites.append({
                'invoker_id': caller_nid,
                'invoked_id': callee_nid,
                'invoke_kind': 'direct',
                'line': line or 0,
                'edge_id': edge_id,
                'file_path': filepath,
                'byte_start': bs,
                'byte_end': be,
            })

        # Heuristic sync primitive detection across non-C/C++ languages.
        # Walks each function's body_text for known lock/spawn idioms and
        # emits sync_primitives records linked to the function node and the
        # variable node (when a `with lock:` / `lock.lock()` / `sync.Mutex`
        # pattern can be identified by name).
        cgdb_sync_primitives = self._emit_sync_primitives(
            language, functions, fn_legacy_to_nid, seen_node_ids,
            cgdb_nodes, fid, filepath, source_bytes,
        )

        # Derive intra-function happens-before edges from program order and
        # lock-protected critical sections.
        cgdb_happens_before = self._emit_happens_before(
            language, functions, fn_legacy_to_nid,
        )

        # Emit a simplified CFG (basic_blocks + cfg_edges) for each function.
        cgdb_basic_blocks, cgdb_cfg_edges = self._emit_cfg(
            language, functions, fn_legacy_to_nid,
        )

        cgdb_includes = self._emit_cgdb_includes(language, source_bytes, fid)

        cgdb_conditions = self._emit_conditions(
            language, functions, fn_legacy_to_nid, filepath,
        )

        # Populate ops_bindings and invoke_sites.dispatch_candidates from
        # vtable_registrations. Each registration produces:
        #   - a synthetic 'vtable::<var>' node (ops_table)
        #   - a 'field' node for the field slot
        #   - an OPS_BIND edge from ops_table → impl function
        #   - an ops_bindings row linking edge_id ↔ ops_table_id, field_node_id, impl_function_id
        #   - dispatch_candidates on the corresponding invoke_sites rows
        vtable_registrations = vtable_registrations or []
        # Map: (struct_type, field_name) → [impl_function_node_id, ...]
        ops_field_to_impls: dict = {}
        for vt in vtable_registrations:
            struct_type = vt.get('struct_type', '') or ''
            var_name = vt.get('var_name', '') or ''
            if not struct_type or not var_name:
                continue
            # Synthetic ops_table node for this vtable instance
            vt_fqn = f'vtable::{var_name}'
            vt_nid = unified_node_id(language, vt_fqn)
            if vt_nid not in seen_node_ids:
                seen_node_ids.add(vt_nid)
                cgdb_nodes.append({
                    'id': vt_nid,
                    'kind': 'ops_table',
                    'name': var_name,
                    'fqn': vt_fqn,
                    'file_id': fid,
                    'file_path': filepath,
                    'line': 0,
                    'col': 0,
                    'byte_start': 0,
                    'byte_end': 0,
                    'type_spelling': f'struct {struct_type}',
                    'attrs': {'struct_type': struct_type, 'condition': vt.get('condition', '') or ''},
                    'source_layer': 'legacy',
                    'confidence': 0.9,
                })
            for reg in vt.get('registrations', []) or []:
                field_name = reg.get('field', '') or ''
                func_name = reg.get('func_name', '') or ''
                if not field_name or not func_name:
                    continue
                # Field node — represents the slot in the ops table
                field_fqn = f'{vt_fqn}::{field_name}'
                field_nid = unified_node_id(language, field_fqn)
                if field_nid not in seen_node_ids:
                    seen_node_ids.add(field_nid)
                    _fld_bs = int(reg.get('start_byte', 0) or 0)
                    _fld_be = int(reg.get('end_byte', 0) or 0)
                    cgdb_nodes.append({
                        'id': field_nid,
                        'kind': 'field',
                        'name': field_name,
                        'fqn': field_fqn,
                        'file_id': fid,
                        'file_path': filepath,
                        'line': int(reg.get('line', 0) or 0),
                        'col': int(reg.get('column', 0) or 0),
                        'byte_start': _fld_bs,
                        'byte_end': _fld_be,
                        'type_spelling': 'function_pointer',
                        'attrs': {'ops_table': vt_fqn, 'field_name': field_name},
                        'source_layer': 'legacy',
                        'confidence': 0.9,
                        'enclosing_symbol_id': vt_nid,
                        'source_snippet': _snippet(_fld_bs, _fld_be),
                    })
                # Impl function node — look up by legacy ID, fall back to
                # synthesizing a stub so the FK relationship is satisfied.
                impl_nid = unified_node_id(language, func_name)
                if impl_nid not in seen_node_ids:
                    seen_node_ids.add(impl_nid)
                    cgdb_nodes.append({
                        'id': impl_nid,
                        'kind': 'function',
                        'name': func_name.split('.')[-1] if '.' in func_name else func_name,
                        'fqn': func_name,
                        'file_id': fid,
                        'file_path': filepath,
                        'line': 0,
                        'col': 0,
                        'byte_start': 0,
                        'byte_end': 0,
                        'type_spelling': '',
                        'attrs': {'external': True, 'ops_impl': True},
                        'source_layer': 'legacy',
                        'confidence': 0.5,
                        'enclosing_symbol_id': None,
                        'legacy_function_id': func_name,
                    })
                # OPS_BIND edge: ops_table → impl function
                ops_edge_id = unified_edge_id(vt_nid, impl_nid, 'OPS_BIND',
                                              int(reg.get('line', 0) or 0))
                cgdb_edges.append({
                    'edge_id': ops_edge_id,
                    'src_id': vt_nid,
                    'dst_id': impl_nid,
                    'kind': 'OPS_BIND',
                    'file_id': fid,
                    'file_path': filepath,
                    'line': int(reg.get('line', 0) or 0) if reg.get('line') else None,
                    'attrs': {'field_name': field_name, 'ops_table': vt_fqn},
                    'source_layer': 'legacy',
                    'confidence': 0.9,
                })
                # ops_bindings row
                cgdb_ops_bindings.append({
                    'edge_id': ops_edge_id,
                    'ops_table_id': vt_nid,
                    'field_node_id': field_nid,
                    'impl_function_id': impl_nid,
                    'signature_match': True,
                })
                # Track for dispatch_candidates on invoke_sites
                key = (struct_type, field_name)
                ops_field_to_impls.setdefault(key, []).append(impl_nid)

        # Populate invoke_sites.dispatch_candidates and arg_bindings.
        # Walk fn_ptr_calls (function-pointer call sites) and look up the
        # candidate implementations by (struct_chain, field_name).
        fn_ptr_calls = fn_ptr_calls or {}
        # Build a map from function name → unified node id for invoker lookup
        fn_name_to_nid = {}
        for fn in functions:
            fn_name = fn.get('name', '') or ''
            if fn_name:
                fn_name_to_nid[fn_name] = unified_node_id(language, fn.get('id', '') or fn_name)
        # Re-emit invoke_sites with dispatch_candidates / arg_bindings where
        # we can find them. We update existing invoke_sites in place by
        # (invoker_id, invoked_id, line) key.
        invoke_site_index: dict = {}
        for isite in cgdb_invoke_sites:
            key = (isite.get('invoker_id'), isite.get('invoked_id'), isite.get('line'))
            invoke_site_index[key] = isite
        for invoker_name, calls in fn_ptr_calls.items():
            invoker_nid = fn_name_to_nid.get(invoker_name)
            if invoker_nid is None:
                continue
            for call in calls or []:
                field_name = call.get('field_name', '') or ''
                struct_chain = call.get('struct_chain', '') or ''
                callee_name = call.get('callee_name', '') or ''
                if not field_name:
                    continue
                # Look up candidate impls by field_name. NOTE: the call
                # site's struct_chain (a variable path like 'dev->ops')
                # cannot be mapped reliably to the vtable's struct_type
                # (e.g. 'file_operations') without type information, so
                # all same-field impls are candidates — dispatch is
                # disambiguated downstream by domain. (The previous code
                # had a self-comparison here that never filtered, plus a
                # redundant fallback loop that repeated the same match.)
                candidates = []
                for (struct_type, f_name), impls in ops_field_to_impls.items():
                    if f_name == field_name:
                        candidates.extend(impls)
                # Update or insert invoke_site for this call
                invoked_nid = unified_node_id(language, callee_name) if callee_name else 0
                line = int(call.get('line', 0) or 0)
                key = (invoker_nid, invoked_nid, line)
                isite = invoke_site_index.get(key)
                if isite is None:
                    # Synthesize a new invoke_site for the function-pointer call
                    isite = {
                        'invoker_id': invoker_nid,
                        'invoked_id': invoked_nid,
                        'invoke_kind': 'function_pointer',
                        'line': line,
                        'edge_id': 0,
                        'file_path': filepath,
                        'dispatch_candidates': candidates,
                        'arg_bindings': [],
                    }
                    cgdb_invoke_sites.append(isite)
                    invoke_site_index[key] = isite
                else:
                    if candidates:
                        isite['dispatch_candidates'] = candidates
                    isite['invoke_kind'] = 'function_pointer'

        # Backfill config_predicate_id on any cgdb_node missing it. Function
        # nodes already get this via _annotate_config_predicates; non-function
        # nodes (var/enum/field/typedef) inherit by byte-range containment
        # against the same #ifdef range map, falling back to UNCONDITIONAL.
        try:
            from _builder.cgdb_config_predicates import UNCONDITIONAL
        except ImportError:
            UNCONDITIONAL = None
        if UNCONDITIONAL is not None:
            for nd in cgdb_nodes:
                if nd.get('config_predicate_id') is not None:
                    continue
                nd['config_predicate_id'] = UNCONDITIONAL.id

        return {
            'cgdb_nodes': cgdb_nodes,
            'cgdb_edges': cgdb_edges,
            'cgdb_invoke_sites': cgdb_invoke_sites,
            'cgdb_doc_comments': cgdb_doc_comments,
            'cgdb_data_flow': cgdb_data_flow,
            'cgdb_sync_primitives': cgdb_sync_primitives,
            'cgdb_happens_before': cgdb_happens_before,
            'cgdb_alias_sets': cgdb_alias_sets,
            'cgdb_basic_blocks': cgdb_basic_blocks,
            'cgdb_cfg_edges': cgdb_cfg_edges,
            'cgdb_types': cgdb_types,
            'cgdb_ops_bindings': cgdb_ops_bindings,
            'cgdb_includes': cgdb_includes,
            'cgdb_conditions': cgdb_conditions,
        }

    def _emit_cgdb_includes(self, language: str, source_bytes: bytes,
                             fid: int) -> list:
        """Extract #include / import / use declarations from source text.

        Returns a list of dicts with keys:
          source_file_id, included_path, is_system, included_file_id (None)

        Pattern set is language-aware:
          - C/C++: #include <foo.h> (system), #include "foo.h" (user)
          - Python: import foo, from foo import bar, import foo.bar
          - Go: import "foo", import ( "foo" ... )
          - Rust: use foo; use foo::bar; extern crate foo;
          - Java: import foo.bar.Baz; import static foo.bar.Baz;
        """
        out = []
        try:
            text = source_bytes.decode('utf-8', errors='replace')
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            return out
        if language == 'python':
            for m in re.finditer(r'^\s*import\s+([A-Za-z_][A-Za-z0-9_\.]*)', text, re.MULTILINE):
                out.append({
                    'source_file_id': fid,
                    'included_path': m.group(1),
                    'is_system': False,
                    'included_file_id': None,
                })
            for m in re.finditer(r'^\s*from\s+([A-Za-z_][A-Za-z0-9_\.]*)\s+import', text, re.MULTILINE):
                out.append({
                    'source_file_id': fid,
                    'included_path': m.group(1),
                    'is_system': False,
                    'included_file_id': None,
                })
        elif language == 'go':
            # Single-line: import "foo"
            for m in re.finditer(r'^\s*import\s+"([^"]+)"', text, re.MULTILINE):
                out.append({
                    'source_file_id': fid,
                    'included_path': m.group(1),
                    'is_system': False,
                    'included_file_id': None,
                })
            # Multi-line: import ( "foo" ... )
            for m in re.finditer(r'import\s*\(([^)]+)\)', text, re.DOTALL):
                for line in m.group(1).split('\n'):
                    lm = re.search(r'"([^"]+)"', line)
                    if lm:
                        out.append({
                            'source_file_id': fid,
                            'included_path': lm.group(1),
                            'is_system': False,
                            'included_file_id': None,
                        })
        elif language == 'rust':
            for m in re.finditer(r'^\s*use\s+([A-Za-z_][A-Za-z0-9_:]*)', text, re.MULTILINE):
                out.append({
                    'source_file_id': fid,
                    'included_path': m.group(1),
                    'is_system': False,
                    'included_file_id': None,
                })
            for m in re.finditer(r'^\s*extern\s+crate\s+([A-Za-z_][A-Za-z0-9_]*)', text, re.MULTILINE):
                out.append({
                    'source_file_id': fid,
                    'included_path': m.group(1),
                    'is_system': False,
                    'included_file_id': None,
                })
        elif language == 'java':
            for m in re.finditer(r'^\s*import\s+(?:static\s+)?([A-Za-z_][A-Za-z0-9_\.]*)', text, re.MULTILINE):
                out.append({
                    'source_file_id': fid,
                    'included_path': m.group(1),
                    'is_system': False,
                    'included_file_id': None,
                })
        elif language == 'asm':
            for m in re.finditer(r'^\s*#include\s+["<]([^">]+)[">]', text, re.MULTILINE):
                is_sys = m.group(0).rstrip().endswith('>')
                out.append({
                    'source_file_id': fid,
                    'included_path': m.group(1),
                    'is_system': is_sys,
                    'included_file_id': None,
                })
        elif language in ('c', 'cpp'):
            for m in re.finditer(r'^\s*#\s*include\s+["<]([^">]+)[">]', text, re.MULTILINE):
                is_sys = '>' in m.group(0).rstrip() and '"' not in m.group(0).split('<')[0]
                out.append({
                    'source_file_id': fid,
                    'included_path': m.group(1),
                    'is_system': is_sys,
                    'included_file_id': None,
                })
        return out

    def _emit_conditions(self, language: str, functions: list,
                          fn_legacy_to_nid: dict, filepath: str = "") -> list:
        """Extract top-level if/switch conditions from each function body.

        Returns a list of dicts with keys:
          id, root_expr_id, kind, operator, left_expr_id, right_expr_id,
          text_form, z3_form, attrs, file_path
        """
        try:
            from _scanner.unified_id import unified_node_id
        except ImportError:
            return []
        out = []
        cond_id_base = 1
        patterns = _CONDITION_PATTERNS_BY_LANG.get(language)
        if not patterns:
            return []
        if_pat, switch_pat = patterns

        for fn in functions:
            fn_id_str = fn.get('id', '') or ''
            if not fn_id_str:
                continue
            body = fn.get('body_text', '') or ''
            if not body:
                continue
            try:
                first_brace = body.find('{')
                if first_brace >= 0:
                    body = body[first_brace:]
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
            seen_texts = set()
            for pat, kind_default in ((if_pat, 'atom'), (switch_pat, 'atom')):
                for m in pat.finditer(body):
                    cond_text = m.group(1).strip()
                    if not cond_text or len(cond_text) > 200:
                        continue
                    cond_text = ' '.join(cond_text.split())
                    if cond_text in seen_texts:
                        continue
                    seen_texts.add(cond_text)
                    kind = 'atom'
                    operator = ''
                    for op, k in _CONDITION_OP_SCAN:
                        if op in cond_text:
                            kind = k
                            operator = op
                            break
                    z3_form = _make_z3_form(cond_text, kind, operator)
                    root_expr_id = None
                    left_expr_id = None
                    right_expr_id = None
                    if kind in ('comparison', 'logical') and operator:
                        left, right = _split_operand(cond_text, operator)
                        if left is not None:
                            left_expr_id = unified_node_id(
                                language, f'{fn_id_str}::expr::{left}')
                        if right is not None:
                            right_expr_id = unified_node_id(
                                language, f'{fn_id_str}::expr::{right}')
                    elif kind == 'unary' and operator == '!':
                        inner = cond_text.lstrip('!').strip()
                        if inner:
                            root_expr_id = unified_node_id(
                                language, f'{fn_id_str}::expr::{inner}')
                    cond_id = unified_node_id(
                        language, f'{fn_id_str}::cond::{cond_id_base}')
                    cond_id_base += 1
                    out.append({
                        'id': cond_id,
                        'root_expr_id': root_expr_id,
                        'kind': kind,
                        'operator': operator,
                        'left_expr_id': left_expr_id,
                        'right_expr_id': right_expr_id,
                        'text_form': cond_text,
                        'z3_form': z3_form,
                        'attrs': {
                            'function_id': fn_legacy_to_nid.get(fn_id_str),
                            'source': 'tree-sitter-regex',
                        },
                        'file_path': filepath,
                    })
        return out

    def _emit_sync_primitives(self, language: str, functions: list,
                               fn_legacy_to_nid: dict, seen_node_ids: set,
                               cgdb_nodes: list, fid: int, filepath: str,
                               source_bytes: bytes = b'') -> list:
        """Heuristic sync primitive detection for non-C/C++ languages.

        Scans each function's body_text for lock acquire/release idioms and
        emits sync_primitives records. Pattern set is language-aware:
          - Python: `with lock:`, `lock.acquire()`, `lock.release()`,
                    `threading.Lock()`, `threading.RLock()`,
                    `threading.Semaphore()`, `threading.Event()`,
                    `threading.Condition()`
          - Go: `mu.Lock()`, `mu.Unlock()`, `sync.Mutex`, `sync.RWMutex`,
                `sync.WaitGroup`, `sync.Once`, `sync.Map`, `sync.Pool`,
                `<-ch` (channel receive = acquire), `ch <-` (channel send)
          - Rust: `lock.unwrap()`, `.read().unwrap()`, `.write().unwrap()`,
                  `Mutex::new`, `RwLock::new`, `std::sync::atomic`
          - Java: `synchronized (obj)`, `lock.lock()`, `lock.unlock()`,
                  `ReentrantLock`, `synchronized`
        """
        try:
            from _scanner.unified_id import unified_node_id
        except ImportError:
            return []
        out = []
        # Per-language (acquire_pattern, release_pattern, kind, var_group)
        # Patterns are simple regex; the first capture group names the
        # sync variable (when present).
        # Pre-compiled merged sync regex per language (built once at module level)
        from _scanner._sync_patterns import get_sync_patterns
        merged_re, alt_meta = get_sync_patterns(language)
        if merged_re is None:
            return []
        alt_meta_by_name = {m[3]: m for m in alt_meta}
        for fn in functions:
            fn_id_str = fn.get('id', '') or ''
            fn_nid = fn_legacy_to_nid.get(fn_id_str)
            if fn_nid is None:
                continue
            body = fn.get('body_text', '') or ''
            if not body:
                continue
            fn_start_byte = int(fn.get('start_byte', 0) or fn.get('byte_start', 0) or 0)
            for m in merged_re.finditer(body):
                gname = m.lastgroup
                if gname is None:
                    continue
                kind, style, var_grp_idx, _ = alt_meta_by_name[gname]
                var_name = m.group(var_grp_idx) if var_grp_idx is not None else ''
                sync_var_nid = None
                if var_name:
                    var_fqn = f'{fn_id_str}::{var_name}'
                    sync_var_nid = unified_node_id(language, var_fqn)
                    if sync_var_nid not in seen_node_ids:
                        seen_node_ids.add(sync_var_nid)
                        sv_bs = fn_start_byte + m.start(var_grp_idx) if var_grp_idx is not None and fn_start_byte else 0
                        sv_be = fn_start_byte + m.end(var_grp_idx) if var_grp_idx is not None and fn_start_byte else 0
                        sv_snippet = ''
                        if sv_bs and sv_be and sv_be > sv_bs and source_bytes:
                            if 0 <= sv_bs < sv_be <= len(source_bytes):
                                snip = source_bytes[sv_bs:sv_be].decode('utf-8', errors='replace')
                                if len(snip) > 4096:
                                    snip = snip[:4096] + '…'
                                sv_snippet = snip
                        cgdb_nodes.append({
                            'id': sync_var_nid,
                            'kind': 'var',
                            'name': var_name,
                            'fqn': var_fqn,
                            'file_id': fid,
                            'file_path': filepath,
                            'line': 0,
                            'col': 0,
                            'byte_start': sv_bs,
                            'byte_end': sv_be,
                            'source_snippet': sv_snippet,
                            'type_spelling': 'sync_var',
                            'attrs': {'inferred_from': 'sync_pattern'},
                            'source_layer': 'legacy',
                            'confidence': 0.6,
                            'enclosing_symbol_id': fn_nid,
                        })
                out.append({
                    'function_id': fn_nid,
                    'kind': kind,
                    'sync_var_id': sync_var_nid,
                    'acquire_stmt_id': None,
                    'release_stmt_id': None,
                })
                if kind == 'lock_acquire' and style in (
                    'with_stmt', 'synchronized', 'lock_guard',
                    'unique_lock',
                ):
                    out.append({
                        'function_id': fn_nid,
                        'kind': 'lock_release',
                        'sync_var_id': sync_var_nid,
                        'acquire_stmt_id': None,
                        'release_stmt_id': None,
                    })
        return out

    def _emit_happens_before(self, language: str, functions: list,
                             fn_legacy_to_nid: dict) -> list:
        """Derive intra-function happens-before edges.

        For each function, identifies variable definitions (writes) and
        subsequent reads of the same variable within the same function body,
        then emits HappensBefore-like records with reason='program_order'.

        Additionally, when a function contains lock_acquire/lock_release
        pairs around a variable write, subsequent reads of that variable
        after the lock release are tagged with reason='lock'.

        The write_event_id and read_event_id reference the variable node id
        (matching the def_stmt_id/use_stmt_id convention used by data_flow
        when no separate stmt nodes exist).
        """
        try:
            from _scanner.unified_id import unified_node_id
        except ImportError:
            return []
        out = []
        # Per-language assignment LHS pattern. Hoisted to module level
        # as _ASSIGN_LHS_HB_BY_LANG.
        assign_lhs_by_lang = _ASSIGN_LHS_HB_BY_LANG
        for fn in functions:
            fn_id_str = fn.get('id', '') or ''
            fn_nid = fn_legacy_to_nid.get(fn_id_str)
            if fn_nid is None:
                continue
            body = fn.get('body_text', '') or ''
            if not body:
                continue
            # Collect (var_name → node_id) for variables visible in this
            # function. local_vars includes both parameters and declared
            # locals.
            local_vars = fn.get('local_vars', []) or []
            var_name_to_nid: dict = {}
            for lv in local_vars:
                lv_name = lv.get('name', '') or ''
                if not lv_name:
                    continue
                lv_fqn = f'{fn_id_str}::{lv_name}'
                var_name_to_nid[lv_name] = unified_node_id(language, lv_fqn)
            if not var_name_to_nid:
                continue
            # Split body into lines to track program order.
            body_lines = body.split('\n')
            assign_pat = assign_lhs_by_lang.get(language)
            # Collect sync primitive lines for this function (line numbers
            # of lock_acquire and lock_release). We re-scan the body for
            # these because sync_primitives records don't carry line info.
            # For `with lock:` (Python) / `synchronized (obj)` (Java) /
            # `lock_guard` (C++), the release is implicit at block exit —
            # we compute the block-end by tracking indentation (Python) or
            # brace depth (Java/C++).
            lock_acquire_lines: list = []
            lock_release_lines: list = []
            # Fallback: inline patterns for the most common acquire/release.
            # Pre-compiled at module level as _HB_ACQUIRE_PAT / _HB_RELEASE_PAT.
            acquire_pat = _HB_ACQUIRE_PAT
            release_pat = _HB_RELEASE_PAT
            # Track implicit-release block ends for `with`/`synchronized`/
            # `lock_guard` style acquires. Map: acquire_line → release_line.
            implicit_release: dict = {}
            for line_idx, line in enumerate(body_lines, 1):
                if acquire_pat.search(line):
                    lock_acquire_lines.append(line_idx)
                    # Determine if this is a block-style acquire (with/sync).
                    is_block = bool(_HB_BLOCK_ACQUIRE_RE.search(line))
                    if is_block:
                        # Find the block end: next line at same/lower
                        # indentation (Python-style) or matching brace
                        # (Java/C++-style). For simplicity, use indentation
                        # for Python and brace-matching for others.
                        acq_indent = len(line) - len(line.lstrip())
                        rel_line = line_idx
                        if '{' in line:
                            # Brace-style: find matching close brace.
                            depth = line.count('{') - line.count('}')
                            for j in range(line_idx + 1, len(body_lines) + 1):
                                nxt = body_lines[j - 1] if j - 1 < len(body_lines) else ''
                                depth += nxt.count('{') - nxt.count('}')
                                if depth <= 0:
                                    rel_line = j
                                    break
                        else:
                            # Python-style: next line at <= acq_indent that
                            # isn't blank or a comment.
                            for j in range(line_idx + 1, len(body_lines) + 1):
                                nxt = body_lines[j - 1] if j - 1 < len(body_lines) else ''
                                if not nxt.strip() or nxt.lstrip().startswith('#'):
                                    continue
                                nxt_indent = len(nxt) - len(nxt.lstrip())
                                if nxt_indent <= acq_indent:
                                    rel_line = j - 1
                                    break
                            else:
                                rel_line = len(body_lines)
                        if rel_line > line_idx:
                            implicit_release[line_idx] = rel_line
                            lock_release_lines.append(rel_line)
                if release_pat.search(line):
                    lock_release_lines.append(line_idx)
            # Sort acquire/release lines for binary search. Implicit-release
            # appends (e.g. `with lock:` block-end at line 10 followed by an
            # explicit release on line 6) can produce out-of-order lists.
            sorted_acquires = sorted(lock_acquire_lines)
            sorted_releases = sorted(lock_release_lines)
            # Walk lines in program order. For each line:
            #  - if it's a write to a known local var, mark the var as written
            #    at this line.
            #  - if it's a read of a previously-written var, emit a
            #    happens_before record (write_event_id = var_nid, read_event_id
            #    = var_nid). Determine reason: if the write occurred inside a
            #    lock_acquire..lock_release critical section and the read is
            #    after the matching release, reason='lock'; otherwise
            #    reason='program_order'.
            # Track (var_name → line_of_last_write).
            last_write_line: dict = {}
            seen_hb_keys: set = set()
            for line_idx, line in enumerate(body_lines, 1):
                # Detect writes via the assignment LHS pattern.
                if assign_pat is not None:
                    for m in assign_pat.finditer(line):
                        lhs_name = m.group(1)
                        if lhs_name in var_name_to_nid:
                            last_write_line[lhs_name] = line_idx
                # Detect reads of any previously-written local var.
                for vname, var_nid in var_name_to_nid.items():
                    if vname not in last_write_line:
                        continue
                    # Skip the write-line itself (the LHS occurrence is a
                    # write, not a read).
                    if last_write_line[vname] == line_idx:
                        continue
                    # Match the variable as a word boundary on this line.
                    if not re.search(r'\b' + re.escape(vname) + r'\b', line):
                        continue
                    # Determine reason: 'lock' if the write line was inside a
                    # critical section (between a lock_acquire and the
                    # subsequent lock_release) and the read line is after the
                    # release; otherwise 'program_order'.
                    # The original condition was:
                    #   ∃(acq, rel): acq <= write_line ∧ rel >= acq ∧
                    #                rel >= write_line ∧ rel < line_idx
                    # Since acq <= write_line ⇒ rel >= acq is implied by
                    # rel >= write_line, this decouples into two independent
                    # existence tests, each O(log) via bisect:
                    #   (1) ∃ acq <= write_line
                    #   (2) ∃ rel with write_line <= rel < line_idx
                    reason = 'program_order'
                    write_line = last_write_line[vname]
                    if sorted_acquires and sorted_releases:
                        a_idx = bisect.bisect_right(sorted_acquires, write_line)
                        if a_idx > 0:
                            r_idx = bisect.bisect_left(sorted_releases, write_line)
                            if (r_idx < len(sorted_releases)
                                    and sorted_releases[r_idx] < line_idx):
                                reason = 'lock'
                    key = (var_nid, var_nid, reason)
                    if key in seen_hb_keys:
                        continue
                    seen_hb_keys.add(key)
                    out.append({
                        'write_event_id': var_nid,
                        'read_event_id': var_nid,
                        'reason': reason,
                        'confidence': 0.7 if reason == 'program_order' else 0.85,
                    })
        return out

    def _emit_cfg(self, language: str, functions: list,
                  fn_legacy_to_nid: dict) -> tuple:
        """Emit a simplified CFG (basic_blocks + cfg_edges) per function.

        For each function, produces:
          - 1 entry block (block_index=0, is_entry=True)
          - N body blocks split at control-flow statements
            (if/elif/else/for/while/try/switch/match)
          - 1 exit block (block_index=N+1, is_exit=True)

        Edges:
          entry → first body block (fallthrough)
          body[i] → body[i+1] (fallthrough for linear runs)
          branch points emit true_branch / false_branch edges
          last body block → exit (fallthrough)

        The block ID is a stable hash of (function_id, block_index) so
        re-scans produce identical IDs.
        """
        try:
            from _scanner.unified_id import unified_node_id
        except ImportError:
            return ([], [])
        # Per-language control-flow keyword patterns. Hoisted to module
        # level as _CF_KEYWORDS_BY_LANG.
        cf_patterns_by_lang = _CF_KEYWORDS_BY_LANG
        cf_pat = cf_patterns_by_lang.get(language)
        blocks_out: list = []
        edges_out: list = []
        for fn in functions:
            fn_id_str = fn.get('id', '') or ''
            fn_nid = fn_legacy_to_nid.get(fn_id_str)
            if fn_nid is None:
                continue
            body = fn.get('body_text', '') or ''
            if not body:
                continue
            body_lines = body.split('\n')
            # Determine the function body's base indentation (first non-empty
            # non-docstring line). Lines at this indent are "statement starts"
            # for CFG block boundaries.
            base_indent = -1
            for line in body_lines[1:]:
                stripped = line.lstrip()
                if not stripped or stripped.startswith('#'):
                    continue
                base_indent = len(line) - len(stripped)
                break
            if base_indent < 0:
                # Single-line function — emit entry + body + exit only.
                body_blocks = [(1, len(body_lines))]
            else:
                # Split body into blocks at top-level control-flow keywords.
                # Each block is (start_line, end_line) inclusive.
                body_blocks = []
                cur_start = 1
                for line_idx, line in enumerate(body_lines[1:], 2):
                    stripped = line.lstrip()
                    if not stripped or stripped.startswith('#'):
                        continue
                    indent = len(line) - len(stripped)
                    if indent != base_indent:
                        continue
                    if cf_pat and cf_pat.match(line):
                        # Close previous block at line_idx - 1.
                        if line_idx - 1 >= cur_start:
                            body_blocks.append((cur_start, line_idx - 1))
                        cur_start = line_idx
                if cur_start <= len(body_lines):
                    body_blocks.append((cur_start, len(body_lines)))
            # Generate block IDs: entry=hash(fn,0), body=hash(fn,i+1),
            # exit=hash(fn,N+1).
            n_body = len(body_blocks)
            entry_id = int(hashlib.sha256(
                f'bb:{fn_nid}:0'.encode('utf-8')).hexdigest()[:15],
                16) & 0x0FFF_FFFF_FFFF_FFFF
            exit_id = int(hashlib.sha256(
                f'bb:{fn_nid}:{n_body + 1}'.encode('utf-8')).hexdigest()[:15],
                16) & 0x0FFF_FFFF_FFFF_FFFF
            body_ids = []
            for i in range(n_body):
                bid = int(hashlib.sha256(
                    f'bb:{fn_nid}:{i + 1}'.encode('utf-8')).hexdigest()[:15],
                    16) & 0x0FFF_FFFF_FFFF_FFFF
                body_ids.append(bid)
            # Emit blocks.
            blocks_out.append({
                'id': entry_id,
                'function_id': fn_nid,
                'block_index': 0,
                'is_entry': True,
                'is_exit': False,
            })
            for i, (start, end) in enumerate(body_blocks):
                # Determine if this block is a branch (starts with if/elif/
                # for/while/try/etc.) for edge kind tagging.
                is_branch = bool(
                    cf_pat and cf_pat.match(body_lines[start - 1] or ''))
                blocks_out.append({
                    'id': body_ids[i],
                    'function_id': fn_nid,
                    'block_index': i + 1,
                    'is_entry': False,
                    'is_exit': False,
                })
                _ = is_branch  # noqa: F841 — kept for future condition_id
            blocks_out.append({
                'id': exit_id,
                'function_id': fn_nid,
                'block_index': n_body + 1,
                'is_entry': False,
                'is_exit': True,
            })
            # Emit edges.
            if not body_ids:
                # Empty body: entry → exit
                edges_out.append({
                    'src_block_id': entry_id,
                    'dst_block_id': exit_id,
                    'kind': 'fallthrough',
                    'function_id': fn_nid,
                })
                continue
            # entry → first body
            edges_out.append({
                'src_block_id': entry_id,
                'dst_block_id': body_ids[0],
                'kind': 'fallthrough',
                'function_id': fn_nid,
            })
            # body[i] → body[i+1]
            for i in range(len(body_ids) - 1):
                # If body[i] starts with a control-flow keyword, emit
                # true_branch + false_branch to next block (simplified).
                first_line = body_lines[body_blocks[i][0] - 1] or ''
                if cf_pat and cf_pat.match(first_line):
                    edges_out.append({
                        'src_block_id': body_ids[i],
                        'dst_block_id': body_ids[i + 1],
                        'kind': 'true_branch',
                        'function_id': fn_nid,
                    })
                    edges_out.append({
                        'src_block_id': body_ids[i],
                        'dst_block_id': body_ids[i + 1],
                        'kind': 'false_branch',
                        'function_id': fn_nid,
                    })
                else:
                    edges_out.append({
                        'src_block_id': body_ids[i],
                        'dst_block_id': body_ids[i + 1],
                        'kind': 'fallthrough',
                        'function_id': fn_nid,
                    })
            # last body → exit
            edges_out.append({
                'src_block_id': body_ids[-1],
                'dst_block_id': exit_id,
                'kind': 'fallthrough',
                'function_id': fn_nid,
            })
        return (blocks_out, edges_out)

    def _classify_doc_kind(self, doc: str) -> str:
        """Classify a doc-comment-like string by its delimiter style."""
        if not doc:
            return ''
        s = doc.strip()
        if s.startswith('"""') or s.startswith("'''"):
            return 'doxygen_block'
        if s.startswith('/**') or s.startswith('/*!'):
            return 'doxygen_block'
        if s.startswith('//'):
            return 'line'
        if s.startswith('/*'):
            return 'block'
        if s.startswith('--') or s.startswith('#'):
            return 'line'
        return 'block'

    @abstractmethod
    def _parse(self, source_bytes: bytes):
        """Parse source and return tree-sitter Tree."""
        ...

    @abstractmethod
    def _extract(self, tree, source_bytes: bytes, filepath: str,
                 source_root: str, domain: str):
        """Extract functions and edges from a parsed tree.

        Returns (functions_list, edges_list).
        """
        ...

    def _node_text(self, node, source_bytes: bytes) -> str:
        return source_bytes[node.start_byte:node.end_byte].decode('utf-8', errors='replace')

    def _normalize_name(self, name: str) -> str:
        return _NORMALIZE_NAME_RE.sub('_', name)

    def _make_func_id(self, domain: str, name: str) -> str:
        return domain.replace(".", "_") + "_" + self._normalize_name(name).lower()

    def _make_empty_id(self, invoker_id: str, cond_index: int) -> str:
        return f"{invoker_id}__cond_{cond_index}"

    # Thread spawn / callback patterns per language
    THREAD_SPAWN_PATTERNS = {
        'pthread_create': (3, "pthread thread entry"),
        '_beginthread': (1, "CRT thread entry"),
        '_beginthreadex': (3, "CRT thread entry ex"),
        'CreateThread': (3, "Win32 thread entry"),
    }

    # Profile-driven callback patterns (set by scanner factory from profile)
    # Maps register_func → (cb_arg_index, concurrency_type)
    _callback_patterns = {}

    @staticmethod
    def _looks_like_callable(text: str) -> bool:
        """Heuristic: does this argument text look like a callable reference
        rather than a string/number/other literal?

        Returns False for:
        - String literals: "..." / '...' / f"..." / r"..."
        - Number literals: 123 / 0x1F / 3.14
        - Boolean/None literals: true / false / None / null / nil
        - Byte strings: b"..." / b'...'

        Returns True for:
        - Bare identifiers: foo
        - Dotted names: obj.method, module.func
        - Pointer/reference-prefixed identifiers: &foo, *foo
        - Lambda expressions: lambda x: ...
        - Method references: ClassName::methodName, obj::method
        - Function calls (used as Higher-Order arg): foo()
        - Parenthesized callable: (foo)
        - Address-of / deref chains: &obj.method, *ptr
        """
        if not text:
            return False
        t = text.strip()
        if not t:
            return False
        # String/byte literal
        if t[0] in ('"', "'") or t.startswith(('f"', "f'", 'r"', "r'", 'b"', "b'", 'rb"', "rb'", 'br"', "br'")):
            return False
        # Number literal (int, float, hex, octal, binary)
        if _LITERAL_NUM_RE_1.match(t) or _LITERAL_NUM_RE_2.match(t):
            return False
        # Boolean / null / none literals
        if t in ('true', 'false', 'True', 'False', 'None', 'null', 'nil', 'NULL', 'nullptr'):
            return False
        # Lambda expressions are callable
        if t.startswith('lambda') and ':' in t:
            return True
        # Strip leading & * (C/C++ address-of / deref)
        stripped = t.lstrip('&* ')
        if not stripped:
            return False
        # Identifier or dotted name (allow :: for C++/Java/Rust method refs)
        if _CALLABLE_IDENT_RE.match(stripped):
            return True
        # Parenthesized callable or call expression: (foo), foo()
        if '(' in stripped or ')' in stripped:
            inner = stripped.strip('()').strip()
            if inner and _CALLABLE_IDENT_RE.match(inner):
                return True
        return False

    def _detect_concurrency_info(self, callee_name: str, args_structured: list) -> dict:
        """Detect if a call creates a concurrent execution (thread spawn, goroutine, etc)."""
        info = {"is_spawn": False, "spawn_target": "", "spawn_arg": "", "concurrency_type": ""}

        pattern = self.THREAD_SPAWN_PATTERNS.get(callee_name)
        if pattern:
            arg_pos, desc = pattern
            target_arg = next((a for a in args_structured if a["pos"] == arg_pos), None)
            if target_arg and self._looks_like_callable(target_arg["value"]):
                info["is_spawn"] = True
                info["spawn_target"] = target_arg["value"].lstrip('*& ')
                info["concurrency_type"] = "thread_spawn"
                arg_pos2 = arg_pos + 1
                arg_arg = next((a for a in args_structured if a["pos"] == arg_pos2), None)
                if arg_arg:
                    info["spawn_arg"] = arg_arg["value"]
            return info

        if callee_name in ('std::thread', 'thread', 'jthread', 'std::jthread'):
            first_arg = next((a for a in args_structured if a["pos"] == 1), None)
            if first_arg and self._looks_like_callable(first_arg["value"]):
                info["is_spawn"] = True
                info["spawn_target"] = first_arg["value"]
                info["concurrency_type"] = "thread_spawn"
            return info

        if callee_name == 'threading.Thread':
            # Python threading.Thread uses keyword target= and args=
            for a in args_structured:
                if a["value"].startswith('target='):
                    target_val = a["value"].split('=', 1)[1].strip()
                    if self._looks_like_callable(target_val):
                        info["is_spawn"] = True
                        info["spawn_target"] = target_val
                        info["concurrency_type"] = "thread_spawn"
                if a["value"].startswith('args='):
                    info["spawn_arg"] = a["value"].split('=', 1)[1].strip()
            return info

        if callee_name in ('Thread', 'submit', 'execute'):
            # Java Thread(runnable) / ExecutorService.submit/execute — positional first-arg.
            # Filter out non-callable first-arg (e.g., cursor.execute("SELECT ...") where
            # the first arg is a SQL string literal, not a Runnable).
            first_arg = next((a for a in args_structured if a["pos"] == 1), None)
            if first_arg and self._looks_like_callable(first_arg["value"]):
                info["is_spawn"] = True
                info["spawn_target"] = first_arg["value"]
                info["concurrency_type"] = "thread_spawn"
            return info

        if callee_name in ('spawn', 'thread_spawn', 'thread::spawn',
                           'tokio::spawn', 'tokio::task::spawn',
                           'async_std::task::spawn', 'smol::spawn'):
            first_arg = next((a for a in args_structured if a["pos"] == 1), None)
            if first_arg and self._looks_like_callable(first_arg["value"]):
                info["is_spawn"] = True
                info["spawn_target"] = first_arg["value"]
                info["concurrency_type"] = "async_spawn" if 'tokio' in callee_name or 'async' in callee_name else "thread_spawn"
            return info

        CALLBACK_PATTERNS = {'register_callback', 'signal', 'atexit', 'on_exit',
                             'SetConsoleCtrlHandler', 'qsort', 'bsearch'}
        if callee_name in CALLBACK_PATTERNS:
            cb_arg_pos = 2 if callee_name == 'signal' else 1
            cb_arg = next((a for a in args_structured if a["pos"] == cb_arg_pos), None)
            if cb_arg and self._looks_like_callable(cb_arg["value"]):
                info["is_spawn"] = False
                info["spawn_target"] = cb_arg["value"].lstrip('*& ')
                info["concurrency_type"] = "callback_register"
            return info

        # Profile-driven callback patterns (e.g., kthread_run, INIT_WORK, call_rcu,
        # rte_eal_mp_remote_launch, rte_intr_callback_register)
        cb_pat = self._callback_patterns.get(callee_name)
        if cb_pat:
            arg_pos, concurrency_type = cb_pat
            if arg_pos < 0:
                # Negative arg_pos means the callback is implicit (not a direct param)
                # e.g., qemu_bh_schedule schedules a previously-registered callback
                info["is_spawn"] = True
                info["spawn_target"] = ""
                info["concurrency_type"] = concurrency_type
            else:
                target_arg = next((a for a in args_structured if a["pos"] == arg_pos), None)
                if target_arg and self._looks_like_callable(target_arg["value"]):
                    info["is_spawn"] = True
                    info["spawn_target"] = target_arg["value"].lstrip('*& ')
                    info["concurrency_type"] = concurrency_type
            return info

        return info

    def _evaluate_preproc_condition(self, condition: str, directive: str) -> bool:
        """Evaluate whether a preprocessor condition branch is alive.

        Default implementation always returns True (parse everything).
        C/C++ scanner overrides with actual #ifdef evaluation using macro_bindings.
        """
        return True

    def _detect_api_entry(self, func_name: str, func_node, source_bytes: bytes) -> tuple:
        """Detect if a function is a public API entry point.

        Returns (is_api: bool, constraints: str).
        Subclasses should override for language-specific detection.
        """
        return False, ""

    def _extract_param_constraints(self, params_text: str) -> str:
        """Extract parameter constraints from a parameter list text."""
        if not params_text or params_text.strip() in ('()', '(void)', '()'):
            return ""
        inner = params_text.strip().strip('()')
        if not inner or inner == 'void':
            return ""
        parts = []
        for param in inner.split(','):
            param = param.strip()
            if not param or param == 'void':
                continue
            tokens = param.split()
            if tokens:
                name = tokens[-1].lstrip('*&')
                type_str = ' '.join(tokens[:-1]).strip() if len(tokens) > 1 else ""
                if type_str:
                    parts.append(f"{name}: {type_str}")
                else:
                    parts.append(name)
        return "; ".join(parts) if parts else ""

    def _extract_body_text(self, func_node, source_bytes: bytes) -> str:
        """Extract the full function body source text."""
        return self._node_text(func_node, source_bytes)

    @staticmethod
    def _extract_doc_comment(func_node, source_bytes: bytes) -> str:
        """Extract the doc comment immediately preceding a function node.

        Recognizes:
        - C/C++/Java: /** ... */ block comments, /// line comments
        - Python: \"\"\" ... \"\"\" docstrings (first statement in body)
        - Rust: /// and //! line comments, /** ... */ block comments
        - Generic: -- comments (SQL-style)

        Returns the comment text with delimiters stripped, or "" if no
        doc comment is found. The comment must be directly adjacent to
        the function (no blank lines between, allowing a single blank
        line for /** blocks).
        """
        if func_node is None:
            return ""
        start_byte = func_node.start_byte
        if start_byte == 0:
            # Check for Python docstring as first statement in body
            return BaseScanner._extract_python_docstring(func_node, source_bytes)
        # Look at the text immediately before the function (up to 2KB)
        lookback = min(start_byte, 2048)
        before = source_bytes[start_byte - lookback:start_byte]
        try:
            before_text = before.decode("utf-8", errors="replace")
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            return ""
        # Find the last block comment /** ... */ before the function
        # Strip trailing whitespace and check what's immediately before
        # the function start.
        stripped = before_text.rstrip()
        if not stripped:
            return BaseScanner._extract_python_docstring(func_node, source_bytes)

        # /** ... */ block comment — take the LAST one (closest to the function).
        # Using findall + last match instead of search, because search with
        # \s*$ anchor would greedily span from the first /** to the last */,
        # including intermediate code in the capture group.
        block_matches = _DOC_COMMENT_BLOCK_RE.findall(stripped)
        if block_matches:
            inner = block_matches[-1]
            # Strip leading * on each line (Javadoc-style)
            lines = []
            for line in inner.split('\n'):
                cleaned = _DOC_COMMENT_LINE_RE.sub('', line)
                lines.append(cleaned)
            return '\n'.join(lines).strip()

        # /// line comments — collect consecutive /// lines immediately before
        lines_before = stripped.split('\n')
        doc_lines = []
        for line in reversed(lines_before):
            stripped_line = line.strip()
            if stripped_line.startswith('///') or stripped_line.startswith('//!'):
                # Strip the prefix (/// or //!) and one optional space
                prefix_len = 3 if stripped_line[:3] in ('///', '//!') else 0
                doc_lines.insert(0, stripped_line[prefix_len:].lstrip())
            elif not stripped_line:
                # Allow blank lines between /// groups? No — require adjacency
                break
            else:
                break
        if doc_lines:
            return '\n'.join(doc_lines).strip()

        # -- line comments (SQL-style)
        dash_lines = []
        for line in reversed(lines_before):
            stripped_line = line.strip()
            if stripped_line.startswith('--'):
                dash_lines.insert(0, stripped_line[2:].lstrip())
            elif not stripped_line:
                break
            else:
                break
        if dash_lines:
            return '\n'.join(dash_lines).strip()

        # Fall back to Python docstring (if applicable)
        return BaseScanner._extract_python_docstring(func_node, source_bytes)

    @staticmethod
    def _extract_python_docstring(func_node, source_bytes: bytes) -> str:
        """Extract a Python docstring from the first statement of a function body.

        Looks for an `expression_statement` containing a string_literal as
        the first body element. Returns the string content (without quotes)
        or "" if not found.
        """
        if func_node is None:
            return ""
        # Find the body block
        body = None
        for child in func_node.children:
            if child.type in ('block', 'body', 'suite'):
                body = child
                break
        if body is None:
            return ""
        # First statement in body
        for child in body.children:
            if child.type == 'expression_statement':
                # First child of expression_statement should be a string
                for sub in child.children:
                    if sub.type in ('string', 'string_literal'):
                        text = source_bytes[sub.start_byte:sub.end_byte].decode(
                            "utf-8", errors="replace")
                        # Strip surrounding quotes (single, double, or triple)
                        text = text.strip()
                        if text.startswith('"""') and text.endswith('"""'):
                            return text[3:-3].strip()
                        if text.startswith("'''") and text.endswith("'''"):
                            return text[3:-3].strip()
                        if text.startswith('"') and text.endswith('"'):
                            return text[1:-1].strip()
                        if text.startswith("'") and text.endswith("'"):
                            return text[1:-1].strip()
                        return text
        return ""

    def _extract_signature(self, func_node, source_bytes: bytes) -> str:
        """Extract the function signature (first line or declaration before body).

        Handles C++ initializer lists like `Foo::Foo() : member{1} {` by
        recognizing brace pairs in the initializer section.
        """
        text = self._node_text(func_node, source_bytes)
        if '{' not in text:
            return text.split('\n')[0].strip()
        # Walk the text to find the opening brace of the function body,
        # skipping brace pairs inside initializer lists (e.g., `: member{1}`)
        depth = 0
        i = 0
        # Skip past any parenthesized parameter list first
        while i < len(text):
            if text[i] == '(':
                depth += 1
            elif text[i] == ')':
                depth -= 1
            elif text[i] == '{' and depth == 0:
                # Check if this is inside an initializer (after ':')
                pre = text[:i].rstrip()
                if pre.endswith(':') or pre.endswith(','):
                    # Inside initializer list — skip this brace pair
                    j = i + 1
                    inner = 1
                    while j < len(text) and inner > 0:
                        if text[j] == '{':
                            inner += 1
                        elif text[j] == '}':
                            inner -= 1
                        j += 1
                    i = j
                    continue
                # This is the function body opening brace
                return text[:i].strip()
            i += 1
        # Fallback: simple split
        return text.split('{')[0].strip()

    def _extract_params(self, func_node, source_bytes: bytes) -> list:
        """Extract formal parameters from a function definition.

        Returns list of {"name", "type", "is_param": True}.
        Works across all languages by checking multiple AST patterns.
        """
        params = []
        params_node = None
        for child in func_node.children:
            if child.type in ('parameter_list', 'parameters'):
                params_node = child
                break
            if child.type == 'function_declarator':
                for inner in child.children:
                    if inner.type in ('parameter_list', 'parameters'):
                        params_node = inner
                        break
            if child.type in ('pointer_declarator', 'reference_declarator'):
                for inner in child.children:
                    if inner.type == 'function_declarator':
                        for inner2 in inner.children:
                            if inner2.type in ('parameter_list', 'parameters'):
                                params_node = inner2
                                break
                if params_node:
                    break

        if params_node is None:
            return params

        for child in params_node.children:
            if child.type == 'parameter_declaration':
                param_name = ""
                param_type = ""
                has_identifier = False
                has_type_identifier = False
                # Track byte range of the identifier for source-level queries.
                param_start_byte = 0
                param_end_byte = 0
                param_line = 0
                param_column = 0
                for sub in child.children:
                    if sub.type == 'identifier':
                        if not param_name:
                            param_name = self._node_text(sub, source_bytes).strip()
                            has_identifier = True
                            param_start_byte = sub.start_byte
                            param_end_byte = sub.end_byte
                            param_line = sub.start_point[0] + 1
                            param_column = sub.start_point[1] + 1
                    elif sub.type in ('type_identifier', 'primitive_type', 'sized_type_specifier',
                                      'generic_type', 'array_type', 'pointer_type',
                                      'qualified_type', 'struct_type', 'interface_type',
                                      'function_type', 'map_type', 'channel_type'):
                        param_type = self._node_text(sub, source_bytes).strip()
                        has_type_identifier = True
                    elif sub.type in ('mut_pattern', 'variadic_parameter_declaration'):
                        for inner in sub.children:
                            if inner.type == 'identifier' and not param_name:
                                param_name = self._node_text(inner, source_bytes).strip()
                                param_start_byte = inner.start_byte
                                param_end_byte = inner.end_byte
                                param_line = inner.start_point[0] + 1
                                param_column = inner.start_point[1] + 1
                            elif inner.type in ('type_identifier', 'primitive_type'):
                                param_type = self._node_text(inner, source_bytes).strip()
                if param_name and has_identifier:
                    params.append({
                        "name": param_name, "type": param_type, "is_param": True,
                        "start_byte": param_start_byte, "end_byte": param_end_byte,
                        "line": param_line, "column": param_column,
                    })
                    continue
                text = self._node_text(child, source_bytes).strip().rstrip(',')
                tokens = text.split()
                if not tokens or tokens[-1] == 'void':
                    continue
                if has_identifier and has_type_identifier:
                    continue
                name = tokens[-1].lstrip('*&')
                type_str = ' '.join(tokens[:-1]).strip().rstrip('*&')
                if name and re.match(r'^[A-Za-z_]\w*$', name):
                    params.append({
                        "name": name, "type": type_str, "is_param": True,
                        "start_byte": child.start_byte, "end_byte": child.end_byte,
                        "line": child.start_point[0] + 1, "column": child.start_point[1] + 1,
                    })
                elif ',' in text:
                    parts = text.split(',')
                    last = parts[-1].strip().split()
                    type_str = ' '.join(last)
                    for part in parts[:-1]:
                        n = part.strip().lstrip('*&')
                        if n and re.match(r'^[A-Za-z_]\w*$', n):
                            params.append({
                                "name": n, "type": type_str, "is_param": True,
                                "start_byte": child.start_byte, "end_byte": child.end_byte,
                                "line": child.start_point[0] + 1,
                                "column": child.start_point[1] + 1,
                            })
            elif child.type == 'identifier':
                name = self._node_text(child, source_bytes).strip().rstrip(',')
                if name and name not in ('self', 'cls', 'void'):
                    params.append({
                        "name": name, "type": "", "is_param": True,
                        "start_byte": child.start_byte, "end_byte": child.end_byte,
                        "line": child.start_point[0] + 1, "column": child.start_point[1] + 1,
                    })
            elif child.type == 'typed_default_parameter':
                text = self._node_text(child, source_bytes).strip()
                m = re.match(r'(\w+)\s*:\s*(\w+)', text)
                if m:
                    params.append({
                        "name": m.group(1), "type": m.group(2), "is_param": True,
                        "start_byte": child.start_byte, "end_byte": child.end_byte,
                        "line": child.start_point[0] + 1, "column": child.start_point[1] + 1,
                    })
            elif child.type == 'default_parameter':
                text = self._node_text(child, source_bytes).strip()
                m = re.match(r'(\w+)\s*=', text)
                if m:
                    params.append({
                        "name": m.group(1), "type": "", "is_param": True,
                        "start_byte": child.start_byte, "end_byte": child.end_byte,
                        "line": child.start_point[0] + 1, "column": child.start_point[1] + 1,
                    })
            elif child.type in ('typed_parameter',):
                text = self._node_text(child, source_bytes).strip()
                m = re.match(r'(\w+)\s*:\s*(\w+)', text)
                if m:
                    params.append({
                        "name": m.group(1), "type": m.group(2), "is_param": True,
                        "start_byte": child.start_byte, "end_byte": child.end_byte,
                        "line": child.start_point[0] + 1, "column": child.start_point[1] + 1,
                    })
            elif child.type in ('list_splat_pattern', 'dictionary_splat_pattern'):
                text = self._node_text(child, source_bytes).strip().lstrip('*')
                if text:
                    params.append({
                        "name": text, "type": "varargs" if child.type == 'list_splat_pattern' else "kwargs",
                        "is_param": True,
                        "start_byte": child.start_byte, "end_byte": child.end_byte,
                        "line": child.start_point[0] + 1, "column": child.start_point[1] + 1,
                    })
            elif child.type == 'self_parameter':
                params.append({
                    "name": "self", "type": "Self", "is_param": True,
                    "start_byte": child.start_byte, "end_byte": child.end_byte,
                    "line": child.start_point[0] + 1, "column": child.start_point[1] + 1,
                })
            elif child.type == 'spread_element':
                text = self._node_text(child, source_bytes).strip().lstrip('.')
                if text:
                    params.append({
                        "name": text, "type": "varargs", "is_param": True,
                        "start_byte": child.start_byte, "end_byte": child.end_byte,
                        "line": child.start_point[0] + 1, "column": child.start_point[1] + 1,
                    })
            elif child.type not in (',', 'comment', 'line_comment', 'block_comment', 'void'):
                text = self._node_text(child, source_bytes).strip().rstrip(',')
                if text and re.match(r'^[A-Za-z_]\w*$', text):
                    params.append({"name": text, "type": "", "is_param": True})

        return params

    def _extract_local_vars(self, body_node, source_bytes: bytes, params_list=None) -> list:
        """Extract local variable assignments from function body.

        Returns list of {"name", "type", "value_snippet", "line", "column",
        "start_byte", "end_byte", "is_param"}.
        Parameters are included first with is_param=True, then body vars with is_param=False.
        """
        vars_list = []
        seen = set()

        if params_list:
            for p in params_list:
                name = p["name"]
                seen.add(name)
                vars_list.append({"name": name, "type": p.get("type", ""),
                                  "value_snippet": "<param>",
                                  "line": p.get("line", 0) or 0,
                                  "column": p.get("column", 0) or 0,
                                  "start_byte": p.get("start_byte", 0) or 0,
                                  "end_byte": p.get("end_byte", 0) or 0,
                                  "is_param": True})

        # Iterative traversal to avoid RecursionError on deeply nested ASTs (Linux kernel)
        if body_node:
            stack = [body_node]
            while stack:
                nd = stack.pop()
                if nd.type in ('declaration', 'declaration_statement',
                                 'variable_declaration', 'local_variable_declaration',
                                 'var_statement', 'short_var_declaration',
                                 'let_declaration', 'let_statement'):
                    text = self._node_text(nd, source_bytes)
                    pos = self._node_position(nd)
                    m = _LOCAL_DECL_RE.match(text.strip())
                    if m:
                        name = m.group(1)
                        val = m.group(2).rstrip(';').strip()
                        if name not in seen and len(name) > 1:
                            seen.add(name)
                            type_match = _LOCAL_DECL_TYPE_RE.match(text.strip())
                            vtype = type_match.group(1) if type_match else ""
                            if not vtype:
                                vtype = self._infer_type_from_value(val)
                            vars_list.append({"name": name, "type": vtype, "value_snippet": val,
                                              "line": pos["line"], "column": pos["column"],
                                              "start_byte": pos["start_byte"], "end_byte": pos["end_byte"],
                                              "is_param": False})
                if nd.type == 'assignment':
                    text = self._node_text(nd, source_bytes)
                    pos = self._node_position(nd)
                    m = re.match(r'(\w+)\s*=\s*(.{0,400})', text.strip(), re.DOTALL)
                    if m:
                        name = m.group(1)
                        val = m.group(2).strip()
                        if name not in seen and name not in ('self', 'True', 'False', 'None') and name != '_':
                            seen.add(name)
                            inferred_type = self._infer_type_from_value(val)
                            vars_list.append({"name": name, "type": inferred_type, "value_snippet": val,
                                              "line": pos["line"], "column": pos["column"],
                                              "start_byte": pos["start_byte"], "end_byte": pos["end_byte"],
                                              "is_param": False})
                for child in reversed(nd.children):
                    stack.append(child)
        return vars_list

    def _infer_type_from_value(self, value: str) -> str:
        """Infer a Python type name from an assignment RHS value snippet.

        Uses a module-level dispatch table keyed by the first identifier
        token, falling through to pre-compiled regex patterns.
        """
        if not value:
            return ''
        v = value.strip()
        v = _INFER_COMMENT_SPLIT_RE.split(v, 1)[0].strip()
        if not v:
            return ''

        # Stage 1: dispatch on first identifier token
        tok_m = _INFER_FIRST_TOKEN_RE.match(v)
        if tok_m:
            handler = _INFER_DISPATCH.get(tok_m.group(1))
            if handler is not None:
                result = handler(v)
                if result:
                    return result

        # Stage 2: literal-form checks dispatched on first character
        c0 = v[0]
        if c0 == "'" or c0 == '"':
            return 'str'
        if c0 == '[':
            if v.endswith(']'):
                return 'list'
            if ' for ' in v:
                return 'list'
            if len(v) >= 2:
                return 'list'
        elif c0 == '{':
            if v == '{}':
                return 'dict'
            if v.endswith('}'):
                inner = v[1:-1].strip()
                if inner and ':' in inner.split(',')[0] and ' for ' not in v:
                    return 'dict'
                if ' for ' in v and ':' not in v.split(' for ')[0]:
                    return 'set'
                if ' for ' in v and ':' in v.split(' for ')[0]:
                    return 'dict'
                return 'set'
            if len(v) >= 2 and ':' in v.split(',', 1)[0] and ' for ' not in v:
                return 'dict'
            if len(v) >= 2 and ':' not in v.split(',', 1)[0]:
                return 'set'
        elif c0 == '(':
            if v.endswith(')'):
                inner = v[1:-1].strip()
                if ',' in inner or inner.endswith(','):
                    return 'tuple'
            if ' for ' in v:
                return 'Generator'
        elif c0 in ('b', 'f', 'r') and len(v) >= 2 and v[1] in ("'", '"'):
            if c0 == 'b':
                return 'bytes'
            return 'str'
        elif c0 == '-' or c0.isdigit():
            if v in ('True', 'False'):
                return 'bool'
            if _INFER_INT_RE.match(v):
                return 'int'
            if (_INFER_FLOAT_RE_1.match(v) or _INFER_FLOAT_RE_2.match(v)
                    or _INFER_FLOAT_RE_3.match(v)):
                return 'float'
            if _INFER_COMPLEX_RE.match(v):
                return 'complex'

        # Stage 3: constructor / type-cast patterns
        m = _INFER_TYPE_CAST_RE.match(v)
        if m:
            return m.group(1)
        m = _INFER_PASCAL_CTOR_RE.match(v)
        if m:
            return m.group(1)
        m = _INFER_QUALIFIED_CTOR_RE.match(v)
        if m:
            return m.group(2)

        # Stage 4: pre-compiled regex chain (suffix/method/arithmetic patterns)
        if _INFER_READ_RE.search(v):
            return 'str|bytes'
        if _INFER_READLINES_RE.search(v):
            return 'list'
        if _INFER_READLINE_RE.search(v):
            return 'str'
        if _INFER_KEYS_RE.search(v):
            return 'KeysView'
        if _INFER_VALUES_RE.search(v):
            return 'ValuesView'
        if _INFER_ITEMS_RE.search(v):
            return 'ItemsView'
        if _INFER_TO_DICT_RE.search(v):
            return 'dict'
        if _INFER_TO_LIST_RE.search(v):
            return 'list'
        if _INFER_TO_STR_RE.search(v):
            return 'str'
        if _INFER_TO_JSON_RE.search(v):
            return 'str'
        if _INFER_GROUP_RE.search(v) or _INFER_GROUPS_RE.search(v):
            return 'str'
        if _INFER_LIST_MUT_RE.search(v):
            return 'list'
        if _INFER_STR_METHOD_RE.search(v):
            return 'str'
        if _INFER_LOWER_FIND_RE.search(v):
            return 'int'
        if _INFER_BOOL_PRED_RE.search(v):
            return 'bool'
        if _INFER_GET_RE.search(v) or _INFER_SETDEFAULT_RE.search(v):
            return 'Any'
        # Boolean expressions
        if _INFER_EQ_RE.search(v) or _INFER_NE_RE.search(v) or _INFER_BOOL_OPS_RE.search(v):
            return 'bool'
        if _INFER_COMPARE_RELOP_RE.search(v):
            return 'bool'
        # Pathlib division (must precede generic arithmetic)
        if _INFER_PATH_DIV_RE.match(v) and not _INFER_PATH_DIV_NUM_RE.match(v) \
                and not v[0].isdigit():
            return 'Path'
        # Arithmetic -> int (with multiplication heuristics)
        if _INFER_ARITH_RE.match(v) or _INFER_MUL_IDENT_RE.match(v) \
                or _INFER_MUL_NUM_R_RE.match(v) or _INFER_MUL_NUM_L_RE.match(v):
            return 'int'
        if _INFER_ADD_INT_L_RE.match(v) or _INFER_ADD_INT_R_RE.match(v):
            return 'int'
        # List concat
        if _INFER_LIST_PLUS_RE.search(v) or _INFER_LIST_PLUS_R_RE.search(v):
            return 'list'
        # String concat
        if _INFER_STR_CONCAT_L_RE.search(v) or _INFER_STR_CONCAT_R_RE.search(v):
            return 'str'
        # tree-sitter positional ops
        if _INFER_START_POINT_RE.search(v) or _INFER_END_POINT_RE.search(v):
            return 'int'
        # Conditional expression
        if ' if ' in v and ' else ' in v:
            return 'Any'
        # Indexing
        if _INFER_INDEX_RE.match(v):
            return 'Any'
        # Attribute access (no call)
        if _INFER_ATTR_ANY_RE.match(v) and '(' not in v:
            return 'Any'
        # Method call on attribute
        if _INFER_METHOD_ANY_RE.match(v):
            return 'Any'
        # argparse attribute
        if _INFER_ARGPARSE_ATTR_RE.match(v):
            return 'argparse_attr'
        # Lowercase user-function call
        if _INFER_USER_FN_RE.match(v):
            return 'Any'
        return ''

    def _extract_callee_args(self, call_node, source_bytes: bytes) -> str:
        """Extract the arguments text from a call expression."""
        args_node = call_node.child_by_field_name('arguments')
        if args_node:
            return self._node_text(args_node, source_bytes).strip('() ')
        for child in call_node.children:
            if child.type in ('argument_list', 'arguments', 'parenthesized_expression'):
                return self._node_text(child, source_bytes).strip('() ')
        return ""

    def _extract_callee_args_structured(self, call_node, source_bytes: bytes) -> list:
        """Extract structured argument list from a call expression.

        Returns list of {"pos": int, "value": str} where pos is 1-based position.
        """
        args_node = call_node.child_by_field_name('arguments')
        if args_node is None:
            for child in call_node.children:
                if child.type in ('argument_list', 'arguments', 'parenthesized_expression'):
                    args_node = child
                    break
        if args_node is None:
            return []

        args = []
        pos = 1
        for child in args_node.children:
            if child.type in (',', 'comment', 'line_comment', 'block_comment', '(', ')'):
                continue
            if child.type in ('argument_list', 'arguments'):
                continue
            text = self._node_text(child, source_bytes).strip()
            if text and text not in ('(', ')'):
                args.append({"pos": pos, "value": text})
                pos += 1
        return args

    def _extract_condition_vars(self, condition_text: str) -> list:
        """Extract variable names referenced in a condition expression."""
        if not condition_text:
            return []
        cleaned = _COND_TOKEN_SUBST_RE_1.sub(' ', condition_text)
        cleaned = _COND_TOKEN_SUBST_RE_2.sub(' ', cleaned)
        cleaned = _COND_TOKEN_SUBST_RE_3.sub(' ', cleaned)
        vars_found = []
        for token in cleaned.split():
            if _COND_TOKEN_IDENT_RE.match(token) and len(token) > 1 and token not in (
                'if', 'else', 'switch', 'case', 'match', 'and', 'or', 'not', 'in', 'is'):
                vars_found.append(token)
        return list(dict.fromkeys(vars_found))

    def _extract_globals(self, tree, source_bytes: bytes, filepath: str,
                         source_root: str) -> dict:
        """Extract global definitions (enums, constants, typedefs, global vars)."""
        rel_path = os.path.relpath(filepath, source_root)
        enums = []
        constants = []
        typedefs = []
        global_vars = []

        # Use iterative traversal instead of recursion to avoid RecursionError
        # on deeply nested AST trees (e.g., Linux kernel macros/headers)
        _SKIP_CHILDREN = frozenset({
            'function_definition', 'method_declaration',
            'function_declaration', 'function_item',
            'constructor_definition', 'destructor_definition',
        })
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type in ('enum_specifier', 'enum_item', 'enum_declaration'):
                self._extract_enum(node, source_bytes, rel_path, enums)
            elif node.type == 'class_definition':
                for child in node.children:
                    if child.type == 'argument_list':
                        arg_text = self._node_text(child, source_bytes)
                        if 'Enum' in arg_text or 'enum' in arg_text.lower():
                            self._extract_py_enum(node, source_bytes, rel_path, enums)
            elif node.type in ('preproc_def', 'preproc_function_def'):
                text = self._node_text(node, source_bytes)
                m = _GLOBAL_DEFINE_RE.match(text)
                if m and not m.group(1).startswith('_'):
                    pos = self._node_position(node)
                    constants.append({"name": m.group(1), "value_snippet": m.group(2).strip(),
                                      "source_file": rel_path, "line": pos["line"],
                                      "column": pos["column"], "start_byte": pos["start_byte"],
                                      "end_byte": pos["end_byte"]})
            elif node.type in ('type_definition', 'declaration'):
                text = self._node_text(node, source_bytes)
                if 'typedef' in text or 'type' in node.type:
                    m = _GLOBAL_TYPEDEF_RE.match(text)
                    if m:
                        pos = self._node_position(node)
                        typedefs.append({"name": m.group(1), "underlying_type": "",
                                        "source_file": rel_path, "line": pos["line"],
                                        "column": pos["column"], "start_byte": pos["start_byte"],
                                        "end_byte": pos["end_byte"]})
            if node.parent and node.parent.type == 'translation_unit':
                if node.type in ('declaration', 'variable_declaration', 'global_variable_declaration'):
                    text = self._node_text(node, source_bytes)
                    m = _GLOBAL_VAR_RE.match(text)
                    if m and m.group(2) not in ('main',):
                        pos = self._node_position(node)
                        global_vars.append({"name": m.group(2), "type": m.group(1).strip(),
                                           "value_snippet": m.group(3) or "", "source_file": rel_path,
                                           "line": pos["line"], "column": pos["column"],
                                           "start_byte": pos["start_byte"], "end_byte": pos["end_byte"]})
            # Push children in reverse order so they are processed left-to-right
            for child in reversed(node.children):
                if child.type not in _SKIP_CHILDREN:
                    stack.append(child)

        return {"enums": enums, "constants": constants, "typedefs": typedefs, "global_vars": global_vars}

    def _extract_enum(self, node, source_bytes, rel_path, enums):
        """Extract enum members from C/C++/Java/Rust enum node."""
        name = ""
        for child in node.children:
            if child.type in ('identifier', 'type_identifier', 'name'):
                name = self._node_text(child, source_bytes)
                break
        values = []
        for child in node.children:
            if child.type in ('enumerator_list', 'enum_body', 'body'):
                for item in child.children:
                    if item.type in ('enumerator', 'enum_constant', 'constant_item'):
                        item_text = self._node_text(item, source_bytes)
                        parts = item_text.split('=', 1)
                        member = parts[0].strip().rstrip(',')
                        val = parts[1].strip().rstrip(',') if len(parts) > 1 else ""
                        if member:
                            values.append({"member": member, "value": val})
            if child.type == 'class_body':
                for item in child.children:
                    if item.type == 'enum_constant':
                        member = self._node_text(item, source_bytes).split('(')[0].strip()
                        values.append({"member": member, "value": ""})
        if name or values:
            pos = self._node_position(node)
            enums.append({"name": name, "values": values,
                          "source_file": rel_path, "line": pos["line"],
                          "column": pos["column"], "start_byte": pos["start_byte"],
                          "end_byte": pos["end_byte"]})

    def _extract_py_enum(self, node, source_bytes, rel_path, enums):
        """Extract Python enum class members."""
        name = ""
        for child in node.children:
            if child.type == 'identifier':
                name = self._node_text(child, source_bytes)
                break
        values = []
        for child in node.children:
            if child.type == 'block':
                for item in child.children:
                    if item.type == 'expression_statement':
                        text = self._node_text(item, source_bytes)
                        m = re.match(r'(\w+)\s*=\s*(.*)', text.strip())
                        if m:
                            values.append({"member": m.group(1), "value": m.group(2).strip()})
        if name or values:
            pos = self._node_position(node)
            enums.append({"name": name, "values": values,
                          "source_file": rel_path, "line": pos["line"],
                          "column": pos["column"], "start_byte": pos["start_byte"],
                          "end_byte": pos["end_byte"]})
