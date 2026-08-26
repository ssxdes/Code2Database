# Analysis Commands Reference (Tier 2)

This document covers the full syntax for all 50 analysis commands and 19 `cgdb_*` MCP tools exposed by the `/Code2Database-analysis` sub-skill. **Read on demand only** — do not load this file into agent context unless you need detailed syntax for a specific command.

The commands are grouped by question type, mirroring the routing table in `SKILL_analysis.md`.

## Concurrency Safety

### `concurrency-risks`

List all global concurrency risk hot-spots detected during the build phase.

```bash
python3 scripts/code2database_builder.py concurrency-risks --graph code2db-out/ [--threshold high|medium|low]
```

Output: ranked list of functions with risk score, shared state count, lock count, thread model annotation.

### `concurrency-analyze`

Pair-wise concurrency safety analysis between two functions or two invocation chains.

```bash
python3 scripts/code2database_builder.py concurrency-analyze \
  --graph code2db-out/ \
  --fn-a function_a --fn-b function_b \
  [--shared-state VAR_NAME] [--json]
```

Output: thread model per function, shared state intersection, lock-held overlap, race verdict (SAFE / RISKY / RACY), evidence trace.

### `detect-races`

Cross-thread data race detection across the whole graph or a scoped subset.

```bash
python3 scripts/code2database_builder.py detect-races \
  --graph code2db-out/ \
  [--scope function_name | --scope domain_name] \
  [--json]
```

Output: list of race pairs with variable, accessing functions, thread models, lock status, evidence (char positions in source).

**HOLDER-edge annotation (Fix #10)**: When the project profile declares `lock_semantics` (see PROFILE_MANUAL.md §3.17) and `build` was run with `--profile`, race evidence includes `lock_held_context` showing the lock function and protected object (e.g., `{"lock_function": "mutex_lock", "lock_variable": "sb->s_lock", "protected_object": "sb"}`). This lets you prove a writer is guarded by a specific lock on a specific object — turning a soft "lock status: held" into a hard, edge-traceable proof.

### `lock-coverage`

Lock-held region analysis with event-stream output and precise char positions.

```bash
python3 scripts/code2database_builder.py lock-coverage \
  --graph code2db-out/ \
  --function function_name \
  [--json]
```

Output: per-lock event stream (acquire at line:col, release at line:col), uncovered sections (code executed without lock), nested-lock warnings.

### `happens-before`

Compute happens-before relationships between two events across threads (uses cgdb sync_primitives + happens_before tables when clang backend is enabled).

```bash
python3 scripts/code2database_builder.py happens-before \
  --graph code2db-out/ \
  --event-a "function_a:line:col" \
  --event-b "function_b:line:col" \
  [--json]
```

Output: ordered / concurrent / undetermined verdict, ordering edges, evidence.

### `memory-ordering`

Analyze memory ordering constraints (atomic operations, memory barriers, READ_ONCE / WRITE_ONCE, smp_mb).

```bash
python3 scripts/code2database_builder.py memory-ordering \
  --graph code2db-out/ \
  --function function_name \
  [--json]
```

Output: list of ordering points with location, operation type, ordering strength (relaxed / acquire / release / seq_cst), paired barrier.

### `who-locks`

Find all functions that acquire a specific lock variable.

```bash
python3 scripts/code2database_builder.py who-locks \
  --graph code2db-out/ \
  --lock LOCK_VAR_NAME \
  [--json]
```

Output: list of functions with acquire location, call context, thread model.

## Value / Parameter Flow

### `value-flow`

Trace parameter→return-value propagation across functions via DATA_FLOW edges.

```bash
python3 scripts/code2database_builder.py value-flow \
  --graph code2db-out/ \
  --start function_name [--param PARAM_NAME] \
  --end function_name \
  [--max-depth N] [--json]
```

Output: chain of propagation steps with function, parameter/return slot, transformation, evidence.

### `param-flow`

Trace parameter flow through a invocation chain (cross-function).

```bash
python3 scripts/code2database_builder.py param-flow \
  --graph code2db-out/ \
  --start function_name --param PARAM_NAME \
  [--end function_name] \
  [--max-depth N] [--json]
```

Output: propagation tree with parameter slot, calling edge, modification annotations.

### `data-dep`

Cross-function data dependency via DATA_DEP edges. Scans ALL nodes (not just one chain).

```bash
python3 scripts/code2database_builder.py data-dep \
  --graph code2db-out/ \
  --var VAR_NAME \
  [--scope function_name | --scope domain_name] \
  [--json]
```

Output: writers and readers of the variable, with cross-function edges and evidence.

### `data-lifecycle`

Resource alloc-use-release tracking.

```bash
python3 scripts/code2database_builder.py data-lifecycle \
  --graph code2db-out/ \
  --resource RESOURCE_NAME \
  [--json]
```

Output: allocation site, use sites, release site, unbalanced warnings.

### `io-path`

I/O path tracing from a function.

```bash
python3 scripts/code2database_builder.py io-path \
  --graph code2db-out/ \
  --function function_name \
  [--max-depth N] [--json]
```

Output: chain of I/O operations (read/write/ioctl/mmap) reachable from the function.

## Impact / Blast Radius

### `impact`

Impact analysis: affected invokers and invoked of a function.

```bash
python3 scripts/code2database_builder.py impact \
  --graph code2db-out/ \
  --function function_name \
  [--direction invokers|invoked|both] \
  [--max-depth N] [--lite] [--json]
```

Output: tree of affected nodes, depth-limited. `--lite` returns counts only.

### `blast-radius`

Compute affected APIs, tests, and domains for a change to a function.

```bash
python3 scripts/code2database_builder.py blast-radius \
  --graph code2db-out/ \
  --function function_name \
  [--json]
```

Output: list of API_entry functions affected, test functions affected, domains touched.

### `neighbors`

Get neighbors of a node (invokers, invoked, or both).

```bash
python3 scripts/code2database_builder.py neighbors \
  --graph code2db-out/ \
  --function function_name \
  [--direction invokers|invoked|both] \
  [--json]
```

### `path`

Find a path between two nodes.

```bash
python3 scripts/code2database_builder.py path \
  --graph code2db-out/ \
  --from function_a --to function_b \
  [--max-depth N] [--json] [--no-cache]
```

**Query result cache (Fix #15)**: `path` results are cached (TTL 600s, max 256 entries per graph). The cache invalidates on (a) TTL expiry, (b) graph SQLite file mtime change (catches daemon tx, manual sqlite3 edits, patcher writes), and (c) node-version bump on `update-node`/`patch-from-diff` for nodes the query touched. Pass `--no-cache` to bypass the cache for a single invocation — the result is computed fresh and is NOT written back to cache. Same caching applies to `describe-node`, `trace-chain`, `reverse-trace`, and `explore-flow`.

### `diff-chains`

Compare invocation paths under two macro configurations.

```bash
python3 scripts/code2database_builder.py diff-chains \
  --graph code2db-out/ \
  --from function_a --to function_b \
  --config-a CONFIG_A --config-b CONFIG_B \
  [--json]
```

Output: paths present in A only, in B only, in both, with condition annotations.

## Path Feasibility

### `path-feasible`

Z3 SMT path feasibility under constraints. Sound when Z3 is installed; heuristic fallback otherwise.

```bash
python3 scripts/code2database_builder.py path-feasible \
  --graph code2db-out/ \
  --from function_a --to function_b \
  [--constraint "VAR == VALUE" ...] \
  [--solver z3|heuristic] \
  [--json]
```

Output: feasible / infeasible verdict, unsat core (if Z3), model (if feasible), evidence chain. Flag results as **provisional** if heuristic was used.

The `--node` mode walks all paths from a node, accumulates conditions, and for each path runs:
1. Z3 / heuristic feasibility (`solve_path_feasibility`)
2. Config-predicate feasibility (`check_config_feasible`, when `--with-configs` is given)
3. **Runtime guard analysis** (`check_runtime_guards_with_profile`, Fix #6) — detects acquire/release regions, type/identity/lock-state predicates. When `--profile /path/to/profile.json` is provided, profile-declared `guard_functions` augment the built-in regex patterns; the output's `runtime_guards.profile_bindings` field lists bindings inferred from profile-declared guards (e.g., `{"sb_type": "blkdev"}`).

```bash
# Direct condition list mode with profile-declared guards
python3 scripts/code2database_builder.py path-feasible \
  --graph code2db-out/ \
  --conditions "if(!sb_is_blkdev_sb(sb))" \
  --profile /path/to/profile.json

# Node-walk mode with profile-declared guards
python3 scripts/code2database_builder.py path-feasible \
  --graph code2db-out/ \
  --node ext4_blkdev_getblock \
  --max-depth 8 \
  --profile /path/to/profile.json
```

See PROFILE_MANUAL.md §3.15 `guard_functions` for the profile schema.

### `path-guards`

Prove writer reachability from an entry point using guard conditions. Walks all paths from `--from` to `--to`, accumulates guard conditions (from edge `call_condition` + function-body `guard_condition` for the target field write), and uses Z3/heuristic to prove whether the conjunction of guards is satisfiable. If ALL paths are infeasible (guards contradict), the writer is unreachable in the scene.

```bash
python3 scripts/code2database_builder.py path-guards \
  --graph code2db-out/ \
  --from ext4_blkdev_getblock \
  --to __bread_gfp \
  --field b_bdev \
  [--value NULL] \
  [--max-depth 8] \
  [--with-configs "CONFIG_X=true"] \
  [--profile /path/to/profile.json]
```

The `--profile` flag (Fix #6) enables profile-declared `guard_functions` (e.g., project-specific type predicates like `sb_is_blkdev_sb`, lock acquire/release like `bd_prepare_to_claim`/`bd_abort_claim`). The output's `runtime_guards.profile_bindings` field surfaces the inferred bindings.

### `resolve-chain`

Conditional chain resolution with macro bindings.

```bash
python3 scripts/code2database_builder.py resolve-chain \
  --graph code2db-out/ \
  --from function_a --to function_b \
  --bindings CONFIG_MACRO=1,OTHER=0 \
  [--json]
```

Output: resolved path with per-edge `call_condition` evaluated under the bindings.

### `extract-signals`

Map `#ifdef` conditions to affected functions and edges.

```bash
python3 scripts/code2database_builder.py extract-signals \
  --graph code2db-out/ \
  [--signal CONFIG_MACRO] \
  [--json]
```

Output: signal map — for each `#ifdef` macro, list of functions and edges gated by it.

## Invariants

### `extract-invariants`

Extract preconditions, postconditions, loop invariants, and state machine from function bodies.

```bash
python3 scripts/code2database_builder.py extract-invariants \
  --graph code2db-out/ \
  --function function_name \
  [--json]
```

Output: list of invariants with kind (precondition / postcondition / loop_invariant / state_machine), expression, confidence (EXTRACTED / INFERRED / AMBIGUOUS), evidence.

### `find-invariants`

Find invariants matching a pattern across the graph.

```bash
python3 scripts/code2database_builder.py find-invariants \
  --graph code2db-out/ \
  --pattern "kind=precondition,var=PTR_NAME" \
  [--json]
```

### `apply-invariants`

Apply extracted invariants to the graph. **AMBIGUOUS never applied; INFERRED require user confirmation; EXTRACTED auto-applied.**

```bash
python3 scripts/code2database_builder.py apply-invariants \
  --graph code2db-out/ \
  --function function_name \
  [--confidence EXTRACTED|INFERRED] \
  [--yes]  # NOT recommended — bypasses confirmation
```

**Database write constraint**: MUST get user confirmation before applying INFERRED invariants. See SKILL.md Database Write Constraint section.

## FFI Tracing

### `ffi-detect`

Detect FFI bindings across the codebase (Python ctypes, Go cgo, Rust extern "C").

```bash
python3 scripts/code2database_builder.py ffi-detect \
  --graph code2db-out/ \
  [--json]
```

Output: list of FFI binding sites with source language, target language, binding kind.

### `ffi-list`

List all FFI binding sites with details.

```bash
python3 scripts/code2database_builder.py ffi-list \
  --graph code2db-out/ \
  [--source-lang python|go|rust] \
  [--json]
```

### `ffi-trace`

Trace a cross-language invocation chain.

```bash
python3 scripts/code2database_builder.py ffi-trace \
  --graph code2db-out/ \
  --from function_name \
  [--max-depth N] [--json]
```

Output: chain crossing language boundaries with FFI binding points annotated.

### `ffi-types`

Show type marshalling for an FFI edge. **Updating the type marshalling table requires user confirmation.**

```bash
python3 scripts/code2database_builder.py ffi-types \
  --graph code2db-out/ \
  --edge EDGE_ID \
  [--update]  # requires user confirmation
  [--json]
```

## Commit Provenance

### `blame-node`

Find the commit that introduced a node.

```bash
python3 scripts/code2database_builder.py blame-node \
  --graph code2db-out/ \
  --function function_name \
  [--json]
```

Output: commit hash, author, date, commit message, file:line of introduction.

### `describe-commit`

Show all changes introduced by a specific commit.

```bash
python3 scripts/code2database_builder.py describe-commit \
  --graph code2db-out/ \
  --commit HASH \
  [--json]
```

Output: list of nodes and edges added/removed/modified by the commit.

### `node-history`

Show commit history of a node (evolution over time).

```bash
python3 scripts/code2database_builder.py node-history \
  --graph code2db-out/ \
  --function function_name \
  [--json]
```

### `graph-provenance`

Show graph-wide provenance summary.

```bash
python3 scripts/code2database_builder.py graph-provenance \
  --graph code2db-out/ \
  [--json]
```

### `find-commits`

Find commits touching a function or file.

```bash
python3 scripts/code2database_builder.py find-commits \
  --graph code2db-out/ \
  --function function_name | --file PATH \
  [--json]
```

## Resource Lifecycle

### `who-allocates`

Find all functions that allocate a resource.

```bash
python3 scripts/code2database_builder.py who-allocates \
  --graph code2db-out/ \
  --resource RESOURCE_NAME \
  [--json]
```

### `who-frees`

Find all functions that free a resource.

```bash
python3 scripts/code2database_builder.py who-frees \
  --graph code2db-out/ \
  --resource RESOURCE_NAME \
  [--json]
```

### `unbalanced-alloc-free`

Find unbalanced allocation/free pairs (potential leak or double-free).

```bash
python3 scripts/code2database_builder.py unbalanced-alloc-free \
  --graph code2db-out/ \
  [--scope function_name] \
  [--json]
```

### `add-semantic-edges`

Add semantic edges (e.g., alloc→free pairing) to the graph. **Requires user confirmation.**

```bash
python3 scripts/code2database_builder.py add-semantic-edges \
  --graph code2db-out/ \
  --kind alloc_free \
  --src function_a --dst function_b \
  [--yes]  # NOT recommended
```

## Field Access

### `field-access`

Per-field read/write tracking across functions.

```bash
python3 scripts/code2database_builder.py field-access \
  --graph code2db-out/ \
  --struct STRUCT_NAME --field FIELD_NAME \
  [--mode read|write|both] \
  [--json]
```

Output: list of functions that read/write the field, with location and context.

**Object origin annotation (Fix #7)**: When the project profile declares `allocation_sites` (see PROFILE_MANUAL.md §3.16) and `build` was run with `--profile`, each writer/reader entry includes an `object_origin` field showing where the variable was initialized (e.g., `"alloc_buffer_head(...):buffer_head"` vs `"jh->bh"`). This lets you distinguish same-typed-different-instance objects — critical for proving that two writers operate on different objects and therefore cannot race.

## Audit

### `audit-log`

View audit log of past writes to the graph.

```bash
python3 scripts/code2database_builder.py audit-log \
  --graph code2db-out/ \
  [--since TIMESTAMP] [--scope function_name] \
  [--json]
```

## Declarative Query

### `query`

Cypher-subset query (MATCH/WHERE/RETURN).

```bash
python3 scripts/code2database_builder.py query \
  --graph code2db-out/ \
  --query "MATCH (a)-[r:INVOKES*1..3]->(b) WHERE b.name='target' RETURN a, r" \
  [--json]
```

Supported clauses: `MATCH` (node/edge patterns), `WHERE` (filters including `CONFIG(var, 'pred')`), `RETURN` (projection). See `references/data_model.md` for the full schema.

## On-demand / Low-weight Commands

### `think-chain`

Full chain thinking with conclusions — produces a step-by-step reasoning trace for a complex query.

```bash
python3 scripts/code2database_builder.py think-chain \
  --graph code2db-out/ \
  --question "Can these two chains race?" \
  [--json]
```

### `intent-query`

Intent-based graph query — natural-language intent resolved to graph operations.

```bash
python3 scripts/code2database_builder.py intent-query \
  --graph code2db-out/ \
  --intent "find all paths that could deadlock" \
  [--json]
```

### `extract-invariants-llm`

LLM-driven invariant extraction (uses an LLM to propose invariants from function bodies).

```bash
python3 scripts/code2database_builder.py extract-invariants-llm \
  --graph code2db-out/ \
  --function function_name \
  [--json]
```

### `explain-label`

Explain why a node got a particular label (API_entry, thread_processor, etc.).

```bash
python3 scripts/code2database_builder.py explain-label \
  --graph code2db-out/ \
  --function function_name \
  [--json]
```

### `why-ambiguous`

Explain why an edge is marked AMBIGUOUS.

```bash
python3 scripts/code2database_builder.py why-ambiguous \
  --graph code2db-out/ \
  --edge EDGE_ID \
  [--json]
```

### `extract-semantics` / `apply-semantics`

Extract semantic descriptions from docs and apply them to the graph. `apply-semantics` **requires user confirmation**.

```bash
python3 scripts/code2database_builder.py extract-semantics \
  --graph code2db-out/ \
  --function function_name \
  [--doc-path PATH] [--json]

python3 scripts/code2database_builder.py apply-semantics \
  --graph code2db-out/ \
  --function function_name \
  [--yes]  # NOT recommended — requires user confirmation
```

### `extract-knowledge` / `apply-knowledge` / `knowledge-query` / `knowledge-validate`

Knowledge management commands. `apply-knowledge` requires user confirmation.

```bash
python3 scripts/code2database_builder.py extract-knowledge --graph code2db-out/ --topic TOPIC [--json]
python3 scripts/code2database_builder.py apply-knowledge --graph code2db-out/ --topic TOPIC [--yes]
python3 scripts/code2database_builder.py knowledge-query --graph code2db-out/ --topic TOPIC [--json]
python3 scripts/code2database_builder.py knowledge-validate --graph code2db-out/ --topic TOPIC [--json]
```

## cgdb MCP Tools (18 tools, clang backend)

These tools query the cgdb (code graph database) layer directly. They require the clang extraction backend (`--extraction-backend clang` or auto with libclang installed). In tree-sitter-only mode, they return empty results.

### `cgdb_search_symbols`

FTS5 search across AST nodes (functions, types, variables).

```json
{"tool": "cgdb_search_symbols", "arguments": {"query": "mutex_lock", "limit": 20}}
```

### `cgdb_find_invokers`

Find invokers of a function via CGDB invoke_sites table.

```json
{"tool": "cgdb_find_invokers", "arguments": {"function": "mutex_lock"}}
```

### `cgdb_find_invoked`

Find invoked of a function.

```json
{"tool": "cgdb_find_invoked", "arguments": {"function": "my_function"}}
```

### `cgdb_get_definition`

Get the definition node of a symbol.

```json
{"tool": "cgdb_get_definition", "arguments": {"symbol": "my_struct"}}
```

### `cgdb_get_function_body`

Get the function body source range (file, start line/col, end line/col).

```json
{"tool": "cgdb_get_function_body", "arguments": {"function": "my_function"}}
```

### `cgdb_get_struct_layout`

Get struct field layout (offsets, types, names).

```json
{"tool": "cgdb_get_struct_layout", "arguments": {"struct": "file_operations"}}
```

### `cgdb_find_type_definition`

Find a type definition by name.

```json
{"tool": "cgdb_find_type_definition", "arguments": {"type_name": "task_struct"}}
```

### `cgdb_find_ops_impls`

Find ops-table implementations (typed vtable dispatch via FieldDecl → FunctionDecl bindings).

```json
{"tool": "cgdb_find_ops_impls", "arguments": {"ops_field": "read_iter", "ops_var": ""}}
```

Returns: list of `{ops_table_id, field_node_id, impl_function_id, signature_match}`.

### `cgdb_find_cfg_paths`

Find paths in the control-flow graph (L4 layer).

```json
{"tool": "cgdb_find_cfg_paths", "arguments": {"function": "my_function", "from_block": 0, "to_block": 5}}
```

### `cgdb_find_data_flow`

Find def-use chains in data-flow analysis (L5 layer).

```json
{"tool": "cgdb_find_data_flow", "arguments": {"function": "my_function", "variable": "ptr"}}
```

### `cgdb_find_aliases`

Find aliases of a pointer (L6 layer — currently a stub for MVP).

```json
{"tool": "cgdb_find_aliases", "arguments": {"variable": "ptr"}}
```

### `cgdb_find_lock_held_calls`

Find calls made while a lock is held (L8 sync_primitives + happens_before).

```json
{"tool": "cgdb_find_lock_held_calls", "arguments": {"lock": "my_mutex"}}
```

### `cgdb_check_race_condition`

Check for race conditions on a variable.

```json
{"tool": "cgdb_check_race_condition", "arguments": {"variable": "shared_counter"}}
```

Returns: list of accessing functions with thread model, lock status, race verdict.

### `cgdb_find_configs_for`

Find config predicates affecting a node (L3.5 layer).

```json
{"tool": "cgdb_find_configs_for", "arguments": {"node_id": 12345}}
```

### `cgdb_find_nodes_under_config`

Find all nodes under a config predicate.

```json
{"tool": "cgdb_find_nodes_under_config", "arguments": {"predicate": "CONFIG_SMP"}}
```

### `cgdb_index_status`

Show cgdb indexing status (per-file, per-layer).

```json
{"tool": "cgdb_index_status", "arguments": {}}
```

Returns: per-file row counts for each cgdb table (cgdb_nodes, cgdb_edges, cgdb_basic_blocks, cgdb_cfg_edges, cgdb_data_flow, cgdb_sync_primitives, cgdb_happens_before, cgdb_predicates, cgdb_ops_bindings).

### `cgdb_time_travel_query`

Query the graph at a past version (L10 time-travel).

```json
{"tool": "cgdb_time_travel_query", "arguments": {"version": "v1.2.3", "node_id": 12345}}
```

### `cgdb_list_versions`

List all recorded graph versions.

```json
{"tool": "cgdb_list_versions", "arguments": {}}
```

## See Also

- `SKILL_analysis.md` — Tier-1 commands + routing table (always loaded when sub-skill is active)
- `references/data_model.md` — cgdb table schemas, node/edge attributes
- Parent `~/.claude/skills/Code2Database/references/usage_reference.md` — Tier-1 command syntax
