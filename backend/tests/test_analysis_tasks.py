"""后台图像任务服务测试：生命周期、结果持久化与重启边界。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from app.core.errors import AnalysisTaskConflictError
from app.repositories.db import MetadataStore
from app.services.analysis_task_service import AnalysisTaskService


class FakeRunner:
    """按固定事件序列驱动任务服务，不调用模型。"""

    def __init__(self, events: list[dict[str, Any]], gate: asyncio.Event | None = None) -> None:
        self.events = events
        self.gate = gate

    async def astream_events(self, _state: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        for event in self.events:
            if self.gate:
                await self.gate.wait()
            await asyncio.sleep(0)
            yield event


def _success_events() -> list[dict[str, Any]]:
    return [
        {"type": "node", "node": "vision_analyze", "label": "图像分析", "status": "done"},
        {"type": "vision", "data": {"summary": "结构化观察"}},
        {"type": "sources", "data": [{"title": "示例资料"}]},
        {
            "type": "done",
            "answer": "仅供参考的回答",
            "safety": {"risk_level": "low"},
            "intent": "image_diagnosis",
            "elapsed_ms": 1234,
        },
    ]


async def _wait_for_service_tasks(service: AnalysisTaskService) -> None:
    tasks = list(service._runtime.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def test_analysis_task_persists_progress_result_without_image_payload(tmp_path: Path) -> None:
    """结果可回读、严格按用户隔离，原始图片不进入数据库字段。"""

    async def scenario() -> None:
        store = MetadataStore(tmp_path / "tasks.db")
        store.ensure_session("s-one", user_id="u-one")
        runner = FakeRunner(_success_events())
        service = AnalysisTaskService(store, runner)
        state = {"session_id": "s-one", "question": "三天了", "image_bytes": "private-image-base64"}

        created = service.create("u-one", "s-one", state)
        assert created.status == "queued"
        await _wait_for_service_tasks(service)

        completed = service.get(created.task_id, "u-one")
        assert completed is not None
        assert completed.status == "succeeded"
        assert completed.progress == 100
        assert completed.result["answer"] == "仅供参考的回答"
        assert completed.result["vision"]["summary"] == "结构化观察"
        assert service.get(created.task_id, "u-two") is None
        stored = store.get_analysis_task(created.task_id, "u-one")
        assert stored is not None
        assert "image_bytes" not in stored
        assert "private-image-base64" not in str(stored)
        assert store.list_messages("s-one")[-1]["content"] == "仅供参考的回答"

    asyncio.run(scenario())


def test_failed_task_can_retry_only_while_input_is_cached(tmp_path: Path) -> None:
    """失败任务可在短时内存保留窗口重试，成功后缓存被清除。"""

    async def scenario() -> None:
        store = MetadataStore(tmp_path / "retry.db")
        store.ensure_session("s-two", user_id="u-two")
        runner = FakeRunner([{"type": "error", "message": "模型暂不可用"}])
        service = AnalysisTaskService(store, runner, retry_cache_minutes=1)
        created = service.create("u-two", "s-two", {"session_id": "s-two", "image_bytes": "img"})
        await _wait_for_service_tasks(service)

        failed = service.get(created.task_id, "u-two")
        assert failed is not None and failed.status == "failed"
        assert failed.retry_available is True

        service.runner = FakeRunner(_success_events())
        retry = service.retry(created.task_id, "u-two")
        assert retry is not None
        assert retry.attempt_count == 2
        await _wait_for_service_tasks(service)
        finished = service.get(created.task_id, "u-two")
        assert finished is not None and finished.status == "succeeded"
        assert finished.retry_available is False

    asyncio.run(scenario())


def test_cancelling_task_stops_runner_and_marks_cancelled(tmp_path: Path) -> None:
    """取消请求会停止运行协程并把状态落为 cancelled。"""

    async def scenario() -> None:
        store = MetadataStore(tmp_path / "cancel.db")
        store.ensure_session("s-three", user_id="u-three")
        gate = asyncio.Event()
        service = AnalysisTaskService(store, FakeRunner(_success_events(), gate=gate))
        created = service.create("u-three", "s-three", {"session_id": "s-three"})
        await asyncio.sleep(0)
        cancelled = await service.cancel(created.task_id, "u-three")
        await _wait_for_service_tasks(service)
        assert cancelled is not None and cancelled.status == "cancelled"
        assert service.get(created.task_id, "u-three").status == "cancelled"

    asyncio.run(scenario())


def test_task_queue_rejects_over_capacity_without_leaking_input(tmp_path: Path) -> None:
    """队列达到并发 + 待处理上限时拒绝新图，避免无限缓存图片。"""

    async def scenario() -> None:
        store = MetadataStore(tmp_path / "capacity.db")
        store.ensure_session("s-capacity", user_id="u-capacity")
        gate = asyncio.Event()
        service = AnalysisTaskService(
            store, FakeRunner(_success_events(), gate=gate), max_concurrency=1, max_pending=0
        )
        service.create("u-capacity", "s-capacity", {"session_id": "s-capacity"})
        await asyncio.sleep(0)
        with pytest.raises(AnalysisTaskConflictError, match="队列已满"):
            service.create(
                "u-capacity",
                "s-capacity",
                {"session_id": "s-capacity", "image_bytes": "rejected-image"},
            )
        assert len(service._runtime) == 1
        first_id = next(iter(service._runtime))
        assert service.get(first_id, "u-capacity").status in {"queued", "running"}
        await service.cancel(first_id, "u-capacity")
        await _wait_for_service_tasks(service)

    asyncio.run(scenario())


def test_service_restart_marks_unfinished_tasks_as_failed(tmp_path: Path) -> None:
    """任务图片不落盘，因此进程重启后未完成任务被标为需重新提交。"""
    store = MetadataStore(tmp_path / "restart.db")
    store.create_analysis_task(
        {
            "task_id": "t-interrupted",
            "user_id": "u-four",
            "session_id": "s-four",
            "status": "running",
            "progress": 42,
            "current_step": "图像分析",
            "created_at": "2026-10-03T10:00:00",
            "updated_at": "2026-10-03T10:00:00",
            "started_at": "2026-10-03T10:00:00",
            "finished_at": "",
        }
    )
    service = AnalysisTaskService(store, FakeRunner([]))
    interrupted = service.get("t-interrupted", "u-four")
    assert interrupted is not None
    assert interrupted.status == "failed"
    assert "重新提交" in interrupted.error
    assert interrupted.retry_available is False
