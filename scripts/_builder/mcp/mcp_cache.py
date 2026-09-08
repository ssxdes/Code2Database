"""MCP cache state and helpers — split from mcp_server.py.

Holds the graph and cgdb store caches so tool modules can import
without circular dependencies with mcp_server.py.
"""

import os
import atexit
import threading
import logging


_GRAPH_CACHE = {}
_GRAPH_CACHE_LOCK = threading.RLock()

_CGDB_STORE_CACHE: dict = {}
_CGDB_STORE_CACHE_LOCK = threading.RLock()


def _get_graph(graph_dir: str):
    """Get a cached graph instance, or load and cache a new one."""
    with _GRAPH_CACHE_LOCK:
        if graph_dir in _GRAPH_CACHE:
            return _GRAPH_CACHE[graph_dir]
        from _builder.graph.graph_build import _load_full_graph
        G = _load_full_graph(graph_dir)
        _GRAPH_CACHE[graph_dir] = G
        return G


def _close_cached_graphs():
    """Close any cached graph connections on exit."""
    with _GRAPH_CACHE_LOCK:
        for graph_dir, G in list(_GRAPH_CACHE.items()):
            try:
                close = getattr(G, "close", None)
                if callable(close):
                    close()
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        _GRAPH_CACHE.clear()


atexit.register(_close_cached_graphs)


def _drop_cgdb_store(graph_dir: str):
    """Close and evict a cached cgdb store."""
    with _CGDB_STORE_CACHE_LOCK:
        store = _CGDB_STORE_CACHE.pop(graph_dir, None)
    if store is not None:
        try:
            store.close()
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)


def _close_cached_cgdb_stores():
    """Close all cached cgdb store connections on exit."""
    for graph_dir in list(_CGDB_STORE_CACHE.keys()):
        _drop_cgdb_store(graph_dir)


atexit.register(_close_cached_cgdb_stores)


def _cgdb_store(graph_dir: str):
    """Get a SQLiteCGDBStore for the graph_dir's code2database.db."""
    import sqlite3
    db_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.exists(db_path):
        _drop_cgdb_store(graph_dir)
        return None
    with _CGDB_STORE_CACHE_LOCK:
        cached = _CGDB_STORE_CACHE.get(graph_dir)
        if cached is not None:
            try:
                cached._ensure_conn().execute("SELECT 1").fetchone()
                return cached
            except sqlite3.Error:
                _drop_cgdb_store(graph_dir)
        try:
            from _builder.cgdb.cgdb_store import SQLiteCGDBStore
            store = SQLiteCGDBStore(db_path)
            conn = store._ensure_conn()
            conn.execute("SELECT 1 FROM cgdb_nodes LIMIT 1").fetchone()
            _CGDB_STORE_CACHE[graph_dir] = store
            return store
        except sqlite3.Error:
            return None


def _mcp_coerce_str(value, max_len: int = 10000) -> str:
    """Best-effort str coercion for MCP arguments.

    MCP clients send JSON whose types we don't control: a stray int for
    a string field used to crash the tool with AttributeError (a 500
    to the client). None → '', scalars (int/float/bool) → str(), str →
    truncated at max_len. Lists/dicts raise ValueError — callers convert
    that into an {'error': ...} response.
    """
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        raise ValueError(f"expected a string, got {type(value).__name__}")
    return str(value)[:max_len]


def _mcp_coerce_int(value, default: int, lo: int, hi: int) -> int:
    """Bounded int coercion for MCP arguments.

    A non-numeric value falls back to the default; out-of-range values
    clamp. Never raises — the old int(args.get(...)) crashed with
    ValueError on client type mistakes.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))
