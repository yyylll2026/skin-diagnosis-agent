"""检索效果评测测试。

分三层：
- **纯函数层**：``_document_order`` / ``_matches`` 的边界行为；
- **服务层**：喂一个假的检索器，把指标算得死死的——不需要 Chroma、不需要模型；
- **接口层**：通过 ``dependency_overrides`` 把评测服务换成假检索器，
  验证鉴权、参数校验与响应结构。

指标这种东西最容易「看起来对」，所以这里的断言全部用可手算的构造数据。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.schemas.knowledge import EvalCase, RetrievedChunk
from app.services.retrieval_eval_service import (
    RetrievalEvalService,
    _document_order,
    _matches,
)

EVAL_API = "/api/v1/kb/eval"

DOC_A = "doc-a"
DOC_B = "doc-b"
DOC_C = "doc-c"
DOC_NOISE = "doc-noise"


def _chunk(doc_id: str = "", title: str = "", source: str = "") -> RetrievedChunk:
    return RetrievedChunk(doc_id=doc_id, title=title, source=source, snippet="片段", score=0.9)


class FakeRetrieval:
    """按 query 返回预置结果的假检索器，用来把指标算准。"""

    def __init__(self, mapping: dict[str, list[RetrievedChunk]]) -> None:
        self.mapping = mapping
        self.calls: list[dict[str, object]] = []

    def search(self, query, top_k=5, category=None, use_rerank=None):
        self.calls.append({"query": query, "top_k": top_k, "use_rerank": use_rerank})
        return list(self.mapping.get(query, []))[:top_k]


class ExplodingRetrieval:
    """模拟检索链路整体故障。"""

    def search(self, query, top_k=5, category=None, use_rerank=None):
        raise RuntimeError("向量库挂了")


def _service(mapping: dict[str, list[RetrievedChunk]]) -> RetrievalEvalService:
    return RetrievalEvalService(Settings(_env_file=None), FakeRetrieval(mapping))


# ---------------------------------------------------------------------------
# 纯函数层
# ---------------------------------------------------------------------------


def test_document_order_deduplicates_by_first_occurrence() -> None:
    """同文档的多个片段只算一次，名次取最早那次。"""
    ordered = _document_order(
        [
            _chunk(DOC_A),
            _chunk(DOC_A),
            _chunk(DOC_B),
            _chunk(DOC_A),
            _chunk(DOC_C),
        ]
    )
    assert [key for key, _ in ordered] == [DOC_A, DOC_B, DOC_C]


def test_document_order_skips_empty_identifiers() -> None:
    """三个标识全空的命中无法比对，直接跳过而不是塞一个空串进去。"""
    assert _document_order([_chunk()]) == []


def test_document_order_falls_back_to_title_when_doc_id_missing() -> None:
    """没有 doc_id 时用标题当展示标识，保证仍能比对。"""
    ordered = _document_order([_chunk(title="银屑病诊疗指南")])
    assert ordered[0][0] == "银屑病诊疗指南"


def test_matches_accepts_substring_label() -> None:
    """标注写「银屑病」要能匹配到「银屑病诊疗指南.pdf」。"""
    candidates = {"doc-1", "银屑病诊疗指南", "银屑病诊疗指南.pdf"}
    assert _matches("银屑病", candidates)
    assert _matches("银屑病诊疗指南.pdf", candidates)
    assert not _matches("荨麻疹", candidates)


def test_matches_ignores_blank_label() -> None:
    assert not _matches("   ", {"银屑病"})


# ---------------------------------------------------------------------------
# 服务层：指标
# ---------------------------------------------------------------------------


def test_perfect_retrieval_scores_one_across_the_board() -> None:
    """每条都在第一位命中，四个指标应当全为 1。"""
    mapping = {"q1": [_chunk(DOC_A)], "q2": [_chunk(DOC_B)]}
    report = _service(mapping).evaluate(
        [
            EvalCase(query="q1", relevant=[DOC_A]),
            EvalCase(query="q2", relevant=[DOC_B]),
        ],
        top_k=5,
    )

    assert report.metrics.case_count == 2
    assert report.metrics.hit_rate == 1.0
    assert report.metrics.mrr == 1.0
    assert report.metrics.recall_at_k == 1.0
    assert report.metrics.precision_at_k == 1.0


def test_late_hit_and_miss_are_reflected_in_mrr() -> None:
    """排第二命中贡献 1/2，完全没召回贡献 0。"""
    mapping = {"q1": [_chunk(DOC_NOISE), _chunk(DOC_A)], "q2": [_chunk("doc-other")]}
    report = _service(mapping).evaluate(
        [
            EvalCase(query="q1", relevant=[DOC_A]),
            EvalCase(query="q2", relevant=[DOC_B]),
        ],
        top_k=5,
    )

    assert report.metrics.hit_rate == 0.5
    assert report.metrics.mrr == 0.25  # (1/2 + 0) / 2
    assert report.cases[0].first_hit_rank == 2
    assert report.cases[0].reciprocal_rank == 0.5
    assert report.cases[1].hit is False
    assert report.cases[1].first_hit_rank is None


def test_recall_and_precision_count_expected_documents() -> None:
    """期望两个只找到一个是 0.5 召回；找到 1 个但返回了 2 条是 0.5 准确。"""
    mapping = {"q1": [_chunk(DOC_A), _chunk(DOC_NOISE)]}
    report = _service(mapping).evaluate(
        [EvalCase(query="q1", relevant=[DOC_A, DOC_B])],
        top_k=5,
    )

    case = report.cases[0]
    assert case.matched == [DOC_A]
    assert case.missed == [DOC_B]
    assert case.recall == 0.5
    assert case.precision == 0.5
    assert case.retrieved == [DOC_A, DOC_NOISE]


def test_precision_is_zero_when_nothing_retrieved() -> None:
    """一条都没召回时准确率必须是 0，不能因为分母是 0 而算成 1。"""
    report = _service({"q1": []}).evaluate([EvalCase(query="q1", relevant=[DOC_A])], top_k=5)

    assert report.cases[0].precision == 0.0
    assert report.cases[0].recall == 0.0
    assert "没有任何召回" in report.notes


def test_duplicate_chunks_do_not_inflate_rank() -> None:
    """同文档命中多个片段时，第二名文档的名次不应被推后。"""
    mapping = {"q1": [_chunk(DOC_NOISE), _chunk(DOC_NOISE), _chunk(DOC_A)]}
    report = _service(mapping).evaluate([EvalCase(query="q1", relevant=[DOC_A])], top_k=5)

    assert report.cases[0].first_hit_rank == 2
    assert report.cases[0].retrieved == [DOC_NOISE, DOC_A]


def test_label_can_match_title_instead_of_doc_id() -> None:
    """标注写文档标题也要能命中，否则用户没法照着文档列表标注。"""
    mapping = {"q1": [_chunk("d-1", title="银屑病诊疗指南", source="银屑病.pdf")]}
    report = _service(mapping).evaluate(
        [EvalCase(query="q1", relevant=["银屑病诊疗指南"])], top_k=5
    )

    assert report.cases[0].hit is True
    assert report.cases[0].matched == ["银屑病诊疗指南"]


def test_retrieval_failure_is_counted_as_miss_not_crash() -> None:
    """检索抛异常时按未命中计入，整轮评测必须跑完。"""
    service = RetrievalEvalService(Settings(_env_file=None), ExplodingRetrieval())
    report = service.evaluate([EvalCase(query="q1", relevant=[DOC_A])], top_k=5)

    assert report.metrics.hit_rate == 0.0
    assert report.cases[0].hit is False


def test_use_rerank_defaults_to_settings_and_can_be_overridden() -> None:
    """重排开关默认跟随配置，显式传入时以传入值为准。"""
    fake = FakeRetrieval({"q1": [_chunk(DOC_A)]})
    service = RetrievalEvalService(Settings(_env_file=None, rerank_enabled=False), fake)

    assert service.evaluate([EvalCase(query="q1", relevant=[DOC_A])]).use_rerank is False
    assert (
        service.evaluate([EvalCase(query="q1", relevant=[DOC_A])], use_rerank=True).use_rerank
        is True
    )
    assert fake.calls[-1]["use_rerank"] is True


def test_by_category_groups_metrics() -> None:
    """分组指标按归类聚合，便于定位「哪一类问题检索不到」。"""
    mapping = {"q1": [_chunk(DOC_A)], "q2": [_chunk(DOC_NOISE)]}
    report = _service(mapping).evaluate(
        [
            EvalCase(query="q1", relevant=[DOC_A], category="皮肤疾病"),
            EvalCase(query="q2", relevant=[DOC_B], category="用药指南"),
            EvalCase(query="q1", relevant=[DOC_A], category="皮肤疾病"),
        ],
        top_k=5,
    )

    grouped = {item.category: item for item in report.by_category}
    assert grouped["皮肤疾病"].hit_rate == 1.0
    assert grouped["皮肤疾病"].case_count == 2
    assert grouped["用药指南"].hit_rate == 0.0


def test_uncategorized_cases_get_a_placeholder_group() -> None:
    """没写归类的样本归到「未分类」，不能从分组里消失。"""
    report = _service({"q1": [_chunk(DOC_A)]}).evaluate(
        [EvalCase(query="q1", relevant=[DOC_A])], top_k=5
    )
    assert [item.category for item in report.by_category] == ["未分类"]


# ---------------------------------------------------------------------------
# 契约层
# ---------------------------------------------------------------------------


def test_short_label_is_rejected_by_schema() -> None:
    """单字标签会让「子串包含」判定几乎全命中，必须在入口拦下。"""
    with pytest.raises(ValidationError):
        EvalCase(query="q1", relevant=["病"])


def test_blank_label_is_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        EvalCase(query="q1", relevant=["   "])


# ---------------------------------------------------------------------------
# 接口层
# ---------------------------------------------------------------------------


@pytest.fixture
def eval_client(auth_client: TestClient):
    """把评测依赖换成假检索器，避免测试依赖 Chroma 与模型。"""
    from app import dependencies

    fake = FakeRetrieval({"面部红斑伴鳞屑如何鉴别": [_chunk("d-1", title="银屑病诊疗指南")]})
    auth_client.app.dependency_overrides[dependencies.get_retrieval_eval_service] = lambda: (
        RetrievalEvalService(Settings(_env_file=None), fake)
    )
    yield auth_client
    auth_client.app.dependency_overrides.clear()


def _register(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/v1/auth/register", json={"username": username, "password": "passw0rd1"}
    )
    assert response.status_code == 200


def test_starter_dataset_endpoint_returns_editable_template(eval_client: TestClient) -> None:
    """模板接口要给出可直接编辑的样本，并说明 relevant 需要替换。"""
    _register(eval_client, "eval_template")
    response = eval_client.get(f"{EVAL_API}/starter")

    assert response.status_code == 200
    body = response.json()
    assert body["cases"], "模板不能是空的"
    assert "relevant" in body["description"]


def test_eval_run_endpoint_returns_metrics_and_details(eval_client: TestClient) -> None:
    """跑一轮评测，汇总指标与逐条明细都要返回。"""
    _register(eval_client, "eval_runner")
    response = eval_client.post(
        f"{EVAL_API}/run",
        json={
            "cases": [
                {
                    "query": "面部红斑伴鳞屑如何鉴别",
                    "relevant": ["银屑病诊疗指南"],
                    "category": "皮肤疾病",
                },
                {"query": "库里没有的问题", "relevant": ["不存在的文档"]},
            ],
            "top_k": 5,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["top_k"] == 5
    assert body["use_rerank"] is False
    assert body["metrics"]["case_count"] == 2
    assert body["metrics"]["hit_rate"] == 0.5
    assert body["metrics"]["mrr"] == 0.5
    assert {item["category"] for item in body["by_category"]} == {"皮肤疾病", "未分类"}
    assert body["cases"][0]["retrieved"] == ["d-1"]
    assert body["cases"][1]["missed"] == ["不存在的文档"]


def test_eval_run_endpoint_rejects_short_label(eval_client: TestClient) -> None:
    """单字标注在接口层就要被挡掉。"""
    _register(eval_client, "eval_validation")
    response = eval_client.post(
        f"{EVAL_API}/run", json={"cases": [{"query": "q", "relevant": ["病"]}]}
    )
    assert response.status_code == 400


def test_eval_endpoints_require_login(auth_client: TestClient) -> None:
    """未登录不能读模板也不能跑评测。"""
    assert auth_client.get(f"{EVAL_API}/starter").status_code == 401
    assert (
        auth_client.post(
            f"{EVAL_API}/run", json={"cases": [{"query": "q", "relevant": ["aa"]}]}
        ).status_code
        == 401
    )
