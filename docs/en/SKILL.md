---
name: Code2Database
description: "Turn a codebase into a queryable code database. Scan once, query forever — no more grep/glob/Read. Supports C/C++/Go/Python/Java/Rust/ASM with invocation graphs, conditional paths, concurrency analysis, data flow, FFI tracing, and 19 cgdb semantic tables. 49 MCP tools + 171 CLI commands. Use /Code2Database when the question involves code structure, call chains, impact analysis, concurrency, or data flow."
trigger: /Code2Database
---

# /Code2Database

**Scan once → persistent graph → query instead of grep.** One tool call answers questions that would otherwise require multiple grep/glob/Read across files.

## Query Priority Chain

When a question is asked, follow this priority:

```
1. Memory (recall) — did we answer this before? → fastest
2. Knowledge (know) — architecture-level invariants/constraints recorded?
3. Graph (query/describe/trace) — query the code graph
4. Source (describe --code) — read source as last resort
```

## When to Activate

- Any question about call relationships, chains, architecture, impact, concurrency
- When `code2db-out/` or `code2database.db` exists — query instead of grep
- `#ifdef` conditional paths, data races, FFI boundaries, data flow

## Quick Start

```bash
# 1. Scan + build (one-time)
python3 scripts/code2database_scanner.py scan --source /path --output ext.json
python3 scripts/code2database_builder.py build --extraction ext.json --outdir code2db-out/

# 2. Query (repeatable)
python3 scripts/code2database_builder.py describe --graph code2db-out/ --node bdev_start
python3 scripts/code2database_builder.py trace --graph code2db-out/ --from bdev_start --to spdk_app_start
python3 scripts/code2database_builder.py serve --graph code2db-out/  # MCP server (49 tools)
```

## Core Commands (20)

| Command | Purpose | Query Layer |
|---------|---------|-------------|
| `query` | Natural-language intent query | Memory→Knowledge→Graph |
| `describe` | Node details + source snippet | Graph→Source |
| `trace` | Call chain A→B with conditions | Graph |
| `impact` | What breaks if I change X? | Graph |
| `find` | Search by pattern (invariants, macros) | Graph |
| `flow` | Data/value/param flow | Graph |
| `concurrency` | Race/deadlock detection | Graph |
| `context` | Get context around a location | Graph |
| `build` | Scan + build graph | — |
| `update` | Incremental re-scan | — |
| `save` | Save Q&A to memory | Memory |
| `recall` | Search memory for past answers | Memory |
| `know` | Query knowledge base | Knowledge |
| `serve` | Start MCP server (49 tools) | All |
| `web-ui` | Interactive browser (cytoscape.js) | All |
| `tx-begin` | Start a transaction | Ops |
| `tx-commit` | Commit transaction (render+compile+lint) | Ops |
| `daemon` | Background auto-sync | Ops |
| `export` | HTML/Mermaid visualization | — |
| `health` | Graph freshness + profile health | — |

All 171 CLI commands remain accessible; the 20 above cover ~95% of agent workflows.

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

49 tools: 30 `code2database_*` (graph queries) + 19 `cgdb_*` (clang semantic layer).

## Constraints

- Start with `context_pack_micro` → `context_pack_lite` → `describe`/`trace`
- Only 7 labels: API_entry, thread_processor, callback_func, constructor, destructor, out_end, unknown_end
- Edge confidence: EXTRACTED / INFERRED / AMBIGUOUS
- DB writes require user confirmation
- Daemon freshness: check `daemon-status` before important queries
