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

from salescopilot.domain.models import ExtractionResult, Fact, RawItem

DEFAULT_CONFIDENCE = 0.8

# 写进提炼账本的模型名与提示词版本：假模型固定这两个值，不与真模型混淆。
MODEL_NAME = "fake"
PROMPT_VERSION = "v0"

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
