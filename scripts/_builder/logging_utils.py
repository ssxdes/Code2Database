"""Structured logging utilities for Code2Database.

Provides:
- `get_logger(name)` — module-level logger with consistent format
- `configure_logging(level, json_format, log_file)` — one-time setup
- `LogContext` — context manager for structured fields
- `StageTimer` — context manager that logs stage duration

Why: replaces ad-hoc `print(..., file=sys.stderr)` with leveled, structured
logging so kernel-scale scans can be post-analyzed (requirement).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional


_CONFIGURED = False
_DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DEFAULT_DATEFMT = "%Y-%m-%dT%H:%M:%S"


class _JsonFormatter(logging.Formatter):
    """Emit log records as JSON lines for machine-parseable logs."""

    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "message",
        "asctime",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, _DEFAULT_DATEFMT),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Attach extra fields passed via logger.info(..., extra={...}) or
        # via LogContext.
        for key, val in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class _StreamHandler(logging.StreamHandler):
    """StreamHandler that defaults to stderr (not stdout)."""

    def __init__(self, stream=None):
        super().__init__(stream if stream is not None else sys.stderr)


def configure_logging(
    level: str = "INFO",
    json_format: bool = False,
    log_file: Optional[str] = None,
    force: bool = False,
) -> logging.Logger:
    """One-time logging setup. Idempotent unless `force=True`.

    Args:
        level: "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL"
        json_format: If True, emit JSON lines (machine-parseable).
        log_file: Optional path; if set, also writes to this file.
        force: Re-configure even if already configured.
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return logging.getLogger("callgraph")

    root = logging.getLogger("callgraph")
    # Clear any prior handlers so re-configure is clean.
    for h in list(root.handlers):
        root.removeHandler(h)

    level_num = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(level_num)

    handler = _StreamHandler()
    if json_format:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT, datefmt=_DEFAULT_DATEFMT))
    root.addHandler(handler)

    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(
                _JsonFormatter() if json_format
                else logging.Formatter(_DEFAULT_FORMAT, datefmt=_DEFAULT_DATEFMT)
            )
            root.addHandler(file_handler)
        except OSError as exc:
            # Don't let logging setup crash the build.
            root.warning("Failed to open log file %s: %s", log_file, exc)

    root.propagate = False
    _CONFIGURED = True
    return root


def get_logger(name: str = "callgraph") -> logging.Logger:
    """Get a child logger under the 'callgraph' namespace.

    Ensures a NullHandler is attached even if `configure_logging` was not
    called, so library users don't see 'No handlers could be found' warnings.
    """
    if not name.startswith("callgraph"):
        name = f"callgraph.{name}"
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


@contextmanager
def LogContext(logger: logging.Logger, **fields: Any) -> Iterator[logging.LoggerAdapter]:
    """Attach structured fields to all log records within the block.

    Example:
        with LogContext(logger, stage="vtable_dispatch") as log:
            log.info("start")
            log.info("done", matches=42)
    """
    adapter = logging.LoggerAdapter(logger, fields)
    yield adapter


class StageTimer:
    """Context manager that logs stage duration and optional stats.

    Example:
        with StageTimer(logger, "build_vtable_dispatch", items=1000) as timer:
            ...
            timer.items = 1234  # update count
        # emits: stage=build_vtable_dispatch items=1234 ms=456.7
    """

    def __init__(self, logger: logging.Logger, stage: str, **extra: Any):
        self._logger = logger
        self._stage = stage
        self._extra = dict(extra)
        self._start = 0.0
        self.items: Optional[int] = extra.get("items") if isinstance(extra.get("items"), int) else None

    def __enter__(self) -> "StageTimer":
        self._start = time.perf_counter()
        self._logger.info("stage_start", extra={"stage": self._stage, **self._extra})
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        ms = (time.perf_counter() - self._start) * 1000.0
        payload = {"stage": self._stage, "ms": round(ms, 2), **self._extra}
        if self.items is not None:
            payload["items"] = self.items
        if exc_type is None:
            self._logger.info("stage_done", extra=payload)
        else:
            payload["error"] = exc_type.__name__
            self._logger.error("stage_failed", extra=payload)
        return False  # don't suppress


def parse_log_level(level: Optional[str]) -> str:
    """Normalize a user-provided log level string."""
    if not level:
        return "INFO"
    upper = level.upper().strip()
    if upper in ("DEBUG", "INFO", "WARNING", "WARN", "ERROR", "CRITICAL"):
        return "INFO" if upper == "WARN" else upper
    return "INFO"


def is_configured() -> bool:
    """Check whether `configure_logging` has been called."""
    return _CONFIGURED
