"""提炼评测的纯指标：只吃数据、不碰 IO、不打日志（脚本与测试共用同一份口径）。

口径（要改口径先改这里，别在评测脚本里另写一份）：

- **命中按「字段值对」算**：`field` 相同且取值相同才算一条命中。
  `objection` 必须枚举值相等（多一个字就是不命中）；其余字段去掉所有空白后相等即算
  （「1.5万」与「1.5 万」、「2000 元」与「2000元」按同一条）。
- **precision** = 命中数 / 模型给出的条数；**recall** = 命中数 / 期望条数。
  任一分母为 0 时记 `0.0`（不是 1.0）——什么都不做不能拿满分。
- **span_valid_rate** = 采纳条目里 `source_span` 逐字出现在原文的比例；没有采纳条目记 `0.0`。

这些数字用来建立基线、看漂移；它们**不判断客户是否成交**，也不进入业务流程。
"""

from collections.abc import Iterable

OBJECTION_FIELD = "objection"


def normalize_value(field: str, value: str) -> str:
    """把取值归一成可比较的字符串：异议按枚举精确比对，其余去掉所有空白。"""
    text = value if isinstance(value, str) else ""
    if field == OBJECTION_FIELD:
        return text.strip()
    return "".join(text.split())


def pair_of(item: dict) -> tuple[str, str] | None:
    """把一条候选压成（字段, 归一后的取值）；字段为空或不是映射时返回 None。"""
    if not isinstance(item, dict):
        return None
    field = item.get("field")
    if not isinstance(field, str) or not field.strip():
        return None
    field_text = field.strip()
    return field_text, normalize_value(field_text, item.get("value"))


def pairs_of(items: Iterable[dict]) -> set[tuple[str, str]]:
    """把一批候选压成去重后的（字段, 取值）集合。"""
    pairs = {pair for pair in (pair_of(item) for item in items) if pair is not None}
    return pairs


def match_count(expected: Iterable[dict], predicted: Iterable[dict]) -> int:
    """期望与预测的交集大小（同一字段值对只算一次）。"""
    return len(pairs_of(expected) & pairs_of(predicted))


def precision_recall_counts(*, matched: int, predicted: int, expected: int) -> tuple[float, float]:
    """按条数算（precision, recall）；分母为 0 记 0.0。"""
    precision = 0.0 if predicted <= 0 else min(matched, predicted) / predicted
    recall = 0.0 if expected <= 0 else min(matched, expected) / expected
    return precision, recall


def field_precision(expected: list[dict], predicted: list[dict]) -> float:
    """模型给出的条目里有多少是对的（多了就掉分）。"""
    precision, _ = precision_recall_counts(
        matched=match_count(expected, predicted),
        predicted=len(pairs_of(predicted)),
        expected=len(pairs_of(expected)),
    )
    return precision


def field_recall(expected: list[dict], predicted: list[dict]) -> float:
    """该抽出来的条目里抽到了多少（漏了就掉分）。"""
    _, recall = precision_recall_counts(
        matched=match_count(expected, predicted),
        predicted=len(pairs_of(predicted)),
        expected=len(pairs_of(expected)),
    )
    return recall


def span_valid(text: str, span: str) -> bool:
    """片段是否逐字出现在原文里；空片段一律算无效（空串在 Python 里能被 `in` 命中）。"""
    if not isinstance(span, str) or not span.strip():
        return False
    if not isinstance(text, str) or not text:
        return False
    return span in text


def span_valid_rate(facts: Iterable[dict], content: str) -> float:
    """采纳条目里片段可回查的比例；一条都没有时记 0.0。"""
    items = list(facts)
    if not items:
        return 0.0
    valid = sum(1 for item in items if span_valid(content, item.get("source_span")))
    return valid / len(items)
