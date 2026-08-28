"""callgraph builder module: index_pack."""

import os
import json
import sys
import re
from pathlib import Path
from collections import Counter, defaultdict
import networkx as nx
from _builder.query import _resolve_detailed_chain, _trace_simple_chain
from _builder.token_budget import estimate_tokens

# Import universal skip names from scanner for automatic external endpoint classification
try:
    from _vendor._regex_c_scanner import _UNIVERSAL_SKIP_NAMES as _SCANNER_SKIP_NAMES
except ImportError:
    _SCANNER_SKIP_NAMES = frozenset()


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
            constraints = api["api_constraints"].replace("|", "\\|")[:40] if api["api_constraints"] else "—"
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


def _build_context_pack(G: nx.DiGraph, outdir: str, source_root: str = "",
                        build_info: dict = None):
    """Generate .code2database_context_pack.json — single-file LLM context for the whole project."""
    # Project summary
    api_entries = []
    thread_entries = []
    callback_entries = []
    endpoint_entries = []
    domain_data = {}

    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        dom = ndata.get("domain", "root")
        labels = ndata.get("labels", [])

        if dom not in domain_data:
            domain_data[dom] = {"apis": 0, "internal": 0, "endpoints": 0, "depends_on": set()}

        if "API_entry" in labels:
            domain_data[dom]["apis"] += 1
            api_entries.append({"id": nid, "name": ndata.get("name", ""),
                                "signature": ndata.get("signature", ""),
                                "domain": dom})
        elif ("out_end" in labels or "unknown_end" in labels) and (
                dom.startswith("external_") or dom == "external"
                or not ndata.get("source_file", "")):
            # Only count truly external endpoints: nodes in external domains
            # or nodes without source files. Internal leaf functions are not
            # external endpoints even if they have out_end label.
            domain_data[dom]["endpoints"] += 1
            endpoint_entries.append({"id": nid, "name": ndata.get("name", ""),
                                      "domain": dom,
                                      "desc": ndata.get("external_desc", "")})
        else:
            domain_data[dom]["internal"] += 1

        if "thread_processor" in labels:
            thread_entries.append({"id": nid, "name": ndata.get("name", ""), "domain": dom})
        if "callback_func" in labels:
            callback_entries.append({"id": nid, "name": ndata.get("name", ""), "domain": dom})

    # Cross-domain dependencies (call edges only, exclude CONTAINS/IMPORTS)
    for u, v, edata in G.edges(data=True):
        if edata.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        u_dom = G.nodes[u].get("domain", "root") if u in G else "root"
        v_dom = G.nodes[v].get("domain", "root") if v in G else "root"
        if u_dom != v_dom and u_dom in domain_data:
            domain_data[u_dom]["depends_on"].add(v_dom)

    # Compute depth ratios and classify
    shallow_domains = []
    deep_domains = []
    external_domains = []
    domain_map_out = {}
    for dom in sorted(domain_data.keys()):
        d = domain_data[dom]
        total = d["apis"] + d["internal"]
        ratio = d["apis"] / total if total > 0 else 0
        d["ratio"] = round(ratio, 2)
        d["depends_on"] = sorted(d["depends_on"])
        del d["ratio"]  # already in domain_map_out
        # External domains go into their own list, not shallow/deep
        if dom.startswith("external_") or dom == "external":
            external_domains.append(dom)
            domain_map_out[dom] = {
                "apis": d["apis"], "internal": d["internal"],
                "ratio": round(ratio, 2), "endpoints": d["endpoints"],
                "depends_on": sorted(d["depends_on"]),
                "is_external": True,
            }
            continue
        domain_map_out[dom] = {
            "apis": d["apis"], "internal": d["internal"],
            "ratio": round(ratio, 2), "endpoints": d["endpoints"],
            "depends_on": sorted(d["depends_on"]),
        }
        if ratio >= 0.5:
            shallow_domains.append(dom)
        elif ratio < 0.3:
            deep_domains.append(dom)

    # Execution scenarios: pre-compute enum-driven call chains
    scenarios = _compute_scenarios(G, outdir)

    # Data flow index
    data_flow = _compute_data_flow(G, outdir)

    # Concurrency summary
    concurrency_summary = {"spawn_points": 0, "concurrent_windows": []}
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        for ca in ndata.get("callee_args", []):
            ci = ca.get("concurrency_info", {})
            if ci.get("is_spawn") or ci.get("concurrency_type") in ("thread_spawn", "goroutine"):
                concurrency_summary["spawn_points"] += 1
                spawn_order = ca.get("call_order", 0)
                main_calls = []
                for succ in G.successors(nid):
                    ed = G.get_edge_data(nid, succ) or {}
                    if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                        continue
                    if ed.get("call_order") is not None and ed["call_order"] > spawn_order and \
                       ed.get("concurrency") not in ("spawn_target", "callback"):
                        main_calls.append(G.nodes[succ].get("name", ""))
                concurrency_summary["concurrent_windows"].append({
                    "spawn_at": f"{ndata.get('source_file', '')}:{ndata.get('line', 0)}",
                    "spawn_fn": ndata.get("name", ""),
                    "thread_fn": ci.get("spawn_target", ""),
                    "main_thread_calls": main_calls,
                })

    # Edge confidence breakdown (call edges only, exclude CONTAINS/IMPORTS)
    edge_confidence = {"EXTRACTED": 0, "INFERRED": 0, "AMBIGUOUS": 0}
    for u, v, edata in G.edges(data=True):
        if edata.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        conf = edata.get("confidence", "EXTRACTED")
        if conf in edge_confidence:
            edge_confidence[conf] += 1

    pack = {
        "project_summary": {
            "source_root": source_root,
            "total_functions": sum(1 for _, d in G.nodes(data=True)
                                   if not d.get("is_empty", False)
                                   and d.get("node_type") != "file"
                                   and not d.get("auto_created", False)),
            "total_domains": len(domain_data),
            "api_entries": [a["name"] for a in api_entries],
            "thread_entries": [t["name"] for t in thread_entries],
            "callback_entries": [c["name"] for c in callback_entries],
            "shallow_domains": shallow_domains,
            "deep_domains": deep_domains,
            "external_domains": external_domains,
            "total_nodes": G.number_of_nodes(),
            "total_edges": G.number_of_edges(),
            "total_nodes_all": G.number_of_nodes(),
            "dead_code_functions": [nd.get("name", nid) for nid, nd in G.nodes(data=True)
                                    if "dead_code" in nd.get("labels", [])],
        },
        "domain_map": domain_map_out,
        "api_catalog": api_entries,
        "execution_scenarios": scenarios,
        "data_flow_index": data_flow,
        "concurrency_summary": concurrency_summary,
        "edge_confidence": edge_confidence,
    }
    if build_info:
        pack["build_config"] = build_info

    # Add community data to context pack
    comm_path = os.path.join(outdir, ".code2database_communities.json")
    if os.path.exists(comm_path):
        comm_data = json.loads(Path(comm_path).read_text(encoding="utf-8"))
        pack["community_map"] = {
            c["id"]: {"label": c.get("label", ""),
                      "size": c.get("symbol_count", 0),
                      "cohesion": c.get("cohesion", 0),
                      "keywords": c.get("keywords", [])}
            for c in comm_data.get("communities", [])
        }

    # Add process data to context pack
    proc_path = os.path.join(outdir, ".code2database_processes.json")
    if os.path.exists(proc_path):
        proc_data = json.loads(Path(proc_path).read_text(encoding="utf-8"))
        pack["execution_processes"] = [
            {"entry": p.get("entry_name", ""), "label": p.get("label", ""),
             "steps": p.get("steps", [])[:10], "score": p.get("entry_score", 0),
             "cross_community": p.get("communities_crossed", 0)}
            for p in proc_data.get("processes", [])[:15]
        ]

    # Hub functions: top betweenness centrality nodes
    hub_functions = _compute_hub_functions(G, top_n=10)
    pack["hub_functions"] = hub_functions

    # Cross-domain hotspots
    pack["cross_domain_hotspots"] = _compute_cross_domain_hotspots(G)

    # Write tiers of context pack
    from _builder.token_budget import estimate_tokens, budget_pack

    # Build micro pack (~200 tokens)
    micro_pack = _build_micro_pack(pack, api_entries, G)
    micro_path = os.path.join(outdir, ".code2database_context_pack_micro.json")
    with open(micro_path, "w", encoding="utf-8") as f:
        json.dump(micro_pack, f, ensure_ascii=False, separators=(',', ':'))

    # Lite: ~500 tokens — compact project_summary + top domains + top API names
    # Truncate api_entries and domain_map for lite tier
    top_api_names = [a["name"] for a in api_entries[:20]]
    top_thread_names = [t["name"] for t in thread_entries[:10]]
    top_callback_names = [c["name"] for c in callback_entries[:10]]

    # Top domains by function count (exclude external domains from project domain ranking)
    project_domain_items = [(d, v) for d, v in domain_map_out.items()
                             if not d.startswith("external_") and d != "external"]
    ext_domain_items = [(d, v) for d, v in domain_map_out.items()
                         if d.startswith("external_") or d == "external"]
    domain_sorted = sorted(project_domain_items,
                           key=lambda x: x[1]["apis"] + x[1]["internal"], reverse=True)
    top_domains = {d: domain_map_out[d] for d, _ in domain_sorted[:15]}

    # Lite: adaptive size based on project scale
    # Small (<500 funcs): ~500t, Medium (500-5K): ~1000t, Large (5K-20K): ~2000t, XL (>20K): ~3000t
    total_funcs = pack["project_summary"]["total_functions"]
    if total_funcs < 500:
        max_apis, max_domains, max_catalog = len(api_entries), len(domain_map_out), len(api_entries)
    else:
        max_apis, max_domains, max_catalog = 20, 15, 30

    # Compute architecture one-line description from domain/hub/entry data
    top_hub_names = [h["name"] for h in hub_functions[:3]]
    top_api_names_full = [a["name"] for a in api_entries[:5]]
    deep_domain_names = deep_domains[:5] if deep_domains else []
    arch_desc = (f"Project with {total_funcs} functions across {pack['project_summary']['total_domains']} domains. "
                 f"Key hubs: {', '.join(top_hub_names[:3])}. "
                 f"Top APIs: {', '.join(top_api_names_full[:3])}.")
    if deep_domain_names:
        arch_desc += f" Deep domains: {', '.join(deep_domain_names[:3])}."

    # Compute top 3 core data flows (API_entry → hub → endpoint paths)
    # Skip for very large graphs (>50K nodes) — shortest_path is too expensive
    core_flows = []
    if G.number_of_nodes() < 50000:
        from _builder.utils import _make_call_graph
        _pack_call_G = _make_call_graph(G)
        for api_entry in api_entries[:10]:
            api_id = api_entry["id"]
            if api_id not in G:
                continue
            for hub in hub_functions[:3]:
                hub_id = hub.get("id", "")
                if hub_id not in G or hub_id == api_id:
                    continue
                try:
                    path = nx.shortest_path(_pack_call_G, api_id, hub_id)
                    if len(path) >= 2:
                        # Continue from hub to any endpoint
                        for ep in endpoint_entries[:20]:
                            ep_id = ep["id"]
                            if ep_id not in G or ep_id == hub_id:
                                continue
                            try:
                                path2 = nx.shortest_path(_pack_call_G, hub_id, ep_id)
                                full_path = path + path2[1:]
                                flow_str = " → ".join(
                                    G.nodes[n].get("name", n) for n in full_path
                                    if not G.nodes[n].get("is_empty", False))
                                core_flows.append(flow_str)
                                if len(core_flows) >= 3:
                                    break
                            except (nx.NetworkXNoPath, nx.NodeNotFound):
                                continue
                        if len(core_flows) >= 3:
                            break
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
            if len(core_flows) >= 3:
                break
        del _pack_call_G
    else:
        # For large graphs, use a lightweight heuristic: just list hub names
        core_flows = [f"{h['name']} (hub, {h['callers_from_domains']} cross-domain callers)"
                      for h in hub_functions[:3]]

    lite_summary = {
        "source_root": pack["project_summary"].get("source_root", ""),
        "architecture": arch_desc,
        "core_data_flows": core_flows[:3],
        "total_functions": total_funcs,
        "total_domains": pack["project_summary"]["total_domains"],
        "total_nodes": pack["project_summary"]["total_nodes"],
        "total_edges": pack["project_summary"]["total_edges"],
        "api_entries": [a["name"] for a in api_entries[:max_apis]],
        "thread_entries": [t["name"] for t in thread_entries[:10]],
        "callback_entries": [c["name"] for c in callback_entries[:10]],
        "shallow_domains": shallow_domains[:10],
        "external_domains": external_domains[:10],
    }

    top_domains_lite = {d: {"apis": domain_map_out[d]["apis"],
                            "internal": domain_map_out[d]["internal"],
                            "ratio": domain_map_out[d]["ratio"]}
                        for d, _ in domain_sorted[:max_domains]}

    lite_pack = {
        "project_summary": lite_summary,
        "domain_map": top_domains_lite,
        "external_domains": {d: {"apis": v["apis"], "internal": v["internal"],
                                  "endpoints": v["endpoints"]}
                             for d, v in ext_domain_items[:10]},
        "api_catalog": [{"n": a["name"], "d": a["domain"]}
                        for a in api_entries[:max_catalog]],
    }
    lite_pack["_token_count"] = estimate_tokens(json.dumps(lite_pack, ensure_ascii=False, separators=(',', ':')))
    lite_path = os.path.join(outdir, ".code2database_context_pack_lite.json")
    Path(lite_path).write_text(
        json.dumps(lite_pack, ensure_ascii=False, separators=(',', ':')) + "\n", encoding="utf-8")

    # Standard: incremental over lite — only adds fields not in lite
    # Target: <5000 tokens
    std_pack = {}
    # Add domains not covered by lite (from position max_domains onward, excluding external)
    lite_domain_set = set(d for d, _ in domain_sorted[:max_domains])
    std_pack["extra_domains"] = {d: domain_map_out[d] for d, _ in domain_sorted
                                  if d not in lite_domain_set}
    # Add external domains not covered by lite
    lite_ext_set = set(d for d, _ in ext_domain_items[:10])
    std_pack["extra_external_domains"] = {d: domain_map_out[d] for d, v in ext_domain_items
                                           if d not in lite_ext_set}
    # Truncate execution_processes: keep top 10 by entry score, truncate steps to 5
    exec_procs = pack.get("execution_processes", [])
    truncated_procs = []
    for p in exec_procs[:10]:
        tp = dict(p)
        if len(tp.get("steps", [])) > 5:
            tp["steps"] = tp["steps"][:5]
            tp["steps_truncated"] = True
        truncated_procs.append(tp)
    std_pack["execution_processes"] = truncated_procs
    std_pack["concurrency_summary"] = {
        "spawn_points": concurrency_summary["spawn_points"],
        "concurrent_windows": concurrency_summary["concurrent_windows"][:5],
    }
    # Community map: top 10 by size
    if pack.get("community_map"):
        comm_sorted = sorted(pack["community_map"].items(),
                             key=lambda x: x[1].get("size", 0), reverse=True)[:10]
        std_pack["community_map"] = dict(comm_sorted)
    std_pack["hub_functions"] = hub_functions[:5]
    std_pack["cross_domain_hotspots"] = pack.get("cross_domain_hotspots", [])[:5]
    std_pack["_token_count"] = estimate_tokens(json.dumps(std_pack, ensure_ascii=False))
    # If still too large (>5000 tokens), truncate further
    if std_pack["_token_count"] > 5000:
        std_pack["extra_domains"] = dict(list(std_pack["extra_domains"].items())[:20])
        std_pack["execution_processes"] = std_pack["execution_processes"][:5]
        std_pack["concurrency_summary"]["concurrent_windows"] = std_pack["concurrency_summary"]["concurrent_windows"][:3]
        std_pack["_token_count"] = estimate_tokens(json.dumps(std_pack, ensure_ascii=False))
    std_pack["_incremental_over"] = "lite"  # marks this as delta
    std_path = os.path.join(outdir, ".code2database_context_pack_standard.json")
    Path(std_path).write_text(
        json.dumps(std_pack, ensure_ascii=False, separators=(',', ':')) + "\n", encoding="utf-8")

    # Full: everything (original), but with truncated api_catalog to keep size manageable
    # Sort api_entries by entry score (if available) and keep top 100
    if len(pack.get("api_catalog", [])) > 100:
        # api_entries in the pack are the full list; sort by domain then name for consistency
        pack["api_catalog"] = sorted(pack["api_catalog"],
                                     key=lambda a: (a.get("domain", ""), a.get("name", "")))[:100]
        # Update the summary api_entries count
        if "project_summary" in pack:
            pack["project_summary"]["api_entries"] = [a["name"] for a in pack["api_catalog"]]
    # Use streaming write for the full pack to avoid double-serialization OOM
    pack_path = os.path.join(outdir, ".code2database_context_pack.json")
    with open(pack_path, "w", encoding="utf-8") as _pf:
        json.dump(pack, _pf, ensure_ascii=False, separators=(',', ':'))
        _pf.write("\n")
    # Estimate tokens from file size instead of re-serializing
    _pack_file_size = os.path.getsize(pack_path)
    pack["_token_count"] = _pack_file_size // 4  # rough: ~4 chars/token

    # Generate human-readable Markdown lite pack
    _write_context_pack_lite_md(outdir, lite_pack)

    # Generate human-readable Markdown micro pack
    _write_context_pack_micro_md(outdir, micro_pack)

    # Phase 3: merge memory + knowledge packs into context_pack so the
    # agent gets all three layers in one shot. Previously these were
    # generated as separate .memory_pack_lite.json and
    # .knowledge_pack_lite.json files that the agent had to fetch
    # independently. Now they're embedded as `memory_summary` and
    # `knowledge_summary` keys in the main context_pack.
    try:
        mem_pack_path = os.path.join(outdir, ".memory_pack_lite.json")
        if os.path.exists(mem_pack_path):
            mem_data = json.loads(Path(mem_pack_path).read_text(encoding="utf-8"))
            pack["memory_summary"] = {
                "top_questions": mem_data.get("top_questions", [])[:5],
                "hot_memories": mem_data.get("hot_memories", [])[:5],
            }
    except Exception:
        pass
    try:
        know_pack_path = os.path.join(outdir, ".knowledge_pack_lite.json")
        if os.path.exists(know_pack_path):
            know_data = json.loads(Path(know_pack_path).read_text(encoding="utf-8"))
            pack["knowledge_summary"] = {
                "files": know_data.get("files", [])[:10],
                "topics": know_data.get("topics", [])[:20],
                "architecture_summary": (know_data.get("architecture_summary", "")
                                         or "")[:500],
            }
    except Exception:
        pass

    # Generate REVIEW_CHECKLIST.md
    _write_review_checklist(outdir, G)

    return pack_path


def _build_micro_pack(pack, api_entries, G):
    """Build ultra-compact micro context pack (~200 tokens)."""
    project_summary = pack.get("project_summary", {})

    # Top 3 domains by function count (exclude external domains)
    domain_map = pack.get("domain_map", {})
    project_domain_items = [(d, v) for d, v in domain_map.items()
                             if not d.startswith("external_") and d != "external"]
    top3_domains = sorted(project_domain_items, key=lambda x: x[1].get("apis", 0) + x[1].get("internal", 0), reverse=True)[:3]

    # Top 3 API names (filter out test functions)
    _test_re = re.compile(r'^(test_|mock_|stub_|bench_|generateTest)', re.IGNORECASE)
    real_apis = [a for a in api_entries if not _test_re.match(a.get("name", ""))]
    top3_apis = [a.get("name", "") for a in real_apis[:3]]

    # Architecture patterns
    patterns = _derive_architecture_patterns(project_summary)

    micro = {
        "project": (project_summary.get("source_root") or "").rstrip("/").split("/")[-1] or "unknown",
        "arch": project_summary.get("architecture", ""),
        "patterns": patterns,
        "stats": {
            "functions": project_summary.get("total_functions", 0),
            "domains": project_summary.get("total_domains", 0),
            "nodes": project_summary.get("total_nodes", 0),
            "edges": project_summary.get("total_edges", 0),
        },
        "top_domains": [d[0] for d in top3_domains],
        "top_apis": top3_apis,
    }

    micro["_token_count"] = estimate_tokens(json.dumps(micro, ensure_ascii=False, separators=(',', ':')))
    return micro


def _write_context_pack_micro_md(outdir, micro_pack):
    """Write ultra-compact micro context pack as Markdown."""
    md_path = os.path.join(outdir, ".code2database_context_pack_micro.md")
    lines = [
        f"# Context Pack (Micro) — {micro_pack.get('project', '')}",
        "",
        f"**Architecture**: {micro_pack.get('arch', '')}",
        f"**Patterns**: {', '.join(micro_pack.get('patterns', []))}",
        f"**Stats**: {micro_pack['stats']['functions']} functions, "
        f"{micro_pack['stats']['domains']} domains, "
        f"{micro_pack['stats']['nodes']} nodes, "
        f"{micro_pack['stats']['edges']} edges",
        f"**Top domains**: {', '.join(micro_pack.get('top_domains', []))}",
        f"**Top APIs**: {', '.join(micro_pack.get('top_apis', []))}",
        "",
        f"<!-- ~{micro_pack.get('_token_count', 0)} tokens -->",
    ]
    Path(md_path).write_text("\n".join(lines), encoding="utf-8")


def _derive_architecture_patterns(summary: dict) -> list:
    """Derive high-level architecture pattern keywords from summary data.

    Returns a list of pattern descriptors like:
    - "event-driven" (many callbacks)
    - "threaded" (many thread entries)
    - "deep-callback-chains" (callbacks > APIs * 0.3)
    - "api-heavy" (API ratio > 0.5)
    - "deep-hierarchy" (many domains, low API ratio)
    - "monolithic" (1-2 domains)
    - "plugin-architecture" (build config with many macros)
    """
    patterns = []
    total_funcs = summary.get('total_functions', 0)
    api_count = len(summary.get('api_entries', []))
    thread_count = len(summary.get('thread_entries', []))
    callback_count = len(summary.get('callback_entries', []))
    domain_count = summary.get('total_domains', 0)
    shallow_domains = summary.get('shallow_domains', [])
    deep_domains = summary.get('deep_domains', [])

    if total_funcs == 0:
        return patterns

    api_ratio = api_count / total_funcs if total_funcs > 0 else 0

    # Architecture patterns
    if callback_count > api_count * 0.3:
        patterns.append("callback-driven")
    if thread_count > 5:
        patterns.append("multi-threaded")
    if api_ratio > 0.5:
        patterns.append("api-heavy")
    elif api_ratio < 0.15 and total_funcs > 50:
        patterns.append("implementation-heavy")
    if domain_count <= 2:
        patterns.append("monolithic")
    elif domain_count >= 8:
        patterns.append("modular")
    if len(shallow_domains) > len(deep_domains) and len(shallow_domains) > 2:
        patterns.append("shallow-api-surface")
    if len(deep_domains) > len(shallow_domains) and len(deep_domains) > 2:
        patterns.append("deep-internal-logic")

    return patterns[:5]  # Cap at 5 patterns


def _write_context_pack_lite_md(outdir: str, lite_pack: dict):
    """Generate .code2database_context_pack_lite.md — human-readable Markdown version.

    Structure:
      1. Project overview (auto-derived from source root + stats)
      2. Architecture patterns (keyword summary)
      3. Key API entries
      4. Key thread/callback entries
      5. Domain map
    """
    lines = ["# Context Pack (Lite)\n"]

    summary = lite_pack.get("project_summary", {})
    source_root = summary.get("source_root", "")

    # Project overview — use basename for readability
    project_name = os.path.basename(source_root) if source_root else "project"

    # Project overview — 1-2 sentence natural language description
    total_funcs = summary.get('total_functions', 0)
    total_domains = summary.get('total_domains', 0)
    total_nodes = summary.get('total_nodes', 0)
    total_edges = summary.get('total_edges', 0)
    arch_desc = summary.get('architecture', '')
    lines.append(f"**Project**: `{project_name}` — "
                 f"{total_funcs} functions across {total_domains} domains "
                 f"({total_nodes} nodes, {total_edges} edges).\n")
    if arch_desc:
        lines.append(f"**Architecture**: {arch_desc}\n")

    # Core data flows
    core_flows = summary.get('core_data_flows', [])
    if core_flows:
        lines.append("## Core Data Flows\n")
        for i, flow in enumerate(core_flows, 1):
            lines.append(f"{i}. `{flow}`")
        lines.append("")

    # Architecture patterns — derived from domain structure and labels
    patterns = _derive_architecture_patterns(summary)
    if patterns:
        lines.append("**Architecture Patterns**: " + " | ".join(patterns) + "\n")

    # Stats line
    lines.append(f"**Stats**: {total_funcs} functions | {total_domains} domains | "
                 f"{total_nodes} nodes | {total_edges} edges\n")

    apis = summary.get("api_entries", [])
    if apis:
        lines.append("## API Entries\n")
        for api in apis[:20]:
            lines.append(f"- `{api}`")
        if len(apis) > 20:
            lines.append(f"- ... and {len(apis) - 20} more")
        lines.append("")

    threads = summary.get("thread_entries", [])
    if threads:
        lines.append("## Thread Entries\n")
        for t in threads[:10]:
            lines.append(f"- `{t}`")
        lines.append("")

    callbacks = summary.get("callback_entries", [])
    if callbacks:
        lines.append("## Callback Entries\n")
        for c in callbacks[:10]:
            lines.append(f"- `{c}`")
        lines.append("")

    domain_map = lite_pack.get("domain_map", {})
    if domain_map:
        lines.append("## Domain Map\n")
        lines.append("| Domain | APIs | Internal | Ratio |")
        lines.append("|--------|------|----------|-------|")
        for dom in sorted(domain_map.keys()):
            d = domain_map[dom]
            lines.append(f"| {dom} | {d.get('apis', 0)} | {d.get('internal', 0)} | {d.get('ratio', 0):.2f} |")
        total_dom = summary.get('total_domains', len(domain_map))
        if total_dom > len(domain_map):
            lines.append(f"| ... | | | *{total_dom - len(domain_map)} more domains* |")
        lines.append("")

    lines.append(f"*Token count: ~{lite_pack.get('_token_count', '?')}*\n")
    Path(os.path.join(outdir, ".code2database_context_pack_lite.md")).write_text(
        "\n".join(lines), encoding="utf-8")


def _truncate_desc(desc: str, max_len: int = 120) -> str:
    """Collapse whitespace and truncate on a word boundary for table cells."""
    if not desc:
        return ""
    flat = " ".join(str(desc).split())
    if len(flat) <= max_len:
        return flat
    cut = flat.rfind(" ", 0, max_len)
    if cut < 40:
        cut = max_len
    return flat[:cut].rstrip() + "…"


def _write_review_checklist(outdir: str, G: nx.DiGraph):
    """Generate REVIEW_CHECKLIST.md listing all LLM-filled nodes for human verification.

    Includes YAML frontmatter with review statistics and per-item status markers.
    Organized by domain for easier human review.
    """
    from datetime import datetime

    try:
        from _builder.auto_enhance import _is_likely_builtin
    except ImportError:
        def _is_likely_builtin(name):
            return False

    def _is_external_placeholder(ndata):
        """Filter auto-created external/builtin callee placeholders.

        These have attrs.external=True or are common Python builtin method
        names (set, get, append, ...) with no byte range — they're not real
        project functions and would pollute the review checklist with noise.
        """
        attrs = ndata.get("attrs", {}) or {}
        if attrs.get("external") is True or ndata.get("external") is True:
            return True
        name = ndata.get("name", "")
        if _is_likely_builtin(name):
            return True
        # Skip nodes with no byte range AND no source_file (likely external)
        if not ndata.get("source_file") and not ndata.get("byte_start", 0):
            return True
        return False

    # Collect LLM-filled nodes
    llm_nodes = []
    heuristic_nodes = []
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        if _is_external_placeholder(ndata):
            continue
        desc = ndata.get("semantic_desc", "")
        # Read supplement_meta first to determine the actual source.
        sup_meta = ndata.get("_supplement_meta", {}) or {}
        sem_meta = sup_meta.get("semantic_desc_supplemented", {}) or {}
        sup_source = sem_meta.get("source", "")
        sup_desc = ndata.get("semantic_desc_supplemented", "")
        # Only treat as LLM-filled if there's an explicit semantic_source
        # indicating LLM. Otherwise, default to checking supplement_meta.
        explicit_source = ndata.get("semantic_source", "")
        if explicit_source in ("llm", "inferred", "plugin"):
            source = explicit_source
        elif sup_source == "heuristic":
            source = "heuristic"
        else:
            source = ""
        if desc and source in ("llm", "inferred", "plugin"):
            llm_nodes.append({
                "name": ndata.get("name", ""),
                "domain": ndata.get("domain", ""),
                "location": f"{ndata.get('source_file', '')}:{ndata.get('line', 0)}",
                "source": source,
                "desc": _truncate_desc(desc, 120),
            })
        elif sup_desc and sup_source == "heuristic":
            heuristic_nodes.append({
                "name": ndata.get("name", ""),
                "domain": ndata.get("domain", ""),
                "location": f"{ndata.get('source_file', '')}:{ndata.get('line', 0)}",
                "source": "heuristic",
                "desc": _truncate_desc(sup_desc, 120),
            })

    # Also collect nodes missing descriptions that need LLM filling
    # Include API_entry and hub functions
    missing_nodes = []
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        desc = (ndata.get("semantic_desc", "")
                or ndata.get("external_desc", "")
                or ndata.get("semantic_desc_supplemented", ""))
        labels = ndata.get("labels", [])
        if not desc and ("API_entry" in labels or "hub" in labels):
            entry_score = ndata.get("entry_score", 0)
            missing_nodes.append({
                "name": ndata.get("name", ""),
                "domain": ndata.get("domain", ""),
                "location": f"{ndata.get('source_file', '')}:{ndata.get('line', 0)}",
                "reason": "API entry without description" if "API_entry" in labels else "Hub without description",
                "entry_score": entry_score,
            })

    # Sort by entry_score and limit to top 200
    missing_nodes.sort(key=lambda x: -x.get("entry_score", 0))
    total_missing = len(missing_nodes)
    missing_nodes = missing_nodes[:200]

    # YAML frontmatter
    frontmatter_lines = [
        "---",
        f"generated: '{datetime.now().strftime('%Y-%m-%d %H:%M')}'",
        f"llm_filled_count: {len(llm_nodes)}",
        f"heuristic_filled_count: {len(heuristic_nodes)}",
        f"missing_desc_count: {total_missing}",
        f"total_review_items: {len(llm_nodes) + len(heuristic_nodes) + len(missing_nodes)}",
        "---",
        "",
    ]

    lines = frontmatter_lines + [
        "# Review Checklist\n",
        f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n",
    ]

    # Section 1: LLM-filled nodes needing verification — grouped by domain
    if llm_nodes:
        lines.append("## LLM-Filled Descriptions (verify accuracy)\n")
        # Group by domain
        llm_by_domain = defaultdict(list)
        for node in llm_nodes:
            llm_by_domain[node["domain"]].append(node)
        for dom in sorted(llm_by_domain.keys()):
            lines.append(f"### {dom}\n")
            lines.append("| # | Status | Function | Location | Confidence | Description |")
            lines.append("|---|--------|----------|----------|------------|-------------|")
            for i, node in enumerate(llm_by_domain[dom], 1):
                conf = "inferred" if node["source"] == "inferred" else "LLM"
                short_desc = node["desc"].replace("|", "\\|")
                lines.append(f"| {i} | ⬜ | {node['name']} | {node['location']} | {conf} | {short_desc} |")
            lines.append("")

    # Section 1.5: Heuristic-filled nodes (rule-based, INFERRED confidence)
    if heuristic_nodes:
        lines.append("## Heuristic-Filled Descriptions (rule-based, review for upgrade)\n")
        lines.append(f"> Showing all {len(heuristic_nodes)} items. These were filled by "
                     "`heuristic-enhance` — rule-based, no LLM. Confidence=INFERRED.\n")
        heuristic_by_domain = defaultdict(list)
        for node in heuristic_nodes:
            heuristic_by_domain[node["domain"]].append(node)
        for dom in sorted(heuristic_by_domain.keys()):
            dom_nodes = heuristic_by_domain[dom]
            lines.append(f"### {dom} ({len(dom_nodes)} items)\n")
            lines.append("| # | Status | Function | Location | Description |")
            lines.append("|---|--------|----------|----------|-------------|")
            for i, node in enumerate(dom_nodes, 1):
                short_desc = node["desc"].replace("|", "\\|")
                lines.append(f"| {i} | ✅ | {node['name']} | {node['location']} | {short_desc} |")
            lines.append("")

    # Section 2: Missing descriptions needing LLM fill — grouped by domain
    if missing_nodes:
        lines.append("## Missing Descriptions (need LLM annotation)\n")
        if total_missing > 200:
            lines.append(f"> Showing top 200 of {total_missing} items (sorted by entry score)\n")
        # Group by domain
        missing_by_domain = defaultdict(list)
        for node in missing_nodes:
            missing_by_domain[node["domain"]].append(node)
        for dom in sorted(missing_by_domain.keys()):
            dom_nodes = missing_by_domain[dom]
            lines.append(f"### {dom} ({len(dom_nodes)} items)\n")
            lines.append("| # | Status | Function | Location | Reason | Score |")
            lines.append("|---|--------|----------|----------|--------|-------|")
            for i, node in enumerate(dom_nodes, 1):
                lines.append(f"| {i} | ☐ | {node['name']} | {node['location']} | "
                             f"{node['reason']} | {node['entry_score']:.2f} |")
            lines.append("")

    if not llm_nodes and not heuristic_nodes and not missing_nodes:
        lines.append("No items require review. All descriptions are AST-extracted or human-written.\n")
    else:
        total = len(llm_nodes) + len(heuristic_nodes) + len(missing_nodes)
        lines.append(f"**Total review items**: {total} "
                     f"({len(llm_nodes)} LLM-filled, {len(heuristic_nodes)} heuristic-filled, "
                     f"{len(missing_nodes)} missing)\n")

    Path(os.path.join(outdir, "REVIEW_CHECKLIST.md")).write_text(
        "\n".join(lines) + "\n", encoding="utf-8")




def _build_indexes(G: nx.DiGraph, outdir: str):
    """Pre-compute and write index files for fast queries."""
    import sys as _sys

    # 1. Reverse index: callers/callees per node (call edges only)
    # For large graphs, use streaming write to avoid OOM from json.dumps
    _ri_path = os.path.join(outdir, ".code2database_reverse_index.json")
    _node_count = G.number_of_nodes()
    if _node_count > 100000:
        # Streaming write: avoid building entire reverse_index dict in memory
        print(f"[build] Writing reverse index (streaming, {_node_count} nodes)...",
              file=_sys.stderr)
        with open(_ri_path, "w", encoding="utf-8") as _ri_f:
            _ri_f.write('{')
            _first_node = True
            for nid in G.nodes:
                callers = []
                for pred in G.predecessors(nid):
                    ed = G.get_edge_data(pred, nid) or {}
                    if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                        continue
                    callers.append({"id": pred, "name": G.nodes[pred].get("name", ""),
                                    "call_order": ed.get("call_order"),
                                    "call_condition": ed.get("call_condition", ""),
                                    "concurrency": ed.get("concurrency", "")})
                callees = []
                for succ in G.successors(nid):
                    ed = G.get_edge_data(nid, succ) or {}
                    if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                        continue
                    callees.append({"id": succ, "name": G.nodes[succ].get("name", ""),
                                    "call_order": ed.get("call_order"),
                                    "call_condition": ed.get("call_condition", ""),
                                    "concurrency": ed.get("concurrency", "")})
                if not _first_node:
                    _ri_f.write(',')
                _first_node = False
                _ri_f.write(json.dumps(nid, ensure_ascii=False) + ':')
                _ri_f.write(json.dumps({"callers": callers, "callees": callees},
                                       ensure_ascii=False, separators=(',', ':')))
            _ri_f.write('}\n')
    else:
        reverse_index = {}
        for nid in G.nodes:
            callers = []
            for pred in G.predecessors(nid):
                ed = G.get_edge_data(pred, nid) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                callers.append({"id": pred, "name": G.nodes[pred].get("name", ""),
                                "call_order": ed.get("call_order"),
                                "call_condition": ed.get("call_condition", ""),
                                "concurrency": ed.get("concurrency", "")})
            callees = []
            for succ in G.successors(nid):
                ed = G.get_edge_data(nid, succ) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                callees.append({"id": succ, "name": G.nodes[succ].get("name", ""),
                                "call_order": ed.get("call_order"),
                                "call_condition": ed.get("call_condition", ""),
                                "concurrency": ed.get("concurrency", "")})
            reverse_index[nid] = {"callers": callers, "callees": callees}
        Path(_ri_path).write_text(
            json.dumps(reverse_index, ensure_ascii=False) + "\n", encoding="utf-8")

    # 2. Condition index: branch conditions per node
    condition_index = {}
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        branches = []
        for succ in G.successors(nid):
            ed = G.get_edge_data(nid, succ) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            cond = ed.get("call_condition", "")
            if cond:
                # Get condition_vars from the target empty node or from the caller
                succ_nd = G.nodes[succ]
                cvars = succ_nd.get("condition_vars", []) if succ_nd.get("is_empty") else []
                # Also check caller's condition_vars
                if not cvars:
                    cvars = ndata.get("condition_vars", [])
                branches.append({"condition": cond, "target_node": succ,
                                 "target_name": G.nodes[succ].get("name", ""),
                                 "condition_vars": cvars})
        if branches:
            condition_index[nid] = branches
    Path(os.path.join(outdir, ".code2database_condition_index.json")).write_text(
        json.dumps(condition_index, ensure_ascii=False) + "\n", encoding="utf-8")

    # 3. Chains index: API_entry → endpoint paths
    # For large graphs: use shortest_path only (fast), skip all_simple_paths (exponential)
    # For small graphs: use all_simple_paths with strict limits
    api_entries = [nid for nid, d in G.nodes(data=True) if "API_entry" in d.get("labels", [])]
    endpoints = [nid for nid, d in G.nodes(data=True)
                 if "out_end" in d.get("labels", []) or "unknown_end" in d.get("labels", [])]
    chains = []
    seen_chains = set()
    ep_set = set(endpoints)
    # Build call-only subgraph for chain pathfinding (exclude CONTAINS/IMPORTS)
    from _builder.utils import _make_call_graph
    _chains_call_G = _make_call_graph(G)

    def _chain_step(path):
        """Build chain_steps from a path."""
        chain_steps = []
        for i, pnid in enumerate(path):
            pnd = G.nodes[pnid]
            step = {"id": pnid, "name": pnd.get("name", ""),
                    "labels": pnd.get("labels", []),
                    "is_empty": pnd.get("is_empty", False),
                    "condition": pnd.get("condition", "")}
            if i > 0:
                ed = G.get_edge_data(path[i-1], pnid) or {}
                step["call_order"] = ed.get("call_order")
                step["call_condition"] = ed.get("call_condition", "")
            chain_steps.append(step)
        return chain_steps

    n_nodes = G.number_of_nodes()
    if n_nodes < 5000 and len(api_entries) * len(endpoints) < 10000:
        # Small graph: compute all simple paths
        for api_id in api_entries[:50]:  # cap at 50 API entries
            for ep_id in endpoints[:100]:  # cap at 100 endpoints
                try:
                    path_count = 0
                    for path in nx.all_simple_paths(_chains_call_G, api_id, ep_id, cutoff=12):
                        real_nodes = tuple(n for n in path if not G.nodes[n].get("is_empty", False))
                        if real_nodes in seen_chains:
                            continue
                        seen_chains.add(real_nodes)
                        chains.append({"from_api": api_id, "to_endpoint": ep_id,
                                       "length": len(path) - 1, "steps": _chain_step(path)})
                        path_count += 1
                        if path_count >= 5:  # max 5 paths per pair
                            break
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
    else:
        # Large graph: only shortest paths for top API entries.
        # For each api_id we run a SINGLE BFS (nx.predecessor) over the call
        # subgraph and reconstruct paths to endpoints/terminals by walking the
        # predecessor tree. This keeps the cost at O(api_count * (V+E)) instead
        # of O(api_count * endpoint_count * (V+E)) — the latter hangs for hours
        # on kernel-sized graphs (716K nodes).
        api_entries_sorted = sorted(api_entries,
                                    key=lambda x: _chains_call_G.out_degree(x), reverse=True)[:30]
        ep_lookup = set(endpoints[:200])
        for api_id in api_entries_sorted:
            # One BFS from this api_id; cutoff bounds the chain depth we care
            # about (12 hops, matching the small-graph branch's cutoff).
            try:
                pred_map, seen = nx.predecessor(_chains_call_G, api_id,
                                                cutoff=12, return_seen=True)
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                continue

            def _reconstruct(target):
                if target not in pred_map:
                    return None
                path = [target]
                cur = target
                while cur != api_id:
                    preds = pred_map.get(cur)
                    if not preds:
                        return None
                    cur = preds[0]
                    path.append(cur)
                path.reverse()
                return path

            found_ep = False
            for ep_id in endpoints[:200]:
                path = _reconstruct(ep_id)
                if path is None:
                    continue
                real_nodes = tuple(n for n in path if not G.nodes[n].get("is_empty", False))
                if real_nodes in seen_chains:
                    continue
                seen_chains.add(real_nodes)
                chains.append({"from_api": api_id, "to_endpoint": ep_id,
                               "length": len(path) - 1, "steps": _chain_step(path)})
                found_ep = True

            # Partial chains: terminal nodes reachable from this API. We can
            # derive these from the same BFS — terminal means out_degree==0
            # in the call subgraph. No second traversal needed.
            if not found_ep:
                terminal_nodes = [n for n in pred_map
                                  if n != api_id
                                  and n not in ep_lookup
                                  and not G.nodes[n].get("is_empty", False)
                                  and _chains_call_G.out_degree(n) == 0][:5]
                for tnid in terminal_nodes:
                    path = _reconstruct(tnid)
                    if path is None:
                        continue
                    real_nodes = tuple(n for n in path
                                       if not G.nodes[n].get("is_empty", False))
                    if real_nodes in seen_chains:
                        continue
                    seen_chains.add(real_nodes)
                    chains.append({"from_api": api_id, "to_endpoint": tnid,
                                   "length": len(path) - 1, "steps": _chain_step(path)})
    chains_data = {"total_chains": len(chains),
                   "api_entries": len(api_entries),
                   "endpoints": len(endpoints)}
    # Streaming write for large chains data
    _chains_path = os.path.join(outdir, ".code2database_chains.json")
    if len(chains) > 5000:
        with open(_chains_path, "w", encoding="utf-8") as _cf:
            _cf.write('{')
            _cf.write(f'"total_chains": {len(chains)}, ')
            _cf.write(f'"api_entries": {len(api_entries)}, ')
            _cf.write(f'"endpoints": {len(endpoints)}, ')
            _cf.write('"chains": [')
            _first = True
            for c in chains:
                if not _first:
                    _cf.write(',')
                _first = False
                json.dump(c, _cf, ensure_ascii=False, separators=(',', ':'))
            _cf.write(']}\n')
    else:
        chains_data["chains"] = chains
        Path(_chains_path).write_text(
            json.dumps(chains_data, ensure_ascii=False) + "\n", encoding="utf-8")

    # Lite chains: top 20 chains with minimal data (<2KB)
    lite_chains = []
    for c in sorted(chains, key=lambda x: -x.get("length", 0))[:20]:
        lite_chains.append({
            "from_api": c.get("from_api", ""),
            "to_endpoint": c.get("to_endpoint", ""),
            "length": c.get("length", 0),
            "path": " → ".join(
                s.get("name", s.get("id", "")) for s in c.get("steps", [])[:8]
                if not s.get("is_empty", False)),
        })
    lite_chains_data = {"total": len(chains), "top_chains": lite_chains}
    Path(os.path.join(outdir, ".code2database_chains_lite.json")).write_text(
        json.dumps(lite_chains_data, ensure_ascii=False, separators=(',', ':')) + "\n",
        encoding="utf-8")

    # 4. Concurrency index: spawn relationships and concurrent groups
    concurrency_index = {"spawn_points": [], "thread_entries": [], "concurrent_groups": []}
    # Find spawn points (nodes that create threads)
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        # Check callee_args for thread spawn patterns
        spawn_info = []
        for ca in ndata.get("callee_args", []):
            ci = ca.get("concurrency_info", {})
            if ci.get("is_spawn") or ci.get("concurrency_type") in ("thread_spawn", "goroutine"):
                target = ci.get("spawn_target", "")
                arg = ci.get("spawn_arg", "")
                spawn_info.append({
                    "callee": ca.get("callee", ""),
                    "spawn_target": target,
                    "spawn_arg": arg,
                    "concurrency_type": ci.get("concurrency_type", ""),
                    "call_order": ca.get("call_order"),
                })
        if spawn_info:
            concurrency_index["spawn_points"].append({
                "node": nid,
                "name": ndata.get("name", ""),
                "spawns": spawn_info,
            })
    # Find thread_processor nodes (functions that run in threads)
    for nid, ndata in G.nodes(data=True):
        if "thread_processor" in ndata.get("labels", []):
            # Find who spawns this function
            spawned_by = []
            for pred in G.predecessors(nid):
                ed = G.get_edge_data(pred, nid) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                if ed.get("concurrency") in ("spawn_target", "thread_spawn", "goroutine"):
                    spawned_by.append({"id": pred, "name": G.nodes[pred].get("name", ""),
                                       "concurrency": ed.get("concurrency", "")})
            # Find the argument passed to this thread function
            spawn_arg = ""
            for sp in concurrency_index["spawn_points"]:
                for s in sp["spawns"]:
                    if s["spawn_target"].lower() in nid.lower() or \
                       s["spawn_target"].lower() in ndata.get("name", "").lower():
                        spawn_arg = s.get("spawn_arg", "")
            concurrency_index["thread_entries"].append({
                "node": nid,
                "name": ndata.get("name", ""),
                "params": ndata.get("params", []),
                "spawned_by": spawned_by,
                "spawn_arg": spawn_arg,
            })
    # Build concurrent groups: for each spawn point, identify which calls run concurrently
    for sp in concurrency_index["spawn_points"]:
        sp_nid = sp["node"]
        sp_nd = G.nodes[sp_nid]
        # Find the spawn_target edge
        for s in sp["spawns"]:
            target_name = s["spawn_target"].lower()
            # The spawned thread runs concurrently with everything after the spawn call
            # Collect calls after the spawn call_order
            spawn_order = s.get("call_order", 0)
            concurrent_calls = []
            for succ in G.successors(sp_nid):
                ed = G.get_edge_data(sp_nid, succ) or {}
                if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                    continue
                succ_order = ed.get("call_order")
                if succ_order is not None and succ_order > spawn_order and \
                   ed.get("concurrency") not in ("spawn_target", "callback"):
                    concurrent_calls.append({
                        "id": succ, "name": G.nodes[succ].get("name", ""),
                        "call_order": succ_order,
                    })
            if concurrent_calls or s.get("spawn_target"):
                # Resolve spawned_thread name to node ID for exact matching
                spawned_thread_id = ""
                target_name_lower = s.get("spawn_target", "").lower()
                for succ in G.successors(sp_nid):
                    ed = G.get_edge_data(sp_nid, succ) or {}
                    if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                        continue
                    if ed.get("concurrency") in ("spawn_target", "callback") and \
                       G.nodes[succ].get("name", "").lower() == target_name_lower:
                        spawned_thread_id = succ
                        break
                concurrency_index["concurrent_groups"].append({
                    "spawn_node": sp_nid,
                    "spawn_name": sp_nd.get("name", ""),
                    "spawn_call_order": spawn_order,
                    "spawned_thread": s.get("spawn_target", ""),
                    "spawned_thread_id": spawned_thread_id,
                    "concurrent_with_thread": concurrent_calls,
                    "concurrency_type": s.get("concurrency_type", ""),
                })
    Path(os.path.join(outdir, ".code2database_concurrency_index.json")).write_text(
        json.dumps(concurrency_index, ensure_ascii=False) + "\n", encoding="utf-8")




def _build_scenarios_file(G: nx.DiGraph, outdir: str, build_info: dict = None):
    """Generate .code2database_scenarios.json — detailed pre-computed execution scenarios.

    For each API_entry + significant enum/const combination, resolves the full call chain
    with pruned dead branches and concurrent windows. More detailed than the summary
    in context_pack — includes step-by-step resolved chains with pruned_branches.
    """
    globals_path = os.path.join(outdir, ".code2database_globals.json")
    globals_map = {}
    if os.path.exists(globals_path):
        # For large globals files (>100MB), skip loading to avoid OOM
        gsize = os.path.getsize(globals_path)
        if gsize > 100_000_000:
            print(f"[scenarios] Skipping globals: {gsize/1e6:.0f}MB too large",
                  file=sys.stderr)
        else:
            gd = json.loads(Path(globals_path).read_text(encoding="utf-8"))
            for enum in gd.get("enums", []):
                for v in enum.get("values", []):
                    member = v["member"]
                    val = v.get("value", member)
                    try:
                        # Coerce numeric strings to int; leave symbolic values as str
                        # so downstream consistency checks compare types uniformly.
                        val = int(val) if str(val).strip().isdigit() else val
                    except (ValueError, TypeError):
                        pass
                    globals_map[member] = val

    api_ids = [nid for nid, d in G.nodes(data=True)
               if "API_entry" in d.get("labels", [])
               and "dead_code" not in d.get("labels", [])]
    # Dead-code function IDs (excluded by build macros)
    dead_ids = {nid for nid, d in G.nodes(data=True)
                if "dead_code" in d.get("labels", [])}
    scenarios = []

    for api_id in api_ids[:30]:
        ndata = G.nodes[api_id]
        # Find condition_vars referencing globals
        relevant_vars = {}
        for cv in ndata.get("condition_vars", []):
            for var in cv.get("vars", []):
                if var in globals_map:
                    relevant_vars[var] = globals_map[var]

        if not relevant_vars:
            # Simple chain, no enum-driven branches
            resolved = _resolve_detailed_chain(G, api_id, {})
            if resolved:
                scenarios.append({
                    "trigger": f"{ndata.get('name', '')}()",
                    "binding": {},
                    "resolved_chain": resolved["steps"],
                    "pruned_branches": resolved["pruned"],
                    "concurrent_window": resolved["concurrent"],
                })
            continue

        # For each relevant variable, try with each value
        for var_name, var_value in relevant_vars.items():
            if isinstance(var_value, int):
                for val in (var_value, 0):  # try true and false
                    binding = {var_name: str(val)}
                    resolved = _resolve_detailed_chain(G, api_id, binding, globals_map)
                    if resolved:
                        scenarios.append({
                            "trigger": f"{ndata.get('name', '')}({var_name}={val})",
                            "binding": binding,
                            "resolved_chain": resolved["steps"],
                            "pruned_branches": resolved["pruned"],
                            "concurrent_window": resolved["concurrent"],
                        })
            else:
                binding = {var_name: str(var_value)}
                resolved = _resolve_detailed_chain(G, api_id, binding, globals_map)
                if resolved:
                    scenarios.append({
                        "trigger": f"{ndata.get('name', '')}({var_name}={var_value})",
                        "binding": binding,
                        "resolved_chain": resolved["steps"],
                        "pruned_branches": resolved["pruned"],
                        "concurrent_window": resolved["concurrent"],
                    })

    scenarios_path = os.path.join(outdir, ".code2database_scenarios.json")
    Path(scenarios_path).write_text(
        json.dumps({"total_scenarios": len(scenarios), "scenarios": scenarios},
                    ensure_ascii=False, separators=(',', ':')) + "\n", encoding="utf-8")




def _build_scenarios_summary_md(G: nx.DiGraph, outdir: str):
    """Generate SCENARIOS_SUMMARY.md — human-readable execution scenario table.

    Linearizes the resolved chain into a single representative path by picking
    the first non-conditional successor at each step (so the displayed path
    reads like a real call chain rather than a DFS fan-out).
    """
    sc_path = os.path.join(outdir, ".code2database_scenarios.json")
    if not os.path.exists(sc_path):
        return
    scenarios_data = json.loads(Path(sc_path).read_text(encoding="utf-8"))
    scenarios = scenarios_data.get("scenarios", []) if isinstance(scenarios_data, dict) else scenarios_data
    if not scenarios:
        return

    def _linearize_chain(chain: list) -> list:
        """Reduce a DFS-fanout chain to a single linear path.

        Strategy: pick the first non-conditional target at each depth, then
        skip duplicate consecutive names (caused by recursive or repeated
        edges). Returns a list of names.
        """
        linear = []
        seen_in_linear = set()
        for step in chain:
            if isinstance(step, dict):
                name = step.get("target", "")
                cond = step.get("condition", "") or ""
            else:
                name = str(step)
                cond = ""
            if not name:
                continue
            # Skip conditional placeholders unless they are the only kind
            # of step we have (then keep one to show the branch).
            if name.startswith("<conditional:"):
                continue
            # Skip consecutive duplicates
            if linear and linear[-1] == name:
                continue
            # Skip cycles
            if name in seen_in_linear:
                continue
            linear.append(name)
            seen_in_linear.add(name)
        return linear

    lines = ["# Execution Scenarios\n"]
    lines.append("| # | Trigger | Path | Concurrent | Pruned Branches |")
    lines.append("|---|---------|------|------------|-----------------|")
    for i, sc in enumerate(scenarios[:30], 1):
        trigger = sc.get("trigger", "")
        chain = sc.get("resolved_chain", [])
        chain_names = _linearize_chain(chain)
        path_str = " → ".join(chain_names[:8])
        if len(chain_names) > 8:
            path_str += " ..."
        cw = sc.get("concurrent_window", [])
        concurrent_names = [w.get("thread_fn", "") for w in cw if w.get("thread_fn")]
        concurrent = ", ".join(concurrent_names)[:40] or "—"
        pruned_items = sc.get("pruned_branches", [])[:3]
        pruned = ", ".join(p.get("condition", str(p))[:20] for p in pruned_items)[:50] or "—"
        lines.append(f"| {i} | {trigger} | {path_str} | {concurrent} | {pruned} |")

    Path(os.path.join(outdir, "SCENARIOS_SUMMARY.md")).write_text(
        "\n".join(lines) + "\n", encoding="utf-8")




def _compute_cross_domain_hotspots(G: nx.DiGraph, top_n: int = 10) -> list:
    """Find domain pairs with the most cross-domain calls."""
    pair_counts = defaultdict(int)
    for u, v, edata in G.edges(data=True):
        if edata.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        u_dom = G.nodes[u].get("domain", "") if u in G else ""
        v_dom = G.nodes[v].get("domain", "") if v in G else ""
        if u_dom and v_dom and u_dom != v_dom:
            pair_counts[(u_dom, v_dom)] += 1
    sorted_pairs = sorted(pair_counts.items(), key=lambda x: x[1], reverse=True)[:top_n]
    return [{"caller_domain": p[0], "callee_domain": p[1], "edge_count": c}
            for p, c in sorted_pairs]




def _compute_data_flow(G: nx.DiGraph, outdir: str) -> dict:
    """Compute data flow index: which params affect which conditions and callees."""
    flow_index = {}
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        if "API_entry" not in ndata.get("labels", []):
            continue
        for p in ndata.get("params", []):
            pname = p["name"]
            ptype = p.get("type", "")
            # Use compound key to avoid collisions when different APIs have same param names
            key = f"{ndata.get('name', '')}.{pname}"
            entry = {"type": ptype, "defined_in": f"{ndata.get('name', '')}(param)",
                      "flows_to_conditions": [], "affects_callees": []}
            for cv in ndata.get("condition_vars", []):
                if pname in cv.get("vars", []):
                    entry["flows_to_conditions"].append(cv["condition"])
            for ca in ndata.get("callee_args", []):
                for arg in ca.get("args", []):
                    if pname in arg.get("value", ""):
                        entry["affects_callees"].append(ca.get("callee", ""))
            if entry["flows_to_conditions"] or entry["affects_callees"]:
                flow_index[key] = entry
    return flow_index




def _compute_hub_functions(G: nx.DiGraph, top_n: int = 10) -> list:
    """Compute hub functions using degree-based heuristic for large graphs.

    For small graphs (<5000 nodes): uses betweenness centrality (accurate).
    For large graphs: uses degree centrality × cross-domain factor (fast).
    Returns list of dicts with 'id', 'name', 'domain', 'betweenness', 'callers_from_domains'.

    Filters out: empty nodes, external-domain nodes, file nodes, builtins
    (Python Py_*, os/sys/io.* etc.), and synthesized stub nodes (no source_file).
    """
    n_nodes = G.number_of_nodes()
    hubs = []

    # Local import to avoid cycle; reuse auto_enhance._is_likely_builtin to
    # filter Python builtins (Py_*, os.path.*, etc.) from hub candidates.
    try:
        from _builder.auto_enhance import _is_likely_builtin
    except Exception:
        def _is_likely_builtin(name: str) -> bool:
            return False

    def _is_hub_filterable(nd: dict, nid) -> bool:
        """Return True if this node should be excluded from hub candidates."""
        if nd.get("is_empty"):
            return True
        if nd.get("domain", "") == "external":
            return True
        if not nd.get("source_file"):
            return True
        if nd.get("node_type") == "file":
            return True
        if "file" in nd.get("labels", []):
            return True
        name = nd.get("name", "") or (str(nid) if nid else "")
        if _is_likely_builtin(name):
            return True
        # Synthesized external stubs (caller/callee without definitions) carry
        # the 'external' attr set by _emit_cgdb_records in scanner base.
        attrs = nd.get("attrs") or {}
        if isinstance(attrs, dict) and attrs.get("external"):
            return True
        return False

    if n_nodes < 5000:
        # Small graph: accurate betweenness centrality on call-only subgraph
        try:
            # Build call-only subgraph for betweenness computation
            from _builder.utils import _make_call_graph
            _hub_call_G = _make_call_graph(G)
            k = min(n_nodes, 200)
            bc = nx.betweenness_centrality(_hub_call_G, normalized=True, k=k)
            sorted_nodes = sorted(bc.items(), key=lambda x: x[1], reverse=True)[:top_n * 5]
            for nid, score in sorted_nodes:
                if score <= 0:
                    continue
                nd = G.nodes[nid]
                if _is_hub_filterable(nd, nid):
                    continue
                hubs.append({
                    "id": nid,
                    "name": nd.get("name", nid),
                    "domain": nd.get("domain", ""),
                    "betweenness": round(score, 4),
                    "callers_from_domains": len({G.nodes[p].get("domain", "")
                                                 for p in G.predecessors(nid)
                                                 if p in G
                                                 and G.nodes[p].get("domain") != nd.get("domain")
                                                 and (G.get_edge_data(p, nid) or {}).get("relation") not in ("CONTAINS", "IMPORTS")}),
                })
                if len(hubs) >= top_n:
                    break
        except Exception:
            pass
    else:
        # Large graph: fast degree-based heuristic
        # Hub score = in_degree × out_degree × cross_domain_factor
        # Pre-compute call-only degrees in a single pass over edges (much faster
        # than iterating predecessors/successors per node with edge_data lookups).
        call_in_deg = Counter()   # nid → incoming call degree
        call_out_deg = Counter()  # nid → outgoing call degree
        # caller_domain_sets[nid] → set of domains that call nid via call edges
        caller_domain_sets = defaultdict(set)

        for u, v, edata in G.edges(data=True):
            if edata.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            call_out_deg[u] += 1
            call_in_deg[v] += 1
            # Track caller domains for cross-domain computation
            u_dom = G.nodes[u].get("domain", "")
            if u_dom:
                caller_domain_sets[v].add(u_dom)

        # Score candidates using pre-computed degrees
        candidates = []
        for nid, nd in G.nodes(data=True):
            if _is_hub_filterable(nd, nid):
                continue
            in_deg = call_in_deg.get(nid, 0)
            out_deg = call_out_deg.get(nid, 0)
            if in_deg == 0 or out_deg == 0:
                continue
            # Cross-domain callers (exclude own domain)
            own_domain = nd.get("domain", "")
            cross_domain = len(caller_domain_sets.get(nid, set()) - {own_domain})
            # Approximate betweenness: degree product × cross-domain boost
            score = (in_deg * out_deg) * (1 + cross_domain * 0.5)
            candidates.append((nid, score, cross_domain))

        candidates.sort(key=lambda x: x[1], reverse=True)
        # Iterate past builtins/filtered nodes that may appear before top_n.
        for nid, score, cross_domain in candidates:
            if len(hubs) >= top_n:
                break
            nd = G.nodes[nid]
            if _is_hub_filterable(nd, nid):
                continue
            # Normalize score to 0-1 range for consistency with betweenness output
            max_score = candidates[0][1] if candidates else 1
            normalized = score / max_score if max_score > 0 else 0
            hubs.append({
                "id": nid,
                "name": nd.get("name", nid),
                "domain": nd.get("domain", ""),
                "betweenness": round(normalized, 4),
                "callers_from_domains": cross_domain,
            })

    return hubs[:top_n]




def _compute_scenarios(G: nx.DiGraph, outdir: str) -> list:
    """Pre-compute execution scenarios for API_entry nodes with enum-driven branches."""
    scenarios = []
    globals_path = os.path.join(outdir, ".code2database_globals.json")
    globals_map = {}
    enum_type_map = {}  # enum_name → {member: value}
    if os.path.exists(globals_path):
        gsize = os.path.getsize(globals_path)
        if gsize > 100_000_000:
            # Large file: use streaming parser to extract only the 'enums' section
            try:
                import ijson
                print(f"[context_pack] Streaming enums from {gsize/1e6:.0f}MB globals.json",
                      file=sys.stderr)
                with open(globals_path, 'rb') as _gf:
                    parser = ijson.parse(_gf)
                    in_enums = False
                    current_enum_name = ""
                    current_enum_vals = {}
                    for prefix, event, value in parser:
                        if prefix == 'enums' and event == 'start_array':
                            in_enums = True
                        elif in_enums and event == 'end_array' and prefix == 'enums':
                            in_enums = False
                            break
                        elif in_enums:
                            if event == 'start_map' and prefix.startswith('enums.item'):
                                current_enum_name = ""
                                current_enum_vals = {}
                            elif event == 'map_key' and prefix == 'enums.item' and value == 'name':
                                pass  # next string will be the name
                            elif (event == 'string' and prefix == 'enums.item.name'):
                                current_enum_name = value
                            elif prefix.startswith('enums.item.values.item'):
                                if event == 'start_map':
                                    pass
                                elif event == 'map_key' and value == 'member':
                                    pass
                                elif event == 'string' and 'member' in prefix:
                                    member = value
                                    current_enum_vals[member] = member
                                    globals_map[member] = member
                                elif event == 'map_key' and value == 'value':
                                    pass
                                elif event in ('number', 'string') and 'value' in prefix:
                                    try:
                                        val = int(value) if isinstance(value, (int, float)) else str(value)
                                    except (ValueError, TypeError):
                                        val = str(value)
                                    # Store the last member's value
                                    if current_enum_vals:
                                        last_member = list(current_enum_vals.keys())[-1]
                                        current_enum_vals[last_member] = val
                                        globals_map[last_member] = val
                            elif event == 'end_map' and prefix.startswith('enums.item'):
                                if current_enum_name and current_enum_vals:
                                    enum_type_map[current_enum_name] = current_enum_vals
                print(f"[context_pack] Loaded {len(enum_type_map)} enums, "
                      f"{len(globals_map)} enum members from stream", file=sys.stderr)
            except ImportError:
                print(f"[context_pack] ijson not available, skipping scenarios for "
                      f"{gsize/1e6:.0f}MB globals.json", file=sys.stderr)
            except Exception as e:
                print(f"[context_pack] Streaming enums failed: {e}, skipping scenarios",
                      file=sys.stderr)
        else:
            gd = json.loads(Path(globals_path).read_text(encoding="utf-8"))
            for enum in gd.get("enums", []):
                enum_name = enum.get("name", "")
                enum_vals = {}
                for v in enum.get("values", []):
                    member = v["member"]
                    val = v.get("value", member)
                    try:
                        val = int(val) if str(val).strip().isdigit() else val
                    except (ValueError, TypeError):
                        pass
                    globals_map[member] = val
                    enum_vals[member] = val
                if enum_name and enum_vals:
                    enum_type_map[enum_name] = enum_vals

    api_ids = [nid for nid, d in G.nodes(data=True) if "API_entry" in d.get("labels", [])]

    for api_id in api_ids[:30]:  # cap for performance
        ndata = G.nodes[api_id]
        # Find condition_vars that reference globals/enums
        relevant_vars = {}  # var_name -> set of possible values
        for cv in ndata.get("condition_vars", []):
            for var in cv.get("vars", []):
                if var in globals_map:
                    relevant_vars[var] = globals_map[var]
                # Also check if var is a param with type matching an enum name
                for p in ndata.get("params", []):
                    if p["name"] == var and p.get("type", "") in enum_type_map:
                        relevant_vars[var] = list(enum_type_map[p["type"]].values())[0]

        if not relevant_vars:
            # No enum-driven branches — just compute the simple chain
            chain = _trace_simple_chain(G, api_id, {})
            if chain:
                scenarios.append({
                    "trigger": f"{ndata.get('name', '')}()",
                    "chain": chain,
                    "condition": "",
                })
            continue

        # For each relevant variable, try resolved chains
        # Simple case: single enum variable with integer values
        for var_name, var_value in relevant_vars.items():
            if isinstance(var_value, int):
                # Try both true and false branches for conditions referencing this var
                bindings_true = {var_name: str(var_value)}
                bindings_false = {var_name: "0"}  # opposite
                chain_t = _trace_simple_chain(G, api_id, bindings_true, globals_map)
                chain_f = _trace_simple_chain(G, api_id, bindings_false, globals_map)
                if chain_t:
                    scenarios.append({
                        "trigger": f"{ndata.get('name', '')}({var_name}={var_value})",
                        "chain": chain_t,
                        "condition": f"{var_name} == {var_value}",
                    })
                if chain_f and chain_f != chain_t:
                    scenarios.append({
                        "trigger": f"{ndata.get('name', '')}({var_name} != {var_value})",
                        "chain": chain_f,
                        "condition": f"{var_name} != {var_value}",
                    })
            else:
                chain = _trace_simple_chain(G, api_id, {var_name: str(var_value)}, globals_map)
                if chain:
                    scenarios.append({
                        "trigger": f"{ndata.get('name', '')}({var_name}={var_value})",
                        "chain": chain,
                        "condition": f"{var_name} = {var_value}",
                    })

    return scenarios


def _generate_mermaid_path_diagram(G: nx.DiGraph, paths: list, title: str = "Critical Paths") -> str:
    """Generate Mermaid flowchart for given paths."""
    import hashlib
    lines = [f"```mermaid", f"flowchart TD"]
    seen = set()
    for path in paths[:5]:
        for i, nid in enumerate(path):
            if nid in seen:
                continue
            seen.add(nid)
            nd = G.nodes[nid]
            name = nd.get("name", nid)
            # Sanitize for mermaid with hash suffix for collision resistance
            safe_id = re.sub(r'[^a-zA-Z0-9_]', '_', nid) + "_" + hashlib.md5(nid.encode()).hexdigest()[:6]
            safe_name = name.replace('"', "'")
            labels = nd.get("labels", [])
            if "API_entry" in labels:
                lines.append(f'    {safe_id}["{safe_name}"]:::api')
            elif "out_end" in labels or "unknown_end" in labels:
                lines.append(f'    {safe_id}("{safe_name}"):::endpoint')
            else:
                lines.append(f'    {safe_id}["{safe_name}"]')
        for i in range(len(path) - 1):
            u_id = re.sub(r'[^a-zA-Z0-9_]', '_', path[i]) + "_" + hashlib.md5(path[i].encode()).hexdigest()[:6]
            v_id = re.sub(r'[^a-zA-Z0-9_]', '_', path[i+1]) + "_" + hashlib.md5(path[i+1].encode()).hexdigest()[:6]
            ed = G.get_edge_data(path[i], path[i+1]) or {}
            cond = ed.get("call_condition", "")
            label = f"|{cond}|" if cond else ""
            lines.append(f"    {u_id} -->{label} {v_id}")
    lines.append("    classDef api fill:#e1f5fe,stroke:#01579b")
    lines.append("    classDef endpoint fill:#fce4ec,stroke:#c62828")
    lines.append("```")
    return "\n".join(lines)


def _classify_endpoint(name: str, domain: str, profile: dict = None,
                       has_source_file: bool = True,
                       source_file: str = "") -> tuple:
    """Classify an endpoint based on naming patterns and domain.

    Args:
        name: Function name to classify.
        domain: Domain string from the graph node.
        profile: Builder config dict from ProfileSchema.to_builder_config().
                 When provided, uses profile's lib_prefix_map instead of
                 hardcoded _EXT_LIB_PREFIXES.

    Returns (type, desc) where type is one of:
    - external_* (categories from profile lib_prefix_map, e.g., external_posix,
      external_openssl, external_lib, etc.),
    - callback, function_pointer, test, internal_private,
      other_internal
    """
    # External library patterns (by name prefix)
    if profile and profile.get("lib_prefix_map"):
        ext_prefixes = profile["lib_prefix_map"]
    else:
        # Universal POSIX/C stdlib prefixes (always available)
        ext_prefixes = {
            'pthread_': 'external_posix',
            'sem_': 'external_posix',
            'epoll_': 'external_posix',
        }

    for prefix, cat in ext_prefixes.items():
        # Case-insensitive matching ONLY for ALL_UPPERCASE prefixes
        # (like SSL_ vs ssl_). Mixed-case prefixes like
        # 'Proj' (C++ namespace) must remain case-sensitive to avoid
        # matching 'proj_' (C function prefix).
        if prefix.isupper() or (prefix.endswith('_') and prefix[:-1].isupper()):
            if name.lower().startswith(prefix.lower()):
                return cat, f"External library function ({prefix[:-1]})"
        else:
            if name.startswith(prefix):
                return cat, f"External library function ({prefix[:-1]})"

    # Auto-classify functions in the scanner's universal skip set.
    # These are standard C/POSIX/library functions that the scanner always skips
    # (e.g., memcpy, pthread_mutex_lock, printf). When they appear as external
    # endpoints (no source file in the project), they should be automatically
    # classified instead of requiring LLM intervention.
    if _SCANNER_SKIP_NAMES and name in _SCANNER_SKIP_NAMES:
        return 'external_lib', 'Standard C/POSIX library function'

    # Auto-classify functions in the profile's skip_names_add list.
    # These are project-specific functions that the profile marks as skip
    # (e.g., kzalloc, spin_lock for Linux kernel). When they appear as external
    # endpoints, they should be automatically classified.
    if profile and name in profile.get("skip_names_add", []):
        return 'external_lib', 'Project-specific library function (profile skip)'

    # Auto-classify tracepoint functions (trace_ prefix = kernel tracepoint infrastructure).
    # These are generated by DECLARE_TRACE/DEFINE_TRACE macros and appear as callees
    # but have no real source in the project — they are false external endpoints.
    if name.startswith('trace_'):
        return 'tracepoint', 'Kernel tracepoint function'

    # C standard library functions (common ones not caught by prefix)
    _C_STD_FUNCS = {
        'strtok', 'strchr', 'strrchr', 'strstr', 'strerror', 'strlen', 'strcpy',
        'strncpy', 'strdup', 'strndup', 'strcmp', 'strncmp', 'strcasecmp', 'strncasecmp',
        'sscanf', 'sprintf', 'snprintf', 'printf', 'fprintf',
        'memcpy', 'memset', 'memmove', 'memcmp',
        'malloc', 'calloc', 'realloc', 'free',
        'atoi', 'atol', 'atof', 'strtol', 'strtoul', 'strtod',
        'qsort', 'bsearch', 'exit', 'abort',
        'fopen', 'fclose', 'fread', 'fwrite', 'fflush', 'fgets',
        'open', 'close', 'read', 'write', 'ioctl',
        'socket', 'bind', 'listen', 'accept', 'connect',
        'send', 'recv', 'sendto', 'recvfrom',
        'setsockopt', 'getsockopt', 'getaddrinfo', 'freeaddrinfo',
        'sigaction', 'signal', 'kill', 'raise',
        'getpid', 'getppid', 'perror',
        'usleep', 'sleep', 'nanosleep',
        'gettimeofday', 'clock_gettime',
        'ntohl', 'ntohs', 'htonl', 'htons', 'inet_ntop', 'inet_pton',
        'isdigit', 'isalpha', 'isalnum', 'isspace', 'isprint',
        'toupper', 'tolower',
        'va_start', 'va_end', 'va_arg', 'va_copy',
        'syslog', 'strcpy_s', 'memcpy_s', 'strcat_s', 'strncat_s',
        'localtime_r', 'gmtime_r', 'asctime_r', 'ctime_r',
        'rand', 'srand',
    }
    if name in _C_STD_FUNCS:
        return 'external_lib', 'C standard library function'

    # Callback patterns with more specific descriptions
    if name.endswith('_cb') or name.endswith('_callback'):
        # Detect callback subtype from naming patterns
        if '_ch_create_' in name or '_ch_destroy_' in name:
            return 'callback', 'Channel lifecycle callback'
        if '_event_' in name:
            return 'callback', 'Event callback'
        if 'rpc_' in name:
            return 'callback', 'RPC callback'
        if 'hotremove' in name or 'hot_remove' in name:
            return 'callback', 'Hot-remove callback'
        return 'callback', 'Callback function'
    # Callback variants: _cb with numeric suffix (_cb3, _cb_1), _cb_ctx, _cb_fun
    if re.match(r'.*_cb\d+$', name) or re.match(r'.*_cb_\d+$', name):
        return 'callback', 'Callback function'
    if name.endswith('_cb_ctx') or name.endswith('_cb_fun') or name.endswith('_cb_func'):
        return 'callback', 'Callback function'
    # Names starting with cb_ or containing _cb_ (not just ending)
    if name.startswith('cb_') or '_cb_' in name:
        return 'callback', 'Callback function'
    if name.endswith('_fn') or name.endswith('_handler'):
        return 'function_pointer', 'Function pointer / handler'
    # Function pointer variants: _fun suffix, _intf (interface) suffix
    if name.endswith('_fun') or name.endswith('_intf'):
        return 'function_pointer', 'Function pointer / handler'
    # Completion/event handler patterns: _cpl (completion), _done, _event, on_*
    if name.endswith('_cpl') or name.endswith('_completion') or name.endswith('_done'):
        return 'callback', 'Completion callback'
    if name.endswith('_event') or name.startswith('on_') or '_on_' in name:
        return 'callback', 'Event handler'
    if name.startswith('handle_') or name.endswith('_handler'):
        return 'function_pointer', 'Event handler'
    # R37: Additional callback patterns
    # _ops callback tables (e.g., tgt_destroy_poll_group_ops)
    if name.endswith('_ops'):
        return 'function_pointer', 'Operation table / vtable'
    # IO operation callbacks (common in bdev/nvme)
    if re.match(r'.*_(read|write|unmap|flush|reset|abort)_cb$', name):
        return 'callback', 'IO completion callback'
    # Init/fini callbacks
    if re.match(r'.*_(init|fini|startup|shutdown|destroy)_cb$', name):
        return 'callback', 'Lifecycle callback'
    # Poller callbacks
    if name.endswith('_poller') or name.endswith('_poll_fn'):
        return 'callback', 'Poller callback'
    # Construct/destruct callbacks - refine with context
    if re.match(r'.*_(construct|destruct|create|delete|remove|add)_cb$', name):
        if 'channel_' in name:
            return 'callback', 'Channel lifecycle callback'
        return 'callback', 'Object lifecycle callback'

    # Test helpers
    if name.startswith('test_') or '_test_' in name.lower() or name.endswith('_test'):
        return 'test', 'Test function'
    # Unit test helpers (e.g., ut_*, expected_*, dummy_*)
    if name.startswith('ut_') or name.startswith('expected_') or name.startswith('dummy_'):
        return 'test', 'Test helper function'

    # Internal private functions (starting with _)
    if name.startswith('_'):
        return 'internal_private', 'Internal private function'

    # Signal handlers
    if name.startswith('sig_') or 'signal_handler' in name:
        return 'internal_private', 'Signal handler'

    # Program entry points: main() in production code; test_entry in test/fuzz/example code
    if name == 'main':
        # Classify main() based on source context
        # Only mark as test_entry when the source is clearly in a test/example/fuzz path.
        # Note: 'app' is NOT treated as test — many C projects (e.g., SPDK) put
        # production executables in app/. Only test/ut/example/fuzz directories
        # are unambiguously non-production.
        _TEST_PATH_SEGMENTS = ('test', 'ut', 'example', 'examples',
                               'fuzz', 'benchmark', 'demo', 'sample',
                               'samples', 'documentation', 'doc',
                               'tools', 'scripts')
        # Check both domain components and source_file path for test indicators
        domain_lower = domain.lower()
        domain_parts = domain_lower.split('.')
        src_lower = source_file.lower().replace("\\", "/")
        src_parts = src_lower.split("/")
        if any(p in _TEST_PATH_SEGMENTS for p in domain_parts) or \
           any(p in _TEST_PATH_SEGMENTS for p in src_parts):
            return 'test_entry', 'Test/example entry point'
        return 'program_entry', 'Program entry point'

    # RPC handler functions (registered via RPC macros)
    if name.startswith('rpc_'):
        return 'rpc_handler', 'RPC handler function'

    # Unresolved callees (no source file) that don't match any known pattern
    # are most likely function pointer parameters — the scanner captured a call
    # through a function pointer parameter but couldn't resolve what it points to.
    if not has_source_file:
        return 'function_pointer', 'Unresolved function pointer call'

    return 'other_internal', ''


def _mark_endpoint_nodes(G: nx.DiGraph, outdir: str, profile: dict = None) -> int:
    """Mark external/unresolved nodes as endpoints with automatic classification.

    Args:
        G: The invocation graph.
        outdir: Output directory for endpoint JSON.
        profile: Builder config dict from ProfileSchema.to_builder_config().

    Nodes with domain='external' or no successors and no source_file
    are classified as endpoints. Returns the count of marked endpoints.
    """
    endpoint_count = 0
    endpoints = []
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        domain = ndata.get("domain", "")
        name = ndata.get("name", nid.split('.')[-1] if '.' in nid else nid)

        # External domain nodes are endpoints (includes "external" and "external_*")
        if domain == "external" or domain.startswith("external_"):
            if "out_end" not in ndata.get("labels", []):
                labels = list(ndata.get("labels", []))
                labels.append("out_end")
                G.nodes[nid]["labels"] = labels
            ep_type, ep_desc = _classify_endpoint(name, domain, profile=profile,
                                                   has_source_file=bool(ndata.get("source_file", "")),
                                                   source_file=ndata.get("source_file", ""))
            endpoint_count += 1
            endpoints.append({"id": nid, "name": name,
                              "domain": domain, "type": ep_type,
                              "desc": ndata.get("external_desc", "") or ep_desc})
            continue

        # Terminal nodes (no successors) that are likely external endpoints
        # Only mark as endpoint if: no source_file (external) OR has explicit
        # external_desc. Internal leaf functions (with source_file, no external_desc)
        # are NOT endpoints — they're just leaves in the invocation graph.
        # Callback functions (_cb, _done, _completion) are internal callbacks,
        # NOT external endpoints — don't mark them as out_end/unknown_end.
        # Use filtered out_degree (call edges only, exclude CONTAINS/IMPORTS)
        call_out_deg = sum(1 for succ in G.successors(nid)
                          if (G.get_edge_data(nid, succ) or {}).get("relation") not in ("CONTAINS", "IMPORTS"))
        if call_out_deg == 0:
            labels = ndata.get("labels", [])
            is_api = "API_entry" in labels
            is_callback = "callback_func" in labels
            is_already_ep = "out_end" in labels or "unknown_end" in labels
            has_ext_desc = bool(ndata.get("external_desc", ""))
            has_src = bool(ndata.get("source_file", ""))
            # Mark as endpoint only if: external (no src) or has external description.
            # Skip callback_func — they are internal callbacks, not external endpoints.
            # Also skip names matching callback patterns even if not labeled callback_func.
            is_callback_pattern = bool(re.match(r'.*(_cb|_cb_\d+|_done|_completion|_cpl|_event)$', name))
            if not is_already_ep and not is_api and not is_callback and not is_callback_pattern:
                if not has_src or has_ext_desc:
                    labels = list(labels)
                    ep_type, ep_desc = _classify_endpoint(name, domain, profile=profile,
                                                           has_source_file=has_src,
                                                           source_file=ndata.get("source_file", ""))
                    if ep_type in ("callback", "function_pointer"):
                        # Don't mark callbacks/function pointers as endpoints
                        continue
                    labels.append("out_end" if ep_type != "other_internal" else "unknown_end")
                    G.nodes[nid]["labels"] = labels
                    endpoint_count += 1
                    endpoints.append({"id": nid, "name": name,
                                      "domain": domain, "type": ep_type,
                                      "desc": ndata.get("external_desc", "") or ep_desc})

    # Internal entry points: program entry (main) and RPC handlers (rpc_*)
    # These are legitimate callgraph entry points even though they have callers
    # (e.g., main called by libc, rpc_* registered by RPC framework).
    # Also applies profile endpoint_rules (e.g., rte_eal_init → program_entry).
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        domain = ndata.get("domain", "")
        name = ndata.get("name", nid.split(".")[-1] if "." in nid else nid)
        labels = ndata.get("labels", [])
        is_already_ep = "out_end" in labels or "unknown_end" in labels or "entry_point" in labels

        # Check profile endpoint_rules first (highest priority)
        ep_type_from_rules = None
        if profile and profile.get("endpoint_rules"):
            for rule in profile["endpoint_rules"]:
                if re.match(rule["pattern"], name):
                    ep_type_from_rules = rule["endpoint_type"]
                    break

        if ep_type_from_rules:
            ep_type = ep_type_from_rules
            ep_desc = f"Profile rule: {ep_type}"
        else:
            ep_type, ep_desc = _classify_endpoint(name, domain, profile=profile,
                                                   has_source_file=bool(ndata.get("source_file", "")),
                                                   source_file=ndata.get("source_file", ""))

        if ep_type in ("program_entry", "rpc_handler", "thread_entry", "test_entry") and not is_already_ep:
            labels = list(labels)
            # test_entry uses its own label so downstream filters can drop
            # test-only entries from API_entry chains; the other entry types
            # keep the generic entry_point tag.
            labels.append("test_entry" if ep_type == "test_entry" else "entry_point")
            G.nodes[nid]["labels"] = labels
            endpoint_count += 1
            endpoints.append({"id": nid, "name": name,
                              "domain": domain, "type": ep_type,
                              "desc": ndata.get("external_desc", "") or ep_desc})

    # High entry_score nodes as endpoint candidates: functions with very high
    # entry scores (top percentile) that aren't already classified as endpoints
    # are likely framework entry points or important dispatch functions.
    # Only applies when the node has no predecessor call edges (it's a root)
    # or has very few callers relative to its callees.
    entry_scores = {}
    for nid, ndata in G.nodes(data=True):
        score = ndata.get("entry_score", 0)
        if score > 0:
            entry_scores[nid] = score
    if entry_scores:
        sorted_scores = sorted(entry_scores.values(), reverse=True)
        if sorted_scores:
            # Top 5% as threshold, minimum score of 5.0
            threshold = max(sorted_scores[max(0, len(sorted_scores) // 20)], 5.0)
            for nid, score in entry_scores.items():
                ndata = G.nodes[nid]
                if ndata.get("is_empty", False):
                    continue
                labels = ndata.get("labels", [])
                is_already_ep = any(l in labels for l in ("out_end", "unknown_end", "entry_point", "API_entry"))
                if is_already_ep:
                    continue
                if score >= threshold:
                    name = ndata.get("name", nid.split(".")[-1] if "." in nid else nid)
                    domain = ndata.get("domain", "")
                    # Only mark if it looks like an entry/init/start function
                    name_lower = name.lower()
                    _ENTRY_PATTERNS = (
                        r'_init$', r'_start$', r'_main$', r'_entry$',
                        r'_launch$', r'_boot$', r'_setup$',
                        r'^main$', r'^run$', r'^start$',
                    )
                    if any(re.match(p, name_lower) for p in _ENTRY_PATTERNS):
                        labels = list(labels)
                        labels.append("entry_point")
                        G.nodes[nid]["labels"] = labels
                        endpoint_count += 1
                        endpoints.append({"id": nid, "name": name,
                                          "domain": domain, "type": "framework_entry",
                                          "desc": f"High entry-score entry point (score={score:.1f})"})

    # Vtable callback endpoints: functions registered in vtables are callback
    # entry points — they are called indirectly through function pointer dispatch.
    # Load vtable registrations and mark the registered functions.
    # The vtables.json format is: {"struct_types": {struct_type: {field: [{func_name, var_name, source_file, condition}]}}}
    vtable_path = os.path.join(outdir, ".code2database_vtables.json")
    if os.path.exists(vtable_path):
        try:
            vtable_data = json.loads(Path(vtable_path).read_text(encoding="utf-8"))
            # Support both old format (vtable_registrations list) and new format (struct_types dict)
            vtable_list = vtable_data.get("vtable_registrations", [])
            struct_types = vtable_data.get("struct_types", {})

            # Build a name-indexed lookup for faster matching
            _name_to_nid = {}
            for nid, ndata in G.nodes(data=True):
                if not ndata.get("is_empty", False):
                    name = ndata.get("name", "")
                    if name:
                        _name_to_nid.setdefault(name, []).append(nid)

            if struct_types:
                # New format: struct_types → {field → [registrations]}
                for struct_type, fields in struct_types.items():
                    for field, regs in fields.items():
                        for reg in regs:
                            func_name = reg.get("func_name", "")
                            if not func_name:
                                continue
                            for target_nid in _name_to_nid.get(func_name, []):
                                if "callback_endpoint" not in G.nodes[target_nid].get("labels", []):
                                    labels = list(G.nodes[target_nid].get("labels", []))
                                    labels.append("callback_endpoint")
                                    G.nodes[target_nid]["labels"] = labels
                                    endpoint_count += 1
                                    endpoints.append({
                                        "id": target_nid,
                                        "name": func_name,
                                        "domain": G.nodes[target_nid].get("domain", ""),
                                        "type": "callback_endpoint",
                                        "desc": f"Vtable callback: {struct_type}.{field}",
                                    })
            elif vtable_list:
                # Legacy format: list of vtable entries with registrations
                for vtable in vtable_list:
                    for reg in vtable.get("registrations", []):
                        func_name = reg.get("func_name", "")
                        if not func_name:
                            continue
                        for target_nid in _name_to_nid.get(func_name, []):
                            if "callback_endpoint" not in G.nodes[target_nid].get("labels", []):
                                labels = list(G.nodes[target_nid].get("labels", []))
                                labels.append("callback_endpoint")
                                G.nodes[target_nid]["labels"] = labels
                                endpoint_count += 1
                                field = reg.get("field", "")
                                struct_type = vtable.get("struct_type", "")
                                endpoints.append({
                                    "id": target_nid,
                                    "name": func_name,
                                    "domain": G.nodes[target_nid].get("domain", ""),
                                    "type": "callback_endpoint",
                                    "desc": f"Vtable callback: {struct_type}.{field}",
                                })
        except (json.JSONDecodeError, IOError):
            pass

    # Heuristic API entry detection: when api_auto_detect is enabled,
    # non-static functions called from a different domain are API entry candidates.
    # This provides zeroconfig API detection for projects without EXPORT_SYMBOL.
    if profile and profile.get("api_auto_detect"):
        _internal_patterns = profile.get("internal_patterns",
            ["_unit_", "_ut_", "_test_", "_perf_", "_verify_", "_example_",
             "_internal", "_priv", "_stub", "_mock"])
        # Project-declared non-API paths (e.g., test/, examples/, app/, scripts/).
        # Functions defined in these paths are never public API even when they
        # have cross-domain callers (test framework macros, etc.).
        _non_api_paths = tuple(
            (profile.get("project_boundaries", {}) or {}).get("non_api_paths", [])
        )
        for nid, ndata in G.nodes(data=True):
            if ndata.get("is_empty", False):
                continue
            labels = ndata.get("labels", [])
            if "API_entry" in labels:
                continue
            if any(l in labels for l in ("callback_func", "test_entry", "program_entry")):
                continue
            name = ndata.get("name", "")
            if not name:
                continue
            if any(p in name.lower() for p in _internal_patterns):
                continue
            if name.startswith("_"):
                continue
            # Skip functions whose source file lives in a non-API path.
            # This catches test/unit/.../main(), examples/foo.c:helper(), etc.
            src = (ndata.get("source_file", "") or "").replace(os.sep, "/")
            if _non_api_paths and any(p in src for p in _non_api_paths):
                continue
            # Check if function has callers from a different domain
            node_domain = ndata.get("domain", "")
            has_cross_domain_caller = False
            for pred in G.predecessors(nid):
                pred_domain = G.nodes[pred].get("domain", "")
                if pred_domain and pred_domain != node_domain:
                    has_cross_domain_caller = True
                    break
            if has_cross_domain_caller:
                labels = list(labels)
                labels.append("API_entry")
                G.nodes[nid]["labels"] = labels
                endpoint_count += 1

    # Write endpoint list
    if endpoints:
        ep_path = os.path.join(outdir, ".code2database_endpoints.json")
        Path(ep_path).write_text(
            json.dumps({"total_endpoints": len(endpoints), "endpoints": endpoints},
                       ensure_ascii=False, separators=(',', ':')) + "\n", encoding="utf-8")
        # Summary of classification
        from collections import Counter
        type_counts = Counter(ep["type"] for ep in endpoints)
        print(f"Endpoints: {len(endpoints)} classified endpoint(s)")
        for t, c in type_counts.most_common():
            print(f"  {t}: {c}")
        print(f"Endpoint list exported to: {ep_path}")

    return endpoint_count
  