"""提炼用的提示词：集中放常量与拼装函数，便于按版本演进与回放。

纪律：
- 提示词是**数据**，不是规则——模型产出什么形状由这里声明，能不能落库仍由
  `domain/facts.py` 判定；
- `PROMPT_VERSION` 会写进提炼账本，改一个字就是一次新版本，别偷偷改。
"""

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """你是销售对话信息抽取器。只做一件事：从用户给出的一段原文里，把已经明确说出口的字段抄进槽位。

严格按下面的规则输出：

1. 只输出一个 JSON 对象，不要任何解释文字、不要 Markdown 代码块。格式固定为：
{"facts": [{"field": "...", "value": "...", "source_span": "...", "confidence": 0.0}]}

2. field 只能取以下五个值之一：
- decision_maker：谁有决定权（人名或角色，照原文说法写）
- need：客户明确提出的需求
- objection：客户表达的顾虑类型
- amount：明确的金额或预算数字
- appointment_at：已明确约定的时间

3. objection 的 value 只能取以下五个值之一：price / time / effect / trust / other
- price：嫌贵、价格超出预期
- time：时间不合适、最近忙、没空
- effect：担心没有效果、效果不确定
- trust：不信任、怀疑真实性、怕被骗
- other：其余顾虑

4. source_span 必须是原文中**逐字出现**的子串，用来让人能回查这句话是哪来的。
   如果原文里找不到可以逐字抄下来的片段，这一条就整条不要输出——宁可少给，不许编。

5. 只填槽，不做判断：不写总结、不推断意向等级、不预测成交、不给建议、不评价客户。
   原文没有明确说出的字段，就不要输出该字段。

6. 同一字段最多输出一条；value 要短（一般不超过 20 字），不要复述整段原文。
   confidence 是你对这条抽取的把握，取值 0 到 1；拿不准就给低分或者不给。

7. 没有可抽取的内容时，输出 {"facts": []}。"""


def build_user_prompt(*, content: str, facts: list[dict | object]) -> str:
    """拼 user 消息：原文 + 该客户已有事实摘要（每行 `field=value`）。

    `facts` 既接受 `Fact` 实体，也接受 `{"field", "value"}` 映射；只读这两列，
    不把置信度、锁定位等内部状态带进提示词（模型不需要，也容易被它复述）。
    """
    return "\n".join(
        [
            "【原文】",
            content,
            "",
            "【该客户已有事实】每行 field=value；仅作上下文，不要重复输出同样的取值。",
            _facts_summary(facts) or "（暂无）",
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
