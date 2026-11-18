"""提醒的纯规则：三类超时算成红 / 黄 / 灰（注入 now，阈值只来自模板）。

三条纪律：
1. **不碰时钟**：`now` 由调用方注入，本模块只做计算，测试才能确定；
2. **不写死阈值**：分钟数 / 天数一律读 `template["thresholds"]`，缺键就跳过该规则，
   不在代码里留一个「兜底 30 分钟」之类的隐藏口径；
3. **一条线索只报一条**：同 kind 只出一条，多个 kind 并存时保留最严重的（red > yellow > gray）；
   已到终止阶段（`close`）的线索一律不报——结束的线索不该再占待办视野。
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from salescopilot.domain.blocks import BLOCKS
from salescopilot.domain.models import INITIAL_STAGE, Followup, Lead

FIRST_RESPONSE = "first_response"
FEEDBACK_WINDOW = "feedback_window"
STALE_FOLLOWUP = "stale_followup"

# 小时换算：文案在「不足一天」时按小时说，够了才按天说——不再出现「已过 0 天」。
HOURS_PER_DAY = 24
SECONDS_PER_HOUR = 3600

# 反馈窗口超时的文案模板：`N` 由 `_overdue_message` 填，单位二选一。
FEEDBACK_OVERDUE_HOURS = "约定时间已过 {n} 小时未更新"
FEEDBACK_OVERDUE_DAYS = "约定时间已过 {n} 天未更新"

# 严重度：数字越大越严重；排序与「取最严重」都只认这张表。
SEVERITY: dict[str, int] = {"red": 3, "yellow": 2, "gray": 1}

# 终止阶段（没有出口的块）从块契约推导，不在这里硬编码块名。
TERMINAL_STAGES = frozenset(block_id for block_id, block in BLOCKS.items() if not block.exits)


@dataclass(frozen=True)
class Reminder:
    """一条提醒。

    - `kind`：哪一类超时（`first_response` / `feedback_window` / `stale_followup`）；
    - `level`：`red` / `yellow` / `gray`；
    - `since_minutes`：从该规则的计时起点算起已经过了多少分钟（前端排序与展示用）。
    """

    lead_id: str
    kind: str
    level: str
    message: str
    since_minutes: int


def compute_reminders(
    *,
    leads: list[Lead],
    followups: list[Followup],
    template: dict,
    now: datetime,
) -> list[Reminder]:
    """按一份模板算这批线索的提醒；返回按严重度倒序、同级别按线索 id 排序的列表。"""
    thresholds = template.get("thresholds") if isinstance(template, dict) else None
    if not isinstance(thresholds, dict):
        return []

    grouped: dict[str, list[Followup]] = {}
    for item in followups:
        grouped.setdefault(item.lead_id, []).append(item)

    moment = _as_aware(now)
    reminders: list[Reminder] = []
    for lead in leads:
        if lead.stage in TERMINAL_STAGES:
            continue
        candidates = _candidates(lead, grouped.get(lead.id, []), thresholds, moment)
        picked = _most_severe(candidates)
        if picked is not None:
            reminders.append(picked)

    return sorted(reminders, key=lambda item: (-SEVERITY[item.level], item.lead_id))


def parse_moment(text: str) -> datetime | None:
    """ISO8601 文本 → 带时区的 datetime；解析不了返回 None（调用方据此跳过该条）。"""
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return _as_aware(parsed)


def threshold_of(template: dict, key: str) -> float | None:
    """读模板阈值；缺键或不是数值时返回 None（不兜底、不猜）。"""
    thresholds = template.get("thresholds") if isinstance(template, dict) else None
    if not isinstance(thresholds, dict):
        return None
    value = thresholds.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _candidates(
    lead: Lead, items: list[Followup], thresholds: dict, now: datetime
) -> list[Reminder]:
    """一条线索的全部候选提醒（可能有三类），后续再取最严重的一条。"""
    first_minutes = _number(thresholds, "first_response_minutes")
    feedback_hours = _number(thresholds, "feedback_window_hours")
    stale_days = _number(thresholds, "stale_followup_days")
    created = parse_moment(lead.created_at)
    latest = _latest(items)

    out: list[Reminder] = []

    # 1. 首响超时（红）：还在入库阶段，创建至今已超过 first_response_minutes。
    if first_minutes is not None and lead.stage == INITIAL_STAGE and created is not None:
        waited = now - created
        if waited > timedelta(minutes=first_minutes):
            out.append(
                Reminder(
                    lead.id,
                    FIRST_RESPONSE,
                    "red",
                    f"超过 {_label(first_minutes)} 分钟未首联",
                    int(waited.total_seconds() // 60),
                )
            )

    # 2. 反馈窗口（黄）／没有下次跟进时间（灰）：都看最近一次跟进。
    #
    # 反馈窗口口径：「约定时间 = next_followup_at 的日期 00:00 + feedback_window_hours 小时」，
    # 过了这个截止时间仍没有更晚的跟进记录 → 黄。
    # 最近一次跟进本身就是最新那条，所以「之后没有新跟进」等价于「它约定的截止时间已过」；
    # 小时数只来自模板阈值，缺键就整条规则跳过（不在代码里兜底 0 小时）。
    if latest is not None:
        agreed = _as_date(latest.next_followup_at)
        if agreed is None:
            baseline = parse_moment(latest.at) or created
            out.append(
                Reminder(
                    lead.id,
                    STALE_FOLLOWUP,
                    "gray",
                    "没有下次跟进时间",
                    _minutes_since(baseline, now),
                )
            )
        elif feedback_hours is not None:
            deadline = datetime.combine(agreed, time.min, UTC) + timedelta(hours=feedback_hours)
            if now > deadline:
                out.append(
                    Reminder(
                        lead.id,
                        FEEDBACK_WINDOW,
                        "yellow",
                        _overdue_message(agreed, now),
                        _minutes_since(deadline, now),
                    )
                )

    # 3. 久未新增跟进（灰）：以最后一次跟进（没有就跟创建时间）为起点。
    if stale_days is not None:
        baseline = parse_moment(latest.at) if latest is not None else created
        if baseline is not None and now - baseline > timedelta(days=stale_days):
            out.append(
                Reminder(
                    lead.id,
                    STALE_FOLLOWUP,
                    "gray",
                    f"超过 {_label(stale_days)} 天未新增跟进",
                    int((now - baseline).total_seconds() // 60),
                )
            )

    return out


def _overdue_message(agreed: date, now: datetime) -> str:
    """反馈窗口超时的人话：不足 1 天按小时说，够 1 天才按天说。

    起点是**约定时间当天 00:00**（不含 `feedback_window_hours`）：那句话说的是
    「约定时间已过多久」，窗口只是「什么时候开始算超时」。不足一小时记 1 小时，
    不写「已过 0 小时」——已经判了超时，说 0 只会让人以为算错了。
    """
    elapsed = now - datetime.combine(agreed, time.min, UTC)
    hours = max(1, int(elapsed.total_seconds() // SECONDS_PER_HOUR))
    if hours < HOURS_PER_DAY:
        return FEEDBACK_OVERDUE_HOURS.format(n=hours)
    return FEEDBACK_OVERDUE_DAYS.format(n=hours // HOURS_PER_DAY)


def _most_severe(reminders: list[Reminder]) -> Reminder | None:
    """同一线索只留一条：取最严重的；同 kind 重复也在这里一起收掉。"""
    if not reminders:
        return None
    return max(reminders, key=lambda item: SEVERITY[item.level])


def _latest(items: list[Followup]) -> Followup | None:
    """最近一次跟进：按 `at` 取最大；解析不了时间的按字符串兜底比较。"""
    if not items:
        return None
    return max(items, key=lambda item: item.at)


def _as_date(text: object) -> date | None:
    """下次跟进时间是 `YYYY-MM-DD`；解析不了（含空串）返回 None。"""
    raw = text.strip() if isinstance(text, str) else ""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _as_aware(moment: datetime) -> datetime:
    """统一成带时区（UTC）：实体里存的都是带偏移的 ISO 串，比较前必须先对齐。"""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def _minutes_since(start: datetime | None, now: datetime) -> int:
    if start is None:
        return 0
    return max(0, int((now - _as_aware(start)).total_seconds() // 60))


def _number(thresholds: dict, key: str) -> float | None:
    value = thresholds.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _label(value: float) -> str:
    """阈值展示：整数按整数显示（30 分钟，而不是 30.0 分钟）。"""
    return str(int(value)) if float(value).is_integer() else str(value)
