# Web UI Reference

The Web UI (`web-ui` command) provides an interactive browser-based interface for exploring the code graph. It renders with cytoscape.js 3.28.1 (served locally with a CDN fallback).

## Starting

```bash
python3 scripts/code2database_builder.py web-ui --graph <dir> [--port 8765]
```

Binds to 127.0.0.1 by default (override with `C2D_WEB_UI_HOST`).

## HTTP API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Serve the single-file HTML UI |
| `/static/cytoscape.min.js` | GET | Local cytoscape.js asset (CDN fallback) |
| `/api/graph/summary` | GET | Node/edge/community counts + source-freshness (staleness badge) |
| `/api/node/<id>` | GET | Single node details + `related_memories` (symbol-grounded veteran Q&A) |
| `/api/neighbors/<id>?depth=<n>` | GET | BFS neighborhood (200-node cap, `truncated` flag) |
| `/api/path?from=<id>&to=<id>` | GET | Shortest path (call edges only, depth ≤ 10) |
| `/api/highlight-path` | GET/POST | Get/set highlighted path (API present; no UI yet) |
| `/api/communities` | GET | List all communities |
| `/api/community/<id>` | GET | Nodes in a specific community (limit 100) |
| `/api/search?q=<query>` | GET | Case-insensitive name search (substring; results carry domain + file:line for disambiguation) |
| `/api/degrees` | GET | Degree map for node sizing (precomputed at reload) |
| `/api/callers/<id>` | GET | Direct callers (call edges, with locations) |
| `/api/callees/<id>` | GET | Direct callees (call edges, with locations) |
| `/api/cycles` | GET | Cyclic call edges (depth-5 reachability, limit 50) |
| `/api/impact?node=<id>` | GET | Reverse-reachability impact analysis (depth ≤ 5, top 200 shown, `truncated` reported) |
| `/api/reload` | POST | Reload graph from disk |
| `/api/code?node=<id>` | GET | Source code snippet (±10 lines, 4 KB cap) |
| `/api/domains` | GET | List all domains with counts |
| `/api/suggestions` | GET | Proactive analysis suggestions |
| `/api/tour` | GET | Guided codebase tour markdown |
| `/api/brief` | GET | Project brief (knowledge/brief.json, rendered) |
| `/api/architecture` | GET | Architecture narrative (ARCHITECTURE_FLOWS.md) |
| `/api/memory/search?q=&author=&symbol=&top=` | GET | Veteran Q&A search (FTS5 over memory.db; empty query = weight-ranked digest) |
| `/api/memory/lineage` | GET | Memory split/merge lineage graph |
| `/api/memory/authors` | GET | Authors index for the memory filter |

## Frontend Features

- **Cytoscape.js 3.28.1** — canvas renderer, served locally for offline use
- **Five layouts** — flow (breadthfirst), force (cose), rings (concentric), circle, grid — auto-tuned by node count
- **Real communities** — build-time Leiden communities (`.code2database_communities.json`) with human-readable labels; falls back to domains when absent
- **Search disambiguation** — multiple matches open a results list (name + file:line); single match jumps directly
- **Focus+context fading** — click node, non-neighbors fade; call/caller edges stay visible
- **Cycle highlight toggle** — cyclic call edges restyled in place
- **Community legend fade** — click a legend entry to fade that community
- **Node-label filter** — fade nodes by label (API_entry / thread / callback / default)
- **Degrees** — node size follows degree (precomputed; no per-click full-graph scan)
- **Staleness badge** — source-vs-graph freshness in the stats bar
- **Truncation surfacing** — neighbors 200-node cap and impact >200/depth-5 limits are shown, not silent
- **Dark/light mode**, **PNG export**, **right-click context menu**
- **Incremental sync** — `syncCyFromModel()` adds/removes changed nodes AND edges (stable `src->tgt` edge ids), restyles in place
- **Project context panels** — Brief, Architecture narrative, Memory Q&A (author filter, lineage view)

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `/` | Focus search bar |
| `f` | Fit view (`cy.fit(undefined, 42)`) |
| `?` | Help modal |
| `d` | Toggle dark/light |
| `p` | Export PNG |
| `+` / `-` | Zoom in / out |
| `1`–`5` | Set exploration depth |
| `Enter` | Re-focus active node at current depth |
| `Escape` | Clear fading + close modals/menus/search results |

## Mouse Interactions

- **Left click node** — focus node + fade non-neighbors
- **Left click background** — clear fading
- **Right click node** — context menu (Focus / Expand depth 2 / Impact / View Code / Copy ID)
- **Wheel** — zoom (cytoscape native; labels hidden below zoom 0.5)

## Performance Notes

- Degrees are precomputed once per reload (two `GROUP BY` queries on the SQLite backend) instead of per-request full-graph scans
- Cycle detection snapshots adjacency under the lock, then runs per-edge BFS outside it
- Incremental sync avoids full re-render

## Limitations

- Read-only (no graph editing via web UI)
- Single graph instance per server
- No SSL/TLS
- `/api/path` and `/api/highlight-path` have no UI controls yet
- No minimap / tooltips (despite earlier README claims — removed)
