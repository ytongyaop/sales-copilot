"""分拣单人工处置（同意 / 改归属 / 拒绝）的服务测试：临时库 + 假模型，全程离线。

六条判据：
1. 同意：要素落成事实，`card_section` 取自要素，分拣单状态与处置留痕都写上；
2. 人工锁定的字段被跳过并给原因，值一个字都不改（人比模型大一票）；
3. 审核后被改坏的出处：落库前再校验一次，该要素跳过（防「审核后被篡改」）；
4. 拒绝：缺理由 422 且不落事实；有理由则只改状态，事实层零写入；
5. 改归属：事实落到新客户身上，分拣单改挂过去，动作记 `fixed_and_accepted`；
6. 窄接口 `decide`：调用后建议四列（`suggestion_json` / `confidence` / `scorer` /
   `dropped_json`）字符串与数值都不变——审计流水不许被处置改写。
"""

import itertools
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from salescopilot.adapters.db.sqlite import (
    SqliteFactRepository,
    SqliteLeadRepository,
    SqliteRawItemRepository,
    SqliteSortingBatchRepository,
)
from salescopilot.adapters.llm.fake import FakeSorting
from salescopilot.adapters.template.loader import load_all
from salescopilot.domain.models import INITIAL_STAGE, Fact, Lead
from salescopilot.services.sorting import SortingService

# 真实模板：落事实的字段白名单要按线索自己的模板（`card` 段）推导。
TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "salescopilot" / "templates"
TEMPLATES = load_all(TEMPLATES_DIR)


def _template_loader(key: str) -> dict:
    return TEMPLATES[key]


# 四个块各有一条要素：公司名（basic）/ 需求（needs）/ 决策人（decision）/ 时间（followup）。
CONTENT = "华远建设有限公司的老板说预算三十万，下周再聊"

LEAD_A_NAME = "华远建设有限公司"
LEAD_A_CONTACT = "13800001111"
LEAD_B_NAME = "远大科技有限公司"
LEAD_B_CONTACT = "13900002222"


def _clock() -> Callable[[], str]:
    """每秒递增的假时钟：分拣单与事实的时间顺序稳定可断言。"""
    counter = itertools.count(1)
    return lambda: f"2026-11-18T10:00:{next(counter):02d}+00:00"


@dataclass(frozen=True)
class _Setup:
    """一套离线装置：两个客户 + 四条仓储 + 假分拣模型。"""

    service: SortingService
    batches: SqliteSortingBatchRepository
    facts: SqliteFactRepository
    leads: SqliteLeadRepository
    lead_a: Lead
    lead_b: Lead
    db: Path


def _setup(tmp_path: Path, *, bad_span: bool = False) -> _Setup:
    db = tmp_path / "confirm.sqlite3"
    leads = SqliteLeadRepository(db)
    raws = SqliteRawItemRepository(db)
    batches = SqliteSortingBatchRepository(db)
    facts = SqliteFactRepository(db)
    lead_a = Lead(
        id="lead-a",
        name=LEAD_A_NAME,
        contact=LEAD_A_CONTACT,
        source="inbound",
        stage=INITIAL_STAGE,
        owner=None,
        created_at="2026-11-18T09:00:00+00:00",
    )
    lead_b = Lead(
        id="lead-b",
        name=LEAD_B_NAME,
        contact=LEAD_B_CONTACT,
        source="inbound",
        stage=INITIAL_STAGE,
        owner=None,
        created_at="2026-11-18T09:01:00+00:00",
    )
    leads.add(lead_a)
    leads.add(lead_b)
    service = SortingService(
        leads,
        raws,
        batches,
        FakeSorting(bad_span=bad_span),
        clock=_clock(),
        facts=facts,
        template_loader=_template_loader,
    )
    return _Setup(service, batches, facts, leads, lead_a, lead_b, db)


def _pending_review(setup: _Setup) -> str:
    """投料（指定客户）得到一张待人确认的单，返回 batch_id。"""
    outcome = setup.service.sort(content=CONTENT, source="paste", lead_id=setup.lead_a.id)
    assert outcome.batch is not None
    assert outcome.batch.status == "pending_review"
    return outcome.batch.id


def _tamper_span(db: Path, batch_id: str, *, field: str, span: str) -> None:
    """直接改库里的 `suggestion_json`：模拟「审核之后建议被改坏」。

    仓储本身不提供这个入口（只追加），所以测试绕过用例层用 SQL 篡改。
    """
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT suggestion_json FROM sorting_batches WHERE id = ?", (batch_id,)
        ).fetchone()
        payload = json.loads(row[0])
        for element in payload["elements"]:
            if element["field"] == field:
                element["source_span"] = span
        conn.execute(
            "UPDATE sorting_batches SET suggestion_json = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), batch_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_accept_writes_facts_with_card_sections(tmp_path: Path) -> None:
    """同意：四条要素各成一条事实，归块与出处都对，分拣单状态与留痕写上。"""
    setup = _setup(tmp_path)
    batch_id = _pending_review(setup)

    outcome = setup.service.confirm(batch_id, action="accept")

    assert outcome.issues == []
    assert outcome.skipped == []
    assert outcome.applied == 4

    stored = {fact.field: fact for fact in setup.facts.list_for(setup.lead_a.id)}
    assert set(stored) == {"company", "need", "decision_maker", "appointment_at"}
    assert {field: fact.card_section for field, fact in stored.items()} == {
        "company": "basic",
        "need": "needs",
        "decision_maker": "decision",
        "appointment_at": "followup",
    }
    for fact in stored.values():
        assert fact.source == "llm"
        assert fact.locked is False
        assert fact.source_span in CONTENT, fact
        assert fact.raw_id == setup.batches.get(batch_id).raw_id

    refreshed = setup.batches.get(batch_id)
    assert refreshed is not None
    assert refreshed.status == "accepted"
    assert refreshed.decided_action == "accepted"
    assert refreshed.decided_at


def test_accept_skips_locked_field_and_keeps_manual_value(tmp_path: Path) -> None:
    """字段已被人工锁定：跳过并给原因，人工值一个字都不改。"""
    setup = _setup(tmp_path)
    batch_id = _pending_review(setup)
    manual = Fact(
        id="fact-locked",
        lead_id=setup.lead_a.id,
        raw_id="",
        field="decision_maker",
        value="张总",
        source="manual",
        source_span="(人工录入)",
        confidence=1.0,
        locked=True,
        at="2026-11-18T09:30:00+00:00",
    )
    setup.facts.add(manual)

    outcome = setup.service.confirm(batch_id, action="accept")

    assert outcome.applied == 3
    assert {"field": "decision_maker", "reason": "人工锁定，跳过"} in outcome.skipped

    kept = setup.facts.get_by_field(setup.lead_a.id, "decision_maker")
    assert kept is not None
    assert kept.value == "张总"
    assert kept.locked is True
    assert kept.source == "manual"
    assert kept.card_section == ""


def test_accept_skips_element_whose_span_was_tampered(tmp_path: Path) -> None:
    """审核后出处被改坏：落库前再校验一次，该要素跳过并说明原因。"""
    setup = _setup(tmp_path)
    batch_id = _pending_review(setup)
    _tamper_span(setup.db, batch_id, field="need", span="客户说下个月总部一定会批")

    outcome = setup.service.confirm(batch_id, action="accept")

    assert outcome.applied == 3
    tampered = [item for item in outcome.skipped if item["field"] == "need"]
    assert tampered, outcome.skipped
    assert "找不到" in tampered[0]["reason"]

    stored = {fact.field: fact for fact in setup.facts.list_for(setup.lead_a.id)}
    assert "need" not in stored
    assert set(stored) == {"company", "decision_maker", "appointment_at"}


def test_reject_requires_reason_and_writes_no_facts(tmp_path: Path) -> None:
    """拒绝：缺理由 422 且原样不动；有理由只改状态，事实层零写入。"""
    setup = _setup(tmp_path)
    batch_id = _pending_review(setup)

    missing = setup.service.confirm(batch_id, action="reject")
    assert missing.issues[0].location == "reason"
    assert missing.applied == 0
    assert setup.facts.list_for(setup.lead_a.id) == []
    assert setup.batches.get(batch_id).status == "pending_review"

    too_short = setup.service.confirm(batch_id, action="reject", reason="丢")
    assert too_short.issues[0].location == "reason"

    done = setup.service.confirm(batch_id, action="reject", reason="客户跟丢了")
    assert done.issues == []
    assert done.applied == 0
    assert setup.facts.list_for(setup.lead_a.id) == []

    refreshed = setup.batches.get(batch_id)
    assert refreshed.status == "rejected"
    assert refreshed.decided_action == "rejected"

    again = setup.service.confirm(batch_id, action="reject", reason="再拒一次")
    assert again.issues[0].location == "status"


def test_fix_moves_facts_to_another_lead(tmp_path: Path) -> None:
    """改归属：事实落到新客户身上，分拣单改挂过去，动作记 fixed_and_accepted。"""
    setup = _setup(tmp_path)
    batch_id = _pending_review(setup)

    outcome = setup.service.confirm(batch_id, action="fix", lead_id=setup.lead_b.id)

    assert outcome.issues == []
    assert outcome.applied == 4
    assert setup.facts.list_for(setup.lead_a.id) == []
    stored = setup.facts.list_for(setup.lead_b.id)
    assert {fact.field for fact in stored} == {
        "company",
        "need",
        "decision_maker",
        "appointment_at",
    }
    assert all(fact.card_section for fact in stored)

    refreshed = setup.batches.get(batch_id)
    assert refreshed.lead_id == setup.lead_b.id
    assert refreshed.status == "accepted"
    assert refreshed.decided_action == "fixed_and_accepted"


def test_decide_touches_only_three_columns(tmp_path: Path) -> None:
    """窄接口反证：调用 decide 后，建议四列一个字符都没变。"""
    setup = _setup(tmp_path)
    batch_id = _pending_review(setup)
    before = setup.batches.get(batch_id)
    assert before is not None

    setup.batches.decide(batch_id, action="accepted", at="2026-11-18T12:00:00+00:00")

    after = setup.batches.get(batch_id)
    assert after is not None
    assert after.suggestion_json == before.suggestion_json
    assert after.confidence == before.confidence
    assert after.scorer == before.scorer
    assert after.dropped_json == before.dropped_json
    assert after.status == "accepted"
    assert after.decided_at == "2026-11-18T12:00:00+00:00"
    assert after.decided_action == "accepted"


def test_accept_syncs_lead_fields_without_overwriting_manual_values(tmp_path: Path) -> None:
    """同意后把可映射的要素补进线索字段：缺的补上，人填过的一个字都不覆盖。"""
    setup = _setup(tmp_path)
    setup.leads.update_fields(setup.lead_a.id, {"need": "人工填的需求"})
    batch_id = _pending_review(setup)

    outcome = setup.service.confirm(batch_id, action="accept")

    assert outcome.issues == []
    assert outcome.applied == 4
    refreshed = setup.leads.get(setup.lead_a.id)
    assert refreshed is not None
    # 要素里有决策人（原文「老板」）：流程推进不再因缺这一项卡住
    assert refreshed.fields["decision_maker"] == "老板"
    # 人填过的需求保持原值
    assert refreshed.fields["need"] == "人工填的需求"


def test_dropped_diagnostics_are_persisted_and_returned(tmp_path: Path) -> None:
    """投料时的丢弃诊断进 `dropped_json`，单条查询原样返回 `dropped`。"""
    setup = _setup(tmp_path, bad_span=True)

    outcome = setup.service.sort(content=CONTENT, source="paste", lead_id=setup.lead_a.id)

    assert outcome.batch is not None
    assert outcome.dropped, "编造的出处必须留下丢弃记录"
    stored = json.loads(outcome.batch.dropped_json)
    assert stored == outcome.dropped

    body, issues = setup.service.get_batch(outcome.batch.id)
    assert issues == []
    assert body is not None
    assert body["dropped"] == outcome.dropped
    assert any("找不到" in entry["reason"] for entry in body["dropped"])
