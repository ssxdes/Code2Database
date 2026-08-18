# Changelog

All notable changes to Code2Database will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-08-11

First public release. Code2Database scans C/C++/Go/Python/Java/Rust/ASM codebases and builds a persistent, queryable code graph database (cgdb) capturing invocation ordering, conditions, conditional compilation paths, concurrency analysis, data race detection, field-level access tracking, and 13 semantic layers (AST nodes, types, config predicates, CFG, data flow, ops_bindings, sync primitives, happens-before, provenance, time-travel versions).

### Core capabilities

- **Dual extraction backend**: `auto` (default — uses clang when libclang is installed, falls back to tree-sitter), `clang` (force clang, enables cgdb semantic layer; libclang 17+), `tree-sitter` (force tree-sitter, no libclang dep). Selected via `--extraction-backend` flag at scan time. **libclang is recommended, NOT required** — tree-sitter-only mode is fully functional for every supported language.
- **13 cgdb semantic tables** populated when clang backend is enabled: L1 AST nodes, L2 types, L3.5 config predicates (`#ifdef CONFIG_*`), L4 CFG (basic blocks + edges), L5 data flow (def-use chains), L6 alias (stub), L7 ops_bindings (typed vtable dispatch via FieldDecl → FunctionDecl) + invoke_sites, L8 sync_primitives + happens_before, L10 provenance + time-travel versions.
- **122 CLI commands** organized into 3 sub-skills: `/Code2Database` (core, 15 Tier-1 high-weight commands), `/Code2Database-analysis` (deep semantic analysis, 13 Tier-1 + 18 `cgdb_*` MCP tools), `/Code2Database-ops` (graph editing + ops, 14 Tier-1).
- **48 MCP tools** (30 `code2database_*` + 18 `cgdb_*`) exposed over stdio transport via `serve` command.
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

- **3-sub-skill split**: `Code2Database` (core, always loaded), `Code2Database-analysis` (on-demand deep analysis), `Code2Database-ops` (on-demand ops). Each sub-skill has its own `SKILL.md` exposing only the commands relevant to its layer. The CLI (`scripts/code2database_builder.py`) is shared — all 122 commands are accessible regardless of which sub-skill is active.
- **Bilingual documentation**: English (`docs/en/`) and Chinese (`docs/zh/`).
- **One-click installer** (`install.sh`) with Claude Code and Codex CLI support.
- **Partial language install**: `scripts/setup.sh --languages c,go` (or `C2D_LANGUAGES` env var) — engineers focused on a single language can install only the tree-sitter grammars they need.
- **MCP server registry** (`server.json`) for discoverability.
- **`CLAUDE.md`** for Claude Code integration; **`AGENTS.md`** for Codex / agent integration.

### Test coverage

- 1066+ tests covering scanner, builder, query, transactions, daemon, FFI, invariants, profile health, doc-code alignment, cgdb layer (CFG, data flow, ops bindings, sync primitives, happens-before, versions), MCP tools, e2e pipelines.
