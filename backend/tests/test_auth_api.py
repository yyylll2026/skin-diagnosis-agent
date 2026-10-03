"""鉴权接口的端到端测试（HTTP 层）。

与 `test_auth_service.py` 的分工：那里测业务规则，这里测**接口层是否正确接线**——
cookie 有没有下发、401 与 403 有没有用对、权限守卫有没有真的挂上。

## 为什么单独搭一个 app

全局的 `tests/conftest.py` 把 `SKIN_AUTH_ENABLED` 固定为 false，
这样既有的 108 条测试不必为鉴权改造付出任何代价。
需要验证鉴权行为的用例在这里**自己构造一个开着鉴权的 app**，
并且用完清理掉 `lru_cache` 单例，避免污染后续测试。

构造方式是设环境变量 → 清缓存 → `create_app()`，
而不是给 `TestClient` 打依赖覆盖——后者会绕过真实的依赖解析链，
测不到「守卫有没有挂在正确的接口上」。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

API = "/api/v1"


@pytest.fixture
def auth_client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """一个开启了鉴权、指向独立临时库的应用客户端。"""
    monkeypatch.setenv("SKIN_AUTH_ENABLED", "true")
    monkeypatch.setenv("SKIN_SQLITE_PATH", str(tmp_path / "auth_api.db"))
    monkeypatch.setenv("SKIN_CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("SKIN_EMBEDDING_BACKEND", "chroma_default")

    from app import dependencies
    from app.config import get_settings

    get_settings.cache_clear()

    # 清掉所有会产生「指向旧库」实例的单例。
    # 漏掉任何一个，测试都可能操作到上一个用例残留的数据库。
    for factory in (
        dependencies.get_metadata_store,
        dependencies.get_analysis_task_service,
        dependencies.get_auth_service,
        dependencies.get_clarify_service,
        dependencies.get_ingest_service,
    ):
        factory.cache_clear()

    from app.main import create_app

    # 不用 `with TestClient(app)`：那会触发 lifespan 预热（建 Chroma 集合、
    # 编译状态图），鉴权测试用不上，还会明显拖慢执行。
    client = TestClient(create_app())

    yield client

    # 卸载时把单例清干净，避免污染其他测试文件
    get_settings.cache_clear()
    for factory in (
        dependencies.get_metadata_store,
        dependencies.get_analysis_task_service,
        dependencies.get_auth_service,
        dependencies.get_clarify_service,
        dependencies.get_ingest_service,
    ):
        factory.cache_clear()


def _register(client: TestClient, username: str, password: str = "passw0rd1"):
    """注册并返回响应。"""
    return client.post(
        f"{API}/auth/register",
        json={"username": username, "password": password, "display_name": username},
    )


def _create_admin(client: TestClient, username: str = "root", password: str = "adminpass1"):
    """直接经服务层建管理员（自助注册只能产生客户，所以必须绕一下）。"""
    from app.dependencies import get_auth_service
    from app.schemas.auth import Role

    return get_auth_service().create_user(username, password, role=Role.ADMIN)


# ---------------------------------------------------------------------------
# 注册 / 登录 / 会话
# ---------------------------------------------------------------------------


def test_registration_creates_customer_and_logs_in(auth_client: TestClient) -> None:
    """注册成功即登录，并下发 httpOnly cookie。"""
    response = _register(auth_client, "alice")
    assert response.status_code == 200
    body = response.json()

    assert body["user"]["username"] == "alice"
    assert body["user"]["role"] == "customer"
    # 关键：响应体里不能有凭据
    assert "token" not in body
    assert "password" not in str(body)

    set_cookie = response.headers.get("set-cookie", "")
    assert "skin_session=" in set_cookie
    assert "HttpOnly" in set_cookie, "会话 cookie 必须是 httpOnly，否则 JS 能读到"
    assert "samesite=lax" in set_cookie.lower(), "缺 SameSite 会失去 CSRF 防线"


def test_registration_cannot_self_assign_admin(auth_client: TestClient) -> None:
    """请求体里塞 role=admin 也不能提权——模型的 schema 里根本没有 role 字段。"""
    response = auth_client.post(
        f"{API}/auth/register",
        json={"username": "sneaky", "password": "passw0rd1", "role": "admin"},
    )
    assert response.status_code == 200
    assert response.json()["user"]["role"] == "customer"


def test_login_and_me(auth_client: TestClient) -> None:
    """登录后 /auth/me 返回身份与权限点。"""
    _register(auth_client, "bob")
    auth_client.post(f"{API}/auth/logout")  # 先登出，验证登录本身

    response = auth_client.post(
        f"{API}/auth/login", json={"username": "bob", "password": "passw0rd1"}
    )
    assert response.status_code == 200

    me = auth_client.get(f"{API}/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "bob"
    assert "kb:read" in me.json()["permissions"]
    assert "kb:write" not in me.json()["permissions"]


def test_me_without_session_returns_401(auth_client: TestClient) -> None:
    """未登录访问 /auth/me 返回 401（前端据此弹登录框）。"""
    response = auth_client.get(f"{API}/auth/me")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


def test_logout_revokes_session(auth_client: TestClient) -> None:
    """登出后原会话立即失效。"""
    _register(auth_client, "carol")
    assert auth_client.get(f"{API}/auth/me").status_code == 200

    assert auth_client.post(f"{API}/auth/logout").status_code == 200
    assert auth_client.get(f"{API}/auth/me").status_code == 401


def test_logout_is_idempotent(auth_client: TestClient) -> None:
    """未登录时登出也应成功，而不是报 401——否则前端会遇到「想登出却报错」。"""
    assert auth_client.post(f"{API}/auth/logout").status_code == 200


def test_wrong_password_returns_401(auth_client: TestClient) -> None:
    """密码错误返回 401，且提示不区分「用户不存在」。"""
    _register(auth_client, "dave")
    auth_client.post(f"{API}/auth/logout")

    bad = auth_client.post(f"{API}/auth/login", json={"username": "dave", "password": "wrongpass1"})
    unknown = auth_client.post(
        f"{API}/auth/login", json={"username": "ghost", "password": "wrongpass1"}
    )
    assert bad.status_code == 401
    assert unknown.status_code == 401
    assert bad.json()["message"] == unknown.json()["message"], "提示必须一致，防用户名枚举"


def test_weak_password_is_rejected(auth_client: TestClient) -> None:
    """弱密码（纯字母）被 400 拒绝。"""
    response = auth_client.post(
        f"{API}/auth/register", json={"username": "weak", "password": "onlyletters"}
    )
    assert response.status_code == 400


def test_validation_error_does_not_echo_the_submitted_password(auth_client: TestClient) -> None:
    """校验失败时**不能把提交的密码回显**在响应里。

    这是回归测试：早期实现直接返回 `exc.errors()`，而 Pydantic 的错误条目里带
    `input`（即字段原文），密码校验一失败就会把明文密码写进响应体——
    同时 `ctx.error` 里的异常对象还会导致响应无法序列化、400 变成 500。
    """
    response = auth_client.post(
        f"{API}/auth/register",
        json={"username": "leaky", "password": "SuperSecretPlaintext"},  # 纯字母 → 校验失败
    )
    assert response.status_code == 400
    assert "SuperSecretPlaintext" not in response.text
    # 结构仍然是可解析的 JSON（说明序列化没炸）
    body = response.json()
    assert body["code"] == "validation_error"
    assert isinstance(body["detail"], list)


def test_duplicate_username_returns_409(auth_client: TestClient) -> None:
    """用户名重复返回 409 而不是 500。"""
    _register(auth_client, "dup")
    response = _register(auth_client, "dup")
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# 权限守卫：客户
# ---------------------------------------------------------------------------


def test_customer_cannot_upload_document(auth_client: TestClient) -> None:
    """客户上传文档必须 403——这是权限模型的核心承诺。"""
    _register(auth_client, "cust")
    response = auth_client.post(
        f"{API}/kb/documents",
        files={"file": ("a.md", b"# hello", "text/markdown")},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_customer_cannot_delete_document(auth_client: TestClient) -> None:
    """客户删文档必须 403。"""
    _register(auth_client, "cust2")
    assert auth_client.delete(f"{API}/kb/documents/whatever").status_code == 403


def test_customer_cannot_reindex_or_reset(auth_client: TestClient) -> None:
    """重建索引与清空知识库是破坏力最高的两个操作，客户一律 403。"""
    _register(auth_client, "cust3")
    assert auth_client.post(f"{API}/kb/reindex").status_code == 403
    assert auth_client.post(f"{API}/kb/reset?confirm=true").status_code == 403


def test_customer_cannot_manage_users(auth_client: TestClient) -> None:
    """客户不能触碰用户管理接口。

    注意这里发的是**合法请求体**：鉴权守卫是子依赖，会在请求体校验之前执行，
    因此无权限的调用方连「字段格式不对」这种信息都拿不到。
    """
    _register(auth_client, "cust4")
    assert auth_client.get(f"{API}/users").status_code == 403
    assert (
        auth_client.post(
            f"{API}/users", json={"username": "newbie2", "password": "passw0rd1"}
        ).status_code
        == 403
    )
    assert auth_client.get(f"{API}/audit/logs").status_code == 403


def test_customer_can_read_knowledge_base(auth_client: TestClient) -> None:
    """客户可以只读查看知识库（列表 + 检索调试）。"""
    _register(auth_client, "reader")
    assert auth_client.get(f"{API}/kb/documents").status_code == 200
    assert (
        auth_client.post(f"{API}/kb/search", json={"query": "湿疹", "top_k": 3}).status_code == 200
    )


# ---------------------------------------------------------------------------
# 权限守卫：管理员
# ---------------------------------------------------------------------------


def test_admin_can_manage_users(auth_client: TestClient) -> None:
    """管理员可以创建用户、改角色、禁用。"""
    _create_admin(auth_client)
    auth_client.post(f"{API}/auth/login", json={"username": "root", "password": "adminpass1"})

    created = auth_client.post(
        f"{API}/users",
        json={"username": "newstaff", "password": "passw0rd1", "role": "customer"},
    )
    assert created.status_code == 200
    user_id = created.json()["user_id"]

    updated = auth_client.patch(f"{API}/users/{user_id}", json={"status": "disabled"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "disabled"

    listed = auth_client.get(f"{API}/users")
    assert listed.status_code == 200
    assert any(item["username"] == "newstaff" for item in listed.json())
    # 用户列表里绝不能出现密码字段
    assert "password_hash" not in str(listed.json())


def test_admin_can_read_audit_logs(auth_client: TestClient) -> None:
    """管理员可以查看审计日志，且登录失败也留痕。"""
    _create_admin(auth_client)
    auth_client.post(f"{API}/auth/login", json={"username": "root", "password": "badpass1"})
    auth_client.post(f"{API}/auth/login", json={"username": "root", "password": "adminpass1"})

    response = auth_client.get(f"{API}/audit/logs?limit=50")
    assert response.status_code == 200
    actions = {row["action"] for row in response.json()}
    assert "login_failed" in actions
    assert "login_success" in actions


def test_reset_password_endpoint_revokes_target_sessions(auth_client: TestClient) -> None:
    """管理员重置他人密码后，对方立刻掉线。"""
    from app.dependencies import get_auth_service

    _create_admin(auth_client)
    _register(auth_client, "victim")
    victim_id = get_auth_service().store.get_user_by_username("victim")["user_id"]

    # 换管理员身份来执行重置
    auth_client.post(f"{API}/auth/logout")
    auth_client.post(f"{API}/auth/login", json={"username": "root", "password": "adminpass1"})

    response = auth_client.post(
        f"{API}/users/{victim_id}/password", json={"new_password": "resetpass99"}
    )
    assert response.status_code == 200
    assert response.json()["sessions_revoked"] is True

    # victim 的旧会话应当已失效（此处用「密码已变」间接确认：旧密码登不上）
    auth_client.post(f"{API}/auth/logout")
    assert (
        auth_client.post(
            f"{API}/auth/login", json={"username": "victim", "password": "passw0rd1"}
        ).status_code
        == 401
    )
    assert (
        auth_client.post(
            f"{API}/auth/login", json={"username": "victim", "password": "resetpass99"}
        ).status_code
        == 200
    )


# ---------------------------------------------------------------------------
# 会话归属（改动面最大的一块）
# ---------------------------------------------------------------------------


def test_customer_cannot_read_others_session(auth_client: TestClient) -> None:
    """客户不能读到别人的会话历史——这是鉴权改造里最容易漏的一环。"""
    from app.dependencies import get_auth_service, get_metadata_store

    # 造一个属于别人的会话
    other = get_auth_service().create_user("other", "otherpass1")
    get_metadata_store().ensure_session("s-others", title="别人的", user_id=other.user_id)
    get_metadata_store().add_message("s-others", "user", "这是一条隐私内容")

    _register(auth_client, "nosy")
    assert auth_client.get(f"{API}/sessions/s-others").status_code == 403
    assert auth_client.delete(f"{API}/sessions/s-others").status_code == 403


def test_session_list_only_contains_own_sessions(auth_client: TestClient) -> None:
    """客户列出的会话只能包含自己的。"""
    from app.dependencies import get_auth_service, get_metadata_store

    other = get_auth_service().create_user("owner", "ownerpass1")
    get_metadata_store().ensure_session("s-owned", title="别人的", user_id=other.user_id)

    _register(auth_client, "inquirer")
    response = auth_client.get(f"{API}/sessions")
    assert response.status_code == 200
    assert all(item["session_id"] != "s-owned" for item in response.json())


def test_admin_sees_all_sessions(auth_client: TestClient) -> None:
    """管理员拥有 session:read_all，能看到他人会话。"""
    from app.dependencies import get_auth_service, get_metadata_store

    other = get_auth_service().create_user("someone", "somepass1")
    get_metadata_store().ensure_session("s-someone", title="某人的", user_id=other.user_id)

    _create_admin(auth_client)
    auth_client.post(f"{API}/auth/login", json={"username": "root", "password": "adminpass1"})
    response = auth_client.get(f"{API}/sessions")
    assert response.status_code == 200
    assert any(item["session_id"] == "s-someone" for item in response.json())


# ---------------------------------------------------------------------------
# 鉴权关闭时的回归保证
# ---------------------------------------------------------------------------


def test_health_is_public_and_reports_auth_state(auth_client: TestClient) -> None:
    """健康检查必须保持公开，否则登录页无从判断要不要弹登录框。"""
    response = auth_client.get(f"{API}/system/health")
    assert response.status_code == 200
    body = response.json()
    assert body["auth_enabled"] is True
    assert body["allow_registration"] is True


def test_chat_requires_login(auth_client: TestClient) -> None:
    """/chat/qa 在未登录时 401（且因为依赖顺序，不会先付出编译状态图的代价）。"""
    response = auth_client.post(f"{API}/chat/qa", json={"message": "湿疹怎么护理"})
    assert response.status_code == 401


def test_system_models_requires_login(auth_client: TestClient) -> None:
    """/system/models 暴露部署细节，要求登录。"""
    assert auth_client.get(f"{API}/system/models").status_code == 401
    assert auth_client.get(f"{API}/system/graph").status_code == 401
