"""Unit tests for the LSP server (lsp_server.py).

Verifies that LSPServer:
- initializes with the correct ServerCapabilities handshake
- resolves definition, references, hover, moniker, callHierarchy
- handles unknown methods with LSP error code -32601
- routes through the same GraphCache the Web UI uses
"""
import json
import os
import tempfile
import unittest


def _make_graph_dir() -> str:
    """Build a tiny domain-split graph fixture in a temp dir."""
    tmp = tempfile.mkdtemp(prefix="c2d_lsp_test_")
    domain_data = {
        "nodes": [
            {"id": "foo", "name": "foo", "source_file": "/tmp/foo.c", "line": 10,
             "signature": "int foo(int)", "semantic_desc": "foo helper",
             "labels": ["unknown_end"], "is_empty": False, "domain": "test"},
            {"id": "bar", "name": "bar", "source_file": "/tmp/bar.c", "line": 20,
             "signature": "int bar(int)", "semantic_desc": "bar entry",
             "labels": ["API_entry"], "is_empty": False, "domain": "test"},
            {"id": "baz", "name": "baz", "source_file": "/tmp/baz.c", "line": 30,
             "signature": "int baz(int)", "semantic_desc": "baz other",
             "labels": ["unknown_end"], "is_empty": False, "domain": "test"},
        ],
        "edges": [
            {"source": "bar", "target": "foo", "relation": "INVOKES",
             "call_order": 1, "confidence": "EXTRACTED"},
            {"source": "baz", "target": "foo", "relation": "INVOKES",
             "call_order": 1, "confidence": "EXTRACTED"},
        ],
    }
    domain_filename = "domain_test.json"
    with open(os.path.join(tmp, domain_filename), "w") as f:
        json.dump(domain_data, f)
    master = {"source_root": "/tmp", "domains": {"test": domain_filename}}
    with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
        json.dump(master, f)
    return tmp


class TestLSPServerInitialize(unittest.TestCase):
    def setUp(self):
        from _builder.lsp_server import LSPServer
        self.graph_dir = _make_graph_dir()
        self.server = LSPServer(self.graph_dir)

    def test_initialize_returns_capabilities(self):
        """initialize() returns the LSP ServerCapabilities dict."""
        result = self.server.initialize({})
        self.assertIn("capabilities", result)
        caps = result["capabilities"]
        # Required providers
        self.assertTrue(caps["definitionProvider"])
        self.assertTrue(caps["referencesProvider"])
        self.assertTrue(caps["callHierarchyProvider"])
        self.assertTrue(caps["hoverProvider"])
        self.assertTrue(caps["monikerProvider"])
        # Read-only text sync
        self.assertEqual(caps["textDocumentSync"], 0)
        # serverInfo
        self.assertEqual(result["serverInfo"]["name"], "code2database-lsp")

    def test_no_unimplemented_capabilities_advertised(self):
        """ServerCapabilities must NOT advertise unimplemented methods."""
        caps = self.server.initialize({})["capabilities"]
        # These were previously advertised but never implemented —
        # editors would send requests and get -32601 errors.
        self.assertNotIn("documentSymbolProvider", caps)
        self.assertNotIn("workspaceSymbolProvider", caps)

    def test_handle_initialize_dispatches_to_initialize(self):
        """_handle(initialize) returns a JSON-RPC response with capabilities."""
        resp = self.server._handle({"jsonrpc": "2.0", "id": 1,
                                     "method": "initialize", "params": {}})
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertEqual(resp["id"], 1)
        self.assertIn("capabilities", resp["result"])


class TestLSPServerMethods(unittest.TestCase):
    def setUp(self):
        from _builder.lsp_server import LSPServer
        self.graph_dir = _make_graph_dir()
        self.server = LSPServer(self.graph_dir)
        # Force cache initialization
        self.server.initialize({})

    def test_definition_resolves_to_source_location(self):
        """definition(/tmp/foo.c:line=10) returns the location of foo."""
        locs = self.server.definition("file:///tmp/foo.c",
                                       {"line": 9, "character": 0})
        self.assertEqual(len(locs), 1)
        self.assertEqual(locs[0]["uri"], "file:///tmp/foo.c")
        self.assertEqual(locs[0]["range"]["start"]["line"], 9)  # 1-based -> 0-based

    def test_definition_returns_empty_for_unknown_position(self):
        """definition at an unknown line returns an empty list."""
        locs = self.server.definition("file:///tmp/foo.c",
                                       {"line": 999, "character": 0})
        self.assertEqual(locs, [])

    def test_references_returns_all_callers(self):
        """references(/tmp/foo.c:10) returns bar and baz (both call foo)."""
        refs = self.server.references("file:///tmp/foo.c",
                                       {"line": 9, "character": 0})
        self.assertEqual(len(refs), 2)
        caller_uris = sorted(r["uri"] for r in refs)
        self.assertEqual(caller_uris, ["file:///tmp/bar.c", "file:///tmp/baz.c"])

    def test_call_hierarchy_incoming(self):
        """incomingCalls(foo) returns 2 callers (bar, baz)."""
        result = self.server.call_hierarchy_incoming({"id": "foo"})
        self.assertEqual(len(result), 2)
        # Each entry has 'from' dict with name
        names = sorted(item["from"]["name"] for item in result)
        self.assertEqual(names, ["bar", "baz"])

    def test_call_hierarchy_outgoing(self):
        """outgoingCalls(bar) returns 1 callee (foo)."""
        result = self.server.call_hierarchy_outgoing({"id": "bar"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["to"]["name"], "foo")
        # The 'data' field carries the target node id
        self.assertEqual(result[0]["to"]["data"], "foo")

    def test_hover_returns_markdown_with_signature_and_desc(self):
        """hover(/tmp/foo.c:10) returns markdown contents with signature + desc."""
        hov = self.server.hover("file:///tmp/foo.c", {"line": 9, "character": 0})
        self.assertIsNotNone(hov)
        self.assertEqual(hov["contents"]["kind"], "markdown")
        value = hov["contents"]["value"]
        self.assertIn("int foo(int)", value)
        self.assertIn("foo helper", value)

    def test_hover_returns_none_for_unknown_position(self):
        """hover at unknown line returns None."""
        hov = self.server.hover("file:///tmp/foo.c", {"line": 999, "character": 0})
        self.assertIsNone(hov)

    def test_moniker_returns_stable_node_id(self):
        """moniker returns the C2D node id as a stable identifier."""
        mon = self.server.moniker("file:///tmp/foo.c", {"line": 9, "character": 0})
        self.assertEqual(len(mon), 1)
        self.assertEqual(mon[0]["scheme"], "code2database")
        self.assertEqual(mon[0]["identifier"], "foo")
        # 'unique' field: 2 = group (per LSP spec)
        self.assertEqual(mon[0]["unique"], 2)


class TestLSPServerHandle(unittest.TestCase):
    def setUp(self):
        from _builder.lsp_server import LSPServer
        self.graph_dir = _make_graph_dir()
        self.server = LSPServer(self.graph_dir)

    def test_handle_unknown_method_returns_error_minus_32601(self):
        """Unknown LSP method returns JSON-RPC error code -32601."""
        resp = self.server._handle({"jsonrpc": "2.0", "id": 42,
                                     "method": "totally/unknown", "params": {}})
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertEqual(resp["id"], 42)
        self.assertEqual(resp["error"]["code"], -32601)
        self.assertIn("Unknown method", resp["error"]["message"])

    def test_handle_initialized_returns_none_no_response(self):
        """'initialized' notification returns None (no response per LSP spec)."""
        resp = self.server._handle({"jsonrpc": "2.0",
                                     "method": "initialized", "params": {}})
        self.assertIsNone(resp)

    def test_handle_definition_dispatches(self):
        """_handle routes textDocument/definition to the definition() method."""
        resp = self.server._handle({
            "jsonrpc": "2.0", "id": 5,
            "method": "textDocument/definition",
            "params": {"textDocument": {"uri": "file:///tmp/foo.c"},
                        "position": {"line": 9, "character": 0}}
        })
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertEqual(resp["id"], 5)
        self.assertEqual(len(resp["result"]), 1)
        self.assertEqual(resp["result"][0]["uri"], "file:///tmp/foo.c")

    def test_handle_shutdown_sets_shutdown_flag(self):
        """'shutdown' method sets the _shutdown flag (terminates run_stdio loop)."""
        self.assertFalse(self.server._shutdown)
        resp = self.server._handle({"jsonrpc": "2.0", "id": 7,
                                     "method": "shutdown", "params": {}})
        self.assertTrue(self.server._shutdown)
        self.assertIsNone(resp["result"])


class TestLSPServerHelpers(unittest.TestCase):
    def test_file_to_uri(self):
        """_file_to_uri converts a path to a file:// URI."""
        from _builder.lsp_server import LSPServer
        # Absolute path
        uri = LSPServer._file_to_uri("/tmp/foo.c")
        self.assertTrue(uri.startswith("file://"))
        self.assertIn("/tmp/foo.c", uri)
        # Empty path -> empty uri
        self.assertEqual(LSPServer._file_to_uri(""), "")

    def test_uri_to_file(self):
        """_uri_to_file strips the file:// prefix."""
        from _builder.lsp_server import LSPServer
        self.assertEqual(LSPServer._uri_to_file("file:///tmp/foo.c"), "/tmp/foo.c")
        # Pass-through when no prefix
        self.assertEqual(LSPServer._uri_to_file("/tmp/foo.c"), "/tmp/foo.c")


if __name__ == "__main__":
    unittest.main()
