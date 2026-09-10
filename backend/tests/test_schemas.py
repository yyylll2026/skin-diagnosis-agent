"""请求契约的宽容性测试。

这一组守的是「客户端写法差异」这类跨端问题。踩过的真实坑：

    前端在没有图片时把可选字段序列化成了 ``"image_mime": null``
    （JS 里 `obj.field ?? null` 是最自然的写法），而后端的默认值
    只在字段**缺失**时生效，显式传 null 会被判为类型错误并返回 400，
    用户看到的是「请求参数有误」，但用户并没有输错任何东西。

结论：「可选字段带默认值」的语义必须包含「传 null 等于没传」，
否则前后端会在字段是否该裁剪上产生无谓的耦合。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.schemas.chat import ChatRequest, Intent
from app.schemas.diagnosis import RiskLevel
from app.schemas.knowledge import SearchRequest

# ---------------------------------------------------------------------------
# ChatRequest：显式 null 必须回落到默认值
# ---------------------------------------------------------------------------


def test_all_optional_fields_explicitly_null_is_accepted() -> None:
    """整体场景复现：纯文本提问时前端把所有可选字段都写成 null。"""
    payload = {
        "session_id": None,
        "message": None,
        "image_base64": None,
        "image_mime": None,
        "use_knowledge": None,
        "stream": None,
    }
    req = ChatRequest.model_validate(payload)
    assert req.message == ""
    assert req.image_base64 is None
    assert req.image_mime == "image/jpeg"
    assert req.use_knowledge is True
    assert req.stream is True


def test_image_mime_null_falls_back_to_jpeg() -> None:
    """核心回归点：image_mime 传 null 不应报错。"""
    req = ChatRequest.model_validate({"message": "皮肤瘙痒", "image_mime": None})
    assert req.image_mime == "image/jpeg"


def test_image_fields_omitted_uses_defaults() -> None:
    """字段直接缺失（老客户端 / curl）同样可用。"""
    req = ChatRequest.model_validate({"message": "湿疹怎么护理"})
    assert req.image_mime == "image/jpeg"
    assert req.use_knowledge is True
    assert req.image_base64 is None


def test_partial_null_only_affects_that_field() -> None:
    """只把某一个字段传成 null，其它字段的显式取值必须保留。"""
    req = ChatRequest.model_validate(
        {"message": "看图", "image_mime": None, "use_knowledge": False, "stream": False}
    )
    assert req.image_mime == "image/jpeg"
    assert req.use_knowledge is False  # 显式 false 不能被默认值覆盖
    assert req.stream is False


def test_image_payload_is_preserved() -> None:
    """带图请求的字段原样保留。"""
    req = ChatRequest.model_validate(
        {"message": "", "image_base64": "AAAA", "image_mime": "image/png"}
    )
    assert req.image_base64 == "AAAA"
    assert req.image_mime == "image/png"


def test_message_too_long_is_rejected() -> None:
    """宽容不等于无约束：超长文本仍要拒绝。"""
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"message": "皮" * 2001})


def test_unknown_fields_are_ignored() -> None:
    """客户端多传字段不应导致失败（向前兼容）。"""
    req = ChatRequest.model_validate({"message": "你好", "some_future_field": 123})
    assert req.message == "你好"


# ---------------------------------------------------------------------------
# SearchRequest
# ---------------------------------------------------------------------------


def test_search_requires_non_empty_query() -> None:
    """检索语句不能为空（min_length=1）。"""
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"query": ""})


def test_search_top_k_bounds() -> None:
    """top_k 超出 1~20 要拒绝。"""
    assert SearchRequest.model_validate({"query": "湿疹", "top_k": 20}).top_k == 20
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"query": "湿疹", "top_k": 21})
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"query": "湿疹", "top_k": 0})


def test_search_category_defaults_to_none() -> None:
    """不传 category 表示全库检索。"""
    assert SearchRequest.model_validate({"query": "湿疹"}).category is None
    assert SearchRequest.model_validate({"query": "湿疹", "category": None}).category is None


# ---------------------------------------------------------------------------
# 枚举：StrEnum 必须能直接当字符串用（JSON 序列化 / 比较）
# ---------------------------------------------------------------------------


def test_intent_serializes_as_plain_string() -> None:
    """意图枚举序列化后应是纯字符串，前端无需额外解包。"""
    assert json.dumps({"intent": Intent.KB_QA}) == '{"intent": "kb_qa"}'
    assert Intent.KB_QA == "kb_qa"


def test_risk_level_serializes_as_plain_string() -> None:
    """风险等级同理，前端按这个字符串做配色映射。"""
    assert json.dumps({"risk": RiskLevel.URGENT}) == '{"risk": "urgent"}'
    assert RiskLevel("high") is RiskLevel.HIGH
