# Troubleshooting

Symptom-first guide for the failure modes a deployment actually hits.
Every section starts from what you observe, then names the command that
answers it.

## First Stop: the `doctor` Probe

One command checks SQLite integrity and foreign keys, schema versions,
content counts, source freshness, the memory store, the knowledge brief
and the daemon state:

```bash
code2database-builder doctor --graph code2db-out/ --json
echo $?   # 0 = healthy, 1 = warnings, 2 = failure
```

| Check | Reports |
|-------|---------|
| `database` | `PRAGMA integrity_check` + foreign-key violations |
| `schema` | stored schema versions vs the tool's current ones |
| `graph_content` | function/edge/file counts |
| `freshness` | source-tree drift since the last scan (new/changed/deleted) |
| `memory_store` | memory.db reachability and entry counts |
| `knowledge_brief` | brief.json presence (missing = warning) |
| `daemon` | whether the sync daemon is running |

Use the exit code in deployment smoke probes: anything other than 0
should page a human; 2 means data-level failure (integrity, missing
database).

## The Graph Says the Source Is Stale

Symptom: `session-init` or `c2d freshen` reports drifted files.

- Run `c2d freshen --graph code2db-out/` — it prints the recommended
  sync path (per-file sync, incremental re-scan, or full rebuild) with
  exit code semantics suited to scripts
- Freshness compares file fingerprints (mtime + size) against the
  manifest and the git HEAD against the recorded source commit — a
  `touch`-only change can look stale; content hashes (clang backend)
  are the stronger signal
- If a daemon is running, check `daemon-status` first: during its
  startup grace window the daemon holds events without syncing them

## Corrupt or Unreadable Database

Symptom: `doctor` fails `database`; queries error out.

```bash
code2database-builder tx-list-snapshots --graph code2db-out/
code2database-builder tx-restore --graph code2db-out/ --snapshot <id>
```

- Transaction snapshots capture the database + key JSONs before every
  multi-step write (`tx-begin`/`patch-from-diff`/...); the newest sane
  snapshot is usually minutes old
- WAL journals recover automatically on the next connection; a hard
  power loss mid-sync leaves the pre-transaction state
- Last resort: rebuild from source (`make`/`c2d setup`) — memory and
  the brief live OUTSIDE the graph database and survive rebuilds

## Sync Daemon Not Responding

Symptom: `daemon-status` cannot connect.

- The control socket is `$TMPDIR/code2database-daemon-<hash>.sock`
  (hash of the absolute graph dir); confirm both sides compute the
  same `--graph` path — a relative path from a different working
  directory hashes differently
- Under systemd, `PrivateTmp=true` would hide the socket from user
  shells — the shipped `deploy/c2d-daemon.service` pins `TMPDIR=/tmp`
  with `PrivateTmp=false` for exactly this reason
- A stale socket file after a crash is cleaned up on next start;
  `daemon-logs` shows the crash context, `.daemon_status.json` the
  last recorded state
- If events piled up during downtime, the daemon defers the recovery
  bulk sync until its startup grace window ends — watch `daemon-status`
  instead of forcing an immediate rebuild

## cgdb Layer Missing or Degraded

Symptom: `cgdb-*` queries return empty; `doctor` is silent about it.

- A marker file `.code2database_cgdb_export_failed.json` in the graph
  directory records a failed cgdb export — delete it and re-run the
  build after addressing the recorded stage
- The cgdb layer requires the clang extraction backend
  (`--extraction-backend clang`, or `auto` with libclang installed);
  tree-sitter-only builds are fully functional but have no cgdb tables
- `pip install "code2database[clang]"` provides libclang

## Memory and the Brief Disagree with the Graph

Symptom: answers cite functions that no longer exist.

- Memory entries whose `node_ids` vanish from the graph are demoted
  automatically (the daemon re-validates after each sync); check
  `manage-memory --action query` for demoted entries and re-ground
  with `save-memory --correct`
- `brief-validate` warns when brief statistics drift more than 20% from
  the graph, and when the brief exceeds its size budget (overflow
  belongs in memory)
- Repeatedly-missed questions appear as known-unknowns in
  `session-init` — capture them with `c2d capture` instead of leaving
  them unanswered

## MCP or Web UI Refuses to Start

Symptom: `serve` or `web-ui` exits immediately.

- Binding a public interface without `--token` is refused by design —
  set `--token` or `C2D_MCP_TOKEN`
- Port already in use: `--port` conflicts surface in the startup error;
  check `ss -ltnp | grep 8765`
- `--read-only` hides write tools by design — clients complaining about
  missing tools are connecting to a read-only deployment
- The HTTP health endpoint (`/health`) answers without auth and is the
  right liveness probe for orchestrators

## Scanner Fails on a Language or Runs Out of Memory

Symptom: scan aborts, or a language's functions never appear.

- A missing tree-sitter grammar makes that language silently empty —
  install the grammar package for the language you scan
- No profile for an exotic tree: `auto-profile` derives one; persistent
  misses should be handled by writing a profile
  (`docs/en/PROFILE_MANUAL.md`)
- MemoryGuard caps scans by system RAM by default — pass
  `--memory-limit` (MB) and raised `--memory-warn-threshold` /
  `--memory-crit-threshold` when running inside busy machines or CI

## Choosing a Re-Sync Path

| Situation | Command |
|-----------|---------|
| A few files changed, graph is SQLite-backed | `build-update` (per-file, transactional) |
| Continuous editing, want it automated | `daemon-start` |
| Branch switch or large structural churn | full `build` (or `make`) |
| Only derived artifacts (packs, summaries) stale | re-run the export/make derived steps |

When in doubt, `c2d freshen` names the recommended path for the current
drift — it exists precisely to make this decision scriptable.
