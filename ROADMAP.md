# Code2Database 愿景与差距分析（Round 21）

> 目标定位：**让 C2D 成为代码项目的 AI 知识系统** —— AI 用它积累经验、跨会话延续上下文、通过脚本查询构建好的代码图谱而非依赖注意力随机检索；人类用它一键加载项目上下文、通过 UI 一眼理解代码业务实现、让新人直接获得前辈经验。

## 一、目标分解与现状对照

| # | 目标 | 现状 | 差距 |
|---|------|------|------|
| G1 | AI 使用中逐渐累积经验（问答入库、按主题分层） | ✅ memory.db（SQLite+FTS5，分类树 bdev/nvme/pcie，split/merge/move 治理，author 归属） | 基本达成 |
| G2 | AI 纠正纠错（错误答案被修正而非沉淀） | ⚠️ correct/reshape 存在但依赖 AI 主动知道流程；无"纠正优先"协议 | 缺协议化入口与文档 |
| G3 | 跨 agent 会话延续上下文（切换会话仍正确探索） | ⚠️ brief（Step 0）+ memory 检索存在，但分散：无一条命令同时加载简报+记忆摘要+图状态 | 缺 **session-init** 一站式入口 |
| G4 | 脚本调用图谱获取目标信息（替代 grep/注意力随机性） | ✅ 215 命令 + 81 MCP 工具 + LSP + 事务/daemon 增量 | 达成；冗余见第三节 |
| G5 | 人类指定 skill + C2D 产出即加载项目上下文 | ⚠️ `knowledge-brief` 可输出，但人类视角无默认入口；context_pack 的 knowledge_summary 读取已删除的 MD pack 文件（**回归，永远为空**） | 修复回归 + session-init |
| G6 | UI 一眼理解代码架构/业务实现 | ⚠️ web-ui 有图探索（cytoscape、节点/路径/环/社区/代码片段），但**不显示简报**（强制规则/模式/坑），**不显示记忆**（前辈 Q&A） | UI 加 Brief + Memory 面板 |
| G7 | 新人直接获得前辈经验 | ⚠️ memory 数据在，但 UI 无检索入口、会话无摘要 | 依赖 G3/G6 的落地 |
| G8 | 避免每次对话重复纠错/prompt | ⚠️ brief 硬规则承载"必须先 XXX"类约束；但"查询失败→补记忆"的反馈闭环（known-unknowns）未接入会话入口 | session-init 摘要中呈现 |

## 二、本轮（Round 21）方案

### C1 本文档（差距分析+方案，即此文件）

### C2 `session-init`：一站式会话入口（G3/G5/G7/G8）
新命令 `session-init --graph code2db-out/ [--json]`，输出四段：
1. **Project Brief**：渲染简报（缺失时提示 brief-extract）
2. **Memory Digest**：按权重取 top 活跃记忆（问题/答案摘要/分类/作者/权重）+ 记忆库统计（条数/分类数/状态分布）—— 新人看到的就是"前辈高频经验"
3. **Graph**：节点/边/域统计 + 简报图漂移检查
4. **Known Unknowns**：kb_query_log 中反复未命中的查询（"这些问题还没有答案，值得 save-memory"）+ 下一步建议（brief.query_paths）

同时修复 `index_pack` 回归：`knowledge_summary` 改读 `knowledge/brief.json`；`memory_summary` 改为从 memory.db 实时生成（原实现读磁盘 pack 文件，build 时是陈旧的）。

### C3 Web UI 加 Brief + Memory 面板（G6/G7）
- 新 API：`/api/brief`（简报 JSON+渲染文本）、`/api/memory/search?q=&top=`（FTS5 检索）、`/api/memory/stats`
- UI：顶栏新增 `Brief` 按钮（模态展示强制规则/模式/抽象/坑——新人第一眼）、`Memory` 按钮（带搜索框的 Q&A 检索面板，含分类/作者/权重）

### C4 文档同步
SKILL.md（en/zh）Step 0 改为 session-init；memory_knowledge.md / usage_reference / AGENTS.md / CHANGELOG 同步。

## 三、功能模块冗余与契合度评估

| 模块簇 | 命令数 | 评估 | 处置 |
|---|---|---|---|
| 检索（search/hybrid-search/semantic-search/ast-search/kb-query/intent-query） | 6 | 分层合理：名字→图→AST→KB→意图，各有索引；文档路由已分层 | 保留，路由文档为准 |
| 节点/上下文（describe/context/explore-flow/neighbors） | 4 | 粒度不同（详情/周围/一次探索），互补 | 保留 |
| 图谱编辑（update-node/edge、insert/delete-node、edit-token、add-function） | 8 | 部分重叠（edit-token vs update-node*）但作用于不同层（token 级 vs 属性级） | 保留，不合并（破坏面大） |
| kb-*（rebuild/query/cluster/migrate/audit/conflict/rollback/forget/global-*） | 12 | Phase1-11 演进产物；kb-migrate 与 kb_paragraphs 双轨属过渡设计 | 保留 global-*（跨项目分享刚需）；kb-migrate 标记为维护模式（文档注明） |
| cgdb-*（19 MCP + CLI） | ~30 | clang 语义层，无冗余 | 保留 |
| 导出（export-html/export-mermaid/export-obsidian/web-ui） | 4 | 格式各异（交互/文档/笔记），受众不同 | 保留 |
| 事务/daemon/快照 | 12 | 一致性与增量，无冗余 | 保留 |
| 记忆/简报（save/search/manage-memory/memory-health/validate-memory + brief×4） | 10 | Round 20 重构后职责清晰 | 本轮补 session-init |
| 微基准/实验（bug-benchmark、embeddings、LSP） | 5 | 实验/评测性质 | 保留，文档已标 experimental |

**结论**：无"必须删除"的冗余 —— 重叠命令都有层次差异且已由 Tier-1/路由文档分层；真正的缺口不在命令数量，而在**入口整合**（session-init）与**人类界面**（UI 面板）。

## 四、后续路线（本轮不做，按优先级）

1. **MCP `session_init` 工具**（低垂果实）：让 MCP 客户端会话自动拉取同一上下文
2. **UI 架构叙事页**：把 ARCHITECTURE_FLOWS.md 的 API→endpoint 执行流叙事嵌入 UI（当前只有文件，无界面）
3. **纠错闭环强化**：`save-memory --correct`（搜索相似→自动 reshape 而非新建变体），把"纠正"变成一等公民操作
4. **记忆血缘图谱**：split/merge 谱系可视化（谁从谁拆出来/合并进谁）
5. **多人协作**：memory.db 只读共享 + 按 author 过滤的 UI 视图

## 五、验证与回归约定

每项独立 commit + `PYTHONPATH=scripts python3 -m pytest tests/ -q` 全量回归；文档随代码同步（SKILL en/zh、references、AGENTS.md、CHANGELOG）。
