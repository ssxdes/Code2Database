"""Export the code graph as PlantUML text diagrams.

PlantUML (https://plantuml.com) renders from a compact text syntax,
so the output can be embedded directly in Markdown, reviewed in a
terminal, and rendered to images by any PlantUML server or plugin
when one is available.

Supported export types:
  - Call neighborhood (a function plus its callers and callees)
  - Module dependencies (domain-level aggregation of call edges)
  - Impact rings (direct and second-order callers, colored by ring)
  - Structure (functions of one file or domain with signatures)

Output: ``@startuml`` / ``@enduml`` text block.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

_ALIAS_SAFE_RE = re.compile(r"[^A-Za-z0-9_]")

_RING_COLORS = {"focus": "#C8E6C9", "direct": "#FFCDD2", "second": "#FFE0B2"}


def _sanitize_alias(name: str, prefix: str, taken: Set[str]) -> str:
    alias = prefix + (_ALIAS_SAFE_RE.sub("_", name) or "x")
    base = alias
    i = 1
    while alias in taken:
        i += 1
        alias = f"{base}_{i}"
    taken.add(alias)
    return alias


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _resolve_node(G, node: str) -> Optional[str]:
    """Match a function name or node id to a graph node id."""
    if node in G:
        return node
    for nid in G.nodes:
        if G.nodes[nid].get("name", "") == node:
            return nid
    lowered = node.lower()
    for nid in G.nodes:
        if lowered in nid.lower():
            return nid
    return None


def _is_call_relation(relation: str) -> bool:
    return relation in ("", "INVOKES", "DISPATCH")


def _call_edges(G) -> List[Tuple[str, str]]:
    return [(u, v) for u, v, ed in G.edges(data=True)
            if _is_call_relation(ed.get("relation", ""))]


def _truncate(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def export_plantuml(
    graph_dir: str,
    mode: str = "call",
    node: Optional[str] = None,
    file: Optional[str] = None,
    domain: Optional[str] = None,
    depth: int = 2,
    max_nodes: int = 60,
    output: Optional[str] = None,
) -> str:
    """Generate a PlantUML diagram from the code graph.

    Args:
        graph_dir: Path to the graph directory.
        mode: One of 'call', 'module', 'impact', 'structure'.
        node: Function name or ID (for 'call' and 'impact' modes).
        file: Source file path substring (for 'structure' mode).
        domain: Domain name (for 'structure' mode).
        depth: Traversal depth for 'call' and 'impact'.
        max_nodes: Rendering cap on nodes.
        output: Output file path. If None, returns the text only.

    Returns:
        The PlantUML text block.
    """
    from _builder.graph.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)

    if mode == "call":
        if not node:
            raise ValueError("--node is required for call mode")
        text = _call_diagram(G, node, depth, max_nodes)
    elif mode == "module":
        text = _module_diagram(G, max_nodes)
    elif mode == "impact":
        if not node:
            raise ValueError("--node is required for impact mode")
        text = _impact_diagram(G, node, depth, max_nodes)
    elif mode == "structure":
        if not file and not domain:
            raise ValueError("--file or --domain is required for structure mode")
        text = _structure_diagram(G, file, domain, max_nodes)
    else:
        raise ValueError(f"unknown mode '{mode}'")

    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(text)
    return text


# ---------------------------------------------------------------------------
# call neighborhood
# ---------------------------------------------------------------------------

def _call_diagram(G, node: str, depth: int, max_nodes: int) -> str:
    target = _resolve_node(G, node)
    if target is None:
        raise ValueError(f"function '{node}' not found")
    lines = ["@startuml", "skinparam shadowing false",
             f"title Call neighborhood: {G.nodes[target].get('name', target)}",
             f"skinparam rectangleBorderColor #455A64"]
    taken: Set[str] = set()

    # forward callees up to depth
    forward: Dict[str, int] = {target: 0}
    queue = [target]
    while queue:
        cur = queue.pop(0)
        d = forward[cur]
        if d >= depth:
            continue
        for succ in G.successors(cur):
            ed = G.get_edge_data(cur, succ) or {}
            if not _is_call_relation(ed.get("relation", "")):
                continue
            if succ not in forward:
                forward[succ] = d + 1
                queue.append(succ)

    # direct callers for context
    callers: List[str] = []
    for pred in G.predecessors(target):
        ed = G.get_edge_data(pred, target) or {}
        if not _is_call_relation(ed.get("relation", "")):
            continue
        callers.append(pred)

    focus = {target} | set(callers)
    render_set = [n for n in forward if n != target] + [target] + callers
    # keep the traversal cap meaningful: callees first, then callers
    if len(render_set) - 1 > max_nodes:
        keep = render_set[:max_nodes + 1]
        dropped = set(render_set) - set(keep)
        forward = {n: d for n, d in forward.items() if n not in dropped}
        render_set = keep

    alias: Dict[str, str] = {}
    for nid in render_set:
        display = G.nodes[nid].get("name") or nid
        if nid == target:
            a = _sanitize_alias(display, "focus_", taken)
            lines.append(f"rectangle {_quote(display)} as {a} #C8E6C9")
        elif nid in callers:
            a = _sanitize_alias(display, "caller_", taken)
            lines.append(f"rectangle {_quote(display)} as {a}")
        else:
            a = _sanitize_alias(display, "callee_", taken)
            lines.append(f"rectangle {_quote(display)} as {a}")
        alias[nid] = a

    for u in list(forward) + callers:
        for v in G.successors(u):
            if v not in alias:
                continue
            ed = G.get_edge_data(u, v) or {}
            if not _is_call_relation(ed.get("relation", "")):
                continue
            label = ed.get("call_condition", "")
            suffix = f" : {_truncate(label, 30)}" if label else ""
            lines.append(f"{alias[u]} --> {alias[v]}{suffix}")

    lines.append("@enduml")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# module dependencies
# ---------------------------------------------------------------------------

def _module_diagram(G, max_nodes: int) -> str:
    weights: Dict[Tuple[str, str], int] = {}
    for u, v in _call_edges(G):
        du = G.nodes[u].get("domain", "") or "?"
        dv = G.nodes[v].get("domain", "") or "?"
        if du == dv:
            continue
        weights[(du, dv)] = weights.get((du, dv), 0) + 1

    lines = ["@startuml", "skinparam shadowing false",
             "title Module dependencies (call-edge aggregation)",
             "skinparam rectangleBorderColor #455A64"]
    taken: Set[str] = set()
    domains = sorted({d for pair in weights for d in pair},
                     key=lambda d: -sum(w for (a, _), w in weights.items() if a == d))
    domains = domains[:max_nodes]
    alias: Dict[str, str] = {}
    for d in domains:
        a = _sanitize_alias(d, "mod_", taken)
        lines.append(f"rectangle {_quote(d)} as {a}")
        alias[d] = a
    for (du, dv), w in sorted(weights.items(), key=lambda kv: -kv[1]):
        if du in alias and dv in alias:
            lines.append(f"{alias[du]} --> {alias[dv]} : {w}")
    lines.append("@enduml")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# impact rings
# ---------------------------------------------------------------------------

def _impact_diagram(G, node: str, depth: int, max_nodes: int) -> str:
    target = _resolve_node(G, node)
    if target is None:
        raise ValueError(f"function '{node}' not found")
    rings: Dict[str, int] = {target: 0}
    queue = [target]
    while queue:
        cur = queue.pop(0)
        d = rings[cur]
        if d >= depth:
            continue
        for pred in G.predecessors(cur):
            ed = G.get_edge_data(pred, cur) or {}
            if not _is_call_relation(ed.get("relation", "")):
                continue
            if pred not in rings:
                rings[pred] = d + 1
                queue.append(pred)

    lines = ["@startuml", "skinparam shadowing false",
             f"title Impact radius: {G.nodes[target].get('name', target)}",
             f"skinparam rectangleBorderColor #455A64"]
    taken: Set[str] = set()
    render = sorted(rings, key=lambda n: rings[n])[:max_nodes + 1]
    alias: Dict[str, str] = {}
    for nid in render:
        display = G.nodes[nid].get("name") or nid
        if nid == target:
            color = _RING_COLORS["focus"]
        elif rings[nid] == 1:
            color = _RING_COLORS["direct"]
        else:
            color = _RING_COLORS["second"]
        a = _sanitize_alias(display, f"r{rings[nid]}_", taken)
        lines.append(f"rectangle {_quote(display)} as {a} {color}")
        alias[nid] = a
    for u in render:
        for v in G.successors(u):
            if v not in alias:
                continue
            ed = G.get_edge_data(u, v) or {}
            if not _is_call_relation(ed.get("relation", "")):
                continue
            lines.append(f"{alias[u]} --> {alias[v]}")
    lines.append("@enduml")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# structure of one file / domain
# ---------------------------------------------------------------------------

def _structure_diagram(G, file: Optional[str], domain: Optional[str],
                       max_nodes: int) -> str:
    if file:
        members = [n for n in sorted(G.nodes)
                   if file in (G.nodes[n].get("source_file", "")
                               or G.nodes[n].get("file_path", ""))]
        title = f"Structure: {file}"
    else:
        members = [n for n in sorted(G.nodes)
                   if G.nodes[n].get("domain", "") == domain]
        title = f"Structure: domain {domain}"
    if not members:
        raise ValueError("no functions matched the given file/domain")

    lines = ["@startuml", "skinparam shadowing false", f"title {title}",
             "skinparam rectangleBorderColor #455A64"]
    taken: Set[str] = set()
    by_file: Dict[str, List[str]] = {}
    for nid in members[:max_nodes]:
        src = G.nodes[nid].get("source_file", "") or "(unknown)"
        by_file.setdefault(src, []).append(nid)
    for src in sorted(by_file):
        lines.append(f"package {_quote(src)} {{")
        for nid in by_file[src]:
            nd = G.nodes[nid]
            sig = nd.get("signature", "") or nd.get("name", nid)
            a = _sanitize_alias(nd.get("name", nid), "fn_", taken)
            lines.append(f"  rectangle {_quote(_truncate(sig))} as {a}")
        lines.append("}")
    lines.append("@enduml")
    return "\n".join(lines) + "\n"


def cmd_export_plantuml(args):
    """CLI handler for `code2database_builder.py export-plantuml`."""
    import sys
    try:
        text = export_plantuml(
            args.graph,
            mode=getattr(args, "mode", "call"),
            node=getattr(args, "node", None),
            file=getattr(args, "file", None),
            domain=getattr(args, "domain", None),
            depth=getattr(args, "depth", 2),
            max_nodes=getattr(args, "max_nodes", 60),
            output=getattr(args, "output", None),
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if not getattr(args, "output", None):
        print(text, end="")
