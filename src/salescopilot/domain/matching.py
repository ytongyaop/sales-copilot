"""归属判定的三级漏斗（纯规则，无 IO）。

判定顺序固定：精确键（L0）→ 字符串相似度（L1，rapidfuzz）→ 向量相似度（L2）。
每一级只在上一级没给出结论时才看；结论只有三档 `auto` / `review` / `reject`。
三条硬约束写进规则本身，不靠调用方自觉：

- L1 的高分（≥ `fuzzy_auto`）必须有第二字段佐证才能 `auto`，否则只能进复核；
- 向量**永不单独授权**：L2 要 `auto` 必须同时有佐证，否则最高只能到 `review`；
- 两个全称只在组织形式后缀上不同（「有限公司」vs「集团」）时，即使字符串 100 分也只能进
  复核——那是不同法人主体，不是笔误（`suffix_only_difference`）。

本模块只做计算：入参都是已取好的值（命中标志、相似度、佐证标志），
不查库、不发网络请求、不取时钟；阈值由调用方按模板传入。
"""

import re
import unicodedata
from dataclasses import asdict, dataclass

from rapidfuzz import distance, fuzz

# 机构名后缀：按从长到短排列，先匹配长的，避免「有限公司」被「公司」先吃掉。
# 注意：「科技」「网络」这类词**不是**后缀——删掉会把两家不同公司判成同一家。
ORG_SUFFIXES = ("股份有限公司", "有限责任公司", "有限公司", "集团", "公司")

# 包裹型括号及其内容（全角、半角都认）；NFKC 已把全角转半角，这里两种都保留以防上层传原串。
_WRAPPED_BRACKETS = re.compile(r"[（(][^（()）]*[)）]")
_WHITESPACE = re.compile(r"\s+")

# 同分时的降级顺序：精确键命中最可信，其次字符串，最后向量。
SCORER_PRIORITY = {"exact": 0, "fuzzy": 1, "vector": 2}
REJECT_SCORER = "none"


@dataclass(frozen=True)
class MatchThresholds:
    """一档模板的判定阈值；字符串是 0-100，向量是余弦 0-1。"""

    fuzzy_auto: float = 95.0
    fuzzy_review: float = 85.0
    vector_auto: float = 0.92
    vector_review: float = 0.80


@dataclass(frozen=True)
class Candidate:
    """一个候选客户；`scorer` 说明这个分数是怎么来的。"""

    lead_id: str
    name: str
    score: float
    scorer: str  # "exact" | "fuzzy" | "vector"


@dataclass(frozen=True)
class MatchDecision:
    """一次判定的结论；`reason` 是写给人看的一句话，会原样进审计日志。"""

    band: str  # "auto" | "review" | "reject"
    scorer: str  # 命中的来源；reject 时用 "none"
    score: float
    reason: str


def _as_text(name: str) -> str:
    return name if isinstance(name, str) else ""


def _squeeze(name: str) -> str:
    """全角转半角并删掉全部空白（含首尾与内部）。"""
    return _WHITESPACE.sub("", unicodedata.normalize("NFKC", _as_text(name)))


def _strip_brackets(text: str) -> str:
    """删掉包裹型括号及其内容；嵌套的靠循环处理。"""
    while True:
        stripped = _WRAPPED_BRACKETS.sub("", text)
        if stripped == text:
            return stripped
        text = stripped


def _strip_org_suffix(text: str, suffixes: tuple[str, ...] = ORG_SUFFIXES) -> str:
    """反复剥掉结尾的组织后缀；剥到只剩后缀本身时停手（不产出空串）。"""
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if text.endswith(suffix) and len(text) > len(suffix):
                text = text[: -len(suffix)]
                changed = True
                break
    return text


def normalize_org(name: str, *, suffixes: tuple[str, ...] = ORG_SUFFIXES) -> str:
    """机构名规范化：全角转半角 → 去空白 → 去后缀 → 去括号内容 → 小写。

    「科技」「网络」等业务词按调研结论保留，只删组织形态后缀。
    """
    text = _strip_org_suffix(_squeeze(name), suffixes)
    return _strip_brackets(text).lower()


def format_key(name: str) -> str:
    """格式归一：全角转半角 → 去全部空白（含内部）→ 统一括号（NFKC 已把全角括号转半角）
    → 小写。

    只抹平「写法差异」，不抹平「叫法差异」：去后缀、去括号内容这些属于语义归一，
    留给 `normalize_org`；这里的输出专门用来判断「是不是同一个全称的不同写法」。
    """
    return _squeeze(name).lower()


def suffix_only_difference(
    raw_a: str, raw_b: str, *, suffixes: tuple[str, ...] = ORG_SUFFIXES
) -> bool:
    """两个全称是否**只**在组织形式后缀上不同（核心串相同、整串不同）。

    「华远科技有限公司」与「华远科技集团」核心串都是「华远科技」，但前者是有限公司、
    后者是集团，法律上是两个主体，不能自动合并，只能进复核。

    核心串与 `normalize_org` 同口径（连括号注记一起去掉）：否则「华远建设（北京）」
    与「华远建设（上海）」规范化后同为「华远建设」、字符串 100 分，会被自动合并成一家，
    而它们其实是在两地注册的两个主体。

    判据分两步，顺序不能换：
    1. 核心串相同（去后缀、去括号内容后相等）——不同就不是「仅组织形式不同」；
    2. **格式归一后**的整串仍不同（见 `format_key`：全角/半角、内部空白、大小写、
       括号写法都抹平）。所以「华远科技 有限公司」与「华远科技有限公司」只差一个
       空格，返回 `False`——那是同一家的两种写法，不是两个法人。

    核心串为空（名称本身只有后缀）时也返回 `False`，避免把两个空壳判成「仅组织形式
    不同」。
    """
    core_a = _strip_brackets(_strip_org_suffix(_squeeze(raw_a), suffixes)).lower()
    core_b = _strip_brackets(_strip_org_suffix(_squeeze(raw_b), suffixes)).lower()
    if not core_a or not core_b or core_a != core_b:
        return False
    return format_key(raw_a) != format_key(raw_b)


def normalize_person(name: str) -> str:
    """人名规范化：全角转半角 → 去空白 → 去括号内容（常是职位注记）→ 小写。"""
    return _strip_brackets(_squeeze(name)).lower()


def normalize_with_alias(name: str, aliases: dict[str, str]) -> str:
    """先查别名表再规范化：key 可以是原名，也可以是规范化后的写法。

    别名表命中就用别名值走 `normalize_org`；没命中就原样规范化，
    保证「华远」与「华远建设集团」在配置了别名时得到同一个键。
    """
    raw_key = _as_text(name).strip()
    target = aliases.get(raw_key)
    if target is None:
        target = aliases.get(normalize_org(raw_key), raw_key)
    return normalize_org(target)


def fuzzy_score(a: str, b: str) -> float:
    """字符串相似度（0-100）：token 排序比与 Jaro-Winkler 取高者。

    两个入参都应是规范化后的串；任一为空串直接返回 0.0。
    """
    if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
        return 0.0
    token_sort = fuzz.token_sort_ratio(a, b)
    jaro_winkler = distance.JaroWinkler.similarity(a, b) * 100.0
    return max(float(token_sort), float(jaro_winkler))


def decide_band(
    *,
    exact_hit: bool,
    fuzzy: float,
    vector: float | None,
    corroborated: bool,
    thresholds: MatchThresholds,
    suffix_only: bool = False,
) -> MatchDecision:
    """按固定顺序过漏斗，返回三档结论。

    顺序（不能变）：精确键 → 仅组织形式不同（强制复核）→ 字符串自动
    → 字符串高分缺佐证 → 字符串复核区 → 向量自动（须佐证）→ 向量复核区 → 判为 reject。
    `score` 保留各自原始量纲：精确键 100.0、字符串 0-100、向量 0-1。

    `suffix_only=True` 时跳过所有 `auto` 分支（精确键除外）：两个全称只在
    「有限公司 / 集团」这类后缀上不同，法律上是不同主体，只能由人复核。
    """
    if exact_hit:
        return MatchDecision(
            band="auto",
            scorer="exact",
            score=100.0,
            reason="精确键命中（手机号/邮箱/信用代码/全称）",
        )

    if suffix_only:
        return MatchDecision(
            band="review",
            scorer="fuzzy",
            score=fuzzy,
            reason="全称仅组织形式不同（如「有限公司」与「集团」），不能自动合并",
        )

    if fuzzy >= thresholds.fuzzy_auto:
        if corroborated:
            return MatchDecision(
                band="auto",
                scorer="fuzzy",
                score=fuzzy,
                reason=(
                    f"字符串相似度 {fuzzy:.1f} ≥ {thresholds.fuzzy_auto:.0f}"
                    " 且有第二字段佐证，自动归属"
                ),
            )
        return MatchDecision(
            band="review",
            scorer="fuzzy",
            score=fuzzy,
            reason=(
                f"字符串相似度 {fuzzy:.1f} ≥ {thresholds.fuzzy_auto:.0f}"
                " 但缺第二字段佐证，高分也只能进复核"
            ),
        )

    if fuzzy >= thresholds.fuzzy_review:
        return MatchDecision(
            band="review",
            scorer="fuzzy",
            score=fuzzy,
            reason=(
                f"字符串 {fuzzy:.1f} 落在 "
                f"{thresholds.fuzzy_review:.0f}-{thresholds.fuzzy_auto:.0f} 复核区"
            ),
        )

    if vector is not None and vector >= thresholds.vector_auto:
        if corroborated:
            return MatchDecision(
                band="auto",
                scorer="vector",
                score=vector,
                reason=(
                    f"向量相似度 {vector:.3f} ≥ {thresholds.vector_auto:.2f}"
                    " 且有第二字段佐证，自动归属"
                ),
            )
        return MatchDecision(
            band="review",
            scorer="vector",
            score=vector,
            reason=(
                f"向量相似度 {vector:.3f} ≥ {thresholds.vector_auto:.2f}"
                " 但缺第二字段佐证，向量不得单独自动归属"
            ),
        )

    if vector is not None and vector >= thresholds.vector_review:
        return MatchDecision(
            band="review",
            scorer="vector",
            score=vector,
            reason=(
                f"向量 {vector:.3f} 落在 "
                f"{thresholds.vector_review:.2f}-{thresholds.vector_auto:.2f} 复核区"
            ),
        )

    best = max(fuzzy, vector * 100.0 if vector is not None else 0.0)
    return MatchDecision(
        band="reject",
        scorer=REJECT_SCORER,
        score=best,
        reason="没有任何候选达到下限，视为新客户",
    )


def rank_candidates(candidates: list[Candidate], *, limit: int = 3) -> list[Candidate]:
    """按分数降序取前 `limit` 个；同分按 exact > fuzzy > vector，再同分保持入参顺序。"""

    def sort_key(candidate: Candidate) -> tuple[float, int]:
        return (-candidate.score, SCORER_PRIORITY.get(candidate.scorer, len(SCORER_PRIORITY)))

    if limit <= 0:
        return []
    return sorted(candidates, key=sort_key)[:limit]


def audit_record(
    decision: MatchDecision,
    candidates: list[Candidate],
    *,
    thresholds: MatchThresholds | None = None,
) -> dict:
    """拼一份可直接写进 `sorting_batches.suggestion_json` 的审计记录。

    不传 `thresholds` 就记默认档，但调用方应按模板实际阈值传入，便于日后复盘阈值变更。
    """
    return {
        "decision": asdict(decision),
        "candidates": [asdict(candidate) for candidate in candidates],
        "thresholds": asdict(thresholds or MatchThresholds()),
    }
