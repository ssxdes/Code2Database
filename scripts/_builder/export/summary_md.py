"""index_pack.summary_md — split from index_pack.py."""

from _builder.export.indexes import (
    _compute_hub_functions,
    _build_scenarios_summary_md,
    _generate_mermaid_path_diagram,
)
"""callgraph builder module: index_pack."""

import os
import json
import sys
import re
from pathlib import Path
from collections import Counter, defaultdict
import networkx as nx
from _builder.query.query import _resolve_detailed_chain, _trace_simple_chain
from _builder.token_budget import estimate_tokens
from _builder.utils import normalize_str_field
import logging


def _build_callgraph_summary_md(G: nx.DiGraph, outdir: str, source_root: str = "",
                                build_info: dict = None):
    """Generate CODE2DATABASE_SUMMARY.md — layered human-readable executive summary.

    Three reading layers:
      L0 (5s): Project description + total stats + domain list + API count
      L1 (30s): Domain table + Top 5 critical paths + community map
      L2 (3min): Full API catalog + concurrency + data flow + confidence + build config (collapsible)
    """
    from datetime import datetime

    domains = defaultdict(lambda: {"apis": [], "internal": [], "thread_entries": [],
                                    "callback_entries": [], "endpoints": []})
    api_ids = []
    ep_ids = []
    dead_funcs = []
    flow_hotspots = []
    _flow_api_entries = []
    _flow_endpoint_entries = []
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        dom = ndata.get("domain", "root")
        labels = ndata.get("labels", [])
        name = ndata.get("name", "")
        entry = {"id": nid, "name": name,
                 "signature": ndata.get("signature", ""),
                 "source_file": ndata.get("source_file", ""),
                 "line": ndata.get("line", 0),
                 "labels": labels,
                 "api_constraints": ndata.get("api_constraints", ""),
                 "external_desc": ndata.get("external_desc", ""),
                 "semantic_desc": ndata.get("semantic_desc", "")}
        if "API_entry" in labels:
            domains[dom]["apis"].append(entry)
            api_ids.append(nid)
            _flow_api_entries.append({"id": nid, "name": name, "domain": dom})
        elif ("out_end" in labels or "unknown_end" in labels) and (
                dom.startswith("external_") or dom == "external"
                or not ndata.get("source_file", "")):
            domains[dom]["endpoints"].append(entry)
            ep_ids.append(nid)
            _flow_endpoint_entries.append({"id": nid, "name": name, "domain": dom})
        else:
            domains[dom]["internal"].append(entry)
        if "thread_processor" in labels:
            domains[dom]["thread_entries"].append(entry)
        if "callback_func" in labels:
            domains[dom]["callback_entries"].append(entry)
        if "dead_code" in labels:
            dead_funcs.append(name or nid)
        if "API_entry" in labels:
            for p in ndata.get("params", []):
                pname = p.get("name", "")
                if not pname:
                    continue
                ptype = p.get("type", "")
                flows_conditions = []
                flows_callees = []
                for cv in ndata.get("condition_vars", []):
                    if pname in cv.get("vars", []):
                        flows_conditions.append(cv.get("condition", ""))
                for ca in ndata.get("callee_args", []):
                    for arg in ca.get("args", []):
                        if pname in (arg.get("value", "") or ""):
                            flows_callees.append(ca.get("callee", ""))
                if flows_conditions or flows_callees:
                    flow_hotspots.append(
                        f"- `{pname}` ({ptype}): flows from {name}(param)"
                        f" → {', '.join(flows_conditions[:3])}"
                        f" → {{{', '.join(flows_callees[:5])}}}")

    all_apis = []
    for dom in sorted(domains.keys()):
        all_apis.extend(domains[dom]["apis"])
    api_count = len(all_apis)
    int_count = sum(len(d["internal"]) for d in domains.values())
    thread_count = sum(len(d["thread_entries"]) for d in domains.values())

    # Separate external domain counts for the summary header
    project_domain_count = sum(1 for d in domains.keys()
                                if not d.startswith("external_") and d != "external")
    ext_domain_count = sum(1 for d in domains.keys()
                           if d.startswith("external_") or d == "external")

    # === L0: 5-second overview ===
    header_domains = f"{project_domain_count} domains"
    if ext_domain_count:
        header_domains += f" + {ext_domain_count} external"
    lines = [f"# Code2Database Summary — {source_root or 'project'}",
             "",
             f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             f"**{G.number_of_nodes()} nodes** | **{G.number_of_edges()} edges** | "
             f"**{header_domains}** | **{api_count} API entries** | **{thread_count} thread entries**",
             ""]

    # === L1: 30-second domain table ===
    # Separate project domains from external/third-party domains
    project_domains = {k: v for k, v in domains.items()
                       if not k.startswith("external_") and k != "external"}
    ext_domains = {k: v for k, v in domains.items()
                   if k.startswith("external_") or k == "external"}

    lines.append("## Architecture Overview")
    lines.append("")
    lines.append("| Domain | API Entries | Internal | Depth Ratio | Assessment |")
    lines.append("|--------|------------|----------|-------------|------------|")
    # Sort domains by total function count (descending) so important subsystems appear first
    sorted_project_domains = sorted(project_domains.keys(),
                                    key=lambda d: len(project_domains[d]["apis"]) + len(project_domains[d]["internal"]),
                                    reverse=True)
    for dom in sorted_project_domains:
        d = project_domains[dom]
        api_c = len(d["apis"])
        int_c = len(d["internal"])
        total = api_c + int_c
        ratio = api_c / total if total > 0 else 0
        assessment = "Deep ✓" if ratio < 0.3 else ("Balanced" if ratio < 0.5 else "Shallow ⚠")
        lines.append(f"| {dom} | {api_c} | {int_c} | {ratio:.2f} | {assessment} |")
    lines.append("")

    # External / Third-Party Domains
    if ext_domains:
        lines.append("### External / Third-Party Domains")
        lines.append("")
        lines.append("| Domain | API Entries | Internal | Endpoints |")
        lines.append("|--------|------------|----------|-----------|")
        for dom in sorted(ext_domains.keys()):
            d = ext_domains[dom]
            api_c = len(d["apis"])
            int_c = len(d["internal"])
            ep_c = len(d["endpoints"])
            lines.append(f"| {dom} | {api_c} | {int_c} | {ep_c} |")
        lines.append("")

    # Hub functions (top betweenness)
    hubs = _compute_hub_functions(G, top_n=5)
    if hubs:
        lines.append("### Hub Functions (Top Betweenness Centrality)")
        lines.append("")
        for h in hubs:
            lines.append(f"- **{h['name']}** ({h['domain']}) — betweenness={h['betweenness']:.4f}, "
                         f"cross-domain callers={h['callers_from_domains']}")
        lines.append("")

    # Community Map (Leiden)
    comm_path = os.path.join(outdir, ".code2database_communities.json")
    if os.path.exists(comm_path):
        comm_data = json.loads(Path(comm_path).read_text(encoding="utf-8"))
        communities = comm_data.get("communities", [])
        if communities:
            lines.append("### Community Map (Leiden Algorithm)")
            lines.append("")
            lines.append("| Community | Label | Size | Cohesion | Keywords |")
            lines.append("|-----------|-------|------|----------|----------|")
            for comm in sorted(communities, key=lambda c: c.get("symbol_count", 0), reverse=True)[:10]:
                kw_str = ", ".join(comm.get("keywords", [])[:5])
                lines.append(f"| {comm['id']} | {comm.get('label', '')} | "
                             f"{comm.get('symbol_count', 0)} | "
                             f"{comm.get('cohesion', 0):.2f} | {kw_str} |")
            lines.append("")

    # Execution Processes (BFS traces)
    proc_path = os.path.join(outdir, ".code2database_processes.json")
    if os.path.exists(proc_path):
        proc_data = json.loads(Path(proc_path).read_text(encoding="utf-8"))
        processes = proc_data.get("processes", [])
        if processes:
            lines.append("### Execution Processes")
            lines.append("")
            for proc in processes[:10]:
                steps = proc.get("steps", [])
                step_str = " → ".join(steps[:8])
                if len(steps) > 8:
                    step_str += " → ..."
                comm_cross = proc.get("communities_crossed", 0)
                lines.append(f"- **{proc.get('label', '')}** "
                             f"(score={proc.get('entry_score', 0):.2f}, "
                             f"steps={proc.get('step_count', 0)}, "
                             f"cross-community={comm_cross}): {step_str}")
            lines.append("")

    # Critical Paths with Mermaid diagram
    chains = []
    top_paths = []
    n_nodes = G.number_of_nodes()
    # For very large graphs (>50K nodes), skip pathfinding entirely —
    # _make_call_graph duplicates the entire graph and shortest_path is O(V+E) per query.
    # Use hub functions (already computed above) as a lightweight alternative.
    if n_nodes < 50000:
        # Build call-only subgraph for pathfinding (exclude CONTAINS/IMPORTS edges)
        from _builder.utils import _make_call_graph
        _call_G = _make_call_graph(G)
        for api_id in api_ids[:20]:
            if n_nodes < 5000:
                # Small graph: find simple paths
                for ep_id in ep_ids[:50]:
                    try:
                        path_count = 0
                        for path in nx.all_simple_paths(_call_G, api_id, ep_id, cutoff=10):
                            real = [(n, G.nodes[n]) for n in path if not G.nodes[n].get("is_empty", False)]
                            if len(real) >= 2:
                                annotated = []
                                for i, (pnid, pnd) in enumerate(real):
                                    if i == 0:
                                        annotated.append(f"**{pnd.get('name', '')}**")
                                    else:
                                        ed = G.get_edge_data(real[i-1][0], pnid) or {}
                                        cond = ed.get("call_condition", "")
                                        prefix = f"[{cond}] " if cond else ""
                                        annotated.append(f"{prefix}{pnd.get('name', '')}")
                                chains.append((len(real), " → ".join(annotated), api_id, ep_id, path))
                                top_paths.append(path)
                                path_count += 1
                                if path_count >= 3:
                                    break
                    except (nx.NetworkXNoPath, nx.NodeNotFound):
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        continue
            else:
                # Medium graph: shortest paths only
                for ep_id in ep_ids[:50]:
                    try:
                        path = nx.shortest_path(_call_G, api_id, ep_id)
                        real = [(n, G.nodes[n]) for n in path if not G.nodes[n].get("is_empty", False)]
                        if len(real) >= 2:
                            annotated = []
                            for i, (pnid, pnd) in enumerate(real):
                                if i == 0:
                                    annotated.append(f"**{pnd.get('name', '')}**")
                                else:
                                    ed = G.get_edge_data(real[i-1][0], pnid) or {}
                                    cond = ed.get("call_condition", "")
                                    prefix = f"[{cond}] " if cond else ""
                                    annotated.append(f"{prefix}{pnd.get('name', '')}")
                            chains.append((len(real), " → ".join(annotated), api_id, ep_id, path))
                            top_paths.append(path)
                    except (nx.NetworkXNoPath, nx.NodeNotFound):
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        continue
        del _call_G
    else:
        # Large graph: use hub-based paths instead of expensive shortest_path
        # Just list top hub functions as critical path proxies
        if hubs:
            for h in hubs[:5]:
                chains.append((1, f"{h['name']} (hub, cross-domain={h['callers_from_domains']})", "", "", []))
    chains.sort(key=lambda x: -x[0])
    if chains:
        lines.append("## Critical Paths (API → Endpoint, longest first)")
        lines.append("")
        for i, (length, chain_str, _, _, _) in enumerate(chains[:20], 1):
            lines.append(f"{i}. {chain_str} ({length} steps)")
        lines.append("")

    # Mermaid diagram for top 3 paths
    if top_paths:
        mermaid = _generate_mermaid_path_diagram(G, top_paths[:3], "Top Critical Paths")
        lines.append("### Critical Path Diagram")
        lines.append("")
        lines.append(mermaid)
        lines.append("")

    # === L2: 3-minute detailed view (collapsible) ===
    lines.append("<details>")
    lines.append("<summary><strong>Full Details</strong> (click to expand)</summary>")
    lines.append("")

    # Public API Catalog (truncated to top 100 by entry score)
    # Only include project APIs (exclude external/third-party domains)
    project_apis = [a for a in all_apis
                    if not a.get("domain", "").startswith("external_")
                    and a.get("domain", "") != "external"]
    if project_apis:
        # Sort by entry_score if available, otherwise by name
        scored_apis = []
        for api in project_apis:
            score = G.nodes.get(api.get("id", ""), {}).get("entry_score", 0)
            scored_apis.append((api, score))
        scored_apis.sort(key=lambda x: -x[1])
        top_apis = scored_apis[:100]

        lines.append("## Public API Catalog")
        if len(project_apis) > 100:
            lines.append(f"\n> Showing top 100 of {len(project_apis)} API entries (sorted by entry score)")
        lines.append("")
        lines.append("| Function | Domain | Signature | Constraints |")
        lines.append("|----------|--------|-----------|-------------|")
        for api, _ in top_apis:
            sig = api["signature"].replace("|", "\\|")[:60]
            _constraints = normalize_str_field(api.get("api_constraints", ""))
            constraints = _constraints.replace("|", "\\|")[:40] if _constraints else "—"
            lines.append(f"| {api['name']} | {api.get('domain', '')} | {sig} | {constraints} |")
        lines.append("")

    # Concurrency Map
    spawn_points = []
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        for ca in ndata.get("callee_args", []):
            ci = ca.get("concurrency_info", {})
            if ci.get("is_spawn") or ci.get("concurrency_type") in ("thread_spawn", "goroutine"):
                target = ci.get("spawn_target", "")
                spawn_order = ca.get("call_order", 0)
                concurrent = []
                for succ in G.successors(nid):
                    ed = G.get_edge_data(nid, succ) or {}
                    if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                        continue
                    if ed.get("call_order") is not None and ed["call_order"] > spawn_order and \
                       ed.get("concurrency") not in ("spawn_target", "callback"):
                        concurrent.append(G.nodes[succ].get("name", ""))
                risk = "Race ⚠" if concurrent else "Safe"
                spawn_points.append({
                    "spawn_node": ndata.get("name", ""),
                    "spawn_file": ndata.get("source_file", ""),
                    "spawn_line": ndata.get("line", 0),
                    "thread_entry": target,
                    "concurrent_with": concurrent,
                    "risk": risk,
                })
    if spawn_points:
        lines.append("## Concurrency Map")
        lines.append("")
        lines.append("| Spawn Point | Thread Entry | Concurrent With | Risk |")
        lines.append("|-------------|-------------|----------------|------|")
        for sp in spawn_points:
            loc = f"{sp['spawn_file']}:{sp['spawn_line']}"
            conc_list = ", ".join(sp["concurrent_with"][:3])
            if len(sp["concurrent_with"]) > 3:
                conc_list += f" +{len(sp['concurrent_with'])-3} more"
            lines.append(f"| {loc} → {sp['spawn_node']} | {sp['thread_entry']} | {conc_list or '—'} | {sp['risk']} |")
        lines.append("")

    # External Endpoints (truncated to top 100 by caller count)
    all_eps = []
    for dom in sorted(domains.keys()):
        all_eps.extend(domains[dom]["endpoints"])
    if all_eps:
        # Sort by number of callers (most-used endpoints first)
        scored_eps = []
        for ep in all_eps:
            caller_count = sum(1 for pred in G.predecessors(ep["id"])
                              if (G.get_edge_data(pred, ep["id"]) or {}).get("relation") not in ("CONTAINS", "IMPORTS")
                              ) if ep["id"] in G else 0
            scored_eps.append((ep, caller_count))
        scored_eps.sort(key=lambda x: -x[1])
        top_eps = scored_eps[:100]

        lines.append("## External Endpoints")
        if len(all_eps) > 100:
            lines.append(f"\n> Showing top 100 of {len(all_eps)} endpoints (sorted by caller count)")
        lines.append("")
        lines.append("| Function | Classification | Description | Callers |")
        lines.append("|----------|---------------|-------------|---------|")
        for ep, _ in top_eps:
            cls = "unknown" if "unknown_end" in ep["labels"] else "external"
            desc = ep.get("external_desc", "") or ep.get("semantic_desc", "") or "(needs classification)"
            desc = desc.replace("|", "\\|")[:50]
            callers = []
            for pred in G.predecessors(ep["id"]):
                ed = G.get_edge_data(pred, ep["id"]) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                callers.append(G.nodes[pred].get("name", ""))
            caller_str = ", ".join(callers[:3])
            if len(callers) > 3:
                caller_str += f" +{len(callers)-3}"
            lines.append(f"| {ep['name']} | {cls} | {desc} | {caller_str or '—'} |")
        lines.append("")

    # Data Flow Hotspots (already collected in main traversal above)
    if flow_hotspots:
        lines.append("## Data Flow Hotspots")
        lines.append("")
        lines.extend(flow_hotspots)
        lines.append("")

    # Confidence breakdown (call edges only, exclude CONTAINS/IMPORTS)
    edge_conf = defaultdict(int)
    for u, v, edata in G.edges(data=True):
        if edata.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        conf = edata.get("confidence", "EXTRACTED")
        edge_conf[conf] += 1
    call_edge_total = sum(edge_conf.values())
    if call_edge_total > 0:
        lines.append("## Edge Confidence")
        lines.append("")
        for conf in ("EXTRACTED", "INFERRED", "AMBIGUOUS"):
            count = edge_conf.get(conf, 0)
            pct = count / call_edge_total * 100 if call_edge_total else 0
            lines.append(f"- **{conf}**: {count} ({pct:.1f}%)")
        lines.append("")

    # Build Configuration section
    if build_info:
        lines.append("## Build Configuration")
        lines.append("")
        lines.append(f"| Item | Value |")
        lines.append(f"|------|-------|")
        lines.append(f"| Build system | {build_info.get('build_system', 'none')} |")
        if build_info.get('selected_config'):
            lines.append(f"| Config | {build_info['selected_config']} |")
        macro_names = list(build_info.get('defined_macros', {}).keys())
        if macro_names:
            lines.append(f"| Defined macros | {', '.join(macro_names[:15])} |")
        if dead_funcs:
            dead_list = ", ".join(dead_funcs[:10])
            if len(dead_funcs) > 10:
                dead_list += f" (+{len(dead_funcs)-10} more)"
            lines.append(f"| Dead code (excluded by macros) | {dead_list} |")
        lines.append("")

    lines.append("</details>")

    summary_path = os.path.join(outdir, "CODE2DATABASE_SUMMARY.md")
    Path(summary_path).write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Also generate SCENARIOS_SUMMARY.md
    _build_scenarios_summary_md(G, outdir)

    # Generate ARCHITECTURE_FLOWS.md — human-readable narrative of core execution flows
    # Compute api_entries and endpoint_entries from G for this function scope
    _flow_api_entries = [{"id": nid, "name": G.nodes[nid].get("name", ""),
                          "domain": G.nodes[nid].get("domain", "")}
                         for nid, d in G.nodes(data=True)
                         if "API_entry" in d.get("labels", []) and not d.get("is_empty", False)]
    _flow_endpoint_entries = [{"id": nid, "name": G.nodes[nid].get("name", ""),
                               "domain": G.nodes[nid].get("domain", "")}
                              for nid, d in G.nodes(data=True)
                              if ("out_end" in d.get("labels", []) or "unknown_end" in d.get("labels", []))
                              and not d.get("is_empty", False)]
    _build_architecture_flows_md(G, outdir, source_root, chains,
                                  _flow_api_entries, _flow_endpoint_entries, domains)

    return summary_path





def _build_architecture_flows_md(G: nx.DiGraph, outdir: str, source_root: str,
                                  chains: list, api_entries: list,
                                  endpoint_entries: list, domains: dict):
    """Generate ARCHITECTURE_FLOWS.md — human-readable narrative of core execution flows."""
    from datetime import datetime

    lines = [f"# Architecture Flows — {source_root or 'project'}",
             "",
             f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             "",
             "This document describes the core execution flows through the codebase.",
             "Each flow traces the path from an API entry point through the system.",
             "",
             ""]

    # Top 5 flows from chains (sorted by length = complexity)
    top_chains = sorted(chains, key=lambda x: -x[0])[:5]

    for i, (length, chain_str, api_id, ep_id, path) in enumerate(top_chains, 1):
        api_name = G.nodes[api_id].get("name", api_id) if api_id in G else api_id
        ep_name = G.nodes[ep_id].get("name", ep_id) if ep_id in G else ep_id

        lines.append(f"## Flow {i}: {api_name} → {ep_name}")
        lines.append("")
        lines.append(f"**Length**: {length} steps")
        lines.append(f"**Path**: {chain_str}")
        lines.append("")

        # Annotate key points: conditions, concurrency, domain crossings
        conditions = []
        concurrency = []
        domain_crossings = []
        prev_domain = None
        for j, pnid in enumerate(path):
            if G.nodes[pnid].get("is_empty", False):
                continue
            nd = G.nodes[pnid]
            dom = nd.get("domain", "")
            if prev_domain and dom != prev_domain:
                domain_crossings.append(f"  - Step {j}: {prev_domain} → {dom} ({nd.get('name', '')})")
            prev_domain = dom
            if j > 0:
                ed = G.get_edge_data(path[j-1], pnid) or {}
                cond = ed.get("call_condition", "")
                conc = ed.get("concurrency", "")
                if cond:
                    conditions.append(f"  - Step {j}: [{cond}] → {nd.get('name', '')}")
                if conc in ("spawn_target", "thread_spawn", "goroutine"):
                    concurrency.append(f"  - Step {j}: {nd.get('name', '')} (spawned thread)")
                elif conc == "callback":
                    concurrency.append(f"  - Step {j}: {nd.get('name', '')} (callback)")

        if conditions:
            lines.append("**Conditions**:")
            lines.extend(conditions)
            lines.append("")
        if concurrency:
            lines.append("**Concurrency**:")
            lines.extend(concurrency)
            lines.append("")
        if domain_crossings:
            lines.append("**Domain Crossings**:")
            lines.extend(domain_crossings)
            lines.append("")
        lines.append("---")
        lines.append("")

    # Also add domain-level flow map
    lines.append("## Domain Flow Map")
    lines.append("")
    lines.append("Shows which domains call into which other domains.")
    lines.append("")
    domain_edges = defaultdict(int)
    for u, v, edata in G.edges(data=True):
        if edata.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        u_dom = G.nodes[u].get("domain", "") if u in G else ""
        v_dom = G.nodes[v].get("domain", "") if v in G else ""
        if u_dom and v_dom and u_dom != v_dom:
            domain_edges[(u_dom, v_dom)] += 1
    if domain_edges:
        sorted_edges = sorted(domain_edges.items(), key=lambda x: -x[1])[:20]
        for (src, dst), count in sorted_edges:
            lines.append(f"- **{src}** → **{dst}** ({count} edges)")
    lines.append("")

    flows_path = os.path.join(outdir, "ARCHITECTURE_FLOWS.md")
    Path(flows_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


