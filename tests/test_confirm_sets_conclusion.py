"""确认草稿 → 「结论」写进线索字段 → 能推进到成交（假生成器 + 临时库，离线）。"""

from pathlib import Path

from salescopilot.adapters.db.sqlite import (
    SqliteDraftRepository,
    SqliteFactRepository,
    SqliteFollowupRepository,
    SqliteLeadRepository,
    SqliteStageEventRepository,
)
from salescopilot.adapters.llm.draft import FakeDrafter
from salescopilot.domain.models import INITIAL_STAGE, Fact, Lead
from salescopilot.services.draft import DraftService
from salescopilot.services.flow import FlowService

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

HITS = [
    {"text": "退费在受理后 15 个工作日内原路退回。", "source": "service_policy.md", "score": 0.91},
]


class StubKnowledge:
    """假检索：固定返回一条命中（草稿要引用它才允许落库）。"""

    def search(self, query: str, k: int = 4) -> list[dict]:
        return HITS[:k]


def _build(tmp_path: Path):
    db = tmp_path / "confirm.sqlite3"
    leads = SqliteLeadRepository(db)
    facts = SqliteFactRepository(db)
    drafts = SqliteDraftRepository(db)
    followups = SqliteFollowupRepository(db)
    events = SqliteStageEventRepository(db)

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
            fields={"decision_maker": "李校长", "need": "想提高数学成绩"},
        )
    )
    facts.add(
        Fact(
            id="fact-need",
            lead_id="lead-1",
            raw_id="",
            field="need",
            value="想提高数学成绩",
            source="manual",
            source_span="(人工录入)",
            confidence=1.0,
            locked=True,
            at="2026-11-16T09:30:00+00:00",
        )
    )

    draft_service = DraftService(
        leads,
        facts,
        StubKnowledge(),
        FakeDrafter(),
        drafts,
        followups=followups,
    )
    flow = FlowService(leads, events, lambda key: TEMPLATE)
    return draft_service, flow, leads


def test_confirm_writes_conclusion_and_reaches_close(tmp_path: Path) -> None:
    draft_service, flow, leads = _build(tmp_path)

    outcome, issues = draft_service.generate("lead-1")
    assert issues == []
    assert outcome.draft_id is not None

    confirmed = draft_service.confirm(
        "lead-1", outcome.draft_id, objection="price", next_followup_at="2026-11-20"
    )

    assert confirmed.issues == []
    assert confirmed.item is not None
    assert confirmed.item.conclusion == outcome.text
    # 返回值里直接能看到「结论」已经落进线索字段
    assert confirmed.fields["conclusion"] == outcome.text
    assert leads.get("lead-1").fields["conclusion"] == outcome.text

    # 一路推到成交：close 的必填就是「结论」，确认时已经写好
    for target in ("first_contact", "qualify", "close"):
        result = flow.advance("lead-1", target)
        assert result.ok is True, [issue.message for issue in result.issues]

    assert leads.get("lead-1").stage == "close"


def test_confirm_failure_returns_empty_fields(tmp_path: Path) -> None:
    draft_service, _, leads = _build(tmp_path)
    outcome, _ = draft_service.generate("lead-1")

    failed = draft_service.confirm(
        "lead-1", outcome.draft_id, objection="price", next_followup_at="2026/11/20"
    )

    assert failed.item is None
    assert failed.fields == {}
    assert [issue.location for issue in failed.issues] == ["next_followup_at"]
    assert "conclusion" not in leads.get("lead-1").fields
