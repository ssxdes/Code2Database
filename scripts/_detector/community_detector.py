#!/usr/bin/env python3
"""Leiden community detection for invocation graphs.

Detects semantic communities in the invocation graph using the Leiden algorithm.
Clusters functions that call each other frequently into communities,
generating heuristic labels from folder patterns and common name prefixes.

Adapted for Code2Database's networkx DiGraph model.
"""

import gc
import json
import os
import re
import sys
from collections import Counter, defaultdict

try:
    import igraph as ig
    import leidenalg
    _HAS_IGRAPH = True
except ImportError:
    _HAS_IGRAPH = False


class CommunityResult:
    """Result of community detection."""

    def __init__(self):
        self.communities = []  # [{"id", "label", "heuristic_label", "keywords",
                               #   "node_ids", "cohesion", "symbol_count"}]
        self.node_community = {}  # node_id → community_id
        self.domain_overlap = {}  # community_id → list of domain names merged into this community


def detect_communities(G, source_root: str = "") -> CommunityResult:
    """Run Leiden community detection on the invocation graph.

    Uses only INVOKES edges (undirected projection) to find clusters.
    Operates on the WHOLE graph to discover cross-domain communities,
    producing fewer communities than domain-based grouping by merging
    related domains into super-communities.
    Falls back to domain-based grouping if igraph/leidenalg unavailable.

    Args:
        G: networkx DiGraph with invocation graph data
        source_root: for heuristic label generation

    Returns:
        CommunityResult with community assignments, cohesion scores,
        and domain_overlap mapping.
    """
    if not _HAS_IGRAPH:
        return _fallback_domain_communities(G)

    # Build undirected graph from INVOKES edges for community detection
    # Only include non-empty, non-dead-code nodes
    real_nodes = {nid for nid, nd in G.nodes(data=True)
                  if not nd.get("is_empty", False)
                  and "dead_code" not in nd.get("labels", [])}

    # For very large graphs (>500K nodes), Leiden can consume excessive memory
    # (igraph needs ~2x the edge list in memory). Fall back to domain-based.
    if len(real_nodes) > 500000:
        print(f"[community] {len(real_nodes)} nodes is too large for Leiden, "
              f"using domain-based fallback", file=sys.stderr)
        return _fallback_domain_communities(G)

    # Build adjacency for igraph
    node_list = sorted(real_nodes)
    node_idx = {nid: i for i, nid in enumerate(node_list)}

    # Collect edges (undirected projection, only INVOKES edges)
    edge_set = set()
    for u, v, edata in G.edges(data=True):
        if edata.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        if u in node_idx and v in node_idx:
            i, j = node_idx[u], node_idx[v]
            if i != j:
                edge_set.add((min(i, j), max(i, j)))

    if len(node_list) < 2:
        return _single_community(G, node_list)

    # Count unique domains in the graph to know the baseline
    domain_set = {G.nodes[nid].get("domain", "root") for nid in node_list}
    num_domains = len(domain_set)

    # Try Leiden with a resolution parameter that produces fewer communities
    # than domains.  Use RBConfigurationVertexPartition (Reichardt-Bornholdt
    # with configuration model) which supports resolution_parameter.
    # Lower resolution → fewer, larger communities (merges related domains).
    ig_graph = ig.Graph(n=len(node_list))
    ig_graph.add_edges(list(edge_set))

    best_partition = None
    best_resolution = None

    # Try decreasing resolution values to find one that gives fewer communities
    for resolution in [0.5, 0.3, 0.2, 0.1]:
        try:
            partition = leidenalg.find_partition(
                ig_graph,
                leidenalg.RBConfigurationVertexPartition,
                resolution_parameter=resolution,
                seed=42,
            )
            num_communities = len(set(partition.membership))
            # Accept if communities are noticeably fewer than domains (at least 20% fewer)
            if num_communities < num_domains * 0.8:
                best_partition = partition
                best_resolution = resolution
                break
            # Clean up if not selected
            del partition
        except Exception:
            continue

    # If no resolution gave fewer communities, fall back to cross-domain affinity
    if best_partition is None:
        del ig_graph
        gc.collect()
        return _cross_domain_affinity_communities(G, node_list, domain_set)

    # Build community assignments from Leiden partition
    result = CommunityResult()
    community_members = defaultdict(list)

    for idx, comm_id in enumerate(best_partition.membership):
        nid = node_list[idx]
        community_members[comm_id].append(nid)

    # Free igraph memory immediately after partition extraction
    del best_partition
    del ig_graph
    gc.collect()

    for comm_id, members in community_members.items():
        if len(members) < 2:
            # Singleton — will be merged into nearest community below
            continue

        # Generate heuristic label and domain overlap
        domains = [G.nodes[m].get("domain", "root") for m in members]
        names = [G.nodes[m].get("name", "") for m in members]
        source_files = [G.nodes[m].get("source_file", "") for m in members]

        heuristic_label = _generate_heuristic_label(domains, names, source_files)
        keywords = _extract_keywords(names, source_files)
        cohesion = _calculate_cohesion(G, members)

        comm_name = f"community_{comm_id}"
        result.communities.append({
            "id": comm_name,
            "label": heuristic_label,
            "heuristic_label": heuristic_label,
            "keywords": keywords,
            "node_ids": members,
            "cohesion": round(cohesion, 3),
            "symbol_count": len(members),
        })

        # Record which domains are present in this community
        domain_counter = Counter(domains)
        result.domain_overlap[comm_name] = sorted(domain_counter.keys())

        for nid in members:
            result.node_community[nid] = comm_name

    # Assign remaining (singleton) nodes to nearest community
    for nid in node_list:
        if nid not in result.node_community:
            best_comm = _nearest_community(G, nid, result)
            if best_comm:
                result.node_community[nid] = best_comm
                for comm in result.communities:
                    if comm["id"] == best_comm:
                        comm["node_ids"].append(nid)
                        comm["symbol_count"] = len(comm["node_ids"])
                        comm["cohesion"] = round(_calculate_cohesion(G, comm["node_ids"]), 3)
                        # Update domain overlap
                        dom = G.nodes[nid].get("domain", "root")
                        if dom not in result.domain_overlap[best_comm]:
                            result.domain_overlap[best_comm].append(dom)
                            result.domain_overlap[best_comm].sort()
                        break

    return result


def _fallback_domain_communities(G) -> CommunityResult:
    """Fallback: group nodes by domain when igraph unavailable."""
    result = CommunityResult()
    domain_members = defaultdict(list)

    for nid, nd in G.nodes(data=True):
        if nd.get("is_empty", False) or "dead_code" in nd.get("labels", []):
            continue
        domain_members[nd.get("domain", "root")].append(nid)

    for domain, members in domain_members.items():
        if len(members) < 1:
            continue
        comm_name = f"community_{domain.replace('.', '_')}"
        names = [G.nodes[m].get("name", "") for m in members]
        source_files = [G.nodes[m].get("source_file", "") for m in members]
        heuristic_label = domain.replace(".", " → ")

        result.communities.append({
            "id": comm_name,
            "label": heuristic_label,
            "heuristic_label": heuristic_label,
            "keywords": _extract_keywords(names, source_files),
            "node_ids": members,
            "cohesion": _calculate_cohesion(G, members),
            "symbol_count": len(members),
        })
        result.domain_overlap[comm_name] = [domain]
        for nid in members:
            result.node_community[nid] = comm_name

    return result


def _cross_domain_affinity_communities(G, node_list, domain_set) -> CommunityResult:
    """Merge highly-connected domains into super-communities.

    Used when Leiden produces community counts close to the domain count,
    indicating it isn't finding cross-domain structure.  This approach
    computes inter-domain call affinity and uses agglomerative merging
    to group domains that call each other frequently.

    Args:
        G: networkx DiGraph with invocation graph data
        node_list: list of non-empty, non-dead-code node IDs
        domain_set: set of unique domain names in the graph

    Returns:
        CommunityResult with merged domain communities.
    """
    # Build node → domain mapping
    node_domain = {}
    for nid in node_list:
        node_domain[nid] = G.nodes[nid].get("domain", "root")

    # Compute inter-domain call counts
    domain_pair_weight = Counter()
    domain_internal_weight = Counter()

    for u, v, edata in G.edges(data=True):
        if edata.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        if u not in node_domain or v not in node_domain:
            continue
        du, dv = node_domain[u], node_domain[v]
        if du == dv:
            domain_internal_weight[du] += 1
        else:
            pair = tuple(sorted([du, dv]))
            domain_pair_weight[pair] += 1

    # Normalize: compute affinity as (cross_edges) / sqrt(size_a * size_b)
    # to avoid just merging the two largest domains
    domain_sizes = Counter(node_domain.values())

    def affinity(pair):
        a, b = pair
        denom = max(1, (domain_sizes[a] * domain_sizes[b]) ** 0.5)
        return domain_pair_weight[pair] / denom

    # Agglomerative merging: repeatedly merge the pair with highest affinity
    # until we've reduced communities to roughly half the domain count
    # (or stop if no pair has meaningful affinity)
    merged = {}  # domain → merged_group-name
    for d in domain_set:
        merged[d] = d

    # Track group sizes (initially same as domain sizes)
    group_sizes = dict(domain_sizes)  # group_name → total node count

    target_count = max(1, len(domain_set) // 2)
    current_groups = set(domain_set)

    while len(current_groups) > target_count:
        # Aggregate edge weights at the group level
        group_pair_weight = Counter()
        for (a, b), w in domain_pair_weight.items():
            ga, gb = merged[a], merged[b]
            if ga == gb:
                continue
            pair = tuple(sorted([ga, gb]))
            group_pair_weight[pair] += w

        if not group_pair_weight:
            break

        # Find pair with highest affinity among current groups
        best_pair = None
        best_aff = 0.0
        for pair, w in group_pair_weight.most_common():
            a, b = pair
            denom = max(1, (group_sizes.get(a, 0) * group_sizes.get(b, 0)) ** 0.5)
            aff = w / denom
            if aff > best_aff:
                best_aff = aff
                best_pair = pair
                break  # most_common is sorted, first valid is best

        if best_pair is None or best_aff < 0.1:
            break  # no meaningful cross-domain connections left

        # Merge: remap all domains in both ga and gb to new_name
        ga, gb = best_pair
        new_name = f"{ga}+{gb}"
        domains_in_ga = [d for d, g in merged.items() if g == ga]
        domains_in_gb = [d for d, g in merged.items() if g == gb]
        for d in domains_in_ga:
            merged[d] = new_name
        for d in domains_in_gb:
            merged[d] = new_name
        current_groups.discard(ga)
        current_groups.discard(gb)
        current_groups.add(new_name)
        # Update group_sizes for the merged group
        group_sizes[new_name] = group_sizes.get(ga, 0) + group_sizes.get(gb, 0)
        group_sizes.pop(ga, None)
        group_sizes.pop(gb, None)

    # Build CommunityResult from merged groups
    result = CommunityResult()
    group_members = defaultdict(list)
    group_domains = defaultdict(list)
    for nid in node_list:
        d = node_domain[nid]
        g = merged[d]
        group_members[g].append(nid)
        group_domains[g].append(d)

    for group_name, members in group_members.items():
        if len(members) < 1:
            continue

        unique_domains = sorted(set(group_domains[group_name]))
        names = [G.nodes[m].get("name", "") for m in members]
        source_files = [G.nodes[m].get("source_file", "") for m in members]

        # Label: join merged domain names with "+"
        if len(unique_domains) > 1:
            heuristic_label = "+".join(unique_domains)
        else:
            heuristic_label = unique_domains[0].replace(".", " → ")

        comm_name = f"community_{group_name.replace('.', '_').replace('+', '_')}"
        result.communities.append({
            "id": comm_name,
            "label": heuristic_label,
            "heuristic_label": heuristic_label,
            "keywords": _extract_keywords(names, source_files),
            "node_ids": members,
            "cohesion": _calculate_cohesion(G, members),
            "symbol_count": len(members),
        })
        result.domain_overlap[comm_name] = unique_domains
        for nid in members:
            result.node_community[nid] = comm_name

    return result


def _single_community(G, node_list) -> CommunityResult:
    """All nodes in one community."""
    result = CommunityResult()
    if not node_list:
        return result

    names = [G.nodes[m].get("name", "") for m in node_list]
    result.communities.append({
        "id": "community_0",
        "label": "all",
        "heuristic_label": "all",
        "keywords": _extract_keywords(names, []),
        "node_ids": list(node_list),
        "cohesion": 0.0,
        "symbol_count": len(node_list),
    })
    for nid in node_list:
        result.node_community[nid] = "community_0"
    return result


def _generate_heuristic_label(domains: list, names: list,
                              source_files: list) -> str:
    """Generate a human-readable community label from member patterns."""
    # Strategy 0: multi-domain label — join top domains with "+"
    if domains:
        domain_counts = Counter(domains)
        # If multiple domains are present and the top domain is not a strong
        # majority, create a composite label from the top contributors
        if len(domain_counts) > 1:
            total = len(domains)
            # Include domains that contribute at least 10% of members
            major_domains = sorted(
                d for d, c in domain_counts.items() if c >= total * 0.1
            )
            if len(major_domains) >= 2:
                return "+".join(major_domains)
        # Single dominant domain
        top_domain, top_count = domain_counts.most_common(1)[0]
        if top_count >= len(domains) * 0.5:
            return top_domain.replace(".", " → ")

    # Strategy 2: common source file directory
    if source_files:
        dirs = [os.path.dirname(sf).replace("/", " → ") for sf in source_files if sf]
        dir_counts = Counter(dirs)
        if dir_counts:
            top_dir, dir_count = dir_counts.most_common(1)[0]
            if dir_count >= len(source_files) * 0.4:
                return top_dir if top_dir else "root"

    # Strategy 3: common name prefix
    if names:
        prefixes = Counter()
        for name in names:
            # Try underscore-separated prefix
            parts = name.split("_")
            if len(parts) >= 2:
                prefixes["_".join(parts[:2])] += 1
            # Try camelCase prefix
            m = re.match(r'([a-z]+)', name)
            if m and len(m.group(1)) >= 3:
                prefixes[m.group(1)] += 1
        if prefixes:
            top_prefix, prefix_count = prefixes.most_common(1)[0]
            if prefix_count >= len(names) * 0.3:
                return top_prefix

    # Fallback: numbered community
    return f"cluster"


def _extract_keywords(names: list, source_files: list) -> list:
    """Extract representative keywords from member names."""
    keywords = []
    # Common meaningful words from function names
    stop_words = {'get', 'set', 'is', 'has', 'do', 'to', 'of', 'in',
                  'on', 'for', 'the', 'and', 'or', 'not', 'with', 'from'}
    word_counts = Counter()

    for name in names:
        # Split snake_case
        for part in re.split(r'[_]', name):
            part = part.lower().strip()
            if len(part) >= 3 and part not in stop_words:
                word_counts[part] += 1
        # Split camelCase
        for part in re.findall(r'[A-Z]?[a-z]+', name):
            part = part.lower()
            if len(part) >= 3 and part not in stop_words:
                word_counts[part] += 1

    # Top keywords
    for word, count in word_counts.most_common(8):
        if count >= 2:
            keywords.append(word)

    return keywords[:8]


def _calculate_cohesion(G, members: list) -> float:
    """Calculate internal edge density (cohesion) for a community.

    Cohesion = actual_internal_edges / max_possible_internal_edges
    """
    member_set = set(members)
    internal_edges = 0

    # Only check edges from member nodes (O(member_out_edges) vs O(total_edges))
    # Exclude CONTAINS/IMPORTS edges — they're not function calls
    for u in members:
        for v in G.successors(u):
            ed = G.get_edge_data(u, v) or {}
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            if v in member_set:
                internal_edges += 1

    n = len(members)
    max_edges = n * (n - 1)  # directed graph max
    if max_edges == 0:
        return 0.0

    return internal_edges / max_edges


def _nearest_community(G, nid, result: CommunityResult) -> str:
    """Find the community with the most edges to this node."""
    best_comm = None
    best_count = 0

    # Count edges to each community (excluding CONTAINS/IMPORTS edges)
    comm_edge_count = Counter()
    for pred in G.predecessors(nid):
        ed = G.get_edge_data(pred, nid) or {}
        if ed.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        if pred in result.node_community:
            comm_edge_count[result.node_community[pred]] += 1
    for succ in G.successors(nid):
        ed = G.get_edge_data(nid, succ) or {}
        if ed.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        if succ in result.node_community:
            comm_edge_count[result.node_community[succ]] += 1

    if comm_edge_count:
        best_comm, best_count = comm_edge_count.most_common(1)[0]

    return best_comm


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import networkx as nx

    # Build a small test graph
    G = nx.DiGraph()
    # Cluster A: init/start/run/stop
    G.add_node("a_init", name="init", domain="module.a", source_file="module/a/main.c", labels=[])
    G.add_node("a_start", name="start", domain="module.a", source_file="module/a/main.c", labels=[])
    G.add_node("a_run", name="run", domain="module.a", source_file="module/a/run.c", labels=[])
    G.add_node("a_stop", name="stop", domain="module.a", source_file="module/a/run.c", labels=[])
    G.add_edge("a_init", "a_start")
    G.add_edge("a_start", "a_run")
    G.add_edge("a_run", "a_stop")

    # Cluster B: dev_open/dev_close/dev_read
    G.add_node("dev_open", name="dev_open", domain="lib.dev", source_file="lib/dev/dev.c", labels=[])
    G.add_node("dev_close", name="dev_close", domain="lib.dev", source_file="lib/dev/dev.c", labels=[])
    G.add_node("dev_read", name="dev_read", domain="lib.dev", source_file="lib/dev/io.c", labels=[])
    G.add_edge("dev_open", "dev_read")
    G.add_edge("dev_read", "dev_close")

    # Cross-cluster edge
    G.add_edge("a_run", "dev_open")

    result = detect_communities(G)
    print(f"Found {len(result.communities)} communities:")
    for comm in result.communities:
        overlap = result.domain_overlap.get(comm['id'], [])
        overlap_str = ", ".join(overlap) if overlap else "none"
        print(f"  {comm['id']}: label={comm['label']}, "
              f"domains=[{overlap_str}], "
              f"keywords={comm['keywords']}, "
              f"cohesion={comm['cohesion']}, "
              f"size={comm['symbol_count']}")
    print(f"\nNode assignments: {dict(list(result.node_community.items())[:10])}")
