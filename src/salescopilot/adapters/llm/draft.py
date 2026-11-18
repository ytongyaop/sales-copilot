"""话术生成器：假实现（离线测试）与 DeepSeek 真实现。

边界与提炼器一致：
- 只有 `adapters/` 能 import SDK；`domain/` 与 `services/` 只认 `DraftGenerator` 协议；
- 密钥由装配点传入，本文件不读环境变量、不读 `.env`；
- 失败（网络、超时、解析不了）一律抛 `DraftError`（中文消息），由用例层翻译成问题。

`used_hit_indexes` 是 `hits` 的 **0 基下标**：模型输出「资料编号」从 1 开始，
适配器负责换算，用例层拿到的永远是能直接 `hits[i]` 的整数。

引用为空时的处理：模型常常「只靠客户事实就把话写完了」，于是 `used` 回 `[]`。
这时追加一段强化指令**重试一次**（最多一次）；重试还空就原样返回 `used=[]`，
由用例层拒绝——**绝不替模型挑一条资料凑引用**：编出来的引用比拒绝生成更糟。
"""

import json
import time
from datetime import date, timedelta

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

# 假实现「编日期」时写的日子：`today - 3 天`，确定是过去的日子（`11月11日`）。
FAKE_PAST_DATE_OFFSET_DAYS = 3

# 第一次写了话术却一条资料都没引用时，追加在 user 消息末尾的强化指令（只追加一次）。
NO_CITATION_HINT = (
    "【重要】你刚才没有引用任何资料。请重新写一次：话术里必须体现至少一条资料片段中的"
    "具体信息，并在 used 中给出对应编号。若确实全部无关，才允许 used 为空。"
)


class FakeDrafter:
    """确定性假生成器：把事实和第一条资料拼成一句，永远引用第 1 条资料。

    `bad_mode=True` 时故意写进禁用词（「保过」），用来反证「草稿含禁用词必须拒收」。
    `bad_dates=N` 时**前 N 次**调用故意写一个已经过去的日期（`today - 3 天`，形如 `11月11日`），
    用来反证「日期站不住要重试一次、两次都编就拒绝」；`calls` 记着被问了几次。
    引用的第一条资料如果不存在，就返回空引用——用来反证「没引用资料必须拒收」。
    """

    def __init__(self, *, bad_mode: bool = False, cite: bool = True, bad_dates: int = 0) -> None:
        self._bad_mode = bad_mode
        self._cite = cite
        self._bad_dates = bad_dates
        self.calls = 0

    def draft(
        self,
        *,
        lead: Lead,
        facts: list[Fact],
        hits: list[dict],
        today: str = "",
        agreed_at: str = "",
    ) -> DraftResult:
        self.calls += 1
        need = _first_value(facts, "need") or "你的需求"
        amount = _first_value(facts, "amount")
        hit_index, piece = _first_citable(hits) if self._cite else (None, "")
        text = f"了解到你关心「{need}」"
        if amount:
            text += f"，预算大约 {amount}"
        if piece:
            text += f"。根据资料：{piece.strip()[:60]}"
        text += f"。我们{FAKE_NEXT_STEP}再聊一次，看看是否合适？"
        if self.calls <= self._bad_dates:
            text += f"{_past_date_text(today)}现场演示方便吗？"
        elif agreed_at and self._bad_dates:
            # 重试那一次写成「与客户已约定时间一致」的日子：证明「改对了就收」这条路走得通。
            text += f"{_agreed_text(agreed_at)}那天再确认一次，方便吗？"
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
    """DeepSeek 话术生成器：要求模型回 JSON（正文 + 用到的资料编号）。

    两种重试各管一件事：
    - **调用/解析失败**：按 `max_retries` 重试（`_complete` 内部）；
    - **成功但没引用资料**（`used == []` 且 `hits` 非空）：追加强化指令再问一次（最多 1 次）。
    两次调用的 token 与耗时都累加进 `DraftResult`，成本如实反映在记录里。
    """

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

    def draft(
        self,
        *,
        lead: Lead,
        facts: list[Fact],
        hits: list[dict],
        today: str = "",
        agreed_at: str = "",
    ) -> DraftResult:
        user_prompt = build_draft_user_prompt(
            facts=list(facts),
            hits=list(hits),
            stage=lead.stage,
            today=today,
            agreed_at=agreed_at,
        )
        system_message = {"role": "system", "content": DRAFT_SYSTEM_PROMPT}

        started = time.perf_counter()
        text, used, token = self._complete(
            [system_message, {"role": "user", "content": user_prompt}]
        )

        if hits and not used:
            # 空引用大多不是模型偷懒，而是它认为「客户事实已经够写了」。给它一次带强化指令的
            # 机会；重试还空就保持 used=[]，让用例层拒绝——这里绝不替它挑资料凑引用。
            try:
                retry_text, retry_used, retry_token = self._complete(
                    [
                        system_message,
                        {"role": "user", "content": f"{user_prompt}\n\n{NO_CITATION_HINT}"},
                    ]
                )
            except DraftError:
                # 重试自己失败（网络/解析）：第一次的正文留着，仍按「未引用」拒绝，不升级成报错。
                pass
            else:
                text, used = retry_text, retry_used
                token += retry_token

        return DraftResult(
            text=text,
            used_hit_indexes=used,
            model=self._model,
            prompt_version=self._prompt_version,
            token=token,
            seconds=time.perf_counter() - started,
        )

    def _complete(self, messages: list[dict]) -> tuple[str, list[int], int]:
        """调一次模型（调用或解析失败按 `max_retries` 重试），返回（正文, 0 基引用, token）。

        只管「这次没拿到可用返回」；模型成功返回却没引用资料不算失败，交给 `draft` 判断。
        每次调用各自重试，所以两次 `_complete` 的耗时与 token 相加就是这次生成的真实成本。
        """
        attempts = 0
        reason = "未知原因"

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
            return text, used, _total_tokens(response)

        raise DraftError(f"话术生成失败：{reason}（已尝试 {attempts} 次）")


def _past_date_text(today: str) -> str:
    """`today` 往前三天的中文日期（`11月11日`）：假实现用来故意写一个过去的日子。

    `today` 不是 `YYYY-MM-DD` 时退回一个固定的过去日子（`11月15日`），
    保证「编日期」这条用例在任何时钟下都还在测同一件事。
    """
    try:
        base = date.fromisoformat(today.strip() if isinstance(today, str) else "")
    except ValueError:
        return "11月15日"
    day = base - timedelta(days=FAKE_PAST_DATE_OFFSET_DAYS)
    return f"{day.month}月{day.day}日"


def _agreed_text(agreed_at: str) -> str:
    """`2026-11-25` → `11月25日`：重试那一次按约定写日子。"""
    try:
        day = date.fromisoformat(agreed_at.strip())
    except ValueError:
        return agreed_at
    return f"{day.month}月{day.day}日"


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
