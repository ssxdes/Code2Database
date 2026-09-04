# Memory 与 Knowledge（项目简报）参考

Code2Database 用两个形态相反的长期存储：

| | Memory | Knowledge（项目简报） |
|---|---|---|
| **角色** | 共享积累的 Q&A 大脑 — 多人、多深度问题 | 本项目的精简固定描述：架构、功能、设计、使用 |
| **存储** | `memory/memory.db`（SQLite WAL + FTS5） | `knowledge/brief.json` |
| **体积** | 无上限增长 | 预算：>3000 字符告警、>6000 报错（`brief-validate`） |
| **加载时机** | 按需（`search-memory`、`kb-query`） | **每次会话启动**（`knowledge-brief`） |
| **更新节奏** | 持续（save/merge/split） | 小范围，仅在架构真正变化时 |

## session-init — 一站式入口

```bash
python3 scripts/code2database_builder.py session-init --graph code2db-out/ [--top 10] [--json]
```

一次加载四层并输出为 prompt 就绪文本：渲染后的简报、记忆摘要（按权重取 top 活跃 Q&A，含分类/作者/阅读数）、图统计 + 简报漂移告警、known unknowns（反复未命中的查询 — 为它们补答案）。空存储退化为引导提示而非报错。这是每次会话的 Step 0（AI）也是最快的项目简报（人类）；Web UI 以交互方式渲染同样的数据（Brief / Memory 面板）。MCP：同样的上下文以 `code2database_session_init` 工具暴露——代理应在会话开始时最先调用它，先于 search/describe/trace。

## Memory（memory.db）

### 布局

```
graph_dir/
├── memory/memory.db        ← categories + memories + FTS5 索引
└── .scratch/               ← TTL 会话状态（文件制）
```

状态生命周期：`active` → `experience`（权重衰减 <0.1 或 node_ids 失效）、`active` → `split`（拆解）、`active` → `merged`（被规范条目吸收）。

### 分层分类

问题按分类路径索引（`bdev/nvme/pcie`）。保存时缺失层级自动创建。分类按主题深度组织问题，检索可按子树收窄：

```bash
# 带分类 + 作者保存
python3 scripts/code2database_builder.py save-memory --graph code2db-out/ \
  --question "nvme 如何提交 IO？" --answer "submission queue doorbell" \
  --category bdev/nvme/pcie --author alice

# 浏览分类树
python3 scripts/code2database_builder.py manage-memory --graph code2db-out/ --action categories
```

### 检索

`search-memory` 以 FTS5 BM25 × 权重运行并支持过滤；结果按相似簇聚组，单个热门 Q&A 不会刷屏：

```bash
python3 scripts/code2database_builder.py search-memory --graph code2db-out/ \
  --query "nvme submit" --category bdev --author alice --top 5
#   --category  前缀过滤（包含所有子分类）
#   --tags      所列 tags 必须全部命中
#   --include-experience  同时搜索归档条目
```

FTS5 无 token 交叠时，token 集相似度回退仍能找到近似项。**中文感知**：unicode61 分词器把整段连续汉字当作单个 token，因此查询包含中文时相似度通道（单字 + 二元组）始终与 FTS5 并行运行并按最高分合并——中文语义不会被静默丢弃。

### 符号锚定（记忆 ↔ 代码）

关于某个具体函数/类型的记忆可以携带它所属的图谱符号名（`--symbol`，可重复），把经验锚定到代码：

- `save-memory ... --symbol nvme_submit_cmd` 把符号名存入 `symbols` 列；合并时吸收进簇根，`--correct` 时传 `--symbol` 可重新锚定。
- `search-memory --symbol nvme_submit_cmd` 按符号精确过滤（大小写不敏感）；MCP `code2database_memory_search` 接受 `symbol` 参数。
- Web UI 在该符号的节点页展示锚定的问答（"Veteran Memories"）——新人读代码时就地看到这个函数的坑。
- `MemoryStore.entries_for_symbol(name)` 是编程接口（按簇去重、按权重排序）。

```bash
python3 scripts/code2database_builder.py save-memory --graph code2db-out/ \
  --question "nvme submit 有什么坑?" --answer "doorbell 写错寄存器会 hang" \
  --category bdev/nvme/pcie --author alice --symbol nvme_submit_cmd
```

### 纠错（correct-first 保存）

当已存的答案被证明是**错的**，直接重存修正文本只会产生重复变体（且合并路径保留权重更高——也就是仍然错误——的答案）。`--correct` 找到最相似的活跃条目并原地重塑，旧答案保留在版本历史中并记录纠正者：

```bash
python3 scripts/code2database_builder.py save-memory --graph code2db-out/ \
  --question "nvme 如何提交 IO？" --answer "修正后的答案" \
  --correct --author alice
# → Corrected memory #12 (was: 'nvme 如何提交 IO？', similarity 0.95)
#   — no new variant created
```

没有相似条目时退化为普通保存（`saved as new`）。

### 治理

大型共享库会积累过泛与重复条目。`manage-memory` 提供治理操作：

```bash
# 把过泛条目拆解为聚焦子条目
python3 scripts/code2database_builder.py manage-memory --graph code2db-out/ \
  --action split --id 12 --parts '[{"question": "pcie 路径", "answer": "..."},
                                   {"question": "tcp 路径", "answer": "..."}]'

# 重复条目合并为规范条目（变体自动重指向）
python3 scripts/code2database_builder.py manage-memory --graph code2db-out/ \
  --action merge --ids 12,15 --canonical 12

# 重新归类
python3 scripts/code2database_builder.py manage-memory --graph code2db-out/ \
  --action move --id 12 --category bdev/nvme/rdma

# 查看血缘树（谁从谁拆出 / 合并进谁 / 谁是谁的变体）
python3 scripts/code2database_builder.py manage-memory --graph code2db-out/ \
  --action lineage

# 贡献者索引（多人共享库）
python3 scripts/code2database_builder.py manage-memory --graph code2db-out/ \
  --action authors
```

同一张血缘图也在 Web UI Memory 面板中交互渲染（Lineage 按钮）。

### 只读共享

Web UI 以只读方式服务 memory（`MemoryStore(read_only=True)`）：共享挂载上的查看者获得完整的检索/摘要/血缘/作者访问——不创建目录、不写 schema、不累加阅读计数——而写操作（save/split/merge/…）仍由库 owner 执行。按 author 过滤的视图（`/api/memory/authors` + 下拉框）回答"alice 在这个项目上沉淀了什么"。


### 权重模型（未变）

`weight = recency(e^{-λ·days}) × importance(合并数 + 答案长度) × access × (1 + boost)`，上限 10.0。`decay` 重算权重并把 <0.1 归档为 experience；`promote` 增加持久化 boost；build 后自动 `consolidate`。

## Knowledge（knowledge/brief.json）

### 会话启动协议

```bash
python3 scripts/code2database_builder.py knowledge-brief --graph code2db-out/
```

在开始工作前把简报渲染进 prompt。若不存在，用 `brief-extract` 自举后再精炼。

### 节

| 节 | 内容 | 示例 |
|---|---|---|
| `project` / `one_liner` / `description` | 名称 + 定位 + 2-4 句架构描述 | "SPDK — 用户态存储 SDK；每核 reactor 事件循环" |
| `hard_rules` | 强制宏/分支/配置：`{rule, type, detail, evidence}` | "强制开启 SPDK_CONFIG_PCI 宏 (macro)" |
| `modes` | 使用场景区分：`{name, when, differences}` | pcie / tcp / rdma 传输 |
| `key_abstractions` | `{name, role}` | bdev / io_channel / reactor |
| `conventions` / `pitfalls` / `query_paths` | 编码约定、已知坑、建议 C2D 查询路径 | |
| `must_know` | 自由格式的最后叮嘱 | |
| `graph_stats` | 自动刷新的节点/边/域计数 | |

### 精炼命令

```bash
python3 scripts/code2database_builder.py brief-extract --graph code2db-out/      # 自举/刷新模板
python3 scripts/code2database_builder.py brief-update --graph code2db-out/ \
  --set one_liner --value "..."                                                 # 标量节
python3 scripts/code2database_builder.py brief-update --graph code2db-out/ \
  --add hard_rules --json '{"rule": "开启 XXX 宏", "type": "macro", "evidence": "meson.build:12"}'
python3 scripts/code2database_builder.py brief-update --graph code2db-out/ \
  --remove modes --index 0                                                      # 小范围删除
python3 scripts/code2database_builder.py brief-update --graph code2db-out/ --refresh-stats
python3 scripts/code2database_builder.py brief-validate --graph code2db-out/    # schema + 体积预算 + 图漂移
```

`brief-validate` 渲染超 6000 字符即失败 — 溢出内容应放进 memory（`save-memory`）或图里，而不是简报。

## 统一 KB 索引

`kb-rebuild-index` 把 **memory.db 条目 + 简报节** 索引进 `code2database.db` 的 `kb_paragraphs`（FTS5+BM25）：

```bash
python3 scripts/code2database_builder.py kb-rebuild-index --graph code2db-out/
python3 scripts/code2database_builder.py kb-query --graph code2db-out/ --query "bdev register"
```

Kind：`memory_qa`、`memory_experience`、`hard_rule`、`mode`、`abstraction`、`description`、`must_know`、`conventions`、`pitfalls`、`query_paths`。`build`/`update` 或 memory/brief 修改后运行。

## 迁移说明

旧的 `memory/*.json`（root/leaf 布局）与 `knowledge/*.md` 存储已退役。JSON 数据不做迁移 — memory 在 memory.db 中全新开始；重要 Q&A 用 `save-memory --category ...` 重建。磁盘上的旧文件仅被忽略。
