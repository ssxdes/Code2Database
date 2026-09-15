"""Shared domain-name matching helpers for export and query surfaces.

C2D domains are dot-hierarchical (``ublock.cli.error_inject``) and use
underscores inside segments. Two user slips dominate when a domain
argument matches nothing: typing a parent domain whose functions all
live in sub-domains, and swapping hyphens for underscores. These
helpers turn both cases into either a match or a concrete hint — an
exact hit always wins, normalization is only a fallback.
"""
from __future__ import annotations

import difflib
from typing import List


def norm_domain(name: str) -> str:
    """Normalize separator style so hyphen/underscore slips can match."""
    return (name or "").replace("-", "_")


def domain_of(G, nid: str) -> str:
    return G.nodes[nid].get("domain") or ""


def exact_domain_nodes(G, domain: str) -> List[str]:
    """Nodes of the exact domain; when absent, nodes of the one domain
    equal to it after -/_ normalization."""
    direct = [n for n in sorted(G.nodes) if domain_of(G, n) == domain]
    if direct:
        return direct
    want = norm_domain(domain)
    return [n for n in sorted(G.nodes) if norm_domain(domain_of(G, n)) == want]


def subtree_domain_nodes(G, domain: str) -> List[str]:
    """Nodes of the domain and every child domain, matched after -/_
    normalization."""
    want = norm_domain(domain)
    return [n for n in sorted(G.nodes)
            if want == norm_domain(domain_of(G, n))
            or norm_domain(domain_of(G, n)).startswith(want + ".")]


def domain_suggestions(G, wanted: str, limit: int = 5) -> List[str]:
    """Close domain candidates for a name that matched nothing.

    Separator variants rank first, then child domains, then edit-
    distance neighbors.
    """
    domains = sorted({domain_of(G, n) for n in G.nodes} - {""})
    by_norm = {norm_domain(d): d for d in domains}
    want = norm_domain(wanted)
    hints: List[str] = []
    if want in by_norm:
        hints.append(by_norm[want])
    hints += [d for d in domains
              if norm_domain(d).startswith(want + ".") and d not in hints]
    for cand in difflib.get_close_matches(want, sorted(by_norm), n=limit,
                                          cutoff=0.6):
        d = by_norm[cand]
        if d not in hints:
            hints.append(d)
    return hints[:limit]
