"""假提炼器：用固定关键词规则产出候选，专供离线测试与演示。

两条纪律：
1. **确定性**：同一段原文永远产出同一批候选（无随机、无时间戳、无外部调用）；
   否则「同一 raw 提炼两次」的幂等测试就证明不了任何东西。
2. **有出处**：每条候选的 `source_span` 都是原文里真实存在的子串（直接取命中片段），
   用例层才能据此判断「没出处的候选该丢」。

行业叫法（称谓、异议说法）只允许出现在适配器层，所以关键词表放在这里，
不放 `domain/` 与 `services/`。`bad_mode=True` 时故意多吐一条没有原文片段的候选，
用来反证「无出处即丢弃」。
"""

import hashlib
import json
import re
from datetime import date, timedelta

from salescopilot.adapters.llm.prompts_sorting import SORTING_PROMPT_VERSION
from salescopilot.domain.matching import Candidate
from salescopilot.domain.models import ExtractionResult, Fact, RawItem, SortingResult

DEFAULT_CONFIDENCE = 0.8

# 写进提炼账本的模型名与提示词版本：假模型固定这两个值，不与真模型混淆。
MODEL_NAME = "fake"
PROMPT_VERSION = "v0"

# 假分拣器把「下周三」算成今天的第几天：固定 7 天，测试里 2026-11-18 → 2026-11-25。
DEFAULT_TODAY = "2026-11-18"
DATE_OFFSET_DAYS = 7

_PRICE_WORDS = ("贵", "价格", "便宜")
_HESITATE_WORDS = ("考虑", "再说", "商量")
_TIME_WORDS = ("下周一", "下周", "明天", "之后")
_ROLE_WORDS = ("校长", "主任", "老板", "家长")
_AMOUNT_WORDS = ("多少钱", "预算", "万", "元")
_AMOUNT_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)?\s*(?:万|元)")


class FakeExtractor:
    """关键词提炼器：命中什么就报什么，命中片段原样作为 `source_span`。"""

    def __init__(self, *, bad_mode: bool = False) -> None:
        self._bad_mode = bad_mode

    def extract(self, *, raw: RawItem, facts: list[Fact]) -> ExtractionResult:
        """按固定规则顺序产出候选；`facts` 只在真模型里当上下文用，假模型不看。"""
        content = raw.content
        candidates: list[dict] = []

        span = _first_hit(content, _PRICE_WORDS)
        if span is not None:
            candidates.append(_candidate("objection", "price", span))

        span = _first_hit(content, _HESITATE_WORDS)
        if span is not None:
            candidates.append(_candidate("need", "待确认决策", span))

        span = _first_hit(content, _TIME_WORDS)
        if span is not None:
            candidates.append(_candidate("appointment_at", span, span))

        span = _first_hit(content, _ROLE_WORDS)
        if span is not None:
            candidates.append(_candidate("decision_maker", span, span))

        amount = _first_amount(content)
        if amount is not None:
            candidates.append(_candidate("amount", amount, amount))

        if self._bad_mode:
            # 故意不带原文片段：用例层必须把它丢掉，并在 skipped 里报出来。
            candidates.append(
                {
                    "field": "need",
                    "value": "无据猜测",
                    "source_span": "",
                    "confidence": DEFAULT_CONFIDENCE,
                }
            )

        return ExtractionResult(
            candidates=candidates,
            model=MODEL_NAME,
            prompt_version=PROMPT_VERSION,
            token=0,
            seconds=0.0,
            raw_output_hash=_payload_hash(raw, candidates),
        )


def _payload_hash(raw: RawItem, candidates: list[dict]) -> str:
    """原文 + 候选 JSON 的稳定哈希：同一段原文永远同一哈希（假模型无需真返回文本）。"""
    payload = (
        raw.content + "\n" + json.dumps(candidates, ensure_ascii=False, sort_keys=True, default=str)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _candidate(field: str, value: str, span: str) -> dict:
    return {
        "field": field,
        "value": value,
        "source_span": span,
        "confidence": DEFAULT_CONFIDENCE,
    }


def _first_hit(content: str, words: tuple[str, ...]) -> str | None:
    """按给定顺序找第一个出现在原文里的词，返回命中的片段本身（即原文子串）。"""
    for word in words:
        if word in content:
            return word
    return None


def _first_amount(content: str) -> str | None:
    """先找形如「2000 元 / 1.5万」的金额片段，找不到再退回金额类关键词。"""
    match = _AMOUNT_PATTERN.search(content)
    if match is not None and match.group(0).strip():
        return match.group(0).strip()
    return _first_hit(content, _AMOUNT_WORDS)


# --------------------------------------------------------------- 分拣判断（T003）

# 句读切分：分拣用的片段以「一句」为单位，切完仍是原文子串，可以直接当出处。
_SENTENCE_SPLIT = re.compile(r"[，。；！？\n]")

# 组织形式：与 `domain/matching.py` 的后缀表同口径（这里只用来切出名字片段）。
_ORG_SPAN = re.compile(
    r"[\u4e00-\u9fa5A-Za-z0-9()（）]{2,20}?(?:股份有限公司|有限责任公司|有限公司|集团|公司)"
)

_SORTING_NEED_WORDS = ("预算", "价格", "需求", "贵", "便宜", "考虑")
_SORTING_ROLE_WORDS = ("老板", "负责人", "经理", "主任", "总监")
_SORTING_TIME_WORDS = ("下周", "明天", "之后", "下个月", "周一", "周二", "周三")

# 相对日期说法：非 ISO 用例（`bad_date=True`）要把「下周三」这种原话原样还给用例层。
_TIME_PHRASE = re.compile(r"下(?:周[一二三四五六日天]?|个月)|明天|后天|之后")


class FakeSorting:
    """关键词分拣器：按固定规则产出 2–4 条要素 + 一个分级 + 一个出库建议。

    - 每条要素的 `source_span` 都是原文里真实存在的**整句**（≥4 字），
      用例层因此能把它们一条不落地对齐回去；
    - 时间类字段的 `value` 是**确定的 ISO 日期**：`today + 7 天`
      （默认 2026-11-18 → 2026-11-25），`source_span` 仍是原文那句「下周三…」；
    - `bad_span=True` 时故意多吐一条原文里根本没有的要素，用来反证「编造的出处必被丢弃」；
    - `bad_date=True` 时时间类字段故意吐原文里的「下周三」，用来反证「非 ISO 日期必被丢弃」；
    - `calls` / `review_calls` 是调用计数：`review_calls` 只在**归属灰区**（调用方传了
      候选）时加一，用来断言「精确键命中时模型没被问过归属」。
    """

    def __init__(
        self, *, bad_span: bool = False, bad_date: bool = False, today: str = DEFAULT_TODAY
    ) -> None:
        self._bad_span = bad_span
        self._bad_date = bad_date
        self._today = today
        self.calls = 0
        self.review_calls = 0
        self.last_candidates: list[Candidate] = []

    def judge(self, *, content: str, candidates: list[Candidate], today: str = "") -> SortingResult:
        """`today` 由调用方（用例层）从时钟取；没传就用构造参数（保证离线可断言）。"""
        self.calls += 1
        if candidates:
            self.review_calls += 1
            self.last_candidates = list(candidates)

        elements = _sorting_elements(content, today=today or self._today, bad_date=self._bad_date)
        if self._bad_span:
            # 故意引用一句原文里没有的话：用例层必须丢掉它并在 dropped 里报出来。
            elements.append(
                {
                    "card_section": "decision",
                    "field": "approval_flow",
                    "value": "需要总部集中审批",
                    "source_span": "客户说下个月总部一定会批",
                    "confidence": 0.95,
                }
            )

        payload = {
            "elements": elements,
            "grading": _sorting_grading(elements),
            "next_action": _sorting_next_action(elements),
        }
        return SortingResult(
            payload=payload,
            raw_text=json.dumps(payload, ensure_ascii=False),
            model=MODEL_NAME,
            prompt_version=SORTING_PROMPT_VERSION,
            token=0,
            seconds=0.0,
        )

    def review(self, *, content: str, candidates: list[Candidate]) -> tuple[str | None, str]:
        """灰区复核的假实现：取相似度最高的候选，理由写清「谁更像、像多少」。

        确定性来自候选顺序（调用方已按分数倒序排好），同一批候选永远同一结论；
        没有候选时明说挑不出来，不编一个客户。
        """
        self.review_calls += 1
        self.last_candidates = list(candidates)
        if not candidates:
            return None, "没有可复核的候选"
        best = candidates[0]
        return best.lead_id, f"候选里「{best.name}」最接近（相似度 {best.score:.1f}），建议归给它"


def _sorting_elements(
    content: str, *, today: str = DEFAULT_TODAY, bad_date: bool = False
) -> list[dict]:
    """按固定顺序抽要素：基础信息 → 需求 → 决策 → 跟进；抽不到就不给这一条。

    时间类字段（`appointment_at`）的取值固定是 `today + 7 天` 的 ISO 日期，
    `source_span` 仍是原文那一句；`bad_date=True` 时故意把原话当取值（非 ISO）。
    """
    elements: list[dict] = []

    org = _first_sentence_match(content, _ORG_SPAN)
    if org is not None:
        elements.append(_element("basic", "company", org, org, 0.9))

    need = _first_sentence_with(content, _SORTING_NEED_WORDS)
    if need is not None:
        elements.append(_element("needs", "need", need, need, DEFAULT_CONFIDENCE))

    role_segment = _first_sentence_with(content, _SORTING_ROLE_WORDS)
    if role_segment is not None:
        role = next(word for word in _SORTING_ROLE_WORDS if word in role_segment)
        elements.append(
            _element("decision", "decision_maker", role, role_segment, DEFAULT_CONFIDENCE)
        )

    time_segment = _first_sentence_with(content, _SORTING_TIME_WORDS)
    if time_segment is not None:
        when = _time_phrase(time_segment) or next(
            word for word in _SORTING_TIME_WORDS if word in time_segment
        )
        value = when if bad_date else (_iso_after(today, DATE_OFFSET_DAYS) or when)
        elements.append(
            _element("followup", "appointment_at", value, time_segment, DEFAULT_CONFIDENCE)
        )

    return elements


def _time_phrase(text: str) -> str:
    """原文里最长的相对日期说法（「下周三」优先于「下周」）：非 ISO 用例拿它当原话取值。"""
    found = [match.group(0) for match in _TIME_PHRASE.finditer(text)]
    return max(found, key=len) if found else ""


def _iso_after(today: str, days: int) -> str:
    """`today + days` 的 ISO 日期；`today` 不是 `YYYY-MM-DD` 时返回空串（不猜日期）。"""
    try:
        base = date.fromisoformat(today.strip() if isinstance(today, str) else "")
    except ValueError:
        return ""
    return (base + timedelta(days=days)).isoformat()


def _first_sentence_with(content: str, words: tuple[str, ...]) -> str | None:
    """第一个「含这些词」且够长的句子（原文子串）；找不到返回 None。"""
    for sentence in _SENTENCE_SPLIT.split(content):
        text = sentence.strip()
        if len(text) >= 4 and any(word in text for word in words):
            return text
    return None


def _first_sentence_match(content: str, pattern: re.Pattern[str]) -> str | None:
    """按句切开后，第一个能匹配 `pattern` 的片段（避免把主语一起吞进名字）。"""
    for sentence in _SENTENCE_SPLIT.split(content):
        match = pattern.search(sentence.strip())
        if match is not None:
            return match.group(0)
    return None


def _sorting_grading(elements: list[dict]) -> dict:
    """按要素覆盖度给 A/B/C：需求 + 决策都有 → A；只有需求 → B；其余 → C。"""
    sections = {element["card_section"] for element in elements}
    if "needs" in sections and "decision" in sections:
        value, reason = "A", "需求与决策两块都有原文依据"
    elif "needs" in sections:
        value, reason = "B", "只有需求依据，决策链还没落实"
    else:
        value, reason = "C", "要素太少，先把基础信息补齐"
    return {
        "value": value,
        "reason": reason,
        "basis": [element["source_span"] for element in elements[:2]],
    }


def _sorting_next_action(elements: list[dict]) -> dict:
    """给一条出库建议（模型侧的那份；用例层会按缺项规则重算，这里只需形状合法）。"""
    sections = {element["card_section"] for element in elements}
    if "decision" in sections:
        action = "按约定时间再确认一次决策流程"
    else:
        action = "补问谁拍板、钱从哪出"
    return {
        "action": action,
        "value_offered": elements[0]["value"] if elements else "",
        "due_hint": next(
            (element["value"] for element in elements if element["card_section"] == "followup"),
            "",
        ),
    }


def _element(section: str, field: str, value: str, span: str, confidence: float) -> dict:
    return {
        "card_section": section,
        "field": field,
        "value": value,
        "source_span": span,
        "confidence": confidence,
    }
