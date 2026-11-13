"""线索入参校验（纯函数）的行为测试：只测 rules，不碰库、不碰接口。"""

from salescopilot.domain.rules import validate_new_lead

VALID = {"name": "张伟", "contact": "13800001111", "source": "referral"}


def test_empty_name_rejected() -> None:
    issues = validate_new_lead(**{**VALID, "name": "   "})

    assert [issue.location for issue in issues] == ["name"]
    assert "名称" in issues[0].message


def test_too_short_contact_rejected() -> None:
    issues = validate_new_lead(**{**VALID, "contact": " 1234 "})

    assert [issue.location for issue in issues] == ["contact"]
    assert "5 个字符" in issues[0].message


def test_unknown_source_rejected() -> None:
    issues = validate_new_lead(**{**VALID, "source": "billboard"})

    assert [issue.location for issue in issues] == ["source"]
    assert "billboard" in issues[0].message


def test_valid_input_has_no_issue() -> None:
    assert validate_new_lead(**VALID) == []
