<div align="center">

# Code2Database

### 将代码工程升级为可查询的代码数据库——不只是可读的文本

**一次扫描 · 持久图谱 · 精准查询 · 更少工具调用 · 更快回答**

[![语言](https://img.shields.io/badge/语言-7%20%2B%20ASM-orange)](#语言支持)
[![MCP工具](https://img.shields.io/badge/MCP工具-53-blueviolet)](#mcp-服务器)
[![查询命令](https://img.shields.io/badge/查询命令-200-success)](#命令参考)
[![许可证: MIT](https://img.shields.io/badge/许可证-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.8%2B-blue?logo=python&logoColor=white)](#安装)
[![tree-sitter](https://img.shields.io/badge/tree--itter-AST-green?logo=tree-sitter&logoColor=white)](#工作原理)

[![Claude Code](https://img.shields.io/badge/Claude_Code-支持-blueviolet.svg)](#安装)
[![Codex CLI](https://img.shields.io/badge/Codex_CLI-支持-blueviolet.svg)](#安装)
[![MCP stdio](https://img.shields.io/badge/MCP_stdio-支持-blue.svg)](#mcp-服务器)

[English](../../README.md) · [Skill 指南](SKILL.md)

</div>

---

## 转变：从*阅读*代码到*查询*代码

现今的 AI 代理理解代码库的方式很慢——`grep`、`glob`、`Read`，一次一个文件，手动重建调用路径和依赖关系。每个问题都要重新发现同样的结构。

**Code2Database 把它变成一次查询。** 扫描一次，代码库就变成一个持久知识图谱：每个函数、每条调用边、每个条件、每个并发隐患、每个字段访问——都可索引、可查询、LLM 可用。下一个问题不再是"让我 grep 一下"，而是一次工具调用，返回精准的节点、节点之间的调用路径、门控条件，以及如果你改动它的影响半径。

> **精准上下文，不是逐文件搜索。** 更少工具调用。更快回答。图谱随团队持续查询而累积价值。

| 只读代码文本              |  →  | 可查询代码数据库                |
| :----------------------- | :-: | :----------------------------- |
| `grep` / `glob` / `Read` | →   | `explore-flow`  → 节点 + 路径   |
| 一次一个文件              | →   | `trace-chain`   → A → B（带条件）|
| 每个问题重新发现          | →   | `detect-races`  → 跨线程隐患    |
| 手动调用链追踪            | →   | `param-flow`    → 跨函数数据流  |
| 无并发可见性              | →   | `field-access`  → 谁读写了 X    |
| 无 `#ifdef` 感知          | →   | `reverse-trace` → 到崩溃点的所有路径 |

---

## 何时使用

- 理解代码架构和函数调用关系
- 从 API 入口追踪执行路径到内部实现
- **影响分析**——修改某函数会影响什么？
- **并发风险**——检测多线程数据竞争和死锁
- **崩溃调试**——从崩溃点反向追踪到所有入口点
- 理解条件编译（`#ifdef`）对调用路径的影响
- 查询哪些函数访问特定结构体字段或全局变量
- 构建可增量更新的代码图
- 给 LLM 代理一个紧凑的项目地图，按需钻取

---

## 快速开始

```bash
# 1. 安装
bash install.sh --lang zh --target all

# 2. 生成项目 profile（项目特定约定：回调、vtable、锁 API）
#    自动检测项目类型，在源码目录写入 .code2database_profile.json。
#    内置 profile 已覆盖常见项目（linux_kernel、dpdk、spdk 等）——
#    可跳过此步，扫描时改用 --profile config/profiles/<type>.json。
python3 scripts/code2database_scanner.py auto-profile \
  --source /path/to/code \
  --outdir /path/to/code

# 3. 扫描源码 → extraction.json（不可变 AST 事实）
python3 scripts/code2database_scanner.py scan \
  --source /path/to/code \
  --profile /path/to/code/.code2database_profile.json \
  --output code2db-out/.code2database_extraction.json

# 4. 构建调用图 → 可查询数据库
python3 scripts/code2database_builder.py build \
  --extraction code2db-out/.code2database_extraction.json \
  --outdir code2db-out/ --build-config auto

# 5. 查询图
python3 scripts/code2database_builder.py explore-flow \
  --graph code2db-out/ --query "初始化" --max-tokens 2000

# 6.（可选）启动后台守护进程实时自动同步
python3 scripts/code2database_builder.py daemon-start \
  --graph code2db-out/ --source /path/to/code
```

构建完成后，先读 `code2db-out/.code2database_context_pack_micro.md`（约 200 tokens）获取项目概览，升级到 `lite` 获取更多细节，再用查询命令钻取。这种 **micro → lite → local** 模式让 token 开销最小——代理只加载问题真正需要的部分。

---

## 安装

### 一键安装（推荐）

```bash
# 交互式：选择语言和安装路径
bash install.sh

# 非交互式：
bash install.sh --lang zh --dir ~/.claude/skills/Code2Database --target claudecode
bash install.sh --lang en --dir ~/.local/share/Code2Database --target codex
bash install.sh --target all  # 为所有支持的工具安装
```

安装器在 `~/.claude/skills/` 下安装 **3 个子技能**：
- `Code2Database`（核心——始终加载，拥有 `scripts/`）
- `Code2Database-analysis`（深度分析——按需）
- `Code2Database-ops`（图谱编辑 + 运维——按需）

两个按需子技能 symlink 到核心的 `scripts/` 目录——单一 CLI，无重复。

支持的目标：
- **claudecode** — Claude Code（安装 skill 到 `~/.claude/skills/`，配置 MCP 服务器）
- **codex** — Codex CLI（添加指令到 `~/.codex/instructions.md`）
- **cursor** — Cursor（创建规则文件 + MCP 配置）
- **opencode** — OpenCode（创建含 MCP 服务器的配置）
- **gemini** — Gemini CLI（创建指令文件）

### 部分语言安装（单语言工程师的精简安装）

如果你只使用一两种语言，可以跳过不需要的 tree-sitter 语法：

```bash
# 仅安装 C 和 Go 语法（跳过 C++、Python、Java、Rust）
C2D_LANGUAGES=c,go bash install.sh --lang zh --target claudecode

# 或直接运行 setup.sh 指定语言
bash scripts/setup.sh --languages c,go
bash scripts/setup.sh --languages c,cpp,rust --with-optional  # 同时安装 libclang + z3
```

有效值：`c`、`cpp`、`go`、`python`、`java`、`rust`、`all`（默认）。ASM 用正则——无需 tree-sitter 语法。

### 手动安装

```bash
# 全部语言（默认）
pip install -r scripts/requirements.txt

# 或通过 setup.sh 指定语言
bash scripts/setup.sh --languages c,go
```

依赖：networkx、tree-sitter + 语言绑定（按语言——见上方"部分语言安装"）
可选（推荐，非必装）：
- `libclang>=17.0` ——启用 cgdb clang 后端（类型化 vtable 派发、CFG、数据流、同步原语、配置谓词）。无此包时 tree-sitter-only 模式仍然完全可用。
- `z3-solver>=4.12` ——可靠的路径可行性（无此包时启发式回退）
- `python-igraph>=0.11` + `leidenalg>=0.10` ——跨域 Leiden 社区检测（无此包时域回退）

---

## 语言支持

每种语言都获得完整的结构提取和跨文件解析，统一进同一张图——无需按语言配置：

| 语言 | 扩展名 | 提取内容 |
|------|--------|---------|
| **C / C++** | `.c` `.cc` `.cpp` `.h` `.hpp` `.cxx` | 函数、调用、`#ifdef` 分支、结构体字段访问、宏、回调 |
| **Go** | `.go` | 函数、方法、goroutine 创建、channel 操作、interface 派发 |
| **Python** | `.py` | 函数、类、装饰器、异步调用、动态派发 |
| **Java** | `.java` | 类、方法、interface 派发、注解、override |
| **Rust** | `.rs` | 函数、trait、impl、async、泛型、宏展开 |
| **汇编 (ASM)** | `.S` `.s` `.asm` | 函数标签、call 指令、寄存器使用（正则提取） |

外加**profile 驱动**的框架/构建系统/项目约定检测——切换一个 Profile JSON 即可让工具适配新项目类型。

---

## 为什么选 Code2Database？——差异在哪里

多数代码图谱工具止步于"函数调用函数"。Code2Database 走得更深：它追踪**门控每次调用的条件**、调用运行的**并发上下文**、它触碰的**字段和全局变量**、以及决定它是否被编译的**条件编译**。这是 *调用图* 和 *可以问真实工程问题的代码数据库* 之间的差距。

| 能力 | 给你带来什么 |
|------|------------|
| **条件感知链** | `if`/`switch`/`#ifdef` 分支 + 空节点聚合。你看到的不仅是"A 调 B"，而是"A **当** `CONFIG_X` 已定义 **且** `flag == 1` 时调 B" |
| **并发建模** | 线程创建、goroutine、回调检测。跨线程数据竞争检测。并发安全分析：这两条链真的能并行执行吗？ |
| **字段级访问追踪** | 查询"哪些函数读/写 `task_struct->pid`"——跨整张图，不是 grep |
| **崩溃反向追踪** | 给定崩溃点，追踪从入口点到达它的*所有*路径。专为事后调试设计 |
| **条件编译（`#ifdef`）** | 图谱知道哪些调用只在哪些 `CONFIG_*` 标志下存在。对内核/嵌入式/跨平台 C 至关重要 |
| **4 级 LLM 上下文包** | `micro`（~200 tokens）→ `lite` → `std` → `full`。代理从最小的地图开始，只在问题需要的地方加载细节 |
| **边置信度** | 每条边标注 `EXTRACTED`（1.0）/ `INFERRED`（0.7–0.95）/ `AMBIGUOUS`（0.1–0.3）——你始终知道什么是发现的 vs 推断的 |
| **Profile → Scan → Build 分离** | Scan 阶段产生不可变 AST 事实；Build 阶段做推理。提取 bug？独立验证 scan。改进推理？秒级重跑 Build，无需重扫 |
| **双语文档（EN/中文）** | Skill 指令、参考文档、用法指南均有英文和中文版 |
| **提交级来源** | 每个节点/边携带 `commit_meta.source_commit`（git/svn 哈希）。工程师用 `git show <hash>` 校验，而非时间戳。见 `describe-commit`、`node-history`、`graph-provenance`、`blame-node`、`find-commits` |
| **Cypher 子集查询** | 声明式图谱查询 `MATCH`/`WHERE`/`RETURN`——`query "MATCH (n)-[r:INVOKES]->(m) WHERE n.name =~ 'foo.*' RETURN n,m"` |
| **值流与 DATA_FLOW 边** | 跨函数追踪参数→返回值传播。回答"这个 NULL 从哪来？"——`value-flow`、`param-flow` |
| **锁持有区域分析** | 基于事件流 + 字符位置的精确锁持有区间，而非仅"锁存在"——`lock-coverage`。降低竞争检测误报 |
| **Z3 路径可行性** | Z3 SMT 求解器自动求解路径可行性；无 Z3 时启发式回退——`path-feasible`。可选 `z3-solver` 依赖 |
| **跨函数数据依赖** | DATA_DEP 边扫描所有节点的读写者，而非仅调用可达后继——`data-dep` |
| **不变量提取** | 每个函数的前置/后置/循环不变量/状态机——`extract-invariants`、`find-invariants`、`apply-invariants` |
| **LLM 自动语义增强** | 置信度阈值自动写入（EXTRACTED+证据自动应用；INFERRED 需确认；AMBIGUOUS 拒绝）+ 批量确认和回滚——`auto-enhance`、`batch-confirm`、`rollback`、`fill-request` |
| **事务性更新** | WAL + 快照 + fcntl 文件锁的原子多步更新——`tx-begin`/`commit`/`rollback`/`status`/`snapshot`/`restore`/`list-snapshots`/`replay-wal` |
| **跨语言 FFI** | Python ctypes / Go cgo / Rust `extern "C"` 边界追踪 + 类型 marshalling——`ffi-detect`、`ffi-list`、`ffi-trace`、`ffi-types` |
| **交互式 Web UI** | 单文件 HTML/cytoscape.js/JS，平移/缩放、点击聚焦、聚焦+上下文淡出、路径高亮、3 种布局算法 (flow/rings/force)、社区复合分组、LOD 标签隐藏、边 `call_condition` 标签、边类型过滤、右键上下文菜单、小地图、FTS5 搜索——`web-ui` |
| **BUG 基准测试** | GraphInvestigator vs GrepInvestigator——衡量召回、精度、工具调用、token、时间——`bug-benchmark` |
| **Profile 健康度与自动演化** | 7 类 0-100 评分；自动检测新回调模式；绑定 git/svn HEAD——`profile-health`、`profile-evolve`、`profile-bind-version` |
| **文档-代码对齐** | 检测返回值/参数/签名/陈旧文档不匹配——`doc-code-check`、`doc-mark-stale`、`doc-alignment-report`、`doc-signature-diff`。`describe-node` 暴露 `doc_code_mismatches` |
| **后台守护进程** | 长驻进程监视源文件（inotify/polling）并以事务自动更新图谱——`daemon-start`/`stop`/`status`/`force-refresh`/`pause`/`resume`/`wait-sync`/`logs`/`reload`/`list-projects`。Unix socket API：`/tmp/code2database-daemon-<project>.sock` |

---

## 核心特性

| 特性 | 说明 |
|------|------|
| **多语言 AST 扫描** | C/C++、Go、Python、Java、Rust、ASM（tree-sitter / 正则） |
| **条件感知链** | `if`/`switch`/`#ifdef` 分支 + 空节点聚合 + 条件编译标注 |
| **并发建模** | 线程创建、goroutine、回调检测；数据竞争检测；并发安全分析 |
| **边置信度** | EXTRACTED(1.0) / INFERRED(0.7-0.95) / AMBIGUOUS(0.1-0.3) + 来源审计 |
| **一键探索** | `explore-flow` — 单次查询获取相关节点、路径和条件 |
| **增量更新** | `quick-update` 无需 LLM；`light-scan`/`patch-from-git` 零 token 更新 |
| **MCP 服务器** | `serve` — 通过 stdio 暴露 53 个查询工具（34 code2database_* + 19 cgdb_*）供 LLM 代理使用 |
| **知识管理** | `extract-knowledge`/`knowledge-query` — 原则性不变知识存储 |
| **记忆系统** | `save-memory`/`search-memory`/`manage-memory` — 持久 Q&A 记忆 + 衰减 |
| **影响半径** | `blast-radius` — 变更影响的函数/API/测试 |
| **数据竞争检测** | `detect-races`/`concurrency-analyze` — 跨线程竞争和死锁检测 |
| **崩溃反向追踪** | `reverse-trace` — 从崩溃点反向追踪所有可达路径 |
| **字段级访问** | `field-access` — 查询谁读写了特定结构体字段或全局变量 |
| **参数流** | `param-flow` — 追踪一个值如何跨函数边界流动 |
| **外部代码分离** | 自动识别第三方/vendor 代码——让图谱只关于*你的*代码 |
| **双语文档** | 英文(`docs/en/`)和中文(`docs/zh/`)skill 指令与参考文档 |
| **提交级来源** | 每个节点/边的 `commit_meta.source_commit`；用 `git show <hash>` 校验 |
| **Cypher 子集查询** | `query` — MATCH/WHERE/RETURN 声明式图谱查询 |
| **值流 + DATA_FLOW** | `value-flow` — 跨函数参数→返回值传播 |
| **锁覆盖** | `lock-coverage` — 基于事件流 + 字符位置的精确锁持有区间 |
| **路径可行性** | `path-feasible` — Z3 SMT 求解器，可靠的路径可行性 |
| **数据依赖** | `data-dep` — 跨函数 DATA_DEP 边；扫描所有节点的读写者 |
| **不变量** | `extract-invariants`/`find-invariants`/`apply-invariants` — 前置/后置/循环不变量/状态机 |
| **自动增强** | `auto-enhance`/`batch-confirm`/`rollback` — 置信度阈值自动写入 |
| **事务** | `tx-begin`/`commit`/`rollback` — WAL + 快照 + fcntl 锁 |
| **FFI 追踪** | `ffi-detect`/`list`/`trace`/`types` — Python ctypes / Go cgo / Rust extern "C" |
| **Web UI** | `web-ui` — 单文件 HTML/cytoscape.js/JS 交互式浏览器 |
| **BUG 基准** | `bug-benchmark` — GraphInvestigator vs GrepInvestigator 召回/精度 |
| **Profile 健康度** | `profile-health`/`evolve`/`bind-version` — 0-100 评分 + 自动演化 |
| **文档-代码对齐** | `doc-code-check`/`mark-stale`/`alignment-report`/`signature-diff` — 检测文档-代码不匹配 |
| **后台守护进程** | `daemon-start`/`stop`/`status`/... — inotify + Unix socket + 事务性同步 |

---

## 工作原理

```
┌──────────────────────────────────────────────────────────────────────────┐
│                       源文件 (C/C++/Go/Python/Java/Rust/ASM)              │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  [Profile]   项目特定配置：skip 名、回调、vtable、                          │
│              域规则、锁模式、框架约定                                       │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  [Scan]      tree-sitter AST 提取 → extraction.json                       │
│              产生不可变事实：函数、参数、body_text、                        │
│              被调用者、条件、#ifdef、字段读/写                              │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  [Build]     推理 + 图构建                                                 │
│              vtable 派发 · 回调桥接 · 域分类 ·                             │
│              社区检测 · 竞争检测 · 边置信度 ·                              │
│              不变量 · FFI 检测 · 文档-代码对齐                              │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  [Daemon]    （可选）inotify/polling → 去抖动 → 批处理                     │
│              → 事务性同步 → 输出文件重建                                   │
│              断路器：>1000 事件/分钟 → 整体重建                            │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  [Query]     micro → lite → local · 53 个 MCP 工具 · 200 CLI 命令         │
│              explore-flow · trace-chain · detect-races · param-flow      │
│              value-flow · lock-coverage · path-feasible · data-dep       │
│              extract-invariants · ffi-trace · doc-code-check · query     │
│              field-access · reverse-trace · blast-radius · daemon-*      │
└──────────────────────────────────────────────────────────────────────────┘
```

### 为什么是三个阶段？

| 问题 | 单体方案 | 三阶段方案 |
|------|---------|-----------|
| 切换项目 | 修改硬编码规则 | 只需替换 Profile JSON |
| 提取 Bug | 无法判断是扫描还是构建的问题 | Scan 阶段可独立验证 |
| 改进推理 | 必须重新扫描所有内容 | 只需重新运行 Build（秒级，非分钟级） |
| 新语言支持 | 重写全部 | 只需编写新的 Scanner |

---

## 能力矩阵

| 能力 | 给你带来什么 |
|------|------------|
| **语言** | 7 种 — C/C++、Go、Python、Java、Rust、ASM（Python + tree-sitter + ASM 正则）|
| **存储** | JSON 输出 + 大图可选 SQLite 后端 |
| **MCP 服务器** | stdio 传输，**53 个查询工具**（34 code2database_* + 19 cgdb_*）供 LLM 代理使用 |
| **CLI 命令** | **200 命令** 分为 3 个子技能（`/Code2Database` 核心、`/Code2Database-analysis`、`/Code2Database-ops`）— Build、Query、Trace、Concurrency、Knowledge、Memory、Provenance、Cypher、Data Flow、Lock Analysis、Path Feasibility、Invariants、Auto-Enhance、Transactions、FFI、Web UI、Benchmark、Profile Health、Doc-Code、Daemon、cgdb（clang 后端） |
| **调用条件解析** | `if`/`switch`/`#ifdef` 分支 + 空节点聚合 |
| **条件编译（`#ifdef`）** | 图谱知道哪些调用只在哪些 `CONFIG_*` 标志下存在 |
| **数据竞争检测** | 跨线程隐患检测 — `detect-races` |
| **并发安全分析** | 这两条链真的能并行执行吗？— `concurrency-analyze` |
| **字段级访问追踪** | 跨整张图查询"谁读/写 `task_struct->pid`"— `field-access` |
| **崩溃反向追踪** | 从入口点到崩溃点的所有路径 — `reverse-trace` |
| **执行场景** | 从 API 入口到叶子的端到端执行流追踪 |
| **构建系统宏解析** | `compile_commands.json` + `#ifdef` 宏展开 |
| **LLM 上下文包** | 4 级 — `micro`（~200 tokens）→ `lite` → `std` → `full` |
| **影响半径分析** | 修改这个函数会影响什么？— `blast-radius` |
| **外部代码分离** | 自动识别 vendor/第三方代码——图谱只关于*你的*代码 |
| **参数流追踪** | 一个值如何跨函数边界流动 — `param-flow` |
| **提交级来源** | 每个节点/边都有 `source_commit`（git/svn 哈希）— `describe-commit`、`node-history`、`graph-provenance`、`blame-node`、`find-commits` |
| **Cypher 子集查询** | 声明式图谱查询 — `query "MATCH ... WHERE ... RETURN ..."` |
| **值流** | 参数→返回值传播、DATA_FLOW 边 — `value-flow` |
| **锁覆盖** | 基于事件流 + 字符位置的精确锁持有区间 — `lock-coverage` |
| **路径可行性** | Z3 SMT 求解器，可靠路径可行性 — `path-feasible` |
| **数据依赖** | 跨函数 DATA_DEP 边 — `data-dep` |
| **不变量提取** | 前置/后置/循环不变量/状态机 — `extract-invariants` |
| **LLM 自动增强** | 置信度阈值自动写入 + 批量确认 + 回滚 — `auto-enhance` |
| **事务性更新** | WAL + 快照 + fcntl 锁的原子写入 — `tx-begin`/`commit`/`rollback` |
| **FFI 追踪** | Python ctypes / Go cgo / Rust extern "C" — `ffi-detect`/`list`/`trace`/`types` |
| **交互式 Web UI** | 单文件 HTML/cytoscape.js/JS 浏览器 — `web-ui` |
| **BUG 基准测试** | GraphInvestigator vs GrepInvestigator 召回/精度 — `bug-benchmark` |
| **Profile 健康度** | 0-100 评分 + 自动演化 + git/svn HEAD 绑定 — `profile-health`/`evolve`/`bind-version` |
| **文档-代码对齐** | 检测文档-代码不匹配；`describe-node` 暴露 — `doc-code-check` |
| **后台守护进程** | 实时文件监视 + 事务性自动同步 — `daemon-start` |

> **设计意图：** Code2Database 不是"更多语言"或"更多工具"——而是*更深的程序语义*。条件、并发、字段访问、`#ifdef`、崩溃追踪。图谱捕获的是工程师真正需要推理的东西，不只是容易提取的东西。

---

## 命令参考

### 构建与更新

| 命令 | 说明 |
|------|------|
| `build` | 从 extraction JSON 构建调用图 |
| `update` | 增量更新（重扫描变更文件） |
| `sync` | 合并本地 + git 追踪的调用图 |
| `quick-update` | 一键补丁 + 轻量扫描（无需 LLM） |
| `auto-profile` | 自动检测项目类型并生成 profile |
| `extract-signals` | 提取 `#ifdef` 条件信号映射 |
| `install-hook` | 安装 git post-commit 钩子自动更新 |

### 查询与探索

| 命令 | 说明 |
|------|------|
| `explore-flow` | 一键按查询检索上下文 |
| `describe-node` | 节点信息（brief/standard/full） |
| `get-code-snippet` | 提取节点周围的源码 |
| `search` | 节点关键词搜索 |
| `key-paths` | 自动提取关键路径 |

### 追踪与分析

| 命令 | 说明 |
|------|------|
| `trace-chain` | 一键从 A 到 B 的带注释路径 |
| `resolve-chain` | 带变量绑定的链追踪 |
| `reverse-trace` | 反向追踪所有到达崩溃点的路径 |
| `diff-chains` | 对比两种绑定下的路径 |
| `blast-radius` | 影响分析（受影响的 API/测试/域） |

### 并发与数据

| 命令 | 说明 |
|------|------|
| `concurrency-risks` | 列出所有并发风险点 |
| `concurrency-analyze` | 分析两个调用链并发执行是否安全 |
| `detect-races` | 检测跨线程数据竞争 |
| `data-lifecycle` | 追踪资源分配-使用-释放 |
| `field-access` | 查询读写特定结构体字段或全局变量的函数 |
| `param-flow` | 跨函数边界追踪参数流 |

### 知识与记忆

| 命令 | 说明 |
|------|------|
| `extract-knowledge` | 从代码推断提取原则性知识 |
| `knowledge-query` | 按主题查询知识 |
| `knowledge-validate` | 对照图谱验证知识（含文档-代码对齐检查） |
| `save-memory` | 保存 Q&A 记忆 |
| `search-memory` | 搜索记忆（带衰减） |
| `manage-memory` | 记忆 CRUD 与衰减（**需用户确认**） |

### 提交来源与历史

| 命令 | 说明 |
|------|------|
| `describe-commit` | 显示某次提交引入的所有变更 |
| `node-history` | 显示节点的提交历史 |
| `graph-provenance` | 显示图级别的来源摘要 |
| `blame-node` | 定位引入某节点的提交 |
| `find-commits` | 查找触及某函数/文件的提交 |

### 查询语言与数据流

| 命令 | 说明 |
|------|------|
| `query` | Cypher 子集查询（MATCH/WHERE/RETURN） |
| `value-flow` | 跨函数追踪参数→返回值传播（DATA_FLOW 边） |
| `param-flow` | 通过调用链追踪参数流 |
| `data-dep` | 跨函数数据依赖（DATA_DEP 边；扫描所有节点） |

### 锁分析与路径可行性

| 命令 | 说明 |
|------|------|
| `lock-coverage` | 基于事件流 + 字符位置的锁持有区域分析 |
| `path-feasible` | Z3 SMT 求解路径可行性（无 Z3 时启发式回退） |

### 不变量与自动增强

| 命令 | 说明 |
|------|------|
| `extract-invariants` | 提取前置/后置/循环不变量/状态机 |
| `find-invariants` | 按模式查找不变量 |
| `apply-invariants` | 应用提取的不变量到图 |
| `auto-enhance` | LLM 自动语义增强（置信度阈值自动写入） |
| `batch-confirm` | 批量确认待处理增强 |
| `rollback` | 回滚已应用的增强 |
| `fill-request` | 列出 LLM 应填充的字段 |

### 事务

| 命令 | 说明 |
|------|------|
| `tx-begin` | 开启事务（快照 + WAL） |
| `tx-commit` | 提交当前事务 |
| `tx-rollback` | 回滚当前事务（恢复快照） |
| `tx-status` | 查看事务状态 |
| `tx-snapshot` | 创建命名快照 |
| `tx-restore` | 从命名快照恢复 |
| `tx-list-snapshots` | 列出所有快照 |
| `tx-replay-wal` | 重放 WAL 条目（崩溃恢复） |

### 跨语言 FFI

| 命令 | 说明 |
|------|------|
| `ffi-detect` | 检测 FFI 绑定（Python ctypes / Go cgo / Rust extern "C"） |
| `ffi-list` | 列出所有 FFI 绑定位置 |
| `ffi-trace` | 追踪跨语言调用链 |
| `ffi-types` | 显示某条 FFI 边的类型映射 |

### Web UI 与基准测试

| 命令 | 说明 |
|------|------|
| `web-ui` | 启动交互式 Web UI 服务器（默认端口 8765） |
| `bug-benchmark` | 运行 GraphInvestigator vs GrepInvestigator 基准测试 |

### Profile 健康度与文档-代码对齐

| 命令 | 说明 |
|------|------|
| `profile-health` | 计算 7 类 0-100 健康度评分 |
| `profile-evolve` | 检测新回调模式；可选应用 EXTRACTED 建议 |
| `profile-bind-version` | 绑定 profile 到当前 git/svn HEAD 提交 |
| `doc-code-check` | 检查文档-代码对齐；检测返回值/参数/签名不匹配 |
| `doc-mark-stale` | 标记某节点文档为陈旧（如代码变更后） |
| `doc-alignment-report` | 生成完整 Markdown 文档-代码对齐报告 |
| `doc-signature-diff` | 检测两个图版本之间的签名变更 |

### 守护进程

| 命令 | 说明 |
|------|------|
| `daemon-start` | 启动后台守护进程（前台运行；阻塞） |
| `daemon-stop` | 停止运行中的守护进程 |
| `daemon-status` | 获取守护进程状态（pid、last_sync、待处理事件、陈旧节点） |
| `daemon-force-refresh` | 强制重新扫描某文件 |
| `daemon-pause` | 暂停守护进程（如手动更新前） |
| `daemon-resume` | 暂停后恢复守护进程 |
| `daemon-wait-sync` | 阻塞至当前同步完成（LLM 代理在查询前调用） |
| `daemon-logs` | 查看守护进程日志（`--follow` 流式输出） |
| `daemon-reload` | 重载守护进程配置（重新读取 profile） |
| `daemon-list-projects` | 列出所有有守护进程状态文件的项目 |

### 服务

| 命令 | 说明 |
|------|------|
| `serve` | 启动 MCP 服务器（stdio 传输，53 工具：34 code2database_* + 19 cgdb_*） |

所有查询命令支持 `--json` 结构化输出和 `--max-tokens` 预算控制。

---

## MCP 服务器

启动 Code2Database 为 MCP 服务器，让任何 MCP 兼容代理（Claude Code、Codex 等）实时访问代码图谱：

```bash
python3 scripts/code2database_builder.py serve --graph code2db-out/
```

通过 stdio 传输暴露 **53 个 MCP 工具**：31 个 `code2database_*` 工具（包括 `explore-flow`、`trace-chain`、`describe-node`、`detect-races`、`param-flow`、`field-access`、`reverse-trace`、`blast-radius`）+ 19 个 `cgdb_*` 工具（启用 clang 提取后端时直接查询 cgdb 层——类型化 vtable 派发、CFG、数据流、同步原语、配置谓词、时间旅行版本）。代理可以直接查询图谱，无需重读源文件——一次工具调用获得精准上下文。

---

## 提取后端（双 clang + tree-sitter）

Code2Database 对 C/C++ 提取支持**双后端**。在扫描时选择后端；选择会记录在 extraction JSON 中，并被 `build` 复用。

| 后端 | 何时使用 | 依赖 | 启用 |
|------|---------|------|------|
| `auto`（默认）| 多数用户 | tree-sitter（必装）+ libclang（可选）| 始终启用 tree-sitter；libclang 存在时启用 cgdb 层 |
| `clang` | 需要深度语义分析的 C/C++ 项目（类型化 vtable 派发、CFG、数据流、同步原语、配置谓词）| libclang 17+（`pip install libclang==17.0.6`）| 完整 cgdb 层 + 19 个 `cgdb_*` MCP 工具 |
| `tree-sitter` | 精简安装，无 libclang 依赖，纯 AST 提取 | 仅 tree-sitter 语言绑定 | 标准调用图——无 cgdb 层 |

**libclang 是推荐项，非必装。** Tree-sitter-only 模式完全可用——所有 7 种语言都可扫描、构建、查询。Clang 后端额外填充 cgdb（代码图谱数据库）层。

```bash
# 仅 tree-sitter（无 libclang）——支持全部 7 种语言
python3 scripts/code2database_scanner.py scan --source /path --extraction-backend tree-sitter

# Auto（可用时用 clang，否则回退 tree-sitter）
python3 scripts/code2database_scanner.py scan --source /path --extraction-backend auto

# 强制 clang 后端——为 C/C++ 启用 cgdb 层
python3 scripts/code2database_scanner.py scan --source /path --extraction-backend clang
```

### cgdb（代码图谱数据库）层

启用 clang 后端时，构建步骤在传统 `functions`/`edges` 表之外额外填充 13 张语义表：

| 层 | 表 | 内容 |
|----|----|----|
| L1 | `cgdb_nodes` | AST 节点（函数、类型、变量、字段）含源码范围 |
| L2 | `cgdb_types` | 类型定义（struct、union、enum、typedef）|
| L3.5 | `cgdb_predicates` | 配置谓词（`#ifdef CONFIG_*`）含源码范围 |
| L4 | `cgdb_basic_blocks` + `cgdb_cfg_edges` | 每函数的控制流图 |
| L5 | `cgdb_data_flow` | 每函数的 def-use 链 |
| L6 | `cgdb_aliases` | 别名分析（MVP 阶段为 stub）|
| L7 | `cgdb_ops_bindings` | 类型化 vtable 派发（FieldDecl → FunctionDecl）|
| L8 | `cgdb_sync_primitives` + `cgdb_happens_before` | 同步原语 + happens-before |
| L10 | `cgdb_versions` | 时间旅行版本查询 |

这些表通过 19 个 `cgdb_*` MCP 工具直接查询——完整工具参考见 `/Code2Database-analysis` 子技能。

---

## 技能激活（3 个子技能）

技能分为 3 个子技能以保持 LLM 上下文精简。每个子技能有自己的 `SKILL.md`，仅暴露与其层相关的命令。CLI（`scripts/code2database_builder.py`）共享——无论哪个子技能激活，全部 200 个命令都可访问。

| 子技能 | 触发 | 用途 |
|--------|------|------|
| `Code2Database`（核心）| `/Code2Database` | 构建 + 浏览——始终加载。15 个 Tier-1 高权重命令（scan、build、explore-flow、describe-node、trace-chain 等）|
| `Code2Database-analysis` | `/Code2Database-analysis` | 深度语义分析——并发、数据流、不变量、FFI、来源、路径可行性、cgdb 表。13 个 Tier-1 命令 + 19 个 `cgdb_*` MCP 工具 |
| `Code2Database-ops` | `/Code2Database-ops` | 图谱编辑 + 运维——事务、守护进程、profile/文档-代码、导出、插件、记忆、embeddings。14 个 Tier-1 命令 |

当核心技能检测到深度分析或运维问题时，会显式移交给相应子技能。MCP 服务器（53 个工具）与技能激活分离——无论哪个子技能激活，全部 53 个工具都可访问。

---

## 守护进程模式

实时自动同步可启动后台守护进程。它通过 inotify（Linux，ctypes，零依赖）或 polling（跨平台回退）监视源文件，对编辑器保存去抖动，批处理变更，并在事务中运行增量扫描。

```bash
# 启动（前台；阻塞）
python3 scripts/code2database_builder.py daemon-start --graph code2db-out/ --source /path/to/project

# 另一个终端——查询状态、暂停手动更新、恢复
python3 scripts/code2database_builder.py daemon-status --graph code2db-out/
python3 scripts/code2database_builder.py daemon-pause --graph code2db-out/ --reason "manual update"
python3 scripts/code2database_builder.py daemon-resume --graph code2db-out/

# 强制刷新某文件
python3 scripts/code2database_builder.py daemon-force-refresh --graph code2db-out/ --path src/foo.c

# 停止
python3 scripts/code2database_builder.py daemon-stop --graph code2db-out/
```

**LLM 代理协议**——重要查询前检查新鲜度：

```bash
# 检查图谱是否最新
daemon-status --graph code2db-out/
# 若 syncing 或有待处理事件，阻塞至同步完成
daemon-wait-sync --graph code2db-out/ --timeout 30
```

守护进程将状态写入 `<graph_dir>/.daemon_status.json`，日志写入 `~/.code2database/daemon-<project>.log`。通过 profile 的 `daemon` 部分配置（见 `docs/<lang>/RUNTIME_CONFIG.md`）。

**断路器**：若事件超过 1000/分钟（如大型分支的 `git checkout`），守护进程切换为"等待 + 整体重建"模式，而非逐文件增量。

---

## 架构：Profile → Scan → Build → Daemon

```
源文件 → [Profile 配置] → [Scan] → extraction.json → [Build] → 调用图输出
                                                              ↓
                                                       [Daemon]（可选）
                                                       inotify/polling → 去抖动
                                                       → 事务性同步
                                                       → 输出文件重建
```

- **Profile**：项目特定配置（skip 名、回调模式、vtable 类型、域规则、锁模式、FFI 绑定、守护进程配置）
- **Scan**：AST 提取（tree-sitter）——产生不可变事实
- **Build**：推理 + 图构建——vtable 派发、回调桥接、域分类、社区检测、竞争检测、不变量提取、FFI 检测、文档-代码对齐
- **Daemon**（可选）：长驻进程监视源文件，并以事务自动更新图谱

这种分离让你可以：
- 切换项目只需替换一个 Profile JSON（无需改代码）
- 独立验证 scan 正确性，不受推理质量影响
- 改进推理后秒级重跑 Build（无需重扫）
- 添加新语言只需编写新的 Scanner
- 通过守护进程模式自动保持图谱最新

---

## 文档

| 路径 | 说明 | 受众 |
|------|------|------|
| `docs/zh/SKILL.md` | Skill 指令 — 精简代理指南（约 3K tokens） | AI 代理（自动加载） |
| `docs/en/SKILL.md` | Skill 指令 — 精简代理指南（英文） | AI 代理（自动加载） |
| `docs/zh/references/usage_reference.md` | 详细命令语法、参数、代码块 | 按需参考 |
| `docs/en/references/usage_reference.md` | 详细命令语法、参数、代码块（英文） | 按需参考 |
| `docs/*/references/` | 数据模型、标签规则、端点流水线等 | 按需参考 |
| `docs/zh/OVERVIEW.md` | 内部架构与算法 | 工具开发者 |
| `docs/zh/PROFILE_MANUAL.md` | Profile 编写指南 | 工具开发者 |
| `docs/zh/RUNTIME_CONFIG.md` | 运行时配置参考 | 工具开发者 |
| `CLAUDE.md` | Claude Code 集成指南 | AI 代理（自动加载） |
| `AGENTS.md` | Codex/代理集成指南 | 工具开发者 |

---

## 重要约束

- **禁止预加载** `scripts/config/profiles/` 或 `docs/*/references/` 到上下文——仅按需读取
- **全局→本地查询模式**：始终从 micro/lite 上下文包开始，再逐步下钻
- **仅支持七种标签** (API_entry, thread_processor, callback_func, constructor, destructor, out_end, unknown_end)
- **找到根因之前不提修复方案**
- **sync/update 后必须验证**
- **DB 写入需用户确认**：LLM 发起的 `update-node`/`update-edge`/`patch-profile`/`apply-semantics`/`apply-invariants`/`auto-enhance`（EXTRACTED+证据可绕过；INFERRED 需确认）/`profile-evolve --apply`（仅 EXTRACTED）/`doc-mark-stale` 必须提示用户确认，防止幻觉数据污染图谱
- **事务性写入**：多步 DB 修改应包裹 `tx-begin`/`tx-commit`，失败时原子回滚；`patch-from-diff`/`patch-from-git` 默认已包裹
- **守护进程新鲜度**：重要查询前调用 `daemon-status`；若 `syncing` 或有待处理事件，调用 `daemon-wait-sync` 阻塞至同步完成
- **文档-代码对齐**：若 `describe-node` 返回非空 `doc_code_mismatches`，文档（`semantic_desc`）可能不可靠——查阅 `body_text` 并考虑 `doc-mark-stale` 直到文档重新提取
- **提交来源**：节点/边的 `commit_meta.source_commit` 是 git/svn 哈希——工程师用 `git show <hash>` 校验，而非时间戳

---

## 许可证

MIT
