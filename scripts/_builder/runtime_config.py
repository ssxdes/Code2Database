"""Runtime configuration loader for config/runtime.json.

``config/runtime.json`` is the operational tuning surface documented in
``docs/*/RUNTIME_CONFIG.md``: how the pipeline runs (parallelism, query
output size), as opposed to the graph itself which is project data.

Discovery order:
1. ``$C2D_RUNTIME_CONFIG`` — explicit path to a JSON file
2. ``<scripts dir>/config/runtime.json`` — the shipped default, which
   also resolves correctly in an installed skill or wheel layout

Behaviour:
- Missing file or unreadable JSON → empty configuration (built-in
  defaults apply); a parse failure is logged once.
- Unknown sections or keys (not present in the documented field
  reference) are reported via a warning log and ignored, so typos
  never silently disable a knob the user believes is active.
- Values with the wrong JSON type for a documented key are dropped
  with a warning, so a stray string can never crash a scan.
- The file is re-read when its mtime changes, so edits apply to the
  next pipeline run without restarting anything.
- Lookups are cached per (path, mtime) and guarded by a lock, making
  ``runtime_get`` cheap enough to call from argparse constructors.

CLI arguments always win: the values loaded here only seed argparse
defaults, and an explicit flag replaces them as usual.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, Optional

from _builder.logging_utils import get_logger

_LOGGER = get_logger("runtime_config")

_LOCK = threading.Lock()
_CACHE: Dict[str, tuple] = {}

# Documented field reference (docs/*/RUNTIME_CONFIG.md). Keys absent
# from a section are accepted-but-reserved unless listed here; keys not
# listed anywhere are unknown and reported.
_SPEC: Dict[str, Dict[str, type]] = {
    "scan": {
        "workers": int,
        "parallel_mode": str,
        "max_file_size_kb": int,
        "skip_dirs": list,
    },
    "build": {
        "default_config": str,
        "max_domain_files": int,
    },
    "query": {
        "default_detail": str,
        "default_max_tokens": int,
        "explore_max_nodes": int,
        "explore_max_tokens": int,
    },
    "memory": {
        "decay_factor": float,
        "consolidate_threshold": int,
        "scratch_ttl_hours": float,
    },
    "semantic": {
        "stale_ratio_threshold": float,
        "stale_api_threshold": int,
    },
    "invariants": {
        "state_machine_threshold": int,
        "extract_preconditions": bool,
        "extract_postconditions": bool,
        "extract_loop_invariants": bool,
        "reject_ambiguous": bool,
    },
    "auto_enhance": {
        "auto_apply_extracted": bool,
        "require_confirm_inferred": bool,
        "reject_ambiguous": bool,
        "rollback_window": int,
        "batch_confirm_size": int,
    },
    "transactions": {
        "snapshot_keep_count": int,
        "lock_timeout_seconds": int,
    },
    "ffi": {
        "detect_python_ctypes": bool,
        "detect_go_cgo": bool,
        "detect_rust_extern": bool,
        "flag_lossy_conversions": bool,
    },
    "web_ui": {
        "port": int,
        "host": str,
        "open_browser": bool,
        "max_nodes_render": int,
    },
    "benchmark": {
        "recall_target": float,
        "max_tool_calls": int,
        "max_tokens": int,
    },
    "profile_health": {
        "min_score": int,
        "auto_apply_extracted": bool,
        "require_confirm_inferred": bool,
        "bind_to_head": bool,
    },
    "doc_code": {
        "check_on_describe": bool,
        "signature_diff_strict": bool,
    },
    "daemon": {
        "enabled": bool,
        "watch_paths": list,
        "exclude_patterns": list,
        "debounce_ms": int,
        "batch_window_ms": int,
        "auto_rebuild_outputs": bool,
        "idle_sleep_minutes": int,
        "max_events_per_minute": int,
        "backend": str,
        "startup_grace_sec": float,
    },
}


def default_config_path() -> Optional[str]:
    """Return the runtime.json path that would be loaded right now."""
    env = os.environ.get("C2D_RUNTIME_CONFIG")
    if env:
        return env
    scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate = os.path.join(scripts_dir, "config", "runtime.json")
    if os.path.isfile(candidate):
        return candidate
    return None


def _type_matches(value: Any, expected: type) -> bool:
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected is str:
        return isinstance(value, str)
    if expected is list:
        return isinstance(value, list)
    return True


def _validate(data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop unknown or wrongly-typed entries, reporting each drop once."""
    cleaned: Dict[str, Any] = {}
    for section, values in data.items():
        if not isinstance(values, dict):
            _LOGGER.warning(
                "runtime.json: section %r is not an object; ignored", section)
            continue
        spec = _SPEC.get(section)
        if spec is None:
            _LOGGER.warning(
                "runtime.json: unknown section %r; ignored (see "
                "docs/en/RUNTIME_CONFIG.md for the field reference)", section)
            continue
        kept: Dict[str, Any] = {}
        for key, value in values.items():
            expected = spec.get(key)
            if expected is None:
                _LOGGER.warning(
                    "runtime.json: unknown key %s.%s; ignored", section, key)
                continue
            if not _type_matches(value, expected):
                _LOGGER.warning(
                    "runtime.json: %s.%s expects %s, got %r; ignored",
                    section, key, expected.__name__, value)
                continue
            kept[key] = value
        if kept:
            cleaned[section] = kept
    return cleaned


def load_runtime_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load (and cache) the runtime configuration as a plain dict."""
    resolved = path or default_config_path()
    if not resolved or not os.path.isfile(resolved):
        return {}
    try:
        mtime = os.path.getmtime(resolved)
    except OSError:
        return {}
    with _LOCK:
        hit = _CACHE.get(resolved)
        if hit is not None and hit[0] == mtime:
            return hit[1]
    try:
        with open(resolved, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        _LOGGER.warning("runtime.json: cannot parse %s (%s); using defaults",
                        resolved, exc)
        return {}
    if not isinstance(data, dict):
        _LOGGER.warning("runtime.json: top level is not an object; ignored")
        return {}
    cleaned = _validate(data)
    with _LOCK:
        _CACHE[resolved] = (mtime, cleaned)
    return cleaned


def runtime_get(section: str, key: str, fallback: Any = None) -> Any:
    """Return a runtime.json value, or ``fallback`` when it is absent."""
    data = load_runtime_config()
    values = data.get(section)
    if not isinstance(values, dict):
        return fallback
    value = values.get(key)
    if value is None:
        return fallback
    return value
