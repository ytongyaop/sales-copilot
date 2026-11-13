"""模板加载器（IO 层）的行为测试：只验证「读不成映射时抛 TemplateError」。"""

from pathlib import Path

import pytest

from salescopilot.adapters.template.loader import TemplateError, load_all, load_template

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "salescopilot" / "templates"


def test_load_all_three() -> None:
    data = load_all(TEMPLATES_DIR)
    assert len(data) == 3
    assert set(data) == {"edu_training", "software_outsourcing", "building_materials"}


def test_syntax_error_mentions_file_and_line(tmp_path: Path) -> None:
    bad = tmp_path / "broken.yaml"
    bad.write_text(
        "template: broken\nflow:\n  - block: intake\n  - block: [unclosed\n",
        encoding="utf-8",
    )

    with pytest.raises(TemplateError) as excinfo:
        load_template(bad)

    message = str(excinfo.value)
    assert "broken.yaml" in message
    assert "line 5" in message


def test_empty_file_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")

    with pytest.raises(TemplateError) as excinfo:
        load_template(empty)

    assert "empty.yaml" in str(excinfo.value)
