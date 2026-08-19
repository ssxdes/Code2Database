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
import re
import sys
import threading
import urllib.parse
import webbrowser
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional, List, Dict, Any, Set, Tuple


# ---------------------------------------------------------------------------
# Graph cache — load once at server startup, refresh on demand
# ---------------------------------------------------------------------------

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
            except (OSError, IOError):
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


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class WebUIHandler(BaseHTTPRequestHandler):
    """HTTP request handler serving the API + the HTML UI."""

    # Set by the factory function before serve_forever()
    cache: GraphCache = None
    highlight_path: List[str] = []  # path highlighted via POST /api/highlight-path

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
                self._send_json(200, {"path": self.highlight_path})
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
            if path == "/api/suggestions":
                try:
                    from _builder.cgdb_suggest import analyze_and_suggest
                    suggestions = analyze_and_suggest(self.cache.graph_dir)
                    self._send_json(200, {"suggestions": suggestions})
                except Exception as exc:
                    self._send_json(200, {"suggestions": [], "error": str(exc)})
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
                    self._send_json(200, {"tour": "", "error": str(exc)})
                return
            self._send_json(404, {"error": f"unknown path {path}"})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/highlight-path":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body) if body else {}
                # Set on the CLASS (not the instance) so all handlers see it
                type(self).highlight_path = data.get("path", [])
                self._send_json(200, {"ok": True, "path": type(self).highlight_path})
                return
            if path == "/api/reload":
                self.cache.reload()
                self._send_json(200, {"ok": True, "summary": self.cache.summary()})
                return
            self._send_json(404, {"error": f"unknown path {path}"})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

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
<title>Code2Database</title>
<style>
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
       background: #fafafa; color: #222; }
#topbar { background: #1a1a2e; color: #fff; padding: 8px 16px;
          display: flex; align-items: center; gap: 12px; }
#topbar h1 { font-size: 16px; margin: 0; font-weight: 500; }
#topbar input { flex: 1; padding: 6px 10px; border-radius: 4px; border: none;
                font-size: 13px; }
#topbar button { padding: 6px 12px; background: #4a90e2; color: #fff;
                 border: none; border-radius: 4px; cursor: pointer; font-size: 13px; }
#topbar button:hover { background: #357ab8; }
#sidebar { position: absolute; left: 0; top: 50px; bottom: 0; width: 320px;
           background: #fff; border-right: 1px solid #ddd; overflow-y: auto;
           padding: 12px; }
#sidebar h2 { font-size: 14px; margin: 0 0 8px; }
#sidebar .field { margin-bottom: 8px; }
#sidebar .field-label { color: #666; font-size: 11px; text-transform: uppercase; }
#sidebar .field-value { font-size: 13px; word-break: break-word; }
#canvas { position: absolute; left: 320px; top: 50px; right: 0; bottom: 0;
          background: #fafafa; cursor: grab; }
#canvas:active { cursor: grabbing; }
#canvas svg { width: 100%; height: 100%; }
.node { fill: #fff; stroke: #4a90e2; stroke-width: 1.5px; cursor: pointer; }
.node.focused { stroke: #e94a4a; stroke-width: 3px; }
.node.entry { fill: #e7f4ff; }
.node.endpoint { fill: #fff4e7; }
.node.ffi { fill: #f0e7ff; stroke: #a070d0; }
.node-label { font-size: 11px; pointer-events: none; text-anchor: middle; }
.edge { stroke: #999; stroke-width: 1px; fill: none; marker-end: url(#arrow); }
.edge.ffi { stroke: #a070d0; stroke-dasharray: 4,2; }
.edge.highlighted { stroke: #e94a4a; stroke-width: 3px; }
.node.highlighted { stroke: #e94a4a; stroke-width: 3px; }
#stats { position: absolute; right: 12px; top: 60px; background: #fff;
         padding: 8px 12px; border: 1px solid #ddd; border-radius: 4px;
         font-size: 12px; color: #444; }
#controls { position: absolute; right: 12px; bottom: 12px; display: flex; gap: 4px; }
#controls button { padding: 6px 10px; background: #fff; border: 1px solid #ccc;
                   border-radius: 4px; cursor: pointer; font-size: 12px; }
#controls button:hover { background: #f0f0f0; }
.community-box { fill: none; stroke: #ccc; stroke-dasharray: 2,2; }
.community-label { font-size: 11px; fill: #888; text-anchor: middle; }
.path-list { background: #f6f6f6; padding: 6px; border-radius: 4px;
             margin-top: 8px; font-family: monospace; font-size: 11px; }
</style>
</head>
<body>
<div id="topbar">
  <h1>Code2Database</h1>
  <input id="search" placeholder="Search function name..." />
  <button id="search-btn">Search</button>
  <button id="reload-btn">Reload</button>
</div>
<div id="sidebar">
  <h2 id="node-title">Select a node</h2>
  <div id="node-details"></div>
</div>
<div id="canvas">
  <svg id="svg">
    <defs>
      <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5"
              markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M 0 0 L 10 5 L 0 10 z" fill="#999"/>
      </marker>
    </defs>
    <g id="viewport"></g>
  </svg>
</div>
<div id="stats"></div>
<div id="controls">
  <button onclick="zoom(1.2)">+</button>
  <button onclick="zoom(0.8)">−</button>
  <button onclick="resetView()">Reset</button>
  <button onclick="expandNeighbors()">Expand</button>
</div>

<script>
let cache = { nodes: [], edges: [], focus: null };
let highlightPath = [];
let viewTransform = { x: 0, y: 0, scale: 1 };
let lastPan = null;

async function api(path, opts) {
  const res = await fetch(path, opts || {});
  return res.json();
}

async function loadSummary() {
  const s = await api('/api/graph/summary');
  document.getElementById('stats').innerHTML =
    `${s.node_count} nodes • ${s.edge_count} edges • ${s.ffi_edge_count} FFI<br>` +
    `${s.community_count} communities`;
}

async function focusNode(nodeId, depth = 1) {
  const data = await api('/api/neighbors/' + encodeURIComponent(nodeId) + '?depth=' + depth);
  cache = data;
  cache.focus = nodeId;
  render();
  loadNodeDetails(nodeId);
}

async function loadNodeDetails(nodeId) {
  const node = await api('/api/node/' + encodeURIComponent(nodeId));
  document.getElementById('node-title').textContent = node.name || nodeId;
  const fields = [
    ['Domain', node.domain],
    ['Labels', (node.labels || []).join(', ')],
    ['Location', node.location],
    ['Signature', node.signature],
    ['Description', node.semantic_desc || node.external_desc || '(none)'],
    ['Constraints', node.api_constraints || '(none)'],
  ];
  let html = '';
  for (const [label, value] of fields) {
    if (!value) continue;
    html += `<div class="field"><div class="field-label">${label}</div>` +
            `<div class="field-value">${escapeHtml(value)}</div></div>`;
  }
  document.getElementById('node-details').innerHTML = html;
}

async function search() {
  const q = document.getElementById('search').value.trim();
  if (!q) return;
  const data = await api('/api/search?q=' + encodeURIComponent(q));
  if (data.results && data.results.length > 0) {
    focusNode(data.results[0].id, 2);
  } else {
    alert('No matches for: ' + q);
  }
}

async function findPath(fromId, toId) {
  const data = await api('/api/path?from=' + encodeURIComponent(fromId) + '&to=' + encodeURIComponent(toId));
  if (data.path) {
    highlightPath = data.path;
    await api('/api/highlight-path', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: highlightPath})
    });
    render();
    const details = document.getElementById('node-details');
    details.innerHTML += '<div class="path-list">Path: ' +
      highlightPath.map(id => id.split(':').pop()).join(' → ') + '</div>';
  } else {
    alert('No path found');
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
}

function render() {
  const svg = document.getElementById('svg');
  let vp = document.getElementById('viewport');
  vp.innerHTML = '';
  vp.setAttribute('transform',
    `translate(${viewTransform.x},${viewTransform.y}) scale(${viewTransform.scale})`);

  // Layout: place nodes in a circle around the focus
  const W = svg.clientWidth, H = svg.clientHeight;
  const cx = W / 2, cy = H / 2;
  const radius = Math.min(W, H) * 0.35;
  const nodePos = {};
  const focusIdx = cache.nodes.findIndex(n => n.id === cache.focus);
  cache.nodes.forEach((n, i) => {
    if (n.id === cache.focus) {
      nodePos[n.id] = { x: cx, y: cy };
    } else {
      const angle = (i / Math.max(1, cache.nodes.length - 1)) * 2 * Math.PI;
      nodePos[n.id] = { x: cx + Math.cos(angle) * radius, y: cy + Math.sin(angle) * radius };
    }
  });

  // Draw edges
  for (const e of cache.edges) {
    const s = nodePos[e.source], t = nodePos[e.target];
    if (!s || !t) continue;
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', s.x); line.setAttribute('y1', s.y);
    line.setAttribute('x2', t.x); line.setAttribute('y2', t.y);
    line.setAttribute('class', 'edge' +
      (e.relation === 'FFI' ? ' ffi' : '') +
      (highlightPath.includes(e.source) && highlightPath.includes(e.target) ? ' highlighted' : ''));
    vp.appendChild(line);
  }

  // Draw nodes
  for (const n of cache.nodes) {
    const pos = nodePos[n.id];
    if (!pos) continue;
    const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('cx', pos.x); circle.setAttribute('cy', pos.y);
    circle.setAttribute('r', 18);
    let cls = 'node';
    if (n.is_focused) cls += ' focused';
    if (n.labels && n.labels.includes('API_entry')) cls += ' entry';
    if (n.labels && (n.labels.includes('out_end') || n.labels.includes('unknown_end'))) cls += ' endpoint';
    if (n.labels && n.labels.includes('ffi_boundary')) cls += ' ffi';
    if (highlightPath.includes(n.id)) cls += ' highlighted';
    circle.setAttribute('class', cls);
    circle.addEventListener('click', () => focusNode(n.id, 1));
    g.appendChild(circle);
    const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    text.setAttribute('x', pos.x);
    text.setAttribute('y', pos.y + 32);
    text.setAttribute('class', 'node-label');
    text.textContent = (n.name || n.id).split(/[:.]/).pop().slice(0, 30);
    g.appendChild(text);
    vp.appendChild(g);
  }
}

function zoom(factor) {
  viewTransform.scale *= factor;
  render();
}

function resetView() {
  viewTransform = { x: 0, y: 0, scale: 1 };
  render();
}

function expandNeighbors() {
  if (cache.focus) focusNode(cache.focus, 2);
}

// Pan support
document.getElementById('svg').addEventListener('mousedown', e => {
  if (e.target.tagName === 'circle') return;
  lastPan = { x: e.clientX, y: e.clientY };
});
document.getElementById('svg').addEventListener('mousemove', e => {
  if (!lastPan) return;
  viewTransform.x += e.clientX - lastPan.x;
  viewTransform.y += e.clientY - lastPan.y;
  lastPan = { x: e.clientX, y: e.clientY };
  render();
});
document.getElementById('svg').addEventListener('mouseup', () => lastPan = null);
document.getElementById('svg').addEventListener('mouseleave', () => lastPan = null);

document.getElementById('search-btn').addEventListener('click', search);
document.getElementById('search').addEventListener('keydown', e => {
  if (e.key === 'Enter') search();
});
document.getElementById('reload-btn').addEventListener('click', async () => {
  await api('/api/reload', { method: 'POST' });
  loadSummary();
  if (cache.focus) focusNode(cache.focus, 1);
});

// Initial load
loadSummary();
window.addEventListener('resize', () => render());
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
        print(f"Error: No invocation graph found at {graph_dir}", file=sys.stderr)
        sys.exit(1)

    cache = GraphCache(graph_dir)
    handler_class = _make_handler_class(cache)
    server = HTTPServer(("0.0.0.0", port), handler_class)
    print(f"Web UI: http://localhost:{port}", file=sys.stderr)
    print(f"Graph: {cache.summary()}", file=sys.stderr)
    print("Ctrl+C to stop", file=sys.stderr)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...", file=sys.stderr)
        server.shutdown()
