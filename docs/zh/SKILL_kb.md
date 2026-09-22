---
name: Code2Database-kb
description: "独立的项目知识库：curated knowledge + 积累的老兵经验，专用 SQLite 存储 + FTS5 检索、版本感知召回、跨域查询。有无代码图谱均可运行。捕获、修正、检索、毕业项目知识与经验时使用 /Code2Database-kb。"
trigger: /Code2Database-kb
---

# /Code2Database-kb

**AI 开工前加载、开工中生长的项目知识库。** 两个存储、一个索引、不依赖图谱。

## 部署形态

kb 子 skill 可独立部署，也可与代码图谱同机部署——共享 CLI，不共享数据文件：

| 存储 | 文件 | 归属 |
|---|---|---|
| knowledge + memory | `memory/memory.db`、`knowledge/knowledge.db`、`kb_index.db` | 本子 skill |
| 代码图谱 | `code2database.db`、`code2database_master.json` | 图谱 skill |

`kb init` 初始化的存储不含任何图谱产物。所有 kb 命令在从未扫描/构建过的目录上都能工作。

## 两个存储——定义、逻辑、物理位置三分

| 维度 | knowledge | memory |
|---|---|---|
| 定义 | 关于本项目的经过整理的稳定事实：规则、模式、抽象、约定、陷阱、查询路线 | 工作中积累的老兵经验：提问 → 答案对 |
| 逻辑模型 | 类型化行（`hard_rule` / `mode` / `abstraction` / `convention` / `pitfall` / `query_path` / `description` / `must_know`），无衰减 | 聚簇的 Q&A 条目：权重、访问计数、合并谱系、衰减为 `experience` |
| 物理文件 | `knowledge/knowledge.db` | `memory/memory.db` |
| 提示视图 | `knowledge/brief.json`（派生，受字数预算约束） | `session-init` 中的 memory digest |

`kb_index.db` 是覆盖两个存储的派生 FTS5/BM25 索引——一次查询同时排序（`source_kind` 区分来源）。`brief.json` 在每次写入后从 knowledge 行重新生成；knowledge 数据库是事实源。

## 会话启动

```bash
python3 scripts/code2database_builder.py kb-init --name my-project   # 一次
python3 scripts/code2database_builder.py session-init               # 每次会话
```

`session-init` 不要求图谱：渲染简报、记忆摘要、known unknowns（反复出现却无人回答的查询——捕获提示）。

## 提炼规则（什么进入 memory）

仅当一条记忆能帮助**空白上下文**下的同类疑问分析时才保存——假设读者只有这个 kb，别的什么都没有。

**过滤掉**（绝不保存）：
- 当前对话的计划、中间状态、未定稿的决策——它们不属于代码项目；放进带 TTL 的 scratch
- 项目本身已有、一次查询/图谱调用即可回答的内容
- 会话私有的工具笔记、提示词草稿、待办清单

**捕获时机**：
- 解决了非平凡的疑问，且排查路径本身就是答案
- 踩了耗费真实调试时间的坑
- 发现了简报未覆盖的强制规则/约束
- 之前存的答案被证伪（`--correct`）
- 回答了 `session-init` 里的 known-unknown

锚定到代码：记忆关于具体函数/类型时传 `--symbol fn_name`。标注学习时的版本：`--version-scope <分支或标签>`（默认 `default`）。

```bash
python3 scripts/code2database_builder.py save-memory \
  --question "bdev_start 线程安全吗?" --answer "不安全——poller 独占，..." \
  --category bdev/nvme --author you --symbol bdev_start --version-scope main
```

## 修正规则（记忆保持锋利）

- **答案错了** → `save-memory --correct` 原地重塑最相似条目（保留版本历史，不产生重复变体）
- **相似记忆** → 保存时自动合并（阈值 0.7）；`manage-memory --action merge/split/move` 重组；每次构建后自动 `compact`
- **更好的表述** → 同一提问 `--correct` 加改进后的答案
- **相对代码过期** → `memory validate` 将 `node_ids` 已离开图谱的条目降级（无图谱时优雅跳过）

## 毕业规则（memory → knowledge）

当一条记忆反复证明有用，它升格为事实：`brief suggest` 挖掘毕业候选（高权重或高合并数）并给出可直接执行的 `brief update` 命令——毕业永远是经过审视的一步，绝不自动进行。knowledge 保持精简：简报超过 3000 字符告警、超过 6000 报错。

## 扩展规则（知识边界怎么生长）

- **相邻域**：当一个新子系统/新语言反复出现在提问里但没有任何记忆覆盖，先补 memory（低成本试错），连续命中后再考虑是否需要新的 `--category` 层级——层级为记忆而生，不预先铺设
- **跨域复用**：另一个域里已验证的解释优先用 `kb-domain add` 挂过来（`kb-query --cross` 标注来源域），而不是复制一份；副本会漂移
- **knowledge 只纵向加深**：扩展 = 给已有 hard_rule / abstraction 补更准的表述（`revise`），而不是横向加新条目；横向增长的内容属于 memory
- **收缩也是扩展**：两条 hard_rule 说的其实是同一件事时合并为一条（`brief update` 重写）；knowledge 的价值密度比条数重要

## 分域分层

- **存储内**：`--category path/to/topic` 构建层级（`bdev/nvme/pcie`），自动创建；按记忆涉及的符号/子系统选择路径
- **跨存储**：每个 kb `.db` 就是一个域。`kb-domain-add <store-dir>` 注册另一个知识库；`kb-query --cross` 搜索所有已关注域并为每条命中标注 `source_domain`

## 版本感知召回

每条记忆和知识条目都携带学习时的代码版本。查询时声明当前工作的版本：

```bash
python3 scripts/code2database_builder.py kb-query \
  --query "queue doorbell" --version-scope release/2.0 --cross
```

`release/2.0` 上学习的条目排最前；其他版本的条目随后，标注 `is_current_scope: false`——绝不过滤，始终标注。

## 查询优先级链

```
1. memory（search-memory / kb-query）   — 之前问过吗？
2. knowledge（kb-query --kinds / brief） — 有整理过的规则吗？
3. graph（仅当部署了图谱 skill）
4. source（最后手段）
```

`kb query` 是跨两存储的一站式入口；`session-init` 是一站式加载。

## 命令面（Tier-1）

`kb init`、`session-init`、`save`、`recall`、`kb query`、`knowledge-brief`、`kb rebuild-index`、`kb known-unknowns`——另有 `brief-*` 整理、`memory manage` 治理、`kb cluster`、`kb-domain-*` 注册、`search semantic`。完整 CLI 保持可访问。
