"""callgraph builder module: update-node / update-edge.

LLM-driven incremental supplements to the callgraph database.

Design principles (per user requirement 2026-08-01):
- 内容可缺少但必须准确 (content may be missing but must be accurate)
- LLM 读源码后调用接口直接补充到数据库 (LLM reads source, then writes supplement to DB)
- **DB 写入必须用户确认** (DB writes must require user confirmation — prevents
  LLM hallucinations from polluting the database)

Supplements are non-destructive: original scan data is preserved, LLM-added
attributes are stored with `_supplemented` prefix in the node's `extra` field
(JSON backend) or `extra_json` column (SQLite backend), with `source` and
`confidence` metadata so downstream queries can distinguish facts from
supplements.
"""

import os
import sys
import json
from pathlib import Path
from typing import Dict, List

from _builder.utils import _find_node_id
import logging


# ---------------------------------------------------------------------------
# Attribute parsing
# ---------------------------------------------------------------------------

def _parse_attr_assignments(attr_list: List[str]) -> Dict[str, object]:
    """Parse repeated --attr key=value arguments into a dict.

    Value may be:
      - bare string: --attr 'call_condition=#ifdef CONFIG_X'
      - JSON:        --attr 'params=[{"name":"ctx","type":"struct foo *"}]'

    Bare strings are left as-is. JSON values (starting with [ or {) are parsed.
    """
    out = {}
    for item in attr_list or []:
        if '=' not in item:
            raise ValueError(f"--attr must be key=value, got: {item!r}")
        key, _, raw = item.partition('=')
        key = key.strip()
        raw = raw.strip()
        if not key:
            raise ValueError(f"--attr key cannot be empty: {item!r}")
        # Try JSON for structured values
        if raw and raw[0] in '[{':
            try:
                out[key] = json.loads(raw)
                continue
            except json.JSONDecodeError:
                # Fall through to treat as bare string
                pass
        out[key] = raw
    return out


# ---------------------------------------------------------------------------
# Confirmation gate
# ---------------------------------------------------------------------------

def _format_value_preview(val, max_len: int = 200) -> str:
    """Format a value for human-readable preview in the confirmation prompt."""
    if val is None:
        return "(none)"
    if isinstance(val, (dict, list)):
        s = json.dumps(val, ensure_ascii=False)
    else:
        s = str(val)
    if len(s) > max_len:
        return s[:max_len] + f"... (+{len(s) - max_len} chars)"
    return s


def _preview_node_changes(node_id: str, old_attrs: Dict, new_attrs: Dict,
                          source: str, confidence: str) -> str:
    """Build a human-readable diff preview for node updates."""
    lines = [
        f"  Node: {node_id}",
        f"  Source: {source}  |  Confidence: {confidence}",
        "",
        "  Attribute changes:",
    ]
    if not new_attrs:
        lines.append("    (no attributes to change)")
        return "\n".join(lines)
    for key, new_val in new_attrs.items():
        stored_key = f"{key}_supplemented" if not key.startswith("_") else key
        old_val = old_attrs.get(stored_key, old_attrs.get(key, ""))
        old_repr = _format_value_preview(old_val)
        new_repr = _format_value_preview(new_val)
        lines.append(f"    {key}:")
        lines.append(f"      old: {old_repr}")
        lines.append(f"      new: {new_repr}")
        lines.append(f"      stored as: {stored_key}")
    return "\n".join(lines)


def _preview_edge_changes(invoker_id: str, invoked_id: str, old_attrs: Dict,
                          new_attrs: Dict, source: str, confidence: str) -> str:
    """Build a human-readable diff preview for edge updates."""
    lines = [
        f"  Edge: {invoker_id}  ->  {invoked_id}",
        f"  Source: {source}  |  Confidence: {confidence}",
        "",
        "  Attribute changes:",
    ]
    if not new_attrs:
        lines.append("    (no attributes to change)")
        return "\n".join(lines)
    for key, new_val in new_attrs.items():
        old_val = old_attrs.get(key, "")
        old_repr = _format_value_preview(old_val)
        new_repr = _format_value_preview(new_val)
        lines.append(f"    {key}:")
        lines.append(f"      old: {old_repr}")
        lines.append(f"      new: {new_repr}")
    return "\n".join(lines)


def _confirm(prompt: str, auto_yes: bool) -> bool:
    """Prompt user for y/n confirmation. auto_yes bypasses the prompt."""
    if auto_yes:
        print(f"[confirm] --yes specified, auto-approving:\n{prompt}\n")
        return True
    print(prompt)
    print()
    try:
        ans = input("Proceed with write? [y/N]: ").strip().lower()
    except EOFError:
        print("\n[confirm] no input available (non-interactive) — write aborted.")
        return False
    if ans not in ("y", "yes"):
        print("[confirm] write aborted by user.")
        return False
    return True


# ---------------------------------------------------------------------------
# Storage backend detection
# ---------------------------------------------------------------------------

def _detect_backend(graph_dir: str) -> str:
    """Return 'json' or 'sqlite' based on which artifacts exist."""
    master = os.path.join(graph_dir, "code2database_master.json")
    if os.path.exists(master):
        return "json"
    db = os.path.join(graph_dir, "code2database.db")
    if os.path.exists(db):
        return "sqlite"
    print(f"Error: no code2database_master.json or code2database.db in {graph_dir}",
          file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# JSON backend write
# ---------------------------------------------------------------------------

def _json_update_node(graph_dir: str, node_id: str, attrs: Dict,
                     source: str, confidence: str) -> bool:
    """Update a node in JSON backend via _load_full_graph + split_by_domain.

    Stores LLM supplements with `_supplemented` suffix to preserve original
    scan data. Also records `_supplement_meta` with source/confidence/timestamp.
    When a SQLite code2database.db also exists in the graph dir, propagate
    selected fields (semantic_desc -> cgdb_nodes.description) to keep the
    cgdb layer in sync with JSON-side supplements.
    """
    from datetime import datetime
    from _builder.graph_build import _load_full_graph, split_by_domain

    G = _load_full_graph(graph_dir)
    if node_id not in G:
        print(f"Error: node {node_id!r} not found in graph", file=sys.stderr)
        return False

    ndata = G.nodes[node_id]
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    meta_key = "_supplement_meta"
    meta = ndata.get(meta_key, {})

    for key, val in attrs.items():
        stored_key = f"{key}_supplemented" if not key.startswith("_") else key
        ndata[stored_key] = val
        meta[stored_key] = {
            "source": source,
            "confidence": confidence,
            "timestamp": timestamp,
            "original": ndata.get(key, ""),
        }
    ndata[meta_key] = meta

    master = json.loads(
        Path(os.path.join(graph_dir, "code2database_master.json")).read_text(encoding="utf-8"))
    source_root = master.get("source_root", "")
    split_by_domain(G, graph_dir, source_root)

    # Propagate to SQLite cgdb_nodes if the DB coexists with JSON.
    db_path = os.path.join(graph_dir, "code2database.db")
    if os.path.exists(db_path) and "semantic_desc" in attrs and attrs["semantic_desc"]:
        try:
            import sqlite3 as _sqlite3
            conn = _sqlite3.connect(db_path)
            try:
                conn.execute(
                    "UPDATE cgdb_nodes SET description=? WHERE fqn=?",
                    (attrs["semantic_desc"], node_id))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    return True


def _json_update_edge(graph_dir: str, invoker_id: str, invoked_id: str,
                     attrs: Dict, source: str, confidence: str) -> bool:
    """Update an edge in JSON backend.

    Edge supplements are stored directly on the edge attributes (no
    `_supplemented` prefix needed since edges are not destructively
    overwritten — we merge). A `_supplement_meta` key records provenance.
    """
    from datetime import datetime
    from _builder.graph_build import _load_full_graph, split_by_domain

    G = _load_full_graph(graph_dir)
    if invoker_id not in G:
        print(f"Error: caller node {invoker_id!r} not found", file=sys.stderr)
        return False
    if invoked_id not in G:
        print(f"Error: callee node {invoked_id!r} not found", file=sys.stderr)
        return False
    if not G.has_edge(invoker_id, invoked_id):
        print(f"Error: no edge from {invoker_id!r} to {invoked_id!r}", file=sys.stderr)
        return False

    edata = G.get_edge_data(invoker_id, invoked_id) or {}
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    meta = edata.get("_supplement_meta", {})

    for key, val in attrs.items():
        old_val = edata.get(key, "")
        meta[key] = {
            "source": source,
            "confidence": confidence,
            "timestamp": timestamp,
            "original": old_val,
        }
        G[invoker_id][invoked_id][key] = val
    G[invoker_id][invoked_id]["_supplement_meta"] = meta

    master = json.loads(
        Path(os.path.join(graph_dir, "code2database_master.json")).read_text(encoding="utf-8"))
    source_root = master.get("source_root", "")
    split_by_domain(G, graph_dir, source_root)
    return True


# ---------------------------------------------------------------------------
# SQLite backend write
# ---------------------------------------------------------------------------

def _sqlite_update_node(graph_dir: str, node_id: str, attrs: Dict,
                       source: str, confidence: str) -> bool:
    """Update a node in SQLite backend via direct UPDATE on functions.extra_json.

    Loads existing extra_json, merges supplemented keys, writes back.
    Also propagates selected supplement fields (semantic_desc, role, etc.)
    to the cgdb_nodes table so cgdb-layer queries see the enriched data.
    """
    import sqlite3
    import zlib
    from datetime import datetime

    db_path = os.path.join(graph_dir, "code2database.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT extra_json FROM functions WHERE id=?", (node_id,)).fetchone()
        if not row:
            print(f"Error: node {node_id!r} not found in SQLite db", file=sys.stderr)
            return False

        extra = {}
        if row[0]:
            try:
                extra = json.loads(row[0])
            except json.JSONDecodeError:
                extra = {}

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        meta = extra.get("_supplement_meta", {})
        for key, val in attrs.items():
            stored_key = f"{key}_supplemented" if not key.startswith("_") else key
            meta[stored_key] = {
                "source": source,
                "confidence": confidence,
                "timestamp": timestamp,
                "original": extra.get(key, ""),
            }
            extra[stored_key] = val
        extra["_supplement_meta"] = meta

        conn.execute(
            "UPDATE functions SET extra_json=? WHERE id=?",
            (json.dumps(extra, ensure_ascii=False), node_id))

        # Propagate to cgdb_nodes so cgdb-layer queries see enriched data.
        # Map supplement field names to cgdb_nodes columns where applicable.
        # cgdb_nodes.id is a numeric hash, but functions.id (string) matches
        # cgdb_nodes.fqn — so we join via fqn to find the right cgdb row.
        cgdb_column_map = {
            "semantic_desc": "description",
        }
        cgdb_updates = {}
        for sup_key, cgdb_col in cgdb_column_map.items():
            if sup_key in attrs and attrs[sup_key]:
                cgdb_updates[cgdb_col] = attrs[sup_key]
        if cgdb_updates:
            set_clauses = ", ".join(f"{k}=?" for k in cgdb_updates)
            params = list(cgdb_updates.values()) + [node_id]
            try:
                conn.execute(
                    f"UPDATE cgdb_nodes SET {set_clauses} WHERE fqn=?",
                    params)
            except sqlite3.OperationalError:
                # cgdb_nodes table may not exist in legacy-only DBs; skip silently.
                pass

        conn.commit()
        return True
    finally:
        conn.close()


def _sqlite_update_edge(graph_dir: str, invoker_id: str, invoked_id: str,
                       attrs: Dict, source: str, confidence: str) -> bool:
    """Update an edge in SQLite backend.

    Maps supplemented keys to edge columns where possible (call_condition,
    concurrency, confidence), and stores the rest in callee_arg_json as a
    JSON merge. Provenance goes into a `_supplement_meta` key in
    callee_arg_json.
    """
    import sqlite3
    from datetime import datetime

    db_path = os.path.join(graph_dir, "code2database.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Verify edge exists
        row = conn.execute(
            "SELECT id, call_condition, concurrency, confidence, callee_arg_json "
            "FROM edges WHERE invoker_id=? AND invoked_id=? LIMIT 1",
            (invoker_id, invoked_id)).fetchone()
        if not row:
            print(f"Error: no edge from {invoker_id!r} to {invoked_id!r} in SQLite db",
                  file=sys.stderr)
            return False

        edge_id = row[0]
        old_call_cond = row[1] or ""
        old_concurrency = row[2] or ""
        old_confidence = row[3] or ""
        old_callee_arg_json = row[4] or ""

        column_updates = {}
        if "call_condition" in attrs:
            column_updates["call_condition"] = attrs["call_condition"]
        if "concurrency" in attrs:
            column_updates["concurrency"] = attrs["concurrency"]
        if "confidence" in attrs:
            column_updates["confidence"] = attrs["confidence"]

        # Remaining attrs go into callee_arg_json as a JSON merge
        remaining = {k: v for k, v in attrs.items()
                     if k not in ("call_condition", "concurrency", "confidence")}
        invoked_arg = {}
        if old_callee_arg_json:
            try:
                invoked_arg = json.loads(old_callee_arg_json)
            except json.JSONDecodeError:
                invoked_arg = {}
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        meta = invoked_arg.get("_supplement_meta", {})
        for key, val in remaining.items():
            meta[key] = {
                "source": source,
                "confidence": confidence,
                "timestamp": timestamp,
                "original": invoked_arg.get(key, ""),
            }
            invoked_arg[key] = val
        if meta:
            invoked_arg["_supplement_meta"] = meta

        if column_updates:
            set_clauses = ", ".join(f"{k}=?" for k in column_updates)
            params = list(column_updates.values()) + [edge_id]
            conn.execute(f"UPDATE edges SET {set_clauses} WHERE id=?", params)
        if remaining:
            conn.execute(
                "UPDATE edges SET callee_arg_json=? WHERE id=?",
                (json.dumps(invoked_arg, ensure_ascii=False), edge_id))
        conn.commit()
        return True
    finally:
        conn.close()


def _sqlite_get_node_extra(graph_dir: str, node_id: str) -> Dict:
    """Read current node extra_json + primary attrs for preview."""
    import sqlite3
    db_path = os.path.join(graph_dir, "code2database.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM functions WHERE id=?", (node_id,)).fetchone()
        if not row:
            return {}
        d = dict(row)
        extra = {}
        if d.get("extra_json"):
            try:
                extra = json.loads(d["extra_json"])
            except json.JSONDecodeError:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        return extra
    finally:
        conn.close()


def _sqlite_get_edge_attrs(graph_dir: str, invoker_id: str, invoked_id: str) -> Dict:
    """Read current edge attrs for preview."""
    import sqlite3
    db_path = os.path.join(graph_dir, "code2database.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT call_condition, concurrency, confidence, callee_arg_json "
            "FROM edges WHERE invoker_id=? AND invoked_id=? LIMIT 1",
            (invoker_id, invoked_id)).fetchone()
        if not row:
            return {}
        d = dict(row)
        out = {
            "call_condition": d.get("call_condition") or "",
            "concurrency": d.get("concurrency") or "",
            "confidence": d.get("confidence") or "",
        }
        if d.get("callee_arg_json"):
            try:
                extra = json.loads(d["callee_arg_json"])
                # Don't include _supplement_meta in preview
                out.update({k: v for k, v in extra.items()
                           if k != "_supplement_meta"})
            except json.JSONDecodeError:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_update_node(args):
    """LLM-driven incremental supplement of node attributes.

    Stores supplements non-destructively (original scan data preserved).
    Requires user confirmation by default; --yes bypasses for automation.

    Examples:
      update-node --graph out/ --node foo --attr 'params=[{"name":"ctx"}]' \
                  --source llm_supplement --confidence EXTRACTED
    """
    graph_dir = args.graph
    node_hint = args.node
    source = args.source
    confidence = args.confidence
    auto_yes = getattr(args, "yes", False)

    try:
        attrs = _parse_attr_assignments(args.attr)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not attrs:
        print("Error: at least one --attr key=value is required", file=sys.stderr)
        sys.exit(1)

    backend = _detect_backend(graph_dir)

    # Resolve node ID
    if backend == "json":
        from _builder.graph_build import _load_full_graph
        G = _load_full_graph(graph_dir)
        node_id = _find_node_id(G, node_hint)
        if not node_id:
            print(f"Error: node matching {node_hint!r} not found", file=sys.stderr)
            sys.exit(1)
        old_attrs = dict(G.nodes[node_id])
        # Close LazySQLiteGraph if that's what we got (won't happen for json backend)
    else:
        # SQLite: try exact ID first, then name match
        import sqlite3
        db_path = os.path.join(graph_dir, "code2database.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        node_id = ""
        try:
            row = conn.execute(
                "SELECT id FROM functions WHERE id=?", (node_hint,)).fetchone()
            if row:
                node_id = row[0]
            else:
                row = conn.execute(
                    "SELECT id FROM functions WHERE name=? LIMIT 1",
                    (node_hint,)).fetchone()
                if row:
                    node_id = row[0]
                else:
                    row = conn.execute(
                        "SELECT id FROM functions WHERE name LIKE ? LIMIT 1",
                        (f"%{node_hint}%",)).fetchone()
                    if row:
                        node_id = row[0]
        finally:
            conn.close()
        if not node_id:
            print(f"Error: node matching {node_hint!r} not found in SQLite db",
                  file=sys.stderr)
            sys.exit(1)
        old_attrs = _sqlite_get_node_extra(graph_dir, node_id)

    # Confirmation gate
    preview = _preview_node_changes(node_id, old_attrs, attrs, source, confidence)
    prompt = (
        "=== update-node: confirmation required ===\n"
        f"  Backend: {backend}\n"
        f"{preview}\n"
        "\n"
        "This will write to the callgraph database. "
        "Verify the new values are correct (e.g., you read them from source)."
    )
    if not _confirm(prompt, auto_yes):
        sys.exit(1)

    # Execute write
    if backend == "json":
        ok = _json_update_node(graph_dir, node_id, attrs, source, confidence)
    else:
        ok = _sqlite_update_node(graph_dir, node_id, attrs, source, confidence)

    if ok:
        # Invalidate query cache entries that touched this node
        try:
            from _builder.query_cache import invalidate_node
            invalidate_node(graph_dir, node_id)
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        try:
            from _builder.audit_log import log_audit
            for attr_name, attr_value in attrs.items():
                log_audit(graph_dir,
                          command="update-node",
                          target_kind="node",
                          target_id=node_id,
                          action="update",
                          attribute=attr_name,
                          before_value=old_attrs.get(attr_name),
                          after_value=attr_value,
                          reason=f"user edit (source={source}, confidence={confidence})")
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        print(f"Updated node {node_id}: {len(attrs)} attribute(s) supplemented")
        print(f"  source={source}  confidence={confidence}")
        print(f"  Supplements stored non-destructively (original scan data preserved)")
    else:
        sys.exit(1)


def cmd_update_edge(args):
    """LLM-driven incremental supplement of edge attributes.

    Examples:
      update-edge --graph out/ --from foo --to bar \
                  --attr 'call_condition=#ifdef CONFIG_X' \
                  --source llm_supplement --confidence EXTRACTED
    """
    graph_dir = args.graph
    caller_hint = args.from_node
    callee_hint = args.to_node
    source = args.source
    confidence = args.confidence
    auto_yes = getattr(args, "yes", False)

    try:
        attrs = _parse_attr_assignments(args.attr)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not attrs:
        print("Error: at least one --attr key=value is required", file=sys.stderr)
        sys.exit(1)

    backend = _detect_backend(graph_dir)

    # Resolve caller and callee IDs
    if backend == "json":
        from _builder.graph_build import _load_full_graph
        G = _load_full_graph(graph_dir)
        invoker_id = _find_node_id(G, caller_hint)
        invoked_id = _find_node_id(G, callee_hint)
        if not invoker_id:
            print(f"Error: caller matching {caller_hint!r} not found", file=sys.stderr)
            sys.exit(1)
        if not invoked_id:
            print(f"Error: callee matching {callee_hint!r} not found", file=sys.stderr)
            sys.exit(1)
        if not G.has_edge(invoker_id, invoked_id):
            print(f"Error: no edge from {invoker_id!r} to {invoked_id!r}", file=sys.stderr)
            sys.exit(1)
        old_attrs = dict(G.get_edge_data(invoker_id, invoked_id) or {})
    else:
        import sqlite3
        db_path = os.path.join(graph_dir, "code2database.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            def _resolve(hint):
                row = conn.execute(
                    "SELECT id FROM functions WHERE id=?", (hint,)).fetchone()
                if row:
                    return row[0]
                row = conn.execute(
                    "SELECT id FROM functions WHERE name=? LIMIT 1", (hint,)).fetchone()
                if row:
                    return row[0]
                row = conn.execute(
                    "SELECT id FROM functions WHERE name LIKE ? LIMIT 1",
                    (f"%{hint}%",)).fetchone()
                return row[0] if row else ""
            invoker_id = _resolve(caller_hint)
            invoked_id = _resolve(callee_hint)
        finally:
            conn.close()
        if not invoker_id:
            print(f"Error: caller matching {caller_hint!r} not found", file=sys.stderr)
            sys.exit(1)
        if not invoked_id:
            print(f"Error: callee matching {callee_hint!r} not found", file=sys.stderr)
            sys.exit(1)
        old_attrs = _sqlite_get_edge_attrs(graph_dir, invoker_id, invoked_id)
        if not old_attrs:
            print(f"Error: no edge from {invoker_id!r} to {invoked_id!r}", file=sys.stderr)
            sys.exit(1)

    # Confirmation gate
    preview = _preview_edge_changes(invoker_id, invoked_id, old_attrs, attrs,
                                    source, confidence)
    prompt = (
        "=== update-edge: confirmation required ===\n"
        f"  Backend: {backend}\n"
        f"{preview}\n"
        "\n"
        "This will write to the callgraph database. "
        "Verify the new values are correct (e.g., you read them from source)."
    )
    if not _confirm(prompt, auto_yes):
        sys.exit(1)

    # Execute write
    if backend == "json":
        ok = _json_update_edge(graph_dir, invoker_id, invoked_id, attrs, source, confidence)
    else:
        ok = _sqlite_update_edge(graph_dir, invoker_id, invoked_id, attrs, source, confidence)

    if ok:
        # Audit log: record this edge update
        try:
            from _builder.audit_log import log_audit
            for attr_name, attr_value in attrs.items():
                log_audit(graph_dir,
                          command="update-edge",
                          target_kind="edge",
                          target_id=f"{invoker_id}->{invoked_id}",
                          action="update",
                          attribute=attr_name,
                          before_value=old_attrs.get(attr_name),
                          after_value=attr_value,
                          reason=f"user edit (source={source}, confidence={confidence})")
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        print(f"Updated edge {invoker_id} -> {invoked_id}: {len(attrs)} attribute(s) supplemented")
        print(f"  source={source}  confidence={confidence}")
    else:
        sys.exit(1)


# ---------------------------------------------------------------------------
# patch-profile: LLM-driven incremental auto-profile calibration
# ---------------------------------------------------------------------------

# Map CLI flag → (top_key, sub_key) in the profile schema.
_PROFILE_FIELD_MAP = {
    "non-api-path": ("project_boundaries", "non_api_paths"),
    "test-path-pattern": ("project_boundaries", "test_path_patterns"),
    "test-file-suffix": ("project_boundaries", "test_file_suffixes"),
    "test-domain-segment": ("project_boundaries", "test_domain_segments"),
    "vendor-prefix": ("project_boundaries", "vendor_domain_prefixes"),
    "external-dir-prefix": ("project_boundaries", "external_dir_prefixes"),
    "lock-acquire-pattern": ("concurrency_patterns", "lock_acquire_patterns"),
    "lock-release-pattern": ("concurrency_patterns", "lock_release_patterns"),
    "io-main-keyword": ("io_classification", "io_main_keywords"),
    "io-side-keyword": ("io_classification", "io_side_keywords"),
}


def _load_profile(graph_dir: str) -> dict:
    """Load .code2database_profile.json from graph dir."""
    prof_path = os.path.join(graph_dir, ".code2database_profile.json")
    if not os.path.exists(prof_path):
        print(f"Error: {prof_path} not found. Run 'build' first to generate profile.",
              file=sys.stderr)
        sys.exit(1)
    return json.loads(Path(prof_path).read_text(encoding="utf-8"))


def _save_profile(graph_dir: str, profile: dict) -> None:
    """Write profile back to .code2database_profile.json (with backup)."""
    prof_path = os.path.join(graph_dir, ".code2database_profile.json")
    # Backup original
    if os.path.exists(prof_path):
        backup_path = prof_path + ".bak"
        Path(backup_path).write_text(
            Path(prof_path).read_text(encoding="utf-8"), encoding="utf-8")
    Path(prof_path).write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def _apply_patch(profile: dict, add_specs: list, remove_specs: list) -> list:
    """Apply add/remove patches to profile. Returns list of changes made."""
    changes = []
    for flag, value in add_specs:
        if flag not in _PROFILE_FIELD_MAP:
            changes.append((flag, value, "add", "ERROR: unknown flag"))
            continue
        top, sub = _PROFILE_FIELD_MAP[flag]
        bucket = profile.setdefault(top, {}).setdefault(sub, [])
        if value in bucket:
            changes.append((flag, value, "add", f"{top}.{sub} (already present)"))
        else:
            bucket.append(value)
            changes.append((flag, value, "add", f"{top}.{sub}"))

    for flag, value in remove_specs:
        if flag not in _PROFILE_FIELD_MAP:
            changes.append((flag, value, "remove", "ERROR: unknown flag"))
            continue
        top, sub = _PROFILE_FIELD_MAP[flag]
        bucket = profile.setdefault(top, {}).setdefault(sub, [])
        if value in bucket:
            bucket.remove(value)
            changes.append((flag, value, "remove", f"{top}.{sub}"))
        else:
            changes.append((flag, value, "remove", f"{top}.{sub} (not present)"))
    return changes


def cmd_patch_profile(args):
    """LLM-driven incremental calibration of auto-profile.

    Non-destructive: writes to .code2database_profile.json with .bak backup.
    Requires user confirmation by default.

    Examples:
      patch-profile --graph out/ \
        --add-non-api-path 'samples/' \
        --add-lock-acquire-pattern 'my_lock\\s*\\(' \
        --remove-vendor-prefix 'old_vendor'
    """
    graph_dir = args.graph
    auto_yes = getattr(args, "yes", False)
    source = args.source

    # Collect add/remove specs from all --add-* and --remove-* flags
    add_specs = []
    remove_specs = []
    for flag_name, (top, sub) in _PROFILE_FIELD_MAP.items():
        # Convert flag_name (e.g., "non-api-path") to argparse dest
        # (--add-non-api-path → add_non_api_path)
        add_attr = "add_" + flag_name.replace("-", "_")
        remove_attr = "remove_" + flag_name.replace("-", "_")
        for v in getattr(args, add_attr, []) or []:
            add_specs.append((flag_name, v))
        for v in getattr(args, remove_attr, []) or []:
            remove_specs.append((flag_name, v))

    if not add_specs and not remove_specs:
        print("Error: at least one --add-* or --remove-* flag is required",
              file=sys.stderr)
        sys.exit(1)

    profile = _load_profile(graph_dir)

    # Build preview
    print("=== patch-profile: confirmation required ===")
    print(f"  Profile: {os.path.join(graph_dir, '.code2database_profile.json')}")
    print(f"  Source: {source}")
    print("")
    print("  Changes:")
    for flag, value in add_specs:
        top, sub = _PROFILE_FIELD_MAP[flag]
        sign = "+"
        print(f"    {sign} {flag}: {value!r}  →  {top}.{sub}")
    for flag, value in remove_specs:
        top, sub = _PROFILE_FIELD_MAP[flag]
        sign = "-"
        print(f"    {sign} {flag}: {value!r}  →  {top}.{sub}")
    print("")

    prompt = (
        "This will modify .code2database_profile.json (a .bak backup will be created). "
        "Verify the values are correct (e.g., you confirmed them by reading source)."
    )
    if not _confirm(prompt, auto_yes):
        sys.exit(1)

    changes = _apply_patch(profile, add_specs, remove_specs)

    # Basic sanity check on patched fields (full schema validation is skipped
    # because the persisted profile is the merged form, which differs from
    # the input form the schema validator expects).
    for top, sub in _PROFILE_FIELD_MAP.values():
        if top in profile and sub in profile[top]:
            val = profile[top][sub]
            if not isinstance(val, list):
                print(f"Error: {top}.{sub} must be a list after patch, got {type(val).__name__}",
                      file=sys.stderr)
                print("Profile NOT saved. Fix the patch and retry.", file=sys.stderr)
                sys.exit(1)

    _save_profile(graph_dir, profile)

    print(f"Patched profile: {len(changes)} change(s) applied")
    print(f"  Backup saved to: {os.path.join(graph_dir, '.code2database_profile.json.bak')}")
    print(f"  Source: {source}")
    print("")
    print("To apply these changes to existing query results, re-run 'build' or")
    print("the relevant query commands (detect-races, io-path, etc.) — they")
    print("auto-load the updated profile.")
