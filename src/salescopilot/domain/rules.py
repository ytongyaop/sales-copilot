"""模板数据的纯校验规则。

只读 dict、不做 IO、不抛异常：所有问题都作为 `ValidationIssue` 返回，由调用方决定怎么报。
位置（`location`）用 `flow[i]` / `flow[i].timeout` / `thresholds.<key>` / `<root>` 这类稳定写法，
方便上层把错误直接翻译成「第几步的第几个块坏了」。
"""

from dataclasses import dataclass

from salescopilot.domain.blocks import BLOCKS, Block

REQUIRED_ROOT_KEYS = ("template", "name", "thresholds", "flow")
REQUIRED_THRESHOLD_KEYS = (
    "first_response_minutes",
    "feedback_window_hours",
    "stale_followup_days",
    "zombie_ratio_max",
)
RATIO_THRESHOLD_KEYS = ("zombie_ratio_max",)
FIRST_BLOCK_ID = "intake"


@dataclass(frozen=True)
class ValidationIssue:
    """一条校验问题：坏在哪（location）＋为什么坏（message）。"""

    location: str
    message: str


def validate_template(data: dict, blocks: dict[str, Block] = BLOCKS) -> list[ValidationIssue]:
    """校验一份模板数据，返回全部问题；空列表表示合法。"""
    issues: list[ValidationIssue] = []
    issues.extend(_check_root_keys(data))
    issues.extend(_check_thresholds(data.get("thresholds")))
    issues.extend(_check_flow(data.get("flow"), blocks, data.get("thresholds")))
    return issues


def _check_root_keys(data: dict) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for key in REQUIRED_ROOT_KEYS:
        if key not in data:
            issues.append(ValidationIssue("<root>", f"缺少顶层键：{key}"))
    return issues


def _check_thresholds(thresholds: object) -> list[ValidationIssue]:
    if not isinstance(thresholds, dict):
        return [ValidationIssue("thresholds", "thresholds 必须是映射（键 -> 数值）")]

    issues: list[ValidationIssue] = []
    for key in REQUIRED_THRESHOLD_KEYS:
        if key not in thresholds:
            issues.append(ValidationIssue(f"thresholds.{key}", f"缺少阈值参数：{key}"))

    for key in REQUIRED_THRESHOLD_KEYS:
        if key not in thresholds:
            continue
        value = thresholds[key]
        location = f"thresholds.{key}"
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            issues.append(ValidationIssue(location, f"{key} 必须是数值，实际是 {value!r}"))
        elif value <= 0:
            issues.append(ValidationIssue(location, f"{key} 必须是正数，实际是 {value!r}"))
        elif key in RATIO_THRESHOLD_KEYS and value > 1:
            issues.append(ValidationIssue(location, f"{key} 必须满足 0 < v <= 1，实际是 {value!r}"))
    return issues


def _check_flow(
    flow: object, blocks: dict[str, Block], thresholds: object
) -> list[ValidationIssue]:
    if not isinstance(flow, list) or not flow:
        return [ValidationIssue("flow", "flow 必须是非空列表")]

    issues: list[ValidationIssue] = []
    block_ids: list[str | None] = []
    seen: dict[str, int] = {}

    for index, step in enumerate(flow):
        if not isinstance(step, dict) or "block" not in step:
            issues.append(ValidationIssue(f"flow[{index}]", "每一项必须是以 'block' 为键的映射"))
            block_ids.append(None)
            continue

        block_id = step["block"]
        if not isinstance(block_id, str) or block_id not in blocks:
            issues.append(ValidationIssue(f"flow[{index}]", f"未知的块：{block_id!r}"))
            block_ids.append(None)
            continue

        block_ids.append(block_id)
        if block_id in seen:
            issues.append(
                ValidationIssue(
                    f"flow[{index}]",
                    f"块 {block_id} 重复出现（首次出现在 flow[{seen[block_id]}]）",
                )
            )
        else:
            seen[block_id] = index

        label = step.get("label")
        if label is not None and label != "" and not isinstance(label, str):
            issues.append(
                ValidationIssue(
                    f"flow[{index}].label",
                    f"块 {block_id} 的 label 必须是非空字符串，实际是 {label!r}",
                )
            )

    issues.extend(_check_order(flow, block_ids, blocks))

    threshold_keys = set(thresholds) if isinstance(thresholds, dict) else None
    if threshold_keys is not None:
        for index, block_id in enumerate(block_ids):
            if block_id is None:
                continue
            timeout_key = blocks[block_id].timeout_key
            if timeout_key is not None and timeout_key not in threshold_keys:
                issues.append(
                    ValidationIssue(
                        f"flow[{index}].timeout",
                        f"块 {block_id} 的 timeout_key {timeout_key} 未在 thresholds 中定义",
                    )
                )
    return issues


def _check_order(
    flow: list, block_ids: list[str | None], blocks: dict[str, Block]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if block_ids and block_ids[0] is not None and block_ids[0] != FIRST_BLOCK_ID:
        issues.append(
            ValidationIssue("flow[0]", f"首块必须是 {FIRST_BLOCK_ID}，实际是 {block_ids[0]}")
        )

    for index in range(len(flow) - 1):
        current, following = block_ids[index], block_ids[index + 1]
        if current is None or following is None:
            continue
        exits = blocks[current].exits
        if not exits:
            issues.append(
                ValidationIssue(
                    f"flow[{index}]",
                    f"块 {current} 是终止块，后面不允许再有块 {following}",
                )
            )
        elif following not in exits:
            issues.append(
                ValidationIssue(
                    f"flow[{index + 1}]",
                    f"块 {following} 不在块 {current} 的允许出口 {exits} 中",
                )
            )

    last_index = len(flow) - 1
    last_block = block_ids[last_index]
    if last_block is not None and blocks[last_block].exits:
        issues.append(
            ValidationIssue(
                f"flow[{last_index}]",
                f"末块 {last_block} 必须是终止块（exits 为空），实际出口是 {blocks[last_block].exits}",
            )
        )
    return issues
