"""提醒规则的纯函数测试：三类提醒各触发一次、关闭阶段不提醒、同线索取最严重、阈值来自模板。

全程无 IO：`now` 固定注入，模板阈值直接写在用例里（换阈值就换口径，证明没有代码里的隐藏常量）。
"""

from datetime import UTC, datetime

from salescopilot.domain.models import INITIAL_STAGE, Followup, Lead
from salescopilot.domain.reminders import (
    FEEDBACK_WINDOW,
    FIRST_RESPONSE,
    STALE_FOLLOWUP,
    compute_reminders,
)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def _template(**thresholds) -> dict:
    base = {
        "first_response_minutes": 30,
        "feedback_window_hours": 2,
        "stale_followup_days": 7,
        "zombie_ratio_max": 0.05,
    }
    base.update(thresholds)
    return {
        "template": "test",
        "name": "测试模板",
        "thresholds": base,
        "flow": [{"block": "intake"}],
    }


def _lead(**overrides) -> Lead:
    base = {
        "id": "lead-1",
        "name": "线索-测试",
        "contact": "13800001111",
        "source": "inbound",
        "stage": INITIAL_STAGE,
        "owner": None,
        "created_at": "2026-11-16T10:00:00+00:00",
    }
    base.update(overrides)
    return Lead(**base)


def _followup(**overrides) -> Followup:
    base = {
        "id": "followup-1",
        "lead_id": "lead-1",
        "conclusion": "客户再考虑",
        "objection": "price",
        "next_followup_at": "2026-11-20",
        "at": "2026-11-15T10:00:00+00:00",
    }
    base.update(overrides)
    return Followup(**base)


def _remind(leads: list[Lead], followups: list[Followup], template: dict):
    return compute_reminders(leads=leads, followups=followups, template=template, now=NOW)


def test_first_response_turns_red_after_template_minutes() -> None:
    """入库阶段创建 2 小时、阈值 30 分钟 → 红灯，消息里写的就是模板里的 30。"""
    reminders = _remind([_lead()], [], _template())

    assert len(reminders) == 1
    reminder = reminders[0]
    assert reminder.kind == FIRST_RESPONSE
    assert reminder.level == "red"
    assert reminder.message == "超过 30 分钟未首联"
    assert reminder.since_minutes == 120


def test_feedback_window_turns_yellow_after_agreed_date() -> None:
    """约定日已过、阶段没到 close → 黄灯，天数按日历天算。"""
    reminders = _remind(
        [_lead(stage="qualify", created_at="2026-11-01T09:00:00+00:00")],
        [_followup(next_followup_at="2026-11-12", at="2026-11-10T10:00:00+00:00")],
        _template(),
    )

    assert len(reminders) == 1
    reminder = reminders[0]
    assert reminder.kind == FEEDBACK_WINDOW
    assert reminder.level == "yellow"
    assert reminder.message == "约定时间已过 4 天未更新"


def test_missing_next_followup_at_turns_gray() -> None:
    """最近一次跟进没填下次跟进时间 → 灰灯「没有下次跟进时间」。"""
    reminders = _remind(
        [_lead(stage="qualify", created_at="2026-11-01T09:00:00+00:00")],
        [_followup(next_followup_at="", at="2026-11-16T09:00:00+00:00")],
        _template(),
    )

    assert len(reminders) == 1
    reminder = reminders[0]
    assert reminder.kind == STALE_FOLLOWUP
    assert reminder.level == "gray"
    assert reminder.message == "没有下次跟进时间"


def test_stale_followup_turns_gray_when_no_new_record_for_days() -> None:
    """一条跟进记录都没有、创建已 15 天 → 灰灯，阈值同样来自模板。"""
    reminders = _remind(
        [_lead(stage="first_contact", created_at="2026-11-01T00:00:00+00:00")],
        [],
        _template(),
    )

    assert len(reminders) == 1
    reminder = reminders[0]
    assert reminder.kind == STALE_FOLLOWUP
    assert reminder.level == "gray"
    assert reminder.message == "超过 7 天未新增跟进"
    assert reminder.since_minutes == 15 * 24 * 60 + 12 * 60


def test_closed_lead_never_gets_reminders() -> None:
    reminders = _remind(
        [_lead(stage="close", created_at="2026-01-01T00:00:00+00:00")],
        [_followup(next_followup_at="2026-02-01", at="2026-01-02T00:00:00+00:00")],
        _template(),
    )

    assert reminders == []


def test_most_severe_kind_wins_within_one_lead() -> None:
    """红线 + 灰线同时成立只报红线；黄线 + 灰线同时成立只报黄线。"""
    red_and_gray = _remind([_lead(created_at="2026-11-01T00:00:00+00:00")], [], _template())
    assert [item.kind for item in red_and_gray] == [FIRST_RESPONSE]

    yellow_and_gray = _remind(
        [_lead(stage="qualify", created_at="2026-11-01T00:00:00+00:00")],
        [_followup(next_followup_at="2026-11-05", at="2026-11-02T00:00:00+00:00")],
        _template(),
    )
    assert [item.kind for item in yellow_and_gray] == [FEEDBACK_WINDOW]


def test_same_kind_is_reported_once() -> None:
    """同 kind 两条候选（没填下次时间 + 久未跟进）也只出一条。"""
    reminders = _remind(
        [_lead(stage="qualify", created_at="2026-11-01T00:00:00+00:00")],
        [_followup(next_followup_at="", at="2026-11-01T00:00:00+00:00")],
        _template(),
    )

    assert len(reminders) == 1
    assert reminders[0].kind == STALE_FOLLOWUP


def test_thresholds_come_from_template_not_code() -> None:
    """同一批线索换模板阈值就换结果：调大首响阈值红灯消失，调久未跟进阈值灰灯消失。"""
    lead = _lead(created_at="2026-11-16T10:00:00+00:00")

    assert [item.kind for item in _remind([lead], [], _template())] == [FIRST_RESPONSE]
    assert _remind([lead], [], _template(first_response_minutes=300)) == []

    idle_lead = _lead(stage="first_contact", created_at="2026-11-08T00:00:00+00:00")
    assert [item.kind for item in _remind([idle_lead], [], _template())] == [STALE_FOLLOWUP]
    assert _remind([idle_lead], [], _template(stale_followup_days=100)) == []
