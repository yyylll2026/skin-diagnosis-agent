"""鉴权接口：注册、登录、登出、当前用户、修改密码。

## 会话放在 httpOnly cookie 里，响应体里没有 token

`/auth/login` 与 `/auth/register` 成功后只回传用户信息与权限点，
**凭据本身通过 `Set-Cookie` 下发且带 `HttpOnly`**。这样前端 JS 根本读不到它，
也就没有「不小心把 token 存进 localStorage 被 XSS 偷走」的可能。

代价是所有请求都要靠浏览器自动带 cookie（同源部署下天然成立，
本项目的 Vite dev server 与单容器模式都是同源），
以及必须考虑 CSRF——防线是 `SameSite=Lax`：跨站发起的 POST 不会带上这个 cookie。

## 注册接口的安全考量

本部署开启了自助注册，这意味着**任何人都能建号**。因此：

- 角色**硬编码为 customer**，请求体里没有 role 字段，杜绝注册即提权；
- 密码强度在 Pydantic 与服务层双重校验（长度可配置）；
- 用户名唯一冲突返回 409 而不是 500。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from loguru import logger

from app.config import Settings, get_settings
from app.dependencies import CurrentUser, get_auth_service
from app.schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    UserInfo,
)
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["鉴权"])


def _client_ip(request: Request) -> str:
    """取客户端 IP。

    只信任直连来源：`request.client.host`。**刻意不解析 `X-Forwarded-For`**——
    那个头可以被客户端任意伪造，用它做限流或审计会被绕过；
    若将来部署在反向代理后面，应在代理层配置可信来源后再改这里。
    """
    return request.client.host if request.client else ""


def _set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    """写入会话 cookie。

    四个属性各有明确作用，缺一个都会出问题：

    - `httponly`：JS 读不到，防 XSS 窃取；
    - `samesite="lax"`：跨站 POST 不带此 cookie，这是本项目主要的 CSRF 防线；
    - `secure`：仅 HTTPS 传输。本地 http 下必须为 False，否则浏览器直接丢弃 cookie，
      表现为「登录成功但下一次请求又是未登录」；
    - `max_age`：与绝对超时一致，让浏览器也按这个时间清理。
    """
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=int(settings.auth_session_ttl_hours * 3600),
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
        path="/",
    )


def _clear_session_cookie(response: Response, settings: Settings) -> None:
    """清除会话 cookie（登出 / 改密后）。"""
    response.delete_cookie(
        key=settings.auth_cookie_name,
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
        path="/",
    )


@router.post("/register", summary="自助注册（角色固定为客户）", response_model=AuthResponse)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> AuthResponse:
    """开放注册。注册成功后**直接登录**，省掉一次往返。

    角色由服务端固定为 `customer`——请求模型里根本没有 role 字段，
    因此不存在「构造请求把自己注册成管理员」的路径。管理员账号只能由
    `scripts/user_cli.py` 创建。

    Raises:
        AppError: 该部署关闭了自助注册（400）。
        UserConflictError: 用户名已被占用（409）。
    """
    user, _ = auth.register(
        username=payload.username,
        password=payload.password,
        display_name=payload.display_name,
        client_ip=_client_ip(request),
    )
    # 注册即登录：立刻签发会话，免去用户再输一次密码
    _, token = auth.login(
        username=user.username,
        password=payload.password,
        user_agent=request.headers.get("user-agent", ""),
        client_ip=_client_ip(request),
    )
    _set_session_cookie(response, settings, token)
    return AuthResponse(user=user, permissions=auth.permissions_of(user.role))


@router.post("/login", summary="登录", response_model=AuthResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> AuthResponse:
    """校验凭据并下发会话 cookie。

    Raises:
        CredentialsError: 用户名或密码错误（401，提示统一，不区分二者）。
        AccountLockedError: 连续失败被锁定（423）。
        AccountDisabledError: 账号被禁用（403）。
    """
    user, token = auth.login(
        username=payload.username,
        password=payload.password,
        user_agent=request.headers.get("user-agent", ""),
        client_ip=_client_ip(request),
    )
    _set_session_cookie(response, settings, token)
    return AuthResponse(user=user, permissions=auth.permissions_of(user.role))


@router.post("/logout", summary="登出")
async def logout(
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, bool]:
    """删除服务端会话并清除 cookie。

    **刻意不要求已登录**：即使会话已过期，登出也应成功——
    否则前端会遇到「想登出却报 401」这种莫名其妙的状态。
    """
    token = request.cookies.get(settings.auth_cookie_name, "")
    auth.logout(token, actor="", client_ip=_client_ip(request))
    _clear_session_cookie(response, settings)
    return {"logged_out": True}


@router.get("/me", summary="获取当前登录用户", response_model=AuthResponse)
async def me(
    user: Annotated[UserInfo, CurrentUser],
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> AuthResponse:
    """返回当前身份与权限点，供前端决定显示哪些入口。

    这是前端启动时调用的第一个接口：401 说明未登录（弹登录框），
    200 则据此渲染角色相关的界面。
    """
    return AuthResponse(user=user, permissions=auth.permissions_of(user.role))


@router.post("/password", summary="修改自己的密码")
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[UserInfo, CurrentUser],
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, object]:
    """修改密码（需验证原密码）。

    改完之后**所有设备都会被踢下线**（包括当前这个），因此这里同时清掉 cookie，
    让前端立刻回到登录页，而不是等到下一次请求才发现 401。
    """
    auth.change_password(
        user=user,
        old_password=payload.old_password,
        new_password=payload.new_password,
        client_ip=_client_ip(request),
    )
    _clear_session_cookie(response, settings)
    logger.info("用户 {} 修改了密码，所有会话已失效", user.username)
    return {"changed": True, "relogin_required": True}
