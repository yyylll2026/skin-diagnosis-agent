"""Chroma 向量库接入层。

几个刻意的技术选择：

1. **PersistentClient + 本地目录**：本地部署场景不需要独立服务，
   sqlite3 + hnswlib 落盘即可，零运维。
2. **hnsw:space = cosine**：皮肤科文档多是短文本片段，余弦距离比 L2
   更稳定；而且余弦距离可直接线性映射到 0~1 的「相关度」，便于前端展示。
3. **单例 + 缓存**：Chroma 客户端内部维护索引文件句柄，反复创建会导致
   句柄泄漏与索引重建。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import chromadb
from langchain_chroma import Chroma
from loguru import logger

from app.config import get_settings
from app.services.llm_factory import get_embeddings


@lru_cache(maxsize=1)
def get_chroma_client() -> chromadb.ClientAPI:
    """获取 Chroma 持久化客户端单例。"""
    settings = get_settings()
    client = chromadb.PersistentClient(path=str(settings.resolved_chroma_dir))
    logger.info("Chroma 持久化目录: {}", settings.resolved_chroma_dir)
    return client


@lru_cache(maxsize=1)
def get_vectorstore() -> Chroma:
    """获取 LangChain 的 Chroma 向量库封装单例。"""
    settings = get_settings()
    return Chroma(
        client=get_chroma_client(),
        collection_name=settings.chroma_collection,
        embedding_function=get_embeddings(),
        # 余弦空间；注意：集合已存在时该参数被忽略，所以必须在首次创建时就定好
        collection_metadata={"hnsw:space": "cosine"},
    )


def get_collection() -> Any:
    """获取底层原生集合对象（用于统计、where 删除等 LangChain 未覆盖的操作）。"""
    settings = get_settings()
    return get_chroma_client().get_or_create_collection(
        name=settings.chroma_collection,
        metadata={"hnsw:space": "cosine"},
    )


def count_chunks() -> int:
    """统计当前集合中的向量块总数。"""
    try:
        return int(get_collection().count())
    except Exception as exc:  # noqa: BLE001 - 统计失败不应影响接口可用性
        logger.warning("统计向量块数量失败: {}", exc)
        return 0


def list_doc_ids() -> list[str]:
    """列出集合中出现过的全部 doc_id（去重）。"""
    try:
        data = get_collection().get(include=["metadatas"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 doc_id 失败: {}", exc)
        return []
    metadatas = data.get("metadatas") or []
    return sorted({str(m.get("doc_id")) for m in metadatas if m and m.get("doc_id")})


def delete_by_doc_id(doc_id: str) -> int:
    """删除某个文档的全部向量块。

    Args:
        doc_id: 文档 ID。

    Returns:
        删除的向量块数量（估算，Chroma 不返回精确值）。
    """
    collection = get_collection()
    before = collection.count()
    collection.delete(where={"doc_id": doc_id})
    after = collection.count()
    return max(before - after, 0)


def reset_collection() -> None:
    """清空整个集合。危险操作，仅供 scripts 里的重建流程调用。"""
    settings = get_settings()
    client = get_chroma_client()
    try:
        client.delete_collection(settings.chroma_collection)
        logger.warning("已删除集合 {}", settings.chroma_collection)
    except Exception as exc:  # noqa: BLE001
        logger.info("删除集合时忽略异常: {}", exc)
    get_vectorstore.cache_clear()
    get_collection()
