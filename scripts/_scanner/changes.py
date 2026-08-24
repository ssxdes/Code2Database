"""Change detection utilities for incremental graph updates.

Extracted from code2database_scanner.py so both the scanner CLI and
_builder/patcher.py can import without circular dependencies.
"""

import json
import os
from pathlib import Path

from _scanner.utils import LANG_EXTENSIONS


# Directories to skip (build artifacts, VCS, dependencies)
_SKIP_DIRS = frozenset({
    '__pycache__', 'node_modules', '.git', '.svn', '.hg',
    'build', 'dist', 'out', 'bin', 'obj',
    'venv', '.venv', '.env',
    '.tox', '.mypy_cache', '.pytest_cache',
    'target', 'CMakeFiles', 'cmake-build-debug', 'cmake-build-release',
    '.cache',
})


def _file_fingerprint(fpath: str) -> str:
    """Compute a lightweight fingerprint for a source file (mtime + size)."""
    try:
        st = os.stat(fpath)
        return f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return ""


def save_manifest(source_root: str, outdir: str) -> int:
    """Save a manifest of all source file fingerprints.

    Returns the number of files fingerprinted.

    Also records source_commit (git/svn HEAD info) so
    engineers can ask "which commit does this graph correspond to?" rather
    than the meaningless "when was the database updated?".
    """
    all_extensions = set()
    for exts in LANG_EXTENSIONS.values():
        all_extensions |= exts

    manifest = {}
    for dirpath, dirnames, filenames in os.walk(source_root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith('.') and d not in _SKIP_DIRS]
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            ext = Path(fpath).suffix.lower()
            if ext in all_extensions:
                rel = os.path.relpath(fpath, source_root)
                manifest[rel] = _file_fingerprint(fpath)

    manifest_data = {"source_root": source_root, "files": manifest}

    # Deficiency 2: attach commit metadata so the manifest records which
    # commit the fingerprints correspond to (not just the database write time).
    try:
        try:
            from _builder.commit_meta import enrich_manifest_with_commit
        except ImportError:
            import sys
            sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
            from _builder.commit_meta import enrich_manifest_with_commit
        enrich_manifest_with_commit(manifest_data, source_root)
    except Exception as exc:
        # Best-effort: if commit detection fails, manifest still saves without
        # source_commit (legacy behavior).
        import sys
        print(f"[manifest] commit detection failed, skipping source_commit: {exc}",
              file=sys.stderr)

    manifest_path = os.path.join(outdir, ".code2database_manifest.json")
    Path(manifest_path).write_text(
        json.dumps(manifest_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )
    return len(manifest)


def detect_changes(source_root: str, outdir: str) -> dict:
    """Compare current source tree against stored manifest.

    Returns dict with keys: new_files, changed_files, deleted_files,
    unchanged_count, needs_full_scan.
    """
    manifest_path = os.path.join(outdir, ".code2database_manifest.json")
    if not os.path.exists(manifest_path):
        return {"new_files": [], "changed_files": [], "deleted_files": [],
                "unchanged_count": 0, "needs_full_scan": True}

    old_manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    old_files = old_manifest.get("files", {})

    all_extensions = set()
    for exts in LANG_EXTENSIONS.values():
        all_extensions |= exts

    current_files = {}
    for dirpath, dirnames, filenames in os.walk(source_root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith('.') and d not in _SKIP_DIRS]
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            ext = Path(fpath).suffix.lower()
            if ext in all_extensions:
                rel = os.path.relpath(fpath, source_root)
                current_files[rel] = _file_fingerprint(fpath)

    new_files, changed_files, deleted_files = [], [], []
    unchanged_count = 0

    for rel, fp in current_files.items():
        abs_path = os.path.join(source_root, rel)
        if rel not in old_files:
            new_files.append(abs_path)
        elif old_files[rel] != fp:
            changed_files.append(abs_path)
        else:
            unchanged_count += 1

    for rel in old_files:
        if rel not in current_files:
            deleted_files.append(os.path.join(source_root, rel))

    return {
        "new_files": new_files,
        "changed_files": changed_files,
        "deleted_files": deleted_files,
        "unchanged_count": unchanged_count,
        "needs_full_scan": False,
    }
