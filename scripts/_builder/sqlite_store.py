#!/usr/bin/env python3
"""SQLite storage backend for Code2Database.

Stores extraction data, graph nodes/edges, and query indexes in SQLite
for efficient querying and reduced disk usage (compression + no redundancy).

Usage:
    store = SQLiteStore(db_path)
    store.connect()
    store.store_functions(functions_list)
    store.store_edges(edges_list)
    store.store_communities(communities_list)
    store.store_entry_scores(scores_list)
    store.store_domain_stats(domain, stats_dict)

    # Query
    func = store.get_function(func_id)
    neighbors = store.get_callers(func_id)
    neighbors = store.get_callees(func_id)
    results = store.search_functions(keyword, limit=10)
    store.close()
"""

import json
import os
import sqlite3
import zlib
from typing import Optional, List, Dict, Any

from _builder.cgdb_schema import apply_cgdb_schema
import logging


class SQLiteStore:
    """SQLite-based storage for invocation graph data."""

    SCHEMA_VERSION = 13

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self):
        """Open database connection and create tables if needed."""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
        self._conn.execute("PRAGMA temp_store=MEMORY")   # Keep temp tables in RAM
        self._conn.execute("PRAGMA mmap_size=268435456")  # 256MB memory-mapped I/O
        self._migrate_schema()
        self._create_tables()

    def _migrate_schema(self):
        """Migrate schema if database exists with older version."""
        try:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row and int(row[0]) < self.SCHEMA_VERSION:
                # Schema changed — drop edges table so it gets recreated with new columns
                if int(row[0]) < 2:
                    self._conn.execute("DROP TABLE IF EXISTS edges")
                # Schema v3: add field_access and global_access tables for SQL-native
                # field-access queries (SQLite as query engine, not
                # serialization container). Old DBs lack these tables; _create_tables
                # will CREATE IF NOT EXISTS, but we bump version to signal rebuild
                # opportunity for projects that want backfill.
                # Schema v4: add audit_log table for command/operator-driven graph
                # edit traceability. _create_tables uses CREATE IF NOT EXISTS so
                # the table is added transparently on first connect.
                # Schema v6: add enclosing_symbol_id column to cgdb_nodes and
                # cgdb_edges (per cgdb-architecture doc 5.4.2). CREATE TABLE IF
                # NOT EXISTS won't add columns to existing tables, so we ALTER.
                if int(row[0]) < 6:
                    self._add_column_if_missing(
                        "cgdb_nodes", "enclosing_symbol_id", "INTEGER"
                    )
                    self._add_column_if_missing(
                        "cgdb_edges", "enclosing_symbol_id", "INTEGER"
                    )
                    # Add indexes for the new column (idempotent).
                    try:
                        self._conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_cgdb_nodes_enclosing "
                            "ON cgdb_nodes(enclosing_symbol_id)"
                        )
                        self._conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_cgdb_edges_enclosing "
                            "ON cgdb_edges(enclosing_symbol_id)"
                        )
                    except sqlite3.OperationalError:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                # null-pointer-deref analysis. Tracks the RHS expression of
                # field writes so we can filter for '= NULL' assignments
                # specifically (e.g., bh->b_bdev = NULL).
                if int(row[0]) < 7:
                    self._add_column_if_missing(
                        "field_access", "assigned_value", "TEXT"
                    )
                    try:
                        self._conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_field_access_value "
                            "ON field_access(assigned_value)"
                        )
                    except sqlite3.OperationalError:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                # to edges for structured vtable dispatch metadata. Previously
                # this info was embedded in call_condition as '#vtable_module=X'
                # which conflated vtable type with module hint. Now we store
                # them separately so queries can filter by specific vtable
                # bindings (e.g., super_operations=ext4_evict_inode).
                if int(row[0]) < 8:
                    self._add_column_if_missing(
                        "edges", "vtable_type", "TEXT"
                    )
                    self._add_column_if_missing(
                        "edges", "vtable_bound_module", "TEXT"
                    )
                    try:
                        self._conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_edges_vtable_type "
                            "ON edges(vtable_type) WHERE vtable_type IS NOT NULL"
                        )
                        self._conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_edges_vtable_module "
                            "ON edges(vtable_bound_module) WHERE vtable_bound_module IS NOT NULL"
                        )
                    except sqlite3.OperationalError:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                # unified knowledge-base search across memory entries
                # and knowledge .md paragraphs. Replaces the per-store
                # Jaccard-token and substring searches with a single
                # FTS5 + BM25 query surface. The kb_paragraphs table
                # is a derived index — rebuildable via kb-rebuild-index
                # from the canonical filesystem sources (memory/*.json
                # and knowledge/*.md). Phase 1 of the KB unification.
                if int(row[0]) < 9:
                    try:
                        self._conn.executescript("""
                            CREATE TABLE IF NOT EXISTS kb_paragraphs (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                source_kind TEXT NOT NULL,
                                source_file TEXT NOT NULL,
                                para_index INTEGER NOT NULL,
                                title TEXT,
                                body TEXT NOT NULL,
                                tags TEXT,
                                node_ids TEXT,
                                weight REAL NOT NULL DEFAULT 1.0,
                                confidence REAL NOT NULL DEFAULT 1.0,
                                kind TEXT NOT NULL,
                                graph_version TEXT,
                                created_at TEXT NOT NULL,
                                accessed_at TEXT,
                                access_count INTEGER DEFAULT 0
                            );
                            CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_kind
                                ON kb_paragraphs(kind);
                            CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_source
                                ON kb_paragraphs(source_kind, source_file);
                            CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_weight
                                ON kb_paragraphs(weight DESC);
                            CREATE VIRTUAL TABLE IF NOT EXISTS kb_paragraphs_fts USING fts5(
                                title, body, tags,
                                content='kb_paragraphs',
                                content_rowid='id',
                                tokenize='porter unicode61'
                            );
                            CREATE TRIGGER IF NOT EXISTS kb_paragraphs_ai AFTER INSERT ON kb_paragraphs BEGIN
                                INSERT INTO kb_paragraphs_fts(rowid, title, body, tags)
                                VALUES (new.id, new.title, new.body, COALESCE(new.tags, ''));
                            END;
                            CREATE TRIGGER IF NOT EXISTS kb_paragraphs_ad AFTER DELETE ON kb_paragraphs BEGIN
                                INSERT INTO kb_paragraphs_fts(kb_paragraphs_fts, rowid, title, body, tags)
                                VALUES ('delete', old.id, old.title, old.body, COALESCE(old.tags, ''));
                            END;
                            CREATE TRIGGER IF NOT EXISTS kb_paragraphs_au AFTER UPDATE ON kb_paragraphs BEGIN
                                INSERT INTO kb_paragraphs_fts(kb_paragraphs_fts, rowid, title, body, tags)
                                VALUES ('delete', old.id, old.title, old.body, COALESCE(old.tags, ''));
                                INSERT INTO kb_paragraphs_fts(rowid, title, body, tags)
                                VALUES (new.id, new.title, new.body, COALESCE(new.tags, ''));
                            END;
                        """)
                    except sqlite3.OperationalError:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                # scope_id groups similar items (FTS5 BM25 > threshold);
                # canonical_id points to the representative item of the
                # cluster (highest weight × confidence); principle_ref
                # links a memory_qa to the knowledge_principle it
                # instantiates. All nullable for backward compat.
                if int(row[0]) < 10:
                    self._add_column_if_missing(
                        "kb_paragraphs", "scope_id", "INTEGER"
                    )
                    self._add_column_if_missing(
                        "kb_paragraphs", "canonical_id", "INTEGER"
                    )
                    self._add_column_if_missing(
                        "kb_paragraphs", "principle_ref", "INTEGER"
                    )
                    try:
                        self._conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_scope "
                            "ON kb_paragraphs(scope_id) WHERE scope_id IS NOT NULL"
                        )
                        self._conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_canonical "
                            "ON kb_paragraphs(canonical_id) WHERE canonical_id IS NOT NULL"
                        )
                        self._conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_principle_ref "
                            "ON kb_paragraphs(principle_ref) WHERE principle_ref IS NOT NULL"
                        )
                    except sqlite3.OperationalError:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                # search (optional). 384-dim float32 = 1536 bytes per
                # row. NULL when no embedding generated (lazy / optional
                # dependency: sentence-transformers).
                if int(row[0]) < 11:
                    self._add_column_if_missing(
                        "kb_paragraphs", "embedding", "BLOB"
                    )
                # Schema v12: Phase 6 — kb_items unified fact-level table.
                # Supersedes kb_paragraphs in the long term; for now
                # kb_paragraphs stays as the operational table and
                # kb_items is the fact-level migration target. Includes
                # versions[] (JSON), confidence, decay_class,
                # provenance_commit. Migration command `kb-migrate` will
                # populate kb_items from kb_paragraphs + filesystem.
                if int(row[0]) < 12:
                    try:
                        self._conn.executescript("""
                            CREATE TABLE IF NOT EXISTS kb_items (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                kind TEXT NOT NULL,
                                scope_id INTEGER,
                                canonical_id INTEGER,
                                principle_ref INTEGER,
                                title TEXT,
                                body TEXT NOT NULL,
                                tags TEXT,
                                node_ids TEXT,
                                source_refs TEXT,
                                weight REAL NOT NULL DEFAULT 1.0,
                                confidence REAL NOT NULL DEFAULT 1.0,
                                decay_class TEXT NOT NULL DEFAULT 'soft',
                                graph_version TEXT,
                                embedding BLOB,
                                versions_json TEXT,
                                created_at TEXT NOT NULL,
                                accessed_at TEXT,
                                access_count INTEGER DEFAULT 0,
                                provenance_commit TEXT,
                                provenance_operator TEXT,
                                FOREIGN KEY (scope_id) REFERENCES kb_items(id),
                                FOREIGN KEY (canonical_id) REFERENCES kb_items(id),
                                FOREIGN KEY (principle_ref) REFERENCES kb_items(id)
                            );
                            CREATE INDEX IF NOT EXISTS idx_kb_items_kind
                                ON kb_items(kind);
                            CREATE INDEX IF NOT EXISTS idx_kb_items_scope
                                ON kb_items(scope_id) WHERE scope_id IS NOT NULL;
                            CREATE INDEX IF NOT EXISTS idx_kb_items_canonical
                                ON kb_items(canonical_id) WHERE canonical_id IS NOT NULL;
                            CREATE INDEX IF NOT EXISTS idx_kb_items_weight
                                ON kb_items(weight DESC);
                            CREATE VIRTUAL TABLE IF NOT EXISTS kb_items_fts USING fts5(
                                title, body, tags,
                                content='kb_items',
                                content_rowid='id',
                                tokenize='porter unicode61'
                            );
                            CREATE TRIGGER IF NOT EXISTS kb_items_ai AFTER INSERT ON kb_items BEGIN
                                INSERT INTO kb_items_fts(rowid, title, body, tags)
                                VALUES (new.id, new.title, new.body, COALESCE(new.tags, ''));
                            END;
                            CREATE TRIGGER IF NOT EXISTS kb_items_ad AFTER DELETE ON kb_items BEGIN
                                INSERT INTO kb_items_fts(kb_items_fts, rowid, title, body, tags)
                                VALUES ('delete', old.id, old.title, old.body, COALESCE(old.tags, ''));
                            END;
                            CREATE TRIGGER IF NOT EXISTS kb_items_au AFTER UPDATE ON kb_items BEGIN
                                INSERT INTO kb_items_fts(kb_items_fts, rowid, title, body, tags)
                                VALUES ('delete', old.id, old.title, old.body, COALESCE(old.tags, ''));
                                INSERT INTO kb_items_fts(rowid, title, body, tags)
                                VALUES (new.id, new.title, new.body, COALESCE(new.tags, ''));
                            END;
                            -- Phase 9: query log for feedback loop
                            -- (records every kb-query call so kb-known-unknowns
                            -- can aggregate unmatched queries).
                            CREATE TABLE IF NOT EXISTS kb_query_log (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                query TEXT NOT NULL,
                                matched INTEGER NOT NULL,
                                match_count INTEGER DEFAULT 0,
                                top_score REAL,
                                queried_at TEXT NOT NULL
                            );
                            CREATE INDEX IF NOT EXISTS idx_kb_query_log_matched
                                ON kb_query_log(matched, queried_at);
                            CREATE INDEX IF NOT EXISTS idx_kb_query_log_query
                                ON kb_query_log(query);
                        """)
                    except sqlite3.OperationalError:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                # watched_c2ds tables. Lets project B's C2D reference
                # functions in project A's C2D without merging dbs.
                # When A updates, B can sync to detect renamed/deleted/added.
                if int(row[0]) < 13:
                    try:
                        self._conn.executescript("""
                            CREATE TABLE IF NOT EXISTS foreign_refs (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                local_node_id TEXT NOT NULL,
                                invoked_name TEXT NOT NULL,
                                invoked_signature TEXT,
                                foreign_c2d_path TEXT NOT NULL,
                                foreign_project_name TEXT,
                                foreign_node_id TEXT,
                                foreign_name TEXT,
                                foreign_domain TEXT,
                                foreign_source_file TEXT,
                                foreign_signature TEXT,
                                status TEXT NOT NULL DEFAULT 'unresolved',
                                resolution_strategy TEXT,
                                last_resolved_at TEXT,
                                call_order INTEGER,
                                call_condition TEXT
                            );
                            CREATE INDEX IF NOT EXISTS idx_foreign_refs_local
                                ON foreign_refs(local_node_id);
                            CREATE INDEX IF NOT EXISTS idx_foreign_refs_status
                                ON foreign_refs(status);
                            CREATE INDEX IF NOT EXISTS idx_foreign_refs_foreign_c2d
                                ON foreign_refs(foreign_c2d_path);
                            CREATE TABLE IF NOT EXISTS watched_c2ds (
                                c2d_path TEXT PRIMARY KEY,
                                project_name TEXT,
                                db_mtime_at_sync TEXT,
                                db_size_at_sync INTEGER,
                                functions_count_at_sync INTEGER,
                                last_synced_at TEXT NOT NULL,
                                sync_status TEXT NOT NULL DEFAULT 'unknown'
                            );
                        """)
                    except sqlite3.OperationalError:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                self._conn.commit()
        except sqlite3.OperationalError:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass

    def _add_column_if_missing(self, table: str, column: str,
                                decl: str) -> None:
        """Add a column to a table if it doesn't already exist.

        SQLite doesn't support IF NOT EXISTS on ALTER TABLE ADD COLUMN, so
        we check PRAGMA table_info first.
        """
        try:
            cols = self._conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
            existing = {row[1] for row in cols}
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                )
        except sqlite3.OperationalError:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass

    def _create_tables(self):
        """Create database tables."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS functions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                domain TEXT NOT NULL,
                source_file TEXT NOT NULL,
                line_number INTEGER,
                signature TEXT,
                labels TEXT,
                body_text_compressed BLOB,
                extra_json TEXT,
                is_api_entry INTEGER DEFAULT 0,
                is_thread_processor INTEGER DEFAULT 0,
                is_callback_func INTEGER DEFAULT 0,
                is_out_end INTEGER DEFAULT 0,
                is_unknown_end INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoker_id TEXT NOT NULL,
                invoked_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                call_order INTEGER,
                call_condition TEXT,
                concurrency TEXT,
                confidence TEXT,
                confidence_score REAL,
                source TEXT,
                evidence TEXT,
                invoked_arg_json TEXT,
                reg_args_json TEXT,
                vtable_type TEXT,
                vtable_bound_module TEXT,
                FOREIGN KEY (invoker_id) REFERENCES functions(id),
                FOREIGN KEY (invoked_id) REFERENCES functions(id)
            );

            CREATE TABLE IF NOT EXISTS communities (
                id TEXT PRIMARY KEY,
                label TEXT,
                heuristic_label TEXT,
                keywords TEXT,
                cohesion REAL,
                symbol_count INTEGER,
                node_ids TEXT
            );

            CREATE TABLE IF NOT EXISTS entry_scores (
                function_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                score REAL NOT NULL,
                domain TEXT,
                FOREIGN KEY (function_id) REFERENCES functions(id)
            );

            CREATE TABLE IF NOT EXISTS domain_stats (
                domain TEXT PRIMARY KEY,
                stats_json TEXT
            );

            -- SQL-native field-access table for O(log n) queries
            -- instead of O(n) Python traversal of all nodes.
            -- Replaces the G.nodes(data=True) loop in cmd_field_access.
            CREATE TABLE IF NOT EXISTS field_access (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                function_id TEXT NOT NULL,
                function_name TEXT NOT NULL,
                domain TEXT,
                source_file TEXT,
                line_number INTEGER,
                thread_model TEXT,
                access_type TEXT NOT NULL,  -- 'read' or 'write'
                struct_chain TEXT,
                field_name TEXT NOT NULL,
                target_func TEXT,
                is_param INTEGER DEFAULT 0,
                assigned_value TEXT,  -- RHS expression for writes (e.g., 'NULL', 'bdev', 'sb->s_bdev'); NULL for reads
                FOREIGN KEY (function_id) REFERENCES functions(id)
            );

            -- SQL-native global-access table (mirrors field_access for globals).
            CREATE TABLE IF NOT EXISTS global_access (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                function_id TEXT NOT NULL,
                function_name TEXT NOT NULL,
                domain TEXT,
                source_file TEXT,
                line_number INTEGER,
                thread_model TEXT,
                access_type TEXT NOT NULL,  -- 'read' or 'write'
                global_name TEXT NOT NULL,
                FOREIGN KEY (function_id) REFERENCES functions(id)
            );

            -- preview: commit-aware change log.
            CREATE TABLE IF NOT EXISTS change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                commit_hash TEXT,
                commit_short TEXT,
                commit_author TEXT,
                commit_date TEXT,
                commit_subject TEXT,
                branch TEXT,
                node_id TEXT,
                change_type TEXT,  -- 'added' / 'modified' / 'deleted'
                diff_summary TEXT,
                affected_attrs TEXT,  -- JSON array
                logged_at TEXT
            );

            -- Audit log: records every DB-modifying operation for traceability.
            -- Distinct from change_log (which records commit-driven source changes).
            -- audit_log records operator/command-driven graph edits.
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                operator TEXT,             -- who/what triggered the change
                command TEXT,              -- CLI command name (update-node, auto-enhance, etc.)
                target_kind TEXT,          -- 'node' / 'edge' / 'profile' / 'graph'
                target_id TEXT,            -- node_id or edge identifier
                action TEXT,               -- 'update' / 'insert' / 'delete' / 'apply' / 'invalidate'
                attribute TEXT,            -- which attribute was changed
                before_value TEXT,         -- JSON-encoded prior value (or NULL)
                after_value TEXT,          -- JSON-encoded new value (or NULL)
                reason TEXT,               -- human-readable reason / context
                tx_id TEXT                 -- transaction id if part of a multi-step tx
            );

            CREATE INDEX IF NOT EXISTS idx_functions_name ON functions(name);
            CREATE INDEX IF NOT EXISTS idx_functions_domain ON functions(domain);
            CREATE INDEX IF NOT EXISTS idx_functions_source ON functions(source_file);

            CREATE VIRTUAL TABLE IF NOT EXISTS functions_fts USING fts5(
                name, signature,
                content='functions', content_rowid='rowid'
            );
            CREATE TRIGGER IF NOT EXISTS functions_ai AFTER INSERT ON functions BEGIN
              INSERT INTO functions_fts(rowid, name, signature)
              VALUES (new.rowid, new.name, new.signature);
            END;
            CREATE TRIGGER IF NOT EXISTS functions_ad AFTER DELETE ON functions BEGIN
              INSERT INTO functions_fts(functions_fts, rowid, name, signature)
              VALUES ('delete', old.rowid, old.name, old.signature);
            END;
            CREATE TRIGGER IF NOT EXISTS functions_au AFTER UPDATE ON functions BEGIN
              INSERT INTO functions_fts(functions_fts, rowid, name, signature)
              VALUES ('delete', old.rowid, old.name, old.signature);
              INSERT INTO functions_fts(rowid, name, signature)
              VALUES (new.rowid, new.name, new.signature);
            END;
            CREATE INDEX IF NOT EXISTS idx_edges_invoker ON edges(invoker_id);
            CREATE INDEX IF NOT EXISTS idx_edges_invoked ON edges(invoked_id);
            CREATE INDEX IF NOT EXISTS idx_edges_relation ON edges(relation);
            CREATE INDEX IF NOT EXISTS idx_edges_invoker_relation ON edges(invoker_id, relation);
            CREATE INDEX IF NOT EXISTS idx_edges_invoked_relation ON edges(invoked_id, relation);
            CREATE INDEX IF NOT EXISTS idx_entry_scores_score ON entry_scores(score DESC);
            -- Indexes backing field/global access SQL queries.
            CREATE INDEX IF NOT EXISTS idx_field_access_field ON field_access(field_name);
            CREATE INDEX IF NOT EXISTS idx_field_access_struct_field ON field_access(struct_chain, field_name);
            CREATE INDEX IF NOT EXISTS idx_field_access_func ON field_access(function_id);
            -- Index for filtering writes by assigned value (e.g., NULL-deref analysis).
            CREATE INDEX IF NOT EXISTS idx_field_access_value ON field_access(assigned_value);
            CREATE INDEX IF NOT EXISTS idx_global_access_name ON global_access(global_name);
            CREATE INDEX IF NOT EXISTS idx_global_access_func ON global_access(function_id);
            -- index change_log by commit and node.
            CREATE INDEX IF NOT EXISTS idx_change_log_commit ON change_log(commit_hash);
            CREATE INDEX IF NOT EXISTS idx_change_log_node ON change_log(node_id);
            -- Audit log indexes: query by target, command, timestamp, tx.
            CREATE INDEX IF NOT EXISTS idx_audit_log_target ON audit_log(target_kind, target_id);
            CREATE INDEX IF NOT EXISTS idx_audit_log_command ON audit_log(command);
            CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_audit_log_tx ON audit_log(tx_id);

            -- Knowledge-base paragraphs (Phase 1: unified FTS5 search).
            -- Derived index from memory/*.json + knowledge/*.md; rebuildable
            -- via kb-rebuild-index. Phase 4 will add scope_id/canonical_id/
            -- principle_ref columns; Phase 5 will add embedding BLOB.
            CREATE TABLE IF NOT EXISTS kb_paragraphs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_kind TEXT NOT NULL,
                source_file TEXT NOT NULL,
                para_index INTEGER NOT NULL,
                title TEXT,
                body TEXT NOT NULL,
                tags TEXT,
                node_ids TEXT,
                weight REAL NOT NULL DEFAULT 1.0,
                confidence REAL NOT NULL DEFAULT 1.0,
                kind TEXT NOT NULL,
                graph_version TEXT,
                created_at TEXT NOT NULL,
                accessed_at TEXT,
                access_count INTEGER DEFAULT 0,
                scope_id INTEGER,
                canonical_id INTEGER,
                principle_ref INTEGER,
                embedding BLOB
            );
            CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_kind
                ON kb_paragraphs(kind);
            CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_source
                ON kb_paragraphs(source_kind, source_file);
            CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_weight
                ON kb_paragraphs(weight DESC);
            CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_scope
                ON kb_paragraphs(scope_id) WHERE scope_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_canonical
                ON kb_paragraphs(canonical_id) WHERE canonical_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_kb_paragraphs_principle_ref
                ON kb_paragraphs(principle_ref) WHERE principle_ref IS NOT NULL;
            CREATE VIRTUAL TABLE IF NOT EXISTS kb_paragraphs_fts USING fts5(
                title, body, tags,
                content='kb_paragraphs',
                content_rowid='id',
                tokenize='porter unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS kb_paragraphs_ai AFTER INSERT ON kb_paragraphs BEGIN
                INSERT INTO kb_paragraphs_fts(rowid, title, body, tags)
                VALUES (new.id, new.title, new.body, COALESCE(new.tags, ''));
            END;
            CREATE TRIGGER IF NOT EXISTS kb_paragraphs_ad AFTER DELETE ON kb_paragraphs BEGIN
                INSERT INTO kb_paragraphs_fts(kb_paragraphs_fts, rowid, title, body, tags)
                VALUES ('delete', old.id, old.title, old.body, COALESCE(old.tags, ''));
            END;
            CREATE TRIGGER IF NOT EXISTS kb_paragraphs_au AFTER UPDATE ON kb_paragraphs BEGIN
                INSERT INTO kb_paragraphs_fts(kb_paragraphs_fts, rowid, title, body, tags)
                VALUES ('delete', old.id, old.title, old.body, COALESCE(old.tags, ''));
                INSERT INTO kb_paragraphs_fts(rowid, title, body, tags)
                VALUES (new.id, new.title, new.body, COALESCE(new.tags, ''));
            END;

            -- Phase 6: kb_items — unified fact-level table with
            -- versions, provenance, decay_class. Long-term successor
            -- to kb_paragraphs; both coexist during migration.
            CREATE TABLE IF NOT EXISTS kb_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                scope_id INTEGER,
                canonical_id INTEGER,
                principle_ref INTEGER,
                title TEXT,
                body TEXT NOT NULL,
                tags TEXT,
                node_ids TEXT,
                source_refs TEXT,
                weight REAL NOT NULL DEFAULT 1.0,
                confidence REAL NOT NULL DEFAULT 1.0,
                decay_class TEXT NOT NULL DEFAULT 'soft',
                graph_version TEXT,
                embedding BLOB,
                versions_json TEXT,
                created_at TEXT NOT NULL,
                accessed_at TEXT,
                access_count INTEGER DEFAULT 0,
                provenance_commit TEXT,
                provenance_operator TEXT,
                FOREIGN KEY (scope_id) REFERENCES kb_items(id),
                FOREIGN KEY (canonical_id) REFERENCES kb_items(id),
                FOREIGN KEY (principle_ref) REFERENCES kb_items(id)
            );
            CREATE INDEX IF NOT EXISTS idx_kb_items_kind
                ON kb_items(kind);
            CREATE INDEX IF NOT EXISTS idx_kb_items_scope
                ON kb_items(scope_id) WHERE scope_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_kb_items_canonical
                ON kb_items(canonical_id) WHERE canonical_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_kb_items_weight
                ON kb_items(weight DESC);
            CREATE VIRTUAL TABLE IF NOT EXISTS kb_items_fts USING fts5(
                title, body, tags,
                content='kb_items',
                content_rowid='id',
                tokenize='porter unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS kb_items_ai AFTER INSERT ON kb_items BEGIN
                INSERT INTO kb_items_fts(rowid, title, body, tags)
                VALUES (new.id, new.title, new.body, COALESCE(new.tags, ''));
            END;
            CREATE TRIGGER IF NOT EXISTS kb_items_ad AFTER DELETE ON kb_items BEGIN
                INSERT INTO kb_items_fts(kb_items_fts, rowid, title, body, tags)
                VALUES ('delete', old.id, old.title, old.body, COALESCE(old.tags, ''));
            END;
            CREATE TRIGGER IF NOT EXISTS kb_items_au AFTER UPDATE ON kb_items BEGIN
                INSERT INTO kb_items_fts(kb_items_fts, rowid, title, body, tags)
                VALUES ('delete', old.id, old.title, old.body, COALESCE(old.tags, ''));
                INSERT INTO kb_items_fts(rowid, title, body, tags)
                VALUES (new.id, new.title, new.body, COALESCE(new.tags, ''));
            END;

            -- Phase 9: query log for feedback loop & known-unknowns.
            CREATE TABLE IF NOT EXISTS kb_query_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                matched INTEGER NOT NULL,
                match_count INTEGER DEFAULT 0,
                top_score REAL,
                queried_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kb_query_log_matched
                ON kb_query_log(matched, queried_at);
            CREATE INDEX IF NOT EXISTS idx_kb_query_log_query
                ON kb_query_log(query);

            -- Phase 1 cross-C2D sync: foreign_refs + watched_c2ds.
            -- Lets project B's C2D reference functions in A's C2D
            -- without merging dbs; sync detects A's changes.
            CREATE TABLE IF NOT EXISTS foreign_refs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                local_node_id TEXT NOT NULL,
                invoked_name TEXT NOT NULL,
                invoked_signature TEXT,
                foreign_c2d_path TEXT NOT NULL,
                foreign_project_name TEXT,
                foreign_node_id TEXT,
                foreign_name TEXT,
                foreign_domain TEXT,
                foreign_source_file TEXT,
                foreign_signature TEXT,
                status TEXT NOT NULL DEFAULT 'unresolved',
                resolution_strategy TEXT,
                last_resolved_at TEXT,
                call_order INTEGER,
                call_condition TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_foreign_refs_local
                ON foreign_refs(local_node_id);
            CREATE INDEX IF NOT EXISTS idx_foreign_refs_status
                ON foreign_refs(status);
            CREATE INDEX IF NOT EXISTS idx_foreign_refs_foreign_c2d
                ON foreign_refs(foreign_c2d_path);
            CREATE TABLE IF NOT EXISTS watched_c2ds (
                c2d_path TEXT PRIMARY KEY,
                project_name TEXT,
                db_mtime_at_sync TEXT,
                db_size_at_sync INTEGER,
                functions_count_at_sync INTEGER,
                last_synced_at TEXT NOT NULL,
                sync_status TEXT NOT NULL DEFAULT 'unknown'
            );
        """)
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("schema_version", str(self.SCHEMA_VERSION))
        )
        self._conn.commit()
        # Apply cgdb (code graph database) 13-layer schema — additive, idempotent.
        # Tables coexist with legacy functions/edges for backward compatibility.
        apply_cgdb_schema(self._conn)
        # Backfill FTS5 index from existing data
        try:
            self._conn.execute(
                "INSERT INTO functions_fts(functions_fts) VALUES ('rebuild')")
            self._conn.commit()
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        # Migrate: add boolean label columns for indexed lookups.
        # Replaces LIKE '%API_entry%' full-table scans with indexed = 1 queries.
        for _col, _decl in [
            ("is_api_entry", "INTEGER DEFAULT 0"),
            ("is_thread_processor", "INTEGER DEFAULT 0"),
            ("is_callback_func", "INTEGER DEFAULT 0"),
            ("is_out_end", "INTEGER DEFAULT 0"),
            ("is_unknown_end", "INTEGER DEFAULT 0"),
            ("is_empty", "INTEGER DEFAULT 0"),
            ("node_type", "TEXT DEFAULT ''"),
        ]:
            self._add_column_if_missing("functions", _col, _decl)
        # Backfill boolean columns from existing labels JSON for migrated DBs.
        # Guard with a meta flag so the 5 LIKE '%label%' full-table scans only
        # run once (first connect on a migrated DB). Without this, every
        # connect() pays 5 full scans on 700K rows even when all rows are
        # already backfilled (the WHERE is_X = 0 clause matches nothing but
        # SQLite still scans every row to check).
        _backfill_done = False
        try:
            _row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'label_backfill_done'"
            ).fetchone()
            _backfill_done = bool(_row and _row[0] == "1")
        except Exception:
            pass
        if not _backfill_done:
            try:
                self._conn.execute(
                    "UPDATE functions SET is_api_entry = 1 "
                    "WHERE labels LIKE '%API_entry%' AND is_api_entry = 0")
                self._conn.execute(
                    "UPDATE functions SET is_thread_processor = 1 "
                    "WHERE labels LIKE '%thread_processor%' AND is_thread_processor = 0")
                self._conn.execute(
                    "UPDATE functions SET is_callback_func = 1 "
                    "WHERE labels LIKE '%callback_func%' AND is_callback_func = 0")
                self._conn.execute(
                    "UPDATE functions SET is_out_end = 1 "
                    "WHERE labels LIKE '%out_end%' AND is_out_end = 0")
                self._conn.execute(
                    "UPDATE functions SET is_unknown_end = 1 "
                    "WHERE labels LIKE '%unknown_end%' AND is_unknown_end = 0")
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) "
                    "VALUES ('label_backfill_done', '1')")
                self._conn.commit()
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        # Create indexes on the boolean columns (after migration backfill)
        try:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_functions_api_entry "
                "ON functions(is_api_entry)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_functions_thread_proc "
                "ON functions(is_thread_processor)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_functions_callback "
                "ON functions(is_callback_func)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_functions_out_end "
                "ON functions(is_out_end)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_functions_unknown_end "
                "ON functions(is_unknown_end)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_functions_is_empty "
                "ON functions(is_empty)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_functions_node_type "
                "ON functions(node_type)")
            self._conn.commit()
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        # Backfill is_empty and node_type from extra_json for migrated DBs.
        # Guard with a meta flag so the LIKE scans only run once.
        _attr_backfill_done = False
        try:
            _row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'attr_backfill_done'"
            ).fetchone()
            _attr_backfill_done = bool(_row and _row[0] == "1")
        except Exception:
            pass
        if not _attr_backfill_done:
            try:
                self._conn.execute(
                    "UPDATE functions SET is_empty = 1 "
                    "WHERE extra_json LIKE '%\"is_empty\":true%' "
                    "AND is_empty = 0")
                self._conn.execute(
                    "UPDATE functions SET is_empty = 1 "
                    "WHERE extra_json LIKE '%\"is_empty\": true%' "
                    "AND is_empty = 0")
                self._conn.execute(
                    "UPDATE functions SET node_type = 'file' "
                    "WHERE extra_json LIKE '%\"node_type\":\"file\"%' "
                    "AND node_type = ''")
                self._conn.execute(
                    "UPDATE functions SET node_type = 'file' "
                    "WHERE extra_json LIKE '%\"node_type\": \"file\"%' "
                    "AND node_type = ''")
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) "
                    "VALUES ('attr_backfill_done', '1')")
                self._conn.commit()
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
    def close(self):
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    # ---- Write operations ----

    def store_functions(self, functions: List[Dict], batch_size=500, autocommit=True):
        """Store function nodes in batches."""
        rows = []
        for func in functions:
            fid = func.get("id", "")
            name = func.get("name", "")
            domain = func.get("domain", "")
            source = func.get("source_file", "")
            line = func.get("line_number", 0) or func.get("line", 0)
            sig = func.get("signature", "")
            labels_raw = func.get("labels", [])
            labels = json.dumps(labels_raw, separators=(',', ':'))

            # Compress body_text to save space
            body = func.get("body_text", "")
            if body:
                body_compressed = sqlite3.Binary(zlib.compress(body.encode("utf-8")))
            else:
                body_compressed = None

            # Extra fields as JSON (everything not in main columns)
            extra_keys = set(func.keys()) - {
                "id", "name", "domain", "source_file", "line_number", "line",
                "signature", "labels", "body_text"
            }
            extra = {k: func[k] for k in extra_keys}
            extra_json = json.dumps(extra, ensure_ascii=False, separators=(',', ':')) if extra else None

            # Pre-computed boolean label flags for fast indexed queries
            # (replaces LIKE '%label_name%' full table scans)
            _label_set = set(labels_raw) if isinstance(labels_raw, list) else set()
            is_api = 1 if "API_entry" in _label_set else 0
            is_thread = 1 if "thread_processor" in _label_set else 0
            is_callback = 1 if "callback_func" in _label_set else 0
            is_out = 1 if "out_end" in _label_set else 0
            is_unknown = 1 if "unknown_end" in _label_set else 0
            # Pre-computed is_empty and node_type for indexed lookups
            # (replaces LIKE on extra_json in nodes(data=True) filter)
            is_empty_val = 1 if func.get("is_empty", False) else 0
            node_type_val = func.get("node_type", "") or ""

            rows.append((fid, name, domain, source, line, sig, labels,
                         body_compressed, extra_json,
                         is_api, is_thread, is_callback, is_out, is_unknown,
                         is_empty_val, node_type_val))

            if len(rows) >= batch_size:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO functions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
                )
                if autocommit:
                    self._conn.commit()
                rows = []

        if rows:
            self._conn.executemany(
                "INSERT OR REPLACE INTO functions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
            )
            if autocommit:
                self._conn.commit()

    def store_edges(self, edges: List[Dict], batch_size=1000, autocommit=True):
        """Store edges in batches."""
        rows = []
        for edge in edges:
            caller = edge.get("invoker", "") or edge.get("caller", "") or edge.get("source", "")
            callee = edge.get("invoked", "") or edge.get("callee", "") or edge.get("target", "")
            relation = edge.get("relation", "INVOKES")
            call_order = edge.get("call_order")
            call_condition = edge.get("call_condition", "")
            concurrency = edge.get("concurrency", "")
            confidence = edge.get("confidence", "")
            confidence_score = edge.get("confidence_score")
            source = edge.get("source", "")
            evidence_raw = edge.get("evidence", "")
            evidence_json = json.dumps(evidence_raw, separators=(',', ':')) if isinstance(evidence_raw, (list, dict)) else evidence_raw
            invoked_arg = edge.get("invoked_args")
            invoked_arg_json = json.dumps(invoked_arg, separators=(',', ':')) if invoked_arg else None
            reg_args = edge.get("reg_args")
            reg_args_json = json.dumps(reg_args, separators=(',', ':')) if reg_args else None
            # Structured vtable dispatch metadata (NULL for non-vtable edges).
            # Parsed from evidence string at ingest time when concurrency='vtable_dispatch'.
            vtable_type = edge.get("vtable_type", "")
            vtable_bound_module = edge.get("vtable_bound_module", "")

            rows.append((caller, callee, relation, call_order, call_condition,
                        concurrency, confidence, confidence_score, source, evidence_json,
                        invoked_arg_json, reg_args_json, vtable_type, vtable_bound_module))

            if len(rows) >= batch_size:
                self._conn.executemany(
                    "INSERT INTO edges (invoker_id, invoked_id, relation, call_order, "
                    "call_condition, concurrency, confidence, confidence_score, source, evidence, "
                    "invoked_arg_json, reg_args_json, vtable_type, vtable_bound_module) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
                )
                if autocommit:
                    self._conn.commit()
                rows = []

        if rows:
            self._conn.executemany(
                "INSERT INTO edges (invoker_id, invoked_id, relation, call_order, "
                "call_condition, concurrency, confidence, confidence_score, source, evidence, "
                "invoked_arg_json, reg_args_json, vtable_type, vtable_bound_module) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
            )
            if autocommit:
                self._conn.commit()

    def store_communities(self, communities: List[Dict]):
        """Store community data."""
        rows = []
        for comm in communities:
            cid = comm.get("id", "")
            label = comm.get("label", "")
            h_label = comm.get("heuristic_label", "")
            keywords = json.dumps(comm.get("keywords", []), separators=(',', ':'))
            cohesion = comm.get("cohesion", 0.0)
            sym_count = comm.get("symbol_count", 0)
            node_ids = json.dumps(comm.get("node_ids", []), separators=(',', ':'))
            rows.append((cid, label, h_label, keywords, cohesion, sym_count, node_ids))
        self._conn.executemany(
            "INSERT OR REPLACE INTO communities VALUES (?,?,?,?,?,?,?)", rows
        )
        self._conn.commit()

    def store_entry_scores(self, scores: List[Dict], batch_size=500):
        """Store entry point scores."""
        rows = []
        for s in scores:
            fid = s.get("id", "")
            name = s.get("name", "")
            score = s.get("score", 0.0)
            domain = s.get("domain", "")
            rows.append((fid, name, score, domain))
            if len(rows) >= batch_size:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO entry_scores VALUES (?,?,?,?)", rows
                )
                self._conn.commit()
                rows = []
        if rows:
            self._conn.executemany(
                "INSERT OR REPLACE INTO entry_scores VALUES (?,?,?,?)", rows
            )
            self._conn.commit()

    def store_domain_stats(self, domain: str, stats: Dict):
        """Store per-domain statistics."""
        self._conn.execute(
            "INSERT OR REPLACE INTO domain_stats VALUES (?,?)",
            (domain, json.dumps(stats, separators=(',', ':')))
        )
        self._conn.commit()

    # ---- SQL-native field/global access storage ----

    def store_field_access_batch(self, functions: List[Dict], autocommit: bool = True):
        """Store field-access records for multiple functions at once.

        More efficient than calling store_field_access per-function
        because it avoids per-function Python call overhead and
        uses a single executemany for all rows.
        """
        all_rows = []
        for function in functions:
            fid = function.get("id", "")
            if not fid:
                continue
            fname = function.get("name", "")
            domain = function.get("domain", "")
            source = function.get("source_file", "")
            line = function.get("line_number", 0) or function.get("line", 0)
            thread_model = function.get("thread_model") or ""
            for fr in function.get("fields_read", []) or []:
                all_rows.append((fid, fname, domain, source, line, thread_model,
                                 "read", fr.get("struct_chain", ""), fr.get("field_name", ""),
                                 fr.get("target_func"), 1 if fr.get("is_param") else 0, None))
            for fw in function.get("fields_written", []) or []:
                all_rows.append((fid, fname, domain, source, line, thread_model,
                                 "write", fw.get("struct_chain", ""), fw.get("field_name", ""),
                                 fw.get("target_func"), 1 if fw.get("is_param") else 0,
                                 fw.get("assigned_value")))
        if not all_rows:
            return
        self._conn.executemany(
            "INSERT INTO field_access (function_id, function_name, domain, "
            "source_file, line_number, thread_model, access_type, struct_chain, "
            "field_name, target_func, is_param, assigned_value) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            all_rows
        )
        if autocommit:
            self._conn.commit()

    def store_global_access_batch(self, functions: List[Dict], autocommit: bool = True):
        """Store global-variable access records for multiple functions at once."""
        all_rows = []
        for function in functions:
            fid = function.get("id", "")
            if not fid:
                continue
            fname = function.get("name", "")
            domain = function.get("domain", "")
            source = function.get("source_file", "")
            line = function.get("line_number", 0) or function.get("line", 0)
            thread_model = function.get("thread_model") or ""
            for gr in function.get("globals_read", []) or []:
                all_rows.append((fid, fname, domain, source, line, thread_model,
                                 "read", gr.get("name", "")))
            for gw in function.get("globals_written", []) or []:
                all_rows.append((fid, fname, domain, source, line, thread_model,
                                 "write", gw.get("name", "")))
        if not all_rows:
            return
        self._conn.executemany(
            "INSERT INTO global_access (function_id, function_name, domain, "
            "source_file, line_number, thread_model, access_type, global_name) "
            "VALUES (?,?,?,?,?,?,?,?)",
            all_rows
        )
        if autocommit:
            self._conn.commit()

    def store_field_access(self, function: Dict, batch_size=1000, autocommit=True):
        """Store field-access records for one function (both reads and writes).

        Called during build for each function node. Replaces the need to scan
        all nodes at query time — query reads from field_access table directly.
        """
        fid = function.get("id", "")
        if not fid:
            return
        fname = function.get("name", "")
        domain = function.get("domain", "")
        source = function.get("source_file", "")
        line = function.get("line_number", 0) or function.get("line", 0)
        thread_model = function.get("thread_model") or ""

        rows = []
        for fr in function.get("fields_read", []) or []:
            rows.append((fid, fname, domain, source, line, thread_model,
                         "read", fr.get("struct_chain", ""), fr.get("field_name", ""),
                         fr.get("target_func"), 1 if fr.get("is_param") else 0,
                         None))  # assigned_value is NULL for reads
        for fw in function.get("fields_written", []) or []:
            rows.append((fid, fname, domain, source, line, thread_model,
                         "write", fw.get("struct_chain", ""), fw.get("field_name", ""),
                         fw.get("target_func"), 1 if fw.get("is_param") else 0,
                         fw.get("assigned_value")))

        if not rows:
            return
        for i in range(0, len(rows), batch_size):
            chunk = rows[i:i + batch_size]
            self._conn.executemany(
                "INSERT INTO field_access (function_id, function_name, domain, "
                "source_file, line_number, thread_model, access_type, struct_chain, "
                "field_name, target_func, is_param, assigned_value) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                chunk
            )
            if autocommit:
                self._conn.commit()

    def store_global_access(self, function: Dict, batch_size=1000, autocommit=True):
        """Store global-variable access records for one function."""
        fid = function.get("id", "")
        if not fid:
            return
        fname = function.get("name", "")
        domain = function.get("domain", "")
        source = function.get("source_file", "")
        line = function.get("line_number", 0) or function.get("line", 0)
        thread_model = function.get("thread_model") or ""

        rows = []
        for gr in function.get("globals_read", []) or []:
            rows.append((fid, fname, domain, source, line, thread_model,
                         "read", gr.get("name", "")))
        for gw in function.get("globals_written", []) or []:
            rows.append((fid, fname, domain, source, line, thread_model,
                         "write", gw.get("name", "")))

        if not rows:
            return
        for i in range(0, len(rows), batch_size):
            chunk = rows[i:i + batch_size]
            self._conn.executemany(
                "INSERT INTO global_access (function_id, function_name, domain, "
                "source_file, line_number, thread_model, access_type, global_name) "
                "VALUES (?,?,?,?,?,?,?,?)",
                chunk
            )
            if autocommit:
                self._conn.commit()

    def clear_field_global_access(self, function_id: Optional[str] = None):
        """Clear field/global access rows. If function_id given, only that function's rows."""
        if function_id:
            self._conn.execute("DELETE FROM field_access WHERE function_id = ?", (function_id,))
            self._conn.execute("DELETE FROM global_access WHERE function_id = ?", (function_id,))
        else:
            self._conn.execute("DELETE FROM field_access")
            self._conn.execute("DELETE FROM global_access")
        self._conn.commit()

    def store_change_log_entry(self, entry: Dict):
        """store one commit-aware change log entry."""
        self._conn.execute(
            "INSERT INTO change_log (commit_hash, commit_short, commit_author, "
            "commit_date, commit_subject, branch, node_id, change_type, "
            "diff_summary, affected_attrs, logged_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (entry.get("commit_hash"), entry.get("commit_short"),
             entry.get("commit_author"), entry.get("commit_date"),
             entry.get("commit_subject"), entry.get("branch"),
             entry.get("node_id"), entry.get("change_type"),
             entry.get("diff_summary"),
             json.dumps(entry.get("affected_attrs", []), separators=(',', ':')) if entry.get("affected_attrs") else None,
             entry.get("logged_at"))
        )
        self._conn.commit()

    # ---- Read operations ----

    def get_function(self, func_id: str) -> Optional[Dict]:
        """Get a function by ID."""
        row = self._conn.execute(
            "SELECT * FROM functions WHERE id = ?", (func_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_function(row)

    def get_callers(self, func_id: str, limit: int = 50) -> List[Dict]:
        """Get all functions that call the given function."""
        rows = self._conn.execute(
            "SELECT f.*, e.call_order, e.call_condition, e.concurrency, e.confidence "
            "FROM edges e JOIN functions f ON e.invoker_id = f.id "
            "WHERE e.invoked_id = ? AND e.relation NOT IN ('CONTAINS', 'IMPORTS') "
            "LIMIT ?", (func_id, limit)
        ).fetchall()
        return [self._row_to_function(r, edge_offset=9) for r in rows]

    def get_callees(self, func_id: str, limit: int = 50) -> List[Dict]:
        """Get all functions called by the given function."""
        rows = self._conn.execute(
            "SELECT f.*, e.call_order, e.call_condition, e.concurrency, e.confidence "
            "FROM edges e JOIN functions f ON e.invoked_id = f.id "
            "WHERE e.invoker_id = ? AND e.relation NOT IN ('CONTAINS', 'IMPORTS') "
            "LIMIT ?", (func_id, limit)
        ).fetchall()
        return [self._row_to_function(r, edge_offset=9) for r in rows]

    def search_functions(self, keyword: str, limit: int = 10) -> List[Dict]:
        """Search functions by keyword (name or domain)."""
        pattern = f"%{keyword}%"
        rows = self._conn.execute(
            "SELECT * FROM functions WHERE name LIKE ? OR domain LIKE ? "
            "LIMIT ?", (pattern, pattern, limit)
        ).fetchall()
        return [self._row_to_function(r) for r in rows]

    def get_entry_points(self, limit: int = 20) -> List[Dict]:
        """Get top entry points by score."""
        rows = self._conn.execute(
            "SELECT e.function_id, e.name, e.score, e.domain "
            "FROM entry_scores e ORDER BY e.score DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{"id": r[0], "name": r[1], "score": r[2], "domain": r[3]} for r in rows]

    def get_community(self, comm_id: str) -> Optional[Dict]:
        """Get community by ID."""
        row = self._conn.execute(
            "SELECT * FROM communities WHERE id = ?", (comm_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "label": row[1], "heuristic_label": row[2],
            "keywords": json.loads(row[3]) if row[3] else [],
            "cohesion": row[4], "symbol_count": row[5],
            "node_ids": json.loads(row[6]) if row[6] else [],
        }

    def get_domain_stats(self, domain: str) -> Optional[Dict]:
        """Get statistics for a domain."""
        row = self._conn.execute(
            "SELECT stats_json FROM domain_stats WHERE domain = ?", (domain,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def get_function_count(self) -> int:
        """Get total function count."""
        return self._conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]

    def get_edge_count(self) -> int:
        """Get total edge count."""
        return self._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    def get_all_domains(self) -> List[str]:
        """Get all unique domain names."""
        rows = self._conn.execute(
            "SELECT DISTINCT domain FROM functions ORDER BY domain"
        ).fetchall()
        return [r[0] for r in rows]

    # ---- SQL-native query methods ----

    def query_field_access(self, field_name: str, struct_name: str = "",
                           access_type: str = "", assigned_value: str = "",
                           limit: int = 200) -> List[Dict]:
        """Query field accessors by field name and optional struct chain.

        Replaces the O(n) loop in cmd_field_access with an indexed SQL lookup.
        Returns list of dicts with keys: function, domain, source_file, line,
        access_type, struct_chain, field_name, thread_model, target_func, is_param,
        assigned_value.
        """
        sql = ("SELECT function_name, domain, source_file, line_number, "
               "access_type, struct_chain, field_name, thread_model, "
               "target_func, is_param, assigned_value "
               "FROM field_access WHERE field_name = ?")
        params: List[Any] = [field_name]
        if struct_name:
            sql += " AND (struct_chain = ? OR struct_chain LIKE ?)"
            params.extend([struct_name, f"%{struct_name}%"])
        if access_type in ("read", "write"):
            sql += " AND access_type = ?"
            params.append(access_type)
        if assigned_value:
            # Match exact (e.g., 'NULL') or starts-with (e.g., 'NULL;').
            # Use case-insensitive LIKE for 'NULL' vs 'null' variants.
            sql += " AND (assigned_value = ? OR assigned_value LIKE ? COLLATE NOCASE)"
            params.extend([assigned_value, f"{assigned_value}%"])
        sql += " LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            entry = {
                "function": r[0], "domain": r[1], "source_file": r[2],
                "line": r[3], "access_type": r[4], "struct_chain": r[5],
                "field_name": r[6], "thread_model": r[7] or "",
            }
            if r[8]:
                entry["target_func"] = r[8]
            if r[9]:
                entry["is_param"] = True
            if r[10]:
                entry["assigned_value"] = r[10]
            out.append(entry)
        return out

    def query_global_access(self, global_name: str, access_type: str = "",
                            limit: int = 200) -> List[Dict]:
        """Query global-variable accessors by name."""
        sql = ("SELECT function_name, domain, source_file, line_number, "
               "access_type, global_name, thread_model FROM global_access "
               "WHERE global_name = ?")
        params: List[Any] = [global_name]
        if access_type in ("read", "write"):
            sql += " AND access_type = ?"
            params.append(access_type)
        sql += " LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [{
            "function": r[0], "domain": r[1], "source_file": r[2],
            "line": r[3], "access_type": r[4], "struct_chain": "(global)",
            "field_name": r[5], "thread_model": r[6] or "",
        } for r in rows]

    def query_invokers_sql(self, invoked_id: str, limit: int = 200) -> List[Dict]:
        """Get invokers of a function via SQL (indexed), with edge metadata."""
        rows = self._conn.execute(
            "SELECT f.id, f.name, f.source_file, f.line_number, f.domain, "
            "e.call_order, e.call_condition, e.concurrency, e.confidence, "
            "e.confidence_score, e.source, e.invoked_arg_json "
            "FROM edges e JOIN functions f ON e.invoker_id = f.id "
            "WHERE e.invoked_id = ? AND e.relation NOT IN ('CONTAINS', 'IMPORTS') "
            "ORDER BY e.call_order LIMIT ?", (invoked_id, limit)
        ).fetchall()
        out = []
        for r in rows:
            entry = {
                "id": r[0], "name": r[1], "source_file": r[2], "line": r[3],
                "domain": r[4], "location": f"{r[2]}:{r[3]}",
                "call_order": r[5], "call_condition": r[6] or "",
                "concurrency": r[7] or "", "confidence": r[8] or "",
            }
            if r[9] is not None:
                entry["confidence_score"] = r[9]
            if r[10]:
                entry["edge_source"] = r[10]
            if r[11]:
                try:
                    entry["invoked_args"] = json.loads(r[11])
                except Exception:
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
            out.append(entry)
        return out

    def query_invoked_sql(self, invoker_id: str, limit: int = 500) -> List[Dict]:
        """Get invoked of a function via SQL (indexed), with edge metadata."""
        rows = self._conn.execute(
            "SELECT f.id, f.name, f.source_file, f.line_number, f.domain, "
            "e.call_order, e.call_condition, e.concurrency, e.confidence, "
            "e.confidence_score, e.invoked_arg_json "
            "FROM edges e JOIN functions f ON e.invoked_id = f.id "
            "WHERE e.invoker_id = ? AND e.relation NOT IN ('CONTAINS', 'IMPORTS') "
            "ORDER BY e.call_order LIMIT ?", (invoker_id, limit)
        ).fetchall()
        out = []
        for r in rows:
            entry = {
                "id": r[0], "name": r[1], "source_file": r[2], "line": r[3],
                "domain": r[4], "location": f"{r[2]}:{r[3]}",
                "call_order": r[5], "call_condition": r[6] or "",
                "concurrency": r[7] or "", "confidence": r[8] or "",
            }
            if r[9] is not None:
                entry["confidence_score"] = r[9]
            if r[10]:
                try:
                    entry["invoked_args"] = json.loads(r[10])
                except Exception:
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
            out.append(entry)
        return out

    def query_function_by_id_sql(self, func_id: str) -> Optional[Dict]:
        """Get full function record by ID via SQL (single-row indexed lookup)."""
        return self.get_function(func_id)

    def query_functions_by_name_sql(self, name_pattern: str, limit: int = 50) -> List[Dict]:
        """Find functions whose name matches a pattern (LIKE), via indexed SQL."""
        rows = self._conn.execute(
            "SELECT * FROM functions WHERE name LIKE ? LIMIT ?",
            (f"%{name_pattern}%", limit)
        ).fetchall()
        return [self._row_to_function(r) for r in rows]

    def query_functions_by_domain_sql(self, domain: str, limit: int = 500) -> List[Dict]:
        """Get all functions in a domain via indexed SQL."""
        rows = self._conn.execute(
            "SELECT * FROM functions WHERE domain = ? LIMIT ?", (domain, limit)
        ).fetchall()
        return [self._row_to_function(r) for r in rows]

    def query_thread_processors_sql(self, limit: int = 200) -> List[Dict]:
        """Find all thread_processor functions via SQL.

        Uses the indexed boolean column is_thread_processor (fast O(log n)
        index lookup). Falls back to json_each cross-join or LIKE scan
        only when the column is missing (pre-migration DBs).
        """
        try:
            rows = self._conn.execute(
                "SELECT * FROM functions WHERE is_thread_processor = 1 LIMIT ?",
                (limit,)
            ).fetchall()
            return [self._row_to_function(r) for r in rows]
        except sqlite3.OperationalError:
            # Column missing (migration failed) — try json_each, then LIKE
            try:
                rows = self._conn.execute(
                    "SELECT f.* FROM functions f, json_each(f.labels) "
                    "WHERE json_each.value = 'thread_processor' LIMIT ?",
                    (limit,)
                ).fetchall()
                return [self._row_to_function(r) for r in rows]
            except sqlite3.OperationalError:
                # Older SQLite without json1 — LIKE scan always works
                rows = self._conn.execute(
                    "SELECT * FROM functions WHERE labels LIKE '%thread_processor%' LIMIT ?",
                    (limit,)
                ).fetchall()
                return [self._row_to_function(r) for r in rows]

    def query_change_log_by_node(self, node_id: str, limit: int = 50) -> List[Dict]:
        """get commit history for a node."""
        rows = self._conn.execute(
            "SELECT commit_hash, commit_short, commit_author, commit_date, "
            "commit_subject, branch, change_type, diff_summary, affected_attrs "
            "FROM change_log WHERE node_id = ? ORDER BY commit_date DESC LIMIT ?",
            (node_id, limit)
        ).fetchall()
        return [{
            "commit": r[0], "commit_short": r[1], "author": r[2], "date": r[3],
            "subject": r[4], "branch": r[5], "change_type": r[6],
            "diff_summary": r[7],
            "affected_attrs": json.loads(r[8]) if r[8] else [],
        } for r in rows]

    def query_change_log_by_commit(self, commit_hash: str) -> List[Dict]:
        """get all node changes for a commit."""
        rows = self._conn.execute(
            "SELECT node_id, change_type, diff_summary, affected_attrs "
            "FROM change_log WHERE commit_hash = ? OR commit_short = ?",
            (commit_hash, commit_hash)
        ).fetchall()
        return [{
            "node_id": r[0], "change_type": r[1], "diff_summary": r[2],
            "affected_attrs": json.loads(r[3]) if r[3] else [],
        } for r in rows]

    # ------------------------------------------------------------------
    # CTE-based recursive path queries (D7+D8)
    # ------------------------------------------------------------------

    def query_call_chain_cte(self, start_id: str, max_depth: int = 5,
                             direction: str = "down") -> List[Dict]:
        """Recursive CTE for call chains starting from start_id.

        direction='down': follow INVOKES edges from invoker to invoked.
        direction='up':   follow INVOKES edges from invoked to invoker.

        Returns list of {depth, function_id, function_name, source_file,
        line_number} entries.
        """
        if direction not in ("down", "up"):
            direction = "down"
        # In edges table, invoker_id -> invoked_id is an INVOKES edge.
        # For 'down' (invoked), we move from invoker_id to invoked_id.
        # For 'up' (invokers), we move from invoked_id to invoker_id.
        if direction == "down":
            next_node = "invoked_id"
            join_col = "invoker_id"
        else:
            next_node = "invoker_id"
            join_col = "invoked_id"

        sql = f"""
            WITH RECURSIVE chain(depth, node_id) AS (
                SELECT 0, ?
                UNION
                SELECT c.depth + 1, e.{next_node}
                FROM chain c
                JOIN edges e ON e.{join_col} = c.node_id
                WHERE c.depth < ?
                  AND e.relation = 'INVOKES'
            )
            SELECT c.depth, c.node_id, f.name, f.source_file, f.line_number
            FROM chain c
            LEFT JOIN functions f ON f.id = c.node_id
            ORDER BY c.depth, f.name
        """
        rows = self._conn.execute(sql, (start_id, max_depth)).fetchall()
        return [{
            "depth": r[0],
            "function_id": r[1],
            "function_name": r[2] or "",
            "source_file": r[3] or "",
            "line_number": r[4] or 0,
        } for r in rows]

    def query_path_between_cte(self, from_id: str, to_id: str,
                               max_depth: int = 10) -> List[Dict]:
        """Recursive CTE to find paths between two functions via INVOKES edges.

        Returns list of {path_length, path} entries where path is a list of
        function_ids starting at from_id and ending at to_id.
        """
        # Build path as a JSON array. SQLite recursive CTEs can accumulate
        # text, so we use a comma-separated string and split at the end.
        sql = """
            WITH RECURSIVE path_search(depth, node_id, path) AS (
                SELECT 0, ?, ?
                UNION
                SELECT p.depth + 1, e.invoked_id,
                       p.path || ',' || e.invoked_id
                FROM path_search p
                JOIN edges e ON e.invoker_id = p.node_id
                WHERE p.depth < ?
                  AND e.relation = 'INVOKES'
                  AND instr(p.path, e.invoked_id) = 0
            )
            SELECT depth, path FROM path_search WHERE node_id = ?
            ORDER BY depth LIMIT 50
        """
        rows = self._conn.execute(
            sql, (from_id, from_id, max_depth, to_id)
        ).fetchall()
        return [{
            "path_length": r[0] + 1,
            "path": r[1].split(","),
        } for r in rows if r[1]]

    # ---- Helper methods ----

    def _row_to_function(self, row, edge_offset=None) -> Dict:
        """Convert a database row to a function dict."""
        func = {
            "id": row[0],
            "name": row[1],
            "domain": row[2],
            "source_file": row[3],
            "line_number": row[4],
            "signature": row[5],
            "labels": json.loads(row[6]) if row[6] else [],
        }
        # Decompress body_text
        if row[7]:
            try:
                func["body_text"] = zlib.decompress(row[7]).decode("utf-8")
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        # (id, name, domain, source_file, line_number, signature, labels,
        # body_text). Filter them out before updating.
        if row[8]:
            try:
                extra = json.loads(row[8])
                _primary_keys = frozenset(func.keys())
                safe_extra = {k: v for k, v in extra.items()
                              if k not in _primary_keys}
                func.update(safe_extra)
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        if edge_offset and len(row) > edge_offset:
            func["call_order"] = row[edge_offset]
            func["call_condition"] = row[edge_offset + 1] or ""
            func["concurrency"] = row[edge_offset + 2] or ""
            func["confidence"] = row[edge_offset + 3] or ""
        return func
