"""Guided codebase tour generator — for new team member onboarding.

Generates a markdown walkthrough document that starts from API entry
points and follows critical paths through the codebase. Like a
"guided tour" of the architecture, explaining what each major module
does and how they connect.

The tour is generated from the code graph, not from hand-written docs.
It uses:
  - API entry points as tour starting locations
  - Critical paths (key-paths) as tour routes
  - Domain structure as chapter/section organization
  - Function descriptions (semantic_desc) as explanations
  - Caller/callee counts as importance indicators

Output: a markdown file `CODEBASE_TOUR.md` that reads like a narrative
guide to the codebase.
"""
from __future__ import annotations

import json
import os
import sys
from typing import List, Dict, Any, Optional


def generate_tour(
    graph_dir: str,
    output_path: Optional[str] = None,
    max_sections: int = 10,
    max_functions_per_section: int = 15,
) -> str:
    """Generate a guided tour markdown document from the code graph.

    Args:
        graph_dir: Path to the graph directory.
        output_path: Where to write the tour. If None, writes to
                     <graph_dir>/CODEBASE_TOUR.md.
        max_sections: Maximum number of domain sections to include.
        max_functions_per_section: Max functions to detail per section.

    Returns:
        The path to the generated tour file.
    """
    from _builder.cgdb_suggest import _load_functions, _load_edges

    functions = _load_functions(graph_dir)
    edges = _load_edges(graph_dir)
    if not functions:
        return _write_tour(output_path or os.path.join(graph_dir, "CODEBASE_TOUR.md"),
                          ["# Codebase Tour\n\nNo graph data found. Run `scan` + `build` first.\n"])

    # Compute caller/callee counts
    caller_count: Dict[str, int] = {}
    callee_count: Dict[str, int] = {}
    for edge in edges:
        if edge.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        src = edge.get("source", "")
        dst = edge.get("target", "")
        if src:
            callee_count[src] = callee_count.get(src, 0) + 1
        if dst:
            caller_count[dst] = caller_count.get(dst, 0) + 1

    # Group functions by domain
    domains: Dict[str, List[str]] = {}
    for fid, func in functions.items():
        if func.get("is_empty", False):
            continue
        dom = func.get("domain", "root")
        domains.setdefault(dom, []).append(fid)

    # Sort domains by size (largest first)
    sorted_domains = sorted(domains.items(), key=lambda x: -len(x[1]))

    # Build the tour document
    lines: List[str] = []
    lines.append("# Codebase Tour\n")
    lines.append(f"This is an auto-generated guided tour of the codebase, "
                 f"based on the code graph database.\n")
    lines.append(f"- **Total functions**: {len(functions)}\n")
    lines.append(f"- **Total edges**: {len(edges)}\n")
    lines.append(f"- **Domains**: {len(domains)}\n\n")
    lines.append("---\n\n")

    # Section 1: API Entry Points
    api_entries = [
        (fid, func) for fid, func in functions.items()
        if "API_entry" in func.get("labels", [])
        and not func.get("is_empty", False)
    ]
    api_entries.sort(key=lambda x: -caller_count.get(x[0], 0))

    if api_entries:
        lines.append("## 1. Entry Points\n\n")
        lines.append("These are the functions that serve as entry points into "
                     "the codebase — external callers start here.\n\n")
        for fid, func in api_entries[:max_functions_per_section]:
            name = func.get("name", "")
            desc = func.get("semantic_desc", "")
            sig = func.get("signature", "")
            cc = caller_count.get(fid, 0)
            src = func.get("source_file", "")
            lines.append(f"### `{name}`\n")
            if sig:
                lines.append(f"- **Signature**: `{sig}`\n")
            lines.append(f"- **Called by**: {cc} function(s)\n")
            if src:
                lines.append(f"- **Location**: `{src}`\n")
            if desc:
                lines.append(f"- **Description**: {desc}\n")
            lines.append("\n")
        lines.append("\n---\n\n")

    # Section 2: Domain Walkthrough
    for i, (domain, fids) in enumerate(sorted_domains[:max_sections], 2):
        lines.append(f"## {i}. Domain: `{domain}`\n\n")
        lines.append(f"This domain contains {len(fids)} functions.\n\n")

        # Sort by importance (caller count)
        fids_sorted = sorted(fids, key=lambda fid: -caller_count.get(fid, 0))
        for fid in fids_sorted[:max_functions_per_section]:
            func = functions[fid]
            name = func.get("name", "")
            desc = func.get("semantic_desc", "")
            sig = func.get("signature", "")
            cc = caller_count.get(fid, 0)
            ce = callee_count.get(fid, 0)
            labels = func.get("labels", [])
            src = func.get("source_file", "")

            lines.append(f"### `{name}`\n")
            if sig:
                lines.append(f"- **Signature**: `{sig}`\n")
            lines.append(f"- **Callers**: {cc} | **Callees**: {ce}\n")
            if labels:
                lines.append(f"- **Labels**: {', '.join(labels)}\n")
            if src:
                lines.append(f"- **Location**: `{src}`\n")
            if desc:
                lines.append(f"- **Description**: {desc}\n")
            else:
                lines.append(f"- **Description**: *(not yet documented — "
                             f"run `extract-semantics --node {name}`)*\n")
            lines.append("\n")

        if len(fids) > max_functions_per_section:
            remaining = len(fids) - max_functions_per_section
            lines.append(f"*...and {remaining} more functions in this domain.*\n\n")
        lines.append("\n---\n\n")

    # Section 3: Hub Functions (high fan-in)
    hubs = [
        (fid, func.get("name", ""), caller_count.get(fid, 0))
        for fid, func in functions.items()
        if not func.get("is_empty", False) and caller_count.get(fid, 0) >= 10
    ]
    hubs.sort(key=lambda x: -x[2])
    if hubs:
        lines.append(f"## {len(sorted_domains) + 2}. Hub Functions\n\n")
        lines.append("These functions are called by many other functions — "
                     "they are the \"backbone\" of the codebase. Changes here "
                     "have wide impact.\n\n")
        lines.append("| Function | Callers | Domain |\n")
        lines.append("|----------|---------|--------|\n")
        for fid, name, cc in hubs[:20]:
            dom = functions[fid].get("domain", "")
            lines.append(f"| `{name}` | {cc} | {dom} |\n")
        lines.append("\n---\n\n")

    # Section 4: Thread/Concurrency Entry Points
    thread_entries = [
        (fid, func.get("name", ""), func.get("semantic_desc", ""))
        for fid, func in functions.items()
        if "thread_processor" in func.get("labels", [])
        and not func.get("is_empty", False)
    ]
    if thread_entries:
        lines.append(f"## {len(sorted_domains) + 3}. Concurrency Entry Points\n\n")
        lines.append("These functions run in separate threads. Pay special "
                     "attention to shared variable access and locking.\n\n")
        for fid, name, desc in thread_entries:
            lines.append(f"- `{name}`")
            if desc:
                lines.append(f" — {desc}")
            lines.append("\n")
        lines.append(f"\n*Run `concurrency-risks` to check for data races.*\n\n")
        lines.append("\n---\n\n")

    # Section 5: Suggestions
    lines.append(f"## {len(sorted_domains) + 4}. Next Steps\n\n")
    lines.append("Here are some suggested commands to explore further:\n\n")
    lines.append("- `explore-flow --query \"initialization\"` — "
                 "find initialization code paths\n")
    lines.append("- `key-paths` — auto-extract critical execution paths\n")
    lines.append("- `concurrency-risks` — check for data races\n")
    lines.append("- `cgdb-suggest` — get proactive improvement suggestions\n")
    lines.append("- `web-ui` — start the interactive Web UI\n")
    lines.append("\n---\n\n")
    lines.append("*This tour was auto-generated from the code graph database. "
                 "Run `cgdb-tour` again after rebuilding the graph to update.*\n")

    return _write_tour(output_path or os.path.join(graph_dir, "CODEBASE_TOUR.md"), lines)


def _write_tour(path: str, lines: List[str]) -> str:
    """Write tour lines to a file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return path


def cmd_cgdb_tour(args):
    """CLI handler for `code2database_builder.py cgdb-tour`."""
    graph_dir = args.graph
    output = getattr(args, "output", None)
    path = generate_tour(graph_dir, output_path=output)
    print(f"Tour written to: {path}")
    return 0
