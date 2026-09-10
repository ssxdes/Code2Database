---
name: Code2Database
description: "将代码库转为可查询的代码数据库。扫描一次，永久查询——不再需要 grep/glob/Read。支持 C/C++/Go/Python/Java/Rust/ASM，调用图、条件路径、并发分析、数据流、FFI 追踪、19 个 cgdb 语义表。83 个 MCP (55 base + 28 design-report) 工具 + 249 个 CLI 命令 (241 builder + 8 scanner)。当代码问题涉及结构、调用链、影响面、并发或数据流时使用 /Code2Database。"
trigger: /Code2Database
---

# /Code2Database

**扫描一次 → 持久图 → 查询替代 grep。** 一次工具调用即可回答原本需要多次 grep/glob/Read 的问题。

## ⚠ 强制第一步 — session-init

**在执行任何其他 C2D 命令之前，每个 AI 会话必须运行一次 `session-init`（且仅需一次）。**

```bash
python3 scripts/code2database_builder.py session-init   # --graph 自动发现 code2db-out/
```

此命令加载完整的项目知识底蕴（brief — 架构规则、hard_rules、陷阱、query_paths）+ 前辈记忆摘要 + 图状态 + 已知未知。**如果不执行此步骤，项目的知识底蕴完全不可见** — 后续所有查询都在无视强制规则和前辈经验的情况下盲操作。session-init 是唯一返回完整 brief 的命令；`query` 和 `describe` 只显示 FTS5 匹配的片段。

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
# 0. 首次接入项目：一键建库（env-check 前置校验，缺件立即报出）
python3 scripts/code2database_builder.py make --source /path/to/project
#   → 阶段 1 env-check 在任何构建步骤之前：缺 compile_commands.json /
#     libclang / tree-sitter 语法包都会预先报出（绝不中途失败）
#   → 阶段 2：扫描 -> 构建 -> 派生产物（value-flow、data-dep、
#     #ifdef 信号、FFI、简报、kb 索引、embeddings）-> 导出
#     （Obsidian 库、HTML）-> profile 健康报告
#   → make --check：只做环境校验，不构建
#   → 重复执行安全：图产物重建，memory/knowledge 保留

# 1. 会话启动（必须）：加载完整项目上下文
python3 scripts/code2database_builder.py session-init    # --graph 自动发现 code2db-out/
#   → 简报 + 前辈记忆摘要 + 图状态 + 未解答问题
#   → 若无简报：brief-extract 自举模板，再用 brief-update 精炼

# 2. 查询（可重复）
python3 scripts/code2database_builder.py describe --node bdev_start
python3 scripts/code2database_builder.py kb-query --query "bdev register"
python3 scripts/code2database_builder.py trace --from bdev_start --to spdk_app_start
python3 scripts/code2database_builder.py serve    # MCP 服务器（83 工具）
```

## 核心命令（26 个）

| 命令 | 用途 | 查询层 |
|------|------|--------|
| `query` | Cypher 子集查询（`MATCH (n:Function) WHERE n.name='foo' RETURN n.id`）。自然语言用 `intent-query` | Graph |
| `kb-query` | 跨 memory + knowledge 的统一 FTS5+BM25 查询 | Memory+Knowledge |
| `describe` | 节点详情 + 源码片段 + memory_refs + knowledge_refs（`describe-node` 的别名） | Graph→Source |
| `trace` | A→B 调用链（含条件）（`trace-chain` 的别名） | Graph |
| `impact` | 改了 X 会影响什么？ | Graph |
| `find` | 按模式查找不变式（`--var`/`--value`/`--kind`）（`find-invariants` 的别名）。查找宏用 `find-macros` | Graph |
| `flow` | 值流（DATA_FLOW/RETURN_FLOW 边）（`value-flow` 的别名）。数据依赖用 `data-dep`；参数流用 `param-flow` | Graph |
| `concurrency` | 列出并发风险对（函数级）（`concurrency-risks` 的别名）。真正的竞争检测用 `detect-races` | Graph |
| `context` | 按 ID/名称描述节点（`describe-node` 的别名）。非基于位置 | Graph |
| `make` | 一键建库：env-check（缺件前置报出）+ 扫描构建 + 全部派生产物与导出 | — |
| `build` | 扫描 + 构建图（手动，make 已封装） | — |
| `update` | 增量重扫 | — |
| `session-init` | 一站式会话上下文：简报 + 记忆摘要 + 图状态（含过期检查）+ 未解答问题（别名：`init`） | Memory+Knowledge |
| `save-memory` | 保存 Q&A 到记忆，支持 `--category bdev/nvme/pcie` `--author` `--symbol fn`（可重复，把记忆锚定到代码符号）（别名：`save`） | Memory |
| `search-memory` | 搜索记忆：FTS5 + `--category/--tags/--author/--symbol` 过滤，中文感知（别名：`recall`） | Memory |
| `knowledge-brief` | 渲染项目简报 — 会话启动必载（别名：`brief`） | Knowledge |
| `kb-rebuild-index` | 从 memory.db + brief.json 重建 FTS5 索引 | Memory+Knowledge |
| `kb-cluster` | 聚类相似项 + 链接 principle | Memory+Knowledge |
| `kb-known-unknowns` | 列出未命中的查询（feedback loop） | Memory+Knowledge |
| `kb-audit` | 知识审计（引用、过期、置信度） | Memory+Knowledge |
| `kb-forget` | 立即删除 memory/knowledge 项 | Memory+Knowledge |
| `serve` | 启动 MCP 服务器（83 工具 (55 base + 28 design-report)） | 全部 |
| `web-ui` | 交互式浏览器（cytoscape.js） | 全部 |
| `tx-begin` | 开始事务 | Ops |
| `daemon` | 显示守护进程状态（`daemon-status` 的别名；启动同步用 `daemon-start`） | Ops |
| `health` | Profile 健康评分（需要 `--source`）（`profile-health` 的别名）。图谱新鲜度用 `daemon-status` 或 `session-init` | — |

全部 249 个 CLI 命令仍可访问；上述 26 个覆盖 ~95% 的 agent 工作流。其他短别名（未列入上表）：`export` → `export-mermaid`。

## 支持语言

C/C++ | Go | Python | Java | Rust | ASM（6 + ASM，C/C++ 共享扫描器）

## 提取后端

- `auto`（默认）— 有 clang 用 clang，无则 tree-sitter
- `clang` — 启用 cgdb 语义层（19 个 `cgdb_*` MCP 工具）
- `tree-sitter` — 无 libclang 依赖

## MCP 服务器

```bash
# 本地（stdio）— 用于 Claude Desktop、Cursor 本地等
python3 scripts/code2database_builder.py serve --graph code2db-out/

# 远程（HTTP）— 跨网络访问，共享 memory/knowledge
python3 scripts/code2database_builder.py serve --graph code2db-out/ \
    --transport http --host 0.0.0.0 --port 8765 \
    --token my-secret --read-only
```

83 工具 (55 base + 28 design-report)：36 个 `code2database_*`（含 `code2database_session_init` 一站式会话上下文、`code2database_save_memory` MCP 侧经验沉淀、`code2database_kb_query` 跨 memory+knowledge 查询）+ 19 个 `cgdb_*`（clang 语义层）。

HTTP 传输（`--transport http`）让远程 MCP 客户端跨网络访问代码图谱和共享 memory/knowledge 库。全部 83 个工具可用，多个客户端共享同一个 `memory/memory.db`——一个 agent 沉淀的经验对其他 agent 立即可见。使用 `--token` 做 Bearer 认证，`--read-only` 在公开端点禁用写入工具。`deploy/` 目录（systemd + nginx 配置）仅存在于源码仓库——克隆源码仓库以获取部署模板。

## 约束

- **会话启动**：先运行 `session-init`（别名 `init`）— 简报（强制规则/模式/坑）+ 记忆摘要（前辈经验）+ 图状态（含源码新鲜度告警——图过期先重建再信任）+ 未解答问题，一次输出
- **纠错协议**：回答项目问题前先 `search-memory`；答案错了用 `save-memory --correct`（原地重塑最相似条目——不产生重复变体）；缺答案用 `save-memory --category ... --author ... --symbol fn`；查询反复未命中（session-init 的 known-unknowns）时把答案沉淀进记忆
- **符号锚定**：记忆关于某个具体函数/类型时，传 `--symbol <name>`（可重复）——Web UI 会在该符号的节点页展示这条问答，`search-memory --symbol` / `code2database_memory_search(symbol=)` 可按符号过滤。合并时记忆吸收符号，`--correct` 时可重新锚定
- **沉淀触发**（什么时候该 save-memory，让经验积累不靠运气）：(a) 解决了一个非平凡问题——排查路径本身就是答案；(b) 踩了耗费真实调试时间的坑；(c) 发现简报未覆盖的强制规则/约束；(d) 纠正了错误答案（`--correct`）；(e) 回答了 session-init 中反复出现的 known-unknowns 问题。图谱一次查询就能回答的不要存。
- `build`/`update` 或修改 memory/brief 后运行 `kb-rebuild-index`
- Memory 是共享积累库（memory.db）：保存时带 `--category 路径/主题` + `--author`；治理用 `manage-memory --action split/merge/move/compact/categories`（compact 在每次 build 后自动合并近似重复根）；`brief-suggest` 建议把高权重记忆毕业进简报
- Knowledge（brief.json）必须精简：`brief-validate` 超过 3000 字符告警；溢出内容放入 memory
- 从 `context_pack_micro` → `context_pack_lite` → `describe`/`trace` 开始
- 只有 7 个标签：API_entry, thread_processor, callback_func, constructor, destructor, out_end, unknown_end
- 边置信度：EXTRACTED / INFERRED / AMBIGUOUS
- DB 写入需用户确认
- 守护进程新鲜度：重要查询前检查 `daemon-status`；注意守护进程在启动宽限期（`startup_grace_active`）内会持有事件而不同步
- `update`/`merge`/`sync` 命令需要内存中的 nx.DiGraph。大型项目（>=5万函数）
  时 `_load_full_graph` 返回 LazySQLiteGraph（只读 SQLite 视图）。这些命令会打印
  友好错误提示使用 `daemon-start` 或 `build`。用 `daemon-start` 做增量同步，或用
  `build-update --source 源码目录 --graph 图目录` 对 SQLite 图做精确的按文件更新
  （content-hash 检测 + #include 闭包；纯格式改动按结构跳过）。
- **`build-update` 跨文件边限制**：`build-update` 只重扫变更的文件。
  当文件 A 中的函数被重命名或删除时，其他文件指向 A 旧函数的调用边会被删除
  （通过 `_delete_legacy_rows`），但**不会重建**——因为调用方文件没有被重扫，
  新的函数 ID（内嵌文件路径）不会匹配。指向变更文件的跨文件调用边在运行完整
  `build` 之前会永久丢失。对于频繁跨文件重构的项目，优先使用 `daemon-start`
  （通过守护进程的事务同步处理此场景），或定期安排完整构建。
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
- **C++ 虚函数分发未解析**：tree-sitter C++ 没有独立的 `virtual_call` 节点
  类型——虚方法调用被当作常规 `call_expression` 解析，仅解析到静态类型的方法，
  而非动态分发目标。C 风格的 ops-table vtable 分发已处理（`vtable_dispatch`
  边正确连接分发函数与注册目标）。对于 C++ 类层次结构中的 `virtual`/`override`，
  请用 `concurrency-analyze` 或手动检查 override 集合。
- **FFI 边需要 `make` 或显式 `ffi-detect`**：单独运行 `build` 命令会生成调用图，
  但不运行 FFI 检测。跨语言 FFI 桥接（Python ctypes、Go cgo、Rust extern "C"）
  由 `ffi-detect` 检测（在 `make` 流水线中自动调用），也可在 `build` 后单独运行。
  如果在多语言项目上用 `build` 而非 `make`，请在之后运行 `ffi-detect --apply`
  来添加 FFI 桥接边。
- **`--scan-subsystems` 丢失跨子系统边**：子系统过滤将扫描范围限制在顶级目录
  （如 `--scan-subsystems fs,block`）。`include/` 中的共享头文件和从被扫描
  子系统到未扫描子系统的调用会变成 phantom external 节点——调用边保留但目标
  节点未解析。如需跨子系统边界的完整调用图保真度，请省略 `--scan-subsystems`
  或将 `include` 目录加入过滤列表。
