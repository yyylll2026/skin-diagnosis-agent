"""智能体运行器：把 LangGraph 的事件流翻译成前端可消费的事件流。

## 为什么要这一层

LangGraph 的 ``astream_events`` 吐的是框架内部事件（``on_chain_start``、
``on_chat_model_stream``、``on_chain_end``……），节点名、数据形状都与前端无关。
如果让 API 层直接处理这些，会把框架细节泄漏到接口契约里，
将来换编排框架就得重写前端。

因此这里做一次**协议收敛**，对外只暴露 8 种事件：

| 事件 | 触发时机 | 前端动作 |
|------|---------|---------|
| ``meta`` | 请求受理 / 意图确定 | 初始化消息气泡、显示意图 |
| ``node`` | 每个节点开始/结束 | 更新「正在做什么」进度 |
| ``vision`` | 视觉分析节点完成 | 渲染结构化诊断卡片 |
| ``sources`` | 检索节点完成 | 渲染参考来源列表 |
| ``token`` | 生成节点逐 token | 追加流式文本 |
| ``answer`` | 兜底整段答案 | 覆盖文本（token 丢失时的补偿） |
| ``done`` | 全流程结束 | 收尾、展示耗时与风险等级 |
| ``error`` | 任何阶段异常 | 展示错误态 |

## 一个实现细节：token 归属

``on_chat_model_stream`` 只告诉我们「有模型在流式输出」，不说是哪个节点。
由于本图是**线性执行**的（没有并行分支），用一个 ``in_generate`` 标志位
在 generate 节点开始/结束时翻转即可准确归属，不必解析 run 树。
"""

from __future__ import annotations

import inspect
import time
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import BaseMessage
from loguru import logger

from app.agent.graph import get_graph
from app.agent.state import NODE_LABELS, NODE_ORDER
from app.core.errors import error_payload
from app.schemas.chat import Intent

_NODE_NAMES = set(NODE_LABELS)


def _extract_text(chunk: Any) -> str:
    """从模型的流式 chunk 中提取纯文本（兼容 content block 列表结构）。"""
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return ""


class AgentRunner:
    """智能体运行器。对外提供流式与非流式两种调用方式。"""

    def __init__(self, graph: Any | None = None) -> None:
        """初始化。

        Args:
            graph: 编译后的 LangGraph 对象；为 None 时取全局单例。
        """
        self._graph = graph
        # 惰性探测：astream_events 是否还接受 version 参数
        self._supports_version: bool | None = None

    @property
    def graph(self) -> Any:
        """惰性获取图实例。"""
        if self._graph is None:
            self._graph = get_graph()
        return self._graph

    @staticmethod
    def build_state(
        session_id: str,
        question: str = "",
        image_base64: str | None = None,
        image_mime: str = "image/jpeg",
        use_knowledge: bool = True,
        history: list[BaseMessage] | None = None,
    ) -> dict[str, Any]:
        """构造初始状态。

        Args:
            session_id: 会话 ID。
            question: 用户文本。
            image_base64: 图片 base64（可带 data URI 前缀）。
            image_mime: 图片 MIME。
            use_knowledge: 是否启用知识库检索。
            history: 最近若干轮历史消息。

        Returns:
            初始状态字典。
        """
        return {
            "session_id": session_id,
            "history": history or [],
            "question": question,
            "image_bytes": image_base64,
            "image_mime": image_mime,
            "use_knowledge": use_knowledge,
            "errors": [],
            "timings": {},
        }

    # ------------------------------------------------------------------
    def _iter_graph_events(self, state: dict[str, Any]) -> Any:
        """调用 ``astream_events``，并兼容不同版本的签名差异。

        ``version="v2"`` 这个参数在 langchain-core 的不同大版本间经历过
        从「必须显式传」到「默认且已废弃」的变化。这里先做一次签名探测，
        再决定是否传参——比 try/except 更干净（异常方案会消耗掉第一个事件）。
        """
        if self._supports_version is None:
            try:
                params = inspect.signature(self.graph.astream_events).parameters
                self._supports_version = "version" in params
            except (TypeError, ValueError):
                # 签名不可内省（装饰器/C 实现）时按「不传」处理，
                # 现代版本默认就是 v2，行为一致
                self._supports_version = False
        if self._supports_version:
            return self.graph.astream_events(state, version="v2")
        return self.graph.astream_events(state)

    async def astream_events(self, state: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """流式执行，产出面向前端的事件。

        Args:
            state: 初始状态。

        Yields:
            事件字典，``type`` 字段标识事件种类。
        """
        started = time.perf_counter()
        in_generate = False
        answer_parts: list[str] = []
        final_answer = ""
        safety: dict[str, Any] | None = None
        errors: list[str] = []
        detected_intent: str | None = None

        yield {
            "type": "meta",
            "session_id": state.get("session_id", ""),
            "has_image": bool(state.get("image_bytes")),
            "use_knowledge": bool(state.get("use_knowledge", True)),
            "nodes": NODE_ORDER,
        }

        try:
            async for event in self._iter_graph_events(state):
                kind = event.get("event")
                name = event.get("name", "")

                if kind == "on_chain_start" and name in _NODE_NAMES:
                    if name == "generate":
                        in_generate = True
                    yield {
                        "type": "node",
                        "node": name,
                        "label": NODE_LABELS[name],
                        "status": "running",
                    }
                    continue

                if kind == "on_chain_end" and name in _NODE_NAMES:
                    if name == "generate":
                        in_generate = False
                    output = (event.get("data") or {}).get("output")
                    if not isinstance(output, dict):
                        output = {}
                    timings = output.get("timings") or {}
                    yield {
                        "type": "node",
                        "node": name,
                        "label": NODE_LABELS[name],
                        "status": "done",
                        "elapsed_ms": timings.get(name),
                    }

                    if name == "route" and output.get("intent"):
                        detected_intent = str(output["intent"])
                        yield {"type": "meta", "intent": detected_intent}

                    if name == "vision_analyze" and output.get("vision"):
                        yield {"type": "vision", "data": output["vision"]}

                    if name == "retrieve":
                        yield {"type": "sources", "data": output.get("sources") or []}

                    if name == "generate" and output.get("answer"):
                        final_answer = str(output["answer"])

                    if name == "safety_review":
                        if output.get("answer"):
                            final_answer = str(output["answer"])
                        safety = output.get("safety")

                    for item in output.get("errors") or []:
                        if item not in errors:
                            errors.append(str(item))
                    continue

                if kind == "on_chat_model_stream" and in_generate:
                    text = _extract_text(event.get("data", {}).get("chunk"))
                    if text:
                        answer_parts.append(text)
                        yield {"type": "token", "text": text}

        except Exception as exc:  # noqa: BLE001 - 统一转成 error 事件
            logger.exception("智能体执行异常")
            payload = error_payload(exc)
            yield {"type": "error", **payload}
            return

        streamed = "".join(answer_parts).strip()
        # token 流缺失（例如模型不支持流式）时，用节点产出兜底，保证前端一定有内容
        if not streamed and final_answer:
            yield {"type": "answer", "text": final_answer}

        yield {
            "type": "done",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "answer": final_answer or streamed,
            "safety": safety,
            "has_image": bool(state.get("image_bytes")),
            "intent": detected_intent
            or (
                Intent.IMAGE_DIAGNOSIS.value
                if state.get("image_bytes")
                else Intent.CHITCHAT.value
            ),
            "errors": errors,
        }

    async def arun(self, state: dict[str, Any]) -> dict[str, Any]:
        """非流式执行，返回最终状态。

        Args:
            state: 初始状态。

        Returns:
            最终状态字典。
        """
        return await self.graph.ainvoke(state)


_runner: AgentRunner | None = None


def get_runner() -> AgentRunner:
    """获取运行器（进程内单例）。"""
    global _runner
    if _runner is None:
        _runner = AgentRunner()
    return _runner
