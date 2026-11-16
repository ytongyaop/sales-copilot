"""归属用例的行为测试：认领 / 重复认领被拒 / 释放后再认领 / 事件留痕（claim、release、manual）。

全程离线：临时库 + 递增假时钟；不碰接口、不碰模型。
"""

import itertools
from collections.abc import Callable
from pathlib import Path

from salescopilot.adapters.db.sqlite import SqliteAssignmentEventRepository, SqliteLeadRepository
from salescopilot.domain.models import INITIAL_STAGE, Lead
from salescopilot.services.assignment import AssignmentService


def _clock() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"2026-11-16T10:00:{next(counter):02d}+00:00"


def _service(tmp_path: Path, *, owner: str | None = None) -> AssignmentService:
    db_path = tmp_path / "assignment.sqlite3"
    leads = SqliteLeadRepository(db_path)
    leads.add(
        Lead(
            id="lead-1",
            name="线索-测试",
            contact="13800001111",
            source="inbound",
            stage=INITIAL_STAGE,
            owner=owner,
            created_at="2026-11-16T09:00:00+00:00",
        )
    )
    return AssignmentService(leads, SqliteAssignmentEventRepository(db_path), clock=_clock())


def test_claim_moves_lead_out_of_pool_and_records_event(tmp_path: Path) -> None:
    service = _service(tmp_path)

    lead, issues = service.claim("lead-1", "王五")

    assert issues == []
    assert lead is not None
    assert lead.owner == "王五"
    assert service.pool() == []
    events = service.events_for("lead-1")
    assert [(event.from_owner, event.to_owner, event.reason) for event in events] == [
        (None, "王五", "claim")
    ]


def test_claim_twice_is_rejected_without_new_event(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.claim("lead-1", "王五")

    lead, issues = service.claim("lead-1", "李四")

    assert lead is not None
    assert lead.owner == "王五"
    assert [(issue.location, issue.message) for issue in issues] == [
        ("owner", "该线索已归属「王五」")
    ]
    assert len(service.events_for("lead-1")) == 1


def test_claim_rejects_blank_owner(tmp_path: Path) -> None:
    service = _service(tmp_path)

    lead, issues = service.claim("lead-1", "   ")

    assert lead is not None
    assert lead.owner is None
    assert [(issue.location, issue.message) for issue in issues] == [("owner", "归属人不能为空")]


def test_claim_unknown_lead_returns_issue(tmp_path: Path) -> None:
    service = _service(tmp_path)

    lead, issues = service.claim("missing", "王五")

    assert lead is None
    assert [(issue.location, issue.message) for issue in issues] == [("lead_id", "线索不存在")]


def test_release_returns_lead_to_pool_and_can_be_claimed_again(tmp_path: Path) -> None:
    service = _service(tmp_path, owner="王五")

    lead, issues = service.release("lead-1")
    assert issues == []
    assert lead is not None and lead.owner is None
    assert [item.id for item in service.pool()] == ["lead-1"]

    again, again_issues = service.claim("lead-1", "李四")
    assert again_issues == []
    assert again is not None and again.owner == "李四"

    events = service.events_for("lead-1")
    assert [(event.from_owner, event.to_owner, event.reason) for event in events] == [
        ("王五", None, "release"),
        (None, "李四", "claim"),
    ]


def test_release_rejects_lead_already_in_pool(tmp_path: Path) -> None:
    service = _service(tmp_path)

    lead, issues = service.release("lead-1")

    assert lead is not None
    assert [(issue.location, issue.message) for issue in issues] == [("owner", "该线索尚未归属")]
    assert service.events_for("lead-1") == []


def test_release_with_manual_reason_is_recorded(tmp_path: Path) -> None:
    """人工手工调整走 `manual`，与业务流程触发的 `release` 分开记，回查时能说清是谁动的。"""
    service = _service(tmp_path, owner="王五")

    lead, issues = service.release("lead-1", reason="manual")

    assert issues == []
    assert lead is not None and lead.owner is None
    assert service.events_for("lead-1")[0].reason == "manual"


def test_release_rejects_unknown_reason(tmp_path: Path) -> None:
    service = _service(tmp_path, owner="王五")

    lead, issues = service.release("lead-1", reason="whatever")

    assert lead is not None
    assert lead.owner == "王五"
    assert issues[0].location == "reason"
