"""Query result cache for Code2Database.

LRU-style cache with TTL and node-version invalidation. Caches the result
of expensive query commands (describe-node, explore-flow, concurrency-risks,
blast-radius, find-invariants, path, trace-chain, reverse-trace) so repeated
queries on unchanged nodes skip recomputation.

Invalidation strategy:
- Each cache entry is keyed by (command, args_tuple, graph_dir, node_versions).
- node_versions is a frozenset of (node_id, version) pairs for the nodes the
  query touched. When update_node or patch_from_diff changes a node, its
  version bumps and entries referencing the old version are evicted on next
  read.
- Graph mtime check (entries also record the mtime of the
  graph SQLite file at cache time. On get, if the file mtime differs, the
  entry is invalidated. This catches any write path that bypasses
  invalidate_node (e.g., daemon transactions, manual sqlite3 edits).
- TTL (default 600s) provides a secondary expiry for safety.
- `--no-cache` CLI flag (callers can set
  `args.no_cache = True` to bypass the cache for a single invocation.

Usage:
    from _builder.query_cache import cached_query, invalidate_node, invalidate_all

    @cached_query('describe-node', ttl=600)
    def cmd_describe_node(args):
        ...

    # On update_node:
    invalidate_node(graph_dir, node_id)
"""
import os
import time
import json
import hashlib
import threading
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional, Tuple
import logging


# Per-graph-dir cache store. Each graph_dir has its own LRU + node-version map.
_CACHES: Dict[str, "_GraphCache"] = {}
_LOCK = threading.Lock()


class _GraphCache:
    """LRU cache for one graph directory, with node-version invalidation."""

    def __init__(self, graph_dir: str, max_entries: int = 256, ttl_seconds: int = 600):
        self.graph_dir = graph_dir
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        # key -> (timestamp, result, graph_mtime_at_cache_time)
        # graph_mtime lets us invalidate entries when the
        # SQLite file is touched by any write path (daemon tx, manual sqlite3,
        # patcher) — even if invalidate_node wasn't called.
        self._entries: OrderedDict[str, Tuple[float, Any, float]] = OrderedDict()
        # node_id -> version (monotonic int)
        self._node_versions: Dict[str, int] = {}
        # key -> set of node_ids touched (for invalidation)
        self._key_nodes: Dict[str, frozenset] = {}
        self._lock = threading.Lock()

    def _graph_mtime(self) -> float:
        """Return mtime of the graph SQLite file, or 0 if not found.

        We check `<graph_dir>/code2database.db` first,
        then fall back to `<graph_dir>/callgraph.db` (legacy name).
        """
        for fname in ("code2database.db", "callgraph.db"):
            p = os.path.join(self.graph_dir, fname)
            if os.path.exists(p):
                try:
                    return os.path.getmtime(p)
                except OSError:
                    return 0
        return 0

    def get(self, key: str, node_versions_snapshot: Dict[str, int]) -> Optional[Any]:
        """Return cached result if still valid, else None.

        node_versions_snapshot: caller's current view of (node_id -> version)
        for the nodes this query depends on. If any version differs from the
        cache's recorded version, the entry is invalidated.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            timestamp, result, cached_mtime = entry
            # TTL check
            if time.time() - timestamp > self.ttl_seconds:
                self._entries.pop(key, None)
                self._key_nodes.pop(key, None)
                return None
            # Graph mtime check
            current_mtime = self._graph_mtime()
            if current_mtime != cached_mtime:
                self._entries.pop(key, None)
                self._key_nodes.pop(key, None)
                return None
            # Node-version check
            touched = self._key_nodes.get(key, frozenset())
            for nid in touched:
                cached_ver = self._node_versions.get(nid, 0)
                current_ver = node_versions_snapshot.get(nid, 0)
                if cached_ver != current_ver:
                    self._entries.pop(key, None)
                    self._key_nodes.pop(key, None)
                    return None
            # LRU touch
            self._entries.move_to_end(key)
            return result

    def put(self, key: str, result: Any,
            node_versions_snapshot: Dict[str, int],
            touched_nodes: frozenset) -> None:
        with self._lock:
            # Evict oldest if at capacity
            while len(self._entries) >= self.max_entries:
                evicted_key, _ev = self._entries.popitem(last=False)
                self._key_nodes.pop(evicted_key, None)
            self._entries[key] = (time.time(), result, self._graph_mtime())
            self._key_nodes[key] = touched_nodes
            # Record the versions we cached against
            for nid in touched_nodes:
                self._node_versions[nid] = node_versions_snapshot.get(nid, 0)

    def invalidate_node(self, node_id: str) -> None:
        """Bump a node's version and evict all entries that touched it."""
        with self._lock:
            self._node_versions[node_id] = self._node_versions.get(node_id, 0) + 1
            # Eager eviction: drop entries that touched this node
            keys_to_drop = [
                k for k, touched in self._key_nodes.items()
                if node_id in touched
            ]
            for k in keys_to_drop:
                self._entries.pop(k, None)
                self._key_nodes.pop(k, None)

    def invalidate_all(self) -> None:
        with self._lock:
            self._entries.clear()
            self._key_nodes.clear()
            self._node_versions.clear()

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "tracked_nodes": len(self._node_versions),
                "max_entries": self.max_entries,
                "ttl_seconds": self.ttl_seconds,
            }


def _get_cache(graph_dir: str) -> _GraphCache:
    """Get or create the cache for a graph directory."""
    with _LOCK:
        if graph_dir not in _CACHES:
            _CACHES[graph_dir] = _GraphCache(graph_dir)
        return _CACHES[graph_dir]


def _make_key(command: str, args: Dict[str, Any], graph_dir: str) -> str:
    """Build a stable hash key from command + args + graph_dir."""
    # Sort args for stability; ignore non-hashable values by json-serializing
    try:
        args_repr = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        args_repr = repr(sorted(args.items(), key=lambda kv: kv[0]))
    h = hashlib.sha256(f"{command}|{graph_dir}|{args_repr}".encode("utf-8"))
    return h.hexdigest()


def cached_query(command: str, ttl: int = 600,
                 touched_nodes_fn: Optional[Callable[[Any], frozenset]] = None,
                 capture_stdout: bool = False):
    """Decorator for query commands.

    Args:
        command: command name (used as part of cache key).
        ttl: cache TTL in seconds.
        touched_nodes_fn: optional function (args -> frozenset of node_ids the
            query depends on). If None, the query is cached without node-version
            invalidation (only TTL).
        capture_stdout: if True, capture stdout produced by the wrapped function
            (for commands that print JSON instead of returning it). The captured
            text is replayed on cache hit.

    The decorated function must accept an `args` namespace dict-like. If
    capture_stdout is False, the function should return a JSON-serializable
    result; if True, the function may print to stdout and return None.
    """
    def decorator(fn):
        def wrapper(args):
            graph_dir = getattr(args, "graph", None) or (args.get("graph") if isinstance(args, dict) else None)
            if not graph_dir:
                return fn(args)

            # --no-cache bypasses cache read AND write.
            # Caller sets args.no_cache = True (CLI flag) to skip caching
            # for this single invocation — useful when stale data is
            # suspected or for one-off fresh queries.
            no_cache = getattr(args, "no_cache", False)
            if isinstance(args, dict):
                no_cache = args.get("no_cache", False)

            try:
                args_dict = {k: v for k, v in vars(args).items() if k != "graph"}
            except TypeError:
                args_dict = dict(args) if isinstance(args, dict) else {}

            cache = _get_cache(graph_dir)
            key = _make_key(command, args_dict, graph_dir)

            touched = frozenset()
            if touched_nodes_fn is not None:
                try:
                    touched = touched_nodes_fn(args) or frozenset()
                except Exception:
                    touched = frozenset()

            with cache._lock:
                snapshot = {nid: cache._node_versions.get(nid, 0) for nid in touched}

            if not no_cache:
                cached = cache.get(key, snapshot)
                if cached is not None:
                    if capture_stdout:
                        import sys
                        sys.stdout.write(cached)
                    return cached

            if capture_stdout:
                import io
                import sys as _sys
                buf = io.StringIO()
                old_stdout = _sys.stdout
                _sys.stdout = buf
                try:
                    fn(args)
                finally:
                    _sys.stdout = old_stdout
                captured = buf.getvalue()
                if not no_cache:
                    try:
                        cache.put(key, captured, snapshot, touched)
                    except Exception:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                return captured
            else:
                result = fn(args)
                if not no_cache:
                    try:
                        cache.put(key, result, snapshot, touched)
                    except Exception:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                return result
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        wrapper._is_cached = True
        return wrapper
    return decorator


def invalidate_node(graph_dir: str, node_id: str) -> None:
    """Bump a node's version and evict cache entries that touched it."""
    cache = _get_cache(graph_dir)
    cache.invalidate_node(node_id)


def invalidate_all(graph_dir: str) -> None:
    """Clear all cache entries for a graph directory."""
    cache = _get_cache(graph_dir)
    cache.invalidate_all()


def cache_stats(graph_dir: str) -> Dict[str, int]:
    """Return cache statistics for a graph directory."""
    cache = _get_cache(graph_dir)
    return cache.stats()
