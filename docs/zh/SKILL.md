---
name: Code2Database
description: "将代码库转为可查询的代码数据库。扫描一次，永久查询——不再需要 grep/glob/Read。支持 C/C++/Go/Python/Java/Rust/ASM，调用图、条件路径、并发分析、数据流、FFI 追踪、19 个 cgdb 语义表。82 个 MCP (54 base + 28 design-report) 工具 + 226 个 CLI 命令。当代码问题涉及结构、调用链、影响面、并发或数据流时使用 /Code2Database。"
trigger: /Code2Database
---

# /Code2Database

**扫描一次 → 持久图 → 查询替代 grep。** 一次工具调用即可回答原本需要多次 grep/glob/Read 的问题。

## 查询优先级链

提问时，按以下优先级查询：

```
1. Memory (recall / kb-query) — 之前回答过这个问题吗？→ 最快
2. Knowledge (know / kb-query) — 有架构级不变式/约束记录吗？
3. Graph (query / describe / trace) — 查询代码图
4. Source (describe --code) — 最后才读源码
```

`kb-query` 是跨越 memory + knowledge 两套存储的统一 FTS5+BM25
查询接口。`query`（Cypher）命令会自动把 top kb 命中作为 `_hints`
字段注入到图查询结果中。

## 何时激活

- 任何关于调用关系、调用链、架构、影响面、并发的问题
- 当 `code2db-out/` 或 `code2database.db` 存在时 — 查询而非 grep
- `#ifdef` 条件路径、数据竞争、FFI 边界、数据流

## 快速开始

```bash
# 0. 会话启动（必须）：加载完整项目上下文
python3 scripts/code2database_builder.py session-init --graph code2db-out/
#   → 简报 + 前辈记忆摘要 + 图状态 + 未解答问题
#   → 若无简报：brief-extract 自举模板，再用 brief-update 精炼

# 1. 扫描 + 构建（一次性）
python3 scripts/code2database_scanner.py scan --source /path --output ext.json
python3 scripts/code2database_builder.py build --extraction ext.json --outdir code2db-out/

# 2. 构建统一 KB 索引（首次构建后或 memory/brief 变更后）
python3 scripts/code2database_builder.py kb-rebuild-index --graph code2db-out/

# 3. 查询（可重复）
python3 scripts/code2database_builder.py describe --graph code2db-out/ --node bdev_start
python3 scripts/code2database_builder.py kb-query --graph code2db-out/ --query "bdev register"
python3 scripts/code2database_builder.py trace --graph code2db-out/ --from bdev_start --to spdk_app_start
python3 scripts/code2database_builder.py serve --graph code2db-out/  # MCP 服务器（82 工具 (54 base + 28 design-report)）
```

## 核心命令（24 个）

| 命令 | 用途 | 查询层 |
|------|------|--------|
| `query` | 自然语言意图查询（kb hint 输出到 stderr；`--with-hints` 包装 stdout） | Memory→Knowledge→Graph |
| `kb-query` | 跨 memory + knowledge 的统一 FTS5+BM25 查询 | Memory+Knowledge |
| `describe` | 节点详情 + 源码片段 + memory_refs + knowledge_refs | Graph→Source |
| `trace` | A→B 调用链（含条件） | Graph |
| `impact` | 改了 X 会影响什么？ | Graph |
| `find` | 按模式查找（不变式、宏） | Graph |
| `flow` | 数据/值/参数流 | Graph |
| `concurrency` | 竞争/死锁检测 | Graph |
| `context` | 获取位置周围上下文 | Graph |
| `build` | 扫描 + 构建图 | — |
| `update` | 增量重扫 | — |
| `session-init` | 一站式会话上下文：简报 + 记忆摘要 + 图状态（含过期检查）+ 未解答问题（别名：`init`） | Memory+Knowledge |
| `save-memory` | 保存 Q&A 到记忆，支持 `--category bdev/nvme/pcie` `--author`（别名：`save`） | Memory |
| `search-memory` | 搜索记忆：FTS5 + `--category/--tags/--author` 过滤（别名：`recall`） | Memory |
| `knowledge-brief` | 渲染项目简报 — 会话启动必载（别名：`brief`） | Knowledge |
| `kb-rebuild-index` | 从 memory.db + brief.json 重建 FTS5 索引 | Memory+Knowledge |
| `kb-cluster` | 聚类相似项 + 链接 principle | Memory+Knowledge |
| `kb-known-unknowns` | 列出未命中的查询（feedback loop） | Memory+Knowledge |
| `kb-audit` | 知识审计（引用、过期、置信度） | Memory+Knowledge |
| `kb-forget` | 立即删除 memory/knowledge 项 | Memory+Knowledge |
| `serve` | 启动 MCP 服务器（82 工具 (54 base + 28 design-report)） | 全部 |
| `web-ui` | 交互式浏览器（cytoscape.js） | 全部 |
| `tx-begin` | 开始事务 | Ops |
| `daemon` | 后台自动同步 | Ops |
| `health` | 图谱新鲜度 + profile 健康 | — |

全部 226 个 CLI 命令仍可访问；上述 24 个覆盖 ~95% 的 agent 工作流。

## 支持语言

C/C++ | Go | Python | Java | Rust | ASM（6 + ASM，C/C++ 共享扫描器）

## 提取后端

- `auto`（默认）— 有 clang 用 clang，无则 tree-sitter
- `clang` — 启用 cgdb 语义层（19 个 `cgdb_*` MCP 工具）
- `tree-sitter` — 无 libclang 依赖

## MCP 服务器

```bash
python3 scripts/code2database_builder.py serve --graph code2db-out/
```

82 工具 (54 base + 28 design-report)：35 个 `code2database_*`（含 `code2database_session_init` 一站式会话上下文 + `code2database_kb_query` 跨 memory+knowledge 查询）+ 19 个 `cgdb_*`（clang 语义层）。

## 约束

- **会话启动**：先运行 `session-init`（别名 `init`）— 简报（强制规则/模式/坑）+ 记忆摘要（前辈经验）+ 图状态 + 未解答问题，一次输出
- **纠错协议**：回答项目问题前先 `search-memory`；答案错了用 `save-memory --correct`（原地重塑最相似条目——不产生重复变体）；缺答案用 `save-memory --category ... --author ...`；查询反复未命中（session-init 的 known-unknowns）时把答案沉淀进记忆
- `build`/`update` 或修改 memory/brief 后运行 `kb-rebuild-index`
- Memory 是共享积累库（memory.db）：保存时带 `--category 路径/主题` + `--author`；治理用 `manage-memory --action split/merge/move/categories`
- Knowledge（brief.json）必须精简：`brief-validate` 超过 3000 字符告警；溢出内容放入 memory
- 从 `context_pack_micro` → `context_pack_lite` → `describe`/`trace` 开始
- 只有 7 个标签：API_entry, thread_processor, callback_func, constructor, destructor, out_end, unknown_end
- 边置信度：EXTRACTED / INFERRED / AMBIGUOUS
- DB 写入需用户确认
- 守护进程新鲜度：重要查询前检查 `daemon-status`；注意守护进程在启动宽限期（`startup_grace_active`）内会持有事件而不同步
- `update`/`merge`/`sync` 命令需要内存中的 nx.DiGraph。大型项目（>=5万函数）
  时 `_load_full_graph` 返回 LazySQLiteGraph（只读 SQLite 视图）。这些命令会打印
  友好错误提示使用 `daemon-start` 或 `build`。用 `daemon-start` 做增量同步。
- 并发分析（`detect-races`、`concurrency-analyze`）是函数级而非访问点级。
  TOCTOU 竞态不被检测。锁检测用 regex 而非 CFG。结果可能有误报/漏报——
  用 `lock-coverage` 做更细粒度分析。
- `path`/`trace-chain` 对不同源文件中的同名函数可能返回歧义结果。
  用 `--source-file` 消歧。传入 `--source-file` 时，`--from`/`--to`
  接受函数名（按 name+file 解析）；不传 `--source-file` 时必须用节点 ID。
  若名字在多文件中都有同名节点，会打印警告列出候选文件。
- `path --domain-filter fs,block` 硬限制遍历只在指定 domain（或 `root`）
  的节点上进行。用于跨子系统可达性查询，确保只在已知子系统集合内搜索。
  支持逗号分隔列表。
