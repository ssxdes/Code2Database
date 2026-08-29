"""callgraph builder module: import_resolve."""

import os
import re
from collections import defaultdict
import networkx as nx
import logging

# Module-level compiled regex (hoisted from _resolve_imports)
_ASM_EXTERN_RE = re.compile(
    r'^\s*(?:extern|\.globl|\.global)\s+([a-zA-Z_]\w*)', re.MULTILINE)

# Hoisted static regexes for per-file import scanning (Finding 25).
# These were bare-string re.finditer calls compiled 30K+ times on kernel.
_INCLUDE_RE = re.compile(r'^\s*#\s*include\s+[<"]([^>"]+)[>"]', re.MULTILINE)
_PY_IMPORT_PAREN_RE = re.compile(r'\bimport\s+(?:\(([^)]*)\)|"([^"]+)")')
_PY_INNER_STR_RE = re.compile(r'"([^"]+)"')
_PY_FROM_IMPORT_RE = re.compile(r'^\s*(?:from\s+(\S+)\s+import|import\s+(\S+))', re.MULTILINE)
_RUST_USE_RE = re.compile(r'^\s*use\s+([\w:]+)', re.MULTILINE)
_RUST_IMPORT_SEMI_RE = re.compile(r'^\s*import\s+([\w.]+)\s*;', re.MULTILINE)

# Cache for file-level includes (path → set of includes)
_file_includes_cache = {}


def _resolve_imports(G, source_root: str) -> int:
    """Resolve cross-file calls through #include import chains.

    For C/C++ projects, scans header files to build a header→function mapping.
    Then resolves call targets through import chains when direct name matching
    fails. Adds INFERRED edges for resolved calls.

    Returns the number of new edges added.
    """
    # Scan for #include directives from source files
    include_map = {}  # source_file → [header_paths]
    header_functions = {}  # header_name → [function_names]

    # Collect #include directives from source text
    _INCLUDE_RE = re.compile(r'^\s*#\s*include\s+[<"]([^>"]+)[>"]', re.MULTILINE)

    # Read source files directly for file-level includes — #include directives
    # live at file scope, not inside function body_text.
    source_files_with_includes = set()
    _INCLUDE_FILE_RE = re.compile(r'^\s*#\s*include\s+[<"]([^>"]+)[>"]', re.MULTILINE)
    _MAX_SOURCE_SIZE = 500_000  # Skip source files > 500KB
    source_exts = {'.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.hxx', '.hh', '.h++',
                   '.go', '.py', '.java', '.rs', '.s', '.S', '.asm'}
    for nid, nd in G.nodes(data=True):
        if nd.get("is_empty", False):
            continue
        source_file = nd.get("source_file", "")
        if not source_file:
            continue
        if source_file in include_map:
            continue  # Already scanned
        # Read the source file directly for #include directives
        full_path = os.path.join(source_root, source_file)
        try:
            if os.path.getsize(full_path) > _MAX_SOURCE_SIZE:
                continue
            with open(full_path, 'r', errors='replace') as f:
                content = f.read()
        except (IOError, OSError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue
        file_includes = []
        for m in _INCLUDE_FILE_RE.finditer(content):
            header = m.group(1)
            file_includes.append(header)

        # ASM-specific: detect extern/.globl/.global declarations and
        # #include in .S files (preprocessed by cpp)
        asm_ext = os.path.splitext(source_file)[1].lower()
        if asm_ext in ('.s', '.asm'):
            for m in _ASM_EXTERN_RE.finditer(content):
                file_includes.append(("asm_extern", m.group(1)))
            # .S files also support #include (preprocessed by cpp) —
            # already captured by _INCLUDE_FILE_RE above

        if file_includes:
            include_map[source_file] = file_includes
            source_files_with_includes.add(source_file)

    if not include_map:
        return 0

    # Collect the set of headers we actually need to scan
    needed_headers = set()
    for headers in include_map.values():
        for h in headers:
            # Skip ASM extern entries (stored as tuples)
            if isinstance(h, tuple):
                continue
            needed_headers.add(h)
            needed_headers.add(os.path.basename(h))

    # Scan only header files that are referenced in include_map
    _DECL_KW = frozenset(('if', 'while', 'for', 'switch', 'return',
                           'sizeof', 'typedef', 'struct', 'enum',
                           'class', 'namespace', 'using', 'define',
                           'defined', 'include', 'ifdef', 'ifndef',
                           'endif', 'pragma', 'elif', 'else', 'case',
                           'default', 'break', 'continue', 'goto', 'do'))
    # Simple non-backtracking pattern: word boundary + identifier + open paren
    _DECL_RE = re.compile(r'\b([A-Za-z_]\w*)\s*\(', re.MULTILINE)
    _MAX_HEADER_SIZE = 1_000_000  # Skip headers > 1MB

    header_exts = {'.h', '.hpp', '.hxx', '.hh', '.h++'}
    for dirpath, dirnames, filenames in os.walk(source_root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith('.') and d not in ('build', '__pycache__', 'node_modules')
                       and not d.startswith('cmake-build-')]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in header_exts:
                continue
            # Skip headers not referenced by any source file
            if fname not in needed_headers:
                rel_check = None
                # Check if any needed header path ends with this filename's directory combo
                found = False
                for nh in needed_headers:
                    if nh.endswith('/' + fname) or nh == fname:
                        found = True
                        break
                if not found:
                    continue

            fpath = os.path.join(dirpath, fname)
            # Skip large header files
            try:
                if os.path.getsize(fpath) > _MAX_HEADER_SIZE:
                    continue
            except OSError:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                continue
            rel_path = os.path.relpath(fpath, source_root)
            # Also skip if we already scanned this header
            if rel_path in header_functions:
                continue

            try:
                with open(fpath, 'r', errors='replace') as f:
                    text = f.read()
            except (IOError, OSError):
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                continue
            funcs = []
            for m in _DECL_RE.finditer(text):
                name = m.group(1)
                if len(name) < 2 or name in _DECL_KW:
                    continue
                # Filter out all-uppercase names that are likely macros
                if name.isupper() and len(name) > 2:
                    continue
                # Filter out names starting with uppercase that look like type names
                # but keep _ prefixed names (e.g., _internal_func)
                if name[0].isupper() and '_' not in name and not name.startswith('_'):
                    continue
                funcs.append(name)

            if funcs:
                header_functions[rel_path] = funcs
                basename = os.path.basename(rel_path)
                if basename not in header_functions:
                    header_functions[basename] = list(funcs)
                else:
                    header_functions[basename].extend(funcs)

    if not header_functions:
        return 0

    # Build function→source_file mapping from graph
    func_by_name = defaultdict(list)  # lowercase_name → [(nid, domain)]
    for nid, nd in G.nodes(data=True):
        if nd.get("is_empty", False):
            continue
        name = nd.get("name", "")
        if name:
            func_by_name[name.lower()].append((nid, nd.get("domain", "root")))

    # Pre-compute lowercase header function sets for O(1) lookup
    # (avoids O(F) list comprehension per edge per header)
    header_funcs_lower = {}
    for hname, funcs in header_functions.items():
        header_funcs_lower[hname] = {f.lower() for f in funcs}

    # Resolve unresolved call targets through import chains
    new_edges = 0
    seen_unresolved = set()
    for u, v, edata in list(G.edges(data=True)):
        target_name = G.nodes[v].get("name", "") if v in G else ""
        # If the target is an external/unresolved node and we have function info.
        # External endpoints use domains like 'ext.rte', 'ext.ibv' — check prefix.
        v_domain = G.nodes[v].get("domain", "") if v in G else ""
        is_external = (v_domain == "external" or v_domain.startswith("ext."))
        if is_external and target_name:
            # Try to find this function in headers included by the caller
            caller_file = G.nodes[u].get("source_file", "") if u in G else ""
            target_lower = target_name.lower()
            if caller_file and caller_file in include_map:
                for header in include_map[caller_file]:
                    # Handle ASM extern entries (stored as tuples)
                    if isinstance(header, tuple) and header[0] == "asm_extern":
                        # Direct symbol import — try to resolve by name
                        sym_name = header[1].lower()
                        if sym_name == target_lower:
                            candidates = func_by_name.get(target_lower, [])
                            if candidates:
                                real_nid = candidates[0][0]
                                if real_nid != v and not G.has_edge(u, real_nid):
                                    G.add_edge(u, real_nid,
                                               call_order=edata.get("call_order"),
                                               call_condition=edata.get("call_condition", ""),
                                               concurrency=edata.get("concurrency", ""),
                                               confidence="INFERRED",
                                               source="asm_extern_resolution",
                                               confidence_score=0.75,
                                               evidence=[{"kind": "asm_extern",
                                                           "weight": 0.75,
                                                           "note": f"resolved via asm extern {header[1]}"}])
                                    new_edges += 1
                        continue

                    # O(1) set lookup instead of O(F) list scan
                    hfuncs_lower = header_funcs_lower.get(header)
                    if hfuncs_lower and target_lower in hfuncs_lower:
                        # Found in header — check if there's a real node for it
                        candidates = func_by_name.get(target_lower, [])
                        if candidates:
                            # Replace external edge with edge to real node
                            real_nid = candidates[0][0]
                            if real_nid != v and not G.has_edge(u, real_nid):
                                G.add_edge(u, real_nid,
                                           call_order=edata.get("call_order"),
                                           call_condition=edata.get("call_condition", ""),
                                           concurrency=edata.get("concurrency", ""),
                                           confidence="INFERRED",
                                           source="import_resolution",
                                           confidence_score=0.75,
                                           evidence=[{"kind": "import_resolution",
                                                       "weight": 0.75,
                                                       "note": f"resolved via #include <{header}>"}])
                                new_edges += 1

    return new_edges


def _compute_fqn(nd: dict, project_name: str = "") -> str:
    """Compute Fully Qualified Name: project.domain.module.function.

    Derives module from source_file path.
    """
    domain = nd.get("domain", "root")
    name = nd.get("name", "")
    source_file = nd.get("source_file", "")

    # Derive module from source file path
    module = ""
    if source_file:
        # Remove extension and convert path separators to dots
        base = os.path.splitext(source_file)[0]
        module = base.replace("/", ".").replace(os.sep, ".")

    parts = [p for p in [project_name, domain, module, name] if p]
    return ".".join(parts)


def _build_resolve_lookups(G: nx.DiGraph) -> tuple:
    """Pre-build lookup structures for _multi_strategy_resolve.

    Returns (name_to_ids, file_to_ids, domain_to_ids).
    Call once before a batch of resolve calls, then pass the result in.
    """
    name_to_ids = defaultdict(list)
    file_to_ids = defaultdict(list)
    domain_to_ids = defaultdict(list)
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        n = ndata.get("name", "")
        n_lower = n.lower()
        name_to_ids[n_lower].append(nid)
        sf = ndata.get("source_file", "")
        if sf:
            file_to_ids[sf].append((nid, n_lower))
        dom = ndata.get("domain", "root")
        domain_to_ids[dom].append((nid, n_lower))
    return (name_to_ids, file_to_ids, domain_to_ids)


def _get_file_includes(source_file: str, source_root: str) -> set:
    """Extract #include/import/use directives from a source file (cached).

    Reads the actual source file, not function body_text — #include is a
    file-scope directive, so scanning body_text would miss it.
    """
    if not source_file or not source_root:
        return set()
    cache_key = f"{source_root}:{source_file}"
    if cache_key in _file_includes_cache:
        return _file_includes_cache[cache_key]

    includes = set()
    full_path = os.path.join(source_root, source_file)
    try:
        with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except (IOError, OSError):
        _file_includes_cache[cache_key] = includes
        return includes

    # C/C++: #include <header> or #include "header"
    for m in _INCLUDE_RE.finditer(content):
        includes.add(m.group(1))
    # Go: import "pkg" or import ( "pkg1" "pkg2" )
    for m in _PY_IMPORT_PAREN_RE.finditer(content):
        if m.group(2):
            includes.add(m.group(2))
        elif m.group(1):
            for inner_m in _PY_INNER_STR_RE.finditer(m.group(1)):
                includes.add(inner_m.group(1))
    # Python: import X / from X import Y
    for m in _PY_FROM_IMPORT_RE.finditer(content):
        includes.add(m.group(1) or m.group(2))
    # Java: import pkg.Class;
    for m in _RUST_IMPORT_SEMI_RE.finditer(content):
        includes.add(m.group(1))
    # Rust: use path::module;
    for m in _RUST_USE_RE.finditer(content):
        includes.add(m.group(1))

    _file_includes_cache[cache_key] = includes
    return includes


def _multi_strategy_resolve(G: nx.DiGraph, callee_name: str, invoker_id: str,
                             id_registry: dict = None,
                             _lookups: tuple = None,
                             source_root: str = "",
                             suffix_index: dict = None) -> tuple:
    """Resolve a callee name using prioritized multi-strategy chain.

    Strategies (highest confidence first):
    1. same_file: callee defined in same source file as caller
    2. import_map: callee reachable via caller's #include chain
    3. same_domain: callee in same architecture domain
    4. suffix_match: callee ID ends with _<normalized_name>
    5. unique_name: callee name is unique across the whole graph
    6. fuzzy: partial name match (lowest confidence)

    Args:
        _lookups: Optional pre-built (name_to_ids, file_to_ids, domain_to_ids)
                  from _build_resolve_lookups(). Pass this when calling
                  _multi_strategy_resolve in a loop to avoid O(N) rebuild per call.
        source_root: Source root directory. Used by import_map strategy to
                  read source files directly for #include extraction.
        suffix_index: Optional pre-built suffix index from _build_suffix_index().
                  When provided, Strategy 4 uses O(1) lookup instead of O(N) scan.

    Returns (resolved_id, strategy, confidence) or ("", "unresolved", 0.0).
    """
    if not callee_name:
        return ("", "unresolved", 0.0)

    caller_nd = G.nodes.get(invoker_id, {})
    caller_file = caller_nd.get("source_file", "")
    caller_domain = caller_nd.get("domain", "root")

    norm_name = re.sub(r'[^a-z0-9_]', '_', callee_name.lower())

    # Use pre-built lookups or build them (slow path for one-off calls)
    if _lookups:
        name_to_ids, file_to_ids, domain_to_ids = _lookups
    else:
        name_to_ids, file_to_ids, domain_to_ids = _build_resolve_lookups(G)

    callee_lower = callee_name.lower()

    # Strategy 1: same_file
    if caller_file and caller_file in file_to_ids:
        for nid, n_lower in file_to_ids[caller_file]:
            if n_lower == callee_lower:
                return (nid, "same_file", 0.95)

    # Strategy 2: import_map (check if callee's source is imported by caller)
    # Read source file directly for file-level includes; #include is file-scope, not body-scope.
    caller_includes = _get_file_includes(caller_file, source_root) if source_root else set()

    if caller_includes:
        # Use pre-built file_to_ids for O(1) lookup instead of O(N) scan
        for inc in caller_includes:
            # Direct match
            if inc in file_to_ids:
                for nid, n_lower in file_to_ids[inc]:
                    if n_lower == callee_lower:
                        return (nid, "import_map", 0.85)
            # Basename match (e.g., inc="lib/bdev.h" matches sf="bdev.h")
            inc_base = os.path.basename(inc)
            for sf, entries in file_to_ids.items():
                if os.path.basename(sf) == inc_base:
                    for nid, n_lower in entries:
                        if n_lower == callee_lower:
                            return (nid, "import_map", 0.85)
            # Python/Java: import matches module path
            inc_path = inc.replace(".", "/")
            for sf, entries in file_to_ids.items():
                if sf.endswith(inc_path + ".py") or sf.endswith(inc_path + ".java"):
                    for nid, n_lower in entries:
                        if n_lower == callee_lower:
                            return (nid, "import_map", 0.85)

    # Strategy 3: same_domain
    if caller_domain in domain_to_ids:
        for nid, n_lower in domain_to_ids[caller_domain]:
            if n_lower == callee_lower:
                return (nid, "same_domain", 0.75)

    # Strategy 4: suffix_match on node IDs
    # Use pre-built suffix_index for O(1) lookup when available,
    # otherwise fall back to O(N) scan of id_registry
    if suffix_index:
        matches = suffix_index.get(norm_name, [])
        if len(matches) == 1:
            return (matches[0], "suffix_match", 0.60)
        elif len(matches) > 1:
            # Prefer matches in same domain
            for mid in matches:
                if mid in G:
                    mid_dom = G.nodes[mid].get("domain", "")
                    if mid_dom and mid_dom == caller_domain:
                        return (mid, "suffix_match", 0.60)
            # Return first match
            return (matches[0], "suffix_match", 0.60)
    elif id_registry:
        suffix = "_" + norm_name
        for full_id in id_registry:
            if full_id.endswith(suffix):
                return (full_id, "suffix_match", 0.60)

    # Strategy 5: unique_name
    matches = name_to_ids.get(callee_lower, [])
    if len(matches) == 1:
        return (matches[0], "unique_name", 0.55)

    # Strategy 6: fuzzy — partial name match
    if matches:
        # Prefer matches in same domain
        same_domain_matches = [
            nid for nid in matches
            if G.nodes[nid].get("domain", "root") == caller_domain
        ]
        if same_domain_matches:
            return (same_domain_matches[0], "fuzzy_same_domain", 0.40)
        return (matches[0], "fuzzy", 0.30)

    return ("", "unresolved", 0.0)


