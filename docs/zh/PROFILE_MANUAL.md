# Profile 手动编写指南

本文档说明如何为 Code2Database 手动编写项目配置文件（profile）。

> **Profile 是让 Code2Database 与项目无关的关键。** 与其硬编码"pthread_create 是线程创建"或"file_operations 是 vtable 结构体"，Profile 让每个项目声明自己的约定：回调注册模式、导出宏、struct_op_types、域规则、锁模式。换一个 JSON 文件，同一个 scanner + builder 就能理解一个完全不同的代码库。这就是工具如何为*你的*项目构建代码数据库——不是一个勉强适配的通用图谱。

---

## 一、什么是 Profile

Profile 是一个 JSON 文件，声明项目特定的知识，让扫描器和构建器能正确解析代码中的：
- 哪些函数名应跳过（如宏、内联工具函数）
- 哪些函数是公共 API
- 回调注册机制（如 `pthread_create` 的回调参数）
- 宏驱动的注册分发（如 `RTE_INIT` 构造宏）
- 端点分类规则（如哪些函数是程序入口）
- 条件编译宏前缀（如 `CONFIG_`、`RTE_`）

**核心原则**：Profile 始终与内置默认值 (`_default.json`) 深度合并。你只需声明与默认值不同的部分。

---

## 二、快速开始

### 最小 Profile

```json
{
  "version": 1,
  "project": {
    "name": "myproject"
  },
  "api_detection": {
    "public_prefixes": ["my_"]
  }
}
```

### 完整结构概览

```json
{
  "version": 1,
  "project": { ... },
  "detection": { ... },
  "project_name_aliases": { ... },
  "skip_names": { ... },
  "api_detection": { ... },
  "callback_detection": { ... },
  "endpoint_classification": { ... },
  "macro_heuristics": { ... },
  "macro_dispatch": { ... },
  "struct_embeddings": { ... },
  "threading_models": { ... },
  "scan_hints": { ... },
  "phases": { ... }
}
```

---

## 三、逐字段详细说明

---

### 3.1 `version`（必填）

| 属性 | 值 |
|------|-----|
| 类型 | 整数 |
| 必填 | 是 |
| 当前唯一合法值 | `1` |

```json
"version": 1
```

---

### 3.2 `project`（项目标识）

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | string | `""` | 项目名称，用于输出标签和显示 |
| `language` | string | `"c"` | 项目语言——之一：`"c"`（C/C++）、`"go"`、`"python"`、`"java"`、`"rust"`、`"asm"`。每种语言都有内置 profile（`_default.json`、`go_default.json`、`python_default.json`、`java_default.json`、`rust_default.json`、`asm_default.json`）。`language` 字段是信息性的——扫描器按文件扩展名自动检测语言；此字段用于 `auto-profile` 时的 profile 模板匹配。 |
| `project_type` | string | 无 | 项目类型标识（如 `"dpdk"`、`"linux_kernel"`），用于模板匹配 |
| `detected_frameworks` | string[] | `[]` | 检测到的框架名列表 |

```json
"project": {
  "name": "dpdk",
  "language": "c",
  "project_type": "dpdk",
  "detected_frameworks": ["dpdk"]
}
```

---

### 3.3 `detection`（自动检测规则，仅用于模板）

此节**不影响扫描/构建**，仅用于 `auto-profile` 命令检测项目类型。手动编写时通常不需要。

| 字段 | 类型 | 说明 |
|------|------|------|
| `dir_markers` | string[] | 目录存在时得分（每个 +10） |
| `file_markers` | string[] | 文件存在时得分（每个 +15） |
| `content_markers` | object[] | 内容模式检测，每项含 `pattern`、`dirs`、`min_hits` |
| `dir_structure` | object | 目录路径检测，格式 `{key: path}` |
| `build_system` | string | 构建系统类型（`"meson"`、`"kbuild"` 等） |
| `macro_prefixes` | string[] | `#ifdef` 宏前缀检测（匹配时 +20） |
| `priority` | integer | 同分时优先级权重 |

```json
"detection": {
  "dir_markers": ["lib", "drivers"],
  "content_markers": [
    {"pattern": "rte_eal_init", "dirs": ["lib", "drivers"], "min_hits": 1}
  ],
  "build_system": "meson",
  "macro_prefixes": ["RTE_"],
  "priority": 30
}
```

---

### 3.4 `project_name_aliases`（项目名别名，仅用于模板）

格式：`{别名: 标准名}`。用于 `auto-profile` 匹配目录名到项目。

```json
"project_name_aliases": {
  "dpdk-source": "dpdk",
  "dpdk-src": "dpdk"
}
```

---

### 3.5 `skip_names`（跳过名称） ⭐ 关键

控制扫描器忽略的函数/宏名。这些名称不会产生调用边。

#### `skip_names.add` — 追加跳过项

| 属性 | 值 |
|------|-----|
| 类型 | string[] |
| 默认值 | ~340 个通用 C 名称（malloc、free、printf 等） |
| 作用 | 添加到跳过集合 |

**何时添加**：
- 项目特有的纯表达式宏（如 `RTE_MIN`、`RTE_MAX`、`RTE_DIM`）
- 断言/panic 宏（如 `rte_panic`、`RTE_ASSERT`）
- 对齐属性宏（如 `__rte_cache_aligned`、`__rte_aligned`）
- 字节序转换函数（如 `rte_cpu_to_be_16`、`rte_be_to_cpu_32`）
- 日志宏（如 `RTE_LOG_DP`、`PMD_DRV_LOG`）

```json
"skip_names": {
  "add": [
    "RTE_MIN", "RTE_MAX", "RTE_DIM",
    "rte_panic", "RTE_ASSERT",
    "__rte_cache_aligned", "__rte_aligned",
    "rte_cpu_to_be_16", "rte_cpu_to_be_32",
    "rte_be_to_cpu_16", "rte_be_to_cpu_32"
  ]
}
```

#### `skip_names.external_lib_prefixes` — 外部库前缀

| 属性 | 值 |
|------|-----|
| 类型 | `{前缀: {category: string, visible: bool}}` |
| 默认值 | `pthread_`、`sem_`、`uuid_`、`numa_`、`aio_` 等 |
| 作用 | `visible: true` → 创建边到外部端点；`visible: false` → 静默跳过 |

**何时配置**：
- 项目依赖的外部库应在此声明
- `category` 用于端点分类（如 `external_posix`、`external_openssl`、`external_vendor`）
- `visible: true` 表示这些调用会产生外部端点节点（在调用图中可见）
- `visible: false` 表示静默忽略（不产生任何节点或边）

```json
"external_lib_prefixes": {
  "pthread_": {"category": "external_posix", "visible": false},
  "mlx5_": {"category": "external_vendor", "visible": true},
  "ibv_": {"category": "external_lib", "visible": true},
  "SSL_": {"category": "external_openssl", "visible": true}
}
```

#### `skip_names.test_framework_prefixes` — 测试框架前缀

| 属性 | 值 |
|------|-----|
| 类型 | string[] |
| 默认值 | `[]` |
| 作用 | 测试框架函数前缀，用于标记测试代码 |

```json
"test_framework_prefixes": ["CU_"]
```

---

### 3.6 `api_detection`（API 检测） ⭐ 关键

#### `api_detection.public_prefixes` — 公共 API 前缀

| 属性 | 值 |
|------|-----|
| 类型 | string[] |
| 默认值 | `[]` |
| 作用 | 标记以这些前缀开头的函数为公共 API 入口点 |

```json
"public_prefixes": ["rte_"]
```

#### `api_detection.internal_patterns` — 内部标记

| 属性 | 值 |
|------|-----|
| 类型 | string[] |
| 默认值 | `["_unit_", "_ut_", "_test_", "_perf_", "_verify_", "_example_", "_internal", "_priv", "_stub", "_mock"]` |
| 作用 | 函数名包含这些子串时，标记为内部/测试代码（非公共 API） |

**何时修改**：如果项目有特殊的内部标记前缀（如 `__rte_`），添加到此处。

```json
"internal_patterns": ["_unit_", "_ut_", "_test_", "__rte_"]
```

#### `api_detection.public_header_paths` — 公共头文件路径

| 属性 | 值 |
|------|-----|
| 类型 | string[]（相对于源码根目录的路径） |
| 默认值 | `[]` |
| 作用 | 包含公共头文件的目录，用于 LLM 分析和 API 表面识别 |

```json
"public_header_paths": [
  "lib/eal/include",
  "lib/mempool/include",
  "lib/net/include",
  "lib/mbuf/include"
]
```

#### `api_detection.export_macros` — 导出宏

| 属性 | 值 |
|------|-----|
| 类型 | string[] |
| 默认值 | `[]` |
| 作用 | 标记导出符号的宏名，扫描器识别被这些宏包裹的函数为入口点 |

```json
"export_macros": ["EXPORT_SYMBOL", "EXPORT_SYMBOL_GPL"]
```

#### `api_detection.struct_op_types` — 虚表结构体类型

| 属性 | 值 |
|------|-----|
| 类型 | string[] |
| 默认值 | `[]` |
| 作用 | 包含函数指针表的结构体名称（vtable），扫描器会为其中的函数指针创建隐式调用边 |

```json
"struct_op_types": [
  "file_operations",
  "inode_operations",
  "platform_driver",
  "net_device_ops"
]
```

#### `api_detection.auto_detect` — 自动检测标志

| 属性 | 值 |
|------|-----|
| 类型 | boolean |
| 默认值 | `false` |
| 作用 | 标记此 profile 是自动生成的，允许扫描器/构建器动态补充 |

手动编写 profile 时设为 `false`（或不设置，默认即 `false`）。

---

### 3.7 `callback_detection`（回调检测） ⭐⭐ 最关键

这是 profile 中**影响最大**的部分。正确声明回调模式直接影响调用图能否识别函数指针调用链。

#### `callback_detection.static_patterns` — 静态回调模式

| 属性 | 值 |
|------|-----|
| 类型 | object[] |
| 默认值 | `[pthread_create 模式]` |
| 作用 | 定义回调注册函数及其参数模式 |

每个条目包含 4 个必填字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `register_func` | string | 注册回调的函数名 |
| `regex` | string | 从调用点提取回调函数名的正则表达式（必须有一个捕获组） |
| `cb_arg_index` | integer | 回调参数的 0 起始位置 |
| `concurrency_type` | string | 并发类型 |

**concurrency_type 可选值**：

| 值 | 含义 |
|----|------|
| `spawn_target` | 创建新线程/进程的入口函数 |
| `callback` | 通用回调注册（中断、信号等） |
| `callback_register` | 回调注册函数 |
| `poller` | 轮询回调（定时器、work item） |
| `timer_callback` | 定时器回调 |

**如何编写 regex**：
- 正则必须匹配函数调用语句，回调函数名用 `(\w+)` 捕获
- 其他参数用 `[^,]*` 或 `[^,)]+` 跳过
- 示例：`pthread_create(&thread, NULL, my_thread_fn, arg)` → regex: `pthread_create\s*\(\s*[^,]*,\s*[^,]*,\s*(\w+)`

```json
"static_patterns": [
  {
    "register_func": "pthread_create",
    "regex": "pthread_create\\s*\\(\\s*[^,]*,\\s*[^,]*,\\s*(\\w+)",
    "cb_arg_index": 2,
    "concurrency_type": "spawn_target"
  },
  {
    "register_func": "rte_eal_mp_remote_launch",
    "regex": "rte_eal_mp_remote_launch\\s*\\(\\s*(\\w+)",
    "cb_arg_index": 0,
    "concurrency_type": "spawn_target"
  },
  {
    "register_func": "request_irq",
    "regex": "request_irq\\s*\\(\\s*[^,]*,\\s*(\\w+)",
    "cb_arg_index": 1,
    "concurrency_type": "callback"
  },
  {
    "register_func": "timer_setup",
    "regex": "timer_setup\\s*\\(\\s*[^,]*,\\s*(\\w+)",
    "cb_arg_index": 1,
    "concurrency_type": "poller"
  }
]
```

#### `callback_detection.generic_cb_suffixes` — 通用回调后缀

| 属性 | 值 |
|------|-----|
| 类型 | string[] |
| 默认值 | `["_cb", "_fn", "_handler", "_callback"]` |
| 作用 | 函数名以这些后缀结尾时，启发式识别为回调函数 |

通常不需要修改。

#### `callback_detection.skip_call_prefixes` — 跳过的调用前缀

| 属性 | 值 |
|------|-----|
| 类型 | string[] |
| 默认值 | `[]` |
| 作用 | 排除特定前缀的调用不作为回调检测 |

```json
"skip_call_prefixes": ["trace_"]
```

#### `callback_detection.skip_callees` — 跳过的被调函数

| 属性 | 值 |
|------|-----|
| 类型 | string[] |
| 默认值 | `[]` |
| 作用 | 在跨文件回调检测中跳过特定被调函数名 |

---

### 3.8 `endpoint_classification`（端点分类）

#### `endpoint_classification.lib_prefix_map` — 库前缀映射

| 属性 | 值 |
|------|-----|
| 类型 | `{前缀: 分类名}` |
| 默认值 | `{"pthread_": "external_posix", "sem_": "external_posix", "epoll_": "external_posix"}` |
| 作用 | 将函数前缀映射到端点分类类别，用于构建器标记外部端点 |

```json
"lib_prefix_map": {
  "pthread_": "external_posix",
  "mlx5_": "external_vendor",
  "ibv_": "external_lib",
  "SSL_": "external_openssl"
}
```

#### `endpoint_classification.endpoint_rules` — 端点规则

| 属性 | 值 |
|------|-----|
| 类型 | object[]，每项含 `pattern`（正则）和 `endpoint_type` |
| 默认值 | `[]` |
| 作用 | 用正则匹配函数名，标记特定函数为端点 |

```json
"endpoint_rules": [
  {"pattern": "^main$", "endpoint_type": "program_entry"},
  {"pattern": "^rte_eal_init$", "endpoint_type": "program_entry"},
  {"pattern": "^rte_eal_mp_remote_launch$", "endpoint_type": "thread_entry"}
]
```

---

### 3.9 `macro_heuristics`（宏启发式）

#### `macro_heuristics.macro_condition_prefixes` — 条件编译宏前缀

| 属性 | 值 |
|------|-----|
| 类型 | string[] |
| 默认值 | `["HAVE_", "ENABLE_", "WITH_", "USE_"]` |
| 作用 | `#ifdef` 条件宏前缀，扫描器将这些识别为条件编译守卫而非函数调用 |

```json
"macro_condition_prefixes": ["RTE_", "HAVE_", "ENABLE_", "WITH_", "USE_"]
```

---

### 3.10 `macro_dispatch`（宏分发） ⭐⭐ 关键

#### `macro_dispatch.registration_macros` — 注册宏

| 属性 | 值 |
|------|-----|
| 类型 | object[] |
| 默认值 | `[]` |
| 作用 | 定义基于构造函数的注册宏，用于自动注册模块/驱动 |

**必填字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `macro_name` | string | 宏名称 |
| `pattern` | string | 匹配宏调用的正则（捕获组提取参数） |
| `struct_arg_index` | integer ≥ 0 | 哪个捕获组包含结构体/入口变量名 |

**可选字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `register_func` | string | 构造函数体内调用的注册函数名 |
| `global_list_var` | string | 全局链表变量名 |
| `iterator_func` | string | 遍历全局链表的函数名 |
| `dispatch_field` | string | 遍历时调用的分发字段名 |
| `handler_arg_index` | integer | handler 参数位置 |
| `dispatch_caller` | string | 分发调用函数名 |
| `generates` | string | 生成类型：`constructor`、`driver_register`、`bus_register` 等 |
| `_confidence` | string | 置信度：`high`、`medium`、`low` |
| `_needs_review` | boolean | 是否需要人工审核 |

```json
"registration_macros": [
  {
    "macro_name": "RTE_INIT",
    "pattern": "RTE_INIT\\s*\\(\\s*(\\w+)\\s*\\)",
    "struct_arg_index": 0,
    "generates": "constructor",
    "_confidence": "high"
  },
  {
    "macro_name": "RTE_PMD_REGISTER_PCI",
    "pattern": "RTE_PMD_REGISTER_PCI\\s*\\(\\s*([^,)]+)\\s*,\\s*([^,)]+)\\s*\\)",
    "struct_arg_index": 1,
    "register_func": "rte_pci_register",
    "iterator_func": "rte_pci_find_device",
    "generates": "driver_register",
    "_confidence": "high"
  }
]
```

#### `macro_dispatch.token_paste_macros` — Token 粘合宏

| 属性 | 值 |
|------|-----|
| 类型 | object[] |
| 默认值 | `[]` |
| 作用 | 使用 `##` 粘合运算符生成函数名的宏 |

**必填字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `macro_name` | string | 宏名称 |
| `template` | string | `##` 表达式（如 `"name##_register"`） |
| `param_names` | string[] | 宏参数名列表 |

```json
"token_paste_macros": [
  {
    "macro_name": "RTE_INIT",
    "template": "RTE_INIT##_func",
    "param_names": ["name"],
    "generates": "constructor"
  }
]
```

#### `macro_dispatch.macro_aliases` — 宏别名

| 属性 | 值 |
|------|-----|
| 类型 | `{宏名: 展开目标}` |
| 默认值 | `{}` |
| 作用 | 简单宏到目标的映射 |

```json
"macro_aliases": {}
```

---

### 3.11 `struct_embeddings`（结构体嵌入）

#### `struct_embeddings.container_of_macros` — container_of 宏

| 属性 | 值 |
|------|-----|
| 类型 | object[] |
| 默认值 | `[]` |
| 作用 | `container_of` 风格的宏定义，用于指针算术解析 |

```json
"container_of_macros": [
  {"macro_name": "container_of"}
]
```

#### `struct_embeddings.manual_entries` — 手动嵌入关系

| 属性 | 值 |
|------|-----|
| 类型 | object[] |
| 默认值 | `[]` |
| 作用 | 手动声明的结构体嵌入关系 |

**必填字段**：`outer_type`、`member`、`inner_type`
**可选字段**：`domain_hint`

```json
"manual_entries": [
  {
    "outer_type": "my_device",
    "member": "base",
    "inner_type": "device_base",
    "domain_hint": "drivers"
  }
]
```

---

### 3.12 `threading_models`（线程模型）

| 属性 | 值 |
|------|-----|
| 类型 | `{模型名: [函数名列表]}` |
| 默认值 | `{}` |
| 作用 | 定义线程/并发模型，用于并发分析 |

```json
"threading_models": {
  "kernel_thread": ["kthread_create", "kthread_run", "kthread_create_on_node"],
  "workqueue": ["INIT_WORK", "INIT_DELAYED_WORK"]
}
```

---

### 3.13 `scan_hints`（扫描提示）

#### `scan_hints.domain_rules` — 域规则 ⭐

| 属性 | 值 |
|------|-----|
| 类型 | object[] |
| 默认值 | `[]` |
| 作用 | 按函数名模式分配域后缀/标签/合并目标 |

**必填字段**：`pattern`（正则）
**至少包含一个动作字段**：`domain_suffix`、`domain_tag`、`merge_to`、`label`

```json
"domain_rules": [
  {"pattern": "ext4_mb_.*", "domain_suffix": "mballoc"},
  {"pattern": "__ext4_.*", "domain_suffix": "internal"},
  {"pattern": "^app\\.test-", "domain_tag": "test"},
  {"pattern": "^lib\\.eal\\.", "label": "core_eal"}
]
```

#### `scan_hints.header_priority_dirs` — 头文件优先目录

| 属性 | 值 |
|------|-----|
| 类型 | string[] |
| 默认值 | `["include"]` |
| 作用 | LLM 分析和 API 检测时优先扫描的目录 |

#### `scan_hints.vtable_module_keys` — 虚表模块键

| 属性 | 值 |
|------|-----|
| 类型 | string[] |
| 默认值 | `[]` |
| 作用 | vtable 结构体中标识所属模块的字段名 |

```json
"vtable_module_keys": ["owner"]
```

---

### 3.14 `concurrency_patterns`（锁模式） — 可选

| 属性 | 值 |
|------|-----|
| 类型 | `{lock_acquire_patterns: string[], lock_release_patterns: string[]}` |
| 默认值 | `{"lock_acquire_patterns": [], "lock_release_patterns": []}` |
| 作用 | 锁获取/释放正则模式；每个条目是带一个捕获组（锁变量）的原始正则，或无捕获组（如 `rcu_read_lock`）。auto-profile 通过扫描源码常见锁函数名自动检测项目专属锁 API。内置参考 profile（linux_kernel.json、spdk.json）已填充项目适合的模式。 |

```json
"concurrency_patterns": {
  "lock_acquire_patterns": ["mutex_lock\\s*\\(\\s*&?(\\w+)",
                            "spin_lock\\s*\\(\\s*&?(\\w+)"],
  "lock_release_patterns": ["mutex_unlock\\s*\\(\\s*&?(\\w+)",
                            "spin_unlock\\s*\\(\\s*&?(\\w+)"]
}
```

---

### 3.15 `guard_functions`（运行时守卫语义） — 可选，修复 #6

| 属性 | 值 |
|------|-----|
| 类型 | object[] |
| 默认值 | `[]` |
| 作用 | 声明项目专属的运行时守卫函数，让 `path-feasible` / `path-guards` / `describe-node` 能根据项目专属守卫含义标注条件，而不是只依赖 `runtime_guards.py` 中硬编码的正则模式（后者覆盖常见 Linux 内核谓词如 `sb_is_blkdev_sb` / `PageUptodate`，但无法泛化到任意项目）。通过 `--profile` 提供 profile 时，声明的守卫是补充（而非替代）内置正则匹配。 |

**必填字段**：`function`、`kind`
**可选字段**：`effect`、`arg_index`、`description`

| 字段 | 类型 | 说明 |
|------|------|------|
| `function` | string | 守卫函数名（如 `"sb_is_blkdev_sb"`） |
| `kind` | enum | 之一：`"type_predicate"`、`"identity_predicate"`、`"lock_state"`、`"acquire"`、`"release"` |
| `effect` | string | `type_predicate`：谓词返回 true 时所断言的类型标签（如 `"blkdev"`）。`acquire`/`release`/`lock_state`：状态改变的锁对象。`identity_predicate`：未用（变量/值从调用点读取）。 |
| `arg_index` | int | 谓词测试的参数索引（`type_predicate`）或锁对象索引（`acquire`/`release`/`lock_state`），0-based。默认 0。 |
| `description` | string | CLI 输出中显示的可读说明。 |

```json
"guard_functions": [
  {"function": "sb_is_blkdev_sb", "kind": "type_predicate",
   "effect": "blkdev", "arg_index": 0,
   "description": "当 arg0 是块设备超级块时返回 true"},
  {"function": "bd_prepare_to_claim", "kind": "acquire",
   "effect": "bd_holder", "arg_index": 0,
   "description": "对 arg0（bdev）获取独占持有者锁"},
  {"function": "bd_abort_claim", "kind": "release",
   "effect": "bd_holder", "arg_index": 0,
   "description": "释放 arg0（bdev）的独占持有者锁"},
  {"function": "mutex_is_locked", "kind": "lock_state",
   "effect": "arg0_lock", "arg_index": 0,
   "description": "当 arg0 处的 mutex 已被持有时返回 true"}
]
```

**CLI 用法**：在 `path-feasible` 或 `path-guards` 后传 `--profile /path/to/profile.json`。运行时守卫分析输出会包含 `profile_bindings`，列出从 profile 声明的守卫推导出的绑定（如 `{"sb_type": "blkdev"}`）。

---

### 3.16 `allocation_sites`（对象来源追踪） — 可选，修复 #7

| 属性 | 值 |
|------|-----|
| 类型 | object[] |
| 默认值 | `[]` |
| 作用 | 声明项目专属的分配函数，让 `field-access` / `field-flow` / `describe-node` 能给 writer/reader 条目附加 `object_origin_type` 标注。否则，skill 只能识别 `bh = alloc_buffer_head()` 初始化了 `bh`，但无法区分 `alloc_buffer_head()` 返回的 `buffer_head` 与 `jh->bh` 取出的 `buffer_head`（可能属于不同 address_space）。声明后，`_trace_object_origin` 返回的来源会带上对象类型，形如 `"<call_expr>:<object_type>"`（如 `"alloc_buffer_head(...):buffer_head"`），让 agent 能跨"同类型不同实例"的变量做对象身份推理。 |

**必填字段**：`function`、`object_type`
**可选字段**：`arg_index`、`description`

| 字段 | 类型 | 说明 |
|------|------|-----|
| `function` | string | 分配函数名（如 `"alloc_buffer_head"`、`"kmalloc"`、`"kmem_cache_alloc"`） |
| `object_type` | string | 返回对象的类型标签（如 `"buffer_head"`、`"void_ptr"`） |
| `arg_index` | int | 若函数返回的是某个被分配子字段的包装/指针，此为初始化参数的下标。`-1`（默认）表示函数自身直接返回该对象（无初始化参数）。 |
| `description` | string | 人类可读的说明。 |

```json
"allocation_sites": [
  {"function": "alloc_buffer_head", "object_type": "buffer_head",
   "arg_index": -1,
   "description": "分配并返回一个新的 buffer_head"},
  {"function": "kmem_cache_alloc", "object_type": "page_cache_page",
   "arg_index": 0,
   "description": "arg0 是决定分配类型的 kmem_cache"}
]
```

**行为**：当 `build` 命令带 `--profile /path/to/profile.json` 时，builder 把 `function` → `object_type` 填入一个模块级映射。建图过程中，`_extract_state_access` 对每次字段写/读调用 `_trace_object_origin`；当追踪遇到已声明的分配函数调用时，返回的来源会带上 object_type 标注。标注会出现在：

- `field-access --struct buffer_head --field b_bdev` — writer/reader 条目带 `object_origin` 字段
- `field-flow --field b_bdev --value NULL` — writer 列表按对象来源分组
- `describe-node <function>` — `fields_written` / `fields_read` 条目带 `object_origin`

**何时声明**：当项目存在类型歧义的分配模式时声明——例如多个工厂函数返回相同名义类型但代表不同对象身份（内核 `buffer_head` 来自不同 address_space、`task_struct` 来自不同命名空间）。如果项目每种类型只有一个分配点，无需声明（类型本身即足够身份）。

---

### 3.17 `lock_semantics`（锁-持有者边） — 可选，修复 #10

| 属性 | 值 |
|------|-----|
| 类型 | object[] |
| 默认值 | `[]` |
| 作用 | 声明项目专属的锁原语（基于函数名，互补于基于正则的 `concurrency_patterns.lock_acquire_patterns`）。当 build 时带 `--profile`，builder 发出 HOLDER 边，把被锁对象链接到持有其锁的函数上下文。`detect-races` / `path-guards` / `describe-node` 能用 "writer 持有 `<lock>` 上的 `<object>`" 上下文标注竞争证据——把软性的 "这个 writer 受保护" 信号变成硬的、边可追溯的证明。 |

**必填字段**：`function`、`kind`
**可选字段**：`arg_index`、`locks_object_at`、`description`

| 字段 | 类型 | 说明 |
|------|------|-----|
| `function` | string | 锁原语名（如 `"mutex_lock"`、`"spin_lock"`、`"down_write"`、`"rcu_read_lock"`） |
| `kind` | enum | `"acquire"` 或 `"release"` |
| `arg_index` | int | 锁对象参数的 0-based 下标。默认 0。 |
| `locks_object_at` | int | 标识*受保护*对象的参数 0-based 下标。`-1`（默认）表示未知——记录锁但不发 HOLDER 边。对 `mutex_lock(&sb->s_lock)`，受保护对象是 `sb`（锁变量链的 head）。 |
| `description` | string | 人类可读的说明。 |

```json
"lock_semantics": [
  {"function": "mutex_lock", "kind": "acquire",
   "arg_index": 0, "locks_object_at": 0,
   "description": "arg0 是 &sb->s_lock；受保护对象是 head 'sb'"},
  {"function": "mutex_unlock", "kind": "release",
   "arg_index": 0, "locks_object_at": 0,
   "description": "释放 mutex_lock 获取的锁"},
  {"function": "down_write", "kind": "acquire",
   "arg_index": 0, "locks_object_at": 0,
   "description": "获取写侧 rwsem；受保护对象是链的 head"},
  {"function": "rcu_read_lock", "kind": "acquire",
   "arg_index": -1, "locks_object_at": -1,
   "description": "RCU 读侧临界区（无具体锁对象）"}
]
```

**行为**：当 `build` 带 `--profile /path/to/profile.json` 时，`detect_semantic_edges` 按函数名匹配每个声明的 acquire 原语，提取 `arg_index`（锁）和 `locks_object_at`（受保护对象）处的参数。锁参数的前缀 `&`/`*` 被剥离；对象参数归约到 head 变量（如从 `sb->s_lock` 取 `sb`）。HOLDER 边从受保护对象发往调用函数，`lock_function` 和 `lock_variable` 作为边属性记录。

**与 `concurrency_patterns` 互补**：`concurrency_patterns.lock_acquire_patterns` 用正则做灵活调用点匹配，驱动 `lock-coverage` / `who-locks`。`lock_semantics` 用精确函数名，驱动 HOLDER 边发出。两者都声明以获得完整锁推理。

**何时声明**：当项目的锁 API 结构化、受保护对象可从调用点识别时声明（如 `mutex_lock(&obj->lock)` 保护 `obj`）。对无受保护对象的全局锁（`rcu_read_lock()` 无参）跳过——把这些的 `locks_object_at` 设为 `-1`。

---

### 3.18 `io_classification`（I/O 关键字） — 可选

| 属性 | 值 |
|------|-----|
| 类型 | `{io_main_keywords: string[], io_side_keywords: string[]}` |
| 默认值 | `{"io_main_keywords": [], "io_side_keywords": []}` |
| 作用 | 将函数分类为 I/O-side（存储/网络/IO 后端）或 I/O-main（前端 handler）的关键字。`io-path` 命令使用。auto-profile 通过分析 I/O 相关源目录中的函数名自动检测项目专属术语。 |

---

### 3.19 `dispatch_tuning`（分发启发式） — 可选

调优 vtable 分发精度。所有子字段有合理默认值；仅当分发检测出现假阳/假阴时才覆盖。

| 子字段 | 类型 | 默认值 | 作用 |
|--------|------|--------|------|
| `max_vtable_dispatch_per_call` | int | `50` | 每个调用点的最大 vtable 分发目标数。内核 `file_operations` 中某些字段有 >50 个注册，需调高。 |
| `max_vtable_dispatch_per_field` | `{field: int}` | `{}` | 按字段覆盖，如 `{"write_iter": 100, "read_iter": 100}`。 |
| `inline_wrapper_patterns` | string[] | `[r"^(?:__)?(?:call\|invoke)_(\\w+)$"]` | 匹配内联包装函数名的正则。第一个捕获组是底层字段名。 |
| `macro_bridge_patterns` | `{pattern, impl}[]` | `[{"pattern": "^(\\w+)$", "impl": "__{1}"}]` | 宏名正则 → 实现名模板（用 `{1}` 表示捕获组）。 |
| `macro_bridge_require_same_domain` | bool | `true` | 是否要求宏桥接在同域内。内核中头文件和实现有时位于不同子目录，需设为 `false`。 |
| `fn_ptr_call_require_evidence` | bool | `false` | 为 `true` 时，只有同一函数内存在 `&func` 取址或结构体赋值证据，才把被调函数当作 `fn_ptr_call`。为 `false` 时用旧式名字后缀启发式。 |

---

### 3.20 `project_boundaries`（路径过滤） — 可选

标记代码为非-API / 测试 / 厂商 / 外部的源路径子串。这些是通用跨项目约定，一般无需覆盖。

| 子字段 | 类型 | 默认值 | 作用 |
|--------|------|--------|------|
| `non_api_paths` | string[] | `[]` | 标记非 API 代码的源路径子串（如 `tools/`、`scripts/`、`selftests/`）。`source_file` 含任意一项的函数不会被标为 `API_entry`。 |
| `test_path_patterns` | string[] | （通用） | 标记测试代码的源路径子串。 |
| `test_file_suffixes` | string[] | （通用） | `_is_test_source` 使用的测试文件后缀。 |
| `test_domain_segments` | string[] | （通用） | 标记测试/单元域的域段。 |
| `vendor_domain_prefixes` | string[] | `[]` | 标记厂商/外部代码的域前缀。默认空 — auto-profile 会检测项目专属厂商前缀。 |
| `external_dir_prefixes` | string[] | （通用） | 常见外部目录名（`vendor`、`third_party` 等）。 |

---

### 3.21 `phases`（阶段跟踪）

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `prescan_completed` | `false` | 预扫描阶段是否完成 |
| `test_scan_completed` | `false` | 测试扫描阶段是否完成 |
| `llm_header_analysis_completed` | `false` | LLM 头文件分析是否完成 |
| `llm_result_check_completed` | `false` | LLM 结果检查是否完成 |

手动编写时通常不需要设置，保持默认 `false` 即可。

---

## 四、编写流程

### 步骤 1：运行 auto-profile 获取基础

```bash
python3 scripts/code2database_scanner.py auto-profile \
  --source /path/to/project --outdir /path/to/project
```

这会生成 `.code2database_profile.json` 作为起点。

### 步骤 2：对照以下清单逐项审查和补充

| 检查项 | 操作 |
|--------|------|
| `skip_names.add` | 检查项目特有的宏和内联函数，添加不在默认列表中的名称 |
| `callback_detection.static_patterns` | **最重要**：搜索项目中所有注册回调的 API，为每个编写模式 |
| `macro_dispatch.registration_macros` | 搜索所有构造宏和注册宏，为每个编写模式 |
| `api_detection.public_prefixes` | 确认公共 API 前缀正确 |
| `api_detection.export_macros` | 检查项目是否有自定义导出宏 |
| `api_detection.struct_op_types` | 检查是否有 vtable 结构体 |
| `endpoint_classification.endpoint_rules` | 为程序入口和特殊端点添加规则 |
| `scan_hints.domain_rules` | 为函数名有明确分组模式的情况添加规则 |

### 步骤 3：验证 Profile

```bash
python3 scripts/code2database_scanner.py validate-profile \
  --profile /path/to/profile.json
```

验证会检查：
- 所有必填节是否存在
- 字段类型是否正确
- 正则表达式是否合法
- 无未知键（防止拼写错误）

### 步骤 4：使用 Profile 扫描

```bash
python3 scripts/code2database_scanner.py scan \
  --source /path/to/project \
  --profile /path/to/profile.json \
  --output code2db-out/.code2database_extraction.json
```

---

## 五、常见项目类型的 Profile 模板

### 5.1 C 标准库项目

```json
{
  "version": 1,
  "project": {"name": "myproject"},
  "api_detection": {"public_prefixes": ["my_"]},
  "callback_detection": {
    "static_patterns": [
      {
        "register_func": "pthread_create",
        "regex": "pthread_create\\s*\\(\\s*[^,]*,\\s*[^,]*,\\s*(\\w+)",
        "cb_arg_index": 2,
        "concurrency_type": "spawn_target"
      }
    ]
  }
}
```

### 5.2 Linux 内核模块

```json
{
  "version": 1,
  "project": {"name": "linux_kernel", "project_type": "linux_kernel"},
  "api_detection": {
    "public_prefixes": [],
    "export_macros": ["EXPORT_SYMBOL", "EXPORT_SYMBOL_GPL"],
    "struct_op_types": ["file_operations", "platform_driver", "net_device_ops"]
  },
  "callback_detection": {
    "static_patterns": [
      {"register_func": "kthread_run", "regex": "kthread_run\\s*\\(\\s*(\\w+)", "cb_arg_index": 0, "concurrency_type": "spawn_target"},
      {"register_func": "request_irq", "regex": "request_irq\\s*\\(\\s*[^,]*,\\s*(\\w+)", "cb_arg_index": 1, "concurrency_type": "callback"},
      {"register_func": "timer_setup", "regex": "timer_setup\\s*\\(\\s*[^,]*,\\s*(\\w+)", "cb_arg_index": 1, "concurrency_type": "poller"}
    ],
    "skip_call_prefixes": ["trace_"]
  },
  "struct_embeddings": {"container_of_macros": [{"macro_name": "container_of"}]},
  "threading_models": {"kernel_thread": ["kthread_create", "kthread_run"]},
  "scan_hints": {"vtable_module_keys": ["owner"]}
}
```

### 5.3 Meson 构建的 C 项目（如 DPDK/SPDK 风格）

```json
{
  "version": 1,
  "project": {"name": "myproject", "project_type": "myproject"},
  "detection": {
    "dir_markers": ["lib", "drivers"],
    "build_system": "meson",
    "macro_prefixes": ["MY_"]
  },
  "skip_names": {
    "add": ["MY_MIN", "MY_MAX", "my_panic", "MY_ASSERT"]
  },
  "api_detection": {
    "public_prefixes": ["my_"],
    "export_macros": ["MY_EXPORT"],
    "struct_op_types": []
  },
  "callback_detection": {
    "static_patterns": [
      {"register_func": "pthread_create", "regex": "pthread_create\\s*\\(\\s*[^,]*,\\s*[^,]*,\\s*(\\w+)", "cb_arg_index": 2, "concurrency_type": "spawn_target"}
    ]
  },
  "endpoint_classification": {
    "endpoint_rules": [{"pattern": "^my_init$", "endpoint_type": "program_entry"}]
  }
}
```

---

## 六、编写回调模式的实用技巧

### 6.1 如何找到项目中的回调注册 API

1. **搜索头文件中的函数声明**，找参数含函数指针的函数：
   ```bash
   grep -rn '(\*.*)(void)' include/ --include='*.h' | grep -i 'register\|callback\|launch\|create'
   ```

2. **搜索源文件中的使用模式**：
   ```bash
   grep -rn 'some_register_func(' lib/ --include='*.c' | head -20
   ```

3. **查看构造宏定义**：
   ```bash
   grep -rn '__attribute__((constructor))' --include='*.h'
   grep -rn '#define.*REGISTER\|INIT' --include='*.h'
   ```

### 6.2 正则编写要点

- **基本原则**：匹配函数调用语法，回调名用 `(\w+)` 捕获，其他参数用 `[^,]*` 或 `[^,)]+` 跳过
- **多参数示例**：
  - `foo(a, cb, c)` → `foo\s*\(\s*[^,]*,\s*(\w+)\s*,\s*[^,]*\)`
  - `bar(cb)` → `bar\s*\(\s*(\w+)\s*\)`
  - `baz(x, y, cb)` → `baz\s*\(\s*[^,]*,\s*[^,]*,\s*(\w+)`
- **注意事项**：
  - 不需要匹配右括号（扫描器会容错）
  - `cb_arg_index` 是 0 起始的参数位置
  - 如果回调参数位置不固定，需要为每种用法写单独的模式

### 6.3 并发类型选择指南

| 场景 | concurrency_type |
|------|-----------------|
| 创建新线程 | `spawn_target` |
| 注册中断处理函数 | `callback` |
| 注册定时器回调 | `poller` |
| 向线程池提交工作 | `callback_register` |
| 注册事件通知 | `callback` |

---

## 七、常见错误与排错

| 错误 | 原因 | 解决 |
|------|------|------|
| `ValueError: Unsupported profile version` | `version` 字段不是 `1` | 设为 `1` |
| `ValueError: Missing required section` | 缺少必填节 | 添加缺失的节（至少为空对象） |
| `ValueError: ... must be a dict with 'category' key` | `external_lib_prefixes` 值格式错误 | 改为 `{"category": "xxx", "visible": true/false}` |
| `ValueError: ... is not a valid regex` | 正则表达式语法错误 | 检查转义字符（JSON 中 `\\` 表示 `\`） |
| 回调函数没有连接到调用图 | `static_patterns` 缺少该回调模式 | 添加对应的注册函数模式 |
| 大量外部函数节点噪声 | `external_lib_prefixes` 中 `visible: true` 的前缀太多 | 改为 `visible: false` |
| 条件编译的函数被当作真实调用 | `macro_condition_prefixes` 缺少项目的宏前缀 | 添加对应的 `#ifdef` 前缀 |

---

## 八、Profile 存放位置

| 位置 | 说明 |
|------|------|
| `项目根目录/.code2database_profile.json` | auto-profile 默认输出位置，扫描器自动查找 |
| `任意路径` | 通过 `--profile` 参数指定 |
| `scripts/config/profiles/<type>.json` | 内置模板（供 auto-profile 使用，不要直接修改） |
