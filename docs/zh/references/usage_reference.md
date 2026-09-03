# 用法参考

Code2Database 的详细命令语法、参数和输出说明。

仅在需要特定命令详情时按需读取。无需时不加载到上下文。

> **这带来的转变**：下面的每条命令都是*对持久代码数据库的查询*，不是逐文件搜索。`explore-flow` 一次调用返回相关节点 + 路径（vs. N 次 grep/Read 往返）。`trace-chain` 返回带条件标注的 A→B（vs. 手动走调用点）。`detect-races` 返回跨线程隐患（vs. 阅读共享资源的每个调用者）。`field-access` 返回谁读写了某字段（vs. 全代码库 grep）。图谱是索引；命令是查询语言。

## 第0步 — 检查前置条件

**手动编写Profile**：详见 `docs/PROFILE_MANUAL.md`，包含完整字段说明、示例和编写流程。

检查 `code2db-out/code2database_master.json` 是否存在。已存在且非重建请求时跳到第4步。

**如果项目没有profile**（`code2db-out/.code2database_profile.json` 不存在且无 `--profile` 参数），先运行auto-profile：

```bash
python3 "$SKILL_DIR/scripts/code2database_scanner.py" auto-profile \
  --source SOURCE_PATH --outdir OUTPUT_DIR
```

auto-profile 自动检测项目类型、回调模式、注册宏、skip_names等，生成完整的profile JSON。

按需安装依赖：
```bash
python3 -c "import networkx; import tree_sitter_c; import tree_sitter_go; import tree_sitter_python; import tree_sitter_java; import tree_sitter_rust; import tree_sitter_cpp" 2>/dev/null || python3 -m pip install networkx tree-sitter tree-sitter-c tree-sitter-cpp tree-sitter-go tree-sitter-python tree-sitter-java tree-sitter-rust -q
```

注：ASM扫描不需要额外的tree-sitter依赖——使用内置正则模式。

## 第0b步 — 自动检测项目规模

对于超过10万函数的代码库，构建过程可能因内存不足而失败。自动检测逻辑在扫描后运行：

- 若 `total_functions > 50,000`：自动启用大项目模式（顺序处理、减少内存缓存、拆分输出）
- 打印通知：`"自动检测到大项目（N个函数），启用优化模式"`
- 自动应用：扫描的 `--split-output`、构建的顺序域加载、split_domain 的流式写入

用户仍可用 `--large-project` 或 `--no-large-project` 强制指定。

## 第1步 — AST扫描提取

```bash
python3 "$SKILL_DIR/scripts/code2database_scanner.py" scan \
  --source SOURCE_PATH --lang auto \
  --output code2db-out/.code2database_extraction.json \
  [--macros "NDEBUG FEATURE_X=1 -DFOO"]
```

scanner输出: functions(函数定义) + edges(调用关系) + domains(架构域) + lang_stats(语言统计) + fn_ptr_calls(函数指针调用点) + struct_types(结构体/vtable字段定义) + includes(C/C++的#include指令)

### 1b — 条件分支调用提取

扫描器提取条件表达式中的函数调用，并用 `call_condition` 标注其执行条件：

- **if条件调用**：`if (validate() && process())` — `validate()` 和 `process()` 均被提取，标注 `if_cond(...)`
- **if/else分支**：consequence中的调用标注 `if(...)`，alternative中的调用标注 `!(...)`
- **switch谓词**：`switch(get_key())` — `get_key()` 被提取，标注 `switch(...)` 条件
- **switch case体**：调用标注对应的case标签
- **while/do-while条件**：`while(has_next())` — `has_next()` 被提取，标注 `while_cond(...)`
- **for循环条件**：`for (; has_next(); )` — `has_next()` 被提取，标注 `for_cond(...)`，循环体边携带 `for(has_next)`
- **for循环init/update**：init和update子句中的调用也被提取（如 `for (init(); ; update())`）
- **三元表达式**：`cond() ? true_fn() : false_fn()` — 条件标注 `ternary_cond(...)`，真分支 `ternary_true(...)`，假分支 `!ternary(...)`
- **复合条件**：`&&` 和 `||` 运算符递归遍历，所有嵌套调用继承父级条件作用域

### 1c — C/C++导入解析与跨文件调用解析

扫描阶段提取每个源文件的 `#include` 指令。构建器通过多策略流水线解析跨文件callee名称：

1. **后缀索引查找**（O(1)）：将callee名与节点ID匹配 — 如 `bar` → `lib.bdev.bar`（当ID含点时）
2. **多策略解析**（6个策略按优先级排序）：
   - `same_file`（0.95）：callee定义在同一源文件
   - `import_map`（0.85）：callee的头文件被caller包含
   - `same_domain`（0.75）：callee在同一架构域
   - `suffix_match`（0.60）：callee名匹配节点ID后缀
   - `unique_name`（0.55）：全局唯一函数名
   - `fuzzy`（0.30-0.40）：部分名称匹配
3. **构建后导入解析**：扫描头文件桥接剩余未解析的外部端点

这意味着 `file_a.c` 中的 `foo()` 调用 `file_b.c` 中定义的 `bar()` 在以下情况下可正确解析：
- 两个文件都被扫描（同域 → same_domain策略）
- `file_a.c` 包含了 `file_b.h`（→ import_map策略）
- `bar` 在整个代码库中名称唯一（→ unique_name策略）

### 1d — ASM特定功能

ASM文件使用专用正则表达式扫描器处理（无tree-sitter语法）。关键能力：

- **节上下文追踪**：函数标注其ELF节（`.text`、`.data`、`.bss`）。仅 `.text` 节符号被视为可调用函数；`.data`/`.bss` 符号分类为数据引用。
- **系统调用建模**：对x86_64内核入口桩，扫描器追踪 `mov %eax, <nr>` 模式后跟 `syscall` 指令，建模对应内核系统调用处理函数为调用目标（INFERRED置信度）。
- **内核ASM宏链**：内核风格宏被识别用于函数边界检测：
  - `SYM_FUNC_START`/`SYM_FUNC_END`、`SYM_FUNC_START_LOCAL`/`SYM_FUNC_END`、`SYM_INNER_LABEL` — 函数边界标记
  - `ENTRY`/`ENDPROC` — 旧版内核函数标记
  - `EXPORT_SYMBOL` / `EXPORT_SYMBOL_GPL` — 导出符号注解，标记函数为API_entry
- **跨语言调用边合并（ASM ↔ C）**：当ASM函数调用C函数（或反之），边在构建阶段合并。构建器通过符号名匹配，协调ASM标签与C函数名。
- **C内联汇编调用提取**：`__asm__`/`asm` volatile块中的C/C++源码被解析提取call指令；提取的callee边标注置信度 `INFERRED` 和来源 `inline_asm`。
- **ARM bl/blr指令支持**：ARM汇编 `bl`（带链接分支）和 `blr`（带链接分支到寄存器）指令被提取为调用边。寄存器间接 `blr` 边建模为函数指针派发（类似C fn_ptr_calls）。
- **GCC扩展asm命名操作数**：内联汇编中 `%[name]` 语法被解析 — `blr %[func]` 解析为命名操作数绑定的变量。
- **asm goto**：`asm goto` 标签产生AMBIGUOUS边，标注 `call_condition=asm_goto`。
- **x86寄存器绑定**：寄存器变量绑定（`register void *rax __asm__("rax")`）和基于操作数的寄存器引用（`call *%2`）被追踪用于间接调用目标解析。
- **JMP尾调用目标**：x86 `jmp` 和 ARM `b`（无条件跳转）建模为尾调用边（INFERRED, confidence_score=0.6）。
- **`__attribute__((naked))`剥离**：naked属性在tree-sitter解析前用空格替换以防解析失败。
- **MSVC `__asm {}` 块**：MSVC语法的内联汇编被捕获并解析call/jmp指令。
- **ERROR节点回退**：当tree-sitter-c无法解析（如寄存器绑定导致ERROR节点），孤立的asm表达式通过正则回退恢复。
- **静态fn-ptr派发数组**：`static const void * const ops[] = {f1, f2}` 模式被收集用于解析dispatch_op目标。

## 第2步 — 语义增强

补充AST遗漏：函数指针/回调、跨文件调用、条件内调用、回调注册流向。详见 `references/semantic_enhancement.md`。

**不要重写已有数据，只追加新发现。**

### 2b — 函数指针与vtable派发解析

对于使用函数指针派发（device_ops、protocol ops、回调链等）的C/C++项目，调用图通过vtable派发流水线解析间接调用目标——此过程在构建步骤中自动执行：

1. **加载扫描的fn_ptr_calls**：验证fn_ptr_calls正确加载自extraction数据
2. **vtable字段名匹配**：从profile的 `struct_op_types` + 扫描的 `struct_types` 构建 `_vtable_field_names`
3. **字段派发解析**：对于具有已知字段名（如 `ops->read`）的fn_ptr_call，解析为vtable数据（`.code2database_vtables.json`）中所有已注册的实现
4. **回调派发边**：对于未解析的fn_ptr调用，添加 `callback_dispatch` 边到该struct op类型的所有已知实现（保守过近似）
5. **调试日志**：记录多少fn_ptr候选、多少已解析、多少失败

### 2c — 宏展开调用解析

扫描器的宏图与调用链分析集成：
1. 当被调用者名称匹配已知宏时，查找宏的展开内容
2. 从展开的宏体中提取函数调用
3. 添加从宏调用点到展开函数的边，置信度 `INFERRED`，来源 `macro_expansion`
4. 在 `evidence` 中标记宏到函数的边为 `{"kind": "macro_expansion", "weight": 0.8}`

## 第3步 — 构建调用图

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" build \
  --extraction code2db-out/.code2database_extraction.json \
  --outdir code2db-out/ \
  [--build-config auto|Release|Debug|none] \
  [--macros "NDEBUG FEATURE_X=1"] \
  [--plugin my_enricher.py] \
  [--plugin-config '{"threshold":0.8}'] \
  [--storage json|sqlite|auto]
```

**存储后端选择**（`--storage`）：
- `json`（小项目默认）：传统JSON存储
- `sqlite`：SQLite数据库后端（`code2database.db`），带索引表，查询<1秒，内存<500MB
- `auto`：>50K函数的项目使用SQLite，否则使用JSON

### 第3b步 — 域路径规范化

域规范化确保域路径一致，在构建步骤中自动执行：
1. 应用profile的 `domain_rules` 合并/折叠域（如 `^drivers/net/ethernet/` → `drivers.net.ethernet`）
2. 扫描后域去重：若两个域去掉公共前缀后后缀相同，则合并
3. 枢纽函数域纠正：内联/头文件函数映射到其定义域而非包含域

### 第3c步 — 内存高效图构建

大代码库的优化：
1. **分块域加载**：逐个加载域文件、构建子图、写入磁盘、释放内存
2. **紧凑图表示**：使用整数数组（CSR格式）替代Python字典，内存减少约5倍
3. **流式边写入**：构建时流式写入边到磁盘，而非在内存中累积
4. **跨域边隔离**：全局操作仅加载跨域边（通常<总量的5%）
5. **主动释放**：每个构建阶段后主动释放数据结构，而非依赖GC
6. **并行split_domain**：多线程写入域文件，使用流式JSON序列化

### 第3d步 — 一致性校验

一致性校验在构建步骤中自动执行。若发现不一致，重新运行 `build` 即可修正。

检查项：
- 节点计数：域表总和与头部总数匹配
- 边计数：置信度分类总和与总边数匹配
- 孤立节点：无边的节点
- 域覆盖：图中但不在任何域中的函数
- 端点一致性：不同输出文件中的端点计数

## 第3e步 — 端点分类

详见 `references/endpoint_pipeline.md`。

特性：
- 通过profile的 `endpoint_types` 配置端点类型（如 `syscall_entry`、`module_init`、`callback_entry`、`irq_entry`、`timer_entry`、`workqueue_entry`）
- 统一所有阶段和输出文件中的端点定义
- 摘要中提供清晰的图例说明不同端点类别

## 第3f步 — 增强API评分

评分公式：
```
Score = baseScore × exportMultiplier × nameMultiplier × frameworkMultiplier + bonusScore
```

额外加分项：
- 匹配profile中的 `public_prefixes`：+20分
- 属于 `struct_op_types`：+15分
- 跨域调用者数量：每个+1分（上限50）
- 是 `program_entry` 或 `callback_entry`：+10分
- 对 `skip_names` 中的通用工具函数惩罚：-10分

## 输出文件

```
code2db-out/
├── code2database_master.json           ← 导航索引
├── code2database.db                    ← SQLite数据库（--storage sqlite|auto时）
├── CODE2DATABASE_SUMMARY.md            ← 人可读摘要(三层L0/L1/L2)
├── REVIEW_CHECKLIST.md             ← LLM填写节点审阅清单
├── SCENARIOS_SUMMARY.md            ← 执行场景人类可读摘要
├── .semantics_changelog.md         ← LLM语义变更日志
├── .code2database_context_pack_micro.json   ← LLM上下文(~200 tokens, 极简)
├── .code2database_context_pack_micro.md     ← 人类可读Markdown版极简上下文
├── .code2database_context_pack_lite.json     ← LLM上下文(~500 tokens)
├── .code2database_context_pack_lite.md       ← 人类可读Markdown版上下文
├── .code2database_context_pack_standard.json ← LLM上下文(~1500 tokens)
├── .code2database_context_pack.json          ← LLM上下文(完整版)
├── .code2database_pipeline_stats.json       ← 流水线统计+一致性校验
├── .code2database_scenarios.json       ← 预计算执行场景
├── .code2database_endpoints.json       ← 端点列表(统一定义)
├── .code2database_build_config.json    ← 构建配置
├── .code2database_communities.json     ← 社区检测
├── .code2database_entry_scores.json    ← 入口点评分
├── .code2database_processes.json       ← 执行流检测
├── .code2database_vtables.json         ← Vtable注册
├── .code2database_globals_enums.json   ← 枚举成员(从globals拆分)
├── .code2database_globals_macros.json  ← 宏定义(从globals拆分)
├── .code2database_globals_typedefs.json ← 类型定义(从globals拆分)
├── .knowledge_pack_lite.json       ← LLM知识包(~300 tokens)
├── .knowledge_pack_standard.json   ← LLM知识包(~800 tokens)
├── .memory_pack_lite.json          ← LLM记忆包(~200 tokens)
├── .memory_pack_standard.json      ← LLM记忆包(~600 tokens)
├── knowledge/                      ← 知识目录(Markdown,人可读)
│   ├── architecture.md
│   ├── module_*.md
│   ├── constraints.md
│   ├── glossary.md
│   ├── patterns.md
│   ├── build_rules.md
│   └── index.json
├── memory/                         ← 持久记忆目录
│   ├── index.json
│   ├── L0_index.json              ← 热记忆(权重>0.7)
│   ├── L1_index.json              ← 温记忆(0.3-0.7)
│   ├── L2_index.json              ← 冷记忆(<0.3)
│   ├── root/                      ← 根记忆(合并后)
│   ├── leaf/                      ← 叶记忆(独立)
│   └── experience/                ← 过时记忆
├── .scratch/                       ← 临时思考记忆
├── domains/                        ← 域JSON(紧凑格式v3)
│   └── */DOMAIN_README.md
└── ...
```

**globals拆分**：不再使用单一 `globals.json`，globals拆分为按类别划分的文件：
- `.code2database_globals_enums.json` — 仅枚举成员
- `.code2database_globals_macros.json` — 仅宏定义
- `.code2database_globals_typedefs.json` — 仅类型定义
- 每个支持按域延迟加载

## 第4步 — 查询和回答

采用**全局→本地**模式：先读context_pack_lite获取全局理解，再用分级查询钻取细节。

### 4a — 全局理解

```bash
cat code2db-out/.code2database_context_pack_lite.json
```

需要更多上下文时升级到standard或full版。

### 4b — 分级单节点描述

```bash
# --brief (~200 tokens): id, name, signature, labels, invokers/invoked, exec_summary
# --standard (~500 tokens): +params, condition_vars, concurrency_info, api_constraints
# --full (~900 tokens): +local_vars, callee_args (--include-body for body_text)
python3 "$SKILL_DIR/scripts/code2database_builder.py" describe-node \
  --graph code2db-out/ --node NODE_ID --detail brief

# 含枢纽角色和可达API/端点
python3 "$SKILL_DIR/scripts/code2database_builder.py" describe-node \
  --graph code2db-out/ --node NODE_ID --detail brief --context

# Token预算控制（自动丢弃低优先级字段）
python3 "$SKILL_DIR/scripts/code2database_builder.py" describe-node \
  --graph code2db-out/ --node NODE_ID --max-tokens 300

# 选择性字段输出
python3 "$SKILL_DIR/scripts/code2database_builder.py" describe-node \
  --graph code2db-out/ --node NODE_ID --fields signature,params,invokers

# JSON格式输出
python3 "$SKILL_DIR/scripts/code2database_builder.py" describe-node \
  --graph code2db-out/ --node NODE_ID --detail brief --json
```

自动解析：接受部分节点名——系统通过搜索索引自动解析短名称，若多个匹配则提供消歧提示。

### 4b2 — 一键式探索

```bash
# 自然语言查询 → 相关节点+路径+条件
python3 "$SKILL_DIR/scripts/code2database_builder.py" explore-flow \
  --graph code2db-out/ --query "device initialization" --max-tokens 2000

# 符号名查询
python3 "$SKILL_DIR/scripts/code2database_builder.py" explore-flow \
  --graph code2db-out/ --query "device_open" --max-nodes 20
```

一步获取最相关节点、子图和关键执行路径。

### 4b3 — 关键路径自动提取

```bash
# 自动从入口点提取top-5关键执行路径
python3 "$SKILL_DIR/scripts/code2database_builder.py" key-paths \
  --graph code2db-out/ --top 5

# 指定入口点
python3 "$SKILL_DIR/scripts/code2database_builder.py" key-paths \
  --graph code2db-out/ --from device_open --top 3
```

关键路径从 `program_entry` 节点BFS追踪3-10步的实际执行路径，按结构相似性去重。

### 4c — 条件链解析

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" resolve-chain \
  --graph code2db-out/ --node NODE_ID --bindings "mode=1,flag=true"
```

### 4d — 一键路径追踪

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" trace-chain \
  --graph code2db-out/ --from FUNC_A --to FUNC_B \
  [--bindings "mode=1"] [--annotate] [--json]
```

输出完整带注释调用路径（每步含签名、条件、并发标记）。

### 4e — 路径差异对比

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" diff-chains \
  --graph code2db-out/ --node NODE_ID \
  --bindings-a "mode=0" --bindings-b "mode=1"
```

### 4f — 并发风险分析

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" concurrency-risks \
  --graph code2db-out/ [--json]
```

列出所有spawn点、并发窗口、按风险等级排序。

### 4g — 并发安全分析

```bash
# 分析两个调用链并发执行是否安全
python3 "$SKILL_DIR/scripts/code2database_builder.py" concurrency-analyze \
  --graph code2db-out/ --chain1 get_io_channel --chain2 for_each_channel

# 或指定单个函数，自动查找并发对等函数
python3 "$SKILL_DIR/scripts/code2database_builder.py" concurrency-analyze \
  --graph code2db-out/ --func get_io_channel
```

分析数据竞争、原子性违反、死锁风险，输出共享资源和保护状态。

### 4h — 数据竞争检测

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" detect-races \
  --graph code2db-out/ [--func FUNCTION_NAME] [--min-severity low|medium|high]
```

基于线程模型和共享状态访问（全局变量/结构体字段）检测数据竞争。

### 4i — 字段级访问追踪

```bash
# 查询哪些函数读写了特定结构体字段
python3 "$SKILL_DIR/scripts/code2database_builder.py" field-access \
  --graph code2db-out/ --struct device_channel --field shared_resource

# 查询哪些函数访问特定全局变量
python3 "$SKILL_DIR/scripts/code2database_builder.py" field-access \
  --graph code2db-out/ --field g_mem_mgr
```

输出包含每个访问者的函数名、域、读写类型、线程模型。

### 4j — 崩溃点反向追踪

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" reverse-trace \
  --graph code2db-out/ --crash-point channel_abort_pending_ops \
  --max-depth 10 --max-paths 20
```

从崩溃点沿INVOKES边反向BFS，列出所有从入口点到崩溃点的路径。优先展示从API_entry/thread_processor出发的路径。

**FIELD_WRITE 嫌疑集成**。调查某个字段读取崩溃（如 `bh->b_bdev` 解引用）时，往往需要同时看到*谁在崩溃点读取该字段*和*谁在其他地方写入该字段*。reverse-trace 本身只显示崩溃点的调用者；新增的 `--suspect-field` 标志把来自 `field_access` 表的字段写入嫌疑者集成到输出中：

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" reverse-trace \
  --graph code2db-out/ --crash-point __bread_gfp \
  --suspect-field b_bdev --suspect-value NULL --suspect-struct bh \
  --max-depth 8 --max-paths 20
```

- `--suspect-field FIELD_NAME` —— 查询 `field_access` 表中该字段的所有写入者；每个写入者作为一个 `field_write_suspects` 条目，含反向 BFS 到入口点的调用链、`guard_condition`（若有）、`object_origin`（若 Profile 声明了 `allocation_sites`）、以及 `reachable_in_scene` 判定（`guarded` / `unguarded`）。
- `--suspect-value VALUE` —— 按赋值值过滤写入者（如 `NULL`、`0`）。用 `NULL` 匹配任意 C NULL 形式（`NULL`、`0`、`0L`、`(void*)0`）。需要 `--suspect-field`。
- `--suspect-struct STRUCT_NAME` —— 按 struct 链过滤写入者（如 `bh` 匹配 `bh->field`）。可选；需要 `--suspect-field`。

JSON 输出包含 `field_write_suspects` 数组和 `field_write_suspects_summary` 块（suspect_count、unguarded_count、field、value_filter、struct_filter）。文本输出在"Concurrency entry points:"后追加"Field write suspects:"段。

这弥补了 reverse-trace 能看到崩溃点调用者却看不到导致崩溃的字段写入嫌疑的缺口（见续篇报告）。要完整字段流分析（读者 + 写者 + race window），直接用 `field-flow`。

### 4k — 数据生命周期追踪

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" data-lifecycle \
  --graph code2db-out/ --resource "buffer" [--json]
```

### 4l — 其他查询命令

```bash
# 搜索
python3 "$SKILL_DIR/scripts/code2database_builder.py" search \
  --graph code2db-out/ --keywords "device register" --top 20

# 加载概览
python3 "$SKILL_DIR/scripts/code2database_builder.py" load --graph code2db-out/ --summary

# 邻居
python3 "$SKILL_DIR/scripts/code2database_builder.py" neighbors \
  --graph code2db-out/ --node NODE_ID --depth 3

# 路径
python3 "$SKILL_DIR/scripts/code2database_builder.py" path \
  --graph code2db-out/ --from NODE1 --to NODE2

# 影响分析
python3 "$SKILL_DIR/scripts/code2database_builder.py" impact \
  --graph code2db-out/ --node NODE_ID --direction reverse

# 域查看
python3 "$SKILL_DIR/scripts/code2database_builder.py" domain \
  --graph code2db-out/ --name lib.device
```

所有命令支持 `--json` 输出结构化数据。

### 4m — I/O路径分析

```bash
# 从函数追踪I/O路径
python3 "$SKILL_DIR/scripts/code2database_builder.py" io-path \
  --graph code2db-out/ --from NODE_ID [--to TO_NODE] \
  [--bindings "FEATURE_X=1"] [--max-nodes 100] [--json]
```

追踪调用图中的输入/输出数据流路径，跟踪读写外部资源的函数。

### 4n — 文件监视器

```bash
# 监视源码目录变更并自动重建
python3 "$SKILL_DIR/scripts/code2database_builder.py" watch \
  --source /path/to/project [--output code2db-out/] [--debounce 2.0]
```

监视源文件变更并自动触发增量重建。防抖间隔防止连续快速重建。

### 4o — 流式查询结果

查询支持 `--stream` 标志，找到匹配即返回：
```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" search \
  --graph code2db-out/ --keywords "log_error" --top 100 --stream
```

长查询期间显示进度指示器。

## 第5步 — 增量更新

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" update \
  --source SOURCE_PATH --graph code2db-out/
```

### 5b — 增量索引

- 基于文件修改时间戳的变更检测
- 文件级依赖图：当文件变更时，仅重建受影响的函数和边
- 维护 `file_deps.json` 跟踪哪些函数/边依赖哪些源文件
- 变更文件 → 仅重扫描这些文件 → 补丁图 → 更新受影响域
- stale节点的语义在 `describe-node` 查询时按需填充

### 5c — 团队同步

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" sync --graph code2db-out/
```

合并策略：同名节点本地优先，补充远程独有节点。

## 第6步 — 文档语义提取

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" extract-semantics \
  --graph code2db-out/ --docs SOURCE_PATH
# Claude填写semantic_desc后:
python3 "$SKILL_DIR/scripts/code2database_builder.py" apply-semantics \
  --graph code2db-out/
```

## 第7步 — 完整调用链思考

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" think-chain \
  --graph code2db-out/ --output code2db-out/.code2database_think_chain.json
```

逐链分析填写conclusion，支持断点续传。

## 第8步 — 提问记忆

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" save-memory \
  --graph code2db-out/ --question "问题" --answer "回答" --tags "tag1,tag2"
python3 "$SKILL_DIR/scripts/code2database_builder.py" search-memory \
  --graph code2db-out/ --query "相关问题" --top 5
```

可信机制：trusted(验证通过,权重1.0) / experience(可能过时,权重0.5-0.7)

## 第8b步 — 高级记忆管理

```bash
# 添加记忆（自动合并到根记忆）
python3 "$SKILL_DIR/scripts/code2database_builder.py" manage-memory \
  --graph code2db-out/ --action add \
  --question "问题" --answer "回答" --tags "tag1"

# 修正记忆字段
python3 "$SKILL_DIR/scripts/code2database_builder.py" manage-memory \
  --graph code2db-out/ --action correct --id 5 --field answer --value "新答案"

# 重塑根记忆（强答案替换整个根）
python3 "$SKILL_DIR/scripts/code2database_builder.py" manage-memory \
  --graph code2db-out/ --action reshape --root-id 1 --answer "全新答案"

# 触发权重衰减（定期运行，无需LLM）
python3 "$SKILL_DIR/scripts/code2database_builder.py" manage-memory \
  --graph code2db-out/ --action decay

# 提升记忆权重
python3 "$SKILL_DIR/scripts/code2database_builder.py" manage-memory \
  --graph code2db-out/ --action promote --id 5 --boost 2.0

# 精炼临时记忆为持久记忆
python3 "$SKILL_DIR/scripts/code2database_builder.py" manage-memory \
  --graph code2db-out/ --action refine --scratch-id session_123 \
  --question "总结Q" --answer "总结A"

# 查询记忆
python3 "$SKILL_DIR/scripts/code2database_builder.py" manage-memory \
  --graph code2db-out/ --action query --query "相关问题" --top 5

# 生成记忆包
python3 "$SKILL_DIR/scripts/code2database_builder.py" manage-memory \
  --graph code2db-out/ --action pack --tier lite
```

记忆分层：L0(热,权重>0.7) / L1(温,0.3-0.7) / L2(冷,<0.3)
根记忆合并：相似问题(Jaccard>0.7)自动合并，保留版本历史
权重衰减：recency x importance x access，低权重自动归档为experience

## 第9步 — 导出

```bash
# HTML可视化(vis-network/mermaid)
python3 "$SKILL_DIR/scripts/code2database_builder.py" export-html --graph code2db-out/

# Obsidian vault导出
python3 "$SKILL_DIR/scripts/code2database_builder.py" export-obsidian --graph code2db-out/
```

## 第9b步 — 知识（项目简报）

```bash
# 会话启动（必须）：把简报渲染进 prompt
python3 "$SKILL_DIR/scripts/code2database_builder.py" knowledge-brief \
  --graph code2db-out/

# 首次：从图谱统计自举简报模板
python3 "$SKILL_DIR/scripts/code2database_builder.py" brief-extract \
  --graph code2db-out/

# 精炼：强制宏、使用模式、坑
python3 "$SKILL_DIR/scripts/code2database_builder.py" brief-update \
  --graph code2db-out/ --add hard_rules \
  --json '{"rule": "强制开启 XXX 宏", "type": "macro"}'

# 校验（schema、体积预算、图漂移）
python3 "$SKILL_DIR/scripts/code2database_builder.py" brief-validate \
  --graph code2db-out/
```

知识即精简的项目简报：`code2db-out/knowledge/brief.json`。

## 第9c步 — 高效图谱更新

代码变更时的轻量更新流程（无需LLM）：

```bash
# 方式1: 基于git diff自动补丁
python3 "$SKILL_DIR/scripts/code2database_builder.py" patch-from-git \
  --graph code2db-out/ --source SOURCE_PATH [--commit-range HEAD~3]

# 方式2: 轻量扫描变更文件
python3 "$SKILL_DIR/scripts/code2database_builder.py" light-scan \
  --source SOURCE_PATH --graph code2db-out/ [--files file1.c,file2.c]

# 方式3: 从diff文件补丁
python3 "$SKILL_DIR/scripts/code2database_builder.py" patch-from-diff \
  --graph code2db-out/ --diff-file changes.diff
```

三层延迟更新策略：
- **Layer 0**（实时，0 LLM token）: 文件变更 → AST重扫描 → 图结构增量更新
- **Layer 1**（延迟，0 LLM token）: git diff → 变更补丁 → 标记stale节点
- **Layer 2**（按需，LLM参与）: 语义描述填充 → 端点分类 → 知识提取

变更积累达阈值后再执行完整语义更新。stale节点的语义在describe-node查询时按需填充。

## 第9d步 — 源码片段获取

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" get-code-snippet \
  --graph code2db-out/ --node NODE_ID --context 10
```

无需读取源文件——直接从图谱节点定位到源码行。

## 第9e步 — 影响半径分析

```bash
# 变更某函数时，哪些API/测试/域会受影响
python3 "$SKILL_DIR/scripts/code2database_builder.py" blast-radius \
  --graph code2db-out/ --node NODE_ID --depth 3
```

返回受影响函数数、API列表、测试列表、受影响域列表。

## 第9f步 — MCP服务器模式

```bash
# 启动MCP服务器（stdio transport），供LLM agent实时查询
python3 "$SKILL_DIR/scripts/code2database_builder.py" serve \
  --graph code2db-out/
```

暴露 **81 个 MCP (53 base + 28 design-report) 工具**（34 个 `code2database_*` + 19 个 `cgdb_*`）：

**34 个 `code2database_*` 工具**（始终可用）：
- code2database_audit_log, code2database_blast_radius, code2database_composite_query, code2database_concurrency, code2database_daemon_status, code2database_data_lifecycle, code2database_describe, code2database_doc_code_check, code2database_domain, code2database_explain_label, code2database_explore, code2database_extract_signals, code2database_ffi_trace, code2database_find_invariants, code2database_foreign_refs, code2database_get_code_snippet, code2database_happens_before, code2database_impact, code2database_kb_query, code2database_key_paths, code2database_knowledge_query, code2database_load, code2database_memory_ordering, code2database_memory_search, code2database_path_feasible, code2database_search, code2database_semantic_status, code2database_sync_foreign, code2database_trace, code2database_unbalanced_alloc_free, code2database_who_allocates, code2database_who_frees, code2database_who_locks, code2database_why_ambiguous

注：CLI 专有命令（apply-invariants、blame-node、data-dep、field-access、lock-coverage、param-flow、profile-health、query、value-flow）**不**作为 MCP 工具暴露——通过 CLI（`python3 scripts/code2database_builder.py <cmd>`）调用。

**19 个 `cgdb_*` 工具**（需 clang 提取后端——`--extraction-backend clang` 或 auto 下已安装 libclang）：
- cgdb_search_symbols, cgdb_find_invokers, cgdb_find_invoked, cgdb_get_definition, cgdb_get_function_body, cgdb_get_struct_layout, cgdb_find_type_definition, cgdb_find_ops_impls, cgdb_find_cfg_paths, cgdb_find_data_flow, cgdb_find_aliases, cgdb_find_lock_held_calls, cgdb_check_race_condition, cgdb_find_configs_for, cgdb_find_nodes_under_config, cgdb_index_status, cgdb_time_travel_query, cgdb_list_versions

在 tree-sitter-only 模式下，`cgdb_*` 工具返回空结果——回退到 `code2database_*` 工具。完整 `cgdb_*` 工具参考见 `~/.claude/skills/Code2Database-analysis/references/analysis_commands.md`。

explore-flow 是一次性上下文检索命令，用于快速定位相关节点和路径。其结果包含 relevance_reason 字段，解释为什么每个节点与查询相关。

## 第9g步 — 记忆健康与Git Hook

```bash
# 检查记忆系统健康状况（统计数、过期数、层级分布）
python3 "$SKILL_DIR/scripts/code2database_builder.py" memory-health \
  --graph code2db-out/

# 安装git post-commit hook，每次commit后自动运行quick-update
python3 "$SKILL_DIR/scripts/code2database_builder.py" install-hook \
  --source /path/to/project

# quick-update带自动阈值：stale比例超过阈值时自动触发semantic update
python3 "$SKILL_DIR/scripts/code2database_builder.py" quick-update \
  --source /path/to/project \
  --graph code2db-out/ \
  --auto-threshold 0.15
```

## 第9h步 — 架构流程与知识验证

```bash
# 查看核心执行流程叙述（自动生成的API→endpoint路径描述）
cat code2db-out/ARCHITECTURE_FLOWS.md

# 验证知识文件质量（空文件、签名文件、模板标记检测）
python3 "$SKILL_DIR/scripts/code2database_builder.py" brief-validate \
  --graph code2db-out/
```

ARCHITECTURE_FLOWS.md 包含从 `program_entry` 到 `API_entry`/`in_end` 的多步执行路径（3-10步），从入口点BFS，路径去重。

## 第9i步 — 条件信号提取

```bash
# 提取#ifdef条件如何影响调用路径
python3 "$SKILL_DIR/scripts/code2database_builder.py" extract-signals \
  --graph code2db-out/
```

输出 `.code2database_signal_map.json`：条件变量 → 受影响边/函数/域的映射，按影响范围排序。

## 第10步 — LLM辅助Profile生成

当auto-profile的检测结果不够充分时（例如回调模式覆盖率低、注册宏缺失），使用LLM辅助生成profile。

### 10a — 自动检测是否需要LLM辅助

```bash
# 生成auto-profile并检查质量
python3 "$SKILL_DIR/scripts/code2database_scanner.py" auto-profile \
  --source SOURCE_PATH --outdir OUTPUT_DIR

# 如果输出显示回调模式少于5个或注册宏少于3个，建议使用LLM辅助
```

### 10b — LLM辅助Profile生成流程

1. **生成auto-profile** — 运行 `auto-profile --source SOURCE_PATH --outdir OUTPUT_DIR`
2. **收集关键头文件** — `llm_phases.collect_key_headers()` 自动选取最重要的头文件
3. **构造LLM提示** — `llm_phases.generate_header_analysis_prompt()` 生成结构化提示
4. **执行LLM分析** — 将提示发送给LLM，获取结构化JSON响应
5. **解析并合并** — `llm_phases.parse_header_analysis_response()` + `apply_header_analysis_to_profile()`
6. **写入更新后的profile** — 保存到 `.code2database_profile.json`

### 10c — LLM提示执行方式

**方式一：CodeAgent直接执行（推荐）**
```
# 读取生成的profile
# 读取关键头文件内容
# 使用LLM自身分析能力，按照 llm_phases.py 中的提示模板分析
# 将分析结果用 parse_header_analysis_response() 解析后合并到profile
```

**方式二：使用--llm-profile标志**
```bash
python3 "$SKILL_DIR/scripts/code2database_scanner.py" auto-profile \
  --source SOURCE_PATH --outdir OUTPUT_DIR --llm-profile
# 输出LLM提示，由用户/agent手动执行
```

### 10d — LLM分析结果质量检查

LLM分析后，验证profile质量：
- `callback_detection.static_patterns` 应 >= 5个（对于知名项目）
- `macro_dispatch.registration_macros` 应 >= 3个
- `skip_names.add` 非空（至少有项目特有的skip名称）
- `api_detection.public_header_paths` 非空
- `endpoint_types` 应包含项目所需的特定类型

如果质量仍不够，执行Phase 6（LLM结果检查）进一步改进。

### 10e — Phase 6: LLM结果检查

```bash
# 扫描完成后，检查extraction质量
python3 "$SKILL_DIR/scripts/code2database_builder.py" build \
  --extraction code2db-out/.code2database_extraction.json \
  --outdir code2db-out/

# 生成结果检查提示
# 使用 llm_phases.generate_result_check_prompt() 生成提示
# 分析extraction中的缺失边、误分类端点
# 用 parse_result_check_response() 解析LLM响应
```

---

## 完整 CLI 命令参考（222 个命令）

全部 222 个 CLI 子命令，涵盖 `code2database_builder.py`（214 个）和 `code2database_scanner.py`（8 个）。每条目显示命令名及其 `--help` 摘要。

| 命令 | 说明 |
|------|------|
| `add-function` | Add a new function to the graph |
| `add-semantic-edges` | Walk graph and add ALLOCATES/FREES/LOCKS/UNLOCKS edges from body text |
| `apply-invariants` | Apply LLM-enhanced invariants from .code2database_invariants.json back to the graph |
| `brief-update` | 更新项目简报的某一节 |
| `apply-semantics` | Apply LLM semantic descriptions to graph |
| `ast-search` | Structural code search: write patterns AS code with $metavars and ... ellipsis |
| `audit-log` | Query the audit log (who edited what, when, why) |
| `auto-enhance` | Auto-enhance a node with LLM-supplied attributes (auto-writes EXTRACTED, prompts INFERRED) |
| `auto-profile` | Auto-detect project type and generate/recommend profile |
| `batch-confirm` | Batch-confirm pending supplements (accept-all / reject-all / per-item / apply) |
| `blame-node` | Attribute a node to its introducing/last-modifying commit |
| `blast-radius` | Show blast radius: affected tests/APIs for a function change |
| `bridge-nodes` | Bridge nodes with high betweenness centrality (chokepoints) |
| `bug-benchmark` | Run BUG benchmark (graph vs grep) and report recall/precision/tool-call/token efficiency |
| `build` | Build invocation graph from extraction JSON |
| `build-diff` | Compare two graph builds: added/removed/changed nodes+edges+communities |
| `build-multi` | Build a unified C2D from a multi-project manifest |
| `c2d-add-foreign` | Register a foreign C2D and resolve cross-project refs |
| `c2d-add-foreign-stub` | Register a vendor SDK stub C2D (signatures only) |
| `c2d-check-compat` | Check if B's foreign_refs still valid against new A version |
| `c2d-list-foreign` | List watched foreign C2Ds with sync status |
| `c2d-pin-foreign` | Pin a foreign_ref so it won't auto-update |
| `c2d-prune-foreign` | Remove old deleted/orphaned foreign_refs |
| `c2d-remove-foreign` | Unregister a foreign C2D |
| `c2d-resolve-foreign` | Force re-resolve stale/deleted foreign_refs by name |
| `c2d-sync-foreign` | Sync foreign_refs with updated foreign C2Ds |
| `c2d-unpin-foreign` | Unpin a pinned foreign_ref |
| `cgdb-cfg-paths` | Enumerate CFG paths through a function (entry → exit blocks) |
| `cgdb-compare` | Compare two graph directories (e.g., main vs feature branch) |
| `cgdb-configs-for` | List config_predicate text_form(s) that gate a given node |
| `cgdb-coverage` | Query graph coverage: --function NAME | --file PATH |
| `cgdb-data-flow` | Show data_flow entries (def-use chains) for a variable node |
| `cgdb-definition` | Find definition nodes (function/var/field/typedef) by name |
| `cgdb-find-invoked` | Find callees (forward closure) of a node via recursive CTE |
| `cgdb-find-invokers` | Find callers (reverse closure) of a node via recursive CTE |
| `cgdb-freshness` | Check if the code graph is stale  |
| `cgdb-function-body` | Return a function's body source text |
| `cgdb-get-source` | Get source text for a node with byte-precise attribution |
| `cgdb-index-status` | Overall cgdb index statistics: node/edge counts by kind, file count |
| `cgdb-layer-summary` | Generate cgdb_layer_summary.md report for all 13 cgdb tables |
| `cgdb-merge-knowledge` | Merge knowledge/memory from another branch's graph  |
| `cgdb-nodes-under-config` | Find all nodes gated by a given config predicate |
| `cgdb-ops-impls` | Find ops_bind implementations for a given field name |
| `cgdb-path` | Find a call path from src to dst via recursive CTE |
| `cgdb-path-feasible` | Check feasibility of a CFG path through blocks (uses Z3 if available) |
| `cgdb-query` | Generic cgdb query: FTS5 symbol search or get_node by id |
| `cgdb-race-check` | Heuristic race-condition check for a function |
| `cgdb-schema-version` | Report current cgdb schema version and available migrations |
| `cgdb-sql` | Run arbitrary read-only SQL against the cgdb database (cross-table joins, ad-hoc analysis) |
| `cgdb-struct-layout` | Return a struct/union's field layout |
| `cgdb-suggest` | Analyze the graph and suggest improvements  |
| `cgdb-time-travel` | Query node state at a past version (by commit_hash or version_id) |
| `cgdb-tour` | Generate a guided codebase tour markdown  |
| `cgdb-type-definition` | Find type definitions (struct/union/enum/typedef/class) by name |
| `cgdb-versions` | List graph_versions rows (newest first), or diff two versions |
| `cgdb-views` | List/run predefined analysis views (hub functions, sync hotspots, doc coverage, etc.) |
| `cgdb-write-coverage` | Rewrite coverage reports |
| `classify-endpoints` | Apply LLM endpoint classification to the graph |
| `co-change` | Mine git log for co-change coupling edges |
| `code-slice` | Extract minimal context: data-flow slice or usage slice for LLM |
| `commit-db-transaction` | Commit a write-back transaction (render+compile+lint+sha256+git) |
| `composite-query` | Query across local + foreign C2Ds via SQLite ATTACH |
| `concurrency-analyze` | Analyze concurrency safety between two call chains or a function and its concurrent peers |
| `concurrency-risks` | List all concurrency risk points sorted by risk level |
| `coverage-cross-c2d` | Compute which functions in target_c2d are called by test_c2d |
| `daemon-force-refresh` | Force daemon to re-scan a specific file immediately |
| `daemon-list-projects` | List all projects with daemon state/log files on this machine |
| `daemon-logs` | Show daemon log file (last N lines, or --follow for streaming) |
| `daemon-pause` | Pause daemon (e.g., before manual updates to avoid conflicts) |
| `daemon-reload` | Reload daemon config (sends SIGHUP; daemon re-reads profile) |
| `daemon-resume` | Resume daemon after pause |
| `daemon-start` | Start long-running daemon (foreground; blocks). Monitors source files and auto-updates graph. |
| `daemon-status` | Get daemon status: pid, last_sync, pending events, stale nodes |
| `daemon-stop` | Stop a running daemon (sends SIGTERM) |
| `daemon-wait-sync` | Block until daemon finishes current sync (LLM agents call before important queries) |
| `data-dep` | Cross-function data dependencies (globals/fields as nodes, mod-read chains, dead writers) |
| `data-lifecycle` | Trace resource allocation→usage→release paths |
| `delete-node` | Soft-delete an AST node by ID |
| `delete-token` | Delete a token by token_id |
| `describe-commit` | Show which nodes/edges a commit affected |
| `describe-node` | Get info about a node. Use --detail brief|standard|full to control output size |
| `detect-changes` | Detect changed files since last manifest |
| `detect-races` | Detect data races between different thread contexts |
| `diff-chains` | Compare execution paths under two different bindings |
| `discover` | Discover macro-based registration dispatch patterns from headers |
| `doc-alignment-report` | Generate full Markdown report of doc-code alignment issues |
| `doc-code-check` | Check doc-code alignment: detect mismatches between semantic_desc (from docs) and body_text (from code) |
| `doc-mark-stale` | Mark a node's doc as stale (e.g., after code change detected by daemon) |
| `doc-signature-diff` | Detect signature changes between two graph versions (old vs new) |
| `domain` | List all nodes/edges in a domain |
| `edit-token` | Edit a token's spelling by token_id |
| `embeddings-build` | Build TF-IDF char n-gram embeddings for semantic search |
| `embeddings-search` | Cosine-similarity search over node embeddings |
| `explain-label` | Explain why a node has a given label (dead_code, API_entry, race_risk, etc.) |
| `explore-flow` | One-shot context retrieval: query → nodes + paths + conditions |
| `export-changes` | Export change graph from git/svn changelog |
| `export-html` | Export invocation graph as interactive HTML |
| `export-mermaid` | Export call chains as Mermaid flowchart diagrams |
| `export-obsidian` | Export invocation graph as Obsidian vault with [[links]] = calls |
| `extract-invariants` | Extract preconditions/postconditions/loop_invariants + state machines from function bodies |
| `extract-invariants-llm` | Extract invariants with LLM consensus and continuous confidence |
| `brief-extract` | 从图谱统计自举/刷新简报模板 |
| `extract-semantics` | Export nodes for LLM semantic description |
| `extract-signals` | Extract #ifdef condition→affected edges map |
| `ffi-auto-link` | Auto-link FFI bindings to watched foreign C2Ds |
| `ffi-detect` | Detect FFI boundaries (Python ctypes, Go cgo, Rust extern \ |
| `ffi-list` | List all FFI edges in the graph |
| `ffi-persist` | Persist FFI edges into SQLite bridge tables |
| `ffi-trace` | Trace the FFI call chain from a node |
| `ffi-types` | Find FFI type mappings matching patterns |
| `field-access` | Find which functions read/write a struct field or global variable |
| `field-flow` | Trace field writes + their call chains (combines field-access + reverse-trace) |
| `fill-request` | List empty fields on a node that the LLM should fill (auto-fill request) |
| `find-commits` | Find commits that recently modified a function |
| `find-invariants` | Find functions guaranteeing a given invariant (e.g., 'ctx->state == READY' after return) |
| `find-macros` | Find macro definitions and invocations |
| `get-code-snippet` | Extract source code snippet around a node |
| `get-pp-branches` | Get #ifdef branch tree for a file |
| `get-string-literals` | Find string literals with optional pattern |
| `graph-diff` | Diff two graph versions |
| `graph-history` | List graph versions or show history of a specific node |
| `graph-provenance` | Show which commit the current graph corresponds to |
| `graph-record-version` | Manually record a graph version |
| `happens-before` | Check happens-before between a writer and reader via locks, RCU, or memory barriers |
| `heuristic-enhance` | Generate heuristic supplements for empty fields — no LLM required (always-works fallback) |
| `hub-nodes` | Most connected nodes (highest in+out degree) |
| `hybrid-search` | Hybrid search: FTS5 BM25 + optional embedding + RRF fusion |
| `impact` | Impact analysis for a node |
| `import-foreign-knowledge` | Copy foreign C2D's knowledge/*.md into local knowledge/ |
| `insert-node-after` | Insert a new AST node after an anchor |
| `insert-token` | Insert tokens after a given anchor token_id |
| `install-hook` | Install git post-commit hook for auto quick-update |
| `intent-query` | Classify a natural-language question and route to a CLI command |
| `io-path` | Trace IO path from a function, auto-detecting vtable dispatch options |
| `kb-audit` | Audit KB: counts, stale, low-confidence, citations |
| `kb-cluster` | Cluster kb_paragraphs by FTS5 similarity + link principles |
| `kb-conflict` | Detect contradictory items in the same cluster |
| `kb-forget` | Immediately delete a kb_paragraph (no decay) |
| `kb-global-add` | Add an entry to the cross-project global KB |
| `kb-global-import` | Import a shared global KB JSON file |
| `kb-global-search` | Search the cross-project global KB |
| `kb-global-share` | Export global KB to a portable JSON file |
| `kb-known-unknowns` | List queries that returned no matches (Phase 9) |
| `kb-migrate` | Migrate kb_paragraphs rows into kb_items (fact-level) |
| `kb-query` | Unified FTS5+BM25 query across memory and knowledge |
| `kb-rebuild-index` | Rebuild the unified kb_paragraphs FTS5 index  |
| `kb-rollback` | Restore a kb_item to a prior version |
| `key-paths` | Extract key execution paths from entry points automatically |
| `knowledge-brief` | 渲染项目简报（会话启动加载） |
| `brief-validate` | 校验简报（schema、体积预算、图漂移） |
| `light-scan` | Lightweight scan of changed files (no LLM) |
| `load` | Load and summarize the invocation graph |
| `lock-coverage` | Analyze lock-held regions and per-access locksets (replaces over-approximation) |
| `lsp-server` | Start Code2Database as a read-only LSP server on stdio  |
| `manage-memory` | Manage persistent memory (add/correct/reshape/decay/promote/refine/query/pack/consolidate/export/import/scratch-*) |
| `manifest` | Save file fingerprint manifest |
| `memory-health` | Report memory system health statistics |
| `memory-ordering` | Show RCU/memory-barrier/atomic primitives used by a function |
| `merge` | Merge new extraction into existing graph |
| `merge-changes` | Merge change graph JSON into existing graph |
| `neighbors` | Get neighbors of a node |
| `node-history` | Show commit history for a node |
| `null-source` | Find all writers of NULL to a struct field (alias: field-flow --value NULL) |
| `param-flow` | Trace parameter flow through the call chain (cross-function) |
| `patch-from-diff` | Patch graph from unified diff text |
| `patch-from-git` | Patch graph from git diff |
| `patch-profile` | LLM-driven incremental calibration of auto-profile (non-destructive, requires user confirmation) |
| `path` | Find shortest call path between two nodes |
| `path-feasible` | Auto-solve path feasibility (no manual bindings needed) |
| `path-guards` | Prove writer reachability from entry using guard conditions |
| `plugins` | List available callgraph plugins |
| `profile` | Generate project profile by scanning source directories |
| `profile-bind-version` | Bind profile to current git/svn HEAD commit so stale profiles can be detected |
| `profile-evolve` | Detect new callback patterns in source and suggest profile additions; optionally apply EXTRACTED-confidence suggestions |
| `profile-health` | Compute 0-100 health score for a project profile (callback patterns, skip_names, vtable_types, etc.) |
| `query` | Run a Cypher-subset query against the graph (unified query language) |
| `quick-update` | One-click: patch + light-scan, no LLM needed |
| `references-of` | List ALL source locations where a symbol is referenced (declaration+calls+reads+writes) |
| `render-source` | Render source from DB tokens |
| `resolve-chain` | Trace call chain from a node with variable bindings to prune dead branches |
| `reverse-trace` | Reverse trace from crash point through callers with condition/concurrency annotation |
| `rollback` | Rollback supplement writes (revert to previous value) |
| `rollback-db-transaction` | Roll back a write-back transaction |
| `runtime-guards` | Detect runtime guard patterns in path conditions |
| `sarif-export` | Export analysis results to SARIF 2.1.0 format for CI/IDE |
| `save-memory` | Save Q&A memory with call chains |
| `scan` | Scan source files for invocation graph extraction |
| `scan-rpc` | Scan source for RPC client calls (HTTP/gRPC) + create stub edges |
| `search` | Search nodes by keywords |
| `search-memory` | Search memory for similar questions |
| `semantic-search` | Neural semantic search: FTS5 BM25 + neural embedding + RRF fusion |
| `semantic-status` | Check if semantic update is recommended |
| `serve` | Start MCP server for LLM agent queries (stdio transport) |
| `sync` | Sync local code2db-out with git-tracked version (local wins) |
| `taint-analysis` | Taint analysis: source/sink/sanitizer propagation through DATA_FLOW edges |
| `think-chain` | Generate complete call chains for structured analysis |
| `trace-chain` | One-shot trace from --from to --to with full annotation |
| `traverse-graph` | Free-form BFS/DFS traversal with depth and token budget |
| `tx-begin` | Begin a graph transaction (snapshot + WAL + write lock) |
| `tx-commit` | Commit the current transaction (clears WAL) |
| `tx-list-snapshots` | List all available snapshots |
| `tx-replay-wal` | Replay or rollback an unfinished WAL (crash recovery) |
| `tx-restore` | Restore graph state from a specific snapshot |
| `tx-rollback` | Rollback the current transaction (restores snapshot) |
| `tx-snapshot` | Take a manual snapshot (without starting a transaction) |
| `tx-status` | Show current transaction state and WAL status |
| `unbalanced-alloc-free` | Find functions that alloc without free (or vice versa) |
| `update` | Incremental update: re-scan changed files and merge |
| `update-edge` | LLM-driven incremental supplement of edge attributes (non-destructive, requires user confirmation) |
| `update-node` | LLM-driven incremental supplement of node attributes (non-destructive, requires user confirmation) |
| `validate` | Validate build output files for correctness |
| `validate-memory` | Validate memory against current graph; invalidate stale → experience |
| `validate-plugin` | Validate a plugin file for interface compliance |
| `validate-profile` | Validate a profile JSON against coverage metrics |
| `value-flow` | Build and query value-flow edges (where does this value come from / go to?) |
| `verify-consistency` | Verify DB render matches disk sha256 |
| `watch` | Auto-sync: watch source directory and update incrementally |
| `web-ui` | Start interactive Web UI server for graph browsing, path highlighting, LOD rendering |
| `who-allocates` | Find functions that allocate a resource (ALLOCATES edges) |
| `who-frees` | Find functions that free a resource (FREES edges) |
| `who-locks` | Find functions that acquire a lock (LOCKS edges) |
| `why-ambiguous` | Explain why an edge is marked AMBIGUOUS (fn_ptr dispatch, dead #ifdef, etc.) |
