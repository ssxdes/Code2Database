"""callgraph builder module: explore-flow — one-shot context retrieval.

Given a natural language query or symbol names, find the most relevant
nodes, paths, and conditions in a single capped call. Eliminates the
multi-step context_pack → describe-node → resolve-chain workflow.
"""

import json
import os
import re
from collections import defaultdict, deque
import networkx as nx
from _builder.graph_build import _load_full_graph
from _builder.query_cache import cached_query
from _builder.utils import _find_node_id, _normalize_id
from _builder.token_budget import estimate_tokens, truncate_to_tokens
import logging


def _bfs_invoke_path(G, start: str, end: str, max_depth: int = 15) -> list:
    """BFS shortest path from start to end, skipping CONTAINS/IMPORTS edges.

    Used in place of nx.shortest_path(call_G, ...) on LazySQLiteGraph where
    building a full in-memory call subgraph would OOM (1.5M+ nodes).
    """
    if start == end:
        return [start]
    visited = {start}
    parent = {start: None}
    queue = deque([(start, 0)])
    while queue:
        nid, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for succ in G.successors(nid):
            if succ in visited:
                continue
            ed = G.get_edge_data(nid, succ) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            visited.add(succ)
            parent[succ] = nid
            if succ == end:
                # Reconstruct path
                path = [succ]
                while parent[path[-1]] is not None:
                    path.append(parent[path[-1]])
                return list(reversed(path))
            queue.append((succ, depth + 1))
    return []


def _tokenize_query(query: str) -> list:
    """Split a query into searchable tokens.

    Handles: whitespace, camelCase splitting, snake_case splitting, CJK characters.
    E.g., "myApiInit" → ["my", "api", "init"], "module_open" → ["module", "open"]

    BUG-DESCRIPTION MODE (IMPROVE-2): when the query contains a long snake_case
    identifier (>= 3 underscore-separated parts, like "__find_get_block_slow"),
    preserve it as a single compound token IN ADDITION to its parts. This lets
    the scorer do exact-name matching for bug descriptions that quote a function
    name verbatim. Without this, "__find_get_block_slow null pointer dereference
    buffer" loses the strong signal of the exact function name.
    """
    # First split camelCase: insert boundary before uppercase after lowercase
    split_camel = re.sub(r'([a-z])([A-Z])', r'\1 \2', query)
    # Split snake_case: underscore as separator
    split_snake = re.sub(r'_', ' ', split_camel)
    # Extract tokens: alphanumeric sequences or CJK character sequences
    tokens = re.findall(r'[a-zA-Z0-9]+|[一-鿿㐀-䶿]+', split_snake.lower())
    # Deduplicate while preserving order
    seen = set()
    result = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


# ---------------------------------------------------------------------------
# Bug-keyword detection (IMPROVE-2)
# ---------------------------------------------------------------------------
# When a user types "null pointer dereference buffer" or "use-after-free in
# jbd2_journal_cancel_revoke", we want to (a) recognize this is a bug-analysis
# query, (b) down-rank generic tokens (null, pointer, dereference, get, set)
# that match thousands of unrelated functions, and (c) boost domain affinity
# (buffer/block/inode → fs/bdev/block subsystems).

_BUG_KEYWORDS = {
    # NULL/UAF class
    "null", "nullptr", "nil", "ptr", "pointer", "deref", "dereference",
    "use", "after", "free", "uaf", "dangling", "wild",
    # Race class
    "race", "racy", "concurrency", "concurrent", "atomicity", "toctou",
    "lockless", "unsynchronized", "data", "thread",
    # Leak class
    "leak", "leaked", "memory", "resource", "unreleased", "orphan",
    # Crash class
    "crash", "panic", "oops", "bug", "kasan", "kmsan", "ubsan",
    "stack", "overflow", "underflow", "corrupt", "corruption",
    # Generic verbs that match too many functions — down-rank
    "get", "set", "put", "find", "do", "handle", "process", "make",
}

# Tokens so common they match 100+ functions in any large codebase.
# Scorer penalizes these to keep them from dominating results.
_GENERIC_TOKENS = {
    "get", "set", "put", "find", "do", "make", "init", "exit", "free",
    "null", "ptr", "pointer", "data", "val", "value", "ret", "return",
    "err", "error", "fail", "check", "test", "is", "has", "the", "a",
    "an", "in", "of", "for", "to", "with", "and", "or", "not",
}

# Domain affinity map — when a query mentions these tokens, prefer nodes
# in the listed domains (file-path prefixes). Helps disambiguate "buffer"
# (which could be any subsystem) toward fs/bdev/block.
_DOMAIN_AFFINITY = {
    # buffer / block / bh → filesystem / block layer
    "buffer": ["fs", "block", "bdev", "jbd2", "ext4", "xfs", "mpage"],
    "block": ["block", "bdev", "fs", "buffer"],
    "bh": ["fs", "buffer", "jbd2", "block"],
    "bdev": ["block", "bdev", "fs"],
    "inode": ["fs", "ext4", "xfs", "inode", "vfs"],
    "page": ["mm", "page", "fs", "filemap"],
    "folio": ["mm", "page", "fs"],
    "journal": ["jbd2", "fs", "ext4", "journal"],
    "revoke": ["jbd2", "journal", "revoke"],
    "commit": ["jbd2", "journal", "transaction"],
    "transaction": ["jbd2", "journal", "transaction"],
    # Concurrency tokens prefer kernel/sync/mm subsystems
    "lock": ["kernel", "sync", "locking", "spinlock", "mutex"],
    "spinlock": ["kernel", "locking", "spinlock"],
    "mutex": ["kernel", "locking", "mutex"],
    "rcu": ["kernel", "rcu"],
    "atomic": ["kernel", "atom"],
    # Memory tokens prefer mm/
    "memory": ["mm", "mem", "kasan"],
    "kasan": ["mm", "kasan"],
    "page_alloc": ["mm", "page_alloc"],
    # Process tokens prefer kernel/
    "task": ["kernel", "sched", "task"],
    "process": ["kernel", "sched", "process"],
    "thread": ["kernel", "sched", "thread"],
    "cpu": ["kernel", "sched", "cpu"],
    # Network tokens
    "socket": ["net", "socket"],
    "skb": ["net", "skb"],
    "packet": ["net", "packet"],
}


def _detect_bug_query(tokens: list) -> bool:
    """Return True if the query looks like a bug-analysis description.

    Trigger: query contains any bug-keyword (null, race, leak, crash, etc.)
    OR contains a function-name-shaped token (>= 3 parts separated by _).
    """
    for t in tokens:
        if t in _BUG_KEYWORDS:
            return True
    # Long snake_case function names are a strong bug-report signal
    # e.g., "__find_get_block_slow" tokenizes to find, get, block, slow
    # — 4 parts, all >= 3 chars. Treat as bug query.
    if len([t for t in tokens if len(t) >= 3]) >= 3:
        return True
    return False


# ---------------------------------------------------------------------------
# Synonym expansion — lightweight semantic matching
# ---------------------------------------------------------------------------
# Maps common verbs (English + Chinese) to C function-name conventions.
# Lets "哪个函数释放了 pid 的内存" match free_pid / put_pid / release_pid /
# destroy_pid etc. without an embedding model.

_SYNONYM_MAP = {
    # Release / free
    "release": ["release", "free", "put", "destroy", "done", "exit", "cleanup",
                "unref", "dealloc", "dispose", "fini", "shutdown"],
    "free": ["free", "release", "put", "destroy", "dealloc", "unref"],
    "dealloc": ["dealloc", "free", "release", "put"],
    "destroy": ["destroy", "free", "release", "exit", "fini", "shutdown"],
    "释放": ["release", "free", "put", "destroy", "done", "exit", "cleanup",
            "unref", "dealloc", "dispose", "fini"],
    "销毁": ["destroy", "free", "release", "exit", "fini", "shutdown"],
    "卸载": ["unload", "exit", "fini", "shutdown", "release", "destroy"],
    # Allocate / create
    "alloc": ["alloc", "new", "create", "make", "get", "init", "setup"],
    "allocate": ["alloc", "new", "create", "make", "get"],
    "create": ["create", "new", "make", "alloc", "init", "setup"],
    "new": ["new", "create", "alloc", "make"],
    "分配": ["alloc", "new", "create", "make", "get", "init"],
    "创建": ["create", "new", "make", "alloc", "init", "setup"],
    "新建": ["new", "create", "make", "alloc"],
    # Lock / unlock
    "lock": ["lock", "acquire", "hold", "grab", "take"],
    "acquire": ["acquire", "lock", "get", "take", "grab"],
    "锁": ["lock", "acquire", "hold", "grab", "take"],
    "加锁": ["lock", "acquire", "hold", "grab"],
    "unlock": ["unlock", "release", "put"],
    "解锁": ["unlock", "release", "put"],
    # Read / write
    "read": ["read", "get", "fetch", "load", "load_n"],
    "get": ["get", "read", "fetch", "load"],
    "fetch": ["fetch", "get", "read", "load"],
    "读": ["read", "get", "fetch", "load"],
    "获取": ["get", "fetch", "acquire", "read", "load"],
    "write": ["write", "set", "store", "store_n", "update"],
    "set": ["set", "write", "store", "update"],
    "写": ["write", "set", "store", "update"],
    "设置": ["set", "write", "store", "update", "configure"],
    # Init / start / stop
    "init": ["init", "setup", "prepare", "start", "begin", "boot"],
    "initialize": ["init", "setup", "prepare", "start", "begin"],
    "初始化": ["init", "setup", "prepare", "start", "begin", "boot"],
    "start": ["start", "begin", "boot", "init", "launch"],
    "启动": ["start", "begin", "boot", "init", "launch"],
    "stop": ["stop", "end", "halt", "shutdown", "exit", "fini"],
    "停止": ["stop", "end", "halt", "shutdown", "exit", "fini"],
    # Check / validate
    "check": ["check", "verify", "validate", "test", "is_", "has_"],
    "validate": ["validate", "verify", "check", "is_"],
    "检查": ["check", "verify", "validate", "test", "is_", "has_"],
    "验证": ["verify", "validate", "check", "test"],
    # Find / search
    "find": ["find", "search", "lookup", "get", "resolve", "match"],
    "search": ["search", "find", "lookup", "resolve", "match"],
    "查找": ["find", "search", "lookup", "resolve", "match"],
    "搜索": ["search", "find", "lookup", "resolve", "match"],
    # Print / log
    "print": ["print", "log", "dump", "trace", "show", "emit"],
    "log": ["log", "print", "trace", "emit"],
    "打印": ["print", "log", "dump", "trace", "show"],
    # Send / receive
    "send": ["send", "transmit", "emit", "post", "dispatch", "notify"],
    "发送": ["send", "transmit", "emit", "post", "dispatch", "notify"],
    "receive": ["receive", "recv", "read", "accept", "handle"],
    "接收": ["receive", "recv", "read", "accept", "handle"],
    # Process / handle
    "process": ["process", "handle", "do_", "run", "exec"],
    "handle": ["handle", "process", "do_", "deal", "treat"],
    "处理": ["process", "handle", "do_", "run", "exec"],
}


def _expand_synonyms(tokens: list) -> list:
    """Expand query tokens with synonym variants.

    Returns a list of (token, is_synonym) tuples — original tokens come first
    (with is_synonym=False, weighted higher by the scorer), then synonym
    expansions (is_synonym=True, weighted lower).
    """
    expanded = []
    seen = {t for t in tokens}
    for t in tokens:
        expanded.append((t, False))
        syns = _SYNONYM_MAP.get(t)
        if syns:
            for s in syns:
                if s not in seen and s != t:
                    expanded.append((s, True))
                    seen.add(s)
    return expanded


def _score_node_relevance(nd: dict, query_tokens: list, focus_domain: str = None,
                          bug_query: bool = False) -> float:
    """Score how relevant a node is to the query tokens.

    Factors: name match, signature match, semantic_desc match, domain match,
    focus_domain bonus/penalty, generic-token penalty, bug-query domain affinity.

    `query_tokens` may be either a list of strings (treated as originals) or
    a list of (token, is_synonym) tuples from _expand_synonyms. Synonyms get
    a lower weight to preserve original-token priority.

    IMPROVE-2: When `bug_query` is True (query contains null/deref/race/leak
    keywords OR a long snake_case function name), apply:
      - Generic-token penalty: tokens like "get", "set", "null", "pointer"
        match too many functions — multiply their contribution by 0.4
      - Domain affinity: if a token (e.g., "buffer") maps to the node's
        domain in _DOMAIN_AFFINITY, add a +2 bonus
      - Compound-name bonus: if the full query's joined form (e.g.,
        "find_get_block_slow") equals the node name, add a strong +8 bonus
        (this preserves the exact function name signal that was being lost)
    """
    score = 0.0
    name = nd.get("name", "").lower()
    sig = nd.get("signature", "").lower()
    desc = (nd.get("semantic_desc", "") or nd.get("external_desc", "")).lower()
    domain = nd.get("domain", "").lower()
    source_file = nd.get("source_file", "").lower()

    # Normalize tokens to (token, is_synonym) tuples
    normalized = []
    for t in query_tokens:
        if isinstance(t, tuple):
            normalized.append(t)
        else:
            normalized.append((t, False))

    # IMPROVE-2: compound-name exact match — preserves the signal of a
    # bug-report query that quotes the function name verbatim.
    # E.g., query tokens [find, get, block, slow] join to "find_get_block_slow"
    # which exactly matches the function name.
    if bug_query:
        original_tokens = [t for t, is_syn in normalized if not is_syn]
        if original_tokens:
            joined = "_".join(original_tokens)
            if name == joined:
                score += 8.0
            elif name.endswith("_" + joined) or name.startswith(joined + "_"):
                score += 5.0
            elif joined in name:
                score += 4.0

    for token, is_synonym in normalized:
        # Synonyms get reduced weight (0.5x) to keep original-token priority
        weight = 0.5 if is_synonym else 1.0
        # IMPROVE-2: generic-token penalty in bug queries
        if bug_query and token in _GENERIC_TOKENS and not is_synonym:
            weight *= 0.4
        # Match against name components (snake_case / camelCase split) to
        # avoid false matches like "put" matching "compute". A token matches
        # the name if it equals the whole name, equals a name part, or is a
        # prefix of a name part (>= 3 chars to avoid noise).
        name_parts = name.replace('-', '_').split('_')
        if name == token:
            score += 5.0 * weight  # exact name match
            # Also count as a whole-part match for backward compat (tests
            # expect both exact + partial bonus when name == token).
            score += 3.0 * weight
        elif token in name_parts:
            score += 3.0 * weight  # whole-part match
        elif len(token) >= 3 and any(p.startswith(token) for p in name_parts):
            score += 3.0 * weight  # prefix-of-part match (e.g., "alloc" → "allocate")
        elif not is_synonym and token in name:
            # Original-token substring match (kept for backward compat with
            # short tokens that don't align to name parts). Synonyms are NOT
            # allowed to match via substring — they must align to a part.
            score += 3.0 * weight
        if token in sig and not is_synonym:
            score += 1.0 * weight
        if token in desc and not is_synonym:
            score += 2.0 * weight
        if token == domain and not is_synonym:
            score += 0.5 * weight

        # IMPROVE-2: domain affinity bonus
        if bug_query and token in _DOMAIN_AFFINITY:
            preferred_domains = _DOMAIN_AFFINITY[token]
            # Check against domain string AND source_file path prefix
            for pd in preferred_domains:
                if pd in domain or domain.startswith(pd) or ("/" + pd) in source_file:
                    score += 2.0
                    break

    # Boost API entries and thread entries
    labels = nd.get("labels", [])
    if "API_entry" in labels:
        score += 1.0
    if "thread_processor" in labels:
        score += 0.5

    # Domain match bonus/penalty for --focus-domain
    if focus_domain:
        if nd.get("domain", "").startswith(focus_domain):
            score += 2.0  # Strong bonus for domain match
        else:
            score *= 0.3  # Penalty for non-matching domain

    return score


def _find_relevant_nodes(G: nx.DiGraph, query_tokens: list, top_n: int = 20,
                         graph_dir: str = None, focus_domain: str = None) -> list:
    """Find top-N nodes most relevant to the query.

    Uses suffix index for fast lookup when available, falls back to linear scan.
    Synonym expansion is applied to the scoring tokens (original tokens get
    higher weight than synonym variants).

    IMPROVE-2: Detects bug-analysis queries (null/deref/race/leak keywords OR
    long snake_case function names) and passes bug_query=True to the scorer.
    Bug queries apply generic-token penalty, domain affinity, and compound-name
    exact-match bonus to avoid irrelevant results like tools/perf, drivers/hid
    for queries about __find_get_block_slow.
    """
    # Expand query tokens with synonyms for scoring (original tokens
    # retain priority via the is_synonym weight in _score_node_relevance).
    expanded_tokens = _expand_synonyms(query_tokens)

    # IMPROVE-2: detect bug-analysis query shape
    bug_query = _detect_bug_query(query_tokens)

    # Try suffix-index accelerated lookup first
    candidate_ids = set()

    # Check if suffix index is available from graph directory
    # (stored in .code2database_suffix_index.json)
    suffix_path = None
    if graph_dir:
        suffix_path = os.path.join(graph_dir, ".code2database_suffix_index.json")
        if os.path.exists(suffix_path):
            try:
                with open(suffix_path, "r", encoding="utf-8") as f:
                    disk_index = json.load(f)
                for token in query_tokens:
                    t = token.lower()
                    norm_t = _normalize_id(t)
                    if norm_t in disk_index:
                        candidate_ids.update(disk_index[norm_t])
            except (json.JSONDecodeError, OSError):
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
    # Uses idx_functions_name index — avoids iterating 1.5M nodes.
    cls_name = type(G).__name__
    if not candidate_ids and cls_name == "LazySQLiteGraph":
        conn = getattr(G, "_conn", None)
        if conn is not None:
            # Strategy: prefer candidates matching MULTIPLE tokens.
            # 1) Exact name match (highest priority)
            # 2) Name contains ALL tokens (concatenated with %) — best for
            #    multi-word queries like "jbd2_journal_cancel_revoke"
            # 3) Per-token substring matches (fallback for partial queries)

            # 1. Exact name match (rare but highest signal)
            for token in query_tokens:
                t = token.lower()
                if not t:
                    continue
                for row in conn.execute(
                    "SELECT id FROM functions WHERE name = ? LIMIT 50",
                    (t,)):
                    candidate_ids.add(row[0])

            # 2. Name contains ALL tokens (in any order, as substrings)
            # Build a LIKE clause: %token1%token2%...%tokenN%
            # This finds names containing all tokens — perfect for exact
            # function name queries split into tokens.
            if query_tokens:
                like_pattern = "%" + "%".join(t.lower() for t in query_tokens) + "%"
                for row in conn.execute(
                    "SELECT id FROM functions WHERE name LIKE ? LIMIT 100",
                    (like_pattern,)):
                    candidate_ids.add(row[0])
                # Also try reversed order (token2...token1) since LIKE is
                # order-sensitive
                like_pattern_rev = "%" + "%".join(reversed([t.lower() for t in query_tokens])) + "%"
                if like_pattern_rev != like_pattern:
                    for row in conn.execute(
                        "SELECT id FROM functions WHERE name LIKE ? LIMIT 100",
                        (like_pattern_rev,)):
                        candidate_ids.add(row[0])

            # IMPROVE-2: bug-query candidate boost — try exact joined-name
            # match (e.g., "find_get_block_slow" → __find_get_block_slow).
            # This ensures the actual function named in the bug report is
            # in the candidate set even when generic-token substring matches
            # would otherwise dominate.
            if bug_query and query_tokens:
                joined = "_".join(t.lower() for t in query_tokens)
                for row in conn.execute(
                    "SELECT id FROM functions WHERE name = ? OR name LIKE ? LIMIT 50",
                    (joined, "%" + joined + "%")):
                    candidate_ids.add(row[0])
                # Also try with leading underscores (kernel convention)
                for row in conn.execute(
                    "SELECT id FROM functions WHERE name LIKE ? LIMIT 50",
                    ("__" + joined,)):
                    candidate_ids.add(row[0])

            # 3. Per-token substring matches (fallback for partial queries)
            for token in query_tokens:
                t = token.lower()
                if not t:
                    continue
                # Name prefix match (uses index)
                for row in conn.execute(
                    "SELECT id FROM functions WHERE name LIKE ? || '%' LIMIT 50",
                    (t,)):
                    candidate_ids.add(row[0])
                # Name substring match (slower but bounded by LIMIT)
                for row in conn.execute(
                    "SELECT id FROM functions WHERE name LIKE '%' || ? || '%' LIMIT 50",
                    (t,)):
                    candidate_ids.add(row[0])
                # ID substring match
                norm_t = _normalize_id(t)
                for row in conn.execute(
                    "SELECT id FROM functions WHERE id LIKE '%' || ? || '%' LIMIT 50",
                    (norm_t,)):
                    candidate_ids.add(row[0])

    # Build a quick in-memory suffix index if we have the graph
    if not candidate_ids:
        # Build a lightweight suffix map from node names
        suffix_map = defaultdict(list)
        # Build suffix map — use streaming (data=True) to avoid N+1
        # query pattern on LazySQLiteGraph (was: for nid in G.nodes:
        # then G.nodes[nid] = separate SELECT per node = 1.5M queries).
        for nid, nd in G.nodes(data=True):
            if nd.get("is_empty", False):
                continue
            name = nd.get("name", "").lower()
            norm = _normalize_id(name)
            suffix_map[norm].append(nid)
            parts = norm.split("_")
            for depth in range(1, min(len(parts) + 1, 4)):
                suffix = "_".join(parts[-depth:])
                suffix_map[suffix].append(nid)

        for token in query_tokens:
            t = token.lower()
            norm_t = _normalize_id(t)
            if norm_t in suffix_map:
                candidate_ids.update(suffix_map[norm_t])
            # Also check partial suffix matches — but only against keys that
            # could plausibly contain the token, using dict lookup instead of
            # iterating all keys (which would be O(N) and defeat the index).
            # For short tokens, check common suffix lengths; for longer tokens,
            # the exact norm_t match above already handles it.
            parts_t = norm_t.split("_")
            for depth in range(1, min(len(parts_t) + 1, 4)):
                suffix = "_".join(parts_t[-depth:])
                if suffix in suffix_map and suffix != norm_t:
                    candidate_ids.update(suffix_map[suffix][:5])

    # Score candidates (or all nodes if no candidates found)
    scored = []
    if candidate_ids:
        for nid in candidate_ids:
            if nid in G:
                ndata = G.nodes[nid]
                if ndata.get("is_empty", False):
                    continue
                score = _score_node_relevance(ndata, expanded_tokens,
                                              focus_domain=focus_domain,
                                              bug_query=bug_query)
                if score > 0:
                    scored.append((nid, score, ndata))
    else:
        # Fallback: full linear scan
        for nid, ndata in G.nodes(data=True):
            if ndata.get("is_empty", False):
                continue
            score = _score_node_relevance(ndata, expanded_tokens,
                                          focus_domain=focus_domain,
                                          bug_query=bug_query)
            if score > 0:
                scored.append((nid, score, ndata))

    scored.sort(key=lambda x: -x[1])
    if focus_domain:
        scored = [(nid, sc, nd) for nid, sc, nd in scored
                  if nd.get("domain", "").startswith(focus_domain)]
    return scored[:top_n]


def _explain_relevance(nd: dict, query_tokens: list) -> str:
    """Generate a human-readable explanation of why a node matched the query."""
    reasons = []
    name = nd.get("name", "").lower()
    domain = nd.get("domain", "").lower()
    semantic = nd.get("semantic_desc", "").lower()
    labels = nd.get("labels", [])

    for token in query_tokens:
        t = token.lower()
        if t in name:
            reasons.append(f"name contains '{t}'")
        elif t in domain:
            reasons.append(f"domain '{domain}' contains '{t}'")
        elif t in semantic:
            reasons.append(f"semantic description mentions '{t}'")

    if "API_entry" in labels and any(t in name for t in query_tokens):
        reasons.append("is a public API entry point")
    if "thread_processor" in labels:
        reasons.append("is a thread entry point")
    if "callback_func" in labels:
        reasons.append("is a callback function")

    if not reasons:
        reasons.append("partial keyword match")

    return "; ".join(reasons[:3])


def _extract_subgraph_context(G: nx.DiGraph, seed_nodes: list, max_depth: int = 2,
                               max_nodes: int = 15) -> dict:
    """Extract a subgraph around seed nodes with depth-limited BFS.

    max_depth adapts: small graphs (<500 nodes) use depth 3, large graphs use depth 2.
    Returns nodes, edges, and key paths.
    """
    visited = set()
    node_data = {}
    edge_data = []

    # BFS from each seed node
    for seed_id, _score, _ndata in seed_nodes:
        if len(visited) >= max_nodes:
            break
        queue = deque([(seed_id, 0)])
        while queue and len(visited) < max_nodes:
            nid, depth = queue.popleft()
            if nid in visited:
                continue
            visited.add(nid)
            nd = G.nodes[nid]
            node_data[nid] = {
                "id": nid,
                "name": nd.get("name", ""),
                "signature": nd.get("signature", ""),
                "domain": nd.get("domain", ""),
                "labels": nd.get("labels", []),
                "location": f"{nd.get('source_file', '')}:{nd.get('line', 0)}",
                "exec_summary": _derive_exec_summary(nd),
            }

            if depth < max_depth:
                for succ in G.successors(nid):
                    if succ not in visited:
                        ed = G.get_edge_data(nid, succ) or {}
                        # Skip non-call edges (CONTAINS, IMPORTS)
                        if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                            continue
                        edge_data.append({
                            "from": nid, "to": succ,
                            "from_name": nd.get("name", ""),
                            "to_name": G.nodes[succ].get("name", ""),
                            "call_condition": ed.get("call_condition", ""),
                            "concurrency": ed.get("concurrency", ""),
                            "call_order": ed.get("call_order"),
                            "confidence": ed.get("confidence", "EXTRACTED"),
                        })
                        queue.append((succ, depth + 1))
                for pred in G.predecessors(nid):
                    if pred not in visited:
                        ed = G.get_edge_data(pred, nid) or {}
                        # Skip non-call edges (CONTAINS, IMPORTS)
                        if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                            continue
                        edge_data.append({
                            "from": pred, "to": nid,
                            "from_name": G.nodes[pred].get("name", ""),
                            "to_name": nd.get("name", ""),
                            "call_condition": ed.get("call_condition", ""),
                            "concurrency": ed.get("concurrency", ""),
                            "call_order": ed.get("call_order"),
                            "confidence": ed.get("confidence", "EXTRACTED"),
                        })
                        queue.append((pred, depth + 1))

    return {"nodes": node_data, "edges": edge_data}


def _derive_exec_summary(nd: dict) -> str:
    """Derive 1-sentence execution summary from node data."""
    desc = nd.get("semantic_desc", "") or nd.get("external_desc", "")
    if desc:
        sentences = re.split(r'(?<=[.!?。！？])\s+', desc.strip())
        return sentences[0][:120] if sentences else desc[:120]
    labels = nd.get("labels", [])
    name = nd.get("name", "")
    if "API_entry" in labels:
        return f"Public API: {name}"
    if "thread_processor" in labels:
        return f"Thread entry: {name}"
    if "callback_func" in labels:
        return f"Callback: {name}"
    return ""


def _generate_exploration_summary(query: str, relevant_nodes: list, key_paths: list,
                                   context: dict) -> str:
    """Generate a 1-2 sentence natural language summary of exploration results."""
    parts = []
    n_nodes = len(relevant_nodes)
    top_name = relevant_nodes[0][2].get("name", "") if relevant_nodes else ""
    domains = set(nd.get("domain", "") for _, _, nd in relevant_nodes)

    if n_nodes == 0:
        return f"No functions matching '{query}'."

    parts.append(f"Found {n_nodes} function(s) related to '{query}'"
                 f" across {len(domains)} domain(s).")

    if key_paths:
        longest = max(key_paths, key=lambda p: p.get("length", 0))
        if longest.get("length", 0) > 1:
            parts.append(f"Key path: {longest['from']} → {longest['to']} "
                         f"({longest['length']} steps).")

    # Describe top match
    if top_name:
        top_summary = _derive_exec_summary(relevant_nodes[0][2])
        if top_summary:
            parts.append(f"Top match: {top_name} — {top_summary}")

    return " ".join(parts)


def _extract_key_paths(G: nx.DiGraph, seed_nodes: list, max_paths: int = 5) -> list:
    """Find key execution paths passing through or starting from seed nodes."""
    paths = []
    seed_ids = {s[0] for s in seed_nodes}

    # Find API→endpoint paths through any seed node
    api_nodes = []
    end_nodes = []
    cls_name = type(G).__name__
    if cls_name == "LazySQLiteGraph":
        # SQL-backed label search — uses idx on labels JSON via scan, but
        # we LIMIT to keep it bounded. Avoids iterating 1.5M nodes.
        conn = getattr(G, "_conn", None)
        if conn is not None:
            # Use indexed boolean columns when available, LIKE fallback otherwise
            _has_lc = "is_api_entry" in {r[1] for r in conn.execute("PRAGMA table_info(functions)").fetchall()}
            if _has_lc:
                for row in conn.execute(
                    "SELECT id FROM functions WHERE is_api_entry = 1 LIMIT 50"):
                    api_nodes.append(row[0])
                for row in conn.execute(
                    "SELECT id FROM functions WHERE is_out_end = 1 OR is_unknown_end = 1 LIMIT 50"):
                    end_nodes.append(row[0])
            else:
                for row in conn.execute(
                    "SELECT id FROM functions WHERE labels LIKE '%API_entry%' LIMIT 50"):
                    api_nodes.append(row[0])
                for row in conn.execute(
                    "SELECT id FROM functions WHERE labels LIKE '%out_end%' OR labels LIKE '%unknown_end%' LIMIT 50"):
                    end_nodes.append(row[0])
    else:
        for nid, ndata in G.nodes(data=True):
            if ndata.get("is_empty", False):
                continue
            labels = ndata.get("labels", [])
            if "API_entry" in labels:
                api_nodes.append(nid)
            if "out_end" in labels or "unknown_end" in labels:
                end_nodes.append(nid)

    # Build call-only subgraph once (exclude CONTAINS/IMPORTS).
    # On LazySQLiteGraph (1.5M+ nodes), this would OOM — skip and use BFS
    # with relation filtering on G directly.
    call_G = None
    if cls_name != "LazySQLiteGraph":
        from _builder.utils import _make_call_graph
        call_G = _make_call_graph(G)

    end_lookup = set(end_nodes[:10])
    for api_id in api_nodes[:10]:
        if len(paths) >= max_paths:
            break
        # Single BFS per api_id; reuse parent map for all end_id lookups.
        # Avoids 100 separate shortest_path calls on potentially 700K+ graphs.
        if call_G is not None:
            try:
                pred_map = nx.predecessor(call_G, api_id, cutoff=15)
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                continue
            for end_id in end_lookup:
                if len(paths) >= max_paths:
                    break
                if end_id not in pred_map or end_id == api_id:
                    continue
                path = [end_id]
                cur = end_id
                while cur != api_id:
                    preds = pred_map.get(cur)
                    if not preds:
                        path = None
                        break
                    cur = preds[0]
                    path.append(cur)
                if path is None:
                    continue
                path.reverse()
                # Check if any seed node is on this path
                if seed_ids & set(path):
                    steps = []
                    for i, nid in enumerate(path):
                        nd = G.nodes[nid]
                        step = {"name": nd.get("name", ""),
                                "domain": nd.get("domain", "")}
                        if i > 0:
                            ed = G.get_edge_data(path[i-1], nid) or {}
                            step["condition"] = ed.get("call_condition", "")
                            step["concurrency"] = ed.get("concurrency", "")
                        steps.append(step)
                    paths.append({
                        "from": G.nodes[api_id].get("name", ""),
                        "to": G.nodes[end_id].get("name", ""),
                        "length": len(path),
                        "steps": steps,
                    })
        else:
            for end_id in end_lookup:
                if len(paths) >= max_paths:
                    break
                try:
                    # BFS on G directly, skipping CONTAINS/IMPORTS edges.
                    path = _bfs_invoke_path(G, api_id, end_id, max_depth=15)
                    if not path:
                        continue
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    continue
                if seed_ids & set(path):
                    steps = []
                    for i, nid in enumerate(path):
                        nd = G.nodes[nid]
                        step = {"name": nd.get("name", ""),
                                "domain": nd.get("domain", "")}
                        if i > 0:
                            ed = G.get_edge_data(path[i-1], nid) or {}
                            step["condition"] = ed.get("call_condition", "")
                            step["concurrency"] = ed.get("concurrency", "")
                        steps.append(step)
                    paths.append({
                        "from": G.nodes[api_id].get("name", ""),
                        "to": G.nodes[end_id].get("name", ""),
                        "length": len(path),
                        "steps": steps,
                    })

    # If no API→endpoint paths through seeds, trace from seeds themselves
    if not paths:
        for seed_id, score, _ in seed_nodes[:3]:
            if len(paths) >= max_paths:
                break
            # BFS forward 5 levels
            visited = set()
            chain = []
            queue = deque([(seed_id, 0)])
            while queue:
                nid, depth = queue.popleft()
                if nid in visited or depth > 5:
                    continue
                visited.add(nid)
                nd = G.nodes[nid]
                chain.append({"name": nd.get("name", ""),
                              "domain": nd.get("domain", ""),
                              "condition": ""})
                if len(chain) > 8:
                    break
                for succ in G.successors(nid):
                    if succ not in visited:
                        ed = G.get_edge_data(nid, succ) or {}
                        if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                            continue
                        # Only set condition on the first successor encountered
                        # to avoid overwriting with later successors' conditions
                        if chain and not chain[-1]["condition"]:
                            chain[-1]["condition"] = ed.get("call_condition", "")
                        queue.append((succ, depth + 1))
            if len(chain) > 1:
                paths.append({
                    "from": chain[0]["name"],
                    "to": chain[-1]["name"],
                    "length": len(chain),
                    "steps": chain,
                })

    return paths


@cached_query('explore-flow', ttl=600, capture_stdout=True)
def cmd_explore_flow(args):
    """One-shot context retrieval: query → relevant nodes + paths + conditions."""
    graph_dir = args.graph
    query = args.query
    max_tokens = getattr(args, "max_tokens", 2000)
    max_nodes = getattr(args, "max_nodes", 15)
    focus_domain = getattr(args, "focus_domain", None)

    if not query or not query.strip():
        print(json.dumps({"error": "Empty query. Provide a search term like 'storage IO submission' or 'thread_entry'."},
                         ensure_ascii=False, indent=2))
        return

    G = _load_full_graph(graph_dir)

    # #10 fix: Exact symbol match pre-step — if the query exactly matches
    # a node name, return that node + its neighbors immediately instead
    # of going through BM25 scoring (which may return irrelevant results
    # for precise symbol names like "bdev_register").
    exact_match = None
    query_lower = query.strip().lower()
    # Use SQL index for LazySQLiteGraph (O(log N) instead of O(N) scan)
    cls_name = type(G).__name__
    if cls_name == "LazySQLiteGraph":
        conn = getattr(G, "_conn", None)
        if conn is not None:
            row = conn.execute(
                "SELECT id FROM functions WHERE lower(name) = ? LIMIT 1",
                (query_lower,)).fetchone()
            if row:
                exact_match = row[0]
        if not exact_match:
            # Fallback for non-ASCII names: SQLite's lower() is ASCII-only,
            # so non-ASCII uppercase names won't match. Fall through to
            # Python iteration which uses Unicode-aware .lower().
            for nid, nd in G.nodes(data=True):
                name = (nd.get("name") or "").lower()
                if name == query_lower:
                    exact_match = nid
                    break
    if exact_match:
        # Return the exact match + its 2-hop neighborhood
        from collections import deque
        visited = {exact_match}
        queue = deque([(exact_match, 0)])
        nodes_data = []
        while queue and len(nodes_data) < max_nodes:
            cur, depth = queue.popleft()
            if depth > 2:
                continue
            nd = G.nodes[cur]
            nodes_data.append({
                "id": cur, "name": nd.get("name", cur),
                "domain": nd.get("domain", ""),
                "labels": nd.get("labels", []),
                "depth": depth,
                "score": 100 if cur == exact_match else 80 - depth * 10,
            })
            for succ in G.successors(cur):
                ed = G.get_edge_data(cur, succ) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                if succ not in visited:
                    visited.add(succ)
                    queue.append((succ, depth + 1))
            for pred in G.predecessors(cur):
                ed = G.get_edge_data(pred, cur) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                if pred not in visited:
                    visited.add(pred)
                    queue.append((pred, depth + 1))
        print(json.dumps({
            "query": query,
            "exact_match": True,
            "nodes": nodes_data,
            "total": len(nodes_data),
            "note": "Exact symbol match — skipped BM25 scoring."
        }, ensure_ascii=False, indent=2, default=str))
        return

    # Adaptive depth: small graphs (<500 nodes) get depth 3, large graphs depth 2
    n_nodes = G.number_of_nodes()
    adaptive_depth = 3 if n_nodes < 500 else 2

    # Tokenize query (with camelCase/snake_case splitting)
    query_tokens = _tokenize_query(query)
    # Expand with synonyms (e.g., "释放" → release/free/put/destroy/done)
    # so natural-language queries match C function-name conventions.
    # Used by the fallback scoring path below.
    expanded_tokens = _expand_synonyms(query_tokens)

    # Find relevant nodes — _find_relevant_nodes expands tokens internally
    # for scoring; we pass original tokens for candidate lookup (the suffix
    # index and SQL LIKE match original tokens only).
    relevant = _find_relevant_nodes(G, query_tokens, top_n=max_nodes, graph_dir=graph_dir, focus_domain=focus_domain)

    if not relevant:
        # Fallback: try matching against node IDs directly
        for token in query_tokens:
            nid = _find_node_id(G, token)
            if nid and nid not in {r[0] for r in relevant}:
                nd = G.nodes[nid]
                score = _score_node_relevance(nd, expanded_tokens, focus_domain=focus_domain)
                relevant.append((nid, max(score, 1.0), nd))

    if not relevant:
        result = {"query": query, "result": "no_matching_nodes",
                  "suggestion": "Try different keywords or use 'search' command first"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # Extract subgraph context with adaptive depth
    context = _extract_subgraph_context(G, relevant, max_depth=adaptive_depth, max_nodes=max_nodes)

    # Extract key paths
    key_paths = _extract_key_paths(G, relevant, max_paths=5)

    # Generate exploration summary
    summary = _generate_exploration_summary(query, relevant, key_paths, context)

    # Build result
    result = {
        "query": query,
        "summary": summary,
        "matching_nodes": len(relevant),
        "top_matches": [
            {"name": nd.get("name", ""), "domain": nd.get("domain", ""),
             "labels": nd.get("labels", []), "location": f"{nd.get('source_file', '')}:{nd.get('line', 0)}",
             "exec_summary": _derive_exec_summary(nd),
             "relevance": round(score, 2),
             "relevance_reason": _explain_relevance(nd, query_tokens)}
            for nid, score, nd in relevant[:10]
        ],
        "subgraph": context,
        "key_paths": key_paths,
    }

    # Token budget control
    result_json = json.dumps(result, ensure_ascii=False)
    result_tokens = estimate_tokens(result_json)

    if max_tokens > 0 and result_tokens > max_tokens:
        # Progressively trim
        # 1. Trim top_matches to fewer entries
        if len(result.get("top_matches", [])) > 5:
            result["top_matches"] = result["top_matches"][:5]
        # 2. Remove edge data
        if "edges" in result.get("subgraph", {}):
            result["subgraph"]["edges"] = [
                {k: v for k, v in e.items() if k in ("from_name", "to_name", "call_condition", "concurrency")}
                for e in result["subgraph"]["edges"]
            ]
        # 3. Trim node data to minimal
        result_json = json.dumps(result, ensure_ascii=False)
        if estimate_tokens(result_json) > max_tokens:
            result["subgraph"]["nodes"] = {
                nid: {k: v for k, v in ndata.items() if k in ("id", "name", "domain", "exec_summary")}
                for nid, ndata in result["subgraph"]["nodes"].items()
            }
            result["subgraph"]["edges"] = [
                {k: v for k, v in e.items() if k in ("from_name", "to_name")}
                for e in result.get("subgraph", {}).get("edges", [])
            ]
        # 4. Trim paths
        result_json = json.dumps(result, ensure_ascii=False)
        if estimate_tokens(result_json) > max_tokens:
            result["key_paths"] = [
                {"from": p["from"], "to": p["to"], "length": p["length"],
                 "steps": [s["name"] for s in p.get("steps", [])]}
                for p in result.get("key_paths", [])
            ]
        # 5. Truncate subgraph edges if still over budget
        result_json = json.dumps(result, ensure_ascii=False)
        if estimate_tokens(result_json) > max_tokens:
            edges = result.get("subgraph", {}).get("edges", [])
            # Keep only half the edges, repeat if needed
            while edges and estimate_tokens(json.dumps(result, ensure_ascii=False)) > max_tokens:
                edges = edges[:len(edges)//2]
                result["subgraph"]["edges"] = edges
        # 6. Remove subgraph entirely if still over budget
        result_json = json.dumps(result, ensure_ascii=False)
        if estimate_tokens(result_json) > max_tokens:
            result.pop("subgraph", None)
            result.pop("key_paths", None)
        # 6. Final truncation
        result_json = json.dumps(result, ensure_ascii=False)
        if estimate_tokens(result_json) > max_tokens:
            truncated = truncate_to_tokens(result_json, max_tokens)
            try:
                last_brace = truncated.rfind('}')
                if last_brace > 0:
                    result = json.loads(truncated[:last_brace+1])
            except json.JSONDecodeError:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
    result["_token_count"] = estimate_tokens(json.dumps(result, ensure_ascii=False))
    print(json.dumps(result, ensure_ascii=False, indent=2))
