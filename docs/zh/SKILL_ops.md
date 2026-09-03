---
name: Code2Database-ops
description: "Code2Database 运维子技能。当用户询问安全图谱编辑（事务、快照、WAL 重放）、保持图谱新鲜（守护进程控制、文件监视、git hook 安装、patch-from-diff、patch-from-git、sync、merge）、profile 健康度与演化、文档-代码对齐与陈旧文档标记、图谱版本、持久记忆管理、导出（HTML、Obsidian、Web UI）、插件、embeddings（实验性）或 BUG 基准测试时激活。提供分层路由：Quick Reference 显示 23 个 Tier-1 高权重命令，路由表按问题类型映射到中权重命令组，按需部分仅列出低权重实验性命令名。数据库写入约束：LLM 在任何修改数据库的命令前必须获得用户确认（update-node、update-edge、patch-profile、apply-semantics、apply-invariants、auto-enhance、profile-evolve --apply、doc-mark-stale、ffi-types、merge-changes、tx-commit）。当 /Code2Database 检测到运维问题并显式移交，或用户输入 /Code2Database-ops 时使用。不适用于：查询图谱（用父 /Code2Database）；不适用于深度语义分析（用 /Code2Database-analysis）。"
trigger: /Code2Database-ops
parent_skill: Code2Database
---

# /Code2Database-ops

**Code2Database 运维层。** 当用户想安全地编辑图谱、保持图谱新鲜、管理 profile/文档-代码、运行导出、控制守护进程、管理事务与快照、或处理持久记忆时激活。

本子技能假设 `code2db-out/` 已存在（由父技能 `/Code2Database` 构建）。它**不**处理为洞察而查询图谱——那是父技能的工作。它**不**处理深度语义分析（并发、数据流、不变量）——那是 `/Code2Database-analysis` 的工作。

## 何时激活

- 用户显式输入 `/Code2Database-ops`
- 父技能 `/Code2Database` 检测到运维问题，并用短语 *"activate Code2Database-ops sub-skill"* 移交
- 用户询问以下任一问题：
  - "如何安全地编辑这个节点/边？" / "更新这个函数的语义"
  - "启动/停止守护进程" / "守护进程是最新的吗？"
  - "改之前先存个快照" / "回滚这个事务"
  - "检查 profile 健康度" / "演化我的 profile" / "绑定 profile 到 HEAD"
  - "文档说 X，代码做 Y——标记文档为陈旧"
  - "导出图谱到 HTML / Obsidian / Web UI"
  - "安装 git hook 自动更新"
  - "把 diff / git diff 作为图谱补丁应用"
  - "把这个 Q&A 保存到记忆" / "搜索记忆"
  - "运行 BUG 基准测试"
  - "搭建 MCP 服务器"

## 数据库写入约束（重要）

LLM 执行任何修改 code graph database的命令时，**必须先获得用户确认**。这是核心原则：**内容可缺少但必须准确**。

**需用户确认的命令**（默认会弹出 y/N 提示）：

- `update-node` — LLM 增量补充节点属性
- `update-edge` — LLM 增量补充/纠正边属性
- `patch-profile` — LLM 增量校准 auto-profile
- `apply-semantics` — 应用 LLM 填写的语义描述到图
- `classify-endpoints` — 应用 LLM 端点分类结果
- `manage-memory --action add/correct/reshape/promote/refine` — 写入持久记忆
- `save-memory` — 保存 Q&A 记忆
- `kb-rebuild-index` — 从 memory.db + brief.json 重建统一 FTS5 索引（每次 build/update 或修改后运行）
- `kb-cluster` — 聚类相似 kb 条目 + 链接 memory_qa → knowledge_principle
- `kb-migrate` — 把 kb_paragraphs 迁移到 kb_items（fact 级 + versions + provenance）
- `kb-forget --id N` — 立即删除某条 kb_paragraph（不靠 decay，写 audit_log）
- `kb-rollback --id N --to-version M` — 把 kb_item 回滚到旧版本（保留当前为版本历史）
- `apply-invariants` — 应用提取的不变量；**AMBIGUOUS 永不应用**；INFERRED 需确认；EXTRACTED 自动应用
- `auto-enhance` — LLM 自动语义增强；EXTRACTED+证据自动写入；**INFERRED 需确认**；AMBIGUOUS 拒绝
- `batch-confirm` — 批量确认待处理的 INFERRED 增强
- `profile-evolve --apply` — 应用 EXTRACTED 置信度的 profile 建议；**INFERRED 需确认**
- `doc-mark-stale` — 标记某节点文档为陈旧（非破坏性但可见）
- `ffi-types` — 更新某条 FFI 边的类型映射表
- `merge-changes` — 将变更图 JSON 合并入现有图（写入节点/边）
- `tx-commit` — 写入事务；将快照 + WAL 条目提交到活动数据库

**LLM 行为准则**：

1. 执行上述命令前，**必须**先在对话中向用户报告：
   - 要修改哪个节点/边/profile 字段
   - 旧值是什么、新值是什么
   - 信息来源（LLM 读源码推断 / 用户告知 / 文档提取）
   - 置信度（EXTRACTED / INFERRED / AMBIGUOUS）
2. 等待用户明确同意（"yes" / "确认" / "继续"）后，再调用命令
3. **禁止**使用 `--yes` / `-y` 标志绕过确认提示，除非用户在对话中明确授权
4. 如果用户拒绝，不得再次尝试同一写入

**非破坏性写入保证**：

- `update-node` 和 `update-edge` 把 LLM 补充信息存储为 `{key}_supplemented` 字段，**不覆盖**原始扫描数据
- 每条补充信息附带 `_supplement_meta`（记录 source / confidence / timestamp / original），可在 `describe-node` 输出中审计
- `apply-invariants`、`auto-enhance`、`profile-evolve` 遵循同样的补充模式；`rollback` 按时间或范围回滚
- 这保证了"数据库准确性"：原始扫描事实始终保留，LLM 增量数据可追溯、可回滚

## Tier 1 — 高权重命令（速查）

| 命令 | 用途 |
|------|------|
| `tx-begin` | 开启事务（快照 + WAL） |
| `tx-commit` | 提交当前事务（写入**需用户确认**） |
| `tx-rollback` | 回滚当前事务（恢复快照） |
| `tx-status` | 查看事务状态 |
| `daemon-start` | 启动后台守护进程（前台运行；阻塞）——inotify + 事务性同步 |
| `daemon-stop` | 停止运行中的守护进程 |
| `daemon-wait-sync` | 阻塞至当前同步完成（**重要查询前调用**） |
| `profile-health` | 计算 7 个维度 0-100 健康度评分 |
| `profile-evolve` | 检测新回调模式；`--apply` 应用 EXTRACTED 建议（INFERRED **需用户确认**） |
| `profile-bind-version` | 绑定 profile 到当前 git/svn HEAD 提交 |
| `doc-code-check` | 检查文档-代码对齐；检测返回值/参数/签名不匹配 |
| `doc-mark-stale` | 标记某节点文档为陈旧（**需用户确认**） |
| `update-node` | LLM 增量补充节点属性（**需用户确认**，非破坏性） |
| `update-edge` | LLM 增量补充边属性（**需用户确认**，非破坏性） |
| `serve` | MCP 服务器模式（stdio，81 个工具 (53 base + 28 design-report)：34 code2database_* + 19 cgdb_*） |
| `kb-rebuild-index` | 从 memory.db + brief.json 重建统一 FTS5 索引（build/update 后运行） |
| `kb-cluster` | 聚类相似 kb 条目 + 链接 principle |
| `kb-audit` | KB 审计：counts by kind / stale / low-confidence / citations |
| `kb-known-unknowns` | 列出未命中的查询（feedback loop） |
| `kb-forget` | 立即删除某条 kb 条目（不靠 decay；**需用户确认**，写 audit_log） |
| `kb-rollback` | 把 kb_item 回滚到旧版本（保留当前为版本历史） |
| `kb-conflict` | 检测同 cluster 内矛盾条目（yes/no, must/must not 等） |
| `kb-global-add` / `kb-global-search` / `kb-global-share` / `kb-global-import` | 跨项目全局 KB（~/.code2database_global_kb/） |

## 路由表 — 按问题类型分组的中权重命令

| 问题类型 | 命令序列 |
|---------|---------|
| **安全图谱编辑** | `tx-begin` → `tx-status` → `update-node` / `update-edge` / `patch-profile` / `classify-endpoints` / `auto-enhance` / `batch-confirm` / `rollback` / `fill-request` / `add-semantic-edges` / `semantic-status` / `audit-log` → `tx-commit`（带确认）→ 必要时 `tx-restore` / `tx-list-snapshots` / `tx-replay-wal` |
| **保持图谱新鲜** | `daemon-start` → `daemon-status` → `daemon-pause` / `daemon-resume` / `daemon-force-refresh` / `daemon-wait-sync` / `daemon-logs` / `daemon-reload` / `daemon-list-projects` → `daemon-stop`；或 `watch` / `sync` / `merge` / `light-scan` / `patch-from-diff` / `patch-from-git` / `install-hook` / `export-changes` / `merge-changes` |
| **profile 与文档-代码** | `profile-health` → `profile-evolve` → `profile-bind-version`；`doc-code-check` → `doc-alignment-report` → `doc-signature-diff` → `doc-mark-stale` |
| **图谱版本** | `graph-record-version` → `graph-history` → `graph-diff` |
| **记忆管理** | `save-memory --category` → `search-memory` → `manage-memory --action split/merge/move/categories` → `memory-health` → `validate-memory` |
| **导出 / 插件 / 基准** | `export-html` / `export-obsidian` / `web-ui`；`plugins` / `validate-plugin`；`bug-benchmark` |
| **Embeddings（实验性）** | `embeddings-build` → `embeddings-search` |

## 按需命令（低权重，实验性 / 罕用）

仅列出**名字**。调用前先读 `references/ops_commands.md`——仅在用户显式要求时使用。

- `embeddings-build`、`embeddings-search` — 语义嵌入（实验性）
- `extract-invariants-llm`、`intent-query`、`think-chain` — LLM 驱动扩展
- `domain` — 查看域结构（父技能中也有）
- `graph-record-version` — 记录命名图谱版本
- `unbalanced-alloc-free` — 查找不平衡的分配/释放对
- `explain-label`、`why-ambiguous` — 解释标签 / 歧义决策

## 激活移交

当检测到属于**并发、数据流、不变量、FFI、路径可行性、来源、cgdb 表**的问题时，移交给分析子技能：

> "这个问题属于深度语义分析。激活 `Code2Database-analysis` 子技能。"

当检测到属于**简单浏览、扫描、构建、一般调用关系**的问题时，移交给父技能：

> "这个问题属于基本图谱导航。激活 `Code2Database` 子技能。"

## 约束（继承自父技能）

- **事务性写入**：多步数据库修改需包裹 `tx-begin`/`tx-commit`。`patch-from-diff`/`patch-from-git` 默认已包裹；用 `--no-transaction` 绕过。`tx-rollback` 中止；`tx-replay-wal` 崩溃恢复
- **守护进程新鲜度**：重要查询前调用 `daemon-status`；若 `syncing` 或 `pending_events > 0`，调用 `daemon-wait-sync` 阻塞至同步完成。断路器在事件率超过 1000/分钟时触发整体重建
- **文档-代码对齐**：`describe-node`（父技能）暴露 `doc_code_mismatches`——若非空，`semantic_desc` 可能不可靠；查阅 `body_text` 并考虑 `doc-mark-stale` 直到文档重新提取
- **profile 演化**：`profile-evolve --apply` 只应用 EXTRACTED 置信度建议；INFERRED **需用户确认**。演化后运行 `profile-bind-version` 绑定 git/svn HEAD
- **记忆管理**：`manage-memory` 写操作（add/correct/reshape/promote/refine/split/merge/move）与 `save-memory` 需用户确认；`brief-update` 修改必载知识简报
- **MCP 服务器**：`serve` 暴露 81 个工具 (53 base + 28 design-report)（34 个 `code2database_*` + 19 个 `cgdb_*`）；无论子技能是否激活，全部可访问
- **禁止预加载** `references/ops_commands.md`——仅在需要某命令的详细语法时按需读取
- **守护进程日志**位于 `~/.code2database/daemon-<project>.log`；守护进程状态位于 `<graph_dir>/.daemon_status.json`

## 参考文档索引

| 文档 | 内容 |
|------|------|
| `references/ops_commands.md` | 所有运维命令（事务、守护进程、profile、文档-代码、导出、插件、记忆、embeddings）的完整语法 |
| `RUNTIME_CONFIG.md` | 运行时调优（invariants、auto_enhance、transactions、ffi、web_ui、benchmark、profile_health、doc_code、daemon 各节） |
| `PROFILE_MANUAL.md` | Profile 编写（skip_names、callback_detection、struct_op_types、registration_macros、domain_rules、threading_models） |
| `references/memory_knowledge.md` | 记忆和知识管理详情 |

**继承自父技能**（`/Code2Database`）：`references/usage_reference.md`、`references/label_rules.md`、`references/data_model.md`、`references/json_schema.md`、`references/usage_examples.md`。

**内部文件**（禁止加载到 agent 上下文）：`OVERVIEW.md`、`scripts/`、`config/profiles/`。这些是工具开发者的实现细节，使用时不需要。
