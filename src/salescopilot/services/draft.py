"""话术草稿用例：生成 → 校验 → 落库（pending）→ 确认成跟进 / 丢弃。

这一层是「能不能落库」的唯一裁判，四条硬门槛按顺序执行，任一不过就不产出草稿：
1. 线索存在；
2. 至少有一条客户事实（没有事实就没有可写的内容）；
3. 检索到资料依据，且模型确实引用了其中至少一条（无出处的草稿等于编）；
4. 正文不含禁用词。

生成器（`DraftGenerator`）只负责「写」，本层只负责「收不收」。
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace

from salescopilot.domain.draft import (
    build_citations_payload,
    build_query,
    check_banned,
    validate_confirm,
)
from salescopilot.domain.errors import DraftError, KnowledgeError
from salescopilot.domain.models import (
    DRAFT_CONFIRMED,
    DRAFT_DISCARDED,
    DRAFT_PENDING,
    Draft,
    Followup,
)
from salescopilot.domain.ports import (
    DraftGenerator,
    DraftRepository,
    FactRepository,
    FollowupRepository,
    LeadRepository,
)
from salescopilot.domain.rules import ValidationIssue, merge_fields, validate_lead_fields
from salescopilot.services.leads import utc_now_iso

# 每次取几段资料：够模型引用、又不至于把提示词灌满。
SEARCH_K = 4

# 检索语句最多拼多少条事实：事实再多，查询词也该保持短。
QUERY_MAX_FACTS = 8


@dataclass(frozen=True)
class DraftOutcome:
    """一次生成的结果：成功时有草稿 id / 正文 / 引用 / 模型名；失败时 issues 非空。"""

    ok: bool
    draft_id: str | None
    text: str
    citations: list[dict]
    model: str


@dataclass(frozen=True)
class ConfirmOutcome:
    """一次确认的结果：落库的跟进、确认后该线索的字段、以及问题列表。

    `fields` 是写完「结论」之后的完整字段视图（不含线索自身的基础属性）；
    失败时为 `{}`，调用方不用去查库就知道这次有没有写成。
    """

    item: Followup | None
    fields: dict[str, str]
    issues: list[ValidationIssue]


class DraftService:
    """草稿用例边界：入参进、（结果, 问题列表）出；不抛业务异常。"""

    def __init__(
        self,
        leads: LeadRepository,
        facts: FactRepository,
        knowledge,
        generator: DraftGenerator,
        drafts: DraftRepository,
        clock: Callable[[], str] | None = None,
        *,
        followups: FollowupRepository | None = None,
    ) -> None:
        self._leads = leads
        self._facts = facts
        self._knowledge = knowledge
        self._generator = generator
        self._drafts = drafts
        self._followups = followups
        self._clock = clock if clock is not None else utc_now_iso

    # ------------------------------------------------------------------ 生成

    def generate(self, lead_id: str) -> tuple[DraftOutcome, list[ValidationIssue]]:
        """按四道门槛生成一条草稿；不通过就返回问题，绝不落库。"""
        lead = self._leads.get(lead_id)
        if lead is None:
            return _fail([ValidationIssue("lead_id", "线索不存在")])

        facts = self._facts.list_for(lead_id)
        if not facts:
            return _fail([ValidationIssue("facts", "还没有客户事实，先提炼或补录")])

        try:
            hits = self._knowledge.search(
                build_query(lead, facts, max_facts=QUERY_MAX_FACTS), k=SEARCH_K
            )
        except KnowledgeError as exc:
            return _fail([ValidationIssue("knowledge", str(exc))])
        if not hits:
            return _fail([ValidationIssue("draft", "没有检索到资料依据，无法生成话术")])

        try:
            result = self._generator.draft(lead=lead, facts=facts, hits=hits)
        except DraftError as exc:
            return _fail([ValidationIssue("draft", str(exc))])

        text = result.text.strip() if isinstance(result.text, str) else ""
        if not text:
            return _fail([ValidationIssue("draft", "模型没有返回话术内容")])

        used = [index for index in result.used_hit_indexes if 0 <= index < len(hits)]
        if not used:
            return _fail([ValidationIssue("draft", "模型未引用任何资料")])
        citations = build_citations_payload([hits[index] for index in used])
        if not citations:
            return _fail([ValidationIssue("draft", "模型引用的资料没有正文")])

        banned = check_banned(text)
        if banned:
            return _fail(banned)

        draft = Draft(
            id=str(uuid.uuid4()),
            lead_id=lead_id,
            text=text,
            citations=citations,
            status=DRAFT_PENDING,
            model=result.model,
            prompt_version=result.prompt_version,
            token=result.token,
            seconds=result.seconds,
            created_at=self._clock(),
        )
        self._drafts.add(draft)
        return DraftOutcome(True, draft.id, text, citations, draft.model), []

    # ------------------------------------------------------------------ 确认

    def confirm(
        self, lead_id: str, draft_id: str, *, objection: str, next_followup_at: str
    ) -> ConfirmOutcome:
        """把草稿确认成一条跟进留痕：结论就是草稿正文，异议与时间由人补。

        落库成功后把同一段正文写进线索的 `conclusion` 字段：`close` 块要靠它才推得动，
        而人要写的就是「这次谈成了什么」——草稿正文正是那句话，不必再手抄一遍。
        """
        draft, issues = self._resolve_pending(lead_id, draft_id)
        if draft is None:
            return ConfirmOutcome(None, {}, issues)

        issues = validate_confirm(
            objection=objection,
            next_followup_at=next_followup_at,
            conclusion=draft.text,
        )
        if issues:
            return ConfirmOutcome(None, {}, issues)

        if self._followups is None:
            return ConfirmOutcome(
                None, {}, [ValidationIssue("draft", "草稿服务未接入跟进仓储，无法确认")]
            )

        item = Followup(
            id=str(uuid.uuid4()),
            lead_id=lead_id,
            conclusion=draft.text,
            objection=objection.strip(),
            next_followup_at=next_followup_at.strip(),
            at=self._clock(),
        )
        self._followups.add(item)
        self._drafts.update_status(draft.id, DRAFT_CONFIRMED)
        return ConfirmOutcome(item, self._write_conclusion(lead_id, draft.text), [])

    def _write_conclusion(self, lead_id: str, text: str) -> dict[str, str]:
        """把草稿正文写进线索的 `conclusion` 字段；返回更新后的字段视图。

        正文过不了字段校验（比如超过 200 字）就不写：宁可让推进时提示「缺结论」，
        也不往字段里塞一条违反 `validate_lead_fields` 的值。
        """
        lead = self._leads.get(lead_id)
        if lead is None:
            return {}
        if validate_lead_fields({"conclusion": text}):
            return dict(lead.fields)

        merged = merge_fields(lead.fields, {"conclusion": text})
        self._leads.update_fields(lead_id, merged)
        return merged

    # ------------------------------------------------------------------ 丢弃

    def discard(self, lead_id: str, draft_id: str) -> tuple[Draft | None, list[ValidationIssue]]:
        """丢弃一条草稿：只改状态，不删记录（留痕才能复盘「为什么没用这条」）。"""
        draft, issues = self._resolve_pending(lead_id, draft_id)
        if draft is None:
            return None, issues

        self._drafts.update_status(draft.id, DRAFT_DISCARDED)
        return replace(draft, status=DRAFT_DISCARDED), []

    # ------------------------------------------------------------------ 内部

    def _resolve_pending(
        self, lead_id: str, draft_id: str
    ) -> tuple[Draft | None, list[ValidationIssue]]:
        """公共前置：草稿在、属于这条线索、状态仍是 pending。"""
        draft = self._drafts.get(draft_id)
        if draft is None or draft.lead_id != lead_id:
            return None, [ValidationIssue("draft", "草稿不存在")]
        if draft.status != DRAFT_PENDING:
            return None, [ValidationIssue("draft", "草稿已确认或已丢弃，不能再次操作")]
        return draft, []

    def list_for(self, lead_id: str) -> list[Draft]:
        """该线索的草稿，按时间正序（顺序由仓储实现保证）。"""
        return self._drafts.list_for(lead_id)


def _fail(issues: list[ValidationIssue]) -> tuple[DraftOutcome, list[ValidationIssue]]:
    return DraftOutcome(False, None, "", [], ""), issues
