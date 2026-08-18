# 调用图 JSON Schema

> **Schema 是让它成为数据库的契约。** 每个节点带 `body_text`、`params`、`callee_args`、`condition_vars`、`ifdef_conditions`——足以在不打开源码的情况下推理一个函数。每条边带 `call_condition`、`concurrency`、`confidence`、`evidence`、`preproc_alive`——足以知道一次调用为什么发生、是否确定。这个 schema 与语言无关：C、Go、Python、Java、Rust、ASM 都产生同样的结构，所以为一个项目写的查询能用于任何项目。

## 边类型

| 边类型 | 描述 | 关键字段 |
|--------|------|----------|
| INVOKES | 函数调用关系 | call_order, call_condition, concurrency, confidence |
| CONTAINS | 文件 → 函数包含关系 | relation="CONTAINS" |
| IMPORTS | 文件 → 文件 #include 关系 | relation="IMPORTS", import_path |

## 概述

调用图以按域拆分的 JSON 文件存储在 `code2db-out/` 目录下。每个域拥有独立的文件，主导航文件提供跨域引用。该 schema 与语言无关——所有语言（C/C++/Go/Python/Java/Rust/ASM）均产生相同的 JSON 结构。

## 主导航文件：`code2database_master.json`

```json
{
  "type": "code2database_master",
  "source_root": "/path/to/source",
  "domains": {
    "lib.device": "code2database_domain_lib_device.json",
    "module.device.nvme": "code2database_domain_module_device_nvme.json"
  },
  "cross_domain_edges": [
    {
      "source": "lib_device_device_register",
      "target": "module_device_nvme_device_nvme_init",
      "call_order": 3,
      "call_condition": "",
      "source_domain": "lib.device",
      "target_domain": "module.device.nvme"
    }
  ],
  "total_nodes": 150,
  "total_edges": 320
}
```

### 字段

| 字段 | 类型 | 描述 |
|------|------|------|
| `type` | string | 始终为 `"code2database_master"` |
| `source_root` | string | 被扫描源码树的绝对路径 |
| `domains` | object | 域名 → 域 JSON 文件名的映射 |
| `cross_domain_edges` | array | 源节点和目标节点位于不同域的边 |
| `total_nodes` | integer | 所有域的节点总数 |
| `total_edges` | integer | 边总数（域内 + 跨域） |

## 域文件：`code2database_domain_<sanitized>.json`

```json
{
  "type": "code2database_domain",
  "domain": "lib.device",
  "nodes": [
    {
      "id": "lib_device_device_register",
      "name": "device_register",
      "source_file": "lib/device/device.c",
      "line": 245,
      "location": "lib/device/device.c:245",
      "domain": "lib.device",
      "labels": ["API_entry"],
      "is_empty": false,
      "condition": "",
      "api_constraints": "device: struct device * (non-NULL); ctx: void *",
      "external_desc": ""
    },
    {
      "id": "lib_device_pthread_create",
      "name": "pthread_create",
      "source_file": "",
      "line": 0,
      "location": "",
      "domain": "external",
      "labels": ["out_end"],
      "is_empty": false,
      "condition": "",
      "api_constraints": "",
      "external_desc": "Creates a new thread"
    },
    {
      "id": "lib_device_my_outlib_func",
      "name": "my_outlib_func",
      "source_file": "",
      "line": 0,
      "location": "",
      "domain": "external",
      "labels": ["unknown_end"],
      "is_empty": false,
      "condition": "",
      "api_constraints": "",
      "external_desc": ""
    }
  ],
  "edges": [
    {
      "source": "lib_device_device_register",
      "target": "lib_device_device_open",
      "call_order": 1,
      "call_condition": ""
    }
  ]
}
```

### 节点字段

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| `id` | string | 是 | 唯一标识符：`{domain_dots_to_underscores}_{func_name_normalized}` |
| `name` | string | 是 | 源码中的原始函数名 |
| `source_file` | string | 是 | 相对于源码根目录的路径（外部节点为空） |
| `line` | integer | 是 | 定义所在行号（空节点/外部节点为 0） |
| `location` | string | 是 | `source_file:line`，用于直接代码索引（外部节点为空） |
| `domain` | string | 是 | 架构域（点分隔的路径组件；未解析的被调用者为 "external"） |
| `labels` | array of string | 是 | 函数标签（参见节点标签章节） |
| `is_empty` | boolean | 是 | 虚拟/条件分组节点为 true |
| `condition` | string | 空节点必填 | 该空节点代表的条件 |
| `api_constraints` | string | 是 | API_entry 函数的参数约束；否则为空 |
| `external_desc` | string | out_end 必填 | 外部函数功能的描述；unknown_end 为空 |
| `semantic_desc` | string | 是 | LLM 提取的语义描述（函数实现、约束、用途、使用场景）；在运行 extract-semantics + apply-semantics 之前为空 |
| `body_text` | string | 是 | 完整的函数体源文本；外部/空节点为空。使 LLM 无需打开源文件即可读取函数逻辑 |
| `signature` | string | 是 | 完整函数签名（返回类型 + 函数名 + 参数）；外部/空节点为空 |
| `params` | array of object | 是 | 形式参数：`{name, type, is_param: true}`；从函数签名中提取；外部/空节点为空 |
| `local_vars` | array of object | 是 | 变量赋值和参数：`{name, type, value_snippet, line, is_param}`；参数在前，标记 `is_param: true` 且 `value_snippet: "<param>"`；外部/空节点为空 |
| `callee_args` | array of object | 是 | 调用点实参：`{call_order, invoked, args_snippet, args: [{pos, value}], concurrency_info: {is_spawn, spawn_target, spawn_arg, concurrency_type}, callback_target}`；支持追踪回调目标和参数流 |
| `condition_vars` | array of object | 是 | 条件中引用的变量：`{condition, vars: [name, ...]}`；支持不读取源码即可评估分支 |
| `preproc_alive` | boolean | 否 | 当函数完全位于无效的预处理器分支中（被构建宏排除）时为 `false`。否则为 `true` 或省略 |

### 边字段

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| `source` | string | 是 | 调用者节点 ID |
| `target` | string | 是 | 被调用者节点 ID |
| `call_order` | integer\|null | 是 | 该调用在调用者函数体中的顺序（invoker→empty 边为 null） |
| `call_condition` | string | 是 | 该调用发生的条件。在 invoker→empty 边上非空；在 empty→invoked 和直接边上为空 |
| `concurrency` | string | 是 | 并发类型：`""`（顺序）、`"thread_spawn"`（调用创建线程）、`"goroutine"`（Go 协程启动）、`"callback"`（回调注册）、`"spawn_target"`（从发起者到被派生线程函数的虚拟边） |
| `confidence` | string | 是 | `"EXTRACTED"`、`"INFERRED"` 或 `"AMBIGUOUS"` |
| `source` | string | 是 | 来源：`"ast"`、`"llm"`、`"manual"`、`"preproc_dead"` 或 `"plugin:<name>"` |
| `confidence_score` | float | 是 | 0.0–1.0 |
| `preproc_condition` | string | 否 | 预处理器条件文本（如 `"#ifdef FEATURE_X"`）——当调用在 #ifdef 块内时出现 |
| `preproc_alive` | boolean | 否 | 当此边位于无效的预处理器分支中（被构建宏排除）时为 `false`。否则为 `true` 或省略 |

## 端点分类文件：`.code2database_endpoints.json`

由 `build` 命令生成，在 LLM 填入分类后由 `classify-endpoints` 命令消费。

```json
{
  "endpoints": [
    {
      "id": "lib_device_pthread_create",
      "name": "pthread_create",
      "domain": "external",
      "invokers": [
        {
          "id": "lib_device_device_start",
          "name": "device_start",
          "source_file": "lib/device/device.c"
        }
      ],
      "classification": "out_end",
      "external_desc": "Creates a new thread"
    },
    {
      "id": "lib_device_my_outlib_func",
      "name": "my_outlib_func",
      "domain": "external",
      "invokers": [],
      "classification": "unknown_end",
      "external_desc": ""
    }
  ]
}
```

### 端点字段

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| `id` | string | 是 | 图中的节点 ID |
| `name` | string | 是 | 函数名 |
| `domain` | string | 是 | 架构域（通常为 "external"） |
| `invokers` | array | 是 | 调用此端点的节点列表（提供上下文） |
| `classification` | string | 是 | LLM 填写：`"out_end"`（已知）或 `"unknown_end"`（不明确） |
| `external_desc` | string | 是 | LLM 填写：out_end 的功能描述；unknown_end 为空 |

## 空节点（条件分组）

空节点代表条件分支点。它们将同一条件下执行的被调用函数进行分组。

**示例（C）：**

```c
void func(int a) {
    if (a) { func1(); func2(); }
    func3();
}
```

生成的图：

- 节点：`func`（真实节点）
- 节点：`func__cond_0`（空节点，条件：`"if(a)"`）
- 边：`func` → `func__cond_0`（call_condition: `"if(a)"`，call_order: null）
- 边：`func__cond_0` → `func1`（call_order: 1，call_condition: ""）
- 边：`func__cond_0` → `func2`（call_order: 2，call_condition: ""）
- 边：`func` → `func3`（call_order: 3，call_condition: ""）

**示例（Python）：**

```python
def process(data):
    if data:
        validate(data)
        transform(data)
    return data
```

生成的图遵循相同的模式，条件为 `if(data)`。

## 节点标签

| 标签 | 含义 | C/C++ | Go | Python | Java | Rust |
|------|------|-------|-----|--------|------|------|
| `thread_processor` | 子线程入口 | `pthread_create`, `std::thread` | `go func()` | `Thread(target=)` | `new Thread()` | `thread::spawn` |
| `callback_func` | 回调入口 | callback args | callback args | callback args | `@Override`/callback | callback args |
| `constructor` | 初始化入口 | `Cls::Cls()` | — | `__init__` | constructor decl | — |
| `destructor` | 清理入口 | `Cls::~Cls()` | — | `__del__` | `finalize()/close()` | `Drop::drop` |
| `API_entry` | 公共 API | non-static | exported (uppercase) | non-private | public method | `pub fn` |
| `out_end` | 已知外部端点 | — | — | — | — | — |
| `unknown_end` | 未知外部端点 | — | — | — | — | — |
| `dead_code` | 被预处理器宏排除（构建配置） | `#ifdef` dead branch | — | — | — | — |

"—" 表示该标签在该语言中不会被应用（该概念在语言中不存在原生对应）。

`API_entry` 节点包含 `api_constraints` 属性，描述输入参数约束（类型、格式限制、值范围）。

`out_end` 节点包含 `external_desc` 属性，描述外部函数的用途。`unknown_end` 节点的 `external_desc` 为空，需要人工审查。

新标签在引入前必须经过用户明确批准。

## 各语言节点 ID 约定

节点 ID 遵循模式 `{domain_underscored}_{normalized_name}`：

| 语言 | 示例名称 | 节点 ID |
|------|----------|---------|
| C | `lib/device/` 中的 `device_register` | `lib_device_device_register` |
| C++ | `lib/device/` 中的 `Device::open` | `lib_device_device_open` |
| Go | `pkg/server/` 中的 `Handler.ServeHTTP` | `pkg_server_handler_servehttp` |
| Python | `app/` 中的 `Server.__init__` | `app_server___init__` |
| Java | `src/main/` 中的 `App.start` | `src_main_app_start` |
| Rust | `crates/engine/` 中的 `Engine::run` | `crates_engine_engine__run` |

规范化规则：
- 所有非字母数字字符 → 下划线
- 全部小写
- `::` → `__`（Rust/C++），`.` → `_`（Go/Python/Java）

## API_entry 检测规则

| 语言 | 检测规则 | 示例 |
|------|----------|------|
| C/C++ | 非静态的顶层函数 | `void device_start()` → API_entry；`static void helper()` → 非 API |
| Go | 首字母大写的函数/方法 | `func Start()` → API_entry；`func listen()` → 非 API |
| Python | 模块级函数和非下划线前缀的方法 | `def run()` → API_entry；`def _helper()` → 非 API |
| Java | 带有 `public` 修饰符的方法 | `public void start()` → API_entry；`private void init()` → 非 API |
| Rust | 带有 `pub` 可见性的函数 | `pub fn run()` → API_entry；`fn main()` → 非 API |

## 架构域分类

域从源文件相对于项目根目录的路径派生（与语言无关）：

| 文件路径 | 域 |
|----------|-----|
| `lib/device/device.c` | `lib.device` |
| `module/device/nvme/device_nvme.c` | `module.device.nvme` |
| `pkg/server/handler.go` | `pkg.server` |
| `app/models/user.py` | `app.models` |
| `src/main/java/com/app/App.java` | `src.main.java.com.app` |
| `crates/engine/src/lib.rs` | `crates.engine.src` |

规则：
- 去掉文件名，仅保留目录组件
- 路径分隔符替换为点
- 单层目录：`lib` → `lib`
- 根目录文件：域为 `root`
- 外部/未解析的被调用者：域为 `external`

## 增量更新：清单文件

文件：`code2db-out/.code2database_manifest.json`

```json
{
  "source_root": "/path/to/source",
  "files": {
    "lib/device/device.c": "1234567890123456789:1024",
    "pkg/server/handler.go": "9876543210987654321:2048"
  }
}
```

指纹格式：`{mtime_ns}:{file_size}`。由 `detect-changes` 用于查找新增/修改/删除的文件。由 `build` 和 `update` 命令自动生成。

## 语义提取文件

文件：`code2db-out/.code2database_semantics.json`

```json
{
  "nodes_to_describe": [
    {
      "id": "lib_device_device_start",
      "name": "device_start",
      "source_file": "lib/device/device.c",
      "line": 17,
      "location": "lib/device/device.c:17",
      "domain": "lib.device",
      "labels": ["thread_processor", "API_entry"],
      "api_constraints": "",
      "semantic_desc": ""
    }
  ],
  "doc_files": ["docs/architecture.md", "README.md"],
  "doc_root": "/path/to/source"
}
```

LLM 为每个节点填写 `semantic_desc`，然后 `apply-semantics` 写回。

## 调用链分析文件

文件：`code2db-out/.code2database_think_chain.json`

```json
{
  "total_chains": 5,
  "api_entries": 2,
  "endpoints": 3,
  "chains": [
    {
      "from_api": "lib_device_device_start",
      "to_endpoint": "pthread_create",
      "length": 3,
      "steps": [
        {"id": "lib_device_device_start", "name": "device_start", "labels": ["thread_processor", "API_entry"], "is_empty": false, "condition": ""},
        {"id": "lib_device_device_start__cond_0", "name": "<conditional:if(mode==1)>", "labels": [], "is_empty": true, "condition": "if(mode==1)", "call_order": null, "call_condition": "if(mode==1)"},
        {"id": "pthread_create", "name": "pthread_create", "labels": ["out_end"], "is_empty": false, "condition": ""}
      ],
      "conclusion": ""
    }
  ]
}
```

用于带检查点/恢复的结构化推理。LLM 为每条链填写 `conclusion`。分析完成后删除。

## 记忆系统

目录：`code2db-out/memory/`

### 索引文件：`memory/index.json`

```json
{
  "entries": [
    {"id": 1, "question": "device_start的调用链是什么", "tags": ["device", "thread"], "status": "trusted"},
    {"id": 2, "question": "旧函数的功能", "tags": [], "status": "experience"}
  ],
  "next_id": 3
}
```

### 记忆条目：`memory/memory_<id>.json`

```json
{
  "id": 1,
  "question": "device_start的调用链是什么",
  "answer": "device_start根据mode条件分支...",
  "chains": [{"from": "api_id", "to": "ep_id", "steps": [...]}],
  "node_ids": ["c_device_start", "c_worker_thread_fn"],
  "tags": ["device", "thread"],
  "status": "trusted",
  "created": "2026-06-29T10:30:00",
  "validated_at": "2026-06-29T10:35:00",
  "merged_count": 0
}
```

### 经验条目：`memory/experience/experience_<id>.json`

当记忆的 `node_ids` 引用的节点不再存在于图中（例如，代码更新删除了这些函数）时，该记忆将失效并移至经验：

```json
{
  "id": 2,
  "question": "旧函数的功能",
  "answer": "它是外部模块的函数",
  "chains": [],
  "node_ids": ["deleted_func_id"],
  "tags": [],
  "status": "experience",
  "created": "2026-06-29T10:00:00",
  "validated_at": "2026-06-29T10:30:00",
  "invalidated_at": "2026-06-29T11:00:00",
  "invalidated_reason": "1 node(s) removed by update: ['deleted_func_id']",
  "merged_count": 0
}
```

**信任生命周期：** `trusted`（所有 node_ids 存在）→ `experience`（部分 node_ids 缺失），在 `update` 或 `validate-memory` 后触发。经验条目仍可搜索，但权重降低（0.5-0.7 倍）。

## 全局变量文件：`.code2database_globals.json`

由 `build` 命令从扫描器提取数据生成。包含评估分支条件所需的枚举/常量/类型定义/全局变量定义。

```json
{
  "enums": [
    {
      "name": "device_state",
      "values": [{"member": "DEVICE_INIT", "value": "0"}, {"member": "DEVICE_RUNNING", "value": "1"}],
      "source_file": "lib/device/device.c",
      "line": 10
    }
  ],
  "constants": [
    {"name": "MODE_ASYNC", "value_snippet": "1", "source_file": "lib/device/device.c", "line": 3}
  ],
  "typedefs": [
    {"name": "device_mode_t", "underlying_type": "", "source_file": "lib/device/device.c", "line": 5}
  ],
  "global_vars": [
    {"name": "device_name", "type": "const char *", "value_snippet": "\"default\"", "source_file": "lib/device/device.c", "line": 12}
  ]
}
```

### 全局变量字段

| 字段 | 类型 | 描述 |
|------|------|------|
| `enums[].name` | string | 枚举类型名称 |
| `enums[].values` | array | `{member, value}` 对 |
| `constants[].name` | string | 常量/宏定义名称 |
| `constants[].value_snippet` | string | 值表达式 |
| `typedefs[].name` | string | 类型定义名称 |
| `global_vars[].name` | string | 全局变量名称 |
| `global_vars[].type` | string | 变量类型 |
| 所有 `source_file`/`line` | string/int | 源码位置 |

## 预计算索引文件

由 `build` 命令生成，用于快速查询而无需重新遍历图。

### 反向索引：`.code2database_reverse_index.json`

每个节点的调用者/被调用者 O(1) 查找。

```json
{
  "lib_device_device_register": {
    "invokers": [
      {"id": "lib_device_device_start", "name": "device_start", "call_order": 2, "call_condition": "if(mode==1)"}
    ],
    "invoked": []
  }
}
```

### 条件索引：`.code2database_condition_index.json`

每个节点的分支条件，用于"哪条边会被执行"的查询。

```json
{
  "lib_device_device_start": [
    {"condition": "if(mode==1)", "target_node": "lib_device_device_start__cond_0", "target_name": "<conditional:if(mode==1)>", "condition_vars": [{"condition": "if(mode==1)", "vars": ["mode"]}]},
    {"condition": "!(mode==1)", "target_node": "lib_device_device_start__cond_0_else", "target_name": "<conditional:!(mode==1)>", "condition_vars": []}
  ]
}
```

### 调用链索引：`.code2database_chains.json`

从 API_entry 到端点的所有简单路径，在构建时预计算。

```json
{
  "total_chains": 5,
  "api_entries": 2,
  "endpoints": 3,
  "chains": [
    {
      "from_api": "lib_device_device_start",
      "to_endpoint": "pthread_create",
      "length": 2,
      "steps": [
        {"id": "lib_device_device_start", "name": "device_start", "labels": ["API_entry"], "is_empty": false, "condition": ""},
        {"id": "pthread_create", "name": "pthread_create", "labels": ["out_end"], "is_empty": false, "condition": ""}
      ]
    }
  ]
}
```

### 并发索引：`.code2database_concurrency_index.json`

线程派生关系和并发执行窗口。

```json
{
  "spawn_points": [
    {
      "node": "root_device_start",
      "name": "device_start",
      "spawns": [
        {
          "invoked": "pthread_create",
          "spawn_target": "worker_thread_fn",
          "spawn_arg": "NULL",
          "concurrency_type": "thread_spawn",
          "call_order": 1
        }
      ]
    }
  ],
  "thread_entries": [
    {
      "node": "root_worker_thread_fn",
      "name": "worker_thread_fn",
      "params": [{"name": "arg", "type": "void *", "is_param": true}],
      "spawned_by": [{"id": "root_device_start", "name": "device_start", "concurrency": "spawn_target"}],
      "spawn_arg": "NULL"
    }
  ],
  "concurrent_groups": [
    {
      "spawn_node": "root_device_start",
      "spawn_name": "device_start",
      "spawn_call_order": 1,
      "spawned_thread": "worker_thread_fn",
      "concurrent_with_thread": [
        {"id": "root_device_start__cond_0", "name": "<conditional:if(mode==1)>", "call_order": null}
      ],
      "concurrency_type": "thread_spawn"
    }
  ]
}
```

### 并发字段

| 字段 | 类型 | 描述 |
|------|------|------|
| `spawn_points[].node` | string | 创建线程的函数节点 ID |
| `spawn_points[].spawns[].spawn_target` | string | 在派生线程中运行的函数名称 |
| `spawn_points[].spawns[].spawn_arg` | string | 传递给派生函数的参数表达式 |
| `spawn_points[].spawns[].concurrency_type` | string | `"thread_spawn"`、`"goroutine"` 或 `"callback_register"` |
| `thread_entries[].node` | string | 作为线程入口运行的函数节点 ID |
| `thread_entries[].params` | array | 线程函数的形式参数（带 `is_param: true`） |
| `thread_entries[].spawned_by` | array | 派生此线程的来源：`{id, name, concurrency}` |
| `thread_entries[].spawn_arg` | string | 在派生点传递给此线程函数的参数 |
| `concurrent_groups[].spawn_node` | string | 创建线程的函数 |
| `concurrent_groups[].spawned_thread` | string | 派生线程函数的名称 |
| `concurrent_groups[].concurrent_with_thread` | array | 父函数中与派生线程并发执行的调用 |
| `concurrent_groups[].concurrency_type` | string | `"thread_spawn"`、`"goroutine"` 等 |

## 边置信度与来源（审计追踪）

每条边携带来源元数据，指示其发现方式和确定程度。

### 边置信度值

| 置信度 | 分数 | 含义 | 示例 |
|--------|------|------|------|
| `EXTRACTED` | 1.0 | AST 扫描器直接观测到——源码中的函数调用 | `device_start()` 调用 `register_device()`——在 tree-sitter AST 中可见 |
| `INFERRED` | 0.7–0.95 | 由 LLM 语义增强或插件添加 | LLM 解析回调目标：`register_callback(cb)` → `cb` 为 `my_handler` |
| `AMBIGUOUS` | 0.1–0.3 | 不确定——函数指针、动态分派、宏展开 | `(*func_ptr)()`——静态分析时目标未知 |

### 边来源值

| 来源 | 含义 |
|------|------|
| `"ast"` | tree-sitter 扫描器提取 |
| `"llm"` | Claude/LLM 语义增强 |
| `"manual"` | 用户手动提供 |
| `"plugin:<name>"` | 外部插件 |

### labels_source

节点标签也携带来源元数据：`"labels_source": {"thread_processor": "ast", "callback_func": "llm"}`。

### 更新后的边 Schema

```json
{
  "source": "lib_device_device_start",
  "target": "lib_device_device_register",
  "call_order": 2,
  "call_condition": "if(mode==1)",
  "concurrency": "",
  "confidence": "EXTRACTED",
  "source": "ast",
  "confidence_score": 1.0
}
```

## 紧凑域文件格式（v3）

域文件现在使用将摘要数据与详情分离的紧凑格式，减少 token 浪费：

```json
{
  "type": "code2database_domain",
  "domain": "lib.device",
  "functions": [
    ["lib_device_device_start", "device_start", "lib/device/device.c", 30, "[\"API_entry\",\"thread_processor\"]", "void device_start(int mode)"],
    ["lib_device_device_register", "register_device", "lib/device/device.c", 50, "[]", "int register_device(struct device *dev)"]
  ],
  "function_details": {
    "lib_device_device_start": {
      "location": "lib/device/device.c:30",
      "labels_source": {"thread_processor": "ast", "API_entry": "ast"},
      "api_constraints": "mode: int",
      "params": [{"name": "mode", "type": "int", "is_param": true}],
      "local_vars": [{"name": "rc", "type": "int", "value_snippet": "0", "line": 31, "is_param": false}],
      "callee_args": [{"call_order": 1, "invoked": "pthread_create", "args_snippet": "&tid, NULL, worker_thread_fn, NULL", "concurrency_info": {"is_spawn": true, "spawn_target": "worker_thread_fn", "spawn_arg": "NULL", "concurrency_type": "thread_spawn"}}],
      "condition_vars": [{"condition": "if(mode==1)", "vars": ["mode"]}]
    }
  },
  "empty_nodes": [
    ["lib_device_device_start__cond_0", "if(mode == 1)", "lib_device_device_start"]
  ],
  "edge_fields": ["source", "target", "call_order", "call_condition",
                  "concurrency", "confidence", "source_tag", "confidence_score"],
  "edges": [
    ["lib_device_device_start", "lib_device_device_register", 1, "if(mode==1)", "", "EXTRACTED", "ast", 1.0],
    ["lib_device_device_start", "lib_device_device_open", 2, "", "", "INFERRED", "llm", 0.8,
     {"pc": "#ifdef(HAVE_CONFIG_H)", "pa": true, "ev": [{"kind": "ast_call", "weight": 1.0, "note": "direct call at line 42"}]}]
  ]
}
```

### 边位置映射（v3 紧凑格式）

| 位置 | 字段 | 描述 |
|------|------|------|
| 0 | `source` | 调用者节点 ID |
| 1 | `target` | 被调用者节点 ID |
| 2 | `call_order` | 调用者函数体内的顺序 |
| 3 | `call_condition` | 调用发生的条件 |
| 4 | `concurrency` | 并发类型（空 = 顺序） |
| 5 | `confidence` | `"EXTRACTED"`、`"INFERRED"` 或 `"AMBIGUOUS"` |
| 6 | `source_tag` | 来源标签：`"ast"`、`"llm"`、`"preproc_dead"`、`"plugin:<name>"` |
| 7 | `confidence_score` | 0.0–1.0 |
| 8+ | extras dict | 稀疏的 `{pc, pa, ev}`，用于 preproc_condition、preproc_alive、evidence |

**重要**：位置 6 是 `source_tag`（来源标签），不是 `source`（位置 0 的调用者节点 ID）。加载到图中时，`source_tag` 映射到边属性 `source`。

### v3 格式与 v1/v2 的差异

- `functions[]` 为紧凑数组：`[id, name, source_file, line, labels_json, signature]`
- `function_details{}` 包含按需加载的大量数据
- `empty_nodes[]` 为紧凑数组：`[id, condition, parent_id]`
- `edges[]` 使用基于位置的数组，配合 `edge_fields` 头部（比字典格式节省约 30-40% token）
- `source_tag` 字段将来源标签与调用者节点 ID（`source`）分离
- 边数组末尾的 extras dict 存储稀疏的可选字段
- 旧版 v1/v2 格式仍可由 `_load_full_graph()` 读取

## 上下文包：`.code2database_context_pack.json`

整个项目的单文件 LLM 上下文。无需读取多个文件即可获得完整的心智模型。

```json
{
  "project_summary": {
    "source_root": "/path/to/source",
    "total_functions": 142,
    "total_domains": 8,
    "api_entries": ["device_start", "register_device"],
    "thread_entries": ["worker_thread_fn"],
    "callback_entries": [],
    "shallow_domains": ["module.device.nvme"],
    "deep_domains": ["lib.device"],
    "total_nodes": 200,
    "total_edges": 350
  },
  "domain_map": {
    "lib.device": {"apis": 2, "internal": 12, "ratio": 0.14, "endpoints": 5, "depends_on": ["external"]},
    "module.device.nvme": {"apis": 8, "internal": 6, "ratio": 0.57, "endpoints": 2, "depends_on": ["lib.device"]}
  },
  "api_catalog": [
    {"id": "root_device_start", "name": "device_start", "signature": "void device_start(int mode)", "domain": "root"}
  ],
  "execution_scenarios": [
    {
      "trigger": "device_start(mode=1)",
      "chain": ["device_start", "→[spawn]worker_thread_fn", "→[if(mode==1)]register_device", "→open_device"],
      "condition": "mode == 1"
    }
  ],
  "data_flow_index": {
    "mode": {"type": "int", "defined_in": "device_start(param)", "flows_to_conditions": ["if(mode==1)"], "affects_callees": ["register_device", "open_device", "close_device"]}
  },
  "concurrency_summary": {
    "spawn_points": 1,
    "concurrent_windows": [
      {"spawn_at": "lib/device/device.c:31", "spawn_fn": "device_start", "thread_fn": "worker_thread_fn", "main_thread_calls": ["register_device", "open_device"]}
    ]
  },
  "edge_confidence": {"EXTRACTED": 320, "INFERRED": 5, "AMBIGUOUS": 2}
}
```

## 执行场景：`.code2database_scenarios.json`

每个 API_entry 的详细预计算场景，带有枚举驱动的分支解析。

```json
{
  "total_scenarios": 2,
  "scenarios": [
    {
      "trigger": "device_start(mode=1)",
      "binding": {"mode": "1"},
      "resolved_chain": [
        {"step": 1, "action": "spawn", "target": "worker_thread_fn", "condition": "", "branch": "", "concurrent": true, "confidence": "EXTRACTED"},
        {"step": 2, "action": "call", "target": "register_device", "condition": "if(mode==1)", "branch": "then", "concurrent": false, "confidence": "EXTRACTED"},
        {"step": 3, "action": "call", "target": "open_device", "condition": "", "branch": "", "concurrent": false, "confidence": "EXTRACTED"}
      ],
      "pruned_branches": [
        {"condition": "!(mode==1)", "dead_target": "close_device", "reason": "condition false per binding {'mode': '1'}"}
      ],
      "concurrent_window": [
        {"spawn_at": "root_device_start:1", "thread_fn": "worker_thread_fn", "main_thread_calls": ["register_device", "open_device"]}
      ]
    }
  ]
}
```

## 人类可读摘要：`CODE2DATABASE_SUMMARY.md`

自动生成的 Markdown 文件，位于 `code2db-out/CODE2DATABASE_SUMMARY.md`。包含：
- 架构概览（域表及深度比）
- 公共 API 目录（函数、域、签名、约束）
- 并发图（派生点、并发调用、风险）
- 关键路径（API→端点，按长度降序）
- 外部端点（分类、描述、调用者）
- 数据流热点（参数流链）
- 边置信度分布

每个域子目录中还会生成对应的 `DOMAIN_README.md` 文件。

## 执行场景：`SCENARIOS_SUMMARY.md`

自动生成的 Markdown 文件，位于 `code2db-out/SCENARIOS_SUMMARY.md`。包含从 API 入口点追踪的执行场景表：

| 列 | 描述 |
|----|------|
| # | 场景编号 |
| Trigger | API 入口函数名 |
| Path | 从入口点的调用链（→ 分隔，最多 8 跳） |
| Concurrent | 链中的并发线程/回调函数 |
| Pruned Branches | 从主路径排除的条件分支 |

机器可读数据：`.code2database_scenarios.json`，包含 `trigger`、`resolved_chain`、`concurrent_window` 和 `pruned_branches` 字段。

## 架构流：`ARCHITECTURE_FLOWS.md`

自动生成的 Markdown 文件，位于 `code2db-out/ARCHITECTURE_FLOWS.md`。包含：

- **前 5 条流**：从 API 入口到端点的最长调用链，标注条件、并发和域交叉
- **域流图**：按边数排名的跨域调用（哪些域调用哪些域）
- **枢纽函数**：按入度+出度排名的连接度最高的函数

在 NetworkX（内存）和 SQLite（流式/低内存）两种构建路径下均会生成。

## 插件架构

在 `.code2database_plugins/` 目录下放置 Python 脚本，或通过 `--plugin` 标志指定。

### 插件接口

```python
class Code2DatabasePlugin:
    def enrich_functions(self, functions, edges, source_root):
        """Add/modify function data before graph build. Return (functions, edges)."""
        return functions, edges

    def enrich_graph(self, G):
        """Modify networkx DiGraph after build. Return G."""
        return G
```

### 插件边标注约定

当插件添加边时，应设置：
- `confidence`: "INFERRED" (0.7-0.95) 或 "AMBIGUOUS" (0.1-0.3)
- `source`: "plugin:<name>"
- `confidence_score`: float 0.0-1.0

## 构建配置文件：`.code2database_build_config.json`

当指定 `--build-config` 时由 `build` 命令生成。包含构建系统检测结果和宏绑定。

```json
{
  "build_system": "cmake",
  "config_files": ["CMakeLists.txt"],
  "selected_config": "Release",
  "defined_macros": {
    "NDEBUG": "",
    "HAVE_CONFIG_H": "1",
    "FEATURE_A": "1"
  },
  "targets": [
    {"name": "mylib", "type": "library", "sources": [], "depends_on": []}
  ],
  "include_dirs": ["include", "lib"]
}
```

### 构建配置字段

| 字段 | 类型 | 描述 |
|------|------|------|
| `build_system` | string | 检测到的构建系统：`"cmake"`、`"make"`、`"spec"`、`"meson"`、`"autotools"`、`"kconfig"`、`"bazel"` 或 `""`（未检测到） |
| `config_files` | string[] | 检测到的构建配置文件的相对路径 |
| `selected_config` | string | 选定的构建配置名称（如 `"Release"`、`"Debug"`） |
| `defined_macros` | object | 宏名 → 值的映射。标志宏（`-DNDEBUG`）值为空字符串，有值宏（`-DVERSION=2`）值为对应值 |
| `targets` | array | 构建目标，包含名称、类型、源文件和依赖信息 |
| `include_dirs` | string[] | 检测到的 include 目录 |

### 预处理器条件评估

当提供宏绑定时，扫描器会评估 `#ifdef`/`#ifndef`/`#if`/`#elif` 条件：

| 条件 | 评估 |
|------|------|
| `#ifdef MACRO` | 如果 MACRO 在 defined_macros 中则为存活 |
| `#ifndef MACRO` | 如果 MACRO 不在 defined_macros 中则为存活 |
| `#if defined(MACRO)` | 与 #ifdef 相同 |
| `#if MACRO` | 如果 MACRO 已定义且值为非零/非空则为存活 |
| `#if MACRO == value` | 如果 defined_macros[MACRO] 等于 value 则为存活 |
| `#if MACRO != value` | 如果 defined_macros[MACRO] 不等于 value 则为存活 |
| `#if A && B` | 如果 A 和 B 都存活则为存活 |
| `#if A \|\| B` | 如果 A 或 B 任一存活则为存活 |

保守策略：如果评估不确定，分支被视为存活（不会产生错误排除）。

## 社区检测文件：`.code2database_communities.json`

由 `build` 命令使用 Leiden 算法对调用图生成。

```json
{
  "total_communities": 5,
  "communities": [
    {
      "id": "community_0",
      "label": "lib → device",
      "heuristic_label": "lib → device",
      "keywords": ["device", "open", "close", "read"],
      "node_ids": ["lib_device_device_open", "lib_device_device_close"],
      "cohesion": 0.333,
      "symbol_count": 3
    }
  ],
  "node_community": {
    "lib_device_device_open": "community_0"
  }
}
```

### 社区字段

| 字段 | 类型 | 描述 |
|------|------|------|
| `id` | string | 社区标识符（如 `"community_0"`） |
| `label` | string | 人类可读的社区标签（从文件夹/名称模式生成） |
| `heuristic_label` | string | 与 label 相同——由启发式规则生成 |
| `keywords` | string[] | 从成员函数名提取的代表关键词 |
| `node_ids` | string[] | 社区成员的节点 ID |
| `cohesion` | float | 内部边密度（0.0–1.0） |
| `symbol_count` | integer | 社区中的函数数量 |

当 igraph/leidenalg 不可用时，回退到基于域的分组。

## 入口点评分文件：`.code2database_entry_scores.json`

```json
{
  "total_scored": 25,
  "top_entries": [
    {"id": "module_main", "name": "main", "score": 4.0, "domain": "module"},
    {"id": "lib_device_device_start", "name": "device_start", "score": 2.5, "domain": "lib.device"}
  ]
}
```

评分公式：`baseScore × exportMultiplier × nameMultiplier × frameworkMultiplier`

| 因子 | 乘数 | 条件 |
|------|------|------|
| baseScore | invoked/(invoker+1) | 始终 |
| exportMultiplier | 2.0 | API_entry 标签 |
| nameMultiplier | 1.5 | handle_/on_/main/run 模式 |
| nameMultiplier | 0.3 | get_/set_/is_/has 模式 |
| frameworkMultiplier | 1.2–1.5 | 检测到 SPDK/Django/Spring 等 |

## 进程检测文件：`.code2database_processes.json`

```json
{
  "total_processes": 3,
  "processes": [
    {
      "entry_point": "module_main",
      "entry_name": "main",
      "entry_score": 4.0,
      "label": "main → ... → cleanup",
      "step_count": 8,
      "steps": ["main", "init", "run", "process_data", "cleanup"],
      "step_ids": ["module_main", "module_init", ...],
      "communities_crossed": 2
    }
  ]
}
```

### 进程字段

| 字段 | 类型 | 描述 |
|------|------|------|
| `entry_point` | string | 入口函数的节点 ID |
| `entry_name` | string | 入口函数名 |
| `entry_score` | float | 入口点评分 |
| `label` | string | 人类可读的进程标签 |
| `step_count` | integer | BFS 追踪中的函数数量 |
| `steps` | string[] | 追踪顺序的函数名 |
| `step_ids` | string[] | 追踪顺序的节点 ID |
| `communities_crossed` | integer | 跨越的 Leiden 社区数量 |

## 边证据

边携带 `evidence` 数组，记录边被发出的原因：

```json
"evidence": [
  {"kind": "ast_call", "weight": 1.0, "note": "direct call at line 42"},
  {"kind": "import_resolution", "weight": 0.75, "note": "resolved via #include <device.h>"}
]
```

| 类型 | 权重 | 描述 |
|------|------|------|
| `ast_call` | 1.0 | AST 直接提取——源码中的调用表达式 |
| `ast_call` | 0.0 | 无效分支——预处理器条件排除了此调用 |
| `import_resolution` | 0.75 | 通过 #include 链解析 |
| `thread_spawn` | 0.85 | 推断的线程派生目标 |

插件可以添加自己的证据条目，`kind` 为 `"plugin:<name>"`。

---

## 知识目录 Schema

位于 `code2db-out/knowledge/`。

### index.json

```json
{
  "files": [
    {"name": "architecture.md", "size": 1234, "headings": ["Overview", "Components"]}
  ],
  "topics": ["Overview", "Components", "API Constraints"]
}
```

### knowledge_pack_lite.json（约 300 token）

```json
{
  "files": ["architecture.md", "module_lib_device.md"],
  "topics": ["Overview", "API Constraints"],
  "architecture_summary": "... (first 500 chars of architecture.md)"
}
```

### knowledge_pack_standard.json（约 800 token）

```json
{
  "files": [{"name": "architecture.md", "headings": ["Overview"]}],
  "architecture": "... (first 2000 chars)",
  "module_summaries": {"lib_device": "... (first 200 chars per module)"},
  "constraints": "...",
  "glossary": "..."
}
```

---

## 记忆目录 Schema

位于 `code2db-out/memory/`。

### index.json

```json
{
  "entries": [
    {"id": 1, "question": "How does device init?", "tags": ["device"], "status": "trusted", "root_id": 1}
  ],
  "next_id": 5,
  "roots": [{"id": 1, "question": "How does device init?"}]
}
```

### 根记忆（root/root_<id>.json）

```json
{
  "id": 1,
  "question": "How does device init?",
  "answer": "Initialization sequence...",
  "root_id": 1,
  "tags": ["device", "init"],
  "node_ids": ["lib_device_init"],
  "status": "trusted",
  "weight": 1.5,
  "created": "2026-07-09T10:00:00",
  "last_accessed": "2026-07-09T12:00:00",
  "merged_count": 2,
  "access_count": 3,
  "reshaped_count": 0,
  "versions": [
    {"answer": "Old answer...", "version": 1, "merged_from": 2}
  ]
}
```

### 叶记忆（leaf/mem_<id>.json）

格式与根记忆相同，但 `root_id` 指向父根记忆。

### 分层索引（L0/L1/L2_index.json）

每个索引包含按权重过滤的条目：
- L0：weight > 0.7（热）
- L1：weight 0.3-0.7（温）
- L2：weight < 0.3（冷）

### memory_pack_lite.json（约 200 token）

```json
{
  "top_questions": ["Q1", "Q2"],
  "hot_memories": [{"id": 1, "q": "Question", "w": 1.5}]
}
```

### memory_pack_standard.json（约 600 token）

```json
{
  "top_questions": ["Q1", "Q2"],
  "all_hot": [{"id": 1, "q": "Question", "a": "Answer", "w": 1.5, "tags": ["tag1"]}],
  "warm_summaries": [{"id": 3, "q": "Question", "a": "Summary", "w": 0.5}]
}
```

### 临时记忆（.scratch/session_<id>.json）

```json
{
  "session_id": "abc123",
  "chain_context": {"chains": [...], "bindings": {...}},
  "react_state": {"step": "analyze", "conclusion": "..."},
  "saved_at": "2026-07-09T10:00:00"
}
```

---

## 过时节点 Schema

被 light-scan 或 patch-from-git 标记为过时的节点具有额外的属性：

```json
{
  "stale": true,
  "semantic_desc": ""
}
```

当 `describe-node` 遇到带有 `--lazy-fill` 的过时节点时，它会自动从源文件提取基本信息（body_text、signature），无需 LLM 参与。

---

## 补丁边 Schema

由补丁操作添加的边使用 `source_tag: "patch"`：

```json
["source_id", "target_id", null, "", "", "EXTRACTED", "patch", 1.0]
```
