"""采集与提炼用例：全项目唯一的提炼入口。

分工固定：提炼器（`domain/ports.py` 的 `FactExtractor`）只负责「吐候选」；
能不能落库、落哪一条，全在 `domain/facts.py`；本层只负责按顺序把它们串起来，
并把每次提炼记成一条账（`ExtractRun`）。路由与仓储都不许出现提炼规则。

幂等约定：
- 同一线索的同一段内容只入库一次（按内容哈希查重），重复提交返回已有的那条；
- 同一条原始素材重复提炼时，候选与上次相同 → 全部命中「重复值」，`accepted=0`，
  事实条数不变（账本照写一条，用于观察「又跑了一遍」）。
"""

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace

from salescopilot.domain.errors import ExtractError
from salescopilot.domain.facts import (
    ALLOWED_FACT_FIELDS,
    MAX_FACT_VALUE_LEN,
    decide_merge,
    validate_candidates_indexed,
)
from salescopilot.domain.models import ExtractRun, Fact, RawItem
from salescopilot.domain.ports import (
    ExtractRunRepository,
    FactExtractor,
    FactRepository,
    LeadRepository,
    RawItemRepository,
)
from salescopilot.domain.rules import ValidationIssue
from salescopilot.services.leads import utc_now_iso

# 一段素材少于这个字数就没有提炼价值（也为「脏输入」留一个统一口径）。
MIN_CONTENT_LEN = 10

# 人工补录的字段没有原文出处，用这个固定标记占位（前端据来源徽标区分 AI / 人工）。
MANUAL_SOURCE_SPAN = "(人工录入)"


@dataclass(frozen=True)
class ExtractOutcome:
    """一次提炼的结果：成功的候选、被丢弃的原因、以及这次运行的账。"""

    ok: bool
    issues: list[ValidationIssue]
    accepted: list[Fact]
    skipped: list[ValidationIssue]
    run: ExtractRun | None


class IngestService:
    """采集与提炼的用例边界：入参进、（实体或问题）出；不抛业务异常。"""

    def __init__(
        self,
        leads: LeadRepository,
        raws: RawItemRepository,
        facts: FactRepository,
        runs: ExtractRunRepository,
        extractor: FactExtractor,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._leads = leads
        self._raws = raws
        self._facts = facts
        self._runs = runs
        self._extractor = extractor
        self._clock = clock if clock is not None else utc_now_iso

    # ------------------------------------------------------------------ 采集

    def ingest(
        self, lead_id: str, *, content: str, source: str
    ) -> tuple[RawItem | None, bool, list[ValidationIssue]]:
        """登记一条原始素材；返回（素材, 是否重复, 问题列表）。

        第三个返回值是「是否命中已有内容」：接口要靠它区分 201 与 200，
        但去重判断本身只在这里做一次（路由不重复算哈希）。
        """
        if self._leads.get(lead_id) is None:
            return None, False, [ValidationIssue("lead_id", "线索不存在")]

        text = content.strip() if isinstance(content, str) else ""
        source_text = source.strip() if isinstance(source, str) else ""
        issues: list[ValidationIssue] = []
        if not text:
            issues.append(ValidationIssue("content", "内容不能为空"))
        elif len(text) < MIN_CONTENT_LEN:
            issues.append(ValidationIssue("content", f"内容至少需要 {MIN_CONTENT_LEN} 个字符"))
        if not source_text:
            issues.append(ValidationIssue("source", "来源不能为空"))
        if issues:
            return None, False, issues

        content_hash = _sha256(text)
        existing = self._raws.find_by_hash(lead_id, content_hash)
        if existing is not None:
            return existing, True, []

        item = RawItem(
            id=str(uuid.uuid4()),
            lead_id=lead_id,
            source=source_text,
            content=text,
            content_hash=content_hash,
            at=self._clock(),
        )
        self._raws.add(item)
        return item, False, []

    def list_raws(self, lead_id: str) -> tuple[list[RawItem], list[ValidationIssue]]:
        """该线索的原始素材，按时间正序；线索不存在时返回问题。"""
        if self._leads.get(lead_id) is None:
            return [], [ValidationIssue("lead_id", "线索不存在")]
        return self._raws.list_for(lead_id), []

    # ------------------------------------------------------------------ 提炼

    def extract(self, raw_id: str) -> ExtractOutcome:
        """对一条原始素材跑一次提炼：校验候选 → 逐条合并 → 落库 → 记账。"""
        raw = self._raws.get(raw_id)
        if raw is None:
            return ExtractOutcome(
                False, [ValidationIssue("raw_id", "原始素材不存在")], [], [], None
            )

        context = self._facts.list_for(raw.lead_id)
        try:
            result = self._extractor.extract(raw=raw, facts=context)
        except ExtractError as exc:
            # 模型侧的失败（网络/超时/解析不了）翻译成一条问题返回，不让接口抛 500。
            return ExtractOutcome(False, [ValidationIssue("extractor", str(exc))], [], [], None)

        candidates = result.candidates
        passed, problems = validate_candidates_indexed(candidates)

        accepted: list[Fact] = []
        skipped: list[ValidationIssue] = list(problems)
        now = self._clock()

        for index, candidate in passed:
            existing = self._facts.get_by_field(raw.lead_id, candidate["field"])
            fact, reason = decide_merge(
                existing, candidate, lead_id=raw.lead_id, raw_id=raw_id, at=now
            )
            if fact is None:
                skipped.append(ValidationIssue(f"candidates[{index}].{candidate['field']}", reason))
                continue

            written = fact if existing is None else replace(fact, id=existing.id)
            if existing is None:
                self._facts.add(written)
            else:
                self._facts.update(written)
            accepted.append(written)

        run = ExtractRun(
            id=str(uuid.uuid4()),
            raw_id=raw_id,
            model=result.model,
            prompt_version=result.prompt_version,
            input_hash=raw.content_hash,
            output_hash=result.raw_output_hash,
            candidates=len(candidates),
            accepted=len(accepted),
            skipped=len(skipped),
            token=result.token,
            seconds=result.seconds,
            at=now,
        )
        self._runs.add(run)
        return ExtractOutcome(True, [], accepted, skipped, run)

    def runs_for(self, raw_id: str) -> list[ExtractRun]:
        """该素材的提炼账本，按时间正序。"""
        return self._runs.list_for_raw(raw_id)

    def model_for(self, fact: Fact) -> str | None:
        """这条事实出自哪个模型：取同一原文最后一次提炼的 `model`。

        人工补录（`raw_id` 为空）没有模型，返回 None；前端据此只显示「人工」，
        不会把人工填的值误标成某个模型产出的。
        """
        if not fact.raw_id:
            return None
        runs = self._runs.list_for_raw(fact.raw_id)
        return runs[-1].model if runs else None

    # ------------------------------------------------------------------ 事实

    def list_facts(self, lead_id: str) -> tuple[list[Fact], list[ValidationIssue]]:
        if self._leads.get(lead_id) is None:
            return [], [ValidationIssue("lead_id", "线索不存在")]
        return self._facts.list_for(lead_id), []

    def set_manual(
        self, lead_id: str, field: str, value: str
    ) -> tuple[Fact | None, list[ValidationIssue]]:
        """人工写一个字段：有就改、没有就建（upsert），一律记 `manual` 并锁定。

        补录也要能成：客户刚说了一句没跑过提炼的话，销售直接填进去，后续提炼不得覆盖。
        """
        if self._leads.get(lead_id) is None:
            return None, [ValidationIssue("lead_id", "线索不存在")]

        field_text = field.strip() if isinstance(field, str) else ""
        if field_text not in ALLOWED_FACT_FIELDS:
            allowed = "、".join(ALLOWED_FACT_FIELDS)
            return None, [
                ValidationIssue("field", f"字段必须是以下之一：{allowed}（实际是 {field_text}）")
            ]

        text = value.strip() if isinstance(value, str) else ""
        if not text:
            return None, [ValidationIssue("value", "取值不能为空")]
        if len(text) > MAX_FACT_VALUE_LEN:
            return None, [
                ValidationIssue(
                    "value", f"取值不能超过 {MAX_FACT_VALUE_LEN} 个字符（实际 {len(text)}）"
                )
            ]

        existing = self._facts.get_by_field(lead_id, field_text)
        if existing is None:
            created = Fact(
                id=str(uuid.uuid4()),
                lead_id=lead_id,
                raw_id="",
                field=field_text,
                value=text,
                source="manual",
                source_span=MANUAL_SOURCE_SPAN,
                confidence=1.0,
                locked=True,
                at=self._clock(),
            )
            self._facts.add(created)
            return created, []

        fact = replace(
            existing,
            value=text,
            source="manual",
            confidence=1.0,
            locked=True,
            at=self._clock(),
        )
        self._facts.update(fact)
        return fact, []

    def unlock(self, lead_id: str, field: str) -> tuple[Fact | None, list[ValidationIssue]]:
        """解除人工锁定；来源仍是 `manual`，只是允许模型再次提案。"""
        resolved = self._resolve_existing(lead_id, field)
        if isinstance(resolved, list):
            return None, resolved

        fact = replace(resolved, locked=False, at=self._clock())
        self._facts.update(fact)
        return fact, []

    def _resolve_existing(self, lead_id: str, field: str) -> Fact | list[ValidationIssue]:
        """公共前置：线索在、字段合法、该字段有事实；否则返回问题列表。"""
        if self._leads.get(lead_id) is None:
            return [ValidationIssue("lead_id", "线索不存在")]

        field_text = field.strip() if isinstance(field, str) else ""
        if field_text not in ALLOWED_FACT_FIELDS:
            allowed = "、".join(ALLOWED_FACT_FIELDS)
            return [
                ValidationIssue("field", f"字段必须是以下之一：{allowed}（实际是 {field_text}）")
            ]

        existing = self._facts.get_by_field(lead_id, field_text)
        if existing is None:
            return [ValidationIssue("fact", f"该线索还没有「{field_text}」这条事实")]
        return existing


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
