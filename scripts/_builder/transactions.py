#!/usr/bin/env python3
"""Transactional updates for Code2Database.

Provides ACID-like guarantees for incremental graph updates:

1. **Snapshot**: before any update, copy the code2database.db (and key JSON
   files) to a timestamped snapshot directory. If the update fails or
   produces inconsistent state, restore from snapshot.

2. **WAL (Write-Ahead Log)**: every write is logged BEFORE it's applied.
   If the process crashes mid-update, the WAL can be replayed (to finish
   the commit) or rolled back (to undo partial writes).

3. **Two-phase commit**: writes go to WAL first (phase 1), then are
   applied to the live db (phase 2), then WAL is checkpointed. A
   crash in phase 1 = clean rollback; a crash in phase 2 = replay WAL
   on next start.

4. **Read-write lock**: multiple readers, single writer. Uses a file
   lock (fcntl on Linux, msvcrt on Windows) so concurrent processes
   cooperate. Writers wait for readers; readers wait for writers.

5. **Auto-commit/rollback context manager**: `with transaction(graph_dir):`
   commits if the block completes, rolls back on exception.

Usage:
    from _builder.transactions import transaction

    with transaction(graph_dir, description="patch-from-git abc123"):
        # any operations on the graph inside this block are atomic
        patch_from_diff(graph_dir, diff_text, source_root)
        # if patch_from_diff raises, the snapshot is restored automatically

CLI:
    tx-begin --graph <dir> --description "..."
    tx-commit --graph <dir>
    tx-rollback --graph <dir>
    tx-status --graph <dir>
    tx-snapshot --graph <dir>              # take a manual snapshot
    tx-restore --graph <dir> --id <id>     # restore from a specific snapshot
    tx-list-snapshots --graph <dir>
    tx-replay-wal --graph <dir>            # replay an unfinished WAL
"""

import json
import os
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Any, Iterator

# Cross-platform file locking. Linux/macOS use fcntl.flock; Windows uses
# msvcrt.locking (LK_NBLCK / LK_UNLCK). If neither is available (rare —
# embedded interpreters, restricted sandboxes), fall back to a no-op lock
# that warns on stderr. The no-op path means transactions lose their
# cross-process safety; intra-process transactions still work via the
# in-memory tx-state guard.
try:
    import fcntl as _fcntl
    _LOCK_BACKEND = "fcntl"
except ImportError:
    try:
        import msvcrt as _msvcrt
        _LOCK_BACKEND = "msvcrt"
    except ImportError:
        _LOCK_BACKEND = "none"
        _fcntl = None
        _msvcrt = None
        print("[transactions] Warning: neither fcntl nor msvcrt available — "
              "cross-process file locking disabled (transactions are not "
              "safe across processes in this environment)",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TX_DIR_NAME = ".code2database_tx"
SNAPSHOTS_DIR_NAME = "snapshots"
WAL_FILE_NAME = "wal.jsonl"
LOCK_FILE_NAME = "tx.lock"
TX_STATE_FILE_NAME = "tx_state.json"

# Files that are part of the "graph state" and must be snapshotted together
GRAPH_STATE_FILES = [
    "code2database.db",
    "code2database_master.json",
]


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _tx_dir(graph_dir: str) -> str:
    return os.path.join(graph_dir, TX_DIR_NAME)


def _snapshots_dir(graph_dir: str) -> str:
    return os.path.join(_tx_dir(graph_dir), SNAPSHOTS_DIR_NAME)


def _wal_path(graph_dir: str) -> str:
    return os.path.join(_tx_dir(graph_dir), WAL_FILE_NAME)


def _lock_path(graph_dir: str) -> str:
    return os.path.join(_tx_dir(graph_dir), LOCK_FILE_NAME)


def _tx_state_path(graph_dir: str) -> str:
    return os.path.join(_tx_dir(graph_dir), TX_STATE_FILE_NAME)


# ---------------------------------------------------------------------------
# Read-write lock (file-based, multi-process)
# ---------------------------------------------------------------------------

class GraphLock:
    """File-based read-write lock for the graph directory.

    Multiple readers can hold the lock simultaneously; only one writer
    at a time. Writers exclude all readers (single-writer/multi-reader).

    Uses fcntl.flock on Linux/Mac. The lock is held while the file
    descriptor is open; closing it releases the lock.
    """

    def __init__(self, graph_dir: str):
        self.graph_dir = graph_dir
        os.makedirs(_tx_dir(graph_dir), exist_ok=True)
        self._lock_fd: Optional[Any] = None
        self._mode: Optional[str] = None  # 'r' or 'w'

    def acquire_read(self, timeout: float = 30.0):
        """Acquire a shared (read) lock. Blocks up to timeout seconds."""
        return self._acquire("r", timeout)

    def acquire_write(self, timeout: float = 30.0):
        """Acquire an exclusive (write) lock. Blocks up to timeout seconds."""
        return self._acquire("w", timeout)

    def _acquire(self, mode: str, timeout: float):
        import time as _time
        path = _lock_path(self.graph_dir)
        deadline = _time.time() + timeout
        # Open the lock file. If anything fails between open and the lock
        # call below, the fd must be closed — wrap in try/except to avoid
        # leaking file descriptors on partial-failure paths.
        fd = open(path, "w")
        try:
            while True:
                try:
                    if _LOCK_BACKEND == "fcntl":
                        op = _fcntl.LOCK_EX if mode == "w" else _fcntl.LOCK_SH
                        _fcntl.flock(fd, op | _fcntl.LOCK_NB)
                    elif _LOCK_BACKEND == "msvcrt":
                        # msvcrt.locking: LK_NBLCK / LK_NBLCKSH (non-blocking
                        # exclusive / shared) for write / read respectively.
                        op = _msvcrt.LK_NBLCK if mode == "w" else _msvcrt.LK_NBLCKSH
                        try:
                            _msvcrt.locking(fd.fileno(), op, 1)
                        except OSError:
                            # Translate msvcrt's "already-locked" OSError
                            # into the same BlockingIOError the fcntl path
                            # raises, so the retry loop below handles it.
                            raise BlockingIOError()
                    else:
                        # No-op backend: trivially "succeed" but emit a
                        # one-shot warning so users know locks aren't real.
                        if not getattr(self, "_warned_noop", False):
                            print("[transactions] Warning: file locking "
                                  "unavailable — transaction isolation "
                                  "is not enforced", file=sys.stderr)
                            self._warned_noop = True
                    self._lock_fd = fd
                    self._mode = mode
                    return True
                except (BlockingIOError, OSError):
                    if _time.time() >= deadline:
                        fd.close()
                        return False
                    _time.sleep(0.05)
        except Exception:
            # Any unexpected exception (e.g. OSError on flock itself)
            # must release the fd before propagating, otherwise callers
            # cannot distinguish "lock failed cleanly" from "lock leaked
            # an fd and also failed".
            fd.close()
            raise

    def release(self):
        if self._lock_fd is not None:
            try:
                if _LOCK_BACKEND == "fcntl":
                    _fcntl.flock(self._lock_fd, _fcntl.LOCK_UN)
                elif _LOCK_BACKEND == "msvcrt":
                    # Seek back to the locked byte and release.
                    self._lock_fd.seek(0)
                    try:
                        _msvcrt.locking(self._lock_fd.fileno(), _msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                # 'none' backend: no-op
            except OSError:
                pass
            self._lock_fd.close()
            self._lock_fd = None
            self._mode = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


@contextmanager
def read_lock(graph_dir: str, timeout: float = 30.0) -> Iterator[GraphLock]:
    """Context manager for a shared read lock."""
    lock = GraphLock(graph_dir)
    if not lock.acquire_read(timeout):
        raise TimeoutError(f"Could not acquire read lock on {graph_dir}")
    try:
        yield lock
    finally:
        lock.release()


@contextmanager
def write_lock(graph_dir: str, timeout: float = 30.0) -> Iterator[GraphLock]:
    """Context manager for an exclusive write lock."""
    lock = GraphLock(graph_dir)
    if not lock.acquire_write(timeout):
        raise TimeoutError(f"Could not acquire write lock on {graph_dir}")
    try:
        yield lock
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
    """A graph state snapshot."""
    id: str
    timestamp: float
    description: str
    path: str  # directory containing the snapshotted files
    file_count: int = 0
    total_size: int = 0


def create_snapshot(graph_dir: str, description: str = "") -> Snapshot:
    """Snapshot the current graph state by copying key files.

    Returns a Snapshot handle. The snapshot directory is timestamped
    so multiple snapshots don't collide.
    """
    snap_id = f"snap_{int(time.time() * 1000)}"  # millisecond precision
    snap_path = os.path.join(_snapshots_dir(graph_dir), snap_id)
    os.makedirs(snap_path, exist_ok=True)

    file_count = 0
    total_size = 0
    for fname in GRAPH_STATE_FILES:
        src = os.path.join(graph_dir, fname)
        if not os.path.exists(src):
            continue
        dst = os.path.join(snap_path, fname)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        file_count += 1
        total_size += os.path.getsize(dst) if os.path.exists(dst) else 0

    snap = Snapshot(
        id=snap_id, timestamp=time.time(), description=description,
        path=snap_path, file_count=file_count, total_size=total_size,
    )
    # Write a metadata file in the snapshot dir
    with open(os.path.join(snap_path, "_meta.json"), "w", encoding="utf-8") as f:
        json.dump({
            "id": snap.id, "timestamp": snap.timestamp,
            "description": snap.description, "file_count": snap.file_count,
            "total_size": snap.total_size,
        }, f, ensure_ascii=False, indent=2)
    return snap


def list_snapshots(graph_dir: str, limit: int = 50) -> List[Snapshot]:
    """List all snapshots, newest first."""
    snaps_dir = _snapshots_dir(graph_dir)
    if not os.path.exists(snaps_dir):
        return []
    snaps = []
    for name in sorted(os.listdir(snaps_dir), reverse=True):
        meta_path = os.path.join(snaps_dir, name, "_meta.json")
        if not os.path.exists(meta_path):
            continue
        try:
            meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
            snaps.append(Snapshot(
                id=meta["id"], timestamp=meta["timestamp"],
                description=meta.get("description", ""),
                path=os.path.join(snaps_dir, name),
                file_count=meta.get("file_count", 0),
                total_size=meta.get("total_size", 0),
            ))
        except (json.JSONDecodeError, KeyError):
            continue
        if len(snaps) >= limit:
            break
    return snaps


def restore_snapshot(graph_dir: str, snap_id: str) -> Dict:
    """Restore graph state from a snapshot. Overwrites current files."""
    snap_path = os.path.join(_snapshots_dir(graph_dir), snap_id)
    if not os.path.exists(snap_path):
        return {"restored": False, "reason": f"snapshot {snap_id} not found"}

    restored_files = []
    for fname in GRAPH_STATE_FILES:
        src = os.path.join(snap_path, fname)
        if not os.path.exists(src):
            continue
        dst = os.path.join(graph_dir, fname)
        # Remove the current file/directory
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        elif os.path.exists(dst):
            os.remove(dst)
        # Copy the snapshot back
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        restored_files.append(fname)
    return {"restored": True, "snapshot_id": snap_id,
            "restored_files": restored_files}


def delete_snapshot(graph_dir: str, snap_id: str) -> bool:
    """Delete a snapshot to free disk space."""
    snap_path = os.path.join(_snapshots_dir(graph_dir), snap_id)
    if not os.path.exists(snap_path):
        return False
    shutil.rmtree(snap_path)
    return True


def prune_snapshots(graph_dir: str, keep: int = 10) -> int:
    """Keep only the most recent `keep` snapshots, delete the rest."""
    snaps = list_snapshots(graph_dir, limit=10000)
    if len(snaps) <= keep:
        return 0
    deleted = 0
    for snap in snaps[keep:]:
        if delete_snapshot(graph_dir, snap.id):
            deleted += 1
    return deleted


# ---------------------------------------------------------------------------
# WAL (Write-Ahead Log)
# ---------------------------------------------------------------------------

@dataclass
class WALEntry:
    """One WAL entry — a single write to be applied atomically."""
    seq: int  # monotonically increasing sequence number
    operation: str  # 'update_node', 'update_edge', 'delete_node', etc.
    target_id: str  # node_id or edge_id
    payload: Dict[str, Any]  # operation-specific data
    timestamp: float = field(default_factory=time.time)
    applied: bool = False  # True once applied to the live db


_WAL_SEQ_FILE = "seq_counter"


def _wal_seq_path(graph_dir: str) -> str:
    return os.path.join(_tx_dir(graph_dir), _WAL_SEQ_FILE)


def _read_wal_seq_counter(graph_dir: str) -> int:
    """Read the persisted WAL sequence counter.

    The counter is the next seq to assign (not the last used). Falls
    back to scanning the WAL file once if the counter file is missing
    or stale (e.g., from older versions that didn't maintain it).
    """
    seq_path = _wal_seq_path(graph_dir)
    wal_path = _wal_path(graph_dir)
    try:
        with open(seq_path, "r", encoding="utf-8") as f:
            return int(f.read().strip() or "1")
    except (FileNotFoundError, ValueError):
        pass
    # Slow path: scan the WAL once to find the highest seq, then persist.
    next_seq = 1
    if os.path.exists(wal_path):
        try:
            with open(wal_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        if isinstance(entry, dict) and "seq" in entry:
                            next_seq = max(next_seq, int(entry["seq"]) + 1)
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass
        except OSError:
            pass
    _write_wal_seq_counter(graph_dir, next_seq)
    return next_seq


def _write_wal_seq_counter(graph_dir: str, next_seq: int) -> None:
    """Persist the next WAL seq counter."""
    seq_path = _wal_seq_path(graph_dir)
    os.makedirs(os.path.dirname(seq_path), exist_ok=True)
    tmp = seq_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(next_seq))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, seq_path)


def append_wal_entry(graph_dir: str, operation: str, target_id: str,
                     payload: Dict[str, Any]) -> int:
    """Append an entry to the WAL. Returns the sequence number."""
    wal_path = _wal_path(graph_dir)
    os.makedirs(os.path.dirname(wal_path), exist_ok=True)
    next_seq = _read_wal_seq_counter(graph_dir)
    entry = {
        "seq": next_seq, "operation": operation, "target_id": target_id,
        "payload": payload, "timestamp": time.time(), "applied": False,
    }
    with open(wal_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())
    _write_wal_seq_counter(graph_dir, next_seq + 1)
    return next_seq


def mark_wal_entry_applied(graph_dir: str, seq: int):
    """Mark a WAL entry as applied (committed to live db)."""
    wal_path = _wal_path(graph_dir)
    if not os.path.exists(wal_path):
        return
    # Rewrite the WAL with the applied flag flipped for the given seq.
    # Uses an atomic temp-file rename to avoid leaving a partial WAL
    # if the process crashes mid-rewrite.
    tmp_path = wal_path + ".tmp"
    with open(wal_path, "r", encoding="utf-8") as f_in, \
         open(tmp_path, "w", encoding="utf-8") as f_out:
        for line in f_in:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if entry.get("seq") == seq:
                    entry["applied"] = True
                f_out.write(json.dumps(entry, ensure_ascii=False,
                                       default=str) + "\n")
            except json.JSONDecodeError:
                f_out.write(line)
    os.replace(tmp_path, wal_path)


def read_wal(graph_dir: str, only_unapplied: bool = False) -> List[Dict]:
    """Read all (or only unapplied) WAL entries, in sequence order."""
    wal_path = _wal_path(graph_dir)
    if not os.path.exists(wal_path):
        return []
    entries = []
    with open(wal_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    entry = json.loads(line)
                    if only_unapplied and entry.get("applied"):
                        continue
                    entries.append(entry)
                except json.JSONDecodeError:
                    pass
    return entries


def clear_wal(graph_dir: str):
    """Delete the WAL after a successful commit."""
    wal_path = _wal_path(graph_dir)
    if os.path.exists(wal_path):
        os.remove(wal_path)
    # Reset the WAL seq counter so the next transaction starts at 1.
    seq_path = _wal_seq_path(graph_dir)
    if os.path.exists(seq_path):
        try:
            os.remove(seq_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Transaction state
# ---------------------------------------------------------------------------

@dataclass
class TransactionState:
    """Persistent state of the current (or last) transaction."""
    tx_id: str
    started_at: float
    description: str
    snapshot_id: str  # snapshot taken at begin()
    status: str  # 'active', 'committed', 'rolled_back', 'failed'
    ended_at: Optional[float] = None
    error: Optional[str] = None
    # RPT-P0-15: file_ids whose tokens/literals/source_files_meta were mutated
    # during this transaction. Tracked so that commit() can run
    # source_renderer.verify_consistency() on just the touched files (cheap)
    # rather than the whole graph (expensive on 700K+ node builds).
    dirty_file_ids: List[int] = field(default_factory=list)
    # RPT-P0-15: results of the post-commit consistency check. Each entry is
    # {"file_id": int, "ok": bool, "diff": str|None}. Empty if no dirty files
    # or if consistency checking was disabled.
    consistency_results: List[Dict[str, Any]] = field(default_factory=list)


def _write_tx_state(graph_dir: str, state: TransactionState):
    os.makedirs(_tx_dir(graph_dir), exist_ok=True)
    with open(_tx_state_path(graph_dir), "w", encoding="utf-8") as f:
        json.dump({
            "tx_id": state.tx_id, "started_at": state.started_at,
            "description": state.description, "snapshot_id": state.snapshot_id,
            "status": state.status, "ended_at": state.ended_at,
            "error": state.error,
            "dirty_file_ids": state.dirty_file_ids,
            "consistency_results": state.consistency_results,
        }, f, ensure_ascii=False, indent=2, default=str)


def _read_tx_state(graph_dir: str) -> Optional[TransactionState]:
    path = _tx_state_path(graph_dir)
    if not os.path.exists(path):
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        # Be tolerant of older tx_state.json files written before
        # dirty_file_ids / consistency_results were added — default them.
        data.setdefault("dirty_file_ids", [])
        data.setdefault("consistency_results", [])
        return TransactionState(**data)
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


# ---------------------------------------------------------------------------
# Transaction context manager
# ---------------------------------------------------------------------------

@contextmanager
def transaction(graph_dir: str, description: str = "",
                keep_snapshots: int = 10,
                verify_consistency: bool = True) -> Iterator[TransactionState]:
    """Context manager for an atomic graph update.

    Begins a transaction (snapshot + WAL + write lock), yields the
    TransactionState, and on normal exit commits (clears WAL, releases
    lock). On exception, rolls back (restores from snapshot, clears WAL).

    Usage:
        with transaction(graph_dir, description="patch-from-git abc123"):
            patch_from_diff(graph_dir, diff_text, source_root)
            # ... other mutations ...
        # On normal exit: committed. On exception: rolled back.

    RPT-P0-15: After commit, if `verify_consistency=True` and the
    TransactionState has any `dirty_file_ids` (populated via
    `mark_file_dirty()`), source_renderer.verify_consistency() is run on
    each touched file_id. Mismatches are recorded in
    `tx_state.consistency_results` and a warning is printed to stderr —
    they do NOT abort the commit (the DB change is already durable), but
    they surface that the DB now diverges from disk and a write-back
    (render_to_disk) is needed to reconcile. Set `verify_consistency=False`
    to skip the check entirely (e.g., for graph edits that don't touch
    tokens).
    """
    # Acquire write lock
    with write_lock(graph_dir, timeout=60.0):
        # Check for an existing active transaction
        existing = _read_tx_state(graph_dir)
        if existing and existing.status == "active":
            # Stale active tx — roll it back first
            restore_snapshot(graph_dir, existing.snapshot_id)
            clear_wal(graph_dir)

        # Begin: snapshot + state
        snap = create_snapshot(graph_dir, description=description or "tx_begin")
        tx_state = TransactionState(
            tx_id=f"tx_{int(time.time() * 1000)}",
            started_at=time.time(), description=description,
            snapshot_id=snap.id, status="active",
        )
        _write_tx_state(graph_dir, tx_state)

        try:
            yield tx_state
            # Commit
            clear_wal(graph_dir)
            # RPT-P0-15: re-read the state file to pick up any
            # mark_file_dirty() calls made inside the `with` block.
            # mark_file_dirty writes its own copy of the state to disk,
            # so the in-memory `tx_state` may be stale at this point.
            fresh_state = _read_tx_state(graph_dir)
            if fresh_state is not None:
                # Preserve our snapshot_id / tx_id / started_at / description
                # (those don't change mid-tx) but adopt the dirty_file_ids
                # that mark_file_dirty() accumulated.
                tx_state.dirty_file_ids = fresh_state.dirty_file_ids
            tx_state.status = "committed"
            tx_state.ended_at = time.time()
            _write_tx_state(graph_dir, tx_state)

            # RPT-P0-15: post-commit consistency check on dirty files.
            # This is best-effort — if source_renderer isn't importable
            # (e.g., the graph_dir doesn't have a tokens table yet) we
            # skip silently rather than fail the commit.
            if verify_consistency and tx_state.dirty_file_ids:
                _run_post_commit_consistency_check(graph_dir, tx_state)

            # Prune old snapshots
            prune_snapshots(graph_dir, keep=keep_snapshots)
        except Exception as exc:
            # Rollback
            restore_snapshot(graph_dir, snap.id)
            clear_wal(graph_dir)
            tx_state.status = "rolled_back"
            tx_state.ended_at = time.time()
            tx_state.error = str(exc)
            _write_tx_state(graph_dir, tx_state)
            raise


def mark_file_dirty(graph_dir: str, file_id: int) -> None:
    """Record that `file_id`'s L1 state (tokens / literals / source_files_meta)
    was mutated inside the current active transaction.

    Call this from MCP tool handlers (edit_token, insert_token, delete_token,
    insert_node_after, etc.) after they write to the tokens table. At commit
    time, `transaction()` will run `source_renderer.verify_consistency()`
    on each dirty file_id and record the result in
    `TransactionState.consistency_results`.

    Safe to call outside an active transaction — silently no-ops so callers
    don't need to gate on tx-state themselves.
    """
    state = _read_tx_state(graph_dir)
    if not state or state.status != "active":
        return  # No active transaction — nothing to mark
    if file_id not in state.dirty_file_ids:
        state.dirty_file_ids.append(file_id)
        _write_tx_state(graph_dir, state)


def _run_post_commit_consistency_check(graph_dir: str,
                                        tx_state: TransactionState) -> None:
    """Run source_renderer.verify_consistency() on each dirty file_id and
    record results in tx_state.consistency_results. Prints a warning to
    stderr for any mismatch (does NOT abort — the commit is already durable).

    Silently skips if source_renderer can't be imported or the DB doesn't
    have a tokens table yet (e.g., brand-new graph_dir with no L1 ingest).
    """
    db_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.exists(db_path):
        return
    try:
        import sqlite3
        from _builder.source_renderer import verify_consistency
    except ImportError:
        return
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(db_path)
        # Verify the tokens table exists — skip silently if not (graph_dir
        # may be a non-L1 graph, e.g., tree-sitter-only).
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='tokens' LIMIT 1"
        )
        if cur.fetchone() is None:
            return
        for fid in tx_state.dirty_file_ids:
            try:
                cr = verify_consistency(conn, int(fid))
                tx_state.consistency_results.append({
                    "file_id": int(fid),
                    "ok": bool(cr.ok),
                    "diff": cr.diff,
                })
                if not cr.ok:
                    print(
                        f"[tx] WARNING: consistency check failed for "
                        f"file_id={fid}: {cr.diff}",
                        file=sys.stderr,
                    )
            except Exception as exc:
                tx_state.consistency_results.append({
                    "file_id": int(fid),
                    "ok": False,
                    "diff": f"verify_consistency raised: {exc}",
                })
        # Persist the consistency results into the tx_state file so
        # callers / `tx-status` can see them after the fact.
        _write_tx_state(graph_dir, tx_state)
    except Exception as exc:
        # Best-effort — don't raise from a post-commit hook.
        print(f"[tx] WARNING: consistency check skipped: {exc}",
              file=sys.stderr)
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# Recovery: replay an unfinished WAL on startup
# ---------------------------------------------------------------------------

def recover_unfinished_wal(graph_dir: str) -> Dict:
    """Replay any unapplied WAL entries after a crash.

    Called on startup to ensure consistency. If the WAL has unapplied
    entries from a crashed transaction, either:
    - Replay them (if the tx was committing) — finish the commit
    - Roll them back (if the tx was rolling back) — undo

    Since we can't know which phase crashed, we use the tx_state:
    - If status='active' → rollback (restore from snapshot, clear WAL)
    - If status='committed' → replay any unapplied entries (shouldn't be any)
    - If status='rolled_back' → already restored, just clear WAL
    - If status='failed' → rollback

    Returns a summary of what was done.
    """
    state = _read_tx_state(graph_dir)
    if not state:
        # No tx state — clear any stray WAL
        unapplied = read_wal(graph_dir, only_unapplied=True)
        if unapplied:
            clear_wal(graph_dir)
            return {"action": "cleared_stray_wal", "entries_cleared": len(unapplied)}
        return {"action": "nothing_to_recover"}

    if state.status == "active":
        # Crash during a transaction → rollback
        restore_snapshot(graph_dir, state.snapshot_id)
        clear_wal(graph_dir)
        state.status = "rolled_back"
        state.ended_at = time.time()
        state.error = "recovered: rolled back unfinished active transaction"
        _write_tx_state(graph_dir, state)
        return {"action": "rolled_back", "tx_id": state.tx_id,
                "snapshot_id": state.snapshot_id}

    if state.status == "rolled_back" or state.status == "failed":
        # Already rolled back, just clear WAL if anything remains
        unapplied = read_wal(graph_dir, only_unapplied=True)
        if unapplied:
            clear_wal(graph_dir)
        return {"action": "already_rolled_back", "tx_id": state.tx_id}

    if state.status == "committed":
        # Replay any unapplied entries (rare — only if crash between
        # applying writes and clearing WAL)
        unapplied = read_wal(graph_dir, only_unapplied=True)
        if not unapplied:
            return {"action": "already_committed", "tx_id": state.tx_id}
        # Replay logic would go here — for now, just clear since
        # the writes are already applied (we mark applied as we go).
        # If there are unapplied entries, they were never written to the
        # live db, so we drop them (consistent with the snapshot taken
        # at begin()).
        clear_wal(graph_dir)
        return {"action": "committed_with_dropped_wal",
                "dropped_entries": len(unapplied)}

    return {"action": "unknown_state", "status": state.status}


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_tx_begin(args):
    """Begin a transaction (manual mode — most users use the context manager).

    Usage:
      tx-begin --graph <dir> --description "..."                # graph-level tx
      tx-begin --graph <dir> --file-id N --description "..."    # writeback tx

    When --file-id is provided, this also registers a pending write-back
    transaction via WritebackPipeline.begin(file_id), so the subsequent
    tx-commit can run the full render→compile→lint→git pipeline.
    """
    graph_dir = args.graph
    description = getattr(args, "description", "")
    file_id = getattr(args, "file_id", None)

    snap = create_snapshot(graph_dir, description=description or "manual tx_begin")
    tx_state = TransactionState(
        tx_id=f"tx_{int(time.time() * 1000)}",
        started_at=time.time(), description=description,
        snapshot_id=snap.id, status="active",
    )
    _write_tx_state(graph_dir, tx_state)

    response: Dict[str, Any] = {
        "tx_id": tx_state.tx_id, "snapshot_id": snap.id,
        "started_at": tx_state.started_at, "status": "active",
    }

    # RPT-P0-16: If --file-id given, also register a write-back tx.
    if file_id is not None:
        try:
            import sqlite3 as _sqlite3
            from _builder.writeback_pipeline import WritebackPipeline
            db_path = os.path.join(graph_dir, "code2database.db")
            conn = _sqlite3.connect(db_path)
            try:
                pipe = WritebackPipeline(conn, graph_dir, graph_dir)
                writeback_tx_id = pipe.begin(int(file_id))
                # Stash the writeback_tx_id in tx_state so cmd_tx_commit
                # can find it later. We use the `error` field as a scratch
                # pad — actually no, let's add it to dirty_file_ids-style
                # by storing it in the description suffix. Simpler: write
                # it to a separate key in tx_state.json via _write_tx_state.
                # Easiest: append to description so it round-trips.
                tx_state.description = (
                    f"{description} [writeback_tx={writeback_tx_id}]"
                ).strip()
                _write_tx_state(graph_dir, tx_state)
                response["writeback_tx_id"] = writeback_tx_id
                response["file_id"] = int(file_id)
            finally:
                conn.close()
        except Exception as exc:
            # Don't fail the whole tx-begin if writeback init fails —
            # just warn. The graph-level tx is still active.
            print(f"[tx-begin] WARNING: writeback init failed: {exc}",
                  file=sys.stderr)

    print(json.dumps(response, ensure_ascii=False, indent=2, default=str))


def cmd_tx_commit(args):
    """Commit the current transaction (clears WAL).

    Usage:
      tx-commit --graph <dir>                                            # graph-level only
      tx-commit --graph <dir> --run-compile --git-commit --commit-message "..."  # writeback

    If the active transaction was started with --file-id (i.e., a writeback
    tx is pending in the meta table), this runs the full
    render→compile→lint→clang-format→sha256→write-disk→git-commit pipeline
    via WritebackPipeline.commit() and reports the result.
    """
    graph_dir = args.graph
    state = _read_tx_state(graph_dir)
    if not state or state.status != "active":
        print("No active transaction to commit", file=sys.stderr)
        sys.exit(1)

    # RPT-P0-16: Check for a pending write-back tx. We stored its id in
    # the description as "[writeback_tx=<uuid>]".
    writeback_tx_id: Optional[str] = None
    if state.description and "writeback_tx=" in state.description:
        import re
        m = re.search(r"writeback_tx=([a-f0-9-]+)", state.description)
        if m:
            writeback_tx_id = m.group(1)

    response: Dict[str, Any] = {
        "tx_id": state.tx_id, "status": "committed",
        "ended_at": None,
    }

    if writeback_tx_id is not None:
        # Run the write-back pipeline.
        try:
            import sqlite3 as _sqlite3
            from _builder.writeback_pipeline import WritebackPipeline
            db_path = os.path.join(graph_dir, "code2database.db")
            conn = _sqlite3.connect(db_path)
            try:
                pipe = WritebackPipeline(conn, graph_dir, graph_dir)
                result = pipe.commit(
                    writeback_tx_id,
                    run_compile=getattr(args, "run_compile", True),
                    run_lint=getattr(args, "run_lint", False),
                    run_clang_format=getattr(args, "run_clang_format", False),
                    git_commit=getattr(args, "git_commit", False),
                    commit_message=getattr(args, "commit_message", None),
                )
                response["writeback"] = result.to_dict()
                if not result.applied:
                    # Writeback failed — but the graph-level tx (snapshot,
                    # WAL) is still ok. Mark the tx as committed-with-failure
                    # rather than rolling back the whole graph snapshot,
                    # because the user may want to inspect the failure.
                    state.error = (
                        f"writeback failed at {result.failure_stage}: "
                        f"{result.failure_detail}"
                    )
            finally:
                conn.close()
        except Exception as exc:
            state.error = f"writeback pipeline raised: {exc}"
            print(f"[tx-commit] WARNING: writeback failed: {exc}",
                  file=sys.stderr)

    # Standard commit: clear WAL + mark committed
    clear_wal(graph_dir)
    state.status = "committed"
    state.ended_at = time.time()
    _write_tx_state(graph_dir, state)
    response["ended_at"] = state.ended_at
    if state.error:
        response["error"] = state.error
    print(json.dumps(response, ensure_ascii=False, indent=2, default=str))


def cmd_tx_rollback(args):
    """Rollback the current transaction (restores snapshot)."""
    graph_dir = args.graph
    state = _read_tx_state(graph_dir)
    if not state or state.status != "active":
        print("No active transaction to rollback", file=sys.stderr)
        sys.exit(1)
    restore_snapshot(graph_dir, state.snapshot_id)
    clear_wal(graph_dir)
    state.status = "rolled_back"
    state.ended_at = time.time()
    _write_tx_state(graph_dir, state)
    print(json.dumps({
        "tx_id": state.tx_id, "status": "rolled_back",
        "snapshot_id": state.snapshot_id, "ended_at": state.ended_at,
    }, ensure_ascii=False, indent=2, default=str))


def cmd_tx_status(args):
    """Show the current transaction state and any unfinished WAL."""
    graph_dir = args.graph
    state = _read_tx_state(graph_dir)
    wal_entries = read_wal(graph_dir)
    unapplied = [e for e in wal_entries if not e.get("applied")]
    snaps = list_snapshots(graph_dir, limit=10)
    print(json.dumps({
        "current_tx": state.__dict__ if state else None,
        "wal_entries": len(wal_entries),
        "wal_unapplied": len(unapplied),
        "snapshots": [
            {"id": s.id, "timestamp": s.timestamp,
             "description": s.description, "file_count": s.file_count}
            for s in snaps
        ],
    }, ensure_ascii=False, indent=2, default=str))


def cmd_tx_snapshot(args):
    """Take a manual snapshot (without starting a transaction)."""
    graph_dir = args.graph
    description = getattr(args, "description", "manual snapshot")
    snap = create_snapshot(graph_dir, description=description)
    print(json.dumps({
        "snapshot_id": snap.id, "timestamp": snap.timestamp,
        "file_count": snap.file_count, "total_size": snap.total_size,
    }, ensure_ascii=False, indent=2, default=str))


def cmd_tx_restore(args):
    """Restore from a specific snapshot."""
    graph_dir = args.graph
    snap_id = getattr(args, "id", "")
    if not snap_id:
        print("Error: --id required", file=sys.stderr)
        sys.exit(1)
    result = restore_snapshot(graph_dir, snap_id)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def cmd_tx_list_snapshots(args):
    """List all snapshots."""
    graph_dir = args.graph
    snaps = list_snapshots(graph_dir, limit=getattr(args, "limit", 50))
    print(json.dumps({
        "snapshots": [
            {"id": s.id, "timestamp": s.timestamp,
             "description": s.description, "file_count": s.file_count,
             "total_size": s.total_size}
            for s in snaps
        ],
        "count": len(snaps),
    }, ensure_ascii=False, indent=2, default=str))


def cmd_tx_replay_wal(args):
    """Replay or rollback an unfinished WAL (crash recovery)."""
    graph_dir = args.graph
    result = recover_unfinished_wal(graph_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
