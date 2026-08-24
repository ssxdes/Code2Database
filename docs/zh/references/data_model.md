# 数据模型参考

> **为什么这是代码数据库，不只是图**：每个节点带 `body_text`、`signature`、`params`、`local_vars`、`callee_args`、`condition_vars`、`ifdef_conditions`——足以在不打开源文件的情况下推理一个函数。每条边带 `call_condition`、`concurrency`、`confidence`、`evidence`、`preproc_alive`——足以知道这次调用*为什么*发生、*是否*确定、*在什么构建配置下*存在。图谱不是结构索引；它是可查询的程序语义模型。

## 图结构

Code2Database 生成一个**有向图（DiGraph）**：

- **节点** = 函数。每个节点包含：name、source_file、line、位置索引（`source_file:line`）、domain、labels、is_empty_node、api_constraints、external_desc、**body_text**、**signature**、**params**、**local_vars**（带 is_param 标志）、**callee_args**（带结构化参数和 concurrency_info）、**condition_vars**、**endpoint_type**、**declaration_only**
- **边** = 调用关系（invoker → invoked）。每条边包含：
  - `call_order`：调用序号（同一 invoker 内从 1 开始）
  - `call_condition`：条件表达式 — 支持 if_cond/if/while_cond/for_cond/switch/ternary_cond/ternary_true/!ternary 及复合 &&/|| 链
  - `concurrency`：并发类型（`""` 顺序执行/`"thread_spawn"`/`"goroutine"`/`"callback"`/`"spawn_target"`/`"callback_dispatch"`）
  - `confidence`：置信度分类（`EXTRACTED`/`INFERRED`/`AMBIGUOUS`）
  - `source`：来源标签（`"ast"`/`"llm"`/`"manual"`/`"plugin:<name>"`/`"preproc_dead"`/`"import_resolution"`/`"macro_expansion"`/`"vtable_resolution"`）
  - `confidence_score`：数值分数（0.0–1.0）— EXTRACTED=1.0, INFERRED=0.7–0.95, AMBIGUOUS=0.1–0.3

节点的 `labels_source` 记录每个标签的来源（例如 `{"thread_processor": "ast", "callback_func": "llm"}`）。

**边类型**：
- `direct_call`：直接调用边
- `callback_dispatch`：从 fn_ptr 调用点到可能实现的边，代表间接调用目标的保守过近似
- `import_edge`：基于 #include 关系的域间边（域 A 包含域 B 的头文件）

**节点属性**：
- `endpoint_type`：out_end/unknown_end 节点的子分类，通过 profile 的 `endpoint_types` 配置
- `declaration_only`：布尔值，若函数在此域中声明但在别处定义则为 true（仅头文件声明）
- `vtable_struct_type`：若此函数是 vtable 注册，它属于哪个 struct_op_type
- `vtable_field`：若此函数是 vtable 注册，它实现了哪个字段

## 空节点

当多个 invoked 共享相同的调用条件时，通过空节点进行聚合：

```c
void func(int a) {
    if (a) { func1(); func2(); }
    func3();
}
```

生成：`func → [if(a)] → func1(1), func2(2)`，以及 `func → func3(3)`

## 架构域

域从源文件路径自动派生（与语言无关）：

| 文件路径 | 域 |
|----------|-----|
| `lib/device/device.c` | `lib.device` |
| `module/device/nvme/device_nvme.c` | `module.device.nvme` |
| `pkg/server/handler.go` | `pkg.server` |
| `src/main/java/com/app/App.java` | `src.main.java.com.app` |
| `crates/engine/src/lib.rs` | `crates.engine.src` |

JSON 输出按域组织。大型图会被拆分为多个文件，通过 `code2database_master.json` 进行导航。

### 域规范化

规范化规则确保同一子系统不会因路径差异而出现为多个独立域：

1. **Profile domain_rules**：应用 profile 中的合并/折叠规则（例如将特定路径前缀映射到统一的域名称）
2. **扫描后去重**：若两个域去掉公共前缀后后缀相同，则合并
3. **枢纽函数域纠正**：内联/头文件函数映射到其**定义**域而非**包含**域

### 域与模块深度

借鉴代码库设计中的 Deep Module 概念：

| 代码库设计概念 | 调用图对应概念 | 含义 |
|---------------|---------------|------|
| **Seam**（接缝位置） | 域边界 | 跨域调用 = 跨接缝依赖 |
| **Interface**（暴露的接口面） | API_entry 函数 | 域对外暴露的公共函数 |
| **Implementation**（隐藏的实现） | 域内非 API 函数 | 不对外暴露的内部逻辑 |
| **Depth**（小接口，大实现） | `API_entry 数量 / 域内函数总数` | 比值越小 = 越深（接口少，行为丰富） |
| **Leverage**（调用者收益） | 一个 API_entry 服务多少外部调用者 | 一个 API_entry 服务 N 个调用者 = 高杠杆 |
| **Locality**（维护者收益） | 变更集中度 | Bug 修复仅涉及一个域的函数 = 高局部性 |

**深度评估**：
- **深模块**（良好）：API_entry <= 3，域函数 > 10 — 接口少，行为丰富
- **浅模块**（需关注）：API_entry > 10，域函数 < 15 — 接口膨胀，实现薄弱
- **删除测试**：移除一个域后，复杂度是消失了（浅 = 仅做传递）还是分散到 N 个调用者（深 = 值得保留）

仅头文件声明（标记 `declaration_only: true`）不计入深度比的 API_entry。

## 边置信度分类

每条边都携带置信度分类和来源标签，帮助人类和 LLM 判断哪些数据是确定的，哪些需要验证：

| 置信度 | 分数 | 含义 | 示例 |
|--------|------|------|------|
| `EXTRACTED` | 1.0 | 从 AST 直接提取 — 调用在源码中明确存在 | `device_start()` 调用 `register_device()` — tree-sitter 可见调用表达式 |
| `INFERRED` | 0.7–0.95 | LLM/插件推理 — 在源码中隐含但需要语义理解 | 回调目标：`register_callback(cb)` → LLM 推断 `cb` 是 `my_handler` |
| `AMBIGUOUS` | 0.1–0.3 | 不确定 — 函数指针、动态派发、宏展开 | `(*func_ptr)()` — 静态分析无法确定目标 |

来源标签：`"ast"`（tree-sitter 扫描器）、`"llm"`（Claude 语义增强）、`"manual"`（用户手动）、`"plugin:<name>"`（外部插件）、`"preproc_dead"`（死预处理分支）、`"import_resolution"`（头文件导入解析）、`"macro_expansion"`（宏展开调用）、`"vtable_resolution"`（vtable 派发解析）

节点的 `labels_source` 记录每个标签的来源。置信度分布统计可在 CODE2DATABASE_SUMMARY.md 中查看。

总边数必须与置信度分类的总和匹配。若边数未对齐，构建步骤的自动校验将标记此问题。

## 构建系统宏解析

C/C++ 代码使用 `#ifdef`/`#ifndef`/`#if` 条件编译。tree-sitter 会解析所有分支（不评估宏），导致调用图包含死代码分支。

**解决方案**：检测构建系统，提取 `-D` 宏定义，评估 `#ifdef` 条件，标记死分支。

支持的构建系统：CMake、Make、Spec（RPM）、Meson、Autotools、Kconfig、Bazel

**标记规则**：
- 死分支中的函数 → `labels: ["dead_code"]`，`labels_source: {"dead_code": "preproc_dead"}`
- 死分支中的边 → `confidence: "AMBIGUOUS"`，`confidence_score: 0.0`，`source: "preproc_dead"`，`preproc_alive: false`
- 活分支中的边 → 额外携带 `preproc_condition` 和 `preproc_alive: true`

**条件编译元数据**：vtable 注册条目可捕获条件编译元数据，包含 `config_condition`（如 `CONFIG_SMP`）和 `condition_active`（基于构建配置宏判断），使调用图能够建模不同构建变体中的差异。

**使用方法**：

```bash
# 自动检测构建系统，选择 Release 配置宏
python3 "$SKILL_DIR/scripts/code2database_builder.py" build \
  --extraction extraction.json --outdir code2db-out/ \
  --build-config auto

# 指定特定配置
python3 "$SKILL_DIR/scripts/code2database_builder.py" build \
  --extraction extraction.json --outdir code2db-out/ \
  --build-config Debug

# 手动指定宏
python3 "$SKILL_DIR/scripts/code2database_builder.py" build \
  --extraction extraction.json --outdir code2db-out/ \
  --macros "NDEBUG HAVE_CONFIG_H=1 FEATURE_X=1"

# 扫描时指定宏
python3 "$SKILL_DIR/scripts/code2database_scanner.py" scan \
  --source /path/to/code --output out.json \
  --macros "NDEBUG FEATURE_A=1 -DFOO"
```

**交互式确认**：当检测到多个构建配置时，构建器会提示选择。在非交互场景下，请显式使用 `--build-config Release`。

**输出**：CODE2DATABASE_SUMMARY.md 增加 "Build Configuration" 部分；context_pack 增加 `build_config` 字段；独立文件 `.code2database_build_config.json` 用于增量更新复用。

## 社区检测（Leiden 算法）

使用 Leiden 算法对调用图进行语义社区检测，补充基于目录路径的域分组。将频繁互调的函数聚类为社区，并赋予启发式标签（来自目录模式、函数名前缀、关键词）。

对于大型图（>100K 节点）的可扩展性策略：
- **分层 Leiden**：对每个域分别运行 Leiden，然后合并结果
- **标签传播**：作为大型图的可扩展替代方案
- **基于采样**：对代表性样本运行社区检测，然后分配剩余节点

**输出文件**：`.code2database_communities.json`（社区列表 + 节点映射）；CODE2DATABASE_SUMMARY.md 增加 "Community Map" 部分；context_pack 增加 `community_map`；节点增加 `community_id` 属性。

**依赖**：`python-igraph` + `leidenalg`（不可用时回退到域分组）

## 入口点评分

多因子评分以识别最可能的公共 API 入口点：

```
Score = baseScore x exportMultiplier x nameMultiplier x frameworkMultiplier + bonusScore
```

- baseScore = callee_count / (caller_count + 1) — 调用多、被调用少 = 编排入口
- exportMultiplier = 2.0（API_entry）/ 1.0
- nameMultiplier = 1.5（handle_/on_/main/run 等）/ 0.3（get_/set_/is_/has 等）
- frameworkMultiplier = 框架检测加成（SPDK/Django/Spring 等 -> 1.5x）
- 额外加分：
  - +20分：匹配 profile 中的 `public_prefixes`
  - +15分：属于 `struct_op_types`
  - 每个跨域调用者+1分（上限50）
  - +10分：是 `program_entry` 或 `callback_entry`
  - **-10分**：在 `skip_names` 中（如 strlen、memcpy 等通用工具函数）

**输出文件**：`.code2database_entry_scores.json`；节点增加 `entry_score` 属性。

## 框架检测

从文件路径自动识别已知框架（SPDK、DPDK、Django、Flask、FastAPI、Spring、Gin、Actix、Rocket、Tokio、Qt 等），用于入口点评分乘数。

支持的框架：C/C++（SPDK/DPDK/libevent/libuv/Qt/GTK）、Python（Django/Flask/FastAPI/Celery/Tornado）、Java（Spring/JAX-RS/Android）、Go（Gin/Echo/Fiber）、Rust（Actix/Rocket/Tokio/Warp/Axum）、Node.js（Express/Next.js/Nuxt/Koa/Fastify）

## 流程检测（执行流）

从最高评分入口点通过 INVOKES 边进行 BFS 追踪，生成执行流。跨社区追踪并赋予启发式标签。流程检测沿 `callback_dispatch` 边和 `direct_call` 边追踪，能够跨越间接调用边界。

**输出文件**：`.code2database_processes.json`；CODE2DATABASE_SUMMARY.md 增加 "Execution Processes" 部分；context_pack 增加 `execution_processes`。

## 证据追踪

每条边携带 `evidence` 数组，记录提取来源和上下文：
- `{"kind": "ast_call", "weight": 1.0, "note": "direct call at line 42"}` — AST 直接提取
- `{"kind": "import_resolution", "weight": 0.75, "note": "resolved via #include <header.h>"}` — 导入解析
- `{"kind": "thread_spawn", "weight": 0.85, "note": "spawn target: func, line 42"}` — 线程创建
- `{"kind": "macro_expansion", "weight": 0.8, "note": "宏展开为实际函数"}` — 宏展开
- `{"kind": "vtable_dispatch", "weight": 0.75, "note": "operations.read -> impl_func", "struct_type": "operations"}` — Vtable 派发
- 死代码分支：`{"kind": "ast_call", "weight": 0.0, "note": "dead branch: #ifdef X, line 42"}`

插件可以添加自己的 evidence 条目。

## 导入解析（C/C++）

扫描头文件（.h/.hpp）构建 header→function 映射，通过多策略解析管道解析跨文件调用目标：

1. **suffix_index**（O(1)）：将 invoked 名与含点的节点 ID 匹配 — 如 `bar` → `lib.bdev.bar`
2. **same_file**（0.95）：invoked 定义在同一源文件
3. **import_map**（0.85）：invoked 的头文件被 invoker `#include`
4. **same_domain**（0.75）：invoked 在同一架构域
5. **suffix_match**（0.60）：invoked 名匹配节点 ID 后缀
6. **unique_name**（0.55）：全局唯一函数名
7. **fuzzy**（0.30–0.40）：部分名称匹配

构建后处理：扫描头文件桥接剩余未解析的外部端点，添加 `INFERRED` 边（confidence=0.75, source="import_resolution"）。

## SQLite 存储后端

对于大型图，SQLite 存储后端提供高效的查询性能，避免将全部数据加载到内存。

数据库模式（遗留表）：

```sql
CREATE TABLE nodes (
    id TEXT PRIMARY KEY,
    name TEXT,
    domain TEXT,
    labels TEXT,  -- JSON数组
    signature TEXT,
    source_file TEXT,
    line INTEGER,
    endpoint_type TEXT,
    declaration_only INTEGER DEFAULT 0,
    entry_score REAL DEFAULT 0
);

CREATE TABLE edges (
    src TEXT,
    dst TEXT,
    type TEXT,  -- direct_call, callback_dispatch, import_edge 等
    call_order INTEGER,
    call_condition TEXT,
    confidence TEXT,
    confidence_score REAL,
    source TEXT,
    concurrency TEXT,
    evidence TEXT,  -- JSON数组
    FOREIGN KEY (src) REFERENCES nodes(id),
    FOREIGN KEY (dst) REFERENCES nodes(id)
);

CREATE TABLE reverse_index (
    node TEXT,
    invokers TEXT  -- caller节点ID的JSON数组
);

CREATE TABLE vtable_dispatch (
    struct_type TEXT,
    field TEXT,
    implementation TEXT,
    config_condition TEXT,
    FOREIGN KEY (implementation) REFERENCES nodes(id)
);

CREATE TABLE field_access (
    struct_name TEXT,
    field TEXT,
    accessor_fn TEXT,
    access_type TEXT,  -- read/write/read-write
    thread_model TEXT,
    FOREIGN KEY (accessor_fn) REFERENCES nodes(id)
);

-- 常用查询索引
CREATE INDEX idx_nodes_name ON nodes(name);
CREATE INDEX idx_nodes_domain ON nodes(domain);
CREATE INDEX idx_nodes_labels ON nodes(labels);
CREATE INDEX idx_edges_src ON edges(src);
CREATE INDEX idx_edges_dst ON edges(dst);
CREATE INDEX idx_edges_type ON edges(type);
CREATE INDEX idx_reverse_node ON reverse_index(node);
CREATE INDEX idx_vtable_struct ON vtable_dispatch(struct_type);
CREATE INDEX idx_field_struct ON field_access(struct_name);
```

## cgdb（代码图数据库）层 —— 54 张强类型语义表（Schema v4）

启用 clang 提取后端（`--extraction-backend clang` 或装了 libclang 的 `auto`）时，Code2Database 会在同一个 `code2database.db` 中填充额外的强类型语义 schema。这些表由 19 个 `cgdb_*` MCP 工具查询，支撑强类型 vtable 分发、CFG 路径查找、def-use 链、Z3 可判定的 config 谓词等功能。Schema 版本：`CGDB_SCHEMA_VERSION = 4`。

| 层 | 表 | 用途 | 关键列 |
|----|----|------|--------|
| L0 | `graph_versions` | 每提交快照，支持时间旅行查询 | `version_id`、`commit_hash`、`created_at`、`parent_version` |
| L1 | `cgdb_nodes`、`cgdb_files` | 多种类一等节点 + 文件注册表 | `node_id`（SHA-256 截断 60 位）、`kind`、`fqn`、`name`、`source_file_id`、`line`、`col`、`enclosing_symbol_id`、`config_predicate_id`、`source_snippet`、`description`、`llm_confidence` |
| L2 | `cgdb_types` | 独立类型系统（builtin/pointer/reference/array/record/enum/function/template/typedef） | `type_id`、`kind`、`name`、`size`、`alignment`、`const`、`volatile`、`pointee_type_id` |
| L3 | `conditions` | Z3 SMT-LIB 布尔表达式树 | `condition_id`、`z3_form`、`human_form`、`kind`（atomic/not/and/or/implies） |
| L3.5 | `config_predicates` | `#ifdef` 谓词树（BDD + Z3 形式），跨语言（Go `//go:build`、Rust `#[cfg]`、Python `sys.platform`、Java `@Profile`、ASM/C `#ifdef`） | `predicate_id`、`normalized_form`、`z3_form`、`bdd_form`、`status`（UNCONDITIONAL/CONTRADICTORY/CONDITIONAL）、`language`、`source_file_id`、`line` |
| L4 | `basic_blocks`、`cfg_edges` | 控制流图 | `block_id`、`function_id`、`statement_ids`、`terminator_kind`；`edge_id`、`src_block`、`dst_block`、`condition_id`、`kind`（fallthrough/true/false/loop_back/exception） |
| L5 | `data_flow`、`alias_sets` | def-use 链 + 指针别名 | `flow_id`、`function_id`、`variable`、`def_block`、`def_kind`（param/var/return）、`use_block`、`use_kind`（call/return/branch/assign）；`alias_set_id`、`pointer`、`aliases`（JSON） |
| L7 | `invoke_sites`、`ops_bindings` | 调用点精化 + 强类型 vtable 分发 | `site_id`、`caller_function_id`、`callee_expr`、`callee_resolved_id`、`condition_id`、`is_indirect`；`binding_id`、`ops_table_id`、`field_node_id`、`impl_function_id`、`signature_match` |
| L8 | `sync_primitives`、`happens_before` | 并发 + 内存模型 | `prim_id`、`kind`（mutex/spinlock/rwlock/atomic/condvar/barrier）、`var_name`、`acquire_site_id`、`release_site_id`、`memory_order`；`hb_id`、`event_a`、`event_b`、`ordering`（happens_before/concurrent/undetermined） |
| FTS | `nodes_fts` | FTS5 虚拟表，用于符号全文搜索 | FTS5 列覆盖 `cgdb_nodes.name`、`fqn`、`description` |

### 跨语言 config 谓词归一化

所有语言都归一化到 L3.5 `config_predicates` 层：

- C/C++/ASM：`#ifdef CONFIG_SMP` → `CONFIG_SMP`
- Go：`//go:build linux && amd64` → `CONFIG_GO_TAG_LINUX AND CONFIG_GO_TAG_AMD64`
- Rust：`#[cfg(target_os = "linux")]` → `CONFIG_CFG_TARGET_OS_LINUX`
- Python：`sys.platform == "linux"` → `CONFIG_PY_PLATFORM_LINUX`
- Java：`@Profile("prod")` → `CONFIG_JAVA_PROFILE_PROD`

每个谓词带 `status` 字段：
- `UNCONDITIONAL`——始终为真（无 `#ifdef` 守卫）
- `CONDITIONAL`——依赖宏值；Z3 形式可满足
- `CONTRADICTORY`——Z3 形式不可满足（死分支）

### Schema 迁移

`cgdb_migrations.run_migrations` 在 schema 版本升级时原地 ALTER 表，保留数据。Schema 版本：4。检查方式：`cgdb_index_status` MCP 工具报告每文件每层行数。

`SQLiteStore.SCHEMA_VERSION`（当前 **12**，在同一 db 的遗留表侧）跟踪非-cgdb schema。v9-v12 新增：

- **kb_paragraphs**（Phase 1）— 跨 `memory/*.json` + `knowledge/*.md` 的统一 FTS5+BM25 索引；替代逐存储的 Jaccard / 子串搜索。可通过 `kb-rebuild-index` 重建。
- **kb_paragraphs_fts** — title/body/tags 的 FTS5 虚拟表（porter + unicode61 分词器），带 AI/AD/AU 触发器。
- **scope_id / canonical_id / principle_ref** 列（Phase 4）— 聚类 + 跨类型链接。
- **embedding BLOB** 列（Phase 5）— 可选的 384 维 float32 语义搜索；sentence-transformers 不可用时为 NULL。
- **kb_items**（Phase 6）— fact 级表，带 versions_json、decay_class、provenance_commit、provenance_operator；长期替代 kb_paragraphs（迁移期间两者共存）。
- **kb_items_fts** — kb_items 的 FTS5。
- **kb_query_log**（Phase 9）— 记录每次 `kb-query` 调用，供 feedback loop 分析；驱动 `kb-known-unknowns`。

### 知识库表（Schema v9-v12）

| 表 | 用途 | Phase |
|---|---|---|
| `kb_paragraphs` | 跨 memory + knowledge 的统一 FTS5 索引（派生；可重建） | 1 |
| `kb_paragraphs_fts` | FTS5 虚拟表（porter + unicode61） | 1 |
| `kb_items` | kb_paragraphs 的 fact 级后继（带 versions + provenance） | 6 |
| `kb_items_fts` | kb_items 的 FTS5 | 6 |
| `kb_query_log` | 查询反馈日志（matched, count, top_score, timestamp） | 9 |

位于 `~/.code2database_global_kb/global.db` 的全局 KB 有独立的
`kb_global` + `kb_global_fts` 表对，用于跨项目知识（Phase 8）。

### 遗留 ↔ cgdb 同步

`cgdb_sync.sync_legacy_and_cgdb()` 保持 `functions`/`edges`（遗留）与 `cgdb_nodes`/`cgdb_edges`（cgdb）同步。遗留表回答"谁调用谁"；cgdb 表回答强类型语义问题。两者共存于同一个 SQLite 数据库。

### 跨语言统一节点 ID

每种语言的每个节点 ID 都是 SHA-256 哈希截断到 60 位（高位清零以适配 SQLite 有符号 INTEGER），加语言前缀（`c:`、`go:`、`py:`、`java:`、`rust:`、`asm:`）。这防止跨语言冲突，同时保持 ID 紧凑。见 `_scanner/unified_id.py`。

## Globals 拆分

全局数据按类别拆分为独立文件，支持按域延迟加载：

- `.code2database_globals_enums.json` — 枚举成员
- `.code2database_globals_macros.json` — 宏定义
- `.code2database_globals_typedefs.json` — 类型定义

上下文包可选择性地包含相关全局数据。

## 插件/扩展架构

`build` 命令支持 `--plugin` 标志加载自定义 Python 脚本，同时自动发现 `.code2database_plugins/` 目录中的插件。

插件接口：

```python
class Code2DatabasePlugin:
    def enrich_functions(self, functions, edges, source_root):
        """构建前：修改 functions/edges 数据。返回 (functions, edges)。"""
        return functions, edges

    def enrich_graph(self, G):
        """构建后：修改 networkx DiGraph。返回 G。"""
        return G

    # 扩展钩子（可选）：
    def custom_scan(self, file_path, language, macro_bindings):
        """自定义扫描器，返回额外的 (functions, edges)。"""
        return [], []

    def custom_query(self, G, query_type, params):
        """自定义查询处理器，返回结果字典或 None（回退到默认处理）。"""
        return None

    def custom_output(self, G, output_dir, format_hint):
        """自定义输出生成。"""
        pass
```

插件添加的边必须设置：`confidence="INFERRED"`，`source="plugin:<name>"`，`confidence_score=0.7-0.95`。

插件配置：`--plugin my_plugin.py --plugin-config '{"threshold": 0.8}'`

插件验证：`code2database_builder.py validate-plugin --plugin my_plugin.py`
