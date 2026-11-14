"""跟进留痕用例：三要素校验 → 落库 → 回查。

校验只调 `domain/rules.py` 的 `validate_followup`；本层不认识 SQLite，
只认识 `domain/ports.py` 的协议。
"""

import uuid
from collections.abc import Callable

from salescopilot.domain.models import Followup
from salescopilot.domain.ports import FollowupRepository, LeadRepository
from salescopilot.domain.rules import ValidationIssue, validate_followup
from salescopilot.services.leads import utc_now_iso


class FollowupService:
    """跟进用例边界：入参进、（是否成功, 问题列表, 留痕）出。"""

    def __init__(
        self,
        leads: LeadRepository,
        followups: FollowupRepository,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._leads = leads
        self._followups = followups
        self._clock = clock if clock is not None else utc_now_iso

    def record(
        self,
        lead_id: str,
        *,
        conclusion: str,
        objection: str,
        next_followup_at: str,
    ) -> tuple[bool, list[ValidationIssue], Followup | None]:
        """记录一条跟进；三要素缺失或非法时返回问题，不落库。"""
        lead = self._leads.get(lead_id)
        if lead is None:
            return False, [ValidationIssue("lead_id", "线索不存在")], None

        issues = validate_followup(
            conclusion=conclusion,
            objection=objection,
            next_followup_at=next_followup_at,
        )
        if issues:
            return False, issues, None

        item = Followup(
            id=str(uuid.uuid4()),
            lead_id=lead_id,
            conclusion=conclusion.strip(),
            objection=objection.strip(),
            next_followup_at=next_followup_at.strip(),
            at=self._clock(),
        )
        self._followups.add(item)
        return True, [], item

    def list_for(self, lead_id: str) -> list[Followup]:
        """该线索的跟进记录，按时间正序（顺序由仓储实现保证）。"""
        return self._followups.list_for(lead_id)
