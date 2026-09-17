"""对话与智能体调用相关的数据契约（也是前后端接口契约）。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from app.schemas.clarify import ClarifyState
from app.schemas.diagnosis import SafetyReport, VisionDiagnosis
from app.schemas.knowledge import RetrievedChunk


class Intent(StrEnum):
    """意图路由结果。决定 LangGraph 走哪条分支。"""

    KB_QA = "kb_qa"  # 纯知识库问答
    IMAGE_DIAGNOSIS = "image_diagnosis"  # 图片诊断（含图文融合）
    CHITCHAT = "chitchat"  # 闲聊/寒暄，不必检索


class ChatRequest(BaseModel):
    """统一智能体入口请求体。

    这是前端的唯一对话入口：纯文本问答、图片诊断、图文融合都走这一个契约，
    差异只体现在 ``image_base64`` 是否为空、``use_knowledge`` 是否为真。
    """

    session_id: str | None = Field(default=None, description="为空则后端新建会话")
    message: str = Field(default="", max_length=2000, description="用户文本输入")
    image_base64: str | None = Field(
        default=None, description="图片 base64（不含 data URI 前缀），为空表示纯文本"
    )
    image_mime: str = Field(default="image/jpeg", description="图片 MIME 类型")
    use_knowledge: bool = Field(default=True, description="是否启用知识库检索增强")
    stream: bool = Field(default=True, description="是否以 SSE 流式返回")

    # ------------------------------------------------------------------
    # 容忍显式 null
    #
    # 踩过的坑：前端在没有图片时会序列化成 ``"image_mime": null``
    # （JS 里 ``obj.field ?? null`` 是最自然的写法），而后端的默认值
    # 只在字段**缺失**时生效，显式传 null 会被判为类型错误并返回 400。
    #
    # 这不是前端的错——「可选字段带默认值」的语义就应当包含「传 null 等于没传」。
    # 因此在这里统一把 null 归一到默认值，前端可以放心地不做字段裁剪。
    # ------------------------------------------------------------------

    @field_validator("message", mode="before")
    @classmethod
    def _null_message(cls, v: object) -> object:
        """``message: null`` 视为空字符串。"""
        return "" if v is None else v

    @field_validator("image_mime", mode="before")
    @classmethod
    def _null_mime(cls, v: object) -> object:
        """``image_mime: null`` 回落到默认 MIME。"""
        return "image/jpeg" if v is None else v

    @field_validator("use_knowledge", "stream", mode="before")
    @classmethod
    def _null_bool(cls, v: object) -> object:
        """布尔开关传 null 时回落到默认值（两者默认均为 True）。"""
        return True if v is None else v


class ChatResponse(BaseModel):
    """非流式响应体（``stream=false`` 时使用）。"""

    session_id: str
    intent: Intent
    answer: str
    vision: VisionDiagnosis | None = None
    sources: list[RetrievedChunk] = Field(default_factory=list)
    safety: SafetyReport = Field(default_factory=SafetyReport)
    # 信息完备性判断的结果：needs_clarification=true 时 answer 里是结构化追问
    clarify: ClarifyState = Field(default_factory=ClarifyState)
    elapsed_ms: int = 0


class SessionMessage(BaseModel):
    """会话中的单条消息（用于历史回放）。"""

    role: str
    content: str
    created_at: str = ""
    has_image: bool = False


class SessionHistory(BaseModel):
    """会话历史响应。"""

    session_id: str
    messages: list[SessionMessage] = Field(default_factory=list)


class ModelInfo(BaseModel):
    """Ollama 中的一个模型。"""

    name: str
    parameter_size: str = ""
    capabilities: list[str] = Field(default_factory=list)
    size_gb: float = 0.0


class HealthResponse(BaseModel):
    """健康检查响应。前端左侧栏底部的状态灯用这个。"""

    status: str = "ok"
    ollama_connected: bool = False
    ollama_base_url: str = ""
    vision_model: str = ""
    vision_model_ready: bool = False
    text_model: str = ""
    text_model_ready: bool = False
    embedding_backend: str = ""
    embedding_model: str = ""
    embedding_model_ready: bool = False
    collection: str = ""
    indexed_chunks: int = 0
    indexed_documents: int = 0
    warnings: list[str] = Field(default_factory=list)
    # 鉴权状态。前端据此决定是否弹登录框、是否显示「注册」入口。
    # 本接口必须保持公开（否则登录页连「系统是否要求登录」都问不到），
    # 因此这里只暴露开关本身，不暴露任何账号信息。
    auth_enabled: bool = False
    allow_registration: bool = False
