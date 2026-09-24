import os
import tempfile

import pytest

# 在导入应用前指定临时数据库，避免污染仓库内的 robot_data.db
_tmp_dir = tempfile.mkdtemp(prefix="lineage-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_lineage.db"
os.environ.setdefault("API_V1_PREFIX", "/api/v1")

from app.database import Base, engine, SessionLocal  # noqa: E402
from app.models import Dataset, DatasetVersion, RobotModel, Scene  # noqa: E402


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from main import app

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c


def make_version(
    db,
    name: str,
    *,
    number: int | None = None,
    owner_team: str = "team",
    version_label: str | None = None,
    published: bool = True,
):
    """创建数据集及其版本；同一 name 再次调用会在该数据集上追加新版本。"""
    robot = db.query(RobotModel).first()
    if not robot:
        robot = RobotModel(name="RM-TEST", manufacturer="M")
        scene = Scene(name="SC-TEST", category="c")
        db.add_all([robot, scene])
        db.flush()
    scene = db.query(Scene).first()

    dataset = db.query(Dataset).filter(Dataset.name == name).first()
    if dataset is None:
        number = number or 1
        dataset = Dataset(
            name=name,
            owner_team=owner_team,
            robot_model_id=robot.id,
            scene_id=scene.id,
            review_status="approved" if published else "draft",
            is_published=published,
            version=version_label or f"v{number}",
            current_version=number,
        )
        db.add(dataset)
        db.flush()
    else:
        number = number or (
            (db.query(DatasetVersion)
             .filter(DatasetVersion.dataset_id == dataset.id)
             .count()) + 1
        )

    label = version_label or f"v{number}"
    version = DatasetVersion(
        dataset_id=dataset.id,
        version_number=number,
        version_label=label,
        is_published=published,
    )
    db.add(version)
    if number > dataset.current_version or published:
        dataset.current_version = max(dataset.current_version, number)
    db.commit()
    db.refresh(version)
    return version
