"""Memory guard module - adaptive memory management for large project scanning.

This module provides:
1. Memory monitoring and alerting
2. Adaptive batch processing based on available memory
3. Streaming output to avoid memory accumulation
4. Automatic garbage collection triggers
5. Graceful degradation when memory is low
"""

import gc
import os
import sys
import threading
import time
import json
from pathlib import Path
from typing import Optional, Callable, Any, List, Dict


class MemoryGuard:
    """Memory guard for monitoring and managing memory usage during scanning.

    Usage:
        guard = MemoryGuard(warn_threshold=0.75, crit_threshold=0.85)
        guard.start_monitoring()

        # During processing
        guard.check_and_adapt()
        guard.maybe_gc()

        guard.stop_monitoring()

    Threshold semantics:
        - If `warn_threshold` / `crit_threshold` are floats in (0, 1]: treated
          as fractions of total system RAM.
        - If `warn_threshold_mb` / `crit_threshold_mb` are provided (in MB):
          used as absolute caps. Absolute caps override fractional thresholds
          and are safer for systems with very large or very small RAM where a
          fixed fraction would either OOM too eagerly or never trigger.
        - When neither absolute cap is given, `crit_threshold` is also clamped
          so that the absolute critical threshold never exceeds
          `total_mb * 0.9` AND never exceeds `available_mb_at_start + total_mb * 0.5`
          (prevents OOM-killer on systems where free RAM is small).
    """

    # Default memory thresholds (as fraction of total)
    DEFAULT_WARN_THRESHOLD = 0.75
    DEFAULT_CRIT_THRESHOLD = 0.85
    # Hard ceiling: critical threshold can never exceed 90% of total RAM.
    _MAX_CRIT_FRACTION = 0.90

    def __init__(self,
                 warn_threshold: float = DEFAULT_WARN_THRESHOLD,
                 crit_threshold: float = DEFAULT_CRIT_THRESHOLD,
                 gc_interval: float = 2.0,
                 batch_reduction_factor: float = 0.5,
                 stats_file: Optional[str] = None,
                 warn_threshold_mb: Optional[float] = None,
                 crit_threshold_mb: Optional[float] = None,
                 dynamic: bool = True):
        """Initialize memory guard.

        Args:
            warn_threshold: Memory usage fraction to trigger warnings (0.0-1.0)
            crit_threshold: Memory usage fraction to trigger critical actions (0.0-1.0)
            gc_interval: Minimum seconds between garbage collection calls
            batch_reduction_factor: Factor to reduce batch size when memory is high
            stats_file: Optional file to write memory stats
            warn_threshold_mb: Absolute warn cap in MB (overrides fraction if set)
            crit_threshold_mb: Absolute critical cap in MB (overrides fraction if set)
            dynamic: If True, clamp fractional crit threshold to a safe absolute
                ceiling based on actually-available RAM at startup.
        """
        self.warn_threshold = warn_threshold
        self.crit_threshold = crit_threshold
        self.gc_interval = gc_interval
        self.batch_reduction_factor = batch_reduction_factor
        self.stats_file = stats_file
        self.warn_threshold_mb = warn_threshold_mb
        self.crit_threshold_mb = crit_threshold_mb

        # Snapshot baseline available memory for dynamic clamping.
        self._baseline_info = self.get_memory_info()
        self._dynamic = dynamic
        if dynamic and crit_threshold_mb is None:
            self._dynamic_crit_ceiling_mb = self._compute_dynamic_crit_ceiling()
        else:
            self._dynamic_crit_ceiling_mb = None

        self._lock = threading.Lock()
        self._monitoring = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._last_gc_time = 0.0
        self._last_check_time = 0.0
        self._stats = {
            "peak_memory_mb": 0,
            "gc_count": 0,
            "batch_reductions": 0,
            "warnings": 0,
            "criticals": 0,
        }
        self._current_batch_multiplier = 1.0
        self._memory_warnings: List[str] = []

    def get_memory_info(self) -> Dict[str, Any]:
        """Get current memory usage information.

        Returns:
            Dict with keys: total_mb, available_mb, used_mb, usage_percent, rss_mb
        """
        info = {
            "total_mb": 0,
            "available_mb": 0,
            "used_mb": 0,
            "usage_percent": 0.0,
            "rss_mb": 0,
        }

        try:
            # Try to get memory info from /proc/meminfo (Linux)
            if os.path.exists("/proc/meminfo"):
                with open("/proc/meminfo", "r") as f:
                    meminfo = {}
                    for line in f:
                        parts = line.split()
                        if len(parts) >= 2:
                            key = parts[0].rstrip(":")
                            try:
                                meminfo[key] = int(parts[1])
                            except ValueError:
                                pass

                # Values are in kB
                total_kb = meminfo.get("MemTotal", 0)
                available_kb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
                free_kb = meminfo.get("MemFree", 0)
                buffers_kb = meminfo.get("Buffers", 0)
                cached_kb = meminfo.get("Cached", 0)

                info["total_mb"] = total_kb / 1024
                info["available_mb"] = available_kb / 1024
                info["used_mb"] = (total_kb - available_kb) / 1024
                if total_kb > 0:
                    info["usage_percent"] = (total_kb - available_kb) / total_kb
            else:
                # Fallback for non-Linux systems (macOS, Windows, WSL-non-procfs).
                # Try psutil first (cross-platform, accurate), then fall back
                # to resource.getrusage (POSIX, RSS only), then assume 50%.
                try:
                    import psutil
                    vm = psutil.virtual_memory()
                    info["total_mb"] = vm.total / (1024 * 1024)
                    info["available_mb"] = vm.available / (1024 * 1024)
                    info["used_mb"] = (vm.total - vm.available) / (1024 * 1024)
                    info["usage_percent"] = vm.percent / 100.0
                except ImportError:
                    import resource
                    usage = resource.getrusage(resource.RUSAGE_SELF)
                    info["rss_mb"] = usage.ru_maxrss / 1024
                    info["usage_percent"] = 0.5  # Unknown, assume 50%

        except Exception as e:
            pass

        return info

    def _compute_dynamic_crit_ceiling(self) -> Optional[float]:
        """Compute a safe absolute ceiling (in MB) for the critical threshold.

        The critical trigger should never be so high that the OOM-killer fires
        before we react. We clamp to min(total * 0.9, baseline_available + total * 0.5).

        Returns ceiling in MB, or None if memory info is unavailable.
        """
        info = self._baseline_info
        total = info.get("total_mb", 0)
        if total <= 0:
            return None
        ceiling1 = total * self._MAX_CRIT_FRACTION
        # If baseline available is large, allow using up to that + half of total.
        # If baseline available is small (already loaded system), be stricter.
        baseline_avail = info.get("available_mb", 0)
        ceiling2 = baseline_avail + total * 0.5
        return min(ceiling1, ceiling2)

    def _effective_crit_threshold_mb(self) -> Optional[float]:
        """Return the effective critical threshold in MB (absolute), or None to
        fall back to fractional comparison.
        """
        if self.crit_threshold_mb is not None:
            return float(self.crit_threshold_mb)
        if self._dynamic and self._dynamic_crit_ceiling_mb is not None:
            return self._dynamic_crit_ceiling_mb
        return None

    def _effective_warn_threshold_mb(self) -> Optional[float]:
        """Return the effective warn threshold in MB (absolute), or None."""
        if self.warn_threshold_mb is not None:
            return float(self.warn_threshold_mb)
        # For warn, derive from fraction of total when dynamic.
        if self._dynamic:
            total = self._baseline_info.get("total_mb", 0)
            if total > 0:
                # Warn at min(warn_fraction * total, crit_ceiling * 0.9)
                frac_mb = total * self.warn_threshold
                crit_mb = self._effective_crit_threshold_mb()
                if crit_mb is not None:
                    return min(frac_mb, crit_mb * 0.95)
                return frac_mb
        return None

    def is_memory_critical(self) -> bool:
        """Check if memory usage is at critical level."""
        info = self.get_memory_info()
        crit_mb = self._effective_crit_threshold_mb()
        if crit_mb is not None:
            return info["used_mb"] >= crit_mb
        return info["usage_percent"] >= self.crit_threshold

    def is_memory_low(self) -> bool:
        """Check if memory usage is at warning level."""
        info = self.get_memory_info()
        warn_mb = self._effective_warn_threshold_mb()
        if warn_mb is not None:
            return info["used_mb"] >= warn_mb
        return info["usage_percent"] >= self.warn_threshold

    def check_and_adapt(self) -> float:
        """Check memory and return adaptive batch multiplier.

        Returns:
            Batch multiplier (0.0-1.0) to use for reducing batch sizes
        """
        with self._lock:
            info = self.get_memory_info()

            # Update peak memory
            if info["used_mb"] > self._stats["peak_memory_mb"]:
                self._stats["peak_memory_mb"] = info["used_mb"]

            # Compute effective thresholds (absolute MB if available, else fraction).
            crit_mb = self._effective_crit_threshold_mb()
            warn_mb = self._effective_warn_threshold_mb()

            is_crit: bool
            is_warn: bool
            if crit_mb is not None and warn_mb is not None:
                is_crit = info["used_mb"] >= crit_mb
                is_warn = (not is_crit) and info["used_mb"] >= warn_mb
            else:
                is_crit = info["usage_percent"] >= self.crit_threshold
                is_warn = (not is_crit) and info["usage_percent"] >= self.warn_threshold

            # Check thresholds
            if is_crit:
                self._stats["criticals"] += 1
                self._current_batch_multiplier = self.batch_reduction_factor ** self._stats["criticals"]
                cap_str = f" (cap={crit_mb:.0f}MB)" if crit_mb is not None else ""
                self._memory_warnings.append(
                    f"CRITICAL: Memory at {info['used_mb']:.0f}MB{cap_str}"
                )
                # Active degradation: invoke registered callbacks
                if hasattr(self, '_degradation_callbacks'):
                    for callback in self._degradation_callbacks:
                        try:
                            callback(info)
                        except Exception:
                            pass
            elif is_warn:
                self._stats["warnings"] += 1
                # Linear backoff between warn and crit.
                if crit_mb is not None and warn_mb is not None and crit_mb > warn_mb:
                    over_warn = max(0.0, info["used_mb"] - warn_mb)
                    span = max(1.0, crit_mb - warn_mb)
                    self._current_batch_multiplier = max(
                        self.batch_reduction_factor,
                        1.0 - over_warn / span
                    )
                else:
                    self._current_batch_multiplier = max(
                        self.batch_reduction_factor,
                        1.0 - (info["usage_percent"] - self.warn_threshold)
                    )
                warn_str = f" (warn={warn_mb:.0f}MB)" if warn_mb is not None else ""
                self._memory_warnings.append(
                    f"WARNING: Memory at {info['used_mb']:.0f}MB{warn_str}"
                )
            else:
                self._current_batch_multiplier = min(1.0, self._current_batch_multiplier + 0.1)

            # Log stats periodically
            if self.stats_file and len(self._memory_warnings) > 0:
                self._write_stats(info)

            return max(0.1, min(1.0, self._current_batch_multiplier))

    def maybe_gc(self, force: bool = False) -> bool:
        """Trigger garbage collection if needed or enough time has passed.

        Args:
            force: Force GC regardless of time since last GC

        Returns:
            True if GC was performed
        """
        with self._lock:
            now = time.time()
            if not force and (now - self._last_gc_time) < self.gc_interval:
                return False

            # Check if memory is getting high
            info = self.get_memory_info()
            if not force and info["usage_percent"] < self.warn_threshold:
                return False

            # Perform garbage collection
            collected = gc.collect()
            self._last_gc_time = now
            self._stats["gc_count"] += 1

            return True

    def _write_stats(self, info: Dict[str, Any]) -> None:
        """Write memory stats to file."""
        try:
            stats = {
                **self._stats,
                "memory": info,
                "warnings_list": self._memory_warnings[-10:],  # Keep last 10
                "timestamp": time.time(),
            }
            Path(self.stats_file).write_text(
                json.dumps(stats, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass

    def start_monitoring(self, interval: float = 5.0) -> None:
        """Start background memory monitoring thread.

        Args:
            interval: Seconds between memory checks
        """
        if self._monitoring:
            return

        self._monitoring = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            args=(interval,),
            daemon=True
        )
        self._monitor_thread.start()

    def _monitor_loop(self, interval: float) -> None:
        """Background monitoring loop."""
        _last_log_time = 0.0
        _last_log_level = ""
        _LOG_INTERVAL = 30.0  # Minimum seconds between repeated log messages
        while self._monitoring:
            try:
                info = self.get_memory_info()
                self.check_and_adapt()

                _now = time.time()
                _level = ""
                if info["usage_percent"] >= self.crit_threshold:
                    self.maybe_gc(force=False)
                    _level = "CRITICAL"
                elif info["usage_percent"] >= self.warn_threshold:
                    _level = "WARNING"

                if _level:
                    # Only log if enough time has passed since last same-level message
                    # or if the level changed (e.g., WARNING → CRITICAL)
                    _elapsed = _now - _last_log_time
                    if _level != _last_log_level or _elapsed >= _LOG_INTERVAL:
                        print(f"[MemoryGuard] {_level}: {info['usage_percent']*100:.1f}% used, "
                              f"{info['used_mb']:.0f}MB/{info['total_mb']:.0f}MB",
                              file=sys.stderr)
                        _last_log_time = _now
                        _last_log_level = _level

            except Exception:
                pass

            time.sleep(interval)

    def stop_monitoring(self) -> None:
        """Stop background monitoring."""
        self._monitoring = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=2.0)
            self._monitor_thread = None

    def get_stats(self) -> Dict[str, Any]:
        """Get memory guard statistics."""
        with self._lock:
            return {**self._stats, "batch_multiplier": self._current_batch_multiplier}

    def wait_for_memory(self, timeout: float = 30.0, target_percent: float = None) -> bool:
        """Wait for memory to drop below critical level.

        Args:
            timeout: Maximum seconds to wait
            target_percent: Wait until usage drops below this (default: warn_threshold)

        Returns:
            True if memory dropped below target, False if timeout
        """
        if target_percent is None:
            target_percent = self.warn_threshold
        deadline = time.time() + timeout
        while time.time() < deadline:
            info = self.get_memory_info()
            if info["usage_percent"] < target_percent:
                return True
            gc.collect()
            time.sleep(1.0)
        return False

    def drop_body_text(self, functions: list) -> int:
        """Drop body_text from function dicts to free memory.

        body_text is the largest field and is only needed at query time
        (where it can be re-read from source). Dropping it during
        scanning/building saves significant memory.

        Args:
            functions: List of function dicts

        Returns:
            Number of functions that had body_text dropped
        """
        dropped = 0
        for func in functions:
            if func.get("body_text"):
                del func["body_text"]
                dropped += 1
        if dropped:
            gc.collect()
        return dropped

    def register_degradation_callback(self, callback: Callable[[Dict], None]):
        """Register a callback to invoke when memory reaches critical level.

        The callback receives memory info dict and should take action
        to reduce memory (e.g., drop body_text, flush to disk).
        """
        if not hasattr(self, '_degradation_callbacks'):
            self._degradation_callbacks = []
        self._degradation_callbacks.append(callback)


class StreamingWriter:
    """Streaming JSON writer that flushes data incrementally to avoid memory buildup.

    Usage:
        writer = StreamingWriter(output_path, chunk_size=1000)
        writer.begin()
        writer.write_item({"key": "value"})
        writer.write_item({"key": "value2"})
        writer.end()
    """

    def __init__(self, output_path: str, chunk_size: int = 1000, indent: int = 2):
        """Initialize streaming writer.

        Args:
            output_path: Path to output JSON file
            chunk_size: Number of items to batch before flushing
            indent: JSON indentation level
        """
        self.output_path = output_path
        self.chunk_size = chunk_size
        self.indent = indent

        self._buffer: List[Any] = []
        self._item_count = 0
        self._file: Optional[Any] = None
        self._started = False
        self._first_item = True

    def begin(self) -> None:
        """Begin writing - opens file and writes opening bracket."""
        os.makedirs(os.path.dirname(self.output_path) or ".", exist_ok=True)
        self._file = open(self.output_path, "w", encoding="utf-8")
        self._file.write('[\n')
        self._started = True
        self._first_item = True

    def write_item(self, item: Any) -> None:
        """Write a single item to the stream."""
        if not self._started:
            self.begin()

        if not self._first_item:
            self._file.write(',\n')
        self._first_item = False

        json_str = json.dumps(item, ensure_ascii=False, indent=self.indent)
        self._file.write(json_str)
        self._item_count += 1

        # Flush periodically
        if self._item_count % self.chunk_size == 0:
            self._file.flush()

    def write_items(self, items: List[Any]) -> None:
        """Write multiple items at once."""
        for item in items:
            self.write_item(item)

    def end(self) -> int:
        """End writing - closes file and returns item count."""
        if self._started and self._file:
            self._file.write('\n]\n')
            self._file.close()
            self._file = None
            self._started = False

        return self._item_count

    def __enter__(self):
        self.begin()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end()
        return False


class StreamingJsonObjectWriter:
    """Streaming writer for complex nested JSON objects.

    Unlike StreamingWriter which writes a flat JSON array, this class
    supports writing a JSON object with mixed scalar and array fields,
    allowing arrays to be written item-by-item to avoid memory buildup.

    Usage:
        writer = StreamingJsonObjectWriter(output_path)
        writer.begin()
        writer.write_scalar("source_root", "/path/to/source")
        writer.write_scalar("domains", ["kernel", "drivers"])
        writer.begin_array("functions")
        for func in large_function_list:
            writer.write_array_item(func)
        writer.end_array()
        writer.begin_array("edges")
        for edge in large_edge_list:
            writer.write_array_item(edge)
        writer.end_array()
        writer.write_scalar("globals", globals_dict)
        writer.end()
    """

    def __init__(self, output_path: str, chunk_size: int = 1000):
        """Initialize streaming object writer.

        Args:
            output_path: Path to output JSON file
            chunk_size: Number of items to batch before flushing
        """
        self.output_path = output_path
        self.chunk_size = chunk_size
        self._file: Optional[Any] = None
        self._started = False
        self._first_field = True
        self._in_array = False
        self._first_array_item = True
        self._item_count = 0
        self._total_items = 0

    def begin(self) -> None:
        """Begin writing - opens file and writes opening brace."""
        os.makedirs(os.path.dirname(self.output_path) or ".", exist_ok=True)
        self._file = open(self.output_path, "w", encoding="utf-8")
        self._file.write('{\n')
        self._started = True
        self._first_field = True

    def write_scalar(self, key: str, value: Any) -> None:
        """Write a scalar field (string, number, list, dict).

        Args:
            key: Field name
            value: Value to write (will be JSON-serialized)
        """
        if not self._started:
            self.begin()
        if not self._first_field:
            self._file.write(',\n')
        self._first_field = False
        json_str = json.dumps(value, ensure_ascii=False)
        self._file.write(f'"{key}": {json_str}')

    def begin_array(self, key: str) -> None:
        """Begin writing an array field.

        Args:
            key: Field name
        """
        if not self._started:
            self.begin()
        if not self._first_field:
            self._file.write(',\n')
        self._first_field = False
        self._file.write(f'"{key}": [\n')
        self._in_array = True
        self._first_array_item = True
        self._item_count = 0

    def write_array_item(self, item: Any) -> None:
        """Write a single item to the current array."""
        if not self._first_array_item:
            self._file.write(',\n')
        self._first_array_item = False
        json_str = json.dumps(item, ensure_ascii=False)
        self._file.write(json_str)
        self._item_count += 1
        self._total_items += 1
        # Flush periodically
        if self._item_count % self.chunk_size == 0:
            self._file.flush()

    def end_array(self) -> int:
        """End the current array field.

        Returns:
            Number of items written in this array
        """
        self._file.write('\n]')
        self._in_array = False
        count = self._item_count
        self._item_count = 0
        return count

    def end(self) -> int:
        """End writing - closes file and returns total item count."""
        if self._started and self._file:
            self._file.write('\n}\n')
            self._file.flush()
            self._file.close()
            self._file = None
            self._started = False
        return self._total_items

    def __enter__(self):
        self.begin()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end()
        return False


class BatchedListCollector:
    """Collects items in batches and writes to disk when batch is full.

    This prevents memory accumulation when collecting large numbers of items.

    Usage:
        collector = BatchedListCollector(output_path, batch_size=10000)
        for item in large_dataset:
            collector.add(item)
        results = collector.flush()  # Returns all items and clears memory
    """

    def __init__(self, batch_size: int = 10000, temp_dir: str = None):
        """Initialize batched collector.

        Args:
            batch_size: Number of items to accumulate before flushing to disk
            temp_dir: Directory for temporary batch files (default: auto-create)
        """
        self.batch_size = batch_size
        self._buffer: List[Any] = []
        self._disk_batches: List[str] = []
        self._temp_dir = temp_dir
        self._batch_count = 0

    def add(self, item: Any) -> None:
        """Add an item to the collector."""
        self._buffer.append(item)
        if len(self._buffer) >= self.batch_size:
            self._flush_batch()

    def add_many(self, items: List[Any]) -> None:
        """Add multiple items at once."""
        for item in items:
            self.add(item)

    def _flush_batch(self) -> None:
        """Flush current buffer to disk."""
        if not self._buffer:
            return

        if self._temp_dir is None:
            self._temp_dir = os.path.join(".", ".tmp_batch")
            os.makedirs(self._temp_dir, exist_ok=True)

        batch_path = os.path.join(self._temp_dir, f"batch_{self._batch_count}.json")
        with open(batch_path, "w", encoding="utf-8") as f:
            json.dump(self._buffer, f, ensure_ascii=False)

        self._disk_batches.append(batch_path)
        self._buffer = []
        self._batch_count += 1

        # Force garbage collection after flush
        gc.collect()

    def flush(self) -> List[Any]:
        """Flush remaining items and return all collected items."""
        if self._buffer:
            self._flush_batch()

        # Load all batches back
        all_items = []
        for batch_path in self._disk_batches:
            try:
                with open(batch_path, "r", encoding="utf-8") as f:
                    all_items.extend(json.load(f))
                os.remove(batch_path)
            except Exception:
                pass

        # Also include in-memory buffer
        all_items.extend(self._buffer)

        # Cleanup
        self._buffer = []
        self._disk_batches = []
        self._batch_count = 0

        if self._temp_dir and os.path.exists(self._temp_dir):
            try:
                os.rmdir(self._temp_dir)
            except OSError:
                pass

        return all_items

    def __len__(self) -> int:
        """Return total number of items collected."""
        return len(self._buffer) + len(self._disk_batches) * self.batch_size

    def __del__(self):
        """Clean up any remaining temp files."""
        if self._temp_dir and os.path.exists(self._temp_dir):
            for batch_path in self._disk_batches:
                try:
                    os.remove(batch_path)
                except OSError:
                    pass
            try:
                os.rmdir(self._temp_dir)
            except OSError:
                pass


def adaptive_batch_size(base_size: int, guard: MemoryGuard) -> int:
    """Calculate adaptive batch size based on memory pressure.

    Args:
        base_size: Base batch size
        guard: MemoryGuard instance

    Returns:
        Adjusted batch size
    """
    multiplier = guard.check_and_adapt()
    return max(100, int(base_size * multiplier))


def memory_safe_append(large_list: List[Any], items: List[Any],
                       guard: Optional[MemoryGuard] = None,
                       max_size: int = 100000) -> None:
    """Memory-safe list extension with optional monitoring.

    Args:
        large_list: List to extend
        items: Items to add
        guard: Optional MemoryGuard for monitoring
        max_size: Maximum size before warning
    """
    if guard:
        guard.check_and_adapt()
        guard.maybe_gc()

    large_list.extend(items)

    # Warn if list is getting large
    if len(large_list) > max_size and len(large_list) % (max_size // 2) == 0:
        print(f"[MemoryGuard] WARNING: List growing large ({len(large_list)} items)",
              file=sys.stderr)


def create_memory_guard(args) -> MemoryGuard:
    """Create MemoryGuard from command line arguments.

    Args:
        args: argparse Namespace with memory-related options

    Returns:
        Configured MemoryGuard instance

    Reads (all optional):
        args.memory_warn_threshold  (float 0..1)
        args.memory_crit_threshold  (float 0..1)
        args.memory_warn_mb         (float, MB)  — absolute warn cap
        args.memory_crit_mb         (float, MB)  — absolute crit cap
        args.memory_stats           (str path)
        args.large_project          (bool)
        args.memory_dynamic         (bool, default True) — disable to use pure fractions
    """
    warn = getattr(args, 'memory_warn_threshold', None)
    crit = getattr(args, 'memory_crit_threshold', None)
    warn_mb = getattr(args, 'memory_warn_mb', None)
    crit_mb = getattr(args, 'memory_crit_mb', None)
    dynamic = getattr(args, 'memory_dynamic', True)

    guard = MemoryGuard(
        warn_threshold=warn if warn else MemoryGuard.DEFAULT_WARN_THRESHOLD,
        crit_threshold=crit if crit else MemoryGuard.DEFAULT_CRIT_THRESHOLD,
        stats_file=getattr(args, 'memory_stats', None),
        warn_threshold_mb=warn_mb,
        crit_threshold_mb=crit_mb,
        dynamic=dynamic,
    )

    # Set batch reduction factor based on project size hint
    if hasattr(args, 'large_project') and args.large_project:
        guard.batch_reduction_factor = 0.3  # More aggressive reduction

    return guard


# Global singleton for quick access
_global_guard: Optional[MemoryGuard] = None


def get_global_guard() -> Optional[MemoryGuard]:
    """Get the global MemoryGuard instance."""
    return _global_guard


def set_global_guard(guard: MemoryGuard) -> None:
    """Set the global MemoryGuard instance."""
    global _global_guard
    _global_guard = guard
