# Web UI 参考文档

Web UI（`web-ui` 命令）提供基于浏览器的交互式代码图浏览界面。加载 cytoscape.js 3.28.1 实现稠密图渲染。

## 启动

```bash
python3 scripts/code2database_builder.py web-ui --graph <dir> [--port 8765]
```

## HTTP API 端点

| 端点 | 方法 | 描述 |
|------|------|------|
| `/` | GET | 提供单文件 HTML UI |
| `/api/graph/summary` | GET | 节点/边/社区计数 |
| `/api/initial-view` | GET | 顶级社区代表节点 + 社区间边 |
| `/api/neighbors/<id>?depth=<n>` | GET | 节点的 BFS 邻域 |
| `/api/node/<id>` | GET | 单节点详情 |
| `/api/search?q=<query>` | GET | FTS5 全文搜索 |
| `/api/search-nodes?q=<query>` | GET | 搜索 + 预加载邻居边 |
| `/api/path?from=<id>&to=<id>` | GET | 两节点间最短路径 |
| `/api/communities` | GET | 列出所有社区 |
| `/api/community/<id>` | GET | 特定社区内的节点 |
| `/api/highlight-path` | GET/POST | 获取/设置高亮路径 |
| `/api/reload` | POST | 从磁盘重新加载图 |
| `/api/code?node=<id>` | GET | 节点源码片段 |
| `/api/domains` | GET | 列出所有域及计数 |
| `/api/impact?node=<id>` | GET | 反向可达性影响分析 |
| `/api/suggestions` | GET | 主动分析建议 |
| `/api/tour` | GET | 引导式代码导览 markdown |

## 前端特性

- **Cytoscape.js 3.28.1** — 内联嵌入，单文件离线可用
- **三种布局算法** — flow (breadthfirst)、rings (concentric)、force (cose)
- **聚焦+上下文淡出** — 点击节点，非邻居淡出到 0.15 不透明度
- **边捆绑** — curve-style: bezier 平行边展开
- **社区复合节点** — 按域分组，点击折叠/展开
- **LOD 标签隐藏** — 缩放 < 0.3 时隐藏标签
- **边 `call_condition` 标签** — 开关按钮显示/隐藏条件
- **选择器边过滤** — `cy.elements('[relation = "TYPE"]').toggleClass('hidden')` 即时切换
- **增量同步** — `syncCyFromModel()` 仅增删变更元素

## 键盘快捷键

| 键 | 动作 |
|----|------|
| `/` | 聚焦搜索栏 |
| `f` | 适配视图 (`cy.fit(undefined, 42)`) |
| `Escape` | 清除聚焦+上下文淡出 |
| `Alt+Left` | 后退导航 |
| `Alt+Right` | 前进导航 |

## 鼠标交互

- **左键点击节点** — 聚焦节点 + 淡出非邻居
- **左键点击背景** — 清除淡出
- **右键** — 上下文菜单（展开/折叠、聚焦、复制 ID）
- **悬停节点** — 显示名称/域/标签提示框
- **悬停边** — 显示 caller→callee + 条件提示框
- **滚轮** — 缩放（cytoscape 原生）

## 性能说明

- Cytoscape.js 3.28.1（~400KB）内联嵌入离线使用
- 增量同步避免全量重渲染
- 选择器边过滤 O(1) 切换，无需重渲染
- LOD 标签隐藏减少低缩放时的 DOM 节点

## 局限

- 只读（无法通过 Web UI 编辑图）
- 每服务器单图实例
- 无 SSL/TLS
- 未加载 cytoscape 扩展
