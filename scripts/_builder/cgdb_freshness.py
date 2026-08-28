"""Graph freshness checker — detect when the code graph is stale.

Checks if source files have changed since the last scan/build by
comparing current file mtimes against the scan manifest. Also
detects:
- New files added (not in manifest)
- Existing files modified (mtime changed)
- Files deleted (in manifest but not on disk)
- Git HEAD changed (different commit than what graph was built from)

Output: a report showing what's stale and suggesting which commands
to run to bring the graph up to date.
"""
from __future__ import annotations

import json
import os
from typing import Dict
import logging


def check_freshness(graph_dir: str, source_root: str = "") -> Dict:
    """Check if the code graph is fresh or stale.

    Returns a dict with:
      - is_fresh: bool (True if nothing changed since last scan)
      - new_files: list of new source files not in manifest
      - changed_files: list of files with changed mtime
      - deleted_files: list of files in manifest but no longer on disk
      - git_head_changed: bool (True if git HEAD differs from graph's commit)
      - last_scan_commit: str (the commit the graph was built from)
      - current_commit: str (current git HEAD)
      - staleness_ratio: float (changed/total, 0.0=fresh, 1.0=fully stale)
      - recommendation: str (suggested command to run)
    """
    result = {
        "is_fresh": True,
        "new_files": [],
        "changed_files": [],
        "deleted_files": [],
        "git_head_changed": False,
        "last_scan_commit": "unknown",
        "current_commit": "unknown",
        "staleness_ratio": 0.0,
        "recommendation": "",
    }

    # Load manifest
    manifest_path = os.path.join(graph_dir, ".code2database_manifest.json")
    manifest = {}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest_data = json.load(f)
                manifest = manifest_data.get("files", {})
                result["last_scan_commit"] = manifest_data.get("source_commit", "unknown")
        except (OSError, json.JSONDecodeError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    current_commit = _detect_git_head(source_root or os.path.dirname(graph_dir))
    result["current_commit"] = current_commit
    if current_commit and result["last_scan_commit"] != "unknown":
        if current_commit != result["last_scan_commit"]:
            result["git_head_changed"] = True
            result["is_fresh"] = False

    # If no manifest, we can't compare — assume stale
    if not manifest:
        result["is_fresh"] = False
        result["recommendation"] = "No scan manifest found. Run `scan` to create the graph."
        return result

    # Check freshness file (written by daemon)
    freshness_path = os.path.join(graph_dir, ".code2database_freshness.json")
    if os.path.exists(freshness_path):
        try:
            with open(freshness_path, "r", encoding="utf-8") as f:
                freshness = json.load(f)
            if freshness.get("last_sync_time"):
                result["last_sync_time"] = freshness["last_sync_time"]
        except (OSError, json.JSONDecodeError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    from _scanner.changes import _ALL_SOURCE_EXTENSIONS, _SKIP_DIRS
    from pathlib import Path

    current_files = {}
    if source_root and os.path.exists(source_root):
        for dirpath, dirnames, filenames in os.walk(source_root):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith('.') and d not in _SKIP_DIRS]
            for fname in filenames:
                dot = fname.rfind('.')
                ext = fname[dot:].lower() if dot >= 0 else ''
                if ext in _ALL_SOURCE_EXTENSIONS:
                    fpath = os.path.join(dirpath, fname)
                    rel = os.path.relpath(fpath, source_root)
                    try:
                        st = os.stat(fpath)
                        current_files[rel] = f"{st.st_mtime_ns}:{st.st_size}"
                    except OSError:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
    new_files = []
    changed_files = []
    for rel, fingerprint in current_files.items():
        if rel not in manifest:
            new_files.append(rel)
        elif manifest[rel] != fingerprint:
            changed_files.append(rel)

    deleted_files = [rel for rel in manifest if rel not in current_files]

    result["new_files"] = new_files[:100]  # Cap for display
    result["changed_files"] = changed_files[:100]
    result["deleted_files"] = deleted_files[:100]
    result["new_count"] = len(new_files)
    result["changed_count"] = len(changed_files)
    result["deleted_count"] = len(deleted_files)

    total = len(manifest)
    changed_total = len(new_files) + len(changed_files) + len(deleted_files)
    if total > 0:
        result["staleness_ratio"] = round(changed_total / total, 3)

    if changed_total > 0:
        result["is_fresh"] = False

    # Recommendation
    if result["git_head_changed"] and changed_total > 0:
        result["recommendation"] = (
            f"Git HEAD changed and {changed_total} files differ. "
            f"Run: scan --source <source> --incremental, then build"
        )
    elif result["git_head_changed"]:
        result["recommendation"] = (
            "Git HEAD changed but no source files differ. "
            "Run: quick-update to rebind profile to new HEAD."
        )
    elif changed_total > 0:
        if changed_total <= 10:
            result["recommendation"] = (
                f"{changed_total} files changed. Run: quick-update"
            )
        elif changed_total <= 100:
            result["recommendation"] = (
                f"{changed_total} files changed. Run: scan --incremental, then build"
            )
        else:
            result["recommendation"] = (
                f"{changed_total} files changed (>100). "
                f"Full re-scan recommended: scan --source <source>"
            )
    else:
        result["recommendation"] = "Graph is up to date."

    return result


def _detect_git_head(source_root: str) -> str:
    """Detect the current git HEAD commit hash."""
    try:
        import subprocess
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=source_root,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()[:12]  # Short hash
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    return "unknown"


def cmd_cgdb_freshness(args):
    """CLI handler for `code2database_builder.py cgdb-freshness`."""
    graph_dir = args.graph
    source_root = getattr(args, "source", "") or os.path.dirname(graph_dir)
    result = check_freshness(graph_dir, source_root)

    if result["is_fresh"]:
        print("✓ Graph is fresh — no changes detected since last scan.")
    else:
        print("⚠ Graph is stale:\n")
        if result["git_head_changed"]:
            print(f"  Git HEAD changed: {result['last_scan_commit']} → {result['current_commit']}")
        if result.get("new_count", 0) > 0:
            print(f"  New files: {result['new_count']}")
            for f in result["new_files"][:5]:
                print(f"    + {f}")
        if result.get("changed_count", 0) > 0:
            print(f"  Modified files: {result['changed_count']}")
            for f in result["changed_files"][:5]:
                print(f"    ~ {f}")
        if result.get("deleted_count", 0) > 0:
            print(f"  Deleted files: {result['deleted_count']}")
            for f in result["deleted_files"][:5]:
                print(f"    - {f}")
        print(f"\n  Staleness: {result['staleness_ratio']*100:.1f}%")
        print(f"\n  {result['recommendation']}")
    return 0 if result["is_fresh"] else 1
