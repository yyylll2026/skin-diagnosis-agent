"""信息完备性判断与追问澄清的单元测试。

与 ``test_safety.py`` 同样的定位：追问的**决策逻辑是确定性的规则**，
所以可以被精确断言，不需要 mock 任何模型。

这一组用例锁住的核心承诺：
- 急症永不因追问延误（``test_red_flag_never_delays_answer``）；
- 知识提问不被追问（``test_knowledge_question_is_not_clarified``）；
- 追问有轮次上限，用尽即兜底作答（``test_round_limit_falls_back_to_answer``）；
- 用户说「直接回答」就不再追问（``test_user_skip_falls_back_to_answer``）；
- 多轮进度真的跨请求累积（``test_collected_dimensions_accumulate_across_turns``）。
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.repositories.db import MetadataStore
from app.services.clarify_service import (
    CORE_DIMENSIONS,
    DIMENSION_META,
    ClarifyService,
)

SESSION = "s-clarify-test"


@pytest.fixture
def service(settings: Settings) -> ClarifyService:
    """一套指向临时目录的隔离服务，不污染开发库。"""
    return ClarifyService(settings, MetadataStore(settings.resolved_sqlite_path))


# ---------------------------------------------------------------------------
# 维度抽取
# ---------------------------------------------------------------------------


def test_extract_duration_supports_chinese_numerals() -> None:
    """「三天」和「3 天」都要能抽到——口语里中文数词远比阿拉伯数字常见。"""
    assert "duration" in ClarifyService.extract("大概三天了")
    assert "duration" in ClarifyService.extract("已经 3 天")
    assert "duration" in ClarifyService.extract("两个月了")


def test_extract_location_sensation_progression() -> None:
    """部位、感觉、变化趋势三条核心维度各自能抽到。"""
    assert ClarifyService.extract("脸上起了红疹")["location"] == "脸"
    assert ClarifyService.extract("特别痒")["sensation"] == "痒"
    assert ClarifyService.extract("还在往外扩大")["progression"] == "扩大"


def test_extract_ignores_vague_words() -> None:
    """「最近」「皮肤」「身上」这类词信息量太低，不该算作维度。"""
    found = ClarifyService.extract("最近皮肤上有点不舒服")
    assert "duration" not in found, "「最近」只表达不久之前，不足以算作时长"
    assert "location" not in found, "「皮肤/身上」太宽泛，等于没说部位"


# ---------------------------------------------------------------------------
# 核心分支：信息足够 → 直接作答
# ---------------------------------------------------------------------------


def test_complete_info_needs_no_clarification(service: ClarifyService) -> None:
    """四条核心维度齐了就不追问。"""
    decision = service.evaluate(SESSION, "脸上起了红疹三天了很痒还在扩大")
    assert decision.needs_clarification is False
    assert decision.bypassed_reason == ""
    assert "三天" in decision.facts_text


def test_three_of_four_dimensions_is_enough(service: ClarifyService) -> None:
    """阈值是 3：缺 1 项就作答，不为凑满而反复追问。"""
    decision = service.evaluate(SESSION, "脸上起了红疹三天了很痒")
    assert decision.needs_clarification is False


# ---------------------------------------------------------------------------
# 核心分支：信息不足 → 追问
# ---------------------------------------------------------------------------


def test_vague_consultation_triggers_clarification(service: ClarifyService) -> None:
    """只说了部位，缺 3 项 → 追问。"""
    decision = service.evaluate(SESSION, "我脸上起了红疹")
    assert decision.needs_clarification is True
    assert decision.round == 1
    assert decision.max_rounds == service.settings.clarify_max_rounds
    # 缺的是「部位」之外的三项
    assert set(decision.missing) == set(CORE_DIMENSIONS) - {"location"}


def test_clarification_is_persisted(service: ClarifyService) -> None:
    """追问进度必须落库，否则下一轮无从判断。"""
    service.evaluate(SESSION, "我脸上起了红疹")
    assert service.has_pending(SESSION) is True
    record = service.load(SESSION)
    assert record is not None
    assert record["rounds"] == 1
    assert record["collected"].get("location") == "脸"
    assert record["pending_fields"]


# ---------------------------------------------------------------------------
# 四条旁路（按优先级）
# ---------------------------------------------------------------------------


def test_red_flag_never_delays_answer(service: ClarifyService) -> None:
    """急症不追问——追问要花一整轮往返，绝不能延误就医。

    这是本机制最重要的一条约束：安全优先于信息完整。
    """
    decision = service.evaluate(SESSION, "我这颗痣最近迅速增大还出血")
    assert decision.needs_clarification is False
    assert decision.bypassed_reason == "urgent"


def test_red_flag_in_later_turn_also_bypasses(service: ClarifyService) -> None:
    """第一轮信息不足、第二轮补上危险信号时，同样要立刻作答。"""
    first = service.evaluate(SESSION, "我脸上起了红疹")
    assert first.needs_clarification is True

    second = service.evaluate(SESSION, "这几天还开始破溃出血了", clarify_pending=True)
    assert second.needs_clarification is False
    assert second.bypassed_reason == "urgent"
    # 危险信号出现后进度应被清空，避免下一次又被拉回追问
    assert service.has_pending(SESSION) is False


def test_knowledge_question_is_not_clarified(service: ClarifyService) -> None:
    """泛泛的知识提问追问「在哪个部位」是荒谬的。"""
    for question in ["湿疹怎么护理", "防晒需要每天涂吗", "银屑病会传染吗"]:
        decision = service.evaluate(SESSION, question)
        assert decision.needs_clarification is False, question
        assert decision.bypassed_reason == "knowledge_question", question


def test_personal_symptom_question_is_still_clarified(service: ClarifyService) -> None:
    """但「我脸上起红疹怎么护理」是个人咨询，应当追问。"""
    decision = service.evaluate(SESSION, "我脸上起红疹怎么护理")
    assert decision.needs_clarification is True


def test_image_bypasses_clarification(service: ClarifyService) -> None:
    """图片诊断不追问：视觉结果已提供部位/形态/颜色等维度。"""
    decision = service.evaluate(SESSION, "", has_image=True)
    assert decision.needs_clarification is False
    assert decision.bypassed_reason == "image"


def test_user_skip_falls_back_to_answer(service: ClarifyService) -> None:
    """用户说「直接回答」就不再追问，转为带缺口的兜底作答。"""
    service.evaluate(SESSION, "我脸上起了红疹")
    decision = service.evaluate(SESSION, "说不清，直接回答吧", clarify_pending=True)
    assert decision.needs_clarification is False
    assert decision.fallback is True
    assert decision.bypassed_reason == "user_skip"
    assert decision.missing, "兜底作答时仍要记录缺了什么"


def test_disabled_switch_skips_clarification(settings: Settings) -> None:
    """关掉开关后完全不走追问。"""
    off = settings.model_copy(update={"clarify_enabled": False})
    service = ClarifyService(off, MetadataStore(off.resolved_sqlite_path))
    decision = service.evaluate(SESSION, "我脸上起了红疹")
    assert decision.needs_clarification is False
    assert decision.bypassed_reason == "disabled"


# ---------------------------------------------------------------------------
# 轮次上限与兜底
# ---------------------------------------------------------------------------


def test_round_limit_falls_back_to_answer(service: ClarifyService) -> None:
    """追问到上限仍不完备时，必须转为兜底作答，不能无限问下去。

    用户始终不补充信息时，体验最差的结果是「一直被问、始终得不到回答」。
    """
    max_rounds = service.settings.clarify_max_rounds

    for round_no in range(1, max_rounds + 1):
        decision = service.evaluate(SESSION, "我脸上起了红疹", clarify_pending=True)
        assert decision.needs_clarification is True, f"第 {round_no} 轮应继续追问"
        assert decision.round == round_no

    # 第 max_rounds + 1 次：触发兜底
    final = service.evaluate(SESSION, "我脸上起了红疹", clarify_pending=True)
    assert final.needs_clarification is False
    assert final.fallback is True
    assert final.bypassed_reason == "round_limit"
    assert final.missing
    assert service.has_pending(SESSION) is False


def test_fallback_note_instructs_conditional_answer(service: ClarifyService) -> None:
    """兜底时的提示词要明确要求条件化分析，而不是让模型假装知道。"""
    for _ in range(service.settings.clarify_max_rounds + 1):
        decision = service.evaluate(SESSION, "我脸上起了红疹", clarify_pending=True)
    note = service.build_note(decision)
    assert "如果" in note
    assert "出现时间" in note


def test_normal_note_is_neutral(service: ClarifyService) -> None:
    """信息完整时的提示词不该带任何警告口吻。"""
    decision = service.evaluate(SESSION, "脸上起了红疹三天了很痒")
    note = service.build_note(decision)
    assert "基本完整" in note
    assert "如果" not in note


# ---------------------------------------------------------------------------
# 多轮累积（追问澄清的实际价值所在）
# ---------------------------------------------------------------------------


def test_collected_dimensions_accumulate_across_turns(service: ClarifyService) -> None:
    """跨轮累积：第一轮说部位、第二轮补时间和感觉，合起来就够了。

    这条用例锁住的是机制的核心价值——用户不必一次说完，
    系统要能把几轮的碎片信息拼成完整的病例摘要。
    """
    first = service.evaluate(SESSION, "我脸上起了红疹")
    assert first.needs_clarification is True

    second = service.evaluate(SESSION, "三天了，很痒", clarify_pending=True)
    assert second.needs_clarification is False
    assert second.collected["location"] == "脸"
    assert second.collected["duration"] == "三天"
    assert second.collected["sensation"] == "痒"
    # 累积的摘要要能被检索与生成复用
    assert "脸" in second.facts_text and "三天" in second.facts_text
    assert service.has_pending(SESSION) is False


def test_terse_reply_is_not_treated_as_chitchat(service: ClarifyService) -> None:
    """追问后的碎片回复（如「三天了」）没有自述人称、没有症状动词，
    但因为有 pending 标记，仍要按「继续咨询」处理。"""
    service.evaluate(SESSION, "我脸上起了红疹")
    decision = service.evaluate(SESSION, "三天了", clarify_pending=True)
    assert decision.collected["duration"] == "三天"
    assert decision.bypassed_reason != "knowledge_question"


# ---------------------------------------------------------------------------
# 文案生成
# ---------------------------------------------------------------------------


def test_build_question_is_structured(service: ClarifyService) -> None:
    """追问问文案要包含：缺失项标题、提问句、为什么问、轮次进度。"""
    decision = service.evaluate(SESSION, "我脸上起了红疹")
    text = service.build_question(decision.to_dict())

    assert DIMENSION_META["duration"]["label"] in text
    assert DIMENSION_META["duration"]["question"] in text
    assert "为什么要问" in text
    assert f"第 1/{service.settings.clarify_max_rounds} 轮追问" in text
    # 必须给出就医提示，不能因为要补信息就耽误急症
    assert "就诊" in text


def test_build_question_shows_collected_info(service: ClarifyService) -> None:
    """已收集到的信息要回显，让用户知道系统记住了什么、不用重复说。"""
    service.evaluate(SESSION, "我脸上起了红疹")
    decision = service.evaluate(SESSION, "三天了", clarify_pending=True)
    text = service.build_question(decision.to_dict())
    assert "已了解到的信息" in text
    assert "三天" in text


def test_decision_dict_is_json_friendly(service: ClarifyService) -> None:
    """判定结果要能直接进 JSON（SSE 事件与接口响应都靠它）。"""
    import json

    decision = service.evaluate(SESSION, "我脸上起了红疹")
    payload = decision.to_dict()
    json.dumps(payload, ensure_ascii=False)  # 不抛异常即合格
    assert payload["missing_keys"]
    assert payload["missing"] == [
        DIMENSION_META[key]["label"] for key in payload["missing_keys"]
    ]


def test_clear_removes_pending(service: ClarifyService) -> None:
    """清除后不应再被判定为追问进行中。"""
    service.evaluate(SESSION, "我脸上起了红疹")
    assert service.has_pending(SESSION) is True
    service.clear(SESSION)
    assert service.has_pending(SESSION) is False
    assert service.load(SESSION) is None
