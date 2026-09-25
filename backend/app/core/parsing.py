"""LLM 输出解析工具。

本地小模型的 JSON 输出经常「不那么干净」：带 markdown 围栏、带解释性前言、
Python 风格的 True/None、尾随逗号。直接 ``json.loads`` 必然频繁失败。
本模块提供一套「宽容解析 + 结构收敛」的工具，是整条链路鲁棒性的兜底。
"""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from loguru import logger
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_PY_LITERAL_MAP = {"True": "true", "False": "false", "None": "null"}


def _strip_fences(text: str) -> str:
    """去掉 markdown 代码围栏，保留围栏内内容。"""
    match = _FENCE_RE.search(text)
    return match.group(1).strip() if match else text.strip()


def _find_balanced_span(text: str, opener: str, closer: str) -> str | None:
    """从第一个 ``opener`` 开始，扫描出配平的括号区间。

    用状态机跳过字符串内部的括号，避免 ``{"desc": "含 } 的文本"}`` 被误截断。

    Args:
        text: 原始文本。
        opener: 起始字符，``{`` 或 ``[``。
        closer: 结束字符，``}`` 或 ``]``。

    Returns:
        配平的 JSON 子串；找不到返回 None。
    """
    start = text.find(opener)
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        char = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def _repair(raw: str) -> str:
    """修补常见 JSON 语法瑕疵。"""
    fixed = raw
    # 去掉尾随逗号：{"a": 1,} / [1,2,]
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    # Python 风格字面量
    for key, value in _PY_LITERAL_MAP.items():
        fixed = re.sub(rf"\b{key}\b", value, fixed)
    return fixed


def extract_json(text: str, expect: str = "object") -> Any:
    """从模型输出中提取 JSON。

    Args:
        text: 模型原始输出。
        expect: ``"object"`` 或 ``"array"``，用于选择括号配对方式。

    Returns:
        解析后的 Python 对象。

    Raises:
        ValueError: 无法提取出合法 JSON。
    """
    if not text or not text.strip():
        raise ValueError("模型输出为空")

    opener, closer = ("{", "}") if expect == "object" else ("[", "]")
    cleaned = _strip_fences(text)

    # 依次尝试：原文 -> 配平子串 -> 修补后的配平子串
    candidates: list[str] = [cleaned]
    span = _find_balanced_span(cleaned, opener, closer)
    if span:
        candidates.extend([span, _repair(span)])
    candidates.append(_repair(cleaned))

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    logger.warning("JSON 提取失败，原文前 300 字：{}", text[:300])
    raise ValueError("无法从模型输出中解析出 JSON")


def parse_model(text: str, model_cls: type[T]) -> T:
    """把模型输出解析并校验为目标 Pydantic 模型。

    对缺失字段采取「宽容策略」：丢弃未知键、用默认值补齐缺失键，
    只有在核心结构完全不可用时才抛出异常。

    Args:
        text: 模型原始输出。
        model_cls: 目标模型类。

    Returns:
        校验后的模型实例。

    Raises:
        ValueError: JSON 无法提取或校验失败。
    """
    data = extract_json(text, expect="object")
    if not isinstance(data, dict):
        raise ValueError(f"期望 JSON 对象，实际得到 {type(data).__name__}")

    allowed = set(model_cls.model_fields.keys())
    filtered = {k: v for k, v in data.items() if k in allowed}
    try:
        return model_cls.model_validate(filtered)
    except ValidationError as exc:
        logger.warning("模型输出校验失败，尝试宽松回退：{}", exc.errors()[:3])
        # 逐字段回退：保留能校验通过的字段，其余走默认值
        safe: dict[str, Any] = {}
        for key, value in filtered.items():
            try:
                model_cls.model_validate({**{k: None for k in allowed}, key: value})
                safe[key] = value
            except ValidationError:
                continue
        try:
            return model_cls.model_validate(safe)
        except ValidationError as exc2:
            raise ValueError(f"模型输出不符合约定结构：{exc2.errors()[:3]}") from exc2


def truncate(text: str, limit: int = 300, suffix: str = "…") -> str:
    """截断长文本，用于构造检索摘要。"""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + suffix


def inline_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """把 JSON Schema 里的 ``$ref`` 就地展开。

    为什么必须做这一步：``model_json_schema()`` 会把嵌套模型抽到 ``$defs`` 里，
    再用 ``{"$ref": "#/$defs/LesionObservation"}`` 引用。Ollama 的 ``format``
    参数背后是 GBNF 语法编译器，对 ``$ref`` 的支持在不同版本间并不一致，
    展开成自包含的扁平结构是兼容性最好的写法。

    Args:
        schema: Pydantic 生成的 JSON Schema。

    Returns:
        已展开 ``$ref`` 的自包含 Schema（原地修改并返回）。
    """
    defs = schema.pop("$defs", {})

    def resolve(node: Any, depth: int = 0) -> Any:
        if depth > 12:  # 防御性上限，避免异常 schema 导致栈溢出
            return node
        if isinstance(node, dict):
            if "$ref" in node:
                ref_name = str(node["$ref"]).rsplit("/", 1)[-1]
                target = defs.get(ref_name, {})
                siblings = {k: v for k, v in node.items() if k != "$ref"}
                return resolve({**target, **siblings}, depth + 1)
            return {key: resolve(value, depth + 1) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(item, depth + 1) for item in node]
        return node

    resolved = resolve(schema)
    return resolved if isinstance(resolved, dict) else schema
