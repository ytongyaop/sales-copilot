"""提炼评测指标的纯函数测试：口径与边界在这里钉死。

三类反证：空集不算满分、全错记 0、空片段不算「出现在原文里」（Python 里 `"" in s` 为真）。
"""

from salescopilot.domain.eval_metrics import (
    field_precision,
    field_recall,
    match_count,
    normalize_value,
    pair_of,
    pairs_of,
    span_valid,
    span_valid_rate,
)


def test_regular_field_strips_all_whitespace() -> None:
    assert normalize_value("amount", " 2000 元 ") == "2000元"
    assert normalize_value("appointment_at", "下 周一") == "下周一"
    assert pair_of({"field": "amount", "value": "2000 元"}) == ("amount", "2000元")


def test_objection_requires_exact_enum_value() -> None:
    assert normalize_value("objection", " price ") == "price"
    # 枚举之外的值（大小写、中文说法）不归一，也就不算命中
    assert normalize_value("objection", "Price") == "Price"
    assert normalize_value("objection", "价格") == "价格"
    assert (
        match_count(
            [{"field": "objection", "value": "price"}],
            [{"field": "objection", "value": "Price"}],
        )
        == 0
    )


def test_pair_of_ignores_malformed_items() -> None:
    assert pair_of({"value": "price"}) is None
    assert pair_of({"field": "  ", "value": "price"}) is None
    assert pair_of("not-a-mapping") is None
    assert pairs_of([{"field": "need", "value": "演示"}, {"field": "need", "value": " 演示 "}]) == {
        ("need", "演示")
    }


def test_precision_recall_on_half_hit() -> None:
    expected = [
        {"field": "objection", "value": "price"},
        {"field": "appointment_at", "value": "下周一"},
    ]
    predicted = [
        {"field": "objection", "value": "price"},
        {"field": "decision_maker", "value": "王校长"},
    ]

    assert match_count(expected, predicted) == 1
    assert field_precision(expected, predicted) == 0.5
    assert field_recall(expected, predicted) == 0.5


def test_precision_recall_on_full_hit() -> None:
    expected = [{"field": "objection", "value": "price"}]
    predicted = [{"field": "objection", "value": "price"}]

    assert field_precision(expected, predicted) == 1.0
    assert field_recall(expected, predicted) == 1.0


def test_precision_recall_on_all_wrong() -> None:
    expected = [{"field": "objection", "value": "price"}]
    predicted = [{"field": "objection", "value": "time"}]

    assert match_count(expected, predicted) == 0
    assert field_precision(expected, predicted) == 0.0
    assert field_recall(expected, predicted) == 0.0


def test_precision_recall_on_empty_sets_is_zero_not_full_marks() -> None:
    assert field_precision([], []) == 0.0
    assert field_recall([], []) == 0.0
    assert field_precision([], [{"field": "need", "value": "演示"}]) == 0.0
    assert field_recall([{"field": "need", "value": "演示"}], []) == 0.0


def test_span_valid_requires_exact_substring_and_non_empty() -> None:
    content = "客户说太贵了，下周再谈"

    assert span_valid(content, "下周") is True
    # 空片段在 Python 里能被 `in` 命中，必须单独判掉
    assert span_valid(content, "") is False
    assert span_valid(content, "   ") is False
    assert span_valid(content, "再考虑") is False
    assert span_valid("", "下周") is False


def test_span_valid_rate_mixed_and_empty() -> None:
    content = "客户说太贵了，下周再谈"
    facts = [
        {"field": "objection", "value": "price", "source_span": "太贵"},
        {"field": "appointment_at", "value": "下周", "source_span": "明天"},
    ]

    assert span_valid_rate(facts, content) == 0.5
    assert span_valid_rate([], content) == 0.0
