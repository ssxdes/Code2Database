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

Get daemon status: pid, last_sync, pending events, stale nodes, circuit breaker state, and startup-grace state (`sync.startup_grace_active`, `sync.startup_grace_remaining_sec` — events seen during the grace window are held, not synced).

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

Start the MCP server (stdio transport, 81 tools: 34 `code2database_*` + 19 `cgdb_*` + 28 design-report).

```bash
python3 scripts/code2database_builder.py serve \
  --graph code2db-out/
```

All 81 tools are accessible regardless of sub-skill activation. MCP is separate from skill layer activation. See `references/analysis_commands.md` for the 19 `cgdb_*` tools; see parent `references/usage_reference.md` for the 34 `code2database_*` tools.

The new `code2database_kb_query` tool (Phase 3) is the unified FTS5+BM25
query surface across memory + knowledge stores:

```json
{"tool": "code2database_kb_query",
 "arguments": {"query": "bdev register io_device", "top": 10,
               "kinds": "memory_qa,knowledge_principle"}}
```

## Knowledge Base (kb-* commands — Phase 1-11)

### `kb-rebuild-index`

Rebuild the unified FTS5 index from `memory/*.json` + `knowledge/*.md`.
Run after each `build` / `update` or after manual memory/knowledge edits.

```bash
python3 scripts/code2database_builder.py kb-rebuild-index \
  --graph code2db-out/
```

### `kb-query`

Unified FTS5+BM25 query across memory + knowledge. Returns ranked hits
with `source_kind` (memory / knowledge), `score`, `body`, `see_also`.

```bash
python3 scripts/code2database_builder.py kb-query \
  --graph code2db-out/ \
  --query "how does bdev register io_device" \
  [--top 10] [--kinds memory_qa,knowledge_principle] \
  [--min-weight 0.0] [--max-tokens 4000] \
  [--semantic] [--global]
```

`--semantic`: enable embedding-based semantic search (requires
sentence-transformers; auto-degrades to FTS5 if unavailable).
`--global`: fall back to `~/.code2database_global_kb/global.db` when the
project KB has no matches.

### `kb-cluster`

Cluster similar kb items via union-find on FTS5 BM25 > threshold. Picks
canonical (highest weight × confidence) per cluster and links
`memory_qa` → `knowledge_principle` via `principle_ref`.

```bash
python3 scripts/code2database_builder.py kb-cluster \
  --graph code2db-out/ \
  [--threshold 0.5]
```

### `kb-migrate`

Migrate `kb_paragraphs` rows → `kb_items` (fact-level with `versions_json`,
`decay_class`, `provenance_commit`). Both tables coexist; kb_items is the
long-term successor.

```bash
python3 scripts/code2database_builder.py kb-migrate \
  --graph code2db-out/
```

### `kb-known-unknowns`

List queries that returned no matches (aggregated from `kb_query_log`).
Helps identify knowledge gaps the user should fill.

```bash
python3 scripts/code2database_builder.py kb-known-unknowns \
  --graph code2db-out/ \
  [--top 20] [--min-occurrences 2]
```

### `kb-audit`

Audit the project KB: counts by kind, stale items (>90d untouched),
low-confidence (<0.5), high-citation (top access_count), most-linked
principles. Optional `--topic` for "what do we know about X".

```bash
python3 scripts/code2database_builder.py kb-audit \
  --graph code2db-out/ \
  [--topic "bdev registration"]
```

### `kb-conflict`

Detect contradictory items within the same cluster (pairwise check for
14 word pairs: yes/no, must/must not, always/never, safe/unsafe, ...).

```bash
python3 scripts/code2database_builder.py kb-conflict \
  --graph code2db-out/
```

### `kb-rollback`

Restore a `kb_item` to a prior version (saves current state as a new
version entry first, so rollback is itself reversible).

```bash
python3 scripts/code2database_builder.py kb-rollback \
  --graph code2db-out/ \
  --id 42 [--to-version 3]
```

### `kb-forget`  [write]

Immediately delete a `kb_paragraph` row (no decay wait). Writes an
audit_log entry (operator, timestamp, reason) for traceability.

```bash
python3 scripts/code2database_builder.py kb-forget \
  --graph code2db-out/ \
  --id 42 \
  --reason "incorrect: bdev_register doesn't call io_device_register"
```

### `kb-global-add` / `kb-global-search` / `kb-global-share` / `kb-global-import`

Cross-project global KB at `~/.code2database_global_kb/global.db`. Stores
project-agnostic knowledge (debugging methodology, protocol standards,
tool usage) reusable across all projects.

```bash
# Add
python3 scripts/code2database_builder.py kb-global-add \
  --title "Linux kernel threading model" \
  --body "Per-thread event loops; no locks needed within thread..." \
  --tags "kernel,threading" --kind principle

# Search
python3 scripts/code2database_builder.py kb-global-search \
  --query "thread safety" [--top 10]

# Share (export to JSON for teammate)
python3 scripts/code2database_builder.py kb-global-share \
  --output ~/global_kb_share.json

# Import (teammate's JSON)
python3 scripts/code2database_builder.py kb-global-import \
  --input ~/global_kb_share.json
```

## Cross-C2D Management (Phase 1 F5/F7/F8)

### `c2d-resolve-foreign`

Force re-resolve stale/deleted foreign_refs by function name, regardless of
whether the foreign C2D's mtime changed. Useful when A renamed a function and
B's refs are stale — `c2d-sync-foreign` only re-resolves on mtime change.

```bash
python3 scripts/code2database_builder.py c2d-resolve-foreign \
  --graph code2db-out/ \
  [--foreign-c2d /path/to/A/c2db-out/]
```

### `c2d-prune-foreign`

Remove old foreign_refs with specified statuses (default: `deleted,orphaned`)
older than `--max-age-days` (default 30). Keeps resolved/stale/unresolved
refs (still active). Prevents foreign_refs table from growing unboundedly.

```bash
python3 scripts/code2database_builder.py c2d-prune-foreign \
  --graph code2db-out/ \
  [--max-age-days 30] \
  [--prune-statuses deleted,orphaned]
```

### `c2d-pin-foreign`  [write]

Pin a resolved foreign_ref so it won't auto-update when A changes. Pinned
refs keep their current `foreign_node_id` even if A renames or deletes the
function. Useful for stable API contracts.

```bash
python3 scripts/code2database_builder.py c2d-pin-foreign \
  --graph code2db-out/ \
  --ref-id 42
```

### `c2d-unpin-foreign`

Restore auto-update behavior for a pinned foreign_ref.

```bash
python3 scripts/code2database_builder.py c2d-unpin-foreign \
  --graph code2db-out/ \
  --ref-id 42
```

## Multi-Project Commands

### `build-multi`

Build a unified C2D from multiple interdependent projects (A→B→C) via a
manifest JSON. Forces project-name domain prefix to prevent collisions.

```bash
python3 scripts/code2database_builder.py build-multi \
  --manifest projects.json --outdir joint_c2db-out/ \
  [-j 8] [--max-workers 48] [--force-rescan A,B] [--no-clang]
```

### `c2d-add-foreign`

Register a foreign C2D (project A) and resolve B's unresolved calls against it.

```bash
python3 scripts/code2database_builder.py c2d-add-foreign \
  --graph B/c2db-out/ --foreign-c2d A/c2db-out/ --project-name A
```

### `c2d-sync-foreign`

Detect changes in foreign C2Ds and re-resolve foreign_refs.

```bash
python3 scripts/code2database_builder.py c2d-sync-foreign \
  --graph B/c2db-out/ [--foreign-c2d A/c2db-out/]
```

### `c2d-list-foreign`

List watched foreign C2Ds with sync status and ref counts.

```bash
python3 scripts/code2database_builder.py c2d-list-foreign --graph B/c2db-out/
```

### `c2d-remove-foreign`

Unregister a foreign C2D. Foreign_refs are marked 'orphaned'.

```bash
python3 scripts/code2database_builder.py c2d-remove-foreign \
  --graph B/c2db-out/ --foreign-c2d A/c2db-out/
```

### `composite-query`

Cross-C2D query via SQLite ATTACH. Supports CALLERS_OF / CALLEES_OF.

```bash
python3 scripts/code2database_builder.py composite-query \
  --graph B/c2db-out/ --query "CALLERS_OF init" \
  --foreign-c2d C/c2db-out/ --top 50
```

### `c2d-check-compat`

Check if B's foreign_refs are still valid against a new version of A.

```bash
python3 scripts/code2database_builder.py c2d-check-compat \
  --graph B/c2db-out/ --against-c2d A_v2/c2db-out/
```

### `coverage-cross-c2d`

Compute which functions in target_c2d are called by test_c2d.

```bash
python3 scripts/code2database_builder.py coverage-cross-c2d \
  --test-c2d test_A/c2db-out/ --target-c2d A/c2db-out/
```

### `c2d-add-foreign-stub`

Register a vendor SDK stub C2D (signatures only, API stable).

```bash
python3 scripts/code2database_builder.py c2d-add-foreign-stub \
  --graph B/c2db-out/ --stub-c2d glibc_stub/ --project-name glibc
```

### `ffi-auto-link`

Auto-link FFI bindings (ctypes/cgo/extern C) to watched foreign C2Ds.

```bash
python3 scripts/code2database_builder.py ffi-auto-link --graph B/c2db-out/
```

### `scan-rpc`

Scan source for HTTP/gRPC calls, create rpc_endpoint stub nodes + edges.

```bash
python3 scripts/code2database_builder.py scan-rpc --graph B/c2db-out/
```

### `import-foreign-knowledge`

Copy foreign C2D's knowledge/*.md into local with project prefix.

```bash
python3 scripts/code2database_builder.py import-foreign-knowledge \
  --graph B/c2db-out/ --foreign-c2d A/c2db-out/ --project-name A
```

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
