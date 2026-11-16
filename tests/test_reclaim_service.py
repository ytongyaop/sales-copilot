"""自动回收用例：超时回收、未超时不动、已是公海不再回收（幂等：两次扫描只一条事件）。"""

from pathlib import Path

from salescopilot.adapters.db.sqlite import SqliteAssignmentEventRepository, SqliteLeadRepository
from salescopilot.domain.models import INITIAL_STAGE, Lead
from salescopilot.services.reminder_service import ReclaimService

NOW = "2026-11-16T12:00:00+00:00"


def _template() -> dict:
    return {
        "template": "test",
        "name": "测试模板",
        "thresholds": {
            "first_response_minutes": 30,
            "feedback_window_hours": 2,
            "stale_followup_days": 7,
            "zombie_ratio_max": 0.05,
        },
        "flow": [{"block": "intake"}],
    }


def _loader(key: str) -> dict:
    return _template()


def _fixed_clock() -> str:
    """固定时钟：回收时间与事件时间可断言。"""
    return NOW


def _service(tmp_path: Path, *, owner: str | None, created_at: str, stage: str = INITIAL_STAGE):
    db_path = tmp_path / "reclaim.sqlite3"
    leads = SqliteLeadRepository(db_path)
    leads.add(
        Lead(
            id="lead-1",
            name="线索-测试",
            contact="13800001111",
            source="inbound",
            stage=stage,
            owner=owner,
            created_at=created_at,
        )
    )
    events = SqliteAssignmentEventRepository(db_path)
    return ReclaimService(leads, events, _loader, clock=_fixed_clock), leads, events


def test_overdue_lead_is_reclaimed_and_event_is_kept(tmp_path: Path) -> None:
    """入库阶段 2 小时还没首联、却挂在王五名下 → 放回公海，并写一条 auto_reclaim 事件。"""
    service, leads, events = _service(
        tmp_path, owner="王五", created_at="2026-11-16T10:00:00+00:00"
    )

    reclaimed = service.scan()

    assert reclaimed == [
        {"lead_id": "lead-1", "from_owner": "王五", "reason": "auto_reclaim", "at": NOW}
    ]
    assert leads.get("lead-1").owner is None
    recorded = events.list_for("lead-1")
    assert [(item.from_owner, item.to_owner, item.reason) for item in recorded] == [
        ("王五", None, "auto_reclaim")
    ]


def test_lead_within_window_is_untouched(tmp_path: Path) -> None:
    """创建才 10 分钟、还没到 30 分钟首响阈值 → 什么都不做。"""
    service, leads, events = _service(
        tmp_path, owner="王五", created_at="2026-11-16T11:50:00+00:00"
    )

    assert service.scan() == []
    assert leads.get("lead-1").owner == "王五"
    assert events.list_for("lead-1") == []


def test_lead_not_in_intake_stage_is_untouched(tmp_path: Path) -> None:
    """已经走到首联之后的线索不再按首响超时回收（那是另一类提醒的事）。"""
    service, leads, _ = _service(
        tmp_path,
        owner="王五",
        created_at="2026-11-01T00:00:00+00:00",
        stage="first_contact",
    )

    assert service.scan() == []
    assert leads.get("lead-1").owner == "王五"


def test_scan_is_idempotent(tmp_path: Path) -> None:
    """连续扫两次：第一次回收，第二次没有可回收的，事件总数仍是 1（不重复写）。"""
    service, _, events = _service(tmp_path, owner="王五", created_at="2026-11-16T10:00:00+00:00")

    first = service.scan()
    second = service.scan()

    assert len(first) == 1
    assert second == []
    assert len(events.list_for("lead-1")) == 1
