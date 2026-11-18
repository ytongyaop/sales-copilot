"""字段白名单的扩展测试：模板声明了信息卡字段后，company / approval_flow 也能被补录。

白名单口径：旧默认 `EDITABLE_LEAD_FIELDS` ∪ 模板 `card` 段声明的字段（只扩大不缩小）。
"""

from salescopilot.domain.rules import allowed_lead_fields, validate_lead_fields

SPEC: dict = {
    "card": {
        "sections": {
            "basic": {
                "label": "基础信息",
                "fields": [
                    {"key": "company", "label": "公司全称", "question": "对方公司全称是什么？"}
                ],
            },
            "decision": {
                "label": "决策流程与预算",
                "fields": [
                    {"key": "approval_flow", "label": "决策流程", "question": "内部走什么流程？"}
                ],
            },
        }
    }
}


def test_company_and_approval_flow_are_rejected_by_default() -> None:
    """不传白名单时仍是旧默认：信息卡字段不在里面（向后兼容的基线）。"""
    issues = validate_lead_fields({"company": "华远建设有限公司", "approval_flow": "先议价再报批"})

    assert [issue.location for issue in issues] == ["fields.company", "fields.approval_flow"]


def test_template_fields_extend_the_whitelist() -> None:
    """按模板扩展后，company / approval_flow 可以落库，旧默认字段不受影响。"""
    allowed = allowed_lead_fields(SPEC)

    assert (
        validate_lead_fields(
            {"company": "华远建设有限公司", "approval_flow": "先议价再报批"}, allowed=allowed
        )
        == []
    )
    assert validate_lead_fields({"decision_maker": "张总"}, allowed=allowed) == []
    assert "company" in allowed and "approval_flow" in allowed
    assert "decision_maker" in allowed and "conclusion" in allowed


def test_unknown_field_still_rejected_and_message_lists_allowed() -> None:
    """白名单扩大后仍然拒绝没声明的字段，提示里能看到允许哪些。"""
    issues = validate_lead_fields({"hobby": "钓鱼"}, allowed=allowed_lead_fields(SPEC))

    assert [issue.location for issue in issues] == ["fields.hobby"]
    assert "company" in issues[0].message
    assert "decision_maker" in issues[0].message


def test_without_template_the_whitelist_is_the_legacy_default() -> None:
    """没有模板声明时 `allowed_lead_fields(None)` 就是旧默认（不凭空扩字段）。"""
    assert "company" not in allowed_lead_fields(None)
    assert "decision_maker" in allowed_lead_fields(None)
