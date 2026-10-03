"""病程趋势聚合服务。

把用户主动填写的健康记录换几个角度看：**时间轴上的程度变化**、
**部位分布**、**程度分布**。刻意放在服务层而不是接口层，因为它是
纯计算（给定记录列表 → 统计结果），可以脱离 HTTP 与数据库单独测试。

## 为什么严重度要转成分数

前端要画折线，需要数值。这里把「未标记 / 轻微 / 中等 / 较明显」映射到
0~3 的分数。**这是自述程度的编码，不是临床评分**——所以：

- 只用于展示趋势的**相对变化**，不做任何医学解释；
- 响应里始终带 ``disclaimer``，前端也必须显示；
- 分数刻度对用户透明（0~3），避免看起来像某种专业量表。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from functools import lru_cache
from typing import Any

from app.config import Settings, get_settings
from app.schemas.health_records import (
    BodySiteStat,
    DailyTrendPoint,
    HealthTrendResponse,
    RecordSeverity,
    SeverityStat,
)

# 自述程度 → 分数。未标记记 0 而不是剔除：它代表「用户没选程度」，
# 让它在折线上体现为低位，比直接丢点更能反映「记录质量」。
SEVERITY_SCORE: dict[str, float] = {
    RecordSeverity.UNKNOWN: 0.0,
    RecordSeverity.MILD: 1.0,
    RecordSeverity.MODERATE: 2.0,
    RecordSeverity.SEVERE: 3.0,
}

# 分数的展示顺序：由重到轻。前端饼图按这个顺序上色，保证颜色稳定。
SEVERITY_ORDER: tuple[RecordSeverity, ...] = (
    RecordSeverity.SEVERE,
    RecordSeverity.MODERATE,
    RecordSeverity.MILD,
    RecordSeverity.UNKNOWN,
)

# 单个窗口最多返回多少天的序列，防止 days 被拉到极端值后返回超大响应。
MAX_SERIES_DAYS = 730


def severity_score(value: str) -> float:
    """把自述程度翻译成 0~3 的分数；未知取值按「未标记」处理。"""
    return SEVERITY_SCORE.get(str(value), 0.0)


def _severity_of_score(score: float) -> RecordSeverity:
    """分数反查程度（用于「当日最明显程度」这类字段）。"""
    best = RecordSeverity.UNKNOWN
    best_score = -1.0
    for severity, value in SEVERITY_SCORE.items():
        if value <= score and value > best_score:
            best, best_score = severity, value
    return best


def _parse_date(value: Any) -> date | None:
    """宽松解析 ``YYYY-MM-DD``；解析不出来就返回 None，不让脏数据打断整次聚合。"""
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


class HealthTrendService:
    """病程趋势聚合。无外部依赖，纯计算。"""

    def __init__(self, settings: Settings | None = None) -> None:
        """初始化。

        Args:
            settings: 全局配置；这里只用于日志/扩展，聚合逻辑不依赖它。
        """
        self.settings = settings or get_settings()

    def summarize(
        self,
        records: list[dict[str, Any]],
        *,
        window_days: int = 90,
        total_records_all: int | None = None,
        today: date | None = None,
    ) -> HealthTrendResponse:
        """把记录列表聚合成趋势结果。

        Args:
            records: 健康记录字典列表（至少含 ``recorded_on`` / ``body_site`` / ``severity``）。
            window_days: 观察窗口天数，从 ``today`` 往前推。
            total_records_all: 不限窗口的历史总数；缺省时用 ``records`` 的长度。
            today: 窗口终点，默认取系统当天。显式传入便于测试。

        Returns:
            可直接序列化给前端的趋势结果。
        """
        window_days = max(1, min(int(window_days), MAX_SERIES_DAYS))
        end = today or date.today()
        start = end - timedelta(days=window_days - 1)

        in_window: list[tuple[date, dict[str, Any]]] = []
        for record in records:
            recorded_on = _parse_date(record.get("recorded_on"))
            if recorded_on is None or recorded_on < start or recorded_on > end:
                continue
            in_window.append((recorded_on, record))

        by_day: dict[date, list[float]] = defaultdict(list)
        by_site: dict[str, list[tuple[date, float]]] = defaultdict(list)
        by_severity: dict[str, int] = defaultdict(int)

        for recorded_on, record in in_window:
            score = severity_score(record.get("severity", ""))
            by_day[recorded_on].append(score)
            site = str(record.get("body_site") or "未填写").strip() or "未填写"
            by_site[site].append((recorded_on, score))
            by_severity[str(record.get("severity") or RecordSeverity.UNKNOWN)] += 1

        total = len(in_window)
        return HealthTrendResponse(
            window_days=window_days,
            range_start=start,
            range_end=end,
            total_records=total,
            total_records_all=total if total_records_all is None else int(total_records_all),
            active_days=len(by_day),
            body_site_count=len(by_site),
            daily=self._daily_series(by_day, end),
            body_sites=self._body_sites(by_site, total),
            severities=self._severities(by_severity, total),
        )

    @staticmethod
    def _daily_series(by_day: dict[date, list[float]], end: date) -> list[DailyTrendPoint]:
        """生成连续的日序列。

        起点取「窗口内最早一条记录」，而不是窗口起点：用户只记了最近三天时，
        前面 87 天的空档没有任何信息量，画出来只是把有效数据挤成一条竖线。
        """
        if not by_day:
            return []

        first = min(by_day)
        points: list[DailyTrendPoint] = []
        cursor = first
        while cursor <= end:
            scores = by_day.get(cursor, [])
            if scores:
                peak = max(scores)
                points.append(
                    DailyTrendPoint(
                        recorded_on=cursor,
                        count=len(scores),
                        max_score=round(peak, 2),
                        avg_score=round(sum(scores) / len(scores), 2),
                        worst_severity=_severity_of_score(peak),
                    )
                )
            else:
                points.append(DailyTrendPoint(recorded_on=cursor, count=0))
            cursor += timedelta(days=1)
        return points

    @staticmethod
    def _body_sites(by_site: dict[str, list[tuple[date, float]]], total: int) -> list[BodySiteStat]:
        """按部位聚合，出现次数多的排前面（次数相同则看最近一次时间）。"""
        stats: list[BodySiteStat] = []
        for site, entries in by_site.items():
            scores = [score for _, score in entries]
            peak = max(scores)
            stats.append(
                BodySiteStat(
                    body_site=site,
                    count=len(entries),
                    last_recorded_on=max(day for day, _ in entries),
                    worst_severity=_severity_of_score(peak),
                    avg_score=round(sum(scores) / len(scores), 2),
                    share=round(len(entries) / total, 4) if total else 0.0,
                )
            )
        stats.sort(key=lambda item: (item.count, item.last_recorded_on), reverse=True)
        return stats

    @staticmethod
    def _severities(by_severity: dict[str, int], total: int) -> list[SeverityStat]:
        """按固定顺序输出程度分布，只保留出现过的档位。"""
        stats: list[SeverityStat] = []
        for severity in SEVERITY_ORDER:
            count = by_severity.get(severity, 0)
            if not count:
                continue
            stats.append(
                SeverityStat(
                    severity=severity,
                    count=count,
                    share=round(count / total, 4) if total else 0.0,
                )
            )
        return stats


@lru_cache(maxsize=1)
def get_health_trend_service() -> HealthTrendService:
    """获取病程趋势服务单例。"""
    return HealthTrendService()
