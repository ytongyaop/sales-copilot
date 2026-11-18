"""一次投料 → 分拣单：校验原文 → 归属漏斗 → 分拣模型 → 出处对齐 → 落单。

分工固定，谁都不越界：
- 归属用 `domain/matching.py` 的三级漏斗；L0 精确键（联系方式）命中才 `auto`，
  L1 只走字符串，L2 向量本增量**只留参数不实装**（`vector_score=None`）；
  灰区与「没有候选」一律进待认领池，不猜、不落客户；
- 要素 / 分级 / 出库三块由 `domain/sorting.py` 校验与重算：`source_span` 对不上原文
  就丢弃那一条并记诊断，分级不合法就用规则兜底，出库建议按「先补缺项」重算；
- 模型返回的原始 JSON 文本原样落 `suggestion_json`（审计要「当时到底吐了什么」）；
- 本层只编排，不写判断规则；路由只调本层。
"""

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from salescopilot.domain.errors import SortingError
from salescopilot.domain.matching import (
    Candidate,
    MatchDecision,
    MatchThresholds,
    decide_band,
    fuzzy_score,
    rank_candidates,
    suffix_only_difference,
)
from salescopilot.domain.models import CARD_SECTIONS, SORTING_STATUSES, SortingBatch
from salescopilot.domain.ports import (
    LeadRepository,
    RawItemRepository,
    SortingBatchRepository,
    SortingJudge,
)
from salescopilot.domain.rules import ValidationIssue
from salescopilot.domain.sorting import (
    ElementHit,
    SortingSuggestion,
    build_next_action,
    contact_tokens,
    grade_from_elements,
    normalized_org,
    org_name_candidates,
    split_diagnostic,
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

# 归属打分者的固定取值：投料时直接指定线索记 exact（等同精确键），其余按漏斗结论。
DESIGNATED_SCORER = "exact"

# 分级兜底函数：入参是已过出处校验的要素，返回 {"value", "reason", "basis"}。
Grader = Callable[[list[ElementHit]], dict]


@dataclass(frozen=True)
class SortingOutcome:
    """一次投料的结果：落下的分拣单、拒收原因、已校验的建议、被丢弃的要素。

    - `batch` 为 None 表示没生成分拣单（原文校验不过、线索不存在、模型失败，
      或没有任何可用要素可供出库建议），此时 `issues` 说明原因；
    - `suggestion` 是**已校验**的三块建议（要素已过滤、分级已兜底、出库建议已重算），
      与 `batch.suggestion_json`（模型原文）刻意不同：一个给人看，一个给审计看；
    - `dropped` 每项形如 `{"index", "reason"}`，`index` 是模型输出里的下标。
    """

    batch: SortingBatch | None
    issues: list[ValidationIssue]
    suggestion: SortingSuggestion | None = None
    dropped: list[dict] = field(default_factory=list)


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
    ) -> None:
        """`extractor` 是分拣模型（要素 / 分级 / 出库一次出）；`grader` 是分级的规则兜底。

        `vector_score`：L2 向量的分数。本增量不装向量模型，恒为 None；参数留在签名里，
        将来接上向量只换装配，不用改用例。
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
        )
        self._batches.add(batch)

        suggestion = SortingSuggestion(
            lead_candidates=list(prompt_candidates),
            elements=elements,
            grading=grading,
            next_action=next_action,
        )
        return SortingOutcome(batch, [], suggestion, dropped)

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

    # ------------------------------------------------------------------ 查询

    def list_batches(
        self, *, status: str | None = None, limit: int = 50
    ) -> tuple[list[dict], list[ValidationIssue]]:
        """分拣单列表（倒序）：可按状态过滤；每条带 `lead_name`（归属未定时为空）。"""
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
        return [_summary(item, names.get(item.lead_id)) for item in wanted[:limit]], []

    def get_batch(self, batch_id: str) -> tuple[dict | None, list[ValidationIssue]]:
        """单张分拣单：带模型原文（`suggestion_json`）与解析后的视图（`suggestion`）。"""
        batch = self._batches.get(batch_id)
        if batch is None:
            return None, [ValidationIssue("batch_id", "分拣单不存在")]

        lead = self._leads.get(batch.lead_id) if batch.lead_id else None
        body = _summary(batch, lead.name if lead is not None else None)
        body.update(
            {
                "raw_id": batch.raw_id,
                "suggestion_json": batch.suggestion_json,
                "suggestion": _loads(batch.suggestion_json),
            }
        )
        return body, []


def _summary(batch: SortingBatch, lead_name: str | None) -> dict:
    """列表里的一张分拣单：不带模型原文，带归属人的名字（没有就 null）。"""
    return {
        "id": batch.id,
        "status": batch.status,
        "lead_id": batch.lead_id,
        "lead_name": lead_name,
        "confidence": batch.confidence,
        "scorer": batch.scorer,
        "created_at": batch.created_at,
    }


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
