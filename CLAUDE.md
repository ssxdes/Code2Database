# CLAUDE.md

This file provides guidance to Claude Code when working with Code2Database.

> **Boundary**: Skill instructions are in `SKILL.md`. Internal implementation details (`OVERVIEW.md`, `scripts/`) should NOT be loaded into context unless debugging the tool itself.

## Project Overview

Code2Database scans C/C++/Go/Python/Java/Rust/ASM codebases and generates directed invocation graphs with invocation ordering, conditions, conditional compilation paths, concurrency analysis, data race detection, and field-level access tracking. It follows a **micro → lite → local** query pattern for token-efficient LLM interaction.

Capabilities: dual clang + tree-sitter extraction backend (auto/clang/tree-sitter via `--extraction-backend`; libclang recommended but NOT required — tree-sitter-only mode is fully functional), cgdb (code graph database) layer with semantic tables (AST nodes, types, config predicates, CFG, data flow, alias, ops_bindings, sync primitives, happens-before, provenance, time-travel versions) exposed via 19 `cgdb_*` MCP tools, commit-based provenance with git hash verification, Cypher-subset queries, value flow / DATA_FLOW edges, lock-held region analysis, Z3 path feasibility, cross-function data dependencies, invariant extraction, LLM auto-semantic enhancement, transactional updates (WAL + snapshots), cross-language FFI tracing, interactive Web UI, BUG benchmark, profile health + auto-evolution, doc-code dual-source truth alignment, and a background daemon for real-time auto-sync.

Distributed as a Python-based skill with 3 sub-skills: `/Code2Database` (core, always loaded — 24 Tier-1 commands), `/Code2Database-analysis` (deep semantic analysis, on-demand), `/Code2Database-ops` (graph editing + ops, on-demand). 222 CLI commands total, 81 MCP tools (53 base + 28 design-report) (34 `code2database_*` + 19 `cgdb_*`). CLI entry points: `scripts/code2database_builder.py`, `scripts/code2database_scanner.py`. Daemon entry point: `daemon-start`.

## Architecture: Profile → Scan → Build

```
Source files → [Profile config] → [Scan] → extraction.json → [Build] → code graph output
                                                                     ↓
                                                              [Daemon] auto-refresh
                                                                     ↓
                                                            [Transactional Sync]
```

- **Profile**: Project-specific config (skip names, callback patterns, vtable types, domain rules, lock patterns, FFI bindings, daemon config)
- **Scan**: AST extraction (tree-sitter) — produces immutable facts
- **Build**: Inference + graph construction — vtable dispatch, callback bridging, domain classification, community detection, race detection, invariant extraction, FFI detection, doc-code alignment
- **Daemon** (optional): long-running process that monitors source files (inotify/polling) and auto-updates the graph in transactions

## Build, Install, Run

```bash
# Install dependencies
bash scripts/setup.sh
# Or manually:
pip install -r scripts/requirements.txt

# Optional: Z3 for path feasibility (sound results; heuristic fallback without it)
pip install z3-solver

# Generate a project profile (project-specific conventions)
# Built-in profiles exist for common projects (linux_kernel, dpdk, spdk) —
# skip this step and pass --profile config/profiles/<type>.json to scan instead.
python3 scripts/code2database_scanner.py auto-profile \
  --source /path/to/project \
  --outdir /path/to/project

# Scan a project
python3 scripts/code2database_scanner.py scan \
  --source /path/to/project \
  --profile /path/to/project/.code2database_profile.json \
  --output code2db-out/.code2database_extraction.json

# Build invocation graph
python3 scripts/code2database_builder.py build \
  --extraction code2db-out/.code2database_extraction.json \
  --outdir code2db-out/ --build-config auto

# Query the graph
python3 scripts/code2database_builder.py explore-flow \
  --graph code2db-out/ --query "initialization" --max-tokens 2000

# Optional: start background daemon for real-time auto-sync
python3 scripts/code2database_builder.py daemon-start \
  --graph code2db-out/ --source /path/to/project
```

## Skill Activation

The skill is split into **3 sub-skills** to keep LLM context lean. Each sub-skill has its own `SKILL.md` exposing only the commands relevant to its layer. The CLI (`scripts/code2database_builder.py`) is shared — all 222 commands are accessible regardless of which sub-skill is active.

| Sub-skill | Trigger | Purpose | Tier-1 commands |
|-----------|---------|---------|-----------------|
| `Code2Database` (core) | `/Code2Database` | Build + browse — always loaded | 24 high-weight commands (scan, build, explore-flow, describe-node, trace-chain, etc.) |
| `Code2Database-analysis` | `/Code2Database-analysis` | Deep semantic analysis (concurrency, data flow, invariants, FFI, provenance, path feasibility, cgdb tables) | 13 high-weight commands (concurrency-analyze, value-flow, path-feasible, find-invariants, ffi-trace, blame-node, etc.) + 19 `cgdb_*` MCP tools |
| `Code2Database-ops` | `/Code2Database-ops` | Graph editing, transactions, daemon, profile/doc-code, exports, plugins, memory, embeddings | 23 high-weight commands (tx-begin/commit/rollback, daemon-start/stop/wait-sync, profile-health, doc-code-check, etc.) |

The main skill instructions are in `docs/<lang>/SKILL.md` (lang = en or zh). When installed, the appropriate language version is placed as `SKILL.md` at each sub-skill root.

- **Trigger**: `/Code2Database` (core), `/Code2Database-analysis` (deep analysis), `/Code2Database-ops` (ops); or questions about invocation relationships, invocation chains, architecture, concurrency
- **Query priority**: context_pack_micro → context_pack_lite → explore-flow → describe-node
- **Query entry points**: `query` (Cypher), `find-invariants`, `ffi-trace`, `path-feasible`, `doc-code-check`, `daemon-status`
- **Sub-skill hand-off**: when the core skill detects a deep-analysis or ops question, it explicitly hands off with phrases like "activate `Code2Database-analysis` sub-skill" or "activate `Code2Database-ops` sub-skill".

## Extraction Backend

Code2Database supports a dual backend for C/C++ extraction:

- `auto` (default) — uses clang when libclang is installed, falls back to tree-sitter otherwise
- `clang` — force the clang backend (enables cgdb layer; requires `pip install libclang==17.0.6`)
- `tree-sitter` — force the tree-sitter backend (no libclang dependency)

```bash
# Force a specific backend at scan time
python3 scripts/code2database_scanner.py scan --source /path --extraction-backend clang
python3 scripts/code2database_scanner.py scan --source /path --extraction-backend tree-sitter
```

**libclang is recommended, NOT required.** Tree-sitter-only mode remains fully functional — every supported language can be scanned, built, and queried. The clang backend additionally populates the cgdb (code graph database) layer with typed semantic tables (CFG, data flow, ops bindings, sync primitives, config predicates) exposed via 19 `cgdb_*` MCP tools.

## Key Directories

| Directory | Purpose |
|-----------|---------|
| `scripts/code2database_scanner.py` | Scanner CLI entry point |
| `scripts/code2database_builder.py` | Builder CLI entry point (main command hub, 222 commands organized into 3 sub-skills: `/Code2Database` core, `/Code2Database-analysis`, `/Code2Database-ops`) |
| `scripts/_scanner/` | Language-specific AST scanners (C, Go, Python, Java, Rust) |
| `scripts/_builder/` | Graph building, query, export, memory, knowledge modules |
| `scripts/_builder/invariants.py` | Invariant extraction (preconditions/postconditions/loop_invariants/state_machine) |
| `scripts/_builder/auto_enhance.py` | LLM auto-semantic enhancement with confidence-threshold auto-write |
| `scripts/_builder/transactions.py` | Transactional updates — WAL + snapshots + fcntl locks |
| `scripts/_builder/ffi_bridge.py` | Cross-language FFI detection (Python ctypes / Go cgo / Rust extern "C") |
| `scripts/_builder/web_ui.py` | Interactive Web UI server (single-file HTML/SVG/JS) |
| `scripts/_builder/bug_benchmark.py` | BUG benchmark — GraphInvestigator vs GrepInvestigator |
| `scripts/_builder/profile_health.py` | Profile health (0-100 across 7 categories) + auto-evolution + git/svn HEAD binding |
| `scripts/_builder/doc_code_align.py` | Doc-code alignment (return value / param / signature / stale-doc mismatch detection) |
| `scripts/_builder/daemon.py` | Background daemon (inotify + polling fallback + Unix socket API) |
| `scripts/_builder/query_lang.py` | Cypher-subset query parser (MATCH/WHERE/RETURN) |
| `scripts/_builder/value_flow.py` | Value flow / DATA_FLOW edges (parameter→return-value propagation) |
| `scripts/_builder/lock_coverage.py` | Lock-held region analysis with event-stream + char positions |
| `scripts/_builder/path_feasibility.py` | Z3 SMT path feasibility (heuristic fallback when Z3 unavailable) |
| `scripts/_builder/data_dep.py` | Cross-function data dependency (DATA_DEP edges) |
| `scripts/_detector/` | Build system, framework, and community detection |
| `scripts/_profile/` | Profile schema and auto-generation |
| `scripts/config/profiles/` | Built-in project profiles (DO NOT read into context) |
| `docs/en/` | English documentation |
| `docs/zh/` | Chinese documentation |

## Important Constraints

- **Never pre-load** `scripts/config/profiles/` or `docs/*/references/` into context — read on demand only
- **Global-to-local query mode**: always start from micro/lite context packs, then drill down
- **Only seven labels** supported (API_entry, thread_processor, callback_func, constructor, destructor, out_end, unknown_end)
- **Do not propose fixes** before finding root cause
- **Always verify** after sync/update operations
- **Transactional writes**: DB-modifying operations (`update-node`, `update-edge`, `patch-profile`, `apply-semantics`, `apply-invariants`, `auto-enhance`, `apply-knowledge`, `doc-mark-stale`, `profile-evolve`) should be wrapped in `tx-begin`/`tx-commit` for multi-step changes. `patch-from-diff`/`patch-from-git` already wrap in a transaction by default; use `--no-transaction` to bypass.
- **Daemon freshness**: if `daemon-status` shows pending events or reports `syncing`, call `daemon-wait-sync` before important queries to ensure the graph is up-to-date. Daemon uses a circuit breaker (>1000 events/min → bulk rebuild).
- **Doc-code alignment**: before reporting a bug based on `semantic_desc`, check `describe-node` output for `doc_code_mismatches` — if non-empty, the doc may be stale; consult `body_text` and consider `doc-mark-stale`.
- **Invariants confidence**: invariants carry confidence (EXTRACTED/INFERRED/AMBIGUOUS). Never apply AMBIGUOUS invariants; INFERRED require user review.
- **FFI tracing**: requires both source and target language scanners to detect a binding site (e.g., Python ctypes invocation site + C function definition).
- **Profile evolution**: `profile-evolve --apply` only applies EXTRACTED-confidence suggestions; INFERRED require manual review. Run `profile-bind-version` after evolution to bind to current git/svn HEAD.
- **Commit provenance**: node/edge `commit_meta.source_commit` is a git/svn hash — engineers verify with `git show <hash>`, not timestamps.

## MCP Server

```bash
python3 scripts/code2database_builder.py serve --graph code2db-out/
```

Exposes 81 MCP tools (53 base + 28 design-report) (34 `code2database_*` + 19 `cgdb_*`) over stdio transport for real-time LLM agent queries. The 19 `cgdb_*` tools query the cgdb (code graph database) layer directly when the clang extraction backend is enabled. Daemon mode provides additional freshness tools via Unix socket at `/tmp/code2database-daemon-<project>.sock`:

```bash
# Query daemon freshness before important MCP queries
python3 scripts/code2database_builder.py daemon-status --graph code2db-out/
python3 scripts/code2database_builder.py daemon-wait-sync --graph code2db-out/ --timeout 30
```

## Daemon Mode

```bash
# Start (foreground; blocks)
python3 scripts/code2database_builder.py daemon-start --graph code2db-out/ --source /path/to/project

# In another terminal — query status, pause for manual updates, resume
python3 scripts/code2database_builder.py daemon-status --graph code2db-out/
python3 scripts/code2database_builder.py daemon-pause --graph code2db-out/ --reason "manual update"
python3 scripts/code2database_builder.py daemon-resume --graph code2db-out/

# Force-refresh a specific file
python3 scripts/code2database_builder.py daemon-force-refresh --graph code2db-out/ --path src/foo.c

# Stop
python3 scripts/code2database_builder.py daemon-stop --graph code2db-out/
```

Daemon writes state to `<graph_dir>/.daemon_status.json` and logs to `~/.code2database/daemon-<project>.log`. Configuration via profile `daemon` section (see `docs/<lang>/RUNTIME_CONFIG.md`).
