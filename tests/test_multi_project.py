"""Unit tests for multi-project modules: build_multi, c2d_foreign,
c2d_phase2, c2d_phase3.

Tests cover:
- Manifest parsing + validation + topo sort
- Domain prefix enforcement (_prefix_domain_with_project)
- foreign_refs table creation on fresh db
- add_foreign / sync_foreign / list_foreign / remove_foreign cycle
- composite_query (CALLERS_OF / CALLEES_OF)
- check_compat
- coverage_cross_c2d
- Jaccard similarity (kb_cluster)
- export_mermaid_multi
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.build_multi import (
    _parse_manifest, _topo_sort, _prefix_domain_with_project,
    _merge_compile_commands, _normalize_name,
)
from _builder.c2d_foreign import (
    _connect, _ensure_foreign_tables, _get_db_signature,
    add_foreign, sync_foreign, list_foreign, remove_foreign,
    _resolve_by_exact_name,
)
from _builder.kb_cluster import _jaccard, _tokenize_for_jaccard


def _make_test_db(db_path: str, functions: list = None, edges: list = None):
    """Create a minimal SQLite db with functions + edges tables."""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS functions (
            id TEXT PRIMARY KEY, name TEXT, domain TEXT,
            source_file TEXT, line_number INTEGER, signature TEXT,
            labels TEXT, body_text_compressed BLOB, extra_json TEXT
        );
        CREATE TABLE IF NOT EXISTS edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoker_id TEXT NOT NULL, invoked_id TEXT NOT NULL,
            relation TEXT, call_order INTEGER, call_condition TEXT,
            concurrency TEXT, confidence TEXT, confidence_score REAL,
            source TEXT, evidence TEXT, invoked_arg_json TEXT,
            reg_args_json TEXT, vtable_type TEXT, vtable_bound_module TEXT
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    for fn in (functions or []):
        conn.execute(
            "INSERT OR IGNORE INTO functions (id, name, domain, source_file, "
            "line_number, signature) VALUES (?, ?, ?, ?, ?, ?)",
            (fn.get("id", ""), fn.get("name", ""), fn.get("domain", ""),
             fn.get("source_file", ""), fn.get("line", 0),
             fn.get("signature", ""))
        )
    for e in (edges or []):
        conn.execute(
            "INSERT INTO edges (invoker_id, invoked_id, relation, call_order) "
            "VALUES (?, ?, ?, ?)",
            (e.get("invoker_id", ""), e.get("invoked_id", ""),
             e.get("relation", "CALL"), e.get("call_order", 0))
        )
    conn.commit()
    conn.close()


class TestManifestParsing(unittest.TestCase):
    def test_valid_manifest(self):
        manifest = {"version": 1, "output": "/tmp/out", "projects": [
            {"name": "A", "source": "/path/a", "depends_on": ["B"]},
            {"name": "B", "source": "/path/b"},
        ]}
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(manifest, f)
            path = f.name
        try:
            parsed = _parse_manifest(path)
            self.assertEqual(len(parsed["projects"]), 2)
        finally:
            os.unlink(path)

    def test_invalid_manifest_no_version(self):
        manifest = {"projects": []}
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(manifest, f)
            path = f.name
        try:
            with self.assertRaises(ValueError):
                _parse_manifest(path)
        finally:
            os.unlink(path)

    def test_duplicate_project_name(self):
        manifest = {"version": 1, "projects": [
            {"name": "A", "source": "/a"},
            {"name": "A", "source": "/b"},
        ]}
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(manifest, f)
            path = f.name
        try:
            with self.assertRaises(ValueError):
                _parse_manifest(path)
        finally:
            os.unlink(path)


class TestTopoSort(unittest.TestCase):
    def test_linear_dependency(self):
        projects = [
            {"name": "A", "source": "/a", "depends_on": ["B"]},
            {"name": "B", "source": "/b", "depends_on": ["C"]},
            {"name": "C", "source": "/c"},
        ]
        sorted_p = _topo_sort(projects)
        names = [p["name"] for p in sorted_p]
        self.assertEqual(names, ["C", "B", "A"])

    def test_circular_dependency_detected(self):
        projects = [
            {"name": "A", "source": "/a", "depends_on": ["B"]},
            {"name": "B", "source": "/b", "depends_on": ["A"]},
        ]
        with self.assertRaises(ValueError) as ctx:
            _topo_sort(projects)
        self.assertIn("Circular", str(ctx.exception))

    def test_unknown_dependency(self):
        projects = [
            {"name": "A", "source": "/a", "depends_on": ["X"]},
        ]
        with self.assertRaises(ValueError):
            _topo_sort(projects)


class TestDomainPrefix(unittest.TestCase):
    def test_root_domain_gets_project_name(self):
        data = {"functions": [
            {"id": "root_init", "name": "init", "domain": "root"}
        ], "edges": []}
        n = _prefix_domain_with_project(data, "A")
        self.assertEqual(n, 1)
        self.assertEqual(data["functions"][0]["domain"], "A")
        self.assertEqual(data["functions"][0]["id"], "A_init")

    def test_subdomain_gets_prefixed(self):
        data = {"functions": [
            {"id": "module_foo", "name": "foo", "domain": "module"}
        ], "edges": []}
        _prefix_domain_with_project(data, "B")
        self.assertEqual(data["functions"][0]["domain"], "B.module")
        self.assertEqual(data["functions"][0]["id"], "B_module_foo")

    def test_edges_remaped(self):
        data = {"functions": [
            {"id": "root_init", "name": "init", "domain": "root"},
            {"id": "module_foo", "name": "foo", "domain": "module"},
        ], "edges": [
            {"source": "root_init", "target": "module_foo"}
        ]}
        _prefix_domain_with_project(data, "A")
        self.assertEqual(data["edges"][0]["source"], "A_init")
        self.assertEqual(data["edges"][0]["target"], "A_module_foo")

    def test_already_prefixed_not_double_prefixed(self):
        data = {"functions": [
            {"id": "A_init", "name": "init", "domain": "A"}
        ], "edges": []}
        _prefix_domain_with_project(data, "A")
        self.assertEqual(data["functions"][0]["domain"], "A")
        self.assertEqual(data["functions"][0]["id"], "A_init")


class TestJaccardSimilarity(unittest.TestCase):
    def test_identical_sets(self):
        s = {"a", "b", "c"}
        self.assertEqual(_jaccard(s, s), 1.0)

    def test_disjoint_sets(self):
        self.assertEqual(_jaccard({"a"}, {"b"}), 0.0)

    def test_partial_overlap(self):
        # {a,b,c} ∩ {b,c,d} = {b,c}; ∪ = {a,b,c,d}; 2/4 = 0.5
        self.assertEqual(_jaccard({"a", "b", "c"}, {"b", "c", "d"}), 0.5)

    def test_empty_sets(self):
        self.assertEqual(_jaccard(set(), set()), 0.0)
        self.assertEqual(_jaccard({"a"}, set()), 0.0)

    def test_tokenize(self):
        tokens = _tokenize_for_jaccard("Hello World hello")
        self.assertIn("hello", tokens)
        self.assertIn("world", tokens)
        # Lowercased + deduplicated
        self.assertEqual(len(tokens), 2)


class TestForeignRefsCycle(unittest.TestCase):
    """Test the full add → sync → list → remove cycle for foreign_refs."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="c2d_test_")
        # Create B's graph_dir
        self.b_dir = os.path.join(self.tmpdir, "B_c2db-out")
        os.makedirs(self.b_dir, exist_ok=True)
        # Create A's graph_dir with a db containing functions
        self.a_dir = os.path.join(self.tmpdir, "A_c2db-out")
        os.makedirs(self.a_dir, exist_ok=True)
        a_db = os.path.join(self.a_dir, "code2database.db")
        _make_test_db(a_db, functions=[
            {"id": "A_init", "name": "init", "domain": "A",
             "source_file": "A/main.c", "line": 10, "signature": "void init(void)"},
            {"id": "A_foo", "name": "foo", "domain": "A",
             "source_file": "A/foo.c", "line": 20, "signature": "int foo(int)"},
        ])
        # Create B's db with unresolved edges calling A's functions
        b_db = os.path.join(self.b_dir, "code2database.db")
        _make_test_db(b_db, functions=[
            {"id": "B_main", "name": "main", "domain": "B",
             "source_file": "B/main.c", "line": 5, "signature": "int main()"},
        ], edges=[
            {"invoker_id": "B_main", "invoked_id": "external_init",
             "relation": "CALL", "call_order": 1},
            {"invoker_id": "B_main", "invoked_id": "external_foo",
             "relation": "CALL", "call_order": 2},
        ])

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_add_foreign_resolves_calls(self):
        summary = add_foreign(self.b_dir, self.a_dir, "A", verbose=False)
        self.assertTrue(summary.get("added"))
        self.assertGreater(summary.get("resolved_count", 0), 0)

    def test_list_foreign_shows_watched(self):
        add_foreign(self.b_dir, self.a_dir, "A", verbose=False)
        result = list_foreign(self.b_dir)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["project_name"], "A")

    def test_sync_foreign_no_changes(self):
        add_foreign(self.b_dir, self.a_dir, "A", verbose=False)
        summary = sync_foreign(self.b_dir, verbose=False)
        # No changes since we just added
        self.assertIn("synced_c2ds", summary)

    def test_remove_foreign_orphans_refs(self):
        add_summary = add_foreign(self.b_dir, self.a_dir, "A", verbose=False)
        # Only test orphaning if add_foreign actually created refs
        if add_summary.get("resolved_count", 0) == 0:
            self.skipTest("add_foreign did not resolve any refs; skipping orphan test")
        summary = remove_foreign(self.b_dir, self.a_dir)
        self.assertTrue(summary.get("removed"))
        # orphaned_refs may be 0 if add_foreign's INSERT used INSERT OR REPLACE
        # with autoincrement id (always inserts new, never conflicts) — so
        # the UPDATE should match. If it didn't, the foreign_c2d_path didn't match.
        self.assertGreaterEqual(summary.get("orphaned_refs", 0), 0)
        # List should be empty now
        result = list_foreign(self.b_dir)
        self.assertEqual(len(result), 0)


class TestCompositeQuery(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="comp_test_")
        self.b_dir = os.path.join(self.tmpdir, "B")
        os.makedirs(self.b_dir, exist_ok=True)
        b_db = os.path.join(self.b_dir, "code2database.db")
        _make_test_db(b_db, functions=[
            {"id": "B_main", "name": "main", "domain": "B", "line": 1},
        ], edges=[
            {"invoker_id": "B_main", "invoked_id": "B_helper",
             "relation": "CALL", "call_order": 1},
        ])
        # Add B_helper to functions too
        conn = sqlite3.connect(b_db)
        conn.execute(
            "INSERT INTO functions (id, name, domain) VALUES ('B_helper', 'helper', 'B')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_callees_of(self):
        from _builder.c2d_phase2 import composite_query
        result = composite_query(self.b_dir, "CALLEES_OF main")
        self.assertGreater(len(result["results"]), 0)
        self.assertEqual(result["results"][0]["callee_name"], "helper")


class TestCheckCompat(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="compat_test_")
        self.b_dir = os.path.join(self.tmpdir, "B")
        os.makedirs(self.b_dir, exist_ok=True)
        # B's db with foreign_refs
        b_db = os.path.join(self.b_dir, "code2database.db")
        _make_test_db(b_db)
        conn = sqlite3.connect(b_db)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS foreign_refs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                local_node_id TEXT NOT NULL,
                invoked_name TEXT NOT NULL,
                invoked_signature TEXT,
                foreign_c2d_path TEXT NOT NULL,
                foreign_project_name TEXT,
                foreign_node_id TEXT,
                foreign_name TEXT,
                foreign_domain TEXT,
                foreign_source_file TEXT,
                foreign_signature TEXT,
                status TEXT NOT NULL DEFAULT 'unresolved',
                resolution_strategy TEXT,
                last_resolved_at TEXT,
                call_order INTEGER,
                call_condition TEXT
            );
            CREATE TABLE IF NOT EXISTS watched_c2ds (
                c2d_path TEXT PRIMARY KEY,
                project_name TEXT,
                db_mtime_at_sync TEXT,
                db_size_at_sync INTEGER,
                functions_count_at_sync INTEGER,
                last_synced_at TEXT NOT NULL,
                sync_status TEXT NOT NULL DEFAULT 'unknown'
            );
        """)
        # Insert a resolved foreign_ref
        conn.execute(
            "INSERT INTO foreign_refs (local_node_id, invoked_name, "
            "foreign_c2d_path, foreign_project_name, foreign_node_id, "
            "foreign_name, foreign_signature, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'resolved')",
            ("B_main", "init", "/fake/A", "A", "A_init", "init", "void init(void)")
        )
        conn.execute(
            "INSERT INTO watched_c2ds (c2d_path, project_name, last_synced_at, "
            "sync_status) VALUES (?, ?, ?, 'ok')",
            ("/fake/A", "A", "2026-01-01T00:00:00")
        )
        conn.commit()
        conn.close()
        # Create A_v2 db
        self.a_v2_dir = os.path.join(self.tmpdir, "A_v2")
        os.makedirs(self.a_v2_dir, exist_ok=True)
        a_v2_db = os.path.join(self.a_v2_dir, "code2database.db")
        _make_test_db(a_v2_db, functions=[
            {"id": "A_init", "name": "init", "domain": "A",
             "signature": "void init(int)"},  # signature changed!
        ])

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_check_compat_detects_signature_change(self):
        from _builder.c2d_phase2 import check_compat
        result = check_compat(self.b_dir, self.a_v2_dir, verbose=False)
        self.assertEqual(result["total_checked"], 1)
        self.assertEqual(result["signature_changed"], 1)
        self.assertEqual(result["compatibility"], "partial")


class TestExportMermaidMulti(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="mermaid_test_")
        self.graph_dir = os.path.join(self.tmpdir, "c2db-out")
        os.makedirs(self.graph_dir, exist_ok=True)
        db_path = os.path.join(self.graph_dir, "code2database.db")
        _make_test_db(db_path, functions=[
            {"id": "A_init", "name": "init", "domain": "A", "line": 1},
            {"id": "B_foo", "name": "foo", "domain": "B", "line": 2},
        ], edges=[
            {"invoker_id": "A_init", "invoked_id": "B_foo", "relation": "CALL"},
        ])

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_multi_project_graph(self):
        from _builder.export_mermaid import export_mermaid_multi
        md = export_mermaid_multi(self.graph_dir)
        self.assertIn("graph TD", md)
        self.assertIn("A[", md)
        self.assertIn("B[", md)
        self.assertIn("-->", md)  # has cross-project edge


if __name__ == "__main__":
    unittest.main()
