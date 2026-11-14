"""话术规则的行为测试：禁用词命中/未命中、引用形状、确认三要素校验。"""

from salescopilot.domain.draft import (
    BANNED_PHRASES,
    build_citations_payload,
    check_banned,
    validate_confirm,
)


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
