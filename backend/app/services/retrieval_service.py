"""混合检索服务：向量召回 + BM25 关键词召回 → RRF 融合 → 可选 LLM 重排。

三种检索策略各自解决什么：

- **向量召回**（Chroma 余弦）解决「语义相似但用词不同」的问题。
  用户说「脸上起红疙瘩」，文档里写「面部丘疹性皮损」，只有向量能匹配上。
- **BM25 关键词召回**解决「专有名词必须精确命中」的问题。
  疾病名、药物名、缩写（如「AML」「ABCDE 法则」）靠语义向量经常召回不准。
- **RRF（Reciprocal Rank Fusion）**解决「两路分数不可比」的问题。
  余弦相似度是 0~1 的有界值，BM25 分数是无界的 TF-IDF 加权和，
  直接加权求和需要凭经验调缩放系数；RRF 只用「排名」不用「分数」，
  天然规避量纲问题，是工业界最省心的融合方式。

关于返回给前端的 ``score``：排序用 RRF，展示用加权混合分。
排序信号和展示信号分开是刻意的——前者要的是鲁棒，后者要的是直观。

中文 BM25 的坑：``rank_bm25`` 默认按空格切词，中文会被整句当成一个 token，
关键词召回直接失效。必须先用 jieba 分词，并注入皮肤科领域词典。
"""

from __future__ import annotations

import threading
from functools import lru_cache
from typing import Any

import jieba
from langchain_core.documents import Document
from loguru import logger

from app.config import Settings, get_settings
from app.core.parsing import extract_json, truncate
from app.core.prompts import RERANK_PROMPT
from app.repositories import vector_store
from app.schemas.knowledge import RetrievedChunk

# jieba 首次加载会打一堆日志，屏蔽掉
jieba.setLogLevel(60)

# 皮肤科领域词典：不加这些词，jieba 会把「银屑病」切成「银屑/病」，
# 「特应性皮炎」切成「特应/性/皮炎」，直接拖垮 BM25 召回质量
DOMAIN_LEXICON: tuple[str, ...] = (
    # 疾病
    "银屑病",
    "湿疹",
    "特应性皮炎",
    "荨麻疹",
    "带状疱疹",
    "白癜风",
    "黄褐斑",
    "玫瑰痤疮",
    "脂溢性皮炎",
    "接触性皮炎",
    "单纯疱疹",
    "毛囊炎",
    "黑素瘤",
    "基底细胞癌",
    "鳞状细胞癌",
    "光化性角化病",
    "红斑狼疮",
    "硬皮病",
    "天疱疮",
    "类天疱疮",
    "药疹",
    "多形红斑",
    "结节性红斑",
    "扁平疣",
    "跖疣",
    "手足癣",
    "体癣",
    "股癣",
    "甲癣",
    "花斑癣",
    "疥疮",
    "虫咬皮炎",
    "痒疹",
    "神经性皮炎",
    "皮肤瘙痒症",
    "鱼鳞病",
    "毛周角化病",
    "痤疮",
    "斑秃",
    "雄激素性脱发",
    "色素痣",
    "皮脂腺囊肿",
    "脂肪瘤",
    "血管瘤",
    "鲜红斑痣",
    "太田痣",
    # 形态与体征
    "丘疹",
    "斑疹",
    "斑块",
    "脓疱",
    "结节",
    "囊肿",
    "风团",
    "水疱",
    "大疱",
    "鳞屑",
    "结痂",
    "糜烂",
    "溃疡",
    "皲裂",
    "苔藓样变",
    "色素沉着",
    "色素减退",
    "毛细血管扩张",
    "抓痕",
    "瘢痕",
    "红斑",
    "浸润",
    "尼氏征",
    "同形反应",
    # 部位
    "面颊",
    "额部",
    "眼睑",
    "口周",
    "颈部",
    "头皮",
    "躯干",
    "四肢",
    "掌跖",
    "指缝",
    "甲周",
    "口腔黏膜",
    "外生殖器",
    "褶皱部位",
    "曝光部位",
    # 诊疗
    "皮肤镜",
    "伍德灯",
    "斑贴试验",
    "皮损",
    "鉴别诊断",
    "糖皮质激素",
    "钙调神经磷酸酶抑制剂",
    "抗组胺药",
    "维A酸",
    "光疗",
    "生物制剂",
)

for _word in DOMAIN_LEXICON:
    jieba.add_word(_word, freq=10000)


def tokenize(text: str) -> list[str]:
    """中文分词，供 BM25 使用。

    Args:
        text: 待分词文本。

    Returns:
        过滤掉空白与单字标点后的词列表。
    """
    return [token.strip() for token in jieba.lcut(text) if token.strip() and len(token.strip()) > 1]


class RetrievalService:
    """混合检索服务。线程安全：BM25 索引重建加锁。"""

    def __init__(self, settings: Settings | None = None) -> None:
        """初始化。

        Args:
            settings: 全局配置。
        """
        self.settings = settings or get_settings()
        self._lock = threading.Lock()
        self._bm25: Any = None
        self._corpus_ids: list[str] = []
        self._corpus_docs: list[str] = []
        self._corpus_meta: list[dict[str, Any]] = []
        self._stamp: int = -1

    # ------------------------------------------------------------------
    # BM25 索引（懒加载 + 自动失效）
    # ------------------------------------------------------------------

    def invalidate(self) -> None:
        """标记索引失效（入库/删除文档后调用）。"""
        with self._lock:
            self._stamp = -1
        logger.debug("BM25 索引已标记失效")

    def _ensure_bm25(self) -> None:
        """确保 BM25 索引与向量库一致。

        用集合的 count 作为版本号：数量变化即重建。
        这比维护显式的版本号更省心，且覆盖了「全量重传」这类场景。
        """
        current = vector_store.count_chunks()
        with self._lock:
            if self._bm25 is not None and current == self._stamp:
                return
            if current == 0:
                self._bm25 = None
                self._stamp = 0
                return

            from rank_bm25 import BM25Okapi

            data = vector_store.get_collection().get(include=["documents", "metadatas"])
            docs = data.get("documents") or []
            metas = data.get("metadatas") or [{}] * len(docs)
            ids = data.get("ids") or [f"chunk-{i}" for i in range(len(docs))]

            corpus = [tokenize(text or "") for text in docs]
            self._bm25 = BM25Okapi(corpus) if corpus else None
            self._corpus_ids = list(ids)
            self._corpus_docs = list(docs)
            self._corpus_meta = list(metas)
            self._stamp = current
            logger.info("BM25 索引重建完成，共 {} 块", len(self._corpus_docs))

    # ------------------------------------------------------------------
    # 单路召回
    # ------------------------------------------------------------------

    def _vector_search(
        self, query: str, limit: int, category: str | None
    ) -> tuple[list[str], dict[str, float], dict[str, Document]]:
        """向量召回。

        Returns:
            (有序的 chunk_id 列表, chunk_id -> 余弦相似度, chunk_id -> Document)
        """
        where = {"category": category} if category else None
        pairs = vector_store.get_vectorstore().similarity_search_with_relevance_scores(
            query, k=limit, filter=where
        )
        order: list[str] = []
        sims: dict[str, float] = {}
        docs: dict[str, Document] = {}
        for doc, score in pairs:
            chunk_id = str(doc.metadata.get("chunk_id") or doc.metadata.get("content_hash") or "")
            if not chunk_id:
                continue
            order.append(chunk_id)
            sims[chunk_id] = float(max(0.0, min(1.0, score)))
            docs[chunk_id] = doc
        return order, sims, docs

    def _bm25_search(
        self, query: str, limit: int, category: str | None
    ) -> tuple[list[str], dict[str, float], dict[str, Document]]:
        """BM25 关键词召回。

        Returns:
            (有序的 chunk_id 列表, chunk_id -> 归一化 BM25 分, chunk_id -> Document)
        """
        self._ensure_bm25()
        if self._bm25 is None:
            return [], {}, {}

        tokens = tokenize(query)
        if not tokens:
            return [], {}, {}

        scores = self._bm25.get_scores(tokens)
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)

        order: list[str] = []
        raw: dict[str, float] = {}
        docs: dict[str, Document] = {}
        for idx in ranked:
            if scores[idx] <= 0:
                break
            meta = self._corpus_meta[idx] or {}
            if category and meta.get("category") != category:
                continue
            chunk_id = str(meta.get("chunk_id") or self._corpus_ids[idx])
            order.append(chunk_id)
            raw[chunk_id] = float(scores[idx])
            docs[chunk_id] = Document(page_content=self._corpus_docs[idx], metadata=meta)
            if len(order) >= limit:
                break

        # 归一化到 0~1，便于与余弦相似度做同尺度加权展示
        peak = max(raw.values()) if raw else 1.0
        normalized = {key: value / peak for key, value in raw.items()}
        return order, normalized, docs

    # ------------------------------------------------------------------
    # 融合
    # ------------------------------------------------------------------

    def _rrf(self, rankings: list[tuple[list[str], float]], k: int) -> dict[str, float]:
        """Reciprocal Rank Fusion。

        公式：``score(d) = Σ_r weight_r / (k + rank_r(d))``，rank 从 1 开始。
        k 取 60（原论文经验值），作用是压平头部差异，避免某一路的 top-1
        因为「恰好排第一」而获得过高权重。

        Args:
            rankings: ``[(有序 chunk_id 列表, 权重), ...]``
            k: RRF 平滑常数。

        Returns:
            chunk_id -> RRF 分。
        """
        fused: dict[str, float] = {}
        for order, weight in rankings:
            for rank, chunk_id in enumerate(order, start=1):
                fused[chunk_id] = fused.get(chunk_id, 0.0) + weight / (k + rank)
        return fused

    def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        category: str | None = None,
        top_n: int | None = None,
    ) -> list[RetrievedChunk]:
        """单查询混合检索。

        Args:
            query: 检索语句。
            top_k: 每路召回的候选数，默认取配置。
            category: 按分类过滤。
            top_n: 最终返回条数，默认取配置的 rerank_top_n。

        Returns:
            按相关度降序的检索结果。
        """
        return self.retrieve_multi([query], top_k=top_k, category=category, top_n=top_n)

    def retrieve_multi(
        self,
        queries: list[str],
        top_k: int | None = None,
        category: str | None = None,
        top_n: int | None = None,
    ) -> list[RetrievedChunk]:
        """多查询混合检索（Multi-Query Retrieval）。

        多查询来自融合层：视觉分析生成的「疾病方向 / 形态特征 / 鉴别处置」
        三条 query 分别召回，再在同一个 RRF 池里融合。这样既扩大了召回面，
        又因为 RRF 只看排名，不会让某条 query 的噪声结果抬高总分。

        查询之间按顺序降权（1.0 / 0.8 / 0.6），体现「第一条最贴近用户真实意图」。

        Args:
            queries: 检索语句列表，建议不超过 4 条。
            top_k: 每路召回候选数。
            category: 按分类过滤。
            top_n: 最终返回条数。

        Returns:
            融合后的检索结果。
        """
        cfg = self.settings
        k_candidates = top_k or cfg.retrieval_top_k
        n_final = top_n or cfg.rerank_top_n

        valid_queries = [q.strip() for q in queries if q and q.strip()]
        if not valid_queries:
            return []
        valid_queries = valid_queries[:4]

        rankings: list[tuple[list[str], float]] = []
        similarities: dict[str, float] = {}
        keyword_scores: dict[str, float] = {}
        doc_map: dict[str, Document] = {}

        for position, query_text in enumerate(valid_queries):
            query_weight = max(0.4, 1.0 - 0.2 * position)

            vec_order, vec_sim, vec_docs = self._vector_search(query_text, k_candidates, category)
            kw_order, kw_score, kw_docs = self._bm25_search(query_text, k_candidates, category)

            rankings.append((vec_order, query_weight * cfg.vector_weight))
            rankings.append((kw_order, query_weight * cfg.bm25_weight))

            for chunk_id, value in vec_sim.items():
                similarities[chunk_id] = max(similarities.get(chunk_id, 0.0), value)
            for chunk_id, value in kw_score.items():
                keyword_scores[chunk_id] = max(keyword_scores.get(chunk_id, 0.0), value)
            doc_map.update(vec_docs)
            doc_map.update(kw_docs)

        if not doc_map:
            return []

        rrf_scores = self._rrf(rankings, cfg.rrf_k)

        # 展示分：加权混合（余弦相似度 + 归一化 BM25），同尺度、直观
        blended: dict[str, float] = {}
        for chunk_id in doc_map:
            sim = similarities.get(chunk_id, 0.0)
            kw = keyword_scores.get(chunk_id, 0.0)
            has_vec = chunk_id in similarities
            has_kw = chunk_id in keyword_scores
            if has_vec and has_kw:
                blended[chunk_id] = cfg.vector_weight * sim + cfg.bm25_weight * kw
            elif has_vec:
                blended[chunk_id] = sim
            else:
                blended[chunk_id] = kw * 0.9  # 纯关键词命中略降权

        # 排序以 RRF 为准（鲁棒），展示以混合分为准（直观）
        ranked_ids = sorted(doc_map, key=lambda cid: rrf_scores.get(cid, 0.0), reverse=True)
        ranked_ids = [cid for cid in ranked_ids if blended.get(cid, 0.0) >= cfg.min_relevance]

        if cfg.rerank_enabled and len(ranked_ids) > 1:
            ranked_ids = self._llm_rerank(valid_queries[0], ranked_ids, doc_map)

        results: list[RetrievedChunk] = []
        for chunk_id in ranked_ids[:n_final]:
            doc = doc_map[chunk_id]
            meta = doc.metadata or {}
            results.append(
                RetrievedChunk(
                    doc_id=str(meta.get("doc_id", "")),
                    title=str(meta.get("title", "")),
                    source=str(meta.get("source", "")),
                    category=str(meta.get("category", "其他")),
                    snippet=truncate(doc.page_content, 300),
                    score=round(blended.get(chunk_id, 0.0), 4),
                    chunk_index=meta.get("chunk_index"),
                    page=meta.get("page"),
                    retriever="rerank" if cfg.rerank_enabled else "hybrid",
                )
            )
        return results

    # ------------------------------------------------------------------
    # LLM 重排（可选）
    # ------------------------------------------------------------------

    def _llm_rerank(
        self, query: str, candidate_ids: list[str], doc_map: dict[str, Document]
    ) -> list[str]:
        """用轻量 LLM 对候选片段做相关性重排。

        交叉编码器级别的精度在本地部署里代价太高，这里退而求其次：
        把候选片段编号后交给小模型打分。**任何失败都回退到原顺序**，
        保证重排只是「加分项」而非「单点故障」。

        Args:
            query: 主查询。
            candidate_ids: 候选 chunk_id（已按 RRF 排序）。
            doc_map: chunk_id -> Document。

        Returns:
            重排后的 chunk_id 列表。
        """
        candidates = candidate_ids[:6]
        if len(candidates) < 2:
            return candidate_ids

        listing = "\n".join(
            f"[{index}] {truncate(doc_map[cid].page_content, 200)}"
            for index, cid in enumerate(candidates)
        )
        prompt = RERANK_PROMPT.format(query=query, candidates=listing)

        try:
            from app.services.llm_factory import get_fast_llm

            raw = get_fast_llm().invoke(prompt).content
            if isinstance(raw, list):  # 某些模型返回 content block 列表
                raw = "".join(str(part) for part in raw)
            items = extract_json(str(raw), expect="array")

            order: list[str] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                index = int(item.get("index", -1))
                if 0 <= index < len(candidates):
                    order.append(candidates[index])

            # 未参与重排的候选（被截断的部分）按原顺序接在后面
            order.extend(cid for cid in candidate_ids if cid not in order)
            logger.debug("LLM 重排完成：{} 个候选", len(order))
            return order
        except Exception as exc:  # noqa: BLE001 - 重排失败必须静默回退
            logger.warning("LLM 重排失败，沿用 RRF 顺序：{}", exc)
            return candidate_ids

    # ------------------------------------------------------------------
    def search(
        self, query: str, top_k: int = 5, category: str | None = None
    ) -> list[RetrievedChunk]:
        """对外暴露的检索入口（供 /kb/search 调试接口使用）。"""
        return self.retrieve_multi([query], top_n=top_k, category=category)


@lru_cache(maxsize=1)
def get_retrieval_service() -> RetrievalService:
    """获取检索服务单例。"""
    return RetrievalService()
