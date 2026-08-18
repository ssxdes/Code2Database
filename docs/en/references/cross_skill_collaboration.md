# Cross-Skill Collaboration Reference

Code2Database provides invocation chain analysis, but scenarios like bug location, new feature development etc. require coordination with other skills.

> **The code-database advantage in collaboration**: Code2Database's graph isn't just nodes and edges — it carries the conditions, concurrency context, field access, and evidence traces that other skills need as *input*. When a debugging skill asks "what could cause this crash?", Code2Database hands it `reverse-trace` output (all paths to the crash) + `detect-races` output (concurrent access) + `field-access` output (shared state) — a complete picture in one pass, not a file-by-file re-discovery. Other skills query the database; they don't re-read the code.

## Skill Discovery Mechanism

**Core principle**: Don't hardcode skill names. Define **capability keywords** required by each scenario, and match against installed skills' descriptions at runtime.

**Discover installed skills**:
```bash
ls ~/.claude/skills/*/SKILL.md 2>/dev/null | while read f; do
  name=$(basename $(dirname "$f"))
  desc=$(head -5 "$f" | grep "description:" | sed 's/.*description: *//')
  echo "$name|$desc"
done
```

**Matching rule**: Each scenario defines capability keywords. Search for these keywords in installed skill descriptions. Matching is fuzzy (substring match suffices).

## Capability Requirements Map

| Scenario | Required capability keywords | Data Code2Database provides |
|----------|---------------------------|----------------------------------|
| Bug location | debug, diagnose, bug, root cause | resolve-chain execution path + concurrent windows + param flow |
| Architecture review | architecture, refactor, deep module, seam | Domain dependencies + module depth ratio + API interface data |
| New feature development | implement, plan, TDD, test-driven | Impact analysis + dependency chains + test seam location |
| Domain terminology alignment | domain, glossary, ubiquitous language | Auto-detected architecture domains + global constants/enums |
| Change verification | verify, verification, complete | Graph data consistency check after sync/update |
| Merge conflict | merge, conflict, resolve | code2db-out JSON conflict → sync command |
| Code review | review, code review | Change-affected domains + affected function list |
| Visual export | diagram, graph, visualize, export | Code graph data → HTML/SVG/PNG |
| **Security/vulnerability analysis** | **security, vulnerability, exploit, crash** | **reverse-trace crash paths + detect-races + concurrency-analyze** |
| **Race condition analysis** | **race, concurrency, data race** | **detect-races + field-access + concurrency-risks** |
| **Indirect call tracing** | **function pointer, dispatch, indirect call** | **explore-flow + describe-node --context for dispatch targets + callback_dispatch edges** |
| **I/O path analysis** | **io, input, output, data flow** | **io-path for I/O path tracing** |

**When multiple skills match**: Prefer the skill with denser keyword occurrence in its description.

**When no skill matches**, prompt user:
```
Current scenario requires <capability description>, but no relevant skill is installed.
Search and install? (y/n)
Search command: npx skills@latest add <skill-name>
Browse: https://github.com/topics/claude-skill
```

## 10a - Bug Location Collaboration

**Trigger**: User says "locate bug"/"debug"/"why does this function give wrong result"/"diagnose" etc.

**Workflow**:

1. Code2Database: Use resolve-chain + bindings to trace problem function's execution path
   → Get: which branches are taken, which functions participate, concurrent windows, param flow

2. Determine if external skill is needed (capability keywords: debug, diagnose, root cause):
   - Bug involves multi-function interaction (conditions+invocation chain+concurrency) → code graph data sufficient, analyze directly
   - Bug is single-function internal logic error → search for installed skill matching "debug/root cause"
   - Bug is non-deterministic/timing-related → search for installed skill matching "diagnose/feedback loop"

3. Context to pass to matched skill:
   - Problem function's describe-node output (body_text + signature + params + condition_vars)
   - resolve-chain result (execution path + which branches alive/pruned)
   - Concurrent windows (concurrent_groups) — if bug relates to thread races
   - Param flow (param_flow) — if bug relates to parameter value passing

**Key principle**: **Don't propose fixes before finding the root cause.** code graph's chain data helps narrow the investigation scope, but is not the fix itself.

**Key principle**: **Build a feedback loop.** code graph's resolve-chain is itself a feedback loop — under given conditions, which code path will execute. If resolve-chain results don't match actual behavior, the code graph data has gaps (needs semantic enhancement).

## 10b - Architecture Review Collaboration

**Trigger**: User says "architecture review"/"module too deep/shallow"/"refactor"/"architecture problem" etc.

**Workflow**:

1. Code2Database: Run describe-node on each API_entry function to get:
   - Each domain's API_entry count (= interface size)
   - Each domain's internal function count (= implementation depth)
   - Cross-domain invocation relationships (= module dependencies)

2. Describe using deep module vocabulary (applicable to any architecture analysis skill):
   - Architecture domain = Seam (where interface lives)
   - API_entry functions = Interface (exposed interface surface)
   - Non-API functions within domain = Implementation (hidden implementation)
   - API_entry count / total domain functions = Depth ratio (smaller = deeper = better)

3. Search for installed skill matching "architecture/refactor/deep module":
   - Match found → call, passing code graph's domain dependency + depth ratio data
   - No match → prompt user to install

4. Search for installed skill matching "domain/glossary":
   - Match found → use deletion test to evaluate domain depth
   - No match → use built-in depth assessment method

**Built-in depth assessment method**:
- **Depth ratio** = `API_entry count / total domain functions`. Smaller = deeper
- **Deletion test**: Imagine removing a domain — does complexity vanish (shallow = just passing through) or scatter to N invokers (deep = earning it back)?
- A domain with API_entry > 10 and internal functions < 15 is likely shallow (interface bloat)

## 10c - New Feature Development Collaboration

**Trigger**: User says "develop new feature"/"implement"/"need to add X" etc.

**Workflow**:

1. Code2Database: Run impact analysis on change target
   → impact --direction reverse: which functions will call the new feature
   → impact --direction forward: what existing functions does the new feature depend on
   → Identify affected domains and API_entries

2. Determine test seams (universal TDD principle):
   - At which API_entry level to write integration tests
   - Where the new feature joins existing invocation chains

3. Search for installed skill matching "plan/implement":
   - Match found → pass code graph impact analysis results as input
   - No match → formulate plan directly in code graph context

4. Search for installed skill matching "TDD/test-driven":
   - Match found → write tests at API_entry seams (code graph data shows which entries to test)
   - No match → remind user to manually write tests at API_entry level

## 10d - Domain Terminology Alignment Collaboration

**Trigger**: User mentions architecture domain names inconsistent with project docs, or code-graph-detected domains conflict with CONTEXT.md terminology

**Workflow**:

1. Code2Database: List all detected domains and global constants

2. Check if project root has CONTEXT.md:
   - Yes: compare domain names with CONTEXT.md terminology
   - Terminology conflict → search for installed skill matching "domain/glossary/ubiquitous language"
   - Domain names absent from CONTEXT.md → suggest supplementing

3. Enum/const names in globals files → suggest adding to glossary

## 10e - Change Verification Collaboration

**Trigger**: After sync or update completes

**Universal principle**: **Must have verification evidence before claiming completion.** Must re-verify graph data after sync/update.

1. After sync/update, immediately run verification:
   ```bash
   python3 code2database_builder.py load --graph code2db-out/ --summary
   ```

2. Verify data:
   - Node count reasonable (should not drop significantly unless files were deleted)
   - Domain list complete
   - API_entry label count correct

3. Run describe-node on key functions to verify data completeness:
   - body_text not empty
   - signature correct
   - params/local_vars present

4. Run consistency validation (built into build step):
   - Re-run `build` if inconsistencies are detected
   - Check for unaccounted nodes/edges
   - Verify endpoint consistency across files
   - Flag domain coverage gaps

5. Search for installed skill matching "verify/verification/complete":
   - Match found → follow that skill's verification process
   - No match → use above built-in verification steps

6. Before completing development branch: if scanned source code was modified, run update to refresh graph, then sync to merge with remote

## 10f - Merge Conflict Handling

**Trigger**: git merge produces JSON conflicts in code2db-out/ directory

**Handling** (built-in, no external skill needed):

Don't manually resolve code graph JSON conflicts! Use sync command instead:
1. Keep local version: `git checkout --ours code2db-out/`
2. Copy remote version to temporary location
3. Run sync to merge:
   ```bash
   python3 code2database_builder.py sync --graph code2db-out/ --git-path <remote-code2db-out-path>
   ```
4. sync auto-merges both datasets (local priority), regenerates all domain files and indexes

For more general merge conflict handling, search for installed skill matching "merge/conflict/resolve".

## 10g - Visualization Collaboration

**Trigger**: User wants to export code graph to other formats (non-HTML)

1. Code2Database: First generate base visualization with export-html
2. Search for installed skill matching "diagram/graph/visualize/export":
   - Match found → call skill, passing code graph data
   - No match → prompt user to install relevant visualization skill

## 10h - Security/Vulnerability Analysis Collaboration

**Trigger**: User mentions "crash report"/"null pointer dereference"/"security vulnerability"/"exploit analysis" etc.

**Workflow**:

1. Code2Database: Reverse-trace from crash point
   ```bash
   python3 code2database_builder.py reverse-trace --graph code2db-out/ --crash-point CRASH_FUNC --max-depth 15 --max-paths 30
   ```
   → Get all paths from entry points to crash point

2. Analyze race conditions (if bug involves concurrency):
   ```bash
   python3 code2database_builder.py detect-races --graph code2db-out/ --func CRASH_FUNC
   ```
   → Get cross-thread data race analysis, shared resource access, lock scopes

3. Check indirect dispatch targets (if crash involves function pointer call):
   ```bash
   python3 code2database_builder.py describe-node --graph code2db-out/ --node CRASH_FUNC --detail full --context
   ```
   → Get all possible targets of indirect calls on the crash path via callback_dispatch edges

4. Determine if external skill is needed (capability keywords: security, vulnerability, exploit):
   - Bug is a clear code path issue → code graph trace sufficient, analyze directly
   - Bug involves complex memory safety → search for installed skill matching "security/vulnerability"
   - Bug involves specific exploit technique → search for installed skill matching "exploit/pwn"

5. Context to pass to matched skill:
   - Full reverse-trace output (all paths from entry to crash)
   - detect-races output (concurrent access analysis)
   - Indirect dispatch targets on the crash path (from describe-node --context)
   - Field-access output for shared data structures

**Key principle**: **For security analysis, fn_ptr_call and callback_dispatch edges are critical.** If these are missing, re-run `build` to trigger fn-ptr resolution.

**Key principle**: **Conditional branch calls are auto-extracted.** The scanner automatically extracts all calls within if/while/for/switch/ternary conditions and annotates them with `call_condition` — no manual enhancement needed.

## 10i - Race Condition Analysis Collaboration

**Trigger**: User mentions "data race"/"race condition"/"concurrent bug"/"TOCTOU" etc.

**Workflow**:

1. Code2Database: Run full race analysis pipeline:
   ```bash
   # Detect data races
   python3 code2database_builder.py detect-races --graph code2db-out/

   # Concurrency safety analysis for specific pair
   python3 code2database_builder.py concurrency-analyze --graph code2db-out/ --func SUSPECT_FUNC

   # Field-level access tracking
   python3 code2database_builder.py field-access --graph code2db-out/ --field SHARED_VAR
   ```

2. Output includes:
   - Shared data structures accessed by multiple threads
   - Lock protection status for each access
   - Race windows (unprotected concurrent access)
   - Concurrent execution path pairs

3. Determine if external skill is needed:
   - Race analysis is comprehensive → code graph data sufficient
   - Need formal verification → search for installed skill matching "formal verification/model checking"
   - Need runtime validation → search for installed skill matching "thread sanitizer/concurrency sanitizer"

4. Context to pass:
   - detect-races output (cross-thread data race analysis)
   - Field-access report for contested fields
   - concurrency-risks output for spawn points

## 10j - Indirect Call Tracing Collaboration

**Trigger**: User asks "which functions implement this callback?"/"what does this function pointer call?"/"vtable dispatch" etc.

**Workflow**:

1. Code2Database: Resolve indirect calls (fn-ptr resolution runs during build, but can be explored):
   ```bash
   # View dispatch targets for a specific function
   python3 code2database_builder.py describe-node --graph code2db-out/ --node SUSPECT_FUNC --detail full --context
   ```

2. Output:
   - Each fn_ptr_call → list of possible target functions (callback_dispatch edges)
   - Conditional metadata on dispatch registrations

3. Determine if external skill is needed:
   - Need more precise resolution → search for installed skill matching "LSP/language server/type inference"
   - Need runtime validation → search for installed skill matching "dynamic analysis/profiling"

4. **Key principle**: `callback_dispatch` edges represent conservative over-approximation (all possible targets). If precision is needed, use `--verbose` to see resolution reasoning, and validate with runtime data.

## Known Skill Match Reference

Current installed skills and scenario correspondence, **for reference only** — new skills auto-enter discovery mechanism:

| Scenario | Known matching skill (if installed) | Install source |
|----------|-------------------------------------|---------------|
| Bug location | systematic-debugging, diagnosing-bugs | superpowers, skills |
| Architecture review | improve-codebase-architecture, codebase-design | skills |
| New feature development | writing-plans, implement, tdd | superpowers, skills |
| Domain terminology alignment | domain-modeling | skills |
| Change verification | verification-before-completion, finishing-a-development-branch | superpowers |
| Merge conflict | resolving-merge-conflicts | skills |
| Visual export | fireworks-tech-graph, architecture-diagram | skills |
| Code review | requesting-code-review, review | superpowers, skills |
| Security/vulnerability analysis | concurrency-analysis | skills |
| Race condition analysis | concurrency-analysis | skills |
| Indirect call tracing | lsp-analysis, type-inference | skills |
