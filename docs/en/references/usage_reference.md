# Usage Reference

Detailed command syntax, parameters, and output descriptions for Code2Database.

Read this file on demand when you need specific command details. Do not load into context unless needed.

> **The shift this enables**: every command below is a *query into a persistent code database*, not a file-by-file search. `explore-flow` returns relevant nodes + paths in one call (vs. N grep/Read round-trips). `trace-chain` returns A→B with conditions annotated (vs. manually walking invocation sites). `detect-races` returns cross-thread hazards (vs. reading every invoker of a shared resource). `field-access` returns who reads/writes a field (vs. grep across the codebase). The graph is the index; the commands are the query language.

## Step 0 — Check Prerequisites

**Manual Profile writing**: See `docs/PROFILE_MANUAL.md` for complete field descriptions, examples, and writing workflow.

Check if `code2db-out/code2database_master.json` exists. If it exists and this is not a rebuild request, skip to Step 4.

**If the project has no profile** (`code2db-out/.code2database_profile.json` doesn't exist and no `--profile` argument), run auto-profile first:

```bash
python3 "$SKILL_DIR/scripts/code2database_scanner.py" auto-profile \
  --source SOURCE_PATH --outdir OUTPUT_DIR
```

auto-profile detects project type, callback patterns, registration macros, skip_names, etc., generating a complete profile JSON.

Install dependencies if needed:
```bash
python3 -c "import networkx; import tree_sitter_c; import tree_sitter_go; import tree_sitter_python; import tree_sitter_java; import tree_sitter_rust; import tree_sitter_cpp" 2>/dev/null || python3 -m pip install networkx tree-sitter tree-sitter-c tree-sitter-cpp tree-sitter-go tree-sitter-python tree-sitter-java tree-sitter-rust -q
```

Note: ASM scanning requires no additional tree-sitter dependency — it uses built-in regex patterns.

## Step 0b — Auto-Detect Project Scale

For codebases with >100K functions, the build can OOM without special handling. Auto-detection logic runs after scan:

- If `total_functions > 50,000`: automatically enable large-project mode (sequential processing, reduced in-memory caching, split output)
- Print notice: `"Auto-detected large project (N functions), enabling optimized mode"`
- Applies: `--split-output` for scan, sequential domain loading for build, streaming writes for split_domain

Users can still force mode with `--large-project` or `--no-large-project`.

## Step 1 — AST Scan Extraction

```bash
python3 "$SKILL_DIR/scripts/code2database_scanner.py" scan \
  --source SOURCE_PATH --lang auto \
  --output code2db-out/.code2database_extraction.json \
  [--macros "NDEBUG FEATURE_X=1 -DFOO"]
```

Scanner output: functions (definitions) + edges (invocation relationships) + domains (architecture domains) + lang_stats (language stats) + fn_ptr_calls (function pointer callsites) + struct_types (struct/vtable field definitions) + includes (#include directives for C/C++)

### 1b — Conditional Branch Call Extraction

The scanner extracts calls within conditional expressions and annotates them with `call_condition`:

- **if-condition calls**: `if (validate() && process())` — both extracted with `if_cond(...)` annotation
- **if/else branches**: consequence calls get `if(...)`, alternative calls get `!(...)`
- **switch predicate**: `switch(get_key())` — `get_key()` extracted with `switch(...)` condition
- **switch case bodies**: calls annotated with the case label
- **while/do-while condition**: `while(has_next())` — `has_next()` extracted with `while_cond(...)` annotation
- **for-loop condition**: `for (; has_next(); )` — `has_next()` extracted with `for_cond(...)` annotation, body edges carry `for(has_next)`
- **for-loop init/update**: calls in init and update clauses extracted (e.g., `for (init(); ; update())`)
- **Ternary expression**: `cond() ? true_fn() : false_fn()` — condition gets `ternary_cond(...)`, true branch `ternary_true(...)`, false branch `!ternary(...)`
- **Compound conditions**: `&&` and `||` operators are traversed recursively; all nested calls inherit the parent condition scope

### 1c — C/C++ Import Resolution & Cross-File Call Resolution

During scan, `#include` directives are extracted per source file. The builder resolves cross-file invoked names through a multi-strategy pipeline:

1. **Suffix index lookup** (O(1)): Matches invoked name against node IDs — e.g., `bar` → `lib.bdev.bar` when the ID contains dots
2. **Multi-strategy resolution** (6 strategies in priority order):
   - `same_file` (0.95): invoked defined in same source file
   - `import_map` (0.85): invoked's header is `#include`d by invoker
   - `same_domain` (0.75): invoked in same architecture domain
   - `suffix_match` (0.60): invoked name matches node ID suffix
   - `unique_name` (0.55): only one function with that name globally
   - `fuzzy` (0.30-0.40): partial name match
3. **Post-build import resolution**: Scans header files to bridge remaining unresolved external endpoints

This means `foo()` in `file_a.c` calling `bar()` defined in `file_b.c` is correctly resolved when:
- Both files are scanned (same domain → same_domain strategy)
- `file_a.c` includes `file_b.h` (→ import_map strategy)
- `bar` is unique across the entire codebase (→ unique_name strategy)

### 1d — ASM-Specific Features

ASM files are processed with a dedicated regex-based scanner (no tree-sitter grammar available). Key capabilities:

- **Section context tracking**: Functions are tagged with their ELF section (`.text`, `.data`, `.bss`). Only `.text` section symbols are treated as callable functions; `.data`/`.bss` symbols are classified as data references.
- **Syscall modeling via rax register tracking**: For x86_64 kernel entry stubs, the scanner tracks `mov %eax, <nr>` patterns preceding `syscall` instructions, modeling the corresponding kernel syscall handler as the call target (INFERRED confidence).
- **Kernel ASM macro chains**: Kernel-style macros are recognized for function boundary detection:
  - `SYM_FUNC_START`/`SYM_FUNC_END`, `SYM_FUNC_START_LOCAL`/`SYM_FUNC_END`, `SYM_INNER_LABEL` — function boundary markers
  - `ENTRY`/`ENDPROC` — legacy kernel function markers
  - `EXPORT_SYMBOL` / `EXPORT_SYMBOL_GPL` — exported symbol annotations, marking functions as API_entry
- **Cross-language call edge merging (ASM <-> C)**: When an ASM function calls a C function (or vice versa), edges are merged during the build phase. The builder matches by symbol name, reconciling ASM labels with C function names.
- **C inline asm call extraction**: `__asm__`/`asm` volatile blocks within C/C++ source are parsed for `call` instructions; extracted invoked edges are annotated with confidence `INFERRED` and source `inline_asm`.
- **ARM bl/blr instruction support**: ARM assembly `bl` (branch with link) and `blr` (branch with link to register) instructions are extracted as call edges. Register-indirect `blr` edges are modeled as function pointer dispatch (similar to C fn_ptr_calls).
- **GCC extended asm named operands**: `%[name]` syntax in inline asm is parsed — `blr %[func]` resolves to the named operand's bound variable.
- **asm goto**: `asm goto` labels produce AMBIGUOUS edges with `call_condition=asm_goto`.
- **x86 register bindings**: Register variable bindings (`register void *rax __asm__("rax")`) and operand-based register references (`call *%2`) are tracked for indirect call target resolution.
- **JMP tail-call targets**: x86 `jmp` and ARM `b` (unconditional) are modeled as tail-call edges (INFERRED, confidence_score=0.6).
- **`__attribute__((naked))` stripping**: Naked attribute is pre-stripped (space-replaced) before tree-sitter parsing to prevent parse failures.
- **MSVC `__asm {}` blocks**: Inline asm in MSVC syntax is captured and parsed for call/jmp instructions.
- **ERROR node fallback**: When tree-sitter-c cannot parse (e.g., register bindings causing ERROR nodes), orphaned asm expressions are recovered via regex fallback.
- **Static fn-ptr dispatch arrays**: `static const void * const ops[] = {f1, f2}` patterns are collected and used to resolve dispatch_op targets.

## Step 2 — Semantic Enhancement

Supplement AST gaps: function pointers/callbacks, cross-file calls, conditional calls, callback registration flow. See `references/semantic_enhancement.md`.

**Do not overwrite existing data — only append new discoveries.**

### 2b — Function Pointer & Vtable Dispatch Resolution

For C/C++ projects that use function pointer dispatch (device_ops, protocol ops, callback chains, etc.), the code graph resolves indirect call targets through the vtable dispatch pipeline — this runs automatically during the build step:

1. **Load fn_ptr_calls from scan**: Verify fn_ptr_calls are correctly loaded from extraction data
2. **Vtable field name matching**: Build `_vtable_field_names` from `struct_op_types` in profile + `struct_types` from scan
3. **Field dispatch resolution**: For each fn_ptr_call with a known field name (e.g., `ops->read`), resolve to all known implementations registered in vtable data (`.code2database_vtables.json`)
4. **Callback dispatch edges**: For unresolved fn_ptr calls, add `callback_dispatch` edges to all known implementations of the struct op type (conservative over-approximation)
5. **Debug logging**: Log how many fn_ptr candidates were found, how many resolved, how many failed

### 2c — Macro-Expanded Call Resolution

The scanner's macro graph is integrated with invocation chain analysis:
1. When a invoked name matches a known macro, look up the macro's expansion
2. Extract function calls from the expanded macro body
3. Add edges from the macro invocation site to the expanded function with confidence `INFERRED`, source `macro_expansion`
4. Mark the macro-to-function edge in `evidence` as `{"kind": "macro_expansion", "weight": 0.8}`

## Step 3 — Build Call Graph

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" build \
  --extraction code2db-out/.code2database_extraction.json \
  --outdir code2db-out/ \
  [--build-config auto|Release|Debug|none] \
  [--macros "NDEBUG FEATURE_X=1"] \
  [--plugin my_enricher.py] \
  [--plugin-config '{"threshold":0.8}'] \
  [--storage json|sqlite|auto]
```

**Storage backend selection** (`--storage`):
- `json` (default for small projects): traditional JSON-based storage
- `sqlite`: SQLite database backend (`code2database.db`) with indexed tables for nodes, edges, reverse_index — queries run in <1 second, memory <500MB
- `auto`: use SQLite for projects with >50K functions, JSON otherwise

### 3b — Domain Path Normalization

Domain normalization ensures consistent domain paths and runs automatically during the build step:
1. Apply `domain_rules` from the profile to merge/collapse domains (e.g., `^drivers/net/ethernet/` → `drivers.net.ethernet`)
2. Post-scan domain deduplication: if two domains have the same suffix after removing common prefixes, merge them
3. Hub function domain correction: inline/header functions mapped to their definition domain, not inclusion domain

### 3c — Memory-Efficient Graph Building

Optimizations for large codebases:
1. **Chunked domain loading**: Load domain files one at a time, build subgraph, write to disk, release memory
2. **Compact graph representation**: Use integer arrays (CSR format) instead of Python dicts, reducing memory by ~5x
3. **Streaming edge writes**: Stream edges to disk during build instead of accumulating in memory
4. **Cross-domain edge isolation**: Load only cross-domain edges (typically <5% of total) for global operations
5. **Proactive release**: Release data structures after each build stage instead of relying on GC
6. **Parallel split_domain**: Write domain files across multiple threads with streaming JSON serialization

### 3d — Consistency Validation

Consistency validation runs automatically as part of the build step. If inconsistencies are found, re-running `build` will correct them.

Checks performed:
- Node count: domain table sum matches header total
- Edge count: confidence breakdown sum matches total edges
- Orphan nodes: nodes with no edges
- Domain coverage: functions in graph but not in any domain
- Endpoint consistency: endpoint counts across different output files

## Step 3e — Endpoint Classification

See `references/endpoint_pipeline.md`.

Features:
- Configurable endpoint types via profile `endpoint_types` (e.g., `syscall_entry`, `module_init`, `callback_entry`, `irq_entry`, `timer_entry`, `workqueue_entry`)
- Unified endpoint definitions across all stages and output files
- Clear legend in summary explaining different endpoint categories

## Step 3f — Enhanced API Scoring

Scoring formula:
```
Score = baseScore × exportMultiplier × nameMultiplier × frameworkMultiplier + bonusScore
```

Bonus scoring:
- Matches `public_prefixes` in profile: +20 points
- Membership in `struct_op_types`: +15 points
- Cross-domain invoker count: +1 per invoker (cap 50)
- Is a `program_entry` or `callback_entry`: +10 points
- Penalty for generic utility functions in `skip_names`: -10 points

## Output Files

```
code2db-out/
├── code2database_master.json           ← Navigation index
├── code2database.db                    ← SQLite database (when --storage sqlite|auto)
├── CODE2DATABASE_SUMMARY.md            ← Human-readable summary (3-layer L0/L1/L2)
├── REVIEW_CHECKLIST.md             ← LLM-filled node review checklist
├── SCENARIOS_SUMMARY.md            ← Execution scenarios human-readable summary
├── .semantics_changelog.md         ← LLM semantic changelog
├── .code2database_context_pack_micro.json   ← LLM context (~200 tokens, ultra-compact)
├── .code2database_context_pack_micro.md     ← Human-readable Markdown micro context
├── .code2database_context_pack_lite.json     ← LLM context (~500 tokens)
├── .code2database_context_pack_lite.md       ← Human-readable Markdown context
├── .code2database_context_pack_standard.json ← LLM context (~1500 tokens)
├── .code2database_context_pack.json          ← LLM context (full)
├── .code2database_pipeline_stats.json       ← Pipeline stats + consistency check
├── .code2database_scenarios.json       ← Pre-computed execution scenarios
├── .code2database_endpoints.json       ← Endpoint list (unified definition)
├── .code2database_build_config.json    ← Build configuration
├── .code2database_communities.json     ← Community detection
├── .code2database_entry_scores.json    ← Entry point scores
├── .code2database_processes.json       ← Execution flow detection
├── .code2database_vtables.json         ← Vtable registrations
├── .code2database_globals_enums.json   ← Enum members (split from globals)
├── .code2database_globals_macros.json  ← Macro definitions (split from globals)
├── .code2database_globals_typedefs.json ← Typedef definitions (split from globals)
├── .knowledge_pack_lite.json       ← LLM knowledge pack (~300 tokens)
├── .knowledge_pack_standard.json   ← LLM knowledge pack (~800 tokens)
├── .memory_pack_lite.json          ← LLM memory pack (~200 tokens)
├── .memory_pack_standard.json      ← LLM memory pack (~600 tokens)
├── knowledge/                      ← Knowledge directory (Markdown, human-readable)
│   ├── architecture.md
│   ├── module_*.md
│   ├── constraints.md
│   ├── glossary.md
│   ├── patterns.md
│   ├── build_rules.md
│   └── index.json
├── memory/                         ← Persistent memory directory
│   ├── index.json
│   ├── L0_index.json              ← Hot memory (weight>0.7)
│   ├── L1_index.json              ← Warm memory (0.3-0.7)
│   ├── L2_index.json              ← Cold memory (<0.3)
│   ├── root/                      ← Root memory (merged)
│   ├── leaf/                      ← Leaf memory (independent)
│   └── experience/                ← Stale memory
├── .scratch/                       ← Temporary scratch memory
├── domains/                        ← Domain JSON (compact format v3)
│   └── */DOMAIN_README.md
└── ...
```

**Globals split**: Instead of a single monolithic `globals.json`, globals are split into category-specific files:
- `.code2database_globals_enums.json` — enum members only
- `.code2database_globals_macros.json` — macro definitions only
- `.code2database_globals_typedefs.json` — typedef definitions only
- Each supports lazy loading per-domain

## Step 4 — Query and Answer

Use **global-to-local** mode: read context_pack_lite for global understanding first, then use tiered queries to drill into details.

### 4a — Global Understanding

```bash
cat code2db-out/.code2database_context_pack_lite.json
```

Upgrade to standard or full when more context is needed.

### 4b — Tiered Single-Node Description

```bash
# --brief (~200 tokens): id, name, signature, labels, invokers/invoked, exec_summary
# --standard (~500 tokens): +params, condition_vars, concurrency_info, api_constraints
# --full (~900 tokens): +local_vars, callee_args (--include-body for body_text)
python3 "$SKILL_DIR/scripts/code2database_builder.py" describe-node \
  --graph code2db-out/ --node NODE_ID --detail brief

# With hub role and reachable APIs/endpoints
python3 "$SKILL_DIR/scripts/code2database_builder.py" describe-node \
  --graph code2db-out/ --node NODE_ID --detail brief --context

# Token budget control (auto-drops low-priority fields)
python3 "$SKILL_DIR/scripts/code2database_builder.py" describe-node \
  --graph code2db-out/ --node NODE_ID --max-tokens 300

# Selective field output
python3 "$SKILL_DIR/scripts/code2database_builder.py" describe-node \
  --graph code2db-out/ --node NODE_ID --fields signature,params,invokers

# JSON format output
python3 "$SKILL_DIR/scripts/code2database_builder.py" describe-node \
  --graph code2db-out/ --node NODE_ID --detail brief --json
```

Auto-resolve: Accept partial node names — the system auto-resolves short names by searching the index, with disambiguation prompts if multiple matches exist.

### 4b2 — One-Shot Explore

```bash
# Natural language query → relevant nodes + paths + conditions
python3 "$SKILL_DIR/scripts/code2database_builder.py" explore-flow \
  --graph code2db-out/ --query "device initialization" --max-tokens 2000

# Symbol name query
python3 "$SKILL_DIR/scripts/code2database_builder.py" explore-flow \
  --graph code2db-out/ --query "device_open" --max-nodes 20
```

Get most relevant nodes, subgraphs, and key execution paths in one step.

### 4b3 — Key Paths Auto-Extraction

```bash
# Auto-extract top-5 critical paths from entry points
python3 "$SKILL_DIR/scripts/code2database_builder.py" key-paths \
  --graph code2db-out/ --top 5

# Specify entry point
python3 "$SKILL_DIR/scripts/code2database_builder.py" key-paths \
  --graph code2db-out/ --from device_open --top 3
```

Key paths use BFS from `program_entry` nodes to trace actual execution paths of 3-10 steps, deduplicated by structural similarity.

### 4c — Conditional Chain Resolution

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" resolve-chain \
  --graph code2db-out/ --node NODE_ID --bindings "mode=1,flag=true"
```

### 4d — One-Shot Path Tracing

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" trace-chain \
  --graph code2db-out/ --from FUNC_A --to FUNC_B \
  [--bindings "mode=1"] [--annotate] [--json]
```

Output complete annotated invocation paths (each step includes signature, conditions, concurrency markers).

### 4e — Path Diff

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" diff-chains \
  --graph code2db-out/ --node NODE_ID \
  --bindings-a "mode=0" --bindings-b "mode=1"
```

### 4f — Concurrency Risk Analysis

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" concurrency-risks \
  --graph code2db-out/ [--json]
```

List all spawn points, concurrency windows, sorted by risk level.

### 4g — Concurrency Safety Analysis

```bash
# Analyze whether two invocation chains can safely execute concurrently
python3 "$SKILL_DIR/scripts/code2database_builder.py" concurrency-analyze \
  --graph code2db-out/ --chain1 get_io_channel --chain2 for_each_channel

# Or specify a single function to auto-find concurrent peer functions
python3 "$SKILL_DIR/scripts/code2database_builder.py" concurrency-analyze \
  --graph code2db-out/ --func get_io_channel
```

Analyze data races, atomicity violations, deadlock risks; output shared resources and protection status.

### 4h — Data Race Detection

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" detect-races \
  --graph code2db-out/ [--func FUNCTION_NAME] [--min-severity low|medium|high]
```

Detect data races based on thread model and shared state access (global variables / struct fields).

### 4i — Field-Level Access Tracking

```bash
# Query which functions read/write a specific struct field
python3 "$SKILL_DIR/scripts/code2database_builder.py" field-access \
  --graph code2db-out/ --struct device_channel --field shared_resource

# Query which functions access a specific global variable
python3 "$SKILL_DIR/scripts/code2database_builder.py" field-access \
  --graph code2db-out/ --field g_mem_mgr
```

Output includes each accessor's function name, domain, read/write type, thread model.

### 4j — Crash Point Reverse Trace

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" reverse-trace \
  --graph code2db-out/ --crash-point channel_abort_pending_ops \
  --max-depth 10 --max-paths 20
```

Reverse BFS from crash point along INVOKES edges, listing all paths from entry points to the crash point. Prioritizes paths starting from API_entry / thread_processor.

**FIELD_WRITE suspects integration**. When investigating a crash that reads a field (e.g., `bh->b_bdev` dereference), you often need to see both *who reads the field at the crash point* and *who writes the field elsewhere*. The reverse-trace alone shows callers of the crash point; the new `--suspect-field` flag integrates field-write suspects from the `field_access` table into the output:

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" reverse-trace \
  --graph code2db-out/ --crash-point __bread_gfp \
  --suspect-field b_bdev --suspect-value NULL --suspect-struct bh \
  --max-depth 8 --max-paths 20
```

- `--suspect-field FIELD_NAME` — query `field_access` for all writers of this field; each writer is included as a `field_write_suspects` entry with reverse-BFS call chains back to entry points, `guard_condition` (if any), `object_origin` (if Profile declares `allocation_sites`), and a `reachable_in_scene` verdict (`guarded` / `unguarded`).
- `--suspect-value VALUE` — filter writers by assigned value (e.g., `NULL`, `0`). Use `NULL` to match any C NULL-form (`NULL`, `0`, `0L`, `(void*)0`). Requires `--suspect-field`.
- `--suspect-struct STRUCT_NAME` — filter writers by struct chain (e.g., `bh` matches `bh->field`). Optional; requires `--suspect-field`.

JSON output includes a `field_write_suspects` array and `field_write_suspects_summary` block (suspect_count, unguarded_count, field, value_filter, struct_filter). Text output appends a "Field write suspects:" section after "Concurrency entry points:".

This closes the gap that `reverse-trace` could see callers of the crash point but not the field-write suspects that may have caused the crash (see 续篇 report ). For full field-flow analysis (readers + writers + race windows), use `field-flow` directly.

### 4k — Data Lifecycle Tracking

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" data-lifecycle \
  --graph code2db-out/ --resource "buffer" [--json]
```

### 4l — Other Query Commands

```bash
# Search
python3 "$SKILL_DIR/scripts/code2database_builder.py" search \
  --graph code2db-out/ --keywords "device register" --top 20

# Load overview
python3 "$SKILL_DIR/scripts/code2database_builder.py" load --graph code2db-out/ --summary

# Neighbors
python3 "$SKILL_DIR/scripts/code2database_builder.py" neighbors \
  --graph code2db-out/ --node NODE_ID --depth 3

# Path
python3 "$SKILL_DIR/scripts/code2database_builder.py" path \
  --graph code2db-out/ --from NODE1 --to NODE2

# Impact analysis
python3 "$SKILL_DIR/scripts/code2database_builder.py" impact \
  --graph code2db-out/ --node NODE_ID --direction reverse

# Domain view
python3 "$SKILL_DIR/scripts/code2database_builder.py" domain \
  --graph code2db-out/ --name lib.device
```

All commands support `--json` for structured output.

### 4m — I/O Path Analysis

```bash
# Trace I/O paths from a function
python3 "$SKILL_DIR/scripts/code2database_builder.py" io-path \
  --graph code2db-out/ --from NODE_ID [--to TO_NODE] \
  [--bindings "FEATURE_X=1"] [--max-nodes 100] [--json]
```

Traces input/output data flow paths through the invocation graph, following functions that read from or write to external resources.

### 4n — File Watcher

```bash
# Watch source directory for changes and auto-rebuild
python3 "$SKILL_DIR/scripts/code2database_builder.py" watch \
  --source /path/to/project [--output code2db-out/] [--debounce 2.0]
```

Monitors source files for changes and triggers incremental rebuilds automatically. Debounce interval prevents rapid successive rebuilds.

### 4o — Streaming Query Results

Queries support `--stream` flag to return matches as they're found:
```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" search \
  --graph code2db-out/ --keywords "log_error" --top 100 --stream
```

Progress indicators show during long queries.

## Step 5 — Incremental Update

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" update \
  --source SOURCE_PATH --graph code2db-out/
```

### 5b — Incremental Index

- File modification timestamp-based change detection
- File-level dependency graph: when a file changes, only rebuild affected functions and edges
- Maintain a `file_deps.json` tracking which functions/edges depend on which source files
- Changed files → rescan only those files → patch graph → update affected domains
- Semantic descriptions for stale nodes filled on-demand during `describe-node` queries

### 5c — Team Sync

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" sync --graph code2db-out/
```

Merge strategy: same-name nodes prefer local, supplement remote-only nodes.

## Step 6 — Document Semantic Extraction

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" extract-semantics \
  --graph code2db-out/ --docs SOURCE_PATH
# Claude fills semantic_desc then:
python3 "$SKILL_DIR/scripts/code2database_builder.py" apply-semantics \
  --graph code2db-out/
```

## Step 7 — Full Chain Thinking

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" think-chain \
  --graph code2db-out/ --output code2db-out/.code2database_think_chain.json
```

Analyze each chain and fill in conclusions; supports checkpoint/resume.

## Step 8 — Q&A Memory

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" save-memory \
  --graph code2db-out/ --question "question" --answer "answer" --tags "tag1,tag2"
python3 "$SKILL_DIR/scripts/code2database_builder.py" search-memory \
  --graph code2db-out/ --query "related question" --top 5
```

Trust mechanism: trusted (verified, weight 1.0) / experience (possibly stale, weight 0.5-0.7)

## Step 8b — Advanced Memory Management

```bash
# Add memory (auto-merged to root memory)
python3 "$SKILL_DIR/scripts/code2database_builder.py" manage-memory \
  --graph code2db-out/ --action add \
  --question "question" --answer "answer" --tags "tag1"

# Correct memory field
python3 "$SKILL_DIR/scripts/code2database_builder.py" manage-memory \
  --graph code2db-out/ --action correct --id 5 --field answer --value "new answer"

# Reshape root memory (strong answer replaces entire root)
python3 "$SKILL_DIR/scripts/code2database_builder.py" manage-memory \
  --graph code2db-out/ --action reshape --root-id 1 --answer "brand new answer"

# Trigger weight decay (run periodically, no LLM needed)
python3 "$SKILL_DIR/scripts/code2database_builder.py" manage-memory \
  --graph code2db-out/ --action decay

# Boost memory weight
python3 "$SKILL_DIR/scripts/code2database_builder.py" manage-memory \
  --graph code2db-out/ --action promote --id 5 --boost 2.0

# Refine scratch memory into persistent memory
python3 "$SKILL_DIR/scripts/code2database_builder.py" manage-memory \
  --graph code2db-out/ --action refine --scratch-id session_123 \
  --question "summary Q" --answer "summary A"

# Query memory
python3 "$SKILL_DIR/scripts/code2database_builder.py" manage-memory \
  --graph code2db-out/ --action query --query "related question" --top 5

# Generate memory pack
python3 "$SKILL_DIR/scripts/code2database_builder.py" manage-memory \
  --graph code2db-out/ --action pack --tier lite
```

Memory tiers: L0 (hot, weight>0.7) / L1 (warm, 0.3-0.7) / L2 (cold, <0.3)
Root memory merging: similar questions (Jaccard>0.7) auto-merge, preserving version history
Weight decay: recency x importance x access, low-weight auto-archived as experience

## Step 9 — Export

```bash
# HTML visualization (vis-network/mermaid)
python3 "$SKILL_DIR/scripts/code2database_builder.py" export-html --graph code2db-out/

# Obsidian vault export
python3 "$SKILL_DIR/scripts/code2database_builder.py" export-obsidian --graph code2db-out/
```

## Step 9b — Knowledge (Project Brief)

```bash
# Session start (MANDATORY): render the brief into your prompt
python3 "$SKILL_DIR/scripts/code2database_builder.py" knowledge-brief \
  --graph code2db-out/

# Bootstrap the brief template from graph stats (first time)
python3 "$SKILL_DIR/scripts/code2database_builder.py" brief-extract \
  --graph code2db-out/

# Curate: mandatory macros, usage modes, pitfalls
python3 "$SKILL_DIR/scripts/code2database_builder.py" brief-update \
  --graph code2db-out/ --add hard_rules \
  --json '{"rule": "强制开启 XXX 宏", "type": "macro"}'

# Validate (schema, size budget, graph drift)
python3 "$SKILL_DIR/scripts/code2database_builder.py" brief-validate \
  --graph code2db-out/
```

Knowledge is the lean per-project brief: `code2db-out/knowledge/brief.json`.

## Step 9c — Efficient Graph Updates

Lightweight update workflow for code changes (no LLM needed):

```bash
# Method 1: Auto-patch from git diff
python3 "$SKILL_DIR/scripts/code2database_builder.py" patch-from-git \
  --graph code2db-out/ --source SOURCE_PATH [--commit-range HEAD~3]

# Method 2: Lightweight scan of changed files
python3 "$SKILL_DIR/scripts/code2database_builder.py" light-scan \
  --source SOURCE_PATH --graph code2db-out/ [--files file1.c,file2.c]

# Method 3: Patch from diff file
python3 "$SKILL_DIR/scripts/code2database_builder.py" patch-from-diff \
  --graph code2db-out/ --diff-file changes.diff
```

Three-layer lazy update strategy:
- **Layer 0** (real-time, 0 LLM tokens): File change → AST rescan → incremental graph update
- **Layer 1** (deferred, 0 LLM tokens): git diff → change patches → mark stale nodes
- **Layer 2** (on-demand, LLM involved): Semantic description fill → endpoint classification → knowledge extraction

Changes accumulate to a threshold before triggering full semantic update. Stale nodes get their semantics filled on-demand during describe-node queries.

## Step 9d — Source Code Snippet

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" get-code-snippet \
  --graph code2db-out/ --node NODE_ID --context 10
```

No need to read source files — locate source code lines directly from graph nodes.

## Step 9e — Blast Radius Analysis

```bash
# Which APIs/tests/domains are affected when changing a function
python3 "$SKILL_DIR/scripts/code2database_builder.py" blast-radius \
  --graph code2db-out/ --node NODE_ID --depth 3
```

Returns affected function count, API list, test list, affected domain list.

## Step 9f — MCP Server Mode

```bash
# Start MCP server (stdio transport) for LLM agent real-time queries
python3 "$SKILL_DIR/scripts/code2database_builder.py" serve \
  --graph code2db-out/
```

Exposes **81 MCP tools (53 base + 28 design-report)** (34 `code2database_*` + 19 `cgdb_*`):

**34 `code2database_*` tools** (always available):
- code2database_audit_log, code2database_blast_radius, code2database_composite_query, code2database_concurrency, code2database_daemon_status, code2database_data_lifecycle, code2database_describe, code2database_doc_code_check, code2database_domain, code2database_explain_label, code2database_explore, code2database_extract_signals, code2database_ffi_trace, code2database_find_invariants, code2database_foreign_refs, code2database_get_code_snippet, code2database_happens_before, code2database_impact, code2database_kb_query, code2database_key_paths, code2database_knowledge_query, code2database_load, code2database_memory_ordering, code2database_memory_search, code2database_path_feasible, code2database_search, code2database_semantic_status, code2database_sync_foreign, code2database_trace, code2database_unbalanced_alloc_free, code2database_who_allocates, code2database_who_frees, code2database_who_locks, code2database_why_ambiguous

Note: CLI-only commands (apply-invariants, blame-node, data-dep, field-access, lock-coverage, param-flow, profile-health, query, value-flow) are NOT exposed as MCP tools — invoke them via the CLI (`python3 scripts/code2database_builder.py <cmd>`).

**19 `cgdb_*` tools** (require clang extraction backend — `--extraction-backend clang` or auto with libclang installed):
- cgdb_search_symbols, cgdb_find_invokers, cgdb_find_invoked, cgdb_get_definition, cgdb_get_function_body, cgdb_get_struct_layout, cgdb_find_type_definition, cgdb_find_ops_impls, cgdb_find_cfg_paths, cgdb_find_data_flow, cgdb_find_aliases, cgdb_find_lock_held_calls, cgdb_check_race_condition, cgdb_find_configs_for, cgdb_find_nodes_under_config, cgdb_index_status, cgdb_time_travel_query, cgdb_list_versions

In tree-sitter-only mode, the `cgdb_*` tools return empty results — fall back to the `code2database_*` tools. See `~/.claude/skills/Code2Database-analysis/references/analysis_commands.md` for the full `cgdb_*` tool reference.

explore-flow is a one-shot context retrieval command for quickly locating relevant nodes and paths. Its results include a relevance_reason field explaining why each node is relevant to the query.

## Step 9g — Memory Health & Git Hook

```bash
# Check memory system health (counts, expired count, tier distribution)
python3 "$SKILL_DIR/scripts/code2database_builder.py" memory-health \
  --graph code2db-out/

# Install git post-commit hook to auto-run quick-update after each commit
python3 "$SKILL_DIR/scripts/code2database_builder.py" install-hook \
  --source /path/to/project

# quick-update with auto-threshold: auto-triggers semantic update when stale ratio exceeds threshold
python3 "$SKILL_DIR/scripts/code2database_builder.py" quick-update \
  --source /path/to/project \
  --graph code2db-out/ \
  --auto-threshold 0.15
```

## Step 9h — Architecture Flows & Knowledge Validation

```bash
# View core execution flow narratives (auto-generated API→endpoint path descriptions)
cat code2db-out/ARCHITECTURE_FLOWS.md

# Validate the project brief (schema, size budget, graph drift)
python3 "$SKILL_DIR/scripts/code2database_builder.py" brief-validate \
  --graph code2db-out/
```

ARCHITECTURE_FLOWS.md contains multi-step execution paths (3-10 steps) from `program_entry` to `API_entry`/`in_end`, with BFS path deduplication.

## Step 9i — Conditional Signal Extraction

```bash
# Extract how #ifdef conditions affect invocation paths
python3 "$SKILL_DIR/scripts/code2database_builder.py" extract-signals \
  --graph code2db-out/
```

Output `.code2database_signal_map.json`: condition variable → affected edges/functions/domains mapping, sorted by impact scope.

## Step 10 — LLM-Assisted Profile Generation

When auto-profile detection is insufficient (e.g., low callback pattern coverage, missing registration macros), use LLM-assisted profile generation.

### 10a — Auto-detect Need for LLM Assistance

```bash
# Generate auto-profile and check quality
python3 "$SKILL_DIR/scripts/code2database_scanner.py" auto-profile \
  --source SOURCE_PATH --outdir OUTPUT_DIR

# If output shows fewer than 5 callback patterns or fewer than 3 registration macros, suggest LLM assistance
```

### 10b — LLM-Assisted Profile Generation Flow

1. **Generate auto-profile** — Run `auto-profile --source SOURCE_PATH --outdir OUTPUT_DIR`
2. **Collect key headers** — `llm_phases.collect_key_headers()` auto-selects the most important headers
3. **Construct LLM prompt** — `llm_phases.generate_header_analysis_prompt()` generates a structured prompt
4. **Execute LLM analysis** — Send the prompt to an LLM, get structured JSON response
5. **Parse and merge** — `llm_phases.parse_header_analysis_response()` + `apply_header_analysis_to_profile()`
6. **Write updated profile** — Save to `.code2database_profile.json`

### 10c — LLM Prompt Execution Method

**Method 1: CodeAgent direct execution (recommended)**
```
# Read the generated profile
# Read key header file contents
# Use LLM's own analysis capability, following the prompt template in llm_phases.py
# Parse the analysis results with parse_header_analysis_response() and merge into profile
```

**Method 2: Using --llm-profile flag**
```bash
python3 "$SKILL_DIR/scripts/code2database_scanner.py" auto-profile \
  --source SOURCE_PATH --outdir OUTPUT_DIR --llm-profile
# Outputs LLM prompt for manual execution by user/agent
```

### 10d — LLM Analysis Quality Check

After LLM analysis, verify profile quality:
- `callback_detection.static_patterns` should be >= 5 (for well-known projects)
- `macro_dispatch.registration_macros` should be >= 3
- `skip_names.add` should be non-empty (at least project-specific skip names)
- `api_detection.public_header_paths` should be non-empty
- `endpoint_types` should include project-specific types as needed

If quality is still insufficient, execute Phase 6 (LLM result check) for further improvement.

### 10e — Phase 6: LLM Result Check

```bash
# After scanning completes, check extraction quality
python3 "$SKILL_DIR/scripts/code2database_builder.py" build \
  --extraction code2db-out/.code2database_extraction.json \
  --outdir code2db-out/

# Generate result check prompt
# Use llm_phases.generate_result_check_prompt() to generate prompt
# Analyze missing edges and misclassified endpoints in extraction
# Parse LLM response with parse_result_check_response()
```

---

## Complete CLI Command Reference (222 commands)

All 222 CLI subparsers across `code2database_builder.py` (214) and `code2database_scanner.py` (8). Each entry shows the command name and its `--help` summary.

| Command | Description |
|---------|-------------|
| `add-function` | Add a new function to the graph |
| `add-semantic-edges` | Walk graph and add ALLOCATES/FREES/LOCKS/UNLOCKS edges from body text |
| `apply-invariants` | Apply LLM-enhanced invariants from .code2database_invariants.json back to the graph |
| `brief-update` | Update a section of the project brief |
| `apply-semantics` | Apply LLM semantic descriptions to graph |
| `ast-search` | Structural code search: write patterns AS code with $metavars and ... ellipsis |
| `audit-log` | Query the audit log (who edited what, when, why) |
| `auto-enhance` | Auto-enhance a node with LLM-supplied attributes (auto-writes EXTRACTED, prompts INFERRED) |
| `auto-profile` | Auto-detect project type and generate/recommend profile |
| `batch-confirm` | Batch-confirm pending supplements (accept-all / reject-all / per-item / apply) |
| `blame-node` | Attribute a node to its introducing/last-modifying commit |
| `blast-radius` | Show blast radius: affected tests/APIs for a function change |
| `bridge-nodes` | Bridge nodes with high betweenness centrality (chokepoints) |
| `bug-benchmark` | Run BUG benchmark (graph vs grep) and report recall/precision/tool-call/token efficiency |
| `build` | Build invocation graph from extraction JSON |
| `build-diff` | Compare two graph builds: added/removed/changed nodes+edges+communities |
| `build-multi` | Build a unified C2D from a multi-project manifest |
| `c2d-add-foreign` | Register a foreign C2D and resolve cross-project refs |
| `c2d-add-foreign-stub` | Register a vendor SDK stub C2D (signatures only) |
| `c2d-check-compat` | Check if B's foreign_refs still valid against new A version |
| `c2d-list-foreign` | List watched foreign C2Ds with sync status |
| `c2d-pin-foreign` | Pin a foreign_ref so it won't auto-update |
| `c2d-prune-foreign` | Remove old deleted/orphaned foreign_refs |
| `c2d-remove-foreign` | Unregister a foreign C2D |
| `c2d-resolve-foreign` | Force re-resolve stale/deleted foreign_refs by name |
| `c2d-sync-foreign` | Sync foreign_refs with updated foreign C2Ds |
| `c2d-unpin-foreign` | Unpin a pinned foreign_ref |
| `cgdb-cfg-paths` | Enumerate CFG paths through a function (entry → exit blocks) |
| `cgdb-compare` | Compare two graph directories (e.g., main vs feature branch) |
| `cgdb-configs-for` | List config_predicate text_form(s) that gate a given node |
| `cgdb-coverage` | Query graph coverage: --function NAME | --file PATH |
| `cgdb-data-flow` | Show data_flow entries (def-use chains) for a variable node |
| `cgdb-definition` | Find definition nodes (function/var/field/typedef) by name |
| `cgdb-find-invoked` | Find callees (forward closure) of a node via recursive CTE |
| `cgdb-find-invokers` | Find callers (reverse closure) of a node via recursive CTE |
| `cgdb-freshness` | Check if the code graph is stale  |
| `cgdb-function-body` | Return a function's body source text |
| `cgdb-get-source` | Get source text for a node with byte-precise attribution |
| `cgdb-index-status` | Overall cgdb index statistics: node/edge counts by kind, file count |
| `cgdb-layer-summary` | Generate cgdb_layer_summary.md report for all 13 cgdb tables |
| `cgdb-merge-knowledge` | Merge knowledge/memory from another branch's graph  |
| `cgdb-nodes-under-config` | Find all nodes gated by a given config predicate |
| `cgdb-ops-impls` | Find ops_bind implementations for a given field name |
| `cgdb-path` | Find a call path from src to dst via recursive CTE |
| `cgdb-path-feasible` | Check feasibility of a CFG path through blocks (uses Z3 if available) |
| `cgdb-query` | Generic cgdb query: FTS5 symbol search or get_node by id |
| `cgdb-race-check` | Heuristic race-condition check for a function |
| `cgdb-schema-version` | Report current cgdb schema version and available migrations |
| `cgdb-sql` | Run arbitrary read-only SQL against the cgdb database (cross-table joins, ad-hoc analysis) |
| `cgdb-struct-layout` | Return a struct/union's field layout |
| `cgdb-suggest` | Analyze the graph and suggest improvements  |
| `cgdb-time-travel` | Query node state at a past version (by commit_hash or version_id) |
| `cgdb-tour` | Generate a guided codebase tour markdown  |
| `cgdb-type-definition` | Find type definitions (struct/union/enum/typedef/class) by name |
| `cgdb-versions` | List graph_versions rows (newest first), or diff two versions |
| `cgdb-views` | List/run predefined analysis views (hub functions, sync hotspots, doc coverage, etc.) |
| `cgdb-write-coverage` | Rewrite coverage reports |
| `classify-endpoints` | Apply LLM endpoint classification to the graph |
| `co-change` | Mine git log for co-change coupling edges |
| `code-slice` | Extract minimal context: data-flow slice or usage slice for LLM |
| `commit-db-transaction` | Commit a write-back transaction (render+compile+lint+sha256+git) |
| `composite-query` | Query across local + foreign C2Ds via SQLite ATTACH |
| `concurrency-analyze` | Analyze concurrency safety between two call chains or a function and its concurrent peers |
| `concurrency-risks` | List all concurrency risk points sorted by risk level |
| `coverage-cross-c2d` | Compute which functions in target_c2d are called by test_c2d |
| `daemon-force-refresh` | Force daemon to re-scan a specific file immediately |
| `daemon-list-projects` | List all projects with daemon state/log files on this machine |
| `daemon-logs` | Show daemon log file (last N lines, or --follow for streaming) |
| `daemon-pause` | Pause daemon (e.g., before manual updates to avoid conflicts) |
| `daemon-reload` | Reload daemon config (sends SIGHUP; daemon re-reads profile) |
| `daemon-resume` | Resume daemon after pause |
| `daemon-start` | Start long-running daemon (foreground; blocks). Monitors source files and auto-updates graph. |
| `daemon-status` | Get daemon status: pid, last_sync, pending events, stale nodes |
| `daemon-stop` | Stop a running daemon (sends SIGTERM) |
| `daemon-wait-sync` | Block until daemon finishes current sync (LLM agents call before important queries) |
| `data-dep` | Cross-function data dependencies (globals/fields as nodes, mod-read chains, dead writers) |
| `data-lifecycle` | Trace resource allocation→usage→release paths |
| `delete-node` | Soft-delete an AST node by ID |
| `delete-token` | Delete a token by token_id |
| `describe-commit` | Show which nodes/edges a commit affected |
| `describe-node` | Get info about a node. Use --detail brief|standard|full to control output size |
| `detect-changes` | Detect changed files since last manifest |
| `detect-races` | Detect data races between different thread contexts |
| `diff-chains` | Compare execution paths under two different bindings |
| `discover` | Discover macro-based registration dispatch patterns from headers |
| `doc-alignment-report` | Generate full Markdown report of doc-code alignment issues |
| `doc-code-check` | Check doc-code alignment: detect mismatches between semantic_desc (from docs) and body_text (from code) |
| `doc-mark-stale` | Mark a node's doc as stale (e.g., after code change detected by daemon) |
| `doc-signature-diff` | Detect signature changes between two graph versions (old vs new) |
| `domain` | List all nodes/edges in a domain |
| `edit-token` | Edit a token's spelling by token_id |
| `embeddings-build` | Build TF-IDF char n-gram embeddings for semantic search |
| `embeddings-search` | Cosine-similarity search over node embeddings |
| `explain-label` | Explain why a node has a given label (dead_code, API_entry, race_risk, etc.) |
| `explore-flow` | One-shot context retrieval: query → nodes + paths + conditions |
| `export-changes` | Export change graph from git/svn changelog |
| `export-html` | Export invocation graph as interactive HTML |
| `export-mermaid` | Export call chains as Mermaid flowchart diagrams |
| `export-obsidian` | Export invocation graph as Obsidian vault with [[links]] = calls |
| `extract-invariants` | Extract preconditions/postconditions/loop_invariants + state machines from function bodies |
| `extract-invariants-llm` | Extract invariants with LLM consensus and continuous confidence |
| `brief-extract` | Initialize/refresh the brief template from graph stats |
| `extract-semantics` | Export nodes for LLM semantic description |
| `extract-signals` | Extract #ifdef condition→affected edges map |
| `ffi-auto-link` | Auto-link FFI bindings to watched foreign C2Ds |
| `ffi-detect` | Detect FFI boundaries (Python ctypes, Go cgo, Rust extern \ |
| `ffi-list` | List all FFI edges in the graph |
| `ffi-persist` | Persist FFI edges into SQLite bridge tables |
| `ffi-trace` | Trace the FFI call chain from a node |
| `ffi-types` | Find FFI type mappings matching patterns |
| `field-access` | Find which functions read/write a struct field or global variable |
| `field-flow` | Trace field writes + their call chains (combines field-access + reverse-trace) |
| `fill-request` | List empty fields on a node that the LLM should fill (auto-fill request) |
| `find-commits` | Find commits that recently modified a function |
| `find-invariants` | Find functions guaranteeing a given invariant (e.g., 'ctx->state == READY' after return) |
| `find-macros` | Find macro definitions and invocations |
| `get-code-snippet` | Extract source code snippet around a node |
| `get-pp-branches` | Get #ifdef branch tree for a file |
| `get-string-literals` | Find string literals with optional pattern |
| `graph-diff` | Diff two graph versions |
| `graph-history` | List graph versions or show history of a specific node |
| `graph-provenance` | Show which commit the current graph corresponds to |
| `graph-record-version` | Manually record a graph version |
| `happens-before` | Check happens-before between a writer and reader via locks, RCU, or memory barriers |
| `heuristic-enhance` | Generate heuristic supplements for empty fields — no LLM required (always-works fallback) |
| `hub-nodes` | Most connected nodes (highest in+out degree) |
| `hybrid-search` | Hybrid search: FTS5 BM25 + optional embedding + RRF fusion |
| `impact` | Impact analysis for a node |
| `import-foreign-knowledge` | Copy foreign C2D's knowledge/*.md into local knowledge/ |
| `insert-node-after` | Insert a new AST node after an anchor |
| `insert-token` | Insert tokens after a given anchor token_id |
| `install-hook` | Install git post-commit hook for auto quick-update |
| `intent-query` | Classify a natural-language question and route to a CLI command |
| `io-path` | Trace IO path from a function, auto-detecting vtable dispatch options |
| `kb-audit` | Audit KB: counts, stale, low-confidence, citations |
| `kb-cluster` | Cluster kb_paragraphs by FTS5 similarity + link principles |
| `kb-conflict` | Detect contradictory items in the same cluster |
| `kb-forget` | Immediately delete a kb_paragraph (no decay) |
| `kb-global-add` | Add an entry to the cross-project global KB |
| `kb-global-import` | Import a shared global KB JSON file |
| `kb-global-search` | Search the cross-project global KB |
| `kb-global-share` | Export global KB to a portable JSON file |
| `kb-known-unknowns` | List queries that returned no matches (Phase 9) |
| `kb-migrate` | Migrate kb_paragraphs rows into kb_items (fact-level) |
| `kb-query` | Unified FTS5+BM25 query across memory and knowledge |
| `kb-rebuild-index` | Rebuild the unified kb_paragraphs FTS5 index  |
| `kb-rollback` | Restore a kb_item to a prior version |
| `key-paths` | Extract key execution paths from entry points automatically |
| `knowledge-brief` | Render the project brief (session-start load) |
| `brief-validate` | Validate the brief (schema, size budget, graph drift) |
| `light-scan` | Lightweight scan of changed files (no LLM) |
| `load` | Load and summarize the invocation graph |
| `lock-coverage` | Analyze lock-held regions and per-access locksets (replaces over-approximation) |
| `lsp-server` | Start Code2Database as a read-only LSP server on stdio  |
| `manage-memory` | Manage persistent memory (add/correct/reshape/decay/promote/refine/query/pack/consolidate/export/import/scratch-*) |
| `manifest` | Save file fingerprint manifest |
| `memory-health` | Report memory system health statistics |
| `memory-ordering` | Show RCU/memory-barrier/atomic primitives used by a function |
| `merge` | Merge new extraction into existing graph |
| `merge-changes` | Merge change graph JSON into existing graph |
| `neighbors` | Get neighbors of a node |
| `node-history` | Show commit history for a node |
| `null-source` | Find all writers of NULL to a struct field (alias: field-flow --value NULL) |
| `param-flow` | Trace parameter flow through the call chain (cross-function) |
| `patch-from-diff` | Patch graph from unified diff text |
| `patch-from-git` | Patch graph from git diff |
| `patch-profile` | LLM-driven incremental calibration of auto-profile (non-destructive, requires user confirmation) |
| `path` | Find shortest call path between two nodes |
| `path-feasible` | Auto-solve path feasibility (no manual bindings needed) |
| `path-guards` | Prove writer reachability from entry using guard conditions |
| `plugins` | List available callgraph plugins |
| `profile` | Generate project profile by scanning source directories |
| `profile-bind-version` | Bind profile to current git/svn HEAD commit so stale profiles can be detected |
| `profile-evolve` | Detect new callback patterns in source and suggest profile additions; optionally apply EXTRACTED-confidence suggestions |
| `profile-health` | Compute 0-100 health score for a project profile (callback patterns, skip_names, vtable_types, etc.) |
| `query` | Run a Cypher-subset query against the graph (unified query language) |
| `quick-update` | One-click: patch + light-scan, no LLM needed |
| `references-of` | List ALL source locations where a symbol is referenced (declaration+calls+reads+writes) |
| `render-source` | Render source from DB tokens |
| `resolve-chain` | Trace call chain from a node with variable bindings to prune dead branches |
| `reverse-trace` | Reverse trace from crash point through callers with condition/concurrency annotation |
| `rollback` | Rollback supplement writes (revert to previous value) |
| `rollback-db-transaction` | Roll back a write-back transaction |
| `runtime-guards` | Detect runtime guard patterns in path conditions |
| `sarif-export` | Export analysis results to SARIF 2.1.0 format for CI/IDE |
| `save-memory` | Save Q&A memory with call chains |
| `scan` | Scan source files for invocation graph extraction |
| `scan-rpc` | Scan source for RPC client calls (HTTP/gRPC) + create stub edges |
| `search` | Search nodes by keywords |
| `search-memory` | Search memory for similar questions |
| `semantic-search` | Neural semantic search: FTS5 BM25 + neural embedding + RRF fusion |
| `semantic-status` | Check if semantic update is recommended |
| `serve` | Start MCP server for LLM agent queries (stdio transport) |
| `sync` | Sync local code2db-out with git-tracked version (local wins) |
| `taint-analysis` | Taint analysis: source/sink/sanitizer propagation through DATA_FLOW edges |
| `think-chain` | Generate complete call chains for structured analysis |
| `trace-chain` | One-shot trace from --from to --to with full annotation |
| `traverse-graph` | Free-form BFS/DFS traversal with depth and token budget |
| `tx-begin` | Begin a graph transaction (snapshot + WAL + write lock) |
| `tx-commit` | Commit the current transaction (clears WAL) |
| `tx-list-snapshots` | List all available snapshots |
| `tx-replay-wal` | Replay or rollback an unfinished WAL (crash recovery) |
| `tx-restore` | Restore graph state from a specific snapshot |
| `tx-rollback` | Rollback the current transaction (restores snapshot) |
| `tx-snapshot` | Take a manual snapshot (without starting a transaction) |
| `tx-status` | Show current transaction state and WAL status |
| `unbalanced-alloc-free` | Find functions that alloc without free (or vice versa) |
| `update` | Incremental update: re-scan changed files and merge |
| `update-edge` | LLM-driven incremental supplement of edge attributes (non-destructive, requires user confirmation) |
| `update-node` | LLM-driven incremental supplement of node attributes (non-destructive, requires user confirmation) |
| `validate` | Validate build output files for correctness |
| `validate-memory` | Validate memory against current graph; invalidate stale → experience |
| `validate-plugin` | Validate a plugin file for interface compliance |
| `validate-profile` | Validate a profile JSON against coverage metrics |
| `value-flow` | Build and query value-flow edges (where does this value come from / go to?) |
| `verify-consistency` | Verify DB render matches disk sha256 |
| `watch` | Auto-sync: watch source directory and update incrementally |
| `web-ui` | Start interactive Web UI server for graph browsing, path highlighting, LOD rendering |
| `who-allocates` | Find functions that allocate a resource (ALLOCATES edges) |
| `who-frees` | Find functions that free a resource (FREES edges) |
| `who-locks` | Find functions that acquire a lock (LOCKS edges) |
| `why-ambiguous` | Explain why an edge is marked AMBIGUOUS (fn_ptr dispatch, dead #ifdef, etc.) |
