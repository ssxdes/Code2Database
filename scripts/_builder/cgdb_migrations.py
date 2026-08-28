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


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """v3 → v4: Add design-report L1/L3/L4 + multi-db + cross-language tables.

    This migration creates the new tables that implement the design report
    (C代码数据库化方案-分析与执行报告.md) appendices C.1 (L1), C.3 (L3),
    C.4 (L4), C.5 (multi-db routing), C.6 (cross-language bridge).

    All new tables are CREATE TABLE IF NOT EXISTS, so this is safe to run
    repeatedly. We also ALTER existing tables to add the additional columns
    expected by the report:
      - alias_sets: add function_id / ssa_value_id / alias_ssa_value_id /
        analysis columns (legacy alias_sets only had ptr1/ptr2/kind/confidence).
      - graph_versions: add sha256 / snapshot_at columns (for char-level
        consistency and history_snapshots equivalence).
      - cgdb_files: add encoding / line_ending / has_bom columns
        (legacy cgdb_files only had language/sha256/line_count/byte_count).

    For fresh v4 databases, all these tables/columns are created by the
    _CGDB_DDL + _CGDB_DDL_V4 scripts directly. This migration is only needed
    when upgrading an existing v3 database to v4.
    """
    # Run the v4-only DDL (creates new tables + new FTS5/triggers). All
    # statements use CREATE ... IF NOT EXISTS so this is safe to run on
    # partially-upgraded databases. We deliberately do NOT re-run the v3
    # _CGDB_DDL because that would re-issue CREATE INDEX statements
    # (e.g., idx_cgdb_files_hash ON cgdb_files(content_hash)) that fail
    # on v3 databases missing the content_hash column (orphan legacy dbs).
    from _builder.cgdb_schema import _CGDB_DDL_V4
    conn.executescript(_CGDB_DDL_V4)

    # ALTER existing tables to add v4 columns (idempotent via column-existence check)
    def _add_column(table: str, column: str, decl: str) -> None:
        try:
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError:
            pass  # table doesn't exist — fresh-ish db, DDL will create it

    # alias_sets: report expects function_id, ssa_value_id, alias_ssa_value_id, analysis
    _add_column("alias_sets", "function_id", "INTEGER REFERENCES ir_functions(id)")
    _add_column("alias_sets", "ssa_value_id", "INTEGER REFERENCES ssa_values(id)")
    _add_column("alias_sets", "alias_ssa_value_id", "INTEGER REFERENCES ssa_values(id)")
    _add_column("alias_sets", "analysis", "TEXT NOT NULL DEFAULT 'heuristic'")

    # graph_versions: report expects sha256 + snapshot_at (history_snapshots equivalence)
    _add_column("graph_versions", "sha256", "TEXT")
    _add_column("graph_versions", "snapshot_at", "INTEGER")

    # cgdb_files: report expects encoding / line_ending / has_bom (for L1 char-level)
    _add_column("cgdb_files", "encoding", "TEXT NOT NULL DEFAULT 'utf-8'")
    _add_column("cgdb_files", "line_ending", "TEXT NOT NULL DEFAULT 'LF'")
    _add_column("cgdb_files", "has_bom", "INTEGER NOT NULL DEFAULT 0")

    # Backfill default language_adapters rows for currently-supported languages
    # (so the table is non-empty after migration). These are best-effort
    # defaults — actual adapter presence is checked at runtime.
    adapters = [
        # (language, tier, ast_adapter, ir_adapter, ir_kind, supported_versions, coverage_level)
        ("c", "A", "libclang", "LLVMIRAdapter", "llvm-ir", '["c89","c99","c11","c17"]', "L1+L2+L3"),
        ("cpp", "A", "libclang", "LLVMIRAdapter", "llvm-ir", '["c++11","c++14","c++17","c++20"]', "L1+L2+L3"),
        ("rust", "A", "rust-analyzer", "LLVMIRAdapter", "llvm-ir", '["rust1.0+"]', "L1+L2+L3"),
        ("go", "B", "go/ast", "GoSSAAdapter", "go-ssa", '["go1.0+"]', "L1+L2+L3"),
        ("java", "B", "tree-sitter-java", "JimpleIRAdapter", "jimple", '["java8","java11","java17","java21"]', "L1+L2+L3"),
        ("python", "C", "tree-sitter-python", "PythonBytecodeAdapter", "python-bytecode", '["python3.8+"]', "L1+L2+L3-weak"),
        ("asm", "C", "regex", None, "none", '["x86_64","aarch64","riscv","loongarch","s390","powerpc","superh","mips","ia64"]', "L1+L2"),
    ]
    for lang, tier, ast_adapter, ir_adapter, ir_kind, versions, coverage in adapters:
        conn.execute(
            "INSERT OR IGNORE INTO language_adapters "
            "(language, tier, ast_adapter, ir_adapter, ir_kind, "
            " supported_versions, coverage_level) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (lang, tier, ast_adapter, ir_adapter, ir_kind, versions, coverage)
        )


# Registry of migrations: (target_version, migration_function)
MIGRATIONS: List[Tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (2, _migrate_v1_to_v2),
    (3, _migrate_v2_to_v3),
    (4, _migrate_v3_to_v4),
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
