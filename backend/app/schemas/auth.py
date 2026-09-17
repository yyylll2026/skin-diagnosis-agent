"""鉴权与用户管理的数据契约。

与项目其余部分一致，这里的 Pydantic 模型同时承担两个职责：
1. 约束**前端提交**的数据（格式、长度、取值范围）；
2. 作为**接口响应**的契约（`response_model`），前端按此渲染。

安全相关的三个刻意的设计：

- **`UserInfo` 里没有密码字段**，任何情况下都不会有。密码只在 `LoginRequest` /
  `RegisterRequest` / `ChangePasswordRequest` 里出现，且这些模型绝不会作为响应模型使用。
- **`role` 用 `StrEnum`**（与 `Intent`、`DocCategory` 同款），序列化成普通字符串，
  前端可直接比较。但**权限判定永远在服务端做**，前端拿到的 role 只用于隐藏入口。
- **登录失败的提示是统一的**（见 `AuthService`），不区分「用户名不存在」与「密码错误」，
  避免被用来枚举已注册用户。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class Role(StrEnum):
    """用户角色。当前只有两类。"""

    ADMIN = "admin"  # 管理员：知识库增删改查 + 用户管理
    CUSTOMER = "customer"  # 客户：只能提问与只读查看知识库


def normalize_username(value: object) -> str:
    """用户名归一化：去首尾空白 + 转小写。

    **这是唯一实现**，`AuthService` 与 `scripts/user_cli.py` 都复用它。

    为什么必须归一化：不做的话「Admin」与「admin」会成为两个账号，
    而且大小写变体会被用来试探某个用户名是否存在。
    为什么必须只有一份：归一化是安全相关的，两处写法一旦漂移，
    就会出现「注册时按 A 规则存、登录时按 B 规则查」这类查不到账号的怪问题。
    放在契约层是因为输入格式属于契约的一部分，而 service 层依赖 schemas 层
    是本项目既定的依赖方向（反之则不行）。
    """
    return str(value or "").strip().lower()


class UserStatus(StrEnum):
    """账号状态。禁用是「软删除」——保留审计痕迹，随时可恢复。"""

    ACTIVE = "active"
    DISABLED = "disabled"


# ---------------------------------------------------------------------------
# 请求
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    """自助注册请求。

    本部署开启了开放注册，任何访客都能建号（角色固定为 customer）。
    管理员账号只能由 CLI 创建，不能通过这个接口产生——否则任何人都能提权。
    """

    username: str = Field(min_length=3, max_length=32, description="登录用户名")
    password: str = Field(min_length=8, max_length=128, description="密码")
    display_name: str = Field(default="", max_length=32, description="显示名，可为空")

    @field_validator("username", mode="before")
    @classmethod
    def _normalize_username(cls, v: object) -> object:
        """交给契约层的唯一实现，避免与服务层/CLI 出现两套规则。"""
        return normalize_username(v)

    @field_validator("username")
    @classmethod
    def _validate_username_charset(cls, v: str) -> str:
        """只允许字母数字与下划线。

        中文用户名在日志与 URL 里容易被转义搞乱，这里收紧字符集；
        展示需求交给 `display_name`。
        """
        if not all(char.isalnum() or char == "_" for char in v):
            raise ValueError("用户名只能包含字母、数字和下划线")
        return v

    @field_validator("password")
    @classmethod
    def _validate_password_strength(cls, v: str) -> str:
        """开放注册的第一道门：必须同时含字母与数字。

        长度下限由配置的 `auth_min_password_length` 在服务层再校验一次
        （Pydantic 的 `min_length` 是硬编码的 8，配置可以更严格）。
        """
        has_letter = any(char.isalpha() for char in v)
        has_digit = any(char.isdigit() for char in v)
        if not (has_letter and has_digit):
            raise ValueError("密码需同时包含字母和数字")
        return v


class LoginRequest(BaseModel):
    """登录请求。"""

    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("username", mode="before")
    @classmethod
    def _normalize_username(cls, v: object) -> object:
        """与注册共用同一套规则——两处不一致会导致「注册的账号登不上」。"""
        return normalize_username(v)


class ChangePasswordRequest(BaseModel):
    """修改自己的密码。修改成功后所有会话失效，需要重新登录。"""

    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def _validate_password_strength(cls, v: str) -> str:
        has_letter = any(char.isalpha() for char in v)
        has_digit = any(char.isdigit() for char in v)
        if not (has_letter and has_digit):
            raise ValueError("密码需同时包含字母和数字")
        return v


# ---------------------------------------------------------------------------
# 管理员操作用户
# ---------------------------------------------------------------------------


class UserCreateRequest(BaseModel):
    """管理员创建用户。可以指定角色与状态，这是与自助注册的关键区别。"""

    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(default="", max_length=32)
    role: Role = Role.CUSTOMER

    @field_validator("username", mode="before")
    @classmethod
    def _normalize_username(cls, v: object) -> object:
        return normalize_username(v)

    @field_validator("username")
    @classmethod
    def _validate_username_charset(cls, v: str) -> str:
        if not all(char.isalnum() or char == "_" for char in v):
            raise ValueError("用户名只能包含字母、数字和下划线")
        return v


class UserUpdateRequest(BaseModel):
    """管理员修改用户信息。所有字段可选，只更新传了的字段。"""

    display_name: str | None = Field(default=None, max_length=32)
    role: Role | None = None
    status: UserStatus | None = None


class ResetPasswordRequest(BaseModel):
    """管理员重置他人密码。重置后该用户所有会话立即失效。"""

    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def _validate_password_strength(cls, v: str) -> str:
        has_letter = any(char.isalpha() for char in v)
        has_digit = any(char.isdigit() for char in v)
        if not (has_letter and has_digit):
            raise ValueError("密码需同时包含字母和数字")
        return v


# ---------------------------------------------------------------------------
# 响应
# ---------------------------------------------------------------------------


class UserInfo(BaseModel):
    """对外暴露的用户信息。

    **刻意不含任何凭据字段**——包括密码哈希。这个模型是
    `/auth/me`、用户列表、注册/登录响应的共同载体，
    只要它不含密码，就不存在「某个接口不小心把哈希吐出去」的可能。
    """

    user_id: str
    username: str
    display_name: str = ""
    role: Role = Role.CUSTOMER
    status: UserStatus = UserStatus.ACTIVE
    created_at: str = ""
    last_login_at: str = ""

    @property
    def is_admin(self) -> bool:
        """便捷判断。注意这**不能**作为权限依据，权限由服务端权限表判定。"""
        return self.role == Role.ADMIN


class AuthResponse(BaseModel):
    """登录/注册成功后的响应。

    会话本身放在 httpOnly cookie 里，**这里不回传 token**——
    前端拿不到凭据，也就没有把它误存到 localStorage 的机会。
    """

    user: UserInfo
    permissions: list[str] = Field(default_factory=list, description="该角色拥有的权限点")


class HealthAuthInfo(BaseModel):
    """健康检查里暴露的鉴权状态，供前端决定是否弹登录框。"""

    auth_enabled: bool = False
    allow_registration: bool = False
