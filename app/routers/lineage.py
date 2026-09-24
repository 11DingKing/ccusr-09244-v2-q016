"""数据集派生谱系接口。"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db, get_db_immediate
from app.schemas.dataset import (
    DatasetDerivationCreate,
    DatasetDerivationResponse,
    DerivationListResponse,
    LineageResponse,
)
from app.services import lineage as lineage_service
from app.services.lineage import LineageError

router = APIRouter()


@router.post(
    "/dataset-derivations",
    response_model=DatasetDerivationResponse,
    tags=["数据集派生谱系"],
)
def create_dataset_derivation(data: DatasetDerivationCreate, db: Session = Depends(get_db_immediate)):
    try:
        return lineage_service.create_derivation(db, data)
    except LineageError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))


@router.get(
    "/dataset-derivations",
    response_model=DerivationListResponse,
    tags=["数据集派生谱系"],
)
def list_dataset_derivations(
    upstream_dataset_id: Optional[int] = Query(None, description="按上游数据集过滤"),
    downstream_dataset_id: Optional[int] = Query(None, description="按下游数据集过滤"),
    include_invalidated: bool = Query(True, description="是否包含因撤销发布而失效的关系"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    return lineage_service.list_derivations(
        db,
        upstream_dataset_id=upstream_dataset_id,
        downstream_dataset_id=downstream_dataset_id,
        include_invalidated=include_invalidated,
        skip=skip,
        limit=limit,
    )


@router.get(
    "/datasets/{dataset_id}/lineage",
    response_model=LineageResponse,
    tags=["数据集派生谱系"],
)
def get_dataset_lineage(
    dataset_id: int,
    direction: str = Query("downstream", description="查询方向：downstream 下游成果 / upstream 上游来源"),
    max_depth: int = Query(10, ge=1, le=50, description="最大分层深度"),
    limit: int = Query(100, ge=1, le=500, description="返回边数上限"),
    include_invalidated: bool = Query(True, description="是否遍历已失效边并标出断链节点"),
    db: Session = Depends(get_db),
):
    try:
        return lineage_service.query_lineage(
            db,
            dataset_id,
            direction=direction,
            max_depth=max_depth,
            limit=limit,
            include_invalidated=include_invalidated,
        )
    except LineageError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
