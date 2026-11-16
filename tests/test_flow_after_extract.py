"""关键回归：提炼补齐字段后，线索能推进过「需求确认」（假模型 + 临时库，离线）。

没有这条测试，B9.5 之前的缺口就会悄悄回来：必填字段没有任何入口能写，
所有线索都卡在 qualify 前面。
"""

from pathlib import Path

from salescopilot.adapters.db.sqlite import (
    SqliteExtractRunRepository,
    SqliteFactRepository,
    SqliteLeadRepository,
    SqliteRawItemRepository,
    SqliteStageEventRepository,
)
from salescopilot.adapters.llm.fake import FakeExtractor
from salescopilot.domain.models import INITIAL_STAGE, Lead
from salescopilot.services.flow import FlowService
from salescopilot.services.ingest import IngestService

TEMPLATE = {
    "template": "test_flow",
    "name": "测试流程",
    "thresholds": {},
    "flow": [{"block": "intake"}, {"block": "first_contact"}, {"block": "qualify"}],
}

CONTENT = "李校长想提高数学成绩，预算 1 万，下周一见面，他还在考虑"


def test_extract_unblocks_qualify(tmp_path: Path) -> None:
    db = tmp_path / "flow.sqlite3"
    leads = SqliteLeadRepository(db)
    leads.add(
        Lead(
            id="lead-1",
            name="李校长",
            contact="13800001111",
            source="referral",
            stage=INITIAL_STAGE,
            owner=None,
            created_at="2026-11-16T09:00:00+00:00",
            template="test_flow",
        )
    )
    ingest = IngestService(
        leads,
        SqliteRawItemRepository(db),
        SqliteFactRepository(db),
        SqliteExtractRunRepository(db),
        FakeExtractor(),
    )
    flow = FlowService(leads, SqliteStageEventRepository(db), lambda key: TEMPLATE)

    assert flow.advance("lead-1", "first_contact").ok is True

    # 提炼之前：必填（决策人 / 核心需求）没处可填 → 推进被拦住
    blocked = flow.advance("lead-1", "qualify")
    assert blocked.ok is False
    assert [issue.location for issue in blocked.issues] == [
        "fields.decision_maker",
        "fields.need",
    ]

    raw, _, issues = ingest.ingest("lead-1", content=CONTENT, source="inbound")
    assert raw is not None and issues == []
    outcome = ingest.extract(raw.id)
    assert outcome.ok is True
    assert "decision_maker" in outcome.field_updates
    assert "need" in outcome.field_updates

    # 提炼之后：同一跳现在过得去（这就是 B9.5 要打通的那一段）
    advanced = flow.advance("lead-1", "qualify")
    assert advanced.ok is True
    assert advanced.lead is not None
    assert advanced.lead.stage == "qualify"
