"""周复盘用例测试：临时库 + 真模板 + 注入时钟，全程离线。

四条判据：
1. `summary()` 的窗口数字正确（本周新增 1、推进 2、关闭 1），且卡点 / 缺项 / 无价值跟进都有内容；
2. `missing_top` 用的是信息卡的真实缺项（两家客户都没事实 → 四块各 2 家，取前三块）；
3. `answer()` 每条写一行 `preference_samples`（`question` 存中文问题），校验失败一条都不写；
4. `answered_this_week()` 作答前 False、作答后 True。
"""

from pathlib import Path

from salescopilot.adapters.db.sqlite import (
    SqliteFactRepository,
    SqliteFollowupRepository,
    SqliteGradingRepository,
    SqliteLeadRepository,
    SqlitePreferenceSampleRepository,
    SqliteStageEventRepository,
)
from salescopilot.adapters.template.loader import load_all
from salescopilot.domain.models import Followup, GradingRecord, Lead, StageEvent
from salescopilot.services.card import CardService
from salescopilot.services.reminder_service import ReminderService
from salescopilot.services.review import ReviewService

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "salescopilot" / "templates"
NOW_STAMP = "2026-11-18T09:00:00+00:00"


class Setup:
    """一套真仓储 + 真模板装配出来的复盘环境（每个用例一份临时库）。"""

    def __init__(self, tmp_path: Path) -> None:
        db = tmp_path / "review.sqlite3"
        self.leads = SqliteLeadRepository(db)
        self.events = SqliteStageEventRepository(db)
        self.followups = SqliteFollowupRepository(db)
        self.history = SqliteGradingRepository(db)
        self.samples = SqlitePreferenceSampleRepository(db)
        self.facts = SqliteFactRepository(db)

        loaded = load_all(TEMPLATES_DIR)

        def loader(key: str) -> dict:
            """按 key 取真模板；未知 key 仍抛 KeyError，与装配点的口径一致。"""
            return loaded[key]

        self.service = ReviewService(
            self.leads,
            self.events,
            self.followups,
            self.history,
            ReminderService(self.leads, self.followups, loader, clock=lambda: NOW_STAMP),
            CardService(self.leads, self.facts, loader),
            self.samples,
            loader,
            clock=lambda: NOW_STAMP,
        )


def _lead(lead_id: str, *, created_at: str, stage: str = "intake") -> Lead:
    return Lead(
        id=lead_id,
        name=f"客户-{lead_id}",
        contact="13800001111",
        source="referral",
        stage=stage,
        owner=None,
        created_at=created_at,
    )


def _fill_busy_week(setup: Setup) -> None:
    """造一周：a 本周新增且推进到底，b 是老客户但本周被人工改档，a 有一次空转跟进。"""
    setup.leads.add(_lead("a", created_at="2026-11-17T08:00:00+00:00"))
    setup.leads.add(_lead("b", created_at="2026-10-01T08:00:00+00:00"))
    setup.leads.update_stage("a", "close")

    setup.events.add(
        StageEvent(
            id="event-1",
            lead_id="a",
            from_stage="intake",
            to_stage="first_contact",
            at="2026-11-17T09:00:00+00:00",
        )
    )
    setup.events.add(
        StageEvent(
            id="event-2",
            lead_id="a",
            from_stage="proposal",
            to_stage="close",
            at="2026-11-18T08:00:00+00:00",
        )
    )

    setup.followups.add(
        Followup(
            id="followup-empty",
            lead_id="a",
            conclusion="只是问候了一下",
            objection="time",
            next_followup_at="2026-11-20",
            at="2026-11-17T10:00:00+00:00",
            value_provided="",
        )
    )
    setup.followups.add(
        Followup(
            id="followup-value",
            lead_id="a",
            conclusion="给了报价区间说明",
            objection="price",
            next_followup_at="2026-11-21",
            at="2026-11-17T11:00:00+00:00",
            value_provided="报价区间说明",
        )
    )

    setup.history.add(
        GradingRecord(
            id="grade-b-1",
            lead_id="b",
            mode="propose",
            at="2026-11-16T08:00:00+00:00",
            ai_value="B",
        )
    )
    setup.history.add(
        GradingRecord(
            id="grade-b-2",
            lead_id="b",
            mode="auto",
            at="2026-11-17T12:00:00+00:00",
            ai_value="B",
            human_value="A",
            reason="预算与决策人都已落实",
        )
    )


def test_summary_counts_the_week(tmp_path: Path) -> None:
    """本周新增 1、推进 2、关闭 1；空转跟进 1 次；人工改档 1 次；卡点只剩还没首联的老客户。"""
    setup = Setup(tmp_path)
    _fill_busy_week(setup)

    summary = setup.service.summary()

    assert summary.window_start == "2026-11-11T09:00:00+00:00"
    assert summary.window_end == NOW_STAMP
    assert summary.new_leads == 1
    assert summary.advanced == 2
    assert summary.closed == 1
    assert summary.needs_info_leads == 0
    assert [item["lead_id"] for item in summary.stuck] == ["b"]
    assert summary.stuck[0]["lead_name"] == "客户-b"
    assert [item["lead_id"] for item in summary.no_value_followups] == ["a"]
    assert [item["lead_id"] for item in summary.grading_changes] == ["b"]
    assert (summary.grading_changes[0]["from"], summary.grading_changes[0]["to"]) == ("B", "A")
    assert [item["section"] for item in summary.missing_top] == ["basic", "needs", "decision"]
    assert [item["count"] for item in summary.missing_top] == [2, 2, 2]
    assert [item["key"] for item in summary.questions] == ["b_to_a", "c_kept", "wrong_grade"]


def test_summary_on_empty_database_is_all_zeros(tmp_path: Path) -> None:
    """空库照样出一张复盘：数字全 0、明细空、三个问题照给。"""
    summary = Setup(tmp_path).service.summary()

    assert (summary.new_leads, summary.advanced, summary.closed) == (0, 0, 0)
    assert summary.stuck == []
    assert summary.missing_top == []
    assert len(summary.questions) == 3


def test_answer_writes_one_row_per_question(tmp_path: Path) -> None:
    """三条作答写三行样本：`question` 存中文问题、`answer` 去空白、`at` 取注入时钟。"""
    setup = Setup(tmp_path)

    saved, issues = setup.service.answer(
        [
            {"key": "b_to_a", "answer": " 城建集团：预算批下来了 "},
            {"key": "c_kept", "answer": "一直跟着，对方在等立项"},
            {"key": "wrong_grade", "answer": "系统把老客户判成 C，其实复购意向很强"},
        ],
        actor="李娜",
    )

    assert (saved, issues) == (3, [])
    rows = setup.samples.list_all()
    assert len(rows) == 3
    assert rows[0].question == "这周你把哪家从 B 提到了 A？为什么？"
    assert rows[0].answer == "城建集团：预算批下来了"
    assert rows[1].question == "哪家你标了 C 但一直没放弃？为什么？"
    assert rows[2].question == "哪次分级你觉得系统判错了？错在哪？"
    assert {row.at for row in rows} == {NOW_STAMP}


def test_answer_rejects_bad_key_and_short_text_without_writing(tmp_path: Path) -> None:
    """问题编号不认识、回答太短、条数越界都报 422 级问题，且一条都不写。"""
    setup = Setup(tmp_path)

    bad_key, key_issues = setup.service.answer([{"key": "unknown", "answer": "随便答一句"}])
    short, short_issues = setup.service.answer([{"key": "b_to_a", "answer": "好"}])
    empty, empty_issues = setup.service.answer([])
    too_many, count_issues = setup.service.answer(
        [
            {"key": "b_to_a", "answer": "理由一"},
            {"key": "c_kept", "answer": "理由二"},
            {"key": "wrong_grade", "answer": "理由三"},
            {"key": "b_to_a", "answer": "理由四"},
        ]
    )

    assert (bad_key, short, empty, too_many) == (0, 0, 0, 0)
    assert [issue.location for issue in key_issues] == ["answers[0].key"]
    assert [issue.location for issue in short_issues] == ["answers[0].answer"]
    assert len(empty_issues) == 1
    assert len(count_issues) == 1
    assert setup.samples.list_all() == []


def test_answered_this_week_flips_after_answering(tmp_path: Path) -> None:
    """作答前 False、作答后 True（窗口内有一行样本即算答过）。"""
    setup = Setup(tmp_path)
    assert setup.service.answered_this_week() is False

    setup.service.answer([{"key": "c_kept", "answer": "这家一直在等预算"}])

    assert setup.service.answered_this_week() is True
