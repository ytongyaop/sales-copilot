"""分级的纯规则：四维打分 → A/B/C/needs_info ＋ 理由 ＋ 依据（ADR-0008）。

三条纪律：
- 只做计算：入参是模板 `grading` 段与已落库的事实，不查库、不读时钟、不发网络请求
  （`today` 由调用方传入，本模块自己不看日历）；
- **分档只由规则产出**：模型不参与打分，也不再给「置信度」；同一批事实永远同一档；
- 权重与分档阈值**只来自模板**（`grading.weights` / `grading.bands`），代码里不写死数字。

打分口径（四维，各维 0–1，乘权重后相加）：
- `amount` 成交金额：≥50 万 → 1.0；≥10 万 → 0.7；>0 → 0.4（解析不了 → 未知）；
  写成区间（`60万到80万`）时取**下限**，理由里注明「取区间下限」，理由见 `parse_amount`；
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

from salescopilot.domain.models import Fact, GradingRecord

# 分档取值：三档 ＋ 「待补全」。`GRADE_VALUES` 只含三档，人工定档只能选这三个。
GRADE_A = "A"
GRADE_B = "B"
GRADE_C = "C"
GRADE_NEEDS_INFO = "needs_info"
GRADE_VALUES: tuple[str, ...] = (GRADE_A, GRADE_B, GRADE_C)

# 还没判过档的客户：`unknown` 不是一个档位，而是「还没有结论」。
# 待办排序、货架分组、详情页徽标都用这个说法，不再各自拼一个「未分级」以外的取值。
GRADE_UNKNOWN = "unknown"

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
# 区间的分隔写法：`60万到80万` / `60万~80万` / `60万至80万` / `60-80万` / `60万—80万`。
_AMOUNT_RANGE_SEP = re.compile(r"到|至|[~～—–\-]")
# 单位倍数：`万 / w` 是一万，`亿` 是一亿；没写单位就是元。
_UNIT_FACTORS = {"万": 10_000.0, "w": 10_000.0, "W": 10_000.0}
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
    """一次分级的结果：档次 ＋ 分数区间 ＋ 四维明细 ＋ 长短两句理由 ＋ 原文依据。

    - `low` / `high`：已知维度按实得分、未知维度按 0 / 满分算出的下界与上界；
      四维齐全时两者相等；
    - `summary`：**短理由**，一句话、不带分数区间，给默认视图那一行用
      （例：`四维齐全，加权 0.80 → A` / `缺「成交金额」，需要补充后再定档`）；
    - `reason`：完整理由，带上下界与分档门槛，展开「查看计算过程」时才看；
    - `basis`：参与打分的原文片段（去重，最多 `MAX_BASIS` 条），能回到事实的出处。
    """

    value: str
    low: float
    high: float
    dims: list[DimScore]
    summary: str
    reason: str
    basis: list[str]


@dataclass(frozen=True)
class AmountParse:
    """一次金额解析的结果：`value`（元）＋ 区间两端（不是区间时两端恒为空）。

    `value` 恒是**用于分档的那个数**：写成区间时取下限。区间给的是范围，分档只能吃一个
    确定数，宁可把单子看小一点，也不把「60 万到 80 万」当成 80 万的大单。
    """

    value: float
    low: float | None = None
    high: float | None = None

    @property
    def is_range(self) -> bool:
        """两端都解析出来才算区间（只写对一端时按那一端处理，不当区间）。"""
        return self.low is not None and self.high is not None


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
        summary=_summary(dims, value=value, low=low),
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
    `一百二十万` / `10,000` / `¥800000` / `三十万元`，以及**区间**：
    `60万到80万` / `60-80万` / `60万~80万` / `60万至80万`。

    区间一律取**下限**（两端都能解析时取小的那个）；只有一端可解析就用那一端；
    两端都解析不了（如 `abc到def`）才返回 `None`。其余写法（如 `约三十万`）同样返回 None。
    """
    detail = parse_amount_detail(text)
    return detail.value if detail is not None else None


def parse_amount_detail(text: str) -> AmountParse | None:
    """同 `parse_amount`，但把「是不是区间、两端各是多少」一并交回，供理由里说明口径。

    取区间下限这件事必须能被复核：`_parse_notes` 据此在理由里写「金额取区间下限 60 万」，
    否则人只看到 60 万，会以为原文就写了 60 万。
    """
    raw = text.strip() if isinstance(text, str) else ""
    if not raw:
        return None
    cleaned = _AMOUNT_NOISE.sub("", raw.replace("人民币", ""))
    if not cleaned:
        return None

    parts = [part for part in _AMOUNT_RANGE_SEP.split(cleaned) if part]
    if len(parts) == 1:
        single = _parse_amount_end(parts[0])
        if single is None:
            return None
        return AmountParse(value=_amount_value(single, None))

    if len(parts) == 2:
        left = _parse_amount_end(parts[0])
        right = _parse_amount_end(parts[1])
        if left is None and right is None:
            return None
        if left is None or right is None:
            # 只写对了一端：这一端仍是可用信息，别因为另一端写坏就整条丢掉。
            return AmountParse(value=_amount_value(left or right, None))
        first = _amount_value(left, right)
        second = _amount_value(right, left)
        low, high = min(first, second), max(first, second)
        return AmountParse(value=low, low=low, high=high)

    return None


def _parse_amount_end(text: str) -> tuple[float, float] | None:
    """解析区间的一端：返回（数值, 单位倍数）；解析不了返回 None。

    单位与数值分开返回，是因为中文里常把单位只写在第二端（`60-80万`），
    这时第一端的 60 要沿用第二端的「万」，不能当成 60 元。
    """
    match = _NUMERIC_AMOUNT.match(text)
    if match is not None:
        unit = match.group(2) or ""
        return float(match.group(1)), _UNIT_FACTORS.get(unit, 1.0)

    value = _parse_chinese_number(text)
    if value is None:
        return None
    if text.endswith("亿"):
        return value / 100_000_000, 100_000_000.0
    if text.endswith("万"):
        return value / 10_000, 10_000.0
    return value, 1.0


def _amount_value(end: tuple[float, float], other: tuple[float, float] | None) -> float:
    """把（数值, 单位）还原成元；自己没写单位、另一端写了时沿用另一端的单位。"""
    number, factor = end
    if factor == 1.0 and other is not None and other[1] != 1.0:
        return number * other[1]
    return number * factor


def parse_iso_date(text: str) -> date | None:
    """把 `YYYY-MM-DD` 解析成日期；不是这个形状或日历上不存在就返回 None。"""
    value = text.strip() if isinstance(text, str) else ""
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def current_value(records: list[GradingRecord]) -> str:
    """该客户当前档位：最近一次**人工值**优先，其次最近一次 AI 值，都没有返回 `unknown`。

    人工值压过 AI 值是分级的主权规则（人改判了就以人为准），所以两列分别倒着找第一条非空；
    这条口径在此收口一次，待办排序、货架分组与详情页徽标都从这里取，别各写一遍。
    """
    latest_human = _latest_non_empty(records, "human_value")
    if latest_human:
        return latest_human
    latest_ai = _latest_non_empty(records, "ai_value")
    return latest_ai or GRADE_UNKNOWN


def _latest_non_empty(records: list[GradingRecord], field: str) -> str:
    """倒着找第一条该列非空的留痕（`human_value` / `ai_value` 用同一套逻辑）。"""
    for record in reversed(records):
        value = getattr(record, field, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


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


def _summary(dims: list[DimScore], *, value: str, low: float) -> str:
    """拼一句**短理由**：不带分数区间的数字，给默认视图那一行（列表 / 抬头）用。

    缺维度时先说缺什么，再按已有结论说「补不补得动」：
    - `缺「成交金额」，需要补充后再定档`（缺项可能改变结论，就是待补全）；
    - `缺「需求匹配度」，已知信息落在 B 档`（上界也进不了 A，补全不会升档）；
    - `缺「成交金额」，按已知信息不足以进 B 档`（上界已经在 B 档以下）。
    四维齐全时说加权分与档位：`四维齐全，加权 0.80 → A` / `加权 0.52，落在 B 档`。
    """
    unknown = [dim for dim in dims if not dim.known]
    missing = "、".join(f"「{dim.label}」" for dim in unknown)

    if unknown:
        if value == GRADE_NEEDS_INFO:
            return f"缺{missing}，需要补充后再定档"
        if value == GRADE_C:
            return f"缺{missing}，按已知信息不足以进 B 档"
        if value == GRADE_B:
            return f"缺{missing}，已知信息落在 B 档"
        return f"缺{missing}，已知信息已够 A 档"

    if value == GRADE_A:
        return f"四维齐全，加权 {low:.2f} → A"
    if value == GRADE_B:
        return f"加权 {low:.2f}，落在 B 档"
    if value == GRADE_C:
        return f"加权 {low:.2f}，低于 B 档"
    # 四维齐全时上下界相等，理论上到不了这里；兜底给一句不带数字的话，别吐空串。
    return "已知信息不足以定档"


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
    """有原文却解析不出来的维度、以及写成区间而取了近似值的维度：理由里点名。

    目前的解析口径只有金额（`80万 / 800000 / 八十万 / 60万到80万` 这类）；解析失败不当 0 分，
    所以必须写出来，否则「缺成交金额」和「金额写成了 abc」看起来是同一件事。
    区间也同理：分档用的是下限，理由里必须说清「取区间下限 60 万」，人才知道 60 万是
    我们选的口径，不是客户原话。
    """
    fact = by_field.get("amount")
    if fact is None:
        return []
    detail = parse_amount_detail(fact.value)
    if detail is None:
        return ["金额无法解析"]
    if detail.is_range:
        return [f"金额取区间下限 {_amount_label(detail.value)}"]
    return []


def _amount_label(value: float) -> str:
    """金额的人话：整万写「60 万」，不满一万写「8000 元」，其余保留一位小数。"""
    if value >= 10_000:
        return f"{value / 10_000:g} 万"
    return f"{value:g} 元"


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
