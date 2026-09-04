# Memory & Knowledge Reference

Code2Database separates two long-lived stores with opposite shapes:

| | Memory | Knowledge (project brief) |
|---|---|---|
| **Role** | Shared accumulating Q&A brain — many people, many question depths | Lean, fixed description of THIS project: architecture, functionality, design, usage |
| **Storage** | `memory/memory.db` (SQLite WAL + FTS5) | `knowledge/brief.json` |
| **Size** | Unbounded growth | Budget: warn >3000 chars, error >6000 (`brief-validate`) |
| **When loaded** | On demand (`search-memory`, `kb-query`) | **Every session start** (`knowledge-brief`) |
| **Update cadence** | Continuously (save/merge/split) | Small scope, only when architecture genuinely changes |

## session-init — the one-shot entry

```bash
python3 scripts/code2database_builder.py session-init --graph code2db-out/ [--top 10] [--json]
```

Loads all four layers in one prompt-ready output: the rendered brief, the memory digest (top active Q&A by weight, with category/author/reads), graph stats + brief drift warning, and known unknowns (recurring unanswered queries — save answers for them). Empty stores degrade to bootstrap hints instead of errors. This is Step 0 of every session (AI) and the fastest project briefing (humans); the web UI renders the same data interactively (Brief / Memory panels). MCP: the same context is exposed as the `code2database_session_init` tool — agents should call it first at session start, before search/describe/trace.

## Memory (memory.db)

### Layout

```
graph_dir/
├── memory/memory.db        ← categories + memories + FTS5 index
└── .scratch/               ← TTL-scoped session state (file-based)
```

Status lifecycle: `active` → `experience` (weight decayed <0.1, or node_ids invalidated), `active` → `split` (governance), `active` → `merged` (absorbed into canonical).

### Hierarchical categories

Questions are indexed by category paths (`bdev/nvme/pcie`). Missing levels are auto-created on save. Categories organize questions by topic depth so retrieval can narrow by subtree:

```bash
# Save with category + author
python3 scripts/code2database_builder.py save-memory --graph code2db-out/ \
  --question "How does nvme submit IO?" --answer "submission queue doorbell" \
  --category bdev/nvme/pcie --author alice

# Browse the category tree
python3 scripts/code2database_builder.py manage-memory --graph code2db-out/ --action categories
```

### Retrieval

`search-memory` runs FTS5 BM25 × weight with filters; results are grouped by similarity cluster so one popular Q&A can't flood the list:

```bash
python3 scripts/code2database_builder.py search-memory --graph code2db-out/ \
  --query "nvme submit" --category bdev --author alice --top 5
#   --category  prefix filter (includes ALL subcategories)
#   --tags      ALL listed tags must be present
#   --include-experience  also search archived entries
```

When FTS5 has no token overlap, a token-set similarity fallback still finds near-matches. **CJK-aware**: the unicode61 tokenizer folds each Chinese run into one token, so for queries containing CJK the similarity pass (chars + bigrams) always runs alongside FTS5 and both passes merge by best score — Chinese semantics are never silently dropped.

### Correction (correct-first save)

When a stored answer turns out to be WRONG, re-saving the corrected text would create a duplicate variant (and the merge path keeps the higher-weight — i.e. still wrong — answer on top). `--correct` finds the most similar active entry and reshapes it in place, preserving the old answer in the version history with corrector attribution:

```bash
python3 scripts/code2database_builder.py save-memory --graph code2db-out/ \
  --question "How does nvme submit IO?" --answer "corrected answer" \
  --correct --author alice
# → Corrected memory #12 (was: 'How does nvme submit IO?', similarity 0.95)
#   — no new variant created
```

Nothing similar? It degrades to a normal save (`saved as new`).

### Governance

Big shared stores accumulate over-broad and duplicate entries. `manage-memory` provides the governance operations:

```bash
# Split an over-broad entry into focused sub-entries
python3 scripts/code2database_builder.py manage-memory --graph code2db-out/ \
  --action split --id 12 --parts '[{"question": "pcie path", "answer": "..."},
                                   {"question": "tcp path", "answer": "..."}]'

# Merge duplicates into a canonical entry (variants re-point automatically)
python3 scripts/code2database_builder.py manage-memory --graph code2db-out/ \
  --action merge --ids 12,15 --canonical 12

# Recategorize
python3 scripts/code2database_builder.py manage-memory --graph code2db-out/ \
  --action move --id 12 --category bdev/nvme/rdma

# View the lineage tree (who split from whom / merged into whom / variants)
python3 scripts/code2database_builder.py manage-memory --graph code2db-out/ \
  --action lineage

# Contributor index (multi-user stores)
python3 scripts/code2database_builder.py manage-memory --graph code2db-out/ \
  --action authors
```

The same lineage graph is rendered interactively in the web UI Memory panel (Lineage button).

### Read-only sharing

The web UI serves memory read-only (`MemoryStore(read_only=True)`): viewers on a shared mount get full search/digest/lineage/author access — no directory creation, no schema writes, no access-counter bumps — while write operations (save/split/merge/...) stay with the store owner. Author-filtered views (`/api/memory/authors` + the dropdown) answer "what did alice learn about this project".


### Weight model (unchanged)

`weight = recency(e^{-λ·days}) × importance(merges + answer length) × access × (1 + boost)`, capped at 10.0. `decay` recomputes weights and archives <0.1 as experience; `promote` adds a persisted boost; builds run `consolidate` automatically.

## Knowledge (knowledge/brief.json)

### Session-start protocol

```bash
python3 scripts/code2database_builder.py knowledge-brief --graph code2db-out/
```

Render the brief into your prompt BEFORE working on the project. If it doesn't exist, bootstrap with `brief-extract`, then curate.

### Sections

| Section | Content | Example |
|---|---|---|
| `project` / `one_liner` / `description` | Name + positioning + 2-4 sentence architecture description | "SPDK — userspace storage SDK; reactor-per-core event loops" |
| `hard_rules` | Mandatory macros/branches/configs: `{rule, type, detail, evidence}` | "强制开启 SPDK_CONFIG_PCI 宏 (macro)" |
| `modes` | Usage scenario distinctions: `{name, when, differences}` | pcie vs tcp vs rdma transport |
| `key_abstractions` | `{name, role}` | bdev / io_channel / reactor |
| `conventions` / `pitfalls` / `query_paths` | Coding rules, known traps, suggested C2D query routes | |
| `must_know` | Free-form final word | |
| `graph_stats` | Auto-refreshed node/edge/domain counts | |

### Curation commands

```bash
python3 scripts/code2database_builder.py brief-extract --graph code2db-out/      # bootstrap/refresh template
python3 scripts/code2database_builder.py brief-update --graph code2db-out/ \
  --set one_liner --value "..."                                                 # scalar sections
python3 scripts/code2database_builder.py brief-update --graph code2db-out/ \
  --add hard_rules --json '{"rule": "开启 XXX 宏", "type": "macro", "evidence": "meson.build:12"}'
python3 scripts/code2database_builder.py brief-update --graph code2db-out/ \
  --remove modes --index 0                                                      # small-scope removal
python3 scripts/code2database_builder.py brief-update --graph code2db-out/ --refresh-stats
python3 scripts/code2database_builder.py brief-validate --graph code2db-out/    # schema + size budget + graph drift
```

`brief-validate` fails above 6000 rendered chars — overflow belongs in memory (`save-memory`) or the graph, not the brief.

## Unified KB index

`kb-rebuild-index` indexes **memory.db entries + brief sections** into `kb_paragraphs` (FTS5+BM25) inside `code2database.db`:

```bash
python3 scripts/code2database_builder.py kb-rebuild-index --graph code2db-out/
python3 scripts/code2database_builder.py kb-query --graph code2db-out/ --query "bdev register"
```

Kinds: `memory_qa`, `memory_experience`, `hard_rule`, `mode`, `abstraction`, `description`, `must_know`, `conventions`, `pitfalls`, `query_paths`. Run after `build`/`update` or after memory/brief edits.

## Migration note

Old `memory/*.json` (root/leaf layout) and `knowledge/*.md` stores are retired. The JSON data is NOT migrated — memory starts fresh in memory.db; recreate important Q&A via `save-memory --category ...`. Old files on disk are simply ignored.
