"""进程内执行、SQLite 跟踪的异步图像分析任务服务。

任务状态和结果可在重启后查看，但原始图片只保留在进程内存中；重启时未完成任务明确标记为失败，
要求用户重新上传，不声称能够恢复没有持久化的图像输入。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from app.agent.state import NODE_ORDER
from app.core.errors import AnalysisTaskConflictError
from app.repositories.db import MetadataStore
from app.schemas.analysis_tasks import AnalysisTaskResponse


class AnalysisTaskService:
    """复用既有 AgentRunner 的任务调度与状态持久化。"""

    def __init__(
        self,
        store: MetadataStore,
        runner: Any,
        *,
        max_concurrency: int = 1,
        max_pending: int = 2,
        retry_cache_minutes: int = 15,
    ) -> None:
        self.store = store
        self.runner = runner
        self._max_concurrency = max(1, max_concurrency)
        self._max_live_tasks = self._max_concurrency + max(0, max_pending)
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._retry_ttl = timedelta(minutes=max(1, retry_cache_minutes))
        self._max_cached_inputs = 2
        self._runtime: dict[str, asyncio.Task[None]] = {}
        self._retry_inputs: dict[str, tuple[datetime, dict[str, Any]]] = {}
        self._retry_owners: dict[str, str] = {}
        self._active_info: dict[str, tuple[str, str]] = {}
        self.store.recover_interrupted_analysis_tasks(self._now())

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _prune_retry_inputs(self) -> None:
        cutoff = datetime.now() - self._retry_ttl
        expired = [
            task_id
            for task_id, (expires_at, _) in self._retry_inputs.items()
            if expires_at < cutoff
        ]
        for task_id in expired:
            self._retry_inputs.pop(task_id, None)
            self._retry_owners.pop(task_id, None)

    def _remember_retry_input(self, task_id: str, state: dict[str, Any]) -> None:
        """限期且限量保留失败任务的原始输入，避免图片缓存无限增长。"""
        self._prune_retry_inputs()
        while len(self._retry_inputs) >= self._max_cached_inputs:
            oldest = next(iter(self._retry_inputs))
            self._retry_inputs.pop(oldest, None)
        self._retry_inputs[task_id] = (datetime.now() + self._retry_ttl, state)

    def _public(self, row: dict[str, Any]) -> AnalysisTaskResponse:
        self._prune_retry_inputs()
        payload = dict(row)
        payload["retry_available"] = (
            payload.get("status") in {"failed", "cancelled"}
            and str(payload.get("task_id")) in self._retry_inputs
        )
        return AnalysisTaskResponse.model_validate(payload)

    def ensure_capacity(self) -> None:
        """在写入用户会话消息前检查本进程任务队列是否已满。"""
        active = sum(not task.done() for task in self._runtime.values())
        if active >= self._max_live_tasks:
            raise AnalysisTaskConflictError("图片分析队列已满，请稍后再试")

    def create(self, user_id: str, session_id: str, state: dict[str, Any]) -> AnalysisTaskResponse:
        """登记任务并立即放入当前进程的协程调度器。"""
        self.ensure_capacity()
        now = self._now()
        task_id = f"t-{uuid.uuid4().hex[:16]}"
        row = self.store.create_analysis_task(
            {
                "task_id": task_id,
                "user_id": user_id,
                "session_id": session_id,
                "status": "queued",
                "progress": 0,
                "current_step": "排队中",
                "created_at": now,
                "updated_at": now,
                "started_at": "",
                "finished_at": "",
            }
        )
        self._remember_retry_input(task_id, state)
        self._retry_owners[task_id] = user_id
        self._active_info[task_id] = (user_id, session_id)
        self._spawn(task_id, state)
        return self._public(row)

    def _spawn(self, task_id: str, state: dict[str, Any]) -> None:
        task = asyncio.create_task(self._execute(task_id, state), name=f"analysis-{task_id}")
        self._runtime[task_id] = task
        task.add_done_callback(lambda _: self._runtime.pop(task_id, None))

    def get(self, task_id: str, user_id: str) -> AnalysisTaskResponse | None:
        row = self.store.get_analysis_task(task_id, user_id)
        return self._public(row) if row else None

    def list(self, user_id: str, limit: int = 50, offset: int = 0) -> list[AnalysisTaskResponse]:
        return [self._public(row) for row in self.store.list_analysis_tasks(user_id, limit, offset)]

    def retry(self, task_id: str, user_id: str) -> AnalysisTaskResponse | None:
        """仅在原图仍处于短时内存缓存时重试，不从磁盘恢复图像。"""
        self._prune_retry_inputs()
        row = self.store.get_analysis_task(task_id, user_id)
        if row is None:
            return None  # type: ignore[return-value]
        if row["status"] not in {"failed", "cancelled"}:
            raise AnalysisTaskConflictError("只有失败或已取消的任务可以重试")
        cached = self._retry_inputs.get(task_id)
        if cached is None:
            raise AnalysisTaskConflictError("图片未持久化或重试期限已过，请重新上传图片创建新任务")
        active = sum(not task.done() for task in self._runtime.values())
        if active >= self._max_live_tasks:
            raise AnalysisTaskConflictError("图片分析队列已满，请稍后再试")
        state = cached[1]
        now = self._now()
        self.store.update_analysis_task(
            task_id,
            {
                "status": "queued",
                "progress": 0,
                "current_step": "排队中（重试）",
                "result": None,
                "error": "",
                "attempt_count": int(row.get("attempt_count") or 1) + 1,
                "updated_at": now,
                "started_at": "",
                "finished_at": "",
            },
        )
        self._active_info[task_id] = (user_id, str(row["session_id"]))
        self._spawn(task_id, state)
        result = self.store.get_analysis_task(task_id, user_id)
        return self._public(result or row)

    async def cancel(self, task_id: str, user_id: str) -> AnalysisTaskResponse | None:
        row = self.store.get_analysis_task(task_id, user_id)
        if row is None:
            return None
        if row["status"] not in {"queued", "running"}:
            return self._public(row)
        now = self._now()
        self.store.update_analysis_task(
            task_id,
            {
                "status": "cancelled",
                "current_step": "已取消",
                "error": "",
                "updated_at": now,
                "finished_at": now,
            },
        )
        task = self._runtime.get(task_id)
        if task and not task.done():
            task.cancel()
        cached = self._retry_inputs.get(task_id)
        if cached:
            self._retry_inputs[task_id] = (datetime.now() + self._retry_ttl, cached[1])
        return self.get(task_id, user_id)

    async def purge_session(self, user_id: str, session_id: str) -> None:
        """会话删除时终止相关运行任务并清除进程内图片缓存。"""
        task_ids = [
            task_id
            for task_id, info in self._active_info.items()
            if info[1] == session_id and (not user_id or info[0] == user_id)
        ]
        task_ids.extend(
            task_id
            for task_id, (_, state) in self._retry_inputs.items()
            if task_id not in task_ids
            and state.get("session_id") == session_id
            and (not user_id or self._retry_owners.get(task_id) == user_id)
        )
        await self._purge_inputs(task_ids)

    async def purge_user(self, user_id: str) -> None:
        """账号删除时终止相关运行任务并清除进程内图片缓存。"""
        task_ids = [task_id for task_id, info in self._active_info.items() if info[0] == user_id]
        task_ids.extend(
            task_id for task_id, owner_id in self._retry_owners.items() if owner_id == user_id
        )
        await self._purge_inputs(list(dict.fromkeys(task_ids)))

    async def _purge_inputs(self, task_ids: list[str]) -> None:
        running: list[asyncio.Task[None]] = []
        for task_id in task_ids:
            self._retry_inputs.pop(task_id, None)
            self._retry_owners.pop(task_id, None)
            task = self._runtime.get(task_id)
            if task and not task.done():
                task.cancel()
                running.append(task)
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        for task_id in task_ids:
            self._active_info.pop(task_id, None)

    async def _execute(self, task_id: str, state: dict[str, Any]) -> None:
        """消费既有 AgentRunner 事件，持久化阶段进度与最终结果。"""
        completed_nodes: set[str] = set()
        result: dict[str, Any] = {
            "session_id": str(state.get("session_id") or ""),
            "intent": "image_diagnosis",
            "answer": "",
            "vision": None,
            "sources": [],
            "safety": None,
            "clarify": None,
            "elapsed_ms": 0,
        }
        error = ""
        saw_done = False
        try:
            async with self._semaphore:
                user_id = self._active_info.get(task_id, ("", ""))[0]
                current = self.store.get_analysis_task(task_id, user_id) if user_id else None
                # 状态以取消请求后的数据库值为准；记录已删除时不再执行任务。
                if current is None:
                    # 已删除的任务不应继续产出结果。
                    return
                if current["status"] == "cancelled":
                    return
                now = self._now()
                self.store.update_analysis_task(
                    task_id,
                    {
                        "status": "running",
                        "current_step": "准备分析",
                        "updated_at": now,
                        "started_at": now,
                    },
                )
                async for event in self.runner.astream_events(state):
                    event_type = event.get("type")
                    if event_type == "node":
                        node = str(event.get("node") or "")
                        label = str(event.get("label") or node or "处理中")
                        if event.get("status") == "done":
                            if node:
                                completed_nodes.add(node)
                            progress = min(
                                95, int(len(completed_nodes) * 95 / max(1, len(NODE_ORDER)))
                            )
                            step = f"已完成：{label}"
                        else:
                            progress = min(
                                94, int(len(completed_nodes) * 95 / max(1, len(NODE_ORDER)))
                            )
                            step = label
                        self.store.update_analysis_task(
                            task_id,
                            {"progress": progress, "current_step": step, "updated_at": self._now()},
                        )
                    elif event_type == "vision":
                        result["vision"] = event.get("data")
                    elif event_type == "sources":
                        result["sources"] = event.get("data") or []
                    elif event_type == "clarify":
                        result["clarify"] = event.get("data")
                    elif event_type == "done":
                        saw_done = True
                        result["answer"] = str(event.get("answer") or "")
                        result["safety"] = event.get("safety")
                        result["clarify"] = event.get("clarify") or result["clarify"]
                        result["elapsed_ms"] = int(event.get("elapsed_ms") or 0)
                        result["intent"] = str(event.get("intent") or "image_diagnosis")
                        if event.get("errors"):
                            result["errors"] = event["errors"]
                    elif event_type == "error":
                        error = str(event.get("message") or event.get("detail") or "分析失败")
                        break

            if error:
                raise RuntimeError(error)
            if not saw_done:
                raise RuntimeError("分析未正常完成，请重新上传图片后重试")

            user_id = self._active_info.get(task_id, ("", ""))[0]
            latest = self.store.get_analysis_task(task_id, user_id) if user_id else None
            if latest is None or latest["status"] == "cancelled":
                return
            if result["answer"]:
                self.store.add_message(
                    str(state.get("session_id") or ""), "assistant", result["answer"]
                )
            now = self._now()
            self.store.update_analysis_task(
                task_id,
                {
                    "status": "succeeded",
                    "progress": 100,
                    "current_step": "分析完成",
                    "result": result,
                    "error": "",
                    "updated_at": now,
                    "finished_at": now,
                },
            )
            self._retry_inputs.pop(task_id, None)
            self._retry_owners.pop(task_id, None)
        except asyncio.CancelledError:
            latest = self.store.get_analysis_task(
                task_id, self._active_info.get(task_id, ("", ""))[0]
            )
            if latest and latest["status"] in {"queued", "running"}:
                now = self._now()
                self.store.update_analysis_task(
                    task_id,
                    {
                        "status": "cancelled",
                        "current_step": "已取消",
                        "updated_at": now,
                        "finished_at": now,
                    },
                )
            raise
        except Exception as exc:  # noqa: BLE001 - 任务失败需落库并可查询
            message = str(exc) or "分析任务失败"
            logger.warning("后台图像分析任务失败 task_id={} error={}", task_id, message)
            user_id = self._active_info.get(task_id, ("", ""))[0]
            latest = self.store.get_analysis_task(task_id, user_id) if user_id else None
            if latest and latest["status"] not in {"cancelled", "succeeded"}:
                now = self._now()
                self.store.update_analysis_task(
                    task_id,
                    {
                        "status": "failed",
                        "current_step": "分析失败",
                        "error": message[:1000],
                        "updated_at": now,
                        "finished_at": now,
                    },
                )
                self._remember_retry_input(task_id, state)
                self._retry_owners[task_id] = user_id
        finally:
            self._active_info.pop(task_id, None)
