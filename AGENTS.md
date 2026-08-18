# AGENTS.md

This file provides instructions for AI coding agents (Codex, Copilot, etc.) working with Code2Database.

> **Boundary**: This file is for developers modifying the Code2Database skill itself. For usage instructions, follow `SKILL.md`. Do NOT load `OVERVIEW.md` or `scripts/` into agent context — they are implementation details.

## Scope

| | |
|--|--|
| **Reads** | `scripts/`, `docs/`, `config/`, `evals/`, and target source directories as needed |
| **Writes** | Only paths required for the requested change; keep diffs minimal |
| **Executes** | `python3` for scanner/builder CLI, `pip` for dependencies, `bash` for setup |
| **Off-limits** | `.code2database_*` output files (use query commands instead), `scripts/config/profiles/` (internal templates), `.code2database_wal.log` / `.code2database_snapshots/` (transaction internals — use `tx-restore` instead) |

## Project Overview

Code2Database is a multi-language code graph generator for C/C++/Go/Python/Java/Rust/ASM codebases. It produces directed invocation graphs with:
- Call ordering and conditional path annotation
- Conditional expression call extraction (if/while/for/switch/ternary with &&/|| compound conditions)
- Cross-file invoked resolution via multi-strategy pipeline (suffix index, import_map, same_domain, unique_name)
- `#ifdef` conditional compilation tracking
- Concurrency risk analysis and data race detection
- Field-level access tracking
- GCC extended asm support (named operands, asm goto, register bindings, JMP tail-calls)
- MSVC `__asm {}` block support and ERROR node fallback
- Static fn-ptr dispatch array resolution (dispatch_op)
- LLM context packs (micro/lite/standard/full tiers)
- MCP server mode for real-time agent queries (48 tools: 30 `code2database_*` + 18 `cgdb_*`)

Capabilities:
- **Dual extraction backend** — `auto` (default, uses clang when libclang is installed, falls back to tree-sitter), `clang` (force clang, enables cgdb layer; libclang 17+), `tree-sitter` (force tree-sitter, no libclang dep). Selected via `--extraction-backend` flag at scan time. **libclang is recommended, NOT required** — tree-sitter-only mode is fully functional.
- **cgdb (code graph database) layer** — when clang backend is enabled, 13 semantic tables are populated alongside legacy `functions`/`edges`: L1 AST nodes, L2 types, L3.5 config predicates, L4 CFG, L5 data flow, L6 alias (stub), L7 ops_bindings (typed vtable dispatch), L8 sync_primitives + happens_before, L10 provenance + time-travel versions. Queried via 18 `cgdb_*` MCP tools.
- Commit-based provenance — every node/edge carries `commit_meta.source_commit` (git/svn hash)
- Cypher-subset query language (`query` command, MATCH/WHERE/RETURN)
- Value flow with DATA_FLOW edges (`value-flow`, `param-flow`)
- Lock-held region analysis with event-stream + char positions (`lock-coverage`)
- Z3 SMT path feasibility (`path-feasible`; optional `z3-solver` dep)
- Cross-function data dependency with DATA_DEP edges (`data-dep`)
- Invariant extraction (`extract-invariants`, `find-invariants`, `apply-invariants`) — preconditions/postconditions/loop_invariants/state_machine
- LLM auto-semantic enhancement with confidence-threshold auto-write (`auto-enhance`, `batch-confirm`, `rollback`, `fill-request`)
- Transactional updates with WAL + snapshots + fcntl locks (`tx-begin`/`commit`/`rollback`/`status`/`snapshot`/`restore`/`list-snapshots`/`replay-wal`)
- Cross-language FFI tracing — Python ctypes / Go cgo / Rust extern "C" (`ffi-detect`/`list`/`trace`/`types`)
- Interactive Web UI — single-file HTML/SVG/JS (`web-ui`)
- BUG benchmark — GraphInvestigator vs GrepInvestigator (`bug-benchmark`)
- Profile health (0-100 across 7 categories) + auto-evolution + git/svn HEAD binding (`profile-health`, `profile-evolve`, `profile-bind-version`)
- Doc-code dual-source truth alignment — return value / param / signature / stale-doc mismatch detection (`doc-code-check`, `doc-mark-stale`, `doc-alignment-report`, `doc-signature-diff`)
- Background daemon — inotify + polling fallback, Unix socket API, circuit breaker, transactional sync, auto output file rebuild (`daemon-start`/`stop`/`status`/`force-refresh`/`pause`/`resume`/`wait-sync`/`logs`/`reload`/`list-projects`)

## Skill Structure (3 sub-skills)

The skill is split into 3 sub-skills to keep LLM context lean. The CLI (`scripts/code2database_builder.py`, 122 commands) is shared — all commands are accessible regardless of sub-skill activation.

| Sub-skill | Trigger | Purpose |
|-----------|---------|---------|
| `Code2Database` (core) | `/Code2Database` | Build + browse — always loaded. 15 Tier-1 high-weight commands. |
| `Code2Database-analysis` | `/Code2Database-analysis` | Deep semantic analysis (concurrency, data flow, invariants, FFI, provenance, path feasibility, cgdb tables). 13 Tier-1 commands + 18 `cgdb_*` MCP tools. |
| `Code2Database-ops` | `/Code2Database-ops` | Graph editing + ops (transactions, daemon, profile/doc-code, exports, plugins, memory, embeddings). 14 Tier-1 commands. |

## Pipeline Architecture

```
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

- **Never directly read** output `.json`/`.md` files — always use query commands
- **Start with micro/lite context packs** before reading detailed data
- **Only 7 labels** allowed: API_entry, thread_processor, callback_func, constructor, destructor, out_end, unknown_end
- **Do not pre-load** `scripts/config/profiles/` or `docs/*/references/` into context
- **Edge confidence** must always be annotated: EXTRACTED / INFERRED / AMBIGUOUS
- **Never skip** intermediate nodes when tracing invocation chains
- **ASM scanner** uses regex (not tree-sitter); inline asm edges use INFERRED confidence; syscall edges use synthetic `syscall_$NAME` nodes; GCC extended asm named operands and asm goto are supported; JMP/ARM `b` treated as tail-calls; `__attribute__((naked))` pre-stripped; MSVC `__asm {}` blocks parsed; ERROR node regex fallback for unparseable constructs
- **Transactional writes**: DB-modifying operations should be wrapped in `tx-begin`/`tx-commit` for multi-step changes. `patch-from-diff`/`patch-from-git` already wrap by default; use `--no-transaction` to bypass.
- **Daemon freshness**: call `daemon-status` before important queries; if `syncing` or pending events, call `daemon-wait-sync` to block until sync completes. Circuit breaker triggers bulk rebuild above 1000 events/minute.
- **Doc-code alignment**: `describe-node` surfaces `doc_code_mismatches` — if non-empty, `semantic_desc` may be unreliable; consult `body_text` and consider `doc-mark-stale`.
- **Invariants confidence**: never apply AMBIGUOUS invariants; INFERRED require user review.
- **FFI tracing**: requires both source and target language scanners to detect a binding site.
- **Profile evolution**: `profile-evolve --apply` only applies EXTRACTED-confidence suggestions; run `profile-bind-version` after evolution to bind to git/svn HEAD.

## Testing

```bash
python3 -m pytest tests/ -v
```

Capability modules have dedicated unit tests in `tests/` covering:
- Invariant extraction (preconditions/postconditions/loop_invariants/state_machine)
- Auto-enhance confidence thresholds + rollback
- Transactions (snapshot/restore/WAL recovery)
- FFI detection (ctypes/cgo/extern "C")
- Web UI HTTP endpoints
- BUG benchmark (GraphInvestigator vs GrepInvestigator)
- Profile health (0-100 scoring) + evolution
- Doc-code alignment (return value / param / signature / stale-doc mismatches)
- Daemon (state persistence, socket API, force-refresh, file modification handling)

## Language Support

| Language | Scanner | Extensions | Notes |
|----------|---------|------------|-------|
| C/C++ | tree-sitter | .c .h .cpp .cc .cxx .hpp | Full AST extraction; FFI target for ctypes/cgo/extern "C" |
| Go | tree-sitter | .go | Full AST extraction; cgo FFI source (`import "C"`) |
| Python | tree-sitter | .py .pyw | Full AST extraction; ctypes FFI source (CDLL/WinDLL/cffi/pybind11) |
| Java | tree-sitter | .java | Full AST extraction |
| Rust | tree-sitter | .rs | Full AST extraction; `extern "C"` FFI source |
| ASM | regex | .s .S .asm | NASM x86_64 + kernel GNU as; no tree-sitter |

Documentation is available in English (`docs/en/`) and Chinese (`docs/zh/`). The `SKILL.md` in each language directory contains the full skill instructions.

## Capability Quick Reference

| Capability | Commands | Purpose |
|------------|----------|---------|
| Commit provenance | `describe-commit`, `node-history`, `graph-provenance`, `blame-node`, `find-commits` | Commit-based provenance with git/svn hash verification |
| Cypher queries | `query` | Cypher-subset query language (MATCH/WHERE/RETURN) |
| Value flow | `value-flow`, `param-flow` | Parameter→return-value propagation; DATA_FLOW edges |
| Lock coverage | `lock-coverage` | Lock-held region analysis with event-stream + char positions |
| Path feasibility | `path-feasible` | Z3 SMT path feasibility (heuristic fallback) |
| Data dependencies | `data-dep` | Cross-function DATA_DEP edges; scans ALL nodes |
| Invariants | `extract-invariants`, `find-invariants`, `apply-invariants` | Preconditions/postconditions/loop_invariants/state_machine |
| Auto-enhancement | `auto-enhance`, `batch-confirm`, `rollback`, `fill-request` | LLM auto-semantic enhancement with confidence thresholds |
| Transactions | `tx-begin`, `tx-commit`, `tx-rollback`, `tx-status`, `tx-snapshot`, `tx-restore`, `tx-list-snapshots`, `tx-replay-wal` | Transactional updates (WAL + snapshots + fcntl locks) |
| FFI tracing | `ffi-detect`, `ffi-list`, `ffi-trace`, `ffi-types` | Cross-language FFI (Python ctypes / Go cgo / Rust extern "C") |
| Web UI | `web-ui` | Interactive single-file HTML/SVG/JS viewer |
| BUG benchmark | `bug-benchmark` | GraphInvestigator vs GrepInvestigator recall/precision |
| Profile health | `profile-health`, `profile-evolve`, `profile-bind-version` | 0-100 scoring + auto-evolution + git/svn HEAD binding |
| Doc-code alignment | `doc-code-check`, `doc-mark-stale`, `doc-alignment-report`, `doc-signature-diff` | Detect doc-code mismatches (return value / param / signature / stale-doc) |
| Background daemon | `daemon-start`, `daemon-stop`, `daemon-status`, `daemon-force-refresh`, `daemon-pause`, `daemon-resume`, `daemon-wait-sync`, `daemon-logs`, `daemon-reload`, `daemon-list-projects` | Real-time file monitoring + transactional auto-sync |
