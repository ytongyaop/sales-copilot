"""话术生成用的提示词：版本号 + system 约束 + user 拼装。

纪律（和提炼提示词一样）：
- 提示词是**数据**：它声明「模型该输出什么形状」，但「能不能落库」由 `services/draft.py` 判；
- `DRAFT_PROMPT_VERSION` 会写进草稿记录，改一个字就是一次新版本，别偷偷改；
- 模型必须带资料编号回来（`used`），否则用例层无法判断引用是否为空，会直接拒绝。
"""

DRAFT_PROMPT_VERSION = "v1"

DRAFT_SYSTEM_PROMPT = """你是销售助理，只负责写一条跟进话术（给销售直接发给客户用）。

只能基于用户给出的「客户事实」与「资料片段」来写，并且遵守以下要求：
1. 全文不超过 120 字，语气自然、口语化，不要罗列小标题。
2. 必须包含一个明确的下一步时间提问（例如「周四下午三点方便吗？」），不要含糊说「之后联系」。
3. 不许承诺效果、不许承诺价格、不许承诺退费、不许保证名额；不许编造资料里没有的信息。
4. 不许使用这些说法：保过、保证提分、保offer、最低价、包退、百分百。
5. 只输出一个 JSON 对象，不要解释文字、不要 Markdown 代码块，格式固定为：
{"text": "话术正文", "used": [1, 3]}
   used 是这次实际用到的资料编号（整数，对应下方【资料片段】的编号）；一个都没用到就输出 []。"""


def build_draft_user_prompt(*, facts: list[dict | object], hits: list[dict], stage: str) -> str:
    """拼 user 消息：客户事实 + 带编号的资料片段 + 当前阶段。

    `facts` 接受 `Fact` 实体或 `{"field", "value"}` 映射；只读这两列，
    不把置信度、锁定位这些内部状态带进提示词。
    """
    return "\n".join(
        [
            f"【客户当前阶段】{stage or '未知'}",
            "",
            "【客户事实】每行 field=value：",
            _facts_summary(facts) or "（暂无）",
            "",
            "【资料片段】编号从 1 开始，`used` 只能填这里的编号：",
            _hits_summary(hits) or "（暂无）",
        ]
    )


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
