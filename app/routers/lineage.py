from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DatasetDerivation
from app.schemas.dataset import (
    DatasetDerivationCreate,
    DatasetDerivationResponse,
    LineageResponse,
)
from app.services import lineage as lineage_service
from app.services.lineage import LineageError

router = APIRouter()


@router.post("/dataset-derivations", response_model=DatasetDerivationResponse, tags=["数据集派生谱系"])
def create_dataset_derivation(data: DatasetDerivationCreate, db: Session = Depends(get_db)):
    """创建派生关系（上游版本 → 下游版本），创建时固定版本与用途。

    - 两端版本必须均已发布，拒绝跨越未发布版本；
    - 拒绝循环派生、重复边（重复边返回 409）；
    - 并发创建相反方向关系时通过写锁串行化，保证不成环。
    """
    try:
        return lineage_service.create_derivation(db, data)
    except LineageError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/dataset-derivations", response_model=list[DatasetDerivationResponse], tags=["数据集派生谱系"])
def list_dataset_derivations(
    upstream_version_id: int | None = Query(None, description="按上游版本过滤"),
    downstream_version_id: int | None = Query(None, description="按下游版本过滤"),
    active_only: bool = Query(False, description="仅返回未失效的边"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """按边查询，稳定按 id 升序返回，支持 skip/limit 分页。"""
    query = db.query(DatasetDerivation)
    if upstream_version_id is not None:
        query = query.filter(DatasetDerivation.upstream_version_id == upstream_version_id)
    if downstream_version_id is not None:
        query = query.filter(DatasetDerivation.downstream_version_id == downstream_version_id)
    if active_only:
        query = query.filter(DatasetDerivation.invalidated_at.is_(None))
    return query.order_by(DatasetDerivation.id.asc()).offset(skip).limit(limit).all()


@router.get(
    "/dataset-versions/{version_id}/lineage",
    response_model=LineageResponse,
    tags=["数据集派生谱系"],
)
def get_version_lineage(
    version_id: int,
    direction: str = Query(
        "downstream",
        pattern="^(upstream|downstream)$",
        description="downstream=该版本影响了哪些下游成果；upstream=它由哪些上游版本派生",
    ),
    max_depth: int = Query(10, ge=1, le=50, description="最大分层深度"),
    per_layer_limit: int = Query(100, ge=1, le=500, description="每层最多返回节点数，超出则截断"),
    db: Session = Depends(get_db),
):
    """按上游/下游方向分层查询谱系；因撤销发布失效的链路会被实时标出。"""
    try:
        return lineage_service.traverse_lineage(db, version_id, direction, max_depth, per_layer_limit)
    except LineageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
