"""推进用例的行为测试：临时库 + 注入模板加载器与时钟，一路推到终点再验历史。"""

import itertools
from collections.abc import Callable
from pathlib import Path

from salescopilot.adapters.db.sqlite import SqliteLeadRepository, SqliteStageEventRepository
from salescopilot.domain.models import Lead
from salescopilot.services.flow import FlowService

# 4 个块 = 3 次推进；块契约仍取自 domain/blocks.py。
TEMPLATE = {
    "template": "test_flow",
    "name": "测试流程",
    "thresholds": {},
    "flow": [
        {"block": "intake"},
        {"block": "first_contact"},
        {"block": "qualify"},
        {"block": "close"},
    ],
}

FIELDS = {
    "decision_maker": "张总",
    "need": "需要一套可配置的流程",
    "conclusion": "已确认合作",
}


def _clock() -> Callable[[], str]:
    """每秒递增的假时钟，保证阶段历史的 at 严格递增、顺序可断言。"""
    counter = itertools.count(1)
    return lambda: f"2026-11-14T10:00:{next(counter):02d}+00:00"


def _build(tmp_path: Path) -> tuple[FlowService, SqliteLeadRepository]:
    db_path = tmp_path / "flow.sqlite3"
    leads = SqliteLeadRepository(db_path)
    events = SqliteStageEventRepository(db_path)
    service = FlowService(leads, events, lambda key: TEMPLATE, clock=_clock())
    return service, leads


def _add_lead(leads: SqliteLeadRepository, lead_id: str) -> Lead:
    lead = Lead(
        id=lead_id,
        name="线索-测试",
        contact="13800001111",
        source="inbound",
        stage="intake",
        owner=None,
        created_at="2026-11-14T09:00:00+00:00",
        template="test_flow",
        fields=dict(FIELDS),
    )
    leads.add(lead)
    return lead


def test_advance_through_template_writes_history(tmp_path: Path) -> None:
    service, leads = _build(tmp_path)
    _add_lead(leads, "lead-1")

    for target in ("first_contact", "qualify", "close"):
        result = service.advance("lead-1", target)
        assert result.ok, result.issues
        assert result.lead is not None
        assert result.lead.stage == target

    history = service.history("lead-1")
    assert len(history) == 3
    assert [(event.from_stage, event.to_stage) for event in history] == [
        ("intake", "first_contact"),
        ("first_contact", "qualify"),
        ("qualify", "close"),
    ]
    assert [event.at for event in history] == sorted(event.at for event in history)


def test_advance_from_terminal_stage_rejected(tmp_path: Path) -> None:
    service, leads = _build(tmp_path)
    _add_lead(leads, "lead-2")
    for target in ("first_contact", "qualify", "close"):
        assert service.advance("lead-2", target).ok

    result = service.advance("lead-2", "close")

    assert result.ok is False
    assert result.lead is not None
    assert result.lead.stage == "close"
    assert [issue.location for issue in result.issues] == ["target_stage"]
    assert "已是最后一步" in result.issues[0].message
    assert len(service.history("lead-2")) == 3


def test_advance_missing_lead_returns_issue(tmp_path: Path) -> None:
    service, _ = _build(tmp_path)

    result = service.advance("missing", "first_contact")

    assert result.ok is False
    assert result.lead is None
    assert result.issues[0].location == "lead_id"
    assert result.issues[0].message == "线索不存在"
