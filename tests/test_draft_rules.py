"""话术规则的行为测试：日期检查、禁用词命中/未命中、引用形状、确认三要素校验。"""

from datetime import date

from salescopilot.domain.draft import (
    BANNED_PHRASES,
    build_citations_payload,
    check_banned,
    check_dates,
    find_dates,
    validate_confirm,
)

TODAY = date(2026, 11, 18)


def test_check_banned_reports_each_hit_with_its_word() -> None:
    text = "这期保证提分，还包退，名额百分百留给你。"

    issues = check_banned(text)

    words = [issue.message for issue in issues]
    assert len(issues) == 3
    assert all(issue.location == "draft.text" for issue in issues)
    assert any("保证提分" in message for message in words)
    assert any("包退" in message for message in words)
    assert any("百分百" in message for message in words)


def test_check_banned_passes_clean_text() -> None:
    text = "想确认一下你这周哪天有空，我们一起看看课程节奏是否合适。"

    assert check_banned(text) == []


def test_every_banned_phrase_is_detected() -> None:
    for phrase in BANNED_PHRASES:
        issues = check_banned(f"这句话里藏了{phrase}两个字眼")
        assert issues, f"禁用词「{phrase}」没有被拦下"


def test_build_citations_payload_normalizes_shape() -> None:
    hits = [
        {"text": "退费在 15 个工作日内原路退回。", "source": "policy.md", "score": 0.81234},
        {"text": "  ", "source": "empty.md", "score": 0.5},
        {"text": "没有来源的片段"},
        "不是字典",
    ]

    citations = build_citations_payload(hits)

    assert len(citations) == 2
    assert citations[0] == {
        "source": "policy.md",
        "text": "退费在 15 个工作日内原路退回。",
        "score": 0.8123,
    }
    assert citations[1] == {"source": "", "text": "没有来源的片段", "score": 0.0}
    assert all(set(item) == {"source", "text", "score"} for item in citations)


def test_validate_confirm_requires_objection_and_date() -> None:
    issues = validate_confirm(objection="", next_followup_at="")

    locations = [issue.location for issue in issues]
    assert "objection" in locations
    assert "next_followup_at" in locations


def test_validate_confirm_rejects_bad_date_and_unknown_objection() -> None:
    issues = validate_confirm(objection="hobby", next_followup_at="2026/11/20")

    messages = "；".join(issue.message for issue in issues)
    assert "客户异议必须是以下之一" in messages
    assert "下次跟进时间必须是 YYYY-MM-DD 格式" in messages


def test_validate_confirm_checks_draft_text_when_given() -> None:
    issues = validate_confirm(objection="price", next_followup_at="", conclusion="")

    assert any(issue.location == "conclusion" for issue in issues)


def test_validate_confirm_passes_with_valid_input() -> None:
    assert validate_confirm(objection="price", next_followup_at="2026-11-20") == []


# ---------------------------------------------------------------- 日期规则（真模型暴露）


def test_check_dates_rejects_a_past_date() -> None:
    """草稿写「11月15日」而今天是 11-18：约一个已经过去的日子，必须拦下。"""
    issues = check_dates("11月15日现场演示方便吗？", today=TODAY)

    assert len(issues) == 1
    assert issues[0].location == "draft.text"
    assert "早于今天" in issues[0].message
    assert "11月15日" in issues[0].message


def test_check_dates_accepts_the_agreed_date() -> None:
    """与客户已约定时间一致：放行（这是我们要写的那种话术）。"""
    assert check_dates("就按11月25日那天下午三点。", today=TODAY, appointment_at="2026-11-25") == []


def test_check_dates_rejects_a_date_that_disagrees_with_the_agreement() -> None:
    """客户约的是 11-25，草稿写了别的日子：拦下，并说清两边各是什么。"""
    issues = check_dates("11月28日现场演示方便吗？", today=TODAY, appointment_at="2026-11-25")

    assert len(issues) == 1
    assert "不一致" in issues[0].message
    assert "11月28日" in issues[0].message and "2026-11-25" in issues[0].message


def test_check_dates_allows_future_date_when_nothing_is_agreed() -> None:
    """还没约定时间时，未来的日子不算违规（只有过去的日子和「与约定不符」才拦）。"""
    assert check_dates("11月28日现场演示方便吗？", today=TODAY, appointment_at="") == []


def test_check_dates_allows_open_question_without_concrete_date() -> None:
    """开放式提问（「您哪天方便？」）不含具体日期，天然不触发日期检查。"""
    assert (
        check_dates("您哪天方便？我这边随时配合。", today=TODAY, appointment_at="2026-11-25") == []
    )


def test_find_dates_reads_iso_and_chinese_forms() -> None:
    """两种写法都要认出来：`YYYY-MM-DD` 与 `M月D日`。"""
    found = find_dates("2026-12-01 或者 12月3日都行", today=TODAY)

    assert [item.value for item in found] == [date(2026, 12, 1), date(2026, 12, 3)]


def test_find_dates_rolls_january_forward_to_next_year() -> None:
    """11 月里说「1月5日」指的是明年：不该被当成十个月前的日子拦下来。"""
    found = find_dates("1月5日再聊", today=TODAY)

    assert [item.value for item in found] == [date(2027, 1, 5)]
