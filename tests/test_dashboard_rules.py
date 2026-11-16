"""仪表盘纯规则的逐项断言：8 个字段各有明确口径，除数为 0 时是 0.0，关闭阶段被排除。

固定数据 + 固定 now，全程无 IO；阈值只写在用例里的模板上（证明没有代码里的隐藏常量）。
"""

from datetime import UTC, datetime

from salescopilot.domain.dashboard import Dashboard, compute_dashboard
from salescopilot.domain.models import INITIAL_STAGE, Followup, Lead

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


def _lead(
    lead_id: str,
    *,
    stage: str,
    owner: str | None,
    created_at: str,
) -> Lead:
    return Lead(
        id=lead_id,
        name=f"线索-{lead_id}",
        contact=f"1380000{lead_id}",
        source="inbound",
        stage=stage,
        owner=owner,
        created_at=created_at,
    )


def _followup(lead_id: str, *, at: str, next_followup_at: str) -> Followup:
    return Followup(
        id=f"followup-{lead_id}",
        lead_id=lead_id,
        conclusion="客户再考虑",
        objection="price",
        next_followup_at=next_followup_at,
        at=at,
    )


def _fixture() -> tuple[list[Lead], list[Followup]]:
    leads = [
        # 26 小时前创建、还在入库阶段：首响红；创建超过 24h 但没离开 intake → 首响率分子不计。
        _lead("1", stage=INITIAL_STAGE, owner=None, created_at="2026-11-15T10:00:00+00:00"),
        # 6 天前创建、已离开 intake、没有任何跟进：首响率分子计入；未超过 7 天，不算久未跟进。
        _lead("2", stage="first_contact", owner="王五", created_at="2026-11-10T12:00:00+00:00"),
        # 有跟进但没填下次跟进时间、已 15 天没新增：灰 + 无下次跟进占比分子。
        _lead("3", stage="qualify", owner=None, created_at="2026-11-14T12:00:00+00:00"),
        # 已关闭：不进「未关闭」的分子分母，也不出提醒。
        _lead("4", stage="close", owner="李四", created_at="2026-11-01T00:00:00+00:00"),
        # 恰好创建满 24 小时：不进首响率分母；约定日 + 2 小时已过 → 黄。
        _lead("5", stage="qualify", owner="赵六", created_at="2026-11-15T12:00:00+00:00"),
    ]
    followups = [
        _followup("3", at="2026-11-01T10:00:00+00:00", next_followup_at=""),
        _followup("4", at="2026-10-01T10:00:00+00:00", next_followup_at=""),
        _followup("5", at="2026-11-15T12:00:00+00:00", next_followup_at="2026-11-15"),
    ]
    return leads, followups


def test_dashboard_fields_match_documented_formulas() -> None:
    leads, followups = _fixture()

    dashboard = compute_dashboard(leads=leads, followups=followups, template=_template(), now=NOW)

    assert isinstance(dashboard, Dashboard)
    # 全部线索（含已关闭）
    assert dashboard.total == 5
    # 没有归属人的线索：1 号与 3 号
    assert dashboard.pool == 2
    # created_at 在 [now-7天, now]：1/2/3/5 号（4 号是 11-01，早于 11-09 12:00）
    assert dashboard.new_this_week == 4
    # 跟进 at 在 [now-7天, now]：只有 5 号那条（11-15）
    assert dashboard.followups_this_week == 1
    # 分子=创建超过 24h 且离开 intake（2/3/4 号）；分母=创建超过 24h（1/2/3/4 号，5 号刚好 24h）
    assert dashboard.first_response_24h_rate == 0.75
    # 分子=未关闭里「没有下次跟进时间且超过 7 天没新增」（3 号）；分母=未关闭（1/2/3/5 号）
    assert dashboard.stale_ratio == 0.25
    # 超过 7 天无新增跟进、且未关闭：只有 3 号
    assert dashboard.silent_count == 1
    # 1 号首响红、5 号反馈窗口黄、3 号灰；键恒存在
    assert dashboard.reminders_by_level == {"red": 1, "yellow": 1, "gray": 1}


def test_empty_book_returns_zeroes_not_nan() -> None:
    """一条线索都没有：除数为 0 的两个比率都是 0.0（不是 NaN）。"""
    dashboard = compute_dashboard(leads=[], followups=[], template=_template(), now=NOW)

    assert dashboard == Dashboard(
        total=0,
        pool=0,
        new_this_week=0,
        followups_this_week=0,
        first_response_24h_rate=0.0,
        stale_ratio=0.0,
        silent_count=0,
        reminders_by_level={"red": 0, "yellow": 0, "gray": 0},
    )


def test_closed_lead_is_excluded_from_open_denominators_and_reminders() -> None:
    """只剩一条关闭线索：未关闭分母为 0 → 比率 0.0；关闭线索不出任何提醒。"""
    leads = [
        _lead("9", stage="close", owner=None, created_at="2026-01-01T00:00:00+00:00"),
    ]
    followups = [_followup("9", at="2026-02-01T10:00:00+00:00", next_followup_at="")]

    dashboard = compute_dashboard(leads=leads, followups=followups, template=_template(), now=NOW)

    assert dashboard.total == 1
    assert dashboard.stale_ratio == 0.0
    assert dashboard.silent_count == 0
    assert dashboard.reminders_by_level == {"red": 0, "yellow": 0, "gray": 0}
    # 关闭线索仍算「已离开 intake」，但 1 月创建早已超过 24h → 首响率 1.0
    assert dashboard.first_response_24h_rate == 1.0
