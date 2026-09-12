# AGENTS.md

This file provides instructions for AI coding agents (Codex, Copilot, etc.) working with Code2Database.

> **Boundary**: This file is for developers modifying the Code2Database skill itself. For usage instructions, follow `SKILL.md`. Do NOT load `OVERVIEW.md` or `scripts/` into agent context — they are implementation details.
>
> **Installed vs. source repo**: this file ships in the installed skill, but some paths it references (`docs/`, `evals/`, `deploy/`, `tests/`, `OVERVIEW.md`, `docs/en/`, `docs/zh/`) exist only in the source repo, not in the install directory. When working in the installed skill, these paths will not resolve — clone the source repo for full developer context.

## Scope

| | |
|--|--|
| **Reads** | `scripts/`, `docs/`, `config/`, `evals/`, and target source directories as needed |
| **Writes** | Only paths required for the requested change; keep diffs minimal |
| **Executes** | `python3` for scanner/builder CLI, `pip` for dependencies, `bash` for setup |
| **Off-limits** | `.code2database_*` output files (use query commands instead), `scripts/config/profiles/` (internal templates), `.code2database_wal.log` / `.code2database_snapshots/` (transaction internals — use `tx-restore` instead) |

## Project Overview

Code2Database is a multi-language code graph generator for C/C++/Go/Python/Java/Rust/ASM codebases. It produces directed invocation graphs with call ordering, conditional path annotation (`#ifdef`, if/while/for/switch/ternary with &&/|| compounds), cross-file resolution (suffix index, import_map, same_domain, unique_name), concurrency analysis, data race detection, field-level access tracking, GCC/MSVC asm support, and static fn-ptr dispatch resolution. Graphs are consumed via tiered LLM context packs (micro/lite/standard/full), a Cypher-subset query language, and an MCP server exposing 83 tools (36 `code2database_*` + 19 `cgdb_*` + 28 design-report).

- **Dual extraction backend** — `auto` (default: clang when libclang is installed, tree-sitter fallback), `clang` (forces clang, populates the cgdb layer; libclang 17+), `tree-sitter` (no libclang dep). Selected via `--extraction-backend` at scan time. libclang is recommended, NOT required — tree-sitter-only mode is fully functional.
- **cgdb layer** (clang backend only) — typed semantic tables alongside the legacy `functions`/`edges`: AST nodes, types, config predicates, CFG, data flow, alias (stub), ops_bindings (typed vtable dispatch), sync_primitives + happens_before, provenance + time-travel versions. Queried via 19 `cgdb_*` MCP tools or the `cgdb-*` CLI family.
- **Dual knowledge/memory stores** — knowledge = lean per-project brief (`knowledge/brief.json`, size-budgeted); memory = shared accumulating SQLite store (`memory/memory.db`, hierarchical categories, FTS5 BM25 retrieval, split/merge/move/compact governance; compact merges near-duplicate roots after every build). `session-init` is the one-shot entry (brief + memory digest + graph state incl. freshness + known-unknowns); `save-memory --correct` is the correct-first save; `brief-suggest` mines graduation candidates (no auto-write).

Full capability catalog with per-command detail: `docs/en/references/usage_reference.md` (intent index → pipeline walkthrough → complete 260-command reference). Do not duplicate it here.

## Skill Structure (3 sub-skills)

The skill is split into 3 sub-skills to keep LLM context lean. The CLI (`scripts/code2database_builder.py`, 252 subcommands + 8 scanner subcommands) is shared — all commands are accessible regardless of sub-skill activation.

| Sub-skill | Trigger | Purpose |
|-----------|---------|---------|
| `Code2Database` (core) | `/Code2Database` | Build + browse — always loaded. 27 Tier-1 commands + the `c2d` umbrella. |
| `Code2Database-analysis` | `/Code2Database-analysis` | Deep semantic analysis (concurrency, data flow, invariants, FFI, provenance, path feasibility, cgdb tables). 13 Tier-1 commands + 19 `cgdb_*` MCP tools. |
| `Code2Database-ops` | `/Code2Database-ops` | Graph editing + ops (transactions, daemon, profile/doc-code, exports, plugins, memory, embeddings). 23 Tier-1 commands. |

## Pipeline Architecture

```
c2d (umbrella: setup → session → ask → capture, + freshen/report; `ask` routes a question to a read-only recipe)
make (one-click: env-check → scan → build → derived artifacts → exports; c2d setup delegates here)
Profile → Scan (AST extraction) → Build (graph construction) → Query
                                  ↓
                            Daemon auto-refresh loop
                                  ↓
                            Transactional Sync
                                  ↓
                            Output file rebuild + freshness marker
```

- **Scan** produces immutable facts (`extraction.json`)
- **Build** performs inference (vtable dispatch, callback bridging, community detection, invariant extraction, FFI detection, doc-code alignment)
- **Query** follows global-to-local: micro pack → lite pack → explore-flow → describe-node
- **Daemon** (optional) monitors source files and auto-updates the graph in transactions

For command details, see `SKILL.md` Quick Reference and `references/usage_reference.md`.

## Constraints

Behavior contracts that a change must not break:

- **Never directly read** output `.json`/`.md` files — always use query commands
- **Start with micro/lite context packs** before reading detailed data
- **Only 7 labels** allowed: API_entry, thread_processor, callback_func, constructor, destructor, out_end, unknown_end
- **Edge confidence** must always be annotated: EXTRACTED / INFERRED / AMBIGUOUS; never treat AMBIGUOUS as fact
- **DB writes need user confirmation** (update/patch/apply/auto-enhance/doc-mark-stale/tx-commit family); EXTRACTED+evidence may bypass for auto-enhance/apply-invariants/profile-evolve, INFERRED never
- **Transactional writes**: wrap multi-step DB changes in `tx-begin`/`tx-commit`; `patch-from-diff`/`patch-from-git` already wrap by default (`--no-transaction` to bypass)
- **Do not pre-load** `scripts/config/profiles/` or `docs/*/references/` into context

Full usage constraints (daemon freshness, doc-code alignment, invariants confidence, FFI, profile evolution): `docs/en/SKILL.md`.

## Testing

```bash
python3 -m pytest tests/ -v
```

Capability modules have dedicated unit tests in `tests/` (137 files) covering invariants, auto-enhance thresholds, transactions, FFI, Web UI (HTTP + JS), daemon, LSP, hybrid search, embeddings, SARIF, AST pattern matching, taint analysis, code intelligence, concurrency, data dependencies, commit provenance, updates, and profile generation.

**Test suite**: 2810+ tests across 137 files. Run with `python3 -m pytest tests/ -v`. (test_daemon_multithread has one timing-sensitive test that can be flaky under load; rerun in isolation if it fails. Tests that drive a real scanner subprocess must pin `--memory-limit 9999` (and raised `--memory-warn/crit-threshold`) — MemoryGuard reads SYSTEM memory, so its auto cap (total RAM × 0.8) can cancel even a tiny scan on a busy machine, and make now fails fast on the leftover checkpoint. tests/test_web_ui_js.py extracts the shipped `<script>` block and runs it under Node.js — skipped when node is not on PATH. tests/test_skill_manifest.py introspects both argparse trees and pins skill.json/skill_analysis.json/skill_ops.json command lists against them — so adding a CLI command without updating the manifests fails CI.)

## Language Support

| Language | Scanner | Extensions | Notes |
|----------|---------|------------|-------|
| C/C++ | tree-sitter | .c .h .cpp .cc .cxx .hpp | Full AST extraction; FFI target for ctypes/cgo/extern "C" |
| Go | tree-sitter | .go | Functions, methods, interfaces/structs (method sets, embedding → IMPLEMENTS), imports, goroutine FFI source (`import "C"`), interface dynamic dispatch (INFERRED DISPATCH edges from statically-typed receivers) |
| Python | tree-sitter | .py .pyw | Full AST extraction; ctypes FFI source (CDLL/WinDLL/cffi/pybind11) |
| Java | tree-sitter | .java | Classes, methods, annotations (Spring routes → API_entry, framework callbacks), extends/implements, JVM main detection |
| Rust | tree-sitter | .rs | Functions, traits, impls, `macro_rules!` definitions + invocations + calls inside macro arguments, attributes (`#[derive]`...); `extern "C"` FFI source |
| ASM | regex | .s .S .asm | NASM x86_64 + kernel GNU as; no tree-sitter. Inline asm edges use INFERRED confidence; syscall edges use synthetic `syscall_$NAME` nodes; JMP/ARM `b` treated as tail-calls; `__attribute__((naked))` pre-stripped; MSVC `__asm {}` blocks parsed; ERROR node regex fallback for unparseable constructs |

Documentation is available in English (`docs/en/`) and Chinese (`docs/zh/`); `scripts/check_docs_sync.py` enforces structural parity between the two trees.

## Key Directories

| Directory | Purpose |
|-----------|---------|
| `scripts/code2database_builder.py` | Builder CLI entry point (command hub) |
| `scripts/code2database_scanner.py` | Scanner CLI entry point |
| `scripts/_scanner/` | Language-specific AST scanners |
| `scripts/_builder/` | Graph building, query, export, memory, knowledge modules |
| `scripts/_builder/analysis/` | Invariants, value flow, lock coverage, path feasibility, data deps |
| `scripts/_builder/ops/` | Transactional updates (WAL + snapshots + fcntl locks) |
| `scripts/_builder/daemon/` | Background daemon (inotify + polling + Unix socket API) |
| `scripts/_builder/mcp/` | MCP server (83 tools, stdio + Streamable HTTP) |
| `scripts/_builder/flow/` | `c2d` umbrella engine + ask recipes |
| `scripts/_builder/misc/` | FFI bridge, Web UI, bug benchmark, doc-code alignment, intent router |
| `scripts/_detector/` / `scripts/_profile/` | Build/framework/community detection; profile schema + generation |
| `scripts/config/profiles/` | Built-in project profiles (DO NOT read into context) |
| `docs/en/` / `docs/zh/` | Documentation (en/zh parity enforced) |
