<div align="center">

# Code2Database

### Turn your codebase into a queryable code database — not just readable text

**One scan. Persistent graph. Surgical queries. Fewer tool calls. Faster answers.**

[![Languages](https://img.shields.io/badge/languages-6%20%2B%20ASM-orange)](#language-support)
[![MCP Tools](https://img.shields.io/badge/MCP_tools-53-blueviolet)](#mcp-server)
[![Query Commands](https://img.shields.io/badge/query_commands-201-success)](#command-reference)
[![Sub-skills](https://img.shields.io/badge/sub_skills-3-9cf)](#skill-activation)
[![Backend](https://img.shields.io/badge/backend-dual%20clang%20%2B%20tree--sitter-blue)](#extraction-backend)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.8%2B-blue?logo=python&logoColor=white)](#installation)
[![tree-sitter](https://img.shields.io/badge/tree--itter-AST-green?logo=tree-sitter&logoColor=white)](#how-it-works)

[![Claude Code](https://img.shields.io/badge/Claude_Code-supported-blueviolet.svg)](#installation)
[![Codex CLI](https://img.shields.io/badge/Codex_CLI-supported-blueviolet.svg)](#installation)
[![MCP stdio](https://img.shields.io/badge/MCP_stdio-supported-blue.svg)](#mcp-server)

[中文文档](docs/zh/README.md) · [English Docs](docs/en/) · [Skill Guide](docs/en/SKILL.md)

</div>

---

## The shift: from *reading* code to *querying* code

AI agents today understand a codebase the slow way — `grep`, `glob`, `Read`, one file at a time, rebuilding invocation paths and dependencies by hand. That's a pile of tool calls and round-trips before the real work even starts. Every question re-discovers the same structure.

**Code2Database turns that into a one-shot query.** Scan once, and your codebase becomes a persistent knowledge graph: every function, every call edge, every condition, every concurrency hazard, every field access — indexable, queryable, and LLM-ready. The next question isn't "let me grep for it" — it's one tool call that returns the exact nodes, the invocation path between them, the conditional branches that gate them, and the blast radius if you change them.

> **Surgical context, not a file-by-file search.** Fewer tool calls. Faster answers. And a graph that compounds in value as your team keeps querying it.

| Read-only code text          |  →  | Queryable code database                |
| :--------------------------- | :-: | :-------------------------------------- |
| `grep` / `glob` / `Read`     | →   | `explore-flow`  → nodes + paths         |
| one file at a time           | →   | `trace-chain`   → A → B with conditions |
| re-discover on every question | →  | `detect-races`  → cross-thread hazards  |
| manual call-path tracing     | →   | `param-flow`    → data flow across funcs |
| no concurrency visibility    | →   | `field-access`  → who reads/writes X    |
| no `#ifdef` awareness        | →   | `reverse-trace` → all paths to crash point |

---

## When to use it

- Understand how a codebase is structured and how functions call each other
- Trace execution paths from API entry points to internal implementations
- **Impact analysis** — what breaks if I change this function?
- **Concurrency risks** — detect data races and deadlocks in multi-threaded code
- **Debug crashes** by reverse-tracing from a crash point back to all entry points
- Understand how `#ifdef` conditional compilation affects invocation paths
- Query which functions access specific struct fields or global variables
- Build an always-up-to-date code graph that updates incrementally
- Give an LLM agent a compact project map it can drill into on demand

---

## Quick Start

```bash
# 1. Install
bash install.sh --lang en --target all

# 2. Generate a project profile (project-specific conventions: callbacks, vtables, lock APIs)
#    Auto-detects project type and writes .code2database_profile.json next to the source.
#    Built-in profiles exist for common projects (linux_kernel, dpdk, spdk, etc.) —
#    you can skip this step and pass --profile config/profiles/<type>.json to scan instead.
python3 scripts/code2database_scanner.py auto-profile \
  --source /path/to/code \
  --outdir /path/to/code

# 3. Scan source code → extraction.json (immutable AST facts)
python3 scripts/code2database_scanner.py scan \
  --source /path/to/code \
  --profile /path/to/code/.code2database_profile.json \
  --output code2db-out/.code2database_extraction.json

# 4. Build invocation graph → queryable database
python3 scripts/code2database_builder.py build \
  --extraction code2db-out/.code2database_extraction.json \
  --outdir code2db-out/ --build-config auto

# 5. Query the graph
python3 scripts/code2database_builder.py explore-flow \
  --graph code2db-out/ --query "initialization" --max-tokens 2000

# 6. (Optional) Start background daemon for real-time auto-sync
python3 scripts/code2database_builder.py daemon-start \
  --graph code2db-out/ --source /path/to/code
```

After building, start with `code2db-out/.code2database_context_pack_micro.md` (~200 tokens) for a project overview, upgrade to `lite` for more detail, then use the query commands to drill down. This **micro → lite → local** pattern keeps token cost minimal — the agent only loads what the question actually needs.

---

## Installation

### One-Click Install (Recommended)

```bash
# Interactive: choose language and install path
bash install.sh

# Non-interactive:
bash install.sh --lang en --dir ~/.claude/skills/Code2Database --target claudecode
bash install.sh --lang zh --dir ~/.local/share/Code2Database --target codex
bash install.sh --target all  # Install for all supported tools
```

The installer installs **3 sub-skills** under `~/.claude/skills/`:
- `Code2Database` (core — always loaded, owns `scripts/`)
- `Code2Database-analysis` (deep analysis — on-demand)
- `Code2Database-ops` (graph editing + ops — on-demand)

The two on-demand sub-skills symlink to the core's `scripts/` directory — single CLI, no duplication.

Supported targets:
- **claudecode** — Claude Code (installs skill to `~/.claude/skills/`, configures MCP server)
- **codex** — Codex CLI (adds instructions to `~/.codex/instructions.md`)
- **cursor** — Cursor (creates rule file + MCP config)
- **opencode** — OpenCode (creates config with MCP server)
- **gemini** — Gemini CLI (creates instructions file)

### Partial Language Install (lean install for single-language engineers)

If you only work with one or two languages, you can skip the tree-sitter grammars you don't need:

```bash
# Install only C and Go grammars (skip C++, Python, Java, Rust)
C2D_LANGUAGES=c,go bash install.sh --lang en --target claudecode

# Or run setup.sh directly with a language filter
bash scripts/setup.sh --languages c,go
bash scripts/setup.sh --languages c,cpp,rust --with-optional  # also installs libclang + z3
```

Valid values: `c`, `cpp`, `go`, `python`, `java`, `rust`, `all` (default). ASM uses regex — no tree-sitter grammar needed.

### Manual Install

```bash
# All languages (default)
pip install -r scripts/requirements.txt

# Or pick specific languages via setup.sh
bash scripts/setup.sh --languages c,go
```

Required: `networkx`, `tree-sitter` + language bindings (per-language — see Partial Language Install above)
Optional (recommended, NOT required):
- `libclang>=17.0` — enables cgdb clang backend (typed vtable dispatch, CFG, data flow, sync primitives, config predicates). Tree-sitter-only mode is fully functional without it.
- `z3-solver>=4.12` — sound path feasibility (heuristic fallback without it)
- `python-igraph>=0.11` + `leidenalg>=0.10` — cross-domain Leiden community detection (domain fallback without them)

---

## Language Support

Every language below gets full structural extraction and cross-file resolution into one graph — no per-language setup:

| Language | Extensions | What gets extracted |
|----------|------------|---------------------|
| **C / C++** | `.c` `.cc` `.cpp` `.h` `.hpp` `.cxx` | Functions, calls, `#ifdef` branches, struct field access, macros, callbacks |
| **Go** | `.go` | Functions, methods, goroutine spawns, channel ops, interface dispatch |
| **Python** | `.py` | Functions, classes, decorators, async calls, dynamic dispatch |
| **Java** | `.java` | Classes, methods, interface dispatch, annotations, overrides |
| **Rust** | `.rs` | Functions, traits, impls, async, generics, macro expansion |
| **Assembly** | `.S` `.s` `.asm` | Function labels, call instructions, register usage (regex-based) |

Plus **profile-driven** detection for frameworks, build systems, and project conventions — swap one Profile JSON to retarget the tool to a new project type.

---

## Why Code2Database? — what's different

Most code-graph tools stop at "function calls function." Code2Database goes deeper: it tracks **the conditions that gate each call**, the **concurrency context** in which it runs, the **fields and globals** it touches, and the **conditional compilation** that decides whether it's compiled at all. That's the gap between an *invocation graph* and a *code database you can ask real engineering questions of*.

| Capability | What it gives you |
|------------|-------------------|
| **Condition-aware chains** | `if`/`switch`/`#ifdef` branches with empty-node aggregation. You see not just "A calls B" but "A calls B **when** `CONFIG_X` is defined **and** `flag == 1`." |
| **Concurrency modeling** | Thread spawn, goroutine, callback detection. Cross-thread data race detection. Concurrency safety analysis: can these two chains actually run in parallel? |
| **Field-level access tracking** | Query "which functions read/write `task_struct->pid`?" — across the whole graph, not by grepping. |
| **Crash reverse-trace** | Given a crash point, trace *all* paths from entry points that reach it. Built for post-mortem debugging. |
| **Conditional compilation (`#ifdef`)** | The graph knows which calls exist only under which `CONFIG_*` flags. Critical for kernel / embedded / cross-platform C. |
| **4-tier LLM context packs** | `micro` (~200 tokens) → `lite` → `std` → `full`. The agent starts with the smallest map and only loads detail where the question demands. |
| **Edge confidence** | Every edge tagged `EXTRACTED` (1.0) / `INFERRED` (0.7–0.95) / `AMBIGUOUS` (0.1–0.3) — you always know what was found vs. guessed. |
| **Profile → Scan → Build separation** | Scan stage produces immutable AST facts; Build stage does inference. Extraction bug? Verify scan independently. Improve inference? Re-run Build in seconds, no re-scan. |
| **Bilingual docs (EN/中文)** | Skill instructions, references, and usage guides in both English and Chinese. |
| **Commit-based provenance** | Every node/edge carries `commit_meta.source_commit` (git/svn hash). Engineers verify with `git show <hash>`, not timestamps. See `describe-commit`, `node-history`, `graph-provenance`, `blame-node`, `find-commits`. |
| **Cypher-subset queries** | Declarative graph queries with `MATCH`/`WHERE`/`RETURN` — `query "MATCH (n)-[r:INVOKES]->(m) WHERE n.name =~ 'foo.*' RETURN n,m"`. |
| **Value flow & DATA_FLOW edges** | Trace parameter→return-value propagation across functions. Solves "where does this NULL come from?" — `value-flow`, `param-flow`. |
| **Lock-held region analysis** | Precise lock-held ranges with event-stream + char positions, not just "lock exists" — `lock-coverage`. Reduces false positives in race detection. |
| **Z3 path feasibility** | Auto-solve path feasibility with Z3 SMT solver; heuristic fallback when unavailable — `path-feasible`. Optional `z3-solver` dep. |
| **Cross-function data dependencies** | DATA_DEP edges scan ALL nodes for readers/writers, not just call-reachable successors — `data-dep`. |
| **Invariant extraction** | Preconditions, postconditions, loop_invariants, state_machine per function — `extract-invariants`, `find-invariants`, `apply-invariants`. |
| **LLM auto-semantic enhancement** | Confidence-threshold auto-write (EXTRACTED+evidence auto-applies; INFERRED requires confirm; AMBIGUOUS rejected) with batch-confirm and rollback — `auto-enhance`, `batch-confirm`, `rollback`, `fill-request`. |
| **Transactional updates** | WAL + snapshots + fcntl file locks for atomic multi-step updates — `tx-begin`/`commit`/`rollback`/`status`/`snapshot`/`restore`/`list-snapshots`/`replay-wal`. |
| **Cross-language FFI** | Python ctypes / Go cgo / Rust `extern "C"` boundary tracing with type marshalling — `ffi-detect`, `ffi-list`, `ffi-trace`, `ffi-types`. |
| **Interactive Web UI** | Single-file HTML/cytoscape.js/JS with pan/zoom, click-to-focus, focus+context fading, path highlighting, 3 layout algorithms (flow/rings/force), community compound grouping, LOD label hiding, edge `call_condition` labels, edge type filter, right-click context menu, minimap, FTS5 search — `web-ui`. |
| **BUG benchmark** | GraphInvestigator vs GrepInvestigator — measures recall, precision, tool calls, tokens, time — `bug-benchmark`. |
| **Profile health & auto-evolution** | 0-100 score across 7 categories; auto-detects new callback patterns; binds to git/svn HEAD — `profile-health`, `profile-evolve`, `profile-bind-version`. |
| **Doc-code alignment** | Detects return-value / param / signature / stale-doc mismatches between docs and code — `doc-code-check`, `doc-mark-stale`, `doc-alignment-report`, `doc-signature-diff`. `describe-node` surfaces `doc_code_mismatches`. |
| **Background daemon** | Long-running process monitors source files (inotify/polling) and auto-updates graph in transactions — `daemon-start`/`stop`/`status`/`force-refresh`/`pause`/`resume`/`wait-sync`/`logs`/`reload`/`list-projects`. Unix socket API at `/tmp/code2database-daemon-<project>.sock`. |

---

## Key Features

| Feature | Description |
|---------|-------------|
| **Multi-language AST scanning** | C/C++, Go, Python, Java, Rust, ASM via tree-sitter / regex |
| **Condition-aware chains** | `if`/`switch`/`#ifdef` branches with empty node aggregation and conditional compilation annotation |
| **Concurrency modeling** | Thread spawn, goroutine, callback detection; data race detection; concurrency safety analysis |
| **Edge confidence** | EXTRACTED (1.0) / INFERRED (0.7-0.95) / AMBIGUOUS (0.1-0.3) with source audit trail |
| **One-shot exploration** | `explore-flow` — single query to get relevant nodes, paths, and conditions |
| **Incremental updates** | `quick-update` — patch graph without LLM; `light-scan` / `patch-from-git` for zero-token updates |
| **MCP server mode** | `serve` — expose 53 MCP tools (34 `code2database_*` + 19 `cgdb_*`) for LLM agents (stdio transport) |
| **Knowledge management** | `extract-knowledge` / `knowledge-query` — principled invariant knowledge storage |
| **Memory system** | `save-memory` / `search-memory` / `manage-memory` — persistent Q&A memory with decay |
| **Blast radius analysis** | `blast-radius` — what functions, APIs, and tests are affected by a change |
| **Data race detection** | `detect-races` / `concurrency-analyze` — cross-thread data race and deadlock detection |
| **Crash reverse-trace** | `reverse-trace` — trace all paths reaching a crash point from entry points |
| **Field-level access** | `field-access` — query which functions read/write specific struct fields or globals |
| **Parameter flow** | `param-flow` — track how a value flows across function boundaries |
| **External code separation** | Automatic detection of third-party / vendored code — keeps the graph about *your* code |
| **Bilingual docs** | English (`docs/en/`) and Chinese (`docs/zh/`) skill instructions and references |
| **Commit provenance** | `commit_meta.source_commit` on every node/edge; verify with `git show <hash>` |
| **Cypher-subset queries** | `query` — MATCH/WHERE/RETURN declarative graph queries |
| **Value flow + DATA_FLOW** | `value-flow` — parameter→return-value propagation across functions |
| **Lock coverage** | `lock-coverage` — precise lock-held ranges with event-stream + char positions |
| **Path feasibility** | `path-feasible` — Z3 SMT solver for sound path feasibility |
| **Data dependencies** | `data-dep` — cross-function DATA_DEP edges; scans ALL nodes for readers/writers |
| **Invariants** | `extract-invariants` / `find-invariants` / `apply-invariants` — preconditions/postconditions/loop_invariants/state_machine |
| **Auto-enhancement** | `auto-enhance` / `batch-confirm` / `rollback` — confidence-threshold auto-write |
| **Transactions** | `tx-begin`/`commit`/`rollback` — WAL + snapshots + fcntl locks |
| **FFI tracing** | `ffi-detect`/`list`/`trace`/`types` — Python ctypes / Go cgo / Rust extern "C" |
| **Web UI** | `web-ui` — single-file HTML/cytoscape.js/JS interactive browser |
| **BUG benchmark** | `bug-benchmark` — GraphInvestigator vs GrepInvestigator recall/precision |
| **Profile health** | `profile-health`/`evolve`/`bind-version` — 0-100 score + auto-evolution |
| **Doc-code alignment** | `doc-code-check`/`mark-stale`/`alignment-report`/`signature-diff` — detect doc-code mismatches |
| **Background daemon** | `daemon-start`/`stop`/`status`/... — inotify + Unix socket + transactional sync |

---

## How It Works

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         Source files (C/C++/Go/Python/Java/Rust/ASM)      │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  [Profile]   project-specific config: skip names, callbacks, vtables,    │
│              domain rules, lock patterns, framework conventions           │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  [Scan]      tree-sitter AST extraction → extraction.json                │
│              Produces immutable facts: functions, params, body_text,     │
│              invoked, conditions, #ifdef, field reads/writes             │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  [Build]     Inference + graph construction                             │
│              vtable dispatch · callback bridging · domain classification │
│              community detection · race detection · edge confidence      │
│              invariants · FFI detection · doc-code alignment             │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  [Daemon]    (optional) inotify/polling → debounce → batch              │
│              → transactional sync → output file rebuild                  │
│              Circuit breaker: >1000 events/min → bulk rebuild           │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  [Query]     micro → lite → local · 53 MCP tools · 207 CLI commands     │
│              explore-flow · trace-chain · detect-races · param-flow      │
│              value-flow · lock-coverage · path-feasible · data-dep       │
│              extract-invariants · ffi-trace · doc-code-check · query     │
│              field-access · reverse-trace · blast-radius · daemon-*      │
└──────────────────────────────────────────────────────────────────────────┘
```

### Why three stages?

| Problem | Monolithic approach | Three-stage approach |
|---------|--------------------|---------------------|
| Switch projects | Change hardcoded rules | Only swap Profile JSON |
| Extraction bug | Can't tell if scan or build is wrong | Scan stage can be independently verified |
| Improve inference | Must re-scan everything | Only re-run Build (seconds, not minutes) |
| New language support | Rewrite everything | Write new Scanner only |

---

## Capability Matrix

| Capability | What it gives you |
|------------|-------------------|
| **Languages** | 6 + ASM — C/C++ (shared scanner), Go, Python, Java, Rust, ASM (regex — no tree-sitter grammar) (Python + tree-sitter + ASM regex) |
| **Storage** | JSON output + optional SQLite backend for large graphs |
| **MCP server** | stdio transport, **50 query tools** (34 `code2database_*` + 19 `cgdb_*`) for LLM agents |
| **CLI commands** | **207 commands** organized into 3 sub-skills (`/Code2Database` core, `/Code2Database-analysis`, `/Code2Database-ops`) — Build, Query, Trace, Concurrency, Knowledge, Memory, Provenance, Cypher, Data Flow, Lock Analysis, Path Feasibility, Invariants, Auto-Enhance, Transactions, FFI, Web UI, Benchmark, Profile Health, Doc-Code, Daemon, cgdb (clang backend) |
| **Call condition parsing** | `if`/`switch`/`#ifdef` branches + empty-node aggregation |
| **Conditional compilation (`#ifdef`)** | Graph knows which calls exist only under which `CONFIG_*` flags |
| **Data race detection** | Cross-thread hazard detection — `detect-races` |
| **Concurrency safety analysis** | Can these two chains actually run in parallel? — `concurrency-analyze` |
| **Field-level access tracking** | "Who reads/writes `task_struct->pid`" across the whole graph — `field-access` |
| **Crash reverse-trace** | All paths from entry points to a crash point — `reverse-trace` |
| **Execution scenarios** | End-to-end execution flow tracking from API entry to leaves |
| **Build-system macro resolution** | `compile_commands.json` + `#ifdef` macro expansion |
| **LLM context packs** | 4 tiers — `micro` (~200 tokens) → `lite` → `std` → `full` |
| **Blast radius analysis** | What breaks if I change this function? — `blast-radius` |
| **External code separation** | Auto-detect vendor/third-party code — graph is about *your* code |
| **Parameter flow tracking** | How a value flows across function boundaries — `param-flow` |
| **Commit provenance** | Every node/edge has `source_commit` (git/svn hash) — `describe-commit`, `node-history`, `graph-provenance`, `blame-node`, `find-commits` |
| **Cypher-subset queries** | Declarative graph queries — `query "MATCH ... WHERE ... RETURN ..."` |
| **Value flow** | Parameter→return-value propagation, DATA_FLOW edges — `value-flow` |
| **Lock coverage** | Precise lock-held ranges with event-stream + char positions — `lock-coverage` |
| **Path feasibility** | Z3 SMT solver for sound feasibility — `path-feasible` |
| **Data dependencies** | Cross-function DATA_DEP edges — `data-dep` |
| **Invariant extraction** | Preconditions/postconditions/loop_invariants/state_machine — `extract-invariants` |
| **LLM auto-enhancement** | Confidence-threshold auto-write + batch-confirm + rollback — `auto-enhance` |
| **Transactional updates** | WAL + snapshots + fcntl locks for atomic writes — `tx-begin`/`commit`/`rollback` |
| **FFI tracing** | Python ctypes / Go cgo / Rust extern "C" — `ffi-detect`/`list`/`trace`/`types` |
| **Interactive Web UI** | Single-file HTML/cytoscape.js/JS browser — `web-ui` |
| **BUG benchmark** | GraphInvestigator vs GrepInvestigator recall/precision — `bug-benchmark` |
| **Profile health** | 0-100 score + auto-evolution + git/svn HEAD binding — `profile-health`/`evolve`/`bind-version` |
| **Doc-code alignment** | Detect doc-code mismatches; surface in `describe-node` — `doc-code-check` |
| **Background daemon** | Real-time file monitoring + transactional auto-sync — `daemon-start` |

> **The design intent:** Code2Database isn't "more languages" or "more tools" — it's *deeper program semantics*. Conditions, concurrency, field access, `#ifdef`, crash tracing. The graph captures what an engineer actually needs to reason about, not just what's easy to extract.

---

## Command Reference

### Build & Update

| Command | Description |
|---------|-------------|
| `build` | Build invocation graph from extraction JSON |
| `update` | Incremental update (rescan changed files) |
| `sync` | Merge local + git-tracked code graph |
| `quick-update` | One-click patch + light-scan (no LLM) |
| `auto-profile` | Auto-detect project type and generate profile |
| `extract-signals` | Extract `#ifdef` conditional signal map |
| `install-hook` | Install git post-commit hook for auto quick-update |

### Query & Explore

| Command | Description |
|---------|-------------|
| `explore-flow` | One-shot context retrieval by query |
| `describe-node` | Node info at brief/standard/full detail |
| `get-code-snippet` | Extract source code around a node |
| `search` | Keyword search over nodes |
| `key-paths` | Automatic critical path extraction |

### Trace & Analyze

| Command | Description |
|---------|-------------|
| `trace-chain` | One-shot annotated path from A to B |
| `resolve-chain` | Trace chain with variable bindings |
| `reverse-trace` | Reverse-trace all paths reaching a crash point |
| `diff-chains` | Compare paths under two bindings |
| `blast-radius` | Impact analysis (affected APIs/tests/domains) |

### Concurrency & Data

| Command | Description |
|---------|-------------|
| `concurrency-risks` | List all concurrency risk points |
| `concurrency-analyze` | Analyze if two invocation chains can safely execute concurrently |
| `detect-races` | Detect data races across thread contexts |
| `data-lifecycle` | Trace resource alloc-use-release |
| `field-access` | Query functions that read/write specific struct fields or globals |
| `param-flow` | Track parameter flow across function boundaries |

### Knowledge & Memory

| Command | Description |
|---------|-------------|
| `extract-knowledge` | Extract principled invariants from code |
| `knowledge-query` | Query stored knowledge |
| `knowledge-validate` | Validate knowledge against graph (includes doc-code alignment) |
| `save-memory` | Save persistent Q&A memory |
| `search-memory` | Search memory with decay |
| `manage-memory` | Manage memory entries |

### Provenance & History

| Command | Description |
|---------|-------------|
| `describe-commit` | Show all changes introduced by a specific commit |
| `node-history` | Show commit history of a node |
| `graph-provenance` | Show graph-wide provenance summary |
| `blame-node` | Find the commit that introduced a node |
| `find-commits` | Find commits touching a function/file |

### Query Language & Data Flow

| Command | Description |
|---------|-------------|
| `query` | Cypher-subset query (MATCH/WHERE/RETURN) |
| `value-flow` | Trace parameter→return-value propagation across functions (DATA_FLOW edges) |
| `param-flow` | Track parameter flow through invocation chain |
| `data-dep` | Cross-function data dependency (DATA_DEP edges; scans ALL nodes) |

### Lock Analysis & Path Feasibility

| Command | Description |
|---------|-------------|
| `lock-coverage` | Lock-held region analysis with event-stream + char positions |
| `path-feasible` | Z3 SMT solver for path feasibility (heuristic fallback) |

### Invariants & Auto-Enhancement

| Command | Description |
|---------|-------------|
| `extract-invariants` | Extract preconditions/postconditions/loop_invariants/state_machine |
| `find-invariants` | Find invariants matching a pattern |
| `apply-invariants` | Apply extracted invariants to graph |
| `auto-enhance` | LLM auto-semantic enhancement (confidence-threshold auto-write) |
| `batch-confirm` | Batch-confirm pending enhancements |
| `rollback` | Rollback applied enhancements |
| `fill-request` | List fields the LLM should fill |

### Transactions

| Command | Description |
|---------|-------------|
| `tx-begin` | Begin a transaction (snapshot + WAL) |
| `tx-commit` | Commit current transaction |
| `tx-rollback` | Rollback current transaction (restores snapshot) |
| `tx-status` | Show transaction status |
| `tx-snapshot` | Create a named snapshot |
| `tx-restore` | Restore from a named snapshot |
| `tx-list-snapshots` | List all snapshots |
| `tx-replay-wal` | Replay WAL entries (crash recovery) |

### Cross-Language FFI

| Command | Description |
|---------|-------------|
| `ffi-detect` | Detect FFI bindings (Python ctypes / Go cgo / Rust extern "C") |
| `ffi-list` | List all FFI binding sites |
| `ffi-trace` | Trace cross-language invocation chains |
| `ffi-types` | Show type marshalling for an FFI edge |

### Web UI & Benchmark

| Command | Description |
|---------|-------------|
| `web-ui` | Start interactive Web UI server (default port 8765) |
| `bug-benchmark` | Run GraphInvestigator vs GrepInvestigator benchmark |

### Profile Health & Doc-Code Alignment

| Command | Description |
|---------|-------------|
| `profile-health` | Compute 0-100 health score across 7 categories |
| `profile-evolve` | Detect new callback patterns; optionally apply EXTRACTED suggestions |
| `profile-bind-version` | Bind profile to current git/svn HEAD commit |
| `doc-code-check` | Check doc-code alignment; detect return-value/param/signature mismatches |
| `doc-mark-stale` | Mark a node's doc as stale (e.g., after code change) |
| `doc-alignment-report` | Generate full Markdown doc-code alignment report |
| `doc-signature-diff` | Detect signature changes between two graph versions |

### Daemon

| Command | Description |
|---------|-------------|
| `daemon-start` | Start background daemon (foreground; blocks) |
| `daemon-stop` | Stop a running daemon |
| `daemon-status` | Get daemon status (pid, last_sync, pending events, stale nodes) |
| `daemon-force-refresh` | Force re-scan a specific file |
| `daemon-pause` | Pause daemon (e.g., before manual updates) |
| `daemon-resume` | Resume daemon after pause |
| `daemon-wait-sync` | Block until current sync completes (LLM agents call before queries) |
| `daemon-logs` | Show daemon log file (use `--follow` for streaming) |
| `daemon-reload` | Reload daemon config (re-reads profile) |
| `daemon-list-projects` | List all projects with daemon state files |

### Serve

| Command | Description |
|---------|-------------|
| `serve` | Start MCP server (stdio transport, 53 tools: 34 `code2database_*` + 19 `cgdb_*`) |

All query commands support `--json` for structured output and `--max-tokens` for budget control.

---

## MCP Server

Launch Code2Database as an MCP server to give any MCP-compatible agent (Claude Code, Codex, etc.) real-time access to your code graph:

```bash
python3 scripts/code2database_builder.py serve --graph code2db-out/
```

Exposes **53 MCP tools** over stdio transport: 34 `code2database_*` tools (including `explore-flow`, `trace-chain`, `describe-node`, `detect-races`, `param-flow`, `field-access`, `reverse-trace`, `blast-radius`) plus 19 `cgdb_*` tools that query the cgdb (code graph database) layer directly when the clang extraction backend is enabled (typed vtable dispatch, CFG, data flow, sync primitives, config predicates, time-travel versions). The agent can query the graph directly without re-reading source files — surgical context in one tool call.

---

## Extraction Backend (dual clang + tree-sitter)

Code2Database supports a **dual backend** for C/C++ extraction. You pick the backend at scan time; the choice is recorded in the extraction JSON and reused by `build`.

| Backend | When to use | Dependencies | Enables |
|---------|-------------|--------------|---------|
| `auto` (default) | Most users | tree-sitter (required) + libclang (optional) | tree-sitter always; cgdb layer when libclang is present |
| `clang` | C/C++ projects needing deep semantic analysis (typed vtable dispatch, CFG, data flow, sync primitives, config predicates) | libclang 17+ (`pip install libclang==17.0.6`) | Full cgdb layer + 19 `cgdb_*` MCP tools |
| `tree-sitter` | Lean install, no libclang dependency, pure AST extraction | tree-sitter language bindings only | Standard code graph — no cgdb layer |

**libclang is recommended, NOT required.** Tree-sitter-only mode is fully functional — every supported language can be scanned, built, and queried. The clang backend additionally populates the cgdb (code graph database) layer.

```bash
# Tree-sitter only (no libclang) — works for all 6 + ASM languages
python3 scripts/code2database_scanner.py scan --source /path --extraction-backend tree-sitter

# Auto (uses clang if available, falls back to tree-sitter)
python3 scripts/code2database_scanner.py scan --source /path --extraction-backend auto

# Force clang backend — enables cgdb layer for C/C++
python3 scripts/code2database_scanner.py scan --source /path --extraction-backend clang
```

### cgdb (code graph database) layer

When the clang backend is enabled, the build step populates the cgdb semantic tables alongside the legacy `functions`/`edges` tables:

| Layer | Table | Content |
|-------|-------|---------|
| L1 | `cgdb_nodes` | AST nodes (functions, types, vars, fields) with source range |
| L2 | `cgdb_types` | Type definitions (struct, union, enum, typedef) |
| L3.5 | `cgdb_predicates` | Config predicates (`#ifdef CONFIG_*`) with source range |
| L4 | `cgdb_basic_blocks` + `cgdb_cfg_edges` | Control-flow graph per function |
| L5 | `cgdb_data_flow` | Def-use chains per function |
| L6 | `cgdb_aliases` | Alias analysis (stub for MVP) |
| L7 | `cgdb_ops_bindings` | Typed vtable dispatch (FieldDecl → FunctionDecl) |
| L8 | `cgdb_sync_primitives` + `cgdb_happens_before` | Sync primitives + happens-before |
| L10 | `cgdb_versions` | Time-travel version queries |

These tables are queried directly via 19 `cgdb_*` MCP tools — see the `/Code2Database-analysis` sub-skill for the full tool reference.

---

## Skill Activation (3 sub-skills)

The skill is split into 3 sub-skills to keep LLM context lean. Each sub-skill has its own `SKILL.md` exposing only the commands relevant to its layer. The CLI (`scripts/code2database_builder.py`) is shared — all 207 commands are accessible regardless of which sub-skill is active.

| Sub-skill | Trigger | Purpose |
|-----------|---------|---------|
| `Code2Database` (core) | `/Code2Database` | Build + browse — always loaded. 15 Tier-1 high-weight commands (scan, build, explore-flow, describe-node, trace-chain, etc.) |
| `Code2Database-analysis` | `/Code2Database-analysis` | Deep semantic analysis — concurrency, data flow, invariants, FFI, provenance, path feasibility, cgdb tables. 13 Tier-1 commands + 19 `cgdb_*` MCP tools |
| `Code2Database-ops` | `/Code2Database-ops` | Graph editing + ops — transactions, daemon, profile/doc-code, exports, plugins, memory, embeddings. 14 Tier-1 commands |

When the core skill detects a deep-analysis or ops question, it explicitly hands off to the appropriate sub-skill. MCP server (53 tools) is separate from skill activation — all 53 tools are accessible regardless of which sub-skill is active.

---

## Daemon Mode

For real-time auto-sync, start the background daemon. It monitors source files via inotify (Linux, ctypes, zero-dependency) or polling (cross-platform fallback), debounces editor saves, batches changes, and runs incremental scans in transactions.

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

**LLM agent protocol** — before important queries, check freshness:

```bash
# Check if graph is up-to-date
daemon-status --graph code2db-out/
# If syncing or pending events, block until sync completes
daemon-wait-sync --graph code2db-out/ --timeout 30
```

Daemon writes state to `<graph_dir>/.daemon_status.json` and logs to `~/.code2database/daemon-<project>.log`. Configuration via profile `daemon` section (see `docs/<lang>/RUNTIME_CONFIG.md`).

**Circuit breaker**: if events exceed 1000/minute (e.g., `git checkout` of a large branch), the daemon switches to "wait + bulk rebuild" mode instead of per-file incremental.

---

## Architecture: Profile → Scan → Build → Daemon

```
Source files → [Profile config] → [Scan] → extraction.json → [Build] → code graph output
                                                                     ↓
                                                              [Daemon] (optional)
                                                              inotify/polling → debounce
                                                              → transactional sync
                                                              → output file rebuild
```

- **Profile**: Project-specific config (skip names, callback patterns, vtable types, domain rules, lock patterns, FFI bindings, daemon config)
- **Scan**: AST extraction (tree-sitter) — produces immutable facts
- **Build**: Inference + graph construction — vtable dispatch, callback bridging, domain classification, community detection, race detection, invariant extraction, FFI detection, doc-code alignment
- **Daemon** (optional): long-running process that monitors source files and auto-updates the graph in transactions

This separation lets you:
- Swap projects by swapping one Profile JSON (no code changes)
- Verify scan correctness independently of inference quality
- Re-run Build in seconds after improving inference (no re-scan)
- Add a new language by writing only a new Scanner
- Keep the graph up-to-date automatically via daemon mode

---

## Documentation

| Path | Description | Audience |
|------|-------------|----------|
| `docs/en/SKILL.md` | Skill instructions — compact agent guide (~3K tokens) | AI agents (auto-loaded) |
| `docs/zh/SKILL.md` | Skill instructions — compact agent guide (Chinese) | AI agents (auto-loaded) |
| `docs/en/references/usage_reference.md` | Detailed command syntax, parameters, code blocks | On-demand reference |
| `docs/zh/references/usage_reference.md` | Detailed command syntax, parameters, code blocks (Chinese) | On-demand reference |
| `docs/*/references/` | Data model, label rules, endpoint pipeline, etc. | On-demand reference |
| `docs/en/OVERVIEW.md` | Internal architecture and algorithms | Tool developers only |
| `docs/en/PROFILE_MANUAL.md` | Profile writing guide | Tool developers |
| `docs/en/RUNTIME_CONFIG.md` | Runtime configuration reference | Tool developers |
| `CLAUDE.md` | Claude Code integration guide | AI agents (auto-loaded) |
| `AGENTS.md` | Codex / agent integration guide | Tool developers |

---

## Important Constraints

- **Never pre-load** `scripts/config/profiles/` or `docs/*/references/` into context — read on demand only
- **Global-to-local query mode**: always start from micro/lite context packs, then drill down
- **Only seven labels** supported (API_entry, thread_processor, callback_func, constructor, destructor, out_end, unknown_end)
- **Do not propose fixes** before finding root cause
- **Always verify** after sync/update operations
- **DB writes need user confirmation**: LLM-initiated `update-node` / `update-edge` / `patch-profile` / `apply-semantics` / `apply-invariants` / `auto-enhance` (EXTRACTED+evidence bypasses; INFERRED requires confirm) / `profile-evolve --apply` (EXTRACTED only) / `doc-mark-stale` must prompt for user confirmation to prevent hallucinated data poisoning the graph
- **Transactional writes**: wrap multi-step DB changes in `tx-begin` / `tx-commit` so failures roll back atomically; `patch-from-diff` / `patch-from-git` already wrap by default
- **Daemon freshness**: before important queries, call `daemon-status`; if `syncing` or pending events, call `daemon-wait-sync` to block until sync completes
- **Doc-code alignment**: if `describe-node` returns non-empty `doc_code_mismatches`, the doc (`semantic_desc`) may be unreliable — consult `body_text` and consider `doc-mark-stale` until docs are re-extracted
- **Commit provenance**: node/edge `commit_meta.source_commit` is a git/svn hash — engineers verify with `git show <hash>`, not timestamps

---

## License

MIT
