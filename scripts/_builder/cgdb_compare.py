"""Cross-graph comparison — diff two code graph directories.

Compares two graph directories (e.g., main branch vs feature branch)
and shows:
  - Functions only in source (new in feature branch)
  - Functions only in target (removed in feature branch)
  - Functions in both but with different signatures
  - Call edges that changed (added/removed)
  - Domain structure differences

Output: a markdown report + JSON summary.
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Any


def compare_graphs(
    source_dir: str,
    target_dir: str,
    output_json: bool = False,
) -> Dict[str, Any]:
    """Compare two graph directories and return differences.

    Args:
        source_dir: The "new" graph (e.g., feature branch).
        target_dir: The "old" graph (e.g., main branch).

    Returns:
        Dict with: only_in_source, only_in_target, signature_changed,
        edge_diff, domain_diff, summary.
    """
    source_funcs = _load_functions(source_dir)
    target_funcs = _load_functions(target_dir)
    source_edges = _load_edges(source_dir)
    target_edges = _load_edges(target_dir)

    source_names = {f.get("name", ""): fid for fid, f in source_funcs.items()}
    target_names = {f.get("name", ""): fid for fid, f in target_funcs.items()}

    # Functions only in source (new in feature branch)
    only_in_source = []
    for name, fid in source_names.items():
        if name not in target_names:
            f = source_funcs[fid]
            only_in_source.append({
                "name": name,
                "domain": f.get("domain", ""),
                "source_file": f.get("source_file", ""),
                "labels": f.get("labels", []),
            })

    # Functions only in target (removed in feature branch)
    only_in_target = []
    for name, fid in target_names.items():
        if name not in source_names:
            f = target_funcs[fid]
            only_in_target.append({
                "name": name,
                "domain": f.get("domain", ""),
                "source_file": f.get("source_file", ""),
                "labels": f.get("labels", []),
            })

    # Functions in both but signature changed
    signature_changed = []
    for name in set(source_names) & set(target_names):
        s_fid = source_names[name]
        t_fid = target_names[name]
        s_sig = source_funcs[s_fid].get("signature", "")
        t_sig = target_funcs[t_fid].get("signature", "")
        if s_sig and t_sig and s_sig != t_sig:
            signature_changed.append({
                "name": name,
                "source_signature": s_sig,
                "target_signature": t_sig,
            })

    # Edge diff (by function name pairs)
    source_edge_set = set()
    for e in source_edges:
        s_name = source_funcs.get(e.get("source", ""), {}).get("name", "")
        t_name = source_funcs.get(e.get("target", ""), {}).get("name", "")
        if s_name and t_name:
            source_edge_set.add((s_name, t_name))

    target_edge_set = set()
    for e in target_edges:
        s_name = target_funcs.get(e.get("source", ""), {}).get("name", "")
        t_name = target_funcs.get(e.get("target", ""), {}).get("name", "")
        if s_name and t_name:
            target_edge_set.add((s_name, t_name))

    edges_added = list(source_edge_set - target_edge_set)
    edges_removed = list(target_edge_set - source_edge_set)

    # Domain diff
    source_domains = {f.get("domain", "") for f in source_funcs.values() if not f.get("is_empty", False)}
    target_domains = {f.get("domain", "") for f in target_funcs.values() if not f.get("is_empty", False)}
    domains_added = source_domains - target_domains
    domains_removed = target_domains - source_domains

    return {
        "only_in_source": only_in_source,
        "only_in_target": only_in_target,
        "signature_changed": signature_changed,
        "edges_added": [{"from": s, "to": t} for s, t in sorted(edges_added)[:100]],
        "edges_removed": [{"from": s, "to": t} for s, t in sorted(edges_removed)[:100]],
        "domains_added": sorted(domains_added),
        "domains_removed": sorted(domains_removed),
        "summary": {
            "source_functions": len(source_funcs),
            "target_functions": len(target_funcs),
            "new_functions": len(only_in_source),
            "removed_functions": len(only_in_target),
            "signature_changed": len(signature_changed),
            "edges_added": len(edges_added),
            "edges_removed": len(edges_removed),
            "domains_added": len(domains_added),
            "domains_removed": len(domains_removed),
        },
    }


def _load_functions(graph_dir: str) -> Dict[str, dict]:
    """Load functions from a graph directory."""
    db_path = os.path.join(graph_dir, "code2database.db")
    if os.path.exists(db_path):
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        funcs = {}
        try:
            rows = conn.execute(
                "SELECT id, name, domain, source_file, signature, labels, extra_json FROM functions"
            ).fetchall()
            for row in rows:
                extra = {}
                if row["extra_json"]:
                    try:
                        extra = json.loads(row["extra_json"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                labels = []
                if row["labels"]:
                    try:
                        labels = json.loads(row["labels"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                funcs[row["id"]] = {
                    "name": row["name"],
                    "domain": row["domain"],
                    "source_file": row["source_file"],
                    "signature": row["signature"] or extra.get("signature", ""),
                    "labels": labels,
                    "is_empty": extra.get("is_empty", False),
                }
        except Exception:
            pass
        finally:
            conn.close()
        return funcs
    return {}


def _load_edges(graph_dir: str) -> List[dict]:
    """Load edges from a graph directory."""
    db_path = os.path.join(graph_dir, "code2database.db")
    if os.path.exists(db_path):
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        edges = []
        try:
            rows = conn.execute(
                "SELECT invoker_id, invoked_id, relation FROM edges"
            ).fetchall()
            for row in rows:
                edges.append({
                    "source": row["invoker_id"],
                    "target": row["invoked_id"],
                    "relation": row["relation"],
                })
        except Exception:
            pass
        finally:
            conn.close()
        return edges
    return []


def cmd_cgdb_compare(args):
    """CLI handler for `code2database_builder.py cgdb-compare`."""
    source = args.source_graph
    target = args.graph
    result = compare_graphs(source, target)
    s = result["summary"]

    print(f"Graph comparison: {source} → {target}\n")
    print(f"Source: {s['source_functions']} functions")
    print(f"Target: {s['target_functions']} functions\n")

    if s["new_functions"] > 0:
        print(f"New functions (only in source): {s['new_functions']}")
        for f in result["only_in_source"][:10]:
            print(f"  + {f['name']} ({f.get('domain', '')})")
        if s["new_functions"] > 10:
            print(f"  ... and {s['new_functions'] - 10} more")
        print()

    if s["removed_functions"] > 0:
        print(f"Removed functions (only in target): {s['removed_functions']}")
        for f in result["only_in_target"][:10]:
            print(f"  - {f['name']} ({f.get('domain', '')})")
        if s["removed_functions"] > 10:
            print(f"  ... and {s['removed_functions'] - 10} more")
        print()

    if s["signature_changed"] > 0:
        print(f"Signature changed: {s['signature_changed']}")
        for f in result["signature_changed"][:10]:
            print(f"  ~ {f['name']}")
            print(f"      old: {f['target_signature']}")
            print(f"      new: {f['source_signature']}")
        print()

    if s["edges_added"] > 0:
        print(f"Edges added: {s['edges_added']}")
        for e in result["edges_added"][:10]:
            print(f"  + {e['from']} → {e['to']}")
        print()

    if s["edges_removed"] > 0:
        print(f"Edges removed: {s['edges_removed']}")
        for e in result["edges_removed"][:10]:
            print(f"  - {e['from']} → {e['to']}")
        print()

    if result["domains_added"]:
        print(f"Domains added: {', '.join(result['domains_added'])}")
    if result["domains_removed"]:
        print(f"Domains removed: {', '.join(result['domains_removed'])}")

    if s["new_functions"] == 0 and s["removed_functions"] == 0 and s["signature_changed"] == 0:
        print("\n✓ Graphs are identical (no differences detected).")

    if getattr(args, "json", False):
        json_path = os.path.join(target, ".code2database_graph_diff.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\nFull diff written to: {json_path}")

    return 0
