"""异步图像分析任务的数据契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class AnalysisTaskCreate(BaseModel):
    """创建一项后台图像分析任务。"""

    session_id: str | None = None
    message: str = Field(default="", max_length=2000)
    image_base64: str = Field(min_length=1, description="图片 base64，仅在进程内存中处理")
    image_mime: str = Field(default="image/jpeg", max_length=80)
    use_knowledge: bool = True

    @field_validator("message", mode="before")
    @classmethod
    def _null_message(cls, value: object) -> object:
        return "" if value is None else value

    @field_validator("image_base64")
    @classmethod
    def _strip_image(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("图片不能为空")
        return value


class AnalysisTaskResponse(BaseModel):
    """后台任务状态；不会包含图片或原始请求体。"""

    task_id: str
    session_id: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    progress: int = Field(ge=0, le=100)
    current_step: str = ""
    result: dict[str, Any] | None = None
    error: str = ""
    retry_available: bool = False
    attempt_count: int = 1
    created_at: str
    updated_at: str
    started_at: str = ""
    finished_at: str = ""
