"""Tests for cgdb_incremental.py — content hash + #include dep graph.

Verifies the IncrementalSync class:
  - detect_changes: identifies new, changed, and deleted files
  - compute_affected_tus: transitive closure when a header is changed
  - mark_clean: updates stored content_hash after rebuild
  - parse_includes: extracts #include directives
  - compute_content_hash: SHA-256 of file content
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.cgdb_incremental import (
    IncrementalSync, compute_content_hash, parse_includes,
    compute_affected_tus,
)


class TestComputeContentHash(unittest.TestCase):
    """Test the SHA-256 content hash function."""

    def test_known_value(self):
        """compute_content_hash matches a known SHA-256."""
        import hashlib
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c',
                                          delete=False) as f:
            f.write("int main(void) { return 0; }\n")
            path = f.name
        self.addCleanup(os.unlink, path)
        expected = hashlib.sha256(
            b"int main(void) { return 0; }\n"
        ).hexdigest()
        self.assertEqual(compute_content_hash(path), expected)

    def test_empty_file(self):
        """Empty file hashes to SHA-256 of empty string."""
        import hashlib
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c',
                                          delete=False) as f:
            path = f.name
        self.addCleanup(os.unlink, path)
        self.assertEqual(compute_content_hash(path),
                         hashlib.sha256(b"").hexdigest())

    def test_missing_file_returns_empty(self):
        """Missing file returns empty string (no exception)."""
        self.assertEqual(compute_content_hash("/nonexistent/file.c"), "")


class TestParseIncludes(unittest.TestCase):
    """Test the #include parser."""

    def test_system_include(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c',
                                          delete=False) as f:
            f.write('#include <stdio.h>\nint main(void) { return 0; }\n')
            path = f.name
        self.addCleanup(os.unlink, path)
        includes = parse_includes(path)
        self.assertEqual(includes, ['stdio.h'])

    def test_local_include(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c',
                                          delete=False) as f:
            f.write('#include "myheader.h"\n')
            path = f.name
        self.addCleanup(os.unlink, path)
        includes = parse_includes(path)
        self.assertEqual(includes, ['myheader.h'])

    def test_multiple_includes(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c',
                                          delete=False) as f:
            f.write('''\
#include <stdio.h>
#include <stdlib.h>
#include "myheader.h"
#include <linux/kernel.h>
''')
            path = f.name
        self.addCleanup(os.unlink, path)
        includes = parse_includes(path)
        self.assertEqual(len(includes), 4)
        self.assertIn('stdio.h', includes)
        self.assertIn('stdlib.h', includes)
        self.assertIn('myheader.h', includes)
        self.assertIn('linux/kernel.h', includes)

    def test_no_includes(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c',
                                          delete=False) as f:
            f.write('int main(void) { return 0; }\n')
            path = f.name
        self.addCleanup(os.unlink, path)
        self.assertEqual(parse_includes(path), [])

    def test_indented_include(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c',
                                          delete=False) as f:
            f.write('   #  include <stdio.h>\n')
            path = f.name
        self.addCleanup(os.unlink, path)
        includes = parse_includes(path)
        self.assertEqual(includes, ['stdio.h'])


class TestIncrementalSyncDetectChanges(unittest.TestCase):
    """Test detect_changes via IncrementalSync."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.db_path = os.path.join(self.tmpdir, "test.db")

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _setup_db_with_files(self, file_hash_map):
        """Create a cgdb_files table populated with (path, content_hash)."""
        import sqlite3
        from _builder.cgdb_schema import apply_cgdb_schema
        conn = sqlite3.connect(self.db_path)
        try:
            apply_cgdb_schema(conn)
            for path, h in file_hash_map.items():
                conn.execute(
                    "INSERT INTO cgdb_files (path, content_hash, sha256, language) "
                    "VALUES (?, ?, ?, ?)",
                    (path, h, h, 'c')
                )
            conn.commit()
        finally:
            conn.close()

    def test_detects_new_file(self):
        """A file present on disk but not in DB is detected as new."""
        # Write a source file on disk
        src_path = os.path.join(self.tmpdir, "new.c")
        with open(src_path, 'w') as f:
            f.write("int main(void) { return 0; }\n")
        # DB has no files
        self._setup_db_with_files({})
        sync = IncrementalSync(self.tmpdir)
        changed = sync.detect_changes(self.db_path)
        self.assertIn(src_path, changed)

    def test_detects_changed_file(self):
        """A file whose content hash differs from stored is detected as changed."""
        src_path = os.path.join(self.tmpdir, "changed.c")
        with open(src_path, 'w') as f:
            f.write("int main(void) { return 1; }\n")
        # DB has a stale hash
        self._setup_db_with_files({src_path: "stale_hash_value"})
        sync = IncrementalSync(self.tmpdir)
        changed = sync.detect_changes(self.db_path)
        self.assertIn(src_path, changed)

    def test_unchanged_file_not_detected(self):
        """A file whose content hash matches stored is not detected."""
        src_path = os.path.join(self.tmpdir, "unchanged.c")
        with open(src_path, 'w') as f:
            f.write("int main(void) { return 0; }\n")
        current_hash = compute_content_hash(src_path)
        self._setup_db_with_files({src_path: current_hash})
        sync = IncrementalSync(self.tmpdir)
        changed = sync.detect_changes(self.db_path)
        self.assertNotIn(src_path, changed)

    def test_detects_deleted_file(self):
        """A file in DB but not on disk is detected as deleted."""
        deleted_path = os.path.join(self.tmpdir, "deleted.c")
        # Don't create the file on disk — only in DB
        self._setup_db_with_files({deleted_path: "some_hash"})
        sync = IncrementalSync(self.tmpdir)
        changed = sync.detect_changes(self.db_path)
        self.assertIn(deleted_path, changed)

    def test_no_db_returns_all_files(self):
        """If DB doesn't exist, all source files are new."""
        src_path = os.path.join(self.tmpdir, "fresh.c")
        with open(src_path, 'w') as f:
            f.write("int main(void) { return 0; }\n")
        # Don't create DB
        sync = IncrementalSync(self.tmpdir)
        changed = sync.detect_changes(self.db_path)
        self.assertIn(src_path, changed)


class TestIncrementalSyncComputeAffectedTus(unittest.TestCase):
    """Test compute_affected_tus via IncrementalSync."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_changed_header_affects_including_tu(self):
        """A changed header should mark all TUs that #include it as affected."""
        # Create a header
        header_path = os.path.join(self.tmpdir, "myheader.h")
        with open(header_path, 'w') as f:
            f.write("#ifndef MYHEADER_H\n#define MYHEADER_H\nint foo(void);\n#endif\n")
        # Create a TU that includes it
        tu_path = os.path.join(self.tmpdir, "main.c")
        with open(tu_path, 'w') as f:
            f.write('#include "myheader.h"\nint main(void) { return foo(); }\n')
        sync = IncrementalSync(self.tmpdir)
        affected = sync.compute_affected_tus([header_path])
        self.assertIn(tu_path, affected,
                      "TU that includes the changed header should be affected")
        # Note: headers themselves are NOT in the affected set — only TUs are
        # returned (compute_affected_tus returns the set of TUs to rebuild).

    def test_changed_transitive_header_affects_tu(self):
        """A changed header included by another header, which is included by a
        TU, should mark the TU as affected (transitive closure)."""
        # Create a deep header (no includes)
        deep_header = os.path.join(self.tmpdir, "deep.h")
        with open(deep_header, 'w') as f:
            f.write("#ifndef DEEP_H\n#define DEEP_H\nint deep(void);\n#endif\n")
        # Create a shallow header that includes the deep one
        shallow_header = os.path.join(self.tmpdir, "shallow.h")
        with open(shallow_header, 'w') as f:
            f.write('#include "deep.h"\nint shallow(void);\n')
        # Create a TU that includes the shallow header
        tu_path = os.path.join(self.tmpdir, "main.c")
        with open(tu_path, 'w') as f:
            f.write('#include "shallow.h"\nint main(void) { return shallow(); }\n')
        sync = IncrementalSync(self.tmpdir)
        affected = sync.compute_affected_tus([deep_header])
        self.assertIn(tu_path, affected,
                      "TU transitively including the changed header should be affected")

    def test_changed_tu_only_affects_itself(self):
        """A changed .c file should only mark itself as affected (TUs aren't
        included by other TUs)."""
        tu_path = os.path.join(self.tmpdir, "main.c")
        with open(tu_path, 'w') as f:
            f.write("int main(void) { return 0; }\n")
        other_tu = os.path.join(self.tmpdir, "other.c")
        with open(other_tu, 'w') as f:
            f.write("int other(void) { return 0; }\n")
        sync = IncrementalSync(self.tmpdir)
        affected = sync.compute_affected_tus([tu_path])
        self.assertIn(tu_path, affected)
        self.assertNotIn(other_tu, affected,
                         "Other TUs should not be affected by a TU change")

    def test_unchanged_files_not_affected(self):
        """Files not in the include closure of changed files are not affected."""
        # Two unrelated TUs
        tu1 = os.path.join(self.tmpdir, "tu1.c")
        with open(tu1, 'w') as f:
            f.write("int tu1(void) { return 0; }\n")
        tu2 = os.path.join(self.tmpdir, "tu2.c")
        with open(tu2, 'w') as f:
            f.write("int tu2(void) { return 0; }\n")
        sync = IncrementalSync(self.tmpdir)
        affected = sync.compute_affected_tus([tu1])
        self.assertIn(tu1, affected)
        self.assertNotIn(tu2, affected)


class TestIncrementalSyncMarkClean(unittest.TestCase):
    """Test mark_clean via IncrementalSync."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.db_path = os.path.join(self.tmpdir, "test.db")

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _setup_db_with_files(self, file_hash_map):
        import sqlite3
        from _builder.cgdb_schema import apply_cgdb_schema
        conn = sqlite3.connect(self.db_path)
        try:
            apply_cgdb_schema(conn)
            for path, h in file_hash_map.items():
                conn.execute(
                    "INSERT INTO cgdb_files (path, content_hash, sha256, language) "
                    "VALUES (?, ?, ?, ?)",
                    (path, h, h, 'c')
                )
            conn.commit()
        finally:
            conn.close()

    def test_mark_clean_updates_hash(self):
        """mark_clean updates the stored content_hash for synced files."""
        src_path = os.path.join(self.tmpdir, "synced.c")
        with open(src_path, 'w') as f:
            f.write("int main(void) { return 0; }\n")
        new_hash = compute_content_hash(src_path)
        # DB has stale hash
        self._setup_db_with_files({src_path: "stale_hash"})
        sync = IncrementalSync(self.tmpdir)
        rows = sync.mark_clean([src_path], self.db_path)
        self.assertEqual(rows, 1)
        # Verify DB was updated
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT content_hash FROM cgdb_files WHERE path = ?",
                (src_path,)
            ).fetchone()
            self.assertEqual(row[0], new_hash)
        finally:
            conn.close()

    def test_mark_clean_deletes_missing_files(self):
        """mark_clean on a non-existent file removes its row from cgdb_files."""
        missing_path = os.path.join(self.tmpdir, "missing.c")
        # Don't create on disk, but add to DB
        self._setup_db_with_files({missing_path: "some_hash"})
        sync = IncrementalSync(self.tmpdir)
        rows = sync.mark_clean([missing_path], self.db_path)
        self.assertEqual(rows, 1)
        # Verify row was deleted
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM cgdb_files WHERE path = ?",
                (missing_path,)
            ).fetchone()
            self.assertEqual(row[0], 0)
        finally:
            conn.close()


class TestComputeAffectedTusWrapper(unittest.TestCase):
    """Test the convenience wrapper compute_affected_tus(changed, root)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_wrapper_returns_same_as_method(self):
        """The convenience wrapper matches the instance method."""
        header = os.path.join(self.tmpdir, "hdr.h")
        with open(header, 'w') as f:
            f.write("int foo(void);\n")
        tu = os.path.join(self.tmpdir, "main.c")
        with open(tu, 'w') as f:
            f.write('#include "hdr.h"\nint main(void) { return foo(); }\n')
        affected1 = compute_affected_tus([header], self.tmpdir)
        sync = IncrementalSync(self.tmpdir)
        affected2 = sync.compute_affected_tus([header])
        self.assertEqual(set(affected1), set(affected2))


if __name__ == "__main__":
    unittest.main()
