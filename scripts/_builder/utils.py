"""callgraph builder module: utils."""

import hashlib
import os
import json
import sys
import re
from pathlib import Path
from collections import defaultdict
import networkx as nx
import logging
from typing import Optional, List, Set


# Cache source_root per graph_dir to avoid re-reading master.json on every
# describe-node / get-source / get-source query. Invalidated only on graph reload.
_SOURCE_ROOT_CACHE: dict = {}

# Domains derive from source directory paths but can also be set through
# node supplements (update-node, community merges): a component carrying
# path separators, an absolute prefix, control characters or hundreds of
# bytes would escape the output tree or break NAME_MAX (255). Unicode
# word characters survive; everything else maps to '_'; over-budget
# components truncate with a short hash so distinct domains stay
# distinct.
_DOMAIN_COMPONENT_SAFE_RE = re.compile(r'[^\w\-]', re.UNICODE)
_DOMAIN_COMPONENT_MAX_BYTES = 120


def _safe_domain_component(component: str) -> str:
    """Filesystem-safe single path component for a domain piece."""
    safe = _DOMAIN_COMPONENT_SAFE_RE.sub('_', (component or "").strip())
    if not safe or safe.strip('_') == "":
        safe = "root"
    if len(safe.encode('utf-8')) > _DOMAIN_COMPONENT_MAX_BYTES:
        digest = hashlib.md5(
            component.encode('utf-8', 'replace')).hexdigest()[:8]
        head = safe.encode('utf-8')[:_DOMAIN_COMPONENT_MAX_BYTES - 9]\
            .decode('utf-8', 'ignore')
        safe = head + '_' + digest
    return safe


def open_write_conn(db_path, timeout=10.0, row_factory=None):
    """sqlite3 connection for code that writes rows.

    The store schemas declare foreign keys (edges -> functions,
    memories -> categories), but sqlite enforces them per connection
    and only when PRAGMA foreign_keys=ON — a bare connect() silently
    accepts orphan rows. Row-writing call sites go through here so the
    declared constraints hold. The pragma runs before any transaction
    starts, as sqlite requires. Bulk build paths keep their own
    connections: they sequence whole-table loads and swap pages via
    the backup API, where per-row enforcement only slows the load.
    """
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.execute("PRAGMA foreign_keys=ON")
    if row_factory is not None:
        conn.row_factory = row_factory
    return conn


def _format_step_exit(rc: int) -> str:
    """Human-readable exit descriptor for subprocess-step logs.

    Negative return codes mean the step died from a signal: -9 is the
    Linux OOM killer, which otherwise shows up as an unexplained failure
    with no stderr output — the user has to dig through dmesg to find
    out. Normal exits render unchanged ("exit 2").
    """
    if rc >= 0:
        return "exit %d" % rc
    try:
        import signal
        sig = signal.Signals(-rc)
    except ValueError:
        return "killed by signal %d" % (-rc)
    desc = "killed by %s" % sig.name
    if -rc == int(signal.SIGKILL):
        desc += " (likely the OOM killer — check dmesg / journalctl)"
    return desc


def resolve_source_root(graph_dir: str) -> str:
    """Derive the project source root for a graph dir.

    Reads source_root from code2database_master.json (which the build
    wrote with the actual source path), falling back to the parent dir
    of graph_dir (the legacy .code2database subdir convention). The
    parent-dir heuristic alone is wrong when the graph dir is not a
    direct subdirectory of the source tree (e.g. a graph built from
    /home/user/proj stored at /tmp/graphs/proj) — it marks every
    manifest file as 'deleted' in freshness checks.
    """
    cached = _SOURCE_ROOT_CACHE.get(graph_dir)
    if cached is not None:
        return cached
    master_path = os.path.join(graph_dir, "code2database_master.json")
    src_root = ""
    if os.path.exists(master_path):
        try:
            master = json.loads(Path(master_path).read_text(encoding="utf-8"))
            src_root = master.get("source_root", "") or ""
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            src_root = ""
    if not src_root:
        src_root = os.path.dirname(os.path.abspath(graph_dir)) or ""
    _SOURCE_ROOT_CACHE[graph_dir] = src_root
    return src_root


def _fts5_escape(query: str) -> str:
    """Escape a free-form query string for FTS5 MATCH.

    FTS5 query syntax treats special chars (:", *, (, ), etc.) as
    operators. For user-typed queries we want lenient token matching:
    split on whitespace, quote each token, join with AND. This way
    "how does bdev register" matches documents containing all three
    tokens (in any order, any distance). Shared by the kb and cgdb
    search paths so both accept the same free-form input safely.

    For CJK text the ``unicode61`` tokenizer folds contiguous CJK
    runs into single tokens, so a pure-CJK query like ``释放`` would
    escape to ``""`` (match nothing) under the Latin-only regex.
    When the query contains CJK ideographs we additionally segment
    them with :func:`_cjk_tokenize` so the escaped MATCH expression
    carries the same word boundaries used on the write side
    (``body_tokenized`` column).  This makes FTS5 MATCH work
    natively for CJK without relying on the Jaccard fallback.
    """
    tokens = re.findall(r'[A-Za-z0-9_]+', query)
    if _has_cjk(query):
        tokens.extend(_cjk_tokenize(query))
    if not tokens:
        return '""'  # match nothing safely
    return " ".join(f'"{t}"' for t in tokens)


def normalize_str_field(value) -> str:
    """Coerce a node-data field value to a string.

    Scanners store some fields (e.g. ``api_constraints``) as ``[]``
    (an empty list) or a list of strings when non-empty. Downstream
    code that calls ``.lower()``, ``.replace()``, or ``" ".join()``
    on these values raises ``AttributeError`` / ``TypeError``.

    This helper normalizes:
      - ``None`` → ``""``
      - ``[]`` (empty list) → ``""``
      - ``["a", "b"]`` → ``"a, b"``
      - ``""`` / ``"text"`` → unchanged
      - other types → ``str(value)``
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, str):
        return value
    return str(value)


def resolve_source_file(file_path: str, graph_dir: str) -> str:
    """Resolve a possibly-relative source file path against the graph's
    source_root, read once from code2database_master.json and cached.

    cgdb_files.path and functions.source_file may both be relative (the
    scanner stores relative paths to keep the graph portable across
    machines). At query time, we resolve them against source_root so
    open(file_path) works regardless of the query process's cwd.

    Falls back to:
      1. file_path as-is if it's already absolute and exists.
      2. source_root + file_path (relative join).
      3. graph_dir's parent dir as a fallback source_root (common project
         layout: graph_dir is a subdirectory of source_root, e.g.
         /proj/.code2database/).
    Returns the original file_path if no resolution succeeds (caller's
    open() will then raise OSError naturally).

    Path-traversal protection: if file_path (or its
    resolved form) escapes source_root via '..' segments, the resolved
    path is rejected. This prevents a malicious code2database.db with
    source_file='../../../etc/passwd' from tricking an MCP tool into
    reading arbitrary files outside the project tree.
    """
    if not file_path:
        return file_path
    # Path-traversal protection: if file_path (or its
    # resolved form) escapes source_root via '..' segments, the resolved
    # path is rejected. This prevents a malicious code2database.db with
    # source_file='../../../etc/passwd' from tricking an MCP tool into
    # reading arbitrary files outside the project tree.
    source_root = resolve_source_root(graph_dir)

    def _contained(candidate: str) -> bool:
        """True iff candidate path is inside source_root (after realpath)."""
        if not candidate or not source_root:
            return False
        try:
            cand_norm = os.path.realpath(candidate)
            root_norm = os.path.realpath(source_root)
            # Ensure root ends with sep so '/proj' doesn't match '/project'.
            if not root_norm.endswith(os.sep):
                root_norm += os.sep
            if not cand_norm.startswith(root_norm):
                logging.getLogger(__name__).warning(
                    "resolve_source_file: rejecting path outside source_root: "
                    "%r (resolved %r, root %r)", file_path, cand_norm, root_norm)
                return False
        except (OSError, ValueError):
            return False
        return True

    # 1. Absolute path that exists AND is contained in source_root:
    #    return as-is (preserves the 'portable absolute path' case).
    if os.path.isabs(file_path) and os.path.exists(file_path):
        if _contained(file_path):
            return file_path
        # Absolute path outside source_root — refuse.
        # Fall through to source_root-relative resolution below.

    # 2. Relative path: join with source_root, check existence + containment.
    if source_root:
        if not os.path.isabs(file_path):
            cand = os.path.join(source_root, file_path)
            if os.path.exists(cand) and _contained(cand):
                return cand
        else:
            # Absolute path that wasn't contained: try basename under
            # source_root as a last resort (handles the case where a path
            # was stored with a different root prefix).
            cand = os.path.join(source_root, os.path.basename(file_path))
            if os.path.exists(cand) and _contained(cand):
                return cand

    # 3. Fallback: return the original file_path only if it's a simple
    #    relative filename (no '..' segments, not absolute) — these pose
    #    no traversal risk and the caller's open() will fail naturally if
    #    the file doesn't exist. Absolute paths and anything containing
    #    '..' are NOT returned as-is, to prevent path traversal.
    if not os.path.isabs(file_path) and '..' not in file_path.split(os.sep):
        return file_path
    return ""


def _ensure_mutable_graph(G, command_name: str = "this command", reason: str = ""):
    """Detect LazySQLiteGraph (read-only SQLite view) early and exit with
    a clear, actionable error.

    Used by commands that mutate the graph (nx.compose, G.nodes[nid][...] = ...)
    or that would otherwise materialize a full in-memory structure over every
    node of a multi-million-node graph. When the project is large
    (>=50K functions), _load_full_graph returns LazySQLiteGraph, which has no
    `.graph` attribute (breaks nx.compose) and rejects item assignment
    (breaks per-node writes). Without this check the user sees a cryptic
    AttributeError deep in networkx — or an unexplained OOM kill.

    Call this immediately after _load_full_graph in any command path that
    uses nx.compose, mutates node attrs, or builds a whole-graph in-memory
    index. ``reason`` overrides the default explanation for commands whose
    failure mode is memory exhaustion rather than graph mutation.
    """
    if type(G).__name__ != "LazySQLiteGraph":
        return
    if not reason:
        reason = ("this command uses in-memory nx.compose + per-node writes,\n"
                  "          but LazySQLiteGraph is a read-only SQLite view "
                  "with no\n"
                  "          .graph attribute and rejects item assignment.")
    print(
        f"Error: '{command_name}' is not supported on SQLite-backed large "
        f"graphs\n"
        f"  Loaded graph: {G.number_of_nodes()} nodes via LazySQLiteGraph "
        f"(db: {getattr(G, '_db_path', '?')})\n"
        f"  Reason: {reason}\n"
        "Alternatives:\n"
        "  1. Run 'daemon-start' for real-time incremental sync (cgdb\n"
        "     incremental path, designed for SQLite-backed large graphs).\n"
        "  2. Run 'build' for a full rebuild from scratch.\n"
        "  3. Force eager load by removing the code2database.db file first\n"
        "     (NOTE: may OOM for >100K-function projects).",
        file=sys.stderr)
    sys.exit(2)


def _streaming_json_lookup(json_path: str, key: str, max_size_mb: int = 200):
    """Look up a single key in a large JSON object file via streaming parse.

    For SQLite-built graphs, .code2database_concurrency_index.json can be
    500+MB. Loading it with json.loads() twice per query (as describe-node did)
    takes minutes for 1.5M nodes. This helper uses ijson to stream-parse only
    until the target key is found, returning its value without materializing
    the rest of the file.

    Args:
        json_path: Path to a JSON object file (top-level keys → values).
        key: Top-level key to look up.
        max_size_mb: Skip streaming (return None) if file exceeds this size.

    Returns:
        The value for `key`, or None if not found.
    """
    try:
        import ijson
    except ImportError:
        # Fallback: full load (slow for large files)
        size_mb = os.path.getsize(json_path) / (1024 * 1024)
        if size_mb > max_size_mb:
            return None  # Refuse to load huge file without streaming
        try:
            data = json.loads(Path(json_path).read_text(encoding="utf-8"))
            return data.get(key)
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            return None

    try:
        with open(json_path, "rb") as f:
            parser = ijson.parse(f, use_float=True)
            in_target = False
            target_depth = 0
            for prefix, event, value in parser:
                if not in_target:
                    # Look for our key at the top level
                    if event == "map_key" and prefix == "" and value == key:
                        in_target = True
                        target_depth = 0
                else:
                    # We're inside the target value — collect until we exit
                    if event in ("start_map", "start_array"):
                        target_depth += 1
                    elif event in ("end_map", "end_array"):
                        target_depth -= 1
                        if target_depth <= 0:
                            # We need to capture the value, but ijson doesn't
                            # give us the parsed value directly. Use items().
                            break
            # If we found the key, re-stream and use ijson.items() to get value
            if in_target:
                with open(json_path, "rb") as f2:
                    items = ijson.items(f2, f"{key}")
                    for item in items:
                        return item
            return None
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        return None


def _streaming_json_has_keys(json_path: str, keys: list) -> dict:
    """Check which of `keys` exist at the top level of a JSON object file.

    Uses streaming parse to avoid loading the entire file. Returns a dict
    mapping each key to a boolean (True if present).

    describe-node uses .get("thread_entries", []) and .get("concurrent_groups", [])
    on .code2database_concurrency_index.json. For SQLite builds, this file is
    node_id-keyed and does NOT have those keys. We need to detect this without
    loading 500+MB.
    """
    result = {k: False for k in keys}
    found_count = 0
    try:
        import ijson
        with open(json_path, "rb") as f:
            parser = ijson.parse(f, use_float=True)
            for prefix, event, value in parser:
                if event == "map_key" and prefix == "" and value in result:
                    if not result[value]:
                        result[value] = True
                        found_count += 1
                        if found_count >= len(keys):
                            break
                # Stop early after enough top-level keys examined
                # (heuristic: if first 50 keys don't match, format is wrong)
                if event == "map_key" and prefix == "" and value not in result:
                    # Track count of unrelated top-level keys seen
                    if not hasattr(_streaming_json_has_keys, "_unrelated_count"):
                        _streaming_json_has_keys._unrelated_count = 0
                    _streaming_json_has_keys._unrelated_count += 1
                    if _streaming_json_has_keys._unrelated_count > 50:
                        _streaming_json_has_keys._unrelated_count = 0
                        break
            _streaming_json_has_keys._unrelated_count = 0
    except ImportError:
        # Fallback: full load
        try:
            size_mb = os.path.getsize(json_path) / (1024 * 1024)
            if size_mb > 200:
                return result  # All False — refuse to load huge file
            data = json.loads(Path(json_path).read_text(encoding="utf-8"))
            for k in keys:
                result[k] = k in data
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    return result


def _experience_dir(graph_dir: str) -> str:
    d = os.path.join(graph_dir, "memory", "experience")
    os.makedirs(d, exist_ok=True)
    return d




def _extract_chain_node_ids(chains: list) -> list:
    """Extract all node IDs referenced in chain data."""
    node_ids = set()
    for chain in chains:
        for step in chain.get("steps", []):
            nid = step.get("id", "")
            if nid:
                node_ids.add(nid)
        from_id = chain.get("from", chain.get("from_api", ""))
        to_id = chain.get("to", chain.get("to_endpoint", ""))
        if from_id:
            node_ids.add(from_id)
        if to_id:
            node_ids.add(to_id)
    return sorted(node_ids)




def _find_node_id(G, partial_id: str) -> str:
    """Find a node ID by exact or partial match.

    For LazySQLiteGraph (large graphs), uses SQL indexes for O(log N) lookup.
    For NetworkX-like graphs, falls back to linear scan (small graph).
    """
    if partial_id in G:
        return partial_id
    # Fast path: SQL-backed lookup for LazySQLiteGraph
    cls_name = type(G).__name__
    if cls_name == "LazySQLiteGraph":
        conn = getattr(G, "_conn", None)
        if conn is not None:
            # 1. Exact name match (fastest)
            row = conn.execute(
                "SELECT id FROM functions WHERE name = ? LIMIT 1",
                (partial_id,)).fetchone()
            if row:
                return row[0]
            # 2. Name prefix match (uses index)
            row = conn.execute(
                "SELECT id FROM functions WHERE name LIKE ? || '%' LIMIT 1",
                (partial_id,)).fetchone()
            if row:
                return row[0]
            # 3. ID substring match (slower but bounded by LIMIT)
            row = conn.execute(
                "SELECT id FROM functions WHERE id LIKE '%' || ? || '%' LIMIT 1",
                (partial_id,)).fetchone()
            if row:
                return row[0]
            # 4. Name substring match
            row = conn.execute(
                "SELECT id FROM functions WHERE name LIKE '%' || ? || '%' LIMIT 1",
                (partial_id,)).fetchone()
            if row:
                return row[0]
            return ""
    # Fallback: linear scan — use streaming (data=True) to avoid N+1
    # query pattern on LazySQLiteGraph (was: for nid in G.nodes: then
    # G.nodes[nid] = separate SELECT per node = 1.5M queries).
    for nid, ndata in G.nodes(data=True):
        if partial_id in nid or ndata.get("name", "") == partial_id:
            return nid
    # Search by name prefix — also streaming
    for nid, ndata in G.nodes(data=True):
        if ndata.get("name", "").startswith(partial_id):
            return nid
    return ""


def _get_body_text(G, nid: str) -> str:
    """Get body_text for a node, lazy-loading from SQLite if needed.

    On LazySQLiteGraph, body_text is not stored in cached attrs (to avoid
    zlib.decompress cost on every node fetch). Use this helper to fetch it
    on demand. On NetworkX-like graphs, returns ndata.get('body_text', '').
    """
    getter = getattr(G, "get_body_text", None)
    if callable(getter):
        return getter(nid)
    ndata = G.nodes[nid]
    return ndata.get("body_text", "")


def _is_condition_alive(condition: str, bindings: dict, globals_map: dict) -> bool:
    """Heuristic check if a condition could be true given variable bindings.

    Returns True if condition might be true (keep), False if definitely false (prune).
    Conservative: if unsure, returns True (keep the branch).

    Supports C preprocessor conditions:
      #ifdef MACRO     → alive if MACRO is bound (defined)
      #ifndef MACRO    → alive if MACRO is NOT bound (not defined)
      #if MACRO        → alive if MACRO is bound and truthy
      #if !MACRO       → alive if MACRO is not bound or falsy
    """
    if not condition:
        return True
    # #if 0 is always dead code
    cond = condition.strip()
    if cond == '#if 0':
        return False
    if not bindings:
        return True
    # Normalize: if(mode==1) → mode == 1
    cond = condition.strip()
    # Remove if/else/switch wrapper
    m = re.match(r'^(?:if|else|switch|match|case)\s*\(?([^)]*)\)?$', cond)
    if m:
        cond = m.group(1).strip()

    # Handle C preprocessor conditions: #ifdef, #ifndef, #if defined(), #if !defined()
    # #ifdef MACRO → alive if MACRO is not explicitly set to 0/undefined in bindings.
    # Conservative: if bindings don't mention the macro, keep the branch (it may be
    # defined at build time). Only prune if bindings explicitly set MACRO=0 or
    # MACRO=undefined.
    ifdef_match = re.match(r'^#ifdef\s+(\w+)$', cond)
    if ifdef_match:
        macro = ifdef_match.group(1)
        if macro in bindings:
            val = bindings[macro]
            # Explicitly disabled: MACRO=0, MACRO=undefined, MACRO=false
            return val.lower() not in ('0', 'undefined', 'false', '')
        # Macro not in bindings: conservatively keep (may be defined at build time)
        return True

    ifndef_match = re.match(r'^#ifndef\s+(\w+)$', cond)
    if ifndef_match:
        macro = ifndef_match.group(1)
        if macro in bindings:
            val = bindings[macro]
            # #ifndef is alive if macro is NOT defined, i.e., value is 0/undefined/false
            return val.lower() in ('0', 'undefined', 'false', '')
        # Macro not in bindings: conservatively keep (we don't know if it's defined)
        return True

    # Handle #if defined(MACRO) and #if !defined(MACRO)
    defined_match = re.match(r'^#!?\s*defined\s*\(\s*(\w+)\s*\)$', cond)
    if defined_match:
        macro = defined_match.group(1)
        is_negated = cond.startswith('#!')
        if macro in bindings:
            val = bindings[macro]
            is_defined = val.lower() not in ('0', 'undefined', 'false', '')
        else:
            # Conservative: macro not in bindings, assume it may be defined
            is_defined = True
        return (not is_defined) if is_negated else is_defined

    # Also handle "!(condition)" for else branches
    is_negated = False
    if cond.startswith('!(') and cond.endswith(')'):
        is_negated = True
        cond = cond[2:-1].strip()
    # Try to evaluate simple comparisons: var == val, var != val, var > val, var < val
    for var_name, var_value in bindings.items():
        # Resolve globals in value
        resolved_value = globals_map.get(var_value, var_value)
        # Pattern: var_name == something
        if var_name in cond:
            # Simple == check
            eq_match = re.search(rf'\b{re.escape(var_name)}\s*==\s*(\w+)', cond)
            if eq_match:
                rhs = eq_match.group(1)
                rhs_resolved = globals_map.get(rhs, rhs)
                try:
                    if str(resolved_value) == str(rhs_resolved):
                        result = True
                    else:
                        result = False
                    return not result if is_negated else result
                except (ValueError, TypeError):
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
            ne_match = re.search(rf'\b{re.escape(var_name)}\s*!=\s*(\w+)', cond)
            if ne_match:
                rhs = ne_match.group(1)
                rhs_resolved = globals_map.get(rhs, rhs)
                try:
                    if str(resolved_value) == str(rhs_resolved):
                        result = False
                    else:
                        result = True
                    return not result if is_negated else result
                except (ValueError, TypeError):
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
    return True




def _load_globals(graph_dir: str) -> dict:
    """Load globals map from .code2database_globals.json.

    Returns a flat dict of name → value for enum members and constants.
    """
    gpath = os.path.join(graph_dir, ".code2database_globals.json")
    if not os.path.exists(gpath):
        return {}
    gd = json.loads(Path(gpath).read_text(encoding="utf-8"))
    # Flatten enums and constants into a single name → value dict
    globals_map = {}
    for enum in gd.get("enums", []):
        for v in enum.get("values", []):
            globals_map[v["member"]] = v.get("value", v["member"])
    for const in gd.get("constants", []):
        globals_map[const["name"]] = const.get("value_snippet", const["name"])
    return globals_map




def _memory_dir(graph_dir: str) -> str:
    d = os.path.join(graph_dir, "memory")
    os.makedirs(d, exist_ok=True)
    return d




def _normalize_id(raw_id: str) -> str:
    """Normalize a node ID: lowercase, replace non-alnum with underscore."""
    return re.sub(r'[^a-z0-9_]', '_', raw_id.lower()) if raw_id else ""


def _derive_domain(rel_path: str) -> str:
    """Derive architecture domain from file path.

    Strategy: use path segments to build a meaningful domain.
    E.g., lib/storage/storage.c → storage, lib/storage/disk/disk.c → storage.disk
          module/storage/disk/disk.c → storage.disk
          test/unit/lib/storage/storage_ut.c → unit.storage
          test/unit/lib/storage/disk/disk_ut.c → unit.storage.disk

    External/third-party directories (vendor/, third_party/, thirdparty/,
    external/, 3rdparty/, contrib/) are detected and the domain is
    prefixed with 'external_' so they are separated from project domains.
    E.g., vendor/huawei/ → external_huawei, third_party/googletest/ → external_googletest
    """
    parts = rel_path.replace(os.sep, '/').split('/')

    # Skip common prefix directories
    skip_prefixes = {'lib', 'module', 'app', 'include', 'src', 'examples'}
    # After test→unit, also skip 'unit' dir (common pattern: test/unit/lib/...)
    skip_after_unit = {'unit', 'lib', 'module', 'src'}
    # External/third-party directory names — domain will be prefixed with 'external_'
    _EXTERNAL_DIRS = {'vendor', 'third_party', 'thirdparty', 'external', '3rdparty', 'contrib'}
    domain_parts = []
    is_external = False

    i = 0
    while i < len(parts):
        p = parts[i]
        # Skip file extension
        if '.' in p and not p.startswith('.'):
            break
        # Detect external/third-party directories
        if p in _EXTERNAL_DIRS and not domain_parts:
            is_external = True
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

    domain = '.'.join(domain_parts) if domain_parts else 'root'
    if is_external:
        domain = f"external_{domain}"
    return domain


def _detect_language_from_path(filepath: str) -> str:
    """Detect source language from file extension."""
    ext = Path(filepath).suffix.lower()
    ext_map = {
        ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
        ".hpp": "cpp", ".go": "go", ".py": "python", ".java": "java",
        ".rs": "rust",
    }
    return ext_map.get(ext, "")


def _output_result(result: dict, json_mode: bool = False):
    """Print result as JSON or formatted text."""
    if json_mode:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_structured(result)




def _parse_bindings(bindings_str: str) -> dict:
    """Parse bindings string like 'mode=1,flag=true' into dict."""
    bindings = {}
    if not bindings_str:
        return bindings
    for pair in bindings_str.split(","):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            bindings[k.strip()] = v.strip()
    return bindings




def _print_structured(obj, indent=0):
    """Recursively print structured data as indented text."""
    prefix = "  " * indent
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                print(f"{prefix}{k}:")
                _print_structured(v, indent + 1)
            else:
                print(f"{prefix}{k}: {v}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, dict):
                print(f"{prefix}[{i}]")
                _print_structured(item, indent + 1)
            else:
                print(f"{prefix}- {item}")
    else:
        print(f"{prefix}{obj}")




def _is_parser_artifact(name: str) -> bool:
    """Detect names that are parser/DSL artifacts, not real function names."""
    # Python argparse DSL patterns: locatedexpr, word(...), value(...), etc.
    artifact_prefixes = ('locatedexpr_', 'locatedexpr(', 'word_', 'value_',
                         'keyword_', 'bookmark_', 'pathstd_')
    name_lower = name.lower()
    for p in artifact_prefixes:
        if name_lower.startswith(p) or p.rstrip('_(') in name_lower:
            return True
    # Contains parenthesized DSL expressions like word(alphanums + '_')
    if '(' in name and ')' in name:
        return True
    # Consecutive underscores (3+) usually from macro/template normalization
    if '___' in name:
        return True
    # Very long names with repeated underscores (>80 chars) are suspicious
    if len(name) > 80 and name.count('_') > len(name) * 0.5:
        return True
    # CUnit test framework functions — not real application code
    if name_lower.startswith('cu_') or name_lower in ('cu_get_error', 'cu_add_suite_with_setup_and_teardown'):
        return True
    return False


# Generic skip names — common C/C++ standard library and system functions
# that are never interesting as invocation graph endpoints. Framework-specific
# skip lists (e.g., framework-specific inline helpers) are maintained in the scanner
# modules and merged at runtime when the scanner is loaded.
# External library prefixes that should not create unresolved call edges.
# Populated from profile's skip_names.external_lib_prefixes at build time.
_EXTERNAL_LIB_PREFIXES = []


def set_external_lib_prefixes(prefixes):
    """Set external library prefixes from profile configuration.

    When a callee name starts with one of these prefixes and doesn't
    resolve to any node in the graph, the edge is skipped rather than
    creating an unresolved external reference.
    """
    global _EXTERNAL_LIB_PREFIXES
    _EXTERNAL_LIB_PREFIXES = sorted(prefixes, key=len, reverse=True)


# Test domain segments used during callee resolution to prefer production
# definitions over test mocks/stubs. Populated from the profile's
# project_boundaries.test_domain_segments at build time; generic defaults
# cover the common unit/ut/test/fuzz conventions.
_DEFAULT_TEST_DOMAIN_SEGMENTS = ("ut", "ut_mock", "unit", "test", "fuzz")
_TEST_DOMAIN_SEGMENTS = _DEFAULT_TEST_DOMAIN_SEGMENTS


def set_test_domain_segments(segments):
    """Set test domain segments from profile configuration."""
    global _TEST_DOMAIN_SEGMENTS
    if segments:
        _TEST_DOMAIN_SEGMENTS = tuple(segments)


def reset_resolver_state():
    """Reset resolver module globals to their import-time defaults.

    Each call to build_graph resets these first so a build does not inherit
    external-prefix or test-segment state from a prior build_graph call in
    the same process (build_multi, scripted multi-project builds, tests).
    """
    global _EXTERNAL_LIB_PREFIXES, _TEST_DOMAIN_SEGMENTS
    _EXTERNAL_LIB_PREFIXES = []
    _TEST_DOMAIN_SEGMENTS = _DEFAULT_TEST_DOMAIN_SEGMENTS


def _candidate_in_test_domain(cid: str, id_registry: dict = None) -> bool:
    """True when a resolution candidate is defined in a test domain.

    Primary signal is the registry entry's domain attribute (dotted
    domain string, e.g. "unit.accel"), which works for both dotted and
    underscore-form node IDs. Entries without a domain fall back to the
    leading segments of dotted IDs ("unit.accel.func"). Underscore-form
    IDs without a registry domain are never classified as test — their
    leading tokens may be part of the function name itself.
    """
    attrs = id_registry.get(cid) if id_registry else None
    if attrs:
        domain = attrs.get("domain", "")
        if domain:
            return any(p in _TEST_DOMAIN_SEGMENTS
                       for p in domain.split("."))
    if "." in cid:
        head = cid.rsplit(".", 1)[0]
        return any(p in _TEST_DOMAIN_SEGMENTS for p in head.split("."))
    return False


_GENERIC_SKIP_NAMES = frozenset({
    # Standard C library functions
    'malloc', 'calloc', 'realloc', 'free', 'printf', 'fprintf', 'sprintf',
    'snprintf', 'scanf', 'fscanf', 'sscanf', 'memcpy', 'memset', 'memmove',
    'strcmp', 'strncmp', 'strcpy', 'strncpy', 'strlen', 'strdup', 'strcat',
    'strncat', 'strstr', 'strchr', 'strrchr', 'atoi', 'atol', 'atof',
    'exit', 'abort', '_exit', 'perror', 'fopen', 'fclose', 'fread', 'fwrite',
    'fgets', 'fputs', 'feof', 'ferror', 'fflush', 'ftell', 'fseek',
    'errno', 'setjmp', 'longjmp',
    # POSIX
    'open', 'close', 'read', 'write', 'ioctl', 'mmap', 'munmap', 'poll',
    'select', 'socket', 'bind', 'listen', 'accept', 'connect', 'send', 'recv',
    'gettimeofday', 'clock_gettime', 'getpid', 'pthread_create',
    'pthread_join', 'pthread_mutex_lock', 'pthread_mutex_unlock',
    # Additional C/POSIX standard library (commonly seen in invocation graphs)
    'strnlen', 'strtok', 'strtoll', 'strtoull', 'basename', 'access',
    'unlink', 'fcntl', 'fstat', 'stat', 'lstat', 'chmod', 'chown',
    'time', 'clock', 'difftime', 'mktime', 'localtime', 'gmtime', 'strftime',
    'getenv', 'setenv', 'unsetenv', 'system', 'execve', 'fork', 'waitpid',
    'kill', 'signal', 'raise', 'alarm', 'pause', 'sleep', 'usleep',
    'getuid', 'getgid', 'geteuid', 'getegid', 'setuid', 'setgid',
    'dlopen', 'dlclose', 'dlsym', 'dlerror',
    'shm_open', 'shm_unlink', 'shmctl', 'shmat', 'shmdt',
    'sem_wait', 'sem_post', 'sem_init', 'sem_destroy', 'sem_timedwait',
    'sem_open', 'sem_close', 'sem_unlink',
    'mlock', 'munlock', 'mprotect', 'msync', 'madvise',
    'prctl', 'sysconf', 'isatty', 'ttyname',
    'sendmsg', 'recvmsg', 'sendto', 'recvfrom', 'shutdown',
    'getsockname', 'getpeername', 'getsockopt', 'setsockopt',
    'eventfd', 'timerfd_create', 'timerfd_settime', 'signalfd',
    'epoll_create', 'epoll_ctl', 'epoll_wait',
    'kqueue', 'kevent',
    'io_setup', 'io_destroy', 'io_getevents', 'io_submit',
    'getopt', 'getopt_long', 'getopt_long_only',
    'memset_s', 'explicit_bzero', 'timingsafe_bcmp',
    # C99/C11 standard macros and builtins
    'va_copy', 'va_start', 'va_end', 'va_arg',
    '__builtin_expect', '__builtin_va_start', '__builtin_va_end',
    '__builtin_va_copy', '__builtin_va_arg',
})


def _build_suffix_index(id_registry: dict) -> dict:
    """Build a suffix lookup index for fast callee resolution.

    Returns: {normalized_name_suffix: [full_id, ...]}
    Key: the last part of an ID after splitting on underscore patterns.
    This replaces the O(N) scan in _resolve_invoked_id with O(1) lookup.
    """
    suffix_map = defaultdict(list)
    for full_id, attrs in id_registry.items():
        # Normalize the same way as _resolve_invoked_id does
        norm = re.sub(r'[^a-z0-9_]', '_', full_id.lower())
        # Index under the full normalized ID
        suffix_map[norm].append(full_id)
        # Also index under the function name portion (after last dot in original ID)
        # This handles cases like "dev.raid.raid_dev_submit_request" where
        # the callee is just "raid_dev_submit_request".
        # DO NOT index by arbitrary underscore suffixes — that creates false
        # matches (e.g., "gpa_to_vva" matching "vhost_gpa_to_vva" when they
        # are different functions).
        if "." in full_id:
            func_name_part = full_id.rsplit(".", 1)[-1].lower()
            func_name_norm = re.sub(r'[^a-z0-9_]', '_', func_name_part)
            if func_name_norm != norm:
                suffix_map[func_name_norm].append(full_id)
        # Also index by the "name" attribute (function name) when the ID uses
        # underscore-separated domain prefixes instead of dots.
        # E.g., ID="fs_ext4_ext4_file_write_iter", name="ext4_file_write_iter"
        # Without this, _resolve_invoked_id("ext4_file_write_iter") can't find
        # the node because the suffix_index only has the full normalized ID
        # and the dot-based name extraction (which finds nothing for _ IDs).
        func_name = attrs.get("name", "")
        if func_name:
            func_name_norm = re.sub(r'[^a-z0-9_]', '_', func_name.lower())
            if func_name_norm != norm and func_name_norm not in suffix_map or full_id not in suffix_map.get(func_name_norm, []):
                suffix_map[func_name_norm].append(full_id)
    return suffix_map


def _resolve_invoked_id(callee_name: str, domain: str, id_registry: dict,
                       suffix_index: dict = None) -> str:
    """Try to resolve a callee name to a full node ID.

    Strategy:
    1. Exact match on normalized name in suffix_index (O(1) if index provided)
    2. Fallback: linear scan of id_registry (O(N), only if no index)
    3. Match with domain prefix
    4. Fallback: return normalized name as-is (may be unresolved)
       Returns "" for parser artifact names (skipped edges).
    """
    # Fast path: if callee_name is already a qualified node ID (contains dot),
    # check if it exists directly in the registry. This avoids normalizing
    # "domain.func_name" → "domain_func_name" which can collide with a
    # different function whose name includes the domain prefix.
    # Example: nvmf.poll_group_update_subsystem normalized to nvmf_poll_group_update_subsystem
    # which matched the caller nvmf.nvmf_poll_group_update_subsystem instead.
    if "." in callee_name and callee_name in id_registry:
        return callee_name

    # Skip obvious parser/DSL artifacts
    if _is_parser_artifact(callee_name):
        return ""

    # Skip names in generic skip list
    if callee_name in _GENERIC_SKIP_NAMES:
        return ""

    # Skip names in framework-specific skip list (if scanner module loaded)
    try:
        from _vendor._regex_c_scanner import _SKIP_NAMES
        if callee_name in _SKIP_NAMES:
            return ""
    except ImportError:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    # These are external dependencies (configured via profile) that are not
    # part of the scanned project. Return a special "ext:" prefixed marker so
    # the builder can create endpoint nodes instead of unresolved call edges.
    # Exception: if the name also starts with the project's own prefix (e.g.,
    # proj_extlib_init has extlib_ prefix but is actually a project-internal function),
    # don't treat it as external.
    if _EXTERNAL_LIB_PREFIXES:
        callee_lower = callee_name.lower()
        for prefix in _EXTERNAL_LIB_PREFIXES:
            if callee_lower.startswith(prefix):
                # Check if this might be a project-internal function that
                # happens to have an external prefix embedded (e.g., proj_extlib_*)
                # by checking if the suffix_index has a NON-TEST match.
                # Test-domain matches (unit.*, ut.*, fuzz.*) are mock/stub
                # functions that shadow the real external library function —
                # they should NOT prevent external endpoint creation.
                if suffix_index is not None:
                    norm_check = re.sub(r'[^a-z0-9_]', '_', callee_lower)
                    candidates_check = suffix_index.get(norm_check, [])
                    prod_matches = [c for c in candidates_check
                                    if not _candidate_in_test_domain(c, id_registry)]
                    if prod_matches:
                        break  # Found in production code — treat as internal
                return f"ext:{prefix.rstrip('_')}:{callee_lower}"

    norm = re.sub(r'[^a-z0-9_]', '_', callee_name.lower())

    # Fast path: use suffix index if available
    if suffix_index is not None:
        candidates = suffix_index.get(norm, [])
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            # Multiple matches: prefer same domain
            # Match the full domain path (not just first component).
            # For domain "unit.accel", candidates like "unit.accel.func"
            # should match, but "accel.func" or "unit.bdev.func" should not.
            # Node IDs use dots as separators (e.g., "unit.accel.func"),
            # so compare using dot-formatted domain prefix directly.
            same_domain = [c for c in candidates
                           if c.startswith(domain + ".")]
            # Also try parent domain match: "unit.accel" caller should
            # match "accel.func" (production) when no unit.accel copy exists
            if not same_domain:
                domain_parts = domain.split(".")
                # Try progressively shorter domain prefixes
                for i in range(len(domain_parts) - 1, 0, -1):
                    parent_prefix = ".".join(domain_parts[i:])
                    parent_matches = [c for c in candidates
                                      if c.startswith(parent_prefix + ".")
                                      and not _candidate_in_test_domain(c, id_registry)]
                    if parent_matches:
                        same_domain = parent_matches
                        break
            if same_domain:
                # Among same-domain matches, prefer EXACT name match over suffix match.
                # e.g., "dev_submit_request" should match "dev.dev_submit_request"
                # not "dev.raid.raid_dev_submit_request"
                exact_matches = [c for c in same_domain
                                 if c.split(".", 1)[-1] == norm]
                if exact_matches:
                    return exact_matches[0]
                return same_domain[0]

            # Prefer production code over test/mock code.
            # When a callee name matches both a production function and a
            # test mock/stub (e.g., extlib_pool_free matches both
            # env_ext.extlib_pool_free and unit.accel.extlib_pool_free),
            # the production version is the real call target.
            prod_candidates = [c for c in candidates
                               if not _candidate_in_test_domain(c, id_registry)]
            if prod_candidates:
                candidates = prod_candidates

            # No same-domain match: prefer exact name match across all candidates
            exact_matches = [c for c in candidates
                             if c.split(".", 1)[-1] == norm]
            if exact_matches:
                return exact_matches[0]
            # Return first match
            return candidates[0]
        # Try as suffix (callee name might be just the function part)
        candidates = suffix_index.get("_" + norm, [])
        if not candidates:
            # Try without leading underscore
            for depth_key in [norm]:
                candidates = suffix_index.get(depth_key, [])
                if candidates:
                    break
    else:
        # Slow path: linear scan (backward compatible)
        for full_id in id_registry:
            if full_id.endswith("_" + norm):
                return full_id

    # Try with domain prefix
    domain_prefix = domain.replace(".", "_") + "_"
    candidate = domain_prefix + norm
    if candidate in id_registry:
        return candidate

    # Return as-is (unresolved)
    return norm




_CJK_RE = re.compile(
    '['
    '\u4e00-\u9fff'           # CJK Unified Ideographs (main block)
    '\u3400-\u4dbf'           # CJK Extension A
    '\u3040-\u309f'           # Hiragana
    '\u30a0-\u30ff'           # Katakana
    '\uac00-\ud7af'           # Hangul Syllables
    '\u1100-\u11ff'           # Hangul Jamo
    '\uff00-\uffef'           # Fullwidth / Halfwidth Forms
    '\uf900-\ufaff'           # CJK Compatibility Ideographs
    '\U00020000-\U0002a6df'   # CJK Extension B
    '\U0002a700-\U0002ebef'   # CJK Extensions C-F
    ']'
)


def _has_cjk(text) -> bool:
    """True if text contains CJK unified ideograph characters.

    Unicode61-tokenized FTS5 indexes treat each contiguous CJK run as
    one token, so CJK query semantics are invisible to the Latin-only
    FTS escape paths — callers use this to route to a similarity pass.
    """
    return bool(_CJK_RE.search(text or ""))


def _similarity_score(tokens_a: set, tokens_b: set) -> float:
    """Jaccard-like similarity between two token sets."""
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union) if union else 0.0




def _simple_tokenize(text: str) -> set:
    """Simple tokenization for similarity matching. Handles CJK and Latin."""
    # Keep CJK chars, alphanumerics; split on punctuation/whitespace
    # For CJK: each character is a token; for Latin: words
    tokens = set()
    # Latin words
    latin = re.findall(r'[a-zA-Z0-9_]+', text.lower())
    tokens.update(latin)
    # CJK characters (each as individual token for broad matching)
    cjk = _CJK_RE.findall(text)
    tokens.update(cjk)
    # Also add CJK bigrams for better matching
    for i in range(len(cjk) - 1):
        tokens.add(cjk[i] + cjk[i+1])
    return tokens


# ---------------------------------------------------------------------------
# CJK pre-tokenization for FTS5 indexing
#
# SQLite FTS5 ``unicode61`` folds each contiguous CJK run into a single
# token, so ``释放内存`` stored as ``body`` is one token and a MATCH on
# ``释放`` returns nothing.  Pre-tokenizing on the write side (space-join
# the words) and on the query side (quote each word) makes FTS5 MATCH
# work naturally for CJK text.
#
# The default tokenizer is jieba (pure-Python, pip-install).  When jieba
# is unavailable the code falls back to the existing char+bigram scheme
# so every environment keeps working.
# ---------------------------------------------------------------------------

_TOKENIZER_CACHE: dict = {}


def _load_stopwords() -> Set[str]:
    """Load the bundled CJK+Latin stopword set (one word per line)."""
    cache_key = "__stopwords__"
    if cache_key in _TOKENIZER_CACHE:
        return _TOKENIZER_CACHE[cache_key]
    sw: Set[str] = set()
    sw_path = Path(__file__).resolve().parent.parent / "config" / "stopwords.txt"
    if sw_path.is_file():
        for line in sw_path.read_text(encoding="utf-8").splitlines():
            w = line.strip()
            if w and not w.startswith("#"):
                sw.add(w)
    _TOKENIZER_CACHE[cache_key] = sw
    return sw


def _char_bigram_tokenize(text: str) -> List[str]:
    """Character + bigram tokenization (the always-available fallback)."""
    cjk = _CJK_RE.findall(text)
    tokens: List[str] = list(cjk)
    for i in range(len(cjk) - 1):
        tokens.append(cjk[i] + cjk[i + 1])
    return tokens


def _jieba_tokenize(text: str, user_dict: Optional[str] = None) -> List[str]:
    """Jieba tokenization with optional user dictionary."""
    cache_key = ("jieba", user_dict)
    if cache_key not in _TOKENIZER_CACHE:
        import jieba  # lazy import — optional dependency
        if user_dict and os.path.isfile(user_dict):
            jieba.load_userdict(user_dict)
        _TOKENIZER_CACHE[cache_key] = jieba
    else:
        jieba = _TOKENIZER_CACHE[cache_key]
    return [t for t in jieba.lcut(text) if t.strip()]


def _cjk_tokenize(text: str, tokenizer: str = "jieba",
                  user_dict: Optional[str] = None) -> List[str]:
    """Tokenize CJK text with the configured tokenizer.

    Falls back to char+bigram when the requested tokenizer is unavailable.
    Non-CJK text is returned as-is (the caller splits on whitespace).
    """
    if tokenizer == "char":
        return _char_bigram_tokenize(text)
    if tokenizer == "jieba":
        try:
            return _jieba_tokenize(text, user_dict)
        except ImportError:
            pass
    return _char_bigram_tokenize(text)


def _cjk_pre_tokenize(text: str, tokenizer: str = "jieba",
                      user_dict: Optional[str] = None,
                      use_stopwords: bool = True) -> str:
    """Pre-tokenize text for FTS5 indexing.

    If *text* contains CJK ideographs, the CJK portions are word-segmented
    and space-joined so ``unicode61`` splits them into individual tokens.
    Latin text is left untouched (it is already whitespace-separated).
    Stopwords are removed when enabled.

    Returns the pre-tokenized string ready to store in a ``*_tokenized``
    column that an FTS5 external-content table indexes.
    """
    if not text or not _has_cjk(text):
        return text or ""
    stopwords = _load_stopwords() if use_stopwords else set()
    tokens = _cjk_tokenize(text, tokenizer, user_dict)
    if stopwords:
        tokens = [t for t in tokens if t not in stopwords]
    return " ".join(tokens)
    """Build a call-only subgraph from G, excluding CONTAINS/IMPORTS edges.

    Optionally skip file nodes (node_type=='file' or 'file' in labels)
    for pathfinding that should only traverse function nodes.

    Args:
        G: Full graph with CONTAINS/IMPORTS edges.
        skip_file_nodes: If True, exclude file nodes from the subgraph.

    Returns:
        A new DiGraph containing only call edges (and their nodes).

    Raises:
        RuntimeError: G is a LazySQLiteGraph (>=50K nodes). Copying every
            node and edge into a fresh in-memory DiGraph is exactly the
            eager load the lazy view exists to avoid — on multi-million-
            node graphs the process gets OOM-killed (or, inside the MCP
            server, hits the per-tool timeout mid-copy). Raising keeps
            the failure explainable in every consumer context (CLI
            traceback, MCP error dict); callers that support lazy views
            pre-gate this call and traverse G directly.
    """
    if type(G).__name__ == "LazySQLiteGraph":
        raise RuntimeError(
            "call-graph copy is not supported on SQLite-backed large "
            f"graphs ({G.number_of_nodes()} nodes via LazySQLiteGraph): "
            "it duplicates every node and edge in memory. Re-run against "
            "an in-memory graph, or use a command that traverses the "
            "lazy view directly.")
    call_G = nx.DiGraph()
    for nid, ndata in G.nodes(data=True):
        if skip_file_nodes and (ndata.get("node_type") == "file"
                                or "file" in ndata.get("labels", [])):
            continue
        call_G.add_node(nid, **ndata)
    for u, v, edata in G.edges(data=True):
        if edata.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        if skip_file_nodes:
            u_nd = G.nodes[u]
            v_nd = G.nodes[v]
            if (u_nd.get("node_type") == "file" or "file" in u_nd.get("labels", [])
                    or v_nd.get("node_type") == "file" or "file" in v_nd.get("labels", [])):
                continue
        call_G.add_edge(u, v, **edata)
    return call_G


# Re-export line lookup helpers from the standalone line_utils module
# (which doesn't depend on networkx), so legacy callers that imported
# them from _builder.utils continue to work.
from _builder.line_utils import (  # noqa: E402
    build_line_starts,
    line_for_offset,
    lines_for_matches,
)




# ---------------------------------------------------------------------------
# Store-dir discovery (graph or knowledge/memory only)
# ---------------------------------------------------------------------------

_STORE_DIR_NAME = "code2db-out"
_GRAPH_STORE_MARKERS = ("code2database.db", "code2database_master.json")
_KB_STORE_MARKERS = (
    "kb_index.db",
    os.path.join("memory", "memory.db"),
    os.path.join("knowledge", "brief.json"),
)


def _dir_with_marker(base: str, markers) -> Optional[str]:
    for marker in markers:
        if os.path.isfile(os.path.join(base, marker)):
            return base
    return None


def resolve_store_dir() -> str:
    """Locate the store directory when --graph is omitted.

    Walks up from the working directory. A directory counts as a store
    when it holds graph artifacts (code2database.db / master.json —
    either storage backend) or knowledge-base artifacts (kb_index.db,
    memory/memory.db, knowledge/brief.json), so a knowledge-only store
    is discovered exactly like a built graph. Graph markers win over
    kb markers within the same directory. Falls back to the
    conventional code2db-out name so the command reports its normal
    not-found error.
    """
    d = os.getcwd()
    while True:
        cand = os.path.join(d, _STORE_DIR_NAME)
        found = (_dir_with_marker(cand, _GRAPH_STORE_MARKERS)
                 or _dir_with_marker(cand, _KB_STORE_MARKERS))
        if found:
            return found
        found = (_dir_with_marker(d, _GRAPH_STORE_MARKERS)
                 or _dir_with_marker(d, _KB_STORE_MARKERS))
        if found:
            return found
        parent = os.path.dirname(d)
        if parent == d:
            return _STORE_DIR_NAME
        d = parent
