"""周复盘的纯规则：本周推进 / 卡点 / 分级变动 / 缺项 / 无价值跟进 + 三个校准问题。

四条纪律：
- 只做计算：入参是已取好的线索 / 阶段历史 / 跟进 / 分级留痕 / 提醒 / 信息卡，不查库、
  不读时钟（`now` 由调用方注入）、不发网络请求；
- **窗口只有一个定义**：`[now - REVIEW_WINDOW_DAYS, now]`（闭区间），所有数字共用它，
  不允许某个字段自己再算一遍「本周」；
- **只统计、不猜测**：时间解析不了的记录不进窗口（宁可少一条，也不编一个时间把它算进去）；
- 行业叫法不出现在这里：分区只认 `CARD_SECTIONS` 与其标签（标签来自模板）。

为什么 `REVIEW_WINDOW_DAYS` 是常量而不是模板参数：周是通用概念，和行业无关；
同一份引擎若允许每份模板各配一个「周」，同一个团队就会有两套「本周」，
跨模板的复盘数字对不上。将来要支持「月度复盘」时，加的是窗口长度参数，不是模板键。
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from salescopilot.domain.blocks import BLOCKS
from salescopilot.domain.card import Card
from salescopilot.domain.grading import GRADE_NEEDS_INFO
from salescopilot.domain.models import Followup, GradingRecord, Lead, StageEvent
from salescopilot.domain.reminders import Reminder, parse_moment
from salescopilot.domain.rules import ValidationIssue

# 周复盘的「本周」= 最近 7 天（滚动窗口）；理由见模块注释。
REVIEW_WINDOW_DAYS = 7

# 缺项排行只留前三：三行够人看清「本周最该补哪几块」，再多就成清单了。
MAX_MISSING_TOP = 3

# 校准问题的三条 key：固定三条，接口与样本表都按这三个 key 认。
QUESTION_B_TO_A = "b_to_a"
QUESTION_C_KEPT = "c_kept"
QUESTION_WRONG_GRADE = "wrong_grade"

# 三个校准问题（固定、中文）：问的是「人怎么判的」，不是「系统算出了什么」。
QUESTIONS: tuple[dict, ...] = (
    {"key": QUESTION_B_TO_A, "text": "这周你把哪家从 B 提到了 A？为什么？"},
    {"key": QUESTION_C_KEPT, "text": "哪家你标了 C 但一直没放弃？为什么？"},
    {"key": QUESTION_WRONG_GRADE, "text": "哪次分级你觉得系统判错了？错在哪？"},
)
# key → 中文问题：写 `preference_samples` 时 `question` 存这句人话，不存 key。
QUESTION_TEXTS: dict[str, str] = {item["key"]: item["text"] for item in QUESTIONS}

# 回答至少两个字：一个字的「好」说不清判断，事后回看等于没答。
MIN_ANSWER_LEN = 2
# 一次最多答三条（就是上面那三个问题），多出来的没有地方可问。
MAX_ANSWERS = len(QUESTIONS)

# 终止阶段（没有出口的块）从块契约推导，不在这里硬编码块名。
TERMINAL_STAGES = frozenset(block_id for block_id, block in BLOCKS.items() if not block.exits)


@dataclass(frozen=True)
class ReviewSummary:
    """一张周复盘：窗口边界 + 五类数字 + 三份明细 + 三个待答问题。

    - `window_start` / `window_end`：本节复盘的窗口（ISO8601，闭区间），
      前端直接显示「本周」的起止，不必自己再算一次；
    - `advanced`：窗口内 `stage_history` 的条数（推进了几次）；
    - `closed`：窗口内走进终止阶段（`BLOCKS` 里没有出口的块）的**线索数**——
      终止块没有出口，同一家正常只会走进去一次，所以它同时等于事件数；
    - `stuck`：当前卡住的提醒（每条带线索名与严重度），来自 `domain/reminders.py`；
    - `needs_info_leads`：最近一次分级落在 `needs_info` 的客户数（缺要素、判不出档）；
    - `missing_top`：按「缺这一块的客户数」降序的前三块，`count` 是客户数不是字段数；
    - `grading_changes`：本周人工改档，`from` 是上一条留痕的值、`to` 是本条人工值；
    - `no_value_followups`：本周 `value_provided` 为空的跟进（空转的那几次）；
    - `questions`：固定三个校准问题，每项 `{"key", "text"}`。
    """

    window_start: str
    window_end: str
    new_leads: int
    advanced: int
    closed: int
    stuck: list[dict] = field(default_factory=list)
    needs_info_leads: int = 0
    missing_top: list[dict] = field(default_factory=list)
    grading_changes: list[dict] = field(default_factory=list)
    no_value_followups: list[dict] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)


def review_window(now: datetime) -> tuple[datetime, datetime]:
    """回看窗口：`[now - REVIEW_WINDOW_DAYS 天, now]`，两端都算进窗口。

    两端都归一成带时区的 datetime（实体里存的是带偏移的 ISO 串），
    这样「7 天前那一秒」和「7 天前当天」不会被字符串比较糊过去。
    """
    end = _aware(now)
    return end - timedelta(days=REVIEW_WINDOW_DAYS), end


def within_window(at: str, start: datetime, end: datetime) -> bool:
    """这条时间戳落在窗口内吗；解析不了返回 False（不猜、不默认算进本周）。"""
    moment = parse_moment(at)
    if moment is None:
        return False
    return _aware(start) <= moment <= _aware(end)


def build_review(
    *,
    leads: list[Lead],
    stage_events: list[StageEvent],
    followups: list[Followup],
    grading_history: list[GradingRecord],
    reminders: list[Reminder],
    cards: list[Card],
    now: datetime,
) -> ReviewSummary:
    """按一批已取好的数据算一张周复盘；顺序不影响结果。

    - 线索 / 阶段历史 / 跟进 / 分级留痕都传**全部行**（跨线索即可），本层自己按线索分组；
    - `cards` 每家一张（`CardService.card` 的产出），只用来数「缺这一块的客户有几个」；
    - `now` 由调用方从注入的时钟取，本模块不看系统日历。
    """
    start, end = review_window(now)
    names = {lead.id: lead.name for lead in leads}

    events = [event for event in stage_events if within_window(event.at, start, end)]
    closed_leads = {event.lead_id for event in events if event.to_stage in TERMINAL_STAGES}

    return ReviewSummary(
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        new_leads=sum(1 for lead in leads if within_window(lead.created_at, start, end)),
        advanced=len(events),
        closed=len(closed_leads),
        stuck=stuck_items(reminders, names),
        needs_info_leads=needs_info_count(leads, grading_history),
        missing_top=missing_top(cards),
        grading_changes=grading_changes(grading_history, names, start=start, end=end),
        no_value_followups=no_value_followups(followups, names, start=start, end=end),
        questions=[dict(item) for item in QUESTIONS],
    )


def stuck_items(reminders: list[Reminder], names: dict[str, str]) -> list[dict]:
    """把提醒折成复盘里的「卡点」：带线索名，前端不必为显示名字再查一遍。

    顺序原样保留（提醒层已按严重度倒序），本层不再排一次。
    """
    return [
        {
            "lead_id": reminder.lead_id,
            "lead_name": names.get(reminder.lead_id, ""),
            "kind": reminder.kind,
            "level": reminder.level,
            "message": reminder.message,
        }
        for reminder in reminders
    ]


def needs_info_count(leads: list[Lead], grading_history: list[GradingRecord]) -> int:
    """最近一次分级是 `needs_info` 的客户数（按线索算，不按留痕条数算）。"""
    grouped = _group_history(grading_history)
    return sum(
        1 for lead in leads if _latest_grading_value(grouped.get(lead.id, [])) == GRADE_NEEDS_INFO
    )


def missing_top(cards: list[Card], *, limit: int = MAX_MISSING_TOP) -> list[dict]:
    """缺项排行：按「缺这一块的客户数」降序，同数保持 `CARD_SECTIONS` 的展示顺序，最多 `limit` 块。

    数的是**客户数**：一家缺三块就算三块各一次，但同一块里缺五个字段只算一次——
    复盘要看的是「多少家卡在这一块」，不是「一共缺几个空」。
    """
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for card in cards:
        seen: set[str] = set()
        for item in card.missing:
            section = _text(item.get("section"))
            if not section or section in seen:
                continue
            seen.add(section)
            counts[section] = counts.get(section, 0) + 1
            label = _text(item.get("section_label"))
            if label and section not in labels:
                labels[section] = label

    ordered = sorted(counts.items(), key=lambda pair: -pair[1])
    return [
        {"section": section, "section_label": labels.get(section, section), "count": count}
        for section, count in ordered[:limit]
    ]


def grading_changes(
    grading_history: list[GradingRecord],
    names: dict[str, str],
    *,
    start: datetime,
    end: datetime,
) -> list[dict]:
    """本周人工改档：窗口内 `human_value` 非空、且与前一条留痕的值不同的那些记录。

    「前一条的值」= 上一条有 `human_value` 的留痕，没有就取上一条的 `ai_value`；
    两条留痕值相同（人重复确认同一档）不算改档，不写进复盘——否则一周点两次就报两次变动。
    前面一条有值的留痕都没有（第一次给人定档）同样不算改档：那是「定档」不是「改判」，
    复盘问的是「你把哪家改了」，把首次定档也算进来会让这个数字失去意义。
    """
    grouped = _group_history(grading_history)
    changes: list[dict] = []
    for lead_id, records in grouped.items():
        previous = ""
        for record in records:
            human = _text(record.human_value)
            if human and previous and human != previous and within_window(record.at, start, end):
                changes.append(
                    {
                        "lead_id": lead_id,
                        "lead_name": names.get(lead_id, ""),
                        "from": previous,
                        "to": human,
                        "reason": _text(record.reason),
                        "at": record.at,
                    }
                )
            if human:
                previous = human
            elif _text(record.ai_value):
                previous = _text(record.ai_value)

    changes.sort(key=lambda item: (item["at"], item["lead_id"]))
    return changes


def no_value_followups(
    followups: list[Followup],
    names: dict[str, str],
    *,
    start: datetime,
    end: datetime,
) -> list[dict]:
    """本周「没给客户新东西」的跟进：`value_provided` 为空的那些（空 != 没记录）。"""
    items = [
        {
            "lead_id": item.lead_id,
            "lead_name": names.get(item.lead_id, ""),
            "at": item.at,
            "conclusion": item.conclusion,
        }
        for item in followups
        if within_window(item.at, start, end) and not _text(item.value_provided)
    ]
    items.sort(key=lambda entry: (entry["at"], entry["lead_id"]))
    return items


def validate_answers(answers: object) -> list[ValidationIssue]:
    """校验一次作答：1–3 条、key 必须是那三个、answer 至少 `MIN_ANSWER_LEN` 个字。

    先全量校验再写库（由用例层保证）：一半写进去、一半报错，会让「本周答过没有」
    这个判断变得说不清。
    """
    items = answers if isinstance(answers, list) else []
    if not items:
        return [ValidationIssue("answers", "至少要回答一个问题")]
    if len(items) > MAX_ANSWERS:
        return [ValidationIssue("answers", f"最多回答 {MAX_ANSWERS} 个问题")]

    issues: list[ValidationIssue] = []
    allowed = " / ".join(QUESTION_TEXTS)
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            issues.append(ValidationIssue(f"answers[{index}]", "每条回答必须是对象"))
            continue
        key = _text(item.get("key")).strip()
        answer = _text(item.get("answer")).strip()
        if key not in QUESTION_TEXTS:
            issues.append(
                ValidationIssue(f"answers[{index}].key", f"问题编号必须是 {allowed} 之一")
            )
        if len(answer) < MIN_ANSWER_LEN:
            issues.append(
                ValidationIssue(f"answers[{index}].answer", f"回答至少需要 {MIN_ANSWER_LEN} 个字符")
            )
    return issues


def _group_history(history: list[GradingRecord]) -> dict[str, list[GradingRecord]]:
    """按线索分组；组内保持调用方给的顺序（仓储是时间正序），不在这里再排。"""
    grouped: dict[str, list[GradingRecord]] = {}
    for record in history:
        grouped.setdefault(record.lead_id, []).append(record)
    return grouped


def _latest_grading_value(records: list[GradingRecord]) -> str:
    """该客户当前的档位：最近一条有值的留痕，优先 `human_value`，其次 `ai_value`。"""
    for record in reversed(records):
        human = _text(record.human_value)
        if human:
            return human
        ai = _text(record.ai_value)
        if ai:
            return ai
    return ""


def _aware(moment: datetime) -> datetime:
    """统一成带时区（UTC）：实体里存的都是带偏移的 ISO 串，比较前必须先对齐。"""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
