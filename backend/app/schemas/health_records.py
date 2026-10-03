"""用户主动填写的健康记录契约；不保存原始图片或模型诊断结论。"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class RecordSeverity(StrEnum):
    """用户自述的症状程度，不代表临床分级。"""

    UNKNOWN = "unknown"
    MILD = "mild"
    MODERATE = "moderate"
    SEVERE = "severe"


class HealthRecordFields(BaseModel):
    """一条个人健康记录的可编辑字段。"""

    recorded_on: date = Field(default_factory=date.today, description="记录日期")
    body_site: str = Field(min_length=1, max_length=80, description="身体部位")
    symptoms: str = Field(min_length=1, max_length=500, description="用户自述表现")
    severity: RecordSeverity = Field(default=RecordSeverity.UNKNOWN, description="用户自述程度")
    notes: str = Field(default="", max_length=2000, description="备注")

    @field_validator("body_site", "symptoms")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("该字段不能为空")
        return value

    @field_validator("notes")
    @classmethod
    def _strip_notes(cls, value: str) -> str:
        return value.strip()


class HealthRecordCreate(HealthRecordFields):
    """新增健康记录请求。"""


class HealthRecordUpdate(BaseModel):
    """局部更新健康记录请求。"""

    recorded_on: date | None = None
    body_site: str | None = Field(default=None, min_length=1, max_length=80)
    symptoms: str | None = Field(default=None, min_length=1, max_length=500)
    severity: RecordSeverity | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("body_site", "symptoms")
    @classmethod
    def _strip_optional_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("该字段不能为空")
        return value

    @field_validator("notes")
    @classmethod
    def _strip_optional_notes(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class HealthRecordResponse(HealthRecordFields):
    """健康记录响应。"""

    record_id: str
    created_at: str
    updated_at: str


class HealthRecordExport(BaseModel):
    """个人数据导出包。"""

    exported_at: str
    records: list[HealthRecordResponse] = Field(default_factory=list)
    disclaimer: str = "以下为用户主动记录的健康信息，不构成医疗诊断。"


# ==================== 病程趋势聚合 ====================
#
# 这一组契约只做「把用户自己填的记录换个角度呈现」，不引入任何医学判断：
# 严重度分数是**用户自述程度的编码**，不是临床评分。


class DailyTrendPoint(BaseModel):
    """一天的趋势点。

    没有记录的日子也会出现在序列里（``count=0``、分数为 None），
    这样前端能画出连续的时间轴，而不是把空档压缩掉造成「一直在发作」的错觉。
    """

    recorded_on: date
    count: int = 0
    max_score: float | None = Field(default=None, description="当日最高自述程度分数")
    avg_score: float | None = Field(default=None, description="当日平均自述程度分数")
    worst_severity: RecordSeverity | None = Field(default=None, description="当日最明显程度")


class BodySiteStat(BaseModel):
    """按身体部位聚合的统计。"""

    body_site: str
    count: int
    last_recorded_on: date
    worst_severity: RecordSeverity
    avg_score: float
    share: float = Field(description="占窗口内记录总数的比例，0~1")


class SeverityStat(BaseModel):
    """按自述程度聚合的分布。"""

    severity: RecordSeverity
    count: int
    share: float = Field(description="占窗口内记录总数的比例，0~1")


class HealthTrendResponse(BaseModel):
    """病程趋势聚合结果。"""

    window_days: int
    range_start: date
    range_end: date
    total_records: int = Field(description="窗口内记录数")
    total_records_all: int = Field(description="全部历史记录数，用于提示「还有更早的记录」")
    active_days: int = Field(description="窗口内有记录的天数")
    body_site_count: int = Field(description="窗口内出现过的身体部位数")
    daily: list[DailyTrendPoint] = Field(default_factory=list)
    body_sites: list[BodySiteStat] = Field(default_factory=list)
    severities: list[SeverityStat] = Field(default_factory=list)
    disclaimer: str = "趋势基于你主动填写的记录，程度分数只是自述编码，不构成临床评分或医疗诊断。"
