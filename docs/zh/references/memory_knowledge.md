# 记忆与知识系统

本参考文档介绍 Code2Database 的持久化记忆与知识抽取系统。
两个系统都以 JSON 文件形式持久化到 graph 输出目录,并使用 `fcntl`
文件锁保证并发安全。

> **代码数据库的复利效应**：图谱是结构真相（代码今天说了什么）。记忆是对话真相（我们讨论过代码的什么）。知识是持久真相（我们从文档+图分析中提取了什么）。三层合起来形成一个数据库，价值随时间复利：每次被回答并保存的查询，都是下一次会话少做的一次 grep。一个咨询这三层的 LLM 代理不是在重读你的代码库——它在查询一个精心整理、可审计、版本锁定的知识库。

## 何时使用哪个系统

| 系统 | 用途 | 存储内容 | 是否衰减? |
|------|------|---------|-----------|
| Memory(记忆) | 跨会话的问答对 | 问题、答案、标签、时间戳、访问次数 | 是 — 30 天未访问的条目会衰减 |
| Knowledge(知识) | 从代码库抽取的事实 | 主题、事实、来源、置信度、相关函数 | 否 — 事实与 graph 版本绑定 |

**Memory** 用于"Claude,记住我们昨天讨论了什么"这类跨会话问答回溯。
**Knowledge** 用于"关于这个模块的错误处理模式我们知道什么"这类
从文档+graph 分析中提取的持久事实。

## Memory 系统

### 文件布局

```
<graph_outdir>/
  memory/
    index.json              ← 元数据:id → 条目指针,标签,上次访问
    entries/
      <id>.json             ← 完整问答条目
```

### 条目 schema

```json
{
  "id": "mem_1709312345_abc123",
  "question": "bdev 层如何注册新的 io_device?",
  "answer": "bdev_register() 在 ... 之后调用 io_device_register()",
  "tags": ["bdev", "io_device", "registration"],
  "created": "2026-07-15T10:23:45Z",
  "last_accessed": "2026-07-29T14:00:00Z",
  "access_count": 3,
  "source": "conversation",
  "domain": "bdev"
}
```

### CLI 命令

| 命令 | 动作 |
|------|------|
| `save-memory --question Q --answer A --tags t1,t2` | 保存新条目 |
| `search-memory --query "bdev register"` | 全文检索 Q+A+标签 |
| `manage-memory --action list` | 列出所有条目及元数据 |
| `manage-memory --action delete --id <id>` | 删除条目 |
| `manage-memory --action decay --threshold-days 30` | 删除 30 天未访问的条目 |
| `memory-health` | 报告条目数、最早条目、衰减候选 |

### 衰减逻辑

当 `now - last_accessed > threshold_days` 时,条目成为衰减候选。
衰减命令会删除衰减候选,但从不删除 `access_count >= 5` 的条目
(高频访问的条目不论多久都会保留)。默认阈值 30 天,可用
`--threshold-days` 覆盖。

### 并发

所有写入都通过 `_atomic_write_locked()`:
1. 先写到 `entries/<id>.json.tmp.<pid>`
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
    index.json              ← 主题 → 知识文件指针
    topics/
      <topic>.json          ← 一个主题的所有事实
```

### 事实 schema

```json
{
  "topic": "error_handling",
  "fact": "所有 bdev 函数在返回负 errno 之前都调用 SPDK_ERRLOG",
  "source": "docs/bdev.md:42",
  "confidence": 0.9,
  "related_functions": ["bdev_register", "bdev_open"],
  "extracted_at": "2026-07-15T10:00:00Z",
  "graph_version": "v1.2.0"
}
```

### CLI 命令

| 命令 | 动作 |
|------|------|
| `extract-knowledge --source docs/ --graph <outdir>` | 从文档+graph 抽取事实 |
| `apply-knowledge --topic error_handling` | 将主题事实应用到 graph 以增强 |
| `knowledge-query --topic error_handling` | 列出某主题的所有事实 |
| `knowledge-validate` | 检查过期引用、低置信度、缺失字段 |

### 校验检查

`knowledge-validate` 会报告:
- `confidence < 0.5` 的事实(建议重新抽取)
- `related_functions` 在当前 graph 中已不存在的事实
- 缺少必填字段(主题、事实、来源)的事实
- 只有一个事实的主题(覆盖薄弱)

## 与查询的集成

查询优先级链:

```
context_pack_micro → context_pack_lite → explore-flow
  → knowledge_pack_lite → memory_pack_lite → describe-node
```

当仅基于 graph 的 pack 无法回答问题时,会查询 `knowledge_pack_lite`
和 `memory_pack_lite`。这些 pack 通过检索记忆/知识索引中标签或
主题与查询词重叠的条目来组装。

### 编程式访问

```python
from _builder.memory_manager import MemoryManager
from _builder.knowledge_manager import KnowledgeManager

mem = MemoryManager(outdir="/path/to/code2db-out")
mem.save("问题", "答案", tags=["bdev"])
results = mem.search("bdev register")

know = KnowledgeManager(outdir="/path/to/code2db-out")
know.query_topic("error_handling")
```

两个管理器在首次使用时会自动创建目录结构。
