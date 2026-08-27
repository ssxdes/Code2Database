"""cgdb storage — GraphWriter + GraphReader ABCs (single responsibility split).

cgdb splits into GraphWriter (bulk import) and GraphReader (interactive
queries). SQLiteCGDBStore implements both via composition.

Concrete backend: SQLiteCGDBStore — uses the same code2database.db connection as
the legacy SQLiteStore (side-by-side coexistence).
"""
import sqlite3
import json
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

from _builder.cgdb_records import (
    IngestBatch, NodeRecord, EdgeRecord, TypeRecord,
    ConfigPredicateRecord, ConditionRecord,
    BasicBlockRecord, CFGEdgeRecord, DataFlowRecord, AliasSetRecord,
    InvokeSiteRecord, OpsBindingRecord,
    SyncPrimitiveRecord, HappensBeforeRecord,
    FileRecord, IncludeRecord,
    DocCommentRecord, MetadataRecord,
)


# ============================================================================
# Abstract base classes
# ============================================================================

class CGDBWriter(ABC):
    """Write side: bulk import of IngestBatch into the cgdb store."""

    @abstractmethod
    def create_schema(self) -> None:
        """Create cgdb tables if missing. Idempotent."""
        ...

    @abstractmethod
    def begin_bulk_load(self) -> None:
        """Start a bulk-load transaction (deferred indexes, PRAGMA tuning)."""
        ...

    @abstractmethod
    def write_batch(self, batch: IngestBatch) -> None:
        """Persist one IngestBatch (one translation unit's worth of records)."""
        ...

    @abstractmethod
    def delete_file_records(self, file_path: str) -> tuple:
        """Delete all records associated with a file. Returns (node_count, edge_count)."""
        ...

    @abstractmethod
    def finalize(self) -> None:
        """Rebuild indexes, run VACUUM, commit bulk-load transaction."""
        ...

    @abstractmethod
    def record_version(self, commit_hash: str, commit_subject: str = "",
                       parent_version_id: Optional[int] = None) -> int:
        """Create a new graph_versions row, return version_id."""
        ...


class CGDBReader(ABC):
    """Read side: interactive queries over the cgdb store."""

    # ---- Basic lookups ----
    @abstractmethod
    def get_node(self, node_id: int) -> Optional[Dict[str, Any]]:
        ...

    @abstractmethod
    def search_symbols(self, query: str, kind: Optional[str] = None,
                       limit: int = 50) -> List[Dict[str, Any]]:
        """Full-text search over node name/fqn/signature/body_text via FTS5."""
        ...

    # ---- Graph traversal ----
    @abstractmethod
    def find_invokers(self, node_id: int, depth: int = 1,
                      edge_types: Optional[List[str]] = None,
                      limit: int = 200) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def find_invoked(self, node_id: int, depth: int = 1,
                     edge_types: Optional[List[str]] = None,
                     limit: int = 500) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def get_neighborhood(self, node_id: int, depth: int = 1,
                         max_nodes: int = 160) -> Dict[str, Any]:
        ...

    @abstractmethod
    def invoke_path(self, src_id: int, dst_id: int,
                    max_len: int = 10) -> List[Dict[str, Any]]:
        ...

    # ---- Semantic queries (cgdb extensions) ----
    @abstractmethod
    def find_ops_impls(self, field_name: str,
                       struct_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Find functions bound to a vtable field (e.g., file_operations.read_iter)."""
        ...

    @abstractmethod
    def find_cfg_paths(self, func_id: int,
                       max_len: int = 10) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def find_data_flow(self, var_id: int) -> Dict[str, Any]:
        ...

    @abstractmethod
    def find_aliases(self, ptr_id: int) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def find_lock_held_calls(self, func_id: int) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def check_path_feasible(self, path: List[int]) -> Dict[str, Any]:
        ...

    # ---- Config queries (L3.5) ----
    @abstractmethod
    def find_configs_for(self, node_id: int) -> List[str]:
        """Return the config predicate text_form for the given node."""
        ...

    @abstractmethod
    def find_nodes_under_config(self, config_predicate: str,
                                limit: int = 500) -> List[int]:
        """Find nodes whose config_predicate matches the given predicate text."""
        ...

    # ---- Extended symbol/type queries (per doc 5.5.7) ----
    @abstractmethod
    def get_definition(self, symbol_name: str,
                       limit: int = 10) -> List[Dict[str, Any]]:
        """Find definition nodes by name (function/var/field/typedef)."""
        ...

    @abstractmethod
    def get_function_body(self, function_name_or_id: str) -> Optional[Dict[str, Any]]:
        """Return the function body source text for a function."""
        ...

    @abstractmethod
    def get_struct_layout(self, struct_name_or_id: str) -> Optional[Dict[str, Any]]:
        """Return a struct/union's field layout."""
        ...

    @abstractmethod
    def find_type_definition(self, type_name: str,
                              limit: int = 10) -> List[Dict[str, Any]]:
        """Find type definitions by name."""
        ...

    @abstractmethod
    def check_race_condition(self, function_id: int) -> List[Dict[str, Any]]:
        """Heuristic race-condition check for a function."""
        ...

    @abstractmethod
    def index_status(self) -> Dict[str, Any]:
        """Return overall index statistics."""
        ...

    # ---- Time-travel queries (per doc 5.5.9) ----
    @abstractmethod
    def time_travel_query_node(self, node_id: int,
                                version_id: int) -> Optional[Dict[str, Any]]:
        """Return the state of a node at a specific version_id."""
        ...

    @abstractmethod
    def list_versions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return recent graph_versions rows, newest first."""
        ...


# ============================================================================
# SQLite concrete implementation
# ============================================================================

class SQLiteCGDBStore(CGDBWriter, CGDBReader):
    """SQLite-backed cgdb store. Shares the code2database.db connection with
    the legacy SQLiteStore (side-by-side schema).
    """

    def __init__(self, db_path: str, conn: Optional[sqlite3.Connection] = None):
        self._db_path = db_path
        self._conn = conn  # may be shared with SQLiteStore
        self._owns_conn = (conn is None)
        self._bulk_load_active = False

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, timeout=30.0)
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA busy_timeout = 30000")
            self._conn.execute("PRAGMA cache_size = -65536")
            self._conn.execute("PRAGMA temp_store = MEMORY")
            self._conn.execute("PRAGMA mmap_size = 268435456")
        return self._conn

    def close(self) -> None:
        if self._owns_conn and self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- CGDBWriter ----

    def create_schema(self) -> None:
        from _builder.cgdb_schema import apply_cgdb_schema
        apply_cgdb_schema(self._ensure_conn())
        # Ensure a default graph_versions row (version_id=1) exists for
        # first_seen_version/last_seen_version FK references.
        conn = self._ensure_conn()
        row = conn.execute(
            "SELECT version_id FROM graph_versions WHERE version_id = 1"
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO graph_versions (version_id, commit_hash, compiled_at) "
                "VALUES (1, 'initial', 0)"
            )
            conn.commit()

    def begin_bulk_load(self) -> None:
        conn = self._ensure_conn()
        # Defer index rebuilds and bump cache for bulk load
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA cache_size = -64000")  # 64MB
        conn.execute("BEGIN")
        self._bulk_load_active = True

    def write_batch(self, batch: IngestBatch) -> None:
        conn = self._ensure_conn()
        in_explicit_tx = self._bulk_load_active
        if not in_explicit_tx:
            conn.execute("BEGIN")
        try:
            self._write_file(conn, batch.file)
            self._write_types(conn, batch.types)
            self._write_config_predicates(conn, batch.config_predicates)
            self._write_conditions(conn, batch.conditions)
            self._write_nodes(conn, batch.nodes)
            self._write_edges(conn, batch.edges)
            self._write_basic_blocks(conn, batch.basic_blocks)
            self._write_cfg_edges(conn, batch.cfg_edges)
            self._write_data_flow(conn, batch.data_flow)
            self._write_alias_sets(conn, batch.alias_sets)
            self._write_invoke_sites(conn, batch.invoke_sites)
            self._write_ops_bindings(conn, batch.ops_bindings)
            self._write_sync_primitives(conn, batch.sync_primitives)
            self._write_happens_before(conn, batch.happens_before)
            self._write_includes(conn, batch.includes)
            self._write_doc_comments(conn, batch.doc_comments)
            self._write_metadata(conn, batch.metadata)
            if not in_explicit_tx:
                conn.execute("COMMIT")
        except Exception:
            if not in_explicit_tx:
                conn.execute("ROLLBACK")
            raise

    def delete_file_records(self, file_path: str) -> tuple:
        """Delete all cgdb records associated with a file path.

        Returns (nodes_deleted, edges_deleted). Cascades to dependent tables:
        ops_bindings, invoke_sites, basic_blocks, cfg_edges, data_flow,
        alias_sets, sync_primitives, happens_before, cgdb_includes.
        """
        conn = self._ensure_conn()
        conn.execute("BEGIN")
        try:
            file_row = conn.execute(
                "SELECT id FROM cgdb_files WHERE path = ?", (file_path,)
            ).fetchone()
            if not file_row:
                conn.execute("ROLLBACK")
                return (0, 0)
            file_id = file_row[0]
            # Find node IDs to delete
            node_ids = [r[0] for r in conn.execute(
                "SELECT id FROM cgdb_nodes WHERE file_id = ?", (file_id,)
            ).fetchall()]
            # Find edge IDs to delete (for ops_bindings edge_id FK)
            edge_ids = [r[0] for r in conn.execute(
                "SELECT id FROM cgdb_edges WHERE file_id = ?", (file_id,)
            ).fetchall()]
            if not node_ids and not edge_ids:
                conn.execute("DELETE FROM cgdb_files WHERE id = ?", (file_id,))
                conn.execute("COMMIT")
                return (0, 0)

            if node_ids:
                placeholder = ",".join("?" * len(node_ids))
                # Delete dependent records that reference node_ids.
                # Order matters for FK constraints:
                #   - cfg_edges references basic_blocks (delete cfg_edges first)
                #   - ops_bindings references cgdb_edges (delete by edge_id below)
                conn.execute(
                    f"DELETE FROM ops_bindings WHERE ops_table_id IN ({placeholder}) "
                    f"OR field_node_id IN ({placeholder}) "
                    f"OR impl_function_id IN ({placeholder})",
                    node_ids + node_ids + node_ids
                )
                conn.execute(
                    f"DELETE FROM invoke_sites WHERE invoker_id IN ({placeholder}) "
                    f"OR invoked_id IN ({placeholder})",
                    node_ids + node_ids
                )
                # Get basic_block IDs first, then delete cfg_edges before basic_blocks
                block_ids = [r[0] for r in conn.execute(
                    f"SELECT id FROM basic_blocks WHERE function_id IN ({placeholder})",
                    node_ids
                ).fetchall()]
                if block_ids:
                    block_placeholder = ",".join("?" * len(block_ids))
                    conn.execute(
                        f"DELETE FROM cfg_edges WHERE src_block_id IN ({block_placeholder}) "
                        f"OR dst_block_id IN ({block_placeholder})",
                        block_ids + block_ids
                    )
                    conn.execute(
                        f"DELETE FROM basic_blocks WHERE id IN ({block_placeholder})",
                        block_ids
                    )
                conn.execute(
                    f"DELETE FROM data_flow WHERE function_id IN ({placeholder}) "
                    f"OR var_id IN ({placeholder})",
                    node_ids + node_ids
                )
                conn.execute(
                    f"DELETE FROM alias_sets WHERE ptr1_node_id IN ({placeholder}) "
                    f"OR ptr2_node_id IN ({placeholder})",
                    node_ids + node_ids
                )
                conn.execute(
                    f"DELETE FROM doc_comments WHERE node_id IN ({placeholder})",
                    node_ids
                )
                conn.execute(
                    f"DELETE FROM node_metadata WHERE node_id IN ({placeholder})",
                    node_ids
                )
                conn.execute(
                    f"DELETE FROM sync_primitives WHERE function_id IN ({placeholder}) "
                    f"OR sync_var_id IN ({placeholder})",
                    node_ids + node_ids
                )

            if edge_ids:
                edge_placeholder = ",".join("?" * len(edge_ids))
                conn.execute(
                    f"DELETE FROM ops_bindings WHERE edge_id IN ({edge_placeholder})",
                    edge_ids
                )
                conn.execute(
                    f"DELETE FROM edge_metadata WHERE edge_id IN ({edge_placeholder})",
                    edge_ids
                )

            # Delete edges where src or dst is in node_ids, or file_id matches
            if node_ids:
                placeholder = ",".join("?" * len(node_ids))
                edges_deleted = conn.execute(
                    f"DELETE FROM cgdb_edges WHERE src_id IN ({placeholder}) "
                    f"OR dst_id IN ({placeholder}) OR file_id = ?",
                    node_ids + node_ids + [file_id]
                ).rowcount
            else:
                edges_deleted = conn.execute(
                    "DELETE FROM cgdb_edges WHERE file_id = ?",
                    (file_id,)
                ).rowcount

            # Delete includes for this file (must come before cgdb_files delete
            # because cgdb_includes.source_file_id REFERENCES cgdb_files.id).
            conn.execute(
                "DELETE FROM cgdb_includes WHERE source_file_id = ?",
                (file_id,)
            )

            # Delete nodes
            if node_ids:
                placeholder = ",".join("?" * len(node_ids))
                nodes_deleted = conn.execute(
                    f"DELETE FROM cgdb_nodes WHERE id IN ({placeholder})",
                    node_ids
                ).rowcount
            else:
                nodes_deleted = 0

            # Delete file
            conn.execute("DELETE FROM cgdb_files WHERE id = ?", (file_id,))
            conn.execute("COMMIT")
            return (nodes_deleted, edges_deleted)
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def finalize(self) -> None:
        conn = self._ensure_conn()
        if self._bulk_load_active:
            conn.execute("COMMIT")
            self._bulk_load_active = False
        conn.execute("PRAGMA synchronous = NORMAL")
        # Rebuild FTS5 index to ensure consistency
        try:
            conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES ('rebuild')")
        except sqlite3.OperationalError:
            pass  # rebuild may fail if table empty; non-fatal
        conn.commit()

    def record_version(self, commit_hash: str, commit_subject: str = "",
                       parent_version_id: Optional[int] = None) -> int:
        conn = self._ensure_conn()
        import time
        cur = conn.execute(
            "INSERT INTO graph_versions (commit_hash, commit_subject, compiled_at, parent_version_id) "
            "VALUES (?, ?, ?, ?)",
            (commit_hash, commit_subject, int(time.time()), parent_version_id)
        )
        conn.commit()
        return cur.lastrowid

    # ---- CGDBReader ----

    def get_node(self, node_id: int) -> Optional[Dict[str, Any]]:
        conn = self._ensure_conn()
        row = conn.execute(
            "SELECT id, kind, name, fqn, file_id, line, col, byte_start, byte_end, "
            "type_spelling, config_predicate_id, attrs, source_layer, confidence, commit_hash "
            "FROM cgdb_nodes WHERE id = ?",
            (node_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "kind": row[1], "name": row[2], "fqn": row[3],
            "file_id": row[4], "line": row[5], "col": row[6],
            "byte_start": row[7], "byte_end": row[8],
            "type_spelling": row[9], "config_predicate_id": row[10],
            "attrs": json.loads(row[11] or "{}"),
            "source_layer": row[12], "confidence": row[13],
            "commit_hash": row[14],
        }

    def search_symbols(self, query: str, kind: Optional[str] = None,
                       limit: int = 50) -> List[Dict[str, Any]]:
        conn = self._ensure_conn()
        # FTS5 search over nodes_fts
        fts_query = query
        sql = ("SELECT n.id, n.kind, n.name, n.fqn, n.line, n.type_spelling "
               "FROM nodes_fts f JOIN cgdb_nodes n ON n.id = f.rowid "
               "WHERE nodes_fts MATCH ? ")
        params: List[Any] = [fts_query]
        if kind:
            sql += " AND n.kind = ?"
            params.append(kind)
        sql += " LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [{"id": r[0], "kind": r[1], "name": r[2], "fqn": r[3],
                 "line": r[4], "type_spelling": r[5]} for r in rows]

    def find_invokers(self, node_id: int, depth: int = 1,
                      edge_types: Optional[List[str]] = None,
                      limit: int = 200,
                      include_vtable_dispatch: bool = False) -> List[Dict[str, Any]]:
        """Find reverse closure of a node (who invokes this node?).

        By default traverses only INVOKES edges (direct calls). When
        include_vtable_dispatch=True, also follows indirect dispatch via
        the ops_bindings + invoke_sites tables — this finds callers
        who invoke the node via `ops->field(...)` even when no pre-computed
        INVOKES edge was emitted at scan time (e.g., the base pointer was
        a function parameter, not a local ops_table variable).

        The vtable dispatch traversal:
        1. Find ops_bindings rows where impl_function_id = node_id (this
           node is the implementation of some vtable field).
        2. Find invoke_sites rows with invoke_kind='ops_bind' whose
           dispatch_candidates JSON contains the target node_id — these
           are the actual call sites that dispatch to this node.
        3. Return their invoker_id as reverse invokers at depth=1.

        Vtable-discovered invokers are returned at depth=1 (they are
        direct indirect-callers, not transitive). The recursive CTE
        continues to walk forward from each discovered invoker using
        the same edge_types filter.
        """
        conn = self._ensure_conn()
        if edge_types is None:
            edge_types = ["INVOKES"]
        placeholders = ",".join("?" * len(edge_types))
        # Recursive CTE with cycle protection
        sql = f"""
            WITH RECURSIVE invokers(depth, node_id, path) AS (
                SELECT 1, src_id, ',' || src_id || ','
                FROM cgdb_edges
                WHERE dst_id = ? AND kind IN ({placeholders})
                UNION ALL
                SELECT c.depth + 1, e.src_id, c.path || e.src_id || ','
                FROM cgdb_edges e
                JOIN invokers c ON e.dst_id = c.node_id
                WHERE c.depth < ?
                  AND e.kind IN ({placeholders})
                  AND c.path NOT LIKE '%,' || e.src_id || ',%'
            )
            SELECT DISTINCT n.id, n.kind, n.name, n.fqn, n.line
            FROM invokers c JOIN cgdb_nodes n ON n.id = c.node_id
            LIMIT ?
        """
        params = [node_id] + edge_types + [depth] + edge_types + [limit]
        rows = conn.execute(sql, params).fetchall()
        direct_invokers = [{"id": r[0], "kind": r[1], "name": r[2],
                            "fqn": r[3], "line": r[4]} for r in rows]

        if not include_vtable_dispatch:
            return direct_invokers

        # -upgrade: indirect call reverse tracing via
        # ops_bindings + invoke_sites tables.
        vtable_invokers = self._find_vtable_dispatch_invokers(
            node_id, depth=depth, edge_types=edge_types, limit=limit)

        # Merge by node id, preferring direct (which already has depth
        # information from the CTE); vtable entries are marked via a
        # 'via_dispatch' flag for downstream consumers.
        seen_ids = {inv["id"] for inv in direct_invokers}
        merged = list(direct_invokers)
        for inv in vtable_invokers:
            if inv["id"] in seen_ids:
                continue
            inv["via_dispatch"] = True
            merged.append(inv)
            seen_ids.add(inv["id"])
        return merged

    def _find_vtable_dispatch_invokers(self, node_id: int, depth: int = 1,
                                       edge_types: Optional[List[str]] = None,
                                       limit: int = 200
                                       ) -> List[Dict[str, Any]]:
        """Find invokers that dispatch to node_id via vtable indirection.

        Two sources:
        1. invoke_sites rows with invoke_kind='ops_bind' (or 'virtual'/
           'function_pointer') whose dispatch_candidates JSON contains
           node_id — the invoker_id column is the indirect caller.
        2. ops_bindings rows where impl_function_id = node_id — for each
           such binding, find invoke_sites whose invoked_id points to
           the field_node_id (the function pointer slot), then walk up
           to invoker_id. This handles the case where dispatch_candidates
           is empty but the binding exists.

        After collecting depth-1 indirect invokers, optionally recurse
        further using the same edge_types filter (so depth=N reaches
        callers-of-callers transitively).
        """
        conn = self._ensure_conn()
        if edge_types is None:
            edge_types = ["INVOKES"]

        indirect_invokers: List[Dict[str, Any]] = []
        seen_ids = {node_id}

        # Source 1: invoke_sites with dispatch_candidates containing node_id
        try:
            invoke_site_rows = conn.execute(
                "SELECT DISTINCT invoker_id FROM invoke_sites "
                "WHERE invoke_kind IN ('ops_bind', 'virtual', 'function_pointer') "
                "AND (dispatch_candidates LIKE ? OR invoked_id = ?)",
                (f'%"{node_id}"%', node_id)
            ).fetchall()
            for r in invoke_site_rows:
                invoker_id = r[0]
                if invoker_id in seen_ids:
                    continue
                node_row = conn.execute(
                    "SELECT id, kind, name, fqn, line FROM cgdb_nodes WHERE id = ?",
                    (invoker_id,)
                ).fetchone()
                if node_row:
                    indirect_invokers.append({
                        "id": node_row[0], "kind": node_row[1],
                        "name": node_row[2], "fqn": node_row[3],
                        "line": node_row[4],
                    })
                    seen_ids.add(invoker_id)
        except sqlite3.OperationalError:
            # invoke_sites table may not exist on older graphs
            pass

        # Source 2: ops_bindings → find function calls that use the same
        # ops_table + field combination. Each such invoke_site's invoker_id
        # is an indirect invoker of node_id.
        try:
            binding_rows = conn.execute(
                "SELECT ops_table_id, field_node_id FROM ops_bindings "
                "WHERE impl_function_id = ?",
                (node_id,)
            ).fetchall()
            for ob_row in binding_rows:
                ops_table_id, field_node_id = ob_row[0], ob_row[1]
                # Find invoke_sites that target the field_node (the
                # function pointer slot) on this ops_table. Since
                # invoke_sites doesn't store ops_table_id directly, we
                # rely on invoked_id matching field_node_id as a proxy.
                site_rows = conn.execute(
                    "SELECT DISTINCT invoker_id FROM invoke_sites "
                    "WHERE invoked_id = ?",
                    (field_node_id,)
                ).fetchall()
                for sr in site_rows:
                    invoker_id = sr[0]
                    if invoker_id in seen_ids:
                        continue
                    node_row = conn.execute(
                        "SELECT id, kind, name, fqn, line FROM cgdb_nodes "
                        "WHERE id = ?",
                        (invoker_id,)
                    ).fetchone()
                    if node_row:
                        indirect_invokers.append({
                            "id": node_row[0], "kind": node_row[1],
                            "name": node_row[2], "fqn": node_row[3],
                            "line": node_row[4],
                        })
                        seen_ids.add(invoker_id)
        except sqlite3.OperationalError:
            pass

        # Optional transitive recursion via direct INVOKES edges from
        # each discovered indirect invoker. This lets depth>1 reach
        # callers-of-indirect-callers.
        if depth > 1 and indirect_invokers:
            placeholders = ",".join("?" * len(edge_types))
            for inv in list(indirect_invokers):
                transitive_sql = f"""
                    WITH RECURSIVE invokers(depth, node_id, path) AS (
                        SELECT 1, src_id, ',' || src_id || ','
                        FROM cgdb_edges
                        WHERE dst_id = ? AND kind IN ({placeholders})
                        UNION ALL
                        SELECT c.depth + 1, e.src_id,
                               c.path || e.src_id || ','
                        FROM cgdb_edges e
                        JOIN invokers c ON e.dst_id = c.node_id
                        WHERE c.depth < ?
                          AND e.kind IN ({placeholders})
                          AND c.path NOT LIKE '%,' || e.src_id || ',%'
                    )
                    SELECT DISTINCT n.id, n.kind, n.name, n.fqn, n.line
                    FROM invokers c JOIN cgdb_nodes n ON n.id = c.node_id
                    LIMIT ?
                """
                t_params = ([inv["id"]] + edge_types + [depth - 1]
                            + edge_types + [limit])
                t_rows = conn.execute(transitive_sql, t_params).fetchall()
                for r in t_rows:
                    nid = r[0]
                    if nid in seen_ids:
                        continue
                    indirect_invokers.append({
                        "id": nid, "kind": r[1], "name": r[2],
                        "fqn": r[3], "line": r[4],
                    })
                    seen_ids.add(nid)
        return indirect_invokers[:limit]

    def find_invoked(self, node_id: int, depth: int = 1,
                     edge_types: Optional[List[str]] = None,
                     limit: int = 500,
                     include_vtable_dispatch: bool = False) -> List[Dict[str, Any]]:
        """Find forward closure of a node (who does this node invoke?).

        When include_vtable_dispatch=True, also follows indirect
        dispatch via ops_bindings — if node_id invokes a vtable field
        (function pointer slot), resolve through ops_bindings to the
        impl functions that may be dispatched to. This is the symmetric
        counterpart to find_invokers's --include-vtable-dispatch.
        """
        conn = self._ensure_conn()
        if edge_types is None:
            edge_types = ["INVOKES"]
        placeholders = ",".join("?" * len(edge_types))
        sql = f"""
            WITH RECURSIVE invoked(depth, node_id, path) AS (
                SELECT 1, dst_id, ',' || dst_id || ','
                FROM cgdb_edges
                WHERE src_id = ? AND kind IN ({placeholders})
                UNION ALL
                SELECT c.depth + 1, e.dst_id, c.path || e.dst_id || ','
                FROM cgdb_edges e
                JOIN invoked c ON e.src_id = c.node_id
                WHERE c.depth < ?
                  AND e.kind IN ({placeholders})
                  AND c.path NOT LIKE '%,' || e.dst_id || ',%'
            )
            SELECT DISTINCT n.id, n.kind, n.name, n.fqn, n.line
            FROM invoked c JOIN cgdb_nodes n ON n.id = c.node_id
            LIMIT ?
        """
        params = [node_id] + edge_types + [depth] + edge_types + [limit]
        rows = conn.execute(sql, params).fetchall()
        direct_invoked = [{"id": r[0], "kind": r[1], "name": r[2],
                           "fqn": r[3], "line": r[4]} for r in rows]

        if not include_vtable_dispatch:
            return direct_invoked

        # -upgrade: forward vtable dispatch resolution.
        # If node_id is an invoker that calls function pointer slots,
        # resolve each slot through ops_bindings to the impl functions.
        vtable_invoked = self._find_vtable_dispatch_invoked(
            node_id, depth=depth, edge_types=edge_types, limit=limit)

        seen_ids = {inv["id"] for inv in direct_invoked}
        merged = list(direct_invoked)
        for inv in vtable_invoked:
            if inv["id"] in seen_ids:
                continue
            inv["via_dispatch"] = True
            merged.append(inv)
            seen_ids.add(inv["id"])
        return merged

    def _find_vtable_dispatch_invoked(self, node_id: int, depth: int = 1,
                                      edge_types: Optional[List[str]] = None,
                                      limit: int = 500
                                      ) -> List[Dict[str, Any]]:
        """Resolve forward vtable dispatch from node_id.

        For each invoke_site where node_id is the invoker and the
        invoked_id is a vtable field (function pointer slot), look up
        ops_bindings to find all impl_function_id values that may be
        dispatched to. Those impls are indirect "invoked" of node_id.
        """
        conn = self._ensure_conn()
        if edge_types is None:
            edge_types = ["INVOKES"]

        indirect_invoked: List[Dict[str, Any]] = []
        seen_ids = {node_id}

        try:
            # Find all invoke_sites where node_id is the invoker
            site_rows = conn.execute(
                "SELECT invoked_id, dispatch_candidates FROM invoke_sites "
                "WHERE invoker_id = ?",
                (node_id,)
            ).fetchall()
            for sr in site_rows:
                invoked_id = sr[0]
                dispatch_candidates_json = sr[1] or '[]'
                # The invoked field node itself is also an indirect invoke target
                # — it represents what's being dispatched through.
                if invoked_id and invoked_id not in seen_ids:
                    node_row = conn.execute(
                        "SELECT id, kind, name, fqn, line FROM cgdb_nodes WHERE id = ?",
                        (invoked_id,)
                    ).fetchone()
                    if node_row:
                        indirect_invoked.append({
                            "id": node_row[0], "kind": node_row[1],
                            "name": node_row[2], "fqn": node_row[3],
                            "line": node_row[4], "via_dispatch": True,
                        })
                        seen_ids.add(invoked_id)
                # Next: if dispatch_candidates is populated, use it
                # directly — it's the scanner's best guess at the
                # possible impl functions.
                try:
                    candidate_ids = json.loads(dispatch_candidates_json)
                    if isinstance(candidate_ids, list):
                        for cid in candidate_ids:
                            if not isinstance(cid, int) or cid in seen_ids:
                                continue
                            node_row = conn.execute(
                                "SELECT id, kind, name, fqn, line FROM cgdb_nodes "
                                "WHERE id = ?",
                                (cid,)
                            ).fetchone()
                            if node_row:
                                indirect_invoked.append({
                                    "id": node_row[0], "kind": node_row[1],
                                    "name": node_row[2], "fqn": node_row[3],
                                    "line": node_row[4],
                                })
                                seen_ids.add(cid)
                except (json.JSONDecodeError, TypeError):
                    pass

                # Second: if invoked_id points to a vtable field, look
                # up ops_bindings to find all impl functions for that
                # field across all ops_tables.
                if invoked_id is not None:
                    impl_rows = conn.execute(
                        "SELECT DISTINCT impl_function_id FROM ops_bindings "
                        "WHERE field_node_id = ?",
                        (invoked_id,)
                    ).fetchall()
                    for ir in impl_rows:
                        impl_id = ir[0]
                        if impl_id in seen_ids:
                            continue
                        node_row = conn.execute(
                            "SELECT id, kind, name, fqn, line FROM cgdb_nodes "
                            "WHERE id = ?",
                            (impl_id,)
                        ).fetchone()
                        if node_row:
                            indirect_invoked.append({
                                "id": node_row[0], "kind": node_row[1],
                                "name": node_row[2], "fqn": node_row[3],
                                "line": node_row[4],
                            })
                            seen_ids.add(impl_id)
        except sqlite3.OperationalError:
            pass

        # Optional transitive recursion via direct INVOKES edges.
        if depth > 1 and indirect_invoked:
            placeholders = ",".join("?" * len(edge_types))
            for inv in list(indirect_invoked):
                transitive_sql = f"""
                    WITH RECURSIVE invoked(depth, node_id, path) AS (
                        SELECT 1, dst_id, ',' || dst_id || ','
                        FROM cgdb_edges
                        WHERE src_id = ? AND kind IN ({placeholders})
                        UNION ALL
                        SELECT c.depth + 1, e.dst_id,
                               c.path || e.dst_id || ','
                        FROM cgdb_edges e
                        JOIN invoked c ON e.src_id = c.node_id
                        WHERE c.depth < ?
                          AND e.kind IN ({placeholders})
                          AND c.path NOT LIKE '%,' || e.dst_id || ',%'
                    )
                    SELECT DISTINCT n.id, n.kind, n.name, n.fqn, n.line
                    FROM invoked c JOIN cgdb_nodes n ON n.id = c.node_id
                    LIMIT ?
                """
                t_params = ([inv["id"]] + edge_types + [depth - 1]
                            + edge_types + [limit])
                t_rows = conn.execute(transitive_sql, t_params).fetchall()
                for r in t_rows:
                    nid = r[0]
                    if nid in seen_ids:
                        continue
                    indirect_invoked.append({
                        "id": nid, "kind": r[1], "name": r[2],
                        "fqn": r[3], "line": r[4],
                    })
                    seen_ids.add(nid)
        return indirect_invoked[:limit]

    def get_neighborhood(self, node_id: int, depth: int = 1,
                         max_nodes: int = 160) -> Dict[str, Any]:
        node = self.get_node(node_id)
        if not node:
            return {"center": None, "invokers": [], "invoked": []}
        invokers = self.find_invokers(node_id, depth=depth, limit=max_nodes)
        invoked = self.find_invoked(node_id, depth=depth, limit=max_nodes)
        return {"center": node, "invokers": invokers, "invoked": invoked}

    def invoke_path(self, src_id: int, dst_id: int,
                    max_len: int = 10) -> List[Dict[str, Any]]:
        conn = self._ensure_conn()
        sql = """
            WITH RECURSIVE path(depth, node_id, edge_id, path_nodes) AS (
                SELECT 1, dst_id, id, ',' || src_id || ',' || dst_id || ','
                FROM cgdb_edges
                WHERE src_id = ? AND kind = 'INVOKES'
                UNION ALL
                SELECT p.depth + 1, e.dst_id, e.id,
                       p.path_nodes || e.dst_id || ','
                FROM cgdb_edges e
                JOIN path p ON e.src_id = p.node_id
                WHERE p.depth < ? AND e.kind = 'INVOKES'
                  AND p.path_nodes NOT LIKE '%,' || e.dst_id || ',%'
            )
            SELECT p.path_nodes, GROUP_CONCAT(p.edge_id, ',') AS edge_ids
            FROM path p
            WHERE p.node_id = ?
            GROUP BY p.path_nodes
            LIMIT 5
        """
        rows = conn.execute(sql, (src_id, max_len, dst_id)).fetchall()
        paths = []
        for row in rows:
            path_node_ids = [int(x) for x in row[0].strip(",").split(",")]
            nodes = [self.get_node(nid) for nid in path_node_ids]
            paths.append({"nodes": nodes, "edge_ids": row[1].split(",") if row[1] else []})
        return paths

    def find_ops_impls(self, field_name: str,
                       struct_type: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = self._ensure_conn()
        sql = """
            SELECT ob.ops_table_id, ob.field_node_id, ob.impl_function_id,
                   ob.signature_match,
                   ft.name AS field_name, st.name AS struct_name,
                   fn.name AS impl_name, fn.fqn AS impl_fqn
            FROM ops_bindings ob
            JOIN cgdb_nodes ft ON ft.id = ob.field_node_id
            JOIN cgdb_nodes fn ON fn.id = ob.impl_function_id
            LEFT JOIN cgdb_nodes st ON st.id = ob.ops_table_id
            WHERE ft.name = ?
        """
        params: List[Any] = [field_name]
        if struct_type:
            sql += " AND st.name = ?"
            params.append(struct_type)
        rows = conn.execute(sql, params).fetchall()
        return [{"ops_table_id": r[0], "field_node_id": r[1],
                 "impl_function_id": r[2], "signature_match": bool(r[3]),
                 "field_name": r[4], "struct_name": r[5],
                 "impl_name": r[6], "impl_fqn": r[7]} for r in rows]

    def find_cfg_paths(self, func_id: int,
                       max_len: int = 10) -> List[Dict[str, Any]]:
        conn = self._ensure_conn()
        sql = """
            WITH RECURSIVE cfg_path(depth, block_id, path_blocks) AS (
                SELECT 1, id, ',' || id || ','
                FROM basic_blocks
                WHERE function_id = ? AND is_entry = 1
                UNION ALL
                SELECT cp.depth + 1, ce.dst_block_id,
                       cp.path_blocks || ce.dst_block_id || ','
                FROM cfg_edges ce
                JOIN cfg_path cp ON ce.src_block_id = cp.block_id
                WHERE cp.depth < ? AND ce.function_id = ?
                  AND cp.path_blocks NOT LIKE '%,' || ce.dst_block_id || ',%'
            )
            SELECT path_blocks FROM cfg_path LIMIT 50
        """
        rows = conn.execute(sql, (func_id, max_len, func_id)).fetchall()
        return [{"block_path": [int(x) for x in r[0].strip(",").split(",")]}
                for r in rows]

    def find_data_flow(self, var_id: int) -> Dict[str, Any]:
        conn = self._ensure_conn()
        rows = conn.execute(
            "SELECT id, function_id, def_block_id, def_stmt_id, "
            "use_block_id, use_stmt_id, kind, path_condition_ids "
            "FROM data_flow WHERE var_id = ?",
            (var_id,)
        ).fetchall()
        return {
            "var_id": var_id,
            "entries": [{"id": r[0], "function_id": r[1],
                         "def_block_id": r[2], "def_stmt_id": r[3],
                         "use_block_id": r[4], "use_stmt_id": r[5],
                         "kind": r[6],
                         "path_condition_ids": json.loads(r[7] or "[]")}
                        for r in rows]
        }

    def find_aliases(self, ptr_id: int) -> List[Dict[str, Any]]:
        conn = self._ensure_conn()
        rows = conn.execute(
            "SELECT ptr1_node_id, ptr2_node_id, kind, confidence "
            "FROM alias_sets WHERE ptr1_node_id = ? OR ptr2_node_id = ?",
            (ptr_id, ptr_id)
        ).fetchall()
        return [{"ptr1": r[0], "ptr2": r[1], "kind": r[2], "confidence": r[3]}
                for r in rows]

    def find_lock_held_calls(self, func_id: int) -> List[Dict[str, Any]]:
        conn = self._ensure_conn()
        # Find calls made while a lock is held: lock_acquire before call,
        # no intervening lock_release.
        rows = conn.execute(
            "SELECT id, sync_var_id, kind, acquire_stmt_id, release_stmt_id, "
            "memory_order FROM sync_primitives WHERE function_id = ? "
            "ORDER BY acquire_stmt_id",
            (func_id,)
        ).fetchall()
        return [{"id": r[0], "sync_var_id": r[1], "kind": r[2],
                 "acquire_stmt_id": r[3], "release_stmt_id": r[4],
                 "memory_order": r[5]} for r in rows]

    def check_path_feasible(self, path: List[int]) -> Dict[str, Any]:
        """Check feasibility of a path through CFG blocks. Returns
        {feasible: bool, unsatisfiable_conditions: [...]}.

        Without Z3, returns a heuristic: feasible if all condition_ids on
        the path are non-contradictory.
        """
        if not path or len(path) < 2:
            return {"feasible": True, "unsatisfiable_conditions": []}
        conn = self._ensure_conn()
        # Collect condition_ids for cfg_edges along the path
        condition_ids = []
        for i in range(len(path) - 1):
            rows = conn.execute(
                "SELECT condition_id FROM cfg_edges "
                "WHERE src_block_id = ? AND dst_block_id = ?",
                (path[i], path[i+1])
            ).fetchall()
            for r in rows:
                if r[0] is not None:
                    condition_ids.append(r[0])
        if not condition_ids:
            return {"feasible": True, "unsatisfiable_conditions": []}
        # Try Z3 if available
        try:
            from z3 import Solver, parse_smt2_string, sat
            solver = Solver()
            # Pull z3_form from conditions
            for cid in condition_ids:
                row = conn.execute(
                    "SELECT z3_form FROM conditions WHERE id = ?", (cid,)
                ).fetchone()
                if row and row[0]:
                    try:
                        solver.add(parse_smt2_string(row[0]))
                    except Exception:
                        pass
            result = solver.check()
            return {"feasible": (result == sat),
                    "unsatisfiable_conditions": [] if result == sat else condition_ids}
        except ImportError:
            # No Z3 — heuristic: assume feasible
            return {"feasible": True, "unsatisfiable_conditions": [],
                    "note": "Z3 unavailable; heuristic only"}

    def find_configs_for(self, node_id: int) -> List[str]:
        conn = self._ensure_conn()
        row = conn.execute(
            "SELECT cp.text_form FROM cgdb_nodes n "
            "JOIN config_predicates cp ON n.config_predicate_id = cp.id "
            "WHERE n.id = ?",
            (node_id,)
        ).fetchone()
        if not row:
            return []
        return [row[0]] if row[0] else []

    def find_nodes_under_config(self, config_predicate: str,
                                limit: int = 500) -> List[int]:
        conn = self._ensure_conn()
        # Match by exact text_form or substring
        rows = conn.execute(
            "SELECT n.id FROM cgdb_nodes n "
            "JOIN config_predicates cp ON n.config_predicate_id = cp.id "
            "WHERE cp.text_form = ? OR cp.text_form LIKE ? "
            "LIMIT ?",
            (config_predicate, f"%{config_predicate}%", limit)
        ).fetchall()
        return [r[0] for r in rows]

    # ---- L1/L2 extended queries (per doc 5.5.7 MCP tool list) ----

    def get_definition(self, symbol_name: str,
                       limit: int = 10) -> List[Dict[str, Any]]:
        """Find definition nodes (function/var/field) by name.

        Searches cgdb_nodes by name, returning those that are definitions
        (i.e., function nodes with body_text, or var/field declarations).
        """
        conn = self._ensure_conn()
        rows = conn.execute(
            "SELECT id, kind, name, fqn, file_id, line, col, type_spelling, "
            "signature, body_text FROM cgdb_nodes "
            "WHERE name = ? AND kind IN ('function','var','field','typedef') "
            "LIMIT ?",
            (symbol_name, limit)
        ).fetchall()
        return [{"id": r[0], "kind": r[1], "name": r[2], "fqn": r[3],
                 "file_id": r[4], "line": r[5], "col": r[6],
                 "type_spelling": r[7], "signature": r[8] or "",
                 "body_text": r[9] or ""} for r in rows]

    def get_function_body(self, function_name_or_id: str) -> Optional[Dict[str, Any]]:
        """Return the function body source text for a function.

        Accepts either a numeric node_id or a function name string.
        """
        conn = self._ensure_conn()
        if isinstance(function_name_or_id, int) or (
                isinstance(function_name_or_id, str)
                and function_name_or_id.isdigit()):
            nid = int(function_name_or_id)
            row = conn.execute(
                "SELECT id, name, fqn, signature, body_text, file_id, line "
                "FROM cgdb_nodes WHERE id = ? AND kind = 'function'",
                (nid,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, name, fqn, signature, body_text, file_id, line "
                "FROM cgdb_nodes WHERE name = ? AND kind = 'function' LIMIT 1",
                (function_name_or_id,)
            ).fetchone()
        if not row:
            return None
        return {"id": row[0], "name": row[1], "fqn": row[2],
                "signature": row[3] or "", "body_text": row[4] or "",
                "file_id": row[5], "line": row[6]}

    def get_struct_layout(self, struct_name_or_id: str) -> Optional[Dict[str, Any]]:
        """Return a struct/union's field layout (field name → type, offset, size).

        Walks cgdb_edges for HAS_FIELD edges from the struct node, joining
        cgdb_nodes for field metadata.
        """
        conn = self._ensure_conn()
        struct_id: Optional[int]
        if isinstance(struct_name_or_id, int) or (
                isinstance(struct_name_or_id, str)
                and struct_name_or_id.isdigit()):
            struct_id = int(struct_name_or_id)
        else:
            row = conn.execute(
                "SELECT id FROM cgdb_nodes WHERE name = ? "
                "AND kind IN ('struct','union','class') LIMIT 1",
                (struct_name_or_id,)
            ).fetchone()
            struct_id = row[0] if row else None
        if struct_id is None:
            return None
        # Fetch struct node
        struct_row = conn.execute(
            "SELECT id, name, fqn, type_spelling FROM cgdb_nodes WHERE id = ?",
            (struct_id,)
        ).fetchone()
        if not struct_row:
            return None
        # Fetch fields via HAS_FIELD edges
        field_rows = conn.execute(
            "SELECT f.id, f.name, f.type_spelling, f.line, f.byte_start "
            "FROM cgdb_edges e JOIN cgdb_nodes f ON f.id = e.dst_id "
            "WHERE e.src_id = ? AND e.kind = 'HAS_FIELD' "
            "ORDER BY f.byte_start",
            (struct_id,)
        ).fetchall()
        return {
            "id": struct_row[0], "name": struct_row[1],
            "fqn": struct_row[2], "type_spelling": struct_row[3],
            "fields": [{"id": r[0], "name": r[1], "type_spelling": r[2],
                        "line": r[3], "byte_start": r[4]} for r in field_rows],
        }

    def find_type_definition(self, type_name: str,
                              limit: int = 10) -> List[Dict[str, Any]]:
        """Find type definitions (struct/union/enum/typedef) by name.

        Searches both cgdb_types (spelling/canonical_spelling) and cgdb_nodes
        (struct/union/enum/typedef kinds).
        """
        conn = self._ensure_conn()
        rows = conn.execute(
            "SELECT n.id, n.kind, n.name, n.fqn, n.file_id, n.line, "
            "n.type_spelling FROM cgdb_nodes n "
            "WHERE n.name = ? AND n.kind IN ('struct','union','enum','typedef','class') "
            "LIMIT ?",
            (type_name, limit)
        ).fetchall()
        return [{"id": r[0], "kind": r[1], "name": r[2], "fqn": r[3],
                 "file_id": r[4], "line": r[5], "type_spelling": r[6]}
                for r in rows]

    def check_race_condition(self, function_id: int) -> List[Dict[str, Any]]:
        """Heuristic race-condition check for a function.

        Looks for:
          - Variables accessed between lock_acquire and lock_release
            (using sync_primitives + data_flow tables)
          - Functions called while a lock is held (using sync_primitives +
            invoke_sites)
        Returns a list of potential race points with var_id, kind, and
        whether the access is protected.
        """
        conn = self._ensure_conn()
        # Get all sync_primitives for this function, ordered by acquire_stmt_id
        syncs = conn.execute(
            "SELECT kind, sync_var_id, acquire_stmt_id, release_stmt_id "
            "FROM sync_primitives WHERE function_id = ? "
            "ORDER BY acquire_stmt_id",
            (function_id,)
        ).fetchall()
        # Pair acquires with releases by sync_var_id
        races: List[Dict[str, Any]] = []
        pending_locks: Dict[int, int] = {}  # sync_var_id → acquire_stmt_id
        for kind, sync_var_id, acq, rel in syncs:
            if kind == 'lock_acquire' and sync_var_id is not None and acq is not None:
                pending_locks[sync_var_id] = acq
            elif kind == 'lock_release' and sync_var_id is not None:
                pending_locks.pop(sync_var_id, None)
        # For each unprotected var (var_id matches a sync_var_id but no lock held),
        # emit a race warning. This is heuristic — production would use
        # clang's Thread Safety Analysis (C++ plugin).
        var_accesses = conn.execute(
            "SELECT var_id, def_stmt_id, use_stmt_id, kind FROM data_flow "
            "WHERE function_id = ?",
            (function_id,)
        ).fetchall()
        for var_id, def_stmt, use_stmt, kind in var_accesses:
            # If this var is also a sync_var (lock), it's not a race target
            if var_id in pending_locks:
                continue
            # Check if any lock is held at this point — heuristic: if there
            # are any pending locks at all, consider this access protected
            if not pending_locks:
                races.append({
                    "var_id": var_id, "def_stmt_id": def_stmt,
                    "use_stmt_id": use_stmt, "kind": kind,
                    "protected": False,
                    "reason": "no lock held during access",
                })
        return races

    def index_status(self) -> Dict[str, Any]:
        """Return overall index statistics: node/edge counts by kind, file count,
        predicate count, etc.
        """
        conn = self._ensure_conn()
        result: Dict[str, Any] = {}
        # File count
        result["file_count"] = conn.execute(
            "SELECT COUNT(*) FROM cgdb_files"
        ).fetchone()[0]
        # Node count by kind
        node_kinds = conn.execute(
            "SELECT kind, COUNT(*) FROM cgdb_nodes GROUP BY kind"
        ).fetchall()
        result["nodes_by_kind"] = {r[0]: r[1] for r in node_kinds}
        result["total_nodes"] = sum(r[1] for r in node_kinds)
        # Edge count by kind
        edge_kinds = conn.execute(
            "SELECT kind, COUNT(*) FROM cgdb_edges GROUP BY kind"
        ).fetchall()
        result["edges_by_kind"] = {r[0]: r[1] for r in edge_kinds}
        result["total_edges"] = sum(r[1] for r in edge_kinds)
        # Type count
        result["type_count"] = conn.execute(
            "SELECT COUNT(*) FROM cgdb_types"
        ).fetchone()[0]
        # Predicate count
        result["predicate_count"] = conn.execute(
            "SELECT COUNT(*) FROM config_predicates"
        ).fetchone()[0]
        # Basic block / CFG counts
        result["basic_block_count"] = conn.execute(
            "SELECT COUNT(*) FROM basic_blocks"
        ).fetchone()[0]
        result["cfg_edge_count"] = conn.execute(
            "SELECT COUNT(*) FROM cfg_edges"
        ).fetchone()[0]
        # Sync primitives
        result["sync_primitive_count"] = conn.execute(
            "SELECT COUNT(*) FROM sync_primitives"
        ).fetchone()[0]
        # Ops bindings
        result["ops_binding_count"] = conn.execute(
            "SELECT COUNT(*) FROM ops_bindings"
        ).fetchone()[0]
        # Versions
        result["version_count"] = conn.execute(
            "SELECT COUNT(*) FROM graph_versions"
        ).fetchone()[0]
        return result

    def load_config_predicates_map(self) -> Dict[int, Dict[str, Any]]:
        """Return a dict mapping config_predicate_id → predicate metadata.

        Used by path-feasibility --with-configs to resolve edge
        config_predicate_id references to their text_form for evaluation.
        """
        conn = self._ensure_conn()
        rows = conn.execute(
            "SELECT id, text_form, z3_form, bdd_serialized, config_macros, "
            "is_unconditional, is_contradictory FROM config_predicates"
        ).fetchall()
        out: Dict[int, Dict[str, Any]] = {}
        for r in rows:
            pid, text_form, z3_form, bdd_serialized, config_macros, uncond, contra = r
            try:
                macros = json.loads(config_macros) if config_macros else []
            except Exception:
                macros = []
            out[pid] = {
                "id": pid,
                "text_form": text_form or "",
                "z3_form": z3_form or "",
                "bdd_serialized": bdd_serialized or "",
                "config_macros": macros,
                "is_unconditional": bool(uncond),
                "is_contradictory": bool(contra),
            }
        return out

    # ---- Time-travel queries (per doc 5.5.9) ----

    def time_travel_query_node(self, node_id: int,
                                version_id: int) -> Optional[Dict[str, Any]]:
        """Return the state of a node at a specific version_id, or None if
        the node didn't exist or had been soft-deleted by that version.
        """
        conn = self._ensure_conn()
        row = conn.execute(
            "SELECT id, kind, name, fqn, file_id, line, col, byte_start, "
            "byte_end, type_spelling, config_predicate_id, attrs, "
            "first_seen_version, last_seen_version, commit_hash "
            "FROM cgdb_nodes WHERE id = ? "
            "AND first_seen_version <= ? "
            "AND (last_seen_version > ? OR last_seen_version = "
            "    (SELECT MAX(version_id) FROM graph_versions))",
            (node_id, version_id, version_id)
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "kind": row[1], "name": row[2], "fqn": row[3],
            "file_id": row[4], "line": row[5], "col": row[6],
            "byte_start": row[7], "byte_end": row[8],
            "type_spelling": row[9], "config_predicate_id": row[10],
            "attrs": json.loads(row[11] or "{}"),
            "first_seen_version": row[12], "last_seen_version": row[13],
            "commit_hash": row[14],
        }

    def list_versions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return recent graph_versions rows, newest first."""
        conn = self._ensure_conn()
        rows = conn.execute(
            "SELECT version_id, commit_hash, commit_subject, compiled_at, "
            "parent_version_id FROM graph_versions "
            "ORDER BY version_id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [{"version_id": r[0], "commit_hash": r[1],
                 "commit_subject": r[2], "compiled_at": r[3],
                 "parent_version_id": r[4]} for r in rows]

    # ---- Private write helpers ----

    def _write_file(self, conn: sqlite3.Connection, file: Optional[FileRecord]) -> None:
        if file is None:
            return
        conn.execute(
            "INSERT OR REPLACE INTO cgdb_files "
            "(id, path, is_system, language, sha256, line_count, byte_count, "
            " commit_hash, last_modified, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (file.id, file.path, int(file.is_system), file.language,
             file.sha256, file.line_count, file.byte_count,
             file.commit_hash, file.last_modified, file.content_hash)
        )

    def _write_types(self, conn: sqlite3.Connection, types: List[TypeRecord]) -> None:
        if not types:
            return
        rows = [
            (t.id, t.spelling, t.canonical_spelling, t.kind,
             t.size_bytes, t.alignment, int(t.is_const), int(t.is_volatile),
             t.pointee_type_id, t.element_type_id, t.record_id,
             json.dumps(t.attrs, ensure_ascii=False))
            for t in types
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO cgdb_types "
            "(id, spelling, canonical_spelling, kind, size_bytes, alignment, "
            " is_const, is_volatile, pointee_type_id, element_type_id, "
            " record_id, attrs) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows
        )

    def _write_config_predicates(self, conn: sqlite3.Connection,
                                  preds: List[ConfigPredicateRecord]) -> None:
        if not preds:
            return
        rows = [
            (p.id, p.root_expr_id, p.text_form, p.z3_form,
             p.bdd_serialized, json.dumps(p.config_macros, ensure_ascii=False),
             int(p.is_unconditional), int(p.is_contradictory))
            for p in preds
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO config_predicates "
            "(id, root_expr_id, text_form, z3_form, bdd_serialized, "
            " config_macros, is_unconditional, is_contradictory) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows
        )

    def _write_conditions(self, conn: sqlite3.Connection,
                          conds: List[ConditionRecord]) -> None:
        if not conds:
            return
        rows = [
            (c.id, c.root_expr_id, c.kind, c.operator,
             c.left_expr_id, c.right_expr_id, c.text_form, c.z3_form,
             json.dumps(c.attrs, ensure_ascii=False))
            for c in conds
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO conditions "
            "(id, root_expr_id, kind, operator, left_expr_id, right_expr_id, "
            " text_form, z3_form, attrs) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows
        )

    def _write_nodes(self, conn: sqlite3.Connection, nodes: List[NodeRecord]) -> None:
        if not nodes:
            return
        rows = []
        for n in nodes:
            signature = n.attrs.get("signature", "") if n.attrs else ""
            body_text = n.attrs.get("body_text", "") if n.attrs else ""
            rows.append((
                n.id, n.kind, n.name, n.fqn, n.file_id, n.line, n.col,
                n.byte_start, n.byte_end, n.type_spelling, n.type_id,
                n.config_predicate_id, n.enclosing_symbol_id,
                signature, body_text, n.source_snippet,
                json.dumps(n.attrs, ensure_ascii=False, default=str),
                n.source_layer, n.confidence,
                n.first_seen_version, n.last_seen_version,
                n.commit_hash, n.legacy_function_id,
            ))
        conn.executemany(
            "INSERT OR REPLACE INTO cgdb_nodes "
            "(id, kind, name, fqn, file_id, line, col, byte_start, byte_end, "
            " type_spelling, type_id, config_predicate_id, enclosing_symbol_id, "
            " signature, body_text, source_snippet, "
            " attrs, source_layer, confidence, first_seen_version, "
            " last_seen_version, commit_hash, legacy_function_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows
        )

    def _write_edges(self, conn: sqlite3.Connection, edges: List[EdgeRecord]) -> None:
        if not edges:
            return
        with_id = []
        without_id = []
        for e in edges:
            attrs_json = json.dumps(e.attrs, ensure_ascii=False, default=str)
            if e.edge_id is not None:
                with_id.append((
                    e.edge_id, e.src_id, e.dst_id, e.kind, e.file_id, e.line, e.col,
                    e.byte_start, e.byte_end, e.condition_id,
                    e.config_predicate_id, e.enclosing_symbol_id, attrs_json,
                    e.source_layer, e.confidence,
                    e.first_seen_version, e.last_seen_version, e.commit_hash,
                ))
            else:
                without_id.append((
                    e.src_id, e.dst_id, e.kind, e.file_id, e.line, e.col,
                    e.byte_start, e.byte_end, e.condition_id,
                    e.config_predicate_id, e.enclosing_symbol_id, attrs_json,
                    e.source_layer, e.confidence,
                    e.first_seen_version, e.last_seen_version, e.commit_hash,
                ))
        if with_id:
            conn.executemany(
                "INSERT OR REPLACE INTO cgdb_edges "
                "(id, src_id, dst_id, kind, file_id, line, col, byte_start, byte_end, "
                " condition_id, config_predicate_id, enclosing_symbol_id, attrs, "
                " source_layer, confidence, first_seen_version, "
                " last_seen_version, commit_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                with_id
            )
        if without_id:
            conn.executemany(
                "INSERT OR REPLACE INTO cgdb_edges "
                "(src_id, dst_id, kind, file_id, line, col, byte_start, byte_end, "
                " condition_id, config_predicate_id, enclosing_symbol_id, attrs, "
                " source_layer, confidence, first_seen_version, "
                " last_seen_version, commit_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                without_id
            )

    def _write_basic_blocks(self, conn: sqlite3.Connection,
                            blocks: List[BasicBlockRecord]) -> None:
        if not blocks:
            return
        rows = [
            (b.id, b.function_id, b.block_index,
             int(b.is_entry), int(b.is_exit),
             json.dumps(b.stmt_ids), b.byte_start, b.byte_end)
            for b in blocks
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO basic_blocks "
            "(id, function_id, block_index, is_entry, is_exit, "
            " stmt_ids, byte_start, byte_end) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows
        )

    def _write_cfg_edges(self, conn: sqlite3.Connection,
                         edges: List[CFGEdgeRecord]) -> None:
        if not edges:
            return
        rows = [
            (e.function_id, e.src_block_id, e.dst_block_id,
             e.kind, e.condition_id)
            for e in edges
        ]
        conn.executemany(
            "INSERT INTO cfg_edges "
            "(function_id, src_block_id, dst_block_id, kind, condition_id) "
            "VALUES (?, ?, ?, ?, ?)",
            rows
        )

    def _write_data_flow(self, conn: sqlite3.Connection,
                         flows: List[DataFlowRecord]) -> None:
        if not flows:
            return
        rows = [
            (f.function_id, f.var_id, f.def_block_id, f.def_stmt_id,
             f.use_block_id, f.use_stmt_id, f.kind,
             json.dumps(f.path_condition_ids))
            for f in flows
        ]
        conn.executemany(
            "INSERT INTO data_flow "
            "(function_id, var_id, def_block_id, def_stmt_id, "
            " use_block_id, use_stmt_id, kind, path_condition_ids) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows
        )

    def _write_alias_sets(self, conn: sqlite3.Connection,
                          aliases: List[AliasSetRecord]) -> None:
        if not aliases:
            return
        rows = [
            (a.ptr1_node_id, a.ptr2_node_id, a.kind, a.confidence)
            for a in aliases
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO alias_sets "
            "(ptr1_node_id, ptr2_node_id, kind, confidence) "
            "VALUES (?, ?, ?, ?)",
            rows
        )

    def _write_invoke_sites(self, conn: sqlite3.Connection,
                            sites: List[InvokeSiteRecord]) -> None:
        if not sites:
            return
        rows = [
            (s.invoker_id, s.invoked_id, s.invoke_expr_id,
             json.dumps(s.arg_bindings, default=str),
             s.invoke_kind,
             json.dumps(s.dispatch_candidates))
            for s in sites
        ]
        conn.executemany(
            "INSERT INTO invoke_sites "
            "(invoker_id, invoked_id, invoke_expr_id, arg_bindings, "
            " invoke_kind, dispatch_candidates) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows
        )

    def _write_ops_bindings(self, conn: sqlite3.Connection,
                            bindings: List[OpsBindingRecord]) -> None:
        if not bindings:
            return
        rows = [
            (b.edge_id, b.ops_table_id, b.field_node_id,
             b.impl_function_id, int(b.signature_match))
            for b in bindings
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO ops_bindings "
            "(edge_id, ops_table_id, field_node_id, impl_function_id, "
            " signature_match) "
            "VALUES (?, ?, ?, ?, ?)",
            rows
        )

    def _write_sync_primitives(self, conn: sqlite3.Connection,
                                prims: List[SyncPrimitiveRecord]) -> None:
        if not prims:
            return
        rows = [
            (s.function_id, s.sync_var_id, s.kind,
             s.acquire_stmt_id, s.release_stmt_id, s.memory_order)
            for s in prims
        ]
        conn.executemany(
            "INSERT INTO sync_primitives "
            "(function_id, sync_var_id, kind, acquire_stmt_id, "
            " release_stmt_id, memory_order) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows
        )

    def _write_happens_before(self, conn: sqlite3.Connection,
                              hbs: List[HappensBeforeRecord]) -> None:
        if not hbs:
            return
        rows = [
            (h.write_event_id, h.read_event_id, h.reason, h.confidence)
            for h in hbs
        ]
        conn.executemany(
            "INSERT INTO happens_before "
            "(write_event_id, read_event_id, reason, confidence) "
            "VALUES (?, ?, ?, ?)",
            rows
        )

    def _write_includes(self, conn: sqlite3.Connection,
                        includes: List[IncludeRecord]) -> None:
        if not includes:
            return
        rows = [
            (inc.source_file_id, inc.included_file_id,
             inc.included_path, int(inc.is_system))
            for inc in includes
        ]
        conn.executemany(
            "INSERT INTO cgdb_includes "
            "(source_file_id, included_file_id, included_path, is_system) "
            "VALUES (?, ?, ?, ?)",
            rows
        )

    def _write_doc_comments(self, conn: sqlite3.Connection,
                             doc_comments: List["DocCommentRecord"]) -> None:
        if not doc_comments:
            return
        rows = []
        for dc in doc_comments:
            try:
                tags_json = json.dumps(dc.tags, ensure_ascii=False)
            except Exception:
                tags_json = "{}"
            rows.append((
                dc.node_id, dc.file_id, dc.line, dc.col,
                dc.comment_kind, dc.raw_text, dc.cleaned_text,
                tags_json, dc.byte_start, dc.byte_end,
            ))
        conn.executemany(
            "INSERT INTO doc_comments "
            "(node_id, file_id, line, col, comment_kind, raw_text, "
            " cleaned_text, tags, byte_start, byte_end) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows
        )

    def _write_metadata(self, conn: sqlite3.Connection,
                         metadata: List["MetadataRecord"]) -> None:
        if not metadata:
            return
        edge_rows = []
        node_rows = []
        for m in metadata:
            row = (m.target_id, m.key, m.value, m.value_type, m.source)
            if m.target_kind == 'edge':
                edge_rows.append(row)
            else:
                # Default to node_metadata for 'node', 'file', 'type'
                node_rows.append(row)
        if edge_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO edge_metadata "
                "(edge_id, key, value, value_type, source) "
                "VALUES (?, ?, ?, ?, ?)",
                edge_rows
            )
        if node_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO node_metadata "
                "(node_id, key, value, value_type, source) "
                "VALUES (?, ?, ?, ?, ?)",
                node_rows
            )


# ============================================================================
# Convenience: get_or_create_store
# ============================================================================

_GLOBAL_STORE: Optional[SQLiteCGDBStore] = None


def get_cgdb_store(db_path: str,
                   conn: Optional[sqlite3.Connection] = None,
                   create_schema: bool = True) -> SQLiteCGDBStore:
    """Get a SQLiteCGDBStore. If a shared connection is provided (e.g., from
    SQLiteStore), uses it; otherwise opens its own connection to db_path.
    """
    global _GLOBAL_STORE
    if _GLOBAL_STORE is None or _GLOBAL_STORE._db_path != db_path:
        _GLOBAL_STORE = SQLiteCGDBStore(db_path, conn=conn)
        if create_schema:
            _GLOBAL_STORE.create_schema()
    return _GLOBAL_STORE
