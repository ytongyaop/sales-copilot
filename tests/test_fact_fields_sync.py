"""提炼 → 线索字段的同步（假模型 + 临时库，全程离线）。

三条反证：
1. 提炼出决策人 / 核心需求 → 线索字段被补上，`field_updates` 说得清补了哪些；
2. 已有值不被覆盖（人填的、之前提炼的都算已填）；
3. 过不了字段校验的事实值（约定时间不是 YYYY-MM-DD）不自动补，但事实本身照常落库。
"""

from pathlib import Path

from salescopilot.adapters.db.sqlite import (
    SqliteExtractRunRepository,
    SqliteFactRepository,
    SqliteLeadRepository,
    SqliteRawItemRepository,
)
from salescopilot.adapters.llm.fake import FakeExtractor
from salescopilot.domain.facts import fields_from_facts
from salescopilot.domain.models import INITIAL_STAGE, Fact, Lead
from salescopilot.services.ingest import IngestService

CONTENT = "李校长想提高数学成绩，预算 1 万，下周一见面，他还在考虑"


def _build(tmp_path: Path) -> tuple[IngestService, SqliteLeadRepository, SqliteFactRepository]:
    db = tmp_path / "sync.sqlite3"
    leads = SqliteLeadRepository(db)
    facts = SqliteFactRepository(db)
    service = IngestService(
        leads,
        SqliteRawItemRepository(db),
        facts,
        SqliteExtractRunRepository(db),
        FakeExtractor(),
    )
    return service, leads, facts


def _add_lead(leads: SqliteLeadRepository, *, fields: dict[str, str] | None = None) -> Lead:
    lead = Lead(
        id="lead-1",
        name="李校长",
        contact="13800001111",
        source="referral",
        stage=INITIAL_STAGE,
        owner=None,
        created_at="2026-11-16T09:00:00+00:00",
        fields={} if fields is None else dict(fields),
    )
    leads.add(lead)
    return lead


def _extract(service: IngestService, lead_id: str):
    raw, duplicated, issues = service.ingest(lead_id, content=CONTENT, source="inbound")
    assert raw is not None and issues == [] and duplicated is False
    return service.extract(raw.id)


def test_extract_fills_missing_lead_fields(tmp_path: Path) -> None:
    service, leads, _ = _build(tmp_path)
    _add_lead(leads)

    outcome = _extract(service, "lead-1")

    assert outcome.ok is True
    # 约定时间是「下周一」，不是 ISO 日期 → 不自动补；其余三项补上
    assert outcome.field_updates == {
        "need": "待确认决策",
        "decision_maker": "校长",
        "amount": "1 万",
    }

    lead = leads.get("lead-1")
    assert lead is not None
    assert lead.fields == outcome.field_updates


def test_existing_value_is_not_overwritten(tmp_path: Path) -> None:
    service, leads, _ = _build(tmp_path)
    _add_lead(leads, fields={"need": "客户自己说的需求"})

    outcome = _extract(service, "lead-1")

    lead = leads.get("lead-1")
    assert lead is not None
    assert lead.fields["need"] == "客户自己说的需求"
    assert "need" not in outcome.field_updates
    # 其余缺的照常补
    assert lead.fields["decision_maker"] == "校长"


def test_non_iso_appointment_stays_a_fact_only(tmp_path: Path) -> None:
    service, leads, facts = _build(tmp_path)
    _add_lead(leads)

    _extract(service, "lead-1")

    lead = leads.get("lead-1")
    assert lead is not None
    assert "appointment_at" not in lead.fields
    # 事实层不按字段格式判：约定时间这条事实照常落库，留给人工补录成 ISO 日期
    fact = facts.get_by_field("lead-1", "appointment_at")
    assert fact is not None and fact.value == "下周一"


def test_fields_from_facts_ignores_objection() -> None:
    """异议不进流程字段（`objection` 不是任何块的必填项）。"""
    objection = Fact(
        id="fact-1",
        lead_id="lead-1",
        raw_id="raw-1",
        field="objection",
        value="price",
        source="llm",
        source_span="贵",
        confidence=0.8,
        locked=False,
        at="2026-11-16T09:10:00+00:00",
    )

    assert fields_from_facts([objection], {}) == {}
