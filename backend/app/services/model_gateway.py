"""模型网关工厂：按 ``SKIN_LLM_BACKEND`` 返回 Ollama 或云端客户端。

业务层只依赖与 ``OllamaClient`` 同构的接口，不关心底下是本机还是百炼。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.config import get_settings


@lru_cache(maxsize=1)
def get_model_gateway() -> Any:
    """返回当前配置下的模型网关单例（OllamaClient 或 CloudLLMClient）。"""
    settings = get_settings()
    if settings.llm_backend == "cloud":
        from app.services.cloud_client import CloudLLMClient

        return CloudLLMClient(
            settings.resolved_cloud_api_base,
            api_key=settings.resolved_cloud_api_key,
            default_timeout=settings.llm_timeout,
        )

    from app.services.ollama_client import OllamaClient

    return OllamaClient(settings.ollama_api, default_timeout=settings.llm_timeout)


def clear_model_gateway() -> None:
    """清掉单例（测试或热切换配置时用）。"""
    get_model_gateway.cache_clear()
