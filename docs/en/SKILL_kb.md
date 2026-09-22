---
name: Code2Database-kb
description: "Standalone project knowledge base: curated knowledge + accumulated veteran experience on dedicated SQLite stores with FTS5 retrieval, version-scoped recall, and cross-domain queries. Runs with or without the code graph. Use /Code2Database-kb when capturing, refining, searching, or graduating project knowledge and experience."
trigger: /Code2Database-kb
---

# /Code2Database-kb

**A knowledge base for code projects that an AI loads before working — and grows while working.** Two stores, one index, no graph required.

## Deployment

The kb sub-skill runs standalone or alongside the code graph — they share the CLI but no data files:

| Store | File | Belongs to |
|---|---|---|
| knowledge + memory | `memory/memory.db`, `knowledge/knowledge.db`, `kb_index.db` | this sub-skill |
| code graph | `code2database.db`, `code2database_master.json` | the graph skill |

`kb init` provisions a store with zero graph artifacts. Every kb command works on a directory that has never been scanned or built.

## The two stores — definition, logic, physical location

| Dimension | knowledge | memory |
|---|---|---|
| Definition | curated, stable facts about THIS project: rules, modes, abstractions, conventions, pitfalls, query routes | episodic veteran experience: question → answer pairs learned while working |
| Logical model | typed rows (`hard_rule` / `mode` / `abstraction` / `convention` / `pitfall` / `query_path` / `description` / `must_know`), no decay | clustered Q&A entries with weight, access counters, merge lineage, decay to `experience` |
| Physical file | `knowledge/knowledge.db` | `memory/memory.db` |
| Prompt view | `knowledge/brief.json` (derived, size-budgeted) | memory digest in `session-init` |

`kb_index.db` is the derived FTS5/BM25 index over BOTH stores — one query ranks them together (`source_kind` tells them apart). `brief.json` is regenerated from the knowledge rows after every write; the knowledge database is the source of truth.

## Session start

```bash
python3 scripts/code2database_builder.py kb-init --name my-project   # once
python3 scripts/code2database_builder.py session-init               # every session
```

`session-init` never requires a graph: it renders the brief, the memory digest, and the known unknowns (recurring queries with no answer — capture prompts).

## Capture rules (what enters memory)

Save a memory ONLY when it would help analyze a similar problem **from a blank context** — assume the reader has this kb and nothing else.

**Filter out** (never store):
- current-conversation plans, scratch state, in-flight decisions — they are not part of the code project; use the TTL scratch store instead
- content the project itself already contains and a query/graph lookup answers in one call
- session-specific tooling notes, prompt drafts, todo lists

**Capture when**:
- a non-trivial question was solved and the investigation path itself is the answer
- a trap cost real debugging time
- a mandatory rule or constraint was discovered that the brief does not cover
- a previously stored answer turned out wrong (`--correct`)
- a known-unknown from `session-init` got answered

Anchor to code: pass `--symbol fn_name` when the memory is about a concrete function/type. Tag the version it was learned on: `--version-scope <branch-or-tag>` (default `default`).

```bash
python3 scripts/code2database_builder.py save-memory \
  --question "is bdev_start thread safe?" --answer "no — poller-owned, ..." \
  --category bdev/nvme --author you --symbol bdev_start --version-scope main
```

## Refinement rules (memory stays sharp)

- **wrong answer** → `save-memory --correct` reshapes the most similar entry in place (version history kept, no duplicate variants)
- **similar memories** → they merge on save (threshold 0.7); `manage-memory --action merge/split/move` reorganizes; `compact` runs after every build
- **better wording** → `--correct` with the same question and an improved answer
- **stale against code** → `memory validate` demotes entries whose `node_ids` left the graph (skipped gracefully when no graph exists)

## Graduation rules (memory → knowledge)

When a memory keeps proving useful, it becomes a fact: `brief suggest` mines graduation candidates (strong weight or merge count) and emits ready-to-run `brief update` commands — graduation is always a reviewed step, never automatic. Knowledge stays lean: the brief warns above 3000 chars, errors above 6000.

## Expansion rules (how the boundary grows)

- **adjacent domains**: when a new subsystem/language keeps showing up in questions but no memory covers it, add a memory first (cheap to be wrong); create a new `--category` level only after repeated hits — hierarchy follows memories, never the reverse
- **cross-domain reuse**: an explanation already validated in another domain gets attached via `kb-domain add` (`kb-query --cross` labels the source domain) instead of copied — copies drift
- **knowledge deepens, never widens**: expanding knowledge means `revise`-ing an existing hard_rule / abstraction with a sharper wording, not appending new items; horizontal growth belongs to memory
- **shrinking is expanding too**: two hard_rules saying the same thing merge into one (`brief update` rewrite); knowledge is valued by density, not entry count

## Domain partitioning

- **within a store**: `--category path/to/topic` builds a hierarchy (`bdev/nvme/pcie`), auto-created; choose the path by the symbols/subsystem the memory is about
- **across stores**: every kb `.db` is one domain. `kb-domain-add <store-dir>` registers another knowledge base; `kb-query --cross` searches all watched domains and labels each hit with `source_domain`

## Version-scoped recall

Every memory and knowledge item carries the code version it was learned on. Queries state the version being worked on:

```bash
python3 scripts/code2database_builder.py kb-query \
  --query "queue doorbell" --version-scope release/2.0 --cross
```

Entries learned on `release/2.0` rank first; entries from other versions follow, labeled `is_current_scope: false` — never filtered out, always marked.

## Query priority chain

```
1. memory (search-memory / kb-query)      — was this asked before?
2. knowledge (kb-query --kinds / brief)   — is there a curated rule?
3. graph (only if the graph skill is deployed)
4. source (last resort)
```

`kb query` is the one-call surface across both stores; `session-init` is the one-call load of everything.

## Command surface (Tier-1)

`kb init`, `session-init`, `save`, `recall`, `kb query`, `knowledge-brief`, `kb rebuild-index`, `kb known-unknowns` — plus `brief-*` curation, `memory manage` governance, `kb cluster`, `kb-domain-*` registry, `search semantic`. The full CLI remains accessible.
