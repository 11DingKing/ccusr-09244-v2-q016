# 机器人作业数据回流服务

这是一个面向机器人作业数据团队的服务端应用，负责管理机型、场景、技能、作业记录、人工标注和数据集。服务使用 FastAPI 提供本地 HTTP 接口，以 SQLite 保存业务数据；质量评分、数据集审核、版本、订阅和统计分析均在同一进程内完成。

## 目录

- `main.py`：应用入口、健康检查和路由注册。
- `app/models`：业务实体及其关系。
- `app/routers`：基础资源、作业、数据集、派生谱系和分析接口。
- `app/services`：评分、统计、策略目录、时间窗口与派生谱系工具。
- `app/seed_data.py`：可重复执行的示例数据初始化逻辑。
- `scripts/init_sample_data.py`：初始化脚本的兼容入口。

## 数据集派生谱系

多个团队基于已发布数据集再加工时，可通过 `POST /api/v1/dataset-derivations`
在**具体版本之间**建立派生边（上游版本 → 下游版本），建边时固定两端版本、
数据集名称快照与用途（purpose）：

- 仅允许连接两端均已发布的版本，拒绝跨越未发布版本、自环与同数据集内派生；
- 拒绝重复边（409）与任何会形成环的边（含并发相反方向创建，服务通过写锁串行化保证）；
- 历史关系不可变：数据集改名、发布新版本都不会改写已有边（展示使用建边时的名称快照）；
- 版本被撤销发布（unpublish / 审核撤销）时，相关边只置失效位（`invalidated_at`/原因），
  边本身保留以维持可追溯；
- `GET /api/v1/dataset-versions/{version_id}/lineage?direction=downstream|upstream`
  按层（BFS）查询一个版本最终影响了哪些下游成果、或由哪些上游派生而来，支持
  `max_depth`、`per_layer_limit`，层内按边 id 稳定排序，并返回每层截断标记、
  深度截断标记与失效链路标记（`path_invalidated` 等）；
- `GET /api/v1/dataset-derivations` 支持按上游/下游版本和有效状态过滤、稳定顺序分页。

## 配置与运行

默认数据库文件为项目根目录的 `robot_data.db`，可以通过 `DATABASE_URL` 指定 SQLite 文件。安装依赖后运行 `python3 main.py`，服务默认监听 `8000` 端口；`GET /health` 返回服务状态，接口文档位于 `/docs`。

初始化示例数据可执行 `python3 scripts/init_sample_data.py`。该命令会重建本地数据库并写入机型、场景、技能、作业、标注及数据集示例。

## 验证

运行 `python3 -m pytest -q` 执行服务和领域工具测试，运行 `python3 -m compileall -q app main.py scripts` 检查编译。测试只使用临时 SQLite 数据库，不需要额外服务。
