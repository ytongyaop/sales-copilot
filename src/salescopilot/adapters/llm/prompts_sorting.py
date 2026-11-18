"""分拣判断用的提示词：版本号 + system 约束 + user 拼装。

纪律（与提炼 / 话术提示词一致）：
- 提示词是**数据**：它声明「模型该输出什么形状」，但「能不能落库」由
  `domain/sorting.py` 判——尤其是 `source_span`，对不上原文的要素一律丢弃；
- `SORTING_PROMPT_VERSION` 会随分拣单一起留痕，改一个字就是一次新版本；
- 只用 `json_object` 模式（DeepSeek 官方接口不支持 `json_schema` 严格模式，会 400）；
- **今天日期由调用方传入**（`YYYY-MM-DD`）：本模块不取时钟，只负责把它讲进提示词。
"""

from datetime import date

from salescopilot.domain.matching import Candidate

# v2：加入「今天日期」与「时间字段必须 ISO 日期」两条约束（相对日期「下周三」→ 具体日期）。
# v3：讲清 `value_offered` 是「给对方的东西」（资料/案例/方案/测评），不能是日期、金额、客户名。
SORTING_PROMPT_VERSION = "v3"

# 星期几的中文名：只用来把「今天」讲清楚，模型据此把「下周三」换算成具体日期。
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

SORTING_SYSTEM_PROMPT = """你是销售流程里的分拣员，只负责把一段原文拆成三块结构化建议。

输出要求（只输出一个 JSON 对象，不要解释文字，不要 Markdown 代码块）：
{"elements":[{"card_section":"basic|needs|decision|followup","field":"字段名","value":"取值","source_span":"原文逐字片段","confidence":0.0}],
 "grading":{"value":"A|B|C","reason":"分级理由","basis":["原文逐字片段"]},
 "next_action":{"action":"下一步动作","value_offered":"这次能给对方什么","due_hint":"时间线索"}}

硬约束，按顺序检查：
1. `source_span` 必须是原文里**逐字出现**的片段（不要改写、不要补主语、不要翻译），
   长度至少 4 个字；在原文里找不到就**不要输出这一条要素**，宁可少给也不许编。
2. `card_section` 只能是四个值之一：`basic`（基础信息）、`needs`（需求与痛点）、
   `decision`（决策流程与预算）、`followup`（跟进记录）。
3. `field` 用通用字段名（例如 contact / need / decision_maker / appointment_at / amount），
   不要自造业务叫法；`value` 是这条要素的结论，`source_span` 是支撑它的话。
4. `confidence` 是 0 到 1 之间的小数。
5. `grading.value` 只能是 A / B / C；`basis` 里每条也必须能在原文里逐字找到。
6. `next_action.action` 必须具体（下一次做什么）；`value_offered` 是**我们准备提供给客户的
   具体东西**（资料 / 案例 / 方案 / 测评 / 报价区间说明这类），**不能是日期、金额、客户名**
   ——时间是 `due_hint` 的事，钱与名字都不是「给的东西」；原文里没有可给的东西就写
   `"value_offered": ""`。时间线索能确定就写进 `due_hint`，不确定就留空字符串。
7. 原文里没有的信息一律不写；不确定就少给，不要凑数。
8. 时间类字段（`appointment_at`，以及模板里 label 含「时间」「日期」的字段）的 `value`
   必须是 `YYYY-MM-DD`：把「下周三」「明天」这类说法按开头给的今天日期换算成具体日期；
   算不出具体日期就**不要输出这一条要素**——不许把「下周三」原样当取值。
9. 第 8 条的 `source_span` 照样是原文里逐字出现的片段（例如「下周三下午再聊」）：
   `value` 是换算后的日期，`source_span` 是支撑它的原话，两者可以不同。"""


def _today_label(today: str) -> str:
    """`YYYY-MM-DD` → `2026-11-18（周三）`；不是合法 ISO 日期就原样返回（不猜星期）。"""
    text = today.strip() if isinstance(today, str) else ""
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return text
    return f"{text}（{_WEEKDAYS[day.weekday()]}）"


def build_sorting_system_prompt(today: str) -> str:
    """拼 system 提示：开头写清「今天」是几号星期几，相对日期才换算得出来。

    日期由调用方（用例层）从时钟取；本模块不取时钟，解析不了就只写原串、不猜星期。
    """
    return f"今天是 {_today_label(today)}。\n\n" + SORTING_SYSTEM_PROMPT


def build_sorting_user_prompt(*, content: str, candidates: list[Candidate], today: str) -> str:
    """拼 user 消息：原文 + （灰区时）归属候选 + 今天日期。

    `candidates` 非空只说明「这票货可能是这几家」，不是结论：模型可以在
    `basis` / `reason` 里参考，但归属由确定性漏斗决定，这里不要求模型改归属。
    末尾附今天日期：模型换算「下周三」这类相对说法要先知道今天是哪天。
    """
    lines = ["【原文】以下是这次要分拣的原文，`source_span` 只能从这里逐字截取：", content]
    if candidates:
        lines.extend(
            [
                "",
                "【可能是这几家】（按相似度排序，仅作参考，不要据此改写原文）：",
            ]
        )
        for index, candidate in enumerate(candidates, start=1):
            lines.append(f"{index}. {candidate.name}（相似度 {candidate.score:.1f}）")
    lines.extend(["", f"今天日期：{_today_label(today)}"])
    return "\n".join(lines)


# ------------------------------------------------------------------ 灰区复核（T004）

# 复核只产出一句话建议，与三块分拣建议分开版本：改复核提示词不影响分拣留痕。
REVIEW_PROMPT_VERSION = "v1"

REVIEW_SYSTEM_PROMPT = """你在做一次归属复核：只判断这段原文最可能属于给定候选里的哪一家。

输出要求（只输出一个 JSON 对象，不要解释文字，不要 Markdown 代码块）：
{"lead_id":"候选里的客户 id，挑不出来就 null","reason":"一句话理由，必须引用原文里的字句"}

硬约束：
1. 只能从给定候选里挑 id；挑不出来就填 null，理由写清缺什么，**不要编造客户**；
2. 理由不超过 60 字，且依据必须是原文里真实出现过的字句；
3. 你给的是建议，不是结论：最终归属由人认领。"""


def build_review_user_prompt(*, content: str, candidates: list[Candidate]) -> str:
    """拼复核的 user 消息：原文 + 候选清单（带 id，模型只能从这里挑）。"""
    lines = ["【原文】", content, "", "【候选客户】只能从下面这个清单里挑 id："]
    for index, candidate in enumerate(candidates, start=1):
        lines.append(
            f"{index}. id={candidate.lead_id}　名称={candidate.name}　相似度={candidate.score:.1f}"
        )
    return "\n".join(lines)
