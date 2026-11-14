"""真模型提炼器的**离线**测试：用假 client 顶替 `openai.OpenAI`，一次网络都不发。

三条主线：
1. 正常 JSON → 候选可解析，且 token / seconds / raw_output_hash 都被填上；
2. JSON 坏 → 按 `max_retries` 重试，仍失败抛 `ExtractError`（中文、含尝试次数）；
3. 提示词按契约拼：system 是固定常量，user 里带原文与「field=value」事实摘要。
"""

import hashlib
from types import SimpleNamespace

import pytest

from salescopilot.adapters.llm import deepseek
from salescopilot.adapters.llm.deepseek import DeepSeekExtractor
from salescopilot.domain.errors import ExtractError
from salescopilot.domain.models import Fact, RawItem

RAW = RawItem(
    id="raw-1",
    lead_id="lead-1",
    source="inbound",
    content="客户说太贵了，下周再谈",
    content_hash="input-hash",
    at="2026-11-14T10:00:00+00:00",
)

EXISTING_FACT = Fact(
    id="fact-1",
    lead_id="lead-1",
    raw_id="raw-0",
    field="objection",
    value="price",
    source="llm",
    source_span="贵",
    confidence=0.8,
    locked=False,
    at="2026-11-14T09:00:00+00:00",
)

GOOD_TEXT = (
    '{"facts": [{"field": "objection", "value": "price", "source_span": "太贵", '
    '"confidence": 0.9}]}'
)


def _response(text: str, token: int = 42) -> SimpleNamespace:
    """仿 OpenAI 响应：只要 choices[0].message.content 与 usage.total_tokens。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(total_tokens=token),
    )


class _FakeChatCompletions:
    """按顺序吐预设回复；只剩一条时永远吐同一条（模拟「一直坏」）。"""

    def __init__(self, replies: list[object]) -> None:
        self.calls: list[dict] = []
        self._replies = list(replies)

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


class _FakeClient:
    """`OpenAI(...)` 的替身：只暴露 chat.completions.create。"""

    def __init__(self, replies: list[object]) -> None:
        self.chat = SimpleNamespace(completions=_FakeChatCompletions(replies))


@pytest.fixture
def install_client(monkeypatch: pytest.MonkeyPatch):
    """把 `deepseek.OpenAI` 换成假工厂；返回（假 client, 构造时收到的 kwargs）。"""

    def _install(replies: list[object]) -> tuple[_FakeClient, dict]:
        client = _FakeClient(replies)
        captured: dict = {}

        def _factory(**kwargs: object) -> _FakeClient:
            captured.update(kwargs)
            return client

        monkeypatch.setattr(deepseek, "OpenAI", _factory)
        return client, captured

    return _install


def _extractor(**kwargs: object) -> DeepSeekExtractor:
    params = {"api_key": "sk-test-not-a-real-key"}
    params.update(kwargs)
    return DeepSeekExtractor(**params)


def test_parses_json_and_records_token_seconds_and_hash(install_client) -> None:
    client, init_kwargs = install_client([_response(GOOD_TEXT, token=42)])

    result = _extractor().extract(raw=RAW, facts=[])

    assert result.candidates == [
        {"field": "objection", "value": "price", "source_span": "太贵", "confidence": 0.9}
    ]
    assert result.model == "deepseek-chat"
    assert result.prompt_version == "v1"
    assert result.token == 42
    assert result.seconds > 0
    assert result.raw_output_hash == hashlib.sha256(GOOD_TEXT.encode("utf-8")).hexdigest()

    # 客户端按契约建：指向 DeepSeek 的 OpenAI 兼容地址
    assert init_kwargs["base_url"] == "https://api.deepseek.com"
    assert init_kwargs["api_key"] == "sk-test-not-a-real-key"

    sent = client.chat.completions.calls[0]
    assert sent["model"] == "deepseek-chat"
    assert sent["temperature"] == 0
    assert sent["response_format"] == {"type": "json_object"}


def test_bad_json_retries_then_raises_with_chinese_message(install_client) -> None:
    client, _ = install_client([_response("这不是 JSON", token=0)])

    with pytest.raises(ExtractError, match="提炼失败"):
        _extractor(max_retries=2).extract(raw=RAW, facts=[])

    # 首次 + 2 次重试 = 3 次
    assert len(client.chat.completions.calls) == 3


def test_network_error_is_retried_then_succeeds(install_client) -> None:
    client, _ = install_client([OSError("连接被重置"), _response(GOOD_TEXT, token=7)])

    result = _extractor(max_retries=1).extract(raw=RAW, facts=[])

    assert len(client.chat.completions.calls) == 2
    assert result.candidates[0]["field"] == "objection"
    assert result.token == 7


def test_missing_facts_key_also_fails(install_client) -> None:
    install_client([_response('{"items": []}', token=0)])

    with pytest.raises(ExtractError, match="已尝试 1 次"):
        _extractor(max_retries=0).extract(raw=RAW, facts=[])


def test_user_prompt_carries_content_and_fact_summary(install_client) -> None:
    client, _ = install_client([_response(GOOD_TEXT, token=1)])

    _extractor().extract(raw=RAW, facts=[EXISTING_FACT])

    messages = client.chat.completions.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == deepseek.SYSTEM_PROMPT
    assert messages[1]["role"] == "user"
    assert RAW.content in messages[1]["content"]
    assert "objection=price" in messages[1]["content"]


def test_system_prompt_states_the_hard_constraints() -> None:
    """提示词是契约的一部分：这几条字面要求丢了，落库规则就形同虚设。"""
    prompt = deepseek.SYSTEM_PROMPT

    assert "source_span" in prompt
    assert "逐字" in prompt
    assert "objection" in prompt
    assert "price / time / effect / trust / other" in prompt
    assert "不预测成交" in prompt
