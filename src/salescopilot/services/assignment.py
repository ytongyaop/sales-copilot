"""归属用例：认领 / 释放 / 公海列表。

规矩固定：能不能认领、能不能释放只问 `domain/assignment.py`；本层只负责
「取线索、按判定落库、把每一次变更记成一条归属事件」，并把结果讲回给路由。
本层不认识 SQLite，只认识 `domain/ports.py` 的协议。

注意：认领/释放失败时仍回传线索本身（`None` 只表示「线索不存在」），
路由据此区分 404（没有这条线索）与 409（状态冲突：已被别人占着 / 本来就在公海）。
"""

import uuid
from collections.abc import Callable
from dataclasses import replace

from salescopilot.domain.assignment import can_claim, can_release, is_in_pool
from salescopilot.domain.models import ALLOWED_ASSIGNMENT_REASONS, AssignmentEvent, Lead
from salescopilot.domain.ports import AssignmentEventRepository, LeadRepository
from salescopilot.domain.rules import ValidationIssue
from salescopilot.services.leads import utc_now_iso


class AssignmentService:
    """归属用例边界：入参进、（线索或 None, 问题列表）出；不抛业务异常。"""

    def __init__(
        self,
        leads: LeadRepository,
        events: AssignmentEventRepository,
        clock: Callable[[], str] | None = None,
        template_loader: Callable[[str], dict] | None = None,
    ) -> None:
        self._leads = leads
        self._events = events
        self._clock = clock if clock is not None else utc_now_iso
        # 预留：目前认领/释放不需要模板数据（阈值不参与归属），保留注入以便将来按模板加规则。
        self._template_loader = template_loader

    def claim(self, lead_id: str, owner: str) -> tuple[Lead | None, list[ValidationIssue]]:
        """把一条公海线索认领到 `owner` 名下；成功时写一条 `claim` 事件。"""
        lead = self._leads.get(lead_id)
        if lead is None:
            return None, [ValidationIssue("lead_id", "线索不存在")]

        text = owner.strip() if isinstance(owner, str) else ""
        if not text:
            return lead, [ValidationIssue("owner", "归属人不能为空")]

        allowed, reason = can_claim(lead)
        if not allowed:
            return lead, [ValidationIssue("owner", reason)]

        self._leads.update_owner(lead_id, text)
        self._events.add(
            AssignmentEvent(
                id=str(uuid.uuid4()),
                lead_id=lead_id,
                from_owner=lead.owner,
                to_owner=text,
                reason="claim",
                at=self._clock(),
            )
        )
        return replace(lead, owner=text), []

    def release(
        self, lead_id: str, *, reason: str = "release"
    ) -> tuple[Lead | None, list[ValidationIssue]]:
        """把线索放回公海；成功时写一条归属事件（默认 `release`，人工调整传 `manual`）。"""
        lead = self._leads.get(lead_id)
        if lead is None:
            return None, [ValidationIssue("lead_id", "线索不存在")]

        if reason not in ALLOWED_ASSIGNMENT_REASONS:
            allowed = "、".join(ALLOWED_ASSIGNMENT_REASONS)
            return lead, [
                ValidationIssue("reason", f"归属原因必须是以下之一：{allowed}（实际是 {reason}）")
            ]

        allowed, message = can_release(lead)
        if not allowed:
            return lead, [ValidationIssue("owner", message)]

        self._leads.update_owner(lead_id, None)
        self._events.add(
            AssignmentEvent(
                id=str(uuid.uuid4()),
                lead_id=lead_id,
                from_owner=lead.owner,
                to_owner=None,
                reason=reason,
                at=self._clock(),
            )
        )
        return replace(lead, owner=None), []

    def pool(self) -> list[Lead]:
        """公海：没有归属人的线索，按创建时间倒序（顺序由仓储实现保证）。"""
        return [lead for lead in self._leads.list() if is_in_pool(lead)]

    def events_for(self, lead_id: str) -> list[AssignmentEvent]:
        """该线索的归属事件，按时间正序（回查用）。"""
        return self._events.list_for(lead_id)
