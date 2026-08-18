# Data Model Reference

> **What makes this a code database, not just a graph**: every node carries `body_text`, `signature`, `params`, `local_vars`, `callee_args`, `condition_vars`, and `ifdef_conditions` — enough to reason about a function without opening the source file. Every edge carries `call_condition`, `concurrency`, `confidence`, `evidence`, and `preproc_alive` — enough to know *why* the call happens, *whether* it's certain, and *under what build config* it exists. The graph isn't a structural index; it's a queryable model of program semantics.

## Graph Structure

Code2Database generates a **directed graph (DiGraph)**:

- **Nodes** = Functions. Each node has: name, source_file, line, position index (`source_file:line`), domain, labels, is_empty_node, api_constraints, external_desc, **body_text**, **signature**, **params**, **local_vars** (with is_param flag), **callee_args** (with structured args and concurrency_info), **condition_vars**, **endpoint_type**, **declaration_only**
- **Edges** = Call relationships (invoker → invoked). Each edge has:
  - `call_order`: call sequence number (starting from 1 within same invoker)
  - `call_condition`: condition expression — supports if_cond/if/while_cond/for_cond/switch/ternary_cond/ternary_true/!ternary and compound &&/|| chains
  - `concurrency`: concurrency type (`""` sequential/`"thread_spawn"`/`"goroutine"`/`"callback"`/`"spawn_target"`/`"callback_dispatch"`)
  - `confidence`: confidence classification (`EXTRACTED`/`INFERRED`/`AMBIGUOUS`)
  - `source`: source tag (`"ast"`/`"llm"`/`"manual"`/`"plugin:<name>"`/`"preproc_dead"`/`"import_resolution"`/`"macro_expansion"`/`"vtable_resolution"`)
  - `confidence_score`: numeric score (0.0–1.0) — EXTRACTED=1.0, INFERRED=0.7–0.95, AMBIGUOUS=0.1–0.3

Node `labels_source` records the origin of each label (e.g. `{"thread_processor": "ast", "callback_func": "llm"}`).

**Edge Types**:
- `callback_dispatch`: Edge from a fn_ptr invocation site to a possible implementation. Represents conservative over-approximation of indirect call targets.
- `import_edge`: Edge between domains based on #include relationships (domain A includes headers from domain B).

**Node Attributes**:
- `endpoint_type`: Sub-classification of out_end/unknown_end nodes (configurable via profile)
- `declaration_only`: Boolean, true if function is declared in this domain but defined elsewhere (header-only declarations)
- `vtable_struct_type`: If this function is a vtable registration, which struct_op_type it belongs to
- `vtable_field`: If this function is a vtable registration, which field it implements

## Empty Nodes

When multiple invoked share the same call condition, they are aggregated through an empty node:

```c
void func(int a) {
    if (a) { func1(); func2(); }
    func3();
}
```

Generates: `func → [if(a)] → func1(1), func2(2)`, and `func → func3(3)`

## Architecture Domain

Domains are automatically derived from source file paths (language-agnostic):

| File path | Domain |
|-----------|--------|
| `lib/device/device.c` | `lib.device` |
| `module/device/nvme/device_nvme.c` | `module.device.nvme` |
| `pkg/server/handler.go` | `pkg.server` |
| `src/main/java/com/app/App.java` | `src.main.java.com.app` |
| `crates/engine/src/lib.rs` | `crates.engine.src` |

JSON output is organized by domain. Large graphs are split into multiple files, navigated by `code2database_master.json`.

### Domain Normalization

Inconsistent domain splitting can cause the same subsystem to appear as two separate domains. Normalization rules:

1. **Profile domain_rules**: Apply merge/collapse rules from profile
   - `^src/core/` → `core`
   - `^lib/utils/` → `lib.utils`
   - etc.
2. **Post-scan deduplication**: If two domains share the same suffix after removing common prefixes, merge them
3. **Hub function domain correction**: Inline/header functions mapped to their **definition** domain, not their **inclusion** domain
   - `malloc` → `stdlib` (not `include.os`)
   - `mutex_lock` → `threading.sync` (not `include.internal`)

### Domain and Module Depth

Borrowing the Deep Module concept from codebase-design:

| codebase-design concept | code graph equivalent | Meaning |
|------------------------|---------------------|---------|
| **Seam** (interface location) | Domain boundary | Cross-domain calls = cross-seam dependencies |
| **Interface** (exposed surface) | API_entry functions | Public functions exposed by the domain |
| **Implementation** (hidden) | Non-API functions within domain | Internal logic not exposed externally |
| **Depth** (small interface, large implementation) | `API_entry count / total functions in domain` | Smaller ratio = deeper (few interfaces, rich behavior) |
| **Leverage** (invoker benefit) | How many external invokers an API_entry serves | One API_entry serving N invokers = high leverage |
| **Locality** (maintainer benefit) | Change concentration | Bug fix only touching one domain function = high locality |

**Depth assessment**:
- **Deep module** (good): API_entry ≤ 3, domain functions > 10 — few interfaces, rich behavior
- **Shallow module** (needs attention): API_entry > 10, domain functions < 15 — interface bloat, thin implementation
- **Deletion test**: After removing a domain, does complexity vanish (shallow = just passing through) or scatter to N invokers (deep = earning it back)

Header-only declarations (marked `declaration_only: true`) are NOT counted as API_entry for depth ratio calculation.

## Edge Confidence Classification (inherited from graphify)

Every edge carries a confidence classification and source tag, helping humans and LLMs judge which data is certain and which needs verification:

| Confidence | Score | Meaning | Example |
|-----------|-------|---------|---------|
| `EXTRACTED` | 1.0 | Directly extracted from AST — call explicitly exists in source | `device_start()` calls `register_device()` — tree-sitter sees the call expression |
| `INFERRED` | 0.7–0.95 | LLM/plugin inference — implicit in source but requires semantic understanding | Callback target: `register_callback(cb)` → LLM infers `cb` is `my_handler` |
| `AMBIGUOUS` | 0.1–0.3 | Uncertain — function pointers, dynamic dispatch, macro expansion | `(*func_ptr)()` — static analysis cannot determine target |

Source tags: `"ast"` (tree-sitter scanner), `"llm"` (Claude semantic enhancement), `"manual"` (user manual), `"plugin:<name>"` (external plugin), `"preproc_dead"` (dead preprocessor branch), `"import_resolution"` (header import resolution), `"macro_expansion"` (macro-expanded call), `"vtable_resolution"` (vtable dispatch resolution)

Node `labels_source` records the source of each label. Confidence distribution statistics are available in CODE2DATABASE_SUMMARY.md.

**Consistency Requirement**: The total edge count MUST match the sum of confidence breakdown categories. Inconsistencies are flagged during the build step's automatic validation.

## Build-System Macro Resolution

C/C++ code uses `#ifdef`/`#ifndef`/`#if` conditional compilation. tree-sitter parses ALL branches (does not evaluate macros), causing the code graph to include dead code branches.

**Solution**: Detect build system, extract `-D` macro definitions, evaluate `#ifdef` conditions, mark dead branches.

Supported build systems: CMake, Make, Spec (RPM), Meson, Autotools, Kconfig, Bazel

**Marking rules**:
- Functions in dead branches → `labels: ["dead_code"]`, `labels_source: {"dead_code": "preproc_dead"}`
- Edges in dead branches → `confidence: "AMBIGUOUS"`, `confidence_score: 0.0`, `source: "preproc_dead"`, `preproc_alive: false`
- Edges in alive branches → additionally carry `preproc_condition` and `preproc_alive: true`

**Conditional compilation metadata on dispatch entries**: Vtable registrations and similar dispatch structures capture conditional compilation metadata. Each registration entry includes:
- `config_condition`: e.g., `FEATURE_SMP`, `MODULE_UNLOAD`
- `condition_active`: based on build configuration macros

This allows the code graph to model build-variant differences in dispatch resolution.

**Usage**:

```bash
# Auto-detect build system, select Release config macros
python3 "$SKILL_DIR/scripts/code2database_builder.py" build \
  --extraction extraction.json --outdir code2db-out/ \
  --build-config auto

# Specify specific config
python3 "$SKILL_DIR/scripts/code2database_builder.py" build \
  --extraction extraction.json --outdir code2db-out/ \
  --build-config Debug

# Specify macros manually
python3 "$SKILL_DIR/scripts/code2database_builder.py" build \
  --extraction extraction.json --outdir code2db-out/ \
  --macros "NDEBUG HAVE_CONFIG_H=1 FEATURE_X=1"

# Specify macros during scan
python3 "$SKILL_DIR/scripts/code2database_scanner.py" scan \
  --source /path/to/code --output out.json \
  --macros "NDEBUG FEATURE_A=1 -DFOO"
```

**Interactive confirmation**: When multiple build configurations are detected, builder prompts for selection. In non-interactive scenarios, use `--build-config Release` explicitly.

**Output**: CODE2DATABASE_SUMMARY.md gains "Build Configuration" section; context_pack gains `build_config` field; standalone file `.code2database_build_config.json` for incremental update reuse.

## Community Detection (Leiden Algorithm)

Uses Leiden algorithm for semantic community detection on the invocation graph, supplementing directory-path-based domain grouping. Clusters frequently inter-calling functions into communities with heuristic labels (from directory patterns, function name prefixes, keywords).

**Scalability**: For graphs with >100K nodes where Leiden is too memory-intensive:
- **Hierarchical Leiden**: Run Leiden on each domain separately, then merge results
- **Label propagation**: Use as scalable alternative for very large graphs
- **Sampling-based**: Run community detection on a representative sample, then assign remaining nodes

**Output files**: `.code2database_communities.json` (community list + node mapping); CODE2DATABASE_SUMMARY.md gains "Community Map" section; context_pack gains `community_map`; nodes gain `community_id` attribute.

**Dependency**: `python-igraph` + `leidenalg` (falls back to domain grouping when unavailable)

## Entry-Point Scoring

Multi-factor scoring to identify the most likely public API entry points:

```
Score = baseScore × exportMultiplier × nameMultiplier × frameworkMultiplier + bonusScore
```

- baseScore = callee_count / (caller_count + 1) — calls many, called by few = orchestration entry
- exportMultiplier = 2.0 (API_entry) / 1.0
- nameMultiplier = 1.5 (handle_/on_/main/run etc.) / 0.3 (get_/set_/is_/has etc.)
- frameworkMultiplier = boosted by framework detection (SPDK/Django/Spring etc. → 1.5x)
- Bonus scores:
  - +20 points: matches `public_prefixes` in profile
  - +15 points: membership in `struct_op_types`
  - +1 per cross-domain invoker (cap 50)
  - +10 points: is `program_entry` or `callback_entry`
  - -10 points: in `skip_names` (generic utility functions like strlen, memcpy)

**Output files**: `.code2database_entry_scores.json`; nodes gain `entry_score` attribute.

## Framework Detection

Automatically identifies known frameworks from file paths (SPDK, DPDK, Django, Flask, FastAPI, Spring, Gin, Actix, Rocket, Tokio, Qt etc.), used for entry-point scoring multipliers.

Supported frameworks: C/C++ (SPDK/DPDK/libevent/libuv/Qt/GTK), Python (Django/Flask/FastAPI/Celery/Tornado), Java (Spring/JAX-RS/Android), Go (Gin/Echo/Fiber), Rust (Actix/Rocket/Tokio/Warp/Axum), Node.js (Express/Next.js/Nuxt/Koa/Fastify)

## Process Detection (Execution Flow)

BFS trace from top-scored entry points through INVOKES edges, generating execution flows. Cross-community tracking with heuristic labels.

Process detection follows `callback_dispatch` edges in addition to `direct_call` edges, enabling tracing across dispatch boundaries.

**Output files**: `.code2database_processes.json`; CODE2DATABASE_SUMMARY.md gains "Execution Processes" section; context_pack gains `execution_processes`.

## Evidence Trace

Every edge carries an `evidence` array recording extraction source and context:
- `{"kind": "ast_call", "weight": 1.0, "note": "direct call at line 42"}` — AST direct extraction
- `{"kind": "import_resolution", "weight": 0.75, "note": "resolved via #include <header.h>"}` — Import resolution
- `{"kind": "thread_spawn", "weight": 0.85, "note": "spawn target: func, line 42"}` — Thread spawn
- `{"kind": "macro_expansion", "weight": 0.8, "note": "CALL_FN expands to real_handler"}` — Macro expansion
- `{"kind": "vtable_dispatch", "weight": 0.75, "note": "ops.read → my_read", "struct_type": "device_ops"}` — Vtable dispatch
- Dead code branch: `{"kind": "ast_call", "weight": 0.0, "note": "dead branch: #ifdef X, line 42"}`

Plugins can add their own evidence entries.

## Import Resolution (C/C++)

Scans header files (.h/.hpp) to build header→function mapping, resolves cross-file call targets through multi-strategy resolution pipeline:

1. **suffix_index** (O(1)): Matches invoked name against node IDs — e.g., `bar` → `lib.bdev.bar`
2. **same_file** (0.95): Callee defined in same source file
3. **import_map** (0.85): Callee's header is `#include`d by invoker
4. **same_domain** (0.75): Callee in same architecture domain
5. **suffix_match** (0.60): Callee name matches node ID suffix
6. **unique_name** (0.55): Only one function with that name globally
7. **fuzzy** (0.30–0.40): Partial name match

Post-build pass: scans header files to bridge remaining unresolved external endpoints via #include chains, adding `INFERRED` edges (confidence=0.75, source="import_resolution").

## SQLite Storage Backend

For large graphs, SQLite provides efficient querying with low memory overhead.

Database schema (legacy tables):

```sql
CREATE TABLE nodes (
    id TEXT PRIMARY KEY,
    name TEXT,
    domain TEXT,
    labels TEXT,  -- JSON array
    signature TEXT,
    source_file TEXT,
    line INTEGER,
    endpoint_type TEXT,
    declaration_only INTEGER DEFAULT 0,
    entry_score REAL DEFAULT 0
);

CREATE TABLE edges (
    src TEXT,
    dst TEXT,
    type TEXT,  -- direct_call, callback_dispatch, import_edge, etc.
    call_order INTEGER,
    call_condition TEXT,
    confidence TEXT,
    confidence_score REAL,
    source TEXT,
    concurrency TEXT,
    evidence TEXT,  -- JSON array
    FOREIGN KEY (src) REFERENCES nodes(id),
    FOREIGN KEY (dst) REFERENCES nodes(id)
);

CREATE TABLE reverse_index (
    node TEXT,
    invokers TEXT  -- JSON array of invoker node IDs
);

CREATE TABLE vtable_dispatch (
    struct_type TEXT,
    field TEXT,
    implementation TEXT,
    config_condition TEXT,
    FOREIGN KEY (implementation) REFERENCES nodes(id)
);

CREATE TABLE field_access (
    struct_name TEXT,
    field TEXT,
    accessor_fn TEXT,
    access_type TEXT,  -- read/write/read-write
    thread_model TEXT,
    FOREIGN KEY (accessor_fn) REFERENCES nodes(id)
);

-- Indexes for common queries
CREATE INDEX idx_nodes_name ON nodes(name);
CREATE INDEX idx_nodes_domain ON nodes(domain);
CREATE INDEX idx_nodes_labels ON nodes(labels);
CREATE INDEX idx_edges_src ON edges(src);
CREATE INDEX idx_edges_dst ON edges(dst);
CREATE INDEX idx_edges_type ON edges(type);
CREATE INDEX idx_reverse_node ON reverse_index(node);
CREATE INDEX idx_vtable_struct ON vtable_dispatch(struct_type);
CREATE INDEX idx_field_struct ON field_access(struct_name);
```

## cgdb (Code Graph Database) Layer — 13 Typed Semantic Tables

When the clang extraction backend is enabled (`--extraction-backend clang` or `auto` with libclang installed), Code2Database populates an additional 13-layer typed semantic schema in the same `code2database.db`. These tables are queried by the 18 `cgdb_*` MCP tools and power features like typed vtable dispatch, CFG path finding, def-use chains, and Z3-reasonable config predicates. Schema version: `CGDB_SCHEMA_VERSION = 3`.

| Layer | Table(s) | Purpose | Key Columns |
|-------|----------|---------|-------------|
| L0 | `graph_versions` | Per-commit snapshot for time-travel queries | `version_id`, `commit_hash`, `created_at`, `parent_version` |
| L1 | `cgdb_nodes`, `cgdb_files` | Multi-kind first-class nodes + file registry | `node_id` (SHA-256 truncated 60-bit), `kind`, `fqn`, `name`, `source_file_id`, `line`, `col`, `enclosing_symbol_id`, `config_predicate_id`, `source_snippet`, `description`, `llm_confidence` |
| L2 | `cgdb_types` | Independent type system (builtin/pointer/reference/array/record/enum/function/template/typedef) | `type_id`, `kind`, `name`, `size`, `alignment`, `const`, `volatile`, `pointee_type_id` |
| L3 | `conditions` | Z3 SMT-LIB boolean expression trees | `condition_id`, `z3_form`, `human_form`, `kind` (atomic/not/and/or/implies) |
| L3.5 | `config_predicates` | `#ifdef` predicate tree (BDD + Z3 form), cross-language (Go `//go:build`, Rust `#[cfg]`, Python `sys.platform`, Java `@Profile`, ASM/C `#ifdef`) | `predicate_id`, `normalized_form`, `z3_form`, `bdd_form`, `status` (UNCONDITIONAL/CONTRADICTORY/CONDITIONAL), `language`, `source_file_id`, `line` |
| L4 | `basic_blocks`, `cfg_edges` | Control flow graph | `block_id`, `function_id`, `statement_ids`, `terminator_kind`; `edge_id`, `src_block`, `dst_block`, `condition_id`, `kind` (fallthrough/true/false/loop_back/exception) |
| L5 | `data_flow`, `alias_sets` | Def-use chain + pointer alias | `flow_id`, `function_id`, `variable`, `def_block`, `def_kind` (param/var/return), `use_block`, `use_kind` (call/return/branch/assign); `alias_set_id`, `pointer`, `aliases` (JSON) |
| L7 | `invoke_sites`, `ops_bindings` | Invocation refinement + typed vtable dispatch | `site_id`, `caller_function_id`, `callee_expr`, `callee_resolved_id`, `condition_id`, `is_indirect`; `binding_id`, `ops_table_id`, `field_node_id`, `impl_function_id`, `signature_match` |
| L8 | `sync_primitives`, `happens_before` | Concurrency + memory model | `prim_id`, `kind` (mutex/spinlock/rwlock/atomic/condvar/barrier), `var_name`, `acquire_site_id`, `release_site_id`, `memory_order`; `hb_id`, `event_a`, `event_b`, `ordering` (happens_before/concurrent/undetermined) |
| FTS | `nodes_fts` | FTS5 virtual table for symbol search | FTS5 columns over `cgdb_nodes.name`, `fqn`, `description` |

### Cross-Language Config Predicate Normalization

All languages normalize to the L3.5 `config_predicates` layer:

- C/C++/ASM: `#ifdef CONFIG_SMP` → `CONFIG_SMP`
- Go: `//go:build linux && amd64` → `CONFIG_GO_TAG_LINUX AND CONFIG_GO_TAG_AMD64`
- Rust: `#[cfg(target_os = "linux")]` → `CONFIG_CFG_TARGET_OS_LINUX`
- Python: `sys.platform == "linux"` → `CONFIG_PY_PLATFORM_LINUX`
- Java: `@Profile("prod")` → `CONFIG_JAVA_PROFILE_PROD`

Each predicate carries a `status` field:
- `UNCONDITIONAL` — always true (no `#ifdef` guard)
- `CONDITIONAL` — depends on macro values; Z3 form is satisfiable
- `CONTRADICTORY` — Z3 form is unsatisfiable (dead branch)

### Schema Migrations

`cgdb_migrations.run_migrations` ALTERs tables in-place when schema version bumps, preserving data. Current schema version: 3. To check: `cgdb_index_status` MCP tool reports per-file row counts per layer.

### Legacy ↔ cgdb Sync

`cgdb_sync.sync_legacy_and_cgdb()` keeps `functions`/`edges` (legacy) and `cgdb_nodes`/`cgdb_edges` (cgdb) synchronized. Legacy tables answer "who calls whom"; cgdb tables answer typed semantic questions. Both coexist in the same SQLite database.

### Cross-Language Unified Node ID

Every node ID across every language is a SHA-256 hash truncated to 60 bits (high bit cleared for SQLite signed INTEGER compatibility), prefixed with a language code (`c:`, `go:`, `py:`, `java:`, `rust:`, `asm:`). This prevents cross-language collisions while keeping IDs compact. See `_scanner/unified_id.py`.

## Globals Split

Global definitions are split into category-specific files for efficient access:
- `.code2database_globals_enums.json` — enum members
- `.code2database_globals_macros.json` — macro definitions
- `.code2database_globals_typedefs.json` — typedef definitions

Each supports lazy loading per domain. Context pack can selectively include relevant globals.

## Plugin/Extension Architecture

The `build` command supports `--plugin` flag to load custom Python scripts, plus auto-discovery from `.code2database_plugins/` directory.

Plugin interface:

```python
class Code2DatabasePlugin:
    def enrich_functions(self, functions, edges, source_root):
        """Pre-build: modify functions/edges data. Return (functions, edges)."""
        return functions, edges

    def enrich_graph(self, G):
        """Post-build: modify networkx DiGraph. Return G."""
        return G

    # Extended hooks (optional):
    def custom_scan(self, file_path, language, macro_bindings):
        """Custom scanner, returns additional (functions, edges)."""
        return [], []

    def custom_query(self, G, query_type, params):
        """Custom query handler, returns result dict or None (fall through to default)."""
        return None

    def custom_output(self, G, output_dir, format_hint):
        """Custom output generation."""
        pass
```

Plugin-added edges must set: `confidence="INFERRED"`, `source="plugin:<name>"`, `confidence_score=0.7-0.95`.

Plugin configuration: `--plugin my_plugin.py --plugin-config '{"threshold": 0.8}'`

Plugin validation: `code2database_builder.py validate-plugin --plugin my_plugin.py`
