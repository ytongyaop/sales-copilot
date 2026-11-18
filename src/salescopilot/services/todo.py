"""今日待办用例：取线索 / 事实 / 跟进 / 分级 → 交给 `domain/todo.py` 排序。

分工固定：
- 排序、建议动作、价值点全在 `domain/todo.py`（纯函数，`today` 由本层注入）；
- 本层只做四件事：取全量线索、按线索取事实与跟进、把 `grading_history` 折成当前档位、
  按线索自己的模板调纯函数再合并；
- 本层不写库、不认识 SQLite，也不认识任何行业字段。

为什么按模板分组：模板决定「给什么价值」（`todo.values`）与条目上限（`todo.max_items`），
不同模板的客户不能用同一份文案排序；同一模板内排序口径完全一致，跨模板合并时按粗排分
（`TodoItem.priority`）稳定合并。
"""

from collections.abc import Callable
from datetime import date

from salescopilot.domain.grading import current_value, parse_iso_date
from salescopilot.domain.models import Lead
from salescopilot.domain.ports import (
    FactRepository,
    FollowupRepository,
    GradingRepository,
    LeadRepository,
)
from salescopilot.domain.todo import TodoItem, build_todo
from salescopilot.services.leads import utc_now_iso


class TodoService:
    """今日待办边界：无入参、条目列表出（没有可跟的客户就是空列表）。"""

    def __init__(
        self,
        leads: LeadRepository,
        facts: FactRepository,
        followups: FollowupRepository,
        grading_history: GradingRepository,
        template_loader: Callable[[str], dict],
        clock: Callable[[], str] | None = None,
    ) -> None:
        """`template_loader` 由装配点注入（已校验过的模板清单），本层不认识文件系统。

        `clock` 只用来取「今天」：领域层不看日历，同一批数据配同一天永远同一个顺序。
        """
        self._leads = leads
        self._facts = facts
        self._followups = followups
        self._grading_history = grading_history
        self._template_loader = template_loader
        self._clock = clock if clock is not None else utc_now_iso

    def list(self) -> list[TodoItem]:
        """今天的待办：已排序、已按各模板 `max_items` 截断；没有客户时是 `[]`。

        模板未知（线索挂了一份磁盘上不存在的模板）的客户直接跳过：没有价值文案就不给建议。
        """
        today = self._today()
        if today is None:
            return []

        leads = self._leads.list()
        if not leads:
            return []

        grouped: dict[str, list[Lead]] = {}
        for lead in leads:
            grouped.setdefault(lead.template, []).append(lead)

        items: list[TodoItem] = []
        for key in sorted(grouped):
            group = grouped[key]
            template = self._template(group[0].template)
            if template is None:
                continue
            facts = [fact for lead in group for fact in self._facts.list_for(lead.id)]
            followups = [item for lead in group for item in self._followups.list_for(lead.id)]
            grading = {lead.id: self._grade(lead.id) for lead in group}
            items.extend(
                build_todo(
                    leads=group,
                    facts=facts,
                    followups=followups,
                    grading=grading,
                    template=template,
                    today=today,
                )
            )

        # 跨模板合并：按粗排分稳定排序（同分保持各模板内部的原始顺序）。
        items.sort(key=lambda item: -item.priority)
        return items

    # -------------------------------------------------------------- 内部动作

    def _template(self, key: str) -> dict | None:
        """取模板数据；未知模板或不是映射都返回 None（该组客户不出待办）。"""
        try:
            template = self._template_loader(key)
        except KeyError:
            return None
        return template if isinstance(template, dict) else None

    def _grade(self, lead_id: str) -> str:
        """该客户当前档位：口径与 `domain/grading.current_value` 同源（人工值优先）。"""
        return current_value(self._grading_history.list_for(lead_id))

    def _today(self) -> date | None:
        """今天：只切注入时钟的日期部分；不是合法日期返回 None（不猜）。"""
        stamp = self._clock()
        return parse_iso_date(stamp[:10] if isinstance(stamp, str) else "")
