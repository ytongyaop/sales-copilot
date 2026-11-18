"""分拣单的纯规则测试：要素校验（含诊断消息）、出库建议的三个分支。

纯规则，不联网、不落库；每条断言都对应 `domain/sorting.py` 的一条判据。
「给什么」（`value_offered`）只认模板文案，模型写的那份只做对照（`model_value_offered`）。
"""

from salescopilot.domain.models import CARD_SECTIONS
from salescopilot.domain.sorting import (
    ElementHit,
    build_next_action,
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
    """有缺项时动作指向最先缺的那一块；「给什么」取模板里该分区的文案。"""
    elements = [_hit(section="needs", field="need", value="预算三十万")]

    action = build_next_action(
        elements, ["basic", "decision", "followup"], fallback_values={"basic": "公司背景资料"}
    )

    assert "基础信息" in action["action"]
    assert action["value_offered"] == "公司背景资料"
    assert action["due_hint"] == ""


def test_next_action_falls_back_to_the_latest_appointment() -> None:
    """没有缺项时指向最近一次约定的时间：`due_hint` 与该字段的取值一致。"""
    elements = [
        _hit(section="basic", field="company", value="华远建设", span="华远建设有限公司"),
        _hit(section="followup", field="appointment_at", value="下周一", span="下周一下午再聊"),
    ]

    action = build_next_action(elements, [], fallback_values={"followup": "上次承诺的跟进"})

    assert action["due_hint"] == "下周一"
    assert action["value_offered"] == "上次承诺的跟进"
    assert "约定时间" in action["action"]


def test_next_action_is_empty_when_nothing_is_usable() -> None:
    """一条要素都没有时 `action` 与 `value_offered` 为空，由用例层拒绝生成分拣单。"""
    action = build_next_action([], list(CARD_SECTIONS), fallback_values={"basic": "公司背景资料"})

    assert action == {
        "action": "",
        "value_offered": "",
        "due_hint": "",
        "model_value_offered": "",
    }


def test_model_value_offered_is_kept_verbatim_and_never_used() -> None:
    """模型把「给什么」填成客户名（真模型实测）：规则值照旧取模板，模型原值只留作对照。

    反证的是真模型产出：`value_offered = "王总（华远建设集团，最终签批人）"`。
    「给客户什么」不能由模型挑——它会把客户名、金额、时间一起挑进来。
    """
    elements = [_hit(section="followup", field="appointment_at", value="2026-11-25")]
    model_text = "王总（华远建设集团，最终签批人）"

    action = build_next_action(
        elements,
        [],
        fallback_values={"followup": "上次承诺的跟进"},
        model_value_offered=model_text,
    )

    assert action["value_offered"] == "上次承诺的跟进"
    assert action["model_value_offered"] == model_text
    assert model_text not in action["value_offered"]
    assert action["due_hint"] == "2026-11-25"


def test_value_offered_is_empty_when_the_template_has_no_such_section() -> None:
    """模板缺该分区（或整个没装配）时 `value_offered` 为空：不拿要素值、不拿日期凑。"""
    elements = [_hit(section="followup", field="appointment_at", value="2026-11-25")]

    for fallback in (None, {}, {"basic": "公司背景资料"}):
        action = build_next_action(elements, [], fallback_values=fallback)

        assert action["value_offered"] == ""
        assert action["model_value_offered"] == ""
        assert action["due_hint"] == "2026-11-25"
        assert action["action"], "动作仍要说清（只是不再拿别的值冒充价值）"


def test_element_values_are_never_used_as_the_offer() -> None:
    """要素里明明有一条像「给的东西」的文案，也不采用：规则优先，只认模板。"""
    elements = [_hit(section="needs", field="need", value="同类项目案例")]

    with_template = build_next_action(
        elements, ["basic"], fallback_values={"basic": "公司背景资料"}
    )
    assert with_template["value_offered"] == "公司背景资料"

    without_template = build_next_action(elements, ["basic"])
    assert without_template["value_offered"] == "", "模板没有就留空，不退回要素值"
