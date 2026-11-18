"""一次投料 → 分拣单：校验原文 → 归属漏斗 → 分拣模型 → 出处对齐 → 落单。

分工固定，谁都不越界：
- 归属用 `domain/matching.py` 的三级漏斗；L0 精确键（联系方式）命中才 `auto`，
  L1 只走字符串，L2 向量本增量**只留参数不实装**（`vector_score=None`）；
  灰区与「没有候选」一律进待认领池，不猜、不落客户；
- 要素 / 分级 / 出库三块由 `domain/sorting.py` 校验与重算：`source_span` 对不上原文
  就丢弃那一条并记诊断，分级不合法就用规则兜底，出库建议按「先补缺项」重算；
- 模型返回的原始 JSON 文本原样落 `suggestion_json`（审计要「当时到底吐了什么」），
  丢弃诊断落 `dropped_json`；
- 本层只编排，不写判断规则；路由只调本层。

人工处置（T004）三条纪律：
- 处置只走仓储的两个窄口（`decide` 写状态与留痕、`attach_lead` 写归属），
  `suggestion_json` / `confidence` / `scorer` / `dropped_json` 一个字都不改；
- 「同意」落事实前**再校验一次出处**（回到投料当时的原文对齐），
  已经被人工锁定的字段一律跳过并说明原因；
- 归属未定的单只进待认领池：认领只挂客户、不自动同意，灰区复核只出建议、不落库。
"""

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from salescopilot.domain.errors import SortingError
from salescopilot.domain.facts import decide_merge
from salescopilot.domain.matching import (
    Candidate,
    MatchDecision,
    MatchThresholds,
    decide_band,
    fuzzy_score,
    rank_candidates,
    suffix_only_difference,
)
from salescopilot.domain.models import (
    CARD_SECTIONS,
    DEFAULT_TEMPLATE,
    INITIAL_STAGE,
    SORTING_STATUSES,
    Lead,
    SortingBatch,
)
from salescopilot.domain.ports import (
    FactRepository,
    LeadRepository,
    RawItemRepository,
    SortingBatchRepository,
    SortingJudge,
)
from salescopilot.domain.rules import CONTACT_TOO_SHORT, MIN_CONTACT_LEN, ValidationIssue
from salescopilot.domain.sorting import (
    ElementHit,
    SortingSuggestion,
    build_next_action,
    contact_from_elements,
    contact_tokens,
    grade_from_elements,
    normalized_org,
    org_name_candidates,
    split_diagnostic,
    validate_decision_reason,
    validate_elements,
)
from salescopilot.services.ingest import register_raw_item
from salescopilot.services.leads import utc_now_iso

# 灰区一次最多把这么多候选交给模型参考：候选越多，模型越容易瞎猜。
MAX_REVIEW_CANDIDATES = 3

# 分级只认这三个值；不合法的分级一律走规则兜底。
GRADING_VALUES = ("A", "B", "C")

# 列表接口的条数上限：够一屏，也挡得住「一次拉全表」。
MAX_BATCH_LIMIT = 200

# 列表里的原文摘要长度：够认出是哪一通，又不用把全文塞进列表。
CONTENT_PREVIEW_LEN = 60

# 归属打分者的固定取值：投料时直接指定线索记 exact（等同精确键），其余按漏斗结论。
DESIGNATED_SCORER = "exact"

# 人工处置动作：同意并落事实 / 改归属后落事实 / 拒绝（不落事实）。
DECISION_ACCEPT = "accept"
DECISION_FIX = "fix"
DECISION_REJECT = "reject"
DECISION_ACTIONS: tuple[str, ...] = (DECISION_ACCEPT, DECISION_FIX, DECISION_REJECT)

# 已经处置完的两个状态：到这里的单不再允许第二次处置。
DECIDED_STATUSES: tuple[str, ...] = ("accepted", "rejected")

# 分拣只证明「这是一条独立线索」，具体来路等人补，所以新客户先记 other。
CLAIM_SOURCE = "other"

# 认领时新建的客户名称至少 2 个字（与跟进结论的 2 字口径一致）。
MIN_LEAD_NAME_LEN = 2

# 分级兜底函数：入参是已过出处校验的要素，返回 {"value", "reason", "basis"}。
Grader = Callable[[list[ElementHit]], dict]


@dataclass(frozen=True)
class SortingOutcome:
    """一次投料的结果：落下的分拣单、拒收原因、已校验的建议、被丢弃的要素。

    - `batch` 为 None 表示没生成分拣单（原文校验不过、线索不存在、模型失败，
      或没有任何可用要素可供出库建议），此时 `issues` 说明原因；
    - `suggestion` 是**已校验**的三块建议（要素已过滤、分级已兜底、出库建议已重算），
      与 `batch.suggestion_json`（模型原文）刻意不同：一个给人看，一个给审计看；
    - `dropped` 每项形如 `{"index", "reason"}`，`index` 是模型输出里的下标；
    - `lead_candidates` 是这次落库的归属候选（`batch.candidates_json` 的同一份内容，
      已是 `{"lead_id", "name", "score", "scorer"}` 字典）；路由直接回给前端，不用再解析 JSON。
    """

    batch: SortingBatch | None
    issues: list[ValidationIssue]
    suggestion: SortingSuggestion | None = None
    dropped: list[dict] = field(default_factory=list)
    lead_candidates: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class ConfirmOutcome:
    """一次人工处置的结果：改完的分拣单、问题、落了几条事实、跳过了什么。

    - `batch` 为 None 表示分拣单不存在；`issues` 非空表示这次处置**没执行**
      （状态不对 / 缺理由 / 客户不存在），此时 `applied` 恒为 0；
    - `applied` 是这次真正写进事实层的条数（新建或覆盖同一条当前事实）；
    - `skipped` 每项形如 `{"field", "reason"}`：既含出处复核不过的要素，
      也含被人工锁定挡下的字段。
    """

    batch: SortingBatch | None
    issues: list[ValidationIssue]
    applied: int = 0
    skipped: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class ReviewOutcome:
    """灰区复核的结论：建议归给谁、为什么。

    `lead_id` 为 None 表示模型挑不出来（或挑了一个不在候选里的 id）。
    结果**只进接口响应、不落库**：不写 `lead_id`、不改 `suggestion_json`——
    复核是建议，归属仍要人认领后才写。
    """

    lead_id: str | None
    reason: str
    issues: list[ValidationIssue] = field(default_factory=list)


class SortingService:
    """投料与分拣的用例边界：入参进、（分拣单或问题）出；不抛业务异常。"""

    def __init__(
        self,
        leads: LeadRepository,
        raws: RawItemRepository,
        batches: SortingBatchRepository,
        extractor: SortingJudge,
        grader: Grader | None = None,
        clock: Callable[[], str] | None = None,
        thresholds: MatchThresholds | None = None,
        alias_table: dict[str, str] | None = None,
        vector_score: float | None = None,
        facts: FactRepository | None = None,
    ) -> None:
        """`extractor` 是分拣模型（要素 / 分级 / 出库一次出）；`grader` 是分级的规则兜底。

        `vector_score`：L2 向量的分数。本增量不装向量模型，恒为 None；参数留在签名里，
        将来接上向量只换装配，不用改用例。
        `facts`：人工处置要把要素落成事实，装配点必须传；没传时确认类用例会返回
        一条「未装配事实仓储」的问题，而不是悄悄什么都不写。
        """
        self._leads = leads
        self._raws = raws
        self._batches = batches
        self._extractor = extractor
        self._grader: Grader = grader if grader is not None else grade_from_elements
        self._clock = clock if clock is not None else utc_now_iso
        self._thresholds = thresholds if thresholds is not None else MatchThresholds()
        self._aliases = dict(alias_table or {})
        self._vector = vector_score
        self._facts = facts

    # ------------------------------------------------------------------ 投料

    def sort(self, *, content: str, source: str, lead_id: str | None = None) -> SortingOutcome:
        """一段原文进，一张分拣单出；归属未定时 `lead_id` 为空、状态进待认领池。"""
        designated = None
        if lead_id is not None:
            designated = self._leads.get(lead_id)
            if designated is None:
                return SortingOutcome(None, [ValidationIssue("lead_id", "线索不存在")])

        # 1) 原文先落库（幂等）：归属未定时用空 lead_id，原文也要留下来等认领。
        raw, _duplicated, issues = register_raw_item(
            self._raws,
            lead_id=designated.id if designated is not None else "",
            content=content,
            source=source,
            clock=self._clock,
        )
        if raw is None:
            return SortingOutcome(None, issues)

        # 2) 归属：指定了就是它；没指定就跑三级漏斗。
        if designated is not None:
            decision = MatchDecision(
                band="auto",
                scorer=DESIGNATED_SCORER,
                score=100.0,
                reason="投料时已指定线索",
            )
            candidates: list[Candidate] = []
            resolved_id: str | None = designated.id
        else:
            decision, candidates, resolved_id = self._attribute(raw.content)
        candidate_payload = _candidate_dicts(candidates)

        # 3) 分拣模型：候选只在灰区交给它参考（精确键命中 / 判为新客户都不问归属）。
        prompt_candidates = candidates if decision.band == "review" else []
        try:
            result = self._extractor.judge(content=raw.content, candidates=prompt_candidates)
        except SortingError as exc:
            return SortingOutcome(None, [ValidationIssue("extractor", str(exc))])

        # 4) 出处对齐：对不上原文的要素丢弃并留诊断，绝不落库。
        elements, diagnostics = validate_elements(raw.content, result.payload.get("elements"))
        dropped = _dropped(diagnostics)

        # 5) 分级：模型的合规就用它，不合规按要素规则兜底（不阻断分拣）。
        grading = _clean_grading(result.payload.get("grading"))
        if grading is None:
            fallback = self._grader(elements)
            grading = {
                "value": fallback.get("value", ""),
                "reason": f"模型分级不可用，按要素规则兜底：{fallback.get('reason', '')}",
                "basis": list(fallback.get("basis", [])),
            }

        # 6) 出库建议：按缺项规则重算；没有任何可用依据就拒绝生成（不落一张空单）。
        missing = [
            section
            for section in CARD_SECTIONS
            if not any(element.card_section == section for element in elements)
        ]
        next_action = build_next_action(elements, missing)
        if not next_action.get("action"):
            return SortingOutcome(
                None,
                [ValidationIssue("next_action", "没有任何可用要素，已拒绝生成分拣单")],
                dropped=dropped,
            )

        # 7) 落单：归属已定 → 待人确认；归属未定 → 待认领。
        batch = SortingBatch(
            id=str(uuid.uuid4()),
            lead_id=resolved_id,
            raw_id=raw.id,
            status="pending_review" if resolved_id else "pending_claim",
            suggestion_json=result.raw_text,
            confidence=float(decision.score),
            scorer=decision.scorer,
            created_at=self._clock(),
            dropped_json=json.dumps(dropped, ensure_ascii=False),
            candidates_json=json.dumps(candidate_payload, ensure_ascii=False),
        )
        self._batches.add(batch)

        suggestion = SortingSuggestion(
            lead_candidates=list(prompt_candidates),
            elements=elements,
            grading=grading,
            next_action=next_action,
        )
        return SortingOutcome(batch, [], suggestion, dropped, candidate_payload)

    # -------------------------------------------------------------- 归属漏斗

    def _attribute(self, content: str) -> tuple[MatchDecision, list[Candidate], str | None]:
        """按三级漏斗判归属：L0 精确键 → L1 字符串 → L2 向量（本增量恒为 None）。

        返回（判定, 候选列表, 归属 id）。只有 `auto` 才给归属 id；灰区与新客户都返回 None，
        由状态 `pending_claim` 表达「等认领」。
        """
        leads = self._leads.list()

        # L0：联系方式精确键（完全相等才算命中）。
        tokens = contact_tokens(content)
        if tokens:
            for lead in leads:
                if tokens & contact_tokens(lead.contact):
                    candidate = Candidate(lead.id, lead.name, 100.0, "exact")
                    decision = decide_band(
                        exact_hit=True,
                        fuzzy=100.0,
                        vector=None,
                        corroborated=False,
                        thresholds=self._thresholds,
                    )
                    return decision, [candidate], lead.id

        # L1：原文里的候选全称 vs 候选客户名字，取每个客户自己的最高分。
        names = org_name_candidates(content)
        scored: list[tuple[Candidate, str]] = []
        for lead in leads:
            core = normalized_org(lead.name, self._aliases)
            best: tuple[float, str] | None = None
            for name in names:
                score = fuzzy_score(normalized_org(name, self._aliases), core)
                if best is None or score > best[0]:
                    best = (score, name)
            if best is not None and best[0] > 0:
                scored.append((Candidate(lead.id, lead.name, best[0], "fuzzy"), best[1]))

        ranked = rank_candidates([item[0] for item in scored], limit=MAX_REVIEW_CANDIDATES)
        by_id = {candidate.lead_id: raw_name for candidate, raw_name in scored}
        top = ranked[0] if ranked else None

        suffix_only = (
            suffix_only_difference(by_id[top.lead_id], top.name) if top is not None else False
        )
        decision = decide_band(
            exact_hit=False,
            fuzzy=top.score if top is not None else 0.0,
            vector=self._vector,
            corroborated=False,
            thresholds=self._thresholds,
            suffix_only=suffix_only,
        )
        resolved = top.lead_id if (top is not None and decision.band == "auto") else None
        return decision, list(ranked), resolved

    # -------------------------------------------------------------- 人工处置

    def confirm(
        self,
        batch_id: str,
        *,
        action: str,
        lead_id: str | None = None,
        reason: str | None = None,
    ) -> ConfirmOutcome:
        """处置一张分拣单：`accept` 同意 / `fix` 改归属后同意 / `reject` 拒绝。

        三条分支的边界：
        - `accept`：只处理 `pending_review` 且已有归属客户的单，把要素落成该客户的事实；
        - `fix`：`lead_id` 必须是已有客户，要素落到**新客户**身上，并把分拣单改挂过去；
        - `reject`：`reason` 必填（≥2 字），不落事实；已处置过的单不能再处置。

        无论哪条分支，处置都只写 `status` / `decided_at` / `decided_action`（改归属再多写
        `lead_id`）：`suggestion_json` 等四列一个字都不改，审计能回看「模型当时说了什么」。
        """
        batch = self._batches.get(batch_id)
        if batch is None:
            return ConfirmOutcome(None, [ValidationIssue("batch_id", "分拣单不存在")])

        normalized = action.strip().lower() if isinstance(action, str) else ""
        if normalized not in DECISION_ACTIONS:
            allowed = " / ".join(DECISION_ACTIONS)
            return ConfirmOutcome(
                batch,
                [
                    ValidationIssue(
                        "action", f"action 必须是 {allowed} 之一（实际是 {normalized!r}）"
                    )
                ],
            )

        if normalized == DECISION_REJECT:
            return self._reject(batch, reason)

        # accept / fix：都要求「待人确认」，且要有一条明确的目标客户。
        if batch.status != "pending_review":
            return ConfirmOutcome(
                batch,
                [
                    ValidationIssue(
                        "status",
                        f"只有待人确认（pending_review）的分拣单可以处置（当前是 {batch.status}）",
                    )
                ],
            )

        target = self._target_lead(batch, normalized, lead_id)
        if isinstance(target, list):
            return ConfirmOutcome(batch, target)

        lead = self._leads.get(target)
        if lead is None:
            return ConfirmOutcome(batch, [ValidationIssue("lead_id", "线索不存在")])

        applied, skipped, write_issues = self._apply_elements(batch, lead.id)
        if write_issues:
            return ConfirmOutcome(batch, write_issues)

        if normalized == DECISION_FIX:
            # 先改挂客户、再记处置：事实已经落到新客户身上，两件事不能一半一半。
            self._batches.attach_lead(batch_id, lead_id=lead.id)
            decision = "fixed_and_accepted"
        else:
            decision = "accepted"
        self._batches.decide(batch_id, action=decision, at=self._clock())
        return ConfirmOutcome(self._batches.get(batch_id), [], applied, skipped)

    def claim(
        self,
        batch_id: str,
        *,
        lead_id: str | None = None,
        create_name: str | None = None,
        owner: str | None = None,
    ) -> tuple[SortingBatch | None, list[ValidationIssue]]:
        """从待认领池里认领一张单：挂已有客户，或按名字新建一个客户。

        成功只做两件事：`lead_id` 挂上、状态改成 `pending_review`（**不自动同意**，
        要素仍要人确认）。`batch` 为 None 表示这次没认领成功（原因在 `issues`）；
        成功时 `issues` 也可能非空——那是提醒，例如要素里没有联系方式、
        客户已建但联系方式留空需要补录。
        """
        batch = self._batches.get(batch_id)
        if batch is None:
            return None, [ValidationIssue("batch_id", "分拣单不存在")]
        if batch.status != "pending_claim":
            return None, [
                ValidationIssue(
                    "status",
                    f"只有待认领池（pending_claim）里的分拣单可以认领（当前是 {batch.status}）",
                )
            ]

        target = lead_id.strip() if isinstance(lead_id, str) else ""
        name = create_name.strip() if isinstance(create_name, str) else ""
        if bool(target) == bool(name):
            return None, [
                ValidationIssue("lead_id", "lead_id 与 create_name 必须给一个（且只给一个）")
            ]

        warnings: list[ValidationIssue] = []
        if target:
            lead = self._leads.get(target)
            if lead is None:
                return None, [ValidationIssue("lead_id", "线索不存在")]
            resolved_id = lead.id
        else:
            if len(name) < MIN_LEAD_NAME_LEN:
                return None, [
                    ValidationIssue("create_name", f"客户名称至少需要 {MIN_LEAD_NAME_LEN} 个字符")
                ]
            contact, warnings = self._contact_for_claim(batch)
            created = Lead(
                id=str(uuid.uuid4()),
                name=name,
                contact=contact,
                source=CLAIM_SOURCE,
                stage=INITIAL_STAGE,
                owner=owner.strip() if isinstance(owner, str) and owner.strip() else None,
                created_at=self._clock(),
                template=DEFAULT_TEMPLATE,
            )
            self._leads.add(created)
            resolved_id = created.id

        self._batches.attach_lead(batch_id, lead_id=resolved_id, status="pending_review")
        return self._batches.get(batch_id), warnings

    def review_candidates(self, batch_id: str) -> ReviewOutcome:
        """灰区复核：待认领且候选 1–3 家时，请模型从候选里挑一个并给理由。

        候选由归属漏斗**重新算一遍**（原文没变，结论就一样），不依赖落库的建议；
        复核结果只进响应：不写 `lead_id`、不改 `suggestion_json`，等人认领。
        没有候选就明确让人「直接新建」，不拿模型瞎猜一个客户。
        """
        batch = self._batches.get(batch_id)
        if batch is None:
            return ReviewOutcome(None, "", [ValidationIssue("batch_id", "分拣单不存在")])
        if batch.status != "pending_claim":
            return ReviewOutcome(
                None,
                "",
                [
                    ValidationIssue(
                        "status",
                        f"只有待认领池（pending_claim）里的分拣单需要复核（当前是 {batch.status}）",
                    )
                ],
            )

        raw = self._raws.get(batch.raw_id)
        if raw is None:
            return ReviewOutcome(None, "", [ValidationIssue("raw_id", "原文不存在，无法复核归属")])

        _decision, candidates, _resolved = self._attribute(raw.content)
        if not candidates:
            return ReviewOutcome(
                None, "", [ValidationIssue("candidates", "没有候选，请直接新建客户")]
            )

        shortlist = list(candidates[:MAX_REVIEW_CANDIDATES])
        try:
            suggested, reason = self._extractor.review(content=raw.content, candidates=shortlist)
        except SortingError as exc:
            return ReviewOutcome(None, "", [ValidationIssue("review", str(exc))])

        # 模型挑了一个不在候选里的 id：当它没挑出来，别把脏 id 当建议发出去。
        if suggested is not None and suggested not in {item.lead_id for item in shortlist}:
            return ReviewOutcome(None, f"复核给的建议不在候选里（{suggested}），请人工判断")
        return ReviewOutcome(suggested, reason or "")

    # -------------------------------------------------------- 处置的公共动作

    def _reject(self, batch: SortingBatch, reason: str | None) -> ConfirmOutcome:
        """拒绝一张单：理由必填、不落事实；已经处置过的单直接挡下。"""
        issues = validate_decision_reason(DECISION_REJECT, reason)
        if batch.status in DECIDED_STATUSES:
            issues.append(ValidationIssue("status", "这张分拣单已经处置过了，不能再拒绝"))
        if issues:
            return ConfirmOutcome(batch, issues)

        self._batches.decide(batch.id, action="rejected", at=self._clock())
        return ConfirmOutcome(self._batches.get(batch.id), [])

    def _target_lead(
        self, batch: SortingBatch, action: str, lead_id: str | None
    ) -> str | list[ValidationIssue]:
        """算出这次处置要落到哪条线索；算不出来就返回问题列表（位置是 `lead_id`）。"""
        if action == DECISION_FIX:
            target = lead_id.strip() if isinstance(lead_id, str) else ""
            if not target:
                return [ValidationIssue("lead_id", "改归属必须指定一个已有的线索")]
            return target

        target = batch.lead_id or ""
        if not target:
            return [
                ValidationIssue(
                    "lead_id",
                    "这张单还没有归属客户，请先认领（POST /sorting/{id}/claim）",
                )
            ]
        return target

    def _apply_elements(
        self, batch: SortingBatch, lead_id: str
    ) -> tuple[int, list[dict], list[ValidationIssue]]:
        """把分拣单里的要素落成该客户的事实：返回（落库条数, 跳过明细, 问题列表）。

        落库前**再校验一次出处**：从 `suggestion_json`（审计原文）重新解析要素，逐条回到
        投料当时的原文里对齐；对不上的跳过——防「审核后建议被改坏」也能一路写到事实层。
        同字段只留一条：已有事实被人工锁定的跳过并说明原因（`decide_merge` 的口径）。
        """
        if self._facts is None:
            return 0, [], [ValidationIssue("facts", "分拣服务没有装配事实仓储，无法落事实")]
        raw = self._raws.get(batch.raw_id)
        if raw is None:
            return 0, [], [ValidationIssue("raw_id", "原文不存在，无法复核出处")]

        payload = _loads(batch.suggestion_json) or {}
        raw_elements = payload.get("elements")
        elements, diagnostics = validate_elements(raw.content, raw_elements)
        skipped: list[dict] = [
            {"field": _element_field(raw_elements, index), "reason": message}
            for index, message in (split_diagnostic(item) for item in diagnostics)
        ]

        now = self._clock()
        applied = 0
        for element in elements:
            existing = self._facts.get_by_field(lead_id, element.field)
            candidate = {
                "field": element.field,
                "value": element.value,
                "source_span": element.source_span,
                "confidence": element.confidence,
            }
            fact, why = decide_merge(
                existing, candidate, lead_id=lead_id, raw_id=batch.raw_id, at=now
            )
            if fact is None:
                skipped.append({"field": element.field, "reason": why})
                continue

            written = replace(fact, card_section=element.card_section)
            if existing is None:
                self._facts.add(written)
            else:
                self._facts.update(replace(written, id=existing.id))
            applied += 1

        return applied, skipped, []

    def _contact_for_claim(self, batch: SortingBatch) -> tuple[str, list[ValidationIssue]]:
        """认领新建客户时取联系方式：只从已过出处校验的要素里找，找不到就留空并提示。"""
        raw = self._raws.get(batch.raw_id)
        if raw is None:
            return "", [ValidationIssue("contact", "原文不存在，新客户的联系方式留空，请补录")]

        payload = _loads(batch.suggestion_json) or {}
        elements, _diagnostics = validate_elements(raw.content, payload.get("elements"))
        contact = contact_from_elements(elements)
        if not contact:
            return "", [
                ValidationIssue("contact", "要素里没有联系方式，客户已建但联系方式留空，请补录")
            ]
        if len(contact) < MIN_CONTACT_LEN:
            return "", [
                ValidationIssue("contact", f"{CONTACT_TOO_SHORT}，客户已建但联系方式留空，请补录")
            ]
        return contact, []

    # ------------------------------------------------------------------ 查询

    def list_batches(
        self, *, status: str | None = None, limit: int = 50
    ) -> tuple[list[dict], list[ValidationIssue]]:
        """分拣单列表（倒序）：可按状态过滤；每条带 `lead_name`（归属未定时为空）、
        `dropped_count`（投料时被丢弃的要素条数，供列表标「有丢弃」）与
        `content_preview`（原文前 60 字，超出加 `…`；原文缺失或为空就是空串）。"""
        issues: list[ValidationIssue] = []
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_BATCH_LIMIT
        ):
            issues.append(
                ValidationIssue("limit", f"limit 必须在 1-{MAX_BATCH_LIMIT} 之间（实际是 {limit}）")
            )

        wanted: list[SortingBatch] = []
        if status is None:
            for state in SORTING_STATUSES:
                wanted.extend(self._batches.list_by_status(state))
        else:
            status_text = status.strip() if isinstance(status, str) else ""
            if status_text not in SORTING_STATUSES:
                allowed = "、".join(SORTING_STATUSES)
                issues.append(
                    ValidationIssue(
                        "status", f"状态必须是以下之一：{allowed}（实际是 {status_text}）"
                    )
                )
            else:
                wanted = self._batches.list_by_status(status_text)

        if issues:
            return [], issues

        names = {lead.id: lead.name for lead in self._leads.list()}
        wanted.sort(key=lambda item: item.created_at, reverse=True)
        items: list[dict] = []
        for item in wanted[:limit]:
            body = _summary(item, names.get(item.lead_id))
            # 列表只多一个「丢了几条」的整数：诊断正文留给单条接口，列表不必背着全文；
            # 解析失败按 0（复用 `_loads_items` 的口径），一条坏 JSON 不该拖垮整页列表。
            body["dropped_count"] = len(_loads_items(item.dropped_json))
            # 原文摘要同理：列表只给前 60 字够认单，全文留给单条接口。
            raw = self._raws.get(item.raw_id)
            body["content_preview"] = _preview(raw.content if raw is not None else "")
            items.append(body)
        return items, []

    def get_batch(self, batch_id: str) -> tuple[dict | None, list[ValidationIssue]]:
        """单张分拣单：带模型原文（`suggestion_json`）、解析后的视图（`suggestion`）、
        投料原文（`content`）、归属候选（`lead_candidates`）与丢弃诊断（`dropped`）。"""
        batch = self._batches.get(batch_id)
        if batch is None:
            return None, [ValidationIssue("batch_id", "分拣单不存在")]

        lead = self._leads.get(batch.lead_id) if batch.lead_id else None
        raw = self._raws.get(batch.raw_id)
        body = _summary(batch, lead.name if lead is not None else None)
        body.update(
            {
                "raw_id": batch.raw_id,
                "content": raw.content if raw is not None else "",
                "suggestion_json": batch.suggestion_json,
                "suggestion": _loads(batch.suggestion_json),
                "lead_candidates": _loads_items(batch.candidates_json),
                "dropped": _loads_items(batch.dropped_json),
            }
        )
        return body, []


def _summary(batch: SortingBatch, lead_name: str | None) -> dict:
    """列表里的一张分拣单：不带模型原文，带归属人的名字（没有就 null）。

    `decided_at` / `decided_action` 是人工处置留痕（没处置过就是 null）；
    处置接口要靠它回「这张单最后怎么处理的」，所以列表与单条都带。
    """
    return {
        "id": batch.id,
        "status": batch.status,
        "lead_id": batch.lead_id,
        "lead_name": lead_name,
        "confidence": batch.confidence,
        "scorer": batch.scorer,
        "created_at": batch.created_at,
        "decided_at": batch.decided_at,
        "decided_action": batch.decided_action,
    }


def _candidate_dicts(candidates: list[Candidate]) -> list[dict]:
    """把归属漏斗的候选翻成落库/回传的固定形状：`{"lead_id", "name", "score", "scorer"}`。"""
    return [
        {
            "lead_id": item.lead_id,
            "name": item.name,
            "score": item.score,
            "scorer": item.scorer,
        }
        for item in candidates
    ]


def _preview(content: str, *, limit: int = CONTENT_PREVIEW_LEN) -> str:
    """原文摘要：前 `limit` 字，超出补 `…`；空内容给空串（不拿占位文字冒充原文）。"""
    text = content if isinstance(content, str) else ""
    if not text:
        return ""
    return text[:limit] + ("…" if len(text) > limit else "")


def _dropped(diagnostics: list[str]) -> list[dict]:
    """把 `elements[i]：原因` 的诊断翻成 `{"index", "reason"}`；形状不对就原样带上原因。"""
    dropped: list[dict] = []
    for message in diagnostics:
        index, reason = split_diagnostic(message)
        dropped.append({"index": index, "reason": reason})
    return dropped


def _clean_grading(raw: object) -> dict | None:
    """校验模型给的分级：`value` 必须是 A/B/C、`reason` 非空、`basis` 是字符串列表。

    不合规返回 None，由用例层走规则兜底——**不修补**模型的值，也不猜它想说什么。
    """
    if not isinstance(raw, dict):
        return None
    value = raw.get("value")
    reason = raw.get("reason")
    basis = raw.get("basis", [])
    if not isinstance(value, str) or value.strip().upper() not in GRADING_VALUES:
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    if not isinstance(basis, list) or not all(isinstance(item, str) for item in basis):
        return None
    return {"value": value.strip().upper(), "reason": reason.strip(), "basis": list(basis)}


def _loads(text: str) -> dict | None:
    """把落库的原文解析回对象；解析不了就返回 None（原文照样能看，不抛异常）。"""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _loads_items(text: str) -> list:
    """把落库的 JSON 数组解析回来；解析不了或不是数组都返回空列表。"""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _element_field(elements: object, index: int | None) -> str:
    """模型原始输出里第 `index` 条要素的字段名；取不到就用 `elements[i]` 占位。"""
    if isinstance(elements, list) and index is not None and 0 <= index < len(elements):
        item = elements[index]
        if isinstance(item, dict):
            name = item.get("field")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return f"elements[{index}]" if index is not None else "elements"
