"""资料切块与引用校验（纯函数，无 IO、不抛异常、不碰向量库）。

切块的目的只有两个：让检索粒度足够小（一段话一个块），同时每个块都能**指回原文**。
所以每块都带 `(start, end)` 相对原文的偏移，`text` 必须逐字等于 `原文[start:end]`；
引用能不能信，由 `is_citation_valid` 当场验，而不是靠模型自觉。

排法（三步）：
1. 先按 Markdown 标题切段（`#` 到 `######` 开头的行）；
2. 段内再按空行切块；
3. 单块仍超过 `max_chars` 时，按句号/问号/感叹号/分号/换行切，超过 `overlap`
   时把上一块末尾的若干字符带进下一块，避免一句话被切两半后两个块都读不懂。
"""

import re
from dataclasses import dataclass

# 句末标点：切超长块时只在这些位置断开（宁可块短一点，也不要在词中间切）。
_SENTENCE_ENDS = "。！？!?；;\n"
# 标题行：最多六级；`\S` 保证「#空格#」这种不是标题。
_HEADING = re.compile(r"^#{1,6}\s+\S")

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
    """段内按空行切块：连续的非空行算一块，空行是分隔符（不进块）。"""
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
    return blocks


def _trim(text: str, start: int, end: int) -> tuple[int, int] | None:
    """去掉首尾空白并回推偏移；去完为空返回 None（这种块直接丢）。"""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start >= end:
        return None
    return start, end


def _pack(text: str, start: int, end: int, limit: int, overlap: int) -> list[tuple[int, int]]:
    """把一块超长文本按句末标点打包成若干 ≤ limit 的片段。"""
    pieces: list[tuple[int, int]] = []
    pos = start
    while pos < end:
        piece_end = min(pos + limit, end)
        if piece_end < end:
            cut = _last_sentence_end(text, pos, piece_end)
            if cut > pos:
                piece_end = cut
        trimmed = _trim(text, pos, piece_end)
        if trimmed is not None:
            pieces.append(trimmed)
        if piece_end >= end:
            break
        pos = max(piece_end - overlap, pos + 1)
    return pieces


def _last_sentence_end(text: str, start: int, limit_end: int) -> int:
    """在 `(start, limit_end]` 里找最后一个句末标点，返回它后一位；没有就返回 start。"""
    best = start
    for index in range(start, limit_end):
        if text[index] in _SENTENCE_ENDS:
            best = index + 1
    return best


def _chunk(text: str, start: int, end: int) -> Chunk:
    return Chunk(id=f"{start}-{end}", source="", text=text[start:end], start=start, end=end)
