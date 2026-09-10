"""安全审查服务：确定性规则兜底，不依赖 LLM。

为什么安全层必须用规则而不是让模型自己判断？
模型的安全意识是概率性的——同一张图问十次，可能有八次提示就医、两次轻描淡写。
医疗场景下这种不确定性不可接受。因此本模块用**关键词规则表**做确定性扫描，
与模型自述的 ``red_flags`` **取并集**，再按严重度取最大值。
规则只会把风险等级**上调**，永远不会下调，保证「宁可保守」。

另一个职责是答案后处理：命中危险信号时在开头插入醒目提示，
结尾统一追加免责声明——这两件事必须由代码保证，不能指望模型每次都记得写。
"""

from __future__ import annotations

from app.core.parsing import truncate
from app.core.prompts import DISCLAIMER, RED_FLAG_RULES, URGENT_LEXICON
from app.schemas.diagnosis import RiskLevel, SafetyReport, VisionDiagnosis

_SEVERITY: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.URGENT: 3,
}

URGENT_BANNER = (
    "> ⚠️ **请尽快就医**：本次分析中出现了需要专业评估的危险信号，"
    "请勿自行处理或拖延，建议尽快前往正规医院皮肤科（或急诊）面诊。\n"
)


class SafetyService:
    """安全审查与答案合规化。"""

    @staticmethod
    def _scan(text: str) -> list[str]:
        """扫描文本中命中的危险信号。

        Args:
            text: 待扫描文本（会把用户的描述、模型观察、生成答案拼在一起扫）。

        Returns:
            命中的危险信号说明（已去重，保留原文中的规则描述）。
        """
        hits: list[str] = []
        for keyword, description in RED_FLAG_RULES.items():
            if keyword in text and description not in hits:
                hits.append(description)
        return hits

    @staticmethod
    def _escalate(text: str, base: RiskLevel) -> RiskLevel:
        """根据紧急词库上调风险等级。"""
        if any(keyword in text for keyword in URGENT_LEXICON):
            return RiskLevel.URGENT
        return base

    def assess(
        self,
        vision: VisionDiagnosis | None = None,
        user_text: str = "",
        answer_text: str = "",
    ) -> SafetyReport:
        """综合模型输出与确定性规则，产出安全报告。

        ## 为什么要区分「两级信号源」

        早期实现把所有文本（用户输入 + 视觉结果 + 生成的答案）拼在一起扫描，
        实测会产生明显的**假阳性**：用户只是泛泛地问「色素痣出现哪些变化需要就医」，
        而模型的回答本身就在**逐条列举**危险信号（不对称、边界不规则、迅速增大……），
        这些词被扫描命中，于是系统给一个纯知识性提问打上了「⚠️ 请尽快就医」横幅。

        因此改为两级：

        | 级别 | 信号源 | 作用 |
        |------|--------|------|
        | 强信号 | 用户输入 + 视觉结构化结果 | 驱动风险升级与强制就医横幅——这是「当事人自身」的证据 |
        | 弱信号 | 生成的答案文本 | 仅补充进 ``red_flags`` 供用户自查，**不参与升级** |

        这样既保住了「模型淡化用户危险信号时仍会升级」的安全性（因为危险信号在用户输入里），
        又消除了「回答提到疾病症状就报警」的噪声。

        Args:
            vision: 视觉诊断结果（纯文本问答时为 None）。
            user_text: 用户输入。
            answer_text: 待展示给用户的答案文本。

        Returns:
            归一化后的安全报告。
        """
        # ---- 强信号源：当事人自身的描述与图像证据 ----
        case_parts = [user_text]
        if vision is not None:
            case_parts.extend(
                [
                    vision.summary,
                    vision.image_quality,
                    " ".join(vision.red_flags),
                    " ".join(h.name for h in vision.hypotheses),
                    " ".join(vision.observations.morphology),
                    vision.observations.border or "",
                    vision.urgency,
                ]
            )
        case_text = " ".join(part for part in case_parts if part)
        case_flags = self._scan(case_text)

        # ---- 弱信号源：答案中提及的危险信号（仅展示） ----
        answer_flags = self._scan(answer_text)

        model_flags = list(vision.red_flags) if vision else []
        merged: list[str] = []
        for flag in [*model_flags, *case_flags, *answer_flags]:
            cleaned = truncate(str(flag).strip(), 80, suffix="")
            if cleaned and cleaned not in merged:
                merged.append(cleaned)

        # 风险等级只由强信号上调（单调性：永不下调）
        base_level = vision.risk_level if vision else RiskLevel.LOW
        if case_flags and _SEVERITY[base_level] < _SEVERITY[RiskLevel.HIGH]:
            base_level = RiskLevel.HIGH
        level = self._escalate(case_text, base_level)

        must_see_doctor = bool(case_flags) or _SEVERITY[level] >= _SEVERITY[RiskLevel.HIGH]

        return SafetyReport(
            risk_level=level,
            red_flags=merged[:8],
            must_see_doctor=must_see_doctor,
            disclaimer=DISCLAIMER,
        )

    @staticmethod
    def augment(answer: str, report: SafetyReport) -> str:
        """给答案加上「紧急提示」与「免责声明」。

        幂等：免责声明已存在时不重复追加（多轮拼接场景下很重要）。

        Args:
            answer: 模型生成的原始答案。
            report: 安全报告。

        Returns:
            合规化后的答案。
        """
        text = (answer or "").strip()

        if report.must_see_doctor and "请尽快就医" not in text:
            text = f"{URGENT_BANNER}\n{text}"

        if DISCLAIMER not in text:
            text = f"{text}\n\n---\n\n*{DISCLAIMER}*"

        return text
