"""个人健康记录与时间线接口。

每条记录始终按当前登录用户过滤，管理员不会越权读取其他用户的个人健康记录。
仅保存用户主动输入的文字，不保存上传图片、模型输出或诊断结论。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response

from app.core.errors import ResourceNotFoundError
from app.dependencies import get_current_user, get_health_trend_service, get_metadata_store
from app.repositories.db import MetadataStore
from app.schemas.auth import UserInfo
from app.schemas.health_records import (
    HealthRecordCreate,
    HealthRecordExport,
    HealthRecordResponse,
    HealthRecordUpdate,
    HealthTrendResponse,
)
from app.services.health_trend_service import HealthTrendService

router = APIRouter(prefix="/health-records", tags=["个人健康记录"])


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _response(row: dict[str, Any]) -> HealthRecordResponse:
    return HealthRecordResponse.model_validate(row)


@router.get("", summary="列出我的健康记录", response_model=list[HealthRecordResponse])
async def list_records(
    user: Annotated[UserInfo, Depends(get_current_user)],
    store: Annotated[MetadataStore, Depends(get_metadata_store)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[HealthRecordResponse]:
    """只返回当前账号的数据，按记录日期倒序分页。"""
    return [_response(row) for row in store.list_health_records(user.user_id, limit, offset)]


@router.get("/export", summary="导出我的健康记录", response_model=HealthRecordExport)
async def export_records(
    user: Annotated[UserInfo, Depends(get_current_user)],
    store: Annotated[MetadataStore, Depends(get_metadata_store)],
) -> Response:
    """以 JSON 附件导出当前账号全部健康记录。"""
    rows = store.export_health_records(user.user_id)
    payload = HealthRecordExport(exported_at=_now(), records=[_response(row) for row in rows])
    content = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, indent=2)
    filename = f"skin-health-records-{datetime.now():%Y%m%d}.json"
    return Response(
        content=content,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/trends", summary="病程趋势聚合", response_model=HealthTrendResponse)
async def health_trends(
    user: Annotated[UserInfo, Depends(get_current_user)],
    store: Annotated[MetadataStore, Depends(get_metadata_store)],
    trends: Annotated[HealthTrendService, Depends(get_health_trend_service)],
    days: Annotated[int, Query(ge=7, le=730, description="观察窗口天数")] = 90,
) -> HealthTrendResponse:
    """把当前账号的记录聚合成趋势数据（日序列 / 部位分布 / 程度分布）。

    聚合在服务层完成，这里只负责取数与窗口计算。**只读**，不产生任何副作用。
    返回的分数是用户自述程度的编码，不是临床评分。
    """
    today = date.today()
    start = today - timedelta(days=days - 1)
    records = store.list_health_records_since(user.user_id, start.isoformat())
    return trends.summarize(
        records,
        window_days=days,
        total_records_all=store.count_health_records(user.user_id),
        today=today,
    )


@router.post("", summary="新增健康记录", response_model=HealthRecordResponse)
async def create_record(
    payload: HealthRecordCreate,
    user: Annotated[UserInfo, Depends(get_current_user)],
    store: Annotated[MetadataStore, Depends(get_metadata_store)],
) -> HealthRecordResponse:
    """记录用户自述的情况，不对其作医疗解释。"""
    now = _now()
    row = store.create_health_record(
        {
            "record_id": f"r-{uuid.uuid4().hex[:16]}",
            "user_id": user.user_id,
            "recorded_on": payload.recorded_on.isoformat(),
            "body_site": payload.body_site.strip(),
            "symptoms": payload.symptoms.strip(),
            "severity": payload.severity.value,
            "notes": payload.notes.strip(),
            "created_at": now,
            "updated_at": now,
        }
    )
    return _response(row)


@router.patch("/{record_id}", summary="修改我的健康记录", response_model=HealthRecordResponse)
async def update_record(
    record_id: str,
    payload: HealthRecordUpdate,
    user: Annotated[UserInfo, Depends(get_current_user)],
    store: Annotated[MetadataStore, Depends(get_metadata_store)],
) -> HealthRecordResponse:
    """仅允许修改当前用户拥有的记录。"""
    existing = store.get_health_record(record_id, user.user_id)
    if existing is None:
        raise ResourceNotFoundError("健康记录不存在")

    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "recorded_on" in changes:
        changes["recorded_on"] = changes["recorded_on"].isoformat()
    if "severity" in changes:
        changes["severity"] = changes["severity"].value
    for key in ("body_site", "symptoms", "notes"):
        if key in changes:
            changes[key] = changes[key].strip()

    store.update_health_record(record_id, user.user_id, changes, _now())
    updated = store.get_health_record(record_id, user.user_id)
    if updated is None:
        raise ResourceNotFoundError("健康记录不存在")
    return _response(updated)


@router.delete("/{record_id}", summary="删除我的健康记录")
async def delete_record(
    record_id: str,
    user: Annotated[UserInfo, Depends(get_current_user)],
    store: Annotated[MetadataStore, Depends(get_metadata_store)],
) -> dict[str, Any]:
    """删除当前用户的一条记录；跨用户 ID 同样按不存在处理。"""
    deleted = store.delete_health_record(record_id, user.user_id)
    if not deleted:
        raise ResourceNotFoundError("健康记录不存在")
    return {"deleted": True, "record_id": record_id}
