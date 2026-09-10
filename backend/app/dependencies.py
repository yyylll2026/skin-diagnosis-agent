"""依赖注入装配。

FastAPI 的 ``Depends`` 与 LangGraph 的节点都从这里取单例，好处是：
- 单例只初始化一次（Chroma 客户端、SQLite 连接工厂、编译后的图）；
- 测试时可以用 ``lru_cache`` 的 ``cache_clear()`` 整体替换依赖；
- 所有服务都接受 ``settings`` 参数，便于用不同配置起多套实例做对比实验。
"""

from __future__ import annotations

from functools import lru_cache

from app.agent.nodes import AgentNodes
from app.config import Settings, get_settings
from app.repositories.db import MetadataStore
from app.repositories.vector_store import get_vectorstore


@lru_cache(maxsize=1)
def get_metadata_store() -> MetadataStore:
    """元数据仓储单例。"""
    return MetadataStore(get_settings().resolved_sqlite_path)


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
def build_nodes() -> AgentNodes:
    """构建节点集合（供 graph 装配使用）。"""
    return AgentNodes(
        vision_service=get_vision_service(),
        retrieval_service=get_retrieval_service(),
        fusion_service=get_fusion_service(),
        safety_service=get_safety_service(),
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
def get_ollama_client_dep() -> OllamaClient:  # noqa: F821
    """Ollama 客户端单例。"""
    from app.services.ollama_client import OllamaClient

    settings = get_settings()
    return OllamaClient(settings.ollama_api, default_timeout=settings.llm_timeout)


def warmup() -> None:
    """预热：提前构建 Chroma 集合与编译状态图，避免首个请求超时。"""
    get_vectorstore()
    get_runner_dep()


__all__ = [
    "build_nodes",
    "get_fusion_service",
    "get_ingest_service",
    "get_metadata_store",
    "get_ollama_client_dep",
    "get_retrieval_service",
    "get_runner_dep",
    "get_safety_service",
    "get_vision_service",
    "warmup",
]
