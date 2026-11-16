"""事实层的纯规则：候选校验 + 合并判定（无 IO、不抛异常）。

模型给什么候选由 `adapters/llm/*` 决定；能不能落库、落哪一条，是这里说了算。
两条硬约束：
1. 没有原文片段的候选一律不要（没有出处的「事实」无法复核）；
2. 人工锁定的事实不被模型覆盖（人比模型大一票）。
"""

import uuid
from dataclasses import dataclass

from salescopilot.domain.models import ALLOWED_OBJECTIONS, Fact
from salescopilot.domain.rules import ValidationIssue, is_filled, validate_lead_fields

# 允许的事实字段：通用叫法，任何行业都能用；扩展枚举只改这里。
ALLOWED_FACT_FIELDS: tuple[str, ...] = (
    "decision_maker",
    "need",
    "objection",
    "amount",
    "appointment_at",
)

# 事实字段 → 线索字段（流程推进要用的那个）：只有这些事实能自动补进线索字段。
# `objection` 不进块契约的必填项，所以不映射；`conclusion` 由「跟进确认」写入，也不来自事实。
FACT_TO_FIELD: dict[str, str] = {
    "decision_maker": "decision_maker",
    "need": "need",
    "appointment_at": "appointment_at",
    "amount": "amount",
}

MAX_FACT_VALUE_LEN = 200


@dataclass(frozen=True)
class _Candidate:
    """校验通过的候选（内部用）：值已去空格，置信度已转 float。"""

    field: str
    value: str
    source_span: str
    confidence: float


def validate_candidates(
    candidates: list[dict],
) -> tuple[list[dict], list[ValidationIssue]]:
    """逐条校验提炼候选；返回（通过的候选, 被丢弃的问题）。

    丢弃规则：字段不在白名单、值去空格后为空或超过 200 字、原文片段为空、
    异议不在枚举内、置信度不是 0..1 的数值。问题位置写成 `candidates[i].<哪一项>`，
    方便前端直接指到第几条候选的哪个字段。
    """
    indexed, issues = validate_candidates_indexed(candidates)
    return [candidate for _, candidate in indexed], issues


def validate_candidates_indexed(
    candidates: list[dict],
) -> tuple[list[tuple[int, dict]], list[ValidationIssue]]:
    """同 `validate_candidates`，但保留候选在原列表里的下标。

    用例层要靠这个下标写出「这条候选为什么没落库」（位置指回模型原始输出）。
    """
    passed: list[tuple[int, dict]] = []
    issues: list[ValidationIssue] = []

    for index, candidate in enumerate(candidates):
        parsed, candidate_issues = _validate_one(index, candidate)
        if parsed is None:
            issues.extend(candidate_issues)
            continue
        passed.append(
            (
                index,
                {
                    "field": parsed.field,
                    "value": parsed.value,
                    "source_span": parsed.source_span,
                    "confidence": parsed.confidence,
                },
            )
        )

    return passed, issues


def _validate_one(index: int, candidate: object) -> tuple[_Candidate | None, list[ValidationIssue]]:
    if not isinstance(candidate, dict):
        return None, [ValidationIssue(f"candidates[{index}]", "候选必须是映射")]

    issues: list[ValidationIssue] = []

    field = candidate.get("field")
    field_text = field.strip() if isinstance(field, str) else ""
    if not field_text:
        issues.append(ValidationIssue(f"candidates[{index}].field", "字段不能为空"))
    elif field_text not in ALLOWED_FACT_FIELDS:
        allowed = "、".join(ALLOWED_FACT_FIELDS)
        issues.append(
            ValidationIssue(
                f"candidates[{index}].field",
                f"字段必须是以下之一：{allowed}（实际是 {field_text}）",
            )
        )

    value = candidate.get("value")
    value_text = value.strip() if isinstance(value, str) else ""
    if not value_text:
        issues.append(ValidationIssue(f"candidates[{index}].value", "取值不能为空"))
    elif len(value_text) > MAX_FACT_VALUE_LEN:
        issues.append(
            ValidationIssue(
                f"candidates[{index}].value",
                f"取值不能超过 {MAX_FACT_VALUE_LEN} 个字符（实际 {len(value_text)}）",
            )
        )

    span = candidate.get("source_span")
    span_text = span.strip() if isinstance(span, str) else ""
    if not span_text:
        issues.append(ValidationIssue(f"candidates[{index}].source_span", "原文片段不能为空"))

    if field_text == "objection" and value_text and value_text not in ALLOWED_OBJECTIONS:
        allowed = "、".join(ALLOWED_OBJECTIONS)
        issues.append(
            ValidationIssue(
                f"candidates[{index}].value",
                f"异议取值必须是以下之一：{allowed}（实际是 {value_text}）",
            )
        )

    confidence = candidate.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        issues.append(ValidationIssue(f"candidates[{index}].confidence", "置信度必须是数值"))
    elif not 0 <= float(confidence) <= 1:
        issues.append(
            ValidationIssue(
                f"candidates[{index}].confidence",
                f"置信度必须在 0 到 1 之间（实际是 {confidence}）",
            )
        )

    if issues:
        return None, issues

    return (
        _Candidate(
            field=field_text,
            value=value_text,
            source_span=span_text,
            confidence=float(confidence),
        ),
        [],
    )


def dedupe_candidates(
    passed: list[tuple[int, dict]],
) -> tuple[list[tuple[int, dict]], list[ValidationIssue]]:
    """同一次提炼里同字段多条候选：只保留最后一条，其余记为 skipped。

    为什么保留最后一条：模型对同一字段给出多个值时，后面的通常是对前面的一次修正
    （同一段原文里「先嫌贵、后说没效果」，以最后说法为准）。位置写成
    `candidates[i].<字段名>`，指回模型原始输出的第几条，方便复核被舍掉的那条。
    """
    kept_order: dict[str, int] = {}
    for order, (_, candidate) in enumerate(passed):
        kept_order[candidate["field"]] = order

    kept: list[tuple[int, dict]] = []
    issues: list[ValidationIssue] = []
    for order, (index, candidate) in enumerate(passed):
        field = candidate["field"]
        if kept_order[field] == order:
            kept.append((index, candidate))
        else:
            issues.append(
                ValidationIssue(f"candidates[{index}].{field}", "同字段重复，保留最后一条")
            )
    return kept, issues


def decide_merge(
    existing: Fact | None,
    candidate: dict,
    *,
    lead_id: str,
    raw_id: str,
    at: str,
) -> tuple[Fact | None, str]:
    """判定一条通过的候选该不该写、写成什么；返回（要写的 fact 或 None, 原因）。

    三条分支：
    - 已有事实被人工锁定 → 不写（"人工锁定，跳过"）；
    - 已有事实的值与候选相同 → 不写（"重复值"）；
    - 其余 → 写（"ok"）：已有事实未锁定时，调用方必须把新值写回**同一条**
      （`id=existing.id`），且 `value / source_span / raw_id / at / confidence / source`
      六项一起覆盖——一个客户一个字段只留一条「当前事实」，历史靠 raw_items + extract_runs 追溯。
    """
    if existing is not None and existing.locked:
        return None, "人工锁定，跳过"
    if existing is not None and existing.value == candidate["value"]:
        return None, "重复值"

    return (
        Fact(
            id=str(uuid.uuid4()),
            lead_id=lead_id,
            raw_id=raw_id,
            field=candidate["field"],
            value=candidate["value"],
            source="llm",
            source_span=candidate["source_span"],
            confidence=float(candidate["confidence"]),
            locked=False,
            at=at,
        ),
        "ok",
    )


def fields_from_facts(facts: list[Fact], current: dict) -> dict:
    """把当前事实补进线索字段：只填 `current` 里缺的键，已填的一律不动（人填的优先）。

    三条取舍：
    1. 只映射 `FACT_TO_FIELD` 里的事实字段——异议与结论都不进流程字段；
    2. 值先过 `validate_lead_fields` 的单字段校验：过不了的（比如约定时间不是
       `YYYY-MM-DD`）不自动补，留给人工补录，免得一条坏值把整批补丁拖死；
    3. 返回的是「这次要补的键」，不含已有键；调用方据它合并落库并回报补了哪些字段。
    """
    patch: dict[str, str] = {}
    for fact in facts:
        target = FACT_TO_FIELD.get(fact.field)
        if target is None or target in patch:
            continue
        if is_filled(current.get(target)):
            continue
        if validate_lead_fields({target: fact.value}):
            continue
        patch[target] = fact.value.strip()
    return patch
