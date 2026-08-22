# 使用示例

> **下面每个示例都是对持久代码数据库的一次调用查询。** 没有 grep、没有 glob、没有跨 N 个文件 Read。图谱构建一次；这些命令查询它。这就是从*阅读*代码到*查询*代码的转变。

## "device_register 的完整调用链是什么？"

1. 搜索 `device_register` → 找到节点 `lib_device_device_register`
2. 遍历后继节点（depth=3）→ 获取所有 callee 和调用条件
3. 遍历前驱节点 → 获取所有 caller
4. 阅读关键源文件验证调用链
5. 在答案中标注每条边的 call_order 和 call_condition

## "Go 项目中 http.Handler 的调用链是什么？"

1. 搜索 `Handler` → 找到 `pkg_server_handler_servehttp`
2. 遍历邻居节点 → 获取调用链
3. 标注标记为 goroutine 的函数

## "这个 Python 类中从 __init__ 到 start 的流程是什么？"

1. 搜索 `__init__` → 找到标记为 constructor 的节点
2. 路径查找 `__init__` → `start`
3. 标注路径上的条件和顺序

## "如果修改 nvme_init，影响范围有多大？"

1. 找到 `module_device_nvme_nvme_init`
2. 运行 `impact --direction reverse` → 获取所有 caller
3. 运行 `impact --direction forward` → 获取所有 callee（依赖分析）
4. 列出受影响的域和函数

## "这个项目的公共 API 有哪些？"

1. 搜索 `API_entry` 标签 → 列出所有外部接口
2. 对每个 API_entry，显示其 api_constraints

## "device_start 在 mode=1 时有 bug，帮我定位"（跨技能协作）

1. resolve-chain --node root_device_start --bindings "mode=1" → 获取 mode=1 下的实际执行路径
2. 检查并发窗口：concurrent_groups 中是否有函数与 thread_fn 并发执行
3. 检查参数流：mode 如何流入条件分支和 callee
4. 如果缺陷缩小到单函数内部逻辑 → 搜索匹配 "debug/root cause" 的已安装技能，调用匹配的技能
5. 如果缺陷涉及时序/非确定性行为 → 搜索匹配 "diagnose/feedback loop" 的已安装技能

## "lib.device 模块是否太浅？"（跨技能协作）

1. 统计 lib_device 域：API_entry 数量 / 域内函数总数 = 深度比率
2. 如果深度比率 > 0.5 → 浅模块（接口膨胀），搜索匹配 "architecture/refactor" 的已安装技能
3. 使用删除测试：移除 lib_device 后，复杂度是消失还是分散？

## "我想给 device 添加新功能，先帮我分析影响"（跨技能协作）

1. impact --node root_device_start --direction reverse → 谁将调用新功能
2. impact --node root_device_start --direction forward → 新功能依赖哪些已有函数
3. 确定测试接缝：在哪个 API_entry 层级编写测试
4. 搜索匹配 "plan/implement" 的已安装技能，传递调用图影响分析数据

## "mode=0 和 mode=1 的执行路径有什么区别？"（新命令）

1. diff-chains --node device_start --bindings-a "mode=0" --bindings-b "mode=1"
2. 输出：表格展示仅在 mode=0 中的路径、仅在 mode=1 中的路径、以及共同路径

## "列出项目中所有并发风险点"（新命令）

1. concurrency-risks --graph code2db-out/
2. 输出：所有 spawn 点、并发窗口，按风险等级排序

## "追踪 'buffer' 资源的生命周期"（新命令）

1. data-lifecycle --graph code2db-out/ --resource "buffer"
2. 输出：分配 → 使用 → 释放路径，包含函数名和条件
