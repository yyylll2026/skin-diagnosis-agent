"""知识库相关的数据契约。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class DocCategory(StrEnum):
    """文档分类，用于检索时做元数据过滤。"""

    DISEASE = "皮肤疾病"
    MEDICATION = "用药指南"
    CARE = "护理建议"
    OTHER = "其他"


class DocumentInfo(BaseModel):
    """已入库文档的登记信息。"""

    doc_id: str
    title: str
    source: str = Field(description="原始文件名")
    category: str = DocCategory.OTHER.value
    chunk_count: int = 0
    size_bytes: int = 0
    created_at: str = ""


class IngestResult(BaseModel):
    """单次入库的结果。"""

    doc_id: str
    title: str
    source: str
    category: str
    chunk_count: int = 0
    elapsed_ms: int = 0
    message: str = ""


class SearchRequest(BaseModel):
    """知识库检索调试请求。"""

    query: str = Field(min_length=1, max_length=500, description="检索语句")
    top_k: int = Field(default=5, ge=1, le=20)
    category: str | None = Field(default=None, description="按分类过滤，None 表示全库")


class RetrievedChunk(BaseModel):
    """一条检索命中。字段 design 与前端 ``SourceList.vue`` 对齐。"""

    doc_id: str = ""
    title: str = ""
    source: str = ""
    category: str = DocCategory.OTHER.value
    snippet: str = Field(default="", description="命中片段原文（已截断）")
    score: float = Field(default=0.0, description="归一化相关度，0~1，越大越相关")
    chunk_index: int | None = None
    page: int | None = None
    retriever: str = Field(
        default="hybrid", description="命中来源：vector / bm25 / hybrid / rerank"
    )


class SearchResponse(BaseModel):
    """检索调试响应。"""

    query: str
    count: int
    elapsed_ms: int
    results: list[RetrievedChunk] = Field(default_factory=list)
