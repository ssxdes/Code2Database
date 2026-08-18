"""cgdb (code graph database) SQLite schema.

Adds 13-layer semantic tables to the existing code2database.db (side-by-side with
the legacy `functions`/`edges` tables for backward compatibility).

Layers:
  L0  meta / graph_versions       — version control + metadata
  L1  nodes / files               — multi-kind first-class nodes
  L2  types                       — independent type system
  L3  conditions                  — Z3-reasonable boolean expression trees
  L3.5 config_predicates          — #ifdef predicate tree (BDD + Z3 form)
  L4  basic_blocks / cfg_edges    — control flow graph
  L5  data_flow / alias_sets      — def-use chain + pointer alias
  L7  invoke_sites / ops_bindings — invocation graph refinement + typed vtable dispatch
  L8  sync_primitives / happens_before — concurrency + memory model
  Full-text: nodes_fts (FTS5 virtual table)

`apply_cgdb_schema(conn)` is idempotent — safe to call on existing databases.
Schema evolution is handled by `cgdb_migrations.run_migrations` — each version
bump has a migration function that ALTERs existing tables in-place, preserving
data.
"""
import sqlite3
import json

CGDB_SCHEMA_VERSION = 3


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
        # Fresh database — apply full DDL at current version
        conn.executescript(_CGDB_DDL)
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
