"""Regex-based C/C++ scanner fallback when tree-sitter is not available.

Provides a basic but functional scanner that extracts:
- Function definitions (name, signature, source_file, line)
- Function calls (caller → callee)
- #ifdef/#if conditions
- Callback registrations

Not as accurate as tree-sitter AST parsing, but works without any C extensions.
"""

import os
import re
import sys
from collections import Counter, defaultdict


# Pattern 1: Full function definition on one line: "type func_name(args) {"
_FUNC_DEF_INLINE_RE = re.compile(
    r'^((?:(?:static|inline|extern|const|unsigned|signed|long|short|struct|enum|union|void|int|char|float|double)\s+)*\w[\w\s\*]*?)\s+'
    r'(\w+)\s*\(([^)]*)\)\s*\{',
)

# Pattern 2: Function name+args on its own line (return type on previous line)
# e.g., "module_get_opts(struct module_opts *opts, size_t opts_size)"
_FUNC_NAME_ARGS_RE = re.compile(
    r'^(\w+)\s*\(([^)]*)\)\s*$',
)
# Pattern 2b: Function name + start of multi-line args (return type on prev line)
# e.g., "device_cmd_read(struct device_ns *ns, ..."
_FUNC_NAME_ARGS_OPEN_RE = re.compile(
    r'^(\w+)\s*\(([^)]*)$',
)

# Pattern for function pointer args: func_name(complex_args) with nested parens
# Used by Strategy 6 as fallback when other strategies fail
_FUNC_NAME_OPEN_PAREN_RE = re.compile(
    r'^(\w+)\s*\(',
)

# Pattern 3: type + name(args) on one line without {
_FUNC_DEF_NOBRACE_RE = re.compile(
    r'^((?:(?:static|inline|extern|const|unsigned|signed|long|short|struct|enum|union|void|int|char|float|double)\s+)*\w[\w\s\*]*?)\s+'
    r'(\w+)\s*\(([^)]*)\)',
)

# Function call pattern: func_name(
_CALL_RE = re.compile(r'\b(\w+)\s*\(')

# Strategy 4: func_name(args) { on one line (return type on previous line)
# e.g., "driver_fail_request(struct device_ctrlr *ctrlr) {"
# This handles the common C pattern where return type is on a separate line above
_FUNC_NAME_ARGS_BRACE_RE = re.compile(
    r'^(\w+)\s*\(([^)]*)\)\s*\{',
)

# Strategy 5: type func_name(open_args on one line, multi-line args, then brace)
# e.g., "int driver_unmap_blocks(struct device_ns *ns, ..., void *driver_ctx,"
# e.g., "struct driver_io *driver_io_update_args(struct driver_io *bio, ..., int iovcnt,"
# followed by continuation args and then "{"
# Uses two-step parsing: first match type+name+open_args, then split type from name
_FUNC_DEF_MULTILINE_ARGS_RE = re.compile(
    r'^(.*?)\(([^)]*)$',
)

# #ifdef/#if/#ifndef pattern
_PPCOND_RE = re.compile(r'^\s*#\s*(ifdef|ifndef|if|elif|else|endif)\b[^\S\n]*(.*?)(?:\s*//.*)?$', re.MULTILINE)

# Universal skip set: C keywords, C stdlib, POSIX, compiler builtins, SIMD intrinsics.
# These apply to ANY C project and contain no project-specific entries.
# The project-specific entries are in _SKIP_NAMES below (migrated to profiles).
_UNIVERSAL_SKIP_NAMES = frozenset({'ARRAY_SIZE', 'LLVMFuzzerRunDriver', 'MAX', 'MIN', 'NULL', '_Static_assert', '__attribute__', '__atomic_add_fetch', '__atomic_clear', '__atomic_compare_exchange_n', '__atomic_exchange_n', '__atomic_fetch_add', '__atomic_fetch_sub', '__atomic_load_n', '__atomic_store_n', '__atomic_sub_fetch', '__atomic_test_and_set', '__builtin_clz', '__builtin_clzll', '__builtin_ctzl', '__builtin_expect', '__builtin_ffs', '__builtin_popcountl', '__builtin_prefetch', '__builtin_unreachable', '__crc32b', '__crc32d', '__format__', '__itt_domain_create', '__itt_init_ittlib', '__itt_metadata_add', '__itt_string_handle_create', '__sync_add_and_fetch', '__sync_and_and_fetch', '__sync_bool_compare_and_swap', '__sync_fetch_and_add', '__sync_fetch_and_sub', '__sync_or_and_fetch', '__sync_sub_and_fetch', '__sync_val_compare_and_swap', '_mm_load_si128', '_mm_stream_si128', 'abort', 'abs', 'accept', 'asctime_r', 'asprintf', 'assert', 'atexit', 'atof', 'atoi', 'atol', 'atomic_add', 'atomic_cmpxchg', 'atomic_dec', 'atomic_inc', 'atomic_read', 'atomic_set', 'atomic_sub', 'auto', 'backtrace_symbols', 'bind', 'break', 'bzero', 'calloc', 'case', 'ceil', 'char', 'chmod', 'chown', 'clearerr', 'clock_gettime', 'close', 'closedir', 'connect', 'const', 'container_of', 'continue', 'ctime', 'ctime_r', 'daemon', 'default', 'defined', 'difftime', 'do', 'double', 'else', 'enum', 'epoll_create', 'epoll_create1', 'epoll_ctl', 'epoll_wait', 'eventfd_write', 'exit', 'extern', 'false', 'fclose', 'fdopen', 'feof', 'ferror', 'fflush', 'fgetpos', 'fgets', 'fileno', 'float', 'flock', 'floor', 'fnmatch', 'fopen', 'for', 'fprintf', 'fputs', 'fread', 'free', 'freeaddrinfo', 'freeifaddrs', 'freopen', 'fseek', 'fsetpos', 'ftell', 'ftruncate', 'fwrite', 'gai_strerror', 'getaddrinfo', 'getchar', 'getcwd', 'getenv', 'gethostbyname', 'getifaddrs', 'getline', 'getopt', 'getpeername', 'getpid', 'getrusage', 'getsockname', 'getsockopt', 'gettimeofday', 'gmtime', 'gmtime_r', 'goto', 'htonl', 'htons', 'if', 'inet_ntop', 'inet_pton', 'inline', 'int', 'int16_t', 'int32_t', 'int64_t', 'int8_t', 'ioctl', 'isalnum', 'isalpha', 'isblank', 'isdigit', 'isgraph', 'isprint', 'isspace', 'isupper', 'isxdigit', 'kill', 'likely', 'listen', 'localtime', 'localtime_r', 'long', 'lseek', 'malloc', 'memchr', 'memcmp', 'memcpy', 'memcpy_s', 'memmove', 'memset', 'min', 'mmap', 'mprotect', 'munmap', 'nanosleep', 'ntohl', 'ntohs', 'offsetof', 'open', 'opendir', 'pclose', 'perror', 'poll', 'popen', 'posix_memalign', 'printf', 'pthread_barrier_destroy', 'pthread_barrier_init', 'pthread_barrier_wait', 'pthread_cancel', 'pthread_cond_broadcast', 'pthread_cond_destroy', 'pthread_cond_init', 'pthread_cond_signal', 'pthread_cond_timedwait', 'pthread_cond_wait', 'pthread_condattr_destroy', 'pthread_condattr_init', 'pthread_condattr_setclock', 'pthread_create', 'pthread_detach', 'pthread_exit', 'pthread_join', 'pthread_kill', 'pthread_mutex_consistent', 'pthread_mutex_destroy', 'pthread_mutex_init', 'pthread_mutex_lock', 'pthread_mutex_trylock', 'pthread_mutex_unlock', 'pthread_mutexattr_destroy', 'pthread_mutexattr_init', 'pthread_mutexattr_setpshared', 'pthread_mutexattr_setrobust', 'pthread_mutexattr_settype', 'pthread_rwlock_destroy', 'pthread_rwlock_init', 'pthread_rwlock_rdlock', 'pthread_rwlock_tryrdlock', 'pthread_rwlock_trywrlock', 'pthread_rwlock_unlock', 'pthread_rwlock_wrlock', 'pthread_self', 'pthread_setcancelstate', 'pthread_sigmask', 'pthread_spin_destroy', 'pthread_spin_init', 'pthread_spin_lock', 'pthread_spin_trylock', 'pthread_spin_unlock', 'putchar', 'puts', 'qsort', 'raise', 'rand', 'rand_r', 'read', 'readdir', 'readlink', 'realloc', 'realpath', 'recv', 'recvfrom', 'register', 'remove', 'rename', 'return', 'rewind', 'sched_getcpu', 'sched_setaffinity', 'scanf', 'select', 'send', 'sendto', 'setrlimit', 'setsockopt', 'setvbuf', 'short', 'shutdown', 'sigaction', 'sigaddset', 'sigemptyset', 'sigfillset', 'signal', 'signed', 'sizeof', 'sleep', 'snprintf', 'snprintf_s', 'socket', 'socketpair', 'sprintf', 'sprintf_s', 'srand', 'srandom', 'sscanf', 'static', 'strcasecmp', 'strcasestr', 'strcat_s', 'strchr', 'strcmp', 'strcpy', 'strcpy_s', 'strcspn', 'strdup', 'strerror', 'strftime', 'strlen', 'strncasecmp', 'strncat_s', 'strncmp', 'strncpy', 'strndup', 'strpbrk', 'strrchr', 'strsep', 'strspn', 'strstr', 'strtod', 'strtok_r', 'strtol', 'strtoul', 'strtoull', 'struct', 'switch', 'syslog', 'tcgetattr', 'tcsetattr', 'tmpfile', 'tmpnam', 'tolower', 'toupper', 'true', 'ttyname', 'typedef', 'typeof', 'uint16_t', 'uint32_t', 'uint64_t', 'uint8_t', 'umask', 'ungetc', 'union', 'unlikely', 'unsigned', 'usleep', 'uuid_clear', 'uuid_compare', 'uuid_copy', 'uuid_generate', 'uuid_generate_sha1', 'uuid_is_null', 'uuid_parse', 'uuid_unparse', 'uuid_unparse_lower', 'va_arg', 'va_end', 'va_start', 'vfprintf', 'void', 'volatile', 'vprintf', 'vsnprintf', 'vsprintf', 'vsscanf', 'vsyslog', 'waitpid', 'while', 'write', 'xstrdup'})

# Legacy _SKIP_NAMES: now an alias for _UNIVERSAL_SKIP_NAMES.
# When profile is provided via --profile, the effective skip set is built from
# _UNIVERSAL_SKIP_NAMES + profile.skip_names.add - visible external prefixes.
# When no profile is provided (backward-compatible mode), _SKIP_NAMES is used
# as-is.  Project-specific entries have been migrated to config/profiles/*.json.
_SKIP_NAMES = _UNIVERSAL_SKIP_NAMES


# C types that appear in function signatures
_C_TYPES = frozenset({
    'void', 'int', 'char', 'long', 'short', 'unsigned', 'signed', 'float',
    'double', 'const', 'volatile', 'static', 'extern', 'inline', 'struct',
    'enum', 'union', 'bool', 'size_t', 'ssize_t', 'uint8_t', 'uint16_t',
    'uint32_t', 'uint64_t', 'int8_t', 'int16_t', 'int32_t', 'int64_t',
})

# Type keywords used for distinguishing function definitions from expressions
_TYPE_KEYWORDS = {'static', 'inline', 'extern', 'const', 'unsigned', 'signed',
                  'long', 'short', 'struct', 'enum', 'union', 'void', 'int', 'char',
                  'float', 'double', 'bool', 'size_t', 'ssize_t', 'pid_t',
                  # stdint.h exact-width types (commonly used in C projects)
                  'int8_t', 'int16_t', 'int32_t', 'int64_t',
                  'uint8_t', 'uint16_t', 'uint32_t', 'uint64_t',
                  'intptr_t', 'uintptr_t', 'ptrdiff_t',
                  '_Bool', '_Atomic'}

# #include pattern for IMPORTS edges
_INCLUDE_RE = re.compile(
    r'^\s*#\s*include\s+[<"](.*?)[>"]',
    re.MULTILINE,
)

# DEFINE_STUB / DEFINE_STUB_V / DEFINE_RETURN_MOCK macro pattern — extract stub/mock function names
_DEFINE_STUB_RE = re.compile(r'DEFINE_(?:STUB(?:_V)?|RETURN_MOCK)\s*\(\s*(\w+)')

# Function-like macro definitions: #define MACRO(args) ...
# These expand inline and should not be treated as real function callees.
# Pattern matches: #define name( immediately followed by ( (not a space),
# which distinguishes function-like macros from object-like macros.
_FUNC_MACRO_DEF_RE = re.compile(r'^\s*#\s*define\s+(\w+)\(', re.MULTILINE)

# Project-wide function-like macro name cache.
# As files are scanned, function-like macros (#define NAME(args)) are collected
# here. When later files call these macros, they are recognized and skipped
# as callees (since macros expand inline, they are not real function calls).
# This is especially important for headers that define inline-like macros
# (e.g., ftl_bug()) that are then called from .c files.
_PROJECT_FUNC_MACROS = set()

# Module-level set of known registration function names (populated from profile
# during scan_c_file). Used by _detect_cross_file_callbacks to identify which
# function calls accept callback arguments, so it can properly annotate
# concurrency and avoid false positive CALLBACK_ARG edges for test frameworks
# and synchronous function pointer passing.
_known_reg_funcs = set()


# Vtable / function pointer table patterns
# Detect struct initializer blocks that register function pointer implementations.
# Pattern: static const struct some_fn_table name = {.field = func, ...}
# Also handles: static struct some_fn_table name = { .field = func, ... }
# Also handles: static struct some_fn_table name[] = { [IDX] = {.field = func}, ... }
_VTABLE_STRUCT_RE = re.compile(
    r'(?:static\s+)?(?:const\s+)?struct\s+(\w+)\s+(\w+)\s*(?:\[[^\]]*\])?\s*=\s*\{',
    re.MULTILINE,
)
# Extract .field_name = func_name from struct initializer body
_VTABLE_FIELD_RE = re.compile(
    r'\.\s*(\w+)\s*=\s*([a-zA-Z_]\w*)',
)
# Detect function pointer calls via struct field access: ptr->table->field(args)
# e.g., dev->ops->submit_request(ioch, dev_io)
# IMPORTANT: Must correctly handle multi-level arrow chains like a->b->method()
# where field_name (group 2) is the LAST segment (the actual method name)
# and struct_chain (group 1) is everything before the last ->
_FN_PTR_CALL_RE = re.compile(
    r'(\w+(?:->\w+)*)\s*->\s*(\w+)\s*\(',
)
# Variant: pointer->struct.field() — common in C vtable/ops dispatch
# e.g., transport->ops.ctrlr_construct(trid, opts, devhandle)
_FN_PTR_CALL_DOT_RE = re.compile(
    r'(?:\w+->)*(\w+)->(\w+)\.(\w+)\s*\(',
)

# Concurrency patterns: detect thread creation and callback registration
# Pattern 1: pthread_create(&tid, NULL, thread_func, arg) → spawn_target
# Generic POSIX: works for any project using pthreads
_PTHREAD_CREATE_RE = re.compile(
    r'pthread_create\s*\(\s*[^,]*,\s*[^,]*,\s*(\w+)',
)
# Framework-specific callback patterns are now loaded from project profiles
# (see config/profiles/*.json). When profile is provided, the scanner uses
# profile.callback_patterns instead of hardcoded patterns.
# Generic: any function call where 2nd arg name ends with _cb, _fn, _handler
_GENERIC_CB_ARG_RE = re.compile(
    r'(\w+)\s*\([^)]*\b(\w+(?:_cb|_fn|_handler|_callback))\b',
)

# Pre-compiled patterns for _strip_comments / _strip_comments_only.
# Previously these were `re.sub(r'...', ...)` string-pattern calls inside
# per-function / per-line loops, causing ~300M recompiles on kernel scans.
_STRIP_BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)
_STRIP_LINE_COMMENT_RE = re.compile(r'//.*$', re.MULTILINE)
_STRIP_STRING_LIT_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"')
_STRIP_PP_DIRECTIVE_RE = re.compile(
    r'^\s*#\s*(if|ifdef|ifndef|elif|else|endif|define|include)\b.*$',
    re.MULTILINE,
)
# Bare identifier check — was `_BARE_IDENT_RE.match(arg)` at 10+ sites.
_BARE_IDENT_RE = re.compile(r'^[a-zA-Z_]\w*$')

# Hoisted line-parsing patterns for _extract_func_defs. These were inline
# re.match/re.search string-pattern calls executed once per source line;
# moving them to module level avoids per-line regex cache churn.
_TYPE_PREFIX_RE = re.compile(
    r'^(?:(?:static|inline|extern|const|unsigned|signed|long|short|'
    r'struct|enum|union|void|int|char|float|double)\s+)'
)
_PAREN_BRACE_RE = re.compile(r'\([^)]*\)\s*\{')
_BEFORE_PAREN_ARGS_BRACE_RE = re.compile(r'^(.*?)\(([^)]*)\)\s*\{')
_TRAILING_IDENT_RE = re.compile(r'(\w+)\s*$')
_PTR_RET_FUNC_NOBRACE_RE = re.compile(
    r'^((?:static|inline|extern|const|unsigned|signed|long|short|'
    r'struct|enum|union|void|int|char|float|double)\s+[\w\s\*]+?)'
    r'\s*\*?\s*(\w+)\s*\(([^)]*)\)\s*$'
)
# Preprocessor conditional markers used by _extract_func_defs and
# _extract_func_bodies to track #ifdef branch depth while scanning for
# the opening brace.
_PP_IF_RE = re.compile(r'^\s*#\s*(if|ifdef|ifndef)\b')
_PP_ELSE_RE = re.compile(r'^\s*#\s*(elif|else)\b')
_PP_ENDIF_RE = re.compile(r'^\s*#\s*endif\b')

# Generic local-variable declaration pattern. Captures the declared name
# so the per-argument callback scan can filter by identifier equality
# (m.group(1) == arg) instead of compiling a re.escape(arg) alternation
# regex for every argument of every function call.
_LOCAL_VAR_TYPE_RE = re.compile(
    r'\b(?:int|long|short|unsigned|char|void|float|double|bool|size_t|'
    r'uint\d+_t|int\d+_t|struct\s+\w+|enum\s+\w+|\w+_t|const\s+\w+)'
    r'\s+(\w+)\s*[=;]'
)

# Generic names that are too ambiguous as callback arguments — they're
# common local variable/parameter names (like 'init', 'ctx', 'data')
# and create false positive edges when they coincidentally match a
# function name defined elsewhere in the project.
_CALLBACK_ARG_GENERIC_NAMES = frozenset({
    'init', 'fini', 'start', 'stop', 'main', 'usage', 'help',
    'close', 'open', 'read', 'write', 'process', 'handle', 'check',
    'run', 'test', 'setup', 'cleanup', 'destroy', 'create',
    'alloc', 'free', 'done', 'complete', 'data', 'ctx', 'arg',
    'buf', 'ptr', 'len', 'size', 'count', 'name', 'type', 'val',
    'result', 'status', 'error', 'rc', 'ret', 'out', 'in',
    'channel', 'io', 'req', 'resp', 'cmd', 'msg', 'event',
    # Additional common variable names that coincidentally match function names
    'feature_id', 'entry', 'callback', 'handler', 'func', 'method',
    'callback_func', 'handler_func', 'notify', 'signal', 'action',
    'func_ptr', 'fn_ptr', 'cb_func', 'cb_handler', 'cb_fn',
    'instance', 'object', 'impl', 'driver', 'device', 'module',
    'port', 'queue', 'ring', 'pool', 'iter', 'iterator',
})

# Functions/macros whose arguments should NEVER be treated as callback targets.
# These are utility macros that take struct field names or type names as arguments,
# not callback registration functions.
_CALLBACK_ARG_SKIP_CALLEES = frozenset({
    # Generic container_of macros (universal across C projects)
    'container_of', 'CONTAINER_OF', '__containerof', 'offsetof',
    # Test framework registration (not call-graph callbacks)
    'CU_ADD_TEST', 'CU_add_test', 'CU_add_suite',
    'TEST', 'CU_ASSERT',
    # Mock/test framework macros
    'MOCK_SET', 'MOCK_CLEAR', 'MOCK_CLEAR_P',
    # Field assignment macro
    'SET_FIELD',
    # Standard library functions that take data args, not callbacks
    'memcpy', 'memset', 'memmove', 'memcmp',
    'strcpy', 'strncpy', 'strcat', 'strncat', 'strcmp', 'strncmp',
    'sprintf', 'snprintf', 'fprintf', 'printf',
    'calloc', 'malloc', 'realloc', 'free',
    'open', 'close', 'read', 'write', 'ioctl',
})

# Common variable names that registration function regexes may incorrectly
# capture as callback targets (e.g., ctx->cb_fn captures "ctx" when regex
# is (\w+) instead of (?:\w+->)?(\w+)).
_NON_FUNC_CB_TARGETS = frozenset({
    'ctx', 'arg', 'cb_arg', 'data', 'buf', 'ptr', 'tmp',
    'ret', 'result', 'param', 'params', 'target', 'handler',
    'self', 'this', 'obj', 'object', 'user_data', 'priv',
})


def scan_c_file(filepath: str, source_root: str = "", api_prefixes: list = None,
                profile: dict = None) -> dict:
    """Scan a single C/C++ file using regex patterns.

    Args:
        filepath: Path to the C/C++ file to scan.
        source_root: Root directory for computing relative paths.
        api_prefixes: List of public API name prefixes (deprecated: use profile).
        profile: Scanner config dict from ProfileSchema.to_scanner_config().
                 When provided, overrides hardcoded skip names and callback patterns.
    """
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            source = f.read()
    except (IOError, OSError):
        return {"functions": [], "edges": []}

    rel_path = os.path.relpath(filepath, source_root) if source_root else filepath
    functions = []
    edges = []
    import_edges = []
    func_names = set()

    # Extract #include directives for IMPORTS edges
    for m in _INCLUDE_RE.finditer(source):
        include_path = m.group(1)
        import_edges.append({
            "source": rel_path,
            "target": include_path,
            "relation": "IMPORTS",
        })

    # Extract #ifdef stack for condition tracking
    pp_conditions = _extract_pp_conditions(source)

    # Extract DEFINE_STUB/DEFINE_STUB_V names — these are test mock functions
    # that should not be treated as real callees
    stub_names = set()
    for m in _DEFINE_STUB_RE.finditer(source):
        stub_names.add(m.group(1))

    # Extract function-like macro names (#define MACRO(args) ...)
    # These expand inline and should not create CALLS edges
    macro_names = set()
    for m in _FUNC_MACRO_DEF_RE.finditer(source):
        mname = m.group(1)
        macro_names.add(mname)
        # NOTE: Do NOT add to _PROJECT_FUNC_MACROS here!
        # File-local macros (especially test mocks like
        # #define proj_put_channel(ch) ut_put_channel(ch))
        # must only affect THIS file, not leak to all subsequent files.
        # _PROJECT_FUNC_MACROS is populated exclusively by the
        # header pre-scan step in scan_directory().

    # Merge api_prefixes from profile if provided
    effective_api_prefixes = api_prefixes
    if profile and profile.get("api_prefixes"):
        extra = profile["api_prefixes"]
        if api_prefixes:
            effective_api_prefixes = list(set(api_prefixes) | set(extra))
        else:
            effective_api_prefixes = extra

    # Extract export macro usages (e.g., EXPORT_SYMBOL(func_name))
    # Functions wrapped in export macros are public API entries regardless of
    # naming convention or header path — the macro is the authoritative signal.
    _exported_names = set()
    _export_macros = profile.get("export_macros", []) if profile else []
    if _export_macros:
        _EXPORT_MACRO_RE = re.compile(
            r'(?:' + '|'.join(re.escape(m) for m in _export_macros) + r')\s*\(\s*(\w+)\s*\)'
        )
        for m in _EXPORT_MACRO_RE.finditer(source):
            _exported_names.add(m.group(1))

    # Extract function definitions
    # Build skip_names_add from profile for function definition extraction
    _skip_names_add = list(profile.get("skip_names_add", [])) if profile else []
    # In test directories, external library function names are always mock/stub
    # implementations — skip extracting them to avoid false edges from production
    # code resolving to test mocks instead of the real external library.
    _skip_prefixes = []
    is_test_file = '/test/' in f'/{rel_path}/' or rel_path.startswith('test/')
    if is_test_file and profile:
        _skip_prefixes = (profile.get("visible_external_prefixes", []) +
                          profile.get("silent_skip_prefixes", []))
    func_defs = _extract_func_defs(source, rel_path, api_prefixes=effective_api_prefixes,
                                   skip_names_add=_skip_names_add,
                                   skip_prefixes=_skip_prefixes)
    for fdef in func_defs:
        func_names.add(fdef["name"])
        # Mark functions wrapped in export macros as API_entry
        if _exported_names and fdef["name"] in _exported_names:
            if "API_entry" not in fdef.get("labels", []):
                fdef.setdefault("labels", []).append("API_entry")
        functions.append(fdef)

    # Build effective skip set
    if profile:
        # Profile-driven: universal + profile additions - visible externals
        base_skip = _UNIVERSAL_SKIP_NAMES | set(profile.get("skip_names_add", []))
        visible_prefixes = profile.get("visible_external_prefixes", [])
        for prefix in visible_prefixes:
            # Case-insensitive matching ONLY for ALL_UPPERCASE prefixes
            # (like SSL_ vs ssl_). Mixed-case prefixes like 'Proj'
            # (C++ namespace) must remain case-sensitive to avoid
            # matching 'proj_' (C function prefix).
            if prefix.isupper() or (prefix.endswith('_') and prefix[:-1].isupper()):
                prefix_lower = prefix.lower()
                base_skip = {n for n in base_skip if not n.lower().startswith(prefix_lower)}
            else:
                base_skip = {n for n in base_skip if not n.startswith(prefix)}
        effective_skip = frozenset(base_skip) | stub_names | macro_names | _PROJECT_FUNC_MACROS
    else:
        # Legacy: use hardcoded _SKIP_NAMES
        effective_skip = _SKIP_NAMES | stub_names | macro_names | _PROJECT_FUNC_MACROS

    # Build compiled callback patterns from profile or use hardcoded
    if profile:
        _profile_cb_patterns = []
        for pat in profile.get("callback_patterns", []):
            _profile_cb_patterns.append({
                "register_func": pat["register_func"],
                "regex": re.compile(pat["regex"]),
                "cb_arg_index": pat.get("cb_arg_index", 1),
                "concurrency_type": pat["concurrency_type"],
            })
        _profile_generic_suffixes = profile.get("generic_cb_suffixes", ["_cb", "_fn", "_handler", "_callback"])
        # Build set of known registration function names for CALLBACK_ARG
        # concurrency annotation — only registration funcs get concurrency=callback.
        # Also update module-level set so _detect_cross_file_callbacks can use it.
        _known_reg_funcs_local = {pat["register_func"] for pat in _profile_cb_patterns}
        _known_reg_funcs.clear()
        _known_reg_funcs.update(_known_reg_funcs_local)
    else:
        _profile_cb_patterns = None  # Use hardcoded patterns
        _profile_generic_suffixes = None
        _known_reg_funcs_local = {"pthread_create"}  # Legacy registration function
        _known_reg_funcs.clear()
        _known_reg_funcs.update(_known_reg_funcs_local)

    # Load skip_call_prefixes: callees matching these prefixes are suppressed
    # (no CALLS edge created). Used for tracepoint calls (trace_*) etc.
    _skip_call_prefixes = profile.get("skip_call_prefixes", []) if profile else []

    # Load skip_callees: specific callee names that should not be treated as
    # callback targets (e.g., container_of macros, token-paste macros).
    # These come from profile's callback_detection.skip_callees field.
    _profile_skip_callees = set(profile.get("skip_callees", [])) if profile else set()
    _effective_skip_callees = _CALLBACK_ARG_SKIP_CALLEES | _profile_skip_callees

    # Build map of function-pointer parameter names per function.
    # A function-pointer parameter has a type ending in _fn, _cb, _handler,
    # _callback, or uses function pointer syntax (*param_name).
    # These are "passthrough callback" parameters — the function receives a
    # callback and forwards it to a registration API or assigns it to a struct field.
    _FP_PARAM_RE = re.compile(
        r'(?:\w+::)*(\w+_fn|\w+_cb|\w+_handler|\w+_callback)\s+(\w+)'  # typedef'd form: type_cb param_name
        r'|\(\s*\*\s*(\w+)\s*\)'  # function pointer param form: (*param_name)
    )

    def _split_outer_params(sig: str) -> list:
        """Split a function signature into top-level parameters, respecting
        nested parentheses (e.g. function pointer types like
        'void (*cb_fn)(void *ctx, int status)')."""
        # Find the outermost parameter list: first '(' to its matching ')'
        depth = 0
        start = -1
        for i, ch in enumerate(sig):
            if ch == '(':
                if depth == 0:
                    start = i + 1
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    # Extract content between first '(' and its matching ')'
                    param_str = sig[start:i]
                    # Split at commas only at depth 0
                    parts = []
                    current = []
                    d = 0
                    for c in param_str:
                        if c == '(' : d += 1
                        elif c == ')': d -= 1
                        if c == ',' and d == 0:
                            parts.append(''.join(current))
                            current = []
                        else:
                            current.append(c)
                    if current:
                        parts.append(''.join(current))
                    return parts
        return []

    _fp_params_by_func = {}  # func_name -> {param_name: arg_index}
    for fdef in func_defs:
        sig = fdef.get("signature", "")
        # Extract all parameter names with their indices from the signature
        # Balanced-paren parsing instead of sig.split('(')[-1]: the latter
        # breaks on function-pointer params like void (*cb_fn)(void *ctx, int status)
        sig_params = _split_outer_params(sig)
        fp_params = _FP_PARAM_RE.findall(sig)
        if fp_params:
            # Map fp param names to their 0-based index in the function's own params
            param_info = {}
            for match in fp_params:
                # match is a tuple: (type_name, param_name, fp_param_name)
                # Either group 2 (typedef'd form) or group 3 (function pointer form) has the name
                pname = match[1] if match[1] else match[2]
                if not pname:
                    continue
                for idx, sig_param in enumerate(sig_params):
                    if pname in sig_param:
                        param_info[pname] = idx
                        break
            if param_info:
                _fp_params_by_func[fdef["name"]] = param_info

    # Extract function calls from each function body
    func_bodies = _extract_func_bodies(source, func_defs)

    # Mark functions without bodies as empty (declarations, not definitions)
    body_func_names = set(func_bodies.keys())
    for fdef in func_defs:
        if fdef["name"] not in body_func_names:
            fdef["is_empty"] = True

    # Pre-compute C++ file flag for fn_ptr_call filtering.
    # obj->method() in C++ is a direct call, not a fn_ptr dispatch.
    _is_cpp = rel_path.lower().endswith(('.cpp', '.cc', '.cxx', '.c++', '.h++', '.hpp', '.hxx'))

    # Hoist the generic-callback-suffix regex compile OUT of the per-function
    # loop. The suffixes don't change per function — they're either from the
    # profile or the default list.
    _active_generic_suffixes = _profile_generic_suffixes if _profile_generic_suffixes is not None else ["_cb", "_fn", "_handler", "_callback"]
    _generic_cb_re = None
    if _active_generic_suffixes:
        suffix_pattern = r'(\w+)\s*\([^)]*\b(\w+(?:' + '|'.join(re.escape(s) for s in _active_generic_suffixes) + r'))\b'
        _generic_cb_re = re.compile(suffix_pattern)

    for caller_name, body_info in func_bodies.items():
        body = body_info["body"]
        body_source_offset = body_info["source_offset"]
        body_start_line = body_info.get("start_line", 0)
        invoker_id = _make_func_id(caller_name, rel_path)

        # Detect concurrency patterns in this body
        seen_edges = set()  # R39: deduplicate edges within function body
        spawn_targets = {}  # invoked_nameb → concurrency type
        reg_func_concurrency = {}  # registration_func_name → concurrency type
        # (When a profile callback pattern matches, we record BOTH the callback
        # name AND the registration function name, so that the main loop can
        # annotate the direct edge to the registration function with concurrency.)

        if _profile_cb_patterns is not None:
            # Profile-driven: iterate over configured callback patterns
            for pat in _profile_cb_patterns:
                for sm in pat["regex"].finditer(body):
                    cb_name = sm.group(1)
                    if cb_name and cb_name not in effective_skip and len(cb_name) > 2:
                        spawn_targets[cb_name] = pat["concurrency_type"]
                    # Also record the registration function's concurrency type
                    # so the main loop can annotate the direct call edge.
                    reg_func_concurrency[pat["register_func"]] = pat["concurrency_type"]
        else:
            # Legacy: pthread_create only (SPDK patterns moved to profiles)
            for sm in _PTHREAD_CREATE_RE.finditer(body):
                spawn_callee = sm.group(1)
                if spawn_callee and spawn_callee not in effective_skip:
                    spawn_targets[spawn_callee] = "spawn_target"
            reg_func_concurrency["pthread_create"] = "spawn_target"

        # Generic callback suffix detection (always active). Uses the
        # pre-compiled _generic_cb_re from outside the per-function loop.
        if _generic_cb_re is not None:
            for sm in _generic_cb_re.finditer(body):
                cb_name = sm.group(2)
                if cb_name and cb_name not in effective_skip and len(cb_name) > 3:
                    if cb_name not in spawn_targets:
                        spawn_targets[cb_name] = "callback"

        call_order = 0
        for m in _CALL_RE.finditer(body):
            invoked_nameb = m.group(1)

            # Detect struct/pointer field calls: ->name(...) or .name(...)
            # These are function pointer calls, not direct function calls
            is_fn_ptr_call = False
            if m.start() >= 2 and body[m.start()-2:m.start()] == '->':
                is_fn_ptr_call = True
            elif m.start() >= 1 and body[m.start()-1] == '.':
                is_fn_ptr_call = True

            if is_fn_ptr_call:
                # Record as fn_ptr call (no specific target resolution)
                # Use _UNIVERSAL_SKIP_NAMES only, not full effective_skip:
                # skip_names.add contains struct field names (cb_fn, writev, etc.)
                # that are valid fn_ptr targets — they are struct field accesses,
                # not standalone function names.
                # Skip for C++ files: obj->method() in C++ is a direct call,
                # not a function pointer dispatch.
                if _is_cpp:
                    continue
                if invoked_nameb not in _UNIVERSAL_SKIP_NAMES and len(invoked_nameb) > 2:
                    edge_key = (invoker_id, invoked_nameb)
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        call_order += 1
                        edge = {
                            "source": invoker_id,
                            "target": invoked_nameb,
                            "call_order": call_order,
                            "call_condition": "",
                            "confidence": "FN_PTR",
                            "source_tag": "ast",
                            "concurrency": "fn_ptr",
                            "evidence": f"fn_ptr_call: {caller_name} -> {invoked_nameb} via ->.{invoked_nameb}()",
                            "_source_file": rel_path,
                        }
                        edges.append(edge)
                continue

            # Skip callees matching skip_call_prefixes (e.g., trace_* calls)
            if _skip_call_prefixes and any(invoked_nameb.startswith(p) for p in _skip_call_prefixes):
                continue

            if invoked_nameb in effective_skip or invoked_nameb == caller_name:
                # Even if skipped as callee, check if it's a concurrency API
                # and extract the callback/thread function as a separate edge
                conc_type = None
                cb_target = None
                is_registration_func = False

                if _profile_cb_patterns is not None:
                    # Profile-driven: match against configured patterns
                    for pat in _profile_cb_patterns:
                        if invoked_nameb == pat["register_func"]:
                            is_registration_func = True
                            pm = pat["regex"].search(body[m.start():m.start()+200])
                            if pm:
                                cb_target = pm.group(1)
                                conc_type = pat["concurrency_type"]
                            break
                else:
                    # Legacy: pthread_create only (SPDK patterns moved to profiles)
                    if invoked_nameb == 'pthread_create':
                        is_registration_func = True
                        pm = _PTHREAD_CREATE_RE.search(body[m.start():m.start()+200])
                        if pm:
                            cb_target = pm.group(1)
                            conc_type = "spawn_target"

                # Create edge to the callback function (if detected)
                # Filter out common variable names (uses module-level constant)
                # that registration function regexes may incorrectly capture
                if (cb_target and cb_target not in effective_skip
                        and len(cb_target) > 2 and cb_target not in _NON_FUNC_CB_TARGETS):
                    edge_key = (invoker_id, cb_target)
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        call_order += 1
                        cb_edge = {
                            "source": invoker_id,
                            "target": cb_target,
                            "call_order": call_order,
                            "call_condition": "",
                            "confidence": "EXTRACTED",
                            "source_tag": "ast",
                            "concurrency": conc_type,
                            "_source_file": rel_path,
                        }
                        if conc_type == "spawn_target":
                            cb_edge["evidence"] = f"spawn_target: {caller_name}() -> {cb_target}() (pthread_create)"
                        elif conc_type:
                            cb_edge["evidence"] = f"callback_arg: {invoked_nameb}() -> {cb_target}() ({conc_type})"
                        edges.append(cb_edge)

                # Create a direct edge to the registration function itself.
                # Registration functions (like proj_thread_send_msg) may be in
                # effective_skip because they're #defined as macros in headers
                # (added to _PROJECT_FUNC_MACROS by the header pre-scan). But for
                # invocation graph completeness, we still need the edge: caller → reg_func.
                # This shows HOW the callback is dispatched, not just THAT it is.
                # NOTE: Do NOT set concurrency on this edge — the registration
                # function itself is not a callback; only the callback argument
                # edge (created above) gets concurrency.
                if is_registration_func and invoked_nameb != caller_name and len(invoked_nameb) > 2:
                    edge_key = (invoker_id, invoked_nameb)
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        call_order += 1
                        reg_edge = {
                            "source": invoker_id,
                            "target": invoked_nameb,
                            "call_order": call_order,
                            "call_condition": "",
                            "confidence": "EXTRACTED",
                            "source_tag": "ast",
                            "_source_file": rel_path,
                        }
                        edges.append(reg_edge)

                continue
            if len(invoked_nameb) <= 2:
                continue
            # Skip ALL_CAPS names that aren't in the function registry — they are
            # almost certainly macros, not real function calls.
            # Exception: names containing lowercase are kept.
            if invoked_nameb.isupper() and len(invoked_nameb) > 3 and invoked_nameb not in func_names:
                continue

            condition = ""
            # Use line-number-based condition matching instead of byte offsets.
            # Byte offsets are unreliable because _strip_comments modifies the body
            # (removing comments, strings, pp directives), causing misalignment.
            # Count newlines before this match in the stripped body to get the
            # relative line number, then add body_start_line for absolute line.
            call_line_in_body = body[:m.start()].count('\n')
            call_line = body_start_line + call_line_in_body
            for cond_start_line, cond_end_line, cond_text in pp_conditions:
                if cond_start_line <= call_line <= cond_end_line:
                    condition = cond_text
                    break

            # Determine concurrency type
            # Only apply concurrency annotation to actual callback/spawn targets,
            # NOT to direct calls to registration functions. A call to
            # proj_for_each_channel() is a direct call, not a callback — the
            # callback is the function pointer ARGUMENT passed to it, which gets
            # its own separate edge with concurrency=callback above (line 500-510).
            concurrency = spawn_targets.get(invoked_nameb, "")
            if not concurrency and invoked_nameb not in reg_func_concurrency:
                # Not a spawn target and not a registration function — no concurrency
                pass
            # If callee IS a registration function, leave concurrency empty:
            # the registration function itself is not a callback; only the
            # callback argument edge (created above) gets concurrency.

            # When a known registration function is NOT in effective_skip, the
            # main loop handles it here. We still need to extract the callback
            # argument and create a separate edge for it, just like we do in the
            # effective_skip branch above.
            if invoked_nameb in _known_reg_funcs_local and invoked_nameb not in effective_skip:
                for pat in (_profile_cb_patterns or []):
                    if pat["register_func"] == invoked_nameb:
                        pm = pat["regex"].search(body[m.start():m.start()+200])
                        if pm:
                            cb_target = pm.group(1)
                            if (cb_target and cb_target not in effective_skip
                                    and len(cb_target) > 2 and cb_target != caller_name
                                    and cb_target not in _NON_FUNC_CB_TARGETS):
                                cb_edge_key = (invoker_id, cb_target)
                                if cb_edge_key not in seen_edges:
                                    seen_edges.add(cb_edge_key)
                                    call_order += 1
                                    edges.append({
                                        "source": invoker_id,
                                        "target": cb_target,
                                        "call_order": call_order,
                                        "call_condition": condition,
                                        "confidence": "EXTRACTED",
                                        "source_tag": "ast",
                                        "concurrency": pat["concurrency_type"],
                                        "evidence": f"callback_arg: {invoked_nameb}() arg#{pat.get('cb_arg_index', '?')}={cb_target}",
                                        "_source_file": rel_path,
                                    })
                        break

            # R39: Deduplicate edges within function body
            edge_key = (invoker_id, invoked_nameb)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)

            call_order += 1
            edge = {
                "source": invoker_id,
                "target": invoked_nameb,
                "call_order": call_order,
                "call_condition": condition,
                "confidence": "EXTRACTED",
                "source_tag": "ast",
                "_source_file": rel_path,
            }
            if concurrency:
                edge["concurrency"] = concurrency
                if concurrency == "spawn_target":
                    edge["evidence"] = f"spawn_target: {caller_name}() -> {invoked_nameb}() (pthread_create)"
                else:
                    edge["evidence"] = f"concurrency_{concurrency}: {caller_name}() -> {invoked_nameb}()"

            edges.append(edge)

    # Build local variable alias map: track assignments of known function names
    # to local variables. This handles patterns like:
    #   fn = my_cb; thread_send_msg(t, fn, ctx);
    # where `fn` is a local alias for `my_cb`.
    _LOCAL_ALIAS_RE = re.compile(r'(?:^|[\n;{}])\s*(\w+)\s*=\s*(\w+)\s*;')
    _local_aliases = {}  # caller_name -> {local_var: func_name}
    for caller_name, body_info in func_bodies.items():
        body = body_info["body"]
        aliases = {}
        for am in _LOCAL_ALIAS_RE.finditer(body):
            local_var = am.group(1)
            func_alias = am.group(2)
            # Only track if the RHS is a known function name and the LHS is NOT
            if (func_alias in func_names and func_alias not in effective_skip
                    and local_var not in func_names and len(local_var) > 2
                    and local_var not in ('if', 'for', 'while', 'switch', 'return',
                                          'sizeof', 'typeof', 'case', 'else', 'do',
                                          'NULL', 'true', 'false', 'rc', 'ret', 'err',
                                          'errno', 'idx', 'len', 'size', 'count', 'num',
                                          'val', 'ptr', 'status', 'result', 'offset')):
                aliases[local_var] = func_alias
        if aliases:
            _local_aliases[caller_name] = aliases

    # Detect callback arguments: function names passed as arguments to other calls.
    # Pattern: some_func(..., callback_name, ...) where callback_name is a known
    # function definition. This catches callbacks registered via function-parameter
    # passing that the profile-driven patterns and generic suffix detection miss.
    # IMPORTANT: We do NOT skip callees in effective_skip here, because skipped
    # callees (like PROJ_POLLER_REGISTER macro) may still register callbacks
    # that need to be detected.
    #
    # Additionally, detect "passthrough callback" functions: functions that receive
    # a function-pointer parameter and forward it to a known registration function.
    # Example: event_notify(desc, event_notify_fn) { thread_send_msg(t, event_notify_fn, c); }
    # When detected, these functions are added to _known_reg_funcs_local so the
    # cross-file callback detection can find their callers.
    #
    # Also resolves local variable aliases: fn = my_cb; reg_func(t, fn, ctx)
    # resolves fn → my_cb and creates the CALLBACK_ARG edge to my_cb.
    # Build edges-by-source index for O(1) lookups in CALLBACK_ARG section.
    # Avoids O(n²) scans where each candidate checks all edges for duplicates.
    _edges_by_source = {}
    for _e in edges:
        _edges_by_source.setdefault(_e['source'], []).append(_e)

    _CB_ARG_CALL_RE = re.compile(r'\b(\w+)\s*\(')
    _cb_arg_seen = set()  # Deduplicate within CALLBACK_ARG loop (separate from main loop)
    _passthrough_reg_funcs = {}  # func_name -> {cb_arg_index, concurrency_type}
    for caller_name, body_info in func_bodies.items():
        body = body_info["body"]
        body_start_line = body_info.get("start_line", 0)
        invoker_id = _make_func_id(caller_name, rel_path)

        for m in _CB_ARG_CALL_RE.finditer(body):
            invoked_nameb = m.group(1)
            # Skip keywords and short names
            if invoked_nameb in ('if', 'for', 'while', 'switch', 'return', 'sizeof',
                               'typeof', 'case', 'else', 'do') or len(invoked_nameb) <= 2:
                continue
            # Skip utility macros that take struct/type arguments, not callbacks
            if invoked_nameb in _effective_skip_callees:
                continue

            # Extract the argument list by finding the matching close paren
            rest = body[m.end():]
            depth = 1
            args_text = ''
            for ch in rest:
                if ch == '(':
                    depth += 1
                    args_text += ch
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        break
                    args_text += ch
                else:
                    args_text += ch

            # Check each comma-separated argument
            for arg_idx, arg in enumerate(args_text.split(',')):
                arg = arg.strip()
                # Remove leading & or * (address-of or dereference)
                arg = arg.lstrip('&*').strip()
                # Check if it's a bare identifier matching a known function name
                if _BARE_IDENT_RE.match(arg):
                    # Validate: the matched name should be a function definition,
                    # not a variable that coincidentally shares the name.
                    # Check if arg name appears as a local variable assignment
                    # in the caller's body (e.g., "int entry = ...;" means
                    # "entry" is a local var, not a callback function).
                    caller_body = func_bodies.get(caller_name, {}).get("body", "")
                    if caller_body:
                        # Check for local variable declarations: type arg = or type arg;
                        # This catches cases like "int entry = 5" where "entry" is a var.
                        # Use a single generic pattern (captures the declared name) and
                        # compare in Python so no per-argument re.compile is needed.
                        is_local_var = any(
                            m.group(1) == arg
                            for m in _LOCAL_VAR_TYPE_RE.finditer(caller_body)
                        )
                        if is_local_var:
                            continue
                        # Check if the arg is used as a function pointer:
                        # If the caller is a known registration function and the
                        # arg position matches the callback position, it's likely valid.
                        # Otherwise, if the arg is passed to a non-registration function
                        # and doesn't have callback naming, skip it.
                        is_known_reg = invoked_nameb in _known_reg_funcs_local
                        if not is_known_reg and arg not in func_names:
                            continue

                    if arg in func_names and arg not in effective_skip and len(arg) > 2 and arg not in _CALLBACK_ARG_GENERIC_NAMES:
                        # Don't create edge if it's the caller itself or already exists
                        if arg == caller_name:
                            continue
                        edge_key = (invoker_id, arg)
                        if edge_key in _cb_arg_seen:
                            continue
                        # Don't create if there's already a direct call edge to this target
                        # from the SAME caller (the main loop already captured it).
                        # Parentheses are critical: without them, Python's operator
                        # precedence makes the `or` split the `and`, causing the
                        # source check to be skipped for the `== arg` branch —
                        # matching edges from ANY source instead of just this caller.
                        has_direct = any(
                                        e.get("target", "").endswith('.' + arg) or
                                         e.get("target") == arg
                                        for e in _edges_by_source.get(invoker_id, []))
                        if has_direct:
                            continue

                        _cb_arg_seen.add(edge_key)
                        call_order += 1

                        # Determine concurrency annotation:
                        # Only set concurrency="callback" when the callee receiving
                        # the function pointer is a known registration function
                        # (like proj_thread_send_msg, pthread_create) OR when the
                        # target has callback/completion-like naming. Synchronous
                        # function pointers (like proj_json_decode_string passed
                        # to JSON parsing) should NOT get concurrency annotation.
                        _cb_concurrency_suffixes = tuple(
                            _profile_generic_suffixes if _profile_generic_suffixes
                            else ("_cb", "_fn", "_handler", "_callback")
                        ) + ("_complete", "_done", "_cpl", "_comp", "_completion",
                             "_cmpl", "_resubmit", "_finish")
                        # Also check for callback keywords embedded in the name
                        # (e.g., _delay_complete_io, dev_ctrlr_complete_cmd)
                        _cb_concurrency_contains = ("_complete_", "_done_", "_cpl_",
                                                    "_completion_", "_cmpl_")
                        if invoked_nameb in _known_reg_funcs_local:
                            cb_concurrency = "callback"
                        elif arg.endswith(_cb_concurrency_suffixes):
                            cb_concurrency = "callback"
                        elif any(kw in arg for kw in _cb_concurrency_contains):
                            cb_concurrency = "callback"
                        else:
                            cb_concurrency = ""

                        edges.append({
                            "source": invoker_id,
                            "target": arg,
                            "call_order": call_order,
                            "call_condition": "",
                            "confidence": "CALLBACK_ARG",
                            "source_tag": "ast",
                            "concurrency": cb_concurrency,
                            "evidence": f"callback_arg: {invoked_nameb}() arg#{arg_idx}={arg}",
                            "_source_file": rel_path,
                        })

                    # Passthrough detection for bare identifiers that are
                    # function-pointer parameters of the current function.
                    # This handles cases like:
                    #   void my_func(msg_fn fn, void *ctx) {
                    #       thread_send_msg(thread, fn, ctx);
                    #   }
                    # where `fn` is a bare identifier (not in func_names) but
                    # is a function-pointer parameter being forwarded to a
                    # registration function.
                    elif (invoked_nameb in _known_reg_funcs_local
                            and caller_name in _fp_params_by_func
                            and arg in _fp_params_by_func[caller_name]
                            and arg not in _NON_FUNC_CB_TARGETS
                            and caller_name not in _passthrough_reg_funcs):
                        # The cb_arg_index is the index of the fp param in
                        # the PASSTHROUGH FUNCTION's own signature, not in
                        # the call to the registration function. This is
                        # needed for cross-file callers to know which arg
                        # position to extract the callback from.
                        arg_idx = _fp_params_by_func[caller_name][arg]
                        # Find concurrency type from the registration function
                        pt_concurrency = "callback"
                        if _profile_cb_patterns:
                            for pat in _profile_cb_patterns:
                                if pat["register_func"] == invoked_nameb:
                                    pt_concurrency = pat["concurrency_type"]
                                    break
                        _passthrough_reg_funcs[caller_name] = {
                            "cb_arg_index": arg_idx,
                            "concurrency_type": pt_concurrency,
                        }
                        # Also add to known reg funcs so cross-file detection
                        # treats this function as a registration func
                        _known_reg_funcs_local.add(caller_name)
                        _known_reg_funcs.add(caller_name)
                    else:
                        # Two resolution paths for non-direct-function-name args:

                        # Path A: Local variable alias resolution.
                        # Handles: fn = my_cb; reg_func(t, fn, ctx)
                        # When `arg` is a local variable that was assigned a known
                        # function name earlier in the same function body, resolve
                        # it and create the CALLBACK_ARG edge to the aliased target.
                        caller_aliases = _local_aliases.get(caller_name)
                        if (caller_aliases and arg in caller_aliases
                                and invoked_nameb in _known_reg_funcs_local):
                            resolved_target = caller_aliases[arg]
                            if (resolved_target not in effective_skip
                                    and len(resolved_target) > 2
                                    and resolved_target != caller_name
                                    and resolved_target not in _CALLBACK_ARG_GENERIC_NAMES):
                                r_edge_key = (invoker_id, resolved_target)
                                if r_edge_key not in _cb_arg_seen:
                                    has_direct_r = any(
                                        e.get("target", "").endswith('.' + resolved_target) or
                                        e.get("target") == resolved_target
                                        for e in _edges_by_source.get(invoker_id, []))
                                    if not has_direct_r:
                                        _cb_arg_seen.add(r_edge_key)
                                        call_order += 1
                                        # Resolve concurrency from registration function
                                        r_concurrency = "callback"
                                        if _profile_cb_patterns:
                                            for pat in _profile_cb_patterns:
                                                if pat["register_func"] == invoked_nameb:
                                                    r_concurrency = pat["concurrency_type"]
                                                    break
                                        edges.append({
                                            "source": invoker_id,
                                            "target": resolved_target,
                                            "call_order": call_order,
                                            "call_condition": "",
                                            "confidence": "CALLBACK_ARG",
                                            "source_tag": "ast",
                                            "concurrency": r_concurrency,
                                            "evidence": f"callback_alias: {invoked_nameb}() arg#{arg_idx}={arg} -> {resolved_target}",
                                            "_source_file": rel_path,
                                            "_alias": arg,  # track the local var for debugging
                                        })

                        # Path B: Passthrough callback detection: if the arg is a
                        # function-pointer parameter of the current function (from
                        # its signature), and it's being passed to a known
                        # registration function, then this function is a passthrough
                        # callback registrar. Record it so cross-file detection can
                        # find callers that pass actual callback functions.
                        if (invoked_nameb in _known_reg_funcs_local
                                and caller_name in _fp_params_by_func
                                and arg in _fp_params_by_func[caller_name]
                                and arg not in _NON_FUNC_CB_TARGETS
                                and caller_name not in _passthrough_reg_funcs):
                            # Use the fp param's index in the PASSTHROUGH FUNCTION's
                            # own signature, not in the call to the registration func
                            arg_idx = _fp_params_by_func[caller_name][arg]
                            # Find concurrency type from the registration function
                            pt_concurrency = "callback"
                            if _profile_cb_patterns:
                                for pat in _profile_cb_patterns:
                                    if pat["register_func"] == invoked_nameb:
                                        pt_concurrency = pat["concurrency_type"]
                                        break
                            _passthrough_reg_funcs[caller_name] = {
                                "cb_arg_index": arg_idx,
                                "concurrency_type": pt_concurrency,
                            }
                            # Also add to _known_reg_funcs_local so cross-file
                            # detection treats this function as a registration func
                            _known_reg_funcs_local.add(caller_name)
                            _known_reg_funcs.add(caller_name)

    # Second pass: Create CALLBACK_ARG edges for callers of passthrough functions.
    # The first pass may have missed these because the passthrough function was
    # added to _known_reg_funcs_local AFTER its callers were already processed.
    # This pass re-processes functions that call passthrough functions with
    # direct function-name arguments.
    if _passthrough_reg_funcs:
        for caller_name, body_info in func_bodies.items():
            body = body_info["body"]
            invoker_id = _make_func_id(caller_name, rel_path)
            for m in _CB_ARG_CALL_RE.finditer(body):
                invoked_nameb = m.group(1)
                if invoked_nameb not in _passthrough_reg_funcs:
                    continue
                pt_info = _passthrough_reg_funcs[invoked_nameb]
                cb_idx = pt_info.get("cb_arg_index", 0)
                pt_concurrency = pt_info.get("concurrency_type", "callback")

                # Extract argument list
                rest = body[m.end():]
                depth = 1
                args_text = ''
                for ch in rest:
                    if ch == '(':
                        depth += 1
                        args_text += ch
                    elif ch == ')':
                        depth -= 1
                        if depth == 0:
                            break
                        args_text += ch
                    else:
                        args_text += ch

                # Extract the arg at cb_arg_index
                arg_list = args_text.split(',')
                if cb_idx >= len(arg_list):
                    continue
                arg = arg_list[cb_idx].strip().lstrip('&*').strip()
                if not _BARE_IDENT_RE.match(arg):
                    continue
                if arg not in func_names or arg in effective_skip or len(arg) <= 2:
                    continue
                if arg == caller_name or arg in _CALLBACK_ARG_GENERIC_NAMES:
                    continue

                target_id = arg
                edge_key = (invoker_id, target_id)
                if edge_key in _cb_arg_seen:
                    continue

                # Check for existing direct edge
                has_direct = any(
                    e.get("target", "").endswith('.' + arg) or
                     e.get("target") == arg
                    for e in _edges_by_source.get(invoker_id, [])
                )
                if has_direct:
                    continue

                _cb_arg_seen.add(edge_key)
                call_order += 1
                edges.append({
                    "source": invoker_id,
                    "target": arg,
                    "call_order": call_order,
                    "call_condition": "",
                    "confidence": "CALLBACK_ARG",
                    "source_tag": "ast",
                    "concurrency": pt_concurrency,
                    "evidence": f"passthrough_callback: {invoked_nameb}() arg#{cb_idx}={arg} via {caller_name}",
                    "_source_file": rel_path,
                    "_passthrough_via": invoked_nameb,
                })

    # Extract vtable registrations (function pointer table struct initializers)
    _struct_op_types = profile.get("struct_op_types", []) if profile else []
    vtable_registrations = _extract_vtable_registrations(source, rel_path, pp_conditions,
                                                         func_names=func_names,
                                                         struct_op_types=_struct_op_types)

    # Extract macro-based registration dispatch (constructor macros, token-paste)
    # Support both scanner config format (macro_dispatch_patterns) and raw profile
    # format (macro_dispatch.registration_macros)
    macro_dispatch_patterns = profile.get("macro_dispatch_patterns", []) if profile else []
    token_paste_macros = profile.get("token_paste_macros", []) if profile else []
    if not macro_dispatch_patterns and profile:
        md = profile.get("macro_dispatch", {})
        if md:
            macro_dispatch_patterns = md.get("registration_macros", [])
    if not token_paste_macros and profile:
        md = profile.get("macro_dispatch", {})
        if md:
            token_paste_macros = md.get("token_paste_macros", [])
    macro_result = _extract_macro_registrations(source, rel_path,
                                                 macro_dispatch_patterns=macro_dispatch_patterns,
                                                 token_paste_macros=token_paste_macros,
                                                 pp_conditions=pp_conditions,
                                                 func_names=func_names)

    # Extract container_of macro usages and struct embedding relationships.
    # These capture type containment information (outer struct embeds inner struct)
    # that the builder uses for improved dispatch resolution and domain hints.
    container_of_macros = profile.get("container_of_macros", []) if profile else []
    embedding_result = _extract_container_of(source, rel_path,
                                             container_of_macros=container_of_macros)
    struct_emb_result = _extract_struct_embeddings(source, rel_path)

    # Extract function pointer calls from each function body.
    # Skip for C++ files: obj->method() in C++ is a direct call,
    # not a function pointer dispatch. Only C files use ->field()
    # as fn_ptr dispatch. (_is_cpp is already defined above, before
    # the main call extraction loop.)
    fn_ptr_calls_by_caller = {}
    if not _is_cpp:
        for caller_name, body_info in func_bodies.items():
            body = body_info["body"]
            fn_ptr_calls = _extract_fn_ptr_calls(body, caller_name, effective_skip)
            if fn_ptr_calls:
                fn_ptr_calls_by_caller[caller_name] = fn_ptr_calls

    # Extract struct field assignments where function names are assigned
    field_assignments = []
    for caller_name, body_info in func_bodies.items():
        body = body_info["body"]
        assigns = _extract_field_assignments(body, caller_name, func_names, effective_skip,
                                             fp_params_by_func=_fp_params_by_func)
        field_assignments.extend(assigns)

    # Also add vtable registration fields as field_assignments.
    # Vtable struct initializers (.field = func) are struct field assignments
    # that the builder needs for FN_PTR resolution. Without this, FN_PTR calls
    # to vtable fields that aren't in _field_dispatch_map (from direct
    # assignments) can't be resolved.
    _fa_seen = set((fa["struct_chain"], fa["field_name"], fa["target_func"])
                   for fa in field_assignments)
    for vt in vtable_registrations:
        var_name = vt.get("var_name", "")
        struct_type = vt.get("struct_type", "")
        # Use var_name as struct_chain (it's the actual variable name used
        # in fn_ptr_calls), falling back to struct_type
        chain = var_name if var_name else struct_type
        for reg in vt.get("registrations", []):
            key = (chain, reg["field"], reg["func_name"])
            if key not in _fa_seen:
                _fa_seen.add(key)
                field_assignments.append({
                    "field_name": reg["field"],
                    "struct_chain": chain,
                    "target_func": reg["func_name"],
                    "caller": f"__vtable_init_{var_name}" if var_name else f"__vtable_init_{struct_type}",
                })

    return {
        "functions": functions,
        "edges": edges,
        "import_edges": import_edges,
        "vtable_registrations": vtable_registrations,
        "macro_registrations": macro_result.get("macro_registrations", []),
        "token_paste_functions": macro_result.get("token_paste_functions", []),
        "container_of_usages": embedding_result.get("container_of_usages", []),
        "conversion_funcs": embedding_result.get("conversion_funcs", []),
        "struct_defs": struct_emb_result.get("struct_defs", []),
        "fn_ptr_calls": fn_ptr_calls_by_caller,
        "field_assignments": field_assignments,
        "passthrough_reg_funcs": _passthrough_reg_funcs,
    }


def _is_macro_style_name(name: str) -> bool:
    """Check if a name follows the UPPER_CASE_WITH_UNDERSCORES macro convention.

    Names like RB_FOREACH_SAFE, TAILQ_FOREACH_FROM, PROJ_RPC_REGISTER
    are macro-style and should not be extracted as function definitions.
    Names like main, test, nop are NOT macro-style (too short or lowercase).
    """
    if len(name) <= 3:
        return False
    # Must be all uppercase letters, digits, and underscores
    # Must start with an uppercase letter
    return bool(re.match(r'^[A-Z][A-Z0-9_]+$', name))


def _extract_func_defs(source: str, rel_path: str, api_prefixes: list = None,
                       skip_names_add: list = None, skip_prefixes: list = None) -> list:
    """Extract function definitions using multi-strategy matching.

    Strategies:
    1. Inline: "type func_name(args) {" on one line
    2. Two-line: "type\\nfunc_name(args)" split across two lines
    3. Name+args only: "func_name(args)" on its own line (preceded by type line)
    """
    # Strip comments and string literals before extraction to prevent
    # false matches like "power of two(2^n)" in comments being extracted
    # as a function definition named "two". Use _strip_comments_only
    # (not _strip_comments) to preserve #ifdef directives which are
    # critical for conditional compilation handling.
    source = _strip_comments_only(source)
    functions = []
    lines = source.split('\n')
    n_lines = len(lines)
    seen_names = set()
    # Combine skip sets: _SKIP_NAMES + project-specific skip + UPPERCASE function-like macros
    # Macros like PROJ_RPC_REGISTER() expand to function definitions but are
    # NOT real function definitions — they are macro invocations.
    # However, LOWERCASE macro names (e.g., nop, ftl_trace_completion) should
    # NOT be skipped from extraction — they often have both a macro version
    # (in an #else branch for conditional compilation) AND a real function
    # definition in a .c file. Skipping them causes real functions to be missed.
    # Heuristic: names matching UPPER_CASE_PATTERN (all uppercase + underscores + digits)
    # are macro-style names that should be skipped. Mixed-case names like
    # proj_thread_send_msg or ftl_trace_completion are kept for extraction.
    import re as _re_for_skip
    _uppercase_macros = frozenset(
        m for m in _PROJECT_FUNC_MACROS
        if _re_for_skip.match(r'^[A-Z][A-Z0-9_]+$', m)
    )
    _def_skip = _SKIP_NAMES | _uppercase_macros
    if skip_names_add:
        _def_skip = _def_skip | frozenset(skip_names_add)
    _skip_prefixes = skip_prefixes or []

    def _should_skip_name(name: str) -> bool:
        """Check if a function name should be skipped from extraction."""
        if name in _def_skip or _is_macro_style_name(name):
            return True
        # Prefix-based skip (used for test-directory mocks of external lib functions)
        # Case-insensitive ONLY for ALL_UPPERCASE prefixes (SSL_ vs ssl_).
        # Mixed-case prefixes like 'Spdk' remain case-sensitive.
        name_lower = name.lower()
        for pfx in _skip_prefixes:
            if pfx.isupper() or (pfx.endswith('_') and pfx[:-1].isupper()):
                if name_lower.startswith(pfx.lower()):
                    return True
            else:
                if name.startswith(pfx):
                    return True
        return False

    i = 0
    while i < n_lines:
        line = lines[i].strip()

        # Skip preprocessor, comments, empty
        if not line or line.startswith('#') or line.startswith('//') or line.startswith('/*'):
            i += 1
            continue

        # Strategy 1: Full inline definition: type func_name(args) { on one line
        m = _FUNC_DEF_INLINE_RE.match(line)
        if m:
            ret_type = m.group(1).strip().split()[-1] if m.group(1).strip() else "void"
            func_name = m.group(2)
            params = m.group(3)

            if not _should_skip_name(func_name) and func_name not in seen_names:
                if ret_type not in ('if', 'while', 'for', 'switch', 'return', 'case'):
                    seen_names.add(func_name)
                    functions.append(_make_func_def(func_name, ret_type, params, rel_path, i + 1, api_prefixes=api_prefixes))
            i += 1
            continue

        # Strategy 1b: Fallback for pointer return types (e.g., "static void *func(args) {")
        # The main regex fails for pointer return types because \w[\w\s\*]*? is non-greedy
        # Use two-step parsing: match type+name+args+{, then extract name from last word before paren
        # IMPORTANT: pre-check must require a type keyword to avoid intercepting lines
        # that Strategy 4 should handle (e.g., "func_name(args) {" with type on prev line)
        if _TYPE_PREFIX_RE.match(line) and _PAREN_BRACE_RE.search(line):
            m_fb = _BEFORE_PAREN_ARGS_BRACE_RE.match(line)
            if m_fb:
                before_paren = m_fb.group(1).rstrip()
                params = m_fb.group(2)
                name_match = _TRAILING_IDENT_RE.search(before_paren)
                if name_match:
                    func_name = name_match.group(1)
                    ret_type_raw = before_paren[:name_match.start()].strip().rstrip('*').strip()
                    first_word = ret_type_raw.split()[0] if ret_type_raw.split() else ''
                    if (not _should_skip_name(func_name) and func_name not in seen_names
                            and first_word in _TYPE_KEYWORDS
                            and len(func_name) > 2):
                        ret_type = ret_type_raw.split()[-1] if ret_type_raw.split() else "void"
                        seen_names.add(func_name)
                        functions.append(_make_func_def(func_name, ret_type, params, rel_path, i + 1, api_prefixes=api_prefixes))
                i += 1
                continue

        # Strategy 2: type + name(args) on one line, no brace
        m = _FUNC_DEF_NOBRACE_RE.match(line)
        if m:
            ret_type_raw = m.group(1).strip()
            func_name = m.group(2)
            params = m.group(3)

            # Skip if this is a declaration (line ends with ';')
            if line.rstrip().endswith(';'):
                i += 1
                continue

            # Verify ret_type looks like a C type/prefix, not a statement
            first_word = ret_type_raw.split()[0] if ret_type_raw.split() else ''
            if (not _should_skip_name(func_name) and func_name not in seen_names
                    and first_word not in ('if', 'while', 'for', 'switch', 'return', 'case', 'else', 'do')
                    and len(func_name) > 2
                    and not ret_type_raw.startswith('(')):
                # Check next line for opening brace
                ret_type = ret_type_raw.split()[-1] if ret_type_raw.split() else "void"
                seen_names.add(func_name)
                functions.append(_make_func_def(func_name, ret_type, params, rel_path, i + 1, api_prefixes=api_prefixes))
            i += 1
            continue

        # Strategy 2b: Pointer return type + name(args) on one line, no brace
        # e.g., "static void *func_name(args)" where brace is on next line
        # The main regex _FUNC_DEF_NOBRACE_RE fails for pointer return types
        # because \w[\w\s\*]*? is non-greedy. Use two-step parsing instead.
        # Only try this if Strategy 2 didn't match (pointer return types)
        if not m:
            # Quick check: line has type-like prefix, name, and (args) but no brace
            m_fb2 = _PTR_RET_FUNC_NOBRACE_RE.match(line)
            if m_fb2:
                ret_type_raw = m_fb2.group(1).strip().rstrip('*').strip()
                func_name = m_fb2.group(2)
                params = m_fb2.group(3)
                first_word = ret_type_raw.split()[0] if ret_type_raw.split() else ''
                if (not _should_skip_name(func_name) and func_name not in seen_names
                        and first_word not in ('if', 'while', 'for', 'switch', 'return', 'case', 'else', 'do')
                        and len(func_name) > 2):
                    # Check next line for opening brace (skip preprocessor lines)
                    has_brace = False
                    for j in range(i + 1, min(i + 5, n_lines)):
                        check_line = lines[j].strip()
                        if check_line.startswith('#'):
                            continue
                        if check_line.startswith('{') or check_line.endswith('{'):
                            has_brace = True
                            break
                        break  # Non-# line that's not a brace = stop
                    if has_brace:
                        ret_type = ret_type_raw.split()[-1] if ret_type_raw.split() else "void"
                        seen_names.add(func_name)
                        functions.append(_make_func_def(func_name, ret_type, params, rel_path, i + 1, api_prefixes=api_prefixes))
                i += 1
                continue

        # Strategy 3: func_name(args) on its own line (return type on prev line)
        m = _FUNC_NAME_ARGS_RE.match(line)
        if m:
            func_name = m.group(1)
            params = m.group(2)

            if (not _should_skip_name(func_name) and func_name not in seen_names
                    and len(func_name) > 2
                    and '(' not in func_name):
                # Check previous line for a return type
                ret_type = "void"
                if i > 0:
                    prev_line = lines[i - 1].strip()
                    # Previous line should be a type (no parens, no semicolons)
                    if (prev_line and not prev_line.startswith('#')
                            and '(' not in prev_line and ';' not in prev_line
                            and '{' not in prev_line
                            and prev_line.split()[0] not in ('if', 'while', 'for', 'switch', 'return', 'case', 'else', 'do')):
                        ret_type = prev_line.split()[-1] if prev_line.split() else "void"

                # Verify opening brace within next few lines (handles multi-line arg lists)
                # Also skip preprocessor lines and type-only lines from #else branches
                # Window of 20: some functions have 14+ parameter lines
                has_brace = False
                pp_branch_depth = 0  # Track #ifdef/#else/#endif to skip alternate branches
                for j in range(i + 1, min(i + 20, n_lines)):
                    check_line = lines[j].strip()
                    # Track preprocessor branch nesting
                    if check_line.startswith('#'):
                        if _PP_IF_RE.match(check_line):
                            pp_branch_depth += 1
                        elif _PP_ELSE_RE.match(check_line):
                            # Entering an alternate branch — skip until matching #endif
                            if pp_branch_depth == 0:
                                pp_branch_depth = 1  # Start skipping
                        elif _PP_ENDIF_RE.match(check_line):
                            if pp_branch_depth > 0:
                                pp_branch_depth -= 1
                        continue
                    # If we're inside an alternate preprocessor branch, skip
                    if pp_branch_depth > 0:
                        continue
                    if check_line.startswith('{') or check_line.endswith('{'):
                        has_brace = True
                        break
                    # Skip lines that look like type declarations from #else branches
                    # e.g., "static struct device_ctrlr *" — no parens, no semicolons
                    if check_line and '(' not in check_line and ';' not in check_line and '=' not in check_line:
                        continue
                    # If we hit a line that looks like another declaration, stop
                    if check_line and not check_line.startswith(',') and not check_line.startswith(')'):
                        break

                # Also accept if the line itself ends with {
                if line.endswith('{'):
                    has_brace = True

                if has_brace:
                    seen_names.add(func_name)
                    functions.append(_make_func_def(func_name, ret_type, params, rel_path, i + 1, api_prefixes=api_prefixes))

        # Strategy 4: func_name(args) { on one line (return type on previous line)
        # e.g., "driver_fail_request(struct device_ctrlr *ctrlr) {"
        # This catches functions where the return type is on the previous line
        # and the function name + params + opening brace are all on one line
        if not m:
            m4 = _FUNC_NAME_ARGS_BRACE_RE.match(line)
            if m4:
                func_name = m4.group(1)
                params = m4.group(2)

                if (not _should_skip_name(func_name) and func_name not in seen_names
                        and len(func_name) > 2):
                    # Check previous line for a return type
                    ret_type = "void"
                    if i > 0:
                        prev_line = lines[i - 1].strip()
                        if (prev_line and not prev_line.startswith('#')
                                and '(' not in prev_line and ';' not in prev_line
                                and '{' not in prev_line
                                and prev_line.split()[0] not in ('if', 'while', 'for', 'switch', 'return', 'case', 'else', 'do')):
                            ret_type = prev_line.split()[-1] if prev_line.split() else "void"

                    seen_names.add(func_name)
                    functions.append(_make_func_def(func_name, ret_type, params, rel_path, i + 1, api_prefixes=api_prefixes))

        # Strategy 3b: func_name(args on its own line, multi-line params
        # e.g., "device_cmd_read(ns, qpair, buffer,"  (continues on next line)
        if not m:
            m2 = _FUNC_NAME_ARGS_OPEN_RE.match(line)
            if m2:
                func_name = m2.group(1)
                params = m2.group(2)

                if (not _should_skip_name(func_name) and func_name not in seen_names
                        and len(func_name) > 2):
                    # Check previous line for a return type
                    # CRITICAL: if the previous line contains '(' or control flow,
                    # this is likely a function CALL continuation, not a definition.
                    ret_type = "void"
                    prev_line_is_return_type = False
                    if i > 0:
                        prev_line = lines[i - 1].strip()
                        if (prev_line and not prev_line.startswith('#')
                                and '(' not in prev_line and ';' not in prev_line
                                and '{' not in prev_line
                                and prev_line.split()[0] not in ('if', 'while', 'for', 'switch', 'return', 'case', 'else', 'do')):
                            ret_type = prev_line.split()[-1] if prev_line.split() else "void"
                            prev_line_is_return_type = True
                    # If previous line has '(' — likely a function call continuation
                    # Skip this strategy entirely (the line is a call, not a definition)
                    _skip_3b = False
                    if i > 0:
                        prev_line = lines[i - 1].strip()
                        if prev_line and '(' in prev_line:
                            _skip_3b = True

                    if not _skip_3b:
                        # Verify opening brace within next few lines
                        # Also skip preprocessor lines and type-only lines from #else branches
                        # Window of 20: some functions have 14+ parameter lines
                        # (e.g., mdns_resolve_handler with 14 params before '{')
                        has_brace = False
                        pp_branch_depth = 0
                        for j in range(i + 1, min(i + 20, n_lines)):
                            check_line = lines[j].strip()
                            if check_line.startswith('#'):
                                if _PP_IF_RE.match(check_line):
                                    pp_branch_depth += 1
                                elif _PP_ELSE_RE.match(check_line):
                                    if pp_branch_depth == 0:
                                        pp_branch_depth = 1
                                elif _PP_ENDIF_RE.match(check_line):
                                    if pp_branch_depth > 0:
                                        pp_branch_depth -= 1
                                continue
                            if pp_branch_depth > 0:
                                continue
                            if check_line.startswith('{') or check_line.endswith('{'):
                                has_brace = True
                                break
                            # Skip type-only lines from #else branches
                            if check_line and '(' not in check_line and ';' not in check_line and '=' not in check_line:
                                continue
                            # Stop if we hit something that's clearly not a continuation
                            # but continue past closing paren ')' on its own line
                            if check_line and not check_line.startswith(',') and not check_line.endswith(',') and not check_line.endswith(')'):
                                break

                        if has_brace:
                            seen_names.add(func_name)
                            # Collect full params by scanning continuation lines
                            full_params = params.rstrip(',').strip()
                            for j in range(i + 1, min(i + 20, n_lines)):
                                check_line = lines[j].strip()
                                if check_line.startswith('{') or check_line.endswith('{'):
                                    break
                                full_params += ', ' + check_line.rstrip(',').strip()
                            # Remove trailing ) if present (closing paren of param list)
                            if full_params.endswith(')'):
                                full_params = full_params[:-1].rstrip(',').strip()
                            functions.append(_make_func_def(func_name, ret_type, full_params, rel_path, i + 1, api_prefixes=api_prefixes))

        # Strategy 3c: func_name(complex args with nested parens, like fn ptr params)
        # e.g., "module_delete_clean(struct module_inst *inst, void (*cb)(void *, int),"
        # This handles cases where [^)]* fails due to nested parens in function pointer params.
        # Match: func_name( at line start, then scan for matching ) counting paren depth.
        if not m and not m2:
            m3c = _FUNC_NAME_OPEN_PAREN_RE.match(line)
            if m3c and not line.endswith(';') and not line.endswith('{'):
                func_name_3c = m3c.group(1)
                if (func_name_3c not in _def_skip and func_name_3c not in seen_names
                        and len(func_name_3c) > 2
                        and func_name_3c[0] not in ('i',)  # quick filter: not 'if'
                        and not func_name_3c.startswith(('while', 'for', 'switch', 'return', 'case', 'else'))):
                    # Check previous line for return type
                    ret_type = "void"
                    prev_line_is_return_type = False
                    if i > 0:
                        prev_line = lines[i - 1].strip()
                        if (prev_line and not prev_line.startswith('#')
                                and '(' not in prev_line and ';' not in prev_line
                                and '{' not in prev_line
                                and prev_line.split()[0] not in ('if', 'while', 'for', 'switch', 'return', 'case', 'else', 'do')):
                            ret_type = prev_line.split()[-1] if prev_line.split() else "void"
                            prev_line_is_return_type = True

                    if prev_line_is_return_type:
                        # Scan for matching close paren across lines, counting depth
                        paren_depth = line.count('(') - line.count(')')
                        full_params = line[m3c.end():]  # everything after the first (
                        scan_line = i + 1
                        while paren_depth > 0 and scan_line < min(i + 10, n_lines):
                            next_line = lines[scan_line].strip()
                            paren_depth += next_line.count('(') - next_line.count(')')
                            if paren_depth > 0:
                                full_params += ' ' + next_line
                            else:
                                # Found the closing paren — include up to it
                                # Truncate after the matching )
                                last_close = 0
                                depth = line.count('(') - line.count(')')
                                for ci, ch in enumerate(next_line):
                                    if ch == '(':
                                        depth += 1
                                    elif ch == ')':
                                        depth -= 1
                                        if depth == 0:
                                            last_close = ci
                                            break
                                full_params += ' ' + next_line[:last_close]
                            scan_line += 1

                        # Now check if there's a { after the params
                        has_brace = False
                        for j in range(scan_line, min(scan_line + 3, n_lines)):
                            check_line = lines[j].strip()
                            if check_line.startswith('#'):
                                continue
                            if check_line.startswith('{') or check_line.endswith('{'):
                                has_brace = True
                                break
                            break

                        if has_brace:
                            seen_names.add(func_name_3c)
                            functions.append(_make_func_def(func_name_3c, ret_type, full_params, rel_path, i + 1, api_prefixes=api_prefixes))

        # Strategy 5: type func_name(open_args, multi-line args, then brace)
        # e.g., "int driver_unmap_blocks(struct device_ns *ns, ..., void *driver_ctx,"
        # e.g., "struct driver_io *driver_io_update_args(struct driver_io *bio, ..., int iovcnt,"
        # Two-step parsing: first match everything-before-open-paren, then split type from name
        if not m and not m4:
            m5 = _FUNC_DEF_MULTILINE_ARGS_RE.match(line)
            if m5:
                before_paren = m5.group(1).rstrip()
                params = m5.group(2)

                # Extract function name: last word-char sequence before the paren
                name_match = _TRAILING_IDENT_RE.search(before_paren)
                if name_match:
                    func_name = name_match.group(1)
                    ret_type_raw = before_paren[:name_match.start()].strip().rstrip('*').strip()

                    first_word = ret_type_raw.split()[0] if ret_type_raw.split() else ''
                    if (not _should_skip_name(func_name) and func_name not in seen_names
                            and first_word not in ('if', 'while', 'for', 'switch', 'return', 'case', 'else', 'do')
                            and first_word in _TYPE_KEYWORDS
                            and len(func_name) > 2
                            and not ret_type_raw.startswith('(')):
                        # Verify opening brace within next few lines
                        # Also skip preprocessor lines and type-only lines from #else branches
                        has_brace = False
                        for j in range(i + 1, min(i + 12, n_lines)):
                            check_line = lines[j].strip()
                            if check_line.startswith('#'):
                                continue
                            if check_line.startswith('{') or check_line.endswith('{'):
                                has_brace = True
                                break
                            # Skip type-only lines from #else branches
                            if check_line and '(' not in check_line and ';' not in check_line and '=' not in check_line:
                                continue
                            # Stop if we hit something that's clearly not a continuation
                            if check_line and not check_line.startswith(',') and not check_line.endswith(',') and not check_line.endswith(')'):
                                break

                        if has_brace:
                            ret_type = ret_type_raw.split()[-1] if ret_type_raw.split() else "void"
                            seen_names.add(func_name)
                            # Collect full params by scanning continuation lines
                            full_params = params.rstrip(',').strip()
                            for j in range(i + 1, min(i + 12, n_lines)):
                                check_line = lines[j].strip()
                                if check_line.startswith('#'):
                                    continue
                                if check_line.startswith('{') or check_line.endswith('{'):
                                    break
                                full_params += ', ' + check_line.rstrip(',').strip()
                            # Remove trailing ) if present (closing paren of param list)
                            if full_params.endswith(')'):
                                full_params = full_params[:-1].rstrip(',').strip()
                            functions.append(_make_func_def(func_name, ret_type, full_params, rel_path, i + 1, api_prefixes=api_prefixes))

        # Strategy 6: func_name(complex_args_with_fn_ptrs) or func_name(complex_args_with_fn_ptrs) {
        # Handles function definitions with function pointer arguments that have
        # nested parentheses, e.g., void (*cb_fn)(void *ctx, int status)
        # Other strategies fail because [^)]* stops at the first ) inside the fn ptr
        # ONLY try this when previous line is a valid C return type AND
        # the line has nested parens (more than one open paren)
        if not m and not m4 and not m5:
            if '(' in line and line.count('(') > 1:
                # Only try for lines with nested parens (function pointer args)
                m6 = _FUNC_NAME_OPEN_PAREN_RE.match(line)
                if m6:
                    func_name = m6.group(1)
                    if (not _should_skip_name(func_name) and func_name not in seen_names
                            and len(func_name) > 2
                            and func_name not in ('if', 'while', 'for', 'switch', 'return',
                                                  'case', 'else', 'do', 'sizeof', 'typeof')):
                        # Check previous line for return type
                        has_valid_type = False
                        ret_type = "void"
                        if i > 0:
                            prev_line = lines[i - 1].strip()
                            if (prev_line and not prev_line.startswith('#')
                                    and '(' not in prev_line and ';' not in prev_line
                                    and '{' not in prev_line
                                    and prev_line.split()[0] not in ('if', 'while', 'for', 'switch', 'return', 'case', 'else', 'do')):
                                first_word = prev_line.split()[0]
                                if first_word in _TYPE_KEYWORDS:
                                    has_valid_type = True
                                    ret_type = prev_line.split()[-1] if prev_line.split() else "void"

                        if has_valid_type:
                            # Find balanced closing paren
                            paren_depth = 0
                            close_paren_pos = -1
                            for ci, ch in enumerate(line):
                                if ch == '(':
                                    paren_depth += 1
                                elif ch == ')':
                                    paren_depth -= 1
                                    if paren_depth == 0:
                                        close_paren_pos = ci
                                        break

                            if close_paren_pos > 0:
                                after_close = line[close_paren_pos + 1:].strip()
                                params_start = line.index('(') + 1
                                full_params = line[params_start:close_paren_pos]

                                if after_close.startswith('{'):
                                    seen_names.add(func_name)
                                    functions.append(_make_func_def(func_name, ret_type, full_params, rel_path, i + 1, api_prefixes=api_prefixes))
                                elif not after_close:
                                    has_brace = False
                                    for j in range(i + 1, min(i + 5, n_lines)):
                                        check_line = lines[j].strip()
                                        if check_line.startswith('#'):
                                            continue
                                        if check_line.startswith('{') or check_line.endswith('{'):
                                            has_brace = True
                                            break
                                        break
                                    if has_brace:
                                        seen_names.add(func_name)
                                        functions.append(_make_func_def(func_name, ret_type, full_params, rel_path, i + 1, api_prefixes=api_prefixes))

        i += 1

    return functions


def _make_func_def(func_name: str, ret_type: str, params: str,
                   rel_path: str, line_num: int,
                   api_prefixes: list = None) -> dict:
    """Create a function definition dict."""
    func_id = _make_func_id(func_name, rel_path)
    domain = _derive_domain(rel_path)
    labels = _derive_labels(func_name, api_prefixes=api_prefixes,
                            source_file=rel_path)

    return {
        "id": func_id,
        "name": func_name,
        "signature": f"{ret_type} {func_name}({params})",
        "source_file": rel_path,
        "line": line_num,
        "labels": labels,
        "domain": domain,
        "is_empty": False,
        "body_text": "",
    }


_REGEX_CALLBACK_SUFFIXES = ('_cb', '_callback', '_handler', '_fn', '_done', '_completion', '_cpl', '_event')


def _derive_labels(func_name: str, api_prefixes: list = None,
                   source_file: str = "") -> list:
    """Derive function labels from name patterns."""
    labels = []
    # Only label 'main' as API_entry if it's in a non-test, non-example path.
    # Test/example/app main functions are not public API entries.
    if func_name == 'main':
        sf_lower = source_file.lower()
        _TEST_PATH_SEGMENTS = ('test', 'ut', 'example', 'examples', 'app',
                               'apps', 'perf', 'benchmark', 'fuzz', 'demo',
                               'sample', 'samples')
        parts = sf_lower.replace('\\', '/').split('/')
        is_test_path = any(p in _TEST_PATH_SEGMENTS for p in parts)
        if not is_test_path:
            labels.append('API_entry')
    # Check configurable API prefixes (from --api-prefixes)
    # But skip API_entry for functions in Documentation/test/example paths
    if api_prefixes:
        sf_lower = source_file.lower()
        _NON_API_PATHS = ('documentation', 'doc', 'test', 'tests', 'example',
                          'examples', 'sample', 'samples', 'benchmark', 'fuzz')
        is_non_api_path = any(p in sf_lower.replace('\\', '/').split('/') for p in _NON_API_PATHS)
        if not is_non_api_path:
            for prefix in api_prefixes:
                if func_name.startswith(prefix) and '_internal' not in func_name:
                    if 'API_entry' not in labels:
                        labels.append('API_entry')
                    break
    # Callback by naming convention: _cb, _done, _completion, _cpl, _event are internal
    # completion handlers, not external endpoints. Auto-label as callback_func.
    if any(pat in func_name.lower() for pat in _REGEX_CALLBACK_SUFFIXES):
        labels.append('callback_func')
    if '_init' in func_name or '_create' in func_name:
        labels.append('constructor')
    if '_fini' in func_name or '_destroy' in func_name or '_cleanup' in func_name or '_free' in func_name:
        labels.append('destructor')
    if '_thread' in func_name.lower() or 'thread_fn' in func_name.lower():
        labels.append('thread_processor')
    # Don't label as unknown_end — that's for external endpoints only
    # Internal functions without a specific label remain unlabeled
    return labels


def _strip_comments(source: str) -> str:
    """Remove C-style block comments, line comments, string literals, and preprocessor directives from source.

    String literals are removed because format strings like "SCT(0x%x)" can produce
    false callee matches when the regex scanner looks for func_name( patterns.
    All removals preserve newline count to maintain line number alignment.
    """
    def _replace_block_comment(match):
        return '\n' * match.group(0).count('\n')
    result = _STRIP_BLOCK_COMMENT_RE.sub(_replace_block_comment, source)
    result = _STRIP_LINE_COMMENT_RE.sub('', result)
    result = _STRIP_STRING_LIT_RE.sub('', result)
    def _replace_pp_with_empty(match):
        return '\n' * match.group(0).count('\n')
    result = _STRIP_PP_DIRECTIVE_RE.sub(_replace_pp_with_empty, result)
    return result


def _strip_comments_only(source: str) -> str:
    """Remove only comments and string literals, preserving preprocessor directives.

    Used for function definition extraction where #ifdef handling is critical.
    """
    def _replace_block_comment(match):
        return '\n' * match.group(0).count('\n')
    result = _STRIP_BLOCK_COMMENT_RE.sub(_replace_block_comment, source)
    result = _STRIP_LINE_COMMENT_RE.sub('', result)
    result = _STRIP_STRING_LIT_RE.sub('', result)
    return result


def _extract_func_bodies(source: str, func_defs: list) -> dict:
    """Extract function bodies using brace counting.

    Returns dict of func_name → {"body": str, "source_offset": int, "start_line": int}
    where source_offset is the byte offset of the body start in the original source,
    and start_line is the 0-indexed line number of the opening brace.

    Handles #ifdef conditional compilation: when multiple #ifdef/#else branches
    exist, only the FIRST branch's braces are counted (the #ifdef branch), and
    #else branches are skipped for brace counting. This prevents brace imbalance
    when both branches contain opening braces but only one closing brace.
    """
    bodies = {}
    lines = source.split('\n')

    # Build line offset map: line index → byte offset in source
    line_offsets = []
    pos = 0
    for line in lines:
        line_offsets.append(pos)
        pos += len(line) + 1  # +1 for '\n'

    # Preprocessor conditional patterns are shared module-level definitions
    # (_PP_IF_RE / _PP_ELSE_RE / _PP_ENDIF_RE) so this function no longer
    # recompiles them on every call.

    for fdef in func_defs:
        start_line = fdef["line"] - 1
        func_name = fdef["name"]

        # Find the opening brace (allow up to 20 lines for long parameter lists)
        body_start = None
        for i in range(start_line, min(start_line + 20, len(lines))):
            if '{' in lines[i]:
                body_start = i
                break

        # If no opening brace found, this is a declaration not a definition — skip
        if body_start is None:
            continue

        # Source offset of the body start (for PP condition alignment)
        source_offset = line_offsets[body_start] if body_start < len(line_offsets) else 0

        # Count braces to find body end, with #ifdef awareness.
        # Track #ifdef nesting: when we enter an #else branch, skip brace counting
        # until the matching #endif. This prevents brace imbalance from counting
        # braces in both branches of a conditional.
        #
        # Pre-strip the body slice ONCE and reuse the stripped lines.
        # Must use _strip_comments (NOT _strip_comments_only) so that
        # #define macros with { } don't throw off brace counting.
        # PP tracking uses raw_line (original line), not stripped, so
        # stripping PP directives from the brace-counting text is safe.
        # Previously `_strip_comments(line)` was called PER LINE inside this
        # loop — ~300M recompiles on kernel-scale scans.
        body_end_limit = min(body_start + 500, len(lines))
        raw_slice = lines[body_start:body_end_limit]
        stripped_slice = _strip_comments('\n'.join(raw_slice)).split('\n')
        body_lines = []
        brace_count = 0
        found_open = False
        pp_skip_depth = 0  # >0 means we're in an #else branch, skip brace counting
        pp_if_depth = 0    # Track #ifdef nesting depth

        for i in range(body_start, body_end_limit):
            line = lines[i]
            raw_line = line.strip()
            _si = i - body_start
            stripped = stripped_slice[_si] if _si < len(stripped_slice) else ''

            # Track preprocessor conditional nesting
            if _PP_IF_RE.match(raw_line):
                if pp_skip_depth > 0:
                    pp_skip_depth += 1
                else:
                    pp_if_depth += 1
            elif _PP_ELSE_RE.match(raw_line):
                if pp_skip_depth > 0:
                    pass  # Already skipping, stay in skip mode
                elif pp_if_depth > 0:
                    # Entering #else branch — skip brace counting
                    pp_skip_depth = 1
            elif _PP_ENDIF_RE.match(raw_line):
                if pp_skip_depth > 0:
                    pp_skip_depth -= 1
                elif pp_if_depth > 0:
                    pp_if_depth -= 1

            # Only count braces when not in an #else branch
            if pp_skip_depth == 0:
                for ch in stripped:
                    if ch == '{':
                        brace_count += 1
                        found_open = True
                    elif ch == '}':
                        brace_count -= 1

            body_lines.append(line)
            if found_open and brace_count <= 0:
                break

        bodies[func_name] = {
            "body": _strip_comments('\n'.join(body_lines)),
            "source_offset": source_offset,
            "start_line": body_start,
        }

    return bodies


def _extract_pp_conditions(source: str) -> list:
    """Extract #ifdef/#if condition spans from source.

    Returns list of (start_line, end_line, condition_text) where line numbers
    are 0-indexed. Using line numbers instead of byte offsets avoids alignment
    issues when the body text is stripped/modified.
    """
    conditions = []
    stack = []
    line_idx = 0

    for line in source.split('\n'):
        m = _PPCOND_RE.match(line)
        if m:
            directive = m.group(1)
            cond_text = m.group(2).strip()

            if directive in ('ifdef', 'ifndef', 'if'):
                condition_str = f"#{directive} {cond_text}" if cond_text else f"#{directive}"
                stack.append((line_idx, condition_str))
            elif directive in ('elif', 'else'):
                if stack:
                    prev_start, prev_cond = stack.pop()
                    conditions.append((prev_start, line_idx - 1, prev_cond))
                    if directive == 'else':
                        # #else has no condition text; include parent #ifdef
                        # condition for clarity (e.g., "#else PROJ_CONFIG_FOO")
                        parent_cond = prev_cond.split(None, 1)[-1] if prev_cond else ''
                        new_cond = f"#else {parent_cond}" if parent_cond else "#else"
                    else:
                        new_cond = f"#{directive} {cond_text}" if cond_text else f"#{directive}"
                    stack.append((line_idx, new_cond))
            elif directive == 'endif':
                if stack:
                    prev_start, prev_cond = stack.pop()
                    conditions.append((prev_start, line_idx, prev_cond))

        line_idx += 1

    return conditions


def _extract_vtable_registrations(source: str, rel_path: str,
                                   pp_conditions: list = None,
                                   func_names: set = None,
                                   struct_op_types: list = None) -> list:
    """Extract vtable (function pointer table) registrations from source.

    Finds struct initializers like:
        static const struct mylib_fn_table ops_table = {
            .submit_request = mylib_submit_request,
            .destruct = mylib_destruct,
        };

    Returns list of dicts:
        {
            "struct_type": "mylib_fn_table",
            "var_name": "ops_table",
            "registrations": [{"field": "submit_request", "func_name": "mylib_submit_request", "condition": ""}],
            "source_file": rel_path,
        }
    """
    if pp_conditions is None:
        pp_conditions = []
    if func_names is None:
        func_names = set()

    # Callback suffixes that indicate a function pointer field
    _CB_SUFFIXES = ('_cb', '_fn', '_handler', '_callback', '_done', '_cpl',
                    '_event', '_notify', '_start', '_stop', '_init', '_fini',
                    '_create', '_destroy', '_open', '_close', '_read', '_write',
                    '_process', '_handle', '_submit', '_complete', '_poll')

    results = []
    lines = source.split('\n')

    for m in _VTABLE_STRUCT_RE.finditer(source):
        struct_type = m.group(1)
        var_name = m.group(2)

        # Find the opening brace position and extract the body
        # Count lines to get line number for condition matching
        start_pos = m.start()
        start_line = source[:start_pos].count('\n')

        # Find matching closing brace
        brace_count = 0
        body_start = source.find('{', start_pos)
        if body_start < 0:
            continue

        brace_count = 1
        pos = body_start + 1
        while pos < len(source) and brace_count > 0:
            ch = source[pos]
            if ch == '{':
                brace_count += 1
            elif ch == '}':
                brace_count -= 1
            pos += 1

        body_text = source[body_start + 1:pos - 1]

        # Extract .field = func_name pairs
        registrations = []
        for fm in _VTABLE_FIELD_RE.finditer(body_text):
            field_name = fm.group(1)
            func_name = fm.group(2)

            # Skip if func_name is a keyword or type
            if func_name in _SKIP_NAMES or func_name in _TYPE_KEYWORDS:
                continue
            # Skip if func_name is NULL
            if func_name == 'NULL':
                continue
            # Skip ALL_CAPS names — these are macro constants or enum values,
            # not function pointers (e.g., UINT64_MAX, RAID0, TAILQ_HEAD_INITIALIZER)
            if func_name.isupper() or re.match(r'^[A-Z][A-Z0-9_]+$', func_name):
                continue
            # Skip very short names — likely variables, not functions (e.g., req, buf, iov)
            if len(func_name) <= 3:
                continue
            # Skip common non-function values: "unused" is a common placeholder
            if func_name in ('unused', 'options'):
                continue

            # Get condition for this registration
            # Calculate line number of this field assignment
            field_line_in_body = body_text[:fm.start()].count('\n')
            field_line = start_line + field_line_in_body + 1  # +1 for the { line

            condition = ""
            for cond_start_line, cond_end_line, cond_text in pp_conditions:
                if cond_start_line <= field_line <= cond_end_line:
                    condition = cond_text
                    break

            registrations.append({
                "field": field_name,
                "func_name": func_name,
                "condition": condition,
            })

        # Only keep this vtable if at least one registration looks like a
        # function pointer (known function name or matches callback suffix).
        # This filters out data struct initializers (iovec, proj_nvme_cpl, etc.)
        # that the regex falsely matches as vtable registrations.
        if registrations:
            # Struct types that are known function pointer tables
            _BASE_VTABLE_TYPE_KEYWORDS = ('fn_table', 'ops', 'module', 'impl',
                                          'scheduler', 'driver', 'callbacks',
                                          'handlers', 'dispatch', 'interface')
            # Merge with profile-provided struct_op_types (e.g., file_operations, inode_operations)
            _profile_op_types = tuple(struct_op_types) if struct_op_types else ()
            _VTABLE_TYPE_KEYWORDS = _BASE_VTABLE_TYPE_KEYWORDS + _profile_op_types
            struct_type_lower = struct_type.lower()
            is_known_vtable_type = any(kw in struct_type_lower for kw in _VTABLE_TYPE_KEYWORDS)
            # Also check exact match for profile-provided types (e.g., "file_operations" exact)
            if not is_known_vtable_type and _profile_op_types:
                is_known_vtable_type = struct_type in _profile_op_types

            has_fn_ptr = False
            for reg in registrations:
                fn = reg['func_name']
                if fn in func_names:
                    has_fn_ptr = True
                    break
            # If no known function names, check callback suffix patterns
            # but only for struct types that look like vtables
            if not has_fn_ptr:
                if is_known_vtable_type:
                    for reg in registrations:
                        fn = reg['func_name']
                        if any(fn.endswith(suf) for suf in _CB_SUFFIXES):
                            has_fn_ptr = True
                            break
            if not has_fn_ptr:
                continue
            # Get condition for the whole vtable definition
            vtable_condition = ""
            for cond_start_line, cond_end_line, cond_text in pp_conditions:
                if cond_start_line <= start_line <= cond_end_line:
                    vtable_condition = cond_text
                    break

            results.append({
                "struct_type": struct_type,
                "var_name": var_name,
                "registrations": registrations,
                "source_file": rel_path,
                "condition": vtable_condition,
            })

    return results


def _extract_macro_registrations(source: str, rel_path: str,
                                  macro_dispatch_patterns: list = None,
                                  token_paste_macros: list = None,
                                  pp_conditions: list = None,
                                  func_names: set = None) -> dict:
    """Extract macro-based registration dispatch from source.

    Handles two categories of "invisible" dispatch:

    1. Registration macros (e.g., PROJ_SUBSYSTEM_REGISTER(name)):
       - Expands to __attribute__((constructor)) that adds a struct to a global list
       - An iterator function later walks the list calling a dispatch field
       - The pattern, struct arg index, iterator func, and dispatch field come
         from the profile's macro_dispatch.registration_macros section

    2. Token-paste macros (e.g., PROJ_REGISTER(name) → name##_register):
       - The ## operator concatenates tokens at compile time
       - The generated function name is invisible to regex scanning
       - The template and param_names come from the profile's
         macro_dispatch.token_paste_macros section

    Returns:
        {
            "macro_registrations": [
                {
                    "macro_name": "PROJ_SUBSYSTEM_REGISTER",
                    "struct_var": "g_proj_subsystem_bdev",
                    "source_file": "lib/bdev/bdev.c",
                    "line": 42,
                    "condition": "",
                },
                ...
            ],
            "token_paste_functions": [
                {
                    "macro_name": "PROJ_REGISTER",
                    "generated_name": "bdev_register",
                    "source_file": "lib/bdev/bdev.c",
                    "line": 42,
                },
                ...
            ],
        }
    """
    if pp_conditions is None:
        pp_conditions = []
    if func_names is None:
        func_names = set()

    macro_registrations = []
    token_paste_functions = []

    # --- Registration macro extraction ---
    if macro_dispatch_patterns:
        for pattern_entry in macro_dispatch_patterns:
            macro_name = pattern_entry.get("macro_name", "")
            regex_str = pattern_entry.get("pattern", "")
            struct_arg_index = pattern_entry.get("struct_arg_index", 0)

            if not macro_name or not regex_str:
                continue

            try:
                compiled_re = re.compile(regex_str)
            except re.error:
                continue

            for m in compiled_re.finditer(source):
                # Extract the captured group at struct_arg_index
                try:
                    struct_var = m.group(struct_arg_index + 1)  # group(0) is full match
                except IndexError:
                    continue

                if not struct_var:
                    continue

                # Calculate line number
                line_num = source[:m.start()].count('\n') + 1

                # Get #ifdef condition
                condition = ""
                for cond_start_line, cond_end_line, cond_text in pp_conditions:
                    if cond_start_line <= line_num <= cond_end_line:
                        condition = cond_text
                        break

                macro_registrations.append({
                    "macro_name": macro_name,
                    "struct_var": struct_var,
                    "source_file": rel_path,
                    "line": line_num,
                    "condition": condition,
                })

    # --- Token-paste macro extraction ---
    if token_paste_macros:
        for tp_entry in token_paste_macros:
            macro_name = tp_entry.get("macro_name", "")
            template = tp_entry.get("template", "")
            param_names = tp_entry.get("param_names", [])

            if not macro_name or not template or not param_names:
                continue

            # Build a regex to find invocations of this macro
            # Pattern: MACRO_NAME(arg1, arg2, ...)
            # We need to capture all arguments to substitute into the template
            n_params = len(param_names)
            arg_pattern = r'\s*([^,)]+)\s*'
            invocation_re_str = (
                rf'\b{re.escape(macro_name)}\s*\(' +
                (arg_pattern + r',') * (n_params - 1) +
                arg_pattern + r'\)'
            )

            try:
                invocation_re = re.compile(invocation_re_str)
            except re.error:
                continue

            for m in invocation_re.finditer(source):
                # Extract arguments
                args = []
                for gi in range(1, n_params + 1):
                    try:
                        arg = m.group(gi).strip()
                        args.append(arg)
                    except IndexError:
                        break

                if len(args) != n_params:
                    continue

                # Substitute parameters into template using word-boundary regex
                # to avoid partial matches (e.g., "module" inside
                # "_bdev_module_register" must not be replaced).
                # Sort by length descending so longer names replace first.
                generated = template
                sorted_params = sorted(
                    zip(param_names, args), key=lambda x: -len(x[0])
                )
                for pname, arg_val in sorted_params:
                    generated = re.sub(
                        r'\b' + re.escape(pname) + r'\b', arg_val, generated
                    )

                # Remove ## operators (token paste — concatenation)
                generated = generated.replace('##', '')

                # Clean up double underscores produced by ## removal,
                # but preserve leading/trailing underscores (they are
                # significant in C identifiers, e.g. _bdev_...).
                while '__' in generated:
                    generated = generated.replace('__', '_')
                # Only strip leading/trailing underscores that were
                # artifacts of paste (single leading/trailing _ is valid C)
                # — do NOT strip here; double-underscore cleanup above
                # is sufficient.

                # Calculate line number
                line_num = source[:m.start()].count('\n') + 1

                token_paste_functions.append({
                    "macro_name": macro_name,
                    "generated_name": generated,
                    "source_file": rel_path,
                    "line": line_num,
                })

    return {
        "macro_registrations": macro_registrations,
        "token_paste_functions": token_paste_functions,
    }


# ---------------------------------------------------------------------------
# container_of / struct embedding extraction
# ---------------------------------------------------------------------------

# Default regex for container_of-like macros:
#   MACRO(ptr, struct outer_type, member)
# Handles optional 'struct' keyword before the type name.
_CONTAINER_OF_DEFAULT_RE = re.compile(
    r'(\w+)\s*\(\s*(\w+)\s*,\s*(?:struct\s+)?(\w+)\s*,\s*(\w+)\s*\)',
)

# Regex for static inline conversion wrapper functions:
#   static inline struct outer_type *func_name(struct inner_type *param) { ... container_of ... }
_CONVERSION_FUNC_RE = re.compile(
    r'static\s+inline\s+'
    r'struct\s+(\w+)\s*\*\s*'          # group(1): outer_type
    r'(\w+)\s*\(\s*'                    # group(2): func_name
    r'struct\s+(\w+)\s*\*\s*'           # group(3): inner_type
    r'(\w+)\s*\)\s*\{'                  # group(4): param_name
    r'([^}]*)'                          # group(5): function body
    r'\}',
    re.DOTALL,
)

# Regex for struct type definitions with embedded struct fields:
#   struct outer_type { ... struct inner_type field_name; ... }
_STRUCT_DEF_RE = re.compile(
    r'(?:typedef\s+)?struct\s+(\w+)\s*\{([^}]+)\}',
    re.DOTALL,
)
_EMBEDDED_FIELD_RE = re.compile(
    r'struct\s+(\w+)\s+(\w+)\s*(?:\[\d*\])?\s*;',
)


def _extract_container_of(source: str, rel_path: str,
                           container_of_macros: list = None) -> dict:
    """Extract container_of macro usages and conversion wrapper functions.

    Detects two patterns:

    1. Direct macro calls: CONTAINER_OF(ptr, struct outer_type, member)
       - Extracts: outer_type, member, inner_type (from ptr's implied type)
       - The profile's container_of_macros list specifies which macro names to look for
       - If no macros specified, detects common names: container_of, CONTAINER_OF, etc.

    2. Static inline conversion wrappers:
       static inline struct outer_type *outer_func(struct inner_type *param) {
           return CONTAINER_OF(param, struct outer_type, member);
       }
       - Extracts: outer_type, func_name, inner_type, param_name, member (from body)

    Returns:
        {
            "container_of_usages": [
                {"outer_type": str, "member": str, "inner_type": str, "source_file": str, "line": int},
                ...
            ],
            "conversion_funcs": [
                {"outer_type": str, "inner_type": str, "member": str, "func_name": str, "source_file": str},
                ...
            ],
        }
    """
    container_of_usages = []
    conversion_funcs = []

    # Build set of macro names to detect
    macro_names = set()
    if container_of_macros:
        for entry in container_of_macros:
            name = entry.get("macro_name", "")
            if name:
                macro_names.add(name)
                # Also add common case variants
                macro_names.add(name.upper())
                macro_names.add(name.lower())
    else:
        # Default: detect common container_of macro names
        macro_names = {"container_of", "CONTAINER_OF", "__containerof"}

    # Extract direct macro usages
    for m in _CONTAINER_OF_DEFAULT_RE.finditer(source):
        func = m.group(1)
        if func not in macro_names:
            continue
        ptr_var = m.group(2)
        outer_type = m.group(3)
        member = m.group(4)

        # inner_type is not directly in the macro call; it's inferred from
        # the ptr variable's type which we don't know at scan time.
        # We can still record the outer_type and member.
        line_num = source[:m.start()].count('\n') + 1
        container_of_usages.append({
            "outer_type": outer_type,
            "member": member,
            "ptr_var": ptr_var,
            "inner_type": "",  # unknown at scan time
            "source_file": rel_path,
            "line": line_num,
        })

    # Also try profile-specific patterns that capture inner_type
    if container_of_macros:
        for entry in container_of_macros:
            pat_str = entry.get("pattern", "")
            if not pat_str:
                continue
            try:
                pat = re.compile(pat_str)
            except re.error:
                continue
            for m in pat.finditer(source):
                groups = m.groups()
                if len(groups) >= 3:
                    outer_type = groups[1] if len(groups) > 1 else ""
                    member = groups[2] if len(groups) > 2 else ""
                    line_num = source[:m.start()].count('\n') + 1
                    # Check for duplicates
                    key = (outer_type, member, rel_path, line_num)
                    if not any(u["outer_type"] == outer_type and u["member"] == member
                               and u["line"] == line_num for u in container_of_usages):
                        container_of_usages.append({
                            "outer_type": outer_type,
                            "member": member,
                            "ptr_var": groups[0] if groups[0] else "",
                            "inner_type": "",
                            "source_file": rel_path,
                            "line": line_num,
                        })

    # Extract static inline conversion wrapper functions
    for m in _CONVERSION_FUNC_RE.finditer(source):
        outer_type = m.group(1)
        func_name = m.group(2)
        inner_type = m.group(3)
        param_name = m.group(4)
        body = m.group(5)

        # Extract member from container_of call in body
        member = ""
        for cm in _CONTAINER_OF_DEFAULT_RE.finditer(body):
            cfunc = cm.group(1)
            if cfunc in macro_names:
                member = cm.group(4) if cm.lastindex >= 4 else ""
                break

        conversion_funcs.append({
            "outer_type": outer_type,
            "inner_type": inner_type,
            "member": member,
            "func_name": func_name,
            "param_name": param_name,
            "source_file": rel_path,
        })

    return {
        "container_of_usages": container_of_usages,
        "conversion_funcs": conversion_funcs,
    }


def _extract_struct_embeddings(source: str, rel_path: str) -> dict:
    """Extract struct type definitions with embedded struct fields.

    Parses struct definitions like:
        struct outer_type {
            struct inner_type field;   // embedded struct
            volatile uint32_t *ptr;   // non-struct field (ignored)
        };

    Only records fields that are themselves struct types (embedding pattern).

    Returns:
        {
            "struct_defs": [
                {
                    "struct_type": "outer_type",
                    "fields": [
                        {"field_type": "inner_type", "field_name": "field"},
                        ...
                    ],
                    "source_file": "path/to/file.c",
                },
                ...
            ],
        }
    """
    struct_defs = []

    for m in _STRUCT_DEF_RE.finditer(source):
        struct_type = m.group(1)
        body = m.group(2)

        # Skip anonymous structs or common non-struct-like names
        if not struct_type or len(struct_type) <= 2:
            continue

        # Find embedded struct fields within this struct definition
        fields = []
        for fm in _EMBEDDED_FIELD_RE.finditer(body):
            field_type = fm.group(1)
            field_name = fm.group(2)
            # Skip common non-embedding patterns
            if field_type in ("list_head", "list_node", "rb_node", "hlist_node",
                              "callback", "pthread_mutex", "pthread_cond"):
                continue
            # Skip very short field names (likely not meaningful embeddings)
            if len(field_name) <= 1:
                continue
            fields.append({
                "field_type": field_type,
                "field_name": field_name,
            })

        # Only record structs that have at least one embedded struct field
        if fields:
            struct_defs.append({
                "struct_type": struct_type,
                "fields": fields,
                "source_file": rel_path,
            })

    return {"struct_defs": struct_defs}


def _extract_fn_ptr_calls(body: str, caller_name: str,
                           effective_skip: set) -> list:
    """Extract function pointer calls via struct field access from a function body.

    Detects patterns like:
        dev->ops->submit_request(ioch, dev_io)
        ch->ops->submit_request(dev, io)
        transport->ops.ctrlr_construct(trid, opts, devhandle)  [dot notation]

    Returns list of dicts:
        {
            "field_name": "submit_request",
            "struct_chain": "bdev->fn_table",  # the chain of dereferences
        }

    NOTE: Only filters against _UNIVERSAL_SKIP_NAMES and _TYPE_KEYWORDS, NOT
    the full effective_skip. The skip_names.add list contains struct field names
    (cb_fn, submit_tasks, writev, etc.) that are correctly skipped for function
    definition/call extraction but must NOT be filtered here — they are struct
    field accesses, not standalone function names.
    """
    results = []
    seen = set()
    for m in _FN_PTR_CALL_RE.finditer(body):
        struct_chain = m.group(1)  # e.g., "fn_table" or intermediate
        field_name = m.group(2)    # e.g., "submit_request"

        if field_name in _UNIVERSAL_SKIP_NAMES or field_name in _TYPE_KEYWORDS:
            continue
        if len(field_name) <= 2:
            continue

        key = (struct_chain, field_name)
        if key in seen:
            continue
        seen.add(key)

        results.append({
            "field_name": field_name,
            "struct_chain": struct_chain,
        })

    # Also detect pointer->struct.field() patterns (dot notation for embedded structs)
    # e.g., transport->ops.ctrlr_construct() captures struct_chain="ops", field_name="ctrlr_construct"
    for m in _FN_PTR_CALL_DOT_RE.finditer(body):
        intermediate = m.group(1)  # e.g., "transport"
        struct_name = m.group(2)   # e.g., "ops"
        field_name = m.group(3)    # e.g., "ctrlr_construct"

        if field_name in _UNIVERSAL_SKIP_NAMES or field_name in _TYPE_KEYWORDS:
            continue
        if len(field_name) <= 2:
            continue

        struct_chain = f"{intermediate}->{struct_name}"
        key = (struct_chain, field_name)
        if key in seen:
            continue
        seen.add(key)

        results.append({
            "field_name": field_name,
            "struct_chain": struct_chain,
        })

    return results


# Regex for struct field assignments: var->field = func_name or var.field = func_name
# Captures: group(1)=struct_chain, group(2)=field_name, group(3)=func_name
_FIELD_ASSIGN_RE = re.compile(
    r'(?:^|[\n;{}])\s*'                         # start of statement
    r'(\w+(?:->\w+)*)\s*->\s*'                   # struct_chain-> (e.g., ctx->cb_fn)
    r'(\w+)\s*=\s*'                              # field_name =
    r'([a-zA-Z_]\w*)\s*;'                        # func_name ;
    r'|'                                         # OR
    r'(?:^|[\n;{}])\s*'                          # start of statement
    r'(\w+(?:\.\w+)*)\s*\.\s*'                   # struct_chain. (e.g., ops.submit)
    r'(\w+)\s*=\s*'                              # field_name =
    r'([a-zA-Z_]\w*)\s*;'                        # func_name ;
    r'|'                                         # OR
    r'(?:^|[\n;{}])\s*'                          # start of statement
    r'(\w+(?:(?:->|\.)(?:\w+))+)\s*(?:->|\.)\s*'  # mixed chain (e.g., args->cb_info.)
    r'(\w+)\s*=\s*'                              # field_name =
    r'([a-zA-Z_]\w*)\s*;'                        # func_name ;
)


def _extract_field_assignments(body: str, caller_name: str,
                                func_names: set, effective_skip: set,
                                fp_params_by_func: dict = None) -> list:
    """Extract struct field assignments where a function name is assigned.

    Detects patterns like:
        ctx->cb_fn = my_callback;
        ops.submit_request = my_submit;
        desc->cb_fn = _resize_notify;

    Also detects param-bridged assignments where the value is a function-pointer
    parameter of the enclosing function:
        bdev_io->internal.data_transfer_cpl = cb_fn;  (cb_fn is a parameter)

    Returns list of dicts:
        {
            "field_name": "cb_fn",
            "struct_chain": "ctx",
            "target_func": "my_callback",
            "caller": "setup_callback",
        }
    For param-bridged assignments, additionally includes:
        {
            "is_param": True,
            "param_index": 1,  # 0-based index in caller's signature
        }
    """
    results = []
    seen = set()
    for m in _FIELD_ASSIGN_RE.finditer(body):
        # Handle ->, ., and mixed ->/. patterns (3 alternatives)
        struct_chain = m.group(1) or m.group(4) or m.group(7)
        field_name = m.group(2) or m.group(5) or m.group(8)
        func_name = m.group(3) or m.group(6) or m.group(9)

        if not struct_chain or not field_name or not func_name:
            continue

        if len(func_name) <= 2 or len(field_name) <= 2:
            continue
        if field_name in _UNIVERSAL_SKIP_NAMES or field_name in _TYPE_KEYWORDS:
            continue
        if func_name == caller_name:
            continue

        # Check if the value is a known function name
        is_known_func = func_name in func_names and func_name not in effective_skip

        # Check if the value is a function-pointer parameter of the caller
        is_fp_param = False
        param_index = -1
        if not is_known_func and fp_params_by_func and caller_name in fp_params_by_func:
            if func_name in fp_params_by_func[caller_name]:
                is_fp_param = True
                param_index = fp_params_by_func[caller_name][func_name]

        if not is_known_func and not is_fp_param:
            continue

        key = (struct_chain, field_name, func_name)
        if key in seen:
            continue
        seen.add(key)

        entry = {
            "field_name": field_name,
            "struct_chain": struct_chain,
            "target_func": func_name,
            "caller": caller_name,
        }
        if is_fp_param:
            entry["is_param"] = True
            entry["param_index"] = param_index

        results.append(entry)

    return results


def _make_func_id(func_name: str, rel_path: str) -> str:
    """Create a unique function ID from name and source file."""
    domain = _derive_domain(rel_path)
    return f"{domain}.{func_name}"


def _derive_domain(rel_path: str) -> str:
    """Derive architecture domain from file path.

    Strategy: use path segments to build a meaningful domain.
    E.g., lib/storage/storage.c → storage, lib/storage/disk/disk.c → storage.disk
          module/storage/disk/disk.c → storage.disk
          test/unit/lib/storage/storage_ut.c → unit.storage
          test/unit/lib/storage/disk/disk_ut.c → unit.storage.disk
    """
    parts = rel_path.replace(os.sep, '/').split('/')

    # Skip common prefix directories
    skip_prefixes = {'lib', 'module', 'app', 'include', 'src', 'examples'}
    # After test→unit, also skip 'unit' dir (common pattern: test/unit/lib/...)
    skip_after_unit = {'unit', 'lib', 'module', 'src'}
    domain_parts = []

    i = 0
    while i < len(parts):
        p = parts[i]
        # Skip the final source file segment (contains extension like .c/.h)
        if '.' in p and not p.startswith('.') and i == len(parts) - 1:
            break
        # Skip intermediate path segments that look like file names
        # (contain a dot but aren't the last segment). These are directory
        # names like "raid1.c" in test/unit/lib/bdev/raid/raid1.c/raid1_ut.c
        # — structural noise, not meaningful domain components.
        if '.' in p and not p.startswith('.') and i < len(parts) - 1:
            i += 1
            continue
        # Skip common prefix dirs
        if p in skip_prefixes and not domain_parts:
            i += 1
            continue
        # Handle test directories specially
        if p == 'test' and not domain_parts:
            domain_parts.append('unit')
            i += 1
            # Skip 'unit' and other structural dirs after test
            while i < len(parts) and parts[i] in skip_after_unit:
                i += 1
            continue
        domain_parts.append(p)
        if len(domain_parts) >= 3:
            break
        i += 1

    return '.'.join(domain_parts) if domain_parts else 'root'

def _disambiguate_func_ids(functions: list, edges: list) -> tuple:
    """Disambiguate duplicate function IDs by extending domains.

    When multiple functions share the same domain.name ID (e.g., multiple
    main() functions in the same domain), extend the domain with additional
    path segments derived from the source file path.

    Also remaps edge source IDs to match the new function IDs.

    Returns (functions, edges) with all IDs unique.
    """
    id_counts = Counter(f["id"] for f in functions)
    dup_ids = {fid for fid, count in id_counts.items() if count > 1}

    if not dup_ids:
        return functions, edges

    # Build old_id -> [(source_file, function)] mapping
    dup_funcs = defaultdict(list)  # old_id -> [(source_file, func_index)]
    for i, f in enumerate(functions):
        if f["id"] in dup_ids:
            dup_funcs[f["id"]].append((f.get("source_file", ""), i))

    # For each duplicate ID, create unique new IDs
    old_to_new = {}  # (old_id, source_file) -> new_id
    used_ids = set(f["id"] for f in functions if f["id"] not in dup_ids)

    for old_id, entries in dup_funcs.items():
        # Group by source_file
        file_groups = defaultdict(list)
        for src_file, idx in entries:
            file_groups[src_file].append(idx)

        for src_file, indices in file_groups.items():
            parts = src_file.replace(os.sep, '/').split('/')
            # Build extended domain from all meaningful path segments
            skip_dirs = {'lib', 'module', 'app', 'include', 'src', 'examples',
                         'test', 'build', 'obj'}
            meaningful = [p for p in parts[:-1] if p not in skip_dirs
                          and not p.startswith('.')
                          and '.' not in p]  # skip dot-containing dirs like "raid1.c"
            file_base = os.path.splitext(parts[-1])[0] if parts else ''

            func_name = old_id.split('.')[-1]  # extract function name from old_id

            if len(file_groups) == 1:
                # All duplicates in same file (e.g., macro expansions)
                # Use file basename as disambiguator + numeric suffix
                for suffix_num, idx in enumerate(indices, 1):
                    if suffix_num == 1:
                        new_id = f"{'.'.join(meaningful[-2:] + [file_base])}.{func_name}" if meaningful else f"{file_base}.{func_name}"
                    else:
                        new_id = f"{'.'.join(meaningful[-2:] + [file_base])}.{func_name}_{suffix_num}" if meaningful else f"{file_base}.{func_name}_{suffix_num}"
                    # Ensure truly unique
                    base_new_id = new_id
                    counter = 2
                    while new_id in used_ids:
                        new_id = f"{base_new_id}_{counter}"
                        counter += 1
                    old_to_new[(old_id, src_file, idx)] = new_id
                    used_ids.add(new_id)
            else:
                # Different files — use file-specific path as disambiguator
                if meaningful:
                    new_domain = '.'.join(meaningful[-3:])
                else:
                    new_domain = file_base
                new_id = f"{new_domain}.{func_name}"
                # Ensure unique
                base_new_id = new_id
                counter = 2
                while new_id in used_ids:
                    new_id = f"{base_new_id}_{counter}"
                    counter += 1
                for idx in indices:
                    old_to_new[(old_id, src_file, idx)] = new_id
                    used_ids.add(new_id)

    # Apply remapping to functions
    for i, f in enumerate(functions):
        key = (f["id"], f.get("source_file", ""), i)
        if key in old_to_new:
            new_id = old_to_new[key]
            f["id"] = new_id
            f["domain"] = '.'.join(new_id.split('.')[:-1])

    # Build per-file mapping: for edges, we need to know which new_id
    # corresponds to the old_id in a specific file context.
    # Since edges don't carry source_file, we build a mapping from
    # (old_id) -> {new_id} but only when the mapping is unambiguous
    # within a single file's scan result.
    # For ambiguous cases, we use the function name + domain to match.

    # Build old_id -> set of possible new_ids
    old_id_to_new_ids = defaultdict(set)
    for (old_id, src_file, idx), new_id in old_to_new.items():
        old_id_to_new_ids[old_id].add(new_id)

    # Build (old_id, new_id) -> source_file mapping
    new_id_to_src = {}
    for (old_id, src_file, idx), new_id in old_to_new.items():
        new_id_to_src[new_id] = src_file

    # For edges: use _source_file to disambiguate which function instance
    # produced each edge. Build (old_id, source_file) -> new_id mapping.
    file_based_remap = {}  # (old_id, source_file) -> new_id
    for (old_id, src_file, idx), new_id in old_to_new.items():
        file_based_remap[(old_id, src_file)] = new_id

    # Also build old_id -> default new_id (first one) for edges without _source_file
    old_id_default_new = {}
    for old_id, entries in dup_funcs.items():
        first_src_file = entries[0][0]
        key = (old_id, first_src_file)
        if key in file_based_remap:
            old_id_default_new[old_id] = file_based_remap[key]

    # Build name -> list of function IDs mapping for target disambiguation
    name_to_ids = defaultdict(list)  # func_name -> [func_id, ...]
    for f in functions:
        name_to_ids[f["name"]].append(f["id"])

    # Build func_id -> source_file mapping
    id_to_src = {}
    for f in functions:
        id_to_src[f["id"]] = f.get("source_file", "")

    # Build (source_file, func_name) -> func_id for same-file resolution
    file_name_to_id = {}
    for f in functions:
        key = (f.get("source_file", ""), f["name"])
        file_name_to_id[key] = f["id"]

    # Directory priority for disambiguation: prefer production code over tests/headers
    _DIR_PRIORITY = {'lib': 1, 'module': 2, 'app': 3, 'examples': 4,
                     'include': 5, 'src': 6, 'vendor': 7, 'test': 8, 'unit': 9}

    def _pick_best_target(possible_ids, caller_file, invoked_nameb):
        """Pick the best target from ambiguous candidates using heuristics."""
        # 1. Same file as caller
        same_file_id = file_name_to_id.get((caller_file, invoked_nameb))
        if same_file_id and same_file_id in possible_ids:
            return same_file_id

        # 2. Same directory as caller
        caller_dir = '/'.join(caller_file.split('/')[:-1]) if caller_file else ''
        for pid in possible_ids:
            pid_src = id_to_src.get(pid, '')
            pid_dir = '/'.join(pid_src.split('/')[:-1])
            if pid_dir == caller_dir:
                return pid

        # 2.5. Same domain tier as caller: if caller is in test/unit/fuzz,
        # prefer targets in test/unit/fuzz directories over production.
        # This prevents test mocks from being resolved to production functions
        # when both exist with the same name.
        _TEST_DIRS = frozenset(('test', 'unit', 'ut', 'fuzz'))
        caller_is_test = caller_file and any(
            p in _TEST_DIRS for p in caller_file.replace(os.sep, '/').split('/'))
        if caller_is_test:
            test_targets = []
            for pid in possible_ids:
                pid_src = id_to_src.get(pid, '')
                if any(p in _TEST_DIRS for p in pid_src.replace(os.sep, '/').split('/')):
                    test_targets.append(pid)
            if test_targets:
                # Among test targets, prefer same sub-domain
                # e.g., caller in test/unit/lib/accel/ should prefer
                # target in test/unit/lib/accel/ over test/unit/lib/bdev/
                for pid in test_targets:
                    pid_src = id_to_src.get(pid, '')
                    pid_dir = '/'.join(pid_src.replace(os.sep, '/').split('/')[:-1])
                    if pid_dir == caller_dir:
                        return pid
                # Among multiple test targets, prefer the one whose source path
                # shares the longest common prefix with the caller's path.
                # This avoids resolving unit.bdev callers to huawei.test.unit targets.
                caller_parts = caller_file.replace(os.sep, '/').split('/')
                # If caller is in a standard test tree (starts with test/),
                # prefer targets also in the standard test tree over vendor
                # subtrees (e.g., huawei/test/). This prevents callers in
                # test/event/ from resolving to huawei/test/unit/vhost/ mocks
                # when a test/unit/bdev/ mock is available.
                caller_root = caller_parts[0]
                if caller_root in ('test', 'unit', 'ut'):
                    same_root = [pid for pid in test_targets
                                 if id_to_src.get(pid, '').replace(os.sep, '/').startswith(caller_root + '/')]
                    if same_root:
                        test_targets = same_root
                best_pid = None
                best_overlap = -1
                for pid in test_targets:
                    pid_src = id_to_src.get(pid, '')
                    pid_parts = pid_src.replace(os.sep, '/').split('/')
                    # Count shared path components from the start
                    overlap = 0
                    for cp, pp in zip(caller_parts, pid_parts):
                        if cp == pp:
                            overlap += 1
                        else:
                            break
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_pid = pid
                return best_pid

        # 3. Prefer production code (lib/module) over tests/headers
        best_id = None
        best_priority = 99
        for pid in possible_ids:
            pid_src = id_to_src.get(pid, '')
            parts = pid_src.replace(os.sep, '/').split('/')
            for p in parts:
                if p in _DIR_PRIORITY:
                    pri = _DIR_PRIORITY[p]
                    if pri < best_priority:
                        best_priority = pri
                        best_id = pid
                    break
        if best_id:
            return best_id

        # 4. Prefer .c over .h (definition over declaration)
        for pid in possible_ids:
            pid_src = id_to_src.get(pid, '')
            if pid_src.endswith(('.c', '.cpp', '.cc')):
                return pid

        # 5. No resolution — return first
        return possible_ids[0] if possible_ids else None

    for edge in edges:
        # Fix source (caller) ID
        src = edge["source"]
        if src in dup_ids:
            src_file = edge.get("_source_file", "")
            if src_file and (src, src_file) in file_based_remap:
                edge["source"] = file_based_remap[(src, src_file)]
            elif src in old_id_default_new:
                edge["source"] = old_id_default_new[src]

        # Fix target (callee) — resolve bare name to disambiguated func ID
        # Skip FN_PTR edges: their targets are struct field names (dynamic dispatch),
        # not specific function names. Resolving them to a single function is wrong
        # because the actual implementation depends on the vtable registration.
        target = edge.get("target", "")
        if target and '.' not in target and edge.get("concurrency", "") != "fn_ptr":
            # Target is a bare name — try to resolve to a specific func ID
            possible_ids = name_to_ids.get(target, [])
            if len(possible_ids) == 1:
                # Unambiguous: only one function with this name
                edge["target"] = possible_ids[0]
            elif len(possible_ids) > 1:
                # Ambiguous: use heuristics to pick best target
                src_file = edge.get("_source_file", "")
                best_id = _pick_best_target(possible_ids, src_file, target)
                if best_id:
                    edge["target"] = best_id
                # else: leave as bare name — consumer must handle ambiguity

        # Remove internal tracking fields
        for _tf in ("_source_file", "_passthrough_via", "_struct_field_arg"):
            if _tf in edge:
                del edge[_tf]

    return functions, edges


def _detect_cross_file_callbacks(all_functions: list, edges: list,
                                  source_root: str = '',
                                  passthrough_reg_funcs: dict = None,
                                  field_assignments: list = None,
                                  skip_callees: set = None) -> list:
    """Post-processing step to detect callback arguments across file boundaries.

    The per-file CALLBACK_ARG detection only finds callbacks defined in the
    same file as the caller. This function uses the global func_names set to
    find callbacks that were missed because they're defined in a different file.

    Uses a two-pass approach, unified over a single file traversal:
      Pass 1 (registration-driven): For functions that call known registration
        functions (from _known_reg_funcs), extract ALL function pointer arguments.
      Pass 2 (suffix-driven): For functions NOT covered by Pass 1, extract
        function pointer arguments that have callback-like suffixes.

    Files are read and parsed only once, with both passes sharing a cache.
    Requires source_root to re-read source files for body extraction.

    Args:
        skip_callees: Set of callee names to skip (from profile's
            callback_detection.skip_callees + built-in _CALLBACK_ARG_SKIP_CALLEES).
            If None, uses _CALLBACK_ARG_SKIP_CALLEES only.
    """
    if not source_root:
        return edges

    global_func_names = {f['name'] for f in all_functions}
    name_to_ids = {}
    id_to_func = {}
    for f in all_functions:
        name_to_ids.setdefault(f['name'], []).append(f['id'])
        id_to_func[f['id']] = f

    # Build effective skip_callees set from parameter + built-in
    _effective_skip_callees = skip_callees if skip_callees is not None else _CALLBACK_ARG_SKIP_CALLEES

    # Build set of caller_ids that already have callback edges.
    # Edges may use either (source, target) or legacy (caller, callee) keys;
    # normalize here so the dedup set is consistent.
    callback_sources = set()
    for e in edges:
        if e.get('concurrency') == 'callback':
            src = e.get('source') or e.get('caller') or ''
            if src:
                callback_sources.add(src)

    # Build existing edge key set for dedup
    seen_edge_keys = set()
    for e in edges:
        src = e.get('source') or e.get('caller') or ''
        tgt = e.get('target') or e.get('callee') or ''
        key = (src, tgt, e.get('call_condition', ''))
        seen_edge_keys.add(key)

    # Use profile's known registration functions (populated by scan_c_file)
    reg_func_names = _known_reg_funcs

    # Passthrough registration functions
    pt_funcs = passthrough_reg_funcs or {}

    # Callback-like suffixes for Pass 2, and contains patterns for Pass 1
    _cross_cb_suffixes = ("_cb", "_fn", "_handler", "_callback",
                          "_complete", "_done", "_cpl", "_comp",
                          "_completion", "_cmpl", "_resubmit", "_finish")
    _cross_cb_contains = ("_complete_", "_done_", "_cpl_", "_completion_", "_cmpl_")

    # Build set of function names that have function-pointer parameters.
    _cross_fp_param_re = re.compile(r'(?:\w+::)*(\w+_fn|\w+_cb|\w+_handler|\w+_callback)\s+\w+')
    _cross_file_fp_params = set()
    for func in all_functions:
        sig = func.get('signature', '')
        if _cross_fp_param_re.search(sig):
            _cross_file_fp_params.add(func['name'])

    # Build edges_by_source for candidate lookup. Normalize legacy
    # caller/callee keys to source/target so the rest of this function
    # can rely on edges_by_source keyed by the invoker id.
    edges_by_source = {}
    for e in edges:
        src = e.get('source') or e.get('caller') or ''
        if not src:
            continue
        edges_by_source.setdefault(src, []).append(e)

    # Build field_assignments lookup for struct-field callback resolution.
    _fa_by_field_struct = {}
    _fa_by_field_flat = {}
    _fa_by_field_domain = {}
    _fa_caller_domain = {}
    for func in all_functions:
        fname = func.get("name", "")
        fdomain = func.get("domain", "")
        if fname and fdomain:
            _fa_caller_domain[fname] = fdomain
    for fa in (field_assignments or []):
        fn = fa.get("field_name", "")
        sc = fa.get("struct_chain", "")
        tf = fa.get("target_func", "")
        caller_name = fa.get("caller", "")
        if fn and tf:
            _fa_by_field_struct.setdefault(fn, {}).setdefault(sc, set()).add(tf)
            _fa_by_field_flat.setdefault(fn, set()).add(tf)
            caller_domain = _fa_caller_domain.get(caller_name, "")
            if caller_domain:
                _fa_by_field_domain.setdefault(fn, {}).setdefault(caller_domain, set()).add(tf)

    # Generic struct names that are too ambiguous for standalone matching
    _GENERIC_STRUCT_CHAINS = frozenset({
        'ctx', 'req', 'base', 'op', 'args', 'data', 'entry',
        'obj', 'handle', 'ptr', 'buf', 'result', 'ret',
    })

    def _resolve_struct_field_arg(sf_struct, sf_field, invoker_id):
        """Resolve struct->field argument to set of target func names."""
        if sf_field not in _fa_by_field_struct:
            return None
        struct_map = _fa_by_field_struct[sf_field]
        caller_domain = ""
        if invoker_id in id_to_func:
            caller_domain = id_to_func.get(invoker_id, {}).get("domain", "")
        elif "." in invoker_id:
            caller_domain = invoker_id.rsplit(".", 1)[0]

        if sf_struct in struct_map and sf_struct not in _GENERIC_STRUCT_CHAINS:
            return struct_map[sf_struct]
        if sf_struct in struct_map and sf_struct in _GENERIC_STRUCT_CHAINS:
            if caller_domain and sf_field in _fa_by_field_domain:
                domain_targets = _fa_by_field_domain[sf_field].get(caller_domain, set())
                struct_targets = struct_map[sf_struct]
                intersection = struct_targets & domain_targets
                if intersection and len(intersection) <= 5:
                    return intersection
        if caller_domain and sf_field in _fa_by_field_domain:
            domain_targets = _fa_by_field_domain[sf_field].get(caller_domain, set())
            if domain_targets and len(domain_targets) <= 5:
                return domain_targets
        if len(struct_map) == 1:
            return next(iter(struct_map.values()))
        return None

    _STRUCT_FIELD_ARG_RE = re.compile(r'^(\w+)->(\w+)$')
    new_edges = []
    _CB_ARG_CALL_RE = re.compile(r'\b(\w+)\s*\(')

    # ------------------------------------------------------------------
    # Classify functions into Pass 1 vs Pass 2 candidates, grouped by file
    # ------------------------------------------------------------------
    pass1_candidates_by_file = {}
    pass2_candidates_by_file = {}
    pass1_func_ids = set()

    for func in all_functions:
        func_id = func['id']
        if func_id in callback_sources or func.get('is_empty', False):
            continue

        src_edges = edges_by_source.get(func_id, [])
        calls_reg = any(
            (lambda t: (t.split('.')[-1] if '.' in t else t) in reg_func_names
                       or (t.split('.')[-1] if '.' in t else t) in pt_funcs)(
                e.get('target') or e.get('callee') or ''
            )
            for e in src_edges
        )

        src_file = func.get('source_file', '')
        if not src_file:
            continue

        if calls_reg:
            pass1_candidates_by_file.setdefault(src_file, []).append(func)
            pass1_func_ids.add(func_id)
        else:
            pass2_candidates_by_file.setdefault(src_file, []).append(func)

    # Limit Pass 2 candidates to prevent runaway processing on huge codebases.
    _PASS2_MAX_CANDIDATES = 10000
    total_pass2 = sum(len(v) for v in pass2_candidates_by_file.values())
    if total_pass2 > _PASS2_MAX_CANDIDATES:
        sorted_files = sorted(pass2_candidates_by_file.items(),
                              key=lambda x: len(x[1]), reverse=True)
        pass2_candidates_by_file = {}
        count = 0
        for sf, cands in sorted_files:
            if count >= _PASS2_MAX_CANDIDATES:
                break
            pass2_candidates_by_file[sf] = cands
            count += len(cands)
        skipped_pass2 = total_pass2 - count
        if skipped_pass2 > 0:
            print(f"[cross-cb] Pass 2: limited to {_PASS2_MAX_CANDIDATES} candidates "
                  f"(skipped {skipped_pass2} low-density candidates)", file=sys.stderr)

    # ------------------------------------------------------------------
    # Single-pass file processing: read each file once, process both Pass 1
    # and Pass 2 candidates from that file
    # ------------------------------------------------------------------
    all_candidate_files = set(pass1_candidates_by_file.keys()) | set(pass2_candidates_by_file.keys())
    _file_cache = {}  # src_file -> file_source string
    _func_defs_cache = {}  # src_file -> list of func_defs
    _func_bodies_cache = {}  # src_file -> dict of func_name -> body_info

    total_files = len(all_candidate_files)
    processed_files = 0

    for src_file in all_candidate_files:
        abs_path = os.path.join(source_root, src_file)
        if not os.path.isfile(abs_path):
            continue

        # Read and parse file once
        try:
            if src_file not in _file_cache:
                with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
                    _file_cache[src_file] = f.read()
            file_source = _file_cache[src_file]
        except (IOError, OSError):
            continue

        # Extract all func defs and bodies once per file
        candidate_names = set()
        for c in pass1_candidates_by_file.get(src_file, []):
            candidate_names.add(c['name'])
        for c in pass2_candidates_by_file.get(src_file, []):
            candidate_names.add(c['name'])

        if src_file not in _func_defs_cache:
            _func_defs_cache[src_file] = [
                fd for fd in _extract_func_defs(file_source, src_file, skip_names_add=[])
                if fd['name'] in candidate_names
            ]
        file_func_defs = _func_defs_cache[src_file]

        if src_file not in _func_bodies_cache:
            _func_bodies_cache[src_file] = _extract_func_bodies(file_source, file_func_defs)
        file_bodies = _func_bodies_cache[src_file]

        # Process Pass 1 candidates for this file
        for func in pass1_candidates_by_file.get(src_file, []):
            func_id = func['id']
            func_name = func['name']
            body_info = file_bodies.get(func_name)
            if not body_info:
                continue
            body = _strip_comments(body_info['body'])

            for m in _CB_ARG_CALL_RE.finditer(body):
                invoked_nameb = m.group(1)
                if invoked_nameb in ('if', 'for', 'while', 'switch', 'return', 'sizeof',
                                   'typeof', 'case', 'else', 'do') or len(invoked_nameb) <= 2:
                    continue

                is_passthrough = invoked_nameb in pt_funcs
                if invoked_nameb not in reg_func_names and not is_passthrough:
                    continue

                # Extract argument list
                rest = body[m.end():]
                depth = 1
                args_text = ''
                for ch in rest:
                    if ch == '(':
                        depth += 1
                        args_text += ch
                    elif ch == ')':
                        depth -= 1
                        if depth == 0:
                            break
                        args_text += ch
                    else:
                        args_text += ch

                # For passthrough functions, only extract the arg at cb_arg_index
                if is_passthrough:
                    pt_info = pt_funcs[invoked_nameb]
                    cb_idx = pt_info.get("cb_arg_index", 0)
                    pt_concurrency = pt_info.get("concurrency_type", "callback")
                    arg_list = args_text.split(',')
                    if cb_idx < len(arg_list):
                        arg = arg_list[cb_idx].strip().lstrip('&*').strip()

                        sf_pt = _STRUCT_FIELD_ARG_RE.match(arg)
                        if sf_pt:
                            sf_struct_pt = sf_pt.group(1)
                            sf_field_pt = sf_pt.group(2)
                            sf_targets_pt = _resolve_struct_field_arg(sf_struct_pt, sf_field_pt, func_id)
                            if sf_targets_pt:
                                for tf_pt in sf_targets_pt:
                                    if tf_pt not in global_func_names:
                                        continue
                                    tf_ids_pt = name_to_ids.get(tf_pt, [])
                                    if not tf_ids_pt:
                                        continue
                                    tf_id_pt = tf_ids_pt[0]
                                    edge_key_pt = (func_id, tf_id_pt, '')
                                    if edge_key_pt not in seen_edge_keys:
                                        has_direct_pt = any(
                                            e.get('target') == tf_id_pt or
                                            e.get('target', '').endswith('.' + tf_pt)
                                            for e in edges_by_source.get(func_id, [])
                                        )
                                        if not has_direct_pt:
                                            seen_edge_keys.add(edge_key_pt)
                                            new_edges.append({
                                                "source": func_id,
                                                "target": tf_id_pt,
                                                "call_order": 0,
                                                "call_condition": "",
                                                "confidence": "CALLBACK_ARG",
                                                "source_tag": "ast",
                                                "concurrency": pt_concurrency,
                                                "evidence": f"crossfile_passthrough: {func_name} -> {invoked_nameb}() {sf_struct_pt}->{sf_field_pt}={tf_pt}",
                                                "_passthrough_via": invoked_nameb,
                                                "_struct_field_arg": f"{sf_struct_pt}->{sf_field_pt}",
                                                "_source_file": src_file,
                                            })
                        elif (_BARE_IDENT_RE.match(arg)
                                and arg in global_func_names and len(arg) > 2
                                and arg != func_name
                                and arg not in _CALLBACK_ARG_GENERIC_NAMES):
                            target_ids = name_to_ids.get(arg, [])
                            if target_ids:
                                target_id = target_ids[0]
                                edge_key = (func_id, target_id, '')
                                if edge_key not in seen_edge_keys:
                                    has_direct = any(
                                        e.get('target') == target_id or
                                        e.get('target', '').endswith('.' + arg)
                                        for e in edges_by_source.get(func_id, [])
                                    )
                                    if not has_direct:
                                        seen_edge_keys.add(edge_key)
                                        new_edges.append({
                                            "source": func_id,
                                            "target": target_id,
                                            "call_order": 0,
                                            "call_condition": "",
                                            "confidence": "CALLBACK_ARG",
                                            "source_tag": "ast",
                                            "concurrency": pt_concurrency,
                                            "evidence": f"crossfile_passthrough: {func_name} -> {invoked_nameb}() arg={arg}",
                                            "_passthrough_via": invoked_nameb,
                                            "_source_file": src_file,
                                        })
                    continue

                # Check each argument against global func_names
                for arg in args_text.split(','):
                    arg = arg.strip().lstrip('&*').strip()

                    sf_match = _STRUCT_FIELD_ARG_RE.match(arg)
                    if sf_match:
                        sf_struct = sf_match.group(1)
                        sf_field = sf_match.group(2)
                        sf_targets = _resolve_struct_field_arg(sf_struct, sf_field, func_id)
                        if sf_targets:
                            for tf_name in sf_targets:
                                if tf_name not in global_func_names:
                                    continue
                                tf_ids = name_to_ids.get(tf_name, [])
                                if not tf_ids:
                                    continue
                                tf_id = tf_ids[0]
                                edge_key = (func_id, tf_id, '')
                                if edge_key in seen_edge_keys:
                                    continue
                                has_direct = any(
                                    e.get('target') == tf_id or
                                    e.get('target', '').endswith('.' + tf_name)
                                    for e in edges_by_source.get(func_id, [])
                                )
                                if has_direct:
                                    continue
                                seen_edge_keys.add(edge_key)
                                new_edges.append({
                                    "source": func_id,
                                    "target": tf_id,
                                    "call_order": 0,
                                    "call_condition": "",
                                    "confidence": "CALLBACK_ARG",
                                    "source_tag": "ast",
                                    "concurrency": "callback",
                                    "evidence": f"crossfile_struct_field: {func_name} -> {invoked_nameb}() {sf_struct}->{sf_field}={tf_name}",
                                    "_struct_field_arg": f"{sf_struct}->{sf_field}",
                                    "_source_file": src_file,
                                })
                        continue

                    if not _BARE_IDENT_RE.match(arg):
                        continue
                    if arg not in global_func_names or len(arg) <= 2:
                        continue
                    if arg == func_name:
                        continue
                    if arg in _CALLBACK_ARG_GENERIC_NAMES:
                        continue

                    target_ids = name_to_ids.get(arg, [])
                    if not target_ids:
                        continue
                    target_id = target_ids[0]
                    edge_key = (func_id, target_id, '')
                    if edge_key in seen_edge_keys:
                        continue
                    has_direct = any(
                        e.get('target') == target_id or e.get('target', '').endswith('.' + arg)
                        for e in edges_by_source.get(func_id, [])
                    )
                    if has_direct:
                        continue
                    seen_edge_keys.add(edge_key)
                    cb_concurrency = "callback"
                    new_edges.append({
                        "source": func_id,
                        "target": target_id,
                        "call_order": 0,
                        "call_condition": "",
                        "confidence": "CALLBACK_ARG",
                        "source_tag": "ast",
                        "concurrency": cb_concurrency,
                        "evidence": f"crossfile_reg_callback: {func_name} -> {invoked_nameb}() arg={arg}",
                        "_source_file": src_file,
                    })

        # Process Pass 2 candidates for this file
        for func in pass2_candidates_by_file.get(src_file, []):
            func_id = func['id']
            func_name = func['name']
            body_info = file_bodies.get(func_name)
            if not body_info:
                continue
            body = _strip_comments(body_info['body'])

            for m in _CB_ARG_CALL_RE.finditer(body):
                invoked_nameb = m.group(1)
                if invoked_nameb in ('if', 'for', 'while', 'switch', 'return', 'sizeof',
                                   'typeof', 'case', 'else', 'do') or len(invoked_nameb) <= 2:
                    continue
                if invoked_nameb in _effective_skip_callees:
                    continue

                rest = body[m.end():]
                depth = 1
                args_text = ''
                for ch in rest:
                    if ch == '(':
                        depth += 1
                        args_text += ch
                    elif ch == ')':
                        depth -= 1
                        if depth == 0:
                            break
                        args_text += ch
                    else:
                        args_text += ch

                for arg in args_text.split(','):
                    arg = arg.strip().lstrip('&*').strip()

                    sf_match2 = _STRUCT_FIELD_ARG_RE.match(arg)
                    if sf_match2:
                        sf_struct2 = sf_match2.group(1)
                        sf_field2 = sf_match2.group(2)
                        sf_targets2 = _resolve_struct_field_arg(sf_struct2, sf_field2, func_id)
                        if sf_targets2:
                            for tf_name2 in sf_targets2:
                                if tf_name2 not in global_func_names:
                                    continue
                                tf_ids2 = name_to_ids.get(tf_name2, [])
                                if not tf_ids2:
                                    continue
                                tf_id2 = tf_ids2[0]
                                edge_key2 = (func_id, tf_id2, '')
                                if edge_key2 in seen_edge_keys:
                                    continue
                                source_edges2 = edges_by_source.get(func_id, [])
                                has_direct2 = any(
                                    e.get('target') == tf_id2 or
                                    e.get('target', '').endswith('.' + tf_name2)
                                    for e in source_edges2
                                )
                                if has_direct2:
                                    continue
                                seen_edge_keys.add(edge_key2)
                                if invoked_nameb in reg_func_names:
                                    sf_concurrency = "callback"
                                elif sf_field2.endswith(_cross_cb_suffixes):
                                    sf_concurrency = "callback"
                                elif invoked_nameb in _cross_file_fp_params:
                                    sf_concurrency = "callback"
                                else:
                                    sf_concurrency = ""
                                new_edges.append({
                                    "source": func_id,
                                    "target": tf_id2,
                                    "call_order": 0,
                                    "call_condition": "",
                                    "confidence": "CALLBACK_ARG",
                                    "source_tag": "ast",
                                    "concurrency": sf_concurrency,
                                    "evidence": f"crossfile_suffix_struct_field: {func_name} -> {invoked_nameb}() {sf_struct2}->{sf_field2}={tf_name2}",
                                    "_struct_field_arg": f"{sf_struct2}->{sf_field2}",
                                    "_source_file": src_file,
                                })
                        continue

                    if not _BARE_IDENT_RE.match(arg):
                        continue
                    if arg not in global_func_names or len(arg) <= 2:
                        continue
                    if arg == func_name:
                        continue
                    if arg in _CALLBACK_ARG_GENERIC_NAMES:
                        continue

                    _PASS2_SKIP_ARGS = frozenset({
                        'poller', 'channel', 'io_device', 'bdev', 'desc',
                        'ctrlr', 'qpair', 'req', 'buf', 'ctx', 'data',
                    })
                    if arg in _PASS2_SKIP_ARGS:
                        continue

                    target_ids = name_to_ids.get(arg, [])
                    if not target_ids:
                        continue
                    target_id = target_ids[0]
                    edge_key = (func_id, target_id, '')
                    if edge_key in seen_edge_keys:
                        continue
                    has_direct = any(
                        e.get('target') == target_id or e.get('target', '').endswith('.' + arg)
                        for e in edges_by_source.get(func_id, [])
                    )
                    if has_direct:
                        continue
                    seen_edge_keys.add(edge_key)

                    if invoked_nameb in reg_func_names:
                        cb_concurrency = "callback"
                    elif arg.endswith(_cross_cb_suffixes) or any(kw in arg for kw in _cross_cb_contains):
                        cb_concurrency = "callback"
                    elif invoked_nameb in _cross_file_fp_params:
                        cb_concurrency = "callback"
                    else:
                        cb_concurrency = ""

                    new_edges.append({
                        "source": func_id,
                        "target": target_id,
                        "call_order": 0,
                        "call_condition": "",
                        "confidence": "CALLBACK_ARG",
                        "source_tag": "ast",
                        "concurrency": cb_concurrency,
                        "evidence": f"crossfile_suffix_callback: {func_name} -> {invoked_nameb}() arg={arg}",
                        "_source_file": src_file,
                    })

        processed_files += 1
        if processed_files % 500 == 0:
            print(f"[cross-cb] Processed {processed_files}/{total_files} files, "
                  f"{len(new_edges)} new edges found", file=sys.stderr)

    edges.extend(new_edges)

    # Clean up internal tracking fields from cross-file callback edges
    for e in edges:
        for _tf in ("_source_file", "_passthrough_via", "_struct_field_arg"):
            if _tf in e:
                del e[_tf]

    return edges


def scan_directory(source_root: str, languages: list = None) -> dict:
    """Scan all C/C++ files in a directory tree."""
    all_functions = []
    all_edges = []
    all_vtable_registrations = []
    all_macro_registrations = []
    all_token_paste_functions = []
    all_container_of_usages = []
    all_conversion_funcs = []
    all_struct_defs = []
    all_fn_ptr_calls = {}
    all_field_assignments = []  # Aggregate struct field assignments
    all_passthrough_reg_funcs = {}  # Aggregate passthrough registration functions
    c_extensions = {'.c', '.h', '.cpp', '.hpp', '.cc', '.cxx'}

    file_count = 0
    # Directories to skip during scanning (generated/build artifacts, VCS, dependencies)
    _SKIP_DIRS = frozenset({
        '__pycache__', 'node_modules', '.git', '.svn', '.hg',
        'build', 'dist', 'out', 'bin', 'obj',
        'venv', '.venv', '.env',
        '.tox', '.mypy_cache', '.pytest_cache',
        'target', 'CMakeFiles', 'cmake-build-debug', 'cmake-build-release',
        '.cache',
    })
    for dirpath, dirnames, filenames in os.walk(source_root):
        dirnames[:] = [d for d in dirnames if not d.startswith('.') and d not in _SKIP_DIRS]

        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in c_extensions:
                continue

            filepath = os.path.join(dirpath, fname)
            result = scan_c_file(filepath, source_root)
            all_functions.extend(result["functions"])
            all_edges.extend(result["edges"])
            file_count += 1

            # Aggregate vtable registrations
            vtable_regs = result.get("vtable_registrations", [])
            if vtable_regs:
                all_vtable_registrations.extend(vtable_regs)

            # Aggregate macro registrations
            macro_regs = result.get("macro_registrations", [])
            if macro_regs:
                all_macro_registrations.extend(macro_regs)
            tp_funcs = result.get("token_paste_functions", [])
            if tp_funcs:
                all_token_paste_functions.extend(tp_funcs)

            # Aggregate container_of usages and struct embedding data
            co_usages = result.get("container_of_usages", [])
            if co_usages:
                all_container_of_usages.extend(co_usages)
            conv_funcs = result.get("conversion_funcs", [])
            if conv_funcs:
                all_conversion_funcs.extend(conv_funcs)
            s_defs = result.get("struct_defs", [])
            if s_defs:
                all_struct_defs.extend(s_defs)

            # Aggregate fn_ptr_calls
            fn_ptr_calls = result.get("fn_ptr_calls", {})
            for caller, calls in fn_ptr_calls.items():
                if caller not in all_fn_ptr_calls:
                    all_fn_ptr_calls[caller] = []
                all_fn_ptr_calls[caller].extend(calls)

            # Aggregate field assignments
            fa = result.get("field_assignments", [])
            if fa:
                all_field_assignments.extend(fa)

            # Aggregate passthrough registration functions
            pt_funcs = result.get("passthrough_reg_funcs", {})
            for func_name, info in pt_funcs.items():
                if func_name not in all_passthrough_reg_funcs:
                    all_passthrough_reg_funcs[func_name] = info

    # Deduplicate edges. Normalize legacy caller/callee keys to source/target
    # so dedup is consistent regardless of which scanner emitted the edge.
    seen_edges = set()
    unique_edges = []
    for edge in all_edges:
        src = edge.get("source") or edge.get("caller") or ""
        tgt = edge.get("target") or edge.get("callee") or ""
        if not src or not tgt:
            continue
        key = (src, tgt, edge.get("call_condition", ""))
        if key not in seen_edges:
            seen_edges.add(key)
            # Ensure both source/target and caller/callee are populated so
            # downstream consumers using either convention see consistent data.
            edge["source"] = src
            edge["target"] = tgt
            edge.setdefault("caller", src)
            edge.setdefault("callee", tgt)
            unique_edges.append(edge)

    # Disambiguate duplicate function IDs
    # When multiple functions share the same domain.name ID, extend the domain
    # with additional path segments to make each ID unique.
    all_functions, unique_edges = _disambiguate_func_ids(all_functions, unique_edges)

    # Post-processing: detect cross-file callback arguments.
    # The per-file CALLBACK_ARG detection only finds callbacks defined in the
    # same file as the caller. This step uses the global func_names set to
    # find callbacks passed as arguments across file boundaries.
    unique_edges = _detect_cross_file_callbacks(all_functions, unique_edges,
                                                 source_root=source_root,
                                                 passthrough_reg_funcs=all_passthrough_reg_funcs,
                                                 field_assignments=all_field_assignments)

    return {
        "functions": all_functions,
        "edges": unique_edges,
        "project": os.path.basename(os.path.abspath(source_root)),
        "file_count": file_count,
        "scanner": "regex_fallback",
        "vtable_registrations": all_vtable_registrations,
        "macro_registrations": all_macro_registrations,
        "token_paste_functions": all_token_paste_functions,
        "container_of_usages": all_container_of_usages,
        "conversion_funcs": all_conversion_funcs,
        "struct_defs": all_struct_defs,
        "fn_ptr_calls": all_fn_ptr_calls,
        "field_assignments": all_field_assignments,
    }
