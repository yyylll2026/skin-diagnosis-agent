"""统一对话入口：知识库问答 + 多模态图文对话。

本文件是整个系统对外的「主入口」。三个接口最终都汇流到同一张 LangGraph 状态图，
差异只在入参组装与响应形态：

```
POST /agent/chat       图文混合，SSE 流式         ┐
POST /chat/qa          纯文本问答，两种响应形态    ├─► build_state() ─► AgentRunner ─► LangGraph
POST /diagnosis/image  multipart 图片上传        ┘
```

这样设计的原因：如果为「问答」和「诊断」各写一套编排逻辑，二者会迅速分叉——
诊断需要知识库依据、问答也可能被追问「那照片这样是什么」，维护两套必然出现行为不一致。
统一成一张图后，「什么时候检索、什么时候调视觉模型」由一个地方（route 节点）决定。
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from sse_starlette.sse import EventSourceResponse

from app.agent.runner import AgentRunner
from app.config import Settings, get_settings
from app.dependencies import get_metadata_store, get_runner_dep
from app.repositories.db import MetadataStore
from app.schemas.chat import ChatRequest, ChatResponse, Intent
from app.schemas.diagnosis import DiagnosisResult, ImageMeta, SafetyReport, VisionDiagnosis
from app.schemas.knowledge import RetrievedChunk

router = APIRouter(tags=["对话与诊断"])


def _new_session_id() -> str:
    """生成会话 ID。"""
    return f"s-{uuid.uuid4().hex[:12]}"


def _load_history(store: MetadataStore, session_id: str, settings: Settings) -> list[BaseMessage]:
    """从 SQLite 读取最近若干轮对话，转成 LangChain 消息。

    只取最近 N 轮（默认 6 轮）而不是全量，原因有两个：
    一是本地 7B 模型 num_ctx 有限，历史会把检索上下文挤掉；
    二是医学问答里太久远的上下文参考价值快速衰减。

    Args:
        store: 元数据仓储。
        session_id: 会话 ID。
        settings: 全局配置。

    Returns:
        LangChain 消息列表（时间正序）。注意最后一条会是本轮用户消息，
        但提示词里的 ``{question}`` 已单独承载，这里保留不影响（模型会看到重复，
        属于可接受的无害冗余；若要严格去重可在拼 history 时排除最后一条）。
    """
    limit = max(2, settings.session_history_turns * 2)
    rows = store.list_messages(session_id, limit=limit)

    messages: list[BaseMessage] = []
    for row in rows[:-1] if rows else []:
        content = row.get("content") or ""
        if not content.strip():
            continue
        if row.get("role") == "user":
            # 历史里的图片不重放（体积大且无必要），用占位符保留语义
            text = (
                f"[用户上传了一张皮肤照片] {content}".strip()
                if row.get("has_image")
                else content
            )
            messages.append(HumanMessage(content=text))
        else:
            messages.append(AIMessage(content=content))
    return messages


def _sse(payload: dict[str, Any]) -> dict[str, str]:
    """把事件字典包装成 SSE 消息。"""
    return {
        "event": payload.get("type", "message"),
        "data": json.dumps(payload, ensure_ascii=False),
    }


def _stream_response(
    runner: AgentRunner, state: dict[str, Any], store: MetadataStore | None = None
) -> EventSourceResponse:
    """把运行器事件流包装成 SSE 响应。"""

    async def event_stream() -> AsyncIterator[dict[str, str]]:
        async for event in runner.astream_events(state):
            # 完整答案落库放在 done 事件上：即使中途客户端断开，已生成的内容仍会入库
            if store is not None and event.get("type") == "done" and event.get("answer"):
                store.add_message(str(state.get("session_id")), "assistant", str(event["answer"]))
            yield _sse(event)

    return EventSourceResponse(event_stream(), ping=15)


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------


@router.post("/agent/chat", summary="智能体统一入口（图文混合，支持 SSE 流式）")
async def agent_chat(
    payload: ChatRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[MetadataStore, Depends(get_metadata_store)],
    runner: Annotated[AgentRunner, Depends(get_runner_dep)],
) -> Any:
    """图文混合对话入口。

    - ``stream=true``（默认）返回 ``text/event-stream``，事件类型见 ``AgentRunner`` 文档；
    - ``stream=false`` 返回完整 JSON，适合脚本调用与自动化测试。

    会话 ID 为空时由后端生成并回传，前端后续请求沿用同一个 ID 即可获得多轮上下文。
    """
    session_id = payload.session_id or _new_session_id()
    store.ensure_session(session_id, title=(payload.message or "图片咨询")[:30])

    if payload.message.strip():
        store.add_message(
            session_id, "user", payload.message, has_image=bool(payload.image_base64)
        )
    elif payload.image_base64:
        store.add_message(session_id, "user", "[上传了一张皮肤照片]", has_image=True)

    state = AgentRunner.build_state(
        session_id=session_id,
        question=payload.message,
        image_base64=payload.image_base64,
        image_mime=payload.image_mime,
        use_knowledge=payload.use_knowledge,
        history=_load_history(store, session_id, settings),
    )

    if not payload.stream:
        final = await runner.arun(state)
        answer = str(final.get("answer") or "")
        store.add_message(session_id, "assistant", answer)
        vision = final.get("vision")
        return ChatResponse(
            session_id=session_id,
            intent=Intent(final.get("intent") or Intent.KB_QA.value),
            answer=answer,
            vision=VisionDiagnosis.model_validate(vision) if vision else None,
            sources=[RetrievedChunk.model_validate(item) for item in (final.get("sources") or [])],
            safety=SafetyReport.model_validate(final.get("safety") or {}),
            elapsed_ms=int(sum((final.get("timings") or {}).values())),
        )

    return _stream_response(runner, state, store)


@router.post("/chat/qa", summary="知识库问答（纯文本，支持 SSE 流式）")
async def chat_qa(
    payload: ChatRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[MetadataStore, Depends(get_metadata_store)],
    runner: Annotated[AgentRunner, Depends(get_runner_dep)],
) -> Any:
    """纯文本知识库问答。

    是 ``/agent/chat`` 的语义化别名：忽略图片字段，强制走「检索 → 生成」链路。
    单独保留这个接口，是为了让前端「文字问答」入口的契约更清晰，
    也便于对 RAG 效果做单变量测试（不掺杂视觉分支）。
    """
    text_only = payload.model_copy(update={"image_base64": None})
    return await agent_chat(text_only, settings=settings, store=store, runner=runner)


# ---------------------------------------------------------------------------
# 图片诊断
# ---------------------------------------------------------------------------


@router.post("/diagnosis/image", summary="图片识别与诊断（multipart 上传）")
async def diagnose_image(
    file: Annotated[UploadFile, File(description="皮肤照片，支持 jpg/png/webp")],
    session_id: Annotated[str | None, Form()] = None,
    note: Annotated[str, Form(description="用户补充描述")] = "",
    use_knowledge: Annotated[bool, Form(description="是否检索知识库补充依据")] = True,
    # 有默认值的依赖一律用 `= Depends(...)` 经典写法：写成
    # `Annotated[T, Depends(...)] = None` 会让 FastAPI 断言「Annotated 与默认值冲突」
    settings: Settings = Depends(get_settings),
    store: MetadataStore = Depends(get_metadata_store),
    runner: AgentRunner = Depends(get_runner_dep),
) -> DiagnosisResult:
    """图片识别接口：接收 multipart 文件，返回结构化诊断 + 知识依据。

    与 ``/agent/chat`` 的区别是**上传方式与响应形态**：
    这里走 multipart（更适合大文件与浏览器原生表单），返回一次性 JSON
    （前端把它渲染成诊断卡片而不是流式气泡）。

    内部复用同一张状态图，因此「图片 → 视觉分析 → 检索依据 → 融合回答」
    的链路与流式入口完全一致，不存在两套行为。

    Raises:
        ImageValidationError: 图片格式不支持、体积超限或已损坏。
    """
    from app.core.errors import ImageValidationError

    if not file.content_type or not file.content_type.startswith("image/"):
        raise ImageValidationError(
            f"仅支持图片文件，当前类型为 {file.content_type or '未知'}"
        )

    raw = await file.read()
    session_id = session_id or _new_session_id()
    store.ensure_session(session_id, title="图片诊断")
    store.add_message(session_id, "user", note or "[上传了一张皮肤照片]", has_image=True)

    state = AgentRunner.build_state(
        session_id=session_id,
        question=note,
        image_base64=base64.b64encode(raw).decode("ascii"),
        image_mime=file.content_type,
        use_knowledge=use_knowledge,
        history=_load_history(store, session_id, settings),
    )

    final = await runner.arun(state)
    answer = str(final.get("answer") or "")
    store.add_message(session_id, "assistant", answer)

    vision = final.get("vision")
    meta = final.get("image_meta") or {}
    safety = SafetyReport.model_validate(final.get("safety") or {})

    if not vision:
        # 视觉失败时降级为文本回答：把错误信息透传给前端，避免用户面对空白卡片
        errors = final.get("errors") or []
        detail = errors[0] if errors else "图像分析未返回结果"
        return DiagnosisResult(
            session_id=session_id,
            diagnosis=VisionDiagnosis(summary=detail, image_quality=detail),
            image_meta=ImageMeta.model_validate(meta) if meta else ImageMeta(),
            sources=[RetrievedChunk.model_validate(item) for item in (final.get("sources") or [])],
            answer=answer,
            safety=safety,
            elapsed_ms=int(sum((final.get("timings") or {}).values())),
        )

    return DiagnosisResult(
        session_id=session_id,
        diagnosis=VisionDiagnosis.model_validate(vision),
        image_meta=ImageMeta.model_validate(meta) if meta else ImageMeta(),
        sources=[RetrievedChunk.model_validate(item) for item in (final.get("sources") or [])],
        answer=answer,
        safety=safety,
        elapsed_ms=int(sum((final.get("timings") or {}).values())),
    )
