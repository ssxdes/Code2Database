# Changelog

All notable changes to Code2Database will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.2] - 2026-08-22

Patch release refactoring the Web UI to **cytoscape.js 3.28.1** for dense-graph UX.

### Web UI refactor (WEBUI-REFACTOR)
- cytoscape.js 3.28.1 inlined (CDN load; offline mode: npm pack cytoscape@3.28.1)
- Three layout algorithms (flow/rings/force)
- Focus+context fading
- Edge bundling (curve-style: bezier)
- Community compound nodes
- LOD label hiding
- Edge `call_condition` labels
- Selector-based edge filter
- Incremental sync (syncCyFromModel)
- Preserved features (17 endpoints, cumulative model, nav, breadcrumb, etc.)

### Documentation updates
- New `docs/{en,zh}/references/web_ui.md` (17 endpoints + features + shortcuts)
- README.md, docs/zh/README.md, AGENTS.md: HTML/SVG/JS → HTML/cytoscape.js/JS
- Badge fix: 7+ASM → 6+ASM (C/C++ shared scanner)

### Tests
- 44 Web UI tests pass (17 HTTP endpoint + 23 GraphCache + 4 import)

## [1.1.0] - 2026-08-18

This release closes major gaps identified in `report/Code2Database-最终差距分析与优化报告.md`
vs the design report `report/C代码数据库化方案-分析与执行报告.md` v4.0. It
adds the design-report appendix B/C (L1/L3/L4/多库/跨语言) tables, the source
renderer, the transactional write-back loop, 28 design-report MCP tools,
an IR-adapter framework, an L1 token-ingest module, expanded evals, and 4
new test files.

### Schema (cgdb v4 — design-report appendix C)

- **Schema version bumped 3 → 4**. New tables added side-by-side with
  existing cgdb tables (idempotent migration; v3→v4 ALTERs existing tables
  for additional columns).
- **Report-L1 无损重建层 (10 tables)**: `tokens` (with `preceding_whitespace`,
  `byte_offset`, `spelling`), `macros`, `macro_invocations`, `pp_branches`,
  `pp_directives`, `pragmas`, `attributes`, `literals`, `string_literals`
  (with `security_flags`), `comments_freeform` + `comments_fts` (FTS5).
  Plus `source_files_meta` (encoding/line_ending/has_bom/disk_sha256/
  rendered_sha256).
- **Report-L3 IR 层 (7 tables)**: `ir_functions`, `ssa_values`,
  `mem_accesses`, `points_to`, `indirect_calls`, `data_deps`, `path_states`.
  Existing `alias_sets` table extended with `function_id`/`ssa_value_id`/
  `alias_ssa_value_id`/`analysis` columns.
- **Report-L4 派生层 (6 tables + 1 extended)**: `call_graph_reachability`,
  `module_deps`, `function_embeddings` (BLOB), `precise_write_sets`,
  `arch_metrics`, `history_snapshots`, `alignment_errors`. Existing
  `graph_versions` extended with `sha256`/`snapshot_at`.
- **Report-多库 routing (2 tables)**: `db_routing`, `precompute_tasks`.
- **Report-跨语言 bridge (6 tables)**: `cross_lang_bindings`, `type_mappings`,
  `ffi_call_sites`, `language_adapters` (with 7 backfilled rows for
  c/cpp/rust/go/java/python/asm), `runtime_observations`, `dependencies`.
- **Naming clarification**: legacy cgdb layers renamed (in docs) as
  CGDB-L0 through CGDB-L11 to disambiguate from design-report L1~L4.
  No code renames (backward compat preserved).

### Source renderer + sha256 consistency (P0-6/7)

- New module `scripts/_builder/source_renderer.py`:
  - `SourceRenderer.render(file_id)` — DB tokens → character-level source bytes
  - `verify_consistency(file_id)` — render DB → sha256 vs disk sha256;
    mismatch recorded in `alignment_errors` (layer='L1',
    error_kind='sha256_mismatch')
  - `verify_all_files()` — bulk consistency check after L1 ingest
  - `update_disk_sha256(file_id)` — refresh after write-back

### Transactional write-back loop (P0-8)

- New module `scripts/_builder/writeback_pipeline.py`:
  - `WritebackPipeline.begin(file_id)` — start a write-back transaction
  - `WritebackPipeline.commit(tx_id, run_compile, run_lint, run_clang_format, git_commit, ...)`
    runs all gates in order: render → (clang-format) → compile → lint →
    sha256 verify → write to disk (atomic .tmp + rename) → git commit
  - On any gate failure: rollback entire transaction (DB snapshot restore,
    .tmp file deleted)
  - Module-level entry points `commit_db_transaction` and
    `rollback_db_transaction` for MCP tool binding

### 28 new design-report MCP tools (P0-9)

- New module `scripts/_builder/mcp_report_tools.py` with 28 MCP tools
  matching design-report appendix B signatures:
  - **L1 (8)**: `render_source`, `verify_consistency`, `edit_token`,
    `insert_token`, `delete_token`, `find_macros`, `get_pp_branches`,
    `get_string_literals`
  - **L2 (8)**: `find_symbol`, `callers_of`, `callees_of`, `who_writes`,
    `who_reads`, `get_context`, `impact_analysis`, `get_module_view`
  - **L3 (7)**: `indirect_targets`, `alias_set`, `trace_data_flow`,
    `cfg_of`, `path_sensitive_states`, `precise_write_set`, `dead_code_in`
  - **写回 (2)**: `commit_db_transaction`, `rollback_db_transaction`
  - **高级编辑 (3)**: `insert_node_after`, `delete_node`, `add_function`
- `mcp_server.py` TOOLS dict now has 77 tools (49 existing + 28 new).

### IR Adapter framework (P1-3)

- New module `scripts/_builder/ir_adapters.py` with abstract base class
  `IRAdapter` and concrete adapters for 18 languages:
  - Tier A (LLVM): `LLVMIRAdapter` for C/C++/Rust/Swift/Zig/CUDA
  - Tier B: `JimpleIRAdapter` for Java/Kotlin/Scala,
    `MSILIRAdapter` for C#/F#/VB.NET, `GoSSAAdapter` for Go
  - Tier C: `PythonBytecodeAdapter` for Python, `TSCompilerAdapter` for
    TypeScript/JavaScript, `_NoIRAdapter` for Lua/Shell
- `get_ir_adapter(language)` factory + `list_supported_languages()` registry.
- Each adapter gracefully degrades when external toolchain not installed.

### L1 ingest module (P1-1/2)

- New module `scripts/_builder/l1_ingest.py`:
  - `ingest_l1(conn, file_path, file_id, ...)` populates L1 tables
    (tokens/macros/macro_invocations/pp_branches/pp_directives/pragmas/
    attributes/literals/string_literals/comments_freeform) using
    libclang's Lexer (`tu.cursor.get_tokens()`) + PPCallbacks simulation
    (cursor walk for MACRO_DEFINITION/MACRO_INSTANTIATION/INCLUDE_DIRECTIVE).
  - Computes `preceding_whitespace` from byte gap between consecutive tokens.
  - Auto-detects BOM / line-ending (CRLF/LF/CR).
  - Computes `security_flags` for string literals (format_string /
    sql_injection / shell_injection / path_traversal).
  - Builds `pp_branches` tree via regex on source lines.
  - Falls back to disk-sha256-only ingest when libclang unavailable.

### Evals expanded to 43 tasks (P0-10)

- `evals/evals_en.json` and `evals/evals_zh.json` expanded from 3 → 43
  tasks across 7 categories:
  - multilang_baseline (3): existing C/Go/Python basic call chains
  - bug_localization (10): NULL deref, double-free, deadlock, off-by-one, ...
  - feature_implementation (10): new functions, callbacks, #ifdef paths, FFI
  - concurrency_risk (5): data races, deadlocks, RCU, atomics
  - data_flow_tracing (5): param flow, NULL sources, global writers
  - macro_related (5): macro definitions, expansions, dispatch, token-paste
  - refactoring (5): extract, rename, inline, move, switch→vtable

### 4 new test files (P0-11)

- `tests/test_transactions.py` — end-to-end transactional write-back tests
  (render, verify_consistency, commit, rollback, edit_token+writeback,
  alignment_errors insert/query)
- `tests/test_web_ui.py` — Web UI HTTP endpoint tests (server start,
  /health, /, module import)
- `tests/test_bug_benchmark.py` — BUG benchmark module tests
  (GraphInvestigator, GrepInvestigator, BenchmarkResult schema)
- `tests/test_profile_health.py` — Profile health scoring + evolution tests
  (score range 0-100, 7 documented categories, EvolutionSuggestion)

### Documentation updates (P1-10/11)

- `docs/en/OVERVIEW.md` and `docs/zh/OVERVIEW.md` updated with naming
  clarification note distinguishing cgdb layers (CGDB-L0~L11) from
  design-report layers (Report-L1~L4 + 多库 + 跨语言).
- New analysis report `report/Code2Database-最终差距分析与优化报告.md`
  documenting 13 core gaps + 49 optimization items with priority ratings.

## [1.0.0] - 2026-08-11

First public release. Code2Database scans C/C++/Go/Python/Java/Rust/ASM codebases and builds a persistent, queryable code graph database (cgdb) capturing invocation ordering, conditions, conditional compilation paths, concurrency analysis, data race detection, field-level access tracking, and the cgdb semantic layers (AST nodes, types, config predicates, CFG, data flow, ops_bindings, sync primitives, happens-before, provenance, time-travel versions).

### Core capabilities

- **Dual extraction backend**: `auto` (default — uses clang when libclang is installed, falls back to tree-sitter), `clang` (force clang, enables cgdb semantic layer; libclang 17+), `tree-sitter` (force tree-sitter, no libclang dep). Selected via `--extraction-backend` flag at scan time. **libclang is recommended, NOT required** — tree-sitter-only mode is fully functional for every supported language.
- **cgdb semantic tables** populated when clang backend is enabled: L1 AST nodes, L2 types, L3.5 config predicates (`#ifdef CONFIG_*`), L4 CFG (basic blocks + edges), L5 data flow (def-use chains), L6 alias (stub), L7 ops_bindings (typed vtable dispatch via FieldDecl → FunctionDecl) + invoke_sites, L8 sync_primitives + happens_before, L10 provenance + time-travel versions.
- **122 CLI commands** organized into 3 sub-skills: `/Code2Database` (core, 15 Tier-1 high-weight commands), `/Code2Database-analysis` (deep semantic analysis, 13 Tier-1 + 18 `cgdb_*` MCP tools), `/Code2Database-ops` (graph editing + ops, 14 Tier-1).
- **49 MCP tools** (30 `code2database_*` + 19 `cgdb_*`) exposed over stdio transport via `serve` command.
- **Multi-language scanning** for C, C++, Go, Python, Java, Rust, ASM (regex-based for NASM x86_64 / kernel GNU as / ARM bl/blr).

### Analysis & Reasoning

- **Commit-based provenance**: every node/edge carries `commit_meta` with `source_commit` (git/svn hash). Engineers verify with `git show <hash>`, not timestamps. Commands: `describe-commit`, `node-history`, `graph-provenance`, `blame-node`, `find-commits`.
- **Cypher-subset query language**: `query` command with `MATCH`/`WHERE`/`RETURN` syntax.
- **Value flow & DATA_FLOW edges**: `value-flow` command traces parameter→return-value propagation across functions.
- **Lock-held region analysis**: `lock-coverage` command with event-stream + character positions.
- **Z3 SMT path feasibility**: `path-feasible` command auto-solves path feasibility with Z3; heuristic fallback when Z3 unavailable.
- **Cross-function data dependency**: `data-dep` command + DATA_DEP edge type.
- **Invariant extraction**: `extract-invariants`, `find-invariants`, `apply-invariants` (preconditions, postconditions, loop_invariants, state_machine).
- **LLM auto-semantic enhancement**: `auto-enhance`, `batch-confirm`, `rollback`, `fill-request` (confidence-threshold auto-write with non-destructive undo).

### Reliability & Operations

- **Transactional updates**: `tx-begin`/`tx-commit`/`tx-rollback`/`tx-status`/`tx-snapshot`/`tx-restore`/`tx-list-snapshots`/`tx-replay-wal`. WAL + snapshots + fcntl file-based read-write lock. Crash recovery via `recover_unfinished_wal`.
- **Cross-language FFI**: `ffi-detect`, `ffi-list`, `ffi-trace`, `ffi-types` (Python ctypes / Go cgo / Rust `extern "C"`).
- **Interactive Web UI**: `web-ui` launches a local HTTP server with single-file HTML/SVG/JS (pan/zoom, click-to-focus, path highlighting, community LOD, FFI coloring, search).
- **BUG benchmark**: `bug-benchmark` runs GraphInvestigator (code graph queries) vs GrepInvestigator (rg/grep + file reads). Measures recall, precision, avg_tool_calls, avg_tokens, avg_time.
- **Background daemon**: `daemon-start`/`stop`/`status`/`force-refresh`/`pause`/`resume`/`wait-sync`/`logs`/`reload`/`list-projects`. inotify-based file monitoring with polling fallback, debounce (500ms), batch window (1000ms), circuit breaker (>1000 events/min triggers bulk rebuild), transactional sync, auto output file rebuild, Unix socket API at `/tmp/code2database-daemon-<project>.sock`.

### Profile & Documentation

- **Profile health + auto-evolution**: `profile-health`, `profile-evolve`, `profile-bind-version`. 0-100 score across 7 categories (callback_patterns, skip_names, vtable_types, api_prefixes, domain_keywords, macro_definitions, profile_version). Auto-evolution detects new callback register functions; EXTRACTED-confidence suggestions auto-applied, INFERRED require review. Binds profile to git/svn HEAD commit so stale profiles are detectable.
- **Doc-code dual-source truth alignment**: `doc-code-check`, `doc-mark-stale`, `doc-alignment-report`, `doc-signature-diff`. Detects return-value, param-name, signature-change, and stale-doc mismatches between `semantic_desc` (from docs) and `body_text` (from code). `describe-node` surfaces `doc_code_mismatches`; `knowledge-validate` includes doc-code alignment.

### Time-travel & Incremental Sync

- **Time-travel version queries**: `cgdb_time_travel_query` and `cgdb_list_versions` enable querying the graph at past versions (`first_seen_version` / `last_seen_version` per node/edge).
- **Incremental sync with content hash**: daemon uses SHA-256 content hash for change detection; `compute_affected_tus` walks transitive `#include` closure to expand changed files.
- **Enclosing symbol tracking**: `enclosing_symbol_id` column on `cgdb_nodes` and `cgdb_edges` for nested-scope queries.

### Distribution

- **3-sub-skill split**: `Code2Database` (core, always loaded), `Code2Database-analysis` (on-demand deep analysis), `Code2Database-ops` (on-demand ops). Each sub-skill has its own `SKILL.md` exposing only the commands relevant to its layer. The CLI (`scripts/code2database_builder.py`) is shared — all 171 commands are accessible regardless of which sub-skill is active.
- **Bilingual documentation**: English (`docs/en/`) and Chinese (`docs/zh/`).
- **One-click installer** (`install.sh`) with Claude Code and Codex CLI support.
- **Partial language install**: `scripts/setup.sh --languages c,go` (or `C2D_LANGUAGES` env var) — engineers focused on a single language can install only the tree-sitter grammars they need.
- **MCP server registry** (`server.json`) for discoverability.
- **`CLAUDE.md`** for Claude Code integration; **`AGENTS.md`** for Codex / agent integration.

### Test coverage

- 1066+ tests covering scanner, builder, query, transactions, daemon, FFI, invariants, profile health, doc-code alignment, cgdb layer (CFG, data flow, ops bindings, sync primitives, happens-before, versions), MCP tools, e2e pipelines.
