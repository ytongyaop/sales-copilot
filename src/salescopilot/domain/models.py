"""领域实体与通用取值集合（纯数据，无 IO、无校验逻辑）。

这里只回答「一条线索长什么样」以及引擎认的通用枚举；校验一律在 `domain/rules.py`，
落库一律在 `adapters/db`。实体里不含任何行业叫法。
"""

from dataclasses import dataclass

# 新线索的初始阶段：与流程块契约的首块同名（见 `domain/blocks.py` 的 FIRST_BLOCK_ID）。
INITIAL_STAGE = "intake"

# 线索来源只认这几个通用入口；扩展枚举要改这里，不要在别处另写一份。
ALLOWED_SOURCES: tuple[str, ...] = ("referral", "inbound", "outbound", "event", "other")


@dataclass(frozen=True)
class Lead:
    """一条线索。

    - `stage`：当前阶段，取值是流程块 id。
    - `owner`：归属人；None 表示尚未分配。
    - `created_at`：ISO8601 字符串，由调用方注入的时钟产生（实体自己不碰时钟）。
    """

    id: str
    name: str
    contact: str
    source: str
    stage: str
    owner: str | None
    created_at: str
