# Label Rules Reference

> **Labels are the queryable role of a function in the codebase.** Seven labels — `API_entry`, `thread_processor`, `callback_func`, `constructor`, `destructor`, `out_end`, `unknown_end` — classify *what kind of thing* a function is, not just where it lives. A query for "all public APIs" filters on `API_entry`; "all thread entry points" filters on `thread_processor`; "all callbacks" filters on `callback_func`. Without labels, you'd have to grep for naming conventions (unreliable across projects and languages). With labels, the graph answers role-based questions directly — that's the database advantage.

## Function Labels

| Label | Meaning | C/C++ | Go | Python | Java | Rust |
|-------|---------|-------|-----|--------|------|------|
| `thread_processor` | Thread execution entry | `pthread_create` etc. | `go func()` | `Thread(target=)` | `new Thread()` | `thread::spawn` |
| `callback_func` | Callback entry function | callback parameter | callback parameter | callback parameter | `@Override`/callback | callback parameter |
| `constructor` | Constructor/init entry | `ClassName::ClassName` | — | `__init__` | constructor declaration | — |
| `destructor` | Destructor/cleanup entry | `~ClassName` | — | `__del__` | `finalize()/close()` | `Drop::drop` |
| `API_entry` | Public API interface | non-static function | Capitalized export | non-private method/module function | public method | `pub fn` |
| `out_end` | Known external endpoint | — | — | — | — | — |
| `unknown_end` | Unknown external endpoint | — | — | — | — | — |

`API_entry` marks public functions exposed to external invokers, with `api_constraints` attribute describing input constraints (parameter types, format restrictions etc.).

`out_end`/`unknown_end` mark invocation chain endpoints (external functions with no source code in the project), auto-annotated by the endpoint classification pipeline.

**Only these seven labels are supported. New labels require explicit user permission.**

## Three-Layer Label Identification

Labels are identified in priority order:

1. **AST heuristics** (scanner auto-complete): tree-sitter automatically detects thread_processor/callback_func/constructor/destructor/API_entry during function definition extraction
2. **LLM semantic enhancement** (Claude supplement): For labels AST cannot identify (e.g. handler at callback registration, event listener callbacks), Claude reads source code to supplement
3. **User confirmation**: If LLM still cannot determine the label, must prompt the user — never guess

## Callback Flow Analysis

When a callback registration pattern is detected (e.g. `pthread_create(&tid, NULL, thread_fn, arg)`, `signal(SIGINT, handler)`, `btn.setOnClickListener(listener)`):

1. Analyze the registration point, identify all possible callback functions registered there
2. Draw a directed edge from the registration call point to the actual callback function (not just to the registration function)
3. Mark the registered callback function as `callback_func`

## Additional Labels

| Label | Meaning | Source |
|-------|---------|--------|
| `dead_code` | Function in dead preprocessor branch | `preproc_dead` |
| `hub` | High betweenness-centrality function | `betweenness` |

## Header Declaration vs API_entry Distinction

In C/C++ projects, header-only domains may contain many function declarations without corresponding definitions in that domain. If these were counted as API_entry, domain metrics would be inflated.

**Rule**: Functions that are **declared** in a header file but have **no definition** in that domain are classified as `header_declaration` in the node's metadata (not a label). They are:
- Not counted as API_entry in domain metrics
- Marked with `declaration_only: true` attribute
- Still included in the graph (they may be called from other files)
- Linked to their implementation via `import_resolution` edges

This prevents header-only domains from appearing as "shallow modules with bloated interfaces."

## Endpoint Type Mapping

While the seven labels remain fixed, endpoint classification supports sub-categories via the `endpoint_type` attribute. Endpoint sub-categories are configurable via the profile `endpoint_types` attribute. Common types include event_handler, plugin_init, callback_entry, message_callback, timer_entry — but these are profile-configured, not hardcoded.

These are not labels but metadata attributes on `out_end`/`unknown_end` nodes, configured via profile `endpoint_types`. They improve endpoint classification coverage for projects with well-known entry-point conventions.
