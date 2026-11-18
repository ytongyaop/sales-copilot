"""时间字段归一（T005.5）：模型吐「下周三」→ 落成 ISO 日期，原文片段仍留着。

三条判据（临时库 + 假模型，全程离线）：
1. 假模型吐 ISO 日期：要素进建议，`value` 是 `2026-11-25`，`source_span` 仍是原文那句；
2. 假模型吐非 ISO（`bad_date=True`）：该要素进 `dropped`，原因带 `YYYY-MM-DD`，
   其它要素一条都不受牵连；
3. 同意后 `fields.appointment_at` 被补成 ISO 日期，推进到 `appointment` 不再被
   「约定时间」卡住。
"""

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from salescopilot.adapters.db.sqlite import (
    SqliteFactRepository,
    SqliteLeadRepository,
    SqliteRawItemRepository,
    SqliteSortingBatchRepository,
    SqliteStageEventRepository,
)
from salescopilot.adapters.llm.fake import FakeSorting
from salescopilot.adapters.template.loader import load_all
from salescopilot.domain.models import INITIAL_STAGE, Lead
from salescopilot.services.flow import FlowService
from salescopilot.services.sorting import SortingService

# 真实模板：时间字段判定要按线索自己的模板（`card` 段 label）推导。
TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "salescopilot" / "templates"
TEMPLATES = load_all(TEMPLATES_DIR)

# 今天固定为 2026-11-18（周三）：假模型按「今天 + 7 天」给出 ISO 日期。
TODAY = "2026-11-18"
NEXT_WEEK = "2026-11-25"

# 四个块各一条要素：公司（basic）/ 需求（needs）/ 决策人（decision）/ 时间（followup）。
CONTENT = "华远建设有限公司的老板说预算三十万，下周三下午再聊"

LEAD_NAME = "华远建设有限公司"
LEAD_CONTACT = "13800001111"


def _template_loader(key: str) -> dict:
    return TEMPLATES[key]


def _clock() -> Callable[[], str]:
    """按秒递增的假时钟：日期部分固定是 `TODAY`，顺序稳定可断言。"""
    counter = itertools.count(1)
    return lambda: f"{TODAY}T10:00:{next(counter):02d}+00:00"


@dataclass(frozen=True)
class _Setup:
    """一套离线装置：一个客户 + 四条仓储 + 分拣服务（假模型）。"""

    service: SortingService
    leads: SqliteLeadRepository
    facts: SqliteFactRepository
    events: SqliteStageEventRepository
    lead: Lead
    db: Path


def _setup(tmp_path: Path, *, bad_date: bool = False) -> _Setup:
    db = tmp_path / "dates.sqlite3"
    leads = SqliteLeadRepository(db)
    raws = SqliteRawItemRepository(db)
    batches = SqliteSortingBatchRepository(db)
    facts = SqliteFactRepository(db)
    events = SqliteStageEventRepository(db)
    lead = Lead(
        id="lead-1",
        name=LEAD_NAME,
        contact=LEAD_CONTACT,
        source="inbound",
        stage=INITIAL_STAGE,
        owner=None,
        created_at=f"{TODAY}T09:00:00+00:00",
    )
    leads.add(lead)
    service = SortingService(
        leads,
        raws,
        batches,
        FakeSorting(bad_date=bad_date, today=TODAY),
        clock=_clock(),
        facts=facts,
        template_loader=_template_loader,
    )
    return _Setup(service, leads, facts, events, lead, db)


def test_fake_returns_iso_date_and_keeps_the_original_span(tmp_path: Path) -> None:
    """假模型把「下周三」算成今天 + 7 天：`value` 是 ISO 日期，出处仍是原文那句。"""
    setup = _setup(tmp_path)

    outcome = setup.service.sort(content=CONTENT, source="paste", lead_id=setup.lead.id)

    assert outcome.batch is not None
    assert outcome.batch.status == "pending_review"
    assert outcome.dropped == [], outcome.dropped

    appointment = next(
        element for element in outcome.suggestion.elements if element.field == "appointment_at"
    )
    assert appointment.value == NEXT_WEEK
    # 出处是原文里真实那一句（≥4 字才够当出处），所以「下周三」只是它的一部分。
    assert "下周三" in appointment.source_span
    assert appointment.source_span in CONTENT


def test_non_iso_date_is_dropped_and_other_elements_survive(tmp_path: Path) -> None:
    """模型把「下周三」原样当取值：该要素进 `dropped`（原因带 YYYY-MM-DD），别的不受影响。"""
    setup = _setup(tmp_path, bad_date=True)

    outcome = setup.service.sort(content=CONTENT, source="paste", lead_id=setup.lead.id)

    assert outcome.batch is not None
    kept = {element.field for element in outcome.suggestion.elements}
    assert "appointment_at" not in kept
    assert {"company", "need", "decision_maker"} <= kept, kept

    reasons = [entry["reason"] for entry in outcome.dropped]
    assert any("YYYY-MM-DD" in reason for reason in reasons), reasons
    assert any("下周三" in reason for reason in reasons), reasons
    # 丢弃下标指回模型原始输出（不是「剩下几条」）。
    assert all(entry["index"] is not None for entry in outcome.dropped)


def test_accepted_date_fills_lead_field_and_unblocks_appointment(tmp_path: Path) -> None:
    """同意后 `fields.appointment_at` 补上 ISO 日期，推进到 appointment 不再被卡住。"""
    setup = _setup(tmp_path)
    # 线索停在「需求确认」：决策人与需求都已填，唯一缺的就是约定时间。
    setup.leads.update_stage(setup.lead.id, "qualify")
    setup.leads.update_fields(setup.lead.id, {"decision_maker": "老板", "need": "预算三十万"})

    outcome = setup.service.sort(content=CONTENT, source="paste", lead_id=setup.lead.id)
    assert outcome.batch is not None
    confirmed = setup.service.confirm(outcome.batch.id, action="accept")
    assert confirmed.issues == [], confirmed.issues

    lead = setup.leads.get(setup.lead.id)
    assert lead is not None
    assert lead.fields["appointment_at"] == NEXT_WEEK
    # 事实层与线索字段同值：不是只在某一侧补上。
    fact = setup.facts.get_by_field(setup.lead.id, "appointment_at")
    assert fact is not None and fact.value == NEXT_WEEK

    flow = FlowService(setup.leads, setup.events, _template_loader, clock=_clock())
    advanced = flow.advance(setup.lead.id, "appointment")
    assert advanced.ok is True, advanced.issues
    assert advanced.lead is not None and advanced.lead.stage == "appointment"
