---
name: Code2Database-ops
description: "Operations sub-skill for Code2Database. Activated when the user asks about safe graph editing (transactions, snapshots, WAL replay), keeping the graph up to date (daemon control, file watching, git hook install, patch-from-diff, patch-from-git, sync, merge), profile health and evolution, doc-code alignment and stale-doc marking, graph versioning, persistent memory management, exports (HTML, Obsidian, Web UI), plugins, embeddings (experimental), or the BUG benchmark. Provides hierarchical routing: 14 Tier-1 high-weight commands shown in Quick Reference, a routing table mapping question types to medium-weight command groups, and an on-demand section listing low-weight experimental commands by name only. Database write constraint: LLM MUST get user confirmation before any DB-modifying command (update-node, update-edge, patch-profile, apply-semantics, apply-invariants, auto-enhance, profile-evolve --apply, doc-mark-stale, ffi-types, merge-changes, tx-commit). Use when /Code2Database detects an ops question and explicitly hands off, or when the user types /Code2Database-ops. Not for: querying the graph (use parent /Code2Database); not for deep semantic analysis (use /Code2Database-analysis)."
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

**Commands requiring user confirmation** (default: y/N prompt):

- `update-node` — LLM-driven incremental node attribute supplement
- `update-edge` — LLM-driven incremental edge attribute supplement
- `patch-profile` — LLM-driven incremental auto-profile calibration
- `apply-semantics` — Apply LLM-filled semantic descriptions to graph
- `classify-endpoints` — Apply LLM endpoint classification results
- `manage-memory --action add/correct/reshape/promote/refine` — Write persistent memory
- `save-memory` — Save Q&A memory
- `apply-invariants` — Apply extracted invariants; **AMBIGUOUS never applied**; INFERRED require confirmation; EXTRACTED auto-applied
- `auto-enhance` — LLM auto-semantic enhancement; EXTRACTED+evidence auto-writes; **INFERRED require confirmation**; AMBIGUOUS rejected
- `batch-confirm` — Batch-confirm pending INFERRED enhancements
- `profile-evolve --apply` — Apply EXTRACTED-confidence profile suggestions; **INFERRED require confirmation**
- `doc-mark-stale` — Mark a node's doc as stale (non-destructive but visible)
- `ffi-types` — Update type marshalling table for an FFI edge
- `merge-changes` — Merge change-graph JSON into the existing graph (writes nodes/edges)
- `tx-commit` — For write transactions; commits snapshot + WAL entries to the live DB

**LLM behavior rules**:

1. Before executing the above commands, **MUST** report to the user in conversation:
   - Which node/edge/profile field will be modified
   - What is the old value, what is the new value
   - Information source (LLM read source / user told / extracted from docs)
   - Confidence level (EXTRACTED / INFERRED / AMBIGUOUS)
2. Wait for explicit user consent ("yes" / "confirm" / "proceed") before calling the command
3. **NEVER** use `--yes` / `-y` flag to bypass confirmation prompt unless user explicitly authorizes in conversation
4. If user declines, do not retry the same write

**Non-destructive write guarantee**:

- `update-node` and `update-edge` store LLM supplements as `{key}_supplemented` fields, **NOT overwriting** original scan data
- Each supplement includes `_supplement_meta` (recording source / confidence / timestamp / original), auditable in `describe-node` output
- `apply-invariants`, `auto-enhance`, and `profile-evolve` follow the same supplement pattern; `rollback` reverts by time or scope
- This guarantees "database accuracy": original scan facts are always preserved, LLM incremental data is traceable and rollback-able

## Tier 1 — High-weight Commands (Quick Reference)

| Command | Purpose |
|---------|---------|
| `tx-begin` | Begin a transaction (snapshot + WAL) |
| `tx-commit` | Commit current transaction (**requires user confirmation** for writes) |
| `tx-rollback` | Rollback current transaction (restores snapshot) |
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
| `serve` | MCP server mode (stdio, 49 tools: 30 code2database_* + 19 cgdb_*) |

## Routing Table — Medium-weight Commands by Question Type

| Question Type | Command Sequence |
|---------------|------------------|
| **Safe graph editing** | `tx-begin` → `tx-status` → `update-node` / `update-edge` / `patch-profile` / `classify-endpoints` / `auto-enhance` / `batch-confirm` / `rollback` / `fill-request` / `add-semantic-edges` / `semantic-status` / `audit-log` → `tx-commit` (with confirmation) → fallback `tx-restore` / `tx-list-snapshots` / `tx-replay-wal` if needed |
| **Keep graph up to date** | `daemon-start` → `daemon-status` → `daemon-pause` / `daemon-resume` / `daemon-force-refresh` / `daemon-wait-sync` / `daemon-logs` / `daemon-reload` / `daemon-list-projects` → `daemon-stop` ; or `watch` / `sync` / `merge` / `light-scan` / `patch-from-diff` / `patch-from-git` / `install-hook` / `export-changes` / `merge-changes` |
| **Profile and doc-code** | `profile-health` → `profile-evolve` → `profile-bind-version` ; `doc-code-check` → `doc-alignment-report` → `doc-signature-diff` → `doc-mark-stale` |
| **Graph versioning** | `graph-record-version` → `graph-history` → `graph-diff` |
| **Memory management** | `save-memory` → `search-memory` → `manage-memory` → `memory-health` → `validate-memory` |
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
- **Memory management**: `manage-memory --action add/correct/reshape/promote/refine` requires user confirmation; `save-memory` requires user confirmation
- **MCP server**: `serve` exposes 49 tools (30 `code2database_*` + 19 `cgdb_*`); all accessible regardless of sub-skill activation
- **Do not pre-load** `references/ops_commands.md` — read on demand only when you need detailed syntax for a specific command
- **Daemon logs** at `~/.code2database/daemon-<project>.log`; daemon state at `<graph_dir>/.daemon_status.json`

## Reference Index

| Document | Content |
|----------|---------|
| `references/ops_commands.md` | Full syntax for all ops commands (transactions, daemon, profile, doc-code, exports, plugins, memory, embeddings) |
| `RUNTIME_CONFIG.md` | Runtime tuning (invariants, auto_enhance, transactions, ffi, web_ui, benchmark, profile_health, doc_code, daemon sections) |
| `PROFILE_MANUAL.md` | Profile authoring (skip_names, callback_detection, struct_op_types, registration_macros, domain_rules, threading_models) |
| `references/memory_knowledge.md` | Memory and knowledge management details |

**Inherited from parent** (`/Code2Database`): `references/usage_reference.md`, `references/label_rules.md`, `references/data_model.md`, `references/json_schema.md`, `references/usage_examples.md`.

**Internal files** (do NOT load into agent context): `OVERVIEW.md`, `scripts/`, `config/profiles/`. These are implementation details for tool developers, not needed for usage.
