"""知识库管理接口：文档入库、列表、删除、检索调试、重建索引。

这些接口面向「运维/管理员」角色，前端由 ``KnowledgePanel.vue``（抽屉面板）消费。
与对话接口分开的原因：知识库操作是**低频、耗时、幂等**的写操作，
而对话是**高频、低延迟**的读操作，两者的超时策略、并发模型、错误语义都不同。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from app.config import Settings, get_settings
from app.dependencies import get_ingest_service, get_metadata_store, get_retrieval_service
from app.repositories import vector_store
from app.repositories.db import MetadataStore
from app.schemas.knowledge import (
    DocumentInfo,
    IngestResult,
    SearchRequest,
    SearchResponse,
)
from app.services.ingest_service import SUPPORTED_SUFFIXES, IngestService
from app.services.retrieval_service import RetrievalService

router = APIRouter(prefix="/kb", tags=["知识库"])

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(name: str) -> str:
    """净化文件名，防止路径穿越与非法字符。

    Args:
        name: 原始文件名（可能来自不可信的前端）。

    Returns:
        仅保留文件名部分并过滤非法字符的结果。
    """
    base = Path(name or "document").name
    cleaned = _UNSAFE_CHARS.sub("_", base).strip(" .")
    return cleaned or "document"


@router.post("/documents", summary="上传文档入库", response_model=IngestResult)
async def upload_document(
    file: Annotated[UploadFile, File(description="支持 pdf/docx/md/txt/html")],
    category: Annotated[str, Form(description="文档分类")] = "其他",
    title: Annotated[str | None, Form(description="自定义标题，默认取文件名")] = None,
    settings: Settings = Depends(get_settings),
    ingest: IngestService = Depends(get_ingest_service),
) -> IngestResult:
    """上传并入库一份文档。

    流程：落盘 → 解析 → 分块 → 向量化 → 写入 Chroma → 登记元数据。
    同一份文件（内容 SHA1 相同）重复上传会**覆盖**旧版本，不会产生重复向量。

    Raises:
        HTTPException: 格式不支持（415）或解析失败（422）。
    """
    from app.core.errors import DocumentParseError

    filename = _safe_filename(file.filename or "document")
    if Path(filename).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail=f"不支持的文件格式，当前支持：{', '.join(sorted(SUPPORTED_SUFFIXES))}",
        )

    raw = await file.read()
    # 上传文件统一落在 uploads/，与人工投放到 docs/ 的语料分开管理，
    # 这样删除文档时可以安全地连带清理上传文件，而不会误删人工语料
    target = settings.upload_dir / filename
    target.write_bytes(raw)

    try:
        return await ingest.ingest_file(target, category=category, title=title)
    except DocumentParseError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc


@router.get("/documents", summary="列出已入库文档", response_model=list[DocumentInfo])
async def list_documents(
    store: Annotated[MetadataStore, Depends(get_metadata_store)],
) -> list[DocumentInfo]:
    """列出全部已入库文档，按入库时间倒序。"""
    return store.list_documents()


@router.delete("/documents/{doc_id}", summary="删除文档及其向量")
async def delete_document(
    doc_id: str,
    settings: Settings = Depends(get_settings),
    store: MetadataStore = Depends(get_metadata_store),
    ingest: IngestService = Depends(get_ingest_service),
) -> dict[str, Any]:
    """删除文档：清理向量块 + 元数据登记 + 上传文件。

    人工投放到 ``data/docs/`` 的原始语料**不会被删除**，只从索引中移除，
    避免误删用户手工整理的资料。
    """
    info = store.get_document(doc_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"文档不存在：{doc_id}")

    ingest.delete_document(doc_id)

    upload_path = settings.upload_dir / _safe_filename(info.source)
    removed_file = False
    if upload_path.exists() and upload_path.is_file():
        try:
            upload_path.unlink()
            removed_file = True
        except OSError:
            pass

    return {"deleted": True, "doc_id": doc_id, "file_removed": removed_file}


@router.post("/search", summary="检索调试", response_model=SearchResponse)
async def search_knowledge(
    payload: SearchRequest,
    retrieval: Annotated[RetrievalService, Depends(get_retrieval_service)],
) -> SearchResponse:
    """直接调用混合检索链路，返回命中片段与分数。

    用途：调参时快速验证「分块大小 / 权重 / 阈值」是否合理，
    不必每次都跑完整的 LLM 生成（本地模型生成一次要十几秒）。
    """
    started = time.perf_counter()
    results = retrieval.search(payload.query, top_k=payload.top_k, category=payload.category)
    return SearchResponse(
        query=payload.query,
        count=len(results),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        results=results,
    )


@router.post("/reindex", summary="重建全部索引")
async def reindex(
    settings: Settings = Depends(get_settings),
    ingest: IngestService = Depends(get_ingest_service),
) -> dict[str, Any]:
    """清空向量库并从 ``data/docs/`` 重新入库全部语料。

    典型使用场景：更换 embedding 模型后必须重建——
    因为 Chroma 集合的向量维度在首次创建时就固定了，换模型不重建会直接报错。
    """
    started = time.perf_counter()
    vector_store.reset_collection()
    results = await ingest.ingest_directory(settings.docs_dir, category="其他")
    return {
        "reindexed": len(results),
        "total_chunks": sum(item.chunk_count for item in results),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "details": [item.model_dump() for item in results],
    }


@router.post("/reset", summary="清空向量库（危险操作）")
async def reset_knowledge_base(
    confirm: Annotated[bool, Query(description="必须显式传 true 才执行")] = False,
    store: MetadataStore = Depends(get_metadata_store),
) -> dict[str, Any]:
    """清空向量库与文档登记表。

    要求显式传 ``confirm=true``，避免误触；操作不可撤销。
    """
    if not confirm:
        raise HTTPException(status_code=400, detail="该操作会清空全部知识库，需显式传 confirm=true")

    vector_store.reset_collection()
    for info in store.list_documents():
        store.delete_document(info.doc_id)
    return {"reset": True}
