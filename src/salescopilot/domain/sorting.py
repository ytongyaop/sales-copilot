"""分拣单的纯规则：出处对齐、要素校验、出库建议、分级兜底。

四条纪律：
- 只做计算：入参是已取好的原文与候选，不查库、不读时钟、不发网络请求；
- 要素能不能进分拣单由这里判，模型说什么不算数；
- `source_span` 必须在原文里真正定位得到，否则该条要素丢弃并给出诊断（编号 + 原因）；
- 出库建议按「先补缺项、再指向约定时间」的固定顺序算；模型给的那一份只留在原始 JSON
  里供审计，不用它做决定。

行业叫法不出现在这里：本模块只认 `CARD_SECTIONS` 与通用字段名。
"""

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from salescopilot.domain.matching import (
    ORG_SUFFIXES,
    Candidate,
    normalize_org,
    normalize_with_alias,
)
from salescopilot.domain.models import CARD_SECTIONS
from salescopilot.domain.rules import ValidationIssue

# 出处片段至少这么长：太短的串（一两个字）在原文里到处都能模糊命中，当不了出处。
MIN_SPAN_LEN = 4

# 模糊对齐的接受下限（0-100）：错一个字（约 90 分）可以接受，错得更多就不认。
DEFAULT_SPAN_MIN_RATIO = 90.0

# 四块的中文名：只用于出库建议的措辞，不参与任何判断。
SECTION_LABELS = {
    "basic": "基础信息",
    "needs": "需求与痛点",
    "decision": "决策流程与预算",
    "followup": "跟进记录",
}

# 时间类字段：出库建议「指向最近一次约定的时间」时按这些字段名找原话。
TIME_FIELDS = ("appointment_at", "next_followup_at", "appointment", "due_at")

# 联系方式精确键：手机号、邮箱、以及 8 位以上的数字串（座机、账号等）。
_PHONE = re.compile(r"1[3-9]\d{9}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_DIGITS = re.compile(r"\d+")

# 从原文抓候选全称：一段名字字符 + 组织形式后缀；左侧会更长，靠截断还原干净的名字。
_ORG_RUN = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9()（）]{2,20}?(?:" + "|".join(ORG_SUFFIXES) + r")")

# 候选全称左侧最多截这么多个字：够覆盖「客户说」「我们联系了」这类前缀。
_MAX_TRUNCATE = 10

# 诊断消息的固定前缀：`elements[3]：原因`，上层据此把下标还原成 `dropped`。
_DIAGNOSTIC = re.compile(r"^elements\[(\d+)\]：(.*)$")


@dataclass(frozen=True)
class ElementHit:
    """一条过关的要素：归哪一块、哪个字段、取值、原文出处与置信度。

    `source_span` 一定是原文里定位得到的片段（精确命中或模糊对齐通过），
    所以前端可以直接在原文里高亮它。
    """

    card_section: str
    field: str
    value: str
    source_span: str
    confidence: float


@dataclass(frozen=True)
class SortingSuggestion:
    """一张分拣单的建议本体：归属候选 + 要素 + 分级 + 出库建议。

    `lead_candidates` 是归属漏斗给出的候选（灰区才有内容，最多 3 条），
    本增量只把它交给模型做参考、不改归属；分级与出库建议都是已校验过的版本。
    """

    lead_candidates: list[Candidate]
    elements: list[ElementHit]
    grading: dict
    next_action: dict


def align_span(
    raw_text: str, quote: str, *, min_ratio: float = DEFAULT_SPAN_MIN_RATIO
) -> tuple[int, int] | None:
    """把模型给的片段定位回原文，返回 `(start, end)`；定不到返回 `None`。

    顺序固定：
    1. 精确子串命中（`str.find`）—— 最常见的路径，快且不会误判；
    2. 失败再模糊对齐（`rapidfuzz` 的部分比对齐），分数 ≥ `min_ratio` 才接受；
    3. 仍失败就是 `None`，调用方据此丢弃该字段。

    短于 `MIN_SPAN_LEN` 的片段一律不接受（先于第 1 步判断）：一两个字的串在原文里
    到处都是，模糊命中率 100 也证明不了「这句话出自这里」。
    """
    text = raw_text if isinstance(raw_text, str) else ""
    span = quote.strip() if isinstance(quote, str) else ""
    if not text or len(span) < MIN_SPAN_LEN:
        return None

    start = text.find(span)
    if start >= 0:
        return start, start + len(span)

    alignment = fuzz.partial_ratio_alignment(span, text)
    if alignment is None or alignment.score < min_ratio:
        return None
    if alignment.dest_end <= alignment.dest_start:
        return None
    return alignment.dest_start, alignment.dest_end


def validate_elements(raw_text: str, hits: list[dict]) -> tuple[list[ElementHit], list[str]]:
    """逐条校验模型吐的要素：过关的留下，不过关的转成一条诊断（不抛异常）。

    逐条判据（任一不满足就丢弃这一条）：
    1. `card_section` 必须在 `CARD_SECTIONS` 里；
    2. `field` / `value` / `source_span` 都必须是非空字符串；
    3. `source_span` 必须能用 `align_span` 在原文里定位到。

    诊断消息形如 `elements[2]：出处片段在原文里找不到（…）`，编号是**模型输出里的下标**
    （不是留下的条数），上层据此把丢弃项还原成 `{"index", "reason"}`。
    """
    kept: list[ElementHit] = []
    diagnostics: list[str] = []
    items = hits if isinstance(hits, list) else []

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            diagnostics.append(f"elements[{index}]：要素必须是对象")
            continue

        section = _text(item.get("card_section")).strip().lower()
        field = _text(item.get("field")).strip()
        value = _text(item.get("value")).strip()
        span = _text(item.get("source_span")).strip()

        if section not in CARD_SECTIONS:
            allowed = "/".join(CARD_SECTIONS)
            diagnostics.append(
                f"elements[{index}]：card_section 必须是 {allowed} 之一（实际是 {section!r}）"
            )
            continue
        if not field or not value or not span:
            diagnostics.append(f"elements[{index}]：field / value / source_span 都不能为空")
            continue

        aligned = align_span(raw_text, span)
        if aligned is None:
            diagnostics.append(f"elements[{index}]：出处片段在原文里找不到（{span}）")
            continue

        kept.append(
            ElementHit(
                card_section=section,
                field=field,
                value=value,
                source_span=raw_text[aligned[0] : aligned[1]],
                confidence=_confidence(item.get("confidence")),
            )
        )

    return kept, diagnostics


def split_diagnostic(message: str) -> tuple[int | None, str]:
    """把 `elements[2]：原因` 拆回（下标, 原因）；形状不对时下标为 None。

    只给上层拼 `dropped` 用，不参与任何判断。
    """
    match = _DIAGNOSTIC.match(message if isinstance(message, str) else "")
    if match is None:
        return None, message
    return int(match.group(1)), match.group(2)


def contact_from_elements(elements: list[ElementHit]) -> str:
    """从已过出处校验的要素里取联系方式：先认 `field='contact'`，再认取值里的手机号 / 邮箱。

    只从要素里取（不回头翻原文）：人确认的是这些要素，新客户资料也只该按它们填。
    多个候选时取**最小那个**（字符串序），保证同一批要素每次得到同一个联系方式。
    """
    for element in elements:
        if element.field == "contact" and element.value.strip():
            return element.value.strip()
    for element in elements:
        tokens = contact_tokens(element.value)
        if tokens:
            return min(tokens)
    return ""


# 拒绝分拣单必须给理由：至少 2 个字。一个字的「滚」说不清为什么，复核时等于没写。
MIN_DECISION_REASON_LEN = 2


def validate_decision_reason(action: str, reason: str | None) -> list[ValidationIssue]:
    """校验处置理由：只有 `reject` 强制要理由，其余动作一律返回空列表。"""
    if action != "reject":
        return []
    text = reason.strip() if isinstance(reason, str) else ""
    if not text:
        return [ValidationIssue("reason", "拒绝分拣单必须写明理由")]
    if len(text) < MIN_DECISION_REASON_LEN:
        return [ValidationIssue("reason", f"理由至少需要 {MIN_DECISION_REASON_LEN} 个字符")]
    return []


def build_next_action(elements: list[ElementHit], missing_sections: list[str]) -> dict:
    """按固定顺序给出库建议：先补缺项，没有缺项再指向最近一次约定的时间。

    - `missing_sections` 非空：动作指向**最先缺**的那一块，`value_offered` 取该块里
      置信度最高的一条要素（该块一条都没有时取全局最高），人一眼知道「补它之前手上
      已经有什么」；
    - 没有缺项：找最后一条时间类字段（`TIME_FIELDS`），`due_hint` 与 `value_offered`
      都用它；一条都没有就退成「确认下一次联系时间」；
    - `value_offered` 为空（没有任何要素可用）时 `action` 置空，由用例层拒绝生成。

    返回的字典三键恒在：`action` / `value_offered` / `due_hint`。
    """
    missing = [section for section in missing_sections if section in CARD_SECTIONS]
    if missing:
        section = missing[0]
        offered = _best_value(elements, section)
        if not offered:
            return {"action": "", "value_offered": "", "due_hint": ""}
        return {
            "action": f"补齐「{SECTION_LABELS.get(section, section)}」要素",
            "value_offered": offered,
            "due_hint": "",
        }

    time_hits = [element for element in elements if element.field in TIME_FIELDS]
    if not time_hits:
        offered = _best_value(elements, None)
        if not offered:
            return {"action": "", "value_offered": "", "due_hint": ""}
        return {"action": "确认下一次联系时间", "value_offered": offered, "due_hint": ""}

    latest = time_hits[-1]
    return {
        "action": f"按约定时间（{latest.value}）跟进",
        "value_offered": latest.value,
        "due_hint": latest.value,
    }


def contact_tokens(text: str) -> set[str]:
    """抽出可与联系方式比对的精确键：手机号、邮箱、8 位以上数字串（统一小写）。

    只做「形式抽取」，不比大小、不做模糊判断——精确键命中要求**完全相等**，
    与 `domain/matching.py` 的 L0 口径一致。
    """
    value = _text(text)
    tokens = set(_PHONE.findall(value))
    tokens.update(email.lower() for email in _EMAIL.findall(value))
    tokens.update(part for part in _DIGITS.findall(value) if len(part) >= 8)
    return tokens


def org_name_candidates(text: str) -> list[str]:
    """从原文按组织形式后缀抓候选全称，返回去重后的原样串（保持出现顺序）。

    做法：找到每个「名字字符 + 后缀」的连续片段，再把左侧前缀按 2–10 个字截断，
    生成一组可能的全称。原文里常带主语（「客户说华远建设工程有限公司…」），
    整段当名字会拉低相似度，截断后才还原得出干净的全称。

    只按后缀（`ORG_SUFFIXES`）识别，不认识任何行业词；没有后缀的短称靠别名表补。
    """
    value = _text(text)
    names: list[str] = []
    for match in _ORG_RUN.finditer(value):
        full = match.group(0)
        suffix = _match_suffix(full)
        body = full[: -len(suffix)]
        for size in range(2, _MAX_TRUNCATE + 1):
            if len(body) < size:
                break
            names.append(body[-size:] + suffix)
        names.append(full)
    return list(dict.fromkeys(names))


def _match_suffix(full: str) -> str:
    """这条候选以哪个后缀结尾；`ORG_SUFFIXES` 从长到短排列，取第一个命中的。"""
    for suffix in ORG_SUFFIXES:
        if full.endswith(suffix):
            return suffix
    return ""


def _best_value(elements: list[ElementHit], section: str | None) -> str:
    """取某一块里置信度最高的取值；该块没有要素时取全局最高；都没有时返回空串。"""
    scoped = [element for element in elements if section is None or element.card_section == section]
    if not scoped:
        scoped = list(elements)
    if not scoped:
        return ""
    return max(scoped, key=lambda element: element.confidence).value


def _confidence(value: object) -> float:
    """置信度统一到 0-1：非数值按 0 处理，越界截断（模型偶尔按百分制给）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return min(max(float(value), 0.0), 1.0)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def normalized_org(name: str, aliases: dict[str, str] | None = None) -> str:
    """机构名归一（可带别名表）：别名表非空时先查别名再规范化，与原口径一致。"""
    if not aliases:
        return normalize_org(name)
    return normalize_with_alias(name, aliases)
