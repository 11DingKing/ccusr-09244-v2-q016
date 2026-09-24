"""派生谱系测试：多级分支、菱形合流、循环竞争、版本撤回、重启查询。"""

import threading
from datetime import datetime, timezone

import pytest

from app.database import SessionLocal
from app.models import DatasetDerivation
from app.schemas.dataset import DatasetDerivationCreate
from app.services import lineage as lineage_service
from app.services.lineage import LineageError

from conftest import make_version


# ---------- 基础创建与校验 ----------

def test_create_derivation_pins_version_and_purpose(db):
    a = make_version(db, "A")
    b = make_version(db, "B")
    edge = lineage_service.create_derivation(db, DatasetDerivationCreate(
        upstream_version_id=a.id, downstream_version_id=b.id,
        purpose="预训练", created_by="alice",
    ))
    assert edge.upstream_version_id == a.id
    assert edge.downstream_version_id == b.id
    assert edge.purpose == "预训练"
    assert edge.upstream_dataset_name == "A"
    assert edge.downstream_dataset_name == "B"
    assert edge.upstream_version_label == "v1"
    assert edge.is_active is True
    assert edge.invalidated_at is None


def test_derive_from_unpublished_version_rejected(db):
    a = make_version(db, "A", published=False)
    b = make_version(db, "B")
    with pytest.raises(LineageError) as ei:
        lineage_service.create_derivation(db, DatasetDerivationCreate(
            upstream_version_id=a.id, downstream_version_id=b.id, purpose="x"))
    assert "未发布" in ei.value.detail


def test_derive_to_unpublished_version_rejected(db):
    a = make_version(db, "A")
    b = make_version(db, "B", published=False)
    with pytest.raises(LineageError) as ei:
        lineage_service.create_derivation(db, DatasetDerivationCreate(
            upstream_version_id=a.id, downstream_version_id=b.id, purpose="x"))
    assert "未发布" in ei.value.detail


def test_self_edge_rejected(db):
    a = make_version(db, "A")
    with pytest.raises(LineageError):
        lineage_service.create_derivation(db, DatasetDerivationCreate(
            upstream_version_id=a.id, downstream_version_id=a.id, purpose="x"))


def test_missing_version_404(db):
    a = make_version(db, "A")
    with pytest.raises(LineageError) as ei:
        lineage_service.create_derivation(db, DatasetDerivationCreate(
            upstream_version_id=a.id, downstream_version_id=99999, purpose="x"))
    assert ei.value.status_code == 404


def test_duplicate_edge_rejected_with_409(db):
    a = make_version(db, "A")
    b = make_version(db, "B")
    payload = dict(upstream_version_id=a.id, downstream_version_id=b.id, purpose="p")
    lineage_service.create_derivation(db, DatasetDerivationCreate(**payload))
    with pytest.raises(LineageError) as ei:
        lineage_service.create_derivation(db, DatasetDerivationCreate(**payload))
    assert ei.value.status_code == 409


# ---------- 多级分支 ----------

def test_multi_level_branch_downstream(db):
    a = make_version(db, "A")
    b1 = make_version(db, "B1")
    b2 = make_version(db, "B2")
    c = make_version(db, "C")
    e1 = lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=a.id, downstream_version_id=b1.id, purpose="p1"))
    e2 = lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=a.id, downstream_version_id=b2.id, purpose="p2"))
    e3 = lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=b1.id, downstream_version_id=c.id, purpose="p3"))

    result = lineage_service.traverse_lineage(db, a.id, "downstream", 10, 100)
    depths = {layer["depth"]: layer for layer in result["layers"]}
    assert set(depths) == {0, 1, 2}
    assert {n["version_id"] for n in depths[1]["nodes"]} == {b1.id, b2.id}
    # 稳定顺序：按 edge_id 升序
    assert [n["edge_id"] for n in depths[1]["nodes"]] == sorted(n["edge_id"] for n in depths[1]["nodes"])
    assert [n["edge_id"] for n in depths[1]["nodes"]] == [e1.id, e2.id]
    assert depths[2]["nodes"][0]["version_id"] == c.id
    assert depths[2]["nodes"][0]["purpose"] == "p3"
    assert result["truncated"] is False
    assert result["depth_truncated"] is False


def test_upstream_direction_layers(db):
    a = make_version(db, "A")
    b = make_version(db, "B")
    c = make_version(db, "C")
    lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=a.id, downstream_version_id=b.id, purpose="p"))
    lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=b.id, downstream_version_id=c.id, purpose="p"))

    result = lineage_service.traverse_lineage(db, c.id, "upstream", 10, 100)
    vids = [n["version_id"] for layer in result["layers"] for n in layer["nodes"]]
    assert vids == [c.id, b.id, a.id]


# ---------- 菱形合流 ----------

def test_diamond_confluence(db):
    a = make_version(db, "A")
    m1 = make_version(db, "M1")
    m2 = make_version(db, "M2")
    z = make_version(db, "Z")
    lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=a.id, downstream_version_id=m1.id, purpose="left"))
    lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=a.id, downstream_version_id=m2.id, purpose="right"))
    lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=m1.id, downstream_version_id=z.id, purpose="merge-l"))
    lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=m2.id, downstream_version_id=z.id, purpose="merge-r"))

    result = lineage_service.traverse_lineage(db, a.id, "downstream", 10, 100)
    depths = {layer["depth"]: layer for layer in result["layers"]}
    assert {n["version_id"] for n in depths[1]["nodes"]} == {m1.id, m2.id}
    # 合流层：Z 只出现一个版本，但通过两条边各产生一个条目
    z_entries = depths[2]["nodes"]
    assert [n["version_id"] for n in z_entries] == [z.id, z.id]
    assert {n["purpose"] for n in z_entries} == {"merge-l", "merge-r"}
    assert depths[2]["total_count"] == 2

    # 反向：从 Z 看上游同样菱形展开
    up = lineage_service.traverse_lineage(db, z.id, "upstream", 10, 100)
    up_depths = {layer["depth"]: layer for layer in up["layers"]}
    assert {n["version_id"] for n in up_depths[1]["nodes"]} == {m1.id, m2.id}
    assert up_depths[2]["nodes"][0]["version_id"] == a.id


def test_version_appears_only_at_shallowest_depth(db):
    a = make_version(db, "A")
    b = make_version(db, "B")
    c = make_version(db, "C")
    lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=a.id, downstream_version_id=b.id, purpose="p1"))
    lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=b.id, downstream_version_id=c.id, purpose="p2"))
    # 额外一条 a->c 的跨层边
    e_ac = lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=a.id, downstream_version_id=c.id, purpose="shortcut"))

    result = lineage_service.traverse_lineage(db, a.id, "downstream", 10, 100)
    depths = {layer["depth"]: layer for layer in result["layers"]}
    # C 在第 1 层（最短深度）展示；b->c 跨层边只合流计数，不产生第 2 层
    assert [n["version_id"] for n in depths[1]["nodes"]] == sorted([b.id, c.id])
    assert 2 not in depths
    assert len(result["layers"]) == 2
    # 经 b->c 的长边计入总数但被合并
    assert result["total_edges"] == 3
    assert result["merged_edges"] == 1
    # shortcut 边仍可在第一层看到
    assert e_ac.id in {n["edge_id"] for n in depths[1]["nodes"]}


# ---------- 循环检测 ----------

def test_direct_cycle_rejected(db):
    a = make_version(db, "A")
    b = make_version(db, "B")
    lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=a.id, downstream_version_id=b.id, purpose="p"))
    with pytest.raises(LineageError) as ei:
        lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=b.id, downstream_version_id=a.id, purpose="reverse"))
    assert "循环" in ei.value.detail


def test_indirect_cycle_rejected(db):
    a = make_version(db, "A")
    b = make_version(db, "B")
    c = make_version(db, "C")
    lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=a.id, downstream_version_id=b.id, purpose="p"))
    lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=b.id, downstream_version_id=c.id, purpose="p"))
    with pytest.raises(LineageError) as ei:
        lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=c.id, downstream_version_id=a.id, purpose="loop"))
    assert "循环" in ei.value.detail


# ---------- 并发相反方向竞争 ----------

def test_concurrent_reverse_edges_never_form_cycle(db):
    a = make_version(db, "A")
    b = make_version(db, "B")

    errors = []
    barrier = threading.Barrier(2)

    def worker(up, down):
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            lineage_service.create_derivation(
                session,
                DatasetDerivationCreate(upstream_version_id=up, downstream_version_id=down, purpose="race"),
            )
        except LineageError as exc:
            errors.append(exc)
        except Exception as exc:  # pragma: no cover - 死锁/锁错误也算失败
            errors.append(exc)
        finally:
            session.close()

    t1 = threading.Thread(target=worker, args=(a.id, b.id))
    t2 = threading.Thread(target=worker, args=(b.id, a.id))
    t1.start(); t2.start()
    t1.join(timeout=30); t2.join(timeout=30)

    assert not t1.is_alive() and not t2.is_alive()
    # 恰好一条成功，另一条因循环被拒（绝不能两条都成功形成环）
    assert len(errors) == 1
    assert "循环" in errors[0].detail
    edges = db.query(DatasetDerivation).all()
    assert len(edges) == 1

    # 校验图确实无环（再创建同边应报重复 409，反向报循环）
    with pytest.raises(LineageError) as dup:
        lineage_service.create_derivation(
            db, DatasetDerivationCreate(
                upstream_version_id=a.id, downstream_version_id=b.id, purpose="race"))
    # 无论哪边成功，重复边都是 409 或循环 400——但不能产生第二条边
    assert dup.value.status_code in (400, 409)
    assert db.query(DatasetDerivation).count() == 1


def test_concurrent_identical_edges_dedup(db):
    a = make_version(db, "A")
    b = make_version(db, "B")
    errors = []
    barrier = threading.Barrier(3)

    def worker():
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            lineage_service.create_derivation(
                session,
                DatasetDerivationCreate(
                    upstream_version_id=a.id, downstream_version_id=b.id, purpose="same"),
            )
        except LineageError as exc:
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    assert db.query(DatasetDerivation).count() == 1
    assert len(errors) == 2
    assert all(e.status_code == 409 for e in errors)


# ---------- 版本撤回与失效链路 ----------

def test_unpublish_invalidates_links_and_keeps_history(db):
    a = make_version(db, "A")
    b = make_version(db, "B")
    c = make_version(db, "C")
    e1 = lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=a.id, downstream_version_id=b.id, purpose="p1"))
    lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=b.id, downstream_version_id=c.id, purpose="p2"))

    # 撤销 B 的发布
    b.is_published = False
    b.unpublished_at = datetime.now(timezone.utc)
    count = lineage_service.invalidate_edges_for_version(db, b.id, "撤销发布")
    db.commit()
    assert count == 2

    # 边没有被删除
    assert db.query(DatasetDerivation).count() == 2

    result = lineage_service.traverse_lineage(db, a.id, "downstream", 10, 100)
    depths = {layer["depth"]: layer for layer in result["layers"]}
    b_nodes = [n for n in depths[1]["nodes"] if n["version_id"] == b.id]
    assert b_nodes[0]["invalidated"] is True
    assert b_nodes[0]["version_published"] is False
    assert b_nodes[0]["edge_active"] is False
    assert b_nodes[0]["edge_invalidate_reason"] == "撤销发布"
    assert b_nodes[0]["path_invalidated"] is True

    # C 版本本身仍发布，但经由失效的 B 到达，链路也应标出失效
    c_nodes = [n for n in depths[2]["nodes"] if n["version_id"] == c.id]
    assert c_nodes[0]["version_published"] is True
    assert c_nodes[0]["path_invalidated"] is True

    # 从 C 向上游看：B 同样标失效，A 仍健康
    up = lineage_service.traverse_lineage(db, c.id, "upstream", 10, 100)
    up_depths = {layer["depth"]: layer for layer in up["layers"]}
    a_node = up_depths[2]["nodes"][0]
    assert a_node["version_id"] == a.id
    assert a_node["path_invalidated"] is True  # 整条链途经失效 B


def test_unpublished_version_cannot_be_linked_afterwards(db):
    a = make_version(db, "A")
    b = make_version(db, "B")
    lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=a.id, downstream_version_id=b.id, purpose="p"))
    b.is_published = False
    lineage_service.invalidate_edges_for_version(db, b.id, "撤销发布")
    db.commit()

    c = make_version(db, "C")
    with pytest.raises(LineageError) as ei:
        lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=b.id, downstream_version_id=c.id, purpose="p"))
    assert "未发布" in ei.value.detail


# ---------- 改名与新版本不改写历史 ----------

def test_rename_and_new_version_do_not_rewrite_history(db):
    a = make_version(db, "A", version_label="1.0")
    b = make_version(db, "B", version_label="1.0")
    edge = lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=a.id, downstream_version_id=b.id, purpose="复用v1"))

    # 数据集改名
    a.dataset.name = "A-改名后"
    b.dataset.name = "B-改名后"
    # 各自发布新版本
    a2 = make_version(db, "A", version_label="1.1")
    b2 = make_version(db, "B", version_label="1.1")
    db.commit()

    db.expire_all()
    edge = db.query(DatasetDerivation).filter(DatasetDerivation.id == edge.id).first()
    # 历史边仍指向原版本与原名称快照
    assert edge.upstream_version_id == a.id
    assert edge.downstream_version_id == b.id
    assert edge.upstream_dataset_name == "A"
    assert edge.downstream_dataset_name == "B"
    assert edge.upstream_version_label == "1.0"
    assert edge.purpose == "复用v1"
    assert a2.id != a.id and b2.id != b.id

    # 谱系查询展示的也是快照名称
    result = lineage_service.traverse_lineage(db, a.id, "downstream", 10, 100)
    node = result["layers"][1]["nodes"][0]
    assert node["dataset_name"] == "B"
    assert node["version_label"] == "1.0"

    # 新版本之间默认无边
    assert db.query(DatasetDerivation).filter(
        DatasetDerivation.upstream_version_id == a2.id).count() == 0


# ---------- 截断信息与稳定顺序 ----------

def test_per_layer_truncation(db):
    a = make_version(db, "A")
    kids = [make_version(db, f"K{i}") for i in range(5)]
    for k in kids:
        lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=a.id, downstream_version_id=k.id, purpose="p"))

    result = lineage_service.traverse_lineage(db, a.id, "downstream", 10, 3)
    layer1 = result["layers"][1]
    assert layer1["total_count"] == 5
    assert layer1["returned_count"] == 3
    assert layer1["truncated"] is True
    assert [n["version_id"] for n in layer1["nodes"]] == sorted(k.id for k in kids)[:3]
    assert result["truncated"] is True
    assert result["returned_edges"] == 3


def test_depth_truncation(db):
    versions = [make_version(db, f"D{i}") for i in range(4)]
    for up, down in zip(versions, versions[1:]):
        lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=up.id, downstream_version_id=down.id, purpose="p"))

    result = lineage_service.traverse_lineage(db, versions[0].id, "downstream", max_depth=2, per_layer_limit=100)
    assert len(result["layers"]) == 3
    assert result["depth_truncated"] is True
    assert result["truncated"] is True


def test_listing_stable_order(db):
    a = make_version(db, "A")
    targets = [make_version(db, f"O{i}") for i in range(5)]
    for t in targets:
        lineage_service.create_derivation(db, DatasetDerivationCreate(upstream_version_id=a.id, downstream_version_id=t.id, purpose="p"))
    rows = db.query(DatasetDerivation).order_by(DatasetDerivation.id.asc()).all()
    assert [r.id for r in rows] == sorted(r.id for r in rows)


# ---------- 重启后查询（持久化） ----------

def test_lineage_survives_engine_restart(tmp_path):
    db_path = tmp_path / "restart.db"
    url = f"sqlite:///{db_path}"

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base

    engine1 = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine1)
    Session1 = sessionmaker(bind=engine1)
    s1 = Session1()
    try:
        a = make_version(s1, "RA")
        b = make_version(s1, "RB")
        c = make_version(s1, "RC")
        lineage_service.create_derivation(s1, DatasetDerivationCreate(upstream_version_id=a.id, downstream_version_id=b.id, purpose="p"))
        lineage_service.create_derivation(s1, DatasetDerivationCreate(upstream_version_id=b.id, downstream_version_id=c.id, purpose="p"))
        a_id, b_id, c_id = a.id, b.id, c.id
    finally:
        s1.close()
        engine1.dispose()

    # 重新打开，模拟进程重启
    engine2 = create_engine(url, connect_args={"check_same_thread": False})
    Session2 = sessionmaker(bind=engine2)
    s2 = Session2()
    try:
        assert s2.query(DatasetDerivation).count() == 2
        result = lineage_service.traverse_lineage(s2, a_id, "downstream", 10, 100)
        vids = [n["version_id"] for layer in result["layers"] for n in layer["nodes"]]
        assert vids == [a_id, b_id, c_id]

        # 重启后成环检测依然有效
        with pytest.raises(LineageError):
            lineage_service.create_derivation(
                s2, DatasetDerivationCreate(
                    upstream_version_id=c_id, downstream_version_id=a_id, purpose="loop"))
    finally:
        s2.close()
        engine2.dispose()
