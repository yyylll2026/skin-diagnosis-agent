"""融合服务：把「视觉诊断」与「知识库」编织成同一份回答。

## 融合的核心思路

图片识别与文本问答不是两个独立功能的简单堆叠，而是**两阶段串联**：

```
图片 ──► Qwen2.5-VL ──► 结构化诊断 ──► 改造为检索意图 ──► Chroma 检索 ──► 融合生成 ──► 回答
                            ▲                              ▲
                            └── 视觉给出「猜什么」          └── 知识库给出「依据是什么」
```

为什么不能各答各的？

- 只做视觉：模型只能说「看起来像湿疹」，没有依据、没有护理建议，用户拿到的是半成品；
- 只做检索：用户上传图片却不知道该问什么关键词，检索无从下手；
- 串联后：视觉把「图像空间」映射到「医学语义空间」（生成疾病名与形态学术语），
  这恰好是向量检索最擅长匹配的东西。检索结果再反过来约束生成的措辞，
  让模型从「自由的视觉描述」收敛到「有据可依的科普回答」。

## 三种输入组合的统一处理

| 输入 | 检索 query 的来源 | 生成方式 |
|------|------------------|---------|
| 纯文本 | 用户问题本身 | 知识库问答提示词 |
| 纯图片 | 视觉结果生成 3 条 query | 图文融合提示词 |
| 图片+文本 | 视觉结果 + 用户文本 | 图文融合提示词 |
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from loguru import logger

from app.core.parsing import extract_json, truncate
from app.core.prompts import (
    CHITCHAT_SYSTEM_PROMPT,
    FUSION_SYSTEM_PROMPT,
    KB_QA_SYSTEM_PROMPT,
    QUERY_BUILD_PROMPT,
)
from app.schemas.diagnosis import VisionDiagnosis
from app.schemas.knowledge import RetrievedChunk

# 注入提示词的参考资料总长度预算（字符）。
# 本地 7B 模型 num_ctx=8192 时，光资料占满会挤掉对话历史与生成空间，
# 因此显式设一个上限，超出的片段直接丢弃（已按相关度排序，丢尾部损失最小）。
CONTEXT_CHAR_BUDGET = 4000


class FusionService:
    """多模态融合服务。"""

    # ------------------------------------------------------------------
    # 一、构造检索意图
    # ------------------------------------------------------------------

    @staticmethod
    def _fallback_queries(user_text: str, vision: VisionDiagnosis | None) -> list[str]:
        """确定性兜底：LLM 不可用或输出异常时，用规则从结构化结果拼检索词。

        结构化诊断本身已经带了疾病名与形态学术语，足够拼出可用的 query。

        Args:
            user_text: 用户原始输入。
            vision: 视觉诊断结果。

        Returns:
            最多 3 条检索语句。
        """
        queries: list[str] = []

        if vision is not None:
            for hypothesis in vision.hypotheses[:2]:
                if hypothesis.name:
                    queries.append(f"{hypothesis.name} 病因 表现 鉴别")

            observations = vision.observations
            morphological = " ".join(observations.morphology[:4])
            colors = " ".join(observations.color[:2])
            site = observations.site or ""
            descriptor = " ".join(part for part in (site, colors, morphological) if part)
            if descriptor:
                queries.append(f"{descriptor} 常见疾病 特点")

            if vision.red_flags:
                queries.append("皮肤 危险信号 就医指征 需警惕")
            else:
                queries.append("皮肤护理 注意事项 何时就医")

        if user_text.strip():
            queries.append(truncate(user_text.strip(), 40, suffix=""))

        # 去重并保序
        unique: list[str] = []
        for query in queries:
            if query and query not in unique:
                unique.append(query)
        return unique[:3]

    def build_queries(
        self,
        user_text: str,
        vision: VisionDiagnosis | None,
        llm: BaseChatModel | None = None,
    ) -> list[str]:
        """构造用于知识库检索的 query 列表。

        有视觉结果时优先让轻量 LLM 生成更「像医学术语」的 query；
        任何失败都回落到确定性拼装，保证链路不中断。

        Args:
            user_text: 用户原始输入。
            vision: 视觉诊断结果，纯文本问答时为 None。
            llm: 用于生成 query 的模型（建议传 fast 模型）。

        Returns:
            1~3 条检索语句。
        """
        if vision is None:
            return [truncate(user_text.strip(), 60, suffix="")] if user_text.strip() else []

        fallback = self._fallback_queries(user_text, vision)
        if llm is None:
            return fallback

        try:
            prompt = QUERY_BUILD_PROMPT.format(
                vision_json=vision.model_dump_json(exclude_none=True),
                user_text=user_text.strip() or "（无）",
            )
            raw = llm.invoke(prompt).content
            if isinstance(raw, list):
                raw = "".join(str(part) for part in raw)

            items = extract_json(str(raw), expect="array")
            queries = [str(item).strip() for item in items if str(item).strip()]

            # 生成的 query 太少时，用兜底结果补齐
            for query in fallback:
                if len(queries) >= 3:
                    break
                if query not in queries:
                    queries.append(query)

            if queries:
                logger.debug("LLM 生成检索意图 {} 条", len(queries))
                return queries[:3]
        except Exception as exc:  # noqa: BLE001 - 查询生成失败必须能降级
            logger.warning("LLM 构造检索意图失败，使用规则兜底：{}", exc)

        return fallback

    # ------------------------------------------------------------------
    # 二、构造上下文
    # ------------------------------------------------------------------

    @staticmethod
    def build_context(sources: list[RetrievedChunk]) -> str:
        """把检索结果拼成带编号的参考资料块。

        编号是给模型做脚注引用的锚点（提示词里要求它写 ``[1][2]``），
        前端把 ``sources`` 按同样顺序渲染，两边就能对上。

        Args:
            sources: 检索结果。

        Returns:
            拼接好的上下文文本；无结果时返回明确的空标记。
        """
        if not sources:
            return "（本次未检索到相关资料，请在回答中明确说明「现有资料未涵盖该问题」）"

        blocks: list[str] = []
        used = 0
        for index, item in enumerate(sources, start=1):
            header = f"[{index}] 《{item.title or item.source}》"
            if item.category:
                header += f"（分类：{item.category}）"
            if item.page:
                header += f" 第 {item.page} 页"
            block = f"{header}\n{item.snippet}"

            if used + len(block) > CONTEXT_CHAR_BUDGET:
                logger.debug("上下文预算用尽，丢弃 {} 条尾部资料", len(sources) - index + 1)
                break
            blocks.append(block)
            used += len(block)

        return "\n\n".join(blocks) if blocks else "（本次未检索到相关资料）"

    # ------------------------------------------------------------------
    # 三、构造生成链
    # ------------------------------------------------------------------

    @staticmethod
    def build_kb_qa_chain(llm: BaseChatModel) -> Any:
        """构建纯知识库问答链（LCEL）。

        Args:
            llm: 文本生成模型。

        Returns:
            ``prompt | llm`` 的可运行链，输入 ``{context, question, history}``。
        """
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", KB_QA_SYSTEM_PROMPT),
                ("placeholder", "{history}"),
                ("human", "{question}"),
            ]
        )
        return prompt | llm

    @staticmethod
    def build_fusion_chain(llm: BaseChatModel) -> Any:
        """构建图文融合生成链（LCEL）。

        Args:
            llm: 文本生成模型。

        Returns:
            ``prompt | llm`` 的可运行链，输入 ``{vision_json, context, question, history}``。
        """
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", FUSION_SYSTEM_PROMPT),
                ("placeholder", "{history}"),
                ("human", "用户的问题：{question}\n\n请按上述结构输出回答。"),
            ]
        )
        return prompt | llm

    @staticmethod
    def build_chitchat_chain(llm: BaseChatModel) -> Any:
        """构建闲聊兜底链。"""
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", CHITCHAT_SYSTEM_PROMPT),
                ("placeholder", "{history}"),
                ("human", "{question}"),
            ]
        )
        return prompt | llm
