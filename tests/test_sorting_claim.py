"""待认领池（认领 / 新建客户 / 灰区复核）的服务测试：临时库 + 假模型，全程离线。

四条判据：
7. 认领到已有客户：状态变 `pending_review`、`lead_id` 写上，**不自动同意**（事实层零写入）；
8. 按名字新建客户：联系方式从要素里取（取不到就留空并给提醒）；
9. 非待认领状态不能认领（返回问题，不改库）；
10. 灰区复核：候选 ≤3 时给建议（只进响应、不落库）；没有候选就明说「请直接新建」。
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
)
from salescopilot.adapters.llm.fake import FakeSorting
from salescopilot.domain.models import DEFAULT_TEMPLATE, INITIAL_STAGE, Lead
from salescopilot.services.sorting import SortingService

# 没有任何候选：判为新客户候选，进待认领池。
NEW_CONTENT = "完全不相干的一通闲聊，预算还在谈，下周再说"
# 只差一个字级差别：落进复核区（85–95），有候选但不敢自动归属。
FUZZY_CONTENT = "华远建设工程有限公司那边说预算三十万，下周一下午再聊"
# 原文里带联系方式：新建客户时从要素里取得到。
CREATE_CONTENT = "远大科技有限公司，预算三十万 电话13800009999，下周再聊"

LEAD_A_NAME = "华远建设有限公司"
LEAD_A_CONTACT = "13800001111"


def _clock() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"2026-11-18T11:00:{next(counter):02d}+00:00"


@dataclass(frozen=True)
class _Setup:
    service: SortingService
    batches: SqliteSortingBatchRepository
    facts: SqliteFactRepository
    leads: SqliteLeadRepository
    lead_a: Lead


def _setup(tmp_path: Path) -> _Setup:
    db = tmp_path / "claim.sqlite3"
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
    leads.add(lead_a)
    service = SortingService(leads, raws, batches, FakeSorting(), clock=_clock(), facts=facts)
    return _Setup(service, batches, facts, leads, lead_a)


def _pending_claim(setup: _Setup, content: str) -> str:
    outcome = setup.service.sort(content=content, source="paste")
    assert outcome.batch is not None
    assert outcome.batch.status == "pending_claim"
    return outcome.batch.id


def test_claim_existing_lead_only_attaches_and_waits_for_review(tmp_path: Path) -> None:
    """认领到已有客户：挂上 `lead_id`、状态转待人确认；不自动同意、不落事实。"""
    setup = _setup(tmp_path)
    batch_id = _pending_claim(setup, NEW_CONTENT)

    batch, issues = setup.service.claim(batch_id, lead_id=setup.lead_a.id)

    assert issues == []
    assert batch is not None
    assert batch.lead_id == setup.lead_a.id
    assert batch.status == "pending_review"
    assert batch.decided_action is None, "认领不等于同意，处置留痕必须还是空"
    assert setup.facts.list_for(setup.lead_a.id) == []


def test_claim_with_create_name_uses_contact_from_elements(tmp_path: Path) -> None:
    """按名字新建客户：联系方式从要素里取，来源记 other，状态转待人确认。"""
    setup = _setup(tmp_path)
    batch_id = _pending_claim(setup, CREATE_CONTENT)

    batch, issues = setup.service.claim(batch_id, create_name="远大科技有限公司")

    assert issues == []
    assert batch is not None
    assert batch.status == "pending_review"
    assert batch.lead_id is not None

    created = setup.leads.get(batch.lead_id)
    assert created is not None
    assert created.name == "远大科技有限公司"
    assert created.contact == "13800009999"
    assert created.source == "other"
    assert created.stage == INITIAL_STAGE
    # 认领新建走默认模板（T010 起是主演示模板 tob_enterprise）；比较常量而不是字面量，
    # 换默认模板时这条断言跟着走，不必再改一次。
    assert created.template == DEFAULT_TEMPLATE


def test_claim_with_create_name_without_contact_warns_and_leaves_it_empty(
    tmp_path: Path,
) -> None:
    """要素里没有联系方式：客户照样建，联系方式留空并给一条提醒（不算失败）。"""
    setup = _setup(tmp_path)
    batch_id = _pending_claim(setup, NEW_CONTENT)

    batch, issues = setup.service.claim(batch_id, create_name="没留电话的新客户")

    assert batch is not None
    assert batch.status == "pending_review"
    assert [issue.location for issue in issues] == ["contact"]
    created = setup.leads.get(batch.lead_id)
    assert created is not None
    assert created.contact == ""


def test_claim_rejects_wrong_state_and_bad_arguments(tmp_path: Path) -> None:
    """非待认领状态 / 两个参数都给 / 客户不存在：一律返回问题且不改库。"""
    setup = _setup(tmp_path)
    pool_batch = _pending_claim(setup, NEW_CONTENT)

    both = setup.service.claim(pool_batch, lead_id=setup.lead_a.id, create_name="另一个名字")
    assert both[0] is None
    assert both[1][0].location == "lead_id"

    neither = setup.service.claim(pool_batch)
    assert neither[0] is None
    assert neither[1][0].location == "lead_id"

    unknown = setup.service.claim(pool_batch, lead_id="not-a-lead")
    assert unknown[0] is None
    assert unknown[1][0].location == "lead_id"
    assert setup.batches.get(pool_batch).status == "pending_claim"

    # 已经有归属的单不再接受认领。
    assigned = setup.service.sort(content=NEW_CONTENT, source="paste", lead_id=setup.lead_a.id)
    assert assigned.batch is not None
    wrong_state = setup.service.claim(assigned.batch.id, lead_id=setup.lead_a.id)
    assert wrong_state[0] is None
    assert wrong_state[1][0].location == "status"


def test_review_suggests_candidate_without_persisting(tmp_path: Path) -> None:
    """灰区复核：返回建议与理由，但分拣单的 `lead_id` 仍是空（只建议、不落库）。"""
    setup = _setup(tmp_path)
    batch_id = _pending_claim(setup, FUZZY_CONTENT)

    outcome = setup.service.review_candidates(batch_id)

    assert outcome.issues == []
    assert outcome.lead_id == setup.lead_a.id
    assert outcome.reason

    refreshed = setup.batches.get(batch_id)
    assert refreshed is not None
    assert refreshed.lead_id is None
    assert refreshed.status == "pending_claim"
    assert refreshed.decided_action is None


def test_review_without_candidates_asks_to_create_lead(tmp_path: Path) -> None:
    """没有候选：明说「请直接新建」，不拿模型瞎猜一个客户。"""
    setup = _setup(tmp_path)
    batch_id = _pending_claim(setup, NEW_CONTENT)

    outcome = setup.service.review_candidates(batch_id)

    assert outcome.lead_id is None
    assert outcome.issues[0].location == "candidates"
    assert "直接新建" in outcome.issues[0].message


def test_review_rejects_wrong_state(tmp_path: Path) -> None:
    """已经归属待确认的单不需要复核：返回状态问题。"""
    setup = _setup(tmp_path)
    outcome = setup.service.sort(content=NEW_CONTENT, source="paste", lead_id=setup.lead_a.id)
    assert outcome.batch is not None

    review = setup.service.review_candidates(outcome.batch.id)

    assert review.issues[0].location == "status"
