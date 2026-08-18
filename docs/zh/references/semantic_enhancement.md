# 语义增强参考

tree-sitter AST 扫描器无法 100% 覆盖所有调用模式。Claude 应补充以下扫描器容易遗漏的场景。

> **为什么这对代码数据库至关重要**：AST 提取给你*骨架*——直接调用、函数定义、结构体赋值。语义增强给你*神经系统*——回调目标、vtable 派发、宏展开、跨文件解析。没有增强，图谱回答"什么直接调什么"。有了增强，它回答"什么*可能*调什么——在任意派发路径、宏配置、回调注册下。"这是结构索引与可以问"执行实际怎么到达这里？"的数据库之间的差距。

## 通用场景

1. **函数指针/回调调用**：`callback(arg)` — 扫描器仅提取变量名，而非实际函数 → 追踪实际回调目标，向回调函数绘制边
2. **回调注册流**：`pthread_create(..., thread_fn, ...)` → 必须向 thread_fn 绘制边，将 thread_fn 标记为 callback_func

## C/C++ 特有

3. **宏展开调用**：`CALL_FN(x)` 展开为 `real_fn(x)` — 需要阅读宏定义
4. **虚函数/多态调用**：`obj->method()` — 需要分析类继承链
5. **信号/事件注册**：`signal(SIGINT, handler)` — handler 应标记为 callback_func

## Go 特有

6. **接口方法调用**：`io.Reader.Read()` — 实际调用哪个实现需要接口追踪
7. **Goroutine 闭包调用**：`go func() { fn() }()` — 闭包内的 fn() 调用

## Python 特有

8. **装饰器包装**：`@wraps` 等装饰器会改变实际调用目标
9. **动态调用**：`getattr(obj, method)()` — 运行时决定
10. **`__call__` 魔术方法**：可调用对象调用

## Java 特有

11. **反射调用**：`Method.invoke()` — 运行时决定
12. **接口回调**：`OnClickListener.onClick()` — 需要找到实现类

## Rust 特有

13. **trait 方法派发**：`dyn Trait` → 需要找到具体实现
14. **宏内部调用**：`println!` 等宏展开

## 条件表达式调用提取

扫描器自动提取条件表达式中的函数调用并标注 `call_condition`，无需手动增强。

### 支持的模式

| 模式 | call_condition | 示例 |
|------|---------------|------|
| if条件 | `if_cond(expr)` | `if (validate() && process())` → 两个调用均标注 `if_cond(validate() && process())` |
| if-consequence | `if(expr)` | true分支中的调用 |
| if-alternative | `!(expr)` | else/else-if分支中的调用 |
| switch谓词 | `switch(expr)` | `switch(get_key())` → 提取 `get_key()` |
| switch case体 | case文本 | `case N:` 下的调用 |
| while条件 | `while_cond(expr)` | `while(has_next())` → 提取 `has_next()` |
| do-while条件 | `while_cond(expr)` | 与while相同 |
| for条件 | `for_cond(expr)` | `for(;has_next();)` → 提取 `has_next()` |
| for循环体 | `for(expr)` | for循环体中的调用 |
| for init/update | 继承作用域 | `for(init(); ; update())` → 两者均提取 |
| 三元条件 | `ternary_cond(expr)` | `cond() ? ... : ...` |
| 三元true分支 | `ternary_true(expr)` | 三元表达式的真分支 |
| 三元false分支 | `!ternary(expr)` | 三元表达式的假分支 |
| 复合 &&/\|\| | 继承父作用域 | `a() && (b() \|\| c())` → 所有调用均提取 |

所有提取的边置信度为 `EXTRACTED`，来源为 `ast` — 这些是源码中可见的真实调用。

## 跨文件调用解析

构建器通过多策略解析管道解析跨文件 callee 名称，无需手动增强。

### 解析管道

| 策略 | 置信度 | 描述 |
|------|--------|------|
| suffix_index | O(1) 查找 | 将callee名与含点的节点ID匹配（如 `bar` → `lib.bdev.bar`） |
| same_file | 0.95 | callee定义在caller的同一源文件 |
| import_map | 0.85 | callee的头文件被caller `#include` |
| same_domain | 0.75 | callee在同一架构域 |
| suffix_match | 0.60 | callee名匹配节点ID后缀 |
| unique_name | 0.55 | 全局唯一函数名 |
| fuzzy | 0.30–0.40 | 部分名称匹配 |

构建后处理：扫描头文件桥接剩余未解析的外部端点，添加 `INFERRED` 边（confidence=0.75, source="import_resolution"）。

### 已知局限

当两个函数在不同域中同名且均非唯一时，解析可能选择错误目标或创建占位节点。这是无链接器信息的静态分析固有局限，可通过 profile 的 `struct_op_types` 和 `public_prefixes` 消歧。

## 宏展开集成

宏展开后的函数调用需要被追踪。增强规则：

- 当被调用者名称匹配宏图中的已知宏时，查找宏的展开内容
- 从展开的宏体中提取函数调用
- 添加从宏调用点到展开函数的边，附带：
  - `confidence: INFERRED`
  - `source: macro_expansion`
  - `evidence: [{"kind": "macro_expansion", "weight": 0.8, "note": "宏展开为实际函数"}]`

示例：
```c
// 宏定义：#define DISPATCH_HANDLER(h, arg) ((h)->callback(arg))
// 调用点：DISPATCH_HANDLER(&handler, data);
// 边：caller → handler_func, confidence=INFERRED, source=macro_expansion
```

## Vtable 派发集成

vtable/函数指针表的注册信息应与调用链追踪集成。增强规则：

- 从 profile 的 `struct_op_types` 和扫描的 `struct_types` 共同构建 `_vtable_field_names`
- 对于具有可识别字段模式（如 `ops->read`、`device->callbacks->process`）的每个 fn_ptr_call：
  - 查找 vtable 数据中所有已注册的实现
  - 添加从调用点到每个实现的 `callback_dispatch` 边
  - 置信度：`INFERRED`，来源：`vtable_resolution`
  - Evidence：`{"kind": "vtable_dispatch", "weight": 0.75, "struct_type": "operations", "field": "read"}`
- 对于回调注册：添加从回调注册调用点到所有已注册回调函数的边
- 对于未解析的 fn_ptr 调用：添加 `callback_dispatch` 边到该 struct op 类型的所有已知实现（保守过近似）

## 操作模式

1. 读取扫描器的提取 JSON，识别孤立节点和未解析的 callee
2. 对每个域的核心文件，阅读源码补充缺失的调用关系和标签
3. 对于 AST 无法识别的标签，由 LLM 判断；如果仍不确定，提示用户
4. 应用宏展开——将宏调用解析为实际函数目标
5. 应用 vtable 派发——将 fn_ptr 调用解析为已注册的实现
6. 将补充的边和标签追加到提取 JSON

**不要覆盖现有数据 — 仅追加新发现。**
