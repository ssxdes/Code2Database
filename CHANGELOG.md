# Changelog

All notable changes to Code2Database will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] - 2026-08-25

**Major feature release**: multi-project aggregate build + cross-C2D live sync.

### Multi-project support (Phase 1-3)
- `build-multi` command: manifest-driven joint C2D from A→B→C interdependent projects. Forces project-name domain prefix (A_init vs B_init never collide). Aggregates include paths, merges compile_commands.json. Reuse mode imports from existing C2D via ATTACH.
- `c2d-add-foreign` / `c2d-sync-foreign` / `c2d-list-foreign` / `c2d-remove-foreign`: cross-C2D reference tracking via foreign_refs + watched_c2ds tables. SQLite ATTACH for read-only cross-db queries.
- `composite-query`: cross-C2D JOIN (CALLERS_OF / CALLEES_OF across local + foreign dbs).
- `c2d-check-compat`: verify B's foreign_refs against new A version (broken/signature-changed/ok).
- `coverage-cross-c2d`: test coverage analysis across C2Ds (which A functions are called by test_A).
- `export-mermaid --multi`: project-level dependency graph visualization.
- `c2d-add-foreign-stub`: vendor SDK signature-only stub C2D (glibc/kernel/DPDK).
- `ffi-auto-link`: auto-link FFI bindings to watched foreign C2Ds.
- `scan-rpc`: scan source for HTTP/gRPC calls, create rpc_endpoint stub nodes + edges.
- `import-foreign-knowledge`: copy foreign C2D's knowledge/*.md into local with project prefix.

### Deep audit fixes (P0)
- `describe-node` transparent foreign_ref fallback (F1): returns foreign callee metadata via ATTACH.
- `kb-query` ATTACH foreign dbs (F2): searches A's knowledge when B's local KB is thin.
- `kb-cluster` uses Jaccard token-set similarity (C1 fix): BM25 was relevance ranking, not similarity.
- Daemon watches foreign db mtimes (F9): auto-syncs foreign_refs when A updates.
- `c2d-add-foreign` signature disambiguation (C2): handles C++ overloads by matching signature.
- MCP tools: 3 new (code2database_foreign_refs, code2database_sync_foreign, code2database_composite_query). Total 53.
- Security: kb-global-share path traversal check (S1), kb-global-import JSON nesting guard (S2), c2d-add-foreign SQLite validity check (S4).
- Daemon foreign sync throttle (P3): min 60s between runs.

### Stats
- 4 new modules: build_multi.py, c2d_foreign.py, c2d_phase2.py, c2d_phase3.py.
- 12 new CLI commands. Total 196.
- 3 new MCP tools. Total 53.
- SCHEMA v13: foreign_refs + watched_c2ds tables.

## [1.2.0] - 2026-08-25

**Major feature release**: unified knowledge base (kb-*) with FTS5+BM25 across
memory + knowledge + global stores. 13 new kb-* CLI commands, 1 new MCP tool,
5 new modules, 3 new SQLite tables, 4 schema migrations.

### Phase 0 — Urgent BUG fixes
- **Storage path split**: `cgdb_merge` and `cgdb_suggest` read from
  `.code2database_memory` and `.code2database_knowledge` (always empty);
  `save-memory` and `apply-knowledge` write to `memory/` and `knowledge/`.
  Unified to canonical paths; rewrote 4 functions in `cgdb_merge.py` with
  root+leaf+experience loading and non-dict sanitize.
- **LazySQLiteGraph incompatibility**: `cmd_update` had early detection
  (prior commit); this release covers the remaining 5 mutation sites —
  `cmd_merge`, `cmd_sync`, `merge_change_graph`, `cmd_apply_semantics`,
  `_mark_file_stale`. New shared helper `_ensure_mutable_graph` in utils.py.
- **ARG_MAX**: `graph_build.py:6525` joined thousands of CONFIG_* macros
  into a single `--macros` CLI arg (kernel exceeds ~128KB ARG_MAX). Added
  `--macros-from <file>` to scanner + `_parse_macros_file` helper;
  graph_build uses tempfile when joined length > 8KB with finally cleanup.
- **Daemon memory invalidate**: `daemon.py` had zero memory references;
  source updates left memory `node_ids` as dangling pointers. Added
  `_invalidate_stale_memory_after_sync()` called from `_sync_incremental`
  and `_sync_bulk`.

### Phase 1 — FTS5 schema + kb-rebuild-index (SCHEMA v9)
- New table `kb_paragraphs` + `kb_paragraphs_fts` (porter + unicode61
  tokenizer) + AI/AD/AU triggers + 3 indexes.
- New module `scripts/_builder/kb_index.py`: `rebuild_kb_index()` walks
  `memory/{root,leaf,experience}/*.json` + `knowledge/*.md` (paragraph-split
  via `##` headings) and bulk-inserts. `query_kb()` returns ranked hits
  with `see_also` and `access_count` bump.
- New CLI `kb-rebuild-index`.

### Phase 2 — memory/knowledge search upgrade
- `cmd_search_memory`, `_tool_memory_search`, `KnowledgeManager.query_knowledge`,
  `_tool_knowledge_query` all try FTS5+BM25 first, fall back to legacy
  Jaccard / substring search when no db.

### Phase 3 — unified query interface
- New CLI `kb-query` with `--kinds` / `--min-weight` / `--max-tokens` /
  `--semantic` / `--global` flags.
- New MCP tool `code2database_kb_query` (TOOLS dict 49 -> 50).
- `cmd_query` (Cypher) injects top-3 kb hits as `_hints` alongside rows,
  realizing the SKILL.md `memory -> knowledge -> graph -> source` chain.
- `cmd_describe_node` returns `memory_refs` + `knowledge_refs`.
- `_build_context_pack` merges `.memory_pack_lite` + `.knowledge_pack_lite`
  into the main context_pack as `memory_summary` + `knowledge_summary`.

### Phase 4 — clustering (SCHEMA v10)
- Added `scope_id` / `canonical_id` / `principle_ref` columns to kb_paragraphs.
- New module `kb_cluster.py`: union-find on FTS5 BM25 > threshold, picks
  canonical (highest weight x confidence), links `memory_qa` -> principle.
- New CLI `kb-cluster`. `query_kb` returns `see_also` from same cluster.

### Phase 5 — embedding schema (SCHEMA v11)
- Added `embedding BLOB` column. `kb-query --semantic` flag interface in
  place; degrades to FTS5 when sentence-transformers unavailable.

### Phase 6 — kb_items unified table (SCHEMA v12)
- New fact-level table `kb_items` + `kb_items_fts` with `versions_json`,
  `decay_class`, `provenance_commit`, `provenance_operator`.
- New CLI `kb-migrate` copies `kb_paragraphs` -> `kb_items` with kind-based
  `decay_class` assignment.

### Phase 8 — cross-project global KB
- New module `kb_global.py`: `~/.code2database_global_kb/global.db` with
  `kb_global` + `kb_global_fts`. `global_add` / `search` / `share` / `import`.
- 4 new CLI: `kb-global-add`, `kb-global-search`, `kb-global-share`,
  `kb-global-import`. `kb-query --global` falls back when project KB empty.

### Phase 9 — feedback loop
- New table `kb_query_log` records every `query_kb` call (matched / count /
  top_score / timestamp).
- New CLI `kb-known-unknowns` aggregates unmatched queries (occurrences >= N).

### Phase 10 — knowledge audit
- New module `kb_audit.py`: `audit_kb()` reports counts by kind, stale items
  (90d untouched), low-confidence (<0.5), high-citation (top access_count),
  most-linked principles, and optional 'what we know about X'.
- `write_audit_log_entry()` reuses the existing `audit_log` table.
- New CLI `kb-audit`.

### Phase 11 — conflict & rollback
- New module `kb_conflict.py`: `detect_conflicts()` pairwise-scan within
  clusters for 14 contradiction word pairs (yes/no, must/must not,
  always/never, safe/unsafe, ...). `rollback_kb_item()` restores from
  `versions_json` (saves current as new version first).
  `forget_kb_paragraph()` immediately deletes with `audit_log` entry.
- 3 new CLI: `kb-conflict`, `kb-rollback`, `kb-forget`.

### SKILL.md aliases
- `save` -> `save-memory`, `recall` -> `search-memory`, `know` ->
  `knowledge-query` (so SKILL.md Quick Reference commands work as documented).

### Doc sync
- 16 `.md` / `.json` files updated for new counts (53 MCP tools /
  34 code2database_* / 200 CLI commands / 27 core).
- `docs/en/references/memory_knowledge.md` and `docs/zh/references/memory_knowledge.md`
  rewritten to match actual code schema (removed fictional `mem_xxx` /
  `topic` / `fact` / `source` / `confidence` / `related_functions` /
  `graph_version` fields; removed `--threshold-days` fictional param;
  replaced with real entry schema + Markdown file schema).
- `docs/{en,zh}/OVERVIEW.md` directory tree updated with 5 new `kb_*.py`
  modules.
- `docs/{en,zh}/SKILL_ops.md` Quick Reference updated with 8 new kb-* ops
  commands; Tier-1 count bumped 14 -> 22.
- `docs/{en,zh}/references/data_model.md` adds kb_paragraphs / kb_items /
  kb_query_log table descriptions.
- `skill.json` tier_1_commands: 20 -> 24 (added `kb-query`,
  `kb-rebuild-index`, `kb-audit`, `kb-forget`).
- `skill_ops.json` tier_1_commands: 5 -> 8 (added `kb-rebuild-index`,
  `kb-audit`, `kb-forget`).
- `skill_analysis.json` tier_1_commands: 5 -> 6 (added `kb-query`).

### Stats
- 5 new modules: `kb_index.py`, `kb_cluster.py`, `kb_global.py`,
  `kb_audit.py`, `kb_conflict.py`.
- 13 new kb-* CLI commands (CLI total 171 -> 184).
- 1 new MCP tool `code2database_kb_query` (MCP total 49 -> 50).
- 3 new tables (`kb_paragraphs` + FTS5, `kb_items` + FTS5, `kb_query_log`).
- 4 schema migrations (v9-v12).

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
- **122 CLI commands** organized into 3 sub-skills: `/Code2Database` (core, 15 Tier-1 high-weight commands), `/Code2Database-analysis` (deep semantic analysis, 13 Tier-1 + 19 `cgdb_*` MCP tools), `/Code2Database-ops` (graph editing + ops, 14 Tier-1).
- **53 MCP tools** (34 `code2database_*` + 19 `cgdb_*`) exposed over stdio transport via `serve` command.
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

- **3-sub-skill split**: `Code2Database` (core, always loaded), `Code2Database-analysis` (on-demand deep analysis), `Code2Database-ops` (on-demand ops). Each sub-skill has its own `SKILL.md` exposing only the commands relevant to its layer. The CLI (`scripts/code2database_builder.py`) is shared — all 200 commands are accessible regardless of which sub-skill is active.
- **Bilingual documentation**: English (`docs/en/`) and Chinese (`docs/zh/`).
- **One-click installer** (`install.sh`) with Claude Code and Codex CLI support.
- **Partial language install**: `scripts/setup.sh --languages c,go` (or `C2D_LANGUAGES` env var) — engineers focused on a single language can install only the tree-sitter grammars they need.
- **MCP server registry** (`server.json`) for discoverability.
- **`CLAUDE.md`** for Claude Code integration; **`AGENTS.md`** for Codex / agent integration.

### Test coverage

- 1066+ tests covering scanner, builder, query, transactions, daemon, FFI, invariants, profile health, doc-code alignment, cgdb layer (CFG, data flow, ops bindings, sync primitives, happens-before, versions), MCP tools, e2e pipelines.
