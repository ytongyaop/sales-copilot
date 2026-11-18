"""投料 → 分拣单的服务测试（临时库 + 假模型，全程离线）。

五条判据：
1. 指定了线索就直接归属，状态待人确认；
2. 不指定线索时走漏斗：联系方式精确键命中 → 自动归属，**归属没被问过模型**（计数断言）；
3. 字符串只到复核区 → 进待认领池，`lead_id` 为空；
4. 编造的出处被丢弃（`dropped` 有记录、该要素不落建议），模型原文仍原样留痕；
5. 同一段原文重复投料不产生第二条原文（沿用采集的幂等口径）。
"""

import itertools
from collections.abc import Callable
from datetime import date
from pathlib import Path

from salescopilot.adapters.db.sqlite import (
    SqliteLeadRepository,
    SqliteRawItemRepository,
    SqliteSortingBatchRepository,
)
from salescopilot.adapters.llm.fake import FakeSorting
from salescopilot.adapters.template.loader import load_all
from salescopilot.domain.grading import grade_from_facts
from salescopilot.domain.models import INITIAL_STAGE, Fact, Lead, SortingBatch
from salescopilot.services.sorting import SortingService

# 真实模板：分拣单的分级要按线索自己的模板 `grading` 段算（权重与阈值不从代码读）。
TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "salescopilot" / "templates"
TEMPLATES = load_all(TEMPLATES_DIR)

# 假时钟的日期部分固定是这一天：紧迫度按「距今天几天」分档，必须钉住。
TODAY = date(2026, 11, 18)


def _template_loader(key: str) -> dict:
    return TEMPLATES[key]


def _facts_from(elements, lead_id: str) -> list[Fact]:
    """把已校验的要素映射成事实（测试侧独立实现），用来对账分拣单的分级。"""
    return [
        Fact(
            id=f"fact-{index}",
            lead_id=lead_id,
            raw_id="",
            field=element.field,
            value=element.value,
            source="rule",
            source_span=element.source_span,
            confidence=element.confidence,
            locked=False,
            at="",
        )
        for index, element in enumerate(elements)
    ]


# 带手机号：L0 精确键应命中下面那条线索。
PHONE_CONTENT = "13800001111 那边说预算三十万，下周一下午再聊"
# 只差一个字级差别：归一后 93 分，落进复核区而不是自动档。
FUZZY_CONTENT = "华远建设工程有限公司那边说预算三十万，下周一下午再聊"
# 没有联系方式、也没有组织形式全称：判为新客户候选。
NEW_CONTENT = "完全不相干的一通闲聊，预算还在谈，下周再说"

LEAD_NAME = "华远建设有限公司"
LEAD_CONTACT = "13800001111"


def _clock() -> Callable[[], str]:
    """每秒递增的假时钟：分拣单的时间顺序稳定可断言。"""
    counter = itertools.count(1)
    return lambda: f"2026-11-18T10:00:{next(counter):02d}+00:00"


def _service(
    tmp_path: Path, *, bad_span: bool = False
) -> tuple[
    SortingService,
    FakeSorting,
    SqliteRawItemRepository,
    SqliteSortingBatchRepository,
    Lead,
]:
    db = tmp_path / "sorting.sqlite3"
    leads = SqliteLeadRepository(db)
    raws = SqliteRawItemRepository(db)
    batches = SqliteSortingBatchRepository(db)
    lead = Lead(
        id="lead-1",
        name=LEAD_NAME,
        contact=LEAD_CONTACT,
        source="inbound",
        stage=INITIAL_STAGE,
        owner=None,
        created_at="2026-11-18T09:00:00+00:00",
    )
    leads.add(lead)
    judge = FakeSorting(bad_span=bad_span)
    return (
        SortingService(
            leads,
            raws,
            batches,
            judge,
            clock=_clock(),
            template_loader=_template_loader,
        ),
        judge,
        raws,
        batches,
        lead,
    )


def test_designated_lead_is_attributed_and_waits_for_review(tmp_path: Path) -> None:
    """传了 `lead_id`：归属确定，状态待人确认，建议三块齐全。"""
    service, _judge, _raws, _batches, lead = _service(tmp_path)

    outcome = service.sort(content=PHONE_CONTENT, source="paste", lead_id=lead.id)

    assert outcome.issues == []
    assert outcome.batch is not None
    assert outcome.batch.lead_id == lead.id
    assert outcome.batch.status == "pending_review"
    assert outcome.batch.scorer == "exact"
    assert outcome.batch.confidence == 100.0
    assert outcome.suggestion is not None
    assert outcome.suggestion.elements, "至少要有要素，否则分拣单没有意义"
    # 分级只由规则产出：与详情页同一个函数、同一份模板权重（ADR-0008）。
    expected = grade_from_facts(
        _template_loader("edu_training"),
        _facts_from(outcome.suggestion.elements, lead.id),
        today=TODAY,
    )
    assert outcome.suggestion.grading["value"] == expected.value
    assert outcome.suggestion.grading["reason"] == expected.reason
    assert outcome.suggestion.grading["basis"] == expected.basis
    assert outcome.suggestion.next_action["action"]


def test_exact_contact_key_auto_without_asking_model_for_attribution(tmp_path: Path) -> None:
    """联系方式精确键命中即自动归属；归属候选一条都没交给模型（计数断言）。"""
    service, judge, _raws, _batches, lead = _service(tmp_path)

    outcome = service.sort(content=PHONE_CONTENT, source="paste")

    assert outcome.batch is not None
    assert outcome.batch.lead_id == lead.id
    assert outcome.batch.status == "pending_review"
    assert outcome.batch.scorer == "exact"
    assert judge.review_calls == 0, "精确键命中时不该再让模型复核归属"
    assert judge.last_candidates == []
    assert judge.calls == 1, "分拣本身仍要调一次模型（要素 / 分级 / 出库）"


def test_fuzzy_candidate_goes_to_pending_claim(tmp_path: Path) -> None:
    """只到字符串复核区（无第二字段佐证）：进待认领池，不替人定归属。"""
    service, judge, _raws, _batches, _lead = _service(tmp_path)

    outcome = service.sort(content=FUZZY_CONTENT, source="paste")

    assert outcome.batch is not None
    assert outcome.batch.lead_id is None
    assert outcome.batch.status == "pending_claim"
    assert outcome.batch.scorer == "fuzzy"
    assert 85.0 <= outcome.batch.confidence < 95.0, outcome.batch.confidence
    assert judge.review_calls == 1, "灰区要把候选交给模型参考"


def test_no_candidate_is_pending_claim_too(tmp_path: Path) -> None:
    """没有任何候选：同样进待认领池（视为新客户候选），不是直接建客户。"""
    service, _judge, _raws, _batches, _lead = _service(tmp_path)

    outcome = service.sort(content=NEW_CONTENT, source="paste")

    assert outcome.batch is not None
    assert outcome.batch.lead_id is None
    assert outcome.batch.status == "pending_claim"
    assert outcome.batch.scorer == "none"


def test_fabricated_span_is_dropped_from_the_suggestion(tmp_path: Path) -> None:
    """编造的出处：丢弃该要素、`dropped` 有记录、建议里没有它；模型原文照样留痕。"""
    service, _judge, _raws, _batches, lead = _service(tmp_path, bad_span=True)

    outcome = service.sort(content=PHONE_CONTENT, source="paste", lead_id=lead.id)

    assert outcome.batch is not None
    assert outcome.dropped, "编造的出处必须留下丢弃记录"
    assert all(entry["index"] is not None for entry in outcome.dropped)
    assert any("找不到" in entry["reason"] for entry in outcome.dropped)

    elements = outcome.suggestion.elements
    assert elements, "合规要素不该被连坐丢掉"
    assert all(element.source_span in PHONE_CONTENT for element in elements)
    assert all(element.value != "需要总部集中审批" for element in elements)
    assert "需要总部集中审批" not in outcome.suggestion.grading["basis"]

    # 审计要的是「模型当时吐了什么」：编造的那条仍在原始 JSON 里。
    assert "客户说下个月总部一定会批" in outcome.batch.suggestion_json


def test_list_batches_reports_dropped_count(tmp_path: Path) -> None:
    """列表带 `dropped_count`：数丢弃诊断的条数；`dropped_json` 坏掉按 0、不抛异常。"""
    service, _judge, _raws, batches, lead = _service(tmp_path, bad_span=True)

    outcome = service.sort(content=PHONE_CONTENT, source="paste", lead_id=lead.id)
    assert outcome.batch is not None
    assert len(outcome.dropped) == 1

    batches.add(
        SortingBatch(
            id="batch-broken",
            raw_id=outcome.batch.raw_id,
            status="pending_review",
            suggestion_json="{}",
            confidence=0.0,
            scorer="none",
            created_at="2026-11-18T11:00:00+00:00",
            dropped_json="{不是数组",
        )
    )

    items, issues = service.list_batches()

    assert issues == []
    counted = {item["id"]: item["dropped_count"] for item in items}
    assert counted == {outcome.batch.id: 1, "batch-broken": 0}


def test_repeated_content_reuses_the_same_raw(tmp_path: Path) -> None:
    """同一段原文投两次：原文只有一条（幂等），分拣单各留一张（审计流水）。"""
    service, _judge, raws, batches, lead = _service(tmp_path)

    first = service.sort(content=PHONE_CONTENT, source="paste", lead_id=lead.id)
    second = service.sort(content=PHONE_CONTENT, source="paste", lead_id=lead.id)

    assert first.batch is not None and second.batch is not None
    assert first.batch.raw_id == second.batch.raw_id
    assert len(raws.list_for(lead.id)) == 1
    assert len(batches.list_by_status("pending_review")) == 2
