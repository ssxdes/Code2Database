"""One-shot component health report for a built graph directory.

``doctor`` consolidates the checks an operator needs before trusting a
graph: SQLite integrity, schema versions, graph content, source
freshness, memory store, knowledge brief and daemon liveness. The
default output is a human table; ``--json`` produces a machine
readable document. The exit code is scriptable:

- ``0`` — every check passed
- ``1`` — at least one warning, no failure
- ``2`` — at least one failure

Every check degrades gracefully: a missing optional store is reported
as informational, a broken one as a failure, and the remaining checks
still run.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Dict, List, Optional

from _builder.logging_utils import get_logger

_LOGGER = get_logger("doctor")

_OK = "ok"
_WARN = "warn"
_FAIL = "fail"

_EXIT_OK = 0
_EXIT_WARN = 1
_EXIT_FAIL = 2


def _check(name: str, status: str, detail: str,
           hint: Optional[str] = None) -> Dict:
    entry = {"check": name, "status": status, "detail": detail}
    if hint:
        entry["hint"] = hint
    return entry


def _check_database(graph_dir: str) -> Dict:
    db_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.isfile(db_path):
        return _check("database", _FAIL,
                      "code2database.db not found",
                      "run: make (or c2d setup --source DIR) to build the graph")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                               timeout=5.0)
    except sqlite3.Error as exc:
        return _check("database", _FAIL, f"cannot open database: {exc}")
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        integrity_result = integrity[0] if integrity else "unknown"
        fk_violations = conn.execute(
            "PRAGMA foreign_key_check").fetchall()
        counts = {
            "functions": conn.execute(
                "SELECT COUNT(*) FROM functions").fetchone()[0],
            "edges": conn.execute(
                "SELECT COUNT(*) FROM edges").fetchone()[0],
        }
    except sqlite3.Error as exc:
        conn.close()
        return _check("database", _FAIL, f"cannot query database: {exc}")
    conn.close()
    if integrity_result != "ok":
        return _check("database", _FAIL,
                      f"integrity_check reported: {integrity_result}",
                      "restore from a snapshot (tx-list-snapshots / tx-restore) "
                      "or rebuild the graph")
    if fk_violations:
        return _check("database", _FAIL,
                      f"{len(fk_violations)} foreign key violation(s): "
                      f"first table {fk_violations[0][0]}",
                      "restore from a snapshot or rebuild the graph")
    return _check("database", _OK,
                  "integrity ok, no foreign key violations "
                  f"({counts['functions']} functions, {counts['edges']} edges)",
                  )
    # note: counts reused by the graph_content check below


def _check_schema(graph_dir: str) -> Dict:
    from _builder.graph.sqlite_store import SQLiteStore
    db_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.isfile(db_path):
        return _check("schema", _FAIL, "database absent")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        rows = dict(conn.execute(
            "SELECT key, value FROM meta").fetchall())
        conn.close()
    except sqlite3.Error as exc:
        return _check("schema", _FAIL, f"cannot read meta table: {exc}")

    main_version = rows.get("schema_version")
    if main_version is None:
        return _check("schema", _WARN,
                      "schema_version not recorded",
                      "the database predates version tracking; a rebuild "
                      "records it")
    if main_version != str(SQLiteStore.SCHEMA_VERSION):
        return _check("schema", _WARN,
                      f"graph schema {main_version}, code expects "
                      f"{SQLiteStore.SCHEMA_VERSION}",
                      "migrations run automatically on the next write; "
                      "back up the graph directory first if it matters")

    cgdb_version = rows.get("cgdb_schema_version")
    if cgdb_version is None:
        return _check("schema", _OK,
                      f"graph schema {main_version}; cgdb layer absent "
                      "(tree-sitter backend)")
    from _builder.cgdb.cgdb_schema import CGDB_SCHEMA_VERSION
    if cgdb_version != str(CGDB_SCHEMA_VERSION):
        return _check("schema", _WARN,
                      f"cgdb schema {cgdb_version}, code expects "
                      f"{CGDB_SCHEMA_VERSION}",
                      "cgdb migrations run automatically on the next write")
    return _check("schema", _OK,
                  f"graph schema {main_version}, cgdb schema {cgdb_version}")


def _check_graph_content(graph_dir: str) -> Dict:
    db_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.isfile(db_path):
        return _check("graph_content", _FAIL, "database absent")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        functions = conn.execute(
            "SELECT COUNT(*) FROM functions").fetchone()[0]
        edges = conn.execute(
            "SELECT COUNT(*) FROM edges").fetchone()[0]
        files = conn.execute(
            "SELECT COUNT(DISTINCT source_file) FROM functions").fetchone()[0]
        conn.close()
    except sqlite3.Error as exc:
        return _check("graph_content", _FAIL, f"cannot query counts: {exc}")
    if functions == 0:
        return _check("graph_content", _FAIL,
                      "graph is empty (0 functions)",
                      "run: make to scan and build")
    detail = f"{functions} functions, {edges} edges across {files} files"
    if edges == 0:
        return _check("graph_content", _WARN, detail + " (no edges)")
    return _check("graph_content", _OK, detail)


def _check_freshness(graph_dir: str) -> Dict:
    try:
        from _builder.cgdb.cgdb_freshness import check_freshness
        result = check_freshness(graph_dir)
    except Exception as exc:  # noqa: BLE001 — freshness must never break doctor
        _LOGGER.debug("freshness check unavailable: %s", exc)
        return _check("freshness", _WARN, "freshness check unavailable")
    if result.get("is_fresh"):
        return _check("freshness", _OK, "source and graph are in sync")
    changed = len(result.get("changed_files", []))
    new = len(result.get("new_files", []))
    deleted = len(result.get("deleted_files", []))
    ratio = result.get("staleness_ratio", 0.0)
    detail = (f"{changed} changed, {new} new, {deleted} deleted "
              f"since last scan (staleness {ratio:.0%})")
    hint = result.get("recommendation") or "run: build-update"
    status = _FAIL if ratio >= 0.5 else _WARN
    return _check("freshness", status, detail, hint)


def _check_memory(graph_dir: str) -> Dict:
    mem_db = os.path.join(graph_dir, "memory", "memory.db")
    if not os.path.isfile(mem_db):
        return _check("memory_store", _OK,
                      "no memory store yet (veteran Q&A accumulates as "
                      "agents capture experience)")
    try:
        from _builder.memory.memory_store import MemoryStore
        store = MemoryStore(graph_dir, read_only=True)
        stats = store.stats()
    except Exception as exc:  # noqa: BLE001
        return _check("memory_store", _FAIL, f"cannot open memory store: {exc}")
    total = stats.get("total", 0)
    if total == 0:
        return _check("memory_store", _OK, "memory store present, 0 entries")
    return _check("memory_store", _OK,
                  f"memory store present, {total} entries "
                  f"(active {stats.get('active', '?')}, "
                  f"archived {stats.get('archived', '?')})")


def _check_brief(graph_dir: str) -> Dict:
    brief_path = os.path.join(graph_dir, "knowledge", "brief.json")
    if not os.path.isfile(brief_path):
        return _check("knowledge_brief", _WARN,
                      "knowledge/brief.json not generated",
                      "run: brief-extract")
    size = os.path.getsize(brief_path)
    age_days = max(0.0, (time.time() - os.path.getmtime(brief_path)) / 86400.0)
    return _check("knowledge_brief", _OK,
                  f"brief present ({size} bytes, {age_days:.1f} days old)")


def _check_daemon(graph_dir: str) -> Dict:
    try:
        from _builder.daemon.daemon import is_daemon_running
        state = is_daemon_running(graph_dir)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("daemon probe unavailable: %s", exc)
        return _check("daemon", _OK, "daemon probe unavailable (optional)")
    if state:
        return _check("daemon", _OK, "daemon running")
    return _check("daemon", _OK,
                  "daemon not running (optional — start with daemon-start)")


_CHECKS = [
    ("database", _check_database),
    ("schema", _check_schema),
    ("graph_content", _check_graph_content),
    ("freshness", _check_freshness),
    ("memory_store", _check_memory),
    ("knowledge_brief", _check_brief),
    ("daemon", _check_daemon),
]


def run_doctor(graph_dir: str) -> Dict:
    """Run every check and return the full report document."""
    from _version import __version__
    results: List[Dict] = []
    for _, func in _CHECKS:
        try:
            results.append(func(graph_dir))
        except Exception as exc:  # noqa: BLE001 — one check never stops the rest
            results.append(_check(_name_of(func), _FAIL,
                                  f"check crashed: {exc}"))
    summary = {
        "ok": sum(1 for r in results if r["status"] == _OK),
        "warn": sum(1 for r in results if r["status"] == _WARN),
        "fail": sum(1 for r in results if r["status"] == _FAIL),
    }
    if summary["fail"]:
        exit_code = _EXIT_FAIL
    elif summary["warn"]:
        exit_code = _EXIT_WARN
    else:
        exit_code = _EXIT_OK
    return {
        "graph_dir": os.path.abspath(graph_dir),
        "tool_version": __version__,
        "checks": results,
        "summary": summary,
        "exit_code": exit_code,
    }


def _name_of(func) -> str:
    for name, candidate in _CHECKS:
        if candidate is func:
            return name
    return func.__name__


def _print_human(report: Dict) -> None:
    width = max(len(r["check"]) for r in report["checks"])
    print(f"Code2Database doctor — {report['graph_dir']}")
    print(f"tool version {report['tool_version']}")
    print()
    for entry in report["checks"]:
        mark = {"ok": "[ok]  ", "warn": "[warn]", "fail": "[FAIL]"}[
            entry["status"]]
        print(f"  {mark} {entry['check']:<{width}}  {entry['detail']}")
        if entry.get("hint"):
            print(f"         {'':<{width}}  -> {entry['hint']}")
    s = report["summary"]
    print()
    print(f"summary: {s['ok']} ok, {s['warn']} warn, {s['fail']} fail "
          f"(exit {report['exit_code']})")


def cmd_doctor(args) -> int:
    """CLI entry: one-shot health report for a graph directory."""
    import sys
    graph_dir = args.graph
    if not os.path.isdir(graph_dir):
        print(f"doctor: graph directory not found: {graph_dir}",
              file=sys.stderr)
        sys.exit(_EXIT_FAIL)
    report = run_doctor(graph_dir)
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    # The builder dispatch drops int return values, so propagate the
    # scriptable exit code explicitly (main() re-raises SystemExit).
    sys.exit(report["exit_code"])
