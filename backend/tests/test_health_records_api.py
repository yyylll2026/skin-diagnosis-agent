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


# ---------------------------------------------------------------------------
# 医生/专家工作台的越权回归
#
# 四个工作台端点只允许持有 `health_records:read_all` 的角色进入（医生/专家/管理员）。
# 客户**没有**该权限点，因此必须一律 403 —— 这是「客户不能读走他人病历」的唯一防线。
#
# 回归背景：这些端点早先挂在 `health_records:read` 上，而客户也持有它，
# 于是任何注册用户登录后都能 GET /health-records/patients 枚举全部患者，
# 并读走任意患者的病历。权限点已拆成 read（自己的）/ read_all（任意患者）。
# ---------------------------------------------------------------------------

WORKBENCH_PATHS = (
    f"{API}/patients",
    f"{API}/patients/p-someone/records",
    f"{API}/patients/p-someone/records/r-someone",
    f"{API}/patients/p-someone/trends",
)


def _register_returning_user_id(client: TestClient, username: str) -> str:
    """注册并返回新账号的 user_id（自助注册只能产生客户角色）。"""
    response = client.post(
        "/api/v1/auth/register", json={"username": username, "password": "passw0rd1"}
    )
    assert response.status_code == 200
    return response.json()["user"]["user_id"]


def _create_user_with_role(username: str, password: str, role: str) -> None:
    """经服务层建指定角色的账号（绕开「自助注册只能建客户」的限制）。"""
    from app.dependencies import get_auth_service
    from app.schemas.auth import Role

    get_auth_service().create_user(username, password, role=Role(role))


def test_patient_workbench_rejects_customer(auth_client: TestClient) -> None:
    """客户访问医生工作台一律 403。

    先让客户自己拥有一条记录，确保「库里确实有数据」也不会让它拿到任何患者视图；
    路径参数用的是不存在的 ID，用来同时确认**守卫先于业务校验执行**——
    否则就会先返回 404，等于把「这个患者 ID 存不存在」泄漏给无权限的调用方。
    """
    _register(auth_client, "nosy_customer")
    assert auth_client.post(API, json=_payload()).status_code == 200

    for path in WORKBENCH_PATHS:
        response = auth_client.get(path)
        assert response.status_code == 403, f"{path} 应拒绝客户，实际 {response.status_code}"
        assert response.json()["code"] == "forbidden"


def test_patient_workbench_allows_doctor_and_reads_the_right_patient(
    auth_client: TestClient,
) -> None:
    """医生能跨患者读取，且读到的确实是那位患者的记录。"""
    patient_id = _register_returning_user_id(auth_client, "patient_zhang")
    created = auth_client.post(API, json=_payload())
    assert created.status_code == 200
    record_id = created.json()["record_id"]

    _create_user_with_role("dr_li", "doctorpass1", "doctor")
    assert auth_client.post("/api/v1/auth/logout").status_code == 200

    login = auth_client.post(
        "/api/v1/auth/login", json={"username": "dr_li", "password": "doctorpass1"}
    )
    assert login.status_code == 200
    assert "health_records:read_all" in login.json()["permissions"]

    patients = auth_client.get(f"{API}/patients")
    assert patients.status_code == 200
    assert patient_id in [row["user_id"] for row in patients.json()]

    records = auth_client.get(f"{API}/patients/{patient_id}/records")
    assert records.status_code == 200
    assert [row["record_id"] for row in records.json()] == [record_id]

    detail = auth_client.get(f"{API}/patients/{patient_id}/records/{record_id}")
    assert detail.status_code == 200
    assert detail.json()["record_id"] == record_id

    trends = auth_client.get(f"{API}/patients/{patient_id}/trends")
    assert trends.status_code == 200
    assert trends.json()["window_days"] == 90
    assert trends.json()["total_records_all"] == 1
