"""Tests for the MCP HTTP transport server.

Covers:
- Authentication (no token / wrong token / correct token / open mode)
- Health endpoint
- JSON-RPC dispatch over HTTP (initialize, tools/list, tools/call, ping)
- Session management (create on initialize, validate, delete)
- Read-only mode (write tools hidden from list, rejected on call)
- Concurrency (multiple simultaneous requests)
- Batched JSON-RPC requests
- SSE response format (Accept: text/event-stream)
- Dispatch shared between stdio and HTTP transports

The server is started in a background thread on a random port, and
requests are made with http.client (stdlib — no external deps required).
"""
import io
import os
import sys
import json
import time
import socket
import shutil
import threading
import http.client
import unittest
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


def _find_free_port() -> int:
    """Find a free TCP port for testing."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _make_graph_dir(tmpdir: str) -> str:
    """Create a minimal graph dir with a code2database.db so MCP tools work."""
    import sqlite3
    db_path = os.path.join(tmpdir, "code2database.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE functions (id TEXT PRIMARY KEY, name TEXT, "
                 "source_file TEXT, line_number INTEGER, domain TEXT, "
                 "labels TEXT, signature TEXT, extra_json TEXT, "
                 "body_text_compressed BLOB, is_empty INTEGER, node_type TEXT)")
    conn.execute("CREATE TABLE edges (invoker_id TEXT, invoked_id TEXT, "
                 "call_order TEXT, call_condition TEXT, concurrency TEXT, "
                 "confidence TEXT, confidence_score REAL, source TEXT, "
                 "evidence TEXT, relation TEXT, vtable_type TEXT, "
                 "vtable_bound_module TEXT)")
    conn.execute("INSERT INTO functions (id, name, source_file, line_number, "
                 "domain, labels, signature, extra_json, is_empty) VALUES "
                 "('fn1', 'test_func', 'test.c', 1, 'test', '[]', '', '{}', 0)")
    conn.execute("INSERT INTO functions (id, name, source_file, line_number, "
                 "domain, labels, signature, extra_json, is_empty) VALUES "
                 "('fn2', 'other_func', 'test.c', 10, 'test', '[]', '', '{}', 0)")
    conn.execute("INSERT INTO edges (invoker_id, invoked_id, call_order, "
                 "confidence, source, relation) VALUES "
                 "('fn1', 'fn2', '0', 'EXTRACTED', 'ast', 'INVOKES')")
    conn.commit()
    conn.close()
    # Also create a memory dir so MemoryStore tools can initialize
    mem_dir = os.path.join(tmpdir, "memory")
    os.makedirs(mem_dir, exist_ok=True)
    return tmpdir


class _ServerCtx:
    """Manages a background MCP HTTP server for testing."""

    def __init__(self, graph_dir, token=None, read_only=False, max_clients=8):
        self.graph_dir = graph_dir
        self.token = token
        self.read_only = read_only
        self.max_clients = max_clients
        self.port = _find_free_port()
        self.server = None
        self.thread = None
        self.error = None

    def start(self):
        try:
            from _builder.mcp_http_server import _make_handler_class, \
                _SESSIONS, _SESSIONS_LOCK
            # Clear any leftover sessions from previous tests
            with _SESSIONS_LOCK:
                _SESSIONS.clear()

            mcp_stats = {"total_calls": 0, "total_output_tokens": 0, "by_tool": {}}
            stats_lock = threading.Lock()
            handler_cls = _make_handler_class(
                self.graph_dir, self.token, self.read_only,
                self.max_clients, mcp_stats, stats_lock)
            from http.server import ThreadingHTTPServer
            self.server = ThreadingHTTPServer(
                ("localhost", self.port), handler_cls)
            self.server.daemon_threads = True
            self.thread = threading.Thread(
                target=self.server.serve_forever, daemon=True)
            self.thread.start()
            time.sleep(0.3)
        except Exception as exc:
            self.error = str(exc)

    def stop(self):
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass

    def request(self, method, path, body=None, headers=None,
                want_response=True):
        """Make an HTTP request and return (status, headers, body_dict_or_str)."""
        conn = http.client.HTTPConnection("localhost", self.port, timeout=10)
        hdrs = dict(headers or {})
        if body is not None:
            hdrs.setdefault("Content-Type", "application/json")
            body = json.dumps(body) if isinstance(body, (dict, list)) else body
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        status = resp.status
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        data = resp.read()
        conn.close()
        if not want_response:
            return status, resp_headers, data
        ct = resp_headers.get("content-type", "")
        if "json" in ct:
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                parsed = data.decode("utf-8", errors="replace")
            return status, resp_headers, parsed
        # SSE or other
        text = data.decode("utf-8", errors="replace")
        if "text/event-stream" in ct:
            # Extract the first data: line
            for line in text.split("\n"):
                if line.startswith("data: "):
                    try:
                        return status, resp_headers, json.loads(line[6:])
                    except json.JSONDecodeError:
                        pass
        return status, resp_headers, text


class TestMcpHttpAuth(unittest.TestCase):
    """Test Bearer token authentication."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        _make_graph_dir(cls.tmpdir)
        cls.ctx = _ServerCtx(cls.tmpdir, token="secret123")
        cls.ctx.start()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.stop()

    def setUp(self):
        if self.ctx.error:
            self.skipTest(f"Server failed: {self.ctx.error}")

    def test_no_token_rejected(self):
        """Request without Authorization header gets 401."""
        status, _, body = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(status, 401)

    def test_wrong_token_rejected(self):
        """Request with wrong token gets 401."""
        status, _, body = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)

    def test_correct_token_accepted(self):
        """Request with correct token gets 200."""
        status, _, body = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": "Bearer secret123"})
        self.assertEqual(status, 200)

    def test_health_requires_auth(self):
        """Health endpoint requires auth when token is set."""
        status, _, _ = self.ctx.request("GET", "/health")
        self.assertEqual(status, 401)

    def test_health_with_auth_succeeds(self):
        """Health endpoint works with valid auth token."""
        status, _, body = self.ctx.request(
            "GET", "/health",
            headers={"Authorization": "Bearer secret123"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["auth_required"])
        self.assertNotIn("graph_dir", body)  # no info leak


class TestMcpHttpOpenMode(unittest.TestCase):
    """Test open mode (no token) — for trusted networks / localhost."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        _make_graph_dir(cls.tmpdir)
        cls.ctx = _ServerCtx(cls.tmpdir, token=None)
        cls.ctx.start()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.stop()

    def setUp(self):
        if self.ctx.error:
            self.skipTest(f"Server failed: {self.ctx.error}")

    def test_no_auth_required(self):
        """In open mode, no Authorization header needed."""
        status, _, body = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"jsonrpc": "2.0", "id": 1, "result": {}})

    def test_health_reports_no_auth(self):
        """Health endpoint reports auth_required=False in open mode."""
        status, _, body = self.ctx.request("GET", "/health")
        self.assertFalse(body["auth_required"])


class TestMcpHttpTools(unittest.TestCase):
    """Test tool listing and calling over HTTP."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        _make_graph_dir(cls.tmpdir)
        cls.ctx = _ServerCtx(cls.tmpdir, token=None)
        cls.ctx.start()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.stop()

    def setUp(self):
        if self.ctx.error:
            self.skipTest(f"Server failed: {self.ctx.error}")

    def test_initialize(self):
        """initialize returns protocol version and server info."""
        status, headers, body = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["serverInfo"]["name"], "Code2Database")
        self.assertEqual(body["result"]["protocolVersion"], "2024-11-05")
        # Session ID should be in the response headers
        self.assertIn("mcp-session-id", headers)

    def test_tools_list(self):
        """tools/list returns all 83 tools."""
        status, _, body = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(status, 200)
        tools = body["result"]["tools"]
        self.assertEqual(len(tools), 83)
        tool_names = [t["name"] for t in tools]
        self.assertIn("code2database_load", tool_names)
        self.assertIn("code2database_memory_search", tool_names)
        self.assertIn("code2database_save_memory", tool_names)

    def test_call_unknown_tool(self):
        """Calling an unknown tool returns an error."""
        status, _, body = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "nonexistent_tool", "arguments": {}}})
        self.assertEqual(status, 200)
        self.assertIn("error", body)

    def test_ping(self):
        """ping returns empty result."""
        status, _, body = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 4, "method": "ping"})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"jsonrpc": "2.0", "id": 4, "result": {}})

    def test_method_not_found(self):
        """Unknown method returns method-not-found error."""
        status, _, body = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 5, "method": "nonexistent/method"})
        self.assertEqual(status, 200)
        self.assertIn("error", body)
        self.assertEqual(body["error"]["code"], -32601)


class TestMcpHttpReadOnly(unittest.TestCase):
    """Test read-only mode: write tools hidden and rejected."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        _make_graph_dir(cls.tmpdir)
        cls.ctx = _ServerCtx(cls.tmpdir, token=None, read_only=True)
        cls.ctx.start()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.stop()

    def setUp(self):
        if self.ctx.error:
            self.skipTest(f"Server failed: {self.ctx.error}")

    def test_write_tools_hidden_from_list(self):
        """In read-only mode, write tools are hidden from tools/list."""
        status, _, body = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(status, 200)
        tool_names = {t["name"] for t in body["result"]["tools"]}
        # Write tools should NOT be in the list
        from _builder.mcp_server import WRITE_TOOLS
        for wt in WRITE_TOOLS:
            self.assertNotIn(wt, tool_names,
                f"Write tool {wt} should be hidden in read-only mode")
        # Total should be 83 - 9 write tools = 74
        self.assertEqual(len(body["result"]["tools"]), 83 - len(WRITE_TOOLS))

    def test_write_tool_call_rejected(self):
        """In read-only mode, calling a write tool returns an error response."""
        status, _, body = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "code2database_save_memory",
                        "arguments": {"question": "q", "answer": "a"}}})
        self.assertEqual(status, 200)
        self.assertTrue(body["result"]["isError"])

    def test_health_reports_read_only(self):
        """Health endpoint reports read_only=True."""
        status, _, body = self.ctx.request("GET", "/health")
        self.assertTrue(body["read_only"])
        self.assertEqual(body["tools_visible"], 83 - 9)

    def test_read_tool_still_works(self):
        """In read-only mode, read tools still work."""
        status, _, body = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "code2database_load", "arguments": {}}})
        self.assertEqual(status, 200)
        self.assertNotIn("isError", body.get("result", {}))


class TestMcpHttpSession(unittest.TestCase):
    """Test session management."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        _make_graph_dir(cls.tmpdir)
        cls.ctx = _ServerCtx(cls.tmpdir, token=None)
        cls.ctx.start()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.stop()

    def setUp(self):
        if self.ctx.error:
            self.skipTest(f"Server failed: {self.ctx.error}")

    def test_initialize_creates_session(self):
        """initialize creates a session and returns it in the header."""
        status, headers, body = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertIn("mcp-session-id", headers)
        self.assertTrue(len(headers["mcp-session-id"]) > 10)

    def test_valid_session_accepted(self):
        """A valid session ID is accepted."""
        # Create session
        _, headers, _ = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        sid = headers["mcp-session-id"]
        # Use session for subsequent request
        status, _, body = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            headers={"Mcp-Session-Id": sid})
        self.assertEqual(status, 200)

    def test_invalid_session_rejected(self):
        """An invalid session ID is rejected with 404."""
        status, _, body = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Mcp-Session-Id": "invalid-session-id"})
        self.assertEqual(status, 404)

    def test_no_session_accepted(self):
        """A request without session ID is accepted (stateless client)."""
        status, _, body = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(status, 200)

    def test_delete_session(self):
        """DELETE /mcp terminates the session."""
        # Create session
        _, headers, _ = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        sid = headers["mcp-session-id"]
        # Delete session
        status, _, body = self.ctx.request("DELETE", "/mcp",
            headers={"Mcp-Session-Id": sid})
        self.assertEqual(status, 200)
        # Now session should be invalid
        status, _, _ = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            headers={"Mcp-Session-Id": sid})
        self.assertEqual(status, 404)


class TestMcpHttpConcurrency(unittest.TestCase):
    """Test concurrent request handling."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        _make_graph_dir(cls.tmpdir)
        cls.ctx = _ServerCtx(cls.tmpdir, token=None, max_clients=8)
        cls.ctx.start()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.stop()

    def setUp(self):
        if self.ctx.error:
            self.skipTest(f"Server failed: {self.ctx.error}")

    def test_concurrent_pings(self):
        """Multiple concurrent requests all succeed."""
        results = []
        errors = []

        def do_ping(i):
            try:
                status, _, body = self.ctx.request("POST", "/mcp",
                    {"jsonrpc": "2.0", "id": i, "method": "ping"})
                if status == 200:
                    results.append(i)
                else:
                    errors.append((i, status))
            except Exception as e:
                errors.append((i, str(e)))

        threads = [threading.Thread(target=do_ping, args=(i,))
                   for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertEqual(len(results), 10, f"Errors: {errors}")

    def test_concurrent_tool_calls(self):
        """Concurrent tool calls don't corrupt the graph cache."""
        results = []
        errors = []

        def do_load(i):
            try:
                status, _, body = self.ctx.request("POST", "/mcp",
                    {"jsonrpc": "2.0", "id": i, "method": "tools/call",
                     "params": {"name": "code2database_load",
                                "arguments": {}}})
                if status == 200 and "result" in body:
                    results.append(i)
                else:
                    errors.append((i, status, str(body)[:200]))
            except Exception as e:
                errors.append((i, str(e)))

        threads = [threading.Thread(target=do_load, args=(i,))
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertEqual(len(results), 5, f"Errors: {errors}")


class TestMcpHttpBatch(unittest.TestCase):
    """Test batched JSON-RPC requests."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        _make_graph_dir(cls.tmpdir)
        cls.ctx = _ServerCtx(cls.tmpdir, token=None)
        cls.ctx.start()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.stop()

    def setUp(self):
        if self.ctx.error:
            self.skipTest(f"Server failed: {self.ctx.error}")

    def test_batch_request(self):
        """A batch of JSON-RPC requests returns a batch of responses."""
        batch = [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ]
        status, _, body = self.ctx.request("POST", "/mcp", batch)
        self.assertEqual(status, 200)
        self.assertIsInstance(body, list)
        self.assertEqual(len(body), 3)
        ids = {r["id"] for r in body}
        self.assertEqual(ids, {1, 2, 3})


class TestMcpHttpSSE(unittest.TestCase):
    """Test SSE response format."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        _make_graph_dir(cls.tmpdir)
        cls.ctx = _ServerCtx(cls.tmpdir, token=None)
        cls.ctx.start()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.stop()

    def setUp(self):
        if self.ctx.error:
            self.skipTest(f"Server failed: {self.ctx.error}")

    def test_sse_response(self):
        """When Accept is text/event-stream only, response is SSE formatted."""
        status, headers, body = self.ctx.request("POST", "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Accept": "text/event-stream"})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"jsonrpc": "2.0", "id": 1, "result": {}})


class TestMcpHttpHealth(unittest.TestCase):
    """Test the /health endpoint."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        _make_graph_dir(cls.tmpdir)
        cls.ctx = _ServerCtx(cls.tmpdir, token=None)
        cls.ctx.start()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.stop()

    def setUp(self):
        if self.ctx.error:
            self.skipTest(f"Server failed: {self.ctx.error}")

    def test_health_fields(self):
        """Health endpoint returns all expected fields."""
        status, _, body = self.ctx.request("GET", "/health")
        self.assertEqual(status, 200)
        for field in ["status", "server", "version", "transport",
                      "tools_total", "tools_visible",
                      "read_only", "auth_required"]:
            self.assertIn(field, body, f"Missing field: {field}")
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["server"], "Code2Database")
        self.assertEqual(body["transport"], "http")
        self.assertEqual(body["tools_total"], 83)

    def test_health_via_post(self):
        """Health endpoint also works via POST for simple clients."""
        status, _, body = self.ctx.request("POST", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")


class TestDispatchShared(unittest.TestCase):
    """Test that the shared dispatch function works identically for both transports."""

    def test_dispatch_ping(self):
        """dispatch_mcp_request returns a valid ping response."""
        from _builder.mcp_server import dispatch_mcp_request
        resp = dispatch_mcp_request("ping", 1, {}, "/tmp", {})
        self.assertEqual(resp, {"jsonrpc": "2.0", "id": 1, "result": {}})

    def test_dispatch_initialize(self):
        """dispatch_mcp_request returns a valid initialize response."""
        from _builder.mcp_server import dispatch_mcp_request
        resp = dispatch_mcp_request("initialize", 1, {}, "/tmp", {})
        self.assertEqual(resp["result"]["serverInfo"]["name"], "Code2Database")
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")

    def test_dispatch_tools_list(self):
        """dispatch_mcp_request returns 83 tools."""
        from _builder.mcp_server import dispatch_mcp_request
        resp = dispatch_mcp_request("tools/list", 1, {}, "/tmp", {})
        self.assertEqual(len(resp["result"]["tools"]), 83)

    def test_dispatch_tools_list_read_only(self):
        """dispatch_mcp_request hides write tools in read-only mode."""
        from _builder.mcp_server import dispatch_mcp_request, WRITE_TOOLS
        resp = dispatch_mcp_request("tools/list", 1, {}, "/tmp", {},
                                    read_only=True)
        tool_names = {t["name"] for t in resp["result"]["tools"]}
        for wt in WRITE_TOOLS:
            self.assertNotIn(wt, tool_names)

    def test_dispatch_notification(self):
        """dispatch_mcp_request returns None for notifications."""
        from _builder.mcp_server import dispatch_mcp_request
        resp = dispatch_mcp_request(
            "notifications/initialized", None, {}, "/tmp", {})
        self.assertIsNone(resp)

    def test_dispatch_method_not_found(self):
        """dispatch_mcp_request returns error for unknown method."""
        from _builder.mcp_server import dispatch_mcp_request
        resp = dispatch_mcp_request("unknown/method", 1, {}, "/tmp", {})
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_dispatch_unknown_tool(self):
        """dispatch_mcp_request returns error for unknown tool."""
        from _builder.mcp_server import dispatch_mcp_request
        resp = dispatch_mcp_request("tools/call", 1,
            {"name": "nonexistent", "arguments": {}}, "/tmp", {})
        self.assertIn("error", resp)

    def test_dispatch_write_tool_read_only(self):
        """dispatch_mcp_request rejects write tools in read-only mode."""
        from _builder.mcp_server import dispatch_mcp_request
        resp = dispatch_mcp_request("tools/call", 1,
            {"name": "code2database_save_memory",
             "arguments": {"question": "q", "answer": "a"}},
            "/tmp", {}, read_only=True)
        self.assertTrue(resp["result"]["isError"])


class TestLazySQLiteGraphThreadSafety(unittest.TestCase):
    """Test that LazySQLiteGraph is thread-safe under concurrent access."""

    def test_concurrent_node_access(self):
        """Concurrent __contains__ and _get_node_attrs don't crash."""
        import sqlite3
        from _builder.streaming_graph import LazySQLiteGraph
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE functions (id TEXT PRIMARY KEY, name TEXT, "
                     "source_file TEXT, line_number INTEGER, domain TEXT, "
                     "labels TEXT, signature TEXT, extra_json TEXT, "
                     "body_text_compressed BLOB, is_empty INTEGER, node_type TEXT)")
        for i in range(100):
            conn.execute("INSERT INTO functions (id, name, source_file, "
                "line_number, domain, labels, signature, extra_json, is_empty) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (f"fn{i}", f"func{i}", "test.c", i, "test", "[]", "", "{}"))
        conn.execute("CREATE TABLE edges (invoker_id TEXT, invoked_id TEXT, "
                     "call_order TEXT, call_condition TEXT, concurrency TEXT, "
                     "confidence TEXT, confidence_score REAL, source TEXT, "
                     "evidence TEXT, relation TEXT, vtable_type TEXT, "
                     "vtable_bound_module TEXT)")
        for i in range(99):
            conn.execute("INSERT INTO edges (invoker_id, invoked_id, "
                "call_order, confidence, source, relation) VALUES "
                "(?, ?, '0', 'EXTRACTED', 'ast', 'INVOKES')",
                (f"fn{i}", f"fn{i+1}"))
        conn.commit()
        conn.close()

        G = LazySQLiteGraph(db_path)
        errors = []

        def worker():
            try:
                for i in range(50):
                    nid = f"fn{i % 100}"
                    _ = nid in G
                    _ = G._get_node_attrs(nid)
                    _ = G.has_edge(f"fn{i % 99}", f"fn{(i % 99) + 1}")
                    _ = G.get_edge_data(f"fn{i % 99}", f"fn{(i % 99) + 1}")
                    _ = list(G.successors(f"fn{i % 99}"))
                    _ = list(G.predecessors(f"fn{i % 99}"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [], f"Concurrent access errors: {errors}")
        G.close()


class TestMcpHttpSecurityHardening(unittest.TestCase):
    """Test security hardening of the MCP HTTP server."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        _make_graph_dir(cls.tmpdir)
        cls.ctx = _ServerCtx(cls.tmpdir, token="secret123")
        cls.ctx.start()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.stop()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        if self.ctx.error:
            self.skipTest(f"Server failed: {self.ctx.error}")

    def test_content_length_too_large(self):
        """POST with Content-Length > 10MB returns 413."""
        conn = http.client.HTTPConnection("127.0.0.1", self.ctx.port, timeout=3)
        conn.request("POST", "/mcp", body="{}",
                     headers={"Content-Length": str(11 * 1024 * 1024),
                              "Authorization": "Bearer secret123"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 413)
        conn.close()

    def test_invalid_content_length(self):
        """POST with non-numeric Content-Length returns 400."""
        conn = http.client.HTTPConnection("127.0.0.1", self.ctx.port, timeout=3)
        conn.request("POST", "/mcp", body="{}",
                     headers={"Content-Length": "abc",
                              "Authorization": "Bearer secret123"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        conn.close()

    def test_health_no_graph_dir_leak(self):
        """Health endpoint does not expose graph_dir path."""
        status, _, body = self.ctx.request(
            "GET", "/health",
            headers={"Authorization": "Bearer secret123"})
        self.assertEqual(status, 200)
        self.assertNotIn("graph_dir", body)

    def test_cors_reflects_origin(self):
        """CORS preflight reflects request Origin instead of *."""
        conn = http.client.HTTPConnection("127.0.0.1", self.ctx.port, timeout=3)
        conn.request("OPTIONS", "/mcp",
                     headers={"Origin": "https://example.com"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 204)
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"),
                         "https://example.com")
        conn.close()


if __name__ == "__main__":
    unittest.main()
