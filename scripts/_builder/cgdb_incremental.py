"""cgdb_incremental — content hash + #include dependency graph for incremental sync.

Per cgdb-architecture-and-poc-report.md Phase 5 (C4):
- Replace mtime+size with SHA-256 content hash for change detection
- Build a #include dependency graph (file → files it includes)
- Compute reverse dependency graph (file → files that include it)
- compute_affected_tus(changed_files) returns transitive closure —
  all TUs that transitively include any changed file

Reuses `_get_file_includes` from `import_resolve.py` for #include parsing.
The cgdb schema's `cgdb_files.content_hash` column stores the per-file hash,
and `cgdb_includes` stores the include graph (source_file_id → included_path).

Usage:
    from _builder.cgdb_incremental import IncrementalSync
    sync = IncrementalSync(source_root)
    changed = sync.detect_changes(db_path)  # returns list of changed file paths
    affected = sync.compute_affected_tus(changed)  # transitive closure
    sync.mark_clean(changed, db_path)  # update content_hash after rebuild
"""
import hashlib
import os
import re
import sqlite3
from typing import Dict, List, Optional, Set, Tuple

try:
    from _builder.import_resolve import _get_file_includes
except ImportError:
    _get_file_includes = None


# Regex for C/C++ #include — kept here too so this module works standalone
# (import_resolve._get_file_includes already does this, but we cache the regex
# for fast in-memory lookups when iterating many files).
_INCLUDE_RE = re.compile(r'^\s*#\s*include\s+[<"]([^>"]+)[>"]', re.MULTILINE)


def compute_content_hash(file_path: str) -> str:
    """Compute SHA-256 hex digest of file content. Returns '' on error."""
    try:
        h = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()
    except (IOError, OSError):
        return ''


def parse_includes(file_path: str) -> List[str]:
    """Parse #include directives from a C/C++ source file.

    Returns a list of included header paths (as written in the source,
    e.g., 'stdio.h' or 'linux/kernel.h').
    """
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except (IOError, OSError):
        return []
    return [m.group(1) for m in _INCLUDE_RE.finditer(content)]


class IncrementalSync:
    """Manages content-hash-based incremental sync + #include dep graph.

    Lifecycle:
        1. Construct with source_root.
        2. Call `detect_changes(db_path)` to get changed files (comparing
           current content hash against stored hash in cgdb_files).
        3. Call `compute_affected_tus(changed_files)` to expand the changed
           set with all TUs that transitively #include a changed file.
        4. Re-scan and rebuild affected TUs.
        5. Call `mark_clean(updated_files, db_path)` to update stored
           content hashes after rebuild.
    """

    def __init__(self, source_root: str):
        self.source_root = os.path.abspath(source_root)
        # include_graph: file_path → list of included header paths (as written)
        self._include_graph: Dict[str, List[str]] = {}
        # reverse_graph: resolved file_path → set of file_paths that include it
        self._reverse_graph: Dict[str, Set[str]] = {}
        # resolved_header_index: header_name → set of full file paths matching
        self._header_index: Dict[str, Set[str]] = {}
        self._graph_built = False

    def _ensure_graph(self) -> None:
        """Lazily build the include graph by scanning source_root."""
        if self._graph_built:
            return
        self._build_include_graph()
        self._graph_built = True

    def _build_include_graph(self) -> None:
        """Walk source_root and build the #include dependency graph.

        For each .c/.cc/.cpp/.cxx file (translation unit), record its
        #include list. For each .h/.hpp file, record it in the header
        index so we can resolve 'foo.h' to actual file paths.
        """
        src_exts = ('.c', '.cc', '.cpp', '.cxx', '.c++')
        hdr_exts = ('.h', '.hpp', '.hh', '.hxx')
        for root, _dirs, files in os.walk(self.source_root):
            # Skip common build/VCS directories
            if any(part in ('.git', '.svn', 'build', 'dist', 'out',
                            '__pycache__', '.cache', 'third_party', 'vendor')
                   for part in root.split(os.sep)):
                continue
            for fname in files:
                fpath = os.path.join(root, fname)
                ext = os.path.splitext(fname)[1].lower()
                if ext in src_exts:
                    includes = parse_includes(fpath)
                    self._include_graph[fpath] = includes
                elif ext in hdr_exts:
                    # Index header by basename and full relative path
                    self._header_index.setdefault(fname, set()).add(fpath)
                    rel = os.path.relpath(fpath, self.source_root)
                    self._header_index.setdefault(rel, set()).add(fpath)
        # Build reverse graph: for each TU, resolve its includes to file paths,
        # then for each resolved header, mark TU as a dependent.
        for tu_path, includes in self._include_graph.items():
            for inc in includes:
                # Try to resolve: basename match, then relative path match
                resolved_set = self._resolve_include(inc)
                for resolved in resolved_set:
                    self._reverse_graph.setdefault(resolved, set()).add(tu_path)

    def _resolve_include(self, include_path: str) -> Set[str]:
        """Resolve a #include path (e.g., 'linux/kernel.h') to actual file paths.

        Returns a set of matching file paths in source_root. Handles:
          - basename match (e.g., 'kernel.h' matches any 'kernel.h')
          - relative path match (e.g., 'linux/kernel.h' matches
            '<root>/linux/kernel.h' or any '<root>/sub/linux/kernel.h')
        """
        result: Set[str] = set()
        # Direct relative-path match
        if include_path in self._header_index:
            result.update(self._header_index[include_path])
        # Basename match
        basename = os.path.basename(include_path)
        if basename in self._header_index:
            result.update(self._header_index[basename])
        return result

    def detect_changes(self, db_path: str) -> List[str]:
        """Detect files whose content hash differs from the stored value.

        Returns a list of file paths (relative to source_root if possible,
        else absolute) whose content has changed (or that are new).
        Files in source_root not present in cgdb_files are considered new.
        Files in cgdb_files not present on disk are considered deleted
        (returned with their stored path).
        """
        self._ensure_graph()
        if not os.path.exists(db_path):
            # No DB yet — everything is new
            return list(self._include_graph.keys())
        conn = sqlite3.connect(db_path)
        try:
            stored = {}
            try:
                rows = conn.execute(
                    "SELECT path, content_hash FROM cgdb_files"
                ).fetchall()
                for path, h in rows:
                    stored[path] = h or ''
            except sqlite3.OperationalError:
                # cgdb_files table doesn't exist yet
                return list(self._include_graph.keys())
        finally:
            conn.close()
        changed: List[str] = []
        # Check existing files on disk against stored hashes
        seen_paths = set()
        for fpath in self._include_graph.keys():
            seen_paths.add(fpath)
            cur_hash = compute_content_hash(fpath)
            stored_hash = stored.get(fpath, '')
            if cur_hash != stored_hash:
                changed.append(fpath)
        # Check for deleted files (in DB but not on disk)
        for path in stored:
            if path not in seen_paths and not os.path.exists(path):
                changed.append(path)
        return changed

    def compute_affected_tus(self, changed_files: List[str]) -> List[str]:
        """Compute the transitive closure of affected TUs.

        For each changed file, find all TUs that directly include it
        (via reverse_graph), then recursively expand: if a TU includes
        a changed header, that TU itself is "affected" — but we don't
        need to re-expand from TUs (TUs aren't included by other TUs).
        However, if a changed file is itself a header that's included
        by another header, we need to walk the reverse_graph transitively.

        Args:
            changed_files: list of changed file paths (absolute or relative
                           to source_root).

        Returns:
            List of TU file paths (.c/.cpp) that need to be re-scanned.
            Includes the changed TUs themselves (a changed .c file is its
            own affected TU).
        """
        self._ensure_graph()
        affected: Set[str] = set()
        # Build a header-to-header reverse graph on the fly: for each header,
        # find which files include it (TUs AND other headers).
        # The _reverse_graph already includes all dependents (TUs + headers),
        # because we index headers too in _build_include_graph... but actually
        # we only parsed includes from src_exts files. Let's also parse
        # includes from header files for the reverse graph.
        header_include_graph: Dict[str, List[str]] = {}
        for hdr_paths in self._header_index.values():
            for hdr_path in hdr_paths:
                if hdr_path not in header_include_graph:
                    header_include_graph[hdr_path] = parse_includes(hdr_path)
        # Update reverse graph with header→header edges
        for hdr_path, includes in header_include_graph.items():
            for inc in includes:
                resolved_set = self._resolve_include(inc)
                for resolved in resolved_set:
                    self._reverse_graph.setdefault(resolved, set()).add(hdr_path)

        # BFS through reverse_graph
        queue = list(changed_files)
        visited: Set[str] = set()
        while queue:
            cur = queue.pop()
            if cur in visited:
                continue
            visited.add(cur)
            # If cur is a TU, it's affected
            ext = os.path.splitext(cur)[1].lower()
            if ext in ('.c', '.cc', '.cpp', '.cxx', '.c++'):
                affected.add(cur)
            # Walk reverse edges: files that include cur
            for dependent in self._reverse_graph.get(cur, set()):
                if dependent not in visited:
                    queue.append(dependent)
        return sorted(affected)

    def mark_clean(self, file_paths: List[str], db_path: str) -> int:
        """Update stored content_hash for the given files in cgdb_files.

        Called after a successful rebuild to mark the files as up-to-date.
        Returns the number of rows updated.
        """
        if not os.path.exists(db_path):
            return 0
        conn = sqlite3.connect(db_path)
        rows = 0
        try:
            for fpath in file_paths:
                if not os.path.exists(fpath):
                    # File was deleted — remove its row
                    cur = conn.execute(
                        "DELETE FROM cgdb_files WHERE path = ?", (fpath,)
                    )
                    rows += cur.rowcount
                    continue
                h = compute_content_hash(fpath)
                cur = conn.execute(
                    "UPDATE cgdb_files SET content_hash = ?, sha256 = ? "
                    "WHERE path = ?",
                    (h, h, fpath)
                )
                rows += cur.rowcount
            conn.commit()
        except sqlite3.OperationalError:
            pass  # cgdb_files table doesn't exist
        finally:
            conn.close()
        return rows

    def get_stored_hash(self, file_path: str, db_path: str) -> str:
        """Return the stored content_hash for a file, or '' if not present."""
        if not os.path.exists(db_path):
            return ''
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT content_hash FROM cgdb_files WHERE path = ?",
                (file_path,)
            ).fetchone()
            return row[0] if row else ''
        except sqlite3.OperationalError:
            return ''
        finally:
            conn.close()


def compute_affected_tus(changed_files: List[str],
                         source_root: str) -> List[str]:
    """Convenience wrapper: compute affected TUs for a set of changed files.

    Constructs an IncrementalSync, builds the include graph, and returns
    the transitive closure of affected TUs.
    """
    sync = IncrementalSync(source_root)
    return sync.compute_affected_tus(changed_files)
