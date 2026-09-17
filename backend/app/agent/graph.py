"""LangGraph 状态图装配。

## 拓扑

```
                    ┌─────────────┐
        START ─────►│ preprocess  │
                    └──────┬──────┘
                           ▼
                    ┌─────────────┐
                    │    route    │  ← 意图识别（规则优先，LLM 兜底）
                    └──────┬──────┘
             ┌─────────────┼──────────────┐
             ▼             ▼              ▼
     ┌───────────────┐ ┌────────────────┐ ┌──────────┐
     │ vision_analyze│ │check_completeness│ │ generate │  ← 闲聊直达生成
     └───────┬───────┘ └───────┬────────┘ └────┬─────┘
             │          ┌──────┴──────┐       │
             │          ▼             ▼       │
             │   ┌───────────┐ ┌────────────┐ │
             │   │  clarify  │ │ build_query│ │  ← 信息不足则追问，够了才检索
             │   └─────┬─────┘ └─────┬──────┘ │
             │         │             ▼        │
             │         │      ┌─────────────┐ │
             │         │      │  retrieve   │ │
             │         │      └──────┬──────┘ │
             │         │             │        │
             └─────────┴─────────────┴────────┤
                                             ▼
                                      ┌─────────────┐
                                      │  generate   │
                                      └──────┬──────┘
                                             ▼
                                      ┌───────────────┐
                                      │ safety_review │
                                      └───────┬───────┘
                                              ▼
                                             END
```

## 关键设计点

- **vision_analyze 之后必然经过 build_query**：这是「融合」的落点——
  视觉结果必须转成检索意图才能撬动知识库，不能直接跳到生成。
- **闲聊直达 generate**：省掉一次无意义的检索，也避免检索噪声污染寒暄回答。
- **generate 在两条支路上复用**：链的选择依据是「有没有视觉结果」而不是「从哪条边来的」，
  这样新增输入组合时不需要改动拓扑。
- **safety_review 是终点**：任何路径最终都要过一遍安全审查，
  保证「每个出口都带免责声明、都做过危险信号扫描」。追问分支也走这条边，
  所以「追问内容同样带免责声明」是由拓扑保证的，不依赖节点自觉。
- **check_completeness 是知识问答分支唯一的入口**：这条路走不到 ``build_query``
  的捷径——想检索，必须先证明信息够了。图片诊断与闲聊则完全不经过它，
  因为这两类输入要么信息已充分，要么根本不需要病情信息。
- **追问支路是单轮往返**：``clarify → safety_review → END``，一问一答即结束本次请求。
  多轮状态不靠图记忆（本项目不用 checkpointer），而是由 ``ClarifyService`` 落库，
  下一轮请求再从 SQLite 读回。这样图的形态保持无状态，重启不丢进度。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agent.nodes import AgentNodes
from app.agent.state import AgentState


def build_graph(nodes: AgentNodes) -> Any:
    """装配并编译状态图。

    Args:
        nodes: 已注入依赖的节点集合。

    Returns:
        编译后的 LangGraph 可执行对象（支持 ``ainvoke`` / ``astream`` / ``astream_events``）。
    """
    graph = StateGraph(AgentState)

    graph.add_node("preprocess", nodes.preprocess)
    graph.add_node("route", nodes.route)
    graph.add_node("vision_analyze", nodes.vision_analyze)
    graph.add_node("check_completeness", nodes.check_completeness)
    graph.add_node("clarify", nodes.clarify)
    graph.add_node("build_query", nodes.build_query)
    graph.add_node("retrieve", nodes.retrieve)
    graph.add_node("generate", nodes.generate)
    graph.add_node("safety_review", nodes.safety_review)

    graph.add_edge(START, "preprocess")
    graph.add_edge("preprocess", "route")

    graph.add_conditional_edges(
        "route",
        nodes.after_route,
        {
            "vision_analyze": "vision_analyze",
            "check_completeness": "check_completeness",
            "generate": "generate",
        },
    )

    # 融合的关键一条边：视觉结果 -> 检索意图
    graph.add_edge("vision_analyze", "build_query")

    # 追问澄清的决策点：信息不足先问，够了才继续检索生成
    graph.add_conditional_edges(
        "check_completeness",
        nodes.after_check_completeness,
        {"clarify": "clarify", "build_query": "build_query"},
    )
    # 追问也要过安全审查：免责声明与危险信号扫描对它是同样必要的
    graph.add_edge("clarify", "safety_review")

    graph.add_conditional_edges(
        "build_query",
        nodes.after_build_query,
        {"retrieve": "retrieve", "generate": "generate"},
    )

    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", "safety_review")
    graph.add_edge("safety_review", END)

    return graph.compile()


@lru_cache(maxsize=1)
def get_graph() -> Any:
    """获取编译后的图单例（编译有开销，全局只做一次）。"""
    from app.dependencies import build_nodes

    return build_graph(build_nodes())


def graph_mermaid() -> str:
    """导出 Mermaid 流程图源码，供设计文档与前端展示使用。"""
    try:
        return get_graph().get_graph().draw_mermaid()
    except Exception:  # noqa: BLE001 - 仅用于文档，失败不影响服务
        return ""
