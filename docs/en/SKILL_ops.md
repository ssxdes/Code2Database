---
name: Code2Database-ops
description: "Operations sub-skill for Code2Database. Activated when the user asks about safe graph editing (transactions, snapshots, WAL replay), keeping the graph up to date (daemon, git hooks, patch-from-diff/git), profile health/evolution, doc-code alignment, graph versioning, persistent memory, exports, plugins, embeddings, or the BUG benchmark. DB-modifying commands require user confirmation. Use when /Code2Database hands off an ops question, or when the user types /Code2Database-ops. Not for: graph queries (parent); deep analysis (/Code2Database-analysis)."
trigger: /Code2Database-ops
parent_skill: Code2Database
---

# /Code2Database-ops

**Operations layer for Code2Database.** Activated when the user wants to safely edit the graph, keep it up to date, manage profile/doc-code, run exports, control the daemon, manage transactions and snapshots, or work with persistent memory.

This sub-skill assumes `code2db-out/` already exists (built by the parent `/Code2Database` skill). It does **not** handle querying the graph for insights — that's the parent skill's job. It does **not** handle deep semantic analysis (concurrency, data flow, invariants) — that's `/Code2Database-analysis`.

## When to Activate

- User explicitly types `/Code2Database-ops`
- The parent `/Code2Database` skill detects an ops question and hands off with the phrase *"activate Code2Database-ops sub-skill"*
- User asks any of:
  - "How do I safely edit this node/edge?" / "Update this function's semantics"
  - "Start/stop the daemon" / "Is the daemon up to date?"
  - "Take a snapshot before I change this" / "Roll back this transaction"
  - "Check profile health" / "Evolve my profile" / "Bind profile to HEAD"
  - "Doc says X, code does Y — mark the doc stale"
  - "Export the graph to HTML / Obsidian / Web UI"
  - "Install a git hook for auto-update"
  - "Apply a diff/git diff as a graph patch"
  - "Save this Q&A to memory" / "Search memory"
  - "Run the BUG benchmark"
  - "Set up the MCP server"

## Database Write Constraint (Important)

LLM MUST get user confirmation before any DB-modifying command. This is the core principle: **content may be missing but must be accurate.**

**Commands requiring user confirmation** (default: y/N prompt) — grouped by family; per-command detail in `references/ops_commands.md`:

- Graph edits: `update-node`, `update-edge`, `patch-profile`, `classify-endpoints`, `apply-semantics`, `merge-changes`
- Memory / knowledge writes: `save-memory`, `manage-memory --action add/correct/reshape/promote/refine/split/merge/move`, `kb-rebuild-index`, `kb-cluster`, `kb-migrate`, `kb-forget`, `kb-rollback`
- Enhancement / invariants / profile: `apply-invariants` (**AMBIGUOUS never applied**; INFERRED require confirmation; EXTRACTED auto-applied), `auto-enhance` (EXTRACTED+evidence auto-writes; **INFERRED require confirmation**), `batch-confirm`, `profile-evolve --apply` (**INFERRED require confirmation**), `doc-mark-stale`, `ffi-types`
- Transactions: `tx-commit` (write transactions: commits snapshot + WAL entries to the live DB)

**LLM behavior rules**:

1. Before executing, report in conversation: which node/edge/profile field changes (old value → new value), the information source (LLM read / user told / extracted from docs), and confidence (EXTRACTED / INFERRED / AMBIGUOUS)
2. Wait for explicit user consent ("yes" / "confirm" / "proceed") before calling the command
3. **NEVER** use `--yes` / `-y` to bypass the confirmation prompt unless the user explicitly authorizes it in conversation
4. If user declines, do not retry the same write

**Non-destructive write guarantee**: `update-node` / `update-edge` / `apply-invariants` / `auto-enhance` / `profile-evolve` store LLM supplements as `{key}_supplemented` fields — original scan data is never overwritten. Each supplement carries `_supplement_meta` (source / confidence / timestamp / original), visible in `describe-node` output; `rollback` reverts by time or scope. Original scan facts are always preserved; LLM incremental data is traceable and rollback-able.

## Tier 1 — High-weight Commands (Quick Reference)

| Command | Purpose |
|---------|---------|
| `tx-begin` | Begin a transaction (snapshot + WAL) |
| `tx-commit` | Commit current transaction (**requires user confirmation** for writes) |
| `tx-rollback` | Roll back current transaction (restores snapshot) |
| `tx-status` | Show transaction status |
| `daemon-start` | Start background daemon (foreground; blocks) — inotify + transactional sync |
| `daemon-stop` | Stop a running daemon |
| `daemon-wait-sync` | Block until current sync completes (**call before important queries**) |
| `profile-health` | Compute 0-100 health score across 7 categories |
| `profile-evolve` | Detect new callback patterns; `--apply` applies EXTRACTED suggestions (**requires user confirmation** for INFERRED) |
| `profile-bind-version` | Bind profile to current git/svn HEAD commit |
| `doc-code-check` | Check doc-code alignment; detect return-value/param/signature mismatches |
| `doc-mark-stale` | Mark a node's doc as stale (**requires user confirmation**) |
| `update-node` | LLM-driven incremental node attribute supplement (**requires user confirmation**, non-destructive) |
| `update-edge` | LLM-driven incremental edge attribute supplement (**requires user confirmation**, non-destructive) |
| `serve` | MCP server mode (stdio or HTTP, 83 tools: 36 code2database_* + 19 cgdb_* + 28 design-report). HTTP: `--transport http --host 0.0.0.0 --port 8765 --token SECRET --read-only` |
| `kb-rebuild-index` | Rebuild unified FTS5 index from memory.db + brief.json (run after build/update) |
| `kb-cluster` | Cluster similar kb items + link principle refs |
| `kb-audit` | KB audit: counts by kind / stale / low-confidence / citations |
| `kb-known-unknowns` | List unmatched queries (feedback loop) |
| `kb-forget` | Immediately delete a kb item (no decay; **requires user confirmation**; writes audit_log) |
| `kb-rollback` | Roll a kb_item back to a prior version (saves current as version history) |
| `kb-conflict` | Detect contradictory items in the same cluster (yes/no, must/must not, ...) |
| `kb-global-add` / `kb-global-search` / `kb-global-share` / `kb-global-import` | Cross-project global KB (~/.code2database_global_kb/) |
| `kb-global-share-memory` / `kb-global-search-memory` / `kb-global-import-memory` | Cross-project global memory Q&A: share high-weight memories to the global KB, search for similar Q&A across projects, import matches into the current project's memory.db (with merge) |

## Routing Table — Medium-weight Commands by Question Type

> **Executable shortcut**: several routing families here (quality checks, doc-alignment) are also runnable in one call from the parent skill — `c2d ask --recipe quality` / `c2d ask --question "..."` (see `c2d recipes`).

| Question Type | Command Sequence |
|---------------|------------------|
| **Safe graph editing** | `tx-begin` → `tx-status` → `update-node` / `update-edge` / `patch-profile` / `classify-endpoints` / `auto-enhance` / `heuristic-enhance` / `batch-confirm` / `rollback` / `fill-request` / `add-semantic-edges` / `semantic-status` / `audit-log` → `tx-commit` (with confirmation) → fallback `tx-restore` / `tx-list-snapshots` / `tx-replay-wal` if needed |
| **Keep graph up to date** | `daemon-start` → `daemon-status` → `daemon-pause` / `daemon-resume` / `daemon-force-refresh` / `daemon-wait-sync` / `daemon-logs` / `daemon-reload` / `daemon-list-projects` → `daemon-stop` ; or `watch` / `sync` / `merge` / `light-scan` / `patch-from-diff` / `patch-from-git` / `install-hook` / `export-changes` / `merge-changes` ; precise per-file update: `build-update --source SRC --graph DIR` or `quick-update --source SRC --graph DIR` |
| **Profile and doc-code** | `profile-health` → `profile-evolve` → `profile-bind-version` ; `doc-code-check` → `doc-alignment-report` → `doc-signature-diff` → `doc-mark-stale` |
| **Graph versioning** | `graph-record-version` → `graph-history` → `graph-diff` |
| **Memory management** | `save-memory --category` → `search-memory` → `manage-memory --action split/merge/move/categories` → `memory-health` → `validate-memory` ; knowledge brief: `brief-extract` → `brief-validate` → `brief-suggest` → `brief-migrate-legacy` ; cross-project: `kb-global-share-memory` → `kb-global-search-memory` → `kb-global-import-memory` |
| **Export / plugin / benchmark** | `export-html` / `export-obsidian` / `web-ui` ; `plugins` / `validate-plugin` ; `bug-benchmark` |
| **Embeddings (experimental)** | `embeddings-build` → `embeddings-search` |

## On-demand Commands (low-weight, experimental / rare)

Listed by **name only**. Read `references/ops_commands.md` before invoking — only when the user explicitly asks for them.

- `embeddings-build`, `embeddings-search` — semantic embeddings (experimental)
- `extract-invariants-llm`, `intent-query`, `think-chain` — LLM-driven extras
- `domain` — view domain structure (also in parent skill)
- `graph-record-version` — record a named graph version
- `unbalanced-alloc-free` — find unbalanced alloc/free pairs
- `explain-label`, `why-ambiguous` — explain labeling / ambiguity decisions

## Activation Hand-off

When you detect a question about **concurrency, data flow, invariants, FFI, path feasibility, provenance, cgdb tables**, hand off to the analysis sub-skill:

> "This question is about deep semantic analysis. Activate `Code2Database-analysis` sub-skill."

When you detect a question about **simple browsing, scanning, building, or general invocation relationships**, hand off to the parent:

> "This question is about basic graph navigation. Activate `Code2Database` sub-skill."

## Constraints (inherited from parent)

- **Transactional writes**: wrap multi-step DB modifications in `tx-begin`/`tx-commit`. `patch-from-diff`/`patch-from-git` already do this by default; use `--no-transaction` to bypass. Use `tx-rollback` to abort; `tx-replay-wal` for crash recovery
- **Daemon freshness**: call `daemon-status` before important queries; if `syncing` or `pending_events > 0`, call `daemon-wait-sync` to block until sync completes. Circuit breaker triggers bulk rebuild above 1000 events/minute
- **Doc-code alignment**: `describe-node` (parent skill) surfaces `doc_code_mismatches` — if non-empty, `semantic_desc` may be unreliable; consult `body_text` and consider `doc-mark-stale` until docs are re-extracted
- **Profile evolution**: `profile-evolve --apply` only applies EXTRACTED-confidence suggestions; INFERRED **require user confirmation**. Run `profile-bind-version` after evolution to bind to git/svn HEAD
- **Memory management**: `manage-memory` write actions (add/correct/reshape/promote/refine/split/merge/move) and `save-memory` require user confirmation; `brief-update` edits the mandatory-load knowledge brief
- **MCP server**: `serve` exposes 83 tools (36 `code2database_*` + 19 `cgdb_*` + 28 design-report); all accessible regardless of sub-skill activation
- **Do not pre-load** `references/ops_commands.md` — read on demand only when you need detailed syntax for a specific command
- **Daemon logs** at `~/.code2database/daemon-<project>.log`; daemon state at `<graph_dir>/.daemon_status.json`

## Reference Index

| Document | Content |
|----------|---------|
| `references/ops_commands.md` | Full syntax for all ops commands (transactions, daemon, profile, doc-code, exports, plugins, memory, embeddings) |
| `RUNTIME_CONFIG.md` *(inherited — parent skill dir)* | Runtime tuning (invariants, auto_enhance, transactions, ffi, web_ui, benchmark, profile_health, doc_code, daemon sections) |
| `PROFILE_MANUAL.md` *(inherited — parent skill dir)* | Profile authoring (skip_names, callback_detection, struct_op_types, registration_macros, domain_rules, threading_models) |

**Inherited from parent** (`/Code2Database`): `references/usage_reference.md`, `references/label_rules.md`, `references/data_model.md`, `references/json_schema.md`, `references/usage_examples.md`, `references/memory_knowledge.md`, `RUNTIME_CONFIG.md`, `PROFILE_MANUAL.md`. These are available at the parent skill's directory.

**Internal files** (do NOT load into agent context): `OVERVIEW.md`, `scripts/`, `config/profiles/`. These are implementation details for tool developers, not needed for usage.
