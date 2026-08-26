# Profile Manual Writing Guide

This document explains how to manually write project configuration files (profiles) for Code2Database.

> **The Profile is what makes Code2Database project-agnostic.** Instead of hardcoding "pthread_create is a thread spawn" or "file_operations is a vtable struct," the Profile lets each project declare its own conventions: callback registration patterns, export macros, struct_op_types, domain rules, lock patterns. Swap one JSON file and the same scanner + builder understand a completely different codebase. That's how the tool builds a code database for *your* project — not a generic graph that sort-of fits.

---

## 1. What is a Profile

A profile is a JSON file that declares project-specific knowledge, enabling the scanner and builder to correctly parse:
- Which function names should be skipped (e.g., macros, inline utility functions)
- Which functions are public APIs
- Callback registration mechanisms (e.g., the callback parameter of `pthread_create`)
- Macro-driven registration dispatch (e.g., `RTE_INIT` constructor macros)
- Endpoint classification rules (e.g., which functions are program entries)
- Conditional compilation macro prefixes (e.g., `CONFIG_`, `RTE_`)

**Core principle**: A profile is always deep-merged with the built-in defaults (`_default.json`). You only need to declare the parts that differ from the defaults.

---

## 2. Quick Start

### Minimal Profile

```json
{
  "version": 1,
  "project": {
    "name": "myproject"
  },
  "api_detection": {
    "public_prefixes": ["my_"]
  }
}
```

### Full Structure Overview

```json
{
  "version": 1,
  "project": { ... },
  "detection": { ... },
  "project_name_aliases": { ... },
  "skip_names": { ... },
  "api_detection": { ... },
  "callback_detection": { ... },
  "endpoint_classification": { ... },
  "macro_heuristics": { ... },
  "macro_dispatch": { ... },
  "struct_embeddings": { ... },
  "threading_models": { ... },
  "scan_hints": { ... },
  "phases": { ... }
}
```

---

## 3. Field-by-Field Reference

---

### 3.1 `version` (Required)

| Attribute | Value |
|-----------|-------|
| Type | Integer |
| Required | Yes |
| Current valid value | `1` |

```json
"version": 1
```

---

### 3.2 `project` (Project Identity)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | `""` | Project name, used for output labels and display |
| `language` | string | `"c"` | Project language — one of `"c"` (C/C++), `"go"`, `"python"`, `"java"`, `"rust"`, `"asm"`. Built-in profiles exist for each (`_default.json`, `go_default.json`, `python_default.json`, `java_default.json`, `rust_default.json`, `asm_default.json`). The `language` field is informational — the scanner auto-detects language from file extension; this field is used for profile-template matching during `auto-profile`. |
| `project_type` | string | None | Project type identifier (e.g., `"dpdk"`, `"linux_kernel"`), used for template matching |
| `detected_frameworks` | string[] | `[]` | List of detected framework names |

```json
"project": {
  "name": "dpdk",
  "language": "c",
  "project_type": "dpdk",
  "detected_frameworks": ["dpdk"]
}
```

---

### 3.3 `detection` (Auto-detection Rules, Template Only)

This section **does not affect scanning/building**; it is only used by the `auto-profile` command to detect the project type. It is typically not needed when writing profiles manually.

| Field | Type | Description |
|-------|------|-------------|
| `dir_markers` | string[] | Directories whose existence contributes score (+10 each) |
| `file_markers` | string[] | Files whose existence contributes score (+15 each) |
| `content_markers` | object[] | Content pattern detection, each item contains `pattern`, `dirs`, `min_hits` |
| `dir_structure` | object | Directory path detection, format `{key: path}` |
| `build_system` | string | Build system type (`"meson"`, `"kbuild"`, etc.) |
| `macro_prefixes` | string[] | `#ifdef` macro prefix detection (+20 on match) |
| `priority` | integer | Priority weight when scores are tied |

```json
"detection": {
  "dir_markers": ["lib", "drivers"],
  "content_markers": [
    {"pattern": "rte_eal_init", "dirs": ["lib", "drivers"], "min_hits": 1}
  ],
  "build_system": "meson",
  "macro_prefixes": ["RTE_"],
  "priority": 30
}
```

---

### 3.4 `project_name_aliases` (Project Name Aliases, Template Only)

Format: `{alias: canonical_name}`. Used by `auto-profile` to map directory names to projects.

```json
"project_name_aliases": {
  "dpdk-source": "dpdk",
  "dpdk-src": "dpdk"
}
```

---

### 3.5 `skip_names` (Skip Names) -- Key

Controls which function/macro names the scanner ignores. These names will not produce call edges.

#### `skip_names.add` -- Additional Skip Entries

| Attribute | Value |
|-----------|-------|
| Type | string[] |
| Default | ~340 common C names (malloc, free, printf, etc.) |
| Effect | Appended to the skip set |

**When to add**:
- Project-specific pure expression macros (e.g., `RTE_MIN`, `RTE_MAX`, `RTE_DIM`)
- Assert/panic macros (e.g., `rte_panic`, `RTE_ASSERT`)
- Alignment attribute macros (e.g., `__rte_cache_aligned`, `__rte_aligned`)
- Byte-order conversion functions (e.g., `rte_cpu_to_be_16`, `rte_be_to_cpu_32`)
- Logging macros (e.g., `RTE_LOG_DP`, `PMD_DRV_LOG`)

```json
"skip_names": {
  "add": [
    "RTE_MIN", "RTE_MAX", "RTE_DIM",
    "rte_panic", "RTE_ASSERT",
    "__rte_cache_aligned", "__rte_aligned",
    "rte_cpu_to_be_16", "rte_cpu_to_be_32",
    "rte_be_to_cpu_16", "rte_be_to_cpu_32"
  ]
}
```

#### `skip_names.external_lib_prefixes` -- External Library Prefixes

| Attribute | Value |
|-----------|-------|
| Type | `{prefix: {category: string, visible: bool}}` |
| Default | `pthread_`, `sem_`, `uuid_`, `numa_`, `aio_`, etc. |
| Effect | `visible: true` -> creates an edge to an external endpoint; `visible: false` -> silently skipped |

**When to configure**:
- External libraries the project depends on should be declared here
- `category` is used for endpoint classification (e.g., `external_posix`, `external_openssl`, `external_vendor`)
- `visible: true` means these calls produce external endpoint nodes (visible in the invocation graph)
- `visible: false` means silently ignored (no nodes or edges produced)

```json
"external_lib_prefixes": {
  "pthread_": {"category": "external_posix", "visible": false},
  "mlx5_": {"category": "external_vendor", "visible": true},
  "ibv_": {"category": "external_lib", "visible": true},
  "SSL_": {"category": "external_openssl", "visible": true}
}
```

#### `skip_names.test_framework_prefixes` -- Test Framework Prefixes

| Attribute | Value |
|-----------|-------|
| Type | string[] |
| Default | `[]` |
| Effect | Test framework function prefixes, used to mark test code |

```json
"test_framework_prefixes": ["CU_"]
```

---

### 3.6 `api_detection` (API Detection) -- Key

#### `api_detection.public_prefixes` -- Public API Prefixes

| Attribute | Value |
|-----------|-------|
| Type | string[] |
| Default | `[]` |
| Effect | Marks functions starting with these prefixes as public API entry points |

```json
"public_prefixes": ["rte_"]
```

#### `api_detection.internal_patterns` -- Internal Markers

| Attribute | Value |
|-----------|-------|
| Type | string[] |
| Default | `["_unit_", "_ut_", "_test_", "_perf_", "_verify_", "_example_", "_internal", "_priv", "_stub", "_mock"]` |
| Effect | When a function name contains these substrings, it is marked as internal/test code (not a public API) |

**When to modify**: If the project has special internal marking prefixes (e.g., `__rte_`), add them here.

```json
"internal_patterns": ["_unit_", "_ut_", "_test_", "__rte_"]
```

#### `api_detection.public_header_paths` -- Public Header Paths

| Attribute | Value |
|-----------|-------|
| Type | string[] (paths relative to the source root) |
| Default | `[]` |
| Effect | Directories containing public headers, used for LLM analysis and API surface identification |

```json
"public_header_paths": [
  "lib/eal/include",
  "lib/mempool/include",
  "lib/net/include",
  "lib/mbuf/include"
]
```

#### `api_detection.export_macros` -- Export Macros

| Attribute | Value |
|-----------|-------|
| Type | string[] |
| Default | `[]` |
| Effect | Macro names that mark exported symbols; the scanner recognizes functions wrapped by these macros as entry points |

```json
"export_macros": ["EXPORT_SYMBOL", "EXPORT_SYMBOL_GPL"]
```

#### `api_detection.struct_op_types` -- Vtable Struct Types

| Attribute | Value |
|-----------|-------|
| Type | string[] |
| Default | `[]` |
| Effect | Struct names containing function pointer tables (vtables); the scanner creates implicit call edges for their function pointers |

```json
"struct_op_types": [
  "file_operations",
  "inode_operations",
  "platform_driver",
  "net_device_ops"
]
```

#### `api_detection.auto_detect` -- Auto-detect Flag

| Attribute | Value |
|-----------|-------|
| Type | boolean |
| Default | `false` |
| Effect | Marks this profile as auto-generated, allowing the scanner/builder to supplement it dynamically |

When writing a profile manually, set this to `false` (or omit it, as the default is `false`).

---

### 3.7 `callback_detection` (Callback Detection) -- Most Critical

This is the **most impactful** section of the profile. Correctly declaring callback patterns directly affects whether the invocation graph can identify function pointer invocation chains.

#### `callback_detection.static_patterns` -- Static Callback Patterns

| Attribute | Value |
|-----------|-------|
| Type | object[] |
| Default | `[pthread_create pattern]` |
| Effect | Defines callback registration functions and their parameter patterns |

Each entry contains 4 required fields:

| Field | Type | Description |
|-------|------|-------------|
| `register_func` | string | Name of the callback registration function |
| `regex` | string | Regular expression to extract the callback function name from the invocation site (must have one capture group) |
| `cb_arg_index` | integer | 0-based position of the callback argument |
| `concurrency_type` | string | Concurrency type |

**concurrency_type options**:

| Value | Meaning |
|-------|---------|
| `spawn_target` | Entry function that creates a new thread/process |
| `callback` | General callback registration (interrupts, signals, etc.) |
| `callback_register` | Callback registration function |
| `poller` | Polling callback (timers, work items) |
| `timer_callback` | Timer callback |

**How to write regex**:
- The regex must match the function call statement, with the callback function name captured by `(\w+)`
- Other arguments are skipped using `[^,]*` or `[^,)]+`
- Example: `pthread_create(&thread, NULL, my_thread_fn, arg)` -> regex: `pthread_create\s*\(\s*[^,]*,\s*[^,]*,\s*(\w+)`

```json
"static_patterns": [
  {
    "register_func": "pthread_create",
    "regex": "pthread_create\\s*\\(\\s*[^,]*,\\s*[^,]*,\\s*(\\w+)",
    "cb_arg_index": 2,
    "concurrency_type": "spawn_target"
  },
  {
    "register_func": "rte_eal_mp_remote_launch",
    "regex": "rte_eal_mp_remote_launch\\s*\\(\\s*(\\w+)",
    "cb_arg_index": 0,
    "concurrency_type": "spawn_target"
  },
  {
    "register_func": "request_irq",
    "regex": "request_irq\\s*\\(\\s*[^,]*,\\s*(\\w+)",
    "cb_arg_index": 1,
    "concurrency_type": "callback"
  },
  {
    "register_func": "timer_setup",
    "regex": "timer_setup\\s*\\(\\s*[^,]*,\\s*(\\w+)",
    "cb_arg_index": 1,
    "concurrency_type": "poller"
  }
]
```

#### `callback_detection.generic_cb_suffixes` -- Generic Callback Suffixes

| Attribute | Value |
|-----------|-------|
| Type | string[] |
| Default | `["_cb", "_fn", "_handler", "_callback"]` |
| Effect | When a function name ends with these suffixes, it is heuristically identified as a callback function |

Usually does not need modification.

#### `callback_detection.skip_call_prefixes` -- Skip Call Prefixes

| Attribute | Value |
|-----------|-------|
| Type | string[] |
| Default | `[]` |
| Effect | Excludes calls with specific prefixes from callback detection |

```json
"skip_call_prefixes": ["trace_"]
```

#### `callback_detection.skip_callees` -- Skip Callee Functions

| Attribute | Value |
|-----------|-------|
| Type | string[] |
| Default | `[]` |
| Effect | Skips specific invoked function names in cross-file callback detection |

---

### 3.8 `endpoint_classification` (Endpoint Classification)

#### `endpoint_classification.lib_prefix_map` -- Library Prefix Map

| Attribute | Value |
|-----------|-------|
| Type | `{prefix: classification_name}` |
| Default | `{"pthread_": "external_posix", "sem_": "external_posix", "epoll_": "external_posix"}` |
| Effect | Maps function prefixes to endpoint classification categories, used by the builder to tag external endpoints |

```json
"lib_prefix_map": {
  "pthread_": "external_posix",
  "mlx5_": "external_vendor",
  "ibv_": "external_lib",
  "SSL_": "external_openssl"
}
```

#### `endpoint_classification.endpoint_rules` -- Endpoint Rules

| Attribute | Value |
|-----------|-------|
| Type | object[], each item contains `pattern` (regex) and `endpoint_type` |
| Default | `[]` |
| Effect | Uses regex to match function names and marks specific functions as endpoints |

```json
"endpoint_rules": [
  {"pattern": "^main$", "endpoint_type": "program_entry"},
  {"pattern": "^rte_eal_init$", "endpoint_type": "program_entry"},
  {"pattern": "^rte_eal_mp_remote_launch$", "endpoint_type": "thread_entry"}
]
```

---

### 3.9 `macro_heuristics` (Macro Heuristics)

#### `macro_heuristics.macro_condition_prefixes` -- Conditional Compilation Macro Prefixes

| Attribute | Value |
|-----------|-------|
| Type | string[] |
| Default | `["HAVE_", "ENABLE_", "WITH_", "USE_"]` |
| Effect | `#ifdef` conditional macro prefixes; the scanner recognizes these as conditional compilation guards rather than function calls |

```json
"macro_condition_prefixes": ["RTE_", "HAVE_", "ENABLE_", "WITH_", "USE_"]
```

---

### 3.10 `macro_dispatch` (Macro Dispatch) -- Key

#### `macro_dispatch.registration_macros` -- Registration Macros

| Attribute | Value |
|-----------|-------|
| Type | object[] |
| Default | `[]` |
| Effect | Defines constructor-based registration macros for automatic module/driver registration |

**Required fields**:

| Field | Type | Description |
|-------|------|-------------|
| `macro_name` | string | Macro name |
| `pattern` | string | Regex matching the macro invocation (capture groups extract arguments) |
| `struct_arg_index` | integer >= 0 | Which capture group contains the struct/entry variable name |

**Optional fields**:

| Field | Type | Description |
|-------|------|-------------|
| `register_func` | string | Registration function name called within the constructor body |
| `global_list_var` | string | Global linked list variable name |
| `iterator_func` | string | Function that iterates over the global list |
| `dispatch_field` | string | Dispatch field name called during iteration |
| `handler_arg_index` | integer | Handler argument position |
| `dispatch_caller` | string | Dispatch invoker function name |
| `generates` | string | Generation type: `constructor`, `driver_register`, `bus_register`, etc. |
| `_confidence` | string | Confidence level: `high`, `medium`, `low` |
| `_needs_review` | boolean | Whether manual review is needed |

```json
"registration_macros": [
  {
    "macro_name": "RTE_INIT",
    "pattern": "RTE_INIT\\s*\\(\\s*(\\w+)\\s*\\)",
    "struct_arg_index": 0,
    "generates": "constructor",
    "_confidence": "high"
  },
  {
    "macro_name": "RTE_PMD_REGISTER_PCI",
    "pattern": "RTE_PMD_REGISTER_PCI\\s*\\(\\s*([^,)]+)\\s*,\\s*([^,)]+)\\s*\\)",
    "struct_arg_index": 1,
    "register_func": "rte_pci_register",
    "iterator_func": "rte_pci_find_device",
    "generates": "driver_register",
    "_confidence": "high"
  }
]
```

#### `macro_dispatch.token_paste_macros` -- Token Paste Macros

| Attribute | Value |
|-----------|-------|
| Type | object[] |
| Default | `[]` |
| Effect | Macros that use the `##` paste operator to generate function names |

**Required fields**:

| Field | Type | Description |
|-------|------|-------------|
| `macro_name` | string | Macro name |
| `template` | string | `##` expression (e.g., `"name##_register"`) |
| `param_names` | string[] | List of macro parameter names |

```json
"token_paste_macros": [
  {
    "macro_name": "RTE_INIT",
    "template": "RTE_INIT##_func",
    "param_names": ["name"],
    "generates": "constructor"
  }
]
```

#### `macro_dispatch.macro_aliases` -- Macro Aliases

| Attribute | Value |
|-----------|-------|
| Type | `{macro_name: expansion_target}` |
| Default | `{}` |
| Effect | Simple macro-to-target mapping |

```json
"macro_aliases": {}
```

---

### 3.11 `struct_embeddings` (Struct Embeddings)

#### `struct_embeddings.container_of_macros` -- container_of Macros

| Attribute | Value |
|-----------|-------|
| Type | object[] |
| Default | `[]` |
| Effect | `container_of`-style macro definitions, used for pointer arithmetic resolution |

```json
"container_of_macros": [
  {"macro_name": "container_of"}
]
```

#### `struct_embeddings.manual_entries` -- Manual Embedding Relationships

| Attribute | Value |
|-----------|-------|
| Type | object[] |
| Default | `[]` |
| Effect | Manually declared struct embedding relationships |

**Required fields**: `outer_type`, `member`, `inner_type`
**Optional field**: `domain_hint`

```json
"manual_entries": [
  {
    "outer_type": "my_device",
    "member": "base",
    "inner_type": "device_base",
    "domain_hint": "drivers"
  }
]
```

---

### 3.12 `threading_models` (Threading Models)

| Attribute | Value |
|-----------|-------|
| Type | `{model_name: [function_name_list]}` |
| Default | `{}` |
| Effect | Defines thread/concurrency models for concurrency analysis |

```json
"threading_models": {
  "kernel_thread": ["kthread_create", "kthread_run", "kthread_create_on_node"],
  "workqueue": ["INIT_WORK", "INIT_DELAYED_WORK"]
}
```

---

### 3.13 `scan_hints` (Scan Hints)

#### `scan_hints.domain_rules` -- Domain Rules

| Attribute | Value |
|-----------|-------|
| Type | object[] |
| Default | `[]` |
| Effect | Assigns domain suffixes/tags/merge targets based on function name patterns |

**Required field**: `pattern` (regex)
**Must include at least one action field**: `domain_suffix`, `domain_tag`, `merge_to`, `label`

```json
"domain_rules": [
  {"pattern": "ext4_mb_.*", "domain_suffix": "mballoc"},
  {"pattern": "__ext4_.*", "domain_suffix": "internal"},
  {"pattern": "^app\\.test-", "domain_tag": "test"},
  {"pattern": "^lib\\.eal\\.", "label": "core_eal"}
]
```

#### `scan_hints.header_priority_dirs` -- Header Priority Directories

| Attribute | Value |
|-----------|-------|
| Type | string[] |
| Default | `["include"]` |
| Effect | Directories to scan with priority during LLM analysis and API detection |

#### `scan_hints.vtable_module_keys` -- Vtable Module Keys

| Attribute | Value |
|-----------|-------|
| Type | string[] |
| Default | `[]` |
| Effect | Field names in vtable structs that identify the owning module |

```json
"vtable_module_keys": ["owner"]
```

---

### 3.14 `concurrency_patterns` (Lock Patterns) -- Optional

| Attribute | Value |
|-----------|-------|
| Type | `{lock_acquire_patterns: string[], lock_release_patterns: string[]}` |
| Default | `{"lock_acquire_patterns": [], "lock_release_patterns": []}` |
| Effect | Lock acquire/release regex patterns; each entry is a raw regex with one capture group for the lock variable (or no group for locks like `rcu_read_lock`). Auto-profile detects project-specific lock APIs by scanning source code for common lock function names. Built-in reference profiles (linux_kernel.json, spdk.json) populate these with project-appropriate patterns. |

```json
"concurrency_patterns": {
  "lock_acquire_patterns": ["mutex_lock\\s*\\(\\s*&?(\\w+)",
                            "spin_lock\\s*\\(\\s*&?(\\w+)"],
  "lock_release_patterns": ["mutex_unlock\\s*\\(\\s*&?(\\w+)",
                            "spin_unlock\\s*\\(\\s*&?(\\w+)"]
}
```

---

### 3.15 `guard_functions` (Runtime Guard Semantics) -- Optional, Fix #6

| Attribute | Value |
|-----------|-------|
| Type | object[] |
| Default | `[]` |
| Effect | Declares project-specific runtime guard functions so `path-feasible` / `path-guards` / `describe-node` can annotate conditions with project-specific guard meaning instead of relying solely on the hardcoded regex patterns in `runtime_guards.py` (which cover common Linux kernel predicates like `sb_is_blkdev_sb` / `PageUptodate` but cannot generalize to arbitrary projects). When the profile is supplied via `--profile`, the declared guards augment (not replace) the built-in regex matches. |

**Required fields**: `function`, `kind`
**Optional fields**: `effect`, `arg_index`, `description`

| Field | Type | Description |
|-------|------|-------------|
| `function` | string | The guard function name (e.g., `"sb_is_blkdev_sb"`) |
| `kind` | enum | One of `"type_predicate"`, `"identity_predicate"`, `"lock_state"`, `"acquire"`, `"release"` |
| `effect` | string | For `type_predicate`: the type tag asserted when the predicate returns true (e.g., `"blkdev"`). For `acquire`/`release`/`lock_state`: the lock object whose state changes. For `identity_predicate`: unused (var/value are read from the call site). |
| `arg_index` | int | 0-based index of the argument the predicate tests (for `type_predicate`) or the lock object (for `acquire`/`release`/`lock_state`). Default 0. |
| `description` | string | Human-readable explanation shown in CLI output. |

```json
"guard_functions": [
  {"function": "sb_is_blkdev_sb", "kind": "type_predicate",
   "effect": "blkdev", "arg_index": 0,
   "description": "returns true when arg0 is a block-device superblock"},
  {"function": "bd_prepare_to_claim", "kind": "acquire",
   "effect": "bd_holder", "arg_index": 0,
   "description": "acquires exclusive holder lock on arg0 (bdev)"},
  {"function": "bd_abort_claim", "kind": "release",
   "effect": "bd_holder", "arg_index": 0,
   "description": "releases exclusive holder lock on arg0 (bdev)"},
  {"function": "mutex_is_locked", "kind": "lock_state",
   "effect": "arg0_lock", "arg_index": 0,
   "description": "returns true when the mutex at arg0 is held"}
]
```

**CLI usage**: pass `--profile /path/to/profile.json` to `path-feasible` or `path-guards`. The runtime guard analysis output will include `profile_bindings` listing bindings inferred specifically from profile-declared guards (e.g., `{"sb_type": "blkdev"}`).

---

### 3.16 `allocation_sites` (Object Origin Tracking) -- Optional, Fix #7

| Attribute | Value |
|-----------|-------|
| Type | object[] |
| Default | `[]` |
| Effect | Declares project-specific allocation functions so `field-access` / `field-flow` / `describe-node` can annotate writer/reader entries with `object_origin_type`. Without this, the skill can identify that `bh = alloc_buffer_head()` initializes `bh` but cannot distinguish a `buffer_head` allocated by `alloc_buffer_head` from one obtained from `jh->bh` (which may belong to a different address_space). With this declaration, `_trace_object_origin` returns origins in the form `"<call_expr>:<object_type>"` (e.g., `"alloc_buffer_head(...):buffer_head"`), enabling the agent to reason about object identity across same-typed-different-instance variables. |

**Required fields**: `function`, `object_type`
**Optional fields**: `arg_index`, `description`

| Field | Type | Description |
|-------|------|-------------|
| `function` | string | The allocation function name (e.g., `"alloc_buffer_head"`, `"kmalloc"`, `"kmem_cache_alloc"`) |
| `object_type` | string | The type tag of the returned object (e.g., `"buffer_head"`, `"void_ptr"`) |
| `arg_index` | int | Index of the initializer argument if the function returns a wrapper/pointer to an allocated subfield. `-1` (default) means the function itself returns the object directly (no initializer arg). |
| `description` | string | Human-readable explanation. |

```json
"allocation_sites": [
  {"function": "alloc_buffer_head", "object_type": "buffer_head",
   "arg_index": -1,
   "description": "allocates and returns a new buffer_head"},
  {"function": "kmem_cache_alloc", "object_type": "page_cache_page",
   "arg_index": 0,
   "description": "arg0 is the kmem_cache that determines the allocated type"}
]
```

**Behavior**: When `build` is invoked with `--profile /path/to/profile.json`, the builder populates a module-level map from `function` → `object_type`. During graph construction, `_extract_state_access` calls `_trace_object_origin` for each field write/read; when the trace encounters a call to a declared allocation function, the resulting origin is annotated with the object_type. This annotation surfaces in:

- `field-access --struct buffer_head --field b_bdev` — writer/reader entries include `object_origin` field
- `field-flow --field b_bdev --value NULL` — writer list groups by object origin
- `describe-node <function>` — `fields_written` / `fields_read` entries include `object_origin`

**When to declare**: Declare allocation sites when the project has type-ambiguous allocation patterns — e.g., multiple factory functions returning the same nominal type but representing different object identities (kernel `buffer_head` from different address_spaces, `task_struct` from different namespaces). For simple projects with one allocation site per type, this is unnecessary (the type itself is sufficient identity).

---

### 3.17 `lock_semantics` (Lock-Holder Edges) -- Optional, Fix #10

| Attribute | Value |
|-----------|-------|
| Type | object[] |
| Default | `[]` |
| Effect | Declares project-specific lock primitives (function-name-based, complementary to the regex-based `concurrency_patterns.lock_acquire_patterns`). When the profile is supplied via `--profile`, the builder emits HOLDER edges linking a locked object to the function context that holds its lock. `detect-races` / `path-guards` / `describe-node` can then annotate race evidence with "writer holds `<lock>` on `<object>`" context — turning a soft "this writer is guarded" signal into a hard, edge-traceable proof. |

**Required fields**: `function`, `kind`
**Optional fields**: `arg_index`, `locks_object_at`, `description`

| Field | Type | Description |
|-------|------|-------------|
| `function` | string | The lock primitive name (e.g., `"mutex_lock"`, `"spin_lock"`, `"down_write"`, `"rcu_read_lock"`) |
| `kind` | enum | `"acquire"` or `"release"` |
| `arg_index` | int | 0-based index of the lock-object argument. Default 0. |
| `locks_object_at` | int | 0-based index of the argument that identifies the *protected* object. `-1` (default) means unknown — the lock is recorded without a HOLDER edge. For `mutex_lock(&sb->s_lock)`, the protected object is `sb` (head of the lock-var chain). |
| `description` | string | Human-readable explanation. |

```json
"lock_semantics": [
  {"function": "mutex_lock", "kind": "acquire",
   "arg_index": 0, "locks_object_at": 0,
   "description": "arg0 is &sb->s_lock; protected object is the head 'sb'"},
  {"function": "mutex_unlock", "kind": "release",
   "arg_index": 0, "locks_object_at": 0,
   "description": "releases the mutex acquired by mutex_lock"},
  {"function": "down_write", "kind": "acquire",
   "arg_index": 0, "locks_object_at": 0,
   "description": "acquires write-side rwsem; protected object is the head of the chain"},
  {"function": "rcu_read_lock", "kind": "acquire",
   "arg_index": -1, "locks_object_at": -1,
   "description": "RCU read-side critical section (no specific lock object)"}
]
```

**Behavior**: When `build` is invoked with `--profile /path/to/profile.json`, `detect_semantic_edges` matches each declared acquire primitive by function name and extracts the args at `arg_index` (the lock) and `locks_object_at` (the protected object). The lock arg's leading `&`/`*` is stripped; the object arg is reduced to its head variable (e.g., `sb` from `sb->s_lock`). A HOLDER edge is emitted from the protected object to the calling function, with `lock_function` and `lock_variable` recorded as edge attributes.

**Complements `concurrency_patterns`**: `concurrency_patterns.lock_acquire_patterns` uses regex for flexible call-site matching and powers `lock-coverage` / `who-locks`. `lock_semantics` uses exact function names and powers HOLDER-edge emission. Declare both for full lock reasoning.

**When to declare**: Declare when the project has structured lock APIs where the protected object is identifiable from the call site (e.g., `mutex_lock(&obj->lock)` protects `obj`). Skip for global locks with no identifiable protected object (`rcu_read_lock()` with no args) — set `locks_object_at` to `-1` for those.

---

### 3.18 `io_classification` (I/O Keywords) -- Optional

| Attribute | Value |
|-----------|-------|
| Type | `{io_main_keywords: string[], io_side_keywords: string[]}` |
| Default | `{"io_main_keywords": [], "io_side_keywords": []}` |
| Effect | Keywords for classifying functions as I/O-side (storage/network/IO backends) vs I/O-main (front-end handlers). Used by `io-path` command. Auto-profile detects project-specific terminology by analyzing function names in I/O-related source directories. |

---

### 3.19 `dispatch_tuning` (Dispatch Heuristics) -- Optional

Tunes vtable dispatch precision. All sub-fields have sensible defaults; override only when dispatch detection produces false positives/negatives.

| Sub-field | Type | Default | Effect |
|-----------|------|---------|--------|
| `max_vtable_dispatch_per_call` | int | `50` | Max vtable dispatch targets per call site. Raise for kernel `file_operations` where some fields have >50 registrations. |
| `max_vtable_dispatch_per_field` | `{field: int}` | `{}` | Per-field overrides, e.g., `{"write_iter": 100, "read_iter": 100}`. |
| `inline_wrapper_patterns` | string[] | `[r"^(?:__)?(?:call\|invoke)_(\\w+)$"]` | Regex patterns matching inline wrapper function names. First capture group is the underlying field name. |
| `macro_bridge_patterns` | `{pattern, impl}[]` | `[{"pattern": "^(\\w+)$", "impl": "__{1}"}]` | Macro-name regex → implementation-name template (use `{1}` for captured group). |
| `macro_bridge_require_same_domain` | bool | `true` | Whether to require same-domain for macro bridge. Set `false` for kernel where headers and impls sometimes live in different subdirectories. |
| `fn_ptr_call_require_evidence` | bool | `false` | When `true`, only treat callee as `fn_ptr_call` if there's evidence of `&func` address-of or struct assignment in the same function. When `false`, use legacy name-suffix heuristic. |

---

### 3.20 `project_boundaries` (Path Filters) -- Optional

Source-path substrings that mark code as non-API / test / vendor / external. These are generic cross-project conventions; rarely need overriding.

| Sub-field | Type | Default | Effect |
|-----------|------|---------|--------|
| `non_api_paths` | string[] | `[]` | Source-path substrings marking non-API code (e.g., `tools/`, `scripts/`, `selftests/`). Functions whose `source_file` contains any of these are NOT labeled `API_entry`. |
| `test_path_patterns` | string[] | (generic) | Source-path substrings marking test code. |
| `test_file_suffixes` | string[] | (generic) | Test file suffixes used by `_is_test_source`. |
| `test_domain_segments` | string[] | (generic) | Domain segments marking test/unit domains. |
| `vendor_domain_prefixes` | string[] | `[]` | Domain prefixes marking vendor/external code. EMPTY by default — auto-profile detects project-specific vendor prefixes. |
| `external_dir_prefixes` | string[] | (generic) | Common external directory names (`vendor`, `third_party`, etc.). |

---

### 3.21 `phases` (Phase Tracking)

| Field | Default | Description |
|-------|---------|-------------|
| `prescan_completed` | `false` | Whether the pre-scan phase is complete |
| `test_scan_completed` | `false` | Whether the test scan phase is complete |
| `llm_header_analysis_completed` | `false` | Whether LLM header analysis is complete |
| `llm_result_check_completed` | `false` | Whether LLM result checking is complete |

When writing a profile manually, these typically do not need to be set; keep the default `false`.

---

## 4. Writing Workflow

### Step 1: Run auto-profile to Get a Baseline

```bash
python3 scripts/code2database_scanner.py auto-profile \
  --source /path/to/project --outdir /path/to/project
```

This generates `.code2database_profile.json` as a starting point.

### Step 2: Review and Supplement Against the Following Checklist

| Check Item | Action |
|------------|--------|
| `skip_names.add` | Review project-specific macros and inline functions; add names not in the default list |
| `callback_detection.static_patterns` | **Most important**: Search for all callback registration APIs in the project and write a pattern for each |
| `macro_dispatch.registration_macros` | Search for all constructor and registration macros and write a pattern for each |
| `api_detection.public_prefixes` | Confirm public API prefixes are correct |
| `api_detection.export_macros` | Check if the project has custom export macros |
| `api_detection.struct_op_types` | Check for vtable structs |
| `endpoint_classification.endpoint_rules` | Add rules for program entries and special endpoints |
| `scan_hints.domain_rules` | Add rules when function names have clear grouping patterns |

### Step 3: Validate the Profile

```bash
python3 scripts/code2database_scanner.py validate-profile \
  --profile /path/to/profile.json
```

Validation checks:
- All required sections are present
- Field types are correct
- Regular expressions are valid
- No unknown keys (prevents typos)

### Step 4: Scan Using the Profile

```bash
python3 scripts/code2database_scanner.py scan \
  --source /path/to/project \
  --profile /path/to/profile.json \
  --output code2db-out/.code2database_extraction.json
```

---

## 5. Profile Templates for Common Project Types

### 5.1 Standard C Library Project

```json
{
  "version": 1,
  "project": {"name": "myproject"},
  "api_detection": {"public_prefixes": ["my_"]},
  "callback_detection": {
    "static_patterns": [
      {
        "register_func": "pthread_create",
        "regex": "pthread_create\\s*\\(\\s*[^,]*,\\s*[^,]*,\\s*(\\w+)",
        "cb_arg_index": 2,
        "concurrency_type": "spawn_target"
      }
    ]
  }
}
```

### 5.2 Linux Kernel Module

```json
{
  "version": 1,
  "project": {"name": "linux_kernel", "project_type": "linux_kernel"},
  "api_detection": {
    "public_prefixes": [],
    "export_macros": ["EXPORT_SYMBOL", "EXPORT_SYMBOL_GPL"],
    "struct_op_types": ["file_operations", "platform_driver", "net_device_ops"]
  },
  "callback_detection": {
    "static_patterns": [
      {"register_func": "kthread_run", "regex": "kthread_run\\s*\\(\\s*(\\w+)", "cb_arg_index": 0, "concurrency_type": "spawn_target"},
      {"register_func": "request_irq", "regex": "request_irq\\s*\\(\\s*[^,]*,\\s*(\\w+)", "cb_arg_index": 1, "concurrency_type": "callback"},
      {"register_func": "timer_setup", "regex": "timer_setup\\s*\\(\\s*[^,]*,\\s*(\\w+)", "cb_arg_index": 1, "concurrency_type": "poller"}
    ],
    "skip_call_prefixes": ["trace_"]
  },
  "struct_embeddings": {"container_of_macros": [{"macro_name": "container_of"}]},
  "threading_models": {"kernel_thread": ["kthread_create", "kthread_run"]},
  "scan_hints": {"vtable_module_keys": ["owner"]}
}
```

### 5.3 Meson-built C Project (e.g., DPDK/SPDK Style)

```json
{
  "version": 1,
  "project": {"name": "myproject", "project_type": "myproject"},
  "detection": {
    "dir_markers": ["lib", "drivers"],
    "build_system": "meson",
    "macro_prefixes": ["MY_"]
  },
  "skip_names": {
    "add": ["MY_MIN", "MY_MAX", "my_panic", "MY_ASSERT"]
  },
  "api_detection": {
    "public_prefixes": ["my_"],
    "export_macros": ["MY_EXPORT"],
    "struct_op_types": []
  },
  "callback_detection": {
    "static_patterns": [
      {"register_func": "pthread_create", "regex": "pthread_create\\s*\\(\\s*[^,]*,\\s*[^,]*,\\s*(\\w+)", "cb_arg_index": 2, "concurrency_type": "spawn_target"}
    ]
  },
  "endpoint_classification": {
    "endpoint_rules": [{"pattern": "^my_init$", "endpoint_type": "program_entry"}]
  }
}
```

---

## 6. Practical Tips for Writing Callback Patterns

### 6.1 How to Find Callback Registration APIs in a Project

1. **Search header files for function declarations** with function pointer parameters:
   ```bash
   grep -rn '(\*.*)(void)' include/ --include='*.h' | grep -i 'register\|callback\|launch\|create'
   ```

2. **Search source files for usage patterns**:
   ```bash
   grep -rn 'some_register_func(' lib/ --include='*.c' | head -20
   ```

3. **Look for constructor macro definitions**:
   ```bash
   grep -rn '__attribute__((constructor))' --include='*.h'
   grep -rn '#define.*REGISTER\|INIT' --include='*.h'
   ```

### 6.2 Regex Writing Tips

- **Basic principle**: Match the function call syntax, capture the callback name with `(\w+)`, skip other arguments with `[^,]*` or `[^,)]+`
- **Multi-argument examples**:
  - `foo(a, cb, c)` -> `foo\s*\(\s*[^,]*,\s*(\w+)\s*,\s*[^,]*\)`
  - `bar(cb)` -> `bar\s*\(\s*(\w+)\s*\)`
  - `baz(x, y, cb)` -> `baz\s*\(\s*[^,]*,\s*[^,]*,\s*(\w+)`
- **Notes**:
  - You do not need to match the closing parenthesis (the scanner tolerates this)
  - `cb_arg_index` is the 0-based argument position
  - If the callback argument position is not fixed, you need to write a separate pattern for each usage

### 6.3 Concurrency Type Selection Guide

| Scenario | concurrency_type |
|----------|-----------------|
| Creating a new thread | `spawn_target` |
| Registering an interrupt handler | `callback` |
| Registering a timer callback | `poller` |
| Submitting work to a thread pool | `callback_register` |
| Registering an event notification | `callback` |

---

## 7. Common Errors and Troubleshooting

| Error | Cause | Solution |
|-------|-------|----------|
| `ValueError: Unsupported profile version` | `version` field is not `1` | Set to `1` |
| `ValueError: Missing required section` | Missing a required section | Add the missing section (at least as an empty object) |
| `ValueError: ... must be a dict with 'category' key` | Incorrect format for `external_lib_prefixes` value | Change to `{"category": "xxx", "visible": true/false}` |
| `ValueError: ... is not a valid regex` | Invalid regular expression syntax | Check escape characters (`\\` represents `\` in JSON) |
| Callback functions not connected to the invocation graph | `static_patterns` is missing the callback pattern | Add the corresponding registration function pattern |
| Excessive external function node noise | Too many prefixes with `visible: true` in `external_lib_prefixes` | Change to `visible: false` |
| Conditionally compiled functions treated as real calls | `macro_condition_prefixes` is missing the project's macro prefixes | Add the corresponding `#ifdef` prefixes |

---

## 8. Profile Storage Locations

| Location | Description |
|----------|-------------|
| `Project root/.code2database_profile.json` | auto-profile default output location; scanner automatically looks here |
| `Any path` | Specified via the `--profile` parameter |
| `scripts/config/profiles/<type>.json` | Built-in templates (used by auto-profile; do not modify directly) |
