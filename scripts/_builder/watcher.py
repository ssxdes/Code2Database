#!/usr/bin/env python3
"""Auto-sync watch service for Code2Database.

Monitors source directory for changes and triggers incremental re-scanning.
Uses watchdog library when available, falls back to polling.
"""

import json
import os
import sys
import time
from pathlib import Path


class WatchService:
    """Watch source directory and trigger incremental updates."""

    def __init__(self, source_root: str, output_dir: str, debounce: float = 2.0):
        self.source_root = os.path.abspath(source_root)
        self.output_dir = os.path.abspath(output_dir)
        self.debounce = debounce
        self._pending_changes = set()
        self._last_process_time = 0
        self._watcher = None

    def start(self):
        """Start watching for changes."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            handler = _ChangeHandler(self)
            self._watcher = Observer()
            self._watcher.schedule(handler, self.source_root, recursive=True)
            self._watcher.start()
            print(f"[Watch] Started watching {self.source_root} (watchdog mode)", file=sys.stderr)
        except ImportError:
            print(f"[Watch] watchdog not available, using polling mode", file=sys.stderr)
            self._watcher = None

    def stop(self):
        """Stop watching."""
        if self._watcher:
            self._watcher.stop()
            self._watcher.join()

    def process_changes(self):
        """Process accumulated changes by running incremental scan."""
        if not self._pending_changes:
            return

        changes = self._pending_changes.copy()
        self._pending_changes.clear()

        # Filter to source files only
        source_exts = {'.c', '.h', '.cpp', '.hpp', '.cc', '.cxx', '.go', '.py', '.java', '.rs'}
        source_changes = [f for f in changes if Path(f).suffix.lower() in source_exts]

        if not source_changes:
            return

        print(f"[Watch] Processing {len(source_changes)} changed files", file=sys.stderr)

        # Run incremental scan
        try:
            from code2database_scanner import scan_files
            import networkx as nx

            result = scan_files(source_changes, self.source_root)

            # Merge with existing extraction
            extraction_path = os.path.join(self.output_dir, ".code2database_extraction.json")
            if os.path.exists(extraction_path):
                with open(extraction_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)

                # Remove old entries for changed files
                changed_set = set(source_changes)
                for key in ("functions", "edges", "import_edges"):
                    existing[key] = [x for x in existing.get(key, [])
                                     if x.get("source_file", x.get("_source_file", "")) not in changed_set]

                # Append new data
                for key in ("functions", "edges", "import_edges"):
                    existing.setdefault(key, []).extend(result.get(key, []))

                with open(extraction_path, "w", encoding="utf-8") as f:
                    json.dump(existing, f, ensure_ascii=False)

                print(f"[Watch] Updated extraction: {len(existing.get('functions', []))} functions", file=sys.stderr)
            else:
                print(f"[Watch] No existing extraction file, skipping merge", file=sys.stderr)

        except Exception as e:
            print(f"[Watch] Error processing changes: {e}", file=sys.stderr)

    def on_file_change(self, filepath: str):
        """Called when a file change is detected."""
        self._pending_changes.add(filepath)
        self._last_process_time = time.time()

    def maybe_process(self):
        """Process changes if debounce period has elapsed."""
        if self._pending_changes and time.time() - self._last_process_time >= self.debounce:
            self.process_changes()

    def poll(self):
        """Polling-based change detection (fallback)."""
        manifest_path = os.path.join(self.output_dir, ".code2database_manifest.json")
        if not os.path.exists(manifest_path):
            return

        try:
            from _scanner.changes import detect_changes
            changes = detect_changes(self.source_root, self.output_dir)
            changed = changes["new_files"] + changes["changed_files"]
            for f in changed:
                self.on_file_change(f)
        except Exception:
            pass


class _ChangeHandler:
    """Watchdog event handler."""

    def __init__(self, service: WatchService):
        self.service = service

    def on_modified(self, event):
        if not event.is_directory:
            self.service.on_file_change(event.src_path)

    def on_created(self, event):
        if not event.is_directory:
            self.service.on_file_change(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self.service.on_file_change(event.src_path)


def cmd_watch(args):
    """Handle watch command."""
    source = args.source
    output = args.output or os.path.join(source, "code2db-out")
    debounce = getattr(args, 'debounce', 2.0)

    service = WatchService(source, output, debounce=debounce)
    service.start()

    try:
        while True:
            time.sleep(1)
            if not service._watcher:
                # Polling fallback
                service.poll()
            service.maybe_process()
    except KeyboardInterrupt:
        print("\n[Watch] Stopping...", file=sys.stderr)
        service.stop()
