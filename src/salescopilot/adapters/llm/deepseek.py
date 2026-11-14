"""真模型提炼器：DeepSeek Chat，走 OpenAI 兼容协议。

边界：
- **只有这里和 `adapters/` 其它文件能 import SDK**；`domain/` 与 `services/` 不认识 openai；
- 网络/超时/解析失败一律重试 `max_retries` 次，仍失败抛 `ExtractError`（中文消息）；
- 每次成功都把模型名、提示词版本、token、耗时、返回文本哈希带回给用例层记账。

密钥由装配点（`entrypoints/bootstrap.py`）传入，本文件不读环境变量、不读 `.env`。
"""

import hashlib
import json
import time

from openai import OpenAI

from salescopilot.adapters.llm.prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from salescopilot.domain.errors import ExtractError
from salescopilot.domain.models import ExtractionResult, Fact, RawItem

BASE_URL = "https://api.deepseek.com"

# 单条失败原因最多带这么多字符：够定位问题，又不至于把整段响应灌进库里。
_REASON_MAX_LEN = 80


class DeepSeekExtractor:
    """按固定 JSON 契约抽槽的提炼器；不做取舍、不落库，只吐候选与证据。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "deepseek-chat",
        prompt_version: str = PROMPT_VERSION,
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        """建一个 OpenAI 兼容客户端；`max_retries` 是**失败后的额外尝试次数**。"""
        self._client = OpenAI(api_key=api_key, base_url=BASE_URL, timeout=timeout)
        self._model = model
        self._prompt_version = prompt_version
        self._max_retries = max(0, int(max_retries))

    def extract(self, *, raw: RawItem, facts: list[Fact]) -> ExtractionResult:
        """对一段原文跑一次抽取；返回候选与本次耗用，失败抛 `ExtractError`。"""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(content=raw.content, facts=list(facts))},
        ]

        started = time.perf_counter()
        attempts = 0
        reason = "未知原因"
        response = None
        text = ""
        candidates: list[dict] = []

        for attempt in range(self._max_retries + 1):
            attempts = attempt + 1
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    temperature=0,
                    response_format={"type": "json_object"},
                )
                text = _message_text(response)
                candidates = _parse_candidates(text)
            except Exception as exc:  # noqa: BLE001 - 网络与解析失败都走同一条重试路径
                reason = _describe(exc)
                continue
            break
        else:
            raise ExtractError(f"提炼失败：{reason}（已尝试 {attempts} 次）")

        seconds = time.perf_counter() - started
        return ExtractionResult(
            candidates=candidates,
            model=self._model,
            prompt_version=self._prompt_version,
            token=_total_tokens(response),
            seconds=seconds,
            raw_output_hash=_sha256(text),
        )


def _message_text(response: object) -> str:
    """取出第一条候选的文本内容；形状不对就当作失败（会触发重试）。"""
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise ValueError("模型没有返回任何内容")
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("模型返回的内容为空")
    return content


def _parse_candidates(text: str) -> list[dict]:
    """把返回文本解析成候选列表；结构不对就是失败（会触发重试），不猜、不修补。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"返回不是合法 JSON（{exc.msg}）") from exc

    if not isinstance(data, dict):
        raise TypeError("返回不是 JSON 对象")
    facts = data.get("facts")
    if not isinstance(facts, list):
        raise TypeError("返回缺少 facts 列表")
    return facts


def _total_tokens(response: object) -> int:
    """读 usage.total_tokens；协议没给就记 0（账本里 0 表示「不知道」，不是免费）。"""
    usage = getattr(response, "usage", None)
    total = getattr(usage, "total_tokens", 0)
    return int(total) if isinstance(total, int) and not isinstance(total, bool) else 0


def _describe(exc: Exception) -> str:
    """把异常压成一句可入库的原因；截断避免把整段响应灌进来。"""
    text = " ".join(str(exc).split())
    if len(text) > _REASON_MAX_LEN:
        text = text[:_REASON_MAX_LEN] + "…"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
