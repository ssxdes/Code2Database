---
name: Code2Database
description: "将代码库转为可查询的代码数据库。扫描一次，永久查询——不再需要 grep/glob/Read。支持 C/C++/Go/Python/Java/Rust/ASM，调用图、条件路径、并发分析、数据流、FFI 追踪、19 个 cgdb 语义表。83 个 MCP (55 base + 28 design-report) 工具 + 260 个 CLI 命令 (252 builder + 8 scanner)。当代码问题涉及结构、调用链、影响面、并发或数据流时使用 /Code2Database。"
trigger: /Code2Database
---

# /Code2Database

**扫描一次 → 持久图 → 查询替代 grep。** 一次工具调用即可回答原本需要多次 grep/glob/Read 的问题。

## 一键式生命周期 — `c2d` 总入口

不需要记住 260 个命令。一个命令覆盖完整工作流 — 只需掌握 4 个动词：

| 动词 | 用途 | 示例 |
|------|------|------|
| `c2d setup` | 一键建库：env-check（缺件前置报出）→ 扫描 → 构建 → 派生产物 → 导出 | `c2d setup --source /path/to/project` |
| `c2d session` | 一次加载上下文：简报 + 记忆摘要 + 图状态 + 已知未知 | `c2d session` |
| `c2d ask` | 提出任意代码疑问 — 匹配的配方自动执行正确的只读命令序列并聚合输出 | `c2d ask --question "is bdev_start thread safe?"` |
| `c2d capture` | 把 Q&A 沉淀到项目记忆 | `c2d capture --question "..." --answer "..." --category bdev --author you` |
| `c2d freshen` | 新鲜度检查 → 路由到全量重建 / 守护进程 / 按文件更新 | `c2d freshen` |
| `c2d report` | 产出工件：设计文档 / 诊断 / html / mermaid / plantuml | `c2d report --kind design --module fs` |

- `c2d recipes` 列出 提问→命令 路由配方（内置 13 个；详情：`c2d recipes --recipe thread-safety`）。`c2d ask` 用 `--question` 自动分类匹配，或用 `--recipe NAME` 直接指定；无匹配时回退到单命令意图路由。
- 每一步都是普通的只读子命令，执行前先回显 — 任意动词可用 `--dry-run` 预览；`c2d ask` 还支持 `--json` 结构化摘要。
- 完整命令面保持可直接使用（见下方 Tier-1 名单；意图索引在 `references/usage_reference.md`）。

## ⚠ 强制第一步 — session-init

**在执行任何其他 C2D 命令之前，每个 AI 会话必须运行一次 `session-init`（且仅需一次）。**

```bash
python3 scripts/code2database_builder.py session-init   # --graph 自动发现 code2db-out/
# 总入口形式：c2d session
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
# 0. 首次接入项目：一键建库（env-check 缺件前置报出；重复执行安全——
#    图产物重建，memory/knowledge 保留）
python3 scripts/code2database_builder.py c2d setup --source /path/to/project

# 1. 会话启动（必须）：简报 + 前辈记忆摘要 + 图状态 + 已知未知
python3 scripts/code2database_builder.py c2d session

# 2. 提问（可重复）— 配方自动选择正确的只读命令
python3 scripts/code2database_builder.py c2d ask --question "is bdev_start thread safe?"
python3 scripts/code2database_builder.py c2d ask --recipe impact --target bdev_start

# 3. 沉淀有价值的 Q&A 到记忆（按需）
python3 scripts/code2database_builder.py c2d capture --question "..." \
    --answer "..." --category bdev --author you

# 原生命令保持可用（进阶用法）：
python3 scripts/code2database_builder.py describe --node bdev_start
python3 scripts/code2database_builder.py trace --from bdev_start --to spdk_app_start
```

## 核心命令（Tier-1）

27 个 Tier-1 命令覆盖 ~95% 的 agent 工作流。任务→命令导航：`references/usage_reference.md` 的意图索引，或运行时的 `c2d recipes` / `c2d verbs`。

- **生命周期**：`c2d`、`make`、`build`、`update`
- **查询**：`query`（Cypher；自然语言用 `intent-query`）、`describe`、`trace`、`impact`、`context`、`find`、`flow`、`concurrency`
- **记忆与知识**：`session-init`、`kb-query`、`save-memory`、`search-memory`、`knowledge-brief`、`kb-rebuild-index`、`kb-cluster`、`kb-known-unknowns`、`kb-audit`、`kb-forget`
- **服务与运维**：`serve`（MCP，83 工具）、`web-ui`、`tx-begin`、`daemon`、`health`

别名：`describe`/`context` → describe-node、`trace` → trace-chain、`find` → find-invariants、`flow` → value-flow、`concurrency` → concurrency-risks、`save` → save-memory、`recall` → search-memory、`brief` → knowledge-brief、`health` → profile-health、`daemon` → daemon-status、`export` → export-mermaid。

全部 260 个 CLI 命令仍可访问。

## 支持语言

C/C++ | Go | Python | Java | Rust | ASM（6 + ASM，C/C++ 共享扫描器）

## 提取后端

- `auto`（默认）— 有 clang 用 clang，无则 tree-sitter
- `clang` — 启用 cgdb 语义层（19 个 `cgdb_*` MCP 工具）
- `tree-sitter` — 无 libclang 依赖

## MCP 服务器

`serve --graph code2db-out/` 本地 stdio；或 `--transport http --host 0.0.0.0 --port 8765 --token SECRET --read-only` 远程模式（Bearer 认证、TLS、`--max-clients`、多客户端共享 `memory/memory.db`——一个 agent 沉淀的经验对其他 agent 立即可见）。83 工具 (55 base + 28 design-report)：36 个 `code2database_*`（含 `code2database_session_init`、`code2database_save_memory`、`code2database_kb_query`）+ 19 个 `cgdb_*`（clang 语义层）。部署模板（systemd + nginx）在 `deploy/`——仅源码仓库。

## 约束

- **会话启动**：先运行 `session-init`（别名 `init`）— 简报（强制规则/模式/坑）+ 记忆摘要（前辈经验）+ 图状态（含源码新鲜度告警——图过期先重建再信任）+ 未解答疑问，一次输出
- **纠错协议**：回答项目疑问前先 `search-memory`；答案错了用 `save-memory --correct`（原地重塑最相似条目——不产生重复变体）；缺答案用 `save-memory --category ... --author ... --symbol fn`；查询反复未命中（known-unknowns）时把答案沉淀进记忆
- **符号锚定**：记忆关于某个具体函数/类型时，传 `--symbol <name>`（可重复）——Web UI 在该符号的节点页展示这条问答，`search-memory --symbol` 可按符号过滤；合并时记忆吸收符号，`--correct` 时可重新锚定
- **沉淀触发**：(a) 解决了非平凡疑问——排查路径本身就是答案；(b) 踩了耗费真实调试时间的坑；(c) 发现简报未覆盖的强制规则/约束；(d) 纠正了错误答案（`--correct`）；(e) 回答了 session-init 中的 known-unknowns。图谱一次查询就能回答的不要存。
- `build`/`update` 或修改 memory/brief 后运行 `kb-rebuild-index`；记忆治理用 `manage-memory --action split/merge/move/compact/categories`（compact 在每次 build 后自动运行）；`brief-suggest` 建议把高权重记忆毕业进简报；简报必须精简（`brief-validate` 超过 3000 字符告警，溢出放入 memory）
- 从 `context_pack_micro` → `context_pack_lite` → `describe`/`trace` 开始；不批量读取输出文件
- 只有 7 个标签：API_entry, thread_processor, callback_func, constructor, destructor, out_end, unknown_end；边置信度 EXTRACTED / INFERRED / AMBIGUOUS
- DB 写入需用户确认；重要查询前检查 `daemon-status`（守护进程在启动宽限期 `startup_grace_active` 内持有事件而不同步）
- **精度边界**（函数级并发分析、C++ 虚派发、`build-update` 跨文件边、`--scan-subsystems`）：见 `references/usage_reference.md` 的行为细则
