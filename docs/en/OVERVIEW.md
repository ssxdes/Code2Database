# Code2Database — Implementation Overview

> **Note**: This document describes internal architecture and algorithms for developers working on Code2Database itself. **AI agents should NOT load this file** — use `SKILL.md` for usage instructions.

This document describes the internal architecture, data flow, design rationale, code framework, and technology stack of Code2Database for developers who want to understand how the tool works under the hood.

## The Design Goal: A Code Database, Not Just a Call Graph

Code2Database was built to answer a question that traditional call-graph tools cannot: **"What does an engineer actually need to reason about when reading this code?"** The answer is rarely just "A calls B." It's:

- **Under what conditions** does A call B? (`call_condition` — if/switch/#ifdef/ternary, cross-language `//go:build` / `#[cfg]` / `sys.platform` / `@Profile`)
- **In what concurrency context** does the call happen? (`thread_model`, `concurrency` edge attribute)
- **What state** does the call touch? (`globals_read/written`, `fields_read/written`, per-field SQL-native `field_access` table)
- **Is the call certain?** (`confidence` — EXTRACTED/INFERRED/AMBIGUOUS + evidence trace)
- **Does the call exist under this build config?** (`ifdef_conditions`, `preproc_alive`, Z3 SMT `path-feasible`)
- **What happens if I change it?** (`blast-radius` — transitive impact)
- **How did execution reach this point?** (`reverse-trace` — backward BFS from any node)
- **Which commit introduced this?** (`commit_meta.source_commit` — git/svn hash verified, not timestamp)
- **Can these two chains race?** (`concurrency-analyze` — pair-wise thread model + lock overlap)
- **Does the doc still match the code?** (`doc_code_mismatches` — return/param/signature/stale-doc)

Most tools stop at the first question. Code2Database answers all of them, persisting the answers in a graph that an LLM agent can query with a single tool call instead of grep/glob/Read across N files. That's the shift from *reading* code to *querying* code.

The architecture below is in service of that goal: a three-stage pipeline that separates immutable AST facts from iterable inferences, a tiered context-pack system that minimizes token cost, a dual tree-sitter + clang extraction backend, a typed cgdb (code graph database) layer for semantic tables, transactional updates with WAL + snapshots, and a real-time daemon for keeping the graph fresh.

## Design Rationale

### Why a Three-Stage Compiler Pipeline (Profile → Scan → Build)

Code2Database deliberately separates three concerns that other tools conflate:

| Stage | Input | Output | Why separated |
|-------|-------|--------|---------------|
| **Profile** | source directory | project profile JSON | Per-project knowledge (callback patterns, struct_op_types, export macros, skip_names) is externalized so the tool code stays generic. New projects only need a new profile, not new tool code. |
| **Scan** | source files + profile | extraction.json (immutable facts) | AST extraction is expensive (minutes for large projects). Facts are verifiable and should never be re-derived. Caching them lets builders iterate in seconds. |
| **Build** | extraction.json + profile | graph + indexes + context packs | Inference (vtable dispatch, callback bridging, race detection, invariants) is iterable — improving one algorithm only requires re-running Build, not re-scanning. |

This mirrors a compiler (frontend → IR → backend): the frontend is the slow, deterministic, project-agnostic part; the backend is the fast, heuristic, project-aware part. The same separation lets Code2Database add new analyses without touching scanners.

### Why Dual Tree-Sitter + Clang Backend

The C/C++ extraction backend has two modes that serve different needs:

- **tree-sitter** (default fallback, no system dependency) — robust to malformed code, handles C/C++/Go/Python/Java/Rust uniformly, produces the canonical legacy graph (functions/edges/vtable_registrations). Cannot resolve types precisely.
- **clang** (optional, requires `pip install libclang==17.0.6`) — uses libclang's AST for precise type resolution, USR-stable node IDs, CFG basic blocks, def-use data flow, sync primitives, happens-before, typed vtable dispatch (FieldDecl → FunctionDecl). Populates the cgdb layer with 13 typed semantic tables.

The `auto` backend (default) runs both: tree-sitter provides legacy-shape data, clang provides cgdb tables, and the `DualBackendScanner` merges them. If libclang is missing, the system degrades gracefully — every supported language can still be scanned, built, and queried; only the 19 `cgdb_*` MCP tools return empty results.

This design lets Code2Database scale from a quick install (`pip install tree-sitter-c`) to a full semantic database (`pip install libclang==17.0.6`) without code changes.

### Why Three Sub-Skills Instead of One

The skill ships as 3 sub-skills (`/Code2Database` core, `/Code2Database-analysis` deep analysis, `/Code2Database-ops` operations) so the LLM agent loads only the commands relevant to its current question:

- **Core (15 Tier-1 commands)** — always loaded. Build, browse, basic query (scan, build, explore-flow, describe-node, trace-chain, neighbors, path, search, key-paths, etc.)
- **Analysis (13 Tier-1 + 19 cgdb_* MCP tools)** — loaded on demand. Concurrency, data flow, invariants, FFI, path feasibility, provenance, cgdb tables.
- **Ops (14 Tier-1 commands)** — loaded on demand. Transactions, daemon, profile health, doc-code alignment, exports, plugins, memory, embeddings.

All 213 CLI commands are accessible via the shared `scripts/code2database_builder.py` regardless of which sub-skill is active. The split is purely about LLM context economy: a 4K-token core skill is always useful; a 20K-token analysis skill should only be loaded when the user asks about races or invariants.

### Why micro → lite → local Query Mode

LLM token cost dominates the user experience. Code2Database solves this with a tiered context-pack system:

```
micro pack (~200 tokens) → lite pack (~500 tokens) → explore-flow → describe-node → get-code-snippet
```

Each tier provides progressively more detail at higher token cost. The agent starts with the micro pack (project snapshot), upgrades to lite (structure), uses explore-flow to locate relevant functions, then drills into specific nodes. A typical bug-hunt session consumes <2K tokens before the agent even calls describe-node — vs. 10K+ tokens if the agent had to grep and Read source files directly.

### Why cgdb (Code Graph Database) Layer

> **Naming clarification (v4+)**: The cgdb layer names below (CGDB-L0 through CGDB-L11 + FTS) are **different from** the design-report L1~L4 layers (`C代码数据库化方案-分析与执行报告.md`):
> - **Report-L1** (无损重建层 / lossless reconstruction): `tokens` / `macros` / `macro_invocations` / `pp_branches` / `pp_directives` / `pragmas` / `attributes` / `literals` / `string_literals` / `comments` — added in schema v4
> - **Report-L2** (AST 层): `symbols` / `ast_nodes` / `references` / `call_edges` / `includes` / `globals` / `types` / `modules` — partially covered by `cgdb_nodes` / `cgdb_edges` / `cgdb_types` / `cgdb_includes`
> - **Report-L3** (IR 层): `ir_functions` / `ssa_values` / `mem_accesses` / `points_to` / `indirect_calls` / `data_deps` / `path_states` — added in schema v4 (LLVM Pass + SVF integration is P1, see `ir_adapters.py`)
> - **Report-L4** (派生层 / derived): `call_graph_reachability` / `module_deps` / `function_embeddings` / `precise_write_sets` / `arch_metrics` / `history_snapshots` / `alignment_errors` — added in schema v4
> - **Report-多库** (multi-DB routing): `db_routing` / `precompute_tasks` — added in schema v4 (router not yet implemented)
> - **Report-跨语言** (cross-language bridge): `cross_lang_bindings` / `type_mappings` / `ffi_call_sites` / `language_adapters` / `runtime_observations` / `dependencies` — added in schema v4
>
> The legacy cgdb layers (CGDB-L0 through CGDB-L11) below are the original semantic-table layers populated by the clang backend; the report layers above are additive (they sit side-by-side in the same SQLite database). See `report/Code2Database-最终差距分析与优化报告.md` for the full gap matrix.

The legacy graph (`functions` + `edges` tables) answers "who calls whom." The cgdb layer adds typed semantic tables that answer questions the legacy graph cannot:

| CGDB-Layer | Table | Answers |
|------------|-------|---------|
| CGDB-L0 | `graph_versions` | Per-commit snapshot for time-travel queries |
| CGDB-L1 | `cgdb_nodes`, `cgdb_files` | Multi-kind first-class nodes (function/method/ctor/dtor/var/parm/field/struct/class/enum/stmt/expr/label/namespace/template/concept/file/macro/include/vtable/ops_table) + file registry |
| CGDB-L2 | `cgdb_types` | Independent type system with size/alignment/const/volatile/pointee |
| CGDB-L3 | `conditions` | Z3-reasonable boolean expression trees |
| CGDB-L3.5 | `config_predicates` | `#ifdef` predicate tree (BDD + Z3 form), cross-language (Go `//go:build`, Rust `#[cfg]`, Python `sys.platform`, Java `@Profile`, ASM/C `#ifdef`) |
| CGDB-L4 | `basic_blocks` + `cfg_edges` | Control flow graph |
| CGDB-L5 | `data_flow` + `alias_sets` | Def-use chain + pointer alias (alias_sets is heuristic stub; Report-L3 `ssa_values`/`points_to`/`indirect_calls` are the full version) |
| CGDB-L6 | (alias — see CGDB-L5 / Report-L3) | (Reserved for full alias analysis via SVF; currently in CGDB-L5 `alias_sets` table) |
| CGDB-L7 | `invoke_sites` + `ops_bindings` | Invocation refinement + typed vtable dispatch (FieldDecl → FunctionDecl) |
| CGDB-L8 | `sync_primitives` + `happens_before` | Concurrency + memory model |
| CGDB-L9 | `cgdb_includes` | `#include` dependency graph for incremental sync |
| CGDB-L10 | `doc_comments` + `graph_versions` | Doc comments + time-travel version queries |
| CGDB-L11 | `node_metadata` + `edge_metadata` | Typed-key metadata per target |
| FTS | `nodes_fts` | Full-text search over cgdb_nodes (FTS5 external-content) |

Plus the design-report v4 layers (additive, populated by `ir_adapters.py` and `source_renderer.py` when full toolchain is installed):

| Report-Layer | Tables (v4) | Populated by |
|--------------|-------------|--------------|
| Report-L1 | `tokens` / `macros` / `macro_invocations` / `pp_branches` / `pp_directives` / `pragmas` / `attributes` / `literals` / `string_literals` / `comments_freeform` + `comments_fts` | `l1_ingest.py` (P1, pending) — libclang Lexer raw_tokens + PPCallbacks simulation |
| Report-L2 | (covered by cgdb_nodes/edges/types/includes) | existing scanners |
| Report-L3 | `ir_functions` / `ssa_values` / `mem_accesses` / `points_to` / `indirect_calls` / `data_deps` / `path_states` | `ir_adapters.py` (P1 — LLVMIRAdapter/JimpleIRAdapter/etc.) |
| Report-L4 | `call_graph_reachability` / `module_deps` / `function_embeddings` / `precise_write_sets` / `arch_metrics` / `history_snapshots` / `alignment_errors` | `l4_derive.py` (P1, pending) — precompute tasks |
| Report-多库 | `db_routing` / `precompute_tasks` | `db_router.py` (P1, pending) |
| Report-跨语言 | `cross_lang_bindings` / `type_mappings` / `ffi_call_sites` / `language_adapters` / `runtime_observations` / `dependencies` | `ffi_bridge.py` (existing) + `ir_adapters.py` |

These tables are populated by the clang backend (for legacy cgdb layers) and the IR/L1/L4 pipelines (for report layers). They are queried by 19 `cgdb_*` MCP tools (legacy) plus 28 design-report MCP tools (`render_source` / `verify_consistency` / `edit_token` / `find_symbol` / `callers_of` / `indirect_targets` / `commit_db_transaction` / etc.). All tables coexist in the same SQLite database (`code2database.db`).

### Why Transactional Updates

Graph modifications (LLM auto-enhance, patch-from-diff, daemon sync) need ACID-like guarantees:

- **Snapshot**: copy `code2database.db` + key JSON files to `.code2database_tx/snapshots/<id>/` before any write.
- **WAL (Write-Ahead Log)**: every write is appended to `.code2database_tx/wal.jsonl` *before* it's applied. Crash mid-write = replay or rollback.
- **Two-phase commit**: WAL first (phase 1), apply to live DB (phase 2), checkpoint (phase 3).
- **File lock**: `fcntl` on Linux, `msvcrt` on Windows — multi-process coordination.

`transaction()` is a context manager: `with transaction(graph_dir):` commits on success, rolls back on exception. `tx-replay-wal` recovers from crashes.

### Why a Daemon

The `watch` command is one-shot; `install-hook` only fires on git commit. Engineers need a long-running process that:

- Watches source files in real time (inotify on Linux, polling elsewhere — zero extra dependencies)
- Batches changes (500ms debounce + 1000ms batch window)
- Wraps updates in transactions
- Auto-rebuilds output files (CODE2DATABASE_SUMMARY.md, context packs, etc.)
- Reports freshness to LLM agents via `.daemon_status.json` + Unix socket
- Has a circuit breaker (>1000 events/min → bulk rebuild)

The daemon coordinates with manual updates via `pause`/`resume` socket commands and exposes a `wait-sync` command that MCP clients should call before important queries.

## Pipeline Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                        PROFILE STAGE                                 │
│  Input: source directory                                             │
│  Output: profile JSON (project-specific scanner/builder config)      │
│  Responsibility: Detect project type, extract domain rules,          │
│                  struct_op_types, export macros, callback patterns   │
│  Files: scripts/_profile/schema.py, generate.py, llm_phases.py      │
│  Built-in profiles: linux_kernel, dpdk, spdk, qemu, zephyr,         │
│                     freertos, asm_default, go/java/python/rust/_default│
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                         SCAN STAGE                                   │
│  Input: source files + profile config + extraction_backend choice    │
│  Output: .code2database_extraction.json (immutable facts) +          │
│          .code2database_manifest.json (file fingerprints)             │
│  Responsibility: Extract raw facts from source code — function       │
│                  definitions, call expressions, callback regs,       │
│                  struct field assignments, #ifdef conditions,        │
│                  cgdb_* tables (when clang backend enabled)          │
│  Backend: auto (dual) | clang (cgdb-only) | tree-sitter (legacy)    │
│  Files: scripts/_scanner/c_scanner.py, clang_scanner.py,            │
│         dual_scanner.py, go_scanner.py, python_scanner.py,          │
│         java_scanner.py, rust_scanner.py, asm_scanner.py,           │
│         base.py, unified_id.py, changes.py, utils.py,               │
│         config_predicates_lang.py                                    │
│  CLI: scripts/code2database_scanner.py                               │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                        BUILD STAGE                                   │
│  Input: .code2database_extraction.json + profile config              │
│  Output: code2db-out/ (graph + indexes + docs + context packs +     │
│          code2database.db with legacy + cgdb tables)                 │
│  Responsibility: Inference and graph construction — vtable dispatch, │
│                  callback bridging, domain classification, external  │
│                  code separation, community detection, endpoint      │
│                  classification, entry scoring, data race detection, │
│                  lock-coverage, invariants, FFI, doc-code,           │
│                  commit-provenance binding to git/svn HEAD           │
│  Files: scripts/_builder/graph_build.py (core, 7447 lines),         │
│         streaming_graph.py, index_pack.py, query.py,                │
│         entry_scoring.py, concurrency_analysis.py,                  │
│         import_resolve.py, lock_coverage.py, invariants.py,         │
│         ffi_bridge.py, doc_code_align.py, profile_health.py,        │
│         value_flow.py, data_dep.py, path_feasibility.py,            │
│         query_lang.py, commit_meta.py, transactions.py,             │
│         daemon.py, mcp_server.py, cgdb_store.py,                    │
│         cgdb_schema.py, cgdb_ingest.py, cgdb_commands.py,           │
│         cgdb_analysis.py, cgdb_ops_bind.py,                         │
│         cgdb_config_predicates.py, cgdb_incremental.py,             │
│         cgdb_versions.py, cgdb_migrations.py, cgdb_records.py,      │
│         cgdb_sync.py, sqlite_store.py, sqlite_postprocess.py,       │
│         memory_manager.py, knowledge_manager.py, semantics.py,      │
│         auto_enhance.py, web_ui.py, bug_benchmark.py, etc.          │
│  CLI: scripts/code2database_builder.py (146 subcommands)             │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼  (optional)
┌──────────────────────────────────────────────────────────────────────┐
│                      DAEMON STAGE                                    │
│  Input: source file changes (inotify events or polling)              │
│  Output: transactional graph updates + freshness marker              │
│  Responsibility: Watch source paths, debounce + batch events,        │
│                  wrap updates in transaction, auto-rebuild           │
│                  output files, expose Unix socket API                │
│  Files: scripts/_builder/daemon.py, watcher.py                      │
│  CLI: scripts/code2database_builder.py daemon-start                  │
│  Socket: /tmp/code2database-daemon-<project>.sock                    │
│  State: <graph_dir>/.daemon_status.json                             │
│  Log: ~/.code2database/daemon-<project>.log                          │
└──────────────────────────────────────────────────────────────────────┘
```

## Key Design Principles

### 1. Fact vs Inference Separation

The most important architectural decision: **Scan produces immutable facts, Build produces iterable inferences**.

- **extraction.json** contains what the source code literally says — function definitions, direct calls, struct field assignments. These are verifiable facts.
- **Build outputs** contain derived information — vtable dispatch edges (inferred from struct field assignments), callback bridges (2-hop resolution), domain classifications, etc. These can be improved without re-scanning.

This separation means:
- Debugging: If a call is missing, check extraction.json to see if the scanner missed it, or if the builder's inference failed.
- Iteration: Improving vtable resolution only requires re-running Build (seconds), not Scan (minutes for large projects).
- New projects: Only the Profile needs to change; the scanner and builder remain the same.

### 2. Global-to-Local Query Model

LLM agents follow a tiered query pattern to minimize token consumption:

```
micro pack (~200t) → lite pack (~500t) → explore-flow → describe-node → get-code-snippet
```

Each tier provides progressively more detail at higher token cost. The agent starts with the micro pack for a project snapshot, upgrades to lite for structure, uses explore-flow to locate relevant functions, then drills into specific nodes.

### 3. Conditional Compilation Awareness (Cross-Language)

The C scanner annotates functions and edges with `ifdef_conditions` — a list of `#ifdef`/`#ifndef`/`#if` conditions that guard the code. Other languages have equivalent constructs, all normalized to the cgdb L3.5 `config_predicates` layer:

- **C/C++/ASM**: `#ifdef` / `#ifndef` / `#if defined()` / `#elif` / `#else`
- **Go**: `//go:build` tags → `CONFIG_GO_TAG_<NAME>`
- **Rust**: `#[cfg(...)]` / `#[feature = "..."]` → `CONFIG_CFG_<KEY>_<VAL>` / `CONFIG_FEATURE_<NAME>`
- **Python**: `sys.platform == "linux"` / `os.name == "posix"` → `CONFIG_PY_PLATFORM_<VAL>`
- **Java**: `@Conditional(...)` / `@Profile("...")` → `CONFIG_JAVA_CONDITIONAL_<NAME>` / `CONFIG_JAVA_PROFILE_<NAME>`

This enables:
- `extract-signals` — map conditional macros to affected functions/edges/domains
- `resolve-chain` with `--bindings` — trace only paths alive under specific macro configurations
- `diff-chains` — compare execution paths under two different macro configurations
- `cgdb_find_nodes_under_config` — list all nodes gated by a config predicate
- `cgdb_find_configs_for` — list config predicates affecting a node
- `path-feasible` — Z3 SMT feasibility under constraints (sound when Z3 installed, heuristic fallback)

The C scanner tracks the preprocessor condition stack during AST traversal, handling nested `#ifdef`/`#elif`/`#else` blocks with proper negation. The clang backend additionally builds Z3 SMT-LIB forms for each condition (L3 `conditions` table) for sound path feasibility.

### 4. Concurrency Safety Analysis

Unlike most existing tools, Code2Database can detect **data races** by combining:

- **Thread model detection**: Functions annotated with `thread_model` and `thread_entry` (from callback/spawn detection — `pthread_create`, `std::thread`, `threading.Thread`, `Thread()`, `spawn`, `tokio::spawn`, goroutines via `go` statement, `CreateThread`, `_beginthread`)
- **Shared state tracking**: Functions annotated with `globals_read`/`globals_written` and `fields_read/written` (SQL-native `field_access` table for O(log n) queries)
- **Lock detection**: Profile-driven lock acquire/release patterns (no hardcoded lock APIs — empty by default, projects populate via `concurrency_patterns.lock_acquire_patterns`); precise lock-held regions via `lock-coverage` event stream with char positions
- **Happens-before** (cgdb L8): `sync_primitives` + `happens_before` tables enable pair-wise ordering checks
- **Race detection**: Pairs of functions in different thread contexts accessing the same resource without common lock protection

Commands: `concurrency-risks` (global), `concurrency-analyze` (pair-wise), `detect-races` (cross-thread), `field-access` (per-resource), `who-locks`, `lock-coverage`, `happens-before`, `memory-ordering`.

### 5. External Code Separation

Third-party and vendor code (vendor/, third_party/, contrib/, huawei/) is automatically classified into `external_*` domains, separated from project domains in all outputs:

- CODE2DATABASE_SUMMARY.md shows external domains in a dedicated section
- Context packs separate project vs external domain data
- Entry scoring filters out external functions

This prevents test utilities and vendor code from polluting API entry point detection.

### 6. Cross-Domain Leiden Community Detection

Standard Leiden community detection produces communities identical to domain-based splitting. To add value, Code2Database:

1. Uses `RBConfigurationVertexPartition` with resolution parameter tuning to produce fewer, broader communities than domains
2. Falls back to cross-domain affinity analysis when Leiden communities ≈ domains
3. Produces `domain_overlap` mapping showing which domains are merged into each community

This reveals cross-cutting concerns (e.g., bdev + nvmf forming a storage community) that pure directory-based grouping misses. Falls back to domain-based grouping if `python-igraph` + `leidenalg` are not installed.

### 7. Non-Destructive LLM Supplements

The database write constraint is "content may be missing but must be accurate." Every LLM-driven write (`update-node`, `update-edge`, `apply-semantics`, `apply-invariants`, `auto-enhance`, `profile-evolve`) stores the supplement as `{key}_supplemented` fields, **NOT overwriting** original scan data. Each supplement includes `_supplement_meta` (source / confidence / timestamp / original), auditable in `describe-node` output. `rollback` reverts by time or scope. Original scan facts are always preserved.

### 8. Confidence-Threshold Auto-Write

LLM supplements carry confidence labels: `EXTRACTED` (directly parsed with evidence), `INFERRED` (heuristic, plausible), `AMBIGUOUS` (uncertain). The auto-write policy:

- `EXTRACTED` + sufficient evidence → auto-write, no confirmation
- `INFERRED` → require user confirmation (LLM MUST report old/new value, source, confidence before executing)
- `AMBIGUOUS` → rejected, never applied

This prevents LLM hallucinations from polluting the graph while letting high-confidence enhancements flow through without user friction.

## Data Model

### Node Attributes

Every node in the invocation graph has:

| Attribute | Type | Description |
|-----------|------|-------------|
| `id` | str | Fully qualified: `project.domain.file.function` (cross-language unified ID: SHA-256 truncated to 60 bits with language prefix, see `unified_id.py`) |
| `name` | str | Function name |
| `domain` | str | Architecture domain (e.g., `lib.bdev`) |
| `source_file` | str | Source file path |
| `line` | int | Line number of definition |
| `signature` | str | Function signature |
| `labels` | list[str] | One of: `API_entry`, `thread_processor`, `callback_func`, `constructor`, `destructor`, `out_end`, `unknown_end` |
| `is_empty` | bool | Condition branch aggregation node |
| `is_external` | bool | Whether in external code domain |
| `ifdef_conditions` | list[str] | `#ifdef` conditions guarding this function |
| `thread_model` | str | Thread model (from spawn/callback detection) |
| `thread_entry` | bool | Whether this function is a thread entry point |
| `entry_score` | float | Multi-factor entry point score |
| `globals_read/written` | list[dict] | Global variables accessed |
| `fields_read/written` | list[dict] | Struct fields accessed (with struct_chain + field_name) |
| `commit_meta` | dict | `{source_commit, author, date, message}` from git/svn HEAD at scan time |
| `preconditions` | list[str] | Conditions that must hold on entry (extracted from body) |
| `postconditions` | list[str] | Conditions guaranteed on exit |
| `loop_invariants` | list[str] | Invariants maintained by loops in the body |
| `state_machine` | dict | `{state_var, states, transitions}` if assignment-count >= 1 |
| `doc_stale` | bool | Set by `doc-mark-stale` when doc/code mismatch detected |
| `doc_stale_reason` | str | Reason for staleness |
| `ffi_binding` | dict | Present on FFI bridge nodes: `{source_lang, target_lang, binding_type}` |
| `_supplement_meta` | dict | Provenance of LLM-applied supplements (source/confidence/timestamp/original) |

### Edge Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `relation` | str | `INVOKES`, `CONTAINS`, `IMPORTS`, `DATA_FLOW`, `DATA_DEP`, `FFI_BRIDGE`, `IMPLEMENTS`, `OPS_BIND` |
| `call_order` | int | Position in caller's call sequence |
| `call_condition` | str | Condition under which this call occurs (e.g., `SPDK_CONFIG_APP_RW`) |
| `concurrency` | str | `vtable_dispatch`, `field_dispatch`, `callback`, `spawn_target`, `thread_spawn`, `goroutine` |
| `confidence` | str | `EXTRACTED`, `INFERRED`, `AMBIGUOUS` |
| `evidence` | str | Human-readable justification for the edge |
| `commit_meta` | dict | Same schema as node `commit_meta` |
| `data_flow_var` | str | Variable name propagated by a `DATA_FLOW` edge |
| `data_flow_kind` | str | `param_to_return`, `param_to_param`, `return_to_param` |
| `data_dep_kind` | str | `read_after_write`, `write_after_read`, `write_after_write` |
| `lock_event` | dict | For `lock-coverage`: `{kind: acquire/release, lock_var, char_pos, line, col}` |
| `ffi_marshalling` | dict | For `FFI_BRIDGE` edges: `{src_type, target_type, lossy, conversion_note}` |

### Context Pack Tiers

| Tier | Target Tokens | Content |
|------|--------------|---------|
| **micro** | ~200 | Project name, architecture one-liner, pattern keywords, top 3 domains/APIs, stats |
| **lite** | ~500 | micro + domain map, full API catalog, thread/callback entries, core data flows |
| **standard** | ~1500 | lite + all domains, execution processes, concurrency summary |
| **full** | full | Everything: community map, all scenarios, data flow index, hub functions |

### cgdb Layer Tables (when clang backend enabled)

| Layer | Table(s) | Purpose |
|-------|----------|---------|
| L0 | `graph_versions` | Per-commit snapshot for time-travel queries |
| L1 | `cgdb_nodes`, `cgdb_files` | Multi-kind first-class nodes + file registry |
| L2 | `cgdb_types` | Independent type system (builtin/pointer/reference/array/record/enum/function/template/typedef) |
| L3 | `conditions` | Z3 SMT-LIB boolean expression trees |
| L3.5 | `config_predicates` | `#ifdef` predicate tree (BDD + Z3 form), cross-language |
| L4 | `basic_blocks`, `cfg_edges` | Control flow graph |
| L5 | `data_flow`, `alias_sets` | Def-use chain + pointer alias |
| L7 | `invoke_sites`, `ops_bindings` | Invocation refinement + typed vtable dispatch |
| L8 | `sync_primitives`, `happens_before` | Concurrency + memory model |
| FTS | `nodes_fts` | FTS5 virtual table for symbol search |

See `references/data_model.md` for full schemas.

## Code Framework

Code2Database is organized into 5 packages under `scripts/`, plus a CLI entry layer. Total ~70K lines of Python.

### Package Layout

```
scripts/
├── code2database_builder.py      ← CLI entry point (146 subcommands, argparse routing)
├── code2database_scanner.py      ← Scanner CLI entry point (8 subcommands)
├── setup.sh                      ← Dependency installer (per-language option)
├── requirements.txt              ← Pinned dependencies
├── check_docs_sync.py            ← Doc-code sync verifier
├── iterate_precision.py          ← Precision iteration helper
├── run_evals.py                  ← Eval runner (evals/evals_*.json)
├── verify_edge_attribution.py    ← Edge attribution verifier
├── _vendor/                      ← Vendored shims (networkx fallback)
│
├── _scanner/                     ← Language-specific AST scanners (12K lines)
│   ├── base.py                   ← BaseScanner ABC: scan_file, _extract, _walk pattern,
│   │                                config predicate annotation, cgdb record emission,
│   │                                concurrency info detection, doc comment extraction,
│   │                                import/include extraction, condition Z3 form generation,
│   │                                sync primitive pattern detection (per-language)
│   ├── unified_id.py             ← Cross-language node ID: SHA-256 truncated to 60 bits,
│   │                                high bit cleared for SQLite signed INTEGER, language prefix
│   ├── changes.py                ← File fingerprint (mtime_ns:size), manifest save/load,
│   │                                detect_changes for incremental updates
│   ├── utils.py                  ← MIN_PYTHON=(3,8), EXTENSION_MAP, LANG_EXTENSIONS,
│   │                                classify_domain
│   ├── c_scanner.py              ← C/C++ tree-sitter scanner (largest, 2992 lines):
│   │                                preprocessor #ifdef stack, vtable_registrations extraction,
│   │                                macro dispatch, callback patterns, fn_ptr_calls,
│   │                                C++ templates/concepts/coroutines, MSVC __asm blocks
│   ├── clang_scanner.py          ← libclang backend (1136 lines): USR-stable IDs, typed nodes,
│   │                                full cgdb_* table population (L1-L8), compile_commands.json
│   ├── dual_scanner.py           ← DualBackendScanner: merges tree-sitter + clang outputs
│   ├── go_scanner.py             ← Go tree-sitter: goroutine detection, //go:build, methods, goto
│   ├── python_scanner.py         ← Python tree-sitter: classes, decorators, sys.platform, match
│   ├── java_scanner.py           ← Java tree-sitter: package, imports, methods, @Profile
│   ├── rust_scanner.py           ← Rust tree-sitter: impl/trait, #[cfg], pub visibility, async
│   ├── asm_scanner.py            ← ASM regex scanner (2784 lines): NASM x86_64 + GNU as AT&T +
│   │                                ARM/AArch64/RISC-V/LoongArch/s390/PowerPC/SuperH/MIPS/IA-64,
│   │                                kernel SYM_FUNC_START/ENTRY/EXPORT_SYMBOL macros, register
│   │                                data flow tracking, syscall number resolution
│   └── config_predicates_lang.py ← Cross-language config predicate extraction:
│                                    Go //go:build, Rust #[cfg], Python sys.platform,
│                                    Java @Profile/@Conditional, ASM/C #ifdef
│
├── _detector/                    ← Detection modules (1.8K lines)
│   ├── build_detector.py         ← Build system detection: CMake, Make, Meson, Autotools,
│   │                                Kconfig, Bazel, MSBuild, spec. Extract -D macros, targets,
│   │                                include dirs. evaluate_pp_condition for #ifdef resolution
│   ├── community_detector.py     ← Leiden community detection (igraph + leidenalg) with
│   │                                RBConfigurationVertexPartition + cross-domain affinity
│   │                                fallback to domain grouping when libs unavailable
│   └── framework_detector.py     ← Framework detection: Django/Flask/FastAPI/Spring/Gin/Echo/
│                                    Actix/Rocket/Tokio/Qt/GTK/libevent/libuv/etc.
│
├── _profile/                     ← Project profile system (4.8K lines)
│   ├── schema.py                 ← ProfileSchema: load/validate/merge/to_scanner_config/
│   │                                to_builder_config. _DEFAULT_PROFILE embedded as Python dict
│   ├── generate.py               ← Auto-profile generation: pre-scan, test scan, auto-config,
│   │                                auto-detect phases. SourceInfoCollector single os.walk
│   └── llm_phases.py             ← LLM-driven Phase 4 (header analysis) + Phase 6 (result check)
│
├── _builder/                     ← Graph building and query modules (54K lines, 70 files)
│   ├── __init__.py               ← Lazy import mechanism (delays module load until first access)
│   ├── graph_build.py            ← Core graph construction (7447 lines): build_graph, cmd_build,
│   │                                domain split, commit hash detection, test domain detection,
│   │                                cgdb wipe-and-rebuild
│   ├── streaming_graph.py        ← StreamingGraph: NetworkX-compatible API that streams to
│   │                                SQLite for low-memory builds (1.4M nodes in ~1.9GB RAM)
│   ├── sqlite_store.py           ← SQLiteStore: WAL journal, 64MB cache, mmap 256MB,
│   │                                schema migration v1→v6, field_access + global_access tables
│   ├── sqlite_postprocess.py     ← Build indexes, CODE2DATABASE_SUMMARY.md, domain READMEs,
│   │                                scenarios, context packs, architecture flows from SQLite
│   ├── cgdb_schema.py            ← cgdb typed semantic schema DDL, CGDB_SCHEMA_VERSION=4, idempotent
│   ├── cgdb_migrations.py        ← Schema evolution (ALTER TABLE in-place, preserves data)
│   ├── cgdb_records.py           ← Dataclass records: NodeRecord, EdgeRecord, TypeRecord,
│   │                                ConfigPredicateRecord, BasicBlockRecord, DataFlowRecord, etc.
│   ├── cgdb_store.py             ← CGDBWriter + CGDBReader ABCs, SQLiteCGDBStore implementation
│   ├── cgdb_ingest.py            ← Bulk import IngestBatch into cgdb tables
│   ├── cgdb_sync.py              ← Sync legacy functions/edges ↔ cgdb_nodes/cgdb_edges
│   ├── cgdb_incremental.py       ← Per-file incremental cgdb update (delete + re-insert)
│   ├── cgdb_versions.py          ← graph_versions time-travel: record version, diff versions
│   ├── cgdb_ops_bind.py          ← Typed vtable dispatch: FieldDecl → FunctionDecl bindings
│   ├── cgdb_config_predicates.py ← ConfigPredicate: BDD + Z3 form, UNCONDITIONAL, CONTRADICTORY
│   ├── cgdb_analysis.py          ← Race detection, lock-held calls, CFG paths over cgdb tables
│   ├── cgdb_commands.py          ← 18+ cgdb_* CLI commands (cgdb-query, cgdb-time-travel, etc.)
│   ├── index_pack.py             ← Context pack generation (micro/lite/standard/full),
│   │                                hub function detection, scenario computation,
│   │                                Mermaid path diagrams (2608 lines)
│   ├── query.py                  ← Query commands: describe-node, trace-chain, reverse-trace,
│   │                                diff-chains, blast-radius, field-access, param-flow,
│   │                                describe-commit, node-history, graph-provenance, blame-node
│   ├── explore.py                ← One-shot explore-flow query engine (keyword → relevant subgraph)
│   ├── key_paths.py              ← Critical path auto-extraction (entry → hub → endpoint)
│   ├── search_cmd.py             ← cmd_load, cmd_search, cmd_path, cmd_neighbors, cmd_impact, cmd_domain
│   ├── entry_scoring.py          ← Multi-factor entry point scoring with test function filtering
│   ├── concurrency_analysis.py   ← Data race detection + pair-wise concurrency safety analysis
│   ├── concurrency.py            ← Concurrency risk listing + data lifecycle tracing
│   ├── memory_ordering.py        ← Memory ordering: atomic ops, barriers, READ_ONCE/WRITE_ONCE, smp_mb
│   ├── import_resolve.py         ← Multi-strategy callee resolution + FQN computation
│   ├── token_budget.py           ← Token estimation and budget-aware truncation
│   ├── memory_manager.py         ← Persistent Q&A memory with decay (root/leaf, scratch)
│   ├── memory_cmd.py             ← Memory CLI: save/search/validate
│   ├── memory_guard.py           ← Memory budget enforcement + auto-consolidation
│   ├── knowledge_manager.py      ← Knowledge extraction, application, query, validation
│   ├── semantics.py              ← Semantic description extraction/application,
│   │                                classify-endpoints, think-chain, extract-signals
│   ├── auto_enhance.py           ← LLM auto-semantic enhancement with confidence-threshold write,
│   │                                batch-confirm, rollback log (1454 lines)
│   ├── invariants.py             ← Extract preconditions/postconditions/loop_invariants/state_machine
│   ├── llm_invariants.py         ← LLM-driven invariant extraction
│   ├── plugins.py                ← Plugin loading and execution
│   ├── patcher.py                ← Incremental patching (patch-from-diff, patch-from-git, light-scan)
│   ├── update_sync.py            ← Update, sync, merge operations
│   ├── update_cmd.py             ← update-node, update-edge, patch-profile (LLM supplements)
│   ├── changelog_update.py       ← quick-update, export-changes, merge-changes, semantic-status
│   ├── export.py                 ← HTML + Obsidian export
│   ├── visualizer.py             ← Graph visualization rendering
│   ├── web_ui.py                 ← Single-file HTML/SVG/JS interactive viewer; HTTP server
│   ├── bug_benchmark.py          ← GraphInvestigator vs GrepInvestigator recall/precision
│   ├── profile_health.py         ← 7-category 0-100 score; evolution suggestions; HEAD binding
│   ├── doc_code_align.py         ← Detect return/param/signature/stale-doc mismatches
│   ├── commit_meta.py            ← Git/svn commit detection, blame, manifest enrichment
│   ├── graph_history.py          ← graph-history, graph-diff, graph-record-version
│   ├── audit_log.py              ← Audit log of past writes to the graph
│   ├── ffi_bridge.py             ← Detect ctypes/cgo/extern "C"; build FFI_BRIDGE edges (948 lines)
│   ├── value_flow.py             ← DATA_FLOW edge construction; param→return propagation
│   ├── lock_coverage.py          ← Lock-held event-stream extraction with char positions
│   ├── path_feasibility.py       ← Z3 SMT encoding; heuristic fallback when Z3 absent
│   ├── data_dep.py               ← DATA_DEP edges; scans ALL nodes for readers/writers
│   ├── intent_router.py          ← Natural-language intent → graph operations
│   ├── query_router.py           ← Query routing (command selection by question type)
│   ├── query_lang.py             ← Cypher-subset parser (MATCH/WHERE/RETURN, 1304 lines)
│   ├── query_cache.py            ← Query result caching
│   ├── transactions.py           ← WAL + snapshots + fcntl file locks; transaction() context
│   ├── daemon.py                 ← inotify + polling; circuit breaker; transactional sync;
│   │                                socket API (1400 lines)
│   ├── watcher.py                ← File change watcher for auto-update
│   ├── update_sync.py            ← cmd_merge, cmd_update, cmd_sync
│   ├── embeddings.py             ← TF-IDF char n-gram embeddings for semantic search
│   ├── explain.py                ← explain-label, why-ambiguous
│   ├── semantic_edges.py         ← who-allocates, who-frees, unbalanced-alloc-free, who-locks,
│   │                                add-semantic-edges
│   ├── logging_utils.py          ← Structured logging (configure_logging, get_logger)
│   ├── mcp_server.py             ← MCP server (stdio transport, 53 tools: 34 code2database_* + 19 cgdb_*)
│   ├── kb_index.py               ← Unified KB FTS5+BM25 index (kb_paragraphs table, cross memory+knowledge query)
│   ├── kb_cluster.py             ← KB clustering (union-find on FTS5 similarity, scope_id/canonical_id/principle_ref)
│   ├── kb_global.py              ← Cross-project global KB (~/.code2database_global_kb/global.db, reusable knowledge)
│   ├── kb_audit.py               ← KB audit (counts by kind, stale, low-confidence, citations, audit_log integration)
│   ├── kb_conflict.py
│   ├── build_multi.py             ← Multi-project aggregate build (manifest-driven, project-name domain prefix)
│   ├── c2d_foreign.py             ← Cross-C2D foreign_refs + watched_c2ds (add/sync/list/remove)
│   ├── c2d_phase2.py              ← Composite query + check-compat + coverage-cross-c2d
│   ├── c2d_phase3.py              ← Vendor stub + FFI auto-link + RPC scan + cross-team knowledge            ← KB conflict detection (within-cluster contradiction pairs) + rollback + forget
│   ├── lsp_server.py             ← LSP server (expose C2D graph to IDEs: definition/references/callHierarchy)
│   ├── lsp_backend.py             ← LSP extraction backend (consume gopls/rust-analyzer/clangd as scan source)
│   ├── validate.py               ← Graph validation
│   └── utils.py                  ← Shared builder utilities (_normalize_id, _resolve_invoked_id,
│                                    _find_node_id, _parse_bindings, _load_globals,
│                                    _ensure_mutable_graph, etc.)
│
├── config/
│   ├── profiles/                 ← Built-in project profiles (DO NOT load into context)
│   │   ├── _default.json         ← Universal default (skip_names, callback suffixes)
│   │   ├── linux_kernel.json     ← Linux kernel (Kconfig, file_operations, EXPORT_SYMBOL)
│   │   ├── dpdk.json             ← DPDK (rte_* APIs, TAILQ, rte_atomic)
│   │   ├── spdk.json             ← SPDK (bdev, rpc, spdk_* APIs)
│   │   ├── qemu.json             ← QEMU (QOM, object_class, trace events)
│   │   ├── zephyr.json           ← Zephyr RTOS (device, kernel, Kconfig)
│   │   ├── freertos.json         ← FreeRTOS (xTask, xQueue, xSemaphore)
│   │   ├── asm_default.json      ← ASM default (NASM + GAS + ARM + RISC-V)
│   │   ├── go_default.json       ← Go default (goroutines, channels, interfaces)
│   │   ├── java_default.json     ← Java default (Spring, Thread, ExecutorService)
│   │   ├── python_default.json   ← Python default (threading, asyncio, ctypes)
│   │   └── rust_default.json     ← Rust default (tokio, std::sync, extern "C")
│   └── runtime.json              ← Runtime config (scan/build/query/memory/semantic sections)
│
└── hooks/
    └── post-commit               ← Git post-commit hook template (auto quick-update)
```

### Module Coupling

The packages are loosely coupled:

- `_scanner` depends on `_profile` (for profile-driven callback patterns) and `_detector.build_detector` (for `evaluate_pp_condition`)
- `_builder` depends on `_scanner` (to re-scan during patch-from-diff), `_detector`, `_profile`
- `_builder` modules communicate through the SQLite store (`sqlite_store.py` + `cgdb_store.py`) — not through direct Python imports — so a module can be replaced without cascading changes
- The CLI entry (`code2database_builder.py`) is a thin argparse router that imports command handlers via lazy import (`_builder/__init__.py`)

### Key Shared Abstractions

- **`unified_node_id(language, fqn, signature, byte_offset)`** — every node ID across every language is a SHA-256 truncated to 60 bits with a language prefix, preventing cross-language collisions while fitting in SQLite's signed INTEGER. See `_scanner/unified_id.py`.
- **`BaseScanner`** — every language scanner inherits from this ABC, sharing the `_walk(node)` recursive pattern with `cond_stack` for conditional call annotation, `_emit_cgdb_records` for cgdb layer population, `_annotate_config_predicates` for cross-language `#ifdef`/`//go:build`/`#[cfg]`/`sys.platform`/`@Profile` normalization.
- **`transaction(graph_dir)`** — every DB-modifying operation should be wrapped in this context manager (snapshot + WAL + file lock). `patch-from-diff`/`patch-from-git` already do this by default.
- **`StreamingGraph`** — NetworkX-compatible API that streams nodes/edges to SQLite instead of holding them in RAM. Drop-in replacement for `nx.DiGraph` in `--storage sqlite --low-memory` mode (1.4M nodes in ~1.9GB RAM).
- **`SQLiteStore` + `CGDBStore`** — coexist in the same `code2database.db`. Legacy `functions`/`edges` tables for backward compat; cgdb typed semantic tables for queries. Schema migrations are idempotent.

## Technology Stack

### Core Runtime

| Component | Purpose | Required? |
|-----------|---------|-----------|
| **Python 3.8+** | Runtime | Required (MIN_PYTHON = (3, 8) in `_scanner/utils.py`) |
| **networkx ≥3.0** | In-memory graph engine (DiGraph, BFS, shortest paths) | Required (vendored shim in `_vendor/` for fallback) |
| **tree-sitter ≥0.22** | AST parser framework | Required |

### Per-Language Tree-Sitter Grammars

| Grammar | Language | Required for |
|---------|----------|--------------|
| `tree-sitter-c ≥0.21` | C | C scanning |
| `tree-sitter-cpp ≥0.22` | C++ | C++ scanning (templates, concepts, coroutines) |
| `tree-sitter-go ≥0.21` | Go | Go scanning (goroutines, channels, interfaces) |
| `tree-sitter-python ≥0.21` | Python | Python scanning (classes, decorators, match) |
| `tree-sitter-java ≥0.21` | Java | Java scanning (package, imports, methods) |
| `tree-sitter-rust ≥0.21` | Rust | Rust scanning (impl/trait, async, cfg) |

ASM (.s .S .asm) uses regex-based scanning — no tree-sitter grammar needed.

`scripts/setup.sh` supports `--languages c,go` for partial installs; `C2D_LANGUAGES` env var does the same.

### Optional Advanced Features

| Component | Purpose | Enabled by |
|-----------|---------|------------|
| **libclang ≥17.0** (pip install libclang==17.0.6) | clang AST backend → cgdb layer (typed vtable dispatch, CFG, data flow, sync primitives, config predicates, ops bindings, happens-before) | `--extraction-backend clang` or `auto` with libclang installed |
| **z3-solver ≥4.12** | Sound SMT path feasibility (`path-feasible`, `cgdb-path-feasible`); heuristic fallback when absent | `pip install z3-solver` |
| **python-igraph ≥0.11** + **leidenalg ≥0.10** | Cross-domain Leiden community detection | Falls back to domain-based grouping when absent |

### Storage

| Component | Purpose |
|-----------|---------|
| **SQLite** (stdlib `sqlite3`) | Primary storage backend (`code2database.db`): legacy tables + cgdb typed semantic tables |
| **WAL journal mode** | `PRAGMA journal_mode=WAL` for concurrent readers + single writer |
| **zlib compression** | `body_text_compressed BLOB` for function bodies |
| **FTS5** | `nodes_fts` virtual table for full-text symbol search (cgdb layer) |

### Concurrency & IPC

| Component | Purpose |
|-----------|---------|
| **fcntl** (POSIX) / **msvcrt** (Windows) | File-based read-write lock for transactional updates |
| **inotify** (Linux, via ctypes) | Real-time file monitoring in daemon — zero extra dependency |
| **polling** (fallback) | Cross-platform file monitoring when inotify unavailable |
| **Unix socket** | `/tmp/code2database-daemon-<project>.sock` for daemon control plane (status, force-refresh, pause, resume, wait-sync) |

### LLM / Agent Integration

| Component | Purpose |
|-----------|---------|
| **MCP (Model Context Protocol) stdio transport** | `serve` command exposes 53 tools (34 `code2database_*` + 19 `cgdb_*`) over JSON-RPC with Content-Length framing |
| **Tiered context packs** | micro (~200 tokens) → lite (~500) → standard (~1500) → full — minimizes LLM token cost |
| **Lazy module imports** | `_builder/__init__.py` delays module load until first access, reducing startup time |

### Foreign Function Interface (FFI) Detection

| Mechanism | Source Language | Target |
|-----------|----------------|--------|
| **ctypes** (CDLL/WinDLL), **cffi**, **pybind11** | Python | C |
| **cgo** (`import "C"`, `//go:cgo_import`) | Go | C |
| **extern "C"** blocks, `#[no_mangle]` exports | Rust | C |

Each FFI binding produces an `FFI_BRIDGE` edge with type marshalling and error mapping metadata.

### Build System Detectors

| Build System | Detected via | Extracts |
|--------------|--------------|----------|
| CMake | `CMakeLists.txt`, `*.cmake` | `-D` macros, targets, include dirs, build types |
| Make | `Makefile`, `*.mk` | Variables, targets |
| Meson | `meson.build` | Variables, dependencies |
| Autotools | `configure.ac`, `Makefile.am` | Autoconf macros |
| Kconfig | `Kconfig`, `Kconfig.*` | CONFIG_* symbols, defaults |
| Bazel | `BUILD`, `BUILD.bazel`, `WORKSPACE` | Targets, deps |
| MSBuild | `*.csproj`, `*.vcxproj` | Properties, items |
| spec | `*.spec` (RPM) | Macros, %define |

Used for `#ifdef` macro resolution and conditional compilation path feasibility.

### Testing & Evaluation

| Component | Purpose |
|-----------|---------|
| **pytest** | Test runner (55 test files, ~17K lines, covering scanner/builder/cgdb/daemon/MCP/concurrency/etc.) |
| **evals/evals_en.json** + **evals_zh.json** | End-to-end scenario evals (multi-language scan + query) |
| **BUG benchmark** | `bug_benchmark.py`: GraphInvestigator vs GrepInvestigator recall/precision/token efficiency |

## Key Algorithms

### Entry Point Scoring

Multi-factor scoring: `score = base_score * export_multiplier * name_multiplier * framework_multiplier`

- **base_score** = callee_count / (caller_count + 1) — functions that call many but are called by few
- **export_multiplier**: 2.0 for `API_entry`, 1.0 otherwise
- **name_multiplier**: 1.5 for entry patterns (main, run, start, handle_, on_), 0.3 for utility patterns (get_, set_, is_), 0.1 for test patterns (test_, mock_, stub_)
- **framework_multiplier**: from detected frameworks

Test functions are filtered out before scoring using patterns: `test_`, `testcase_`, `spec_`, `bench_`, `mock_`, `stub_`, `fake_`, `generateTest`, and `main` (unless in `/app/`).

### Vtable Dispatch Resolution

Three-phase resolution of indirect calls through struct field assignments:

1. **Direct field assignment** (e.g., `.submit_request = nvme_submit_request`): Creates `vtable_dispatch` edge from the calling function to the assigned function
2. **Callback bridging** (2-hop): If function A calls function B with a callback argument, and that callback is registered elsewhere, create edge from A to the callback target
3. **Module hint**: Registration variable names are parsed to derive `#vtable_module=<module>` conditions, enabling conditional path resolution
4. **Typed vtable dispatch** (cgdb L7, clang backend only): `ops_bindings` table links FieldDecl → FunctionDecl via `cgdb_find_ops_impls` MCP tool, returning `{ops_table_id, field_node_id, impl_function_id, signature_match}`

### Data Race Detection

```
For each shared resource (global variable or struct field):
  Collect all functions that access it (read or write)
  For each pair of accessing functions in different thread contexts:
    If no common mutex protection → flag as data_race (if write involved) or atomic_violation (both read)
    If different locks held → flag as deadlock_risk
```

Thread context is determined by `thread_model` + `thread_entry` attributes: functions with different thread entry points are in different contexts. Lock patterns are profile-driven (no hardcoded lock APIs) — projects populate `concurrency_patterns.lock_acquire_patterns` / `lock_release_patterns`.

### Conditional Compilation Tracking

During C scanner AST traversal, a `_ifdef_stack` tracks the nesting of `#ifdef`/`#ifndef`/`#if`/`#elif`/`#else` blocks:

1. Enter `preproc_ifdef`: push condition onto stack
2. Enter `preproc_elif`: replace stack top with parent-negated + elif condition
3. Enter `preproc_else`: replace stack top with negated parent condition
4. Function definitions inherit the current `_ifdef_stack` as `ifdef_conditions`
5. Call expressions inherit both function-level and inline `ifdef` conditions as `call_condition`

The clang backend additionally builds Z3 SMT-LIB forms (L3 `conditions` table) for sound path feasibility. Cross-language predicates are normalized in `_scanner/config_predicates_lang.py`:

- Go `//go:build linux && amd64` → `CONFIG_GO_TAG_LINUX AND CONFIG_GO_TAG_AMD64`
- Rust `#[cfg(target_os = "linux")]` → `CONFIG_CFG_TARGET_OS_LINUX`
- Python `sys.platform == "linux"` → `CONFIG_PY_PLATFORM_LINUX`
- Java `@Profile("prod")` → `CONFIG_JAVA_PROFILE_PROD`

### Commit Provenance Binding

At scan time, the scanner invokes `git rev-parse HEAD` (or `svn info`) to capture the current commit hash, then stamps every node/edge with `commit_meta = {source_commit, author, date, message}`. `blame-node` walks `git log -- <file>` (or `svn log`) to find the introducing commit. `node-history` aggregates commit evolution. Engineers verify with `git show <hash>`, not timestamps.

### Value Flow Propagation

For each function, build a per-parameter dataflow signature by walking the body: a `DATA_FLOW` edge from caller-parameter `p_i` to callee-return `r` is created when the body's return statement is `return p_i` (or `return expr(p_i)` for a tracked expression). `value-flow` follows the transitive closure to answer "where does this NULL come from?" without re-reading source.

### Lock-Held Event Stream

The scanner tokenizes each function body and emits a stream of `{kind: acquire|release, lock_var, char_pos, line, col}` events. The builder merges events into lock-held ranges. `lock-coverage` returns the ranges; `concurrency-analyze` consumes them to replace the previous "lock exists" heuristic with precise overlap checks.

### Invariant Extraction

Three sub-algorithms:
1. **Preconditions**: parse `if (!cond) return` patterns near function entry; emit `cond` as a precondition.
2. **Postconditions**: parse `return cond` or `assert(cond)` near exit; emit `cond` as a postcondition.
3. **State machine**: count assignments to a single state variable; threshold is `>= 1`. If the variable takes ≥2 distinct literal values, emit `{state_var, states, transitions}`.

Confidence is `EXTRACTED` for directly parsed patterns, `INFERRED` for indirect ones, `AMBIGUOUS` for body-shape guesses. `apply-invariants` rejects AMBIGUOUS and requires user confirmation for INFERRED.

### Transactional Updates

`transaction()` is a context manager that:
1. On enter: acquires an fcntl exclusive lock on `.code2database_tx/tx.lock`, snapshots the live DB to `.code2database_tx/snapshots/<txid>/`, opens `.code2database_tx/wal.jsonl` for append.
2. On exit (success): flushes WAL, fsyncs, atomically renames snapshot to live, releases lock.
3. On exit (failure): replays WAL backward to undo, restores snapshot, releases lock.
4. Crash recovery (`tx-replay-wal`): on next process start, detects unfinished WAL entries and replays them.

### Profile Health Scoring

Seven categories, each scored 0-100:
- `callback_patterns` (25 pts) — coverage of `static_patterns` vs. actual callback registrations in code
- `skip_names` (15 pts) — coverage of project-specific macros
- `vtable_types` (15 pts) — coverage of `struct_op_types`
- `api_prefixes` (10 pts) — `public_prefixes` accuracy
- `domain_keywords` (15 pts) — `domain_rules` accuracy
- `macro_definitions` (10 pts) — `registration_macros` coverage
- `profile_version` (10 pts) — match between profile version and current schema

Final score = weighted average. `profile-evolve` runs the scanner in observation mode, detects new callback registration functions, and emits EXTRACTED-confidence suggestions (auto-applied with `--apply`) or INFERRED-confidence (require user confirmation). `profile-bind-version` writes `profile_version_bound_commit` to bind the profile to the current HEAD; if HEAD changes, `profile-health` flags staleness.

### Doc-Code Alignment

`describe-node` exposes `doc_code_mismatches`: a list of mismatches between `semantic_desc` (from docs) and `body_text` (from code). Four mismatch types:
1. **Return-value mismatch** — doc says "returns X", code returns Y.
2. **Param-name mismatch** — doc names a parameter differently from code.
3. **Signature change** — doc signature differs from code signature.
4. **Stale-doc** — doc references functions/files that no longer exist.

`doc-mark-stale` sets `doc_stale=true` and `doc_stale_reason`. `knowledge-validate` runs the same check during knowledge validation.

### Daemon

```
inotify wait → debounce 500ms → batch window 1000ms → transaction() {
    re-scan changed files → patch graph → rebuild outputs
} → write .daemon_status.json → notify socket clients
```

Circuit breaker: if events/minute > 1000 (configurable via `daemon.circuit_breaker_threshold`), switch to bulk rebuild for that minute. Socket API at `/tmp/code2database-daemon-<project>.sock` accepts JSON commands: `status`, `force-refresh`, `pause`, `resume`, `wait-sync`. State persisted to `<graph_dir>/.daemon_status.json` for non-MCP clients. Adaptive batch window auto-grows under load (200ms floor, 5000ms ceiling).

### MCP Server

`serve` exposes 53 tools over stdio JSON-RPC with Content-Length framing:

- 34 `code2database_*` tools (load, search, describe, explore, trace, impact, key_paths, concurrency, data_lifecycle, domain, knowledge_query, memory_search, semantic_status, etc.)
- 19 `cgdb_*` tools (cgdb_search_symbols, cgdb_find_invokers, cgdb_find_invoked, cgdb_get_definition, cgdb_get_function_body, cgdb_get_struct_layout, cgdb_find_type_definition, cgdb_find_ops_impls, cgdb_find_cfg_paths, cgdb_find_data_flow, cgdb_find_aliases, cgdb_find_lock_held_calls, cgdb_check_race_condition, cgdb_find_configs_for, cgdb_find_nodes_under_config, cgdb_index_status, cgdb_time_travel_query, cgdb_list_versions)

The MCP server is accessible regardless of sub-skill activation; sub-skills are purely an LLM-context economy mechanism.

## Storage: JSON + SQLite Dual Backend

The primary storage is JSON files for human readability, but a SQLite backend (`sqlite_store.py`) is the default for builds:

- **Efficient querying**: SQL queries instead of loading entire JSON files
- **Reduced disk usage**: zlib compression for body_text, no redundant indexes
- **Scalability**: Handles 1.4M-node graphs with the `StreamingGraph` low-memory backend (~1.9GB RAM vs ~24GB with NetworkX)
- **cgdb layer**: 13 typed semantic tables coexist with legacy `functions`/`edges` for backward compat
- **Schema evolution**: `cgdb_migrations.run_migrations` ALTERs tables in-place, preserving data

The SQLite store uses WAL journal mode (concurrent readers + single writer), 64MB cache, 256MB mmap, and `field_access`/`global_access` SQL-native tables for O(log n) per-field queries (replacing O(n) Python traversal).

## Capability Summary

Code2Database's current capabilities, organized by category:

### Scanning & Extraction
- 7 languages: C, C++, Go, Python, Java, Rust, ASM (NASM x86_64, GNU as AT&T, ARM/AArch64, RISC-V, LoongArch, s390, PowerPC, SuperH, MIPS, IA-64)
- Dual backend: tree-sitter (default) + clang (optional, cgdb layer)
- Cross-language unified node IDs (SHA-256 truncated to 60 bits with language prefix)
- Incremental scanning via file fingerprints (`changes.py`)
- Profile-driven callback/lock/vtable patterns
- Conditional compilation tracking (#ifdef, //go:build, #[cfg], sys.platform, @Profile)

### Graph Construction
- Vtable dispatch resolution (3-phase + typed ops_bindings via clang)
- Callback bridging (2-hop)
- Domain classification + external code separation
- Cross-domain Leiden community detection
- Endpoint classification + entry point scoring
- Commit provenance (git/svn HEAD binding)
- Data race detection + lock-held regions
- Invariant extraction (preconditions/postconditions/loop_invariants/state_machine)
- FFI bridge detection (ctypes/cgo/extern "C")
- Value flow (DATA_FLOW edges) + cross-function data dependency (DATA_DEP edges)

### Query & Analysis
- 146 CLI subcommands (3 sub-skills: core 15, analysis 13, ops 14 Tier-1)
- 53 MCP tools (34 code2database_* + 19 cgdb_*)
- Cypher-subset query language (MATCH/WHERE/RETURN)
- Z3 SMT path feasibility (heuristic fallback)
- Tiered context packs (micro/lite/standard/full)
- Reverse trace, blast radius, impact analysis
- Memory ordering analysis (atomics, barriers, READ_ONCE/WRITE_ONCE)
- Happens-before relationship computation

### Operations & Reliability
- Transactional updates (WAL + snapshots + fcntl locks)
- Background daemon (inotify + circuit breaker + Unix socket API)
- Profile health (0-100 across 7 categories) + auto-evolution + HEAD binding
- Doc-code alignment (return/param/signature/stale-doc mismatches)
- LLM auto-enhancement with confidence-threshold auto-write + rollback
- Persistent Q&A memory with decay + scratch
- Knowledge extraction/query/validation
- Web UI (single-file HTML/SVG/JS)
- HTML/Obsidian export
- BUG benchmark (GraphInvestigator vs GrepInvestigator)
- Plugin system
- Embeddings (TF-IDF char n-gram semantic search)
- Git post-commit hook for auto quick-update

### Distribution
- Python skill with 3 sub-skills (`/Code2Database`, `/Code2Database-analysis`, `/Code2Database-ops`)
- One-click installer (`install.sh`) for Claude Code / Cursor / Codex / OpenCode / Gemini
- Per-language install (`C2D_LANGUAGES` env var or `setup.sh --languages`)
- 55 test files, ~17K lines (scanner, builder, cgdb, daemon, MCP, concurrency, FFI, etc.)
- End-to-end evals in English and Chinese (`evals/evals_en.json`, `evals/evals_zh.json`)
- Bilingual documentation (`docs/en/`, `docs/zh/`)
