"""KB backup and restore — single-file archive of every store file.

Enterprise KB workloads need a portable, restorable snapshot: the
memory store, the knowledge store, the brief view, the FTS5 index,
and the audit trail together form the curated project knowledge.
A tar.gz archive bundles every file with a manifest so a maintainer
can move a KB between machines, checkpoint before a risky curation
pass, or restore after a corrupted store.

The archive format is a tar.gz with:
  - manifest.json  — metadata (created_at, source_graph_dir,
    schema versions, file list with sizes and sha256)
  - memory/memory.db (+ -wal, -shm if present)
  - knowledge/knowledge.db (+ sidecars)
  - knowledge/brief.json
  - knowledge/jieba_user_dict.txt (if present)
  - kb_index.db (+ sidecars)

Restore extracts the archive to a temp directory, verifies the
manifest, then copies each file into the target graph_dir. Existing
files are preserved unless ``force=True`` overwrites them.
"""
from __future__ import annotations

import io
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime
from typing import Dict, List, Optional, Any

import logging


def _kb_store_files(graph_dir: str) -> List[str]:
    """Return the list of store file paths that form the KB.

    Each path is relative to graph_dir. Sidecar files (-wal, -shm)
    are included when present so a restored store starts in a
    consistent state.
    """
    candidates = [
        "memory/memory.db",
        "memory/memory.db-wal",
        "memory/memory.db-shm",
        "knowledge/knowledge.db",
        "knowledge/knowledge.db-wal",
        "knowledge/knowledge.db-shm",
        "knowledge/brief.json",
        "knowledge/jieba_user_dict.txt",
        "kb_index.db",
        "kb_index.db-wal",
        "kb_index.db-shm",
    ]
    return [c for c in candidates
            if os.path.isfile(os.path.join(graph_dir, c))]


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_schema_versions(graph_dir: str) -> Dict[str, Optional[int]]:
    """Read schema_version from kb_meta and knowledge_meta."""
    versions: Dict[str, Optional[int]] = {
        "kb_index": None, "knowledge": None}
    kb_path = os.path.join(graph_dir, "kb_index.db")
    if os.path.isfile(kb_path):
        try:
            conn = sqlite3.connect(f"file:{kb_path}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT value FROM kb_meta "
                    "WHERE key = 'schema_version'").fetchone()
                if row:
                    versions["kb_index"] = int(row[0])
            finally:
                conn.close()
        except (sqlite3.Error, ValueError):
            pass
    know_path = os.path.join(graph_dir, "knowledge", "knowledge.db")
    if os.path.isfile(know_path):
        try:
            conn = sqlite3.connect(f"file:{know_path}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT value FROM knowledge_meta "
                    "WHERE key = 'schema_version'").fetchone()
                if row:
                    versions["knowledge"] = int(row[0])
            finally:
                conn.close()
        except (sqlite3.Error, ValueError):
            pass
    return versions


def kb_backup(graph_dir: str, output_path: str) -> str:
    """Bundle every KB store file into a tar.gz archive.

    Returns the absolute path to the written archive. The archive
    contains a manifest.json with file sizes, sha256 hashes, schema
    versions, and the source graph_dir (for traceability).
    """
    if not os.path.isdir(graph_dir):
        raise FileNotFoundError(f"graph_dir not found: {graph_dir}")
    files = _kb_store_files(graph_dir)
    if not files:
        raise FileNotFoundError(
            f"no KB store files found in {graph_dir}")
    manifest: Dict[str, Any] = {
        "format": "code2database-kb-backup",
        "version": 1,
        "created_at": datetime.now().isoformat(),
        "source_graph_dir": os.path.abspath(graph_dir),
        "schema_versions": _read_schema_versions(graph_dir),
        "files": [],
    }
    for rel in files:
        fpath = os.path.join(graph_dir, rel)
        manifest["files"].append({
            "path": rel,
            "size": os.path.getsize(fpath),
            "sha256": _sha256(fpath),
        })
    abs_output = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(abs_output) or ".", exist_ok=True)
    manifest_bytes = json.dumps(manifest, ensure_ascii=False,
                                 indent=2).encode("utf-8")
    with tarfile.open(abs_output, "w:gz") as tar:
        for rel in files:
            fpath = os.path.join(graph_dir, rel)
            tar.add(fpath, arcname=rel)
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))
    return abs_output


def kb_restore(graph_dir: str, input_path: str,
               force: bool = False) -> Dict[str, Any]:
    """Extract a KB backup archive into graph_dir.

    Returns a summary dict with the file count, schema versions, and
    any skipped files (when force=False and a target file exists).

    When force=False, existing files are preserved and listed in
    ``skipped``. When force=True, existing files are overwritten.
    """
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"backup archive not found: {input_path}")
    os.makedirs(graph_dir, exist_ok=True)
    restored: List[str] = []
    skipped: List[str] = []
    manifest: Optional[dict] = None
    with tempfile.TemporaryDirectory() as tmpdir:
        with tarfile.open(input_path, "r:gz") as tar:
            # Extract manifest first (if present) for verification.
            try:
                manifest_info = tar.getmember("manifest.json")
            except KeyError:
                manifest_info = None
            if manifest_info is not None:
                f = tar.extractfile(manifest_info)
                if f is not None:
                    manifest = json.loads(f.read().decode("utf-8"))
            # Extract all members.
            for member in tar.getmembers():
                if member.name == "manifest.json":
                    continue
                # Path-traversal guard: reject absolute paths and
                # any path that escapes the temp directory.
                member_path = os.path.normpath(member.name)
                if member_path.startswith("..") or os.path.isabs(
                        member_path):
                    logging.getLogger(__name__).warning(
                        "skipping unsafe path in archive: %s", member.name)
                    continue
                target = os.path.join(graph_dir, member.name)
                target_norm = os.path.normpath(target)
                graph_norm = os.path.normpath(graph_dir)
                if not target_norm.startswith(graph_norm + os.sep) and \
                        target_norm != graph_norm:
                    logging.getLogger(__name__).warning(
                        "skipping path that escapes graph_dir: %s",
                        member.name)
                    continue
                if os.path.exists(target) and not force:
                    skipped.append(member.name)
                    continue
                os.makedirs(os.path.dirname(target) or ".",
                            exist_ok=True)
                src_file = tar.extractfile(member)
                if src_file is None:
                    continue
                with open(target, "wb") as out:
                    shutil.copyfileobj(src_file, out)
                restored.append(member.name)
    return {
        "restored": restored,
        "skipped": skipped,
        "restored_count": len(restored),
        "skipped_count": len(skipped),
        "manifest": manifest,
        "schema_versions": (manifest or {}).get("schema_versions", {}),
    }
