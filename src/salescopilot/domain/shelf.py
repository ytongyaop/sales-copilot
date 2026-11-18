"""客户货架的纯规则：一条客户 ＋ 它的信息卡 ＋ 当前档位 → 一张小卡。

三条纪律：
- 只做计算：入参是已取好的线索 / 信息卡 / 档位 / 跟进，不查库、不读时钟、不发请求；
- **不重算覆盖度与缺项**：比例与缺项数直接取 `domain/card.py` 的产出，货架上看到的数字
  必须与详情页一致（两处各算一遍，迟早对不上）；
- 档位只认 `domain/grading.py` 给的 A / B / C / `needs_info` / `unknown`，不在这里另定一套。
"""

from dataclasses import dataclass

from salescopilot.domain.card import Card
from salescopilot.domain.models import Followup, Lead


@dataclass(frozen=True)
class ShelfEntry:
    """货架上的一张客户小卡：谁、归谁、档位、覆盖度、缺几项、下次约定。

    - `grade`：`domain/grading.current_value` 的结果（`unknown` 表示还没分级，页面归「待分级」）；
    - `coverage`：信息卡覆盖度（0..1）；`filled` / `total` 是它的分子分母，页面画比例条用；
    - `missing_count`：信息卡还缺几个字段（与缺项问题句同一个清单）；
    - `next_appointment`：最近一次跟进里的下次约定日期；没有就是空串；
    - `reminder_level`：卡片右上角那个色点用的严重度（`red` / `yellow` / `gray`），
      由 `domain/reminders.py` 判定后**原样带过来**，本层不重判超时；没有提醒就是空串。
    """

    lead_id: str
    name: str
    owner: str
    grade: str
    coverage: float
    filled: int
    total: int
    missing_count: int
    next_appointment: str
    reminder_level: str = ""


def build_entry(
    *,
    lead: Lead,
    card: Card,
    grade: str,
    followups: list[Followup],
    reminder_level: str = "",
) -> ShelfEntry:
    """拼一张小卡：客户的名称 / 归属人 + 信息卡的两个数 + 下次约定 + 提醒严重度。

    `reminder_level` 由调用方从提醒用例取来传入（没有提醒用默认空串）：
    货架只负责把它画成色点，判定口径仍然只有 `domain/reminders.py` 一处。
    """
    return ShelfEntry(
        lead_id=lead.id,
        name=lead.name,
        owner=lead.owner.strip() if isinstance(lead.owner, str) else "",
        grade=grade,
        coverage=card.coverage,
        filled=sum(section.filled for section in card.sections),
        total=sum(section.total for section in card.sections),
        missing_count=len(card.missing),
        next_appointment=next_appointment(followups),
        reminder_level=reminder_level,
    )


def next_appointment(followups: list[Followup]) -> str:
    """下次约定：最近一条跟进里的 `next_followup_at`（口径同 `domain/todo.py` 的约定时间）。

    「最近一条」按 `at` 取最大；那一条没写下次时间就是空串，不往前翻旧记录凑一个日期——
    旧约定被新跟进覆盖之后就不再是「下次」了。
    """
    if not followups:
        return ""
    latest = max(followups, key=lambda item: item.at)
    raw = latest.next_followup_at
    return raw.strip() if isinstance(raw, str) else ""
