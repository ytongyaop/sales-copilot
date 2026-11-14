"""话术草稿的纯规则：禁令词检查、引用整理、落库前三要素校验（无 IO）。

模型写出来的是「草稿」，不是「已发送的话」。所以这里只做两件事：
1. 禁用词命中就拦下来（承诺效果、承诺价格、百分百这类说法，写出来就是风险）；
2. 引用统一形状，前端不用猜字段名。
"""

from salescopilot.domain.models import Fact, Lead
from salescopilot.domain.rules import ValidationIssue, validate_followup

# 明令不许出现在草稿里的说法：都是「替客户做决定」或「替机构兜底」的承诺。
BANNED_PHRASES: tuple[str, ...] = (
    "保过",
    "保证提分",
    "保offer",
    "最低价",
    "包退",
    "百分百",
)

# 引用列表里每一项只留这三列：哪份资料、哪段原文、相似度多少。
CITATION_KEYS = ("source", "text", "score")


def check_banned(text: str) -> list[ValidationIssue]:
    """检查草稿里的禁用词；命中几个就报几条，位置统一写 `draft.text`。"""
    content = text if isinstance(text, str) else ""
    issues: list[ValidationIssue] = []
    for phrase in BANNED_PHRASES:
        if phrase in content:
            issues.append(ValidationIssue("draft.text", f"草稿包含禁用词「{phrase}」"))
    return issues


def build_citations_payload(hits: list[dict]) -> list[dict]:
    """把检索结果压成统一的引用形状：`{source, text, score}`。

    缺字段的命中直接跳过（没有出处的引用等于没有引用）；`score` 保留四位小数，
    只用于展示可信度，不参与任何判定。
    """
    citations: list[dict] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        text = hit.get("text")
        source = hit.get("source")
        if not isinstance(text, str) or not text.strip():
            continue
        score = hit.get("score", 0.0)
        citations.append(
            {
                "source": source if isinstance(source, str) else "",
                "text": text,
                "score": round(float(score), 4) if isinstance(score, (int, float)) else 0.0,
            }
        )
    return citations


def build_query(lead: Lead, facts: list[Fact], *, max_facts: int = 8) -> str:
    """用事实的关键值拼检索语句：`field=value` 逐行，最多取 `max_facts` 条。

    只取事实，不取线索名称/联系方式——查询词越像「客户在关心什么」，召回越准；
    拼法固定，同样的输入永远同样的查询，方便复现一次生成。
    """
    lines: list[str] = []
    for fact in facts:
        field = fact.field.strip() if isinstance(fact.field, str) else ""
        value = fact.value.strip() if isinstance(fact.value, str) else ""
        if field and value:
            lines.append(f"{field}={value}")
        if len(lines) >= max_facts:
            break
    query = "\n".join(lines)
    return query or (lead.name or "")


def validate_confirm(
    *, objection: str, next_followup_at: str, conclusion: str | None = None
) -> list[ValidationIssue]:
    """确认草稿时的三要素校验：异议在枚举内、日期是 `YYYY-MM-DD`。

    `conclusion` 由用例层传入（通常就是草稿正文）；不传时只校验后两项，
    因为「草稿能不能落库」是生成阶段的事，确认阶段只补人工填的那两项。
    """
    if conclusion is None:
        return _validate_objection_and_date(objection, next_followup_at)
    return validate_followup(
        conclusion=conclusion, objection=objection, next_followup_at=next_followup_at
    )


def _validate_objection_and_date(objection: str, next_followup_at: str) -> list[ValidationIssue]:
    issues = validate_followup(
        conclusion="草稿确认", objection=objection, next_followup_at=next_followup_at
    )
    return [issue for issue in issues if issue.location in ("objection", "next_followup_at")]


__all__ = (
    "BANNED_PHRASES",
    "CITATION_KEYS",
    "build_citations_payload",
    "build_query",
    "check_banned",
    "validate_confirm",
)
