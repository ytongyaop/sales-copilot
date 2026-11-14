"""采集与提炼用例的行为测试：临时库 + 假提炼器，全程离线。

三条反证都在这份文件里：
1. 无原文片段的候选被丢弃（`test_bad_mode_drops_candidate_without_span`）；
2. 同一条原文重复提炼不改事实条数（`test_extract_twice_is_idempotent`）；
3. 人工锁定的事实不被模型覆盖（`test_locked_fact_survives_reextract`）。
"""

import itertools
from collections.abc import Callable
from pathlib import Path

import pytest

from salescopilot.adapters.db.sqlite import (
    SqliteExtractRunRepository,
    SqliteFactRepository,
    SqliteLeadRepository,
    SqliteRawItemRepository,
)
from salescopilot.adapters.llm.fake import FakeExtractor
from salescopilot.domain.models import INITIAL_STAGE, Lead
from salescopilot.services.ingest import IngestService

CONTENT = "客户说太贵了，还说下周再商量"


def _clock() -> Callable[[], str]:
    """每秒递增的假时钟：事实/账本的时间顺序稳定可断言。"""
    counter = itertools.count(1)
    return lambda: f"2026-11-14T10:00:{next(counter):02d}+00:00"


def _service(tmp_path: Path, *, bad_mode: bool = False) -> IngestService:
    db_path = tmp_path / "ingest.sqlite3"
    leads = SqliteLeadRepository(db_path)
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
    return IngestService(
        leads,
        SqliteRawItemRepository(db_path),
        SqliteFactRepository(db_path),
        SqliteExtractRunRepository(db_path),
        FakeExtractor(bad_mode=bad_mode),
        clock=_clock(),
    )


def _ingest(service: IngestService, content: str = CONTENT) -> str:
    item, duplicated, issues = service.ingest("lead-1", content=content, source="inbound")
    assert issues == [], issues
    assert item is not None
    assert duplicated is False
    return item.id


def test_extract_writes_facts_with_source_span(tmp_path: Path) -> None:
    service = _service(tmp_path)
    raw_id = _ingest(service)

    outcome = service.extract(raw_id)

    assert outcome.ok is True
    assert outcome.issues == []
    facts = service.list_facts("lead-1")[0]
    assert [fact.field for fact in facts] == ["objection", "need", "appointment_at"]
    for fact in facts:
        assert fact.source == "llm"
        assert fact.locked is False
        assert fact.source_span
        assert fact.source_span in CONTENT
        assert fact.raw_id == raw_id
    assert {fact.field: fact.value for fact in facts} == {
        "objection": "price",
        "need": "待确认决策",
        "appointment_at": "下周",
    }
    assert outcome.run is not None
    assert outcome.run.candidates == 3
    assert outcome.run.accepted == 3
    assert outcome.run.skipped == 0
    assert outcome.run.token == 0


def test_bad_mode_drops_candidate_without_span(tmp_path: Path) -> None:
    """反证 1：假模型故意多吐一条没有原文片段的候选，用例层必须丢掉并报出位置。"""
    service = _service(tmp_path, bad_mode=True)
    content = "这个价格有点贵，下周再细聊"
    raw_id = _ingest(service, content)

    outcome = service.extract(raw_id)

    assert outcome.ok is True
    accepted_fields = [fact.field for fact in outcome.accepted]
    assert accepted_fields == ["objection", "appointment_at"]
    assert "need" not in accepted_fields

    assert [issue.location for issue in outcome.skipped] == ["candidates[2].source_span"]
    assert outcome.skipped[0].message == "原文片段不能为空"
    assert outcome.run is not None
    assert outcome.run.candidates == 3
    assert outcome.run.accepted == 2
    assert outcome.run.skipped == 1
    assert len(service.list_facts("lead-1")[0]) == 2


def test_extract_twice_is_idempotent(tmp_path: Path) -> None:
    """反证 2：同一条原文提炼两次，事实条数不变、第二次一条也不采纳。"""
    service = _service(tmp_path)
    raw_id = _ingest(service)

    first = service.extract(raw_id)
    before = len(service.list_facts("lead-1")[0])
    second = service.extract(raw_id)

    assert first.accepted != []
    assert second.ok is True
    assert second.accepted == []
    assert len(service.list_facts("lead-1")[0]) == before
    assert sorted(issue.location for issue in second.skipped) == [
        "candidates[0].objection",
        "candidates[1].need",
        "candidates[2].appointment_at",
    ]
    assert {issue.message for issue in second.skipped} == {"重复值"}
    assert second.run is not None
    assert second.run.accepted == 0
    assert second.run.skipped == 3
    assert len(service.runs_for(raw_id)) == 2


def test_locked_fact_survives_reextract(tmp_path: Path) -> None:
    """反证 3：人工把异议改成 effect 并锁定，再提炼一次仍是 effect。"""
    service = _service(tmp_path)
    raw_id = _ingest(service)
    service.extract(raw_id)

    fact, issues = service.set_manual("lead-1", "objection", "effect")
    assert issues == []
    assert fact is not None
    assert fact.value == "effect"
    assert fact.source == "manual"
    assert fact.locked is True

    again = service.extract(raw_id)

    objection = next(fact for fact in service.list_facts("lead-1")[0] if fact.field == "objection")
    assert objection.field == "objection"
    assert objection.value == "effect"
    assert objection.locked is True
    assert objection.source == "manual"
    assert [issue.location for issue in again.skipped if "objection" in issue.location] == [
        "candidates[0].objection"
    ]
    assert [issue.message for issue in again.skipped if "objection" in issue.location] == [
        "人工锁定，跳过"
    ]


def test_same_content_returns_same_raw(tmp_path: Path) -> None:
    service = _service(tmp_path)

    first = service.ingest("lead-1", content=CONTENT, source="inbound")
    second = service.ingest("lead-1", content=f"  {CONTENT}  ", source="referral")

    assert first[0] is not None and second[0] is not None
    assert second[1] is True
    assert second[0].id == first[0].id
    assert second[0].content == CONTENT
    assert len(service.list_raws("lead-1")[0]) == 1


@pytest.mark.parametrize(
    ("content", "location"),
    [("", "content"), ("太短", "content")],
)
def test_ingest_rejects_short_or_empty_content(tmp_path: Path, content: str, location: str) -> None:
    service = _service(tmp_path)

    item, duplicated, issues = service.ingest("lead-1", content=content, source="inbound")

    assert item is None
    assert duplicated is False
    assert [issue.location for issue in issues] == [location]


def test_ingest_unknown_lead_and_unknown_raw(tmp_path: Path) -> None:
    service = _service(tmp_path)

    item, duplicated, issues = service.ingest("missing", content=CONTENT, source="inbound")
    assert item is None
    assert duplicated is False
    assert issues[0].location == "lead_id"

    outcome = service.extract("raw-missing")
    assert outcome.ok is False
    assert outcome.issues[0].location == "raw_id"
    assert outcome.run is None


def test_manual_edit_rejects_unknown_field_and_missing_fact(tmp_path: Path) -> None:
    service = _service(tmp_path)
    raw_id = _ingest(service)
    service.extract(raw_id)

    fact, issues = service.set_manual("lead-1", "hobby", "钓鱼")
    assert fact is None
    assert issues[0].location == "field"

    fact, issues = service.set_manual("lead-1", "amount", "2000 元")
    assert fact is None
    assert issues[0].location == "fact"
    assert "amount" in issues[0].message


def test_unlock_keeps_manual_source(tmp_path: Path) -> None:
    service = _service(tmp_path)
    raw_id = _ingest(service)
    service.extract(raw_id)
    service.set_manual("lead-1", "need", "想先看演示")

    fact, issues = service.unlock("lead-1", "need")

    assert issues == []
    assert fact is not None
    assert fact.locked is False
    assert fact.source == "manual"
    assert fact.value == "想先看演示"
