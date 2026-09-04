#!/usr/bin/env python3
"""Federated queries across registered C2D graphs.

Direction: cross-project joint graphs (build-multi) physically merge
everything into ONE database — expensive, all-or-nothing. Federation
is the pluggable alternative: each project keeps its own graph; a
small registry (default ~/.code2database_federated.json, override
with --registry or C2D_FEDERATED_REGISTRY) maps names to graph dirs,
and the fed-* commands open every registered graph on demand and
annotate results with their source graph.

Commands (CLI):
    federate-register --name NAME --graph DIR
    federate-list
    federate-remove --name NAME
    fed-search --query Q [--top N]
    fed-neighbors --node NAME_OR_ID [--depth N]
    fed-path --from NAME_OR_ID --to NAME_OR_ID
"""

import json
import os
import sys
from typing import Optional

DEFAULT_REGISTRY = os.path.join(
    os.path.expanduser("~"), ".code2database_federated.json")


def _registry_path(registry: Optional[str] = None) -> str:
    if registry:
        return registry
    return os.environ.get("C2D_FEDERATED_REGISTRY", DEFAULT_REGISTRY)


def _load_registry(registry: Optional[str] = None) -> dict:
    path = _registry_path(registry)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_registry(data: dict, registry: Optional[str] = None) -> None:
    path = _registry_path(registry)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def federate_register(name: str, graph_dir: str,
                      registry: Optional[str] = None) -> dict:
    """Register (or replace) a named graph in the federation."""
    graph_dir = os.path.abspath(graph_dir)
    if not os.path.isdir(graph_dir):
        raise FileNotFoundError(f"graph directory not found: {graph_dir}")
    data = _load_registry(registry)
    data[name] = {"graph_dir": graph_dir}
    _save_registry(data, registry)
    return data[name]


def federate_remove(name: str, registry: Optional[str] = None) -> None:
    data = _load_registry(registry)
    if name not in data:
        raise KeyError(f"'{name}' is not registered "
                       f"(known: {sorted(data)})")
    del data[name]
    _save_registry(data, registry)


def federate_list(registry: Optional[str] = None) -> dict:
    return _load_registry(registry)


def _load_graphs(registry: Optional[str] = None):
    """Yield (name, graph_dir, G) for every registered graph that loads."""
    try:
        from _builder.graph_build import _load_full_graph
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from _builder.graph_build import _load_full_graph
    for name, entry in sorted(federate_list(registry).items()):
        graph_dir = entry.get("graph_dir", "")
        try:
            G = _load_full_graph(graph_dir)
        except Exception as exc:
            print(f"[federate] skipping {name} ({graph_dir}): {exc}",
                  file=sys.stderr)
            continue
        if G is not None:
            yield name, graph_dir, G


def _resolve(G, name_or_id: str):
    """Node id for a name-or-id: exact id first, then unique name."""
    if name_or_id in G:
        return name_or_id
    matches = [nid for nid, nd in G.nodes(data=True)
               if nd.get("name", "") == name_or_id
               and not nd.get("is_empty", False)
               and nd.get("node_type") != "file"]
    if len(matches) == 1:
        return matches[0]
    return None


def fed_search(query: str, top: int = 20,
               registry: Optional[str] = None) -> list:
    """Search node names across every registered graph.

    Results are annotated with their source graph; same-named symbols
    in different projects all appear (the disambiguation single-graph
    search cannot give)."""
    results = []
    q = query.lower()
    for name, graph_dir, G in _load_graphs(registry):
        for nid, nd in G.nodes(data=True):
            if nd.get("is_empty", False) or nd.get("node_type") == "file":
                continue
            node_name = nd.get("name", "")
            if not node_name or q not in node_name.lower():
                continue
            results.append({
                "graph": name,
                "graph_dir": graph_dir,
                "id": nid,
                "name": node_name,
                "domain": nd.get("domain", ""),
                "source_file": nd.get("source_file", ""),
                "line": nd.get("line", 0),
            })
            if len(results) >= top:
                return results
    return results


def fed_neighbors(name_or_id: str, depth: int = 1, max_nodes: int = 100,
                  resolve_by_name: bool = False,
                  registry: Optional[str] = None) -> list:
    """Neighbors of a node in EVERY graph that contains it.

    Returns one entry per containing graph: {graph, nodes, edges}.
    With resolve_by_name, a bare function name resolves in each graph
    independently (the same symbol is a different node per project)."""
    out = []
    for gname, graph_dir, G in _load_graphs(registry):
        nid = name_or_id if name_or_id in G else (
            _resolve(G, name_or_id) if resolve_by_name else None)
        if nid is None:
            continue
        visited = {nid}
        frontier = [nid]
        nodes = [{"id": nid, "name": G.nodes[nid].get("name", nid)}]
        edges = []
        for _ in range(depth):
            nxt = []
            for cur in frontier:
                for succ in G.successors(cur):
                    ed = G.get_edge_data(cur, succ) or {}
                    if ed.get("relation") == "CONTAINS":
                        continue
                    edges.append({"source": cur, "target": succ,
                                  "relation": ed.get("relation", "INVOKES")})
                    if succ not in visited:
                        visited.add(succ)
                        nxt.append(succ)
                        if len(nodes) < max_nodes:
                            nodes.append({"id": succ,
                                          "name": G.nodes[succ].get(
                                              "name", succ)})
                for pred in G.predecessors(cur):
                    ed = G.get_edge_data(pred, cur) or {}
                    if ed.get("relation") == "CONTAINS":
                        continue
                    edges.append({"source": pred, "target": cur,
                                  "relation": ed.get("relation", "INVOKES")})
                    if pred not in visited:
                        visited.add(pred)
                        nxt.append(pred)
                        if len(nodes) < max_nodes:
                            nodes.append({"id": pred,
                                          "name": G.nodes[pred].get(
                                              "name", pred)})
            frontier = nxt
        out.append({"graph": gname, "graph_dir": graph_dir,
                    "focus": nid, "nodes": nodes, "edges": edges})
    return out


def fed_path(from_name: str, to_name: str, resolve_by_name: bool = False,
             registry: Optional[str] = None) -> list:
    """Shortest call path between two nodes, per graph containing both.

    Returns [{graph, path: [node ids]}] — only graphs where BOTH
    endpoints resolve."""
    from collections import deque
    out = []
    for gname, graph_dir, G in _load_graphs(registry):
        def _endpoints():
            a = from_name if from_name in G else (
                _resolve(G, from_name) if resolve_by_name else None)
            b = to_name if to_name in G else (
                _resolve(G, to_name) if resolve_by_name else None)
            return a, b

        a, b = _endpoints()
        if a is None or b is None:
            continue
        # BFS over call edges only
        visited = {a}
        prev = {a: None}
        queue = deque([a])
        found = False
        while queue and not found:
            cur = queue.popleft()
            for succ in G.successors(cur):
                ed = G.get_edge_data(cur, succ) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                if succ in visited:
                    continue
                visited.add(succ)
                prev[succ] = cur
                if succ == b:
                    found = True
                    break
                queue.append(succ)
        if not found:
            continue
        path = []
        cur = b
        while cur is not None:
            path.append(cur)
            cur = prev.get(cur)
        path.reverse()
        out.append({"graph": gname, "graph_dir": graph_dir, "path": path})
    return out


# ---------------------------------------------------------------------------
# CLI handlers
# ---------------------------------------------------------------------------

def cmd_federate_register(args):
    entry = federate_register(args.name, args.graph,
                              registry=getattr(args, "registry", None))
    print(f"Registered '{args.name}' → {entry['graph_dir']}")


def cmd_federate_list(args):
    data = federate_list(registry=getattr(args, "registry", None))
    if not data:
        print("No graphs registered "
              "(federate-register --name NAME --graph DIR)")
        return
    for name in sorted(data):
        print(f"{name:16} {data[name]['graph_dir']}")


def cmd_federate_remove(args):
    federate_remove(args.name, registry=getattr(args, "registry", None))
    print(f"Removed '{args.name}'")


def cmd_fed_search(args):
    results = fed_search(args.query, top=int(getattr(args, "top", "20")),
                         registry=getattr(args, "registry", None))
    if getattr(args, "json", False):
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    for r in results:
        loc = ""
        if r.get("source_file"):
            base = os.path.basename(r["source_file"])
            loc = f"  ({base}:{r.get('line', 0)})"
        print(f"[{r['graph']}] {r['name']}  ({r.get('domain', '')}){loc}")
    if not results:
        print(f"No matches for: {args.query}")


def cmd_fed_neighbors(args):
    results = fed_neighbors(
        args.node, depth=int(getattr(args, "depth", "1")),
        resolve_by_name=bool(getattr(args, "by_name", False)),
        registry=getattr(args, "registry", None))
    if getattr(args, "json", False):
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    for entry in results:
        print(f"[{entry['graph']}] focus={entry['focus']}")
        for e in entry["edges"]:
            print(f"  {e['source']} -[{e['relation']}]-> {e['target']}")
    if not results:
        print(f"Node not found in any registered graph: {args.node}")


def cmd_fed_path(args):
    results = fed_path(
        args.from_node, args.to_node,
        resolve_by_name=bool(getattr(args, "by_name", False)),
        registry=getattr(args, "registry", None))
    if getattr(args, "json", False):
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    for entry in results:
        print(f"[{entry['graph']}] " + " -> ".join(entry["path"]))
    if not results:
        print("No graph contains both endpoints (or no path).")
