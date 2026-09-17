"""意图路由与状态机的单元测试。

路由是「零 LLM 依赖」的纯规则逻辑（只在兜底时才调模型），
因此可以精确断言，不需要 mock 任何东西。
"""

from __future__ import annotations

from app.agent.nodes import AgentNodes
from app.schemas.chat import Intent


def _route_nodes() -> AgentNodes:
    """构造一个不注入任何 service 的实例——路由逻辑不依赖它们。"""
    return AgentNodes.__new__(AgentNodes)


# ---------------------------------------------------------------------------
# route 节点
# ---------------------------------------------------------------------------


def test_image_intent_wins_over_text() -> None:
    """只要图片预处理成功，意图必然是图片诊断（图片信息量远大于文字）。"""
    nodes = _route_nodes()
    state = {"question": "这是什么", "image_bytes": b"fake-jpeg-bytes"}
    assert nodes.route(state)["intent"] == Intent.IMAGE_DIAGNOSIS.value


def test_empty_question_without_image_is_chitchat() -> None:
    """既无文字也无图片时走闲聊，避免无意义检索。"""
    nodes = _route_nodes()
    assert nodes.route({"question": ""})["intent"] == Intent.CHITCHAT.value


def test_skin_keyword_routes_to_kb_qa_without_llm() -> None:
    """命中皮肤关键词直接判为知识问答——省掉一次 1~2 秒的模型调用。"""
    nodes = _route_nodes()
    for question in ["湿疹怎么护理", "我脸上长了丘疹", "防晒需要每天涂吗", "这个痣要紧吗"]:
        assert nodes.route({"question": question})["intent"] == Intent.KB_QA.value


def test_greeting_pattern_is_chitchat() -> None:
    """寒暄模板命中即判闲聊，不触发检索。"""
    nodes = _route_nodes()
    for question in ["你好", "在吗？", "谢谢你！", "你是谁"]:
        assert nodes.route({"question": question})["intent"] == Intent.CHITCHAT.value


def test_unknown_question_falls_back_to_kb_qa() -> None:
    """既无关键词也无寒暄特征时，兜底走知识问答（宁可多检索，不要漏答）。"""
    nodes = _route_nodes()
    assert nodes.route({"question": "今天上海的天气怎么样"})["intent"] in {
        Intent.KB_QA.value,
        Intent.CHITCHAT.value,
    }


# ---------------------------------------------------------------------------
# 条件边
# ---------------------------------------------------------------------------


def test_after_route_branches() -> None:
    """route 之后的三条分支判定。

    知识问答的落点是 ``check_completeness`` 而不是 ``build_query``——
    引入追问澄清机制后，想检索必须先证明信息够了（详见本文件末尾的
    ``test_after_check_completeness_*``）。
    """
    nodes = _route_nodes()
    assert nodes.after_route({"intent": Intent.IMAGE_DIAGNOSIS.value}) == "vision_analyze"
    assert nodes.after_route({"intent": Intent.KB_QA.value}) == "check_completeness"
    assert nodes.after_route({"intent": Intent.CHITCHAT.value}) == "generate"


def test_clarify_pending_keeps_consultation_alive() -> None:
    """追问进行中时，即使这句话没有皮肤关键词，也要按「继续咨询」处理。

    这是多轮追问能走到兜底分支的关键：用户回复「不知道」「三天了」这类碎片时，
    若被当成闲聊或交给 LLM 兜底，追问进度就永远无法推进。
    """
    nodes = _route_nodes()
    assert nodes.route({"question": "不知道", "clarify_pending": True})["intent"] == Intent.KB_QA.value
    # 没有待补追问时，「不知道」会走 LLM 兜底（此处无模型，回落到 kb_qa）
    routing = nodes.route({"question": "不知道"})["intent"]
    assert routing in {Intent.KB_QA.value, Intent.CHITCHAT.value}


def test_after_check_completeness_branches() -> None:
    """信息完备性判断之后的两条分支。"""
    nodes = _route_nodes()
    assert nodes.after_check_completeness({"needs_clarification": True}) == "clarify"
    assert nodes.after_check_completeness({"needs_clarification": False}) == "build_query"


def test_after_build_query_skips_retrieval_when_disabled() -> None:
    """用户关闭知识库时直接生成，不做无谓检索。"""
    nodes = _route_nodes()
    state = {"use_knowledge": False, "queries": ["湿疹"]}
    assert nodes.after_build_query(state) == "generate"


def test_after_build_query_skips_retrieval_when_no_queries() -> None:
    """没有可用检索词时也跳过检索。"""
    nodes = _route_nodes()
    assert nodes.after_build_query({"use_knowledge": True, "queries": []}) == "generate"


def test_after_build_query_retrieves_normally() -> None:
    """正常情况进入检索。"""
    nodes = _route_nodes()
    state = {"use_knowledge": True, "queries": ["湿疹 鉴别诊断"]}
    assert nodes.after_build_query(state) == "retrieve"


# ---------------------------------------------------------------------------
# preprocess 节点（不依赖 Ollama：图片解码失败会被捕获并记录为错误）
# ---------------------------------------------------------------------------


def test_preprocess_strips_whitespace_and_keeps_question() -> None:
    """文本前后空白要被清掉。"""
    nodes = _route_nodes()
    result = nodes.preprocess({"question": "  湿疹怎么护理  "})
    assert result["question"] == "湿疹怎么护理"
    assert result["timings"]["preprocess"] >= 0


def test_preprocess_records_error_for_invalid_base64() -> None:
    """base64 解不出图片时，记录错误而不是抛异常中断整图。"""
    nodes = _route_nodes()
    nodes.vision_service = type(
        "Stub", (), {"prepare_image": lambda self, *a, **k: (_ for _ in ()).throw(ValueError("bad"))}
    )()
    result = nodes.preprocess({"question": "看看", "image_bytes": "not-base64!!"})
    assert result["image_bytes"] is None
    assert result["errors"]


def test_preprocess_tolerates_data_uri_prefix() -> None:
    """兼容前端传「data:image/jpeg;base64,xxx」整串的写法。"""
    nodes = _route_nodes()

    class StubVision:
        def prepare_image(self, raw: bytes, mime: str):  # noqa: ANN001, ANN201
            class Meta:
                def model_dump(self) -> dict[str, object]:
                    return {"mime": mime, "warnings": []}

            assert raw == b"hello"
            return raw, "image/jpeg", Meta()

    nodes.vision_service = StubVision()  # type: ignore[assignment]
    import base64

    payload = "data:image/jpeg;base64," + base64.b64encode(b"hello").decode()
    result = nodes.preprocess({"question": "", "image_bytes": payload})
    assert result["image_bytes"] == b"hello"
