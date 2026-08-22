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
       background: #1a1a2e; color: #e0e0e0; }
#topbar { background: #0d1b2a; color: #fff; padding: 8px 16px;
          display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
#topbar h1 { font-size: 16px; margin: 0; font-weight: 500; }
#topbar input { flex: 1; min-width: 200px; padding: 6px 10px; border-radius: 4px;
                border: 1px solid #334; background: #16213e; color: #fff; font-size: 13px; }
#topbar button, #topbar select { padding: 6px 12px; background: #4a90e2; color: #fff;
                 border: none; border-radius: 4px; cursor: pointer; font-size: 13px; }
#topbar button:hover { background: #357ab8; }
#cy { position: absolute; left: 0; top: 50px; right: 0; bottom: 0; background: #16213e; }
#sidebar { position: absolute; right: 0; top: 50px; bottom: 0; width: 300px;
           background: #0d1b2a; border-left: 1px solid #334; overflow-y: auto;
           padding: 12px; color: #ccc; }
#sidebar h2 { font-size: 14px; margin: 0 0 8px; color: #8ab; }
#sidebar .field { margin-bottom: 8px; }
#sidebar .field-label { color: #678; font-size: 11px; text-transform: uppercase; }
#sidebar .field-value { font-size: 13px; word-break: break-word; }
#sidebar .action-btns { display: flex; gap: 4px; margin-top: 8px; flex-wrap: wrap; }
.action-btn { padding: 4px 8px; background: #4a90e2; color: #fff; border: none;
              border-radius: 3px; cursor: pointer; font-size: 11px; }
.action-btn:hover { background: #357ab8; }
#stats { position: absolute; left: 12px; bottom: 12px; background: rgba(13,27,42,0.9);
         padding: 6px 10px; border-radius: 4px; font-size: 12px; color: #8ab; }
#breadcrumb { display: flex; gap: 4px; flex-wrap: wrap; align-items: center; }
#breadcrumb .crumb { cursor: pointer; color: #6ab; font-size: 12px; }
#breadcrumb .crumb:hover { text-decoration: underline; }
#breadcrumb .sep { color: #445; font-size: 12px; }
#loading { position: fixed; top: 50%; left: 50%; transform: translate(-50%,-50%);
           background: rgba(74,144,226,0.9); color: #fff; padding: 12px 24px;
           border-radius: 8px; font-size: 14px; display: none; z-index: 9999; }
</style>
</head>
<body>
<div id="topbar">
  <h1>Code2Database</h1>
  <input id="search" placeholder="Search function name..." />
  <button id="search-btn">Search</button>
  <select id="layout-select">
    <option value="breadthfirst">Flow</option>
    <option value="concentric">Rings</option>
    <option value="cose">Force</option>
  </select>
  <button id="fit-btn">Fit</button>
  <button id="reload-btn">Reload</button>
  <button id="suggest-btn">Suggest</button>
  <button id="dark-btn">Dark</button>
</div>
<div id="cy"></div>
<div id="sidebar">
  <h2 id="node-title">Select a node</h2>
  <div id="node-details"></div>
</div>
<div id="stats"></div>
<div id="loading">Loading...</div>
<!-- cytoscape.js 3.28.1: for offline use, replace this CDN script tag
     with an inlined copy of cytoscape.min.js (npm pack cytoscape@3.28.1) -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js"></script>
<script>
let cy = null;
let cache = { nodes: [], edges: [], focus: null };
let allNodes = {};
let allEdges = {};
let expandedSet = new Set();
let navHistory = [];
let navFuture = [];
let highlightPath = [];

async function api(path, opts) {
  const res = await fetch(path, opts || {});
  return res.json();
}

function showLoading() { document.getElementById('loading').style.display = 'block'; }
function hideLoading() { document.getElementById('loading').style.display = 'none'; }

async function loadSummary() {
  const s = await api('/api/graph/summary');
  document.getElementById('stats').innerHTML =
    s.node_count + ' nodes &middot; ' + s.edge_count + ' edges';
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

function edgeClasses(edge) {
  let cls = [];
  if (edge.relation === 'FFI') cls.push('ffi');
  if (highlightPath.includes(edge.source) && highlightPath.includes(edge.target)) cls.push('highlighted');
  return cls.join(' ');
}

function buildCyElements() {
  let eles = [];
  for (const [id, node] of Object.entries(allNodes)) {
    eles.push({
      data: { id: id, name: node.name || id, labels: node.labels || [] },
      classes: nodeClasses(node)
    });
  }
  for (const [key, edge] of Object.entries(allEdges)) {
    eles.push({
      data: { source: edge.source, target: edge.target, relation: edge.relation || 'INVOKES',
               condition: edge.call_condition || '' },
      classes: edgeClasses(edge)
    });
  }
  return eles;
}

function runLayout() {
  if (!cy) return;
  const layoutName = document.getElementById('layout-select').value;
  cy.layout({ name: layoutName, animate: true, padding: 42,
    spacingFactor: 1.2 }).run();
}

function initCy() {
  cy = cytoscape({
    container: document.getElementById('cy'),
    elements: buildCyElements(),
    style: [
      { selector: 'node', style: { 'background-color': '#4a90e2', 'width': 24, 'height': 24,
        'label': 'data(name)', 'font-size': '8px', 'color': '#ccc', 'text-valign': 'bottom',
        'text-margin-y': 4 } },
      { selector: 'node.entry', style: { 'background-color': '#e7f4ff', 'border-color': '#4a90e2', 'border-width': 2 } },
      { selector: 'node.endpoint', style: { 'background-color': '#fff4e7', 'border-color': '#e94a4a' } },
      { selector: 'node.ffi', style: { 'background-color': '#f0e7ff', 'border-color': '#a070d0' } },
      { selector: 'node.focused', style: { 'border-color': '#e94a4a', 'border-width': 3 } },
      { selector: 'edge', style: { 'width': 2, 'line-color': '#445', 'curve-style': 'bezier',
        'target-arrow-shape': 'triangle', 'arrow-scale': 0.8 } },
      { selector: 'edge.ffi', style: { 'line-color': '#a070d0', 'line-style': 'dashed' } },
      { selector: 'edge.highlighted', style: { 'width': 4, 'line-color': '#e94a4a' } },
      { selector: '.faded', style: { 'opacity': 0.15 } },
      { selector: 'edge.show-condition', style: { 'label': 'data(condition)', 'font-size': '6px', 'color': '#678',
        'text-rotation': 'autorotate' } },
    ],
    layout: { name: 'breadthfirst', animate: true, padding: 42, spacingFactor: 1.2 }
  });
  cy.on('tap', 'node', function(evt) {
    focusNode(evt.target.id(), 1);
  });
  cy.on('tap', function(evt) {
    if (evt.target === cy) {
      cy.elements().removeClass('faded');
    }
  });
}

function syncCyFromModel() {
  if (!cy) { initCy(); return; }
  const currentIds = new Set(cy.nodes().map(n => n.id()));
  const modelIds = new Set(Object.keys(allNodes));
  for (const id of modelIds) {
    if (!currentIds.has(id)) {
      cy.add({ data: { id: id, name: allNodes[id].name || id, labels: allNodes[id].labels || [] } });
    }
  }
  for (const id of currentIds) {
    if (!modelIds.has(id)) {
      cy.remove('#' + id);
    }
  }
  runLayout();
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
      allNodes[n.id] = n;
    }
    for (const e of (data.edges || [])) {
      allEdges[e.source + '->' + e.target] = e;
    }
    syncCyFromModel();
    loadNodeDetails(nodeId);
    applyFocusContext(nodeId);
  } finally { hideLoading(); }
}

async function loadNodeDetails(nodeId) {
  try {
    const node = await api('/api/node/' + encodeURIComponent(nodeId));
    document.getElementById('node-title').textContent = node.name || nodeId;
    let html = '';
    const fields = [
      ['Domain', node.domain], ['Labels', (node.labels||[]).join(', ')],
      ['Location', node.location], ['Signature', node.signature],
      ['Description', node.semantic_desc || node.external_desc || '(none)'],
    ];
    for (const [label, value] of fields) {
      if (!value) continue;
      html += '<div class="field"><div class="field-label">' + label + '</div>' +
              '<div class="field-value">' + escapeHtml(value) + '</div></div>';
    }
    html += '<div class="action-btns">' +
      '<button class="action-btn" onclick="loadCode(\''+escapeHtml(nodeId)+'\')">View Code</button>' +
      '<button class="action-btn" onclick="loadImpact(\''+escapeHtml(nodeId)+'\')">Impact</button>' +
      '</div>';
    document.getElementById('node-details').innerHTML = html;
  } catch(e) {}
}

async function loadCode(nodeId) {
  const data = await api('/api/code?node=' + encodeURIComponent(nodeId));
  alert(data.code ? data.code.substring(0, 2000) : '(no source available)');
}

async function loadImpact(nodeId) {
  const data = await api('/api/impact?node=' + encodeURIComponent(nodeId));
  alert('Affected: ' + (data.affected_count || 0) + ' functions');
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
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

document.getElementById('search-btn').addEventListener('click', search);
document.getElementById('search').addEventListener('keydown', e => {
  if (e.key === 'Enter') search();
});
document.getElementById('layout-select').addEventListener('change', runLayout);
document.getElementById('fit-btn').addEventListener('click', () => { if (cy) cy.fit(undefined, 42); });
document.getElementById('reload-btn').addEventListener('click', async () => {
  await api('/api/reload', { method: 'POST' });
  loadSummary();
  if (cache.focus) focusNode(cache.focus, 1);
});
document.getElementById('dark-btn').addEventListener('click', () => {
  document.body.style.background = document.body.style.background === '#0d1b2a' ? '#1a1a2e' : '#0d1b2a';
});
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  switch(e.key) {
    case '/': e.preventDefault(); document.getElementById('search').focus(); break;
    case 'f': if (cy) cy.fit(undefined, 42); break;
    case 'Escape': if (cy) cy.elements().removeClass('faded'); break;
  }
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
