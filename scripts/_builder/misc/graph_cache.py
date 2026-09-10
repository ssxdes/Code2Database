"""Graph cache for Web UI — split from web_ui.py.

In-memory cache of the loaded graph + indices, with thread-safe
refresh on demand.
"""

import json
import os
import sys
import threading
import logging
from collections import defaultdict, deque
from typing import Optional, List, Dict, Any, Set, Tuple

from _builder.utils import normalize_str_field

_log = logging.getLogger(__name__)

class GraphCache:
    """In-memory cache of the loaded graph + indices.

    The web UI does many small queries per second (one per click); we
    don't want to hit the disk for each. Cache the graph and pre-build
    neighbor indices.
    """

    def __init__(self, graph_dir: str):
        self.graph_dir = graph_dir
        self.G = None
        self._community_of: Dict[str, str] = {}
        self._communities: Dict[str, List[str]] = defaultdict(list)
        self._name_to_id: Dict[str, str] = {}
        self._lock = threading.RLock()
        self._freshness = None
        self._freshness_ts = 0.0
        self._degrees: Dict[str, int] = {}
        self._in_deg: Dict[str, int] = {}
        self._out_deg: Dict[str, int] = {}
        self.reload()

    def freshness(self) -> Optional[Dict]:
        """Source-vs-graph freshness (staleness badge).

        Walks the source tree, so cached 10s — enough for a page load
        or two, cheap enough not to matter. None when the check itself
        is unavailable (degrades silently in the UI).
        """
        import time
        now = time.time()
        with self._lock:
            if (self._freshness is not None
                    and now - self._freshness_ts < 10.0):
                return self._freshness
        try:
            from _builder.cgdb.cgdb_freshness import check_freshness
            from _builder.utils import resolve_source_root
            # Derive source_root from code2database_master.json (the build
            # wrote the actual source path), not from the parent dir of
            # graph_dir — a relocated graph dir would otherwise flag every
            # manifest file as 'deleted'. Mirrors session-init's resolution.
            src_root = resolve_source_root(self.graph_dir)
            fr = check_freshness(self.graph_dir, src_root, use_cache=False)
            slim = {
                "is_fresh": fr.get("is_fresh", True),
                "staleness_ratio": fr.get("staleness_ratio", 0.0),
                "changed_count": fr.get("changed_count", 0),
                "new_count": fr.get("new_count", 0),
                "deleted_count": fr.get("deleted_count", 0),
                "git_head_changed": fr.get("git_head_changed", False),
                "recommendation": fr.get("recommendation", ""),
            }
        except Exception:
            slim = None
        with self._lock:
            self._freshness = slim
            self._freshness_ts = now
        return slim

    def reload(self):
        """Reload the graph from disk (e.g., after a daemon update)."""
        with self._lock:
            try:
                from _builder.graph.graph_build import _load_full_graph
            except ImportError:
                sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
                from _builder.graph.graph_build import _load_full_graph
            self.G = _load_full_graph(self.graph_dir)
            # Build indices
            self._community_of = {}
            self._communities = defaultdict(list)
            self._name_to_id = {}
            self._freshness = None
            self._freshness_ts = 0.0
            comm_of, comm_labels = self._load_community_map()
            self._comm_labels = comm_labels
            for nid, nd in self.G.nodes(data=True):
                if nd.get("is_empty", False) or nd.get("node_type") == "file":
                    continue
                # Prefer build-time community detection (Leiden merges
                # related domains); fall back to the source domain.
                comm = comm_of.get(nid) or nd.get("domain", "root")
                self._community_of[nid] = comm
                self._communities[comm].append(nid)
                name = nd.get("name", "")
                if name:
                    self._name_to_id[name.lower()] = nid
            self._compute_degrees()

    def _load_community_map(self):
        """Load .code2database_communities.json (Leiden output).

        The build pipeline computes cross-domain communities and writes
        this file — nothing ever read it; the UI presented source-file
        domains as "communities" instead. Returns (node_community,
        community_labels); ({}, {}) when the file is absent/corrupt,
        which restores the domain fallback.
        """
        path = os.path.join(self.graph_dir, ".code2database_communities.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}, {}
        nc = data.get("node_community")
        if nc is None and data.get("node_community_list"):
            # >100K-node stream variant: [nid, comm_id] pairs
            nc = {nid: cid for nid, cid in data["node_community_list"]}
        labels = {}
        for c in data.get("communities", []):
            cid = c.get("id")
            if cid:
                labels[cid] = (c.get("label") or c.get("heuristic_label")
                               or str(cid))
        return (nc or {}), labels

    def _compute_degrees(self):
        """Precompute the degree maps once per reload.

        /api/degrees used to recompute every node's degree on every
        request: O(N+E) on the eager backend, but on the lazy SQLite
        backend each "unit" is an indexed SQL query + JSON parse
        (~3N+2E queries per request), all while holding the global
        GraphCache lock — so every node click froze the whole UI. The
        lazy backend is served by two GROUP BY queries instead.
        """
        self._degrees = {}
        in_deg: Dict[str, int] = {}
        out_deg: Dict[str, int] = {}
        lazy = False
        try:
            from _builder.graph.streaming_graph import LazySQLiteGraph
            lazy = isinstance(self.G, LazySQLiteGraph)
        except Exception:
            lazy = False
        if lazy:
            try:
                conn = self.G._conn
                for row in conn.execute(
                        "SELECT invoker_id, COUNT(*) FROM edges "
                        "WHERE relation IS NOT NULL "
                        "  AND relation NOT IN ('CONTAINS','IMPORTS') "
                        "GROUP BY invoker_id"):
                    out_deg[row[0]] = row[1]
                for row in conn.execute(
                        "SELECT invoked_id, COUNT(*) FROM edges "
                        "WHERE relation IS NOT NULL "
                        "  AND relation NOT IN ('CONTAINS','IMPORTS') "
                        "GROUP BY invoked_id"):
                    in_deg[row[0]] = row[1]
                empty = {row[0] for row in conn.execute(
                    "SELECT id FROM functions WHERE is_empty = 1")}
                for nid in self.G.nodes():
                    if nid in empty:
                        continue
                    self._degrees[nid] = (in_deg.get(nid, 0)
                                          + out_deg.get(nid, 0))
                self._in_deg = in_deg
                self._out_deg = out_deg
                return
            except Exception:
                # Schema drift / unexpected shape: fall through to the
                # generic graph pass below (slower on lazy, still right).
                self._degrees = {}
                in_deg, out_deg = {}, {}
        for nid in self.G.nodes():
            if self.G.nodes[nid].get("is_empty", False):
                continue
            self._degrees[nid] = 0
        for u, v, ed in self.G.edges(data=True):
            if (ed or {}).get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            out_deg[u] = out_deg.get(u, 0) + 1
            in_deg[v] = in_deg.get(v, 0) + 1
            if u in self._degrees:
                self._degrees[u] += 1
            if v in self._degrees:
                self._degrees[v] += 1
        self._in_deg = in_deg
        self._out_deg = out_deg

    def summary(self) -> Dict:
        """High-level graph summary for the UI's initial load."""
        with self._lock:
            node_count = sum(1 for _, d in self.G.nodes(data=True)
                             if not d.get("is_empty", False)
                             and d.get("node_type") != "file")
            edge_count = sum(1 for _, _, d in self.G.edges(data=True)
                             if d.get("relation") == "INVOKES")
            ffi_count = sum(1 for _, _, d in self.G.edges(data=True)
                            if d.get("relation") == "FFI")
            communities = []
            for comm, nodes in self._communities.items():
                communities.append({
                    "id": comm, "node_count": len(nodes),
                    "label": self._comm_labels.get(comm, ""),
                    "sample_names": [
                        self.G.nodes[n].get("name", n) for n in nodes[:5]
                    ],
                })
            return {
                "node_count": node_count,
                "edge_count": edge_count,
                "ffi_edge_count": ffi_count,
                "community_count": len(self._communities),
                "communities": sorted(communities,
                                      key=lambda c: -c["node_count"])[:50],
            }

    def get_node(self, node_id: str) -> Optional[Dict]:
        """Get a single node's details."""
        with self._lock:
            if node_id not in self.G:
                return None
            nd = self.G.nodes[node_id]
            return {
                "id": node_id,
                "name": nd.get("name", ""),
                "domain": nd.get("domain", ""),
                "labels": nd.get("labels", []),
                "source_file": nd.get("source_file", ""),
                "line": nd.get("line", 0),
                "location": f'{nd.get("source_file", "")}:{nd.get("line", 0)}',
                "signature": nd.get("signature", ""),
                "semantic_desc": nd.get("semantic_desc", ""),
                "external_desc": nd.get("external_desc", ""),
                "api_constraints": normalize_str_field(nd.get("api_constraints", "")),
                "is_empty": nd.get("is_empty", False),
            }

    def neighbors(self, node_id: str, depth: int = 1, max_nodes: int = 200) -> Dict:
        """Get neighbors within depth N (BFS). Returns nodes + edges for rendering.

        For depth >= 2 we cap at max_nodes to keep the UI responsive.
        """
        with self._lock:
            if node_id not in self.G:
                return {"error": "node not found"}
            visited = {node_id}
            queue = deque([(node_id, 0)])
            nodes = []
            edges = []
            seen_edges: Set[Tuple[str, str]] = set()
            while queue and len(nodes) < max_nodes:
                cur, d = queue.popleft()
                if d >= depth:
                    continue
                cur_nd = self.G.nodes[cur]
                nodes.append({
                    "id": cur, "name": cur_nd.get("name", cur),
                    "domain": cur_nd.get("domain", ""),
                    "community": self._community_of.get(cur, ""),
                    "labels": cur_nd.get("labels", []),
                    "is_focused": cur == node_id,
                    "depth": d,
                })
                # Forward edges (callees)
                for succ in self.G.successors(cur):
                    ed = self.G.get_edge_data(cur, succ) or {}
                    rel = ed.get("relation", "INVOKES")
                    if rel == "CONTAINS":
                        continue
                    if (cur, succ) not in seen_edges:
                        seen_edges.add((cur, succ))
                        edges.append({
                            "source": cur, "target": succ,
                            "relation": rel,
                            "call_order": ed.get("call_order"),
                            "call_condition": ed.get("call_condition", ""),
                        })
                    if succ not in visited:
                        visited.add(succ)
                        queue.append((succ, d + 1))
                # Reverse edges (callers)
                for pred in self.G.predecessors(cur):
                    ed = self.G.get_edge_data(pred, cur) or {}
                    rel = ed.get("relation", "INVOKES")
                    if rel == "CONTAINS":
                        continue
                    if (pred, cur) not in seen_edges:
                        seen_edges.add((pred, cur))
                        edges.append({
                            "source": pred, "target": cur,
                            "relation": rel,
                            "call_order": ed.get("call_order"),
                            "call_condition": ed.get("call_condition", ""),
                        })
                    if pred not in visited:
                        visited.add(pred)
                        queue.append((pred, d + 1))
            return {"focus": node_id, "depth": depth,
                    "nodes": nodes, "edges": edges,
                    "truncated": len(nodes) >= max_nodes}

    def shortest_path(self, from_id: str, to_id: str, max_depth: int = 10) -> Dict:
        """BFS shortest path from from_id to to_id (call edges only)."""
        with self._lock:
            if from_id not in self.G or to_id not in self.G:
                return {"error": "node not found"}
            if from_id == to_id:
                return {"path": [from_id], "length": 1}
            visited = {from_id}
            queue = deque([(from_id, [from_id])])
            while queue:
                cur, path = queue.popleft()
                if len(path) >= max_depth:
                    continue
                for succ in self.G.successors(cur):
                    ed = self.G.get_edge_data(cur, succ) or {}
                    if ed.get("relation") not in ("INVOKES", "FFI"):
                        continue
                    if succ in visited:
                        continue
                    new_path = path + [succ]
                    if succ == to_id:
                        return {"path": new_path, "length": len(new_path)}
                    visited.add(succ)
                    queue.append((succ, new_path))
            return {"error": "no path found", "from": from_id, "to": to_id}

    def list_communities(self) -> List[Dict]:
        """List all communities (for LOD zoom-out view)."""
        with self._lock:
            out = []
            for comm, nodes in self._communities.items():
                out.append({
                    "id": comm, "node_count": len(nodes),
                    "sample_names": [
                        self.G.nodes[n].get("name", n) for n in nodes[:5]
                    ],
                })
            return sorted(out, key=lambda c: -c["node_count"])

    def community_nodes(self, comm_id: str, limit: int = 100) -> Dict:
        """List nodes in a community."""
        with self._lock:
            nodes = self._communities.get(comm_id, [])
            return {
                "community": comm_id,
                "node_count": len(nodes),
                "nodes": [
                    {"id": n, "name": self.G.nodes[n].get("name", n),
                     "labels": self.G.nodes[n].get("labels", [])}
                    for n in nodes[:limit]
                ],
            }

    def search(self, query: str, limit: int = 30) -> List[Dict]:
        """Search nodes by name (case-insensitive substring)."""
        with self._lock:
            query_lower = query.lower()
            results = []
            exact_id = self._name_to_id.get(query_lower)
            if exact_id:
                nd = self.G.nodes[exact_id]
                results.append({"id": exact_id,
                                "name": nd.get("name", ""),
                                "domain": nd.get("domain", ""),
                                "labels": nd.get("labels", []),
                                "source_file": nd.get("source_file", ""),
                                "line": nd.get("line", 0),
                                "score": 100})
            for nid, nd in self.G.nodes(data=True):
                if nd.get("is_empty", False) or nd.get("node_type") == "file":
                    continue
                name = nd.get("name", "")
                if not name:
                    continue
                if query_lower in name.lower() and nid != exact_id:
                    score = 50 if name.lower().startswith(query_lower) else 30
                    results.append({"id": nid, "name": name,
                                    "domain": nd.get("domain", ""),
                                    "labels": nd.get("labels", []),
                                    "source_file": nd.get("source_file", ""),
                                    "line": nd.get("line", 0),
                                    "score": score})
                    if len(results) >= limit:
                        break
            results.sort(key=lambda r: -r["score"])
            return results[:limit]

    def get_code_snippet(self, node_id: str, context_lines: int = 10) -> str:
        """Return source code around a function node."""
        payload = self.get_code_payload(node_id, context_lines)
        return payload.get("code", "")

    def get_code_payload(self, node_id: str, context_lines: int = 10) -> Dict:
        """Return source code around a function node with file + line metadata.

        The page-in code panel uses ``file``/``line`` to title the panel
        so the user can jump to the same location in an editor. The
        legacy ``get_code_snippet`` is preserved as a thin wrapper so
        existing callers (incl. tests) keep working.
        """
        with self._lock:
            if node_id not in self.G:
                return {"code": "", "file": "", "line": 0}
            nd = self.G.nodes[node_id]
            source_file = nd.get("source_file", "")
            line = nd.get("line", 0)
            if not source_file or not line:
                return {"code": nd.get("body_text", "")[:2000],
                        "file": source_file, "line": line}
            try:
                with open(source_file, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                start = max(0, line - context_lines - 1)
                end = min(len(lines), line + context_lines)
                snippet = "".join(lines[start:end])
                return {"code": snippet[:4000], "file": source_file, "line": line}
            except OSError:
                return {"code": nd.get("body_text", "")[:2000],
                        "file": source_file, "line": line}

    def list_domains(self) -> List[Dict]:
        """Return list of domains with node/edge counts."""
        with self._lock:
            domain_nodes: Dict[str, int] = defaultdict(int)
            domain_edges: Dict[str, int] = defaultdict(int)
            for nid, nd in self.G.nodes(data=True):
                if nd.get("is_empty", False):
                    continue
                dom = nd.get("domain", "root")
                domain_nodes[dom] += 1
            for u, v, ed in self.G.edges(data=True):
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                u_dom = self.G.nodes[u].get("domain", "root") if u in self.G else "?"
                v_dom = self.G.nodes[v].get("domain", "root") if v in self.G else "?"
                if u_dom == v_dom:
                    domain_edges[u_dom] += 1
                else:
                    domain_edges[f"{u_dom}→{v_dom}"] += 1
            return sorted(
                [{"domain": dom, "nodes": cnt, "internal_edges": domain_edges.get(dom, 0)}
                 for dom, cnt in domain_nodes.items()],
                key=lambda d: -d["nodes"]
            )

    def impact_analysis(self, node_id: str, max_depth: int = 5) -> Dict:
        """Reverse-reachability analysis: who calls this function?"""
        with self._lock:
            if node_id not in self.G:
                return {"error": "node not found"}
            affected = []
            visited = {node_id}
            queue = deque([(node_id, 0)])
            while queue:
                # popleft: list.pop(0) made wide BFS O(V^2)
                cur, depth = queue.popleft()
                if depth >= max_depth:
                    continue
                for pred in self.G.predecessors(cur):
                    if pred in visited:
                        continue
                    ed = self.G.get_edge_data(pred, cur) or {}
                    if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                        # Do NOT mark visited here — the caller may also
                        # reach this node through a call edge on another
                        # path, and must still be reported then.
                        continue
                    visited.add(pred)
                    nd = self.G.nodes[pred]
                    affected.append({
                        "id": pred,
                        "name": nd.get("name", ""),
                        "domain": nd.get("domain", ""),
                        "depth": depth + 1,
                        "source_file": nd.get("source_file", ""),
                    })
                    queue.append((pred, depth + 1))
            return {
                "node_id": node_id,
                "affected_count": len(affected),
                "affected": affected[:200],
                "truncated": len(affected) > 200,
                "max_depth": max_depth,
            }

    # --- callers/callees, cycle detection, degree ---

    def get_callers(self, node_id: str) -> List[Dict]:
        """Direct callers of a node (incoming call edges)."""
        with self._lock:
            if node_id not in self.G:
                return []
            callers = []
            for pred in self.G.predecessors(node_id):
                ed = self.G.get_edge_data(pred, node_id) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                nd = self.G.nodes[pred]
                callers.append({
                    "id": pred, "name": nd.get("name", pred),
                    "domain": nd.get("domain", ""),
                    "source_file": nd.get("source_file", ""),
                    "line": nd.get("line", 0),
                    "call_order": ed.get("call_order"),
                    "call_condition": ed.get("call_condition", ""),
                    "confidence": ed.get("confidence", "EXTRACTED"),
                })
            return sorted(callers, key=lambda c: c.get("call_order") or 0)

    def get_callees(self, node_id: str) -> List[Dict]:
        """Direct callees of a node (outgoing call edges)."""
        with self._lock:
            if node_id not in self.G:
                return []
            callees = []
            for succ in self.G.successors(node_id):
                ed = self.G.get_edge_data(node_id, succ) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                nd = self.G.nodes[succ]
                callees.append({
                    "id": succ, "name": nd.get("name", succ),
                    "domain": nd.get("domain", ""),
                    "source_file": nd.get("source_file", ""),
                    "line": nd.get("line", 0),
                    "call_order": ed.get("call_order"),
                    "call_condition": ed.get("call_condition", ""),
                    "confidence": ed.get("confidence", "EXTRACTED"),
                })
            return sorted(callees, key=lambda c: c.get("call_order") or 0)

    def get_node_degree(self, node_id: str) -> Dict:
        """In-degree + out-degree for node sizing (precomputed at reload)."""
        with self._lock:
            if node_id not in self.G:
                return {"in_degree": 0, "out_degree": 0}
            in_deg = self._in_deg.get(node_id, 0)
            out_deg = self._out_deg.get(node_id, 0)
            return {"in_degree": in_deg, "out_degree": out_deg,
                    "total": in_deg + out_deg}

    def detect_cycles(self, limit: int = 50) -> List[Dict]:
        """Find cyclic call edges (A→B where B can reach A)."""
        # Snapshot the call-edge adjacency ONCE under the lock, then run
        # the per-edge depth-5 BFS outside it. The old code held self._lock
        # for the entire scan: on a large acyclic graph it walks every edge
        # with a branching^5 BFS each, blocking every other endpoint
        # (including /api/graph/summary needed at page load) for minutes.
        with self._lock:
            succ = {}
            edges = []
            for u, v, ed in self.G.edges(data=True):
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                edges.append((u, v))
                succ.setdefault(u, []).append(v)
        cycles = []
        count = 0
        for u, v in edges:
            if count >= limit:
                break
            # Quick check: does v have a path back to u?
            if u == v:
                cycles.append({"source": u, "target": v, "type": "self_loop"})
                count += 1
                continue
            # BFS from v to find u (depth-limited to 5)
            visited = {v}
            queue = deque([(v, 0)])
            found = False
            while queue and not found:
                cur, d = queue.popleft()
                if d >= 5:
                    continue
                for s in succ.get(cur, ()):
                    if s == u:
                        found = True
                        break
                    if s not in visited:
                        visited.add(s)
                        queue.append((s, d + 1))
            if found:
                cycles.append({"source": u, "target": v, "type": "cycle"})
                count += 1
        return cycles

    # --- project context (brief + memory) for humans ---

    def brief(self) -> Dict:
        """The project brief: rendered prompt form + structured JSON."""
        from _builder.kb.brief import load_brief, render_brief_prompt
        brief = load_brief(self.graph_dir)
        if brief is None:
            return {"brief": None, "rendered": "", "missing": True}
        return {"brief": brief,
                "rendered": render_brief_prompt(self.graph_dir, brief),
                "missing": False}

    def memory_search(self, query: str, top: int = 10,
                      author: str = "", symbol: str = "") -> Dict:
        """Search the shared memory store (veteran Q&A).

        Empty query returns the weight-ranked digest — what a newcomer
        should read first. Read-only: the UI never mutates the shared
        store (no access-counter bumps, no dir/db creation).
        """
        from _builder.memory.memory_store import MemoryStore
        if not os.path.exists(os.path.join(self.graph_dir, "memory",
                                           "memory.db")):
            return {"results": [], "stats": {}}
        store = MemoryStore(self.graph_dir, read_only=True)
        if symbol:
            # symbol grounding: exact match against the symbols column
            results = store.search(query or symbol, top_n=top,
                                   author=author or None, symbol=symbol)
        elif query and query.strip():
            results = store.search(query, top_n=top,
                                   author=author or None)
        else:
            results = store.digest(limit=top, author=author or None)
        try:
            stats = store.stats()
        except Exception:
            stats = {}
        return {"results": results, "stats": stats}

    def memories_for_node(self, node: Dict) -> list:
        """Veteran Q&A grounded to this node's symbol (top 3).

        The "pitfalls of this function" view: memories whose symbols
        list contains the node name, ranked by weight. Read-only,
        degrades to [] when no store/column exists.
        """
        from _builder.memory.memory_store import MemoryStore
        name = (node or {}).get("name") or ""
        if not name:
            return []
        if not os.path.exists(os.path.join(self.graph_dir, "memory",
                                           "memory.db")):
            return []
        try:
            store = MemoryStore(self.graph_dir, read_only=True)
            return store.entries_for_symbol(name, top=3)
        except Exception:
            logging.getLogger(__name__).debug("silent exception",
                                              exc_info=True)
            return []

    def memory_lineage(self) -> Dict:
        """The memory governance lineage graph (split/merge/variant)."""
        from _builder.memory.memory_store import MemoryStore
        if not os.path.exists(os.path.join(self.graph_dir, "memory",
                                           "memory.db")):
            return {"nodes": [], "edges": []}
        return MemoryStore(self.graph_dir, read_only=True).lineage()

    def memory_authors(self) -> Dict:
        """Contributors to the shared memory store (author filter)."""
        from _builder.memory.memory_store import MemoryStore
        if not os.path.exists(os.path.join(self.graph_dir, "memory",
                                           "memory.db")):
            return {"authors": []}
        return {"authors": MemoryStore(
            self.graph_dir, read_only=True).authors()}

    def architecture(self) -> Dict:
        """The ARCHITECTURE_FLOWS.md narrative (written at build time).

        The build already generates a human-readable narrative of the
        core execution flows (API entry → endpoint chains with
        conditions/concurrency/domain crossings); this serves it to the
        UI so a newcomer can read the architecture story without
        opening the output directory.
        """
        path = os.path.join(self.graph_dir, "ARCHITECTURE_FLOWS.md")
        if not os.path.exists(path):
            return {"content": "", "missing": True}
        try:
            with open(path, encoding="utf-8") as f:
                return {"content": f.read(), "missing": False}
        except OSError:
            return {"content": "", "missing": True}

    def get_all_degrees(self) -> Dict[str, int]:
        """Degree map for node sizing (precomputed at reload time)."""
        with self._lock:
            return dict(self._degrees)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

# Single highlight-path per server (module-level; see WebUIHandler note).
_HIGHLIGHT_PATH: List[str] = []


