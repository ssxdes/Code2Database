"""Audit log: records every DB-modifying operation for traceability.

Distinct from change_log (which records commit-driven source changes).
audit_log records operator/command-driven graph edits — who ran what
command, what was changed, before/after values, when, and why.

Supports both SQLite backend (code2database.db) and JSON backend
(code2database_master.json) — in the JSON backend, the log is appended to
<graph_dir>/audit_log.jsonl (one JSON record per line).

Usage from any DB-modifying command:

    from _builder.audit_log import log_audit
    log_audit(graph_dir,
              command="update-node",
              target_kind="node",
              target_id=node_id,
              action="update",
              attribute="semantic_desc",
              before_value=old_desc,
              after_value=new_desc,
              reason="user edit")

For multi-step transactions, pass a consistent tx_id:

    tx_id = new_tx_id()
    for change in changes:
        log_audit(graph_dir, ..., tx_id=tx_id, ...)
"""
import json
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional
import logging


def new_tx_id() -> str:
    """Generate a new transaction id for grouping related audit entries."""
    return "tx_" + uuid.uuid4().hex[:12]


def _now_iso() -> str:
    """Current time as ISO-8601 string (second precision)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _to_jsonable(value: Any) -> Optional[str]:
    """Convert a value to a JSON string for storage. Returns None if value is None.

    Long body_text values are truncated to 4KB to keep the audit log compact.
    """
    if value is None:
        return None
    if isinstance(value, str) and len(value) > 4096:
        value = value[:4096] + "...[truncated]"
    try:
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    except (TypeError, ValueError):
        return repr(value)


def log_audit(graph_dir: str,
              command: str = "",
              target_kind: str = "",
              target_id: str = "",
              action: str = "",
              attribute: str = "",
              before_value: Any = None,
              after_value: Any = None,
              reason: str = "",
              operator: str = "",
              tx_id: str = "") -> bool:
    """Append an entry to the audit log.

    Returns True on success, False on failure (never raises — audit logging
    is best-effort and should not break the host operation).
    """
    timestamp = _now_iso()
    record = {
        "timestamp": timestamp,
        "operator": operator or _detect_operator(),
        "command": command,
        "target_kind": target_kind,
        "target_id": target_id,
        "action": action,
        "attribute": attribute,
        "before_value": _to_jsonable(before_value),
        "after_value": _to_jsonable(after_value),
        "reason": reason,
        "tx_id": tx_id or "",
    }

    try:
        db_path = os.path.join(graph_dir, "code2database.db")
        if os.path.exists(db_path):
            return _log_audit_sqlite(db_path, record)
        # JSON backend: append to audit_log.jsonl
        return _log_audit_json(graph_dir, record)
    except Exception:
        return False


def _detect_operator() -> str:
    """Best-effort detection of who/what triggered the change."""
    try:
        import getpass
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    # Could be extended to detect MCP/CLI/daemon sources via env vars
    source = os.environ.get("CALLGRAPH_AUDIT_OPERATOR", "")
    return source or user


def _log_audit_sqlite(db_path: str, record: Dict) -> bool:
    """Append an audit record to the SQLite audit_log table."""
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """INSERT INTO audit_log
                   (timestamp, operator, command, target_kind, target_id,
                    action, attribute, before_value, after_value, reason, tx_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (record["timestamp"], record["operator"], record["command"],
                 record["target_kind"], record["target_id"], record["action"],
                 record["attribute"], record["before_value"], record["after_value"],
                 record["reason"], record["tx_id"])
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        # Fall back to JSON sidecar
        return _log_audit_json(os.path.dirname(db_path), record)


def _log_audit_json(graph_dir: str, record: Dict) -> bool:
    """Append an audit record to audit_log.jsonl in the graph directory."""
    log_path = os.path.join(graph_dir, "audit_log.jsonl")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def query_audit_log(graph_dir: str,
                    target_id: Optional[str] = None,
                    command: Optional[str] = None,
                    target_kind: Optional[str] = None,
                    action: Optional[str] = None,
                    tx_id: Optional[str] = None,
                    since: Optional[str] = None,
                    until: Optional[str] = None,
                    limit: int = 100,
                    offset: int = 0) -> Dict:
    """Query the audit log with optional filters.

    Returns dict with:
      - entries: list of audit record dicts
      - total: total count matching the filters (before limit/offset)
      - source: 'sqlite' or 'json'
    """
    db_path = os.path.join(graph_dir, "code2database.db")
    if os.path.exists(db_path):
        return _query_audit_log_sqlite(db_path, target_id, command, target_kind,
                                       action, tx_id, since, until, limit, offset)
    return _query_audit_log_json(graph_dir, target_id, command, target_kind,
                                 action, tx_id, since, until, limit, offset)


def _query_audit_log_sqlite(db_path: str,
                            target_id: Optional[str],
                            command: Optional[str],
                            target_kind: Optional[str],
                            action: Optional[str],
                            tx_id: Optional[str],
                            since: Optional[str],
                            until: Optional[str],
                            limit: int,
                            offset: int) -> Dict:
    """Query audit_log table in SQLite."""
    where_clauses = []
    params: List[Any] = []
    if target_id:
        where_clauses.append("target_id = ?")
        params.append(target_id)
    if command:
        where_clauses.append("command = ?")
        params.append(command)
    if target_kind:
        where_clauses.append("target_kind = ?")
        params.append(target_kind)
    if action:
        where_clauses.append("action = ?")
        params.append(action)
    if tx_id:
        where_clauses.append("tx_id = ?")
        params.append(tx_id)
    if since:
        where_clauses.append("timestamp >= ?")
        params.append(since)
    if until:
        where_clauses.append("timestamp <= ?")
        params.append(until)
    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    try:
        conn = sqlite3.connect(db_path)
        try:
            # Total count
            total_row = conn.execute(
                "SELECT COUNT(*) FROM audit_log" + where_sql, params
            ).fetchone()
            total = total_row[0] if total_row else 0

            # Fetch entries (newest first)
            rows = conn.execute(
                """SELECT timestamp, operator, command, target_kind, target_id,
                          action, attribute, before_value, after_value, reason, tx_id
                   FROM audit_log""" + where_sql +
                " ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [limit, offset]
            ).fetchall()

            entries = []
            for r in rows:
                entries.append({
                    "timestamp": r[0], "operator": r[1], "command": r[2],
                    "target_kind": r[3], "target_id": r[4], "action": r[5],
                    "attribute": r[6],
                    "before_value": _try_parse_json(r[7]),
                    "after_value": _try_parse_json(r[8]),
                    "reason": r[9], "tx_id": r[10],
                })
            return {"entries": entries, "total": total, "source": "sqlite"}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"entries": [], "total": 0, "source": "sqlite",
                "error": str(exc)}


def _query_audit_log_json(graph_dir: str,
                          target_id: Optional[str],
                          command: Optional[str],
                          target_kind: Optional[str],
                          action: Optional[str],
                          tx_id: Optional[str],
                          since: Optional[str],
                          until: Optional[str],
                          limit: int,
                          offset: int) -> Dict:
    """Query audit_log.jsonl in JSON backend."""
    log_path = os.path.join(graph_dir, "audit_log.jsonl")
    if not os.path.exists(log_path):
        return {"entries": [], "total": 0, "source": "json"}
    entries = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    continue
                if target_id and rec.get("target_id") != target_id:
                    continue
                if command and rec.get("command") != command:
                    continue
                if target_kind and rec.get("target_kind") != target_kind:
                    continue
                if action and rec.get("action") != action:
                    continue
                if tx_id and rec.get("tx_id") != tx_id:
                    continue
                if since and rec.get("timestamp", "") < since:
                    continue
                if until and rec.get("timestamp", "") > until:
                    continue
                entries.append(rec)
    except OSError:
        return {"entries": [], "total": 0, "source": "json", "error": "read failed"}
    total = len(entries)
    # Reverse (newest first) and apply offset/limit
    entries.reverse()
    paged = entries[offset:offset + limit]
    return {"entries": paged, "total": total, "source": "json"}


def _try_parse_json(value: Optional[str]) -> Any:
    """Try to parse a JSON string back to a value. Returns the raw string on failure."""
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def annotate_fact_source(node_data: Dict, source: str, command: str = "",
                         timestamp: Optional[str] = None) -> Dict:
    """Annotate a node/edge with a 'fact_source' marker.

    This is used to track which facts came from where:
      - 'ast': extracted from AST by the scanner
      - 'inferred': inferred by the builder (vtable dispatch, callback bridge, etc.)
      - 'llm': LLM-enhanced (semantic_desc, invariants, etc.)
      - 'user': user-edited via update-node/patcher
      - 'daemon': auto-synced by the daemon

    Returns the modified node_data dict (mutated in place).
    """
    if not isinstance(node_data, dict):
        return node_data
    fact_sources = node_data.get("_fact_sources") or {}
    fact_sources[command or "_default"] = {
        "source": source,
        "timestamp": timestamp or _now_iso(),
    }
    node_data["_fact_sources"] = fact_sources
    return node_data


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def cmd_audit_log(args):
    """Query the audit log.

    Examples:
      audit-log --graph out/
      audit-log --graph out/ --node my_func
      audit-log --graph out/ --command update-node --limit 50
      audit-log --graph out/ --tx tx_abc123
    """
    import sys
    graph_dir = args.graph
    result = query_audit_log(
        graph_dir,
        target_id=args.node,
        command=args.command,
        target_kind=args.target_kind,
        action=args.action,
        tx_id=args.tx,
        since=args.since,
        until=args.until,
        limit=args.limit,
        offset=args.offset,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
