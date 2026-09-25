"""OpenAI 兼容云端模型客户端（阿里云百炼 / 任意 compatible-mode 端点）。

与 ``OllamaClient`` 保持同构的最小接口：``list_models`` / ``has_model`` /
``chat`` / ``embed`` / ``aclose``，使 vision、健康检查、工厂可以按
``SKIN_LLM_BACKEND`` 切换而无需改业务编排。

协议上做了两处本地 Ollama 没有的映射：

- ``response_format``：dict 按 JSON Schema 或 JSON 模式下发；部分兼容端点
  只认 ``{"type": "json_object"}``，400 时由上层三级降级重试；
- 图片：挂到最后一条 user 消息的 ``image_url`` content block（data URI）。
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from loguru import logger

from app.core.errors import ModelOutputError, OllamaUnavailableError


class CloudLLMClient:
    """OpenAI 兼容 HTTP 客户端。实例应复用（内部持有连接池）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        default_timeout: float = 180.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = (api_key or "").strip()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(connect=10.0, read=default_timeout, write=60.0, pool=10.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _require_key(self) -> None:
        if not self.api_key:
            raise OllamaUnavailableError(
                "未配置云端模型 API Key：请设置 SKIN_CLOUD_API_KEY 或 DASHSCOPE_API_KEY"
            )

    # ------------------------------------------------------------------
    # 探活
    # ------------------------------------------------------------------

    async def list_models(self) -> list[dict[str, Any]]:
        """列出云端可用模型；失败返回空列表（健康检查据此降级）。"""
        if not self.api_key:
            return []
        try:
            resp = await self._client.get("/models")
            resp.raise_for_status()
            data = resp.json()
            items = data.get("data") or []
            # OpenAI 用 id；映射成 Ollama 风格的 name，便于 has_model 复用
            return [{"name": str(item.get("id", ""))} for item in items]
        except Exception as exc:  # noqa: BLE001 - 探活失败不该炸掉调用方
            logger.warning("拉取云端模型列表失败: {}", exc)
            return []

    async def has_model(self, model: str) -> bool:
        """按配置名判断模型是否可用；列表拉不到时，有 Key 即视为就绪。"""
        if not self.api_key:
            return False
        models = await self.list_models()
        if not models:
            # 网关可能禁用 /models，有 Key 就交给真正调用时再暴露 4xx/401
            return True
        names = {str(m.get("name", "")) for m in models}
        if model in names:
            return True
        base = model.split(":")[0]
        return any(n.split(":")[0] == base for n in names)

    async def version(self) -> str:
        return "cloud"

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
        """一次性对话（非流式）。参数语义对齐 OllamaClient.chat。"""
        self._require_key()
        payload_messages = [dict(m) for m in messages]

        if images:
            if not payload_messages:
                raise ValueError("messages 不能为空")
            # 找到最后一条 user；没有则追加，避免把图片挂到 system 上
            user_idx = next(
                (
                    i
                    for i in range(len(payload_messages) - 1, -1, -1)
                    if payload_messages[i].get("role") == "user"
                ),
                None,
            )
            if user_idx is None:
                payload_messages.append({"role": "user", "content": ""})
                user_idx = len(payload_messages) - 1

            content = payload_messages[user_idx].get("content", "")
            # 已是 content block 列表则保持结构，否则包一层
            blocks: list[dict[str, Any]]
            if isinstance(content, list):
                blocks = list(content)
            else:
                blocks = [{"type": "text", "text": str(content or "")}]
            for img in images:
                data_uri = f"data:image/jpeg;base64,{base64.b64encode(img).decode('ascii')}"
                blocks.append({"type": "image_url", "image_url": {"url": data_uri}})
            payload_messages[user_idx]["content"] = blocks

        payload: dict[str, Any] = {
            "model": model,
            "messages": payload_messages,
            "stream": False,
        }
        temperature = (options or {}).get("temperature")
        if temperature is not None:
            payload["temperature"] = temperature
        max_tokens = (options or {}).get("num_predict")
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        if response_format is not None:
            if isinstance(response_format, str) and response_format == "json":
                payload["response_format"] = {"type": "json_object"}
            elif isinstance(response_format, dict):
                # 多数兼容网关只稳定支持 json_object；Schema 作为顶层扩展字段
                # 尝试下发，被拒时上层会降级
                payload["response_format"] = {"type": "json_object", "json_schema": response_format}
            # freeform / None：不传

        try:
            resp = await self._client.post(
                "/chat/completions",
                json=payload,
                timeout=httpx.Timeout(connect=10.0, read=timeout or 180.0, write=60.0, pool=10.0),
            )
        except httpx.HTTPError as exc:
            raise OllamaUnavailableError(
                f"无法连接云端模型服务（{self.base_url}）", str(exc)
            ) from exc

        if resp.status_code >= 400:
            # 400 常见于 response_format 不被支持 —— 交由上层降级重试
            raise ModelOutputError(f"云端模型返回 {resp.status_code}", detail=resp.text[:500])

        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise ModelOutputError("云端模型响应缺少 choices", detail=str(data)[:500])
        message = choices[0].get("message") or {}
        content = message.get("content", "")
        if isinstance(content, list):
            content = "".join(
                part if isinstance(part, str) else str(part.get("text", "")) for part in content
            )
        return str(content)

    async def chat_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        options: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[str]:
        """流式对话，逐块产出增量文本。"""
        self._require_key()
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        temperature = (options or {}).get("temperature")
        if temperature is not None:
            payload["temperature"] = temperature

        timeout_cfg = httpx.Timeout(connect=10.0, read=timeout or 300.0, write=60.0, pool=10.0)
        try:
            async with self._client.stream(
                "POST", "/chat/completions", json=payload, timeout=timeout_cfg
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "ignore")
                    raise ModelOutputError(
                        f"云端模型流式返回 {resp.status_code}", detail=body[:500]
                    )
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        if raw == "[DONE]":
                            break
                        continue
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content") or ""
                    if piece:
                        yield piece
        except httpx.HTTPError as exc:
            raise OllamaUnavailableError("流式请求云端模型失败", str(exc)) from exc

    # ------------------------------------------------------------------
    # 向量化
    # ------------------------------------------------------------------

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        """调用云端 /embeddings 生成向量。"""
        self._require_key()
        try:
            resp = await self._client.post(
                "/embeddings",
                json={"model": model, "input": texts},
                timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
            )
            resp.raise_for_status()
            items = resp.json().get("data") or []
            # OpenAI 按 index 排序；确保与输入对齐
            items = sorted(items, key=lambda x: int(x.get("index", 0)))
            return [[float(x) for x in item.get("embedding", [])] for item in items]
        except httpx.HTTPError as exc:
            raise OllamaUnavailableError(f"云端向量化失败：{exc}") from exc
