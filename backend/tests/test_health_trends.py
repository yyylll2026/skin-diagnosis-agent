"""病程趋势聚合测试。

分两层：
- **纯计算层**：直接喂记录列表给 ``HealthTrendService.summarize``，显式传 ``today``，
  不碰数据库也不依赖系统当天，断言可以写得很死。
- **接口层**：走 ``/health-records/trends``，验证取数窗口与用户隔离。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.schemas.health_records import RecordSeverity
from app.services.health_trend_service import HealthTrendService, severity_score

API = "/api/v1/health-records"


def _record(day: date, site: str, severity: str) -> dict[str, str]:
    """构造一条最小可用的记录字典。"""
    return {"recorded_on": day.isoformat(), "body_site": site, "severity": severity}


# ---------------------------------------------------------------------------
# 纯计算层
# ---------------------------------------------------------------------------


def test_severity_score_maps_levels_and_falls_back_to_unknown() -> None:
    """程度分数字典要覆盖四档，脏值统一按「未标记」处理。"""
    assert severity_score("severe") == 3.0
    assert severity_score("moderate") == 2.0
    assert severity_score("mild") == 1.0
    assert severity_score("unknown") == 0.0
    assert severity_score("这不是一个档位") == 0.0


def test_daily_series_is_continuous_and_fills_gaps() -> None:
    """中间没记录的日子要补成 count=0，否则折线会把空档压缩掉。"""
    today = date(2026, 10, 10)
    records = [
        _record(date(2026, 10, 8), "面颊", "mild"),
        _record(date(2026, 10, 10), "面颊", "severe"),
    ]

    result = HealthTrendService().summarize(records, window_days=30, today=today)

    assert [point.recorded_on for point in result.daily] == [
        date(2026, 10, 8),
        date(2026, 10, 9),
        date(2026, 10, 10),
    ]
    assert result.daily[1].count == 0
    assert result.daily[1].max_score is None
    assert result.daily[-1].max_score == 3.0
    assert result.daily[-1].worst_severity == RecordSeverity.SEVERE
    assert result.active_days == 2


def test_daily_series_starts_at_first_record_not_window_start() -> None:
    """窗口起点不该硬拉满——用户只记了三天时，前面 27 天没有信息量。"""
    today = date(2026, 10, 10)
    records = [_record(date(2026, 10, 9), "颈部", "mild")]

    result = HealthTrendService().summarize(records, window_days=30, today=today)

    assert len(result.daily) == 2
    assert result.daily[0].recorded_on == date(2026, 10, 9)
    assert result.range_start == date(2026, 9, 11)  # 窗口本身仍是 30 天


def test_records_outside_window_are_excluded() -> None:
    """窗口外的记录不参与统计，但历史总数要照实报出来。"""
    today = date(2026, 10, 10)
    records = [_record(date(2026, 1, 1), "面颊", "severe")]

    result = HealthTrendService().summarize(
        records, window_days=30, total_records_all=5, today=today
    )

    assert result.total_records == 0
    assert result.total_records_all == 5
    assert result.daily == []
    assert result.body_sites == []
    assert result.severities == []


def test_body_sites_are_ranked_by_frequency_then_recency() -> None:
    """部位按出现次数排序，次数相同看最近一次记录时间。"""
    today = date(2026, 10, 10)
    records = [
        _record(date(2026, 10, 1), "面颊", "mild"),
        _record(date(2026, 10, 2), "面颊", "moderate"),
        _record(date(2026, 10, 3), "颈部", "severe"),
    ]

    result = HealthTrendService().summarize(records, window_days=30, today=today)

    assert [site.body_site for site in result.body_sites] == ["面颊", "颈部"]
    cheek = result.body_sites[0]
    assert cheek.count == 2
    assert cheek.last_recorded_on == date(2026, 10, 2)
    assert cheek.worst_severity == RecordSeverity.MODERATE
    assert cheek.avg_score == 1.5
    assert cheek.share == pytest.approx(2 / 3, abs=1e-4)
    assert result.body_site_count == 2


def test_severity_distribution_uses_canonical_order() -> None:
    """程度分布按「由重到轻」固定顺序输出，前端配色才稳定。"""
    today = date(2026, 10, 10)
    records = [
        _record(date(2026, 10, 1), "面颊", "mild"),
        _record(date(2026, 10, 2), "面颊", "severe"),
        _record(date(2026, 10, 3), "颈部", "mild"),
    ]

    result = HealthTrendService().summarize(records, window_days=30, today=today)

    assert [item.severity for item in result.severities] == [
        RecordSeverity.SEVERE,
        RecordSeverity.MILD,
    ]
    assert result.severities[0].count == 1
    assert result.severities[1].count == 2
    assert result.severities[1].share == pytest.approx(2 / 3, abs=1e-4)


def test_unparsable_date_does_not_break_aggregation() -> None:
    """脏日期跳过即可，不能让整次聚合失败。"""
    today = date(2026, 10, 10)
    records = [{"recorded_on": "不是日期", "body_site": "面颊", "severity": "mild"}]

    result = HealthTrendService().summarize(records, window_days=30, today=today)

    assert result.total_records == 0


def test_window_days_is_clamped() -> None:
    """窗口天数有上限，避免被传成极端值后返回超大序列。"""
    result = HealthTrendService().summarize([], window_days=10_000)
    assert result.window_days == 730


# ---------------------------------------------------------------------------
# 接口层
# ---------------------------------------------------------------------------


def _register(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/v1/auth/register", json={"username": username, "password": "passw0rd1"}
    )
    assert response.status_code == 200


def _create(client: TestClient, day: date, site: str, severity: str) -> None:
    response = client.post(
        API,
        json={
            "recorded_on": day.isoformat(),
            "body_site": site,
            "symptoms": f"{site}不适",
            "severity": severity,
        },
    )
    assert response.status_code == 200


def test_trends_endpoint_aggregates_current_user_only(auth_client: TestClient) -> None:
    """趋势接口只统计当前账号，且窗口按天数生效。"""
    _register(auth_client, "trend_owner")
    today = date.today()
    _create(auth_client, today, "左侧面颊", "moderate")
    _create(auth_client, today - timedelta(days=2), "左侧面颊", "mild")
    _create(auth_client, today - timedelta(days=400), "颈部", "severe")  # 窗口外

    response = auth_client.get(f"{API}/trends", params={"days": 90})
    assert response.status_code == 200
    body = response.json()

    assert body["window_days"] == 90
    assert body["total_records"] == 2
    assert body["total_records_all"] == 3  # 更早的那条要报出来
    assert body["body_site_count"] == 1
    assert body["daily"][0]["recorded_on"] == (today - timedelta(days=2)).isoformat()
    assert body["daily"][-1]["max_score"] == 2.0
    assert body["severities"][0]["severity"] == "moderate"
    assert "不构成" in body["disclaimer"]

    auth_client.post("/api/v1/auth/logout")
    _register(auth_client, "trend_stranger")
    other = auth_client.get(f"{API}/trends").json()
    assert other["total_records"] == 0
    assert other["daily"] == []


def test_trends_endpoint_rejects_out_of_range_days(auth_client: TestClient) -> None:
    """窗口天数超界应当被参数校验拦下。"""
    _register(auth_client, "trend_bounds")
    assert auth_client.get(f"{API}/trends", params={"days": 1}).status_code == 400
    assert auth_client.get(f"{API}/trends", params={"days": 5000}).status_code == 400


def test_trends_endpoint_requires_login(auth_client: TestClient) -> None:
    """未登录不能读取趋势。"""
    assert auth_client.get(f"{API}/trends").status_code == 401
