"""视觉诊断相关的数据契约。

这些 Pydantic 模型承担两个职责：
1. **约束 LLM 输出**：模型 JSON Schema 会被注入提示词，让 Qwen2.5-VL 按结构吐 JSON；
   返回后再用同一套模型校验，把「自由文本」收敛成「可编程对象」。
2. **约束前后端契约**：FastAPI 的响应模型，前端 ``DiagnosisCard.vue`` 严格按此渲染。

所有字段都给默认值，是因为本地小模型的 JSON 输出经常缺字段——
宁可让缺失字段回落到「未描述」，也不要因为一个字段校验失败丢掉整次诊断。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from app.schemas.knowledge import RetrievedChunk

# 模型经常无视枚举约束，直接吐中文或大写枚举名。这里做一次归一化，
# 让「结构性小瑕疵」不至于让整次诊断报废。
_RISK_ALIASES: dict[str, str] = {
    "low": "low", "低": "low", "低风险": "low", "轻微": "low", "无需处理": "low",
    "medium": "medium", "中": "medium", "中等": "medium", "中度": "medium", "一般": "medium",
    "high": "high", "高": "high", "较高": "high", "高风险": "high", "严重": "high",
    "urgent": "urgent", "紧急": "urgent", "危急": "urgent", "急诊": "urgent", "立即就医": "urgent",
}


class RiskLevel(StrEnum):
    """风险分级。前端按此着色（绿/琥珀/橙/红）。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"

    @classmethod
    def from_any(cls, value: object) -> RiskLevel:
        """把任意写法的风险等级归一化为枚举值。

        Args:
            value: 模型输出的原始值，可能是 ``"高"``、``"HIGH"``、``"高风险"`` 等。

        Returns:
            归一化后的枚举值，无法识别时回落到 MEDIUM（宁可保守）。
        """
        if isinstance(value, RiskLevel):
            return value
        key = str(value or "").strip().lower()
        return cls(_RISK_ALIASES.get(key, "medium"))



class LesionObservation(BaseModel):
    """皮损客观观察。只描述「看得见」的事实，不含推断。"""

    site: str | None = Field(default=None, description="皮损部位，如：面颊、前臂内侧")
    morphology: list[str] = Field(
        default_factory=list, description="形态学描述，如：红斑、丘疹、鳞屑、水疱、结痂"
    )
    color: list[str] = Field(default_factory=list, description="颜色描述，如：淡红、暗褐、瓷白")
    border: str | None = Field(default=None, description="边界特征，如：边界清晰、边界模糊")
    size_estimate: str | None = Field(default=None, description="尺寸估计，如：约 5mm、直径约 2cm")
    distribution: str | None = Field(
        default=None, description="分布特征，如：单发、多发、对称分布、沿神经节段分布"
    )

    @field_validator("morphology", "color", mode="before")
    @classmethod
    def _ensure_list(cls, v: object) -> object:
        """模型有时把列表写成 ``"红斑、丘疹"`` 这样的顿号分隔字符串。"""
        if isinstance(v, str):
            parts = [p.strip() for p in v.replace("，", "、").replace(",", "、").split("、")]
            return [p for p in parts if p]
        return v


class DiagnosisHypothesis(BaseModel):
    """疑似疾病方向。注意：是「方向」而非「确诊」。"""

    name: str = Field(description="疑似疾病或病症名称")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="置信度，0~1")
    rationale: str = Field(default="", description="支持该方向的图像证据")

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalize_confidence(cls, v: object) -> object:
        """模型常把置信度写成百分数（如 75），统一归一到 0~1。"""
        try:
            value = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.5
        if value > 1.0:
            value = value / 100.0
        return max(0.0, min(1.0, value))


class SafetyReport(BaseModel):
    """安全审查结果，由确定性规则生成，不依赖 LLM。"""

    risk_level: RiskLevel = RiskLevel.LOW
    red_flags: list[str] = Field(default_factory=list, description="命中的危险信号")
    must_see_doctor: bool = Field(default=False, description="是否强制建议尽快就医")
    disclaimer: str = Field(default="", description="必须随答案一同展示的免责声明")


class VisionDiagnosis(BaseModel):
    """Qwen2.5-VL 的结构化视觉分析结果。"""

    summary: str = Field(default="", description="一句话视觉印象总述")
    image_quality: str = Field(default="", description="图像质量评价：是否清晰/过暗/过曝/非皮肤")
    observations: LesionObservation = Field(default_factory=LesionObservation)
    hypotheses: list[DiagnosisHypothesis] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    red_flags: list[str] = Field(default_factory=list)
    recommended_department: str = Field(default="皮肤科", description="建议就诊科室")
    urgency: str = Field(default="如症状持续或加重，建议择期就诊", description="就医紧迫度")
    disclaimer: str = Field(default="", description="模型自述的免责声明")

    @field_validator("risk_level", mode="before")
    @classmethod
    def _normalize_risk(cls, v: object) -> RiskLevel:
        """接受中文/大写等任意写法的风险等级。"""
        return RiskLevel.from_any(v)

    @field_validator("hypotheses", mode="before")
    @classmethod
    def _ensure_hypotheses(cls, v: object) -> object:
        """把 hypotheses 的三种「非规范但常见」写法收敛成对象数组。

        模型实际会输出：纯字符串、顿号分隔的字符串、字符串数组、
        单对象。少处理一种就会导致整条 hypotheses 被校验丢弃（置信度信息全丢），
        因此这里逐一覆盖。
        """
        if isinstance(v, str):
            return [
                {"name": item.strip()}
                for item in v.replace("，", "、").split("、")
                if item.strip()
            ]
        if isinstance(v, dict):
            return [v]
        if isinstance(v, list):
            # 列表元素是字符串时补成 {"name": ...}；已是 dict 的原样保留
            return [{"name": item.strip()} if isinstance(item, str) else item for item in v]
        return v

    @field_validator("red_flags", mode="before")
    @classmethod
    def _ensure_flags(cls, v: object) -> object:
        """危险信号同样可能是字符串。"""
        if isinstance(v, str):
            return [item.strip() for item in v.replace("，", "、").split("、") if item.strip()]
        return v


class ImageMeta(BaseModel):
    """图片预处理后的元信息，随诊断一起返回，便于排查「模型看不准」的锅在谁身上。"""

    mime: str = "image/jpeg"
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    resized: bool = False
    warnings: list[str] = Field(default_factory=list, description="如：图像过小、分辨率不足")


class DiagnosisResult(BaseModel):
    """图片诊断接口的完整响应体。"""

    session_id: str
    diagnosis: VisionDiagnosis
    image_meta: ImageMeta
    # 用具体的 RetrievedChunk 而不是 list[dict]：让 OpenAPI 文档与前端
    # SourceList.vue 的字段契约保持一致，也避免「传入模型实例被拒」这类问题
    sources: list[RetrievedChunk] = Field(
        default_factory=list, description="融合检索到的知识库依据"
    )
    answer: str = Field(default="", description="面向用户的自然语言回答")
    safety: SafetyReport = Field(default_factory=SafetyReport)
    elapsed_ms: int = 0
