# 运维命令参考（Tier 3）

本文档覆盖 `/Code2Database-ops` 子技能暴露的所有运维命令的完整语法：事务、守护进程、profile/文档-代码、图谱版本、记忆、导出、插件、embeddings。**按需读取**——除非需要某命令的详细语法，否则不要加载到 agent 上下文。

**数据库写入约束**：标记 **[write]** 的命令在执行前需用户确认。完整报告-等待协议见 `SKILL_ops.md` 数据库写入约束节。

## 事务

### `tx-begin`

开启事务。创建当前图状态的快照并启动 Write-Ahead Log（WAL）。

```bash
python3 scripts/code2database_builder.py tx-begin \
  --graph code2db-out/ \
  [--label "描述性标签"]
```

### `tx-commit`  [write]

提交当前事务。快照 + WAL 条目应用到活动数据库。写入**需用户确认**。

```bash
python3 scripts/code2database_builder.py tx-commit \
  --graph code2db-out/ \
  [--yes]  # 不推荐——绕过确认
```

### `tx-rollback`

回滚当前事务。恢复 `tx-begin` 时取的快照。

```bash
python3 scripts/code2database_builder.py tx-rollback \
  --graph code2db-out/
```

### `tx-status`

显示当前事务状态（活动 / 已提交 / 已回滚、快照路径、WAL 条目数）。

```bash
python3 scripts/code2database_builder.py tx-status \
  --graph code2db-out/
```

### `tx-snapshot`

创建当前图状态的命名快照（不开启事务）。

```bash
python3 scripts/code2database_builder.py tx-snapshot \
  --graph code2db-out/ \
  --name SNAPSHOT_NAME
```

### `tx-restore`

从命名快照恢复图。**需用户确认**（覆盖当前状态）。

```bash
python3 scripts/code2database_builder.py tx-restore \
  --graph code2db-out/ \
  --name SNAPSHOT_NAME \
  [--yes]
```

### `tx-list-snapshots`

列出所有命名快照。

```bash
python3 scripts/code2database_builder.py tx-list-snapshots \
  --graph code2db-out/
```

### `tx-replay-wal`

重放 WAL 条目（崩溃恢复）。当之前的事务被中断时使用。

```bash
python3 scripts/code2database_builder.py tx-replay-wal \
  --graph code2db-out/ \
  [--dry-run]
```

## 图谱编辑（需用户确认）

### `update-node`  [write]

LLM 增量补充节点属性。补充信息存储为 `{key}_supplemented` 字段——不覆盖原始扫描数据。

```bash
python3 scripts/code2database_builder.py update-node \
  --graph code2db-out/ \
  --function function_name \
  --field semantic_desc \
  --value "读取共享状态 Y 前先获取互斥锁 X" \
  --confidence INFERRED \
  --source "LLM 从 body_text + lock-coverage 推断" \
  [--yes]
```

### `update-edge`  [write]

LLM 增量补充边属性。

```bash
python3 scripts/code2database_builder.py update-edge \
  --graph code2db-out/ \
  --edge EDGE_ID \
  --field call_condition \
  --value "if (flag == 1)" \
  --confidence EXTRACTED \
  --source "AST" \
  [--yes]
```

### `patch-profile`  [write]

LLM 增量校准 auto-profile。非破坏性，带备份。

```bash
python3 scripts/code2database_builder.py patch-profile \
  --graph code2db-out/ \
  --field callback_detection \
  --value '...' \
  [--yes]
```

### `classify-endpoints`  [write]

应用 LLM 端点分类结果到图。

```bash
python3 scripts/code2database_builder.py classify-endpoints \
  --graph code2db-out/ \
  --function function_name \
  --label out_end \
  --external-desc "HTTP API: POST /users" \
  [--yes]
```

### `auto-enhance`  [write]

LLM 自动语义增强。EXTRACTED+证据自动写入；INFERRED 需用户确认；AMBIGUOUS 拒绝。

```bash
python3 scripts/code2database_builder.py auto-enhance \
  --graph code2db-out/ \
  [--scope function_name] \
  [--confidence-threshold EXTRACTED] \
  [--dry-run]  # 预览不写入
  [--yes]  # 不提示直接应用 INFERRED——不推荐
```

### `batch-confirm`  [write]

批量确认待处理的 INFERRED 增强。

```bash
python3 scripts/code2database_builder.py batch-confirm \
  --graph code2db-out/ \
  [--scope function_name] \
  [--yes]
```

### `rollback`

按时间或范围回滚已应用的增强。

```bash
python3 scripts/code2database_builder.py rollback \
  --graph code2db-out/ \
  [--since TIMESTAMP] [--scope function_name] \
  [--yes]
```

### `fill-request`

列出 LLM 应填充的字段（自动填充请求队列）。

```bash
python3 scripts/code2database_builder.py fill-request \
  --graph code2db-out/ \
  [--scope function_name]
```

### `add-semantic-edges`  [write]

向图添加语义边（如 alloc→free 配对、回调注册）。

```bash
python3 scripts/code2database_builder.py add-semantic-edges \
  --graph code2db-out/ \
  --kind alloc_free \
  --src function_a --dst function_b \
  [--yes]
```

### `semantic-status`

查看语义提取状态（多少节点有 semantic_desc、多少待处理）。

```bash
python3 scripts/code2database_builder.py semantic-status \
  --graph code2db-out/
```

### `audit-log`

查看图的历史写入审计日志。

```bash
python3 scripts/code2database_builder.py audit-log \
  --graph code2db-out/ \
  [--since TIMESTAMP] [--scope function_name] \
  [--json]
```

## 守护进程

### `daemon-start`

启动后台守护进程（前台运行；阻塞）。通过 inotify（或轮询回退）监视源文件，并通过事务自动同步变更。

```bash
python3 scripts/code2database_builder.py daemon-start \
  --graph code2db-out/ \
  --source /path/to/project \
  [--polling-interval SEC]
```

### `daemon-stop`

停止运行中的守护进程。

```bash
python3 scripts/code2database_builder.py daemon-stop \
  --graph code2db-out/
```

### `daemon-status`

获取守护进程状态：pid、last_sync、待处理事件、陈旧节点、断路器状态，以及启动宽限期状态（`sync.startup_grace_active`、`sync.startup_grace_remaining_sec`——宽限期内观察到的事件会被持有、不触发同步）。

```bash
python3 scripts/code2database_builder.py daemon-status \
  --graph code2db-out/
```

### `daemon-force-refresh`

强制重新扫描某文件（绕过变更检测）。

```bash
python3 scripts/code2database_builder.py daemon-force-refresh \
  --graph code2db-out/ \
  --path src/foo.c
```

### `daemon-pause`

暂停守护进程（例如手动更新前）。

```bash
python3 scripts/code2database_builder.py daemon-pause \
  --graph code2db-out/ \
  --reason "手动更新"
```

### `daemon-resume`

暂停后恢复守护进程。

```bash
python3 scripts/code2database_builder.py daemon-resume \
  --graph code2db-out/
```

### `daemon-wait-sync`

阻塞至当前同步完成。**重要查询前调用**确保图谱最新。

```bash
python3 scripts/code2database_builder.py daemon-wait-sync \
  --graph code2db-out/ \
  --timeout 30
```

### `daemon-logs`

查看守护进程日志文件。`--follow` 流式输出。

```bash
python3 scripts/code2database_builder.py daemon-logs \
  --graph code2db-out/ \
  [--follow] [--lines N]
```

### `daemon-reload`

重载守护进程配置（重新读取 profile）。

```bash
python3 scripts/code2database_builder.py daemon-reload \
  --graph code2db-out/
```

### `daemon-list-projects`

列出所有有守护进程状态文件的项目。

```bash
python3 scripts/code2database_builder.py daemon-list-projects
```

## 保持图谱新鲜

### `watch`

文件监视器，变更时自动重建（进程内，无守护进程）。

```bash
python3 scripts/code2database_builder.py watch \
  --graph code2db-out/ \
  --source /path/to/project
```

### `sync`

团队合并：本地优先，补充远程。

```bash
python3 scripts/code2database_builder.py sync \
  --graph code2db-out/ \
  --remote /path/to/remote/graph
```

### `merge`

合并多个来源的图。

```bash
python3 scripts/code2database_builder.py merge \
  --graph code2db-out/ \
  --inputs /path/a /path/b /path/c
```

### `light-scan`

仅对变更文件做轻量扫描（比全量扫描快）。

```bash
python3 scripts/code2database_builder.py light-scan \
  --source /path/to/project \
  --output code2db-out/.code2database_extraction.json \
  [--changed-since TIMESTAMP]
```

### `patch-from-diff`  [write]

从 diff 文件补丁图。默认包裹事务；用 `--no-transaction` 绕过。

```bash
python3 scripts/code2database_builder.py patch-from-diff \
  --graph code2db-out/ \
  --diff /path/to/changes.diff \
  [--no-transaction] \
  [--yes]
```

### `patch-from-git`  [write]

基于 `git diff` 自动补丁图。默认包裹事务。

```bash
python3 scripts/code2database_builder.py patch-from-git \
  --graph code2db-out/ \
  [--source /path/to/repo] \
  [--commit HASH] \
  [--no-transaction] \
  [--yes]
```

### `install-hook`

安装 git post-commit 钩子，提交时自动更新。

```bash
python3 scripts/code2database_builder.py install-hook \
  --graph code2db-out/ \
  --source /path/to/repo
```

### `export-changes`

导出变更图谱（两个图状态之间的 diff）。

```bash
python3 scripts/code2database_builder.py export-changes \
  --graph code2db-out/ \
  --from-version v1 --to-version v2 \
  --output /path/to/changes.json
```

### `merge-changes`

将变更图谱合并到当前图。**需用户确认。**

```bash
python3 scripts/code2database_builder.py merge-changes \
  --graph code2db-out/ \
  --changes /path/to/changes.json \
  [--yes]
```

## profile 与文档-代码

### `profile-health`

计算 7 个维度 0-100 健康度评分（回调覆盖、vtable 覆盖、域规则、锁模式、FFI 绑定、守护进程配置、文档-代码对齐）。

```bash
python3 scripts/code2database_builder.py profile-health \
  --graph code2db-out/ \
  [--json]
```

### `profile-evolve`  [write]

检测新回调模式和其他值得 profile 的结构。`--apply` 应用 EXTRACTED 置信度建议；INFERRED 需用户确认。

```bash
python3 scripts/code2database_builder.py profile-evolve \
  --graph code2db-out/ \
  [--apply] \
  [--yes]  # 不提示直接应用 INFERRED——不推荐
  [--json]
```

### `profile-bind-version`

绑定 profile 到当前 git/svn HEAD 提交（记录 commit 哈希，便于之后检测 profile 漂移）。

```bash
python3 scripts/code2database_builder.py profile-bind-version \
  --graph code2db-out/ \
  [--source /path/to/repo]
```

### `doc-code-check`

检查文档-代码对齐；检测返回值 / 参数 / 签名 / 陈旧文档不匹配。

```bash
python3 scripts/code2database_builder.py doc-code-check \
  --graph code2db-out/ \
  [--scope function_name] \
  [--json]
```

### `doc-mark-stale`  [write]

标记某节点文档为陈旧（非破坏性但可见——`describe-node` 会警告用户）。

```bash
python3 scripts/code2database_builder.py doc-mark-stale \
  --graph code2db-out/ \
  --function function_name \
  --reason "签名在 commit abc123 中变更" \
  [--yes]
```

### `doc-alignment-report`

生成完整 Markdown 文档-代码对齐报告。

```bash
python3 scripts/code2database_builder.py doc-alignment-report \
  --graph code2db-out/ \
  --output /path/to/report.md
```

### `doc-signature-diff`

检测两个图版本之间的签名变更。

```bash
python3 scripts/code2database_builder.py doc-signature-diff \
  --graph code2db-out/ \
  --from-version v1 --to-version v2 \
  [--json]
```

## 图谱版本

### `graph-record-version`

记录命名图谱版本（供后续 diff 的快照）。

```bash
python3 scripts/code2database_builder.py graph-record-version \
  --graph code2db-out/ \
  --name v1.2.3 \
  [--notes "Release 1.2.3"]
```

### `graph-history`

显示图谱的版本历史。

```bash
python3 scripts/code2database_builder.py graph-history \
  --graph code2db-out/
```

### `graph-diff`

对比两个图谱版本。

```bash
python3 scripts/code2database_builder.py graph-diff \
  --graph code2db-out/ \
  --from-version v1 --to-version v2 \
  [--json]
```

## 记忆管理

### `save-memory`  [write]

保存 Q&A 到持久记忆。**需用户确认。**

```bash
python3 scripts/code2database_builder.py save-memory \
  --graph code2db-out/ \
  --question "..." --answer "..." \
  --tags tag1,tag2 \
  [--symbol nvme_submit_cmd]... \
  [--yes]
```

`--symbol`（可重复）把记忆锚定到代码符号：Web UI 会在该符号的节点页
展示这条问答，`search-memory --symbol` 可按符号过滤。

### `search-memory`

按查询或标签搜索持久记忆。

```bash
python3 scripts/code2database_builder.py search-memory \
  --graph code2db-out/ \
  --query "..." \
  [--tags tag1,tag2] \
  [--symbol nvme_submit_cmd] \
  [--limit N]
```

### `manage-memory`  [write]

高级记忆 CRUD 和衰减。动作：`add`、`correct`、`reshape`、`promote`、`refine`、`decay`。**需用户确认。**

```bash
python3 scripts/code2database_builder.py manage-memory \
  --graph code2db-out/ \
  --action add \
  --id MEM_ID \
  --content "..." \
  [--yes]
```

### `memory-health`

检查记忆系统健康（计数、衰减状态、覆盖）。

```bash
python3 scripts/code2database_builder.py memory-health \
  --graph code2db-out/
```

### `validate-memory`

验证记忆条目的准确性和新鲜度。

```bash
python3 scripts/code2database_builder.py validate-memory \
  --graph code2db-out/ \
  [--id MEM_ID]
```

## 导出 / 插件 / 基准

### `export-html`

导出图谱到交互式 HTML 可视化。

```bash
python3 scripts/code2database_builder.py export-html \
  --graph code2db-out/ \
  --output /path/to/visualization.html \
  [--scope function_name]
```

### `export-obsidian`

导出图谱到 Obsidian vault（带交叉链接的 markdown 文件）。

```bash
python3 scripts/code2database_builder.py export-obsidian \
  --graph code2db-out/ \
  --output /path/to/vault/
```

### `web-ui`

启动交互式 Web UI 服务器（默认端口 8765）。

```bash
python3 scripts/code2database_builder.py web-ui \
  --graph code2db-out/ \
  --port 8765 \
  [--browser]
```

### `plugins`

列出可用插件。

```bash
python3 scripts/code2database_builder.py plugins
```

### `validate-plugin`

验证插件文件。

```bash
python3 scripts/code2database_builder.py validate-plugin \
  --plugin /path/to/plugin.json
```

### `bug-benchmark`

运行 GraphInvestigator vs GrepInvestigator 基准测试（recall / precision / tool-calls / tokens / time）。

```bash
python3 scripts/code2database_builder.py bug-benchmark \
  --graph code2db-out/ \
  --scenario tests/fixtures/bug_benchmark/ \
  [--json]
```

## Embeddings（实验性）

### `embeddings-build`

为图谱构建语义嵌入（实验性）。

```bash
python3 scripts/code2database_builder.py embeddings-build \
  --graph code2db-out/ \
  [--model all-MiniLM-L6-v2]
```

### `embeddings-search`

使用嵌入在图上做语义搜索。

```bash
python3 scripts/code2database_builder.py embeddings-search \
  --graph code2db-out/ \
  --query "找出获取互斥锁的函数" \
  [--limit N]
```

## MCP 服务器

### `serve`

启动 MCP 服务器（stdio 传输，82 个工具 (54 base + 28 design-report)：35 个 `code2database_*` + 19 个 `cgdb_*`）。

```bash
python3 scripts/code2database_builder.py serve \
  --graph code2db-out/
```

无论子技能是否激活，全部 82 个工具 (54 base + 28 design-report)都可访问。MCP 与技能层激活分离。19 个 `cgdb_*` 工具见 `references/analysis_commands.md`；35 个 `code2database_*` 工具见父技能 `references/usage_reference.md`。

新增 `code2database_kb_query` 工具（Phase 3）是跨越 memory + knowledge
存储的统一 FTS5+BM25 查询接口：

```json
{"tool": "code2database_kb_query",
 "arguments": {"query": "bdev register io_device", "top": 10,
               "kinds": "memory_qa,knowledge_principle"}}
```

## 知识库（kb-* 命令 — Phase 1-11）

### `kb-rebuild-index`

从 `memory/*.json` + `knowledge/*.md` 重建统一 FTS5 索引。
每次 `build` / `update` 后或手动修改 memory/knowledge 后运行。

```bash
python3 scripts/code2database_builder.py kb-rebuild-index \
  --graph code2db-out/
```

### `kb-query`

跨 memory + knowledge 的统一 FTS5+BM25 查询。返回带 `source_kind`、
`score`、`body`、`see_also` 的排序结果。中文查询（unicode61 分词器
不可见）在 FTS5 零结果时回退到 token 集相似度扫描。

```bash
python3 scripts/code2database_builder.py kb-query \
  --graph code2db-out/ \
  --query "bdev 如何注册 io_device" \
  [--top 10] [--kinds memory_qa,knowledge_principle] \
  [--min-weight 0.0] [--max-tokens 4000] \
  [--semantic] [--global]
```

`--semantic`：启用 embedding 语义搜索（需 sentence-transformers；
不可用时自动降级到 FTS5）。
`--global`：项目 KB 无命中时回退到 `~/.code2database_global_kb/global.db`。

### `kb-cluster`

按 FTS5 BM25 > 阈值用 union-find 聚类相似 kb 条目。每 cluster 选
canonical（最高 weight × confidence）并链接
`memory_qa` → `knowledge_principle`（`principle_ref`）。

```bash
python3 scripts/code2database_builder.py kb-cluster \
  --graph code2db-out/ \
  [--threshold 0.5]
```

### `kb-migrate`

把 `kb_paragraphs` 行迁移到 `kb_items`（fact 级 + `versions_json` +
`decay_class` + `provenance_commit`）。两表共存；kb_items 是长期后继。

```bash
python3 scripts/code2database_builder.py kb-migrate \
  --graph code2db-out/
```

### `kb-known-unknowns`

列出未命中的查询（从 `kb_query_log` 聚合）。帮助识别知识缺口。

```bash
python3 scripts/code2database_builder.py kb-known-unknowns \
  --graph code2db-out/ \
  [--top 20] [--min-occurrences 2]
```

### `kb-audit`

审计项目 KB：按 kind 计数、过期条目（>90 天未访问）、低置信度
（<0.5）、高引用（top access_count）、最被链接的 principles。
可选 `--topic` 用于"关于 X 我们知道什么"。

```bash
python3 scripts/code2database_builder.py kb-audit \
  --graph code2db-out/ \
  [--topic "bdev 注册"]
```

### `kb-conflict`

检测同 cluster 内矛盾条目（pairwise 检查 14 个矛盾词对：
yes/no、must/must not、always/never、safe/unsafe 等）。

```bash
python3 scripts/code2database_builder.py kb-conflict \
  --graph code2db-out/
```

### `kb-rollback`

把 `kb_item` 回滚到旧版本（先把当前状态保存为新版本条目，
所以回滚本身可逆）。

```bash
python3 scripts/code2database_builder.py kb-rollback \
  --graph code2db-out/ \
  --id 42 [--to-version 3]
```

### `kb-forget`  [write]

立即删除某条 `kb_paragraph`（不等 decay）。写 audit_log 条目
（操作者、时间戳、原因）以备审计。

```bash
python3 scripts/code2database_builder.py kb-forget \
  --graph code2db-out/ \
  --id 42 \
  --reason "incorrect: bdev_register 不调用 io_device_register"
```

### `kb-global-add` / `kb-global-search` / `kb-global-share` / `kb-global-import`

跨项目全局 KB（`~/.code2database_global_kb/global.db`）。存储项目无关
知识（调试方法论、协议标准、工具用法），可跨所有项目复用。

```bash
# 添加
python3 scripts/code2database_builder.py kb-global-add \
  --title "Linux 内核线程模型" \
  --body "每线程事件循环；线程内无需锁..." \
  --tags "kernel,threading" --kind principle

# 搜索
python3 scripts/code2database_builder.py kb-global-search \
  --query "thread safety" [--top 10]

# 导出（给 teammate）
python3 scripts/code2database_builder.py kb-global-share \
  --output ~/global_kb_share.json

# 导入（teammate 的 JSON）
python3 scripts/code2database_builder.py kb-global-import \
  --input ~/global_kb_share.json
```

## 跨 C2D 管理（Phase 1 F5/F7/F8）

### `c2d-resolve-foreign`

强制按函数名重新解析 stale/deleted foreign_refs，不检查外部 C2D 的 mtime 是否变化。
适用于 A 重命名了函数、B 的 refs 过期的场景——`c2d-sync-foreign` 仅在 mtime 变化时重新解析。

```bash
python3 scripts/code2database_builder.py c2d-resolve-foreign \
  --graph code2db-out/ \
  [--foreign-c2d /path/to/A/c2db-out/]
```

### `c2d-prune-foreign`

删除指定状态（默认 `deleted,orphaned`）且超过 `--max-age-days`（默认 30 天）的 foreign_refs。
保留 resolved/stale/unresolved（仍活跃的）refs。防止 foreign_refs 表无界增长。

```bash
python3 scripts/code2database_builder.py c2d-prune-foreign \
  --graph code2db-out/ \
  [--max-age-days 30] \
  [--prune-statuses deleted,orphaned]
```

### `c2d-pin-foreign`  [write]

锁定一条 resolved foreign_ref，使其在 A 变化时不自动更新。锁定的 refs 即使 A 重命名或删除
函数也保持当前 `foreign_node_id`。适用于稳定的 API 契约场景。

```bash
python3 scripts/code2database_builder.py c2d-pin-foreign \
  --graph code2db-out/ \
  --ref-id 42
```

### `c2d-unpin-foreign`

恢复被锁定的 foreign_ref 的自动更新行为。

```bash
python3 scripts/code2database_builder.py c2d-unpin-foreign \
  --graph code2db-out/ \
  --ref-id 42
```

## 多项目命令

### `build-multi`

通过 manifest JSON 从多个有依赖关系的项目（A→B→C）构建统一 C2D。强制项目名 domain 前缀防止冲突。

```bash
python3 scripts/code2database_builder.py build-multi \
  --manifest projects.json --outdir joint_c2db-out/ \
  [-j 8] [--max-workers 48] [--force-rescan A,B] [--no-clang]
```

### `c2d-add-foreign`

注册外部 C2D（项目 A）并解析 B 的未解析调用。

```bash
python3 scripts/code2database_builder.py c2d-add-foreign \
  --graph B/c2db-out/ --foreign-c2d A/c2db-out/ --project-name A
```

### `c2d-sync-foreign`

检测外部 C2D 变更并重新解析 foreign_refs。

```bash
python3 scripts/code2database_builder.py c2d-sync-foreign \
  --graph B/c2db-out/ [--foreign-c2d A/c2db-out/]
```

### `c2d-list-foreign`

列出所有被监听的外部 C2D 及同步状态和引用计数。

```bash
python3 scripts/code2database_builder.py c2d-list-foreign --graph B/c2db-out/
```

### `c2d-remove-foreign`

注销外部 C2D。foreign_refs 标记为 'orphaned'。

```bash
python3 scripts/code2database_builder.py c2d-remove-foreign \
  --graph B/c2db-out/ --foreign-c2d A/c2db-out/
```

### `composite-query`

跨 C2D 查询（SQLite ATTACH）。支持 CALLERS_OF / CALLEES_OF。

```bash
python3 scripts/code2database_builder.py composite-query \
  --graph B/c2db-out/ --query "CALLERS_OF init" \
  --foreign-c2d C/c2db-out/ --top 50
```

### `c2d-check-compat`

检查 B 的 foreign_refs 对新版本 A 是否仍有效。

```bash
python3 scripts/code2database_builder.py c2d-check-compat \
  --graph B/c2db-out/ --against-c2d A_v2/c2db-out/
```

### `coverage-cross-c2d`

计算 target_c2d 中哪些函数被 test_c2d 调用。

```bash
python3 scripts/code2database_builder.py coverage-cross-c2d \
  --test-c2d test_A/c2db-out/ --target-c2d A/c2db-out/
```

### `c2d-add-foreign-stub`

注册 vendor SDK 签名 stub C2D（仅签名，API 稳定）。

```bash
python3 scripts/code2database_builder.py c2d-add-foreign-stub \
  --graph B/c2db-out/ --stub-c2d glibc_stub/ --project-name glibc
```

### `ffi-auto-link`

自动链接 FFI 绑定（ctypes/cgo/extern C）到被监听的外部 C2D。

```bash
python3 scripts/code2database_builder.py ffi-auto-link --graph B/c2db-out/
```

### `scan-rpc`

扫描源码中的 HTTP/gRPC 调用，创建 rpc_endpoint stub 节点和边。

```bash
python3 scripts/code2database_builder.py scan-rpc --graph B/c2db-out/
```

### `import-foreign-knowledge`

将外部 C2D 的 knowledge/*.md 复制到本地（带项目前缀）。

```bash
python3 scripts/code2database_builder.py import-foreign-knowledge \
  --graph B/c2db-out/ --foreign-c2d A/c2db-out/ --project-name A
```

## 按需 / 低权重命令

### `domain`

查看域结构（父技能中也有）。

```bash
python3 scripts/code2database_builder.py domain \
  --graph code2db-out/ \
  [--json]
```

### `extract-invariants-llm`

LLM 驱动的不变量提取（分析子技能中也有）。

### `intent-query` / `think-chain`

LLM 驱动的推理助手（分析子技能中也有）。

### `unbalanced-alloc-free`

查找不平衡的分配/释放对（分析子技能中也有）。

### `explain-label` / `why-ambiguous`

解释标签 / 歧义决策（分析子技能中也有）。

## 另见

- `SKILL_ops.md` — Tier-1 命令 + 路由表（子技能激活时始终加载）
- `RUNTIME_CONFIG.md` — 运行时调优参数
- `PROFILE_MANUAL.md` — profile 编写
- `~/.claude/skills/Code2Database-analysis/references/analysis_commands.md` — 分析命令
- 父技能 `~/.claude/skills/Code2Database/references/usage_reference.md` — Tier-1 命令语法
