"""Export call chains as Mermaid flowchart diagrams.

Generates Mermaid markdown diagrams from the code graph for use in
documentation, README files, and design docs.

Supported export types:
  - Call chain (A → B → C with conditions)
  - Domain architecture (module-level dependency graph)
  - Critical paths (top-N key execution paths)
  - Function detail (one function + its callers/callees)

Output: markdown with embedded Mermaid code blocks.
"""
from __future__ import annotations

import os
from typing import Dict, List, Any, Optional
import logging


def export_mermaid(
    graph_dir: str,
    mode: str = "chain",
    node: Optional[str] = None,
    from_node: Optional[str] = None,
    to_node: Optional[str] = None,
    depth: int = 5,
    top_n: int = 10,
    output: Optional[str] = None,
) -> str:
    """Generate a Mermaid diagram from the code graph.

    Args:
        graph_dir: Path to the graph directory.
        mode: One of 'chain', 'domain', 'paths', 'function'.
        node: Function name or ID (for 'chain' and 'function' modes).
        from_node/to_node: Start/end for path tracing (for 'chain' mode).
        depth: Maximum traversal depth.
        top_n: Number of paths to include (for 'paths' mode).
        output: Output file path. If None, prints to stdout.

    Returns:
        The Mermaid markdown string.
    """
    from _builder.cgdb_suggest import _load_functions, _load_edges
    functions = _load_functions(graph_dir)
    edges = _load_edges(graph_dir)

    if mode == "domain":
        md = _export_domain_diagram(functions, edges)
    elif mode == "paths":
        md = _export_critical_paths(functions, edges, top_n)
    elif mode == "function" and node:
        md = _export_function_detail(functions, edges, node)
    elif mode == "chain":
        if from_node and to_node:
            md = _export_call_chain(functions, edges, from_node, to_node, depth)
        elif node:
            md = _export_function_neighborhood(functions, edges, node, depth)
        else:
            return "Error: --node or --from/--to required for chain mode"
    else:
        return f"Error: unknown mode '{mode}'"

    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(md)
    return md


def _name_to_id(functions: Dict, name: str) -> Optional[str]:
    """Match function name to ID."""
    for fid, func in functions.items():
        if func.get("name", "") == name or fid == name:
            return fid
    # Fuzzy: match by last component
    for fid, func in functions.items():
        if name in func.get("name", "") or func.get("name", "") in name:
            return fid
    return None


def _short_name(full_id: str, functions: Dict) -> str:
    """Get a short display name for a function ID."""
    func = functions.get(full_id, {})
    name = func.get("name", full_id)
    return name[:40]


def _export_call_chain(
    functions: Dict, edges: List, from_name: str, to_name: str, depth: int
) -> str:
    """Export a call chain from A to B as a Mermaid flowchart."""
    from_id = _name_to_id(functions, from_name)
    to_id = _name_to_id(functions, to_name)
    if not from_id:
        return f"Error: function '{from_name}' not found"
    if not to_id:
        return f"Error: function '{to_name}' not found"

    # BFS from from_id to to_id
    adj = {}
    for e in edges:
        if e.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        adj.setdefault(e["source"], []).append(e["target"])

    visited = {from_id}
    queue = [(from_id, [from_id])]
    path = None
    while queue:
        cur, p = queue.pop(0)
        if cur == to_id:
            path = p
            break
        if len(p) >= depth:
            continue
        for nxt in adj.get(cur, []):
            if nxt not in visited:
                visited.add(nxt)
                queue.append((nxt, p + [nxt]))

    if not path:
        return f"No path found from {from_name} to {to_name}"

    lines = ["```mermaid", "graph LR"]
    for i in range(len(path) - 1):
        a = _short_name(path[i], functions)
        b = _short_name(path[i + 1], functions)
        lines.append(f'  {a} --> {b}')
    lines.append("```")
    return "\n".join(lines)


def _export_function_neighborhood(
    functions: Dict, edges: List, name: str, depth: int
) -> str:
    """Export a function's caller/callee neighborhood."""
    fid = _name_to_id(functions, name)
    if not fid:
        return f"Error: function '{name}' not found"

    callers = []
    callees = []
    for e in edges:
        if e.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        if e["target"] == fid:
            callers.append(e["source"])
        if e["source"] == fid:
            callees.append(e["target"])

    lines = ["```mermaid", "graph TD"]
    center = _short_name(fid, functions)
    # Callers above
    for c in callers[:10]:
        cn = _short_name(c, functions)
        lines.append(f'  {cn} --> {center}')
    # Callees below
    for c in callees[:10]:
        cn = _short_name(c, functions)
        lines.append(f'  {center} --> {cn}')
    lines.append("```")
    return "\n".join(lines)


def _export_domain_diagram(functions: Dict, edges: List) -> str:
    """Export domain-level architecture diagram."""
    domains = set()
    domain_edges = {}
    for fid, func in functions.items():
        if func.get("is_empty", False):
            continue
        domains.add(func.get("domain", "root"))
    for e in edges:
        if e.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        src_dom = functions.get(e["source"], {}).get("domain", "?")
        dst_dom = functions.get(e["target"], {}).get("domain", "?")
        if src_dom != dst_dom:
            key = (src_dom, dst_dom)
            domain_edges[key] = domain_edges.get(key, 0) + 1

    lines = ["```mermaid", "graph LR"]
    for (src, dst), count in sorted(domain_edges.items()):
        lines.append(f'  {src} -->|{count}| {dst}')
    lines.append("```")
    return "\n".join(lines)


def _export_critical_paths(functions: Dict, edges: List, top_n: int) -> str:
    """Export top-N critical paths as Mermaid."""
    # Find API entry points
    api_ids = [fid for fid, f in functions.items()
               if "API_entry" in f.get("labels", []) and not f.get("is_empty", False)]
    if not api_ids:
        return "No API entry points found"

    adj = {}
    for e in edges:
        if e.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        adj.setdefault(e["source"], []).append(e["target"])

    lines = ["```mermaid", "graph TD"]
    for api_id in api_ids[:top_n]:
        path = _bfs_longest_path(api_id, adj, max_depth=5)
        for i in range(len(path) - 1):
            a = _short_name(path[i], functions)
            b = _short_name(path[i + 1], functions)
            lines.append(f'  {a} --> {b}')
    lines.append("```")
    return "\n".join(lines)


def _export_function_detail(
    functions: Dict, edges: List, name: str
) -> str:
    """Export detailed view of one function with metadata."""
    fid = _name_to_id(functions, name)
    if not fid:
        return f"Error: function '{name}' not found"
    func = functions[fid]
    callers = []
    callees = []
    for e in edges:
        if e.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        if e["target"] == fid:
            callers.append(e["source"])
        if e["source"] == fid:
            callees.append(e["target"])

    lines = [f"## {func.get('name', name)}\n"]
    sig = func.get("signature", "")
    if sig:
        lines.append(f"**Signature**: `{sig}`\n")
    lines.append(f"**Domain**: `{func.get('domain', '')}`\n")
    lines.append(f"**Callers**: {len(callers)} | **Callees**: {len(callees)}\n")
    labels = func.get("labels", [])
    if labels:
        lines.append(f"**Labels**: {', '.join(labels)}\n")
    desc = func.get("semantic_desc", "")
    if desc:
        lines.append(f"\n{desc}\n")

    lines.append("\n### Call Graph\n")
    lines.append("```mermaid")
    lines.append("graph TD")
    center = _short_name(fid, functions)
    for c in callers[:8]:
        lines.append(f'  {_short_name(c, functions)} --> {center}')
    for c in callees[:8]:
        lines.append(f'  {center} --> {_short_name(c, functions)}')
    lines.append("```")
    return "\n".join(lines)


def _bfs_longest_path(start: str, adj: Dict, max_depth: int) -> List[str]:
    """Find the longest path from start (BFS, limited depth)."""
    best_path = [start]
    queue = [(start, [start])]
    while queue:
        cur, path = queue.pop(0)
        if len(path) > len(best_path):
            best_path = path
        if len(path) >= max_depth:
            continue
        for nxt in adj.get(cur, []):
            if nxt not in path:
                queue.append((nxt, path + [nxt]))
    return best_path


def export_mermaid_multi(graph_dir: str, output: str = None) -> str:
    """Multi-project dependency graph.

    Renders project-level boxes (one per top-level domain prefix) with
    edge counts between them. Used to visualize A -> B -> C dependency
    chain at the project granularity.
    """
    import sqlite3
    db_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.exists(db_path):
        return f"%% No db found at {db_path}"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Collect project-level stats + cross-project edges
    # Project = first segment of domain (e.g., 'A' from 'A.module')
    projects: Dict[str, Dict[str, Any]] = {}
    try:
        rows = conn.execute(
            "SELECT domain, COUNT(*) AS cnt FROM functions "
            "WHERE domain IS NOT NULL GROUP BY domain"
        ).fetchall()
        for r in rows:
            domain = r["domain"]
            project = domain.split(".")[0] if "." in domain else domain
            if project not in projects:
                projects[project] = {"functions": 0, "domains": set()}
            projects[project]["functions"] += r["cnt"]
            projects[project]["domains"].add(domain)
    except sqlite3.Error:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    cross_edges: Dict[str, Dict[str, int]] = {}
    try:
        rows = conn.execute(
            "SELECT f_invoker.domain AS caller_domain, "
            "       f_invoked.domain AS callee_domain, "
            "       COUNT(*) AS edge_count "
            "FROM edges e "
            "JOIN functions f_invoker ON e.invoker_id = f_invoker.id "
            "JOIN functions f_invoked ON e.invoked_id = f_invoked.id "
            "WHERE f_invoker.domain IS NOT NULL "
            "  AND f_invoked.domain IS NOT NULL "
            "GROUP BY f_invoker.domain, f_invoked.domain"
        ).fetchall()
        for r in rows:
            caller_proj = r["caller_domain"].split(".")[0] if "." in r["caller_domain"] else r["caller_domain"]
            callee_proj = r["callee_domain"].split(".")[0] if "." in r["callee_domain"] else r["callee_domain"]
            if caller_proj == callee_proj:
                continue  # skip intra-project
            if caller_proj not in cross_edges:
                cross_edges[caller_proj] = {}
            cross_edges[caller_proj][callee_proj] = (
                cross_edges[caller_proj].get(callee_proj, 0) + r["edge_count"]
            )
    except sqlite3.Error:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    conn.close()
    # Build Mermaid graph
    lines = ["graph TD"]
    # Project nodes
    for proj, stats in sorted(projects.items()):
        lines.append(
            f'    {proj}["{proj} ({stats["functions"]} funcs)"]'
        )
    # Cross-project edges
    for caller_proj, callees in sorted(cross_edges.items()):
        for callee_proj, count in sorted(callees.items(), key=lambda x: -x[1]):
            lines.append(f'    {caller_proj} -->|{count} calls| {callee_proj}')
    md = "\n".join(lines) + "\n"
    if output:
        Path(output).write_text(md, encoding="utf-8")
    return md


def cmd_export_mermaid(args):
    """CLI handler for `code2database_builder.py export-mermaid`."""
    graph_dir = args.graph
    mode = getattr(args, "mode", "chain")
    node = getattr(args, "node", None)
    from_node = getattr(args, "from", None)
    to_node = getattr(args, "to", None)
    depth = getattr(args, "depth", 5)
    top_n = getattr(args, "top", 10)
    output = getattr(args, "output", None)
    multi = getattr(args, "multi", False)

    if multi:
        md = export_mermaid_multi(graph_dir, output)
        if not output:
            print(md)
        else:
            print(f"Multi-project Mermaid diagram written to: {output}")
        return 0

    md = export_mermaid(graph_dir, mode, node, from_node, to_node,
                       depth, top_n, output)
    if not output:
        print(md)
    else:
        print(f"Mermaid diagram written to: {output}")
    return 0
