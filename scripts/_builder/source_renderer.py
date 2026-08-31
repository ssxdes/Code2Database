"""Source renderer + sha256 character-level consistency check.

Implements design-report §11.6 (Source code reconstruction and consistency):

  1. Render source from DB tokens table (sorted by file_id, line, col).
     Each token's `preceding_whitespace` is emitted before its `spelling`.
  2. Append `trailing_whitespace` from `source_files_meta`.
  3. Honor BOM / line-ending (CRLF/LF/CR) from `source_files_meta`.
  4. sha256 the rendered bytes and compare against `source_files_meta.disk_sha256`.
  5. On mismatch, record into `alignment_errors` and surface to the caller.

The renderer is the foundation of the design-report "DB ⇄ 文本双向可逆"
principle. When LLM edits the DB (via edit_token / insert_token / delete_token
/ insert_node_after / etc.), the write-back pipeline renders the new source,
runs sha256 against the on-disk file, and refuses to commit if mismatched
(unless the on-disk file is also being rewritten as part of the transaction).
"""
from __future__ import annotations

import sqlite3
import hashlib
from dataclasses import dataclass
from typing import Optional


@dataclass
class RenderResult:
    """Outcome of a render_source() call."""
    content: bytes
    sha256: str
    matches_disk: bool
    disk_sha256: Optional[str]
    file_id: int
    path: Optional[str]
    error: Optional[str] = None
    token_count: int = 0


@dataclass
class ConsistencyResult:
    """Outcome of a verify_consistency() call."""
    file_id: int
    path: Optional[str]
    db_sha256: str
    disk_sha256: Optional[str]
    ok: bool
    diff: Optional[str] = None
    error_id: Optional[int] = None  # row id in alignment_errors if mismatch


class SourceRenderer:
    """Render source code from the DB tokens table.

    This is the inverse of the L1 token-ingest pipeline. Given a file_id,
    it walks the `tokens` table ordered by (line, col) [or seq], emits
    `preceding_whitespace` + `spelling` for each token, appends
    `trailing_whitespace` from `source_files_meta`, and applies BOM /
    line-ending normalization.

    If the `tokens` table is empty for the file (e.g., the file was scanned
    via the legacy tree-sitter path that doesn't populate tokens), the
    renderer falls back to reading the file from disk and returns
    `matches_disk=True` trivially (no DB-side state to compare).
    """

    def __init__(self, conn: sqlite3.Connection, source_root: str = ""):
        self.conn = conn
        self.source_root = source_root

    def _resolve_path(self, file_path: str) -> str:
        """Resolve a possibly-relative file path using source_root.

        Mirrors _builder.l1_ingest._resolve_source_path logic:
          1. If file_path exists as-is, use it.
          2. If source_root given and file_path is relative,
             try os.path.join(source_root, file_path).
          3. If source_root given and file_path is absolute (source tree
             was renamed), try joining source_root with the tail.
          4. Return original path as fallback (caller's open() will raise).
        """
        if not file_path:
            return file_path
        import os
        if os.path.exists(file_path):
            return file_path
        if not self.source_root:
            return file_path
        if not os.path.isabs(file_path):
            import os
            cand = os.path.join(self.source_root, file_path)
            if os.path.exists(cand):
                return cand
        else:
            parts = file_path.replace("\\", "/").split("/")
            for start in range(1, len(parts)):
                tail = os.path.join(self.source_root, *parts[start:])
                if os.path.exists(tail):
                    return tail
        return file_path

    def render(self, file_id: int) -> RenderResult:
        """Render the source for `file_id` from the tokens table.

        Returns a RenderResult with the rendered bytes and sha256.
        """
        # Look up file metadata
        row = self.conn.execute(
            "SELECT path, encoding, line_ending, has_bom, trailing_whitespace, "
            "disk_sha256 FROM cgdb_files f LEFT JOIN source_files_meta m "
            "ON m.file_id = f.id WHERE f.id = ?",
            (file_id,)
        ).fetchone()
        if row is None:
            return RenderResult(
                content=b"", sha256=hashlib.sha256(b"").hexdigest(),
                matches_disk=False, disk_sha256=None, file_id=file_id,
                path=None, error=f"file_id {file_id} not found"
            )
        path, encoding, line_ending, has_bom, trailing_ws, disk_sha256 = row
        encoding = encoding or "utf-8"
        line_ending = line_ending or "LF"
        has_bom = bool(has_bom)

        # Resolve relative paths using source_root.
        # cgdb_files.path may be relative (e.g., "crypto/xcbc.c"), and
        # open(path, "rb") will fail unless CWD happens to be the source
        # root. _resolve_source_path joins with source_root when the path
        # is relative and source_root is provided.
        resolved_path = self._resolve_path(path)

        # Fetch tokens ordered by seq (preserves source order)
        token_rows = self.conn.execute(
            "SELECT seq, preceding_whitespace, spelling FROM tokens "
            "WHERE file_id = ? ORDER BY seq",
            (file_id,)
        ).fetchall()

        if not token_rows:
            # No tokens in DB — fall back to reading the file from disk
            try:
                with open(resolved_path, "rb") as f:
                    content = f.read()
                sha = hashlib.sha256(content).hexdigest()
                return RenderResult(
                    content=content, sha256=sha, matches_disk=True,
                    disk_sha256=sha, file_id=file_id, path=resolved_path,
                    token_count=0
                )
            except (OSError, FileNotFoundError) as exc:
                return RenderResult(
                    content=b"", sha256=hashlib.sha256(b"").hexdigest(),
                    matches_disk=False, disk_sha256=None, file_id=file_id,
                    path=resolved_path, error=f"no tokens and cannot read disk: {exc}"
                )

        # Build the rendered string
        parts: list[str] = []
        for _seq, preceding_ws, spelling in token_rows:
            if preceding_ws:
                parts.append(preceding_ws)
            if spelling is not None:
                parts.append(spelling)
        if trailing_ws:
            parts.append(trailing_ws)

        text = "".join(parts)

        # Normalize line endings per source_files_meta
        # First, normalize any \r\n or \r to \n, then apply target ending
        text_normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        if line_ending == "CRLF":
            text_normalized = text_normalized.replace("\n", "\r\n")
        elif line_ending == "CR":
            text_normalized = text_normalized.replace("\n", "\r")
        # LF: no further change

        # Encode
        try:
            content = text_normalized.encode(encoding)
        except (LookupError, UnicodeEncodeError):
            content = text_normalized.encode("utf-8", errors="replace")
            encoding = "utf-8"

        # Prepend BOM if needed
        if has_bom and encoding.lower() in ("utf-8", "utf-8-sig"):
            if not content.startswith(b"\xef\xbb\xbf"):
                content = b"\xef\xbb\xbf" + content

        sha = hashlib.sha256(content).hexdigest()
        matches = (disk_sha256 is not None and sha == disk_sha256)

        return RenderResult(
            content=content, sha256=sha, matches_disk=matches,
            disk_sha256=disk_sha256, file_id=file_id, path=resolved_path,
            token_count=len(token_rows)
        )

    def render_to_string(self, file_id: int) -> str:
        """Convenience: render and decode to string."""
        result = self.render(file_id)
        if result.error:
            return ""
        try:
            return result.content.decode("utf-8")
        except UnicodeDecodeError:
            return result.content.decode("utf-8", errors="replace")

    def verify_consistency(self, file_id: int) -> ConsistencyResult:
        """Render source from DB, compute sha256, compare against disk file sha256.

        On mismatch:
        - Record an entry in `alignment_errors` (layer='L1', table_name='tokens',
          error_kind='sha256_mismatch').
        - Return ConsistencyResult with ok=False and the diff hint.

        On match:
        - Update `source_files_meta.rendered_sha256` and `last_verified_at`.
        - Return ConsistencyResult with ok=True.
        """
        result = self.render(file_id)
        if result.error:
            return ConsistencyResult(
                file_id=file_id, path=result.path,
                db_sha256=result.sha256, disk_sha256=None,
                ok=False, diff=result.error
            )

        # Read disk file sha256 (re-read fresh, not from source_files_meta)
        disk_sha: Optional[str] = None
        if result.path:
            try:
                with open(result.path, "rb") as f:
                    disk_bytes = f.read()
                disk_sha = hashlib.sha256(disk_bytes).hexdigest()
            except (OSError, FileNotFoundError):
                disk_sha = None

        ok = (disk_sha is not None and disk_sha == result.sha256)

        if ok:
            # Update source_files_meta with rendered_sha256 + last_verified_at
            import time
            now = int(time.time())
            self.conn.execute(
                "UPDATE source_files_meta SET rendered_sha256 = ?, "
                "disk_sha256 = COALESCE(disk_sha256, ?), last_verified_at = ? "
                "WHERE file_id = ?",
                (result.sha256, disk_sha, now, file_id)
            )
            self.conn.commit()
            return ConsistencyResult(
                file_id=file_id, path=result.path,
                db_sha256=result.sha256, disk_sha256=disk_sha, ok=True
            )

        # Mismatch — record in alignment_errors
        import time
        now = int(time.time())
        diff_hint = None
        if disk_sha is None:
            diff_hint = f"disk file missing or unreadable: {result.path}"
        else:
            # Simple diff hint: first differing byte index
            try:
                with open(result.path, "rb") as f:
                    disk_bytes = f.read()
                rendered = result.content
                diff_idx = -1
                for i in range(min(len(disk_bytes), len(rendered))):
                    if disk_bytes[i] != rendered[i]:
                        diff_idx = i
                        break
                if diff_idx < 0 and len(disk_bytes) != len(rendered):
                    diff_idx = min(len(disk_bytes), len(rendered))
                diff_hint = (
                    f"first diff at byte {diff_idx}; "
                    f"disk_len={len(disk_bytes)} rendered_len={len(rendered)}"
                )
            except OSError:
                diff_hint = "cannot read disk for diff"

        cur = self.conn.execute(
            "INSERT INTO alignment_errors "
            "(layer, table_name, row_id, error_kind, raw_payload, detected_at, resolved) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (
                "L1", "tokens", file_id, "sha256_mismatch",
                f"db_sha256={result.sha256} disk_sha256={disk_sha} {diff_hint}",
                now
            )
        )
        self.conn.commit()
        return ConsistencyResult(
            file_id=file_id, path=result.path,
            db_sha256=result.sha256, disk_sha256=disk_sha,
            ok=False, diff=diff_hint, error_id=cur.lastrowid
        )

    def verify_all_files(self) -> list[ConsistencyResult]:
        """Verify consistency for every file in source_files_meta.

        Returns a list of ConsistencyResult (one per file). Mismatches are
        recorded in alignment_errors. Use this after a bulk L1 token ingest
        to detect any files where DB→file rendering diverges from disk.
        """
        file_ids = [
            row[0] for row in self.conn.execute(
                "SELECT file_id FROM source_files_meta ORDER BY file_id"
            )
        ]
        results = []
        for fid in file_ids:
            results.append(self.verify_consistency(fid))
        return results

    # ------------------------------------------------------------------
    # Helpers for the write-back pipeline
    # ------------------------------------------------------------------

    def update_disk_sha256(self, file_id: int, new_sha: Optional[str] = None) -> None:
        """Refresh source_files_meta.disk_sha256 from the current disk file.

        Called after a write-back transaction has successfully written the
        rendered source to disk, so subsequent verify_consistency() calls
        will pass.
        """
        row = self.conn.execute(
            "SELECT path FROM cgdb_files WHERE id = ?", (file_id,)
        ).fetchone()
        if row is None or not row[0]:
            return
        path = row[0]
        if new_sha is None:
            try:
                with open(path, "rb") as f:
                    new_sha = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                return
        import time
        now = int(time.time())
        self.conn.execute(
            "UPDATE source_files_meta SET disk_sha256 = ?, "
            "rendered_sha256 = NULL, last_verified_at = ? WHERE file_id = ?",
            (new_sha, now, file_id)
        )
        self.conn.commit()


def render_source(conn: sqlite3.Connection, file_id: int,
                   source_root: str = "") -> RenderResult:
    """Module-level convenience: render source for a file_id."""
    return SourceRenderer(conn, source_root=source_root).render(file_id)


def verify_consistency(conn: sqlite3.Connection, file_id: int,
                       source_root: str = "") -> ConsistencyResult:
    """Module-level convenience: verify consistency for a file_id."""
    return SourceRenderer(conn, source_root=source_root).verify_consistency(file_id)


def verify_all_files(conn: sqlite3.Connection,
                     source_root: str = "") -> list[ConsistencyResult]:
    """Module-level convenience: verify consistency for all files."""
    return SourceRenderer(conn, source_root=source_root).verify_all_files()


def compute_sha256(path: str) -> Optional[str]:
    """Compute sha256 of a file on disk. Returns None on error."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None
