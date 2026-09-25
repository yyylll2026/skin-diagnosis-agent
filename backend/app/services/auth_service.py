"""鉴权服务：密码、会话、权限、审计。

## 设计要点

**1. 鉴权关闭时不存在「匿名用户」，而是「合成管理员」。**
`auth_enabled=false`（默认，也是既有测试与本地开发依赖的状态）下，所有请求被
视为一个拥有全部权限的合成身份。这样开关只改变「是否校验身份」，不改变任何
接口的业务语义——既有 108 条测试因此完全不受影响。

**2. 权限判定永远在服务端，且集中在一张权限表里。**
前端拿到的 `role` 只用于隐藏入口。真正的拦截靠 `require_permission("kb:write")`
这类依赖，而权限表 `PERMISSIONS` 是唯一真相源——加第三个角色时只改这张表。

**3. 会话是可撤销的，这是选「服务端会话」而非 JWT 的根本原因。**
token 明文只存在于用户浏览器的 cookie 里，库中存 SHA256 哈希。
因此「管理员强制下线」「改密码踢掉所有设备」「禁用账号」都是删一行的事，
不需要维护 JWT 黑名单。

**4. 登录失败必须与「用户名不存在」表现一致。**
无论用户名不存在还是密码错误，都抛同一个 `CredentialsError`，消息完全相同、
耗时也尽量接近（用户名不存在时也走一次 bcrypt 校验，见 `_dummy_verify`），
否则响应差异会成为枚举已注册用户的通道。
"""

from __future__ import annotations

import contextlib
import hashlib
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any

import bcrypt
from loguru import logger

from app.config import Settings, get_settings
from app.core.errors import (
    AccountDisabledError,
    AccountLockedError,
    AppError,
    CredentialsError,
    PermissionDeniedError,
    UserConflictError,
)
from app.repositories.db import MetadataStore
from app.schemas.auth import Role, UserInfo, UserStatus, normalize_username

# ---------------------------------------------------------------------------
# 权限模型（薄 RBAC）
#
# 只有两个角色，本可以写成 if role == ADMIN 的散落判断；之所以仍然建一张权限表，
# 是因为「哪里需要什么权限」和「哪个角色有什么权限」是两件独立的事——
# 前者散落在各接口（那是必要的，权限点就该写在它保护的那个接口旁边），
# 后者集中在这里。加第三个角色（如只读审计员）时只动这张表。
# ---------------------------------------------------------------------------

PERMISSION_KB_READ = "kb:read"
PERMISSION_KB_WRITE = "kb:write"
PERMISSION_KB_REINDEX = "kb:reindex"
PERMISSION_KB_RESET = "kb:reset"
PERMISSION_CHAT_USE = "chat:use"
PERMISSION_SESSION_READ_ALL = "session:read_all"
PERMISSION_USER_READ = "user:read"
PERMISSION_USER_WRITE = "user:write"
PERMISSION_AUDIT_READ = "audit:read"

PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.ADMIN: frozenset(
        {
            PERMISSION_KB_READ,
            PERMISSION_KB_WRITE,
            PERMISSION_KB_REINDEX,
            PERMISSION_KB_RESET,
            PERMISSION_CHAT_USE,
            PERMISSION_SESSION_READ_ALL,
            PERMISSION_USER_READ,
            PERMISSION_USER_WRITE,
            PERMISSION_AUDIT_READ,
        }
    ),
    # 客户：能提问、能只读查看知识库，不能有任何写操作
    Role.CUSTOMER: frozenset({PERMISSION_KB_READ, PERMISSION_CHAT_USE}),
}

# bcrypt 的轮数。12 在现代 CPU 上约 0.2~0.3 秒，是「抗暴力破解」与
# 「登录响应可接受」之间的常用折中。测试里会临时调低以加快速度。
_BCRYPT_ROUNDS = 12

# 用户不存在时用来消耗等量时间的假哈希（见模块文档第 4 点）。
# 这里是 "dummy-password" 的 bcrypt 哈希，**不是任何真实账号的密码**。
_DUMMY_HASH = b"$2b$12$C6UzMDM.H6dfI/f/IKcEe.6EOxDdPqFmRREvL5OZ0dCICVE7wXk2O"


class AuthService:
    """鉴权与用户管理。"""

    def __init__(
        self, settings: Settings | None = None, store: MetadataStore | None = None
    ) -> None:
        """初始化。

        Args:
            settings: 全局配置。
            store: 元数据仓储。
        """
        self.settings = settings or get_settings()
        self.store = store or MetadataStore(self.settings.resolved_sqlite_path)

    @staticmethod
    def normalize_username(username: str) -> str:
        """用户名归一化：去首尾空白 + 转小写。

        **必须放在服务层调用，而不是只放在 Pydantic 模型里。** 归一化是安全相关的
        （不做的话「Admin」和「admin」会成为两个账号，且大小写变体能被用来试探
        用户名是否存在），因此不能只依赖某一个入口的 schema——
        HTTP、CLI、脚本都必须经过同一套规则。

        实现委托给 `schemas.auth.normalize_username`，保证只有一份规则：
        service 依赖 schemas 是本项目既定的依赖方向，反过来则不行。
        """
        return normalize_username(username)

    # ------------------------------------------------------------------
    # 一、开关与合成身份
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """鉴权是否启用。"""
        return bool(self.settings.auth_enabled)

    @staticmethod
    def local_admin() -> UserInfo:
        """鉴权关闭时使用的合成管理员。

        `user_id="local"` 是一个约定：凡是需要「会话归属」的地方，
        在鉴权关闭时都用它，从而让归属校验自然通过（不做任何过滤）。
        """
        return UserInfo(
            user_id="local",
            username="local",
            display_name="本地模式",
            role=Role.ADMIN,
            status=UserStatus.ACTIVE,
        )

    def permissions_of(self, role: Role) -> list[str]:
        """取某角色的权限点列表（用于登录响应，供前端隐藏入口）。"""
        return sorted(PERMISSIONS.get(role, frozenset()))

    def has_permission(self, role: Role, permission: str) -> bool:
        """判断角色是否拥有某权限点。"""
        return permission in PERMISSIONS.get(role, frozenset())

    def ensure_permission(self, user: UserInfo, permission: str) -> None:
        """权限不足时抛 403。

        Raises:
            PermissionDeniedError: 当前角色不具备该权限点。
        """
        if not self.enabled:
            return
        if not self.has_permission(user.role, permission):
            raise PermissionDeniedError(f"当前账号（{user.role}）没有执行该操作的权限")

    # ------------------------------------------------------------------
    # 二、密码
    # ------------------------------------------------------------------

    def hash_password(self, password: str) -> str:
        """生成密码哈希（bcrypt，自带随机盐）。"""
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(_BCRYPT_ROUNDS)).decode()

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        """校验密码。bcrypt 自身即是恒定时间比较，不需要额外包装。

        哈希串损坏（例如被手工改坏）时返回 False 而不是抛异常——
        否则一个坏账号会让登录接口 500，反而暴露了「这个用户名存在」。
        """
        try:
            return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
        except (ValueError, TypeError):
            logger.warning("密码哈希格式异常，判定为校验失败")
            return False

    @staticmethod
    def _dummy_verify(password: str) -> None:
        """用户名不存在时也走一次 bcrypt，抹平响应时间差异。

        不做这一步的话，「用户名不存在」会立即返回，而「密码错误」要等
        bcrypt 算完（约 0.2 秒）——攻击者据此就能批量枚举出哪些用户名已注册。
        """
        with contextlib.suppress(ValueError, TypeError):
            bcrypt.checkpw(password.encode("utf-8"), _DUMMY_HASH)

    def validate_password(self, password: str) -> None:
        """按配置校验密码强度（长度下限可配置，比 Pydantic 的硬编码更严）。

        Raises:
            AppError: 长度不足。
        """
        minimum = self.settings.auth_min_password_length
        if len(password) < minimum:
            raise AppError(f"密码长度至少 {minimum} 位", detail={"field": "password"})

    # ------------------------------------------------------------------
    # 三、注册
    # ------------------------------------------------------------------

    def register(
        self,
        username: str,
        password: str,
        display_name: str = "",
        client_ip: str = "",
    ) -> tuple[UserInfo, str]:
        """自助注册。

        **角色硬编码为 customer**——开放注册的接口绝不能接受调用方指定角色，
        否则任何人都能把自己注册成管理员。

        Raises:
            AppError: 注册功能被关闭。
            UserConflictError: 用户名已被占用。
        """
        if not self.settings.auth_allow_registration:
            raise AppError("本系统未开放自助注册，请联系管理员创建账号")
        self.validate_password(password)

        try:
            row = self._create_user(
                username=username,
                password=password,
                role=Role.CUSTOMER,
                display_name=display_name or username,
            )
        except UserConflictError:
            self.audit("register", actor=username, result="conflict", client_ip=client_ip)
            raise

        user = self._to_info(row)
        self.audit("register", actor=username, target=user.user_id, client_ip=client_ip)
        logger.info("新用户注册：{}（{}）", username, Role.CUSTOMER.value)
        return user, "注册成功"

    def create_user(
        self,
        username: str,
        password: str,
        role: Role = Role.CUSTOMER,
        display_name: str = "",
        actor: str = "",
        client_ip: str = "",
    ) -> UserInfo:
        """管理员创建用户（可指定角色，这是与自助注册的关键区别）。"""
        self.validate_password(password)
        row = self._create_user(
            username=username,
            password=password,
            role=role,
            display_name=display_name or username,
        )
        info = self._to_info(row)
        self.audit(
            "user_create",
            actor=actor,
            target=f"{info.username}({info.role})",
            client_ip=client_ip,
        )
        return info

    def _create_user(
        self, username: str, password: str, role: Role, display_name: str
    ) -> dict[str, Any]:
        """落库创建用户，把唯一约束冲突翻译成业务异常。

        用户名在这里归一化，保证所有调用方（HTTP / CLI / 测试）行为一致。
        """
        user_id = f"u-{uuid.uuid4().hex[:12]}"
        normalized = self.normalize_username(username)
        try:
            return self.store.create_user(
                user_id=user_id,
                username=normalized,
                password_hash=self.hash_password(password),
                role=role.value,
                display_name=display_name or normalized,
                created_at=self._now_iso(),
            )
        except sqlite3.IntegrityError as exc:
            raise UserConflictError(f"用户名 {normalized} 已被占用") from exc

    # ------------------------------------------------------------------
    # 四、登录
    # ------------------------------------------------------------------

    def login(
        self,
        username: str,
        password: str,
        user_agent: str = "",
        client_ip: str = "",
    ) -> tuple[UserInfo, str]:
        """校验凭据并签发新会话。

        Returns:
            ``(用户信息, 会话 token 明文)``。token 明文只在此刻返回一次，
            调用方应立刻写入 httpOnly cookie，不要落库、不要记日志。

        Raises:
            CredentialsError: 用户名或密码错误（提示统一，不区分二者）。
            AccountLockedError: 账号因连续失败被锁定。
            AccountDisabledError: 账号被禁用。
        """
        row = self.store.get_user_by_username(self.normalize_username(username))

        if row is None:
            # 不存在的用户名也耗一次 bcrypt 的时间，避免时序差异被用来枚举账号
            self._dummy_verify(password)
            self.audit("login_failed", actor=username, result="no_user", client_ip=client_ip)
            raise CredentialsError("用户名或密码错误")

        if self._is_locked(row):
            self.audit("login_failed", actor=username, result="locked", client_ip=client_ip)
            raise AccountLockedError("账号因多次登录失败已被临时锁定，请稍后再试")

        if not self.verify_password(password, str(row.get("password_hash") or "")):
            self._register_failure(row)
            self.audit("login_failed", actor=username, result="bad_password", client_ip=client_ip)
            raise CredentialsError("用户名或密码错误")

        if str(row.get("status")) != UserStatus.ACTIVE.value:
            self.audit("login_failed", actor=username, result="disabled", client_ip=client_ip)
            raise AccountDisabledError("账号已被禁用，请联系管理员")

        # 登录成功：清空失败计数与锁定状态
        self.store.update_user(
            str(row["user_id"]),
            {"failed_attempts": 0, "locked_until": "", "last_login_at": self._now_iso()},
        )

        now = datetime.now()
        token = secrets.token_urlsafe(32)
        self.store.create_auth_session(
            token_hash=self._token_hash(token),
            user_id=str(row["user_id"]),
            created_at=now.isoformat(timespec="seconds"),
            expires_at=self._initial_expiry(now),
            user_agent=user_agent[:200],
            client_ip=client_ip,
        )
        # 顺手清理过期会话，省掉一个定时任务
        self.store.purge_expired_auth_sessions(self._now_iso())

        self.audit("login_success", actor=username, client_ip=client_ip)
        logger.info("登录成功：{}（{}）", username, row.get("role"))
        return self._to_info({**row, "last_login_at": self._now_iso()}), token

    def logout(self, token: str, actor: str = "", client_ip: str = "") -> None:
        """登出：删除该 token 对应的会话。"""
        if not token:
            return
        removed = self.store.delete_auth_session(self._token_hash(token))
        if removed:
            self.audit("logout", actor=actor, client_ip=client_ip)

    def force_logout(self, user_id: str, actor: str = "", client_ip: str = "") -> int:
        """踢掉某用户的全部会话（管理员操作）。"""
        count = self.store.delete_user_auth_sessions(user_id)
        self.audit("user_force_logout", actor=actor, target=user_id, client_ip=client_ip)
        return count

    # ------------------------------------------------------------------
    # 五、会话校验
    # ------------------------------------------------------------------

    def resolve_session(self, token: str) -> UserInfo | None:
        """把 cookie 里的 token 换成用户身份。

        校验三件事：会话存在、未过期（绝对 + 空闲）、账号仍启用。
        任一不成立返回 None（由依赖层翻译成 401）。

        会话有效时顺带滑动续期。

        Returns:
            用户信息；无效时返回 None。
        """
        if not token:
            return None

        row = self.store.get_auth_session(self._token_hash(token))
        if row is None:
            return None

        now = datetime.now()
        expires_at = self._parse_dt(str(row.get("expires_at") or ""))
        if expires_at is None or now >= expires_at:
            # 过期会话顺手删掉，不让它继续占用一次查询
            self.store.delete_auth_session(str(row["token_hash"]))
            return None

        user_row = self.store.get_user(str(row["user_id"]))
        if user_row is None:
            self.store.delete_auth_session(str(row["token_hash"]))
            return None

        if str(user_row.get("status")) != UserStatus.ACTIVE.value:
            # 被禁用的账号：连会话一起清掉，实现「禁用即下线」
            self.store.delete_user_auth_sessions(str(user_row["user_id"]))
            return None

        # 滑动续期，但不超过「创建时间 + 绝对 TTL」这条硬上限
        created_at = self._parse_dt(str(row.get("created_at") or "")) or now
        absolute_cap = created_at + timedelta(hours=self.settings.auth_session_ttl_hours)
        renewed = min(
            now + timedelta(minutes=self.settings.auth_idle_timeout_minutes),
            absolute_cap,
        )
        self.store.touch_auth_session(
            str(row["token_hash"]), renewed.isoformat(timespec="seconds"), self._now_iso()
        )

        return self._to_info(user_row)

    def is_session_expired(self, token: str) -> bool:
        """仅判断会话是否已失效（供测试与排查用）。"""
        return self.resolve_session(token) is None

    def ensure_session_access(self, session_id: str, user: UserInfo) -> None:
        """校验当前用户是否有权访问某个对话会话。

        这是权限模型里最容易漏的一环：不做这个检查的话，任何登录用户只要
        猜到一个 session_id 就能读走别人的对话——而 session_id 是
        `s-<12位hex>` 这样的短字符串，并非密码学强度。

        规则：

        - 鉴权关闭 → 放行（保持既有行为）；
        - 拥有 `session:read_all`（管理员）→ 放行；
        - 会话尚无归属（新建的 / 鉴权关闭时期创建的）→ 放行；
        - 归属是自己的 → 放行；
        - 其余 → 403。

        Raises:
            PermissionDeniedError: 会话属于其他用户。
        """
        if not self.enabled:
            return
        if self.has_permission(user.role, PERMISSION_SESSION_READ_ALL):
            return
        owner = self.store.get_session_owner(session_id)
        if owner and owner != user.user_id:
            logger.warning(
                "越权访问会话被拒绝：user={} session={} owner={}", user.username, session_id, owner
            )
            raise PermissionDeniedError("无权访问该会话")

    def visible_user_id_for_sessions(self, user: UserInfo) -> str | None:
        """列出会话时应使用的过滤条件。

        Returns:
            客户端返回其 user_id（只看自己）；管理员返回 None（看全部）。
        """
        if not self.enabled:
            return None
        if self.has_permission(user.role, PERMISSION_SESSION_READ_ALL):
            return None
        return user.user_id

    # ------------------------------------------------------------------
    # 六、用户管理（管理员）
    # ------------------------------------------------------------------

    def list_users(self, keyword: str = "") -> list[UserInfo]:
        """列出用户（可按用户名/显示名搜索）。"""
        return [self._to_info(row) for row in self.store.list_users(keyword=keyword)]

    def get_user(self, user_id: str) -> UserInfo | None:
        """按 ID 取用户。"""
        row = self.store.get_user(user_id)
        return self._to_info(row) if row else None

    def update_user(
        self,
        user_id: str,
        *,
        display_name: str | None = None,
        role: Role | None = None,
        status: UserStatus | None = None,
        actor: UserInfo | None = None,
        client_ip: str = "",
    ) -> UserInfo:
        """修改用户信息（显示名 / 角色 / 状态）。

        三条保护性约束，缺一不可：

        1. **不能禁用或降级自己**——避免管理员一操作就把自己锁在门外；
        2. **不能让启用中的管理员数量降到 0**——否则系统再也没人能管用户；
        3. 状态改为 disabled 时**立即清掉该用户的所有会话**，实现「禁用即下线」。

        Raises:
            AppError: 用户不存在。
            PermissionDeniedError: 触发了保护性约束。
        """
        row = self.store.get_user(user_id)
        if row is None:
            raise AppError(f"用户不存在：{user_id}")

        actor_name = actor.username if actor else ""
        actor_id = actor.user_id if actor else ""

        # 约束 1：不能对自己做降权/禁用
        if actor_id and actor_id == user_id:
            if status == UserStatus.DISABLED:
                raise PermissionDeniedError("不能禁用当前登录的管理员自己")
            if role is not None and role != Role.ADMIN and str(row.get("role")) == Role.ADMIN.value:
                raise PermissionDeniedError("不能把自己降级，请让其他管理员操作")

        # 约束 2：保护最后一个启用中的管理员
        is_active_admin = (
            str(row.get("role")) == Role.ADMIN.value
            and str(row.get("status")) == UserStatus.ACTIVE.value
        )
        if is_active_admin:
            becoming_non_admin = role is not None and role != Role.ADMIN
            becoming_disabled = status == UserStatus.DISABLED
            if (becoming_non_admin or becoming_disabled) and (
                self.store.count_active_admins(exclude_user_id=user_id) == 0
            ):
                raise PermissionDeniedError("系统中必须保留至少一个启用状态的管理员，操作已拒绝")

        fields: dict[str, Any] = {}
        if display_name is not None:
            fields["display_name"] = display_name
        if role is not None:
            fields["role"] = role.value
        if status is not None:
            fields["status"] = status.value

        if not self.store.update_user(user_id, fields):
            raise AppError(f"用户更新失败：{user_id}")

        # 约束 3：禁用即下线
        if status == UserStatus.DISABLED:
            self.store.delete_user_auth_sessions(user_id)

        self.audit(
            "user_update",
            actor=actor_name,
            target=f"{row.get('username')}:{fields}",
            client_ip=client_ip,
        )
        updated = self.store.get_user(user_id) or {}
        return self._to_info(updated)

    def delete_user(self, user_id: str, actor: UserInfo | None = None, client_ip: str = "") -> None:
        """删除用户。

        同样有两条保护：不能删自己、不能删最后一个启用中的管理员。

        Raises:
            AppError: 用户不存在。
            PermissionDeniedError: 触发了保护性约束。
        """
        row = self.store.get_user(user_id)
        if row is None:
            raise AppError(f"用户不存在：{user_id}")

        actor_id = actor.user_id if actor else ""
        if actor_id and actor_id == user_id:
            raise PermissionDeniedError("不能删除当前登录的账号")

        is_active_admin = (
            str(row.get("role")) == Role.ADMIN.value
            and str(row.get("status")) == UserStatus.ACTIVE.value
        )
        if is_active_admin and self.store.count_active_admins(exclude_user_id=user_id) == 0:
            raise PermissionDeniedError("系统中必须保留至少一个启用状态的管理员，删除已拒绝")

        self.store.delete_user(user_id)
        self.audit(
            "user_delete",
            actor=actor.username if actor else "",
            target=str(row.get("username")),
            client_ip=client_ip,
        )
        logger.info("用户已删除：{}", row.get("username"))

    def reset_password(
        self,
        user_id: str,
        new_password: str,
        actor: UserInfo | None = None,
        client_ip: str = "",
    ) -> None:
        """管理员重置他人密码。重置后该用户所有会话立即失效。

        刻意**不允许重置自己的密码**：管理员改自己的密码应走 `/auth/password`，
        那条路径会校验旧密码。这里若也允许，就等于给了一个「绕过旧密码」的后门。

        Raises:
            PermissionDeniedError: 试图重置自己的密码。
        """
        self.validate_password(new_password)
        row = self.store.get_user(user_id)
        if row is None:
            raise AppError(f"用户不存在：{user_id}")

        actor_id = actor.user_id if actor else ""
        if actor_id and actor_id == user_id:
            raise PermissionDeniedError("修改自己的密码请使用「修改密码」功能（需验证原密码）")

        self.store.update_user(
            user_id,
            {
                "password_hash": self.hash_password(new_password),
                "failed_attempts": 0,
                "locked_until": "",
            },
        )
        self.store.delete_user_auth_sessions(user_id)
        self.audit(
            "user_reset_password",
            actor=actor.username if actor else "",
            target=str(row.get("username")),
            client_ip=client_ip,
        )

    def change_password(
        self,
        user: UserInfo,
        old_password: str,
        new_password: str,
        client_ip: str = "",
    ) -> None:
        """用户修改自己的密码。需要验证原密码，改完踢掉所有会话。

        Raises:
            CredentialsError: 原密码错误。
            AppError: 用户不存在。
        """
        self.validate_password(new_password)
        row = self.store.get_user(user.user_id)
        if row is None:
            raise AppError("用户不存在")

        if not self.verify_password(old_password, str(row.get("password_hash") or "")):
            self.audit(
                "password_change", actor=user.username, result="bad_old", client_ip=client_ip
            )
            raise CredentialsError("原密码不正确")

        self.store.update_user(
            user.user_id,
            {
                "password_hash": self.hash_password(new_password),
                "failed_attempts": 0,
                "locked_until": "",
            },
        )
        # 改完密码把所有设备踢下线，这是安全惯例
        self.store.delete_user_auth_sessions(user.user_id)
        self.audit("password_change", actor=user.username, client_ip=client_ip)

    # ------------------------------------------------------------------
    # 七、审计
    # ------------------------------------------------------------------

    def audit(
        self,
        action: str,
        actor: str = "",
        target: str = "",
        result: str = "ok",
        detail: str = "",
        client_ip: str = "",
    ) -> None:
        """写审计日志。

        **审计失败绝不能影响主流程**——用户登录成功却因为"写日志失败"而收到 500，
        是本末倒置。因此这里吞掉所有异常，只留一条 warning。
        """
        try:
            self.store.add_audit_log(
                action=action,
                actor=actor[:64],
                target=target[:200],
                result=result,
                detail=detail[:500],
                client_ip=client_ip[:64],
                created_at=self._now_iso(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("审计日志写入失败（不影响主流程）：{}", exc)

    def list_audit_logs(self, limit: int = 100, action: str = "") -> list[dict[str, Any]]:
        """读取审计日志（管理员）。"""
        return self.store.list_audit_logs(limit=limit, action=action)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _token_hash(token: str) -> str:
        """token 的 SHA256。

        用 SHA256 而不是 bcrypt：token 是 256 位的高熵随机串，不存在被暴力破解的
        风险，因此不需要慢哈希；而慢哈希会让每个请求都多花 0.2 秒。
        慢哈希是用来对付低熵的人类密码的，用错场景只会拖慢自己。
        """
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _initial_expiry(self, now: datetime) -> str:
        """首次签发时的过期时间：空闲超时与绝对 TTL 取较小者。"""
        idle = now + timedelta(minutes=self.settings.auth_idle_timeout_minutes)
        absolute = now + timedelta(hours=self.settings.auth_session_ttl_hours)
        return min(idle, absolute).isoformat(timespec="seconds")

    def _is_locked(self, row: dict[str, Any]) -> bool:
        """账号当前是否处于锁定状态。"""
        locked_until = self._parse_dt(str(row.get("locked_until") or ""))
        return bool(locked_until and datetime.now() < locked_until)

    def _register_failure(self, row: dict[str, Any]) -> None:
        """累计失败次数，达到阈值即锁定。"""
        attempts = int(row.get("failed_attempts") or 0) + 1
        fields: dict[str, Any] = {"failed_attempts": attempts}
        if attempts >= self.settings.auth_max_failed_attempts:
            unlock_at = datetime.now() + timedelta(minutes=self.settings.auth_lockout_minutes)
            fields["locked_until"] = unlock_at.isoformat(timespec="seconds")
            fields["failed_attempts"] = 0  # 锁定期满后从零开始，避免一错即锁
            logger.warning(
                "账号 {} 连续失败 {} 次，锁定至 {}",
                row.get("username"),
                attempts,
                fields["locked_until"],
            )
        self.store.update_user(str(row["user_id"]), fields)

    @staticmethod
    def _now_iso() -> str:
        """当前时间的 ISO 字符串（与项目其余部分保持一致：本地时间、秒级）。"""
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _parse_dt(value: str) -> datetime | None:
        """解析 ISO 时间串，失败返回 None。"""
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _to_info(row: dict[str, Any]) -> UserInfo:
        """把数据库行转成对外契约。

        **这是唯一一处从 row 构造 UserInfo 的地方**，且只挑非敏感字段，
        因此不可能出现「某个接口不小心把 password_hash 吐出去」。
        """
        return UserInfo(
            user_id=str(row.get("user_id") or ""),
            username=str(row.get("username") or ""),
            display_name=str(row.get("display_name") or ""),
            role=Role(str(row.get("role") or Role.CUSTOMER.value))
            if str(row.get("role")) in {r.value for r in Role}
            else Role.CUSTOMER,
            status=UserStatus(str(row.get("status") or UserStatus.ACTIVE.value))
            if str(row.get("status")) in {s.value for s in UserStatus}
            else UserStatus.ACTIVE,
            created_at=str(row.get("created_at") or ""),
            last_login_at=str(row.get("last_login_at") or ""),
        )

    # ------------------------------------------------------------------
    # 便捷方法：供脚本与测试使用
    # ------------------------------------------------------------------

    def has_any_admin(self) -> bool:
        """系统里是否已有启用状态的管理员（CLI 与启动自检用）。"""
        return self.store.count_active_admins() > 0


def get_auth_service() -> AuthService:
    """构造默认鉴权服务（供脚本使用；Web 层请走 dependencies 里的单例）。"""
    return AuthService(get_settings(), MetadataStore(get_settings().resolved_sqlite_path))


__all__ = [
    "AuthService",
    "PERMISSIONS",
    "PERMISSION_AUDIT_READ",
    "PERMISSION_CHAT_USE",
    "PERMISSION_KB_READ",
    "PERMISSION_KB_REINDEX",
    "PERMISSION_KB_RESET",
    "PERMISSION_KB_WRITE",
    "PERMISSION_SESSION_READ_ALL",
    "PERMISSION_USER_READ",
    "PERMISSION_USER_WRITE",
    "get_auth_service",
]
