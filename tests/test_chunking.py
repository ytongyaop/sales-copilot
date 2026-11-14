"""切块规则的行为测试：偏移、超长切分、标题与正文合并、列表残片、空块丢弃。

切块的唯一价值是「每块都能指回原文」，所以每条断言都以
`original[start:end] == chunk.text` 为底线，不只看块数。
"""

import itertools

from salescopilot.domain.knowledge import Chunk, chunk_markdown, is_citation_valid

LONG_BLOCK = "这是一句用来测试超长切分的普通话。" * 40  # 约 640 字，超过默认 500


def test_heading_starts_block_and_merges_with_body() -> None:
    """标题不单独成块：它作为块的起始标记，与其后的正文合并在同一块里。"""
    text = "# 第一节\n\n甲段落内容。\n\n# 第二节\n\n乙段落内容。\n"
    chunks = chunk_markdown(text)

    assert [chunk.text for chunk in chunks] == [
        "# 第一节\n\n甲段落内容。",
        "# 第二节\n\n乙段落内容。",
    ]
    # 每块都以标题开头、并带着标题下的正文（标题单独成块时这里会失败）。
    for chunk in chunks:
        assert chunk.text.startswith("# ")
        assert "段落内容" in chunk.text
        assert text[chunk.start : chunk.end] == chunk.text
    # 没有任何一块同时跨两个标题：跨了就说明标题切段没生效。
    for chunk in chunks:
        assert not ("# 第一节" in chunk.text and "# 第二节" in chunk.text)


def test_list_items_leave_no_dangling_markers() -> None:
    """列表项被切开时不能留下孤立的「-」或「*」：块首尾必须是真实内容。"""
    text = "\n".join(
        f"- 第{index}项内容，这一条写得比较长，用来让切分点落在列表项之间。"
        for index in range(1, 12)
    )
    chunks = chunk_markdown(text, max_chars=40)

    assert len(chunks) > 1, "这份列表必须真的被切开，否则测不到边界"
    for chunk in chunks:
        assert text[chunk.start : chunk.end] == chunk.text
        assert is_citation_valid(chunk, text)
        assert chunk.text.strip() == chunk.text
        assert chunk.text not in {"-", "*", "- -", "* *"}
        assert not chunk.text.endswith(("-", "*"))
        assert not chunk.text.startswith(("- ", "* "))


def test_blank_lines_split_blocks() -> None:
    text = "第一段内容。\n\n第二段内容。"
    chunks = chunk_markdown(text)

    assert [chunk.text for chunk in chunks] == ["第一段内容。", "第二段内容。"]


def test_offsets_match_original_exactly() -> None:
    text = "# 标题\n\n一段正文，带标点。\n\n- 列表项一\n- 列表项二\n"
    chunks = chunk_markdown(text, max_chars=20)

    assert chunks
    for chunk in chunks:
        assert text[chunk.start : chunk.end] == chunk.text
        assert chunk.text.strip() == chunk.text
        assert is_citation_valid(chunk, text)


def test_long_block_is_split_with_sentence_boundaries() -> None:
    text = f"# 长文\n\n{LONG_BLOCK}"
    chunks = chunk_markdown(text, max_chars=100)

    body = [chunk for chunk in chunks if chunk.text.startswith("这是")]
    assert body, "超长段落必须被切成多块"
    for chunk in body:
        assert len(chunk.text) <= 100
        assert chunk.text.endswith("。"), "切分应落在句末，不该切在句子中间"
        assert is_citation_valid(chunk, text)


def test_overlap_repeats_tail_of_previous_chunk() -> None:
    text = LONG_BLOCK
    chunks = chunk_markdown(text, max_chars=100, overlap=10)

    assert len(chunks) >= 2
    for previous, following in itertools.pairwise(chunks):
        assert following.start < previous.end, "开重叠时应能从尾部和上一块咬合"
        assert following.text[:10] == text[following.start : following.start + 10]


def test_empty_blocks_are_dropped() -> None:
    text = "\n\n\n   \n\n# 标题\n\n\n\n正文。\n\n   \n"
    chunks = chunk_markdown(text)

    # 标题与其后正文合并成一块，中间的空白原样保留；只有空白行形不成块。
    assert len(chunks) == 1
    assert chunks[0].text.startswith("# 标题")
    assert chunks[0].text.endswith("正文。")
    assert all(chunk.text.strip() for chunk in chunks)


def test_non_markdown_text_still_chunks() -> None:
    chunks = chunk_markdown("A" * 30, max_chars=10)

    assert len(chunks) == 3
    assert "".join(chunk.text for chunk in chunks) == "A" * 30


def test_is_citation_valid_rejects_tampered_chunk() -> None:
    original = "客户关心价格与上课时间。"
    good = Chunk(id="0-6", source="s", text="客户关心价格", start=0, end=6)

    assert is_citation_valid(good, original)
    assert not is_citation_valid(Chunk("x", "s", "不在原文里的话", 0, 7), original)
    assert not is_citation_valid(Chunk("x", "s", "", 0, 1), original)
    assert not is_citation_valid(Chunk("x", "s", "客户", 0, 99), original)


def test_empty_input_returns_empty_list() -> None:
    assert chunk_markdown("") == []
    assert chunk_markdown("   \n\n  ") == []
