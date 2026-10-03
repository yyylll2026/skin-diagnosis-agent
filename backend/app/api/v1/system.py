"""系统状态与会话接口。

``/system/health`` 是前端左侧栏状态灯的数据源，因此它的**降级策略**很重要：
即使 Ollama 挂了、embedding 模型没拉，接口也必须正常返回 200 并把问题写在
``warnings`` 里，而不是抛 5xx——否则前端会显示「服务异常」，
用户根本看不到「该 ollama pull bge-m3」这种可操作的提示。

## 鉴权在哪些地方介入

- ``/system/health`` **保持公开**：它得回答「这个系统要不要登录」，
  如果它自己都要登录，登录页就无从判断该不该显示。
- ``/system/models`` 与 ``/system/graph`` 要求登录：它们暴露部署细节（模型名、
  编排拓扑），对未认证访客没有价值。
- ``/sessions*`` 要求登录，且**客户端只能看到自己的会话**——
  这正是本次鉴权改造中改动面最大的一块，因为会话表原本没有归属概念。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.agent.graph import graph_mermaid
from app.config import Settings, get_settings
from app.dependencies import (
    get_analysis_task_service,
    get_auth_service,
    get_current_user,
    get_metadata_store,
    get_ollama_client_dep,
)
from app.repositories import vector_store
from app.repositories.db import MetadataStore
from app.schemas.auth import UserInfo
from app.schemas.chat import HealthResponse, ModelInfo, SessionHistory, SessionMessage
from app.services.analysis_task_service import AnalysisTaskService
from app.services.auth_service import PERMISSION_SESSION_READ_ALL, AuthService

router = APIRouter(prefix="/system", tags=["系统"])
sessions_router = APIRouter(prefix="/sessions", tags=["会话"])


@router.get("/health", summary="健康检查", response_model=HealthResponse)
async def health(
    settings: Annotated[Settings, Depends(get_settings)],
    client: Annotated[Any, Depends(get_ollama_client_dep)],
) -> HealthResponse:
    """返回各依赖组件的就绪状态。

    不做「不健康就返回 503」的处理，理由见模块文档：这里的定位是诊断面板，不是探针。
    """
    warnings: list[str] = []
    is_cloud = settings.llm_backend == "cloud"

    if is_cloud:
        api_key = settings.resolved_cloud_api_key
        ollama_connected = bool(api_key)
        if not ollama_connected:
            warnings.append(
                "云端模型未配置 API Key，请设置 SKIN_CLOUD_API_KEY 或 DASHSCOPE_API_KEY"
            )
        vision_model = settings.effective_vision_model
        text_model = settings.effective_text_model
        vision_ready = ollama_connected
        text_ready = ollama_connected
        model_base = settings.resolved_cloud_api_base
    else:
        models = await client.list_models()
        ollama_connected = bool(models)
        names = {str(m.get("name", "")) for m in models}

        def ready(model: str) -> bool:
            """判断模型是否已拉取（兼容不带 tag 的写法）。"""
            if model in names:
                return True
            base = model.split(":")[0]
            return any(name.split(":")[0] == base for name in names)

        if not ollama_connected:
            warnings.append(f"无法连接 Ollama（{settings.ollama_api}），请确认已执行 ollama serve")

        vision_model = settings.vision_model
        text_model = settings.text_model
        vision_ready = ready(vision_model)
        if ollama_connected and not vision_ready:
            warnings.append(f"视觉模型未就绪，请执行：ollama pull {vision_model}")

        text_ready = ready(text_model)
        if ollama_connected and not text_ready:
            warnings.append(f"文本模型未就绪，请执行：ollama pull {text_model}")
        model_base = settings.ollama_api

    embedding_backend = settings.embedding_backend
    embedding_model = settings.effective_embedding_model
    embedding_ready = True

    if embedding_backend == "ollama" and not is_cloud:
        embedding_ready = ready(settings.embedding_model)
        if ollama_connected and not embedding_ready:
            warnings.append(
                f"向量模型未就绪，请执行：ollama pull {settings.embedding_model}"
                "（或改用 SKIN_EMBEDDING_BACKEND=chroma_default 做快速验证）"
            )
    elif embedding_backend == "cloud":
        embedding_ready = ollama_connected
        if not embedding_ready:
            warnings.append("云端向量化未就绪：缺少 API Key 或 embedding 后端配置错误")

    documents = get_metadata_store().list_documents()
    chunks = vector_store.count_chunks()
    if chunks == 0:
        warnings.append("知识库为空，请先上传皮肤科文档，否则问答将缺少依据")

    return HealthResponse(
        status="ok" if not warnings else "degraded",
        llm_backend=settings.llm_backend,
        # 前端状态灯读 ollama_connected；云端模式下用它表示「模型网关可达」
        ollama_connected=ollama_connected,
        ollama_base_url=model_base,
        vision_model=vision_model,
        vision_model_ready=vision_ready,
        text_model=text_model,
        text_model_ready=text_ready,
        embedding_backend=embedding_backend,
        embedding_model=embedding_model,
        embedding_model_ready=embedding_ready,
        collection=settings.chroma_collection,
        indexed_chunks=chunks,
        indexed_documents=len(documents),
        warnings=warnings,
        auth_enabled=bool(settings.auth_enabled),
        allow_registration=bool(settings.auth_allow_registration),
    )


@router.get("/models", summary="列出可用模型", response_model=list[ModelInfo])
async def list_models(
    client: Annotated[Any, Depends(get_ollama_client_dep)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[UserInfo, Depends(get_current_user)],
) -> list[ModelInfo]:
    """列出模型网关中的可用模型及其能力（vision / tools / thinking）。"""
    models = await client.list_models()
    if settings.llm_backend == "cloud":
        return [ModelInfo(name=str(m.get("name", ""))) for m in models]
    result: list[ModelInfo] = []
    for item in models:
        details = item.get("details") or {}
        result.append(
            ModelInfo(
                name=str(item.get("name", "")),
                parameter_size=str(details.get("parameter_size", "")),
                capabilities=[str(c) for c in (item.get("capabilities") or [])],
                size_gb=round(float(item.get("size", 0)) / 1e9, 2),
            )
        )
    return result


@router.get("/graph", summary="导出智能体流程图（Mermaid）")
async def graph_definition(
    _: Annotated[UserInfo, Depends(get_current_user)],
) -> dict[str, Any]:
    """返回 LangGraph 的 Mermaid 源码，便于在界面或文档中可视化编排拓扑。

    要求登录：编排拓扑属于部署细节，对未认证访客没有价值。
    """
    return {"mermaid": graph_mermaid()}


# ---------------------------------------------------------------------------
# 会话
#
# 三个接口都加了两道逻辑：必须登录，且客户端只能触及自己的会话。
# 归属过滤放在 `visible_user_id_for_sessions`，访问校验放在
# `ensure_session_access`——两者都在服务层，因为它们是业务不变量，
# 将来从 CLI 或后台任务访问会话时同样要生效。
# ---------------------------------------------------------------------------


@sessions_router.get("", summary="列出会话")
async def list_sessions(
    store: Annotated[MetadataStore, Depends(get_metadata_store)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
    user: Annotated[UserInfo, Depends(get_current_user)],
) -> list[dict[str, Any]]:
    """列出会话（按最近活跃倒序）。

    客户端只返回自己的会话；管理员返回全部（其具有 `session:read_all`）。
    """
    return store.list_sessions(user_id=auth.visible_user_id_for_sessions(user))


@sessions_router.get("/{session_id}", summary="获取会话历史", response_model=SessionHistory)
async def get_session(
    session_id: str,
    store: Annotated[MetadataStore, Depends(get_metadata_store)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
    user: Annotated[UserInfo, Depends(get_current_user)],
) -> SessionHistory:
    """获取指定会话的消息历史（时间正序）。

    Raises:
        PermissionDeniedError: 该会话属于其他用户（403）。
    """
    auth.ensure_session_access(session_id, user)
    rows = store.list_messages(session_id, limit=200)
    return SessionHistory(
        session_id=session_id,
        messages=[
            SessionMessage(
                role=str(row.get("role", "user")),
                content=str(row.get("content", "")),
                created_at=str(row.get("created_at", "")),
                has_image=bool(row.get("has_image")),
            )
            for row in rows
        ],
    )


@sessions_router.delete("/{session_id}", summary="删除会话")
async def delete_session(
    session_id: str,
    store: Annotated[MetadataStore, Depends(get_metadata_store)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
    user: Annotated[UserInfo, Depends(get_current_user)],
    tasks: Annotated[AnalysisTaskService, Depends(get_analysis_task_service)],
) -> dict[str, Any]:
    """删除会话及其消息、追问状态和关联后台任务。

    Raises:
        PermissionDeniedError: 该会话属于其他用户（403）。
    """
    auth.ensure_session_access(session_id, user)
    task_owner = "" if auth.has_permission(user.role, PERMISSION_SESSION_READ_ALL) else user.user_id
    await tasks.purge_session(task_owner, session_id)
    store.delete_session(session_id)
    return {"deleted": True, "session_id": session_id}
