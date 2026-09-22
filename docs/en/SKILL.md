---
name: Code2Database
description: "Turn a codebase into a queryable code database for the questions grep cannot answer in a few calls: reverse queries (callers, impact), global aggregates (domains, key paths, value flow), races, and cross-session memory. Scan once, query forever. C/C++/Go/Python/Java/Rust/ASM invocation graphs: conditional paths, concurrency analysis, data flow, FFI tracing, a 13-layer typed cgdb semantic store. One-shot lifecycle via the c2d umbrella (setup → session → ask → capture). 83 MCP tools + 120 visible CLI commands (umbrella families; 155 legacy spellings still parse). Use /Code2Database when the question involves code structure, call chains, impact analysis, concurrency, or data flow."
trigger: /Code2Database
---

# /Code2Database

**Scan once → persistent graph → answer reverse and global questions in one call.** Point lookups stay with grep/read; this database answers what grep cannot: who calls this, what breaks downstream, where a field flows, which paths are racy.

## One-Click Lifecycle — the `c2d` Umbrella

You do not need to memorize the 120-command visible surface. One command covers the whole workflow — learn 4 verbs:

| Verb | Purpose | Example |
|------|---------|---------|
| `c2d setup` | One-click ingest: env-check (fail fast) → scan → build → derived artifacts → exports | `c2d setup --source /path/to/project` |
| `c2d session` | One-shot context load: brief + memory digest + graph state + known-unknowns | `c2d session` |
| `c2d ask` | Ask any code question — the matched recipe runs the right read-only command sequence with aggregated output | `c2d ask --question "is bdev_start thread safe?"` |
| `c2d capture` | Save a Q&A into project memory | `c2d capture --question "..." --answer "..." --category bdev --author you` |
| `c2d freshen` | Freshness check → routes to full rebuild / daemon watch / per-file sync | `c2d freshen` |
| `c2d report` | Generate an artifact: design doc / diagnosis / mermaid / plantuml | `c2d report --kind design --module fs` |

- `c2d recipes` lists the question→command routing recipes (13 built in; detail view: `c2d recipes --recipe thread-safety`). `c2d ask` classifies `--question` against them, or run one directly with `--recipe NAME`; no match falls back to the single-command intent router.
- Every step is a normal read-only subcommand, echoed before it runs — preview any verb with `--dry-run`; `c2d ask` also accepts `--json` for a structured summary.
- The full command surface stays available for direct use (Tier-1 list below; intent index in `references/usage_reference.md`).

## ⚠ MANDATORY FIRST STEP — session-init

**Before any other C2D command, run `session-init` exactly once per AI session.**

```bash
python3 scripts/code2database_builder.py session-init   # --graph auto-discovers code2db-out/
# umbrella form: c2d session
```

This loads the complete project knowledge (brief — architecture rules, hard_rules, pitfalls, query_paths) + veteran memory digest + graph state + known-unknowns. **Without this step, the project's accumulated knowledge is invisible** — every subsequent query operates blind to mandatory rules and prior experience. Session-init is the only command that surfaces the full brief; `query` and `describe` only show FTS5-matched fragments. It works on knowledge/memory-only stores too — a directory without a graph needs `kb init` once, then every knowledge-base command runs standalone (see `/Code2Database-kb`).

## Query Priority Chain

When a question is asked, follow this priority:

```
1. Memory (recall / kb query) — did we answer this before? → fastest
2. Knowledge (know / kb query) — architecture-level invariants recorded?
3. Graph (query / describe / trace) — query the code graph
4. Source (describe --code) — read source as last resort
```

`kb query` is the unified FTS5+BM25 query surface across both memory
and knowledge stores. The `query` (Cypher) command automatically surfaces
top kb hits as a `_hints` field alongside graph rows.

## When to Activate

- Any question about call relationships, chains, architecture, impact, concurrency
- When `code2db-out/` or `code2database.db` exists — reverse/global questions go to the graph, point lookups to grep
- `#ifdef` conditional paths, data races, FFI boundaries, data flow

## When NOT to Use This Skill (grep wins)

This skill's edge is what grep cannot do in a few tool calls: reverse
queries (who calls this? what breaks downstream?), precomputed global
aggregates (domains, communities, key paths, value flow), cross-session
memory, and token-budgeted context packs. For point lookups, direct
grep/read is faster, always fresh, and needs no index:

- Where is X defined, or what does X call forward? → `rg` + read the file
- Reading one function or file → read the file
- Any literal string/symbol text search → `rg`

Rule of thumb: **if grep + read answers it in ≤3 tool calls, grep.**
Reach for this skill when the question is reverse, global, or
budget-bound — callers, impact radius, races, field flow, the domain
map, or session memory.

## Quick Start

```bash
# 0. First time on a project: one-click ingest (env-check fails fast;
#    re-runs are safe — graph artifacts rebuilt, memory/knowledge preserved)
python3 scripts/code2database_builder.py c2d setup --source /path/to/project

# 1. Session start (MANDATORY): brief + veteran memory digest + graph state + known-unknowns
python3 scripts/code2database_builder.py c2d session

# 2. Ask (repeatable) — recipes pick the right read-only commands
python3 scripts/code2database_builder.py c2d ask --question "is bdev_start thread safe?"
python3 scripts/code2database_builder.py c2d ask --recipe impact --target bdev_start

# 3. Capture valuable Q&A into memory (as needed)
python3 scripts/code2database_builder.py c2d capture --question "..." \
    --answer "..." --category bdev --author you

# Raw commands remain available for power use:
python3 scripts/code2database_builder.py describe --node bdev_start
python3 scripts/code2database_builder.py trace --from bdev_start --to spdk_app_start
```

## Core Commands (Tier-1)

The 21 Tier-1 commands cover ~95% of agent workflows. Task→command navigation: the intent index in `references/usage_reference.md`, or `c2d recipes` / `c2d verbs` at runtime.

- **Lifecycle**: `c2d`, `make`, `build`, `update`
- **Query**: `query` (Cypher; natural language: `intent-query`), `describe`, `trace`, `impact`, `context`, `find`, `flow`, `concurrency`
- **Memory & knowledge**: `session-init`, `kb query`, `memory save/search`, `brief` — kb governance commands live in the ops and kb sub-skills
- **Serving & ops**: `serve` (MCP, 83 tools), `tx`, `daemon`, `health`

Umbrella families (`tx begin`, `kb query`, `cgdb find-invokers`, ...) collapse 27 prefix families into the 120-command visible surface; the 155 legacy spellings still parse — full map in `references/usage_reference.md`.

Aliases: `describe`/`context` → describe-node, `trace` → trace-chain, `find` → find-invariants, `flow` → value-flow, `concurrency` → concurrency-risks, `save` → save-memory, `recall` → search-memory, `brief` → knowledge-brief, `health` → profile-health, `daemon` → daemon-status, `export` → export-mermaid.

All 275 CLI command spellings remain accessible (120 visible + 155 hidden legacy).

## Supported Languages

C/C++ | Go | Python | Java | Rust | ASM (6 + ASM, C/C++ share scanner)

## Extraction Backend

- `auto` (default) — clang when available, tree-sitter fallback
- `clang` — enables cgdb semantic layer (19 `cgdb_*` MCP tools)
- `tree-sitter` — no libclang dependency

## MCP Server

`serve --graph code2db-out/` for local stdio, or `--transport http --host 0.0.0.0 --port 8765 --token SECRET --read-only` for remote (Bearer auth, TLS, `--max-clients`, shared `memory/memory.db` across clients — experiences saved by one agent are immediately visible to others). 83 tools: 36 `code2database_*` (incl. `code2database_session_init`, `code2database_save_memory`, `code2database_kb_query`) + 19 `cgdb_*` (clang layer) + 28 design-report. Deploy templates (systemd + nginx) in `deploy/` — source repo only.

## Constraints

- **Session start**: run `session-init` FIRST — brief (mandatory rules/modes/pitfalls) + memory digest (veteran experience) + graph state with source-freshness warning + known-unknowns, in one prompt-ready output
- **Correction protocol**: `recall` before answering a project question; WRONG answer → `save --correct` (reshapes the most similar entry in place — no duplicate variant); MISSING → `save --category ... --author ... --symbol fn`; repeatedly-missed queries (known-unknowns in session-init) → capture the answer into memory
- **Symbol grounding**: memories about a specific function/type pass `--symbol <name>` (repeatable); `recall --symbol` filters by it; memories absorb symbols on merge and re-ground on `--correct`
- **Capture triggers**: save after (a) solving something non-trivial, (b) hitting a pitfall that cost real debugging time, (c) discovering a mandatory rule the brief lacks, (d) correcting a wrong answer, (e) answering a known-unknown; skip anything the graph answers in one query
- Run `kb rebuild-index` after `build`/`update` or memory/brief edits; govern memory with `memory manage --action split/merge/move/compact/categories` (compact auto-runs after every build); `brief suggest` proposes graduating memories into the brief; keep the brief lean (`brief validate` warns above 3000 chars — move overflow into memory)
- Start with `context_pack_micro` → `context_pack_lite` → `describe`/`trace`; never bulk-read output files
- Only 7 labels: API_entry, thread_processor, callback_func, constructor, destructor, out_end, unknown_end; every edge carries EXTRACTED / INFERRED / AMBIGUOUS confidence
- DB writes require user confirmation; check `daemon` before important queries (the daemon holds — does not sync — events during its startup grace window)
- **Accuracy caveats** (function-level concurrency, C++ virtual dispatch, `build update` cross-file edges, `--scan-subsystems`): see Behavior Notes in `references/usage_reference.md`
