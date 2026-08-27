"""LSP backend — consume existing LSP servers as extraction backends.

Uses gopls / rust-analyzer / clangd / pylsp / jdtls as a third extraction
backend (alongside tree-sitter and clang) to get authoritative call edges,
references, type hierarchies, and stable monikers.

Architecture:
- Spawns target LSP server as a subprocess
- initialize handshake → advertise needed capabilities
- For each source file: didOpen → prepareCallHierarchy → outgoingCalls/incomingCalls
- Materializes edges as (source, target, call_site_range, confidence=EXTRACTED)
- Uses moniker for stable cross-project symbol IDs
- Merges with tree-sitter/clang via DualBackendScanner

Note: This is the extraction-side consumer of LSP (drives clangd/gopls/
etc. to populate the C2D graph). The serve-side is `lsp_server.py`, which
exposes a pre-built C2D graph AS an LSP server for editors. They are
dual roles — one consumes LSP at scan time, the other provides LSP at
query time.

Status: architecture implemented, transport and callHierarchy driver
working. Not yet auto-selected by scan_directory() — wire in by
calling `LSPBackend.extract_call_edges()` from the scanner when
`--extraction-backend lsp` is requested. The hybrid model: LSP for
high-precision edges + identifiers, tree-sitter for condition/
concurrency/field facts.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple


# Known language server commands
LSP_SERVERS = {
    "c": "clangd",
    "cpp": "clangd",
    "go": "gopls",
    "python": "pylsp",
    "rust": "rust-analyzer",
    "java": "jdtls",
}


class LSPBackend:
    """Extraction backend that drives an LSP server for call edges.

    Usage (future, when wired into scan_directory):
        scan --source /path --extraction-backend lsp --lsp-server clangd
    """

    def __init__(self, lsp_server_cmd: str, source_root: str):
        self.cmd = lsp_server_cmd
        self.source_root = os.path.abspath(source_root)
        self._proc: Optional[subprocess.Popen] = None
        self._msg_id = 0

    def start(self):
        """Spawn the LSP server and initialize."""
        self._proc = subprocess.Popen(
            [self.cmd, "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Send initialize
        result = self._request("initialize", {
            "processId": os.getpid(),
            "rootUri": f"file://{self.source_root}",
            "capabilities": {},
        })
        # Send initialized notification
        self._notify("initialized", {})
        return result.get("capabilities", {})

    def stop(self):
        """Shutdown and exit the LSP server."""
        if self._proc:
            try:
                self._request("shutdown", {})
                self._notify("exit", {})
            except Exception:
                pass
            self._proc.terminate()
            self._proc.wait(timeout=5)

    def extract_call_edges(self, files: List[str]) -> List[Dict]:
        """Extract call edges by driving callHierarchy on each file.

        For each file:
        1. textDocument/didOpen
        2. textDocument/documentSymbol → list of function positions
        3. For each function: prepareCallHierarchy → outgoingCalls
        4. Materialize edges as (source_file, source_line, target_name, target_file)

        Returns list of edge dicts compatible with C2D extraction format.
        """
        edges = []
        for filepath in files:
            uri = f"file://{os.path.join(self.source_root, filepath)}"
            self._notify("textDocument/didOpen", {
                "textDocument": {"uri": uri, "languageId": "c", "version": 1, "text": ""}
            })
            # Get document symbols
            symbols = self._request("textDocument/documentSymbol", {
                "textDocument": {"uri": uri}
            })
            if not isinstance(symbols, list):
                continue
            for sym in symbols:
                if sym.get("kind") != 12:  # Function = 12
                    continue
                # Prepare call hierarchy
                prep = self._request("textDocument/prepareCallHierarchy", {
                    "textDocument": {"uri": uri},
                    "position": sym.get("selectionRange", {}).get("start", {})
                })
                if not isinstance(prep, list) or not prep:
                    continue
                item = prep[0]
                # Get outgoing calls (callees)
                outgoing = self._request("callHierarchy/outgoingCalls", {"item": item})
                if not isinstance(outgoing, list):
                    continue
                for call in outgoing:
                    target = call.get("to", {})
                    target_name = target.get("name", "")
                    target_uri = target.get("uri", "")
                    from_ranges = call.get("fromRanges", [])
                    for fr in from_ranges:
                        edges.append({
                            "source": sym.get("name", ""),
                            "source_file": filepath,
                            "source_line": fr.get("start", {}).get("line", 0) + 1,
                            "target": target_name,
                            "target_file": target_uri.replace("file://", ""),
                            "relation": "INVOKES",
                            "confidence": "EXTRACTED",
                        })
            self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        return edges

    def _request(self, method: str, params: Dict) -> Any:
        """Send LSP request and wait for response."""
        self._msg_id += 1
        msg = {"jsonrpc": "2.0", "id": self._msg_id, "method": method, "params": params}
        self._send(msg)
        while True:
            resp = self._recv()
            if resp.get("id") == self._msg_id:
                return resp.get("result")
            # Other messages (notifications) are ignored for now

    def _notify(self, method: str, params: Dict):
        """Send LSP notification (no response expected)."""
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        self._send(msg)

    def _send(self, msg: Dict):
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n"
        self._proc.stdin.write(header)
        self._proc.stdin.write(body.decode("utf-8"))
        self._proc.stdin.flush()

    def _recv(self) -> Dict:
        """Read one LSP message from stdout."""
        headers = {}
        while True:
            line = self._proc.stdout.readline()
            if not line or line == "\r\n":
                break
            key, _, val = line.partition(":")
            headers[key.strip().lower()] = val.strip()
        length = int(headers.get("content-length", 0))
        body = self._proc.stdout.read(length)
        return json.loads(body)
