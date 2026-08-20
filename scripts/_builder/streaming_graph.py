#!/usr/bin/env python3
"""Streaming graph backend that writes to SQLite incrementally.

Replaces nx.DiGraph for --storage sqlite --low-memory builds to avoid
holding the entire graph in memory. Provides the same API surface that
build_graph() expects, but streams nodes and edges to SQLite as they
are created, keeping only lightweight lookup structures in RAM.

Memory budget for 1.4M nodes / 4.35M edges (deferred mode):
  - id_registry:       ~1.4M × ~200 bytes  = ~280 MB
  - edge_set:          ~4.35M × ~64 bytes  = ~280 MB  (vs 870MB for edge_data)
  - adjacency lists:   ~4.35M × ~64 bytes  = ~280 MB
  - edge_batch:        ~4.35M × ~300 bytes = ~1.3 GB  (written to SQLite in close)
  - SQLite cache:       64 MB
  Total: ~1.9 GB vs ~2.4 GB without deferred mode vs ~24 GB with NetworkX
"""

import gc
import os
import sys
import time
from typing import Optional, Dict, List, Any, Tuple, Set


class _StreamingNodeView:
    """NetworkX-compatible NodeView for StreamingGraph.

    Supports:
      - view[nid] → node attributes dict
      - view(data=True) → iterator of (nid, attrs)
      - view() → iterator of nids
      - nid in view → bool
    """

    def __init__(self, id_registry: Dict[str, Dict]):
        self._registry = id_registry

    def __getitem__(self, nid: str) -> Dict:
        return self._registry[nid]

    def get(self, nid: str, default=None):
        """Get node attributes, returning default if not found."""
        return self._registry.get(nid, default)

    def __contains__(self, nid) -> bool:
        return nid in self._registry

    def __iter__(self):
        yield from self._registry.keys()

    def __len__(self):
        return len(self._registry)

    def __call__(self, data: bool = False):
        """When called like G.nodes(data=True), return appropriate iterator."""
        if data:
            return iter(self._registry.items())
        return iter(self._registry.keys())


class _StreamingEdgeView:
    """NetworkX-compatible EdgeView for StreamingGraph.

    Supports:
      - view(data=True) → iterator of (u, v, attrs)
      - view() → iterator of (u, v) tuples
      - (u, v) in view → bool
      - view[u, v] → edge attributes dict (like NetworkX G.edges[u, v])
    """

    def __init__(self, graph: 'StreamingGraph'):
        self._graph = graph

    def __call__(self, data: bool = False):
        """When called like G.edges(data=True), return appropriate iterator."""
        edge_data = self._graph._edge_data
        edge_set = self._graph._edge_set
        if self._graph._deferred:
            # In deferred mode, iterate from _edge_set (no attrs available)
            if data:
                return ((u, v, {}) for u, v in edge_set)
            return iter(edge_set)
        if data:
            return iter((u, v, attrs) for (u, v), attrs in edge_data.items())
        return iter((u, v) for u, v in edge_data)

    def __contains__(self, item) -> bool:
        if self._graph._deferred:
            return item in self._graph._edge_set
        return item in self._graph._edge_data

    def __getitem__(self, key):
        """Support G.edges[u, v] → edge attributes dict."""
        if isinstance(key, tuple) and len(key) == 2:
            if self._graph._deferred:
                return {}
            return self._graph._edge_data[key]
        raise TypeError(f"Edge key must be a 2-tuple (u, v), got {type(key)}")

    def __iter__(self):
        if self._graph._deferred:
            yield from self._graph._edge_set
        else:
            yield from self._graph._edge_data.keys()

    def __len__(self):
        if self._graph._deferred:
            return len(self._graph._edge_set)
        return len(self._graph._edge_data)


class _EdgeAttrProxy:
    """Proxy for G[u][v] access pattern — returns edge attributes dict."""

    def __init__(self, graph: 'StreamingGraph', u: str):
        self._graph = graph
        self._u = u

    def __getitem__(self, v: str) -> Dict:
        if self._graph._deferred:
            return {}
        return self._graph._edge_data[(self._u, v)]


class _NodeEdgeProxy:
    """Proxy for G[u] access — returns object with [v] subscript for edge data."""

    def __init__(self, graph: 'StreamingGraph', u: str):
        self._graph = graph
        self._u = u

    def __getitem__(self, v: str) -> Dict:
        if self._graph._deferred:
            return {}
        return self._graph._edge_data[(self._u, v)]


class StreamingGraph:
    """NetworkX-compatible graph that streams to SQLite.

    Implements the subset of nx.DiGraph API used by build_graph():
      - add_node(id, **attrs)
      - add_edge(u, v, **attrs)
      - has_node(id) -> bool
      - has_edge(u, v) -> bool
      - nodes(data=True) -> iterator
      - nodes[nid] -> dict (node attributes)
      - edges(data=True) -> iterator
      - G[u][v] -> dict (edge attributes)
      - number_of_nodes() -> int
      - number_of_edges() -> int
      - remove_node(id)
      - degree(nid) -> int
      - __contains__(id) -> bool  (for "nid in G")
      - predecessors(nid) -> iterator
      - successors(nid) -> iterator

    In deferred mode (enabled via set_deferred(True)), edge attributes are
    not stored in _edge_data — only edge keys are tracked in _edge_set.
    This saves ~590MB for 5.4M edges at the cost of losing edge attribute
    access until set_deferred(False) is called to rebuild _edge_data.
    """

    def __init__(self, db_path: str, batch_size_functions: int = 5000,
                 batch_size_edges: int = 5000):
        from _builder.sqlite_store import SQLiteStore
        self._store = SQLiteStore(db_path)
        self._store.connect()
        self._func_batch: List[Dict] = []
        self._edge_batch: List[Dict] = []
        self._func_batch_size = batch_size_functions
        self._edge_batch_size = batch_size_edges

        # Lightweight in-memory structures
        self.id_registry: Dict[str, Dict] = {}
        self._edge_data: Dict[Tuple[str, str], Dict] = {}
        self._edge_set: Set[Tuple[str, str]] = set()
        self._node_count = 0
        self._edge_count = 0

        # Deferred mode: skip _edge_data storage during edge processing
        self._deferred = False

        # Degree tracking
        self._in_degree: Dict[str, int] = {}
        self._out_degree: Dict[str, int] = {}

        # Adjacency lists for predecessors/successors support
        self._predecessors: Dict[str, Set[str]] = {}
        self._successors: Dict[str, Set[str]] = {}

        # Flush counters for progress reporting
        self._funcs_flushed = 0
        self._edges_flushed = 0

        # Views
        self._node_view = _StreamingNodeView(self.id_registry)
        self._edge_view = _StreamingEdgeView(self)

    # ---- Deferred mode ----

    def set_deferred(self, enabled: bool):
        """Enable or disable deferred edge storage mode.

        In deferred mode, add_edge() does NOT store edge attributes in
        _edge_data — only the edge key (u, v) is tracked in _edge_set.
        This saves ~590MB for 5.4M edges but edge attribute queries
        (G.edges[data=True], G[u][v]) return empty dicts.

        When disabling deferred mode, _edge_data is rebuilt from _edge_batch.
        """
        if enabled and not self._deferred:
            # Entering deferred mode: clear _edge_data to free memory
            self._edge_data.clear()
            gc.collect()
            self._deferred = True
            print(f"[StreamingGraph] Entering deferred mode "
                  f"({len(self._edge_set)} edges tracked, "
                  f"{len(self._edge_batch)} in batch)",
                  file=sys.stderr)
        elif not enabled and self._deferred:
            # Leaving deferred mode: rebuild _edge_data from _edge_batch
            self._deferred = False
            _rebuild_start = time.time()
            for edge_dict in self._edge_batch:
                u = edge_dict.get("caller", "")
                v = edge_dict.get("callee", "")
                if u and v:
                    key = (u, v)
                    if key in self._edge_set:
                        if key not in self._edge_data:
                            self._edge_data[key] = dict(edge_dict)
                            # Remove caller/callee from attrs (they're the key)
                            self._edge_data[key].pop("caller", None)
                            self._edge_data[key].pop("callee", None)
                        else:
                            # Merge attributes
                            merged = dict(edge_dict)
                            merged.pop("caller", None)
                            merged.pop("callee", None)
                            self._edge_data[key].update(merged)
            _rebuild_elapsed = time.time() - _rebuild_start
            # Free _edge_batch after rebuild — close() will write from _edge_data
            _batch_freed = len(self._edge_batch)
            self._edge_batch.clear()
            gc.collect()
            print(f"[StreamingGraph] Rebuilt _edge_data from batch: "
                  f"{len(self._edge_data)} edges in {_rebuild_elapsed:.1f}s "
                  f"(freed {_batch_freed} batch entries)",
                  file=sys.stderr)

    # ---- Write operations ----

    def add_node(self, node_id: str, **attrs):
        """Add a node and queue it for SQLite batch write."""
        self.id_registry[node_id] = dict(attrs, id=node_id)
        self._node_count += 1
        # Queue for batch write
        self._func_batch.append(dict(attrs, id=node_id))
        if len(self._func_batch) >= self._func_batch_size:
            self._flush_functions()
        # Initialize degree/adjacency tracking
        if node_id not in self._out_degree:
            self._out_degree[node_id] = 0
        if node_id not in self._in_degree:
            self._in_degree[node_id] = 0
        if node_id not in self._successors:
            self._successors[node_id] = set()
        if node_id not in self._predecessors:
            self._predecessors[node_id] = set()

    def add_edge(self, u: str, v: str, **attrs):
        """Add an edge and queue it for SQLite batch write.

        Like NetworkX, merges attributes if edge already exists.
        In deferred mode, attributes are stored only in _edge_batch
        (not _edge_data) to save memory.
        """
        edge_key = (u, v)

        if self._deferred:
            # Deferred mode: only track edge key, store attrs in batch
            is_new = edge_key not in self._edge_set
            if is_new:
                self._edge_set.add(edge_key)
                self._edge_count += 1
                # Update degree tracking
                self._out_degree[u] = self._out_degree.get(u, 0) + 1
                self._in_degree[v] = self._in_degree.get(v, 0) + 1
                # Update adjacency lists
                self._successors.setdefault(u, set()).add(v)
                self._predecessors.setdefault(v, set()).add(u)
            # Queue for batch write (attrs captured for close())
            self._edge_batch.append(dict(attrs, caller=u, callee=v))
            # In deferred mode, flush when batch reaches size to bound memory
            # (close() will NOT delete/rewrite edges in deferred mode — they
            # are already in SQLite, so intermediate flushes are safe).
            if len(self._edge_batch) >= self._edge_batch_size:
                self._flush_edges()
        else:
            # Normal mode: store full attrs in _edge_data
            is_new = edge_key not in self._edge_data
            if is_new:
                self._edge_data[edge_key] = dict(attrs)
                if self._deferred:
                    self._edge_set.add(edge_key)
                self._edge_count += 1
                # Update degree tracking (only for new edges)
                self._out_degree[u] = self._out_degree.get(u, 0) + 1
                self._in_degree[v] = self._in_degree.get(v, 0) + 1
                # Update adjacency lists (only for new edges)
                self._successors.setdefault(u, set()).add(v)
                self._predecessors.setdefault(v, set()).add(u)
            else:
                # Merge attributes like NetworkX does
                self._edge_data[edge_key].update(attrs)

            # Queue for batch write
            self._edge_batch.append(dict(attrs, caller=u, callee=v))
            if len(self._edge_batch) >= self._edge_batch_size:
                self._flush_edges()

    def remove_node(self, node_id: str):
        """Remove a node and all its edges."""
        if node_id not in self.id_registry:
            return
        del self.id_registry[node_id]
        self._node_count -= 1

        # Remove edges involving this node
        if self._deferred:
            edges_to_remove = [ek for ek in self._edge_set
                               if ek[0] == node_id or ek[1] == node_id]
            for ek in edges_to_remove:
                self._edge_set.discard(ek)
                self._edge_count -= 1
                u, v = ek
                if u in self._out_degree:
                    self._out_degree[u] = max(0, self._out_degree[u] - 1)
                if v in self._in_degree:
                    self._in_degree[v] = max(0, self._in_degree[v] - 1)
                if u in self._successors:
                    self._successors[u].discard(v)
                if v in self._predecessors:
                    self._predecessors[v].discard(u)
        else:
            edges_to_remove = [(u, v) for u, v in self._edge_data
                               if u == node_id or v == node_id]
            for ek in edges_to_remove:
                del self._edge_data[ek]
                self._edge_set.discard(ek)
                self._edge_count -= 1
                u, v = ek
                if u in self._out_degree:
                    self._out_degree[u] = max(0, self._out_degree[u] - 1)
                if v in self._in_degree:
                    self._in_degree[v] = max(0, self._in_degree[v] - 1)
                if u in self._successors:
                    self._successors[u].discard(v)
                if v in self._predecessors:
                    self._predecessors[v].discard(u)

        # Remove from degree and adjacency tracking
        self._out_degree.pop(node_id, None)
        self._in_degree.pop(node_id, None)
        self._successors.pop(node_id, None)
        self._predecessors.pop(node_id, None)

    # ---- Query operations ----

    def has_node(self, node_id: str) -> bool:
        return node_id in self.id_registry

    def has_edge(self, u: str, v: str) -> bool:
        if self._deferred:
            return (u, v) in self._edge_set
        return (u, v) in self._edge_data

    def number_of_nodes(self) -> int:
        return self._node_count

    def number_of_edges(self) -> int:
        return self._edge_count

    def degree(self, node_id: str) -> int:
        return self._out_degree.get(node_id, 0) + self._in_degree.get(node_id, 0)

    def predecessors(self, node_id: str):
        """Iterate over predecessor node IDs."""
        return iter(self._predecessors.get(node_id, set()))

    def successors(self, node_id: str):
        """Iterate over successor node IDs."""
        return iter(self._successors.get(node_id, set()))

    def in_edges(self, node_id: str, data: bool = False):
        """Iterate over incoming edges to node_id.

        When data=True, yields (u, node_id, attrs) tuples.
        Otherwise yields (u, node_id) tuples.
        """
        preds = self._predecessors.get(node_id, set())
        if data:
            if self._deferred:
                return ((u, node_id, {}) for u in preds)
            return ((u, node_id, self._edge_data[(u, node_id)])
                    for u in preds if (u, node_id) in self._edge_data)
        return ((u, node_id) for u in preds)

    def out_edges(self, node_id: str, data: bool = False):
        """Iterate over outgoing edges from node_id.

        When data=True, yields (node_id, v, attrs) tuples.
        Otherwise yields (node_id, v) tuples.
        """
        succs = self._successors.get(node_id, set())
        if data:
            if self._deferred:
                return ((node_id, v, {}) for v in succs)
            return ((node_id, v, self._edge_data[(node_id, v)])
                    for v in succs if (node_id, v) in self._edge_data)
        return ((node_id, v) for v in succs)

    def __contains__(self, node_id) -> bool:
        return node_id in self.id_registry

    def __getitem__(self, key):
        """Support G[u] → _NodeEdgeProxy for G[u][v] pattern."""
        if isinstance(key, str):
            return _NodeEdgeProxy(self, key)
        # Convert integer keys to string (some edge sources may be stored as ints)
        if isinstance(key, int):
            key = str(key)
            return _NodeEdgeProxy(self, key)
        raise TypeError(f"Unsupported key type: {type(key)}")

    @property
    def nodes(self):
        """Return NodeView for G.nodes[nid] and G.nodes(data=True) access."""
        return self._node_view

    @property
    def edges(self):
        """Return EdgeView for G.edges(data=True) and (u,v) in G.edges."""
        return self._edge_view

    # ---- Flush operations ----

    def _flush_functions(self):
        """Flush queued function nodes to SQLite."""
        if self._func_batch:
            self._store.store_functions(self._func_batch)
            self._funcs_flushed += len(self._func_batch)
            self._func_batch = []

    def _flush_edges(self):
        """Flush queued edges to SQLite."""
        if self._edge_batch:
            self._store.store_edges(self._edge_batch)
            self._edges_flushed += len(self._edge_batch)
            self._edge_batch = []

    def flush_all(self):
        """Flush all pending batches to SQLite."""
        self._flush_functions()
        self._flush_edges()

    # ---- Cleanup ----

    def close(self):
        """Flush remaining data, write final functions/edges, and close.

        In deferred mode, edges were streamed to SQLite during build (via
        _flush_edges in add_edge), so close() only flushes the residual batch
        and does NOT delete/rewrite edges — they are already persisted with
        their original attributes. Functions are still rewritten from
        id_registry to capture mutations (domain merges, label updates).

        In normal mode, rewrites from id_registry/_edge_data to capture
        any mutations (domain merges, label updates, call_condition additions).

        Uses a single transaction for the entire write to avoid thousands
        of fdatasync calls.
        """
        import gc

        # Start a single transaction for the entire write
        self._store._conn.execute("BEGIN TRANSACTION")

        # Clear tables
        self._store._conn.execute("DELETE FROM functions")
        if not self._deferred:
            # Normal mode: edges will be rewritten from _edge_data, so clear
            # them. Deferred mode: edges were streamed during build — do NOT
            # clear (would lose the streamed edges).
            self._store._conn.execute("DELETE FROM edges")

        _REWRITE_BATCH = 5000

        # Write functions from id_registry (captures mutations)
        _rewrite_batch = []
        _rewritten = 0
        for nid, attrs in self.id_registry.items():
            _rewrite_batch.append(attrs)
            if len(_rewrite_batch) >= _REWRITE_BATCH:
                self._store.store_functions(_rewrite_batch, autocommit=False)
                _rewritten += len(_rewrite_batch)
                _rewrite_batch = []

        if _rewrite_batch:
            self._store.store_functions(_rewrite_batch, autocommit=False)
            _rewritten += len(_rewrite_batch)

        if _rewritten > 0:
            print(f"[StreamingGraph] Wrote {_rewritten} functions to SQLite",
                  file=sys.stderr)

        # Free id_registry memory before processing edges
        self.id_registry.clear()
        gc.collect()

        if self._deferred:
            # Deferred mode: flush residual batch (most edges already streamed
            # to SQLite during build via add_edge's _flush_edges calls).
            _edge_written = 0
            if self._edge_batch:
                _edge_write_batch = []
                for edge_dict in self._edge_batch:
                    _edge_write_batch.append(edge_dict)
                    if len(_edge_write_batch) >= _REWRITE_BATCH:
                        self._store.store_edges(_edge_write_batch, autocommit=False)
                        _edge_written += len(_edge_write_batch)
                        _edge_write_batch = []
                if _edge_write_batch:
                    self._store.store_edges(_edge_write_batch, autocommit=False)
                    _edge_written += len(_edge_write_batch)
                # Free batch memory
                self._edge_batch.clear()
                gc.collect()
            if _edge_written > 0:
                print(f"[StreamingGraph] Wrote {_edge_written} residual edges to SQLite (deferred)",
                      file=sys.stderr)
        else:
            # Normal mode: rewrite from _edge_data (captures mutations)
            _edge_rewrite_batch = []
            _edge_rewritten = 0
            for (u, v), attrs in self._edge_data.items():
                _edge_rewrite_batch.append(dict(attrs, caller=u, callee=v))
                if len(_edge_rewrite_batch) >= _REWRITE_BATCH:
                    self._store.store_edges(_edge_rewrite_batch, autocommit=False)
                    _edge_rewritten += len(_edge_rewrite_batch)
                    _edge_rewrite_batch = []
            if _edge_rewrite_batch:
                self._store.store_edges(_edge_rewrite_batch, autocommit=False)
                _edge_rewritten += len(_edge_rewrite_batch)
            if _edge_rewritten > 0:
                print(f"[StreamingGraph] Wrote {_edge_rewritten} edges to SQLite (from edge_data)",
                      file=sys.stderr)

        if _rewritten > 0:
            print(f"[StreamingGraph] Final: {_rewritten} functions, "
                  f"{self._edge_count} edges in SQLite", file=sys.stderr)

        # Commit the single transaction and checkpoint
        self._store._conn.execute("COMMIT")
        # Ensure all writes are flushed to disk before closing
        # (critical for WAL mode — other connections must see the data)
        self._store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._store.close()

    # ---- Memory management ----

    def shrink_dedup(self):
        """Clear edge data, adjacency lists, and dedup set to free memory.

        Only call after all edges have been processed and flushed.
        After this, has_edge(), predecessors(), successors(), and
        edges(data=True) will no longer work correctly.
        """
        before_edges = len(self._edge_data)
        before_set = len(self._edge_set)
        before_succ = sum(len(s) for s in self._successors.values())
        self._edge_data.clear()
        self._edge_set.clear()
        self._successors.clear()
        self._predecessors.clear()
        gc.collect()
        print(f"[StreamingGraph] Cleared edge data ({before_edges} entries), "
              f"edge set ({before_set} entries), "
              f"adjacency lists ({before_succ} entries)", file=sys.stderr)


class LazySQLiteGraph:
    """NetworkX-DiGraph-compatible view over a code2database.db SQLite file.

    P0_fix: For large projects (>100K functions) that use --storage sqlite,
    loading the full graph into NetworkX consumes 10+ GB and times out.
    This class provides the subset of nx.DiGraph API that query commands
    use, but fetches nodes/edges from SQLite on demand.

    Supported API:
      - __contains__(nid) -> bool
      - __getitem__(nid) -> dict (node attrs, with .get() support)
      - nodes(data=True) -> iterator (WARNING: loads all — use sparingly)
      - nodes[nid] -> dict
      - has_node(nid) -> bool
      - has_edge(u, v) -> bool
      - number_of_nodes() -> int
      - number_of_edges() -> int
      - predecessors(nid) -> iterator of caller IDs
      - successors(nid) -> iterator of callee IDs
      - in_edges(nid, data=True) -> iterator
      - out_edges(nid, data=True) -> iterator
      - edges(data=True) -> iterator (WARNING: loads all)
      - get_edge_data(u, v) -> dict
      - add_node / add_edge — raises NotImplementedError (read-only)
      - degree(nid) -> int

    Limitations:
      - Read-only (no add_node/add_edge)
      - nodes(data=True) and edges(data=True) load everything — avoid for 1.5M-node graphs
      - No multi-graph support (parallel edges collapsed)
    """

    def __init__(self, db_path: str):
        import sqlite3
        from _builder.sqlite_store import SQLiteStore
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._store = SQLiteStore(db_path)
        self._store.connect()
        # Cache for single-node lookups (bounded LRU via dict size)
        self._node_cache = {}
        self._node_cache_max = 10000
        self._edge_cache = {}
        self._edge_cache_max = 50000
        self._succ_cache = {}
        self._succ_cache_max = 50000
        self._pred_cache = {}
        self._pred_cache_max = 50000

    def __contains__(self, node_id) -> bool:
        if node_id in self._node_cache:
            return True
        row = self._conn.execute(
            "SELECT 1 FROM functions WHERE id=? LIMIT 1", (node_id,)).fetchone()
        return row is not None

    def has_node(self, node_id: str) -> bool:
        return node_id in self

    def __iter__(self):
        # Yield all node IDs. Without this, `for nid in G` falls back to
        # sequence protocol with int indices, which __getitem__ rejects.
        cur = self._conn.execute("SELECT id FROM functions")
        for row in cur:
            yield row[0]

    def __getitem__(self, key):
        # G[u][v] → edge attrs; G[nid] not typically used (use G.nodes[nid])
        if isinstance(key, str):
            return self._get_node_attrs(key)
        raise TypeError(f"Unsupported key type: {type(key)}")

    def _get_node_attrs(self, nid: str) -> dict:
        if nid in self._node_cache:
            return self._node_cache[nid]
        attrs = self._fetch_node(nid)
        if attrs:
            if len(self._node_cache) >= self._node_cache_max:
                # Evict ~25% of cache (simple FIFO eviction)
                evict_count = self._node_cache_max // 4
                for k in list(self._node_cache.keys())[:evict_count]:
                    del self._node_cache[k]
            self._node_cache[nid] = attrs
        return attrs

    def _fetch_node(self, nid: str) -> dict:
        import json as _json
        row = self._conn.execute(
            "SELECT * FROM functions WHERE id=?", (nid,)).fetchone()
        if not row:
            return {}
        return self._build_attrs_from_row(dict(row))

    def _build_attrs_from_row(self, row_dict: dict) -> dict:
        """Build node attrs dict from a SQLite row dict.

        Shared between _fetch_node (single-node lookup) and __call__(data=True)
        (batch iteration) to avoid duplicated logic. body_text is left empty
        — use get_body_text(nid) to fetch it lazily.
        """
        import json as _json
        labels_raw = row_dict.get("labels", "[]")
        try:
            labels = _json.loads(labels_raw) if labels_raw else []
        except (_json.JSONDecodeError, TypeError):
            labels = []
        extra = {}
        extra_raw = row_dict.get("extra_json")
        if extra_raw:
            try:
                extra = _json.loads(extra_raw)
            except (_json.JSONDecodeError, TypeError):
                pass
        # Merge `_supplemented` keys back into the canonical attribute so
        # describe-node and other consumers see the supplemented value.
        for supp_key, supp_val in list(extra.items()):
            if supp_key.endswith("_supplemented") and supp_val:
                base_key = supp_key[:-len("_supplemented")]
                if not extra.get(base_key):
                    extra[base_key] = supp_val
        # NOTE: body_text_compressed is NOT decompressed here — it's expensive
        # (zlib.decompress on every node fetch). Use get_body_text(nid) when
        # you actually need the function body. Most queries (race detection,
        # explore-flow, trace-chain) don't need body_text.
        return {
            "name": row_dict.get("name", ""),
            "source_file": row_dict.get("source_file", ""),
            "line": row_dict.get("line_number", 0) or 0,
            "domain": row_dict.get("domain", "root"),
            "labels": labels,
            "labels_source": extra.get("labels_source", {l: "ast" for l in labels}),
            "is_empty": extra.get("is_empty", False),
            "condition": extra.get("condition", ""),
            "api_constraints": extra.get("api_constraints", ""),
            "external_desc": extra.get("external_desc", ""),
            "semantic_desc": extra.get("semantic_desc", ""),
            "body_text": "",  # lazy — use get_body_text(nid) to fetch
            "signature": row_dict.get("signature", ""),
            "params": extra.get("params", []),
            "local_vars": extra.get("local_vars", []),
            "callee_args": extra.get("callee_args", []),
            "condition_vars": extra.get("condition_vars", []),
            "preproc_alive": extra.get("preproc_alive", True),
            "node_type": extra.get("node_type", ""),
            "thread_model": extra.get("thread_model"),
            "thread_entry": extra.get("thread_entry", False),
            "thread_model_inherited": extra.get("thread_model_inherited"),
            "globals_read": extra.get("globals_read", []),
            "globals_written": extra.get("globals_written", []),
            "fields_read": extra.get("fields_read", []),
            "fields_written": extra.get("fields_written", []),
            "language": extra.get("language", ""),
            "reg_transfers": extra.get("reg_transfers", []),
            "reg_state_final": extra.get("reg_state_final", {}),
            "goto_jumps": extra.get("goto_jumps", []),
            "goto_labels": extra.get("goto_labels", []),
            "stale": extra.get("stale", False),
            # Deficiency 8: invariants (preconditions/postconditions/loop_invariants
            # + state machine) — extracted by invariants.py and stored in extra_json
            # alongside other LLM-supplementable fields.
            "preconditions": extra.get("preconditions", []),
            "postconditions": extra.get("postconditions", []),
            "loop_invariants": extra.get("loop_invariants", []),
            "state_machine": extra.get("state_machine"),
            "_invariant_meta": extra.get("_invariant_meta"),
        }

    @property
    def nodes(self):
        """NetworkX-compatible nodes accessor.

        Supports both G.nodes (returns _LazyNodeView) and G.nodes(data=True)
        (returns iterator). The _LazyNodeView supports G.nodes[nid] -> dict,
        G.nodes(data=True), iter(G.nodes), len(G.nodes), and `nid in G.nodes`.

        Note: This is a property that returns a _LazyNodeView. The view itself
        is callable with data=... and supports __getitem__/__iter__/__len__.
        """
        return _LazyNodeView(self)

    def has_edge(self, u: str, v: str) -> bool:
        cache_key = (u, v)
        if cache_key in self._edge_cache:
            return self._edge_cache[cache_key] is not None
        row = self._conn.execute(
            "SELECT 1 FROM edges WHERE invoker_id=? AND invoked_id=? LIMIT 1",
            (u, v)).fetchone()
        exists = row is not None
        if not exists:
            if len(self._edge_cache) >= self._edge_cache_max:
                evict_count = self._edge_cache_max // 4
                for k in list(self._edge_cache.keys())[:evict_count]:
                    del self._edge_cache[k]
            self._edge_cache[cache_key] = None
        return exists

    def get_body_text(self, nid: str) -> str:
        """Lazily decompress and return body_text for a node.

        Use this instead of ndata['body_text'] when you need the actual
        function body. The cached attrs from _fetch_node store body_text=''
        to avoid zlib.decompress cost on every node fetch.
        """
        import zlib
        row = self._conn.execute(
            "SELECT body_text_compressed FROM functions WHERE id=?",
            (nid,)).fetchone()
        if not row:
            return ""
        blob = row[0]
        if not blob:
            return ""
        try:
            return zlib.decompress(blob).decode("utf-8", errors="replace")
        except Exception:
            return ""

    def get_edge_data(self, u: str, v: str) -> dict:
        cache_key = (u, v)
        if cache_key in self._edge_cache:
            return self._edge_cache[cache_key] or {}
        import json as _json
        row = self._conn.execute(
            "SELECT * FROM edges WHERE invoker_id=? AND invoked_id=? LIMIT 1",
            (u, v)).fetchone()
        if not row:
            if len(self._edge_cache) >= self._edge_cache_max:
                evict_count = self._edge_cache_max // 4
                for k in list(self._edge_cache.keys())[:evict_count]:
                    del self._edge_cache[k]
            self._edge_cache[cache_key] = None
            return {}
        row_dict = dict(row)
        evidence = []
        ev_raw = row_dict.get("evidence")
        if ev_raw:
            try:
                evidence = _json.loads(ev_raw) if isinstance(ev_raw, str) else ev_raw
            except (_json.JSONDecodeError, TypeError):
                pass
        attrs = {
            "call_order": row_dict.get("call_order"),
            "call_condition": row_dict.get("call_condition", "") or "",
            "concurrency": row_dict.get("concurrency", "") or "",
            "confidence": row_dict.get("confidence", "EXTRACTED") or "EXTRACTED",
            "confidence_score": row_dict.get("confidence_score", 1.0) or 1.0,
            "source": row_dict.get("source", "ast") or "ast",
            "evidence": evidence,
            "relation": row_dict.get("relation", "INVOKES") or "INVOKES",
            "vtable_type": row_dict.get("vtable_type", "") or "",
            "vtable_bound_module": row_dict.get("vtable_bound_module", "") or "",
        }
        if len(self._edge_cache) >= self._edge_cache_max:
            evict_count = self._edge_cache_max // 4
            for k in list(self._edge_cache.keys())[:evict_count]:
                del self._edge_cache[k]
        self._edge_cache[cache_key] = attrs
        return attrs

    def number_of_nodes(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]

    def number_of_edges(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    def degree(self, node_id: str) -> int:
        in_d = len(list(self.predecessors(node_id)))
        out_d = len(list(self.successors(node_id)))
        return in_d + out_d

    def predecessors(self, node_id: str):
        cached = self._pred_cache.get(node_id)
        if cached is not None:
            yield from cached
            return
        cur = self._conn.execute(
            "SELECT invoker_id FROM edges WHERE invoked_id=? "
            "AND relation NOT IN ('CONTAINS', 'IMPORTS')",
            (node_id,))
        result = [row[0] for row in cur]
        if len(self._pred_cache) >= self._pred_cache_max:
            evict = self._pred_cache_max // 4
            for k in list(self._pred_cache.keys())[:evict]:
                del self._pred_cache[k]
        self._pred_cache[node_id] = result
        yield from result

    def successors(self, node_id: str):
        cached = self._succ_cache.get(node_id)
        if cached is not None:
            yield from cached
            return
        cur = self._conn.execute(
            "SELECT invoked_id FROM edges WHERE invoker_id=? "
            "AND relation NOT IN ('CONTAINS', 'IMPORTS')",
            (node_id,))
        result = [row[0] for row in cur]
        if len(self._succ_cache) >= self._succ_cache_max:
            evict = self._succ_cache_max // 4
            for k in list(self._succ_cache.keys())[:evict]:
                del self._succ_cache[k]
        self._succ_cache[node_id] = result
        yield from result

    def in_edges(self, node_id: str, data: bool = False):
        if data:
            cur = self._conn.execute(
                "SELECT invoker_id, invoked_id, call_order, call_condition, "
                "concurrency, confidence, confidence_score, source, evidence, "
                "relation FROM edges WHERE invoked_id=?", (node_id,))
            import json as _json
            for row in cur:
                row_dict = dict(row)
                evidence = []
                ev_raw = row_dict.get("evidence")
                if ev_raw:
                    try:
                        evidence = _json.loads(ev_raw) if isinstance(ev_raw, str) else ev_raw
                    except (_json.JSONDecodeError, TypeError):
                        pass
                attrs = {
                    "call_order": row_dict.get("call_order"),
                    "call_condition": row_dict.get("call_condition", "") or "",
                    "concurrency": row_dict.get("concurrency", "") or "",
                    "confidence": row_dict.get("confidence", "EXTRACTED") or "EXTRACTED",
                    "confidence_score": row_dict.get("confidence_score", 1.0) or 1.0,
                    "source": row_dict.get("source", "ast") or "ast",
                    "evidence": evidence,
                    "relation": row_dict.get("relation", "INVOKES") or "INVOKES",
                }
                yield (row_dict["invoker_id"], row_dict["invoked_id"], attrs)
        else:
            cur = self._conn.execute(
                "SELECT invoker_id, invoked_id FROM edges WHERE invoked_id=?",
                (node_id,))
            for row in cur:
                yield (row[0], row[1])

    def out_edges(self, node_id: str, data: bool = False):
        if data:
            cur = self._conn.execute(
                "SELECT invoker_id, invoked_id, call_order, call_condition, "
                "concurrency, confidence, confidence_score, source, evidence, "
                "relation FROM edges WHERE invoker_id=?", (node_id,))
            import json as _json
            for row in cur:
                row_dict = dict(row)
                evidence = []
                ev_raw = row_dict.get("evidence")
                if ev_raw:
                    try:
                        evidence = _json.loads(ev_raw) if isinstance(ev_raw, str) else ev_raw
                    except (_json.JSONDecodeError, TypeError):
                        pass
                attrs = {
                    "call_order": row_dict.get("call_order"),
                    "call_condition": row_dict.get("call_condition", "") or "",
                    "concurrency": row_dict.get("concurrency", "") or "",
                    "confidence": row_dict.get("confidence", "EXTRACTED") or "EXTRACTED",
                    "confidence_score": row_dict.get("confidence_score", 1.0) or 1.0,
                    "source": row_dict.get("source", "ast") or "ast",
                    "evidence": evidence,
                    "relation": row_dict.get("relation", "INVOKES") or "INVOKES",
                }
                yield (row_dict["invoker_id"], row_dict["invoked_id"], attrs)
        else:
            cur = self._conn.execute(
                "SELECT invoker_id, invoked_id FROM edges WHERE invoker_id=?",
                (node_id,))
            for row in cur:
                yield (row[0], row[1])

    def edges(self, data: bool = False):
        if data:
            cur = self._conn.execute(
                "SELECT invoker_id, invoked_id, call_order, call_condition, "
                "concurrency, confidence, confidence_score, source, evidence, "
                "relation FROM edges")
            import json as _json
            for row in cur:
                row_dict = dict(row)
                evidence = []
                ev_raw = row_dict.get("evidence")
                if ev_raw:
                    try:
                        evidence = _json.loads(ev_raw) if isinstance(ev_raw, str) else ev_raw
                    except (_json.JSONDecodeError, TypeError):
                        pass
                attrs = {
                    "call_order": row_dict.get("call_order"),
                    "call_condition": row_dict.get("call_condition", "") or "",
                    "concurrency": row_dict.get("concurrency", "") or "",
                    "confidence": row_dict.get("confidence", "EXTRACTED") or "EXTRACTED",
                    "confidence_score": row_dict.get("confidence_score", 1.0) or 1.0,
                    "source": row_dict.get("source", "ast") or "ast",
                    "evidence": evidence,
                    "relation": row_dict.get("relation", "INVOKES") or "INVOKES",
                }
                yield (row_dict["invoker_id"], row_dict["invoked_id"], attrs)
        else:
            cur = self._conn.execute(
                "SELECT invoker_id, invoked_id FROM edges")
            for row in cur:
                yield (row[0], row[1])

    def add_node(self, *args, **kwargs):
        raise NotImplementedError("LazySQLiteGraph is read-only")

    def add_edge(self, *args, **kwargs):
        raise NotImplementedError("LazySQLiteGraph is read-only")

    def remove_node(self, *args, **kwargs):
        raise NotImplementedError("LazySQLiteGraph is read-only")

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass
        try:
            self._store.close()
        except Exception:
            pass


class _LazyNodeView:
    """Helper for G.nodes[nid] access pattern, also callable with data=True.

    Supports:
      - G.nodes[nid] -> dict (node attrs)
      - G.nodes(data=True) -> iterator of (nid, attrs) tuples
      - G.nodes(data=False) -> iterator of nids
      - iter(G.nodes) -> iterator of nids
      - len(G.nodes) -> int
      - nid in G.nodes -> bool
    """

    def __init__(self, graph: 'LazySQLiteGraph'):
        self._graph = graph

    def __getitem__(self, nid: str) -> dict:
        return self._graph._get_node_attrs(nid)

    def __call__(self, data: bool = False):
        if data:
            # Single SELECT * — avoids N+1 query pattern. For 1.5M nodes,
            # this is the difference between ~30s and >180s timeout.
            cur = self._graph._conn.execute("SELECT * FROM functions")
            for row in cur:
                row_dict = dict(row)
                nid = row_dict.get("id", "")
                if not nid:
                    continue
                # Use _build_attrs_from_row directly (no cache) — batch
                # iteration is one-shot, caching 1.5M nodes would OOM.
                yield (nid, self._graph._build_attrs_from_row(row_dict))
        else:
            cur = self._graph._conn.execute("SELECT id FROM functions")
            for row in cur:
                yield row[0]

    def __iter__(self):
        cur = self._graph._conn.execute("SELECT id FROM functions")
        for row in cur:
            yield row[0]

    def __len__(self):
        return self._graph.number_of_nodes()

    def __contains__(self, nid):
        return nid in self._graph

