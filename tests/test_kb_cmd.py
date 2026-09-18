"""CLI surface of the kb sub-skill: kb-init and the domain registry.

kb-init provisions a standalone knowledge/memory store (no graph
artifacts); the kb-domain-* commands manage the store's identity and
the other kb domains it may query. Runs the real builder CLI.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "scripts")
BUILDER = os.path.join(SCRIPTS, "code2database_builder.py")


def _run(*argv, cwd=None):
    return subprocess.run(
        [sys.executable, BUILDER, "--log-level", "CRITICAL"] + list(argv),
        capture_output=True, text=True, timeout=120, cwd=cwd)


class TestKbInit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_kbinit_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = os.path.join(self.tmp, "acme", "code2db-out")

    def test_init_provisions_all_stores_without_graph(self):
        proc = _run("kb-init", "--graph", self.store, "--name", "acme-kb")
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        for rel in ("memory" + os.sep + "memory.db",
                    "knowledge" + os.sep + "knowledge.db",
                    "kb_index.db"):
            self.assertTrue(os.path.isfile(os.path.join(self.store, rel)),
                            f"missing {rel}")
        for rel in ("code2database.db", "code2database_master.json"):
            self.assertFalse(os.path.exists(os.path.join(self.store, rel)),
                             f"graph artifact {rel} must not be created")
        self.assertIn("acme-kb", proc.stdout)

    def test_init_is_idempotent(self):
        _run("kb-init", "--graph", self.store)
        proc = _run("kb-init", "--graph", self.store, "--name", "renamed")
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("(existing)", proc.stdout)
        # rename took effect
        proc2 = _run("kb-domain-name", "--graph", self.store)
        self.assertIn("renamed", proc2.stdout)

    def test_init_then_capture_then_query_workflow(self):
        _run("kb-init", "--graph", self.store, "--name", "acme-kb")
        proc = _run("save-memory", "--graph", self.store,
                    "--question", "how does the doorbell register work",
                    "--answer", "write the submission tail to ring it",
                    "--category", "nvme/queue", "--author", "tester",
                    "--version-scope", "main")
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        q = _run("kb-query", "--graph", self.store,
                 "--query", "doorbell register",
                 "--version-scope", "main")
        self.assertEqual(q.returncode, 0, q.stderr[-2000:])
        self.assertIn("doorbell", q.stdout)

    def test_init_auto_discovers_kb_only_store(self):
        # A knowledge-only code2db-out resolves without --graph.
        _run("kb-init", "--graph", self.store)
        proc = _run("kb-domain-name", cwd=os.path.dirname(self.store))
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("acme", proc.stdout)  # parent-dir fallback name


class TestDomainRegistryCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_kbdom_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.local = os.path.join(self.tmp, "beta", "code2db-out")
        self.remote = os.path.join(self.tmp, "gamma", "code2db-out")
        for d in (self.local, self.remote):
            proc = _run("kb-init", "--graph", d)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        proc = _run("save-memory", "--graph", self.remote,
                    "--question", "how does the retry timer restart",
                    "--answer", "call the periodic reset path",
                    "--category", "timers", "--author", "tester")
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])

    def test_add_list_remove_cycle(self):
        add = _run("kb-domain-add", "--graph", self.local,
                   "--path", self.remote, "--name", "gamma-kb")
        self.assertEqual(add.returncode, 0, add.stderr[-2000:])
        listing = _run("kb-domain-list", "--graph", self.local)
        self.assertEqual(listing.returncode, 0, listing.stderr[-2000:])
        self.assertIn("gamma-kb", listing.stdout)
        rem = _run("kb-domain-remove", "--graph", self.local,
                   "--path", self.remote)
        self.assertEqual(rem.returncode, 0, rem.stderr[-2000:])
        self.assertIn('"removed": true', rem.stdout)

    def test_cross_query_via_cli(self):
        _run("kb-domain-add", "--graph", self.local, "--path", self.remote)
        q = _run("kb-query", "--graph", self.local,
                 "--query", "retry timer restart", "--cross")
        self.assertEqual(q.returncode, 0, q.stderr[-2000:])
        self.assertIn("gamma", q.stdout)
        self.assertIn("source_domain", q.stdout)

    def test_add_rejects_missing_store(self):
        nowhere = os.path.join(self.tmp, "nowhere")
        add = _run("kb-domain-add", "--graph", self.local,
                   "--path", nowhere)
        self.assertNotEqual(add.returncode, 0)


if __name__ == "__main__":
    unittest.main()
