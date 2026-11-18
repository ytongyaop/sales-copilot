"""周复盘纯规则测试：只喂数据、不看时钟、不碰库。

五条判据：
1. 窗口是闭区间：正好 7 天前那一秒算进，再早一秒不算；
2. `advanced` 数阶段事件、`closed` 只数走进终止阶段的线索；
3. `grading_changes` 只报「人工值真的换了」，重复确认同一档不算，跨窗口也能比出 `from`；
4. `no_value_followups` 只统计空值（空 = 无价值跟进，不是没记录）；
5. `missing_top` 按客户数降序、最多三块，同一块里缺多个字段只算一家。
"""

from datetime import UTC, datetime

from salescopilot.domain.card import Card
from salescopilot.domain.models import Followup, GradingRecord, Lead, StageEvent
from salescopilot.domain.reminders import Reminder
from salescopilot.domain.review import (
    QUESTIONS,
    REVIEW_WINDOW_DAYS,
    build_review,
    review_window,
    within_window,
)

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
# 正好落在窗口起点（now - 7 天）：闭区间，必须算进。
EDGE = "2026-11-11T09:00:00+00:00"
# 比窗口起点早一秒：不算。
JUST_OUT = "2026-11-11T08:59:59+00:00"
INSIDE = "2026-11-17T10:00:00+00:00"
LONG_AGO = "2026-10-01T10:00:00+00:00"


def _lead(lead_id: str, *, created_at: str = INSIDE, stage: str = "intake") -> Lead:
    return Lead(
        id=lead_id,
        name=f"客户-{lead_id}",
        contact="13800001111",
        source="referral",
        stage=stage,
        owner=None,
        created_at=created_at,
    )


def _event(lead_id: str, *, to_stage: str, at: str = INSIDE, from_stage: str = "intake"):
    return StageEvent(
        id=f"event-{lead_id}-{at}-{to_stage}",
        lead_id=lead_id,
        from_stage=from_stage,
        to_stage=to_stage,
        at=at,
    )


def _followup(lead_id: str, *, at: str = INSIDE, value: str = "") -> Followup:
    return Followup(
        id=f"followup-{lead_id}-{at}",
        lead_id=lead_id,
        conclusion="这周又聊了一次",
        objection="price",
        next_followup_at="2026-11-20",
        at=at,
        value_provided=value,
    )


def _grading(
    lead_id: str,
    *,
    at: str,
    ai: str | None = None,
    human: str | None = None,
    reason: str | None = None,
) -> GradingRecord:
    return GradingRecord(
        id=f"grade-{lead_id}-{at}-{human or ai}",
        lead_id=lead_id,
        mode="propose" if human is None else "auto",
        at=at,
        ai_value=ai,
        human_value=human,
        reason=reason,
    )


def _card(sections: list[str], *, label: str = "分区") -> Card:
    """一张缺项信息卡：每个分区给一条缺项（`section_label` 带分区名便于断言）。"""
    missing = [
        {
            "section": section,
            "section_label": f"{label}-{section}",
            "field": f"{section}_field",
            "label": "字段名",
            "question": "下次该问什么？",
        }
        for section in sections
    ]
    return Card(sections=[], coverage=0.0, missing=missing)


def _summary(
    *,
    leads=(),
    stage_events=(),
    followups=(),
    grading_history=(),
    reminders=(),
    cards=(),
):
    return build_review(
        leads=list(leads),
        stage_events=list(stage_events),
        followups=list(followups),
        grading_history=list(grading_history),
        reminders=list(reminders),
        cards=list(cards),
        now=NOW,
    )


def test_window_is_closed_on_both_ends() -> None:
    """7 天前那一秒算进、再早一秒不算；窗口两端原样报出。"""
    summary = _summary(leads=[_lead("edge", created_at=EDGE), _lead("out", created_at=JUST_OUT)])

    assert summary.new_leads == 1
    assert summary.window_start == EDGE
    assert summary.window_end == "2026-11-18T09:00:00+00:00"
    assert within_window(EDGE, *review_window(NOW)) is True
    assert within_window(JUST_OUT, *review_window(NOW)) is False
    assert REVIEW_WINDOW_DAYS == 7


def test_window_rejects_unparseable_time() -> None:
    """时间解析不了就不进窗口统计（宁可不数，也不编一个时间）。"""
    assert within_window("", *review_window(NOW)) is False
    assert within_window("下周三", *review_window(NOW)) is False
    assert within_window(LONG_AGO, *review_window(NOW)) is False


def test_advanced_counts_events_and_closed_counts_leads() -> None:
    """`advanced` 数窗口内事件条数；`closed` 只数走进终止阶段（close）的线索。"""
    summary = _summary(
        leads=[_lead("a"), _lead("b")],
        stage_events=[
            _event("a", to_stage="first_contact"),
            _event("b", to_stage="qualify"),
            _event("a", to_stage="close"),
            _event("a", to_stage="close", at=JUST_OUT),  # 窗口外，不数
        ],
    )

    assert summary.advanced == 3
    assert summary.closed == 1


def test_grading_changes_only_reports_real_shifts() -> None:
    """A → A 重复确认不算改档；A → B 算，且 `from` 取上一条（可以是窗口外的 AI 值）。"""
    summary = _summary(
        leads=[_lead("a"), _lead("b")],
        grading_history=[
            # a：窗口外 AI 提议 A，窗口内人先确认 A（同值不算），再改成 B（算）
            _grading("a", at=LONG_AGO, ai="A"),
            _grading("a", at="2026-11-17T09:00:00+00:00", ai="A", human="A"),
            _grading("a", at="2026-11-17T11:00:00+00:00", human="B", reason="预算已谈清"),
            # b：窗口内的 AI 提议本身不是改档（`human_value` 为空）
            _grading("b", at="2026-11-16T09:00:00+00:00", ai="C"),
        ],
    )

    assert len(summary.grading_changes) == 1
    change = summary.grading_changes[0]
    assert change["lead_id"] == "a"
    assert change["lead_name"] == "客户-a"
    assert (change["from"], change["to"]) == ("A", "B")
    assert change["reason"] == "预算已谈清"


def test_first_time_grading_is_not_a_change() -> None:
    """第一次给人定档（前面没有任何有值的留痕）算定档，不算改判。"""
    summary = _summary(
        leads=[_lead("a"), _lead("b")],
        grading_history=[
            _grading("a", at=INSIDE, human="A"),
            _grading("b", at=INSIDE, ai="B", human="B"),
        ],
    )

    assert summary.grading_changes == []


def test_no_value_followups_only_counts_empty_values() -> None:
    """只有 `value_provided` 为空的跟进才进「空转」列表，窗口外的空值不算。"""
    summary = _summary(
        leads=[_lead("a")],
        followups=[
            _followup("a", at=INSIDE),
            _followup("a", at="2026-11-16T10:00:00+00:00", value="报价区间说明"),
            _followup("a", at=JUST_OUT),
        ],
    )

    assert [item["lead_id"] for item in summary.no_value_followups] == ["a"]
    assert summary.no_value_followups[0]["conclusion"] == "这周又聊了一次"
    assert summary.no_value_followups[0]["lead_name"] == "客户-a"


def test_needs_info_counts_latest_grading_per_lead() -> None:
    """`needs_info_leads` 按最近一次有效档位算：后来人定了 C，就不再算「待补全」。"""
    summary = _summary(
        leads=[_lead("a"), _lead("b"), _lead("c")],
        grading_history=[
            _grading("a", at=INSIDE, ai="needs_info"),
            _grading("b", at=INSIDE, ai="needs_info"),
            _grading("b", at="2026-11-17T11:00:00+00:00", human="C"),
            _grading("c", at=INSIDE, ai="B"),
        ],
    )

    assert summary.needs_info_leads == 1


def test_missing_top_sorts_and_truncates() -> None:
    """缺项排行：5/3/1/1 家 → 取前三块；同一块里缺两个字段的客户只算一家。"""
    cards = (
        [_card(["basic"]) for _ in range(5)]
        + [_card(["needs"]) for _ in range(3)]
        + [_card(["decision"])]
        + [_card(["followup"])]
        + [_card(["basic", "basic"])]  # 同一块缺两次：仍只算一家 basic
    )

    top = _summary(cards=cards).missing_top

    assert [item["section"] for item in top] == ["basic", "needs", "decision"]
    assert [item["count"] for item in top] == [6, 3, 1]
    assert top[0]["section_label"] == "分区-basic"


def test_summary_always_offers_three_questions() -> None:
    """空数据也返回一张完整复盘：数字全 0，三个问题照给（顺序固定）。"""
    summary = _summary()

    assert (summary.new_leads, summary.advanced, summary.closed) == (0, 0, 0)
    assert summary.stuck == []
    assert [item["key"] for item in summary.questions] == [item["key"] for item in QUESTIONS]
    assert summary.questions[0]["text"] == "这周你把哪家从 B 提到了 A？为什么？"


def test_stuck_items_carry_lead_names() -> None:
    """卡点原样带线索名：前端显示「谁卡住了」不必再查一次。"""
    summary = _summary(
        leads=[_lead("a")],
        reminders=[Reminder("a", "first_response", "red", "超过 30 分钟未首联", 600)],
    )

    assert summary.stuck == [
        {
            "lead_id": "a",
            "lead_name": "客户-a",
            "kind": "first_response",
            "level": "red",
            "message": "超过 30 分钟未首联",
        }
    ]
