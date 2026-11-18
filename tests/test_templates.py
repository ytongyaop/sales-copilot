"""块契约与模板校验的行为测试。

这里只测「纯校验函数」与「三份模板数据」：不推进状态机、不碰 API。
"""

from pathlib import Path

from salescopilot.adapters.template.loader import load_all
from salescopilot.domain.blocks import BLOCKS
from salescopilot.domain.rules import validate_template

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "salescopilot" / "templates"
TEMPLATE_NAMES = {"edu_training", "software_outsourcing", "building_materials"}


def _load() -> dict[str, dict]:
    return load_all(TEMPLATES_DIR)


def test_all_three_templates_valid() -> None:
    data = _load()
    assert set(data) == TEMPLATE_NAMES
    for name, template in data.items():
        assert validate_template(template) == [], f"模板 {name} 应合法，实际有问题"


def test_removing_appointment_breaks_edu_only() -> None:
    blocks = dict(BLOCKS)
    del blocks["appointment"]
    data = _load()

    edu_issues = validate_template(data["edu_training"], blocks=blocks)
    assert edu_issues, "教培模板引用了 appointment，块被删掉后必须报错"
    assert any("appointment" in issue.message for issue in edu_issues)

    assert validate_template(data["building_materials"], blocks=blocks) == []


def test_block_reuse_at_least_five() -> None:
    counts = dict.fromkeys(BLOCKS, 0)
    for template in _load().values():
        for step in template["flow"]:
            counts[step["block"]] += 1

    assert sum(1 for count in counts.values() if count >= 2) >= 5, counts


def test_unknown_block_reports_location() -> None:
    template = _load()["edu_training"]
    flow = [dict(step) for step in template["flow"]]
    flow[3]["block"] = "nonexistent"
    issues = validate_template({**template, "flow": flow})

    assert any(
        issue.location == "flow[3]" and "nonexistent" in issue.message for issue in issues
    ), issues


def test_missing_threshold_reports_key() -> None:
    template = _load()["edu_training"]
    thresholds = dict(template["thresholds"])
    del thresholds["first_response_minutes"]
    issues = validate_template({**template, "thresholds": thresholds})

    assert any(
        issue.location == "thresholds.first_response_minutes"
        and "first_response_minutes" in issue.message
        for issue in issues
    ), issues


def test_bad_order_rejected() -> None:
    template = _load()["edu_training"]
    step_map = {step["block"]: step for step in template["flow"]}
    flow = [dict(step_map["intake"]), dict(step_map["proposal"])]
    issues = validate_template({**template, "flow": flow})

    assert any(issue.location == "flow[1]" and "proposal" in issue.message for issue in issues), (
        issues
    )


def test_terminal_block_must_be_last() -> None:
    template = _load()["edu_training"]
    flow = [dict(step) for step in template["flow"]] + [{"block": "proposal"}]
    issues = validate_template({**template, "flow": flow})

    assert any(issue.location == "flow[5]" and "close" in issue.message for issue in issues), issues


# ------------------------------------------------------- 分级段（T006 / ADR-0008）


def _with_grading(template: dict, **patch: object) -> dict:
    """复制一份模板，只替换 `grading` 段里的指定键（其余保持原样）。"""
    grading = {**template["grading"], **patch}
    return {**template, "grading": grading}


def test_all_templates_declare_grading_section() -> None:
    """三份模板都带完整的 grading 段：四维权重（和为 1）、两个阈值、默认模式。"""
    for name, template in _load().items():
        grading = template["grading"]
        assert set(grading["weights"]) == {"amount", "urgency", "clarity", "fit"}, name
        assert abs(sum(grading["weights"].values()) - 1.0) <= 0.001, name
        assert grading["bands"]["a_min"] > grading["bands"]["b_min"], name
        assert grading["default_mode"] in ("propose", "auto"), name


def test_missing_grading_section_rejected() -> None:
    """缺 `grading` 段：顶层键缺一项 + grading 本身不合法，两条都要报出来。"""
    template = _load()["edu_training"]
    data = {key: value for key, value in template.items() if key != "grading"}

    issues = validate_template(data)

    assert any(issue.location == "<root>" and "grading" in issue.message for issue in issues), (
        issues
    )
    assert any(issue.location == "grading" for issue in issues), issues


def test_grading_weights_must_sum_to_one() -> None:
    """权重和不是 1 → 报错（档位之间就没有可比性了）。"""
    template = _load()["edu_training"]
    weights = {**template["grading"]["weights"], "fit": 0.1}

    issues = validate_template(_with_grading(template, weights=weights))

    assert any(
        issue.location == "grading.weights" and "之和必须为 1.0" in issue.message
        for issue in issues
    ), issues


def test_grading_missing_weight_key_rejected() -> None:
    """四维权重缺一项 → 报错并指到那个维度。"""
    template = _load()["edu_training"]
    weights = dict(template["grading"]["weights"])
    del weights["clarity"]

    issues = validate_template(_with_grading(template, weights=weights))

    assert any(issue.location == "grading.weights.clarity" for issue in issues), issues


def test_grading_negative_weight_rejected() -> None:
    """负权重 → 报错（打分方向会反过来）。"""
    template = _load()["edu_training"]
    weights = {**template["grading"]["weights"], "amount": -0.1, "fit": 0.5}

    issues = validate_template(_with_grading(template, weights=weights))

    assert any(
        issue.location == "grading.weights.amount" and "非负" in issue.message for issue in issues
    ), issues


def test_grading_bands_must_be_ordered_and_inside_unit_interval() -> None:
    """`a_min` 不大于 `b_min` → 报错；阈值必须落在 (0,1) 内。"""
    template = _load()["edu_training"]

    swapped = validate_template(_with_grading(template, bands={"a_min": 0.4, "b_min": 0.6}))
    assert any(
        issue.location == "grading.bands.a_min" and "必须大于" in issue.message for issue in swapped
    ), swapped

    out_of_range = validate_template(_with_grading(template, bands={"a_min": 1.2, "b_min": 0.45}))
    assert any(
        issue.location == "grading.bands.a_min" and "0 < v < 1" in issue.message
        for issue in out_of_range
    ), out_of_range


def test_grading_default_mode_must_be_known() -> None:
    """默认模式只认 propose / auto。"""
    template = _load()["edu_training"]

    issues = validate_template(_with_grading(template, default_mode="silent"))

    assert any(issue.location == "grading.default_mode" for issue in issues), issues
