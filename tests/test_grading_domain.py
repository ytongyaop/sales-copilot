"""分级规则的纯函数测试：四维打分 / 上下界 / 分档 / 金额解析 / 紧迫度分档。

全程离线、无库：`grade_from_facts` 只吃「模板 `grading` 段 + 事实列表」，
今天是入参（`today`），所以同一批事实配同一天永远得到同一个结果。

反证放在显眼处：**四维缺一（哪怕其余满分）不得为 A**。
"""

from datetime import date, timedelta

import pytest

from salescopilot.domain.grading import DimScore, grade_from_facts, parse_amount
from salescopilot.domain.models import Fact

TODAY = date(2026, 11, 18)

# 与三份模板同一组默认口径：金额 .3 / 紧迫 .3 / 决策 .2 / 匹配 .2，A 0.75、B 0.45。
SPEC = {
    "grading": {
        "weights": {"amount": 0.3, "urgency": 0.3, "clarity": 0.2, "fit": 0.2},
        "bands": {"a_min": 0.75, "b_min": 0.45},
        "default_mode": "propose",
    }
}


def _fact(field: str, value: str, *, span: str | None = None) -> Fact:
    """造一条事实：出处默认用取值本身（够长即可，纯函数不校验能否在原文定位）。"""
    return Fact(
        id=f"fact-{field}",
        lead_id="lead-1",
        raw_id="raw-1",
        field=field,
        value=value,
        source="manual",
        source_span=span if span is not None else f"{field} 的原文：{value}",
        confidence=1.0,
        locked=False,
        at="2026-11-18T09:00:00+00:00",
    )


def _full_facts(amount: str = "60万", days: int = 3) -> list[Fact]:
    """四维齐全的一批事实：金额大、约定在即、决策链清楚、匹配点明确。"""
    return [
        _fact("amount", amount),
        _fact("appointment_at", (TODAY + timedelta(days=days)).isoformat()),
        _fact("decision_maker", "王总"),
        _fact("approval_flow", "总部审批后签约"),
        _fact("our_fit", "方案正好对上他们的需求"),
    ]


def _dim(result, key: str) -> DimScore:
    return next(dim for dim in result.dims if dim.key == key)


# ---------------------------------------------------------------- A 档与反证


def test_all_four_dims_full_scores_is_a() -> None:
    """四维齐全且都满分 → A；上下界相等、理由里带加权与门槛。"""
    result = grade_from_facts(SPEC, _full_facts(), today=TODAY)

    assert result.value == "A"
    assert all(dim.known for dim in result.dims)
    assert [dim.key for dim in result.dims] == ["amount", "urgency", "clarity", "fit"]
    assert all(dim.score == 1.0 for dim in result.dims)
    assert result.low == 1.0
    assert result.high == 1.0
    assert "四维齐全" in result.reason
    assert "加权 1.00 ≥ 0.75 → A" in result.reason
    assert result.basis, "A 档必须给出原文依据"
    assert len(result.basis) <= 6


# 反证：唯一缺的那一维是「成交金额」，其余四维里其余三项都是满分。
def test_missing_one_dim_can_never_be_a_even_if_the_rest_are_full() -> None:
    """缺一维就不是 A：其它三维满分也不行（A 档要求四维齐全）。"""
    facts = [fact for fact in _full_facts() if fact.field != "amount"]

    result = grade_from_facts(SPEC, facts, today=TODAY)

    assert result.value != "A"
    assert result.value == "needs_info", "缺金额时上下界跨 A 档，只能待补全"
    assert not _dim(result, "amount").known
    assert all(_dim(result, key).score == 1.0 for key in ("urgency", "clarity", "fit"))
    # 下界 0.70（满分三维），上界 1.00（金额按满权重算）——落在 A 档两侧。
    assert result.low == pytest.approx(0.7, abs=1e-6)
    assert result.high == pytest.approx(1.0, abs=1e-6)
    assert "缺「成交金额」" in result.reason
    assert "跨档" in result.reason


def test_clarity_alone_missing_is_not_enough_for_a() -> None:
    """只有「决策清晰度」未知（另两项决策要素都缺）：同样不得为 A。"""
    facts = [
        fact for fact in _full_facts() if fact.field not in ("decision_maker", "approval_flow")
    ]

    result = grade_from_facts(SPEC, facts, today=TODAY)

    assert not _dim(result, "clarity").known
    assert result.value == "needs_info"
    assert "缺「决策清晰度」" in result.reason


# ------------------------------------------------------------ 上下界与分档


def test_bounds_inside_b_band_gives_b() -> None:
    """上下界都落在 B 区间（0.45–0.75）→ B。"""
    facts = [
        _fact("amount", "20万"),
        _fact("appointment_at", (TODAY + timedelta(days=20)).isoformat()),
        _fact("decision_maker", "王总"),
    ]

    result = grade_from_facts(SPEC, facts, today=TODAY)

    assert result.value == "B"
    assert result.low == pytest.approx(0.49, abs=1e-6)
    assert result.high == pytest.approx(0.69, abs=1e-6)
    assert "落在 0.45–0.75 之间 → B" in result.reason


def test_bounds_inside_c_band_gives_c() -> None:
    """四维齐全但都很弱：上界已经低于 B 档下限 → C（缺什么都不会变）。"""
    facts = [
        _fact("amount", "5万"),
        _fact("appointment_at", (TODAY + timedelta(days=60)).isoformat()),
        _fact("decision_maker", "王总"),
        _fact("need", "想换一套流程"),
    ]

    result = grade_from_facts(SPEC, facts, today=TODAY)

    assert result.value == "C"
    assert all(dim.known for dim in result.dims), "这一组是四维齐全的弱单"
    assert result.high == pytest.approx(0.41, abs=1e-6)
    assert "低于 B 档下限 0.45 → C" in result.reason


def test_bounds_crossing_bands_gives_needs_info() -> None:
    """下界在 B 档、上界在 A 档：跨档 → needs_info，不替人拍板。"""
    facts = [
        _fact("amount", "60万"),
        _fact("appointment_at", (TODAY + timedelta(days=3)).isoformat()),
        _fact("need", "想换一套流程"),
    ]

    result = grade_from_facts(SPEC, facts, today=TODAY)

    assert result.value == "needs_info"
    assert result.low == pytest.approx(0.7, abs=1e-6)
    assert result.high == pytest.approx(0.9, abs=1e-6)
    assert 0.45 <= result.low < 0.75 <= result.high


# ---------------------------------------------------------------- 金额解析


@pytest.mark.parametrize(
    ("text", "expected_score"),
    [
        ("80万", 1.0),
        ("800000", 1.0),
        ("八十万", 1.0),
        ("三十万", 0.7),
    ],
)
def test_amount_writing_styles_are_parsed_into_bands(text: str, expected_score: float) -> None:
    """`80万` / `800000` / `八十万` / `三十万` 都要落进正确档位。"""
    result = grade_from_facts(SPEC, [_fact("amount", text)], today=TODAY)
    amount_dim = _dim(result, "amount")

    assert amount_dim.known is True, text
    assert amount_dim.score == expected_score, text


@pytest.mark.parametrize("text", ["abc", "还在谈", ""])
def test_unparsable_amount_is_unknown_and_says_so(text: str) -> None:
    """解析不了金额：该维算未知（不当 0 分），理由里明确写「金额无法解析」。"""
    result = grade_from_facts(SPEC, [_fact("amount", text)], today=TODAY)
    amount_dim = _dim(result, "amount")

    assert amount_dim.known is False
    assert amount_dim.score == 0.0
    assert "金额无法解析" in result.reason


def test_parse_amount_handles_common_styles() -> None:
    """金额解析的边界：万 / w / 千分位 / 货币符号 / 中文数字。"""
    assert parse_amount("80万") == 800_000
    assert parse_amount("80w") == 800_000
    assert parse_amount("1.5万") == 15_000
    assert parse_amount("10,000") == 10_000
    assert parse_amount("¥800000") == 800_000
    assert parse_amount("一百二十万") == 1_200_000
    assert parse_amount("三十万元") == 300_000
    assert parse_amount("abc") is None
    assert parse_amount("") is None


# ---------------------------------------------------------------- 紧迫度分档


@pytest.mark.parametrize(
    ("days", "expected_score"),
    [(3, 1.0), (20, 0.6), (60, 0.3)],
)
def test_urgency_bands_by_days_from_today(days: int, expected_score: float) -> None:
    """今天 +3 / +20 / +60 天分别落进 1.0 / 0.6 / 0.3。"""
    facts = [_fact("appointment_at", (TODAY + timedelta(days=days)).isoformat())]

    result = grade_from_facts(SPEC, facts, today=TODAY)

    assert _dim(result, "urgency").score == expected_score


def test_urgency_ignores_non_iso_dates() -> None:
    """约定时间不是 ISO 日期（例如「下周三」）：算未知，不猜。"""
    result = grade_from_facts(SPEC, [_fact("appointment_at", "下周三")], today=TODAY)

    assert _dim(result, "urgency").known is False
    assert result.value == "needs_info"


# ------------------------------------------------------------ 纯函数与稳定性


def test_same_inputs_same_result() -> None:
    """同一批事实 + 同一天 → 完全相等的结果（可回归、可对账）。"""
    first = grade_from_facts(SPEC, _full_facts(), today=TODAY)
    second = grade_from_facts(SPEC, _full_facts(), today=TODAY)

    assert first == second
    assert isinstance(first.dims[0], DimScore)


def test_basis_dedupes_and_skips_unknown_dims() -> None:
    """依据只收参与打分的片段，去重后最多 6 条。"""
    facts = [
        _fact("amount", "60万", span="预算六十万"),
        _fact("amount", "60万", span="预算六十万"),
        _fact("appointment_at", (TODAY + timedelta(days=3)).isoformat(), span="下周三见面"),
    ]

    result = grade_from_facts(SPEC, facts, today=TODAY)

    assert result.basis == ["预算六十万", "下周三见面"]
