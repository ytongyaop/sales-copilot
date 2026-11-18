"""分拣判断用的提示词：版本号 + system 约束 + user 拼装。

纪律（与提炼 / 话术提示词一致）：
- 提示词是**数据**：它声明「模型该输出什么形状」，但「能不能落库」由
  `domain/sorting.py` 判——尤其是 `source_span`，对不上原文的要素一律丢弃；
- `SORTING_PROMPT_VERSION` 会随分拣单一起留痕，改一个字就是一次新版本；
- 只用 `json_object` 模式（DeepSeek 官方接口不支持 `json_schema` 严格模式，会 400）。
"""

from salescopilot.domain.matching import Candidate

SORTING_PROMPT_VERSION = "v1"

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
6. `next_action.action` 必须具体（下一次做什么），`value_offered` 不许为空；
   时间线索能确定就写进 `due_hint`，不确定就留空字符串。
7. 原文里没有的信息一律不写；不确定就少给，不要凑数。"""


def build_sorting_user_prompt(*, content: str, candidates: list[Candidate]) -> str:
    """拼 user 消息：原文 + （灰区时）归属候选。

    `candidates` 非空只说明「这票货可能是这几家」，不是结论：模型可以在
    `basis` / `reason` 里参考，但归属由确定性漏斗决定，这里不要求模型改归属。
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
    return "\n".join(lines)
