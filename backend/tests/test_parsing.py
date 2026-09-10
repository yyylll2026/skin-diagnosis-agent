"""模型输出解析的单元测试。

这一组测试守的是系统最容易崩的一环：本地小模型的 JSON 输出质量。
只要这些用例是绿的，就说明「模型吐了脏数据」不会直接变成 500。
"""

from __future__ import annotations

import pytest

from app.core.parsing import extract_json, inline_json_schema, parse_model, truncate
from app.schemas.diagnosis import RiskLevel, VisionDiagnosis


def test_extract_json_plain() -> None:
    """裸 JSON 能直接解析。"""
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_markdown_fence() -> None:
    """带 ```json 围栏时能剥掉围栏。"""
    raw = '```json\n{"a": 1, "b": "x"}\n```'
    assert extract_json(raw) == {"a": 1, "b": "x"}


def test_extract_json_with_preamble_and_epilogue() -> None:
    """模型爱写「好的，以下是结果：」这类前言，也要能提取。"""
    raw = '好的，以下是分析结果：\n{"a": 1}\n希望对你有所帮助！'
    assert extract_json(raw) == {"a": 1}


def test_extract_json_repairs_trailing_comma() -> None:
    """尾随逗号是本地模型的常见失误。"""
    assert extract_json('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}


def test_extract_json_repairs_python_literals() -> None:
    """Python 风格的 True/None 要能转成 JSON 字面量。"""
    assert extract_json('{"a": True, "b": None}') == {"a": True, "b": None}


def test_extract_json_handles_braces_inside_strings() -> None:
    """字符串里出现 } 不能把括号配对搞乱。"""
    raw = '{"desc": "皮损呈 } 形分布", "n": 1}'
    assert extract_json(raw) == {"desc": "皮损呈 } 形分布", "n": 1}


def test_extract_json_array() -> None:
    """数组提取。"""
    assert extract_json('["皮疹", "湿疹"]', expect="array") == ["皮疹", "湿疹"]


def test_extract_json_raises_on_garbage() -> None:
    """完全不是 JSON 时要明确报错，而不是返回兜底值。"""
    with pytest.raises(ValueError):
        extract_json("这张图片我不太确定该怎么描述。")


def test_parse_model_drops_unknown_keys() -> None:
    """模型多给的字段要被丢弃，而不是引发校验失败。"""
    raw = '{"summary": "面部红斑", "未知字段": "xxx"}'
    result = parse_model(raw, VisionDiagnosis)
    assert result.summary == "面部红斑"


def test_parse_model_fills_missing_fields() -> None:
    """只给了一个字段时，其余走默认值。"""
    result = parse_model('{"summary": "仅一句话"}', VisionDiagnosis)
    assert result.summary == "仅一句话"
    assert result.hypotheses == []
    assert result.recommended_department == "皮肤科"


def test_confidence_percent_is_normalized() -> None:
    """置信度写成百分数（75）要归一化成 0.75。"""
    raw = '{"hypotheses": [{"name": "湿疹", "confidence": 75}]}'
    result = parse_model(raw, VisionDiagnosis)
    assert result.hypotheses[0].confidence == pytest.approx(0.75)


def test_confidence_out_of_range_is_clamped() -> None:
    """超出 0~1 的值要被夹紧。"""
    raw = '{"hypotheses": [{"name": "湿疹", "confidence": 250}]}'
    assert parse_model(raw, VisionDiagnosis).hypotheses[0].confidence == 1.0


def test_chinese_risk_level_is_normalized() -> None:
    """模型返回中文风险等级也要认。"""
    for raw_value, expected in [
        ("高", RiskLevel.HIGH),
        ("紧急", RiskLevel.URGENT),
        ("低风险", RiskLevel.LOW),
        ("HIGH", RiskLevel.HIGH),
    ]:
        raw = f'{{"risk_level": "{raw_value}"}}'
        assert parse_model(raw, VisionDiagnosis).risk_level == expected


def test_hypotheses_as_plain_list_is_coerced() -> None:
    """模型把 hypotheses 写成字符串数组时要能收敛成对象数组。"""
    raw = '{"hypotheses": ["湿疹", "接触性皮炎"]}'
    result = parse_model(raw, VisionDiagnosis)
    assert [item.name for item in result.hypotheses] == ["湿疹", "接触性皮炎"]


def test_morphology_as_delimited_string_is_coerced() -> None:
    """形态描述被写成顿号分隔的字符串时要转成数组。"""
    raw = '{"observations": {"morphology": "红斑、丘疹、鳞屑"}}'
    result = parse_model(raw, VisionDiagnosis)
    assert result.observations.morphology == ["红斑", "丘疹", "鳞屑"]


def test_inline_json_schema_removes_refs() -> None:
    """Schema 展开后不应残留 $ref / $defs —— Ollama 的语法编译器认不了。"""
    schema = inline_json_schema(VisionDiagnosis.model_json_schema())
    dumped = str(schema)
    assert "$ref" not in dumped
    assert "$defs" not in dumped
    assert schema.get("type") == "object"


def test_truncate_collapses_whitespace() -> None:
    """截断会把换行压平，避免破坏上下文排版。"""
    assert truncate("a\n\nb   c", limit=100) == "a b c"
    assert len(truncate("x" * 500, limit=10)) == 11  # 10 + 省略号
