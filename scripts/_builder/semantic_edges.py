"""Semantic edges: READS / WRITES / ALLOCATES / FREES / LOCKS / UNLOCKS.

These complement the existing INVOKES / CONTAINS / IMPORTS edges with
finer-grained relationships between functions and the resources they
operate on. The edges are inferred from the function body text using
profile patterns and built-in defaults.

Edge types:
  - READS:    function reads a global / struct field
  - WRITES:   function writes a global / struct field
  - ALLOCATES: function allocates a resource (kmalloc, malloc, new, etc.)
  - FREES:    function frees a resource (kfree, free, delete, etc.)
  - LOCKS:    function acquires a lock (mutex_lock, spin_lock, etc.)
  - UNLOCKS:  function releases a lock (mutex_unlock, spin_unlock, etc.)

Resource target identification:
  - For ALLOCATES/FREES, the target is the allocation-family name
    (e.g., "kmalloc", "kfree") — these are "function-family" edges.
  - For LOCKS/UNLOCKS, the target is the lock variable name when
    extractable, else the lock function name (e.g., "mutex_lock").
  - For READS/WRITES, existing field_access / global_access tables
    already capture this — we don't duplicate.

Resource nodes (D1 first-class citizenship) is partially addressed by
allowing edges to non-function targets (the resource name itself is
stored as a virtual node with node_type='resource').

Query commands:
  - who-allocates --resource R: list functions that allocate R
  - who-frees --resource R:     list functions that free R
  - unbalanced-alloc-free:      find functions that alloc but never free
                                (or vice versa) within their component
"""
import json
import os
import re
import sys
from typing import Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Built-in semantic patterns
# ---------------------------------------------------------------------------

DEFAULT_ALLOC_PATTERNS = [
    r'\bkmalloc\s*\(', r'\bkzalloc\s*\(', r'\bvmalloc\s*\(',
    r'\bmalloc\s*\(', r'\bcalloc\s*\(', r'\brealloc\s*\(',
    r'\bnew\s+[A-Z][A-Za-z_0-9]*\s*[\(\{]',  # C++ new
    r'\bg_new\s*\(', r'\bg_malloc\s*\(',
    r'\balloc_pages\s*\(', r'\b__get_free_pages\s*\(',
    r'\bcreate_.*\s*\(',  # create_X() constructors
    r'\bmake_.*\s*\(',
]

DEFAULT_FREE_PATTERNS = [
    r'\bkfree\s*\(', r'\bvfree\s*\(', r'\bfree\s*\(',
    r'\bdelete\s+',  # C++ delete
    r'\bg_free\s*\(', r'\bg_clear_pointer\s*\(',
    r'\bdestroy_.*\s*\(', r'\bcleanup_.*\s*\(',
    r'\bfree_pages\s*\(', r'\b__free_pages\s*\(',
    r'\bput_pages\s*\(',
]

DEFAULT_LOCK_PATTERNS = [
    r'\bmutex_lock\s*\(', r'\bmutex_lock_interruptible\s*\(',
    r'\bspin_lock\s*\(', r'\bspin_lock_irqsave\s*\(',
    r'\braw_spin_lock\s*\(', r'\b_down\s*\(',
    r'\bdown_read\s*\(', r'\bdown_write\s*\(',
    r'\bread_lock\s*\(', r'\bwrite_lock\s*\(',
    r'\brtc_spin_lock\s*\(',
]

DEFAULT_UNLOCK_PATTERNS = [
    r'\bmutex_unlock\s*\(', r'\bspin_unlock\s*\(',
    r'\bspin_unlock_irqrestore\s*\(', r'\braw_spin_unlock\s*\(',
    r'\b_up\s*\(', r'\bup_read\s*\(', r'\bup_write\s*\(',
    r'\bread_unlock\s*\(', r'\bwrite_unlock\s*\(',
    r'\brtc_spin_unlock\s*\(',
]


def _compile_patterns(patterns: List[str]) -> List[re.Pattern]:
    return [re.compile(p) for p in patterns]


def _detect_resource_calls(body_text: str, patterns: List[re.Pattern]) -> List[str]:
    """Detect resource-family function calls in body text. Returns unique names."""
    found: List[str] = []
    seen: Set[str] = set()
    for pat in patterns:
        for m in pat.finditer(body_text):
            # Extract the function name (first identifier matched)
            name = m.group(0).strip().rstrip('(').strip()
            # For 'new Foo(' patterns, include 'new Foo'
            if name and name not in seen:
                seen.add(name)
                found.append(name)
    return found


def _detect_lock_var(body_text: str, lock_pattern: re.Pattern) -> Optional[str]:
    """Try to extract the lock variable name from a lock call.

    e.g., 'mutex_lock(&my_lock)' -> 'my_lock'
    """
    m = lock_pattern.search(body_text)
    if not m:
        return None
    # The pattern matches up to and including '('. Look at what's after.
    end = m.end()
    rest = body_text[end:end + 80]
    # Skip optional '&' (address-of) and extract identifier
    arg_match = re.match(r'\s*&\s*([A-Za-z_][A-Za-z_0-9]*)', rest)
    if arg_match:
        return arg_match.group(1)
    # Fall back: no '&', just an identifier
    arg_match = re.match(r'\s*([A-Za-z_][A-Za-z_0-9]*)', rest)
    if arg_match:
        return arg_match.group(1)
    return None


def detect_semantic_edges(node_data: Dict, profile: Optional[Dict] = None) -> List[Dict]:
    """Detect semantic edges (ALLOCATES/FREES/LOCKS/UNLOCKS) for a single function.

    Returns a list of edge dicts: {"relation": ..., "target": ..., "line": ...}
    The caller is responsible for adding these to the graph.
    """
    body_text = node_data.get("body_text", "") or ""
    if not body_text:
        return []

    # Get patterns from profile or fall back to defaults
    cp = ((profile or {}).get("concurrency_patterns") or {})
    alloc_pats = cp.get("alloc_patterns") or DEFAULT_ALLOC_PATTERNS
    free_pats = cp.get("free_patterns") or DEFAULT_FREE_PATTERNS
    lock_pats = cp.get("lock_acquire_patterns") or DEFAULT_LOCK_PATTERNS
    unlock_pats = cp.get("lock_release_patterns") or DEFAULT_UNLOCK_PATTERNS

    edges: List[Dict] = []
    invoker_id = node_data.get("id", "")

    # ALLOCATES edges
    for resource in _detect_resource_calls(body_text, _compile_patterns(alloc_pats)):
        edges.append({
            "invoker_id": invoker_id,
            "target": resource,
            "target_kind": "resource",
            "relation": "ALLOCATES",
            "confidence": "EXTRACTED",
        })

    # FREES edges
    for resource in _detect_resource_calls(body_text, _compile_patterns(free_pats)):
        edges.append({
            "invoker_id": invoker_id,
            "target": resource,
            "target_kind": "resource",
            "relation": "FREES",
            "confidence": "EXTRACTED",
        })

    # LOCKS edges (with variable name when extractable)
    for lock_pat in _compile_patterns(lock_pats):
        for m in lock_pat.finditer(body_text):
            lock_func = m.group(0).rstrip("(").strip()
            lock_var = _detect_lock_var(body_text, lock_pat)
            target = lock_var or lock_func
            edges.append({
                "invoker_id": invoker_id,
                "target": target,
                "target_kind": "lock",
                "relation": "LOCKS",
                "confidence": "EXTRACTED",
                "lock_function": lock_func,
            })

    # UNLOCKS edges
    for unlock_pat in _compile_patterns(unlock_pats):
        for m in unlock_pat.finditer(body_text):
            unlock_func = m.group(0).rstrip("(").strip()
            unlock_var = _detect_lock_var(body_text, unlock_pat)
            target = unlock_var or unlock_func
            edges.append({
                "invoker_id": invoker_id,
                "target": target,
                "target_kind": "lock",
                "relation": "UNLOCKS",
                "confidence": "EXTRACTED",
                "lock_function": unlock_func,
            })

    # HOLDER edges — link protected object to lock holder.
    # Uses profile.lock_semantics (function-name-based, complementary to the
    # regex-based lock_acquire_patterns above). When a function calls a
    # declared acquire primitive with `locks_object_at` >= 0, emit a HOLDER
    # edge from the protected object (arg at that index) to the calling
    # function. detect-races / path-guards can then annotate race evidence
    # with "writer holds <lock> on <object>" context.
    lock_semantics = (profile or {}).get("lock_semantics") or []
    if lock_semantics and body_text:
        # Build a regex per declared primitive: name(args) — capture args.
        for entry in lock_semantics:
            fn = entry.get("function", "")
            kind = entry.get("kind", "")
            if not fn or kind != "acquire":
                continue
            arg_idx = entry.get("arg_index", 0)
            obj_idx = entry.get("locks_object_at", -1)
            if obj_idx < 0:
                continue  # No protected-object index declared — skip HOLDER.
            # Match fn(arg0, arg1, ...) — capture the full arg list.
            call_re = re.compile(
                r'\b' + re.escape(fn) + r'\s*\(([^)]*)\)'
            )
            for m in call_re.finditer(body_text):
                arg_str = m.group(1)
                args = [a.strip() for a in arg_str.split(',')]
                if arg_idx >= len(args) or obj_idx >= len(args):
                    continue
                lock_arg = args[arg_idx]
                obj_arg = args[obj_idx]
                # Strip leading & or * for the lock and object identifiers.
                lock_name = lock_arg.lstrip('&*').strip()
                obj_name = obj_arg.lstrip('&*').strip()
                # Strip field accesses: for `mutex_lock(&sb->s_lock)`,
                # the protected object is `sb` (the head of the chain).
                head_match = re.match(r'([A-Za-z_]\w*)', obj_name)
                if not head_match:
                    continue
                obj_head = head_match.group(1)
                edges.append({
                    "invoker_id": invoker_id,
                    "target": obj_head,
                    "target_kind": "object",
                    "relation": "HOLDER",
                    "confidence": "EXTRACTED",
                    "lock_function": fn,
                    "lock_variable": lock_name,
                    "protected_object": obj_head,
                })

    return edges


def add_semantic_edges_to_graph(G, profile: Optional[Dict] = None) -> int:
    """Walk all function nodes in G and add semantic edges as edge attributes.

    Edges are added with relation in (ALLOCATES, FREES, LOCKS, UNLOCKS).
    The target is stored as the invoked_id (a virtual resource/lock node).

    Because networkx DiGraph stores one edge per (u, v) pair, we encode the
    relation in the target id when the same resource is both locked and
    unlocked by the same function (so LOCKS and UNLOCKS become distinct
    edges to distinct virtual nodes).

    Returns the number of edges added.
    """
    added = 0
    for nid, nd in list(G.nodes(data=True)):
        if nd.get("node_type") == "file" or nd.get("is_empty"):
            continue
        sem_edges = detect_semantic_edges(nd, profile)
        # Deduplicate within this function: same (relation, target) only once
        seen_edges = set()
        for e in sem_edges:
            key = (e["relation"], e["target"])
            if key in seen_edges:
                continue
            seen_edges.add(key)
            # Encode relation in target id when needed to avoid (u, v) collision
            # for LOCKS vs UNLOCKS on the same lock variable.
            if e["relation"] in ("LOCKS", "UNLOCKS"):
                target_id = f"resource::{e['relation'].lower()}::{e['target']}"
            else:
                target_id = f"resource::{e['target']}"
            if not G.has_node(target_id):
                G.add_node(target_id, name=e["target"],
                           node_type=e["target_kind"],
                           labels=[e["target_kind"]],
                           is_virtual=True)
            if not G.has_edge(nid, target_id):
                G.add_edge(nid, target_id,
                           relation=e["relation"],
                           confidence=e["confidence"],
                           concurrency="semantic",
                           lock_function=e.get("lock_function", ""))
                added += 1
    return added


# ---------------------------------------------------------------------------
# Query commands
# ---------------------------------------------------------------------------

def who_allocates(G, resource: str = "") -> List[Dict]:
    """Find functions that allocate a resource (or any resource if no name)."""
    results = []
    for u, v, d in G.edges(data=True):
        if d.get("relation") != "ALLOCATES":
            continue
        if G.has_node(v):
            target_name = G.nodes[v].get("name", v)
        elif "::" in v:
            target_name = v.split("::", 1)[-1]
        else:
            target_name = v
        if resource and target_name != resource:
            continue
        nd = G.nodes[u]
        results.append({
            "function_id": u,
            "function_name": nd.get("name", ""),
            "resource": target_name,
            "source_file": nd.get("source_file", ""),
            "line": nd.get("line", 0),
        })
    return results


def who_frees(G, resource: str = "") -> List[Dict]:
    """Find functions that free a resource (or any resource if no name)."""
    results = []
    for u, v, d in G.edges(data=True):
        if d.get("relation") != "FREES":
            continue
        if G.has_node(v):
            target_name = G.nodes[v].get("name", v)
        elif "::" in v:
            target_name = v.split("::", 1)[-1]
        else:
            target_name = v
        if resource and target_name != resource:
            continue
        nd = G.nodes[u]
        results.append({
            "function_id": u,
            "function_name": nd.get("name", ""),
            "resource": target_name,
            "source_file": nd.get("source_file", ""),
            "line": nd.get("line", 0),
        })
    return results


def unbalanced_alloc_free(G) -> Dict:
    """Find functions that allocate but never free, or free but never allocate.

    Heuristic: compare a function's ALLOCATES count vs FREES count.
    A function with ALLOCATES > 0 and FREES == 0 is flagged as
    'allocates_without_free'. A function with FREES > 0 and ALLOCATES == 0
    is flagged as 'frees_without_alloc' (this is often legitimate —
    e.g., a destructor — so we report but don't claim it's a bug).
    """
    def _name(v):
        if G.has_node(v):
            return G.nodes[v].get("name", v)
        if "::" in v:
            return v.split("::", 1)[-1]
        return v

    allocators: Dict[str, List[str]] = {}
    freers: Dict[str, List[str]] = {}
    for u, v, d in G.edges(data=True):
        rel = d.get("relation")
        if rel == "ALLOCATES":
            allocators.setdefault(u, []).append(_name(v))
        elif rel == "FREES":
            freers.setdefault(u, []).append(_name(v))

    alloc_no_free = []
    free_no_alloc = []
    imbalance = []

    all_func_ids = set(allocators) | set(freers)
    for fid in all_func_ids:
        allocs = allocators.get(fid, [])
        frees = freers.get(fid, [])
        if allocs and not frees:
            alloc_no_free.append({
                "function_id": fid,
                "function_name": G.nodes[fid].get("name", "") if fid in G else "",
                "allocated_resources": allocs,
            })
        elif frees and not allocs:
            free_no_alloc.append({
                "function_id": fid,
                "function_name": G.nodes[fid].get("name", "") if fid in G else "",
                "freed_resources": frees,
            })
        elif allocs and frees and len(allocs) != len(frees):
            imbalance.append({
                "function_id": fid,
                "function_name": G.nodes[fid].get("name", "") if fid in G else "",
                "alloc_count": len(allocs),
                "free_count": len(frees),
                "allocated_resources": allocs,
                "freed_resources": frees,
            })

    return {
        "allocates_without_free": alloc_no_free,
        "frees_without_alloc": free_no_alloc,
        "count_imbalance": imbalance,
        "total_allocators": len(allocators),
        "total_freers": len(freers),
    }


def who_locks(G, lock_name: str = "") -> List[Dict]:
    """Find functions that acquire a lock (or any lock if no name)."""
    results = []
    for u, v, d in G.edges(data=True):
        if d.get("relation") != "LOCKS":
            continue
        # Resolve target name: prefer node 'name' attr (works for both
        # resource::lock and resource::locks::lock_var target_id formats).
        if G.has_node(v):
            target_name = G.nodes[v].get("name", v)
        elif "::" in v:
            target_name = v.split("::", 1)[-1]
        else:
            target_name = v
        if lock_name and target_name != lock_name:
            continue
        nd = G.nodes[u]
        results.append({
            "function_id": u,
            "function_name": nd.get("name", ""),
            "lock": target_name,
            "lock_function": d.get("lock_function", ""),
            "source_file": nd.get("source_file", ""),
        })
    return results


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_who_allocates(args):
    """List functions that allocate a resource."""
    from _builder.graph_build import _load_full_graph
    G = _load_full_graph(args.graph)
    results = who_allocates(G, getattr(args, "resource", "") or "")
    print(json.dumps({"count": len(results), "functions": results},
                     indent=2, ensure_ascii=False))


def cmd_who_frees(args):
    """List functions that free a resource."""
    from _builder.graph_build import _load_full_graph
    G = _load_full_graph(args.graph)
    results = who_frees(G, getattr(args, "resource", "") or "")
    print(json.dumps({"count": len(results), "functions": results},
                     indent=2, ensure_ascii=False))


def cmd_unbalanced_alloc_free(args):
    """Find functions that allocate without freeing (or vice versa)."""
    from _builder.graph_build import _load_full_graph
    G = _load_full_graph(args.graph)
    result = unbalanced_alloc_free(G)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_who_locks(args):
    """List functions that acquire a lock."""
    from _builder.graph_build import _load_full_graph
    G = _load_full_graph(args.graph)
    results = who_locks(G, getattr(args, "lock", "") or "")
    print(json.dumps({"count": len(results), "functions": results},
                     indent=2, ensure_ascii=False))


def cmd_add_semantic_edges(args):
    """Walk the graph and add ALLOCATES/FREES/LOCKS/UNLOCKS edges to function nodes."""
    from _builder.graph_build import _load_full_graph, split_by_domain
    G = _load_full_graph(args.graph)
    profile = None
    profile_path = os.path.join(args.graph, ".code2database_profile.json")
    if os.path.exists(profile_path):
        try:
            with open(profile_path) as f:
                profile = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    added = add_semantic_edges_to_graph(G, profile)
    # Persist
    if added > 0:
        try:
            split_by_domain(G, args.graph, "")
        except Exception as exc:
            print(f"Warning: persist failed: {exc}", file=sys.stderr)
    print(json.dumps({"semantic_edges_added": added}, indent=2, ensure_ascii=False))
