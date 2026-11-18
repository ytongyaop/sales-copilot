"""分级的纯规则：四维打分 → A/B/C/needs_info ＋ 理由 ＋ 依据（ADR-0008）。

三条纪律：
- 只做计算：入参是模板 `grading` 段与已落库的事实，不查库、不读时钟、不发网络请求
  （`today` 由调用方传入，本模块自己不看日历）；
- **分档只由规则产出**：模型不参与打分，也不再给「置信度」；同一批事实永远同一档；
- 权重与分档阈值**只来自模板**（`grading.weights` / `grading.bands`），代码里不写死数字。

打分口径（四维，各维 0–1，乘权重后相加）：
- `amount` 成交金额：≥50 万 → 1.0；≥10 万 → 0.7；>0 → 0.4（解析不了 → 未知）；
- `urgency` 紧迫度：距今天 ≤7 天 → 1.0；≤30 天 → 0.6；其余 → 0.3（没有约定时间 → 未知）；
- `clarity` 决策清晰度：决策者与决策流程都有 → 1.0；只有一项 → 0.5；都没有 → 未知；
- `fit` 需求匹配度：我方匹配点 → 1.0；只有核心需求 → 0.5；都没有 → 未知。

未知维度不猜：把它们当 0 算下界、按满权重算上界。上下界都落在同一档才给档；
跨档就是 `needs_info`（缺的那一项可能改变结论，人补一句比机器编一句强）。
A 档额外要求**四维齐全**——四缺一的「满分」不是满分。
"""

import re
from dataclasses import dataclass
from datetime import date

from salescopilot.domain.models import Fact

# 分档取值：三档 ＋ 「待补全」。`GRADE_VALUES` 只含三档，人工定档只能选这三个。
GRADE_A = "A"
GRADE_B = "B"
GRADE_C = "C"
GRADE_NEEDS_INFO = "needs_info"
GRADE_VALUES: tuple[str, ...] = (GRADE_A, GRADE_B, GRADE_C)

# 四个维度及其展示名：键名与模板 `grading.weights` 一一对应（顺序即展示顺序）。
DIM_AMOUNT = "amount"
DIM_URGENCY = "urgency"
DIM_CLARITY = "clarity"
DIM_FIT = "fit"
DIM_ORDER: tuple[str, ...] = (DIM_AMOUNT, DIM_URGENCY, DIM_CLARITY, DIM_FIT)
DIM_LABELS: dict[str, str] = {
    DIM_AMOUNT: "成交金额",
    DIM_URGENCY: "紧迫度",
    DIM_CLARITY: "决策清晰度",
    DIM_FIT: "需求匹配度",
}
# 理由里用的短名：一句 `金额 0.7 / 紧迫 1.0 / 决策 0.5 / 匹配 1.0` 读起来才不啰嗦。
DIM_SHORT_LABELS: dict[str, str] = {
    DIM_AMOUNT: "金额",
    DIM_URGENCY: "紧迫",
    DIM_CLARITY: "决策",
    DIM_FIT: "匹配",
}

# 模板 `grading` 段必须给的键：权重四项、分档两个阈值、默认模式（模板校验与打分共用）。
GRADING_WEIGHT_KEYS: tuple[str, ...] = DIM_ORDER
GRADING_BAND_KEYS: tuple[str, ...] = ("a_min", "b_min")
GRADING_REQUIRED_KEYS: tuple[str, ...] = ("weights", "bands", "default_mode")

# 权重之和的容差：模板里写小数，浮点相加不会精确等于 1，差 0.001 以内算合法。
WEIGHT_SUM_TOLERANCE = 0.001

# 金额分档（单位：元）。50 万、10 万是销售侧通行的「大单 / 中单」分界，
# 改口径就改这里，并由模板权重决定它对总分的影响。
AMOUNT_HIGH_YUAN = 500_000
AMOUNT_MID_YUAN = 100_000
AMOUNT_HIGH_SCORE = 1.0
AMOUNT_MID_SCORE = 0.7
AMOUNT_LOW_SCORE = 0.4

# 紧迫度分档（距今天的天数）：一周内要见、一个月内要见、更远。
URGENCY_SOON_DAYS = 7
URGENCY_MID_DAYS = 30
URGENCY_SOON_SCORE = 1.0
URGENCY_MID_SCORE = 0.6
URGENCY_FAR_SCORE = 0.3

# 决策清晰度：两项都齐 1.0，只有一项 0.5（缺的那项仍是空白，不能当满分）。
CLARITY_FULL_SCORE = 1.0
CLARITY_PARTIAL_SCORE = 0.5

# 需求匹配度：我方匹配点 1.0，只有核心需求 0.5（知道他要什么，还不知道我们对得上）。
FIT_FULL_SCORE = 1.0
FIT_PARTIAL_SCORE = 0.5

# 参与每一维打分的事实字段：依据（basis）按这张表回收原文片段。
DIM_FACT_FIELDS: dict[str, tuple[str, ...]] = {
    DIM_AMOUNT: ("amount",),
    DIM_URGENCY: ("appointment_at",),
    DIM_CLARITY: ("decision_maker", "approval_flow"),
    DIM_FIT: ("our_fit", "need"),
}

# 依据最多回几条原文片段：够人复核，又不至于把整段原文塞进分拣单。
MAX_BASIS = 6

# 金额写法：数字 + 可选「万 / w」。
_NUMERIC_AMOUNT = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(万|w)?$")
# 金额里可以直接忽略的装饰：货币符号、千分位、空白、「元 / 块」（`人民币` 单独替换）。
_AMOUNT_NOISE = re.compile(r"[,\s¥￥元块]")
_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_SECTIONS = {"万": 10_000, "亿": 100_000_000}


@dataclass(frozen=True)
class DimScore:
    """一个维度的打分结果：权重来自模板，`known=False` 时 `score` 不参与计算。"""

    key: str
    label: str
    weight: float
    known: bool
    score: float


@dataclass(frozen=True)
class GradingResult:
    """一次分级的结果：档次 ＋ 分数区间 ＋ 四维明细 ＋ 中文理由 ＋ 原文依据。

    - `low` / `high`：已知维度按实得分、未知维度按 0 / 满分算出的下界与上界；
      四维齐全时两者相等；
    - `reason`：一句话说明「为什么是这个档」或「为什么待补全」；
    - `basis`：参与打分的原文片段（去重，最多 `MAX_BASIS` 条），能回到事实的出处。
    """

    value: str
    low: float
    high: float
    dims: list[DimScore]
    reason: str
    basis: list[str]


def grading_spec(spec: dict) -> dict:
    """从模板里取出 `grading` 段；缺段或段不是映射就抛 `KeyError`（模板校验已保证不会）。"""
    grading = spec.get("grading") if isinstance(spec, dict) else None
    if not isinstance(grading, dict):
        raise KeyError("模板缺少 grading 段")
    weights = grading.get("weights")
    bands = grading.get("bands")
    if not isinstance(weights, dict) or not isinstance(bands, dict):
        raise KeyError("模板 grading 段缺少 weights / bands")
    return grading


def grade_from_facts(spec: dict, facts: list[Fact], *, today: date) -> GradingResult:
    """按模板权重与分档阈值，把该客户的事实打成 A / B / C / needs_info。

    `today` 由调用方传（用例层从注入的时钟取日期后传进来），本函数不看系统时钟。
    """
    grading = grading_spec(spec)
    weights = {key: float(grading["weights"][key]) for key in GRADING_WEIGHT_KEYS}
    a_min = float(grading["bands"]["a_min"])
    b_min = float(grading["bands"]["b_min"])

    by_field = _latest_by_field(facts)
    dims = [
        _amount_dim(by_field, weights[DIM_AMOUNT]),
        _urgency_dim(by_field, weights[DIM_URGENCY], today),
        _clarity_dim(by_field, weights[DIM_CLARITY]),
        _fit_dim(by_field, weights[DIM_FIT]),
    ]

    low = round(sum(dim.score * dim.weight for dim in dims if dim.known), 6)
    high = round(low + sum(dim.weight for dim in dims if not dim.known), 6)
    value = _band_of(dims, low, high, a_min=a_min, b_min=b_min)

    return GradingResult(
        value=value,
        low=low,
        high=high,
        dims=dims,
        reason=_reason(
            dims,
            low,
            high,
            a_min=a_min,
            b_min=b_min,
            value=value,
            notes=_parse_notes(by_field),
        ),
        basis=_basis(dims, by_field),
    )


def parse_amount(text: str) -> float | None:
    """把常见金额写法解析成「元」；解析不了返回 `None`（不猜、不当 0）。

    支持的写法：`800000` / `80万` / `80w` / `1.5万` / `八十万` / `三十万` /
    `一百二十万` / `10,000` / `¥800000` / `三十万元`。其余（如 `abc`、`约三十万`）返回 None。
    """
    raw = text.strip() if isinstance(text, str) else ""
    if not raw:
        return None
    cleaned = _AMOUNT_NOISE.sub("", raw.replace("人民币", ""))
    if not cleaned:
        return None

    match = _NUMERIC_AMOUNT.match(cleaned)
    if match is not None:
        number = float(match.group(1))
        return number * 10_000 if match.group(2) else number

    chinese = _parse_chinese_number(cleaned)
    return chinese


def parse_iso_date(text: str) -> date | None:
    """把 `YYYY-MM-DD` 解析成日期；不是这个形状或日历上不存在就返回 None。"""
    value = text.strip() if isinstance(text, str) else ""
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


# --------------------------------------------------------------------- 四维打分


def _latest_by_field(facts: list[Fact]) -> dict[str, Fact]:
    """同一字段只留最后一条（仓储口径是一个字段一条当前事实，这里再兜一次）。"""
    by_field: dict[str, Fact] = {}
    for fact in facts:
        if isinstance(fact, Fact):
            by_field[fact.field] = fact
    return by_field


def _amount_dim(by_field: dict[str, Fact], weight: float) -> DimScore:
    fact = by_field.get("amount")
    amount = parse_amount(fact.value) if fact is not None else None
    if amount is None or amount <= 0:
        return _unknown(DIM_AMOUNT, weight)
    if amount >= AMOUNT_HIGH_YUAN:
        return _known(DIM_AMOUNT, weight, AMOUNT_HIGH_SCORE)
    if amount >= AMOUNT_MID_YUAN:
        return _known(DIM_AMOUNT, weight, AMOUNT_MID_SCORE)
    return _known(DIM_AMOUNT, weight, AMOUNT_LOW_SCORE)


def _urgency_dim(by_field: dict[str, Fact], weight: float, today: date) -> DimScore:
    fact = by_field.get("appointment_at")
    when = parse_iso_date(fact.value) if fact is not None else None
    if when is None:
        return _unknown(DIM_URGENCY, weight)
    # 已经过去的约定时间同样算「很急」：那是欠着的约，不该被当成远期机会。
    days = (when - today).days
    if days <= URGENCY_SOON_DAYS:
        return _known(DIM_URGENCY, weight, URGENCY_SOON_SCORE)
    if days <= URGENCY_MID_DAYS:
        return _known(DIM_URGENCY, weight, URGENCY_MID_SCORE)
    return _known(DIM_URGENCY, weight, URGENCY_FAR_SCORE)


def _clarity_dim(by_field: dict[str, Fact], weight: float) -> DimScore:
    decision_maker = _filled(by_field, "decision_maker")
    approval_flow = _filled(by_field, "approval_flow")
    if decision_maker and approval_flow:
        return _known(DIM_CLARITY, weight, CLARITY_FULL_SCORE)
    if decision_maker or approval_flow:
        return _known(DIM_CLARITY, weight, CLARITY_PARTIAL_SCORE)
    return _unknown(DIM_CLARITY, weight)


def _fit_dim(by_field: dict[str, Fact], weight: float) -> DimScore:
    if _filled(by_field, "our_fit"):
        return _known(DIM_FIT, weight, FIT_FULL_SCORE)
    if _filled(by_field, "need"):
        return _known(DIM_FIT, weight, FIT_PARTIAL_SCORE)
    return _unknown(DIM_FIT, weight)


def _filled(by_field: dict[str, Fact], field: str) -> bool:
    fact = by_field.get(field)
    return fact is not None and bool(fact.value.strip())


def _known(key: str, weight: float, score: float) -> DimScore:
    return DimScore(key=key, label=DIM_LABELS[key], weight=weight, known=True, score=score)


def _unknown(key: str, weight: float) -> DimScore:
    """未知维度：分数记 0，权重仍保留（上界计算要用）。"""
    return DimScore(key=key, label=DIM_LABELS[key], weight=weight, known=False, score=0.0)


# ------------------------------------------------------------------ 上下界与分档


def _band_of(dims: list[DimScore], low: float, high: float, *, a_min: float, b_min: float) -> str:
    """按上下界定档，判断顺序即优先级（先看「怎么都到不了 B」，再看 A / B / 跨档）。"""
    if high < b_min:
        return GRADE_C
    if low >= a_min and all(dim.known for dim in dims):
        return GRADE_A
    if low >= b_min and high < a_min:
        return GRADE_B
    return GRADE_NEEDS_INFO


def _reason(
    dims: list[DimScore],
    low: float,
    high: float,
    *,
    a_min: float,
    b_min: float,
    value: str,
    notes: list[str],
) -> str:
    """拼一句人能直接读的理由：先说缺不缺，再说分数区间与档位门槛。"""
    unknown = [dim for dim in dims if not dim.known]
    if unknown:
        head = "缺" + "、".join(f"「{dim.label}」" for dim in unknown)
        score_text = f"总分区间 {low:.2f}–{high:.2f}"
    else:
        head = (
            "四维齐全（"
            + " / ".join(f"{DIM_SHORT_LABELS[dim.key]} {dim.score:.1f}" for dim in dims)
            + "）"
        )
        score_text = f"加权 {low:.2f}"
    if value == GRADE_A:
        tail = f"加权 {low:.2f} ≥ {a_min:.2f} → A"
    elif value == GRADE_C:
        tail = f"{score_text} 低于 B 档下限 {b_min:.2f} → C"
    elif value == GRADE_B:
        tail = f"{score_text} 落在 {b_min:.2f}–{a_min:.2f} 之间 → B"
    else:
        missing = "、".join(f"「{dim.label}」" for dim in unknown)
        tail = f"总分区间 {low:.2f}–{high:.2f} 跨档（{b_min:.2f} / {a_min:.2f}），需补全{missing}"
    suffix = f"（{'；'.join(notes)}）" if notes else ""
    return f"{head}，{tail}{suffix}"


def _parse_notes(by_field: dict[str, Fact]) -> list[str]:
    """有原文却解析不出来的维度：理由里点名，人一眼知道「该补一条能算的写法」。

    目前的解析口径只有金额（`80万 / 800000 / 八十万` 这类）；解析失败不当 0 分，
    所以必须写出来，否则「缺成交金额」和「金额写成了 abc」看起来是同一件事。
    """
    fact = by_field.get("amount")
    if fact is not None and parse_amount(fact.value) is None:
        return ["金额无法解析"]
    return []


def _basis(dims: list[DimScore], by_field: dict[str, Fact]) -> list[str]:
    """回收参与打分的原文片段：只取已知维度用到的字段，去重后最多 `MAX_BASIS` 条。"""
    spans: list[str] = []
    for dim in dims:
        if not dim.known:
            continue
        for field in DIM_FACT_FIELDS[dim.key]:
            fact = by_field.get(field)
            if fact is None:
                continue
            span = fact.source_span.strip()
            if span and span not in spans:
                spans.append(span)
    return spans[:MAX_BASIS]


def _parse_chinese_number(text: str) -> float | None:
    """解析中文数字（`三十` / `一百二十` / `八十万`）；含不认识的字就返回 None。

    规则照中文计数的读法：`十 / 百 / 千` 是节内单位，`万 / 亿` 是节单位；
    `十` 开头（如「十二万」）按 1×10 处理。返回的是不含金额单位的绝对值。
    """
    total = 0.0
    section = 0.0
    number = 0.0
    seen = False

    for char in text:
        if char in _CN_DIGITS:
            number = float(_CN_DIGITS[char])
            seen = True
        elif char in _CN_UNITS:
            unit = float(_CN_UNITS[char])
            section += (number if number else 1.0) * unit
            number = 0.0
            seen = True
        elif char in _CN_SECTIONS:
            section += number
            total += (section if section else 1.0) * float(_CN_SECTIONS[char])
            section = 0.0
            number = 0.0
            seen = True
        else:
            return None

    if not seen:
        return None
    return total + section + number
