"""O22: Unit tests for MCP server framing and tool dispatch.

Tests the JSON-RPC stdio framing (Content-Length header parsing/writing),
large message handling, and the tool dispatch table — without spawning a
real subprocess. The framing functions are the most bug-prone part of the
MCP server (off-by-one in Content-Length, EOF handling, malformed JSON),
so they get the most coverage.
"""

import io
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))


class TestMcpFraming(unittest.TestCase):
    """Test _read_message / _write_message framing."""

    def _make_framed(self, msg: dict) -> str:
        body = json.dumps(msg, ensure_ascii=False)
        return f"Content-Length: {len(body.encode('utf-8'))}\r\n\r\n{body}"

    def test_write_message_basic(self):
        """_write_message produces a valid Content-Length frame."""
        from _builder.mcp_server import _write_message
        msg = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            _write_message(msg)
            out = mock_out.getvalue()
        self.assertIn("Content-Length:", out)
        self.assertIn("\r\n\r\n", out)
        # Body should be valid JSON
        body = out.split("\r\n\r\n", 1)[1]
        parsed = json.loads(body)
        self.assertEqual(parsed, msg)

    def test_write_message_unicode(self):
        """_write_message handles non-ASCII (Chinese) content correctly."""
        from _builder.mcp_server import _write_message
        msg = {"result": {"name": "测试函数", "desc": "中文描述"}}
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            _write_message(msg)
            out = mock_out.getvalue()
        # Content-Length should be the byte length of the UTF-8 body,
        # not the character count.
        body = out.split("\r\n\r\n", 1)[1]
        header_line = out.split("\r\n", 1)[0]
        declared_len = int(header_line.split(":", 1)[1].strip())
        self.assertEqual(declared_len, len(body.encode("utf-8")))
        parsed = json.loads(body)
        self.assertEqual(parsed["result"]["name"], "测试函数")

    def test_read_message_basic(self):
        """_read_message parses a valid Content-Length frame."""
        from _builder.mcp_server import _read_message
        msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        framed = self._make_framed(msg)
        with patch("sys.stdin", new_callable=io.StringIO) as mock_in:
            mock_in.write(framed)
            mock_in.seek(0)
            result = _read_message()
        self.assertEqual(result, msg)

    def test_read_message_eof(self):
        """_read_message returns _EOF_SENTINEL on EOF (empty stdin)."""
        from _builder.mcp_server import _read_message, _EOF_SENTINEL
        with patch("sys.stdin", new_callable=io.StringIO) as mock_in:
            mock_in.write("")
            mock_in.seek(0)
            result = _read_message()
        self.assertIs(result, _EOF_SENTINEL)

    def test_read_message_large(self):
        """_read_message handles a large (1MB) message without truncation."""
        from _builder.mcp_server import _read_message
        # Build a 1MB+ payload
        big_data = "x" * (1024 * 1024)
        msg = {"jsonrpc": "2.0", "id": 2, "params": {"data": big_data}}
        framed = self._make_framed(msg)
        with patch("sys.stdin", new_callable=io.StringIO) as mock_in:
            mock_in.write(framed)
            mock_in.seek(0)
            result = _read_message()
        self.assertEqual(result, msg)
        self.assertEqual(len(result["params"]["data"]), 1024 * 1024)

    def test_read_message_malformed_json(self):
        """_read_message returns None on malformed JSON body."""
        from _builder.mcp_server import _read_message
        body = "{not valid json"
        framed = f"Content-Length: {len(body.encode('utf-8'))}\r\n\r\n{body}"
        with patch("sys.stdin", new_callable=io.StringIO) as mock_in:
            mock_in.write(framed)
            mock_in.seek(0)
            result = _read_message()
        self.assertIsNone(result)

    def test_read_message_no_content_length_fallback(self):
        """_read_message falls back to line-based reading without Content-Length."""
        from _builder.mcp_server import _read_message
        msg = {"jsonrpc": "2.0", "id": 3, "method": "ping"}
        # No Content-Length header — just a JSON line followed by newline
        with patch("sys.stdin", new_callable=io.StringIO) as mock_in:
            mock_in.write(json.dumps(msg) + "\n")
            mock_in.seek(0)
            result = _read_message()
        self.assertEqual(result, msg)

    def test_round_trip(self):
        """A message written by _write_message can be read by _read_message."""
        from _builder.mcp_server import _read_message, _write_message
        msg = {"jsonrpc": "2.0", "id": 42, "method": "tools/call",
               "params": {"name": "search", "arguments": {"keywords": "test"}}}
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            _write_message(msg)
            framed = mock_out.getvalue()
        with patch("sys.stdin", new_callable=io.StringIO) as mock_in:
            mock_in.write(framed)
            mock_in.seek(0)
            result = _read_message()
        self.assertEqual(result, msg)


class TestMcpToolDispatch(unittest.TestCase):
    """Test the tool dispatch table covers all expected tools."""

    def test_tool_registry_has_16_tools(self):
        """The MCP server should expose 16 tools (per CLAUDE.md)."""
        from _builder import mcp_server
        # The tool dispatch table is in run_mcp_server; check the _tool_* funcs
        tool_funcs = [name for name in dir(mcp_server) if name.startswith("_tool_")]
        # CLAUDE.md says 16 tools. We have at least these:
        expected = [
            "_tool_load", "_tool_search", "_tool_describe", "_tool_explore",
            "_tool_trace", "_tool_impact", "_tool_key_paths", "_tool_concurrency",
            "_tool_data_lifecycle", "_tool_domain", "_tool_knowledge_query",
            "_tool_memory_search", "_tool_semantic_status", "_tool_get_code_snippet",
            "_tool_blast_radius", "_tool_extract_signals",
        ]
        for exp in expected:
            self.assertIn(exp, tool_funcs, f"Missing tool function: {exp}")

    def test_tool_load_returns_summary(self):
        """_tool_load returns a dict with nodes/edges/api_entries keys.

        We build a minimal valid graph dir (master + one domain file) so the
        loader succeeds and we can verify the summary contract.
        """
        from _builder.mcp_server import _tool_load
        import tempfile, os, json
        with tempfile.TemporaryDirectory() as tmp:
            # Create a minimal graph dir with code2database_master.json + one
            # domain file in the legacy format (nodes + edges lists).
            domain_file = "domain_test.json"
            master = {
                "type": "code2database_master",
                "version": 1,
                "domains": {"test": domain_file},
            }
            with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
                json.dump(master, f)
            domain_data = {
                "nodes": [
                    {"id": "n1", "name": "foo", "labels": ["API_entry"],
                     "domain": "test", "is_empty": False},
                    {"id": "n2", "name": "bar", "labels": ["thread_processor"],
                     "domain": "test", "is_empty": False},
                ],
                "edges": [
                    {"source": "n1", "target": "n2", "concurrency": "INVOKES"},
                ],
            }
            with open(os.path.join(tmp, domain_file), "w") as f:
                json.dump(domain_data, f)
            result = _tool_load({}, tmp)
            self.assertIsInstance(result, dict)
            self.assertEqual(result["nodes"], 2)
            self.assertEqual(result["edges"], 1)
            self.assertEqual(result["api_entries"], 1)
            self.assertEqual(result["thread_entries"], 1)


if __name__ == "__main__":
    unittest.main()


class TestExpandedToolRegistry(unittest.TestCase):
    """Tests for the expanded MCP tool registry (D37)."""

    def test_at_least_30_tools_registered(self):
        """The TOOLS registry should have at least 30 tools after expansion."""
        from _builder.mcp_server import TOOLS
        self.assertGreaterEqual(len(TOOLS), 30)

    def test_new_tools_present(self):
        """All newly-added tools are present in the registry."""
        from _builder.mcp_server import TOOLS
        expected_new_tools = [
            "code2database_path_feasible",
            "code2database_find_invariants",
            "code2database_ffi_trace",
            "code2database_doc_code_check",
            "code2database_daemon_status",
            "code2database_who_allocates",
            "code2database_who_frees",
            "code2database_who_locks",
            "code2database_explain_label",
            "code2database_why_ambiguous",
            "code2database_audit_log",
            "code2database_happens_before",
            "code2database_memory_ordering",
            "code2database_unbalanced_alloc_free",
        ]
        for tool in expected_new_tools:
            self.assertIn(tool, TOOLS, f"missing tool: {tool}")

    def test_each_tool_has_required_fields(self):
        """Each tool entry has description, inputSchema, and handler."""
        from _builder.mcp_server import TOOLS
        for name, spec in TOOLS.items():
            self.assertIn("description", spec, f"{name} missing description")
            self.assertIn("inputSchema", spec, f"{name} missing inputSchema")
            self.assertIn("handler", spec, f"{name} missing handler")
            self.assertTrue(callable(spec["handler"]),
                            f"{name} handler is not callable")

    def test_tool_input_schemas_are_valid(self):
        """Each tool's inputSchema is a dict with type=object."""
        from _builder.mcp_server import TOOLS
        for name, spec in TOOLS.items():
            schema = spec["inputSchema"]
            self.assertEqual(schema.get("type"), "object",
                             f"{name} inputSchema.type must be 'object'")
            self.assertIn("properties", schema,
                          f"{name} inputSchema missing properties")
            self.assertIn("required", schema,
                          f"{name} inputSchema missing required")


class TestMcpSessionInitTool(unittest.TestCase):
    """code2database_session_init — one-shot session context tool."""

    def _make_graph_dir(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="c2d_mcp_sess_")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp,
                                                           ignore_errors=True))
        nodes = [{"id": f"n{i}", "name": f"n{i}", "source_file": "/tmp/x.c",
                  "line": i + 1, "domain": "test", "labels": [],
                  "is_empty": False} for i in range(3)]
        edges = [{"source": "n0", "target": "n1", "relation": "INVOKES",
                  "confidence": "EXTRACTED"}]
        with open(os.path.join(tmp, "domain_test.json"), "w") as f:
            json.dump({"nodes": nodes, "edges": edges}, f)
        with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
            json.dump({"source_root": "/tmp",
                       "domains": {"test": "domain_test.json"}}, f)
        return tmp

    def test_registry_contains_session_init(self):
        from _builder.mcp_server import TOOLS
        self.assertIn("code2database_session_init", TOOLS)
        spec = TOOLS["code2database_session_init"]
        # no required params — callable with an empty arguments dict
        self.assertEqual(spec["inputSchema"]["required"], [])

    def test_empty_graph_dir_degrades_to_hints(self):
        """Bare graph dir: the tool never raises, layers become hints."""
        from _builder.mcp_server import _tool_session_init
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = _tool_session_init({}, tmp)
            self.assertIsInstance(result, dict)
            for key in ("brief", "brief_rendered", "memory", "graph",
                        "known_unknowns", "hints", "rendered"):
                self.assertIn(key, result)
            self.assertIn("=== Code2Database Session Context ===",
                          result["rendered"])
            self.assertTrue(any("brief-extract" in h
                                for h in result["hints"]))

    def test_full_context_renders_all_layers(self):
        """Populated graph dir: brief + memory digest flow into the
        prompt-ready rendered form."""
        from _builder.mcp_server import _tool_session_init
        from _builder.brief import brief_extract, brief_update
        from _builder.memory_store import MemoryStore
        graph_dir = self._make_graph_dir()
        brief_extract(graph_dir)
        brief_update(graph_dir, set_field="project", set_value="PX")
        store = MemoryStore(graph_dir)
        store.add("How does nvme submit IO?", "doorbell",
                  category="bdev/nvme/pcie", author="alice", no_merge=True)
        result = _tool_session_init({"top": 5}, graph_dir)
        self.assertEqual(result["graph"]["nodes"], 3)
        digest = result["memory"]["digest"]
        self.assertEqual(len(digest), 1)
        self.assertEqual(digest[0]["question"], "How does nvme submit IO?")
        # structured field AND rendered text both carry the context
        self.assertIn("[bdev/nvme/pcie]", result["rendered"])
        self.assertIn("doorbell", result["rendered"])
        self.assertIn("3 nodes", result["rendered"])

    def test_top_param_clamps_digest(self):
        """top=1 limits the memory digest length."""
        from _builder.mcp_server import _tool_session_init
        from _builder.memory_store import MemoryStore
        graph_dir = self._make_graph_dir()
        store = MemoryStore(graph_dir)
        for i in range(3):
            store.add(f"q{i}", f"a{i}", category="cat", no_merge=True)
        result = _tool_session_init({"top": 1}, graph_dir)
        self.assertEqual(len(result["memory"]["digest"]), 1)

    def test_session_init_digest_shows_symbols(self):
        """Symbol-grounded digest entries render with ⟨symbols⟩."""
        from _builder.mcp_server import _tool_session_init
        from _builder.memory_store import MemoryStore
        graph_dir = self._make_graph_dir()
        store = MemoryStore(graph_dir)
        store.add("how to submit", "doorbell", category="nvme",
                  no_merge=True, symbols=["nvme_submit_cmd"])
        result = _tool_session_init({}, graph_dir)
        self.assertIn("⟨nvme_submit_cmd⟩", result["rendered"])


class TestMcpMemorySearchSymbol(unittest.TestCase):
    """Round 24: memory_search symbol filter (memory↔code grounding)."""

    def test_symbol_filter_routes_to_store(self):
        from _builder.mcp_server import _tool_memory_search
        from _builder.memory_store import MemoryStore
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp(prefix="c2d_mcp_sym_")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        store = MemoryStore(tmp)
        store.add("how does submit work", "doorbell", no_merge=True,
                  symbols=["nvme_submit_cmd"])
        store.add("unrelated thing", "x", no_merge=True)
        out = _tool_memory_search(
            {"query": "submit", "symbol": "NVME_SUBMIT_CMD"}, tmp)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["question"], "how does submit work")
        self.assertEqual(out[0]["symbols"], ["nvme_submit_cmd"])

    def test_symbol_no_match_returns_empty_list(self):
        from _builder.mcp_server import _tool_memory_search
        from _builder.memory_store import MemoryStore
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp(prefix="c2d_mcp_sym2_")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        MemoryStore(tmp).add("q", "a", no_merge=True)
        out = _tool_memory_search({"query": "q", "symbol": "nope"}, tmp)
        self.assertEqual(out, [])

    def test_registry_schema_has_symbol_param(self):
        from _builder.mcp_server import TOOLS
        props = TOOLS["code2database_memory_search"]["inputSchema"][
            "properties"]
        self.assertIn("symbol", props)
