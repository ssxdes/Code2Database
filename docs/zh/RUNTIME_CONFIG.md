# 运行时配置指南

**文件**: `config/runtime.json`

此文件为调用图流水线提供默认运行时参数。所有值均可手动编辑，修改在下一次流水线运行时生效。

> **为你的环境调优数据库。** `runtime.json` 是当默认值不合适时你拧的旋钮：受约束 CI 机器的扫描并行度、长时间运行代理会话的内存合并阈值、快速移动仓库的过期检测灵敏度。图谱本身是项目数据；这个文件是*运营*数据——流水线怎么跑，不是它提取什么。

---

## 何时编辑

当需要在不修改源代码的情况下调整流水线行为时编辑此文件——例如，为资源受限的机器调整并行度、更改内存管理阈值，或自定义查询默认值。

---

## 字段参考

### `scan` — 扫描阶段参数

| 字段 | 类型 | 默认值 | CLI 覆盖 | 说明 |
|------|------|--------|----------|------|
| `workers` | int | `0` | `--workers` | 并行扫描工作线程数。`0` = 自动（取 CPU 核心数与 8 的较小值）。`1` = 顺序执行（无并行）。在多核机器上增大此值可加快扫描速度；内存紧张时减小此值。 |
| `parallel_mode` | string | `"thread"` | `--parallel-mode` | 多工作线程扫描的并行模型：`"thread"`（ThreadPoolExecutor — 受 GIL 约束但无 pickle 开销；tree-sitter 在解析期间释放 GIL，因此 C/C++ 扫描可获得真实加速）或 `"process"`（ProcessPoolExecutor + fork COW — 每个子进程拥有独立的解释器和 tree-sitter 解析器，绕过 GIL 进行 Python 密集型后处理）。当代码库 >1000 个文件且每个文件工作量较大时，使用 `"process"` 可获得显著加速。子进程通过写时复制继承内存，内存开销有界（每个子进程约 500MB 的 Python + 模块基线；结果不会在子进程中累积）。在 fork 不可用的平台（非 Linux）上回退到线程模式。 |
| `max_file_size_kb` | int | `1024` | — | 扫描的最大文件大小（单位：KB）。超过此大小的文件将被跳过。如果需要包含大型生成文件则增大此值；若要跳过庞大的自动生成代码则减小此值。 |
| `skip_dirs` | string[] | `[".git", "__pycache__", "node_modules", "build", ".cache"]` | — | 扫描时跳过的目录名（按目录名匹配，非路径）。这些值与扫描器内置的跳过集合（`__pycache__`、`node_modules`、`.git`、`build`、`dist`、`out`、`bin`、`obj`、`venv`、`.venv`、`.tox`、`.cache`、`third_party`、`vendor` 等）**合并**。添加需要排除的项目特定目录（如 `["generated", "vendor"]`）。 |

### `build` — 构建阶段参数

| 字段 | 类型 | 默认值 | CLI 覆盖 | 说明 |
|------|------|--------|----------|------|
| `default_config` | string | `"auto"` | `--build-config` | 用于 `#ifdef` 宏解析的构建配置。`"auto"` = 从构建系统自动检测。也可以是 `compile_commands.json` 的路径或构建类型名称，如 `"Release"`、`"Debug"`。 |
| `max_domain_files` | int | `50` | `--max-domain-files` | 写入域拆分输出时每个子目录的最大 JSON 文件数。`0` = 扁平模式（所有内容写入一个文件）。对于非常大的项目，增大此值可保持单个文件较小；减小此值可减少文件数量。 |

### `query` — 查询阶段参数

| 字段 | 类型 | 默认值 | CLI 覆盖 | 说明 |
|------|------|--------|----------|------|
| `default_detail` | string | `"brief"` | `--detail` | 查询响应的默认详细程度。可选值：`"brief"`（一行摘要）、`"standard"`（中等详情）、`"full"`（完整信息）。 |
| `default_max_tokens` | int | `500` | `--max-tokens` | 查询响应的默认最大输出 token 数。`0` = 无限制。较低的值产生较短的响应；较高的值提供更多详情。 |
| `explore_max_nodes` | int | `15` | — | `explore` 查询返回的最大节点数。控制目标节点周围邻域探索的广度。 |
| `explore_max_tokens` | int | `2000` | — | `explore` 查询响应的最大输出 token 数。 |

### `memory` — 记忆系统参数

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `decay_factor` | float | `0.95` | 记忆整合期间应用的权重衰减乘数。控制未使用的记忆条目随时间丢失权重的速度。范围 0.0–1.0。较高的值 = 较慢的衰减（记忆持续更久）；较低的值 = 较快的衰减（过时记忆更早被裁剪）。实际衰减使用指数衰减，每天 `DECAY_LAMBDA = -ln(decay_factor)`。 |
| `consolidate_threshold` | int | `100` | 触发自动整合的最少记忆条目数。整合执行权重衰减、归档低权重条目并重建索引。设置更高可减少整合频率；设置更低可保持记忆更紧凑。 |
| `scratch_ttl_hours` | float | `24.0` | 临时（scratch）记忆条目的生存时间（单位：小时）。临时条目在此时长后自动过期。增大此值可延长临时上下文的存活时间；减小此值可更快清理。 |

### `semantic` — 语义分析参数

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `stale_ratio_threshold` | float | `0.15` | 推荐语义更新的过时节点比率阈值。当过时（outdated）节点的占比超过此值时，系统建议运行完整语义更新。较低的值 = 更敏感（更早推荐更新）；较高的值 = 更宽容。 |
| `stale_api_threshold` | int | `1` | 触发语义更新推荐的最小过时 API 入口数。如果任何 API 入口变为过时状态，则推荐刷新。设置为 `0` 表示不单独因过时 API 触发；增大此值可容忍更多过时 API。 |

### `invariants` — 不变量提取参数

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `state_machine_threshold` | int | `1` | 单个状态变量的最小赋值次数，超过后将其视为状态机。增大到 `2` 可减少计数器变量较多的项目上的误报。 |
| `extract_preconditions` | bool | `true` | 提取函数入口附近的 `if (!cond) return` 模式作为前置条件。 |
| `extract_postconditions` | bool | `true` | 提取出口附近的 `return cond` / `assert(cond)` 作为后置条件。 |
| `extract_loop_invariants` | bool | `true` | 从 `for`/`while` 循环体中提取循环不变量。 |
| `reject_ambiguous` | bool | `true` | 永不应用 AMBIGUOUS 置信度的不变量。INFERRED 需用户确认；EXTRACTED 自动应用。 |

### `auto_enhance` — LLM 自动增强参数

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `auto_apply_extracted` | bool | `true` | 自动应用 EXTRACTED+证据的增强，无需提示。 |
| `require_confirm_inferred` | bool | `true` | INFERRED 增强需用户确认。 |
| `reject_ambiguous` | bool | `true` | 直接拒绝 AMBIGUOUS 增强。 |
| `rollback_window` | int | `100` | 回滚日志中保留的已应用增强条目数。超出此窗口的旧条目会被裁剪。 |
| `batch_confirm_size` | int | `20` | 每次 `batch-confirm` 调用处理的待确认 INFERRED 增强数量。 |

### `transactions` — 事务性更新参数

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `wal_enabled` | bool | `true` | 启用写前日志（WAL）。设置为 `false` 适用于非关键工作区（更快但无崩溃恢复）。 |
| `snapshot_keep_count` | int | `10` | 保留的命名快照数量。更旧的快照会被裁剪。 |
| `lock_timeout_seconds` | int | `30` | fcntl 锁获取超时时间。对于慢磁盘或繁忙 CI 可增大此值。 |
| `auto_replay_on_start` | bool | `true` | 下次进程启动时自动重放未完成的 WAL。 |

### `ffi` — 跨语言 FFI 参数

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `detect_python_ctypes` | bool | `true` | 检测 Python `CDLL`/`WinDLL`/`cffi`/`pybind11` 绑定。 |
| `detect_go_cgo` | bool | `true` | 检测 Go `import "C"` cgo 绑定。 |
| `detect_rust_extern` | bool | `true` | 检测 Rust `extern "C"` / `#[no_mangle]` 绑定。 |
| `flag_lossy_conversions` | bool | `true` | 当类型转换可能丢失数据（如 int64 → int32）时，将类型marshalling边标记为 `lossy: true`。 |

### `web_ui` — Web UI 参数

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `port` | int | `8765` | 交互式 Web UI 的 HTTP 端口。 |
| `host` | string | `"127.0.0.1"` | 绑定主机。使用 `"0.0.0.0"` 可共享访问（不推荐）。 |
| `open_browser` | bool | `false` | 启动时自动打开浏览器。 |
| `max_nodes_render` | int | `5000` | SVG 中渲染的最大节点数；更大的图谱会被采样。 |

### `benchmark` — BUG 基准测试参数

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `recall_target` | float | `0.95` | GraphInvestigator 的目标召回率。低于此值时，基准报告将测试标记为"图谱能力不足"。 |
| `max_tool_calls` | int | `30` | 每个调查员的工具调用硬上限。 |
| `max_tokens` | int | `20000` | 每个调查员的 token 预算。 |

### `profile_health` — Profile 健康度参数

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `min_score` | int | `70` | 可接受的最低 profile 健康度评分。低于此值时，`profile-health` 将 profile 标记为陈旧。 |
| `auto_apply_extracted` | bool | `true` | `profile-evolve --apply` 自动应用 EXTRACTED 置信度的建议。 |
| `require_confirm_inferred` | bool | `true` | INFERRED 建议需用户确认。 |
| `bind_to_head` | bool | `true` | 在 `profile-bind-version` 时将 profile 绑定到 git/svn HEAD。陈旧 profile（HEAD 不匹配）会被标记。 |

### `doc_code` — 文档-代码对齐参数

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `check_on_describe` | bool | `true` | `describe-node` 默认暴露 `doc_code_mismatches`。 |
| `check_on_knowledge_validate` | bool | `true` | `knowledge-validate` 运行文档-代码对齐检查。 |
| `signature_diff_strict` | bool | `false` | 严格签名差异检查（参数顺序重要）。 |

### `daemon` — 后台守护进程参数

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `false` | 构建后自动启动守护进程。建议：保持 `false` 并显式使用 `daemon-start` 启动。 |
| `watch_paths` | string[] | `[]` | 要监视的源路径。为空 = 使用最近扫描的 `--source`。 |
| `exclude_patterns` | string[] | `["*.swp", "*.tmp", ".git/*"]` | 排除监视的 glob 模式。 |
| `debounce_ms` | int | `500` | 编辑器保存的去抖动窗口。较低的值 = 响应更快但重扫描更多。 |
| `batch_window_ms` | int | `1000` | 合并多个事件的批处理窗口。 |
| `auto_rebuild_outputs` | bool | `true` | 每次同步后触碰 `CODE2DATABASE_SUMMARY.md`、上下文包、索引。 |
| `idle_sleep_minutes` | int | `30` | 守护进程进入低功耗轮询前的空闲睡眠时间。 |
| `max_events_per_minute` | int | `1000` | 断路器阈值；超过此值触发整体重建。 |
| `backend` | string | `"auto"` | `"inotify"`（Linux）、`"polling"` 或 `"auto"`（可用时使用 inotify，否则 polling）。 |
| `startup_grace_sec` | float | `60` | 启动宽限期：守护进程启动后该窗口内观察到的文件事件会被持有、不触发同步。避免刚完成的构建或守护进程重启引发事件风暴。`daemon-wait-sync` 和 `daemon-force-refresh` 会提前结束宽限期。可通过环境变量 `CALLGRAPH_DAEMON_STARTUP_GRACE_SEC` 覆盖。 |

**注意**：守护进程不会监视自己的 `--graph` 输出目录（防止自反馈循环）——除非 graph_dir 与源码根目录相同。

---

## 与 CLI 参数的关系

大多数 `runtime.json` 字段提供默认值，可通过命令行参数覆盖：

| runtime.json 路径 | CLI 参数 | 说明 |
|---|---|---|
| `scan.workers` | `code2database scan --workers N` | CLI 优先 |
| `build.default_config` | `code2database build --build-config VALUE` | CLI 优先 |
| `build.max_domain_files` | `code2database build --max-domain-files N` | CLI 优先 |
| `query.default_detail` | `code2database describe --detail LEVEL` | CLI 优先 |
| `query.default_max_tokens` | `code2database describe --max-tokens N` | CLI 优先 |
| `scan.max_file_size_kb` | — | 无 CLI 覆盖，仅限 runtime.json |
| `scan.skip_dirs` | — | 无 CLI 覆盖，仅限 runtime.json |
| `memory.*` | — | 无 CLI 覆盖，仅限 runtime.json |
| `semantic.*` | — | 无 CLI 覆盖，仅限 runtime.json |

---

## 常见自定义配置

### 内存受限机器（< 16GB RAM）
```json
{
  "scan": {
    "workers": 1,
    "max_file_size_kb": 512
  },
  "memory": {
    "consolidate_threshold": 50,
    "scratch_ttl_hours": 4
  }
}
```

### 高性能机器（64+ 核，64GB+ RAM）
```json
{
  "scan": {
    "workers": 16,
    "max_file_size_kb": 4096
  },
  "build": {
    "max_domain_files": 100
  }
}
```

### 包含大量自动生成目录的项目
```json
{
  "scan": {
    "skip_dirs": [".git", "__pycache__", "node_modules", "build", ".cache",
                  "generated", "autogen", "protobuf"]
  }
}
```

### 更激进的过时检测
```json
{
  "semantic": {
    "stale_ratio_threshold": 0.08,
    "stale_api_threshold": 0
  }
}
```
