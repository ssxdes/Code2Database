# Ops Commands Reference (Tier 3)

This document covers the full syntax for all ops commands exposed by the `/Code2Database-ops` sub-skill: transactions, daemon, profile/doc-code, graph versioning, memory, exports, plugins, embeddings. **Read on demand only** — do not load this file into agent context unless you need detailed syntax for a specific command.

**Database write constraint**: any command marked **[write]** requires user confirmation before executing. See `SKILL_ops.md` Database Write Constraint section for the full report-and-wait protocol.

## Transactions

### `tx-begin`

Begin a transaction. Creates a snapshot of the current graph state and starts a Write-Ahead Log (WAL).

```bash
python3 scripts/code2database_builder.py tx-begin \
  --graph code2db-out/ \
  [--label "descriptive label"]
```

### `tx-commit`  [write]

Commit the current transaction. Snapshot + WAL entries are applied to the live DB. **Requires user confirmation** for write transactions.

```bash
python3 scripts/code2database_builder.py tx-commit \
  --graph code2db-out/ \
  [--yes]  # NOT recommended — bypasses confirmation
```

### `tx-rollback`

Rollback the current transaction. Restores the snapshot taken at `tx-begin`.

```bash
python3 scripts/code2database_builder.py tx-rollback \
  --graph code2db-out/
```

### `tx-status`

Show the current transaction status (active / committed / rolled-back, snapshot path, WAL entries count).

```bash
python3 scripts/code2database_builder.py tx-status \
  --graph code2db-out/
```

### `tx-snapshot`

Create a named snapshot of the current graph state (does not begin a transaction).

```bash
python3 scripts/code2database_builder.py tx-snapshot \
  --graph code2db-out/ \
  --name SNAPSHOT_NAME
```

### `tx-restore`

Restore the graph from a named snapshot. **Requires user confirmation** (overwrites current state).

```bash
python3 scripts/code2database_builder.py tx-restore \
  --graph code2db-out/ \
  --name SNAPSHOT_NAME \
  [--yes]
```

### `tx-list-snapshots`

List all named snapshots.

```bash
python3 scripts/code2database_builder.py tx-list-snapshots \
  --graph code2db-out/
```

### `tx-replay-wal`

Replay WAL entries (crash recovery). Use if a previous transaction was interrupted.

```bash
python3 scripts/code2database_builder.py tx-replay-wal \
  --graph code2db-out/ \
  [--dry-run]
```

## Graph Editing (require user confirmation)

### `update-node`  [write]

LLM-driven incremental node attribute supplement. Stores supplements as `{key}_supplemented` fields — does NOT overwrite original scan data.

```bash
python3 scripts/code2database_builder.py update-node \
  --graph code2db-out/ \
  --function function_name \
  --field semantic_desc \
  --value "Acquires mutex X before reading shared state Y" \
  --confidence INFERRED \
  --source "LLM read from body_text + lock-coverage" \
  [--yes]
```

### `update-edge`  [write]

LLM-driven incremental edge attribute supplement.

```bash
python3 scripts/code2database_builder.py update-edge \
  --graph code2db-out/ \
  --edge EDGE_ID \
  --field call_condition \
  --value "if (flag == 1)" \
  --confidence EXTRACTED \
  --source "AST" \
  [--yes]
```

### `patch-profile`  [write]

LLM-driven incremental auto-profile calibration. Non-destructive with backup.

```bash
python3 scripts/code2database_builder.py patch-profile \
  --graph code2db-out/ \
  --field callback_detection \
  --value '...' \
  [--yes]
```

### `classify-endpoints`  [write]

Apply LLM endpoint classification results to the graph.

```bash
python3 scripts/code2database_builder.py classify-endpoints \
  --graph code2db-out/ \
  --function function_name \
  --label out_end \
  --external-desc "HTTP API: POST /users" \
  [--yes]
```

### `auto-enhance`  [write]

LLM auto-semantic enhancement. EXTRACTED+evidence auto-writes; INFERRED require user confirmation; AMBIGUOUS rejected.

```bash
python3 scripts/code2database_builder.py auto-enhance \
  --graph code2db-out/ \
  [--scope function_name] \
  [--confidence-threshold EXTRACTED] \
  [--dry-run]  # preview without writing
  [--yes]  # apply INFERRED without prompting — NOT recommended
```

### `batch-confirm`  [write]

Batch-confirm pending INFERRED enhancements.

```bash
python3 scripts/code2database_builder.py batch-confirm \
  --graph code2db-out/ \
  [--scope function_name] \
  [--yes]
```

### `rollback`

Rollback applied enhancements by time or scope.

```bash
python3 scripts/code2database_builder.py rollback \
  --graph code2db-out/ \
  [--since TIMESTAMP] [--scope function_name] \
  [--yes]
```

### `fill-request`

List fields the LLM should fill (auto-fill request queue).

```bash
python3 scripts/code2database_builder.py fill-request \
  --graph code2db-out/ \
  [--scope function_name]
```

### `add-semantic-edges`  [write]

Add semantic edges (e.g., alloc→free pairing, callback registration) to the graph.

```bash
python3 scripts/code2database_builder.py add-semantic-edges \
  --graph code2db-out/ \
  --kind alloc_free \
  --src function_a --dst function_b \
  [--yes]
```

### `semantic-status`

Check semantic extraction status (how many nodes have semantic_desc, how many are pending).

```bash
python3 scripts/code2database_builder.py semantic-status \
  --graph code2db-out/
```

### `audit-log`

View audit log of past writes to the graph.

```bash
python3 scripts/code2database_builder.py audit-log \
  --graph code2db-out/ \
  [--since TIMESTAMP] [--scope function_name] \
  [--json]
```

## Daemon

### `daemon-start`

Start the background daemon (foreground; blocks). Monitors source files via inotify (or polling fallback) and auto-syncs changes through transactions.

```bash
python3 scripts/code2database_builder.py daemon-start \
  --graph code2db-out/ \
  --source /path/to/project \
  [--polling-interval SEC]
```

### `daemon-stop`

Stop a running daemon.

```bash
python3 scripts/code2database_builder.py daemon-stop \
  --graph code2db-out/
```

### `daemon-status`

Get daemon status: pid, last_sync, pending events, stale nodes, circuit breaker state.

```bash
python3 scripts/code2database_builder.py daemon-status \
  --graph code2db-out/
```

### `daemon-force-refresh`

Force re-scan of a specific file (bypasses change detection).

```bash
python3 scripts/code2database_builder.py daemon-force-refresh \
  --graph code2db-out/ \
  --path src/foo.c
```

### `daemon-pause`

Pause the daemon (e.g., before manual updates).

```bash
python3 scripts/code2database_builder.py daemon-pause \
  --graph code2db-out/ \
  --reason "manual update"
```

### `daemon-resume`

Resume the daemon after pause.

```bash
python3 scripts/code2database_builder.py daemon-resume \
  --graph code2db-out/
```

### `daemon-wait-sync`

Block until the current sync completes. **Call before important queries** to ensure the graph is up-to-date.

```bash
python3 scripts/code2database_builder.py daemon-wait-sync \
  --graph code2db-out/ \
  --timeout 30
```

### `daemon-logs`

Show the daemon log file. Use `--follow` for streaming.

```bash
python3 scripts/code2database_builder.py daemon-logs \
  --graph code2db-out/ \
  [--follow] [--lines N]
```

### `daemon-reload`

Reload daemon config (re-reads profile).

```bash
python3 scripts/code2database_builder.py daemon-reload \
  --graph code2db-out/
```

### `daemon-list-projects`

List all projects with daemon state files.

```bash
python3 scripts/code2database_builder.py daemon-list-projects
```

## Keep Graph Up to Date

### `watch`

File watcher for auto-rebuild on changes (in-process, no daemon).

```bash
python3 scripts/code2database_builder.py watch \
  --graph code2db-out/ \
  --source /path/to/project
```

### `sync`

Team merge: prefer local, supplement remote.

```bash
python3 scripts/code2database_builder.py sync \
  --graph code2db-out/ \
  --remote /path/to/remote/graph
```

### `merge`

Merge graphs from multiple sources.

```bash
python3 scripts/code2database_builder.py merge \
  --graph code2db-out/ \
  --inputs /path/a /path/b /path/c
```

### `light-scan`

Lightweight scan of changed files only (faster than full scan).

```bash
python3 scripts/code2database_builder.py light-scan \
  --source /path/to/project \
  --output code2db-out/.code2database_extraction.json \
  [--changed-since TIMESTAMP]
```

### `patch-from-diff`  [write]

Patch the graph from a diff file. Wrapped in a transaction by default; use `--no-transaction` to bypass.

```bash
python3 scripts/code2database_builder.py patch-from-diff \
  --graph code2db-out/ \
  --diff /path/to/changes.diff \
  [--no-transaction] \
  [--yes]
```

### `patch-from-git`  [write]

Auto-patch the graph from `git diff`. Wrapped in a transaction by default.

```bash
python3 scripts/code2database_builder.py patch-from-git \
  --graph code2db-out/ \
  [--source /path/to/repo] \
  [--commit HASH] \
  [--no-transaction] \
  [--yes]
```

### `install-hook`

Install a git post-commit hook for auto-update on commit.

```bash
python3 scripts/code2database_builder.py install-hook \
  --graph code2db-out/ \
  --source /path/to/repo
```

### `export-changes`

Export a change graph (diff between two graph states).

```bash
python3 scripts/code2database_builder.py export-changes \
  --graph code2db-out/ \
  --from-version v1 --to-version v2 \
  --output /path/to/changes.json
```

### `merge-changes`

Merge a change graph into the current graph. **Requires user confirmation.**

```bash
python3 scripts/code2database_builder.py merge-changes \
  --graph code2db-out/ \
  --changes /path/to/changes.json \
  [--yes]
```

## Profile & Doc-Code

### `profile-health`

Compute a 0-100 health score across 7 categories (callback coverage, vtable coverage, domain rules, lock patterns, FFI bindings, daemon config, doc-code alignment).

```bash
python3 scripts/code2database_builder.py profile-health \
  --graph code2db-out/ \
  [--json]
```

### `profile-evolve`  [write]

Detect new callback patterns and other profile-worthy structures. `--apply` applies EXTRACTED-confidence suggestions; INFERRED require user confirmation.

```bash
python3 scripts/code2database_builder.py profile-evolve \
  --graph code2db-out/ \
  [--apply] \
  [--yes]  # apply INFERRED without prompting — NOT recommended
  [--json]
```

### `profile-bind-version`

Bind the profile to the current git/svn HEAD commit (records the commit hash so profile drift can be detected later).

```bash
python3 scripts/code2database_builder.py profile-bind-version \
  --graph code2db-out/ \
  [--source /path/to/repo]
```

### `doc-code-check`

Check doc-code alignment; detect return-value / param / signature / stale-doc mismatches.

```bash
python3 scripts/code2database_builder.py doc-code-check \
  --graph code2db-out/ \
  [--scope function_name] \
  [--json]
```

### `doc-mark-stale`  [write]

Mark a node's doc as stale (non-destructive but visible — `describe-node` will warn users).

```bash
python3 scripts/code2database_builder.py doc-mark-stale \
  --graph code2db-out/ \
  --function function_name \
  --reason "signature changed in commit abc123" \
  [--yes]
```

### `doc-alignment-report`

Generate a full Markdown doc-code alignment report.

```bash
python3 scripts/code2database_builder.py doc-alignment-report \
  --graph code2db-out/ \
  --output /path/to/report.md
```

### `doc-signature-diff`

Detect signature changes between two graph versions.

```bash
python3 scripts/code2database_builder.py doc-signature-diff \
  --graph code2db-out/ \
  --from-version v1 --to-version v2 \
  [--json]
```

## Graph Versioning

### `graph-record-version`

Record a named graph version (snapshot for later diffing).

```bash
python3 scripts/code2database_builder.py graph-record-version \
  --graph code2db-out/ \
  --name v1.2.3 \
  [--notes "Release 1.2.3"]
```

### `graph-history`

Show the version history of the graph.

```bash
python3 scripts/code2database_builder.py graph-history \
  --graph code2db-out/
```

### `graph-diff`

Diff two graph versions.

```bash
python3 scripts/code2database_builder.py graph-diff \
  --graph code2db-out/ \
  --from-version v1 --to-version v2 \
  [--json]
```

## Memory Management

### `save-memory`  [write]

Save a Q&A to persistent memory. **Requires user confirmation.**

```bash
python3 scripts/code2database_builder.py save-memory \
  --graph code2db-out/ \
  --question "..." --answer "..." \
  --tags tag1,tag2 \
  [--yes]
```

### `search-memory`

Search persistent memory by query or tags.

```bash
python3 scripts/code2database_builder.py search-memory \
  --graph code2db-out/ \
  --query "..." \
  [--tags tag1,tag2] \
  [--limit N]
```

### `manage-memory`  [write]

Advanced memory CRUD and decay. Actions: `add`, `correct`, `reshape`, `promote`, `refine`, `decay`. **Requires user confirmation.**

```bash
python3 scripts/code2database_builder.py manage-memory \
  --graph code2db-out/ \
  --action add \
  --id MEM_ID \
  --content "..." \
  [--yes]
```

### `memory-health`

Check memory system health (count, decay status, coverage).

```bash
python3 scripts/code2database_builder.py memory-health \
  --graph code2db-out/
```

### `validate-memory`

Validate memory entries for accuracy and freshness.

```bash
python3 scripts/code2database_builder.py validate-memory \
  --graph code2db-out/ \
  [--id MEM_ID]
```

## Exports / Plugins / Benchmark

### `export-html`

Export the graph to an interactive HTML visualization.

```bash
python3 scripts/code2database_builder.py export-html \
  --graph code2db-out/ \
  --output /path/to/visualization.html \
  [--scope function_name]
```

### `export-obsidian`

Export the graph to an Obsidian vault (markdown files with cross-links).

```bash
python3 scripts/code2database_builder.py export-obsidian \
  --graph code2db-out/ \
  --output /path/to/vault/
```

### `web-ui`

Start the interactive Web UI server (default port 8765).

```bash
python3 scripts/code2database_builder.py web-ui \
  --graph code2db-out/ \
  --port 8765 \
  [--browser]
```

### `plugins`

List available plugins.

```bash
python3 scripts/code2database_builder.py plugins
```

### `validate-plugin`

Validate a plugin file.

```bash
python3 scripts/code2database_builder.py validate-plugin \
  --plugin /path/to/plugin.json
```

### `bug-benchmark`

Run the GraphInvestigator vs GrepInvestigator benchmark (recall / precision / tool-calls / tokens / time).

```bash
python3 scripts/code2database_builder.py bug-benchmark \
  --graph code2db-out/ \
  --scenario tests/fixtures/bug_benchmark/ \
  [--json]
```

## Embeddings (experimental)

### `embeddings-build`

Build semantic embeddings for the graph (experimental).

```bash
python3 scripts/code2database_builder.py embeddings-build \
  --graph code2db-out/ \
  [--model all-MiniLM-L6-v2]
```

### `embeddings-search`

Semantic search over the graph using embeddings.

```bash
python3 scripts/code2database_builder.py embeddings-search \
  --graph code2db-out/ \
  --query "find functions that acquire a mutex" \
  [--limit N]
```

## MCP Server

### `serve`

Start the MCP server (stdio transport, 50 tools: 31 `code2database_*` + 19 `cgdb_*`).

```bash
python3 scripts/code2database_builder.py serve \
  --graph code2db-out/
```

All 50 tools are accessible regardless of sub-skill activation. MCP is separate from skill layer activation. See `references/analysis_commands.md` for the 19 `cgdb_*` tools; see parent `references/usage_reference.md` for the 31 `code2database_*` tools.

## On-demand / Low-weight Commands

### `domain`

View domain structure (also available in parent skill).

```bash
python3 scripts/code2database_builder.py domain \
  --graph code2db-out/ \
  [--json]
```

### `extract-invariants-llm`

LLM-driven invariant extraction (also in analysis sub-skill).

### `intent-query` / `think-chain`

LLM-driven reasoning helpers (also in analysis sub-skill).

### `unbalanced-alloc-free`

Find unbalanced alloc/free pairs (also in analysis sub-skill).

### `explain-label` / `why-ambiguous`

Explain labeling / ambiguity decisions (also in analysis sub-skill).

## See Also

- `SKILL_ops.md` — Tier-1 commands + routing table (always loaded when sub-skill is active)
- `RUNTIME_CONFIG.md` — runtime tuning parameters
- `PROFILE_MANUAL.md` — profile authoring
- `~/.claude/skills/Code2Database-analysis/references/analysis_commands.md` — analysis commands
- Parent `~/.claude/skills/Code2Database/references/usage_reference.md` — Tier-1 command syntax
