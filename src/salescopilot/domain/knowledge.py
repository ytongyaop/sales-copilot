"""资料切块与引用校验（纯函数，无 IO、不抛异常、不碰向量库）。

切块的目的只有两个：让检索粒度足够小（一段话一个块），同时每个块都能**指回原文**。
所以每块都带 `(start, end)` 相对原文的偏移，`text` 必须逐字等于 `原文[start:end]`；
引用能不能信，由 `is_citation_valid` 当场验，而不是靠模型自觉。

排法（三步）：
1. 先按 Markdown 标题切段（`#` 到 `######` 开头的行）；
2. 段内再按空行切块；**标题行不单独成块**——它是块的起始标记，和紧随其后的正文合并，
   块文本里保留标题（例如「`## 一、章节名`」+ 正文），检索时标题与内容才不会互相脱节；
3. 单块仍超过 `max_chars` 时，优先在换行处断开（一行内容不切在中间），其次句末标点；
   超过 `overlap` 时把上一块末尾的若干字符带进下一块，避免一句话被切两半后两块都读不懂。
   列表项被切开时，块首尾悬空的纯符号（`-` / `*` 之类）一律回推掉，不留下孤立标记。
"""

import re
from dataclasses import dataclass

# 句末标点：切超长块时只在这些位置断开（宁可块短一点，也不要在词中间切）。
_SENTENCE_ENDS = "。！？!?；;"
# 标题行：最多六级；`\S` 保证「#空格#」这种不是标题。
_HEADING = re.compile(r"^#{1,6}\s+\S")
# 块首尾可能残留的「纯符号」：列表项被切开后留下的孤立标记。
_EDGE_SYMBOLS = "-*+·•◦"
# 列表标记只认行首的「- 」/「* 」（后面必须跟空白，避免把「**加粗**」的星号当标记）。
_LIST_MARKS = "-*"

# 索引清单的键：写在 domain，适配器与用例层各读各写同一份约定，避免格式漂移。
MANIFEST_KEYS: tuple[str, ...] = ("embed_model", "chunk_count", "built_at")


@dataclass(frozen=True)
class Chunk:
    """一个资料块：`text` 逐字来自原文 `[start, end)`，`source` 是它属于哪份资料。"""

    id: str
    source: str
    text: str
    start: int
    end: int


def chunk_markdown(text: str, *, max_chars: int = 500, overlap: int = 0) -> list[Chunk]:
    """把一份 Markdown 切成带偏移的块；空块与纯空白块一律丢弃。

    `source` 留空（切块不认识文件系统），由调用方（用例层）填上资料名。
    `id` 只在同一份资料内唯一，拍成 `<start>-<end>`；跨资料重名由调用方加前缀。
    """
    if not isinstance(text, str) or not text:
        return []

    limit = max(1, int(max_chars))
    span = max(0, int(overlap))

    chunks: list[Chunk] = []
    for sec_start, sec_end in _sections(text):
        for block_start, block_end in _blocks(text, sec_start, sec_end):
            trimmed = _trim(text, block_start, block_end)
            if trimmed is None:
                continue
            t_start, t_end = trimmed
            if t_end - t_start <= limit:
                chunks.append(_chunk(text, t_start, t_end))
                continue
            for piece_start, piece_end in _pack(text, t_start, t_end, limit, span):
                chunks.append(_chunk(text, piece_start, piece_end))
    return chunks


def is_citation_valid(chunk: Chunk, original: str) -> bool:
    """这条引用是否真的出自原文：偏移对得上、文本在原文里、去空白后非空。"""
    if not isinstance(original, str) or not chunk.text.strip():
        return False
    if chunk.start < 0 or chunk.end > len(original) or chunk.start >= chunk.end:
        return False
    return original[chunk.start : chunk.end] == chunk.text and chunk.text in original


def _sections(text: str) -> list[tuple[int, int]]:
    """按标题行切段；第一个标题之前的内容也算一段（可能是引言）。"""
    starts = [0]
    offset = 0
    for line in text.splitlines(keepends=True):
        if _HEADING.match(line) and offset != starts[-1]:
            starts.append(offset)
        offset += len(line)

    return [
        (start, starts[index + 1] if index + 1 < len(starts) else len(text))
        for index, start in enumerate(starts)
    ]


def _blocks(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """段内按空行切块：连续的非空行算一块，空行是分隔符（不进块）。

    切完再把「只有一行标题」的块并进它后面的正文：标题单独成块时既没有信息量，
    又会让正文失去归属，检索出来的引用像半句话。
    """
    blocks: list[tuple[int, int]] = []
    offset = start
    block_start: int | None = None

    for line in text[start:end].splitlines(keepends=True):
        line_len = len(line)
        is_blank = not line.strip()
        if is_blank:
            if block_start is not None:
                blocks.append((block_start, offset))
                block_start = None
        elif block_start is None:
            block_start = offset
        offset += line_len

    if block_start is not None:
        blocks.append((block_start, end))
    return _merge_titles(blocks, text)


def _merge_titles(blocks: list[tuple[int, int]], text: str) -> list[tuple[int, int]]:
    """把「单行标题」块与其后一块合并（合并区间中间的空白原样保留，偏移仍指向原文）。"""
    merged: list[tuple[int, int]] = []
    index = 0
    while index < len(blocks):
        block_start, block_end = blocks[index]
        following = blocks[index + 1] if index + 1 < len(blocks) else None
        if following is not None and _is_title_line(text, block_start, block_end):
            merged.append((block_start, following[1]))
            index += 2
            continue
        merged.append((block_start, block_end))
        index += 1
    return merged


def _is_title_line(text: str, start: int, end: int) -> bool:
    """这个块是不是「只有一行 Markdown 标题」；是的话它应该当下一块的起始标记。"""
    span = text[start:end].rstrip()
    return "\n" not in span and bool(_HEADING.match(span))


def _trim(text: str, start: int, end: int) -> tuple[int, int] | None:
    """去掉首尾空白与悬空的纯符号残片并回推偏移；去完为空返回 None（这种块直接丢）。

    只回推偏移、不改文本，所以 `text` 仍逐字等于 `原文[start:end]`，引用照样能回溯。
    """
    start = _skip_leading(text, start, end)
    end = _skip_trailing(text, start, end)
    if start >= end:
        return None
    return start, end


def _skip_leading(text: str, start: int, end: int) -> int:
    while start < end and text[start].isspace():
        start += 1
    # 列表项被切开后，块首可能只剩「- 」/「* 」这种孤立标记：连同标记一起去掉。
    if start + 1 < end and text[start] in _LIST_MARKS and text[start + 1].isspace():
        start += 1
        while start < end and text[start].isspace():
            start += 1
    return start


def _skip_trailing(text: str, start: int, end: int) -> int:
    while end > start and text[end - 1].isspace():
        end -= 1
    # 行尾悬空的纯符号（最多两个）不是内容：砍掉后引用片段读起来仍是完整的一句。
    cut = end
    while cut > start and end - cut < 2 and text[cut - 1] in _EDGE_SYMBOLS:
        cut -= 1
    return cut


def _pack(text: str, start: int, end: int, limit: int, overlap: int) -> list[tuple[int, int]]:
    """把一块超长文本打包成若干 ≤ limit 的片段（优先按换行断，其次句末标点）。"""
    pieces: list[tuple[int, int]] = []
    pos = start
    while pos < end:
        piece_end = min(pos + limit, end)
        if piece_end < end:
            cut = _cut_point(text, pos, piece_end)
            if cut > pos:
                piece_end = cut
        trimmed = _trim(text, pos, piece_end)
        if trimmed is not None:
            pieces.append(trimmed)
        if piece_end >= end:
            break
        pos = max(piece_end - overlap, pos + 1)
    return pieces


def _cut_point(text: str, start: int, limit_end: int) -> int:
    """这一段该断在哪：优先最后一个换行（不切在一行中间），其次句末标点；都没有就不切。"""
    newline = text.rfind("\n", start, limit_end)
    if newline > start:
        return newline + 1
    return _last_sentence_end(text, start, limit_end)


def _last_sentence_end(text: str, start: int, limit_end: int) -> int:
    """在 `(start, limit_end]` 里找最后一个句末标点，返回它后一位；没有就返回 start。"""
    best = start
    for index in range(start, limit_end):
        if text[index] in _SENTENCE_ENDS:
            best = index + 1
    return best


def _chunk(text: str, start: int, end: int) -> Chunk:
    return Chunk(id=f"{start}-{end}", source="", text=text[start:end], start=start, end=end)
