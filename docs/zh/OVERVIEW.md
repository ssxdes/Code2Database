# Code2Database — 实现概述

> **注意**：本文档描述 Code2Database 的内部架构与算法，仅供开发该工具本身的开发者参考。**AI 代理不应加载此文件** —— 请使用 `SKILL.md` 获取用法说明。

本文档面向希望深入了解 Code2Database 内部工作机制的开发者，描述其内部架构、数据流、设计思路、代码框架与技术栈。

## 设计目标：代码数据库，不只是调用图

Code2Database 旨在回答传统调用图工具无法回答的问题：**"工程师阅读这段代码时，真正需要推理什么？"** 答案很少只是"A 调 B"。它是：

- **在什么条件下** A 调用 B？（`call_condition`——if/switch/#ifdef/三元，跨语言 `//go:build` / `#[cfg]` / `sys.platform` / `@Profile`）
- **在什么并发上下文中** 这次调用发生？（`thread_model`、`concurrency` 边属性）
- **什么状态** 被这次调用触碰？（`globals_read/written`、`fields_read/written`，每字段 SQL 原生 `field_access` 表）
- **这次调用确定吗？**（`confidence`——EXTRACTED/INFERRED/AMBIGUOUS + 证据链）
- **这次调用在当前构建配置下存在吗？**（`ifdef_conditions`、`preproc_alive`，Z3 SMT `path-feasible`）
- **如果我改它，会影响什么？**（`blast-radius`——传递影响）
- **执行是怎么到达这里的？**（`reverse-trace`——从任意节点反向 BFS）
- **哪个提交引入了它？**（`commit_meta.source_commit`——git/svn 哈希可验证，而非时间戳）
- **这两条链会竞争吗？**（`concurrency-analyze`——成对线程模型 + 锁重叠）
- **文档还跟代码对得上吗？**（`doc_code_mismatches`——返回值/参数/签名/陈旧文档）

多数工具止步于第一个问题。Code2Database 回答了全部问题，把答案持久化在图谱里，让 LLM 代理可以用一次工具调用查询，而不是跨 N 个文件 grep/glob/Read。这就是从*阅读*代码到*查询*代码的转变。

下面的架构服务于这个目标：三阶段流水线把不可变 AST 事实与可迭代推理分离；分层上下文包系统最小化 token 开销；双 tree-sitter + clang 提取后端；强类型 cgdb（代码图数据库）层提供语义表；带 WAL + 快照的事务性更新；以及保持图谱新鲜度的实时守护进程。

## 设计思路

### 为什么是三阶段编译器流水线（Profile → Scan → Build）

Code2Database 刻意分离了其他工具常混淆的三个关注点：

| 阶段 | 输入 | 输出 | 为何分离 |
|------|------|------|---------|
| **Profile** | 源代码目录 | 项目 profile JSON | 项目特定知识（回调模式、struct_op_types、导出宏、skip_names）被外置，使工具代码保持通用。新项目只需新 profile，无需改工具代码。 |
| **Scan** | 源文件 + profile | extraction.json（不可变事实） | AST 提取很慢（大型项目数分钟）。事实可验证，不应重新推导。缓存它们让构建器在数秒内迭代。 |
| **Build** | extraction.json + profile | 图 + 索引 + 上下文包 | 推理（vtable 分发、回调桥接、竞争检测、不变量）可迭代——改进某个算法只需重跑 Build，无需重扫。 |

这镜像了编译器（前端 → IR → 后端）：前端是慢、确定、项目无关的部分；后端是快、启发式、项目感知的部分。同样的分离让 Code2Database 能在不触碰扫描器的情况下增加新分析。

### 为什么是双 tree-sitter + clang 后端

C/C++ 提取后端有两种模式，服务于不同需求：

- **tree-sitter**（默认回退，无系统依赖）——对畸形代码鲁棒，统一处理 C/C++/Go/Python/Java/Rust，产生规范的遗留图（functions/edges/vtable_registrations）。无法精确解析类型。
- **clang**（可选，需 `pip install libclang==17.0.6`）——使用 libclang 的 AST 进行精确类型解析、USR 稳定节点 ID、CFG 基本块、def-use 数据流、同步原语、happens-before、强类型 vtable 分发（FieldDecl → FunctionDecl）。用 13 张强类型语义表填充 cgdb 层。

`auto` 后端（默认）两者都跑：tree-sitter 提供遗留形态数据，clang 提供 cgdb 表，`DualBackendScanner` 合并它们。若 libclang 缺失，系统优雅降级——每种支持的语言仍可扫描、构建、查询；仅 19 个 `cgdb_*` MCP 工具返回空结果。

这个设计让 Code2Database 能从快速安装（`pip install tree-sitter-c`）扩展到完整语义数据库（`pip install libclang==17.0.6`）而无需改代码。

### 为什么是三个子 skill 而不是一个

skill 以 3 个子 skill 形式发布（`/Code2Database` 核心、`/Code2Database-analysis` 深度分析、`/Code2Database-ops` 运维），让 LLM 代理只加载与当前问题相关的命令：

- **核心（15 个 Tier-1 命令）**——常驻加载。构建、浏览、基础查询（scan、build、explore-flow、describe-node、trace-chain、neighbors、path、search、key-paths 等）。
- **分析（13 个 Tier-1 + 19 个 cgdb_* MCP 工具）**——按需加载。并发、数据流、不变量、FFI、路径可行性、来源、cgdb 表。
- **运维（14 个 Tier-1 命令）**——按需加载。事务、守护进程、profile 健康、文档-代码对齐、导出、插件、记忆、嵌入。

全部 200 个 CLI 命令都通过共享的 `scripts/code2database_builder.py` 可访问，无论哪个子 skill 激活。这个拆分纯粹是为了 LLM 上下文经济：4K-token 的核心 skill 总是有用；20K-token 的分析 skill 只应在用户问及竞争或不变量时加载。

### 为什么是 micro → lite → local 查询模式

LLM token 成本主导用户体验。Code2Database 用分层上下文包系统解决这个问题：

```
micro 包（~200 token） → lite 包（~500 token） → explore-flow → describe-node → get-code-snippet
```

每一层提供更多细节，token 成本更高。代理从 micro 包（项目快照）开始，升级到 lite（结构），用 explore-flow 定位相关函数，然后下钻到具体节点。一次典型的 bug 狩猎会话在代理调用 describe-node 之前消耗 <2K token——而代理直接 grep 和 Read 源文件则需 10K+ token。

### 为什么是 cgdb（代码图数据库）层

> **命名澄清（v4+）**：下方 cgdb 层命名（CGDB-L0 至 CGDB-L11 + FTS）**与**设计报告 `C代码数据库化方案-分析与执行报告.md` 中的 L1~L4 层**是不同概念**：
> - **报告 L1**（无损重建层）：`tokens` / `macros` / `macro_invocations` / `pp_branches` / `pp_directives` / `pragmas` / `attributes` / `literals` / `string_literals` / `comments` —— schema v4 新增
> - **报告 L2**（AST 层）：`symbols` / `ast_nodes` / `references` / `call_edges` / `includes` / `globals` / `types` / `modules` —— 部分由 `cgdb_nodes` / `cgdb_edges` / `cgdb_types` / `cgdb_includes` 覆盖
> - **报告 L3**（IR 层）：`ir_functions` / `ssa_values` / `mem_accesses` / `points_to` / `indirect_calls` / `data_deps` / `path_states` —— schema v4 新增（LLVM Pass + SVF 集成属于 P1，见 `ir_adapters.py`）
> - **报告 L4**（派生层）：`call_graph_reachability` / `module_deps` / `function_embeddings` / `precise_write_sets` / `arch_metrics` / `history_snapshots` / `alignment_errors` —— schema v4 新增
> - **报告多库**：`db_routing` / `precompute_tasks` —— schema v4 新增（路由层未实现，属 P1）
> - **报告跨语言**：`cross_lang_bindings` / `type_mappings` / `ffi_call_sites` / `language_adapters` / `runtime_observations` / `dependencies` —— schema v4 新增
>
> 下方遗留 cgdb 层（CGDB-L0 至 CGDB-L11）是 clang 后端填充的原始语义表分层；上述报告层是附加（与遗留表共存于同一个 SQLite 数据库）。完整差距矩阵见 `report/Code2Database-最终差距分析与优化报告.md`。

遗留图（`functions` + `edges` 表）回答"谁调用谁"。cgdb 层增加强类型语义表，回答遗留图无法回答的问题：

| CGDB 层 | 表 | 回答 |
|---------|----|------|
| CGDB-L0 | `graph_versions` | 每提交快照，支持时间旅行查询 |
| CGDB-L1 | `cgdb_nodes`、`cgdb_files` | 多种类一等节点（function/method/ctor/dtor/var/parm/field/struct/class/enum/stmt/expr/label/namespace/template/concept/file/macro/include/vtable/ops_table）+ 文件注册表 |
| CGDB-L2 | `cgdb_types` | 独立类型系统，含 size/alignment/const/volatile/pointee |
| CGDB-L3 | `conditions` | Z3 可判定的布尔表达式树 |
| CGDB-L3.5 | `config_predicates` | `#ifdef` 谓词树（BDD + Z3 形式），跨语言（Go `//go:build`、Rust `#[cfg]`、Python `sys.platform`、Java `@Profile`、ASM/C `#ifdef`） |
| CGDB-L4 | `basic_blocks` + `cfg_edges` | 控制流图 |
| CGDB-L5 | `data_flow` + `alias_sets` | def-use 链 + 指针别名（alias_sets 是启发式 stub；报告 L3 的 `ssa_values`/`points_to`/`indirect_calls` 是完整版） |
| CGDB-L6 | （alias —— 见 CGDB-L5 / 报告 L3） | （为通过 SVF 的完整别名分析预留；当前在 CGDB-L5 `alias_sets` 表） |
| CGDB-L7 | `invoke_sites` + `ops_bindings` | 调用点精化 + 强类型 vtable 分发（FieldDecl → FunctionDecl） |
| CGDB-L8 | `sync_primitives` + `happens_before` | 并发 + 内存模型 |
| CGDB-L9 | `cgdb_includes` | `#include` 依赖图，用于增量同步 |
| CGDB-L10 | `doc_comments` + `graph_versions` | 文档注释 + 时间旅行版本查询 |
| CGDB-L11 | `node_metadata` + `edge_metadata` | 按 target 的类型化键元数据 |
| FTS | `nodes_fts` | 对 cgdb_nodes 的全文搜索（FTS5 external-content） |

加上设计报告 v4 层（附加，当完整工具链安装时由 `ir_adapters.py` 和 `source_renderer.py` 填充）：

| 报告层 | 表（v4） | 填充者 |
|--------|----------|--------|
| 报告 L1 | `tokens` / `macros` / `macro_invocations` / `pp_branches` / `pp_directives` / `pragmas` / `attributes` / `literals` / `string_literals` / `comments_freeform` + `comments_fts` | `l1_ingest.py`（P1，待实现）—— libclang Lexer raw_tokens + PPCallbacks 模拟 |
| 报告 L2 | （由 cgdb_nodes/edges/types/includes 覆盖） | 现有扫描器 |
| 报告 L3 | `ir_functions` / `ssa_values` / `mem_accesses` / `points_to` / `indirect_calls` / `data_deps` / `path_states` | `ir_adapters.py`（P1 —— LLVMIRAdapter/JimpleIRAdapter/等） |
| 报告 L4 | `call_graph_reachability` / `module_deps` / `function_embeddings` / `precise_write_sets` / `arch_metrics` / `history_snapshots` / `alignment_errors` | `l4_derive.py`（P1，待实现）—— 预计算任务 |
| 报告多库 | `db_routing` / `precompute_tasks` | `db_router.py`（P1，待实现） |
| 报告跨语言 | `cross_lang_bindings` / `type_mappings` / `ffi_call_sites` / `language_adapters` / `runtime_observations` / `dependencies` | `ffi_bridge.py`（现有）+ `ir_adapters.py` |

这些表由 clang 后端填充（遗留 cgdb 层）和 IR/L1/L4 流水线填充（报告层）。它们由 19 个 `cgdb_*` MCP 工具（遗留）+ 28 个设计报告 MCP 工具（`render_source` / `verify_consistency` / `edit_token` / `find_symbol` / `callers_of` / `indirect_targets` / `commit_db_transaction` / 等）查询。所有表共存于同一个 SQLite 数据库（`code2database.db`）。

### 为什么需要事务性更新

图修改（LLM auto-enhance、patch-from-diff、守护进程同步）需要 ACID 类保证：

- **快照**：在任何写之前，把 `code2database.db` + 关键 JSON 文件复制到 `.code2database_tx/snapshots/<id>/`。
- **WAL（写前日志）**：每次写在应用*之前*先追加到 `.code2database_tx/wal.jsonl`。写时崩溃 = 重放或回滚。
- **两阶段提交**：先 WAL（阶段 1），再应用到活 DB（阶段 2），再 checkpoint（阶段 3）。
- **文件锁**：Linux 用 `fcntl`，Windows 用 `msvcrt`——多进程协调。

`transaction()` 是上下文管理器：`with transaction(graph_dir):` 成功则提交，异常则回滚。`tx-replay-wal` 从崩溃中恢复。

### 为什么需要守护进程

`watch` 命令是一次性的；`install-hook` 只在 git 提交时触发。工程师需要一个长期运行的进程，能够：

- 实时监视源文件（Linux 用 inotify，其他平台轮询——零额外依赖）
- 批处理变更（500ms debounce + 1000ms 批窗口）
- 把更新包裹在事务中
- 自动重建输出文件（CODE2DATABASE_SUMMARY.md、上下文包等）
- 通过 `.daemon_status.json` + Unix socket 向 LLM 代理报告新鲜度
- 带熔断器（>1000 事件/分钟 → 批量重建）

守护进程通过 `pause`/`resume` socket 命令与手动更新协调，并暴露 `wait-sync` 命令——MCP 客户端在重要查询前应调用它。

## 流水线架构

```
┌──────────────────────────────────────────────────────────────────────┐
│                        PROFILE 阶段                                  │
│  输入：源代码目录                                                     │
│  输出：profile JSON（项目特定的扫描器/构建器配置）                     │
│  职责：检测项目类型，提取领域规则，                                    │
│        struct_op_types、导出宏、回调模式                              │
│  文件：scripts/_profile/schema.py, generate.py, llm_phases.py        │
│  内置 profile：linux_kernel, dpdk, spdk, qemu, zephyr,              │
│                freertos, asm_default, go/java/python/rust/_default  │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                         SCAN 阶段                                    │
│  输入：源文件 + profile 配置 + extraction_backend 选择                │
│  输出：.code2database_extraction.json（不可变事实）+                  │
│        .code2database_manifest.json（文件指纹）                       │
│  职责：从源代码中提取原始事实 — 函数定义、调用表达式、回调注册，       │
│        结构体字段赋值、#ifdef 条件，                                  │
│        cgdb_* 表（启用 clang 后端时）                                 │
│  后端：auto（双） | clang（仅 cgdb） | tree-sitter（遗留）            │
│  文件：scripts/_scanner/c_scanner.py, clang_scanner.py,             │
│        dual_scanner.py, go_scanner.py, python_scanner.py,           │
│        java_scanner.py, rust_scanner.py, asm_scanner.py,            │
│        base.py, unified_id.py, changes.py, utils.py,                │
│        config_predicates_lang.py                                    │
│  CLI：scripts/code2database_scanner.py                               │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                        BUILD 阶段                                    │
│  输入：.code2database_extraction.json + profile 配置                  │
│  输出：code2db-out/（图 + 索引 + 文档 + 上下文包 +                    │
│       code2database.db，含遗留表 + cgdb 表）                          │
│  职责：推理与图构建 — vtable 分发、                                   │
│        回调桥接、领域分类、外部代码分离、                              │
│        社区检测、端点分类、入口评分、数据竞争检测、                    │
│        lock-coverage、不变量、FFI、文档-代码、                        │
│        提交来源绑定到 git/svn HEAD                                    │
│  文件：scripts/_builder/graph_build.py（核心，7447 行），            │
│        streaming_graph.py, index_pack.py, query.py,                 │
│        entry_scoring.py, concurrency_analysis.py,                   │
│        import_resolve.py, lock_coverage.py, invariants.py,          │
│        ffi_bridge.py, doc_code_align.py, profile_health.py,         │
│        value_flow.py, data_dep.py, path_feasibility.py,             │
│        query_lang.py, commit_meta.py, transactions.py,              │
│        daemon.py, mcp_server.py, cgdb_store.py,                     │
│        cgdb_schema.py, cgdb_ingest.py, cgdb_commands.py,            │
│        cgdb_analysis.py, cgdb_ops_bind.py,                          │
│        cgdb_config_predicates.py, cgdb_incremental.py,              │
│        cgdb_versions.py, cgdb_migrations.py, cgdb_records.py,       │
│        cgdb_sync.py, sqlite_store.py, sqlite_postprocess.py,        │
│        memory_manager.py, knowledge_manager.py, semantics.py,       │
│        auto_enhance.py, web_ui.py, bug_benchmark.py 等              │
│  CLI：scripts/code2database_builder.py（146 个子命令）                │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼  （可选）
┌──────────────────────────────────────────────────────────────────────┐
│                      守护进程阶段                                     │
│  输入：源文件变更（inotify 事件或轮询）                                │
│  输出：事务性图更新 + 新鲜度标记                                       │
│  职责：监视源路径，debounce + 批处理事件，                             │
│        将更新包裹在事务中，自动重建输出文件，                          │
│        暴露 Unix socket API                                          │
│  文件：scripts/_builder/daemon.py, watcher.py                       │
│  CLI：scripts/code2database_builder.py daemon-start                  │
│  Socket：/tmp/code2database-daemon-<project>.sock                    │
│  状态：<graph_dir>/.daemon_status.json                              │
│  日志：~/.code2database/daemon-<project>.log                         │
└──────────────────────────────────────────────────────────────────────┘
```

## 关键设计原则

### 1. 事实与推理分离

最重要的架构决策：**Scan 产生不可变事实，Build 产生可迭代推理**。

- **extraction.json** 包含源代码字面所写——函数定义、直接调用、结构体字段赋值。这些是可验证的事实。
- **Build 输出** 包含派生信息——vtable 分发边（从结构体字段赋值推断）、回调桥接（2 跳解析）、领域分类等。这些可在不重扫的情况下改进。

这个分离意味着：
- 调试：如果某个调用缺失，查 extraction.json 看是扫描器漏了，还是构建器推理失败。
- 迭代：改进 vtable 解析只需重跑 Build（秒级），无需重跑 Scan（大项目分钟级）。
- 新项目：只需改 Profile，扫描器和构建器不变。

### 2. 全局到局部的查询模型

LLM 代理遵循分层查询模式以最小化 token 消耗：

```
micro 包（~200t） → lite 包（~500t） → explore-flow → describe-node → get-code-snippet
```

每一层提供更多细节，token 成本更高。代理从 micro 包获取项目快照开始，升级到 lite 获取结构，用 explore-flow 定位相关函数，然后下钻到具体节点。

### 3. 条件编译感知（跨语言）

C 扫描器用 `ifdef_conditions` 注解函数和边——一组守卫代码的 `#ifdef`/`#ifndef`/`#if` 条件。其他语言有等价构造，全部归一化到 cgdb L3.5 `config_predicates` 层：

- **C/C++/ASM**：`#ifdef` / `#ifndef` / `#if defined()` / `#elif` / `#else`
- **Go**：`//go:build` 标签 → `CONFIG_GO_TAG_<NAME>`
- **Rust**：`#[cfg(...)]` / `#[feature = "..."]` → `CONFIG_CFG_<KEY>_<VAL>` / `CONFIG_FEATURE_<NAME>`
- **Python**：`sys.platform == "linux"` / `os.name == "posix"` → `CONFIG_PY_PLATFORM_<VAL>`
- **Java**：`@Conditional(...)` / `@Profile("...")` → `CONFIG_JAVA_CONDITIONAL_<NAME>` / `CONFIG_JAVA_PROFILE_<NAME>`

这支持：
- `extract-signals`——把条件宏映射到受影响的函数/边/领域
- `resolve-chain --bindings`——只追踪在特定宏配置下存活的路径
- `diff-chains`——比较两种宏配置下的执行路径
- `cgdb_find_nodes_under_config`——列出被某个 config 谓词守卫的所有节点
- `cgdb_find_configs_for`——列出影响某个节点的 config 谓词
- `path-feasible`——Z3 SMT 在约束下的可行性（装 Z3 时是可靠的，否则启发式回退）

C 扫描器在 AST 遍历期间跟踪预处理条件栈，正确处理嵌套 `#ifdef`/`#elif`/`#else` 块及其取反。clang 后端额外为每个条件构建 Z3 SMT-LIB 形式（L3 `conditions` 表），用于可靠的路径可行性。

### 4. 并发安全分析

与多数现有工具不同，Code2Database 能通过组合以下能力检测**数据竞争**：

- **线程模型检测**：用 `thread_model` 和 `thread_entry` 注解函数（来自回调/spawn 检测——`pthread_create`、`std::thread`、`threading.Thread`、`Thread()`、`spawn`、`tokio::spawn`、goroutine `go` 语句、`CreateThread`、`_beginthread`）
- **共享状态跟踪**：用 `globals_read`/`globals_written` 和 `fields_read/written` 注解函数（SQL 原生 `field_access` 表支持 O(log n) 查询）
- **锁检测**：profile 驱动的锁获取/释放模式（无硬编码锁 API——默认为空，项目通过 `concurrency_patterns.lock_acquire_patterns` 填充）；通过 `lock-coverage` 事件流 + 字符位置精确定位锁持有区域
- **Happens-before**（cgdb L8）：`sync_primitives` + `happens_before` 表支持成对顺序检查
- **竞争检测**：在不同线程上下文中访问同一资源且无共同锁保护的函数对

命令：`concurrency-risks`（全局）、`concurrency-analyze`（成对）、`detect-races`（跨线程）、`field-access`（每资源）、`who-locks`、`lock-coverage`、`happens-before`、`memory-ordering`。

### 5. 外部代码分离

第三方和厂商代码（vendor/、third_party/、contrib/、huawei/）被自动分类到 `external_*` 领域，在所有输出中与项目领域分离：

- CODE2DATABASE_SUMMARY.md 在专门章节展示外部领域
- 上下文包分离项目 vs 外部领域数据
- 入口评分过滤掉外部函数

这防止测试工具和厂商代码污染 API 入口点检测。

### 6. 跨领域 Leiden 社区检测

标准 Leiden 社区检测产生的社区与领域划分相同。为增加价值，Code2Database：

1. 使用 `RBConfigurationVertexPartition` 调节 resolution 参数，产生比领域更少、更广的社区
2. 当 Leiden 社区 ≈ 领域时，回退到跨领域亲和度分析
3. 产生 `domain_overlap` 映射，显示哪些领域被合并到每个社区

这揭示了纯目录分组会错过的横切关注点（如 bdev + nvmf 形成存储社区）。若未安装 `python-igraph` + `leidenalg`，回退到基于领域的分组。

### 7. 非破坏性 LLM 补充

数据库写入约束是"内容可能缺失，但必须准确"。每次 LLM 驱动的写（`update-node`、`update-edge`、`apply-semantics`、`apply-invariants`、`auto-enhance`、`profile-evolve`）把补充存储为 `{key}_supplemented` 字段，**不覆盖**原始扫描数据。每个补充包含 `_supplement_meta`（来源/置信度/时间戳/原始值），可在 `describe-node` 输出中审计。`rollback` 按时间或范围回滚。原始扫描事实始终保留。

### 8. 置信度阈值自动写入

LLM 补充带置信度标签：`EXTRACTED`（带证据直接解析）、`INFERRED`（启发式，合理）、`AMBIGUOUS`（不确定）。自动写入策略：

- `EXTRACTED` + 充分证据 → 自动写入，无需确认
- `INFERRED` → 需用户确认（LLM 必须在执行前报告旧/新值、来源、置信度）
- `AMBIGUOUS` → 拒绝，永不应用

这防止 LLM 幻觉污染图谱，同时让高置信度增强无需用户摩擦地流通。

## 数据模型

### 节点属性

调用图中每个节点都有：

| 属性 | 类型 | 描述 |
|------|------|------|
| `id` | str | 全限定：`project.domain.file.function`（跨语言统一 ID：SHA-256 截断到 60 位 + 语言前缀，见 `unified_id.py`） |
| `name` | str | 函数名 |
| `domain` | str | 架构领域（如 `lib.bdev`） |
| `source_file` | str | 源文件路径 |
| `line` | int | 定义行号 |
| `signature` | str | 函数签名 |
| `labels` | list[str] | 之一：`API_entry`、`thread_processor`、`callback_func`、`constructor`、`destructor`、`out_end`、`unknown_end` |
| `is_empty` | bool | 条件分支聚合节点 |
| `is_external` | bool | 是否在外部代码领域 |
| `ifdef_conditions` | list[str] | 守卫此函数的 `#ifdef` 条件 |
| `thread_model` | str | 线程模型（来自 spawn/回调检测） |
| `thread_entry` | bool | 是否为线程入口点 |
| `entry_score` | float | 多因子入口评分 |
| `globals_read/written` | list[dict] | 访问的全局变量 |
| `fields_read/written` | list[dict] | 访问的结构体字段（含 struct_chain + field_name） |
| `commit_meta` | dict | 扫描时 git/svn HEAD 的 `{source_commit, author, date, message}` |
| `preconditions` | list[str] | 入口处必须成立的条件（从函数体提取） |
| `postconditions` | list[str] | 出口处保证的条件 |
| `loop_invariants` | list[str] | 函数体中循环维持的不变量 |
| `state_machine` | dict | 赋值计数 >= 1 时的 `{state_var, states, transitions}` |
| `doc_stale` | bool | 检测到文档/代码不匹配时由 `doc-mark-stale` 设置 |
| `doc_stale_reason` | str | 陈旧原因 |
| `ffi_binding` | dict | FFI 桥接节点上有：`{source_lang, target_lang, binding_type}` |
| `_supplement_meta` | dict | LLM 应用补充的来源（source/confidence/timestamp/original） |

### 边属性

| 属性 | 类型 | 描述 |
|------|------|------|
| `relation` | str | `INVOKES`、`CONTAINS`、`IMPORTS`、`DATA_FLOW`、`DATA_DEP`、`FFI_BRIDGE`、`IMPLEMENTS`、`OPS_BIND` |
| `call_order` | int | 在调用者调用序列中的位置 |
| `call_condition` | str | 此调用发生的条件（如 `SPDK_CONFIG_APP_RW`） |
| `concurrency` | str | `vtable_dispatch`、`field_dispatch`、`callback`、`spawn_target`、`thread_spawn`、`goroutine` |
| `confidence` | str | `EXTRACTED`、`INFERRED`、`AMBIGUOUS` |
| `evidence` | str | 该边的人类可读理由 |
| `commit_meta` | dict | 与节点 `commit_meta` 相同的 schema |
| `data_flow_var` | str | `DATA_FLOW` 边传播的变量名 |
| `data_flow_kind` | str | `param_to_return`、`param_to_param`、`return_to_param` |
| `data_dep_kind` | str | `read_after_write`、`write_after_read`、`write_after_write` |
| `lock_event` | dict | 用于 `lock-coverage`：`{kind: acquire/release, lock_var, char_pos, line, col}` |
| `ffi_marshalling` | dict | 用于 `FFI_BRIDGE` 边：`{src_type, target_type, lossy, conversion_note}` |

### 上下文包分层

| 层 | 目标 token | 内容 |
|----|-----------|------|
| **micro** | ~200 | 项目名、架构一句话、模式关键词、前 3 个领域/API、统计 |
| **lite** | ~500 | micro + 领域图、完整 API 目录、线程/回调入口、核心数据流 |
| **standard** | ~1500 | lite + 全部领域、执行流程、并发摘要 |
| **full** | 完整 | 一切：社区图、所有场景、数据流索引、hub 函数 |

### cgdb 层表（启用 clang 后端时）

| 层 | 表 | 用途 |
|----|----|------|
| L0 | `graph_versions` | 每提交快照，支持时间旅行查询 |
| L1 | `cgdb_nodes`、`cgdb_files` | 多种类一等节点 + 文件注册表 |
| L2 | `cgdb_types` | 独立类型系统（builtin/pointer/reference/array/record/enum/function/template/typedef） |
| L3 | `conditions` | Z3 SMT-LIB 布尔表达式树 |
| L3.5 | `config_predicates` | `#ifdef` 谓词树（BDD + Z3 形式），跨语言 |
| L4 | `basic_blocks`、`cfg_edges` | 控制流图 |
| L5 | `data_flow`、`alias_sets` | def-use 链 + 指针别名 |
| L7 | `invoke_sites`、`ops_bindings` | 调用点精化 + 强类型 vtable 分发 |
| L8 | `sync_primitives`、`happens_before` | 并发 + 内存模型 |
| FTS | `nodes_fts` | FTS5 虚拟表，用于符号全文搜索 |

完整 schema 见 `references/data_model.md`。

## 代码框架

Code2Database 在 `scripts/` 下组织成 5 个包，外加 CLI 入口层。总计约 70K 行 Python。

### 包布局

```
scripts/
├── code2database_builder.py      ← CLI 入口（146 个子命令，argparse 路由）
├── code2database_scanner.py      ← 扫描器 CLI 入口（8 个子命令）
├── setup.sh                      ← 依赖安装器（支持按语言安装）
├── requirements.txt              ← 锁定依赖
├── check_docs_sync.py            ← 文档-代码同步校验器
├── iterate_precision.py          ← 精度迭代助手
├── run_evals.py                  ← 评测运行器（evals/evals_*.json）
├── verify_edge_attribution.py    ← 边归因校验器
├── _vendor/                      ← 内置 shim（networkx 回退）
│
├── _scanner/                     ← 语言特定 AST 扫描器（12K 行）
│   ├── base.py                   ← BaseScanner ABC：scan_file、_extract、_walk 模式、
│   │                                config 谓词注解、cgdb 记录发射、
│   │                                并发信息检测、文档注释提取、
│   │                                import/include 提取、condition Z3 形式生成、
│   │                                同步原语模式检测（按语言）
│   ├── unified_id.py             ← 跨语言节点 ID：SHA-256 截断到 60 位，
│   │                                高位清零以适配 SQLite 有符号 INTEGER，加语言前缀
│   ├── changes.py                ← 文件指纹（mtime_ns:size）、manifest 保存/加载、
│   │                                detect_changes 用于增量更新
│   ├── utils.py                  ← MIN_PYTHON=(3,8)、EXTENSION_MAP、LANG_EXTENSIONS、
│   │                                classify_domain
│   ├── c_scanner.py              ← C/C++ tree-sitter 扫描器（最大，2992 行）：
│   │                                预处理 #ifdef 栈、vtable_registrations 提取、
│   │                                宏分发、回调模式、fn_ptr_calls、
│   │                                C++ 模板/concepts/coroutines、MSVC __asm 块
│   ├── clang_scanner.py          ← libclang 后端（1136 行）：USR 稳定 ID、强类型节点、
│   │                                完整 cgdb_* 表填充（L1-L8）、compile_commands.json
│   ├── dual_scanner.py           ← DualBackendScanner：合并 tree-sitter + clang 输出
│   ├── go_scanner.py             ← Go tree-sitter：goroutine 检测、//go:build、方法、goto
│   ├── python_scanner.py         ← Python tree-sitter：类、装饰器、sys.platform、match
│   ├── java_scanner.py           ← Java tree-sitter：包、import、方法、@Profile
│   ├── rust_scanner.py           ← Rust tree-sitter：impl/trait、#[cfg]、pub 可见性、async
│   ├── asm_scanner.py            ← ASM 正则扫描器（2784 行）：NASM x86_64 + GNU as AT&T +
│   │                                ARM/AArch64/RISC-V/LoongArch/s390/PowerPC/SuperH/MIPS/IA-64，
│   │                                内核 SYM_FUNC_START/ENTRY/EXPORT_SYMBOL 宏、
│   │                                寄存器数据流跟踪、系统调用号解析
│   └── config_predicates_lang.py ← 跨语言 config 谓词提取：
│                                    Go //go:build、Rust #[cfg]、Python sys.platform、
│                                    Java @Profile/@Conditional、ASM/C #ifdef
│
├── _detector/                    ← 检测模块（1.8K 行）
│   ├── build_detector.py         ← 构建系统检测：CMake、Make、Meson、Autotools、
│   │                                Kconfig、Bazel、MSBuild、spec。提取 -D 宏、目标、
│   │                                include 目录。evaluate_pp_condition 用于 #ifdef 解析
│   ├── community_detector.py     ← Leiden 社区检测（igraph + leidenalg），含
│   │                                RBConfigurationVertexPartition + 跨领域亲和度，
│   │                                库不可用时回退到领域分组
│   └── framework_detector.py     ← 框架检测：Django/Flask/FastAPI/Spring/Gin/Echo/
│                                    Actix/Rocket/Tokio/Qt/GTK/libevent/libuv 等
│
├── _profile/                     ← 项目 profile 系统（4.8K 行）
│   ├── schema.py                 ← ProfileSchema：load/validate/merge/to_scanner_config/
│   │                                to_builder_config。_DEFAULT_PROFILE 以 Python dict 内嵌
│   ├── generate.py               ← 自动 profile 生成：预扫描、测试扫描、auto-config、
│   │                                auto-detect 阶段。SourceInfoCollector 单次 os.walk
│   └── llm_phases.py             ← LLM 驱动的 Phase 4（头文件分析）+ Phase 6（结果检查）
│
├── _builder/                     ← 图构建和查询模块（54K 行，70 个文件）
│   ├── __init__.py               ← 懒加载机制（首次访问才加载模块）
│   ├── graph_build.py            ← 核心图构建（7447 行）：build_graph、cmd_build、
│   │                                领域拆分、提交哈希检测、测试领域检测、
│   │                                cgdb 擦除重建
│   ├── streaming_graph.py        ← StreamingGraph：NetworkX 兼容 API，流式写入
│   │                                SQLite，适合低内存构建（1.4M 节点 ~1.9GB RAM）
│   ├── sqlite_store.py           ← SQLiteStore：WAL 日志、64MB 缓存、mmap 256MB、
│   │                                schema 迁移 v1→v6、field_access + global_access 表
│   ├── sqlite_postprocess.py     ← 构建索引、CODE2DATABASE_SUMMARY.md、领域 README、
│   │                                场景、上下文包、架构流程（从 SQLite）
│   ├── cgdb_schema.py            ← cgdb 强类型语义 schema DDL，CGDB_SCHEMA_VERSION=4，幂等
│   ├── cgdb_migrations.py        ← Schema 演进（原地 ALTER TABLE，保留数据）
│   ├── cgdb_records.py           ← Dataclass 记录：NodeRecord、EdgeRecord、TypeRecord、
│   │                                ConfigPredicateRecord、BasicBlockRecord、DataFlowRecord 等
│   ├── cgdb_store.py             ← CGDBWriter + CGDBReader ABC，SQLiteCGDBStore 实现
│   ├── cgdb_ingest.py            ← 批量导入 IngestBatch 到 cgdb 表
│   ├── cgdb_sync.py              ← 同步遗留 functions/edges ↔ cgdb_nodes/cgdb_edges
│   ├── cgdb_incremental.py       ← 按文件增量 cgdb 更新（删除 + 重插）
│   ├── cgdb_versions.py          ← graph_versions 时间旅行：记录版本、diff 版本
│   ├── cgdb_ops_bind.py          ← 强类型 vtable 分发：FieldDecl → FunctionDecl 绑定
│   ├── cgdb_config_predicates.py ← ConfigPredicate：BDD + Z3 形式，UNCONDITIONAL、CONTRADICTORY
│   ├── cgdb_analysis.py          ← 竞争检测、锁持有调用、cgdb 表上的 CFG 路径
│   ├── cgdb_commands.py          ← 18+ 个 cgdb_* CLI 命令（cgdb-query、cgdb-time-travel 等）
│   ├── index_pack.py             ← 上下文包生成（micro/lite/standard/full）、
│   │                                hub 函数检测、场景计算、
│   │                                Mermaid 路径图（2608 行）
│   ├── query.py                  ← 查询命令：describe-node、trace-chain、reverse-trace、
│   │                                diff-chains、blast-radius、field-access、param-flow、
│   │                                describe-commit、node-history、graph-provenance、blame-node
│   ├── explore.py                ← 一次性 explore-flow 查询引擎（关键词 → 相关子图）
│   ├── key_paths.py              ← 关键路径自动提取（入口 → hub → 端点）
│   ├── search_cmd.py             ← cmd_load、cmd_search、cmd_path、cmd_neighbors、cmd_impact、cmd_domain
│   ├── entry_scoring.py          ← 多因子入口评分，含测试函数过滤
│   ├── concurrency_analysis.py   ← 数据竞争检测 + 成对并发安全分析
│   ├── concurrency.py            ← 并发风险列举 + 数据生命周期追踪
│   ├── memory_ordering.py        ← 内存序：原子操作、屏障、READ_ONCE/WRITE_ONCE、smp_mb
│   ├── import_resolve.py         ← 多策略被调用者解析 + FQN 计算
│   ├── token_budget.py           ← Token 估算与预算感知截断
│   ├── memory_manager.py         ← 持久化 Q&A 记忆，带衰减（root/leaf、scratch）
│   ├── memory_cmd.py             ← 记忆 CLI：save/search/validate
│   ├── memory_guard.py           ← 记忆预算强制 + 自动合并
│   ├── knowledge_manager.py      ← 知识提取、应用、查询、校验
│   ├── semantics.py              ← 语义描述提取/应用、
│   │                                classify-endpoints、think-chain、extract-signals
│   ├── auto_enhance.py           ← LLM 自动语义增强，置信度阈值写入、
│   │                                批量确认、回滚日志（1454 行）
│   ├── invariants.py             ← 提取 preconditions/postconditions/loop_invariants/state_machine
│   ├── llm_invariants.py         ← LLM 驱动的不变量提取
│   ├── plugins.py                ← 插件加载与执行
│   ├── patcher.py                ← 增量打补丁（patch-from-diff、patch-from-git、light-scan）
│   ├── update_sync.py            ← update、sync、merge 操作
│   ├── update_cmd.py             ← update-node、update-edge、patch-profile（LLM 补充）
│   ├── changelog_update.py       ← quick-update、export-changes、merge-changes、semantic-status
│   ├── export.py                 ← HTML + Obsidian 导出
│   ├── visualizer.py             ← 图可视化渲染
│   ├── web_ui.py                 ← 单文件 HTML/SVG/JS 交互查看器；HTTP 服务器
│   ├── bug_benchmark.py          ← GraphInvestigator vs GrepInvestigator 召回/精确
│   ├── profile_health.py         ← 7 类 0-100 评分；演进建议；HEAD 绑定
│   ├── doc_code_align.py         ← 检测返回/参数/签名/陈旧文档不匹配
│   ├── commit_meta.py            ← Git/svn 提交检测、blame、manifest 富化
│   ├── graph_history.py          ← graph-history、graph-diff、graph-record-version
│   ├── audit_log.py              ← 过去写图的审计日志
│   ├── ffi_bridge.py             ← 检测 ctypes/cgo/extern "C"；构建 FFI_BRIDGE 边（948 行）
│   ├── value_flow.py             ← DATA_FLOW 边构建；参数→返回值传播
│   ├── lock_coverage.py          ← 锁持有事件流提取，带字符位置
│   ├── path_feasibility.py       ← Z3 SMT 编码；无 Z3 时启发式回退
│   ├── data_dep.py               ← DATA_DEP 边；扫描所有节点找读者/写者
│   ├── intent_router.py          ← 自然语言意图 → 图操作
│   ├── query_router.py           ← 查询路由（按问题类型选命令）
│   ├── query_lang.py             ← Cypher 子集解析器（MATCH/WHERE/RETURN，1304 行）
│   ├── query_cache.py            ← 查询结果缓存
│   ├── transactions.py           ← WAL + 快照 + fcntl 文件锁；transaction() 上下文
│   ├── daemon.py                 ← inotify + 轮询；熔断器；事务性同步；
│   │                                socket API（1400 行）
│   ├── watcher.py                ← 文件变更监视器，用于自动更新
│   ├── update_sync.py            ← cmd_merge、cmd_update、cmd_sync
│   ├── embeddings.py             ← TF-IDF 字符 n-gram 嵌入，用于语义搜索
│   ├── explain.py                ← explain-label、why-ambiguous
│   ├── semantic_edges.py         ← who-allocates、who-frees、unbalanced-alloc-free、who-locks、
│   │                                add-semantic-edges
│   ├── logging_utils.py          ← 结构化日志（configure_logging、get_logger）
│   ├── mcp_server.py             ← MCP 服务器（stdio 传输，53 个工具：34 code2database_* + 19 cgdb_*）
│   ├── kb_index.py               ← 统一 KB FTS5+BM25 索引（kb_paragraphs 表，跨 memory+knowledge 查询）
│   ├── kb_cluster.py             ← KB 聚类（union-find on FTS5 similarity，scope_id/canonical_id/principle_ref）
│   ├── kb_global.py              ← 跨项目全局 KB（~/.code2database_global_kb/global.db，跨项目复用知识）
│   ├── kb_audit.py               ← KB 审计（counts by kind、stale、low-confidence、citations、audit_log 接入）
│   ├── kb_conflict.py
│   ├── build_multi.py             ← Multi-project aggregate build (manifest-driven, project-name domain prefix)
│   ├── c2d_foreign.py             ← Cross-C2D foreign_refs + watched_c2ds (add/sync/list/remove)
│   ├── c2d_phase2.py              ← Composite query + check-compat + coverage-cross-c2d
│   ├── c2d_phase3.py              ← Vendor stub + FFI auto-link + RPC scan + cross-team knowledge            ← KB 冲突检测（同 cluster 内矛盾词对）+ rollback + forget
│   ├── validate.py               ← 图校验
│   └── utils.py                  ← 共享构建器工具（_normalize_id、_resolve_invoked_id、
│                                    _find_node_id、_parse_bindings、_load_globals、
│                                    _ensure_mutable_graph 等）
│
├── config/
│   ├── profiles/                 ← 内置项目 profile（不要加载进上下文）
│   │   ├── _default.json         ← 通用默认（skip_names、回调后缀）
│   │   ├── linux_kernel.json     ← Linux 内核（Kconfig、file_operations、EXPORT_SYMBOL）
│   │   ├── dpdk.json             ← DPDK（rte_* API、TAILQ、rte_atomic）
│   │   ├── spdk.json             ← SPDK（bdev、rpc、spdk_* API）
│   │   ├── qemu.json             ← QEMU（QOM、object_class、trace events）
│   │   ├── zephyr.json           ← Zephyr RTOS（device、kernel、Kconfig）
│   │   ├── freertos.json         ← FreeRTOS（xTask、xQueue、xSemaphore）
│   │   ├── asm_default.json      ← ASM 默认（NASM + GAS + ARM + RISC-V）
│   │   ├── go_default.json       ← Go 默认（goroutine、channel、interface）
│   │   ├── java_default.json     ← Java 默认（Spring、Thread、ExecutorService）
│   │   ├── python_default.json   ← Python 默认（threading、asyncio、ctypes）
│   │   └── rust_default.json     ← Rust 默认（tokio、std::sync、extern "C"）
│   └── runtime.json              ← 运行时配置（scan/build/query/memory/semantic 段）
│
└── hooks/
    └── post-commit               ← Git post-commit 钩子模板（自动 quick-update）
```

### 模块耦合

包之间松耦合：

- `_scanner` 依赖 `_profile`（profile 驱动的回调模式）和 `_detector.build_detector`（`evaluate_pp_condition`）
- `_builder` 依赖 `_scanner`（patch-from-diff 时重扫）、`_detector`、`_profile`
- `_builder` 模块之间通过 SQLite 存储（`sqlite_store.py` + `cgdb_store.py`）通信——而非直接 Python 导入——因此替换一个模块不会引发连锁修改
- CLI 入口（`code2database_builder.py`）是薄薄的 argparse 路由器，通过懒加载（`_builder/__init__.py`）导入命令处理函数

### 关键共享抽象

- **`unified_node_id(language, fqn, signature, byte_offset)`**——每种语言的每个节点 ID 都是 SHA-256 截断到 60 位 + 语言前缀，防止跨语言冲突，同时适配 SQLite 有符号 INTEGER。见 `_scanner/unified_id.py`。
- **`BaseScanner`**——每种语言的扫描器都继承自这个 ABC，共享 `_walk(node)` 递归模式（带 `cond_stack` 用于条件调用注解）、`_emit_cgdb_records`（cgdb 层填充）、`_annotate_config_predicates`（跨语言 `#ifdef`/`//go:build`/`#[cfg]`/`sys.platform`/`@Profile` 归一化）。
- **`transaction(graph_dir)`**——每个改 DB 的操作都应包裹在这个上下文管理器里（快照 + WAL + 文件锁）。`patch-from-diff`/`patch-from-git` 默认已包裹。
- **`StreamingGraph`**——NetworkX 兼容 API，把节点/边流式写入 SQLite 而非内存。在 `--storage sqlite --low-memory` 模式下是 `nx.DiGraph` 的即插即用替代（1.4M 节点 ~1.9GB RAM）。
- **`SQLiteStore` + `CGDBStore`**——共存于同一个 `code2database.db`。遗留 `functions`/`edges` 表向后兼容；cgdb 强类型语义表用于查询。Schema 迁移幂等。

## 技术栈

### 核心运行时

| 组件 | 用途 | 必需？ |
|------|------|--------|
| **Python 3.8+** | 运行时 | 必需（`_scanner/utils.py` 中 MIN_PYTHON = (3, 8)） |
| **networkx ≥3.0** | 内存图引擎（DiGraph、BFS、最短路径） | 必需（`_vendor/` 有回退 shim） |
| **tree-sitter ≥0.22** | AST 解析框架 | 必需 |

### 按语言的 tree-sitter 语法

| 语法 | 语言 | 用于 |
|------|------|------|
| `tree-sitter-c ≥0.21` | C | C 扫描 |
| `tree-sitter-cpp ≥0.22` | C++ | C++ 扫描（模板、concepts、coroutines） |
| `tree-sitter-go ≥0.21` | Go | Go 扫描（goroutine、channel、interface） |
| `tree-sitter-python ≥0.21` | Python | Python 扫描（类、装饰器、match） |
| `tree-sitter-java ≥0.21` | Java | Java 扫描（包、import、方法） |
| `tree-sitter-rust ≥0.21` | Rust | Rust 扫描（impl/trait、async、cfg） |

ASM（.s .S .asm）用正则扫描——无需 tree-sitter 语法。

`scripts/setup.sh` 支持 `--languages c,go` 部分安装；`C2D_LANGUAGES` 环境变量等效。

### 可选高级功能

| 组件 | 用途 | 启用方式 |
|------|------|---------|
| **libclang ≥17.0**（`pip install libclang==17.0.6`） | clang AST 后端 → cgdb 层（强类型 vtable 分发、CFG、数据流、同步原语、config 谓词、ops 绑定、happens-before） | `--extraction-backend clang` 或装了 libclang 的 `auto` |
| **z3-solver ≥4.12** | 可靠 SMT 路径可行性（`path-feasible`、`cgdb-path-feasible`）；缺失时启发式回退 | `pip install z3-solver` |
| **python-igraph ≥0.11** + **leidenalg ≥0.10** | 跨领域 Leiden 社区检测 | 缺失时回退到基于领域的分组 |

### 存储

| 组件 | 用途 |
|------|------|
| **SQLite**（标准库 `sqlite3`） | 主存储后端（`code2database.db`）：遗留表 + cgdb 强类型语义表 |
| **WAL 日志模式** | `PRAGMA journal_mode=WAL`，支持并发读者 + 单写者 |
| **zlib 压缩** | 函数体的 `body_text_compressed BLOB` |
| **FTS5** | `nodes_fts` 虚拟表，用于符号全文搜索（cgdb 层） |

### 并发与 IPC

| 组件 | 用途 |
|------|------|
| **fcntl**（POSIX）/ **msvcrt**（Windows） | 事务性更新的文件级读写锁 |
| **inotify**（Linux，经 ctypes） | 守护进程实时文件监视——零额外依赖 |
| **轮询**（回退） | inotify 不可用时的跨平台文件监视 |
| **Unix socket** | `/tmp/code2database-daemon-<project>.sock`，守护进程控制面（status、force-refresh、pause、resume、wait-sync） |

### LLM / 代理集成

| 组件 | 用途 |
|------|------|
| **MCP（Model Context Protocol）stdio 传输** | `serve` 命令通过 JSON-RPC 暴露 53 个工具（31 个 `code2database_*` + 19 个 `cgdb_*`），带 Content-Length 帧 |
| **分层上下文包** | micro（~200 token） → lite（~500） → standard（~1500） → full——最小化 LLM token 成本 |
| **懒加载模块导入** | `_builder/__init__.py` 延迟模块加载到首次访问，降低启动时间 |

### 外部函数接口（FFI）检测

| 机制 | 源语言 | 目标 |
|------|--------|------|
| **ctypes**（CDLL/WinDLL）、**cffi**、**pybind11** | Python | C |
| **cgo**（`import "C"`、`//go:cgo_import`） | Go | C |
| **extern "C"** 块、`#[no_mangle]` 导出 | Rust | C |

每个 FFI 绑定产生一条 `FFI_BRIDGE` 边，带类型编组与错误映射元数据。

### 构建系统检测器

| 构建系统 | 检测方式 | 提取内容 |
|----------|---------|---------|
| CMake | `CMakeLists.txt`、`*.cmake` | `-D` 宏、目标、include 目录、build types |
| Make | `Makefile`、`*.mk` | 变量、目标 |
| Meson | `meson.build` | 变量、依赖 |
| Autotools | `configure.ac`、`Makefile.am` | Autoconf 宏 |
| Kconfig | `Kconfig`、`Kconfig.*` | CONFIG_* 符号、默认值 |
| Bazel | `BUILD`、`BUILD.bazel`、`WORKSPACE` | 目标、依赖 |
| MSBuild | `*.csproj`、`*.vcxproj` | 属性、项 |
| spec | `*.spec`（RPM） | 宏、%define |

用于 `#ifdef` 宏解析和条件编译路径可行性。

### 测试与评测

| 组件 | 用途 |
|------|------|
| **pytest** | 测试运行器（55 个测试文件，~17K 行，覆盖扫描器/构建器/cgdb/守护进程/MCP/并发等） |
| **evals/evals_en.json** + **evals_zh.json** | 端到端场景评测（多语言扫描 + 查询） |
| **BUG benchmark** | `bug_benchmark.py`：GraphInvestigator vs GrepInvestigator 召回/精确/token 效率 |

## 关键算法

### 入口点评分

多因子评分：`score = base_score * export_multiplier * name_multiplier * framework_multiplier`

- **base_score** = callee_count / (caller_count + 1)——调用多但被调用少的函数
- **export_multiplier**：`API_entry` 为 2.0，其他为 1.0
- **name_multiplier**：入口模式（main、run、start、handle_、on_）为 1.5，工具模式（get_、set_、is_）为 0.3，测试模式（test_、mock_、stub_）为 0.1
- **framework_multiplier**：来自检测到的框架

测试函数在评分前用模式过滤：`test_`、`testcase_`、`spec_`、`bench_`、`mock_`、`stub_`、`fake_`、`generateTest`，以及 `main`（除非在 `/app/`）。

### Vtable 分发解析

通过结构体字段赋值解析间接调用的三阶段：

1. **直接字段赋值**（如 `.submit_request = nvme_submit_request`）：从调用函数到被赋值函数创建 `vtable_dispatch` 边
2. **回调桥接**（2 跳）：若函数 A 以回调参数调用函数 B，而该回调在别处注册，则从 A 到回调目标创建边
3. **模块提示**：解析注册变量名以派生 `#vtable_module=<module>` 条件，支持条件路径解析
4. **强类型 vtable 分发**（cgdb L7，仅 clang 后端）：`ops_bindings` 表通过 `cgdb_find_ops_impls` MCP 工具链接 FieldDecl → FunctionDecl，返回 `{ops_table_id, field_node_id, impl_function_id, signature_match}`

### 数据竞争检测

```
对每个共享资源（全局变量或结构体字段）：
  收集所有访问它（读或写）的函数
  对每对在不同线程上下文中访问的函数：
    若无共同 mutex 保护 → 标记为 data_race（涉及写）或 atomic_violation（都读）
    若持有不同锁 → 标记为 deadlock_risk
```

线程上下文由 `thread_model` + `thread_entry` 属性决定：具有不同线程入口点的函数在不同上下文。锁模式由 profile 驱动（无硬编码锁 API）——项目填充 `concurrency_patterns.lock_acquire_patterns` / `lock_release_patterns`。

### 条件编译跟踪

C 扫描器在 AST 遍历期间用 `_ifdef_stack` 跟踪 `#ifdef`/`#ifndef`/`#if`/`#elif`/`#else` 块的嵌套：

1. 进入 `preproc_ifdef`：压栈
2. 进入 `preproc_elif`：栈顶替换为父取反 + elif 条件
3. 进入 `preproc_else`：栈顶替换为父取反
4. 函数定义继承当前 `_ifdef_stack` 作为 `ifdef_conditions`
5. 调用表达式继承函数级和内联 `ifdef` 条件作为 `call_condition`

clang 后端额外构建 Z3 SMT-LIB 形式（L3 `conditions` 表）用于可靠路径可行性。跨语言谓词在 `_scanner/config_predicates_lang.py` 中归一化：

- Go `//go:build linux && amd64` → `CONFIG_GO_TAG_LINUX AND CONFIG_GO_TAG_AMD64`
- Rust `#[cfg(target_os = "linux")]` → `CONFIG_CFG_TARGET_OS_LINUX`
- Python `sys.platform == "linux"` → `CONFIG_PY_PLATFORM_LINUX`
- Java `@Profile("prod")` → `CONFIG_JAVA_PROFILE_PROD`

### 提交来源绑定

扫描时，扫描器调用 `git rev-parse HEAD`（或 `svn info`）捕获当前提交哈希，然后给每个节点/边盖戳 `commit_meta = {source_commit, author, date, message}`。`blame-node` 走 `git log -- <file>`（或 `svn log`）找引入提交。`node-history` 聚合提交演进。工程师用 `git show <hash>` 验证，而非时间戳。

### 值流传播

对每个函数，通过遍历函数体构建每参数数据流签名：当函数体的返回语句是 `return p_i`（或对被跟踪表达式 `return expr(p_i)`）时，从调用者参数 `p_i` 到被调用者返回 `r` 创建一条 `DATA_FLOW` 边。`value-flow` 跟踪传递闭包，回答"这个 NULL 从哪来？"而无需重读源码。

### 锁持有事件流

扫描器对每个函数体分词，发射 `{kind: acquire|release, lock_var, char_pos, line, col}` 事件流。构建器把事件合并为锁持有区间。`lock-coverage` 返回区间；`concurrency-analyze` 消费它们，用精确重叠检查取代之前的"存在锁"启发式。

### 不变量提取

三个子算法：
1. **前置条件**：解析函数入口附近的 `if (!cond) return` 模式；发射 `cond` 作为前置条件。
2. **后置条件**：解析出口附近的 `return cond` 或 `assert(cond)`；发射 `cond` 作为后置条件。
3. **状态机**：计算对单个状态变量的赋值次数；阈值 `>= 1`。若该变量取 ≥2 个不同字面值，发射 `{state_var, states, transitions}`。

置信度：直接解析的模式为 `EXTRACTED`，间接的为 `INFERRED`，函数体形状猜测为 `AMBIGUOUS`。`apply-invariants` 拒绝 AMBIGUOUS，INFERRED 需用户确认。

### 事务性更新

`transaction()` 是上下文管理器：
1. 进入：在 `.code2database_tx/tx.lock` 上获取 fcntl 排他锁，把活 DB 快照到 `.code2database_tx/snapshots/<txid>/`，打开 `.code2database_tx/wal.jsonl` 追加。
2. 退出（成功）：刷 WAL、fsync、原子重命名快照为活、释放锁。
3. 退出（失败）：反向重放 WAL 撤销、恢复快照、释放锁。
4. 崩溃恢复（`tx-replay-wal`）：下次进程启动时检测未完成的 WAL 条目并重放。

### Profile 健康评分

七类，每类 0-100 分：
- `callback_patterns`（25 分）——`static_patterns` 对代码中实际回调注册的覆盖
- `skip_names`（15 分）——项目特定宏的覆盖
- `vtable_types`（15 分）——`struct_op_types` 的覆盖
- `api_prefixes`（10 分）——`public_prefixes` 准确度
- `domain_keywords`（15 分）——`domain_rules` 准确度
- `macro_definitions`（10 分）——`registration_macros` 覆盖
- `profile_version`（10 分）——profile 版本与当前 schema 的匹配

最终分 = 加权平均。`profile-evolve` 在观察模式跑扫描器，检测新回调注册函数，发射 EXTRACTED 置信度建议（`--apply` 自动应用）或 INFERRED 置信度（需用户确认）。`profile-bind-version` 把 `profile_version_bound_commit` 写入，绑定到当前 HEAD；HEAD 变化时 `profile-health` 标记陈旧。

### 文档-代码对齐

`describe-node` 暴露 `doc_code_mismatches`：`semantic_desc`（来自文档）与 `body_text`（来自代码）之间的不匹配列表。四种不匹配：
1. **返回值不匹配**——文档说"返回 X"，代码返回 Y。
2. **参数名不匹配**——文档命名参数与代码不同。
3. **签名变更**——文档签名与代码签名不同。
4. **陈旧文档**——文档引用不再存在的函数/文件。

`doc-mark-stale` 设置 `doc_stale=true` 和 `doc_stale_reason`。`knowledge-validate` 在知识校验期间跑同样的检查。

### 守护进程

```
inotify 等待 → debounce 500ms → 批窗口 1000ms → transaction() {
    重扫变更文件 → 打补丁图 → 重建输出
} → 写 .daemon_status.json → 通知 socket 客户端
```

熔断器：若事件/分钟 > 1000（可通过 `daemon.circuit_breaker_threshold` 配置），该分钟切到批量重建。`/tmp/code2database-daemon-<project>.sock` 的 socket API 接受 JSON 命令：`status`、`force-refresh`、`pause`、`resume`、`wait-sync`。状态持久化到 `<graph_dir>/.daemon_status.json` 供非 MCP 客户端使用。自适应批窗口在负载下自动增长（200ms 下限，5000ms 上限）。

### MCP 服务器

`serve` 通过 stdio JSON-RPC 暴露 53 个工具，带 Content-Length 帧：

- 31 个 `code2database_*` 工具（load、search、describe、explore、trace、impact、key_paths、concurrency、data_lifecycle、domain、knowledge_query、memory_search、semantic_status 等）
- 19 个 `cgdb_*` 工具（cgdb_search_symbols、cgdb_find_invokers、cgdb_find_invoked、cgdb_get_definition、cgdb_get_function_body、cgdb_get_struct_layout、cgdb_find_type_definition、cgdb_find_ops_impls、cgdb_find_cfg_paths、cgdb_find_data_flow、cgdb_find_aliases、cgdb_find_lock_held_calls、cgdb_check_race_condition、cgdb_find_configs_for、cgdb_find_nodes_under_config、cgdb_index_status、cgdb_time_travel_query、cgdb_list_versions）

MCP 服务器无论哪个子 skill 激活都可访问；子 skill 纯粹是 LLM 上下文经济机制。

## 存储：JSON + SQLite 双后端

主存储是 JSON 文件（人类可读），但 SQLite 后端（`sqlite_store.py`）是构建的默认选择：

- **高效查询**：用 SQL 查询而非加载整个 JSON 文件
- **降低磁盘占用**：body_text 用 zlib 压缩，无冗余索引
- **可扩展性**：用 `StreamingGraph` 低内存后端处理 1.4M 节点图（~1.9GB RAM vs NetworkX 的 ~24GB）
- **cgdb 层**：13 张强类型语义表与遗留 `functions`/`edges` 共存，向后兼容
- **Schema 演进**：`cgdb_migrations.run_migrations` 原地 ALTER 表，保留数据

SQLite 存储用 WAL 日志模式（并发读者 + 单写者）、64MB 缓存、256MB mmap，以及 SQL 原生 `field_access`/`global_access` 表用于 O(log n) 每字段查询（取代 O(n) Python 遍历）。

## 能力总结

Code2Database 当前能力，按类别组织：

### 扫描与提取
- 7 种语言：C、C++、Go、Python、Java、Rust、ASM（NASM x86_64、GNU as AT&T、ARM/AArch64、RISC-V、LoongArch、s390、PowerPC、SuperH、MIPS、IA-64）
- 双后端：tree-sitter（默认）+ clang（可选，cgdb 层）
- 跨语言统一节点 ID（SHA-256 截断到 60 位 + 语言前缀）
- 通过文件指纹增量扫描（`changes.py`）
- profile 驱动的回调/锁/vtable 模式
- 条件编译跟踪（#ifdef、//go:build、#[cfg]、sys.platform、@Profile）

### 图构建
- Vtable 分发解析（3 阶段 + 经 clang 的强类型 ops_bindings）
- 回调桥接（2 跳）
- 领域分类 + 外部代码分离
- 跨领域 Leiden 社区检测
- 端点分类 + 入口点评分
- 提交来源（git/svn HEAD 绑定）
- 数据竞争检测 + 锁持有区域
- 不变量提取（preconditions/postconditions/loop_invariants/state_machine）
- FFI 桥接检测（ctypes/cgo/extern "C"）
- 值流（DATA_FLOW 边）+ 跨函数数据依赖（DATA_DEP 边）

### 查询与分析
- 146 个 CLI 子命令（3 个子 skill：核心 15、分析 13、运维 14 个 Tier-1）
- 53 个 MCP 工具（34 code2database_* + 19 cgdb_*）
- Cypher 子集查询语言（MATCH/WHERE/RETURN）
- Z3 SMT 路径可行性（启发式回退）
- 分层上下文包（micro/lite/standard/full）
- 反向追踪、爆炸半径、影响分析
- 内存序分析（原子操作、屏障、READ_ONCE/WRITE_ONCE）
- Happens-before 关系计算

### 运维与可靠性
- 事务性更新（WAL + 快照 + fcntl 锁）
- 后台守护进程（inotify + 熔断器 + Unix socket API）
- Profile 健康（7 类 0-100）+ 自动演进 + HEAD 绑定
- 文档-代码对齐（返回/参数/签名/陈旧文档不匹配）
- LLM 自动增强，带置信度阈值自动写入 + 回滚
- 持久化 Q&A 记忆，带衰减 + scratch
- 知识提取/查询/校验
- Web UI（单文件 HTML/SVG/JS）
- HTML/Obsidian 导出
- BUG benchmark（GraphInvestigator vs GrepInvestigator）
- 插件系统
- 嵌入（TF-IDF 字符 n-gram 语义搜索）
- Git post-commit 钩子，自动 quick-update

### 分发
- Python skill，3 个子 skill（`/Code2Database`、`/Code2Database-analysis`、`/Code2Database-ops`）
- 一键安装器（`install.sh`），支持 Claude Code / Cursor / Codex / OpenCode / Gemini
- 按语言安装（`C2D_LANGUAGES` 环境变量或 `setup.sh --languages`）
- 55 个测试文件，~17K 行（扫描器、构建器、cgdb、守护进程、MCP、并发、FFI 等）
- 中英文端到端评测（`evals/evals_en.json`、`evals/evals_zh.json`）
- 双语文档（`docs/en/`、`docs/zh/`）
