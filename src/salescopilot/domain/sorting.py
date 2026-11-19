"""分拣单的纯规则：出处对齐、要素校验、出库建议、分级兜底。

四条纪律：
- 只做计算：入参是已取好的原文与候选，不查库、不读时钟、不发网络请求；
- 要素能不能进分拣单由这里判，模型说什么不算数；
- `source_span` 必须在原文里真正定位得到，否则该条要素丢弃并给出诊断（编号 + 原因）；
- 出库建议按「先补缺项、再指向约定时间」的固定顺序算；「这次给客户什么」一律取模板
  `todo.values` 的文案，模型写的那一份只留作对照（`model_value_offered`），不用它做决定。

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

# 没有缺项时 `value_offered` 取哪一块的模板文案：与 `domain/todo.py` 同一口径
# （第一个缺项所在分区；一项不缺就取 followup），两处不再各写一套。
OFFERED_FALLBACK_SECTION = "followup"


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


# 能当「新客户名」的字段：只在基础信息里找——需求、金额、时间这些不能当名字。
NEW_LEAD_NAME_FIELDS = ("company", "contact_person", "name")


def suggested_new_name(elements: list[ElementHit]) -> str:
    """从已过出处校验的要素里挑一个「新客户名」建议；挑不到返回空串。

    只认 `card_section=basic` 且 `field` 在 `NEW_LEAD_NAME_FIELDS` 里的**第一条非空取值**：
    真实聊天记录常常只有「某某公司」或「张总」，够认人、就够建客户（0.1.3 放宽新建门槛）。
    这**只是建议**：不落库，人可以在界面上改名后再认领。
    """
    for element in elements:
        if element.card_section != "basic":
            continue
        if element.field not in NEW_LEAD_NAME_FIELDS:
            continue
        value = element.value.strip()
        if value:
            return value
    return ""


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


def build_next_action(
    elements: list[ElementHit],
    missing_sections: list[str],
    *,
    fallback_values: dict[str, str] | None = None,
    model_value_offered: str = "",
) -> dict:
    """按固定顺序给出库建议：先补缺项，没有缺项再指向最近一次约定的时间。

    「这次给客户什么」**只认模板**，要素取值与模型的话都不参与：

    - `value_offered` 取模板 `todo.values` 里对应分区的文案（分区口径与 `domain/todo.py`
      一致）：有缺项时取**最先缺**那一块，一项不缺时取 `OFFERED_FALLBACK_SECTION`；
      模板没有该分区就留空——留空比把日期、金额或客户名塞进「提供什么」这一格诚实，
      前端本来就不显示空建议；
    - `missing_sections` 非空：动作指向最先缺的那一块；
    - 没有缺项：找最后一条时间类字段（`TIME_FIELDS`），`due_hint` 用它；
    - 一条要素都没有时 `action` 与 `value_offered` 都为空，由用例层拒绝生成（这一条不因
      `fallback_values` 而放宽：没有要素的单子本来就没有可出库的依据）。

    `model_value_offered` 是模型自己写的那一份，**原样透传**（只把非字符串收成空串），
    放在 `model_value_offered` 里供审计与对照展示，不参与任何判断——真模型实测会把它
    填成客户名（「王总（华远建设集团，最终签批人）」），正是「让模型决定给什么」的
    必然结果，所以这一格改由规则定。

    `fallback_values` 是模板 `todo.values`（分区 → 文案），由用例层按该客户的模板传入；
    本函数不读模板、不猜文案。返回的字典四键恒在：`action` / `value_offered` /
    `due_hint` / `model_value_offered`。
    """
    offered_by_model = model_value_offered.strip() if isinstance(model_value_offered, str) else ""
    if not elements:
        return _next_action("", "", "", offered_by_model)

    missing = [section for section in missing_sections if section in CARD_SECTIONS]
    if missing:
        section = missing[0]
        return _next_action(
            f"补齐「{SECTION_LABELS.get(section, section)}」要素",
            _fallback_value(fallback_values, section),
            "",
            offered_by_model,
        )

    time_hits = [element for element in elements if element.field in TIME_FIELDS]
    if not time_hits:
        return _next_action(
            "确认下一次联系时间",
            _fallback_value(fallback_values, OFFERED_FALLBACK_SECTION),
            "",
            offered_by_model,
        )

    latest = time_hits[-1]
    return _next_action(
        f"按约定时间（{latest.value}）跟进",
        _fallback_value(fallback_values, OFFERED_FALLBACK_SECTION),
        latest.value,
        offered_by_model,
    )


def _next_action(action: str, value_offered: str, due_hint: str, model_value_offered: str) -> dict:
    """出库建议的固定形状：四个键恒在（前端与用例层只认这一套，不再各拼一份）。"""
    return {
        "action": action,
        "value_offered": value_offered,
        "due_hint": due_hint,
        "model_value_offered": model_value_offered,
    }


def _fallback_value(values: dict[str, str] | None, section: str) -> str:
    """模板里该分区的价值文案：没有模板、没有这一段就返回空串（不拿别的分区凑）。"""
    if not isinstance(values, dict):
        return ""
    text = values.get(section)
    return text if isinstance(text, str) else ""


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
