"""今日待办用例测试：临时库 + 真模板数据，全程离线。

四条判据：
1. 三个客户（A / B / 没判过档，其中一个约定已过期）→ 顺序按分级，再看到期；
2. 分级取最近一次的 `human_value`，没有才取 `ai_value`；
3. 每条都带价值点与建议动作，`lead_id` 不重复；
4. 空库返回 `[]`。
"""

from pathlib import Path

from salescopilot.adapters.db.sqlite import (
    SqliteFactRepository,
    SqliteFollowupRepository,
    SqliteGradingRepository,
    SqliteLeadRepository,
)
from salescopilot.adapters.template.loader import load_all
from salescopilot.domain.models import Fact, Followup, GradingRecord, Lead
from salescopilot.services.todo import TodoService

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "salescopilot" / "templates"
TODAY_STAMP = "2026-11-18T09:00:00+00:00"


def _build(tmp_path: Path) -> tuple[TodoService, SqliteLeadRepository, dict]:
    """装配一套真仓储 + 真模板；返回（服务, 线索仓储, 其余仓储字典）。"""
    db = tmp_path / "todo.sqlite3"
    leads = SqliteLeadRepository(db)
    facts = SqliteFactRepository(db)
    followups = SqliteFollowupRepository(db)
    history = SqliteGradingRepository(db)
    loaded = load_all(TEMPLATES_DIR)
    service = TodoService(
        leads,
        facts,
        followups,
        history,
        lambda key: loaded[key],
        clock=lambda: TODAY_STAMP,
    )
    return service, leads, {"facts": facts, "followups": followups, "history": history}


def _lead(lead_id: str, *, created_at: str = "2026-11-01T09:00:00+00:00") -> Lead:
    return Lead(
        id=lead_id,
        name=f"客户-{lead_id}",
        contact="13800001111",
        source="referral",
        stage="intake",
        owner=None,
        created_at=created_at,
    )


def _fact(lead_id: str, field: str) -> Fact:
    return Fact(
        id=f"fact-{lead_id}-{field}",
        lead_id=lead_id,
        raw_id="raw-1",
        field=field,
        value="已采集",
        source="llm",
        source_span="已采集",
        confidence=0.8,
        locked=False,
        at="2026-11-02T09:00:00+00:00",
        card_section="",
    )


def _followup(lead_id: str, *, due: str, at: str = "2026-11-10T10:00:00+00:00") -> Followup:
    return Followup(
        id=f"followup-{lead_id}",
        lead_id=lead_id,
        conclusion="客户说要再看看",
        objection="price",
        next_followup_at=due,
        at=at,
        value_provided="同类客户案例",
    )


def _grade(lead_id: str, *, at: str, ai_value: str | None = None, human: str | None = None):
    return GradingRecord(
        id=f"grade-{lead_id}-{at}",
        lead_id=lead_id,
        mode="propose" if human is None else "auto",
        at=at,
        ai_value=ai_value,
        human_value=human,
    )


def test_order_follows_grade_then_due(tmp_path: Path) -> None:
    """A（约定已过期）→ B → 没判过档的客户：先按分级，再看到期。"""
    service, leads, repos = _build(tmp_path)
    for lead_id in ("a", "b", "c"):
        leads.add(_lead(lead_id, created_at=f"2026-11-0{1 + len(lead_id)}T09:00:00+00:00"))
    repos["history"].add(_grade("a", at="2026-11-18T08:00:00+00:00", ai_value="A", human="A"))
    repos["history"].add(_grade("b", at="2026-11-18T08:05:00+00:00", ai_value="B"))
    repos["followups"].add(_followup("a", due="2026-11-16"))
    repos["followups"].add(_followup("b", due="2026-11-30"))
    repos["facts"].add(_fact("a", "company"))

    items = service.list()

    assert [item.lead_id for item in items] == ["a", "b", "c"]
    assert [item.grade for item in items] == ["A", "B", "unknown"]
    assert items[0].due_kind == "overdue"
    assert items[0].action  # 有缺项时是「补问…」，无论如何不能是空
    assert items[0].value_offered
    assert "约定 2 天前已过" in items[0].reason


def test_latest_human_value_beats_ai_value(tmp_path: Path) -> None:
    """改判后按人工值排队：AI 说 A、人改成 C，就排在 B 后面。"""
    service, leads, repos = _build(tmp_path)
    leads.add(_lead("a", created_at="2026-11-02T09:00:00+00:00"))
    leads.add(_lead("b", created_at="2026-11-03T09:00:00+00:00"))
    repos["history"].add(_grade("a", at="2026-11-10T09:00:00+00:00", ai_value="A"))
    repos["history"].add(_grade("a", at="2026-11-11T09:00:00+00:00", ai_value="A", human="C"))
    repos["history"].add(_grade("b", at="2026-11-10T09:00:00+00:00", ai_value="B"))

    items = service.list()

    assert [item.lead_id for item in items] == ["b", "a"]
    assert [item.grade for item in items] == ["B", "C"]


def test_every_item_has_value_and_unique_lead(tmp_path: Path) -> None:
    """每条都带价值点、建议动作与最多三条缺项；同一家不会出现两次。"""
    service, leads, repos = _build(tmp_path)
    leads.add(_lead("a"))
    repos["history"].add(_grade("a", at="2026-11-18T08:00:00+00:00", ai_value="A", human="A"))

    items = service.list()

    assert len(items) == 1
    item = items[0]
    assert item.value_offered.strip()
    assert item.action.strip()
    assert item.reason.strip()
    assert item.lead_name == "客户-a"
    assert 0 < len(item.missing) <= 3
    assert len({it.lead_id for it in items}) == len(items)


def test_empty_database_returns_empty_list(tmp_path: Path) -> None:
    """没有线索 → `[]`（不是 None、也不编一条占位）。"""
    service, _leads, _repos = _build(tmp_path)

    assert service.list() == []


def test_unknown_template_is_skipped_not_crashed(tmp_path: Path) -> None:
    """线索挂着磁盘上不存在的模板：跳过它，其余客户照常出待办。"""
    service, leads, _repos = _build(tmp_path)
    leads.add(
        Lead(
            id="ghost",
            name="客户-ghost",
            contact="13800001111",
            source="referral",
            stage="intake",
            owner=None,
            created_at="2026-11-01T09:00:00+00:00",
            template="not_a_template",
        )
    )
    leads.add(_lead("ok"))

    items = service.list()

    assert [item.lead_id for item in items] == ["ok"]
