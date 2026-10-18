"""鉴权服务的单元测试。

这一组锁住的是**安全性质**，而不是实现细节。每一条挂了都意味着一个真实风险：

- `test_login_error_message_is_identical_for_unknown_user` —— 防用户名枚举
- `test_password_is_never_stored_in_plaintext` —— 防库泄露后直接可用
- `test_session_token_is_stored_hashed` —— 同上
- `test_customer_cannot_write_knowledge_base` —— 权限模型的核心承诺
- `test_last_admin_cannot_be_demoted_or_deleted` —— 防把自己锁在系统外
- `test_disable_revokes_sessions_immediately` —— 「禁用」必须立即生效

为了让 bcrypt 不至于拖慢测试，这里把轮数降到 4（生产仍是 12）。
轮数只影响计算耗时，不影响被验证的行为。
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.core.errors import (
    AccountDisabledError,
    AccountLockedError,
    AppError,
    CredentialsError,
    PermissionDeniedError,
    UserConflictError,
)
from app.repositories.db import MetadataStore
from app.schemas.auth import Role, UserStatus
from app.services import auth_service as auth_module
from app.services.auth_service import (
    PERMISSION_HEALTH_READ,
    PERMISSION_HEALTH_READ_ALL,
    PERMISSION_KB_READ,
    PERMISSION_KB_WRITE,
    PERMISSION_SESSION_READ_ALL,
    PERMISSION_USER_READ,
    PERMISSIONS,
    AuthService,
)


@pytest.fixture(autouse=True)
def _fast_bcrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 bcrypt 轮数降到 4，让测试秒级跑完（生产默认 12）。"""
    monkeypatch.setattr(auth_module, "_BCRYPT_ROUNDS", 4)


def _service(tmp_path, **overrides) -> AuthService:
    """构造一套开启鉴权、指向临时库的服务。"""
    values = {"sqlite_path": tmp_path / "auth.db", "auth_enabled": True}
    values.update(overrides)
    settings = Settings(_env_file=None, **values)
    return AuthService(settings, MetadataStore(settings.resolved_sqlite_path))


@pytest.fixture
def service(tmp_path) -> AuthService:
    return _service(tmp_path)


@pytest.fixture
def admin(service: AuthService):
    return service.create_user("root", "adminpass1", role=Role.ADMIN)


@pytest.fixture
def customer(service: AuthService):
    return service.create_user("alice", "userpass1", role=Role.CUSTOMER)


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------


def test_password_is_never_stored_in_plaintext(service: AuthService) -> None:
    """库里存的必须是哈希，且明文密码不能出现在任何字段里。"""
    service.create_user("bob", "secretpass1")
    row = service.store.get_user_by_username("bob")
    assert row is not None
    assert row["password_hash"] != "secretpass1"
    assert row["password_hash"].startswith("$2b$")
    # 整行序列化后也不应包含明文
    assert "secretpass1" not in str(row)


def test_password_hash_is_salted(service: AuthService) -> None:
    """同一密码两次哈希结果不同（说明加了随机盐）。"""
    service.create_user("u1", "samepass11")
    service.create_user("u2", "samepass11")
    h1 = service.store.get_user_by_username("u1")["password_hash"]
    h2 = service.store.get_user_by_username("u2")["password_hash"]
    assert h1 != h2


def test_password_length_is_enforced(service: AuthService) -> None:
    """短于配置下限的密码必须被拒（开放注册的第一道门）。"""
    with pytest.raises(AppError):
        service.create_user("shorty", "ab1")


def test_corrupted_hash_fails_closed(service: AuthService) -> None:
    """哈希串损坏时判定为校验失败，而不是抛异常（否则会 500 并暴露账号存在）。"""
    assert service.verify_password("anything", "not-a-bcrypt-hash") is False


# ---------------------------------------------------------------------------
# 注册（开放注册）
# ---------------------------------------------------------------------------


def test_register_creates_customer_not_admin(service: AuthService) -> None:
    """开放注册**只能**产生客户角色——否则任何人都能注册成管理员。"""
    user, _ = service.register("newbie", "newpass11")
    assert user.role == Role.CUSTOMER


def test_register_rejects_duplicate_username(service: AuthService) -> None:
    """用户名唯一，冲突翻译成 409 而不是 500。"""
    service.register("dup", "pass1234a")
    with pytest.raises(UserConflictError):
        service.register("DUP", "pass1234a")  # 大小写归一后视为同名


def test_register_can_be_disabled(tmp_path) -> None:
    """关闭注册开关后拒绝自助注册。"""
    service = _service(tmp_path, auth_allow_registration=False)
    with pytest.raises(AppError):
        service.register("nobody", "pass1234a")


def test_username_is_normalized(service: AuthService) -> None:
    """用户名去空白并转小写，避免 Admin / admin 变成两个账号。"""
    user, _ = service.register("  MixedCase  ", "pass1234a")
    assert user.username == "mixedcase"
    # 用大写也能登录
    logged_in, _ = service.login("MIXEDCASE", "pass1234a")
    assert logged_in.user_id == user.user_id


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------


def test_login_returns_user_and_token(service: AuthService, customer) -> None:
    user, token = service.login("alice", "userpass1")
    assert user.user_id == customer.user_id
    assert len(token) >= 32
    assert service.resolve_session(token).user_id == customer.user_id


def test_login_error_message_is_identical_for_unknown_user(service: AuthService, customer) -> None:
    """「用户名不存在」与「密码错误」的对外表现必须完全一致，防用户名枚举。"""
    with pytest.raises(CredentialsError) as unknown:
        service.login("no-such-user", "whatever1")
    with pytest.raises(CredentialsError) as badpass:
        service.login("alice", "wrongpass1")
    assert unknown.value.message == badpass.value.message


def test_login_failure_locks_account(service: AuthService, customer) -> None:
    """连续失败达到阈值后锁定，且锁定期内即使密码正确也拒绝。"""
    limit = service.settings.auth_max_failed_attempts
    for _ in range(limit):
        with pytest.raises(CredentialsError):
            service.login("alice", "wrongpass1")
    with pytest.raises(AccountLockedError):
        service.login("alice", "userpass1")


def test_disabled_account_cannot_login(service: AuthService, customer, admin) -> None:
    """被禁用的账号无法登录。"""
    service.update_user(customer.user_id, status=UserStatus.DISABLED, actor=admin)
    with pytest.raises(AccountDisabledError):
        service.login("alice", "userpass1")


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


def test_session_token_is_stored_hashed(service: AuthService, customer) -> None:
    """库里只存 token 的哈希——库泄露也不能直接拿去当凭据用。"""
    _, token = service.login("alice", "userpass1")
    rows = service.store.list_user_auth_sessions(customer.user_id)
    assert rows, "应当有一条会话记录"
    stored = {row["token_hash"] for row in rows}
    assert token not in stored
    assert service._token_hash(token) in stored


def test_unknown_token_resolves_to_none(service: AuthService) -> None:
    """伪造的 token 必须解析失败。"""
    assert service.resolve_session("obviously-not-a-real-token") is None
    assert service.resolve_session("") is None


def test_logout_revokes_single_session(service: AuthService, customer) -> None:
    """登出后该 token 立即失效。"""
    _, token = service.login("alice", "userpass1")
    assert service.resolve_session(token) is not None
    service.logout(token, actor="alice")
    assert service.resolve_session(token) is None


def test_expired_session_is_rejected(service: AuthService, customer) -> None:
    """过期会话必须失效，并被顺手清理掉。

    这里直接改库里的过期时间，而不是把超时调到亚秒再 sleep：
    项目的所有时间戳都是**秒级**（`isoformat(timespec="seconds")`），
    亚秒级超时会被截断，测出来的行为没有参考价值（而且会拖慢测试）。
    真实验证的是「过期检查」这段逻辑，改库足以覆盖。
    """
    _, token = service.login("alice", "userpass1")
    assert service.resolve_session(token) is not None

    token_hash = service.store.list_user_auth_sessions(customer.user_id)[0]["token_hash"]
    service.store.touch_auth_session(token_hash, "2000-01-01T00:00:00", "2000-01-01T00:00:00")

    assert service.resolve_session(token) is None
    assert service.store.get_auth_session(token_hash) is None, "过期会话应被顺手清掉"


def test_session_expiry_uses_the_shorter_limit(service: AuthService, customer) -> None:
    """签发的过期时间应是「空闲超时」与「绝对 TTL」中较小者。

    默认 idle=120 分钟 < ttl=12 小时，所以应该取 idle。
    这条测试不 sleep，只校验计算逻辑。
    """
    from datetime import datetime, timedelta

    _, token = service.login("alice", "userpass1")
    row = service.store.get_auth_session(service._token_hash(token))
    assert row is not None

    created = datetime.fromisoformat(row["created_at"])
    expires = datetime.fromisoformat(row["expires_at"])
    idle_window = timedelta(minutes=service.settings.auth_idle_timeout_minutes)

    assert created < expires
    # 允许 1 秒误差：时间戳按秒截断，会让有效期略微变短（偏保守，是安全的方向）
    assert expires - created <= idle_window + timedelta(seconds=1)


def test_force_logout_revokes_all_sessions(service: AuthService, customer) -> None:
    """管理员踢人：该用户所有设备的会话一起失效。"""
    _, t1 = service.login("alice", "userpass1")
    _, t2 = service.login("alice", "userpass1")
    count = service.force_logout(customer.user_id, actor="root")
    assert count == 2
    assert service.resolve_session(t1) is None
    assert service.resolve_session(t2) is None


def test_change_password_revokes_sessions(service: AuthService, customer) -> None:
    """改密码后所有设备下线（安全惯例）。"""
    user, token = service.login("alice", "userpass1")
    service.change_password(user, "userpass1", "brandnewpass2")
    assert service.resolve_session(token) is None
    # 新密码可用，旧密码不可用
    service.login("alice", "brandnewpass2")
    with pytest.raises(CredentialsError):
        service.login("alice", "userpass1")


def test_change_password_requires_old_password(service: AuthService, customer) -> None:
    """不知道原密码就改不了密码。"""
    user, _ = service.login("alice", "userpass1")
    with pytest.raises(CredentialsError):
        service.change_password(user, "wrongold1", "brandnewpass2")


def test_disable_revokes_sessions_immediately(service: AuthService, customer, admin) -> None:
    """「禁用」必须立即生效——已登录的会话也要被清掉。"""
    _, token = service.login("alice", "userpass1")
    service.update_user(customer.user_id, status=UserStatus.DISABLED, actor=admin)
    assert service.resolve_session(token) is None


# ---------------------------------------------------------------------------
# 权限模型
# ---------------------------------------------------------------------------


def test_admin_has_write_permissions(service: AuthService) -> None:
    """管理员拥有全部权限点。"""
    for permission in (PERMISSION_KB_READ, PERMISSION_KB_WRITE, PERMISSION_USER_READ):
        assert service.has_permission(Role.ADMIN, permission) is True


def test_customer_cannot_write_knowledge_base(service: AuthService) -> None:
    """客户只能读知识库，一个写权限都没有——这是本次权限模型的核心承诺。"""
    assert service.has_permission(Role.CUSTOMER, PERMISSION_KB_READ) is True
    for permission in (PERMISSION_KB_WRITE, PERMISSION_USER_READ, PERMISSION_SESSION_READ_ALL):
        assert service.has_permission(Role.CUSTOMER, permission) is False


def test_customer_cannot_read_other_patients_records(service: AuthService) -> None:
    """客户只有「读自己」，没有「读任意患者」。

    这是医生工作台四个端点的**唯一防线**（见 api/v1/health_records.py）。
    回归背景：`health_records:read` 曾经同时承担「读自己」与「读全部」两种语义，
    客户也持有它，于是任何注册用户都能枚举全部患者并读走病历。
    修法是把它拆成 read / read_all 两个权限点。本用例锁住拆分结果。
    """
    assert service.has_permission(Role.CUSTOMER, PERMISSION_HEALTH_READ) is True
    assert service.has_permission(Role.CUSTOMER, PERMISSION_HEALTH_READ_ALL) is False


def test_clinicians_can_read_other_patients_records(service: AuthService) -> None:
    """医生/专家/管理员持有跨患者读取权限，否则工作台对谁都打不开。"""
    for role in (Role.DOCTOR, Role.EXPERT, Role.ADMIN):
        assert service.has_permission(role, PERMISSION_HEALTH_READ_ALL) is True, role


def test_every_role_is_registered_in_the_permission_table() -> None:
    """每个角色都必须在权限表里有条目。

    `has_permission` 用的是 `PERMISSIONS.get(role, frozenset())`，
    漏登记不会报错，只会静默变成「零权限角色」——这种失败方式很难排查。
    """
    assert set(PERMISSIONS) == set(Role)


def test_admin_permissions_are_a_superset_of_every_other_role() -> None:
    """管理员必须是所有角色的超集。

    这条不变量一旦破坏，就会出现「某角色能做的事管理员反而不能做」，
    而真实原因通常是往权限表加权限点时漏了 ADMIN 那一行。
    """
    admin = PERMISSIONS[Role.ADMIN]
    for role in Role:
        if role is Role.ADMIN:
            continue
        missing = PERMISSIONS[role] - admin
        assert not missing, f"{role} 有而管理员没有的权限点：{sorted(missing)}"


def test_ensure_permission_raises_403_for_customer(service: AuthService, customer) -> None:
    """越权调用抛 403（不是 401——已登录但没有权限，语义不同）。"""
    with pytest.raises(PermissionDeniedError) as exc:
        service.ensure_permission(customer, PERMISSION_KB_WRITE)
    assert exc.value.http_status == 403


def test_ensure_permission_passes_when_disabled(tmp_path) -> None:
    """鉴权关闭时不做权限校验，保证既有行为不变。"""
    service = _service(tmp_path, auth_enabled=False)
    service.ensure_permission(service.local_admin(), PERMISSION_KB_WRITE)  # 不抛异常即通过


# ---------------------------------------------------------------------------
# 会话归属
# ---------------------------------------------------------------------------


def test_session_access_is_limited_to_owner(service: AuthService, customer, admin) -> None:
    """会话归属校验：自己的可访问，别人的 403，管理员可访问全部。"""
    # 建两个客户
    other = service.create_user("bob", "bobpass11", role=Role.CUSTOMER)
    service.store.ensure_session("s-bob", title="bob 的会话", user_id=other.user_id)

    with pytest.raises(PermissionDeniedError):
        service.ensure_session_access("s-bob", customer)
    # 管理员有 session:read_all，可以访问
    service.ensure_session_access("s-bob", admin)
    # 归属人自己可以访问
    service.ensure_session_access("s-bob", other)


def test_unowned_session_is_accessible(service: AuthService, customer) -> None:
    """尚无归属的会话（如刚创建）不阻塞——否则第一次请求就 403。"""
    service.ensure_session_access("s-brand-new", customer)


def test_visible_user_id_filters_for_customer(service: AuthService, customer, admin) -> None:
    """列表过滤条件：客户只看自己，管理员看全部。"""
    assert service.visible_user_id_for_sessions(customer) == customer.user_id
    assert service.visible_user_id_for_sessions(admin) is None


# ---------------------------------------------------------------------------
# 保护性约束
# ---------------------------------------------------------------------------


def test_last_admin_cannot_be_deleted(service: AuthService, admin) -> None:
    """系统必须保留至少一个启用中的管理员。

    注意 actor 的选取：如果用被删者自己当 actor，会先被「不能删自己」那条拦下，
    就测不到真正想验证的约束。所以这里用一个**第三方** actor
    （服务层不校验调用者角色，权限由 API 层的 require_permission 负责），
    从而把「最后一个管理员」这条守卫单独隔离出来。
    """
    other = service.create_user("root2", "adminpass2", role=Role.ADMIN)
    service.delete_user(admin.user_id, actor=other)  # 此时只剩 other 一个管理员

    bystander = service.create_user("bystander", "bypass1234", role=Role.CUSTOMER)
    with pytest.raises(PermissionDeniedError):
        service.delete_user(other.user_id, actor=bystander)
    # 确认真的没被删掉
    assert service.store.get_user(other.user_id) is not None


def test_last_admin_guard_blocks_demotion(service: AuthService, admin) -> None:
    """把最后一个管理员降级为客户同样必须拒绝。"""
    other = service.create_user("root2", "adminpass2", role=Role.ADMIN)
    service.delete_user(admin.user_id, actor=other)
    # 此时 other 是唯一管理员，用一个非管理员 actor 绕过「不能改自己」的限制
    bystander = service.create_user("bypasser", "bypass1234", role=Role.CUSTOMER)
    with pytest.raises(PermissionDeniedError):
        service.update_user(other.user_id, role=Role.CUSTOMER, actor=bystander)


def test_cannot_disable_self(service: AuthService, admin) -> None:
    """管理员不能禁用自己（避免把自己锁在门外）。"""
    with pytest.raises(PermissionDeniedError):
        service.update_user(admin.user_id, status=UserStatus.DISABLED, actor=admin)


def test_cannot_delete_self(service: AuthService, admin) -> None:
    """不能删除当前登录账号。"""
    with pytest.raises(PermissionDeniedError):
        service.delete_user(admin.user_id, actor=admin)


def test_cannot_reset_own_password_via_admin_endpoint(service: AuthService, admin) -> None:
    """管理员重置密码的接口不能用来改自己的密码——
    那等于绕过了「修改密码需验证原密码」的防线。"""
    with pytest.raises(PermissionDeniedError):
        service.reset_password(admin.user_id, "newpass1234", actor=admin)


def test_admin_reset_password_revokes_target_sessions(
    service: AuthService, customer, admin
) -> None:
    """管理员重置他人密码后，对方的会话立即失效。"""
    _, token = service.login("alice", "userpass1")
    service.reset_password(customer.user_id, "resetpass99", actor=admin)
    assert service.resolve_session(token) is None
    service.login("alice", "resetpass99")


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


def test_audit_records_login_success_and_failure(service: AuthService, customer) -> None:
    """登录成功与失败都要留痕。"""
    service.login("alice", "userpass1")
    with pytest.raises(CredentialsError):
        service.login("alice", "wrongpass1")

    actions = {row["action"] for row in service.list_audit_logs(limit=50)}
    assert "login_success" in actions
    assert "login_failed" in actions


def test_audit_records_management_operations(service: AuthService, admin) -> None:
    """管理类操作要留痕，否则「谁改了谁」无从追溯。"""
    service.create_user("newguy", "pass1234a")
    service.update_user(
        service.store.get_user_by_username("newguy")["user_id"],
        status=UserStatus.DISABLED,
        actor=admin,
    )
    actions = {row["action"] for row in service.list_audit_logs(limit=50)}
    assert "user_create" in actions
    assert "user_update" in actions


def test_audit_never_contains_password(service: AuthService) -> None:
    """审计日志里绝不能出现明文密码。"""
    service.register("auditme", "plaintext123")
    dump = str(service.list_audit_logs(limit=50))
    assert "plaintext123" not in dump


def test_audit_failure_does_not_break_flow(service: AuthService, monkeypatch) -> None:
    """审计写入失败不能影响主流程——登录成功却因「写日志失败」而 500 是本末倒置。"""
    monkeypatch.setattr(
        service.store, "add_audit_log", lambda **_: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    service.register("resilient", "pass1234a")  # 不抛异常即通过
