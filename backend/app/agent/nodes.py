"""智能体节点实现。

每个节点是一个纯函数式的「状态进、增量出」单元，只依赖注入进来的 service，
不直接碰数据库、不直接碰 HTTP。这样切分的好处是：单个节点可以脱离整图独立测试
（见 ``tests/test_nodes.py``），也便于后续把某一个节点换成更复杂的实现。

节点的职责边界：

| 节点 | 职责 | 不做什么 |
|------|------|---------|
| preprocess | 输入净化、图片解码与预处理 | 不判意图 |
| route | 意图分类（规则优先 + LLM 兜底） | 不做检索 |
| vision_analyze | 调 VL 模型产出结构化诊断 | 不生成面向用户的回答 |
| check_completeness | 判断信息是否足够，决定追问还是作答 | 不生成追问文案、不检索 |
| clarify | 把判定结果渲染成结构化追问 | 不判断是否需要追问 |
| build_query | 把视觉/文本转成检索意图 | 不执行检索 |
| retrieve | 混合检索 + 拼上下文 | 不做重排以外的加工 |
| generate | 调 LLM 产出自然语言回答（流式） | 不做安全审查 |
| safety_review | 规则审查 + 答案合规化 | 不改写医学内容 |

`check_completeness` 与 `clarify` 刻意拆成两个节点：前者只做「要不要问」的判定并落库进度，
后者只做「怎么问」的文案渲染。这样追问的**决策逻辑**可以在不渲染任何文本的情况下被精确测试
（见 `tests/test_clarify.py`），文案改版也不会碰判定逻辑。
"""

from __future__ import annotations

import base64
import binascii
import re
import time
from typing import Any

from loguru import logger

from app.agent.state import AgentState
from app.core.errors import ImageValidationError
from app.core.parsing import extract_json
from app.core.prompts import ROUTER_PROMPT
from app.schemas.chat import Intent
from app.schemas.diagnosis import SafetyReport, VisionDiagnosis
from app.services.clarify_service import ClarifyService
from app.services.fusion_service import FusionService
from app.services.llm_factory import get_fast_llm, get_text_llm
from app.services.retrieval_service import RetrievalService
from app.services.safety_service import SafetyService
from app.services.vision_service import VisionService

# 皮肤相关关键词：命中即走知识库问答，无需调 LLM 做意图分类（省 1~2 秒）
SKIN_KEYWORDS: tuple[str, ...] = (
    "皮肤", "皮损", "皮疹", "湿疹", "瘙痒", "痒", "红疹", "疙瘩", "斑点", "痘",
    "痤疮", "粉刺", "痘印", "荨麻疹", "风团", "癣", "真菌", "疣", "疱疹", "银屑病",
    "牛皮癣", "白癜风", "白斑", "黄褐斑", "色斑", "雀斑", "胎记", "痣", "黑素瘤",
    "过敏", "接触性皮炎", "日光性皮炎", "晒伤", "脱发", "头皮屑", "毛囊炎", "疥疮",
    "红斑", "丘疹", "水疱", "脓疱", "鳞屑", "脱皮", "皲裂", "溃烂", "破溃", "化脓",
    "激素", "药膏", "外用药", "抗组胺", "维A酸", "保湿", "防晒", "护肤", "洗脸",
    "忌口", "传染", "会好", "怎么办", "严重吗", "挂什么科", "看什么科",
)

# 明显是寒暄/闲聊的短句
_CHITCHAT_RE = re.compile(
    r"^\s*("
    r"你好|您好|hi|hello|hey|哈喽|嗨|在吗|在不在|早上好|晚上好|下午好|"
    r"谢谢|感谢|多谢|thanks|thank you|"
    r"你是谁|你叫什么|介绍一下你|你能做什么|你会什么|怎么用"
    r")[\s!！。.~～,，]*$",
    re.IGNORECASE,
)


def _elapsed(state_timings: dict[str, float], name: str, started: float) -> dict[str, float]:
    """记录节点耗时（用于定位性能瓶颈）。"""
    timings = dict(state_timings or {})
    timings[name] = round((time.perf_counter() - started) * 1000, 1)
    return timings


class AgentNodes:
    """节点集合。持有各 service 引用，构建一次、复用多次。"""

    def __init__(
        self,
        vision_service: VisionService,
        retrieval_service: RetrievalService,
        fusion_service: FusionService,
        safety_service: SafetyService,
        clarify_service: ClarifyService,
    ) -> None:
        """注入依赖。

        Args:
            vision_service: 视觉诊断服务。
            retrieval_service: 混合检索服务。
            fusion_service: 融合服务（检索意图构造 + 链构建）。
            safety_service: 安全审查服务。
            clarify_service: 信息完备性判断与追问澄清服务。
        """
        self.vision_service = vision_service
        self.retrieval_service = retrieval_service
        self.fusion_service = fusion_service
        self.safety_service = safety_service
        self.clarify_service = clarify_service
        self._chains: dict[str, Any] = {}

    # ------------------------------------------------------------------
    def _chain(self, kind: str) -> Any:
        """惰性构建并缓存生成链。"""
        if kind not in self._chains:
            llm = get_text_llm()
            builder = {
                "kb_qa": self.fusion_service.build_kb_qa_chain,
                "fusion": self.fusion_service.build_fusion_chain,
                "chitchat": self.fusion_service.build_chitchat_chain,
            }[kind]
            self._chains[kind] = builder(llm)
        return self._chains[kind]

    # ------------------------------------------------------------------
    # 1. 预处理
    # ------------------------------------------------------------------

    def preprocess(self, state: AgentState) -> dict[str, Any]:
        """净化文本输入、解码并预处理图片。

        图片预处理放在这里而不是 API 层，是为了让「base64 直传」与「multipart 上传」
        两条入口共用同一套校验逻辑，也保证流式与非流式响应行为一致。

        Args:
            state: 当前状态。

        Returns:
            增量状态：``image_bytes`` / ``image_mime`` / ``image_meta`` / ``question``。
        """
        started = time.perf_counter()
        question = (state.get("question") or "").strip()

        update: dict[str, Any] = {"question": question, "errors": list(state.get("errors") or [])}

        raw_b64 = state.get("image_bytes")
        if raw_b64:
            try:
                # 兼容「带 data URI 前缀」与「纯 base64」两种写法
                payload = raw_b64
                if isinstance(payload, str) and payload.startswith("data:"):
                    payload = payload.split(",", 1)[-1]
                decoded = base64.b64decode(payload, validate=False)
                image_bytes, mime, meta = self.vision_service.prepare_image(
                    decoded, state.get("image_mime") or "image/jpeg"
                )
                update.update(
                    {
                        "image_bytes": image_bytes,
                        "image_mime": mime,
                        "image_meta": meta.model_dump(),
                    }
                )
            except (binascii.Error, ValueError, ImageValidationError) as exc:
                logger.warning("图片预处理失败：{}", exc)
                message = exc.message if isinstance(exc, ImageValidationError) else f"图片解码失败：{exc}"
                update["errors"] = [*update["errors"], message]
                update.update({"image_bytes": None, "image_meta": None})

        update["timings"] = _elapsed(state.get("timings") or {}, "preprocess", started)
        return update

    # ------------------------------------------------------------------
    # 2. 意图路由
    # ------------------------------------------------------------------

    def route(self, state: AgentState) -> dict[str, Any]:
        """判定意图，决定后续走哪条分支。

        决策顺序（**规则优先**，因为本地 LLM 调用一次要 1~2 秒，
        而 90% 的情况用关键词就能判准）：

        1. 有图片（且预处理成功）→ 图片诊断；
        2. 命中皮肤关键词，**或该会话有未完成的追问** → 知识库问答；
        3. 匹配寒暄模板 → 闲聊；
        4. 其余交给轻量模型分类，失败则回落到知识库问答。

        第 2 条里「有未完成的追问」这一半是追问机制能收敛的前提：
        追问后的回复往往是碎片（「三天了」「不知道」），既不含皮肤关键词，
        也匹配不上寒暄模板。若不特殊处理，它们会被判成闲聊或交给 LLM 兜底，
        追问进度就永远推进不到「兜底作答」，用户会卡在只被追问、拿不到回答的状态。

        Args:
            state: 当前状态。

        Returns:
            增量状态：``intent``。
        """
        started = time.perf_counter()
        question = state.get("question") or ""

        if state.get("image_bytes"):
            intent = Intent.IMAGE_DIAGNOSIS.value
        elif not question:
            intent = Intent.CHITCHAT.value
        # 命中皮肤关键词，或该会话有未完成的追问时，都走知识库问答。
        # 后半个条件是「多轮追问能收敛」的关键：用户在追问后的回复往往是碎片
        # （「三天了」「不知道」），既不含皮肤关键词也匹配不上寒暄模板，
        # 若放任它落到 LLM 兜底或闲聊，追问进度就永远推进不到兜底作答，
        # 用户会卡在「一直被问、始终得不到回答」的状态。
        elif any(keyword in question for keyword in SKIN_KEYWORDS) or state.get(
            "clarify_pending"
        ):
            intent = Intent.KB_QA.value
        elif _CHITCHAT_RE.match(question):
            intent = Intent.CHITCHAT.value
        else:
            intent = self._llm_route(question)

        logger.info("意图路由 -> {}（{}）", intent, question[:30] or "（无文本）")
        return {"intent": intent, "timings": _elapsed(state.get("timings") or {}, "route", started)}

    @staticmethod
    def _llm_route(question: str) -> str:
        """用轻量模型兜底分类，任何异常都回落到知识库问答。"""
        try:
            raw = get_fast_llm().invoke(ROUTER_PROMPT.format(message=question[:300])).content
            if isinstance(raw, list):
                raw = "".join(str(part) for part in raw)
            label = str(raw).strip().lower()
            if "chitchat" in label:
                return Intent.CHITCHAT.value
            if "kb_qa" in label:
                return Intent.KB_QA.value
        except Exception as exc:  # noqa: BLE001 - 分类失败不应中断对话
            logger.warning("LLM 意图分类失败，回落到知识库问答：{}", exc)
        return Intent.KB_QA.value

    # ------------------------------------------------------------------
    # 3. 视觉分析
    # ------------------------------------------------------------------

    async def vision_analyze(self, state: AgentState) -> dict[str, Any]:
        """调用 Qwen2.5-VL 做结构化视觉分析。

        图片已在 preprocess 阶段处理完毕；这里如果视觉调用失败，
        **不抛异常中断整图**，而是降级为「无视觉结果 + 错误说明」，
        让后续走纯文本问答路径——用户至少还能得到一个回答，而不是一片空白。

        Args:
            state: 当前状态。

        Returns:
            增量状态：``vision``。
        """
        started = time.perf_counter()
        image = state.get("image_bytes")
        errors = list(state.get("errors") or [])

        if not image:
            return {"vision": None, "timings": _elapsed(state.get("timings") or {}, "vision_analyze", started)}

        try:
            diagnosis = await self.vision_service.analyze(
                image,
                user_text=state.get("question") or "",
                mime=state.get("image_mime") or "image/jpeg",
            )
            return {
                "vision": diagnosis.model_dump(mode="json"),
                "timings": _elapsed(state.get("timings") or {}, "vision_analyze", started),
            }
        except Exception as exc:  # noqa: BLE001 - 视觉失败降级为文本问答
            logger.error("视觉分析失败，降级为文本问答：{}", exc)
            message = getattr(exc, "message", None) or f"图像分析失败：{exc}"
            return {
                "vision": None,
                "errors": [*errors, message],
                "timings": _elapsed(state.get("timings") or {}, "vision_analyze", started),
            }

    # ------------------------------------------------------------------
    # 3.5 信息完备性判断（追问澄清的决策点）
    # ------------------------------------------------------------------

    def check_completeness(self, state: AgentState) -> dict[str, Any]:
        """判断信息是否足够，并在不足时登记一轮追问。

        这是「追问澄清」机制的决策节点，**只做判定、不生成文案**：
        判定结果写进 ``state["clarify"]``，同时把多轮进度（已收集信息、
        待补字段、已用轮次）落库；文案交给下一个 ``clarify`` 节点渲染。

        它继承了安全层的价值观，有四条旁路（优先级从高到低）：

        1. **急症**：命中危险信号直接作答——追问会引入一轮往返延迟，
           绝不能因为「想多问一句」而延误就医；
        2. **用户要求直答**：说了「直接回答」「不想说」就尊重用户，转入兜底作答；
        3. **知识提问**：「湿疹怎么护理」这类泛泛提问追问部位毫无意义；
        4. **图片诊断**：视觉结果已提供部位/形态/颜色等维度，信息已较充分。

        Args:
            state: 当前状态。

        Returns:
            增量状态：``clarify`` / ``needs_clarification`` / ``case_summary`` / ``clarify_note``。
        """
        started = time.perf_counter()
        question = state.get("question") or ""
        session_id = state.get("session_id") or ""

        try:
            decision = self.clarify_service.evaluate(
                session_id,
                question,
                has_image=bool(state.get("image_bytes")),
                clarify_pending=bool(state.get("clarify_pending")),
            )
        except Exception as exc:  # noqa: BLE001 - 追问判定失败绝不能阻断回答
            logger.error("信息完备性判断失败，直接进入回答流程：{}", exc)
            return {
                "clarify": None,
                "needs_clarification": False,
                "clarify_note": "",
                "timings": _elapsed(state.get("timings") or {}, "check_completeness", started),
            }

        update: dict[str, Any] = {
            "clarify": decision.to_dict(),
            "needs_clarification": decision.needs_clarification,
            "clarify_note": self.clarify_service.build_note(decision),
        }
        # 多轮累积的病例摘要：既让检索语句比「三天了，很痒」这句话更有信息量，
        # 也让生成模型知道前面几轮到底问出了什么
        if decision.facts_text:
            update["case_summary"] = decision.facts_text

        logger.info(
            "完备性判断：追问={} 缺失={} 轮次={}/{} 旁路={}",
            decision.needs_clarification,
            decision.missing,
            decision.round,
            decision.max_rounds,
            decision.bypassed_reason or "-",
        )
        update["timings"] = _elapsed(state.get("timings") or {}, "check_completeness", started)
        return update

    def clarify(self, state: AgentState) -> dict[str, Any]:
        """把判定结果渲染成结构化的追问文案。

        文案用确定性模板拼装而非调模型，理由有两条：
        - **追问天然是结构化的**（「缺哪几项」直接决定「问哪几句」），模板比生成更准；
        - 追问本身要占用一整轮往返，不该再叠加一次模型调用的延迟与失败风险。

        产出的 ``answer`` 会照常经过 ``safety_review``，
        因此免责声明与紧急横幅的保证在这里同样成立。

        Args:
            state: 当前状态。

        Returns:
            增量状态：``answer``（追问文本）。
        """
        started = time.perf_counter()
        clarify = state.get("clarify") or {}
        answer = self.clarify_service.build_question(clarify)
        logger.info("生成追问：缺失 {} 项", len(clarify.get("missing_keys") or []))
        return {
            "answer": answer,
            "timings": _elapsed(state.get("timings") or {}, "clarify", started),
        }

    # ------------------------------------------------------------------
    # 4. 构建检索意图（融合层）
    # ------------------------------------------------------------------

    def build_query(self, state: AgentState) -> dict[str, Any]:
        """把视觉结果 / 用户文本转成检索 query。

        若存在多轮累积的 ``case_summary``（追问澄清收集来的维度），会拼在本轮问题之前：
        用户补充信息那一轮说的话往往很短（「三天了，很痒」），
        单靠这句话去检索召回质量很差，而「部位：面部；出现时间：3 天；伴随感觉：瘙痒」
        的信息密度高得多。

        Args:
            state: 当前状态。

        Returns:
            增量状态：``queries``。
        """
        started = time.perf_counter()
        vision = state.get("vision")
        vision_obj = VisionDiagnosis.model_validate(vision) if vision else None

        try:
            llm = get_fast_llm()
        except Exception:  # noqa: BLE001 - 模型不可用时走纯规则路径
            llm = None

        user_text = (state.get("question") or "").strip()
        case_summary = (state.get("case_summary") or "").strip()
        if case_summary:
            user_text = f"{case_summary} {user_text}".strip()

        queries = self.fusion_service.build_queries(user_text, vision_obj, llm=llm)
        logger.info("检索意图 {} 条：{}", len(queries), queries)
        return {
            "queries": queries,
            "timings": _elapsed(state.get("timings") or {}, "build_query", started),
        }

    # ------------------------------------------------------------------
    # 5. 检索
    # ------------------------------------------------------------------

    def retrieve(self, state: AgentState) -> dict[str, Any]:
        """混合检索并拼装上下文。

        Args:
            state: 当前状态。

        Returns:
            增量状态：``sources`` / ``context``。
        """
        started = time.perf_counter()
        queries = state.get("queries") or []
        error_prefix = ""

        try:
            sources = self.retrieval_service.retrieve_multi(queries)
        except Exception as exc:  # noqa: BLE001 - 检索失败不应中断生成
            logger.error("知识库检索失败：{}", exc)
            sources = []
            error_prefix = f"知识库检索失败（{exc}）"

        context = self.fusion_service.build_context(sources)
        logger.info("检索命中 {} 条资料", len(sources))

        update: dict[str, Any] = {
            "sources": [item.model_dump(mode="json") for item in sources],
            "context": context,
            "timings": _elapsed(state.get("timings") or {}, "retrieve", started),
        }
        if error_prefix:
            update["errors"] = [*(state.get("errors") or []), error_prefix]
        return update

    # ------------------------------------------------------------------
    # 6. 生成
    # ------------------------------------------------------------------

    async def generate(self, state: AgentState) -> dict[str, Any]:
        """生成面向用户的回答。

        三条生成链的选择依据：**视觉结果是否存在**比意图更本质——
        只要拿到了视觉诊断，就必须用融合链（因为要输出「观察→方向→依据→建议」结构）。

        Args:
            state: 当前状态。

        Returns:
            增量状态：``answer``。
        """
        started = time.perf_counter()
        question = state.get("question") or ""
        vision = state.get("vision")
        context = state.get("context") or ""
        history = state.get("history") or []

        if vision:
            chain = self._chain("fusion")
            payload = {
                "vision_json": VisionDiagnosis.model_validate(vision).model_dump_json(
                    exclude_none=True
                ),
                "context": context,
                "question": question or "请分析这张皮肤照片。",
                "history": history,
            }
        elif state.get("intent") == Intent.CHITCHAT.value:
            chain = self._chain("chitchat")
            payload = {"question": question, "history": history}
        else:
            chain = self._chain("kb_qa")
            payload = {
                "context": context,
                "question": question,
                # 信息足不足会改变回答的写法：缺口大时要给「如果……则可能……」的
                # 条件化分析，而不是装作知道。默认值保证正常路径下这一节读起来通顺。
                "clarify_note": state.get("clarify_note")
                or "用户提供的信息基本完整，按常规流程作答。",
                "history": history,
            }

        try:
            response = await chain.ainvoke(payload)
            content = response.content
            if isinstance(content, list):  # content block 列表
                content = "".join(
                    part if isinstance(part, str) else str(part.get("text", ""))
                    for part in content
                )
            answer = str(content).strip()
        except Exception as exc:  # noqa: BLE001 - 生成失败给可读的兜底话术
            logger.error("生成回答失败：{}", exc)
            answer = (
                "抱歉，本地模型生成回答时出现异常，请稍后重试。"
                "如果问题较紧急，建议直接前往皮肤科面诊。"
            )
            return {
                "answer": answer,
                "errors": [*(state.get("errors") or []), f"生成失败：{exc}"],
                "timings": _elapsed(state.get("timings") or {}, "generate", started),
            }

        return {
            "answer": answer,
            "timings": _elapsed(state.get("timings") or {}, "generate", started),
        }

    # ------------------------------------------------------------------
    # 7. 安全审查
    # ------------------------------------------------------------------

    def safety_review(self, state: AgentState) -> dict[str, Any]:
        """规则化安全审查 + 答案合规化。

        放在生成之后而不是之前：因为审查要同时看「模型说了什么」
        和「用户问了什么」，只有答案落地后才能做完整的危险信号扫描。

        Args:
            state: 当前状态。

        Returns:
            增量状态：``answer``（已追加免责声明）/ ``safety``。
        """
        started = time.perf_counter()
        vision = state.get("vision")
        report = self.safety_service.assess(
            vision=VisionDiagnosis.model_validate(vision) if vision else None,
            user_text=state.get("question") or "",
            answer_text=state.get("answer") or "",
        )
        answer = self.safety_service.augment(state.get("answer") or "", report)

        logger.info(
            "安全审查：risk={}，强制就医={}，危险信号 {} 条",
            report.risk_level.value,
            report.must_see_doctor,
            len(report.red_flags),
        )
        return {
            "answer": answer,
            "safety": report.model_dump(mode="json"),
            "timings": _elapsed(state.get("timings") or {}, "safety_review", started),
        }

    # ------------------------------------------------------------------
    # 条件边判定
    # ------------------------------------------------------------------

    @staticmethod
    def after_route(state: AgentState) -> str:
        """route 之后的走向。

        知识库问答先过 ``check_completeness`` 而不是直奔 ``build_query``：
        信息不够时应该先追问，而不是拿一句「我脸上起了红疹」去检索出一堆泛泛的资料。
        """
        intent = state.get("intent")
        if intent == Intent.IMAGE_DIAGNOSIS.value:
            return "vision_analyze"
        if intent == Intent.KB_QA.value:
            return "check_completeness"
        return "generate"

    @staticmethod
    def after_check_completeness(state: AgentState) -> str:
        """check_completeness 之后的走向：追问，或进入正常回答流程。

        能走到这个节点的只有知识库问答分支（图片与闲聊都在 route 处就分流出去了），
        所以这里只需区分「追问」与「继续检索生成」两件事。
        """
        if state.get("needs_clarification"):
            return "clarify"
        return "build_query"

    @staticmethod
    def after_build_query(state: AgentState) -> str:
        """build_query 之后的走向：关闭知识库或没有可用 query 时直接生成。"""
        if not state.get("use_knowledge", True):
            return "generate"
        if not state.get("queries"):
            return "generate"
        return "retrieve"

    @staticmethod
    def collect_safety(state: AgentState) -> SafetyReport:
        """从状态中还原安全报告（供非流式响使用）。"""
        safety = state.get("safety") or {}
        return SafetyReport.model_validate(safety) if safety else SafetyReport()

    @staticmethod
    def parse_json_loose(text: str) -> Any:
        """宽容解析 JSON（暴露给测试与运维脚本）。"""
        return extract_json(text)
