"""出处对齐的判据测试（纯规则，不联网、不落库）。

四条判据各一条用例：精确命中、模糊命中（错一个字的量级）、完全不命中、
短片段不接受。每条都对应 `domain/sorting.py` 的 `align_span` 的一个分支。
"""

from salescopilot.domain.sorting import MIN_SPAN_LEN, align_span

RAW = "华远建设有限公司的张老板说预算三十万，下周一下午再聊"


def test_exact_substring_returns_offsets() -> None:
    """精确子串命中：返回原始下标，切片就是那句话本身。"""
    aligned = align_span(RAW, "预算三十万")

    assert aligned is not None
    assert RAW[aligned[0] : aligned[1]] == "预算三十万"
    assert aligned == (RAW.find("预算三十万"), RAW.find("预算三十万") + len("预算三十万"))


def test_fuzzy_alignment_accepts_near_miss() -> None:
    """模型多写一个字（93 分）仍能对齐回原文那一段，返回的窗口落在原文里。"""
    aligned = align_span(RAW, "下周一下午再聊聊")

    assert aligned is not None
    start, end = aligned
    assert 0 <= start < end <= len(RAW)
    assert RAW[start:end] == "下周一下午再聊"


def test_unrelated_quote_returns_none() -> None:
    """原文里没有的话一律判无出处——编造的片段不该被模糊对齐「凑」进来。"""
    assert align_span(RAW, "客户说下个月总部一定会批") is None


def test_short_quote_is_rejected_even_if_exact() -> None:
    """短片段不接受：一两个字的串到处都是，精确命中也不能当出处。"""
    assert MIN_SPAN_LEN == 4
    assert "下周" in RAW
    assert align_span(RAW, "下周") is None
    assert align_span(RAW, "  ") is None
    assert align_span("", "预算三十万") is None
