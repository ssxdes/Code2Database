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

from _builder.utils import normalize_str_field


# ---------------------------------------------------------------------------
# Graph cache — load once at server startup, refresh on demand
# ---------------------------------------------------------------------------

_log = logging.getLogger(__name__)

from _builder.misc.graph_cache import GraphCache

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
        # No 'Access-Control-Allow-Origin: *'. The server binds 127.0.0.1
        # specifically to keep source snippets local, but the wildcard
        # re-opened it to any website in the victim's browser (fetch() to
        # localhost + read response = source exfiltration). The bundled UI
        # is served from this same origin, so it needs no CORS at all.
        self.send_header("X-Content-Type-Options", "nosniff")
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
                    os.path.dirname(__file__), "..", "static", "cytoscape.min.js")
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
                s = self.cache.summary()
                fr = self.cache.freshness()
                if fr is not None:
                    s["freshness"] = fr
                self._send_json(200, s)
                return
            if path.startswith("/api/node/"):
                node_id = urllib.parse.unquote(path[len("/api/node/"):])
                node = self.cache.get_node(node_id)
                if node:
                    # veteran Q&A grounded to this symbol
                    try:
                        node["related_memories"] = \
                            self.cache.memories_for_node(node)
                    except Exception:
                        node["related_memories"] = []
                    self._send_json(200, node)
                else:
                    self._send_json(404, {"error": "node not found"})
                return
            if path.startswith("/api/neighbors/"):
                node_id = urllib.parse.unquote(path[len("/api/neighbors/"):])
                try:
                    depth = int(query.get("depth", ["1"])[0])
                except ValueError:
                    self._send_json(400, {"error": "depth must be an integer"})
                    return
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
                payload = self.cache.get_code_payload(node_id)
                self._send_json(200, payload)
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
                    from _builder.cgdb.cgdb_suggest import analyze_and_suggest
                    suggestions = analyze_and_suggest(self.cache.graph_dir)
                    self._send_json(200, {"suggestions": suggestions})
                except Exception as exc:
                    logging.getLogger(__name__).warning("suggestions failed", exc_info=True)
                    self._send_json(500, {"suggestions": [], "error": "internal error"})
                return
            if path == "/api/tour":
                try:
                    from _builder.cgdb.cgdb_tour import generate_tour
                    import tempfile
                    # Per-request private temp file. The old FIXED path
                    # (/tmp/c2d_tour.md) let two concurrent requests read
                    # while the other truncated the shared file (torn
                    # output), and a pre-created symlink there let a local
                    # attacker overwrite an arbitrary file with tour text.
                    _td = tempfile.mkdtemp(prefix="c2d_tour_")
                    tour_path = generate_tour(self.cache.graph_dir,
                                              output_path=os.path.join(
                                                  _td, "tour.md"))
                    with open(tour_path, "r", encoding="utf-8") as f:
                        tour_content = f.read()
                    try:
                        os.unlink(tour_path)
                        os.rmdir(_td)
                    except OSError:
                        pass
                    self._send_json(200, {"tour": tour_content})
                except Exception as exc:
                    logging.getLogger(__name__).warning("tour generation failed", exc_info=True)
                    self._send_json(500, {"tour": "", "error": "internal error"})
                return
            # --- project context (brief + memory) ---
            if path == "/api/brief":
                try:
                    self._send_json(200, self.cache.brief())
                except Exception as exc:
                    logging.getLogger(__name__).warning("brief failed",
                                                        exc_info=True)
                    self._send_json(500, {"brief": None, "rendered": "",
                                          "error": "internal error"})
                return
            if path == "/api/memory/search":
                q = query.get("q", [""])[0]
                author = query.get("author", [""])[0]
                symbol = query.get("symbol", [""])[0]
                try:
                    top = int(query.get("top", ["10"])[0])
                except ValueError:
                    top = 10
                top = max(1, min(top, 50))
                try:
                    self._send_json(200, self.cache.memory_search(
                        q, top, author, symbol))
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "memory search failed", exc_info=True)
                    self._send_json(500, {"results": [], "stats": {},
                                          "error": "internal error"})
                return
            # --- architecture narrative (ARCHITECTURE_FLOWS.md) ---
            if path == "/api/architecture":
                try:
                    self._send_json(200, self.cache.architecture())
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "architecture failed", exc_info=True)
                    self._send_json(500, {"content": "", "missing": True,
                                          "error": "internal error"})
                return
            # --- memory governance lineage ---
            if path == "/api/memory/lineage":
                try:
                    self._send_json(200, self.cache.memory_lineage())
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "memory lineage failed", exc_info=True)
                    self._send_json(500, {"nodes": [], "edges": [],
                                          "error": "internal error"})
                return
            # --- memory contributors (author filter) ---
            if path == "/api/memory/authors":
                try:
                    self._send_json(200, self.cache.memory_authors())
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "memory authors failed", exc_info=True)
                    self._send_json(500, {"authors": [],
                                          "error": "internal error"})
                return
            self._send_json(404, {"error": f"unknown path {path}"})
        except Exception as exc:
            logging.getLogger(__name__).warning("web_ui handler error", exc_info=True); self._send_json(500, {"error": "internal error"})

    def _origin_allowed(self) -> bool:
        """True unless the request is a cross-origin BROWSER request.

        Non-browser clients (curl, MCP servers, agents) send no Origin
        header and are always allowed. Browsers always attach Origin to
        cross-origin POSTs — reject those so a malicious page can't drive
        /api/reload (or other POSTs) from the victim's browser.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        # Same-origin check against the Host the client connected to.
        host = self.headers.get("Host", "")
        try:
            from urllib.parse import urlparse
            o = urlparse(origin)
            return (o.netloc == host) or (o.hostname in ("127.0.0.1", "localhost")
                                          and host.startswith(("127.0.0.1", "localhost")))
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            return False

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if not self._origin_allowed():
                self._send_json(403, {"error": "cross-origin requests are not allowed"})
                return
            if path == "/api/highlight-path":
                _cl = self.headers.get("Content-Length", 0)
                try:
                    length = int(_cl)
                except (TypeError, ValueError):
                    self._send_json(400, {"error": "invalid Content-Length"})
                    return
                # Cap POST body size to prevent memory blow-up / DoS —
                # a malicious or buggy client can send Content-Length:
                # 9999999999 and the server would attempt to allocate
                # ~10GB. 1 MB is plenty for a highlight-path request.
                # Negative values are rejected too: read(-1) on a socket
                # means 'read until EOF' — the exact unbounded read the
                # cap is meant to prevent.
                if length < 0:
                    self._send_json(400, {"error": "invalid Content-Length"})
                    return
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
        # CORS preflight: same-origin only (see _send_json note — the
        # bundled UI is served from this origin and needs no CORS).
        self.send_response(204)
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
/* prefers-reduced-motion */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
}
/* Dark Mode (OLED) + Swiss Style per ui-ux-pro-max row 82 */
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
/* Typography — JetBrains Mono for code/identifiers */
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

/* Callers/Callees lists in sidebar */
.call-list { margin: 6px 0; }
.call-list-title { font-size: 11px; color: var(--muted); text-transform: uppercase; margin-bottom: 3px; }
.call-item { padding: 3px 6px; border-radius: 4px; cursor: pointer; font-size: 12px;
  display: flex; align-items: center; gap: 4px; min-width: 0; }
.call-item:hover { background: var(--bg); }
.call-item .call-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1; }
.call-item .call-cond { font-size: 10px; color: var(--accent); flex-shrink: 0; }
.call-item .call-conf { font-size: 10px; flex-shrink: 0; }

/* Community legend */
#legend { position: absolute; left: 10px; top: 52px; background: rgba(13,27,42,0.9);
  padding: 0; border-radius: 6px; font-size: 11px; max-height: 300px; overflow-y: auto;
  border: 1px solid var(--border); z-index: var(--z-toolbar); }
#legend.collapsed { max-height: none; overflow: visible; }
#legend.collapsed #legend-body { display: none; }
#legend .legend-header { display: flex; align-items: center; justify-content: space-between;
  gap: 6px; padding: 6px 8px; cursor: pointer; user-select: none; }
#legend .legend-header:hover { background: rgba(255,255,255,0.06); }
#legend .legend-title { font-weight: 600; color: var(--fg); }
#legend .legend-toggle { color: var(--muted-fg); font-size: 10px; flex-shrink: 0; }
#legend-body { padding: 0 8px 6px; }
#legend .legend-item { display: flex; align-items: center; gap: 4px; cursor: pointer; padding: 2px; }
#legend .legend-color { width: 12px; height: 12px; border-radius: 3px; flex-shrink: 0; }
#legend .legend-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#legend .legend-more { padding: 4px 2px; color: var(--primary); cursor: pointer;
  font-size: 10px; text-align: center; }
#legend .legend-more:hover { text-decoration: underline; }

/* Depth slider */
#depth-control { display: flex; align-items: center; gap: 4px; }
#depth-slider { width: 80px; }
#spacing-control { display: flex; align-items: center; gap: 4px; }
#spacing-slider { width: 60px; }

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

/* Help modal */
#help-modal { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.6);
  display: none; align-items: center; justify-content: center; z-index: var(--z-modal); }
#help-content { background: var(--card); padding: 20px; border-radius: 12px; max-width: 500px;
  border: 1px solid var(--border); }
#help-content h2 { margin-top: 0; }
#help-content table { border-collapse: collapse; width: 100%; }
#help-content td { padding: 4px 8px; border-bottom: 1px solid var(--border); font-size: 12px; }
#help-content kbd { background: var(--bg); padding: 1px 5px; border-radius: 3px;
  border: 1px solid var(--border); font-size: 11px; font-family: monospace; }

/* project context modals */
#brief-modal, #memory-modal { position: fixed; top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,0.6); display: none; align-items: center; justify-content: center;
  z-index: var(--z-modal); }
#brief-content { background: var(--card); padding: 20px; border-radius: 12px;
  max-width: 640px; max-height: 80vh; border: 1px solid var(--border);
  display: flex; flex-direction: column; }
#memory-content { background: var(--card); padding: 20px; border-radius: 12px;
  max-width: 640px; max-height: 80vh; border: 1px solid var(--border);
  display: flex; flex-direction: column; }
.ctx-body { white-space: pre-wrap; word-break: break-word; overflow-y: auto;
  font-size: 12px; line-height: 1.5; margin: 0; flex: 1; text-align: left; }
#memory-search-row { display: flex; gap: 6px; margin-bottom: 8px; }
#memory-search-input { flex: 1; padding: 4px 8px; border-radius: 4px;
  border: 1px solid var(--border); background: var(--bg); color: var(--fg);
  font-size: 12px; }
.memory-stats { font-size: 11px; color: var(--muted-fg); margin-bottom: 8px; }
#memory-results { overflow-y: auto; flex: 1; text-align: left; }
.mem-item { padding: 8px; border: 1px solid var(--border); border-radius: 6px;
  margin-bottom: 6px; font-size: 12px; }
.mem-q { font-weight: 600; margin-bottom: 2px; }
.mem-a { color: var(--muted-fg); margin-bottom: 4px; white-space: pre-wrap;
  word-break: break-word; }
.mem-meta { font-size: 10px; color: var(--muted-fg); text-transform: uppercase;
  letter-spacing: 0.5px; }

/* Right-click context menu */
#ctx-menu { position: fixed; display: none; background: var(--card); border: 1px solid var(--border);
  border-radius: 6px; padding: 4px 0; z-index: var(--z-modal); min-width: 140px; }
#ctx-menu .ctx-item { padding: 6px 12px; cursor: pointer; font-size: 12px; }
#ctx-menu .ctx-item:hover { background: var(--bg); }

/* Node type filter */
#filter-panel { display: none; position: absolute; right: 330px; top: 52px;
  background: var(--card); padding: 8px; border-radius: 6px; border: 1px solid var(--border);
  z-index: var(--z-toolbar); }
#filter-panel label { display: flex; align-items: center; gap: 4px; font-size: 11px; cursor: pointer; }
#search-results { display: none; position: absolute; top: 100%; left: 0; right: 0; z-index: 60;
  background: var(--card); border: 1px solid var(--border); border-radius: 6px;
  margin-top: 2px; max-height: 260px; overflow-y: auto; box-shadow: 0 8px 24px rgba(0,0,0,.45); }
#search-results .sr-item { padding: 6px 10px; cursor: pointer; font-size: 12px;
  display: flex; justify-content: space-between; gap: 10px; align-items: center; }
#search-results .sr-item:hover, #search-results .sr-item.active { background: var(--primary); color: #fff; }
#search-results .sr-badge { font-size: 9px; padding: 1px 5px; border-radius: 3px;
  background: var(--accent); color: #000; text-transform: uppercase; letter-spacing: 0.3px;
  flex-shrink: 0; }
#search-results .sr-loc { color: #6b7280; font-size: 10px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; max-width: 55%; }
#search-results .sr-item:hover .sr-loc, #search-results .sr-item.active .sr-loc { color: #dbeafe; }
.call-loc { font-size: 10px; color: #6b7280; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; max-width: 180px; }

/* Lightweight minimap — canvas-based, no external dependency.
   Shows node positions as dots and the current viewport as a
   rectangle; click to pan. */
#minimap-wrap { position: absolute; right: 320px; bottom: 10px; width: 140px; height: 100px;
  background: rgba(13,27,42,0.9); border: 1px solid var(--border); border-radius: 6px;
  z-index: var(--z-toolbar); display: none; cursor: crosshair; }
#minimap-canvas { width: 100%; height: 100%; display: block; }

/* Edge-type legend — compact key for call / import / ffi edges */
#edge-legend { position: absolute; left: 10px; bottom: 32px;
  background: rgba(13,27,42,0.9); padding: 6px 8px; border-radius: 6px;
  font-size: 10px; border: 1px solid var(--border); z-index: var(--z-toolbar);
  display: none; }
#edge-legend .edge-legend-title { color: var(--muted-fg); margin-bottom: 3px;
  font-size: 9px; text-transform: uppercase; letter-spacing: 0.5px; }
#edge-legend .edge-legend-item { display: flex; align-items: center; gap: 4px; padding: 1px 0; }
#edge-legend .edge-swatch { width: 20px; height: 0; border-top-width: 2px;
  border-top-style: solid; flex-shrink: 0; }

/* Source code panel — replaces the alert()-based viewer.
   Sits alongside the sidebar (right side), supports full scroll +
   text selection, and shows the file path + line range so users can
   locate the snippet in their editor. */
#code-panel { position: absolute; right: 320px; top: 42px; bottom: 0; width: 480px;
  background: var(--card); border-left: 1px solid var(--border);
  border-right: 1px solid var(--border); display: none; flex-direction: column;
  z-index: var(--z-sidebar); }
#code-panel.visible { display: flex; }
#code-panel-header { display: flex; align-items: center; justify-content: space-between;
  padding: 6px 10px; border-bottom: 1px solid var(--border); gap: 6px; }
#code-panel-title { font-size: 11px; color: var(--muted-fg); overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; flex: 1; font-family: monospace; }
#code-panel-actions { display: flex; gap: 4px; flex-shrink: 0; }
#code-panel-actions button { padding: 2px 8px; background: var(--primary); color: #fff;
  border: none; border-radius: 4px; cursor: pointer; font-size: 11px; }
#code-panel-actions button:hover { opacity: 0.85; }
#code-content { white-space: pre; font-family: "JetBrains Mono", "Fira Code", monospace;
  font-size: 12px; line-height: 1.5; overflow: auto; padding: 8px 12px;
  color: var(--fg); flex: 1; user-select: text; }
#code-empty { padding: 20px; color: var(--muted-fg); font-size: 12px; text-align: center; }
</style>
</head>
<body>
<a href="#cy" class="skip-link">Skip to graph</a>
<div id="topbar">
  <h1>Code2Database</h1>
  <div id="search-wrap" style="position:relative; flex:1; display:flex; gap:4px;">
    <input id="search" placeholder="Search function..." aria-label="Search functions"
      autocomplete="off" style="flex:1; min-width:180px;" />
    <button id="search-btn" aria-label="Search">Search</button>
    <div id="search-results" role="listbox" aria-label="Search results"></div>
  </div>
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
  <div id="spacing-control">
    <label for="spacing-slider" style="font-size:11px">Spacing</label>
    <input type="range" id="spacing-slider" min="50" max="200" value="100" step="10" aria-label="Layout spacing" />
  </div>
  <button id="fit-btn" aria-label="Fit graph to screen">Fit</button>
  <button id="png-btn" aria-label="Export as PNG">PNG</button>
  <button id="json-btn" aria-label="Export visible subgraph as JSON">JSON</button>
  <button id="filter-btn" aria-pressed="false">Filter</button>
  <button id="cycle-btn" aria-pressed="false">Cycles</button>
  <button id="reload-btn" aria-label="Reload graph">Reload</button>
  <button id="brief-btn" aria-label="Show project brief">Brief</button>
  <button id="memory-btn" aria-label="Search veteran Q&A memory">Memory</button>
  <button id="arch-btn" aria-label="Show architecture narrative">Arch</button>
  <button id="help-btn" aria-label="Help">?</button>
  <button id="dark-btn" aria-pressed="true">Dark</button>
</div>
<div id="cy" role="application" aria-label="Code graph visualization" tabindex="0"></div>
<div id="sidebar" role="complementary" aria-label="Node details">
  <h2 id="node-title">Select a node</h2>
  <div id="node-details"></div>
</div>
<div id="legend" style="display:none"></div>
<div id="code-panel" role="complementary" aria-label="Source code">
  <div id="code-panel-header">
    <span id="code-panel-title">Source</span>
    <span id="code-panel-actions">
      <button onclick="copyCode()" aria-label="Copy code">Copy</button>
      <button onclick="closeCodePanel()" aria-label="Close panel">&times;</button>
    </span>
  </div>
  <pre id="code-content"></pre>
  <div id="code-empty" style="display:none">No source available</div>
</div>
<div id="filter-panel">
  <label><input type="checkbox" class="filter-label" value="API_entry" checked> API Entry</label>
  <label><input type="checkbox" class="filter-label" value="out_end" checked> External</label>
  <label><input type="checkbox" class="filter-label" value="callback_func" checked> Callback</label>
  <label><input type="checkbox" class="filter-label" value="__default" checked> Other</label>
</div>
<div id="stats"></div>
<div id="edge-legend">
  <div class="edge-legend-title">Edge Types</div>
  <div class="edge-legend-item"><span class="edge-swatch" style="border-color:#64748b"></span>Call</div>
  <div class="edge-legend-item"><span class="edge-swatch" style="border-color:#f59e0b;border-top-style:dashed"></span>Import</div>
  <div class="edge-legend-item"><span class="edge-swatch" style="border-color:#a855f7;border-top-style:dotted"></span>FFI</div>
</div>
<div id="minimap-wrap" aria-label="Minimap"><canvas id="minimap-canvas"></canvas></div>
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
      <tr><td><kbd>J</kbd></td><td>Export visible subgraph as JSON</td></tr>
      <tr><td>Click node</td><td>Focus + show callers/callees</td></tr>
      <tr><td>Right-click node</td><td>Context menu (Focus / Expand / Collapse / Collapse All / Impact / Code / Copy)</td></tr>
    </table>
    <p style="text-align:center;margin-top:12px"><button class="action-btn" onclick="document.getElementById('help-modal').style.display='none'">Close</button></p>
  </div>
</div>
<!-- project context modals (brief + veteran memory) -->
<div id="brief-modal" role="dialog" aria-label="Project brief" aria-modal="true">
  <div id="brief-content">
    <h2>Project Brief</h2>
    <pre id="brief-body" class="ctx-body">Loading…</pre>
    <p style="text-align:center;margin-top:12px"><button class="action-btn" onclick="document.getElementById('brief-modal').style.display='none'">Close</button></p>
  </div>
</div>
<div id="memory-modal" role="dialog" aria-label="Memory search" aria-modal="true">
  <div id="memory-content">
    <h2>Memory — Veteran Q&amp;A</h2>
    <div id="memory-search-row">
      <input id="memory-search-input" placeholder="Search Q&A (empty = top by weight)" aria-label="Search memory" />
      <input id="memory-symbol-input" placeholder="symbol (e.g. nvme_submit_cmd)" aria-label="Filter by symbol" style="max-width:180px" />
      <select id="memory-author-select" aria-label="Filter by author">
        <option value="">All authors</option>
      </select>
      <button id="memory-search-btn" class="action-btn">Search</button>
      <button id="memory-lineage-btn" class="action-btn" aria-label="Show split/merge lineage">Lineage</button>
    </div>
    <div id="memory-stats" class="memory-stats"></div>
    <div id="memory-results"></div>
    <p style="text-align:center;margin-top:12px"><button class="action-btn" onclick="document.getElementById('memory-modal').style.display='none'">Close</button></p>
  </div>
</div>
<!-- architecture narrative modal (ARCHITECTURE_FLOWS.md) -->
<div id="arch-modal" role="dialog" aria-label="Architecture narrative" aria-modal="true">
  <div id="arch-content">
    <h2>Architecture — Core Execution Flows</h2>
    <pre id="arch-body" class="ctx-body">Loading…</pre>
    <p style="text-align:center;margin-top:12px"><button class="action-btn" onclick="document.getElementById('arch-modal').style.display='none'">Close</button></p>
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
// Expand-tree: nodeId -> Set<childId> of nodes introduced by
// expanding nodeId. Used by collapseNode/collapseAll to remove
// only the nodes a particular expand brought in, preserving
// nodes shared across multiple expand paths.
let expandChildren = {};

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
// Safe interpolation of a string into an inline JS handler attribute
// (onclick="fn(...)"). escapeHtml alone is WRONG there: the HTML parser
// decodes entities BEFORE the JS engine parses the attribute, so
// escapeHtml("x');alert(1);//") breaks back out of the JS string.
// JSON.stringify quotes/escapes for the JS layer, escapeHtml for the
// HTML-attribute layer. Usage: onclick="fn(' + jsAttr(v) + ')" — note
// jsAttr already includes the surrounding double quotes.
function jsAttr(s) { return escapeHtml(JSON.stringify(String(s == null ? '' : s))); }

async function loadSummary() {
  const s = await api('/api/graph/summary');
  window._lastSummary = s;
  let statsHtml = s.node_count + ' nodes · ' + s.edge_count + ' edges';
  // Staleness badge: source files vs scan manifest
  if (s.freshness) {
    if (s.freshness.is_fresh) {
      statsHtml += ' · <span style="color:#4a4">fresh</span>';
    } else {
      const changed = (s.freshness.changed_count || 0) + (s.freshness.new_count || 0) + (s.freshness.deleted_count || 0);
      statsHtml += ' · <span style="color:#c33;font-weight:bold" title="' + escapeHtml(s.freshness.recommendation || '') + '">STALE (' + changed + ' files)</span>';
    }
  }
  document.getElementById('stats').innerHTML = statsHtml;
  window._lastStatsHtml = statsHtml;
  // Build community legend — collapsible to avoid occluding the canvas
  // when a project has many domains (libstorage yields dozens). Default
  // collapses above 8 entries; the "+N more" row expands inline.
  const legend = document.getElementById('legend');
  const communities = s.communities || [];
  const LEGEND_PREVIEW = 10;
  const lh = '<div class="legend-header" onclick="toggleLegend()">' +
      '<span class="legend-title">Domains (' + communities.length + ')</span>' +
      '<span class="legend-toggle" id="legend-toggle">' +
      (communities.length > 8 ? '▶' : '▼') + '</span></div>' +
      '<div id="legend-body">' +
      communities.slice(0, LEGEND_PREVIEW).map((c, i) => {
        const color = COMMUNITY_COLORS[i % COMMUNITY_COLORS.length];
        return '<div class="legend-item" onclick="toggleCommunity(' + jsAttr(c.id) + ')"' +
          ' title="' + escapeHtml(c.label || c.id) + '">' +
          '<div class="legend-color" style="background:' + color + '"></div>' +
          '<span class="legend-label">' + escapeHtml(c.label || c.id) + ' (' + c.node_count + ')</span></div>';
      }).join('') +
      (communities.length > LEGEND_PREVIEW
        ? '<div class="legend-more" onclick="expandLegend(event)">+' +
          (communities.length - LEGEND_PREVIEW) + ' more</div>' : '') +
      '</div>';
  legend.innerHTML = lh;
  legend.style.display = communities.length ? 'block' : 'none';
  // Default-collapse when there are many communities so the legend
  // doesn't dominate the upper-left corner on first load.
  if (communities.length > 8) {
    legend.classList.add('collapsed');
  } else {
    legend.classList.remove('collapsed');
  }
}

function toggleLegend() {
  const legend = document.getElementById('legend');
  const toggle = document.getElementById('legend-toggle');
  if (!legend) return;
  const collapsed = legend.classList.toggle('collapsed');
  if (toggle) toggle.textContent = collapsed ? '▶' : '▼';
}

function expandLegend(ev) {
  if (ev) ev.stopPropagation();
  const legend = document.getElementById('legend');
  if (!legend) return;
  const body = document.getElementById('legend-body');
  if (!body) return;
  // Re-render the full community list, dropping the "+N more" row.
  const s = window._lastSummary;
  if (!s || !s.communities) return;
  body.innerHTML = s.communities.map((c, i) => {
    const color = COMMUNITY_COLORS[i % COMMUNITY_COLORS.length];
    return '<div class="legend-item" onclick="toggleCommunity(' + jsAttr(c.id) + ')"' +
      ' title="' + escapeHtml(c.label || c.id) + '">' +
      '<div class="legend-color" style="background:' + color + '"></div>' +
      '<span class="legend-label">' + escapeHtml(c.label || c.id) + ' (' + c.node_count + ')</span></div>';
  }).join('');
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

// Edge type + confidence styling
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
    // Skip dangling edges: /api/neighbors deliberately returns edges
    // to depth-boundary nodes that are NOT part of the node set, and
    // cytoscape throws "nonexistant source/target" on those — the
    // exception escapes initCy() and leaves the canvas silently blank.
    if (!allNodes[edge.source] || !allNodes[edge.target]) continue;
    eles.push({
      data: { id: key, source: edge.source, target: edge.target,
              relation: edge.relation || 'INVOKES',
              condition: edge.call_condition || '',
              confidence: edge.confidence || 'EXTRACTED' },
      classes: edgeClasses(edge)
    });
  }
  return eles;
}

// Force-sim auto-tuning by node count, with a manual spacing
// multiplier (0.5x–2x) that lets users tighten or loosen the layout
// without changing the algorithm.
function getLayoutOptions() {
  const n = Object.keys(allNodes).length;
  const name = document.getElementById('layout-select').value;
  const spacingMult = (parseInt(
    (document.getElementById('spacing-slider') || {}).value || '100', 10)) / 100;
  const opts = { name: name, animate: n < 200, padding: 42 };
  if (name === 'cose') {
    opts.nodeRepulsion = (n > 100 ? 4000 : 10000) * spacingMult;
    opts.idealEdgeLength = (n > 100 ? 50 : 100) * spacingMult;
    opts.gravity = n > 200 ? 0.3 : 0.1;
  }
  if (name === 'breadthfirst') {
    opts.spacingFactor = (n > 50 ? 1.0 : 1.2) * spacingMult;
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
      // Edge type styling
      { selector: 'edge.call-edge', style: { 'width': 2, 'line-color': '#64748b', 'curve-style': 'bezier',
        'target-arrow-shape': 'triangle', 'arrow-scale': 0.8, 'opacity': 0.6 } },
      { selector: 'edge.import-edge', style: { 'line-color': '#f59e0b', 'line-style': 'dashed', 'opacity': 0.5 } },
      { selector: 'edge.ffi-edge', style: { 'line-color': '#a855f7', 'line-style': 'dotted', 'opacity': 0.6 } },
      // Edge confidence
      { selector: 'edge.inferred', style: { 'line-style': 'dashed', 'opacity': 0.4 } },
      { selector: 'edge.ambiguous', style: { 'line-style': 'dotted', 'opacity': 0.3 } },
      // Cycle detection
      { selector: 'edge.cycle-edge', style: { 'line-color': '#ef4444', 'line-style': 'dashed', 'width': 3 } },
      // Highlight
      { selector: 'edge.highlighted', style: { 'width': 4, 'line-color': '#f59e0b', 'opacity': 0.9 } },
      { selector: '.faded', style: { 'opacity': 0.12 } },
      // Label zoom threshold
      { selector: 'node', style: { 'text-opacity': 0 } },
      { selector: 'node[degree > 0]', style: { 'text-opacity': 1 } },
      { selector: 'edge.show-condition', style: { 'label': 'data(condition)', 'font-size': '6px',
        'color': '#f59e0b', 'text-rotation': 'autorotate', 'opacity': 0.7 } },
    ],
    layout: getLayoutOptions(),
    wheelSensitivity: 0.2,
  });
  // Node click → callers/callees panel
  cy.on('tap', 'node', function(evt) {
    activeNodeId = evt.target.id();
    focusNode(evt.target.id(), parseInt(document.getElementById('depth-slider').value));
  });
  cy.on('tap', function(evt) {
    if (evt.target === cy) { cy.elements().removeClass('faded'); }
  });
  // Right-click context menu
  cy.on('cxttap', 'node', function(evt) {
    showContextMenu(evt.target.id(), evt.originalEvent.clientX, evt.originalEvent.clientY);
  });
  // Edge hover tooltip — shows relation + confidence + condition
  // so users can identify edge semantics without zooming in.
  cy.on('mouseover', 'edge', function(evt) {
    const e = evt.target;
    const rel = e.data('relation') || 'INVOKES';
    const conf = e.data('confidence') || 'EXTRACTED';
    const cond = e.data('condition') || '';
    let tip = rel + ' · ' + conf;
    if (cond) tip += ' · ' + cond;
    document.getElementById('stats').textContent = tip;
  });
  cy.on('mouseout', 'edge', function() {
    if (window._lastStatsHtml) {
      document.getElementById('stats').innerHTML = window._lastStatsHtml;
    }
  });
  // Label zoom threshold
  cy.on('zoom', function() {
    const z = cy.zoom();
    if (z < 0.5) {
      cy.style().selector('node').style('text-opacity', 0).update();
    } else {
      cy.style().selector('node').style('text-opacity', 1).update();
    }
    drawMinimap();
  });
  cy.on('pan', function() { drawMinimap(); });
  // Apply community colors
  applyCommunityColors();
  initMinimap();
  drawMinimap();
}

// Community coloring
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

// --- Lightweight minimap ---
// Canvas-based, no external JS dependency. Redraws on layout (node
// positions change) and pan/zoom (viewport rectangle moves).
function drawMinimap() {
  const wrap = document.getElementById('minimap-wrap');
  const canvas = document.getElementById('minimap-canvas');
  if (!wrap || !canvas || !cy) return;
  if (Object.keys(allNodes).length === 0) { wrap.style.display = 'none'; return; }
  wrap.style.display = 'block';
  const ctx = canvas.getContext('2d');
  const w = canvas.width = wrap.clientWidth;
  const h = canvas.height = wrap.clientHeight;
  ctx.clearRect(0, 0, w, h);
  // Compute the bounding box of all rendered nodes.
  const nodes = cy.nodes();
  if (nodes.length === 0) return;
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  nodes.forEach(n => {
    const p = n.position();
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  });
  const rangeX = (maxX - minX) || 1;
  const rangeY = (maxY - minY) || 1;
  const pad = 6;
  const scaleX = (w - 2 * pad) / rangeX;
  const scaleY = (h - 2 * pad) / rangeY;
  const scale = Math.min(scaleX, scaleY);
  const offX = pad + (w - 2 * pad - rangeX * scale) / 2;
  const offY = pad + (h - 2 * pad - rangeY * scale) / 2;
  // Draw nodes as dots.
  ctx.fillStyle = '#4a90e2';
  nodes.forEach(n => {
    const p = n.position();
    const x = offX + (p.x - minX) * scale;
    const y = offY + (p.y - minY) * scale;
    ctx.fillRect(x - 0.5, y - 0.5, 1.5, 1.5);
  });
  // Draw the viewport rectangle.
  const vp = cy.extent();
  const vx = offX + (vp.x1 - minX) * scale;
  const vy = offY + (vp.y1 - minY) * scale;
  const vw = vp.w * scale;
  const vh = vp.h * scale;
  ctx.strokeStyle = '#f59e0b';
  ctx.lineWidth = 1;
  ctx.strokeRect(vx, vy, vw, vh);
}

function initMinimap() {
  const wrap = document.getElementById('minimap-wrap');
  if (!wrap || !cy) return;
  // Click-to-pan: clicking on the minimap centers the main viewport
  // on the corresponding graph position.
  wrap.addEventListener('click', function(ev) {
    if (!cy) return;
    const canvas = document.getElementById('minimap-canvas');
    const rect = canvas.getBoundingClientRect();
    const px = (ev.clientX - rect.left) / rect.width * canvas.width;
    const py = (ev.clientY - rect.top) / rect.height * canvas.height;
    // Reverse-map the minimap pixel back to graph coordinates.
    const nodes = cy.nodes();
    if (nodes.length === 0) return;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    nodes.forEach(n => {
      const p = n.position();
      if (p.x < minX) minX = p.x;
      if (p.x > maxX) maxX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.y > maxY) maxY = p.y;
    });
    const rangeX = (maxX - minX) || 1;
    const rangeY = (maxY - minY) || 1;
    const pad = 6;
    const scale = Math.min((canvas.width - 2*pad)/rangeX, (canvas.height - 2*pad)/rangeY);
    const offX = pad + (canvas.width - 2*pad - rangeX*scale) / 2;
    const offY = pad + (canvas.height - 2*pad - rangeY*scale) / 2;
    const gx = minX + (px - offX) / scale;
    const gy = minY + (py - offY) / scale;
    cy.panBy({ x: cy.width()/2 - cy.cyViewport().width/2, y: 0 });
    cy.center({ x: gx, y: gy });
    drawMinimap();
  });
}

function syncCyFromModel() {
  if (!cy) { initCy(); return; }
  const currentIds = new Set(cy.nodes().map(n => n.id()));
  const modelIds = new Set(Object.keys(allNodes));
  for (const id of modelIds) {
    if (!currentIds.has(id)) {
      const node = allNodes[id];
      cy.add({ data: { id: id, name: node.name || id, labels: node.labels || [],
        degree: node.degree || 0, community: node.community || node.domain || '' },
        classes: nodeClasses(node) });
    }
  }
  for (const id of currentIds) {
    if (!modelIds.has(id)) { cy.remove('#' + id); }
  }
  // Edge sync. The old code synced nodes only: after the first
  // initCy() no edge was ever added, so exploration rendered
  // edgeless graphs and the cycle toggle never restyled anything.
  const currentEdgeIds = new Set(cy.edges().map(e => e.id()));
  for (const key in allEdges) {
    const edge = allEdges[key];
    if (!allNodes[edge.source] || !allNodes[edge.target]) continue;
    if (!currentEdgeIds.has(key)) {
      cy.add({ data: { id: key, source: edge.source, target: edge.target,
              relation: edge.relation || 'INVOKES',
              condition: edge.call_condition || '',
              confidence: edge.confidence || 'EXTRACTED' },
              classes: edgeClasses(edge) });
    } else {
      // Restyle in place (cycle toggle / future highlight-path).
      cy.getElementById(key).classes(edgeClasses(edge));
    }
  }
  runLayout();
  applyCommunityColors();
  drawMinimap();
  // Show the edge-type legend once the graph has edges.
  const edgeLegend = document.getElementById('edge-legend');
  if (edgeLegend) {
    edgeLegend.style.display = Object.keys(allEdges).length > 0 ? 'block' : 'none';
  }
  // Update stats to show visible vs total so users know how much of
  // the graph they're currently viewing after focus/expand/collapse.
  const visN = Object.keys(allNodes).length;
  const visE = Object.keys(allEdges).length;
  const base = window._lastStatsHtml || '';
  const composed = base
    ? base + ' · showing ' + visN + ' nodes / ' + visE + ' edges'
    : visN + ' nodes · ' + visE + ' edges';
  document.getElementById('stats').innerHTML = composed;
  window._lastStatsHtml = base;
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
    // Snapshot existing node IDs so we can record which nodes this
    // particular focus/expand call introduced — collapseNode uses
    // this map to remove only the nodes the expand brought in.
    const _priorIds = new Set(Object.keys(allNodes));
    if (!expandChildren[nodeId]) expandChildren[nodeId] = new Set();
    // Fetch the degree map ONCE (the old per-node loop issued one
    // /api/degrees request per neighbor — up to 200 identical requests,
    // each recomputing every node's degree under the global lock).
    let degreeMap = {};
    try { degreeMap = (await api('/api/degrees')).degrees || {}; } catch (e) { console.warn(e); }
    for (const n of (data.nodes || [])) {
      const deg = degreeMap[n.id] || 0;
      allNodes[n.id] = { ...n, degree: deg,
        community: n.community || n.domain || '' };
      if (!_priorIds.has(n.id) && n.id !== nodeId) {
        expandChildren[nodeId].add(n.id);
      }
    }
    for (const e of (data.edges || [])) {
      allEdges[e.source + '->' + e.target] = e;
    }
    // Surface the 200-node view cap instead of failing silently.
    if (data.truncated) {
      document.getElementById('stats').textContent =
        'View truncated at 200 nodes — lower the depth for detail';
    }
    syncCyFromModel();
    loadNodeDetails(nodeId);
    applyFocusContext(nodeId);
  } finally { hideLoading(); }
}

// Collapse a node's expand-children: remove the neighbors it brought
// in, preserving nodes that another expand path also reaches. This
// lets users undo a single Expand without a full Reload.
function _countExpandParents(childId) {
  let cnt = 0;
  for (const pid in expandChildren) {
    if (expandChildren[pid] && expandChildren[pid].has(childId)) cnt++;
  }
  return cnt;
}

function collapseNode(nodeId) {
  const children = expandChildren[nodeId];
  if (!children || children.size === 0) return;
  // Iterate over a copy — we mutate the set during recursion.
  const kids = Array.from(children);
  for (const childId of kids) {
    children.delete(childId);
    // Only remove the child if no other expand path reaches it AND
    // it's not the currently focused node (which the user is
    // actively inspecting).
    if (_countExpandParents(childId) === 0 && childId !== activeNodeId) {
      // Recursively collapse the child's own subtree first.
      collapseNode(childId);
      delete expandChildren[childId];
      delete allNodes[childId];
      // Remove every edge touching the child.
      for (const key in allEdges) {
        const e = allEdges[key];
        if (e.source === childId || e.target === childId) {
          delete allEdges[key];
        }
      }
    }
  }
  syncCyFromModel();
}

// Collapse everything except the focused node, then re-expand at
// depth 1 for a clean starting view — no backend Reload needed.
function collapseAll() {
  if (!activeNodeId) return;
  allNodes = {};
  allEdges = {};
  expandChildren = {};
  syncCyFromModel();
  focusNode(activeNodeId, 1);
}

// Click node → callers/callees detail panel
async function loadNodeDetails(nodeId) {
  try {
    const [node, callers, callees] = await Promise.all([
      api('/api/node/' + encodeURIComponent(nodeId)),
      api('/api/callers/' + encodeURIComponent(nodeId)),
      api('/api/callees/' + encodeURIComponent(nodeId)),
    ]);
    document.getElementById('node-title').textContent = node.name || nodeId;
    let html = '';
    // Degree summary: in (callers) + out (callees) = total. Shows
    // the node's connectivity at a glance without scanning the lists.
    const inDeg = (callers.callers || []).length;
    const outDeg = (callees.callees || []).length;
    // Edge-confidence breakdown across both directions — surfaces
    // how much of the node's connectivity is inferred vs extracted.
    const confCounts = { EXTRACTED: 0, INFERRED: 0, AMBIGUOUS: 0 };
    (callers.callers || []).concat(callees.callees || []).forEach(c => {
      const k = c.confidence || 'EXTRACTED';
      confCounts[k] = (confCounts[k] || 0) + 1;
    });
    const confParts = Object.entries(confCounts)
      .filter(([_, n]) => n > 0)
      .map(([k, n]) => n + ' ' + k);
    const fields = [
      ['Domain', node.domain], ['Labels', (node.labels||[]).join(', ')],
      ['Location', node.location], ['Signature', node.signature],
      ['Degree', inDeg + outDeg > 0 ? (inDeg + ' in / ' + outDeg + ' out') : ''],
      ['Edge Confidence', confParts.length ? confParts.join(' · ') : ''],
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
        html += '<div class="call-item" onclick="focusNode(' + jsAttr(c.id) + ',' +
          (document.getElementById('depth-slider').value) + ')">' +
          '<span class="call-name mono">' + escapeHtml(c.name) + '</span>' +
          (c.source_file ? '<span class="call-loc" title="' +
            escapeHtml(c.source_file + ':' + (c.line || 0)) + '">' +
            escapeHtml(shortLoc(c.source_file, c.line)) + '</span>' : '') +
          (c.call_condition ? '<span class="call-cond">' + escapeHtml(c.call_condition.substring(0,20)) + '</span>' : '') +
          (c.confidence !== 'EXTRACTED' ? '<span class="call-conf" style="color:#f59e0b">' + escapeHtml(String(c.confidence || '').substring(0,3)) + '</span>' : '') +
          '</div>';
      });
      html += '</div>';
    }
    // Callees list
    if (callees.callees && callees.callees.length > 0) {
      html += '<div class="call-list"><div class="call-list-title">Callees (' + callees.callees.length + ')</div>';
      callees.callees.forEach(c => {
        html += '<div class="call-item" onclick="focusNode(' + jsAttr(c.id) + ',' +
          (document.getElementById('depth-slider').value) + ')">' +
          '<span class="call-name mono">' + escapeHtml(c.name) + '</span>' +
          (c.source_file ? '<span class="call-loc" title="' +
            escapeHtml(c.source_file + ':' + (c.line || 0)) + '">' +
            escapeHtml(shortLoc(c.source_file, c.line)) + '</span>' : '') +
          (c.call_condition ? '<span class="call-cond">' + escapeHtml(c.call_condition.substring(0,20)) + '</span>' : '') +
          (c.confidence !== 'EXTRACTED' ? '<span class="call-conf" style="color:#f59e0b">' + escapeHtml(String(c.confidence || '').substring(0,3)) + '</span>' : '') +
          '</div>';
      });
      html += '</div>';
    }
    // veteran Q&A grounded to this symbol (memory ↔ code)
    if (node.related_memories && node.related_memories.length > 0) {
      html += '<div class="call-list"><div class="call-list-title">Veteran Memories (' + node.related_memories.length + ')</div>';
      node.related_memories.forEach(m => {
        const meta = [m.author, 'w=' + m.weight, m.category]
          .filter(Boolean).join(' · ');
        html += '<div class="call-item" style="flex-direction:column;align-items:flex-start;gap:2px">' +
          '<span class="call-name">' + escapeHtml(m.question) + '</span>' +
          '<span style="font-size:11px;color:#9ca3af">' + escapeHtml(m.answer.substring(0,160)) + '</span>' +
          (meta ? '<span style="font-size:10px;color:#6b7280">' + escapeHtml(meta) + '</span>' : '') +
          '</div>';
      });
      html += '</div>';
    }
    // Action buttons
    html += '<div class="action-btns">' +
      '<button class="action-btn" onclick="loadCode(' + jsAttr(nodeId) + ')">View Code</button>' +
      '<button class="action-btn" onclick="loadImpact(' + jsAttr(nodeId) + ')">Impact</button>' +
      '<button class="action-btn" onclick="exportPNG()">PNG</button>' +
      '</div>';
    document.getElementById('node-details').innerHTML = html;
  } catch(e) { console.error(e); }
}

async function loadCode(nodeId) {
  const data = await api('/api/code?node=' + encodeURIComponent(nodeId));
  const panel = document.getElementById('code-panel');
  const content = document.getElementById('code-content');
  const empty = document.getElementById('code-empty');
  const title = document.getElementById('code-panel-title');
  const code = data.code || '';
  if (code) {
    content.textContent = code;
    content.style.display = 'block';
    empty.style.display = 'none';
  } else {
    content.style.display = 'none';
    empty.style.display = 'block';
  }
  // Show file path + line range in the title bar so users can jump
  // to the same location in their editor. The backend returns the
  // source file and the node's starting line.
  const fileLabel = data.file ? data.file.split('/').pop() : '';
  const lineLabel = data.line ? (':' + data.line) : '';
  title.textContent = fileLabel ? (fileLabel + lineLabel) : (nodeId || 'Source');
  title.title = data.file ? (data.file + lineLabel) : '';
  panel.classList.add('visible');
}

function closeCodePanel() {
  document.getElementById('code-panel').classList.remove('visible');
}

function copyCode() {
  const content = document.getElementById('code-content');
  if (!content.textContent) return;
  navigator.clipboard.writeText(content.textContent).then(() => {
    const btn = event.target;
    const orig = btn.textContent;
    btn.textContent = 'Copied';
    setTimeout(() => { btn.textContent = orig; }, 1200);
  });
}

// Blast-radius overlay (impact as visual highlight)
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
    // Replace (not append to) the impact line — the old innerHTML +=
    // stacked one "N affected" row per click.
    const sidebar = document.getElementById('node-details');
    const old = document.getElementById('impact-line');
    if (old) old.remove();
    const div = document.createElement('div');
    div.id = 'impact-line';
    div.className = 'call-list';
    let title = 'Impact: ' + (data.affected_count||0) + ' affected';
    if (data.truncated) title += ' (showing first 200)';
    if (data.max_depth) title += ' · depth ≤ ' + data.max_depth;
    div.innerHTML = '<div class="call-list-title">' + escapeHtml(title) + '</div>';
    sidebar.appendChild(div);
  } finally { hideLoading(); }
}

// PNG export
function exportPNG() {
  if (!cy) return;
  const png64 = cy.png({ full: true, scale: 2, bg: '#0d1b2a' });
  const a = document.createElement('a');
  a.href = png64;
  a.download = 'code2database_graph.png';
  a.click();
}

// JSON export — dump the current visible subgraph (allNodes +
// allEdges) so users can share/import the explored view without a
// backend round-trip.
function exportJSON() {
  const nodes = Object.values(allNodes).map(n => ({
    id: n.id, name: n.name || n.id, labels: n.labels || [],
    domain: n.domain || '', community: n.community || '',
    degree: n.degree || 0,
  }));
  const edges = Object.values(allEdges).map(e => ({
    source: e.source, target: e.target,
    relation: e.relation || 'INVOKES',
    confidence: e.confidence || 'EXTRACTED',
    condition: e.call_condition || '',
  }));
  const data = {
    focus: cache.focus || null,
    node_count: nodes.length,
    edge_count: edges.length,
    nodes: nodes,
    edges: edges,
  };
  const blob = new Blob([JSON.stringify(data, null, 2)],
    { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'code2database_subgraph.json';
  a.click();
  URL.revokeObjectURL(url);
}

// Right-click context menu
function showContextMenu(nodeId, x, y) {
  const menu = document.getElementById('ctx-menu');
  menu.innerHTML = '';
  const items = [
    { label: 'Focus', action: () => focusNode(nodeId, 1) },
    { label: 'Expand (depth 2)', action: () => focusNode(nodeId, 2) },
    { label: 'Collapse', action: () => collapseNode(nodeId) },
    { label: 'Collapse All', action: () => collapseAll() },
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

// Cycle detection toggle
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

// Community legend toggle
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

// Dark mode toggle — persists to localStorage so a page refresh
// keeps the user's preference instead of always defaulting to dark.
function toggleDark() {
  const root = document.documentElement;
  const btn = document.getElementById('dark-btn');
  const isLight = root.classList.contains('light');
  if (isLight) {
    root.classList.remove('light');
    btn.setAttribute('aria-pressed', 'true');
    btn.textContent = 'Dark';
    try { localStorage.setItem('c2d-theme', 'dark'); } catch (e) {}
  } else {
    root.classList.add('light');
    btn.setAttribute('aria-pressed', 'false');
    btn.textContent = 'Light';
    try { localStorage.setItem('c2d-theme', 'light'); } catch (e) {}
  }
}

// Apply saved theme on load — called before the first paint to
// avoid a flash of the wrong theme.
(function applySavedTheme() {
  let saved = null;
  try { saved = localStorage.getItem('c2d-theme'); } catch (e) {}
  if (saved === 'light') {
    document.documentElement.classList.add('light');
    const btn = document.getElementById('dark-btn');
    if (btn) {
      btn.setAttribute('aria-pressed', 'false');
      btn.textContent = 'Light';
    }
  }
})();

// Node type filter
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

function shortLoc(sourceFile, line) {
  if (!sourceFile) return '';
  const base = String(sourceFile).split('/').pop();
  return line ? base + ':' + line : base;
}

function hideSearchResults() {
  const el = document.getElementById('search-results');
  if (el) { el.style.display = 'none'; el.innerHTML = ''; }
}

async function search() {
  const q = document.getElementById('search').value.trim();
  if (!q) return;
  const data = await api('/api/search?q=' + encodeURIComponent(q));
  const results = data.results || [];
  if (!results.length) {
    hideSearchResults();
    document.getElementById('stats').textContent = 'No matches for: ' + q;
    return;
  }
  if (results.length === 1) {
    hideSearchResults();
    focusNode(results[0].id, 2);
    return;
  }
  // Disambiguation list: same-named functions in different files used
  // to be indistinguishable (the UI auto-jumped to the first match).
  const resEl = document.getElementById('search-results');
  resEl.innerHTML = '';
  results.slice(0, 10).forEach((r, i) => {
    const div = document.createElement('div');
    div.className = 'sr-item' + (i === 0 ? ' active' : '');
    div.setAttribute('role', 'option');
    const nm = document.createElement('span');
    nm.className = 'mono';
    nm.textContent = r.name;
    // Node-type badge from labels — surfaces whether the hit is an
    // API entry, external endpoint, callback, etc. at a glance.
    const lbls = r.labels || [];
    const primaryLabel = lbls.find(l =>
      l === 'API_entry' || l === 'out_end' || l === 'unknown_end' ||
      l === 'callback_func' || l === 'constructor' || l === 'destructor');
    if (primaryLabel) {
      const badge = document.createElement('span');
      badge.className = 'sr-badge';
      badge.textContent = primaryLabel.replace('_', ' ');
      div.appendChild(badge);
    }
    div.appendChild(nm);
    // Domain tag — helps distinguish same-named functions across modules.
    if (r.domain) {
      const dom = document.createElement('span');
      dom.className = 'sr-loc';
      dom.textContent = r.domain;
      div.appendChild(dom);
    }
    const loc = document.createElement('span');
    loc.className = 'sr-loc';
    loc.textContent = shortLoc(r.source_file, r.line);
    if (loc.textContent) div.appendChild(loc);
    div.onclick = () => { hideSearchResults(); focusNode(r.id, 2); };
    resEl.appendChild(div);
  });
  resEl.style.display = 'block';
}

// Event listeners
document.getElementById('search-btn').addEventListener('click', search);
document.getElementById('search').addEventListener('keydown', e => { if (e.key === 'Enter') search(); });
document.getElementById('layout-select').addEventListener('change', runLayout);
document.getElementById('spacing-slider').addEventListener('change', runLayout);
document.getElementById('depth-slider').addEventListener('input', e => {
  document.getElementById('depth-val').textContent = e.target.value;
});
document.getElementById('depth-slider').addEventListener('change', e => {
  if (activeNodeId) focusNode(activeNodeId, parseInt(e.target.value));
});
document.getElementById('fit-btn').addEventListener('click', () => { if (cy) cy.fit(undefined, 42); });
document.getElementById('png-btn').addEventListener('click', exportPNG);
document.getElementById('json-btn').addEventListener('click', exportJSON);
document.getElementById('cycle-btn').addEventListener('click', toggleCycles);
document.getElementById('reload-btn').addEventListener('click', async () => {
  await api('/api/reload', { method: 'POST' }); loadSummary();
  if (cache.focus) focusNode(cache.focus, 1);
});
document.getElementById('help-btn').addEventListener('click', () => {
  const m = document.getElementById('help-modal');
  m.style.display = m.style.display === 'flex' ? 'none' : 'flex';
});

// project context panels
async function loadBrief() {
  const modal = document.getElementById('brief-modal');
  const body = document.getElementById('brief-body');
  modal.style.display = 'flex';
  body.textContent = 'Loading…';
  try {
    const resp = await fetch('/api/brief');
    const data = await resp.json();
    if (data.error) { body.textContent = 'Error loading brief.'; return; }
    body.textContent = data.missing
      ? 'No project brief yet.\n\nInitialize one with:\n  brief-extract --graph <graph-dir>\nthen curate with brief-update.'
      : data.rendered;
  } catch (e) { body.textContent = 'Error loading brief: ' + e; }
}

function renderMemoryResults(data) {
  const statsEl = document.getElementById('memory-stats');
  const resEl = document.getElementById('memory-results');
  const s = data.stats || {};
  statsEl.textContent = (s.active_entries || 0) + ' active · '
    + (s.categories || 0) + ' categories'
    + (s.experience_entries ? ' · ' + s.experience_entries + ' archived' : '');
  resEl.innerHTML = '';
  if (!data.results || !data.results.length) {
    resEl.innerHTML = '<div class="mem-item">No memories found.</div>';
    return;
  }
  for (const r of data.results) {
    const item = document.createElement('div');
    item.className = 'mem-item';
    const q = document.createElement('div'); q.className = 'mem-q';
    q.textContent = r.question;
    const a = document.createElement('div'); a.className = 'mem-a';
    a.textContent = r.answer || '(no answer)';
    const meta = document.createElement('div'); meta.className = 'mem-meta';
    const parts = [];
    if (r.category) parts.push(r.category);
    if (r.author) parts.push(r.author);
    parts.push('w=' + r.weight);
    if (r.access_count) parts.push(r.access_count + ' reads');
    if (r.variant_count) parts.push('+' + r.variant_count + ' variants');
    if (r.symbols && r.symbols.length) parts.push('⟨' + r.symbols.join(', ') + '⟩');
    meta.textContent = parts.join(' · ');
    item.appendChild(q); item.appendChild(a); item.appendChild(meta);
    resEl.appendChild(item);
  }
}

async function searchMemory() {
  const q = document.getElementById('memory-search-input').value;
  const author = document.getElementById('memory-author-select').value;
  const symbol = document.getElementById('memory-symbol-input').value;
  let url = '/api/memory/search?q=' + encodeURIComponent(q) + '&top=20';
  if (author) url += '&author=' + encodeURIComponent(author);
  if (symbol) url += '&symbol=' + encodeURIComponent(symbol);
  try {
    const resp = await fetch(url);
    renderMemoryResults(await resp.json());
  } catch (e) {
    document.getElementById('memory-results').innerHTML =
      '<div class="mem-item">Error searching memory: ' + escapeHtml(String(e)) + '</div>';
  }
}

// author filter — multi-user read-only view
async function loadMemoryAuthors() {
  const select = document.getElementById('memory-author-select');
  try {
    const resp = await fetch('/api/memory/authors');
    const data = await resp.json();
    const current = select.value;
    select.innerHTML = '<option value="">All authors</option>';
    for (const a of (data.authors || [])) {
      const opt = document.createElement('option');
      opt.value = a.author === '(unattributed)' ? '' : a.author;
      opt.textContent = a.author + ' (' + (a.active || 0) + ' active)';
      select.appendChild(opt);
    }
    select.value = current;
  } catch (e) { /* filter stays on 'All authors' */ }
}

// architecture narrative (ARCHITECTURE_FLOWS.md from the build)
async function loadArchitecture() {
  const modal = document.getElementById('arch-modal');
  const body = document.getElementById('arch-body');
  modal.style.display = 'flex';
  body.textContent = 'Loading…';
  try {
    const resp = await fetch('/api/architecture');
    const data = await resp.json();
    if (data.error) { body.textContent = 'Error loading architecture.'; return; }
    body.textContent = data.missing
      ? 'No ARCHITECTURE_FLOWS.md in this graph directory.\n\nIt is generated at build time (graph build / sqlite postprocess).\nRebuild the graph to produce it.'
      : data.content;
  } catch (e) { body.textContent = 'Error loading architecture: ' + e; }
}

// memory lineage tree (split / merge / variant relations)
function renderMemoryLineage(data) {
  const statsEl = document.getElementById('memory-stats');
  const resEl = document.getElementById('memory-results');
  const byId = {};
  for (const n of (data.nodes || [])) byId[n.id] = n;
  // children[from] = [{to, type}]
  const children = {};
  for (const e of (data.edges || [])) {
    (children[e.from] = children[e.from] || []).push(e);
  }
  const EDGE_LABEL = {split: 'split', merged_into: 'merged into',
                      variant: 'variant'};
  statsEl.textContent = (data.nodes || []).length + ' entries · '
    + (data.edges || []).length + ' lineage links';
  resEl.innerHTML = '';
  const hasParent = new Set((data.edges || []).map(e => e.to));
  const roots = (data.nodes || []).filter(n => !hasParent.has(n.id));
  if (!roots.length && (data.nodes || []).length) {
    // cyclic safety: fall back to all nodes as roots
    roots.push(...data.nodes);
  }
  const addLine = (indent, text) => {
    const div = document.createElement('div');
    div.className = 'mem-item';
    div.textContent = indent + text;
    resEl.appendChild(div);
  };
  const seen = new Set();
  const walk = (node, indent) => {
    if (seen.has(node.id)) return;  // cycle guard
    seen.add(node.id);
    const meta = [];
    if (node.category) meta.push(node.category);
    if (node.author) meta.push(node.author);
    meta.push('w=' + node.weight);
    addLine(indent, '#' + node.id + ' [' + node.status + '] '
            + node.question + '  (' + meta.join(' · ') + ')');
    for (const e of (children[node.id] || [])) {
      const child = byId[e.to];
      if (!child) continue;
      addLine(indent + '  ', '└─' + EDGE_LABEL[e.type] + '→ #' + child.id);
      walk(child, indent + '    ');
    }
  };
  for (const n of roots) walk(n, '');
  if (!(data.nodes || []).length) {
    addLine('', 'No memories yet — lineage appears once entries are '
            + 'split / merged / saved as variants.');
  }
}

async function loadMemoryLineage() {
  try {
    const resp = await fetch('/api/memory/lineage');
    renderMemoryLineage(await resp.json());
  } catch (e) {
    document.getElementById('memory-results').innerHTML =
      '<div class="mem-item">Error loading lineage: '
      + escapeHtml(String(e)) + '</div>';
  }
}

document.getElementById('brief-btn').addEventListener('click', loadBrief);
document.getElementById('arch-btn').addEventListener('click', loadArchitecture);
document.getElementById('memory-btn').addEventListener('click', () => {
  const m = document.getElementById('memory-modal');
  m.style.display = m.style.display === 'flex' ? 'none' : 'flex';
  if (m.style.display === 'flex') { searchMemory(); loadMemoryAuthors(); }
});
document.getElementById('memory-search-btn').addEventListener('click', searchMemory);
document.getElementById('memory-author-select').addEventListener('change', searchMemory);
document.getElementById('memory-lineage-btn').addEventListener('click', loadMemoryLineage);
document.getElementById('memory-search-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') searchMemory();
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

// Keyboard navigation
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  switch(e.key) {
    case '/': e.preventDefault(); document.getElementById('search').focus(); break;
    case 'f': case 'F': if (cy) cy.fit(undefined, 42); break;
    case '?': document.getElementById('help-btn').click(); break;
    case 'd': case 'D': toggleDark(); break;
    case 'p': case 'P': exportPNG(); break;
    case 'j': case 'J': exportJSON(); break;
    case '+': case '=': if (cy) cy.zoom(cy.zoom() * 1.3); break;
    case '-': if (cy) cy.zoom(cy.zoom() / 1.3); break;
    case 'Escape':
      if (cy) cy.elements().removeClass('faded');
      hideSearchResults();
      document.getElementById('help-modal').style.display = 'none';
      document.getElementById('brief-modal').style.display = 'none';
      document.getElementById('memory-modal').style.display = 'none';
      document.getElementById('arch-modal').style.display = 'none';
      document.getElementById('ctx-menu').style.display = 'none';
      document.getElementById('filter-panel').style.display = 'none';
      closeCodePanel();
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

// Close context menu + search dropdown on click anywhere
document.addEventListener('click', (e) => {
  document.getElementById('ctx-menu').style.display = 'none';
  if (!e.target.closest || !e.target.closest('#search-wrap')) hideSearchResults();
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
