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
