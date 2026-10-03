"""依赖注入装配。

FastAPI 的 ``Depends`` 与 LangGraph 的节点都从这里取单例，好处是：
- 单例只初始化一次（Chroma 客户端、SQLite 连接工厂、编译后的图）；
- 测试时可以用 ``lru_cache`` 的 ``cache_clear()`` 整体替换依赖；
- 所有服务都接受 ``settings`` 参数，便于用不同配置起多套实例做对比实验。

鉴权相关的两个依赖（``get_current_user`` / ``require_permission``）也在这里，
因为它们和其余依赖一样是「装配」问题：接口只声明「我需要什么权限」，
「怎么拿到当前用户」「权限表长什么样」都由这一层决定。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request

from app.agent.nodes import AgentNodes
from app.config import Settings, get_settings
from app.core.errors import AuthenticationError
from app.repositories.db import MetadataStore
from app.repositories.vector_store import get_vectorstore
from app.schemas.auth import UserInfo


@lru_cache(maxsize=1)
def get_metadata_store() -> MetadataStore:
    """元数据仓储单例。连接串由 ``SKIN_DATABASE_URL`` 决定（默认 SQLite）。"""
    return MetadataStore(get_settings().resolved_database_url)


@lru_cache(maxsize=1)
def get_ingest_service() -> IngestService:  # noqa: F821 - 延迟导入避免循环
    """入库服务单例。"""
    from app.services.ingest_service import IngestService

    settings: Settings = get_settings()
    return IngestService(settings, get_metadata_store())


@lru_cache(maxsize=1)
def get_retrieval_service() -> RetrievalService:  # noqa: F821
    """检索服务单例。

    注意：与 ``app.services.retrieval_service.get_retrieval_service`` 是同一个对象，
    这里只是转发，避免出现两份 BM25 缓存。
    """
    from app.services.retrieval_service import get_retrieval_service as _get

    return _get()


@lru_cache(maxsize=1)
def get_vision_service() -> VisionService:  # noqa: F821
    """视觉服务单例。"""
    from app.services.vision_service import get_vision_service as _get

    return _get()


@lru_cache(maxsize=1)
def get_safety_service() -> SafetyService:  # noqa: F821
    """安全服务单例。"""
    from app.services.safety_service import SafetyService

    return SafetyService()


@lru_cache(maxsize=1)
def get_fusion_service() -> FusionService:  # noqa: F821
    """融合服务单例。"""
    from app.services.fusion_service import FusionService

    return FusionService()


@lru_cache(maxsize=1)
def get_health_trend_service() -> HealthTrendService:  # noqa: F821
    """病程趋势聚合服务单例（无状态纯计算，缓存只为统一装配风格）。"""
    from app.services.health_trend_service import HealthTrendService

    return HealthTrendService()


@lru_cache(maxsize=1)
def get_retrieval_eval_service() -> RetrievalEvalService:  # noqa: F821
    """检索评测服务单例。

    与检索服务共享同一个 ``RetrievalService``，否则评测跑的是另一份
    BM25 缓存，测出来的指标和线上实际链路不是一回事。
    """
    from app.services.retrieval_eval_service import RetrievalEvalService

    return RetrievalEvalService(get_settings(), get_retrieval_service())


@lru_cache(maxsize=1)
def get_clarify_service() -> ClarifyService:  # noqa: F821
    """信息完备性判断与追问澄清服务单例。

    复用同一个 ``MetadataStore``（它自己也是单例），
    避免出现两份指向同一 SQLite 的仓储实例。
    """
    from app.services.clarify_service import ClarifyService

    return ClarifyService(get_settings(), get_metadata_store())


@lru_cache(maxsize=1)
def get_auth_service() -> AuthService:  # noqa: F821
    """鉴权服务单例。"""
    from app.services.auth_service import AuthService

    return AuthService(get_settings(), get_metadata_store())


# ---------------------------------------------------------------------------
# 鉴权依赖
# ---------------------------------------------------------------------------


def get_current_user(request: Request) -> UserInfo:
    """解析当前登录用户。

    刻意写成同步函数：FastAPI 会把同步依赖丢进线程池执行，
    因此这里的 SQLite 查询不会阻塞事件循环。若写成 ``async def``，
    这个同步查询就会成为所有请求路径上的阻塞点。

    ``auth_enabled=false`` 时返回合成管理员，从而让既有行为完全不变。

    Raises:
        AuthenticationError: 未携带有效会话（401），前端据此弹登录框。
    """
    service = get_auth_service()
    if not service.enabled:
        return service.local_admin()

    token = request.cookies.get(get_settings().auth_cookie_name, "")
    user = service.resolve_session(token)
    if user is None:
        raise AuthenticationError("登录状态已失效，请重新登录")
    return user


# 便于在端点签名里写成 Annotated[UserInfo, CurrentUser]
CurrentUser = Depends(get_current_user)


def require_permission(permission: str) -> object:
    """构造一个「要求某权限点」的依赖。

    用法::

        @router.post("/kb/documents")
        async def upload(user: Annotated[UserInfo, Depends(require_permission(PERMISSION_KB_WRITE))]):
            ...

    权限点写在**它保护的那个接口旁边**，而「哪个角色拥有它」集中在
    ``AuthService.PERMISSIONS``——两者分离，避免权限定义散落各处后互相矛盾。
    """

    def _guard(user: Annotated[UserInfo, Depends(get_current_user)]) -> UserInfo:
        get_auth_service().ensure_permission(user, permission)
        return user

    return _guard


@lru_cache(maxsize=1)
def build_nodes() -> AgentNodes:
    """构建节点集合（供 graph 装配使用）。"""
    return AgentNodes(
        vision_service=get_vision_service(),
        retrieval_service=get_retrieval_service(),
        fusion_service=get_fusion_service(),
        safety_service=get_safety_service(),
        clarify_service=get_clarify_service(),
    )


@lru_cache(maxsize=1)
def get_runner_dep() -> AgentRunner:  # noqa: F821
    """智能体运行器单例。"""
    # 触发一次图编译，让首次请求不必承担编译延迟
    from app.agent.graph import get_graph
    from app.agent.runner import get_runner

    get_graph()
    return get_runner()


@lru_cache(maxsize=1)
def get_analysis_task_service() -> AnalysisTaskService:  # noqa: F821
    """异步图像分析任务服务单例。"""
    from app.services.analysis_task_service import AnalysisTaskService

    settings = get_settings()
    from app.agent.runner import get_runner

    return AnalysisTaskService(
        get_metadata_store(),
        get_runner(),
        max_concurrency=settings.analysis_max_concurrency,
        max_pending=settings.analysis_max_pending,
        retry_cache_minutes=settings.analysis_retry_cache_minutes,
    )


@lru_cache(maxsize=1)
def get_ollama_client_dep():  # noqa: F821
    """模型网关单例（Ollama 或云端）；函数名保留以兼容既有导入。"""
    from app.services.model_gateway import get_model_gateway

    return get_model_gateway()


def warmup() -> None:
    """预热：提前构建 Chroma 集合与编译状态图，避免首个请求超时。"""
    # 先建元数据库连接并把 schema 升到最新：连接串写错、或容器里漏拷
    # alembic/ 目录时，应该在启动日志里就暴露出来，而不是等第一个请求才 500。
    get_metadata_store()
    get_vectorstore()
    get_runner_dep()


__all__ = [
    "CurrentUser",
    "build_nodes",
    "get_analysis_task_service",
    "get_auth_service",
    "get_clarify_service",
    "get_current_user",
    "get_fusion_service",
    "get_health_trend_service",
    "get_ingest_service",
    "get_metadata_store",
    "get_ollama_client_dep",
    "get_retrieval_eval_service",
    "get_retrieval_service",
    "get_runner_dep",
    "get_safety_service",
    "get_vision_service",
    "require_permission",
    "warmup",
]
