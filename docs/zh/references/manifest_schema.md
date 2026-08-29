# build-multi Manifest Schema (中文)

`build-multi` 命令通过 JSON manifest 文件从多个有依赖关系的项目构建统一 C2D。

## Manifest 格式

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

## 字段说明

### 顶层

| 字段 | 必填 | 说明 |
|---|---|---|
| `version` | 是 | Manifest 格式版本（当前为 `1`） |
| `projects` | 是 | 项目条目列表（见下） |
| `output` | 否 | 默认输出目录（被 `--outdir` 覆盖） |
| `jobs` | 否 | 默认并行 worker 数（被 `-j` 覆盖） |

### 项目条目

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 项目名（合法标识符：`[A-Za-z_][A-Za-z0-9_]*`）。用作 domain 前缀。 |
| `source` | 是* | 源码根目录。除非提供了 `existing_c2d`，否则必填。 |
| `include_paths` | 否 | clang `-I` 路径列表。跨所有项目聚合。 |
| `compile_commands` | 否 | 本项目的 `compile_commands.json` 路径。跨所有项目合并。 |
| `macros` | 否 | 宏定义列表（如 `["CONFIG_X=1", "DEBUG"]`）。 |
| `depends_on` | 否 | 依赖的其他项目名列表。用于拓扑排序。 |
| `existing_c2d` | 否 | 已构建好的本项目 C2D 路径。如提供且未过期，直接导入节点和边（不重扫）。 |
| `rescan_if_older_than_hours` | 否 | 若 `existing_c2d` 的 db 超过 N 小时，则重扫。 |

\* 除非提供了 `existing_c2d` 且未过期且不在 `--force-rescan` 中，`source` 必填。

## Domain 前缀强制

每个函数的 `domain` 字段被强制以项目名开头：

- `domain="root"` → `domain="<项目名>"`
- `domain="module"` → `domain="<项目名>.module"`
- `domain="<项目名>.module"` → 不变（已有前缀）

legacy 字符串 `id` 重生成为 `<项目名>_<函数名>`（小写）。
60 位 `cgdb_node_id`（SHA-256(language+FQN+signature)）因 FQN 含项目前缀而自动变化——跨项目无冲突。

## 拓扑排序

按 `depends_on` 拓扑排序。循环依赖被检测并报告。示例：
```
A depends_on B
B depends_on A
→ 错误：Circular dependency: A -> B -> A
```

## 复用模式

如提供 `existing_c2d` 且 db 未过期（mtime 在 `rescan_if_older_than_hours` 内），
节点和边通过 SQLite `ATTACH DATABASE` + `INSERT INTO ... SELECT FROM` 直接导入。
这避免重扫稳定项目。

用 `--force-rescan A,B` 强制重扫特定项目。

## CLI

```bash
python3 scripts/code2database_builder.py build-multi \
  --manifest projects.json \
  --outdir /tmp/joint_c2db-out/ \
  [-j 8] \
  [--force-rescan A,B] \
  [--no-clang]
```

## 示例 manifest（A -> B -> C 依赖）

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

构建顺序（拓扑）：C → B → A。A、B、C 的所有 include 路径被聚合到一个 `--clang-args` 字符串中。跨项目 `#include` 解析通过 `import_resolve.py` 中的 `import_map` 策略完成。
