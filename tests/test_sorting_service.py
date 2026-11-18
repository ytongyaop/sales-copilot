"""投料 → 分拣单的服务测试（临时库 + 假模型，全程离线）。

判据：
1. 指定了线索就直接归属，状态待人确认；
2. 不指定线索时走漏斗：联系方式精确键命中 → 自动归属，**归属没被问过模型**（计数断言）；
3. 规范化全称一致（原文「华远建设集团」＝客户「华远建设集团」）同样算 L0 精确键：
   待人确认、`scorer=exact`、**不调模型复核**（ADR-0007 的 L0 含「规范化全称」）；
4. 只差组织形式后缀（「华远科技集团」vs「华远科技有限公司」）**不算**精确键，只能复核；
5. 原文只有简称（「华远」）、原文里的名字没有后缀时也判不出精确键，照旧走模糊；
6. 字符串只到复核区 → 进待认领池，`lead_id` 为空；
7. 编造的出处被丢弃（`dropped` 有记录、该要素不落建议），模型原文仍原样留痕；
8. 出库建议的 `value_offered` 不许是日期（模型给的日期值退回模板文案）；
9. 同一段原文重复投料不产生第二条原文（沿用采集的幂等口径）。
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
# 原文里的机构名与客户名**完全一致**：规范化全称应当命中 L0，不调模型复核。
EXACT_NAME_CONTENT = "华远建设集团那边说，预算 60万到80万，下周三下午到现场聊"
# 只差组织形式后缀（集团 vs 有限公司）：核心串相同，法律上是两个主体，只能进复核。
SUFFIX_ONLY_CONTENT = "华远科技集团那边说，预算 60万到80万，下周三下午到现场聊"
# 原文只有简称「华远」，没有组织形式后缀：抽不出全称，判不出精确键。
SHORT_NAME_CONTENT = "华远那边说预算三十万，下周三下午再聊"
# 四块要素齐全：出库建议会从「时间字段」取值，正好用来反证 `value_offered` 不许是日期。
FULL_CONTENT = "华远建设集团那边说预算 60万到80万，负责人王经理，下周三下午到现场聊"
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


def _service_with_names(
    tmp_path: Path, *names: str
) -> tuple[SortingService, FakeSorting, list[Lead]]:
    """按给定名字建线索（顺序即 id 顺序），其余与 `_service` 一致。"""
    db = tmp_path / "sorting_names.sqlite3"
    leads = SqliteLeadRepository(db)
    raws = SqliteRawItemRepository(db)
    batches = SqliteSortingBatchRepository(db)
    created: list[Lead] = []
    for index, name in enumerate(names, start=1):
        lead = Lead(
            id=f"lead-{index}",
            name=name,
            contact="",
            source="inbound",
            stage=INITIAL_STAGE,
            owner=None,
            created_at=f"2026-11-18T09:0{index}:00+00:00",
        )
        leads.add(lead)
        created.append(lead)
    judge = FakeSorting()
    service = SortingService(
        leads, raws, batches, judge, clock=_clock(), template_loader=_template_loader
    )
    return service, judge, created


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


def test_same_normalized_full_name_is_exact_without_review(tmp_path: Path) -> None:
    """原文「华远建设集团」与客户同名：L0 规范化全称命中 → 待人确认、`scorer=exact`、不问模型。

    反证的是真模型产出：句子里的全称与库里名字一字不差，候选分数 100，却仍进待认领池
    （`scorer=fuzzy`）——因为旧实现只把手机 / 邮箱 / 信用代码当精确键。
    """
    service, judge, leads = _service_with_names(tmp_path, "华远建设集团")

    outcome = service.sort(content=EXACT_NAME_CONTENT, source="paste")

    assert outcome.batch is not None
    assert outcome.batch.status == "pending_review"
    assert outcome.batch.scorer == "exact"
    assert outcome.batch.confidence == 100.0
    assert outcome.batch.lead_id == leads[0].id
    assert judge.review_calls == 0, "规范化全称命中也属于精确键，不该再让模型复核归属"
    assert judge.last_candidates == []

    # 判定理由要能回答「为什么算到这家」：写清是规范化全称一致，不是打分打出来的。
    decision, candidates, resolved = service._attribute(EXACT_NAME_CONTENT)
    assert decision.scorer == "exact"
    assert "规范化全称一致" in decision.reason
    assert resolved == leads[0].id
    assert [item.scorer for item in candidates] == ["exact"]


def test_suffix_only_difference_is_not_exact(tmp_path: Path) -> None:
    """「华远科技集团」vs「华远科技有限公司」：核心串一样但主体不同 → 不进 auto。

    ADR-0007 的这条安全规则不因 L0 补上「规范化全称」而放宽。
    """
    service, _judge, _leads = _service_with_names(tmp_path, "华远科技有限公司")

    outcome = service.sort(content=SUFFIX_ONLY_CONTENT, source="paste")

    assert outcome.batch is not None
    assert outcome.batch.scorer != "exact", "仅组织形式不同不许算精确键"
    assert outcome.batch.lead_id is None, "两个法人主体，不能自动归属"
    assert outcome.batch.status == "pending_claim"
    assert outcome.batch.scorer == "fuzzy"


def test_short_name_only_is_not_exact(tmp_path: Path) -> None:
    """原文只有简称「华远」：抽不出带后缀的全称，判不出精确键，照旧走模糊（或判新客户）。"""
    service, _judge, _leads = _service_with_names(tmp_path, "华远建设集团")

    outcome = service.sort(content=SHORT_NAME_CONTENT, source="paste")

    assert outcome.batch is not None
    assert outcome.batch.scorer != "exact"
    assert outcome.batch.lead_id is None
    assert outcome.batch.status == "pending_claim"


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


def test_value_offered_is_never_a_date(tmp_path: Path) -> None:
    """出库建议的「提供什么」不许是日期：落到模板 `todo.values` 的文案，日期留在 `due_hint`。

    反证的是真模型产出：`next_action.value_offered = "2026-11-25"`——时间占了「给什么」这一格。
    """
    service, _judge, _raws, _batches, lead = _service(tmp_path)

    outcome = service.sort(content=FULL_CONTENT, source="paste", lead_id=lead.id)

    assert outcome.suggestion is not None
    next_action = outcome.suggestion.next_action
    template_value = TEMPLATES[lead.template]["todo"]["values"]["followup"]
    assert next_action["due_hint"] == "2026-11-25", "约定时间仍要落在 due_hint 上"
    assert next_action["value_offered"] == template_value
    assert next_action["value_offered"] != next_action["due_hint"]


def test_repeated_content_reuses_the_same_raw(tmp_path: Path) -> None:
    """同一段原文投两次：原文只有一条（幂等），分拣单各留一张（审计流水）。"""
    service, _judge, raws, batches, lead = _service(tmp_path)

    first = service.sort(content=PHONE_CONTENT, source="paste", lead_id=lead.id)
    second = service.sort(content=PHONE_CONTENT, source="paste", lead_id=lead.id)

    assert first.batch is not None and second.batch is not None
    assert first.batch.raw_id == second.batch.raw_id
    assert len(raws.list_for(lead.id)) == 1
    assert len(batches.list_by_status("pending_review")) == 2
