"""Structured design document generation from the code graph.

`design-doc` renders a nine-section software implementation design
document for one module (a domain or a source file), pulling every
number and name from the built graph so the document stays in sync
with the code by construction:

1. Implementation Model — functions and their sizes
2. Context View — external callers and callees crossing the boundary
3. Logical View — internal hubs and call structure
4. Interface Design — entry-labeled functions with signatures
5. Data Model — globals and struct fields read/written
6. Algorithm Implementation — deepest internal call chains
7. Security Design — memory-copy sinks, async spawns, gated access
8. Developer Test Model — test functions exercising the module
9. Runtime View — thread entries, callbacks, entry labels

Output: Markdown.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

_TEST_PATH_RE = re.compile(r"/(test|tests|unit|ut|unittest|fuzz)/", re.IGNORECASE)
_TEST_DOMAIN_RE = re.compile(r"(^|\.)(ut|ut_mock|unit|test|fuzz)(\.|$)")
_MEMORY_SINK_RE = re.compile(
    r"\b(?:memcpy|memmove|strcpy|strncpy|strcat|strncat|sprintf|vsprintf)\b")
_MAX_CHAIN_LEN = 10
_MAX_CHAINS = 3


def _is_call_relation(relation: str) -> bool:
    return relation in ("", "INVOKES", "DISPATCH")


def _call_neighbors(G, nid: str) -> Tuple[List[str], List[str]]:
    callees = [v for v in G.successors(nid)
               if _is_call_relation((G.get_edge_data(nid, v) or {}).get("relation", ""))]
    callers = [u for u in G.predecessors(nid)
               if _is_call_relation((G.get_edge_data(u, nid) or {}).get("relation", ""))]
    return callees, callers


def _is_test_node(nd: dict) -> bool:
    src = nd.get("source_file", "") or ""
    if _TEST_PATH_RE.search(src.replace("\\", "/")):
        return True
    dom = nd.get("domain", "") or ""
    return bool(_TEST_DOMAIN_RE.search(dom))


def _name(G, nid: str) -> str:
    return G.nodes[nid].get("name") or nid


def design_doc(graph_dir: str, module: str,
               output: Optional[str] = None) -> str:
    """Render the nine-section design document for a module.

    Args:
        graph_dir: C2D graph directory.
        module: Domain name (exact) or source-file substring.
        output: Write the document here when given; always returned.

    Raises:
        ValueError: when no functions match the module.
    """
    from _builder.graph.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)

    by_domain = [n for n in G.nodes
                 if G.nodes[n].get("domain", "") == module]
    if by_domain:
        members = sorted(by_domain)
        module_kind = "domain"
    else:
        members = sorted(n for n in G.nodes
                         if module in (G.nodes[n].get("source_file", "")
                                       or G.nodes[n].get("file_path", "")))
        module_kind = "file"
    if not members:
        raise ValueError(
            f"no functions found for module '{module}' "
            f"(tried domain match and source-file substring)")
    member_set = set(members)

    lines: List[str] = [f"# Design Document: {module}",
                        "",
                        f"Source: code graph ({len(members)} functions, "
                        f"matched by {module_kind}).",
                        ""]

    # 1. Implementation Model --------------------------------------------
    lines += ["## 1. Implementation Model", ""]
    lines += ["| Function | File | Labels |", "|---|---|---|"]
    for nid in members[:80]:
        nd = G.nodes[nid]
        labels = ", ".join(nd.get("labels", []) or []) or "—"
        lines.append(f"| `{_name(G, nid)}` | {nd.get('source_file', '')} "
                     f"| {labels} |")
    if len(members) > 80:
        lines.append(f"| … | ({len(members) - 80} more) | |")
    lines.append("")

    # 2. Context View ----------------------------------------------------
    external_callers: Set[str] = set()
    external_callees: Set[str] = set()
    for nid in members:
        callees, callers = _call_neighbors(G, nid)
        for c in callers:
            if c not in member_set:
                external_callers.add(c)
        for c in callees:
            if c not in member_set:
                external_callees.add(c)
    lines += ["## 2. Context View", ""]
    lines.append(f"- External callers (who calls into the module): "
                 f"{len(external_callers)}")
    lines.append(f"- External callees (what the module depends on): "
                 f"{len(external_callees)}")
    if external_callers:
        lines += ["", "**Representative external callers:**"]
        for nid in sorted(external_callers)[:15]:
            nd = G.nodes[nid]
            lines.append(f"- `{_name(G, nid)}` ({nd.get('domain', '?')})")
    if external_callees:
        lines += ["", "**Representative external dependencies:**"]
        for nid in sorted(external_callees)[:15]:
            nd = G.nodes[nid]
            lines.append(f"- `{_name(G, nid)}` ({nd.get('domain', '?')})")
    lines.append("")

    # 3. Logical View ----------------------------------------------------
    degrees = []
    for nid in members:
        callees, callers = _call_neighbors(G, nid)
        internal_out = sum(1 for c in callees if c in member_set)
        internal_in = sum(1 for c in callers if c in member_set)
        degrees.append((internal_in + internal_out, nid))
    degrees.sort(reverse=True)
    lines += ["## 3. Logical View", ""]
    lines.append("**Internal hubs (by in-module degree):**")
    for deg, nid in degrees[:10]:
        if deg == 0:
            break
        lines.append(f"- `{_name(G, nid)}` — {deg} internal call links")
    lines.append("")

    # 4. Interface Design --------------------------------------------------
    entries = [nid for nid in members
               if "API_entry" in (G.nodes[nid].get("labels", []) or [])]
    lines += ["## 4. Interface Design", ""]
    if entries:
        lines += ["| Entry | Signature |", "|---|---|"]
        for nid in sorted(entries):
            sig = G.nodes[nid].get("signature", "") or _name(G, nid)
            lines.append(f"| `{_name(G, nid)}` | `{sig}` |")
    else:
        lines.append("No API_entry-labeled functions in this module.")
    lines.append("")

    # 5. Data Model --------------------------------------------------------
    globals_read: Set[str] = set()
    globals_written: Set[str] = set()
    fields_read: Set[str] = set()
    fields_written: Set[str] = set()
    for nid in members:
        nd = G.nodes[nid]
        globals_read.update(nd.get("globals_read", []) or [])
        globals_written.update(nd.get("globals_written", []) or [])
        fields_read.update(nd.get("fields_read", []) or [])
        fields_written.update(nd.get("fields_written", []) or [])
    lines += ["## 5. Data Model", ""]
    lines.append(f"- Globals read: {len(globals_read)}"
                 + (f" — {sorted(globals_read)[:10]}" if globals_read else ""))
    lines.append(f"- Globals written: {len(globals_written)}"
                 + (f" — {sorted(globals_written)[:10]}" if globals_written else ""))
    lines.append(f"- Struct fields read: {len(fields_read)}"
                 + (f" — {sorted(fields_read)[:10]}" if fields_read else ""))
    lines.append(f"- Struct fields written: {len(fields_written)}"
                 + (f" — {sorted(fields_written)[:10]}" if fields_written else ""))
    lines.append("")

    # 6. Algorithm Implementation -------------------------------------------
    chains = _deepest_internal_chains(G, member_set)
    lines += ["## 6. Algorithm Implementation", ""]
    if chains:
        lines.append("**Deepest internal call chains:**")
        for chain in chains:
            lines.append("- " + " → ".join(f"`{_name(G, n)}`" for n in chain))
    else:
        lines.append("No internal call chains (functions are independent).")
    lines.append("")

    # 7. Security Design -----------------------------------------------------
    sink_callers: List[str] = []
    spawn_edges: List[str] = []
    for nid in members:
        callees, _ = _call_neighbors(G, nid)
        for c in callees:
            cname = _name(G, c)
            if _MEMORY_SINK_RE.search(cname):
                sink_callers.append(f"`{_name(G, nid)}` → `{cname}`")
        for v in G.successors(nid):
            ed = G.get_edge_data(nid, v) or {}
            conc = ed.get("concurrency", "")
            if conc == "async_spawn":
                spawn_edges.append(f"`{_name(G, nid)}` spawns `{_name(G, v)}`")
    lines += ["## 7. Security Design", ""]
    lines.append(f"- Memory-copy sinks reached: {len(sink_callers)}"
                 + ("" if sink_callers else " (none)"))
    for s in sorted(set(sink_callers))[:10]:
        lines.append(f"  - {s}")
    lines.append(f"- Async spawns: {len(spawn_edges)}"
                 + ("" if spawn_edges else " (none)"))
    for s in sorted(set(spawn_edges))[:10]:
        lines.append(f"  - {s}")
    lines.append("")

    # 8. Developer Test Model --------------------------------------------------
    test_callers: Set[str] = set()
    for nid in members:
        _, callers = _call_neighbors(G, nid)
        for c in callers:
            if c not in member_set and _is_test_node(G.nodes[c]):
                test_callers.add(c)
    covered = {nid for nid in members
               if any(c in test_callers
                      for c in _call_neighbors(G, nid)[1])}
    ratio = len(covered) / len(members) if members else 0.0
    lines += ["## 8. Developer Test Model", ""]
    lines.append(f"- Test functions calling into the module: "
                 f"{len(test_callers)}")
    lines.append(f"- Member functions reachable from tests: "
                 f"{len(covered)}/{len(members)} ({ratio:.0%})")
    for nid in sorted(test_callers)[:10]:
        lines.append(f"- `{_name(G, nid)}` ({G.nodes[nid].get('domain', '?')})")
    lines.append("")

    # 9. Runtime View -------------------------------------------------------------
    thread_entries = [nid for nid in members
                      if G.nodes[nid].get("thread_entry")]
    callbacks = [nid for nid in members
                 if "callback_func" in (G.nodes[nid].get("labels", []) or [])]
    lines += ["## 9. Runtime View", ""]
    lines.append(f"- Thread entries: {len(thread_entries)}"
                 + (f" — {sorted(_name(G, n) for n in thread_entries)[:8]}"
                    if thread_entries else ""))
    lines.append(f"- Callback registrations: {len(callbacks)}"
                 + (f" — {sorted(_name(G, n) for n in callbacks)[:8]}"
                    if callbacks else ""))
    lines.append("")

    doc = "\n".join(lines)
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(doc)
    return doc


def _deepest_internal_chains(G, member_set: Set[str]) -> List[List[str]]:
    """Longest internal call chains, up to three distinct ones."""
    best: List[List[str]] = []

    def _dfs(chain: List[str]) -> None:
        if len(chain) >= _MAX_CHAIN_LEN:
            return
        cur = chain[-1]
        callees, _ = _call_neighbors(G, cur)
        extended = False
        for c in sorted(callees):
            if c in member_set and c not in chain:
                _dfs(chain + [c])
                extended = True
        if not extended and len(chain) >= 2:
            best.append(list(chain))

    for nid in sorted(member_set):
        _dfs([nid])

    best.sort(key=lambda ch: (-len(ch), ch))
    picked: List[List[str]] = []
    for chain in best:
        if all(set(chain) & set(p) != set(chain) for p in picked):
            picked.append(chain)
        if len(picked) >= _MAX_CHAINS:
            break
    return picked


def cmd_design_doc(args):
    """CLI handler for `code2database_builder.py design-doc`."""
    import sys
    try:
        doc = design_doc(args.graph, args.module,
                         output=getattr(args, "output", None))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if not getattr(args, "output", None):
        print(doc, end="")
