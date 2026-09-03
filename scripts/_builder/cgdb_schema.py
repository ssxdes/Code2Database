"""cgdb (code graph database) SQLite schema.

Adds 13-layer semantic tables to the existing code2database.db (side-by-side with
the legacy `functions`/`edges` tables for backward compatibility).

CGDB-Layers (legacy cgdb naming — distinct from design-report L1~L4; see OVERVIEW.md):
  CGDB-L0   meta / graph_versions                  — version control + metadata
  CGDB-L1   nodes / files                           — multi-kind first-class nodes
  CGDB-L2   types                                   — independent type system
  CGDB-L3   conditions                              — Z3-reasonable boolean expression trees
  CGDB-L3.5 config_predicates                       — #ifdef predicate tree (BDD + Z3 form)
  CGDB-L4   basic_blocks / cfg_edges                — control flow graph
  CGDB-L5   data_flow / alias_sets                  — def-use chain + pointer alias
  CGDB-L7   invoke_sites / ops_bindings             — invocation graph refinement + typed vtable dispatch
  CGDB-L8   sync_primitives / happens_before        — concurrency + memory model
  CGDB-L9   cgdb_includes                          — #include dependency graph
  CGDB-L10  doc_comments + graph_versions           — comments + time-travel
  CGDB-L11  node_metadata / edge_metadata           — typed-key metadata
  Full-text: nodes_fts (FTS5 virtual table)

Design-report layers (per report/C代码数据库化方案-分析与执行报告.md) — distinct from cgdb layers:
  Report-L1  无损重建层: tokens / macros / macro_invocations / pp_branches /
              pp_directives / pragmas / attributes / literals / string_literals /
              comments (+ source_files_meta extension to cgdb_files)
  Report-L2  AST 层:    symbols / ast_nodes / references / call_edges / includes /
              globals / types / modules / git_meta (partially covered by cgdb_nodes
              + cgdb_edges + cgdb_types + cgdb_includes)
  Report-L3  IR 层:     ir_functions / cfg_blocks / cfg_edges / ssa_values /
              mem_accesses / alias_sets / points_to / indirect_calls / data_deps /
              path_states (new in v4 — see Report-L3 section below)
  Report-L4  派生层:    call_graph_reachability / module_deps / function_embeddings /
              precise_write_sets / arch_metrics / history_snapshots / alignment_errors
              (new in v4 — see Report-L4 section below)
  Report-多库: db_routing / precompute_tasks (new in v4 — see Multi-DB routing section)
  Report-跨语言: cross_lang_bindings / type_mappings / ffi_call_sites /
              language_adapters / runtime_observations / dependencies
              (new in v4 — see Cross-language bridge section)

`apply_cgdb_schema(conn)` is idempotent — safe to call on existing databases.
Schema evolution is handled by `cgdb_migrations.run_migrations` — each version
bump has a migration function that ALTERs existing tables in-place, preserving
data.
"""
import sqlite3

CGDB_SCHEMA_VERSION = 5


def apply_cgdb_schema(conn: sqlite3.Connection) -> None:
    """Create all cgdb tables, indexes, triggers on the given connection.

    Idempotent: uses CREATE TABLE IF NOT EXISTS. Safe to call on databases
    that already have legacy `functions`/`edges` tables — cgdb tables are
    additive and do not conflict.

    Schema migration: if the existing db has an older `cgdb_schema_version`
    in `meta`, runs all migrations up to CGDB_SCHEMA_VERSION. New databases
    start at the current version (no migrations needed).
    """
    # Ensure meta table exists (it's created by sqlite_store, but cgdb_schema
    # may be called standalone during tests).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Check existing schema version
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", ("cgdb_schema_version",)
    ).fetchone()
    current_version = int(row[0]) if row else 0

    if current_version == 0:
        # Fresh database — apply full DDL (v3 base + v4 additions) at current version
        conn.executescript(_CGDB_DDL)
        conn.executescript(_CGDB_DDL_V4)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("cgdb_schema_version", str(CGDB_SCHEMA_VERSION))
        )
    elif current_version < CGDB_SCHEMA_VERSION:
        # Existing database at older version — run migrations
        from _builder.cgdb_migrations import run_migrations
        new_version = run_migrations(conn, current_version, CGDB_SCHEMA_VERSION)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("cgdb_schema_version", str(new_version))
        )
    # else: already at current version, nothing to do

    conn.commit()


# ============================================================================
# DDL: all cgdb tables, indexes, triggers in one script
# ============================================================================
_CGDB_DDL = """
-- ============================================================================
-- L0: graph_versions — per-commit snapshot for time-travel queries
-- ============================================================================
CREATE TABLE IF NOT EXISTS graph_versions (
  version_id INTEGER PRIMARY KEY AUTOINCREMENT,
  commit_hash TEXT NOT NULL,
  commit_short TEXT,
  commit_subject TEXT,
  compiled_at INTEGER NOT NULL,
  parent_version_id INTEGER,
  diff_summary TEXT,
  FOREIGN KEY (parent_version_id) REFERENCES graph_versions(version_id)
);
CREATE INDEX IF NOT EXISTS idx_cgdb_versions_commit ON graph_versions(commit_hash);

-- ============================================================================
-- L1: files — separate file table (solves ASTDump JSON no-file-field issue)
-- ============================================================================
CREATE TABLE IF NOT EXISTS cgdb_files (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  is_system INTEGER NOT NULL DEFAULT 0,
  language TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  line_count INTEGER,
  byte_count INTEGER,
  commit_hash TEXT NOT NULL DEFAULT 'unknown',
  last_modified INTEGER,
  content_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_cgdb_files_path ON cgdb_files(path);
CREATE INDEX IF NOT EXISTS idx_cgdb_files_hash ON cgdb_files(content_hash);

-- ============================================================================
-- L1: nodes — multi-kind first-class nodes (replaces single-type functions)
-- ============================================================================
CREATE TABLE IF NOT EXISTS cgdb_nodes (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN (
    'function', 'method', 'constructor', 'destructor',
    'var', 'parm', 'field', 'enum_constant', 'typedef',
    'struct', 'class', 'union', 'enum',
    'stmt', 'expr', 'decl_ref', 'member_ref',
    'label', 'namespace', 'template', 'concept',
    'file', 'macro', 'include',
    'vtable', 'ops_table'
  )),
  name TEXT NOT NULL,
  fqn TEXT NOT NULL,
  file_id INTEGER,
  line INTEGER NOT NULL DEFAULT 0,
  col INTEGER NOT NULL DEFAULT 0,
  byte_start INTEGER NOT NULL DEFAULT 0,
  byte_end INTEGER NOT NULL DEFAULT 0,
  type_spelling TEXT,
  type_id INTEGER,
  config_predicate_id INTEGER,
  -- Per cgdb-architecture doc 5.4.2: enclosing FunctionDecl's node_id,
  -- derived via cursor.semantic_parent walk. 0/NULL = file/TU scope.
  enclosing_symbol_id INTEGER,
  -- Denormalized FTS columns (extracted from attrs for fast full-text search).
  -- These mirror the values in attrs.signature / attrs.body_text but as
  -- real columns so FTS5 external-content mode can index them directly.
  signature TEXT,
  body_text TEXT,
  attrs TEXT NOT NULL DEFAULT '{}',
  source_layer TEXT NOT NULL DEFAULT 'ast' CHECK (source_layer IN ('ast','cfg','analysis','llm')),
  confidence REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
  first_seen_version INTEGER NOT NULL DEFAULT 1,
  last_seen_version INTEGER NOT NULL DEFAULT 1,
  commit_hash TEXT NOT NULL DEFAULT 'unknown',
  -- v2: source snippet for evidence traceability (read source text without
  -- going back to file). Populated at scan time from byte_start..byte_end.
  source_snippet TEXT,
  -- v2: LLM/heuristic-enhanced description (separate from doc_comments
  -- which captures raw comment text). Empty by default.
  description TEXT NOT NULL DEFAULT '',
  llm_confidence REAL NOT NULL DEFAULT 0.0,
  -- Link to legacy functions table for backward compat (when kind='function')
  legacy_function_id TEXT,
  FOREIGN KEY (file_id) REFERENCES cgdb_files(id),
  FOREIGN KEY (type_id) REFERENCES cgdb_types(id),
  FOREIGN KEY (config_predicate_id) REFERENCES config_predicates(id),
  FOREIGN KEY (first_seen_version) REFERENCES graph_versions(version_id),
  FOREIGN KEY (last_seen_version) REFERENCES graph_versions(version_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cgdb_nodes_unique ON cgdb_nodes(fqn, file_id, byte_start);
CREATE INDEX IF NOT EXISTS idx_cgdb_nodes_kind ON cgdb_nodes(kind);
CREATE INDEX IF NOT EXISTS idx_cgdb_nodes_name ON cgdb_nodes(name);
CREATE INDEX IF NOT EXISTS idx_cgdb_nodes_fqn ON cgdb_nodes(fqn);
CREATE INDEX IF NOT EXISTS idx_cgdb_nodes_file_line ON cgdb_nodes(file_id, line);
CREATE INDEX IF NOT EXISTS idx_cgdb_nodes_kind_file ON cgdb_nodes(kind, file_id);
CREATE INDEX IF NOT EXISTS idx_cgdb_nodes_enclosing ON cgdb_nodes(enclosing_symbol_id);
CREATE INDEX IF NOT EXISTS idx_cgdb_nodes_pred ON cgdb_nodes(config_predicate_id);
CREATE INDEX IF NOT EXISTS idx_cgdb_nodes_legacy ON cgdb_nodes(legacy_function_id);
CREATE INDEX IF NOT EXISTS idx_cgdb_nodes_description ON cgdb_nodes(description) WHERE description != '';

-- ============================================================================
-- L2: types — independent type system
-- ============================================================================
CREATE TABLE IF NOT EXISTS cgdb_types (
  id INTEGER PRIMARY KEY,
  spelling TEXT NOT NULL,
  canonical_spelling TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN (
    'builtin', 'pointer', 'reference', 'array',
    'record', 'enum', 'function', 'template', 'typedef'
  )),
  size_bytes INTEGER,
  alignment INTEGER,
  is_const INTEGER DEFAULT 0,
  is_volatile INTEGER DEFAULT 0,
  pointee_type_id INTEGER,
  element_type_id INTEGER,
  record_id INTEGER,
  attrs TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_cgdb_types_spelling ON cgdb_types(spelling);
CREATE INDEX IF NOT EXISTS idx_cgdb_types_canonical ON cgdb_types(canonical_spelling);
CREATE INDEX IF NOT EXISTS idx_cgdb_types_pointee ON cgdb_types(pointee_type_id);

-- ============================================================================
-- L3: conditions — Z3-reasonable boolean expression trees (for CFG branches)
-- ============================================================================
CREATE TABLE IF NOT EXISTS conditions (
  id INTEGER PRIMARY KEY,
  root_expr_id INTEGER,
  kind TEXT NOT NULL CHECK (kind IN ('comparison','logical','unary','atom','macro_call')),
  operator TEXT,
  left_expr_id INTEGER,
  right_expr_id INTEGER,
  text_form TEXT,
  z3_form TEXT,
  attrs TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_conditions_root ON conditions(root_expr_id);
CREATE INDEX IF NOT EXISTS idx_conditions_text ON conditions(text_form);

-- ============================================================================
-- L3.5: config_predicates — #ifdef predicate tree (per cdb 5.2)
-- ============================================================================
CREATE TABLE IF NOT EXISTS config_predicates (
  id INTEGER PRIMARY KEY,
  root_expr_id INTEGER,
  text_form TEXT NOT NULL,
  z3_form TEXT NOT NULL DEFAULT '',
  bdd_serialized TEXT NOT NULL DEFAULT '',
  config_macros TEXT NOT NULL DEFAULT '[]',
  is_unconditional INTEGER DEFAULT 0,
  is_contradictory INTEGER DEFAULT 0,
  FOREIGN KEY (root_expr_id) REFERENCES cgdb_nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_pred_macros ON config_predicates(config_macros);
CREATE INDEX IF NOT EXISTS idx_pred_text ON config_predicates(text_form);
CREATE INDEX IF NOT EXISTS idx_pred_bdd ON config_predicates(bdd_serialized);
CREATE INDEX IF NOT EXISTS idx_pred_unconditional ON config_predicates(is_unconditional);

-- ============================================================================
-- L1: cgdb_edges — semantic edges (complements legacy edges table)
-- ============================================================================
CREATE TABLE IF NOT EXISTS cgdb_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  src_id INTEGER NOT NULL,
  dst_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN (
    'INVOKES', 'OPS_BIND', 'READS', 'WRITES',
    'ALLOCATES', 'FREES', 'LOCKS', 'UNLOCKS',
    'NEXT', 'BRANCHES',
    'HAS_FIELD', 'HAS_PARAM', 'HAS_LOCAL',
    'RETURNS', 'DECLARES', 'REFERENCES',
    'OVERRIDES', 'IMPLEMENTS', 'INSTANTIATES',
    'THROWS', 'IMPORTS', 'MACRO_EXPANDS_TO',
    'FFI_BINDS', 'FFI_INVOKES'
  )),
  file_id INTEGER,
  line INTEGER,
  col INTEGER,
  byte_start INTEGER,
  byte_end INTEGER,
  condition_id INTEGER,
  config_predicate_id INTEGER,
  -- Per cgdb-architecture doc 5.4.2: enclosing FunctionDecl's node_id.
  enclosing_symbol_id INTEGER,
  attrs TEXT NOT NULL DEFAULT '{}',
  source_layer TEXT NOT NULL DEFAULT 'ast' CHECK (source_layer IN ('ast','cfg','analysis','llm')),
  confidence REAL NOT NULL DEFAULT 1.0,
  first_seen_version INTEGER NOT NULL DEFAULT 1,
  last_seen_version INTEGER NOT NULL DEFAULT 1,
  commit_hash TEXT NOT NULL DEFAULT 'unknown',
  legacy_edge_id INTEGER,
  source_snippet TEXT,
  FOREIGN KEY (src_id) REFERENCES cgdb_nodes(id),
  FOREIGN KEY (dst_id) REFERENCES cgdb_nodes(id),
  FOREIGN KEY (file_id) REFERENCES cgdb_files(id),
  FOREIGN KEY (condition_id) REFERENCES conditions(id),
  FOREIGN KEY (config_predicate_id) REFERENCES config_predicates(id)
);
CREATE INDEX IF NOT EXISTS idx_cgdb_edges_src_kind ON cgdb_edges(src_id, kind);
CREATE INDEX IF NOT EXISTS idx_cgdb_edges_dst_kind ON cgdb_edges(dst_id, kind);
CREATE INDEX IF NOT EXISTS idx_cgdb_edges_kind ON cgdb_edges(kind);
CREATE INDEX IF NOT EXISTS idx_cgdb_edges_src_dst ON cgdb_edges(src_id, dst_id);
CREATE INDEX IF NOT EXISTS idx_cgdb_edges_file_line ON cgdb_edges(file_id, line);
CREATE INDEX IF NOT EXISTS idx_cgdb_edges_pred ON cgdb_edges(config_predicate_id);
CREATE INDEX IF NOT EXISTS idx_cgdb_edges_enclosing ON cgdb_edges(enclosing_symbol_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cgdb_edges_unique ON cgdb_edges(src_id, dst_id, kind, file_id, line, col);

-- ============================================================================
-- L4: basic_blocks + cfg_edges — control flow graph
-- ============================================================================
CREATE TABLE IF NOT EXISTS basic_blocks (
  id INTEGER PRIMARY KEY,
  function_id INTEGER NOT NULL,
  block_index INTEGER NOT NULL,
  is_entry INTEGER DEFAULT 0,
  is_exit INTEGER DEFAULT 0,
  stmt_ids TEXT NOT NULL DEFAULT '[]',
  byte_start INTEGER,
  byte_end INTEGER,
  FOREIGN KEY (function_id) REFERENCES cgdb_nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_blocks_function ON basic_blocks(function_id, block_index);

CREATE TABLE IF NOT EXISTS cfg_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  function_id INTEGER NOT NULL,
  src_block_id INTEGER NOT NULL,
  dst_block_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('fallthrough','true_branch','false_branch','exception')),
  condition_id INTEGER,
  FOREIGN KEY (function_id) REFERENCES cgdb_nodes(id),
  FOREIGN KEY (src_block_id) REFERENCES basic_blocks(id),
  FOREIGN KEY (dst_block_id) REFERENCES basic_blocks(id),
  FOREIGN KEY (condition_id) REFERENCES conditions(id)
);
CREATE INDEX IF NOT EXISTS idx_cfg_edges_src ON cfg_edges(src_block_id);
CREATE INDEX IF NOT EXISTS idx_cfg_edges_dst ON cfg_edges(dst_block_id);
CREATE INDEX IF NOT EXISTS idx_cfg_edges_function ON cfg_edges(function_id);

-- ============================================================================
-- L5: data_flow + alias_sets — def-use chain + pointer alias
-- ============================================================================
CREATE TABLE IF NOT EXISTS data_flow (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  function_id INTEGER NOT NULL,
  var_id INTEGER NOT NULL,
  def_block_id INTEGER,
  def_stmt_id INTEGER,
  use_block_id INTEGER,
  use_stmt_id INTEGER,
  kind TEXT NOT NULL CHECK (kind IN ('def','use','def_use','may_def','may_use')),
  path_condition_ids TEXT NOT NULL DEFAULT '[]',
  FOREIGN KEY (function_id) REFERENCES cgdb_nodes(id),
  FOREIGN KEY (var_id) REFERENCES cgdb_nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_dataflow_var ON data_flow(var_id);
CREATE INDEX IF NOT EXISTS idx_dataflow_def ON data_flow(def_stmt_id);
CREATE INDEX IF NOT EXISTS idx_dataflow_use ON data_flow(use_stmt_id);
CREATE INDEX IF NOT EXISTS idx_dataflow_function ON data_flow(function_id);

CREATE TABLE IF NOT EXISTS alias_sets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ptr1_node_id INTEGER NOT NULL,
  ptr2_node_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('must_alias','may_alias','no_alias')),
  confidence REAL NOT NULL DEFAULT 1.0,
  FOREIGN KEY (ptr1_node_id) REFERENCES cgdb_nodes(id),
  FOREIGN KEY (ptr2_node_id) REFERENCES cgdb_nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_alias_ptr1 ON alias_sets(ptr1_node_id);
CREATE INDEX IF NOT EXISTS idx_alias_ptr2 ON alias_sets(ptr2_node_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_alias_pair ON alias_sets(ptr1_node_id, ptr2_node_id, kind);

-- ============================================================================
-- L7: invoke_sites + ops_bindings — invocation graph refinement
-- ============================================================================
CREATE TABLE IF NOT EXISTS invoke_sites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  invoker_id INTEGER NOT NULL,
  invoked_id INTEGER NOT NULL,
  invoke_expr_id INTEGER,
  arg_bindings TEXT NOT NULL DEFAULT '[]',
  invoke_kind TEXT NOT NULL CHECK (invoke_kind IN (
    'direct', 'ops_bind', 'virtual', 'function_pointer', 'template_instantiation'
  )),
  dispatch_candidates TEXT NOT NULL DEFAULT '[]',
  FOREIGN KEY (invoker_id) REFERENCES cgdb_nodes(id),
  FOREIGN KEY (invoked_id) REFERENCES cgdb_nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_invokesites_invoker ON invoke_sites(invoker_id);
CREATE INDEX IF NOT EXISTS idx_invokesites_invoked ON invoke_sites(invoked_id);
CREATE INDEX IF NOT EXISTS idx_invokesites_kind ON invoke_sites(invoke_kind);

CREATE TABLE IF NOT EXISTS ops_bindings (
  edge_id INTEGER PRIMARY KEY REFERENCES cgdb_edges(id),
  ops_table_id INTEGER NOT NULL,
  field_node_id INTEGER NOT NULL,
  impl_function_id INTEGER NOT NULL,
  signature_match INTEGER DEFAULT 1,
  FOREIGN KEY (ops_table_id) REFERENCES cgdb_nodes(id),
  FOREIGN KEY (field_node_id) REFERENCES cgdb_nodes(id),
  FOREIGN KEY (impl_function_id) REFERENCES cgdb_nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_opsbind_field ON ops_bindings(field_node_id);
CREATE INDEX IF NOT EXISTS idx_opsbind_impl ON ops_bindings(impl_function_id);
CREATE INDEX IF NOT EXISTS idx_opsbind_table ON ops_bindings(ops_table_id);

-- ============================================================================
-- L8: sync_primitives + happens_before — concurrency + memory model
-- ============================================================================
CREATE TABLE IF NOT EXISTS sync_primitives (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  function_id INTEGER NOT NULL,
  sync_var_id INTEGER,
  kind TEXT NOT NULL CHECK (kind IN (
    'lock_acquire', 'lock_release',
    'rcu_read_lock', 'rcu_read_unlock',
    'atomic_load', 'atomic_store',
    'memory_barrier', 'read_once', 'write_once'
  )),
  acquire_stmt_id INTEGER,
  release_stmt_id INTEGER,
  memory_order TEXT,
  FOREIGN KEY (function_id) REFERENCES cgdb_nodes(id),
  FOREIGN KEY (sync_var_id) REFERENCES cgdb_nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_sync_function ON sync_primitives(function_id);
CREATE INDEX IF NOT EXISTS idx_sync_var ON sync_primitives(sync_var_id);

CREATE TABLE IF NOT EXISTS happens_before (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  write_event_id INTEGER NOT NULL,
  read_event_id INTEGER NOT NULL,
  reason TEXT NOT NULL CHECK (reason IN ('lock','rcu','atomic','memory_barrier','program_order','unbalanced_release','barrier','volatile')),
  confidence REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_happens_before_write ON happens_before(write_event_id);
CREATE INDEX IF NOT EXISTS idx_happens_before_read ON happens_before(read_event_id);

-- ============================================================================
-- L9: includes — #include dependency graph for incremental sync
-- ============================================================================
CREATE TABLE IF NOT EXISTS cgdb_includes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_file_id INTEGER NOT NULL,
  included_file_id INTEGER,
  included_path TEXT NOT NULL,
  is_system INTEGER DEFAULT 0,
  FOREIGN KEY (source_file_id) REFERENCES cgdb_files(id)
);
CREATE INDEX IF NOT EXISTS idx_includes_source ON cgdb_includes(source_file_id);
CREATE INDEX IF NOT EXISTS idx_includes_target ON cgdb_includes(included_file_id);
CREATE INDEX IF NOT EXISTS idx_includes_path ON cgdb_includes(included_path);

-- ============================================================================
-- L10: doc_comments — doc-comment extraction per node
-- ============================================================================

CREATE TABLE IF NOT EXISTS doc_comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id INTEGER NOT NULL,
  file_id INTEGER,
  line INTEGER DEFAULT 0,
  col INTEGER DEFAULT 0,
  comment_kind TEXT NOT NULL DEFAULT '',
  raw_text TEXT NOT NULL DEFAULT '',
  cleaned_text TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '{}',
  byte_start INTEGER DEFAULT 0,
  byte_end INTEGER DEFAULT 0,
  FOREIGN KEY (node_id) REFERENCES cgdb_nodes(id),
  FOREIGN KEY (file_id) REFERENCES cgdb_files(id)
);
CREATE INDEX IF NOT EXISTS idx_doc_comments_node ON doc_comments(node_id);
CREATE INDEX IF NOT EXISTS idx_doc_comments_file ON doc_comments(file_id);
CREATE INDEX IF NOT EXISTS idx_doc_comments_kind ON doc_comments(comment_kind);

-- ============================================================================
-- L11: node_metadata / edge_metadata — typed-key metadata per target
-- ============================================================================

CREATE TABLE IF NOT EXISTS node_metadata (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id INTEGER NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL DEFAULT '',
  value_type TEXT NOT NULL DEFAULT 'str',
  source TEXT NOT NULL DEFAULT 'scanner',
  FOREIGN KEY (node_id) REFERENCES cgdb_nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_node_metadata_node ON node_metadata(node_id);
CREATE INDEX IF NOT EXISTS idx_node_metadata_key ON node_metadata(key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_node_metadata_unique
  ON node_metadata(node_id, key);

CREATE TABLE IF NOT EXISTS edge_metadata (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  edge_id INTEGER NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL DEFAULT '',
  value_type TEXT NOT NULL DEFAULT 'str',
  source TEXT NOT NULL DEFAULT 'scanner',
  FOREIGN KEY (edge_id) REFERENCES cgdb_edges(id)
);
CREATE INDEX IF NOT EXISTS idx_edge_metadata_edge ON edge_metadata(edge_id);
CREATE INDEX IF NOT EXISTS idx_edge_metadata_key ON edge_metadata(key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_edge_metadata_unique
  ON edge_metadata(edge_id, key);

-- ============================================================================
-- Full-text search: nodes_fts (FTS5 virtual table, external-content mode)
-- ============================================================================
-- External-content FTS5 over cgdb_nodes. The `signature` and `body_text`
-- columns on cgdb_nodes are denormalized from attrs JSON for FTS5 access.
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
  name, fqn, signature, body_text,
  content='cgdb_nodes', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS cgdb_nodes_ai AFTER INSERT ON cgdb_nodes BEGIN
  INSERT INTO nodes_fts(rowid, name, fqn, signature, body_text)
  VALUES (new.id, new.name, new.fqn, new.signature, new.body_text);
END;
CREATE TRIGGER IF NOT EXISTS cgdb_nodes_ad AFTER DELETE ON cgdb_nodes BEGIN
  INSERT INTO nodes_fts(nodes_fts, rowid, name, fqn, signature, body_text)
  VALUES ('delete', old.id, old.name, old.fqn, old.signature, old.body_text);
END;
CREATE TRIGGER IF NOT EXISTS cgdb_nodes_au AFTER UPDATE ON cgdb_nodes BEGIN
  INSERT INTO nodes_fts(nodes_fts, rowid, name, fqn, signature, body_text)
  VALUES ('delete', old.id, old.name, old.fqn, old.signature, old.body_text);
  INSERT INTO nodes_fts(rowid, name, fqn, signature, body_text)
  VALUES (new.id, new.name, new.fqn, new.signature, new.body_text);
END;

-- ============================================================================
-- View: cdb_nodes — convenience view over cgdb_nodes only.
-- (Legacy functions table is merged in Python by CGDBReader to avoid
-- hard dependency on functions table being present.)
-- ============================================================================
CREATE VIEW IF NOT EXISTS cdb_nodes AS
  SELECT
    n.id AS node_id,
    n.kind,
    n.name,
    n.fqn,
    n.line,
    n.col,
    n.byte_start,
    n.byte_end,
    n.type_spelling,
    n.config_predicate_id,
    n.source_layer,
    n.confidence,
    n.commit_hash,
    n.legacy_function_id
  FROM cgdb_nodes n;
"""

# ============================================================================
# DDL v4 additions — design-report L1/L3/L4 + multi-db + cross-language bridge
# These tables implement the design report (C代码数据库化方案-分析与执行报告.md)
# appendix C.1 (L1), C.3 (L3), C.4 (L4), C.5 (multi-db routing), C.6 (cross-lang).
# They are additive — they do NOT replace the legacy cgdb tables above; they sit
# side-by-side in the same db. Kept as a separate DDL string so the v3→v4
# migration can run ONLY this part without re-touching v3 tables/indexes.
# ============================================================================
_CGDB_DDL_V4 = """
-- ============================================================================
-- ============================================================================
-- ==                                                                        ==
-- ==  Schema v4 additions — design-report L1/L3/L4 + multi-db + cross-lang ==
-- ==  These tables implement the design report (C代码数据库化方案-分析与执行报告.md) ==
-- ==  appendix C.1 (L1), C.3 (L3), C.4 (L4), C.5 (multi-db routing),       ==
-- ==  C.6 (cross-language bridge). They are additive — they do NOT replace  ==
-- ==  the legacy cgdb tables above; they sit side-by-side in the same db.   ==
-- ==                                                                        ==
-- ============================================================================
-- ============================================================================

-- ============================================================================
-- Report-L1: source_files_meta — extension columns on cgdb_files for L1
--无损重建层. Adds encoding / line_ending / has_bom / byte_length / loc / mtime
-- to cgdb_files via a side table (cgdb_files already has path/language/sha256/
-- line_count/byte_count/commit_hash/last_modified/content_hash).
-- Kept as a side table to avoid ALTER TABLE on the existing cgdb_files schema.
-- ============================================================================
CREATE TABLE IF NOT EXISTS source_files_meta (
  file_id INTEGER PRIMARY KEY REFERENCES cgdb_files(id) ON DELETE CASCADE,
  encoding TEXT NOT NULL DEFAULT 'utf-8',
  line_ending TEXT NOT NULL DEFAULT 'LF',
  has_bom INTEGER NOT NULL DEFAULT 0,
  mtime_ns INTEGER NOT NULL DEFAULT 0,
  trailing_whitespace TEXT,
  -- sha256 of the original disk file (for char-level consistency check)
  -- Distinct from cgdb_files.content_hash which is for incremental sync.
  disk_sha256 TEXT NOT NULL DEFAULT '',
  -- sha256 of the rendered source from DB tokens (for verify_consistency)
  rendered_sha256 TEXT,
  last_verified_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_source_files_meta_sha ON source_files_meta(disk_sha256);

-- ============================================================================
-- Report-L1: tokens — full token stream for character-level reconstruction
-- Each token carries its preceding_whitespace so DB→file render is byte-exact.
-- ============================================================================
CREATE TABLE IF NOT EXISTS tokens (
  id INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES cgdb_files(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN (
    'keyword','identifier','literal','punct','comment','whitespace',
    'string_literal','int_literal','float_literal','char_literal'
  )),
  spelling TEXT NOT NULL,
  line INTEGER NOT NULL,
  col INTEGER NOT NULL,
  end_line INTEGER NOT NULL DEFAULT 0,
  end_col INTEGER NOT NULL DEFAULT 0,
  byte_offset INTEGER NOT NULL DEFAULT 0,
  byte_length INTEGER NOT NULL DEFAULT 0,
  preceding_whitespace TEXT NOT NULL DEFAULT '',
  -- Cross-references (filled per-kind)
  symbol_id INTEGER,
  literal_id INTEGER,
  comment_id INTEGER,
  macro_id INTEGER,
  macro_invocation_id INTEGER,
  pp_branch_id INTEGER,
  pp_directive_id INTEGER,
  pragma_id INTEGER,
  attribute_id INTEGER,
  ast_node_id INTEGER,
  language TEXT NOT NULL DEFAULT 'c',
  UNIQUE(file_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_tokens_file_line ON tokens(file_id, line);
CREATE INDEX IF NOT EXISTS idx_tokens_seq ON tokens(file_id, seq);
CREATE INDEX IF NOT EXISTS idx_tokens_symbol ON tokens(symbol_id);
CREATE INDEX IF NOT EXISTS idx_tokens_ast_node ON tokens(ast_node_id);
CREATE INDEX IF NOT EXISTS idx_tokens_kind ON tokens(kind);
CREATE INDEX IF NOT EXISTS idx_tokens_byte ON tokens(file_id, byte_offset);

-- ============================================================================
-- Report-L1: macros — macro definitions (function-like / variadic / params)
-- ============================================================================
CREATE TABLE IF NOT EXISTS macros (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  file_id INTEGER NOT NULL REFERENCES cgdb_files(id) ON DELETE CASCADE,
  line INTEGER NOT NULL DEFAULT 0,
  col INTEGER NOT NULL DEFAULT 0,
  is_function_like INTEGER NOT NULL DEFAULT 0,
  is_variadic INTEGER NOT NULL DEFAULT 0,
  params TEXT NOT NULL DEFAULT '[]',
  body_token_ids TEXT NOT NULL DEFAULT '[]',
  body_text TEXT NOT NULL DEFAULT '',
  is_undef INTEGER NOT NULL DEFAULT 0,
  defined_at_token_id INTEGER REFERENCES tokens(id),
  language TEXT NOT NULL DEFAULT 'c'
);
CREATE INDEX IF NOT EXISTS idx_macros_name ON macros(name);
CREATE INDEX IF NOT EXISTS idx_macros_file ON macros(file_id, line);

-- ============================================================================
-- Report-L1: macro_invocations — macro expansion sites
-- ============================================================================
CREATE TABLE IF NOT EXISTS macro_invocations (
  id INTEGER PRIMARY KEY,
  macro_id INTEGER NOT NULL REFERENCES macros(id) ON DELETE CASCADE,
  file_id INTEGER NOT NULL REFERENCES cgdb_files(id) ON DELETE CASCADE,
  line INTEGER NOT NULL DEFAULT 0,
  col INTEGER NOT NULL DEFAULT 0,
  arg_token_ids TEXT NOT NULL DEFAULT '[]',
  expanded_text TEXT,
  at_token_id INTEGER REFERENCES tokens(id)
);
CREATE INDEX IF NOT EXISTS idx_macroinv_macro ON macro_invocations(macro_id);
CREATE INDEX IF NOT EXISTS idx_macroinv_file ON macro_invocations(file_id, line);

-- ============================================================================
-- Report-L1: pp_branches — conditional compilation branch tree
-- ============================================================================
CREATE TABLE IF NOT EXISTS pp_branches (
  id INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES cgdb_files(id) ON DELETE CASCADE,
  parent_id INTEGER REFERENCES pp_branches(id),
  kind TEXT NOT NULL CHECK (kind IN ('if','ifdef','ifndef','elif','else','endif')),
  condition TEXT,
  condition_token_ids TEXT NOT NULL DEFAULT '[]',
  start_line INTEGER NOT NULL DEFAULT 0,
  end_line INTEGER NOT NULL DEFAULT 0,
  is_active INTEGER NOT NULL DEFAULT 1,
  config_hash TEXT,
  language TEXT NOT NULL DEFAULT 'c'
);
CREATE INDEX IF NOT EXISTS idx_pp_branches_file ON pp_branches(file_id);
CREATE INDEX IF NOT EXISTS idx_pp_branches_parent ON pp_branches(parent_id);

-- ============================================================================
-- Report-L1: pp_directives — preprocessor directives (include/pragma/line/error)
-- ============================================================================
CREATE TABLE IF NOT EXISTS pp_directives (
  id INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES cgdb_files(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('include','pragma','line','error','warning')),
  line INTEGER NOT NULL DEFAULT 0,
  col INTEGER NOT NULL DEFAULT 0,
  raw_text TEXT,
  parsed_payload TEXT,
  at_token_id INTEGER REFERENCES tokens(id)
);
CREATE INDEX IF NOT EXISTS idx_pp_directives_file ON pp_directives(file_id, line);
CREATE INDEX IF NOT EXISTS idx_pp_directives_kind ON pp_directives(kind);

-- ============================================================================
-- Report-L1: pragmas — pragma directives
-- ============================================================================
CREATE TABLE IF NOT EXISTS pragmas (
  id INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES cgdb_files(id) ON DELETE CASCADE,
  line INTEGER NOT NULL DEFAULT 0,
  col INTEGER NOT NULL DEFAULT 0,
  pragma_kind TEXT,
  raw_text TEXT,
  parsed_payload TEXT,
  pp_directive_id INTEGER REFERENCES pp_directives(id),
  at_token_id INTEGER REFERENCES tokens(id)
);
CREATE INDEX IF NOT EXISTS idx_pragmas_file ON pragmas(file_id, line);
CREATE INDEX IF NOT EXISTS idx_pragmas_kind ON pragmas(pragma_kind);

-- ============================================================================
-- Report-L1: attributes — __attribute__((...)) metadata
-- ============================================================================
CREATE TABLE IF NOT EXISTS attributes (
  id INTEGER PRIMARY KEY,
  ast_node_id INTEGER NOT NULL REFERENCES cgdb_nodes(id) ON DELETE CASCADE,
  attr_kind TEXT,
  raw_text TEXT,
  parsed_payload TEXT,
  at_token_id INTEGER REFERENCES tokens(id)
);
CREATE INDEX IF NOT EXISTS idx_attributes_node ON attributes(ast_node_id);
CREATE INDEX IF NOT EXISTS idx_attributes_kind ON attributes(attr_kind);

-- ============================================================================
-- Report-L1: literals — numeric / char / string literals (parsed form).
-- String literals get a parent row here (kind='string') plus a child row in
-- string_literals with security_flags etc. The 'string' value was added to
-- the CHECK constraint after the original schema shipped — l1_ingest.py
-- inserts 'string' for the parent row, and the original constraint
-- ('int','float','char','imaginary','other') silently rejected it via the
-- bare `except Exception: continue` in the token loop, leaving the
-- string_literals table empty.
-- ============================================================================
CREATE TABLE IF NOT EXISTS literals (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('int','float','char','string','imaginary','other')),
  value TEXT,
  raw_text TEXT NOT NULL,
  base INTEGER,
  suffix TEXT,
  token_id INTEGER REFERENCES tokens(id)
);
CREATE INDEX IF NOT EXISTS idx_literals_token ON literals(token_id);
CREATE INDEX IF NOT EXISTS idx_literals_kind ON literals(kind);

-- ============================================================================
-- Report-L1: string_literals — precise byte content for security audit
-- ============================================================================
CREATE TABLE IF NOT EXISTS string_literals (
  id INTEGER PRIMARY KEY,
  literal_id INTEGER REFERENCES literals(id) ON DELETE CASCADE,
  raw_bytes BLOB,
  decoded TEXT,
  encoding TEXT,
  is_wide INTEGER NOT NULL DEFAULT 0,
  in_function_id INTEGER,
  security_flags TEXT,
  token_id INTEGER REFERENCES tokens(id)
);
CREATE INDEX IF NOT EXISTS idx_strlit_literal ON string_literals(literal_id);
CREATE INDEX IF NOT EXISTS idx_strlit_decoded ON string_literals(decoded);
CREATE INDEX IF NOT EXISTS idx_strlit_func ON string_literals(in_function_id);

-- ============================================================================
-- Report-L1: comments_fts — full-text search over doc_comments + raw comments
-- Adds FTS5 over doc_comments. We also store line/block/doc comment rows here.
-- (doc_comments above is per-node; this is for free-form comment search.)
-- ============================================================================
CREATE TABLE IF NOT EXISTS comments_freeform (
  id INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES cgdb_files(id) ON DELETE CASCADE,
  line INTEGER NOT NULL,
  end_line INTEGER NOT NULL DEFAULT 0,
  col INTEGER NOT NULL DEFAULT 0,
  end_col INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('line','block','doc')),
  attached_symbol_id INTEGER REFERENCES cgdb_nodes(id),
  language TEXT NOT NULL DEFAULT 'c'
);
CREATE INDEX IF NOT EXISTS idx_comments_freeform_file ON comments_freeform(file_id, line);
CREATE INDEX IF NOT EXISTS idx_comments_freeform_kind ON comments_freeform(kind);
CREATE INDEX IF NOT EXISTS idx_comments_freeform_sym ON comments_freeform(attached_symbol_id);

CREATE VIRTUAL TABLE IF NOT EXISTS comments_fts USING fts5(
  text, content='comments_freeform', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS comments_freeform_ai AFTER INSERT ON comments_freeform BEGIN
  INSERT INTO comments_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS comments_freeform_ad AFTER DELETE ON comments_freeform BEGIN
  INSERT INTO comments_fts(comments_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS comments_freeform_au AFTER UPDATE ON comments_freeform BEGIN
  INSERT INTO comments_fts(comments_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO comments_fts(rowid, text) VALUES (new.id, new.text);
END;

-- ============================================================================
-- Report-L3: ir_functions — IR-level function descriptors (aligned to symbols)
-- ============================================================================
CREATE TABLE IF NOT EXISTS ir_functions (
  id INTEGER PRIMARY KEY,
  symbol_id INTEGER NOT NULL REFERENCES cgdb_nodes(id) ON DELETE CASCADE,
  ir_name TEXT,
  entry_block_id INTEGER REFERENCES basic_blocks(id),
  num_blocks INTEGER NOT NULL DEFAULT 0,
  num_instructions INTEGER NOT NULL DEFAULT 0,
  aligned INTEGER NOT NULL DEFAULT 1,
  debug_metadata_present INTEGER NOT NULL DEFAULT 0,
  alignment_errors TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_irfunc_symbol ON ir_functions(symbol_id);

-- ============================================================================
-- Report-L3: ssa_values — SSA values aligned to AST nodes / tokens
-- ============================================================================
CREATE TABLE IF NOT EXISTS ssa_values (
  id INTEGER PRIMARY KEY,
  function_id INTEGER NOT NULL REFERENCES ir_functions(id) ON DELETE CASCADE,
  value_name TEXT NOT NULL,
  type TEXT,
  def_kind TEXT NOT NULL CHECK (def_kind IN ('entry','instruction','constant','phi','call','load','store','alloca','argument','unknown')),
  def_block_id INTEGER REFERENCES basic_blocks(id),
  def_line INTEGER,
  def_col INTEGER,
  aligned_symbol_id INTEGER REFERENCES cgdb_nodes(id),
  aligned_ast_node_id INTEGER REFERENCES cgdb_nodes(id),
  aligned_token_id INTEGER REFERENCES tokens(id)
);
CREATE INDEX IF NOT EXISTS idx_ssa_func ON ssa_values(function_id);
CREATE INDEX IF NOT EXISTS idx_ssa_symbol ON ssa_values(aligned_symbol_id);
CREATE INDEX IF NOT EXISTS idx_ssa_block ON ssa_values(def_block_id);

-- ============================================================================
-- Report-L3: mem_accesses — pointer/value read-write sites with SSA tracking
-- ============================================================================
CREATE TABLE IF NOT EXISTS mem_accesses (
  id INTEGER PRIMARY KEY,
  function_id INTEGER NOT NULL REFERENCES ir_functions(id) ON DELETE CASCADE,
  block_id INTEGER REFERENCES basic_blocks(id),
  kind TEXT NOT NULL CHECK (kind IN ('load','store','memcpy','memset','atomic_load','atomic_store','rmw','cmpxchg')),
  ptr_ssa_id INTEGER REFERENCES ssa_values(id),
  value_ssa_id INTEGER REFERENCES ssa_values(id),
  line INTEGER NOT NULL DEFAULT 0,
  col INTEGER NOT NULL DEFAULT 0,
  aligned_symbol_id INTEGER REFERENCES cgdb_nodes(id),
  aligned_ast_node_id INTEGER REFERENCES cgdb_nodes(id),
  aligned_token_id INTEGER REFERENCES tokens(id)
);
CREATE INDEX IF NOT EXISTS idx_mem_func ON mem_accesses(function_id);
CREATE INDEX IF NOT EXISTS idx_mem_symbol ON mem_accesses(aligned_symbol_id);
CREATE INDEX IF NOT EXISTS idx_mem_block ON mem_accesses(block_id);

-- ============================================================================
-- Report-L3: points_to — points-to sets (target symbol / kind / analysis)
-- ============================================================================
CREATE TABLE IF NOT EXISTS points_to (
  ssa_value_id INTEGER NOT NULL REFERENCES ssa_values(id) ON DELETE CASCADE,
  target_symbol_id INTEGER REFERENCES cgdb_nodes(id),
  target_kind TEXT NOT NULL CHECK (target_kind IN ('function','global','stack','heap','unknown','null','aggregate')),
  analysis TEXT NOT NULL CHECK (analysis IN ('svf','clang-dataflow','andersen','steensgaard','heuristic','manual')),
  confidence REAL NOT NULL DEFAULT 1.0,
  PRIMARY KEY (ssa_value_id, target_symbol_id, analysis)
);
CREATE INDEX IF NOT EXISTS idx_pt_target ON points_to(target_symbol_id);
CREATE INDEX IF NOT EXISTS idx_pt_analysis ON points_to(analysis);

-- ============================================================================
-- Report-L3: indirect_calls — call-site→target candidates (LLVM+SVF aligned)
-- ============================================================================
CREATE TABLE IF NOT EXISTS indirect_calls (
  call_site_id INTEGER PRIMARY KEY,
  call_edge_id INTEGER NOT NULL REFERENCES cgdb_edges(id) ON DELETE CASCADE,
  function_id INTEGER NOT NULL REFERENCES ir_functions(id) ON DELETE CASCADE,
  line INTEGER NOT NULL DEFAULT 0,
  col INTEGER NOT NULL DEFAULT 0,
  possible_target_symbol_id INTEGER REFERENCES cgdb_nodes(id),
  confidence REAL NOT NULL DEFAULT 1.0,
  analysis TEXT NOT NULL CHECK (analysis IN ('svf','andersen','steensgaard','devirt','heuristic','manual'))
);
CREATE INDEX IF NOT EXISTS idx_icall_edge ON indirect_calls(call_edge_id);
CREATE INDEX IF NOT EXISTS idx_icall_target ON indirect_calls(possible_target_symbol_id);
CREATE INDEX IF NOT EXISTS idx_icall_func ON indirect_calls(function_id);

-- ============================================================================
-- Report-L3: data_deps — SSA-level data dependencies (def-use across blocks)
-- ============================================================================
CREATE TABLE IF NOT EXISTS data_deps (
  from_ssa_id INTEGER NOT NULL REFERENCES ssa_values(id) ON DELETE CASCADE,
  to_ssa_id INTEGER NOT NULL REFERENCES ssa_values(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('def-use','mem-dep','ctrl-dep','phi','alias')),
  function_id INTEGER NOT NULL REFERENCES ir_functions(id) ON DELETE CASCADE,
  PRIMARY KEY (from_ssa_id, to_ssa_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_dep_from ON data_deps(from_ssa_id);
CREATE INDEX IF NOT EXISTS idx_dep_to ON data_deps(to_ssa_id);
CREATE INDEX IF NOT EXISTS idx_dep_func ON data_deps(function_id);

-- ============================================================================
-- Report-L3: path_states — path-sensitive analysis state per (function, block)
-- ============================================================================
CREATE TABLE IF NOT EXISTS path_states (
  id INTEGER PRIMARY KEY,
  function_id INTEGER NOT NULL REFERENCES ir_functions(id) ON DELETE CASCADE,
  block_id INTEGER REFERENCES basic_blocks(id),
  path_id TEXT NOT NULL,
  constraints TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL DEFAULT '{}',
  line INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_path_func ON path_states(function_id);
CREATE INDEX IF NOT EXISTS idx_path_block ON path_states(block_id);
CREATE INDEX IF NOT EXISTS idx_path_pathid ON path_states(path_id);

-- ============================================================================
-- Report-L3 (upgrade): alias_sets_v3_view — view exposing the v3 alias_sets
-- with the additional analysis/ssa_value columns expected by the report.
-- The underlying alias_sets table is left untouched for backward compatibility;
-- new columns are populated via ALTER TABLE (see cgdb_migrations v3→v4).
-- ============================================================================

-- ============================================================================
-- Report-L4: call_graph_reachability — precomputed reachability matrix
-- ============================================================================
CREATE TABLE IF NOT EXISTS call_graph_reachability (
  source_symbol_id INTEGER NOT NULL REFERENCES cgdb_nodes(id) ON DELETE CASCADE,
  target_symbol_id INTEGER NOT NULL REFERENCES cgdb_nodes(id) ON DELETE CASCADE,
  distance INTEGER NOT NULL DEFAULT 0,
  paths_count INTEGER NOT NULL DEFAULT 0,
  config_predicate_id INTEGER REFERENCES config_predicates(id),
  PRIMARY KEY (source_symbol_id, target_symbol_id)
);
CREATE INDEX IF NOT EXISTS idx_reach_source ON call_graph_reachability(source_symbol_id);
CREATE INDEX IF NOT EXISTS idx_reach_target ON call_graph_reachability(target_symbol_id);
CREATE INDEX IF NOT EXISTS idx_reach_pred ON call_graph_reachability(config_predicate_id);

-- ============================================================================
-- Report-L4: module_deps — module dependency matrix (edge count per pair)
-- ============================================================================
CREATE TABLE IF NOT EXISTS module_deps (
  from_module TEXT NOT NULL,
  to_module TEXT NOT NULL,
  edge_count INTEGER NOT NULL DEFAULT 0,
  edge_kind_breakdown TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (from_module, to_module)
);
CREATE INDEX IF NOT EXISTS idx_moddeps_from ON module_deps(from_module);
CREATE INDEX IF NOT EXISTS idx_moddeps_to ON module_deps(to_module);

-- ============================================================================
-- Report-L4: function_embeddings — vector embeddings for semantic search
-- ============================================================================
CREATE TABLE IF NOT EXISTS function_embeddings (
  symbol_id INTEGER PRIMARY KEY REFERENCES cgdb_nodes(id) ON DELETE CASCADE,
  embedding BLOB,
  model TEXT NOT NULL DEFAULT 'tfidf-ngram',
  dim INTEGER NOT NULL DEFAULT 0,
  generated_at INTEGER NOT NULL DEFAULT 0,
  config_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_emb_model ON function_embeddings(model);
CREATE INDEX IF NOT EXISTS idx_emb_generated ON function_embeddings(generated_at);

-- ============================================================================
-- Report-L4: precise_write_sets — global variable writer sites (AST+IR merged)
-- ============================================================================
CREATE TABLE IF NOT EXISTS precise_write_sets (
  global_symbol_id INTEGER NOT NULL REFERENCES cgdb_nodes(id) ON DELETE CASCADE,
  writer_symbol_id INTEGER NOT NULL REFERENCES cgdb_nodes(id) ON DELETE CASCADE,
  loc_line INTEGER NOT NULL DEFAULT 0,
  loc_col INTEGER NOT NULL DEFAULT 0,
  via_path TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 1.0,
  source TEXT NOT NULL CHECK (source IN ('ast','ir-ssa','ir-alias','merged','manual')),
  PRIMARY KEY (global_symbol_id, writer_symbol_id, loc_line)
);
CREATE INDEX IF NOT EXISTS idx_writeset_global ON precise_write_sets(global_symbol_id);
CREATE INDEX IF NOT EXISTS idx_writeset_writer ON precise_write_sets(writer_symbol_id);

-- ============================================================================
-- Report-L4: arch_metrics — coupling / cohesion / complexity / cycle_count
-- ============================================================================
CREATE TABLE IF NOT EXISTS arch_metrics (
  module_name TEXT PRIMARY KEY,
  coupling REAL NOT NULL DEFAULT 0.0,
  cohesion REAL NOT NULL DEFAULT 0.0,
  complexity REAL NOT NULL DEFAULT 0.0,
  cycle_count INTEGER NOT NULL DEFAULT 0,
  fan_in INTEGER NOT NULL DEFAULT 0,
  fan_out INTEGER NOT NULL DEFAULT 0,
  computed_at INTEGER NOT NULL DEFAULT 0,
  cycles_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_archmetrics_coupling ON arch_metrics(coupling);
CREATE INDEX IF NOT EXISTS idx_archmetrics_complexity ON arch_metrics(complexity);

-- ============================================================================
-- Report-L4: history_snapshots — per-commit source sha256 + commit_sha
-- (Distinct from graph_versions which is per-build; this is per-source-snapshot
--  and carries the disk sha256 used in consistency verification.)
-- ============================================================================
CREATE TABLE IF NOT EXISTS history_snapshots (
  id INTEGER PRIMARY KEY,
  snapshot_at INTEGER NOT NULL DEFAULT 0,
  source_file_id INTEGER REFERENCES cgdb_files(id) ON DELETE CASCADE,
  sha256 TEXT NOT NULL,
  commit_sha TEXT,
  graph_version_id INTEGER REFERENCES graph_versions(version_id)
);
CREATE INDEX IF NOT EXISTS idx_hist_time ON history_snapshots(snapshot_at);
CREATE INDEX IF NOT EXISTS idx_hist_file ON history_snapshots(source_file_id);
CREATE INDEX IF NOT EXISTS idx_hist_sha ON history_snapshots(sha256);

-- ============================================================================
-- Report-L4: alignment_errors — layer-misalignment registry (write-back block)
-- ============================================================================
CREATE TABLE IF NOT EXISTS alignment_errors (
  id INTEGER PRIMARY KEY,
  layer TEXT NOT NULL CHECK (layer IN ('L1','L2','L3','L4','cross_lang','all')),
  table_name TEXT,
  row_id INTEGER,
  error_kind TEXT NOT NULL,
  raw_payload TEXT,
  detected_at INTEGER NOT NULL DEFAULT 0,
  resolved INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_align_err_layer ON alignment_errors(layer);
CREATE INDEX IF NOT EXISTS idx_align_err_table ON alignment_errors(table_name);
CREATE INDEX IF NOT EXISTS idx_align_err_kind ON alignment_errors(error_kind);
CREATE INDEX IF NOT EXISTS idx_align_err_unresolved ON alignment_errors(layer, resolved) WHERE resolved = 0;

-- ============================================================================
-- Report-多库 (multi-db routing): db_routing + precompute_tasks
-- Even though current implementation uses single SQLite db, these tables let
-- the DBRouter class decide where to route queries when multi-db is enabled.
-- ============================================================================
CREATE TABLE IF NOT EXISTS db_routing (
  table_name TEXT PRIMARY KEY,
  db_name TEXT NOT NULL,
  engine TEXT NOT NULL CHECK (engine IN ('sqlite','duckdb','neo4j','sqlite-vec','chroma','bitmap','json1')),
  dsn TEXT,
  last_synced_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_dbrouting_db ON db_routing(db_name);

CREATE TABLE IF NOT EXISTS precompute_tasks (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  query_template TEXT NOT NULL,
  output_table TEXT NOT NULL,
  last_run_at INTEGER,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed')),
  last_error TEXT,
  rows_written INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_precompute_status ON precompute_tasks(status);

-- ============================================================================
-- Report-跨语言 (cross-language bridge): cross_lang_bindings
-- FFI bindings between symbols across languages (Python↔C, Go↔C, Rust↔C, etc.)
-- ============================================================================
CREATE TABLE IF NOT EXISTS cross_lang_bindings (
  id INTEGER PRIMARY KEY,
  from_symbol_id INTEGER NOT NULL REFERENCES cgdb_nodes(id) ON DELETE CASCADE,
  to_symbol_id INTEGER NOT NULL REFERENCES cgdb_nodes(id) ON DELETE CASCADE,
  ffi_kind TEXT NOT NULL CHECK (ffi_kind IN (
    'pybind11','cython','ctypes','cffi','napi','emscripten','wasm',
    'jni','jna','bindgen','cbindgen','cgo','extern_c','other'
  )),
  calling_convention TEXT NOT NULL DEFAULT 'cdecl',
  binding_source TEXT,
  confidence REAL NOT NULL DEFAULT 1.0,
  aligned INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_xlang_from ON cross_lang_bindings(from_symbol_id);
CREATE INDEX IF NOT EXISTS idx_xlang_to ON cross_lang_bindings(to_symbol_id);
CREATE INDEX IF NOT EXISTS idx_xlang_kind ON cross_lang_bindings(ffi_kind);

-- ============================================================================
-- Report-跨语言: type_mappings — cross-language type marshal map
-- ============================================================================
CREATE TABLE IF NOT EXISTS type_mappings (
  id INTEGER PRIMARY KEY,
  from_type_id INTEGER REFERENCES cgdb_types(id),
  to_type_id INTEGER REFERENCES cgdb_types(id),
  from_type_spelling TEXT NOT NULL DEFAULT '',
  to_type_spelling TEXT NOT NULL DEFAULT '',
  from_language TEXT NOT NULL DEFAULT 'c',
  to_language TEXT NOT NULL DEFAULT 'c',
  mapping_kind TEXT NOT NULL CHECK (mapping_kind IN ('identity','widening','narrowing','marshalling','lossy','unsupported')),
  marshalling_cost TEXT NOT NULL DEFAULT 'none',
  notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_typemap_from ON type_mappings(from_type_id);
CREATE INDEX IF NOT EXISTS idx_typemap_to ON type_mappings(to_type_id);
CREATE INDEX IF NOT EXISTS idx_typemap_lang ON type_mappings(from_language, to_language);

-- ============================================================================
-- Report-跨语言: ffi_call_sites — actual call sites that cross language
-- boundary via an FFI binding
-- ============================================================================
CREATE TABLE IF NOT EXISTS ffi_call_sites (
  id INTEGER PRIMARY KEY,
  binding_id INTEGER NOT NULL REFERENCES cross_lang_bindings(id) ON DELETE CASCADE,
  file_id INTEGER NOT NULL REFERENCES cgdb_files(id) ON DELETE CASCADE,
  line INTEGER NOT NULL DEFAULT 0,
  col INTEGER NOT NULL DEFAULT 0,
  at_token_id INTEGER REFERENCES tokens(id),
  cross_lang_call_edge_id INTEGER REFERENCES cgdb_edges(id),
  marshalling_data_symbol_id INTEGER REFERENCES cgdb_nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_ffi_site_binding ON ffi_call_sites(binding_id);
CREATE INDEX IF NOT EXISTS idx_ffi_site_file ON ffi_call_sites(file_id, line);

-- ============================================================================
-- Report-跨语言: language_adapters — registry of installed language adapters
-- ============================================================================
CREATE TABLE IF NOT EXISTS language_adapters (
  language TEXT PRIMARY KEY,
  tier TEXT NOT NULL CHECK (tier IN ('A','B','C')),
  ast_adapter TEXT NOT NULL,
  ir_adapter TEXT,
  ir_kind TEXT NOT NULL CHECK (ir_kind IN ('llvm-ir','jimple','msil','go-ssa','python-bytecode','ts-compiler','none')),
  supported_versions TEXT NOT NULL DEFAULT '[]',
  adapter_version TEXT NOT NULL DEFAULT '0.1.0',
  coverage_level TEXT NOT NULL CHECK (coverage_level IN ('L1+L2','L1+L2+L3-weak','L1+L2+L3','L1+L2+L3+L4')),
  notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_langadapter_tier ON language_adapters(tier);

-- ============================================================================
-- Report-跨语言: runtime_observations — C-档 runtime sampling for dynamic langs
-- ============================================================================
CREATE TABLE IF NOT EXISTS runtime_observations (
  id INTEGER PRIMARY KEY,
  language TEXT NOT NULL,
  symbol_id INTEGER REFERENCES cgdb_nodes(id) ON DELETE CASCADE,
  observed_type TEXT,
  observed_targets TEXT NOT NULL DEFAULT '[]',
  observed_at_block_id INTEGER REFERENCES basic_blocks(id),
  source TEXT NOT NULL CHECK (source IN ('runtime_observed','static_inferred','declared','hybrid')),
  confidence REAL NOT NULL DEFAULT 1.0,
  observed_at INTEGER NOT NULL DEFAULT 0,
  scenario TEXT
);
CREATE INDEX IF NOT EXISTS idx_runtime_symbol ON runtime_observations(symbol_id);
CREATE INDEX IF NOT EXISTS idx_runtime_lang ON runtime_observations(language);
CREATE INDEX IF NOT EXISTS idx_runtime_source ON runtime_observations(source);

-- ============================================================================
-- Report-跨语言: dependencies — per-language dependency manifest entries
-- (pip/npm/cargo/maven/nuget/pypi)
-- ============================================================================
CREATE TABLE IF NOT EXISTS dependencies (
  id INTEGER PRIMARY KEY,
  language TEXT NOT NULL,
  name TEXT NOT NULL,
  version TEXT,
  source TEXT NOT NULL CHECK (source IN ('pip','npm','cargo','maven','nuget','pypi','go-mod','gem','composer','other')),
  resolved_path TEXT,
  is_dev INTEGER NOT NULL DEFAULT 0,
  license TEXT,
  declared_in_file TEXT
);
CREATE INDEX IF NOT EXISTS idx_dep_name ON dependencies(name);
CREATE INDEX IF NOT EXISTS idx_dep_lang ON dependencies(language);
CREATE INDEX IF NOT EXISTS idx_dep_source ON dependencies(source);
"""


# ============================================================================
# Helper: schema version check
# ============================================================================
def get_cgdb_schema_version(conn: sqlite3.Connection) -> int:
    """Return the cgdb schema version stored in meta, or 0 if not present."""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'cgdb_schema_version'"
    ).fetchone()
    return int(row[0]) if row else 0


def needs_cgdb_migration(conn: sqlite3.Connection) -> bool:
    """Return True if cgdb schema is missing or older than current version."""
    return get_cgdb_schema_version(conn) < CGDB_SCHEMA_VERSION
