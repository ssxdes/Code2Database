"""MCP server over Streamable HTTP transport.

Implements the MCP Streamable HTTP transport specification using only
Python's standard library (``http.server``), so it works with zero
additional dependencies.  All 83 MCP tools registered in ``mcp_server.TOOLS``
are exposed to remote clients over HTTP, enabling cross-network access
for Claude Desktop, Cursor, and any MCP-compatible client.

Usage::

    python code2database_builder.py serve --graph code2db-out/ \
        --transport http --host 0.0.0.0 --port 8765 \
        --token my-secret

Protocol: clients send JSON-RPC 2.0 requests via POST /mcp.
The server responds with ``application/json`` (direct response) or
``text/event-stream`` (SSE) depending on the ``Accept`` header.

Session management: on ``initialize`` the server generates a random
session ID and returns it in the ``Mcp-Session-Id`` response header.
Clients SHOULD include this header on subsequent requests.  Sessions
are stateless on the server side (the graph_dir is the only state),
so a missing or stale session ID does not cause errors — it is
validated only for protocol compliance.

Security:
  - Bearer token authentication (``--token`` flag or ``C2D_MCP_TOKEN``
    env var).  Every request must include ``Authorization: Bearer <token>``.
  - Optional TLS (``--tls-cert`` / ``--tls-key``).
  - ``--read-only`` mode hides write tools and rejects write calls.
  - ``--max-clients`` semaphore limits concurrent in-flight requests.
  - For production, put behind an nginx reverse proxy with TLS
    termination and rate limiting (see deploy/mcp-nginx.conf).
"""

import json
import os
import ssl
import sys
import time
import secrets
import threading
import logging
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

logger = logging.getLogger(__name__)

# Session store: session_id -> {"created": float, "graph_dir": str}
# Sessions are informational only (the server is stateless beyond the
# graph_dir).  They exist so that clients that send Mcp-Session-Id get
# a proper lifecycle (initialize -> ... -> DELETE).
_SESSIONS: dict = {}
_SESSIONS_LOCK = threading.Lock()
_SESSION_TTL = 3600.0  # 1 hour idle timeout


def _gc_sessions():
    """Remove expired sessions (called under _SESSIONS_LOCK)."""
    now = time.time()
    expired = [sid for sid, info in _SESSIONS.items()
               if now - info["created"] > _SESSION_TTL]
    for sid in expired:
        del _SESSIONS[sid]


def _create_session(graph_dir: str) -> str:
    """Create a new session and return its ID."""
    with _SESSIONS_LOCK:
        _gc_sessions()
        sid = secrets.token_urlsafe(32)
        _SESSIONS[sid] = {"created": time.time(), "graph_dir": graph_dir}
    return sid


def _validate_session(sid: Optional[str]) -> bool:
    """Check whether *sid* is a known, non-expired session.

    Returns True for valid sessions and for None (stateless clients).
    Returns False only for a non-empty but unknown session ID, which
    indicates a protocol error by the client.
    """
    if not sid:
        return True  # stateless client — allowed
    with _SESSIONS_LOCK:
        _gc_sessions()
        return sid in _SESSIONS


def _delete_session(sid: Optional[str]):
    """Invalidate *sid* if it exists."""
    if not sid:
        return
    with _SESSIONS_LOCK:
        _SESSIONS.pop(sid, None)


class _McpHTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler implementing MCP Streamable HTTP transport."""

    # Class-level config — set by run_mcp_server_http before serve_forever.
    _graph_dir: str = ""
    _token: Optional[str] = None
    _read_only: bool = False
    _semaphore: Optional[threading.Semaphore] = None
    _mcp_stats: dict = None  # shared stats dict (same as stdio mode)
    _stats_lock: Optional[threading.Lock] = None

    # Suppress default logging (overridable via logging config).
    def log_message(self, fmt, *args):
        logger.debug("HTTP " + fmt, *args)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _check_auth(self) -> bool:
        """Return True if the request passes authentication."""
        if not self._token:
            return True  # no token configured — open mode
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return secrets.compare_digest(auth[7:], self._token)
        return False

    def _send_json(self, status: int, body: dict,
                   extra_headers: Optional[dict] = None):
        """Send a JSON HTTP response."""
        payload = json.dumps(body, ensure_ascii=False, indent=2)
        data = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("MCP-Protocol-Version", "2024-11-05")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_sse(self, body: dict, extra_headers: Optional[dict] = None):
        """Send a JSON-RPC response wrapped in a single SSE event.

        Some MCP clients send ``Accept: text/event-stream`` only.  In that
        case we wrap the response as ``event: message\\ndata: <json>\\n\\n``.
        """
        payload = json.dumps(body, ensure_ascii=False)
        data = f"event: message\ndata: {payload}\n\n".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("MCP-Protocol-Version", "2024-11-05")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _client_wants_sse(self) -> bool:
        """Check Accept header: if only text/event-stream is present, use SSE."""
        accept = self.headers.get("Accept", "application/json")
        accepts = [a.strip().split(";")[0] for a in accept.split(",")]
        has_json = "application/json" in accepts or "*/*" in accepts
        has_sse = "text/event-stream" in accepts
        return has_sse and not has_json

    def _send_response(self, body: dict, session_id: Optional[str] = None):
        """Send response as JSON or SSE depending on Accept header."""
        headers = {}
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        if self._client_wants_sse():
            self._send_sse(body, headers)
        else:
            self._send_json(200, body, headers)

    def _send_error(self, status: int, message: str):
        """Send a simple error response."""
        self._send_json(status, {"error": message})

    # ------------------------------------------------------------------
    # POST /mcp — JSON-RPC request
    # ------------------------------------------------------------------

    def do_POST(self):
        # Health check also accessible via POST for simple clients.
        if self.path == "/health":
            if not self._check_auth():
                return self._send_error(401, "Unauthorized")
            return self._handle_health()

        if self.path not in ("/mcp", "/mcp/"):
            return self._send_error(404, f"Not found: {self.path}")

        # Auth
        if not self._check_auth():
            return self._send_error(401, "Unauthorized: invalid or missing Bearer token")

        # Concurrency limit
        if self._semaphore is not None:
            if not self._semaphore.acquire(timeout=30):
                return self._send_error(503, "Server busy: too many concurrent requests")
        try:
            self._handle_mcp_post()
        finally:
            if self._semaphore is not None:
                self._semaphore.release()

    def _handle_mcp_post(self):
        # Read body
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            return self._send_error(400, "Invalid Content-Length header")
        if content_length == 0:
            return self._send_error(400, "Empty request body")
        if content_length > 10 * 1024 * 1024:
            return self._send_error(413, "Request body too large (max 10MB)")
        raw = self.rfile.read(content_length)
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._send_error(400, "Invalid JSON")

        # Support batched requests (JSON-RPC 2.0 spec)
        if isinstance(msg, list):
            return self._handle_batch(msg)

        if not isinstance(msg, dict):
            return self._send_error(400, "Request must be a JSON object")

        self._handle_single(msg)

    def _handle_single(self, msg: dict):
        """Handle a single JSON-RPC message."""
        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params", {})
        if not isinstance(params, dict):
            params = {}
        session_id = self.headers.get("Mcp-Session-Id")

        # Validate session (if client claims to have one)
        if session_id and not _validate_session(session_id):
            return self._send_error(404, "Invalid or expired session")

        # On initialize, create a new session
        new_session_id = None
        if method == "initialize":
            new_session_id = _create_session(self._graph_dir)

        # Dispatch — no global lock (stats mutations are GIL-safe counters)
        from _builder.mcp.mcp_server import dispatch_mcp_request
        try:
            response = dispatch_mcp_request(
                method, msg_id, params, self._graph_dir,
                self._mcp_stats, self._read_only)
        except Exception:
            logger.warning("uncaught error in dispatch_mcp_request",
                           exc_info=True)
            response = {"jsonrpc": "2.0", "id": msg_id,
                        "error": {"code": -32603,
                                  "message": "Internal error"}}

        if response is None:
            # Notification (no response expected)
            return self._send_json(202, {}, {})
        self._send_response(response, session_id=new_session_id or session_id)

    def _handle_batch(self, messages: list):
        """Handle a JSON-RPC batch request."""
        if not messages:
            return self._send_error(400, "Invalid Request: empty batch")
        responses = []
        from _builder.mcp.mcp_server import dispatch_mcp_request
        for msg in messages:
            if not isinstance(msg, dict):
                responses.append({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32600,
                                            "message": "Invalid Request"}})
                continue
            method = msg.get("method", "")
            msg_id = msg.get("id")
            params = msg.get("params", {})
            if not isinstance(params, dict):
                params = {}
            try:
                response = dispatch_mcp_request(
                    method, msg_id, params, self._graph_dir,
                    self._mcp_stats, self._read_only)
            except Exception:
                logger.warning("uncaught error in batch dispatch",
                               exc_info=True)
                response = {"jsonrpc": "2.0", "id": msg_id,
                            "error": {"code": -32603,
                                      "message": "Internal error"}}
            if response is not None:
                responses.append(response)
        if responses:
            self._send_json(200, responses)
        else:
            # All notifications — no content
            self._send_json(202, [])

    # ------------------------------------------------------------------
    # GET /health — server health check
    # ------------------------------------------------------------------

    def do_GET(self):
        if self.path == "/health":
            if not self._check_auth():
                return self._send_error(401, "Unauthorized")
            return self._handle_health()
        if self.path in ("/mcp", "/mcp/"):
            # GET on /mcp opens an SSE stream for server notifications.
            # We don't push server-initiated notifications, so respond
            # with an empty stream that the client can keep open.
            if not self._check_auth():
                return self._send_error(401, "Unauthorized")
            # Concurrency limit for SSE too (prevents FD/thread exhaustion)
            if self._semaphore is not None:
                if not self._semaphore.acquire(timeout=30):
                    return self._send_error(503, "Server busy")
            try:
                return self._handle_sse_stream()
            finally:
                if self._semaphore is not None:
                    self._semaphore.release()
        self._send_error(404, f"Not found: {self.path}")

    def _handle_health(self):
        from _builder.mcp.mcp_server import TOOLS, WRITE_TOOLS
        available = len(TOOLS)
        visible = len(TOOLS) - len(WRITE_TOOLS) if self._read_only else len(TOOLS)
        self._send_json(200, {
            "status": "ok",
            "server": "Code2Database",
            "version": "2.1.0",
            "transport": "http",
            "tools_total": available,
            "tools_visible": visible,
            "read_only": self._read_only,
            "auth_required": bool(self._token),
        })

    def _handle_sse_stream(self):
        """Open a long-lived SSE connection.

        We don't have server-pushed notifications to send, so we send
        a heartbeat comment every 30 seconds to keep the connection alive
        until the client disconnects or the max duration elapses.
        """
        _SSE_MAX_DURATION = 300.0  # 5 minutes
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("MCP-Protocol-Version", "2024-11-05")
        self.end_headers()
        start = time.time()
        try:
            while time.time() - start < _SSE_MAX_DURATION:
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                time.sleep(30)
        except Exception:
            pass  # client disconnected or socket error

    # ------------------------------------------------------------------
    # DELETE /mcp — terminate session
    # ------------------------------------------------------------------

    def do_DELETE(self):
        if self.path not in ("/mcp", "/mcp/"):
            return self._send_error(404, f"Not found: {self.path}")
        if not self._check_auth():
            return self._send_error(401, "Unauthorized")
        session_id = self.headers.get("Mcp-Session-Id")
        _delete_session(session_id)
        self._send_json(200, {"status": "session terminated"})

    # ------------------------------------------------------------------
    # OPTIONS — CORS preflight
    # ------------------------------------------------------------------

    def do_OPTIONS(self):
        origin = self.headers.get("Origin", "")
        self.send_response(204)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, Accept, "
                         "Mcp-Session-Id, MCP-Protocol-Version")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()


def _make_handler_class(graph_dir, token, read_only, max_clients,
                        mcp_stats, stats_lock):
    """Create a handler subclass with the given configuration bound.

    We use a factory function instead of class attributes so that each
    server instance gets its own isolated config (important for testing
    and for running multiple servers in the same process).
    """
    sem = threading.Semaphore(max_clients)

    class _Handler(_McpHTTPHandler):
        _graph_dir = graph_dir
        _token = token
        _read_only = read_only
        _semaphore = sem
        _mcp_stats = mcp_stats
        _stats_lock = stats_lock

    return _Handler


def run_mcp_server_http(graph_dir: str, host: str = "0.0.0.0",
                        port: int = 8765, token: Optional[str] = None,
                        read_only: bool = False,
                        tls_cert: Optional[str] = None,
                        tls_key: Optional[str] = None,
                        max_clients: int = 32):
    """Run the MCP server over Streamable HTTP transport.

    Parameters:
        graph_dir: Path to the code2database output directory.
        host: Bind address (``0.0.0.0`` = all interfaces).
        port: TCP port.
        token: Bearer token for authentication. If None and
               ``C2D_MCP_TOKEN`` env var is set, uses that.
        read_only: If True, hide write tools and reject write calls.
        tls_cert: Path to TLS certificate file (PEM).
        tls_key: Path to TLS private key file (PEM).
        max_clients: Max concurrent in-flight requests.

    This function blocks until the server is shut down (Ctrl-C or
    SIGTERM).  It prints the listen address and auth status on startup.
    """
    if token is None:
        token = os.environ.get("C2D_MCP_TOKEN")
    # Treat empty string as no token (prevents auth bypass via --token="")
    if token and not token.strip():
        token = None

    # Propagate read-only mode to the tool handlers (kb-query tools skip
    # their access_count / query-log writes so a read-only server is
    # genuinely write-free, not just write-tool-filtered).
    from _builder.mcp.mcp_cache import set_mcp_read_only
    set_mcp_read_only(read_only)

    # Shared stats (same structure as stdio mode)
    mcp_stats = {"total_calls": 0, "total_output_tokens": 0, "by_tool": {}}
    stats_lock = threading.Lock()

    # Periodic stats writer
    _stop_event = threading.Event()

    def _write_stats_loop():
        while not _stop_event.wait(30):
            stats_path = os.path.join(graph_dir, ".code2database_mcp_stats.json")
            try:
                with stats_lock:
                    snapshot = json.loads(json.dumps(mcp_stats))
                from pathlib import Path
                Path(stats_path).write_text(
                    json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
            except Exception:
                logger.debug("stats write failed", exc_info=True)

    stats_thread = threading.Thread(target=_write_stats_loop, daemon=True)
    stats_thread.start()

    # Write stats on exit
    import atexit
    def _final_stats_write():
        _stop_event.set()
        stats_path = os.path.join(graph_dir, ".code2database_mcp_stats.json")
        try:
            with stats_lock:
                Path(stats_path).write_text(
                    json.dumps(mcp_stats, indent=2) + "\n", encoding="utf-8")
        except Exception:
            logger.debug("final stats write failed", exc_info=True)
    atexit.register(_final_stats_write)

    handler_cls = _make_handler_class(
        graph_dir, token, read_only, max_clients, mcp_stats, stats_lock)

    server = ThreadingHTTPServer((host, port), handler_cls)
    server.daemon_threads = True

    # TLS
    if bool(tls_cert) != bool(tls_key):
        print("ERROR: --tls-cert and --tls-key must both be set for TLS.",
              file=sys.stderr, flush=True)
        sys.exit(1)
    if tls_cert and tls_key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(tls_cert, tls_key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

    # Print startup banner
    proto = "https" if (tls_cert and tls_key) else "http"
    print(f"Code2Database MCP server (HTTP) listening on "
          f"{proto}://{host}:{port}/mcp", flush=True)
    print(f"  graph_dir:  {graph_dir}", flush=True)
    print(f"  read_only:  {read_only}", flush=True)
    print(f"  auth:       {'Bearer token required' if token else 'OPEN (no auth!)'}",
          flush=True)
    print(f"  max_clients: {max_clients}", flush=True)
    print(f"  health:     {proto}://{host}:{port}/health", flush=True)
    if not token:
        print("  WARNING: No --token set. Anyone who can reach this port "
              "can query your code graph AND write memories.", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down MCP HTTP server...", flush=True)
    finally:
        server.shutdown()
        server.server_close()
