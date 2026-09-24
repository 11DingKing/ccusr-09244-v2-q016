import os
import tempfile

import pytest

# 必须在导入应用之前指定临时数据库，引擎在导入时按该 URL 创建。
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine, SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Dataset,
    DatasetVersion,
    RobotModel,
    Scene,
)
import main  # noqa: E402


@pytest.fixture
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def factory(db):
    """创建测试用基础资源与数据集/版本的工厂。"""
    rm = RobotModel(name="RM-测试机型", manufacturer="测试厂商")
    scene = Scene(name="测试场景", category="测试分类")
    db.add_all([rm, scene])
    db.flush()

    def make_dataset(
        name,
        *,
        published=True,
        version_label="1.0",
        version_number=1,
        owner_team="测试团队",
    ):
        dataset = Dataset(
            name=name,
            robot_model_id=rm.id,
            scene_id=scene.id,
            owner_team=owner_team,
            total_items=1,
            review_status="approved" if published else "draft",
            is_published=published,
            version=version_label,
            current_version=version_number,
        )
        db.add(dataset)
        db.flush()
        version = DatasetVersion(
            dataset_id=dataset.id,
            version_number=version_number,
            version_label=version_label,
            is_published=published,
            total_items=1,
        )
        db.add(version)
        db.commit()
        db.refresh(dataset)
        db.refresh(version)
        return dataset, version

    return make_dataset


def edge_body(upstream, version, downstream, purpose="再加工训练", **extra):
    body = {
        "upstream_dataset_id": upstream.id,
        "upstream_version_id": version.id,
        "downstream_dataset_id": downstream.id,
        "purpose": purpose,
    }
    body.update(extra)
    return body
