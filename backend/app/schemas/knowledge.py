"""知识库相关的数据契约。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


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


# ==================== 检索效果评测 ====================
#
# 为什么标注用「文档标识」而不是「chunk_id」：
# 重新分块（改 chunk_size）或换 embedding 重建索引后 chunk_id 会全部变掉，
# 标注如果绑在 chunk 上就废了。绑在文档上，标注集可以长期复用。
#
# 代价是评测粒度停在「文档级召回」——它回答的是「该给模型看的资料有没有被找出来」，
# 这恰好是 RAG 里最要紧的一环；至于「同一文档里哪一段排前面」，
# 由 MRR 和逐条明细来体现。


class EvalCase(BaseModel):
    """一条标注样本：一个问题 + 期望被召回的文档。"""

    query: str = Field(min_length=1, max_length=500, description="检索语句")
    relevant: list[str] = Field(
        min_length=1,
        description="期望命中的文档标识（doc_id / 标题 / 原始文件名，三者任一匹配即算命中）",
    )
    category: str | None = Field(default=None, description="该样本的归类，用于分组看指标")
    note: str = Field(default="", max_length=300, description="标注备注，例如为什么这条算相关")

    @field_validator("relevant")
    @classmethod
    def _labels_must_be_meaningful(cls, value: list[str]) -> list[str]:
        """标签至少 2 个字符。

        单字标签（如「病」）会让「子串包含」判定几乎全命中，指标虚高到没有意义，
        这种标注错误必须在入口就拦下，而不是等跑出一份漂亮但错误的报告。
        """
        cleaned = [item.strip() for item in value if item and item.strip()]
        if not cleaned:
            raise ValueError("relevant 不能为空")
        for item in cleaned:
            if len(item) < 2:
                raise ValueError(f"文档标识「{item}」太短，至少需要 2 个字符")
        return cleaned


class EvalRequest(BaseModel):
    """一次检索评测请求。"""

    cases: list[EvalCase] = Field(min_length=1, max_length=200, description="标注样本，上限 200 条")
    top_k: int = Field(default=5, ge=1, le=20, description="取前 K 条结果参与指标计算")
    category: str | None = Field(default=None, description="限定在某个分类内检索，None 表示全库")
    use_rerank: bool | None = Field(
        default=None, description="覆盖重排开关；None 表示跟随服务端配置"
    )


class EvalCaseResult(BaseModel):
    """单条样本的评测明细，用于定位「哪一类问题检索不到」。"""

    query: str
    relevant: list[str]
    category: str = Field(default="", description="样本归类，空串表示未分类")
    hit: bool = Field(description="前 K 条里是否出现了任意一个期望文档")
    first_hit_rank: int | None = Field(default=None, description="首个命中的名次，从 1 开始")
    matched: list[str] = Field(default_factory=list, description="实际命中的标注项")
    missed: list[str] = Field(default_factory=list, description="未被召回的标注项")
    retrieved: list[str] = Field(default_factory=list, description="实际召回的唯一文档标识（有序）")
    recall: float = 0.0
    precision: float = 0.0
    reciprocal_rank: float = 0.0
    elapsed_ms: int = 0
    note: str = ""


class EvalMetrics(BaseModel):
    """一组样本的汇总指标。"""

    case_count: int
    k: int
    hit_rate: float = Field(description="命中率：至少召回一个期望文档的样本占比")
    mrr: float = Field(description="平均倒数排名：首个正确结果名次的倒数均值")
    recall_at_k: float = Field(description="召回率：期望文档被找出来的平均比例")
    precision_at_k: float = Field(description="准确率：召回结果中相关文档的平均占比")


class EvalCategoryMetrics(EvalMetrics):
    """按归类分组的指标。"""

    category: str


class EvalResponse(BaseModel):
    """检索评测结果。"""

    metrics: EvalMetrics
    by_category: list[EvalCategoryMetrics] = Field(default_factory=list)
    cases: list[EvalCaseResult] = Field(default_factory=list)
    top_k: int
    category: str | None = None
    use_rerank: bool = False
    elapsed_ms: int = 0
    notes: str = ""


class EvalDataset(BaseModel):
    """一套可直接编辑的标注集（用于给前端预填模板）。"""

    name: str
    description: str = ""
    cases: list[EvalCase] = Field(default_factory=list)
