#!/usr/bin/env python3
"""Commit-metadata extraction for Code2Database.

Provides git/svn commit detection so nodes/edges can be attributed to the
commit that introduced or last modified them — engineers ask "which commit
introduced this bug", not "when was the database updated".

Public API:
- detect_vcs_info(source_root) → dict with vcs type, head commit, branch, etc.
- enrich_manifest_with_commit(manifest_dict, source_root) → adds source_commit
- query_commit_for_file(source_root, file_path) → (introduced_commit, last_modified_commit)
- query_commit_for_lines(source_root, file_path, line_range) → blame-style mapping
- store_commit_aware_change_log(store, source_root, changed_files, branch)

Design notes:
- All git invocations use --no-pager and -c to suppress pager/config issues.
- svn support is best-effort: many projects are git, svn is rare but supported.
- For non-VCS projects, returns vcs_type='none' and callers fall back to mtime.
- Heavy operations (git blame) are optional and only invoked when explicitly
  needed — the build path only records current HEAD, not per-line blame.
"""

import os
import re
import subprocess
import sys
from typing import Optional, Dict, Tuple, List
import logging


def _run_git(args: List[str], cwd: str, timeout: int = 10) -> Optional[str]:
    """Run a git command, return stdout or None on failure."""
    try:
        result = subprocess.run(
            ["git", "-c", "core.pager=cat"] + args,
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
            check=False
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _run_svn(args: List[str], cwd: str, timeout: int = 15) -> Optional[str]:
    """Run an svn command, return stdout or None on failure."""
    try:
        result = subprocess.run(
            ["svn"] + args,
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
            check=False
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def detect_vcs_info(source_root: str) -> Dict:
    """Detect VCS info for a source tree.

    Returns a dict with:
        type: 'git' / 'svn' / 'none'
        head: full commit hash (git) or revision number (svn)
        head_short: short hash or revision
        branch: branch name (git only)
        author: commit author
        date: commit date (ISO 8601)
        subject: commit subject (first line of message)
        is_dirty: True if working tree has uncommitted changes (git)
        svn_revision: revision number (svn only)
    """
    info = {
        "type": "none",
        "head": None, "head_short": None,
        "branch": None, "author": None,
        "date": None, "subject": None,
        "is_dirty": False, "svn_revision": None,
    }
    if not source_root or not os.path.isdir(source_root):
        return info

    # Try git first (most common)
    git_dir = _run_git(["rev-parse", "--git-dir"], source_root)
    if git_dir is not None:
        info["type"] = "git"
        head = _run_git(["rev-parse", "HEAD"], source_root)
        if head:
            info["head"] = head
            info["head_short"] = head[:8]
        # Branch
        branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], source_root)
        info["branch"] = branch if branch and branch != "HEAD" else None
        # Author/date/subject
        fmt = "%H%n%an%n%aI%n%s"
        log = _run_git(["--no-pager", "log", "-1", f"--pretty=format:{fmt}", "HEAD"],
                       source_root)
        if log:
            lines = log.split("\n", 3)
            if len(lines) >= 4:
                info["author"] = lines[1]
                info["date"] = lines[2]
                info["subject"] = lines[3]
        # Dirty check
        status = _run_git(["status", "--porcelain"], source_root)
        info["is_dirty"] = bool(status and status.strip())
        return info

    # Try svn
    svn_info = _run_svn(["info", "--show-item", "revision", source_root], source_root)
    if svn_info and svn_info.isdigit():
        info["type"] = "svn"
        info["svn_revision"] = svn_info
        info["head"] = svn_info
        info["head_short"] = svn_info
        # Try to get last commit author/date
        svn_log = _run_svn(["log", "-l", "1", "--xml", source_root], source_root, timeout=30)
        if svn_log:
            author_m = re.search(r"<author>([^<]+)</author>", svn_log)
            date_m = re.search(r"<date>([^<]+)</date>", svn_log)
            msg_m = re.search(r"<msg>([^<]*)</msg>", svn_log)
            if author_m:
                info["author"] = author_m.group(1)
            if date_m:
                info["date"] = date_m.group(1)
            if msg_m:
                info["subject"] = msg_m.group(1)
        return info

    return info


def enrich_manifest_with_commit(manifest: Dict, source_root: str) -> Dict:
    """Add a 'source_commit' block to an existing manifest dict.

    Used by save_manifest to record which commit the manifest corresponds to.
    The build_timestamp stays separate — it's the database write time (not
    engineer-relevant), source_commit is the code commit (engineer-relevant).
    """
    vcs = detect_vcs_info(source_root)
    manifest["source_commit"] = vcs
    return manifest


def query_commit_for_file(source_root: str, file_path: str) -> Tuple[Optional[str], Optional[str]]:
    """Get (introduced_commit, last_modified_commit) for a file.

    introduced_commit: the commit that first added the file (git log --diff-filter=A)
    last_modified_commit: the most recent commit touching the file (git log -1)

    Returns (None, None) if not a git repo or file not tracked.
    """
    rel = file_path
    if os.path.isabs(file_path) and source_root:
        try:
            rel = os.path.relpath(file_path, source_root)
        except ValueError:
            rel = file_path

    introduced = _run_git(["log", "--diff-filter=A", "--follow", "--pretty=format:%H",
                           "--", rel], source_root, timeout=15)
    if introduced:
        # Take the last line (oldest commit, since git log is newest-first)
        lines = [l for l in introduced.split("\n") if l.strip()]
        introduced = lines[-1] if lines else None
    else:
        introduced = None

    last_mod = _run_git(["log", "-1", "--pretty=format:%H", "--", rel],
                        source_root, timeout=10)
    return (introduced, last_mod or None)


def query_blame_for_lines(source_root: str, file_path: str,
                          start_line: int, end_line: int) -> Dict[int, str]:
    """Git blame a line range, returning {line_number: commit_hash}.

    Only used when explicitly needed (e.g., attributing a specific function
    body to a commit). Skipped by default in the build path.
    """
    rel = file_path
    if os.path.isabs(file_path) and source_root:
        try:
            rel = os.path.relpath(file_path, source_root)
        except ValueError:
            rel = file_path

    line_range = f"{start_line},{end_line}"
    out = _run_git(["blame", "-L", line_range, "--line-porcelain", "--", rel],
                   source_root, timeout=30)
    if not out:
        return {}
    result = {}
    current_line = None
    for line in out.split("\n"):
        if line.startswith("commit "):
            current_commit = line.split()[1]
        elif re.match(r"^\S+ \d+ \d+ \d+", line):
            # Format: <hash> <orig-line> <final-line> <line-count>
            parts = line.split()
            if len(parts) >= 3:
                try:
                    final_line = int(parts[2])
                    if current_line:
                        result[final_line] = current_line
                except ValueError:
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
    return result


def commit_meta_for_node(source_root: str, node_id: str,
                         source_file: str, line: int) -> Dict:
    """Build a commit_meta dict for a single node.

    For efficiency, this only queries last_modified_commit per file (not per
    line) by default. Per-line blame is opt-in via query_blame_for_lines.
    """
    vcs = detect_vcs_info(source_root)
    if vcs["type"] == "none":
        return {
            "type": "none",
            "scan_timestamp": None,  # filled by caller
            "file_mtime": _safe_mtime(source_file),
            "warning": "no vcs detected, falling back to mtime-based tracking"
        }

    introduced, last_mod = query_commit_for_file(source_root, source_file)

    if vcs["type"] == "git":
        meta = {
            "type": "git",
            "introduced_commit": introduced,
            "introduced_commit_short": introduced[:8] if introduced else None,
            "last_modified_commit": last_mod,
            "last_modified_commit_short": last_mod[:8] if last_mod else None,
            "current_branch": vcs.get("branch"),
            "current_head": vcs.get("head"),
            "current_head_short": vcs.get("head_short"),
        }
        # Fill in author/date/subject for last_modified_commit
        if last_mod:
            fmt = "%an%n%aI%n%s"
            log = _run_git(["--no-pager", "log", "-1", f"--pretty=format:{fmt}", last_mod],
                           source_root, timeout=10)
            if log:
                lines = log.split("\n", 2)
                if len(lines) >= 3:
                    meta["last_modified_commit_author"] = lines[0]
                    meta["last_modified_commit_date"] = lines[1]
                    meta["last_modified_commit_subject"] = lines[2]
        if introduced and introduced != last_mod:
            fmt = "%an%n%aI%n%s"
            log = _run_git(["--no-pager", "log", "-1", f"--pretty=format:{fmt}", introduced],
                           source_root, timeout=10)
            if log:
                lines = log.split("\n", 2)
                if len(lines) >= 3:
                    meta["introduced_commit_author"] = lines[0]
                    meta["introduced_commit_date"] = lines[1]
                    meta["introduced_commit_subject"] = lines[2]
        return meta

    if vcs["type"] == "svn":
        return {
            "type": "svn",
            "svn_revision": vcs.get("svn_revision"),
            "last_modified_revision": last_mod,
            "current_head": vcs.get("svn_revision"),
        }

    return {"type": "none", "warning": "unknown vcs"}


def _safe_mtime(file_path: str) -> Optional[str]:
    try:
        st = os.stat(file_path)
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(st.st_mtime, timezone.utc)
        return dt.isoformat()
    except OSError:
        return None


def store_commit_aware_change_log(store, source_root: str,
                                  changed_files: List[str],
                                  branch: Optional[str] = None) -> int:
    """Record a change_log entry per changed file (commit-aware).

    Called after a build/update to record which commit affected which files.
    Uses current HEAD as the commit (assumes changes are committed).
    Returns number of entries written.
    """
    from datetime import datetime, timezone
    vcs = detect_vcs_info(source_root)
    if vcs["type"] == "none":
        return 0

    commit_hash = vcs.get("head")
    if not commit_hash:
        return 0

    logged_at = datetime.now(timezone.utc).isoformat()
    written = 0
    for file_path in changed_files:
        entry = {
            "commit_hash": commit_hash,
            "commit_short": vcs.get("head_short"),
            "commit_author": vcs.get("author"),
            "commit_date": vcs.get("date"),
            "commit_subject": vcs.get("subject"),
            "branch": branch or vcs.get("branch"),
            "node_id": None,  # filled by caller with affected node ids
            "change_type": "modified",
            "diff_summary": f"file changed: {file_path}",
            "affected_attrs": ["body_text", "callee_args"],
            "logged_at": logged_at,
        }
        try:
            store.store_change_log_entry(entry)
            written += 1
        except Exception as exc:
            print(f"[commit_meta] change_log write failed for {file_path}: {exc}",
                  file=sys.stderr)
    return written
