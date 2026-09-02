"""callgraph builder module: export."""

import os
import json
import re
from pathlib import Path
from collections import defaultdict
import networkx as nx
from _builder.graph_build import _load_full_graph
import logging

# Filesystem- and URL-safe name for a domain: domain names derive from
# directory paths and may contain quotes, angle brackets, spaces, etc.
# The old '.'-only replacement let those through into href="" attributes.
_DOMAIN_SAFE_RE = re.compile(r'[^A-Za-z0-9_\-]')


def _safe_domain_filename(domain: str) -> str:
    return _DOMAIN_SAFE_RE.sub('_', domain)


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
        safe_domain = domain.replace(".", "_").replace("-", "_")
        lines.append(f"    subgraph {safe_domain}[{domain}]")
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
            label_parts.append(cond)
        label = "|".join(label_parts) if label_parts else ""
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




def _build_domain_subgraph(G, domain_node_list):
    """Build a DiGraph subgraph for a domain, including cross-domain call edges."""
    sub_G = nx.DiGraph()
    node_ids = set()
    for nid, ndata in domain_node_list:
        sub_G.add_node(nid, **ndata)
        node_ids.add(nid)
    for u, v, edata in G.edges(data=True):
        # Skip non-call edges (CONTAINS/IMPORTS) in visualization
        if edata.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        if u in node_ids or v in node_ids:
            if u not in sub_G:
                sub_G.add_node(u, **G.nodes[u])
            if v not in sub_G:
                sub_G.add_node(v, **G.nodes[v])
            sub_G.add_edge(u, v, **edata)
    return sub_G


def _export_mermaid(G, output, max_nodes, domain_nodes, total_nodes):
    """Export using Mermaid flowchart with Tailwind styling (static, printable)."""
    if total_nodes <= max_nodes:
        _write_mermaid_html(G, output, "Call Graph")
        print(f"HTML exported: {output} ({total_nodes} nodes, mermaid)")
    else:
        html_dir = os.path.join(os.path.dirname(output), "html")
        os.makedirs(html_dir, exist_ok=True)

        index_links = []
        for domain in sorted(domain_nodes.keys()):
            nodes = domain_nodes[domain]
            sub_G = _build_domain_subgraph(G, nodes)

            safe_name = _safe_domain_filename(domain)
            domain_html = os.path.join(html_dir, f"domain_{safe_name}_mermaid.html")
            _write_mermaid_html(sub_G, domain_html, f"Call Graph — {domain}")
            index_links.append((domain, f"html/domain_{safe_name}_mermaid.html", sub_G.number_of_nodes()))

        _write_mermaid_index_html(html_dir, index_links, output, total_nodes)
        print(f"HTML exported: {output} (mermaid index) + {len(index_links)} domain files")




def _export_vis_network(G, output, max_nodes, domain_nodes, total_nodes):
    """Export using vis-network (interactive force-directed graph)."""
    if total_nodes <= max_nodes:
        _write_html_file(G, output, "Call Graph", full_graph=True)
        print(f"HTML exported: {output} ({total_nodes} nodes, vis-network)")
    else:
        html_dir = os.path.join(os.path.dirname(output), "html")
        os.makedirs(html_dir, exist_ok=True)

        index_links = []
        for domain in sorted(domain_nodes.keys()):
            nodes = domain_nodes[domain]
            sub_G = _build_domain_subgraph(G, nodes)

            safe_name = _safe_domain_filename(domain)
            domain_html = os.path.join(html_dir, f"domain_{safe_name}.html")
            _write_html_file(sub_G, domain_html, f"Call Graph — {domain}", full_graph=False)
            index_links.append((domain, f"html/domain_{safe_name}.html", sub_G.number_of_nodes()))

        _write_index_html(html_dir, index_links, output, total_nodes)
        print(f"HTML exported: {output} (index) + {len(index_links)} domain files in {html_dir}/")




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




def _node_color(ndata: dict) -> dict:
    """Determine node color based on labels."""
    labels = ndata.get("labels", [])
    if ndata.get("is_empty", False):
        return {"background": "#d4d4d4", "border": "#999999"}
    if "API_entry" in labels:
        return {"background": "#4caf50", "border": "#2e7d32"}
    if "unknown_end" in labels:
        return {"background": "#ff5252", "border": "#d32f2f"}
    if "out_end" in labels:
        return {"background": "#ff9800", "border": "#e65100"}
    if "thread_processor" in labels:
        return {"background": "#2196f3", "border": "#1565c0"}
    if "callback_func" in labels:
        return {"background": "#9c27b0", "border": "#6a1b9a"}
    if "constructor" in labels:
        return {"background": "#00bcd4", "border": "#00838f"}
    if "destructor" in labels:
        return {"background": "#795548", "border": "#4e342e"}
    return {"background": "#e0e0e0", "border": "#757575"}




def _node_shape(ndata: dict) -> str:
    if ndata.get("is_empty", False):
        return "diamond"
    labels = ndata.get("labels", [])
    if "API_entry" in labels:
        return "box"
    if "out_end" in labels or "unknown_end" in labels:
        return "triangle"
    return "dot"




def _write_html_file(G: nx.DiGraph, output_path: str, title: str, full_graph: bool = True):
    """Write a single HTML file with vis-network visualization."""
    import html as html_module

    safe_title = html_module.escape(title)

    # Build nodes JSON
    vis_nodes = []
    for nid, ndata in G.nodes(data=True):
        color = _node_color(ndata)
        shape = _node_shape(ndata)
        labels = ndata.get("labels", [])
        label_str = ", ".join(labels) if labels else ""
        name = ndata.get("name", nid)
        location = ndata.get("location", "") or f"{ndata.get('source_file', '')}:{ndata.get('line', 0)}"

        title_html = (f"<b>{html_module.escape(name)}</b><br>"
                      f"File: {html_module.escape(location)}<br>"
                      f"Domain: {html_module.escape(ndata.get('domain', ''))}<br>"
                      f"Labels: {html_module.escape(label_str)}<br>"
                      f"Constraints: {html_module.escape(ndata.get('api_constraints', ''))}<br>"
                      f"Desc: {html_module.escape(ndata.get('external_desc', '') or ndata.get('semantic_desc', ''))}")

        vis_nodes.append({
            "id": nid,
            "label": name if len(name) <= 25 else name[:22] + "...",
            "title": title_html,
            "color": color,
            "shape": shape,
            "font": {"size": 12},
            "size": 20 if "API_entry" in labels else (10 if ndata.get("is_empty") else 15),
        })

    # Build edges JSON (skip non-call edges like CONTAINS/IMPORTS)
    vis_edges = []
    for u, v, edata in G.edges(data=True):
        if edata.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        cond = edata.get("call_condition", "")
        order = edata.get("call_order")
        concurrency = edata.get("concurrency", "")
        label_parts = []
        if order is not None:
            label_parts.append(f"#{order}")
        if cond:
            label_parts.append(cond)
        label = " ".join(label_parts) if label_parts else ""

        # Edge color based on concurrency type
        edge_color = "#888888"
        edge_width = 1
        if concurrency == "thread_spawn":
            edge_color = "#ff5252"  # red
            edge_width = 2
        elif concurrency == "spawn_target":
            edge_color = "#2196f3"  # blue
            edge_width = 2
        elif concurrency == "goroutine":
            edge_color = "#ff9800"  # orange
            edge_width = 2
        elif concurrency == "callback":
            edge_color = "#9c27b0"  # purple
            edge_width = 1.5

        vis_edges.append({
            "from": u,
            "to": v,
            "label": label if len(label) <= 30 else label[:27] + "...",
            "arrows": "to",
            "font": {"size": 10, "align": "middle"},
            "color": {"color": edge_color, "highlight": "#2196f3"},
            "dashes": bool(cond),
            "width": edge_width,
        })

    # Escape < > to prevent JSON-in-<script> XSS: if a node name contains
    # </script>, the HTML parser closes the script tag and the payload
    # executes. json.dumps(ensure_ascii=False) does NOT escape < > &.
    nodes_json = json.dumps(vis_nodes, ensure_ascii=False).replace("<", "\\u003c")
    edges_json = json.dumps(vis_edges, ensure_ascii=False).replace("<", "\\u003c")

    # Community grouping info for collapse/expand
    comm_groups = defaultdict(list)
    for nid, ndata in G.nodes(data=True):
        cid = ndata.get("community_id", "")
        if cid:
            comm_groups[cid].append(nid)

    communities_json = json.dumps(
        [{"id": cid, "nodes": nids} for cid, nids in comm_groups.items()],
        ensure_ascii=False).replace("<", "\\u003c")

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{safe_title}</title>
  <script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
  <style>
    body {{ font-family: sans-serif; margin: 0; padding: 10px; }}
    #toolbar {{ padding: 5px 10px; background: #f5f5f5; border-bottom: 1px solid #ccc; display: flex; gap: 10px; align-items: center; }}
    #search {{ width: 200px; padding: 4px 8px; border: 1px solid #ccc; border-radius: 4px; }}
    #mynetwork {{ width: 100%; height: 88vh; border: 1px solid #ccc; }}
    .legend {{ position: absolute; top: 50px; right: 10px; background: white; padding: 10px;
               border: 1px solid #ccc; border-radius: 4px; font-size: 12px; z-index: 1; }}
    .legend-item {{ margin: 3px 0; }}
    .legend-dot {{ display: inline-block; width: 12px; height: 12px; border-radius: 50%; margin-right: 5px; }}
    button {{ padding: 4px 10px; border: 1px solid #ccc; border-radius: 4px; cursor: pointer; background: white; }}
    button:hover {{ background: #e3f2fd; }}
  </style>
</head>
<body>
  <div id="toolbar">
    <input type="text" id="search" placeholder="Search nodes (name/domain/label)..." oninput="doSearch()">
    <button onclick="resetSearch()">Reset</button>
    <button onclick="collapseAll()">Collapse Communities</button>
    <button onclick="expandAll()">Expand All</button>
    <span style="margin-left:auto; font-size:12px; color:#666">{G.number_of_nodes()} nodes, {G.number_of_edges()} edges</span>
  </div>
  <div id="mynetwork"></div>
  <div class="legend">
    <div class="legend-item"><span class="legend-dot" style="background:#4caf50"></span>API_entry</div>
    <div class="legend-item"><span class="legend-dot" style="background:#ff9800"></span>out_end</div>
    <div class="legend-item"><span class="legend-dot" style="background:#ff5252"></span>unknown_end</div>
    <div class="legend-item"><span class="legend-dot" style="background:#2196f3"></span>thread_processor</div>
    <div class="legend-item"><span class="legend-dot" style="background:#9c27b0"></span>callback_func</div>
    <div class="legend-item"><span class="legend-dot" style="background:#00bcd4"></span>constructor</div>
    <div class="legend-item"><span class="legend-dot" style="background:#e0e0e0"></span>regular</div>
    <div class="legend-item" style="margin-top:8px">Dashed = conditional</div>
    <div class="legend-item"><span style="color:#ff5252">━━</span> thread_spawn</div>
    <div class="legend-item"><span style="color:#2196f3">━━</span> spawn_target</div>
    <div class="legend-item"><span style="color:#ff9800">━━</span> goroutine</div>
    <div class="legend-item"><span style="color:#9c27b0">━━</span> callback</div>
  </div>
  <script>
    var allNodes = {nodes_json};
    var allEdges = {edges_json};
    var communities = {communities_json};
    var nodes = new vis.DataSet(allNodes);
    var edges = new vis.DataSet(allEdges);
    var container = document.getElementById('mynetwork');
    var data = {{nodes: nodes, edges: edges}};
    var options = {{
      physics: {{
        solver: 'forceAtlas2Based',
        forceAtlas2Based: {{ gravitationalConstant: -50, springLength: 100 }},
        stabilization: {{ iterations: 100 }}
      }},
      interaction: {{ hover: true, tooltipDelay: 200, navigationButtons: true, keyboard: true }},
      layout: {{ improvedLayout: true }}
    }};
    var network = new vis.Network(container, data, options);

    // Search: highlight matching nodes, dim others
    function doSearch() {{
      var q = document.getElementById('search').value.toLowerCase();
      if (!q) {{ resetSearch(); return; }}
      var updates = [];
      allNodes.forEach(function(n) {{
        var match = (n.label || '').toLowerCase().includes(q) ||
                    (n.title || '').toLowerCase().includes(q);
        updates.push({{id: n.id, opacity: match ? 1.0 : 0.15}});
      }});
      nodes.update(updates);
    }}

    function resetSearch() {{
      document.getElementById('search').value = '';
      var updates = [];
      allNodes.forEach(function(n) {{ updates.push({{id: n.id, opacity: 1.0}}); }});
      nodes.update(updates);
    }}

    // Community collapse/expand
    var collapsed = new Set();
    function collapseAll() {{
      communities.forEach(function(c) {{
        if (c.nodes.length > 1) {{
          collapsed.add(c.id);
          c.nodes.forEach(function(nid, i) {{
            nodes.update({{id: nid, hidden: true}});
          }});
        }}
      }});
    }}

    function expandAll() {{
      collapsed.clear();
      allNodes.forEach(function(n) {{ nodes.update({{id: n.id, hidden: false}}); }});
    }}

    // Double-click community node to toggle
    network.on("doubleClick", function(params) {{
      if (params.nodes.length === 1) {{
        var nid = params.nodes[0];
        var nd = nodes.get(nid);
        // Toggle: show/hide connected nodes
        var connected = network.getConnectedNodes(nid);
        if (connected.length > 0) {{
          var isHidden = nodes.get(connected[0]).hidden;
          connected.forEach(function(cid) {{
            nodes.update({{id: cid, hidden: !isHidden}});
          }});
        }}
      }}
    }});
  </script>
</body>
</html>"""

    Path(output_path).write_text(html, encoding="utf-8")




def _write_index_html(html_dir: str, links: list, index_path: str, total_nodes: int):
    """Write an index HTML that links to per-domain graph pages."""
    import html as html_module
    items = []
    for domain, href, count in links:
        # domain derives from a directory path — escape for HTML text and
        # keep the href restricted to the sanitized filename.
        items.append(f'<li><a href="{html_module.escape(href, quote=True)}">'
                     f'{html_module.escape(domain)}</a> ({count} nodes)</li>')

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Call Graph — Domain Index</title>
  <style>
    body {{ font-family: sans-serif; margin: 20px; }}
    h1 {{ color: #333; }}
    ul {{ list-style: none; padding: 0; }}
    li {{ margin: 8px 0; }}
    a {{ color: #2196f3; text-decoration: none; font-size: 16px; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <h1>Call Graph — {total_nodes} nodes across {len(links)} domains</h1>
  <p>Click a domain to view its invocation graph:</p>
  <ul>
    {"".join(items)}
  </ul>
</body>
</html>"""

    Path(index_path).write_text(html, encoding="utf-8")




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
        constraints = html_module.escape(ndata.get("api_constraints", ""))
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




def cmd_export_html(args):
    """Export the invocation graph as an interactive HTML file."""
    graph_dir = args.graph

    # If no explicit format, ask the user to choose
    fmt = getattr(args, "format", None)
    if not fmt:
        print("Choose HTML export format:")
        print()
        print("  [1] vis-network  — Interactive force-directed graph (drag/zoom/pan)")
        print("      Pros: Fully interactive, real-time physics layout, click-to-explore nodes,")
        print("            handles large graphs (500+ nodes), best for exploration & debugging")
        print("      Cons: Requires internet for CDN JS, layout is non-deterministic,")
        print("            heavier page weight (~2MB with library)")
        print()
        print("  [2] mermaid      — Static rendered diagram with Tailwind CSS styling")
        print("      Pros: Self-contained (no external JS), deterministic layout, lightweight,")
        print("            easy to commit to Git/docs, renders in GitHub Markdown previews")
        print("      Cons: Limited interactivity (hover only), struggles with 200+ nodes,")
        print("            layout can be messy for dense graphs")
        print()
        try:
            choice = input("Enter choice [1 or 2, default=1]: ").strip()
        except EOFError:
            choice = "1"
        if choice == "2":
            fmt = "mermaid"
        else:
            fmt = "vis-network"
        print(f"Selected: {fmt}")

    if fmt == "mermaid":
        output = args.output if args.output else os.path.join(graph_dir, "code2database_mermaid.html")
    else:
        output = args.output if args.output else os.path.join(graph_dir, "callgraph.html")
    max_nodes = args.max_nodes if args.max_nodes else 500

    G = _load_full_graph(graph_dir)

    # Load community assignments if available (for collapse/expand feature)
    comm_path = os.path.join(graph_dir, ".code2database_communities.json")
    if os.path.exists(comm_path):
        try:
            comm_data = json.loads(Path(comm_path).read_text(encoding="utf-8"))
            for comm in comm_data.get("communities", []):
                cid = comm.get("id", "")
                for nid in comm.get("node_ids", []):
                    if nid in G:
                        G.nodes[nid]["community_id"] = cid
        except (json.JSONDecodeError, OSError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    domain_nodes = defaultdict(list)
    for nid, ndata in G.nodes(data=True):
        domain_nodes[ndata.get("domain", "root")].append((nid, ndata))

    total_nodes = G.number_of_nodes()

    if fmt == "mermaid":
        _export_mermaid(G, output, max_nodes, domain_nodes, total_nodes)
    else:
        _export_vis_network(G, output, max_nodes, domain_nodes, total_nodes)




def cmd_export_obsidian(args):
    """Export invocation graph as Obsidian vault with [[links]] = calls."""
    G = _load_full_graph(args.graph)
    output_dir = args.output or os.path.join(args.graph, "obsidian-vault")
    os.makedirs(output_dir, exist_ok=True)

    # Generate notes
    note_count = 0
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty") or ndata.get("domain", "") == "external":
            continue
        name = ndata.get("name", nid.split("_")[-1] if "_" in nid else nid)
        domain = ndata.get("domain", "")
        labels = ndata.get("labels", [])
        sig = ndata.get("signature", "")
        source = ndata.get("source_file", "")
        line = ndata.get("line", 0)

        # YAML frontmatter
        frontmatter = "---\n"
        frontmatter += f"id: \"{nid}\"\n"
        frontmatter += f"domain: \"{domain}\"\n"
        if labels:
            frontmatter += f"labels: {json.dumps(labels)}\n"
        if sig:
            sig_safe = json.dumps(sig)  # Proper JSON escaping handles \n, \t, :, #, etc.
            frontmatter += f'signature: {sig_safe}\n'
        frontmatter += f"source: \"{source}:{line}\"\n"
        if ndata.get("entry_score"):
            frontmatter += f"entry_score: {ndata['entry_score']}\n"
        frontmatter += "---\n\n"

        # Body
        body = f"# {name}\n\n"
        if ndata.get("api_constraints"):
            body += f"**Constraints**: {ndata['api_constraints']}\n\n"

        # Callers (backlinks, call edges only)
        callers = [c for c in G.predecessors(nid)
                   if (G.get_edge_data(c, nid) or {}).get("relation") not in ("CONTAINS", "IMPORTS")]
        if callers:
            body += "## Called by\n"
            for c in callers:
                cname = G.nodes[c].get("name", c)
                body += f"- [[{cname}]]\n"
            body += "\n"

        # Callees (forward links, call edges only)
        callees = [s for s in G.successors(nid)
                   if (G.get_edge_data(nid, s) or {}).get("relation") not in ("CONTAINS", "IMPORTS")]
        if callees:
            body += "## Calls\n"
            for s in callees:
                sname = G.nodes[s].get("name", s)
                ed = G.get_edge_data(nid, s) or {}
                cond = ed.get("call_condition", "")
                order = ed.get("call_order", "")
                note = f" (order={order})" if order else ""
                note += f" [{cond}]" if cond else ""
                body += f"- [[{sname}]]{note}\n"
            body += "\n"

        # Write to domain subfolder (sanitize to prevent path traversal)
        safe_domain = re.sub(r'[^\w.]', '_', domain).replace(".", "/")
        # Resolve and verify the path stays within output_dir
        note_dir = os.path.normpath(os.path.join(output_dir, safe_domain))
        if not note_dir.startswith(os.path.normpath(output_dir)):
            note_dir = output_dir
        os.makedirs(note_dir, exist_ok=True)
        safe_name = name.replace("/", "_").replace("\\", "_")
        note_path = os.path.join(note_dir, f"{safe_name}.md")
        Path(note_path).write_text(frontmatter + body, encoding="utf-8")
        note_count += 1

    print(f"Obsidian vault exported: {note_count} notes → {output_dir}")


