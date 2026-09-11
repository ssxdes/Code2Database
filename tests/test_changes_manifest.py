#!/usr/bin/env python3
"""Tests for _scanner.changes — manifest fingerprinting and change
detection with the --exclude-dirs skip conventions.

The manifest walk must mirror the scan walk: built-in generated/VCS/
dependency dirs are skipped, '!name' entries re-include a built-in skip
directory for projects that keep real source there (e.g., lib/build/),
and the effective scope is recorded in the manifest so later change
detection replays the same walk.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from _scanner.changes import (effective_skip_dirs, save_manifest,
                              detect_changes, _SKIP_DIRS)


def _touch(path, content="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _read_manifest(outdir):
    with open(os.path.join(outdir, ".code2database_manifest.json")) as f:
        return json.load(f)


class TestEffectiveSkipDirs(unittest.TestCase):

    def test_builtin_dirs_skipped(self):
        for d in ("build", "dist", "third_party", "vendor", "external",
                  "node_modules", "__pycache__", ".git"):
            self.assertIn(d, effective_skip_dirs(None))

    def test_additions_extend_builtins(self):
        skip = effective_skip_dirs(["generated"])
        self.assertIn("generated", skip)
        self.assertIn("build", skip)

    def test_reinclude_removes_builtin(self):
        skip = effective_skip_dirs(["!build"])
        self.assertNotIn("build", skip)
        self.assertIn("dist", skip)

    def test_reinclude_and_add_together(self):
        skip = effective_skip_dirs(["!build", "generated"])
        self.assertNotIn("build", skip)
        self.assertIn("generated", skip)

    def test_empty_entries_ignored(self):
        self.assertEqual(effective_skip_dirs(["", None]), _SKIP_DIRS)


class TestManifestExcludeScope(unittest.TestCase):

    def _setup(self):
        tmp = tempfile.mkdtemp(prefix="c2d_changes_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        src = os.path.join(tmp, "src")
        out = os.path.join(tmp, "out")
        os.makedirs(out)
        _touch(os.path.join(src, "main.c"))
        _touch(os.path.join(src, "lib", "build", "impl.c"))
        _touch(os.path.join(src, "third_party", "vend.c"))
        return src, out

    def _rels(self, files):
        return {p.replace(os.sep, "/") for p in files}

    def test_build_source_reincluded(self):
        src, out = self._setup()
        save_manifest(src, out, exclude_dirs=["!build"])
        rels = self._rels(_read_manifest(out)["files"])
        self.assertIn("lib/build/impl.c", rels)

    def test_build_source_skipped_by_default(self):
        src, out = self._setup()
        save_manifest(src, out)
        rels = self._rels(_read_manifest(out)["files"])
        self.assertNotIn("lib/build/impl.c", rels)
        self.assertIn("main.c", rels)

    def test_vendor_not_tracked(self):
        src, out = self._setup()
        save_manifest(src, out)
        rels = self._rels(_read_manifest(out)["files"])
        self.assertNotIn("third_party/vend.c", rels)

    def test_manifest_records_exclude_list(self):
        src, out = self._setup()
        save_manifest(src, out, exclude_dirs=["!build"])
        self.assertEqual(_read_manifest(out).get("exclude_dirs"), ["!build"])

    def test_detect_changes_replays_stored_scope(self):
        src, out = self._setup()
        save_manifest(src, out, exclude_dirs=["!build"])
        _touch(os.path.join(src, "lib", "build", "impl.c"), "changed-body")
        changes = detect_changes(src, out)
        changed = self._rels(os.path.relpath(p, src)
                             for p in changes["changed_files"])
        self.assertIn("lib/build/impl.c", changed)

    def test_detect_changes_caller_scope_wins(self):
        src, out = self._setup()
        save_manifest(src, out, exclude_dirs=["!build"])
        # caller passes an explicit scope: build/ is skipped again, so the
        # file under lib/build/ leaves the walk and counts as deleted
        changes = detect_changes(src, out, exclude_dirs=[])
        deleted = self._rels(os.path.relpath(p, src)
                             for p in changes["deleted_files"])
        self.assertIn("lib/build/impl.c", deleted)

    def test_detect_changes_without_manifest_requests_full_scan(self):
        src, out = self._setup()
        changes = detect_changes(src, out)
        self.assertTrue(changes["needs_full_scan"])

    def test_legacy_manifest_without_exclude_key_still_works(self):
        src, out = self._setup()
        with open(os.path.join(out, ".code2database_manifest.json"), "w") as f:
            json.dump({"source_root": src, "files": {"main.c": "1:1"}}, f)
        changes = detect_changes(src, out)
        self.assertFalse(changes["needs_full_scan"])
        # main.c was fingerprinted differently in the legacy manifest
        changed = self._rels(os.path.relpath(p, src)
                             for p in changes["changed_files"])
        self.assertIn("main.c", changed)


class TestScanDirectoryReinclude(unittest.TestCase):

    def test_reinclude_build_scans_source_there(self):
        from code2database_scanner import scan_directory
        with tempfile.TemporaryDirectory() as tmpdir:
            _touch(os.path.join(tmpdir, "lib", "build", "impl.c"),
                   "int build_side_helper(void) { return 1; }\n")
            result = scan_directory(tmpdir, lang="c", exclude_dirs=["!build"])
            names = {f["name"] for f in result.get("functions", [])}
            self.assertIn("build_side_helper", names)

    def test_build_skipped_by_default(self):
        from code2database_scanner import scan_directory
        with tempfile.TemporaryDirectory() as tmpdir:
            _touch(os.path.join(tmpdir, "lib", "build", "impl.c"),
                   "int build_side_helper(void) { return 1; }\n")
            result = scan_directory(tmpdir, lang="c")
            names = {f["name"] for f in result.get("functions", [])}
            self.assertNotIn("build_side_helper", names)


class TestBuildUpdateWalkScope(unittest.TestCase):

    def _make_db(self, graph_dir, rows):
        os.makedirs(graph_dir, exist_ok=True)
        db = os.path.join(graph_dir, "code2database.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE cgdb_files (id INTEGER PRIMARY KEY, "
                     "path TEXT, content_hash TEXT)")
        conn.executemany(
            "INSERT INTO cgdb_files (path, content_hash) VALUES (?, ?)", rows)
        conn.commit()
        conn.close()
        return db

    def test_reincluded_files_compared_not_ignored(self):
        from _builder.build.build_update import detect_db_changes
        import hashlib

        def _hash(path):
            with open(path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()

        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "src")
            graph = os.path.join(tmpdir, "graph")
            fpath = os.path.join(src, "lib", "build", "impl.c")
            _touch(fpath, "int impl(void) { return 1; }\n")
            db = self._make_db(graph, [("lib/build/impl.c", _hash(fpath))])
            # manifest declares the re-include scope the scan used
            with open(os.path.join(graph, ".code2database_manifest.json"),
                      "w") as f:
                json.dump({"source_root": src, "files": {},
                           "exclude_dirs": ["!build"]}, f)
            result = detect_db_changes(src, db)
            self.assertEqual(result["changed"], [])
            self.assertEqual(result["added"], [])
            self.assertEqual(result["deleted"], [])

            # content change under the re-included dir must be detected
            _touch(fpath, "int impl(void) { return 2; }\n")
            result = detect_db_changes(src, db)
            self.assertIn(os.path.abspath(fpath), result["changed"])

    def test_no_manifest_defaults_to_builtins(self):
        from _builder.build.build_update import detect_db_changes
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "src")
            graph = os.path.join(tmpdir, "graph")
            _touch(os.path.join(src, "third_party", "vend.c"), "int v(void);\n")
            db = self._make_db(graph, [])
            result = detect_db_changes(src, db)
            self.assertEqual(result["added"], [])


class TestMergeManifestPreservesScope(unittest.TestCase):

    def test_local_scope_wins(self):
        from _builder.build.update_sync import _merge_manifest
        with tempfile.TemporaryDirectory() as tmpdir:
            local = os.path.join(tmpdir, "local.json")
            git = os.path.join(tmpdir, "git.json")
            out = os.path.join(tmpdir, "out.json")
            with open(local, "w") as f:
                json.dump({"source_root": "/src", "files": {"a.c": "1:1"},
                           "exclude_dirs": ["!build"]}, f)
            with open(git, "w") as f:
                json.dump({"source_root": "/src", "files": {"b.c": "2:2"},
                           "exclude_dirs": []}, f)
            _merge_manifest(local, git, out, source_root="/src")
            with open(out) as f:
                merged = json.load(f)
            self.assertEqual(merged.get("exclude_dirs"), ["!build"])
            self.assertEqual(sorted(merged["files"]), ["a.c", "b.c"])

    def test_git_scope_used_when_local_absent(self):
        from _builder.build.update_sync import _merge_manifest
        with tempfile.TemporaryDirectory() as tmpdir:
            local = os.path.join(tmpdir, "local.json")
            git = os.path.join(tmpdir, "git.json")
            out = os.path.join(tmpdir, "out.json")
            with open(local, "w") as f:
                json.dump({"source_root": "/src", "files": {}}, f)
            with open(git, "w") as f:
                json.dump({"source_root": "/src", "files": {"b.c": "2:2"},
                           "exclude_dirs": ["!build"]}, f)
            _merge_manifest(local, git, out, source_root="/src")
            with open(out) as f:
                merged = json.load(f)
            self.assertEqual(merged.get("exclude_dirs"), ["!build"])


if __name__ == "__main__":
    unittest.main()
