"""模板文件读取（IO 只在这一层）。

`load_template` 读单份 YAML，`load_all` 读整个目录。任何"读不成映射"的情况都抛
`TemplateError`，并在消息里带上文件路径；YAML 语法错误还带上行列位置，
方便上层直接说"哪个文件第几行坏了"，而不是静默降级成默认流程。
"""

from pathlib import Path
from typing import Any

import yaml


class TemplateError(Exception):
    """模板文件无法读取、不是合法 YAML、或顶层不是映射。"""


def load_template(path: Path) -> dict:
    """读一份模板 YAML，返回顶层映射；不合法一律抛 `TemplateError`。"""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TemplateError(f"{path}: 无法读取模板文件：{exc}") from exc

    try:
        data: Any = yaml.safe_load(text)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark else "未知位置"
        raise TemplateError(f"{path}: YAML 语法错误（{where}）：{exc.problem}") from exc
    except yaml.YAMLError as exc:
        raise TemplateError(f"{path}: YAML 解析失败：{exc}") from exc

    if data is None:
        raise TemplateError(f"{path}: 模板文件为空")
    if not isinstance(data, dict):
        raise TemplateError(f"{path}: 模板顶层必须是映射，实际是 {type(data).__name__}")
    return data


def load_all(template_dir: Path) -> dict[str, dict]:
    """读整个模板目录：文件名（不含后缀）-> 模板数据。"""
    template_dir = Path(template_dir)
    if not template_dir.is_dir():
        raise TemplateError(f"{template_dir}: 模板目录不存在")

    return {path.stem: load_template(path) for path in sorted(template_dir.glob("*.yaml"))}
