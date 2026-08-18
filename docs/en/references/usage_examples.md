# Usage Examples

> **Every example below is a one-call query into a persistent code database.** No grep, no glob, no Read across N files. The graph was built once; these commands query it. That's the shift from *reading* code to *querying* code.

## "What is the complete invocation chain of device_register?"

1. Search `device_register` → find node `lib_device_device_register`
2. Traverse successors (depth=3) → get all invoked and invocation conditions
3. Traverse predecessors → get all invokers
4. Read key source files to verify invocation chain
5. Annotate each edge's call_order and call_condition in the answer

## "What is the invocation chain for http.Handler in the Go project?"

1. Search `Handler` → find `pkg_server_handler_servehttp`
2. Traverse neighbors → get invocation chain
3. Annotate goroutine-labeled functions

## "What is the flow from __init__ to start in this Python class?"

1. Search `__init__` → find constructor-labeled node
2. Path find `__init__` → `start`
3. Annotate conditions and order along the path

## "How big is the impact if I modify nvme_init?"

1. Find `module_device_nvme_nvme_init`
2. Run `impact --direction reverse` → get all invokers
3. Run `impact --direction forward` → get all invoked (dependency analysis)
4. List affected domains and functions

## "What are the public APIs of this project?"

1. Search `API_entry` label → list all external interfaces
2. For each API_entry, display its api_constraints

## "device_start has a bug when mode=1, help me locate it" (Cross-Skill Collaboration)

1. resolve-chain --node root_device_start --bindings "mode=1" → get actual execution path under mode=1
2. Check concurrent windows: are there functions executing concurrently with thread_fn in concurrent_groups
3. Check param flow: how does mode flow into condition branches and invoked
4. If bug narrows to single-function internal logic → search for installed skill matching "debug/root cause", call matching skill
5. If bug involves timing/non-deterministic behavior → search for installed skill matching "diagnose/feedback loop"

## "Is the lib.device module too shallow?" (Cross-Skill Collaboration)

1. Count lib_device domain: API_entry count / total domain functions = depth ratio
2. If depth ratio > 0.5 → shallow module (interface bloat), search for installed skill matching "architecture/refactor"
3. Use deletion test: after removing lib_device, does complexity vanish or scatter?

## "I want to add a new feature to device, help me analyze impact first" (Cross-Skill Collaboration)

1. impact --node root_device_start --direction reverse → who will call the new feature
2. impact --node root_device_start --direction forward → what existing functions does the new feature depend on
3. Determine test seams: at which API_entry level to write tests
4. Search for installed skill matching "plan/implement", pass code graph impact analysis data

## "What's the difference between mode=0 and mode=1 execution paths?" (New command)

1. diff-chains --node device_start --bindings-a "mode=0" --bindings-b "mode=1"
2. Output: table showing paths only in mode=0, only in mode=1, and common paths

## "List all concurrency risk points in this project" (New command)

1. concurrency-risks --graph code2db-out/
2. Output: all spawn points, concurrent windows, sorted by risk level

## "Trace the lifecycle of 'buffer' resource" (New command)

1. data-lifecycle --graph code2db-out/ --resource "buffer"
2. Output: allocation → usage → release path with function names and conditions
