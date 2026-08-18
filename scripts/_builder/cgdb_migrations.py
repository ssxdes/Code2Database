"""Schema migrations for cgdb (code graph database).

Each migration function upgrades the schema from version N to N+1.
`apply_cgdb_schema` reads the current version from `meta.cgdb_schema_version`
and runs all migrations with target_version > current_version, in order.

Guidelines for writing migrations:
- Each migration MUST be idempotent (use IF NOT EXISTS / OR IGNORE).
- Each migration MUST preserve existing data (use ALTER TABLE, not DROP+CREATE).
- If a column is added with NOT NULL, supply a DEFAULT.
- Bump CGDB_SCHEMA_VERSION in cgdb_schema.py when adding a migration here.
"""
import sqlite3
import json
from typing import Callable, List, Tuple


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2: Add source snippet column for evidence traceability.

    Adds `source_snippet` to cgdb_nodes so users can read the source text
    of a node directly from the database without going back to the file.
    Also adds `description` column for LLM/heuristic-enhanced descriptions
    (separate from doc_comments which captures raw comment text).
    """
    # Add source_snippet column to cgdb_nodes (NULL for backward compat)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(cgdb_nodes)")}
    if "source_snippet" not in cols:
        conn.execute("ALTER TABLE cgdb_nodes ADD COLUMN source_snippet TEXT")
    if "description" not in cols:
        conn.execute("ALTER TABLE cgdb_nodes ADD COLUMN description TEXT DEFAULT ''")
    if "llm_confidence" not in cols:
        conn.execute("ALTER TABLE cgdb_nodes ADD COLUMN llm_confidence REAL DEFAULT 0.0")

    # Add source_snippet column to cgdb_edges (guard against partial v1 dbs)
    try:
        edge_cols = {row[1] for row in conn.execute("PRAGMA table_info(cgdb_edges)")}
        if "source_snippet" not in edge_cols:
            conn.execute("ALTER TABLE cgdb_edges ADD COLUMN source_snippet TEXT")
    except sqlite3.OperationalError:
        pass  # cgdb_edges table doesn't exist — fresh-ish db, DDL will create it

    # Add index for description search
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cgdb_nodes_description "
        "ON cgdb_nodes(description) WHERE description != ''"
    )


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """v2 → v3: Backfill commit_hash so the new NOT NULL DEFAULT 'unknown'
    constraint (applied on fresh DBs by the DDL) is satisfied for existing rows.

    SQLite cannot ALTER COLUMN to add NOT NULL to an existing column, so
    for upgraded databases we only backfill NULL values. Fresh databases
    get the strict NOT NULL DEFAULT 'unknown' from the v3 DDL.
    """
    for tbl in ("cgdb_nodes", "cgdb_edges", "cgdb_files"):
        try:
            conn.execute(
                f"UPDATE {tbl} SET commit_hash = 'unknown' "
                f"WHERE commit_hash IS NULL"
            )
        except sqlite3.OperationalError:
            # Table doesn't exist (fresh-ish db) — DDL will create it
            pass


# Registry of migrations: (target_version, migration_function)
MIGRATIONS: List[Tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (2, _migrate_v1_to_v2),
    (3, _migrate_v2_to_v3),
]


def run_migrations(conn: sqlite3.Connection, current_version: int,
                   target_version: int) -> int:
    """Run all migrations with target_version > current_version, in order.

    Returns the new version (== target_version if all succeeded).
    """
    applied = 0
    for tv, fn in MIGRATIONS:
        if tv <= current_version:
            continue
        if tv > target_version:
            break
        try:
            fn(conn)
            applied += 1
        except Exception as exc:
            # Log and abort — partial migration is bad, but better than
            # silently claiming we're at the target version.
            print(f"[cgdb_migrations] migration to v{tv} failed: {exc}",
                  flush=True)
            raise
    return current_version + applied
