---
name: Code2Database
description: "将代码工程升级为可查询的代码数据库。一次扫描 C/C++/Go/Python/Java/Rust/ASM，生成持久的带向调用图，含调用顺序、调用条件、条件编译路径(#ifdef)、并发分析、数据竞争检测、字段级访问追踪、置信度分类和来源审计。一次工具调用查询，替代 grep/glob/Read。能力：双 clang + tree-sitter 后端（auto/clang/tree-sitter，通过 --extraction-backend；libclang 推荐，非必装）、基于提交的来源追踪（git 哈希校验）、Cypher 子集查询、值流（DATA_FLOW 边）、锁持有区域分析、Z3 路径可行性、跨函数数据依赖、不变量提取（前置/后置/循环不变量/状态机）、LLM 自动语义增强（置信度阈值自动写入）、事务性更新（WAL + 快照 + fcntl 锁）、跨语言 FFI 追踪（Python ctypes / Go cgo / Rust extern C）、交互式 Web UI、BUG 基准测试、profile 健康度评分与自动演化（绑定 git/svn HEAD）、文档-代码双源真相对齐、后台守护进程（inotify + Unix socket API）、cgdb（代码图谱数据库）层提供 19 个 cgdb_* MCP 工具直接查询 clang 衍生语义表（类型、CFG、数据流、ops 绑定、同步原语、配置谓词、时间旅行版本）。122 个 CLI 命令分为 3 个子技能：本核心技能（下方 15 个 Tier-1 高权重命令）、/Code2Database-analysis（深度语义分析）、/Code2Database-ops（图谱编辑 + 运维）。49 个 MCP 工具（30 code2database_* + 19 cgdb_*）。当用户询问调用关系/调用链/调用顺序/条件调用路径/条件编译影响/架构域结构/API入口/外部端点分类/并发风险/数据竞争/不变量/FFI 边界/文档-代码一致性，或要生成/可视化调用图时使用——特别是code2db-out/已存在时。不适用于：写单元测试/简单查单个文件/不涉及调用关系的一般性问题。"
trigger: /Code2Database
sub_skills: ["Code2Database-analysis", "Code2Database-ops"]
---

# /Code2Database

**从只读代码文本到一键查询的代码数据库。** 扫描 C/C++/Go/Python/Java/Rust/ASM 代码工程，生成带调用顺序、调用条件和条件编译路径的有向调用图。一次扫描 → 持久图谱 → 精准查询 → 更少工具调用 → 更快回答。

这是**核心**子技能——始终加载。覆盖**构建 + 浏览**工作流（下方速查表显示 15 个高权重命令）。深度语义分析（并发、数据流、不变量、FFI、来源、路径可行性、cgdb 表）请激活 `/Code2Database-analysis`。图谱编辑、事务、守护进程、profile/文档-代码、导出、插件、记忆请激活 `/Code2Database-ops`。见下方**子技能激活**节。

## 支持语言

C(.c .h .cpp .cc .cxx .hpp) | Go(.go) | Python(.py .pyw) | Java(.java) | Rust(.rs) | ASM(.s .S .asm)

语言通过文件扩展名自动检测，也可用 `--lang` 强制指定。ASM使用正则表达式扫描（无tree-sitter语法）。

## 提取后端

Code2Database 对 C/C++ 提取支持**双后端**：

- `auto`（默认）——已安装 libclang 时用 clang，否则回退 tree-sitter
- `clang`——强制 clang 后端（启用 cgdb 层；需 `pip install libclang==17.0.6`）
- `tree-sitter`——强制 tree-sitter 后端（无 libclang 依赖）

**libclang 是推荐项，非必装。** Tree-sitter-only 模式仍然完全可用——可扫描、构建、查询所有支持的语言。Clang 后端额外填充 cgdb（代码图谱数据库）层的类型化语义表（CFG、数据流、ops 绑定、同步原语、配置谓词），通过 18 个 `cgdb_*` MCP 工具暴露。

```bash
# 仅 tree-sitter（无 libclang）
python3 scripts/code2database_scanner.py scan --source /path --extraction-backend tree-sitter

# 双后端（auto——可用时用 clang）
python3 scripts/code2database_scanner.py scan --source /path --extraction-backend auto

# 强制 clang 后端（启用 cgdb 层）
python3 scripts/code2database_scanner.py scan --source /path --extraction-backend clang
```

## 何时激活

- 用户输入 `/Code2Database` 或询问调用关系/调用链/调用顺序/条件调用路径
- 存在 `code2db-out/` 目录时的架构/并发问题
- 需要定位bug、架构评审、影响分析且涉及多函数交互
- 安全漏洞分析（崩溃报告、空指针解引用、竞态条件）
- **任何原本需要跨多个文件 grep/glob/Read 的问题**——如果 code graph 已存在，优先查询它
- 关于"**为什么**"会有这次调用（条件、#ifdef）、两条链"**是否**"会竞争、 "**谁**"触碰了某字段的问题——这些从图谱可以回答，从源码不行
- 不变量查询（前置/后置/状态机） → `find-invariants`
- 跨语言 FFI 追踪（Python→C、Go→C、Rust→C） → `ffi-trace`
- 约束下的路径可行性 → `path-feasible`
- 文档-代码一致性（"文档说X，代码做Y"） → `doc-code-check`
- 重要查询前的守护进程新鲜度检查 → `daemon-status` / `daemon-wait-sync`
- profile 健康度 / 新回调模式检测 → `profile-health` / `profile-evolve`
- 声明式图谱查询（Cypher 子集） → `query "MATCH ... WHERE ... RETURN ..."`
- 提交级来源追踪（"哪个 commit 引入了这个 bug？"） → `blame-node` / `describe-commit`

## 为什么是代码数据库，不只是调用图

多数代码图谱工具止步于"函数调用函数"。Code2Database 走得更深：

| 你问 | 图谱回答 |
|------|---------|
| "哪些函数访问 `task_struct->pid`？" | `field-access`——跨整张图，无需 grep |
| "这两条调用链会竞争吗？" | `concurrency-analyze`——线程模型 + 共享状态 + 锁分析 |
| "改这个函数会影响什么？" | `blast-radius`——受影响的 API、测试、域 |
| "执行是怎么到达这个崩溃点的？" | `reverse-trace`——从入口到崩溃的所有路径 |
| "哪些调用只在 `CONFIG_SMP` 下存在？" | `extract-signals` + `resolve-chain --bindings` |
| "什么条件门控这次调用？" | 每条边带 `call_condition`（if/switch/#ifdef/三元） |
| "这条边确定吗？" | 每条边标注 `EXTRACTED` / `INFERRED` / `AMBIGUOUS` + 来源 |
| "这个函数强制了什么不变量？" | `find-invariants`——前置 / 后置 / 循环不变量 / 状态机 |
| "哪个 Python 函数通过 ctypes 调到 C？" | `ffi-trace`——跨语言 FFI 追踪（Python/Go/Rust → C） |
| "这条调用路径在这些约束下可行吗？" | `path-feasible`——Z3 SMT 求解器，可靠性更高 |
| "文档和代码一致吗？" | `doc-code-check`——检测返回值 / 参数 / 签名 / 文档陈旧不匹配 |
| "守护进程是最新的吗？" | `daemon-status`——上次同步时间、待处理事件、陈旧节点 |
| "哪个 commit 引入了这个 bug？" | `blame-node` / `describe-commit`——提交级来源（git 哈希，非时间戳） |
| "哪些函数持有这把锁？" | `lock-coverage`——精确锁持有区间，事件流 + 字符位置 |
| "这个 NULL 从哪来？" | `value-flow`——参数→返回值跨函数传播 |
| "我的 profile 还有效吗？" | `profile-health`——7 个维度 0-100 分 |

这是 *调用图* 和 *可以问真实工程问题的代码数据库* 之间的差距。

## 工作流程

```
[0] 检查前置条件 → [0b] 自动检测项目规模 → [1] AST扫描 → [2] 语义增强
    → [2b] 函数指针/vtable解析 → [3] 构建图 → [3b] 域规范化
    → [3c] 端点分类 → [3d] 一致性校验 → [4] 查询/回答
                                                                      ↑
                                            [守护进程] 自动刷新循环
                                                  ↓ 包裹在
                                            [事务性同步] 快照 + WAL
                                                  ↓ 触碰
                                            输出文件 + .code2database_freshness.json
```

ASM文件使用正则表达式扫描（非tree-sitter），支持NASM x86_64、GNU as `.S`和ARM `bl`/`blr`指令。

构建步骤同时执行：锁覆盖事件流提取、不变量提取、FFI 绑定检测、文档-代码对齐、提交来源绑定到 `git/svn HEAD`。守护进程在构建后监视源文件，通过事务推送变更。

## 命令速查 — Tier 1 高权重命令

下方 15 个命令覆盖构建 + 浏览工作流。这是约 80% 场景下你需要的命令。深度分析或运维操作请激活子技能（见下方**子技能激活**节）。

| 命令 | 用途 |
|------|------|
| `scan` | AST扫描源码 → `.code2database_extraction.json` |
| `auto-profile` | 自动生成项目profile |
| `build` | 从extraction数据构建调用图 |
| `update` | 增量更新（重扫描变更文件） |
| `quick-update` | 快速更新，带stale比例自动阈值 |
| `explore-flow` | 一键自然语言查询 → 相关节点+路径 |
| `describe-node` | 分级节点描述（brief/standard/full）；暴露 `doc_code_mismatches`、`preconditions`、`postconditions`、`loop_invariants`、`state_machine`、`auto_fill_request` |
| `search` | 节点关键词搜索 |
| `load` | 加载图概览/摘要 |
| `get-code-snippet` | 获取节点的源码片段（`--persist` 写回body_text，**需用户确认**） |
| `trace-chain` | 正向追踪从A到B的路径 |
| `reverse-trace` | 从崩溃点反向BFS到入口点 |
| `key-paths` | 自动提取关键执行路径 |
| `query` | Cypher 子集查询（MATCH/WHERE/RETURN） |
| `daemon-status` | 获取守护进程状态（pid、last_sync、待处理事件、陈旧节点）——新鲜度检查 |

## 子技能激活

对于 Tier-1 之外的命令，激活以下两个按需子技能之一。每个子技能有自己的 SKILL.md，内含自己的 Tier-1 表 + 路由表。

### `/Code2Database-analysis` — 深度语义分析

当用户询问以下问题时激活：

- **并发安全 / 数据竞争** → `concurrency-analyze`、`detect-races`、`lock-coverage`、`happens-before`、`memory-ordering`、`who-locks`
- **值 / 参数流** → `value-flow`、`param-flow`、`data-dep`、`data-lifecycle`、`io-path`
- **约束下路径可行性** → `path-feasible`、`resolve-chain`、`extract-signals`
- **不变量** → `extract-invariants`、`find-invariants`、`apply-invariants`
- **FFI 边界** → `ffi-detect`、`ffi-list`、`ffi-trace`、`ffi-types`
- **提交级来源** → `blame-node`、`describe-commit`、`node-history`、`graph-provenance`、`find-commits`
- **资源生命周期** → `who-allocates`、`who-frees`、`unbalanced-alloc-free`
- **字段级访问** → `field-access`、`blast-radius`、`impact`、`neighbors`、`path`、`diff-chains`
- **直接查询 cgdb 表**（clang 后端） → 18 个 `cgdb_*` MCP 工具（search_symbols、find_invokers、find_invoked、get_definition、get_function_body、get_struct_layout、find_type_definition、find_ops_impls、find_cfg_paths、find_data_flow、find_aliases、find_lock_held_calls、check_race_condition、find_configs_for、find_nodes_under_config、index_status、time_travel_query、list_versions）

**移交短语**：*"激活 `Code2Database-analysis` 子技能。"* 然后加载 `~/.claude/skills/Code2Database-analysis/SKILL.md`。

### `/Code2Database-ops` — 图谱编辑 + 运维

当用户询问以下问题时激活：

- **安全图谱编辑** → `tx-begin`/`tx-commit`/`tx-rollback`/`tx-status`、`tx-snapshot`/`tx-restore`/`tx-list-snapshots`/`tx-replay-wal`、`update-node`、`update-edge`、`patch-profile`、`classify-endpoints`、`auto-enhance`、`batch-confirm`、`rollback`、`fill-request`、`add-semantic-edges`、`semantic-status`、`audit-log`
- **保持图谱新鲜** → `daemon-start`/`stop`/`force-refresh`/`pause`/`resume`/`wait-sync`/`logs`/`reload`/`list-projects`、`watch`、`sync`、`merge`、`light-scan`、`patch-from-diff`、`patch-from-git`、`install-hook`、`export-changes`、`merge-changes`
- **profile 与文档-代码** → `profile-health`、`profile-evolve`、`profile-bind-version`、`doc-code-check`、`doc-mark-stale`、`doc-alignment-report`、`doc-signature-diff`
- **图谱版本** → `graph-history`、`graph-diff`、`graph-record-version`
- **记忆管理** → `save-memory`、`search-memory`、`manage-memory`、`memory-health`、`validate-memory`
- **导出 / 插件 / 基准** → `export-html`、`export-obsidian`、`web-ui`、`plugins`、`validate-plugin`、`bug-benchmark`
- **Embeddings（实验性）** → `embeddings-build`、`embeddings-search`
- **MCP 服务器** → `serve`（48 个工具：30 个 `code2database_*` + 18 个 `cgdb_*`）

**移交短语**：*"激活 `Code2Database-ops` 子技能。"* 然后加载 `~/.claude/skills/Code2Database-ops/SKILL.md`。

### 何时不移交

如果问题只是简单的调用关系查询（"谁调用 X？"、"Y 调用什么？"、"显示从 A 到 B 的路径"），用上方 Tier-1 命令回答即可——无需激活子技能。子技能用于**专门**工作负载。

## MCP 服务器

```bash
python3 scripts/code2database_builder.py serve --graph code2db-out/
```

通过 stdio 传输暴露 **48 个 MCP 工具**（30 个 `code2database_*` + 18 个 `cgdb_*`）供 LLM agent 实时查询。无论哪个子技能激活，全部 48 个工具都可访问——MCP 与技能层激活分离。

## 核心工作流步骤

### 第0步 — 前置条件

如果 `code2db-out/code2database_master.json` 已存在且非重建请求，跳到第4步。

若无profile，先运行 `auto-profile`。按需安装依赖（networkx、tree-sitter语言绑定；ASM无需额外依赖；可选 `z3-solver` 用于可靠的路径可行性）。

### 第1步 — 扫描

```
python3 "$SKILL_DIR/scripts/code2database_scanner.py" scan \
  --source SOURCE_PATH --lang auto \
  --output code2db-out/.code2database_extraction.json
```

扫描器提取：functions、edges、domains、fn_ptr_calls、struct_types、includes。ASM扫描器使用正则提取call/jmp/bl/blr。C/C++中的内联`__asm__`块也会被提取（INFERRED置信度）。

### 第2步 — 语义增强与函数指针解析

补充AST遗漏（函数指针、跨文件调用、条件调用）。函数指针/vtable派发解析在构建步骤中自动执行。详见 `references/semantic_enhancement.md`。

### 第3步 — 构建

```
python3 "$SKILL_DIR/scripts/code2database_builder.py" build \
  --extraction code2db-out/.code2database_extraction.json \
  --outdir code2db-out/ [--storage json|sqlite|auto]
```

域规范化、一致性校验、锁覆盖提取、不变量提取、FFI 检测、文档-代码对齐、提交来源绑定均已内置在构建步骤中。大项目（>50K函数）使用 `--storage auto` 选择SQLite。

### 第4步 — 查询

采用**全局→本地**模式：先读 `context_pack_lite`，再用分级查询钻取。见下方**查询路由指南**。

## 查询路由指南

| 问题类型 | 推荐查询路径 | 说明 |
|---------|-------------|------|
| 项目整体架构 | context_pack_micro → context_pack_lite → CODE2DATABASE_SUMMARY.md | 先看micro（~200t），再看lite获取详情 |
| 特定函数/模块 | explore-flow → describe-node → get-code-snippet | 一键定位→详情→源码 |
| 调用链追踪 | trace-chain → resolve-chain | 从A到B完整路径，带条件标注 |
| 并发风险 | concurrency-risks → concurrency-analyze → describe-node --context | 全局风险→具体并发对→函数详情 |
| 数据竞争 | detect-races → field-access | 检测跨线程竞争→字段级访问 |
| 锁覆盖 | lock-coverage → concurrency-analyze | 精确锁持有区间 → 成对安全分析 |
| 崩溃调试 | reverse-trace → trace-chain | 从崩溃点反向追踪→正向追踪具体链路 |
| 影响分析 | blast-radius / impact --lite | 受影响的API/测试/域 |
| 条件编译影响 | extract-signals → resolve-chain / diff-chains | 信号映射→解析或对比路径 |
| 数据生命周期 | data-lifecycle → field-access | 资源分配-使用-释放→字段访问者 |
| 值流 / NULL 来源 | value-flow → param-flow → describe-node | 追踪参数→返回值传播 → 参数流 → 接收方详情 |
| 数据依赖 | data-dep → field-access | 跨函数读写扫描 → 字段级访问 |
| 路径可行性 | trace-chain → path-feasible | 取得路径 → 在约束下用 SMT 检查可行性（Z3 或启发式回退） |
| 代码变更管理 | quick-update / install-hook | 一键更新；hook实现自动更新 |
| 知识验证 | knowledge-validate | 检查知识文件质量 |
| 知识查询 | knowledge-query → memory search | 先查知识，再查记忆 |
| 安全/漏洞分析 | reverse-trace → detect-races → concurrency-analyze | 崩溃路径→竞争检测→并发详情 |
| 函数指针派发追踪 | explore-flow → describe-node --context | 定位派发点→查看实现 |
| I/O路径分析 | io-path | 从函数追踪I/O路径 |
| 不变量推理 | find-invariants → describe-node --invariants | 按模式匹配不变量 → 审视前置/后置/循环不变量/状态机 |
| 跨语言 FFI | ffi-detect → ffi-list → ffi-trace → ffi-types | 检测绑定 → 列出位置 → 追踪跨语言链 → 检视类型映射 |
| 文档-代码不匹配 | doc-code-check → doc-signature-diff → doc-mark-stale | 检测不匹配 → 差分签名 → 必要时标记文档陈旧 |
| 守护进程健康 | daemon-status → daemon-wait-sync → daemon-logs | 检查状态 → 阻塞至同步完成 → 异常时查看日志 |
| profile 健康度 | profile-health → profile-evolve → profile-bind-version | 7 类评分 → 应用 EXTRACTED 建议 → 重新绑定 HEAD |
| 提交来源 | blame-node → describe-commit → node-history | 找到引入提交 → 显示所有变更 → 节点历史 |
| 声明式图谱查询 | query "MATCH ... WHERE ... RETURN ..." | Cypher 子集；用于专用命令未覆盖的一次性结构化查询 |

**禁止直接Read输出文件（.json/.md），必须通过查询命令获取数据。** 查询命令返回精确、精简结果；直接读取浪费token且可能过时。

## 约束

- **查询优先级**：context_pack_micro → context_pack_lite → explore-flow → knowledge_pack_lite → memory_pack_lite → describe-node
- **优先使用 describe-node** 获取函数信息，不再需要读取源文件
- **需要判断分支时**用 `resolve-chain` + bindings 或查看 scenarios
- **参数流追踪**用 `describe-node` 的 `param_flow` 字段
- **全局→本地模式**：始终从micro/lite包开始，再逐步下钻
- 引用函数时标注：函数名、源文件路径:行号、架构域
- 追踪调用链时**完整走完**，不可跳过中间节点
- 条件调用必须标注 `call_condition`
- API_entry函数需附带 `api_constraints`
- out_end/unknown_end需注明 `external_desc` 或标注需人工确认
- 不确定的信息标注为推测，建议用户验证
- 每次查询读取的源文件不超过10个
- **禁止预加载** profile模板（`config/profiles/`）和reference文档——仅按需读取
- 仅支持七种标签（详见 `references/label_rules.md`），不可自行新增
- 标签识别遵循三层流程：AST启发式 → LLM补充 → 用户确认
- 外部代码域自动识别（vendor/third_party/contrib）→ `external_*`
- 回调注册处必须画边到实际回调函数
- 端点分类走完整流水线（详见 `references/endpoint_pipeline.md`）
- 跨Skill协作按能力关键词动态匹配（详见 `references/cross_skill_collaboration.md`）
- 边置信度必须标注（EXTRACTED/INFERRED/AMBIGUOUS + source）
- 插件添加的边不可冒充EXTRACTED
- **函数指针边强制**：每个fn_ptr调用必须至少有一条 `callback_dispatch` 边
- **条件分支调用自动提取**：if/while/for/switch/三元条件内的调用自动提取并标注 `call_condition`
- **一致性校验**：已内置在构建步骤中；若发现不一致请重新运行build
- **ASM扫描器使用正则**（非tree-sitter）；ASM边始终为INFERRED置信度
- 找到根因之前不提修复方案
- sync/update后必须验证
- **自动检测大项目模式**：从函数计数自动检测；无需手动 `--large-project`
- **域规范化**：始终应用domain_rules和去重
- **事务性写入**：多步数据库修改需包裹 `tx-begin`/`tx-commit`。`patch-from-diff`/`patch-from-git` 默认已包裹；用 `--no-transaction` 绕过。`tx-rollback` 中止；`tx-replay-wal` 崩溃恢复
- **守护进程新鲜度**：重要查询前调用 `daemon-status`；若 `syncing` 或 `pending_events > 0`，调用 `daemon-wait-sync` 阻塞至同步完成。断路器在事件率超过 1000/分钟时触发整体重建
- **文档-代码对齐**：`describe-node` 暴露 `doc_code_mismatches`——若非空，`semantic_desc` 可能不可靠；查阅 `body_text` 并考虑 `doc-mark-stale` 直到文档重新提取
- **不变量置信度**：禁止应用 AMBIGUOUS 不变量；INFERRED 不变量在 `apply-invariants` 前**需用户确认**。状态机提取阈值为 `assign_counts[state_var] >= 1`
- **FFI 追踪**：需要源语言和目标语言扫描器同时检测到绑定位置（如 Python ctypes 源 + C 目标）。类型映射可能有损——`ffi-types` 标记有损转换
- **profile 演化**：`profile-evolve --apply` 只应用 EXTRACTED 置信度建议；INFERRED **需用户确认**。演化后运行 `profile-bind-version` 绑定 git/svn HEAD
- **提交来源**：每个节点/边带 `commit_meta.source_commit`（git/svn 哈希）。用 `git show <hash>` 校验，而非时间戳。`blame-node` 找到引入提交；`node-history` 显示演化
- **路径可行性**：`path-feasible` 仅在安装 Z3 时保证可靠（`pip install z3-solver`）；无 Z3 时启发式回退可能误报——结果标注为临时
- **值流**：DATA_FLOW 边追踪参数→返回值传播；若某节点缺少 DATA_FLOW 边，链路断裂——不得无证据推断流

## 数据库写入约束（重要）

LLM 执行任何修改 code graph database的命令时，**必须先获得用户确认**，再执行写入。这是为了防止 LLM 幻觉把错误信息导入数据库，违反"内容可缺少但必须准确正确"的核心原则。

**需用户确认的命令**（默认会弹出 y/N 提示）：

- `update-node` — LLM 增量补充节点属性
- `update-edge` — LLM 增量补充/纠正边属性
- `patch-profile` — LLM 增量校准 auto-profile
- `apply-semantics` — 应用 LLM 填写的语义描述到图
- `classify-endpoints` — 应用 LLM 端点分类结果
- `manage-memory --action add/correct/reshape/promote/refine` — 写入持久记忆
- `save-memory` — 保存 Q&A 记忆
- `apply-invariants` — 应用提取的不变量；**AMBIGUOUS 永不应用**；INFERRED 需确认；EXTRACTED 自动应用
- `auto-enhance` — LLM 自动语义增强；EXTRACTED+证据自动写入；**INFERRED 需确认**；AMBIGUOUS 拒绝
- `batch-confirm` — 批量确认待处理的 INFERRED 增强
- `profile-evolve --apply` — 应用 EXTRACTED 置信度的 profile 建议；**INFERRED 需确认**
- `doc-mark-stale` — 标记某节点文档为陈旧（非破坏性但可见）
- `ffi-types` — 更新某条 FFI 边的类型映射表
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

## 参考文档索引

需要详情时按需读取以下文档——**禁止一次性全部加载**：

| 文档 | 内容 |
|------|------|
| `references/usage_reference.md` | Tier-1 命令的完整语法、参数、代码块、输出格式 |
| `references/label_rules.md` | 标签定义和分类规则 |
| `references/data_model.md` | 节点/边属性、上下文包层级、cgdb 表 schema |
| `references/semantic_enhancement.md` | 语义提取和增强详情 |
| `references/endpoint_pipeline.md` | 端点分类流水线 |
| `references/cross_skill_collaboration.md` | 跨Skill协作协议 |
| `references/memory_knowledge.md` | 记忆和知识管理详情 |
| `references/json_schema.md` | `code2database_master.json` 完整 JSON schema |
| `references/usage_examples.md` | 常见场景的查询示例 |

**子技能参考文档**（仅在对应子技能激活时加载）：

| 文档 | 内容 |
|------|------|
| `~/.claude/skills/Code2Database-analysis/references/analysis_commands.md` | Tier-2 分析命令（并发、数据流、不变量、FFI、来源、cgdb MCP 工具） |
| `~/.claude/skills/Code2Database-ops/references/ops_commands.md` | Tier-3 运维命令（事务、守护进程、profile、文档-代码、导出、插件、记忆、embeddings） |

运行时调优参数（invariants、auto_enhance、transactions、ffi、web_ui、benchmark、profile_health、doc_code、daemon 各节）详见 `RUNTIME_CONFIG.md`。

Profile 编写（skip_names、callback_detection、struct_op_types、registration_macros、domain_rules、threading_models）详见 `PROFILE_MANUAL.md`。

**内部文件**（禁止加载到agent上下文）：`OVERVIEW.md`、`scripts/`、`config/profiles/`。这些是工具开发者的实现细节，使用时不需要。
