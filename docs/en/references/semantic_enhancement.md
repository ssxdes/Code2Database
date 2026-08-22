# Semantic Enhancement Reference

tree-sitter AST scanner cannot 100% cover all call patterns. Claude should supplement the following scenarios the scanner easily misses.

> **Why this matters for the code database**: AST extraction gives you the *skeleton* — direct calls, function definitions, struct assignments. Semantic enhancement gives you the *nervous system* — callback targets, vtable dispatch, macro expansions, cross-file resolution. Without enhancement, the graph answers "what calls what directly." With enhancement, it answers "what calls what *possibly* — under any dispatch path, macro config, or callback registration." That's the difference between a structural index and a database you can ask "how does execution actually reach here?"

## Universal Scenarios

1. **Function pointer/callback calls**: `callback(arg)` — scanner only extracts variable name, not actual function → track actual callback target, draw edge to callback function
2. **Callback registration flow**: `pthread_create(..., thread_fn, ...)` → must draw edge to thread_fn, mark thread_fn as callback_func

## C/C++ Specific

3. **Macro-expanded calls**: `CALL_FN(x)` expands to `real_fn(x)` — need to read macro definition
4. **Virtual/polymorphic calls**: `obj->method()` — need to analyze class inheritance chain
5. **Signal/event registration**: `signal(SIGINT, handler)` — handler should be marked callback_func

## Go Specific

6. **Interface method calls**: `io.Reader.Read()` — which implementation is actually called requires interface tracking
7. **Goroutine closure calls**: `go func() { fn() }()` — fn() within the closure

## Python Specific

8. **Decorator wrapping**: `@wraps` etc. decorators change the actual call target
9. **Dynamic calls**: `getattr(obj, method)()` — determined at runtime
10. **`__call__` magic method**: callable object invocations

## Java Specific

11. **Reflection calls**: `Method.invoke()` — determined at runtime
12. **Interface callbacks**: `OnClickListener.onClick()` — need to find implementing class

## Rust Specific

13. **trait method dispatch**: `dyn Trait` → need to find concrete implementation
14. **Macro-internal calls**: `println!` etc. macro expansion

## Conditional Expression Call Extraction

The scanner automatically extracts calls within conditional expressions and annotates them with `call_condition`. No manual enhancement needed.

### Supported Patterns

| Pattern | call_condition | Example |
|---------|---------------|---------|
| if-condition | `if_cond(expr)` | `if (validate() && process())` → both calls get `if_cond(validate() && process())` |
| if-consequence | `if(expr)` | body calls under true branch |
| if-alternative | `!(expr)` | else/else-if branch calls |
| switch predicate | `switch(expr)` | `switch(get_key())` → `get_key()` extracted |
| switch case body | case text | calls under `case N:` |
| while condition | `while_cond(expr)` | `while(has_next())` → `has_next()` extracted |
| do-while condition | `while_cond(expr)` | same as while |
| for condition | `for_cond(expr)` | `for(;has_next();)` → `has_next()` extracted |
| for body | `for(expr)` | body calls under the for loop |
| for init/update | inherited scope | `for(init(); ; update())` → both extracted |
| Ternary condition | `ternary_cond(expr)` | `cond() ? ... : ...` |
| Ternary true | `ternary_true(expr)` | true branch of ternary |
| Ternary false | `!ternary(expr)` | false branch of ternary |
| Compound &&/\|\| | inherited from parent | `a() && (b() \|\| c())` → all calls extracted |

All extracted edges have confidence `EXTRACTED` and source `ast` — these are real calls visible in the source.

## Cross-File Call Resolution

The builder resolves cross-file invoked names through a multi-strategy pipeline. No manual enhancement needed.

### Resolution Pipeline

| Strategy | Confidence | Description |
|----------|-----------|-------------|
| suffix_index | O(1) lookup | Matches invoked name against node IDs with dots (e.g., `bar` → `lib.bdev.bar`) |
| same_file | 0.95 | Callee defined in same source file as invoker |
| import_map | 0.85 | Callee's header file is `#include`d by invoker |
| same_domain | 0.75 | Callee in same architecture domain |
| suffix_match | 0.60 | Callee name matches node ID suffix |
| unique_name | 0.55 | Only one function with that name globally |
| fuzzy | 0.30–0.40 | Partial name match |

Post-build pass: scans header files to bridge remaining unresolved external endpoints via `#include` chains, adding `INFERRED` edges (confidence=0.75, source="import_resolution").

### Known Limitation

When two functions share the same name in different domains and neither is unique, resolution may pick the wrong target or create a placeholder node. This is an inherent limitation of static analysis without linker information. Use profile `struct_op_types` and `public_prefixes` to disambiguate.

## Macro Expansion Integration

Macro-expanded function calls may not be directly traceable. Enhancement:
- When a invoked name matches a known macro in the macro graph, look up the macro's expansion
- Extract function calls from the expanded macro body
- Add edges from the macro invocation site to the expanded functions with:
  - `confidence: INFERRED`
  - `source: macro_expansion`
  - `evidence: [{"kind": "macro_expansion", "weight": 0.8, "note": "CALL_FN expands to real_handler"}]`

Example:
```c
// Macro: #define DISPATCH_HANDLER(h, arg) ((h)->ops->handle(arg))
// Call site: DISPATCH_HANDLER(ctx, payload);
// Edge: invoker → real_handler, confidence=INFERRED, source=macro_expansion
```

## Vtable Dispatch Integration

Function pointer calls through struct operation tables can be resolved when vtable registration data is available. Enhancement:
- Build `_vtable_field_names` from both `struct_op_types` in profile AND `struct_types` from scan data
- For each fn_ptr_call with a recognizable field pattern (e.g., `ops->read`, `file->f_op->write`):
  - Look up all implementations registered in vtable data
  - Add `callback_dispatch` edges from the invocation site to each implementation
  - Confidence: `INFERRED`, source: `vtable_resolution`
  - Evidence: `{"kind": "vtable_dispatch", "weight": 0.75, "struct_type": "device_ops", "field": "read"}`
- For callback registration patterns: add edges from registration invocation sites to all registered callbacks
- For unresolved fn_ptr calls: add `callback_dispatch` edges to ALL known implementations of the struct op type (conservative over-approximation)

## Operation Mode

1. Read scanner's extraction JSON, identify orphan nodes and unresolved invoked
2. For each domain's core files, read source code to supplement missing invocation relationships and labels
3. For labels AST cannot identify, LLM judges; if still uncertain, prompt user
4. Apply macro expansion — resolve macro calls to actual function targets
5. Apply vtable dispatch — resolve fn_ptr calls to registered implementations
6. Append supplemented edges and labels to extraction JSON

**Do not overwrite existing data — only append new discoveries.**
