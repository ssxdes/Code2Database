#!/usr/bin/env python3
"""Cross-function data-dependency analysis.

Models data dependencies between functions through shared global variables
and struct fields. The current graph only has INVOKES edges; this adds:

1. WRITES edges: function A writes global g → (A) -[:WRITES {var: g}]-> (g)
2. READS edges:  function B reads global g → (B) -[:READS {var: g}]-> (g)
   (or in the reverse direction: (g) -[:READ_BY]-> (B))

3. Modified-then-read chain: if A writes g and B reads g, B is data-dependent
   on A through g. This is the "mod-read chain" used for hazard detection.

4. blast-radius-with-data-dep: extend blast-radius to include both call-chain
   and data-dependency impacts. Modifying a global's writer affects all
   readers, even if they don't call each other.

Engineer question this answers:
- "If I change how this global is initialized, what reads it?" → forward
  data-dep walk.
- "Is this global ever read? If not, the writer is dead code." → check
  READS edges for the global.
- "What functions depend on the order of A before B?" → mod-read chains.
"""

import json
import os
import sys
from collections import defaultdict, deque
from typing import List, Dict, Set


# ---------------------------------------------------------------------------
# Build data-dependency edges from existing globals_read/written, fields_read/written
# ---------------------------------------------------------------------------

def build_data_dep_edges(G) -> Dict:
    """Build data-dependency edges and a global/field node registry.

    Returns:
        {
            'global_nodes': {global_name: {id, type, writers: [...], readers: [...]}},
            'field_nodes': {struct_chain->field: {id, writers: [...], readers: [...]}},
            'edges': [
                {caller, callee, relation, var, access_type, line, ...},
                ...
            ],
            'mod_read_chains': [
                {writer, reader, var, chain},
                ...
            ],
        }
    """
    global_nodes: Dict[str, Dict] = {}
    field_nodes: Dict[str, Dict] = {}

    for nid, nd in G.nodes(data=True):
        if nd.get("is_empty", False) or nd.get("node_type") == "file":
            continue
        func_name = nd.get("name", "")
        # Process globals_read/written
        for gr in nd.get("globals_read", []) or []:
            gname = gr.get("name", "")
            if not gname:
                continue
            if gname not in global_nodes:
                global_nodes[gname] = {
                    "id": f"global:{gname}",
                    "name": gname,
                    "type": "global",
                    "writers": [],
                    "readers": [],
                }
            global_nodes[gname]["readers"].append({
                "function": func_name, "function_id": nid,
                "line": gr.get("line", 0),
            })
        for gw in nd.get("globals_written", []) or []:
            gname = gw.get("name", "")
            if not gname:
                continue
            if gname not in global_nodes:
                global_nodes[gname] = {
                    "id": f"global:{gname}",
                    "name": gname,
                    "type": "global",
                    "writers": [],
                    "readers": [],
                }
            global_nodes[gname]["writers"].append({
                "function": func_name, "function_id": nid,
                "line": gw.get("line", 0),
            })
        # Process fields_read/written
        for fr in nd.get("fields_read", []) or []:
            sc = fr.get("struct_chain", "")
            fn = fr.get("field_name", "")
            if not fn:
                continue
            key = f"{sc}->{fn}" if sc else fn
            if key not in field_nodes:
                field_nodes[key] = {
                    "id": f"field:{key}",
                    "name": key,
                    "type": "field",
                    "struct_chain": sc,
                    "field_name": fn,
                    "writers": [],
                    "readers": [],
                }
            field_nodes[key]["readers"].append({
                "function": func_name, "function_id": nid,
                "line": fr.get("line", 0),
            })
        for fw in nd.get("fields_written", []) or []:
            sc = fw.get("struct_chain", "")
            fn = fw.get("field_name", "")
            if not fn:
                continue
            key = f"{sc}->{fn}" if sc else fn
            if key not in field_nodes:
                field_nodes[key] = {
                    "id": f"field:{key}",
                    "name": key,
                    "type": "field",
                    "struct_chain": sc,
                    "field_name": fn,
                    "writers": [],
                    "readers": [],
                }
            field_nodes[key]["writers"].append({
                "function": func_name, "function_id": nid,
                "line": fw.get("line", 0),
            })

    # Build mod-read chains: for each global/field, every (writer, reader) pair
    mod_read_chains = []
    for gname, info in global_nodes.items():
        for w in info["writers"]:
            for r in info["readers"]:
                if w["function_id"] != r["function_id"]:
                    mod_read_chains.append({
                        "writer": w["function"], "writer_id": w["function_id"],
                        "reader": r["function"], "reader_id": r["function_id"],
                        "var": gname, "var_type": "global",
                    })
    for key, info in field_nodes.items():
        for w in info["writers"]:
            for r in info["readers"]:
                if w["function_id"] != r["function_id"]:
                    mod_read_chains.append({
                        "writer": w["function"], "writer_id": w["function_id"],
                        "reader": r["function"], "reader_id": r["function_id"],
                        "var": key, "var_type": "field",
                    })

    return {
        "global_nodes": global_nodes,
        "field_nodes": field_nodes,
        "mod_read_chains": mod_read_chains,
    }


# ---------------------------------------------------------------------------
# Forward data-dep impact: who reads what this function writes?
# ---------------------------------------------------------------------------

def forward_data_dep_impact(G, start_id: str, max_depth: int = 5) -> Dict:
    """Find all functions that read what `start_id` writes (transitively).

    Engineer question: "I'm changing how this function initializes a global
    — what other functions depend on that global's value?"
    """
    nd = G.nodes[start_id]
    # Collect globals/fields this function writes
    written_globals = set()
    written_fields = set()
    for gw in nd.get("globals_written", []) or []:
        if gw.get("name"):
            written_globals.add(gw["name"])
    for fw in nd.get("fields_written", []) or []:
        sc = fw.get("struct_chain", "")
        fn = fw.get("field_name", "")
        if fn:
            key = f"{sc}->{fn}" if sc else fn
            written_fields.add(key)

    # Find all readers — scan every node in the graph, not just call-reachable
    # successors. Data dependencies cross call-chain boundaries: function A
    # writing global g and function B reading g are data-dependent even if
    # there is no INVOKES edge between them.
    impacted: Dict[str, Set[str]] = defaultdict(set)  # function_id → reasons

    for cur_id, cur_nd in G.nodes(data=True):
        if cur_id == start_id:
            continue
        if cur_nd.get("is_empty", False) or cur_nd.get("node_type") == "file":
            continue
        for gr in cur_nd.get("globals_read", []) or []:
            if gr.get("name") in written_globals:
                impacted[cur_id].add(f"read global {gr['name']}")
        for fr in cur_nd.get("fields_read", []) or []:
            sc = fr.get("struct_chain", "")
            fn = fr.get("field_name", "")
            if fn:
                key = f"{sc}->{fn}" if sc else fn
                if key in written_fields:
                    impacted[cur_id].add(f"read field {key}")

    # Build result
    impacted_list = []
    for func_id, reasons in impacted.items():
        if func_id == start_id:
            continue
        nd_imp = G.nodes[func_id]
        impacted_list.append({
            "function": nd_imp.get("name", func_id),
            "function_id": func_id,
            "reasons": sorted(reasons),
        })

    return {
        "start_function": nd.get("name", start_id),
        "start_function_id": start_id,
        "writes": {
            "globals": sorted(written_globals),
            "fields": sorted(written_fields),
        },
        "impacted_readers": impacted_list,
        "total_impacted": len(impacted_list),
    }


# ---------------------------------------------------------------------------
# Reverse data-dep impact: who writes what this function reads?
# ---------------------------------------------------------------------------

def reverse_data_dep_impact(G, start_id: str, max_depth: int = 5) -> Dict:
    """Find all functions that write what `start_id` reads (transitively).

    Engineer question: "This function reads a global — where does that
    global get its value? If it's never written, that's a bug (uninitialized
    read)."
    """
    nd = G.nodes[start_id]
    read_globals = set()
    read_fields = set()
    for gr in nd.get("globals_read", []) or []:
        if gr.get("name"):
            read_globals.add(gr["name"])
    for fr in nd.get("fields_read", []) or []:
        sc = fr.get("struct_chain", "")
        fn = fr.get("field_name", "")
        if fn:
            key = f"{sc}->{fn}" if sc else fn
            read_fields.add(key)

    # Find all writers — scan every node in the graph, not just call-reachable
    # predecessors. Data dependencies cross call-chain boundaries.
    sources: Dict[str, Set[str]] = defaultdict(set)
    written_globals_found: Set[str] = set()

    for cur_id, cur_nd in G.nodes(data=True):
        if cur_id == start_id:
            continue
        if cur_nd.get("is_empty", False) or cur_nd.get("node_type") == "file":
            continue
        for gw in cur_nd.get("globals_written", []) or []:
            if gw.get("name") in read_globals:
                sources[cur_id].add(f"writes global {gw['name']}")
                written_globals_found.add(gw["name"])
        for fw in cur_nd.get("fields_written", []) or []:
            sc = fw.get("struct_chain", "")
            fn = fw.get("field_name", "")
            if fn:
                key = f"{sc}->{fn}" if sc else fn
                if key in read_fields:
                    sources[cur_id].add(f"writes field {key}")

    sources_list = []
    for func_id, reasons in sources.items():
        if func_id == start_id:
            continue
        nd_src = G.nodes[func_id]
        sources_list.append({
            "function": nd_src.get("name", func_id),
            "function_id": func_id,
            "reasons": sorted(reasons),
        })

    return {
        "start_function": nd.get("name", start_id),
        "start_function_id": start_id,
        "reads": {
            "globals": sorted(read_globals),
            "fields": sorted(read_fields),
        },
        "writers": sources_list,
        "total_writers": len(sources_list),
        "uninitialized_reads": sorted(read_globals - written_globals_found),
    }


# ---------------------------------------------------------------------------
# Combined blast-radius: call-chain + data-dep
# ---------------------------------------------------------------------------

def blast_radius_with_data_dep(G, start_id: str, max_depth: int = 5) -> Dict:
    """Blast radius combining call-chain and data-dependency impacts.

    Replaces blast-radius's call-only traversal with a combined walk:
    - Forward callees (call chain impact)
    - Forward data-dep readers (data dependent impact)
    - Reverse callers (who calls this affects them too)
    - Reverse data-dep writers (if I change what I write, readers are impacted)
    """
    # Forward call chain
    forward_callers = set()
    queue = deque([(start_id, 0)])
    visited = {start_id}
    while queue:
        cur, d = queue.popleft()
        if d >= max_depth:
            continue
        for succ in G.successors(cur):
            ed = G.get_edge_data(cur, succ) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            if succ not in visited:
                visited.add(succ)
                forward_callers.add(succ)
                queue.append((succ, d + 1))

    # Reverse call chain
    reverse_callers = set()
    queue = deque([(start_id, 0)])
    visited = {start_id}
    while queue:
        cur, d = queue.popleft()
        if d >= max_depth:
            continue
        for pred in G.predecessors(cur):
            ed = G.get_edge_data(pred, cur) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            if pred not in visited:
                visited.add(pred)
                reverse_callers.add(pred)
                queue.append((pred, d + 1))

    # Data-dep impacts
    fwd_data = forward_data_dep_impact(G, start_id, max_depth)
    rev_data = reverse_data_dep_impact(G, start_id, max_depth)

    return {
        "start_function": G.nodes[start_id].get("name", start_id),
        "start_function_id": start_id,
        "call_chain_impact": {
            "forward_callees": sorted(forward_callers),
            "reverse_callers": sorted(reverse_callers),
        },
        "data_dep_impact": {
            "forward_readers": fwd_data["impacted_readers"],
            "reverse_writers": rev_data["writers"],
        },
        "total_impacted_functions": len(forward_callers) + len(reverse_callers) +
            len(fwd_data["impacted_readers"]) + len(rev_data["writers"]),
    }


# ---------------------------------------------------------------------------
# Dead-code detection: writer with no readers
# ---------------------------------------------------------------------------

def find_dead_writers(G) -> List[Dict]:
    """Find functions that write globals/fields that no one reads.

    Engineer question: "Is this function dead code? It writes a global
    but nothing reads it."
    """
    # Build reader sets
    global_readers: Dict[str, Set[str]] = defaultdict(set)
    field_readers: Dict[str, Set[str]] = defaultdict(set)
    for nid, nd in G.nodes(data=True):
        for gr in nd.get("globals_read", []) or []:
            if gr.get("name"):
                global_readers[gr["name"]].add(nid)
        for fr in nd.get("fields_read", []) or []:
            sc = fr.get("struct_chain", "")
            fn = fr.get("field_name", "")
            if fn:
                key = f"{sc}->{fn}" if sc else fn
                field_readers[key].add(nid)

    dead = []
    for nid, nd in G.nodes(data=True):
        if nd.get("is_empty", False):
            continue
        dead_writes = []
        for gw in nd.get("globals_written", []) or []:
            gname = gw.get("name", "")
            if gname and not global_readers.get(gname):
                dead_writes.append(f"global {gname}")
        for fw in nd.get("fields_written", []) or []:
            sc = fw.get("struct_chain", "")
            fn = fw.get("field_name", "")
            if fn:
                key = f"{sc}->{fn}" if sc else fn
                if not field_readers.get(key):
                    dead_writes.append(f"field {key}")
        if dead_writes:
            dead.append({
                "function": nd.get("name", nid),
                "function_id": nid,
                "dead_writes": dead_writes,
            })
    return dead


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def cmd_data_dep(args):
    """Query cross-function data dependencies.

    Usage:
        data-dep --graph <dir> --build                # build DATA_DEP edges
        data-dep --graph <dir> --forward --node <id>  # who reads my writes?
        data-dep --graph <dir> --reverse --node <id>  # who writes my reads?
        data-dep --graph <dir> --blast --node <id>    # combined blast radius
        data-dep --graph <dir> --dead-writers         # find dead code
    """
    import json
    import os
    graph_dir = args.graph
    try:
        from _builder.graph_build import _load_full_graph
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from _builder.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)

    if getattr(args, "build", False):
        result = build_data_dep_edges(G)
        out_path = os.path.join(graph_dir, ".code2database_data_dep.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({
                "global_count": len(result["global_nodes"]),
                "field_count": len(result["field_nodes"]),
                "mod_read_chain_count": len(result["mod_read_chains"]),
                "globals": list(result["global_nodes"].values()),
                "fields": list(result["field_nodes"].values()),
                "mod_read_chains": result["mod_read_chains"],
            }, f, ensure_ascii=False, indent=2, default=str)
        print(f"Built data-dep index: {len(result['global_nodes'])} globals, "
              f"{len(result['field_nodes'])} fields, "
              f"{len(result['mod_read_chains'])} mod-read chains → {out_path}",
              file=sys.stderr)
        return

    if getattr(args, "dead_writers", False):
        dead = find_dead_writers(G)
        print(json.dumps({"dead_writers": dead, "count": len(dead)},
                         ensure_ascii=False, indent=2, default=str))
        return

    node = getattr(args, "node", "")
    if not node:
        print("Specify --build, --dead-writers, or --node with --forward/--reverse/--blast",
              file=sys.stderr)
        sys.exit(1)

    from _builder.utils import _find_node_id
    node_id = _find_node_id(G, node)
    if not node_id:
        print(f"Node not found: {node}", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "forward", False):
        result = forward_data_dep_impact(G, node_id)
    elif getattr(args, "reverse", False):
        result = reverse_data_dep_impact(G, node_id)
    elif getattr(args, "blast", False):
        result = blast_radius_with_data_dep(G, node_id)
    else:
        print("Specify --forward, --reverse, --blast, --build, or --dead-writers",
              file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
