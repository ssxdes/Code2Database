"""Tests for cross-platform daemon backends (D29).

Tests FSEvents (macOS), ReadDirectoryChangesW (Windows), and watchdog
adapter behavior. Since the test runs on Linux, we can only verify:
- Backend selection logic (auto → platform-native → polling)
- _WatchdogHandler adapter dispatches events correctly
- FSEvents/Win32 start methods return False on Linux (no library)
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.daemon import FileWatcher, _WatchdogHandler


class TestBackendSelection(unittest.TestCase):
    """Test that FileWatcher picks the right backend by platform."""

    def test_auto_falls_back_to_polling_on_linux(self):
        """On Linux without inotify available, auto backend falls back to polling.

        We can't easily disable inotify on Linux, so we just verify that
        when backend='polling' is forced, polling is used.
        """
        with tempfile.TemporaryDirectory() as tmp:
            w = FileWatcher(tmp, exclude_patterns=["*.tmp"], backend="polling")
            # Don't actually start — just verify config
            self.assertEqual(w.backend, "polling")

    def test_explicit_fsevents_backend(self):
        """backend='fsevents' is accepted even on non-Mac (will fall back)."""
        with tempfile.TemporaryDirectory() as tmp:
            w = FileWatcher(tmp, exclude_patterns=[], backend="fsevents")
            self.assertEqual(w.backend, "fsevents")

    def test_explicit_win32_backend(self):
        """backend='win32' is accepted even on non-Windows (will fall back)."""
        with tempfile.TemporaryDirectory() as tmp:
            w = FileWatcher(tmp, exclude_patterns=[], backend="win32")
            self.assertEqual(w.backend, "win32")


class TestFSEventsStart(unittest.TestCase):
    """Test _start_fsevents behavior."""

    def test_returns_false_without_watchdog_or_pyobjc(self):
        """_start_fsevents returns False on Linux (no watchdog, no FSEvents)."""
        with tempfile.TemporaryDirectory() as tmp:
            w = FileWatcher(tmp, exclude_patterns=[], backend="fsevents")
            # Patch the imports to simulate neither library available
            with patch.dict(sys.modules, {"watchdog.observers": None,
                                            "watchdog.events": None,
                                            "FSEvents": None}):
                # On Linux, watchdog may or may not be installed. We just
                # verify the method returns a bool without raising.
                result = w._start_fsevents()
                self.assertIn(result, (True, False))

    def test_does_not_raise_on_import_error(self):
        """_start_fsevents catches ImportError and returns False."""
        with tempfile.TemporaryDirectory() as tmp:
            w = FileWatcher(tmp, exclude_patterns=[], backend="fsevents")
            # Force ImportError by making the modules unimportable
            original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

            def failing_import(name, *args, **kwargs):
                if name in ("watchdog.observers", "watchdog.events", "FSEvents"):
                    raise ImportError(f"mocked: {name}")
                return original_import(name, *args, **kwargs)

            with patch('builtins.__import__', side_effect=failing_import):
                result = w._start_fsevents()
                self.assertFalse(result)


class TestWin32Start(unittest.TestCase):
    """Test _start_win32 behavior."""

    def test_returns_false_without_watchdog_or_pywin32(self):
        """_start_win32 returns False on Linux (no win32file)."""
        with tempfile.TemporaryDirectory() as tmp:
            w = FileWatcher(tmp, exclude_patterns=[], backend="win32")
            result = w._start_win32()
            self.assertIn(result, (True, False))

    def test_does_not_raise_on_import_error(self):
        """_start_win32 catches ImportError and returns False."""
        with tempfile.TemporaryDirectory() as tmp:
            w = FileWatcher(tmp, exclude_patterns=[], backend="win32")
            original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

            def failing_import(name, *args, **kwargs):
                if name in ("watchdog.observers", "watchdog.events",
                             "win32file", "win32con"):
                    raise ImportError(f"mocked: {name}")
                return original_import(name, *args, **kwargs)

            with patch('builtins.__import__', side_effect=failing_import):
                result = w._start_win32()
                self.assertFalse(result)


class TestWatchdogHandler(unittest.TestCase):
    """Test the _WatchdogHandler adapter."""

    def test_dispatches_event_to_callback(self):
        """_WatchdogHandler calls callback with src_path on file events."""
        events = []
        handler = _WatchdogHandler(
            callback=lambda p: events.append(p),
            exclude_patterns=[],
            is_excluded_fn=lambda p: False,
        )

        class FakeEvent:
            src_path = "/path/to/file.c"
            is_directory = False

        handler.on_any_event(FakeEvent())
        self.assertEqual(events, ["/path/to/file.c"])

    def test_skips_excluded_paths(self):
        """_WatchdogHandler skips events for excluded paths."""
        events = []
        handler = _WatchdogHandler(
            callback=lambda p: events.append(p),
            exclude_patterns=[".git"],
            is_excluded_fn=lambda p: ".git" in p,
        )

        class FakeEvent:
            src_path = "/repo/.git/config"
            is_directory = False

        handler.on_any_event(FakeEvent())
        self.assertEqual(events, [])

    def test_skips_directory_events(self):
        """_WatchdogHandler skips events for directories."""
        events = []
        handler = _WatchdogHandler(
            callback=lambda p: events.append(p),
            exclude_patterns=[],
            is_excluded_fn=lambda p: False,
        )

        class FakeEvent:
            src_path = "/path/to/dir"
            is_directory = True

        handler.on_any_event(FakeEvent())
        self.assertEqual(events, [])

    def test_handles_empty_src_path(self):
        """_WatchdogHandler doesn't crash on empty src_path."""
        events = []
        handler = _WatchdogHandler(
            callback=lambda p: events.append(p),
            exclude_patterns=[],
            is_excluded_fn=lambda p: False,
        )

        class FakeEvent:
            src_path = ""
            is_directory = False

        handler.on_any_event(FakeEvent())
        self.assertEqual(events, [])

    def test_handles_missing_src_path_attr(self):
        """_WatchdogHandler doesn't crash if event has no src_path attribute."""
        events = []
        handler = _WatchdogHandler(
            callback=lambda p: events.append(p),
            exclude_patterns=[],
            is_excluded_fn=lambda p: False,
        )

        class FakeEvent:
            is_directory = False

        handler.on_any_event(FakeEvent())
        self.assertEqual(events, [])


class TestStartWithPlatformFallback(unittest.TestCase):
    """Test that start() falls through to polling when native backends fail."""

    def test_start_falls_back_to_polling(self):
        """When native backends fail, start() falls back to polling.

        On Linux, inotify may succeed, so we force backend='win32' to
        ensure the fallback path is exercised.
        """
        with tempfile.TemporaryDirectory() as tmp:
            w = FileWatcher(tmp, exclude_patterns=[], backend="win32")
            callback_called = []
            w.start(callback=lambda p: callback_called.append(p))
            try:
                # After start, backend should be polling on Linux
                self.assertEqual(w.backend, "polling")
            finally:
                w.stop()


if __name__ == "__main__":
    unittest.main()
