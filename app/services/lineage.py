"""数据集派生谱系：建边、失效与分层查询。

谱系以 DatasetDerivation 有向边（上游数据集/版本 → 下游数据集）表达。建边时固定
上游版本与名称快照，并在一个以 BEGIN IMMEDIATE 启动的事务内完成“重复边/环/发布
状态”检查与写入，因此并发（包括同时创建相反方向的两条边）也只能串行提交，第二
条边一定能看到第一条边并拒绝成环。上游撤销发布时只把边标记失效，不删除、不改写
任何历史快照。
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import Dataset, DatasetDerivation, DatasetVersion

# 查询时的安全上限，避免异常深度参数导致无界遍历
HARD_EDGE_CAP = 5000


class LineageError(ValueError):
    """谱系规则被违反，status 为建议的 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _load_datasets(db: Session, upstream_id: int, downstream_id: int):
    upstream = db.query(Dataset).filter(Dataset.id == upstream_id).first()
    if not upstream:
        raise LineageError("上游数据集不存在", status=404)
    downstream = db.query(Dataset).filter(Dataset.id == downstream_id).first()
    if not downstream:
        raise LineageError("下游数据集不存在", status=404)
    return upstream, downstream


def _would_cycle(children: dict[int, set[int]], start: int, target: int) -> bool:
    """新增 start→target 后是否成环：等价于 target 能否沿既有边到达 start。"""
    if start == target:
        return True
    queue: deque[int] = deque([target])
    seen = {target}
    while queue:
        current = queue.popleft()
        for nxt in children.get(current, ()):  # type: ignore[union-attr]
            if nxt == start:
                return True
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return False


def create_derivation(db: Session, payload: Any) -> DatasetDerivation:
    # 调用方需绑定 immediate 引擎：环检查与写入必须在同一个写锁事务内，
    # 并发（包括相反方向同时建边）串行提交，第二条边一定能看到第一条边。
    upstream_id = payload.upstream_dataset_id
    downstream_id = payload.downstream_dataset_id
    version_id = payload.upstream_version_id
    purpose = (payload.purpose or "").strip()

    if not purpose:
        raise LineageError("用途不能为空", status=400)
    if upstream_id == downstream_id:
        raise LineageError("上游与下游不能是同一个数据集", status=400)

    upstream, downstream = _load_datasets(db, upstream_id, downstream_id)

    version = db.query(DatasetVersion).filter(
        DatasetVersion.id == version_id,
        DatasetVersion.dataset_id == upstream_id,
    ).first()
    if not version:
        raise LineageError("指定的上游版本不存在或不属于该上游数据集", status=400)
    if not version.is_published:
        # 版本级发布状态是唯一权威：它只在显式撤销发布时失效，数据集处于新版本
        # 草稿期不会影响历史已发布版本，因此也不会阻断对旧版本的合法派生。
        raise LineageError("只能基于已发布版本创建派生关系，禁止跨越未发布版本", status=400)

    duplicate = db.query(DatasetDerivation.id).filter(
        DatasetDerivation.upstream_version_id == version_id,
        DatasetDerivation.downstream_dataset_id == downstream_id,
    ).first()
    if duplicate:
        raise LineageError("该派生关系已存在，不允许重复建边", status=409)

    # 全量载入边构建邻接表（含已失效边：历史边依然参与成环判断）。
    rows = db.query(
        DatasetDerivation.upstream_dataset_id,
        DatasetDerivation.downstream_dataset_id,
    ).all()
    children: dict[int, set[int]] = {}
    for up_id, down_id in rows:
        children.setdefault(up_id, set()).add(down_id)

    if _would_cycle(children, upstream_id, downstream_id):
        raise LineageError("创建该派生关系会形成循环谱系", status=409)

    edge = DatasetDerivation(
        upstream_dataset_id=upstream_id,
        upstream_version_id=version_id,
        downstream_dataset_id=downstream_id,
        upstream_name_snapshot=upstream.name,
        upstream_version_label_snapshot=version.version_label,
        downstream_name_snapshot=downstream.name,
        purpose=purpose,
        project_name=payload.project_name,
        created_by=payload.created_by,
        upstream_published=True,
    )
    db.add(edge)
    try:
        db.commit()
    except Exception:
        # 并发下唯一约束兜底：另一事务已插入相同边。
        db.rollback()
        raise LineageError("该派生关系已存在，不允许重复建边", status=409)
    db.refresh(edge)
    return edge


def apply_publication_revocation(db: Session, upstream_dataset_id: int) -> int:
    """撤销发布的核心操作：下线版本、失效出边，但不提交。

    供已处于请求事务中的流程（审核 revoke、unpublish 接口）在同一事务内调用，
    由调用方统一 commit。返回失效边数。
    """
    now = datetime.now(timezone.utc)
    db.query(DatasetVersion).filter(
        DatasetVersion.dataset_id == upstream_dataset_id,
        DatasetVersion.is_published.is_(True),
    ).update({DatasetVersion.is_published: False}, synchronize_session=False)
    edges = db.query(DatasetDerivation).filter(
        DatasetDerivation.upstream_dataset_id == upstream_dataset_id,
        DatasetDerivation.upstream_published.is_(True),
    ).all()
    for edge in edges:
        edge.upstream_published = False
        edge.invalidated_at = now
    return len(edges)


def _edge_to_dict(edge: DatasetDerivation) -> dict[str, Any]:
    return {
        "id": edge.id,
        "upstream_dataset_id": edge.upstream_dataset_id,
        "upstream_version_id": edge.upstream_version_id,
        "upstream_version_label": edge.upstream_version_label_snapshot,
        "upstream_name_snapshot": edge.upstream_name_snapshot,
        "downstream_dataset_id": edge.downstream_dataset_id,
        "downstream_name_snapshot": edge.downstream_name_snapshot,
        "purpose": edge.purpose,
        "project_name": edge.project_name,
        "created_by": edge.created_by,
        "upstream_published": edge.upstream_published,
        "invalidated": not edge.upstream_published,
        "invalidated_at": edge.invalidated_at,
        "created_at": edge.created_at,
    }


def query_lineage(
    db: Session,
    dataset_id: int,
    direction: str = "downstream",
    max_depth: int = 10,
    limit: int = 100,
    include_invalidated: bool = True,
) -> dict[str, Any]:
    """按上游或下游方向分层（BFS）查询谱系。

    - downstream：列出直接/间接派生自该数据集的成果；upstream：反向追溯来源。
    - 节点按 dataset_id 升序、边按 id 升序，顺序稳定。
    - 遍历深度内的全部可达边用于计算 total 与失效标记，返回边受 limit 截断并通过
      truncated/returned_edges/total_edges 明示。
    """
    if direction not in ("upstream", "downstream"):
        raise LineageError("direction 仅支持 upstream 或 downstream", status=400)
    if max_depth < 1 or max_depth > 50:
        raise LineageError("max_depth 必须位于 1 到 50 之间", status=400)
    if limit < 1 or limit > 500:
        raise LineageError("limit 必须位于 1 到 500 之间", status=400)

    root = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if not root:
        raise LineageError("数据集不存在", status=404)

    edge_rows = db.query(DatasetDerivation).order_by(DatasetDerivation.id.asc()).all()
    if not include_invalidated:
        edge_rows = [e for e in edge_rows if e.upstream_published]

    # 邻接表：downstream 用 upstream→children；upstream 用 downstream→parents。
    adjacency: dict[int, list[DatasetDerivation]] = {}
    for edge in edge_rows:
        key = edge.upstream_dataset_id if direction == "downstream" else edge.downstream_dataset_id
        adjacency.setdefault(key, []).append(edge)

    # BFS 求最短深度，并收集深度内全部可达边（已按 id 排序）。
    depth_of = {dataset_id: 0}
    reachable: list[DatasetDerivation] = []
    frontier = [dataset_id]
    for depth in range(max_depth):
        next_frontier: list[int] = []
        for node in frontier:
            for edge in adjacency.get(node, []):
                child = edge.downstream_dataset_id if direction == "downstream" else edge.upstream_dataset_id
                reachable.append(edge)
                if child not in depth_of:
                    depth_of[child] = depth + 1
                    next_frontier.append(child)
        frontier = next_frontier
        if not frontier:
            break

    reachable.sort(key=lambda e: e.id)
    if len(reachable) > HARD_EDGE_CAP:
        reachable = reachable[:HARD_EDGE_CAP]

    total_edges = len(reachable)
    page_edges = reachable[:limit]

    # 仅沿“有效边”再做一次 BFS：无法经全有效路径到达的节点即因撤销发布而断链。
    valid_adjacency: dict[int, set[int]] = {}
    for edge in reachable:
        if not edge.upstream_published:
            continue
        key = edge.upstream_dataset_id if direction == "downstream" else edge.downstream_dataset_id
        child = edge.downstream_dataset_id if direction == "downstream" else edge.upstream_dataset_id
        valid_adjacency.setdefault(key, set()).add(child)
    valid_reachable = {dataset_id}
    queue: deque[int] = deque([dataset_id])
    while queue:
        current = queue.popleft()
        for nxt in valid_adjacency.get(current, set()):
            if nxt not in valid_reachable:
                valid_reachable.add(nxt)
                queue.append(nxt)

    # 节点上挂载的边只放当前页内、且指向该节点的边，保证分层输出完整可还原。
    node_ids = {dataset_id}
    for edge in page_edges:
        node_ids.add(edge.upstream_dataset_id)
        node_ids.add(edge.downstream_dataset_id)
    names = dict(
        db.query(Dataset.id, Dataset.name).filter(Dataset.id.in_(node_ids)).all()
    )

    edges_by_child: dict[int, list[dict[str, Any]]] = {}
    for edge in page_edges:
        child = edge.downstream_dataset_id if direction == "downstream" else edge.upstream_dataset_id
        edges_by_child.setdefault(child, []).append(_edge_to_dict(edge))

    layers: list[dict[str, Any]] = []
    for depth in range(1, max_depth + 1):
        nodes_at_depth = sorted(node_id for node_id, d in depth_of.items() if d == depth)
        # 只展示有边进入当前页的节点，避免截断后出现没有边的空壳节点
        nodes_at_depth = [node_id for node_id in nodes_at_depth if node_id in edges_by_child]
        if not nodes_at_depth:
            continue
        nodes = []
        for node_id in nodes_at_depth:
            nodes.append({
                "dataset_id": node_id,
                "name": names.get(node_id, ""),
                "invalidated": node_id not in valid_reachable,
                "edges": edges_by_child[node_id],
            })
        layers.append({"depth": depth, "nodes": nodes})

    return {
        "root": {"dataset_id": root.id, "name": root.name},
        "direction": direction,
        "max_depth": max_depth,
        "limit": limit,
        "include_invalidated": include_invalidated,
        "total_edges": total_edges,
        "returned_edges": len(page_edges),
        "truncated": total_edges > len(page_edges),
        "layers": layers,
    }


def list_derivations(
    db: Session,
    upstream_dataset_id: Optional[int] = None,
    downstream_dataset_id: Optional[int] = None,
    include_invalidated: bool = True,
    skip: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    query = db.query(DatasetDerivation)
    if upstream_dataset_id is not None:
        query = query.filter(DatasetDerivation.upstream_dataset_id == upstream_dataset_id)
    if downstream_dataset_id is not None:
        query = query.filter(DatasetDerivation.downstream_dataset_id == downstream_dataset_id)
    if not include_invalidated:
        query = query.filter(DatasetDerivation.upstream_published.is_(True))

    total = query.count()
    rows = (
        query.order_by(DatasetDerivation.id.asc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return {
        "items": [_edge_to_dict(edge) for edge in rows],
        "total": total,
        "skip": skip,
        "limit": limit,
        "truncated": total > skip + len(rows),
    }
