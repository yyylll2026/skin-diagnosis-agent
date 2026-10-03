"""用户管理接口（管理员专用）。

## 为什么单独一个文件

知识库接口面向「运维/管理员」，这些接口面向「管理员对账号的操作」，
两者的权限点不同（`kb:*` vs `user:*`），失败语义也不同——
知识库操作失败多是格式问题（415/422），用户操作失败多是冲突与约束（403/409）。

**所有写操作都要求 `user:write`**，读操作要求 `user:read`。
客户角色两个权限点都没有，因此本文件的全部接口对客户都是 403。

## 两条守卫都放在服务层

「不能删自己」「不能删/降级最后一个管理员」这两条**不是**在接口层判断的，
而是 `AuthService` 内部的约束。原因是它们属于业务不变量：
将来如果加了 CLI 或后台任务也能改用户，这一层保护必须同样生效。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from app.dependencies import get_analysis_task_service, get_auth_service, require_permission
from app.schemas.auth import (
    ResetPasswordRequest,
    UserCreateRequest,
    UserInfo,
    UserUpdateRequest,
)
from app.services.analysis_task_service import AnalysisTaskService
from app.services.auth_service import (
    PERMISSION_AUDIT_READ,
    PERMISSION_USER_READ,
    PERMISSION_USER_WRITE,
    AuthService,
)

router = APIRouter(prefix="/users", tags=["用户管理"])
audit_router = APIRouter(prefix="/audit", tags=["审计"])


def _client_ip(request: Request) -> str:
    """取客户端 IP（只信任直连来源，理由见 auth.py 同名函数）。"""
    return request.client.host if request.client else ""


@router.get("", summary="用户列表", response_model=list[UserInfo])
async def list_users(
    auth: Annotated[AuthService, Depends(get_auth_service)],
    _: Annotated[UserInfo, Depends(require_permission(PERMISSION_USER_READ))],
    keyword: Annotated[str, Query(description="按用户名或显示名模糊搜索")] = "",
) -> list[UserInfo]:
    """列出用户，支持关键字搜索。响应模型里没有密码字段，见 `UserInfo`。"""
    return auth.list_users(keyword=keyword)


@router.post("", summary="创建用户（可指定角色）", response_model=UserInfo)
async def create_user(
    payload: UserCreateRequest,
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    actor: Annotated[UserInfo, Depends(require_permission(PERMISSION_USER_WRITE))],
) -> UserInfo:
    """管理员建号。与自助注册的关键区别是**可以指定角色**。

    Raises:
        UserConflictError: 用户名已被占用（409）。
        AppError: 密码长度不达标（400）。
    """
    return auth.create_user(
        username=payload.username,
        password=payload.password,
        role=payload.role,
        display_name=payload.display_name,
        actor=actor.username,
        client_ip=_client_ip(request),
    )


@router.patch("/{user_id}", summary="修改用户（显示名 / 角色 / 状态）", response_model=UserInfo)
async def update_user(
    user_id: str,
    payload: UserUpdateRequest,
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    actor: Annotated[UserInfo, Depends(require_permission(PERMISSION_USER_WRITE))],
) -> UserInfo:
    """局部更新：只改传了的字段。

    把状态改为 `disabled` 会**立即踢掉该用户的所有会话**，实现「禁用即下线」。

    Raises:
        PermissionDeniedError: 试图禁用/降级自己，或会导致系统没有可用管理员（403）。
    """
    return auth.update_user(
        user_id,
        display_name=payload.display_name,
        role=payload.role,
        status=payload.status,
        actor=actor,
        client_ip=_client_ip(request),
    )


@router.delete("/{user_id}", summary="删除用户")
async def delete_user(
    user_id: str,
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    actor: Annotated[UserInfo, Depends(require_permission(PERMISSION_USER_WRITE))],
    tasks: Annotated[AnalysisTaskService, Depends(get_analysis_task_service)],
) -> dict[str, Any]:
    """删除用户。

    会连带清掉其登录会话；**对话历史保留**，因为那属于审计线索，
    删掉会让「谁在什么时候问过什么」无从追溯。

    Raises:
        PermissionDeniedError: 试图删自己，或删除最后一个启用中的管理员（403）。
    """
    auth.delete_user(user_id, actor=actor, client_ip=_client_ip(request))
    await tasks.purge_user(user_id)
    return {"deleted": True, "user_id": user_id}


@router.post("/{user_id}/password", summary="重置他人密码")
async def reset_password(
    user_id: str,
    payload: ResetPasswordRequest,
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    actor: Annotated[UserInfo, Depends(require_permission(PERMISSION_USER_WRITE))],
) -> dict[str, Any]:
    """重置指定用户的密码，并踢掉其所有会话。

    **不能用来重置自己的密码**——那等于绕过了「修改密码需验证原密码」这道防线。
    管理员改自己的密码请用 `/auth/password`。

    Raises:
        PermissionDeniedError: 目标是当前登录账号（403）。
    """
    auth.reset_password(user_id, payload.new_password, actor=actor, client_ip=_client_ip(request))
    return {"reset": True, "user_id": user_id, "sessions_revoked": True}


@router.post("/{user_id}/force-logout", summary="强制某用户下线")
async def force_logout(
    user_id: str,
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    actor: Annotated[UserInfo, Depends(require_permission(PERMISSION_USER_WRITE))],
) -> dict[str, Any]:
    """踢掉目标用户的全部登录会话。

    这是选择「服务端会话」而非 JWT 换来的能力：撤销就是删一行，
    不需要维护黑名单，也不会出现「token 还没过期所以踢不掉」的尴尬。
    """
    count = auth.force_logout(user_id, actor=actor.username, client_ip=_client_ip(request))
    return {"user_id": user_id, "revoked_sessions": count}


@router.get("/{user_id}/sessions", summary="查看某用户的在线会话")
async def list_user_sessions(
    user_id: str,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    _: Annotated[UserInfo, Depends(require_permission(PERMISSION_USER_READ))],
) -> list[dict[str, Any]]:
    """列出该用户当前有效的登录会话（设备、IP、最后活跃时间）。

    只返回元信息，**不返回任何凭据**（库里存的本来也只是 token 的哈希）。
    """
    return [
        {
            "created_at": row.get("created_at", ""),
            "last_seen_at": row.get("last_seen_at", ""),
            "expires_at": row.get("expires_at", ""),
            "client_ip": row.get("client_ip", ""),
            "user_agent": row.get("user_agent", ""),
        }
        for row in auth.store.list_user_auth_sessions(user_id)
    ]


@audit_router.get("/logs", summary="审计日志")
async def list_audit_logs(
    auth: Annotated[AuthService, Depends(get_auth_service)],
    _: Annotated[UserInfo, Depends(require_permission(PERMISSION_AUDIT_READ))],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    action: Annotated[str, Query(description="按动作过滤，如 login_failed")] = "",
) -> list[dict[str, Any]]:
    """查看审计日志（登录成败、管理操作）。

    审计表只增不改，是排查「谁动了什么」的唯一依据。
    """
    return auth.list_audit_logs(limit=limit, action=action)
