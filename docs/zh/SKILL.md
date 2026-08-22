---
name: Code2Database
description: "将代码库转为可查询的代码数据库。扫描一次，永久查询——不再需要 grep/glob/Read。支持 C/C++/Go/Python/Java/Rust/ASM，调用图、条件路径、并发分析、数据流、FFI 追踪、19 个 cgdb 语义表。49 个 MCP 工具 + 171 个 CLI 命令。当代码问题涉及结构、调用链、影响面、并发或数据流时使用 /Code2Database。"
trigger: /Code2Database
---

# /Code2Database

**扫描一次 → 持久图 → 查询替代 grep。** 一次工具调用即可回答原本需要多次 grep/glob/Read 的问题。

## 查询优先级链

提问时，按以下优先级查询：

```
1. Memory (recall) — 之前回答过这个问题吗？→ 最快
2. Knowledge (know) — 有架构级不变式/约束记录吗？
3. Graph (query/describe/trace) — 查询代码图
4. Source (describe --code) — 最后才读源码
```

## 何时激活

- 任何关于调用关系、调用链、架构、影响面、并发的问题
- 当 `code2db-out/` 或 `code2database.db` 存在时 — 查询而非 grep
- `#ifdef` 条件路径、数据竞争、FFI 边界、数据流

## 快速开始

```bash
# 1. 扫描 + 构建（一次性）
python3 scripts/code2database_scanner.py scan --source /path --output ext.json
python3 scripts/code2database_builder.py build --extraction ext.json --outdir code2db-out/

# 2. 查询（可重复）
python3 scripts/code2database_builder.py describe --graph code2db-out/ --node bdev_start
python3 scripts/code2database_builder.py trace --graph code2db-out/ --from bdev_start --to spdk_app_start
python3 scripts/code2database_builder.py serve --graph code2db-out/  # MCP 服务器（49 工具）
```

## 核心命令（20 个）

| 命令 | 用途 | 查询层 |
|------|------|--------|
| `query` | 自然语言意图查询 | Memory→Knowledge→Graph |
| `describe` | 节点详情 + 源码片段 | Graph→Source |
| `trace` | A→B 调用链（含条件） | Graph |
| `impact` | 改了 X 会影响什么？ | Graph |
| `find` | 按模式查找（不变式、宏） | Graph |
| `flow` | 数据/值/参数流 | Graph |
| `concurrency` | 竞争/死锁检测 | Graph |
| `context` | 获取位置周围上下文 | Graph |
| `build` | 扫描 + 构建图 | — |
| `update` | 增量重扫 | — |
| `save` | 保存 Q&A 到记忆 | Memory |
| `recall` | 搜索记忆中的历史答案 | Memory |
| `know` | 查询知识库 | Knowledge |
| `serve` | 启动 MCP 服务器（49 工具） | 全部 |
| `web-ui` | 交互式浏览器（cytoscape.js） | 全部 |
| `tx-begin` | 开始事务 | Ops |
| `tx-commit` | 提交事务（渲染+编译+lint） | Ops |
| `daemon` | 后台自动同步 | Ops |
| `export` | HTML/Mermaid 可视化 | — |
| `health` | 图谱新鲜度 + profile 健康 | — |

全部 171 个 CLI 命令仍可访问；上述 20 个覆盖 ~95% 的 agent 工作流。

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

49 工具：30 个 `code2database_*`（图查询）+ 19 个 `cgdb_*`（clang 语义层）。

## 约束

- 从 `context_pack_micro` → `context_pack_lite` → `describe`/`trace` 开始
- 只有 7 个标签：API_entry, thread_processor, callback_func, constructor, destructor, out_end, unknown_end
- 边置信度：EXTRACTED / INFERRED / AMBIGUOUS
- DB 写入需用户确认
- 守护进程新鲜度：重要查询前检查 `daemon-status`
