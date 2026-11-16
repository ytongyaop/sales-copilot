"""CSV 导入的纯解析与校验：文本进、行与问题出，不碰库、不碰时钟、不写文件。

规则（与 `services/import_service.py` 的分工：这里只判断「这一行长什么样」，
去重与落库由用例层做）：

1. UTF-8 文本（首部 BOM 自动去掉），逗号分隔；
2. 第一行可以是有表头的（出现「姓名 / 名称」即视为表头），也可以没有表头
   （此时列序固定为 name, contact, source, owner）；表头里不认识的列直接忽略；
3. `source` 只认 `ALLOWED_SOURCES`：英文键原样接受，中文别名按 `SOURCE_ALIASES` 翻译；
4. 空行跳过；缺姓名或联系方式的行记一条问题并跳过，**不中断整批**；联系方式长度
   与新建线索共用 `domain/rules.py` 的同一条规则（去空格后至少 `MIN_CONTACT_LEN` 个字符）；
5. 超过 2MB 的文本整批拒绝（问题行号记 0，表示是文件级问题）。
"""

import csv
import io
from dataclasses import dataclass

from salescopilot.domain.models import ALLOWED_SOURCES
from salescopilot.domain.rules import CONTACT_TOO_SHORT, MIN_CONTACT_LEN

MAX_BYTES = 2 * 1024 * 1024

# 表头别名：键是通用字段名，值是 CSV 里允许出现的列名。
CSV_COLUMNS: dict[str, tuple[str, ...]] = {
    "name": ("姓名", "名称"),
    "contact": ("联系方式", "电话", "手机"),
    "source": ("来源", "渠道"),
    "owner": ("归属", "负责人"),
}

# 来源的中文别名：只影响解析，不新增枚举（真源仍是 `ALLOWED_SOURCES`）。
SOURCE_ALIASES: dict[str, str] = {
    "转介绍": "referral",
    "主动咨询": "inbound",
    "主动触达": "outbound",
    "活动": "event",
    "其他": "other",
}

# 无表头时的固定列序。
DEFAULT_COLUMNS: tuple[str, ...] = ("name", "contact", "source", "owner")


@dataclass(frozen=True)
class ParsedRow:
    """一行可导入的数据；`row_no` 是 CSV 里的物理行号（从 1 开始，表头占 1）。"""

    row_no: int
    name: str
    contact: str
    source: str
    owner: str | None


@dataclass(frozen=True)
class RowIssue:
    """一条不能导入的原因；`row_no=0` 表示文件级问题（如文件过大）。"""

    row_no: int
    message: str


def parse_csv(text: str) -> tuple[list[ParsedRow], list[RowIssue]]:
    """解析一段 CSV 文本：返回（可导入的行, 逐行问题）。"""
    raw = text if isinstance(text, str) else ""
    if len(raw.encode("utf-8")) > MAX_BYTES:
        return [], [RowIssue(0, "文件过大")]

    cleaned = raw.lstrip("\ufeff")
    rows: list[ParsedRow] = []
    issues: list[RowIssue] = []

    header_checked = False
    columns: tuple[str | None, ...] = DEFAULT_COLUMNS

    reader = csv.reader(io.StringIO(cleaned, newline=""))
    for cells in reader:
        row_no = reader.line_num
        cleaned_cells = [_cell(value) for value in cells]
        if all(value == "" for value in cleaned_cells):
            continue

        if not header_checked:
            header_checked = True
            if _looks_like_header(cleaned_cells):
                columns = _header_columns(cleaned_cells)
                continue

        values = _pick(cleaned_cells, columns)
        name = values.get("name", "")
        contact = values.get("contact", "")
        source_text = values.get("source", "")
        owner = values.get("owner", "") or None

        if not name:
            issues.append(RowIssue(row_no, "缺少姓名"))
            continue
        if not contact:
            issues.append(RowIssue(row_no, "缺少联系方式"))
            continue
        if len(contact) < MIN_CONTACT_LEN:
            # 与 `POST /leads` 同一条规则：导入不再接受「非空但太短」的联系方式。
            issues.append(RowIssue(row_no, CONTACT_TOO_SHORT))
            continue

        source = normalize_source(source_text)
        if source is None:
            issues.append(
                RowIssue(row_no, "缺少来源" if not source_text else f"来源不合法：{source_text}")
            )
            continue

        rows.append(
            ParsedRow(
                row_no=row_no,
                name=name,
                contact=contact,
                source=source,
                owner=owner,
            )
        )

    return rows, issues


def normalize_source(value: str) -> str | None:
    """来源取值归一：英文键（大小写不敏感）或中文别名；其它一律返回 None。"""
    text = value.strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in ALLOWED_SOURCES:
        return lowered
    return SOURCE_ALIASES.get(text)


def _cell(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _looks_like_header(cells: list[str]) -> bool:
    """第一行的任一单元格是「姓名 / 名称」就当作表头。"""
    names = CSV_COLUMNS["name"]
    return any(value in names for value in cells)


def _header_columns(cells: list[str]) -> tuple[str | None, ...]:
    """把表头单元格翻译成字段名；不认识的列记 None，数据行里直接忽略。"""
    mapping: list[str | None] = []
    for value in cells:
        mapping.append(_field_for_header(value))
    return tuple(mapping)


def _field_for_header(value: str) -> str | None:
    for field, aliases in CSV_COLUMNS.items():
        if value in aliases:
            return field
    return None


def _pick(cells: list[str], columns: tuple[str | None, ...]) -> dict[str, str]:
    """按列映射取本行的字段值；同一字段出现多次时以最左一列为准。"""
    values: dict[str, str] = {}
    for index, field in enumerate(columns):
        if field is None or field in values or index >= len(cells):
            continue
        values[field] = cells[index]
    return values
