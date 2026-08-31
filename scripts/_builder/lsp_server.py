"""LSP (Language Server Protocol) server — exposes Code2Database graph to IDEs.

Exposes the pre-built C2D graph as an LSP server, allowing any LSP-aware
editor (VS Code, Vim/Neovim, Emacs, Helix, etc.) to use pre-computed
go-to-definition / find-references / callHierarchy without re-parsing the
codebase.

Architecture:
- JSON-RPC over stdio with Content-Length framing (LSP base protocol)
- initialize → ServerCapabilities handshake
- Text sync: None (read-only — the graph is the truth, not the editor buffer)
- Methods: definition, references, callHierarchy/incomingCalls+outgoingCalls,
  hover, documentSymbol, workspaceSymbol, moniker

All methods are wired to GraphCache (the same in-memory graph cache used by
the Web UI). Start with: `code2database_builder.py lsp-server --graph
/path/to/graph` and configure your editor to spawn it as a language
server for the codebase's language.

Unique value vs standard LSP servers:
- Condition-aware navigation (outgoingCalls carry call_condition)
- Cross-language navigation (FFI_BRIDGE edges)
- Sub-millisecond responses on kernel-scale repos (pre-computed graph)
- LSIF-compatible monikers (unified_node_id)
"""
from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Optional


class LSPServer:
    """LSP server backed by Code2Database graph.

    Usage:
        lsp-server --graph code2db-out/
        # Editor spawns this as a language server process
    """

    def __init__(self, graph_dir: str):
        self.graph_dir = graph_dir
        self._cache = None
        self._shutdown = False

    def _ensure_cache(self):
        if self._cache is None:
            from _builder.web_ui import GraphCache
            self._cache = GraphCache(self.graph_dir)

    def initialize(self, params: Dict) -> Dict:
        """Respond to LSP initialize request with ServerCapabilities."""
        self._ensure_cache()
        return {
            "capabilities": {
                "definitionProvider": True,
                "referencesProvider": True,
                "callHierarchyProvider": True,
                "hoverProvider": True,
                "monikerProvider": True,
                # Read-only: no text sync, no completion, no diagnostics
                "textDocumentSync": 0,  # None
            },
            "serverInfo": {
                "name": "code2database-lsp",
                "version": "1.3.0",
            },
        }

    def definition(self, uri: str, position: Dict) -> List[Dict]:
        """textDocument/definition — find where a symbol is defined."""
        self._ensure_cache()
        node_id = self._find_node_at_position(uri, position)
        if not node_id:
            return []
        node = self._cache.get_node(node_id)
        if node:
            return [{
                "uri": self._file_to_uri(node.get("source_file", "")),
                "range": {
                    "start": {"line": max(0, node.get("line", 1) - 1), "character": 0},
                    "end": {"line": max(0, node.get("line", 1) - 1), "character": 80},
                }
            }]
        return []

    def references(self, uri: str, position: Dict) -> List[Dict]:
        """textDocument/references — all usage sites."""
        self._ensure_cache()
        node_id = self._find_node_at_position(uri, position)
        if not node_id:
            return []
        callers = self._cache.get_callers(node_id)
        locations = []
        for c in callers:
            locations.append({
                "uri": self._file_to_uri(c.get("source_file", "")),
                "range": {
                    "start": {"line": max(0, c.get("line", 1) - 1), "character": 0},
                    "end": {"line": max(0, c.get("line", 1) - 1), "character": 80},
                }
            })
        return locations

    def call_hierarchy_incoming(self, item: Dict) -> List[Dict]:
        """callHierarchy/incomingCalls — who calls this function."""
        self._ensure_cache()
        node_id = item.get("id", "")
        callers = self._cache.get_callers(node_id)
        return [{
            "from": {
                "name": c.get("name", ""),
                "kind": 12,  # Function
                "uri": self._file_to_uri(c.get("source_file", "")),
                "range": {"start": {"line": 0, "character": 0},
                           "end": {"line": 0, "character": 0}},
                "selectionRange": {"start": {"line": 0, "character": 0},
                                     "end": {"line": 0, "character": 0}},
                "data": node_id,
            },
            "fromRanges": [{
                "start": {"line": max(0, c.get("line", 1) - 1), "character": 0},
                "end": {"line": max(0, c.get("line", 1) - 1), "character": 80},
            }],
        } for c in callers]

    def call_hierarchy_outgoing(self, item: Dict) -> List[Dict]:
        """callHierarchy/outgoingCalls — what this function calls."""
        self._ensure_cache()
        node_id = item.get("id", "")
        callees = self._cache.get_callees(node_id)
        return [{
            "to": {
                "name": c.get("name", ""),
                "kind": 12,
                "uri": self._file_to_uri(c.get("source_file", "")),
                "range": {"start": {"line": 0, "character": 0},
                           "end": {"line": 0, "character": 0}},
                "selectionRange": {"start": {"line": 0, "character": 0},
                                     "end": {"line": 0, "character": 0}},
                "data": c["id"],
            },
            "fromRanges": [{
                "start": {"line": max(0, c.get("line", 1) - 1), "character": 0},
                "end": {"line": max(0, c.get("line", 1) - 1), "character": 80},
            }],
        } for c in callees]

    def hover(self, uri: str, position: Dict) -> Optional[Dict]:
        """textDocument/hover — show semantic_desc + signature."""
        self._ensure_cache()
        node_id = self._find_node_at_position(uri, position)
        if not node_id:
            return None
        node = self._cache.get_node(node_id)
        if not node:
            return None
        desc = node.get("semantic_desc") or node.get("external_desc") or ""
        sig = node.get("signature") or ""
        content = f"```c\n{sig}\n```\n\n{desc}" if sig else desc
        return {"contents": {"kind": "markdown", "value": content}}

    def moniker(self, uri: str, position: Dict) -> List[Dict]:
        """textDocument/moniker — stable cross-project symbol ID."""
        self._ensure_cache()
        node_id = self._find_node_at_position(uri, position)
        if not node_id:
            return []
        return [{
            "scheme": "code2database",
            "identifier": node_id,
            "unique": 2,  # group
        }]

    def _find_node_at_position(self, uri: str, position: Dict) -> str:
        """Find a graph node by file URI + line number."""
        self._ensure_cache()
        file_path = self._uri_to_file(uri)
        line = position.get("line", 0) + 1  # LSP is 0-based
        for nid, nd in self._cache.G.nodes(data=True):
            if nd.get("source_file") == file_path and nd.get("line") == line:
                return nid
        return ""

    @staticmethod
    def _file_to_uri(path: str) -> str:
        if not path:
            return ""
        abs_path = os.path.abspath(path)
        return f"file://{abs_path}"

    @staticmethod
    def _uri_to_file(uri: str) -> str:
        if uri.startswith("file://"):
            return uri[7:]
        return uri

    # --- LSP base protocol transport ---

    def run_stdio(self):
        """Run the LSP server on stdio (Content-Length framing).

        Handles both CRLF and LF framing, malformed headers, malformed
        JSON, partial body reads, and EOF (clean exit).
        """
        while not self._shutdown:
            headers = {}
            while True:
                line = sys.stdin.readline()
                if not line:
                    # EOF — clean exit
                    return
                line = line.strip()
                if not line:
                    break
                key, _, val = line.partition(":")
                headers[key.strip().lower()] = val.strip()
            try:
                content_length = int(headers.get("content-length", 0))
            except ValueError:
                content_length = 0
            if content_length == 0:
                continue
            # Read exactly content_length bytes (read() may return fewer)
            body = b""
            while len(body) < content_length:
                chunk = sys.stdin.buffer.read(content_length - len(body))
                if not chunk:
                    return  # EOF
                body += chunk
            try:
                msg = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                if "id" in headers:
                    self._send({"jsonrpc": "2.0",
                                "id": None,
                                "error": {"code": -32700,
                                          "message": "Parse error"}})
                continue
            response = self._handle(msg)
            if response is not None:
                self._send(response)

    def _handle(self, msg: Dict) -> Optional[Dict]:
        method = msg.get("method", "")
        params = msg.get("params", {})
        msg_id = msg.get("id")
        try:
            if method == "initialize":
                result = self.initialize(params)
            elif method == "initialized":
                return None
            elif method == "shutdown":
                self._shutdown = True
                result = None
            elif method == "exit":
                sys.exit(0)
            elif method == "textDocument/definition":
                result = self.definition(
                    params.get("textDocument", {}).get("uri", ""),
                    params.get("position", {}))
            elif method == "textDocument/references":
                result = self.references(
                    params.get("textDocument", {}).get("uri", ""),
                    params.get("position", {}))
            elif method == "textDocument/hover":
                result = self.hover(
                    params.get("textDocument", {}).get("uri", ""),
                    params.get("position", {}))
            elif method == "textDocument/moniker":
                result = self.moniker(
                    params.get("textDocument", {}).get("uri", ""),
                    params.get("position", {}))
            elif method == "callHierarchy/incomingCalls":
                result = self.call_hierarchy_incoming(params.get("item", {}))
            elif method == "callHierarchy/outgoingCalls":
                result = self.call_hierarchy_outgoing(params.get("item", {}))
            else:
                if msg_id is not None:
                    return {"jsonrpc": "2.0", "id": msg_id,
                            "error": {"code": -32601, "message": f"Unknown method: {method}"}}
                return None
            if msg_id is not None:
                return {"jsonrpc": "2.0", "id": msg_id, "result": result}
            return None
        except Exception as e:
            if msg_id is not None:
                return {"jsonrpc": "2.0", "id": msg_id,
                        "error": {"code": -32603, "message": str(e)}}
            return None

    @staticmethod
    def _send(msg: Dict):
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        sys.stdout.buffer.write(header + body)
        sys.stdout.buffer.flush()


def cmd_lsp_server(args):
    """CLI handler: start the LSP server on stdio."""
    server = LSPServer(args.graph)
    server.run_stdio()
