"""Ollama 原生 REST 客户端（httpx 异步实现）。

为什么不完全依赖 LangChain 的 ChatOllama？
- 结构化输出：Ollama ≥0.5 支持 ``format`` 传 JSON Schema 做语法约束，
  这是把「模型自由文本」变成「可校验对象」最可靠的手段，而 LangChain 的封装未暴露该参数；
- 健康检查 / 模型列举：需要直接打 ``/api/tags``；
- 版本兼容：LangChain 对 Ollama 多模态消息的处理在 0.2/0.3 之间有差异，
  直连 REST 可以把这层不确定性收敛到一处。

本模块只做「协议适配」，不含任何业务语义。
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from loguru import logger

from app.core.errors import ModelOutputError, OllamaUnavailableError


class OllamaClient:
    """Ollama HTTP 客户端。实例应复用（内部持有连接池）。"""

    def __init__(self, base_url: str, default_timeout: float = 180.0) -> None:
        """初始化客户端。

        Args:
            base_url: Ollama 服务地址，如 http://127.0.0.1:11434
            default_timeout: 默认读超时（秒）。视觉推理明显慢于文本，建议分开配置。
        """
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(connect=5.0, read=default_timeout, write=30.0, pool=10.0),
        )

    async def aclose(self) -> None:
        """关闭底层连接池。应用 shutdown 时调用。"""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # 探活与模型管理
    # ------------------------------------------------------------------

    async def list_models(self) -> list[dict[str, Any]]:
        """列出本地已有模型。

        Returns:
            模型字典列表；服务不可达时返回空列表（由调用方决定是否视为异常）。
        """
        try:
            resp = await self._client.get("/api/tags")
            resp.raise_for_status()
            return resp.json().get("models", [])
        except Exception as exc:  # noqa: BLE001 - 探活失败不该炸掉调用方
            logger.warning("拉取 Ollama 模型列表失败: {}", exc)
            return []

    async def has_model(self, model: str) -> bool:
        """判断某个模型（含 tag）是否已就绪。

        兼容 ``qwen2.5vl`` 与 ``qwen2.5vl:7b`` 两种写法：用户配置不带 tag 时，
        只要存在同名前缀模型即视为命中。
        """
        models = await self.list_models()
        names = {m.get("name", "") for m in models}
        if model in names:
            return True
        base = model.split(":")[0]
        return any(n.split(":")[0] == base for n in names)

    async def version(self) -> str:
        """获取 Ollama 版本号，失败返回空串。"""
        try:
            resp = await self._client.get("/api/version")
            resp.raise_for_status()
            return str(resp.json().get("version", ""))
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------
    # 对话
    # ------------------------------------------------------------------

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        images: list[bytes] | None = None,
        response_format: dict[str, Any] | str | None = None,
        options: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> str:
        """一次性对话（非流式）。

        Args:
            model: 模型名。
            messages: OpenAI 风格消息列表 ``[{"role": ..., "content": ...}]``。
            images: 附在**最后一条 user 消息**上的图片字节列表（Ollama 的 images 字段）。
            response_format: 传给 Ollama 的 ``format``。传 dict 即 JSON Schema 约束；
                传 ``"json"`` 即仅要求合法 JSON；传 None 表示自由文本。
            options: 采样参数，如 ``{"temperature": 0.2, "num_ctx": 8192}``。
            timeout: 覆盖默认读超时。

        Returns:
            模型输出的文本内容。

        Raises:
            OllamaUnavailableError: 服务不可达或返回非 2xx。
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": options or {},
        }
        if response_format is not None:
            payload["format"] = response_format
        if images:
            if not payload["messages"]:
                raise ValueError("messages 不能为空")
            last = payload["messages"][-1]
            last["images"] = [base64.b64encode(img).decode("ascii") for img in images]

        try:
            resp = await self._client.post(
                "/api/chat",
                json=payload,
                timeout=httpx.Timeout(connect=5.0, read=timeout or 180.0, write=60.0, pool=10.0),
            )
        except httpx.HTTPError as exc:
            raise OllamaUnavailableError(
                f"无法连接 Ollama（{self.base_url}），请确认服务已启动：ollama serve", str(exc)
            ) from exc

        if resp.status_code >= 400:
            # 400 常见于模型不支持 format 约束 —— 交由上层降级重试
            raise ModelOutputError(f"Ollama 返回 {resp.status_code}", detail=resp.text[:500])

        data = resp.json()
        return (data.get("message") or {}).get("content", "")

    async def chat_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        options: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[str]:
        """流式对话，逐块产出增量文本（用于 SSE 兜底路径）。

        Yields:
            每次增量输出的文本片段。
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": options or {},
        }
        timeout_cfg = httpx.Timeout(connect=5.0, read=timeout or 300.0, write=60.0, pool=10.0)
        try:
            async with self._client.stream(
                "POST", "/api/chat", json=payload, timeout=timeout_cfg
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "ignore")
                    raise ModelOutputError(f"Ollama 流式返回 {resp.status_code}", detail=body[:500])
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    piece = (chunk.get("message") or {}).get("content", "")
                    if piece:
                        yield piece
                    if chunk.get("done"):
                        break
        except httpx.HTTPError as exc:
            raise OllamaUnavailableError("流式请求 Ollama 失败", str(exc)) from exc

    # ------------------------------------------------------------------
    # 向量化（embedding）
    # ------------------------------------------------------------------

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        """调用 Ollama 生成文本向量。

        Args:
            model: 向量模型名，如 bge-m3。
            texts: 待向量化文本列表。

        Returns:
            与输入等长的向量列表。

        Raises:
            OllamaUnavailableError: 服务不可达。
        """
        try:
            resp = await self._client.post(
                "/api/embed",
                json={"model": model, "input": texts},
                timeout=httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=10.0),
            )
            resp.raise_for_status()
            return resp.json().get("embeddings", [])
        except httpx.HTTPError as exc:
            raise OllamaUnavailableError(f"Ollama 向量化失败：{exc}") from exc
