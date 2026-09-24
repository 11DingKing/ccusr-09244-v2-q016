"""派生谱系 HTTP 接口集成测试：走完整发布流程后建边、查询、撤回。"""

import pytest


def _create_published_dataset(client, name: str, *, owner_team: str = "team-x") -> dict:
    """创建数据集并通过 提交→审核通过 发布，返回含 published_version_id 的信息。"""
    rm = client.post("/api/v1/robot-models", json={
        "name": f"RM-{name}", "manufacturer": "M"}).json()
    sc = client.post("/api/v1/scenes", json={
        "name": f"SC-{name}", "category": "c"}).json()
    sk = client.post("/api/v1/skills", json={
        "name": f"SK-{name}", "category": "c"}).json()
    op = client.post("/api/v1/operations", json={
        "robot_model_id": rm["id"],
        "scene_id": sc["id"],
        "skill_id": sk["id"],
        "motion_trajectory": {"x": 1},
        "perception_records": {"y": 2},
        "timestamp_start": "2025-01-01T00:00:00Z",
        "timestamp_end": "2025-01-01T00:01:00Z",
    }).json()

    ds = client.post("/api/v1/datasets", json={
        "name": name,
        "owner_team": owner_team,
        "robot_model_id": rm["id"],
        "scene_id": sc["id"],
        "skill_id": sk["id"],
        "operation_data_ids": [op["id"]],
    }).json()

    r = client.post(f"/api/v1/datasets/{ds['id']}/review", json={"action": "submit"})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/v1/datasets/{ds['id']}/review",
                    json={"action": "approve", "reviewer": "boss"})
    assert r.status_code == 200, r.text
    review = r.json()

    versions = client.get(f"/api/v1/datasets/{ds['id']}/versions").json()
    published = [v for v in versions if v["id"] == review["dataset_version_id"]]
    assert published and published[0]["id"]
    return {"dataset": ds, "version_id": review["dataset_version_id"]}


def _derive(client, upstream_vid, downstream_vid, purpose="p", expected=200):
    r = client.post("/api/v1/dataset-derivations", json={
        "upstream_version_id": upstream_vid,
        "downstream_version_id": downstream_vid,
        "purpose": purpose,
        "created_by": "tester",
    })
    assert r.status_code == expected, r.text
    return r


def test_full_publish_derive_lineage_flow(client):
    a = _create_published_dataset(client, "BaseSet")
    b = _create_published_dataset(client, "MidSet")
    c = _create_published_dataset(client, "LeafSet")

    edge1 = _derive(client, a["version_id"], b["version_id"], "预训练").json()
    _derive(client, b["version_id"], c["version_id"], "微调")

    assert edge1["upstream_dataset_name"] == "BaseSet"
    assert edge1["upstream_version_label"] == "1.0"
    assert edge1["is_active"] is True

    # 下游方向分层
    r = client.get(f"/api/v1/dataset-versions/{a['version_id']}/lineage",
                   params={"direction": "downstream"})
    assert r.status_code == 200
    body = r.json()
    vids = [n["version_id"] for layer in body["layers"] for n in layer["nodes"]]
    assert vids == [a["version_id"], b["version_id"], c["version_id"]]
    assert body["layers"][1]["nodes"][0]["purpose"] == "预训练"
    assert body["truncated"] is False

    # 上游方向
    r = client.get(f"/api/v1/dataset-versions/{c['version_id']}/lineage",
                   params={"direction": "upstream"})
    body = r.json()
    vids = [n["version_id"] for layer in body["layers"] for n in layer["nodes"]]
    assert vids == [c["version_id"], b["version_id"], a["version_id"]]


def test_rejects_duplicate_cycle_and_unpublished_over_http(client):
    a = _create_published_dataset(client, "SetA")
    b = _create_published_dataset(client, "SetB")

    dup = _derive(client, a["version_id"], b["version_id"], expected=200)
    # 重复边 409
    r = _derive(client, a["version_id"], b["version_id"], expected=409)
    assert "重复" in r.json()["detail"]
    # 反向边成环 400
    r = _derive(client, b["version_id"], a["version_id"], expected=400)
    assert "循环" in r.json()["detail"]

    # 未发布版本：新数据集（草稿）
    rm = client.get("/api/v1/robot-models").json()[0]
    sc = client.get("/api/v1/scenes").json()[0]
    draft = client.post("/api/v1/datasets", json={
        "name": "DraftSet", "owner_team": "t",
        "robot_model_id": rm["id"], "scene_id": sc["id"],
    }).json()
    draft_vid = client.get(f"/api/v1/datasets/{draft['id']}/versions").json()[-1]["id"]
    r = _derive(client, a["version_id"], draft_vid, expected=400)
    assert "未发布" in r.json()["detail"]

    # 不存在的版本 404
    assert _derive(client, a["version_id"], 999999, expected=404)
    # 自环 400
    assert _derive(client, a["version_id"], a["version_id"], expected=400)


def test_unpublish_marks_lineage_invalid_over_http(client):
    a = _create_published_dataset(client, "UpSet")
    b = _create_published_dataset(client, "DownSet")
    _derive(client, a["version_id"], b["version_id"], "复用")

    # 撤销上游数据集发布
    r = client.post(f"/api/v1/datasets/{a['dataset']['id']}/unpublish")
    assert r.status_code == 200

    r = client.get(f"/api/v1/dataset-versions/{a['version_id']}/lineage",
                   params={"direction": "downstream"})
    body = r.json()
    node = body["layers"][1]["nodes"][0]
    # B 版本自身仍发布，但与被撤回的 A 之间的边失效，整条链路标失效
    assert node["version_published"] is True
    assert node["invalidated"] is False
    assert node["edge_active"] is False
    assert node["edge_invalidate_reason"] == "撤销发布"
    assert node["path_invalidated"] is True

    # 边仍然可列出（历史不删除）
    r = client.get("/api/v1/dataset-derivations",
                   params={"upstream_version_id": a["version_id"]})
    assert len(r.json()) == 1
    assert r.json()[0]["is_active"] is False


def test_derivation_listing_filters_and_order(client):
    a = _create_published_dataset(client, "RootSet")
    kids = [_create_published_dataset(client, f"ChildSet{i}") for i in range(4)]
    for k in kids:
        _derive(client, a["version_id"], k["version_id"], purpose="p")

    r = client.get("/api/v1/dataset-derivations",
                   params={"upstream_version_id": a["version_id"]})
    rows = r.json()
    assert [row["id"] for row in rows] == sorted(row["id"] for row in rows)
    assert len(rows) == 4

    # 不相关过滤为空
    r = client.get("/api/v1/dataset-derivations",
                   params={"downstream_version_id": a["version_id"]})
    assert r.json() == []


def test_lineage_truncation_metadata_over_http(client):
    a = _create_published_dataset(client, "TrunkSet")
    kids = [_create_published_dataset(client, f"BranchSet{i}") for i in range(4)]
    for k in kids:
        _derive(client, a["version_id"], k["version_id"], purpose="p")

    r = client.get(f"/api/v1/dataset-versions/{a['version_id']}/lineage",
                   params={"direction": "downstream", "per_layer_limit": 2})
    body = r.json()
    layer1 = body["layers"][1]
    assert layer1["total_count"] == 4
    assert layer1["returned_count"] == 2
    assert layer1["truncated"] is True
    assert body["truncated"] is True
    # 顺序稳定：两次查询结果一致
    r2 = client.get(f"/api/v1/dataset-versions/{a['version_id']}/lineage",
                    params={"direction": "downstream", "per_layer_limit": 2})
    assert [n["version_id"] for n in r2.json()["layers"][1]["nodes"]] == \
           [n["version_id"] for n in layer1["nodes"]]

    # 深度截断
    r = client.get(f"/api/v1/dataset-versions/{a['version_id']}/lineage",
                   params={"direction": "downstream", "max_depth": 0})
    assert r.status_code == 422  # ge=1 校验


def test_rename_does_not_change_pinned_edge(client):
    a = _create_published_dataset(client, "OldNameA")
    b = _create_published_dataset(client, "OldNameB")
    _derive(client, a["version_id"], b["version_id"], "fixed")

    # 真实改名流程：发布新版本 -> 草稿期改名 -> 重新审核发布
    r = client.post(f"/api/v1/datasets/{a['dataset']['id']}/versions",
                    json={"change_description": "改名"})
    assert r.status_code == 200, r.text
    r = client.put(f"/api/v1/datasets/{a['dataset']['id']}", json={"name": "NewNameA"})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/v1/datasets/{a['dataset']['id']}/review", json={"action": "submit"})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/v1/datasets/{a['dataset']['id']}/review",
                    json={"action": "approve", "reviewer": "boss"})
    assert r.status_code == 200, r.text

    # 旧边仍固定建边时的名称与版本，不受改名/新版本影响
    r = client.get("/api/v1/dataset-derivations",
                   params={"upstream_version_id": a["version_id"]})
    edge = r.json()[0]
    assert edge["upstream_dataset_name"] == "OldNameA"
    assert edge["upstream_version_label"] == "1.0"
    assert edge["is_active"] is True

    # 从旧版本出发的谱系展示快照名
    r = client.get(f"/api/v1/dataset-versions/{a['version_id']}/lineage")
    node = r.json()["layers"][1]["nodes"][0]
    assert node["dataset_name"] == "OldNameB"
