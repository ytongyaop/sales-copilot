"""空引用重试：不联网，用假 client 回放「同一个问题问两次」的两种结局。

三条行为（对应 B9.6 的验收）：
1. 第一次 `used=[]`、第二次 `used=[1]` → 采用第二次（换算成 0 基下标），token 是两次之和；
2. 两次都 `used=[]` → 最终仍是 `[]`（由用例层拒绝，适配器不替模型挑资料）；
3. 第一次就引用了 → 只调用一次，不白花第二遍的钱。
"""

from types import SimpleNamespace

from salescopilot.adapters.llm.draft import NO_CITATION_HINT, DeepSeekDrafter
from salescopilot.domain.models import INITIAL_STAGE, Fact, Lead

HITS = [
    {"text": "退费在受理后 15 个工作日内原路退回。", "source": "service_policy.md", "score": 0.91},
    {"text": "试听需提前 1 天预约。", "source": "service_policy.md", "score": 0.72},
]

LEAD = Lead(
    id="lead-1",
    name="线索-测试",
    contact="13800001111",
    source="inbound",
    stage=INITIAL_STAGE,
    owner=None,
    created_at="2026-11-16T09:00:00+00:00",
)

FACTS = [
    Fact(
        id="fact-need",
        lead_id="lead-1",
        raw_id="",
        field="need",
        value="想了解退费规则",
        source="manual",
        source_span="(人工录入)",
        confidence=1.0,
        locked=True,
        at="2026-11-16T09:30:00+00:00",
    )
]


class StubClient:
    """假 OpenAI client：按脚本回放（content, total_tokens），并记下每次请求的 messages。

    结构与真 SDK 一致：`client.chat.completions.create(...)` 返回带
    `choices[0].message.content` 与 `usage.total_tokens` 的对象。
    """

    def __init__(self, replies: list[tuple[str, int]]) -> None:
        self.calls: list[list[dict]] = []
        self._replies = list(replies)
        self.chat = SimpleNamespace(completions=self)

    def create(self, *, model: str, messages: list[dict], temperature: float, response_format):
        self.calls.append([dict(message) for message in messages])
        content, total_tokens = self._replies.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(total_tokens=total_tokens),
        )


def _drafter(replies: list[tuple[str, int]]) -> tuple[DeepSeekDrafter, StubClient]:
    """造一个不联网的生成器：构造完把内部 client 换成假实现（不产生任何请求）。"""
    drafter = DeepSeekDrafter(api_key="test-key")
    client = StubClient(replies)
    drafter._client = client
    return drafter, client


def test_retries_once_and_adopts_the_second_answer() -> None:
    drafter, client = _drafter(
        [
            ('{"text": "第一次只写了客户事实", "used": []}', 100),
            ('{"text": "第二次引用了资料", "used": [1]}', 120),
        ]
    )

    result = drafter.draft(lead=LEAD, facts=FACTS, hits=HITS)

    assert len(client.calls) == 2
    assert result.text == "第二次引用了资料"
    assert result.used_hit_indexes == [0]
    assert result.token == 220
    assert result.seconds >= 0.0
    # 强化指令只出现在第二次的 user 消息末尾（第一次是干净的原提示词）
    assert NO_CITATION_HINT not in client.calls[0][1]["content"]
    assert client.calls[1][1]["content"].endswith(NO_CITATION_HINT)


def test_keeps_empty_used_when_both_attempts_cite_nothing() -> None:
    drafter, client = _drafter(
        [
            ('{"text": "第一次没有引用", "used": []}', 100),
            ('{"text": "第二次还是没有引用", "used": []}', 110),
        ]
    )

    result = drafter.draft(lead=LEAD, facts=FACTS, hits=HITS)

    assert len(client.calls) == 2
    assert result.used_hit_indexes == []
    assert result.token == 210


def test_does_not_retry_when_the_first_answer_cites() -> None:
    drafter, client = _drafter([('{"text": "一次就引用了", "used": [1]}', 100)])

    result = drafter.draft(lead=LEAD, facts=FACTS, hits=HITS)

    assert len(client.calls) == 1
    assert result.used_hit_indexes == [0]
    assert result.token == 100
