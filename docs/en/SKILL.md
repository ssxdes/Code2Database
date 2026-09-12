---
name: Code2Database
description: "Turn a codebase into a queryable code database. Scan once, query forever — no more grep/glob/Read. Supports C/C++/Go/Python/Java/Rust/ASM with invocation graphs, conditional paths, concurrency analysis, data flow, FFI tracing, and 19 cgdb semantic tables. 83 MCP tools (55 base + 28 design-report) + 260 CLI commands (252 builder + 8 scanner). Use /Code2Database when the question involves code structure, call chains, impact analysis, concurrency, or data flow."
trigger: /Code2Database
---

# /Code2Database

**Scan once → persistent graph → query instead of grep.** One tool call answers questions that would otherwise require multiple grep/glob/Read across files.

## One-Click Lifecycle — the `c2d` Umbrella

You do not need to memorize the 260-command surface. One command covers the whole workflow — learn 4 verbs:

| Verb | Purpose | Example |
|------|---------|---------|
| `c2d setup` | One-click ingest: env-check (fail fast) → scan → build → derived artifacts → exports | `c2d setup --source /path/to/project` |
| `c2d session` | One-shot context load: brief + memory digest + graph state + known-unknowns | `c2d session` |
| `c2d ask` | Ask any code question — the matched recipe runs the right read-only command sequence with aggregated output | `c2d ask --question "is bdev_start thread safe?"` |
| `c2d capture` | Save a Q&A into project memory | `c2d capture --question "..." --answer "..." --category bdev --author you` |
| `c2d freshen` | Freshness check → routes to full rebuild / daemon watch / per-file update | `c2d freshen` |
| `c2d report` | Generate an artifact: design doc / diagnosis / html / mermaid / plantuml | `c2d report --kind design --module fs` |

- `c2d recipes` lists the question→command routing recipes (13 built in; detail view: `c2d recipes --recipe thread-safety`). `c2d ask` classifies `--question` against them, or run one directly with `--recipe NAME`; no match falls back to the single-command intent router.
- Every step is a normal read-only subcommand, echoed before it runs — preview any verb with `--dry-run`; `c2d ask` also accepts `--json` for a structured summary.
- The full command surface stays available for direct use (Core Commands below).

## ⚠ MANDATORY FIRST STEP — session-init

**Before any other C2D command, run `session-init` exactly once per AI session.**

```bash
python3 scripts/code2database_builder.py session-init   # --graph auto-discovers code2db-out/
# umbrella form: c2d session
```

This loads the complete project knowledge (brief — architecture rules, hard_rules, pitfalls, query_paths) + veteran memory digest + graph state + known-unknowns. **Without this step, the project's knowledge底蕴 is invisible** — every subsequent query operates blind to mandatory rules and prior experience. Session-init is the only command that surfaces the full brief; `query` and `describe` only show FTS5-matched fragments.

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
python3 scripts/code2database_builder.py c2d setup --source /path/to/project
#   (= make — stage 1 env-check BEFORE any build step: missing
#     compile_commands.json / libclang / tree-sitter grammars are
#     reported up front, never mid-build; stage 2: scan -> build ->
#     derived artifacts (value-flow, data-dep, #ifdef signals, FFI,
#     brief, kb index, embeddings) -> exports -> profile-health;
#     --check = env-check only; re-runs are safe: graph artifacts
#     rebuilt, memory/knowledge preserved)

# 1. Session start (MANDATORY): load the full project context
python3 scripts/code2database_builder.py c2d session
#   (= session-init — brief + veteran memory digest + graph state +
#     unanswered questions; if no brief yet: brief-extract to
#     bootstrap, then curate with brief-update)

# 2. Ask (repeatable) — recipes pick the right read-only commands
python3 scripts/code2database_builder.py c2d ask --question "is bdev_start thread safe?"
python3 scripts/code2database_builder.py c2d ask --question "what breaks if I change util_sum?"
python3 scripts/code2database_builder.py c2d ask --recipe impact --target bdev_start

# 3. Capture valuable Q&A into memory (as needed)
python3 scripts/code2database_builder.py c2d capture --question "..." \
    --answer "..." --category bdev --author you

# Raw commands remain available for power use:
python3 scripts/code2database_builder.py describe --node bdev_start
python3 scripts/code2database_builder.py kb-query --query "bdev register"
python3 scripts/code2database_builder.py trace --from bdev_start --to spdk_app_start
python3 scripts/code2database_builder.py serve    # MCP server (83 tools)
```

## Core Commands (27)

The direct command surface for power use — the `c2d` umbrella above wraps the most common paths.

| Command | Purpose | Query Layer |
|---------|---------|-------------|
| `c2d` | One-click umbrella over the whole lifecycle: `setup` (ingest) → `session` (context) → `ask` (question → read-only command recipe) → `capture` (save memory); plus `freshen` (freshness routing) and `report` (design/diagnose/diagram artifacts); `c2d recipes` lists the routing table | — |
| `query` | Cypher-subset query (`MATCH (n:Function) WHERE n.name='foo' RETURN n.id`). For natural-language, use `intent-query` | Graph |
| `kb-query` | Unified FTS5+BM25 across memory + knowledge | Memory+Knowledge |
| `describe` | Node details + source snippet + memory_refs + knowledge_refs (alias for `describe-node`) | Graph→Source |
| `trace` | Call chain A→B with conditions (alias for `trace-chain`) | Graph |
| `impact` | What breaks if I change X? | Graph |
| `find` | Find invariants by pattern (`--var`/`--value`/`--kind`) (alias for `find-invariants`). For macros, use `find-macros` | Graph |
| `flow` | Value flow (DATA_FLOW/RETURN_FLOW edges) (alias for `value-flow`). For data deps use `data-dep`; for params use `param-flow` | Graph |
| `concurrency` | List concurrency risk pairs (function-level) (alias for `concurrency-risks`). For race detection use `detect-races` | Graph |
| `context` | Describe a node by ID/name (alias for `describe-node`). Not location-based | Graph |
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
| `daemon` | Show daemon status (alias for `daemon-status`; to start sync use `daemon-start`) | Ops |
| `health` | Profile health score (requires `--source`) (alias for `profile-health`). For graph freshness use `daemon-status` or `session-init` | — |

All 260 CLI commands remain accessible; the 27 above cover ~95% of agent workflows. Additional short aliases (not listed above): `export` → `export-mermaid`.

## Supported Languages

C/C++ | Go | Python | Java | Rust | ASM (6 + ASM, C/C++ share scanner)

## Extraction Backend

- `auto` (default) — clang when available, tree-sitter fallback
- `clang` — enables cgdb semantic layer (19 `cgdb_*` MCP tools)
- `tree-sitter` — no libclang dependency

## MCP Server

```bash
# Local (stdio) — for Claude Desktop, Cursor local, etc.
python3 scripts/code2database_builder.py serve --graph code2db-out/

# Remote (HTTP) — for cross-network access, shared memory/knowledge
python3 scripts/code2database_builder.py serve --graph code2db-out/ \
    --transport http --host 0.0.0.0 --port 8765 \
    --token my-secret --read-only
```

83 tools: 36 `code2database_*` (incl. `code2database_session_init` one-shot session context, `code2database_save_memory` for MCP-side experience accumulation, `code2database_kb_query` for unified memory+knowledge search) + 19 `cgdb_*` (clang semantic layer) + 28 design-report.

HTTP transport (`--transport http`) enables remote MCP clients to access your code graph and shared memory/knowledge base over the network. All 83 tools are available, and multiple clients share the same `memory/memory.db` — experiences saved by one agent are immediately visible to others. Use `--token` for Bearer auth and `--read-only` to disable write tools on public endpoints. The `deploy/` directory (systemd + nginx configs) exists only in the source repo — clone it to access deployment templates.

## Constraints

- **Session start**: run `session-init` (alias `init`) FIRST — brief (mandatory rules/modes/pitfalls) + memory digest (veteran experience) + graph state with source-freshness warning (rebuild before trusting a STALE graph) + known-unknowns, in one prompt-ready output
- **Correction protocol**: before answering a project question, `search-memory` first; if an answer is WRONG use `save-memory --correct` (reshapes the most similar entry in place — no duplicate variant); if MISSING use `save-memory --category ... --author ... --symbol fn`; if a query repeatedly misses (known-unknowns in session-init), capture the answer into memory
- **Symbol grounding**: when a memory is about a specific function/type, pass `--symbol <name>` (repeatable) — the web UI shows that Q&A on the symbol's node page, and `search-memory --symbol` / `code2database_memory_search(symbol=)` filter by it. Memories absorb symbols on merge and re-ground on `--correct`
- **Capture triggers** (when to save-memory, so accumulation doesn't depend on luck): after (a) solving a non-trivial problem — the resolution path IS the answer; (b) hitting a pitfall that cost real debugging time; (c) discovering a mandatory rule/constraint the brief doesn't capture yet; (d) correcting a wrong answer (`--correct`); (e) answering a known-unknowns query from session-init. Skip anything the graph answers in one query.
- Run `kb-rebuild-index` after `build`/`update` or after memory/brief edits
- Memory is a shared accumulating store (memory.db): save with `--category path/to/topic` + `--author`; govern with `manage-memory --action split/merge/move/compact/categories` (compact merges near-duplicate roots automatically after every build); `brief-suggest` proposes graduating high-weight memories into the brief
- Knowledge (brief.json) must stay lean: `brief-validate` warns above 3000 chars; move overflow into memory instead
- Start with `context_pack_micro` → `context_pack_lite` → `describe`/`trace`
- Only 7 labels: API_entry, thread_processor, callback_func, constructor, destructor, out_end, unknown_end
- Edge confidence: EXTRACTED / INFERRED / AMBIGUOUS
- DB writes require user confirmation
- Daemon freshness: check `daemon-status` before important queries; note the daemon holds (does not sync) events during its startup grace window (`startup_grace_active`)
- `update`/`merge`/`sync` commands require in-memory nx.DiGraph. On large projects
  (>=50K functions), `_load_full_graph` returns LazySQLiteGraph (read-only SQLite view).
  These commands will print a friendly error directing to `daemon-start` or `build`.
  Use `daemon-start` for incremental sync (designed for SQLite-backed large graphs), or
  `build-update --source SRC --graph DIR` for a precise per-file update of the
  SQLite graph (content-hash detection + #include closure; format-only edits
  are skipped structurally).
- **`build-update` cross-file edge limitation**: `build-update`
  rescans only changed files. When a function is renamed or deleted in file A,
  edges from *other* files that called A's old function are deleted (via
  `_delete_legacy_rows`) but **not recreated** — the calling files aren't
  rescanned, so the new function ID (which embeds the file path) won't match.
  Cross-file call edges pointing into the changed file are permanently lost
  until a full `build` is run. For projects with frequent cross-file refactors,
  prefer `daemon-start` (which handles this via the daemon's transactional
  sync) or schedule periodic full builds.
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
- **C++ virtual dispatch not resolved**: tree-sitter C++ has no separate
  `virtual_call` node type — virtual method calls are parsed as regular
  `call_expression` and only resolve to the statically-typed method, not to
  dynamic dispatch targets. C-style ops-table vtable dispatch IS handled
  (`vtable_dispatch` edges connect dispatch functions to registered targets).
  For C++ class hierarchies with `virtual`/`override`, use `concurrency-analyze`
  or manually inspect override sets.
- **FFI edges require `make` or explicit `ffi-detect`**: the standalone `build`
  command produces the invocation graph but does NOT run FFI detection.
  Cross-language FFI bridges (Python ctypes, Go cgo, Rust extern "C") are
  detected by `ffi-detect` (called automatically in the `make` pipeline) or
  can be run separately after `build`. If you use `build` instead of `make`
  on a multi-language project, run `ffi-detect --apply` afterward to add
  FFI bridge edges.
- **`--scan-subsystems` drops cross-subsystem edges**: subsystem filtering
  restricts the scan to top-level directories (e.g. `--scan-subsystems fs,block`).
  Shared header files in `include/` and calls from the scanned subsystem into
  unscanned subsystems become phantom external nodes — the call edge is
  preserved but the target node is unresolved. For full call-graph fidelity
  across subsystem boundaries, omit `--scan-subsystems` or include the
  `include` directory in the filter list.
