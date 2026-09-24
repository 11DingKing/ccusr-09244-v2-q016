import threading

from app.database import engine, immediate_engine, SessionLocal
from app.models import DatasetVersion
from app.services.lineage import apply_publication_revocation

API = "/api/v1"


def edge_body(upstream, version, downstream, purpose="再加工训练", **extra):
    body = {
        "upstream_dataset_id": upstream.id,
        "upstream_version_id": version.id,
        "downstream_dataset_id": downstream.id,
        "purpose": purpose,
    }
    body.update(extra)
    return body


def create_edge(client, body, expected=200):
    resp = client.post(f"{API}/dataset-derivations", json=body)
    assert resp.status_code == expected, resp.text
    return resp.json() if expected < 400 else resp


def ids_by_depth(payload):
    return {
        layer["depth"]: sorted(node["dataset_id"] for node in layer["nodes"])
        for layer in payload["layers"]
    }


# ---------- 基础建边与不可变快照 ----------

def test_create_derivation_fixes_version_purpose_and_snapshots(client, factory):
    a, av = factory("上游数据集")
    b, _ = factory("下游数据集")
    edge = create_edge(client, edge_body(a, av, b, purpose="模型预训练"))
    assert edge["upstream_version_id"] == av.id
    assert edge["upstream_version_label"] == "1.0"
    assert edge["upstream_name_snapshot"] == "上游数据集"
    assert edge["downstream_name_snapshot"] == "下游数据集"
    assert edge["purpose"] == "模型预训练"
    assert edge["invalidated"] is False
    assert edge["upstream_published"] is True


def test_reject_self_derivation(client, factory):
    a, av = factory("A")
    resp = create_edge(client, edge_body(a, av, a), expected=400)
    assert "同一个数据集" in resp.json()["detail"]


def test_reject_unpublished_and_foreign_version(client, factory):
    a, av = factory("A草稿", published=False)
    b, _ = factory("B")
    resp = create_edge(client, edge_body(a, av, b), expected=400)
    assert "未发布" in resp.json()["detail"]

    c, cv = factory("C")
    # 版本 cv 属于 C，却被声称为上游 b 的版本引用
    resp = client.post(f"{API}/dataset-derivations", json={
        "upstream_dataset_id": b.id,
        "upstream_version_id": cv.id,
        "downstream_dataset_id": c.id,
        "purpose": "跨版本",
    })
    assert resp.status_code == 400


def test_reject_duplicate_edge_and_empty_purpose(client, factory):
    a, av = factory("A")
    b, _ = factory("B")
    create_edge(client, edge_body(a, av, b, purpose="用途一"))
    resp = create_edge(client, edge_body(a, av, b, purpose="用途二"), expected=409)
    assert "重复" in resp.json()["detail"]

    # 不同下游仍可建边
    c, _ = factory("C")
    create_edge(client, edge_body(a, av, c, purpose="用途三"))

    resp = client.post(f"{API}/dataset-derivations", json={
        "upstream_dataset_id": a.id,
        "upstream_version_id": av.id,
        "downstream_dataset_id": c.id,
        "purpose": "",
    })
    assert resp.status_code == 422  # 空用途被 schema 拒绝

    # 纯空白用途由服务层归一化后拒绝
    resp = client.post(f"{API}/dataset-derivations", json={
        "upstream_dataset_id": a.id,
        "upstream_version_id": av.id,
        "downstream_dataset_id": c.id,
        "purpose": "   ",
    })
    assert resp.status_code == 400


# ---------- 多级分支与菱形合流 ----------

def test_downstream_multilevel_branch_and_diamond(client, factory):
    # A → B → D，A → C → D（菱形），A → E（分支）
    a, av = factory("A")
    b, bv = factory("B")
    c, cv = factory("C")
    d, _ = factory("D")
    e, _ = factory("E")
    for up, uv, down in [
        (a, av, b), (a, av, c), (b, bv, d), (c, cv, d), (a, av, e)
    ]:
        create_edge(client, edge_body(up, uv, down))

    payload = client.get(f"{API}/datasets/{a.id}/lineage?direction=downstream").json()
    assert ids_by_depth(payload) == {1: sorted([b.id, c.id, e.id]), 2: [d.id]}

    depth2 = payload["layers"][1]["nodes"]
    d_node = next(n for n in depth2 if n["dataset_id"] == d.id)
    # 菱形合流：D 由 B、C 两条边汇入
    assert len(d_node["edges"]) == 2
    assert {e["upstream_dataset_id"] for e in d_node["edges"]} == {b.id, c.id}
    assert payload["total_edges"] == 5
    assert payload["truncated"] is False


def test_upstream_direction_traces_sources(client, factory):
    a, av = factory("A")
    b, bv = factory("B")
    c, cv = factory("C")
    d, _ = factory("D")
    for up, uv, down in [(a, av, b), (a, av, c), (b, bv, d), (c, cv, d)]:
        create_edge(client, edge_body(up, uv, down))

    payload = client.get(f"{API}/datasets/{d.id}/lineage?direction=upstream").json()
    assert payload["root"]["dataset_id"] == d.id
    assert ids_by_depth(payload) == {1: sorted([b.id, c.id]), 2: [a.id]}


def test_max_depth_limits_traversal(client, factory):
    chain = []
    prev_ds = prev_ver = None
    for i in range(4):
        ds, ver = factory(f"N{i}")
        if prev_ds is not None:
            create_edge(client, edge_body(prev_ds, prev_ver, ds))
        prev_ds, prev_ver = ds, ver
        chain.append(ds)
    payload = client.get(
        f"{API}/datasets/{chain[0].id}/lineage?direction=downstream&max_depth=1"
    ).json()
    assert ids_by_depth(payload) == {1: [chain[1].id]}


# ---------- 循环 ----------

def test_reject_cycle_sequential(client, factory):
    a, av = factory("A")
    b, bv = factory("B")
    c, cv = factory("C")
    create_edge(client, edge_body(a, av, b))
    create_edge(client, edge_body(b, bv, c))
    resp = create_edge(client, edge_body(c, cv, a), expected=409)
    assert "循环" in resp.json()["detail"]


def test_concurrent_opposite_edges_never_form_cycle(client, factory):
    a, av = factory("并发A")
    b, bv = factory("并发B")
    clients = [client, client.__class__(client.app)]
    barrier = threading.Barrier(2)
    statuses = {}

    def make(local, key, up, uv, down):
        barrier.wait()
        statuses[key] = local.post(f"{API}/dataset-derivations", json=edge_body(up, uv, down, purpose=key)).status_code

    threads = [
        threading.Thread(target=make, args=(clients[0], "a->b", a, av, b)),
        threading.Thread(target=make, args=(clients[1], "b->a", b, bv, a)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(statuses.values()) == [200, 409], statuses
    # 最终只有一条边，谱系无环
    listing = client.get(f"{API}/dataset-derivations").json()
    assert listing["total"] == 1


# ---------- 撤销发布导致链路失效 ----------

def test_unpublish_invalidates_chain_and_marks_nodes(client, factory, db):
    a, av = factory("根A")
    b, bv = factory("中间B")
    c, _ = factory("末端C")
    create_edge(client, edge_body(a, av, b))
    create_edge(client, edge_body(b, bv, c))

    resp = client.post(f"{API}/datasets/{a.id}/unpublish")
    assert resp.status_code == 200

    payload = client.get(f"{API}/datasets/{a.id}/lineage?direction=downstream").json()
    node_at = {}
    for layer in payload["layers"]:
        for node in layer["nodes"]:
            node_at[node["dataset_id"]] = node

    # A→B 边失效：B 与其后的 C 都不再有全有效路径
    assert node_at[b.id]["invalidated"] is True
    assert node_at[c.id]["invalidated"] is True
    edge_ab = node_at[b.id]["edges"][0]
    assert edge_ab["invalidated"] is True
    assert edge_ab["invalidated_at"] is not None
    # B→C 边本身仍有效，只是整条链路被上游断链拖累
    edge_bc = node_at[c.id]["edges"][0]
    assert edge_bc["invalidated"] is False

    # 失效版本不能再被新关系引用
    d, _ = factory("D")
    resp = client.post(f"{API}/dataset-derivations", json=edge_body(a, av, d, purpose="再引用"))
    assert resp.status_code == 400

    # include_invalidated=false 时失效边被排除，B/C 不再出现在下游谱系
    filtered = client.get(
        f"{API}/datasets/{a.id}/lineage?direction=downstream&include_invalidated=false"
    ).json()
    all_nodes = [n for layer in filtered["layers"] for n in layer["nodes"]]
    assert all_nodes == []


def test_upstream_query_marks_revoked_source(client, factory):
    a, av = factory("源A")
    b, bv = factory("源B")
    c, _ = factory("汇C")
    create_edge(client, edge_body(a, av, b))
    create_edge(client, edge_body(b, bv, c))
    client.post(f"{API}/datasets/{a.id}/unpublish")

    payload = client.get(f"{API}/datasets/{c.id}/lineage?direction=upstream").json()
    node_at = {n["dataset_id"]: n for layer in payload["layers"] for n in layer["nodes"]}
    assert node_at[b.id]["invalidated"] is False
    assert node_at[a.id]["invalidated"] is True


def test_revoke_review_action_invalidates_edges(client, factory):
    a, av = factory("待撤A")
    b, _ = factory("待撤B")
    create_edge(client, edge_body(a, av, b))
    # revoke 仅允许 pending_review/approved，先置为 pending_review 再 revoke 不适用，
    # 直接走 unpublish 的等价服务：这里通过审核状态机校验 revoke 在 approved 下可用。
    resp = client.post(f"{API}/datasets/{a.id}/review", json={"action": "revoke"})
    assert resp.status_code == 200
    listing = client.get(
        f"{API}/dataset-derivations", params={"upstream_dataset_id": a.id}
    ).json()
    assert listing["items"][0]["invalidated"] is True


# ---------- 历史不被改名 / 新版本改写 ----------

def test_history_survives_rename_and_new_version(client, factory, db):
    a, av = factory("原名A", version_label="1.0")
    b, _ = factory("下游B")
    create_edge(client, edge_body(a, av, b, purpose="固定用途"))

    # 数据集改名并发布新版本
    a.name = "改名后的A"
    db.flush()
    v2 = DatasetVersion(
        dataset_id=a.id, version_number=2, version_label="2.0", is_published=True
    )
    db.add(v2)
    db.commit()

    edge = client.get(
        f"{API}/dataset-derivations", params={"downstream_dataset_id": b.id}
    ).json()["items"][0]
    assert edge["upstream_name_snapshot"] == "原名A"
    assert edge["upstream_version_label"] == "1.0"
    assert edge["purpose"] == "固定用途"
    assert edge["upstream_version_id"] == av.id


def test_new_draft_version_does_not_block_old_published_version(client, factory, db):
    # 上游已发布 v1；数据集进入新版本草稿期（is_published=False），但 v1 仍可被引用。
    a, av = factory("上游", version_label="1.0")
    a.is_published = False
    a.review_status = "draft"
    draft = DatasetVersion(
        dataset_id=a.id, version_number=2, version_label="2.0", is_published=False
    )
    db.add(draft)
    db.commit()

    b, _ = factory("引用方")
    edge = create_edge(client, edge_body(a, av, b, purpose="继续引用旧版"))
    assert edge["upstream_version_id"] == av.id

    # 草稿版本 v2 仍然不能被跨越引用
    c, _ = factory("另一引用方")
    resp = create_edge(client, edge_body(a, draft, c, purpose="引用草稿"), expected=400)
    assert "未发布" in resp.json()["detail"]


# ---------- 稳定顺序与截断 ----------

def test_stable_order_and_truncation(client, factory):
    root, rv = factory("根")
    downstream = []
    for i in range(5):
        ds, _ = factory(f"支{i}")
        create_edge(client, edge_body(root, rv, ds, purpose=f"用途{i}"))
        downstream.append(ds)

    payload = client.get(
        f"{API}/datasets/{root.id}/lineage?direction=downstream&limit=2"
    ).json()
    assert payload["total_edges"] == 5
    assert payload["returned_edges"] == 2
    assert payload["truncated"] is True
    edges = [n["edges"][0] for layer in payload["layers"] for n in layer["nodes"]]
    assert [e["id"] for e in edges] == sorted(e["id"] for e in edges)

    listing = client.get(f"{API}/dataset-derivations?skip=1&limit=2").json()
    assert listing["total"] == 5
    assert listing["truncated"] is True
    assert [item["id"] for item in listing["items"]] == sorted(item["id"] for item in listing["items"])


# ---------- 列表过滤 ----------

def test_list_filter_by_upstream_and_downstream(client, factory):
    a, av = factory("LA")
    b, bv = factory("LB")
    c, _ = factory("LC")
    create_edge(client, edge_body(a, av, b))
    create_edge(client, edge_body(b, bv, c))

    up = client.get(f"{API}/dataset-derivations", params={"upstream_dataset_id": a.id}).json()
    assert up["total"] == 1 and up["items"][0]["downstream_dataset_id"] == b.id

    down = client.get(f"{API}/dataset-derivations", params={"downstream_dataset_id": c.id}).json()
    assert down["total"] == 1 and down["items"][0]["upstream_dataset_id"] == b.id

    active = client.get(
        f"{API}/dataset-derivations",
        params={"upstream_dataset_id": a.id, "include_invalidated": "false"},
    ).json()
    assert active["total"] == 1
    direct = SessionLocal()
    try:
        apply_publication_revocation(direct, a.id)
        direct.commit()
    finally:
        direct.close()
    active2 = client.get(
        f"{API}/dataset-derivations",
        params={"upstream_dataset_id": a.id, "include_invalidated": "false"},
    ).json()
    assert active2["total"] == 0


# ---------- 重启后查询 ----------

def test_lineage_survives_connection_restart(client, factory):
    a, av = factory("重启A")
    b, bv = factory("重启B")
    c, _ = factory("重启C")
    create_edge(client, edge_body(a, av, b))
    create_edge(client, edge_body(b, bv, c))

    # 模拟服务重启：释放全部连接池并重新建表（create_all 幂等），数据应落盘保留。
    engine.dispose()
    immediate_engine.dispose()
    from app.database import Base
    Base.metadata.create_all(bind=engine)

    fresh = client.__class__(client.app)
    payload = fresh.get(f"{API}/datasets/{a.id}/lineage?direction=downstream").json()
    assert ids_by_depth(payload) == {1: [b.id], 2: [c.id]}
    assert payload["total_edges"] == 2


def test_lineage_not_found(client, factory):
    a, _ = factory("X")
    resp = client.get(f"{API}/datasets/{a.id}/lineage?direction=sideways")
    assert resp.status_code == 400
    resp = client.get(f"{API}/datasets/999999/lineage")
    assert resp.status_code == 404
