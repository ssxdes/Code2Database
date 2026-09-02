#!/usr/bin/env python3
"""Background daemon for real-time file monitoring and auto-update.

The existing `watch` command is "watch + one-shot build" — it runs once and
exits. `install-hook` only fires on git commit, not on editor save. There's
no long-running daemon that:
- Monitors file changes in real-time (inotify/fsevents/watchman)
- Performs incremental scans (reusing patch-from-diff pipeline)
- Updates output files (CODE2DATABASE_SUMMARY.md, etc.) automatically
- Coordinates with manual updates (pause/resume) to avoid conflicts
- Reports freshness to LLM agents via shared status file + Unix socket

This module provides:

1. **Daemon class**: long-running process that monitors source files,
   batches changes, runs incremental scans in transactions, and updates
   output files. Uses ctypes-based inotify on Linux (zero dependency),
   falls back to polling on other platforms.

2. **State file**: <graph_dir>/.daemon_status.json with pid, status,
   last_sync_at, pending_events, stale_nodes.

3. **Unix socket API**: /tmp/code2database-daemon-<hash>.sock (hash of graph_dir) exposes
   - status: get current daemon state
   - force-refresh <path>: trigger immediate rescan of path
   - pause / resume: coordinate with manual updates
   - wait-sync: block until current sync completes

4. **CLI**: daemon start/stop/status/logs/reload/add-project/remove-project/list-projects

5. **Circuit breaker**: if events/minute > threshold, switch to
   "wait + bulk rebuild" mode instead of per-file incremental.

6. **Transactional updates**: each sync wrapped in
   transaction() context (snapshot + WAL + rollback on failure).

7. **Auto output file rebuild**: after each sync, regenerate affected
   output files (CODE2DATABASE_SUMMARY.md, .code2database_context_pack_lite.json,
   .code2database_endpoints.json, .code2database_communities.json, etc.).
"""

import json
import os
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, List, Dict, Set, Callable
import logging


# Linux inotify event bit-flags (see <sys/inotify.h>). Used as named
# constants instead of raw hex masks so the watch-mask construction is
# self-documenting. Values are stable across Linux kernel versions.
_IN_MODIFY    = 0x00000002  # File was modified
_IN_ATTRIB    = 0x00000004  # Metadata changed (permissions, timestamps, etc.)
_IN_CLOSE_WRITE = 0x00000008  # Writable file was closed
_IN_CREATE    = 0x00000100  # File/directory created in watched dir
_IN_DELETE    = 0x00000200  # File/directory deleted in watched dir
_IN_DELETE_SELF = 0x00000400  # Watched file/directory itself deleted
_IN_MOVE_SELF = 0x00000800  # Watched file/directory itself moved
_IN_MOVED_FROM = 0x00000040  # File moved from watched dir
_IN_MOVED_TO  = 0x00000080  # File moved to watched dir
_IN_ISDIR     = 0x40000000  # Event subject is a directory (flag, not event)
_IN_Q_OVERFLOW = 0x00004000  # Kernel event queue overflowed — events were DROPPED
_DEFAULT_WATCH_MASK = (_IN_MODIFY | _IN_ATTRIB | _IN_CLOSE_WRITE |
                         _IN_CREATE | _IN_DELETE | _IN_DELETE_SELF |
                         _IN_MOVE_SELF | _IN_MOVED_FROM | _IN_MOVED_TO)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default config (overridable via profile "daemon" section)
DEFAULT_CONFIG = {
    "enabled": True,
    "watch_paths": [],  # empty = whole source_root
    "exclude_patterns": ["*.swp", "*.tmp", "*.bak", "*~",
                          "build/", ".git/", "__pycache__/", "node_modules/"],
    "debounce_ms": 500,
    "batch_window_ms": 1000,
    "auto_rebuild_outputs": True,
    "idle_sleep_minutes": 30,
    "max_events_per_minute": 1000,
    "backend": "auto",  # auto / inotify / polling
    # D32: circuit breaker + adaptive batch options
    "circuit_breaker_window_sec": 60.0,  # window for event-rate counting
    "circuit_breaker_threshold": 1000,   # events/window → bulk rebuild
    "circuit_breaker_cooldown_sec": 30.0,  # after tripping, stay in bulk mode
    "adaptive_batch": True,              # auto-grow batch_window under load
    "adaptive_batch_min_ms": 200,        # floor for adaptive batch_window
    "adaptive_batch_max_ms": 5000,       # ceiling for adaptive batch_window
    "format_only_filter": True,          # distinguish formatting vs real changes
}

# File extensions to monitor
MONITORED_EXTS = {".c", ".h", ".cpp", ".hpp", ".cc", ".cxx",
                  ".go", ".py", ".java", ".rs", ".S",
                  ".json",  # profile config
                  ".cmake", ".txt"}  # CMakeLists.txt, Makefile

# Output files to rebuild after sync
OUTPUT_FILES = [
    "CODE2DATABASE_SUMMARY.md",
    "ARCHITECTURE_FLOWS.md",
    ".code2database_context_pack_lite.json",
    ".code2database_context_pack_lite.md",
    ".code2database_context_pack_micro.json",
    ".code2database_context_pack_micro.md",
    ".code2database_scenarios.json",
    ".code2database_endpoints.json",
    ".code2database_communities.json",
    ".code2database_entry_scores.json",
    ".code2database_signal_map.json",
    ".code2database_chains.json",
    ".code2database_chains_lite.json",
    ".code2database_reverse_index.json",
    ".code2database_condition_index.json",
    ".code2database_concurrency_index.json",
]

# Status values
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_SYNCING = "syncing"
STATUS_IDLE = "idle"
STATUS_STOPPED = "stopped"
STATUS_CRASHED = "crashed"


# ---------------------------------------------------------------------------
# State file management
# ---------------------------------------------------------------------------

@dataclass
class DaemonState:
    """Persisted daemon state, written to <graph_dir>/.daemon_status.json."""
    pid: int = 0
    status: str = STATUS_STOPPED
    started_at: float = 0.0
    last_sync_at: float = 0.0
    last_sync_duration_ms: int = 0
    last_sync_files: int = 0
    last_error: str = ""
    pending_events: int = 0
    stale_nodes: int = 0
    total_syncs: int = 0
    total_files_scanned: int = 0
    paused: bool = False
    paused_reason: str = ""
    config: Dict = field(default_factory=dict)
    projects: List[Dict] = field(default_factory=list)  # multi-project support

    def to_dict(self) -> Dict:
        return asdict(self)

    def write(self, graph_dir: str):
        """Atomically write state to <graph_dir>/.daemon_status.json."""
        path = Path(graph_dir) / ".daemon_status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: write to a PER-WRITER tmp file then rename. The old
        # fixed '.json.tmp' name is shared by the main loop, the sync worker
        # and the watcher callback — two concurrent writers could race so
        # one rename raised FileNotFoundError (which used to kill the
        # sync-worker thread or abandon the inotify event buffer).
        tmp = path.with_suffix(
            f".json.tmp.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        try:
            tmp.replace(path)
        except OSError:
            # Best effort: another writer may have removed our tmp (or the
            # target moved). The other writer's rename covers the update.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @classmethod
    def read(cls, graph_dir: str) -> "DaemonState":
        """Read state from <graph_dir>/.daemon_status.json."""
        path = Path(graph_dir) / ".daemon_status.json"
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            return cls()


# ---------------------------------------------------------------------------
# File watcher (inotify via ctypes, with polling fallback)
# ---------------------------------------------------------------------------

class _WatchdogHandler:
    """Adapter for the watchdog library (cross-platform FSEvents/ReadDirectoryChangesW/inotify).

    Used by FileWatcher when the `watchdog` package is available, so we get
    native kernel events on macOS (FSEvents) and Windows (ReadDirectoryChangesW)
    without writing platform-specific ctypes code ourselves.
    """

    def __init__(self, callback, exclude_patterns, is_excluded_fn):
        self._callback = callback
        self._exclude_patterns = exclude_patterns
        self._is_excluded = is_excluded_fn

    def on_any_event(self, event):
        """Watchdog callback for any file/directory event."""
        try:
            from watchdog.events import FileSystemEventHandler  # type: ignore
        except ImportError:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        src = getattr(event, "src_path", "") or ""
        if not src:
            return
        if self._is_excluded(src):
            return
        # Only fire for files (not directories) and only for modified/created
        if getattr(event, "is_directory", False):
            return
        if self._callback:
            self._callback(src)


class FileWatcher:
    """Watch directories for file changes.

    Uses inotify on Linux (via ctypes, no dependency). Falls back to
    polling (mtime + size comparison) on other platforms or when inotify
    fails.
    """

    def __init__(self, source_root: str,
                 exclude_patterns: List[str],
                 backend: str = "auto",
                 use_content_hash: bool = False,
                 graph_dir: str = ""):
        self.source_root = os.path.abspath(source_root)
        self.exclude_patterns = exclude_patterns
        self.backend = backend
        # When use_content_hash is True, the polling fallback computes SHA-256
        # of file contents instead of mtime+size. This catches changes that
        # mtime misses (e.g., touch -d, in-place rewrites within the same
        # second) and matches the cgdb incremental-sync baseline.
        self.use_content_hash = use_content_hash
        self._graph_dir = graph_dir
        self._stop = False
        self._callback: Optional[Callable[[str], None]] = None
        # Called (throttled) when the kernel inotify queue overflows and
        # events were dropped — the daemon reacts by queueing a bulk
        # resync, otherwise it would silently stay stale.
        self._on_overflow: Optional[Callable[[], None]] = None
        self._last_overflow_ts = 0.0
        self._thread: Optional[threading.Thread] = None
        self._inotify_fd = -1
        self._polling_state: Dict[str, str] = {}  # path → content hash (or mtime+size sig)

    def start(self, callback: Callable[[str], None],
              on_overflow: Optional[Callable[[], None]] = None):
        """Start watching; call callback(path) on each file change.

        on_overflow (optional) is called — throttled — when the kernel
        event queue overflows (IN_Q_OVERFLOW), meaning events were
        permanently dropped and change tracking can no longer be trusted.
        """
        self._callback = callback
        if on_overflow is not None:
            self._on_overflow = on_overflow
        self._stop = False
        # Try platform-native watchers first, fall back to polling.
        # Order: inotify (Linux) → FSEvents (macOS) → ReadDirectoryChangesW
        # (Windows) → polling (anywhere).
        if self.backend in ("auto", "inotify") and sys.platform.startswith("linux"):
            if self._start_inotify():
                self._thread = threading.Thread(target=self._run_inotify,
                                                 daemon=True, name="daemon-inotify")
                self._thread.start()
                return
        if self.backend in ("auto", "fsevents") and sys.platform == "darwin":
            if self._start_fsevents():
                self._thread = threading.Thread(target=self._run_fsevents,
                                                 daemon=True, name="daemon-fsevents")
                self._thread.start()
                return
        if self.backend in ("auto", "win32") and sys.platform in ("win32", "cygwin"):
            if self._start_win32():
                self._thread = threading.Thread(target=self._run_win32,
                                                 daemon=True, name="daemon-win32")
                self._thread.start()
                return
        # Fall back to polling
        self.backend = "polling"
        self._init_polling_state()
        self._thread = threading.Thread(target=self._run_polling,
                                         daemon=True, name="daemon-poller")
        self._thread.start()

    def stop(self):
        """Stop watching."""
        self._stop = True
        if self._inotify_fd >= 0:
            try:
                # os.close works on inotify fd
                os.close(self._inotify_fd)
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
            self._inotify_fd = -1
        if self._thread:
            self._thread.join(timeout=2.0)

    def _start_inotify(self) -> bool:
        """Initialize inotify via ctypes. Returns False if unavailable."""
        try:
            import ctypes
            import errno as _errno
            try:
                libc = ctypes.CDLL("libc.so.6", use_errno=True)
            except OSError:
                libc = ctypes.CDLL(None, use_errno=True)
            libc.inotify_init1.argtypes = [ctypes.c_int]
            libc.inotify_init1.restype = ctypes.c_int
            fd = libc.inotify_init1(0x800)
            if fd < 0:
                return False
            self._inotify_fd = fd
            self._libc = libc
            self._watch_descriptors = []
            self._wd_to_path: Dict[int, str] = {}
            self._inotify_exhausted = False
            mask = (_DEFAULT_WATCH_MASK
                    )
            self._add_watch_recursive(self.source_root, mask)
            if self._inotify_exhausted:
                self._log("inotify watch limit exhausted; falling back to polling")
                # Close the inotify fd so it doesn't leak for the daemon's
                # lifetime — the polling fallback will take over.
                try:
                    os.close(self._inotify_fd)
                except OSError:
                    pass
                self._inotify_fd = -1
                return False
            return True
        except Exception:
            self._inotify_fd = -1
            return False

    def _add_watch_recursive(self, dir_path: str, mask: int):
        """Add inotify watch for dir_path and all subdirectories."""
        import ctypes
        import errno as _errno
        libc = self._libc
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        libc.inotify_add_watch.restype = ctypes.c_int
        for dirpath, dirnames, filenames in os.walk(dir_path):
            dirnames[:] = [d for d in dirnames
                           if not self._is_excluded(os.path.join(dirpath, d))]
            wd = libc.inotify_add_watch(self._inotify_fd,
                                         dirpath.encode("utf-8"), mask)
            if wd >= 0:
                self._watch_descriptors.append(wd)
                self._wd_to_path[wd] = dirpath
            else:
                err = ctypes.get_errno()
                if err == _errno.ENOSPC:
                    self._inotify_exhausted = True
                    return

    def _is_excluded(self, path: str) -> bool:
        """Check if path matches any exclude pattern."""
        import fnmatch
        norm_path = path.replace("\\", "/")
        parts = norm_path.split("/")
        for pat in self.exclude_patterns:
            if pat.endswith("/"):
                clean = pat.rstrip("/")
                if clean in parts:
                    return True
            elif pat.startswith("*"):
                if fnmatch.fnmatch(os.path.basename(path), pat):
                    return True
            elif "/" in pat:
                if pat in norm_path:
                    return True
            else:
                if pat in parts:
                    return True
        return False

    def _run_inotify(self):
        """Read inotify events and dispatch to callback."""
        while not self._stop:
            try:
                import select as _select
                _poller = _select.poll()
                _poller.register(self._inotify_fd, _select.POLLIN)
                while not self._stop:
                    events = _poller.poll(1000)
                    if not events:
                        continue
                    data = os.read(self._inotify_fd, 65536)
                    if not data:
                        continue
                    self._process_inotify_buffer(data)
            except Exception:
                time.sleep(0.1)

    def _fire_callback(self, path: str):
        if self._callback:
            # Guard EACH callback: an exception here used to bubble to the
            # outer 'except Exception', abandoning the rest of the
            # already-read event buffer — inotify never re-delivers
            # dropped events.
            try:
                self._callback(path)
            except Exception:
                logging.getLogger(__name__).debug(
                    "silent exception", exc_info=True)

    def _add_dir_watch(self, dir_path: str) -> bool:
        """Add an inotify watch on dir_path; returns True on success.

        Sets _inotify_exhausted on ENOSPC; logs other failures instead of
        silently leaving the subtree unwatched.
        """
        import ctypes as _ct
        if self._inotify_exhausted:
            return False
        _wd = self._libc.inotify_add_watch(
            self._inotify_fd, dir_path.encode("utf-8"), _DEFAULT_WATCH_MASK)
        if _wd >= 0:
            self._wd_to_path[_wd] = dir_path
            return True
        _err = _ct.get_errno()
        if _err == 28:  # ENOSPC — watch limit reached
            self._inotify_exhausted = True
        else:
            # EACCES / ENOENT race / ENOMEM etc: previously swallowed —
            # the subtree stayed permanently unwatched with no trace.
            logging.getLogger(__name__).warning(
                "daemon: inotify_add_watch(%s) failed (errno=%s)",
                dir_path, _err)
        return False

    def _rescan_new_dir(self, dir_path: str, _depth: int = 0):
        """Watch subdirs and callback existing files of a newly-watched dir.

        Files created inside a brand-new directory BEFORE the watch took
        effect generate no events (the dir wasn't watched yet) — without
        this rescan, 'mkdir d && write d/foo.c' lost foo.c forever.
        """
        if _depth > 8 or self._inotify_exhausted:
            return
        try:
            entries = sorted(os.listdir(dir_path))
        except OSError:
            return
        for e in entries:
            p = os.path.join(dir_path, e)
            if self._is_excluded(p):
                continue
            if os.path.isdir(p):
                if self._add_dir_watch(p):
                    self._rescan_new_dir(p, _depth + 1)
            else:
                ext = Path(p).suffix.lower()
                if ext in MONITORED_EXTS or not ext:
                    self._fire_callback(p)

    def _repoint_dir_watches(self, old_path: str, new_path: str):
        """Re-point all wd→path mappings under a renamed directory.

        inotify watches the inode, so after 'git mv src/old src/new' the
        wds stay valid but _wd_to_path kept the OLD paths: subsequent
        events inside were attributed to nonexistent paths while the real
        new paths got nothing.
        """
        old_pfx = old_path.rstrip(os.sep) + os.sep
        for wd, p in list(self._wd_to_path.items()):
            if p == old_path:
                self._wd_to_path[wd] = new_path
            elif p.startswith(old_pfx):
                self._wd_to_path[wd] = new_path + p[len(old_path):]

    def _process_inotify_buffer(self, data: bytes):
        """Parse one inotify read buffer and dispatch events."""
        _pending_dir_moves = {}  # cookie -> old full path (MOVED_FROM|ISDIR)
        offset = 0
        while offset + 16 <= len(data):
            wd = int.from_bytes(data[offset:offset+4], "little", signed=True)
            mask = int.from_bytes(data[offset+4:offset+8], "little")
            cookie = int.from_bytes(data[offset+8:offset+12], "little")
            name_len = int.from_bytes(data[offset+12:offset+16], "little")
            if mask & _IN_Q_OVERFLOW:
                # Kernel dropped events (burst exceeded max_queued_events,
                # default 16384). The wd is -1 and no path is recoverable —
                # the daemon must bulk-resync or it silently stays stale
                # forever. Throttle to one signal per 5s so a sustained
                # overflow burst queues one bulk job, not hundreds.
                _now = time.time()
                if _now - self._last_overflow_ts >= 5.0:
                    self._last_overflow_ts = _now
                    logging.getLogger(__name__).warning(
                        "daemon: inotify queue overflow — events were "
                        "dropped; requesting bulk resync")
                    if self._on_overflow:
                        try:
                            self._on_overflow()
                        except Exception:
                            logging.getLogger(__name__).debug(
                                "silent exception", exc_info=True)
                offset += 16 + name_len
                continue
            name = data[offset+16:offset+16+name_len].rstrip(b"\0").decode("utf-8", errors="replace")
            base_path = self._wd_to_path.get(wd, "")
            if base_path:
                full_path = os.path.join(base_path, name) if name else base_path
                if not self._is_excluded(full_path):
                    if mask & 0x8000:  # IN_IGNORED — watch removed (dir deleted/moved): drop the wd mapping
                        self._wd_to_path.pop(wd, None)
                    elif mask & _IN_MOVE_SELF:
                        # The watched dir ITSELF was moved somewhere we
                        # can't see (parent unwatched / cross-tree). The wd
                        # follows the inode but we no longer know the path:
                        # drop the mapping and stale-mark the old subtree.
                        self._wd_to_path.pop(wd, None)
                        logging.getLogger(__name__).warning(
                            "daemon: watched dir moved: %s (stale-marking; "
                            "run daemon-force-refresh if needed)", base_path)
                    elif mask & _IN_MOVED_FROM and mask & _IN_ISDIR:
                        # Directory renamed away — pair with the MOVED_TO
                        # via the inotify cookie (same read buffer). Fire
                        # on the old path so the sync stale-marks it.
                        _pending_dir_moves[cookie] = full_path
                    elif mask & _IN_MOVED_TO and mask & _IN_ISDIR:
                        _old = _pending_dir_moves.pop(cookie, None)
                        if _old:
                            # Rename within the watched tree: re-point all
                            # subtree mappings old -> new.
                            self._repoint_dir_watches(_old, full_path)
                        # else: dir moved IN from outside the watched tree —
                        # its contents were never watched; watch + rescan.
                        if self._add_dir_watch(full_path):
                            self._rescan_new_dir(full_path)
                    elif mask & _IN_CREATE and mask & _IN_ISDIR:
                        if self._add_dir_watch(full_path):
                            # Rescan for files created between mkdir and
                            # the watch taking effect (no events exist for
                            # them).
                            self._rescan_new_dir(full_path)
                    ext = Path(full_path).suffix.lower()
                    if ext in MONITORED_EXTS or not ext:
                        self._fire_callback(full_path)
            offset += 16 + name_len
        # MOVED_FROM|ISDIR entries with no matching MOVED_TO in this buffer
        # were moved OUT of the watched tree: drop their subtree mappings
        # (events from the moved subtree no longer belong to us) and
        # stale-mark the old paths.
        for _old in _pending_dir_moves.values():
            self._repoint_drop_subtree(_old)
            self._fire_callback(_old)

    def _repoint_drop_subtree(self, old_path: str):
        """Remove wd→path mappings for a directory moved out of the tree."""
        old_pfx = old_path.rstrip(os.sep) + os.sep
        for wd, p in list(self._wd_to_path.items()):
            if p == old_path or p.startswith(old_pfx):
                self._wd_to_path.pop(wd, None)

    # ---- macOS FSEvents backend ----
    def _start_fsevents(self) -> bool:
        """Start FSEvents watcher via pyobjc-core (CoreFoundation) or watchdog.

        Returns False if neither library is available, so the caller falls
        back to polling. Uses CoreFoundation's CFRunLoop so we don't pull
        in the heavier FSEvents framework unless available.
        """
        try:
            # Prefer watchdog (cross-platform) if installed
            from watchdog.observers import Observer  # type: ignore
            from watchdog.events import FileSystemEventHandler  # type: ignore
            self._fsevents_observer = Observer()
            handler = _WatchdogHandler(self._callback, self.exclude_patterns,
                                        self._is_excluded)
            self._fsevents_observer.schedule(
                handler, self.source_root, recursive=True)
            self._fsevents_observer.start()
            self._fsevents_lib = "watchdog"
            return True
        except ImportError:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        try:
            # Try pyobjc FSEvents
            import FSEvents  # type: ignore  # noqa
            self._fsevents_lib = "pyobjc"
            return True
        except ImportError:
            return False

    def _run_fsevents(self):
        """Run FSEvents event loop (only used for pyobjc backend)."""
        # watchdog handles its own thread; pyobjc path would run CFRunLoop
        # here. For now, just sleep until stop.
        while not self._stop:
            time.sleep(0.5)

    # ---- Windows ReadDirectoryChangesW backend ----
    def _start_win32(self) -> bool:
        """Start ReadDirectoryChangesW watcher via win32file (pywin32) or
        watchdog.

        Returns False if neither library is available.
        """
        try:
            from watchdog.observers import Observer  # type: ignore
            from watchdog.events import FileSystemEventHandler  # type: ignore
            self._win32_observer = Observer()
            handler = _WatchdogHandler(self._callback, self.exclude_patterns,
                                        self._is_excluded)
            self._win32_observer.schedule(
                handler, self.source_root, recursive=True)
            self._win32_observer.start()
            self._win32_lib = "watchdog"
            return True
        except ImportError:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        try:
            import win32file  # type: ignore  # noqa
            import win32con  # type: ignore  # noqa
            self._win32_lib = "pywin32"
            self._win32_handle = None
            return True
        except ImportError:
            return False

    def _run_win32(self):
        """Run ReadDirectoryChangesW event loop (only used for pywin32)."""
        # watchdog handles its own thread; pywin32 path would loop on
        # ReadDirectoryChangesW here. For now, just sleep until stop.
        while not self._stop:
            time.sleep(0.5)

    def _file_signature(self, full_path: str) -> str:
        """Return a signature string for the file.

        With use_content_hash=True, returns SHA-256 hex of file content.
        Otherwise returns f"{mtime}+{size}" — fast but can miss in-place
        rewrites that preserve mtime+size.
        """
        try:
            st = os.stat(full_path)
        except OSError:
            return ""
        if not self.use_content_hash:
            return f"{st.st_mtime_ns}+{st.st_size}"
        # Content hash — slower but matches cgdb incremental-sync baseline.
        try:
            import hashlib
            h = hashlib.sha256()
            with open(full_path, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    h.update(chunk)
            return h.hexdigest()
        except (IOError, OSError):
            return f"{st.st_mtime_ns}+{st.st_size}"  # fallback

    def _init_polling_state(self):
        """Initialize polling state by scanning current files."""
        for dirpath, dirnames, filenames in os.walk(self.source_root):
            dirnames[:] = [d for d in dirnames
                           if not self._is_excluded(os.path.join(dirpath, d))]
            for fname in filenames:
                full_path = os.path.join(dirpath, fname)
                ext = Path(fname).suffix.lower()
                if ext not in MONITORED_EXTS:
                    continue
                if self._is_excluded(full_path):
                    continue
                sig = self._file_signature(full_path)
                if sig:
                    self._polling_state[full_path] = sig

    def _run_polling(self):
        """Polling loop: compare signatures every poll interval.

        Two-phase detection: first use fast mtime_ns+size fingerprint
        to filter candidates, then compute expensive SHA-256 only for
        files whose mtime/size changed. This reduces polling cost from
        O(N_files × file_size) to O(N_files + changed_files × file_size).
        """
        incremental_sync = None
        if self.use_content_hash and self._graph_dir:
            try:
                from _builder.cgdb_incremental import IncrementalSync
                db_path = Path(self._graph_dir) / "code2database.db"
                if db_path.exists():
                    incremental_sync = IncrementalSync(self.source_root)
                    incremental_sync.db_path = str(db_path)
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        _poll_interval = getattr(self, 'polling_interval', 2.0)
        while not self._stop:
            time.sleep(_poll_interval)
            current: Dict[str, str] = {}
            candidates_for_hash = []
            for dirpath, dirnames, filenames in os.walk(self.source_root):
                dirnames[:] = [d for d in dirnames
                               if not self._is_excluded(os.path.join(dirpath, d))]
                for fname in filenames:
                    full_path = os.path.join(dirpath, fname)
                    ext = Path(fname).suffix.lower()
                    if ext not in MONITORED_EXTS:
                        continue
                    if self._is_excluded(full_path):
                        continue
                    try:
                        st = os.stat(full_path)
                    except OSError:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        continue
                    fast_sig = f"{st.st_mtime_ns}+{st.st_size}"
                    current[full_path] = fast_sig
                    old_sig = self._polling_state.get(full_path)
                    if old_sig is None or old_sig != fast_sig:
                        candidates_for_hash.append(full_path)
            for full_path in candidates_for_hash:
                sig = self._file_signature(full_path)
                if not sig:
                    continue
                current[full_path] = sig
                old_content = self._polling_state.get(full_path)
                if old_content is None or old_content != sig:
                    if self._callback:
                        self._callback(full_path)
            for old_path in list(self._polling_state.keys()):
                if old_path not in current:
                    if self._callback:
                        self._callback(old_path)
                    del self._polling_state[old_path]

            # DB-aware change detection — compare current file content hashes
            # against the cgdb_files table's stored content_hash. Any file
            # whose DB hash differs from its current hash but wasn't caught
            # by the polling state (e.g., daemon was offline when the file
            # changed) gets flagged as changed via the callback.
            if incremental_sync is not None:
                try:
                    changed_in_db = incremental_sync.detect_changes(
                        incremental_sync.db_path
                    )
                    for changed_path in changed_in_db:
                        if changed_path not in current and self._callback:
                            self._callback(changed_path)
                except Exception:
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
            self._polling_state = current


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class Daemon:
    """Long-running daemon that monitors source files and auto-updates graph."""

    def __init__(self, graph_dir: str, source_root: str,
                 config: Optional[Dict] = None,
                 profile_path: str = ""):
        self.graph_dir = os.path.abspath(graph_dir)
        self.source_root = os.path.abspath(source_root)
        self.profile_path = profile_path
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        # Deep-copy list/dict values so mutations don't corrupt the
        # module-level DEFAULT_CONFIG for the next Daemon instance.
        import copy as _copy
        for _k, _v in self.config.items():
            if isinstance(_v, (list, dict)):
                self.config[_k] = _copy.deepcopy(_v)
        self.state = DaemonState(
            pid=os.getpid(),
            status=STATUS_RUNNING,
            started_at=time.time(),
            config=self.config,
        )
        self._watcher: Optional[FileWatcher] = None
        self._pending: Set[str] = set()
        self._pending_lock = threading.Lock()
        self._last_sync_time = 0.0
        from collections import deque
        self._event_timestamps = deque()  # for circuit breaker
        # Use $TMPDIR (POSIX) if set, otherwise fall back to /tmp.
        # Respects system conventions: macOS sets TMPDIR automatically,
        # Linux defaults to /tmp, sandboxes/container runtimes may use
        # a private tmpdir that the daemon MUST use (not /tmp).
        self._socket_path = _daemon_socket_path(self.graph_dir)
        self._socket_thread: Optional[threading.Thread] = None
        self._server_socket: Optional[socket.socket] = None
        self._stop = False
        # D31: separate sync worker thread so socket queries aren't blocked
        # by long-running syncs. Main loop dispatches sync jobs to this worker;
        # status is reported via _sync_busy / _sync_pending_jobs.
        self._sync_worker_thread: Optional[threading.Thread] = None
        from collections import deque
        self._sync_jobs = deque()
        self._sync_jobs_lock = threading.Lock()
        self._sync_busy = False
        self._sync_busy_lock = threading.Lock()
        self._last_sync_result: Optional[Dict] = None
        self._last_synced_content = {}
        self._last_synced_content_max = 1000
        # Lock for the foreign-sync throttle (check-and-set of
        # _last_foreign_sync_ts) — prevents TOCTOU race where both
        # the main thread and sync worker thread pass the throttle
        # check concurrently and both call sync_foreign.
        self._foreign_sync_lock = threading.Lock()
        self._state_dirty = False
        self._last_state_write = 0.0

        # Crash recovery: check for previous daemon state
        _old_state = DaemonState.read(self.graph_dir)
        if _old_state and _old_state.status == STATUS_SYNCING:
            self._log("recovering from crash: last sync was in-progress")
        self._recovered_pending_count = _old_state.pending_events if _old_state else 0

    def start(self):
        """Start the daemon (foreground; blocks until stop())."""
        self._write_state()
        self._setup_signal_handlers()
        # Wrap setup in try/except so that if any step fails (socket
        # bind, sync worker thread start, watcher start), _cleanup()
        # runs and releases all resources acquired so far (socket fd,
        # thread handle, inotify fd, socket file). Previously, an
        # exception in any setup step leaked all resources acquired
        # by earlier steps — the socket file persisted, blocking the
        # next daemon-start.
        try:
            self._start_socket_server()
            self._start_sync_worker()
            if self._recovered_pending_count > 0:
                self._log(f"recovering {self._recovered_pending_count} pending events via bulk sync")
                self._enqueue_sync_job("bulk", [])
                self._recovered_pending_count = 0
            # Start file watcher. Enable content-hash polling when cgdb is in use
            # so the daemon's change detection matches the cgdb incremental-sync
            # baseline (SHA-256 instead of mtime+size).
            use_content_hash = self.config.get("use_content_hash", False)
            # Auto-enable content hash if a cgdb-enabled DB exists
            if not use_content_hash:
                db_path = Path(self.graph_dir) / "code2database.db"
                if db_path.exists():
                    try:
                        import sqlite3
                        conn = sqlite3.connect(str(db_path))
                        try:
                            conn.execute("SELECT 1 FROM cgdb_nodes LIMIT 1").fetchone()
                            use_content_hash = True
                        except Exception:
                            logging.getLogger(__name__).debug("silent exception", exc_info=True)
                            pass
                        finally:
                            conn.close()
                    except Exception:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
            self._watcher = FileWatcher(
                self.source_root,
                self.config.get("exclude_patterns", []),
                self.config.get("backend", "auto"),
                use_content_hash=use_content_hash,
                graph_dir=self.graph_dir,
            )
            self._watcher.start(
                self._on_file_change,
                on_overflow=self._on_watch_overflow,
            )
        except Exception:
            self._cleanup()
            raise
        # Main loop: batch + dispatch sync jobs to the worker thread
        # Apply env-var overrides (D32) — CALLGRAPH_DAEMON_* overrides config
        self._apply_env_overrides()
        batch_window_ms = self.config.get("batch_window_ms", 1000)
        debounce = self.config.get("debounce_ms", 500) / 1000.0
        idle_sleep = self.config.get("idle_sleep_minutes", 30) * 60
        last_activity = time.time()
        last_breaker_trip = 0.0
        while not self._stop:
            now = time.time()
            # Check paused state — skip all processing while paused
            if self.state.paused:
                if self.state.status != STATUS_PAUSED:
                    self.state.status = STATUS_PAUSED
                    self._write_state()
                time.sleep(1.0)
                continue
            # F9: periodically check watched foreign C2D db mtimes
            if now - getattr(self, '_last_foreign_check', 0) > 60.0:
                self._last_foreign_check = now
                self._check_watched_foreign_c2ds()
            with self._pending_lock:
                pending_count = len(self._pending)
            if pending_count == 0:
                # Idle — check if we should sleep
                if now - last_activity > idle_sleep:
                    self.state.status = STATUS_IDLE
                    if getattr(self, '_state_dirty', False):
                        self._write_state()
                        self._state_dirty = False
                elif getattr(self, '_state_dirty', False) and \
                     now - getattr(self, '_last_state_write', 0) > 1.0:
                    self._write_state()
                    self._state_dirty = False
                    self._last_state_write = now
                time.sleep(1.0)
                continue
            # Wait for debounce + batch window
            time_since_last_event = now - self._last_sync_time
            if time_since_last_event < debounce:
                time.sleep(debounce - time_since_last_event)
                continue
            # D32: adaptive batch — grow batch_window when event rate is high
            # so we collect more events per sync (fewer, larger syncs).
            if self.config.get("adaptive_batch", True):
                batch_window_ms = self._compute_adaptive_batch_window()
            time.sleep(batch_window_ms / 1000.0)
            # Snapshot pending paths and dispatch a sync job to the worker
            with self._pending_lock:
                paths_to_sync = sorted(self._pending)
                self._pending.clear()
                self.state.pending_events = 0
            # D32: filter out format-only changes (whitespace/comments only)
            if self.config.get("format_only_filter", True):
                paths_to_sync = self._filter_format_only(paths_to_sync)
            self._prune_old_events(now)
            # D32: configurable circuit breaker with cooldown
            window = self.config.get("circuit_breaker_window_sec", 60.0)
            threshold = self.config.get("circuit_breaker_threshold", 1000)
            cooldown = self.config.get("circuit_breaker_cooldown_sec", 30.0)
            events_in_window = len(self._event_timestamps)
            in_cooldown = (now - last_breaker_trip) < cooldown
            if events_in_window > threshold or in_cooldown:
                job_kind = "bulk"
                if not in_cooldown:
                    last_breaker_trip = now
                    self._log(f"circuit breaker tripped: {events_in_window} events "
                              f"in {window}s > threshold {threshold}")
            else:
                job_kind = "incremental"
            self._enqueue_sync_job(job_kind, paths_to_sync)
            last_activity = time.time()
        # Cleanup
        self._cleanup()

    def _apply_env_overrides(self):
        """Apply CALLGRAPH_DAEMON_* env-var overrides to self.config (D32)."""
        env_map = {
            "CALLGRAPH_DAEMON_BATCH_WINDOW_MS": ("batch_window_ms", int),
            "CALLGRAPH_DAEMON_DEBOUNCE_MS": ("debounce_ms", int),
            "CALLGRAPH_DAEMON_MAX_EVENTS_PER_MINUTE": (
                "max_events_per_minute", int),
            "CALLGRAPH_DAEMON_CIRCUIT_BREAKER_THRESHOLD": (
                "circuit_breaker_threshold", int),
            "CALLGRAPH_DAEMON_CIRCUIT_BREAKER_WINDOW_SEC": (
                "circuit_breaker_window_sec", float),
            "CALLGRAPH_DAEMON_CIRCUIT_BREAKER_COOLDOWN_SEC": (
                "circuit_breaker_cooldown_sec", float),
            "CALLGRAPH_DAEMON_ADAPTIVE_BATCH": ("adaptive_batch",
                                                  lambda x: x.lower() == "true"),
            "CALLGRAPH_DAEMON_FORMAT_ONLY_FILTER": ("format_only_filter",
                                                      lambda x: x.lower() == "true"),
            "CALLGRAPH_DAEMON_BACKEND": ("backend", str),
        }
        for env_key, (config_key, caster) in env_map.items():
            val = os.environ.get(env_key)
            if val is None:
                continue
            try:
                self.config[config_key] = caster(val)
            except (ValueError, TypeError):
                self._log(f"ignoring invalid env var {env_key}={val!r}")

    def _compute_adaptive_batch_window(self) -> int:
        """Compute adaptive batch_window_ms based on current event rate.

        If events are arriving fast, grow the batch window so we collect
        more per sync (fewer syncs, less overhead). If events are slow,
        shrink back toward the floor.
        """
        min_ms = self.config.get("adaptive_batch_min_ms", 200)
        max_ms = self.config.get("adaptive_batch_max_ms", 5000)
        base_ms = self.config.get("batch_window_ms", 1000)
        threshold = self.config.get("circuit_breaker_threshold", 1000)
        # Rate over the breaker window
        events_in_window = len(self._event_timestamps)
        if threshold <= 0:
            return base_ms
        load_ratio = events_in_window / threshold
        if load_ratio < 0.3:
            return min_ms
        if load_ratio > 0.8:
            return max_ms
        # Linear interpolation between min and max
        scaled = min_ms + (max_ms - min_ms) * (load_ratio - 0.3) / 0.5
        return int(scaled)

    def _filter_format_only(self, paths: List[str]) -> List[str]:
        """Filter out paths whose only changes are whitespace/comments.

        Compares the file's current content to its last-synced content
        (if available). If the diff is whitespace-only or only adds/removes
        comment lines, the file is skipped (treated as a format-only change).
        Files without a known previous content are kept (safer to sync).
        """
        if not paths:
            return paths
        kept: List[str] = []
        for p in paths:
            if not os.path.isfile(p):
                kept.append(p)  # deletion — always sync
                continue
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except OSError:
                kept.append(p)
                continue
            prev = self._last_synced_content.get(p)
            if prev is None:
                kept.append(p)
            elif self._is_format_only_change(prev, content):
                pass  # Skip — only formatting changed
            else:
                kept.append(p)
            # Update cache with LRU eviction
            if len(self._last_synced_content) >= self._last_synced_content_max:
                _oldest = next(iter(self._last_synced_content))
                del self._last_synced_content[_oldest]
            self._last_synced_content[p] = content
        return kept

    def _is_format_only_change(self, prev: str, cur: str) -> bool:
        """Heuristic: true if prev and cur differ only in whitespace or
        comment lines.

        Strips comments and whitespace from both, then compares. If equal,
        the change is format-only. Comment stripping is language-agnostic:
        drops `//...` and `/* ... */` comments. Does NOT drop `#...`
        lines because in C/C++ those are preprocessor directives
        (#include, #define, #ifdef) that affect semantics.
        """
        def _strip(s: str) -> str:
            lines = []
            in_block = False
            for line in s.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if in_block:
                    if "*/" in stripped:
                        after = stripped[stripped.index("*/") + 2:].strip()
                        in_block = False
                        if after and not after.startswith("//"):
                            lines.append("".join(after.split()))
                    continue
                if "/*" in stripped:
                    before = stripped[:stripped.index("/*")].strip()
                    if before and not before.startswith("//"):
                        lines.append("".join(before.split()))
                    if "*/" not in stripped[stripped.index("/*") + 2:]:
                        in_block = True
                    continue
                if stripped.startswith("//"):
                    continue
                lines.append("".join(stripped.split()))
            return "\n".join(lines)
        return _strip(prev) == _strip(cur)

    def _start_sync_worker(self):
        """Start the background sync worker thread (D31)."""
        self._sync_worker_thread = threading.Thread(
            target=self._sync_worker_loop,
            daemon=True, name="daemon-sync-worker"
        )
        self._sync_worker_thread.start()

    def _sync_worker_loop(self):
        """Worker loop: process queued sync jobs.

        Runs in a separate thread so socket queries (in another thread)
        aren't blocked while sync runs. Each job is either 'incremental'
        or 'bulk'. Updates _sync_busy and _last_sync_result for status
        reporting.
        """
        while not self._stop:
            job = None
            with self._sync_jobs_lock:
                if self._sync_jobs:
                    job = self._sync_jobs.popleft()
            if job is None:
                time.sleep(0.2)
                continue
            # Guard the ENTIRE per-job body: an exception outside the inner
            # try (e.g. _write_state on a transient disk error) used to
            # propagate out of the loop and permanently kill the sync-worker
            # thread — jobs then queued forever while status claimed
            # 'running'.
            try:
                with self._sync_busy_lock:
                    self._sync_busy = True
                self.state.status = STATUS_SYNCING
                self._write_state()
                try:
                    if job.get("kind") == "bulk":
                        result = self._sync_bulk()
                    else:
                        # Restore pending paths for incremental sync
                        with self._pending_lock:
                            self._pending.update(job.get("paths", []))
                        result = self._sync_incremental()
                    self._last_sync_result = {
                        "kind": job["kind"],
                        "completed_at": time.time(),
                        "path_count": len(job.get("paths", [])),
                        "ok": True,
                    }
                except Exception as exc:
                    self._last_sync_result = {
                        "kind": job["kind"],
                        "completed_at": time.time(),
                        "path_count": len(job.get("paths", [])),
                        "ok": False,
                        "error": str(exc),
                    }
                finally:
                    with self._sync_busy_lock:
                        self._sync_busy = False
                    self.state.status = STATUS_RUNNING
                    self._write_state()
            except Exception:
                # Never let the worker thread die: release busy flag and
                # keep processing the queue.
                with self._sync_busy_lock:
                    self._sync_busy = False
                self.state.status = STATUS_RUNNING
                logging.getLogger(__name__).warning(
                    "daemon: sync job crashed worker loop (recovered)",
                    exc_info=True)

    def _enqueue_sync_job(self, kind: str, paths: List[str]):
        """Add a sync job to the worker queue."""
        with self._sync_jobs_lock:
            self._sync_jobs.append({
                "kind": kind,
                "paths": list(paths),
                "queued_at": time.time(),
            })

    def is_sync_busy(self) -> bool:
        """Check if a sync is currently in progress."""
        with self._sync_busy_lock:
            return self._sync_busy

    def get_sync_status(self) -> Dict:
        """Get detailed sync status for queries."""
        with self._sync_jobs_lock:
            queued = len(self._sync_jobs)
        with self._sync_busy_lock:
            busy = self._sync_busy
        return {
            "busy": busy,
            "queued_jobs": queued,
            "last_result": self._last_sync_result,
        }

    def stop(self):
        """Signal the daemon to stop."""
        self._stop = True
        self.state.status = STATUS_STOPPED
        self._write_state()

    def _setup_signal_handlers(self):
        """Handle SIGTERM/SIGINT gracefully.

        Only works in the main thread; if daemon runs in a background
        thread (e.g., for testing), signal handlers are skipped — the
        caller is responsible for calling stop().
        """
        try:
            def handler(signum, frame):
                self.stop()
            signal.signal(signal.SIGTERM, handler)
            signal.signal(signal.SIGINT, handler)
            # SIGHUP is not available on Windows; only register on POSIX.
            # Reload is best-effort and may fail silently if config is
            # being mutated concurrently — see M1/M3 in the audit notes.
            sighup = getattr(signal, "SIGHUP", None)
            if sighup is not None:
                def reload_handler(signum, frame):
                    self._log("SIGHUP received, reloading config")
                    try:
                        self._apply_env_overrides()
                        if self.profile_path and os.path.exists(self.profile_path):
                            profile = json.loads(Path(self.profile_path).read_text(encoding="utf-8"))
                            self.config = {**DEFAULT_CONFIG, **(profile.get("daemon", {}) or {})}
                            self._apply_env_overrides()
                        self._log("config reloaded")
                    except Exception as exc:
                        self._log(f"reload failed: {exc}")
                signal.signal(sighup, reload_handler)
        except (ValueError, OSError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    def _on_watch_overflow(self):
        """Called by the watcher when the kernel event queue overflowed.

        Events were permanently dropped, so incremental pending state is
        incomplete — queue a bulk resync (same recovery path as daemon
        restart with pending events) to re-derive freshness from disk.
        """
        self._log("inotify queue overflow: events dropped, queueing bulk resync")
        self._enqueue_sync_job("bulk", [])

    def _on_file_change(self, path: str):
        """Called by watcher on each file change."""
        now = time.time()
        self._event_timestamps.append(now)
        with self._pending_lock:
            self._pending.add(path)
            self.state.pending_events = len(self._pending)
        self._last_sync_time = now
        self._state_dirty = True
        if now - getattr(self, '_last_state_write', 0) > 1.0:
            self._write_state()
            self._last_state_write = now

    def _prune_old_events(self, now: float):
        window = self.config.get("circuit_breaker_window_sec", 60.0)
        cutoff = now - window
        while self._event_timestamps and self._event_timestamps[0] < cutoff:
            self._event_timestamps.popleft()

    def _sync_incremental(self):
        """Run incremental sync: re-scan only changed files.

        If cgdb incremental sync is available, expand the changed-files set
        via compute_affected_tus() so that all TUs transitively including a
        changed header are also re-scanned.
        """
        with self._pending_lock:
            files = sorted(self._pending)
            self._pending.clear()
            self.state.pending_events = 0
        if not files:
            return
        # Expand via #include dep graph (cgdb Phase 5 incremental sync).
        # This catches the common case where a header changed and all TUs
        # that #include it (directly or transitively) need to be re-scanned.
        expanded_files = files
        try:
            from _builder.cgdb_incremental import IncrementalSync
            sync = IncrementalSync(self.source_root)
            affected = sync.compute_affected_tus(files)
            if affected and len(affected) > len(files):
                self._log(
                    f"expanded {len(files)} changed → {len(affected)} affected "
                    f"(via #include dep graph)"
                )
                # Preserve non-TU files (e.g., deleted headers) from original
                # set, plus add affected TUs.
                expanded_files = sorted(set(files) | set(affected))
        except Exception as exc:
            # Fall back to original file set if cgdb incremental unavailable
            self._log(f"cgdb incremental expansion skipped: {exc}")
        self.state.status = STATUS_SYNCING
        self._write_state()
        start = time.time()
        try:
            # Use transaction for atomic update
            self._run_transactional_sync(expanded_files)
            duration_ms = int((time.time() - start) * 1000)
            self.state.last_sync_at = time.time()
            self.state.last_sync_duration_ms = duration_ms
            self.state.last_sync_files = len(expanded_files)
            self.state.total_syncs += 1
            self.state.total_files_scanned += len(expanded_files)
            self.state.last_error = ""
            self.state.status = STATUS_RUNNING
            # Update content hashes in cgdb_files for the synced files
            try:
                from _builder.cgdb_incremental import IncrementalSync
                sync = IncrementalSync(self.source_root)
                db_path = Path(self.graph_dir) / "code2database.db"
                if db_path.exists():
                    sync.mark_clean(expanded_files, str(db_path))
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
            # Invalidate memory entries whose node_ids no longer exist
            # (graph changed → some Q&A may reference removed nodes).
            self._invalidate_stale_memory_after_sync()
            # Phase 1 cross-C2D: re-resolve foreign refs (B's unresolved
            # calls may now match A's existing functions after B's update).
            self._sync_foreign_refs_after_local_update()
        except Exception as exc:
            self.state.last_error = str(exc)
            self.state.status = STATUS_RUNNING
            self._log(f"sync failed: {exc}")
        self._write_state()

    def _sync_bulk(self):
        """Bulk rebuild: re-scan whole source (circuit breaker triggered)."""
        self._log("circuit breaker triggered, doing bulk rebuild")
        self.state.status = STATUS_SYNCING
        self._write_state()
        with self._pending_lock:
            self._pending.clear()
            self.state.pending_events = 0
        start = time.time()
        try:
            self._run_bulk_rebuild()
            duration_ms = int((time.time() - start) * 1000)
            self.state.last_sync_at = time.time()
            self.state.last_sync_duration_ms = duration_ms
            self.state.last_sync_files = -1  # bulk
            self.state.total_syncs += 1
            self.state.last_error = ""
            self.state.status = STATUS_RUNNING
            # Invalidate memory entries after bulk rebuild (graph may have
            # changed significantly → many memory refs may now be stale).
            self._invalidate_stale_memory_after_sync()
            # Phase 1 cross-C2D: also re-resolve foreign refs after bulk.
            self._sync_foreign_refs_after_local_update()
        except Exception as exc:
            self.state.last_error = str(exc)
            self.state.status = STATUS_RUNNING
            self._log(f"bulk rebuild failed: {exc}")
        self._write_state()

    def _run_transactional_sync(self, files: List[str]):
        """Run incremental sync wrapped in a write lock with timeout.

        If the write lock cannot be acquired (user transaction in progress),
        the files are re-queued for the next sync cycle — we do NOT fall
        back to direct sync, as writing without the lock while a user
        transaction is active would bypass snapshot/rollback semantics.
        """
        try:
            from _builder.transactions import write_lock
        except ImportError:
            self._run_direct_sync(files)
            return
        try:
            with write_lock(self.graph_dir, timeout=30.0):
                self._run_direct_sync(files)
        except TimeoutError:
            self._log(f"transaction lock timeout (user tx in progress?), "
                      f"deferring {len(files)} files to next sync cycle")
            with self._pending_lock:
                self._pending.update(files)
            self.state.pending_events = len(self._pending)
        except Exception as exc:
            self._log(f"transactional sync failed: {exc}, deferring to next cycle")
            with self._pending_lock:
                self._pending.update(files)
            self.state.pending_events = len(self._pending)

    def _run_direct_sync(self, files: List[str]):
        """Run direct incremental sync using stale-marking."""
        for f in files:
            if not os.path.exists(f):
                # File was deleted — mark it stale so graph nodes
                # from this file are flagged as stale (was: silent
                # continue, leaving orphan nodes as "live" forever).
                self._mark_file_stale(f)
                self._log(f"file deleted: {f} — marked stale")
                continue
            # Light-scan the file and patch
            try:
                self._mark_file_stale(f)
            except Exception as exc:
                self._log(f"failed to sync {f}: {exc}")
        # Rebuild output files if configured
        if self.config.get("auto_rebuild_outputs", True):
            self._rebuild_output_files()

    def _run_bulk_rebuild(self):
        """Run full rebuild via scanner + builder."""
        # Just touch all output files for now — full rebuild is expensive
        # and should be done by user explicitly via `build` command.
        # Daemon's bulk mode marks all nodes stale and rebuilds outputs.
        if self.config.get("auto_rebuild_outputs", True):
            self._rebuild_output_files()

    def _mark_file_stale(self, file_path: str):
        """Mark all functions in file_path as stale in the graph."""
        try:
            master_path = Path(self.graph_dir) / "code2database_master.json"
            db_path = Path(self.graph_dir) / "code2database.db"
            if not master_path.exists() and not db_path.exists():
                self._log(f"graph not yet built, skipping stale-mark for {file_path}")
                return

            norm_file = os.path.normpath(os.path.abspath(file_path))
            base_name = os.path.basename(norm_file)

            if db_path.exists():
                import sqlite3
                conn = sqlite3.connect(str(db_path))
                try:
                    conn.execute(
                        "UPDATE functions SET stale = 1 "
                        "WHERE source_file = ? OR source_file LIKE ?",
                        (norm_file, f"%/{base_name}")
                    )
                    # Keep extra_json in sync — consumers that read
                    # extra_json.stale (validate.py _load_all_functions)
                    # would otherwise see stale=false despite the column
                    # being set. Same pattern as update_cmd's sync fix.
                    conn.execute(
                        "UPDATE functions SET extra_json = json_set("
                        "extra_json, '$.stale', json('1')) "
                        "WHERE (source_file = ? OR source_file LIKE ?) "
                        "AND extra_json IS NOT NULL",
                        (norm_file, f"%/{base_name}")
                    )
                    conn.commit()
                except sqlite3.Error:
                    logging.getLogger(__name__).warning(
                        "daemon: stale-mark UPDATE failed", exc_info=True)
                finally:
                    conn.close()
                return

            from _builder.graph_build import _load_full_graph
            G = _load_full_graph(self.graph_dir)
            # LazySQLiteGraph (read-only SQLite view) doesn't support
            # G.nodes[nid][...] = ... assignment; skip mutation and rely on
            # the SQL UPDATE path above for SQLite-backed graphs.
            if type(G).__name__ == "LazySQLiteGraph":
                self._log(
                    f"skip stale-mark via LazySQLiteGraph (read-only); "
                    f"SQL path handles it for SQLite-backed graphs"
                )
                return
            if not hasattr(self, '_file_to_nodes_cache'):
                self._file_to_nodes_cache = {}
                for nid, nd in G.nodes(data=True):
                    sf = nd.get("source_file", "")
                    if sf:
                        norm_sf = os.path.normpath(os.path.abspath(sf))
                        self._file_to_nodes_cache.setdefault(norm_sf, []).append(nid)
            for nid in self._file_to_nodes_cache.get(norm_file, []):
                G.nodes[nid]["stale"] = True
        except Exception as exc:
            self._log(f"mark_file_stale failed: {exc}")

    def _invalidate_stale_memory_after_sync(self):
        """After graph sync, mark memory entries with missing node_ids as
        'experience' (stale) so they don't pollute search results.

        Called from _sync_incremental and _sync_bulk. Safe to call on
        LazySQLiteGraph (only reads G.nodes() — no mutation).
        """
        try:
            from _builder.memory_cmd import _auto_validate_memory
            from _builder.graph_build import _load_full_graph
            G = _load_full_graph(self.graph_dir)
            # _auto_validate_memory only reads set(G.nodes()); safe on
            # LazySQLiteGraph (which supports __contains__ + nodes()).
            _auto_validate_memory(G, os.path.join(self.graph_dir, "memory"),
                                  self.graph_dir)
        except Exception as exc:
            self._log(f"memory invalidate after sync skipped: {exc}")
            # Non-fatal: memory stays as-is; user can run `validate-memory`
            # manually to catch stale entries.

    def _sync_foreign_refs_after_local_update(self):
        """After B's graph updates, re-resolve foreign refs in case B's
        unresolved calls now match A's existing functions.

        Phase 1 cross-C2D sync integration. Iterates over watched_c2ds
        and calls sync_foreign for each. Best-effort: errors are logged
        but don't fail the daemon sync.

        P2 throttle: min 60s between runs to avoid hammering foreign
        dbs when B updates frequently.
        """
        # Throttle: don't re-sync foreign refs more than once per 60s.
        # Use a lock to prevent the TOCTOU race where both the main
        # thread (via _check_watched_foreign_c2ds every 60s) and the
        # sync worker thread (via _sync_incremental / _sync_bulk)
        # pass the throttle check concurrently and both call
        # sync_foreign — causing concurrent ATTACH + UPDATE on
        # watched_c2ds / foreign_refs from separate connections.
        now = time.time()
        with self._foreign_sync_lock:
            if getattr(self, '_last_foreign_sync_ts', 0) and \
                    now - self._last_foreign_sync_ts < 60.0:
                return  # throttled
            self._last_foreign_sync_ts = now
        try:
            from _builder.c2d_foreign import sync_foreign
            summary = sync_foreign(self.graph_dir, verbose=False)
            if summary.get("synced_c2ds"):
                synced = [s for s in summary["synced_c2ds"]
                           if s.get("status") == "synced"]
                if synced:
                    self._log(
                        f"foreign refs synced: {len(synced)} c2d(s); "
                        f"newly_resolved={summary.get('newly_resolved', 0)}, "
                        f"deleted={summary.get('deleted_marked', 0)}"
                    )
        except Exception as exc:
            self._log(f"foreign refs sync after local update skipped: {exc}")

    def _check_watched_foreign_c2ds(self):
        """F9: periodically check watched foreign C2D db mtimes.

        If a foreign C2D (A) has been updated (mtime changed), trigger
        sync_foreign to re-resolve B's foreign_refs. Called from the
        main loop every ~60 seconds (polling-based, cross-platform).
        """
        try:
            import sqlite3
            db_path = os.path.join(self.graph_dir, "code2database.db")
            if not os.path.exists(db_path):
                return
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                try:
                    watched = conn.execute(
                        "SELECT c2d_path, db_mtime_at_sync FROM watched_c2ds "
                        "WHERE sync_status IN ('ok', 'stub')"
                    ).fetchall()
                except sqlite3.OperationalError:
                    return  # watched_c2ds table doesn't exist
                needs_sync = False
                for w in watched:
                    fdb_path = os.path.join(w["c2d_path"], "code2database.db")
                    if not os.path.exists(fdb_path):
                        continue
                    try:
                        current_mtime = str(os.path.getmtime(fdb_path))
                    except OSError:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        continue
                    if current_mtime != w["db_mtime_at_sync"]:
                        needs_sync = True
                        self._log(f"foreign c2d changed: {w['c2d_path']}")
                        break
            finally:
                conn.close()
            if needs_sync:
                self._sync_foreign_refs_after_local_update()
        except Exception as exc:
            self._log(f"watched foreign c2d check failed: {exc}")

    def _rebuild_output_files(self):
        """Rebuild affected output files (CODE2DATABASE_SUMMARY.md, etc.)."""
        # For each output file, update last_updated_at marker
        # (full rebuild is the user's responsibility via `build`)
        for fname in OUTPUT_FILES:
            fpath = Path(self.graph_dir) / fname
            if fpath.exists():
                # Touch the file's mtime to signal freshness
                try:
                    os.utime(fpath, None)
                except OSError:
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
        freshness = {
            "last_updated_at": time.time(),
            "source_commit": "",  # would be filled from git if available
            "daemon_pid": os.getpid(),
        }
        fresh_path = Path(self.graph_dir) / ".code2database_freshness.json"
        # Atomic write: tmp + os.replace
        tmp_path = fresh_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(freshness, indent=2), encoding="utf-8")
        os.replace(str(tmp_path), str(fresh_path))

    def _start_socket_server(self):
        """Start Unix socket server for daemon-status/force-refresh/etc."""
        try:
            self._server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            # Remove stale socket file
            if os.path.exists(self._socket_path):
                os.unlink(self._socket_path)
            self._server_socket.bind(self._socket_path)
            self._server_socket.listen(5)
            self._server_socket.settimeout(1.0)
            os.chmod(self._socket_path, 0o600)
            self._socket_thread = threading.Thread(
                target=self._accept_loop, daemon=True, name="daemon-socket")
            self._socket_thread.start()
        except Exception as exc:
            self._log(f"failed to start socket server: {exc}")

    def _accept_loop(self):
        """Accept connections and handle requests."""
        _conn_sem = threading.Semaphore(64)
        while not self._stop and self._server_socket:
            try:
                conn, _ = self._server_socket.accept()
                def _handle_with_sem(conn=conn):
                    _conn_sem.acquire()
                    try:
                        self._handle_client(conn)
                    finally:
                        _conn_sem.release()
                threading.Thread(target=_handle_with_sem,
                                  daemon=True).start()
            except socket.timeout:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                continue
            except OSError:
                break

    def _handle_client(self, conn: socket.socket):
        """Handle one client request."""
        try:
            conn.settimeout(10.0)
            data = conn.recv(65536).decode("utf-8", errors="replace").strip()
            if not data:
                return
            request = json.loads(data)
            cmd = request.get("cmd", "")
            response = self._handle_command(cmd, request)
            conn.sendall(json.dumps(response, ensure_ascii=False).encode("utf-8"))
        except Exception as exc:
            try:
                conn.sendall(json.dumps({"error": str(exc)}).encode("utf-8"))
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        finally:
            conn.close()

    def _handle_command(self, cmd: str, request: Dict) -> Dict:
        """Handle a daemon socket command."""
        if cmd == "status":
            status = self.state.to_dict()
            # D31: include sync-worker status so clients can see queued jobs
            status["sync"] = self.get_sync_status()
            return status
        elif cmd == "sync-status":
            # D31: dedicated sync-status command
            return {"ok": True, "sync": self.get_sync_status()}
        elif cmd == "force-refresh":
            path = request.get("path", "")
            if path:
                with self._pending_lock:
                    self._pending.add(os.path.abspath(path))
                return {"ok": True, "message": f"queued {path} for refresh"}
            return {"ok": False, "error": "path required"}
        elif cmd == "pause":
            self.state.paused = True
            self.state.paused_reason = request.get("reason", "manual")
            self._write_state()
            return {"ok": True, "message": "daemon paused"}
        elif cmd == "resume":
            self.state.paused = False
            self.state.paused_reason = ""
            self._write_state()
            return {"ok": True, "message": "daemon resumed"}
        elif cmd == "wait-sync":
            # Block until pending_events == 0 AND sync worker is idle
            timeout = float(request.get("timeout", 30.0))
            start = time.time()
            while time.time() - start < timeout:
                with self._pending_lock:
                    pending_empty = len(self._pending) == 0
                with self._sync_jobs_lock:
                    jobs_empty = len(self._sync_jobs) == 0
                with self._sync_busy_lock:
                    not_busy = not self._sync_busy
                if pending_empty and jobs_empty and not_busy:
                    return {"ok": True, "message": "sync complete"}
                time.sleep(0.2)
            return {"ok": False, "error": "timeout waiting for sync"}
        else:
            return {"ok": False, "error": f"unknown command: {cmd}"}

    def _write_state(self):
        """Write current state to disk."""
        self.state.write(self.graph_dir)

    def _log(self, msg: str):
        """Log to daemon log file (with simple size-based rotation)."""
        log_path = Path.home() / ".callgraph" / f"daemon-{Path(self.graph_dir).name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Rotate: if log exceeds 10MB, truncate to prevent unbounded growth
        try:
            if log_path.exists() and log_path.stat().st_size > 10 * 1024 * 1024:
                _mode = "w"
            else:
                _mode = "a"
        except OSError:
            _mode = "a"
        try:
            with open(log_path, _mode, encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    def _cleanup(self):
        """Clean up socket and watcher on shutdown."""
        if self._watcher:
            self._watcher.stop()
        if self._sync_worker_thread:
            self._sync_worker_thread.join(timeout=2.0)
        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        if os.path.exists(self._socket_path):
            try:
                os.unlink(self._socket_path)
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        self.state.status = STATUS_STOPPED
        self._write_state()


# ---------------------------------------------------------------------------
# Daemon client (for daemon-status/force-refresh/etc. CLI commands)
# ---------------------------------------------------------------------------

def _daemon_socket_path(graph_dir: str) -> str:
    """Compute the daemon socket path for a graph_dir.

    Shared by the daemon (server bind) and daemon_query (client connect)
    so they can never diverge again (a previous fix changed only the
    server side, breaking the whole socket API).
    """
    import hashlib as _hashlib
    _tmpdir = os.environ.get("TMPDIR") or "/tmp"
    _path_hash = _hashlib.md5(os.path.abspath(graph_dir).encode()).hexdigest()[:8]
    return os.path.join(_tmpdir, f"code2database-daemon-{_path_hash}.sock")


def daemon_query(graph_dir: str, cmd: str, **kwargs) -> Dict:
    """Send a command to a running daemon via Unix socket.

    Returns the daemon's response dict. If daemon is not running, returns
    {"error": "daemon not running"}.
    """
    socket_path = _daemon_socket_path(graph_dir)
    if not os.path.exists(socket_path):
        # Check if daemon is supposed to be running
        state = DaemonState.read(graph_dir)
        if state.status == STATUS_RUNNING and state.pid:
            # State says running but socket missing — stale state
            return {"error": "daemon socket not found (state may be stale)",
                    "state": state.to_dict()}
        return {"error": "daemon not running"}
    request = {"cmd": cmd, **kwargs}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(30.0)
            s.connect(socket_path)
            s.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8"))
            response = s.recv(65536).decode("utf-8", errors="replace")
            return json.loads(response)
    except Exception as exc:
        return {"error": str(exc)}


def is_daemon_running(graph_dir: str) -> bool:
    """Check if daemon process is alive."""
    state = DaemonState.read(graph_dir)
    if state.status != STATUS_RUNNING:
        return False
    if not state.pid:
        return False
    try:
        os.kill(state.pid, 0)  # signal 0 = check if alive
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_daemon_start(args):
    """Start the daemon (foreground; blocks)."""
    graph_dir = args.graph
    source_root = args.source
    # Check if a daemon is already running — prevent double-start
    # which hijacks the socket and causes two daemons racing on
    # the same graph DB and state file.
    if is_daemon_running(graph_dir):
        print(f"Error: daemon is already running for {graph_dir}",
              file=sys.stderr)
        print("Use 'daemon-stop' first, or 'daemon-status' to check.",
              file=sys.stderr)
        sys.exit(1)
    config = {}
    profile_path = getattr(args, "profile", "") or ""
    if profile_path and os.path.exists(profile_path):
        try:
            profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
            config = profile.get("daemon", {}) or {}
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    daemon = Daemon(graph_dir, source_root, config=config,
                    profile_path=profile_path)
    print(f"[daemon] starting: graph={graph_dir} source={source_root}", file=sys.stderr)
    _sock = _daemon_socket_path(graph_dir)
    print(f"[daemon] socket: {_sock}", file=sys.stderr)
    print(f"[daemon] status file: {graph_dir}/.daemon_status.json", file=sys.stderr)
    print(f"[daemon] log file: ~/.code2database/daemon-{Path(graph_dir).name}.log",
          file=sys.stderr)
    print("[daemon] press Ctrl+C to stop", file=sys.stderr)
    daemon.start()


def cmd_daemon_stop(args):
    """Stop a running daemon."""
    graph_dir = args.graph
    state = DaemonState.read(graph_dir)
    if not state.pid:
        print(json.dumps({"ok": False, "error": "no daemon PID in state file"}))
        sys.exit(1)
    try:
        os.kill(state.pid, signal.SIGTERM)
        print(json.dumps({"ok": True, "pid": state.pid, "message": "sent SIGTERM"},
                         indent=2))
    except OSError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        sys.exit(1)


def cmd_daemon_status(args):
    """Get daemon status (via socket if running, else from state file)."""
    graph_dir = args.graph
    if is_daemon_running(graph_dir):
        result = daemon_query(graph_dir, "status")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        state = DaemonState.read(graph_dir)
        print(json.dumps({
            "running": False,
            "state": state.to_dict(),
        }, ensure_ascii=False, indent=2, default=str))


def cmd_daemon_force_refresh(args):
    """Force refresh a specific file."""
    graph_dir = args.graph
    path = args.path
    result = daemon_query(graph_dir, "force-refresh", path=path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_daemon_pause(args):
    """Pause daemon (e.g., before manual updates)."""
    graph_dir = args.graph
    reason = getattr(args, "reason", "manual") or "manual"
    result = daemon_query(graph_dir, "pause", reason=reason)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_daemon_resume(args):
    """Resume daemon after pause."""
    graph_dir = args.graph
    result = daemon_query(graph_dir, "resume")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_daemon_wait_sync(args):
    """Wait for current sync to complete."""
    graph_dir = args.graph
    timeout = getattr(args, "timeout", 30.0)
    result = daemon_query(graph_dir, "wait-sync", timeout=timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_daemon_logs(args):
    """Show daemon logs."""
    graph_dir = args.graph
    log_path = Path.home() / ".callgraph" / f"daemon-{Path(graph_dir).name}.log"
    if not log_path.exists():
        print(f"No log file at {log_path}", file=sys.stderr)
        sys.exit(1)
    follow = getattr(args, "follow", False)
    if follow:
        import subprocess
        try:
            subprocess.run(["tail", "-f", str(log_path)])
        except KeyboardInterrupt:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    else:
        # Show last N lines
        n = getattr(args, "n", 50)
        try:
            subprocess.run(["tail", f"-n", str(n), str(log_path)])
        except Exception:
            content = log_path.read_text(encoding="utf-8", errors="replace")
            lines = content.split("\n")
            print("\n".join(lines[-n:]))


def cmd_daemon_reload(args):
    """Reload daemon config (re-reads profile)."""
    graph_dir = args.graph
    # Reload = stop + start (simple implementation)
    state = DaemonState.read(graph_dir)
    if state.pid:
        try:
            os.kill(state.pid, signal.SIGHUP)
            print(json.dumps({"ok": True, "message": "sent SIGHUP (daemon will reload config)"}))
            return
        except OSError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
            sys.exit(1)
    print(json.dumps({"ok": False, "error": "daemon not running"}))


def cmd_daemon_list_projects(args):
    """List all projects with daemon state files."""
    # Scan common graph dirs for .daemon_status.json
    found = []
    home_callgraph = Path.home() / ".callgraph"
    if home_callgraph.exists():
        for f in home_callgraph.iterdir():
            if f.name.startswith("daemon-") and f.name.endswith(".log"):
                project = f.name[len("daemon-"):-len(".log")]
                found.append({"project": project, "log_file": str(f)})
    # Also check current dir
    state = DaemonState.read(getattr(args, "graph", "."))
    if state.pid:
        found.append({"project": Path(getattr(args, "graph", ".")).name,
                       "state": state.to_dict()})
    print(json.dumps({"projects": found}, ensure_ascii=False, indent=2, default=str))
