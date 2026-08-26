# 分析命令参考（Tier 2）

本文档覆盖 `/Code2Database-analysis` 子技能暴露的 50 个分析命令和 19 个 `cgdb_*` MCP 工具的完整语法。**按需读取**——除非需要某命令的详细语法，否则不要加载到 agent 上下文。

命令按问题类型分组，与 `SKILL_analysis.md` 中的路由表对应。

## 并发安全

### `concurrency-risks`

列出构建阶段检测到的全局并发风险热点。

```bash
python3 scripts/code2database_builder.py concurrency-risks --graph code2db-out/ [--threshold high|medium|low]
```

输出：按风险评分排序的函数列表，含共享状态数、锁数、线程模型标注。

### `concurrency-analyze`

两个函数或两条调用链之间的成对并发安全分析。

```bash
python3 scripts/code2database_builder.py concurrency-analyze \
  --graph code2db-out/ \
  --fn-a function_a --fn-b function_b \
  [--shared-state VAR_NAME] [--json]
```

输出：每个函数的线程模型、共享状态交集、锁持有重叠、竞争判定（SAFE / RISKY / RACY）、证据链。

### `detect-races`

跨线程数据竞争检测（全图或某子集）。

```bash
python3 scripts/code2database_builder.py detect-races \
  --graph code2db-out/ \
  [--scope function_name | --scope domain_name] \
  [--json]
```

输出：竞争对列表，含变量、访问函数、线程模型、锁状态、证据（源码字符位置）。

### `lock-coverage`

锁持有区域分析，事件流输出 + 精确字符位置。

```bash
python3 scripts/code2database_builder.py lock-coverage \
  --graph code2db-out/ \
  --function function_name \
  [--json]
```

输出：每把锁的事件流（line:col 获取、line:col 释放）、未覆盖段（无锁执行的代码）、嵌套锁警告。

### `happens-before`

计算两个跨线程事件之间的 happens-before 关系（启用 clang 后端时使用 cgdb sync_primitives + happens_before 表）。

```bash
python3 scripts/code2database_builder.py happens-before \
  --graph code2db-out/ \
  --event-a "function_a:line:col" \
  --event-b "function_b:line:col" \
  [--json]
```

输出：有序 / 并发 / 不确定 判定、排序边、证据。

### `memory-ordering`

分析内存序约束（原子操作、内存屏障、READ_ONCE / WRITE_ONCE、smp_mb）。

```bash
python3 scripts/code2database_builder.py memory-ordering \
  --graph code2db-out/ \
  --function function_name \
  [--json]
```

输出：排序点列表，含位置、操作类型、序强度（relaxed / acquire / release / seq_cst）、配对屏障。

### `who-locks`

查找所有获取某锁变量的函数。

```bash
python3 scripts/code2database_builder.py who-locks \
  --graph code2db-out/ \
  --lock LOCK_VAR_NAME \
  [--json]
```

输出：函数列表，含获取位置、调用上下文、线程模型。

## 值 / 参数流

### `value-flow`

通过 DATA_FLOW 边跨函数追踪参数→返回值传播。

```bash
python3 scripts/code2database_builder.py value-flow \
  --graph code2db-out/ \
  --start function_name [--param PARAM_NAME] \
  --end function_name \
  [--max-depth N] [--json]
```

输出：传播步骤链，含函数、参数/返回槽、转换、证据。

### `param-flow`

跨函数追踪参数传递路径。

```bash
python3 scripts/code2database_builder.py param-flow \
  --graph code2db-out/ \
  --start function_name --param PARAM_NAME \
  [--end function_name] \
  [--max-depth N] [--json]
```

输出：传播树，含参数槽、调用边、修改标注。

### `data-dep`

跨函数数据依赖（DATA_DEP 边）。扫描所有节点（不止一条链）。

```bash
python3 scripts/code2database_builder.py data-dep \
  --graph code2db-out/ \
  --var VAR_NAME \
  [--scope function_name | --scope domain_name] \
  [--json]
```

输出：变量的写入者和读取者，含跨函数边和证据。

### `data-lifecycle`

资源分配-使用-释放追踪。

```bash
python3 scripts/code2database_builder.py data-lifecycle \
  --graph code2db-out/ \
  --resource RESOURCE_NAME \
  [--json]
```

输出：分配点、使用点、释放点、不平衡警告。

### `io-path`

从函数追踪 I/O 路径。

```bash
python3 scripts/code2database_builder.py io-path \
  --graph code2db-out/ \
  --function function_name \
  [--max-depth N] [--json]
```

输出：从该函数可达的 I/O 操作（read/write/ioctl/mmap）链。

## 影响 / 爆炸半径

### `impact`

影响分析：某函数的受影响调用者和被调用者。

```bash
python3 scripts/code2database_builder.py impact \
  --graph code2db-out/ \
  --function function_name \
  [--direction invokers|invoked|both] \
  [--max-depth N] [--lite] [--json]
```

输出：受影响节点树，深度受限。`--lite` 仅返回计数。

### `blast-radius`

计算某函数变更受影响的 API、测试、域。

```bash
python3 scripts/code2database_builder.py blast-radius \
  --graph code2db-out/ \
  --function function_name \
  [--json]
```

输出：受影响的 API_entry 函数、测试函数、触及的域。

### `neighbors`

获取节点的邻居（调用者、被调用者或两者）。

```bash
python3 scripts/code2database_builder.py neighbors \
  --graph code2db-out/ \
  --function function_name \
  [--direction invokers|invoked|both] \
  [--json]
```

### `path`

查找两节点间路径。

```bash
python3 scripts/code2database_builder.py path \
  --graph code2db-out/ \
  --from function_a --to function_b \
  [--max-depth N] [--json]
```

### `diff-chains`

对比两种宏配置下的调用路径。

```bash
python3 scripts/code2database_builder.py diff-chains \
  --graph code2db-out/ \
  --from function_a --to function_b \
  --config-a CONFIG_A --config-b CONFIG_B \
  [--json]
```

输出：仅在 A、仅在 B、两者都有的路径，带条件标注。

## 路径可行性

### `path-feasible`

Z3 SMT 约束下路径可行性。安装 Z3 时可靠；否则启发式回退。

```bash
python3 scripts/code2database_builder.py path-feasible \
  --graph code2db-out/ \
  --from function_a --to function_b \
  [--constraint "VAR == VALUE" ...] \
  [--solver z3|heuristic] \
  [--json]
```

输出：可行 / 不可行 判定、unsat core（若 Z3）、model（若可行）、证据链。若用启发式，结果标注为**临时**。

`--node` 模式从某节点遍历所有路径，累积条件，对每条路径运行：
1. Z3 / 启发式可行性（`solve_path_feasibility`）
2. 配置谓词可行性（`check_config_feasible`，需 `--with-configs`）
3. **运行时守卫分析**（`check_runtime_guards_with_profile`，修复 #6）— 检测 acquire/release 区间、类型/身份/锁状态谓词。提供 `--profile /path/to/profile.json` 时，profile 声明的 `guard_functions` 会补充（而非替代）内置正则模式；输出的 `runtime_guards.profile_bindings` 字段列出从 profile 守卫推导的绑定（如 `{"sb_type": "blkdev"}`）。

```bash
# 直接条件列表 + profile 守卫
python3 scripts/code2database_builder.py path-feasible \
  --graph code2db-out/ \
  --conditions "if(!sb_is_blkdev_sb(sb))" \
  --profile /path/to/profile.json

# 节点遍历模式 + profile 守卫
python3 scripts/code2database_builder.py path-feasible \
  --graph code2db-out/ \
  --node ext4_blkdev_getblock \
  --max-depth 8 \
  --profile /path/to/profile.json
```

profile schema 见 PROFILE_MANUAL.md §3.15 `guard_functions`。

### `path-guards`

用守卫条件证明 writer 从入口可达。从 `--from` 遍历所有路径到 `--to`，累积守卫条件（来自边的 `call_condition` + 目标字段写的函数体 `guard_condition`），用 Z3/启发式证明守卫合取式是否可满足。若所有路径都不可行（守卫矛盾），则 writer 在场景中不可达。

```bash
python3 scripts/code2database_builder.py path-guards \
  --graph code2db-out/ \
  --from ext4_blkdev_getblock \
  --to __bread_gfp \
  --field b_bdev \
  [--value NULL] \
  [--max-depth 8] \
  [--with-configs "CONFIG_X=true"] \
  [--profile /path/to/profile.json]
```

`--profile` 参数（修复 #6）启用 profile 声明的 `guard_functions`（如项目专属类型谓词 `sb_is_blkdev_sb`、锁 acquire/release `bd_prepare_to_claim`/`bd_abort_claim`）。输出的 `runtime_guards.profile_bindings` 字段展示推导出的绑定。

### `resolve-chain`

带宏绑定的条件链解析。

```bash
python3 scripts/code2database_builder.py resolve-chain \
  --graph code2db-out/ \
  --from function_a --to function_b \
  --bindings CONFIG_MACRO=1,OTHER=0 \
  [--json]
```

输出：解析后的路径，每条边带在绑定下求值的 `call_condition`。

### `extract-signals`

映射 `#ifdef` 条件到受影响的函数和边。

```bash
python3 scripts/code2database_builder.py extract-signals \
  --graph code2db-out/ \
  [--signal CONFIG_MACRO] \
  [--json]
```

输出：信号映射——每个 `#ifdef` 宏，列出受其门控的函数和边。

## 不变量

### `extract-invariants`

从函数体提取前置条件、后置条件、循环不变量、状态机。

```bash
python3 scripts/code2database_builder.py extract-invariants \
  --graph code2db-out/ \
  --function function_name \
  [--json]
```

输出：不变量列表，含类型（precondition / postcondition / loop_invariant / state_machine）、表达式、置信度（EXTRACTED / INFERRED / AMBIGUOUS）、证据。

### `find-invariants`

跨图查找匹配某模式的不变量。

```bash
python3 scripts/code2database_builder.py find-invariants \
  --graph code2db-out/ \
  --pattern "kind=precondition,var=PTR_NAME" \
  [--json]
```

### `apply-invariants`

应用提取的不变量到图。**AMBIGUOUS 永不应用；INFERRED 需用户确认；EXTRACTED 自动应用。**

```bash
python3 scripts/code2database_builder.py apply-invariants \
  --graph code2db-out/ \
  --function function_name \
  [--confidence EXTRACTED|INFERRED] \
  [--yes]  # 不推荐——绕过确认
```

**数据库写入约束**：应用 INFERRED 不变量前必须获得用户确认。见 SKILL.md 数据库写入约束节。

## FFI 追踪

### `ffi-detect`

跨代码库检测 FFI 绑定（Python ctypes、Go cgo、Rust extern "C"）。

```bash
python3 scripts/code2database_builder.py ffi-detect \
  --graph code2db-out/ \
  [--json]
```

输出：FFI 绑定位置列表，含源语言、目标语言、绑定类型。

### `ffi-list`

列出所有 FFI 绑定位置及详情。

```bash
python3 scripts/code2database_builder.py ffi-list \
  --graph code2db-out/ \
  [--source-lang python|go|rust] \
  [--json]
```

### `ffi-trace`

追踪跨语言调用链。

```bash
python3 scripts/code2database_builder.py ffi-trace \
  --graph code2db-out/ \
  --from function_name \
  [--max-depth N] [--json]
```

输出：跨越语言边界的链，标注 FFI 绑定点。

### `ffi-types`

显示某条 FFI 边的类型映射。**更新类型映射表需用户确认。**

```bash
python3 scripts/code2database_builder.py ffi-types \
  --graph code2db-out/ \
  --edge EDGE_ID \
  [--update]  # 需用户确认
  [--json]
```

## 提交来源

### `blame-node`

定位引入某节点的提交。

```bash
python3 scripts/code2database_builder.py blame-node \
  --graph code2db-out/ \
  --function function_name \
  [--json]
```

输出：commit 哈希、作者、日期、commit 消息、引入位置 file:line。

### `describe-commit`

显示某次提交引入的所有变更。

```bash
python3 scripts/code2database_builder.py describe-commit \
  --graph code2db-out/ \
  --commit HASH \
  [--json]
```

输出：该提交添加/删除/修改的节点和边列表。

### `node-history`

显示节点的提交历史（随时间演化）。

```bash
python3 scripts/code2database_builder.py node-history \
  --graph code2db-out/ \
  --function function_name \
  [--json]
```

### `graph-provenance`

显示图级别的来源摘要。

```bash
python3 scripts/code2database_builder.py graph-provenance \
  --graph code2db-out/ \
  [--json]
```

### `find-commits`

查找触及某函数或文件的提交。

```bash
python3 scripts/code2database_builder.py find-commits \
  --graph code2db-out/ \
  --function function_name | --file PATH \
  [--json]
```

## 资源生命周期

### `who-allocates`

查找所有分配某资源的函数。

```bash
python3 scripts/code2database_builder.py who-allocates \
  --graph code2db-out/ \
  --resource RESOURCE_NAME \
  [--json]
```

### `who-frees`

查找所有释放某资源的函数。

```bash
python3 scripts/code2database_builder.py who-frees \
  --graph code2db-out/ \
  --resource RESOURCE_NAME \
  [--json]
```

### `unbalanced-alloc-free`

查找不平衡的分配/释放对（潜在泄漏或双重释放）。

```bash
python3 scripts/code2database_builder.py unbalanced-alloc-free \
  --graph code2db-out/ \
  [--scope function_name] \
  [--json]
```

### `add-semantic-edges`

向图添加语义边（如 alloc→free 配对）。**需用户确认。**

```bash
python3 scripts/code2database_builder.py add-semantic-edges \
  --graph code2db-out/ \
  --kind alloc_free \
  --src function_a --dst function_b \
  [--yes]  # 不推荐
```

## 字段访问

### `field-access`

跨函数按字段追踪读/写访问。

```bash
python3 scripts/code2database_builder.py field-access \
  --graph code2db-out/ \
  --struct STRUCT_NAME --field FIELD_NAME \
  [--mode read|write|both] \
  [--json]
```

输出：读/写该字段的函数列表，含位置和上下文。

## 审计

### `audit-log`

查看图的历史写入审计日志。

```bash
python3 scripts/code2database_builder.py audit-log \
  --graph code2db-out/ \
  [--since TIMESTAMP] [--scope function_name] \
  [--json]
```

## 声明式查询

### `query`

Cypher 子集查询（MATCH/WHERE/RETURN）。

```bash
python3 scripts/code2database_builder.py query \
  --graph code2db-out/ \
  --query "MATCH (a)-[r:INVOKES*1..3]->(b) WHERE b.name='target' RETURN a, r" \
  [--json]
```

支持子句：`MATCH`（节点/边模式）、`WHERE`（含 `CONFIG(var, 'pred')` 过滤）、`RETURN`（投影）。完整 schema 见 `references/data_model.md`。

## 按需 / 低权重命令

### `think-chain`

完整调用链思考与结论——为复杂查询生成逐步推理轨迹。

```bash
python3 scripts/code2database_builder.py think-chain \
  --graph code2db-out/ \
  --question "这两条链会竞争吗？" \
  [--json]
```

### `intent-query`

意图驱动的图谱查询——自然语言意图解析为图操作。

```bash
python3 scripts/code2database_builder.py intent-query \
  --graph code2db-out/ \
  --intent "找出所有可能死锁的路径" \
  [--json]
```

### `extract-invariants-llm`

LLM 驱动的不变量提取（用 LLM 从函数体提议不变量）。

```bash
python3 scripts/code2database_builder.py extract-invariants-llm \
  --graph code2db-out/ \
  --function function_name \
  [--json]
```

### `explain-label`

解释某节点为何得到某标签（API_entry、thread_processor 等）。

```bash
python3 scripts/code2database_builder.py explain-label \
  --graph code2db-out/ \
  --function function_name \
  [--json]
```

### `why-ambiguous`

解释某边为何标记为 AMBIGUOUS。

```bash
python3 scripts/code2database_builder.py why-ambiguous \
  --graph code2db-out/ \
  --edge EDGE_ID \
  [--json]
```

### `extract-semantics` / `apply-semantics`

从文档提取语义描述并应用到图。`apply-semantics` **需用户确认**。

```bash
python3 scripts/code2database_builder.py extract-semantics \
  --graph code2db-out/ \
  --function function_name \
  [--doc-path PATH] [--json]

python3 scripts/code2database_builder.py apply-semantics \
  --graph code2db-out/ \
  --function function_name \
  [--yes]  # 不推荐——需用户确认
```

### `extract-knowledge` / `apply-knowledge` / `knowledge-query` / `knowledge-validate`

知识管理命令。`apply-knowledge` 需用户确认。

```bash
python3 scripts/code2database_builder.py extract-knowledge --graph code2db-out/ --topic TOPIC [--json]
python3 scripts/code2database_builder.py apply-knowledge --graph code2db-out/ --topic TOPIC [--yes]
python3 scripts/code2database_builder.py knowledge-query --graph code2db-out/ --topic TOPIC [--json]
python3 scripts/code2database_builder.py knowledge-validate --graph code2db-out/ --topic TOPIC [--json]
```

## cgdb MCP 工具（18 个，clang 后端）

这些工具直接查询 cgdb（代码图谱数据库）层。需要 clang 提取后端（`--extraction-backend clang` 或 auto 下已安装 libclang）。在 tree-sitter-only 模式下返回空结果。

### `cgdb_search_symbols`

FTS5 搜索 AST 节点（函数、类型、变量）。

```json
{"tool": "cgdb_search_symbols", "arguments": {"query": "mutex_lock", "limit": 20}}
```

### `cgdb_find_invokers`

通过 CGDB invoke_sites 表查找某函数的调用者。

```json
{"tool": "cgdb_find_invokers", "arguments": {"function": "mutex_lock"}}
```

### `cgdb_find_invoked`

查找某函数的被调用者。

```json
{"tool": "cgdb_find_invoked", "arguments": {"function": "my_function"}}
```

### `cgdb_get_definition`

获取某符号的定义节点。

```json
{"tool": "cgdb_get_definition", "arguments": {"symbol": "my_struct"}}
```

### `cgdb_get_function_body`

获取函数体源码范围（文件、起止 line/col）。

```json
{"tool": "cgdb_get_function_body", "arguments": {"function": "my_function"}}
```

### `cgdb_get_struct_layout`

获取 struct 字段布局（偏移、类型、名）。

```json
{"tool": "cgdb_get_struct_layout", "arguments": {"struct": "file_operations"}}
```

### `cgdb_find_type_definition`

按名查找类型定义。

```json
{"tool": "cgdb_find_type_definition", "arguments": {"type_name": "task_struct"}}
```

### `cgdb_find_ops_impls`

查找 ops 表实现（通过 FieldDecl → FunctionDecl 绑定的类型化 vtable 派发）。

```json
{"tool": "cgdb_find_ops_impls", "arguments": {"ops_field": "read_iter", "ops_var": ""}}
```

返回：`{ops_table_id, field_node_id, impl_function_id, signature_match}` 列表。

### `cgdb_find_cfg_paths`

在控制流图（L4 层）中查找路径。

```json
{"tool": "cgdb_find_cfg_paths", "arguments": {"function": "my_function", "from_block": 0, "to_block": 5}}
```

### `cgdb_find_data_flow`

在数据流分析（L5 层）中查找 def-use 链。

```json
{"tool": "cgdb_find_data_flow", "arguments": {"function": "my_function", "variable": "ptr"}}
```

### `cgdb_find_aliases`

查找某指针的别名（L6 层——MVP 阶段为 stub）。

```json
{"tool": "cgdb_find_aliases", "arguments": {"variable": "ptr"}}
```

### `cgdb_find_lock_held_calls`

查找在持有锁时发出的调用（L8 sync_primitives + happens_before）。

```json
{"tool": "cgdb_find_lock_held_calls", "arguments": {"lock": "my_mutex"}}
```

### `cgdb_check_race_condition`

检查某变量上的竞争条件。

```json
{"tool": "cgdb_check_race_condition", "arguments": {"variable": "shared_counter"}}
```

返回：访问函数列表，含线程模型、锁状态、竞争判定。

### `cgdb_find_configs_for`

查找影响某节点的配置谓词（L3.5 层）。

```json
{"tool": "cgdb_find_configs_for", "arguments": {"node_id": 12345}}
```

### `cgdb_find_nodes_under_config`

查找某配置谓词下的所有节点。

```json
{"tool": "cgdb_find_nodes_under_config", "arguments": {"predicate": "CONFIG_SMP"}}
```

### `cgdb_index_status`

显示 cgdb 索引状态（按文件、按层）。

```json
{"tool": "cgdb_index_status", "arguments": {}}
```

返回：每个 cgdb 表的每文件行数（cgdb_nodes、cgdb_edges、cgdb_basic_blocks、cgdb_cfg_edges、cgdb_data_flow、cgdb_sync_primitives、cgdb_happens_before、cgdb_predicates、cgdb_ops_bindings）。

### `cgdb_time_travel_query`

在过去版本查询图（L10 时间旅行）。

```json
{"tool": "cgdb_time_travel_query", "arguments": {"version": "v1.2.3", "node_id": 12345}}
```

### `cgdb_list_versions`

列出所有已记录的图谱版本。

```json
{"tool": "cgdb_list_versions", "arguments": {}}
```

## 另见

- `SKILL_analysis.md` — Tier-1 命令 + 路由表（子技能激活时始终加载）
- `references/data_model.md` — cgdb 表 schema、节点/边属性
- 父技能 `~/.claude/skills/Code2Database/references/usage_reference.md` — Tier-1 命令语法
