"""今日待办的纯规则：排序、建议动作、该给什么价值。

四层排序（逐层比较，前一层分不出来才看下一层）：
1. 分级 `A > B > C > unknown`；
2. 到期程度 `overdue`（约定已过）> `today`（约定就是今天）> `week`（窗口内）> `none`；
3. 缺项数多者优先；
4. 最久未联系者优先（最近一次跟进的时间；一条跟进都没有就按创建时间）。

五条纪律：
- 只做计算：入参是已取好的线索 / 事实 / 跟进 / 分级，不查库、不读时钟、不发网络请求；
- **不写死数字**：条目上限 `max_items`、一周窗口 `week_days`、四块的价值文案 `values`
  全部来自模板 `todo:` 段；缺键就当「没有这一档 / 这条不显示」，代码里不留兜底数字；
- **不给空建议**：`value_offered` 取不到就整条不出现——宁可不显示，也不给一句空话；
- 每家最多一条：待办是「今天先打给谁」，同一家出现两次只会占位置；
- 行业叫法不出现在这里：本模块只认 `CARD_SECTIONS` 与模板给的值。
"""

from dataclasses import dataclass
from datetime import date, timedelta

from salescopilot.domain.card import build_card
from salescopilot.domain.models import CARD_SECTIONS, Fact, Followup, Lead

# 分级的比较顺序：A 最靠前，`unknown`（含 `needs_info`、没分级）垫底。
GRADE_ORDER: tuple[str, ...] = ("A", "B", "C", "unknown")
GRADE_RANK: dict[str, int] = {
    grade: len(GRADE_ORDER) - index for index, grade in enumerate(GRADE_ORDER)
}
# 理由里给分级用的中文：`A 类` / `未分级`（不给结论时也说得清楚「这条还没判过档」）。
GRADE_LABELS: dict[str, str] = {"A": "A 类", "B": "B 类", "C": "C 类", "unknown": "未分级"}

# 到期程度的比较顺序：越靠前越急。
DUE_ORDER: tuple[str, ...] = ("overdue", "today", "week", "none")
DUE_RANK: dict[str, int] = {kind: len(DUE_ORDER) - index for index, kind in enumerate(DUE_ORDER)}

# 待办条目上最多带几条缺项标签：三条够人一眼看清「先问哪几句」，再多就成清单了。
MAX_MISSING_LABELS = 3
# 理由里最多点几项缺项：`缺「决策者」「决策流程」` 读起来正好，五项就没人读了。
REASON_MISSING_SHOWN = 2

# 排序用的粗排分进位：分级一档差 100，到期一档差 10，缺项数压在小数位（最多数到 9）。
PRIORITY_GRADE_STEP = 100
PRIORITY_DUE_STEP = 10
PRIORITY_MISSING_CAP = 9


@dataclass(frozen=True)
class TodoItem:
    """一条待办：今天该跟谁、为什么、做什么、给什么。

    - `grade`：A / B / C / unknown（`needs_info` 也归 unknown：它表示「还没判出来」）；
    - `priority`：前三层的粗排分（越大越靠前），只用于排序说明；第四层「最久未联系」
      不进这个数，靠时间兜底；
    - `due_kind`：`overdue` / `today` / `week` / `none`，口径见 `_due_kind`；
    - `reason`：为什么现在要跟（人话）；`action`：今天做什么；
    - `value_offered`：这次给客户什么（来自模板 `todo.values`，一定非空）；
    - `missing`：还缺的要素中文标签，最多 `MAX_MISSING_LABELS` 条。
    """

    lead_id: str
    lead_name: str
    grade: str
    priority: int
    due_kind: str
    reason: str
    action: str
    value_offered: str
    missing: list[str]


def build_todo(
    *,
    leads: list[Lead],
    facts: list[Fact],
    followups: list[Followup],
    grading: dict[str, str],
    template: dict,
    today: date,
) -> list[TodoItem]:
    """按一份模板给这批线索排今日待办；返回已排序、已截断的条目。

    - `facts` / `followups` 传**全部**行即可（内部按 `lead_id` 分组），顺序不影响结果；
    - `grading` 是「线索 id → 该客户当前档位」，不认识的取值按 `unknown` 处理；
    - 模板没有 `todo:` 段或其中 `values` / `max_items` 不合法时返回空列表
      （没有价值文案就不该给建议，这里不猜一句凑数）。
    """
    spec = todo_spec(template)
    if spec is None:
        return []

    values, max_items, week_days = spec
    facts_by_lead = _group_by_lead(facts)
    followups_by_lead = _group_by_lead(followups)

    rows: list[tuple[int, int, int, str, TodoItem]] = []
    for lead in leads:
        lead_facts = facts_by_lead.get(lead.id, [])
        lead_followups = followups_by_lead.get(lead.id, [])
        missing = _missing(template, lead_facts)
        item = _build_item(
            lead,
            missing,
            lead_followups,
            values=values,
            week_days=week_days,
            grade=_grade_of(grading.get(lead.id)),
            today=today,
        )
        if item is None:
            continue
        rows.append(
            (
                -GRADE_RANK[item.grade],
                -DUE_RANK[item.due_kind],
                -len(missing),
                _last_contact(lead, lead_followups),
                item,
            )
        )

    rows.sort(key=lambda row: row[:4])
    return [row[4] for row in rows[:max_items]]


def todo_spec(template: dict) -> tuple[dict[str, str], int, int | None] | None:
    """读模板 `todo:` 段：返回（价值文案, 条目上限, 一周窗口天数）；不可用返回 None。

    - `values` 逐块解析：只收「非空字符串」的条目，缺的块**不补**——该块对应的客户会在
      `_value_offered` 那里落空、整条不出现（模板校验 `validate_template` 会先把这种模板
      拦在门外，这里只是不让它变成一句空建议）；
    - 一块可用文案都没有、或 `max_items` 不是正整数 → 整份待办都不出（返回 None）；
    - `week_days` 没写就是 None —— 这时不产出 `week` 档，而不是在代码里补一个 7。
    """
    todo = template.get("todo") if isinstance(template, dict) else None
    if not isinstance(todo, dict):
        return None

    raw_values = todo.get("values")
    if not isinstance(raw_values, dict):
        return None
    values: dict[str, str] = {}
    for key in CARD_SECTIONS:
        text = raw_values.get(key)
        if isinstance(text, str) and text.strip():
            values[key] = text.strip()
    if not values:
        return None

    max_items = todo.get("max_items")
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0:
        return None

    week_days = todo.get("week_days")
    if isinstance(week_days, bool) or not isinstance(week_days, int) or week_days <= 0:
        week_days = None
    return values, max_items, week_days


def _build_item(
    lead: Lead,
    missing: list[dict],
    followups: list[Followup],
    *,
    values: dict[str, str],
    week_days: int | None,
    grade: str,
    today: date,
) -> TodoItem | None:
    """算一条待办；拿不到价值文案时返回 None（这条不出现）。"""
    sections = [item["section"] for item in missing]
    due_date = _due_date(followups)
    due_kind = _due_kind(due_date, today=today, week_days=week_days)

    value_offered = _value_offered(values, sections)
    if not value_offered:
        return None

    labels = [item["label"] for item in missing]
    missing_count = min(len(labels), PRIORITY_MISSING_CAP)
    return TodoItem(
        lead_id=lead.id,
        lead_name=lead.name,
        grade=grade,
        priority=(
            GRADE_RANK[grade] * PRIORITY_GRADE_STEP
            + DUE_RANK[due_kind] * PRIORITY_DUE_STEP
            + missing_count
        ),
        due_kind=due_kind,
        reason=_reason(grade, due_kind, due_date, today=today, labels=labels),
        action=_action(labels, due_kind),
        value_offered=value_offered,
        missing=labels[:MAX_MISSING_LABELS],
    )


def _missing(template: dict, facts: list[Fact]) -> list[dict]:
    """缺项清单（复用 T005 的 `build_card`）：顺序就是 `basic → needs → decision → followup`。"""
    return build_card(template, facts).missing


def _value_offered(values: dict[str, str], sections: list[str]) -> str:
    """该给什么：取**第一个缺项**所在分区的文案；都不缺就取 `followup` 的。

    模板缺这一块的文案时返回空串 —— 上层据此把整条待办拿掉，不拿别的块凑。
    """
    key = sections[0] if sections else "followup"
    return values.get(key, "")


def _action(labels: list[str], due_kind: str) -> str:
    """今天做什么：补缺项优先，其次按约定的紧迫程度给固定说法（确定性、中文）。"""
    if labels:
        return f"补问「{labels[0]}」"
    if due_kind == "overdue":
        return "按上次约定回电"
    if due_kind == "today":
        return "今天跟进"
    if due_kind == "week":
        return "本周内联系"
    return "确认下一次联系时间"


def _reason(
    grade: str, due_kind: str, due_date: date | None, *, today: date, labels: list[str]
) -> str:
    """为什么现在跟：`A 类 · 约定昨天已过 · 缺「决策者」「决策流程」`。"""
    parts = [GRADE_LABELS[grade], _due_phrase(due_kind, due_date, today), _missing_phrase(labels)]
    return " · ".join(parts)


def _due_phrase(due_kind: str, due_date: date | None, today: date) -> str:
    """到期程度的人话：昨天 / 今天 / N 天后 / 还没约定。

    `due_kind == "none"` 有两种来路：真没约（`due_date is None`）与「约了但还在窗口外」。
    后者必须说「下次约定在 N 天后」——人家明明约了，说「还没约定」就是栽赃。
    """
    if due_date is None:
        return "还没约定下次时间"
    if due_kind == "none":
        return f"下次约定在 {(due_date - today).days} 天后"
    if due_kind == "overdue":
        days = (today - due_date).days
        return "约定昨天已过" if days == 1 else f"约定 {days} 天前已过"
    if due_kind == "today":
        return "约定今天"
    return f"约定 {(due_date - today).days} 天后"


def _missing_phrase(labels: list[str]) -> str:
    """缺项的人话：最多点前两项，一项不缺就明说「要素齐全」。"""
    if not labels:
        return "要素齐全"
    shown = "".join(f"「{label}」" for label in labels[:REASON_MISSING_SHOWN])
    return f"缺{shown}"


def _due_kind(due_date: date | None, *, today: date, week_days: int | None) -> str:
    """到期程度：约定已过 / 就是今天 / 一周内 / 没有或还远。

    `week` 的窗口天数只来自模板 `todo.week_days`；没配就没有这一档（不兜底 7 天）。
    """
    if due_date is None:
        return "none"
    if due_date < today:
        return "overdue"
    if due_date == today:
        return "today"
    if week_days is not None and due_date <= today + timedelta(days=week_days):
        return "week"
    return "none"


def _due_date(followups: list[Followup]) -> date | None:
    """约定时间：最近一次跟进里的 `next_followup_at`；没有跟进或解析不了返回 None。"""
    latest = _latest_followup(followups)
    if latest is None:
        return None
    raw = latest.next_followup_at.strip() if isinstance(latest.next_followup_at, str) else ""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _last_contact(lead: Lead, followups: list[Followup]) -> str:
    """最近一次联系的时间：取最近一条跟进的 `at`；一条都没有就按创建时间。

    两者都是带偏移的 ISO 串（同一时钟产出），字符串比较即可得到先后。
    """
    latest = _latest_followup(followups)
    if latest is not None and latest.at:
        return latest.at
    return lead.created_at


def _latest_followup(followups: list[Followup]) -> Followup | None:
    """最近一条跟进：按 `at` 取最大（解析不了时间时退化成字符串比较）。"""
    if not followups:
        return None
    return max(followups, key=lambda item: item.at)


def _grade_of(value: str | None) -> str:
    """档位归一：只认 A / B / C；`needs_info`、空值与其他取值一律 `unknown`。"""
    text = value.strip().upper() if isinstance(value, str) else ""
    return text if text in GRADE_RANK else "unknown"


def _group_by_lead(items: list[Fact] | list[Followup]) -> dict[str, list]:
    """按 `lead_id` 分组：调用方传全量行，本层自己分组，顺序保持原样。"""
    grouped: dict[str, list] = {}
    for item in items:
        grouped.setdefault(item.lead_id, []).append(item)
    return grouped
