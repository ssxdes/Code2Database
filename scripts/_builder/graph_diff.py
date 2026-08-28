"""Graph diff — compare two builds and return structural differences.

Fills the graph-diff gap identified by both code-review-graph and
codecharta. Given two graph directories (before/after), returns
added/removed/changed nodes + edges + community shifts.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict


def graph_diff(before_dir: str, after_dir: str, detail: str = "summary") -> Dict[str, Any]:
    """Compare two graph builds and return structural differences.

    Args:
        before_dir: Previous build directory.
        after_dir: Current build directory.
        detail: "summary" (counts only) or "full" (lists all changes).

    Returns:
        {nodes: {added, removed, changed}, edges: {added, removed},
         communities: {added, removed, shifted}, stats}
    """
    def _load_node_ids(graph_dir: str) -> Dict[str, Dict]:
        """Load node id → {name, domain, labels, source_file, line}."""
        nodes = {}
        # Try SQLite first
        db_path = os.path.join(graph_dir, "code2database.db")
        if os.path.exists(db_path):
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT id, name, domain, source_file, line_number, labels "
                    "FROM functions"
                ).fetchall()
                for r in rows:
                    nodes[r["id"]] = {
                        "id": r["id"],
                        "name": r["name"] or "",
                        "domain": r["domain"] or "",
                        "source_file": r["source_file"] or "",
                        "line": r["line_number"] or 0,
                        "labels": r["labels"] or "",
                    }
            except Exception:
                pass
            conn.close()
            if nodes:
                return nodes
        # Fallback: load from domain JSON files
        for fname in os.listdir(graph_dir):
            if not fname.endswith("_master.json"):
                continue
            master = json.loads(Path(os.path.join(graph_dir, fname)).read_text(encoding="utf-8"))
            for domain, info in master.get("domains", {}).items():
                for func in info.get("functions", []):
                    nid = func.get("id", "")
                    if nid:
                        nodes[nid] = {
                            "id": nid, "name": func.get("name", ""),
                            "domain": func.get("domain", domain),
                            "source_file": func.get("source_file", ""),
                            "line": func.get("line", 0),
                            "labels": ",".join(func.get("labels", [])),
                        }
        return nodes

    def _load_edges(graph_dir: str) -> set:
        """Load edge set as {(source, target, relation)} tuples."""
        edges = set()
        db_path = os.path.join(graph_dir, "code2database.db")
        if os.path.exists(db_path):
            import sqlite3
            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT invoker_id, invoked_id, relation FROM edges"
                ).fetchall()
                for r in rows:
                    edges.add((r[0], r[1], r[2]))
            except Exception:
                pass
            conn.close()
            if edges:
                return edges
        return edges

    before_nodes = _load_node_ids(before_dir)
    after_nodes = _load_node_ids(after_dir)
    before_edges = _load_edges(before_dir)
    after_edges = _load_edges(after_dir)

    before_ids = set(before_nodes.keys())
    after_ids = set(after_nodes.keys())

    added_nodes = after_ids - before_ids
    removed_nodes = before_ids - after_ids
    changed_nodes = set()
    for nid in before_ids & after_ids:
        b = before_nodes[nid]
        a = after_nodes[nid]
        if b.get("name") != a.get("name") or b.get("domain") != a.get("domain") or \
           b.get("source_file") != a.get("source_file") or b.get("line") != a.get("line"):
            changed_nodes.add(nid)

    added_edges = after_edges - before_edges
    removed_edges = before_edges - after_edges

    # Community shifts
    before_domains = defaultdict(list)
    after_domains = defaultdict(list)
    for nid, nd in before_nodes.items():
        before_domains[nd.get("domain", "")].append(nid)
    for nid, nd in after_nodes.items():
        after_domains[nd.get("domain", "")].append(nid)
    before_communities = set(before_domains.keys())
    after_communities = set(after_domains.keys())
    added_communities = after_communities - before_communities
    removed_communities = before_communities - after_communities

    result = {
        "stats": {
            "before_nodes": len(before_nodes),
            "after_nodes": len(after_nodes),
            "before_edges": len(before_edges),
            "after_edges": len(after_edges),
            "added_nodes": len(added_nodes),
            "removed_nodes": len(removed_nodes),
            "changed_nodes": len(changed_nodes),
            "added_edges": len(added_edges),
            "removed_edges": len(removed_edges),
            "added_communities": len(added_communities),
            "removed_communities": len(removed_communities),
        },
    }

    if detail == "full":
        result["nodes"] = {
            "added": [{"id": nid, **after_nodes[nid]} for nid in sorted(added_nodes)],
            "removed": [{"id": nid, **before_nodes[nid]} for nid in sorted(removed_nodes)],
            "changed": [{"id": nid,
                         "before": before_nodes[nid],
                         "after": after_nodes[nid]} for nid in sorted(changed_nodes)],
        }
        result["edges"] = {
            "added": [{"source": e[0], "target": e[1], "relation": e[2]} for e in sorted(added_edges)],
            "removed": [{"source": e[0], "target": e[1], "relation": e[2]} for e in sorted(removed_edges)],
        }
        result["communities"] = {
            "added": sorted(added_communities),
            "removed": sorted(removed_communities),
        }

    return result


def cmd_graph_diff(args):
    """CLI handler: graph-diff."""
    result = graph_diff(args.before, args.after, detail=getattr(args, "detail", "summary"))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
