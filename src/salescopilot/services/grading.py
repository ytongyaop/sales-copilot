"""分级的用例边界：提议（propose）/ 直接定（auto）两种模式 ＋ 只追加的留痕。

分工固定：
- **打分与分档在 `domain/grading.py`**（纯函数，权重与阈值从模板读），本层不算分；
- 本层只做三件事：取该客户的模板与事实 → 调纯函数 → 把结果按模式写进
  `grading_history`（`ai_value` / `human_value` / `reason` 三列留痕）；
- 历史**只追加、不 update**：改判是「再记一行」，不是「把上一次改掉」，
  审计才能回答「谁在什么时候把 A 改成了 B、为什么」。

两种模式的区别只在「要不要等人确认」：
- `propose`：只写一行建议（`ai_value` 有值、`human_value` 为空），等 `decide`；
- `auto`：建议写完**立刻**再写一行采纳（`ai_value` 与 `human_value` 都是规则结果）。
模式默认取模板 `grading.default_mode`，接口可按次覆盖（不做全局设置页）。
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from salescopilot.domain.grading import (
    GRADE_VALUES,
    GradingResult,
    grade_from_facts,
    grading_spec,
    parse_iso_date,
)
from salescopilot.domain.models import GRADING_MODES, GradingRecord, Lead
from salescopilot.domain.ports import FactRepository, GradingRepository, LeadRepository
from salescopilot.domain.rules import ValidationIssue
from salescopilot.services.leads import utc_now_iso

# 两种模式的固定取值：与 `domain/models.py` 的 `GRADING_MODES` 同一份清单。
MODE_PROPOSE = "propose"
MODE_AUTO = "auto"

# 改判理由至少这么长：一个字的「行」说明不了为什么，复核时等于没写。
MIN_REASON_LEN = 2

# auto 模式采纳行的固定理由：它不是人写的，写明「规则结果直接采纳」，别和人工理由混。
AUTO_REASON = "auto 模式：规则结果直接采纳"


@dataclass(frozen=True)
class GradingDecision:
    """一次人工定档的结果：新写的那行留痕 ＋ 当时采用的模式。"""

    record: GradingRecord
    mode: str


class GradingService:
    """分级边界：线索 id 进，（分级结果 / 留痕行 / 问题）出；不抛业务异常。"""

    def __init__(
        self,
        leads: LeadRepository,
        facts: FactRepository,
        history: GradingRepository,
        template_loader: Callable[[str], dict],
        clock: Callable[[], str] | None = None,
    ) -> None:
        """`template_loader` 由装配点注入（已校验过的模板清单），本层不认识文件系统。

        `clock` 只用来取「今天」与留痕时间：今天再传给 `grade_from_facts`，
        领域层自己不看日历（同一批事实配同一天，永远同一档）。
        """
        self._leads = leads
        self._facts = facts
        self._history = history
        self._template_loader = template_loader
        self._clock = clock if clock is not None else utc_now_iso

    # ------------------------------------------------------------------ 提议

    def propose(
        self, lead_id: str, *, mode: str | None = None
    ) -> tuple[GradingResult | None, list[ValidationIssue]]:
        """按规则给这个客户出一档，并按模式留痕；返回（分级结果, 问题）。

        `mode` 不传就用模板默认；`auto` 模式下多写一行「采纳」，所以一次调用可能落两行。
        """
        lead, spec, issues = self._lead_and_spec(lead_id)
        if lead is None or spec is None:
            return None, issues

        resolved, mode_issues = self._resolve_mode(mode, spec, records=[])
        if mode_issues:
            return None, mode_issues

        today = self._today()
        if today is None:
            return None, [ValidationIssue("clock", "时钟返回值不是 YYYY-MM-DD，无法做紧迫度判断")]

        result = grade_from_facts(spec, self._facts.list_for(lead_id), today=today)
        self._append(
            lead_id,
            mode=MODE_PROPOSE,
            ai_value=result.value,
            human_value=None,
            reason=result.reason,
        )
        if resolved == MODE_AUTO:
            self._append(
                lead_id,
                mode=MODE_AUTO,
                ai_value=result.value,
                human_value=result.value,
                reason=AUTO_REASON,
            )
        return result, []

    # ------------------------------------------------------------------ 定档

    def decide(
        self,
        lead_id: str,
        *,
        value: str,
        reason: str | None = None,
        actor: str | None = None,
        mode: str | None = None,
    ) -> tuple[GradingDecision | None, list[ValidationIssue]]:
        """人工定档：`value` 只能是 A/B/C；**改档必须写理由**（≥2 字），不改档可空。

        `ai_value` 取最近一次提议的值（`needs_info` 也算一次提议）：对 `needs_info`
        直接定档时，`value` 必然与它不同，所以同样要求写理由。
        """
        lead, spec, issues = self._lead_and_spec(lead_id)
        if lead is None or spec is None:
            return None, issues

        value_text = value.strip().upper() if isinstance(value, str) else ""
        if value_text not in GRADE_VALUES:
            allowed = " / ".join(GRADE_VALUES)
            return None, [
                ValidationIssue("value", f"分级必须是 {allowed} 之一（实际是 {value!r}）")
            ]

        records = self._history.list_for(lead_id)
        latest_ai = _latest_ai_value(records)

        reason_text = reason.strip() if isinstance(reason, str) else ""
        if latest_ai is None or value_text != latest_ai:
            problem = _reason_problem(reason_text, has_ai_value=latest_ai is not None)
            if problem is not None:
                return None, [problem]

        resolved, mode_issues = self._resolve_mode(mode, spec, records=records)
        if mode_issues:
            return None, mode_issues

        record = self._append(
            lead_id,
            mode=resolved,
            ai_value=latest_ai,
            human_value=value_text,
            reason=reason_text or None,
            actor=actor.strip() if isinstance(actor, str) and actor.strip() else None,
        )
        return GradingDecision(record=record, mode=resolved), []

    # ------------------------------------------------------------------ 查询

    def history(self, lead_id: str) -> tuple[list[dict], list[ValidationIssue]]:
        """该客户的留痕，**按时间倒序**（最新一行在最前）；线索不存在返回 `([], 问题)`。"""
        if self._leads.get(lead_id) is None:
            return [], [ValidationIssue("lead_id", "线索不存在")]
        records = self._history.list_for(lead_id)
        return [_record_dict(record) for record in reversed(records)], []

    # -------------------------------------------------------------- 内部动作

    def _lead_and_spec(
        self, lead_id: str
    ) -> tuple[Lead | None, dict | None, list[ValidationIssue]]:
        """取线索与它自己的模板：线索不存在 404，模板未知 / 缺段按问题返回。"""
        lead = self._leads.get(lead_id)
        if lead is None:
            return None, None, [ValidationIssue("lead_id", "线索不存在")]

        try:
            spec = self._template_loader(lead.template)
        except KeyError:
            return lead, None, [ValidationIssue("template", f"未知模板：「{lead.template}」")]
        if not isinstance(spec, dict):
            return lead, None, [ValidationIssue("template", f"模板「{lead.template}」不是映射")]

        try:
            grading_spec(spec)
        except KeyError as exc:
            return (
                lead,
                None,
                [ValidationIssue("template", f"模板「{lead.template}」{exc.args[0]}")],
            )
        return lead, spec, []

    def _resolve_mode(
        self, mode: str | None, spec: dict, *, records: list[GradingRecord]
    ) -> tuple[str, list[ValidationIssue]]:
        """算出这次用哪种模式：显式传入 > 最近一行的模式 > 模板默认。

        「沿用当前模式」是为了让一次改动之后的定档不因为默认值不同而跳模式；
        显式传入永远优先（接口按次覆盖，不做全局开关）。
        """
        if mode is not None:
            text = mode.strip() if isinstance(mode, str) else ""
            if text not in GRADING_MODES:
                allowed = " / ".join(GRADING_MODES)
                return MODE_PROPOSE, [
                    ValidationIssue("mode", f"mode 必须是 {allowed} 之一（实际是 {mode!r}）")
                ]
            return text, []

        for record in reversed(records):
            if record.mode in GRADING_MODES:
                return record.mode, []
        return str(grading_spec(spec)["default_mode"]), []

    def _today(self) -> date | None:
        """今天：只切注入时钟的日期部分；不是合法日期就返回 None（不猜）。"""
        stamp = self._clock()
        return parse_iso_date(stamp[:10] if isinstance(stamp, str) else "")

    def _append(
        self,
        lead_id: str,
        *,
        mode: str,
        ai_value: str | None,
        human_value: str | None,
        reason: str | None,
        actor: str | None = None,
    ) -> GradingRecord:
        """往 `grading_history` 追加一行（只追加，绝不 update 已有行）。"""
        record = GradingRecord(
            id=str(uuid.uuid4()),
            lead_id=lead_id,
            mode=mode,
            at=self._clock(),
            ai_value=ai_value,
            human_value=human_value,
            reason=reason,
            actor=actor,
        )
        self._history.add(record)
        return record


def _latest_ai_value(records: list[GradingRecord]) -> str | None:
    """最近一次提议的值：倒着找第一条带 `ai_value` 的记录（采纳行也带，同样算）。"""
    for record in reversed(records):
        if record.ai_value:
            return record.ai_value
    return None


def _reason_problem(reason: str, *, has_ai_value: bool) -> ValidationIssue | None:
    """改档理由的校验：没有 AI 值或改了档都必须写够字数，否则返回问题。"""
    if not reason:
        message = "改档必须写明理由" if has_ai_value else "还没有 AI 建议值，直接定档必须写明理由"
        return ValidationIssue("reason", message)
    if len(reason) < MIN_REASON_LEN:
        return ValidationIssue("reason", f"理由至少需要 {MIN_REASON_LEN} 个字符")
    return None


def _record_dict(record: GradingRecord) -> dict:
    """留痕行的对外形状：三列（`ai_value` / `human_value` / `reason`）＋ 模式与操作者。"""
    return {
        "id": record.id,
        "lead_id": record.lead_id,
        "mode": record.mode,
        "at": record.at,
        "ai_value": record.ai_value,
        "human_value": record.human_value,
        "reason": record.reason,
        "actor": record.actor,
    }
