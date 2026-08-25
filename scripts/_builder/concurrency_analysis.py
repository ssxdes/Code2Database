"""Data race detection for the invocation graph.

Builds on E2 (shared state: globals_read/written, fields_read/written) and
E4 (thread model: thread_model, thread_entry, thread_model_inherited) to
detect potential data races between functions executing in different thread
contexts that access the same shared resource without proper synchronization.
"""

import os
import re
import sys
from collections import defaultdict

from _builder.utils import _output_result, _find_node_id, _get_body_text
from _builder.graph_build import _load_full_graph


# ---------------------------------------------------------------------------
# Mutex / lock detection heuristics (profile-driven)
# ---------------------------------------------------------------------------
# Lock acquire/release patterns are compiled from
# profile.concurrency_patterns.lock_acquire_patterns / lock_release_patterns.
# Project-agnostic: the tool default has EMPTY lists (no hardcoded lock APIs);
# auto-profile detects project-specific lock APIs by scanning source code,
# and built-in reference profiles (linux_kernel.json, spdk.json, etc.)
# populate these with project-appropriate patterns.
# When no profile is loaded, lock detection is disabled (returns empty set),
# which is the safe behavior — no false lock-protection claims.

_LOCK_PATTERN_CACHE = {}  # profile_id → (acquire_patterns, release_patterns)


def _compile_lock_patterns(profile):
    """Compile lock acquire/release regex patterns from the profile.

    Args:
        profile: Builder config dict from ProfileSchema.to_builder_config(),
                 or None.

    Returns:
        Tuple (acquire_patterns, release_patterns) — each a list of compiled
        regex patterns. Empty lists when profile is None or has no patterns.
    """
    if not profile:
        return [], []
    cp = profile.get("concurrency_patterns", {}) if isinstance(profile, dict) else {}
    acquire_strs = cp.get("lock_acquire_patterns", []) or []
    release_strs = cp.get("lock_release_patterns", []) or []
    # Cache key by id() — profiles are typically loaded once per command
    cache_key = id(profile)
    if cache_key in _LOCK_PATTERN_CACHE:
        return _LOCK_PATTERN_CACHE[cache_key]
    acquire = []
    for pat_str in acquire_strs:
        try:
            acquire.append(re.compile(pat_str))
        except re.error:
            pass  # Skip invalid patterns silently — profile validation should catch these
    release = []
    for pat_str in release_strs:
        try:
            release.append(re.compile(pat_str))
        except re.error:
            pass
    _LOCK_PATTERN_CACHE[cache_key] = (acquire, release)
    return acquire, release


def _load_profile_from_graph_dir(graph_dir):
    """Load the persisted profile from the graph output directory.

    The build command persists the builder profile to
    <graph_dir>/.code2database_profile.json so that downstream query commands
    (detect-races, concurrency-analyze) can access project-specific patterns
    without requiring --profile to be re-specified.

    Returns:
        Builder config dict, or None if not found.
    """
    if not graph_dir:
        return None
    profile_path = os.path.join(graph_dir, ".code2database_profile.json")
    if not os.path.isfile(profile_path):
        return None
    try:
        import json
        with open(profile_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return None


def _detect_locks_held(ndata, profile=None, G=None, nid=None):
    """Return a set of lock variable names that are acquired in this function.

    Uses profile.concurrency_patterns.lock_acquire_patterns to detect locks.
    When profile is None or has empty patterns, returns an empty set (no
    lock protection detected) — this is the safe behavior, avoiding false
    claims of lock protection.

    Args:
        ndata: Node attrs dict (must contain body_text OR callee_args).
            When G and nid are also provided, body_text is fetched lazily
            via _get_body_text (handles SQLite LazySQLiteGraph where
            body_text is compressed in body_text_compressed and not
            auto-decompressed on batch iteration).
        profile: Profile dict with concurrency_patterns.
        G: Optional graph (for lazy body_text fetch from SQLite).
        nid: Optional node id (required when G is provided).

    Returns a set of lock names (strings).  An empty set means no lock
    protection was detected.
    """
    acquire_patterns, release_patterns = _compile_lock_patterns(profile)
    if not acquire_patterns and not release_patterns:
        return set()  # No patterns → no lock detection (safe default)

    locks_acquired = set()
    locks_released = set()

    # Use _get_body_text when G is provided — on StreamingGraph /
    # LazySQLiteGraph, body_text is empty in cached attrs (compressed in
    # body_text_compressed to avoid per-fetch zlib cost). Without this,
    # lock detection silently returns empty for all --low-memory builds.
    if G is not None and nid is not None:
        body = _get_body_text(G, nid)
    else:
        body = ndata.get("body_text", "")
    for pat in acquire_patterns:
        for m in pat.finditer(body):
            groups = m.groups()
            if groups:
                locks_acquired.add(groups[0].lstrip("&"))
            else:
                locks_acquired.add("__rcu_read_lock__")
    for pat in release_patterns:
        for m in pat.finditer(body):
            groups = m.groups()
            if groups:
                locks_released.add(groups[0].lstrip("&"))
            else:
                locks_released.add("__rcu_read_lock__")

    # Also check callee_args for lock API calls
    for ca in ndata.get("callee_args", []):
        callee_name = ca.get("callee", "")
        for pat in acquire_patterns:
            m = pat.search(callee_name + "()")
            if m:
                groups = m.groups()
                if groups:
                    locks_acquired.add(groups[0].lstrip("&"))
                else:
                    locks_acquired.add("__rcu_read_lock__")
        for pat in release_patterns:
            m = pat.search(callee_name + "()")
            if m:
                groups = m.groups()
                if groups:
                    locks_released.add(groups[0].lstrip("&"))
                else:
                    locks_released.add("__rcu_read_lock__")

    # If a lock is acquired and released within the same function, the
    # critical section is internal and does not protect against races
    # with other functions.  We only consider locks that are *held*
    # while accessing shared data -- for simplicity, if the lock appears
    # in the body we consider it potentially held.  A more precise
    # analysis would require dominator/post-dominator analysis.
    return locks_acquired


# ---------------------------------------------------------------------------
# Thread context helpers
# ---------------------------------------------------------------------------

def _get_thread_context(ndata):
    """Return a tuple (thread_model, thread_entry) for a node.

    thread_model: the model this function runs in (direct or inherited).
    thread_entry: the entry-point function name for this thread context,
                  or None if the function is a thread entry itself.

    Returns (model, entry_name) where model may be None.
    """
    model = ndata.get("thread_model") or ndata.get("thread_model_inherited")
    entry = ndata.get("thread_entry", False)
    entry_name = ndata.get("name", "") if entry else None
    return model, entry_name


def _same_thread_context(ndata_a, ndata_b):
    """Return True if two functions are in the same thread context.

    Same context means: same thread_model AND same thread_entry point.
    Functions with no thread context at all are considered the same
    context (main/unknown) -- we do NOT report races between two
    unknown-context functions since that would be too noisy.
    """
    model_a, entry_a = _get_thread_context(ndata_a)
    model_b, entry_b = _get_thread_context(ndata_b)

    # If neither has a thread context, they are assumed to be in the
    # same default context -- skip to avoid false positives.
    if model_a is None and model_b is None:
        return True

    # Same explicit thread entry point → same context
    if entry_a and entry_b and entry_a == entry_b:
        return True

    # Different models → definitely different contexts
    if model_a != model_b:
        return False

    # Same model but different entry points → different contexts
    # (e.g., two pthreads created from different entry functions)
    if entry_a != entry_b:
        return False

    # Same model and both None entries → same default context for that model
    return True


# ---------------------------------------------------------------------------
# Core: detect_data_races
# ---------------------------------------------------------------------------

def detect_data_races(G, target_func=None, profile=None):
    """Detect potential data races in the invocation graph.

    A data race exists when two functions in different thread contexts
    access the same shared resource (global variable or struct field)
    and at least one access is a write, with no common mutex protection.

    Args:
        G: networkx DiGraph (the full invocation graph with E2/E4 attributes).
        target_func: If given, only report races involving this function.
        profile: Builder config dict (from ProfileSchema.to_builder_config())
                 used for lock detection. When None, lock detection is
                 disabled (no false lock-protection claims).

    Returns:
        List of race dicts, each with the schema described in the module docstring.
    """
    # ------------------------------------------------------------------
    # Step 1: Build resource → accessor index
    # ------------------------------------------------------------------
    # resource_key -> list of (node_id, access_type)
    # access_type is "read" or "write"
    # For globals:   resource_key = "global_var:<name>"
    # For fields:    resource_key = "struct_field:<struct>.<field>"
    resource_accessors = defaultdict(list)

    # Also track per-function lock sets for protection detection
    func_locks = {}  # node_id -> set of lock names

    target_nid = None
    if target_func:
        target_nid = _find_node_id(G, target_func)
        if not target_nid:
            return []

    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        if ndata.get("node_type") == "file":
            continue

        # If target_func was specified, only index this node if it is
        # the target or could race with the target.  We don't know the
        # target's thread context yet, so index everything and filter
        # later.
        # (For the target_func path we will filter in Step 2.)

        # Detect locks held by this function (profile-driven).
        # Pass G/nid so _detect_locks_held can lazily fetch body_text from
        # SQLite on StreamingGraph / LazySQLiteGraph (low-memory builds).
        func_locks[nid] = _detect_locks_held(ndata, profile, G=G, nid=nid)

        # Index global variables
        for g in ndata.get("globals_read", []):
            gname = g.get("name", "") if isinstance(g, dict) else str(g)
            if gname:
                resource_accessors[("global_var", gname)].append(
                    (nid, "read"))

        for g in ndata.get("globals_written", []):
            gname = g.get("name", "") if isinstance(g, dict) else str(g)
            if gname:
                resource_accessors[("global_var", gname)].append(
                    (nid, "write"))

        # Index struct fields
        for f in ndata.get("fields_read", []):
            if isinstance(f, dict):
                sc = f.get("struct_chain", "")
                fn = f.get("field_name", "")
                if sc and fn:
                    resource_accessors[("struct_field", f"{sc}.{fn}")].append(
                        (nid, "read"))

        for f in ndata.get("fields_written", []):
            if isinstance(f, dict):
                sc = f.get("struct_chain", "")
                fn = f.get("field_name", "")
                if sc and fn:
                    resource_accessors[("struct_field", f"{sc}.{fn}")].append(
                        (nid, "write"))

    # ------------------------------------------------------------------
    # Step 2: Check pairs within each resource's accessor list
    # ------------------------------------------------------------------
    races = []
    race_counter = 0
    seen_pairs = set()  # (nid_a, nid_b, resource_key) to dedup

    for (rtype, rname), accessors in resource_accessors.items():
        if len(accessors) < 2:
            continue

        # If target_func specified, at least one accessor must be target_nid
        if target_nid:
            has_target = any(nid == target_nid for nid, _ in accessors)
            if not has_target:
                continue

        for i in range(len(accessors)):
            for j in range(i + 1, len(accessors)):
                nid_a, access_a = accessors[i]
                nid_b, access_b = accessors[j]

                # Order the pair consistently for dedup
                if nid_a > nid_b:
                    nid_a, nid_b = nid_b, nid_a
                    access_a, access_b = access_b, access_a

                pair_key = (nid_a, nid_b, rtype, rname)
                if pair_key in seen_pairs:
                    continue

                ndata_a = G.nodes[nid_a]
                ndata_b = G.nodes[nid_b]

                # Skip if same thread context
                if _same_thread_context(ndata_a, ndata_b):
                    continue

                # If target_func specified, one must be the target
                if target_nid and nid_a != target_nid and nid_b != target_nid:
                    continue

                seen_pairs.add(pair_key)

                # Determine severity
                if access_a == "write" or access_b == "write":
                    severity = "high"
                else:
                    severity = "low"

                # Determine protection: if both functions hold the same
                # mutex, the race is likely protected
                locks_a = func_locks.get(nid_a, set())
                locks_b = func_locks.get(nid_b, set())
                common_locks = locks_a & locks_b
                protection = "none"
                if common_locks:
                    # Use the first common lock name as representative
                    protection = sorted(common_locks)[0]

                # Determine confidence
                model_a, entry_a = _get_thread_context(ndata_a)
                model_b, entry_b = _get_thread_context(ndata_b)

                if (entry_a and entry_b and entry_a != entry_b
                        and (access_a == "write" or access_b == "write")):
                    confidence = "high"
                else:
                    confidence = "medium"

                race_counter += 1
                races.append({
                    "race_id": f"race_{race_counter}",
                    "thread_a": {
                        "function": ndata_a.get("name", nid_a),
                        "thread_model": model_a or "unknown",
                        "thread_entry": entry_a or "unknown",
                    },
                    "thread_b": {
                        "function": ndata_b.get("name", nid_b),
                        "thread_model": model_b or "unknown",
                        "thread_entry": entry_b or "unknown",
                    },
                    "shared_resource": {
                        "name": rname,
                        "type": rtype,
                        "access_a": access_a,
                        "access_b": access_b,
                    },
                    "protection": protection,
                    "severity": severity,
                    "confidence": confidence,
                })

    # Sort: high severity first, then high confidence, then by resource name
    races.sort(key=lambda r: (
        0 if r["severity"] == "high" else 1,
        0 if r["confidence"] == "high" else 1,
        r["shared_resource"]["name"],
    ))

    return races


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def cmd_detect_races(args):
    """CLI handler for the detect-races subcommand."""
    G = _load_full_graph(args.graph)

    target_func = getattr(args, "func", None)
    min_severity = getattr(args, "min_severity", "low")
    json_mode = getattr(args, "json", False)

    # Load profile from graph dir (persisted by build) or --profile arg.
    # Profile provides project-specific lock APIs for accurate race
    # protection detection.
    profile = None
    profile_arg = getattr(args, "profile", None)
    if profile_arg:
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
            from _profile import ProfileSchema
            profile = ProfileSchema.load(profile_arg).to_builder_config()
        except Exception as _e:
            print(f"[detect-races] Warning: failed to load --profile {profile_arg}: {_e}",
                  file=sys.stderr)
    if profile is None:
        profile = _load_profile_from_graph_dir(args.graph)

    # Check if profile has lock patterns configured. If not, race detection
    # may silently miss races (no lock protection info). Warn the user.
    cp = (profile or {}).get("concurrency_patterns", {}) if profile else {}
    has_lock_acquire = bool(cp.get("lock_acquire_patterns"))
    has_lock_release = bool(cp.get("lock_release_patterns"))
    profile_warning = ""
    if not (has_lock_acquire and has_lock_release):
        profile_warning = (
            "Profile lacks lock_acquire_patterns / lock_release_patterns — "
            "race detection may UNDERREPORT (no lock protection info). "
            "Run auto-profile or patch-profile --add-lock-acquire-pattern "
            "to enable full detection."
        )
        print(f"[detect-races] WARNING: {profile_warning}", file=sys.stderr)

    races = detect_data_races(G, target_func=target_func, profile=profile)

    # Filter by minimum severity
    min_level = _SEVERITY_ORDER.get(min_severity, 2)
    races = [r for r in races
             if _SEVERITY_ORDER.get(r["severity"], 2) <= min_level]

    if json_mode:
        result = {
            "total_races": len(races),
            "high_severity": sum(1 for r in races if r["severity"] == "high"),
            "low_severity": sum(1 for r in races if r["severity"] == "low"),
            "protected": sum(1 for r in races if r["protection"] != "none"),
            "unprotected": sum(1 for r in races if r["protection"] == "none"),
            "races": races,
        }
        if profile_warning:
            result["profile_warning"] = profile_warning
        _output_result(result, json_mode=True)
    else:
        _print_race_table(races)
        if profile_warning:
            print()
            print(f"WARNING: {profile_warning}")


def _print_race_table(races):
    """Print races in a human-readable table format."""
    if not races:
        print("No data races detected.")
        return

    high = sum(1 for r in races if r["severity"] == "high")
    low = sum(1 for r in races if r["severity"] == "low")
    protected = sum(1 for r in races if r["protection"] != "none")

    print(f"Data Races: {len(races)} detected "
          f"({high} high severity, {low} low severity, "
          f"{protected} protected by mutex)")
    print()

    # Header
    fmt = "{:<10} {:<28} {:<28} {:<30} {:<6} {:<8} {:<10} {}"
    print(fmt.format(
        "Race ID", "Thread A Func", "Thread B Func",
        "Shared Resource", "Acc A", "Acc B", "Severity", "Protection"))
    print("-" * 130)

    for r in races:
        res = r["shared_resource"]
        print(fmt.format(
            r["race_id"],
            _truncate(r["thread_a"]["function"], 28),
            _truncate(r["thread_b"]["function"], 28),
            _truncate(res["name"], 30),
            res["access_a"][:6],
            res["access_b"][:6],
            r["severity"][:8],
            r["protection"][:10] if r["protection"] != "none" else "none",
        ))

    print()
    print("Thread contexts:")
    shown = set()
    for r in races:
        for side in ("thread_a", "thread_b"):
            key = (r[side]["function"], r[side]["thread_model"],
                   r[side]["thread_entry"])
            if key not in shown:
                shown.add(key)
                print(f"  {r[side]['function']}: "
                      f"model={r[side]['thread_model']}, "
                      f"entry={r[side]['thread_entry']}")


def _truncate(s, maxlen):
    """Truncate string to maxlen with ellipsis."""
    if len(s) <= maxlen:
        return s
    return s[:maxlen - 3] + "..."


# ---------------------------------------------------------------------------
# Concurrency safety analysis: can two call chains execute concurrently?
# ---------------------------------------------------------------------------

def _collect_chain_resources(G, chain_nodes):
    """Collect all shared resources accessed by a set of chain nodes.

    Returns:
        dict mapping (resource_type, resource_name) -> access_type
        where access_type is "read", "write", or "read_write"
    """
    resources = {}
    for nid in chain_nodes:
        if nid not in G:
            continue
        ndata = G.nodes[nid]
        if ndata.get("is_empty", False):
            continue

        # Global variables
        for g in ndata.get("globals_read", []):
            gname = g.get("name", "") if isinstance(g, dict) else str(g)
            if gname:
                key = ("global_var", gname)
                current = resources.get(key, "read")
                resources[key] = "read_write" if current == "write" else "read"

        for g in ndata.get("globals_written", []):
            gname = g.get("name", "") if isinstance(g, dict) else str(g)
            if gname:
                key = ("global_var", gname)
                current = resources.get(key, "write")
                resources[key] = "read_write" if current == "read" else "write"

        # Struct fields
        for f in ndata.get("fields_read", []):
            if isinstance(f, dict):
                sc = f.get("struct_chain", "")
                fn = f.get("field_name", "")
                if sc and fn:
                    key = ("struct_field", f"{sc}.{fn}")
                    current = resources.get(key, "read")
                    resources[key] = "read_write" if current == "write" else "read"

        for f in ndata.get("fields_written", []):
            if isinstance(f, dict):
                sc = f.get("struct_chain", "")
                fn = f.get("field_name", "")
                if sc and fn:
                    key = ("struct_field", f"{sc}.{fn}")
                    current = resources.get(key, "write")
                    resources[key] = "read_write" if current == "read" else "write"

    return resources


def _collect_chain_locks(G, chain_nodes, profile=None):
    """Collect all lock names held across chain nodes.

    Args:
        profile: Builder config dict for lock detection (profile-driven).

    Returns:
        set of lock variable names
    """
    locks = set()
    for nid in chain_nodes:
        if nid not in G:
            continue
        ndata = G.nodes[nid]
        if ndata.get("is_empty", False):
            continue
        # Pass G/nid for lazy body_text fetch from SQLite (low-memory builds).
        locks |= _detect_locks_held(ndata, profile, G=G, nid=nid)
    return locks


def _collect_thread_contexts(G, chain_nodes):
    """Collect thread context information for chain nodes.

    Returns:
        list of dicts with function, thread_model, thread_entry
    """
    contexts = []
    seen = set()
    for nid in chain_nodes:
        if nid not in G:
            continue
        ndata = G.nodes[nid]
        if ndata.get("is_empty", False):
            continue
        model, entry = _get_thread_context(ndata)
        key = (ndata.get("name", ""), model, entry)
        if key not in seen:
            seen.add(key)
            contexts.append({
                "function": ndata.get("name", nid),
                "thread_model": model or "unknown",
                "thread_entry": entry or "unknown",
            })
    return contexts


def _chains_in_different_contexts(G, chain1_nodes, chain2_nodes):
    """Check if any node from chain1 and any node from chain2 are in different
    thread contexts.

    Returns True if there exists at least one pair of nodes (one from each chain)
    that are in different thread contexts.
    """
    for n1 in chain1_nodes:
        if n1 not in G:
            continue
        ndata1 = G.nodes[n1]
        if ndata1.get("is_empty", False):
            continue
        for n2 in chain2_nodes:
            if n2 not in G:
                continue
            ndata2 = G.nodes[n2]
            if ndata2.get("is_empty", False):
                continue
            if not _same_thread_context(ndata1, ndata2):
                return True
    return False


def _find_concurrent_peers(G, target_nid):
    """Find all functions in different thread contexts that share state
    with the target function.

    Returns:
        list of node IDs that are in a different thread context and
        share at least one global variable or struct field with target.
    """
    ndata_target = G.nodes[target_nid]
    target_resources = _collect_chain_resources(G, [target_nid])
    if not target_resources:
        return []

    # Build resource -> node IDs index for the whole graph (excluding target)
    resource_to_nodes = defaultdict(set)
    for nid, ndata in G.nodes(data=True):
        if nid == target_nid or ndata.get("is_empty", False):
            continue
        for g in ndata.get("globals_read", []):
            gname = g.get("name", "") if isinstance(g, dict) else str(g)
            if gname:
                resource_to_nodes[("global_var", gname)].add(nid)
        for g in ndata.get("globals_written", []):
            gname = g.get("name", "") if isinstance(g, dict) else str(g)
            if gname:
                resource_to_nodes[("global_var", gname)].add(nid)
        for f in ndata.get("fields_read", []):
            if isinstance(f, dict):
                sc = f.get("struct_chain", "")
                fn = f.get("field_name", "")
                if sc and fn:
                    resource_to_nodes[("struct_field", f"{sc}.{fn}")].add(nid)
        for f in ndata.get("fields_written", []):
            if isinstance(f, dict):
                sc = f.get("struct_chain", "")
                fn = f.get("field_name", "")
                if sc and fn:
                    resource_to_nodes[("struct_field", f"{sc}.{fn}")].add(nid)

    # Find nodes in different thread contexts that share resources with target
    peers = set()
    for rkey in target_resources:
        for nid in resource_to_nodes.get(rkey, set()):
            ndata = G.nodes[nid]
            if not _same_thread_context(ndata_target, ndata):
                peers.add(nid)

    return list(peers)


def concurrency_analyze(G, chain1_nodes, chain2_nodes=None, func_name=None,
                        profile=None):
    """Analyze whether two call chains can safely execute concurrently.

    If func_name is provided instead of chain2_nodes: find the function, then
    find all functions in different thread contexts that share state with it.

    If both chains provided: analyze if the two chains can safely execute
    concurrently.

    Args:
        G: networkx DiGraph (the full invocation graph with E2/E4 attributes).
        chain1_nodes: list of node IDs forming the first call chain.
        chain2_nodes: list of node IDs forming the second call chain (optional).
        func_name: function name to analyze (alternative to chain2_nodes;
                   finds the function and all concurrent peers).
        profile: Builder config dict (from ProfileSchema.to_builder_config())
                 for lock detection. When None, lock detection is disabled.

    Returns:
        dict with keys: safe, thread_contexts, shared_resources, risks
    """
    # Resolve func_name if provided
    if func_name is not None:
        target_nid = _find_node_id(G, func_name)
        if not target_nid:
            return {
                "safe": True,
                "thread_contexts": [],
                "shared_resources": [],
                "risks": [{"type": "info", "description": f"Function '{func_name}' not found in graph", "shared_resource": None, "thread_a": None, "thread_b": None}],
            }
        chain1_nodes = [target_nid]
        # Find concurrent peers automatically
        peer_nids = _find_concurrent_peers(G, target_nid)
        chain2_nodes = peer_nids

    # Ensure chain1_nodes is a list
    if chain1_nodes is None:
        chain1_nodes = []

    # Validate chains
    valid_chain1 = [n for n in chain1_nodes if n in G]
    valid_chain2 = [n for n in chain2_nodes if n in G] if chain2_nodes else []

    # Step a: Thread context check
    different_contexts = _chains_in_different_contexts(G, valid_chain1, valid_chain2)

    # Step b: Shared state detection
    resources1 = _collect_chain_resources(G, valid_chain1)
    resources2 = _collect_chain_resources(G, valid_chain2)

    # Find shared resources
    shared_keys = set(resources1.keys()) & set(resources2.keys())

    shared_resources = []
    for rtype, rname in sorted(shared_keys):
        access1 = resources1.get((rtype, rname), "read")
        access2 = resources2.get((rtype, rname), "read")
        # Determine protection
        locks1 = _collect_chain_locks(G, valid_chain1, profile)
        locks2 = _collect_chain_locks(G, valid_chain2, profile)
        common_locks = locks1 & locks2
        protected_by = sorted(common_locks)[0] if common_locks else None
        shared_resources.append({
            "resource": rname if rtype == "global_var" else rname,
            "type": rtype,
            "access_chain1": access1,
            "access_chain2": access2,
            "protected_by": protected_by,
        })

    # Step c: Lock protection check
    locks1 = _collect_chain_locks(G, valid_chain1, profile)
    locks2 = _collect_chain_locks(G, valid_chain2, profile)

    # Step d: Race window detection -- build risks
    risks = []

    if not different_contexts:
        # Same thread context -- no concurrency risk
        pass
    elif not shared_keys:
        # Different threads but no shared state -- no data race
        pass
    else:
        # There are shared resources accessed from different thread contexts
        for sr in shared_resources:
            if sr["protected_by"]:
                # Protected by a common lock -- likely safe for this resource
                continue

            # Determine risk type
            access1 = sr["access_chain1"]
            access2 = sr["access_chain2"]

            if access1 == "write" or access2 == "write":
                # At least one write with no common lock = data race
                risk_type = "data_race"
            elif access1 == "read_write" or access2 == "read_write":
                risk_type = "data_race"
            else:
                # Both read -- no data race, but still a concurrent access
                # Could indicate an atomicity violation if the reads are
                # part of a check-then-act pattern, but we flag as lower
                # severity under atomic_violation
                risk_type = "atomic_violation"

            # Determine which functions are involved
            # Find one function from each chain that accesses this resource
            func_a = None
            func_b = None
            rkey = (sr["type"], sr["resource"])
            for nid in valid_chain1:
                if nid not in G:
                    continue
                ndata = G.nodes[nid]
                # Check if this node accesses the resource
                node_res = _collect_chain_resources(G, [nid])
                if rkey in node_res:
                    func_a = ndata.get("name", nid)
                    break

            for nid in valid_chain2:
                if nid not in G:
                    continue
                ndata = G.nodes[nid]
                node_res = _collect_chain_resources(G, [nid])
                if rkey in node_res:
                    func_b = ndata.get("name", nid)
                    break

            risks.append({
                "type": risk_type,
                "description": (
                    f"Concurrent {access1}/{access2} on {sr['resource']} "
                    f"between {func_a} and {func_b} without common lock"
                ),
                "shared_resource": sr["resource"],
                "thread_a": func_a,
                "thread_b": func_b,
            })

        # Deadlock risk: if both chains hold different locks and access
        # resources protected by the other chain's locks
        if locks1 and locks2 and not (locks1 & locks2):
            # Each chain holds at least one lock that the other doesn't hold.
            # This is a potential deadlock if there's a lock ordering issue.
            # We check if chain1 accesses a resource protected by a lock held
            # only in chain2, and vice versa.
            chain1_protected_resources = set()
            for rkey, access in resources1.items():
                if rkey in shared_keys:
                    # Check if any chain2 lock protects this resource
                    # in another function
                    pass  # Already checked above

            # Simpler heuristic: if both chains hold different locks and
            # access shared state, flag a potential deadlock risk
            risks.append({
                "type": "deadlock_risk",
                "description": (
                    f"Potential deadlock: chain1 holds {sorted(locks1)} "
                    f"while chain2 holds {sorted(locks2)}, "
                    f"both access shared state"
                ),
                "shared_resource": ", ".join(
                    sr["resource"] for sr in shared_resources),
                "thread_a": valid_chain1[0] if valid_chain1 else "unknown",
                "thread_b": valid_chain2[0] if valid_chain2 else "unknown",
            })

    # Collect thread contexts for output
    thread_contexts = _collect_thread_contexts(G, valid_chain1 + valid_chain2)

    # Determine overall safety
    has_unprotected_races = any(
        r["type"] in ("data_race", "atomic_violation", "deadlock_risk")
        for r in risks
    )
    safe = not has_unprotected_races

    return {
        "safe": safe,
        "thread_contexts": thread_contexts,
        "shared_resources": shared_resources,
        "risks": risks,
    }


# ---------------------------------------------------------------------------
# CLI command: concurrency-analyze
# ---------------------------------------------------------------------------

def cmd_concurrency_analyze(args):
    """CLI handler for the concurrency-analyze subcommand."""
    G = _load_full_graph(args.graph)
    json_mode = getattr(args, "json", False)

    chain1_name = getattr(args, "chain1", None)
    chain2_name = getattr(args, "chain2", None)
    func_name = getattr(args, "func", None)
    # Accept --from as alias for --func/--chain1; --to as alias for --chain2
    from_node = getattr(args, "from_node", None)
    to_node = getattr(args, "to_node", None)
    if from_node and not func_name and not chain1_name:
        func_name = from_node
    if to_node and not chain2_name:
        chain2_name = to_node

    # Load profile from graph dir (persisted by build) or --profile arg.
    # Profile provides project-specific lock APIs for accurate concurrency
    # protection detection.
    profile = None
    profile_arg = getattr(args, "profile", None)
    if profile_arg:
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
            from _profile import ProfileSchema
            profile = ProfileSchema.load(profile_arg).to_builder_config()
        except Exception as _e:
            print(f"[concurrency-analyze] Warning: failed to load --profile {profile_arg}: {_e}",
                  file=sys.stderr)
    if profile is None:
        profile = _load_profile_from_graph_dir(args.graph)

    # --func is an alternative to --chain1
    if func_name and not chain1_name:
        chain1_name = func_name

    if not chain1_name:
        print("Error: --chain1 or --func is required", file=sys.stderr)
        sys.exit(1)

    # Resolve chain1 node
    chain1_nid = _find_node_id(G, chain1_name)
    if not chain1_nid:
        print(f"Error: Function '{chain1_name}' not found in graph",
              file=sys.stderr)
        sys.exit(1)

    # Resolve chain2 nodes if provided (can be a function name)
    chain2_nids = None
    if chain2_name:
        chain2_nid = _find_node_id(G, chain2_name)
        if not chain2_nid:
            print(f"Error: Function '{chain2_name}' not found in graph",
                  file=sys.stderr)
            sys.exit(1)
        chain2_nids = [chain2_nid]

    # If func_name is provided without chain2, find concurrent peers
    if func_name and not chain2_name:
        result = concurrency_analyze(G, [chain1_nid], func_name=func_name,
                                     profile=profile)
    else:
        result = concurrency_analyze(G, [chain1_nid], chain2_nids,
                                     profile=profile)

    if json_mode:
        _output_result(result, json_mode=True)
    else:
        _print_concurrency_result(result)


def _print_concurrency_result(result):
    """Print concurrency analysis result in human-readable format."""
    if result["safe"]:
        print("Concurrency Safety: SAFE")
    else:
        print("Concurrency Safety: UNSAFE -- potential races detected")

    print()
    print("Thread Contexts:")
    for tc in result["thread_contexts"]:
        print(f"  {tc['function']}: model={tc['thread_model']}, "
              f"entry={tc['thread_entry']}")

    if result["shared_resources"]:
        print()
        print("Shared Resources:")
        fmt = "{:<30} {:<14} {:<14} {:<14} {}"
        print(fmt.format("Resource", "Type", "Access(C1)", "Access(C2)", "Protected By"))
        print("-" * 90)
        for sr in result["shared_resources"]:
            print(fmt.format(
                _truncate(sr["resource"], 30),
                sr["type"][:14],
                sr["access_chain1"][:14],
                sr["access_chain2"][:14],
                sr["protected_by"] or "none",
            ))

    if result["risks"]:
        print()
        print("Risks:")
        for risk in result["risks"]:
            rtype = risk["type"]
            if rtype == "data_race":
                label = "DATA RACE"
            elif rtype == "atomic_violation":
                label = "ATOMIC VIOLATION"
            elif rtype == "deadlock_risk":
                label = "DEADLOCK RISK"
            else:
                label = rtype.upper()
            print(f"  [{label}] {risk['description']}")
            if risk.get("thread_a"):
                print(f"    Thread A: {risk['thread_a']}")
            if risk.get("thread_b"):
                print(f"    Thread B: {risk['thread_b']}")

    # #11 fix: Annotate analysis limitations so users don't over-trust results
    print("\n" + "=" * 70)
    print("ANALYSIS LIMITATIONS (please read before drawing conclusions):")
    print("  - Lock protection is function-level (not access-site-level).")
    print("    A function may access a field both inside and outside a lock hold,")
    print("    but this analysis cannot distinguish the two cases.")
    print("  - TOCTOU (time-of-check time-of-use) races are NOT detected.")
    print("    Check-then-act patterns within a single function are not analyzed.")
    print("  - Lock detection uses regex on body_text (not CFG-aware).")
    print("    False positives (lock released before access) and false negatives")
    print("    (different variable name for same lock) may occur.")
    print("  - Deadlock detection is heuristic (no lock-order graph analysis).")
    print("  - Use lock-coverage for access-site-level analysis.")
    print("  - Use happens-before for memory-ordering-aware analysis.")
    print("=" * 70)
