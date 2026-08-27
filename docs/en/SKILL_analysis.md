---
name: Code2Database-analysis
description: "Deep semantic analysis sub-skill for Code2Database. Activated when the user asks about concurrency safety, data races, value flow, parameter flow, data dependencies, path feasibility under constraints, invariants (preconditions/postconditions/loop_invariants/state_machine), FFI boundaries (Python ctypes / Go cgo / Rust extern C), commit-level provenance, lock-held regions, resource allocation/free, blast radius of a change, or wants to query the cgdb (code graph database) layer directly via the 19 cgdb_* MCP tools. Provides hierarchical routing: 13 Tier-1 high-weight commands shown in Quick Reference, a routing table mapping question types to medium-weight command groups, and an on-demand section listing low-weight experimental commands by name only. Use when /Code2Database detects a deep-analysis question and explicitly hands off, or when the user types /Code2Database-analysis. Not for: graph building, scanning, or simple browsing (use parent /Code2Database); not for transactions, daemon, profile editing, or exports (use /Code2Database-ops)."
trigger: /Code2Database-analysis
parent_skill: Code2Database
---

# /Code2Database-analysis

**Deep semantic analysis layer for Code2Database.** Activated when the user's question goes beyond "what calls what" — concurrency safety, data races, value flow, path feasibility under constraints, invariants, FFI boundaries, commit-level provenance, or direct cgdb (code graph database) queries via the 19 `cgdb_*` MCP tools.

This sub-skill does **not** re-scan or rebuild the graph. It assumes `code2db-out/` is already built (via the parent `/Code2Database` skill) and the graph is fresh (run `daemon-status` / `daemon-wait-sync` first if a daemon is running).

## When to Activate

- User explicitly types `/Code2Database-analysis`
- The parent `/Code2Database` skill detects a deep-analysis question and hands off with the phrase *"activate Code2Database-analysis sub-skill"*
- User asks any of:
  - "Is this thread-safe?" / "Can these two chains race?"
  - "Where does this NULL / value come from?"
  - "What breaks if I change this function?"
  - "Is this invocation path feasible under these constraints?"
  - "What invariants does this function enforce?"
  - "Which Python/Go/Rust function calls into C?"
  - "Which commit introduced this bug?"
  - "Who allocates / frees this resource?"
  - "Which functions hold this lock?"
  - "Query cgdb tables directly" (clang backend — types, CFG, data flow, ops bindings, sync primitives, config predicates, time-travel versions)

## Tier 1 — High-weight Commands (Quick Reference)

These are the commands you'll reach for most often. Each one replaces dozens of grep/Read calls.

| Command | Purpose |
|---------|---------|
| `concurrency-analyze` | Pair-wise concurrency safety analysis (thread model + shared state + lock analysis) |
| `detect-races` | Cross-thread data race detection |
| `lock-coverage` | Lock-held region analysis with event-stream + char positions |
| `value-flow` | Trace parameter→return-value propagation (DATA_FLOW edges) |
| `param-flow` | Trace parameter flow through invocation chain (cross-function) |
| `data-dep` | Cross-function data dependency (DATA_DEP edges; scans ALL nodes) |
| `field-access` | Per-field read/write tracking across functions |
| `path-feasible` | Z3 SMT path feasibility (heuristic fallback when Z3 absent) |
| `blast-radius` | Affected APIs / tests / domains for a change |
| `find-invariants` | Find invariants matching a pattern (preconditions / postconditions / loop_invariants / state_machine) |
| `ffi-trace` | Trace cross-language invocation chains (Python ctypes / Go cgo / Rust extern C) |
| `blame-node` | Find the commit that introduced a node |
| `query` | Cypher-subset query (MATCH/WHERE/RETURN) for one-off structured queries |

## Routing Table — Medium-weight Commands by Question Type

When the question type matches one of these, use the listed command sequence. Read the reference file (`references/analysis_commands.md`) only when you need detailed syntax.

| Question Type | Command Sequence |
|---------------|------------------|
| **Is this thread-safe?** | `concurrency-risks` → `concurrency-analyze` → `detect-races` → `lock-coverage` → `happens-before` → `memory-ordering` → `who-locks` |
| **Where does this NULL / value come from?** | `value-flow` → `param-flow` → `data-dep` → `data-lifecycle` → `io-path` |
| **What breaks if I change this?** | `impact` → `blast-radius` → `neighbors` → `path` → `diff-chains` |
| **Is this path feasible?** | `path-feasible` → `resolve-chain` → `extract-signals` |
| **What invariants does this function enforce?** | `extract-invariants` → `find-invariants` → `apply-invariants` |
| **Which Python/Go/Rust function calls into C?** | `ffi-detect` → `ffi-list` → `ffi-trace` → `ffi-types` |
| **Which commit introduced this?** | `blame-node` → `describe-commit` → `node-history` → `graph-provenance` → `find-commits` |
| **Who allocates / frees this resource?** | `who-allocates` → `who-frees` → `unbalanced-alloc-free` → `add-semantic-edges` |
| **Query cgdb tables directly (clang backend)** | use the 19 `cgdb_*` MCP tools — see "cgdb MCP Tools" section below |

## cgdb MCP Tools (clang backend — 19 tools)

When the user wants to query the cgdb layer directly (clang-derived semantic tables), use these MCP tools. They are accessible regardless of sub-skill activation — MCP is separate from the skill layer.

| MCP Tool | Purpose |
|----------|---------|
| `cgdb_search_symbols` | FTS5 search across AST nodes (functions, types, vars) |
| `cgdb_find_invokers` | Find invokers of a function (CGDB invoke_sites) |
| `cgdb_find_invoked` | Find invoked of a function |
| `cgdb_get_definition` | Get the definition node of a symbol |
| `cgdb_get_function_body` | Get the function body source range |
| `cgdb_get_struct_layout` | Get struct field layout |
| `cgdb_find_type_definition` | Find type definition by name |
| `cgdb_find_ops_impls` | Find ops-table implementations (typed vtable dispatch) |
| `cgdb_find_cfg_paths` | Find paths in the control-flow graph |
| `cgdb_find_data_flow` | Find def-use chains in data-flow analysis |
| `cgdb_find_aliases` | Find aliases of a pointer (stub — MVP) |
| `cgdb_find_lock_held_calls` | Find calls made while a lock is held |
| `cgdb_check_race_condition` | Check for race conditions on a variable |
| `cgdb_find_configs_for` | Find config predicates affecting a node |
| `cgdb_find_nodes_under_config` | Find all nodes under a config predicate |
| `cgdb_index_status` | Show cgdb indexing status (per-file, per-layer) |
| `cgdb_time_travel_query` | Query the graph at a past version |
| `cgdb_list_versions` | List all recorded graph versions |

**Prerequisite**: cgdb tables are populated only when the `clang` extraction backend is enabled (auto-detected when libclang is installed, or forced with `--extraction-backend clang`). In tree-sitter-only mode, cgdb tables are empty and these MCP tools return empty results — fall back to the standard `code2database_*` MCP tools.

## On-demand Commands (low-weight, experimental / rare)

These commands are listed by **name only**. They are experimental, niche, or rarely needed. Read `references/analysis_commands.md` to learn what each does before invoking — only when the user explicitly asks for them.

- `think-chain` — full chain thinking with conclusions
- `intent-query` — intent-based graph query
- `extract-invariants-llm` — LLM-driven invariant extraction
- `explain-label` — explain why a node got a particular label
- `why-ambiguous` — explain why an edge is marked AMBIGUOUS
- `extract-semantics` / `apply-semantics` — extract and apply semantic descriptions from docs
- `extract-knowledge` / `apply-knowledge` / `knowledge-query` / `knowledge-validate` — knowledge management
- `audit-log` — view audit log of past writes

## Activation Hand-off

When you detect a question that belongs to **graph editing, transactions, daemon, profile/doc-code, exports, plugins, memory, embeddings**, hand off to the ops sub-skill:

> "This question is about graph operations / daemon / profile / transactions. Activate `Code2Database-ops` sub-skill."

When you detect a question about **simple browsing, scanning, building, or general invocation relationships**, hand off to the parent:

> "This question is about basic graph navigation. Activate `Code2Database` sub-skill."

## Constraints (inherited from parent)

- **Global-to-local mode**: start from `context_pack_lite` / `describe-node` before drilling into specialized analysis commands
- **Read no more than 10 source files per query**
- **Edge confidence**: every edge is `EXTRACTED` / `INFERRED` / `AMBIGUOUS` + source — never treat AMBIGUOUS as fact
- **Path feasibility**: sound only with Z3 installed; without Z3, heuristic fallback may produce false positives — flag results as provisional
- **Value flow**: DATA_FLOW edges trace parameter→return-value propagation; if a node lacks DATA_FLOW edges, the chain is broken — do not infer flow without evidence
- **FFI tracing**: requires BOTH source and target language scanners to detect a binding site
- **Invariants confidence**: never apply AMBIGUOUS invariants; INFERRED require user confirmation before `apply-invariants`
- **Commit provenance**: every node/edge carries `commit_meta.source_commit` (git/svn hash). Verify with `git show <hash>`, not timestamps
- **Database write constraint**: any DB-modifying command (`apply-invariants`, `add-semantic-edges`, `apply-semantics`, `apply-knowledge`) **requires user confirmation first**. See `references/analysis_commands.md` for the full protocol.
- **Do not pre-load** `references/analysis_commands.md` — read on demand only when you need detailed syntax for a specific command
- **cgdb clang backend is recommended, not required** — tree-sitter-only mode remains fully functional; cgdb_* MCP tools return empty results in that mode

## Reference Index

| Document | Content |
|----------|---------|
| `references/analysis_commands.md` | Full syntax for all 48 analysis commands + 19 cgdb_* MCP tools |
| `references/data_model.md` | Node/edge attributes, context pack tiers, cgdb table schemas |
| `references/semantic_enhancement.md` | Semantic extraction and enhancement details |
| `references/endpoint_pipeline.md` | Endpoint classification pipeline |
| `references/cross_skill_collaboration.md` | Cross-skill collaboration protocols |
| `references/usage_examples.md` | Worked query examples across common scenarios |

**Inherited from parent** (`/Code2Database`): `references/usage_reference.md`, `references/label_rules.md`, `references/json_schema.md`, `references/memory_knowledge.md`. These are available at the parent skill's references directory.

**Internal files** (do NOT load into agent context): `OVERVIEW.md`, `scripts/`, `config/profiles/`. These are implementation details for tool developers, not needed for usage.
