---
name: Code2Database-analysis
description: "Code2Database 的深度语义分析子技能。当用户询问并发安全、数据竞争、值流、参数流、数据依赖、约束下路径可行性、不变量（前置/后置/循环不变量/状态机）、FFI 边界（Python ctypes / Go cgo / Rust extern C）、提交级来源、锁持有区域、资源分配/释放、变更影响范围，或想通过 19 个 cgdb_* MCP 工具直接查询 cgdb（代码图谱数据库）层时激活。提供分层路由：Quick Reference 显示 13 个 Tier-1 高权重命令，路由表按问题类型映射到中权重命令组，按需部分仅列出低权重实验性命令名。当 /Code2Database 检测到深度分析问题并显式移交，或用户输入 /Code2Database-analysis 时使用。不适用于：图谱构建、扫描、简单浏览（用父 /Code2Database）；不适用于事务、守护进程、profile 编辑、导出（用 /Code2Database-ops）。"
trigger: /Code2Database-analysis
parent_skill: Code2Database
---

# /Code2Database-analysis

**Code2Database 的深度语义分析层。** 当用户问题超越"谁调用谁"——涉及并发安全、数据竞争、值流、约束下路径可行性、不变量、FFI 边界、提交级来源、或直接通过 19 个 `cgdb_*` MCP 工具查询 cgdb（代码图谱数据库）层时激活。

本子技能**不**重新扫描或重建图谱。它假设 `code2db-out/` 已构建完成（通过父技能 `/Code2Database`）且图谱是新鲜的（若守护进程在运行，先调用 `daemon-status` / `daemon-wait-sync`）。

## 何时激活

- 用户显式输入 `/Code2Database-analysis`
- 父技能 `/Code2Database` 检测到深度分析问题，并用短语 *"activate Code2Database-analysis sub-skill"* 移交
- 用户询问以下任一问题：
  - "这是线程安全的吗？" / "这两条链会竞争吗？"
  - "这个 NULL / 值从哪来？"
  - "改这个函数会影响什么？"
  - "这条调用路径在这些约束下可行吗？"
  - "这个函数强制了什么不变量？"
  - "哪个 Python/Go/Rust 函数调到了 C？"
  - "哪个 commit 引入了这个 bug？"
  - "谁分配 / 释放了这个资源？"
  - "哪些函数持有这把锁？"
  - "直接查询 cgdb 表"（clang 后端——类型、CFG、数据流、ops 绑定、同步原语、配置谓词、时间旅行版本）

## Tier 1 — 高权重命令（速查）

最常使用的命令。每一条都能替代数十次 grep/Read。

| 命令 | 用途 |
|------|------|
| `concurrency-analyze` | 成对并发安全分析（线程模型 + 共享状态 + 锁分析） |
| `detect-races` | 跨线程数据竞争检测 |
| `lock-coverage` | 锁持有区域分析，事件流 + 字符位置 |
| `value-flow` | 追踪参数→返回值传播（DATA_FLOW 边） |
| `param-flow` | 跨函数追踪参数传递路径 |
| `data-dep` | 跨函数数据依赖（DATA_DEP 边；扫描所有节点） |
| `field-access` | 按字段追踪读/写访问 |
| `path-feasible` | Z3 SMT 求解路径可行性（无 Z3 时启发式回退） |
| `blast-radius` | 变更影响的 API/测试/域 |
| `find-invariants` | 按模式查找不变量（前置 / 后置 / 循环不变量 / 状态机） |
| `ffi-trace` | 追踪跨语言调用链（Python ctypes / Go cgo / Rust extern C） |
| `blame-node` | 定位引入某节点的提交 |
| `query` | Cypher 子集查询（MATCH/WHERE/RETURN），用于一次性结构化查询 |

## 路由表 — 按问题类型分组的中权重命令

当问题类型匹配下列某项时，使用所列命令序列。仅在需要详细语法时才读取参考文档（`references/analysis_commands.md`）。

| 问题类型 | 命令序列 |
|---------|---------|
| **这是线程安全的吗？** | `concurrency-risks` → `concurrency-analyze` → `detect-races` → `lock-coverage` → `happens-before` → `memory-ordering` → `who-locks` |
| **这个 NULL / 值从哪来？** | `value-flow` → `param-flow` → `data-dep` → `data-lifecycle` → `io-path` |
| **改这个会影响什么？** | `impact` → `blast-radius` → `neighbors` → `path` → `diff-chains` |
| **这条路径可行吗？** | `path-feasible` → `resolve-chain` → `extract-signals` |
| **这个函数强制了什么不变量？** | `extract-invariants` → `find-invariants` → `apply-invariants` |
| **哪个 Python/Go/Rust 函数调到 C？** | `ffi-detect` → `ffi-list` → `ffi-trace` → `ffi-types` |
| **哪个 commit 引入了这个？** | `blame-node` → `describe-commit` → `node-history` → `graph-provenance` → `find-commits` |
| **谁分配 / 释放了这个资源？** | `who-allocates` → `who-frees` → `unbalanced-alloc-free` → `add-semantic-edges` |
| **直接查询 cgdb 表（clang 后端）** | 使用 19 个 `cgdb_*` MCP 工具——见下方"cgdb MCP 工具"节 |

## cgdb MCP 工具（clang 后端 — 19 个工具）

当用户想直接查询 cgdb 层（clang 衍生的语义表）时，使用这些 MCP 工具。无论子技能是否激活，它们都可访问——MCP 与技能层分离。

| MCP 工具 | 用途 |
|---------|------|
| `cgdb_search_symbols` | FTS5 搜索 AST 节点（函数、类型、变量） |
| `cgdb_find_invokers` | 查找某函数的调用者（CGDB invoke_sites） |
| `cgdb_find_invoked` | 查找某函数的被调用者 |
| `cgdb_get_definition` | 获取某符号的定义节点 |
| `cgdb_get_function_body` | 获取函数体源码范围 |
| `cgdb_get_struct_layout` | 获取 struct 字段布局 |
| `cgdb_find_type_definition` | 按名查找类型定义 |
| `cgdb_find_ops_impls` | 查找 ops 表实现（类型化 vtable 派发） |
| `cgdb_find_cfg_paths` | 在控制流图中查找路径 |
| `cgdb_find_data_flow` | 在数据流分析中查找 def-use 链 |
| `cgdb_find_aliases` | 查找某指针的别名（stub — MVP） |
| `cgdb_find_lock_held_calls` | 查找在持有锁时发出的调用 |
| `cgdb_check_race_condition` | 检查某变量上的竞争条件 |
| `cgdb_find_configs_for` | 查找影响某节点的配置谓词 |
| `cgdb_find_nodes_under_config` | 查找某配置谓词下的所有节点 |
| `cgdb_index_status` | 显示 cgdb 索引状态（按文件、按层） |
| `cgdb_time_travel_query` | 在过去版本查询图谱 |
| `cgdb_list_versions` | 列出所有已记录的图谱版本 |
| `cgdb_get_source` | 获取节点的源文本（字节级精确归因） |

**前置条件**：仅当 `clang` 提取后端启用时（安装 libclang 后自动检测，或用 `--extraction-backend clang` 强制）才会填充 cgdb 表。在 tree-sitter-only 模式下，cgdb 表为空，这些 MCP 工具返回空结果——回退到标准 `code2database_*` MCP 工具。

## 按需命令（低权重，实验性 / 罕用）

这些命令仅列出**名字**。它们是实验性的、细分场景的或罕用的。在用户显式要求时，先读 `references/analysis_commands.md` 了解每个命令做什么，再调用。

- `think-chain` — 完整调用链思考与结论
- `intent-query` — 意图驱动的图谱查询
- `extract-invariants-llm` — LLM 驱动的不变量提取
- `explain-label` — 解释某节点为何得到某标签
- `why-ambiguous` — 解释某边为何标记为 AMBIGUOUS
- `extract-semantics` / `apply-semantics` — 从文档提取并应用语义描述
- `audit-log` — 查看历史写入审计日志

## 激活移交

当检测到属于**图谱编辑、事务、守护进程、profile/文档-代码、导出、插件、记忆、embeddings**的问题时，移交给 ops 子技能：

> "这个问题属于图谱运维 / 守护进程 / profile / 事务。激活 `Code2Database-ops` 子技能。"

当检测到属于**简单浏览、扫描、构建、一般调用关系**的问题时，移交给父技能：

> "这个问题属于基本图谱导航。激活 `Code2Database` 子技能。"

## 约束（继承自父技能）

- **全局→本地模式**：先看 `context_pack_lite` / `describe-node`，再用专用分析命令钻取
- **每次查询读取源文件不超过 10 个**
- **边置信度**：每条边带 `EXTRACTED` / `INFERRED` / `AMBIGUOUS` + 来源——绝不把 AMBIGUOUS 当事实
- **路径可行性**：仅安装 Z3 时可靠；无 Z3 时启发式回退可能误报——结果标注为临时
- **值流**：DATA_FLOW 边追踪参数→返回值传播；若某节点缺少 DATA_FLOW 边，链路断裂——不得无证据推断流
- **FFI 追踪**：需要源语言和目标语言扫描器同时检测到绑定位置
- **不变量置信度**：禁止应用 AMBIGUOUS 不变量；INFERRED 在 `apply-invariants` 前需用户确认
- **提交来源**：每个节点/边带 `commit_meta.source_commit`（git/svn 哈希）。用 `git show <hash>` 校验，而非时间戳
- **数据库写入约束**：任何修改数据库的命令（`apply-invariants`、`add-semantic-edges`、`apply-semantics`）**必须先获得用户确认**。完整协议见 `references/analysis_commands.md`
- **禁止预加载** `references/analysis_commands.md`——仅在需要某命令的详细语法时按需读取
- **cgdb clang 后端是推荐项，非必装**——tree-sitter-only 模式仍然完全可用；该模式下 cgdb_* MCP 工具返回空结果

## 参考文档索引

| 文档 | 内容 |
|------|------|
| `references/analysis_commands.md` | 50 个分析命令 + 19 个 cgdb_* MCP 工具的完整语法 |
| `references/data_model.md` | 节点/边属性、上下文包层级、cgdb 表 schema |
| `references/semantic_enhancement.md` | 语义提取和增强详情 |
| `references/endpoint_pipeline.md` | 端点分类流水线 |
| `references/cross_skill_collaboration.md` | 跨 Skill 协作协议 |
| `references/usage_examples.md` | 常见场景的查询示例 |

**继承自父技能**（`/Code2Database`）：`references/usage_reference.md`、`references/label_rules.md`、`references/json_schema.md`、`references/memory_knowledge.md`。这些位于父技能的 references 目录。

**内部文件**（禁止加载到 agent 上下文）：`OVERVIEW.md`、`scripts/`、`config/profiles/`。这些是工具开发者的实现细节，使用时不需要。
