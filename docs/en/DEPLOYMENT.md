# Deployment Guide

How to run Code2Database as a stable, service-grade component: install
paths, MCP service deployment, the sync daemon, backup/restore, and
health monitoring.

## Deployment Modes

| Mode | Command surface | When to use |
|------|-----------------|-------------|
| CLI (interactive) | `c2d`, `code2database-builder`, `code2database-scanner` | Engineering workstations, CI jobs, scripted pipelines |
| MCP over stdio | `serve --graph <dir>` | Local LLM agents (Claude Desktop, Cursor local mode) |
| MCP over HTTP | `serve --transport http ...` | Shared/team access behind TLS + token auth |
| Sync daemon | `daemon-start --graph <dir> --source <src>` | Keeping a graph current while engineers edit code |
| Web UI | `web-ui --graph <dir>` | Interactive browsing of a built graph |

## Prerequisites

- Python 3.10+ (matches `pyproject.toml`)
- Core install: `pip install code2database` (or `pip install .` from the
  repository) — pulls `networkx` and the `tree-sitter` grammars
- Optional capabilities ship as wheel extras (install only what you use):

| Extra | Provides |
|-------|----------|
| `clang` | cgdb typed backend (vtable dispatch, CFG, data flow) |
| `solver` | sound path feasibility via z3 |
| `community` | cross-domain Leiden communities |
| `daemon` | file watching on macOS/Windows (Linux uses built-in inotify) |
| `resources` | cross-platform memory telemetry |
| `streaming` | streaming parse of very large globals.json |
| `neural` | optional neural embedding provider |

```bash
# Examples
pip install code2database                     # core
pip install "code2database[clang,solver]"     # typed backend + solver
pip install "code2database[daemon,resources]" # host-style deployment
```

## Install Paths and Entry Points

The wheel installs three console entries (all wrap the same code):

| Entry | Role |
|-------|------|
| `c2d` | Umbrella lifecycle: setup → session → ask → capture (+ freshen/report) |
| `code2database-builder` | Full 253-command builder CLI |
| `code2database-scanner` | The 8 scanner verbs |

A repository checkout works the same way without installation
(`python3 scripts/code2database_builder.py ...` with `PYTHONPATH`-free
execution from the repo root).

## First Build per Project

```bash
c2d setup --source /path/to/project --graph code2db-out/
```

`setup` delegates to `make`: environment check → scan → build → derived
artifacts → exports. The graph directory (`code2db-out/`) is
self-contained: one SQLite database, the memory store, the knowledge
brief, indexes and history — all inside it.

## MCP Service Deployment

### stdio (local agents)

Point the agent's MCP configuration at the builder with `serve` and the
graph directory. No network surface is opened.

### HTTP (shared access)

```bash
code2database-builder serve --graph /opt/Code2Database/code2db-out \
    --transport http --host 127.0.0.1 --port 8765 \
    --token <strong-secret> --read-only --max-clients 32
```

- Token auth: `--token` or `C2D_MCP_TOKEN`; every request must carry
  `Authorization: Bearer <token>`
- `--read-only` hides write tools (memory saves, database transactions,
  token edits) from remote clients
- TLS via `--tls-cert/--tls-key`, or terminate TLS at nginx
  (`deploy/mcp-nginx.conf`: rate limiting, SSE-compatible proxying)
- The server refuses to bind a public interface without a token

For production, run under systemd with `deploy/c2d-mcp.service`
(hardened: `NoNewPrivileges`, `ProtectSystem=strict`, `MemoryMax`) and
front it with the provided nginx configuration:

```bash
sudo cp deploy/c2d-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now c2d-mcp
curl http://localhost:8765/health   # deployment smoke probe
```

## Daemon Service Deployment

The daemon watches source files and syncs the graph inside
transactions (snapshot + rollback on failure). Run it under
systemd with `deploy/c2d-daemon.service`:

```bash
sudo cp deploy/c2d-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now c2d-daemon
```

Operational facts:

- The control socket is `$TMPDIR/code2database-daemon-<hash>.sock`
  (hash of the absolute graph dir); the service unit pins `TMPDIR=/tmp`
  and keeps `PrivateTmp=false` so shell commands can reach it
- Control from any shell: `daemon-status`, `daemon-pause`,
  `daemon-resume`, `daemon-force-refresh`, `daemon-wait-sync`,
  `daemon-logs`, `daemon-list-projects`
- State lives in `<graph_dir>/.daemon_status.json`; a crashed daemon is
  detected on restart, its pending events carried over, and the
  recovery bulk sync deferred until the startup grace window ends
  (crash-loop protection)
- Above the events/minute threshold a circuit breaker switches to
  "wait + bulk rebuild" instead of per-file syncing

## Backup and Restore

The graph directory is the unit of backup. Copy it as a whole:

| Path | Content |
|------|---------|
| `code2database.db` | The graph database (functions, edges, cgdb layers, edit trail, change rows) |
| `graph_versions.db` | Accumulated build history (counts per build/sync) |
| `memory/memory.db` | The shared memory store — **the hardest artifact to rebuild** (accumulated veteran knowledge) |
| `knowledge/brief.json` | The curated project brief |
| `.code2database_manifest.json` | Source fingerprints and the source commit anchor |

```bash
# Cold backup (daemon stopped or after daemon-pause)
rsync -a code2db-out/ backup/code2db-out/

# Point-in-time recovery without external backups
code2database-builder tx-list-snapshots --graph code2db-out/
code2database-builder tx-restore --graph code2db-out/ --snapshot <id>
```

Machine migration: copy the graph directory and the source tree; the
manifest records `source_root` — re-run `c2d freshen` first, then a
sync path if the source moved. The cross-project global knowledge store
lives in `~/.code2database_global_kb/` — back it up separately if you
share principles across projects (`kb-global-share` /
`kb-global-import` move bundles between machines).

## Health Monitoring

`doctor` is the one-shot probe — designed to sit in deployment
pipelines and monitoring cron:

```bash
code2database-builder doctor --graph code2db-out/ --json
echo $?   # 0 = healthy, 1 = warnings, 2 = failure
```

It checks SQLite integrity and foreign keys, schema versions, content
counts, source freshness, the memory store, the knowledge brief and the
daemon state. For drift over time, `graph-history` reads the
accumulated version rows (node/edge counts per build and sync), and
`graph-provenance` reports which source commit and tool version
produced the current database.

## Data Sensitivity

Graph output mirrors the scanned source: function names, file paths
and — with the clang backend — string literals. Treat the graph
directory and its backups as source-code-sensitive. See
[SECURITY.md](../../SECURITY.md) for the full policy.
