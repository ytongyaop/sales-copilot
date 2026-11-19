"""线索入参校验（纯函数）的行为测试：只测 rules，不碰库、不碰接口。

0.1.3 起「能不能建」与「建了要补什么」分开判：`check_new_lead` 同时给 errors / warnings，
`validate_new_lead` 只是「只取 errors」的兼容入口。
"""

from salescopilot.domain.rules import (
    MISSING_CONTACT_WARNING,
    check_new_lead,
    validate_new_lead,
)

VALID = {"name": "张伟", "contact": "13800001111", "source": "referral"}


def test_empty_name_rejected() -> None:
    issues = validate_new_lead(**{**VALID, "name": "   "})

    assert [issue.location for issue in issues] == ["name"]
    assert "名称" in issues[0].message


def test_empty_name_is_an_error_in_check() -> None:
    """名字是唯一的主要条件：空名字仍是错误（拦落库），不是提醒。"""
    check = check_new_lead(**{**VALID, "name": "   "})

    assert [issue.location for issue in check.errors] == ["name"]
    assert check.warnings == []


def test_too_short_contact_rejected() -> None:
    issues = validate_new_lead(**{**VALID, "contact": " 1234 "})

    assert [issue.location for issue in issues] == ["contact"]
    assert "5 个字符" in issues[0].message


def test_half_contact_is_still_an_error() -> None:
    """「139」这种半截号是**错误**：写了的联系方式必须够 5 个字符，免得建出打不通的客户。"""
    check = check_new_lead(**{**VALID, "contact": "139"})

    assert [issue.location for issue in check.errors] == ["contact"]
    assert "5 个字符" in check.errors[0].message
    assert check.warnings == []


def test_empty_contact_is_a_warning_not_an_error() -> None:
    """联系方式为空：无错误、一条提醒（客户照建，只是无法触达，要人补录）。"""
    check = check_new_lead(**{**VALID, "contact": "   "})

    assert check.errors == []
    assert [issue.location for issue in check.warnings] == ["contact"]
    assert check.warnings[0].message == MISSING_CONTACT_WARNING
    assert "没有联系方式" in check.warnings[0].message


def test_unknown_source_rejected() -> None:
    issues = validate_new_lead(**{**VALID, "source": "billboard"})

    assert [issue.location for issue in issues] == ["source"]
    assert "billboard" in issues[0].message


def test_valid_input_has_no_issue() -> None:
    assert validate_new_lead(**VALID) == []


def test_valid_input_has_no_warning_either() -> None:
    check = check_new_lead(**VALID)

    assert check.errors == []
    assert check.warnings == []
