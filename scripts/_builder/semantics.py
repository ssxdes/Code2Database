"""callgraph builder module: semantics."""

import os
import json
import sys
from pathlib import Path
import networkx as nx
from _builder.utils import _ensure_mutable_graph
from _builder.graph_build import _load_full_graph, split_by_domain
import logging


def cmd_apply_semantics(args):
    """Apply LLM-filled semantic descriptions back into the graph."""
    graph_dir = args.graph
    sem_path = os.path.join(graph_dir, ".code2database_semantics.json")

    if not os.path.exists(sem_path):
        print("No .code2database_semantics.json found. Run 'extract-semantics' first.", file=sys.stderr)
        sys.exit(1)

    G = _load_full_graph(graph_dir)
    # Apply-semantics mutates node attrs (G.nodes[nid]["semantic_desc"] = ...);
    # LazySQLiteGraph is read-only and will crash on assignment. Detect early.
    _ensure_mutable_graph(G, "apply-semantics")
    sem_data = json.loads(Path(sem_path).read_text(encoding="utf-8"))
    nodes = sem_data.get("nodes_to_describe", [])

    updated = 0
    for node in nodes:
        nid = node["id"]
        desc = node.get("semantic_desc", "").strip()
        if nid not in G:
            continue
        if not desc:
            continue
        G.nodes[nid]["semantic_desc"] = desc
        updated += 1

    # Write back
    master = json.loads(Path(os.path.join(graph_dir, "code2database_master.json")).read_text(encoding="utf-8"))
    source_root = master.get("source_root", "")
    split_by_domain(G, graph_dir, source_root)

    # Generate semantics changelog for human review
    _write_semantics_changelog(graph_dir, nodes, updated)

    print(f"Applied semantic descriptions: {updated} node(s) updated")




def cmd_classify_endpoints(args):
    """Apply LLM classification results to endpoint nodes.

    Reads .code2database_endpoints.json (with LLM-filled classification/external_desc),
    updates the graph: out_end+desc for known, unknown_end for unclear.
    Reports unknown_end count.
    """
    graph_dir = args.graph
    ep_path = os.path.join(graph_dir, ".code2database_endpoints.json")

    if not os.path.exists(ep_path):
        print("No .code2database_endpoints.json found. Run 'build' first.", file=sys.stderr)
        sys.exit(1)

    G = _load_full_graph(graph_dir)
    ep_data = json.loads(Path(ep_path).read_text(encoding="utf-8"))
    endpoints = ep_data.get("endpoints", [])

    out_end_count = 0
    unknown_end_count = 0
    unknown_names = []

    for ep in endpoints:
        nid = ep["id"]
        classification = ep.get("classification", "").strip()
        desc = ep.get("external_desc", "").strip()

        if nid not in G:
            continue

        ndata = G.nodes[nid]
        labels = ndata.get("labels", [])
        # Remove any previous out_end/unknown_end from labels
        labels = [l for l in labels if l not in ("out_end", "unknown_end")]

        if classification == "unknown_end" or (not desc and classification != "out_end"):
            labels.append("unknown_end")
            ndata["external_desc"] = ""
            unknown_end_count += 1
            unknown_names.append(ndata.get("name", nid))
        else:
            labels.append("out_end")
            ndata["external_desc"] = desc
            out_end_count += 1

        ndata["labels"] = labels

    # Write back updated graph
    master = json.loads(Path(os.path.join(graph_dir, "code2database_master.json")).read_text(encoding="utf-8"))
    source_root = master.get("source_root", "")
    split_by_domain(G, graph_dir, source_root)

    print(f"Classified {out_end_count + unknown_end_count} endpoint(s):")
    print(f"  out_end (known): {out_end_count}")
    print(f"  unknown_end (unclear): {unknown_end_count}")
    if unknown_end_count > 0:
        print(f"WARNING: {unknown_end_count} unknown_end nodes need manual review:")
        for name in unknown_names[:20]:
            print(f"  - {name}")
        if len(unknown_names) > 20:
            print(f"  ... and {len(unknown_names) - 20} more")




def cmd_extract_semantics(args):
    """Export nodes for LLM semantic description, plus any project documentation files found."""
    graph_dir = args.graph
    doc_dir = args.docs if args.docs else ""

    G = _load_full_graph(graph_dir)

    # Collect real (non-empty, non-external) nodes that lack semantic_desc
    nodes_to_describe = []
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        if not ndata.get("source_file", ""):
            continue
        if ndata.get("semantic_desc", ""):
            continue
        nodes_to_describe.append({
            "id": nid,
            "name": ndata.get("name", ""),
            "source_file": ndata.get("source_file", ""),
            "line": ndata.get("line", 0),
            "location": ndata.get("location", f"{ndata.get('source_file', '')}:{ndata.get('line', 0)}"),
            "domain": ndata.get("domain", "root"),
            "labels": ndata.get("labels", []),
            "api_constraints": ndata.get("api_constraints", ""),
            "semantic_desc": "",  # LLM fills this
        })

    # Collect documentation files from the project
    doc_files = []
    doc_extensions = {".md", ".txt", ".rst", ".adoc", ".tex", ".org"}
    if doc_dir and os.path.isdir(doc_dir):
        for dirpath, dirnames, filenames in os.walk(doc_dir):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith('.') and d not in ('__pycache__', 'node_modules', 'build')]
            for fname in filenames:
                ext = Path(fname).suffix.lower()
                if ext in doc_extensions:
                    doc_files.append(os.path.relpath(os.path.join(dirpath, fname), doc_dir))

    out_path = os.path.join(graph_dir, ".code2database_semantics.json")
    Path(out_path).write_text(
        json.dumps({
            "nodes_to_describe": nodes_to_describe,
            "doc_files": doc_files,
            "doc_root": doc_dir,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )

    print(f"Semantic extraction template: {len(nodes_to_describe)} nodes, {len(doc_files)} doc files")
    print(f"Exported to: {out_path}")
    if nodes_to_describe:
        print("Claude should read this file, analyze doc files, and fill semantic_desc for each node.")
        print("Then run 'apply-semantics' to write descriptions back into the graph.")




def cmd_think_chain(args):
    """Generate complete call chains from API_entry to out_end/unknown_end.

    Outputs a JSON file with full chains for Claude to analyze.
    Used for structured thinking with checkpoint/recovery support.
    """
    graph_dir = args.graph
    out_path = args.output if args.output else os.path.join(graph_dir, ".code2database_think_chain.json")

    G = _load_full_graph(graph_dir)

    # Find API_entry nodes and endpoint nodes
    api_entries = []
    endpoints = []
    for nid, ndata in G.nodes(data=True):
        labels = ndata.get("labels", [])
        if "API_entry" in labels:
            api_entries.append(nid)
        if "out_end" in labels or "unknown_end" in labels:
            endpoints.append(nid)

    # Find all simple paths from each API_entry to each endpoint
    chains = []
    seen_chains = set()  # dedup identical chains
    max_chains = getattr(args, 'max_chains', 200)  # safety limit

    # For large graphs all_simple_paths is dangerous (exponential in graph
    # connectivity). Cap the inputs and use a tighter cutoff, or skip the
    # enumeration entirely past a size threshold.
    n_nodes = G.number_of_nodes()
    if n_nodes > 50000:
        # Treat as large: limit API entries and endpoints, lower the cutoff.
        api_entries = api_entries[:30]
        endpoints = endpoints[:50]
        max_depth = min(max_depth, 8)
    else:
        api_entries = api_entries[:200]
        endpoints = endpoints[:200]
    max_depth = getattr(args, 'max_depth', 10) or 10

    # Build a call-only subgraph for pathfinding (exclude CONTAINS/IMPORTS)
    from _builder.utils import _make_call_graph
    call_G = _make_call_graph(G)

    for api_id in api_entries:
        if len(chains) >= max_chains:
            break
        for ep_id in endpoints:
            if len(chains) >= max_chains:
                break
            try:
                path_count = 0
                for path in nx.all_simple_paths(call_G, api_id, ep_id, cutoff=max_depth):
                    path_count += 1
                    if path_count > 20:  # limit paths per (api, endpoint) pair
                        break
                    # Build chain key for dedup (ignore empty nodes for comparison)
                    real_nodes = tuple(n for n in path if not G.nodes[n].get("is_empty", False))
                    if real_nodes in seen_chains:
                        continue
                    seen_chains.add(real_nodes)

                    # Build chain with edge attributes
                    chain_steps = []
                    for i, nid in enumerate(path):
                        nd = G.nodes[nid]
                        step = {
                            "id": nid,
                            "name": nd.get("name", ""),
                            "labels": nd.get("labels", []),
                            "is_empty": nd.get("is_empty", False),
                            "condition": nd.get("condition", ""),
                        }
                        if i > 0:
                            ed = G.get_edge_data(path[i-1], nid) or {}
                            step["call_order"] = ed.get("call_order")
                            step["call_condition"] = ed.get("call_condition", "")
                        chain_steps.append(step)

                    chains.append({
                        "from_api": api_id,
                        "to_endpoint": ep_id,
                        "length": len(path) - 1,
                        "steps": chain_steps,
                        "conclusion": "",  # LLM fills
                    })
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                continue
    # and standalone chains (API_entry with no path to endpoint)
    api_set = set(api_entries)
    ep_set = set(endpoints)
    for api_id in api_entries:
        if len(chains) >= max_chains:
            break
        # Check if this API has any path to an endpoint (using BFS reachability, not all_simple_paths)
        has_endpoint_path = False
        reachable = set()
        try:
            reachable = nx.descendants(call_G, api_id)
            has_endpoint_path = bool(reachable & ep_set)
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        if not has_endpoint_path:
            # Generate partial chain: API_entry → deepest reachable nodes
            for nid in sorted(reachable):
                nd = G.nodes[nid]
                if nd.get("is_empty", False):
                    continue
                # Leaf node (no successors)
                if call_G.out_degree(nid) == 0 and nid not in ep_set:
                    try:
                        path = nx.shortest_path(call_G, api_id, nid)
                        real_nodes = tuple(n for n in path if not G.nodes[n].get("is_empty", False))
                        if real_nodes not in seen_chains:
                            seen_chains.add(real_nodes)
                            chain_steps = []
                            for i, p_nid in enumerate(path):
                                p_nd = G.nodes[p_nid]
                                step = {
                                    "id": p_nid,
                                    "name": p_nd.get("name", ""),
                                    "labels": p_nd.get("labels", []),
                                    "is_empty": p_nd.get("is_empty", False),
                                    "condition": p_nd.get("condition", ""),
                                }
                                if i > 0:
                                    ed = G.get_edge_data(path[i-1], p_nid) or {}
                                    step["call_order"] = ed.get("call_order")
                                    step["call_condition"] = ed.get("call_condition", "")
                                chain_steps.append(step)
                            chains.append({
                                "from_api": api_id,
                                "to_endpoint": nid,
                                "length": len(path) - 1,
                                "steps": chain_steps,
                                "conclusion": "",
                            })
                    except (nx.NetworkXNoPath, nx.NodeNotFound):
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        continue
    Path(out_path).write_text(
        json.dumps({
            "total_chains": len(chains),
            "api_entries": len(api_entries),
            "endpoints": len(endpoints),
            "chains": chains,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )

    print(f"Call chains: {len(chains)} from {len(api_entries)} API_entry → {len(endpoints)} endpoints")
    print(f"Exported to: {out_path}")


def _write_semantics_changelog(graph_dir: str, nodes: list, updated_count: int):
    """Generate .semantics_changelog.md for human review of LLM changes."""
    from datetime import datetime

    lines = [
        "# Semantics Changelog",
        "",
        f"**Date**: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**Nodes updated**: {updated_count}",
        "",
        "## Changes",
        "",
    ]

    for node in nodes:
        desc = node.get("semantic_desc", "").strip()
        if not desc:
            continue
        name = node.get("name", node.get("id", ""))
        source = node.get("source_file", "")
        line = node.get("line", 0)
        old_desc = node.get("previous_desc", "")
        lines.append(f"- **{name}** ({source}:{line})")
        if old_desc:
            lines.append(f"  - Before: {old_desc[:80]}")
        lines.append(f"  - After: {desc[:80]}")
        lines.append("")

    lines.append("## Review Needed")
    lines.append("")
    lines.append("The following entries were filled by LLM and should be verified:")
    lines.append("")
    for node in nodes:
        desc = node.get("semantic_desc", "").strip()
        if not desc:
            continue
        name = node.get("name", node.get("id", ""))
        source = node.get("source_file", "")
        line = node.get("line", 0)
        lines.append(f"- [ ] {name} ({source}:{line})")

    changelog_path = os.path.join(graph_dir, ".semantics_changelog.md")
    Path(changelog_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_extract_signals(args):
    """Extract how #ifdef conditions affect call chain paths.

    Reads the condition index and graph, builds a mapping:
    condition → {affected_edges, affected_functions, affected_chains}.
    Output: .code2database_signal_map.json
    """
    graph_dir = args.graph
    out_path = args.output if args.output else os.path.join(graph_dir, ".code2database_signal_map.json")

    G = _load_full_graph(graph_dir)

    # Load condition index
    cond_path = os.path.join(graph_dir, ".code2database_condition_index.json")
    if not os.path.exists(cond_path):
        print("No condition index found. Run 'build' first.", file=sys.stderr)
        sys.exit(1)

    cond_data = json.loads(Path(cond_path).read_text(encoding="utf-8"))

    # Build signal map: condition → affected nodes/edges
    signal_map = {}
    for nid, branches in cond_data.items():
        nd = G.nodes.get(nid)
        if not nd:
            continue
        node_name = nd.get("name", nid)

        for branch in branches:
            condition = branch.get("condition", "")
            if not condition:
                continue

            # Normalize condition: extract the #ifdef variable name
            # e.g., "#ifdef HAVE_RDMA" → "HAVE_RDMA"
            cond_var = condition.strip()
            for prefix in ("#ifdef ", "#ifndef ", "#if ", "#elif "):
                if cond_var.startswith(prefix):
                    cond_var = cond_var[len(prefix):].strip()
                    break

            target_name = branch.get("target_name", "")
            target_node = branch.get("target_node", "")

            if cond_var not in signal_map:
                signal_map[cond_var] = {
                    "condition": condition,
                    "affected_edges": [],
                    "affected_functions": set(),
                    "affected_domains": set(),
                }

            signal_map[cond_var]["affected_edges"].append({
                "source": node_name,
                "target": target_name,
                "condition": condition,
            })
            signal_map[cond_var]["affected_functions"].add(node_name)
            if target_name:
                signal_map[cond_var]["affected_functions"].add(target_name)
            signal_map[cond_var]["affected_domains"].add(nd.get("domain", ""))

    # Also scan preproc_condition edges directly (call edges only)
    for u, v, edata in G.edges(data=True):
        if edata.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        pp_cond = edata.get("preproc_condition", "")
        if not pp_cond:
            continue
        cond_var = pp_cond.strip()
        for prefix in ("#ifdef ", "#ifndef ", "#if ", "#elif "):
            if cond_var.startswith(prefix):
                cond_var = cond_var[len(prefix):].strip()
                break

        if cond_var not in signal_map:
            signal_map[cond_var] = {
                "condition": pp_cond,
                "affected_edges": [],
                "affected_functions": set(),
                "affected_domains": set(),
            }
        u_name = G.nodes[u].get("name", u) if u in G else u
        v_name = G.nodes[v].get("name", v) if v in G else v
        signal_map[cond_var]["affected_edges"].append({
            "source": u_name,
            "target": v_name,
            "condition": pp_cond,
            "alive": edata.get("preproc_alive", True),
        })
        signal_map[cond_var]["affected_functions"].add(u_name)
        signal_map[cond_var]["affected_functions"].add(v_name)
        u_dom = G.nodes[u].get("domain", "") if u in G else ""
        signal_map[cond_var]["affected_domains"].add(u_dom)

    # Convert sets to sorted lists for JSON serialization
    for var in signal_map:
        signal_map[var]["affected_functions"] = sorted(signal_map[var]["affected_functions"])
        signal_map[var]["affected_domains"] = sorted(signal_map[var]["affected_domains"])

    # Sort by number of affected edges (most impactful signals first)
    sorted_signals = sorted(signal_map.items(), key=lambda x: -len(x[1]["affected_edges"]))

    output = {
        "total_signals": len(sorted_signals),
        "signals": {var: data for var, data in sorted_signals},
    }

    Path(out_path).write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )

    print(f"Signal extraction: {len(sorted_signals)} condition signals found")
    for var, data in sorted_signals[:10]:
        print(f"  {var}: {len(data['affected_edges'])} edges, "
              f"{len(data['affected_functions'])} functions, "
              f"{len(data['affected_domains'])} domains")
    print(f"Exported to: {out_path}")