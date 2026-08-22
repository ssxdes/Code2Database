---
name: Code2Database
description: "Turn a codebase into a queryable code database. Scan C/C++/Go/Python/Java/Rust/ASM once and generate a persistent directed invocation graph with invocation ordering, conditions, conditional compilation paths (#ifdef), concurrency analysis, data race detection, field-level access tracking, confidence classification, and evidence traces. Query with one tool call instead of grep/glob/Read. Capabilities: dual clang + tree-sitter backend (auto/clang/tree-sitter via --extraction-backend; libclang recommended but NOT required), commit-based provenance with git hash verification, Cypher-subset queries, value flow / DATA_FLOW edges, lock-held region analysis, Z3 path feasibility, cross-function data dependencies, invariant extraction (preconditions/postconditions/loop_invariants/state_machine), LLM auto-semantic enhancement with confidence-threshold auto-write, transactional updates (WAL + snapshots + fcntl locks), cross-language FFI tracing (Python ctypes / Go cgo / Rust extern C), interactive Web UI, BUG benchmark, profile health + auto-evolution with git/svn HEAD binding, doc-code dual-source truth alignment, a background daemon with inotify + Unix socket API, and a cgdb (code graph database) layer with 19 cgdb_* MCP tools for direct clang-derived semantic table queries (types, CFG, data flow, ops bindings, sync primitives, config predicates, time-travel versions). 122 CLI commands organized into 3 sub-skills: this core (15 Tier-1 high-weight commands shown below), /Code2Database-analysis (deep semantic analysis), /Code2Database-ops (graph editing + ops). 49 MCP tools (30 code2database_* + 19 cgdb_*). Use when user asks about code invocation relationships, function invocation chains, invocation ordering, conditional invocation paths, conditional compilation effects, architecture domain structure, API entry points, external endpoint classification, concurrency risks, data races, invariants, FFI boundaries, doc-code consistency, or wants to generate/visualize an invocation graph — especially when code2db-out/ exists. Not for: writing unit tests, simple single-file lookups, or general questions unrelated to invocation relationships."
trigger: /Code2Database
sub_skills: ["Code2Database-analysis", "Code2Database-ops"]
---

# /Code2Database

**From read-only code text to a one-click query code database.** Scan C/C++/Go/Python/Java/Rust/ASM codebases and generate directed invocation graphs with invocation ordering, conditions, and conditional compilation paths. One scan → persistent graph → surgical queries → fewer tool calls → faster answers.

This is the **core** sub-skill — always loaded. It covers **build + browse** workflows (15 high-weight commands shown in Quick Reference below). For deep semantic analysis (concurrency, data flow, invariants, FFI, provenance, path feasibility, cgdb tables), activate `/Code2Database-analysis`. For graph editing, transactions, daemon, profile/doc-code, exports, plugins, memory, activate `/Code2Database-ops`. See **Sub-skill Activation** section below.

## Supported Languages

C(.c .h .cpp .cc .cxx .hpp) | Go(.go) | Python(.py .pyw) | Java(.java) | Rust(.rs) | ASM(.s .S .asm)

Language is auto-detected by file extension; can be forced with `--lang`. ASM uses regex-based scanning (no tree-sitter grammar).

## Extraction Backend

Code2Database supports a **dual backend** for C/C++ extraction:

- `auto` (default) — uses clang when libclang is installed, falls back to tree-sitter otherwise
- `clang` — force the clang backend (enables cgdb layer; requires `pip install libclang==17.0.6`)
- `tree-sitter` — force the tree-sitter backend (no libclang dependency)

**libclang is recommended, NOT required.** Tree-sitter-only mode remains fully functional — you can scan, build, and query every supported language. The clang backend additionally populates the cgdb (code graph database) layer with typed semantic tables (CFG, data flow, ops bindings, sync primitives, config predicates) exposed via 18 `cgdb_*` MCP tools.

```bash
# Tree-sitter only (no libclang)
python3 scripts/code2database_scanner.py scan --source /path --extraction-backend tree-sitter

# Dual backend (auto — uses clang if available)
python3 scripts/code2database_scanner.py scan --source /path --extraction-backend auto

# Force clang backend (enables cgdb layer)
python3 scripts/code2database_scanner.py scan --source /path --extraction-backend clang
```

## When to Activate

- User enters `/Code2Database` or asks about invocation relationships/chains/ordering/conditional paths
- Architecture/concurrency questions when `code2db-out/` directory exists
- Bug locating, architecture review, impact analysis involving multi-function interactions
- Security vulnerability analysis (crash reports, null-ptr-deref, race conditions)
- **Any question that would otherwise require grep/glob/Read across multiple files** — if a code graph exists, query it instead
- Questions about **why** a call happens (conditions, #ifdef), **whether** two chains can race, or **who** touches a field — these are answerable from the graph, not from raw source
- Invariant queries (preconditions/postconditions/state_machine) → `find-invariants`
- Cross-language FFI tracing (Python→C, Go→C, Rust→C) → `ffi-trace`
- Path feasibility under constraints → `path-feasible`
- Doc-code consistency ("doc says X, code does Y") → `doc-code-check`
- Daemon freshness check before important queries → `daemon-status` / `daemon-wait-sync`
- Profile health / new callback pattern detection → `profile-health` / `profile-evolve`
- Declarative graph queries (Cypher-subset) → `query "MATCH ... WHERE ... RETURN ..."`
- Commit-level provenance ("which commit introduced this bug?") → `blame-node` / `describe-commit`

## Why a Code Database, Not Just a Call Graph

Most code-graph tools stop at "function calls function." Code2Database goes deeper:

| You ask | The graph answers |
|---------|-------------------|
| "Which functions access `task_struct->pid`?" | `field-access` — across the whole graph, no grep |
| "Can these two invocation chains race?" | `concurrency-analyze` — thread model + shared state + lock analysis |
| "What breaks if I change this function?" | `blast-radius` — affected APIs, tests, domains |
| "How did execution reach this crash point?" | `reverse-trace` — all paths from entry points to the crash |
| "Which calls exist only under `CONFIG_SMP`?" | `extract-signals` + `resolve-chain --bindings` |
| "What conditions gate this call?" | Every edge carries `call_condition` (if/switch/#ifdef/ternary) |
| "Is this edge certain?" | Every edge tagged `EXTRACTED` / `INFERRED` / `AMBIGUOUS` + source |
| "What invariants does this function enforce?" | `find-invariants` — preconditions / postconditions / loop_invariants / state_machine |
| "Which Python function calls into C via ctypes?" | `ffi-trace` — cross-language FFI tracing (Python/Go/Rust → C) |
| "Is this invocation path feasible under these constraints?" | `path-feasible` — Z3 SMT solver for sound feasibility |
| "Is the doc consistent with the code?" | `doc-code-check` — detects return-value / param / signature / stale-doc mismatches |
| "Is the daemon up to date?" | `daemon-status` — last sync time, pending events, stale nodes |
| "Which commit introduced this bug?" | `blame-node` / `describe-commit` — commit-level provenance (git hash, not timestamp) |
| "Which functions hold this lock?" | `lock-coverage` — precise lock-held ranges with event-stream + char positions |
| "Where does this NULL come from?" | `value-flow` — parameter→return-value propagation across functions |
| "Is my profile still valid?" | `profile-health` — 0-100 score across 7 categories |

This is the gap between an *invocation graph* and a *code database you can ask real engineering questions of*.

## Workflow

```
[0] Check prerequisites → [0b] Auto-detect project scale → [1] AST scan → [2] Semantic enhancement
    → [2b] Fn-ptr/vtable resolution → [3] Build graph → [3b] Domain normalization
    → [3c] Endpoint classification → [3d] Consistency validation → [4] Query/answer
                                                                      ↑
                                            [Daemon] auto-refresh loop
                                                  ↓ wrapped in
                                            [Transactional Sync] snapshot + WAL
                                                  ↓ touches
                                            Output files + .code2database_freshness.json
```

ASM files use regex-based scanning (not tree-sitter) since no reliable tree-sitter grammar exists. Supports NASM x86_64, GNU as `.S`, and ARM `bl`/`blr` instructions.

The build step also runs: lock-coverage event-stream extraction, invariant extraction, FFI binding detection, doc-code alignment, and commit-provenance binding to `git/svn HEAD`. The daemon monitors source files post-build and pushes changes through transactions.

## Quick Reference — Tier 1 Commands (high-weight)

The 15 commands below cover the build + browse workflow. They are the commands you'll reach for ~80% of the time. For deeper analysis or operations, activate a sub-skill (see **Sub-skill Activation** below).

| Command | Purpose |
|---------|---------|
| `scan` | AST scan source code → `.code2database_extraction.json` |
| `auto-profile` | Auto-generate project profile |
| `build` | Build invocation graph from extraction data |
| `update` | Incremental update (rescan changed files) |
| `quick-update` | Fast update with auto-threshold for stale ratio |
| `explore-flow` | One-shot natural language query → relevant nodes + paths |
| `describe-node` | Tiered node description (brief/standard/full); surfaces `doc_code_mismatches`, `preconditions`, `postconditions`, `loop_invariants`, `state_machine`, `auto_fill_request` |
| `search` | Keyword search over nodes |
| `load` | Load graph overview/summary |
| `get-code-snippet` | Retrieve source code for a node (`--persist` writes to body_text, **requires user confirmation**) |
| `trace-chain` | Forward trace path from A to B |
| `reverse-trace` | Reverse BFS from crash point to entry points |
| `key-paths` | Auto-extract critical execution paths |
| `query` | Cypher-subset query (MATCH/WHERE/RETURN) |
| `daemon-status` | Get daemon status (pid, last_sync, pending events, stale nodes) — freshness check |

## Sub-skill Activation

For commands beyond the Tier-1 set above, activate one of the two on-demand sub-skills. Each sub-skill has its own SKILL.md with a Tier-1 table + routing table for its own command group.

### `/Code2Database-analysis` — Deep Semantic Analysis

Activate when the user asks about:

- **Concurrency safety / data races** → `concurrency-analyze`, `detect-races`, `lock-coverage`, `happens-before`, `memory-ordering`, `who-locks`
- **Value / parameter flow** → `value-flow`, `param-flow`, `data-dep`, `data-lifecycle`, `io-path`
- **Path feasibility under constraints** → `path-feasible`, `resolve-chain`, `extract-signals`
- **Invariants** → `extract-invariants`, `find-invariants`, `apply-invariants`
- **FFI boundaries** → `ffi-detect`, `ffi-list`, `ffi-trace`, `ffi-types`
- **Commit-level provenance** → `blame-node`, `describe-commit`, `node-history`, `graph-provenance`, `find-commits`
- **Resource lifecycle** → `who-allocates`, `who-frees`, `unbalanced-alloc-free`
- **Field-level access** → `field-access`, `blast-radius`, `impact`, `neighbors`, `path`, `diff-chains`
- **Direct cgdb table queries** (clang backend) → 19 `cgdb_*` MCP tools (search_symbols, find_invokers, find_invoked, get_definition, get_function_body, get_source, get_struct_layout, find_type_definition, find_ops_impls, find_cfg_paths, find_data_flow, find_aliases, find_lock_held_calls, check_race_condition, find_configs_for, find_nodes_under_config, index_status, time_travel_query, list_versions)

**Hand-off phrase**: *"Activate `Code2Database-analysis` sub-skill."* Then load `~/.claude/skills/Code2Database-analysis/SKILL.md`.

### `/Code2Database-ops` — Graph Editing + Operations

Activate when the user asks about:

- **Safe graph editing** → `tx-begin`/`tx-commit`/`tx-rollback`/`tx-status`, `tx-snapshot`/`tx-restore`/`tx-list-snapshots`/`tx-replay-wal`, `update-node`, `update-edge`, `patch-profile`, `classify-endpoints`, `auto-enhance`, `batch-confirm`, `rollback`, `fill-request`, `add-semantic-edges`, `semantic-status`, `audit-log`
- **Keeping the graph up to date** → `daemon-start`/`stop`/`force-refresh`/`pause`/`resume`/`wait-sync`/`logs`/`reload`/`list-projects`, `watch`, `sync`, `merge`, `light-scan`, `patch-from-diff`, `patch-from-git`, `install-hook`, `export-changes`, `merge-changes`
- **Profile & doc-code** → `profile-health`, `profile-evolve`, `profile-bind-version`, `doc-code-check`, `doc-mark-stale`, `doc-alignment-report`, `doc-signature-diff`
- **Graph versioning** → `graph-history`, `graph-diff`, `graph-record-version`
- **Memory management** → `save-memory`, `search-memory`, `manage-memory`, `memory-health`, `validate-memory`
- **Exports / plugins / benchmark** → `export-html`, `export-obsidian`, `web-ui`, `plugins`, `validate-plugin`, `bug-benchmark`
- **Embeddings (experimental)** → `embeddings-build`, `embeddings-search`
- **MCP server** → `serve` (48 tools: 30 `code2database_*` + 18 `cgdb_*`)

**Hand-off phrase**: *"Activate `Code2Database-ops` sub-skill."* Then load `~/.claude/skills/Code2Database-ops/SKILL.md`.

### When NOT to hand off

If the question is a simple call-relationship lookup ("who calls X?", "what does Y call?", "show me the path from A to B"), answer it from the Tier-1 commands above — no sub-skill activation needed. Sub-skills are for **specialized** workloads.

## MCP Server

```bash
python3 scripts/code2database_builder.py serve --graph code2db-out/
```

Exposes **48 MCP tools** (30 `code2database_*` + 18 `cgdb_*`) over stdio transport for real-time LLM agent queries. All 48 tools are accessible regardless of which sub-skill is active — MCP is separate from skill layer activation.

## Core Workflow Steps

### Step 0 — Prerequisites

If `code2db-out/code2database_master.json` exists and this is not a rebuild, skip to Step 4.

If no profile exists, run `auto-profile` first. Install dependencies if needed (networkx, tree-sitter language bindings; ASM needs no extra dependency; optional `z3-solver` for sound path feasibility).

### Step 1 — Scan

```
python3 "$SKILL_DIR/scripts/code2database_scanner.py" scan \
  --source SOURCE_PATH --lang auto \
  --output code2db-out/.code2database_extraction.json
```

Scanner extracts: functions, edges, domains, fn_ptr_calls, struct_types, includes. ASM scanner uses regex for call/jmp/bl/blr. Inline `__asm__` blocks in C/C++ are also extracted (INFERRED confidence).

### Step 2 — Semantic Enhancement & Fn-ptr Resolution

Supplement AST gaps (function pointers, cross-file calls, conditional calls). Fn-ptr/vtable dispatch resolution runs automatically during build. See `references/semantic_enhancement.md`.

### Step 3 — Build

```
python3 "$SKILL_DIR/scripts/code2database_builder.py" build \
  --extraction code2db-out/.code2database_extraction.json \
  --outdir code2db-out/ [--storage json|sqlite|auto]
```

Domain normalization, consistency validation, lock-coverage extraction, invariant extraction, FFI detection, doc-code alignment, and commit-provenance binding are all built into the build step. For large projects (>50K functions), `--storage auto` selects SQLite.

### Step 4 — Query

Use **global-to-local** mode: read `context_pack_lite` first, then use tiered queries. See **Query Routing Guide** below.

## Query Routing Guide

| Question Type | Recommended Query Path | Notes |
|---------------|----------------------|-------|
| Overall architecture | context_pack_micro → context_pack_lite → CODE2DATABASE_SUMMARY.md | Start with micro (~200t), then lite for details |
| Specific function/module | explore-flow → describe-node → get-code-snippet | One-shot locate → details → source code |
| Call chain tracing | trace-chain → resolve-chain | Complete path A→B with condition annotations |
| Concurrency risks | concurrency-risks → concurrency-analyze → describe-node --context | Global risks → specific pairs → function details |
| Data races | detect-races → field-access | Detect cross-thread races → field-level access |
| Lock coverage | lock-coverage → concurrency-analyze | Precise lock-held ranges → pair-wise safety analysis |
| Crash debugging | reverse-trace → trace-chain | Reverse from crash point → forward specific chains |
| Impact analysis | blast-radius / impact --lite | Affected APIs/tests/domains |
| Conditional compilation | extract-signals → resolve-chain / diff-chains | Signal map → resolve or diff paths |
| Data lifecycle | data-lifecycle → field-access | Resource alloc-use-release → field accessors |
| Value flow / NULL source | value-flow → param-flow → describe-node | Trace parameter→return-value propagation → parameter flow → receiver details |
| Data dependency | data-dep → field-access | Cross-function reader/writer scan → field-level access |
| Path feasibility | trace-chain → path-feasible | Get the path → check SMT feasibility under constraints (Z3 or heuristic fallback) |
| Code change management | quick-update / install-hook | One-shot update; hook for auto-update |
| Knowledge validation | knowledge-validate | Check knowledge file quality |
| Knowledge query | knowledge-query → memory search | Search knowledge first, then memory |
| Security/vulnerability | reverse-trace → detect-races → concurrency-analyze | Crash paths → races → concurrency details |
| Fn-ptr dispatch tracing | explore-flow → describe-node --context | Locate dispatch sites → view implementations |
| I/O path analysis | io-path | Trace I/O paths from a function |
| Invariant reasoning | find-invariants → describe-node --invariants | Match invariants by pattern → review preconditions/postconditions/loop_invariants/state_machine |
| Cross-language FFI | ffi-detect → ffi-list → ffi-trace → ffi-types | Detect bindings → list sites → trace cross-language chain → inspect type marshalling |
| Doc-code mismatch | doc-code-check → doc-signature-diff → doc-mark-stale | Detect mismatch → diff signatures → mark doc stale if needed |
| Daemon health | daemon-status → daemon-wait-sync → daemon-logs | Check status → block until sync done → inspect logs if anomalous |
| Profile health | profile-health → profile-evolve → profile-bind-version | Score 7 categories → apply EXTRACTED suggestions → rebind to HEAD |
| Commit provenance | blame-node → describe-commit → node-history | Find introducing commit → show all changes → history of a node |
| Declarative graph query | query "MATCH ... WHERE ... RETURN ..." | Cypher-subset; for one-off structured queries not covered by specialized commands |

**Do NOT directly Read output files (.json/.md). Always use query commands.** They return precise, compact results; direct reads waste tokens.

## Constraints

- **Query priority**: context_pack_micro → context_pack_lite → explore-flow → knowledge_pack_lite → memory_pack_lite → describe-node
- **Prefer describe-node** for function info — no need to read source files
- **For branch decisions** use `resolve-chain` + bindings or check scenarios
- **Parameter flow tracing** uses `describe-node`'s `param_flow` field
- **Global-to-local mode**: always start with micro/lite packs before drilling down
- When referencing functions, include: name, source file:line, architecture domain
- When tracing chains, walk the **complete path** — never skip intermediate nodes
- Conditional calls must be annotated with `call_condition`
- API_entry functions must include `api_constraints`
- out_end/unknown_end must have `external_desc` or note needing manual confirmation
- Uncertain information must be marked speculative, suggest user verification
- Read no more than 10 source files per query
- **Do not pre-load** profile templates (`config/profiles/`) or reference docs — read on demand only
- Only seven labels supported (see `references/label_rules.md`), no custom additions
- Label identification: AST heuristics → LLM supplement → User confirmation
- External code domains auto-detected (vendor/third_party/contrib) → `external_*`
- Callback registration points must have edges to actual callback functions
- Endpoint classification follows complete pipeline (see `references/endpoint_pipeline.md`)
- Cross-skill collaboration uses capability keyword dynamic matching (see `references/cross_skill_collaboration.md`)
- Edge confidence must be annotated (EXTRACTED/INFERRED/AMBIGUOUS + source)
- Plugin-added edges cannot masquerade as EXTRACTED
- **Fn-ptr edges mandatory**: every fn_ptr call must have at least a `callback_dispatch` edge
- **Conditional expression calls auto-extracted**: calls in if/while/for/switch/ternary conditions are auto-extracted with `call_condition` annotations
- **Consistency validation**: built into the build step; re-run build if inconsistencies found
- **ASM scanner uses regex** (not tree-sitter); ASM edges always INFERRED confidence
- Do not propose fixes before finding the root cause
- Must verify after sync/update
- **Auto-detect large-project mode** from function count; no manual `--large-project` needed
- **Domain normalization**: always apply domain_rules and deduplication
- **Transactional writes**: wrap multi-step DB modifications in `tx-begin`/`tx-commit`. `patch-from-diff`/`patch-from-git` already do this by default; use `--no-transaction` to bypass. Use `tx-rollback` to abort; `tx-replay-wal` for crash recovery
- **Daemon freshness**: call `daemon-status` before important queries; if `syncing` or `pending_events > 0`, call `daemon-wait-sync` to block until sync completes. Circuit breaker triggers bulk rebuild above 1000 events/minute
- **Doc-code alignment**: `describe-node` surfaces `doc_code_mismatches` — if non-empty, `semantic_desc` may be unreliable; consult `body_text` and consider `doc-mark-stale` until docs are re-extracted
- **Invariants confidence**: never apply AMBIGUOUS invariants; INFERRED invariants **require user confirmation** before `apply-invariants`. State machine extraction threshold is `assign_counts[state_var] >= 1`
- **FFI tracing**: requires BOTH source and target language scanners to detect a binding site (e.g., Python ctypes source + C target). Type marshalling may be lossy — `ffi-types` flags lossy conversions
- **Profile evolution**: `profile-evolve --apply` only applies EXTRACTED-confidence suggestions; INFERRED **require user confirmation**. Run `profile-bind-version` after evolution to bind to git/svn HEAD
- **Commit provenance**: every node/edge carries `commit_meta.source_commit` (git/svn hash). Verify with `git show <hash>`, not timestamps. `blame-node` finds the introducing commit; `node-history` shows evolution
- **Path feasibility**: `path-feasible` is sound only with Z3 installed (`pip install z3-solver`); without Z3, heuristic fallback may produce false positives — flag results as provisional
- **Value flow**: DATA_FLOW edges trace parameter→return-value propagation; if a node lacks DATA_FLOW edges, the chain is broken — do not infer flow without evidence

## Database Write Constraint (Important)

When LLM executes any command that modifies the code graph database, it **MUST get user confirmation first** before writing. This prevents LLM hallucinations from polluting the database, violating the core principle "content may be missing but must be accurate."

**Commands requiring user confirmation** (default: y/N prompt):

- `update-node` — LLM-driven incremental node attribute supplement
- `update-edge` — LLM-driven incremental edge attribute supplement
- `patch-profile` — LLM-driven incremental auto-profile calibration
- `apply-semantics` — Apply LLM-filled semantic descriptions to graph
- `classify-endpoints` — Apply LLM endpoint classification results
- `manage-memory --action add/correct/reshape/promote/refine` — Write persistent memory
- `save-memory` — Save Q&A memory
- `apply-invariants` — Apply extracted invariants; **AMBIGUOUS never applied**; INFERRED require confirmation; EXTRACTED auto-applied
- `auto-enhance` — LLM auto-semantic enhancement; EXTRACTED+evidence auto-writes; **INFERRED require confirmation**; AMBIGUOUS rejected
- `batch-confirm` — Batch-confirm pending INFERRED enhancements
- `profile-evolve --apply` — Apply EXTRACTED-confidence profile suggestions; **INFERRED require confirmation**
- `doc-mark-stale` — Mark a node's doc as stale (non-destructive but visible)
- `ffi-types` — Update type marshalling table for an FFI edge
- `tx-commit` — For write transactions; commits snapshot + WAL entries to the live DB

**LLM behavior rules**:

1. Before executing the above commands, **MUST** report to the user in conversation:
   - Which node/edge/profile field will be modified
   - What is the old value, what is the new value
   - Information source (LLM read source / user told / extracted from docs)
   - Confidence level (EXTRACTED / INFERRED / AMBIGUOUS)
2. Wait for explicit user consent ("yes" / "confirm" / "proceed") before calling the command
3. **NEVER** use `--yes` / `-y` flag to bypass confirmation prompt unless user explicitly authorizes in conversation
4. If user declines, do not retry the same write

**Non-destructive write guarantee**:

- `update-node` and `update-edge` store LLM supplements as `{key}_supplemented` fields, **NOT overwriting** original scan data
- Each supplement includes `_supplement_meta` (recording source / confidence / timestamp / original), auditable in `describe-node` output
- `apply-invariants`, `auto-enhance`, and `profile-evolve` follow the same supplement pattern; `rollback` reverts by time or scope
- This guarantees "database accuracy": original scan facts are always preserved, LLM incremental data is traceable and rollback-able

## Reference Index

For details beyond this guide, read these on demand — **never load all at once**:

| Document | Content |
|----------|---------|
| `references/usage_reference.md` | Full command syntax for Tier-1 commands, parameters, code blocks, output formats |
| `references/label_rules.md` | Label definitions and classification rules |
| `references/data_model.md` | Node/edge attributes, context pack tiers, cgdb table schemas |
| `references/semantic_enhancement.md` | Semantic extraction and enhancement details |
| `references/endpoint_pipeline.md` | Endpoint classification pipeline |
| `references/cross_skill_collaboration.md` | Cross-skill collaboration protocols |
| `references/memory_knowledge.md` | Memory and knowledge management details |
| `references/json_schema.md` | Full JSON schema for `code2database_master.json` |
| `references/usage_examples.md` | Worked query examples across common scenarios |

**Sub-skill references** (loaded only when the corresponding sub-skill is activated):

| Document | Content |
|----------|---------|
| `~/.claude/skills/Code2Database-analysis/references/analysis_commands.md` | Tier-2 analysis commands (concurrency, data flow, invariants, FFI, provenance, cgdb MCP tools) |
| `~/.claude/skills/Code2Database-ops/references/ops_commands.md` | Tier-3 ops commands (transactions, daemon, profile, doc-code, exports, plugins, memory, embeddings) |

For runtime tuning parameters (invariants, auto_enhance, transactions, ffi, web_ui, benchmark, profile_health, doc_code, daemon sections), see `RUNTIME_CONFIG.md`.

For profile authoring (skip_names, callback_detection, struct_op_types, registration_macros, domain_rules, threading_models), see `PROFILE_MANUAL.md`.

**Internal files** (do NOT load into agent context): `OVERVIEW.md`, `scripts/`, `config/profiles/`. These are implementation details for tool developers, not needed for usage.
