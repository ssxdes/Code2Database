---
name: Code2Database
description: "Turn a codebase into a queryable code database. Scan once, query forever — no more grep/glob/Read. Supports C/C++/Go/Python/Java/Rust/ASM with invocation graphs, conditional paths, concurrency analysis, data flow, FFI tracing, and 19 cgdb semantic tables. 53 MCP tools + 196 CLI commands. Use /Code2Database when the question involves code structure, call chains, impact analysis, concurrency, or data flow."
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
# 1. Scan + build (one-time)
python3 scripts/code2database_scanner.py scan --source /path --output ext.json
python3 scripts/code2database_builder.py build --extraction ext.json --outdir code2db-out/

# 2. Build unified KB index (after first build, or after memory/knowledge changes)
python3 scripts/code2database_builder.py kb-rebuild-index --graph code2db-out/

# 3. Query (repeatable)
python3 scripts/code2database_builder.py describe --graph code2db-out/ --node bdev_start
python3 scripts/code2database_builder.py kb-query --graph code2db-out/ --query "bdev register"
python3 scripts/code2database_builder.py trace --graph code2db-out/ --from bdev_start --to spdk_app_start
python3 scripts/code2database_builder.py serve --graph code2db-out/  # MCP server (53 tools)
```

## Core Commands (24)

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
| `build` | Scan + build graph | — |
| `update` | Incremental re-scan | — |
| `save-memory` | Save Q&A to memory (alias: `save`) | Memory |
| `search-memory` | Search memory for past answers (alias: `recall`) | Memory |
| `knowledge-query` | Query knowledge base (alias: `know`) | Knowledge |
| `kb-rebuild-index` | Rebuild FTS5 index from filesystem | Memory+Knowledge |
| `kb-cluster` | Cluster similar items + link principles | Memory+Knowledge |
| `kb-known-unknowns` | List unanswered queries (feedback loop) | Memory+Knowledge |
| `kb-audit` | Knowledge audit (citations, staleness, confidence) | Memory+Knowledge |
| `kb-forget` | Immediately delete a memory/knowledge item | Memory+Knowledge |
| `serve` | Start MCP server (53 tools) | All |
| `web-ui` | Interactive browser (cytoscape.js) | All |
| `tx-begin` | Start a transaction | Ops |
| `daemon` | Background auto-sync | Ops |
| `health` | Graph freshness + profile health | — |

All 196 CLI commands remain accessible; the 24 above cover ~95% of agent workflows.

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

53 tools: 34 `code2database_*` (incl. new `code2database_kb_query` for unified memory+knowledge search) + 19 `cgdb_*` (clang semantic layer).

## Constraints

- Run `kb-rebuild-index` after `build`/`update` or after manual memory/knowledge edits
- Start with `context_pack_micro` → `context_pack_lite` → `describe`/`trace`
- Only 7 labels: API_entry, thread_processor, callback_func, constructor, destructor, out_end, unknown_end
- Edge confidence: EXTRACTED / INFERRED / AMBIGUOUS
- DB writes require user confirmation
- Daemon freshness: check `daemon-status` before important queries
