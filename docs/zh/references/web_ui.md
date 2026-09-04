# Web UI 参考文档

Web UI（`web-ui` 命令）提供基于浏览器的交互式代码图浏览界面。使用 cytoscape.js 3.28.1 渲染（本地提供，CDN 兜底）。

## 启动

```bash
python3 scripts/code2database_builder.py web-ui --graph <dir> [--port 8765]
```

默认绑定 127.0.0.1（可用 `C2D_WEB_UI_HOST` 覆盖）。

## HTTP API 端点

| 端点 | 方法 | 描述 |
|------|------|------|
| `/` | GET | 提供单文件 HTML UI |
| `/static/cytoscape.min.js` | GET | 本地 cytoscape.js 资源（CDN 兜底） |
| `/api/graph/summary` | GET | 节点/边/社区计数 + 源码新鲜度（过期徽标） |
| `/api/node/<id>` | GET | 单节点详情 + `related_memories`（按符号关联的前辈问答） |
| `/api/neighbors/<id>?depth=<n>` | GET | BFS 邻域（200 节点上限，带 `truncated` 标志） |
| `/api/path?from=<id>&to=<id>` | GET | 最短路径（仅调用边，深度 ≤ 10） |
| `/api/highlight-path` | GET/POST | 获取/设置高亮路径（API 已有；暂无 UI 控件） |
| `/api/communities` | GET | 列出所有社区 |
| `/api/community/<id>` | GET | 特定社区内的节点（上限 100） |
| `/api/search?q=<query>` | GET | 大小写不敏感名称搜索（子串；结果带 domain + 文件:行号 便于消歧） |
| `/api/degrees` | GET | 节点尺寸用的度数图（reload 时预计算） |
| `/api/callers/<id>` | GET | 直接调用者（调用边，带位置） |
| `/api/callees/<id>` | GET | 直接被调用者（调用边，带位置） |
| `/api/cycles` | GET | 循环调用边（深度 5 可达性，上限 50） |
| `/api/impact?node=<id>` | GET | 反向可达性影响分析（深度 ≤ 5，显示前 200 条，报告 `truncated`） |
| `/api/reload` | POST | 从磁盘重新加载图 |
| `/api/code?node=<id>` | GET | 源码片段（±10 行，4 KB 上限） |
| `/api/domains` | GET | 列出所有域及计数 |
| `/api/suggestions` | GET | 主动分析建议 |
| `/api/tour` | GET | 引导式代码库导览 markdown |
| `/api/brief` | GET | 项目简报（knowledge/brief.json，已渲染） |
| `/api/architecture` | GET | 架构叙事（ARCHITECTURE_FLOWS.md） |
| `/api/memory/search?q=&author=&symbol=&top=` | GET | 前辈问答检索（memory.db FTS5；空查询 = 按权重摘要） |
| `/api/memory/lineage` | GET | 记忆 split/merge 谱系图 |
| `/api/memory/authors` | GET | 记忆作者索引（用于过滤器） |

## 前端功能

- **Cytoscape.js 3.28.1** — canvas 渲染器，本地提供可离线使用
- **五种布局** — flow（breadthfirst）、force（cose）、rings（concentric）、circle、grid — 按节点数自动调优
- **真实社区** — 构建期 Leiden 社区（`.code2database_communities.json`）带人类可读标签；缺失时回退到域
- **搜索消歧** — 多匹配时打开结果列表（名称 + 文件:行号）；单匹配直接跳转
- **焦点+上下文淡化** — 点击节点淡化非邻居；调用/被调用边保持可见
- **循环高亮开关** — 循环调用边原地重绘样式
- **社区图例淡化** — 点击图例条目淡化该社区
- **节点标签过滤** — 按标签淡化节点（API_entry / thread / callback / default）
- **度数** — 节点尺寸随度数（预计算；不再每次点击全图扫描）
- **过期徽标** — 状态栏显示源码-图新鲜度
- **截断浮出** — 邻域 200 节点上限与影响分析 >200/深度 5 限制会显示而非静默
- **深色/浅色模式**、**PNG 导出**、**右键上下文菜单**
- **增量同步** — `syncCyFromModel()` 增删变化的节点和边（稳定 `src->tgt` 边 ID），原地重绘样式
- **项目上下文面板** — 简报、架构叙事、记忆问答（作者过滤、谱系视图）

## 键盘快捷键

| 键 | 动作 |
|-----|------|
| `/` | 聚焦搜索框 |
| `f` | 适配视图（`cy.fit(undefined, 42)`） |
| `?` | 帮助弹窗 |
| `d` | 切换深色/浅色 |
| `p` | 导出 PNG |
| `+` / `-` | 放大 / 缩小 |
| `1`–`5` | 设置探索深度 |
| `Enter` | 按当前深度重新聚焦活动节点 |
| `Escape` | 清除淡化 + 关闭弹窗/菜单/搜索结果 |

## 鼠标交互

- **左键点击节点** — 聚焦节点 + 淡化非邻居
- **左键点击背景** — 清除淡化
- **右键点击节点** — 上下文菜单（聚焦 / 深度 2 展开 / 影响分析 / 查看代码 / 复制 ID）
- **滚轮** — 缩放（cytoscape 原生；缩放低于 0.5 时隐藏标签）

## 性能说明

- 度数在每次 reload 时预计算一次（SQLite 后端为两条 `GROUP BY` 查询），而非每次请求全图扫描
- 环检测在锁内做邻接快照，然后在锁外跑逐边 BFS
- 增量同步避免全量重绘

## 限制

- 只读（Web UI 不能编辑图）
- 每服务器单图实例
- 无 SSL/TLS
- `/api/path` 与 `/api/highlight-path` 暂无 UI 控件
- 无小地图 / 工具提示（早期 README 声称有——已移除）
