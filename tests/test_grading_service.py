"""分级用例的测试：提议 / 定档 / 双模式 / 留痕只追加（临时库 + 真实模板，全程离线）。

五条判据：
1. `propose` 只追加一行建议（`ai_value` 有值、`human_value` 为空、`mode='propose'`）；
2. 改档必须写理由（≥2 字），不改档可以不写；
3. `auto` 模式：建议之后立刻再落一行「采纳」（`ai_value == human_value`）；
4. 历史按时间倒序、只追加（定两次档就多两行，旧行一个字都不改）；
5. `needs_info` 也能直接定档，但同样要写理由。
"""

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from salescopilot.adapters.db.sqlite import (
    SqliteFactRepository,
    SqliteGradingRepository,
    SqliteLeadRepository,
)
from salescopilot.adapters.template.loader import load_all
from salescopilot.domain.models import (
    DEFAULT_TEMPLATE,
    INITIAL_STAGE,
    Fact,
    GradingRecord,
    Lead,
)
from salescopilot.services.grading import GradingService

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "salescopilot" / "templates"
TEMPLATES = load_all(TEMPLATES_DIR)

# 假时钟的日期部分固定：紧迫度按「距今天几天」分档，必须钉住。
TODAY = "2026-11-18"


def _template_loader(key: str) -> dict:
    return TEMPLATES[key]


def _clock() -> Callable[[], str]:
    """每秒递增的假时钟：同一批操作里的留痕顺序稳定可断言。"""
    counter = itertools.count(1)
    return lambda: f"{TODAY}T10:00:{next(counter):02d}+00:00"


@dataclass(frozen=True)
class _Setup:
    service: GradingService
    leads: SqliteLeadRepository
    facts: SqliteFactRepository
    history: SqliteGradingRepository
    lead: Lead


def _fact(field: str, value: str) -> Fact:
    return Fact(
        id=f"fact-{field}",
        lead_id="lead-1",
        raw_id="raw-1",
        field=field,
        value=value,
        source="manual",
        source_span=f"{field} 的原文：{value}",
        confidence=1.0,
        locked=False,
        at=f"{TODAY}T09:00:00+00:00",
    )


FULL_FACTS = [
    _fact("amount", "60万"),
    _fact("appointment_at", (date(2026, 11, 18) + timedelta(days=3)).isoformat()),
    _fact("decision_maker", "王总"),
    _fact("approval_flow", "总部审批后签约"),
    _fact("our_fit", "方案正好对上他们的需求"),
]


def _setup(tmp_path: Path, *, facts: list[Fact] | None = None) -> _Setup:
    db = tmp_path / "grading.sqlite3"
    leads = SqliteLeadRepository(db)
    repo = SqliteFactRepository(db)
    history = SqliteGradingRepository(db)
    lead = Lead(
        id="lead-1",
        name="华远建设有限公司",
        contact="13800001111",
        source="inbound",
        stage=INITIAL_STAGE,
        owner=None,
        created_at=f"{TODAY}T09:00:00+00:00",
        template=DEFAULT_TEMPLATE,
    )
    leads.add(lead)
    for fact in FULL_FACTS if facts is None else facts:
        repo.add(fact)

    service = GradingService(leads, repo, history, _template_loader, clock=_clock())
    return _Setup(service, leads, repo, history, lead)


# ------------------------------------------------------------------ 提议


def test_propose_appends_one_suggestion_row(tmp_path: Path) -> None:
    """提议只落一行：AI 值有、人工值为空、模式是 propose、理由来自规则。"""
    setup = _setup(tmp_path)

    result, issues = setup.service.propose(setup.lead.id)

    assert issues == []
    assert result is not None
    assert result.value == "A"

    rows = setup.history.list_for(setup.lead.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.mode == "propose"
    assert row.ai_value == "A"
    assert row.human_value is None
    assert row.reason == result.reason
    assert row.actor is None


def test_propose_unknown_lead_returns_issue(tmp_path: Path) -> None:
    """线索不存在：不落任何留痕，问题位置是 lead_id（接口据此 404）。"""
    setup = _setup(tmp_path)

    result, issues = setup.service.propose("not-a-lead")

    assert result is None
    assert [issue.location for issue in issues] == ["lead_id"]
    assert setup.history.list_for("not-a-lead") == []


def test_propose_with_bad_mode_is_rejected(tmp_path: Path) -> None:
    """模式只认 propose / auto：别的值不落库，问题位置是 mode。"""
    setup = _setup(tmp_path)

    result, issues = setup.service.propose(setup.lead.id, mode="silent")

    assert result is None
    assert [issue.location for issue in issues] == ["mode"]
    assert setup.history.list_for(setup.lead.id) == []


# ------------------------------------------------------------------ 定档


def test_decide_changing_value_requires_reason(tmp_path: Path) -> None:
    """改档不给理由 → 拒绝，且不写任何行（留痕里不该出现「无理由的改判」）。"""
    setup = _setup(tmp_path)
    setup.service.propose(setup.lead.id)

    decision, issues = setup.service.decide(setup.lead.id, value="C")

    assert decision is None
    assert [issue.location for issue in issues] == ["reason"]
    assert len(setup.history.list_for(setup.lead.id)) == 1, "只有提议那一行"


def test_decide_reason_too_short_is_rejected(tmp_path: Path) -> None:
    """改档理由至少 2 个字。"""
    setup = _setup(tmp_path)
    setup.service.propose(setup.lead.id)

    decision, issues = setup.service.decide(setup.lead.id, value="C", reason="贵")

    assert decision is None
    assert [issue.location for issue in issues] == ["reason"]
    assert "至少需要 2 个字符" in issues[0].message


def test_decide_with_reason_records_ai_and_human_values(tmp_path: Path) -> None:
    """改档成功：新行同时留下 AI 原值与人工值，理由原样存。"""
    setup = _setup(tmp_path)
    setup.service.propose(setup.lead.id)

    decision, issues = setup.service.decide(
        setup.lead.id, value="C", reason="预算砍半，先降级", actor="李娜"
    )

    assert issues == []
    assert decision is not None
    record = decision.record
    assert record.ai_value == "A"
    assert record.human_value == "C"
    assert record.reason == "预算砍半，先降级"
    assert record.actor == "李娜"
    assert record.mode == "propose"
    assert len(setup.history.list_for(setup.lead.id)) == 2


def test_decide_without_changing_value_needs_no_reason(tmp_path: Path) -> None:
    """不改档可以不写理由：人工值等于 AI 值，就是「确认这一档」。"""
    setup = _setup(tmp_path)
    setup.service.propose(setup.lead.id)

    decision, issues = setup.service.decide(setup.lead.id, value="A")

    assert issues == []
    assert decision is not None
    assert decision.record.ai_value == "A"
    assert decision.record.human_value == "A"
    assert decision.record.reason is None


def test_decide_invalid_value_is_rejected(tmp_path: Path) -> None:
    """档次只认 A / B / C。"""
    setup = _setup(tmp_path)

    decision, issues = setup.service.decide(setup.lead.id, value="D", reason="随便定")

    assert decision is None
    assert [issue.location for issue in issues] == ["value"]


def test_decide_needs_info_needs_reason_too(tmp_path: Path) -> None:
    """`needs_info` 直接定档：必填理由；写了就落一行（AI 值是 needs_info）。"""
    setup = _setup(tmp_path, facts=[_fact("need", "想换一套流程")])
    result, _ = setup.service.propose(setup.lead.id)
    assert result is not None and result.value == "needs_info"

    blocked, issues = setup.service.decide(setup.lead.id, value="B")
    assert blocked is None
    assert [issue.location for issue in issues] == ["reason"]

    decision, issues = setup.service.decide(setup.lead.id, value="B", reason="老板电话里确认过预算")
    assert issues == []
    assert decision is not None
    assert decision.record.ai_value == "needs_info"
    assert decision.record.human_value == "B"


# ------------------------------------------------------------------ 双模式


def test_auto_mode_appends_suggestion_then_adoption(tmp_path: Path) -> None:
    """auto 模式：先落建议行（propose），再落采纳行（auto，AI 值 = 人工值）。"""
    setup = _setup(tmp_path)

    result, issues = setup.service.propose(setup.lead.id, mode="auto")

    assert issues == []
    assert result is not None
    rows = setup.history.list_for(setup.lead.id)
    assert [row.mode for row in rows] == ["propose", "auto"]

    suggestion, adoption = rows
    assert suggestion.ai_value == result.value
    assert suggestion.human_value is None
    assert adoption.ai_value == result.value
    assert adoption.human_value == result.value
    assert adoption.reason and adoption.reason != result.reason


def test_template_default_mode_is_used_when_not_passed(tmp_path: Path) -> None:
    """不传 mode 就用模板 `grading.default_mode`（三份模板当前都是 propose）。"""
    setup = _setup(tmp_path)

    setup.service.propose(setup.lead.id)

    rows = setup.history.list_for(setup.lead.id)
    assert [row.mode for row in rows] == ["propose"]


def test_decide_keeps_the_mode_in_use(tmp_path: Path) -> None:
    """定档沿用当前模式（最近一行的模式），不会因为默认值不同而跳模式。"""
    setup = _setup(tmp_path)
    setup.service.propose(setup.lead.id, mode="auto")

    decision, issues = setup.service.decide(setup.lead.id, value="B", reason="复核后发现预算不够")

    assert issues == []
    assert decision is not None
    assert decision.record.mode == "auto"


# ------------------------------------------------------------------ 历史


def test_history_is_newest_first_and_append_only(tmp_path: Path) -> None:
    """历史倒序、只追加：定两次档就多两行，最早那行内容不变。"""
    setup = _setup(tmp_path)
    setup.service.propose(setup.lead.id)
    first = setup.history.list_for(setup.lead.id)[0]

    setup.service.decide(setup.lead.id, value="B", reason="先降一档观察")
    setup.service.decide(setup.lead.id, value="C", reason="客户预算确认砍半")

    rows, issues = setup.service.history(setup.lead.id)

    assert issues == []
    assert len(rows) == 3
    assert [row["human_value"] for row in rows] == ["C", "B", None]
    assert rows[-1]["id"] == first.id
    assert rows[-1]["reason"] == first.reason, "旧行一个字都不改"
    assert isinstance(setup.history.list_for(setup.lead.id)[0], GradingRecord)


def test_history_of_unknown_lead_returns_issue(tmp_path: Path) -> None:
    """线索不存在：返回空列表 + lead_id 问题（接口据此 404）。"""
    setup = _setup(tmp_path)

    rows, issues = setup.service.history("not-a-lead")

    assert rows == []
    assert [issue.location for issue in issues] == ["lead_id"]
