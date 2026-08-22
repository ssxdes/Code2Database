"""Smoke tests for scan_directory(scan_subsystems=...) subsystem filtering.

Verifies that scan_directory's scan_subsystems parameter restricts the scan
to the specified top-level directories. Uses a temp source tree with
multiple subsystem directories.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from code2database_scanner import scan_directory


_SAMPLE_C = b"""
int alpha(void) { return 1; }
int beta(void) { return 2; }
"""


def _make_subdir_tree(root):
    """Create a source tree with mm/ kernel/ net/ subsystem dirs."""
    for sub in ('mm', 'kernel', 'net'):
        d = os.path.join(root, sub)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'a.c'), 'wb') as f:
            f.write(_SAMPLE_C)
    # Also a top-level file (no subsystem)
    with open(os.path.join(root, 'top.c'), 'wb') as f:
        f.write(_SAMPLE_C)


class TestScanDirectoryImport(unittest.TestCase):
    """Verify the scanner module imports cleanly."""

    def test_module_imports_cleanly(self):
        import code2database_scanner as cs
        self.assertTrue(hasattr(cs, 'scan_directory'))
        self.assertTrue(callable(cs.scan_directory))


class TestScanDirectoryNoFilter(unittest.TestCase):
    """Without scan_subsystems, all source files are scanned."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        _make_subdir_tree(self.tmpdir)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_scan_returns_dict(self):
        result = scan_directory(self.tmpdir, lang='c', workers=1, quiet=True)
        self.assertIsInstance(result, dict)

    def test_scan_picks_up_all_files(self):
        result = scan_directory(self.tmpdir, lang='c', workers=1, quiet=True)
        # functions list should include alpha and beta
        funcs = result.get('functions', [])
        names = {f.get('name') for f in funcs}
        self.assertIn('alpha', names)
        self.assertIn('beta', names)


class TestScanDirectorySubsystemFilter(unittest.TestCase):
    """scan_subsystems restricts scan to the listed top-level dirs."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        _make_subdir_tree(self.tmpdir)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_scan_subsystems_mm_only(self):
        result = scan_directory(self.tmpdir, lang='c', workers=1,
                                quiet=True, scan_subsystems=['mm'])
        # Every scanned file path should be under mm/ (relative paths
        # like 'mm/a.c' returned by scan_directory)
        scanned_paths = []
        for fn in result.get('functions', []):
            sf = fn.get('source_file', '').replace('\\', '/')
            scanned_paths.append(sf)
        # mm/a.c should be present (one of the scanned files)
        self.assertTrue(any(p.startswith('mm/') or p.endswith('mm/a.c')
                            for p in scanned_paths),
                        f'expected mm/ file in: {scanned_paths}')
        # top-level top.c must NOT be present
        self.assertFalse(any('top.c' in p for p in scanned_paths),
                         f'top.c leaked through subsystem filter: {scanned_paths}')

    def test_scan_subsystems_multiple(self):
        result = scan_directory(self.tmpdir, lang='c', workers=1,
                                quiet=True, scan_subsystems=['mm', 'net'])
        # Should have scanned mm/ and net/ but not kernel/ or top.c
        scanned_paths = []
        for fn in result.get('functions', []):
            sf = fn.get('source_file', '').replace('\\', '/')
            scanned_paths.append(sf)
        # mm and net files should be present
        self.assertTrue(any(p.startswith('mm/') or p.endswith('mm/a.c')
                            for p in scanned_paths),
                        f'mm/ missing: {scanned_paths}')
        self.assertTrue(any(p.startswith('net/') or p.endswith('net/a.c')
                            for p in scanned_paths),
                        f'net/ missing: {scanned_paths}')
        # kernel should NOT be present
        self.assertFalse(any(p.startswith('kernel/') or 'kernel/' in p
                             for p in scanned_paths),
                         f'kernel/ leaked through filter: {scanned_paths}')
        # top-level top.c must NOT be present
        self.assertFalse(any('top.c' in p for p in scanned_paths))

    def test_scan_subsystems_empty_string_skipped(self):
        # Empty entries are filtered out; scan should still run on all dirs
        result = scan_directory(self.tmpdir, lang='c', workers=1,
                                quiet=True, scan_subsystems=['', '  '])
        self.assertIsInstance(result, dict)


class TestScanDirectoryEmpty(unittest.TestCase):
    """Scanning an empty dir returns a result dict (no crash)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_dir_scan(self):
        result = scan_directory(self.tmpdir, lang='c', workers=1, quiet=True)
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result.get('functions', [])), 0)

    def test_empty_dir_with_subsystem_filter(self):
        result = scan_directory(self.tmpdir, lang='c', workers=1,
                                quiet=True, scan_subsystems=['mm'])
        self.assertIsInstance(result, dict)


if __name__ == '__main__':
    unittest.main()
