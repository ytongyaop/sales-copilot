"""跟进三要素规则的纯函数测试：只测 domain/rules.py 的 validate_followup。"""

from salescopilot.domain.rules import validate_followup

GOOD = {
    "conclusion": "客户下周再谈",
    "objection": "price",
    "next_followup_at": "2026-11-20",
}


def test_empty_conclusion_rejected() -> None:
    issues = validate_followup(**{**GOOD, "conclusion": "   "})

    assert [issue.location for issue in issues] == ["conclusion"]
    assert "结论" in issues[0].message


def test_one_char_conclusion_rejected() -> None:
    issues = validate_followup(**{**GOOD, "conclusion": "好"})

    assert [issue.location for issue in issues] == ["conclusion"]
    assert "2 个字符" in issues[0].message


def test_empty_objection_rejected() -> None:
    issues = validate_followup(**{**GOOD, "objection": "  "})

    assert [issue.location for issue in issues] == ["objection"]
    assert "异议" in issues[0].message


def test_unknown_objection_rejected() -> None:
    """异议必须是通用枚举里的取值，随便写一个词要报错并列出可选值。"""
    issues = validate_followup(**{**GOOD, "objection": "budget"})

    assert [issue.location for issue in issues] == ["objection"]
    assert "budget" in issues[0].message
    assert "price" in issues[0].message


def test_empty_next_followup_at_rejected() -> None:
    issues = validate_followup(**{**GOOD, "next_followup_at": ""})

    assert [issue.location for issue in issues] == ["next_followup_at"]
    assert "不能为空" in issues[0].message


def test_bad_date_rejected() -> None:
    issues = validate_followup(**{**GOOD, "next_followup_at": "2026/11/20"})

    assert [issue.location for issue in issues] == ["next_followup_at"]
    assert "YYYY-MM-DD" in issues[0].message


def test_impossible_date_rejected() -> None:
    """形状对但日历上不存在（2 月 30 日）也要被拒。"""
    issues = validate_followup(**{**GOOD, "next_followup_at": "2026-02-30"})

    assert [issue.location for issue in issues] == ["next_followup_at"]


def test_valid_followup_passes() -> None:
    assert validate_followup(**GOOD) == []
