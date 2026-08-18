# Endpoint Classification Pipeline

Call chain endpoints (external APIs with no source code in the project directory) are classified through this pipeline.

> **Why endpoints get first-class treatment**: in a code database, every invocation chain has to *end* somewhere — either at a function you own, or at a function you don't (a libc call, a kernel syscall, a vendor library). The pipeline tags each endpoint with `endpoint_type` (program_entry, callback_entry, external_posix, function_pointer, …) so queries like "show me all the places this code enters a kernel syscall" or "which functions are reachable from main?" return useful answers instead of a flat list of leaf nodes. Classification turns "dead-end nodes" into queryable entry/exit points.

## Step 1: Auto-mark

All invoked nodes with no source code definition are uniformly marked `out_end`.

## Step 2: Export for analysis

Builder exports all endpoints to `code2db-out/.code2database_endpoints.json`.

## Step 3: Endpoint Type Classification

External endpoints can be classified into sub-categories via configurable profile `endpoint_types` rules. This replaces a flat `out_end` classification with richer metadata that captures how each endpoint enters the system.

| endpoint_type | Pattern | Example |
|---------------|---------|---------|
| `event_handler` | Event handler entry points | Functions matching `on_*`/`handle_*` naming conventions |
| `plugin_init` | Plugin/module initialization | Functions registered via plugin init macros |
| `callback_entry` | Callback registered via vtable/struct_ops | Functions assigned to operation structure fields |
| `message_callback` | Message/callback entry | Functions registered via callback registration APIs |
| `timer_entry` | Timer callback entry | Functions registered via timer setup APIs |
| `rpc_handler` | RPC/service handler patterns | `svc_process` |
| `program_entry` | Program entry points (main, etc.) | `main` |
| `external_posix` | Standard POSIX API calls | `pthread_create`, `open`, `read` |
| `function_pointer` | Called via function pointer dispatch | `(*callback)(arg)` |

Classification uses profile `endpoint_types` rules:
```json
{
  "endpoint_types": {
    "program_entry": {
      "name_patterns": ["^main$"]
    },
    "callback_entry": {
      "registration_functions": ["register_callback", "set_handler"]
    },
    "external_posix": {
      "name_patterns": ["^pthread_", "^open$", "^read$", "^write$", "^close$"]
    }
  }
}
```

## Step 4: LLM Classification

Claude reads the endpoint list and for each endpoint judges:

- Function clear (e.g. `pthread_create` → "create child thread") → fill `external_desc`, keep `out_end`
- Function unclear (e.g. unknown external dynamic library function) → re-mark as `unknown_end`
- Function matches endpoint_type pattern → set `endpoint_type` attribute

## Step 5: Write back to graph

Run the classify-endpoints command to update graph data:

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" classify-endpoints \
  --graph code2db-out/
```

## Step 6: Report

Count `unknown_end` entries and report to user, noting which require manual confirmation.

## Unified Endpoint Definitions

All stages use a unified definition of "endpoint":
- **Broad definition** (used by `mark_endpoints`): All leaf nodes without invoked in the project = endpoints
- **Classification definition** (used by `endpoints.json`): External functions with no source code = external endpoints
- **Summary includes a legend** explaining both counts and their relationship

Endpoint categories in summary:
```
Endpoints:
  Total (no invoked in project): 88,249
  External (no source code):      422
  Classified:                     399 (94.5%)
  Unclassified (unknown_end):      23 (5.5%)

  By type:
    callback_entry:   150
    program_entry:     85
    external_posix:    72
    function_pointer:  45
    module_init:       32
    irq_entry:         15
```
