# 机器人作业数据回流服务

这是一个面向机器人作业数据团队的服务端应用，负责管理机型、场景、技能、作业记录、人工标注和数据集。服务使用 FastAPI 提供本地 HTTP 接口，以 SQLite 保存业务数据；质量评分、数据集审核、版本、订阅和统计分析均在同一进程内完成。

## 目录

- `main.py`：应用入口、健康检查和路由注册。
- `app/models`：业务实体及其关系。
- `app/routers`：基础资源、作业、数据集和分析接口。
- `app/services`：评分、统计、策略目录、时间窗口与派生谱系工具。
- `app/seed_data.py`：可重复执行的示例数据初始化逻辑。
- `scripts/init_sample_data.py`：初始化脚本的兼容入口。

## 派生谱系

在“直接复用次数”之上，新增可追溯的数据集派生谱系（`dataset_derivations` 有向边：上游数据集/版本 → 下游数据集）。

- **固定上游版本与用途**：建边时写入上游版本 ID、版本标签及上/下游名称快照，历史关系不随数据集改名或发布新版本而改写。
- **建边约束**：只允许引用上游的**已发布版本**（拒绝跨越未发布版本）；拒绝自环、会形成环的边以及 `(上游版本, 下游)` 重复边。
- **并发防环**：建边在以 `BEGIN IMMEDIATE` 启动的串行写事务中完成“检查-写入”，同时创建相反方向的两条边也只会成功一条。
- **撤销发布**：上游撤销发布（`/unpublish` 或审核 `revoke`）会下线其版本并把出边标记为失效（`invalidated_at`），边保留不删除；查询时标出已无全有效路径可达的断链节点。
- **分层查询**：`GET /datasets/{id}/lineage?direction=downstream|upstream` 按 BFS 深度分层，节点按 ID、边按 ID 稳定排序，返回 `total_edges/returned_edges/truncated` 截断信息；`include_invalidated=false` 可只看有效链路。

主要接口（前缀 `/api/v1`）：

- `POST /dataset-derivations`：创建派生关系。
- `GET /dataset-derivations`：按 `upstream_dataset_id` / `downstream_dataset_id` 过滤、分页（稳定顺序，含 `truncated`）。
- `GET /datasets/{dataset_id}/lineage`：按方向分层查询谱系并标出失效链路。

## 配置与运行

默认数据库文件为项目根目录的 `robot_data.db`，可以通过 `DATABASE_URL` 指定 SQLite 文件。安装依赖后运行 `python3 main.py`，服务默认监听 `8000` 端口；`GET /health` 返回服务状态，接口文档位于 `/docs`。

初始化示例数据可执行 `python3 scripts/init_sample_data.py`。该命令会重建本地数据库并写入机型、场景、技能、作业、标注及数据集示例。

## 验证

运行 `python3 -m pytest -q` 执行服务和领域工具测试，运行 `python3 -m compileall -q app main.py scripts` 检查编译。测试只使用临时 SQLite 数据库，不需要额外服务。
