# build-multi Manifest Schema

The `build-multi` command builds a unified C2D from multiple interdependent
projects via a JSON manifest file.

## Manifest format

```json
{
  "version": 1,
  "projects": [
    {
      "name": "A",
      "source": "/path/to/A",
      "include_paths": ["/path/to/A/include", "/path/to/A/src"],
      "compile_commands": "/path/to/A/build/compile_commands.json",
      "macros": ["CONFIG_A=1", "DEBUG"],
      "depends_on": ["B", "C"],
      "existing_c2d": "/path/to/A/code2db-out",
      "rescan_if_older_than_hours": 24
    }
  ],
  "output": "/tmp/joint_c2db-out",
  "jobs": 8
}
```

## Field reference

### Top-level

| Field | Required | Description |
|---|---|---|
| `version` | Yes | Manifest format version (currently `1`) |
| `projects` | Yes | List of project entries (see below) |
| `output` | No | Default output directory (overridden by `--outdir`) |
| `jobs` | No | Default parallel workers (overridden by `-j`) |

### Project entry

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Project name (valid identifier: `[A-Za-z_][A-Za-z0-9_]*`). Used as domain prefix. |
| `source` | Yes* | Source root directory to scan. Required unless `existing_c2d` is provided. |
| `include_paths` | No | List of `-I` paths for clang. Aggregated across all projects. |
| `compile_commands` | No | Path to `compile_commands.json` for this project. Merged across all projects. |
| `macros` | No | List of macro definitions (e.g., `["CONFIG_X=1", "DEBUG"]`). |
| `depends_on` | No | List of project names this project depends on. Used for topological sort. |
| `existing_c2d` | No | Path to an already-built C2D for this project. If provided and fresh, nodes/edges are imported directly (no re-scan). |
| `rescan_if_older_than_hours` | No | If `existing_c2d` db is older than N hours, re-scan instead of reusing. |

\* `source` is required unless `existing_c2d` is provided AND not expired AND
not in `--force-rescan`.

## Domain prefix enforcement

Every function's `domain` field is forced to start with the project name:

- `domain="root"` → `domain="<project_name>"`
- `domain="module"` → `domain="<project_name>.module"`
- `domain="<project_name>.module"` → unchanged (already prefixed)

The legacy string `id` is regenerated as `<project_name>_<function_name>` (lowercase).
The 60-bit `cgdb_node_id` (SHA-256 hash of language+FQN+signature) auto-changes
because the FQN includes the project prefix — so no collision across projects.

## Topological sort

Projects are topologically sorted by `depends_on`. Circular dependencies
are detected and reported with the cycle path. Example:
```
A depends_on B
B depends_on A
→ Error: Circular dependency: A -> B -> A
```

## Reuse mode

If `existing_c2d` is provided and the db is fresh (mtime within
`rescan_if_older_than_hours`), nodes and edges are imported directly
via SQLite `ATTACH DATABASE` + `INSERT INTO ... SELECT FROM`.
This avoids re-scanning stable projects.

Use `--force-rescan A,B` to force re-scan of specific projects
(e.g., when A's source has changed but `existing_c2d` is still fresh).

## CLI

```bash
python3 scripts/code2database_builder.py build-multi \
  --manifest projects.json \
  --outdir /tmp/joint_c2db-out/ \
  [-j 8] \
  [--force-rescan A,B] \
  [--no-clang]
```

## Example manifest (A -> B -> C dependency)

```json
{
  "version": 1,
  "projects": [
    {
      "name": "C",
      "source": "/src/C",
      "include_paths": ["/src/C/include"]
    },
    {
      "name": "B",
      "source": "/src/B",
      "include_paths": ["/src/B/include"],
      "depends_on": ["C"]
    },
    {
      "name": "A",
      "source": "/src/A",
      "include_paths": ["/src/A/include", "/src/A/src"],
      "compile_commands": "/src/A/build/compile_commands.json",
      "macros": ["CONFIG_A=1"],
      "depends_on": ["B", "C"]
    }
  ],
  "output": "/tmp/joint_c2db-out",
  "jobs": 8
}
```

Build order (topological): C → B → A. All include paths from A, B, C
are aggregated into one `--clang-args` string. Cross-project `#include`
resolution works via `import_map` strategy in `import_resolve.py`.
