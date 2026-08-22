# Web UI Reference

The Web UI (`web-ui` command) provides an interactive browser-based interface for exploring the code graph. It loads cytoscape.js 3.28.1 for dense-graph rendering.

## Starting

```bash
python3 scripts/code2database_builder.py web-ui --graph <dir> [--port 8765]
```

## HTTP API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Serve the single-file HTML UI |
| `/api/graph/summary` | GET | Node/edge/community counts |
| `/api/initial-view` | GET | Top communities' representative nodes + inter-community edges |
| `/api/neighbors/<id>?depth=<n>` | GET | BFS neighborhood of a node |
| `/api/node/<id>` | GET | Single node details |
| `/api/search?q=<query>` | GET | FTS5-powered full-text search |
| `/api/search-nodes?q=<query>` | GET | Search with pre-loaded neighbor edges |
| `/api/path?from=<id>&to=<id>` | GET | Shortest path between two nodes |
| `/api/communities` | GET | List all communities |
| `/api/community/<id>` | GET | Nodes in a specific community |
| `/api/highlight-path` | GET/POST | Get/set highlighted path |
| `/api/reload` | POST | Reload graph from disk |
| `/api/code?node=<id>` | GET | Source code snippet for a node |
| `/api/domains` | GET | List all domains with counts |
| `/api/impact?node=<id>` | GET | Reverse-reachability impact analysis |
| `/api/suggestions` | GET | Proactive analysis suggestions |
| `/api/tour` | GET | Guided codebase tour markdown |

## Frontend Features

- **Cytoscape.js 3.28.1** — inlined for single-file offline use
- **Three layout algorithms** — flow (breadthfirst), rings (concentric), force (cose)
- **Focus+context fading** — click node, non-neighbors fade to 0.15 opacity
- **Edge bundling** — curve-style: bezier for parallel edges
- **Community compound nodes** — group by domain, click to collapse/expand
- **LOD label hiding** — labels hidden when zoom < 0.3
- **Edge `call_condition` labels** — toggle button to show/hide conditions
- **Selector-based edge filter** — hide edge types via `cy.elements('[relation = "TYPE"]').toggleClass('hidden')`
- **Incremental sync** — `syncCyFromModel()` only adds/removes changed elements

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `/` | Focus search bar |
| `f` | Fit view (`cy.fit(undefined, 42)`) |
| `Escape` | Clear focus+context fading |
| `Alt+Left` | Navigate back |
| `Alt+Right` | Navigate forward |

## Mouse Interactions

- **Left click node** — focus node + fade non-neighbors
- **Left click background** — clear fading
- **Right click** — context menu (Expand/Collapse, Focus, Copy ID)
- **Hover node** — tooltip with name/domain/labels
- **Hover edge** — tooltip with caller→callee + condition
- **Wheel** — zoom (cytoscape native)

## Performance Notes

- Cytoscape.js 3.28.1 (~400KB) inlined for offline use
- Incremental sync avoids full re-render
- Selector-based edge filter is O(1) toggle, no re-render
- LOD label hiding reduces DOM nodes at low zoom

## Limitations

- Read-only (no graph editing via web UI)
- Single graph instance per server
- No SSL/TLS
- No cytoscape extensions loaded
