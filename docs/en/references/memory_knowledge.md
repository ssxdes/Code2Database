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
    index.json              ← metadata: id → entry pointer, tags, last_access
    entries/
      <id>.json             ← full Q&A entry
```

### Entry schema

```json
{
  "id": "mem_1709312345_abc123",
  "question": "How does the bdev layer register a new io_device?",
  "answer": "bdev_register() calls io_device_register() after ...",
  "tags": ["bdev", "io_device", "registration"],
  "created": "2026-07-15T10:23:45Z",
  "last_accessed": "2026-07-29T14:00:00Z",
  "access_count": 3,
  "source": "conversation",
  "domain": "bdev"
}
```

### CLI commands

| Command | Action |
|---------|--------|
| `save-memory --question Q --answer A --tags t1,t2` | Save a new entry |
| `search-memory --query "bdev register"` | Full-text search across Q+A+tags |
| `manage-memory --action list` | List all entries with metadata |
| `manage-memory --action delete --id <id>` | Delete an entry |
| `manage-memory --action decay --threshold-days 30` | Remove entries not accessed in 30 days |
| `memory-health` | Report entry count, oldest entry, decay candidates |

### Decay logic

An entry is a decay candidate when `now - last_accessed > threshold_days`.
The decay command deletes decay candidates but never touches entries
with `access_count >= 5` (frequently-revisited entries are preserved
regardless of age). Default threshold is 30 days; override with
`--threshold-days`.

### Concurrency

All writes go through `_atomic_write_locked()`:
1. Write to `entries/<id>.json.tmp.<pid>`
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
    index.json              ← topic → knowledge file pointer
    topics/
      <topic>.json          ← list of facts for one topic
```

### Fact schema

```json
{
  "topic": "error_handling",
  "fact": "All bdev functions use SPDK_ERRLOG before returning negative errno",
  "source": "docs/bdev.md:42",
  "confidence": 0.9,
  "related_functions": ["bdev_register", "bdev_open"],
  "extracted_at": "2026-07-15T10:00:00Z",
  "graph_version": "v1.2.0"
}
```

### CLI commands

| Command | Action |
|---------|--------|
| `extract-knowledge --source docs/ --graph <outdir>` | Extract facts from docs + graph |
| `apply-knowledge --topic error_handling` | Apply a topic's facts to enrich the graph |
| `knowledge-query --topic error_handling` | List all facts for a topic |
| `knowledge-validate` | Check facts for stale references, low confidence, missing fields |

### Validation checks

`knowledge-validate` reports:
- Facts with `confidence < 0.5` (worth re-extracting)
- Facts whose `related_functions` no longer exist in the current graph
- Facts missing required fields (topic, fact, source)
- Topics with only one fact (thin coverage)

## Integration with queries

The query priority chain is:

```
context_pack_micro → context_pack_lite → explore-flow
  → knowledge_pack_lite → memory_pack_lite → describe-node
```

`knowledge_pack_lite` and `memory_pack_lite` are consulted when the
graph-only packs don't answer the question. The packs are assembled by
querying the memory/knowledge indexes for entries whose tags or topics
overlap the query terms.

## Programmatic access

```python
from _builder.memory_manager import MemoryManager
from _builder.knowledge_manager import KnowledgeManager

mem = MemoryManager(outdir="/path/to/code2db-out")
mem.save("question", "answer", tags=["bdev"])
results = mem.search("bdev register")

know = KnowledgeManager(outdir="/path/to/code2db-out")
know.query_topic("error_handling")
```

Both managers auto-create their directory structure on first use.
