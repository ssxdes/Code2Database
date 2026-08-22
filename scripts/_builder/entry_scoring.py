"""callgraph builder module: entry_scoring."""

import os
import json
import sys
import re
from pathlib import Path
from collections import defaultdict
import networkx as nx
from collections import deque
from _detector.framework_detector import get_entry_multiplier, FrameworkHint, detect_frameworks_for_project
from _builder.utils import _output_result, _print_structured, _find_node_id, _parse_bindings, _load_globals, _is_condition_alive


def _calculate_entry_point_score(name: str, is_api_entry: bool,
                                 caller_count: int, callee_count: int,
                                 frameworks: list = None) -> float:
    """Multi-factor entry point scoring.

    Score = baseScore * exportMultiplier * nameMultiplier * frameworkMultiplier

    Multi-factor scoring model:
    - Base score: callee_count / (caller_count + 1) — functions that call many but
      are called by few are likely orchestration entry points
    - Export multiplier: 2.0 for API_entry, 1.0 otherwise
    - Name patterns: entry-pattern bonus (handle*, on*, main, init, etc.)
    - Utility penalty: 0.3 for get/set/is/has patterns
    - Framework multiplier: from framework detection
    """
    # Base score: outgoing/incoming ratio
    base_score = callee_count / (caller_count + 1)

    # Export multiplier
    export_mult = 2.0 if is_api_entry else 1.0

    # Name pattern multipliers
    name_lower = name.lower()
    entry_patterns = (r'^main$', r'^run$', r'^start$', r'^execute$',
                      r'^handle_', r'^on_', r'^process_',
                      r'^do_', r'^perform_', r'^dispatch_')
    utility_patterns = (r'^get_', r'^set_', r'^is_', r'^has_',
                        r'^check_', r'^validate_',
                        r'^test_', r'^mock_', r'^stub_')

    name_mult = 1.0
    if any(re.match(p, name_lower) for p in entry_patterns):
        name_mult = 1.5
    elif any(re.match(p, name_lower) for p in utility_patterns):
        name_mult = 0.3

    test_patterns = (r'^test_', r'^mock_', r'^stub_', r'^bench_', r'^spec_')
    if any(re.match(p, name_lower) for p in test_patterns):
        name_mult = 0.1

    # Framework multiplier
    framework_mult = 1.0
    if frameworks:
        framework_mult = get_entry_multiplier(name, frameworks)

    return round(base_score * export_mult * name_mult * framework_mult, 3)


def _score_entry_points_lightweight(id_registry: dict,
                                     profile: dict = None) -> dict:
    """Lightweight entry point scoring using id_registry only (no NetworkX).

    Returns dict of node_id → (node_attrs, score).
    Uses a simplified scoring that only considers name patterns and API labels,
    without caller/callee counts (which require the graph).
    """
    scores = {}

    for nid, nd in id_registry.items():
        if nd.get("is_empty", False):
            continue
        name = nd.get("name", "")
        domain = nd.get("domain", "")
        source = nd.get("source_file", "")

        # Skip test functions
        _test_patterns = (
            r'^test_', r'^testcase_', r'^spec_', r'^bench_', r'^benchmark_',
            r'^mock_', r'^stub_', r'^fake_',
        )
        _name_lower = name.lower()
        if any(re.match(p, _name_lower) or re.search(p, _name_lower) for p in _test_patterns):
            continue
        # Skip external/unresolved nodes
        if domain == "external":
            continue
        if not source:
            continue

        is_api = "API_entry" in nd.get("labels", [])
        # Simple scoring: API entry points get high scores,
        # name patterns get moderate scores, everything else gets 1.0
        if is_api:
            score = 5.0
        else:
            entry_patterns = (r'^main$', r'^run$', r'^start$', r'^execute$',
                              r'^handle_', r'^on_', r'^process_',
                              r'^do_', r'^perform_', r'^dispatch_')
            utility_patterns = (r'^get_', r'^set_', r'^is_', r'^has_',
                                r'^check_', r'^validate_')
            if any(re.match(p, _name_lower) for p in entry_patterns):
                score = 2.0
            elif any(re.match(p, _name_lower) for p in utility_patterns):
                score = 0.3
            else:
                score = 1.0

        if score > 0:
            scores[nid] = (nd, score)

    return scores




def _detect_processes(G, entry_scores: dict, comm_result=None) -> list:
    """BFS trace from scored entry points through INVOKES edges.

    Groups similar paths, labels with heuristic names.
    Adds cross-community tracking.

    Returns list of process dicts.
    """
    _ARCHETYPE_KEYWORDS = {
        'init': ['init', 'start', 'construct', 'create', 'open', 'launch', 'boot'],
        'io': ['read', 'write', 'submit', 'process', 'send', 'receive', 'transfer', 'execute'],
        'shutdown': ['fini', 'destruct', 'close', 'cleanup', 'destroy', 'release', 'free', 'abort'],
        'config': ['config', 'json', 'rpc', 'setup', 'parse', 'option', 'param'],
    }

    def _classify_archetype(name, source_file=""):
        name_lower = name.lower()
        for archetype, keywords in _ARCHETYPE_KEYWORDS.items():
            for kw in keywords:
                if kw in name_lower:
                    return archetype
        return 'other'

    processes = []
    # Get top entry points by score
    if not entry_scores:
        # Fallback: use API_entry nodes
        entry_scores = {nid: 1.0 for nid, nd in G.nodes(data=True)
                       if "API_entry" in nd.get("labels", [])}

    # Classify entry points by archetype for diverse chain selection
    MAX_PER_ARCHETYPE = 5
    archetype_groups = defaultdict(list)
    for entry_id, score in entry_scores.items():
        nd = G.nodes[entry_id]
        name = nd.get("name", "")
        source = nd.get("source_file", "")
        archetype = _classify_archetype(name, source)
        archetype_groups[archetype].append((entry_id, score))

    # Sort each group by score descending, take top N
    selected_entries = []
    for archetype in ('init', 'io', 'shutdown', 'config', 'other'):
        group = sorted(archetype_groups.get(archetype, []), key=lambda x: x[1], reverse=True)
        selected_entries.extend(group[:MAX_PER_ARCHETYPE])

    # If we still have room, add more from highest-scoring remaining entries
    if len(selected_entries) < 20:
        seen_ids = {e[0] for e in selected_entries}
        remaining = [(eid, sc) for eid, sc in sorted(entry_scores.items(), key=lambda x: x[1], reverse=True)
                     if eid not in seen_ids]
        selected_entries.extend(remaining[:20 - len(selected_entries)])

    seen_paths = set()  # Deduplicate similar paths

    for entry_id, score in selected_entries:
        if entry_id not in G:
            continue

        # BFS trace
        visited = {entry_id}
        path = [entry_id]
        queue = deque([entry_id])
        communities_crossed = set()

        entry_comm = comm_result.node_community.get(entry_id, "") if comm_result else ""
        if entry_comm:
            communities_crossed.add(entry_comm)

        while queue and len(path) < 30:
            current = queue.popleft()
            for succ in G.successors(current):
                if succ in visited:
                    continue
                if G.nodes[succ].get("is_empty", False):
                    continue
                if "dead_code" in G.nodes[succ].get("labels", []):
                    continue
                # Skip spawn_target edges (they represent parallel threads, not sequential flow)
                ed = G.get_edge_data(current, succ) or {}
                if ed.get("concurrency") in ("spawn_target", "callback"):
                    continue
                # Skip non-call edges (CONTAINS, IMPORTS)
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue

                visited.add(succ)
                path.append(succ)
                queue.append(succ)

                if comm_result:
                    succ_comm = comm_result.node_community.get(succ, "")
                    if succ_comm and succ_comm != entry_comm:
                        communities_crossed.add(succ_comm)

        if len(path) < 2:
            continue

        # Create path signature for deduplication (archetype-aware)
        nd_entry = G.nodes[entry_id]
        archetype = _classify_archetype(nd_entry.get("name", ""), nd_entry.get("source_file", ""))
        path_sig = f"{archetype}:{'→'.join(G.nodes[nid].get('name', nid) for nid in path[:5])}"
        if path_sig in seen_paths:
            continue
        seen_paths.add(path_sig)

        # Generate process label
        entry_name = G.nodes[entry_id].get("name", "")
        label = _generate_process_label(entry_name, path, G)

        processes.append({
            "entry_point": entry_id,
            "entry_name": entry_name,
            "entry_score": score,
            "label": label,
            "step_count": len(path),
            "steps": [G.nodes[nid].get("name", nid) for nid in path],
            "step_ids": path,
            "communities_crossed": len(communities_crossed),
        })

    return processes




def _generate_process_label(entry_name: str, path: list, G) -> str:
    """Generate a human-readable label for an execution process."""
    if not path or len(path) < 2:
        return entry_name

    # Use entry→significant_intermediate→endpoint pattern
    names = [G.nodes[nid].get("name", nid) for nid in path]
    if len(names) <= 3:
        return " → ".join(names)

    # Abbreviate: entry → ... → last
    return f"{names[0]} → ... → {names[-1]}"




def _score_entry_points(G, source_root: str = "") -> dict:
    """Score all nodes as potential entry points.

    Returns dict of node_id → score.
    """
    from collections import Counter
    frameworks = detect_frameworks_for_project(source_root) if source_root else []
    scores = {}

    # Precompute call-only in/out degrees via a single edge traversal,
    # replacing the per-node predecessors()/successors() traversal that
    # was O(V × avg_degree) with O(E + V).
    call_in_deg = Counter()
    call_out_deg = Counter()
    for u, v, edata in G.edges(data=True):
        if edata.get("relation", "") in ("CONTAINS", "IMPORTS"):
            continue
        call_in_deg[v] += 1
        if not G.nodes[v].get("is_empty", False):
            call_out_deg[u] += 1

    for nid, nd in G.nodes(data=True):
        if nd.get("is_empty", False):
            continue
        if "dead_code" in nd.get("labels", []):
            continue
        _test_patterns = (
            r'^test_', r'^testcase_', r'^spec_', r'^bench_', r'^benchmark_',
            r'^mock_', r'^stub_', r'^fake_',
            r'^generateTest', r'^generate_test',
            r'^main$',
        )
        _name_lower = nd.get("name", "").lower()
        _source = nd.get("source_file", "")
        if any(re.match(p, _name_lower) or re.search(p, _name_lower) for p in _test_patterns):
            if not (_name_lower == "main" and "/app/" in _source):
                continue
        if nd.get("domain", "") == "external":
            continue
        if not nd.get("source_file", ""):
            continue
        is_api = "API_entry" in nd.get("labels", [])
        if "file" in nd.get("labels", []):
            continue
        caller_count = call_in_deg.get(nid, 0)
        real_callee_count = call_out_deg.get(nid, 0)
        score = _calculate_entry_point_score(
            nd.get("name", ""), is_api, caller_count, real_callee_count,
            frameworks)
        if score > 0:
            scores[nid] = score

    for nid, score in scores.items():
        G.nodes[nid]["entry_score"] = score

    return scores


