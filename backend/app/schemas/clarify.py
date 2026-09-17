"""追问澄清相关的数据契约。

与 ``diagnosis.py`` 里的 ``VisionDiagnosis`` 一样，这个模型同时承担两个职责：
1. 约束 LangGraph 状态里 ``clarify`` 字段的形状（节点产出的就是它的 dict 形式）；
2. 作为接口契约的一部分，出现在 ``ChatResponse`` 与 SSE ``clarify`` 事件里，
   前端 ``ClarifyCard.vue`` 按此渲染。

字段全部带默认值：追问被旁路（急症 / 知识提问 / 图片诊断）时只需要表达
「本次没有追问」，不该强制所有字段都有值。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClarifyState(BaseModel):
    """一次信息完备性判断的结果。"""

    needs_clarification: bool = Field(
        default=False, description="是否需要先追问；false 表示已进入正常回答流程"
    )
    missing: list[str] = Field(
        default_factory=list, description="缺失的信息维度（中文标签，面向用户展示）"
    )
    missing_keys: list[str] = Field(
        default_factory=list, description="缺失的信息维度（内部 key，便于前端做定制渲染）"
    )
    collected: dict[str, str] = Field(
        default_factory=dict, description="已收集到的信息，形如 {\"出现时间\": \"3 天\"}"
    )
    round: int = Field(default=0, description="当前已发起的追问轮次（从 1 开始）")
    max_rounds: int = Field(default=0, description="追问轮次上限")
    fallback: bool = Field(
        default=False,
        description="是否因信息不足而转入兜底作答（轮次用尽或用户要求直接回答）",
    )
    bypassed_reason: str = Field(
        default="",
        description=(
            "未追问的原因：urgent 命中危险信号 / image 图片诊断 / "
            "knowledge_question 知识提问 / user_skip 用户要求直接回答 / "
            "round_limit 轮次用尽 / disabled 功能关闭"
        ),
    )
