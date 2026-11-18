"""话术草稿的纯规则：日期检查、禁令词检查、引用整理、落库前三要素校验（无 IO）。

模型写出来的是「草稿」，不是「已发送的话」。所以这里只做三件事：
1. 草稿里的**日期**要站得住：不能出现早于今天的日子，也不能与客户已约定时间对不上
   （编一个过去的日子约客户，是最容易当场丢客户的一种错）；
2. 禁用词命中就拦下来（承诺效果、承诺价格、百分百这类说法，写出来就是风险）；
3. 引用统一形状，前端不用猜字段名。

纪律：今天由调用方传入（`today`），本模块不读时钟、不查库。
"""

import re
from dataclasses import dataclass
from datetime import date

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

# 草稿里算数的日期写法：`2026-11-25` 与 `11月25日` / `11月25号`。
# 只认这两种「确定的日期」：「下周」「月底」这类说法不可核，不参与判定。
_ISO_DATE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")
_CN_DATE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")

# 开放式提问的措辞：日期所在的那一小句里有这些说法时，是在问客户「哪天方便」，不判违规。
OPEN_QUESTION_HINTS: tuple[str, ...] = ("哪天", "什么时候", "什么时间", "几号", "何时")

# 只说「几月几日」时按今天所在年份算；算出来比今天早半年以上，就当成明年的那一次
# （11 月里说「1 月 5 日」通常指明年 1 月，不该被当成十个月前的日子拦下来）。
YEAR_ROLLOVER_DAYS = 180

# 分句用的标点：判断「这句话是不是开放式提问」只在同一小句里找措辞。
_CLAUSE_SPLIT = re.compile(r"[，,。；;！!？?\n]")

# 日期问题挂在草稿正文上（与禁用词同一位置，前端只认这一处）。
DATE_ISSUE_LOCATION = "draft.text"


@dataclass(frozen=True)
class DraftDate:
    """草稿里找到的一个日期：原文写法（`11月15日`）与解析后的日子。"""

    raw: str
    value: date


def find_dates(text: str, *, today: date) -> list[DraftDate]:
    """抽出草稿里的确定日期，按出现顺序返回；同一个写法只留第一次（报一次就够）。

    只认 `YYYY-MM-DD` 与 `M月D日 / M月D号`：这两种能落到具体某一天，才谈得上「早于今天」
    或「与约定不一致」。相对说法（下周 / 月底）本来就没有确定日期，强判只会误伤。
    """
    content = text if isinstance(text, str) else ""
    if not content:
        return []

    found: list[tuple[int, DraftDate]] = []
    for match in _ISO_DATE.finditer(content):
        value = _parse_iso(match.group(0))
        if value is not None:
            found.append((match.start(), DraftDate(match.group(0), value)))
    for match in _CN_DATE.finditer(content):
        day = _from_month_day(match, today=today)
        if day is not None:
            found.append((match.start(), DraftDate(match.group(0), day)))
    found.sort(key=lambda item: item[0])

    dates: list[DraftDate] = []
    seen: set[str] = set()
    for _start, item in found:
        if item.raw in seen:
            continue
        seen.add(item.raw)
        dates.append(item)
    return dates


def check_dates(
    text: str, *, today: date, appointment_at: str | None = None
) -> list[ValidationIssue]:
    """检查草稿里的日期站不站得住；返回问题列表（空列表表示可以落库）。

    三条判据，按顺序：
    1. 日期早于今天 → 违规（不许约一个已经过去的日子）；
    2. 客户已有约定时间（`appointment_at`，`YYYY-MM-DD`）而草稿写了别的日子 → 违规；
    3. 日期所在那一小句里有开放式提问措辞（「哪天」「几号」…）→ 放行：
       那是在问客户哪天方便，不是在替客户定日子。

    同一段草稿里同一个写法只报一次；`appointment_at` 不是合法日期时只按第 1 条判。
    """
    content = text if isinstance(text, str) else ""
    agreed = _parse_iso(appointment_at.strip()) if isinstance(appointment_at, str) else None
    agreed_text = agreed.isoformat() if agreed is not None else ""

    issues: list[ValidationIssue] = []
    for item in find_dates(content, today=today):
        if _is_open_question(content, item.raw):
            continue
        if item.value < today:
            issues.append(
                ValidationIssue(
                    DATE_ISSUE_LOCATION,
                    f"草稿里的日期「{item.raw}」早于今天（{today.isoformat()}），不能约已经过去的日子",
                )
            )
            continue
        if agreed is not None and item.value != agreed:
            issues.append(
                ValidationIssue(
                    DATE_ISSUE_LOCATION,
                    f"草稿里的日期「{item.raw}」与客户已约定时间（{agreed_text}）不一致",
                )
            )
    return issues


def _is_open_question(content: str, raw: str) -> bool:
    """这个日期所在的小句里有没有「哪天 / 几号」这类开放提问的措辞。"""
    for clause in _CLAUSE_SPLIT.split(content):
        if raw not in clause:
            continue
        if any(hint in clause for hint in OPEN_QUESTION_HINTS):
            return True
    return False


def _from_month_day(match: re.Match[str], *, today: date) -> date | None:
    """`11月25日` → 具体日期：按今天的年份算，早过半年就顺延到明年；日历上不存在返回 None。"""
    month, day = int(match.group(1)), int(match.group(2))
    try:
        value = date(today.year, month, day)
    except ValueError:
        return None
    if (today - value).days > YEAR_ROLLOVER_DAYS:
        try:
            value = date(today.year + 1, month, day)
        except ValueError:
            return None
    return value


def _parse_iso(text: str) -> date | None:
    """`YYYY-MM-DD` → 日期；形状不对或日历上不存在都返回 None。"""
    value = text.strip() if isinstance(text, str) else ""
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


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
    "DATE_ISSUE_LOCATION",
    "OPEN_QUESTION_HINTS",
    "DraftDate",
    "build_citations_payload",
    "build_query",
    "check_banned",
    "check_dates",
    "find_dates",
    "validate_confirm",
)
