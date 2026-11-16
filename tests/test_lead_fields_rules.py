"""线索字段规则的纯函数测试：白名单、空值、ISO 日期、合并口径、缺字段计算。

全是 `domain/rules.py` 的纯函数，不建库、不联网。
"""

from salescopilot.domain.rules import (
    merge_fields,
    missing_fields_for,
    validate_lead_fields,
)


def test_valid_patch_passes() -> None:
    assert validate_lead_fields({"decision_maker": "李校长", "need": "想提高数学成绩"}) == []
    assert validate_lead_fields({"appointment_at": "2026-11-20"}) == []
    assert validate_lead_fields({}) == []


def test_key_outside_whitelist_is_rejected() -> None:
    issues = validate_lead_fields({"hobby": "钓鱼"})

    assert [issue.location for issue in issues] == ["fields.hobby"]
    assert "必须是以下之一" in issues[0].message
    # 白名单里的字段一个都没漏（提示里要能看出允许哪些）
    assert "decision_maker" in issues[0].message


def test_blank_or_non_text_value_is_rejected() -> None:
    assert [issue.location for issue in validate_lead_fields({"need": "   "})] == ["fields.need"]

    issues = validate_lead_fields({"amount": 1000})
    assert [issue.location for issue in issues] == ["fields.amount"]
    assert issues[0].message == "「金额」必须是文本"


def test_appointment_at_must_be_iso_date() -> None:
    for bad in ("下周一", "2026/11/20", "2026-11-20T10:00"):
        issues = validate_lead_fields({"appointment_at": bad})
        assert [issue.location for issue in issues] == ["fields.appointment_at"]
        assert "YYYY-MM-DD" in issues[0].message


def test_value_too_long_is_rejected() -> None:
    issues = validate_lead_fields({"need": "长" * 201})

    assert [issue.location for issue in issues] == ["fields.need"]
    assert "200" in issues[0].message


def test_merge_fields_only_overrides_given_keys() -> None:
    current = {"need": "旧需求", "amount": "1 万"}
    merged = merge_fields(current, {"need": "新需求", "decision_maker": "李校长"})

    assert merged == {"need": "新需求", "amount": "1 万", "decision_maker": "李校长"}
    # 不改调用方手里的那个 dict
    assert current == {"need": "旧需求", "amount": "1 万"}


def test_merge_fields_strips_surrounding_spaces() -> None:
    assert merge_fields({}, {"need": "  提高成绩  "}) == {"need": "提高成绩"}


def test_missing_fields_for_reads_block_contract() -> None:
    # qualify 的必填项是决策人 + 核心需求（块契约来自 domain/blocks.py）
    assert missing_fields_for("qualify", {}) == ["decision_maker", "need"]
    assert missing_fields_for("qualify", {"decision_maker": "李校长", "need": "  "}) == ["need"]
    assert missing_fields_for("qualify", {"decision_maker": "李校长", "need": "提高成绩"}) == []

    # intake 的两个必填项是线索自身属性（contact / source）
    assert missing_fields_for("intake", {}) == ["contact", "source"]
    assert missing_fields_for("intake", {"contact": "13800001111", "source": "referral"}) == []

    assert missing_fields_for("close", {"conclusion": "已成交"}) == []
    # 未知块不编造结论，直接返回空列表（块名合法性由模板校验负责）
    assert missing_fields_for("nonexistent", {}) == []
