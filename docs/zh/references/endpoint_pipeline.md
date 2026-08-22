# 端点分类流水线

调用链端点（项目目录中无源码的外部 API）通过此流水线进行分类。

> **为什么端点获得一等公民待遇**：在代码数据库里，每条调用链都必须*结束*于某处——要么是你拥有的函数，要么是你不拥有的（libc 调用、内核 syscall、厂商库）。流水线用 `endpoint_type`（program_entry、callback_entry、external_posix、function_pointer……）标注每个端点，让"展示所有进入内核 syscall 的位置"或"从 main 可达哪些函数？"这样的查询返回有用答案，而不是一个扁平的叶子节点列表。分类把"死胡同节点"变成可查询的入口/出口点。

## 第 1 步：自动标记

所有没有源码定义的 invoked 节点统一标记为 `out_end`。

## 第 2 步：导出供分析

构建器将所有端点导出到 `code2db-out/.code2database_endpoints.json`。

## 第 3 步：端点类型分类

通过 profile 的 `endpoint_types` 支持可配置的端点类型。每个端点类型可通过名称模式（`name_patterns`）、注册宏（`registration_macros`）和注册函数（`registration_functions`）来匹配。

内置端点类型包括：

| endpoint_type | 含义 | 示例 |
|---------------|------|------|
| `event_handler` | 事件处理器入口 | 匹配 `on_*`/`handle_*` 命名约定 |
| `plugin_init` | 插件/模块初始化 | 通过插件初始化宏注册的函数 |
| `callback_entry` | 通过vtable/struct_ops注册的回调 | 赋值到操作结构体字段的函数 |
| `message_callback` | 消息/回调入口 | 通过回调注册API注册的函数 |
| `timer_entry` | 定时器回调入口 | 通过定时器设置API注册的函数 |
| `rpc_handler` | RPC/服务处理器模式 | `svc_process` |
| `program_entry` | 程序入口点（main 等） | `main` |
| `external_posix` | 标准 POSIX API 调用 | `pthread_create`、`open`、`read` |
| `function_pointer` | 通过函数指针派发调用 | `(*callback)(arg)` |

用户可通过 profile 的 `endpoint_types` 添加项目特定的端点类型。例如，对于使用事件处理器、插件初始化、消息回调等模式的项目，可配置对应的端点类型：

```json
{
  "endpoint_types": {
    "program_entry": {
      "name_patterns": ["^main$"]
    },
    "callback_entry": {
      "registration_functions": ["register_callback", "set_handler"]
    },
    "external_posix": {
      "name_patterns": ["^pthread_", "^open$", "^read$", "^write$", "^close$"]
    }
  }
}
```

## 第 4 步：LLM 分类

Claude 读取端点列表，对每个端点判断：

- 函数含义明确 → 填充 `external_desc`，保留 `out_end`
- 函数含义不明确（例如未知的外部动态库函数）→ 重新标记为 `unknown_end`
- 函数匹配 endpoint_type 模式 → 设置 `endpoint_type` 属性

## 第 5 步：写回图数据

运行 classify-endpoints 命令更新图数据：

```bash
python3 "$SKILL_DIR/scripts/code2database_builder.py" classify-endpoints \
  --graph code2db-out/
```

## 第 6 步：报告

统计 `unknown_end` 条目并向用户报告，注明哪些需要手动确认。

## 端点定义说明

所有阶段使用统一的端点定义：

- **广义定义**（`mark_endpoints` 使用）：项目中无 invoked 的所有叶节点 = 端点
- **分类定义**（`endpoints.json` 使用）：无源码的外部函数 = 外部端点
- 摘要包含图例，说明两个计数及其关系

摘要中的端点分类示例：
```
Endpoints:
  Total (no invoked in project): 88,249
  External (no source code):        422
  Classified:                       399 (94.5%)
  Unclassified (unknown_end):        23 (5.5%)

  By type:
    external_posix:   150
    rpc_handler:       85
    program_entry:     72
    function_pointer:  45
```
