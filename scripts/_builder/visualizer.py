#!/usr/bin/env python3
"""HTML visualization exporter for invocation graphs.

Generates a standalone HTML file with embedded vis-network for interactive
graph visualization. No external dependencies required.
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional


# Minimal vis-network CDN for embedding
_VIS_NETWORK_CDN = "https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"

_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{title}</title>
    <style>
        body {{ font-family: monospace; margin: 0; padding: 0; }}
        #search {{ position: fixed; top: 10px; left: 10px; z-index: 1000; }}
        #search input {{ width: 300px; padding: 6px; font-size: 14px; border: 1px solid #ccc; }}
        #network {{ width: 100vw; height: 100vh; border: 1px solid lightgray; }}
        #info {{ position: fixed; bottom: 10px; left: 10px; background: white; padding: 8px;
                 border: 1px solid #ccc; max-width: 400px; font-size: 12px; z-index: 1000;
                 display: none; }}
        #legend {{ position: fixed; top: 10px; right: 10px; background: white; padding: 8px;
                   border: 1px solid #ccc; font-size: 12px; z-index: 1000; }}
    </style>
</head>
<body>
    <div id="search"><input type="text" placeholder="Search nodes..." id="searchInput"></div>
    <div id="network"></div>
    <div id="info"></div>
    <div id="legend">
        <b>Domain Colors:</b><br>
        {legend}
        <br><br>
        <b>Controls:</b> Scroll=Zoom, Drag=Pan, Click=Info
    </div>
    <script src="{vis_cdn}"></script>
    <script>
    const nodes = new vis.DataSet({nodes_json});
    const edges = new vis.DataSet({edges_json});
    const container = document.getElementById('network');
    const data = {{ nodes, edges }};
    const options = {{
        nodes: {{
            shape: 'box',
            font: {{ size: 11 }},
            margin: 5,
            borderWidth: 1,
        }},
        edges: {{
            arrows: {{ to: {{ enabled: true, scaleFactor: 0.5 }} }},
            smooth: {{ type: 'continuous' }},
            color: {{ color: '#888', highlight: '#333' }},
        }},
        physics: {{
            solver: 'forceAtlas2Based',
            forceAtlas2Based: {{ gravitationalConstant: -80, springLength: 100 }},
            stabilization: {{ iterations: 100 }},
        }},
        interaction: {{ hover: true, tooltipDelay: 200 }},
    }};
    const network = new vis.Network(container, data, options);

    // Node click handler
    network.on('click', function(params) {{
        const info = document.getElementById('info');
        if (params.nodes.length > 0) {{
            const nodeId = params.nodes[0];
            const node = nodes.get(nodeId);
            info.style.display = 'block';
            info.innerHTML = '<b>' + node.label + '</b><br>' +
                'Domain: ' + (node.domain || 'N/A') + '<br>' +
                'File: ' + (node.source || 'N/A') + '<br>' +
                (node.signature ? 'Sig: ' + node.signature + '<br>' : '') +
                (node.labels ? 'Labels: ' + node.labels.join(', ') : '');
        }} else {{
            info.style.display = 'none';
        }}
    }});

    // Search handler
    document.getElementById('searchInput').addEventListener('input', function(e) {{
        const query = e.target.value.toLowerCase();
        if (!query) {{
            network.selectNodes([]);
            return;
        }}
        const matches = nodes.getIds().filter(id => {{
            const n = nodes.get(id);
            return n.label.toLowerCase().includes(query) ||
                   (n.domain && n.domain.toLowerCase().includes(query));
        }});
        if (matches.length > 0) {{
            network.selectNodes(matches.slice(0, 20));
            network.focus(matches[0], {{ scale: 1.5 }});
        }}
    }});
    </script>
</body>
</html>"""


def _assign_domain_colors(domains):
    """Assign distinct colors to domains."""
    colors = [
        '#4e79a7', '#f28e2b', '#e15759', '#76b7b2', '#59a14f',
        '#edc948', '#b07aa1', '#ff9da7', '#9c755f', '#bab0ac',
        '#86bcb6', '#8cd17d', '#b6992d', '#499894', '#e15759',
        '#f1ce63', '#a0cbe8', '#ffbe7d', '#d37295', '#fabfd2',
    ]
    domain_colors = {}
    for i, domain in enumerate(sorted(domains)):
        domain_colors[domain] = colors[i % len(colors)]
    return domain_colors


def export_domain_overview(G, output_path: str, source_root: str = "",
                           max_nodes: int = 200):
    """Export a domain-level overview graph as HTML.

    Each node represents a domain, edges represent cross-domain calls.
    """
    from collections import defaultdict

    # Build domain-level graph
    domain_calls = defaultdict(int)  # (caller_domain, callee_domain) -> count
    domain_funcs = defaultdict(int)

    for nid, nd in G.nodes(data=True):
        domain = nd.get("domain", "root")
        if not nd.get("is_empty", False):
            domain_funcs[domain] += 1

    for u, v, ed in G.edges(data=True):
        if ed.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        u_domain = G.nodes[u].get("domain", "root")
        v_domain = G.nodes[v].get("domain", "root")
        if u_domain != v_domain:
            domain_calls[(u_domain, v_domain)] += 1

    domain_colors = _assign_domain_colors(domain_funcs.keys())

    # Build vis-network data
    vis_nodes = []
    for domain, count in sorted(domain_funcs.items(), key=lambda x: -x[1])[:max_nodes]:
        vis_nodes.append({
            "id": domain,
            "label": domain.split(".")[-1] if "." in domain else domain,
            "title": f"{domain}\n{count} functions",
            "domain": domain,
            "color": {"background": domain_colors[domain], "border": "#333"},
            "size": max(15, min(50, count / 2)),
            "font": {"color": "#fff" if count > 10 else "#000"},
        })

    vis_edges = []
    for (caller, callee), count in sorted(domain_calls.items(), key=lambda x: -x[1])[:500]:
        if caller in {n["id"] for n in vis_nodes} and callee in {n["id"] for n in vis_nodes}:
            vis_edges.append({
                "from": caller,
                "to": callee,
                "value": count,
                "title": f"{caller} → {callee}: {count} calls",
                "width": max(1, min(8, count / 10)),
            })

    _write_html(vis_nodes, vis_edges, output_path,
                f"Domain Overview — {source_root}", domain_colors)


def export_function_detail(G, output_path: str, domain: str,
                           source_root: str = "", max_nodes: int = 100):
    """Export a function-level detail graph for a specific domain."""
    domain_nodes = {nid: nd for nid, nd in G.nodes(data=True)
                    if nd.get("domain") == domain and not nd.get("is_empty", False)}

    if not domain_nodes:
        return

    domain_colors = _assign_domain_colors([domain])
    color = domain_colors[domain]

    vis_nodes = []
    for nid, nd in sorted(domain_nodes.items(),
                          key=lambda x: -len(list(G.successors(x[0]))))[:max_nodes]:
        labels = nd.get("labels", [])
        is_api = "API_entry" in labels
        is_thread = "thread_processor" in labels
        node_color = "#e15759" if is_api else "#59a14f" if is_thread else color

        vis_nodes.append({
            "id": nid,
            "label": nd.get("name", nid),
            "title": f"{nd.get('name', nid)}\n{nd.get('signature', '')}",
            "domain": domain,
            "source": nd.get("source_file", ""),
            "signature": nd.get("signature", ""),
            "labels": labels,
            "color": {"background": node_color, "border": "#333"},
        })

    node_ids = {n["id"] for n in vis_nodes}
    vis_edges = []
    for u, v, ed in G.edges(data=True):
        if u in node_ids and v in node_ids:
            if ed.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            vis_edges.append({
                "from": u,
                "to": v,
                "arrows": "to",
                "title": ed.get("call_condition", ""),
            })

    _write_html(vis_nodes, vis_edges, output_path,
                f"Function Detail — {domain}", domain_colors)


def _write_html(vis_nodes, vis_edges, output_path, title, domain_colors):
    """Write the HTML file from template."""
    legend_html = "<br>".join(
        f'<span style="color:{c}">■</span> {d}'
        for d, c in sorted(domain_colors.items())
    )

    html = _HTML_TEMPLATE.format(
        title=title,
        vis_cdn=_VIS_NETWORK_CDN,
        nodes_json=json.dumps(vis_nodes, ensure_ascii=False),
        edges_json=json.dumps(vis_edges, ensure_ascii=False),
        legend=legend_html,
    )

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    Path(output_path).write_text(html, encoding="utf-8")
    return len(vis_nodes)


def cmd_export_html(args):
    """Handle export-html command."""
    import networkx as nx
    from _builder.utils import _load_full_graph

    graph_dir = args.graph
    output = args.output
    mode = getattr(args, 'mode', 'domain')
    domain = getattr(args, 'domain', None)
    max_nodes = getattr(args, 'max_nodes', 200)

    G = _load_full_graph(graph_dir)
    source_root = ""
    # Try to get source_root from master file
    master_path = os.path.join(graph_dir, "code2database_master.json")
    if os.path.exists(master_path):
        with open(master_path, "r", encoding="utf-8") as f:
            master = json.load(f)
            source_root = master.get("source_root", "")

    if mode == "domain" or not domain:
        count = export_domain_overview(G, output, source_root, max_nodes)
        print(f"Exported domain overview: {count} domain nodes → {output}", file=sys.stderr)
    else:
        count = export_function_detail(G, output, domain, source_root, max_nodes)
        print(f"Exported function detail for '{domain}': {count} functions → {output}", file=sys.stderr)
