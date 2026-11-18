"""周复盘用例：取本周数据 → 交 `domain/review.py` 汇总 → 收三个校准问题的答案。

分工固定：
- 统计口径、窗口长度、三个问题、作答校验全在 `domain/review.py`（纯函数，`now` 由本层注入）；
- 本层只做四件事：取全量线索与各自的阶段历史 / 跟进 / 分级留痕、调提醒与信息卡两个用例
  拿卡点与缺项、把答案写进 `preference_samples`、回答「本周答过没有」；
- 本层不写业务规则，不自己算窗口，也不认识 SQLite。

`template_loader` 由装配点注入（与 `CardService` / `TodoService` 同一套入参）：
信息卡已由 `card_service` 组装，本层不直接读模板，留着它是为了让装配点只维护一条取值路径。
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from salescopilot.domain.models import PreferenceSample
from salescopilot.domain.ports import (
    FollowupRepository,
    GradingRepository,
    LeadRepository,
    PreferenceSampleRepository,
    StageEventRepository,
)
from salescopilot.domain.reminders import parse_moment
from salescopilot.domain.review import (
    QUESTION_TEXTS,
    ReviewSummary,
    build_review,
    review_window,
    validate_answers,
    within_window,
)
from salescopilot.domain.rules import ValidationIssue
from salescopilot.services.card import CardService
from salescopilot.services.leads import utc_now_iso
from salescopilot.services.reminder_service import ReminderService


class ReviewService:
    """周复盘边界：无入参出汇总；作答进、（写了几条, 问题列表）出。"""

    def __init__(
        self,
        leads: LeadRepository,
        stage_events: StageEventRepository,
        followups: FollowupRepository,
        grading_history: GradingRepository,
        reminder_service: ReminderService,
        card_service: CardService,
        samples: PreferenceSampleRepository,
        template_loader: Callable[[str], dict],
        clock: Callable[[], str] | None = None,
    ) -> None:
        """依赖全部由装配点注入：四条业务仓储 + 提醒 / 信息卡两个用例 + 偏好样本仓储。"""
        self._leads = leads
        self._stage_events = stage_events
        self._followups = followups
        self._grading_history = grading_history
        self._reminder_service = reminder_service
        self._card_service = card_service
        self._samples = samples
        self._template_loader = template_loader
        self._clock = clock if clock is not None else utc_now_iso

    def summary(self) -> ReviewSummary:
        """本周复盘：没有客户时也返回一张空复盘（数字全 0、问题照给）。

        `answered` 不在这里：那是「这周答过没有」的接口层拼装件，
        由路由调 `answered_this_week()` 与汇总合并，服务不替接口决定输出形状。
        """
        leads = self._leads.list()
        cards = []
        for lead in leads:
            card, _issues = self._card_service.card(lead.id)
            if card is not None:
                cards.append(card)

        return build_review(
            leads=leads,
            stage_events=_flat(self._stage_events.list_for(lead.id) for lead in leads),
            followups=_flat(self._followups.list_for(lead.id) for lead in leads),
            grading_history=_flat(self._grading_history.list_for(lead.id) for lead in leads),
            reminders=self._reminder_service.list_all(),
            cards=cards,
            now=self._now(),
        )

    def answer(
        self, answers: list[dict], *, actor: str | None = None
    ) -> tuple[int, list[ValidationIssue]]:
        """写一次作答：每条一行 `preference_samples`，`question` 存中文问题。

        校验不过就**一条都不写**（先全量校验再落库）：半条写进去会让
        「本周答过没有」变成说不清的状态。

        `actor` 目前不做记录：`preference_samples` 没有操作者列，
        与其在 `answer` 里拼一句「李娜：…」，不如等表要留痕时再加一列。
        """
        issues = validate_answers(answers)
        if issues:
            return 0, issues

        at = self._clock()
        items = answers if isinstance(answers, list) else []
        for item in items:
            key = item["key"].strip() if isinstance(item, dict) else ""
            # key 已由 `validate_answers` 保证是三个之一，这里只取它对的中文问题。
            question = QUESTION_TEXTS.get(key)
            if not question:
                continue
            self._samples.add(
                PreferenceSample(
                    id=str(uuid.uuid4()),
                    question=question,
                    answer=item["answer"].strip(),
                    at=at,
                    lead_id=None,
                )
            )
        return len(items), []

    def answered_this_week(self) -> bool:
        """本周（同一窗口）是否已经答过：只要有一行样本落在窗口内就算答过。"""
        return bool(self.answers_this_week())

    def answers_this_week(self) -> list[dict]:
        """本周已作答的样本：`[{"key","question","answer","at"}]`，按作答时间正序。

        - 窗口与复盘数字同一个（`review_window`），不放宽也不另算一次「本周」；
        - `key` 由问题中文反查 `QUESTION_TEXTS`：样本表里存的是人话问题，接口要还给前端编号；
          反查不到（历史上存过别的问题句）就给空串，不猜它属于哪一问；
        - 只读：本方法不写库，作答入口仍然只有 `answer()`。
        """
        start, end = review_window(self._now())
        key_of = {text: key for key, text in QUESTION_TEXTS.items()}
        items = [
            {
                "key": key_of.get(sample.question.strip(), ""),
                "question": sample.question,
                "answer": sample.answer,
                "at": sample.at,
            }
            for sample in self._samples.list_all()
            if within_window(sample.at, start, end)
        ]
        items.sort(key=lambda item: item["at"])
        return items

    # -------------------------------------------------------------- 内部动作

    def _now(self) -> datetime:
        """注入时钟 → 带时区的时刻；解析不了就退回当前时刻（不让接口 500）。"""
        moment = parse_moment(self._clock())
        return moment if moment is not None else datetime.now(UTC)


def _flat(groups) -> list:
    """把「按线索分组的一批列表」摊平成一个列表：本层不关心分组，纯函数自己分。"""
    return [item for group in groups for item in group]
