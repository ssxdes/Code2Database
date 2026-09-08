"""index_pack.context_packs — split from index_pack.py."""

from _builder.export.indexes import (
    _compute_data_flow, _compute_hub_functions,
    _compute_scenarios, _compute_cross_domain_hotspots,
    _build_scenarios_summary_md, _generate_mermaid_path_diagram,
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
                                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                                continue
                        if len(core_flows) >= 3:
                            break
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
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
    # Generate human-readable Markdown lite pack
    _write_context_pack_lite_md(outdir, lite_pack)

    # Generate human-readable Markdown micro pack
    _write_context_pack_micro_md(outdir, micro_pack)

    # merge memory + knowledge packs into context_pack so the
    # agent gets all three layers in one shot. Previously these were
    # generated as separate .memory_pack_lite.json and
    # .knowledge_pack_lite.json files that the agent had to fetch
    # independently. Now they're embedded as `memory_summary` and
    # `knowledge_summary` keys in the main context_pack.
    # NOTE: this MUST run before the pack is serialized below — the old
    # order wrote the file first, so the merged summaries only ever
    # existed in the in-memory dict and the on-disk context_pack never
    # contained them (the feature was dead on every build).
    # knowledge_summary comes from the project brief
    # (knowledge/brief.json). The old .knowledge_pack_lite.json source
    # was removed with the MD knowledge system — the key had
    # been silently absent from every build since.
    try:
        from _builder.kb.brief import load_brief
        brief = load_brief(outdir)
        if brief is not None:
            pack["knowledge_summary"] = {
                "project": brief.get("project", ""),
                "one_liner": brief.get("one_liner", ""),
                "hard_rules": [hr.get("rule", "") for hr in
                               (brief.get("hard_rules") or [])][:10],
                "modes": [m.get("name", "") for m in
                          (brief.get("modes") or [])][:10],
                "pitfalls": (brief.get("pitfalls") or [])[:5],
            }
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    # memory_summary is generated fresh from memory.db — the
    # old source (.memory_pack_lite.json) is only written by an explicit
    # `manage-memory --action pack`, so it was stale on every build.
    try:
        from _builder.memory.memory_store import MemoryStore
        store = MemoryStore(outdir)
        digest = store.digest(limit=10)
        if digest:
            pack["memory_summary"] = {
                "top_questions": [e["question"][:80]
                                  for e in digest[:5]],
                "hot_memories": [{"id": e["id"], "q": e["question"][:60],
                                  "w": e["weight"]}
                                 for e in digest if e["weight"] > 0.7][:5],
            }
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass

    pack_path = os.path.join(outdir, ".code2database_context_pack.json")
    with open(pack_path, "w", encoding="utf-8") as _pf:
        json.dump(pack, _pf, ensure_ascii=False, separators=(',', ':'))
        _pf.write("\n")
    # Estimate tokens from file size instead of re-serializing
    _pack_file_size = os.path.getsize(pack_path)
    pack["_token_count"] = _pack_file_size // 4  # rough: ~4 chars/token

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
        from _builder.build.auto_enhance import _is_likely_builtin
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




