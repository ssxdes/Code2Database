"""Every shipped surface must carry the same version number.

The version lives in one place — scripts/_version.py — and the CLI
entry points, the three skill manifests, the MCP registry manifest,
the SARIF tool driver and the LSP serverInfo all reference it. A
drift between any two of these means an upgrade path a customer
cannot reason about, so this suite pins them together.
"""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _version


def _load_entry(script: str):
    spec = importlib.util.spec_from_file_location(
        "_verprobe_" + Path(script).stem, SCRIPTS / script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestVersionSingleSource(unittest.TestCase):

    def test_builder_version_imported_from_single_source(self):
        builder = _load_entry("code2database_builder.py")
        self.assertEqual(builder.__version__, _version.__version__)

    def test_scanner_reports_version_flag(self):
        scanner = _load_entry("code2database_scanner.py")
        out = io.StringIO()
        old_argv = sys.argv[:]
        try:
            sys.argv = ["code2database_scanner.py", "--version"]
            with contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit) as cm:
                    scanner.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(cm.exception.code, 0)
        self.assertIn(_version.__version__, out.getvalue())

    def test_skill_manifests_share_the_version(self):
        for name in ("skill.json", "skill_analysis.json", "skill_ops.json"):
            data = json.loads((REPO / name).read_text())
            self.assertEqual(data["version"], _version.__version__, name)

    def test_mcp_registry_manifest_shares_the_version(self):
        data = json.loads((REPO / "server.json").read_text())
        self.assertEqual(data["version"], _version.__version__)
        for pkg in data.get("packages", []):
            self.assertEqual(pkg["version"], _version.__version__)

    def test_sarif_tool_driver_defaults_to_single_source(self):
        from _builder.export.sarif_output import results_to_sarif
        doc = results_to_sarif([{
            "rule_id": "probe-rule",
            "message": "probe message",
            "level": "warning",
            "file": "a.c",
            "line": 1,
        }])
        driver = doc["runs"][0]["tool"]["driver"]
        self.assertEqual(driver["version"], _version.__version__)

    def test_lsp_server_info_shares_the_version(self):
        from _builder.misc.lsp_server import LSPServer
        with tempfile.TemporaryDirectory() as tmp:
            server = LSPServer(tmp)
            # serverInfo is static; skip the graph cache load entirely.
            server._ensure_cache = lambda: None
            info = server.initialize({})
        self.assertEqual(info["serverInfo"]["version"], _version.__version__)


if __name__ == "__main__":
    unittest.main()
