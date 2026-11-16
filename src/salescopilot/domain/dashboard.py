"""仪表盘指标的纯计算：注入 `now`，只读实体，不碰库、不碰时钟、不出现行业词。

每个指标的口径就写在它字段定义的那行注释里（分子 / 分母一目了然），
实现必须与注释一致；除数为 0 时一律返回 `0.0`，不返回 `NaN`。
关闭阶段从 `BLOCKS` 的「没有出口」推导，不在这里写死块名字符串。
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from salescopilot.domain.assignment import is_in_pool
from salescopilot.domain.blocks import BLOCKS
from salescopilot.domain.models import INITIAL_STAGE, Followup, Lead
from salescopilot.domain.reminders import compute_reminders, parse_moment

# 终止阶段 = 没有出口的块（v1 是 `close`）；推导而不是写死，块契约改了这里自动跟上。
TERMINAL_STAGES = frozenset(block_id for block_id, block in BLOCKS.items() if not block.exits)

WEEK = timedelta(days=7)
FIRST_RESPONSE_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class Dashboard:
    """一屏指标。

    - `total`：全部线索（含已关闭）——数的是「库里有几条」。
    - `pool`：全部线索里没有归属人的条数（公海）。
    - `new_this_week`：`created_at` 落在 [now - 7 天, now] 的线索数。
    - `followups_this_week`：跟进记录的 `at` 落在 [now - 7 天, now] 的条数。
    - `first_response_24h_rate`：分子 = 创建超过 24 小时且阶段已离开 `intake` 的线索；
      分母 = 创建超过 24 小时的线索；分母为 0 时 `0.0`。
    - `stale_ratio`：分子 = 未关闭线索中「最近一次跟进没有下次跟进时间、且距最近一次跟进
      （没有跟进则距创建）超过 `stale_followup_days`」的线索；分母 = 全部未关闭线索；
      分母为 0 时 `0.0`。
    - `silent_count`：未关闭线索中「距最近一次跟进（没有跟进则距创建）超过
      `stale_followup_days`」的线索数（不要求有没有下次跟进时间）。
    - `reminders_by_level`：三类提醒的条数，键恒为 `red` / `yellow` / `gray`（没有就是 0）。
    """

    total: int
    pool: int
    new_this_week: int
    followups_this_week: int
    first_response_24h_rate: float
    stale_ratio: float
    silent_count: int
    reminders_by_level: dict[str, int]


def compute_dashboard(
    *,
    leads: list[Lead],
    followups: list[Followup],
    template: dict,
    now: datetime,
) -> Dashboard:
    """按一批线索 / 跟进 / 一份模板阈值算一屏指标；`now` 由调用方注入。"""
    moment = _as_aware(now)
    week_start = moment - WEEK
    stale_days = _number(template, "stale_followup_days")

    latest_by_lead: dict[str, Followup] = {}
    for item in followups:
        lead_id = item.lead_id
        current = latest_by_lead.get(lead_id)
        if current is None:
            latest_by_lead[lead_id] = item
            continue
        previous_at = parse_moment(current.at)
        this_at = parse_moment(item.at)
        # 解析不了时间的按 `at` 原始字符串兜底，至少顺序稳定、不会丢记录。
        if (this_at is not None and previous_at is not None and this_at > previous_at) or (
            this_at is None and item.at > current.at
        ):
            latest_by_lead[lead_id] = item

    total = len(leads)
    pool = sum(1 for lead in leads if is_in_pool(lead))
    new_this_week = sum(
        1 for lead in leads if _in_window(parse_moment(lead.created_at), week_start, moment)
    )
    followups_this_week = sum(
        1 for item in followups if _in_window(parse_moment(item.at), week_start, moment)
    )

    first_eligible = 0
    first_responded = 0
    stale_numerator = 0
    open_leads = 0
    silent_count = 0

    for lead in leads:
        created = parse_moment(lead.created_at)
        latest = latest_by_lead.get(lead.id)

        # 首响率：只看创建超过 24 小时的线索；阶段离开 intake 即算已首联。
        if created is not None and moment - created > FIRST_RESPONSE_WINDOW:
            first_eligible += 1
            if lead.stage != INITIAL_STAGE:
                first_responded += 1

        # 关闭线索不进「未关闭」的分子分母，也不出提醒。
        if lead.stage in TERMINAL_STAGES:
            continue

        open_leads += 1
        baseline = _baseline(latest, created)
        idle_over = (
            stale_days is not None
            and baseline is not None
            and moment - baseline > timedelta(days=stale_days)
        )
        has_next = latest is not None and _as_date(latest.next_followup_at) is not None
        if idle_over and not has_next:
            stale_numerator += 1
        if idle_over:
            silent_count += 1

    reminders_by_level: dict[str, int] = {"red": 0, "yellow": 0, "gray": 0}
    for reminder in compute_reminders(
        leads=leads, followups=followups, template=template, now=moment
    ):
        reminders_by_level[reminder.level] = reminders_by_level.get(reminder.level, 0) + 1

    return Dashboard(
        total=total,
        pool=pool,
        new_this_week=new_this_week,
        followups_this_week=followups_this_week,
        first_response_24h_rate=_ratio(first_responded, first_eligible),
        stale_ratio=_ratio(stale_numerator, open_leads),
        silent_count=silent_count,
        reminders_by_level=reminders_by_level,
    )


def _baseline(latest: Followup | None, created: datetime | None) -> datetime | None:
    """久未跟进的计时起点：最近一次跟进的时间；一条都没有就用创建时间。"""
    if latest is None:
        return created
    return parse_moment(latest.at) or created


def _in_window(moment: datetime | None, start: datetime, end: datetime) -> bool:
    """闭区间 [start, end]；时间解析不出来的一律不计入。"""
    return moment is not None and start <= moment <= end


def _ratio(numerator: int, denominator: int) -> float:
    """除数为 0 返回 0.0（不是 NaN，也不是抛错）。"""
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _as_date(text: object) -> date | None:
    """下次跟进时间只认 `YYYY-MM-DD`；空串或解析不了返回 None。"""
    raw = text.strip() if isinstance(text, str) else ""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _number(template: object, key: str) -> float | None:
    """读模板阈值；缺键或不是数值返回 None（不兜底、不猜）。"""
    thresholds = template.get("thresholds") if isinstance(template, dict) else None
    if not isinstance(thresholds, dict):
        return None
    value = thresholds.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_aware(moment: datetime) -> datetime:
    """统一成带时区（UTC）：实体里存的是带偏移的 ISO 串，比较前先对齐。"""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment
