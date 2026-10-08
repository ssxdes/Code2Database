"""callgraph builder module: export."""

import os
import json
import re
from pathlib import Path
from collections import defaultdict
import networkx as nx
from _builder.graph.graph_build import _load_full_graph
from _builder.utils import _safe_domain_component
import logging

# Filesystem- and URL-safe name for a domain: domain names derive from
# directory paths and may contain quotes, angle brackets, spaces, etc.
# The old '.'-only replacement let those through into href="" attributes.
_DOMAIN_SAFE_RE = re.compile(r'[^A-Za-z0-9_\-]')


def _esc(value) -> str:
    """html.escape that tolerates list/dict values from node data.

    Node attributes like ``api_constraints`` may be stored as a list
    (e.g. ``["no_preempt", "irq_safe"]``).  Passing a list to
    ``html.escape`` raises ``AttributeError: 'list' object has no
    attribute 'replace'``.  This helper coerces non-str values to a
    comma-joined string before escaping.
    """
    import html as _html
    if not isinstance(value, str):
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        elif value is None:
            value = ""
        else:
            value = str(value)
    return _html.escape(value)


def _safe_domain_filename(domain: str) -> str:
    """Filesystem- and mermaid-safe name for a domain, length-capped.

    The ASCII-only substitution keeps mermaid subgraph IDs portable;
    the shared component helper then applies the byte budget (120)
    with a digest suffix, so the domain_{name}_mermaid.html export
    path cannot blow past NAME_MAX (255) when a supplemented domain
    carries a very long name.
    """
    return _safe_domain_component(_DOMAIN_SAFE_RE.sub('_', domain))


def _build_mermaid_graph(G: nx.DiGraph) -> str:
    """Build a Mermaid flowchart definition from a networkx DiGraph."""
    lines = ["flowchart LR"]

    # Class definitions for styling
    lines.append("    classDef apiEntry fill:#4caf50,stroke:#2e7d32,color:#fff,font-weight:bold")
    lines.append("    classDef outEnd fill:#ff9800,stroke:#e65100,color:#fff")
    lines.append("    classDef unknownEnd fill:#ff5252,stroke:#d32f2f,color:#fff")
    lines.append("    classDef threadProc fill:#2196f3,stroke:#1565c0,color:#fff")
    lines.append("    classDef callbackFunc fill:#9c27b0,stroke:#6a1b9a,color:#fff")
    lines.append("    classDef constructor fill:#00bcd4,stroke:#00838f,color:#fff")
    lines.append("    classDef destructor fill:#795548,stroke:#4e342e,color:#fff")
    lines.append("    classDef emptyNode fill:#d4d4d4,stroke:#999,stroke-dasharray:3 3")
    lines.append("    classDef regular fill:#e0e0e0,stroke:#757575")

    # Group nodes by domain as subgraphs
    domain_groups = defaultdict(list)
    for nid, ndata in G.nodes(data=True):
        domain = ndata.get("domain", "root")
        domain_groups[domain].append((nid, ndata))

    # Track which class each node belongs to
    node_classes = {}

    for domain, nodes_list in domain_groups.items():
        # ID must be a safe identifier (whitelist), label must be escaped —
        # domain derives from directory paths and may contain ] " etc.,
        # either of which corrupts the diagram syntax.
        safe_domain = _safe_domain_filename(domain)
        lines.append(f"    subgraph {safe_domain}[{_mermaid_label(domain, max_len=60)}]")
        for nid, ndata in nodes_list:
            mid = _mermaid_node_id(nid)
            name = ndata.get("name", nid)
            labels = ndata.get("labels", [])
            is_empty = ndata.get("is_empty", False)

            label = _mermaid_label(name)
            if is_empty:
                cond = ndata.get("condition", "")
                label = _mermaid_label(f"<{cond}>" if cond else name)
                lines.append(f'        {mid}["{label}"]')
                node_classes[mid] = "emptyNode"
            else:
                if "API_entry" in labels:
                    lines.append(f'        {mid}["{label}"]:::apiEntry')
                    node_classes[mid] = "apiEntry"
                elif "unknown_end" in labels:
                    lines.append(f'        {mid}{{"{label}"}}:::unknownEnd')
                    node_classes[mid] = "unknownEnd"
                elif "out_end" in labels:
                    lines.append(f'        {mid}{{"{label}"}}:::outEnd')
                    node_classes[mid] = "outEnd"
                elif "thread_processor" in labels:
                    lines.append(f'        {mid}["{label}"]:::threadProc')
                    node_classes[mid] = "threadProc"
                elif "callback_func" in labels:
                    lines.append(f'        {mid}["{label}"]:::callbackFunc')
                    node_classes[mid] = "callbackFunc"
                elif "constructor" in labels:
                    lines.append(f'        {mid}["{label}"]:::constructor')
                    node_classes[mid] = "constructor"
                elif "destructor" in labels:
                    lines.append(f'        {mid}["{label}"]:::destructor')
                    node_classes[mid] = "destructor"
                else:
                    lines.append(f'        {mid}("{label}"):::regular')
                    node_classes[mid] = "regular"
        lines.append("    end")

    # Edges (skip non-call edges like CONTAINS/IMPORTS)
    for u, v, edata in G.edges(data=True):
        if edata.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        mu = _mermaid_node_id(u)
        mv = _mermaid_node_id(v)
        cond = edata.get("call_condition", "")
        order = edata.get("call_order")
        label_parts = []
        if order is not None:
            label_parts.append(f"#{order}")
        if cond:
            # Escape the condition: it is a raw C expression and routinely
            # contains '"' (string compares) and '||' — either breaks the
            # quoted edge-label syntax and the whole diagram fails to
            # render. _mermaid_label swaps '"' for "'" and escapes | ] } < >.
            label_parts.append(_mermaid_label(cond, max_len=60))
        label = " ".join(label_parts) if label_parts else ""
        if cond:
            # Dashed line for conditional
            if label:
                lines.append(f"    {mu} -.->|\"{label}\"| {mv}")
            else:
                lines.append(f"    {mu} -.-> {mv}")
        else:
            if label:
                lines.append(f"    {mu} -->|\"{label}\"| {mv}")
            else:
                lines.append(f"    {mu} --> {mv}")

    return "\n".join(lines)


def _domain_pages(G, domain_nodes):
    """Build per-domain DiGraphs in ONE pass over the edge list.

    A domain page contains the domain's own nodes plus every call edge
    with at least one endpoint in the domain (cross-domain endpoints
    join as context nodes) — the same page composition
    _build_domain_subgraph produced, but without re-scanning every edge
    once per domain. On a 2.3M-node, 5K-domain graph the per-domain
    rescan was ~10 billion edge visits; this is one pass plus per-page
    assembly.
    """
    domain_of = {}
    for dom, nodes in domain_nodes.items():
        for nid, _ in nodes:
            domain_of[nid] = dom
    edge_buckets = defaultdict(list)
    for u, v, edata in G.edges(data=True):
        if edata.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        du = domain_of.get(u)
        dv = domain_of.get(v)
        if du is not None:
            edge_buckets[du].append((u, v, edata))
        if dv is not None and dv != du:
            edge_buckets[dv].append((u, v, edata))
    pages = []
    for dom in sorted(domain_nodes.keys()):
        nodes = domain_nodes[dom]
        sub_G = nx.DiGraph()
        for nid, ndata in nodes:
            sub_G.add_node(nid, **ndata)
        for u, v, edata in edge_buckets.get(dom, ()):
            if u not in sub_G:
                sub_G.add_node(u, **G.nodes[u])
            if v not in sub_G:
                sub_G.add_node(v, **G.nodes[v])
            sub_G.add_edge(u, v, **edata)
        pages.append((dom, sub_G))
    return pages


def _export_mermaid(G, output, max_nodes, domain_nodes, total_nodes):
    """Export using Mermaid flowchart with Tailwind styling (static, printable)."""
    if total_nodes <= max_nodes:
        _write_mermaid_html(G, output, "Call Graph")
        print(f"HTML exported: {output} ({total_nodes} nodes, mermaid)")
    else:
        html_dir = os.path.join(os.path.dirname(output), "html")
        os.makedirs(html_dir, exist_ok=True)

        index_links = []
        for domain, sub_G in _domain_pages(G, domain_nodes):
            safe_name = _safe_domain_filename(domain)
            domain_html = os.path.join(html_dir, f"domain_{safe_name}_mermaid.html")
            _write_mermaid_html(sub_G, domain_html, f"Call Graph — {domain}")
            index_links.append((domain, f"html/domain_{safe_name}_mermaid.html", sub_G.number_of_nodes()))

        _write_mermaid_index_html(html_dir, index_links, output, total_nodes)
        print(f"HTML exported: {output} (mermaid index) + {len(index_links)} domain files")


def _mermaid_label(name: str, max_len: int = 25) -> str:
    """Truncate and escape a label for Mermaid.

    Escapes characters that are special in Mermaid syntax: ] } | " < >
    """
    s = name if len(name) <= max_len else name[:max_len - 2] + ".."
    # Replace Mermaid-special characters
    s = s.replace('"', "'")
    s = s.replace("]", "\\]").replace("}", "\\}").replace("|", "\\|")
    s = s.replace("<", "\\<").replace(">", "\\>")
    s = s.replace("[", "\\[").replace("{", "\\{")
    return s


def _mermaid_node_id(nid: str) -> str:
    """Convert node ID to a Mermaid-safe identifier.

    Uses a hash suffix to avoid collisions when different IDs map to
    the same sanitized string (e.g., "a-b" and "a_b" both → "a_b").
    """
    import hashlib
    safe = re.sub(r'[^a-zA-Z0-9_]', '_', nid)
    # Add short hash suffix for collision resistance
    h = hashlib.md5(nid.encode()).hexdigest()[:6]
    return f"{safe}_{h}"


def _write_mermaid_html(G: nx.DiGraph, output_path: str, title: str):
    """Write a single HTML file with Mermaid flowchart + Tailwind styling."""
    import html as html_module

    mermaid_src = _build_mermaid_graph(G)

    # Build node detail table for legend/reference (HTML-escaped for XSS prevention)
    node_rows = []
    for nid, ndata in G.nodes(data=True):
        name = html_module.escape(ndata.get("name", nid))
        labels = html_module.escape(", ".join(ndata.get("labels", [])))
        loc = html_module.escape(ndata.get("location", ""))
        domain = html_module.escape(ndata.get("domain", ""))
        constraints = _esc(ndata.get("api_constraints", ""))
        desc = html_module.escape(ndata.get("semantic_desc", "") or ndata.get("external_desc", ""))
        node_rows.append(
            f'<tr><td class="font-mono text-xs">{name}</td>'
            f'<td class="text-xs">{domain}</td>'
            f'<td class="text-xs">{labels}</td>'
            f'<td class="font-mono text-xs">{loc}</td>'
            f'<td class="text-xs">{constraints}</td>'
            f'<td class="text-xs">{desc}</td></tr>'
        )

    node_table = "\n".join(node_rows)
    # Mermaid source needs HTML entity escaping for the template
    mermaid_escaped = mermaid_src.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_title = html_module.escape(title)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{safe_title}</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script type="module">
    import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
    mermaid.initialize({{ startOnLoad: true, theme: "neutral", securityLevel: "loose" }});
  </script>
  <style>
    .mermaid {{ max-width: 100%; overflow-x: auto; }}
  </style>
</head>
<body class="bg-stone-50 text-slate-900 font-sans">
  <main class="max-w-7xl mx-auto px-6 py-8 space-y-8">
    <header>
      <h1 class="text-2xl font-semibold">{safe_title}</h1>
      <p class="text-sm text-slate-500">{G.number_of_nodes()} nodes, {G.number_of_edges()} edges</p>
      <div class="flex flex-wrap gap-3 mt-3 text-xs">
        <span class="px-2 py-1 rounded bg-green-600 text-white">API_entry</span>
        <span class="px-2 py-1 rounded bg-orange-500 text-white">out_end</span>
        <span class="px-2 py-1 rounded bg-red-500 text-white">unknown_end</span>
        <span class="px-2 py-1 rounded bg-blue-500 text-white">thread_processor</span>
        <span class="px-2 py-1 rounded bg-purple-600 text-white">callback_func</span>
        <span class="px-2 py-1 rounded bg-cyan-500 text-white">constructor</span>
        <span class="px-2 py-1 rounded bg-gray-300 text-black">condition (empty)</span>
        <span class="px-2 py-1 rounded bg-gray-200 text-black border">regular</span>
        <span class="ml-4 text-slate-500">Dashed arrow = conditional call</span>
      </div>
    </header>

    <section class="bg-white rounded-lg border border-slate-200 p-4">
      <pre class="mermaid">
{mermaid_escaped}
      </pre>
    </section>

    <section>
      <h2 class="text-lg font-semibold mb-3">Node Reference</h2>
      <div class="overflow-x-auto">
        <table class="w-full text-sm border-collapse">
          <thead class="bg-slate-100">
            <tr>
              <th class="text-left px-2 py-1">Name</th>
              <th class="text-left px-2 py-1">Domain</th>
              <th class="text-left px-2 py-1">Labels</th>
              <th class="text-left px-2 py-1">Location</th>
              <th class="text-left px-2 py-1">Constraints</th>
              <th class="text-left px-2 py-1">Description</th>
            </tr>
          </thead>
          <tbody>
{node_table}
          </tbody>
        </table>
      </div>
    </section>
  </main>
</body>
</html>"""

    Path(output_path).write_text(html, encoding="utf-8")


def _write_mermaid_index_html(html_dir: str, links: list, index_path: str, total_nodes: int):
    """Write an index HTML for per-domain Mermaid graph pages."""
    import html as html_module
    items = []
    for domain, href, count in links:
        # domain derives from a directory path — escape for HTML text and
        # keep the href restricted to the sanitized filename.
        items.append(f'<li class="py-2"><a href="{html_module.escape(href, quote=True)}" '
                     f'class="text-blue-600 hover:underline text-base">{html_module.escape(domain)}</a> '
                     f'<span class="text-sm text-slate-500">({count} nodes)</span></li>')

    link_html = "\n".join(items)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Call Graph — Domain Index</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-stone-50 text-slate-900 font-sans">
  <main class="max-w-3xl mx-auto px-6 py-12">
    <h1 class="text-2xl font-semibold mb-2">Call Graph — {total_nodes} nodes across {len(links)} domains</h1>
    <p class="text-sm text-slate-500 mb-6">Click a domain to view its invocation graph (Mermaid format)</p>
    <ul class="list-none p-0 space-y-1">
{link_html}
    </ul>
  </main>
</body>
</html>"""

    Path(index_path).write_text(html, encoding="utf-8")


