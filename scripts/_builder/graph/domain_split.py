"""callgraph builder module: domain_split — split from graph_build.py."""

import logging
import os
import json
import sys
import re
import time
from pathlib import Path
from collections import defaultdict, Counter
import networkx as nx
from _builder.graph.streaming_graph import StreamingGraph
from _builder.utils import _resolve_invoked_id
import _builder.utils as _utils


from _builder.graph.sqlite_postprocess import (
    _build_domain_readmes_from_sqlite,
)

def _domain_subdir(domain: str, domain_count: dict, max_per_dir: int = 50) -> str:
    """Compute the subdirectory path under domains/ for a domain.

    Uses hierarchical grouping: domain "x.y.z" tries "x/", then "x/y/",
    then "x/y/z/" as needed based on file count.
    Returns a relative path like "x/" or "x/y/".

    Defensive: an empty domain returns "root/" so os.path.join keeps the
    path relative. Without this, "".split(".") == [""] and "/".join([""])
    == "" produces prefix == "/", which os.path.join treats as absolute.
    """
    if not domain:
        return "root/"
    parts = domain.split(".")
    for depth in range(1, len(parts) + 1):
        prefix = "/".join(parts[:depth]) + "/"
        matching = sum(1 for d in domain_count
                       if ".".join(d.split(".")[:depth]) == ".".join(parts[:depth]))
        if matching <= max_per_dir or depth == len(parts):
            return prefix
    return "/".join(parts) + "/"

def split_by_domain(G: nx.DiGraph, outdir: str, source_root: str = "",
                    max_per_dir: int = 50, build_info: dict = None,
                    profile: dict = None,
                    node_supplements: dict = None):
    """Split the graph into per-domain JSON files with a master navigation file.

    Domain JSON files are organized under a ``domains/`` subdirectory with
    hierarchical grouping.  When a group exceeds *max_per_dir* files, it is
    split into deeper sub-directories based on domain components.

    Single-function domains are merged into their parent domain to reduce
    domain fragmentation (e.g., "lib.storage" stays but "lib.storage.util"
    with 1 function merges into "lib.storage").

    Profile domain_rules can:
      - Tag domains with domain_tag (e.g., mark test domains)
      - Merge domains with merge_to (e.g., app.test-* → app)
      - Label domains with label (e.g., drivers.net.*.base → vendor_sdk)

    node_supplements: optional {nid: {key: value, ...}} dict for LazySQLiteGraph.
      When the graph is a LazySQLiteGraph, G.nodes(data=True) yields fresh dicts
      that are not cached, so modifications (e.g., _supplemented keys from
      apply_heuristic_enhancement_batch) are lost.  Pass them via this parameter
      and they will be merged into the node attrs during per-domain writing.

    Creates:
    - code2database_master.json  (in outdir/)
    - domains/<group>/code2database_domain_<sanitized>.json  (hierarchical)
    """
    _split_start = time.time()

    # --- OPTIMIZATION for LazySQLiteGraph ---
    # Avoid multiple G.nodes(data=True) traversals (each re-executes SQL +
    # json.loads on extra_json for 207K+ nodes, taking ~25-30s each).
    # For LazySQLiteGraph, use _query_node_domains() for lightweight
    # classification and _nodes_data_for_split() for a single cached
    # full-data pass.  This reduces 3-4 full traversals + ~1M individual
    # node lookups to 1 lightweight query + 1 full-data pass.
    _is_lazy = hasattr(G, '_conn') and hasattr(G, '_query_node_domains')

    if _is_lazy:
        # Phase 1: Lightweight classification — no json.loads, no body_text
        _node_info = G._query_node_domains()  # {nid: {domain, is_empty, node_type}}
        _domain_map = {nid: info["domain"] for nid, info in _node_info.items()}
        _func_node_count = sum(
            1 for info in _node_info.values()
            if not info["is_empty"] and info["node_type"] != "file"
        )
        _cached_node_ids = set(_node_info.keys())
    else:
        _node_info = None
        _domain_map = None
        _func_node_count = sum(1 for _, nd in G.nodes(data=True)
                               if not nd.get("is_empty", False) and nd.get("node_type") != "file")
        _cached_node_ids = None

    print(f"[build] Splitting domains... ({_func_node_count} function nodes)",
          file=sys.stderr)
    domains_dir = os.path.join(outdir, "domains")
    # Clean up stale domain files from previous builds
    if os.path.isdir(domains_dir):
        import shutil
        shutil.rmtree(domains_dir)
    os.makedirs(domains_dir, exist_ok=True)

    # Apply profile domain_rules before grouping
    # This must happen BEFORE domain_nodes grouping so the domain
    # assignments are updated on the graph nodes.
    domain_labels = {}  # domain → label (e.g., "vendor_sdk", "core_eal")
    _domain_overrides = {}  # nid → new_domain (for LazySQLiteGraph, where G.nodes[] writes are cache-only)
    if profile and profile.get("domain_rules"):
        # Pre-compile all domain rule regexes ONCE, then do a SINGLE
        # pass over all nodes (Finding 3 + 33: was O(R×N), now O(R+N)).
        _compiled_rules = []
        for rule in profile["domain_rules"]:
            pattern = rule.get("pattern", "")
            if not pattern:
                continue
            _compiled_rules.append((
                re.compile(pattern),
                rule.get("merge_to", ""),
                rule.get("label", ""),
                rule.get("domain_tag", ""),
            ))
        # Single pass: for each node, test all pre-compiled rules
        if _is_lazy:
            for nid, info in _node_info.items():
                domain = info["domain"]
                if not domain or domain == "root":
                    continue
                for pat_re, merge_to, label, domain_tag in _compiled_rules:
                    if pat_re.match(domain):
                        if merge_to:
                            _domain_overrides[nid] = merge_to
                            info["domain"] = merge_to
                            _domain_map[nid] = merge_to
                        if label:
                            effective_domain = merge_to or domain
                            domain_labels[effective_domain] = label
                        if domain_tag:
                            effective_domain = merge_to or domain
                            domain_labels[effective_domain] = domain_tag
                        break  # first matching rule wins
        else:
            for nid, ndata in G.nodes(data=True):
                domain = ndata.get("domain", "")
                if not domain or domain == "root":
                    continue
                for pat_re, merge_to, label, domain_tag in _compiled_rules:
                    if pat_re.match(domain):
                        if merge_to:
                            G.nodes[nid]["domain"] = merge_to
                            ndata["domain"] = merge_to
                        if label:
                            effective_domain = merge_to or domain
                            domain_labels[effective_domain] = label
                        if domain_tag:
                            effective_domain = merge_to or domain
                            domain_labels[effective_domain] = domain_tag
                        break  # first matching rule wins

    # Group nodes by domain. Normalize empty/missing domain to "root" so
    # there is a single canonical bucket for files at source_root and for
    # synthetic nodes (vtables, fn_ptr auto-targets) that were created
    # without an explicit domain. Without this, an empty-string domain
    # produces a broken master['domains'] entry: os.path.join treats any
    # path starting with "/" as absolute, so _domain_subdir("") == "/"
    # collapses the relative path to "/<filename>".
    domain_nodes = defaultdict(list)
    if _is_lazy:
        # Use lightweight _node_info for grouping; full ndata fetched lazily
        # in the per-domain writing phase via _nodes_data_for_split().
        for nid, info in _node_info.items():
            domain = info["domain"]
            domain_nodes[domain].append((nid, None))
    else:
        for nid, ndata in G.nodes(data=True):
            domain = ndata.get("domain") or "root"
            domain_nodes[domain].append((nid, ndata))

    # Merge single-function domains into their parent domain
    # A domain with only 1 non-empty function is too granular
    _MIN_DOMAIN_SIZE = 2  # minimum non-empty functions to form a domain
    domains_to_merge = {}
    for domain in list(domain_nodes.keys()):
        # Count real (non-file) functions — file nodes don't count toward
        # domain size because they are synthetic artifacts from #include
        if _is_lazy:
            # Use _node_info for classification (nd is None in domain_nodes)
            real_func_count = sum(
                1 for nid, _ in domain_nodes[domain]
                if not _node_info[nid]["is_empty"] and _node_info[nid]["node_type"] != "file"
            )
            non_empty_count = sum(
                1 for nid, _ in domain_nodes[domain]
                if not _node_info[nid]["is_empty"]
            )
        else:
            real_func_count = sum(
                1 for _, nd in domain_nodes[domain]
                if not nd.get("is_empty", False) and nd.get("node_type") != "file"
            )
            non_empty_count = sum(1 for _, nd in domain_nodes[domain] if not nd.get("is_empty", False))
        if real_func_count == 0:
            # File-only domain: merge into a connected domain
            # 1. Try predecessors (files that #include this one)
            # 2. Try successors (files this one #includes)
            # 3. Try parent domain by name
            # 4. Fall back to 'root'
            best_parent = None
            for nid, _ndata in domain_nodes[domain]:
                for pred in G.predecessors(nid):
                    if _is_lazy:
                        pred_domain = _domain_map.get(pred, "")
                    elif pred in G:
                        pred_domain = G.nodes[pred].get("domain", "")
                    else:
                        pred_domain = ""
                    if pred_domain and pred_domain != domain:
                        best_parent = pred_domain
                        break
                if best_parent:
                    break
            if not best_parent:
                for nid, _ndata in domain_nodes[domain]:
                    for succ in G.successors(nid):
                        if _is_lazy:
                            succ_domain = _domain_map.get(succ, "")
                        elif succ in G:
                            succ_domain = G.nodes[succ].get("domain", "")
                        else:
                            succ_domain = ""
                        if succ_domain and succ_domain != domain:
                            best_parent = succ_domain
                            break
                    if best_parent:
                        break
            if not best_parent and "." in domain:
                best_parent = domain.rsplit(".", 1)[0]
            if not best_parent:
                best_parent = "root"
            domains_to_merge[domain] = best_parent
        elif non_empty_count < _MIN_DOMAIN_SIZE and "." in domain:
            # Find parent domain (strip last component)
            parent = domain.rsplit(".", 1)[0]
            domains_to_merge[domain] = parent

    if domains_to_merge:
        merged_count = 0
        file_only_count = 0
        for child, parent in domains_to_merge.items():
            # Ensure parent domain exists in domain_nodes
            if parent not in domain_nodes:
                domain_nodes[parent] = []
            # Reassign nodes from child to parent in the graph
            if _is_lazy:
                is_file_only = all(
                    _node_info[nid]["node_type"] == "file" or _node_info[nid]["is_empty"]
                    for nid, _ in domain_nodes[child]
                )
            else:
                is_file_only = all(
                    nd.get("node_type") == "file" or nd.get("is_empty", False)
                    for _, nd in domain_nodes[child]
                )
            if is_file_only:
                file_only_count += 1
            for nid, ndata in domain_nodes[child]:
                if _is_lazy:
                    # Update _node_info and _domain_map (LazySQLiteGraph cache
                    # is write-through but evictable; track overrides reliably)
                    if not _node_info[nid]["is_empty"]:
                        _node_info[nid]["domain"] = parent
                        _domain_map[nid] = parent
                else:
                    if not ndata.get("is_empty", False):
                        G.nodes[nid]["domain"] = parent
                        ndata["domain"] = parent
                domain_nodes[parent].append((nid, ndata))
            del domain_nodes[child]
            merged_count += 1
        print(f"Domain merge: {merged_count} small domain(s) merged into parents "
              f"({file_only_count} file-only, {len(domain_nodes)} domains remaining)")

    # Collect edges per domain (both endpoints in same domain)
    # and cross-domain edges (go into master).
    # Structural edges (CONTAINS, IMPORTS) are separated from call edges
    # so that cross_domain_edges contains only function-call relationships.
    domain_edges = defaultdict(list)
    cross_domain_edges = []
    structural_edges = []
    # Track edge counts by concurrency type for master summary
    _edge_type_counts = Counter()

    _STRUCTURAL_RELATIONS = frozenset({"CONTAINS", "IMPORTS"})

    for u, v, edata in G.edges(data=True):
        relation = edata.get("relation", "")
        if _is_lazy:
            u_domain = _domain_map.get(u, "root")
            v_domain = _domain_map.get(v, "root")
        else:
            u_domain = G.nodes[u].get("domain", "root") if u in G else "root"
            v_domain = G.nodes[v].get("domain", "root") if v in G else "root"
        edge_record = {
            "source": u,
            "target": v,
            "call_order": edata.get("call_order"),
            "call_condition": edata.get("call_condition", ""),
            "concurrency": edata.get("concurrency", ""),
            "confidence": edata.get("confidence", "EXTRACTED"),
            "source_tag": edata.get("source", "ast"),
            "confidence_score": edata.get("confidence_score", 1.0),
        }
        # Include relation field for non-INVOKES edges (CONTAINS, IMPORTS)
        if relation:
            edge_record["relation"] = relation
        if edata.get("import_path"):
            edge_record["import_path"] = edata["import_path"]
        if edata.get("preproc_condition"):
            edge_record["preproc_condition"] = edata["preproc_condition"]
        if not edata.get("preproc_alive", True):
            edge_record["preproc_alive"] = False
        if edata.get("evidence"):
            edge_record["evidence"] = edata["evidence"]

        # Track edge types for master summary (before classification)
        _concurrency = edata.get("concurrency", "") or ""
        _confidence = edata.get("confidence", "EXTRACTED") or "EXTRACTED"
        _edge_type_counts[f"concurrency:{_concurrency}"] += 1
        _edge_type_counts[f"confidence:{_confidence}"] += 1

        # Structural edges (CONTAINS, IMPORTS) go to a separate list
        # so they don't pollute call-graph analysis
        if relation in _STRUCTURAL_RELATIONS:
            structural_edges.append({
                **edge_record,
                "source_domain": u_domain,
                "target_domain": v_domain,
            })
            # Also include in domain_edges for per-domain file output
            if u_domain == v_domain:
                domain_edges[u_domain].append(edge_record)
            continue

        if u_domain == v_domain:
            domain_edges[u_domain].append(edge_record)
        else:
            # NOTE: do NOT filter cross-domain edges by id shape here.
            # Real scanner node ids never contain dots (_make_func_id
            # builds domain.replace('.', '_') + '_' + name), so a
            # historical `'.' not in v` check silently dropped EVERY
            # cross-domain call edge (70% of a real project's edges).
            # Both endpoints of a G edge are always nodes (unresolved
            # callees get auto-created external-domain nodes during
            # edge processing), and dangling edges are removed by the
            # membership filter below (all_node_ids) before writing.
            cross_domain_edges.append({
                **edge_record,
                "source_domain": u_domain,
                "target_domain": v_domain,
            })

    # Pre-compute subdirectory assignments for all domains
    domain_count = {d: len(nodes) for d, nodes in domain_nodes.items()}
    domain_subdirs = {}
    for domain in sorted(domain_nodes.keys()):
        domain_subdirs[domain] = _domain_subdir(domain, domain_count, max_per_dir)

    # For LazySQLiteGraph: fetch full node data in a single pass for per-domain
    # writing.  This avoids 207K+ individual G.nodes[nid] lookups (each is a
    # separate SQL query + json.loads).  The _nodes_data_for_split() method
    # excludes body_text_compressed to save I/O.
    if _is_lazy:
        _full_node_data = G._nodes_data_for_split()  # {nid: ndata}
        # Merge node_supplements into fetched data.  For LazySQLiteGraph,
        # G.nodes(data=True) yields ephemeral dicts (not cached), so
        # _supplemented keys written by apply_heuristic_enhancement_batch
        # are lost unless we merge them back here.
        if node_supplements:
            for nid, supp in node_supplements.items():
                if nid in _full_node_data and supp:
                    _full_node_data[nid].update(supp)
    else:
        _full_node_data = None

    # Write per-domain files
    domain_map = {}
    total_nodes = 0
    total_edges = 0

    for domain in sorted(domain_nodes.keys()):
        nodes = domain_nodes[domain]
        edges = domain_edges.get(domain, [])

        if _is_lazy and _full_node_data is not None:
            # Replace (nid, None) with (nid, ndata) from pre-fetched data
            nodes = [(nid, _full_node_data.get(nid, {})) for nid, _ in nodes]
        nodes.sort(key=lambda x: x[1].get("name", ""))
        edges.sort(key=lambda x: (x.get("call_order") or 999, x.get("source", "")))

        sanitized = domain.replace(".", "_") if domain else "root"
        filename = f"code2database_domain_{sanitized}.json"

        # Compact format: split into summary rows + details + empty nodes
        func_rows = []       # [id, name, source_file, line, labels_json, signature]
        func_details = {}    # id → {body_text, params, local_vars, callee_args, condition_vars, ...}
        empty_rows = []      # [id, condition, parent_id]

        for nid, ndata in nodes:
            is_empty = ndata.get("is_empty", False)
            if is_empty:
                cond = ndata.get("condition", "")
                parent_id = nid.rsplit("__cond_", 1)[0] if "__cond_" in nid else ""
                empty_rows.append([nid, cond, parent_id])
                continue

            name = ndata.get("name", "")
            source_file = ndata.get("source_file", "")
            line = ndata.get("line", 0)
            labels = ndata.get("labels", [])
            labels_src = ndata.get("labels_source", {})
            signature = ndata.get("signature", "")
            location = f"{source_file}:{line}"

            func_rows.append([nid, name, source_file, line,
                              json.dumps(labels) if labels else "",
                              signature])

            # Build compact details — omit empty/default fields
            details = {"location": location}
            if ndata.get("node_type"):
                details["node_type"] = ndata["node_type"]
            if labels_src:
                details["labels_source"] = labels_src
            constraints = ndata.get("api_constraints", "")
            if constraints:
                details["api_constraints"] = constraints
            ext_desc = ndata.get("external_desc", "")
            if ext_desc:
                details["external_desc"] = ext_desc
            sem_desc = ndata.get("semantic_desc", "")
            if sem_desc:
                details["semantic_desc"] = sem_desc
            body = ndata.get("body_text", "")
            if body:
                details["body_text"] = body
            params = ndata.get("params", [])
            if params:
                details["params"] = params
            # local_vars: filter out is_param entries (already in params)
            lvars = ndata.get("local_vars", [])
            body_vars = [v for v in lvars if not v.get("is_param", False)]
            if body_vars:
                details["local_vars"] = body_vars
            # callee_args: omit empty concurrency_info
            callee_args = ndata.get("callee_args", [])
            if callee_args:
                compact_args = []
                for ca in callee_args:
                    compact_ca = {"call_order": ca.get("call_order"),
                                  "callee": ca.get("callee", ""),
                                  "args_snippet": ca.get("args_snippet", "")}
                    if ca.get("args"):
                        compact_ca["args"] = ca["args"]
                    ci = ca.get("concurrency_info", {})
                    if ci and (ci.get("is_spawn") or ci.get("concurrency_type")):
                        compact_ca["concurrency_info"] = ci
                    if ca.get("callback_target"):
                        compact_ca["callback_target"] = ca["callback_target"]
                    compact_args.append(compact_ca)
                details["callee_args"] = compact_args
            cvars = ndata.get("condition_vars", [])
            if cvars:
                details["condition_vars"] = cvars
            if not ndata.get("preproc_alive", True):
                details["preproc_alive"] = False
            # Thread model info
            tm = ndata.get("thread_model")
            if tm:
                details["thread_model"] = tm
            if ndata.get("thread_entry", False):
                details["thread_entry"] = True
            tmi = ndata.get("thread_model_inherited")
            if tmi:
                details["thread_model_inherited"] = tmi
            # State access info
            for sa_key in ("globals_read", "globals_written",
                           "fields_read", "fields_written"):
                sa_val = ndata.get(sa_key, [])
                if sa_val:
                    details[sa_key] = sa_val
            # ASM register-level data flow
            lang = ndata.get("language", "")
            if lang:
                details["language"] = lang
            reg_transfers = ndata.get("reg_transfers", [])
            if reg_transfers:
                details["reg_transfers"] = reg_transfers
            reg_state_final = ndata.get("reg_state_final", {})
            if reg_state_final:
                details["reg_state_final"] = reg_state_final
            # Goto control flow info
            goto_jumps = ndata.get("goto_jumps", [])
            if goto_jumps:
                details["goto_jumps"] = goto_jumps
            goto_labels = ndata.get("goto_labels", [])
            if goto_labels:
                details["goto_labels"] = goto_labels
            # LLM supplement fields (from update-node command) — persist
            # any key with `_supplemented` suffix and the `_supplement_meta`
            # provenance dict so LLM-driven DB updates survive serialization.
            for k, v in ndata.items():
                if k.endswith("_supplemented") and v:
                    details[k] = v
            supp_meta = ndata.get("_supplement_meta")
            if supp_meta:
                details["_supplement_meta"] = supp_meta

            func_details[nid] = details

        # Compact edge format: position-based arrays
        EDGE_FIELDS = ["source", "target", "call_order", "call_condition",
                       "concurrency", "confidence", "source_tag", "confidence_score"]
        compact_edges = []
        for e in edges:
            row = [
                e.get("source", ""),
                e.get("target", ""),
                e.get("call_order"),
                e.get("call_condition", ""),
                e.get("concurrency", ""),
                e.get("confidence", "EXTRACTED"),
                e.get("source_tag", "ast"),
                e.get("confidence_score", 1.0),
            ]
            # Extra fields as dict (sparse)
            extras = {}
            if e.get("relation"):
                extras["rel"] = e["relation"]
            if e.get("import_path"):
                extras["ip"] = e["import_path"]
            if e.get("preproc_condition"):
                extras["pc"] = e["preproc_condition"]
            if not e.get("preproc_alive", True):
                extras["pa"] = False
            if e.get("evidence"):
                extras["ev"] = e["evidence"]
            if e.get("reg_args"):
                extras["reg_args"] = e["reg_args"]
            if extras:
                row.append(extras)
            compact_edges.append(row)

        domain_data = {
            "type": "code2database_domain",
            "format_version": 3,
            "domain": domain,
            "functions": func_rows,
            "function_details": func_details,
            "empty_nodes": empty_rows,
            "edge_fields": EDGE_FIELDS,
            "edges": compact_edges,
        }

        domain_data["functions"].sort(key=lambda x: x[1])  # sort by name
        domain_data["edges"].sort(key=lambda x: (x[2] or 999, x[0]))

        subdir = domain_subdirs[domain]
        rel_path = os.path.join("domains", subdir, filename)
        full_dir = os.path.join(outdir, "domains", subdir)
        os.makedirs(full_dir, exist_ok=True)

        filepath = os.path.join(outdir, rel_path)
        # Safety check: ensure filepath is within outdir
        if not filepath.startswith(outdir):
            filepath = os.path.join(outdir, "domains", sanitized, filename)
            os.makedirs(os.path.join(outdir, "domains", sanitized), exist_ok=True)
        # Use compact JSON (no indent) for large domains to reduce
        # serialization time and file size. indent=2 on 100K+ entries
        # can take minutes; compact mode is 5-10x faster.
        _use_indent = len(func_rows) < 500
        Path(filepath).write_text(
            json.dumps(domain_data, ensure_ascii=False,
                      indent=2 if _use_indent else None,
                      separators=(",", ":") if not _use_indent else None) + "\n",
            encoding="utf-8"
        )

        domain_map[domain] = rel_path
        total_nodes += len(nodes)
        total_edges += len(edges)

    # Remove dangling cross-domain edges whose source or target doesn't exist
    # in the graph. These arise when callee resolution returns an unresolved
    # name, or when a node was removed after edges were already collected
    # (e.g., C++ artifact removal, parameter-name-only node removal).
    if _is_lazy:
        all_node_ids = _cached_node_ids
    else:
        # NetworkX DiGraph supports `nid in G` via O(1) dict lookup.
        # Don't materialize a 700K-element set just for membership tests.
        all_node_ids = G
    before_dangling = len(cross_domain_edges)
    cross_domain_edges = [e for e in cross_domain_edges
                          if e.get("source", "") in all_node_ids
                          and e.get("target", "") in all_node_ids]
    dangling_removed = before_dangling - len(cross_domain_edges)
    if dangling_removed:
        print(f"Removed {dangling_removed} dangling cross-domain edges (source/target not in graph)")

    # Write master navigation file
    # Build edge_type_counts summary from the counter
    _etc = dict(_edge_type_counts)
    master = {
        "type": "code2database_master",
        "source_root": source_root,
        "domains": domain_map,
        "domain_labels": domain_labels,
        "cross_domain_edges": sorted(cross_domain_edges,
                                      key=lambda x: (x.get("source_domain", ""),
                                                     x.get("target_domain", ""),
                                                     x.get("source", ""))),
        "structural_edges": sorted(structural_edges,
                                    key=lambda x: (x.get("relation", ""),
                                                   x.get("source_domain", ""),
                                                   x.get("target_domain", ""),
                                                   x.get("source", ""))),
        "edge_type_counts": _etc,
        "total_nodes": total_nodes,
        "total_edges": total_edges + len(cross_domain_edges),
    }
    if build_info:
        master["build_config"] = build_info

    master_path = os.path.join(outdir, "code2database_master.json")
    # Use streaming write for large master JSON to avoid OOM
    _total_cde = len(cross_domain_edges) + len(structural_edges)
    if _total_cde > 100000:
        with open(master_path, "w", encoding="utf-8") as _mf:
            _mf.write('{\n  "type": "code2database_master",\n')
            _mf.write(f'  "source_root": {json.dumps(source_root)},\n')
            _mf.write('  "domains": ')
            json.dump(domain_map, _mf, ensure_ascii=False, indent=2)
            _mf.write(',\n  "cross_domain_edges": [')
            _first = True
            for e in cross_domain_edges:
                if not _first:
                    _mf.write(',')
                _first = False
                json.dump(e, _mf, ensure_ascii=False, separators=(',', ':'))
            _mf.write('],\n  "structural_edges": [')
            _first = True
            for e in structural_edges:
                if not _first:
                    _mf.write(',')
                _first = False
                json.dump(e, _mf, ensure_ascii=False, separators=(',', ':'))
            _mf.write(f'],\n  "edge_type_counts": ')
            json.dump(_etc, _mf, ensure_ascii=False, separators=(',', ':'))
            _mf.write(f',\n  "total_nodes": {total_nodes},\n')
            _mf.write(f'  "total_edges": {total_edges + len(cross_domain_edges)}\n')
            if build_info:
                _mf.write(',  "build_config": ')
                json.dump(build_info, _mf, ensure_ascii=False, indent=2)
                _mf.write('\n')
            _mf.write('}\n')
    else:
        Path(master_path).write_text(
            json.dumps(master, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8"
        )

    _split_elapsed = time.time() - _split_start
    print(f"[build] Domains split in {_split_elapsed:.0f}s ({len(domain_nodes)} domains)",
          file=sys.stderr)

    return master_path



def _build_domain_readmes(G, outdir):
    """Generate README.md for each domain subdirectory."""
    # Group nodes by top-level domain directory
    domain_dir_nodes = defaultdict(list)
    for nid, ndata in G.nodes(data=True):
        domain = ndata.get("domain", "root")
        parts = domain.split(".")
        top_dir = parts[0] if parts else "root"
        domain_dir_nodes[top_dir].append((nid, ndata))

    for top_dir, nodes in domain_dir_nodes.items():
        readme_dir = os.path.join(outdir, "domains", top_dir)
        readme_path = os.path.join(readme_dir, "README.md")
        os.makedirs(readme_dir, exist_ok=True)

        # Separate API entries, thread entries, and internals
        real = [(nid, nd) for nid, nd in nodes if not nd.get("is_empty", False)]
        api_entries = [(nid, nd) for nid, nd in real if "API_entry" in nd.get("labels", [])]
        thread_entries = [(nid, nd) for nid, nd in real if "thread_processor" in nd.get("labels", [])]
        callback_entries = [(nid, nd) for nid, nd in real if "callback_func" in nd.get("labels", [])]

        lines = [f"# Domain: {top_dir}\n"]
        lines.append(f"Functions: {len(nodes)}\n")
        # Count unique sub-domains
        sub_domains = sorted(set(ndata.get("domain", "root") for _, ndata in nodes))
        if len(sub_domains) > 1:
            lines.append(f"Sub-domains: {', '.join(sub_domains)}\n")
        if api_entries:
            lines.append(f"\n## Public API ({len(api_entries)})\n")
            for nid, nd in sorted(api_entries, key=lambda x: x[1].get("name", ""))[:30]:
                lines.append(f"- `{nd.get('name', '')}` — {nd.get('signature', '')[:80]}")
        if thread_entries:
            lines.append(f"\n## Thread Entries ({len(thread_entries)})\n")
            for nid, nd in sorted(thread_entries, key=lambda x: x[1].get("name", "")):
                lines.append(f"- `{nd.get('name', '')}`")
        if callback_entries:
            lines.append(f"\n## Callback Functions ({len(callback_entries)})\n")
            for nid, nd in sorted(callback_entries, key=lambda x: x[1].get("name", ""))[:20]:
                lines.append(f"- `{nd.get('name', '')}`")
        Path(readme_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


