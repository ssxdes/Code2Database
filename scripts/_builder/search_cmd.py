"""callgraph builder module: search_cmd."""

import os
import json
import sys
import re
from pathlib import Path
from collections import defaultdict
import networkx as nx
from _builder.utils import _find_node_id, _load_globals
from _builder.graph_build import _load_full_graph


def cmd_domain(args):
    """List all nodes and edges in a specific architecture domain.

    P0_fix: Support SQLite-only builds by querying code2database.db directly
    when code2database_master.json is absent.
    """
    graph_dir = args.graph
    master_path = os.path.join(graph_dir, "code2database_master.json")
    domain_name = args.name

    if not os.path.exists(master_path):
        # P0_fix: SQLite fallback
        db_path = os.path.join(graph_dir, "code2database.db")
        if os.path.exists(db_path):
            return _cmd_domain_from_sqlite(db_path, domain_name)
        print(f"Error: {master_path} not found and no code2database.db fallback",
              file=sys.stderr)
        sys.exit(1)

    master = json.loads(Path(master_path).read_text(encoding="utf-8"))

    # Find matching domain
    matched_domain = None
    filename = None
    for domain, fname in master.get("domains", {}).items():
        if domain == domain_name or domain_name.lower() in domain.lower():
            matched_domain = domain
            filename = fname
            break

    if not matched_domain:
        print(f"Domain '{domain_name}' not found. Available: {list(master.get('domains', {}).keys())}",
              file=sys.stderr)
        sys.exit(1)

    domain_path = os.path.join(graph_dir, filename)
    domain_data = json.loads(Path(domain_path).read_text(encoding="utf-8"))
    print(json.dumps(domain_data, ensure_ascii=False, indent=2))


def _cmd_domain_from_sqlite(db_path: str, domain_name: str):
    """P0_fix: Query a domain's nodes and edges from SQLite."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Find matching domain (case-insensitive substring)
        cur = conn.execute(
            "SELECT DISTINCT domain FROM functions ORDER BY domain")
        all_domains = [r[0] for r in cur.fetchall()]
        matched = None
        for d in all_domains:
            if d == domain_name or domain_name.lower() in d.lower():
                matched = d
                break
        if not matched:
            print(f"Domain '{domain_name}' not found. Available (first 50): "
                  f"{all_domains[:50]}", file=sys.stderr)
            sys.exit(1)
        # Load nodes in this domain
        cur = conn.execute("SELECT * FROM functions WHERE domain=?", (matched,))
        nodes = []
        for row in cur:
            row_dict = dict(row)
            labels_raw = row_dict.get("labels", "[]")
            try:
                labels = json.loads(labels_raw) if labels_raw else []
            except (json.JSONDecodeError, TypeError):
                labels = []
            nodes.append({
                "id": row_dict.get("id"),
                "name": row_dict.get("name"),
                "source_file": row_dict.get("source_file"),
                "line": row_dict.get("line_number"),
                "domain": row_dict.get("domain"),
                "labels": labels,
                "signature": row_dict.get("signature", ""),
            })
        # Load edges where caller is in this domain
        cur = conn.execute(
            "SELECT * FROM edges WHERE invoker_id IN "
            "(SELECT id FROM functions WHERE domain=?)", (matched,))
        edges = []
        for row in cur:
            row_dict = dict(row)
            edges.append({
                "source": row_dict.get("invoker_id"),
                "target": row_dict.get("invoked_id"),
                "relation": row_dict.get("relation", "INVOKES"),
                "call_order": row_dict.get("call_order"),
                "call_condition": row_dict.get("call_condition", ""),
                "confidence": row_dict.get("confidence", "EXTRACTED"),
                "source_tag": row_dict.get("source", "ast"),
            })
        result = {"domain": matched, "nodes": nodes, "edges": edges,
                  "node_count": len(nodes), "edge_count": len(edges)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        conn.close()




def _is_call_edge(edata: dict) -> bool:
    """Check if an edge represents a function call (not CONTAINS/IMPORTS)."""
    relation = edata.get("relation", "")
    return relation not in ("CONTAINS", "IMPORTS")


def _is_file_node(ndata: dict) -> bool:
    """Check if a node represents a file (not a function)."""
    return "file" in ndata.get("labels", []) or ndata.get("node_type") == "file"


def cmd_impact(args):
    G = _load_full_graph(args.graph)

    if args.node not in G:
        candidates = [n for n in G.nodes if args.node.lower() in n.lower()]
        print(f"Node '{args.node}' not found. Similar: {candidates[:5]}", file=sys.stderr)
        sys.exit(1)

    direction = args.direction
    if direction == "reverse":
        # Who calls this function? (callers / dependents)
        direct = []
        for pred in G.predecessors(args.node):
            ed = G.get_edge_data(pred, args.node) or {}
            if not _is_call_edge(ed):
                continue
            d = G.nodes[pred]
            direct.append({
                "id": pred, "name": d.get("name", ""),
                "source_file": d.get("source_file", ""),
                "domain": d.get("domain", ""),
                "call_order": ed.get("call_order"),
                "call_condition": ed.get("call_condition", ""),
            })

        second_order = []
        seen_second = set()
        for pred in G.predecessors(args.node):
            ed0 = G.get_edge_data(pred, args.node) or {}
            if not _is_call_edge(ed0):
                continue
            for pred2 in G.predecessors(pred):
                if pred2 != args.node and pred2 not in seen_second:
                    ed = G.get_edge_data(pred2, pred) or {}
                    if not _is_call_edge(ed):
                        continue
                    seen_second.add(pred2)
                    d2 = G.nodes[pred2]
                    second_order.append({
                        "id": pred2, "name": d2.get("name", ""),
                        "source_file": d2.get("source_file", ""),
                        "domain": d2.get("domain", ""),
                        "via": pred,
                    })
    else:
        # Forward: what does this function call? (callees / dependencies)
        direct = []
        for succ in G.successors(args.node):
            ed = G.get_edge_data(args.node, succ) or {}
            if not _is_call_edge(ed):
                continue
            d = G.nodes[succ]
            direct.append({
                "id": succ, "name": d.get("name", ""),
                "source_file": d.get("source_file", ""),
                "domain": d.get("domain", ""),
                "call_order": ed.get("call_order"),
                "call_condition": ed.get("call_condition", ""),
            })

        second_order = []
        seen_second = set()
        for succ in G.successors(args.node):
            ed0 = G.get_edge_data(args.node, succ) or {}
            if not _is_call_edge(ed0):
                continue
            for succ2 in G.successors(succ):
                if succ2 != args.node and succ2 not in seen_second:
                    ed = G.get_edge_data(succ, succ2) or {}
                    if not _is_call_edge(ed):
                        continue
                    seen_second.add(succ2)
                    d2 = G.nodes[succ2]
                    second_order.append({
                        "id": succ2, "name": d2.get("name", ""),
                        "source_file": d2.get("source_file", ""),
                        "domain": d2.get("domain", ""),
                        "via": succ,
                    })

    domains_affected = set()
    for item in direct + second_order:
        dom = item.get("domain")
        if dom:
            domains_affected.add(dom)

    lite = getattr(args, "lite", False)
    if lite:
        # Lite mode: only return counts and domains, no node lists
        result = {
            "node": args.node,
            "direction": direction,
            "direct_impact": len(direct),
            "second_order_impact": len(second_order),
            "domains_affected": sorted(domains_affected),
        }
    else:
        result = {
            "node": args.node,
            "direction": direction,
            "direct_impact": len(direct),
            "second_order_impact": len(second_order),
            "domains_affected": sorted(domains_affected),
            "direct": direct[:50],
            "second_order": second_order[:50],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))




def cmd_load(args):
    G = _load_full_graph(args.graph)

    if args.summary:
        domains = set()
        label_counts = defaultdict(int)
        for _, d in G.nodes(data=True):
            domains.add(d.get("domain", "root"))
            for label in d.get("labels", []):
                label_counts[label] += 1
        empty_count = sum(1 for _, d in G.nodes(data=True) if d.get("is_empty"))

        print(f"Nodes: {G.number_of_nodes()} (including {empty_count} empty/conditional nodes)")
        print(f"Edges: {G.number_of_edges()}")
        print(f"Domains: {len(domains)}")
        if label_counts:
            print(f"Labels: {dict(label_counts)}")
        print(f"Domains list: {', '.join(sorted(domains)[:20])}")
    else:
        print(json.dumps({
            "nodes": G.number_of_nodes(),
            "edges": G.number_of_edges(),
        }, ensure_ascii=False))




def cmd_neighbors(args):
    G = _load_full_graph(args.graph)

    if args.node not in G:
        # Try partial match
        candidates = [n for n in G.nodes if args.node.lower() in n.lower()]
        if candidates:
            print(f"Node '{args.node}' not found. Similar: {candidates[:5]}", file=sys.stderr)
        else:
            print(f"Node '{args.node}' not found in graph.", file=sys.stderr)
        sys.exit(1)

    # P0_fix: For high-degree nodes (e.g. mutex_lock with 21K callers), unbounded
    # BFS at depth 2+ can visit millions of nodes. Cap total results.
    max_results = getattr(args, "max_results", 200)

    visited = {args.node}
    frontier = {args.node}
    result = []
    truncated = False

    for d in range(1, args.depth + 1):
        if truncated:
            break
        next_frontier = set()
        for n in frontier:
            if truncated:
                break
            for nb in G.successors(n):
                if nb not in visited:
                    ed = G.get_edge_data(n, nb) or {}
                    if not _is_call_edge(ed):
                        continue
                    if _is_file_node(G.nodes[nb]):
                        continue
                    visited.add(nb)
                    next_frontier.add(nb)
                    nd = G.nodes[nb]
                    result.append({
                        "id": nb, "name": nd.get("name", ""),
                        "source_file": nd.get("source_file", ""),
                        "domain": nd.get("domain", ""),
                        "labels": nd.get("labels", []),
                        "call_order": ed.get("call_order"),
                        "call_condition": ed.get("call_condition", ""),
                        "direction": "calls",
                        "distance": d,
                    })
                    if len(result) >= max_results:
                        truncated = True
                        break
            if truncated:
                break
            for nb in G.predecessors(n):
                if nb not in visited:
                    ed = G.get_edge_data(nb, n) or {}
                    if not _is_call_edge(ed):
                        continue
                    if _is_file_node(G.nodes[nb]):
                        continue
                    visited.add(nb)
                    next_frontier.add(nb)
                    nd = G.nodes[nb]
                    result.append({
                        "id": nb, "name": nd.get("name", ""),
                        "source_file": nd.get("source_file", ""),
                        "domain": nd.get("domain", ""),
                        "labels": nd.get("labels", []),
                        "call_order": ed.get("call_order"),
                        "call_condition": ed.get("call_condition", ""),
                        "direction": "called_by",
                        "distance": d,
                    })
                    if len(result) >= max_results:
                        truncated = True
                        break
        frontier = next_frontier

    print(json.dumps(result, ensure_ascii=False, indent=2))
    suffix = " (truncated, use --max-results to increase)" if truncated else ""
    print(f"\n{len(result)} neighbors within depth {args.depth}{suffix}", file=sys.stderr)




def _node_domain(G, nid):
    """Get domain attribute of a node safely."""
    try:
        d = G.nodes[nid] or {}
        return d.get("domain", "") or ""
    except Exception:
        return ""


def _bfs_with_domain_preference(G, from_node, to_node, no_filter, prefer_same_domain,
                                origin_domain, strict_vtable_domain=False,
                                vtable_bindings=None):
    """BFS that, at vtable_dispatch junctures, prefers targets whose domain
    matches origin_domain. Falls back to cross-domain targets only if no
    same-domain path reaches to_node.

    vtable_dispatch edges create one edge per registered implementation. When
    traversing from a function in domain D, the BFS should prefer dispatch
    targets in domain D (e.g., ext4_evict_inode when called from ext4 code),
    not arbitrary other implementations (e.g., affs_evict_inode). This
    eliminates false-positive cross-filesystem paths.

    If strict_vtable_domain is True, cross-domain vtable_dispatch edges are
    completely excluded (no fallback). Use this when analyzing whether a path
    exists within a single subsystem.

    If vtable_bindings is provided (dict: vtable_type → impl_name), only
    vtable_dispatch edges whose vtable_type matches AND whose target's name
    matches the bound impl_name are followed. Other dispatches of the same
    vtable_type are excluded. This is the most precise filter.
    """
    from collections import deque
    import heapq

    counter = 0
    heap = []
    heapq.heappush(heap, (0, counter, from_node))
    counter += 1
    came_from = {from_node: None}
    node_tier = {from_node: 0}

    found = False
    while heap:
        tier, _, cur = heapq.heappop(heap)
        if cur == to_node:
            found = True
            break
        for nb in G.successors(cur):
            if nb in came_from:
                continue
            if not no_filter:
                ed = G.get_edge_data(cur, nb) or {}
                concurrency = ed.get("concurrency", "")
                if concurrency in ("spawn_target", "callback"):
                    continue
                cond = ed.get("call_condition", "")
                if cond and ("#if 0" in cond or "#ifdef 0" in cond):
                    continue
                # Per-vtable explicit binding (highest precision)
                if vtable_bindings and concurrency == "vtable_dispatch":
                    vt_type = ed.get("vtable_type", "") or ""
                    if vt_type in vtable_bindings:
                        # Only follow if target name matches the bound impl
                        bound_impl = vtable_bindings[vt_type]
                        nb_name = ""
                        try:
                            nb_name = (G.nodes[nb] or {}).get("name", "") or ""
                        except Exception:
                            pass
                        if nb_name != bound_impl:
                            continue
                # Domain preference for vtable_dispatch
                if prefer_same_domain and concurrency == "vtable_dispatch" and origin_domain:
                    nb_domain = _node_domain(G, nb)
                    if nb_domain and nb_domain != origin_domain and nb_domain != "root":
                        if strict_vtable_domain:
                            continue  # Exclude cross-domain vtable_dispatch entirely
                        # Deprioritize (tier 1) — same-domain paths preferred
                        new_tier = 1
                    else:
                        new_tier = tier
                else:
                    new_tier = tier
            else:
                new_tier = tier
            came_from[nb] = cur
            node_tier[nb] = new_tier
            heapq.heappush(heap, (new_tier, counter, nb))
            counter += 1

    if not found:
        raise nx.NetworkXNoPath(from_node, to_node)

    path = []
    cur = to_node
    while cur is not None:
        path.append(cur)
        cur = came_from[cur]
    path.reverse()
    return path


def _parse_vtable_bindings(bindings_str):
    """Parse --vtable-bind argument: 'type1=impl1,type2=impl2' → dict."""
    if not bindings_str:
        return None
    result = {}
    for pair in bindings_str.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        vtype, impl = pair.split("=", 1)
        vtype = vtype.strip()
        impl = impl.strip()
        if vtype and impl:
            result[vtype] = impl
    return result if result else None


def cmd_path(args):
    G = _load_full_graph(args.graph)

    missing = []
    for node_id, lookup in [(args.from_node, "from"), (args.to_node, "to")]:
        if node_id not in G:
            candidates = [n for n in G.nodes if node_id.lower() in n.lower()]
            print(f"Node '{node_id}' not found. Similar: {candidates[:5]}", file=sys.stderr)
            missing.append(node_id)
    if missing:
        try:
            from _builder.coverage_report import path_not_found_hints
            hints = path_not_found_hints(args.graph,
                                        missing_src=missing[0] if missing else None,
                                        missing_dst=missing[1] if len(missing) > 1 else None)
            if hints.get('suggestion'):
                print(f"Hints: {hints['suggestion']}", file=sys.stderr)
            if hints.get('missing_common_subsystems'):
                print(f"Missing common subsystems: {', '.join(hints['missing_common_subsystems'][:5])}", file=sys.stderr)
        except Exception:
            pass
        sys.exit(1)

    no_filter = getattr(args, 'no_condition_filter', False)
    prefer_same_domain = getattr(args, 'prefer_same_domain', True)
    strict_vtable_domain = getattr(args, 'strict_vtable_domain', False)
    vtable_bindings = _parse_vtable_bindings(getattr(args, 'vtable_bind', '') or '')

    origin_domain = _node_domain(G, args.from_node)

    # P0_fix: For LazySQLiteGraph (large SQLite-backed graphs), building a full
    # call-only subgraph via _make_call_graph iterates 1.5M nodes + 4.8M edges,
    # which times out. Use direct BFS on the lazy graph instead.
    is_lazy = type(G).__name__ == "LazySQLiteGraph"
    try:
        if is_lazy:
            path = _bfs_with_domain_preference(
                G, args.from_node, args.to_node, no_filter,
                prefer_same_domain, origin_domain,
                strict_vtable_domain=strict_vtable_domain,
                vtable_bindings=vtable_bindings
            )
        else:
            from _builder.utils import _make_call_graph
            call_G = _make_call_graph(G, skip_file_nodes=True)

            if not no_filter:
                edges_to_remove = []
                for u, v, ed in call_G.edges(data=True):
                    concurrency = ed.get("concurrency", "")
                    if concurrency in ("spawn_target", "callback"):
                        edges_to_remove.append((u, v))
                    cond = ed.get("call_condition", "")
                    if cond and ("#if 0" in cond or "#ifdef 0" in cond):
                        edges_to_remove.append((u, v))
                call_G.remove_edges_from(edges_to_remove)

                # Domain filtering for vtable_dispatch edges.
                if prefer_same_domain and origin_domain:
                    vtable_edges_by_src = defaultdict(list)
                    for u, v, ed in call_G.edges(data=True):
                        if ed.get("concurrency") == "vtable_dispatch":
                            vtable_edges_by_src[u].append((v, ed))
                    edges_to_remove = []
                    for u, targets in vtable_edges_by_src.items():
                        same_domain_targets = [
                            (v, ed) for v, ed in targets
                            if (call_G.nodes[v].get("domain", "") or "") in
                               (origin_domain, "root", "")
                        ]
                        if strict_vtable_domain:
                            # Always remove cross-domain vtable_dispatch edges
                            for v, ed in targets:
                                v_domain = call_G.nodes[v].get("domain", "") or ""
                                if v_domain and v_domain != origin_domain and v_domain != "root":
                                    edges_to_remove.append((u, v))
                        elif same_domain_targets:
                            # Soft preference: only remove cross-domain if same-domain exists
                            for v, ed in targets:
                                v_domain = call_G.nodes[v].get("domain", "") or ""
                                if v_domain and v_domain != origin_domain and v_domain != "root":
                                    edges_to_remove.append((u, v))
                    call_G.remove_edges_from(edges_to_remove)

            path = nx.shortest_path(call_G, args.from_node, args.to_node)
    except nx.NetworkXNoPath:
        print("No call path found between these nodes.", file=sys.stderr)
        sys.exit(1)

    path_info = []
    for nid in path:
        d = G.nodes[nid]
        path_info.append({
            "id": nid,
            "name": d.get("name", ""),
            "source_file": d.get("source_file", ""),
            "domain": d.get("domain", ""),
            "labels": d.get("labels", []),
        })

    edges = []
    for i in range(len(path) - 1):
        ed = G.get_edge_data(path[i], path[i + 1]) or {}
        edges.append({
            "from": path[i],
            "to": path[i + 1],
            "call_order": ed.get("call_order"),
            "call_condition": ed.get("call_condition", ""),
        })

    result = {"path": path_info, "edges": edges, "length": len(path) - 1}
    print(json.dumps(result, ensure_ascii=False, indent=2))




def cmd_search(args):
    """Search nodes by keywords.

    P0_fix: When code2database_master.json is absent (SQLite-only build),
    use SQLite-native search instead of loading the full 1.5M-node graph
    into memory. The full-graph approach times out for kernel-scale projects.
    """
    graph_dir = args.graph
    master_path = os.path.join(graph_dir, "code2database_master.json")
    if not os.path.exists(master_path):
        db_path = os.path.join(graph_dir, "code2database.db")
        if os.path.exists(db_path):
            return _cmd_search_from_sqlite(db_path, args)
        print(f"Error: {master_path} not found and no code2database.db fallback",
              file=sys.stderr)
        sys.exit(1)
    G = _load_full_graph(args.graph)
    keywords = [kw.lower() for kw in args.keywords.split()]
    results = []

    for nid, d in G.nodes(data=True):
        if d.get("is_empty", False):
            continue
        name = (d.get("name") or "").lower()
        nid_lower = nid.lower()
        source = d.get("source_file", "").lower()
        domain = d.get("domain", "").lower()
        body = (d.get("body_text") or "").lower()
        semantic = (d.get("semantic_desc") or "").lower()
        ext_desc = (d.get("external_desc") or "").lower()
        constraints = (d.get("api_constraints") or "").lower()
        # Also search callee_args
        callee_args_text = " ".join(
            ca.get("args_snippet", "").lower() + ca.get("callee", "").lower()
            for ca in d.get("callee_args", [])
        )

        score = 0
        for kw in keywords:
            if kw in name:
                score += 3
            if kw in nid_lower:
                score += 2
            if kw in source:
                score += 1
            if kw in domain:
                score += 1
            if kw in body:
                score += 1
            if kw in semantic:
                score += 2
            if kw in ext_desc:
                score += 2
            if kw in constraints:
                score += 1
            if kw in callee_args_text:
                score += 1

        if score > 0:
            results.append({
                "id": nid,
                "name": d.get("name", ""),
                "source_file": d.get("source_file", ""),
                "domain": d.get("domain", ""),
                "labels": d.get("labels", []),
                "signature": d.get("signature", ""),
                "score": score,
                "degree": sum(1 for _ in G.successors(nid)
                              if _is_call_edge(G.get_edge_data(nid, _) or {}))
                           + sum(1 for _ in G.predecessors(nid)
                                 if _is_call_edge(G.get_edge_data(_, nid) or {})),
            })

    results.sort(key=lambda x: (-x["score"], -x["degree"]))

    # Deduplicate by node id (same node may match multiple keywords)
    seen_ids = set()
    deduped = []
    for r in results:
        if r["id"] not in seen_ids:
            seen_ids.add(r["id"])
            deduped.append(r)

    top = deduped[:args.top]
    print(json.dumps(top, ensure_ascii=False, indent=2))
    print(f"\nFound {len(results)} matches, showing top {len(top)}", file=sys.stderr)


def _cmd_search_from_sqlite(db_path: str, args):
    """SQLite-native search — avoids loading full graph into memory.

    Uses SQL LIKE queries on functions table to find matches, then computes
    degree via a single LEFT JOIN with GROUP BY subqueries (replacing the
    former per-result N+1 COUNT queries). Handles 1.5M-node graphs in
    seconds instead of timing out.
    """
    import sqlite3
    keywords = [kw.lower() for kw in args.keywords.split()]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conditions = []
        params = []
        for kw in keywords:
            like = f"%{kw}%"
            conditions.append(
                "(LOWER(name) LIKE ? OR LOWER(id) LIKE ? OR "
                "LOWER(source_file) LIKE ? OR LOWER(domain) LIKE ?)")
            params.extend([like, like, like, like])
        where_clause = " OR ".join(conditions)
        sql = (
            "SELECT f.id, f.name, f.source_file, f.domain, f.line_number, "
            "       f.labels, f.signature, f.extra_json, "
            "       COALESCE(o.out_deg, 0) AS out_deg, "
            "       COALESCE(i.in_deg,  0) AS in_deg "
            "FROM functions f "
            "LEFT JOIN ( "
            "    SELECT invoker_id AS nid, COUNT(*) AS out_deg "
            "    FROM edges WHERE relation = 'INVOKES' "
            "    GROUP BY invoker_id "
            ") o ON o.nid = f.id "
            "LEFT JOIN ( "
            "    SELECT invoked_id AS nid, COUNT(*) AS in_deg "
            "    FROM edges WHERE relation = 'INVOKES' "
            "    GROUP BY invoked_id "
            ") i ON i.nid = f.id "
            f"WHERE ({where_clause})"
        )
        cur = conn.execute(sql, params)
        results = []
        for row in cur:
            row_dict = dict(row)
            nid = row_dict.get("id")
            if not nid:
                continue
            name = (row_dict.get("name") or "")
            source = (row_dict.get("source_file") or "")
            domain = (row_dict.get("domain") or "")
            nid_lower = nid.lower()
            name_lower = name.lower()
            source_lower = source.lower()
            domain_lower = domain.lower()

            extra = {}
            extra_raw = row_dict.get("extra_json")
            if extra_raw:
                try:
                    extra = json.loads(extra_raw)
                except (json.JSONDecodeError, TypeError):
                    pass
            if extra.get("is_empty", False):
                continue
            semantic = (extra.get("semantic_desc") or "").lower()
            ext_desc = (extra.get("external_desc") or "").lower()
            constraints = (extra.get("api_constraints") or "").lower()

            score = 0
            for kw in keywords:
                if kw in name_lower:
                    score += 3
                if kw in nid_lower:
                    score += 2
                if kw in source_lower:
                    score += 1
                if kw in domain_lower:
                    score += 1
                if kw in semantic:
                    score += 2
                if kw in ext_desc:
                    score += 2
                if kw in constraints:
                    score += 1

            if score == 0:
                continue
            labels_raw = row_dict.get("labels", "[]")
            try:
                labels = json.loads(labels_raw) if labels_raw else []
            except (json.JSONDecodeError, TypeError):
                labels = []
            # Degree from the LEFT JOIN — no N+1 COUNT queries.
            in_deg = int(row_dict.get("in_deg", 0))
            out_deg = int(row_dict.get("out_deg", 0))
            results.append({
                "id": nid,
                "name": name,
                "source_file": source,
                "domain": domain,
                "labels": labels,
                "signature": row_dict.get("signature", ""),
                "score": score,
                "degree": in_deg + out_deg,
            })

        results.sort(key=lambda x: (-x["score"], -x["degree"]))
        # Deduplicate
        seen_ids = set()
        deduped = []
        for r in results:
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                deduped.append(r)
        top = deduped[:args.top]
        print(json.dumps(top, ensure_ascii=False, indent=2))
        print(f"\nFound {len(results)} matches, showing top {len(top)}",
              file=sys.stderr)
    finally:
        conn.close()


