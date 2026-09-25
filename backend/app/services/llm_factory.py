"""LangChain 模型工厂。

统一产出 LangChain 的 ``BaseChatModel`` / ``Embeddings`` 实例，供 LangGraph 节点与
各 service 复用。集中在此处的原因：

- **连接复用**：ChatOllama 内部持有 httpx 客户端，反复 new 会耗尽连接；
- **参数一致性**：``num_ctx`` / ``keep_alive`` 这类部署级参数只在一个地方设置；
- **可替换性**：把 embedding 后端抽象成工厂，切换 Ollama / sentence-transformers /
  Chroma 内置向量模型时，业务代码零改动。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from langchain_core.embeddings import Embeddings
from langchain_ollama import ChatOllama, OllamaEmbeddings
from loguru import logger

from app.config import Settings, get_settings
from app.core.errors import EmbeddingError, OllamaUnavailableError

# ---------------------------------------------------------------------------
# 文本生成模型
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def build_chat_model(model: str, temperature: float, num_predict: int) -> Any:
    """构造聊天模型实例（带缓存，相同参数复用同一实例）。

    Args:
        model: 模型名。
        temperature: 采样温度。知识问答建议 0.1~0.3 以抑制胡编。
        num_predict: 单次最大生成 token 数。

    Returns:
        ChatOllama 或云端 ChatOpenAI（BaseChatModel 兼容）。
    """
    settings: Settings = get_settings()

    if settings.llm_backend == "cloud":
        from langchain_openai import ChatOpenAI

        api_key = settings.resolved_cloud_api_key
        if not api_key:
            raise OllamaUnavailableError(
                "云端模型未配置 API Key",
                "请设置 SKIN_CLOUD_API_KEY 或 DASHSCOPE_API_KEY",
            )
        return ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=settings.resolved_cloud_api_base,
            temperature=temperature,
            max_tokens=num_predict,
            timeout=settings.llm_timeout,
            max_retries=2,
        )

    return ChatOllama(
        model=model,
        base_url=settings.ollama_api,
        temperature=temperature,
        num_ctx=settings.num_ctx,
        num_predict=num_predict,
        keep_alive=settings.keep_alive,
        # 超时放在 client_kwargs 里；本地 7B 模型冷启动可能超过 60s
        client_kwargs={"timeout": settings.llm_timeout},
    )


def get_text_llm(temperature: float = 0.2) -> Any:
    """主力文本模型：用于知识问答与图文融合生成。"""
    settings = get_settings()
    return build_chat_model(settings.effective_text_model, temperature, settings.num_predict)


def get_fast_llm(temperature: float = 0.0) -> Any:
    """轻量模型：用于意图分类、查询重写、重排等短任务，追求低延迟。"""
    settings = get_settings()
    return build_chat_model(settings.effective_fast_model, temperature, 512)


def get_vision_llm(temperature: float = 0.1) -> Any:
    """多模态模型（LangChain 路径）。

    仅在 ``SKIN_VISION_BACKEND=langchain`` 时使用；默认走 REST 路径，
    因为 REST 可以传 ``format`` 做 JSON Schema 语法约束。
    """
    settings = get_settings()
    return build_chat_model(settings.effective_vision_model, temperature, settings.num_predict)


# ---------------------------------------------------------------------------
# Embedding 模型
# ---------------------------------------------------------------------------


class _ChromaEFAdapter(Embeddings):
    """把 Chroma 的 EmbeddingFunction 适配成 LangChain 的 Embeddings 接口。"""

    def __init__(self, fn: Any) -> None:
        self._fn = fn

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float(x) for x in vec] for vec in self._fn(texts)]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class _CloudEmbeddings(Embeddings):
    """同步包装的云端向量化（Chroma/LangChain 检索侧多为同步调用）。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        key = self._settings.resolved_cloud_api_key
        if not key:
            raise EmbeddingError(
                "云端向量化未配置 API Key",
                "请设置 SKIN_CLOUD_API_KEY 或 DASHSCOPE_API_KEY，"
                "或改用 SKIN_EMBEDDING_BACKEND=chroma_default",
            )
        base = self._settings.resolved_cloud_api_base
        model = self._settings.effective_embedding_model
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                f"{base}/embeddings",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": model, "input": texts},
            )
            if resp.status_code >= 400:
                raise EmbeddingError(
                    f"云端向量化失败（HTTP {resp.status_code}）",
                    resp.text[:400],
                )
            items = resp.json().get("data") or []
            items = sorted(items, key=lambda x: int(x.get("index", 0)))
            vectors = [[float(x) for x in item.get("embedding", [])] for item in items]
            if len(vectors) != len(texts):
                raise EmbeddingError(
                    "云端向量化返回数量与输入不一致",
                    f"expected={len(texts)} got={len(vectors)}",
                )
            return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]


def _build_embeddings(settings: Settings) -> Embeddings:
    """按配置构造 embedding 后端。"""
    backend = settings.embedding_backend

    if backend == "cloud":
        logger.info("Embedding 后端 = Cloud / {}", settings.effective_embedding_model)
        return _CloudEmbeddings(settings)

    if backend == "ollama":
        logger.info("Embedding 后端 = Ollama / {}", settings.embedding_model)
        return OllamaEmbeddings(
            model=settings.embedding_model,
            base_url=settings.ollama_api,
            client_kwargs={"timeout": 120.0},
        )

    # 延迟导入：只有真正用到时才需要 chromadb 的额外依赖
    from chromadb.utils import embedding_functions

    if backend == "hf":
        logger.info("Embedding 后端 = sentence-transformers / {}", settings.embedding_hf_model)
        try:
            fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=settings.embedding_hf_model
            )
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(
                '加载本地 sentence-transformers 模型失败，请先安装可选依赖：pip install -e ".[hf]"',
                str(exc),
            ) from exc
        return _ChromaEFAdapter(fn)

    logger.warning(
        "Embedding 后端 = Chroma 内置 ONNX MiniLM：零配置但中文语义效果一般，"
        "不建议用于生产。推荐 SKIN_EMBEDDING_BACKEND=ollama + ollama pull bge-m3"
    )
    return _ChromaEFAdapter(embedding_functions.DefaultEmbeddingFunction())


@lru_cache(maxsize=1)
def get_embeddings() -> Embeddings:
    """获取 embedding 实例单例。"""
    return _build_embeddings(get_settings())
