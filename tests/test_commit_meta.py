"""Unit tests for commit_meta.py — commit-based provenance.

commit_meta.py detects VCS info (git/svn/none), enriches manifests with
source_commit blocks, queries per-file introduction/last-modified
commits, and runs git blame for line ranges.

Coverage:
- detect_vcs_info: git repo, non-VCS dir, missing dir
- enrich_manifest_with_commit: source_commit block added
- query_commit_for_file: introduced + last_modified commits
- _safe_mtime: mtime fallback for non-VCS files
"""
import os
import subprocess
import tempfile
import unittest


def _init_git_repo(repo_dir: str) -> str:
    """Initialize a real git repo with one commit, for integration tests."""
    subprocess.run(["git", "init"], cwd=repo_dir, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"],
                   cwd=repo_dir, check=True, capture_output=True)
    # Create a file and commit it
    fpath = os.path.join(repo_dir, "hello.c")
    with open(fpath, "w") as f:
        f.write("int main() { return 0; }\n")
    subprocess.run(["git", "add", "hello.c"], cwd=repo_dir, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"],
                   cwd=repo_dir, check=True, capture_output=True)
    return fpath


def _git_has_binary() -> bool:
    """Check if git binary is available (skip tests if not)."""
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


@unittest.skipUnless(_git_has_binary(), "git binary not available")
class TestDetectVcsInfoGit(unittest.TestCase):
    """Tests for detect_vcs_info on a real git repo."""

    def setUp(self):
        self.repo_dir = tempfile.mkdtemp(prefix="c2d_commit_git_")
        _init_git_repo(self.repo_dir)

    def test_detects_git_type(self):
        from _builder.commit_meta import detect_vcs_info
        info = detect_vcs_info(self.repo_dir)
        self.assertEqual(info["type"], "git")

    def test_returns_head_commit_hash(self):
        from _builder.commit_meta import detect_vcs_info
        info = detect_vcs_info(self.repo_dir)
        self.assertIsNotNone(info["head"])
        # SHA-1 hash is 40 chars
        self.assertEqual(len(info["head"]), 40)

    def test_returns_short_head(self):
        from _builder.commit_meta import detect_vcs_info
        info = detect_vcs_info(self.repo_dir)
        self.assertIsNotNone(info["head_short"])
        self.assertEqual(len(info["head_short"]), 8)

    def test_returns_branch(self):
        from _builder.commit_meta import detect_vcs_info
        info = detect_vcs_info(self.repo_dir)
        # Default branch is 'main' or 'master' depending on git version
        self.assertIsNotNone(info["branch"])
        self.assertIn(info["branch"], ("main", "master"))

    def test_returns_author(self):
        from _builder.commit_meta import detect_vcs_info
        info = detect_vcs_info(self.repo_dir)
        self.assertEqual(info["author"], "Test User")

    def test_returns_subject(self):
        from _builder.commit_meta import detect_vcs_info
        info = detect_vcs_info(self.repo_dir)
        self.assertEqual(info["subject"], "initial commit")

    def test_returns_date(self):
        from _builder.commit_meta import detect_vcs_info
        info = detect_vcs_info(self.repo_dir)
        self.assertIsNotNone(info["date"])

    def test_clean_repo_not_dirty(self):
        from _builder.commit_meta import detect_vcs_info
        info = detect_vcs_info(self.repo_dir)
        self.assertFalse(info["is_dirty"])

    def test_dirty_repo_detected(self):
        from _builder.commit_meta import detect_vcs_info
        # Modify the tracked file → working tree becomes dirty
        with open(os.path.join(self.repo_dir, "hello.c"), "a") as f:
            f.write("// modified\n")
        info = detect_vcs_info(self.repo_dir)
        self.assertTrue(info["is_dirty"])


class TestDetectVcsInfoNonVcs(unittest.TestCase):
    """Tests for detect_vcs_info on a non-VCS directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2d_commit_novcs_")
        with open(os.path.join(self.tmp, "plain.c"), "w") as f:
            f.write("int x = 0;\n")

    def test_returns_none_type(self):
        from _builder.commit_meta import detect_vcs_info
        info = detect_vcs_info(self.tmp)
        self.assertEqual(info["type"], "none")
        self.assertIsNone(info["head"])
        self.assertIsNone(info["branch"])

    def test_missing_dir_returns_none_type(self):
        from _builder.commit_meta import detect_vcs_info
        info = detect_vcs_info("/nonexistent/path/that/does/not/exist")
        self.assertEqual(info["type"], "none")

    def test_empty_string_returns_none_type(self):
        from _builder.commit_meta import detect_vcs_info
        info = detect_vcs_info("")
        self.assertEqual(info["type"], "none")


@unittest.skipUnless(_git_has_binary(), "git binary not available")
class TestEnrichManifestWithCommit(unittest.TestCase):
    """Tests for enrich_manifest_with_commit."""

    def setUp(self):
        self.repo_dir = tempfile.mkdtemp(prefix="c2d_commit_manifest_")
        _init_git_repo(self.repo_dir)

    def test_adds_source_commit_block(self):
        from _builder.commit_meta import enrich_manifest_with_commit
        manifest = {"files": [{"path": "hello.c", "size": 100}]}
        result = enrich_manifest_with_commit(manifest, self.repo_dir)
        self.assertIn("source_commit", result)
        self.assertEqual(result["source_commit"]["type"], "git")
        self.assertIsNotNone(result["source_commit"]["head"])

    def test_preserves_existing_manifest_fields(self):
        from _builder.commit_meta import enrich_manifest_with_commit
        manifest = {"files": [{"path": "x.c"}], "scan_time": "2026-01-01"}
        result = enrich_manifest_with_commit(manifest, self.repo_dir)
        self.assertIn("files", result)
        self.assertIn("scan_time", result)
        self.assertIn("source_commit", result)


@unittest.skipUnless(_git_has_binary(), "git binary not available")
class TestQueryCommitForFile(unittest.TestCase):
    """Tests for query_commit_for_file."""

    def setUp(self):
        self.repo_dir = tempfile.mkdtemp(prefix="c2d_commit_queryfile_")
        self.fpath = _init_git_repo(self.repo_dir)

    def test_returns_introduced_and_last_modified(self):
        from _builder.commit_meta import query_commit_for_file
        introduced, last_mod = query_commit_for_file(self.repo_dir, "hello.c")
        # Both should be the same commit (only one commit in the repo)
        self.assertIsNotNone(introduced)
        self.assertIsNotNone(last_mod)
        self.assertEqual(introduced, last_mod)
        self.assertEqual(len(introduced), 40)

    def test_untracked_file_returns_none_none(self):
        from _builder.commit_meta import query_commit_for_file
        # Create a new untracked file
        untracked = os.path.join(self.repo_dir, "untracked.c")
        with open(untracked, "w") as f:
            f.write("int y = 1;\n")
        introduced, last_mod = query_commit_for_file(self.repo_dir, "untracked.c")
        self.assertIsNone(introduced)
        self.assertIsNone(last_mod)

    def test_absolute_path_converted_to_relative(self):
        """Passing an absolute path should still work (converted to relative)."""
        from _builder.commit_meta import query_commit_for_file
        abs_path = os.path.join(self.repo_dir, "hello.c")
        introduced, last_mod = query_commit_for_file(self.repo_dir, abs_path)
        self.assertIsNotNone(introduced)
        self.assertIsNotNone(last_mod)


class TestSafeMtime(unittest.TestCase):
    """Tests for _safe_mtime (fallback for non-VCS files)."""

    def test_returns_iso_format_for_existing_file(self):
        from _builder.commit_meta import _safe_mtime
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        try:
            mtime = _safe_mtime(tmp.name)
            self.assertIsNotNone(mtime)
            # ISO 8601 format: YYYY-MM-DDTHH:MM:SS
            self.assertRegex(mtime, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        finally:
            os.unlink(tmp.name)

    def test_returns_none_for_missing_file(self):
        from _builder.commit_meta import _safe_mtime
        self.assertIsNone(_safe_mtime("/nonexistent/file/path"))


if __name__ == "__main__":
    unittest.main()
