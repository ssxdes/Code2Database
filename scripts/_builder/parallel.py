"""Parallel execution helpers for build/scan hot loops.

This module centralizes the choice between sequential and parallel execution
so callers don't re-implement the same boilerplate. Two execution modes are
supported:

* ``ThreadPoolExecutor`` (default for in-process parallelism) — works with
  shared graph state, no pickling overhead. Tree-sitter and ``re`` release
  the GIL, so regex-heavy per-node work (state_access extraction, heuristic
  enhance) actually parallelizes.

  **GIL caveat (Phase J audit, 2026-08-27)**: Python's tree-sitter bindings
  release the GIL *during parse*, but the per-node post-processing (extracting
  fields, building dicts, walking AST via Python) holds the GIL. On
  CPU-bound workloads where post-processing dominates, ThreadPoolExecutor
  can saturate a single core even with 48 threads. If you observe this
  pattern (multi-threaded scan at ~100% CPU on one core), consider:

  1. Profile the scanner to confirm where time is spent (cProfile).
  2. If post-processing dominates, use ``map_files_processpool`` below to
     run the scanner in child processes (each gets its own GIL).
  3. Be aware of memory cost: each child process duplicates Python state.
     On a 5GB RSS scan, 16 children = ~80GB. Cap workers conservatively.

* ``ProcessPoolExecutor`` — true multi-core for pure-Python CPU-bound loops,
  but requires picklable work units. Caller must slice data into self-contained
  chunks and pass a top-level function (not a lambda or local closure).
  See ``map_files_processpool`` for the file-level scan helper.

The choice is governed by a single ``jobs`` integer:

* ``jobs == 0`` — auto, capped at ``min(cpu_count, max_workers_cap)``
* ``jobs == 1`` — sequential (no executor overhead)
* ``jobs >= 2`` — explicit parallelism

For very large graphs (>= ``LARGE_GRAPH_NODES``), the helper auto-caps worker
count to avoid memory blow-up from duplicated per-worker state — the caller
still benefits from parallelism but the cap prevents thrashing.

**Configuration**: the hard cap can be overridden via:
  1. Environment variable ``C2D_MAX_WORKERS`` (highest priority)
  2. CLI flag ``--max-workers`` (if the caller passes it)
  3. Default: ``min(cpu_count, 16)`` — was hardcoded 8, but modern machines
     with 64+ cores and 250GB+ RAM benefit from higher parallelism,
     especially for GIL-releasing workloads (tree-sitter, regex, zlib).
"""

from __future__ import annotations

import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Tuple
import logging


def map_files_processpool(
    file_items: List[Tuple[str, str]],
    work_fn: Callable[[str, str], Any],
    jobs: int = 0,
    max_workers_cap: int = 0,
    desc: str = "",
    batch_size: int = 50,
) -> List[Any]:
    """Apply ``work_fn(file_path, file_lang)`` to each file using ProcessPoolExecutor.

    Phase J (2026-08-27): true multi-core for CPU-bound tree-sitter parsing.
    The Python tree-sitter bindings do NOT release the GIL during parse, so
    ThreadPoolExecutor serializes 48 threads onto a single core. This helper
    uses ProcessPoolExecutor to bypass the GIL — each child process gets its
    own Python interpreter, its own GIL, and its own tree-sitter parser.

    Requirements on ``work_fn``:
    - Must be a top-level module function (not a lambda or local closure).
    - Must accept exactly ``(file_path: str, file_lang: str)``.
    - Any config it needs must be read from a module-level context set by
      ``_set_process_scan_context()`` in the child (call this in the
      ``initializer``).
    - Must return a picklable result (dict of primitives/lists).

    Args:
        file_items: List of ``(file_path, file_lang)`` tuples.
        work_fn: Top-level callable, picklable.
        jobs: Worker count (0=auto, 1=sequential, N=N processes).
        max_workers_cap: Override the hard cap.
        desc: Human-readable label (printed once).
        batch_size: Submit futures in batches (smaller for processes —
            each Future carries pickle overhead).

    Returns:
        List of results in the same order as ``file_items``. ``None`` entries
        for items where the worker raised.
    """
    n = len(file_items)
    if n == 0:
        return []
    workers = cap_for_graph(resolve_jobs(jobs, max_workers_cap=max_workers_cap), n)
    if workers <= 1 or n < 2:
        return [work_fn(fp, fl) for fp, fl in file_items]

    results: List[Any] = [None] * n
    idx = 0
    ctx = multiprocessing.get_context("spawn")  # spawn is safer (no inherited state)
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
        active: Dict[Any, int] = {}
        while idx < n or active:
            batch_end = min(idx + batch_size, n)
            while idx < batch_end:
                fp, fl = file_items[idx]
                fut = pool.submit(work_fn, fp, fl)
                active[fut] = idx
                idx += 1
            completed = [f for f in active if f.done()]
            if not completed:
                for f in as_completed(list(active.keys())):
                    completed.append(f)
                    break
            for fut in completed:
                i = active.pop(fut)
                try:
                    results[i] = fut.result()
                except Exception:
                    results[i] = None
    return results



# Graphs above this node count are considered "large"; parallel loops over
# their nodes auto-cap workers to avoid per-worker memory duplication.
LARGE_GRAPH_NODES = 100_000

# Default hard ceiling on worker count. Was 8 (too low for modern hardware),
# changed to 16 in the previous fix — but 16 causes oversubscription on
# machines with fewer than 16 cores (4-core box + -j 48 → 16 threads = 4x
# oversubscription → OS scheduling overhead + memory waste).
#
# Now: the effective default is min(cpu_count, 16) — never more than the
# machine's core count, but at most 16 for safety on high-core boxes.
# Override via C2D_MAX_WORKERS env var or --max-workers CLI flag.
DEFAULT_MAX_WORKERS = 16


def _get_max_workers_cap(cli_override: int = 0) -> int:
    """Resolve the effective worker cap.

    Priority: CLI --max-workers > C2D_MAX_WORKERS env > min(cpu_count, 16).

    The default is min(cpu_count, 16) — this prevents oversubscription on
    low-core machines (4-core box won't get 16 threads) while still capping
    at 16 on high-core machines (128-core box won't get 128 threads unless
    the user explicitly overrides).

    Use --max-workers N or C2D_MAX_WORKERS=N to override (e.g., 48 on a
    64-core, 250GB RAM box for maximum throughput).
    """
    # CLI override (highest priority — user explicitly chose)
    if cli_override and cli_override > 0:
        return cli_override
    # Environment variable
    env = os.environ.get("C2D_MAX_WORKERS", "")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    try:
        cpu = multiprocessing.cpu_count()
    except (NotImplementedError, OSError):
        cpu = 4
    return max(2, min(cpu, DEFAULT_MAX_WORKERS))


def resolve_jobs(jobs: int, max_workers_cap: int = 0) -> int:
    """Normalize a ``--jobs`` style argument into a concrete worker count.

    ``0`` (auto) → ``min(cpu_count, max_workers_cap)``.
    ``1`` → sequential (returned as 1; callers should skip executor setup).
    Negative values are clamped to 1.

    ``max_workers_cap``: override the hard cap (0 = use env/default).
    """
    cap = _get_max_workers_cap(max_workers_cap)
    if jobs is None or jobs <= 0:
        try:
            cpu = multiprocessing.cpu_count()
        except (NotImplementedError, OSError):
            cpu = 4
        return max(2, min(cpu, cap))
    if jobs == 1:
        return 1
    return min(jobs, cap)


def cap_for_graph(jobs: int, n_nodes: int) -> int:
    """Auto-cap workers when operating on very large graphs.

    On 700K+ node graphs, each thread's per-iteration state (regex match
    objects, dict copies) can balloon memory. We keep the speedup but cap
    the worker count so memory pressure stays bounded.

    The caps here are LOWER bounds — they never reduce below 2, and they
    only apply to graphs above LARGE_GRAPH_NODES. For smaller graphs, the
    full requested jobs count is used (bounded by resolve_jobs).
    """
    if n_nodes >= 500_000:
        return max(2, min(jobs, 4))
    if n_nodes >= 200_000:
        return max(2, min(jobs, 6))
    return jobs


def map_nodes(
    items: List[Tuple[Any, Any]],
    work_fn: Callable[[Any, Any], Any],
    jobs: int = 0,
    max_workers_cap: int = 0,
    desc: str = "",
    batch_size: int = 200,
    parallel_mode: str = "thread",
) -> List[Any]:
    """Apply ``work_fn(node_id, node_data)`` to each item, returning results in order.

    Designed for per-node loops over ``G.nodes(data=True)`` where each call
    is independent and returns a small picklable result (e.g., a dict of
    fields to merge back into the node).

    When ``parallel_mode='thread'`` (default), uses ThreadPoolExecutor — the
    caller's ``work_fn`` can safely read/write shared state via the returned
    dict, since merging happens sequentially on the main thread. Best for
    I/O-bound or GIL-releasing workloads (tree-sitter parse, re.finditer).

    When ``parallel_mode='process'``, uses ProcessPoolExecutor with fork COW.
    Best for pure-Python CPU-bound workloads (regex matching, dict
    construction, AST walking) where the GIL serializes ThreadPool workers.
    Requirements: ``work_fn`` must be a top-level module function (picklable),
    and each (node_id, node_data) tuple must be picklable. On Linux (default
    fork start method), child processes inherit the parent's memory via
    copy-on-write — large shared state (graph, globals) is available without
    pickling.

    Args:
        items: List of ``(node_id, node_data)`` tuples.
        work_fn: Top-level or local callable taking ``(node_id, node_data)``.
            For ``parallel_mode='process'``, must be top-level (picklable).
        jobs: Worker count (0=auto, 1=sequential, N=N workers).
        max_workers_cap: Override the hard cap (0=use env/default).
        desc: Human-readable label printed once at start.
        batch_size: Submit futures in batches of this size.
        parallel_mode: 'thread' (default) or 'process'.

    Returns:
        List of results, in the same order as ``items``.
    """
    n = len(items)
    if n == 0:
        return []
    workers = cap_for_graph(resolve_jobs(jobs, max_workers_cap=max_workers_cap), n)
    if workers <= 1 or n < 2:
        return [work_fn(nid, nd) for nid, nd in items]

    # ProcessPoolExecutor path: for pure-Python CPU-bound workloads.
    if parallel_mode == "process" and n > 100:
        try:
            from concurrent.futures import ProcessPoolExecutor
            import multiprocessing as _mp
            ctx = _mp.get_context("fork")
            chunk_size = max(1, n // (workers * 4))
            with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
                # pool.map handles batching internally; chunk_size controls
                # how many items each worker processes before returning.
                # work_fn must be a top-level function (picklable).
                results = list(pool.map(
                    lambda item: work_fn(item[0], item[1]),
                    items,
                    chunksize=chunk_size,
                ))
            return results
        except (ImportError, OSError, BrokenPipeError, AttributeError):
            pass  # fall back to ThreadPoolExecutor

    # ThreadPoolExecutor path (default or fallback).
    results: List[Any] = [None] * n
    idx = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        active: Dict[Any, int] = {}  # future → index in results
        while idx < n or active:
            # Submit a batch
            batch_end = min(idx + batch_size, n)
            while idx < batch_end:
                nid, nd = items[idx]
                fut = pool.submit(work_fn, nid, nd)
                active[fut] = idx
                idx += 1
            # Drain completed futures
            completed = [f for f in active if f.done()]
            if not completed:
                # Wait for at least one
                for f in as_completed(list(active.keys())):
                    completed.append(f)
                    break
            for fut in completed:
                i = active.pop(fut)
                try:
                    results[i] = fut.result()
                except Exception:
                    results[i] = None
    return results


def merge_node_attributes(
    G,
    items: List[Tuple[Any, Any]],
    work_fn: Callable[[Any, Any], Optional[Dict[str, Any]]],
    jobs: int = 0,
    max_workers_cap: int = 0,
    desc: str = "",
    batch_size: int = 200,
    parallel_mode: str = "thread",
) -> int:
    """Parallel map then merge per-node results back into the graph.

    ``work_fn`` returns either ``None`` (skip this node) or a dict of
    attributes to set on the node. This helper runs the parallel map and
    applies the writes sequentially on the main thread (thread-safe).

    Returns the count of nodes that produced a non-empty result.
    """
    results = map_nodes(items, work_fn, jobs=jobs,
                        max_workers_cap=max_workers_cap,
                        desc=desc, batch_size=batch_size,
                        parallel_mode=parallel_mode)
    count = 0
    for (nid, _nd), res in zip(items, results):
        if not res:
            continue
        node_touched = False
        for k, v in res.items():
            # Match the original per-node loop's `if val:` semantics —
            # falsy values (empty list, empty string, 0, False, None) are
            # treated as "nothing to set" and skipped. Callers that need
            # to write 0/False should do so directly on the node, not via
            # this helper.
            if not v:
                continue
            G.nodes[nid][k] = v
            node_touched = True
        if node_touched:
            count += 1
    return count


__all__ = [
    "LARGE_GRAPH_NODES",
    "DEFAULT_MAX_WORKERS",
    "_get_max_workers_cap",
    "resolve_jobs",
    "cap_for_graph",
    "map_nodes",
    "merge_node_attributes",
    "map_files_processpool",
]
