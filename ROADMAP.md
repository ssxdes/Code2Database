# Code2Database 愿景与差距分析

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

## 四、后续路线（按优先级）

1. ~~**MCP `session_init` 工具**（低垂果实）：让 MCP 客户端会话自动拉取同一上下文~~ ✅ Round 22
2. ~~**UI 架构叙事页**：把 ARCHITECTURE_FLOWS.md 的 API→endpoint 执行流叙事嵌入 UI（当前只有文件，无界面）~~ ✅ Round 23（Arch 面板 + /api/architecture）
3. ~~**纠错闭环强化**：`save-memory --correct`（搜索相似→自动 reshape 而非新建变体），把"纠正"变成一等公民操作~~ ✅ Round 23（correct_similar + --correct）
4. ~~**记忆血缘图谱**：split/merge 谱系可视化（谁从谁拆出来/合并进谁）~~ ✅ Round 23（lineage() + CLI/UI 渲染）
5. ~~**多人协作**：memory.db 只读共享 + 按 author 过滤的 UI 视图~~ ✅ Round 23（read_only MemoryStore + authors 索引 + UI 过滤）

## 五、验证与回归约定

每项独立 commit + `PYTHONPATH=scripts python3 -m pytest tests/ -q` 全量回归；文档随代码同步（SKILL en/zh、references、AGENTS.md、CHANGELOG）。

---

# Round 24 差距分析（2026-09-04）

Round 21–23 落地后，七大目标能力（经验累积/纠错/跨会话/脚本查图/人类加载/UI 探索/新人继承）的**框架层已全部存在**。本轮审计从"目标能否在真实使用中成立"出发，发现六个**质量与闭环**缺口——不是缺功能，而是关键链路上有断点。

## 一、差距清单（按对目标的威胁排序）

| # | 缺口 | 现状证据 | 对目标的威胁 | 方案 |
|---|------|----------|--------------|------|
| G1 | **MCP 侧无 save-memory** —— 纯 MCP 接入的 agent 只能查（`memory_search`）不能存 | `mcp_server.py` 54 个 `_tool_*` 中无 save_memory | "AI 逐渐累积经验"在 MCP 接入路径上断链 | 新增 `code2database_save_memory` MCP 工具（82→83） |
| G2 | **中文检索双病灶**：① `memory_store.search` FTS 主通道 `_fts5_escape` 只提取拉丁 token，中英混合查询的中文语义被静默丢弃（纯中文查询退化为全表相似度扫描，能用但无 BM25 排序）；② `kb_index.query_kb` **无任何 fallback**，纯中文查询 `_fts5_escape` 返回 `'""'` → **零结果**（影响 kb-query CLI + knowledge_query/kb_query MCP + 统一检索） | `memory_store.py:174` / `kb_index.py:287` 均为 `re.findall(r'[A-Za-z0-9_]+')`；kb_query_kb 无 fallback 分支 | 用户以中文为主，经验检索"看似有索引实际搜不到"直接瓦解 G1/G7 | ① memory search：查询含 CJK 时 FTS+相似度双通道合并（按簇根去重取最高分）；② kb query：FTS 零结果且查询含 CJK 时退化为 `_simple_tokenize` bigram 相似度扫描 |
| G3 | **图新鲜度未闭环**：`cgdb_freshness.check_freshness()` 已存在（manifest mtime/size 比对 + git HEAD 检查），但 **session_init 不调用它**（Layer 3 只有 brief 漂移），web UI `/api/graph/summary` 也不含新鲜度 | `session_init.py` Layer 3 只比 node/edge 计数；`/api/graph/summary` 仅返回 `cache.summary()` | AI 在过期图上自信回答 = **制造错误经验**；人类看 UI 不知图已过期 | session_init Layer 3 接入 `check_freshness`（stale → hint 建议 rebuild）；`/api/graph/summary` 增加 `freshness` 字段；MCP `session_init` 自动获得 |
| G4 | **记忆与图谱无关联**：memory 无 symbol 字段；`/api/node` 不返回相关记忆；UI 符号页看不到"这个函数的坑" | memory 表无 symbols 列；`web_ui.py` get_node 无记忆注入 | 经验漂浮在代码之外——新人看代码时前辈经验不可见，违背"得到前辈经验"的核心体验 | memory 加 `symbols` JSON 列（旧库 ALTER 迁移）；`save-memory --symbol`（可重复）；`search(symbol=)`；`/api/node` 注入 `related_memories` top-3；UI 符号详情显示；MCP `memory_search` 加 symbol 参数 |
| G5 | **SKILL 无"何时存记忆"协议**：纠错协议（--correct）已协议化，但正向沉淀（什么情况该 save-memory）无触发准则，全凭 AI 自觉 | SKILL.md 仅有 correction protocol | 经验累积速率取决于模型心情，知识系统衰减为"偶尔想起来才存" | SKILL en/zh 增补 memory-capture triggers（排查成功后/踩坑后/澄清误解后/新约束发现后） |
| G6 | **kb_query_log 只记录 kb 检索**：session_init 的 known-unknowns 依赖 kb_query_log，但 memory search 未记入 → 记忆层"反复没答案的问题"不可见 | `query_kb(log_query=True)`；`MemoryStore.search` 无日志 | known-unknowns 覆盖不全，"该沉淀什么"的信号丢失一半 | `MemoryStore.search` 零结果时记入 kb_query_log（复用同表，标 kind=memory_miss） |

## 二、冗余复查（对照 Round 21 第三节）

Round 21 结论仍成立：无"必须删除"的模块。本轮复查两点：
- memory 三层（store/manager/cmd）与 facade —— 各层有独立消费者（store→MCP/UI、manager→CLI、cmd→参数解析），保留。
- 本轮新增项全部为**补断点**而非加层：G1–G6 不引入新模块，只在既有模块内补链路。

## 三、Round 24 执行计划

每项独立 commit + 全量回归 + 文档同步判断：

1. **本文档**（差距分析+方案）
2. **G2 中文检索修复**：memory 双通道合并 + kb CJK fallback（含中文查询测试）
3. **G3 新鲜度闭环**：session_init + /api/graph/summary + MCP 自动获得
4. **G4 记忆↔符号关联**：schema 迁移 + CLI/UI/MCP 三面 + related_memories
5. **G1 MCP save-memory**：82→83 工具
6. **G6 记忆检索零结果入日志**（并入 5 或独立小项）
7. **G5 SKILL 触发协议** + AGENTS/CHANGELOG/usage_reference 全量文档同步
