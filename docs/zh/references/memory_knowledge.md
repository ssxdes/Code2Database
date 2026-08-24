# 记忆与知识系统

本参考文档介绍 Code2Database 的持久化记忆与知识抽取系统。
两个系统都以 JSON 文件形式持久化到 graph 输出目录,并使用 `fcntl`
文件锁保证并发安全。

> **代码数据库的复利效应**：图谱是结构真相（代码今天说了什么）。记忆是对话真相（我们讨论过代码的什么）。知识是持久真相（我们从文档+图分析中提取了什么）。三层合起来形成一个数据库，价值随时间复利：每次被回答并保存的查询，都是下一次会话少做的一次 grep。一个咨询这三层的 LLM 代理不是在重读你的代码库——它在查询一个精心整理、可审计、版本锁定的知识库。

## 何时使用哪个系统

| 系统 | 用途 | 存储内容 | 是否衰减? |
|------|------|---------|-----------|
| Memory(记忆) | 跨会话的问答对 | question、answer、tags、chains、node_ids、weight、status | 是 — weight<0.1 时归档到 experience |
| Knowledge(知识) | 从代码库抽取的事实 | Markdown 文件（architecture/principles/glossary/constraints/patterns 等） | 否 — 文件无 weight，不衰减 |

**Memory** 用于"Claude,记住我们昨天讨论了什么"这类跨会话问答回溯。
**Knowledge** 用于"关于这个模块的错误处理模式我们知道什么"这类
从文档+graph 分析中提取的持久事实。

## Memory 系统

### 文件布局

```
<graph_outdir>/
  memory/
    index.json              ← 元数据：id → 条目指针、tags、status、root_id
    L0_index.json           ← 热点记忆（weight > 0.7）
    L1_index.json           ← 温记忆（0.3 < weight <= 0.7）
    L2_index.json           ← 冷记忆（weight <= 0.3）
    root/
      root_<id>.json        ← 规范合并条目（每个 Q&A 聚类一个）
    leaf/
      mem_<id>.json          ← 独立 Q&A 条目（可能被合并到 root）
    experience/
      experience_<id>.json   ← 归档条目（weight 衰减或 graph 变更导致失效）
      index.json            ← experience 索引
  .scratch/                 ← 会话级临时记忆（带 TTL）
```

### 条目 schema

```json
{
  "id": 5,
  "question": "bdev 层如何注册新的 io_device?",
  "answer": "bdev_register() 在 ... 之后调用 io_device_register()",
  "tags": ["bdev", "io_device", "registration"],
  "chains": [],
  "node_ids": ["bdev_register", "io_device_register"],
  "status": "trusted",
  "weight": 1.23,
  "root_id": 5,
  "merged_count": 2,
  "access_count": 3,
  "knowledge_refs": ["principles.md"],
  "versions": [{"answer": "...", "version": 1, "merged_from": 7}],
  "created": "2026-07-15T10:23:45",
  "last_accessed": "2026-07-29T14:00:00",
  "validated_at": "2026-07-29T14:00:00"
}
```

### CLI 命令

| 命令 | 动作 |
|------|------|
| `save-memory --question Q --answer A --tags t1,t2` | 保存新条目（相似时自动合并到 root） |
| `search-memory --query "bdev register"` | 用 Jaccard token 相似度搜索记忆 |
| `manage-memory --action add/correct/reshape/promote/refine` | 写入持久记忆（脚本化操作） |
| `manage-memory --action decay` | 执行 weight 衰减 + 归档低权重到 experience |
| `manage-memory --action consolidate` | 一次性：decay + 重建索引 + 归档 |
| `validate-memory` | 对照当前 graph 检查条目；过期 → experience |
| `memory-health` | 报告条目数、分层计数、最早条目、scratch 会话 |

### 衰减逻辑

条目 weight = `recency × importance × access`，上限 10.0：
- `recency = exp(-0.05 × days)` — 约 14 天衰减到 50%
- `importance = 1 + 0.10 × merged_count + 0.05 × (answer_length / 1000)`
- `access = 1 + 0.10 × access_count`

当 `weight < 0.1` 时,条目被归档到 `experience/` 并标记
`status="experience"`。`decay` 动作会重算 weight 并归档；
`consolidate` 动作在一次性内完成 decay + 重建所有索引。
没有 `--threshold-days` 参数 — 衰减通过 `memory_manager.py` 中的
`DECAY_LAMBDA` 常量持续进行。

### 并发

所有写入都通过 `_atomic_write_locked()`:
1. 先写到 `<file>.tmp.<pid>`
2. `os.replace()` 重命名为最终路径(POSIX 上原子)
3. 重命名期间持有 `fcntl.flock(LOCK_EX)` 排他锁

读取使用 `fcntl.flock(LOCK_SH)` 共享锁,多个读取互不阻塞。
在没有 fcntl 的系统(Windows)上,锁会优雅跳过 — 系统仍可工作,
只是没有并发写保护。

## Knowledge 系统

### 文件布局

```
<graph_outdir>/
  knowledge/
    index.json              ← files + topics 索引（extract-knowledge 时重建）
    _meta.json              ← 每文件来源 provenance（auto/manual/llm_generated）
    _memory_links.json      ← 到 memory 条目的双向链接
    architecture.md         ← graph 推断的架构总览
    principles.md           ← LLM 整理的协议/契约原则
    glossary.md             ← 术语（从 labels 自动抽取）
    constraints.md          ← API 约束、配置规则
    patterns.md             ← 代码模式
    build_rules.md          ← 构建系统规则
    detail_*.md             ← 自动抽取的源码模式（macros、structs 等）
    custom_*.md             ← 用户/LLM 定义的主题
```

### Knowledge 文件 schema

Knowledge 以 **Markdown 文件**形式存储（非结构化 JSON）。每个文件
是一个主题；文件中每个 `##` 标题是一个子主题。`index.json` 列出
文件及其标题：

```json
{
  "files": [
    {"name": "principles.md", "size": 4523, "headings": ["Protocol Standards", "API Contracts"]}
  ],
  "topics": ["Protocol Standards", "API Contracts"]
}
```

### CLI 命令

| 命令 | 动作 |
|------|------|
| `extract-knowledge --source docs/ --graph <outdir>` | 抽取知识模板（graph 推断 + 文档标题） |
| `apply-knowledge --graph <outdir>` | 把 LLM 填好的 `.code2database_knowledge_input.json` 应用到 knowledge/ |
| `knowledge-query --topic "error_handling"` | 按主题搜索知识（跨 .md 文件子串匹配） |
| `knowledge-validate` | 检查过期 domain、纯签名文件、纯模板文件 |

### 校验检查

`knowledge-validate` 会报告:
- Knowledge 文件引用的 domain 在当前 graph 中已不存在（过期）
- 只含函数签名的 Knowledge 文件（无实际知识）
- 只含模板注释（`FILL IN`、`LLM_FILL`）的 Knowledge 文件
- 文档-代码对齐不匹配（semantic_desc vs body_text/signature）

## 统一 KB 查询（Phase 1-3 升级）

上述 memory / knowledge 两套存储原本各自独立搜索（Jaccard / 子串匹配），
Phase 1-3 引入统一 FTS5+BM25 查询面 `kb-query`：

- `kb-rebuild-index` 把 `memory/*.json` + `knowledge/*.md` 索引到
  `code2database.db` 的 `kb_paragraphs` 表 + FTS5 虚拟表
- `kb-query --query "..."` 跨两套存储统一查询，返回带 BM25 score
  和 source_kind（memory / knowledge）的排序结果
- `query`（Cypher）命令会自动把 top 3 kb 命中注入到结果的
  `_hints` 字段
- `describe-node` 返回节点的 `memory_refs` 和 `knowledge_refs`
- MCP 工具 `code2database_kb_query` 暴露给 LLM 代理

## 与查询的集成

查询优先级链**目前是 aspirational** — `cmd_query` 当前仅查询
graph + cgdb 表。要咨询 memory/knowledge,代理必须显式调用
`search-memory` / `knowledge-query`（或其 MCP 等价物
`code2database_memory_search` / `code2database_knowledge_query`），
或用新的统一 `kb-query` 命令。

```
context_pack_micro → context_pack_lite → explore-flow
  → (手动) knowledge-query / kb-query → (手动) search-memory / kb-query → describe-node
```

`memory_pack_lite.json` 和 `knowledge_pack_lite.json` 分别由
`manage-memory --action pack` 和 `extract-knowledge` 生成；
它们与 `context_pack_lite.json` 并排放置，供代理按需查阅。
Phase 3 起 `_build_context_pack` 会把这两份 pack 合并进
context_pack 的 `memory_summary` + `knowledge_summary` 字段。

## 编程式访问

```python
from _builder.memory_manager import MemoryManager
from _builder.knowledge_manager import KnowledgeManager
from _builder.kb_index import query_kb, rebuild_kb_index

# 重建统一 FTS5 索引（在 build/update 后运行）
rebuild_kb_index("/path/to/code2db-out")

# 统一查询（跨 memory + knowledge）
results = query_kb("/path/to/code2db-out", "bdev register", top_n=10)

# 单独访问 memory
mem = MemoryManager(graph_dir="/path/to/code2db-out")
mem.add(question="...", answer="...", tags=["bdev"], node_ids=["bdev_register"])
results = mem.query("bdev register")

# 单独访问 knowledge
know = KnowledgeManager(graph_dir="/path/to/code2db-out")
know.query_knowledge("error_handling", max_tokens=500)
```

两个管理器在首次使用时会自动创建目录结构。
