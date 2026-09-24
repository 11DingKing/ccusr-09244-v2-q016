from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Float, Boolean, JSON, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class RobotModel(Base):
    __tablename__ = "robot_models"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    manufacturer = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    capabilities = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    operations = relationship("OperationData", back_populates="robot_model")
    datasets = relationship("Dataset", back_populates="robot_model")


class Scene(Base):
    __tablename__ = "scenes"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    category = Column(String(50), nullable=False, index=True)
    description = Column(Text, nullable=True)
    environment_tags = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    operations = relationship("OperationData", back_populates="scene")
    datasets = relationship("Dataset", back_populates="scene")


class Skill(Base):
    __tablename__ = "skills"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    category = Column(String(50), nullable=False, index=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    operations = relationship("OperationData", back_populates="skill")


class OperationData(Base):
    __tablename__ = "operation_data"

    id = Column(Integer, primary_key=True, index=True)
    robot_model_id = Column(Integer, ForeignKey("robot_models.id"), nullable=False, index=True)
    scene_id = Column(Integer, ForeignKey("scenes.id"), nullable=False, index=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False, index=True)
    robot_serial = Column(String(100), nullable=True, index=True)

    motion_trajectory = Column(JSON, nullable=False)
    perception_records = Column(JSON, nullable=False)
    grasp_result = Column(JSON, nullable=True)

    timestamp_start = Column(DateTime(timezone=True), nullable=False)
    timestamp_end = Column(DateTime(timezone=True), nullable=False)
    duration_ms = Column(Integer, nullable=True)

    environment_conditions = Column(JSON, nullable=True)
    hardware_status = Column(JSON, nullable=True)

    quality_score = Column(Float, nullable=True)
    completeness_score = Column(Float, nullable=True)
    data_grade = Column(String(10), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    robot_model = relationship("RobotModel", back_populates="operations")
    scene = relationship("Scene", back_populates="operations")
    skill = relationship("Skill", back_populates="operations")
    annotation = relationship("Annotation", back_populates="operation_data", uselist=False, cascade="all, delete-orphan")
    dataset_items = relationship("DatasetItem", back_populates="operation_data", cascade="all, delete-orphan")


class Annotation(Base):
    __tablename__ = "annotations"

    id = Column(Integer, primary_key=True, index=True)
    operation_data_id = Column(Integer, ForeignKey("operation_data.id"), nullable=False, unique=True, index=True)

    is_success = Column(Boolean, nullable=False, index=True)
    failure_category = Column(String(50), nullable=True, index=True)
    failure_subcategory = Column(String(100), nullable=True)
    failure_description = Column(Text, nullable=True)

    annotator = Column(String(100), nullable=True)
    annotation_time = Column(DateTime(timezone=True), server_default=func.now())
    review_status = Column(String(20), default="pending", index=True)
    reviewer = Column(String(100), nullable=True)
    review_notes = Column(Text, nullable=True)

    annotation_quality_score = Column(Float, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    operation_data = relationship("OperationData", back_populates="annotation")


class Dataset(Base):
    __tablename__ = "datasets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False, index=True)
    description = Column(Text, nullable=True)
    version = Column(String(20), default="1.0")

    robot_model_id = Column(Integer, ForeignKey("robot_models.id"), nullable=False, index=True)
    scene_id = Column(Integer, ForeignKey("scenes.id"), nullable=False, index=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=True, index=True)

    owner_team = Column(String(100), nullable=False)
    contact_person = Column(String(100), nullable=True)
    review_status = Column(String(20), default="draft", index=True)
    is_published = Column(Boolean, default=False, index=True)
    published_at = Column(DateTime(timezone=True), nullable=True)

    current_version = Column(Integer, default=1)

    total_items = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    failure_count = Column(Integer, default=0)
    annotation_complete_rate = Column(Float, default=0.0)
    average_quality_score = Column(Float, nullable=True)
    reuse_count = Column(Integer, default=0, index=True)

    data_grade = Column(String(10), nullable=True, index=True)
    tags = Column(JSON, nullable=True)
    license_info = Column(String(200), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    robot_model = relationship("RobotModel", back_populates="datasets")
    scene = relationship("Scene", back_populates="datasets")
    items = relationship("DatasetItem", back_populates="dataset", cascade="all, delete-orphan")
    reuse_records = relationship("DatasetReuse", back_populates="dataset", cascade="all, delete-orphan")
    versions = relationship("DatasetVersion", back_populates="dataset", cascade="all, delete-orphan")
    reviews = relationship("DatasetReview", back_populates="dataset", cascade="all, delete-orphan")
    subscriptions = relationship("DatasetSubscription", back_populates="dataset", cascade="all, delete-orphan")
    upstream_derivations = relationship(
        "DatasetDerivation",
        foreign_keys="DatasetDerivation.upstream_dataset_id",
        back_populates="upstream_dataset",
        cascade="all, delete-orphan",
    )
    downstream_derivations = relationship(
        "DatasetDerivation",
        foreign_keys="DatasetDerivation.downstream_dataset_id",
        back_populates="downstream_dataset",
        cascade="all, delete-orphan",
    )


class DatasetItem(Base):
    __tablename__ = "dataset_items"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    operation_data_id = Column(Integer, ForeignKey("operation_data.id"), nullable=False, index=True)
    added_at = Column(DateTime(timezone=True), server_default=func.now())

    dataset = relationship("Dataset", back_populates="items")
    operation_data = relationship("OperationData", back_populates="dataset_items")


class DatasetReuse(Base):
    __tablename__ = "dataset_reuses"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    dataset_version_id = Column(Integer, ForeignKey("dataset_versions.id"), nullable=True, index=True)
    reusing_team = Column(String(100), nullable=False)
    purpose = Column(String(200), nullable=True)
    project_name = Column(String(200), nullable=True)
    reuse_date = Column(DateTime(timezone=True), server_default=func.now())
    notes = Column(Text, nullable=True)

    dataset = relationship("Dataset", back_populates="reuse_records")
    version = relationship("DatasetVersion", back_populates="reuse_records")


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    version_label = Column(String(20), nullable=False)
    change_description = Column(Text, nullable=True)

    total_items = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    failure_count = Column(Integer, default=0)
    annotation_complete_rate = Column(Float, default=0.0)
    average_quality_score = Column(Float, nullable=True)
    data_grade = Column(String(10), nullable=True)

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # 版本级发布标记：审核通过时置为 True；只有显式撤销发布才置 False。
    # 数据集改名、创建新版本草稿都不影响历史已发布版本，谱系边因此可固定在该版本上。
    is_published = Column(Boolean, nullable=False, default=False, index=True)

    dataset = relationship("Dataset", back_populates="versions")
    reuse_records = relationship("DatasetReuse", back_populates="version")
    upstream_derivations = relationship(
        "DatasetDerivation",
        foreign_keys="DatasetDerivation.upstream_version_id",
        back_populates="upstream_version",
    )


class DatasetReview(Base):
    __tablename__ = "dataset_reviews"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    dataset_version_id = Column(Integer, ForeignKey("dataset_versions.id"), nullable=True, index=True)
    action = Column(String(20), nullable=False, index=True)
    reviewer = Column(String(100), nullable=True)
    review_notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    dataset = relationship("Dataset", back_populates="reviews")
    version = relationship("DatasetVersion")


class DatasetSubscription(Base):
    __tablename__ = "dataset_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    subscriber_team = Column(String(100), nullable=False)
    contact_person = Column(String(100), nullable=True)
    notify_on_new_version = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    dataset = relationship("Dataset", back_populates="subscriptions")


class DatasetDerivation(Base):
    """数据集派生谱系边：downstream 基于 upstream 的某个已发布版本加工而成。

    边在创建时固定上游版本及其名称快照，之后数据集改名、发布新版本都不会改写
    历史边；上游撤销发布时通过 upstream_published 快照将该边标记为失效，边本身
    保留，谱系仍可追溯。(upstream_version_id, downstream_dataset_id) 唯一，
    重复边在数据库层被拒绝。
    """

    __tablename__ = "dataset_derivations"

    id = Column(Integer, primary_key=True, index=True)
    upstream_dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    upstream_version_id = Column(Integer, ForeignKey("dataset_versions.id"), nullable=False)
    downstream_dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)

    # 创建时固定的不可变快照，数据集改名或新版本发布均不改写历史关系
    upstream_name_snapshot = Column(String(200), nullable=False)
    upstream_version_label_snapshot = Column(String(20), nullable=False)
    downstream_name_snapshot = Column(String(200), nullable=False)
    purpose = Column(String(200), nullable=False)
    project_name = Column(String(200), nullable=True)
    created_by = Column(String(100), nullable=True)

    # 上游版本在创建时必须已发布；撤销发布后置为 False，边标记为失效但不删除
    upstream_published = Column(Boolean, nullable=False, default=True, index=True)
    invalidated_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    upstream_dataset = relationship(
        "Dataset", foreign_keys=[upstream_dataset_id], back_populates="upstream_derivations"
    )
    downstream_dataset = relationship(
        "Dataset", foreign_keys=[downstream_dataset_id], back_populates="downstream_derivations"
    )
    upstream_version = relationship(
        "DatasetVersion", foreign_keys=[upstream_version_id], back_populates="upstream_derivations"
    )

    @property
    def upstream_version_label(self) -> str:
        return self.upstream_version_label_snapshot

    @property
    def invalidated(self) -> bool:
        return not self.upstream_published

    __table_args__ = (
        UniqueConstraint(
            "upstream_version_id", "downstream_dataset_id",
            name="uq_derivation_upstream_version_downstream"
        ),
        Index("ix_derivations_up_ds", "upstream_dataset_id", "upstream_published"),
        Index("ix_derivations_down_ds", "downstream_dataset_id", "upstream_published"),
    )
