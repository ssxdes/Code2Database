#!/usr/bin/env python3
"""Documentation-code dual source truth alignment.

The current system has a "dual source truth" problem:
- `knowledge_manager` extracts facts from docs → stored as Markdown
- `extract-semantics` extracts descriptions from docs → applied to graph nodes
  as `semantic_desc`, `external_desc`, `api_constraints`
- BUT: docs may say "function returns 0 on success" while code returns -1
- `knowledge-validate` only checks reference staleness (related_functions exist),
  NOT doc-vs-code semantic consistency
- LLM queries read both `semantic_desc` (from docs) and `body_text` (from code)
  without knowing they may disagree

This module provides:

1. **Doc-code consistency check**: compare each node's semantic_desc / external_desc
   against its actual code (body_text, signature, params, return type). Detect:
   - Return value mismatch (doc says "returns 0", code returns something else)
   - Param mismatch (doc mentions param X, code has different params)
   - Function signature change (doc was extracted against old signature)
   - Stale doc (semantic_desc's commit is older than body_text's commit)

2. **Mark doc stale**: when code changes, mark related nodes' docs as stale
   so LLM queries can flag the inconsistency.

3. **LLM query augmentation**: when describe-node runs, include a
   `doc_code_mismatches` field listing detected mismatches.

4. **Signature change detection**: compare two graph versions (or current graph
   vs source) to detect signature changes that should trigger doc re-extraction.

CLI:
    doc-code-check --graph <dir>                # check all nodes for mismatches
    doc-mark-stale --graph <dir> --node <id> --reason <text>
    doc-alignment-report --graph <dir>          # full report (Markdown)
"""

import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, List, Dict, Set, Tuple
import logging


# ---------------------------------------------------------------------------
# Mismatch detection
# ---------------------------------------------------------------------------

@dataclass
class DocCodeMismatch:
    """One detected inconsistency between doc and code."""
    node_id: str
    kind: str  # return_value / param_name / signature_change / stale_doc / missing_in_code
    doc_claim: str  # what the doc says
    code_fact: str  # what the code actually does
    severity: str = "warning"  # warning / error
    detail: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)


# Regex patterns for extracting claims from semantic_desc / external_desc
_RETURN_CLAIM_RES = [
    re.compile(r'return[s]?\s+(\d+|true|false|null|0x[0-9a-fA-F]+|\w+)\s+(?:on|if|when|for)\s+(\w+)', re.IGNORECASE),
    re.compile(r'return[s]?\s+(\w+)\s+(?:on|if|when)\s+(\w+)', re.IGNORECASE),
    re.compile(r'(\w+)\s+(?:on|if|when)\s+\w+[,\s]+return[s]?\s+(\S+)', re.IGNORECASE),
    re.compile(r'return[s]?\s+value[s]?\s*[:\-]?\s*(.+?)(?:[;\n]|$)', re.IGNORECASE),
]

_PARAM_MENTION_RES = [
    re.compile(r'@param\s*(?:\[(?:in|out|in,out)\]\s*)?(\w+)', re.IGNORECASE),
    re.compile(r'param(?:eter)?\s+(\w+)\s*[:\-]', re.IGNORECASE),
    re.compile(r'\b(\w+)\s*[:\-]\s*(?:input|output|in/out|param)', re.IGNORECASE),
]

_RETURN_VALUE_RES = [
    re.compile(r'\breturn\s+(-?\d+|true|false|null|0x[0-9a-fA-F]+|NULL|[A-Z_]+)\s*;', re.IGNORECASE),
    re.compile(r'\breturn\s+(\w+)\s*;', re.IGNORECASE),
]


def _extract_doc_return_claims(text: str) -> List[Tuple[str, str]]:
    """Extract (condition, return_value) pairs from doc text.

    Returns list of (condition, value) tuples, e.g., [("success", "0"), ("failure", "-1")].
    """
    if not text:
        return []
    claims = []
    for pat in _RETURN_CLAIM_RES:
        for m in pat.finditer(text):
            groups = m.groups()
            if len(groups) >= 2:
                cond, val = groups[0], groups[1]
                # Normalize: "return 0 on success" → ("success", "0")
                # The regex captures in different orders depending on pattern
                if cond.lower() in ("success", "failure", "error", "ok"):
                    claims.append((cond.lower(), val.lower()))
                elif val.lower() in ("success", "failure", "error", "ok"):
                    claims.append((val.lower(), cond.lower()))
                else:
                    claims.append((cond.lower(), val.lower()))
        if claims:
            break  # only use first matching pattern
    return claims


def _extract_code_return_values(body_text: str) -> List[str]:
    """Extract actual return values from function body."""
    if not body_text:
        return []
    values = []
    for pat in _RETURN_VALUE_RES:
        for m in pat.finditer(body_text):
            v = m.group(1)
            if v and v not in values:
                values.append(v.lower())
        if values:
            break
    return values


def _extract_doc_param_mentions(text: str) -> Set[str]:
    """Extract param names mentioned in doc text."""
    if not text:
        return set()
    mentions = set()
    for pat in _PARAM_MENTION_RES:
        for m in pat.finditer(text):
            mentions.add(m.group(1))
        if mentions:
            break
    # Also scan for "@param name" or "param name:" patterns more loosely
    for m in re.finditer(r'@param\s*(?:\[(?:in|out|in,out)\]\s*)?(\w+)', text, re.IGNORECASE):
        mentions.add(m.group(1))
    return mentions


def _extract_actual_params(node_data: Dict) -> List[str]:
    """Get actual param names from node data."""
    params = node_data.get("params", []) or []
    names = []
    for p in params:
        if isinstance(p, dict):
            name = p.get("name", "")
            if name:
                names.append(name)
        elif isinstance(p, str):
            names.append(p)
    return names


def _check_return_value_mismatch(node_id: str, node_data: Dict) -> List[DocCodeMismatch]:
    """Check if doc's return value claims match code's actual returns."""
    mismatches = []
    doc_text = " ".join(filter(None, [
        node_data.get("semantic_desc", ""),
        node_data.get("external_desc", ""),
        node_data.get("api_constraints", ""),
        node_data.get("doc_comment", ""),
    ]))
    if not doc_text:
        return mismatches
    body_text = node_data.get("body_text", "") or ""
    if not body_text:
        return mismatches

    doc_claims = _extract_doc_return_claims(doc_text)
    code_values = _extract_code_return_values(body_text)
    if not doc_claims or not code_values:
        return mismatches

    # For each doc claim, check if the value appears in code's returns
    for cond, val in doc_claims:
        if val not in code_values:
            # Doc says "returns 0 on success" but code never returns 0
            mismatches.append(DocCodeMismatch(
                node_id=node_id,
                kind="return_value",
                doc_claim=f"returns {val} on {cond}",
                code_fact=f"actual returns: {', '.join(code_values[:5])}",
                severity="warning",
                detail=f"doc claims return value {val} for condition {cond}, but code never returns this value",
            ))
    return mismatches


def _check_param_mismatch(node_id: str, node_data: Dict) -> List[DocCodeMismatch]:
    """Check if doc-mentioned params match actual params."""
    mismatches = []
    doc_text = " ".join(filter(None, [
        node_data.get("semantic_desc", ""),
        node_data.get("external_desc", ""),
        node_data.get("api_constraints", ""),
        node_data.get("doc_comment", ""),
    ]))
    if not doc_text:
        return mismatches
    doc_params = _extract_doc_param_mentions(doc_text)
    actual_params = _extract_actual_params(node_data)
    if not doc_params or not actual_params:
        return mismatches

    # Doc-mentioned params not in actual params
    missing_in_code = doc_params - set(actual_params)
    for p in missing_in_code:
        mismatches.append(DocCodeMismatch(
            node_id=node_id,
            kind="param_name",
            doc_claim=f"doc mentions param '{p}'",
            code_fact=f"actual params: {', '.join(actual_params)}",
            severity="warning",
            detail=f"doc references param '{p}' which is not in function signature",
        ))
    return mismatches


def _check_stale_doc(node_id: str, node_data: Dict) -> List[DocCodeMismatch]:
    """Check if doc is older than code (using commit_meta)."""
    mismatches = []
    sem_meta = node_data.get("_semantic_meta") or {}
    body_meta = node_data.get("commit_meta") or node_data.get("_commit_meta") or {}
    sem_commit = sem_meta.get("source_commit", "") if isinstance(sem_meta, dict) else ""
    body_commit = body_meta.get("source_commit", "") if isinstance(body_meta, dict) else ""
    if not sem_commit or not body_commit:
        return mismatches
    if sem_commit != body_commit:
        mismatches.append(DocCodeMismatch(
            node_id=node_id,
            kind="stale_doc",
            doc_claim=f"semantic_desc from commit {sem_commit[:12]}",
            code_fact=f"body_text from commit {body_commit[:12]}",
            severity="warning",
            detail="doc was extracted against an older commit; code may have changed since",
        ))
    return mismatches


def _check_signature_change(node_id: str, node_data: Dict) -> List[DocCodeMismatch]:
    """Check if signature in node differs from signature mentioned in doc."""
    mismatches = []
    doc_text = node_data.get("external_desc", "") or node_data.get("doc_comment", "") or ""
    if not doc_text:
        return mismatches
    actual_sig = node_data.get("signature", "") or ""
    if not actual_sig:
        return mismatches
    # If doc contains a function-signature-like pattern, compare
    # Look for "func_name(args)" in doc
    name = node_data.get("name", "")
    if not name:
        return mismatches
    # Find patterns like `name(...)` in doc
    sig_in_doc = re.findall(re.escape(name) + r'\s*\(([^)]*)\)', doc_text)
    if not sig_in_doc:
        return mismatches
    # Extract actual param types from signature
    actual_match = re.search(re.escape(name) + r'\s*\(([^)]*)\)', actual_sig)
    if not actual_match:
        return mismatches
    actual_args = actual_match.group(1).strip()
    for doc_args in sig_in_doc:
        doc_args = doc_args.strip()
        if doc_args and doc_args != actual_args:
            # Allow param name differences but flag type differences
            # Simple heuristic: if lengths differ significantly, flag
            if abs(len(doc_args) - len(actual_args)) > 5 or \
               _args_types_differ(doc_args, actual_args):
                mismatches.append(DocCodeMismatch(
                    node_id=node_id,
                    kind="signature_change",
                    doc_claim=f"doc shows {name}({doc_args[:50]})",
                    code_fact=f"actual {name}({actual_args[:50]})",
                    severity="warning",
                    detail="function signature in doc differs from code",
                ))
                break
    return mismatches


def _args_types_differ(a: str, b: str) -> bool:
    """Heuristic: compare argument types (not names) in two arg strings."""
    def types(s: str) -> List[str]:
        parts = [p.strip() for p in s.split(",") if p.strip()]
        # Take last word of each part (the type, in C-like syntax: "const char *name" → "name")
        # Actually for types, take everything except last identifier
        result = []
        for p in parts:
            tokens = p.split()
            if len(tokens) > 1:
                # type is all but last token
                result.append(" ".join(tokens[:-1]))
            else:
                result.append(p)
        return result
    return types(a) != types(b)


def check_doc_code_alignment(graph_dir: str,
                              source_root: str = "",
                              node_filter: Optional[List[str]] = None) -> Dict:
    """Check all nodes (or filtered set) for doc-code mismatches.

    Returns dict with:
      - mismatches: list of DocCodeMismatch dicts
      - checked_count: number of nodes checked
      - mismatched_count: number of nodes with at least one mismatch
      - by_kind: counts per mismatch kind
    """
    from _builder.graph_build import _load_full_graph
    try:
        G = _load_full_graph(graph_dir)
    except Exception as exc:
        return {"error": f"Cannot load graph: {exc}"}

    all_mismatches: List[DocCodeMismatch] = []
    checked = 0
    mismatched_nodes: Set[str] = set()

    for nid, nd in G.nodes(data=True):
        if nd.get("is_empty", False) or nd.get("node_type") == "file":
            continue
        if node_filter and nid not in node_filter and nd.get("name", "") not in node_filter:
            continue
        # Skip nodes without any doc fields
        has_doc = any(nd.get(k) for k in ("semantic_desc", "external_desc",
                                          "api_constraints", "doc_comment"))
        if not has_doc:
            continue
        checked += 1
        node_mismatches: List[DocCodeMismatch] = []
        node_mismatches.extend(_check_return_value_mismatch(nid, nd))
        node_mismatches.extend(_check_param_mismatch(nid, nd))
        node_mismatches.extend(_check_stale_doc(nid, nd))
        node_mismatches.extend(_check_signature_change(nid, nd))
        if node_mismatches:
            mismatched_nodes.add(nid)
            all_mismatches.extend(node_mismatches)

    by_kind: Dict[str, int] = {}
    for m in all_mismatches:
        by_kind[m.kind] = by_kind.get(m.kind, 0) + 1

    return {
        "mismatches": [m.to_dict() for m in all_mismatches],
        "checked_count": checked,
        "mismatched_count": len(mismatched_nodes),
        "mismatched_nodes": sorted(mismatched_nodes),
        "by_kind": by_kind,
    }


# ---------------------------------------------------------------------------
# Mark doc stale (used when code changes are detected)
# ---------------------------------------------------------------------------

def mark_doc_stale(graph_dir: str, node_id: str, reason: str) -> bool:
    """Mark a node's doc as stale.

    Writes 'doc_stale', 'doc_stale_reason', 'doc_stale_at' into the node's
    extra_json. Supports both backends:
      - JSON backend: domain-split files referenced by code2database_master.json
      - SQLite backend: code2database.db functions.extra_json column

    Returns True if the node was found and updated.
    """
    import time
    timestamp = str(int(time.time()))

    # Try SQLite backend first (code2database.db)
    db_path = os.path.join(graph_dir, "code2database.db")
    if os.path.exists(db_path) and not os.path.exists(
            os.path.join(graph_dir, "code2database_master.json")):
        return _mark_doc_stale_sqlite(db_path, node_id, reason, timestamp)

    # JSON backend: load full graph, find node, write back via split_by_domain
    master_path = os.path.join(graph_dir, "code2database_master.json")
    if not os.path.exists(master_path):
        return False
    try:
        from _builder.graph_build import _load_full_graph, split_by_domain
        G = _load_full_graph(graph_dir)
        if node_id not in G:
            return False
        ndata = G.nodes[node_id]
        ndata["doc_stale"] = True
        ndata["doc_stale_reason"] = reason
        ndata["doc_stale_at"] = timestamp
        # Persist back via split_by_domain (rewrites domain files)
        # Use the existing source_root from master
        master = json.loads(Path(master_path).read_text(encoding="utf-8"))
        source_root = master.get("source_root", "")
        split_by_domain(G, graph_dir, source_root,
                        build_config=master.get("build_config"))
        return True
    except Exception as exc:
        # Fallback: try direct domain file edit
        return _mark_doc_stale_json_fallback(graph_dir, node_id, reason, timestamp)


def _mark_doc_stale_sqlite(db_path: str, node_id: str, reason: str,
                            timestamp: str) -> bool:
    """SQLite backend: update extra_json directly."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT extra_json FROM functions WHERE id=?", (node_id,)).fetchone()
        if not row:
            return False
        extra = {}
        if row[0]:
            try:
                extra = json.loads(row[0])
            except Exception:
                extra = {}
        extra["doc_stale"] = True
        extra["doc_stale_reason"] = reason
        extra["doc_stale_at"] = timestamp
        conn.execute(
            "UPDATE functions SET extra_json=? WHERE id=?",
            (json.dumps(extra, ensure_ascii=False), node_id))
        conn.commit()
        return True
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        return False
    finally:
        conn.close()


def _mark_doc_stale_json_fallback(graph_dir: str, node_id: str, reason: str,
                                   timestamp: str) -> bool:
    """Fallback: edit domain JSON files directly to add doc_stale flag."""
    master_path = os.path.join(graph_dir, "code2database_master.json")
    try:
        master = json.loads(Path(master_path).read_text(encoding="utf-8"))
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        return False
    domains = master.get("domains", {}) or {}
    for domain, filename in domains.items():
        domain_path = os.path.join(graph_dir, filename)
        if not os.path.exists(domain_path):
            continue
        try:
            data = json.loads(Path(domain_path).read_text(encoding="utf-8"))
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue
        updated = False
        # Legacy format with nodes list
        if "nodes" in data:
            for n in data["nodes"]:
                if n.get("id") == node_id:
                    n["doc_stale"] = True
                    n["doc_stale_reason"] = reason
                    n["doc_stale_at"] = timestamp
                    updated = True
                    break
        # Compact format with function_details
        elif "function_details" in data:
            details = data["function_details"]
            if node_id in details:
                details[node_id]["doc_stale"] = True
                details[node_id]["doc_stale_reason"] = reason
                details[node_id]["doc_stale_at"] = timestamp
                updated = True
        if updated:
            Path(domain_path).write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")
            return True
    return False


def detect_signature_changes(old_graph_dir: str, new_graph_dir: str) -> List[Dict]:
    """Compare two graph versions to detect signature changes.

    Returns list of {node_id, old_sig, new_sig} dicts for nodes whose
    signature changed between the two versions.
    """
    from _builder.graph_build import _load_full_graph
    try:
        G_old = _load_full_graph(old_graph_dir)
        G_new = _load_full_graph(new_graph_dir)
    except Exception as exc:
        return [{"error": f"Cannot load graphs: {exc}"}]

    changes = []
    for nid, new_nd in G_new.nodes(data=True):
        if nid not in G_old:
            continue
        old_nd = G_old.nodes[nid]
        old_sig = old_nd.get("signature", "") or ""
        new_sig = new_nd.get("signature", "") or ""
        if old_sig and new_sig and old_sig != new_sig:
            changes.append({
                "node_id": nid,
                "name": new_nd.get("name", ""),
                "old_signature": old_sig,
                "new_signature": new_sig,
            })
    return changes


# ---------------------------------------------------------------------------
# Full alignment report (Markdown)
# ---------------------------------------------------------------------------

def generate_alignment_report(graph_dir: str, source_root: str = "") -> str:
    """Generate a Markdown report of doc-code alignment issues."""
    result = check_doc_code_alignment(graph_dir, source_root)
    if "error" in result:
        return f"# Doc-Code Alignment Report\n\n**Error**: {result['error']}\n"

    lines = ["# Doc-Code Alignment Report", ""]
    lines.append(f"- Checked nodes: **{result['checked_count']}**")
    lines.append(f"- Nodes with mismatches: **{result['mismatched_count']}**")
    lines.append(f"- Total mismatches: **{len(result['mismatches'])}**")
    lines.append("")
    if result["by_kind"]:
        lines.append("## Mismatches by kind")
        lines.append("")
        for kind, count in sorted(result["by_kind"].items(),
                                   key=lambda x: -x[1]):
            lines.append(f"- **{kind}**: {count}")
        lines.append("")

    if result["mismatches"]:
        lines.append("## Detailed mismatches")
        lines.append("")
        # Group by node
        by_node: Dict[str, List[Dict]] = {}
        for m in result["mismatches"]:
            by_node.setdefault(m["node_id"], []).append(m)
        for nid in sorted(by_node.keys()):
            lines.append(f"### `{nid}`")
            lines.append("")
            for m in by_node[nid]:
                lines.append(f"- **{m['kind']}** ({m['severity']}): "
                             f"doc says *{m['doc_claim']}*, code: *{m['code_fact']}*")
                if m["detail"]:
                    lines.append(f"  - {m['detail']}")
            lines.append("")
    else:
        lines.append("**No mismatches detected.** All checked nodes' docs align with code.")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_doc_code_check(args):
    """Check doc-code alignment for all nodes.

    Usage: doc-code-check --graph <dir> [--node <id>...] [--json]
    """
    graph_dir = args.graph
    node_filter = getattr(args, "node", None) or None
    result = check_doc_code_alignment(graph_dir, node_filter=node_filter)
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Checked {result.get('checked_count', 0)} nodes")
        print(f"Found {len(result.get('mismatches', []))} mismatches "
              f"across {result.get('mismatched_count', 0)} nodes")
        if result.get("by_kind"):
            print("By kind:")
            for k, v in sorted(result["by_kind"].items(), key=lambda x: -x[1]):
                print(f"  {k}: {v}")
        if result.get("mismatches"):
            print("\nMismatches:")
            for m in result["mismatches"][:20]:
                print(f"  [{m['kind']}] {m['node_id']}: {m['detail'] or m['doc_claim']}")
            if len(result["mismatches"]) > 20:
                print(f"  ... and {len(result['mismatches']) - 20} more")


def cmd_doc_mark_stale(args):
    """Mark a node's doc as stale.

    Usage: doc-mark-stale --graph <dir> --node <id> --reason <text>
    """
    graph_dir = args.graph
    node_id = args.node
    reason = args.reason
    ok = mark_doc_stale(graph_dir, node_id, reason)
    if ok:
        print(json.dumps({
            "ok": True,
            "node_id": node_id,
            "reason": reason,
            "message": f"marked doc as stale for {node_id}",
        }, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({
            "ok": False,
            "node_id": node_id,
            "error": "node not found or master.json missing",
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)


def cmd_doc_alignment_report(args):
    """Generate full doc-code alignment report (Markdown).

    Usage: doc-alignment-report --graph <dir> [--source <root>] [-o <path>]
    """
    graph_dir = args.graph
    source_root = getattr(args, "source", "") or ""
    report = generate_alignment_report(graph_dir, source_root)
    out_path = getattr(args, "output", "") or ""
    if out_path:
        Path(out_path).write_text(report, encoding="utf-8")
        print(f"Report written to {out_path}", file=sys.stderr)
    else:
        print(report)


def cmd_doc_signature_diff(args):
    """Detect signature changes between two graph versions.

    Usage: doc-signature-diff --old-graph <dir> --new-graph <dir>
    """
    old_dir = args.old_graph
    new_dir = args.new_graph
    changes = detect_signature_changes(old_dir, new_dir)
    print(json.dumps({
        "change_count": len(changes),
        "changes": changes,
    }, ensure_ascii=False, indent=2, default=str))
