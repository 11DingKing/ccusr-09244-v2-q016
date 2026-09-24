"""数据集派生谱系服务。

派生边（DatasetDerivation）在创建时固定两端的具体版本与名称快照，
历史关系不随数据集改名或新版本发布而改变。版本被撤销发布后，关联边只置失效位，
查询时实时标出失效链路。
"""

from collections import deque
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.database import acquire_lineage_lock
from app.models import Dataset, DatasetDerivation, DatasetVersion
from app.schemas.dataset import DatasetDerivationCreate


class LineageError(Exception):
    def __init__(self, detail: str, status_code: int = 400):
        self.detail = detail
        self.status_code = status_code


def _get_version(db: Session, version_id: int, role: str) -> DatasetVersion:
    version = db.query(DatasetVersion).filter(DatasetVersion.id == version_id).first()
    if not version:
        raise LineageError(f"{role}版本不存在", status_code=404)
    return version


def _reaches(db: Session, start_version_id: int, target_version_id: int) -> bool:
    """沿 start_version 的下游方向（start 作为上游）能否到达 target。

    用于判定新增边 upstream→downstream 是否成环：
    成环当且仅当 downstream 已能沿下游方向到达 upstream。
    失效边仍参与成环检测——失效不等于删除，不允许借失效边“复活”环。
    """
    if start_version_id == target_version_id:
        return True
    seen = {start_version_id}
    frontier = deque([start_version_id])
    while frontier:
        rows = db.query(DatasetDerivation.downstream_version_id).filter(
            DatasetDerivation.upstream_version_id.in_(list(frontier))
        ).all()
        frontier = deque()
        for (next_id,) in rows:
            if next_id == target_version_id:
                return True
            if next_id not in seen:
                seen.add(next_id)
                frontier.append(next_id)
    return False


def create_derivation(db: Session, data: DatasetDerivationCreate) -> DatasetDerivation:
    if data.upstream_version_id == data.downstream_version_id:
        raise LineageError("上游版本与下游版本不能相同")

    # 先抢占写锁，串行化并发创建（含相反方向的竞争），避免双重检查之间被插入成环
    acquire_lineage_lock(db)

    upstream = _get_version(db, data.upstream_version_id, "上游")
    downstream = _get_version(db, data.downstream_version_id, "下游")

    if upstream.dataset_id == downstream.dataset_id:
        raise LineageError("不允许在同一数据集的版本之间建立派生关系（版本演进由版本历史表达）")

    # 重复边优先返回 409，保证幂等（即使端点版本后来被撤销发布）
    existing = db.query(DatasetDerivation).filter(
        DatasetDerivation.upstream_version_id == data.upstream_version_id,
        DatasetDerivation.downstream_version_id == data.downstream_version_id,
    ).first()
    if existing:
        raise LineageError("该派生关系已存在，不允许重复边", status_code=409)

    if not upstream.is_published:
        raise LineageError("跨越未发布版本：上游版本尚未发布，不能建立派生关系")
    if not downstream.is_published:
        raise LineageError("跨越未发布版本：下游版本尚未发布，不能建立派生关系")

    # 新边 upstream→downstream 成环 ⇔ downstream 当前已能到达 upstream
    if _reaches(db, data.downstream_version_id, data.upstream_version_id):
        raise LineageError("拒绝循环派生：下游版本已是上游版本的祖先")

    upstream_dataset = db.query(Dataset).filter(Dataset.id == upstream.dataset_id).first()
    downstream_dataset = db.query(Dataset).filter(Dataset.id == downstream.dataset_id).first()

    edge = DatasetDerivation(
        upstream_dataset_id=upstream.dataset_id,
        upstream_version_id=upstream.id,
        downstream_dataset_id=downstream.dataset_id,
        downstream_version_id=downstream.id,
        purpose=data.purpose,
        created_by=data.created_by,
        upstream_dataset_name=upstream_dataset.name,
        upstream_version_label=upstream.version_label,
        downstream_dataset_name=downstream_dataset.name,
        downstream_version_label=downstream.version_label,
    )
    db.add(edge)
    db.commit()
    db.refresh(edge)
    return edge


def invalidate_edges_for_version(db: Session, version_id: int, reason: str) -> int:
    """版本撤销发布时，将其作为任一端点的有效边置为失效（不删除边）。"""
    edges = db.query(DatasetDerivation).filter(
        DatasetDerivation.invalidated_at.is_(None),
        (DatasetDerivation.upstream_version_id == version_id)
        | (DatasetDerivation.downstream_version_id == version_id),
    ).all()
    now = datetime.now(timezone.utc)
    for edge in edges:
        edge.invalidated_at = now
        edge.invalidate_reason = reason
    return len(edges)


def traverse_lineage(
    db: Session,
    root_version_id: int,
    direction: str,
    max_depth: int,
    per_layer_limit: int,
) -> dict:
    """从根版本出发按 upstream/downstream 方向分层（BFS）遍历谱系。

    分层语义：
    - 第 0 层为根版本；每个版本只在其最短可达深度出现一次。
    - 菱形合流时，同一版本在同一层可对应多个条目（每个直接前驱一条边），
      边信息（purpose/edge_id）分别保留。
    - 顺序稳定：层内按 (edge_id, version_id) 升序，与字典序/插入偶然顺序无关。
    - 截断：每层最多返回 per_layer_limit 个条目（仍按全量邻接扩展下一层）；
      达到 max_depth 仍有未展示层时 depth_truncated=True。
    - 失效实时计算：版本撤销发布（is_published=False）或路径上任一边失效，
      该条链路标记 path_invalidated=True；边失效不删除，仍可被遍历到。
    - 名称使用建边时固定的快照，数据集改名不改变历史展示。
    """
    if direction not in ("upstream", "downstream"):
        raise LineageError("direction 必须为 upstream 或 downstream")

    root = db.query(DatasetVersion).filter(DatasetVersion.id == root_version_id).first()
    if not root:
        raise LineageError("根版本不存在", status_code=404)
    root_dataset = db.query(Dataset).filter(Dataset.id == root.dataset_id).first()

    version_rows: Dict[int, DatasetVersion] = {root.id: root}
    dataset_owner: Dict[int, Optional[str]] = {root_dataset.id: root_dataset.owner_team}

    def _load_versions(version_ids):
        missing = {vid for vid in version_ids if vid not in version_rows}
        if missing:
            for v in db.query(DatasetVersion).filter(DatasetVersion.id.in_(missing)).all():
                version_rows[v.id] = v
            ds_ids = {version_rows[vid].dataset_id for vid in missing}
            for d in db.query(Dataset).filter(Dataset.id.in_(ds_ids)).all():
                dataset_owner[d.id] = d.owner_team

    def _node(vid: int, edge: Optional[DatasetDerivation], chain_healthy: bool) -> dict:
        v = version_rows[vid]
        if edge is not None:
            if direction == "downstream":
                pinned_name, pinned_label = edge.downstream_dataset_name, edge.downstream_version_label
            else:
                pinned_name, pinned_label = edge.upstream_dataset_name, edge.upstream_version_label
        else:
            pinned_name, pinned_label = root_dataset.name, v.version_label

        edge_active = edge is None or edge.invalidated_at is None
        return {
            "dataset_id": v.dataset_id,
            "version_id": v.id,
            "dataset_name": pinned_name,
            "version_label": pinned_label,
            "owner_team": dataset_owner.get(v.dataset_id),
            "version_published": bool(v.is_published),
            "invalidated": not bool(v.is_published),
            "path_invalidated": not chain_healthy,
            "edge_id": edge.id if edge else None,
            "purpose": edge.purpose if edge else None,
            "edge_active": edge_active,
            "edge_invalidated_at": edge.invalidated_at if edge else None,
            "edge_invalidate_reason": edge.invalidate_reason if edge else None,
        }

    layers = [{
        "depth": 0,
        "total_count": 1,
        "returned_count": 1,
        "truncated": False,
        "nodes": [_node(root.id, None, bool(root.is_published))],
    }]

    # healthy[vid]：是否存在一条根→vid 的全有效路径（所有边有效、所有版本已发布）
    healthy: Dict[int, bool] = {root.id: bool(root.is_published)}
    visited = {root.id}
    frontier = [root.id]
    depth = 0
    total_edges = 0
    returned_edges = 0
    merged_away = 0
    any_layer_truncated = False

    while frontier and depth < max_depth:
        depth += 1
        if direction == "downstream":
            edges = db.query(DatasetDerivation).filter(
                DatasetDerivation.upstream_version_id.in_(frontier)
            ).all()
        else:
            edges = db.query(DatasetDerivation).filter(
                DatasetDerivation.downstream_version_id.in_(frontier)
            ).all()
        edges.sort(key=lambda e: e.id)
        total_edges += len(edges)

        # target_vid -> 该层到达它的边（菱形合流可能有多条）
        arrivals: Dict[int, List[DatasetDerivation]] = {}
        for e in edges:
            target = e.downstream_version_id if direction == "downstream" else e.upstream_version_id
            arrivals.setdefault(target, []).append(e)

        entries = []          # (target, edge)
        next_frontier = []
        for target in sorted(arrivals):
            target_edges = arrivals[target]
            if target in visited:
                # 版本已在更浅的层展示；边仍计入总数但不重复展示节点
                merged_away += len(target_edges)
                continue
            visited.add(target)
            next_frontier.append(target)
            entries.extend((target, e) for e in target_edges)

        if not entries:
            # 所有邻接都合流到已展示版本（跨层边），不产生新层
            frontier = []
            break

        _load_versions([t for t, _ in entries])
        for target in next_frontier:
            v = version_rows[target]
            # 存在一条全有效路径（边有效且前驱健康）且版本自身仍发布，才算健康
            healthy[target] = bool(v.is_published) and any(
                e.invalidated_at is None
                and healthy.get(
                    e.upstream_version_id if direction == "downstream" else e.downstream_version_id,
                    False,
                )
                for e in arrivals[target]
            )

        nodes = []
        for target, e in entries:
            parent = e.upstream_version_id if direction == "downstream" else e.downstream_version_id
            chain_healthy = (
                healthy[target] and e.invalidated_at is None and healthy.get(parent, False)
            )
            nodes.append(_node(target, e, chain_healthy))
        nodes.sort(key=lambda n: (n["edge_id"], n["version_id"]))

        truncated = len(nodes) > per_layer_limit
        any_layer_truncated = any_layer_truncated or truncated
        shown = nodes[:per_layer_limit]
        returned_edges += len(shown)

        layers.append({
            "depth": depth,
            "total_count": len(nodes),
            "returned_count": len(shown),
            "truncated": truncated,
            "nodes": shown,
        })
        frontier = next_frontier

    depth_truncated = bool(frontier)

    return {
        "root_version_id": root_version_id,
        "direction": direction,
        "max_depth": max_depth,
        "layers": layers,
        "total_edges": total_edges,
        "returned_edges": returned_edges,
        "merged_edges": merged_away,
        "truncated": any_layer_truncated or depth_truncated,
        "depth_truncated": depth_truncated,
    }
