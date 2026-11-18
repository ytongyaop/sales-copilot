"""话术用例的四道门槛与确认/丢弃流程（假知识库 + 假生成器，全程离线）。

反证清单：
1. 没有事实 → 拒绝；
2. 没有检索到资料 → 拒绝（硬门槛）；
3. 模型没引用资料 → 拒绝；
4. 草稿含禁用词 → 拒绝；
5. 草稿写了**过去**的日期 → 重试一次（重试改对了就收）；
6. 两次都写过去的日期 → 拒绝，并给中文原因（不落库）。
"""

import itertools
from collections.abc import Callable
from pathlib import Path

from salescopilot.adapters.db.sqlite import (
    SqliteDraftRepository,
    SqliteFactRepository,
    SqliteFollowupRepository,
    SqliteLeadRepository,
)
from salescopilot.adapters.llm.draft import FakeDrafter
from salescopilot.domain.models import DRAFT_PENDING, INITIAL_STAGE, Fact, Lead
from salescopilot.services.draft import DraftService

HITS = [
    {"text": "退费在受理后 15 个工作日内原路退回。", "source": "service_policy.md", "score": 0.91},
    {"text": "试听需提前 1 天预约。", "source": "service_policy.md", "score": 0.72},
]


class StubKnowledge:
    """假检索：返回固定命中，并记录收到的查询语句（用于断言「用事实拼查询」）。"""

    def __init__(self, hits: list[dict]) -> None:
        self.hits = hits
        self.queries: list[str] = []

    def search(self, query: str, k: int = 4) -> list[dict]:
        self.queries.append(query)
        return self.hits[:k]


def _clock() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"2026-11-14T10:00:{next(counter):02d}+00:00"


def _service(
    tmp_path: Path, *, hits: list[dict] | None = None, drafter=None
) -> tuple[
    DraftService,
    SqliteFactRepository,
    SqliteDraftRepository,
    StubKnowledge,
    SqliteFollowupRepository,
]:
    db = tmp_path / "draft.sqlite3"
    leads = SqliteLeadRepository(db)
    leads.add(
        Lead(
            id="lead-1",
            name="线索-测试",
            contact="13800001111",
            source="inbound",
            stage=INITIAL_STAGE,
            owner=None,
            created_at="2026-11-14T09:00:00+00:00",
        )
    )
    facts = SqliteFactRepository(db)
    drafts = SqliteDraftRepository(db)
    followups = SqliteFollowupRepository(db)
    knowledge = StubKnowledge(HITS if hits is None else hits)
    service = DraftService(
        leads,
        facts,
        knowledge,
        FakeDrafter() if drafter is None else drafter,
        drafts,
        clock=_clock(),
        followups=followups,
    )
    return service, facts, drafts, knowledge, followups


def _add_fact(
    facts: SqliteFactRepository, field: str = "need", value: str = "想了解退费规则"
) -> None:
    facts.add(
        Fact(
            id=f"fact-{field}",
            lead_id="lead-1",
            raw_id="",
            field=field,
            value=value,
            source="manual",
            source_span="(人工录入)",
            confidence=1.0,
            locked=True,
            at="2026-11-14T09:30:00+00:00",
        )
    )


def test_generate_rejected_when_no_facts(tmp_path: Path) -> None:
    service, _, drafts, _, _ = _service(tmp_path)

    outcome, issues = service.generate("lead-1")

    assert outcome.ok is False
    assert [issue.message for issue in issues] == ["还没有客户事实，先提炼或补录"]
    assert drafts.list_for("lead-1") == []


def test_generate_rejected_when_no_knowledge_hits(tmp_path: Path) -> None:
    service, facts, drafts, _, _ = _service(tmp_path, hits=[])
    _add_fact(facts)

    outcome, issues = service.generate("lead-1")

    assert outcome.ok is False
    assert [issue.message for issue in issues] == ["没有检索到资料依据，无法生成话术"]
    assert drafts.list_for("lead-1") == []


def test_generate_rejected_when_model_cites_nothing(tmp_path: Path) -> None:
    service, facts, drafts, _, _ = _service(tmp_path, drafter=FakeDrafter(cite=False))
    _add_fact(facts)

    outcome, issues = service.generate("lead-1")

    assert outcome.ok is False
    assert [issue.location for issue in issues] == ["draft"]
    assert [issue.message for issue in issues] == [
        "模型没有引用任何资料，已拒绝生成（可补充资料，或在跟进里写明依据后再生成）"
    ]
    assert drafts.list_for("lead-1") == []


def test_generate_rejected_when_text_contains_banned_phrase(tmp_path: Path) -> None:
    service, facts, drafts, _, _ = _service(tmp_path, drafter=FakeDrafter(bad_mode=True))
    _add_fact(facts)

    outcome, issues = service.generate("lead-1")

    assert outcome.ok is False
    assert [issue.location for issue in issues] == ["draft.text"]
    assert "保过" in issues[0].message
    assert drafts.list_for("lead-1") == []


def test_generate_saves_pending_draft_with_citations(tmp_path: Path) -> None:
    service, facts, drafts, _, _ = _service(tmp_path)
    _add_fact(facts)

    outcome, issues = service.generate("lead-1")

    assert issues == []
    assert outcome.ok is True
    assert outcome.draft_id
    assert outcome.text
    assert outcome.model == "fake"
    assert len(outcome.citations) == 1
    assert outcome.citations[0]["source"] == "service_policy.md"
    assert "15 个工作日" in outcome.citations[0]["text"]

    saved = drafts.get(outcome.draft_id)
    assert saved is not None
    assert saved.status == DRAFT_PENDING
    assert saved.text == outcome.text
    assert saved.citations == outcome.citations


def test_generate_uses_fact_values_as_query(tmp_path: Path) -> None:
    service, facts, _, knowledge, _ = _service(tmp_path)
    _add_fact(facts, field="need", value="想了解退费规则")

    service.generate("lead-1")

    assert knowledge.queries == ["need=想了解退费规则"]


def test_generate_retries_once_when_the_draft_writes_a_past_date(tmp_path: Path) -> None:
    """第一次编了个过去的日期（11月11日）→ 再问一次；第二次按约定写（11月20日）就收。"""
    drafter = FakeDrafter(bad_dates=1)
    service, facts, drafts, _, _ = _service(tmp_path, drafter=drafter)
    _add_fact(facts, field="appointment_at", value="2026-11-20")

    outcome, issues = service.generate("lead-1")

    assert issues == []
    assert outcome.ok is True
    assert drafter.calls == 2, "日期不合格要重试一次（只一次）"
    assert "11月11日" not in outcome.text, "编出来的过去日子不许进草稿"
    assert "11月20日" in outcome.text, "重试那次要按客户已约定时间写"
    assert drafts.get(outcome.draft_id) is not None


def test_generate_rejects_when_both_attempts_write_a_past_date(tmp_path: Path) -> None:
    """两次都编过去的日期 → 拒绝落库，并给中文原因（说清是哪天早于哪天）。"""
    drafter = FakeDrafter(bad_dates=2)
    service, facts, drafts, _, _ = _service(tmp_path, drafter=drafter)
    _add_fact(facts)

    outcome, issues = service.generate("lead-1")

    assert outcome.ok is False
    assert outcome.draft_id is None
    assert drafter.calls == 2, "只重试一次，不无限重问模型"
    assert [issue.location for issue in issues] == ["draft.text"]
    assert "早于今天" in issues[0].message
    assert "11月11日" in issues[0].message and "2026-11-14" in issues[0].message
    assert drafts.list_for("lead-1") == [], "被拒的草稿一条都不许落库"


def test_confirm_without_next_date_is_rejected(tmp_path: Path) -> None:
    service, facts, _, _, _ = _service(tmp_path)
    _add_fact(facts)
    outcome, _ = service.generate("lead-1")

    result = service.confirm("lead-1", outcome.draft_id, objection="price", next_followup_at="")

    assert result.item is None
    assert any(issue.location == "next_followup_at" for issue in result.issues)


def test_confirm_writes_followup_with_draft_text_as_conclusion(tmp_path: Path) -> None:
    service, facts, drafts, _, followups = _service(tmp_path)
    _add_fact(facts)
    outcome, _ = service.generate("lead-1")

    result = service.confirm(
        "lead-1", outcome.draft_id, objection="price", next_followup_at="2026-11-20"
    )

    assert result.issues == []
    assert result.item is not None
    assert result.item.conclusion == outcome.text
    assert result.item.objection == "price"
    assert result.item.next_followup_at == "2026-11-20"
    assert [record.id for record in followups.list_for("lead-1")] == [result.item.id]

    saved = drafts.get(outcome.draft_id)
    assert saved is not None and saved.status == "confirmed"


def test_confirm_after_discard_is_rejected(tmp_path: Path) -> None:
    service, facts, drafts, _, _ = _service(tmp_path)
    _add_fact(facts)
    outcome, _ = service.generate("lead-1")

    discarded, discard_issues = service.discard("lead-1", outcome.draft_id)
    assert discard_issues == []
    assert discarded is not None
    assert drafts.get(outcome.draft_id).status == "discarded"

    result = service.confirm(
        "lead-1", outcome.draft_id, objection="price", next_followup_at="2026-11-20"
    )

    assert result.item is None
    assert [issue.message for issue in result.issues] == ["草稿已确认或已丢弃，不能再次操作"]


def test_confirm_unknown_draft_is_rejected(tmp_path: Path) -> None:
    service, _, _, _, _ = _service(tmp_path)

    result = service.confirm("lead-1", "missing", objection="price", next_followup_at="2026-11-20")

    assert result.item is None
    assert [issue.message for issue in result.issues] == ["草稿不存在"]
