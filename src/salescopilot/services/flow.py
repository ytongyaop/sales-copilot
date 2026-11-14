"""阶段推进用例：取线索 → 取模板 → 校验 → 改阶段 + 写阶段历史。

顺序固定：所有判断都交给 `domain/rules.py` 的 `validate_advance`，本层只负责
「拿数据、按结果落库、把结果讲回去」。本层不认识 SQLite，只认识 `domain/ports.py`。
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace

from salescopilot.domain.models import Lead, StageEvent
from salescopilot.domain.ports import LeadRepository, StageEventRepository
from salescopilot.domain.rules import ValidationIssue, validate_advance
from salescopilot.services.leads import utc_now_iso


@dataclass(frozen=True)
class AdvanceResult:
    """一次推进的结果：成功时带更新后的线索与新写的那条阶段历史。"""

    ok: bool
    lead: Lead | None
    issues: list[ValidationIssue]
    event: StageEvent | None = None


class FlowService:
    """推进用例边界：入参进、结果或问题列表出；不抛业务异常。"""

    def __init__(
        self,
        leads: LeadRepository,
        events: StageEventRepository,
        template_loader: Callable[[str], dict],
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._leads = leads
        self._events = events
        self._template_loader = template_loader
        self._clock = clock if clock is not None else utc_now_iso

    def advance(self, lead_id: str, target_stage: str) -> AdvanceResult:
        """把线索推进到目标阶段；校验不过时原样返回问题，不落库。"""
        lead = self._leads.get(lead_id)
        if lead is None:
            return AdvanceResult(False, None, [ValidationIssue("lead_id", "线索不存在")])

        try:
            template = self._template_loader(lead.template)
        except KeyError:
            return AdvanceResult(
                False, lead, [ValidationIssue("template", f"未知模板：「{lead.template}」")]
            )

        issues = validate_advance(template, lead.stage, target_stage, self._fields(lead))
        if issues:
            return AdvanceResult(False, lead, issues)

        self._leads.update_stage(lead_id, target_stage)
        event = StageEvent(
            id=str(uuid.uuid4()),
            lead_id=lead_id,
            from_stage=lead.stage,
            to_stage=target_stage,
            at=self._clock(),
        )
        self._events.add(event)
        return AdvanceResult(True, replace(lead, stage=target_stage), [], event)

    def history(self, lead_id: str) -> list[StageEvent]:
        """该线索的阶段历史，按时间正序（顺序由仓储实现保证）。"""
        return self._events.list_for(lead_id)

    @staticmethod
    def _fields(lead: Lead) -> dict[str, str]:
        """线索的字段字典：基础属性 + `fields` 里已采集的业务字段。"""
        return {
            "name": lead.name,
            "contact": lead.contact,
            "source": lead.source,
            "owner": lead.owner or "",
            "created_at": lead.created_at,
            **lead.fields,
        }
