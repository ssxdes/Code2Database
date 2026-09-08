"""callgraph builder module: thread_models — split from graph_build.py."""

import logging
import os
import json
import sys
import re
import time
from pathlib import Path
from collections import defaultdict, Counter
import networkx as nx
from _builder.graph.streaming_graph import StreamingGraph
from _builder.utils import _resolve_invoked_id
import _builder.utils as _utils


def _detect_thread_models(G: nx.DiGraph, builder_profile: dict = None,
                           jobs: int = 0, max_workers: int = 0,
                           parallel_mode: str = "thread",
                           explicit_parallel_mode: bool = False) -> dict:
    """Detect threading models used by functions in the graph.

    Scans all nodes for threading API usage in body_text and callee_args,
    returning a dict of {node_id: thread_model} for functions that directly
    use threading APIs.

    Recognized models:
    - pthread: POSIX threads (pthread_create, pthread_mutex_lock, etc.)
    - goroutine: Go goroutines (go keyword in .go files)
    - Project-specific models from profile (e.g., reactor for event-driven,
      kernel_thread for Linux kernel)
    """
    # Universal threading API patterns (POSIX only).
    # Project-specific patterns come from profile's threading_models field.
    _THREAD_PATTERNS = [
        # pthread model (universal)
        (r'\bpthread_create\b', 'pthread'),
        (r'\bpthread_mutex_lock\b', 'pthread'),
        (r'\bpthread_mutex_unlock\b', 'pthread'),
        (r'\bpthread_join\b', 'pthread'),
        (r'\bpthread_cond_wait\b', 'pthread'),
        (r'\bpthread_cond_signal\b', 'pthread'),
        (r'\bpthread_rwlock_rdlock\b', 'pthread'),
        (r'\bpthread_rwlock_wrlock\b', 'pthread'),
    ]

    # Load project-specific threading models from profile
    if builder_profile:
        for model_name, patterns in builder_profile.get("threading_models", {}).items():
            for pat_entry in patterns:
                pattern = pat_entry.get("pattern", "")
                if pattern:
                    _THREAD_PATTERNS.append((pattern, model_name))

    # Compile patterns for efficiency
    _compiled = [(re.compile(pat), model) for pat, model in _THREAD_PATTERNS]

    # Go goroutine pattern (only for .go files)
    _GOROUTINE_RE = re.compile(r'\bgo\s+\w+')
    _GO_EXT = '.go'

    thread_models = {}

    # Build candidate list once (skip empty + file nodes)
    _thread_candidates = [(nid, ndata) for nid, ndata in G.nodes(data=True)
                          if not ndata.get("is_empty", False)
                          and ndata.get("node_type") != "file"]

    def _check_thread_model(nid, ndata):
        """Per-node check: returns (nid, model) or (nid, None)."""
        body = ndata.get("body_text", "")
        source_file = ndata.get("source_file", "")
        detected_model = None
        if body:
            for compiled_pat, model in _compiled:
                if compiled_pat.search(body):
                    detected_model = model
                    break
            if not detected_model and source_file.endswith(_GO_EXT):
                if _GOROUTINE_RE.search(body):
                    detected_model = 'goroutine'
        if not detected_model:
            for ca in ndata.get("callee_args", []):
                callee_name = ca.get("callee", "")
                for compiled_pat, model in _compiled:
                    if compiled_pat.search(callee_name):
                        detected_model = model
                        break
                if detected_model:
                    break
        return (nid, detected_model) if detected_model else (nid, None)

    # Parallel: re.search is a C extension that releases the GIL during
    # matching, so ThreadPoolExecutor gives real speedup even for this
    # pure-regex workload. On kernel: 1.5M nodes × 20 patterns = 30M
    # re.search calls (~15 min serial → ~2-3 min with 8+ threads).
    try:
        from _builder.build.parallel import map_nodes
        _thread_results = map_nodes(
            _thread_candidates, _check_thread_model,
            jobs=jobs,
            max_workers_cap=max_workers,
            parallel_mode=parallel_mode,
            explicit_parallel_mode=explicit_parallel_mode,
            desc="thread_model_detection",
        )
        for nid, model in _thread_results:
            if model:
                thread_models[nid] = model
    except ImportError:
        # Fallback: serial loop
        for nid, ndata in _thread_candidates:
            _nid, _model = _check_thread_model(nid, ndata)
            if _model:
                thread_models[_nid] = _model

    return thread_models



def _propagate_thread_models(G: nx.DiGraph, thread_models: dict) -> int:
    """Propagate thread model information along call chains.

    BFS from each thread entry point (function that directly uses threading APIs).
    Called functions inherit the model via thread_model_inherited attribute.
    Propagation stops at function boundaries where new threads are created
    (i.e., functions that already have their own thread_model.

    Args:
        G: The invocation graph.
        thread_models: Dict of {node_id: thread_model} from _detect_thread_models.

    Returns:
        Count of unique nodes that received thread_model_inherited.
    """
    from collections import deque

    # Set thread_model on direct users. thread_entry_id records the
    # entry's own node id so downstream analysis can key thread contexts
    # by node identity instead of function NAME (same-named static
    # thread routines in different files are DIFFERENT threads).
    for nid, model in thread_models.items():
        if nid in G:
            G.nodes[nid]["thread_model"] = model
            G.nodes[nid]["thread_entry"] = True
            G.nodes[nid]["thread_entry_id"] = nid

    # BFS propagation from each thread entry point
    _inherited_set = set()
    for entry_id, model in thread_models.items():
        if entry_id not in G:
            continue
        visited = {entry_id}
        queue = deque()

        # Seed queue with direct callees of the entry point
        for succ in G.successors(entry_id):
            ed = G.get_edge_data(entry_id, succ) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            if succ not in visited:
                queue.append(succ)
                visited.add(succ)

        while queue:
            current = queue.popleft()
            if current not in G:
                continue
            current_ndata = G.nodes[current]

            # Stop propagation at functions that create their own threads
            # (they start a new thread context)
            if current_ndata.get("thread_model") is not None:
                continue

            # Mark with inherited model + the entry point's node id.
            # Without thread_entry_inherited, every callee of ANY entry
            # with the same model landed in the same (model, None)
            # context bucket — callees of two different pthread routines
            # were judged 'same thread' and their races were missed.
            current_ndata["thread_model_inherited"] = model
            current_ndata["thread_entry_inherited"] = entry_id
            _inherited_set.add(current)

            # Continue propagation to callees
            for succ in G.successors(current):
                ed = G.get_edge_data(current, succ) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                if succ not in visited:
                    visited.add(succ)
                    queue.append(succ)

    return len(_inherited_set)



def _validate_stats_consistency(G: nx.DiGraph, pipeline_node_count: int) -> dict:
    """Validate consistency between pipeline stats node count and context-pack function count.

    The context pack excludes nodes that are empty, file-typed, or auto_created,
    which leads to a different (lower) function count than the raw pipeline total.
    This function computes both counts, warns if they diverge by more than 1%,
    and returns a reconciliation dict for inclusion in pipeline stats.

    Args:
        G: The invocation graph DiGraph.
        pipeline_node_count: The raw node count (G.number_of_nodes()) recorded
            in the pipeline stats.

    Returns:
        A dict with keys: context_pack_count, pipeline_count, delta,
        delta_pct, exceeds_threshold, explanation.
    """
    # Count using the same logic as _build_context_pack (index_pack.py):
    # exclude is_empty, node_type=="file", auto_created
    context_pack_count = 0
    excluded = {"empty": 0, "file": 0, "auto_created": 0, "dead_code": 0}
    for _, nd in G.nodes(data=True):
        if nd.get("is_empty", False):
            excluded["empty"] += 1
            continue
        if nd.get("node_type") == "file":
            excluded["file"] += 1
            continue
        if nd.get("auto_created", False):
            excluded["auto_created"] += 1
            continue
        # dead_code is tracked but NOT excluded from the context-pack count;
        # it is however excluded from some other outputs, so we record it.
        if "dead_code" in nd.get("labels", []):
            excluded["dead_code"] += 1
        context_pack_count += 1

    delta = pipeline_node_count - context_pack_count
    delta_pct = (delta / pipeline_node_count * 100) if pipeline_node_count > 0 else 0.0
    exceeds_threshold = abs(delta_pct) > 1.0

    explanation = (
        f"Pipeline count ({pipeline_node_count}) includes all graph nodes. "
        f"Context-pack count ({context_pack_count}) excludes "
        f"{excluded['empty']} empty, "
        f"{excluded['file']} file-type, and "
        f"{excluded['auto_created']} auto_created nodes "
        f"(delta={delta}, {delta_pct:.1f}%). "
        f"Additionally {excluded['dead_code']} dead_code nodes are present but "
        f"included in the context-pack count."
    )

    if exceeds_threshold:
        print(f"WARNING: Stats consistency check — {explanation}", file=sys.stderr)

    return {
        "context_pack_count": context_pack_count,
        "pipeline_count": pipeline_node_count,
        "delta": delta,
        "delta_pct": round(delta_pct, 2),
        "exceeds_1pct_threshold": exceeds_threshold,
        "excluded": excluded,
        "explanation": explanation,
    }



def _compile_dispatch_patterns(inline_wrapper_patterns_cfg: list,
                                macro_bridge_patterns_cfg: list):
    """Pre-compile inline wrapper (O5) and macro bridge (O6) regex patterns.

    Extracted from build_graph() so the compilation logic can be unit-tested
    in isolation. Invalid patterns are skipped with a stderr warning rather
    than aborting the build (a single bad pattern should not poison the
    entire dispatch pipeline).

    Args:
        inline_wrapper_patterns_cfg: List of regex pattern strings for
            inline-wrapper detection (e.g. ``[r"^(?:__)?(?:call|invoke)_(\\w+)$"]``).
        macro_bridge_patterns_cfg: List of dicts ``{"pattern": str, "impl": str}``
            for macro-bridge detection. Entries missing pattern or impl are skipped.

    Returns:
        Tuple ``(inline_wrapper_regexes, macro_bridge_compiled)``:
        - ``inline_wrapper_regexes``: list of compiled ``re.Pattern`` objects
        - ``macro_bridge_compiled``: list of ``(compiled_pattern, impl_string)`` tuples
    """
    inline_wrapper_regexes = []
    for _pat in inline_wrapper_patterns_cfg:
        try:
            inline_wrapper_regexes.append(re.compile(_pat))
        except re.error as _e:
            print(f"[build] Warning: invalid inline_wrapper_pattern {_pat!r}: {_e}",
                  file=sys.stderr)

    macro_bridge_compiled = []
    for _mb in macro_bridge_patterns_cfg:
        _pat = _mb.get("pattern") if isinstance(_mb, dict) else None
        _impl = _mb.get("impl") if isinstance(_mb, dict) else None
        if not _pat or not _impl:
            continue
        try:
            macro_bridge_compiled.append((re.compile(_pat), _impl))
        except re.error as _e:
            print(f"[build] Warning: invalid macro_bridge_pattern {_pat!r}: {_e}",
                  file=sys.stderr)

    return inline_wrapper_regexes, macro_bridge_compiled


