# 部署指南

如何把 Code2Database 作为稳定的服务级组件运行：安装路径、MCP 服务部署、
同步守护进程、备份恢复与健康监测。

## 部署模式

| 模式 | 命令面 | 适用场景 |
|------|--------|----------|
| CLI（交互式） | `c2d`、`code2database-builder`、`code2database-scanner` | 工程师工作站、CI 任务、脚本流水线 |
| MCP over stdio | `serve --graph <目录>` | 本地 LLM 代理（Claude Desktop、Cursor 本地模式） |
| MCP over HTTP | `serve --transport http ...` | TLS + 令牌鉴权后的共享/团队访问 |
| 同步守护进程 | `daemon-start --graph <目录> --source <源码>` | 工程师持续改代码时保持图谱最新 |
| Web UI | `web-ui --graph <目录>` | 交互式浏览已构建的图谱 |

## 前置条件

- Python 3.10+（与 `pyproject.toml` 一致）
- 核心安装：`pip install code2database`（或从仓库 `pip install .`）——
  会拉取 `networkx` 与 `tree-sitter` 各语法包
- 可选能力以 wheel extras 形式发布（按需安装）：

| Extra | 提供 |
|-------|------|
| `clang` | cgdb 强类型后端（vtable 分发、CFG、数据流） |
| `solver` | 基于 z3 的可靠路径可行性 |
| `community` | 跨域 Leiden 社区检测 |
| `daemon` | macOS/Windows 的文件监听（Linux 用内置 inotify） |
| `resources` | 跨平台内存遥测 |
| `streaming` | 超大 globals.json 的流式解析 |
| `neural` | 可选的神经嵌入提供方 |

```bash
# 示例
pip install code2database                     # 核心
pip install "code2database[clang,solver]"     # 强类型后端 + 求解器
pip install "code2database[daemon,resources]" # 宿主机式部署
```

## 安装路径与入口

wheel 安装三个控制台入口（同一套代码）：

| 入口 | 角色 |
|------|------|
| `c2d` | 总入口生命周期：setup → session → ask → capture（另含 freshen/report） |
| `code2database-builder` | 完整的 253 命令 builder CLI |
| `code2database-scanner` | 8 个 scanner 动词 |

仓库检出同样可以直接运行（在仓库根目录执行
`python3 scripts/code2database_builder.py ...`，无需设置 `PYTHONPATH`）。

## 每个项目的首次构建

```bash
c2d setup --source /path/to/project --graph code2db-out/
```

`setup` 委托给 `make`：环境检查 → 扫描 → 构建 → 派生产物 → 导出。
图谱目录（`code2db-out/`）自包含：一个 SQLite 数据库、记忆库、知识
brief、索引与历史——全部在其中。

## MCP 服务部署

### stdio（本地代理）

把代理的 MCP 配置指向 builder 的 `serve` 与图谱目录即可，不开放任何
网络面。

### HTTP（共享访问）

```bash
code2database-builder serve --graph /opt/Code2Database/code2db-out \
    --transport http --host 127.0.0.1 --port 8765 \
    --token <强密钥> --read-only --max-clients 32
```

- 令牌鉴权：`--token` 或 `C2D_MCP_TOKEN`；每个请求必须携带
  `Authorization: Bearer <token>`
- `--read-only` 对远端客户端隐藏写工具（记忆保存、数据库事务、
  token 编辑）
- TLS 经 `--tls-cert/--tls-key`，或在 nginx 终止
  （`deploy/mcp-nginx.conf`：限速、SSE 兼容代理）
- 无令牌时服务拒绝绑定公网接口

生产环境用 systemd 运行 `deploy/c2d-mcp.service`（已加固：
`NoNewPrivileges`、`ProtectSystem=strict`、`MemoryMax`），前置提供好的
nginx 配置：

```bash
sudo cp deploy/c2d-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now c2d-mcp
curl http://localhost:8765/health   # 部署冒烟探针
```

## 守护进程服务部署

守护进程监听源码文件并在事务中同步图谱（快照 + WAL + 失败回滚）。
用 systemd 运行 `deploy/c2d-daemon.service`：

```bash
sudo cp deploy/c2d-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now c2d-daemon
```

运维要点：

- 控制套接字为 `$TMPDIR/code2database-daemon-<哈希>.sock`（图谱目录
  绝对路径的哈希）；服务单元固定 `TMPDIR=/tmp` 且保持
  `PrivateTmp=false`，shell 命令才能连上
- 任意 shell 可用的控制面：`daemon-status`、`daemon-pause`、
  `daemon-resume`、`daemon-force-refresh`、`daemon-wait-sync`、
  `daemon-logs`、`daemon-list-projects`
- 状态存于 `<graph_dir>/.daemon_status.json`；重启时检测崩溃残留，
  接续 pending 事件，并把恢复性批量同步推迟到启动宽限窗口之后
  （防崩溃循环）
- 事件速率超过阈值时熔断器切换为"等待 + 批量重建"，不再逐文件同步

## 备份与恢复

图谱目录即备份单元，整体拷贝：

| 路径 | 内容 |
|------|------|
| `code2database.db` | 图谱数据库（函数、边、cgdb 各层、操作留痕、变更行） |
| `graph_versions.db` | 累积的构建历史（每次构建/同步的计数） |
| `memory/memory.db` | 共享记忆库——**最难重建的产物**（积累的老手经验） |
| `knowledge/brief.json` | 人工治理的项目 brief |
| `.code2database_manifest.json` | 源码指纹与源码 commit 锚点 |

```bash
# 冷备份（守护进程已停，或先 daemon-pause）
rsync -a code2db-out/ backup/code2db-out/

# 无外部备份的时点恢复
code2database-builder tx-list-snapshots --graph code2db-out/
code2database-builder tx-restore --graph code2db-out/ --snapshot <id>
```

跨机器迁移：拷贝图谱目录与源码树；manifest 记录了 `source_root`——
源码移动后先跑 `c2d freshen`，再走同步路径。跨项目全局知识库位于
`~/.code2database_global_kb/`——若在项目间共享原理，需单独备份
（`kb-global-share` / `kb-global-import` 在机器间搬运bundle）。

## 健康监测

`doctor` 是一次性探针——为部署流水线与监控 cron 设计：

```bash
code2database-builder doctor --graph code2db-out/ --json
echo $?   # 0 = 健康，1 = 有告警，2 = 失败
```

它检查 SQLite 完整性与外键、schema 版本、内容计数、源码新鲜度、
记忆库、知识 brief 与守护进程状态。时间维度上的漂移看
`graph-history`（累积的版本行：每次构建/同步的节点/边计数），
`graph-provenance` 报告当前数据库对应的源码 commit 与工具版本。

## 数据敏感性

图谱输出会镜像被扫描的源码：函数名、文件路径，以及 clang 后端下的
字符串字面量。图谱目录及其备份都应按源码同级敏感度对待。完整策略见
[SECURITY.md](../../SECURITY.md)。
