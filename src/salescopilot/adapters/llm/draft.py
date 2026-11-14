"""话术生成器：假实现（离线测试）与 DeepSeek 真实现。

边界与提炼器一致：
- 只有 `adapters/` 能 import SDK；`domain/` 与 `services/` 只认 `DraftGenerator` 协议；
- 密钥由装配点传入，本文件不读环境变量、不读 `.env`；
- 失败（网络、超时、解析不了）一律抛 `DraftError`（中文消息），由用例层翻译成问题。

`used_hit_indexes` 是 `hits` 的 **0 基下标**：模型输出「资料编号」从 1 开始，
适配器负责换算，用例层拿到的永远是能直接 `hits[i]` 的整数。
"""

import json
import time

from openai import OpenAI

from salescopilot.adapters.llm.prompts_draft import (
    DRAFT_PROMPT_VERSION,
    DRAFT_SYSTEM_PROMPT,
    build_draft_user_prompt,
)
from salescopilot.domain.errors import DraftError
from salescopilot.domain.models import DraftResult, Fact, Lead

BASE_URL = "https://api.deepseek.com"

# 写话术可以有一点变化，但别太飘：温度固定 0.3，回放时能对得上。
TEMPERATURE = 0.3

# 写进草稿记录的模型名：假模型固定 fake，不与真模型混淆。
FAKE_MODEL = "fake"

_REASON_MAX_LEN = 80

# 假实现默认的下一步时间：固定值，保证离线测试可断言。
FAKE_NEXT_STEP = "周三上午十点"


class FakeDrafter:
    """确定性假生成器：把事实和第一条资料拼成一句，永远引用第 1 条资料。

    `bad_mode=True` 时故意写进禁用词（「保过」），用来反证「草稿含禁用词必须拒收」。
    引用的第一条资料如果不存在，就返回空引用——用来反证「没引用资料必须拒收」。
    """

    def __init__(self, *, bad_mode: bool = False, cite: bool = True) -> None:
        self._bad_mode = bad_mode
        self._cite = cite

    def draft(self, *, lead: Lead, facts: list[Fact], hits: list[dict]) -> DraftResult:
        need = _first_value(facts, "need") or "你的需求"
        amount = _first_value(facts, "amount")
        hit_index, piece = _first_citable(hits) if self._cite else (None, "")
        text = f"了解到你关心「{need}」"
        if amount:
            text += f"，预算大约 {amount}"
        if piece:
            text += f"。根据资料：{piece.strip()[:60]}"
        text += f"。我们{FAKE_NEXT_STEP}再聊一次，看看是否合适？"
        if self._bad_mode:
            text += "（保过）"

        return DraftResult(
            text=text,
            used_hit_indexes=[hit_index] if hit_index is not None else [],
            model=FAKE_MODEL,
            prompt_version=DRAFT_PROMPT_VERSION,
            token=0,
            seconds=0.0,
        )


class DeepSeekDrafter:
    """DeepSeek 话术生成器：要求模型回 JSON（正文 + 用到的资料编号），解析失败即重试。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "deepseek-chat",
        prompt_version: str = DRAFT_PROMPT_VERSION,
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        self._client = OpenAI(api_key=api_key, base_url=BASE_URL, timeout=timeout)
        self._model = model
        self._prompt_version = prompt_version
        self._max_retries = max(0, int(max_retries))

    def draft(self, *, lead: Lead, facts: list[Fact], hits: list[dict]) -> DraftResult:
        messages = [
            {"role": "system", "content": DRAFT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_draft_user_prompt(
                    facts=list(facts), hits=list(hits), stage=lead.stage
                ),
            },
        ]

        started = time.perf_counter()
        attempts = 0
        reason = "未知原因"
        response = None
        text = ""
        used: list[int] = []

        for attempt in range(self._max_retries + 1):
            attempts = attempt + 1
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    temperature=TEMPERATURE,
                    response_format={"type": "json_object"},
                )
                text, used = _parse_draft(_message_text(response))
            except Exception as exc:  # noqa: BLE001 - 网络与解析失败都走同一条重试路径
                reason = _describe(exc)
                continue
            break
        else:
            raise DraftError(f"话术生成失败：{reason}（已尝试 {attempts} 次）")

        return DraftResult(
            text=text,
            used_hit_indexes=used,
            model=self._model,
            prompt_version=self._prompt_version,
            token=_total_tokens(response),
            seconds=time.perf_counter() - started,
        )


def _first_value(facts: list[Fact], field: str) -> str:
    for fact in facts:
        if fact.field == field and fact.value.strip():
            return fact.value.strip()
    return ""


def _first_citable(hits: list[dict]) -> tuple[int | None, str]:
    """挑第一条能当引用正文的片段：优先跳过只有标题的行。

    标题块（`# ...`）单看没有信息量，引到话术里只会像半句话；只有全是标题时才退回去用它。
    返回（下标, 文本）；一条都没有就是（None, ""），让用例层走「未引用资料」的拒绝分支。
    """
    fallback: tuple[int, str] | None = None
    for index, hit in enumerate(hits):
        text = hit.get("text") if isinstance(hit, dict) else None
        if not isinstance(text, str) or not text.strip():
            continue
        if fallback is None:
            fallback = (index, text)
        if not text.lstrip().startswith("#"):
            return index, text
    return fallback if fallback is not None else (None, "")


def _message_text(response: object) -> str:
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise ValueError("模型没有返回任何内容")
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("模型返回的内容为空")
    return content


def _parse_draft(text: str) -> tuple[str, list[int]]:
    """解析 `{"text": ..., "used": [...]}`；编号从 1 开始，这里统一换算成 0 基。

    编号大于资料条数这种「编出来的引用」一律丢弃——留着只会让引用列表对不上资料。
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"返回不是合法 JSON（{exc.msg}）") from exc
    if not isinstance(data, dict):
        raise TypeError("返回不是 JSON 对象")

    body = data.get("text")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("返回里缺少话术正文")

    raw_used = data.get("used", [])
    if not isinstance(raw_used, list):
        raw_used = []
    used: list[int] = []
    for item in raw_used:
        if isinstance(item, bool) or not isinstance(item, int):
            continue
        index = item - 1
        if index >= 0 and index not in used:
            used.append(index)

    return body.strip(), used


def _total_tokens(response: object) -> int:
    usage = getattr(response, "usage", None)
    total = getattr(usage, "total_tokens", 0)
    return int(total) if isinstance(total, int) and not isinstance(total, bool) else 0


def _describe(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    if len(text) > _REASON_MAX_LEN:
        text = text[:_REASON_MAX_LEN] + "…"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
