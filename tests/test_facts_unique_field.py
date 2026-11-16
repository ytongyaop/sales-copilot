"""事实唯一化（B7 必修 bug）：同一客户同一字段只留一条「当前事实」。

三条断言：
1. 两次提炼把同字段从 price 改成 effect：仍只有一条，值 / 片段 / 原文编号换成新的，id 不变（原地更新）；
2. 人工锁定的事实不被覆盖；
3. 同一次提炼里同字段多条候选：保留最后一条，前一条记为 skipped（带位置与原因）。
"""

import itertools
from collections.abc import Callable
from pathlib import Path

from salescopilot.adapters.db.sqlite import (
    SqliteExtractRunRepository,
    SqliteFactRepository,
    SqliteLeadRepository,
    SqliteRawItemRepository,
)
from salescopilot.domain.models import INITIAL_STAGE, ExtractionResult, Lead
from salescopilot.services.ingest import IngestService

PRICE_CANDIDATE = {
    "field": "objection",
    "value": "price",
    "source_span": "太贵",
    "confidence": 0.8,
}
EFFECT_CANDIDATE = {
    "field": "objection",
    "value": "effect",
    "source_span": "没效果",
    "confidence": 0.7,
}


class ScriptedExtractor:
    """按调用次数吐固定候选：第一次给脚本[0]，之后都给脚本[-1]。"""

    def __init__(self, scripts: list[list[dict]]) -> None:
        self._scripts = scripts
        self._calls = 0

    def extract(self, *, raw, facts) -> ExtractionResult:
        index = min(self._calls, len(self._scripts) - 1)
        self._calls += 1
        return ExtractionResult(
            candidates=self._scripts[index],
            model="stub",
            prompt_version="v0",
            token=0,
            seconds=0.0,
            raw_output_hash=f"hash-{self._calls}",
        )


def _clock() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"2026-11-16T10:00:{next(counter):02d}+00:00"


def _service(tmp_path: Path, scripts: list[list[dict]]) -> IngestService:
    db_path = tmp_path / "facts.sqlite3"
    leads = SqliteLeadRepository(db_path)
    leads.add(
        Lead(
            id="lead-1",
            name="线索-测试",
            contact="13800001111",
            source="inbound",
            stage=INITIAL_STAGE,
            owner=None,
            created_at="2026-11-16T09:00:00+00:00",
        )
    )
    return IngestService(
        leads,
        SqliteRawItemRepository(db_path),
        SqliteFactRepository(db_path),
        SqliteExtractRunRepository(db_path),
        ScriptedExtractor(scripts),
        clock=_clock(),
    )


def _ingest(service: IngestService, content: str) -> str:
    item, duplicated, issues = service.ingest("lead-1", content=content, source="inbound")
    assert issues == [], issues
    assert item is not None and duplicated is False
    return item.id


def _only_objection(service: IngestService):
    facts = [fact for fact in service.list_facts("lead-1")[0] if fact.field == "objection"]
    assert len(facts) == 1, facts
    return facts[0]


def test_second_extract_overwrites_the_same_field_in_place(tmp_path: Path) -> None:
    """第二次把 objection 从 price 改成 effect：只有一条，值/片段/原文编号都是新的，id 不变。"""
    service = _service(tmp_path, [[PRICE_CANDIDATE], [EFFECT_CANDIDATE]])

    first_raw = _ingest(service, "客户说太贵了，先放着看看")
    first = service.extract(first_raw)
    assert [fact.value for fact in first.accepted] == ["price"]
    original_id = _only_objection(service).id

    second_raw = _ingest(service, "客户又说没效果，不太敢买")
    second = service.extract(second_raw)

    objection = _only_objection(service)
    assert objection.id == original_id
    assert objection.value == "effect"
    assert objection.source_span == "没效果"
    assert objection.raw_id == second_raw
    assert objection.at == second.accepted[0].at
    assert objection.confidence == 0.7
    assert objection.source == "llm"

    # 历史不丢：两条原文、两次提炼账本都还在。
    assert len(service.list_raws("lead-1")[0]) == 2
    assert len(service.runs_for(second_raw)) == 1
    assert len(service.runs_for(first_raw)) == 1


def test_locked_fact_is_not_overwritten(tmp_path: Path) -> None:
    """人工锁定后，模型再提炼同一字段也不改值（保持原样并记入 skipped）。"""
    service = _service(tmp_path, [[PRICE_CANDIDATE], [EFFECT_CANDIDATE]])

    raw_id = _ingest(service, "客户说太贵了，先放着看看")
    service.extract(raw_id)
    manual, issues = service.set_manual("lead-1", "objection", "effect")
    assert issues == [] and manual is not None

    again = service.extract(raw_id)

    objection = _only_objection(service)
    assert objection.id == manual.id
    assert objection.value == "effect"
    assert objection.source == "manual"
    assert objection.locked is True
    assert [(issue.location, issue.message) for issue in again.skipped] == [
        ("candidates[0].objection", "人工锁定，跳过")
    ]


def test_same_run_keeps_last_candidate_and_skips_the_rest(tmp_path: Path) -> None:
    """同一次提炼里同字段两条候选：只落最后一条，前一条记 skipped（位置指回原始输出）。"""
    service = _service(tmp_path, [[PRICE_CANDIDATE, EFFECT_CANDIDATE]])
    raw_id = _ingest(service, "客户先说太贵，后说没效果")

    outcome = service.extract(raw_id)

    assert [fact.value for fact in outcome.accepted] == ["effect"]
    assert [(issue.location, issue.message) for issue in outcome.skipped] == [
        ("candidates[0].objection", "同字段重复，保留最后一条")
    ]
    objection = _only_objection(service)
    assert objection.value == "effect"
    assert objection.source_span == "没效果"
    assert outcome.run is not None
    assert outcome.run.accepted == 1
    assert outcome.run.skipped == 1
