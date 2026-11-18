"""话术生成用的提示词：版本号 + system 约束 + user 拼装。

纪律（和提炼提示词一样）：
- 提示词是**数据**：它声明「模型该输出什么形状」，但「能不能落库」由 `services/draft.py` 判；
- `DRAFT_PROMPT_VERSION` 会写进草稿记录，改一个字就是一次新版本，别偷偷改；
- 模型必须带资料编号回来（`used`），否则用例层无法判断引用是否为空，会直接拒绝；
- **今天日期与客户已约定时间由调用方传入**：模型不知道今天是几号，就会写出「11月15日」
  这种已经过去的日子。本模块不读时钟，只负责把这两个值讲进提示词。
"""

from datetime import date

# v2：加入「今天日期（含星期）」与「客户已约定时间」，「不许编日期」写成硬约束。
DRAFT_PROMPT_VERSION = "v2"

# 星期几的中文名：把「今天」讲成模型能换算相对日期的样子。
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

DRAFT_SYSTEM_PROMPT = """你是销售助理，只负责写一条跟进话术（给销售直接发给客户用）。

只能基于用户给出的「客户事实」与「资料片段」来写，并且遵守以下要求：
1. 全文不超过 120 字，语气自然、口语化，不要罗列小标题。
2. 必须包含一个明确的下一步时间提问（例如「周四下午三点方便吗？」），不要含糊说「之后联系」。
3. 不许承诺效果、不许承诺价格、不许承诺退费、不许保证名额；不许编造资料里没有的信息。
4. 不许使用这些说法：保过、保证提分、保offer、最低价、包退、百分百。
5. 时间的硬约束，按顺序检查：
   a. **不得出现早于今天的日期**（不要写已经过去的日子去约客户）；
   b. 若提到具体时间，必须与「客户已约定时间」完全一致；没有约定、或你算不准时，
      改成开放式提问（例如「您哪天方便？」「您这周什么时候方便？」），不许自己编一个日子；
   c. 除「客户事实」「资料片段」「今天日期」「客户已约定时间」里出现过的日期与数字之外，
      不得编造任何日期、金额、人数、比例。
6. 只输出一个 JSON 对象，不要解释文字、不要 Markdown 代码块，格式固定为：
{"text": "话术正文", "used": [1, 3]}
   used 是这次实际用到的资料编号（整数，对应下方【资料片段】的编号）；一个都没用到就输出 []。"""


def build_draft_user_prompt(
    *,
    facts: list[dict | object],
    hits: list[dict],
    stage: str,
    today: str = "",
    agreed_at: str = "",
) -> str:
    """拼 user 消息：今天 + 客户事实 + 客户已约定时间 + 带编号的资料片段 + 当前阶段。

    `facts` 接受 `Fact` 实体或 `{"field", "value"}` 映射；只读这两列，
    不把置信度、锁定位这些内部状态带进提示词。

    `today` / `agreed_at` 由用例层从注入的时钟与仓储取：`today` 不是合法 `YYYY-MM-DD`
    时只写原串（不猜星期）；`agreed_at` 为空就明说「暂无约定，请用开放式提问」——
    留一句空话比让模型自己编一个日子强。
    """
    return "\n".join(
        [
            f"【今天日期】{_today_label(today) or '（未提供）'}",
            f"【客户已约定时间】{agreed_at.strip() or '（暂无约定：请用开放式提问，不要自己定日子）'}",
            "",
            f"【客户当前阶段】{stage or '未知'}",
            "",
            "【客户事实】每行 field=value：",
            _facts_summary(facts) or "（暂无）",
            "",
            "【资料片段】编号从 1 开始，`used` 只能填这里的编号：",
            _hits_summary(hits) or "（暂无）",
        ]
    )


def _today_label(today: str) -> str:
    """`YYYY-MM-DD` → `2026-11-18（周三）`；不是合法 ISO 日期就原样返回（不猜星期）。"""
    text = today.strip() if isinstance(today, str) else ""
    if not text:
        return ""
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return text
    return f"{text}（{_WEEKDAYS[day.weekday()]}）"


def _facts_summary(facts: list[dict | object]) -> str:
    lines: list[str] = []
    for fact in facts:
        field = fact.get("field") if isinstance(fact, dict) else getattr(fact, "field", None)
        value = fact.get("value") if isinstance(fact, dict) else getattr(fact, "value", None)
        if isinstance(field, str) and field.strip() and isinstance(value, str) and value.strip():
            lines.append(f"{field.strip()}={value.strip()}")
    return "\n".join(lines)


def _hits_summary(hits: list[dict]) -> str:
    lines: list[str] = []
    for index, hit in enumerate(hits, start=1):
        if not isinstance(hit, dict):
            continue
        text = hit.get("text")
        source = hit.get("source")
        if not isinstance(text, str) or not text.strip():
            continue
        source_text = source if isinstance(source, str) and source.strip() else "未命名资料"
        lines.append(f"[{index}] 来源：{source_text}\n片段：{text.strip()}")
    return "\n".join(lines)
