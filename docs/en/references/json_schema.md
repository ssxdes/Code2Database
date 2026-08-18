# Call Graph JSON Schema

> **The schema is the contract that makes this a database.** Every node carries `body_text`, `params`, `callee_args`, `condition_vars`, `ifdef_conditions` — enough to reason about a function without opening source. Every edge carries `call_condition`, `concurrency`, `confidence`, `evidence`, `preproc_alive` — enough to know why a call happens and whether it's certain. This schema is language-agnostic: C, Go, Python, Java, Rust, and ASM all produce the same structure, so a query written for one project works for any project.

## Edge Types

| Edge Type | Description | Key Fields |
|-----------|-------------|------------|
| INVOKES | Function invocation relationship | call_order, call_condition, concurrency, confidence |
| CONTAINS | File → function containment | relation="CONTAINS" |
| IMPORTS | File → file #include relationship | relation="IMPORTS", import_path |

## Overview

The invocation graph is stored as domain-split JSON files under `code2db-out/`. Each domain gets its own file, and a master navigation file provides cross-referencing. The schema is language-agnostic — all languages (C/C++/Go/Python/Java/Rust/ASM) produce the same JSON structure.

## Master Navigation File: `code2database_master.json`

```json
{
  "type": "code2database_master",
  "source_root": "/path/to/source",
  "domains": {
    "lib.device": "code2database_domain_lib_device.json",
    "module.device.nvme": "code2database_domain_module_device_nvme.json"
  },
  "cross_domain_edges": [
    {
      "source": "lib_device_device_register",
      "target": "module_device_nvme_device_nvme_init",
      "call_order": 3,
      "call_condition": "",
      "source_domain": "lib.device",
      "target_domain": "module.device.nvme"
    }
  ],
  "total_nodes": 150,
  "total_edges": 320
}
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Always `"code2database_master"` |
| `source_root` | string | Absolute path to the scanned source tree |
| `domains` | object | Map of domain name → domain JSON filename |
| `cross_domain_edges` | array | Edges where source and target are in different domains |
| `total_nodes` | integer | Total nodes across all domains |
| `total_edges` | integer | Total edges (domain + cross-domain) |

## Domain File: `code2database_domain_<sanitized>.json`

```json
{
  "type": "code2database_domain",
  "domain": "lib.device",
  "nodes": [
    {
      "id": "lib_device_device_register",
      "name": "device_register",
      "source_file": "lib/device/device.c",
      "line": 245,
      "location": "lib/device/device.c:245",
      "domain": "lib.device",
      "labels": ["API_entry"],
      "is_empty": false,
      "condition": "",
      "api_constraints": "dev: struct device * (non-NULL); ctx: void *",
      "external_desc": ""
    },
    {
      "id": "lib_device_pthread_create",
      "name": "pthread_create",
      "source_file": "",
      "line": 0,
      "location": "",
      "domain": "external",
      "labels": ["out_end"],
      "is_empty": false,
      "condition": "",
      "api_constraints": "",
      "external_desc": "Creates a new thread"
    },
    {
      "id": "lib_device_my_outlib_func",
      "name": "my_outlib_func",
      "source_file": "",
      "line": 0,
      "location": "",
      "domain": "external",
      "labels": ["unknown_end"],
      "is_empty": false,
      "condition": "",
      "api_constraints": "",
      "external_desc": ""
    }
  ],
  "edges": [
    {
      "source": "lib_device_device_register",
      "target": "lib_device_device_open",
      "call_order": 1,
      "call_condition": ""
    }
  ]
}
```

### Node Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Unique identifier: `{domain_dots_to_underscores}_{func_name_normalized}` |
| `name` | string | yes | Original function name as written in source |
| `source_file` | string | yes | Relative path from source root (empty for external nodes) |
| `line` | integer | yes | Definition line number (0 for empty/external nodes) |
| `location` | string | yes | `source_file:line` for direct code indexing (empty for external nodes) |
| `domain` | string | yes | Architecture domain (dot-separated path components; "external" for unresolved invoked) |
| `labels` | array of string | yes | Function labels (see Node Labels section) |
| `is_empty` | boolean | yes | True for virtual/conditional-grouping nodes |
| `condition` | string | yes on empty | The condition this empty node represents |
| `api_constraints` | string | yes | Parameter constraints for API_entry functions; empty otherwise |
| `external_desc` | string | yes on out_end | Description of what the external function does; empty for unknown_end |
| `semantic_desc` | string | yes | LLM-extracted semantic description (function implementation, constraints, purpose, usage scenarios); empty until extract-semantics + apply-semantics is run |
| `body_text` | string | yes | Full function body source text; empty for external/empty nodes. Enables LLM to read function logic without opening source files |
| `signature` | string | yes | Full function signature (return type + name + params); empty for external/empty nodes |
| `params` | array of object | yes | Formal parameters: `{name, type, is_param: true}`; extracted from function signature; empty for external/empty nodes |
| `local_vars` | array of object | yes | Variable assignments and parameters: `{name, type, value_snippet, line, is_param}`; params listed first with `is_param: true` and `value_snippet: "<param>"`; empty for external/empty nodes |
| `callee_args` | array of object | yes | Call site arguments: `{call_order, invoked, args_snippet, args: [{pos, value}], concurrency_info: {is_spawn, spawn_target, spawn_arg, concurrency_type}, callback_target}`; enables tracking callback targets and parameter flow |
| `condition_vars` | array of object | yes | Variables referenced in conditions: `{condition, vars: [name, ...]}`; enables branch evaluation without reading source |
| `preproc_alive` | boolean | no | `false` when this function is entirely within a dead preprocessor branch (excluded by build macros). `true` or absent otherwise |

### Edge Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source` | string | yes | Caller node ID |
| `target` | string | yes | Callee node ID |
| `call_order` | integer\|null | yes | Sequential order of this call within the invoker's body (null for invoker→empty edges) |
| `call_condition` | string | yes | Condition under which this call occurs. Non-empty on invoker→empty edges; empty on empty→invoked and direct edges |
| `concurrency` | string | yes | Concurrency type: `""` (sequential), `"thread_spawn"` (call creates a thread), `"goroutine"` (Go goroutine launch), `"callback"` (callback registration), `"spawn_target"` (virtual edge from spawner to the spawned thread function) |
| `confidence` | string | yes | `"EXTRACTED"`, `"INFERRED"`, or `"AMBIGUOUS"` |
| `source` | string | yes | Origin: `"ast"`, `"llm"`, `"manual"`, `"preproc_dead"`, or `"plugin:<name>"` |
| `confidence_score` | float | yes | 0.0–1.0 |
| `preproc_condition` | string | no | Preprocessor condition text (e.g., `"#ifdef FEATURE_X"`) — present when call is inside an #ifdef block |
| `preproc_alive` | boolean | no | `false` when this edge is in a dead preprocessor branch (excluded by build macros). `true` or absent otherwise |

## Endpoint Classification File: `.code2database_endpoints.json`

Generated by `build` command, consumed by `classify-endpoints` command after LLM fills in the classification.

```json
{
  "endpoints": [
    {
      "id": "lib_device_pthread_create",
      "name": "pthread_create",
      "domain": "external",
      "invokers": [
        {
          "id": "lib_device_device_start",
          "name": "device_start",
          "source_file": "lib/device/device.c"
        }
      ],
      "classification": "out_end",
      "external_desc": "Creates a new thread"
    },
    {
      "id": "lib_device_my_outlib_func",
      "name": "my_outlib_func",
      "domain": "external",
      "invokers": [],
      "classification": "unknown_end",
      "external_desc": ""
    }
  ]
}
```

### Endpoint Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Node ID in the graph |
| `name` | string | yes | Function name |
| `domain` | string | yes | Architecture domain (typically "external") |
| `invokers` | array | yes | List of nodes that call this endpoint (provides context) |
| `classification` | string | yes | LLM fills: `"out_end"` (known) or `"unknown_end"` (unclear) |
| `external_desc` | string | yes | LLM fills: functional description for out_end; empty for unknown_end |

## Empty Nodes (Conditional Grouping)

Empty nodes represent conditional branching points. They group invoked that execute under the same condition.

**Example (C):**

```c
void func(int a) {
    if (a) { func1(); func2(); }
    func3();
}
```

Generated graph:

- Node: `func` (real)
- Node: `func__cond_0` (empty, condition: `"if(a)"`)
- Edge: `func` → `func__cond_0` (call_condition: `"if(a)"`, call_order: null)
- Edge: `func__cond_0` → `func1` (call_order: 1, call_condition: "")
- Edge: `func__cond_0` → `func2` (call_order: 2, call_condition: "")
- Edge: `func` → `func3` (call_order: 3, call_condition: "")

**Example (Python):**

```python
def process(data):
    if data:
        validate(data)
        transform(data)
    return data
```

Generated graph follows the same pattern with `if(data)` condition.

## Node Labels

| Label | Meaning | C/C++ | Go | Python | Java | Rust |
|-------|---------|-------|-----|--------|------|------|
| `thread_processor` | Sub-thread entry | `pthread_create`, `std::thread` | `go func()` | `Thread(target=)` | `new Thread()` | `thread::spawn` |
| `callback_func` | Callback entry | callback args | callback args | callback args | `@Override`/callback | callback args |
| `constructor` | Init entry | `Cls::Cls()` | — | `__init__` | constructor decl | — |
| `destructor` | Cleanup entry | `Cls::~Cls()` | — | `__del__` | `finalize()/close()` | `Drop::drop` |
| `API_entry` | Public API | non-static | exported (uppercase) | non-private | public method | `pub fn` |
| `out_end` | Known external endpoint | — | — | — | — | — |
| `unknown_end` | Unknown external endpoint | — | — | — | — | — |
| `dead_code` | Excluded by preprocessor macros (build config) | `#ifdef` dead branch | — | — | — | — |

"-" means the label is never applied for that language (the concept doesn't exist natively).

`API_entry` nodes include the `api_constraints` attribute describing input parameter constraints (types, format restrictions, value ranges).

`out_end` nodes include the `external_desc` attribute describing the external function's purpose. `unknown_end` nodes have empty `external_desc` and require manual review.

New labels must be explicitly approved by the user before being introduced.

## Per-Language Node ID Conventions

Node IDs follow the pattern `{domain_underscored}_{normalized_name}`:

| Language | Example Name | Node ID |
|----------|-------------|---------|
| C | `device_register` in `lib/device/` | `lib_device_device_register` |
| C++ | `Device::open` in `lib/device/` | `lib_device_device_open` |
| Go | `Handler.ServeHTTP` in `pkg/server/` | `pkg_server_handler_servehttp` |
| Python | `Server.__init__` in `app/` | `app_server___init__` |
| Java | `App.start` in `src/main/` | `src_main_app_start` |
| Rust | `Engine::run` in `crates/engine/` | `crates_engine_engine__run` |

Normalization rules:
- All non-alphanumeric characters → underscore
- Everything lowercased
- `::` → `__` (Rust/C++), `.` → `_` (Go/Python/Java)

## API_entry Detection Rules

| Language | Detection Rule | Example |
|----------|---------------|---------|
| C/C++ | Non-static top-level functions | `void device_start()` → API_entry; `static void helper()` → not API |
| Go | Functions/methods with first letter uppercase | `func Start()` → API_entry; `func listen()` → not API |
| Python | Module-level functions and non-underscore-prefixed methods | `def run()` → API_entry; `def _helper()` → not API |
| Java | Methods with `public` modifier | `public void start()` → API_entry; `private void init()` → not API |
| Rust | Functions with `pub` visibility | `pub fn run()` → API_entry; `fn main()` → not API |

## Architecture Domain Classification

Domains are derived from the source file path relative to the project root (language-independent):

| File Path | Domain |
|-----------|--------|
| `lib/device/device.c` | `lib.device` |
| `module/device/nvme/device_nvme.c` | `module.device.nvme` |
| `pkg/server/handler.go` | `pkg.server` |
| `app/models/user.py` | `app.models` |
| `src/main/java/com/app/App.java` | `src.main.java.com.app` |
| `crates/engine/src/lib.rs` | `crates.engine.src` |

Rules:
- Drop the filename, keep directory components only
- Replace path separators with dots
- Single-level directories: `lib` → `lib`
- Root-level files: domain is `root`
- External/unresolved invoked: domain is `external`

## Incremental Update: Manifest File

File: `code2db-out/.code2database_manifest.json`

```json
{
  "source_root": "/path/to/source",
  "files": {
    "lib/device/device.c": "1234567890123456789:1024",
    "pkg/server/handler.go": "9876543210987654321:2048"
  }
}
```

Fingerprint format: `{mtime_ns}:{file_size}`. Used by `detect-changes` to find new/changed/deleted files. Generated automatically by `build` and `update` commands.

## Semantic Extraction File

File: `code2db-out/.code2database_semantics.json`

```json
{
  "nodes_to_describe": [
    {
      "id": "lib_device_device_start",
      "name": "device_start",
      "source_file": "lib/device/device.c",
      "line": 17,
      "location": "lib/device/device.c:17",
      "domain": "lib.device",
      "labels": ["thread_processor", "API_entry"],
      "api_constraints": "",
      "semantic_desc": ""
    }
  ],
  "doc_files": ["docs/architecture.md", "README.md"],
  "doc_root": "/path/to/source"
}
```

LLM fills `semantic_desc` for each node, then `apply-semantics` writes back.

## Call Chain Analysis File

File: `code2db-out/.code2database_think_chain.json`

```json
{
  "total_chains": 5,
  "api_entries": 2,
  "endpoints": 3,
  "chains": [
    {
      "from_api": "lib_device_device_start",
      "to_endpoint": "pthread_create",
      "length": 3,
      "steps": [
        {"id": "lib_device_device_start", "name": "device_start", "labels": ["thread_processor", "API_entry"], "is_empty": false, "condition": ""},
        {"id": "lib_device_device_start__cond_0", "name": "<conditional:if(mode==1)>", "labels": [], "is_empty": true, "condition": "if(mode==1)", "call_order": null, "call_condition": "if(mode==1)"},
        {"id": "pthread_create", "name": "pthread_create", "labels": ["out_end"], "is_empty": false, "condition": ""}
      ],
      "conclusion": ""
    }
  ]
}
```

Used for structured thinking with checkpoint/recovery. LLM fills `conclusion` for each chain. Deleted after analysis completes.

## Memory System

Directory: `code2db-out/memory/`

### Index File: `memory/index.json`

```json
{
  "entries": [
    {"id": 1, "question": "device_start的调用链是什么", "tags": ["device", "thread"], "status": "trusted"},
    {"id": 2, "question": "旧函数的功能", "tags": [], "status": "experience"}
  ],
  "next_id": 3
}
```

### Memory Entry: `memory/memory_<id>.json`

```json
{
  "id": 1,
  "question": "device_start的调用链是什么",
  "answer": "device_start根据mode条件分支...",
  "chains": [{"from": "api_id", "to": "ep_id", "steps": [...]}],
  "node_ids": ["c_device_start", "c_worker_thread_fn"],
  "tags": ["device", "thread"],
  "status": "trusted",
  "created": "2026-06-29T10:30:00",
  "validated_at": "2026-06-29T10:35:00",
  "merged_count": 0
}
```

### Experience Entry: `memory/experience/experience_<id>.json`

When a memory's `node_ids` reference nodes that no longer exist in the graph (e.g., after code update deleted those functions), the memory is invalidated and moved to experience:

```json
{
  "id": 2,
  "question": "旧函数的功能",
  "answer": "它是外部模块的函数",
  "chains": [],
  "node_ids": ["deleted_func_id"],
  "tags": [],
  "status": "experience",
  "created": "2026-06-29T10:00:00",
  "validated_at": "2026-06-29T10:30:00",
  "invalidated_at": "2026-06-29T11:00:00",
  "invalidated_reason": "1 node(s) removed by update: ['deleted_func_id']",
  "merged_count": 0
}
```

**Trust lifecycle:** `trusted` (all node_ids present) → `experience` (some node_ids missing) after `update` or `validate-memory`. Experience entries are still searchable but with reduced weight (0.5-0.7x).

## Globals File: `.code2database_globals.json`

Generated by `build` command from scanner extraction data. Contains enum/constant/typedef/global_var definitions needed to evaluate branch conditions.

```json
{
  "enums": [
    {
      "name": "device_state",
      "values": [{"member": "DEVICE_INIT", "value": "0"}, {"member": "DEVICE_RUNNING", "value": "1"}],
      "source_file": "lib/device/device.c",
      "line": 10
    }
  ],
  "constants": [
    {"name": "MODE_ASYNC", "value_snippet": "1", "source_file": "lib/device/device.c", "line": 3}
  ],
  "typedefs": [
    {"name": "device_mode_t", "underlying_type": "", "source_file": "lib/device/device.c", "line": 5}
  ],
  "global_vars": [
    {"name": "device_name", "type": "const char *", "value_snippet": "\"default\"", "source_file": "lib/device/device.c", "line": 12}
  ]
}
```

### Globals Fields

| Field | Type | Description |
|-------|------|-------------|
| `enums[].name` | string | Enum type name |
| `enums[].values` | array | `{member, value}` pairs |
| `constants[].name` | string | Constant/define name |
| `constants[].value_snippet` | string | Value expression |
| `typedefs[].name` | string | Typedef name |
| `global_vars[].name` | string | Global variable name |
| `global_vars[].type` | string | Variable type |
| All `source_file`/`line` | string/int | Source location |

## Pre-Computed Index Files

Generated by `build` command for fast queries without re-traversing the graph.

### Reverse Index: `.code2database_reverse_index.json`

O(1) lookup of invokers/invoked per node.

```json
{
  "lib_device_device_register": {
    "invokers": [
      {"id": "lib_device_device_start", "name": "device_start", "call_order": 2, "call_condition": "if(mode==1)"}
    ],
    "invoked": []
  }
}
```

### Condition Index: `.code2database_condition_index.json`

Branch conditions per node for "which edge will be taken" queries.

```json
{
  "lib_device_device_start": [
    {"condition": "if(mode==1)", "target_node": "lib_device_device_start__cond_0", "target_name": "<conditional:if(mode==1)>", "condition_vars": [{"condition": "if(mode==1)", "vars": ["mode"]}]},
    {"condition": "!(mode==1)", "target_node": "lib_device_device_start__cond_0_else", "target_name": "<conditional:!(mode==1)>", "condition_vars": []}
  ]
}
```

### Chains Index: `.code2database_chains.json`

All simple paths from API_entry to endpoint, pre-computed at build time.

```json
{
  "total_chains": 5,
  "api_entries": 2,
  "endpoints": 3,
  "chains": [
    {
      "from_api": "lib_device_device_start",
      "to_endpoint": "pthread_create",
      "length": 2,
      "steps": [
        {"id": "lib_device_device_start", "name": "device_start", "labels": ["API_entry"], "is_empty": false, "condition": ""},
        {"id": "pthread_create", "name": "pthread_create", "labels": ["out_end"], "is_empty": false, "condition": ""}
      ]
    }
  ]
}
```

### Concurrency Index: `.code2database_concurrency_index.json`

Thread spawn relationships and concurrent execution windows.

```json
{
  "spawn_points": [
    {
      "node": "root_device_start",
      "name": "device_start",
      "spawns": [
        {
          "invoked": "pthread_create",
          "spawn_target": "worker_thread_fn",
          "spawn_arg": "NULL",
          "concurrency_type": "thread_spawn",
          "call_order": 1
        }
      ]
    }
  ],
  "thread_entries": [
    {
      "node": "root_worker_thread_fn",
      "name": "worker_thread_fn",
      "params": [{"name": "arg", "type": "void *", "is_param": true}],
      "spawned_by": [{"id": "root_device_start", "name": "device_start", "concurrency": "spawn_target"}],
      "spawn_arg": "NULL"
    }
  ],
  "concurrent_groups": [
    {
      "spawn_node": "root_device_start",
      "spawn_name": "device_start",
      "spawn_call_order": 1,
      "spawned_thread": "worker_thread_fn",
      "concurrent_with_thread": [
        {"id": "root_device_start__cond_0", "name": "<conditional:if(mode==1)>", "call_order": null}
      ],
      "concurrency_type": "thread_spawn"
    }
  ]
}
```

### Concurrency Fields

| Field | Type | Description |
|-------|------|-------------|
| `spawn_points[].node` | string | Node ID of the function that creates a thread |
| `spawn_points[].spawns[].spawn_target` | string | Name of the function that runs in the spawned thread |
| `spawn_points[].spawns[].spawn_arg` | string | Argument expression passed to the spawned function |
| `spawn_points[].spawns[].concurrency_type` | string | `"thread_spawn"`, `"goroutine"`, or `"callback_register"` |
| `thread_entries[].node` | string | Node ID of the function that runs as a thread entry |
| `thread_entries[].params` | array | Formal parameters of the thread function (with `is_param: true`) |
| `thread_entries[].spawned_by` | array | Who spawns this thread: `{id, name, concurrency}` |
| `thread_entries[].spawn_arg` | string | The argument passed to this thread function at spawn site |
| `concurrent_groups[].spawn_node` | string | The function that creates the thread |
| `concurrent_groups[].spawned_thread` | string | Name of the spawned thread function |
| `concurrent_groups[].concurrent_with_thread` | array | Calls in the parent that execute concurrently with the spawned thread |
| `concurrent_groups[].concurrency_type` | string | `"thread_spawn"`, `"goroutine"`, etc. |

## Edge Confidence & Source (Audit Trail)

Every edge carries provenance metadata indicating how it was discovered and how certain it is.

### Edge Confidence Values

| Confidence | Score | Meaning | Example |
|------------|-------|---------|---------|
| `EXTRACTED` | 1.0 | Directly observed by AST scanner — a function call in source code | `device_start()` calls `register_device()` — seen in tree-sitter AST |
| `INFERRED` | 0.7–0.95 | Added by LLM semantic enhancement or plugin | Callback target resolved by LLM: `register_callback(cb)` → `cb` is `my_handler` |
| `AMBIGUOUS` | 0.1–0.3 | Uncertain — function pointer, dynamic dispatch, macro expansion | `(*func_ptr)()` — target unknown at static analysis time |

### Edge Source Values

| Source | Meaning |
|--------|---------|
| `"ast"` | tree-sitter scanner extracted |
| `"llm"` | Claude/LLM semantic enhancement |
| `"manual"` | User-supplied |
| `"plugin:<name>"` | External plugin |

### labels_source

Node labels also carry source metadata: `"labels_source": {"thread_processor": "ast", "callback_func": "llm"}`.

### Updated Edge Schema

```json
{
  "source": "lib_device_device_start",
  "target": "lib_device_register_device",
  "call_order": 2,
  "call_condition": "if(mode==1)",
  "concurrency": "",
  "confidence": "EXTRACTED",
  "source": "ast",
  "confidence_score": 1.0
}
```

## Compact Domain File Format (v3)

Domain files now use a compact format separating summary data from details, reducing token waste:

```json
{
  "type": "code2database_domain",
  "domain": "lib.device",
  "functions": [
    ["lib_device_device_start", "device_start", "lib/device/device.c", 30, "[\"API_entry\",\"thread_processor\"]", "void device_start(int mode)"],
    ["lib_device_register_device", "register_device", "lib/device/device.c", 50, "[]", "int register_device(struct device *dev)"]
  ],
  "function_details": {
    "lib_device_device_start": {
      "location": "lib/device/device.c:30",
      "labels_source": {"thread_processor": "ast", "API_entry": "ast"},
      "api_constraints": "mode: int",
      "params": [{"name": "mode", "type": "int", "is_param": true}],
      "local_vars": [{"name": "rc", "type": "int", "value_snippet": "0", "line": 31, "is_param": false}],
      "callee_args": [{"call_order": 1, "invoked": "pthread_create", "args_snippet": "&tid, NULL, worker_thread_fn, NULL", "concurrency_info": {"is_spawn": true, "spawn_target": "worker_thread_fn", "spawn_arg": "NULL", "concurrency_type": "thread_spawn"}}],
      "condition_vars": [{"condition": "if(mode==1)", "vars": ["mode"]}]
    }
  },
  "empty_nodes": [
    ["lib_device_device_start__cond_0", "if(mode == 1)", "lib_device_device_start"]
  ],
  "edge_fields": ["source", "target", "call_order", "call_condition",
                  "concurrency", "confidence", "source_tag", "confidence_score"],
  "edges": [
    ["lib_device_device_start", "lib_device_register_device", 1, "if(mode==1)", "", "EXTRACTED", "ast", 1.0],
    ["lib_device_device_start", "lib_device_device_open", 2, "", "", "INFERRED", "llm", 0.8,
     {"pc": "#ifdef(HAVE_CONFIG_H)", "pa": true, "ev": [{"kind": "ast_call", "weight": 1.0, "note": "direct call at line 42"}]}]
  ]
}
```

### Edge position mapping (v3 compact)

| Position | Field | Description |
|----------|-------|-------------|
| 0 | `source` | Caller node ID |
| 1 | `target` | Callee node ID |
| 2 | `call_order` | Sequential order within invoker body |
| 3 | `call_condition` | Condition under which call occurs |
| 4 | `concurrency` | Concurrency type (empty = sequential) |
| 5 | `confidence` | `"EXTRACTED"`, `"INFERRED"`, or `"AMBIGUOUS"` |
| 6 | `source_tag` | Provenance: `"ast"`, `"llm"`, `"preproc_dead"`, `"plugin:<name>"` |
| 7 | `confidence_score` | 0.0–1.0 |
| 8+ | extras dict | Sparse `{pc, pa, ev}` for preproc_condition, preproc_alive, evidence |

**Important**: Position 6 is `source_tag` (provenance tag), NOT `source` (which is the invoker node ID at position 0). When loaded into the graph, `source_tag` maps to the edge attribute `source`.

### v3 format changes from v1/v2

- `functions[]` is a compact array: `[id, name, source_file, line, labels_json, signature]`
- `function_details{}` contains heavy data loaded on demand
- `empty_nodes[]` is a compact array: `[id, condition, parent_id]`
- `edges[]` use position-based arrays with `edge_fields` header (~30-40% token savings vs dict format)
- `source_tag` field separates provenance tag from invoker node ID (`source`)
- Extras dict at end of edge array stores sparse optional fields
- Legacy v1/v2 formats are still readable by `_load_full_graph()`

## Context Pack: `.code2database_context_pack.json`

Single-file LLM context for the whole project. Gives a complete mental model without needing to read multiple files.

```json
{
  "project_summary": {
    "source_root": "/path/to/source",
    "total_functions": 142,
    "total_domains": 8,
    "api_entries": ["device_start", "register_device"],
    "thread_entries": ["worker_thread_fn"],
    "callback_entries": [],
    "shallow_domains": ["module.device.nvme"],
    "deep_domains": ["lib.device"],
    "total_nodes": 200,
    "total_edges": 350
  },
  "domain_map": {
    "lib.device": {"apis": 2, "internal": 12, "ratio": 0.14, "endpoints": 5, "depends_on": ["external"]},
    "module.device.nvme": {"apis": 8, "internal": 6, "ratio": 0.57, "endpoints": 2, "depends_on": ["lib.device"]}
  },
  "api_catalog": [
    {"id": "root_device_start", "name": "device_start", "signature": "void device_start(int mode)", "domain": "root"}
  ],
  "execution_scenarios": [
    {
      "trigger": "device_start(mode=1)",
      "chain": ["device_start", "→[spawn]worker_thread_fn", "→[if(mode==1)]register_device", "→open_device"],
      "condition": "mode == 1"
    }
  ],
  "data_flow_index": {
    "mode": {"type": "int", "defined_in": "device_start(param)", "flows_to_conditions": ["if(mode==1)"], "affects_callees": ["register_device", "open_device", "close_device"]}
  },
  "concurrency_summary": {
    "spawn_points": 1,
    "concurrent_windows": [
      {"spawn_at": "lib/device/device.c:31", "spawn_fn": "device_start", "thread_fn": "worker_thread_fn", "main_thread_calls": ["register_device", "open_device"]}
    ]
  },
  "edge_confidence": {"EXTRACTED": 320, "INFERRED": 5, "AMBIGUOUS": 2}
}
```

## Execution Scenarios: `.code2database_scenarios.json`

Detailed pre-computed scenarios for each API_entry with enum-driven branch resolution.

```json
{
  "total_scenarios": 2,
  "scenarios": [
    {
      "trigger": "device_start(mode=1)",
      "binding": {"mode": "1"},
      "resolved_chain": [
        {"step": 1, "action": "spawn", "target": "worker_thread_fn", "condition": "", "branch": "", "concurrent": true, "confidence": "EXTRACTED"},
        {"step": 2, "action": "call", "target": "register_device", "condition": "if(mode==1)", "branch": "then", "concurrent": false, "confidence": "EXTRACTED"},
        {"step": 3, "action": "call", "target": "open_device", "condition": "", "branch": "", "concurrent": false, "confidence": "EXTRACTED"}
      ],
      "pruned_branches": [
        {"condition": "!(mode==1)", "dead_target": "close_device", "reason": "condition false per binding {'mode': '1'}"}
      ],
      "concurrent_window": [
        {"spawn_at": "root_device_start:1", "thread_fn": "worker_thread_fn", "main_thread_calls": ["register_device", "open_device"]}
      ]
    }
  ]
}
```

## Human-Readable Summary: `CODE2DATABASE_SUMMARY.md`

Auto-generated Markdown at `code2db-out/CODE2DATABASE_SUMMARY.md`. Contains:
- Architecture Overview (domain table with depth ratio)
- Public API Catalog (function, domain, signature, constraints)
- Concurrency Map (spawn points, concurrent calls, risk)
- Critical Paths (API→endpoint, longest first)
- External Endpoints (classification, description, invokers)
- Data Flow Hotspots (parameter flow chains)
- Edge Confidence breakdown

Per-domain `DOMAIN_README.md` files are also generated in each domain subdirectory.

## Execution Scenarios: `SCENARIOS_SUMMARY.md`

Auto-generated Markdown at `code2db-out/SCENARIOS_SUMMARY.md`. Contains a table of execution scenarios traced from API entry points:

| Column | Description |
|--------|-------------|
| # | Scenario index |
| Trigger | API entry function name |
| Path | Call chain from entry point (→ separated, up to 8 hops) |
| Concurrent | Concurrent thread/callback functions in the chain |
| Pruned Branches | Conditional branches excluded from the main path |

Machine-readable data: `.code2database_scenarios.json` with `trigger`, `resolved_chain`, `concurrent_window`, and `pruned_branches` fields.

## Architecture Flows: `ARCHITECTURE_FLOWS.md`

Auto-generated Markdown at `code2db-out/ARCHITECTURE_FLOWS.md`. Contains:

- **Top 5 Flows**: Longest invocation chains from API entries to endpoints, with conditions, concurrency, and domain crossings annotated
- **Domain Flow Map**: Cross-domain call edges ranked by volume (which domains call into which)
- **Hub Functions**: Most-connected functions by in-degree + out-degree

Generated for both NetworkX (in-memory) and SQLite (streaming/low-memory) build paths.

## Plugin Architecture

Drop-in Python scripts in `.code2database_plugins/` or specified via `--plugin` flag.

### Plugin Interface

```python
class Code2DatabasePlugin:
    def enrich_functions(self, functions, edges, source_root):
        """Add/modify function data before graph build. Return (functions, edges)."""
        return functions, edges

    def enrich_graph(self, G):
        """Modify networkx DiGraph after build. Return G."""
        return G
```

### Plugin Edge Annotation Convention

When a plugin adds edges, it should set:
- `confidence`: "INFERRED" (0.7-0.95) or "AMBIGUOUS" (0.1-0.3)
- `source`: "plugin:<name>"
- `confidence_score`: float 0.0-1.0

## Build Configuration File: `.code2database_build_config.json`

Generated by `build` command when `--build-config` is specified. Contains build system detection results and macro bindings.

```json
{
  "build_system": "cmake",
  "config_files": ["CMakeLists.txt"],
  "selected_config": "Release",
  "defined_macros": {
    "NDEBUG": "",
    "HAVE_CONFIG_H": "1",
    "FEATURE_A": "1"
  },
  "targets": [
    {"name": "mylib", "type": "library", "sources": [], "depends_on": []}
  ],
  "include_dirs": ["include", "lib"]
}
```

### Build Config Fields

| Field | Type | Description |
|-------|------|-------------|
| `build_system` | string | Detected build system: `"cmake"`, `"make"`, `"spec"`, `"meson"`, `"autotools"`, `"kconfig"`, `"bazel"`, or `""` (none detected) |
| `config_files` | string[] | Relative paths to detected build configuration files |
| `selected_config` | string | Name of selected build configuration (e.g., `"Release"`, `"Debug"`) |
| `defined_macros` | object | Macro name → value mapping. Empty string for flag macros (`-DNDEBUG`), value for valued macros (`-DVERSION=2`) |
| `targets` | array | Build targets with name, type, sources, and dependency info |
| `include_dirs` | string[] | Detected include directories |

### Preprocessor Condition Evaluation

When macro bindings are provided, the scanner evaluates `#ifdef`/`#ifndef`/`#if`/`#elif` conditions:

| Condition | Evaluation |
|-----------|------------|
| `#ifdef MACRO` | alive if MACRO is in defined_macros |
| `#ifndef MACRO` | alive if MACRO is NOT in defined_macros |
| `#if defined(MACRO)` | same as #ifdef |
| `#if MACRO` | alive if MACRO is defined and value is non-zero/non-empty |
| `#if MACRO == value` | alive if defined_macros[MACRO] equals value |
| `#if MACRO != value` | alive if defined_macros[MACRO] does not equal value |
| `#if A && B` | alive if both A and B are alive |
| `#if A \|\| B` | alive if either A or B is alive |

Conservative: if evaluation is uncertain, the branch is treated as alive (no false exclusions).

## Community Detection File: `.code2database_communities.json`

Generated by `build` command using Leiden algorithm on the invocation graph.

```json
{
  "total_communities": 5,
  "communities": [
    {
      "id": "community_0",
      "label": "lib → device",
      "heuristic_label": "lib → device",
      "keywords": ["device", "open", "close", "read"],
      "node_ids": ["lib_device_device_open", "lib_device_device_close"],
      "cohesion": 0.333,
      "symbol_count": 3
    }
  ],
  "node_community": {
    "lib_device_device_open": "community_0"
  }
}
```

### Community Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Community identifier (e.g., `"community_0"`) |
| `label` | string | Human-readable community label (from folder/name patterns) |
| `heuristic_label` | string | Same as label — generated by heuristic rules |
| `keywords` | string[] | Representative keywords extracted from member function names |
| `node_ids` | string[] | Node IDs of community members |
| `cohesion` | float | Internal edge density (0.0–1.0) |
| `symbol_count` | integer | Number of functions in the community |

When igraph/leidenalg unavailable, falls back to domain-based grouping.

## Entry Point Scores File: `.code2database_entry_scores.json`

```json
{
  "total_scored": 25,
  "top_entries": [
    {"id": "module_main", "name": "main", "score": 4.0, "domain": "module"},
    {"id": "lib_device_device_start", "name": "device_start", "score": 2.5, "domain": "lib.device"}
  ]
}
```

Score formula: `baseScore × exportMultiplier × nameMultiplier × frameworkMultiplier`

| Factor | Multiplier | Condition |
|--------|-----------|-----------|
| baseScore | invoked/(invoker+1) | always |
| exportMultiplier | 2.0 | API_entry label |
| nameMultiplier | 1.5 | handle_/on_/main/run patterns |
| nameMultiplier | 0.3 | get_/set_/is_/has patterns |
| frameworkMultiplier | 1.2–1.5 | SPDK/Django/Spring/etc. detected |

## Process Detection File: `.code2database_processes.json`

```json
{
  "total_processes": 3,
  "processes": [
    {
      "entry_point": "module_main",
      "entry_name": "main",
      "entry_score": 4.0,
      "label": "main → ... → cleanup",
      "step_count": 8,
      "steps": ["main", "init", "run", "process_data", "cleanup"],
      "step_ids": ["module_main", "module_init", ...],
      "communities_crossed": 2
    }
  ]
}
```

### Process Fields

| Field | Type | Description |
|-------|------|-------------|
| `entry_point` | string | Node ID of the entry function |
| `entry_name` | string | Function name of the entry |
| `entry_score` | float | Entry-point score |
| `label` | string | Human-readable process label |
| `step_count` | integer | Number of functions in the BFS trace |
| `steps` | string[] | Function names in trace order |
| `step_ids` | string[] | Node IDs in trace order |
| `communities_crossed` | integer | Number of Leiden communities crossed |

## Edge Evidence

Edges carry an `evidence` array documenting why the edge was emitted:

```json
"evidence": [
  {"kind": "ast_call", "weight": 1.0, "note": "direct call at line 42"},
  {"kind": "import_resolution", "weight": 0.75, "note": "resolved via #include <device.h>"}
]
```

| Kind | Weight | Description |
|------|--------|-------------|
| `ast_call` | 1.0 | Direct AST extraction — call expression in source |
| `ast_call` | 0.0 | Dead branch — preprocessor condition excludes this call |
| `import_resolution` | 0.75 | Resolved through #include chain |
| `thread_spawn` | 0.85 | Inferred thread spawn target |

Plugins can add their own evidence entries with `kind: "plugin:<name>"`.

---

## Knowledge Directory Schema

Located at `code2db-out/knowledge/`.

### index.json

```json
{
  "files": [
    {"name": "architecture.md", "size": 1234, "headings": ["Overview", "Components"]}
  ],
  "topics": ["Overview", "Components", "API Constraints"]
}
```

### knowledge_pack_lite.json (~300 tokens)

```json
{
  "files": ["architecture.md", "module_lib_device.md"],
  "topics": ["Overview", "API Constraints"],
  "architecture_summary": "... (first 500 chars of architecture.md)"
}
```

### knowledge_pack_standard.json (~800 tokens)

```json
{
  "files": [{"name": "architecture.md", "headings": ["Overview"]}],
  "architecture": "... (first 2000 chars)",
  "module_summaries": {"lib_device": "... (first 200 chars per module)"},
  "constraints": "...",
  "glossary": "..."
}
```

---

## Memory Directory Schema

Located at `code2db-out/memory/`.

### index.json

```json
{
  "entries": [
    {"id": 1, "question": "How does device init?", "tags": ["device"], "status": "trusted", "root_id": 1}
  ],
  "next_id": 5,
  "roots": [{"id": 1, "question": "How does device init?"}]
}
```

### Root Memory (root/root_<id>.json)

```json
{
  "id": 1,
  "question": "How does device init?",
  "answer": "Initialization sequence...",
  "root_id": 1,
  "tags": ["device", "init"],
  "node_ids": ["lib_device_init"],
  "status": "trusted",
  "weight": 1.5,
  "created": "2026-07-09T10:00:00",
  "last_accessed": "2026-07-09T12:00:00",
  "merged_count": 2,
  "access_count": 3,
  "reshaped_count": 0,
  "versions": [
    {"answer": "Old answer...", "version": 1, "merged_from": 2}
  ]
}
```

### Leaf Memory (leaf/mem_<id>.json)

Same format as root, but `root_id` points to the parent root.

### Layered Indexes (L0/L1/L2_index.json)

Each contains entries filtered by weight:
- L0: weight > 0.7 (hot)
- L1: weight 0.3-0.7 (warm)
- L2: weight < 0.3 (cold)

### memory_pack_lite.json (~200 tokens)

```json
{
  "top_questions": ["Q1", "Q2"],
  "hot_memories": [{"id": 1, "q": "Question", "w": 1.5}]
}
```

### memory_pack_standard.json (~600 tokens)

```json
{
  "top_questions": ["Q1", "Q2"],
  "all_hot": [{"id": 1, "q": "Question", "a": "Answer", "w": 1.5, "tags": ["tag1"]}],
  "warm_summaries": [{"id": 3, "q": "Question", "a": "Summary", "w": 0.5}]
}
```

### Scratch Memory (.scratch/session_<id>.json)

```json
{
  "session_id": "abc123",
  "chain_context": {"chains": [...], "bindings": {...}},
  "react_state": {"step": "analyze", "conclusion": "..."},
  "saved_at": "2026-07-09T10:00:00"
}
```

---

## Stale Node Schema

Nodes marked as stale by light-scan or patch-from-git have an additional attribute:

```json
{
  "stale": true,
  "semantic_desc": ""
}
```

When `describe-node` encounters a stale node with `--lazy-fill`, it auto-extracts
basic info (body_text, signature) from the source file without LLM involvement.

---

## Patch Edge Schema

Edges added by patch operations use `source_tag: "patch"`:

```json
["source_id", "target_id", null, "", "", "EXTRACTED", "patch", 1.0]
```
