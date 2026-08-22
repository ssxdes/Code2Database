# 标签规则参考

> **标签是函数在代码库中可查询的角色。** 七个标签——`API_entry`、`thread_processor`、`callback_func`、`constructor`、`destructor`、`out_end`、`unknown_end`——分类函数*是什么类型的东西*，不只是它在哪里。查询"所有公共 API"按 `API_entry` 过滤；"所有线程入口"按 `thread_processor` 过滤；"所有回调"按 `callback_func` 过滤。没有标签，你不得不 grep 命名约定（跨项目和语言不可靠）。有了标签，图谱直接回答基于角色的问题——这就是数据库的优势。

## 函数标签

| 标签 | 含义 | C/C++ | Go | Python | Java | Rust |
|------|------|-------|-----|--------|------|------|
| `thread_processor` | 线程执行入口 | `pthread_create` 等 | `go func()` | `Thread(target=)` | `new Thread()` | `thread::spawn` |
| `callback_func` | 回调入口函数 | 回调参数 | 回调参数 | 回调参数 | `@Override`/回调 | 回调参数 |
| `constructor` | 构造/初始化入口 | `ClassName::ClassName` | — | `__init__` | 构造函数声明 | — |
| `destructor` | 析构/清理入口 | `~ClassName` | — | `__del__` | `finalize()/close()` | `Drop::drop` |
| `API_entry` | 公共 API 接口 | 非 static 函数 | 首字母大写的导出函数 | 非 private 方法/模块函数 | public 方法 | `pub fn` |
| `out_end` | 已知外部端点 | — | — | — | — | — |
| `unknown_end` | 未知外部端点 | — | — | — | — | — |

`API_entry` 标记对外暴露的公共函数，具有 `api_constraints` 属性描述输入约束（参数类型、格式限制等）。

`out_end`/`unknown_end` 标记调用链端点（项目中无源码的外部函数），由端点分类流水线自动标注。

**仅支持以上七种标签。新标签需要用户明确授权。**

## 三层标签识别

标签按优先级顺序识别：

1. **AST 启发式**（扫描器自动完成）：tree-sitter 在提取函数定义时自动检测 thread_processor/callback_func/constructor/destructor/API_entry
2. **LLM 语义增强**（Claude 补充）：对于 AST 无法识别的标签（如回调注册处的 handler、事件监听器回调），Claude 阅读源码进行补充
3. **用户确认**：如果 LLM 仍无法确定标签，必须提示用户 — 禁止猜测

## 回调流分析

当检测到回调注册模式时（例如 `pthread_create(&tid, NULL, thread_fn, arg)`、`signal(SIGINT, handler)`、`btn.setOnClickListener(listener)`）：

1. 分析注册点，识别在该处注册的所有可能的回调函数
2. 从注册调用点向实际回调函数绘制有向边（而不仅仅是到注册函数）
3. 将注册的回调函数标记为 `callback_func`

## 附加标签

| 标签 | 含义 | 来源 |
|------|------|------|
| `dead_code` | 死预处理分支中的函数 | `preproc_dead` |
| `hub` | 高介数中心性函数 | `betweenness` |

## 头文件声明与 API_entry 区分

在 C/C++ 项目中，头文件域可能包含大量函数声明而非实现。为避免 API 指标膨胀，头文件中**声明**但该域中**无定义**的函数在节点元数据中分类为 `header_declaration`（非标签）。它们：

- 不计入域指标的 API_entry
- 标记 `declaration_only: true` 属性
- 仍包含在图中（可能被其他文件调用）
- 通过 `import_resolution` 边链接到其实现

这防止了仅头文件域被误认为"接口膨胀的浅模块"。

## 端点子类别

虽然七种标签保持固定，但端点分类支持通过 `endpoint_type` 属性配置子类别。子类别由 profile 的 `endpoint_types` 属性定义。常见类型包括 event_handler、plugin_init、callback_entry、message_callback、timer_entry——但这些是 profile 配置项，而非硬编码。

`endpoint_type` 不是标签，而是 `out_end`/`unknown_end` 节点上的元数据属性。通过配置适当的端点类型模式，可以显著提高端点分类的覆盖率。
