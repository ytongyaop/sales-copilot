"""运行时配置：只从进程环境变量与仓库根 `.env` 读。

三条纪律：
1. **不写死密钥**：只读 `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` 等变量，代码里不出现任何真实 key；
2. **`.env` 可选**：没有 `.env` 就只读环境变量，不报错、不打印密钥；
3. **空值当没配**：`.env.example` 里留空的变量不能冒充有效配置，否则「配置齐了」的判定就失效。

`live` 开关（环境变量 `SALESCOPILOT_LIVE`）决定这次是否接真模型：
默认假模型（离线、不花钱），只有显式开才走真模型；开了但缺 key 一律报错，不静默退回。

谁调 `load_settings()`：只有 `entrypoints/bootstrap.py`（装配点），其它层拿组装好的对象。
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_CHAT_MODEL = "deepseek-chat"
DEFAULT_EMBED_MODEL = "text-embedding-v3"
# 真模型开关的取值：只认这三种真值写法，其余（含空值）都是关。
_TRUE_FLAGS = frozenset({"1", "true", "yes"})


class ConfigError(Exception):
    """配置缺失或非法；消息直接给人看（中文），不带密钥内容。"""


@dataclass(frozen=True)
class Settings:
    """一次运行所需的配置快照；缺项用 None 表示「没配」，不编默认值。"""

    live: bool
    deepseek_api_key: str | None
    chat_model: str
    dashscope_api_key: str | None
    embed_model: str


def load_settings() -> Settings:
    """读仓库根的 `.env`（存在才读、不覆盖已有环境变量），再取环境变量组装 Settings。"""
    load_dotenv(env_path(), override=False)
    return Settings(
        live=_flag(os.environ.get("SALESCOPILOT_LIVE")),
        deepseek_api_key=_text(os.environ.get("DEEPSEEK_API_KEY")),
        chat_model=_text(os.environ.get("CHAT_MODEL")) or DEFAULT_CHAT_MODEL,
        dashscope_api_key=_text(os.environ.get("DASHSCOPE_API_KEY")),
        embed_model=_text(os.environ.get("EMBED_MODEL")) or DEFAULT_EMBED_MODEL,
    )


def require_chat_key(settings: Settings) -> str:
    """要真模型时必须先有 key；缺失直接抛业务语言错误，不偷偷退回假模型。"""
    if not settings.deepseek_api_key:
        raise ConfigError("缺少 DEEPSEEK_API_KEY：请在项目根 .env 中配置")
    return settings.deepseek_api_key


def require_embed_key(settings: Settings) -> str:
    """要真向量时必须先有 key；缺失直接抛业务语言错误，不偷偷退回假向量。"""
    if not settings.dashscope_api_key:
        raise ConfigError("缺少 DASHSCOPE_API_KEY：请在项目根 .env 中配置")
    return settings.dashscope_api_key


def env_path() -> Path:
    """仓库根 `.env` 的位置：`src/salescopilot/config.py` 往上两级就是仓库根。"""
    return Path(__file__).resolve().parents[2] / ".env"


def _text(raw: str | None) -> str | None:
    """去空白；空串与纯空白都当「没配」。"""
    if raw is None:
        return None
    return raw.strip() or None


def _flag(raw: str | None) -> bool:
    """布尔开关：`"1" / "true" / "yes"`（忽略大小写与首尾空白）为真，其余（含没配）为假。"""
    text = _text(raw)
    return text.lower() in _TRUE_FLAGS if text else False
