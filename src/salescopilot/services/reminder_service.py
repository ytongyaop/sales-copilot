"""提醒与自动回收用例：读线索 + 跟进 + 模板阈值 → 出提醒 / 把超时线索放回公海。

分工：判定全在 `domain/reminders.py`（纯函数、注入 now）与 `domain/assignment.py`；
本层只负责取数据、按时间调规则、把结果落库或返回给路由。

**没有后台线程**：扫描是一次显式调用（`POST /reclaim/scan` 或 `scripts/scan_reclaim.py`），
调用方决定什么时候跑；重复调用不产生新事件（已是公海的线索不再回收）。
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from salescopilot.domain.assignment import is_in_pool
from salescopilot.domain.models import INITIAL_STAGE, AssignmentEvent, Lead
from salescopilot.domain.ports import (
    AssignmentEventRepository,
    FollowupRepository,
    LeadRepository,
)
from salescopilot.domain.reminders import (
    FIRST_RESPONSE,
    SEVERITY,
    Reminder,
    compute_reminders,
    parse_moment,
)
from salescopilot.services.leads import utc_now_iso


class ReminderService:
    """提醒用例边界：跨线索列表与单线索列表；阈值全部来自该线索自己的模板。"""

    def __init__(
        self,
        leads: LeadRepository,
        followups: FollowupRepository,
        template_loader: Callable[[str], dict],
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._leads = leads
        self._followups = followups
        self._template_loader = template_loader
        self._clock = clock if clock is not None else utc_now_iso

    def list_all(self) -> list[Reminder]:
        """全部线索的提醒：按严重度倒序、同级别按线索 id 排序。"""
        now = _as_now(self._clock())
        reminders: list[Reminder] = []
        for lead in self._leads.list():
            reminders.extend(self._for(lead, now))
        return sorted(reminders, key=lambda item: (-SEVERITY[item.level], item.lead_id))

    def for_lead(self, lead_id: str) -> list[Reminder] | None:
        """该线索的提醒；线索不存在返回 `None`（路由据此给 404），存在但没有提醒返回空列表。"""
        lead = self._leads.get(lead_id)
        if lead is None:
            return None
        return self._for(lead, _as_now(self._clock()))

    def lead_names(self) -> dict[str, str]:
        """线索 id → 名称：`GET /reminders` 每条要带 `lead_name`，前端才不用另查一次。"""
        return {lead.id: lead.name for lead in self._leads.list()}

    def _for(self, lead: Lead, now: datetime) -> list[Reminder]:
        template = self._template(lead.template)
        if template is None:
            return []
        return compute_reminders(
            leads=[lead],
            followups=self._followups.list_for(lead.id),
            template=template,
            now=now,
        )

    def _template(self, key: str) -> dict | None:
        try:
            return self._template_loader(key)
        except KeyError:
            return None


class ReclaimService:
    """首响超时自动回收：入库阶段还没首联、却已经挂在某人名下的线索，放回公海。"""

    def __init__(
        self,
        leads: LeadRepository,
        events: AssignmentEventRepository,
        template_loader: Callable[[str], dict],
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._leads = leads
        self._events = events
        self._template_loader = template_loader
        self._clock = clock if clock is not None else utc_now_iso

    def scan(self) -> list[dict]:
        """扫一遍：返回被回收的线索（`[{lead_id, from_owner, reason, at}]`），没有回收就给空列表。

        判定口径与提醒完全一致（同一个 `compute_reminders` 出红提醒），
        所以「列表上显示红」与「被回收」不会互相打架。
        """
        now = _as_now(self._clock())
        reclaimed: list[dict] = []
        for lead in self._leads.list():
            if lead.stage != INITIAL_STAGE or is_in_pool(lead):
                continue
            template = self._template(lead.template)
            if template is None:
                continue

            overdue = [
                item
                for item in compute_reminders(
                    leads=[lead], followups=[], template=template, now=now
                )
                if item.kind == FIRST_RESPONSE
            ]
            if not overdue:
                continue

            from_owner = lead.owner
            at = self._clock()
            self._leads.update_owner(lead.id, None)
            self._events.add(
                AssignmentEvent(
                    id=str(uuid.uuid4()),
                    lead_id=lead.id,
                    from_owner=from_owner,
                    to_owner=None,
                    reason="auto_reclaim",
                    at=at,
                )
            )
            reclaimed.append(
                {
                    "lead_id": lead.id,
                    "from_owner": from_owner,
                    "reason": "auto_reclaim",
                    "at": at,
                }
            )
        return reclaimed

    def _template(self, key: str) -> dict | None:
        try:
            return self._template_loader(key)
        except KeyError:
            return None


def _as_now(raw: str) -> datetime:
    """时钟字符串 → 带时区的 datetime；解析不了就退回当前时刻（不让列表接口 500）。"""
    moment = parse_moment(raw)
    return moment if moment is not None else datetime.now(UTC)
