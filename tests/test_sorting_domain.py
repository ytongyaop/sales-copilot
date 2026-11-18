"""分拣单的纯规则测试：要素校验（含诊断消息）、出库建议的两个分支、分级兜底。

纯规则，不联网、不落库；每条断言都对应 `domain/sorting.py` 的一条判据。
"""

from salescopilot.domain.models import CARD_SECTIONS
from salescopilot.domain.sorting import (
    ElementHit,
    build_next_action,
    grade_from_elements,
    split_diagnostic,
    validate_elements,
)

RAW = "华远建设有限公司的张老板说预算三十万，下周一下午再聊"

VALID = {
    "card_section": "needs",
    "field": "need",
    "value": "预算三十万",
    "source_span": "预算三十万",
    "confidence": 0.8,
}


def _hit(
    section: str = "needs",
    field: str = "need",
    value: str = "预算三十万",
    span: str = "预算三十万",
    confidence: float = 0.8,
) -> ElementHit:
    return ElementHit(
        card_section=section, field=field, value=value, source_span=span, confidence=confidence
    )


def test_card_sections_is_the_only_source_of_truth() -> None:
    """四块的取值直接取 `CARD_SECTIONS`：这里不另写一份白名单。"""
    assert CARD_SECTIONS == ("basic", "needs", "decision", "followup")


def test_valid_element_is_kept_with_aligned_span() -> None:
    """合规要素原样留下；置信度越界（百分制）截断到 0-1。"""
    kept, diagnostics = validate_elements(RAW, [{**VALID, "confidence": 88}])

    assert diagnostics == []
    assert len(kept) == 1
    assert kept[0] == _hit(confidence=1.0)
    assert kept[0].source_span in RAW


def test_unknown_card_section_is_dropped_with_index() -> None:
    """块名不在 `CARD_SECTIONS` 里：丢掉，并在诊断里给出下标与原因。"""
    kept, diagnostics = validate_elements(RAW, [{**VALID, "card_section": "summary"}])

    assert kept == []
    assert len(diagnostics) == 1
    assert diagnostics[0].startswith("elements[0]：")
    assert "card_section" in diagnostics[0]

    index, reason = split_diagnostic(diagnostics[0])
    assert index == 0
    assert "card_section" in reason


def test_empty_field_value_or_span_is_dropped() -> None:
    """三个必填项任一为空就丢掉；诊断里点名是「哪一条」。"""
    for key in ("field", "value", "source_span"):
        kept, diagnostics = validate_elements(RAW, [VALID, {**VALID, key: "  "}])

        assert [element.field for element in kept] == ["need"]
        assert len(diagnostics) == 1
        assert diagnostics[0].startswith("elements[1]：")
        assert "不能为空" in diagnostics[0]


def test_fabricated_span_is_dropped_not_kept() -> None:
    """编造的出处（原文里找不到）被丢弃，诊断说明「在原文里找不到」。"""
    fabricated = {**VALID, "value": "需要总部审批", "source_span": "客户说下个月总部一定会批"}

    kept, diagnostics = validate_elements(RAW, [fabricated])

    assert kept == []
    assert len(diagnostics) == 1
    assert "出处片段在原文里找不到" in diagnostics[0]
    index, _reason = split_diagnostic(diagnostics[0])
    assert index == 0


def test_non_dict_element_is_reported_not_raised() -> None:
    """模型吐出乱七八糟的形状也只记诊断，不抛异常（接口不该 500）。"""
    kept, diagnostics = validate_elements(RAW, ["不是对象", None])

    assert kept == []
    assert len(diagnostics) == 2
    assert diagnostics[0].startswith("elements[0]：")
    assert diagnostics[1].startswith("elements[1]：")


def test_next_action_prefers_the_first_missing_section() -> None:
    """有缺项时动作指向最先缺的那一块，并且必须给出「手上已有的价值」。"""
    elements = [_hit(section="needs", field="need", value="预算三十万")]

    action = build_next_action(elements, ["basic", "decision", "followup"])

    assert "基础信息" in action["action"]
    assert action["value_offered"] == "预算三十万"
    assert action["due_hint"] == ""


def test_next_action_falls_back_to_the_latest_appointment() -> None:
    """没有缺项时指向最近一次约定的时间：`due_hint` 与该字段的取值一致。"""
    elements = [
        _hit(section="basic", field="company", value="华远建设", span="华远建设有限公司"),
        _hit(section="followup", field="appointment_at", value="下周一", span="下周一下午再聊"),
    ]

    action = build_next_action(elements, [])

    assert action["due_hint"] == "下周一"
    assert action["value_offered"] == "下周一"
    assert "约定时间" in action["action"]


def test_next_action_is_empty_when_nothing_is_usable() -> None:
    """一条要素都没有时 `action` 为空，由用例层拒绝生成分拣单。"""
    action = build_next_action([], list(CARD_SECTIONS))

    assert action == {"action": "", "value_offered": "", "due_hint": ""}


def test_grade_from_elements_covers_a_b_c() -> None:
    """分级兜底只看要素覆盖：需求 + 决策 → A；只有需求 → B；其余 → C。"""
    assert grade_from_elements([_hit("needs"), _hit("decision", "decision_maker")])["value"] == "A"
    assert grade_from_elements([_hit("needs")])["value"] == "B"
    assert (
        grade_from_elements([_hit("basic", "company", "华远建设", "华远建设有限公司")])["value"]
        == "C"
    )
    assert grade_from_elements([])["value"] == "C"
