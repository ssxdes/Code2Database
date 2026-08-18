"""Tests for the audit log module."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.audit_log import (
    log_audit, query_audit_log, new_tx_id, annotate_fact_source,
)


class TestLogAuditJSON(unittest.TestCase):
    """Test audit logging in JSON backend (audit_log.jsonl)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_log_creates_jsonl_file(self):
        """log_audit creates audit_log.jsonl when no SQLite db exists."""
        ok = log_audit(self.tmpdir,
                       command="update-node",
                       target_kind="node",
                       target_id="n1",
                       action="update",
                       attribute="semantic_desc",
                       before_value="old",
                       after_value="new",
                       reason="test")
        self.assertTrue(ok)
        log_path = os.path.join(self.tmpdir, "audit_log.jsonl")
        self.assertTrue(os.path.exists(log_path))

    def test_log_entries_are_valid_json(self):
        """Each line in audit_log.jsonl is valid JSON with expected fields."""
        log_audit(self.tmpdir, command="update-node",
                  target_kind="node", target_id="n1", action="update",
                  attribute="semantic_desc", after_value="v1")
        log_audit(self.tmpdir, command="update-edge",
                  target_kind="edge", target_id="a->b", action="update",
                  attribute="confidence", after_value="EXTRACTED")
        log_path = os.path.join(self.tmpdir, "audit_log.jsonl")
        with open(log_path) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            rec = json.loads(line)
            self.assertIn("timestamp", rec)
            self.assertIn("command", rec)
            self.assertIn("target_kind", rec)
            self.assertIn("target_id", rec)

    def test_long_value_truncated(self):
        """Long values (>4KB) are truncated to keep log compact."""
        long_value = "x" * 5000
        log_audit(self.tmpdir, command="update-node",
                  target_kind="node", target_id="n1",
                  attribute="body_text", after_value=long_value)
        log_path = os.path.join(self.tmpdir, "audit_log.jsonl")
        with open(log_path) as f:
            rec = json.loads(f.readline())
        # after_value should contain the truncation marker
        self.assertIn("truncated", rec["after_value"])
        # Truncated to roughly 4KB + marker, far less than 5KB
        self.assertLess(len(rec["after_value"]), 4300)


class TestQueryAuditLogJSON(unittest.TestCase):
    """Test querying the JSON audit log."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_query_returns_all_entries(self):
        """query_audit_log returns all entries when no filters."""
        log_audit(self.tmpdir, command="update-node", target_id="n1")
        log_audit(self.tmpdir, command="update-edge", target_id="a->b")
        result = query_audit_log(self.tmpdir)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["source"], "json")
        self.assertEqual(len(result["entries"]), 2)

    def test_query_filter_by_target_id(self):
        """query_audit_log filters by target_id."""
        log_audit(self.tmpdir, command="update-node", target_id="n1")
        log_audit(self.tmpdir, command="update-node", target_id="n2")
        result = query_audit_log(self.tmpdir, target_id="n1")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["entries"][0]["target_id"], "n1")

    def test_query_filter_by_command(self):
        """query_audit_log filters by command name."""
        log_audit(self.tmpdir, command="update-node", target_id="n1")
        log_audit(self.tmpdir, command="auto-enhance", target_id="n2")
        result = query_audit_log(self.tmpdir, command="auto-enhance")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["entries"][0]["command"], "auto-enhance")

    def test_query_filter_by_tx_id(self):
        """query_audit_log filters by tx_id (multi-step transactions)."""
        tx = new_tx_id()
        log_audit(self.tmpdir, command="patch-from-diff", target_id="n1",
                  tx_id=tx)
        log_audit(self.tmpdir, command="patch-from-diff", target_id="n2",
                  tx_id=tx)
        log_audit(self.tmpdir, command="update-node", target_id="n3",
                  tx_id="other_tx")
        result = query_audit_log(self.tmpdir, tx_id=tx)
        self.assertEqual(result["total"], 2)

    def test_query_limit_and_offset(self):
        """query_audit_log respects limit and offset."""
        for i in range(5):
            log_audit(self.tmpdir, command="update-node",
                      target_id=f"n{i}", action="update")
        result = query_audit_log(self.tmpdir, limit=2, offset=0)
        self.assertEqual(len(result["entries"]), 2)
        self.assertEqual(result["total"], 5)
        # Newest first: n4, n3
        ids = [e["target_id"] for e in result["entries"]]
        self.assertEqual(ids, ["n4", "n3"])
        # Offset 2: n2, n1
        result2 = query_audit_log(self.tmpdir, limit=2, offset=2)
        ids2 = [e["target_id"] for e in result2["entries"]]
        self.assertEqual(ids2, ["n2", "n1"])


class TestQueryAuditLogSQLite(unittest.TestCase):
    """Test querying the SQLite audit log."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        # Create a SQLite db with the audit_log table
        db_path = os.path.join(self.tmpdir, "code2database.db")
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                operator TEXT,
                command TEXT,
                target_kind TEXT,
                target_id TEXT,
                action TEXT,
                attribute TEXT,
                before_value TEXT,
                after_value TEXT,
                reason TEXT,
                tx_id TEXT
            );
        """)
        conn.commit()
        conn.close()

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_log_to_sqlite(self):
        """log_audit writes to SQLite audit_log table when db exists."""
        ok = log_audit(self.tmpdir,
                       command="update-node",
                       target_kind="node",
                       target_id="n1",
                       action="update",
                       attribute="semantic_desc",
                       after_value="new")
        self.assertTrue(ok)
        # Verify in db
        db_path = os.path.join(self.tmpdir, "code2database.db")
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT command, target_id FROM audit_log").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "update-node")
        self.assertEqual(rows[0][1], "n1")

    def test_query_from_sqlite(self):
        """query_audit_log reads from SQLite when db exists."""
        log_audit(self.tmpdir, command="update-node", target_id="n1",
                  action="update", attribute="x")
        log_audit(self.tmpdir, command="auto-enhance", target_id="n2",
                  action="apply", attribute="y")
        result = query_audit_log(self.tmpdir)
        self.assertEqual(result["source"], "sqlite")
        self.assertEqual(result["total"], 2)
        # Newest first: n2 (auto-enhance), n1 (update-node)
        self.assertEqual(result["entries"][0]["target_id"], "n2")
        self.assertEqual(result["entries"][1]["target_id"], "n1")

    def test_query_filter_target_id_sqlite(self):
        """Filtering by target_id works on SQLite backend."""
        log_audit(self.tmpdir, command="update-node", target_id="n1")
        log_audit(self.tmpdir, command="update-node", target_id="n2")
        result = query_audit_log(self.tmpdir, target_id="n1")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["entries"][0]["target_id"], "n1")


class TestAnnotateFactSource(unittest.TestCase):
    """Test the fact-source annotation helper."""

    def test_annotate_first_source(self):
        """annotate_fact_source adds a fact_source marker to a node."""
        node = {"name": "foo", "semantic_desc": "does foo"}
        annotate_fact_source(node, source="llm", command="auto-enhance")
        self.assertIn("_fact_sources", node)
        self.assertIn("auto-enhance", node["_fact_sources"])
        self.assertEqual(node["_fact_sources"]["auto-enhance"]["source"], "llm")
        self.assertIn("timestamp", node["_fact_sources"]["auto-enhance"])

    def test_annotate_multiple_sources(self):
        """annotate_fact_source preserves prior sources when adding new ones."""
        node = {"name": "foo"}
        annotate_fact_source(node, source="ast", command="scan")
        annotate_fact_source(node, source="llm", command="auto-enhance")
        self.assertEqual(len(node["_fact_sources"]), 2)
        self.assertIn("scan", node["_fact_sources"])
        self.assertIn("auto-enhance", node["_fact_sources"])

    def test_annotate_with_explicit_timestamp(self):
        """annotate_fact_source accepts an explicit timestamp."""
        node = {"name": "foo"}
        annotate_fact_source(node, source="user", command="update-node",
                             timestamp="2026-08-04T12:00:00")
        self.assertEqual(node["_fact_sources"]["update-node"]["timestamp"],
                         "2026-08-04T12:00:00")


class TestNewTxId(unittest.TestCase):
    """Test the transaction id generator."""

    def test_tx_id_format(self):
        """new_tx_id returns a string starting with tx_."""
        tx = new_tx_id()
        self.assertTrue(tx.startswith("tx_"))
        self.assertGreaterEqual(len(tx), 10)

    def test_tx_id_uniqueness(self):
        """Each call to new_tx_id returns a different value."""
        ids = {new_tx_id() for _ in range(20)}
        self.assertEqual(len(ids), 20)


if __name__ == "__main__":
    unittest.main()
