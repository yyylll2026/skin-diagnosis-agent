"""安全审查的单元测试。

安全层是**规则驱动**的，所以可以被精确断言——这正是选择规则而非 LLM 的价值。
这一组用例必须永远为绿：任何一条挂掉都意味着系统存在合规风险。
"""

from __future__ import annotations

from app.core.prompts import DISCLAIMER
from app.schemas.diagnosis import RiskLevel, VisionDiagnosis
from app.services.safety_service import SafetyService

service = SafetyService()


def test_low_risk_when_nothing_suspicious() -> None:
    """平淡的问答不应被误报为高风险。"""
    report = service.assess(user_text="冬天皮肤干燥该怎么护理？", answer_text="建议加强保湿。")
    assert report.risk_level == RiskLevel.LOW
    assert report.red_flags == []
    assert report.must_see_doctor is False


def test_vision_red_flags_force_doctor_visit() -> None:
    """模型自述的危险信号必须触发强制就医。"""
    vision = VisionDiagnosis(
        summary="色素痣形态不规则",
        red_flags=["边界不规则", "颜色不均"],
    )
    report = service.assess(vision=vision)
    assert report.must_see_doctor is True
    assert "边界不规则" in report.red_flags
    assert report.risk_level in {RiskLevel.HIGH, RiskLevel.URGENT}


def test_rule_scan_catches_keyword_that_model_plays_down() -> None:
    """用户描述里有危险信号、模型却轻描淡写时，规则必须兜住。

    这是安全层存在的根本理由：不能依赖模型每次都主动上报。
    """
    vision = VisionDiagnosis(summary="", risk_level=RiskLevel.LOW)
    report = service.assess(
        vision=vision,
        user_text="我这颗痣最近几个月迅速增大，还时不时出血",
        answer_text="从照片看没什么大问题，可以先观察一段时间。",
    )
    assert report.must_see_doctor is True
    assert report.risk_level == RiskLevel.URGENT


def test_educational_answer_enumerating_red_flags_does_not_alarm() -> None:
    """回归测试：泛泛的知识提问不应报警。

    踩过的坑：模型的回答本身就在**逐条列举**危险信号（「不对称、边界不规则、
    迅速增大……」），早期实现把这些词当成「当事人的信号」扫描命中，
    于是给一个纯知识性问题打上了「⚠️ 请尽快就医」横幅。
    现在危险信号按两级来源区分：强信号（用户输入 + 视觉结果）才驱动升级，
    答案里提到的只作为自查清单展示。
    """
    answer = (
        "色素痣出现以下变化应尽快就医：近期明显增大、不对称、边界不规则、"
        "颜色不均、直径超过 6 毫米、破溃出血、伴发热。"
    )
    report = service.assess(
        user_text="色素痣出现哪些变化需要尽快就医？",
        answer_text=answer,
    )
    assert report.must_see_doctor is False
    assert report.risk_level == RiskLevel.LOW
    # 信号仍然作为「自查清单」展示给用户，只是不触发升级
    assert report.red_flags


def test_urgent_lexicon_escalates_to_urgent() -> None:
    """命中紧急词库时直接拉到 urgent。"""
    report = service.assess(user_text="孩子身上起了大疱还伴呼吸困难")
    assert report.risk_level == RiskLevel.URGENT


def test_risk_level_is_never_downgraded() -> None:
    """规则只能上调风险等级，绝不能把模型判定的 high 降下来。"""
    vision = VisionDiagnosis(summary="考虑尽快就诊", risk_level=RiskLevel.HIGH)
    report = service.assess(vision=vision, answer_text="看起来没什么问题。")
    assert report.risk_level == RiskLevel.HIGH
    assert report.must_see_doctor is True


def test_red_flags_are_deduplicated() -> None:
    """同一危险信号在多个位置出现时只保留一条。"""
    vision = VisionDiagnosis(red_flags=["皮损破溃", "皮损破溃", "出血"])
    report = service.assess(vision=vision)
    assert len(report.red_flags) == len(set(report.red_flags))


def test_augment_appends_disclaimer() -> None:
    """答案必须带上免责声明。"""
    report = service.assess(user_text="你好")
    text = service.augment("建议保持皮肤清洁。", report)
    assert DISCLAIMER in text


def test_augment_is_idempotent() -> None:
    """重复调用不应叠加多份免责声明（多轮拼接场景会踩这个坑）。"""
    report = service.assess(user_text="你好")
    once = service.augment("建议保湿。", report)
    twice = service.augment(once, report)
    assert once.count(DISCLAIMER) == 1
    assert twice.count(DISCLAIMER) == 1


def test_augment_adds_urgent_banner_when_needed() -> None:
    """需要就医时，答案开头要有醒目提示。"""
    vision = VisionDiagnosis(red_flags=["皮损破溃"])
    report = service.assess(vision=vision)
    text = service.augment("请保持局部清洁。", report)
    assert text.startswith("> ⚠️")
    assert "请尽快就医" in text


def test_non_skin_chitchat_is_low_risk() -> None:
    """闲聊不应被危险信号规则误伤。"""
    report = service.assess(user_text="你好呀", answer_text="你好，有什么可以帮你？")
    assert report.risk_level == RiskLevel.LOW
    assert report.must_see_doctor is False
