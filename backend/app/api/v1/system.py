"""系统状态与会话接口。

``/system/health`` 是前端左侧栏状态灯的数据源，因此它的**降级策略**很重要：
即使 Ollama 挂了、embedding 模型没拉，接口也必须正常返回 200 并把问题写在
``warnings`` 里，而不是抛 5xx——否则前端会显示「服务异常」，
用户根本看不到「该 ollama pull bge-m3」这种可操作的提示。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.agent.graph import graph_mermaid
from app.config import Settings, get_settings
from app.dependencies import (
    get_metadata_store,
    get_ollama_client_dep,
)
from app.repositories import vector_store
from app.repositories.db import MetadataStore
from app.schemas.chat import HealthResponse, ModelInfo, SessionHistory, SessionMessage
from app.services.ollama_client import OllamaClient

router = APIRouter(prefix="/system", tags=["系统"])
sessions_router = APIRouter(prefix="/sessions", tags=["会话"])


@router.get("/health", summary="健康检查", response_model=HealthResponse)
async def health(
    settings: Annotated[Settings, Depends(get_settings)],
    client: Annotated[OllamaClient, Depends(get_ollama_client_dep)],
) -> HealthResponse:
    """返回各依赖组件的就绪状态。

    不做「不健康就返回 503」的处理，理由见模块文档：这里的定位是诊断面板，不是探针。
    """
    warnings: list[str] = []
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
        warnings.append(
            f"无法连接 Ollama（{settings.ollama_api}），请确认已执行 ollama serve"
        )

    vision_ready = ready(settings.vision_model)
    if ollama_connected and not vision_ready:
        warnings.append(f"视觉模型未就绪，请执行：ollama pull {settings.vision_model}")

    text_ready = ready(settings.text_model)
    if ollama_connected and not text_ready:
        warnings.append(f"文本模型未就绪，请执行：ollama pull {settings.text_model}")

    embedding_ready = True
    if settings.embedding_backend == "ollama":
        embedding_ready = ready(settings.embedding_model)
        if ollama_connected and not embedding_ready:
            warnings.append(
                f"向量模型未就绪，请执行：ollama pull {settings.embedding_model}"
                "（或改用 SKIN_EMBEDDING_BACKEND=chroma_default 做快速验证）"
            )

    documents = get_metadata_store().list_documents()
    chunks = vector_store.count_chunks()
    if chunks == 0:
        warnings.append("知识库为空，请先上传皮肤科文档，否则问答将缺少依据")

    return HealthResponse(
        status="ok" if not warnings else "degraded",
        ollama_connected=ollama_connected,
        ollama_base_url=settings.ollama_api,
        vision_model=settings.vision_model,
        vision_model_ready=vision_ready,
        text_model=settings.text_model,
        text_model_ready=text_ready,
        embedding_backend=settings.embedding_backend,
        embedding_model=settings.embedding_model,
        embedding_model_ready=embedding_ready,
        collection=settings.chroma_collection,
        indexed_chunks=chunks,
        indexed_documents=len(documents),
        warnings=warnings,
    )


@router.get("/models", summary="列出 Ollama 可用模型", response_model=list[ModelInfo])
async def list_models(
    client: Annotated[OllamaClient, Depends(get_ollama_client_dep)],
) -> list[ModelInfo]:
    """列出本地 Ollama 已拉取的模型及其能力（vision / tools / thinking）。"""
    models = await client.list_models()
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
async def graph_definition() -> dict[str, Any]:
    """返回 LangGraph 的 Mermaid 源码，便于在界面或文档中可视化编排拓扑。"""
    return {"mermaid": graph_mermaid()}


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


@sessions_router.get("", summary="列出会话")
async def list_sessions(
    store: Annotated[MetadataStore, Depends(get_metadata_store)],
) -> list[dict[str, Any]]:
    """列出全部会话（按最近活跃倒序）。"""
    return store.list_sessions()


@sessions_router.get("/{session_id}", summary="获取会话历史", response_model=SessionHistory)
async def get_session(
    session_id: str,
    store: Annotated[MetadataStore, Depends(get_metadata_store)],
) -> SessionHistory:
    """获取指定会话的消息历史（时间正序）。"""
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
) -> dict[str, Any]:
    """删除会话及其全部消息。"""
    store.delete_session(session_id)
    return {"deleted": True, "session_id": session_id}
