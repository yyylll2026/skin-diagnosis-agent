"""个人健康记录 API 测试：验证 CRUD、导出与按用户隔离。

``auth_client`` 夹具定义在 ``tests/conftest.py``，供本文件与
``test_health_trends.py`` 共用。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

API = "/api/v1/health-records"


def _register(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/v1/auth/register", json={"username": username, "password": "passw0rd1"}
    )
    assert response.status_code == 200


def _payload(symptoms: str = "面颊出现红斑和轻微瘙痒") -> dict[str, str]:
    return {
        "recorded_on": "2026-10-01",
        "body_site": "左侧面颊",
        "symptoms": symptoms,
        "severity": "mild",
        "notes": "晚上更明显",
    }


def test_health_record_crud_and_export_are_scoped_to_current_user(
    auth_client: TestClient,
) -> None:
    """不同用户只能访问自己的记录，导出也只包含本人数据。"""
    _register(auth_client, "record_owner")
    created = auth_client.post(API, json=_payload())
    assert created.status_code == 200
    record = created.json()
    assert record["body_site"] == "左侧面颊"
    assert record["recorded_on"] == "2026-10-01"

    listed = auth_client.get(API)
    assert listed.status_code == 200
    assert [item["record_id"] for item in listed.json()] == [record["record_id"]]

    updated = auth_client.patch(f"{API}/{record['record_id']}", json={"symptoms": "红斑减轻"})
    assert updated.status_code == 200
    assert updated.json()["symptoms"] == "红斑减轻"

    exported = auth_client.get(f"{API}/export")
    assert exported.status_code == 200
    assert "attachment;" in exported.headers["content-disposition"]
    assert exported.json()["records"][0]["record_id"] == record["record_id"]

    auth_client.post("/api/v1/auth/logout")
    _register(auth_client, "record_other")
    assert auth_client.get(API).json() == []
    assert auth_client.get(f"{API}/export").json()["records"] == []
    assert auth_client.get(f"{API}/{record['record_id']}").status_code == 404
    assert (
        auth_client.patch(f"{API}/{record['record_id']}", json={"symptoms": "偷读"}).status_code
        == 404
    )
    assert auth_client.delete(f"{API}/{record['record_id']}").status_code == 404

    auth_client.post("/api/v1/auth/logout")
    auth_client.post(
        "/api/v1/auth/login", json={"username": "record_owner", "password": "passw0rd1"}
    )
    deleted = auth_client.delete(f"{API}/{record['record_id']}")
    assert deleted.status_code == 200
    assert auth_client.get(API).json() == []


def test_health_record_requires_login(auth_client: TestClient) -> None:
    """未登录时不能读写健康记录。"""
    assert auth_client.get(API).status_code == 401
    assert auth_client.post(API, json=_payload()).status_code == 401


def test_health_record_rejects_blank_required_fields(auth_client: TestClient) -> None:
    """空白部位/症状不能写入个人时间线。"""
    _register(auth_client, "record_validation")
    payload = _payload()
    payload["body_site"] = "   "
    response = auth_client.post(API, json=payload)
    assert response.status_code == 400
