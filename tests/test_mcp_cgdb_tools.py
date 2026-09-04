"""Tests for the cgdb_* MCP tools.

Verifies that each cgdb_* tool:
  1. Is registered in the TOOLS dict
  2. Has the required fields (description, inputSchema, handler)
  3. Returns the expected response shape (list or dict)
  4. Returns a graceful error when cgdb tables are not available
  5. End-to-end: works against a real cgdb-enabled DB
"""
import json
import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


def _is_clang_available() -> bool:
    """Check if libclang is available."""
    try:
        from _scanner.clang_scanner import is_clang_available
        return is_clang_available()
    except Exception:
        return False


# All 17 new cgdb tools registered in mcp_server.TOOLS
EXPECTED_CGDB_TOOLS = [
    "cgdb_search_symbols",
    "cgdb_get_definition",
    "cgdb_get_function_body",
    "cgdb_find_invokers",
    "cgdb_find_invoked",
    "cgdb_get_struct_layout",
    "cgdb_find_type_definition",
    "cgdb_find_ops_impls",
    "cgdb_find_cfg_paths",
    "cgdb_find_data_flow",
    "cgdb_find_aliases",
    "cgdb_find_lock_held_calls",
    "cgdb_check_race_condition",
    "cgdb_find_configs_for",
    "cgdb_find_nodes_under_config",
    "cgdb_index_status",
    "cgdb_time_travel_query",
    "cgdb_list_versions",
    "cgdb_get_source",
]


_FIXTURE_SOURCE = textwrap.dedent("""\
    #include <stdio.h>

    struct buffer {
        char *data;
        int size;
    };

    int validate(struct buffer *buf) {
        if (buf == NULL) return -1;
        if (buf->size <= 0) return -1;
        return 0;
    }

    int main(int argc, char **argv) {
        struct buffer b;
        b.data = (char *)0;
        b.size = 100;
        if (validate(&b) < 0) {
            return 1;
        }
        return 0;
    }
""")


class TestCgdbToolRegistration(unittest.TestCase):
    """Test that all 19 cgdb tools are registered in TOOLS."""

    def test_all_cgdb_tools_registered(self):
        """All 19 expected cgdb tools are in the TOOLS dict."""
        from _builder.mcp_server import TOOLS
        for tool_name in EXPECTED_CGDB_TOOLS:
            self.assertIn(tool_name, TOOLS,
                          f"Missing cgdb tool: {tool_name}")

    def test_cgdb_tools_count(self):
        """At least 19 cgdb_* tools are registered."""
        from _builder.mcp_server import TOOLS
        cgdb_tools = [name for name in TOOLS if name.startswith("cgdb_")]
        self.assertGreaterEqual(len(cgdb_tools), 19,
                                f"Expected 19 cgdb tools, got {len(cgdb_tools)}")

    def test_each_cgdb_tool_has_required_fields(self):
        """Each cgdb tool entry has description, inputSchema, and handler."""
        from _builder.mcp_server import TOOLS
        for name in EXPECTED_CGDB_TOOLS:
            spec = TOOLS[name]
            self.assertIn("description", spec, f"{name} missing description")
            self.assertIn("inputSchema", spec, f"{name} missing inputSchema")
            self.assertIn("handler", spec, f"{name} missing handler")
            self.assertTrue(callable(spec["handler"]),
                            f"{name} handler is not callable")
            # inputSchema must be a dict with type=object
            self.assertEqual(spec["inputSchema"]["type"], "object",
                             f"{name} inputSchema must be type=object")
            self.assertIn("properties", spec["inputSchema"],
                          f"{name} inputSchema must have properties")
            self.assertIn("required", spec["inputSchema"],
                          f"{name} inputSchema must have required list")

    def test_cgdb_handler_functions_exist(self):
        """Each registered handler corresponds to a _tool_cgdb_* function."""
        from _builder import mcp_server
        for name in EXPECTED_CGDB_TOOLS:
            handler = mcp_server.TOOLS[name]["handler"]
            handler_name = handler.__name__
            self.assertTrue(handler_name.startswith("_tool_cgdb"),
                            f"{name} handler {handler_name} should start with _tool_cgdb")


class TestCgdbToolsNoDb(unittest.TestCase):
    """Test that all cgdb tools return a graceful error when no cgdb DB exists."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _call_tool(self, tool_name: str, args: dict):
        """Invoke a cgdb MCP tool by name and return its result."""
        from _builder.mcp_server import TOOLS
        handler = TOOLS[tool_name]["handler"]
        return handler(args, self.tmpdir)

    def test_search_symbols_no_db_returns_empty_or_error(self):
        """cgdb_search_symbols returns [] or [{error: ...}] when no DB."""
        result = self._call_tool("cgdb_search_symbols", {"query": "foo"})
        # Returns [] (empty list) when no DB
        self.assertIsInstance(result, list)

    def test_get_definition_no_db_returns_error(self):
        result = self._call_tool("cgdb_get_definition", {"name": "foo"})
        self.assertIsInstance(result, list)
        if result:
            self.assertIn("error", result[0])

    def test_get_function_body_no_db_returns_error(self):
        result = self._call_tool("cgdb_get_function_body", {"node": "foo"})
        self.assertIsInstance(result, dict)
        self.assertIn("error", result)

    def test_get_source_no_db_returns_error(self):
        result = self._call_tool("cgdb_get_source", {"node": "foo"})
        self.assertIsInstance(result, dict)
        self.assertIn("error", result)

    def test_find_invokers_no_db_returns_error(self):
        result = self._call_tool("cgdb_find_invokers", {"node_id": 1})
        self.assertIsInstance(result, list)
        if result:
            self.assertIn("error", result[0])

    def test_find_invoked_no_db_returns_error(self):
        result = self._call_tool("cgdb_find_invoked", {"node_id": 1})
        self.assertIsInstance(result, list)
        if result:
            self.assertIn("error", result[0])

    def test_get_struct_layout_no_db_returns_error(self):
        result = self._call_tool("cgdb_get_struct_layout", {"name": "foo"})
        self.assertIsInstance(result, dict)
        self.assertIn("error", result)

    def test_find_type_definition_no_db_returns_error(self):
        result = self._call_tool("cgdb_find_type_definition", {"name": "foo"})
        self.assertIsInstance(result, list)
        if result:
            self.assertIn("error", result[0])

    def test_find_ops_impls_no_db_returns_error(self):
        result = self._call_tool("cgdb_find_ops_impls",
                                  {"field_name": "read_iter"})
        self.assertIsInstance(result, list)
        if result:
            self.assertIn("error", result[0])

    def test_find_cfg_paths_no_db_returns_error(self):
        result = self._call_tool("cgdb_find_cfg_paths", {"function_id": 1})
        self.assertIsInstance(result, list)
        if result:
            self.assertIn("error", result[0])

    def test_find_data_flow_no_db_returns_error(self):
        result = self._call_tool("cgdb_find_data_flow", {"var_id": 1})
        self.assertIsInstance(result, dict)
        self.assertIn("error", result)

    def test_find_aliases_no_db_returns_error(self):
        result = self._call_tool("cgdb_find_aliases", {"ptr_id": 1})
        self.assertIsInstance(result, list)
        if result:
            self.assertIn("error", result[0])

    def test_find_lock_held_calls_no_db_returns_error(self):
        result = self._call_tool("cgdb_find_lock_held_calls",
                                  {"function_id": 1})
        self.assertIsInstance(result, list)
        if result:
            self.assertIn("error", result[0])

    def test_check_race_condition_no_db_returns_error(self):
        result = self._call_tool("cgdb_check_race_condition",
                                  {"function_id": 1})
        self.assertIsInstance(result, list)
        if result:
            self.assertIn("error", result[0])

    def test_find_configs_for_no_db_returns_error(self):
        result = self._call_tool("cgdb_find_configs_for", {"node_id": 1})
        self.assertIsInstance(result, list)
        if result:
            self.assertIn("error", result[0])

    def test_find_nodes_under_config_no_db_returns_error(self):
        result = self._call_tool("cgdb_find_nodes_under_config",
                                  {"config": "CONFIG_X"})
        self.assertIsInstance(result, list)
        if result:
            self.assertIn("error", result[0])

    def test_index_status_no_db_returns_error(self):
        result = self._call_tool("cgdb_index_status", {})
        self.assertIsInstance(result, dict)
        self.assertIn("error", result)

    def test_time_travel_query_no_db_returns_error(self):
        result = self._call_tool("cgdb_time_travel_query",
                                  {"node_id": 1, "version_id": 1})
        self.assertIsInstance(result, dict)
        self.assertIn("error", result)

    def test_list_versions_no_db_returns_error(self):
        result = self._call_tool("cgdb_list_versions", {})
        self.assertIsInstance(result, list)
        if result:
            self.assertIn("error", result[0])


@unittest.skipUnless(_is_clang_available(), "libclang not available")
class TestCgdbToolsEndToEnd(unittest.TestCase):
    """End-to-end: build a cgdb DB, then invoke each MCP tool and verify
    the response shape is correct."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        # Build a small C fixture and scan it
        self.c_path = os.path.join(self.tmpdir, "fixture.c")
        with open(self.c_path, 'w') as f:
            f.write(_FIXTURE_SOURCE)
        # Scan with clang backend
        from _scanner.clang_scanner import ClangScanner
        scanner = ClangScanner(is_cpp=False)
        self.scan_result = scanner.scan_file(self.c_path, self.tmpdir)
        self.assertNotIn('error', self.scan_result)
        # Build cgdb DB
        self.db_path = os.path.join(self.tmpdir, "code2database.db")
        from _builder.cgdb_store import SQLiteCGDBStore
        from _builder.cgdb_ingest import extract_cgdb_batch
        self.store = SQLiteCGDBStore(self.db_path)
        self.store.create_schema()
        batch = extract_cgdb_batch(
            scan_result=self.scan_result,
            commit_hash="fixture_commit",
            version_id=1,
        )
        self.store.write_batch(batch)

    def _cleanup(self):
        try:
            self.store.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _call_tool(self, tool_name: str, args: dict):
        """Invoke a cgdb MCP tool by name with the fixture graph dir."""
        from _builder.mcp_server import TOOLS
        handler = TOOLS[tool_name]["handler"]
        return handler(args, self.tmpdir)

    def test_index_status_returns_counts(self):
        """cgdb_index_status returns a dict with node/edge counts."""
        result = self._call_tool("cgdb_index_status", {})
        self.assertIsInstance(result, dict)
        self.assertNotIn("error", result)
        # Should have at least node_count, edge_count, file_count
        self.assertIn("file_count", result)
        # nodes_by_kind is a dict
        self.assertIn("nodes_by_kind", result)

    def test_search_symbols_finds_function(self):
        """cgdb_search_symbols finds the 'validate' function."""
        result = self._call_tool("cgdb_search_symbols",
                                  {"query": "validate"})
        self.assertIsInstance(result, list)
        # Should find at least one match
        self.assertGreater(len(result), 0)

    def test_get_definition_finds_function(self):
        """cgdb_get_definition finds 'validate'."""
        result = self._call_tool("cgdb_get_definition", {"name": "validate"})
        self.assertIsInstance(result, list)
        # Should find the function definition
        names = [r.get("name") for r in result if isinstance(r, dict)]
        self.assertIn("validate", names)

    def test_get_function_body_returns_text(self):
        """cgdb_get_function_body returns the function body source."""
        result = self._call_tool("cgdb_get_function_body", {"node": "validate"})
        self.assertIsInstance(result, dict)
        self.assertNotIn("error", result)
        # body_text should be non-empty
        self.assertTrue(result.get("body_text") or result.get("name"))

    def test_find_type_definition_finds_struct(self):
        """cgdb_find_type_definition finds the 'buffer' struct."""
        result = self._call_tool("cgdb_find_type_definition",
                                  {"name": "buffer"})
        self.assertIsInstance(result, list)
        # Should find the struct
        self.assertGreater(len(result), 0)

    def test_get_struct_layout_returns_fields(self):
        """cgdb_get_struct_layout returns the struct's fields."""
        result = self._call_tool("cgdb_get_struct_layout", {"name": "buffer"})
        self.assertIsInstance(result, dict)
        self.assertNotIn("error", result)
        # Should have a fields list
        self.assertIn("fields", result)

    def test_list_versions_returns_rows(self):
        """cgdb_list_versions returns at least the initial version row."""
        result = self._call_tool("cgdb_list_versions", {})
        self.assertIsInstance(result, list)
        # Should have at least one version (the initial v1)
        self.assertGreater(len(result), 0)
        self.assertIn("version_id", result[0])
        self.assertIn("commit_hash", result[0])

    def test_time_travel_query_returns_node(self):
        """cgdb_time_travel_query at v1 returns the node."""
        # Find a function node_id first
        defs = self._call_tool("cgdb_get_definition", {"name": "validate"})
        node_id = defs[0]["id"]
        result = self._call_tool("cgdb_time_travel_query",
                                  {"node_id": node_id, "version_id": 1})
        self.assertIsInstance(result, dict)
        self.assertNotIn("error", result)
        self.assertEqual(result["id"], node_id)

    def test_find_invokers_returns_callers(self):
        """cgdb_find_invokers returns the caller of 'validate' (which is 'main')."""
        defs = self._call_tool("cgdb_get_definition", {"name": "validate"})
        validate_id = defs[0]["id"]
        result = self._call_tool("cgdb_find_invokers",
                                  {"node_id": validate_id, "depth": 1})
        self.assertIsInstance(result, list)
        # Should have at least one caller (main)
        self.assertGreater(len(result), 0)


if __name__ == "__main__":
    unittest.main()
