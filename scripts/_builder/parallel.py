"""Parallel execution helpers for build/scan hot loops.

This module centralizes the choice between sequential and parallel execution
so callers don't re-implement the same boilerplate. Two execution modes are
supported:

* ``ThreadPoolExecutor`` (default for in-process parallelism) — works with
  shared graph state, no pickling overhead. Tree-sitter and ``re`` release
  the GIL, so regex-heavy per-node work (state_access extraction, heuristic
  enhance) actually parallelizes.
* ``ProcessPoolExecutor`` — true multi-core for pure-Python CPU-bound loops,
  but requires picklable work units. Caller must slice data into self-contained
  chunks and pass a top-level function (not a lambda or local closure).

The choice is governed by a single ``jobs`` integer:

* ``jobs == 0`` — auto, capped at ``min(cpu_count, 8)``
* ``jobs == 1`` — sequential (no executor overhead)
* ``jobs >= 2`` — explicit parallelism

For very large graphs (>= ``LARGE_GRAPH_NODES``), the helper auto-caps worker
count to avoid memory blow-up from duplicated per-worker state — the caller
still benefits from parallelism but the cap prevents thrashing.
"""

from __future__ import annotations

import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

# Graphs above this node count are considered "large"; parallel loops over
# their nodes auto-cap workers to avoid per-worker memory duplication.
LARGE_GRAPH_NODES = 100_000

# Hard ceiling on worker count. Past 8 workers, Python's GIL contention
# and per-worker memory overhead tend to outweigh throughput gains for
# the regex/AST workloads this module serves.
MAX_WORKERS_HARD_CAP = 8


def resolve_jobs(jobs: int) -> int:
    """Normalize a ``--jobs`` style argument into a concrete worker count.

    ``0`` (auto) → ``min(cpu_count, MAX_WORKERS_HARD_CAP)``.
    ``1`` → sequential (returned as 1; callers should skip executor setup).
    Negative values are clamped to 1.
    """
    if jobs is None or jobs <= 0:
        try:
            cpu = multiprocessing.cpu_count()
        except (NotImplementedError, OSError):
            cpu = 4
        return max(2, min(cpu, MAX_WORKERS_HARD_CAP))
    if jobs == 1:
        return 1
    return min(jobs, MAX_WORKERS_HARD_CAP)


def cap_for_graph(jobs: int, n_nodes: int) -> int:
    """Auto-cap workers when operating on very large graphs.

    On 700K+ node graphs, each thread's per-iteration state (regex match
    objects, dict copies) can balloon memory. We keep the speedup but cap
    the worker count so memory pressure stays bounded.
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
    desc: str = "",
    batch_size: int = 200,
) -> List[Any]:
    """Apply ``work_fn(node_id, node_data)`` to each item, returning results in order.

    Designed for per-node loops over ``G.nodes(data=True)`` where each call
    is independent and returns a small picklable result (e.g., a dict of
    fields to merge back into the node). Uses ThreadPoolExecutor — the
    caller's ``work_fn`` can safely read/write shared state via the returned
    dict, since merging happens sequentially on the main thread.

    Args:
        items: List of ``(node_id, node_data)`` tuples (typically
            ``list(G.nodes(data=True))``).
        work_fn: Top-level or local callable taking ``(node_id, node_data)``
            and returning a result. For ThreadPoolExecutor this can be a
            closure; no pickling is involved.
        jobs: Worker count (0=auto, 1=sequential, N=N threads).
        desc: Human-readable label printed once at start (for stderr logs).
        batch_size: Submit futures in batches of this size to bound the
            number of pending Future objects in flight.

    Returns:
        List of results, in the same order as ``items``.
    """
    n = len(items)
    if n == 0:
        return []
    workers = cap_for_graph(resolve_jobs(jobs), n)
    if workers <= 1 or n < 2:
        return [work_fn(nid, nd) for nid, nd in items]

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
    desc: str = "",
    batch_size: int = 200,
) -> int:
    """Parallel map then merge per-node results back into the graph.

    ``work_fn`` returns either ``None`` (skip this node) or a dict of
    attributes to set on the node. This helper runs the parallel map and
    applies the writes sequentially on the main thread (thread-safe).

    Returns the count of nodes that produced a non-empty result.
    """
    results = map_nodes(items, work_fn, jobs=jobs, desc=desc,
                        batch_size=batch_size)
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
    "MAX_WORKERS_HARD_CAP",
    "resolve_jobs",
    "cap_for_graph",
    "map_nodes",
    "merge_node_attributes",
]
