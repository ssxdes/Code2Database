"""Shared domain-name matching helpers for export and query surfaces.

C2D domains are dot-hierarchical (``ublock.cli.error_inject``) and use
underscores inside segments. When a domain argument matches nothing,
the usual cause is a separator slip — a hyphen typed for an underscore
or a dot. These helpers compare names after collapsing ``-``, ``_``
and ``.`` into one separator class, with two guards:

- an exact hit always wins; normalized matching is fallback only
- a normalized match is used only when it resolves to exactly one
  real domain, so two domains that differ only in separator style
  can never silently blend into one module
"""
from __future__ import annotations

import difflib
import re
from typing import List

_SEPARATOR_RUN = re.compile(r"[-_.]+")

# A domain is a test domain when one dotted path component says so.
_TEST_COMPONENT_RE = re.compile(
    r"(^|\.)(ut|ut_mock|unit|unittest|test|tests|fuzz)(\.|$)")


def is_test_domain(domain: str) -> bool:
    return bool(_TEST_COMPONENT_RE.search(domain or ""))


def canon_domain(name: str) -> str:
    """Canonical form: separator runs collapse to single dots."""
    return _SEPARATOR_RUN.sub(".", (name or "").strip())


def domain_of(G, nid: str) -> str:
    return G.nodes[nid].get("domain") or ""


def _all_domains(G) -> List[str]:
    return sorted({domain_of(G, n) for n in G.nodes} - {""})


def exact_domain_nodes(G, domain: str) -> List[str]:
    """Nodes of the exact domain; when absent, nodes of the single
    domain equal to it after separator canonicalization."""
    direct = [n for n in sorted(G.nodes) if domain_of(G, n) == domain]
    if direct:
        return direct
    want = canon_domain(domain)
    candidates = [d for d in _all_domains(G)
                  if canon_domain(d) == want]
    if len(candidates) == 1:
        return [n for n in sorted(G.nodes)
                if domain_of(G, n) == candidates[0]]
    return []


def subtree_domain_nodes(G, domain: str) -> List[str]:
    """Nodes of the domain and every child domain, matched after
    separator canonicalization."""
    want = canon_domain(domain)
    if not want or want == ".":
        return []
    out = []
    for n in sorted(G.nodes):
        c = canon_domain(domain_of(G, n))
        if c == want or c.startswith(want + "."):
            out.append(n)
    return out


def domain_suggestions(G, wanted: str, limit: int = 5) -> List[str]:
    """Close domain candidates for a name that matched nothing.

    Separator-class variants rank first, then child domains, then
    edit-distance neighbors.
    """
    domains = _all_domains(G)
    want = canon_domain(wanted)
    hints: List[str] = [d for d in domains if canon_domain(d) == want]
    hints += [d for d in domains
              if canon_domain(d).startswith(want + ".")
              and d not in hints]
    for cand in difflib.get_close_matches(want, [canon_domain(d) for d in domains],
                                          n=limit, cutoff=0.6):
        for d in domains:
            if canon_domain(d) == cand and d not in hints:
                hints.append(d)
    return hints[:limit]
