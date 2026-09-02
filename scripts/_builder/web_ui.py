#!/usr/bin/env python3
"""Interactive Web UI for Code2Database.

Provides a self-contained HTTP server with a built-in graph browser:
- zoom/pan, click-to-focus, neighbor expansion
- path highlighting (POST a path → server returns highlighted subgraph)
- LOD (Level of Detail): when zoomed out, show communities; when zoomed
  in, show individual nodes and edges
- cross-language FFI edges shown in a distinct color

No external JS framework — uses native SVG + a small JS controller,
served from the same HTTP server. The frontend fits in <500 lines of
JS so it's easy to audit and customize.

Why not React Flow? Two reasons:
1. Self-contained: no node_modules, no build step, no CDN dependency.
   The whole UI is one HTML file with inline CSS/JS, served by the
   same Python process that owns the graph. Works offline.
2. Audit-friendly: engineers reading the code can understand the
   rendering logic without learning a framework.

Usage:
    code2database_builder web-ui --graph out/ --port 8765
    # then open http://localhost:8765 in a browser

API endpoints:
    GET  /api/graph/summary            — overall graph stats + community list
    GET  /api/node/<id>                — node details (same as describe-node)
    GET  /api/neighbors/<id>?depth=2   — neighbors within depth N
    GET  /api/path?from=<id>&to=<id>   — shortest path (call edges only)
    POST /api/highlight-path           — highlight a saved path
    GET  /api/communities              — list communities (LOD zoom-out)
    GET  /api/community/<id>           — nodes in a community
    GET  /api/search?q=<name>          — find nodes by name
    GET  /                             — the HTML UI
"""

import json
import os
import sys
import threading
import urllib.parse
import webbrowser
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from typing import Optional, List, Dict, Any, Set, Tuple
import logging


# ---------------------------------------------------------------------------
# Graph cache — load once at server startup, refresh on demand
# ---------------------------------------------------------------------------

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
        self.reload()

    def reload(self):
        """Reload the graph from disk (e.g., after a daemon update)."""
        with self._lock:
            try:
                from _builder.graph_build import _load_full_graph
            except ImportError:
                sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
                from _builder.graph_build import _load_full_graph
            self.G = _load_full_graph(self.graph_dir)
            # Build indices
            self._community_of = {}
            self._communities = defaultdict(list)
            self._name_to_id = {}
            for nid, nd in self.G.nodes(data=True):
                if nd.get("is_empty", False) or nd.get("node_type") == "file":
                    continue
                comm = nd.get("domain", "root")
                self._community_of[nid] = comm
                self._communities[comm].append(nid)
                name = nd.get("name", "")
                if name:
                    self._name_to_id[name.lower()] = nid

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
                "api_constraints": nd.get("api_constraints", ""),
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
                results.append({"id": exact_id,
                                "name": self.G.nodes[exact_id].get("name", ""),
                                "score": 100})
            for nid, nd in self.G.nodes(data=True):
                if nd.get("is_empty", False) or nd.get("node_type") == "file":
                    continue
                name = nd.get("name", "")
                if not name:
                    continue
                if query_lower in name.lower() and nid != exact_id:
                    score = 50 if name.lower().startswith(query_lower) else 30
                    results.append({"id": nid, "name": name, "score": score})
                    if len(results) >= limit:
                        break
            results.sort(key=lambda r: -r["score"])
            return results[:limit]

    def get_code_snippet(self, node_id: str, context_lines: int = 10) -> str:
        """Return source code around a function node."""
        with self._lock:
            if node_id not in self.G:
                return ""
            nd = self.G.nodes[node_id]
            source_file = nd.get("source_file", "")
            line = nd.get("line", 0)
            if not source_file or not line:
                return nd.get("body_text", "")[:2000]
            try:
                with open(source_file, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                start = max(0, line - context_lines - 1)
                end = min(len(lines), line + context_lines)
                snippet = "".join(lines[start:end])
                return snippet[:4000]  # Cap for API response
            except OSError:
                return nd.get("body_text", "")[:2000]

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
            queue = [(node_id, 0)]
            while queue:
                cur, depth = queue.pop(0)
                if depth >= max_depth:
                    continue
                for pred in self.G.predecessors(cur):
                    if pred in visited:
                        continue
                    visited.add(pred)
                    ed = self.G.get_edge_data(pred, cur) or {}
                    if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                        continue
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
        """In-degree + out-degree for node sizing."""
        with self._lock:
            if node_id not in self.G:
                return {"in_degree": 0, "out_degree": 0}
            in_deg = sum(1 for p in self.G.predecessors(node_id)
                         if (self.G.get_edge_data(p, node_id) or {}).get("relation") not in ("CONTAINS", "IMPORTS"))
            out_deg = sum(1 for s in self.G.successors(node_id)
                          if (self.G.get_edge_data(node_id, s) or {}).get("relation") not in ("CONTAINS", "IMPORTS"))
            return {"in_degree": in_deg, "out_degree": out_deg, "total": in_deg + out_deg}

    def detect_cycles(self, limit: int = 50) -> List[Dict]:
        """Find cyclic call edges (A→B where B can reach A)."""
        with self._lock:
            cycles = []
            # Simple approach: for each edge A→B, check if B can reach A via BFS (depth-limited)
            count = 0
            for u, v, ed in self.G.edges(data=True):
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
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
                    for s in self.G.successors(cur):
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

    def get_all_degrees(self) -> Dict[str, int]:
        """Compute degree for all nodes (for node sizing)."""
        with self._lock:
            degrees = {}
            for nid in self.G.nodes():
                if self.G.nodes[nid].get("is_empty", False):
                    continue
                deg = self.get_node_degree(nid)
                degrees[nid] = deg["total"]
            return degrees


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

# Single highlight-path per server (module-level; see WebUIHandler note).
_HIGHLIGHT_PATH: List[str] = []


class WebUIHandler(BaseHTTPRequestHandler):
    """HTTP request handler serving the API + the HTML UI."""

    # Set by the factory function before serve_forever()
    cache: GraphCache = None
    # Path highlighted via POST /api/highlight-path. Module-level (not a
    # class attr with a shared [] default) so a second handler class
    # (e.g., in tests) doesn't silently share state; documented single
    # highlight-path per server.

    def log_message(self, fmt, *args):
        # Suppress default access log (too noisy); route through stderr only
        # for errors.
        pass

    def _send_json(self, status: int, payload: Any):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/" or path == "/index.html":
                self._send_html(_HTML_UI)
                return
            # Serve cytoscape.min.js locally for offline/air-gapped use.
            # Falls back to CDN if the local file is missing (e.g.,
            # the static/ directory wasn't installed).
            if path == "/static/cytoscape.min.js":
                _cy_path = os.path.join(
                    os.path.dirname(__file__), "static", "cytoscape.min.js")
                if os.path.exists(_cy_path):
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "application/javascript; charset=utf-8")
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    with open(_cy_path, "rb") as f:
                        self.wfile.write(f.read())
                    return
                # Local file missing — redirect to CDN
                self.send_response(302)
                self.send_header(
                    "Location",
                    "https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js")
                self.end_headers()
                return
            if path == "/api/graph/summary":
                self._send_json(200, self.cache.summary())
                return
            if path.startswith("/api/node/"):
                node_id = urllib.parse.unquote(path[len("/api/node/"):])
                node = self.cache.get_node(node_id)
                if node:
                    self._send_json(200, node)
                else:
                    self._send_json(404, {"error": "node not found"})
                return
            if path.startswith("/api/neighbors/"):
                node_id = urllib.parse.unquote(path[len("/api/neighbors/"):])
                depth = int(query.get("depth", ["1"])[0])
                self._send_json(200, self.cache.neighbors(node_id, depth))
                return
            if path == "/api/path":
                from_id = query.get("from", [""])[0]
                to_id = query.get("to", [""])[0]
                if not from_id or not to_id:
                    self._send_json(400, {"error": "from and to required"})
                    return
                self._send_json(200, self.cache.shortest_path(from_id, to_id))
                return
            if path == "/api/highlight-path":
                self._send_json(200, {"path": _HIGHLIGHT_PATH})
                return
            if path == "/api/communities":
                self._send_json(200, {"communities": self.cache.list_communities()})
                return
            if path.startswith("/api/community/"):
                comm_id = urllib.parse.unquote(path[len("/api/community/"):])
                self._send_json(200, self.cache.community_nodes(comm_id))
                return
            if path == "/api/search":
                q = query.get("q", [""])[0]
                if not q:
                    self._send_json(400, {"error": "q required"})
                    return
                self._send_json(200, {"results": self.cache.search(q)})
                return
            # --- New endpoints ---
            if path == "/api/code":
                node_id = query.get("node", [""])[0]
                if not node_id:
                    self._send_json(400, {"error": "node required"})
                    return
                code = self.cache.get_code_snippet(node_id)
                self._send_json(200, {"code": code})
                return
            if path == "/api/domains":
                self._send_json(200, {"domains": self.cache.list_domains()})
                return
            if path == "/api/impact":
                node_id = query.get("node", [""])[0]
                if not node_id:
                    self._send_json(400, {"error": "node required"})
                    return
                self._send_json(200, self.cache.impact_analysis(node_id))
                return
            # --- P0/P1 endpoints: callers, callees, cycles, degrees ---
            if path.startswith("/api/callers/"):
                node_id = urllib.parse.unquote(path[len("/api/callers/"):])
                self._send_json(200, {"callers": self.cache.get_callers(node_id)})
                return
            if path.startswith("/api/callees/"):
                node_id = urllib.parse.unquote(path[len("/api/callees/"):])
                self._send_json(200, {"callees": self.cache.get_callees(node_id)})
                return
            if path == "/api/cycles":
                self._send_json(200, {"cycles": self.cache.detect_cycles()})
                return
            if path == "/api/degrees":
                self._send_json(200, {"degrees": self.cache.get_all_degrees()})
                return
            if path == "/api/suggestions":
                try:
                    from _builder.cgdb_suggest import analyze_and_suggest
                    suggestions = analyze_and_suggest(self.cache.graph_dir)
                    self._send_json(200, {"suggestions": suggestions})
                except Exception as exc:
                    logging.getLogger(__name__).warning("suggestions failed", exc_info=True)
                    self._send_json(500, {"suggestions": [], "error": "internal error"})
                return
            if path == "/api/tour":
                try:
                    from _builder.cgdb_tour import generate_tour
                    import tempfile
                    tour_path = generate_tour(self.cache.graph_dir,
                                              output_path=os.path.join(
                                                  tempfile.gettempdir(), "c2d_tour.md"))
                    with open(tour_path, "r", encoding="utf-8") as f:
                        tour_content = f.read()
                    self._send_json(200, {"tour": tour_content})
                except Exception as exc:
                    logging.getLogger(__name__).warning("tour generation failed", exc_info=True)
                    self._send_json(500, {"tour": "", "error": "internal error"})
                return
            self._send_json(404, {"error": f"unknown path {path}"})
        except Exception as exc:
            logging.getLogger(__name__).warning("web_ui handler error", exc_info=True); self._send_json(500, {"error": "internal error"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/highlight-path":
                length = int(self.headers.get("Content-Length", 0))
                # Cap POST body size to prevent memory blow-up / DoS —
                # a malicious or buggy client can send Content-Length:
                # 9999999999 and the server would attempt to allocate
                # ~10GB. 1 MB is plenty for a highlight-path request.
                if length > 1_048_576:
                    self._send_json(413, {"error": "Request body too large (max 1MB)"})
                    return
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body) if body else {}
                # Set on the CLASS (not the instance) so all handlers see it
                _HIGHLIGHT_PATH[:] = data.get("path", [])
                self._send_json(200, {"ok": True, "path": list(_HIGHLIGHT_PATH)})
                return
            if path == "/api/reload":
                self.cache.reload()
                self._send_json(200, {"ok": True, "summary": self.cache.summary()})
                return
            self._send_json(404, {"error": f"unknown path {path}"})
        except Exception as exc:
            logging.getLogger(__name__).warning("web_ui handler error", exc_info=True); self._send_json(500, {"error": "internal error"})

    def do_OPTIONS(self):
        # CORS preflight
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def _make_handler_class(cache: GraphCache):
    """Factory: creates a handler class with the cache bound."""
    cls = type("BoundWebUIHandler", (WebUIHandler,), {"cache": cache})
    return cls


# ---------------------------------------------------------------------------
# HTML UI (single-file, no external deps)
# ---------------------------------------------------------------------------

_HTML_UI = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Code2Database</title>
<style>
/* P2: prefers-reduced-motion */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
}
/* P0: Dark Mode (OLED) + Swiss Style per ui-ux-pro-max row 82 */
:root {
  --bg: #0d1b2a; --fg: #e0e0e0; --card: #16213e; --border: #334155;
  --primary: #4a90e2; --accent: #f59e0b; --danger: #ef4444; --success: #22c55e;
  --muted: #678; --muted-fg: #94a3b8; --ring: #4a90e2;
  --z-toolbar: 10; --z-sidebar: 20; --z-modal: 30; --z-loading: 40;
}
:root.light {
  --bg: #f8fafc; --fg: #1e293b; --card: #ffffff; --border: #e2e8f0;
  --primary: #1e40af; --accent: #d97706; --danger: #dc2626; --success: #16a34a;
  --muted: #64748b; --muted-fg: #475569; --ring: #1e40af;
}
* { box-sizing: border-box; }
body { margin: 0; font-family: "IBM Plex Sans", -apple-system, "Segoe UI", Roboto, sans-serif;
       background: var(--bg); color: var(--fg); font-size: 13px; }
/* P2: Typography — JetBrains Mono for code/identifiers */
code, .mono, #node-details .field-value { font-family: "JetBrains Mono", "Fira Code", monospace; }

/* Skip link for A11y */
.skip-link { position: absolute; top: -40px; left: 0; background: var(--primary); color: #fff;
  padding: 8px 16px; z-index: var(--z-loading); text-decoration: none; }
.skip-link:focus { top: 0; }

/* Topbar */
#topbar { background: var(--card); color: var(--fg); padding: 6px 12px;
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  border-bottom: 1px solid var(--border); }
#topbar h1 { font-size: 14px; margin: 0; font-weight: 600; }
#topbar input { flex: 1; min-width: 180px; padding: 5px 10px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--bg); color: var(--fg); font-size: 13px; }
#topbar input:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }
#topbar button, #topbar select { padding: 5px 10px; background: var(--primary); color: #fff;
  border: none; border-radius: 6px; cursor: pointer; font-size: 12px; transition: all 150ms ease; }
#topbar button:hover { opacity: 0.85; transform: translateY(-1px); }
#topbar button:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
#topbar button:active { transform: translateY(0); }
#topbar button[aria-pressed="true"] { background: var(--accent); }

/* Cytoscape canvas */
#cy { position: absolute; left: 0; top: 42px; right: 320px; bottom: 0; background: var(--bg); }

/* Sidebar */
#sidebar { position: absolute; right: 0; top: 42px; bottom: 0; width: 320px;
  background: var(--card); border-left: 1px solid var(--border); overflow-y: auto;
  padding: 10px; color: var(--muted-fg); }
#sidebar h2 { font-size: 13px; margin: 0 0 6px; color: var(--fg); font-weight: 600; }
#sidebar .field { margin-bottom: 6px; }
#sidebar .field-label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; }
#sidebar .field-value { font-size: 12px; word-break: break-word; overflow-wrap: anywhere; }
#sidebar .action-btns { display: flex; gap: 4px; margin-top: 8px; flex-wrap: wrap; }
.action-btn { padding: 3px 8px; background: var(--primary); color: #fff; border: none;
  border-radius: 4px; cursor: pointer; font-size: 11px; transition: all 150ms ease; }
.action-btn:hover { opacity: 0.85; }
.action-btn:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

/* P0: Callers/Callees lists in sidebar */
.call-list { margin: 6px 0; }
.call-list-title { font-size: 11px; color: var(--muted); text-transform: uppercase; margin-bottom: 3px; }
.call-item { padding: 3px 6px; border-radius: 4px; cursor: pointer; font-size: 12px;
  display: flex; align-items: center; gap: 4px; min-width: 0; }
.call-item:hover { background: var(--bg); }
.call-item .call-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1; }
.call-item .call-cond { font-size: 10px; color: var(--accent); flex-shrink: 0; }
.call-item .call-conf { font-size: 10px; flex-shrink: 0; }

/* P0: Community legend */
#legend { position: absolute; left: 10px; top: 52px; background: rgba(13,27,42,0.9);
  padding: 8px; border-radius: 6px; font-size: 11px; max-height: 300px; overflow-y: auto;
  border: 1px solid var(--border); z-index: var(--z-toolbar); }
#legend .legend-item { display: flex; align-items: center; gap: 4px; cursor: pointer; padding: 2px; }
#legend .legend-color { width: 12px; height: 12px; border-radius: 3px; flex-shrink: 0; }
#legend .legend-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* P0: Depth slider */
#depth-control { display: flex; align-items: center; gap: 4px; }
#depth-slider { width: 80px; }

/* Stats */
#stats { position: absolute; left: 10px; bottom: 10px; background: rgba(13,27,42,0.9);
  padding: 4px 8px; border-radius: 4px; font-size: 11px; color: var(--muted-fg); }

/* Breadcrumb */
#breadcrumb { display: flex; gap: 3px; flex-wrap: wrap; align-items: center; }
.crumb { cursor: pointer; color: var(--primary); font-size: 11px; }
.crumb:hover { text-decoration: underline; }

/* Loading */
#loading { position: fixed; top: 50%; left: 50%; transform: translate(-50%,-50%);
  background: var(--primary); color: #fff; padding: 10px 20px; border-radius: 6px;
  font-size: 13px; display: none; z-index: var(--z-loading); }

/* P0: Help modal */
#help-modal { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.6);
  display: none; align-items: center; justify-content: center; z-index: var(--z-modal); }
#help-content { background: var(--card); padding: 20px; border-radius: 12px; max-width: 500px;
  border: 1px solid var(--border); }
#help-content h2 { margin-top: 0; }
#help-content table { border-collapse: collapse; width: 100%; }
#help-content td { padding: 4px 8px; border-bottom: 1px solid var(--border); font-size: 12px; }
#help-content kbd { background: var(--bg); padding: 1px 5px; border-radius: 3px;
  border: 1px solid var(--border); font-size: 11px; font-family: monospace; }

/* P0: Right-click context menu */
#ctx-menu { position: fixed; display: none; background: var(--card); border: 1px solid var(--border);
  border-radius: 6px; padding: 4px 0; z-index: var(--z-modal); min-width: 140px; }
#ctx-menu .ctx-item { padding: 6px 12px; cursor: pointer; font-size: 12px; }
#ctx-menu .ctx-item:hover { background: var(--bg); }

/* P1: Node type filter */
#filter-panel { display: none; position: absolute; right: 330px; top: 52px;
  background: var(--card); padding: 8px; border-radius: 6px; border: 1px solid var(--border);
  z-index: var(--z-toolbar); }
#filter-panel label { display: flex; align-items: center; gap: 4px; font-size: 11px; cursor: pointer; }
</style>
</head>
<body>
<a href="#cy" class="skip-link">Skip to graph</a>
<div id="topbar">
  <h1>Code2Database</h1>
  <input id="search" placeholder="Search function..." aria-label="Search functions" />
  <button id="search-btn" aria-label="Search">Search</button>
  <select id="layout-select" aria-label="Layout algorithm">
    <option value="breadthfirst">Flow</option>
    <option value="cose">Force</option>
    <option value="concentric">Rings</option>
    <option value="circle">Circle</option>
    <option value="grid">Grid</option>
  </select>
  <div id="depth-control">
    <label for="depth-slider" style="font-size:11px">Depth</label>
    <input type="range" id="depth-slider" min="1" max="5" value="1" class="w-20" aria-label="BFS depth" />
    <span id="depth-val" style="font-size:11px;width:12px">1</span>
  </div>
  <button id="fit-btn" aria-label="Fit graph to screen">Fit</button>
  <button id="png-btn" aria-label="Export as PNG">PNG</button>
  <button id="filter-btn" aria-pressed="false">Filter</button>
  <button id="cycle-btn" aria-pressed="false">Cycles</button>
  <button id="reload-btn" aria-label="Reload graph">Reload</button>
  <button id="help-btn" aria-label="Help">?</button>
  <button id="dark-btn" aria-pressed="true">Dark</button>
</div>
<div id="cy" role="application" aria-label="Code graph visualization" tabindex="0"></div>
<div id="sidebar" role="complementary" aria-label="Node details">
  <h2 id="node-title">Select a node</h2>
  <div id="node-details"></div>
</div>
<div id="legend" style="display:none"></div>
<div id="filter-panel">
  <label><input type="checkbox" class="filter-label" value="API_entry" checked> API Entry</label>
  <label><input type="checkbox" class="filter-label" value="out_end" checked> External</label>
  <label><input type="checkbox" class="filter-label" value="callback_func" checked> Callback</label>
  <label><input type="checkbox" class="filter-label" value="__default" checked> Other</label>
</div>
<div id="stats"></div>
<div id="breadcrumb"></div>
<div id="loading" role="status" aria-live="polite">Loading...</div>
<div id="ctx-menu" role="menu"></div>
<div id="help-modal" role="dialog" aria-label="Help" aria-modal="true">
  <div id="help-content">
    <h2>Keyboard Shortcuts &amp; Help</h2>
    <table>
      <tr><td><kbd>/</kbd></td><td>Focus search box</td></tr>
      <tr><td><kbd>F</kbd></td><td>Fit graph to screen</td></tr>
      <tr><td><kbd>+</kbd> / <kbd>-</kbd></td><td>Zoom in / out</td></tr>
      <tr><td><kbd>Enter</kbd></td><td>Drill into selected node (expand neighbors)</td></tr>
      <tr><td><kbd>Esc</kbd></td><td>Clear focus / close panel</td></tr>
      <tr><td><kbd>1</kbd>-<kbd>5</kbd></td><td>Set BFS depth</td></tr>
      <tr><td><kbd>?</kbd></td><td>Toggle this help</td></tr>
      <tr><td><kbd>D</kbd></td><td>Toggle dark / light mode</td></tr>
      <tr><td><kbd>P</kbd></td><td>Export PNG</td></tr>
      <tr><td>Click node</td><td>Focus + show callers/callees</td></tr>
      <tr><td>Right-click node</td><td>Context menu (Focus/Impact/Copy)</td></tr>
    </table>
    <p style="text-align:center;margin-top:12px"><button class="action-btn" onclick="document.getElementById('help-modal').style.display='none'">Close</button></p>
  </div>
</div>
<!-- Cytoscape.js 3.28.1 — served locally for offline/air-gapped use.
     Falls back to CDN if /static/cytoscape.min.js returns 302 redirect. -->
<script src="/static/cytoscape.min.js"></script>
<script>
// CDN fallback: if local cytoscape.min.js failed to load (302 redirect
// or file missing), load from CDN. This enables both offline-first
// and always-available operation.
if (typeof cytoscape === 'undefined') {
  document.write('<script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js"><\/script>');
}
</script>
<script>
let cy = null;
let cache = { nodes: [], edges: [], focus: null };
let allNodes = {};
let allEdges = {};
let expandedSet = new Set();
let navHistory = [];
let highlightPath = [];
let cycleEdges = new Set();
let maxDegree = 1;
let activeNodeId = null;

// Community color palette (categorical hues)
const COMMUNITY_COLORS = [
  '#4a90e2','#e94a4a','#22c55e','#f59e0b','#a855f7','#06b6d4',
  '#ec4899','#84cc16','#f97316','#6366f1','#14b8a6','#e879f9',
  '#facc15','#fb7185','#8b5cf6','#10b981','#f43f5e','#3b82f6',
];

async function api(path, opts) { const r = await fetch(path, opts || {}); return r.json(); }
function showLoading() { document.getElementById('loading').style.display = 'block'; }
function hideLoading() { document.getElementById('loading').style.display = 'none'; }
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]); }

async function loadSummary() {
  const s = await api('/api/graph/summary');
  document.getElementById('stats').innerHTML = s.node_count + ' nodes · ' + s.edge_count + ' edges';
  // Build community legend
  const legend = document.getElementById('legend');
  let lh = '';
  s.communities.forEach((c, i) => {
    const color = COMMUNITY_COLORS[i % COMMUNITY_COLORS.length];
    lh += '<div class="legend-item" onclick="toggleCommunity(\'' + escapeHtml(c.id) + '\')">' +
      '<div class="legend-color" style="background:' + color + '"></div>' +
      '<span class="legend-label">' + escapeHtml(c.id) + ' (' + c.node_count + ')</span></div>';
  });
  legend.innerHTML = lh;
  legend.style.display = lh ? 'block' : 'none';
}

function communityColor(node) {
  const comm = node.community || node.domain || '';
  const idx = Object.keys(allNodes).indexOf(node.id || '');
  // Use community hash for stable color
  let hash = 0;
  for (let i = 0; i < comm.length; i++) hash = ((hash << 5) - hash + comm.charCodeAt(i)) | 0;
  return COMMUNITY_COLORS[Math.abs(hash) % COMMUNITY_COLORS.length];
}

function nodeClasses(node) {
  let cls = [];
  if (node.is_focused) cls.push('focused');
  if (node.labels) {
    if (node.labels.includes('API_entry')) cls.push('entry');
    if (node.labels.includes('out_end') || node.labels.includes('unknown_end')) cls.push('endpoint');
    if (node.labels.includes('ffi_boundary')) cls.push('ffi');
  }
  return cls.join(' ');
}

// P0: Edge type + confidence styling
function edgeClasses(edge) {
  let cls = [];
  const rel = edge.relation || 'INVOKES';
  if (rel === 'FFI') cls.push('ffi-edge');
  else if (rel === 'IMPORTS') cls.push('import-edge');
  else cls.push('call-edge');
  // Confidence
  const conf = edge.confidence || 'EXTRACTED';
  if (conf === 'INFERRED') cls.push('inferred');
  else if (conf === 'AMBIGUOUS') cls.push('ambiguous');
  // Cycle
  const ek = edge.source + '->' + edge.target;
  if (cycleEdges.has(ek)) cls.push('cycle-edge');
  // Highlight
  if (highlightPath.includes(edge.source) && highlightPath.includes(edge.target)) cls.push('highlighted');
  return cls.join(' ');
}

function buildCyElements() {
  let eles = [];
  for (const [id, node] of Object.entries(allNodes)) {
    const deg = node.degree || 0;
    const size = 16 + Math.sqrt(deg) * 8;
    eles.push({
      data: { id: id, name: node.name || id, labels: node.labels || [],
              degree: deg, community: node.community || node.domain || '' },
      classes: nodeClasses(node)
    });
  }
  for (const [key, edge] of Object.entries(allEdges)) {
    eles.push({
      data: { source: edge.source, target: edge.target,
              relation: edge.relation || 'INVOKES',
              condition: edge.call_condition || '',
              confidence: edge.confidence || 'EXTRACTED' },
      classes: edgeClasses(edge)
    });
  }
  return eles;
}

// P2: Force-sim auto-tuning by node count
function getLayoutOptions() {
  const n = Object.keys(allNodes).length;
  const name = document.getElementById('layout-select').value;
  const opts = { name: name, animate: n < 200, padding: 42 };
  if (name === 'cose') {
    opts.nodeRepulsion = n > 100 ? 4000 : 10000;
    opts.idealEdgeLength = n > 100 ? 50 : 100;
    opts.gravity = n > 200 ? 0.3 : 0.1;
  }
  if (name === 'breadthfirst') {
    opts.spacingFactor = n > 50 ? 1.0 : 1.2;
  }
  return opts;
}

function runLayout() {
  if (!cy) return;
  cy.layout(getLayoutOptions()).run();
}

function initCy() {
  cy = cytoscape({
    container: document.getElementById('cy'),
    elements: buildCyElements(),
    style: [
      { selector: 'node', style: {
        'background-color': 'data(community) ? "#4a90e2" : "#4a90e2"',
        'width': 'data(degree) ? mapData(degree, 0, 30, 16, 40) : 24',
        'height': 'data(degree) ? mapData(degree, 0, 30, 16, 40) : 24',
        'label': 'data(name)', 'font-size': '8px', 'color': '#ccc',
        'text-valign': 'bottom', 'text-margin-y': 4,
        'text-wrap': 'ellipsis', 'text-max-width': '80px',
        'border-width': 0,
      } },
      { selector: 'node.entry', style: { 'background-color': '#60a5fa', 'border-color': '#3b82f6', 'border-width': 2 } },
      { selector: 'node.endpoint', style: { 'background-color': '#fb923c', 'border-color': '#f97316' } },
      { selector: 'node.ffi', style: { 'background-color': '#c084fc', 'border-color': '#a855f7' } },
      { selector: 'node.focused', style: { 'border-color': '#ef4444', 'border-width': 3 } },
      // P0: Edge type styling
      { selector: 'edge.call-edge', style: { 'width': 2, 'line-color': '#64748b', 'curve-style': 'bezier',
        'target-arrow-shape': 'triangle', 'arrow-scale': 0.8, 'opacity': 0.6 } },
      { selector: 'edge.import-edge', style: { 'line-color': '#f59e0b', 'line-style': 'dashed', 'opacity': 0.5 } },
      { selector: 'edge.ffi-edge', style: { 'line-color': '#a855f7', 'line-style': 'dotted', 'opacity': 0.6 } },
      // P1: Edge confidence
      { selector: 'edge.inferred', style: { 'line-style': 'dashed', 'opacity': 0.4 } },
      { selector: 'edge.ambiguous', style: { 'line-style': 'dotted', 'opacity': 0.3 } },
      // P1: Cycle detection
      { selector: 'edge.cycle-edge', style: { 'line-color': '#ef4444', 'line-style': 'dashed', 'width': 3 } },
      // Highlight
      { selector: 'edge.highlighted', style: { 'width': 4, 'line-color': '#f59e0b', 'opacity': 0.9 } },
      { selector: '.faded', style: { 'opacity': 0.12 } },
      // P1: Label zoom threshold
      { selector: 'node', style: { 'text-opacity': 0 } },
      { selector: 'node[degree > 0]', style: { 'text-opacity': 1 } },
      { selector: 'edge.show-condition', style: { 'label': 'data(condition)', 'font-size': '6px',
        'color': '#f59e0b', 'text-rotation': 'autorotate', 'opacity': 0.7 } },
    ],
    layout: getLayoutOptions(),
    wheelSensitivity: 0.2,
  });
  // P0: Node click → callers/callees panel
  cy.on('tap', 'node', function(evt) {
    activeNodeId = evt.target.id();
    focusNode(evt.target.id(), parseInt(document.getElementById('depth-slider').value));
  });
  cy.on('tap', function(evt) {
    if (evt.target === cy) { cy.elements().removeClass('faded'); }
  });
  // P1: Right-click context menu
  cy.on('cxttap', 'node', function(evt) {
    showContextMenu(evt.target.id(), evt.originalEvent.clientX, evt.originalEvent.clientY);
  });
  // P1: Label zoom threshold
  cy.on('zoom', function() {
    const z = cy.zoom();
    if (z < 0.5) {
      cy.style().selector('node').style('text-opacity', 0).update();
    } else {
      cy.style().selector('node').style('text-opacity', 1).update();
    }
  });
  // P0: Apply community colors
  applyCommunityColors();
}

// P0: Community coloring
function applyCommunityColors() {
  if (!cy) return;
  cy.nodes().forEach(node => {
    const comm = node.data('community');
    if (comm) {
      let hash = 0;
      for (let i = 0; i < comm.length; i++) hash = ((hash << 5) - hash + comm.charCodeAt(i)) | 0;
      const color = COMMUNITY_COLORS[Math.abs(hash) % COMMUNITY_COLORS.length];
      node.style('background-color', color);
    }
  });
}

function mapData(val, fromMin, fromMax, toMin, toMax) {
  if (fromMax === fromMin) return toMin;
  const t = Math.max(0, Math.min(1, (val - fromMin) / (fromMax - fromMin)));
  return toMin + t * (toMax - toMin);
}

function syncCyFromModel() {
  if (!cy) { initCy(); return; }
  const currentIds = new Set(cy.nodes().map(n => n.id()));
  const modelIds = new Set(Object.keys(allNodes));
  for (const id of modelIds) {
    if (!currentIds.has(id)) {
      cy.add({ data: { id: id, name: allNodes[id].name || id, labels: allNodes[id].labels || [],
        degree: allNodes[id].degree || 0, community: allNodes[id].community || allNodes[id].domain || '' } });
    }
  }
  for (const id of currentIds) {
    if (!modelIds.has(id)) { cy.remove('#' + id); }
  }
  runLayout();
  applyCommunityColors();
}

function applyFocusContext(focusId) {
  if (!cy) return;
  cy.elements().removeClass('faded');
  const connected = cy.elements('node#' + focusId + ' *edge, edge *node#' + focusId);
  cy.elements().not(connected).not('#' + focusId).addClass('faded');
}

async function focusNode(nodeId, depth) {
  showLoading();
  try {
    const data = await api('/api/neighbors/' + encodeURIComponent(nodeId) + '?depth=' + (depth || 1));
    cache = data;
    cache.focus = nodeId;
    if (!allNodes[nodeId]) {
      allNodes[nodeId] = { id: nodeId, name: nodeId, is_focused: true };
    }
    for (const n of (data.nodes || [])) {
      const deg = await api('/api/degrees').then(d => d.degrees?.[n.id] || 0).catch(() => 0);
      allNodes[n.id] = { ...n, degree: deg, community: n.domain || '' };
    }
    for (const e of (data.edges || [])) {
      allEdges[e.source + '->' + e.target] = e;
    }
    syncCyFromModel();
    loadNodeDetails(nodeId);
    applyFocusContext(nodeId);
  } finally { hideLoading(); }
}

// P0: Click node → callers/callees detail panel
async function loadNodeDetails(nodeId) {
  try {
    const [node, callers, callees] = await Promise.all([
      api('/api/node/' + encodeURIComponent(nodeId)),
      api('/api/callers/' + encodeURIComponent(nodeId)),
      api('/api/callees/' + encodeURIComponent(nodeId)),
    ]);
    document.getElementById('node-title').textContent = node.name || nodeId;
    let html = '';
    const fields = [
      ['Domain', node.domain], ['Labels', (node.labels||[]).join(', ')],
      ['Location', node.location], ['Signature', node.signature],
      ['Description', node.semantic_desc || node.external_desc || '(none)'],
    ];
    for (const [label, value] of fields) {
      if (!value) continue;
      html += '<div class="field"><div class="field-label">' + escapeHtml(label) + '</div>' +
              '<div class="field-value">' + escapeHtml(value) + '</div></div>';
    }
    // Callers list
    if (callers.callers && callers.callers.length > 0) {
      html += '<div class="call-list"><div class="call-list-title">Callers (' + callers.callers.length + ')</div>';
      callers.callers.forEach(c => {
        html += '<div class="call-item" onclick="focusNode(\'' + escapeHtml(c.id) + '\',' + 
          (document.getElementById('depth-slider').value) + ')">' +
          '<span class="call-name mono">' + escapeHtml(c.name) + '</span>' +
          (c.call_condition ? '<span class="call-cond">' + escapeHtml(c.call_condition.substring(0,20)) + '</span>' : '') +
          (c.confidence !== 'EXTRACTED' ? '<span class="call-conf" style="color:#f59e0b">' + c.confidence.substring(0,3) + '</span>' : '') +
          '</div>';
      });
      html += '</div>';
    }
    // Callees list
    if (callees.callees && callees.callees.length > 0) {
      html += '<div class="call-list"><div class="call-list-title">Callees (' + callees.callees.length + ')</div>';
      callees.callees.forEach(c => {
        html += '<div class="call-item" onclick="focusNode(\'' + escapeHtml(c.id) + '\',' +
          (document.getElementById('depth-slider').value) + ')">' +
          '<span class="call-name mono">' + escapeHtml(c.name) + '</span>' +
          (c.call_condition ? '<span class="call-cond">' + escapeHtml(c.call_condition.substring(0,20)) + '</span>' : '') +
          (c.confidence !== 'EXTRACTED' ? '<span class="call-conf" style="color:#f59e0b">' + c.confidence.substring(0,3) + '</span>' : '') +
          '</div>';
      });
      html += '</div>';
    }
    // Action buttons
    html += '<div class="action-btns">' +
      '<button class="action-btn" onclick="loadCode(\''+escapeHtml(nodeId)+'\')">View Code</button>' +
      '<button class="action-btn" onclick="loadImpact(\''+escapeHtml(nodeId)+'\')">Impact</button>' +
      '<button class="action-btn" onclick="exportPNG()">PNG</button>' +
      '</div>';
    document.getElementById('node-details').innerHTML = html;
  } catch(e) { console.error(e); }
}

async function loadCode(nodeId) {
  const data = await api('/api/code?node=' + encodeURIComponent(nodeId));
  alert(data.code ? data.code.substring(0, 2000) : '(no source available)');
}

// P1: Blast-radius overlay (impact as visual highlight)
async function loadImpact(nodeId) {
  showLoading();
  try {
    const data = await api('/api/impact?node=' + encodeURIComponent(nodeId));
    if (cy) {
      cy.elements().removeClass('faded');
      const affectedIds = new Set([nodeId, ...(data.affected||[]).map(a => a.id)]);
      cy.nodes().forEach(n => {
        if (!affectedIds.has(n.id())) n.addClass('faded');
      });
    }
    const sidebar = document.getElementById('node-details');
    sidebar.innerHTML += '<div class="call-list"><div class="call-list-title">Impact: ' +
      (data.affected_count||0) + ' affected</div></div>';
  } finally { hideLoading(); }
}

// P0: PNG export
function exportPNG() {
  if (!cy) return;
  const png64 = cy.png({ full: true, scale: 2, bg: '#0d1b2a' });
  const a = document.createElement('a');
  a.href = png64;
  a.download = 'code2database_graph.png';
  a.click();
}

// P1: Right-click context menu
function showContextMenu(nodeId, x, y) {
  const menu = document.getElementById('ctx-menu');
  menu.innerHTML = '';
  const items = [
    { label: 'Focus', action: () => focusNode(nodeId, 1) },
    { label: 'Expand (depth 2)', action: () => focusNode(nodeId, 2) },
    { label: 'Impact Analysis', action: () => loadImpact(nodeId) },
    { label: 'View Code', action: () => loadCode(nodeId) },
    { label: 'Copy ID', action: () => navigator.clipboard.writeText(nodeId) },
  ];
  items.forEach(item => {
    const el = document.createElement('div');
    el.className = 'ctx-item';
    el.textContent = item.label;
    el.onclick = () => { item.action(); menu.style.display = 'none'; };
    menu.appendChild(el);
  });
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';
  menu.style.display = 'block';
}

// P1: Cycle detection toggle
async function toggleCycles() {
  const btn = document.getElementById('cycle-btn');
  if (cycleEdges.size > 0) {
    cycleEdges.clear();
    btn.setAttribute('aria-pressed', 'false');
    syncCyFromModel();
    return;
  }
  showLoading();
  try {
    const data = await api('/api/cycles');
    (data.cycles||[]).forEach(c => {
      cycleEdges.add(c.source + '->' + c.target);
    });
    btn.setAttribute('aria-pressed', 'true');
    syncCyFromModel();
  } finally { hideLoading(); }
}

// P0: Community legend toggle
let hiddenCommunities = new Set();
function toggleCommunity(commId) {
  if (hiddenCommunities.has(commId)) {
    hiddenCommunities.delete(commId);
  } else {
    hiddenCommunities.add(commId);
  }
  if (!cy) return;
  cy.nodes().forEach(n => {
    const comm = n.data('community');
    if (comm && hiddenCommunities.has(comm)) {
      n.addClass('faded');
    } else {
      n.removeClass('faded');
    }
  });
}

// P0: Dark mode toggle
function toggleDark() {
  const root = document.documentElement;
  const btn = document.getElementById('dark-btn');
  if (root.classList.contains('light')) {
    root.classList.remove('light');
    btn.setAttribute('aria-pressed', 'true');
    btn.textContent = 'Dark';
  } else {
    root.classList.add('light');
    btn.setAttribute('aria-pressed', 'false');
    btn.textContent = 'Light';
  }
}

// P0: Node type filter
function applyFilters() {
  if (!cy) return;
  const checked = new Set();
  document.querySelectorAll('.filter-label:checked').forEach(cb => checked.add(cb.value));
  cy.nodes().forEach(n => {
    const labels = n.data('labels') || [];
    let visible = false;
    if (checked.has('__default')) visible = true;
    for (const l of labels) {
      if (checked.has(l)) { visible = true; break; }
    }
    if (!visible && labels.length > 0) n.addClass('faded');
    else n.removeClass('faded');
  });
}

async function search() {
  const q = document.getElementById('search').value.trim();
  if (!q) return;
  const data = await api('/api/search?q=' + encodeURIComponent(q));
  if (data.results && data.results.length > 0) {
    focusNode(data.results[0].id, 2);
  } else {
    document.getElementById('stats').textContent = 'No matches for: ' + q;
  }
}

// Event listeners
document.getElementById('search-btn').addEventListener('click', search);
document.getElementById('search').addEventListener('keydown', e => { if (e.key === 'Enter') search(); });
document.getElementById('layout-select').addEventListener('change', runLayout);
document.getElementById('depth-slider').addEventListener('input', e => {
  document.getElementById('depth-val').textContent = e.target.value;
});
document.getElementById('depth-slider').addEventListener('change', e => {
  if (activeNodeId) focusNode(activeNodeId, parseInt(e.target.value));
});
document.getElementById('fit-btn').addEventListener('click', () => { if (cy) cy.fit(undefined, 42); });
document.getElementById('png-btn').addEventListener('click', exportPNG);
document.getElementById('cycle-btn').addEventListener('click', toggleCycles);
document.getElementById('reload-btn').addEventListener('click', async () => {
  await api('/api/reload', { method: 'POST' }); loadSummary();
  if (cache.focus) focusNode(cache.focus, 1);
});
document.getElementById('help-btn').addEventListener('click', () => {
  const m = document.getElementById('help-modal');
  m.style.display = m.style.display === 'flex' ? 'none' : 'flex';
});
document.getElementById('dark-btn').addEventListener('click', toggleDark);
document.getElementById('filter-btn').addEventListener('click', () => {
  const btn = document.getElementById('filter-btn');
  const panel = document.getElementById('filter-panel');
  const visible = panel.style.display === 'block';
  panel.style.display = visible ? 'none' : 'block';
  btn.setAttribute('aria-pressed', !visible);
  if (!visible) applyFilters();
});
document.querySelectorAll('.filter-label').forEach(cb => cb.addEventListener('change', applyFilters));

// P1: Keyboard navigation
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  switch(e.key) {
    case '/': e.preventDefault(); document.getElementById('search').focus(); break;
    case 'f': case 'F': if (cy) cy.fit(undefined, 42); break;
    case '?': document.getElementById('help-btn').click(); break;
    case 'd': case 'D': toggleDark(); break;
    case 'p': case 'P': exportPNG(); break;
    case '+': case '=': if (cy) cy.zoom(cy.zoom() * 1.3); break;
    case '-': if (cy) cy.zoom(cy.zoom() / 1.3); break;
    case 'Escape':
      if (cy) cy.elements().removeClass('faded');
      document.getElementById('help-modal').style.display = 'none';
      document.getElementById('ctx-menu').style.display = 'none';
      document.getElementById('filter-panel').style.display = 'none';
      break;
    case 'Enter':
      if (activeNodeId) focusNode(activeNodeId, parseInt(document.getElementById('depth-slider').value));
      break;
    case '1': case '2': case '3': case '4': case '5':
      document.getElementById('depth-slider').value = e.key;
      document.getElementById('depth-val').textContent = e.key;
      if (activeNodeId) focusNode(activeNodeId, parseInt(e.key));
      break;
  }
});

// Close context menu on click anywhere
document.addEventListener('click', () => {
  document.getElementById('ctx-menu').style.display = 'none';
});

loadSummary();
window.addEventListener('resize', () => { if (cy) cy.resize(); });
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def cmd_web_ui(args):
    """Start the interactive Web UI server.

    Usage: web-ui --graph <dir> --port 8765 [--open]
    """
    graph_dir = args.graph
    port = getattr(args, "port", 8765) or 8765
    open_browser = getattr(args, "open", False)

    if not os.path.exists(os.path.join(graph_dir, "code2database_master.json")) and \
       not os.path.exists(os.path.join(graph_dir, "code2database.db")):
        _log.error("No invocation graph found at %s", graph_dir)
        sys.exit(1)

    cache = GraphCache(graph_dir)
    handler_class = _make_handler_class(cache)
    # Use ThreadingHTTPServer (not single-threaded HTTPServer) so one
    # slow request (e.g., /api/tour which scans temp files) doesn't
    # block every other client. Bind to 127.0.0.1 (not 0.0.0.0) by
    # default to avoid exposing the code graph to the local network —
    # the web UI serves source snippets via /api/code, which is a data
    # leak if reachable from other machines.
    bind_host = os.environ.get("C2D_WEB_UI_HOST", "127.0.0.1")
    server = ThreadingHTTPServer((bind_host, port), handler_class)
    _log.info("Web UI: http://localhost:%s", port)
    _log.info("Graph: %s", cache.summary())
    _log.info("Ctrl+C to stop")
    timer = None
    if open_browser:
        timer = threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{port}"))
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log.info("Stopping...")
        server.shutdown()
    finally:
        if timer is not None:
            timer.cancel()
