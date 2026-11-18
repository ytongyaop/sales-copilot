"""话术草稿用例：生成 → 校验 → 落库（pending）→ 确认成跟进 / 丢弃。

这一层是「能不能落库」的唯一裁判，五道门槛按顺序执行，任一不过就不产出草稿：
1. 线索存在；
2. 至少有一条客户事实（没有事实就没有可写的内容）；
3. 检索到资料依据，且模型确实引用了其中至少一条（无出处的草稿等于编）；
4. 正文不含禁用词；
5. 正文里的日期站得住：不早于今天，且与客户已约定时间一致（`domain/draft.check_dates`）。
   这一道不过时**重试一次**（同一个问题再问一遍，与「空引用重试」同一套「最多一次」）；
   两次都不行就拒绝，并把原因写清楚。

生成器（`DraftGenerator`）只负责「写」，本层只负责「收不收」。今天与「已约定时间」由本层
从注入的时钟与仓储取，再交给生成器写进提示词——模型不知道今天是几号，就不可能不写错日子。
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace

from salescopilot.domain.draft import (
    build_citations_payload,
    build_query,
    check_banned,
    check_dates,
    validate_confirm,
)
from salescopilot.domain.errors import DraftError, KnowledgeError
from salescopilot.domain.grading import parse_iso_date
from salescopilot.domain.models import (
    DRAFT_CONFIRMED,
    DRAFT_DISCARDED,
    DRAFT_PENDING,
    Draft,
    Fact,
    Followup,
    Lead,
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

# 一次生成最多问模型几次：第一次照样问；只有「日期不合格」会再问第二次（与空引用重试
# 同一套「最多一次」的口径），其余门槛一次不过就拒绝。
MAX_GENERATE_ATTEMPTS = 2


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
        """按五道门槛生成一条草稿；不通过就返回问题，绝不落库。

        「日期不合格」会重试一次：模型第一次写了过去的日期（或与约定不符），说明它没吃透
        今天几号，再问一遍通常就对了——但只重试一次，两次都编就拒绝，不让人替它挑日子。
        重试时用的仍是同一份事实与资料（不换题目），所以成本可预期：最坏两次调用。
        """
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

        today_text = self._today()
        today = parse_iso_date(today_text)
        agreed = self._agreed_at(lead, facts)

        for attempt in range(1, MAX_GENERATE_ATTEMPTS + 1):
            try:
                result = self._generator.draft(
                    lead=lead, facts=facts, hits=hits, today=today_text, agreed_at=agreed
                )
            except DraftError as exc:
                return _fail([ValidationIssue("draft", str(exc))])

            text = result.text.strip() if isinstance(result.text, str) else ""
            if not text:
                return _fail([ValidationIssue("draft", "模型没有返回话术内容")])

            used = [index for index in result.used_hit_indexes if 0 <= index < len(hits)]
            if not used:
                return _fail(
                    [
                        ValidationIssue(
                            "draft",
                            "模型没有引用任何资料，已拒绝生成（可补充资料，或在跟进里写明依据后再生成）",
                        )
                    ]
                )
            citations = build_citations_payload([hits[index] for index in used])
            if not citations:
                return _fail([ValidationIssue("draft", "模型引用的资料没有正文")])

            banned = check_banned(text)
            if banned:
                return _fail(banned)

            # 时钟不是 YYYY-MM-DD 时判不了日子：这一道跳过（其余门槛照旧），不拿系统时钟硬判。
            date_issues = (
                check_dates(text, today=today, appointment_at=agreed) if today is not None else []
            )
            if not date_issues:
                break
            if attempt == MAX_GENERATE_ATTEMPTS:
                return _fail(date_issues)

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

    def _today(self) -> str:
        """今天（`YYYY-MM-DD`）：只切注入时钟的日期部分，本层不自己取时钟。"""
        return self._clock()[:10]

    def _agreed_at(self, lead: Lead, facts: list[Fact]) -> str:
        """客户已约定时间（`YYYY-MM-DD`）：取几个来源里**最晚**的那个合法日期，没有就空串。

        来源依次是：事实 `appointment_at`、线索字段 `appointment_at`、最近一条跟进的
        `next_followup_at`。为什么不只认事实：真实数据里事实常常记着**上一次**见面的日子
        （已过去），而「下周三」这种新约定记在跟进的 `next_followup_at` 上；只认前者会让
        模型把话术引回一个已经过去的日子——那正是这次要修的错。

        全都是过去的日子时同样返回它（由 `check_dates` 的「早于今天」那一条拦），
        本层不替业务判断「过期约定算不算约定」。
        """
        candidates: list[str] = []
        for fact in facts:
            if fact.field == "appointment_at" and isinstance(fact.value, str):
                candidates.append(fact.value)
        field_value = lead.fields.get("appointment_at") if isinstance(lead.fields, dict) else None
        if isinstance(field_value, str):
            candidates.append(field_value)
        if self._followups is not None:
            for followup in self._followups.list_for(lead.id):
                if isinstance(followup.next_followup_at, str):
                    candidates.append(followup.next_followup_at)

        dates = [parse_iso_date(value.strip()) for value in candidates]
        found = [value for value in dates if value is not None]
        if not found:
            return ""
        return max(found).isoformat()

    def confirm(
        self,
        lead_id: str,
        draft_id: str,
        *,
        objection: str,
        next_followup_at: str,
        value_provided: str = "",
    ) -> ConfirmOutcome:
        """把草稿确认成一条跟进留痕：结论就是草稿正文，异议与时间由人补。

        落库成功后把同一段正文写进线索的 `conclusion` 字段：`close` 块要靠它才推得动，
        而人要写的就是「这次谈成了什么」——草稿正文正是那句话，不必再手抄一遍。

        `value_provided` 与 `FollowupService.record` 同口径：**允许为空**，空串就是
        「这次跟进没给客户新东西」，周复盘据此统计空转次数，所以这里不校验、不拒绝。
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
            value_provided=value_provided.strip() if isinstance(value_provided, str) else "",
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
