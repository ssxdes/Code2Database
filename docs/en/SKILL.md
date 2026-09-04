---
name: Code2Database
description: "Turn a codebase into a queryable code database. Scan once, query forever — no more grep/glob/Read. Supports C/C++/Go/Python/Java/Rust/ASM with invocation graphs, conditional paths, concurrency analysis, data flow, FFI tracing, and 19 cgdb semantic tables. 83 MCP tools (55 base + 28 design-report) + 227 CLI commands. Use /Code2Database when the question involves code structure, call chains, impact analysis, concurrency, or data flow."
trigger: /Code2Database
---

# /Code2Database

**Scan once → persistent graph → query instead of grep.** One tool call answers questions that would otherwise require multiple grep/glob/Read across files.

## Query Priority Chain

When a question is asked, follow this priority:

```
1. Memory (recall / kb-query) — did we answer this before? → fastest
2. Knowledge (know / kb-query) — architecture-level invariants recorded?
3. Graph (query / describe / trace) — query the code graph
4. Source (describe --code) — read source as last resort
```

`kb-query` is the unified FTS5+BM25 query surface across both memory
and knowledge stores. The `query` (Cypher) command automatically surfaces
top kb hits as a `_hints` field alongside graph rows.

## When to Activate

- Any question about call relationships, chains, architecture, impact, concurrency
- When `code2db-out/` or `code2database.db` exists — query instead of grep
- `#ifdef` conditional paths, data races, FFI boundaries, data flow

## Quick Start

```bash
# 0. First time on a project: one-click ingestion (env-check fails fast)
python3 scripts/code2database_builder.py make --source /path/to/project
#   → phase 1 env-check BEFORE any build step: missing compile_commands.json /
#     libclang / tree-sitter grammars are reported up front (never mid-build)
#   → phase 2: scan -> build -> derived artifacts (value-flow, data-dep,
#     #ifdef signals, FFI, brief, kb index, embeddings) -> exports
#     (Obsidian vault, HTML) -> profile-health report
#   → make --check: env-check only, no build
#   → re-runs are safe: graph artifacts rebuilt, memory/knowledge preserved

# 1. Session start (MANDATORY): load the full project context
python3 scripts/code2database_builder.py session-init    # --graph auto-discovers code2db-out/
#   → brief + veteran memory digest + graph state + unanswered questions
#   → if no brief yet: brief-extract to bootstrap, then curate with brief-update

# 2. Query (repeatable)
python3 scripts/code2database_builder.py describe --node bdev_start
python3 scripts/code2database_builder.py kb-query --query "bdev register"
python3 scripts/code2database_builder.py trace --from bdev_start --to spdk_app_start
python3 scripts/code2database_builder.py serve    # MCP server (83 tools)
```

## Core Commands (25)

| Command | Purpose | Query Layer |
|---------|---------|-------------|
| `query` | Natural-language intent query (kb hints to stderr; `--with-hints` wraps stdout) | Memory→Knowledge→Graph |
| `kb-query` | Unified FTS5+BM25 across memory + knowledge | Memory+Knowledge |
| `describe` | Node details + source snippet + memory_refs + knowledge_refs | Graph→Source |
| `trace` | Call chain A→B with conditions | Graph |
| `impact` | What breaks if I change X? | Graph |
| `find` | Search by pattern (invariants, macros) | Graph |
| `flow` | Data/value/param flow | Graph |
| `concurrency` | Race/deadlock detection | Graph |
| `context` | Get context around a location | Graph |
| `make` | One-click ingestion: env-check (fail fast) then scan + build + all derived artifacts + exports | — |
| `build` | Scan + build graph (manual, make wraps it) | — |
| `update` | Incremental re-scan | — |
| `session-init` | One-shot session context: brief + memory digest + graph (+staleness check) + known-unknowns (alias: `init`) | Memory+Knowledge |
| `save-memory` | Save Q&A to memory, `--category bdev/nvme/pcie` `--author` `--symbol fn` repeatable — grounds the memory to code (alias: `save`) | Memory |
| `search-memory` | Search memory: FTS5 + `--category/--tags/--author/--symbol` filters, CJK-aware (alias: `recall`) | Memory |
| `knowledge-brief` | Render project brief — load at session start (alias: `brief`) | Knowledge |
| `kb-rebuild-index` | Rebuild FTS5 index from memory.db + brief.json | Memory+Knowledge |
| `kb-cluster` | Cluster similar items + link principles | Memory+Knowledge |
| `kb-known-unknowns` | List unanswered queries (feedback loop) | Memory+Knowledge |
| `kb-audit` | Knowledge audit (citations, staleness, confidence) | Memory+Knowledge |
| `kb-forget` | Immediately delete a memory/knowledge item | Memory+Knowledge |
| `serve` | Start MCP server (83 tools) | All |
| `web-ui` | Interactive browser (cytoscape.js) | All |
| `tx-begin` | Start a transaction | Ops |
| `daemon` | Background auto-sync | Ops |
| `health` | Graph freshness + profile health | — |

All 227 CLI commands remain accessible; the 25 above cover ~95% of agent workflows.

## Supported Languages

C/C++ | Go | Python | Java | Rust | ASM (6 + ASM, C/C++ share scanner)

## Extraction Backend

- `auto` (default) — clang when available, tree-sitter fallback
- `clang` — enables cgdb semantic layer (19 `cgdb_*` MCP tools)
- `tree-sitter` — no libclang dependency

## MCP Server

```bash
python3 scripts/code2database_builder.py serve --graph code2db-out/
```

83 tools: 36 `code2database_*` (incl. `code2database_session_init` one-shot session context, `code2database_save_memory` for MCP-side experience accumulation, `code2database_kb_query` for unified memory+knowledge search) + 19 `cgdb_*` (clang semantic layer) + 28 design-report.

## Constraints

- **Session start**: run `session-init` (alias `init`) FIRST — brief (mandatory rules/modes/pitfalls) + memory digest (veteran experience) + graph state with source-freshness warning (rebuild before trusting a STALE graph) + known-unknowns, in one prompt-ready output
- **Correction protocol**: before answering a project question, `search-memory` first; if an answer is WRONG use `save-memory --correct` (reshapes the most similar entry in place — no duplicate variant); if MISSING use `save-memory --category ... --author ... --symbol fn`; if a query repeatedly misses (known-unknowns in session-init), capture the answer into memory
- **Symbol grounding**: when a memory is about a specific function/type, pass `--symbol <name>` (repeatable) — the web UI shows that Q&A on the symbol's node page, and `search-memory --symbol` / `code2database_memory_search(symbol=)` filter by it. Memories absorb symbols on merge and re-ground on `--correct`
- **Capture triggers** (when to save-memory, so accumulation doesn't depend on luck): after (a) solving a non-trivial problem — the resolution path IS the answer; (b) hitting a pitfall that cost real debugging time; (c) discovering a mandatory rule/constraint the brief doesn't capture yet; (d) correcting a wrong answer (`--correct`); (e) answering a known-unknowns query from session-init. Skip anything the graph answers in one query.
- Run `kb-rebuild-index` after `build`/`update` or after memory/brief edits
- Memory is a shared accumulating store (memory.db): save with `--category path/to/topic` + `--author`; govern with `manage-memory --action split/merge/move/categories`
- Knowledge (brief.json) must stay lean: `brief-validate` warns above 3000 chars; move overflow into memory instead
- Start with `context_pack_micro` → `context_pack_lite` → `describe`/`trace`
- Only 7 labels: API_entry, thread_processor, callback_func, constructor, destructor, out_end, unknown_end
- Edge confidence: EXTRACTED / INFERRED / AMBIGUOUS
- DB writes require user confirmation
- Daemon freshness: check `daemon-status` before important queries; note the daemon holds (does not sync) events during its startup grace window (`startup_grace_active`)
- `update`/`merge`/`sync` commands require in-memory nx.DiGraph. On large projects
  (>=50K functions), `_load_full_graph` returns LazySQLiteGraph (read-only SQLite view).
  These commands will print a friendly error directing to `daemon-start` or `build`.
  Use `daemon-start` for incremental sync (designed for SQLite-backed large graphs).
- Concurrency analysis (`detect-races`, `concurrency-analyze`) is function-level,
  not access-site-level. TOCTOU races are NOT detected. Lock detection uses regex,
  not CFG. Results may have false positives/negatives — use `lock-coverage` for
  finer-grained analysis.
- `path`/`trace-chain` may return ambiguous results for same-name functions in
  different source files. Use `--source-file` to disambiguate. When `--source-file`
  is provided, `--from`/`--to` accept function names (resolved by name+file);
  without `--source-file`, they must be node IDs. If a name resolves to multiple
  nodes across files, a warning is printed listing the candidate source files.
- `path --domain-filter fs,block` hard-restricts traversal to nodes whose domain
  is in the allowlist (or `root`). Use for cross-subsystem reachability queries
  that must stay within a known set of subsystems. Comma-separated list supported.
