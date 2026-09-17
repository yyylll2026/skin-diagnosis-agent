"""信息完备性判断与追问澄清服务。

## 这个模块解决什么问题

用户常抛出一句「我脸上起了红疹怎么办」，信息量只够支撑一句泛泛的科普，
不足以给出有针对性的建议。于是系统在检索之前先做一次**完备性判断**：
关键维度缺得太多就先追问，而不是硬着头皮作答。

## 四条不可动摇的设计约束

1. **急症永不追问**。追问会引入一轮往返延迟；如果用户已经描述了危险信号
   （迅速增大、破溃出血、伴发热……），必须立刻作答并升级风险等级。
   安全层「宁可保守」的原则在这里的体现是：**宁可少问一句，不可延误一秒**。

2. **只有「个人症状咨询」才追问**。用户问「湿疹怎么护理」「防晒需要每天涂吗」是
   泛泛的知识提问，追问「长在哪个部位」既荒谬又招人烦。判定见 `_is_personal_consultation`。

3. **图片诊断不追问**。视觉分析本身就会产出部位、形态、颜色、分布等维度，
   信息已经比较充分；而且图片诊断的接口契约是「返回一次性诊断卡片」，
   中途插一轮追问会破坏这个体验。

4. **追问有次数上限，用尽即兜底作答**。反复追问却不作答，用户体验比"答得不完美"更差。
   达到上限时转入**带缺口的条件化作答**，并在回答里明确说明缺了什么、为什么重要。

## 维度设计

核心维度（决定「信息是否足够」）：

| key | 标签 | 为什么关键 |
|-----|------|-----------|
| `duration` | 出现时间 | 急性与慢性的处置路径完全不同 |
| `location` | 部位与范围 | 部位是最强的先验之一（如曝光部位 vs 褶皱部位） |
| `sensation` | 伴随感觉 | 是否瘙痒是鉴别的重要线索 |
| `progression` | 变化趋势 | 变化速度直接关系到是否需要尽快就诊 |

补充维度（核心维度基本齐了才可能追问，用于把回答做实）：

`trigger` 诱因 / `history` 既往史 / `treatment` 已处理

## 判定顺序（顺序本身就是优先级，不可随意调整）

```
禁用 / 图片  → 不追问
危险信号     → 不追问（立刻作答并升级）
用户要求直答 → 不追问（进入兜底作答）
非个人咨询   → 不追问（知识问答）
信息已足够   → 不追问
轮次已用尽   → 不追问（进入兜底作答）
其余         → 追问
```

抽取与判定全部是**确定性规则**，不调模型。理由与 `safety_service.py` 一致：
规则可被精确断言，而「问不问」这种分支决策一旦交给模型就变得不可测。
追问文案同样是模板拼装——它天然是结构化的，且零延迟。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

from app.config import Settings, get_settings
from app.core.prompts import RED_FLAG_RULES, URGENT_LEXICON
from app.repositories.db import MetadataStore

# ---------------------------------------------------------------------------
# 维度定义
# ---------------------------------------------------------------------------

CORE_DIMENSIONS: tuple[str, ...] = ("duration", "location", "sensation", "progression")
OPTIONAL_DIMENSIONS: tuple[str, ...] = ("trigger", "history", "treatment")

DIMENSION_META: dict[str, dict[str, str]] = {
    "duration": {
        "label": "出现时间",
        "question": "这个情况大概出现多久了？（例如：3 天、2 周、半年以上）",
        "why": "急性与慢性的处理思路差别很大，是判断轻重缓急的第一依据",
    },
    "location": {
        "label": "部位与范围",
        "question": "具体在哪个部位？范围有多大，是单处还是多处？",
        "why": "不同部位的常见病因并不相同",
    },
    "sensation": {
        "label": "伴随感觉",
        "question": "有没有瘙痒、疼痛、灼热或麻木感？还是完全没有感觉？",
        "why": "是否瘙痒、是否疼痛是重要的鉴别线索",
    },
    "progression": {
        "label": "变化趋势",
        "question": "这段时间是在扩大、增多、加重，还是保持稳定、有所好转？",
        "why": "变化速度关系到是否需要尽快就诊",
    },
    "trigger": {
        "label": "可能诱因",
        "question": "出现前有没有接触新的东西？比如换了护肤品、吃了特定食物、晒过太阳或服用过药物？",
        "why": "找到诱因往往能直接指向病因",
    },
    "history": {
        "label": "既往情况",
        "question": "以前有没有出现过类似情况？是否有过敏史或家族中类似病史？",
        "why": "既往史能提示复发倾向与体质因素",
    },
    "treatment": {
        "label": "已做处理",
        "question": "目前有自行用过什么药或做过什么处理吗？（包括外用药膏）",
        "why": "已用药物会影响皮损表现，也避免重复建议",
    },
}

# ---------------------------------------------------------------------------
# 抽取规则（确定性）
# ---------------------------------------------------------------------------

# 出现时间：数字或中文数词 + 时间单位（「3 天」「三天」「两个月」），
# 或明确的时间指代（「昨天」「上周」）。
# 刻意不含「最近」——它只表达"不久之前"，信息量不足以算作时长。
_CN_NUMERAL = r"[0-9一二三四五六七八九十百千两半几]+"
_DURATION_RE = re.compile(
    r"(?:" + _CN_NUMERAL + r")\s*(?:个)?(?:小时|天|日|周|礼拜|月|年)"
    r"|(?:今天|昨天|前天|上周|上个月|去年|半年前|之前|从小)"
)

# 部位：只保留有鉴别价值的身体部位词。
# 刻意排除「皮肤」「身上」这类过于宽泛的词——「我皮肤痒」等于没说部位。
_LOCATIONS: tuple[str, ...] = (
    "脸", "面部", "面颊", "脸颊", "额头", "下巴", "口周", "嘴唇", "嘴角", "眼睑", "眼睛",
    "鼻子", "耳朵", "头皮", "脖子", "颈部", "后颈", "锁骨",
    "胸口", "胸前", "后背", "背部", "腰", "腹部", "肚子", "躯干",
    "上臂", "前臂", "手臂", "胳膊", "手背", "手心", "手掌", "手指", "手腕", "肘",
    "大腿", "小腿", "膝盖", "腿", "脚背", "脚底", "脚趾", "脚踝", "脚",
    "腋下", "腹股沟", "会阴", "外阴", "臀部", "指甲", "甲周", "黏膜", "口腔",
    "全身", "四肢",
)

_SENSATIONS: tuple[str, ...] = (
    "痒", "瘙痒", "发痒", "不痒", "不痛不痒", "疼", "痛", "刺痛", "疼痛", "压痛",
    "灼热", "烧灼", "麻木", "无感觉", "没感觉", "没有感觉",
)

_PROGRESSIONS: tuple[str, ...] = (
    "扩大", "扩散", "蔓延", "变大", "增多", "变多", "增加", "加重", "更严重", "变严重",
    "好转", "消退", "减轻", "没变化", "没有变化", "没扩大", "稳定", "反复", "持续",
    "一直", "新发", "又长", "又起", "转移",
)

_TRIGGERS: tuple[str, ...] = (
    "换了", "新换", "化妆品", "护肤品", "防晒霜", "洗面奶", "面膜", "吃了", "海鲜",
    "辛辣", "喝酒", "晒太阳", "暴晒", "日晒", "药物", "吃药", "新衣服", "出汗",
    "接触", "宠物", "花粉", "烫发", "染发", "被虫", "蚊虫",
)

_HISTORY: tuple[str, ...] = (
    "以前", "既往", "小时候", "有过", "曾经", "过敏史", "过敏体质", "家族", "遗传",
    "老毛病", "复发", "又犯了", "第二次",
)

_TREATMENTS: tuple[str, ...] = (
    "用过", "涂了", "抹了", "擦了", "敷了", "吃了", "药膏", "激素", "抗过敏",
    "抗组胺", "去医院", "看过医生", "没管", "没处理", "自己买", "自行用药",
)

_EXTRACT_RULES: dict[str, tuple[Any, ...]] = {
    "duration": (_DURATION_RE,),
    "location": _LOCATIONS,
    "sensation": _SENSATIONS,
    "progression": _PROGRESSIONS,
    "trigger": _TRIGGERS,
    "history": _HISTORY,
    "treatment": _TREATMENTS,
}

# 个人咨询的标志：患者自述（含家属代述）才会追问
_PERSONAL_MARKERS: tuple[str, ...] = (
    "我", "本人", "自己", "孩子", "小孩", "宝宝", "儿子", "女儿", "老婆", "老公",
    "家人", "妈妈", "爸爸", "母亲", "父亲", "老人", "娃",
)

# 症状描述词：只有出现这些具体词（或抽到了维度）才认为在「描述病情」。
# 刻意不含「一直」「又」「有点」「变」这类弱词——它们太常出现在非病情语境里，
# 例如「我一直想问你，湿疹会遗传吗」会被误判成症状描述。
_SYMPTOM_VERBS: tuple[str, ...] = (
    "长", "起", "出现", "肿", "破", "脱皮", "掉皮", "流水", "结痂", "溃", "烂",
    "痒", "痛", "疼", "红", "斑", "疹", "痘", "疙瘩", "水疱", "脓",
)

# 泛泛的知识提问句式：即使没有症状动词，也不该追问
_KNOWLEDGE_RE = re.compile(
    r"(怎么|如何|怎样)(护理|预防|治疗|改善|缓解|处理|保养|判断)"
    r"|(有哪些|有什么|都有哪些)(类型|种类|症状|原因|危害|注意|误区|区别|方法|表现)"
    r"|(是什么|啥)(原因|回事|病|情况)"
    r"|会传染吗|传染吗|能根治吗|能治好|多久能好|严重吗|要紧吗"
    r"|怎么(鉴别|区别|区分)"
)

# 用户明确表示不再补充
_SKIP_RE = re.compile(
    r"直接(回答|说|告诉我|给建议)"
    r"|不想(说|补充|回答)|不方便(说|讲)|说不清|不清楚|不知道|不记得|答不上"
    r"|就(这样|按这个|照这个)(回答|说|给)?"
)


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------


@dataclass
class ClarifyDecision:
    """一次完备性判断的结果。"""

    needs_clarification: bool = False
    missing: list[str] = field(default_factory=list)
    collected: dict[str, str] = field(default_factory=dict)
    round: int = 0
    max_rounds: int = 0
    fallback: bool = False
    bypassed_reason: str = ""
    facts_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转成对外契约（供 SSE 事件与接口响应用）。"""
        return {
            "needs_clarification": self.needs_clarification,
            "missing": [DIMENSION_META[key]["label"] for key in self.missing if key in DIMENSION_META],
            "missing_keys": list(self.missing),
            "collected": {
                DIMENSION_META[key]["label"]: value
                for key, value in self.collected.items()
                if key in DIMENSION_META
            },
            "round": self.round,
            "max_rounds": self.max_rounds,
            "fallback": self.fallback,
            "bypassed_reason": self.bypassed_reason,
        }


class ClarifyService:
    """完备性判断 + 追问文案 + 多轮状态管理。"""

    def __init__(self, settings: Settings | None = None, store: MetadataStore | None = None) -> None:
        """初始化。

        Args:
            settings: 全局配置，默认取单例。
            store: 元数据仓储（用于持久化追问进度），默认按配置新建。
        """
        self.settings = settings or get_settings()
        self.store = store or MetadataStore(self.settings.resolved_sqlite_path)

    # ------------------------------------------------------------------
    # 一、维度抽取
    # ------------------------------------------------------------------

    @staticmethod
    def extract(text: str) -> dict[str, str]:
        """从一段文本里抽取已出现的维度及其证据片段。

        Args:
            text: 用户输入。

        Returns:
            ``{维度 key: 命中的原文片段}``；未命中的维度不出现。
        """
        if not text:
            return {}

        found: dict[str, str] = {}
        for key, rules in _EXTRACT_RULES.items():
            for rule in rules:
                if hasattr(rule, "search"):
                    match = rule.search(text)
                    hit = match.group(0) if match else ""
                else:
                    # rule 本身就是候选词。注意这里不能再对它做迭代——
                    # 字符串是可迭代的，会把「护肤品」拆成「护」「肤」「品」，
                    # 于是「湿疹怎么护理」也会命中「护」。必须整词判断。
                    hit = rule if rule in text else ""
                if hit:
                    found[key] = hit.strip()
                    break
        return found

    @staticmethod
    def _compose_facts(collected: dict[str, str]) -> str:
        """把已收集的维度拼成一句可读的病例摘要。

        既用于注入生成提示词（让模型知道已知什么），
        也用于拼检索 query（比单看本轮那句「三天了，很痒」信息量大得多）。
        """
        parts = [
            f"{DIMENSION_META[key]['label']}：{value}"
            for key, value in collected.items()
            if key in DIMENSION_META and value
        ]
        return "；".join(parts)

    # ------------------------------------------------------------------
    # 二、旁路判定
    # ------------------------------------------------------------------

    @staticmethod
    def _has_red_flag(text: str) -> bool:
        """扫描危险信号与紧急词库。

        与 ``safety_service`` 同源（复用 ``RED_FLAG_RULES`` / ``URGENT_LEXICON``），
        保证「追问层认为可以等」和「安全层认为要升级」不会出现矛盾。
        """
        if not text:
            return False
        if any(word in text for word in URGENT_LEXICON):
            return True
        return any(keyword in text for keyword in RED_FLAG_RULES)

    @staticmethod
    def _is_personal_consultation(question: str, collected: dict[str, str]) -> bool:
        """判断这是不是「患者在描述自己的情况」。

        判定顺序：

        1. 命中知识提问句式**且没有症状线索** → 不是（「湿疹怎么护理」）；
        2. 没有自述人称，但抽到了 ≥2 个维度 → 是
           （口语里主语常被省略，「脸上红疹三天了很痒」维度密度足够高）；
        3. 其余看「有自述人称 + 有症状线索」。

        第 1 条必须在第 2、3 条之前：否则「我脸上起红疹怎么护理」也能通过。
        而「没有症状线索」这个附加条件又保住了它——那句话里有「起」和「红疹」，
        属于症状描述，应当追问。
        """
        has_person = any(marker in question for marker in _PERSONAL_MARKERS)
        has_symptom = bool(collected) or any(verb in question for verb in _SYMPTOM_VERBS)

        if _KNOWLEDGE_RE.search(question) and not has_symptom:
            return False
        if not has_person and len(collected) >= 2:
            return True
        return has_person and has_symptom

    # ------------------------------------------------------------------
    # 三、主判定
    # ------------------------------------------------------------------

    def evaluate(
        self,
        session_id: str,
        question: str,
        *,
        has_image: bool = False,
        clarify_pending: bool = False,
    ) -> ClarifyDecision:
        """判断是否需要追问，并维护多轮状态。

        Args:
            session_id: 会话 ID（追问进度按会话持久化）。
            question: 本轮用户输入。
            has_image: 本轮是否带图（带图则跳过追问）。
            clarify_pending: 本轮之前是否处于「追问未完成」状态。

        Returns:
            判定结果；需要追问时状态已落库。
        """
        if not self.settings.clarify_enabled:
            return ClarifyDecision(bypassed_reason="disabled")

        # 图片诊断不追问：视觉结果已提供部位/形态/颜色等维度
        if has_image:
            self.clear(session_id)
            return ClarifyDecision(bypassed_reason="image")

        record = self.load(session_id)
        collected: dict[str, str] = dict(record.get("collected") or {}) if record else {}
        collected.update(self.extract(question))
        rounds_used = int(record.get("rounds") or 0) if record else 0
        facts_text = self._compose_facts(collected)

        # ---- 优先级 1：急症 ----
        # 一旦出现危险信号就立刻作答，绝不因追问延误就医。
        # 注意扫描对象是「本轮输入 + 累计已收集信息」，避免把上一轮提到的危险信号漏掉。
        if self._has_red_flag(question) or self._has_red_flag(facts_text):
            self.clear(session_id)
            logger.info("完备性判断：命中危险信号，跳过追问直接作答")
            return ClarifyDecision(
                collected=collected, facts_text=facts_text, bypassed_reason="urgent"
            )

        missing_core = [key for key in CORE_DIMENSIONS if not collected.get(key)]
        filled_core = len(CORE_DIMENSIONS) - len(missing_core)

        # ---- 优先级 2：用户明确要求直接回答 ----
        # 尊重用户自主权：反复追问一个不想说的人是不礼貌的
        if _SKIP_RE.search(question):
            self.clear(session_id)
            logger.info("完备性判断：用户要求直接回答，转入兜底作答")
            return ClarifyDecision(
                missing=missing_core, collected=collected, facts_text=facts_text,
                fallback=bool(missing_core), bypassed_reason="user_skip",
                max_rounds=self.settings.clarify_max_rounds,
            )

        # ---- 优先级 3：非个人症状咨询（知识提问）----
        # 追问进行中则不再做这个判定——追问后的回复往往很短（「三天了」），
        # 很可能是省略主语的碎片，按知识提问处理会丢掉上下文。
        if not clarify_pending and not self._is_personal_consultation(question, collected):
            self.clear(session_id)
            logger.info("完备性判断：识别为知识提问，跳过追问")
            return ClarifyDecision(
                collected=collected, facts_text=facts_text, bypassed_reason="knowledge_question"
            )

        # ---- 优先级 4：信息已足够 ----
        if filled_core >= self.settings.clarify_min_dimensions:
            self.clear(session_id)
            logger.info("完备性判断：信息已足够（{}/{}）", filled_core, len(CORE_DIMENSIONS))
            return ClarifyDecision(collected=collected, facts_text=facts_text)

        # ---- 优先级 5：轮次用尽 → 兜底作答 ----
        if rounds_used >= self.settings.clarify_max_rounds:
            self.clear(session_id)
            logger.info(
                "完备性判断：追问轮次已用尽（{}/{}），转入兜底作答，缺失 {}",
                rounds_used, self.settings.clarify_max_rounds, missing_core,
            )
            return ClarifyDecision(
                missing=missing_core, collected=collected, facts_text=facts_text,
                round=rounds_used, max_rounds=self.settings.clarify_max_rounds,
                fallback=True, bypassed_reason="round_limit",
            )

        # ---- 默认：追问 ----
        rounds_used += 1
        self.save(session_id, collected, missing_core, rounds_used)
        logger.info(
            "完备性判断：信息不足（{}/{}），发起第 {} 轮追问，缺失 {}",
            filled_core, len(CORE_DIMENSIONS), rounds_used, missing_core,
        )
        return ClarifyDecision(
            needs_clarification=True, missing=missing_core, collected=collected,
            round=rounds_used, max_rounds=self.settings.clarify_max_rounds,
            facts_text=facts_text,
        )

    # ------------------------------------------------------------------
    # 四、文案生成
    # ------------------------------------------------------------------

    def build_question(self, clarify: dict[str, Any]) -> str:
        """把判定结果渲染成结构化的追问文案（确定性模板）。

        追问放在最前面、逐条编号、每条都说明「为什么要问」——
        让用户明白这不是刁难，而是为了给更准的判断。

        Args:
            clarify: ``ClarifyDecision.to_dict()`` 的结果。

        Returns:
            Markdown 格式的追问文本。
        """
        missing_keys: list[str] = list(clarify.get("missing_keys") or [])
        collected: dict[str, Any] = dict(clarify.get("collected") or {})
        round_no = int(clarify.get("round") or 1)
        max_rounds = int(clarify.get("max_rounds") or self.settings.clarify_max_rounds)

        lines: list[str] = ["为了给出更有针对性的建议，还需要了解几项关键信息："]

        if collected:
            known = "；".join(f"{label}：{value}" for label, value in collected.items())
            lines.extend(["", f"**已了解到的信息**：{known}"])

        lines.append("")
        for index, key in enumerate(missing_keys, start=1):
            meta = DIMENSION_META.get(key)
            if not meta:
                continue
            lines.append(f"**{index}. {meta['label']}**")
            lines.append(meta["question"])
            lines.append(f"> 为什么要问：{meta['why']}")
            lines.append("")

        lines.append(f"（第 {round_no}/{max_rounds} 轮追问）")
        lines.append("")
        lines.append(
            "补充这些信息后我可以给出更具体的分析；如果暂时说不清，直接回复「直接回答」也可以，"
            "我会说明信息不足带来的局限。"
        )
        lines.append("")
        lines.append(
            "另外：如果近期出现明显加重、破溃出血、迅速扩大或伴发热，请直接前往正规医院皮肤科就诊，"
            "不必等待补充信息。"
        )

        return "\n".join(lines)

    def build_note(self, decision: ClarifyDecision) -> str:
        """生成注入提示词的「信息完整性提示」。

        Args:
            decision: 判定结果。

        Returns:
            给生成模型的说明；信息完整时返回常规流程的默认说明。
        """
        default = "用户提供的信息基本完整，按常规流程作答。"
        if not decision.fallback or not decision.missing:
            return default

        labels = "、".join(
            DIMENSION_META[key]["label"] for key in decision.missing if key in DIMENSION_META
        )
        known = self._compose_facts(decision.collected) or "（未收集到明确的病情信息）"
        return (
            f"用户连续 {decision.max_rounds} 轮均未补充以下关键信息：{labels}。\n"
            f"已收集到的信息：{known}\n"
            "请在回答中严格遵守：\n"
            f"1. 不要假装知道缺失的信息；对「{labels}」逐项给出「如果……则可能……」的条件化分析；\n"
            f"2. 明确说明缺少「{labels}」会降低判断的精确度，并请用户方便时补充；\n"
            "3. 结尾提醒：若症状加重、出现破溃出血、迅速扩大或伴发热，请及时就医。"
        )

    # ------------------------------------------------------------------
    # 五、多轮状态持久化
    # ------------------------------------------------------------------

    def load(self, session_id: str) -> dict[str, Any] | None:
        """读取某会话的追问进度。"""
        if not session_id:
            return None
        return self.store.get_clarification(session_id)

    def save(
        self,
        session_id: str,
        collected: dict[str, str],
        pending_fields: list[str],
        rounds: int,
    ) -> None:
        """保存追问进度（已收集信息 + 待补充字段 + 已用轮次）。"""
        if not session_id:
            return
        self.store.upsert_clarification(
            session_id=session_id,
            collected=json.dumps(collected, ensure_ascii=False),
            pending_fields=json.dumps(pending_fields, ensure_ascii=False),
            rounds=rounds,
            updated_at=datetime.now().isoformat(timespec="seconds"),
        )

    def clear(self, session_id: str) -> None:
        """清除追问进度（信息补齐、被旁路或兜底作答之后）。"""
        if not session_id:
            return
        self.store.delete_clarification(session_id)

    def has_pending(self, session_id: str) -> bool:
        """该会话是否处于「追问未完成」状态。"""
        record = self.load(session_id)
        return bool(record and record.get("pending_fields"))
