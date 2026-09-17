"""LangGraph 智能体的状态定义。

关于「为什么不用 checkpointer 存会话」：
LangGraph 自带 ``MemorySaver`` / ``SqliteSaver`` 做状态持久化，看起来很省事，
但本项目选择把会话历史放在自己的 SQLite 里，理由是：
1. **可查询**：前端要「会话列表 + 历史回放」，需要能按 session_id 查、能删；
   checkpointer 的存储格式是框架内部实现，不适合直接当业务表用；
2. **可控**：图片是多模态输入的大对象，塞进 checkpoint 会让存储快速膨胀，
   而我们只需要在「当次请求」内用它，不需要持久化；
3. **可移植**：换 LangGraph 版本时不必担心 checkpoint 兼容性。

因此状态是**单次请求级**的：每次调用由 API 层从 SQLite 读出最近 N 轮历史，
注入 ``history`` 字段，跑完即弃。
"""

from __future__ import annotations

from typing import Any, TypedDict

from langchain_core.messages import BaseMessage


class AgentState(TypedDict, total=False):
    """单次请求的流转状态。

    所有字段都是 ``total=False``（可选），因为不同分支只填自己负责的部分：
    纯文本问答不会填 ``vision``，图片诊断也不会填空的 ``queries``。
    """

    # ---- 输入 ----
    session_id: str
    history: list[BaseMessage]
    question: str
    image_bytes: bytes | None
    image_mime: str
    use_knowledge: bool
    # 本轮之前是否处于「追问未完成」状态。
    # 由 API 层从 SQLite 读出后注入，使 route/节点的判定保持纯函数（不碰数据库），
    # 也便于测试直接构造状态。
    clarify_pending: bool

    # ---- 中间产物 ----
    intent: str
    image_meta: dict[str, Any] | None
    vision: dict[str, Any] | None
    queries: list[str]
    sources: list[dict[str, Any]]
    context: str
    # 多轮累积的病例摘要（如「部位与范围：面部；出现时间：3 天」），
    # 由 check_completeness 从追问进度中还原，供检索与生成复用。
    case_summary: str

    # ---- 追问澄清 ----
    needs_clarification: bool
    clarify: dict[str, Any] | None
    clarify_note: str

    # ---- 输出 ----
    answer: str
    safety: dict[str, Any] | None
    errors: list[str]

    # ---- 观测 ----
    timings: dict[str, float]


# 节点名 -> 面向用户的中文标签。前端左侧进度提示与 SSE 事件都用它。
NODE_LABELS: dict[str, str] = {
    "preprocess": "预处理输入",
    "route": "意图识别",
    "vision_analyze": "图像分析",
    "check_completeness": "信息完备性判断",
    "clarify": "生成追问",
    "build_query": "构建检索意图",
    "retrieve": "检索知识库",
    "generate": "生成回答",
    "safety_review": "安全审查",
}

# 节点全集（大致按执行顺序排列）。实际走向依条件边而定：
#   route ─┬─ vision_analyze ─┐
#          ├─ check_completeness ─┬─ clarify ─┐
#          │                      └─ build_query ─ retrieve ─┐
#          └─ generate ◄────────────────────────────────────┘
#                     └─ safety_review
NODE_ORDER: list[str] = [
    "preprocess",
    "route",
    "vision_analyze",
    "check_completeness",
    "clarify",
    "build_query",
    "retrieve",
    "generate",
    "safety_review",
]
