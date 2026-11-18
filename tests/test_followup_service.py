"""跟进用例的行为测试：临时库，记录成功、回查、线索不存在三种情况。"""

import itertools
from pathlib import Path

from salescopilot.adapters.db.sqlite import SqliteFollowupRepository, SqliteLeadRepository
from salescopilot.domain.models import Lead
from salescopilot.services.followup import FollowupService

VALID = {
    "conclusion": "客户说下周再谈",
    "objection": "price",
    "next_followup_at": "2026-11-20",
}


def _build(tmp_path: Path) -> tuple[FollowupService, SqliteLeadRepository]:
    db_path = tmp_path / "followup.sqlite3"
    leads = SqliteLeadRepository(db_path)
    followups = SqliteFollowupRepository(db_path)
    counter = itertools.count(1)
    service = FollowupService(
        leads, followups, clock=lambda: f"2026-11-14T10:00:{next(counter):02d}+00:00"
    )
    return service, leads


def _add_lead(leads: SqliteLeadRepository, lead_id: str) -> None:
    leads.add(
        Lead(
            id=lead_id,
            name="线索-跟进",
            contact="13800001111",
            source="inbound",
            stage="intake",
            owner=None,
            created_at="2026-11-14T09:00:00+00:00",
        )
    )


def test_record_then_list(tmp_path: Path) -> None:
    service, leads = _build(tmp_path)
    _add_lead(leads, "lead-1")

    ok, issues, item = service.record("lead-1", **VALID)

    assert ok is True
    assert issues == []
    assert item is not None
    assert item.lead_id == "lead-1"
    assert item.conclusion == VALID["conclusion"]

    stored = service.list_for("lead-1")
    assert len(stored) == 1
    assert stored[0].objection == "price"
    assert stored[0].next_followup_at == "2026-11-20"


def test_record_invalid_three_elements_rejected(tmp_path: Path) -> None:
    service, leads = _build(tmp_path)
    _add_lead(leads, "lead-1")

    ok, issues, item = service.record(
        "lead-1", conclusion="  ", objection="budget", next_followup_at="2026/11/20"
    )

    assert ok is False
    assert item is None
    assert [issue.location for issue in issues] == [
        "conclusion",
        "objection",
        "next_followup_at",
    ]
    assert service.list_for("lead-1") == []


def test_record_unknown_lead_rejected(tmp_path: Path) -> None:
    service, _ = _build(tmp_path)

    ok, issues, item = service.record("missing", **VALID)

    assert ok is False
    assert item is None
    assert issues[0].location == "lead_id"
    assert issues[0].message == "线索不存在"


def test_list_for_is_chronological(tmp_path: Path) -> None:
    service, leads = _build(tmp_path)
    _add_lead(leads, "lead-1")

    service.record("lead-1", **VALID)
    service.record("lead-1", **{**VALID, "conclusion": "客户要求先看资料"})

    stored = service.list_for("lead-1")
    assert [item.at for item in stored] == sorted(item.at for item in stored)
    assert stored[1].conclusion == "客户要求先看资料"


def test_value_provided_roundtrip(tmp_path: Path) -> None:
    """「本次提供的价值」写进去能原样读回（前后空格去掉）。"""
    service, leads = _build(tmp_path)
    _add_lead(leads, "lead-1")

    ok, issues, item = service.record("lead-1", **VALID, value_provided="  同类客户案例  ")

    assert (ok, issues) == (True, [])
    assert item is not None
    assert item.value_provided == "同类客户案例"
    assert service.list_for("lead-1")[0].value_provided == "同类客户案例"


def test_empty_value_provided_is_allowed_and_marked(tmp_path: Path) -> None:
    """不填价值不算错：落库为空串，等于显式标记「这次是无价值跟进」（供复盘统计）。"""
    service, leads = _build(tmp_path)
    _add_lead(leads, "lead-1")

    ok, issues, item = service.record("lead-1", **VALID)  # 不传 value_provided
    ok_blank, issues_blank, blank_item = service.record(
        "lead-1", **{**VALID, "conclusion": "客户没回"}, value_provided="   "
    )

    assert (ok, issues) == (True, [])
    assert (ok_blank, issues_blank) == (True, [])
    assert item is not None and item.value_provided == ""
    assert blank_item is not None and blank_item.value_provided == ""
    # 两条都落了库：空值不是「没记录」，是「记录了但没给价值」
    assert [item.value_provided for item in service.list_for("lead-1")] == ["", ""]
