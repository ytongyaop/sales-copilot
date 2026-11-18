"""模板 `card` 段的校验测试：四要素齐全、字段三件套齐全、key 不重复，错误带位置。

两条入口都要报得出位置：纯校验函数 `domain.rules.validate_template` 与读文件的
`adapters.template.loader.load_template`（后者还要带文件路径）。
"""

import copy
from pathlib import Path

import pytest
import yaml

from salescopilot.adapters.template.loader import TemplateError, load_all, load_template
from salescopilot.domain.rules import validate_template

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "salescopilot" / "templates"
BASE = load_all(TEMPLATES_DIR)["building_materials"]


def _mutated() -> dict:
    return copy.deepcopy(BASE)


def test_all_three_templates_have_valid_card() -> None:
    """三份模板的 card 段都合法（基线：不误报）。"""
    for name, template in load_all(TEMPLATES_DIR).items():
        assert validate_template(template) == [], f"{name} 的 card 段应合法"


def test_missing_section_is_reported_with_location() -> None:
    """缺一个分区：报在 `card.sections.<分区>`，并说清缺的是哪个。"""
    template = _mutated()
    del template["card"]["sections"]["followup"]

    issues = validate_template(template)

    assert any(
        issue.location == "card.sections.followup" and "缺少信息卡分区" in issue.message
        for issue in issues
    ), issues


def test_field_without_key_is_reported_with_index() -> None:
    """字段缺 key：位置指到第几个分区的第几个字段。"""
    template = _mutated()
    del template["card"]["sections"]["basic"]["fields"][0]["key"]

    issues = validate_template(template)

    assert any(
        issue.location == "card.sections.basic.fields[0].key" and "key" in issue.message
        for issue in issues
    ), issues


def test_field_without_question_is_reported_with_index() -> None:
    """字段缺 question：缺项就报缺，不拿 label 顶替。"""
    template = _mutated()
    template["card"]["sections"]["needs"]["fields"][1]["question"] = "   "

    issues = validate_template(template)

    assert any(issue.location == "card.sections.needs.fields[1].question" for issue in issues), (
        issues
    )


def test_duplicate_field_key_is_reported_with_both_locations() -> None:
    """字段 key 重复：报在第二次出现的位置，并指出第一次在哪。"""
    template = _mutated()
    template["card"]["sections"]["basic"]["fields"][1]["key"] = "company"

    issues = validate_template(template)

    duplicate = [issue for issue in issues if "重复" in issue.message]
    assert duplicate, issues
    assert duplicate[0].location == "card.sections.basic.fields[1].key"
    assert "card.sections.basic.fields[0].key" in duplicate[0].message


def test_missing_card_section_is_reported_at_root() -> None:
    """整段 card 缺失：报顶层键缺失（不假装有卡片）。"""
    template = _mutated()
    del template["card"]

    issues = validate_template(template)

    assert any(issue.location == "<root>" and "card" in issue.message for issue in issues), issues


def test_loader_rejects_bad_card_with_file_path(tmp_path: Path) -> None:
    """读文件时就拦下：异常消息带文件路径与具体位置。"""
    template = _mutated()
    template["card"]["sections"]["basic"]["fields"][0]["key"] = ""
    bad = tmp_path / "bad_card.yaml"
    bad.write_text(yaml.safe_dump(template, allow_unicode=True), encoding="utf-8")

    with pytest.raises(TemplateError) as excinfo:
        load_template(bad)

    message = str(excinfo.value)
    assert "bad_card.yaml" in message
    assert "card.sections.basic.fields[0].key" in message
