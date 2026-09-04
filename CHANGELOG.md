# Changelog

All notable changes to Code2Database will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] - 2026-09-04

### Round 23 — ROADMAP follow-ups: architecture UI, correct-first save, memory lineage, multi-user views

All four remaining ROADMAP items, closing the gap analysis from Round 21.

- **Web UI: Architecture panel** — the build already wrote ARCHITECTURE_FLOWS.md (core execution-flow narrative: API entry → endpoint chains with conditions/concurrency/domain crossings) but it was file-only. `GET /api/architecture` + topbar Arch button render it in the UI; a missing file degrades to a hint.
- **`save-memory --correct`** — correction as a first-class save: finds the most similar active entry (same threshold as merge-on-add, targeting the cluster root) and reshapes it in place — old answer preserved in version history with corrector attribution — instead of creating a duplicate variant of the wrong answer. Falls back to a normal save when nothing is similar. `MemoryStore.correct_similar()`; `reshape(corrected_by=...)`.
- **Memory lineage graph** — `MemoryStore.lineage()` exposes the split/merged_into/variant relations (pure read); CLI `manage-memory --action lineage` renders the tree; `GET /api/memory/lineage` + the Memory-panel Lineage button render it interactively (tombstones visible, cycle-guarded).
- **Multi-user read-only sharing + author views** — `MemoryStore(read_only=True)`: mode=ro connections (WAL-safe fallback), no dir/schema creation, no access-counter bumps, loud failure on writes; the web UI memory endpoints all serve read-only now (GETs no longer create `memory/` dirs) and degrade to empty when no store exists. `manage-memory --action authors` + `GET /api/memory/authors` (contributor index) and an author dropdown filtering search/digest (`digest(author=)` filter added).

Tests: +29 (memory_store: TestCorrectSimilar 4, TestLineage 5, TestReadOnlyAndAuthors 7; memory_cmd: --correct 2 + lineage/authors actions 3; web UI: architecture 3, lineage 2, authors/filter 3). Suite: 2114 tests / 111 files. Docs: SKILL.md correction protocol (en/zh), memory_knowledge.md (en/zh) correction/lineage/authors/read-only sections, AGENTS.md, CHANGELOG.

### Round 22 — MCP `session_init` tool

ROADMAP follow-up (lowest-hanging): the one-shot session context from Round 21 is now available where agents actually work — the MCP tool surface.

- **`code2database_session_init`** — one call returns the project brief (mandatory rules/modes/pitfalls), memory digest (veteran Q&A ranked by weight), graph state with brief-drift warning, known-unknowns (repeatedly unanswered queries worth capturing into memory), plus a prompt-ready `rendered` form identical to the `session-init` CLI output. Structured fields alongside let agents follow up programmatically. Every layer degrades to a hint when absent — never raises. No required parameters (`top` optional, default 10).
- MCP tool count: 81 → 82 (35 `code2database_*` + 19 `cgdb_*` + 28 design-report).

Tests: +5 (test_mcp_server: registry presence, empty-dir degradation, full-context rendering, top clamping). Suite: 2085 tests / 111 files. Docs: SKILL.md / OVERVIEW.md / README / SKILL_ops / ops_commands / usage_reference / memory_knowledge (en+zh), AGENTS.md.

### Round 21 — C2D as the project's AI knowledge system (session-init + human UI context)

Vision-driven round (see ROADMAP.md for the full gap analysis): close the distance between C2D and "the AI knowledge system for a code project".

- **`session-init`** — the one-shot session entry for AI agents AND humans: rendered project brief, memory digest (top active Q&A by weight with category/author/reads — the veteran-experience view for newcomers), graph stats + brief drift warning, and known unknowns (recurring unanswered queries surfaced as save-memory suggestions, closing a feedback loop that was hidden behind kb-known-unknowns). Empty stores degrade to bootstrap hints. CLI: `session-init [--top] [--json]`, alias `init`.
- **context_pack regression fixed** — `knowledge_summary` had been reading the removed `.knowledge_pack_lite.json` (silently absent from every build since Round 20); now sourced from `knowledge/brief.json`. `memory_summary` read a stale pack file only written by explicit actions; now generated fresh from memory.db via the new public `MemoryStore.digest()`.
- **Web UI: Brief + Memory panels** — `/api/brief` + `/api/memory/search` (empty query = weight-ranked digest); topbar buttons open the rendered brief (one-glance project context) and a searchable veteran Q&A panel (category/author/weight/reads, textContent-only rendering).
- **Correction protocol documented** (SKILL.md en/zh): search-memory before answering; correct/reshape wrong answers instead of saving duplicates; save missing answers with category+author; known-unknowns feed back into memory.
- Module redundancy assessment recorded in ROADMAP.md: no must-delete overlap; the gap was entry-point integration, not command count.

Tests: +15 (test_session_init 9 incl. context_pack embedding; web UI project-context endpoints 6). Docs: SKILL.md (en/zh) Step 0 + protocol, memory_knowledge.md (en/zh), usage_reference (en/zh), AGENTS.md.

### Memory → SQLite, Knowledge → project brief (Round 20)

Redesigned the two long-lived stores around their real shapes: memory is BIG and messy (many people, many question depths, shared accumulation) while knowledge is LEAN and mandatory (fixed per-project architecture/function/design/usage, loaded into the prompt at every session start).

**Memory (memory/memory.db — SQLite WAL + FTS5):**
- `categories` table with materialized paths (`bdev/nvme/pcie`) — missing levels auto-created on save; `manage-memory --action categories` lists the tree with direct/subtree counts
- `memories` + `memories_fts`: BM25 retrieval × weight with category-prefix (subtree), tag (ALL), author, and status filters; token-similarity fallback; results grouped by similarity cluster (variant_count) so one hot Q&A can't flood the list
- Governance for the shared store: `split` (over-broad entry → focused children with lineage), `merge` (duplicates → canonical, variants re-pointed), `move` (recategorize)
- `author` attribution for multi-user stores
- Weight/decay formula, merge threshold 0.7, weak-merge protection, persisted boost, L0/L1/L2 packs — all unchanged from the JSON store
- MemoryManager is now a facade over MemoryStore (public API stable — graph_build/MCP/CLI call sites unchanged); scratch sessions stay file-based
- Old `memory/*.json` data is NOT migrated (fresh start per user decision); files on disk are ignored

**Knowledge (knowledge/brief.json — the project brief):**
- Sections: project / one_liner / description / hard_rules (mandatory macros/branches/configs with type+evidence) / modes (usage scenario distinctions, e.g. pcie vs tcp vs rdma) / key_abstractions / conventions / pitfalls / query_paths / must_know / auto graph_stats
- Session-start protocol: `knowledge-brief` (alias `brief`) renders the prompt form — documented as Step 0 in SKILL.md (en/zh)
- `brief-update --set/--add/--remove/--refresh-stats` for small-scope adjustments; `brief-extract` bootstraps from graph stats; `brief-validate` enforces the size budget (warn >3000 chars, error >6000) and flags graph drift >20%
- The old knowledge/*.md system (extract/apply/query/validate commands, curation guard, MD packs, `knowledge_manager.py`) is fully removed; `import-foreign-knowledge` now copies the foreign brief; cgdb cross-branch knowledge merging retired (briefs are per-project curated); memory merging is DB→DB
- kb_index sources switched: memory from memory.db, knowledge from brief sections (new FTS kinds: hard_rule/mode/abstraction/...); MCP knowledge tool falls back to rendering the brief itself

**Tests:** +96 new/rewritten (test_memory_store 62, test_knowledge_brief 34); test_memory_manager*/test_memory_cmd rewritten for DB semantics; test_kb_modules/test_c2d_phase3 fixtures switched to DB+brief; old MD tests removed. Suite: 2067 tests / 110 files.

### Deep Audit Round 19 — test-coverage enrichment (276 new cases, 10 files, 4 bugs found by the new tests)

Systematic interface-by-interface test enrichment driven by a static coverage map (public functions per module vs test references). New test files: test_search_cmd (44), test_query_cmds (41), test_transactions_wal_api (21), test_line_utils (14), test_logging_utils (23), test_memory_guard (36), test_cgdb_commands (34), test_memory_cmd (18), test_analysis_helpers (25), test_validate (20).

**Bugs found by the new tests (each fixed + regression-pinned):**
- `path --domain-filter` excluding an endpoint node crashed with a raw nx.NodeNotFound traceback (warning promised "No path will be found" then died) — now a graceful exit-1 error.
- `get-code-snippet` labeled every line one higher than its real file line (zip already yields 1-indexed numbers via range(start+1,…); the format string printed i+1).
- `search-memory` legacy path never hydrated answers for root-layout entries (the current default layout): scored results lacked root_id, so the is_root check was always False and root/root_N.json was never read.
- `explore-flow`'s exact-symbol-match fast path (#10 fix) only ran for LazySQLiteGraph — regular NetworkX graphs (JSON builds, small projects) always fell through to BM25 scoring. The Unicode-aware fallback loop now runs for any graph when the SQL lookup didn't fire.

**Coverage added:** search_cmd 6 commands (scoring tiers, LIKE escaping, vtable/domain/condition path filters, impact/neighbors semantics, SQLite fallbacks); query.py 11 command surfaces (describe-node exact-ID contract, trace/resolve/diff chains with bindings, blast radius, field access with NULL-form filter, param flow word-boundary, code snippet, provenance, blame, reverse trace entry-first ordering); transactions WAL + snapshot management APIs (crash-safety seq counter contract, batch applied marks, prune/restore/list); cgdb CLI commands against a real 13-layer store (read-only SQL guard, views, schema version, closures, node-id resolution ladder); memory_cmd save/search/validate lifecycle (merge, experience demotion, root/leaf layout); pure utils previously at 0% (line_utils, logging_utils incl. JSON formatter/StageTimer, memory_guard thresholds/gc gating/streaming writers); analysis helpers (code_slice, co_change against a real git repo, lock_coverage per-access locksets, key_paths, explore-flow); validate.py post-build checks (edge logic, call-chain integrity, data consistency, exit codes).

### Deep Audit Rounds 17–18 — transaction integrity, memory/knowledge correctness, concurrency false negatives, latent L3 tools

**Round 18 — remaining backlog:**
- Fix thread-context partitioning: callees of DIFFERENT thread entries with the same model all shared one (model, None) bucket — races between two thread families were never compared. Contexts now keyed by the entry's node id (thread_entry_id / thread_entry_inherited propagated at build time); same-named static thread routines in different files are distinct threads. Legacy graphs keep old behavior.
- Fix groupless lock patterns all mapping to one sentinel ('__rcu_read_lock__') — rcu_read_lock vs preempt_disable were 'the same lock', wrongly suppressing/annotating races. Per-pattern stable sentinels now.
- Fix GraphLock same-thread re-entrancy deadlock: nested write_lock inside transaction() blocked against itself for the full 60s timeout. Re-entrant acquisition served from an in-process registry; cross-thread/process contention unchanged.
- Fix remove_node during StreamingGraph's deferred window: flushed edge rows stayed in SQLite and residual batch entries re-introduced them at close(). Now deletes flushed rows (fatal on failure) and purges batch entries.
- Fix StreamingGraph.close() masking commit failures as 'cannot start a transaction within a transaction' — the original error (disk full/IO) now propagates.
- Fix trace_data_flow's recursive CTE (never walked multi-hop paths — result was from's direct deps ∪ target's deps); indirect_targets now filters by file via the call-edge join; path_sensitive_states resolves fn through ir_functions (was feeding a cgdb_nodes.id into an ir_functions-keyed table); find_macros N+1 batched into one query; cgdb schema v5 adds idx_tokens_ast_node (find_symbol/delete_node were full-scanning tokens).
- Fix writeback begin() swallowing snapshot failures (tx registered with no rollback path — rollback silently 'succeeded' at nothing). Snapshot creation now fatal, failing cleanly before any edit applies.
- Fix query silent degradations: field-flow's partial reader list presented as complete (false negative on the race analysis itself); CTE→networkx fallback invisible at debug level; unbounded '*1..' silently clamped to depth 5 (now a stderr NOTE); reverse-trace enumerated exponentially many paths before keeping 20 (capped at 10000 with a result flag).
- Fix memory: _pack_lite's top_questions was insertion order, not weight; decay archiving a root orphaned its leaves (now promoted to self-referencing roots).
- Fix knowledge writes racing daemon sync (whole batch under the graph write lock; degrades with warning on timeout).
- mcp _tool_search SQL fallback now logs at WARNING; transactions state writes fsynced (crash durability); deadlock-heuristic dead code removed; ANALYSIS LIMITATIONS text updated (TOCTOU is detected).

**Round 17 — transaction/memory/knowledge/concurrency/streaming integrity:**
- Fix snapshots missing WAL-resident commits (checkpoint first, -wal sidecar fallback; restore returns the exact snapshot state); restore surfaced sidecar-deletion failures instead of swallowing them.
- Fix tx-begin silently orphaning an active transaction (now rolls it back first, mirroring the context manager).
- Fix failed rollbacks reported as rolled_back (all three paths now keep the tx ACTIVE + retryable, preserve the WAL as evidence, exit nonzero).
- Fix memory_manager: corrupt JSON crashed every memory op (tolerated with defaults); 'stronger wins' merge condition was dead code; promote boosts erased by decay (now a persistent formula term); RMW cycles raced (flock across load→mutate→save, verified with threads and processes).
- Fix knowledge extract-knowledge silently overwriting hand-curated files (provenance-guarded writes; source precedence override > intentional batch > record > batch > inference).
- Fix TOCTOU pairing across structs (keyed by struct_chain+field) and missing thread-context check.
- Fix daemon perpetual sync storm when graph_dir == source_root (daemon's own .daemon_status.json writes re-entering the event stream); Round-15 grace test was a 1-in-10 lottery (fake violated the drain contract).
- Fix StreamingGraph failed edge reload wiping every streamed edge (deferred flag now stays set on failure); cross-flush edge attributes merge on reload; reload streams instead of fetchall.
- Fix describe-commit/node-history reporting a corrupt change_log as 'never tracked'; mcp_server _CGDB_STORE_CACHE leaked SQLite connections; stats reconciliation mismatch was invisible (now a stderr WARNING); transactions module docstring no longer promises unimplemented WAL replay/two-phase commit.

## [Unreleased] - 2026-09-03

### Deep Audit Round 16 — MCP design-report write path, query engine robustness, build-flow DB resilience

**P0 Critical Bug Fixes:**
- Fix `insert_token`/`delete_token` (MCP design-report) — direct `seq = seq ± N` UPDATEs transiently collided with not-yet-shifted rows under `UNIQUE(file_id, seq)` (SQLite updates in rowid order, not seq order); insert-after-any-anchor always failed with `UNIQUE constraint failed`. Both now shift through negative seq space (order-independent).
- Fix `alias_set` (MCP) — every call on a populated DB failed `no such column: a.analysis`: the query selected `a.analysis`/`a.function_id` which `alias_sets` never had. The `scope` param (a function *name*) was compared against the fabricated integer column. Scope now resolves to the function's node id and matches the OTHER alias endpoint's `enclosing_symbol_id`.
- Fix `add_function` (MCP) — inserted body tokens with `file_id NULL`, violating NOT NULL; any call with `body_tokens` failed wholesale. Tokens now require/validate a `file_id` and append after the file's MAX(seq).
- Fix MCP edit tools returned FAKE transaction ids (`edit_token_5`) — `commit_db_transaction` resolves ids via the `writeback_tx_file:{tx_id}` meta key these tools never wrote, so every commit failed "no such transaction_id" and rollback had no snapshot. Tools now open a real `WritebackPipeline` transaction before applying the edit; verified end-to-end (edit → rollback restores previous token).
- Fix rebuild duplication: `ingest_l1`'s per-file cleanup ran `DELETE ... WHERE file_id = ?` on `literals`/`attributes` (no such column — OperationalError swallowed by except-pass) and never touched `string_literals`: every rebuild appended a fresh copy of each file's literal rows (measured: 3 builds → 3× string_literals). Token-linked tables now cleaned via the file's token ids.
- Fix `delete_file_records` (cgdb_store) — rolled back `FOREIGN KEY constraint failed` for ANY L1-ingested file (cgdb_files delete cascades tokens → trips literals' NO ACTION FK), in both the main path and the no-nodes early-exit branch. L1 rows now deleted explicitly first; the method also never cleaned L1 tables at all.
- Fix `_CGDB_WIPE_TABLES` listed `predicates` — a table that never existed in any schema version (real table: `config_predicates`); every build since the init commit paid a spurious "no such table: predicates" warning/error.
- Fix `SQLiteStore.connect()` on a corrupt/non-SQLite `code2database.db` — bare `sqlite3.DatabaseError: file is not a database` traceback with no hint what to do. Now an actionable RuntimeError naming the file and remedy (original error chained). Regression tests pin connect() on fresh dir / 0-byte file / partial DB without `meta` (the exact "no such table: meta" crash) and the guarded wipe.

**P1 Query Engine Fixes:**
- Fix `LIMIT abc`/`LIMIT 2.5` crashed the parser with a bare ValueError (CLI only catches SyntaxError) — now a proper SyntaxError.
- Fix LIMIT 0 returned ALL rows on the networkx path (truthiness check) but 500 on the SQLite CTE path; LIMIT -1 returned 0 rows vs unlimited on the two paths. Canonical semantics now (both engines): negative = no limit, 0 = zero rows.
- Fix deep `WHERE ((((...))))` nesting (3000 levels) crashed with RecursionError — parser now caps paren depth at 100 with a SyntaxError.
- Fix `WHERE n.line < 'abc'` (int vs str) crashed with TypeError — ordering comparisons on mismatched types now follow SQL NULL semantics (row doesn't match).

**P2 Housekeeping:**
- Docs: CLI command count 222 → 226 everywhere (README badge en/zh, SKILL.md en/zh, skill*.json, AGENTS.md); AGENTS.md test counts updated (1664 tests / 93 files).
- Docs: daemon `startup_grace_sec` (env `CALLGRAPH_DAEMON_STARTUP_GRACE_SEC`), graph_dir self-exclusion, `daemon-status` grace keys, and the cgdb export-failure marker `.code2database_cgdb_export_failed.json` documented (README, RUNTIME_CONFIG en/zh, ops_commands en/zh, SKILL.md en/zh).

## [Unreleased] - 2026-09-02

### Deep Audit Rounds 13–15 — spawn pools, daemon feedback loops, cgdb export visibility

- Fix daemon FileWatcher reacted to its own graph_dir writes — perpetual sync loop via `.daemon_status.json` (a MONITORED `.json`); graph_dir subtree now excluded from watching.
- Fix daemon had no startup grace — restart after build/crash immediately re-synced the event flurry; `startup_grace_sec` (default 60, env `CALLGRAPH_DAEMON_STARTUP_GRACE_SEC`) holds events during the window; `wait-sync`/`force-refresh` end grace early.
- Fix three process pools forked the caller's whole state into workers (`run_l1_ingest`, `_extract_state_access_all` + pre-strip, `parallel.map_nodes`) — spawn context with initializer-based state injection; `map_nodes` gained a `BrokenExecutor` serial fallback.
- Fix cgdb export failure was a stderr-only WARNING — silent semantic-data loss; now ERROR + `<outdir>/.code2database_cgdb_export_failed.json` marker (cleared on success) + stdout summary warning.
- Fix graph_build read the scenarios file before the same build wrote it (SCENARIOS_SUMMARY.md); sqlite storage built the entire index/doc pipeline twice; web_ui/value_flow 500s on bad depth input (now 400).

## [Unreleased] - 2026-08-31

### Deep Audit Round 3 — Architecture, Implementation Logic, Crash Safety, Protocol Compliance

**P0 Critical Bug Fixes:**
- Fix `_run_transactional_sync` in daemon.py — was permanently dead code since it was written: `GraphLock(graph_dir, mode="w", timeout=30.0)` always raised `TypeError` (GraphLock.__init__ only accepts `graph_dir`), `lock._release()` was `AttributeError` (method is `release()`), and lock timeout was never checked (sync ran without lock). Rewrote using `write_lock()` context manager. On timeout/failure, now re-queues actual file paths (was only restoring count, losing paths forever).
- Fix `LazySQLiteGraph._node_neg_cache` eviction crash — `set.keys()` + `del set[k]` → `AttributeError` + `TypeError`. Triggered after 10K missing-node lookups (guaranteed crash for long-lived LSP/MCP servers on large graphs). Converted to `OrderedDict` with `popitem()`.
- Fix `restore_snapshot` non-atomic per-file — `os.remove(dst)` + `shutil.copy2(src, dst)` left NO database file if killed mid-restore. Now uses temp-file + fsync + `os.replace` (crash-safe).
- Fix `cmd_tx_restore` ran without write lock — concurrent daemon sync or another transaction could corrupt the DB. Now acquires write lock with 10s timeout.

**P1 High-Impact Bug Fixes:**
- Fix daemon `pause` socket command was a no-op — main loop never checked `self.state.paused`, so daemon continued syncing after `daemon-pause`. Now checks at top of each iteration and sets `STATUS_PAUSED`.
- Fix `_prune_old_events` hardcoded 60s window — ignored configurable `circuit_breaker_window_sec` (user who set 120s thought they widened the window but daemon still counted only last 60s).
- Fix `FileWatcher._run_polling` referenced daemon-only attributes (`self.graph_dir`, `self._pending`, `self._pending_lock`, `self.state`) — `AttributeError` caught by try/except, so DB-aware change detection silently no-op'd in polling fallback mode. Fixed: added `graph_dir` param to FileWatcher, uses `self._callback()` instead of daemon internals.
- Fix LSP server advertised but unimplemented capabilities (`documentSymbolProvider`, `workspaceSymbolProvider`) — editors sent requests, server returned `-32601` errors. Removed from ServerCapabilities.
- Fix LSP `run_stdio` header parser: accepted only CRLF framing (hung on LF-only clients), crashed on malformed `Content-Length` (no try/except), crashed on malformed JSON, didn't handle partial body reads, infinite loop on EOF. Now handles all edge cases with `-32700` Parse error responses.
- Fix LSP `_send` mixed text+binary stdout — `\r\r\n` on Windows. Now writes entire response to `stdout.buffer` in one binary write.
- Fix `StreamingGraph.close()` not idempotent — second call executed `DELETE FROM functions` + wrote zero functions → empty table. Added `_closed` flag.
- Fix `LazySQLiteGraph` missing `__del__` — fd leak when callers don't use `with`. Added `__del__` calling `close()`.

**P2 Implementation Logic Fixes:**
- Fix `intent_router` 3 misrouted commands: `call-chain`→`path`, `race-detect`→`detect-races`, removed `find-concepts` (no such command existed).
- Fix `query_router` no-op self-aliases (`route_invokers = route_invokers`).
- Fix `query_router` 3 `print(stderr)`→`_log.warning()` for consistent logging.
- Fix `query_cache` dead node-version check — read from live map and snapshot built from same map, so `!=` branch never fired. Removed dead code, updated docstring.
- Fix `build_phases` goto exception logger: DEBUG→WARNING with edge IDs.
- Remove 2 dead return values: `_edge_target_index` from `_create_empty_conditional_nodes`, `_known_struct_types` from `_build_struct_embedding_index`.
- Fix MCP: `ImportError` for `mcp_report_tools` was silently swallowed (server started with 53 tools instead of 81 with no warning). Now logs at ERROR.
- Fix MCP: 7 tools missing required-param validation (`_tool_explain_label`, `_tool_edit_token`, `_tool_data_lifecycle`, `_tool_domain`, `_tool_knowledge_query`, `_tool_memory_search`, `_tool_composite_query`).
- Fix MCP: `_tool_audit_log` missing `int()` type coercion for `limit`.
- Fix MCP module docstring: listed 13 of 81 tools. Updated to full breakdown.
- Fix `cmd_kb_migrate` silent per-row exception swallowing — now counts failures, logs at WARNING, includes `"failed"` count in JSON output.
- Refactor `null-source` alias from hand-copied 5 args to `parents=[p_ff]` pattern (inherits all field-flow args automatically).
- Fix 3 resource leaks: `export_mermaid.py`, `graph_diff.py`, `mcp_server.py` (2 sites) — `conn.close()` was outside `finally` block, leaked on non-sqlite3 exceptions.
- Fix `web_ui.py` `threading.Timer` was non-daemon and never cancelled — delayed process exit by 0.5s on shutdown. Now daemon + `cancel()` in `finally`.

**CLI Builder Improvements:**
- Fix module docstring shadowed by `import logging` on line 2 — `__doc__` was `None`.
- Remove dead import `_detect_build_system` (never referenced).
- Add `--version` flag (prints `code2database_builder 1.3.0`).
- Add `RuntimeError` guard in alias loop if canonical subparser missing (was silently skipped).
- Wrap `parser.parse_args()` in try/except `KeyboardInterrupt` → exit 130.

**Dead Code Removal:**
- Remove 4 dead `cmd_*` wrappers in `c2d_phase3.py` (code2database_builder.py has its own versions).

**Documentation:**
- Update MCP module docstring from 13 to 81 tools.
- Fix README MCP tool examples: were CLI command names (`explore-flow`, `detect-races`), now actual MCP tool names (`code2database_explore`, `code2database_detect_races`). Synced zh/README.md.
- Fix stale D37 comment: `16 -> 30+` → `16 -> 34`.

**Test Coverage:**
- New `tests/test_streaming_graph.py` (8 tests): LazySQLiteGraph negative cache eviction, basic read ops, StreamingGraph close idempotency.
- Add `test_module_has_docstring`, `test_version_flag_prints_version_and_exits_zero` to test_cli_command_coverage.py.
- Tighten `test_help_lists_at_least_26_commands` → `_200` (was too loose for 222 commands).
- Add `test_no_unimplemented_capabilities_advertised` to test_lsp_server.py.
- Update intent_router tests for corrected command names.
- **Test suite**: 1605 tests across 88 files (was 1583/87).

## [Unreleased] - 2026-08-29

### Deep Audit Round 2 — Architecture, Doc Sync, CLI Aliases, Dead Code

**P0 Critical Bug Fix:**
- Fix 12 SKILL.md CLI aliases (`describe`, `trace`, `find`, `flow`, `concurrency`, `context`, `save`, `recall`, `know`, `health`, `daemon`, `export`) not registered as argparse subparsers — running them produced "invalid choice" error. Registered each as a subparser inheriting all arguments from its canonical form via `parents=[canonical_parser]`.

**Documentation Sync Fixes:**
- Fix `skill.json` `mcp_tools_count` 53→81 and add `design_report: 28` to `mcp_tools_breakdown`
- Fix `server.json` description to include "+ 28 design-report" (was 34+19=53, not 81)
- Fix `mcp_report_tools.py` docstring 27→28 (actual tool count)
- Fix `skill.json` `tier_1_commands` (27→24, matching SKILL.md Core Commands table)
- Fix `skill_analysis.json` `tier_1_commands` (6→13, matching SKILL_analysis.md table)
- Fix `skill_ops.json` `tier_1_commands` (11→23, matching SKILL_ops.md table)
- Fix `skill_analysis.json` `cgdb_mcp_tools` array (18→19, add missing `cgdb_get_source`)
- Fix `skill.json` `commands` array (123→222, now lists all subparsers)
- Fix `skill.json` `core_commands` (27→24)
- Fix `CHANGELOG.md` line 247 "27 core" → "24 core"
- Fix `docs/en/SKILL_ops.md` description "30 Tier-1" → "23 Tier-1"
- Fix `docs/zh/SKILL_ops.md` description "30 个 Tier-1" → "23 个 Tier-1"
- Fix `install.sh` line 781 "50 tools (31+19)" → "81 tools (34+19+28)"
- Fix `docs/en/OVERVIEW.md` + `docs/zh/OVERVIEW.md`: remove references to deleted modules (`ir_adapters.py`, `lsp_backend.py`, `visualizer.py`), fix stale counts (214→222 subcommands, 55→87 test files, MCP 81=34+19→81=34+19+28)

**check_docs_sync.py Fix:**
- Change non-recursive `glob("*.md")` to recursive `rglob("*.md")` — previously skipped entire `references/` subdirectory (13 files × 2 languages). Now checks all 20 files (was 6).
- Fix `docs/zh/references/manifest_schema.md` — add missing "示例 manifest" section + JSON code block (was 9 headings/6 code blocks, now 10/8 matching EN)
- Fix `docs/en/references/memory_knowledge.md` — add missing "Unified KB Query (Phase 1-3 Upgrade)" section, update "Future (planned)" → implemented, add `kb_index` API examples (now matches ZH)

**Dead Code Removal:**
- Remove `scripts/_builder/ir_adapters.py` (818 lines) — stub framework, never imported/wired
- Remove `scripts/_builder/lsp_backend.py` (182 lines) — unfinished extraction backend, never imported
- Remove `scripts/_builder/visualizer.py` (272 lines) — superseded by `export.py` + `export_mermaid.py` + `web_ui.py`
- Remove `generate.py` (root, 2 bytes) — dead placeholder file

**Logging Migration:**
- Migrate 25 `print(..., file=sys.stderr)` → `logging` calls across 8 modules that had zero logging: `web_ui.py` (5), `value_flow.py` (3), `data_dep.py` (1), `invariants.py` (5), `explain.py` (1), `memory_cmd.py` (2), `query_router.py` (7), `runtime_guards.py` (1)

**Test Coverage:**
- Add `TestCLIAliasesRegistered` class with 2 tests verifying 12 aliases appear in `--help` and inherit identical arguments from canonical subparsers

## [Unreleased] - 2026-08-28

### Deep Audit — Code Quality, Performance, Test Coverage, Doc Sync

**P0 Critical Bug Fixes:**
- Fix `hybrid_search.py` missing `defaultdict` import (NameError on dense embedding channel)
- Fix `neural_embed.py` cosine_similarity formula bug (nb computed as dot product instead of b's norm)
- Fix `action.yml` 4 bugs (placeholder URL, wrong --query flag, missing --json flags)
- Fix `transactions.py` unconditional `import fcntl` (Windows broken despite docstring claiming msvcrt fallback)
- Fix `_LazyNodeView` missing `.get()` method (large-graph extract-signals crash on SPDK/Kernel)
- Fix `add_foreign_stub` DETACH-before-commit ("database is locked")
- Wire LSP server CLI subparser + register in _LAZY_IMPORTS + 16 tests

**Performance Fixes (from BUG reports + re-audit, 20+ findings):**
- `_FIELD_ACCESS_RE`/`_FIELD_WRITE_RE`/`_FN_PTR_PARAM_RE` hoisted to module level (1.5M+ re.compile calls eliminated)
- `_extract_state_access` shadowed-locals path: pre-compiled regex + filter instead of per-function recompile (~62h → ~30s on kernel)
- Pre-strip state_access loop parallelized via ProcessPoolExecutor with fork COW
- Scanner flush: cross-flush regex cache reuse (30× → 1× compile)
- `_trace_object_access`: single finditer cache instead of 9× full-body scans per function
- `map_nodes` extended with `parallel_mode='process'` support
- `_detect_thread_models` parallelized via `map_nodes`
- Background aggregator thread for scanner (eliminates serial result-merge bottleneck)
- Scanner `--parallel-mode process` + c_scanner/import_resolve/memory_ordering regex hoists
- `detect_data_races` O(M²) pair iteration → thread-context partitioning (O(M×K))
- `_detect_toctou_patterns` per-(node×pattern) regex recompile → pre-compile once per profile
- `value_flow` hop regex cached in `_ASGN_RE_CACHE`
- Split-output auto-enable threshold lowered 10K→5K files

**Code Quality:**
- 5 `json.loads(open().read())` resource leaks → `with open() as f: json.load(f)`
- 1 builtin shadow renamed (`file` → `file_record` in cgdb_store.py)
- `/tmp` socket path hardened (respects `$TMPDIR`)
- `/proc/meminfo` fallback improved (psutil → resource.getrusage → 50%)
- inotify bit-flags extracted to named constants
- 230 unused imports removed (AST-verified, 90 files)
- 441 silent `except Exception: pass/continue` sites annotated with logging.debug
- Web UI offline support (local cytoscape.js + CDN fallback)

**Test Coverage (+331 tests, 1250 → 1581):**
- test_lsp_server.py (16), test_hybrid_search.py (20), test_neural_embed.py (13)
- test_sarif_output.py (18), test_ast_pattern.py (33), test_taint_analysis.py (14)
- test_code_intelligence.py (21), test_invariants.py (25)
- test_concurrency_analysis.py (18), test_data_dep.py (11)
- test_commit_meta.py (19), test_update_cmd.py (28), test_profile_generate.py (17)
- 3 pre-existing test skips fixed (ProfileHealth API, BenchmarkResult class name, WAL lock)

**Documentation Sync:**
- CLI command count: 213→222 (214 builder + 8 scanner)
- MCP tools: 53→81 (53 base + 28 design-report)
- Tier-1 counts unified: 24/13/23 (was 15/13/14 in some docs)
- All 222 CLI subparsers documented in usage_reference.md (en+zh)
- `check_docs_sync.py` regex fixed (dead `callgraph_` prefix → `code2database_/cgdb_`)
- `build_phases.py` added to OVERVIEW.md module tree
- AGENTS.md Testing section updated with 12+ new test modules
- `server.json` version 1.0.0→1.3.0

**Architecture:**
- `build_graph()` decomposed: 23 phases extracted to `build_phases.py` (620 lines saved)
- `action.yml` hardened (configurable repository URL, correct flag references)
- Scanner `--parallel-mode {thread,process}` for true multi-core scanning

### Phase J — Parallelism audit + ProcessPoolExecutor helper

- `scripts/_builder/parallel.py`: add `map_files_processpool` helper for
  true multi-core file-level parallelism via `ProcessPoolExecutor` (spawn
  context). Bypasses the GIL for CPU-bound tree-sitter post-processing.
  Worker function must be a top-level module function (not a lambda/closure);
  see the docstring for usage and memory caveats.
- `scripts/_builder/parallel.py`: expanded the module docstring to document
  the GIL caveat — Python tree-sitter releases the GIL during parse, but
  per-node post-processing (AST walking, dict building) holds it. On
  CPU-bound workloads where post-processing dominates, ThreadPoolExecutor
  saturates a single core even with 48 threads. `map_files_processpool`
  is the escape hatch.
- **Audit conclusion**: the existing `ThreadPoolExecutor` in
  `code2database_scanner.py:903` is the correct default — tree-sitter and
  `re` release the GIL during the dominant cost (parse, regex matching),
  so multi-threading yields real speedup. The new `map_files_processpool`
  is available for the rare case where Python post-processing dominates.

### Phase A-H — Deep audit fixes (9 deficiencies)

Phases A-H address 9 deficiencies identified in the deep verification audit
(see `Skill修改记录报告_续篇.md`). Each phase is committed separately.

- **Phase A (Fix #2)**: `path --source-file` now actually filters by source
  file. Previously the flag was accepted but silently ignored.
- **Phase B (Fix #11 + new #1/#2)**: doc/code/skill.json consistency —
  schema drift between `SKILL.md` examples, `usage_reference.md`, and the
  actual CLI parsers fixed.
- **Phase C (Fix #9)**: `--domain-filter` flag on `trace-chain` and
  `reverse-trace` for cross-domain long-chain tracking.
- **Phase D (Fix #6)**: guard-function runtime semantics annotation —
  `guard_functions` from profile now feed into `path-feasible` and
  `path-guards` via `runtime_guards.profile_bindings`.
- **Phase E (Fix #7)**: object ownership / origin tracking — profile-declared
  `allocation_sites` enable `object_origin` field on field-write suspects.
- **Phase F (Fix #10)**: lock semantics / HOLDER edges — profile-declared
  `lock_semantics` produce `lock_held_context` evidence in `detect-races`.
- **Phase G (Fix #15)**: query result cache with three-layer invalidation
  (TTL + graph mtime + node-version) and `--no-cache` bypass flag on
  `path`, `describe-node`, `trace-chain`, `reverse-trace`, `explore-flow`.
- **Phase H (Fix #1, P0)**: FIELD_WRITE suspects integration — `reverse-trace`
  gains `--suspect-field` / `--suspect-value` / `--suspect-struct` flags that
  pull writers from the `field_access` table and include them in the output
  with reverse-BFS call chains, `guard_condition`, `object_origin`, and a
  `reachable_in_scene` verdict. Closes the P0 gap that `reverse-trace` could
  see callers of the crash point but not the field-write suspects that may
  have caused the crash.

## [1.3.0] - 2026-08-25

**Major feature release**: multi-project aggregate build + cross-C2D live sync.

### Multi-project support (Phase 1-3)
- `build-multi` command: manifest-driven joint C2D from A→B→C interdependent projects. Forces project-name domain prefix (A_init vs B_init never collide). Aggregates include paths, merges compile_commands.json. Reuse mode imports from existing C2D via ATTACH.
- `c2d-add-foreign` / `c2d-sync-foreign` / `c2d-list-foreign` / `c2d-remove-foreign`: cross-C2D reference tracking via foreign_refs + watched_c2ds tables. SQLite ATTACH for read-only cross-db queries.
- `composite-query`: cross-C2D JOIN (CALLERS_OF / CALLEES_OF across local + foreign dbs).
- `c2d-check-compat`: verify B's foreign_refs against new A version (broken/signature-changed/ok).
- `coverage-cross-c2d`: test coverage analysis across C2Ds (which A functions are called by test_A).
- `export-mermaid --multi`: project-level dependency graph visualization.
- `c2d-add-foreign-stub`: vendor SDK signature-only stub C2D (glibc/kernel/DPDK).
- `ffi-auto-link`: auto-link FFI bindings to watched foreign C2Ds.
- `scan-rpc`: scan source for HTTP/gRPC calls, create rpc_endpoint stub nodes + edges.
- `import-foreign-knowledge`: copy foreign C2D's knowledge/*.md into local with project prefix.

### Deep audit fixes (P0)
- `describe-node` transparent foreign_ref fallback (F1): returns foreign callee metadata via ATTACH.
- `kb-query` ATTACH foreign dbs (F2): searches A's knowledge when B's local KB is thin.
- `kb-cluster` uses Jaccard token-set similarity (C1 fix): BM25 was relevance ranking, not similarity.
- Daemon watches foreign db mtimes (F9): auto-syncs foreign_refs when A updates.
- `c2d-add-foreign` signature disambiguation (C2): handles C++ overloads by matching signature.
- MCP tools: 3 new (code2database_foreign_refs, code2database_sync_foreign, code2database_composite_query). Total 53.
- Security: kb-global-share path traversal check (S1), kb-global-import JSON nesting guard (S2), c2d-add-foreign SQLite validity check (S4).
- Daemon foreign sync throttle (P3): min 60s between runs.

### Stats
- 4 new modules: build_multi.py, c2d_foreign.py, c2d_phase2.py, c2d_phase3.py.
- 12 new CLI commands. Total 196.
- 3 new MCP tools. Total 53.
- SCHEMA v13: foreign_refs + watched_c2ds tables.

## [1.2.0] - 2026-08-25

**Major feature release**: unified knowledge base (kb-*) with FTS5+BM25 across
memory + knowledge + global stores. 13 new kb-* CLI commands, 1 new MCP tool,
5 new modules, 3 new SQLite tables, 4 schema migrations.

### Phase 0 — Urgent BUG fixes
- **Storage path split**: `cgdb_merge` and `cgdb_suggest` read from
  `.code2database_memory` and `.code2database_knowledge` (always empty);
  `save-memory` and `apply-knowledge` write to `memory/` and `knowledge/`.
  Unified to canonical paths; rewrote 4 functions in `cgdb_merge.py` with
  root+leaf+experience loading and non-dict sanitize.
- **LazySQLiteGraph incompatibility**: `cmd_update` had early detection
  (prior commit); this release covers the remaining 5 mutation sites —
  `cmd_merge`, `cmd_sync`, `merge_change_graph`, `cmd_apply_semantics`,
  `_mark_file_stale`. New shared helper `_ensure_mutable_graph` in utils.py.
- **ARG_MAX**: `graph_build.py:6525` joined thousands of CONFIG_* macros
  into a single `--macros` CLI arg (kernel exceeds ~128KB ARG_MAX). Added
  `--macros-from <file>` to scanner + `_parse_macros_file` helper;
  graph_build uses tempfile when joined length > 8KB with finally cleanup.
- **Daemon memory invalidate**: `daemon.py` had zero memory references;
  source updates left memory `node_ids` as dangling pointers. Added
  `_invalidate_stale_memory_after_sync()` called from `_sync_incremental`
  and `_sync_bulk`.

### Phase 1 — FTS5 schema + kb-rebuild-index (SCHEMA v9)
- New table `kb_paragraphs` + `kb_paragraphs_fts` (porter + unicode61
  tokenizer) + AI/AD/AU triggers + 3 indexes.
- New module `scripts/_builder/kb_index.py`: `rebuild_kb_index()` walks
  `memory/{root,leaf,experience}/*.json` + `knowledge/*.md` (paragraph-split
  via `##` headings) and bulk-inserts. `query_kb()` returns ranked hits
  with `see_also` and `access_count` bump.
- New CLI `kb-rebuild-index`.

### Phase 2 — memory/knowledge search upgrade
- `cmd_search_memory`, `_tool_memory_search`, `KnowledgeManager.query_knowledge`,
  `_tool_knowledge_query` all try FTS5+BM25 first, fall back to legacy
  Jaccard / substring search when no db.

### Phase 3 — unified query interface
- New CLI `kb-query` with `--kinds` / `--min-weight` / `--max-tokens` /
  `--semantic` / `--global` flags.
- New MCP tool `code2database_kb_query` (TOOLS dict 49 -> 50).
- `cmd_query` (Cypher) injects top-3 kb hits as `_hints` alongside rows,
  realizing the SKILL.md `memory -> knowledge -> graph -> source` chain.
- `cmd_describe_node` returns `memory_refs` + `knowledge_refs`.
- `_build_context_pack` merges `.memory_pack_lite` + `.knowledge_pack_lite`
  into the main context_pack as `memory_summary` + `knowledge_summary`.

### Phase 4 — clustering (SCHEMA v10)
- Added `scope_id` / `canonical_id` / `principle_ref` columns to kb_paragraphs.
- New module `kb_cluster.py`: union-find on FTS5 BM25 > threshold, picks
  canonical (highest weight x confidence), links `memory_qa` -> principle.
- New CLI `kb-cluster`. `query_kb` returns `see_also` from same cluster.

### Phase 5 — embedding schema (SCHEMA v11)
- Added `embedding BLOB` column. `kb-query --semantic` flag interface in
  place; degrades to FTS5 when sentence-transformers unavailable.

### Phase 6 — kb_items unified table (SCHEMA v12)
- New fact-level table `kb_items` + `kb_items_fts` with `versions_json`,
  `decay_class`, `provenance_commit`, `provenance_operator`.
- New CLI `kb-migrate` copies `kb_paragraphs` -> `kb_items` with kind-based
  `decay_class` assignment.

### Phase 8 — cross-project global KB
- New module `kb_global.py`: `~/.code2database_global_kb/global.db` with
  `kb_global` + `kb_global_fts`. `global_add` / `search` / `share` / `import`.
- 4 new CLI: `kb-global-add`, `kb-global-search`, `kb-global-share`,
  `kb-global-import`. `kb-query --global` falls back when project KB empty.

### Phase 9 — feedback loop
- New table `kb_query_log` records every `query_kb` call (matched / count /
  top_score / timestamp).
- New CLI `kb-known-unknowns` aggregates unmatched queries (occurrences >= N).

### Phase 10 — knowledge audit
- New module `kb_audit.py`: `audit_kb()` reports counts by kind, stale items
  (90d untouched), low-confidence (<0.5), high-citation (top access_count),
  most-linked principles, and optional 'what we know about X'.
- `write_audit_log_entry()` reuses the existing `audit_log` table.
- New CLI `kb-audit`.

### Phase 11 — conflict & rollback
- New module `kb_conflict.py`: `detect_conflicts()` pairwise-scan within
  clusters for 14 contradiction word pairs (yes/no, must/must not,
  always/never, safe/unsafe, ...). `rollback_kb_item()` restores from
  `versions_json` (saves current as new version first).
  `forget_kb_paragraph()` immediately deletes with `audit_log` entry.
- 3 new CLI: `kb-conflict`, `kb-rollback`, `kb-forget`.

### SKILL.md aliases
- `save` -> `save-memory`, `recall` -> `search-memory`, `know` ->
  `knowledge-query` (so SKILL.md Quick Reference commands work as documented).

### Doc sync
- 16 `.md` / `.json` files updated for new counts (53 MCP tools /
  34 code2database_* / 222 CLI commands / 24 core).
- `docs/en/references/memory_knowledge.md` and `docs/zh/references/memory_knowledge.md`
  rewritten to match actual code schema (removed fictional `mem_xxx` /
  `topic` / `fact` / `source` / `confidence` / `related_functions` /
  `graph_version` fields; removed `--threshold-days` fictional param;
  replaced with real entry schema + Markdown file schema).
- `docs/{en,zh}/OVERVIEW.md` directory tree updated with 5 new `kb_*.py`
  modules.
- `docs/{en,zh}/SKILL_ops.md` Quick Reference updated with 8 new kb-* ops
  commands; Tier-1 count bumped 14 -> 22.
- `docs/{en,zh}/references/data_model.md` adds kb_paragraphs / kb_items /
  kb_query_log table descriptions.
- `skill.json` tier_1_commands: 20 -> 24 (added `kb-query`,
  `kb-rebuild-index`, `kb-audit`, `kb-forget`).
- `skill_ops.json` tier_1_commands: 5 -> 8 (added `kb-rebuild-index`,
  `kb-audit`, `kb-forget`).
- `skill_analysis.json` tier_1_commands: 5 -> 6 (added `kb-query`).

### Stats
- 5 new modules: `kb_index.py`, `kb_cluster.py`, `kb_global.py`,
  `kb_audit.py`, `kb_conflict.py`.
- 13 new kb-* CLI commands (CLI total 171 -> 184).
- 1 new MCP tool `code2database_kb_query` (MCP total 49 -> 50).
- 3 new tables (`kb_paragraphs` + FTS5, `kb_items` + FTS5, `kb_query_log`).
- 4 schema migrations (v9-v12).

## [1.1.2] - 2026-08-22

Patch release refactoring the Web UI to **cytoscape.js 3.28.1** for dense-graph UX.

### Web UI refactor (WEBUI-REFACTOR)
- cytoscape.js 3.28.1 inlined (CDN load; offline mode: npm pack cytoscape@3.28.1)
- Three layout algorithms (flow/rings/force)
- Focus+context fading
- Edge bundling (curve-style: bezier)
- Community compound nodes
- LOD label hiding
- Edge `call_condition` labels
- Selector-based edge filter
- Incremental sync (syncCyFromModel)
- Preserved features (17 endpoints, cumulative model, nav, breadcrumb, etc.)

### Documentation updates
- New `docs/{en,zh}/references/web_ui.md` (17 endpoints + features + shortcuts)
- README.md, docs/zh/README.md, AGENTS.md: HTML/SVG/JS → HTML/cytoscape.js/JS
- Badge fix: 7+ASM → 6+ASM (C/C++ shared scanner)

### Tests
- 44 Web UI tests pass (17 HTTP endpoint + 23 GraphCache + 4 import)

## [1.1.0] - 2026-08-18

This release closes major gaps identified in `report/Code2Database-最终差距分析与优化报告.md`
vs the design report `report/C代码数据库化方案-分析与执行报告.md` v4.0. It
adds the design-report appendix B/C (L1/L3/L4/多库/跨语言) tables, the source
renderer, the transactional write-back loop, 28 design-report MCP tools,
an IR-adapter framework, an L1 token-ingest module, expanded evals, and 4
new test files.

### Schema (cgdb v4 — design-report appendix C)

- **Schema version bumped 3 → 4**. New tables added side-by-side with
  existing cgdb tables (idempotent migration; v3→v4 ALTERs existing tables
  for additional columns).
- **Report-L1 无损重建层 (10 tables)**: `tokens` (with `preceding_whitespace`,
  `byte_offset`, `spelling`), `macros`, `macro_invocations`, `pp_branches`,
  `pp_directives`, `pragmas`, `attributes`, `literals`, `string_literals`
  (with `security_flags`), `comments_freeform` + `comments_fts` (FTS5).
  Plus `source_files_meta` (encoding/line_ending/has_bom/disk_sha256/
  rendered_sha256).
- **Report-L3 IR 层 (7 tables)**: `ir_functions`, `ssa_values`,
  `mem_accesses`, `points_to`, `indirect_calls`, `data_deps`, `path_states`.
  Existing `alias_sets` table extended with `function_id`/`ssa_value_id`/
  `alias_ssa_value_id`/`analysis` columns.
- **Report-L4 派生层 (6 tables + 1 extended)**: `call_graph_reachability`,
  `module_deps`, `function_embeddings` (BLOB), `precise_write_sets`,
  `arch_metrics`, `history_snapshots`, `alignment_errors`. Existing
  `graph_versions` extended with `sha256`/`snapshot_at`.
- **Report-多库 routing (2 tables)**: `db_routing`, `precompute_tasks`.
- **Report-跨语言 bridge (6 tables)**: `cross_lang_bindings`, `type_mappings`,
  `ffi_call_sites`, `language_adapters` (with 7 backfilled rows for
  c/cpp/rust/go/java/python/asm), `runtime_observations`, `dependencies`.
- **Naming clarification**: legacy cgdb layers renamed (in docs) as
  CGDB-L0 through CGDB-L11 to disambiguate from design-report L1~L4.
  No code renames (backward compat preserved).

### Source renderer + sha256 consistency (P0-6/7)

- New module `scripts/_builder/source_renderer.py`:
  - `SourceRenderer.render(file_id)` — DB tokens → character-level source bytes
  - `verify_consistency(file_id)` — render DB → sha256 vs disk sha256;
    mismatch recorded in `alignment_errors` (layer='L1',
    error_kind='sha256_mismatch')
  - `verify_all_files()` — bulk consistency check after L1 ingest
  - `update_disk_sha256(file_id)` — refresh after write-back

### Transactional write-back loop (P0-8)

- New module `scripts/_builder/writeback_pipeline.py`:
  - `WritebackPipeline.begin(file_id)` — start a write-back transaction
  - `WritebackPipeline.commit(tx_id, run_compile, run_lint, run_clang_format, git_commit, ...)`
    runs all gates in order: render → (clang-format) → compile → lint →
    sha256 verify → write to disk (atomic .tmp + rename) → git commit
  - On any gate failure: rollback entire transaction (DB snapshot restore,
    .tmp file deleted)
  - Module-level entry points `commit_db_transaction` and
    `rollback_db_transaction` for MCP tool binding

### 28 new design-report MCP tools (P0-9)

- New module `scripts/_builder/mcp_report_tools.py` with 28 MCP tools
  matching design-report appendix B signatures:
  - **L1 (8)**: `render_source`, `verify_consistency`, `edit_token`,
    `insert_token`, `delete_token`, `find_macros`, `get_pp_branches`,
    `get_string_literals`
  - **L2 (8)**: `find_symbol`, `callers_of`, `callees_of`, `who_writes`,
    `who_reads`, `get_context`, `impact_analysis`, `get_module_view`
  - **L3 (7)**: `indirect_targets`, `alias_set`, `trace_data_flow`,
    `cfg_of`, `path_sensitive_states`, `precise_write_set`, `dead_code_in`
  - **写回 (2)**: `commit_db_transaction`, `rollback_db_transaction`
  - **高级编辑 (3)**: `insert_node_after`, `delete_node`, `add_function`
- `mcp_server.py` TOOLS dict now has 77 tools (49 existing + 28 new).

### IR Adapter framework (P1-3)

- New module `scripts/_builder/ir_adapters.py` with abstract base class
  `IRAdapter` and concrete adapters for 18 languages:
  - Tier A (LLVM): `LLVMIRAdapter` for C/C++/Rust/Swift/Zig/CUDA
  - Tier B: `JimpleIRAdapter` for Java/Kotlin/Scala,
    `MSILIRAdapter` for C#/F#/VB.NET, `GoSSAAdapter` for Go
  - Tier C: `PythonBytecodeAdapter` for Python, `TSCompilerAdapter` for
    TypeScript/JavaScript, `_NoIRAdapter` for Lua/Shell
- `get_ir_adapter(language)` factory + `list_supported_languages()` registry.
- Each adapter gracefully degrades when external toolchain not installed.

### L1 ingest module (P1-1/2)

- New module `scripts/_builder/l1_ingest.py`:
  - `ingest_l1(conn, file_path, file_id, ...)` populates L1 tables
    (tokens/macros/macro_invocations/pp_branches/pp_directives/pragmas/
    attributes/literals/string_literals/comments_freeform) using
    libclang's Lexer (`tu.cursor.get_tokens()`) + PPCallbacks simulation
    (cursor walk for MACRO_DEFINITION/MACRO_INSTANTIATION/INCLUDE_DIRECTIVE).
  - Computes `preceding_whitespace` from byte gap between consecutive tokens.
  - Auto-detects BOM / line-ending (CRLF/LF/CR).
  - Computes `security_flags` for string literals (format_string /
    sql_injection / shell_injection / path_traversal).
  - Builds `pp_branches` tree via regex on source lines.
  - Falls back to disk-sha256-only ingest when libclang unavailable.

### Evals expanded to 43 tasks (P0-10)

- `evals/evals_en.json` and `evals/evals_zh.json` expanded from 3 → 43
  tasks across 7 categories:
  - multilang_baseline (3): existing C/Go/Python basic call chains
  - bug_localization (10): NULL deref, double-free, deadlock, off-by-one, ...
  - feature_implementation (10): new functions, callbacks, #ifdef paths, FFI
  - concurrency_risk (5): data races, deadlocks, RCU, atomics
  - data_flow_tracing (5): param flow, NULL sources, global writers
  - macro_related (5): macro definitions, expansions, dispatch, token-paste
  - refactoring (5): extract, rename, inline, move, switch→vtable

### 4 new test files (P0-11)

- `tests/test_transactions.py` — end-to-end transactional write-back tests
  (render, verify_consistency, commit, rollback, edit_token+writeback,
  alignment_errors insert/query)
- `tests/test_web_ui.py` — Web UI HTTP endpoint tests (server start,
  /health, /, module import)
- `tests/test_bug_benchmark.py` — BUG benchmark module tests
  (GraphInvestigator, GrepInvestigator, BenchmarkResult schema)
- `tests/test_profile_health.py` — Profile health scoring + evolution tests
  (score range 0-100, 7 documented categories, EvolutionSuggestion)

### Documentation updates (P1-10/11)

- `docs/en/OVERVIEW.md` and `docs/zh/OVERVIEW.md` updated with naming
  clarification note distinguishing cgdb layers (CGDB-L0~L11) from
  design-report layers (Report-L1~L4 + 多库 + 跨语言).
- New analysis report `report/Code2Database-最终差距分析与优化报告.md`
  documenting 13 core gaps + 49 optimization items with priority ratings.

## [1.0.0] - 2026-08-11

First public release. Code2Database scans C/C++/Go/Python/Java/Rust/ASM codebases and builds a persistent, queryable code graph database (cgdb) capturing invocation ordering, conditions, conditional compilation paths, concurrency analysis, data race detection, field-level access tracking, and the cgdb semantic layers (AST nodes, types, config predicates, CFG, data flow, ops_bindings, sync primitives, happens-before, provenance, time-travel versions).

### Core capabilities

- **Dual extraction backend**: `auto` (default — uses clang when libclang is installed, falls back to tree-sitter), `clang` (force clang, enables cgdb semantic layer; libclang 17+), `tree-sitter` (force tree-sitter, no libclang dep). Selected via `--extraction-backend` flag at scan time. **libclang is recommended, NOT required** — tree-sitter-only mode is fully functional for every supported language.
- **cgdb semantic tables** populated when clang backend is enabled: L1 AST nodes, L2 types, L3.5 config predicates (`#ifdef CONFIG_*`), L4 CFG (basic blocks + edges), L5 data flow (def-use chains), L6 alias (stub), L7 ops_bindings (typed vtable dispatch via FieldDecl → FunctionDecl) + invoke_sites, L8 sync_primitives + happens_before, L10 provenance + time-travel versions.
- **222 CLI commands** organized into 3 sub-skills: `/Code2Database` (core, 24 Tier-1 high-weight commands), `/Code2Database-analysis` (deep semantic analysis, 13 Tier-1 + 19 `cgdb_*` MCP tools), `/Code2Database-ops` (graph editing + ops, 23 Tier-1).
- **53 MCP tools** (34 `code2database_*` + 19 `cgdb_*`) exposed over stdio transport via `serve` command.
- **Multi-language scanning** for C, C++, Go, Python, Java, Rust, ASM (regex-based for NASM x86_64 / kernel GNU as / ARM bl/blr).

### Analysis & Reasoning

- **Commit-based provenance**: every node/edge carries `commit_meta` with `source_commit` (git/svn hash). Engineers verify with `git show <hash>`, not timestamps. Commands: `describe-commit`, `node-history`, `graph-provenance`, `blame-node`, `find-commits`.
- **Cypher-subset query language**: `query` command with `MATCH`/`WHERE`/`RETURN` syntax.
- **Value flow & DATA_FLOW edges**: `value-flow` command traces parameter→return-value propagation across functions.
- **Lock-held region analysis**: `lock-coverage` command with event-stream + character positions.
- **Z3 SMT path feasibility**: `path-feasible` command auto-solves path feasibility with Z3; heuristic fallback when Z3 unavailable.
- **Cross-function data dependency**: `data-dep` command + DATA_DEP edge type.
- **Invariant extraction**: `extract-invariants`, `find-invariants`, `apply-invariants` (preconditions, postconditions, loop_invariants, state_machine).
- **LLM auto-semantic enhancement**: `auto-enhance`, `batch-confirm`, `rollback`, `fill-request` (confidence-threshold auto-write with non-destructive undo).

### Reliability & Operations

- **Transactional updates**: `tx-begin`/`tx-commit`/`tx-rollback`/`tx-status`/`tx-snapshot`/`tx-restore`/`tx-list-snapshots`/`tx-replay-wal`. WAL + snapshots + fcntl file-based read-write lock. Crash recovery via `recover_unfinished_wal`.
- **Cross-language FFI**: `ffi-detect`, `ffi-list`, `ffi-trace`, `ffi-types` (Python ctypes / Go cgo / Rust `extern "C"`).
- **Interactive Web UI**: `web-ui` launches a local HTTP server with single-file HTML/SVG/JS (pan/zoom, click-to-focus, path highlighting, community LOD, FFI coloring, search).
- **BUG benchmark**: `bug-benchmark` runs GraphInvestigator (code graph queries) vs GrepInvestigator (rg/grep + file reads). Measures recall, precision, avg_tool_calls, avg_tokens, avg_time.
- **Background daemon**: `daemon-start`/`stop`/`status`/`force-refresh`/`pause`/`resume`/`wait-sync`/`logs`/`reload`/`list-projects`. inotify-based file monitoring with polling fallback, debounce (500ms), batch window (1000ms), circuit breaker (>1000 events/min triggers bulk rebuild), transactional sync, auto output file rebuild, Unix socket API at `/tmp/code2database-daemon-<project>.sock`.

### Profile & Documentation

- **Profile health + auto-evolution**: `profile-health`, `profile-evolve`, `profile-bind-version`. 0-100 score across 7 categories (callback_patterns, skip_names, vtable_types, api_prefixes, domain_keywords, macro_definitions, profile_version). Auto-evolution detects new callback register functions; EXTRACTED-confidence suggestions auto-applied, INFERRED require review. Binds profile to git/svn HEAD commit so stale profiles are detectable.
- **Doc-code dual-source truth alignment**: `doc-code-check`, `doc-mark-stale`, `doc-alignment-report`, `doc-signature-diff`. Detects return-value, param-name, signature-change, and stale-doc mismatches between `semantic_desc` (from docs) and `body_text` (from code). `describe-node` surfaces `doc_code_mismatches`; `knowledge-validate` includes doc-code alignment.

### Time-travel & Incremental Sync

- **Time-travel version queries**: `cgdb_time_travel_query` and `cgdb_list_versions` enable querying the graph at past versions (`first_seen_version` / `last_seen_version` per node/edge).
- **Incremental sync with content hash**: daemon uses SHA-256 content hash for change detection; `compute_affected_tus` walks transitive `#include` closure to expand changed files.
- **Enclosing symbol tracking**: `enclosing_symbol_id` column on `cgdb_nodes` and `cgdb_edges` for nested-scope queries.

### Distribution

- **3-sub-skill split**: `Code2Database` (core, always loaded), `Code2Database-analysis` (on-demand deep analysis), `Code2Database-ops` (on-demand ops). Each sub-skill has its own `SKILL.md` exposing only the commands relevant to its layer. The CLI (`scripts/code2database_builder.py`) is shared — all 222 commands are accessible regardless of which sub-skill is active.
- **Bilingual documentation**: English (`docs/en/`) and Chinese (`docs/zh/`).
- **One-click installer** (`install.sh`) with Claude Code and Codex CLI support.
- **Partial language install**: `scripts/setup.sh --languages c,go` (or `C2D_LANGUAGES` env var) — engineers focused on a single language can install only the tree-sitter grammars they need.
- **MCP server registry** (`server.json`) for discoverability.
- **`CLAUDE.md`** for Claude Code integration; **`AGENTS.md`** for Codex / agent integration.

### Test coverage

- 1066+ tests covering scanner, builder, query, transactions, daemon, FFI, invariants, profile health, doc-code alignment, cgdb layer (CFG, data flow, ops bindings, sync primitives, happens-before, versions), MCP tools, e2e pipelines.
