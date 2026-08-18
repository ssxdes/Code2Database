"""Graph history versioning (D9).

Tracks logical graph versions in a `graph_versions` SQLite table and provides:
- `record_version`: capture a version snapshot after a build/update
- `graph_history`: list versions for a node or the whole graph
- `graph_diff`: compute node/edge added/removed/changed between two versions

Each version is identified by an integer `version_id` and stores:
- timestamp, description, commit_hash (if known)
- node_count, edge_count (summary stats)
- snapshot_id (optional link to a transactions.py snapshot dir)

Diff is computed by comparing the JSON state at two version snapshot dirs,
or by comparing two graph_dir states directly.
"""
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Try sqlite_store for connection helper
try:
    from _builder.sqlite_store import SQLiteStore
except ImportError:
    SQLiteStore = None  # type: ignore


HISTORY_DB_NAME = "graph_versions.db"
HISTORY_TABLE = "graph_versions"


def _history_db_path(graph_dir: str) -> str:
    return os.path.join(graph_dir, HISTORY_DB_NAME)


def _ensure_history_db(graph_dir: str):
    """Create the graph_versions table if it doesn't exist."""
    import sqlite3
    db_path = _history_db_path(graph_dir)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                description TEXT,
                commit_hash TEXT,
                commit_short TEXT,
                node_count INTEGER DEFAULT 0,
                edge_count INTEGER DEFAULT 0,
                snapshot_id TEXT,
                snapshot_path TEXT,
                operator TEXT,
                meta TEXT
            )
        """)
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{HISTORY_TABLE}_ts "
            f"ON {HISTORY_TABLE}(timestamp)"
        )
        conn.commit()
    finally:
        conn.close()


def record_version(graph_dir: str,
                   description: str = "",
                   commit_hash: Optional[str] = None,
                   commit_short: Optional[str] = None,
                   operator: Optional[str] = None,
                   snapshot_id: Optional[str] = None,
                   snapshot_path: Optional[str] = None,
                   node_count: Optional[int] = None,
                   edge_count: Optional[int] = None,
                   meta: Optional[Dict[str, Any]] = None) -> int:
    """Record a new graph version. Returns the new version_id."""
    import sqlite3
    _ensure_history_db(graph_dir)

    if node_count is None or edge_count is None:
        nc, ec = _count_graph(graph_dir)
        if node_count is None:
            node_count = nc
        if edge_count is None:
            edge_count = ec

    db_path = _history_db_path(graph_dir)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            f"""INSERT INTO {HISTORY_TABLE}
               (timestamp, description, commit_hash, commit_short,
                node_count, edge_count, snapshot_id, snapshot_path,
                operator, meta)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.strftime("%Y-%m-%dT%H:%M:%S"),
             description, commit_hash or "", commit_short or "",
             node_count, edge_count, snapshot_id or "", snapshot_path or "",
             operator or "", json.dumps(meta or {}, ensure_ascii=False))
        )
        conn.commit()
        return cur.lastrowid or 0
    finally:
        conn.close()


def _count_graph(graph_dir: str) -> Tuple[int, int]:
    """Count nodes and edges in the current graph.

    Returns (0, 0) if the graph dir has no master file and no SQLite db
    (e.g., when only snapshot subdirs exist); avoids raising/sys.exit.
    """
    master_path = os.path.join(graph_dir, "code2database_master.json")
    db_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.exists(master_path) and not os.path.exists(db_path):
        return 0, 0
    try:
        from _builder.graph_build import _load_full_graph
        G = _load_full_graph(graph_dir)
        return G.number_of_nodes(), G.number_of_edges()
    except Exception:
        return 0, 0


def list_versions(graph_dir: str, limit: int = 50) -> List[Dict[str, Any]]:
    """List graph versions, newest first."""
    import sqlite3
    db_path = _history_db_path(graph_dir)
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""SELECT version_id, timestamp, description, commit_hash,
                      commit_short, node_count, edge_count, snapshot_id,
                      operator
               FROM {HISTORY_TABLE}
               ORDER BY version_id DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_version(graph_dir: str, version_id: int) -> Optional[Dict[str, Any]]:
    """Get a single version by id."""
    import sqlite3
    db_path = _history_db_path(graph_dir)
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            f"""SELECT * FROM {HISTORY_TABLE} WHERE version_id = ?""",
            (version_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def node_history(graph_dir: str, node_id: str,
                 limit: int = 50) -> List[Dict[str, Any]]:
    """Get history of a specific node across versions.

    For each version that has a snapshot, check if the node existed and
    capture its key attributes. Returns list of changesets.
    """
    import sqlite3
    db_path = _history_db_path(graph_dir)
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""SELECT version_id, timestamp, description, commit_hash,
                      snapshot_path, node_count, edge_count
               FROM {HISTORY_TABLE}
               WHERE snapshot_path IS NOT NULL AND snapshot_path != ''
               ORDER BY version_id ASC LIMIT ?""",
            (limit,)
        ).fetchall()
    finally:
        conn.close()

    history = []
    for row in rows:
        snap_path = row["snapshot_path"]
        if not snap_path or not os.path.isdir(snap_path):
            continue
        node_data = _load_node_from_snapshot(snap_path, node_id)
        if node_data is None:
            history.append({
                "version_id": row["version_id"],
                "timestamp": row["timestamp"],
                "description": row["description"],
                "commit_hash": row["commit_hash"],
                "status": "absent",
            })
        else:
            history.append({
                "version_id": row["version_id"],
                "timestamp": row["timestamp"],
                "description": row["description"],
                "commit_hash": row["commit_hash"],
                "status": "present",
                "node": node_data,
            })
    return history


def _load_node_from_snapshot(snap_path: str,
                             node_id: str) -> Optional[Dict[str, Any]]:
    """Load a single node's data from a snapshot directory."""
    master_path = os.path.join(snap_path, "code2database_master.json")
    if os.path.exists(master_path):
        try:
            with open(master_path, encoding="utf-8") as f:
                master = json.load(f)
            for domain, fname in master.get("domains", {}).items():
                dom_path = os.path.join(snap_path, fname)
                if not os.path.exists(dom_path):
                    continue
                with open(dom_path, encoding="utf-8") as df:
                    dom = json.load(df)
                for n in dom.get("nodes", []):
                    if n.get("id") == node_id:
                        return n
        except (json.JSONDecodeError, OSError):
            pass
    # Try SQLite snapshot
    db_path = os.path.join(snap_path, "code2database.db")
    if os.path.exists(db_path):
        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT * FROM functions WHERE id = ?", (node_id,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    return None


def _load_nodes_from_dir(graph_dir: str) -> Dict[str, Dict[str, Any]]:
    """Load all nodes from a graph dir or snapshot dir, keyed by id."""
    nodes: Dict[str, Dict[str, Any]] = {}
    master_path = os.path.join(graph_dir, "code2database_master.json")
    if os.path.exists(master_path):
        try:
            with open(master_path, encoding="utf-8") as f:
                master = json.load(f)
            for domain, fname in master.get("domains", {}).items():
                dom_path = os.path.join(graph_dir, fname)
                if not os.path.exists(dom_path):
                    continue
                with open(dom_path, encoding="utf-8") as df:
                    dom = json.load(df)
                for n in dom.get("nodes", []):
                    nid = n.get("id", "")
                    if nid:
                        nodes[nid] = n
        except (json.JSONDecodeError, OSError):
            pass
    db_path = os.path.join(graph_dir, "code2database.db")
    if os.path.exists(db_path):
        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute("SELECT * FROM functions").fetchall()
                for r in rows:
                    nid = r["id"] if "id" in r.keys() else None
                    if nid:
                        nodes[nid] = dict(r)
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    return nodes


def _load_edges_from_dir(graph_dir: str) -> List[Dict[str, Any]]:
    """Load all edges from a graph dir or snapshot dir."""
    edges: List[Dict[str, Any]] = []
    master_path = os.path.join(graph_dir, "code2database_master.json")
    if os.path.exists(master_path):
        try:
            with open(master_path, encoding="utf-8") as f:
                master = json.load(f)
            for domain, fname in master.get("domains", {}).items():
                dom_path = os.path.join(graph_dir, fname)
                if not os.path.exists(dom_path):
                    continue
                with open(dom_path, encoding="utf-8") as df:
                    dom = json.load(df)
                for e in dom.get("edges", []):
                    edges.append(e)
        except (json.JSONDecodeError, OSError):
            pass
    db_path = os.path.join(graph_dir, "code2database.db")
    if os.path.exists(db_path):
        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute("SELECT * FROM edges").fetchall()
                for r in rows:
                    edges.append(dict(r))
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    return edges


def _edge_key(edge: Dict[str, Any]) -> Tuple[str, str, str]:
    """Build a comparable key for an edge."""
    src = edge.get("source") or edge.get("invoker_id") or edge.get("caller") or ""
    dst = edge.get("target") or edge.get("invoked_id") or edge.get("callee") or ""
    rel = edge.get("relation") or edge.get("concurrency") or "INVOKES"
    return (str(src), str(dst), str(rel))


def graph_diff(graph_dir: str,
               from_version: Optional[int] = None,
               to_version: Optional[int] = None,
               from_path: Optional[str] = None,
               to_path: Optional[str] = None) -> Dict[str, Any]:
    """Compute the diff between two graph versions.

    Either specify version_ids (looked up in graph_versions table to find
    snapshot paths), or specify explicit paths.

    Returns a dict with:
    - added_nodes, removed_nodes, changed_nodes (with attr diffs)
    - added_edges, removed_edges
    - summary counts
    """
    if from_path is None:
        from_path = _resolve_version_path(graph_dir, from_version)
    if to_path is None:
        to_path = _resolve_version_path(graph_dir, to_version)
    if from_path is None or to_path is None:
        raise ValueError(
            f"Cannot resolve version paths (from={from_version}, to={to_version}). "
            "Specify explicit paths or record versions with snapshot_path."
        )

    from_nodes = _load_nodes_from_dir(from_path)
    to_nodes = _load_nodes_from_dir(to_path)
    from_edges = {ke: e for e in _load_edges_from_dir(from_path)
                  for ke in [_edge_key(e)]}
    to_edges = {ke: e for e in _load_edges_from_dir(to_path)
                for ke in [_edge_key(e)]}

    added_nodes = []
    removed_nodes = []
    changed_nodes = []
    for nid, n in to_nodes.items():
        if nid not in from_nodes:
            added_nodes.append(n)
        else:
            old = from_nodes[nid]
            diffs = _attr_diff(old, n)
            if diffs:
                changed_nodes.append({"id": nid, "name": n.get("name", ""),
                                      "changes": diffs})
    for nid, n in from_nodes.items():
        if nid not in to_nodes:
            removed_nodes.append(n)

    added_edges = [e for k, e in to_edges.items() if k not in from_edges]
    removed_edges = [e for k, e in from_edges.items() if k not in to_edges]

    return {
        "from_path": from_path,
        "to_path": to_path,
        "summary": {
            "added_nodes": len(added_nodes),
            "removed_nodes": len(removed_nodes),
            "changed_nodes": len(changed_nodes),
            "added_edges": len(added_edges),
            "removed_edges": len(removed_edges),
        },
        "added_nodes": added_nodes,
        "removed_nodes": removed_nodes,
        "changed_nodes": changed_nodes,
        "added_edges": added_edges,
        "removed_edges": removed_edges,
    }


def _resolve_version_path(graph_dir: str,
                          version_id: Optional[int]) -> Optional[str]:
    """Resolve a version_id to a snapshot directory path."""
    if version_id is None:
        return graph_dir  # current state
    v = get_version(graph_dir, version_id)
    if not v:
        return None
    snap = v.get("snapshot_path", "")
    return snap if snap else None


def _attr_diff(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Compute attribute-level diff between two node dicts."""
    diffs: Dict[str, Any] = {}
    keys = set(old.keys()) | set(new.keys())
    skip = {"id"}  # don't report id changes
    for k in keys:
        if k in skip:
            continue
        ov = old.get(k)
        nv = new.get(k)
        if ov != nv:
            diffs[k] = {"from": ov, "to": nv}
    return diffs


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_graph_history(args):
    """List graph versions or show history of a specific node."""
    graph_dir = args.graph
    if getattr(args, "node", None):
        history = node_history(graph_dir, args.node, limit=args.limit or 50)
        print(json.dumps(history, ensure_ascii=False, indent=2, default=str))
        return
    versions = list_versions(graph_dir, limit=args.limit or 50)
    if not versions:
        print("No graph versions recorded.")
        return
    print(f"Found {len(versions)} versions (newest first):")
    for v in versions:
        print(f"  v{v['version_id']} [{v['timestamp']}] "
              f"nodes={v['node_count']} edges={v['edge_count']} "
              f"commit={v.get('commit_short', '') or '-'} "
              f"desc={v.get('description', '') or '-'}")


def cmd_graph_diff(args):
    """Diff two graph versions."""
    graph_dir = args.graph
    result = graph_diff(
        graph_dir,
        from_version=getattr(args, "from_version", None),
        to_version=getattr(args, "to_version", None),
        from_path=getattr(args, "from_path", None),
        to_path=getattr(args, "to_path", None),
    )
    if getattr(args, "summary_only", False):
        print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
        return
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def cmd_graph_record_version(args):
    """Manually record a graph version."""
    vid = record_version(
        args.graph,
        description=getattr(args, "description", "") or "",
        commit_hash=getattr(args, "commit_hash", None),
        commit_short=getattr(args, "commit_short", None),
        operator=getattr(args, "operator", None),
    )
    print(f"Recorded version v{vid}")
