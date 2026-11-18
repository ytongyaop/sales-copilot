"""今日待办的纯规则测试：四层排序、每家一条、价值点非空、动作文案（离线、不落库）。

每条断言对应 `domain/todo.py` 的一条判据；模板是手写的最小映射，不读磁盘。
"""

from datetime import date

from salescopilot.domain.models import Fact, Followup, Lead
from salescopilot.domain.todo import build_todo

TODAY = date(2026, 11, 18)

# 四个分区各一个字段：缺项数就是「没填的字段数」，最大 4。
VALUES = {
    "basic": "公司背景资料",
    "needs": "同类客户案例",
    "decision": "方案与报价区间说明",
    "followup": "上次承诺的跟进",
}


def _spec(*, values: dict | None = None, max_items: int = 20, week_days: int | None = 7) -> dict:
    """最小模板：四块各一个字段；`todo` 段按需裁剪（用来验「缺文案就不出条目」）。"""
    todo = {"values": dict(VALUES if values is None else values), "max_items": max_items}
    if week_days is not None:
        todo["week_days"] = week_days
    return {
        "card": {
            "sections": {
                "basic": {
                    "label": "基础信息",
                    "fields": [{"key": "company", "label": "公司全称", "question": "哪家？"}],
                },
                "needs": {
                    "label": "需求与痛点",
                    "fields": [{"key": "need", "label": "核心需求", "question": "要什么？"}],
                },
                "decision": {
                    "label": "决策流程与预算",
                    "fields": [
                        {"key": "decision_maker", "label": "决策者", "question": "谁签字？"},
                        {"key": "approval_flow", "label": "决策流程", "question": "走什么流程？"},
                    ],
                },
                "followup": {
                    "label": "跟进记录",
                    "fields": [
                        {"key": "next_step", "label": "下一步动作", "question": "下次谈什么？"}
                    ],
                },
            }
        },
        "todo": todo,
    }


def _lead(lead_id: str, *, created_at: str = "2026-11-01T09:00:00+00:00") -> Lead:
    return Lead(
        id=lead_id,
        name=f"客户-{lead_id}",
        contact="13800001111",
        source="inbound",
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
    )


def _fill(lead_id: str, fields: list[str]) -> list[Fact]:
    return [_fact(lead_id, field) for field in fields]


# 四个字段名：填满即「要素齐全」。
FULL = ["company", "need", "decision_maker", "approval_flow", "next_step"]


def test_first_layer_is_grade() -> None:
    """只有分级不同：A → B → C → unknown（没判过档的垫底）。"""
    leads = [_lead("c"), _lead("unknown"), _lead("b"), _lead("a")]
    grading = {"a": "A", "b": "B", "c": "C", "unknown": "needs_info"}

    items = build_todo(
        leads=leads, facts=[], followups=[], grading=grading, template=_spec(), today=TODAY
    )

    assert [item.lead_id for item in items] == ["a", "b", "c", "unknown"]
    assert [item.grade for item in items] == ["A", "B", "C", "unknown"]


def test_second_layer_is_due_kind() -> None:
    """同分级看到期：已过期 → 今天 → 一周内 → 没约定。"""
    leads = [_lead("none"), _lead("week"), _lead("today"), _lead("overdue")]
    followups = [
        _followup("overdue", due="2026-11-16"),
        _followup("today", due="2026-11-18"),
        _followup("week", due="2026-11-21"),
    ]

    items = build_todo(
        leads=leads,
        facts=[],
        followups=followups,
        grading=dict.fromkeys(["none", "week", "today", "overdue"], "B"),
        template=_spec(),
        today=TODAY,
    )

    assert [(item.lead_id, item.due_kind) for item in items] == [
        ("overdue", "overdue"),
        ("today", "today"),
        ("week", "week"),
        ("none", "none"),
    ]


def test_third_layer_is_missing_count() -> None:
    """同分级同到期看缺项：缺得多的先做（先把该问的问全）。"""
    leads = [_lead("full"), _lead("half"), _lead("most")]
    facts = _fill("half", ["company", "need", "decision_maker", "approval_flow"])
    facts += _fill("most", ["company", "need"]) + _fill("full", FULL)

    items = build_todo(
        leads=leads,
        facts=facts,
        followups=[],
        grading=dict.fromkeys(["full", "half", "most"], "B"),
        template=_spec(),
        today=TODAY,
    )

    assert [item.lead_id for item in items] == ["most", "half", "full"]
    assert [len(item.missing) for item in items] == [3, 1, 0]


def test_fourth_layer_is_longest_silence() -> None:
    """前三层打平：最久没联系的先做（无跟进按创建时间）。"""
    leads = [
        _lead("recent", created_at="2026-11-17T09:00:00+00:00"),
        _lead("oldest", created_at="2026-10-01T09:00:00+00:00"),
        _lead("middle", created_at="2026-11-05T09:00:00+00:00"),
    ]

    items = build_todo(
        leads=leads,
        facts=[],
        followups=[],
        grading=dict.fromkeys(["recent", "oldest", "middle"], "B"),
        template=_spec(),
        today=TODAY,
    )

    assert [item.lead_id for item in items] == ["oldest", "middle", "recent"]


def test_each_lead_appears_at_most_once() -> None:
    """同一家只出一条：多条事实 / 多条跟进不会让它占两个位置。"""
    leads = [_lead("a"), _lead("b")]
    facts = _fill("a", ["company"]) + _fill("a", ["company"])
    followups = [
        _followup("a", due="2026-11-16"),
        _followup("a", due="2026-11-19", at="2026-11-12T10:00:00+00:00"),
    ]

    items = build_todo(
        leads=leads,
        facts=facts,
        followups=followups,
        grading={"a": "A", "b": "A"},
        template=_spec(),
        today=TODAY,
    )

    assert [item.lead_id for item in items] == ["a", "b"]
    assert len({item.lead_id for item in items}) == len(items)


def test_value_offered_comes_from_the_first_missing_section() -> None:
    """价值点取「第一个缺项所在分区」的文案：只缺决策块就给方案与报价说明。"""
    facts = _fill("a", ["company", "need", "next_step"])

    items = build_todo(
        leads=[_lead("a")],
        facts=facts,
        followups=[],
        grading={"a": "B"},
        template=_spec(),
        today=TODAY,
    )

    assert items[0].missing[0] == "决策者"
    assert items[0].value_offered == VALUES["decision"]
    assert items[0].action == "补问「决策者」"


def test_value_offered_falls_back_to_followup_when_nothing_is_missing() -> None:
    """要素齐全时给「上次承诺的跟进」——这是唯一不需要补齐场景的默认价值。"""
    items = build_todo(
        leads=[_lead("a")],
        facts=_fill("a", FULL),
        followups=[],
        grading={"a": "B"},
        template=_spec(),
        today=TODAY,
    )

    assert items[0].missing == []
    assert items[0].value_offered == VALUES["followup"]


def test_item_without_template_value_is_filtered_out() -> None:
    """模板缺这一块的文案 → 该条**不出现**（宁可不显示，也不给空建议）。"""
    values = {key: value for key, value in VALUES.items() if key != "decision"}
    facts = _fill("a", ["company", "need", "next_step"]) + _fill("b", ["company"])

    items = build_todo(
        leads=[_lead("a"), _lead("b")],
        facts=facts,
        followups=[],
        grading={"a": "A", "b": "A"},
        template=_spec(values=values),
        today=TODAY,
    )

    # a 的第一个缺项在 decision（无文案）→ 被过滤；b 的第一个缺项在 needs（有文案）→ 留下
    assert [item.lead_id for item in items] == ["b"]
    assert items[0].value_offered == VALUES["needs"]


def test_action_wording_for_due_kinds() -> None:
    """动作文案四种分支：缺项 → 回电 → 今天 → 本周内。"""
    facts = _fill("overdue", FULL) + _fill("today", FULL) + _fill("week", FULL)
    followups = [
        _followup("overdue", due="2026-11-17"),
        _followup("today", due="2026-11-18"),
        _followup("week", due="2026-11-20"),
    ]

    items = build_todo(
        leads=[_lead("overdue"), _lead("today"), _lead("week"), _lead("missing")],
        facts=facts,
        followups=followups,
        grading={"missing": "A"},
        template=_spec(),
        today=TODAY,
    )

    actions = {item.lead_id: item.action for item in items}
    assert actions["missing"] == "补问「公司全称」"
    assert actions["overdue"] == "按上次约定回电"
    assert actions["today"] == "今天跟进"
    assert actions["week"] == "本周内联系"


def test_reason_spells_out_grade_due_and_missing() -> None:
    """理由是人话：`A 类 · 约定昨天已过 · 缺「决策者」「决策流程」`。"""
    items = build_todo(
        leads=[_lead("a")],
        facts=_fill("a", ["company", "need", "next_step"]),
        followups=[_followup("a", due="2026-11-17")],
        grading={"a": "A"},
        template=_spec(),
        today=TODAY,
    )

    assert items[0].reason == "A 类 · 约定昨天已过 · 缺「决策者」「决策流程」"
    assert items[0].priority > 0


def test_week_window_comes_from_template_only() -> None:
    """没配 `week_days` 就没有「一周内」这一档——代码里不留兜底 7 天。"""
    followups = [_followup("a", due="2026-11-21")]

    with_window = build_todo(
        leads=[_lead("a")],
        facts=[],
        followups=followups,
        grading={"a": "B"},
        template=_spec(week_days=7),
        today=TODAY,
    )
    without_window = build_todo(
        leads=[_lead("a")],
        facts=[],
        followups=followups,
        grading={"a": "B"},
        template=_spec(week_days=None),
        today=TODAY,
    )

    assert with_window[0].due_kind == "week"
    assert without_window[0].due_kind == "none"


def test_max_items_truncates_from_the_template() -> None:
    """条目上限来自模板 `max_items`：留最靠前的几条，不是前几个碰巧排到的。"""
    leads = [_lead("a"), _lead("b"), _lead("c")]

    items = build_todo(
        leads=leads,
        facts=[],
        followups=[],
        grading={"a": "A", "b": "B", "c": "unknown"},
        template=_spec(max_items=2),
        today=TODAY,
    )

    assert [item.lead_id for item in items] == ["a", "b"]


def test_template_without_todo_section_returns_nothing() -> None:
    """模板没有 `todo:` 段（老模板）→ 不出待办，而不是编一份默认文案。"""
    spec = _spec()
    del spec["todo"]

    items = build_todo(
        leads=[_lead("a")],
        facts=[],
        followups=[],
        grading={"a": "A"},
        template=spec,
        today=TODAY,
    )

    assert items == []
