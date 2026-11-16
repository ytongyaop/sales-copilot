"""CSV 解析的纯函数测试：有/无表头、中文来源别名、缺字段跳过、空行、BOM、超大文本。

全程无 IO，直接给字符串；解析只判断「这一行长什么样」，不碰库。
"""

from salescopilot.domain.csv_import import (
    MAX_BYTES,
    ParsedRow,
    RowIssue,
    parse_csv,
)


def test_header_csv_maps_columns_and_skips_blank_lines() -> None:
    text = "姓名,联系方式,来源,归属\n张三,13800001111,转介绍,王五\n\n李四,13800002222,活动,\n"

    rows, issues = parse_csv(text)

    assert issues == []
    assert rows == [
        ParsedRow(row_no=2, name="张三", contact="13800001111", source="referral", owner="王五"),
        ParsedRow(row_no=4, name="李四", contact="13800002222", source="event", owner=None),
    ]


def test_headerless_csv_uses_fixed_column_order() -> None:
    text = "赵六,13900001111,inbound,李四\n钱七,13900002222,主动咨询,\n"

    rows, issues = parse_csv(text)

    assert issues == []
    assert [row.row_no for row in rows] == [1, 2]
    assert rows[0] == ParsedRow(
        row_no=1, name="赵六", contact="13900001111", source="inbound", owner="李四"
    )
    assert rows[1].source == "inbound"
    assert rows[1].owner is None


def test_chinese_source_aliases_are_translated() -> None:
    aliases = {
        "转介绍": "referral",
        "主动咨询": "inbound",
        "主动触达": "outbound",
        "活动": "event",
        "其他": "other",
    }
    lines = "姓名,联系方式,来源\n" + "".join(
        f"线索{index},{13800000000 + index},{alias}\n" for index, alias in enumerate(aliases, 1)
    )

    rows, issues = parse_csv(lines)

    assert issues == []
    assert [row.source for row in rows] == list(aliases.values())


def test_missing_fields_are_reported_and_batch_continues() -> None:
    text = "姓名,联系方式,来源\n,13800001111,inbound\n李四,,inbound\n王五,13800003333,活动\n"

    rows, issues = parse_csv(text)

    assert rows == [
        ParsedRow(row_no=4, name="王五", contact="13800003333", source="event", owner=None)
    ]
    assert [issue.row_no for issue in issues] == [2, 3]
    assert [issue.message for issue in issues] == ["缺少姓名", "缺少联系方式"]


def test_invalid_source_is_reported_and_row_is_skipped() -> None:
    text = "姓名,联系方式,来源\n张三,13800001111,朋友推荐\n"

    rows, issues = parse_csv(text)

    assert rows == []
    assert issues == [RowIssue(row_no=2, message="来源不合法：朋友推荐")]


def test_bom_is_stripped_before_header_detection() -> None:
    text = "\ufeff姓名,联系方式,来源\n张三,13800001111,转介绍\n"

    rows, issues = parse_csv(text)

    assert issues == []
    assert rows == [
        ParsedRow(row_no=2, name="张三", contact="13800001111", source="referral", owner=None)
    ]


def test_oversized_text_is_rejected_as_a_whole() -> None:
    text = "a" * (MAX_BYTES + 1)

    rows, issues = parse_csv(text)

    assert rows == []
    assert issues == [RowIssue(row_no=0, message="文件过大")]
