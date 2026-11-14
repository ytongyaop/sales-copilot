"""阶段推进规则的纯函数测试：只测 domain/rules.py，不碰库、不碰接口。

flow 用一份最小假模板（4 个块：intake → first_contact → qualify → close），
块契约（requires）仍取自 domain/blocks.py 的真实定义，保证测的是生产规则。
"""

from salescopilot.domain.rules import validate_advance

FLOW = {
    "flow": [
        {"block": "intake"},
        {"block": "first_contact"},
        {"block": "qualify"},
        {"block": "close"},
    ]
}


def test_skip_stage_rejected_with_target_location() -> None:
    """跳步：intake 只能到 first_contact，目标给 qualify 必须被拒。"""
    issues = validate_advance(FLOW, "intake", "qualify", {"contact": "13800001111"})

    assert [issue.location for issue in issues] == ["target_stage"]
    assert "只能推进到 first_contact" in issues[0].message
    assert "qualify" in issues[0].message


def test_missing_required_fields_rejected_per_field() -> None:
    """first_contact → qualify 需要 decision_maker 与 need，缺一个记一条。"""
    issues = validate_advance(FLOW, "first_contact", "qualify", {"contact": "13800001111"})

    assert [issue.location for issue in issues] == ["fields.decision_maker", "fields.need"]
    assert "decision_maker" in issues[0].message
    assert "need" in issues[1].message


def test_blank_field_counts_as_missing() -> None:
    """纯空格也算没填，不能拿空格绕过必填。"""
    issues = validate_advance(
        FLOW, "first_contact", "qualify", {"contact": "13800001111", "decision_maker": "   "}
    )

    assert [issue.location for issue in issues] == ["fields.decision_maker", "fields.need"]


def test_legal_advance_passes() -> None:
    """intake → first_contact 只要求 contact 已填。"""
    assert validate_advance(FLOW, "intake", "first_contact", {"contact": "13800001111"}) == []


def test_unknown_target_stage_rejected() -> None:
    issues = validate_advance(FLOW, "intake", "nonexistent", {"contact": "13800001111"})

    assert [issue.location for issue in issues] == ["target_stage"]
    assert "nonexistent" in issues[0].message


def test_advance_from_terminal_stage_rejected() -> None:
    """已经在最后一块时不允许再推进。"""
    issues = validate_advance(FLOW, "close", "close", {})

    assert [issue.location for issue in issues] == ["target_stage"]
    assert "已是最后一步" in issues[0].message
