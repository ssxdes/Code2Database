# Memory & Knowledge System

This reference documents the persistent memory and knowledge extraction
systems in Code2Database. Both systems persist data as JSON files
in the graph output directory and use `fcntl` file locking for
concurrent-access safety.

> **The code-database compounding effect**: the graph is the structural
> truth (what the code says today). Memory is the conversational truth
> (what we've discussed about the code). Knowledge is the durable truth
> (what we've extracted from docs + graph analysis). Together they form
> a three-layer database that compounds in value: every query that
> gets answered and saved is one less grep the next session has to do.
> An LLM agent that consults all three isn't re-reading your codebase —
> it's querying a curated, audited, version-pinned knowledge base.

## When to use which

| System | Purpose | What it stores | Decay? |
|--------|---------|----------------|--------|
| Memory | Q&A pairs from conversations | question, answer, tags, timestamp, access_count | Yes — entries decay after 30 days unused |
| Knowledge | Extracted facts about the codebase | topic, fact, source, confidence, related functions | No — facts are version-pinned to the graph |

Use **memory** for "Claude, remember what we discussed yesterday" —
session-spanning Q&A recall. Use **knowledge** for "what do we know
about the error-handling pattern in this module" — durable facts
extracted from docs + graph analysis.

## Memory system

### File layout

```
<graph_outdir>/
  memory/
    index.json              ← metadata: id → entry pointer, tags, status, root_id
    L0_index.json           ← hot memory (weight > 0.7)
    L1_index.json           ← warm memory (0.3 < weight <= 0.7)
    L2_index.json           ← cold memory (weight <= 0.3)
    root/
      root_<id>.json        ← canonical merged entry (one per Q&A cluster)
    leaf/
      mem_<id>.json          ← individual Q&A entry (may be merged into a root)
    experience/
      experience_<id>.json   ← archived entries (weight decayed or graph changed)
      index.json            ← experience index
  .scratch/                 ← session-scoped temporary memory (TTL'd)
```

### Entry schema

```json
{
  "id": 5,
  "question": "How does the bdev layer register a new io_device?",
  "answer": "bdev_register() calls io_device_register() after ...",
  "tags": ["bdev", "io_device", "registration"],
  "chains": [],
  "node_ids": ["bdev_register", "io_device_register"],
  "status": "trusted",
  "weight": 1.23,
  "root_id": 5,
  "merged_count": 2,
  "access_count": 3,
  "knowledge_refs": ["principles.md"],
  "versions": [{"answer": "...", "version": 1, "merged_from": 7}],
  "created": "2026-07-15T10:23:45",
  "last_accessed": "2026-07-29T14:00:00",
  "validated_at": "2026-07-29T14:00:00"
}
```

### CLI commands

| Command | Action |
|---------|--------|
| `save-memory --question Q --answer A --tags t1,t2` | Save a new entry (auto-merges to root if similar) |
| `search-memory --query "bdev register"` | Search memory by Jaccard token similarity |
| `manage-memory --action add/correct/reshape/promote/refine` | Write persistent memory (script-based ops) |
| `manage-memory --action decay` | Run weight decay + archive low-weight entries to experience |
| `manage-memory --action consolidate` | One-pass: decay + rebuild indexes + archive |
| `validate-memory` | Check entries against current graph; mark stale → experience |
| `memory-health` | Report entry count, layer counts, oldest entry, scratch sessions |

### Decay logic

Entry weight = `recency × importance × access`, capped at 10.0:
- `recency = exp(-0.05 × days)` — ~14 days to 50% weight
- `importance = 1 + 0.10 × merged_count + 0.05 × (answer_length / 1000)`
- `access = 1 + 0.10 × access_count`

When `weight < 0.1`, the entry is archived to `experience/` with
`status="experience"`. The `decay` action recomputes weights and
archives; the `consolidate` action runs decay + rebuilds all indexes
in one pass. There is no `--threshold-days` parameter — decay is
continuous via the `DECAY_LAMBDA` constant in `memory_manager.py`.

### Concurrency

All writes go through `_atomic_write_locked()`:
1. Write to `<file>.tmp.<pid>`
2. `os.replace()` to the final path (atomic on POSIX)
3. Exclusive `fcntl.flock(LOCK_EX)` during the rename

Reads use shared `fcntl.flock(LOCK_SH)` so multiple readers don't block
each other. On systems without fcntl (Windows), the locks are skipped
gracefully — the system still works, just without concurrent-write
protection.

## Knowledge system

### File layout

```
<graph_outdir>/
  knowledge/
    index.json              ← files + topics index (rebuilt on extract-knowledge)
    _meta.json              ← per-file source provenance (auto/manual/llm_generated)
    _memory_links.json      ← bidirectional links to memory entries
    architecture.md         ← graph-inferred architecture overview
    principles.md           ← LLM-curated protocol/contract principles
    glossary.md             ← terminology (auto-extracted from labels)
    constraints.md          ← API constraints, configuration rules
    patterns.md             ← Code patterns
    build_rules.md          ← Build-system rules
    detail_*.md             ← Auto-extracted source patterns (macros, structs, etc.)
    custom_*.md             ← User/LLM-defined topics
```

### Knowledge file schema

Knowledge is stored as **Markdown files** (not structured JSON). Each
file is a topic; each `##` heading in a file is a sub-topic. The
`index.json` lists files with their headings:

```json
{
  "files": [
    {"name": "principles.md", "size": 4523, "headings": ["Protocol Standards", "API Contracts"]}
  ],
  "topics": ["Protocol Standards", "API Contracts"]
}
```

### CLI commands

| Command | Action |
|---------|--------|
| `extract-knowledge --source docs/ --graph <outdir>` | Extract knowledge template (graph-inferred + doc headings) |
| `apply-knowledge --graph <outdir>` | Apply LLM-filled `.code2database_knowledge_input.json` back to knowledge/ |
| `knowledge-query --topic "error_handling"` | Search knowledge by topic (substring match across .md files) |
| `knowledge-validate` | Check for stale domains, signatures-only files, template-only files |

### Validation checks

`knowledge-validate` reports:
- Knowledge files referencing domains that no longer exist in the graph (stale)
- Knowledge files containing only function signatures (no actual knowledge)
- Knowledge files containing only template comments (`FILL IN`, `LLM_FILL`)
- Doc-code alignment mismatches (semantic_desc vs body_text/signature)

## Unified KB Query (Phase 1-3 Upgrade)

The memory and knowledge stores were originally searched independently
(Jaccard / substring match). Phase 1-3 introduces a unified FTS5+BM25
query surface, `kb-query`:

- `kb-rebuild-index` indexes `memory/*.json` + `knowledge/*.md` into
  the `kb_paragraphs` table + FTS5 virtual table in `code2database.db`
- `kb-query --query "..."` runs a unified query across both stores,
  returning ranked results with a BM25 score and a `source_kind`
  (memory / knowledge)
- The `query` (Cypher) command auto-injects the top 3 kb hits into
  the result's `_hints` field
- `describe-node` returns the node's `memory_refs` and `knowledge_refs`
- The MCP tool `code2database_kb_query` is exposed to LLM agents

## Integration with queries

The query priority chain is **aspirational** — `cmd_query` currently
queries only the graph + cgdb tables. To consult memory/knowledge, the
agent must explicitly call `search-memory` / `knowledge-query` (or
their MCP equivalents `code2database_memory_search` /
`code2database_knowledge_query`), or use the new unified `kb-query`
command.

```
context_pack_micro → context_pack_lite → explore-flow
  → (manual) knowledge-query / kb-query → (manual) search-memory / kb-query → describe-node
```

`memory_pack_lite.json` and `knowledge_pack_lite.json` are generated
by `manage-memory --action pack` and `extract-knowledge` respectively;
they sit alongside `context_pack_lite.json` for the agent to consult.
Since Phase 3, `_build_context_pack` merges these two packs into the
`memory_summary` + `knowledge_summary` fields of the context pack.

## Programmatic access

```python
from _builder.memory_manager import MemoryManager
from _builder.knowledge_manager import KnowledgeManager
from _builder.kb_index import query_kb, rebuild_kb_index

# Rebuild the unified FTS5 index (run after build/update)
rebuild_kb_index("/path/to/code2db-out")

# Unified query (across memory + knowledge)
results = query_kb("/path/to/code2db-out", "bdev register", top_n=10)

# Single-store memory
mem = MemoryManager(graph_dir="/path/to/code2db-out")
mem.add(question="...", answer="...", tags=["bdev"], node_ids=["bdev_register"])
results = mem.query("bdev register")

# Single-store knowledge
know = KnowledgeManager(graph_dir="/path/to/code2db-out")
know.query_knowledge("error_handling", max_tokens=500)
```

Both managers auto-create their directory structure on first use.
