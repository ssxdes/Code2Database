"""Tests for the cgdb-export-failure marker (M3).

The cgdb export failure used to be logged as a stderr WARNING only:
builds reported success while cgdb semantic tables (L1 tokens, types,
vtables, invoke_sites, ...) were silently missing or partial — e.g.
when pool workers were killed by the OOM killer
("A process in the process pool was terminated abruptly...").
Now the failure is escalated to ERROR, persisted as
.code2database_cgdb_export_failed.json in the output dir, surfaced in
the build's stdout summary, and cleared again by the next successful
cgdb export.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest

_HERE = os.path.dirname(__file__)
SCRIPTS = os.path.normpath(os.path.join(_HERE, '..', 'scripts'))
sys.path.insert(0, SCRIPTS)

from _builder.graph_build import (
    _mark_cgdb_export_failed,
    _clear_cgdb_export_failed,
    _CGDB_EXPORT_FAILED_MARKER,
)


_TEST_C_SOURCE = textwrap.dedent("""\
    struct buffer { char *data; int size; };
    int validate(struct buffer *buf) {
        if (buf == NULL) return -1;
        if (buf->size <= 0) return -1;
        return 0;
    }
    int main(int argc, char **argv) {
        struct buffer b;
        b.size = 100;
        if (validate(&b) < 0) return 1;
        return 0;
    }
""")


class TestMarkerHelpers(unittest.TestCase):

    def test_mark_writes_payload_and_clear_removes(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = _mark_cgdb_export_failed(
                tmp, "boom: pool broke", {"stage": "cgdb_export"})
            self.assertEqual(
                os.path.basename(marker), _CGDB_EXPORT_FAILED_MARKER)
            self.assertTrue(os.path.exists(marker))
            with open(marker, encoding="utf-8") as f:
                payload = json.load(f)
            self.assertEqual(payload["error"], "boom: pool broke")
            self.assertEqual(payload["stage"], "cgdb_export")
            self.assertIn("failed_at", payload)
            # Clear removes it; clearing again is a no-op.
            _clear_cgdb_export_failed(tmp)
            self.assertFalse(os.path.exists(marker))
            _clear_cgdb_export_failed(tmp)  # must not raise
            self.assertFalse(os.path.exists(marker))


class TestBuildWiring(unittest.TestCase):
    """Full CLI build with a FORCED cgdb export failure (via
    sitecustomize injection) must: exit 0, write the marker, print the
    WARNING on stdout — and a subsequent clean build must clear it."""

    def setUp(self):
        try:
            from _scanner.clang_scanner import is_clang_available
        except ImportError:
            self.skipTest("ClangScanner not available")
        if not is_clang_available():
            self.skipTest("libclang not available")
        self.tmpdir = tempfile.mkdtemp(prefix="cgdb_marker_")
        self.addCleanup(lambda: shutil.rmtree(self.tmpdir, ignore_errors=True))
        self.src_dir = os.path.join(self.tmpdir, "src")
        os.makedirs(self.src_dir)
        with open(os.path.join(self.src_dir, "m.c"), "w") as f:
            f.write(_TEST_C_SOURCE)
        self.extraction = os.path.join(self.tmpdir, "extraction.json")
        self.outdir = os.path.join(self.tmpdir, "out")
        self.inject_dir = os.path.join(self.tmpdir, "inject")

    def _run(self, cmd, extra_env=None, check=True):
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180,
            cwd=os.path.dirname(SCRIPTS), env=env)
        if check and result.returncode != 0:
            raise AssertionError(
                f"Command failed: {' '.join(cmd)}\n"
                f"stdout: {result.stdout[-3000:]}\nstderr: {result.stderr[-3000:]}")
        return result

    def _scan(self):
        self._run([sys.executable,
                   os.path.join(SCRIPTS, "code2database_scanner.py"), "scan",
                   "--source", self.src_dir, "--output", self.extraction,
                   "--extraction-backend", "auto"])

    def _build(self, force_fail=False):
        cmd = [sys.executable, os.path.join(SCRIPTS, "code2database_builder.py"),
               "build", "--extraction", self.extraction,
               "--outdir", self.outdir, "--build-config", "auto",
               "--storage", "sqlite"]
        if force_fail:
            # sitecustomize.py is auto-imported by the child interpreter
            # at startup; patch extract_cgdb_batch to raise so the cgdb
            # export block fails exactly like a killed pool would.
            os.makedirs(self.inject_dir, exist_ok=True)
            with open(os.path.join(self.inject_dir, "sitecustomize.py"),
                      "w") as f:
                f.write(
                    "import _builder.cgdb_ingest as _ci\n"
                    "def _boom(*a, **k):\n"
                    "    raise RuntimeError('forced cgdb export failure')\n"
                    "_ci.extract_cgdb_batch = _boom\n")
            env = {"PYTHONPATH": self.inject_dir + os.pathsep + SCRIPTS}
            return self._run(cmd, extra_env=env, check=False)
        return self._run(cmd, check=False)

    def test_failed_export_writes_marker_then_clean_build_clears(self):
        self._scan()
        marker = os.path.join(self.outdir, _CGDB_EXPORT_FAILED_MARKER)

        # 1. Forced-failure build: exits 0 (legacy graph still usable),
        #    writes the marker, warns on stdout.
        r = self._build(force_fail=True)
        self.assertEqual(r.returncode, 0,
                         f"build must survive cgdb export failure\n"
                         f"stdout: {r.stdout[-2000:]}\nstderr: {r.stderr[-2000:]}")
        self.assertTrue(os.path.exists(marker),
                        "failure marker must exist after failed export")
        with open(marker, encoding="utf-8") as f:
            payload = json.load(f)
        self.assertIn("forced cgdb export failure", payload["error"])
        self.assertIn("cgdb export FAILED", r.stdout)
        self.assertIn(_CGDB_EXPORT_FAILED_MARKER, r.stdout)

        # 2. Clean rebuild: cgdb export succeeds -> marker cleared.
        r2 = self._build()
        self.assertEqual(r2.returncode, 0,
                         f"clean rebuild failed\nstdout: {r2.stdout[-2000:]}"
                         f"\nstderr: {r2.stderr[-2000:]}")
        self.assertFalse(os.path.exists(marker),
                         "stale failure marker must be cleared by a "
                         "successful cgdb export")
        # And the clean build actually populated cgdb tables.
        db = os.path.join(self.outdir, "code2database.db")
        conn = sqlite3.connect(db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM cgdb_nodes").fetchone()[0]
        finally:
            conn.close()
        self.assertGreater(n, 0)


if __name__ == "__main__":
    unittest.main()
