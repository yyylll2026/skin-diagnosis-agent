"""后台图像分析任务接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.agent.runner import AgentRunner
from app.api.v1.chat import _load_history, _new_session_id
from app.config import Settings, get_settings
from app.core.errors import ResourceNotFoundError
from app.dependencies import (
    get_analysis_task_service,
    get_auth_service,
    get_clarify_service,
    get_current_user,
    get_metadata_store,
)
from app.repositories.db import MetadataStore
from app.schemas.analysis_tasks import AnalysisTaskCreate, AnalysisTaskResponse
from app.schemas.auth import UserInfo
from app.services.analysis_task_service import AnalysisTaskService
from app.services.auth_service import PERMISSION_CHAT_USE, AuthService
from app.services.clarify_service import ClarifyService

router = APIRouter(prefix="/analysis-tasks", tags=["后台分析任务"])


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交后台图像分析任务",
    response_model=AnalysisTaskResponse,
)
async def create_analysis_task(
    payload: AnalysisTaskCreate,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    user: Annotated[UserInfo, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[MetadataStore, Depends(get_metadata_store)],
    clarify: Annotated[ClarifyService, Depends(get_clarify_service)],
    tasks: Annotated[AnalysisTaskService, Depends(get_analysis_task_service)],
) -> AnalysisTaskResponse:
    """立即返回任务 ID；图片只在服务进程内存中处理，不写入任务表或会话历史。"""
    session_id = payload.session_id or _new_session_id()
    auth.ensure_permission(user, PERMISSION_CHAT_USE)
    auth.ensure_session_access(session_id, user)
    tasks.ensure_capacity()
    store.ensure_session(
        session_id, title=(payload.message or "图片分析")[:30], user_id=user.user_id
    )

    if payload.message.strip():
        store.add_message(session_id, "user", payload.message, has_image=True)
    else:
        store.add_message(session_id, "user", "[上传了一张皮肤照片]", has_image=True)

    state = AgentRunner.build_state(
        session_id=session_id,
        question=payload.message,
        image_base64=payload.image_base64,
        image_mime=payload.image_mime,
        use_knowledge=payload.use_knowledge,
        history=_load_history(store, session_id, settings),
        clarify_pending=clarify.has_pending(session_id),
    )
    return tasks.create(user.user_id, session_id, state)


@router.get("", summary="列出我的分析任务", response_model=list[AnalysisTaskResponse])
async def list_analysis_tasks(
    user: Annotated[UserInfo, Depends(get_current_user)],
    tasks: Annotated[AnalysisTaskService, Depends(get_analysis_task_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AnalysisTaskResponse]:
    """按创建时间倒序返回当前账号的任务。"""
    return tasks.list(user.user_id, limit, offset)


@router.get("/{task_id}", summary="查询分析任务", response_model=AnalysisTaskResponse)
async def get_analysis_task(
    task_id: str,
    user: Annotated[UserInfo, Depends(get_current_user)],
    tasks: Annotated[AnalysisTaskService, Depends(get_analysis_task_service)],
) -> AnalysisTaskResponse:
    """任务只能由其创建者查询。"""
    result = tasks.get(task_id, user.user_id)
    if result is None:
        raise ResourceNotFoundError("分析任务不存在")
    return result


@router.post("/{task_id}/cancel", summary="取消分析任务", response_model=AnalysisTaskResponse)
async def cancel_analysis_task(
    task_id: str,
    user: Annotated[UserInfo, Depends(get_current_user)],
    tasks: Annotated[AnalysisTaskService, Depends(get_analysis_task_service)],
) -> AnalysisTaskResponse:
    """取消当前账号仍在排队或运行的任务。"""
    result = await tasks.cancel(task_id, user.user_id)
    if result is None:
        raise ResourceNotFoundError("分析任务不存在")
    return result


@router.post("/{task_id}/retry", summary="重试失败的分析任务", response_model=AnalysisTaskResponse)
async def retry_analysis_task(
    task_id: str,
    user: Annotated[UserInfo, Depends(get_current_user)],
    tasks: Annotated[AnalysisTaskService, Depends(get_analysis_task_service)],
) -> AnalysisTaskResponse:
    """仅在进程内短时保留了原始输入时重试；否则客户端需重新上传图片。"""
    result = tasks.retry(task_id, user.user_id)
    if result is None:
        raise ResourceNotFoundError("分析任务不存在")
    return result
