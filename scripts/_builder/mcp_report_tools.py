"""28 MCP tools implementing the report appendix B signatures.

These tools are added on top of the existing 53 tools (34 code2database_*
+ 19 cgdb_*) in mcp_server.py. They implement the report
(C代码数据库化方案-分析与执行报告.md) appendix B:

  L1 无损重建层 (8 tools):
    - render_source
    - verify_consistency
    - edit_token
    - insert_token
    - delete_token
    - find_macros
    - get_pp_branches
    - get_string_literals

  L2 AST 层 (8 tools):
    - find_symbol
    - callers_of
    - callees_of
    - who_writes
    - who_reads
    - get_context
    - impact_analysis
    - get_module_view

  L3 IR 层 (7 tools):
    - indirect_targets
    - alias_set
    - trace_data_flow
    - cfg_of
    - path_sensitive_states
    - precise_write_set
    - dead_code_in

  写回与事务 (2 tools):
    - commit_db_transaction
    - rollback_db_transaction

  高级语义编辑 (3 tools):
    - insert_node_after
    - delete_node
    - add_function

Each tool handler has signature `def handler(args: dict, graph_dir: str) -> dict|list`
to match the existing `_tool_*` handlers in mcp_server.py.
"""
from __future__ import annotations

import os
import sqlite3
import json
from typing import Optional
import logging

# Lazily import to avoid circular deps at module-load time
def _get_conn(graph_dir: str) -> sqlite3.Connection:
    """Open or get the cached SQLite connection for `graph_dir`."""
    db_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"code2database.db not found in {graph_dir}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _close_conn(conn: Optional[sqlite3.Connection]) -> None:
    if conn is not None:
        try:
            conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
# L1 无损重建层 tools (8)
# ===========================================================================

def _tool_render_source(args: dict, graph_dir: str) -> dict:
    """render_source(file_id) -> {content, sha256, matches_disk}"""
    from _builder.source_renderer import render_source
    file_id = int(args.get("file_id", 0))
    if not file_id:
        return {"error": "file_id is required"}
    conn = None
    try:
        conn = _get_conn(graph_dir)
        result = render_source(conn, file_id)
        if result.error:
            return {"error": result.error, "file_id": file_id}
        return {
            "file_id": file_id,
            "path": result.path,
            "sha256": result.sha256,
            "matches_disk": result.matches_disk,
            "disk_sha256": result.disk_sha256,
            "token_count": result.token_count,
            "content_length": len(result.content),
            # Note: we return a truncated preview, not full content, to
            # avoid flooding the LLM context with megabytes of source.
            "content_preview": result.content[:2000].decode(
                "utf-8", errors="replace"
            ) if result.content else "",
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        _close_conn(conn)


def _tool_verify_consistency(args: dict, graph_dir: str) -> dict:
    """verify_consistency(file_id) -> {db_sha256, disk_sha256, ok, diff}"""
    from _builder.source_renderer import verify_consistency
    file_id = int(args.get("file_id", 0))
    if not file_id:
        return {"error": "file_id is required"}
    conn = None
    try:
        conn = _get_conn(graph_dir)
        result = verify_consistency(conn, file_id)
        return {
            "file_id": file_id,
            "db_sha256": result.db_sha256,
            "disk_sha256": result.disk_sha256,
            "ok": result.ok,
            "diff": result.diff,
            "error_id": result.error_id,
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        _close_conn(conn)


def _tool_edit_token(args: dict, graph_dir: str) -> dict:
    """edit_token(token_id, new_text) -> {affected_nodes, transaction_id}"""
    token_id = int(args.get("token_id", 0))
    new_text = args.get("new_text", "")
    if not token_id:
        return {"error": "token_id is required"}
    conn = None
    try:
        conn = _get_conn(graph_dir)
        # Get old spelling for audit
        row = conn.execute(
            "SELECT file_id, spelling FROM tokens WHERE id = ?",
            (token_id,)
        ).fetchone()
        if row is None:
            return {"error": f"token_id {token_id} not found"}
        file_id, old_text = row["file_id"], row["spelling"]
        # Update the token
        conn.execute(
            "UPDATE tokens SET spelling = ? WHERE id = ?",
            (new_text, token_id)
        )
        # Find affected AST nodes (by ast_node_id FK)
        affected_nodes = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT ast_node_id FROM tokens WHERE id = ?",
                (token_id,)
            ).fetchall() if r[0]
        ]
        conn.commit()
        return {
            "token_id": token_id,
            "file_id": file_id,
            "old_text": old_text,
            "new_text": new_text,
            "affected_nodes": affected_nodes,
            "transaction_id": f"edit_token_{token_id}",
            "note": "DB edit applied; run commit_db_transaction to render and write to disk",
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        _close_conn(conn)


def _tool_insert_token(args: dict, graph_dir: str) -> dict:
    """insert_token(after_token_id, tokens[]) -> {new_token_ids, transaction_id}"""
    after_token_id = int(args.get("after_token_id", 0))
    tokens = args.get("tokens", [])
    if not after_token_id or not tokens:
        return {"error": "after_token_id and tokens[] are required"}
    conn = None
    try:
        conn = _get_conn(graph_dir)
        # Find the file_id, seq, line, col of the anchor token
        row = conn.execute(
            "SELECT file_id, seq, line, col, byte_offset FROM tokens WHERE id = ?",
            (after_token_id,)
        ).fetchone()
        if row is None:
            return {"error": f"after_token_id {after_token_id} not found"}
        file_id, anchor_seq, anchor_line, anchor_col, anchor_byte = (
            row["file_id"], row["seq"], row["line"], row["col"], row["byte_offset"]
        )
        # Shift all tokens with seq > anchor_seq up by len(tokens)
        conn.execute(
            "UPDATE tokens SET seq = seq + ? WHERE file_id = ? AND seq > ?",
            (len(tokens), file_id, anchor_seq)
        )
        # Insert new tokens
        new_ids = []
        for i, tok in enumerate(tokens):
            kind = tok.get("kind", "identifier")
            spelling = tok.get("spelling", "")
            preceding_ws = tok.get("preceding_whitespace", "")
            new_seq = anchor_seq + 1 + i
            cur = conn.execute(
                "INSERT INTO tokens (file_id, seq, kind, spelling, line, col, "
                "byte_offset, byte_length, preceding_whitespace) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (file_id, new_seq, kind, spelling, anchor_line, anchor_col,
                 anchor_byte + i, len(spelling), preceding_ws)
            )
            new_ids.append(cur.lastrowid)
        conn.commit()
        return {
            "after_token_id": after_token_id,
            "new_token_ids": new_ids,
            "transaction_id": f"insert_token_{after_token_id}",
            "note": "DB edit applied; run commit_db_transaction to render and write to disk",
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        _close_conn(conn)


def _tool_delete_token(args: dict, graph_dir: str) -> dict:
    """delete_token(token_id) -> {affected_nodes, transaction_id}"""
    token_id = int(args.get("token_id", 0))
    if not token_id:
        return {"error": "token_id is required"}
    conn = None
    try:
        conn = _get_conn(graph_dir)
        row = conn.execute(
            "SELECT file_id, seq FROM tokens WHERE id = ?", (token_id,)
        ).fetchone()
        if row is None:
            return {"error": f"token_id {token_id} not found"}
        file_id, seq = row["file_id"], row["seq"]
        # Find affected AST nodes BEFORE delete
        affected_nodes = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT ast_node_id FROM tokens WHERE id = ?",
                (token_id,)
            ).fetchall() if r[0]
        ]
        # Delete
        conn.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
        # Shift tokens with seq > deleted_seq down by 1
        conn.execute(
            "UPDATE tokens SET seq = seq - 1 WHERE file_id = ? AND seq > ?",
            (file_id, seq)
        )
        conn.commit()
        return {
            "token_id": token_id,
            "file_id": file_id,
            "affected_nodes": affected_nodes,
            "transaction_id": f"delete_token_{token_id}",
            "note": "DB edit applied; run commit_db_transaction to render and write to disk",
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        _close_conn(conn)


def _tool_find_macros(args: dict, graph_dir: str) -> list:
    """find_macros(name?) -> [{name, params, body, defined_at, used_at[]}]"""
    name = args.get("name")
    conn = None
    try:
        conn = _get_conn(graph_dir)
        if name:
            macros = conn.execute(
                "SELECT id, name, file_id, line, is_function_like, is_variadic, "
                "params, body_text, is_undef, defined_at_token_id "
                "FROM macros WHERE name = ? ORDER BY file_id, line",
                (name,)
            ).fetchall()
        else:
            macros = conn.execute(
                "SELECT id, name, file_id, line, is_function_like, is_variadic, "
                "params, body_text, is_undef, defined_at_token_id "
                "FROM macros ORDER BY name LIMIT 500"
            ).fetchall()
        result = []
        for m in macros:
            uses = conn.execute(
                "SELECT file_id, line, col FROM macro_invocations "
                "WHERE macro_id = ? ORDER BY file_id, line",
                (m["id"],)
            ).fetchall()
            result.append({
                "macro_id": m["id"],
                "name": m["name"],
                "file_id": m["file_id"],
                "line": m["line"],
                "is_function_like": bool(m["is_function_like"]),
                "is_variadic": bool(m["is_variadic"]),
                "params": json.loads(m["params"] or "[]"),
                "body_text": m["body_text"],
                "is_undef": bool(m["is_undef"]),
                "defined_at_token_id": m["defined_at_token_id"],
                "used_at": [{"file_id": u["file_id"], "line": u["line"],
                             "col": u["col"]} for u in uses],
            })
        return result
    except Exception as exc:
        return [{"error": str(exc)}]
    finally:
        _close_conn(conn)


def _tool_get_pp_branches(args: dict, graph_dir: str) -> list:
    """get_pp_branches(file_id) -> [{kind, condition, line, active, children[]}]"""
    file_id = int(args.get("file_id", 0))
    if not file_id:
        return [{"error": "file_id is required"}]
    conn = None
    try:
        conn = _get_conn(graph_dir)
        branches = conn.execute(
            "SELECT id, parent_id, kind, condition, start_line, end_line, "
            "is_active, config_hash FROM pp_branches "
            "WHERE file_id = ? ORDER BY start_line",
            (file_id,)
        ).fetchall()
        # Build tree
        nodes_by_id = {b["id"]: {
            "branch_id": b["id"],
            "kind": b["kind"],
            "condition": b["condition"],
            "line": b["start_line"],
            "end_line": b["end_line"],
            "active": bool(b["is_active"]),
            "config_hash": b["config_hash"],
            "children": [],
        } for b in branches}
        roots = []
        for b in branches:
            n = nodes_by_id[b["id"]]
            if b["parent_id"] and b["parent_id"] in nodes_by_id:
                nodes_by_id[b["parent_id"]]["children"].append(n)
            else:
                roots.append(n)
        return roots
    except Exception as exc:
        return [{"error": str(exc)}]
    finally:
        _close_conn(conn)


def _tool_get_string_literals(args: dict, graph_dir: str) -> list:
    """get_string_literals(pattern?) -> [{loc, bytes, in_function}]"""
    pattern = args.get("pattern")
    conn = None
    try:
        conn = _get_conn(graph_dir)
        if pattern:
            rows = conn.execute(
                "SELECT s.id, s.literal_id, s.raw_bytes, s.decoded, s.encoding, "
                "s.is_wide, s.in_function_id, s.security_flags, t.line, t.col "
                "FROM string_literals s LEFT JOIN tokens t ON s.token_id = t.id "
                "WHERE s.decoded LIKE ? ORDER BY t.line LIMIT 500",
                (f"%{pattern}%",)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT s.id, s.literal_id, s.raw_bytes, s.decoded, s.encoding, "
                "s.is_wide, s.in_function_id, s.security_flags, t.line, t.col "
                "FROM string_literals s LEFT JOIN tokens t ON s.token_id = t.id "
                "ORDER BY t.line LIMIT 500"
            ).fetchall()
        return [{
            "literal_id": r["literal_id"],
            "decoded": r["decoded"],
            "encoding": r["encoding"],
            "is_wide": bool(r["is_wide"]),
            "in_function_id": r["in_function_id"],
            "security_flags": json.loads(r["security_flags"] or "[]") if r["security_flags"] else [],
            "line": r["line"],
            "col": r["col"],
        } for r in rows]
    except Exception as exc:
        return [{"error": str(exc)}]
    finally:
        _close_conn(conn)


# ===========================================================================
# L2 AST 层 tools (8)
# ===========================================================================

def _tool_find_symbol(args: dict, graph_dir: str) -> dict:
    """find_symbol(name, kind?) -> {loc, signature, doc, token_range}"""
    name = args.get("name", "")
    kind = args.get("kind")
    if not name:
        return {"error": "name is required"}
    conn = None
    try:
        conn = _get_conn(graph_dir)
        if kind:
            row = conn.execute(
                "SELECT id, kind, name, fqn, file_id, line, col, byte_start, "
                "byte_end, signature, body_text, type_spelling, enclosing_symbol_id "
                "FROM cgdb_nodes WHERE name = ? AND kind = ? "
                "ORDER BY file_id, line LIMIT 1",
                (name, kind)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, kind, name, fqn, file_id, line, col, byte_start, "
                "byte_end, signature, body_text, type_spelling, enclosing_symbol_id "
                "FROM cgdb_nodes WHERE name = ? ORDER BY file_id, line LIMIT 1",
                (name,)
            ).fetchone()
        if row is None:
            return {"error": f"symbol {name} not found"}
        # Find doc comment
        doc = conn.execute(
            "SELECT cleaned_text FROM doc_comments WHERE node_id = ? LIMIT 1",
            (row["id"],)
        ).fetchone()
        # Find token range
        token_range = conn.execute(
            "SELECT MIN(id) AS start_id, MAX(id) AS end_id FROM tokens "
            "WHERE ast_node_id = ?", (row["id"],)
        ).fetchone()
        return {
            "symbol_id": row["id"],
            "kind": row["kind"],
            "name": row["name"],
            "fqn": row["fqn"],
            "loc": {"file_id": row["file_id"], "line": row["line"],
                    "col": row["col"]},
            "byte_range": [row["byte_start"], row["byte_end"]],
            "signature": row["signature"],
            "doc": doc["cleaned_text"] if doc else "",
            "type_spelling": row["type_spelling"],
            "enclosing_symbol_id": row["enclosing_symbol_id"],
            "token_range": {"start_id": token_range["start_id"],
                            "end_id": token_range["end_id"]} if token_range and token_range["start_id"] else None,
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        _close_conn(conn)


def _tool_callers_of(args: dict, graph_dir: str) -> list:
    """callers_of(fn) -> [{caller, loc, call_kind}]"""
    fn = args.get("fn", "")
    if not fn:
        return [{"error": "fn is required"}]
    conn = None
    try:
        conn = _get_conn(graph_dir)
        rows = conn.execute(
            "SELECT e.src_id AS caller_id, n_src.name AS caller_name, "
            "n_src.fqn AS caller_fqn, e.file_id, e.line, e.col, e.kind, "
            "e.confidence "
            "FROM cgdb_edges e JOIN cgdb_nodes n_src ON e.src_id = n_src.id "
            "JOIN cgdb_nodes n_dst ON e.dst_id = n_dst.id "
            "WHERE e.kind = 'INVOKES' AND n_dst.name = ? "
            "ORDER BY e.file_id, e.line LIMIT 500",
            (fn,)
        ).fetchall()
        return [{
            "caller": r["caller_name"],
            "caller_fqn": r["caller_fqn"],
            "caller_id": r["caller_id"],
            "loc": {"file_id": r["file_id"], "line": r["line"], "col": r["col"]},
            "call_kind": r["kind"],
            "confidence": r["confidence"],
        } for r in rows]
    except Exception as exc:
        return [{"error": str(exc)}]
    finally:
        _close_conn(conn)


def _tool_callees_of(args: dict, graph_dir: str) -> list:
    """callees_of(fn) -> [{callee, loc, call_kind}]"""
    fn = args.get("fn", "")
    if not fn:
        return [{"error": "fn is required"}]
    conn = None
    try:
        conn = _get_conn(graph_dir)
        rows = conn.execute(
            "SELECT e.dst_id AS callee_id, n_dst.name AS callee_name, "
            "n_dst.fqn AS callee_fqn, e.file_id, e.line, e.col, e.kind, "
            "e.confidence "
            "FROM cgdb_edges e JOIN cgdb_nodes n_dst ON e.dst_id = n_dst.id "
            "JOIN cgdb_nodes n_src ON e.src_id = n_src.id "
            "WHERE e.kind = 'INVOKES' AND n_src.name = ? "
            "ORDER BY e.file_id, e.line LIMIT 500",
            (fn,)
        ).fetchall()
        return [{
            "callee": r["callee_name"],
            "callee_fqn": r["callee_fqn"],
            "callee_id": r["callee_id"],
            "loc": {"file_id": r["file_id"], "line": r["line"], "col": r["col"]},
            "call_kind": r["kind"],
            "confidence": r["confidence"],
        } for r in rows]
    except Exception as exc:
        return [{"error": str(exc)}]
    finally:
        _close_conn(conn)


def _tool_who_writes(args: dict, graph_dir: str) -> list:
    """who_writes(global_var) -> [{fn, loc}]"""
    var = args.get("global_var", "")
    if not var:
        return [{"error": "global_var is required"}]
    conn = None
    try:
        conn = _get_conn(graph_dir)
        rows = conn.execute(
            "SELECT e.src_id, n_src.name AS fn_name, n_src.fqn AS fn_fqn, "
            "e.file_id, e.line, e.col "
            "FROM cgdb_edges e JOIN cgdb_nodes n_src ON e.src_id = n_src.id "
            "JOIN cgdb_nodes n_dst ON e.dst_id = n_dst.id "
            "WHERE e.kind = 'WRITES' AND n_dst.name = ? "
            "ORDER BY e.file_id, e.line LIMIT 500",
            (var,)
        ).fetchall()
        return [{
            "fn": r["fn_name"], "fn_fqn": r["fn_fqn"],
            "loc": {"file_id": r["file_id"], "line": r["line"], "col": r["col"]},
        } for r in rows]
    except Exception as exc:
        return [{"error": str(exc)}]
    finally:
        _close_conn(conn)


def _tool_who_reads(args: dict, graph_dir: str) -> list:
    """who_reads(global_var) -> [{fn, loc}]"""
    var = args.get("global_var", "")
    if not var:
        return [{"error": "global_var is required"}]
    conn = None
    try:
        conn = _get_conn(graph_dir)
        rows = conn.execute(
            "SELECT e.src_id, n_src.name AS fn_name, n_src.fqn AS fn_fqn, "
            "e.file_id, e.line, e.col "
            "FROM cgdb_edges e JOIN cgdb_nodes n_src ON e.src_id = n_src.id "
            "JOIN cgdb_nodes n_dst ON e.dst_id = n_dst.id "
            "WHERE e.kind = 'READS' AND n_dst.name = ? "
            "ORDER BY e.file_id, e.line LIMIT 500",
            (var,)
        ).fetchall()
        return [{
            "fn": r["fn_name"], "fn_fqn": r["fn_fqn"],
            "loc": {"file_id": r["file_id"], "line": r["line"], "col": r["col"]},
        } for r in rows]
    except Exception as exc:
        return [{"error": str(exc)}]
    finally:
        _close_conn(conn)


def _tool_get_context(args: dict, graph_dir: str) -> dict:
    """get_context(loc, radius_lines) -> {ast, code, comments, token_range}"""
    loc = args.get("loc", {})
    radius = int(args.get("radius_lines", 5))
    file_id = loc.get("file_id")
    line = loc.get("line")
    if not file_id or not line:
        return {"error": "loc.file_id and loc.line are required"}
    conn = None
    try:
        conn = _get_conn(graph_dir)
        # Find AST nodes in the radius
        nodes = conn.execute(
            "SELECT id, kind, name, fqn, line, col, byte_start, byte_end, "
            "signature, body_text "
            "FROM cgdb_nodes WHERE file_id = ? AND line BETWEEN ? AND ? "
            "ORDER BY line",
            (file_id, line - radius, line + radius)
        ).fetchall()
        # Find tokens in the radius
        tokens = conn.execute(
            "SELECT id, seq, kind, spelling, line, col, preceding_whitespace "
            "FROM tokens WHERE file_id = ? AND line BETWEEN ? AND ? "
            "ORDER BY seq",
            (file_id, line - radius, line + radius)
        ).fetchall()
        # Find comments in the radius
        comments = conn.execute(
            "SELECT id, line, end_line, text, kind FROM comments_freeform "
            "WHERE file_id = ? AND line BETWEEN ? AND ? ORDER BY line",
            (file_id, line - radius, line + radius)
        ).fetchall()
        return {
            "loc": {"file_id": file_id, "line": line},
            "radius_lines": radius,
            "ast": [{
                "node_id": n["id"], "kind": n["kind"], "name": n["name"],
                "fqn": n["fqn"], "line": n["line"], "col": n["col"],
                "signature": n["signature"],
            } for n in nodes],
            "code": "".join(
                (t["preceding_whitespace"] or "") + (t["spelling"] or "")
                for t in tokens
            ),
            "comments": [{
                "comment_id": c["id"], "line": c["line"],
                "end_line": c["end_line"], "text": c["text"],
                "kind": c["kind"],
            } for c in comments],
            "token_range": {
                "start_id": tokens[0]["id"] if tokens else None,
                "end_id": tokens[-1]["id"] if tokens else None,
            } if tokens else None,
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        _close_conn(conn)


def _tool_impact_analysis(args: dict, graph_dir: str) -> dict:
    """impact_analysis(symbol) -> {affected_files, affected_fns, risk_level}"""
    symbol = args.get("symbol", "")
    if not symbol:
        return {"error": "symbol is required"}
    conn = None
    try:
        conn = _get_conn(graph_dir)
        # Look up the symbol
        row = conn.execute(
            "SELECT id FROM cgdb_nodes WHERE name = ? LIMIT 1", (symbol,)
        ).fetchone()
        if row is None:
            return {"error": f"symbol {symbol} not found"}
        symbol_id = row["id"]
        # Find all callers (reverse reachability via recursive CTE)
        affected = conn.execute(
            "WITH RECURSIVE callers(id) AS ("
            "  SELECT ? "
            "  UNION"
            "  SELECT e.src_id FROM cgdb_edges e JOIN callers c ON e.dst_id = c.id "
            "  WHERE e.kind IN ('INVOKES','OPS_BIND','IMPLEMENTS','OVERRIDES')"
            ") "
            "SELECT n.id, n.name, n.fqn, n.file_id, n.line "
            "FROM callers JOIN cgdb_nodes n ON callers.id = n.id "
            "WHERE n.id != ? "
            "ORDER BY n.file_id, n.line LIMIT 1000",
            (symbol_id, symbol_id)
        ).fetchall()
        affected_files = sorted({r["file_id"] for r in affected if r["file_id"]})
        affected_fns = [{
            "fn_id": r["id"], "name": r["name"], "fqn": r["fqn"],
            "file_id": r["file_id"], "line": r["line"],
        } for r in affected]
        # Risk level: heuristic based on count
        n = len(affected)
        risk = "high" if n > 50 else "medium" if n > 10 else "low" if n > 0 else "none"
        return {
            "symbol": symbol,
            "symbol_id": symbol_id,
            "affected_files": affected_files,
            "affected_fns": affected_fns,
            "affected_count": n,
            "risk_level": risk,
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        _close_conn(conn)


def _tool_get_module_view(args: dict, graph_dir: str) -> dict:
    """get_module_view(module) -> {files, symbols_summary, deps, metrics}"""
    module = args.get("module", "")
    if not module:
        return {"error": "module is required"}
    conn = None
    try:
        conn = _get_conn(graph_dir)
        # Files in module (by path match — domain concept)
        files = conn.execute(
            "SELECT id, path, language, line_count, byte_count FROM cgdb_files "
            "WHERE path LIKE ? ORDER BY path",
            (f"%{module}%",)
        ).fetchall()
        # Symbols in module
        symbols = conn.execute(
            "SELECT id, kind, name, fqn, file_id, line "
            "FROM cgdb_nodes WHERE fqn LIKE ? OR name LIKE ? "
            "ORDER BY kind, name LIMIT 200",
            (f"%{module}%", f"%{module}%")
        ).fetchall()
        # Module deps (from module_deps)
        deps = conn.execute(
            "SELECT from_module, to_module, edge_count FROM module_deps "
            "WHERE from_module = ? OR to_module = ?",
            (module, module)
        ).fetchall()
        # Metrics
        metrics = conn.execute(
            "SELECT * FROM arch_metrics WHERE module_name = ?",
            (module,)
        ).fetchone()
        return {
            "module": module,
            "files": [{"file_id": f["id"], "path": f["path"],
                       "language": f["language"],
                       "line_count": f["line_count"],
                       "byte_count": f["byte_count"]} for f in files],
            "symbols_summary": {
                "total": len(symbols),
                "by_kind": {k: sum(1 for s in symbols if s["kind"] == k)
                            for k in {s["kind"] for s in symbols}},
            },
            "deps": [{"from": d["from_module"], "to": d["to_module"],
                      "edge_count": d["edge_count"]} for d in deps],
            "metrics": dict(metrics) if metrics else None,
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        _close_conn(conn)


# ===========================================================================
# L3 IR 层 tools (7)
# ===========================================================================

def _tool_indirect_targets(args: dict, graph_dir: str) -> list:
    """indirect_targets(call_site_loc) -> [{target_fn, confidence}]"""
    loc = args.get("call_site_loc", {})
    file_id = loc.get("file_id")
    line = loc.get("line")
    if not file_id or not line:
        return [{"error": "call_site_loc.file_id and call_site_loc.line required"}]
    conn = None
    try:
        conn = _get_conn(graph_dir)
        rows = conn.execute(
            "SELECT ic.call_site_id, ic.call_edge_id, ic.function_id, "
            "ic.line, ic.col, ic.possible_target_symbol_id, "
            "n.name AS target_fn, ic.confidence, ic.analysis "
            "FROM indirect_calls ic "
            "LEFT JOIN cgdb_nodes n ON ic.possible_target_symbol_id = n.id "
            "WHERE ic.line = ? "
            "ORDER BY ic.confidence DESC",
            (line,)
        ).fetchall()
        return [{
            "target_fn": r["target_fn"],
            "target_symbol_id": r["possible_target_symbol_id"],
            "confidence": r["confidence"],
            "analysis": r["analysis"],
            "call_site_id": r["call_site_id"],
        } for r in rows]
    except Exception as exc:
        return [{"error": str(exc)}]
    finally:
        _close_conn(conn)


def _tool_alias_set(args: dict, graph_dir: str) -> list:
    """alias_set(variable, scope?) -> [{alias_var, kind, confidence}]"""
    variable = args.get("variable", "")
    scope = args.get("scope")
    if not variable:
        return [{"error": "variable is required"}]
    conn = None
    try:
        conn = _get_conn(graph_dir)
        # Look up the variable's node id
        row = conn.execute(
            "SELECT id FROM cgdb_nodes WHERE name = ? "
            "AND kind IN ('var','parm','field') LIMIT 1",
            (variable,)
        ).fetchone()
        if row is None:
            return []
        var_id = row["id"]
        # Find aliases
        if scope:
            rows = conn.execute(
                "SELECT a.ptr1_node_id, a.ptr2_node_id, a.kind, a.confidence, "
                "a.analysis, a.function_id, "
                "n1.name AS var1, n2.name AS var2 "
                "FROM alias_sets a "
                "LEFT JOIN cgdb_nodes n1 ON a.ptr1_node_id = n1.id "
                "LEFT JOIN cgdb_nodes n2 ON a.ptr2_node_id = n2.id "
                "WHERE (a.ptr1_node_id = ? OR a.ptr2_node_id = ?) "
                "AND a.function_id = ? "
                "ORDER BY a.confidence DESC",
                (var_id, var_id, scope)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT a.ptr1_node_id, a.ptr2_node_id, a.kind, a.confidence, "
                "a.analysis, a.function_id, "
                "n1.name AS var1, n2.name AS var2 "
                "FROM alias_sets a "
                "LEFT JOIN cgdb_nodes n1 ON a.ptr1_node_id = n1.id "
                "LEFT JOIN cgdb_nodes n2 ON a.ptr2_node_id = n2.id "
                "WHERE (a.ptr1_node_id = ? OR a.ptr2_node_id = ?) "
                "ORDER BY a.confidence DESC",
                (var_id, var_id)
            ).fetchall()
        return [{
            "alias_var": r["var2"] if r["ptr1_node_id"] == var_id else r["var1"],
            "kind": r["kind"],
            "confidence": r["confidence"],
            "analysis": r["analysis"] if "analysis" in r.keys() else "heuristic",
            "function_id": r["function_id"] if "function_id" in r.keys() else None,
        } for r in rows]
    except Exception as exc:
        return [{"error": str(exc)}]
    finally:
        _close_conn(conn)


def _tool_trace_data_flow(args: dict, graph_dir: str) -> dict:
    """trace_data_flow(from_var, to_var?) -> {path, deps, locs}"""
    from_var = args.get("from_var", "")
    to_var = args.get("to_var")
    if not from_var:
        return {"error": "from_var is required"}
    conn = None
    try:
        conn = _get_conn(graph_dir)
        # Look up from_var SSA value
        from_row = conn.execute(
            "SELECT s.id, s.function_id, s.value_name, s.def_line "
            "FROM ssa_values s WHERE s.value_name = ? LIMIT 1",
            (from_var,)
        ).fetchone()
        if from_row is None:
            return {"error": f"from_var {from_var} not found in ssa_values"}
        from_ssa_id = from_row["id"]
        # If to_var specified, find to_ssa_id
        to_ssa_id = None
        if to_var:
            to_row = conn.execute(
                "SELECT id FROM ssa_values WHERE value_name = ? LIMIT 1",
                (to_var,)
            ).fetchone()
            if to_row:
                to_ssa_id = to_row["id"]
        # Trace data_deps
        if to_ssa_id:
            deps = conn.execute(
                "WITH RECURSIVE dep_chain(from_id, to_id, kind, fn_id, depth) AS ("
                "  SELECT from_ssa_id, to_ssa_id, kind, function_id, 0 "
                "  FROM data_deps WHERE from_ssa_id = ? "
                "  UNION "
                "  SELECT d.from_ssa_id, d.to_ssa_id, d.kind, d.function_id, dc.depth+1 "
                "  FROM data_deps d JOIN dep_chain dc ON d.from_ssa_id = dc.to_id "
                "  WHERE dc.to_id = ? AND dc.depth < 20"
                ") "
                "SELECT * FROM dep_chain ORDER BY depth",
                (from_ssa_id, to_ssa_id)
            ).fetchall()
        else:
            deps = conn.execute(
                "SELECT from_ssa_id, to_ssa_id, kind, function_id "
                "FROM data_deps WHERE from_ssa_id = ? LIMIT 500",
                (from_ssa_id,)
            ).fetchall()
        return {
            "from_var": from_var,
            "to_var": to_var,
            "from_ssa_id": from_ssa_id,
            "to_ssa_id": to_ssa_id,
            "path": [{
                "from_ssa_id": r["from_ssa_id"] if "from_ssa_id" in r.keys() else r["from_id"],
                "to_ssa_id": r["to_ssa_id"] if "to_ssa_id" in r.keys() else r["to_id"],
                "kind": r["kind"],
                "function_id": r["function_id"] if "function_id" in r.keys() else r["fn_id"],
                "depth": r["depth"] if "depth" in r.keys() else 0,
            } for r in deps],
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        _close_conn(conn)


def _tool_cfg_of(args: dict, graph_dir: str) -> dict:
    """cfg_of(fn) -> {blocks, edges, conditions}"""
    fn = args.get("fn", "")
    if not fn:
        return {"error": "fn is required"}
    conn = None
    try:
        conn = _get_conn(graph_dir)
        # Find the function node
        row = conn.execute(
            "SELECT id FROM cgdb_nodes WHERE name = ? AND kind = 'function' LIMIT 1",
            (fn,)
        ).fetchone()
        if row is None:
            return {"error": f"function {fn} not found"}
        fn_id = row["id"]
        # Get blocks
        blocks = conn.execute(
            "SELECT id, block_index, is_entry, is_exit, stmt_ids, "
            "byte_start, byte_end FROM basic_blocks "
            "WHERE function_id = ? ORDER BY block_index",
            (fn_id,)
        ).fetchall()
        # Get edges
        edges = conn.execute(
            "SELECT id, src_block_id, dst_block_id, kind, condition_id "
            "FROM cfg_edges WHERE function_id = ? ORDER BY id",
            (fn_id,)
        ).fetchall()
        return {
            "fn": fn,
            "fn_id": fn_id,
            "blocks": [{
                "block_id": b["id"], "block_index": b["block_index"],
                "is_entry": bool(b["is_entry"]), "is_exit": bool(b["is_exit"]),
                "stmt_ids": json.loads(b["stmt_ids"] or "[]"),
                "byte_range": [b["byte_start"], b["byte_end"]] if b["byte_start"] else None,
            } for b in blocks],
            "edges": [{
                "edge_id": e["id"], "src": e["src_block_id"], "dst": e["dst_block_id"],
                "kind": e["kind"], "condition_id": e["condition_id"],
            } for e in edges],
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        _close_conn(conn)


def _tool_path_sensitive_states(args: dict, graph_dir: str) -> dict:
    """path_sensitive_states(fn, condition?) -> {paths, constraints, states}"""
    fn = args.get("fn", "")
    condition = args.get("condition")
    if not fn:
        return {"error": "fn is required"}
    conn = None
    try:
        conn = _get_conn(graph_dir)
        row = conn.execute(
            "SELECT id FROM cgdb_nodes WHERE name = ? AND kind = 'function' LIMIT 1",
            (fn,)
        ).fetchone()
        if row is None:
            return {"error": f"function {fn} not found"}
        fn_id = row["id"]
        # Get path states
        if condition:
            states = conn.execute(
                "SELECT id, block_id, path_id, constraints, state, line "
                "FROM path_states WHERE function_id = ? "
                "AND constraints LIKE ? ORDER BY line LIMIT 500",
                (fn_id, f"%{condition}%")
            ).fetchall()
        else:
            states = conn.execute(
                "SELECT id, block_id, path_id, constraints, state, line "
                "FROM path_states WHERE function_id = ? "
                "ORDER BY line LIMIT 500",
                (fn_id,)
            ).fetchall()
        return {
            "fn": fn, "fn_id": fn_id,
            "paths": [{
                "state_id": s["id"], "block_id": s["block_id"],
                "path_id": s["path_id"],
                "constraints": json.loads(s["constraints"] or "{}"),
                "state": json.loads(s["state"] or "{}"),
                "line": s["line"],
            } for s in states],
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        _close_conn(conn)


def _tool_precise_write_set(args: dict, graph_dir: str) -> list:
    """precise_write_set(global_var) -> [{fn, loc, via_path}]"""
    var = args.get("global_var", "")
    if not var:
        return [{"error": "global_var is required"}]
    conn = None
    try:
        conn = _get_conn(graph_dir)
        rows = conn.execute(
            "SELECT pws.global_symbol_id, pws.writer_symbol_id, pws.loc_line, "
            "pws.via_path, pws.confidence, pws.source, "
            "ng.name AS global_name, nw.name AS writer_name "
            "FROM precise_write_sets pws "
            "JOIN cgdb_nodes ng ON pws.global_symbol_id = ng.id "
            "LEFT JOIN cgdb_nodes nw ON pws.writer_symbol_id = nw.id "
            "WHERE ng.name = ? "
            "ORDER BY pws.loc_line, pws.confidence DESC",
            (var,)
        ).fetchall()
        return [{
            "fn": r["writer_name"], "loc_line": r["loc_line"],
            "via_path": r["via_path"], "confidence": r["confidence"],
            "source": r["source"],
        } for r in rows]
    except Exception as exc:
        return [{"error": str(exc)}]
    finally:
        _close_conn(conn)


def _tool_dead_code_in(args: dict, graph_dir: str) -> list:
    """dead_code_in(fn) -> [{loc, reason}]"""
    fn = args.get("fn", "")
    if not fn:
        return [{"error": "fn is required"}]
    conn = None
    try:
        conn = _get_conn(graph_dir)
        row = conn.execute(
            "SELECT id FROM cgdb_nodes WHERE name = ? AND kind = 'function' LIMIT 1",
            (fn,)
        ).fetchone()
        if row is None:
            return [{"error": f"function {fn} not found"}]
        fn_id = row["id"]
        # Dead code = basic blocks not reachable from entry
        # Entry block has is_entry=1; any block not in BFS from entry is dead.
        unreachable = conn.execute(
            "WITH RECURSIVE reachable(id) AS ("
            "  SELECT id FROM basic_blocks WHERE function_id = ? AND is_entry = 1 "
            "  UNION "
            "  SELECT ce.dst_block_id FROM cfg_edges ce "
            "  JOIN reachable r ON ce.src_block_id = r.id "
            "  WHERE ce.function_id = ?"
            ") "
            "SELECT b.id, b.block_index, b.byte_start, b.byte_end, b.stmt_ids "
            "FROM basic_blocks b WHERE b.function_id = ? "
            "AND b.id NOT IN (SELECT id FROM reachable)",
            (fn_id, fn_id, fn_id)
        ).fetchall()
        return [{
            "loc": {"block_id": r["id"], "block_index": r["block_index"],
                    "byte_range": [r["byte_start"], r["byte_end"]] if r["byte_start"] else None},
            "reason": "unreachable from entry block",
        } for r in unreachable]
    except Exception as exc:
        return [{"error": str(exc)}]
    finally:
        _close_conn(conn)


# ===========================================================================
# 写回与事务 tools (2)
# ===========================================================================

def _tool_commit_db_transaction(args: dict, graph_dir: str) -> dict:
    """commit_db_transaction(transaction_id) -> {render_ok, consistency_ok, ...}"""
    from _builder.writeback_pipeline import commit_db_transaction as _commit
    tx_id = args.get("transaction_id", "")
    if not tx_id:
        return {"error": "transaction_id is required"}
    run_compile = bool(args.get("run_compile", True))
    run_lint = bool(args.get("run_lint", False))
    run_clang_format = bool(args.get("run_clang_format", False))
    git_commit = bool(args.get("git_commit", False))
    commit_message = args.get("commit_message")
    conn = None
    try:
        conn = _get_conn(graph_dir)
        result = _commit(
            conn, graph_dir, graph_dir, tx_id,
            run_compile=run_compile, run_lint=run_lint,
            run_clang_format=run_clang_format,
            git_commit=git_commit, commit_message=commit_message,
        )
        return result.to_dict()
    except Exception as exc:
        return {"error": str(exc), "applied": False}
    finally:
        _close_conn(conn)


def _tool_rollback_db_transaction(args: dict, graph_dir: str) -> dict:
    """rollback_db_transaction(transaction_id) -> {rolled_back: true}"""
    from _builder.writeback_pipeline import rollback_db_transaction as _rollback
    tx_id = args.get("transaction_id", "")
    if not tx_id:
        return {"error": "transaction_id is required"}
    conn = None
    try:
        conn = _get_conn(graph_dir)
        ok = _rollback(conn, graph_dir, tx_id)
        return {"rolled_back": ok}
    except Exception as exc:
        return {"rolled_back": False, "error": str(exc)}
    finally:
        _close_conn(conn)


# ===========================================================================
# 高级语义编辑 tools (3)
# ===========================================================================

def _tool_insert_node_after(args: dict, graph_dir: str) -> dict:
    """insert_node_after(ast_node_id, node_spec) -> {new_node_id, token_ids, transaction_id}"""
    ast_node_id = int(args.get("ast_node_id", 0))
    node_spec = args.get("node_spec", {})
    if not ast_node_id or not node_spec:
        return {"error": "ast_node_id and node_spec are required"}
    conn = None
    try:
        conn = _get_conn(graph_dir)
        # Look up anchor node
        anchor = conn.execute(
            "SELECT id, file_id, line, byte_end, enclosing_symbol_id "
            "FROM cgdb_nodes WHERE id = ?", (ast_node_id,)
        ).fetchone()
        if anchor is None:
            return {"error": f"ast_node_id {ast_node_id} not found"}
        # Insert new node right after anchor
        new_kind = node_spec.get("kind", "stmt")
        new_name = node_spec.get("name", "_inserted")
        new_fqn = node_spec.get("fqn", new_name)
        new_line = node_spec.get("line", anchor["line"])
        new_byte = node_spec.get("byte_start", anchor["byte_end"])
        cur = conn.execute(
            "INSERT INTO cgdb_nodes (kind, name, fqn, file_id, line, col, "
            "byte_start, byte_end, attrs, source_layer, confidence, "
            "enclosing_symbol_id) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?, '{}', 'llm', 0.7, ?)",
            (new_kind, new_name, new_fqn, anchor["file_id"], new_line,
             new_byte, new_byte, anchor["enclosing_symbol_id"])
        )
        new_id = cur.lastrowid
        conn.commit()
        return {
            "new_node_id": new_id,
            "token_ids": [],  # tokens not yet created
            "transaction_id": f"insert_node_{new_id}",
            "note": "DB node inserted; run commit_db_transaction to render and write to disk",
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        _close_conn(conn)


def _tool_delete_node(args: dict, graph_dir: str) -> dict:
    """delete_node(ast_node_id) -> {affected_tokens, transaction_id}"""
    ast_node_id = int(args.get("ast_node_id", 0))
    if not ast_node_id:
        return {"error": "ast_node_id is required"}
    conn = None
    try:
        conn = _get_conn(graph_dir)
        # Find affected tokens
        affected_tokens = [r[0] for r in conn.execute(
            "SELECT id FROM tokens WHERE ast_node_id = ?", (ast_node_id,)
        ).fetchall()]
        # Soft-delete the node (mark last_seen_version as 0)
        conn.execute(
            "UPDATE cgdb_nodes SET last_seen_version = 0 WHERE id = ?",
            (ast_node_id,)
        )
        # Soft-delete associated tokens
        conn.execute(
            "UPDATE tokens SET ast_node_id = NULL WHERE ast_node_id = ?",
            (ast_node_id,)
        )
        conn.commit()
        return {
            "affected_tokens": affected_tokens,
            "transaction_id": f"delete_node_{ast_node_id}",
            "note": "DB node soft-deleted; run commit_db_transaction to render and write to disk",
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        _close_conn(conn)


def _tool_add_function(args: dict, graph_dir: str) -> dict:
    """add_function(signature, body_tokens) -> {symbol_id, token_ids, transaction_id}"""
    signature = args.get("signature", "")
    body_tokens = args.get("body_tokens", [])
    if not signature:
        return {"error": "signature is required"}
    conn = None
    try:
        conn = _get_conn(graph_dir)
        # Parse function name from signature (very naive — just first identifier)
        sig_words = signature.replace("(", " ").replace("*", " ").split()
        fn_name = sig_words[1] if len(sig_words) > 1 else sig_words[-1]
        # Insert new function node
        cur = conn.execute(
            "INSERT INTO cgdb_nodes (kind, name, fqn, file_id, line, col, "
            "byte_start, byte_end, signature, body_text, attrs, source_layer, "
            "confidence) "
            "VALUES ('function', ?, ?, NULL, 0, 0, 0, 0, ?, '', "
            "'{\"inserted\":true}', 'llm', 0.7)",
            (fn_name, fn_name, signature)
        )
        symbol_id = cur.lastrowid
        # Insert body tokens (placeholder — real tokens would come from
        # tokenizing the body; we just store a placeholder for now)
        token_ids = []
        for i, tok in enumerate(body_tokens[:200]):  # cap at 200 tokens
            cur = conn.execute(
                "INSERT INTO tokens (file_id, seq, kind, spelling, line, col, "
                "byte_offset, byte_length, preceding_whitespace, ast_node_id) "
                "VALUES (NULL, ?, ?, ?, 0, 0, 0, 0, '', ?)",
                (i, tok.get("kind", "identifier"),
                 tok.get("spelling", ""), symbol_id)
            )
            token_ids.append(cur.lastrowid)
        conn.commit()
        return {
            "symbol_id": symbol_id,
            "token_ids": token_ids,
            "transaction_id": f"add_function_{symbol_id}",
            "note": "DB function added; run commit_db_transaction to render and write to disk",
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        _close_conn(conn)


# ===========================================================================
# TOOLS_REPORT dict — imported by mcp_server.py and merged into TOOLS
# ===========================================================================

TOOLS_REPORT = {
    # ---- L1 (8) ----
    "render_source": {
        "description": "Render source code from DB tokens table for a file_id. Returns sha256 and matches_disk flag. (design-report L1)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "integer", "description": "File ID in cgdb_files"},
            },
            "required": ["file_id"],
        },
        "handler": _tool_render_source,
    },
    "verify_consistency": {
        "description": "Verify character-level sha256 consistency: render DB tokens, compare against disk file sha256. Records mismatch in alignment_errors. (design-report L1)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "integer", "description": "File ID to verify"},
            },
            "required": ["file_id"],
        },
        "handler": _tool_verify_consistency,
    },
    "edit_token": {
        "description": "Edit a single token's spelling by token_id. Returns affected AST nodes. Use commit_db_transaction to write to disk. (design-report L1)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "token_id": {"type": "integer", "description": "Token ID to edit"},
                "new_text": {"type": "string", "description": "New spelling for the token"},
            },
            "required": ["token_id", "new_text"],
        },
        "handler": _tool_edit_token,
    },
    "insert_token": {
        "description": "Insert one or more tokens after a given anchor token_id. Shifts subsequent token seq values. (design-report L1)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "after_token_id": {"type": "integer", "description": "Anchor token ID"},
                "tokens": {
                    "type": "array",
                    "items": {"type": "object",
                              "properties": {
                                  "kind": {"type": "string"},
                                  "spelling": {"type": "string"},
                                  "preceding_whitespace": {"type": "string"},
                              }},
                    "description": "Tokens to insert",
                },
            },
            "required": ["after_token_id", "tokens"],
        },
        "handler": _tool_insert_token,
    },
    "delete_token": {
        "description": "Delete a single token by token_id. Shifts subsequent token seq values down. (design-report L1)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "token_id": {"type": "integer", "description": "Token ID to delete"},
            },
            "required": ["token_id"],
        },
        "handler": _tool_delete_token,
    },
    "find_macros": {
        "description": "Find macro definitions and their invocation sites by name (or all if name omitted). (design-report L1)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Macro name (optional)"},
            },
            "required": [],
        },
        "handler": _tool_find_macros,
    },
    "get_pp_branches": {
        "description": "Get the conditional compilation (#ifdef/#ifndef/#if/#elif/#else/#endif) branch tree for a file_id. (design-report L1)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "integer", "description": "File ID"},
            },
            "required": ["file_id"],
        },
        "handler": _tool_get_pp_branches,
    },
    "get_string_literals": {
        "description": "Find string literals (with optional pattern filter). Returns decoded text, encoding, security flags. (design-report L1)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Substring pattern (optional)"},
            },
            "required": [],
        },
        "handler": _tool_get_string_literals,
    },
    # ---- L2 (8) ----
    "find_symbol": {
        "description": "Find a symbol by name (and optional kind). Returns location, signature, doc, token_range. (design-report L2)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name"},
                "kind": {"type": "string", "description": "Symbol kind (optional)"},
            },
            "required": ["name"],
        },
        "handler": _tool_find_symbol,
    },
    "callers_of": {
        "description": "Find all functions that call the given function. Returns caller, location, call_kind. (design-report L2)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fn": {"type": "string", "description": "Function name"},
            },
            "required": ["fn"],
        },
        "handler": _tool_callers_of,
    },
    "callees_of": {
        "description": "Find all functions called by the given function. Returns callee, location, call_kind. (design-report L2)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fn": {"type": "string", "description": "Function name"},
            },
            "required": ["fn"],
        },
        "handler": _tool_callees_of,
    },
    "who_writes": {
        "description": "Find all functions that write to a global variable. (design-report L2)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "global_var": {"type": "string", "description": "Global variable name"},
            },
            "required": ["global_var"],
        },
        "handler": _tool_who_writes,
    },
    "who_reads": {
        "description": "Find all functions that read a global variable. (design-report L2)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "global_var": {"type": "string", "description": "Global variable name"},
            },
            "required": ["global_var"],
        },
        "handler": _tool_who_reads,
    },
    "get_context": {
        "description": "Get AST/code/comments/token_range context around a location (within radius_lines). (design-report L2)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "loc": {"type": "object",
                        "properties": {
                            "file_id": {"type": "integer"},
                            "line": {"type": "integer"},
                        },
                        "required": ["file_id", "line"]},
                "radius_lines": {"type": "integer", "description": "Default 5"},
            },
            "required": ["loc"],
        },
        "handler": _tool_get_context,
    },
    "impact_analysis": {
        "description": "Analyze the impact of changing a symbol — find all reverse-reachable callers via recursive CTE. (design-report L2)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol name"},
            },
            "required": ["symbol"],
        },
        "handler": _tool_impact_analysis,
    },
    "get_module_view": {
        "description": "Get a module view: files, symbols_summary, deps, metrics. (design-report L2)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "module": {"type": "string", "description": "Module/domain name"},
            },
            "required": ["module"],
        },
        "handler": _tool_get_module_view,
    },
    # ---- L3 (7) ----
    "indirect_targets": {
        "description": "Find possible indirect call targets at a call site (from indirect_calls table). Returns target_fn, confidence, analysis. (design-report L3)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "call_site_loc": {"type": "object",
                                  "properties": {
                                      "file_id": {"type": "integer"},
                                      "line": {"type": "integer"},
                                  },
                                  "required": ["file_id", "line"]},
            },
            "required": ["call_site_loc"],
        },
        "handler": _tool_indirect_targets,
    },
    "alias_set": {
        "description": "Find the alias set of a variable (may/must/no-alias, confidence, analysis source). (design-report L3)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "variable": {"type": "string", "description": "Variable name"},
                "scope": {"type": "string", "description": "Function scope (optional)"},
            },
            "required": ["variable"],
        },
        "handler": _tool_alias_set,
    },
    "trace_data_flow": {
        "description": "Trace data flow between two variables via SSA data_deps (recursive CTE). Returns path with deps and locs. (design-report L3)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_var": {"type": "string", "description": "Source variable name"},
                "to_var": {"type": "string", "description": "Target variable name (optional)"},
            },
            "required": ["from_var"],
        },
        "handler": _tool_trace_data_flow,
    },
    "cfg_of": {
        "description": "Get the control-flow graph of a function. Returns blocks + edges. (design-report L3)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fn": {"type": "string", "description": "Function name"},
            },
            "required": ["fn"],
        },
        "handler": _tool_cfg_of,
    },
    "path_sensitive_states": {
        "description": "Get path-sensitive analysis states for a function (optionally filtered by a condition). (design-report L3)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fn": {"type": "string", "description": "Function name"},
                "condition": {"type": "string", "description": "Condition filter (optional)"},
            },
            "required": ["fn"],
        },
        "handler": _tool_path_sensitive_states,
    },
    "precise_write_set": {
        "description": "Get the precise write set of a global variable (from precise_write_sets table). (design-report L3)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "global_var": {"type": "string", "description": "Global variable name"},
            },
            "required": ["global_var"],
        },
        "handler": _tool_precise_write_set,
    },
    "dead_code_in": {
        "description": "Find dead code (unreachable blocks) in a function via CFG BFS from entry. (design-report L3)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fn": {"type": "string", "description": "Function name"},
            },
            "required": ["fn"],
        },
        "handler": _tool_dead_code_in,
    },
    # ---- 写回 (2) ----
    "commit_db_transaction": {
        "description": "Commit a write-back transaction: render DB → source bytes, run clang-format (optional), compile, lint, sha256 verify, write to disk, git commit (optional). All gates must pass or rollback. (design-report B.4)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "transaction_id": {"type": "string", "description": "Transaction ID from begin/edit_token/insert_token/..."},
                "run_compile": {"type": "boolean", "description": "Run clang -fsyntax-only (default true)"},
                "run_lint": {"type": "boolean", "description": "Run cppcheck/clang-tidy (default false)"},
                "run_clang_format": {"type": "boolean", "description": "Run clang-format (default false)"},
                "git_commit": {"type": "boolean", "description": "Run git commit after write (default false)"},
                "commit_message": {"type": "string", "description": "Git commit message (optional)"},
            },
            "required": ["transaction_id"],
        },
        "handler": _tool_commit_db_transaction,
    },
    "rollback_db_transaction": {
        "description": "Roll back a write-back transaction: restore DB from snapshot, delete .tmp files. (design-report B.4)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "transaction_id": {"type": "string", "description": "Transaction ID"},
            },
            "required": ["transaction_id"],
        },
        "handler": _tool_rollback_db_transaction,
    },
    # ---- 高级编辑 (3) ----
    "insert_node_after": {
        "description": "Insert a new AST node after a given anchor node_id. Use commit_db_transaction to render and write to disk. (design-report B.5)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ast_node_id": {"type": "integer", "description": "Anchor AST node ID"},
                "node_spec": {"type": "object",
                              "properties": {
                                  "kind": {"type": "string"},
                                  "name": {"type": "string"},
                                  "fqn": {"type": "string"},
                                  "line": {"type": "integer"},
                                  "byte_start": {"type": "integer"},
                              },
                              "required": ["kind", "name"]},
            },
            "required": ["ast_node_id", "node_spec"],
        },
        "handler": _tool_insert_node_after,
    },
    "delete_node": {
        "description": "Soft-delete an AST node by ID. Marks last_seen_version=0. Use commit_db_transaction to render and write to disk. (design-report B.5)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ast_node_id": {"type": "integer", "description": "AST node ID to delete"},
            },
            "required": ["ast_node_id"],
        },
        "handler": _tool_delete_node,
    },
    "add_function": {
        "description": "Add a new function to the graph. Inserts a function node + body tokens. Use commit_db_transaction to render and write to disk. (design-report B.5)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "signature": {"type": "string", "description": "Function signature"},
                "body_tokens": {"type": "array",
                                "items": {"type": "object",
                                          "properties": {
                                              "kind": {"type": "string"},
                                              "spelling": {"type": "string"},
                                          }},
                                "description": "Body tokens (max 200)"},
            },
            "required": ["signature"],
        },
        "handler": _tool_add_function,
    },
}
