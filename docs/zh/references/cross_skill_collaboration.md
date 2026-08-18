# 跨技能协作参考

Code2Database 提供调用链分析，但缺陷定位、新功能开发等场景需要与其他技能协调配合。

> **代码数据库在协作中的优势**：Code2Database 的图谱不只是节点和边——它带着其他技能所需的*输入*：条件、并发上下文、字段访问、证据链。当调试技能问"什么可能导致这个崩溃？"时，Code2Database 递给它 `reverse-trace` 输出（到崩溃的所有路径）+ `detect-races` 输出（并发访问）+ `field-access` 输出（共享状态）——一次给出完整画像，不是逐文件重新发现。其他技能查询数据库；它们不重读代码。

## 技能发现机制

**核心原则**：不要硬编码技能名称。定义每个场景所需的**能力关键词**，在运行时与已安装技能的描述进行匹配。

**发现已安装技能**：
```bash
ls ~/.claude/skills/*/SKILL.md 2>/dev/null | while read f; do
  name=$(basename $(dirname "$f"))
  desc=$(head -5 "$f" | grep "description:" | sed 's/.*description: *//')
  echo "$name|$desc"
done
```

**匹配规则**：每个场景定义能力关键词。在已安装技能描述中搜索这些关键词。匹配采用模糊方式（子串匹配即可）。

## 能力需求映射

| 场景 | 所需能力关键词 | Code2Database 提供的数据 |
|------|--------------|------------------------------|
| 缺陷定位 | debug, diagnose, bug, root cause | resolve-chain 执行路径 + 并发窗口 + 参数流 |
| 架构评审 | architecture, refactor, deep module, seam | 域依赖 + 模块深度比 + API 接口数据 |
| 新功能开发 | implement, plan, TDD, test-driven | 影响分析 + 依赖链 + 测试接缝位置 |
| 领域术语对齐 | domain, glossary, ubiquitous language | 自动检测的架构域 + 全局常量/枚举 |
| 变更验证 | verify, verification, complete | 同步/更新后的图数据一致性检查 |
| 合并冲突 | merge, conflict, resolve | code2db-out JSON 冲突 → sync 命令 |
| 代码评审 | review, code review | 变更影响的域 + 受影响的函数列表 |
| 可视化导出 | diagram, graph, visualize, export | 调用图数据 → HTML/SVG/PNG |
| **安全/漏洞分析** | **security, vulnerability, exploit, crash** | **reverse-trace 崩溃路径 + detect-races + concurrency-analyze** |
| **竞态条件分析** | **race, concurrency, data race** | **detect-races + field-access + concurrency-risks** |
| **间接调用追踪** | **function pointer, dispatch, indirect call** | **explore-flow + describe-node --context 查看派发目标 + callback_dispatch 边** |
| **I/O路径分析** | **io, input, output, data flow** | **io-path 追踪I/O路径** |

**当多个技能匹配时**：优先选择描述中关键词出现密度更高的技能。

**当无技能匹配时**，提示用户：
```
当前场景需要 <能力描述>，但未安装相关技能。
是否搜索并安装？(y/n)
搜索命令：npx skills@latest add <skill-name>
浏览：https://github.com/topics/claude-skill
```

## 10a - 缺陷定位协作

**触发**：用户说"定位缺陷"/"debug"/"为什么这个函数结果不对"/"diagnose"等。

**工作流**：

1. Code2Database：使用 resolve-chain + bindings 追踪问题函数的执行路径
   → 获取：哪些分支被执行、哪些函数参与、并发窗口、参数流

2. 判断是否需要外部技能（能力关键词：debug, diagnose, root cause）：
   - 缺陷涉及多函数交互（条件+调用链+并发）→ 调用图数据充足，直接分析
   - 缺陷是单函数内部逻辑错误 → 搜索匹配 "debug/root cause" 的已安装技能
   - 缺陷是非确定性/时序相关 → 搜索匹配 "diagnose/feedback loop" 的已安装技能

3. 传递给匹配技能的上下文：
   - 问题函数的 describe-node 输出（body_text + signature + params + condition_vars）
   - resolve-chain 结果（执行路径 + 哪些分支存活/剪枝）
   - 并发窗口（concurrent_groups）— 如果缺陷与线程竞争相关
   - 参数流（param_flow）— 如果缺陷与参数值传递相关

**关键原则**：**在找到根因之前不要提出修复方案。** 调用图的链路数据帮助缩小调查范围，但本身不是修复。

**关键原则**：**建立反馈循环。** code graph 的 resolve-chain 本身就是一个反馈循环 — 在给定条件下，哪些代码路径会执行。如果 resolve-chain 结果与实际行为不符，说明调用图数据存在缺口（需要语义增强）。

## 10b - 架构评审协作

**触发**：用户说"架构评审"/"模块太深/太浅"/"重构"/"架构问题"等。

**工作流**：

1. Code2Database：对每个 API_entry 函数运行 describe-node，获取：
   - 每个域的 API_entry 数量（= 接口规模）
   - 每个域的内部函数数量（= 实现深度）
   - 跨域调用关系（= 模块依赖）

2. 使用深模块术语描述（适用于任何架构分析技能）：
   - 架构域 = Seam（接口所在位置）
   - API_entry 函数 = Interface（暴露的接口面）
   - 域内非 API 函数 = Implementation（隐藏的实现）
   - API_entry 数量 / 域内函数总数 = Depth 比率（越小 = 越深 = 越好）

3. 搜索匹配 "architecture/refactor/deep module" 的已安装技能：
   - 找到 → 调用，传递调用图的域依赖 + 深度比率数据
   - 未找到 → 提示用户安装

4. 搜索匹配 "domain/glossary" 的已安装技能：
   - 找到 → 使用删除测试评估域深度
   - 未找到 → 使用内置深度评估方法

**内置深度评估方法**：
- **Depth 比率** = `API_entry 数量 / 域内函数总数`。越小 = 越深
- **删除测试**：想象移除一个域 — 复杂度是消失了（浅 = 仅做传递）还是分散到 N 个调用者（深 = 值得保留）？
- API_entry > 10 且内部函数 < 15 的域很可能是浅模块（接口膨胀）

## 10c - 新功能开发协作

**触发**：用户说"开发新功能"/"实现"/"需要添加 X"等。

**工作流**：

1. Code2Database：对变更目标运行影响分析
   → impact --direction reverse：哪些函数将调用新功能
   → impact --direction forward：新功能依赖哪些已有函数
   → 识别受影响的域和 API_entry

2. 确定测试接缝（通用 TDD 原则）：
   - 在哪个 API_entry 层级编写集成测试
   - 新功能在哪里接入现有调用链

3. 搜索匹配 "plan/implement" 的已安装技能：
   - 找到 → 将调用图影响分析结果作为输入传递
   - 未找到 → 在调用图上下文中直接制定计划

4. 搜索匹配 "TDD/test-driven" 的已安装技能：
   - 找到 → 在 API_entry 接缝处编写测试（调用图数据显示需要测试哪些入口）
   - 未找到 → 提醒用户在 API_entry 层级手动编写测试

## 10d - 领域术语对齐协作

**触发**：用户提到架构域名称与项目文档不一致，或调用图检测的域与 CONTEXT.md 术语冲突

**工作流**：

1. Code2Database：列出所有检测到的域和全局常量

2. 检查项目根目录是否有 CONTEXT.md：
   - 有：比较域名称与 CONTEXT.md 术语
   - 术语冲突 → 搜索匹配 "domain/glossary/ubiquitous language" 的已安装技能
   - 域名称不在 CONTEXT.md 中 → 建议补充

3. 全局文件中的枚举/常量名称 → 建议添加到术语表

## 10e - 变更验证协作

**触发**：sync 或 update 完成后

**通用原则**：**声明完成前必须有验证证据。** sync/update 后必须重新验证图数据。

1. sync/update 完成后，立即运行验证：
   ```bash
   python3 code2database_builder.py load --graph code2db-out/ --summary
   ```

2. 验证数据：
   - 节点数量合理（除非删除了文件，否则不应大幅下降）
   - 域列表完整
   - API_entry 标签数量正确

3. 对关键函数运行 describe-node 验证数据完整性：
   - body_text 不为空
   - signature 正确
   - params/local_vars 存在

4. 运行一致性校验（已内置在构建步骤中）：
   - 若检测到不一致，重新运行 `build`
   - 检查未计入的节点/边
   - 验证各文件间端点一致性
   - 标记域覆盖缺口

5. 搜索匹配 "verify/verification/complete" 的已安装技能：
   - 找到 → 遵循该技能的验证流程
   - 未找到 → 使用上述内置验证步骤

6. 开发分支完成前：如果扫描的源代码被修改过，运行 update 刷新图，然后 sync 合并到远程

## 10f - 合并冲突处理

**触发**：git merge 在 code2db-out/ 目录中产生 JSON 冲突

**处理方式**（内置，无需外部技能）：

不要手动解决调用图 JSON 冲突！使用 sync 命令代替：
1. 保留本地版本：`git checkout --ours code2db-out/`
2. 将远程版本复制到临时位置
3. 运行 sync 合并：
   ```bash
   python3 code2database_builder.py sync --graph code2db-out/ --git-path <remote-code2db-out-path>
   ```
4. sync 自动合并两份数据集（本地优先），重新生成所有域文件和索引

如需更通用的合并冲突处理，搜索匹配 "merge/conflict/resolve" 的已安装技能。

## 10g - 可视化协作

**触发**：用户希望将调用图导出为其他格式（非 HTML）

1. Code2Database：首先使用 export-html 生成基础可视化
2. 搜索匹配 "diagram/graph/visualize/export" 的已安装技能：
   - 找到 → 调用技能，传递调用图数据
   - 未找到 → 提示用户安装相关可视化技能

## 10h - 安全/漏洞分析协作

**触发**：用户提到"崩溃报告"/"空指针解引用"/"安全漏洞"/"漏洞利用分析"等。

**工作流**：

1. Code2Database：从崩溃点反向追踪
   ```bash
   python3 code2database_builder.py reverse-trace --graph code2db-out/ --crash-point CRASH_FUNC --max-depth 15 --max-paths 30
   ```
   → 获取所有从入口点到崩溃点的路径

2. 分析竞态条件（若缺陷涉及并发）：
   ```bash
   python3 code2database_builder.py detect-races --graph code2db-out/ --func CRASH_FUNC
   ```
   → 获取跨线程数据竞争分析、共享资源访问、锁范围

3. 检查间接派发目标（若崩溃涉及函数指针调用）：
   ```bash
   python3 code2database_builder.py describe-node --graph code2db-out/ --node CRASH_FUNC --detail full --context
   ```
   → 通过 callback_dispatch 边获取崩溃路径上间接调用的所有可能目标

4. 判断是否需要外部技能（能力关键词：security, vulnerability, exploit）：
   - 缺陷是清晰的代码路径问题 → 调用图追踪充足，直接分析
   - 缺陷涉及复杂内存安全 → 搜索匹配 "security/vulnerability" 的已安装技能
   - 缺陷涉及特定利用技术 → 搜索匹配 "exploit/pwn" 的已安装技能

5. 传递给匹配技能的上下文：
   - 完整的 reverse-trace 输出（从入口到崩溃的所有路径）
   - detect-races 输出（并发访问分析）
   - 崩溃路径上的间接派发目标（来自 describe-node --context）
   - 共享数据结构的 field-access 输出

**关键原则**：**安全分析中，fn_ptr_call 和 callback_dispatch 边至关重要。** 若缺失，重新运行 `build` 触发函数指针解析。

**关键原则**：**条件分支调用自动提取。** 扫描器自动提取 if/while/for/switch/三元条件中的所有调用并标注 `call_condition`，无需手动增强。

## 10i - 竞态条件分析协作

**触发**：用户提到"数据竞争"/"竞态条件"/"并发缺陷"/"TOCTOU"等。

**工作流**：

1. Code2Database：运行完整竞态分析流水线：
   ```bash
   # 检测数据竞争
   python3 code2database_builder.py detect-races --graph code2db-out/

   # 特定函数的并发安全分析
   python3 code2database_builder.py concurrency-analyze --graph code2db-out/ --func SUSPECT_FUNC

   # 字段级访问追踪
   python3 code2database_builder.py field-access --graph code2db-out/ --field SHARED_VAR
   ```

2. 输出包含：
   - 多线程访问的共享数据结构
   - 每次访问的锁保护状态
   - 竞态窗口（无保护的并发访问）
   - 并发执行路径对

3. 判断是否需要外部技能：
   - 竞态分析已全面 → 调用图数据充足
   - 需要形式化验证 → 搜索匹配 "formal verification/model checking" 的已安装技能
   - 需要运行时验证 → 搜索匹配 "thread sanitizer/concurrency sanitizer" 的已安装技能

4. 传递的上下文：
   - detect-races 输出（跨线程数据竞争分析）
   - 争议字段的 field-access 报告
   - spawn 点的 concurrency-risks 输出

## 10j - 间接调用追踪协作

**触发**：用户问"哪些函数实现了这个回调？"/"这个函数指针调用了什么？"/"vtable派发"等。

**工作流**：

1. Code2Database：解析间接调用（fn-ptr解析在构建步骤中运行，可进一步探索）：
   ```bash
   # 查看特定函数的派发目标
   python3 code2database_builder.py describe-node --graph code2db-out/ --node SUSPECT_FUNC --detail full --context
   ```

2. 输出：
   - 每个 fn_ptr_call → 可能的目标函数列表（callback_dispatch 边）
   - 派发注册上的条件元数据

3. 判断是否需要外部技能：
   - 需要更精确的解析 → 搜索匹配 "LSP/language server/type inference" 的已安装技能
   - 需要运行时验证 → 搜索匹配 "dynamic analysis/profiling" 的已安装技能

4. **关键原则**：`callback_dispatch` 边代表保守过近似（所有可能目标）。若需要精确性，使用 `--verbose` 查看解析推理，并用运行时数据验证。

## 已知技能匹配参考

当前已安装技能与场景的对应关系，**仅供参考** — 新技能会自动进入发现机制：

| 场景 | 已知匹配技能（如已安装） | 安装来源 |
|------|------------------------|---------|
| 缺陷定位 | systematic-debugging, diagnosing-bugs | superpowers, skills |
| 架构评审 | improve-codebase-architecture, codebase-design | skills |
| 新功能开发 | writing-plans, implement, tdd | superpowers, skills |
| 领域术语对齐 | domain-modeling | skills |
| 变更验证 | verification-before-completion, finishing-a-development-branch | superpowers |
| 合并冲突 | resolving-merge-conflicts | skills |
| 可视化导出 | fireworks-tech-graph, architecture-diagram | skills |
| 代码评审 | requesting-code-review, review | superpowers, skills |
| 安全/漏洞分析 | concurrency-analysis | skills |
| 竞态条件分析 | concurrency-analysis | skills |
| 间接调用追踪 | lsp-analysis, type-inference | skills |
