"""检索效果评测服务。

## 这个模块解决什么问题

在此之前，检索调参（分块大小、向量/BM25 权重、min_relevance、重排开关）
只能靠「随手搜一条看看感觉对不对」——**没有数字，就没有回归**：
改了参数之后到底是变好还是变坏，谁也说不清。

这里把「标注集 → 跑检索 → 算指标」这条链路固化下来：

| 指标 | 回答的问题 | 什么时候会掉 |
|---|---|---|
| Hit@K | 该给模型的资料**有没有被找出来** | 召回不足、分块过大 |
| MRR | 正确结果**排得够不够前** | 融合权重失衡、噪声片段挤占头部 |
| Recall@K | 一条问题的多个答案**找全了没有** | top_k 太小、分类过滤写错 |
| Precision@K | 找出来的东西**有多少是没用的** | min_relevance 太低、分块过碎 |

## 关键设计

- **标注绑文档，不绑 chunk**。换 embedding 或改 chunk_size 后 chunk_id 全变，
  标注绑 chunk 就废了；绑文档可以长期复用（见 ``schemas/knowledge.py`` 的说明）。
- **指标只依赖检索，不调 LLM 生成**。一次评测几十条样本也就几秒，
  可以频繁跑；真接上生成环节，一轮就是十几分钟，没人会天天跑。
- **逐条明细一并返回**。只有总分的话，指标掉了也不知道从哪条开始查；
  带上「期望 vs 实际召回」才能直接定位到具体问题。
"""

from __future__ import annotations

import time
from collections import defaultdict
from functools import lru_cache

from loguru import logger

from app.config import Settings, get_settings
from app.schemas.knowledge import (
    EvalCase,
    EvalCaseResult,
    EvalCategoryMetrics,
    EvalDataset,
    EvalMetrics,
    EvalResponse,
    RetrievedChunk,
)
from app.services.retrieval_service import RetrievalService, get_retrieval_service

UNCATEGORIZED = "未分类"

# 内置标注模板。
#
# 故意不叫「数据集」而叫「模板」：``relevant`` 里写的是**示例文档标题**，
# 用户必须换成自己知识库里真实的标题或文件名才有意义。直接跑会得到一片 0，
# 这是正确行为——说明标注还没落到真实语料上，而不是评测坏了。
STARTER_DATASET = EvalDataset(
    name="starter",
    description=(
        "皮肤科检索标注模板。请把每条 relevant 换成你知识库里真实的文档标题或文件名"
        "（可在知识库面板的文档列表里复制），否则所有指标都会是 0。"
    ),
    cases=[
        EvalCase(
            query="面部红斑伴鳞屑如何鉴别",
            relevant=["银屑病诊疗指南"],
            category="皮肤疾病",
            note="示例：把「银屑病诊疗指南」换成你库里实际文档的标题",
        ),
        EvalCase(
            query="特应性皮炎的一线外用药有哪些",
            relevant=["特应性皮炎外用治疗"],
            category="用药指南",
        ),
        EvalCase(
            query="荨麻疹反复发作超过六周怎么处理",
            relevant=["慢性荨麻疹诊疗"],
            category="皮肤疾病",
        ),
        EvalCase(
            query="带状疱疹后神经痛怎么护理",
            relevant=["带状疱疹护理常规"],
            category="护理建议",
        ),
        EvalCase(
            query="儿童湿疹日常保湿要注意什么",
            relevant=["儿童湿疹日常护理"],
            category="护理建议",
        ),
        EvalCase(
            query="皮肤镜诊断黑素瘤的 ABCDE 法则",
            relevant=["皮肤镜诊断规范"],
            category="皮肤疾病",
        ),
        EvalCase(
            query="糖皮质激素长期外用的不良反应",
            relevant=["糖皮质激素外用指南"],
            category="用药指南",
        ),
        EvalCase(
            query="手足癣和湿疹怎么区分",
            relevant=["手足癣诊疗指南"],
            category="皮肤疾病",
        ),
    ],
)


def _document_order(chunks: list[RetrievedChunk]) -> list[tuple[str, set[str]]]:
    """把命中片段折叠成「按名次排列的唯一文档」。

    同一条文档会命中多个片段，评测关心的是「这条文档有没有被找出来」，
    所以按首次出现的名次去重——名次取最早的那次，否则 MRR 会被同文档的
    后续片段稀释掉。

    Returns:
        ``[(展示用文档标识, 该文档的全部可比对标识集合), ...]``，按名次升序。
    """
    ordered: list[tuple[str, set[str]]] = []
    seen: set[str] = set()
    for chunk in chunks:
        key = (chunk.doc_id or chunk.title or chunk.source or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        candidates = {
            value.strip().lower()
            for value in (chunk.doc_id, chunk.title, chunk.source)
            if value and value.strip()
        }
        ordered.append((key, candidates))
    return ordered


def _matches(label: str, candidates: set[str]) -> bool:
    """标注项是否命中某个文档。

    用「标注是文档标识的子串」判定，而不是严格相等：用户很自然会写
    「银屑病」去匹配「银屑病诊疗指南.pdf」。反向（文档标识是标注的子串）
    不做——那会让短的 doc_id 意外命中长标注。
    """
    needle = label.strip().lower()
    if not needle:
        return False
    return any(needle in candidate for candidate in candidates)


class RetrievalEvalService:
    """在固定标注集上量化检索效果。"""

    def __init__(
        self, settings: Settings | None = None, retrieval: RetrievalService | None = None
    ) -> None:
        """初始化。

        Args:
            settings: 全局配置，用于取默认的重排开关。
            retrieval: 检索服务。必须与线上共用同一个实例，否则评测跑的是
                另一份 BM25 缓存，指标和真实链路对不上。
        """
        self.settings = settings or get_settings()
        self.retrieval = retrieval or get_retrieval_service()

    @staticmethod
    def starter_dataset() -> EvalDataset:
        """返回可直接编辑的标注模板。"""
        return STARTER_DATASET

    def evaluate(
        self,
        cases: list[EvalCase],
        *,
        top_k: int = 5,
        category: str | None = None,
        use_rerank: bool | None = None,
    ) -> EvalResponse:
        """跑一轮评测。

        Args:
            cases: 标注样本。
            top_k: 取前 K 条结果参与计算。
            category: 限定检索分类，None 表示全库。
            use_rerank: 覆盖重排开关；None 表示跟随配置。

        Returns:
            汇总指标 + 分组指标 + 逐条明细。
        """
        resolved_rerank = self.settings.rerank_enabled if use_rerank is None else use_rerank
        started = time.perf_counter()

        results = [
            self._run_case(case, top_k=top_k, category=category, use_rerank=resolved_rerank)
            for case in cases
        ]

        notes = ""
        if results and not any(result.retrieved for result in results):
            notes = (
                "本轮没有任何召回：知识库可能是空的，或者标注里的文档标识"
                "与库中实际标题/文件名对不上。"
            )

        return EvalResponse(
            metrics=self._aggregate(results, top_k),
            by_category=self._by_category(cases, results, top_k),
            cases=results,
            top_k=top_k,
            category=category,
            use_rerank=resolved_rerank,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            notes=notes,
        )

    def _run_case(
        self, case: EvalCase, *, top_k: int, category: str | None, use_rerank: bool
    ) -> EvalCaseResult:
        """跑一条样本。**检索异常按「未命中」处理**，不让一条坏样本废掉整轮评测。"""
        started = time.perf_counter()
        try:
            chunks = self.retrieval.search(
                case.query, top_k=top_k, category=category, use_rerank=use_rerank
            )
        except Exception as exc:  # noqa: BLE001 - 评测必须跑完整轮
            logger.warning("评测样本检索失败，按未命中计入：{} / {}", case.query, exc)
            chunks = []

        ordered = _document_order(chunks)

        matched: list[str] = []
        first_rank: int | None = None
        hit_ranks: set[int] = set()
        for label in case.relevant:
            for rank, (_, candidates) in enumerate(ordered, start=1):
                if not _matches(label, candidates):
                    continue
                matched.append(label)
                hit_ranks.add(rank)
                if first_rank is None or rank < first_rank:
                    first_rank = rank
                break

        missed = [label for label in case.relevant if label not in matched]
        return EvalCaseResult(
            query=case.query,
            relevant=list(case.relevant),
            category=(case.category or "").strip(),
            hit=first_rank is not None,
            first_hit_rank=first_rank,
            matched=matched,
            missed=missed,
            retrieved=[key for key, _ in ordered],
            recall=round(len(matched) / len(case.relevant), 4),
            precision=round(len(hit_ranks) / len(ordered), 4) if ordered else 0.0,
            reciprocal_rank=round(1.0 / first_rank, 4) if first_rank else 0.0,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            note=case.note,
        )

    @staticmethod
    def _aggregate(results: list[EvalCaseResult], k: int) -> EvalMetrics:
        """把逐条结果汇总成一组指标。"""
        count = len(results)
        if not count:
            return EvalMetrics(
                case_count=0, k=k, hit_rate=0.0, mrr=0.0, recall_at_k=0.0, precision_at_k=0.0
            )
        return EvalMetrics(
            case_count=count,
            k=k,
            hit_rate=round(sum(1 for item in results if item.hit) / count, 4),
            mrr=round(sum(item.reciprocal_rank for item in results) / count, 4),
            recall_at_k=round(sum(item.recall for item in results) / count, 4),
            precision_at_k=round(sum(item.precision for item in results) / count, 4),
        )

    @classmethod
    def _by_category(
        cls, cases: list[EvalCase], results: list[EvalCaseResult], k: int
    ) -> list[EvalCategoryMetrics]:
        """按样本归类分组统计，用来发现「哪一类问题检索不到」。"""
        groups: dict[str, list[EvalCaseResult]] = defaultdict(list)
        for case, result in zip(cases, results, strict=True):
            groups[(case.category or "").strip() or UNCATEGORIZED].append(result)

        return [
            EvalCategoryMetrics(category=name, **cls._aggregate(items, k).model_dump())
            for name, items in sorted(groups.items(), key=lambda pair: pair[0])
        ]


@lru_cache(maxsize=1)
def get_retrieval_eval_service() -> RetrievalEvalService:
    """获取评测服务单例（与检索服务共享同一实例）。"""
    return RetrievalEvalService()


__all__: list[str] = [
    "STARTER_DATASET",
    "UNCATEGORIZED",
    "RetrievalEvalService",
    "get_retrieval_eval_service",
]
