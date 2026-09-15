# 故障排查

以症状为先的排查指南，覆盖部署实际会遇到的失败模式。每一节都从
你观察到的现象出发，再给出回答该现象的命令。

## 第一站：`doctor` 探针

一条命令检查 SQLite 完整性与外键、schema 版本、内容计数、源码
新鲜度、记忆库、知识 brief 与守护进程状态：

```bash
code2database-builder doctor --graph code2db-out/ --json
echo $?   # 0 = 健康，1 = 有告警，2 = 失败
```

| 检查项 | 报告内容 |
|--------|----------|
| `database` | `PRAGMA integrity_check` + 外键违规 |
| `schema` | 库内 schema 版本与工具当前版本比对 |
| `graph_content` | 函数/边/文件计数 |
| `freshness` | 上次扫描以来源码漂移（新增/变更/删除） |
| `memory_store` | memory.db 可达性与条目计数 |
| `knowledge_brief` | brief.json 是否存在（缺失 = 告警） |
| `daemon` | 同步守护进程是否在运行 |

退出码可直接用于部署冒烟探针：非 0 就该叫人来看；2 表示数据级
失败（完整性、数据库缺失）。

## 图谱提示源码已过期

现象：`session-init` 或 `c2d freshen` 报告文件漂移。

- 运行 `c2d freshen --graph code2db-out/`——它打印推荐的同步路径
  （按文件同步、增量重扫或完整重建），退出码语义适合脚本消费
- 新鲜度用文件指纹（mtime + size）对比 manifest，并用 git HEAD
  对比记录的源码 commit——仅 `touch` 过的文件可能显得过期；
  内容哈希（clang 后端）是更强的信号
- 若守护进程在跑，先看 `daemon-status`：启动宽限窗口内守护进程
  只持有事件而不同步

## 数据库损坏或不可读

现象：`doctor` 的 `database` 项失败；查询报错。

```bash
code2database-builder tx-list-snapshots --graph code2db-out/
code2database-builder tx-restore --graph code2db-out/ --snapshot <id>
```

- 事务快照在每次多步写入（`tx-begin`/`patch-from-diff`/…）前捕获
  数据库与关键 JSON；最新的健康快照通常只落后几分钟
- WAL 日志会在下一次连接时自动恢复；同步中硬断电留下的是事务前
  状态
- 最后手段：从源码重建（`make`/`c2d setup`）——记忆库与 brief
  位于图谱数据库之外，重建不会动它们

## 同步守护进程无响应

现象：`daemon-status` 连不上。

- 控制套接字为 `$TMPDIR/code2database-daemon-<哈希>.sock`（图谱
  目录绝对路径的哈希）；确认两侧算的是同一个 `--graph` 路径——
  从不同工作目录给的相对路径会哈希出不同结果
- systemd 下 `PrivateTmp=true` 会把套接字对用户 shell 隐藏——
  随附的 `deploy/c2d-daemon.service` 正因此固定 `TMPDIR=/tmp` 且
  `PrivateTmp=false`
- 崩溃后残留的套接字文件会在下次启动时清理；`daemon-logs` 给出
  崩溃上下文，`.daemon_status.json` 是最后记录的状态
- 停机期间堆积的事件：守护进程会把恢复性批量同步推迟到启动宽限
  窗口之后——看 `daemon-status`，不要急于强制重建

## cgdb 层缺失或降级

现象：`cgdb-*` 查询返回空；`doctor` 未报告异常。

- 图谱目录里的标记文件 `.code2database_cgdb_export_failed.json`
  记录了失败的 cgdb 导出——处理完记录的阶段后删除它并重跑构建
- cgdb 层要求 clang 提取后端（`--extraction-backend clang`，或装了
  libclang 的 `auto`）；纯 tree-sitter 构建功能完整但没有 cgdb 表
- `pip install "code2database[clang]"` 提供 libclang

## 记忆与 brief 和图谱对不上

现象：回答引用了已不存在的函数。

- `node_ids` 从图谱消失的记忆条目会被自动降级（守护进程每次同步
  后重新校验）；用 `manage-memory --action query` 查看被降级条目，
  再用 `save-memory --correct` 重新锚定
- brief 统计与图谱漂移超过 20% 时 `brief-validate` 告警，brief 超
  出预算时也告警（溢出内容应放进记忆）
- 反复落空的疑问会以 known-unknowns 出现在 `session-init` 里——用
  `c2d capture` 把答案沉淀下来，而不是留着不答

## MCP 或 Web UI 拒绝启动

现象：`serve` 或 `web-ui` 立即退出。

- 无 `--token` 绑定公网接口被设计性拒绝——设置 `--token` 或
  `C2D_MCP_TOKEN`
- 端口占用：`--port` 冲突会体现在启动错误里；查
  `ss -ltnp | grep 8765`
- `--read-only` 依设计隐藏写工具——客户端抱怨缺工具时，先确认它
  连的是只读部署
- HTTP 健康端点（`/health`）无需鉴权，适合作为编排器的存活探针

## 扫描器在某语言上失败或内存不足

现象：扫描中止，或某语言的函数始终不出现。

- 缺少该语言的 tree-sitter 语法包会让该语言静默为空——安装所扫
  语言的语法包
- 奇异代码树没有 profile：`auto-profile` 会推导一个；持续不中应
  手写 profile（`docs/zh/PROFILE_MANUAL.md`）
- MemoryGuard 默认按系统内存封顶扫描——在繁忙机器或 CI 里传入
  `--memory-limit`（MB）并调高 `--memory-warn-threshold` /
  `--memory-crit-threshold`

## 选择重同步路径

| 场景 | 命令 |
|------|------|
| 少量文件变更，图谱为 SQLite 后端 | `build-update`（按文件、事务化） |
| 持续编辑，希望自动化 | `daemon-start` |
| 切分支或大规模结构性变动 | 完整 `build`（或 `make`） |
| 只有派生产物（pack、摘要）过期 | 重跑导出/make 派生步骤 |

拿不准时，`c2d freshen` 会针对当前漂移点名推荐路径——它的存在
就是为了让这个决策可脚本化。
