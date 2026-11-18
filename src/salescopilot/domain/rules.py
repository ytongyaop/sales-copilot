"""模板数据的纯校验规则。

只读 dict、不做 IO、不抛异常：所有问题都作为 `ValidationIssue` 返回，由调用方决定怎么报。
位置（`location`）用 `flow[i]` / `flow[i].timeout` / `thresholds.<key>` / `<root>` 这类稳定写法，
方便上层把错误直接翻译成「第几步的第几个块坏了」。
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date

from salescopilot.domain.blocks import BLOCKS, Block
from salescopilot.domain.card import declared_field_keys
from salescopilot.domain.grading import (
    GRADING_BAND_KEYS,
    GRADING_WEIGHT_KEYS,
    WEIGHT_SUM_TOLERANCE,
)
from salescopilot.domain.labels import block_label, field_label
from salescopilot.domain.models import (
    ALLOWED_OBJECTIONS,
    ALLOWED_SOURCES,
    CARD_SECTIONS,
    EDITABLE_LEAD_FIELDS,
    GRADING_MODES,
)

# 联系方式的最小长度与对应的原因文案：新建线索（这里）与 CSV 导入
# （`domain/csv_import.py`）共用这一条，两边不各写一份阈值和一句提示。
MIN_CONTACT_LEN = 5
CONTACT_TOO_SHORT = "联系方式至少需要 5 个字符"

# 线索字段取值的长度上限：与事实取值同一口径（`domain/facts.py` 的 MAX_FACT_VALUE_LEN）。
MAX_FIELD_VALUE_LEN = 200

REQUIRED_ROOT_KEYS = ("template", "name", "thresholds", "flow", "card", "grading")
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


def validate_new_lead(*, name: str, contact: str, source: str) -> list[ValidationIssue]:
    """校验一条待新建线索的入参；空列表表示可以落库。

    规则：三个字段去掉首尾空格后都不能为空；`contact` 去空格后至少 5 个字符；
    `source` 必须在 `ALLOWED_SOURCES` 里。问题逐条返回，不抛异常、不落库。
    """
    issues: list[ValidationIssue] = []

    if not name.strip():
        issues.append(ValidationIssue("name", "名称不能为空"))

    stripped_contact = contact.strip()
    if not stripped_contact:
        issues.append(ValidationIssue("contact", "联系方式不能为空"))
    elif len(stripped_contact) < MIN_CONTACT_LEN:
        issues.append(ValidationIssue("contact", CONTACT_TOO_SHORT))

    stripped_source = source.strip()
    allowed = "、".join(ALLOWED_SOURCES)
    if not stripped_source:
        issues.append(ValidationIssue("source", "来源不能为空"))
    elif stripped_source not in ALLOWED_SOURCES:
        issues.append(
            ValidationIssue("source", f"来源必须是以下之一：{allowed}（实际是 {stripped_source}）")
        )

    return issues


def validate_template_key(template: str, allowed: Collection[str]) -> list[ValidationIssue]:
    """校验线索要挂的模板是否在已加载清单里；空列表表示可用。

    清单由装配点（`entrypoints/bootstrap.py`）从已加载模板传进来，本函数不认识文件系统。
    """
    if template in allowed:
        return []
    return [ValidationIssue("template", f"未知模板：「{template}」")]


def validate_followup(
    *, conclusion: str, objection: str, next_followup_at: str
) -> list[ValidationIssue]:
    """校验一条跟进留痕的三要素；空列表表示可以落库。

    规则：结论去空格后非空且至少 2 个字符；异议必须在 `ALLOWED_OBJECTIONS` 里；
    下次跟进时间必须是 `YYYY-MM-DD`（`date.fromisoformat` 解析不了就算不合法）。
    问题逐条返回，不抛异常、不落库。
    """
    issues: list[ValidationIssue] = []

    text = conclusion.strip() if isinstance(conclusion, str) else ""
    if not text:
        issues.append(ValidationIssue("conclusion", "跟进结论不能为空"))
    elif len(text) < 2:
        issues.append(ValidationIssue("conclusion", "跟进结论至少需要 2 个字符"))

    objection_value = objection.strip() if isinstance(objection, str) else ""
    allowed = "、".join(ALLOWED_OBJECTIONS)
    if not objection_value:
        issues.append(ValidationIssue("objection", "客户异议不能为空"))
    elif objection_value not in ALLOWED_OBJECTIONS:
        issues.append(
            ValidationIssue(
                "objection",
                f"客户异议必须是以下之一：{allowed}（实际是 {objection_value}）",
            )
        )

    raw_date = next_followup_at.strip() if isinstance(next_followup_at, str) else ""
    if not raw_date:
        issues.append(ValidationIssue("next_followup_at", "下次跟进时间不能为空"))
    elif not _is_iso_date(raw_date):
        issues.append(
            ValidationIssue(
                "next_followup_at",
                f"下次跟进时间必须是 YYYY-MM-DD 格式（实际是 {raw_date}）",
            )
        )

    return issues


def _is_iso_date(text: str) -> bool:
    """只认严格 `YYYY-MM-DD`：先比形状，再让 `date.fromisoformat` 判定日历合法性。"""
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def validate_advance(
    template: dict, current_stage: str, target_stage: str, fields: dict
) -> list[ValidationIssue]:
    """校验一次阶段推进；空列表表示可以推进。

    规则：
    1. 当前阶段与目标阶段都必须是模板 `flow` 里的块；
    2. 目标阶段必须是当前阶段在 `flow` 里的紧邻下一块（不许跳步，也不许倒退）；
    3. 目标块的 `requires` 字段必须都已填（空串、纯空格、None 都算没填）。

    问题逐条返回，不抛异常、不写库；块契约只从 `blocks.py` 取，不在这里硬编码块名顺序。
    """
    issues: list[ValidationIssue] = []
    block_ids = _flow_block_ids(template)

    if current_stage not in block_ids:
        issues.append(
            ValidationIssue("current_stage", f"未知的阶段：「{block_label(current_stage)}」")
        )

    if target_stage not in block_ids:
        issues.append(
            ValidationIssue("target_stage", f"未知的阶段：「{block_label(target_stage)}」")
        )
        return issues

    issues.extend(_check_next_stage(block_ids, current_stage, target_stage))
    if not issues:
        issues.extend(_check_advance_fields(target_stage, fields))
    return issues


def _flow_block_ids(template: dict) -> list[str]:
    flow = template.get("flow")
    if not isinstance(flow, list):
        return []
    return [
        step["block"]
        for step in flow
        if isinstance(step, dict) and isinstance(step.get("block"), str)
    ]


def _check_next_stage(
    block_ids: list[str], current_stage: str, target_stage: str
) -> list[ValidationIssue]:
    """只允许走到紧邻下一块；当前阶段不在 flow 里时先不判断（上面已经报过）。"""
    if current_stage not in block_ids:
        return []

    index = block_ids.index(current_stage)
    if index == len(block_ids) - 1:
        return [
            ValidationIssue(
                "target_stage",
                f"阶段「{block_label(current_stage)}」已是最后一步，不能再推进"
                f"（收到「{block_label(target_stage)}」）",
            )
        ]

    expected = block_ids[index + 1]
    if target_stage != expected:
        return [
            ValidationIssue(
                "target_stage",
                f"只能推进到「{block_label(expected)}」，收到「{block_label(target_stage)}」",
            )
        ]
    return []


def _check_advance_fields(target_stage: str, fields: dict) -> list[ValidationIssue]:
    block = BLOCKS.get(target_stage)
    if block is None:
        # 模板引用了未知块：模板校验阶段已经报过，这里不重复判断，也不硬编码块名。
        return []

    issues: list[ValidationIssue] = []
    for name in block.requires:
        if not is_filled(fields.get(name)):
            issues.append(
                ValidationIssue(
                    f"fields.{name}",
                    f"进入阶段「{block_label(target_stage)}」前必须先填写「{field_label(name)}」",
                )
            )
    return issues


def is_filled(value: object) -> bool:
    """这个字段算不算「填过了」：None 与纯空白字符串都算没填（数字 0 算填了）。"""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def allowed_lead_fields(spec: dict | None = None) -> tuple[str, ...]:
    """线索字段白名单：块契约的旧默认 ∪ 该模板 `card` 段声明的字段。

    旧默认一定保留（向后兼容：老线索、老模板、老调用方都不受影响）；
    模板声明的字段按声明顺序追加在后，去重。
    """
    declared = declared_field_keys(spec) if spec else ()
    return tuple(dict.fromkeys((*EDITABLE_LEAD_FIELDS, *declared)))


def validate_lead_fields(
    fields: dict, *, allowed: Collection[str] | None = None
) -> list[ValidationIssue]:
    """校验一份线索字段补丁；空列表表示可以落库。

    规则：
    1. 键必须在白名单内（`allowed` 由调用方传，默认 `EDITABLE_LEAD_FIELDS`；
       模板声明了信息卡字段时，调用方传「旧默认 ∪ 模板字段」，见 `allowed_lead_fields`）；
    2. 取值去首尾空格后不能为空，长度不超过 `MAX_FIELD_VALUE_LEN`；
    3. `appointment_at` 只认 `YYYY-MM-DD`，统一 ISO 日期（中文时间描述留给事实层的原文，
       进流程字段的必须是可比较的日期）。

    问题位置写成 `fields.<字段名>`，与推进校验（`validate_advance`）的位置口径一致。
    """
    if not isinstance(fields, dict):
        return [ValidationIssue("fields", "字段补丁必须是映射（字段名 -> 取值）")]

    permitted = tuple(allowed) if allowed is not None else EDITABLE_LEAD_FIELDS
    issues: list[ValidationIssue] = []
    allowed_text = "、".join(permitted)
    for name, value in fields.items():
        location = f"fields.{name}"
        if name not in permitted:
            issues.append(
                ValidationIssue(location, f"字段必须是以下之一：{allowed_text}（实际是 {name}）")
            )
            continue

        label = field_label(name)
        if not isinstance(value, str):
            issues.append(ValidationIssue(location, f"「{label}」必须是文本"))
            continue

        text = value.strip()
        if not text:
            issues.append(ValidationIssue(location, f"「{label}」不能为空"))
        elif len(text) > MAX_FIELD_VALUE_LEN:
            issues.append(
                ValidationIssue(
                    location,
                    f"「{label}」不能超过 {MAX_FIELD_VALUE_LEN} 个字符（实际 {len(text)}）",
                )
            )
        elif name == "appointment_at" and not _is_iso_date(text):
            issues.append(
                ValidationIssue(location, f"「{label}」必须是 YYYY-MM-DD 格式（实际是 {text}）")
            )

    return issues


def merge_fields(current: dict, patch: dict) -> dict:
    """合并字段补丁：只覆盖传入的键，其余键原样保留（不删任何已有字段）。

    字符串取值去首尾空格后落库，与 `validate_lead_fields` 的判定口径保持一致。
    输入一律复制，不改调用方手里的那个 dict。
    """
    merged = dict(current)
    for name, value in patch.items():
        merged[name] = value.strip() if isinstance(value, str) else value
    return merged


def missing_fields_for(block_id: str, fields: dict) -> list[str]:
    """进入该块前还缺哪些必填字段（顺序与块契约一致）；未知块返回空列表。

    块契约只从 `blocks.py` 取；`fields` 传「基础属性 + 已采集字段」的合并视图
    （线索的 `contact` / `source` 本身就是实体属性，不是 `fields` 里的键）。
    """
    block = BLOCKS.get(block_id)
    if block is None:
        return []
    return [name for name in block.requires if not is_filled(fields.get(name))]


def validate_template(data: dict, blocks: dict[str, Block] = BLOCKS) -> list[ValidationIssue]:
    """校验一份模板数据，返回全部问题；空列表表示合法。"""
    issues: list[ValidationIssue] = []
    issues.extend(_check_root_keys(data))
    issues.extend(_check_thresholds(data.get("thresholds")))
    issues.extend(_check_flow(data.get("flow"), blocks, data.get("thresholds")))
    if "card" in data:
        issues.extend(_check_card(data.get("card")))
    issues.extend(_check_grading(data.get("grading")))
    return issues


def _check_grading(grading: object) -> list[ValidationIssue]:
    """校验模板的 `grading` 段：四维权重齐全、和为 1、非负；两个分档阈值合法；模式合法。

    为什么这么严：分档是「规则主算」的唯一出口，权重少一项或和不是 1，档位就失去可比性；
    `a_min` 不大于 `b_min` 会让 B 档区间为空（A 与 C 之间没有过渡），必须当场报出来。
    位置写成 `grading.weights.<维度>` / `grading.bands.<阈值>`，上层可直接指到哪一项坏了。
    """
    if not isinstance(grading, dict):
        return [ValidationIssue("grading", "grading 必须是映射（weights / bands / default_mode）")]

    issues: list[ValidationIssue] = []
    issues.extend(_check_grading_weights(grading.get("weights")))
    issues.extend(_check_grading_bands(grading.get("bands")))

    mode = grading.get("default_mode")
    allowed = " / ".join(GRADING_MODES)
    if not isinstance(mode, str) or not mode.strip():
        issues.append(ValidationIssue("grading.default_mode", "default_mode 不能为空"))
    elif mode.strip() not in GRADING_MODES:
        issues.append(
            ValidationIssue(
                "grading.default_mode",
                f"default_mode 必须是 {allowed} 之一（实际是 {mode!r}）",
            )
        )
    return issues


def _check_grading_weights(weights: object) -> list[ValidationIssue]:
    """四维权重：四个键齐全、都是非负数值、和为 1.0（容差 `WEIGHT_SUM_TOLERANCE`）。"""
    if not isinstance(weights, dict):
        return [ValidationIssue("grading.weights", "weights 必须是映射（维度 -> 权重）")]

    issues: list[ValidationIssue] = []
    total = 0.0
    complete = True
    for key in GRADING_WEIGHT_KEYS:
        location = f"grading.weights.{key}"
        if key not in weights:
            issues.append(ValidationIssue(location, f"缺少权重：{key}"))
            complete = False
            continue
        value = weights[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            issues.append(ValidationIssue(location, f"{key} 权重必须是数值，实际是 {value!r}"))
            complete = False
        elif value < 0:
            issues.append(ValidationIssue(location, f"{key} 权重必须非负，实际是 {value!r}"))
            complete = False
        else:
            total += float(value)

    extra = [key for key in weights if key not in GRADING_WEIGHT_KEYS]
    if extra:
        allowed = "、".join(GRADING_WEIGHT_KEYS)
        issues.append(
            ValidationIssue(
                f"grading.weights.{extra[0]}",
                f"未知的维度「{extra[0]}」：只允许 {allowed}",
            )
        )

    if complete and abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
        issues.append(
            ValidationIssue(
                "grading.weights",
                f"四项权重之和必须为 1.0（实际 {total:.4f}）",
            )
        )
    return issues


def _check_grading_bands(bands: object) -> list[ValidationIssue]:
    """两个分档阈值：都在 (0,1) 内，且 `a_min` 必须大于 `b_min`（否则 B 档为空）。"""
    if not isinstance(bands, dict):
        return [ValidationIssue("grading.bands", "bands 必须是映射（a_min / b_min）")]

    issues: list[ValidationIssue] = []
    values: dict[str, float] = {}
    for key in GRADING_BAND_KEYS:
        location = f"grading.bands.{key}"
        if key not in bands:
            issues.append(ValidationIssue(location, f"缺少分档阈值：{key}"))
            continue
        value = bands[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            issues.append(ValidationIssue(location, f"{key} 必须是数值，实际是 {value!r}"))
        elif not 0 < float(value) < 1:
            issues.append(ValidationIssue(location, f"{key} 必须满足 0 < v < 1，实际是 {value!r}"))
        else:
            values[key] = float(value)

    if len(values) == len(GRADING_BAND_KEYS) and values["a_min"] <= values["b_min"]:
        issues.append(
            ValidationIssue(
                "grading.bands.a_min",
                f"a_min 必须大于 b_min（实际 a_min={values['a_min']}, b_min={values['b_min']}）",
            )
        )
    return issues


def _check_card(card: object) -> list[ValidationIssue]:
    """校验模板的 `card` 段：四要素齐全、字段三件套齐全、字段 key 全局不重复。

    位置写成 `card.sections.<分区>` / `card.sections.<分区>.fields[i].<哪一项>`，
    上层可以直接说「第几个分区的第几个字段缺 key」。
    """
    if not isinstance(card, dict):
        return [ValidationIssue("card", "card 必须是映射（sections -> 分区）")]

    sections = card.get("sections")
    if not isinstance(sections, dict):
        return [ValidationIssue("card.sections", "card.sections 必须是映射（分区 -> 字段列表）")]

    issues: list[ValidationIssue] = []
    for key in CARD_SECTIONS:
        if key not in sections:
            issues.append(ValidationIssue(f"card.sections.{key}", f"缺少信息卡分区：{key}"))
    for key in sections:
        if key not in CARD_SECTIONS:
            allowed = "、".join(CARD_SECTIONS)
            issues.append(
                ValidationIssue(
                    f"card.sections.{key}", f"未知的信息卡分区「{key}」：只允许 {allowed}"
                )
            )

    seen: dict[str, str] = {}
    for section_key, section in sections.items():
        location = f"card.sections.{section_key}"
        if not isinstance(section, dict):
            issues.append(ValidationIssue(location, "分区必须是映射（label + fields）"))
            continue

        label = section.get("label")
        if not isinstance(label, str) or not label.strip():
            issues.append(ValidationIssue(f"{location}.label", "分区必须有非空 label"))

        fields = section.get("fields")
        if not isinstance(fields, list) or not fields:
            issues.append(ValidationIssue(f"{location}.fields", "分区必须是非空字段列表"))
            continue

        for index, spec in enumerate(fields):
            field_location = f"{location}.fields[{index}]"
            if not isinstance(spec, dict):
                issues.append(
                    ValidationIssue(field_location, "字段必须是映射（key / label / question）")
                )
                continue
            for name in ("key", "label", "question"):
                value = spec.get(name)
                if not isinstance(value, str) or not value.strip():
                    issues.append(
                        ValidationIssue(f"{field_location}.{name}", f"字段必须有非空 {name}")
                    )

            key = spec.get("key")
            if not isinstance(key, str) or not key.strip():
                continue
            key = key.strip()
            if key in seen:
                issues.append(
                    ValidationIssue(
                        f"{field_location}.key", f"字段 key「{key}」重复（首次出现在 {seen[key]}）"
                    )
                )
            else:
                seen[key] = f"{field_location}.key"

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
            issues.append(ValidationIssue(f"flow[{index}]", f"未知的块：「{block_id}」"))
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
